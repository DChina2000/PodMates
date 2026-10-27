"""学表情包：只学群里「常用」的，发的时机由主模型在回复时决定（工具调用）。

- 常用：同一张图（按画面内容算指纹，近似图算同一张）被多人或多次发过才学，阈值可设。
- 学：用看图模型出描述（字数上限在设置里，默认 250）并判断是不是适合的表情包（含不宜内容的不学），描述存成「表情包」类记忆、算向量；图片文件存本机。
  每张记下是按多少字描述的（desc_max）；设置里的字数改了，已有的在后台自动重新描述（redescribe）。
- 存：按记忆空间（默认按群）；每个群有张数上限，全体有容量上限，满了先删用得最少的。
- 发：模型调用 send_sticker(mood)，按心情在本群的表情包里找出几张候选，把描述给她看；她在回复里写【表情N】挑一张，跟在文字后面发出去，不写就不发。
- 收藏：QQ 小号收藏的表情（NapCat 的 fetch_custom_face）定期同步下来，各写一句描述；属于这个账号，它所在的所有 QQ 群都能用，
  不占「每个群最多存几张」，满了先删用得最少的时也只删学来的；从收藏里删掉的，这里跟着删。
  微信小号的「自定义表情」同理：没有下载接口，由 wxsticker 打开微信的表情面板一格一格翻着看（记每张的画面指纹和位置，放大预览截下来交给看图模型写描述），
  目录存在 kv 的 wxfav:{账号} 里；发的时候也是翻到那一格、对上指纹再点。
"""
import hashlib
import io
import json
import sqlite3
import time
from pathlib import Path

from .llm import UsageCapReached
from .memory import parse_json
from .paths import data_dir

NEAR = 6  # 指纹相差几位以内算同一张
MAX_BYTES = 6 * 1024 * 1024
FAV_COUNT = 500  # 每次向 NapCat 要多少张收藏（fetch_custom_face 的 count，它自己默认 48）
FAV_BATCH = 40  # 一次同步最多新描述几张（第一次同步收藏很多时，剩下的下一轮接着做）
FAV_PLATFORMS = ("qq", "wechat")  # 有「账号收藏的表情」的平台
OLD_DESC_MAX = 40  # 没记下 desc_max 的旧记录，是按这个字数描述的
DESC_FOCUS = "画面里是什么，图上的文字照原样写出（没有就不写），表达什么情绪，适合在什么场合发"
# 提示词里的 {n} 换成设置的字数（不用 format：VISION_PROMPT 里有 JSON 的花括号）
FAV_PROMPT = "这是聊天账号收藏的一张表情。用中文（{n}字内）写清：" + DESC_FOCUS + "。只输出这段描述。"
GROUP_PROMPT = "这是群聊里常用的一张表情包。用中文（{n}字内）写清：" + DESC_FOCUS + "。只输出这段描述。"
QUERY_PROMPT = "这是一张表情包。为了查清它的出处或含义，给出一个最合适的搜索词（图上有字就优先用图上的字）。只输出搜索词。"
SEARCH_REF = "\n\n下面是联网搜索的结果，只作参考；和画面对不上的不要用。\n"
SEARCH_HINT = "\n\n认不出这张图的出处或含义时，可以用 web_search 查一下；搜到的只作参考，和画面对不上的不要用。"
VISION_PROMPT = ("这是群聊里有人发的一张图。判断它是不是群友常用的表情包或梗图，并给一句描述。只输出JSON：\n"
                 '{"sticker": true或false, "ok": true或false, "desc": "中文，{n}字内：' + DESC_FOCUS + '"}\n'
                 "sticker：是表情包/梗图/贴纸（不是普通照片、聊天截图、二维码、文档）。\n"
                 "ok：内容适合所有人看（没有色情、血腥暴力、仇恨歧视、广告引流、真人隐私、政治敏感）；不确定就写 false。")


