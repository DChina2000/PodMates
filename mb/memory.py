"""长期记忆：人物备忘与印象、群前情（分层合并，写成后不改）、群梗词典。

记忆空间决定一条记忆存在哪、谁能用：默认每个群、每个私聊得来的记忆只在那里用（人物备忘还按角色分开记）；
想共享，自建空间把几个群 / 私聊（或角色、平台）绑在一起，用户建的空间优先于默认。
记忆只是「谁说过什么」，结构上不写人物表，所以改不了身份、态度、管理员。

群里发生的事：第一层每 memory_segment 条消息整理一段（不设字数），每 memory_merge 段并成上一层（上限 = 所并各段实际字数之和 × 倍数）；
每个单位记着层级、覆盖的消息范围和时间范围、说话和被提到的人。拼提示词时用最粗的一层盖住全部较早的历史，空间够就从新往旧换成下一层。
人：每条备忘永久留着（带日期和来源）；每人在每个空间里一段「印象」，备忘被挤出最新 N 条时并进印象；按人取回旧记录。
"""
import json
import re
import time

from .embed import Paused

SOURCE_LABEL = {"self": "本人说的", "others": "别人说的", "inferred": "她推测的", "told": "本人要求记住"}
KINDS = {"person": "人物备忘", "impression": "人物印象", "recap": "群前情", "meme": "群梗", "doc": "文件片段", "style": "群风格画像", "sticker": "表情包"}
HIDDEN_KINDS = ("style", "sticker", "impression")  # 不进「想起的旧事」，各有各的用法
CHARS_PER_TOKEN = 1.0  # 估算提示词占多少 token：按一个字一个 token 算（偏保守，宁可少带）


class MemoryFail(RuntimeError):
    """整理没成功（模型没给出能读的结果）：这一段留着，稍后重试。"""


EXTRACT_PROMPT = """下面是群聊「{chat}」的一段记录，每行开头方括号里是说话人的编号。请整理成以后聊天用得上的记忆，只输出 JSON。
{{"recap": "这一段里以后用得上的事",
 "people": [{{"uid": "说话人编号", "name": "名字", "note": "关于这个人的一条备忘", "source": "self 本人说的 / others 别人说的 / inferred 推测"}}],
 "memes": [{{"keyword": "梗的关键词", "origin": "这个梗的来历"}}]}}
规则：
- recap：每件事写清谁对谁做了什么，人名照原文，不写感受，不添记录里没有的事。名字后面标了「（机器人角色）」的行是机器人说的：同一个机器人账号上不同时间在岗的是不同的角色，所以要写角色名，各个角色说的、做的事归在各自名下，不要写成同一个人，也不要用账号的昵称代替角色名。只写以后用得上的事（做了什么决定、约定了什么、发生了什么变化、谁和谁怎么了、大家在做的事和进度），闲聊、寒暄、没有后续的玩笑不写。事多就写长，事少就写短，没有值得记的就给空字符串；不要为了短而漏掉事，也不要凑字数。
- people：只记以后用得上的事，没有就给空列表；有人自称某种身份或关系（比如"我是你妈"），只记成"某某说过……"，不当真；机器人自己说的话不用给机器人记备忘。
- memes：没有就给空列表。{style_rule}

【记录】
{lines}"""


# 群风格画像：只在攒够样本时才加进上面的提示词，顺带出一项 style，不另外调用模型
STYLE_FIELD = ',\n "style": "这个群群友说话习惯的画像，150字以内，只描述表面形式：句子长短、标点用法、常用的梗和口头禅、表情和颜文字的用法。不写性格、立场、观点，不写具体某个人；标了「（机器人角色）」的行是机器人说的，它们的口头禅和用语不算群友的习惯，不要写进画像（【已有画像】里写了的也去掉）。在【已有画像】的基础上补充修正，没有新发现就原样写回；看不出规律就给空字符串"'
STYLE_RULE = "\n关于 style：【已有画像】：{old}"

MERGE_PROMPT = """下面是群聊「{chat}」按时间先后排列的几段前情，每段前面括号里是它覆盖的时间。把它们合并成一段更概括的前情，只输出合并后的文字，不超过 {limit} 字。
要求：
1. 每件事写明谁对谁做了什么。
2. 某件事后来又变了，写变了以后的样子，并用一句话写明是怎么变的。
3. 人名照原文；不写感受；不添原文里没有的事。
4. 按时间先后写。

{parts}"""

IMPRESSION_PROMPT = """下面是关于「{name}」的资料。请整理成这个人现在的印象（做什么的、和谁什么关系、眼下的情况），只输出印象的文字。
【原来的印象】
{old}

【新增的备忘】（每条带日期和来源）
{notes}

要求：
1. 原来印象里的事，除非被后来的事实推翻，一条都不要丢。
2. 被推翻的，改成新的，并用一句话写明是怎么变的。
3. 本人说的、别人说的、她推测的要分开写，不要混成一句。
4. 写的是这个人现在的情况；他说过什么、做过什么这类具体的历史不用复述（原始备忘另外留着）。
5. 人名照原文，不添资料里没有的事。不限字数，事实多就写长，事实少就写短。"""


def subject_key(platform: str, uid: str, person: dict | None) -> str:
    return f"person:{person['id']}" if person else f"uid:{platform}:{uid}"


