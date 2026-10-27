"""QQ：经 NapCat 的 OneBot v11 正向 WebSocket。程序替用户启动 NapCat、写好配置，再连上去。"""
import asyncio
import json
import re
import time

import aiohttp

from .base import Channel, Msg, Muted
from .napcat import NapCatProcess, default_root, new_token
from .. import tls

# 常用 QQ 表情（OneBot face id）
FACES = {
    "惊讶": 0, "撇嘴": 1, "色": 2, "发呆": 3, "得意": 4, "流泪": 5, "害羞": 6, "闭嘴": 7, "睡": 8, "大哭": 9,
    "尴尬": 10, "发怒": 11, "调皮": 12, "呲牙": 13, "微笑": 14, "难过": 15, "酷": 16, "抓狂": 18, "吐": 19,
    "偷笑": 20, "可爱": 21, "白眼": 22, "傲慢": 23, "困": 25, "惊恐": 26, "流汗": 27, "憨笑": 28, "奋斗": 30,
    "疑问": 32, "嘘": 33, "晕": 34, "衰": 36, "骷髅": 37, "敲打": 38, "再见": 39, "发抖": 41, "爱情": 42,
    "猪头": 46, "拥抱": 49, "蛋糕": 53, "玫瑰": 63, "爱心": 66, "赞": 76, "踩": 77, "握手": 78, "胜利": 79,
    "冷汗": 96, "擦汗": 97, "抠鼻": 98, "鼓掌": 99, "坏笑": 101, "哈欠": 104, "鄙视": 105, "委屈": 106,
    "快哭了": 107, "阴险": 108, "吓": 110, "可怜": 111, "菜刀": 112, "拳头": 120, "OK": 124, "眨眼睛": 172,
    "泪奔": 173, "无奈": 174, "卖萌": 175, "喷血": 177, "斜眼笑": 178, "doge": 179, "惊喜": 180, "笑哭": 182,
    "托腮": 212, "脑阔疼": 262, "捂脸": 264, "辣眼睛": 265, "哦哟": 266, "头秃": 267, "问号脸": 268,
    "暗中观察": 269, "emm": 270, "吃瓜": 271, "呵呵哒": 272, "我酸了": 273, "汪汪": 277, "摸鱼": 285,
}
FACE_NAMES = {v: k for k, v in FACES.items()}
FACE_RE = re.compile(r"\[(" + "|".join(map(re.escape, sorted(FACES, key=len, reverse=True))) + r")\]")


# NapCat 发群消息被禁言时的报错：EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg … "result": 120
# 只认这个（和明说「被禁言」的）；网络、风控（「风控」另有处理）、消息过长等别的失败不是禁言
MUTE_ERR = re.compile(r'"result"\s*:\s*120\b|被禁言')


def is_mute_error(msg: str) -> bool:
    return bool(MUTE_ERR.search(msg or ""))


def is_sticker_image(d: dict) -> bool:
    """NapCat 的 image 段是不是表情（自定义表情、动图）：sub_type 为 1，或 summary 里写着「表情」（如「[动画表情]」）。
    推断，来源是 NapCat 文档里的示例和 OneBot 社区的通常写法，没在真机上核实过；部署后按实际数据改这里。"""
    return str(d.get("sub_type")) == "1" or "表情" in str(d.get("summary") or "")


def to_segments(text: str, faces: bool) -> list[dict]:
    if not faces:
        return [{"type": "text", "data": {"text": text}}]
    out, pos = [], 0
    for m in FACE_RE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "data": {"text": text[pos:m.start()]}})
        out.append({"type": "face", "data": {"id": str(FACES[m.group(1)])}})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "data": {"text": text[pos:]}})
    return out or [{"type": "text", "data": {"text": text}}]