def dhash(data: bytes) -> int | None:
    """9×8 灰度差分指纹（64 位）。动图取第一帧。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        im = im.convert("L").resize((9, 8))
        px = list(im.tobytes())
    except Exception:
        return None
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def ext_of(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def tool_spec() -> dict:
    return {"name": "send_sticker",
            "description": "按心情找几张这个群里常用的表情包给你挑，选中的跟在你的文字后面发出去。只在气氛合适、一张图比一句话更到位时才用，别每句都发。",
            "parameters": {"type": "object", "properties": {"mood": {"type": "string", "description": "你想表达的心情或场合，几个字，如「无语」「开心」「嘲讽」「晚安」"}},
                           "required": ["mood"]}}


BLOCKED = "\0blocked"  # _describe_fav 的描述位：被内容审核拦下（这张记成 blocked，不收，以后不再试）


class ContentBlocked(Exception):
    """给表情包写描述被服务商的内容审核拦下，重试和去掉搜索都没过：调用方停手，不再试这张。"""


def is_blocked(ex) -> bool:
    """服务商的内容审核拦截（智谱 / Z.ai 回 1301 和 contentFilter）。"""
    s = str(ex)
    return "contentFilter" in s or '"1301"' in s


class Stickers:
    def __init__(self, engine):
        self.e = engine
        self.db = engine.db
        self.busy: set[str] = set()

    def desc_max(self) -> int:
        """设置里的表情包描述字数上限（看图模型写描述的字数、存的时候截的字数）。"""
        return max(1, int(self.e.settings.g("sticker_desc_max") or 250))

    async def vis_ask(self, bot, data: bytes, prompt: str, platform, chat) -> str:
        """写描述，遇到服务商的内容审核拦截：带搜索时原样再试「内容审核拦下时重试几次」次，还不行就去掉搜索再试
        「内容审核拦下后不带搜索再试几次」次（本来就不搜的，从这一段开始）；都被拦就抛 ContentBlocked，由调用方停手。"""
        g = self.e.settings.g
        plan = ([True] * (1 + int(g("sticker_block_retry") or 0)) if self._will_search(bot) else [False]) \
            + [False] * int(g("sticker_block_retry_plain") or 0)
        last = None
        for search in plan:
            try:
                return await self._vis_once(bot, data, prompt, platform, chat, search)
            except UsageCapReached:
                raise
            except Exception as ex:
                if not is_blocked(ex):
                    raise
                last = ex
        raise ContentBlocked(str(last)[:200])

    def _will_search(self, bot) -> bool:
        g = self.e.settings.g
        return bool(g("sticker_desc_search")) and int(g("sticker_desc_search_max") or 0) > 0 \
            and self.e.search.available(False)[0] and not self.e.llm.hard_capped(bot.rid)

    async def _vis_once(self, bot, data: bytes, prompt: str, platform, chat, search: bool) -> str:
        """给一张表情包写描述：看图模型按 prompt 答。设置里打开了「给表情包写描述时联网搜索」、又有可用的搜索服务、看图模型也接受工具时，
        先搜再答（默认：先问出一个搜索词、搜一次，再带着结果和 web_search 工具写描述；让看图模型自己判断：直接写，认不出时它自己搜）；
        搜不了、模型不接受工具、搜索出错都照常只看图。用量记「表情包」，搜索本身记「表情包搜索」，不算进每天的搜索次数。"""
        import base64
        e = self.e
        g = e.settings.g
        durl = f"data:image/{ext_of(data).replace('jpg', 'jpeg')};base64," + base64.b64encode(data).decode()

        def msgs(text):
            return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": durl}}, {"type": "text", "text": text}]}]
        rounds = int(g("sticker_desc_search_max") or 0)
        if not search:
            return await e.llm.chat("vis", msgs(prompt), bot.rid, platform, chat, tag="sticker")
        kw = dict(tag="sticker", search_tag="sticker_search", counted=False)
        if g("sticker_desc_search_auto"):
            return await e.search.vis_chat(msgs(prompt + SEARCH_HINT), bot.rid, platform, chat, rounds=rounds, **kw)
        try:  # 第一步：先不带工具，让看图模型给出搜索词
            query = (await e.llm.chat("vis", msgs(QUERY_PROMPT), bot.rid, platform, chat, tag="sticker")).strip().split("\n")[0]
            query = query.replace("*", "").replace("`", "").strip("\"'“”「」# ")  # 模型有时给搜索词套上 **粗体** 之类的记号
        except UsageCapReached:
            raise
        except Exception as ex:
            e.log("warn", "sticker", f"表情包搜索词没问出来，只看图：{type(ex).__name__}: {ex}")
            query = ""
        found, text = await e.search.lookup(query, bot.rid, platform, chat, "sticker_search", False) if query else (False, "")
        if not found:
            return await e.llm.chat("vis", msgs(prompt), bot.rid, platform, chat, tag="sticker")
        return await e.search.vis_chat(msgs(prompt + SEARCH_REF + text), bot.rid, platform, chat, rounds=rounds - 1, **kw)

    # ---------- 位置 ----------
    def space(self, room) -> str:
        return self.e.memory.space("sticker", None, room.platform, room.key)

    @staticmethod
    def folder() -> Path:
        d = data_dir() / "stickers"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def count(self, space: str) -> int:
        return self.db.x("SELECT COUNT(*) FROM stickers WHERE space=? AND status='learned'", (space,)).fetchone()[0]

    def available(self, room, bot=None) -> bool:
        """room 是 None（私聊）：只有这个账号自己的收藏可用，私聊没有学来的表情包。"""
        return bool(room and self.count(self.space(room)) > 0) or bool(bot and self.fav_on(bot, room) and self.fav_count(bot.aid) > 0)

    # ---------- 账号收藏的表情 ----------
    @staticmethod
    def fav_space(aid: str) -> str:
        return f"fav:{aid}"

    def fav_on(self, bot, room=None) -> bool:
        return bool(bot and bot.platform in FAV_PLATFORMS and self.e.S("sticker_fav_use", bot, room))

    def fav_count(self, aid: str) -> int:
        return self.count(self.fav_space(aid))

    # ---------- 学 ----------
    async def observe(self, bot, room, urls: list[str], uid: str):
        """看到群里有图：记一笔指纹；常用了就学。在后台跑，出错只记日志。"""
        e = self.e
        if not urls or not e.S("sticker_learn", bot, room) or not bot.channel:
            return
        if e.llm.hard_capped(bot.rid):
            return
        for url in urls[:2]:
            try:
                await self._observe_one(bot, room, url, uid)
            except Exception as ex:
                e.log("warn", "sticker", f"[{room.name}] 看表情包出错：{type(ex).__name__}: {ex}")

    async def _observe_one(self, bot, room, url: str, uid: str):
        e = self.e
        now = time.time()
        q = room.stk_fetch
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= 120:  # 每个群每小时最多下载这么多张来算指纹
            return
        q.append(now)
        data = await bot.channel.fetch_image(url)
        if not data or len(data) > MAX_BYTES:
            return
        h = dhash(data)
        if h is None:
            return
        space = self.space(room)
        row = None
        for r in self.db.rows("SELECT * FROM stickers WHERE space=?", (space,)):
            if hamming(int(r["phash"], 16), h) <= NEAR:
                row = r
                break
        if row is None:
            sid = self.db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, uses)
                               VALUES (?,?,?,?,?,?,?,'candidate',0)""", (space, room.key, f"{h:016x}", 1, json.dumps([uid]), now, now)).lastrowid
            row = self.db.rows("SELECT * FROM stickers WHERE id=?", (sid,))[0]
        else:
            users = json.loads(row["users"] or "[]")
            if uid not in users:
                users.append(uid)
            self.db.x("UPDATE stickers SET seen=seen+1, users=?, last_ts=? WHERE id=?", (json.dumps(users[-20:]), now, row["id"]))
            row = self.db.rows("SELECT * FROM stickers WHERE id=?", (row["id"],))[0]
        if row["status"] != "candidate":
            return
        users = json.loads(row["users"] or "[]")
        if row["seen"] < int(e.S("sticker_min_times", bot, room)) and len(users) < int(e.S("sticker_min_users", bot, room)):
            return
        await self.learn(bot, room, row, data)

    async def learn(self, bot, room, row: dict, data: bytes):
        e = self.e
        if row["id"] in self.busy:
            return
        day = time.strftime("%Y-%m-%d")
        cap = int(e.S("sticker_daily_learn", bot, room))
        done = self.db.x("SELECT COALESCE(SUM(n),0) FROM counters WHERE day=? AND name='sticker_learn' AND room=?", (day, room.key)).fetchone()[0]
        if cap and done >= cap:
            return
        if not e.S("vis_model", bot, room):
            return
        self.busy.add(row["id"])
        n = self.desc_max()
        try:
            out = await self.vis_ask(bot, data, VISION_PROMPT.replace("{n}", str(n)), room.platform, room.key)
        except ContentBlocked as ex:
            self.db.x("UPDATE stickers SET status='rejected' WHERE id=?", (row["id"],))
            e.log("info", "sticker", f"[{room.name}] 这张图不学（被服务商的内容审核拦下，重试和去掉搜索都没过）：{ex}")
            return
        finally:
            self.busy.discard(row["id"])
        d = parse_json(out)
        desc = str(d.get("desc") or "").strip()[:n]
        if not (d.get("sticker") and d.get("ok") is True and desc):
            self.db.x("UPDATE stickers SET status='rejected' WHERE id=?", (row["id"],))
            e.log("info", "sticker", f"[{room.name}] 这张图不学（{'不是表情包' if not d.get('sticker') else '不适合'}）")
            return
        path = self.folder() / f"{hashlib.sha1(data).hexdigest()[:20]}.{ext_of(data)}"
        path.write_bytes(data)
        mid = e.memory.add(row["space"], "sticker", row["phash"], "表情包", desc, "inferred", None, room.key, status="active")
        self.db.x("UPDATE stickers SET status='learned', file=?, size=?, mem_id=?, descr=?, desc_max=? WHERE id=?",
                  (path.name, len(data), mid, desc, n, row["id"]))
        self.db.x("""INSERT INTO counters (day, name, role_id, room, n) VALUES (?, 'sticker_learn', '', ?, 1)
                     ON CONFLICT (day, name, role_id, room) DO UPDATE SET n=n+1""", (day, room.key))
        e.log("info", "sticker", f"[{room.name}] 学会一张表情包：{desc}")
        await e.memory.flush_index()
        self.trim(room, bot)

    # ---------- 容量 ----------
    def trim(self, room, bot=None):
        e = self.e
        space = self.space(room)
        limit = int(e.S("sticker_max_count", bot, room) or 0)
        while limit and self.count(space) > limit:
            self.evict(space=space)
        cap = int(e.settings.g("sticker_cache_mb") or 0) * 1048576
        while cap and self.total_bytes() > cap:
            if not self.evict():
                break

    def total_bytes(self) -> int:
        return self.db.x("SELECT COALESCE(SUM(size),0) FROM stickers WHERE status='learned'").fetchone()[0]

    def evict(self, space: str | None = None) -> bool:
        sql = ("SELECT * FROM stickers WHERE status='learned' AND COALESCE(source,'')<>'fav'" + (" AND space=?" if space else "")  # 账号收藏的不删
               + " ORDER BY uses ASC, COALESCE(last_used, 0) ASC, id ASC LIMIT 1")
        rows = self.db.rows(sql, (space,) if space else ())
        if not rows:
            return False
        self.delete(rows[0]["id"])
        return True

    def delete(self, sid: int):
        r = self.db.rows("SELECT * FROM stickers WHERE id=?", (sid,))
        if not r:
            return
        r = r[0]
        if r.get("file"):
            (self.folder() / r["file"]).unlink(missing_ok=True)
        if r.get("mem_id"):
            self.db.x("DELETE FROM memories WHERE id=?", (r["mem_id"],))
            self.db.x("DELETE FROM memvec WHERE mem_id=?", (r["mem_id"],))
        # 删掉后保留一条「已拒绝」的指纹，免得很快又被学回来
        self.db.x("UPDATE stickers SET status='rejected', file=NULL, size=0, mem_id=NULL WHERE id=?", (sid,))

    # ---------- 选 ----------
    async def candidates(self, room, mood: str, bot=None, n: int = 10) -> list[dict]:
        """按心情在本群学来的表情包、加上这个账号收藏的表情里找至多 n 张最贴的候选（最近刚发过的往后排）。
        向量找不到就按字面找，再找不到用最常用的几张；一张表情都没有返回空列表。"""
        spaces = ([self.space(room)] if room else []) + ([self.fav_space(bot.aid)] if self.fav_on(bot, room) else [])
        rows = {r["mem_id"]: r for sp in spaces for r in self.db.rows("SELECT * FROM stickers WHERE space=? AND status='learned'", (sp,))}
        if not rows:
            return []
        hits = []
        try:
            hits = await self.e.embedder.search(mood, spaces, n, 0.0, set(), kinds=("sticker",))
        except Exception as ex:
            self.e.log("warn", "sticker", f"找表情包的向量检索出错，改用关键词：{ex}")
        cands = [rows[h["id"]] for h in hits if h["id"] in rows]
        if not cands:  # 没有向量或没搜到：按字面找，再不行就取最常用的
            words = [w for w in mood.replace("，", " ").replace(",", " ").split() if w]
            cands = [r for r in rows.values() if any(w in (r.get("descr") or "") for w in words)]
            if not cands:
                cands = sorted(rows.values(), key=lambda r: -r["uses"])
        now = time.time()
        cands.sort(key=lambda r: 1 if now - (r["last_used"] or 0) < 600 else 0)  # 稳定排序：最近发过的靠后
        return cands[:n]

    def load(self, row: dict) -> bytes | None:
        try:
            return (self.folder() / row["file"]).read_bytes()
        except (OSError, TypeError):
            return None

    def used(self, sid: int):
        self.db.x("UPDATE stickers SET uses=uses+1, last_used=? WHERE id=?", (time.time(), sid))

    # ---------- 重新描述 ----------
    def redesc_todo(self, force: bool = False) -> list[dict]:
        """已学会的表情包里要重新描述的：force 全部，否则只要 desc_max（没记的算 40）和现在的设置不同的。"""
        return self.db.rows("SELECT * FROM stickers WHERE status='learned'" + ("" if force else " AND COALESCE(desc_max, ?) <> ?"),
                            () if force else (OLD_DESC_MAX, self.desc_max()))

    def redesc_bot(self, row: dict):
        """给这张表情重新描述时用哪个账号（用量记在它的角色名下）：收藏的用它所属的账号，群里学来的用同平台的第一个账号。"""
        if row.get("source") == "fav":
            return self.e.bots.get(row.get("account"))
        plat = str(row["room"]).split(":")[0]
        return next((b for b in self.e.bots.values() if b.platform == plat), None)

    async def redescribe(self, force: bool = False) -> dict:
        """后台把已有的表情包按现在的字数上限重新描述一遍（一张一次看图调用，不挡回复）。force：不看 desc_max，全部重来（面板上的按钮）。
        状态（学会 / 拒绝）、内容检查结果都不变；图片没了、没配看图模型、用量到硬上限、单张出错：跳过这张，留到下一轮。返回 {total, done, skipped}。"""
        e = self.e
        out = {"total": 0, "done": 0, "skipped": 0}
        if "redesc" in self.busy:
            return out
        n = self.desc_max()
        todo = [(r, b) for r in self.redesc_todo(force) if (b := self.redesc_bot(r)) and e.S("vis_model", b)]
        if not todo:
            return out
        self.busy.add("redesc")
        out["total"] = len(todo)
        e.log("info", "sticker", f"表情包重新描述：开始，共 {len(todo)} 张（按 {n} 字）")
        try:
            for row, b in todo:
                data = self.load(row)
                if not data or e.llm.hard_capped(b.rid):
                    out["skipped"] += 1
                    continue
                fav = row.get("source") == "fav"
                try:
                    res = await self.vis_ask(b, data, (FAV_PROMPT if fav else GROUP_PROMPT).replace("{n}", str(n)), b.platform, None if fav else row["room"])
                    desc = self.clean_desc(res, n)
                except ContentBlocked as ex:  # 重试、去掉搜索都没过：保留原来的描述，记成按现在的字数处理过，以后不再重描它
                    self.db.x("UPDATE stickers SET desc_max=? WHERE id=?", (n, row["id"]))
                    e.log("warn", "sticker", f"表情包重新描述：这张被服务商的内容审核拦下，保留原来的描述，以后不再重描：{(row.get('descr') or '')[:40]}（{ex}）")
                    out["kept"] = out.get("kept", 0) + 1
                    continue
                except Exception as ex:
                    e.log("warn", "sticker", f"表情包重新描述出错（这张留到下一轮）：{type(ex).__name__}: {ex}")
                    desc = ""
                if not desc:
                    out["skipped"] += 1
                    continue
                self.db.x("UPDATE stickers SET descr=?, desc_max=? WHERE id=?", (desc, n, row["id"]))
                if row.get("mem_id"):
                    self.db.x("UPDATE memories SET content=? WHERE id=?", (desc, row["mem_id"]))
                    self.db.x("DELETE FROM memvec WHERE mem_id=?", (row["mem_id"],))
                    e.memory.pending.append(row["mem_id"])
                out["done"] += 1
                if out["done"] % 10 == 0:  # 一轮可能很长：每十张就把向量算上，免得重描过的在这期间挑不到
                    await e.memory.flush_index()
            await e.memory.flush_index()
        finally:
            self.busy.discard("redesc")
        e.log("info", "sticker", f"表情包重新描述：完成 {out['done']} 张，跳过 {out['skipped']} 张（留到下一轮）"
              + (f"，{out['kept']} 张被内容审核拦下、保留原来的描述" if out.get("kept") else ""))
        return out

    # ---------- 面板 ----------
    def listing(self) -> list[dict]:
        return self.db.rows("""SELECT id, space, room, seen, status, descr, size, uses, last_used, first_ts, COALESCE(source,'') AS source, account FROM stickers
                               WHERE status='learned' ORDER BY COALESCE(source,'') DESC, room, uses DESC, id DESC""")

    # ---------- 账号收藏的表情：同步 ----------
    def purge(self, sid: int):
        """收藏里删掉了：这里整条删（文件、描述、向量、记录），不留「已拒绝」的指纹——以后再收藏回来要能重新收。"""
        r = self.db.rows("SELECT * FROM stickers WHERE id=?", (sid,))
        if not r:
            return
        r = r[0]
        if r.get("file"):
            (self.folder() / r["file"]).unlink(missing_ok=True)
        if r.get("mem_id"):
            self.db.x("DELETE FROM memories WHERE id=?", (r["mem_id"],))
            self.db.x("DELETE FROM memvec WHERE mem_id=?", (r["mem_id"],))
        self.db.x("DELETE FROM stickers WHERE id=?", (sid,))

    def save_kv(self, key: str, value):
        """同步的收尾记录。停机时同步任务被取消，库可能已经关了：这时不写，也不报错（下次启动照常同步）。"""
        try:
            self.db.set_kv(key, value)
        except sqlite3.ProgrammingError:
            pass

    async def sync_favorites(self, bot, force: bool = False, min_age: float = 0) -> dict:
        """把这个 QQ 账号收藏的表情同步下来：新出现的图下载、用看图模型各写一句描述（按画面指纹认重复，每张只描述一次）；
        从收藏里删掉的，这里跟着删。收藏的不按「是不是表情包、合不合适」丢弃（「收藏的表情也做内容检查」打开时才检查）。
        返回 {total, added, removed, kept, rejected, pending, error}。force：不看「用账号收藏的表情」开关（面板上点了「现在同步收藏」）。
        min_age：距上次同步不到这么多秒就不做（上线时同步用，网络一抖动就重连的号不会一直去问 NapCat）。"""
        e = self.e
        aid = bot.aid
        out = {"total": 0, "added": 0, "removed": 0, "kept": 0, "rejected": 0, "blocked": 0, "pending": 0, "error": ""}
        ch = bot.channel
        if bot.platform not in FAV_PLATFORMS or ch is None or ch.status != "online":
            out["error"] = "这个账号没在线"
            return out
        if not force and not e.S("sticker_fav_use", bot):
            out["error"] = "没开「用账号收藏的表情」"
            return out
        if aid in self.busy:
            out["error"] = "正在同步"
            return out
        if min_age and time.time() - float((self.db.kv(f"favsync:{aid}") or {}).get("ts") or 0) < min_age:
            out["error"] = "刚同步过"
            return out
        self.busy.add(aid)
        try:
            return await (self._sync_wechat(bot, out) if bot.platform == "wechat" else self._sync_favorites(bot, out))
        except Exception as ex:
            out["error"] = f"{type(ex).__name__}: {ex}"
            e.log("warn", "sticker", f"[{aid}] 同步收藏的表情出错：{out['error']}")
            return out
        finally:
            self.busy.discard(aid)
            self.save_kv(f"favsync:{aid}", {"ts": time.time(), **out})

    async def _sync_favorites(self, bot, out: dict) -> dict:
        e, ch, aid = self.e, bot.channel, bot.aid
        space = self.fav_space(aid)
        room_key = f"fav:{aid}"
        check = bool(e.S("sticker_fav_check", bot))
        urls = await ch.call("fetch_custom_face", count=FAV_COUNT)
        urls = list(dict.fromkeys(str(u) for u in (urls or []) if u))
        out["total"] = len(urls)
        # 链接 → 画面指纹：没变的链接不再下载
        fm: dict = dict(self.db.kv(f"favmap:{aid}") or {})
        blobs: dict[str, bytes] = {}
        cur: dict[str, int] = {}
        for url in urls:
            key = url.split("?")[0]
            if fm.get(key):
                cur[url] = int(fm[key], 16)
                continue
            data = await ch.fetch_image(url)
            if not data or len(data) > MAX_BYTES:
                continue
            h = dhash(data)
            if h is None:
                continue
            fm[key] = f"{h:016x}"
            cur[url] = h
            blobs[url] = data
        keep_map = {}
        for u in cur:
            keep_map[u.split("?")[0]] = fm[u.split("?")[0]]
        self.db.set_kv(f"favmap:{aid}", keep_map)
        have = list(cur.values())
        if not check:  # 不做内容检查：以前检查时拒绝过的，现在放行（删掉那些记录，下面当新的处理）
            for r in self.db.rows("SELECT id FROM stickers WHERE space=? AND status='rejected'", (space,)):
                self.db.x("DELETE FROM stickers WHERE id=?", (r["id"],))
        for r in self.db.rows("SELECT * FROM stickers WHERE space=?", (space,)):  # 从收藏里删掉的：这里跟着删
            if not any(hamming(int(r["phash"], 16), h) <= NEAR for h in have):
                if r["status"] == "learned":
                    out["removed"] += 1
                self.purge(r["id"])
        known = [int(r["phash"], 16) for r in self.db.rows("SELECT phash FROM stickers WHERE space=?", (space,))]
        out["kept"] = self.fav_count(aid)
        todo = []
        for url, h in cur.items():
            if any(hamming(k, h) <= NEAR for k in known):
                continue
            known.append(h)  # 收藏里两张几乎一样的，只描述一张
            todo.append((url, h))
        if todo and not e.S("vis_model", bot):
            out["pending"] = len(todo)
            e.log("info", "sticker", f"[{aid}] 收藏里有 {len(todo)} 张新表情，但没配看图模型，没法写描述，先放着")
            return out
        for url, h in todo[:FAV_BATCH]:
            if e.llm.hard_capped(bot.rid):
                out["pending"] += 1
                continue
            data = blobs.get(url) or await ch.fetch_image(url)
            if not data:
                out["pending"] += 1
                continue
            n = self.desc_max()
            res = await self._describe_fav(bot, data, check, n)
            if res is None:  # 看图出错：留到下一轮
                out["pending"] += 1
                continue
            desc, ok = res
            now = time.time()
            if not ok:
                st = "blocked" if desc == BLOCKED else "rejected"
                self.db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, uses, source, account)
                             VALUES (?,?,?,?,?,?,?,?,0,'fav',?)""", (space, room_key, f"{h:016x}", 1, "[]", now, now, st, aid))
                out[st] += 1
                continue
            path = self.folder() / f"{hashlib.sha1(data).hexdigest()[:20]}.{ext_of(data)}"
            path.write_bytes(data)
            mid = e.memory.add(space, "sticker", f"{h:016x}", "收藏的表情", desc, "inferred", None, room_key, status="active")
            self.db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, file, size, uses, mem_id, descr, source, account, desc_max)
                         VALUES (?,?,?,?,?,?,?,'learned',?,?,0,?,?,'fav',?,?)""",
                      (space, room_key, f"{h:016x}", 1, "[]", now, now, path.name, len(data), mid, desc, aid, n))
            out["added"] += 1
        out["pending"] += max(0, len(todo) - FAV_BATCH)
        out["kept"] = self.fav_count(aid)
        await e.memory.flush_index()
        self.trim_total()
        e.log("info", "sticker", f"[{aid}] 同步收藏的表情：收藏里共 {out['total']} 张，新收 {out['added']} 张，删掉 {out['removed']} 张，现有 {out['kept']} 张"
              + (f"，{out['rejected']} 张没通过内容检查" if out["rejected"] else "") + (f"，{out['blocked']} 张被服务商的内容审核拦下（不收）" if out["blocked"] else "")
              + (f"，还有 {out['pending']} 张下一轮再描述" if out["pending"] else ""))
        return out

    # ---------- 微信的「自定义表情」：同步、发 ----------
    async def _sync_wechat(self, bot, out: dict) -> dict:
        """目录（kv wxfav:账号）= [{sid: 表情包记录的 id, pos: 第几格, ticks: 翻到它要滚几格滚轮, fps: 画面指纹（十六进制，一组）}]。
        扫一遍「自定义表情」，认得出的更新位置和指纹，新的用放大预览的截图写描述（每张只描述一次），目录里有而这次没扫到的跟着删。
        扫的过程中出错就什么都不改（也不会误删）。"""
        from .wxsticker import merge_fps
        e, ch, aid = self.e, bot.channel, bot.aid
        space, room_key = self.fav_space(aid), f"fav:{aid}"
        check = bool(e.S("sticker_fav_check", bot))
        if not check:  # 不做内容检查：以前检查时拒绝过的（或面板里删掉的），现在放行，下面当新的处理
            for r in self.db.rows("SELECT id FROM stickers WHERE space=? AND status='rejected'", (space,)):
                self.db.x("DELETE FROM stickers WHERE id=?", (r["id"],))
        rows = {r["id"]: r for r in self.db.rows("SELECT * FROM stickers WHERE space=?", (space,))}
        entries = [x for x in (self.db.kv(f"wxfav:{aid}") or {}).get("entries", []) if x.get("sid") in rows]
        has_vis = bool(e.S("vis_model", bot))
        res = await ch.fav_scan([(x["sid"], [int(f, 16) for f in x["fps"]]) for x in entries], FAV_BATCH if has_vis else 0)
        out["total"] = res["cells"]
        by_key = {g["key"]: g for g in res["groups"] if g["key"] is not None}
        kept: list[dict] = []
        try:
            for x in entries:
                g = by_key.get(x["sid"])
                if g is None:  # 收藏里删掉了：这里跟着删
                    if rows[x["sid"]]["status"] == "learned":
                        out["removed"] += 1
                    self.purge(x["sid"])
                    continue
                old = [int(f, 16) for f in x["fps"]]
                kept.append({"sid": x["sid"], "pos": g["pos"], "ticks": g["ticks"], "fps": [f"{f:016x}" for f in merge_fps(old, g["fps"])]})
            new = [g for g in res["groups"] if g["key"] is None]
            if new and not has_vis:
                e.log("info", "sticker", f"[{aid}] 自定义表情里有 {len(new)} 张新表情，但没配看图模型，没法写描述，先放着")
            for g in new:
                if g["pending"] or e.llm.hard_capped(bot.rid):
                    out["pending"] += 1
                    continue
                buf = io.BytesIO()
                g["preview"].save(buf, "PNG")
                data = buf.getvalue()
                n = self.desc_max()
                r = await self._describe_fav(bot, data, check, n)
                if r is None:  # 看图出错：留到下一轮
                    out["pending"] += 1
                    continue
                desc, ok = r
                now, ph = time.time(), f"{g['fps'][0]:016x}"
                if not ok:
                    st = "blocked" if desc == BLOCKED else "rejected"
                    sid = self.db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, uses, source, account)
                                       VALUES (?,?,?,?,?,?,?,?,0,'fav',?)""", (space, room_key, ph, 1, "[]", now, now, st, aid)).lastrowid
                    out[st] += 1
                else:
                    path = self.folder() / f"{hashlib.sha1(data).hexdigest()[:20]}.png"
                    path.write_bytes(data)
                    mid = e.memory.add(space, "sticker", ph, "收藏的表情", desc, "inferred", None, room_key, status="active")
                    sid = self.db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, file, size, uses, mem_id, descr, source, account, desc_max)
                                       VALUES (?,?,?,?,?,?,?,'learned',?,?,0,?,?,'fav',?,?)""",
                                    (space, room_key, ph, 1, "[]", now, now, path.name, len(data), mid, desc, aid, n)).lastrowid
                    out["added"] += 1
                kept.append({"sid": sid, "pos": g["pos"], "ticks": g["ticks"], "fps": [f"{f:016x}" for f in g["fps"]]})
        finally:
            self.save_kv(f"wxfav:{aid}", {"entries": kept, "ts": time.time()})
        out["kept"] = self.fav_count(aid)
        await e.memory.flush_index()
        self.trim_total()
        e.log("info", "sticker", f"[{aid}] 同步微信的自定义表情：共 {res['cells']} 格，新收 {out['added']} 张，删掉 {out['removed']} 张，现有 {out['kept']} 张"
              + (f"，{out['rejected']} 张没通过内容检查" if out["rejected"] else "") + (f"，{out['blocked']} 张被服务商的内容审核拦下（不收）" if out["blocked"] else "")
              + (f"，还有 {out['pending']} 张下一轮再描述" if out["pending"] else ""))
        return out

    def wx_entry(self, bot, row: dict) -> dict | None:
        """这张表情在微信「自定义表情」里的位置和指纹（发表情用）；目录里没有返回 None。"""
        for x in (self.db.kv(f"wxfav:{bot.aid}") or {}).get("entries", []):
            if x.get("sid") == row["id"]:
                return {"sid": x["sid"], "pos": x["pos"], "ticks": x["ticks"], "fps": [int(f, 16) for f in x["fps"]]}
        return None

    def wx_save(self, bot, entry: dict):
        """发完把找到后更新了的位置、补上的指纹写回目录。"""
        cat = self.db.kv(f"wxfav:{bot.aid}") or {}
        for x in cat.get("entries", []):
            if x.get("sid") == entry["sid"]:
                x.update(pos=entry["pos"], ticks=entry["ticks"], fps=[f"{f:016x}" for f in entry["fps"]])
                self.db.set_kv(f"wxfav:{bot.aid}", cat)
                return

    @staticmethod
    def clean_desc(out, n: int) -> str:
        """看图模型直接回的描述：去掉引号，模型没听话回了 JSON 的话取里面的 desc，截到 n 字。"""
        out = str(out or "").strip()
        if out.startswith("{"):
            out = str(parse_json(out).get("desc") or "")
        return out.strip().strip("\"'“”「」")[:n]

    async def _describe_fav(self, bot, data: bytes, check: bool, n: int) -> tuple[str, bool] | None:
        """给一张收藏的表情写描述（n 字内）。返回（描述, 通过吗）；看图出错返回 None。不做内容检查时一律通过（描述写不出来就不收这张，下一轮再试）。"""
        e = self.e
        try:
            out = await self.vis_ask(bot, data, (VISION_PROMPT if check else FAV_PROMPT).replace("{n}", str(n)), bot.platform, None)
        except ContentBlocked as ex:
            e.log("warn", "sticker", f"[{bot.aid}] 这张收藏的表情被服务商的内容审核拦下（重试和去掉搜索都没过），不收，以后不再试：{ex}")
            return BLOCKED, False
        except Exception as ex:
            e.log("warn", "sticker", f"[{bot.aid}] 给收藏的表情写描述出错：{type(ex).__name__}: {ex}")
            return None
        if check:
            d = parse_json(out)
            desc = str(d.get("desc") or "").strip()[:n]
            return (desc, bool(d.get("sticker") and d.get("ok") is True and desc)) if desc else ("", False)
        desc = self.clean_desc(out, n)
        return (desc, True) if desc else None

    def trim_total(self):
        cap = int(self.e.settings.g("sticker_cache_mb") or 0) * 1048576
        while cap and self.total_bytes() > cap:
            if not self.evict():  # 只剩收藏的了：不删
                break

    def fav_status(self) -> list[dict]:
        """面板用：每个 QQ / 微信账号现有几张收藏的表情、上次同步的结果。"""
        out = []
        for a in self.db.docs("account"):
            if a.get("platform") not in FAV_PLATFORMS:
                continue
            st = self.db.kv(f"favsync:{a['id']}") or {}
            out.append({"id": a["id"], "name": a.get("name") or a.get("qq") or a["id"], "count": self.fav_count(a["id"]), "last": st})
        return out