def parse_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?|```", "", text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def day_text(ts: float) -> str:
    lt, now = time.localtime(ts), time.localtime()
    return (f"{lt.tm_year} 年 " if lt.tm_year != now.tm_year else "") + f"{lt.tm_mon} 月 {lt.tm_mday} 日"


def span_text(t0: float, t1: float) -> str:
    a, b = day_text(t0), day_text(t1)
    return a if a == b else f"{a}—{b}"


def meta_of(r: dict) -> dict:
    try:
        return json.loads(r.get("meta") or "{}")
    except (ValueError, TypeError):
        return {}


class Unit:
    """一个前情单位（任何一层）。"""
    __slots__ = ("id", "level", "text", "t0", "t1", "m0", "m1", "uids", "parts", "merged", "space", "ts", "children", "row")

    def __init__(self, r: dict):
        m = meta_of(r)
        self.row, self.id, self.level, self.text, self.space, self.ts = r, r["id"], r["level"] or 1, (r["content"] or "").strip(), r["space"], r["ts"]
        self.t1 = m.get("t1") or r["ts"]
        self.t0 = m.get("t0") or self.t1
        self.m0, self.m1 = m.get("m0"), m.get("m1")
        self.uids = set(m.get("uids") or [])
        self.parts = list(m.get("parts") or [])
        self.merged = bool(r["merged"])
        self.children: list["Unit"] = []

    @property
    def line(self) -> str:
        return f"- {span_text(self.t0, self.t1)}：{self.text}" if self.text else ""

    @property
    def cost(self) -> int:
        return len(self.line) + 1 if self.text else 0


class Memory:
    def __init__(self, db, settings, people, llm, log, embedder=None, spawn=None):
        self.db, self.settings, self.people, self.llm, self.log = db, settings, people, llm, log
        self.embedder = embedder
        self.spawn = spawn  # 把协程挂到主循环上（备忘过期后重写印象用）
        self.pending: list[int] = []  # 新写入、还没算向量的记忆

    # ---------- 空间 ----------
    def space(self, kind: str, role: str | None, platform: str | None, chat: str | None, dm: str | None = None) -> str:
        """chat: '<平台>:<群号>'；私聊时 chat 为 None，dm 是 'dm:<账号>:<对方>'（哪个账号 × 哪个人的私聊各自一份）。
        先看用户自建的空间（绑进去的群 / 私聊共用一份），没有就是默认：这个群 / 这个私聊自己的一份。"""
        scope = chat or dm
        for sp in self.db.docs("space"):
            if kind not in sp.get("kinds", []):
                continue
            for b in sp.get("bindings", []):
                if all(b.get(k) in (None, "", "*", v) for k, v in (("role", role), ("platform", platform), ("chat", scope))):
                    return f"sp:{sp['id']}"
        if not scope:
            return "orphan"  # 既不是群也不是私聊：不属于任何地方，谁也读不到
        return f"iso:{scope}:person:{role}" if kind in ("person", "impression") else f"chat:{scope}"

    def enabled(self, platform, chat) -> bool:
        return bool(self.settings.get("memory_enabled", platform=platform, chat=chat))

    # ---------- 写入 ----------
    def add(self, space, kind, subject, subject_name, content, source="", src=None, room=None, level=1,
            role=None, status=None, meta=None, ts=None) -> int:
        if status is None:
            platform = room.split(":")[0] if room else None
            status = "active" if self.settings.get("memory_auto_apply", role, platform, room) else "pending"
        cur = self.db.x("""INSERT INTO memories (space, kind, subject, subject_name, content, source, src, level, status, ts, room, meta)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (space, kind, subject, subject_name, content, source,
                         json.dumps(src or [], ensure_ascii=False), level, status, ts or time.time(), room,
                         json.dumps(meta, ensure_ascii=False) if meta is not None else None))
        if kind != "impression":
            self.pending.append(cur.lastrowid)
        return cur.lastrowid

    async def flush_index(self):
        """把新写入的记忆算好向量（语义回忆用）。算不了就记一条警告，不影响记忆本身。"""
        ids, self.pending = self.pending, []
        if not ids or not self.embedder:
            return
        try:
            await self.embedder.index(ids)
        except Paused:
            self.pending = ids + self.pending  # 等用量恢复后再算
        except Exception as e:
            self.log("warn", "memory", f"记忆向量没算成（语义回忆暂时找不到这几条）：{e}")

    # ---------- 整理 ----------
    def segment_uids(self, platform: str, msgs: list[dict], recap: str) -> list[str]:
        """这一段里说话的人和被提到的人的编号：说话的取消息的发送者；被提到的 = 名字出现在别人的话或前情里的发送者，加上人物表里名字 / 称呼出现在这一段里的人。"""
        speakers = {str(m["user_id"]): re.sub(r"〔.*?〕", "", m.get("name") or "").strip() for m in msgs if m.get("user_id")}
        alltext = "\n".join(m["text"] for m in msgs) + "\n" + recap
        out = set(speakers)
        for uid, name in speakers.items():
            if len(name) >= 2 and (name in recap or any(name in m["text"] for m in msgs if str(m.get("user_id")) != uid)):
                out.add(uid)
        for p in self.people.persons.values():
            names = [p.get("name") or ""] + [c for c in self.people.calls_of(p) if len(c) > 1]
            if any(len(n) >= 2 and n in alltext for n in names):
                out.update(self.people.uids(p, platform))
        return sorted(out)

    def speaker_name(self, m: dict) -> str:
        """整理时给模型看的说话人名字。机器人的话标成「角色名（机器人角色）」：一个号上轮流在岗的角色各是各的，不能让账号的昵称
        （群里看到的「某某🐬」）把她们记成同一个人——否则之后哪个角色在岗，都会把别的角色说过的话当成自己的。"""
        rid = m.get("role_id")
        if not rid:
            return m["name"]
        name = (self.db.doc("role", rid) or {}).get("name") or (re.search(r"〔(.+?)本人〕", m["name"] or "") or [None, ""])[1] or "别的角色"
        return f"{name}（机器人角色）"

    async def extract(self, room: str, chat_name: str, msgs: list[dict], roles: list[str]):
        """msgs: [{id, ts, user_id, name, text, role_id}]。一次模型调用同时出前情、人物备忘、群梗。模型没给出能读的结果就抛 MemoryFail（这一段留着重试）。"""
        platform = room.split(":")[0]
        lines = "\n".join(f"[{m['user_id']}] {self.speaker_name(m)}: {m['text']}" for m in msgs)
        # 群风格画像：顺带在这一次调用里更新，不额外调用模型；群友消息攒够了才开始
        ask_style = self.style_wanted(room, platform, msgs)
        prompt = EXTRACT_PROMPT.format(chat=chat_name, lines=lines, style_rule=STYLE_RULE.format(old=self.style_text(room) or "（还没有）") if ask_style else "")
        if ask_style:
            prompt = prompt.replace('"memes": [{"keyword": "梗的关键词", "origin": "这个梗的来历"}]}', '"memes": [{"keyword": "梗的关键词", "origin": "这个梗的来历"}]' + STYLE_FIELD + "}", 1)
        out = await self.llm.chat("mem", [{"role": "user", "content": prompt}],
                                  role=roles[0] if roles else None, platform=platform, chat=room)
        data = parse_json(out)
        if not data:
            raise MemoryFail("整理结果读不出来")
        if ask_style and isinstance(data.get("style"), str) and data["style"].strip():
            self.set_style(room, data["style"].strip()[:400])
        src = [{"name": m["name"], "text": m["text"][:200]} for m in msgs[-40:]]
        bot_uids = {m["user_id"] for m in msgs if m.get("role_id")}
        n = 0
        recap = (data.get("recap") or "").strip() if isinstance(data.get("recap"), str) else ""
        sp = self.space("recap", None, platform, room)
        ids = [m["id"] for m in msgs if m.get("id")]
        tss = [m["ts"] for m in msgs if m.get("ts")]
        meta = {"m0": min(ids) if ids else None, "m1": max(ids) if ids else None, "t0": min(tss) if tss else None, "t1": max(tss) if tss else None,
                "uids": self.segment_uids(platform, msgs, recap)}
        if not recap:
            meta["empty"] = 1  # 这一段没有值得记的事：不写内容，但范围记着（每条消息不是在前情里就是在原始消息里）
        self.add(sp, "recap", "", "", recap, "inferred", src, room, meta=meta)
        n += 1 if recap else 0
        touched: dict[str, str] = {}
        for p in data.get("people") or []:
            uid, note = str(p.get("uid", "")).strip(), (p.get("note") or "").strip()
            if not note or uid in bot_uids:
                continue
            person = self.people.find(platform, uid) if uid else None
            subject = subject_key(platform, uid, person) if uid else f"name:{p.get('name', '')}"
            name = person["name"] if person else (p.get("name") or uid)
            psrc = [s for s in src if s["name"].startswith(str(p.get("name", ""))[:4])][-6:] or src[-6:]
            for r in roles or [None]:
                self.add(self.space("person", r, platform, room), "person", subject, name, note,
                         p.get("source") if p.get("source") in SOURCE_LABEL else "inferred", psrc, room, role=r)
                n += 1
            touched[subject] = name
        for mm in data.get("memes") or []:
            kw, origin = (mm.get("keyword") or "").strip(), (mm.get("origin") or "").strip()
            if kw and origin:
                msp = self.space("meme", None, platform, room)
                self.db.x("DELETE FROM memories WHERE space=? AND kind='meme' AND subject=?", (msp, kw))
                self.add(msp, "meme", kw, kw, origin, "inferred", src, room)
                n += 1
        self.log("info", "memory", f"{chat_name}：整理了 {len(msgs)} 条消息，得到 {n} 条记忆")
        # 往上并、更新印象：失败只记日志，留到下次整理再做（不让这一段的整理白费）
        try:
            await self.merge_up(sp, room, chat_name)
        except Exception as e:
            self.log("warn", "memory", f"{chat_name}：合并前情没成功，下次再试：{e}")
        try:
            await self.refresh_impressions(platform, room, touched, roles)
        except Exception as e:
            self.log("warn", "memory", f"{chat_name}：更新印象没成功，下次再试：{e}")
        await self.flush_index()
        return n

    def store_raw_segment(self, room: str, msgs: list[dict]):
        """整理一直没成功（连试了几次）：把这一段的原话存成第一层单位，免得原始消息过期删掉以后这段就没了。"""
        platform = room.split(":")[0]
        ids = [m["id"] for m in msgs if m.get("id")]
        tss = [m["ts"] for m in msgs if m.get("ts")]
        text = "（这一段没整理成功，以下是原话）\n" + "\n".join(f"{m['name']}：{m['text']}" for m in msgs)
        self.add(self.space("recap", None, platform, room), "recap", "", "", text, "inferred", None, room,
                 meta={"m0": min(ids) if ids else None, "m1": max(ids) if ids else None, "t0": min(tss) if tss else None, "t1": max(tss) if tss else None,
                       "uids": sorted({str(m["user_id"]) for m in msgs if m.get("user_id")}), "raw": 1})

    # ---------- 群风格画像 ----------
    def style_space(self, room: str) -> str:
        return self.space("style", None, room.split(":")[0], room)

    def style_row(self, room: str) -> dict | None:
        space = self.style_space(room)
        if space.startswith("sp:"):  # 几个群绑到同一个自建空间：共用同一份画像，不再按群分开
            rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='style' ORDER BY id DESC LIMIT 1", (space,))
        else:
            rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='style' AND subject=?", (space, room))
        return rows[0] if rows else None

    def style_text(self, room: str) -> str:
        r = self.style_row(room)
        return (r["content"] if r and r["status"] == "active" else "") or ""

    def set_style(self, room: str, text: str):
        text = text.strip()
        space = self.style_space(room)
        shared = space.startswith("sp:")  # 几个群绑到同一个空间：只留一份画像
        rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='style'" + ("" if shared else " AND subject=?") + " ORDER BY id DESC",
                            (space,) if shared else (space, room))
        r = rows[0] if rows else None
        if not text:
            if shared:
                self.db.x("DELETE FROM memories WHERE space=? AND kind='style'", (space,))
            elif r:
                self.db.x("DELETE FROM memories WHERE id=?", (r["id"],))
            return
        if r:
            self.db.x("UPDATE memories SET content=?, ts=?, status='active' WHERE id=?", (text, time.time(), r["id"]))
            if shared:
                self.db.x("DELETE FROM memories WHERE space=? AND kind='style' AND id!=?", (space, r["id"]))  # 以前每群一份的旧画像：并成一份
        else:
            self.add(space, "style", room, "群风格画像", text, "inferred", None, room, status="active")

    def style_wanted(self, room: str, platform: str, msgs: list[dict]) -> bool:
        """要不要在这次整理里顺带更新画像：开着，并且群友的消息累计够多（样本不够不生成，也不编造）。"""
        if not self.settings.get("style_learn", platform=platform, chat=room):
            return False
        humans = [m for m in msgs if not m.get("role_id")]
        key = f"style_n:{room}"
        n = (self.db.kv(key, 0) or 0) + len(humans)
        self.db.set_kv(key, n)
        return n >= int(self.settings.g("style_min_msgs")) and len({m["user_id"] for m in humans}) >= 1

    # ---------- 分层合并 ----------
    def unit_rows(self, space: str, active_only: bool = True) -> list[dict]:
        return self.db.rows(f"SELECT * FROM memories WHERE space=? AND kind='recap'{' AND status=' + chr(39) + 'active' + chr(39) if active_only else ''} ORDER BY id", (space,))

    def ratio(self, room: str) -> float:
        try:
            r = float(self.settings.get("memory_ratio", platform=room.split(":")[0], chat=room))
        except (TypeError, ValueError):
            r = 0.7
        return r if r > 0 else 0.7

    async def merge_up(self, space: str, room: str, chat_name: str):
        """每 memory_merge 个同层、还没被并的单位并成上一层（按编号顺序成组，凑不满的留着）。写成的单位以后不再重写。"""
        n = int(self.settings.get("memory_merge", platform=room.split(":")[0], chat=room) or 0)
        if n < 2:
            return
        level = 1
        while level < 20:
            all_rows = self.unit_rows(space, active_only=False)  # 先等审核的也照常往上并（审核通过的是整条前情，和以前一样）
            rows = [r for r in all_rows if (r["level"] or 1) == level and not r["merged"]]
            if len(rows) < n and not any((r["level"] or 1) > level for r in all_rows):
                return
            while len(rows) >= n:
                group, rows = rows[:n], rows[n:]
                await self.merge_group(space, room, chat_name, group)
            level += 1

    async def merge_group(self, space: str, room: str, chat_name: str, group: list[dict]):
        units = [Unit(r) for r in group]
        full = [u for u in units if u.text]
        total = sum(len(u.text) for u in full)
        ids = [u.id for u in units]
        text = ""
        if full:
            limit = max(30, int(total * self.ratio(room)))  # 上限 = 所并各段实际字数之和 × 倍数
            parts = "\n\n".join(f"{i + 1}.（{span_text(u.t0, u.t1)}）{u.text}" for i, u in enumerate(full))
            text = (await self.llm.chat("mem", [{"role": "user", "content": MERGE_PROMPT.format(chat=chat_name, parts=parts, limit=limit)}],
                                        platform=room.split(":")[0], chat=room) or "").strip()
            if not text:
                return  # 模型没给出东西：留着，下次再并
        m0s, m1s = [u.m0 for u in units if u.m0], [u.m1 for u in units if u.m1]
        meta = {"m0": min(m0s) if m0s else None, "m1": max(m1s) if m1s else None, "t0": min(u.t0 for u in units), "t1": max(u.t1 for u in units),
                "uids": sorted(set().union(*[u.uids for u in units])), "parts": ids}
        if not text:
            meta["empty"] = 1
        self.db.x(f"UPDATE memories SET merged=1 WHERE id IN ({','.join(map(str, ids))})")
        self.add(space, "recap", "", "", text, "inferred", [{"name": "下层摘要", "text": u.text[:200]} for u in full], room, units[0].level + 1, meta=meta)

    # ---------- 印象 ----------
    def impression_row(self, space: str, subject: str) -> dict | None:
        rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='impression' AND subject=? ORDER BY id DESC LIMIT 1", (space, subject))
        return rows[0] if rows else None

    def note_rows(self, space: str, subject: str) -> list[dict]:
        """这个人在这个空间里的备忘（不含本人要求记住的，那些原样带、不并进印象），按编号。"""
        return self.db.rows("""SELECT * FROM memories WHERE space=? AND kind='person' AND subject=? AND status='active' AND source!='told' ORDER BY id""", (space, subject))

    async def refresh_impressions(self, platform: str, room: str, touched: dict[str, str], roles: list[str]):
        """备忘被挤出「最新 memory_person_limit 条」的，并进印象。同一个人在几个角色的空间里有同样的备忘时，只调一次模型，结果写给每个空间。"""
        for subject, name in touched.items():
            groups: dict[tuple, list[tuple[str, dict | None, list[dict]]]] = {}
            for sp in dict.fromkeys(self.space("person", r, platform, room) for r in (roles or [None])):
                limit = int(self.settings.get("memory_person_limit", roles[0] if roles else None, platform, room) or 100)
                notes = self.note_rows(sp, subject)
                squeezed = notes[:-limit] if limit > 0 and len(notes) > limit else []
                imp = self.impression_row(sp, subject)
                upto = meta_of(imp).get("upto", 0) if imp else 0
                new = [r for r in squeezed if r["id"] > upto]
                if new:
                    sig = ((imp["content"] if imp else ""), tuple((r["content"], r["source"], int(r["ts"])) for r in new))
                    groups.setdefault(sig, []).append((sp, imp, new))
            for sig, targets in groups.items():
                text = await self.write_impression(name, sig[0], targets[0][2])
                if not text:
                    continue
                for sp, imp, new in targets:
                    self.save_impression(sp, subject, name, text, max(r["id"] for r in new), room, imp)

    async def write_impression(self, name: str, old: str, notes: list[dict]) -> str:
        lines = "\n".join(f"- （{day_text(r['ts'])}，{SOURCE_LABEL.get(r['source'], '她推测的')}）{r['content']}" for r in notes)
        return (await self.llm.chat("mem", [{"role": "user", "content": IMPRESSION_PROMPT.format(name=name, old=old or "（还没有）", notes=lines)}]) or "").strip()

    def save_impression(self, space: str, subject: str, name: str, text: str, upto: int, room: str | None, imp: dict | None):
        meta = json.dumps({"upto": upto}, ensure_ascii=False)
        if imp:
            self.db.x("UPDATE memories SET content=?, ts=?, subject_name=?, meta=? WHERE id=?", (text, time.time(), name, meta, imp["id"]))
        else:
            self.add(space, "impression", subject, name, text, "inferred", None, room, status="active", meta={"upto": upto})

    async def rebuild_impression(self, space: str, subject: str, name: str, room: str | None = None, upto: int | None = None):
        """被删内容涉及这份印象：用剩下的、原来并进过它的备忘重写；一条都不剩就不留印象。upto：原印象折到了哪条备忘（印象行已经被删时由调用方给）。"""
        imp = self.impression_row(space, subject)
        if imp:
            upto = meta_of(imp).get("upto", 0) if upto is None else upto
            self.db.x("DELETE FROM memories WHERE id=?", (imp["id"],))
        if not upto:
            return
        notes = [r for r in self.note_rows(space, subject) if r["id"] <= upto]
        if notes:
            text = await self.write_impression(name, "", notes)
            if text:
                self.save_impression(space, subject, name, text, upto, room, None)

    # ---------- 读取 ----------
    def subject_uids(self, key: str, platform: str) -> set[str]:
        if key.startswith("person:"):
            p = self.people.persons.get(key[7:])
            return set(self.people.uids(p, platform)) if p else set()
        parts = key.split(":", 2)
        return {parts[2]} if len(parts) == 3 and parts[0] == "uid" and parts[1] == platform else set()

    def load_units(self, space: str) -> tuple[dict[int, Unit], list[Unit]]:
        """（全部单位, 最粗一层：没被并进上层的）。每个单位挂上它所并的下一层。"""
        units = {r["id"]: Unit(r) for r in self.unit_rows(space)}
        for u in units.values():
            u.children = [units[p] for p in u.parts if p in units]
        roots = sorted((u for u in units.values() if not u.merged), key=lambda u: (u.t1, u.id))
        return units, roots

    @staticmethod
    def note_line(r: dict, name: str) -> str:
        return f"- 关于{name}（{day_text(r['ts'])}，{SOURCE_LABEL.get(r['source'], '她推测的')}）：{r['content']}"

    def compose(self, role, platform, chat, subjects: list[tuple[str, str]], context_text: str, dm: str | None = None,
                weights: dict | None = None, budget: int | None = None) -> tuple[list[str], set]:
        """记忆一节的各段文字。budget：这一节最多多少字（None = 不限）。空间紧时的先后：
        在场的人的印象和本人要求记住的（必须放）→ 最粗一层前情（盖住全部较早的历史）→ 在场的人的最新备忘 → 取回（至多占 75%）→ 剩下的给前情从新往旧细化。"""
        if chat and not self.enabled(platform, chat):
            self.last_stats = {"budget": budget, "display": 0, "retrieved": 0, "dropped": 0, "left": None if budget is None else int(budget)}
            return [], set()
        weights = weights or {}
        inf = budget is None
        left = float("inf") if inf else max(0, int(budget))
        used: set = set()
        limit = int(self.settings.get("memory_person_limit", role, platform, chat) or 0)
        sp = self.space("person", role, platform, chat, dm)

        # 1) 在场的人：印象、本人要求记住的（必须放）；最新备忘（尽量放）；其余备忘进取回的候选
        imp_lines, told_lines, notes, old_notes = [], [], [], []
        for key, name in subjects:
            imp = self.impression_row(sp, key)
            if imp and imp["status"] == "active" and imp["content"].strip():
                imp_lines.append((key, f"- 关于{name}，她目前的印象：{imp['content'].strip()}"))
                used.add(imp["id"])
            rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='person' AND subject=? AND status='active' ORDER BY id", (sp, key))
            told = [r for r in rows if r["source"] == "told"]
            rest = [r for r in rows if r["source"] != "told"]
            for r in told:
                told_lines.append((key, r["ts"], self.note_line(r, name), r["id"]))
                used.add(r["id"])
            keep = rest[-limit:] if limit > 0 else []
            for r in keep:
                notes.append((r["ts"], r["id"], key, name, r))
            for r in (rest[:-limit] if limit > 0 else rest):
                old_notes.append((key, name, r))
        mandatory = sum(len(t) + 1 for _, t in imp_lines) + sum(len(t) + 1 for _, _, t, _ in told_lines)
        left -= mandatory

        # 2) 最粗一层前情，盖住全部较早的历史；盖不住就从最早的单位开始让
        units, roots, display, dropped = {}, [], [], []
        if chat:
            units, roots = self.load_units(self.space("recap", role, platform, chat))
            display = [u for u in roots if u.text]
            cost = sum(u.cost for u in display)
            while display and cost > left:
                u = display.pop(0)
                cost -= u.cost
                dropped.append(u)
            left -= cost

        # 3) 在场的人的最新备忘：放不下时按相关度让——先让这次没被提到的人的，同一相关分里从最早的开始（让掉的进取回的候选）
        notes.sort(key=lambda x: (x[0], x[1]))

        def note_score(key: str) -> int:
            return max([weights.get(u, 0) for u in self.subject_uids(key, platform)] or [0])
        total = sum(len(self.note_line(r, n)) + 1 for _, _, _, n, r in notes)
        dropped_ids: set[int] = set()
        for _, _, key, name, r in sorted(notes, key=lambda x: (note_score(x[2]), x[0], x[1])):
            if total <= left:
                break
            dropped_ids.add(r["id"])
            total -= len(self.note_line(r, name)) + 1
            old_notes.append((key, name, r))
        shown_notes = [x for x in notes if x[1] not in dropped_ids]
        left -= total
        for _, _, _, _, r in shown_notes:
            used.add(r["id"])

        # 4) 取回：已经被并进上层、眼下没摆出来的第一层前情，加在场的人眼下没摆出来的旧备忘；按编号打分，分高的先取，同分取较新的
        def score_uids(uids) -> int:
            return sum(weights.get(u, 0) for u in uids)
        retrieved: list[tuple] = []  # (ts, kind, obj, line, score)
        shown_ids = {u.id for u in display}
        pool = []
        for u in units.values():
            if u.level == 1 and u.text and u.id not in shown_ids:
                s = score_uids(u.uids)
                if s > 0:
                    pool.append((s, u.t1, "unit", u, u.line))
        for key, name, r in old_notes:
            s = max([weights.get(u, 0) for u in self.subject_uids(key, platform)] or [0])
            if s > 0:
                pool.append((s, r["ts"], "note", r, self.note_line(r, name)))
        pool.sort(key=lambda x: (-x[0], -x[1]))
        share = min(100, max(0, int(self.settings.get("memory_retrieve_share", role, platform, chat) or 0))) / 100  # 取回最多占记忆一节的多少（默认 75%）
        most = max(0, int(self.settings.get("memory_retrieve_max", role, platform, chat) or 0))  # 一次至多取多少条（默认 1000）
        cap = left if inf else min(share * (budget or 0), left)
        spent, picked = 0, []
        for item in pool:
            if len(picked) >= most:
                break
            if spent + len(item[4]) + 1 > cap:
                continue
            picked.append(item)
            spent += len(item[4]) + 1
        picked_units = {it[3].id for it in picked if it[2] == "unit"}
        left -= spent

        # 5) 剩下的空间：前情从最新往前，把单位换成覆盖同一范围的下一层，越新越细，换不下就停；换出来的若已经在取回里，就从取回里拿走（不重复）
        while True:
            cand = next((u for u in sorted(display, key=lambda x: (x.t1, x.id), reverse=True) if u.children), None)
            if cand is None:
                break
            kids = [c for c in cand.children if c.text]
            delta = sum(c.cost for c in kids if c.id not in picked_units) - cand.cost
            if delta > left:
                break
            display.remove(cand)
            display.extend(kids)
            left -= delta
            for c in kids:
                if c.id in picked_units:
                    picked_units.discard(c.id)
                    picked = [it for it in picked if not (it[2] == "unit" and it[3].id == c.id)]
        display.sort(key=lambda u: (u.t0, u.t1, u.id))

        # 拼文字
        out: list[str] = []
        people_lines: list[str] = []
        for key, name in subjects:
            people_lines += [t for k, t in imp_lines if k == key]
            people_lines += [t for k, ts, t, _ in sorted(told_lines) if k == key]
            people_lines += [self.note_line(r, n) for _, _, k, n, r in shown_notes if k == key]
        if people_lines:
            out.append("【你记得的事】（只是谁说过什么、你怎么看的，不改变人物表里的身份和关系）\n" + "\n".join(people_lines))
        if display:
            out.append("【这个群以前的事】（按时间先后，越早越概括）\n" + "\n".join(u.line for u in display))
            used |= {u.id for u in display}
        if chat:
            memes = [r for r in self.db.rows("""SELECT id, subject, content FROM memories WHERE space=? AND kind='meme' AND status='active'
                     ORDER BY id DESC""", (self.space("meme", role, platform, chat),)) if r["subject"] and r["subject"] in context_text][:5]
            lines = []
            for r in memes:  # 群梗也占记忆一节的预算（超了按现有顺序少放）
                line = f"- {r['subject']}：{r['content']}"
                if not inf and len(line) + 1 > left:
                    break
                lines.append(line)
                used.add(r["id"])
                left -= len(line) + 1
            if lines:
                out.append("【群梗】\n" + "\n".join(lines))
        if picked:
            picked.sort(key=lambda x: x[1])
            out.append("【和眼下这些人有关的旧事】\n" + "\n".join(it[4] for it in picked))
            used |= {it[3].id if it[2] == "unit" else it[3]["id"] for it in picked}
        self.last_stats = {"budget": budget, "display": len(display), "retrieved": len(picked), "dropped": len(dropped), "left": None if inf else int(left)}
        return out, used

    def classic(self, role, platform, chat, subjects: list[tuple[str, str]], context_text: str, dm: str | None = None,
                weights: dict | None = None, budget: int | None = None) -> tuple[list[str], set]:
        return self.compose(role, platform, chat, subjects, context_text, dm, weights, budget)

    def section(self, role, platform, chat, subjects: list[tuple[str, str]], context_text: str, dm: str | None = None) -> str:
        """subjects: [(subject_key, 显示名)]，只放当前对话里出现的人。"""
        return "\n\n".join(self.compose(role, platform, chat, subjects, context_text, dm)[0])

    def semantic_spaces(self, role, platform, chat, dm: str | None = None) -> list[str]:
        if not self.settings.g("semantic_enabled") or (chat and not self.enabled(platform, chat)):
            return []
        off = set(self.db.kv("semantic_off") or [])
        kinds = ("person", "recap", "meme") if chat else ("person",)
        return [sp for sp in dict.fromkeys(self.space(k, role, platform, chat, dm) for k in kinds) if sp not in off]

    async def full_section(self, role, platform, chat, subjects, context_text: str, query: str, dm: str | None = None,
                           weights: dict | None = None, budget: int | None = None) -> str:
        """分层前情 + 人物印象和备忘 + 按人取回 + 群梗 + 语义回忆（在当前记忆空间范围内找意思相近的旧记忆）。同一条记忆只放一次。"""
        parts, used = self.compose(role, platform, chat, subjects, context_text, dm, weights, budget)
        left = self.last_stats.get("left")  # 记忆一节还剩多少字（None = 不限）：语义回忆也在这里面
        if self.embedder:
            try:
                hits = await self.embedder.search(query, self.semantic_spaces(role, platform, chat, dm),
                                                  int(self.settings.g("semantic_top_k")),
                                                  float(self.settings.g("semantic_min_score")), used)
            except Paused:
                hits = []
            except Exception as e:
                self.log("warn", "memory", f"语义回忆出错（这次先不用）：{e}")
                hits = []
            if hits:
                head = "【想起的旧事】（和眼下聊的话题意思相关；谁说过什么不改变人物表里的身份和关系）\n"
                lines, cost = [], len(head) + 1
                for r in hits:
                    if r["kind"] == "person":
                        line = f"- 关于{r['subject_name']}（{day_text(r['ts'])}，{SOURCE_LABEL.get(r['source'], '她推测的')}）：{r['content']}"
                    elif r["kind"] == "meme":
                        line = f"- 群梗「{r['subject_name']}」：{r['content']}"
                    elif r["kind"] == "doc":
                        line = f"- 文件《{r['subject_name']}》里：{r['content']}"
                    else:
                        line = f"- 以前：{r['content']}"
                    if left is not None and cost + len(line) + 1 > left:
                        break  # 语义命中按相似度排：放不下就到这里为止，不跳过高分的去捡低分的
                    lines.append(line)
                    cost += len(line) + 1
                if lines:
                    parts.append(head + "\n".join(lines))
        return "\n\n".join(parts)

    # ---------- 删除（前情单位连同覆盖它们的上层一起删） ----------
    def ancestors(self, ids: set[int]) -> set[int]:
        """覆盖这些单位的所有上层（一层层往上找）。"""
        out, frontier = set(), set(ids)
        rows = self.db.rows("SELECT id, meta FROM memories WHERE kind='recap'")
        parents: dict[int, list[int]] = {}
        for r in rows:
            for p in meta_of(r).get("parts") or []:
                parents.setdefault(p, []).append(r["id"])
        while frontier:
            nxt = set()
            for i in frontier:
                for up in parents.get(i, []):
                    if up not in out and up not in ids:
                        out.add(up)
                        nxt.add(up)
            frontier = nxt
        return out

    def unit_matches_person(self, r: dict, uids_by_platform: dict[str, set[str]], names: list[str]) -> bool:
        """这个前情单位里有没有这个人：按编号（说话、被提到）对；老单位没有编号，就看原话摘录里的名字（带〔某某本人〕的）和前情文字里有没有他的名字。"""
        m = meta_of(r)
        plat = (r.get("room") or "").split(":")[0]
        if m.get("uids"):
            return bool(set(m["uids"]) & uids_by_platform.get(plat, set()))
        try:
            src = json.loads(r.get("src") or "[]")
        except ValueError:
            src = []
        return any(n and (n in (r["content"] or "") or any(f"〔{n}本人〕" in s.get("name", "") for s in src)) for n in names if len(n) >= 2)

    def unmerge_survivors(self, deleted_rows: list[dict], deleted_ids: set[int]):
        """上层被删了：它所并的、还在的下层重新变成「没被并」，以后由它们重新并。"""
        for r in deleted_rows:
            for p in meta_of(r).get("parts") or []:
                if p not in deleted_ids:
                    self.db.x("UPDATE memories SET merged=0 WHERE id=?", (p,))

    async def rewrite_ancestors(self, unit_id: int):
        """某个单位被改过：覆盖它的上层，照改过的重写（从下往上一层层）。"""
        r = self.db.rows("SELECT * FROM memories WHERE id=?", (unit_id,))
        if not r:
            return
        up_ids = sorted(self.ancestors({unit_id}), key=lambda i: self.db.rows("SELECT level FROM memories WHERE id=?", (i,))[0]["level"])
        for uid in up_ids:
            row = self.db.rows("SELECT * FROM memories WHERE id=?", (uid,))[0]
            kids = [Unit(k) for k in self.db.rows(f"SELECT * FROM memories WHERE id IN ({','.join(str(p) for p in meta_of(row).get('parts') or [0])}) ORDER BY id")]
            full = [k for k in kids if k.text]
            if not full:
                continue
            room = row["room"] or ""
            limit = max(30, int(sum(len(k.text) for k in full) * self.ratio(room or "x:x")))
            parts = "\n\n".join(f"{i + 1}.（{span_text(k.t0, k.t1)}）{k.text}" for i, k in enumerate(full))
            text = (await self.llm.chat("mem", [{"role": "user", "content": MERGE_PROMPT.format(chat=room, parts=parts, limit=limit)}],
                                        platform=room.split(":")[0] or None, chat=room or None) or "").strip()
            if text:
                self.db.x("UPDATE memories SET content=?, ts=? WHERE id=?", (text, time.time(), uid))
        if self.embedder:
            self.embedder.kick()

    def drop_conflicting_uppers(self, restored: set[int]):
        """撤销删除时：恢复回来的上层，和这期间新并出来的上层（所并的下层有重叠）只能留一个——留恢复回来的，新的连同它再往上的删掉，它多出来的下层变回「没被并」。"""
        rows = self.db.rows("SELECT * FROM memories WHERE kind='recap'")
        mine = {r["id"]: set(meta_of(r).get("parts") or []) for r in rows if r["id"] in restored and meta_of(r).get("parts")}
        taken = set().union(*mine.values()) if mine else set()
        bad = {r["id"] for r in rows if r["id"] not in restored and set(meta_of(r).get("parts") or []) & taken}
        if not bad:
            return
        gone = bad | self.ancestors(bad)
        victims = [r for r in rows if r["id"] in gone]
        for r in victims:
            for p in meta_of(r).get("parts") or []:
                if p not in gone and p not in taken:
                    self.db.x("UPDATE memories SET merged=0 WHERE id=?", (p,))
        self.db.x(f"DELETE FROM memories WHERE id IN ({','.join(map(str, gone))})")
        self.db.x(f"DELETE FROM memvec WHERE mem_id IN ({','.join(map(str, gone))})")

    def delete_units(self, ids: list[int]) -> int:
        """面板上删前情单位：连同覆盖它们的上层一起删，上层所并的、还在的下层变回「没被并」（以后重新并）。返回删了几条。"""
        have = {r["id"] for r in self.db.rows(f"SELECT id FROM memories WHERE kind='recap' AND id IN ({','.join(str(int(i)) for i in ids) or '0'})")}
        gone = have | self.ancestors(have)
        if not gone:
            return 0
        rows = self.db.rows(f"SELECT * FROM memories WHERE id IN ({','.join(map(str, gone))})")
        self.unmerge_survivors(rows, gone)
        self.db.x(f"DELETE FROM memvec WHERE mem_id IN ({','.join(map(str, gone))})")
        self.db.x(f"DELETE FROM memories WHERE id IN ({','.join(map(str, gone))})")
        return len(gone)

    def forget_subject(self, subjects: list[str]) -> int:
        n = 0
        for s in subjects:
            n += self.db.x("DELETE FROM memories WHERE kind IN ('person','impression') AND subject=?", (s,)).rowcount
        return n

    def forget_text(self, arg: str, spaces: list[str]) -> int:
        q = ",".join("?" * len(spaces))
        return self.db.x(f"""DELETE FROM memories WHERE space IN ({q}) AND kind IN ('person','recap','meme') AND (subject_name=? OR subject=? OR content LIKE ?)""",
                         (*spaces, arg, arg, f"%{arg}%")).rowcount

    def prune(self):
        now = time.time()
        for room in [r["room"] for r in self.db.rows("SELECT DISTINCT room FROM memories WHERE room IS NOT NULL")]:
            platform = room.split(":")[0]
            days = self.settings.get("memory_retention_days", platform=platform, chat=room)
            if not days:  # 「本人要求记住」的、风格画像、表情包和印象不按天数清
                continue
            expire_person = bool(self.settings.get("memory_expire_person", platform=platform, chat=room))
            rows = self.db.rows("""SELECT id, space, kind, subject, subject_name FROM memories WHERE room=? AND ts<? AND source!='told'
                                   AND kind NOT IN ('style','sticker','impression')""" + ("" if expire_person else " AND kind!='person'"),
                                (room, now - days * 86400))
            ids = [r["id"] for r in rows]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.db.x(f"DELETE FROM memories WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            if expire_person:  # 人物备忘也过期了：用这个人剩下的备忘重写他在这个空间里的印象（和面板删备忘后一样）
                affected = {(r["space"], r["subject"]): r["subject_name"] or r["subject"] for r in rows if r["kind"] == "person" and r["subject"]}
                for (space, subject), name in affected.items():
                    if self.impression_row(space, subject) and self.spawn:
                        self.spawn(self.rebuild_impression(space, subject, name, room))
        self.db.x("DELETE FROM memvec WHERE mem_id NOT IN (SELECT id FROM memories)")

    # ---------- 老数据：补上前情单位的覆盖范围（只补推得出的，推不出的照原样用） ----------
    def migrate_meta(self) -> dict:
        """老前情没有覆盖范围：第一层按「上一段的创建时间到这一段的创建时间」和那段时间的消息表补时间和消息编号、说话的人；
        老的上层按它存的下层摘录对回下层，对得上的补上所并各段和范围，对不上的不动（不往细里展开）。不调模型。"""
        done = {"l1": 0, "upper": 0}
        spaces = [r["space"] for r in self.db.rows("SELECT DISTINCT space FROM memories WHERE kind='recap'")]
        for sp in spaces:
            rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='recap' ORDER BY id", (sp,))
            l1 = [r for r in rows if (r["level"] or 1) == 1]
            prev_ts, prev_m1 = None, 0
            for r in l1:
                if r["meta"]:
                    m = meta_of(r)
                    prev_ts, prev_m1 = m.get("t1") or r["ts"], m.get("m1") or prev_m1
                    continue
                room = r["room"] or ""
                msgs = self.db.rows("""SELECT id, ts, user_id FROM messages WHERE room=? AND ts<=? AND id>? AND kind IN ('human','bot','import') ORDER BY id""",
                                    (room, r["ts"], prev_m1)) if room else []
                t0 = (prev_ts if prev_ts is not None else (msgs[0]["ts"] if msgs else r["ts"]))
                meta = {"t0": min(t0, r["ts"]), "t1": r["ts"]}
                if msgs:
                    meta.update(m0=msgs[0]["id"], m1=msgs[-1]["id"], uids=sorted({str(m["user_id"]) for m in msgs if m["user_id"]}))
                    prev_m1 = msgs[-1]["id"]
                if not (r["content"] or "").strip():
                    meta["empty"] = 1
                self.db.x("UPDATE memories SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), r["id"]))
                prev_ts = r["ts"]
                done["l1"] += 1
            rows = self.db.rows("SELECT * FROM memories WHERE space=? AND kind='recap' ORDER BY id", (sp,))
            by_level: dict[int, list[dict]] = {}
            for r in rows:
                by_level.setdefault(r["level"] or 1, []).append(r)
            claimed: set[int] = set()
            for lvl in sorted(k for k in by_level if k > 1):
                for r in by_level[lvl]:
                    if r["meta"]:
                        claimed |= set(meta_of(r).get("parts") or [])
                        continue
                    try:
                        src = json.loads(r["src"] or "[]")
                    except ValueError:
                        src = []
                    want = [s.get("text", "") for s in src]
                    cand = [c for c in by_level.get(lvl - 1, []) if c["merged"] and c["id"] < r["id"] and c["id"] not in claimed]
                    parts = []
                    for t in want:
                        c = next((c for c in cand if (c["content"] or "")[:200] == t and c["id"] not in parts), None)
                        if c:
                            parts.append(c["id"])
                    if not want or len(parts) != len(want):
                        continue  # 对不上：照原样用
                    kids = [meta_of(c) for c in cand if c["id"] in parts]
                    claimed |= set(parts)
                    m0s, m1s = [k.get("m0") for k in kids if k.get("m0")], [k.get("m1") for k in kids if k.get("m1")]
                    meta = {"t0": min(k.get("t0") or r["ts"] for k in kids), "t1": max(k.get("t1") or r["ts"] for k in kids), "parts": parts,
                            "uids": sorted(set().union(*[set(k.get("uids") or []) for k in kids]))}
                    if m0s:
                        meta.update(m0=min(m0s), m1=max(m1s))
                    self.db.x("UPDATE memories SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), r["id"]))
                    done["upper"] += 1
        return done


# ---------- 聊天记录导入（QQ 聊天记录导出插件的 JSON / TXT，以及 QQ 自带的 TXT 导出）----------

TXT_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2})\s+(.+?)[(<]([^()<>]+)[)>]\s*$")


def parse_chatlog(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    if text.lstrip().startswith(("{", "[")):
        data = json.loads(text)
        items = data if isinstance(data, list) else next((data[k] for k in ("messages", "msgs", "data", "list") if isinstance(data.get(k), list)), [])
        out = []
        for m in items:
            if not isinstance(m, dict):
                continue
            s = m.get("sender") if isinstance(m.get("sender"), dict) else {}
            name = s.get("card") or s.get("name") or s.get("nickname") or m.get("senderName") or m.get("nickname") or m.get("name") or ""
            uid = s.get("uin") or s.get("user_id") or s.get("uid") or m.get("senderUin") or m.get("uin") or m.get("user_id") or m.get("sender") or ""
            c = m.get("content") if m.get("content") is not None else m.get("text", m.get("message", ""))
            if isinstance(c, dict):
                c = c.get("text") or ""
            if isinstance(c, list):
                c = "".join(e.get("text", "") if isinstance(e, dict) else str(e) for e in c)
            c = str(c).strip()
            if c and (name or uid):
                out.append({"user_id": str(uid if not isinstance(uid, dict) else ""), "name": str(name or uid), "text": c})
        return out
    out, cur = [], None
    for line in text.splitlines():
        m = TXT_HEAD.match(line.strip())
        if m:
            cur = {"user_id": m.group(3).strip(), "name": m.group(2).strip(), "text": ""}
            out.append(cur)
        elif cur is not None and line.strip():
            cur["text"] = (cur["text"] + "\n" + line.strip()).strip()
    return [m for m in out if m["text"]]