class QQChannel(Channel):
    platform = "qq"
    supports_image = True
    supports_at = True

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.session: aiohttp.ClientSession | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.seq = 0
        self.stopping = False
        self.napcat: NapCatProcess | None = None
        self.qr = ""
        self.task: asyncio.Task | None = None

    # ---- 配置 ----
    @property
    def port(self) -> int:
        return int(self.account.get("ws_port") or 3101)

    @property
    def url(self) -> str:
        return self.account.get("ws_url") or f"ws://127.0.0.1:{self.port}"

    def token(self) -> str:
        db = self.engine.db
        t = db.secret(f"onebot:{self.aid}")
        if not t:
            t = new_token()
            db.set_secret(f"onebot:{self.aid}", t)
        return t

    def webui_token(self) -> str:
        db = self.engine.db
        t = db.secret("napcat_webui")
        if not t:
            t = new_token()[:16]
            db.set_secret("napcat_webui", t)
        return t

    def on_qr(self, url: str):
        self.qr = url
        self.set_status("waiting_login", "请用手机 QQ 扫码登录")
        self.engine.emit("qr", {"id": self.aid, "url": url})

    # ---- 启停 ----
    async def start(self):
        self.stopping = False
        self.set_status("starting", "正在启动")
        if self.account.get("manage_napcat", True):
            root = self.engine.db.kv("napcat_dir") or str(default_root())
            self.napcat = NapCatProcess(self, root, str(self.account["qq"]), self.port, self.token(),
                                        int(self.account.get("webui_port") or 6101), self.engine.settings.g("hide_console"),
                                        self.webui_token())
            try:
                await self.napcat.start()
            except Exception as e:
                self.set_status("error", str(e))
                self.engine.log("error", "napcat", f"QQ {self.account.get('qq')}：{e}")
                return
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.stopping = True
        if self.ws and self.napcat:
            try:
                await asyncio.wait_for(self.call("bot_exit"), 3)
            except Exception:
                pass
        if self.task:
            self.task.cancel()
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
            self.session = None
        if self.napcat:
            await self.napcat.stop()

    async def _loop(self):
        headers = {"Authorization": f"Bearer {self.token()}"}
        offline_since = None
        while not self.stopping:
            try:
                if self.session is None:
                    self.session = tls.session()
                async with self.session.ws_connect(self.url, headers=headers, heartbeat=30, max_msg_size=20 << 20) as ws:
                    self.ws = ws
                    offline_since = None
                    asyncio.create_task(self._on_connect())
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            self._dispatch(json.loads(m.data))
                        elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            self.ws = None
            if self.stopping:
                return
            if self.status == "online":
                offline_since = asyncio.get_running_loop().time()
                self.set_status("starting", "连接断开，正在重连")
            elif offline_since and asyncio.get_running_loop().time() - offline_since > 120:
                self.engine.notify(f"QQ {self.account.get('qq')} 掉线超过 2 分钟", self.account)
                offline_since = None
            await asyncio.sleep(3)

    async def _on_connect(self):
        try:
            info = await self.call("get_login_info")
            self.self_id, self.self_name = str(info.get("user_id", "")), info.get("nickname", "")
            self.qr = ""
            self.set_status("online", f"已登录 {self.self_name}（{self.self_id}）")
            self.engine.log("info", "system", f"QQ {self.self_id} 已连接")
            groups = await self.call("get_group_list") or []
            for g in groups:
                self.engine.register_chat(self.aid, "qq", str(g["group_id"]), g.get("group_name", ""))
            b = self.engine.bots.get(self.aid)
            if b:  # 上线时主动查一遍：这个号在哪些群里被禁言（含全员禁言）
                asyncio.create_task(self.engine.scan_bans(b, [str(g["group_id"]) for g in groups]))
                self.engine.spawn(self.engine.stickers.sync_favorites(b, min_age=600))  # 上线时对一次收藏的表情（没开「用账号收藏的表情」就什么都不做；十分钟内刚同步过的不重复）
        except Exception as e:
            self.engine.log("warn", "system", f"QQ 连接后初始化失败：{e}")

    # ---- API ----
    async def call(self, action: str, **params):
        if not self.ws:
            raise ConnectionError("QQ 未连接")
        self.seq += 1
        echo = f"mb{self.seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        try:
            r = await asyncio.wait_for(fut, 30)
        finally:
            self.pending.pop(echo, None)
        if r.get("status") == "failed" or r.get("retcode", 0) != 0:
            msg = r.get("message") or r.get("wording") or ""
            if action in ("send_group_msg", "send_msg") and params.get("group_id") and is_mute_error(msg):
                self.engine.ban_update(self.aid, f"qq:{params['group_id']}", unknown=True)  # 只知道被禁言，不知道到什么时候：之后靠复查确认
                raise Muted(f"{action} 失败：这个号在群里被禁言了")
            if "风控" in msg:
                self.engine.notify(f"QQ {self.self_id} 触发风控：{msg}", self.account)
            raise RuntimeError(f"{action} 失败：{msg or r.get('retcode')}")
        return r.get("data")

    def _dispatch(self, ev: dict):
        if "echo" in ev and ev["echo"] in self.pending:
            fut = self.pending[ev["echo"]]
            if not fut.done():
                fut.set_result(ev)
            return
        pt = ev.get("post_type")
        if pt == "message":
            asyncio.create_task(self._on_message(ev))
        elif pt == "notice":
            self._on_notice(ev)
        elif pt == "request":
            b = self.engine.bots.get(self.aid)
            if b:
                asyncio.create_task(self.engine.on_request(b, ev.get("request_type"), str(ev.get("user_id")),
                                                           ev.get("flag", ""), ev.get("sub_type", "")))

    def _on_notice(self, ev: dict):
        nt = ev.get("notice_type")
        if nt == "bot_offline":
            self.engine.notify(f"QQ {self.self_id} 被踢下线：{ev.get('message', '')}", self.account)
            self.set_status("error", "被踢下线")
        elif nt == "group_ban" and ev.get("group_id"):
            self._on_group_ban(ev)
        elif nt == "group_upload" and ev.get("file"):
            f = ev["file"]
            asyncio.create_task(self.engine.on_message(Msg(
                "qq", self.aid, "group", str(ev["group_id"]), str(ev.get("user_id")), str(ev.get("user_id")), "",
                files=[{"name": f.get("name") or "文件", "id": f.get("id") or "", "size": int(f.get("size") or 0),
                        "busid": f.get("busid"), "group": str(ev["group_id"])}],
                dedup=f"upload|{ev['group_id']}|{f.get('id')}")))
        elif nt == "notify" and ev.get("sub_type") == "poke" and ev.get("group_id"):
            asyncio.create_task(self.engine.on_message(Msg(
                "qq", self.aid, "group", str(ev["group_id"]), str(ev.get("user_id")), str(ev.get("user_id")), "",
                poke_target=str(ev.get("target_id")), dedup=f"poke|{ev.get('time')}|{ev.get('user_id')}")))

    def _on_group_ban(self, ev: dict):
        """群禁言通知：user_id 是被禁言的人（0 = 全员禁言），duration 是秒数（解除时是 0）。只管针对这个号的和全员禁言的。"""
        rk, uid, dur = f"qq:{ev['group_id']}", str(ev.get("user_id") or 0), int(ev.get("duration") or 0)
        if uid == str(self.self_id):
            if ev.get("sub_type") == "lift_ban" or dur <= 0:
                self.engine.ban_update(self.aid, rk, member_until=None, unknown=False)
            else:
                self.engine.ban_update(self.aid, rk, member_until=time.time() + dur, unknown=False)
        elif uid in ("0", ""):
            asyncio.create_task(self._refresh_ban(str(ev["group_id"])))  # 全员禁言开 / 关：要看这个号在群里是不是群主 / 管理员，所以重新查一次

    async def _refresh_ban(self, group_id: str):
        try:
            res = await self.query_ban(group_id, fresh=True)
        except Exception as ex:
            self.engine.log("debug", "system", f"[qq:{group_id}] 查禁言状态没成功：{ex}")
            return
        if res:
            self.engine.ban_update(self.aid, f"qq:{group_id}", unknown=False, **res)

    async def query_ban(self, chat_id, fresh=False):
        """主动查：这个号在这个群里被不被禁言（get_group_member_info 的 shut_up_timestamp 和 role，get_group_info 的 group_all_shut）。不往群里发任何东西。"""
        gid, uid = int(chat_id), int(self.self_id)
        info = await self.call("get_group_member_info", group_id=gid, user_id=uid, no_cache=bool(fresh)) or {}
        ginfo = await self.call("get_group_info", group_id=gid, no_cache=bool(fresh)) or {}
        until = float(info.get("shut_up_timestamp") or 0)
        if until > 1e11:  # 毫秒
            until /= 1000
        return {"member_until": until if until > time.time() else None, "all": bool(ginfo.get("group_all_shut")), "role": info.get("role") or "member"}

    async def _on_message(self, ev: dict):
        segs = ev.get("message") if isinstance(ev.get("message"), list) else [{"type": "text", "data": {"text": ev.get("raw_message", "")}}]
        text, at_ids, images, emotes, reply_id, files, stickers = [], [], [], [], "", [], []
        for s in segs:
            t, d = s.get("type"), s.get("data") or {}
            if t == "text":
                text.append(d.get("text", ""))
            elif t == "at":
                if str(d.get("qq")) != "all":
                    at_ids.append(str(d.get("qq")))
            elif t == "reply":
                reply_id = str(d.get("id", ""))
            elif t == "image":
                if d.get("url"):
                    images.append(d["url"])
                    if is_sticker_image(d):
                        emotes.append(d["url"])
            elif t == "file":
                files.append({"name": d.get("file") or d.get("name") or "文件", "id": d.get("file_id") or "",
                              "size": int(d.get("file_size") or 0), "url": d.get("url") or ""})
            elif t == "face":
                text.append(f"[{FACE_NAMES.get(int(d.get('id', -1)), '表情')}]")
            elif t in ("mface", "dice", "rps"):
                text.append("[表情]")
                if t == "mface" and d.get("url"):
                    stickers.append(d["url"])
        reply_uid = ""
        if reply_id:
            try:
                reply_uid = str((await self.call("get_msg", message_id=int(reply_id)) or {}).get("sender", {}).get("user_id", ""))
            except Exception:
                pass
        sender = ev.get("sender") or {}
        group = ev.get("message_type") == "group"
        seq = ev.get("real_seq") or ev.get("message_seq")
        await self.engine.on_message(Msg(
            "qq", self.aid, "group" if group else "private", str(ev.get("group_id") if group else ev.get("user_id")),
            str(ev.get("user_id")), sender.get("card") or sender.get("nickname") or "群友", "".join(text).strip(),
            msg_id=str(ev.get("message_id", "")), chat_name=ev.get("group_name", ""), at_ids=at_ids,
            reply_to_uid=reply_uid, reply_to_id=reply_id, images=images, emotes=emotes, files=files, stickers=stickers, sender_role=sender.get("role", ""),
            dedup=f"{ev.get('group_id')}|{ev.get('user_id')}|{seq}" if group and seq else ""))

    async def send_image(self, chat_type, chat_id, data, reply_to=None):
        import base64
        msg = ([{"type": "reply", "data": {"id": str(reply_to)}}] if reply_to else []) + \
            [{"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode()}}]
        if chat_type == "group":
            r = await self.call("send_group_msg", group_id=int(chat_id), message=msg)
        else:
            r = await self.call("send_private_msg", user_id=int(chat_id), message=msg)
        return [str((r or {}).get("message_id", ""))]

    supports_mention = True

    @staticmethod
    def with_mentions(text: str, mention: list, faces: bool) -> list[dict]:
        """文字里按先后顺序对上的「@名字」换成真正的 @ 段，原来那段文字不留（不重复）；找不到的跳过。@ 后面补一个空格，免得和后面的字连在一起。"""
        out, pos = [], 0
        for uid, name in mention:
            i = text.find("@" + name, pos)
            if i < 0:
                continue
            if i > pos:
                out += to_segments(text[pos:i], faces)
            out.append({"type": "at", "data": {"qq": str(uid)}})
            pos = i + 1 + len(name)
            if not text[pos:pos + 1].isspace():
                out.append({"type": "text", "data": {"text": " "}})
        if pos < len(text):
            out += to_segments(text[pos:], faces)
        return out or to_segments(text, faces)

    async def send(self, chat_type, chat_id, text, reply_to=None, at=None, mention=None):
        faces = self.engine.settings.get("qq_face", platform="qq", chat=f"qq:{chat_id}" if chat_type == "group" else None)
        msg = ([{"type": "reply", "data": {"id": str(reply_to)}}] if reply_to else []) + \
            [x for uid, _ in (at or []) for x in ({"type": "at", "data": {"qq": str(uid)}}, {"type": "text", "data": {"text": " "}})] + \
            (self.with_mentions(text, mention, faces) if mention else to_segments(text, faces))
        if chat_type == "group":
            r = await self.call("send_group_msg", group_id=int(chat_id), message=msg)
        else:
            r = await self.call("send_private_msg", user_id=int(chat_id), message=msg)
        return [str((r or {}).get("message_id", ""))]

    async def recall(self, chat_type, chat_id, msg_id):
        await self.call("delete_msg", message_id=int(msg_id))
        return True

    async def fetch_file(self, f, max_bytes):
        url = f.get("url") or ""
        if not url and f.get("id"):
            try:
                if f.get("group"):
                    r = await self.call("get_group_file_url", group_id=int(f["group"]), file_id=f["id"], busid=int(f.get("busid") or 0))
                else:
                    r = await self.call("get_file", file_id=f["id"])
                url = (r or {}).get("url") or ""
            except Exception:
                url = ""
        if not url:
            return None
        async with tls.session(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.get(url) as r:
                if r.status != 200 or (r.content_length or 0) > max_bytes:
                    return None
                buf = bytearray()
                async for chunk in r.content.iter_chunked(1 << 16):
                    buf += chunk
                    if len(buf) > max_bytes:
                        return None
                return bytes(buf)

    async def fetch_image(self, url):
        async with tls.session(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url) as r:
                return await r.read() if r.status == 200 else None

    async def set_profile(self, nickname="", signature="", avatar=None):
        if nickname or signature:
            await self.call("set_qq_profile", nickname=nickname or self.self_name, personal_note=signature)
        if avatar:
            import base64
            await self.call("set_qq_avatar", file="base64://" + base64.b64encode(avatar).decode())

    async def set_chat_card(self, chat_id, card):
        await self.call("set_group_card", group_id=int(chat_id), user_id=int(self.self_id), card=card)

    async def answer_request(self, kind, flag, sub_type, approve):
        if kind == "friend":
            await self.call("set_friend_add_request", flag=flag, approve=approve)
        else:
            await self.call("set_group_add_request", flag=flag, sub_type=sub_type, approve=approve)

    async def clean_cache(self):
        await self.call("clean_cache")
