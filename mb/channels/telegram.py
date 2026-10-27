"""Telegram：官方 Bot API，getUpdates 长轮询（不需要公网地址）。

群里要收到所有消息，得在 BotFather 里关掉隐私模式（/setprivacy → Disable）并重新拉进群；
不关的话只收得到 @她、回复她和命令。国内需要代理（在「模型与接口 → 代理地址」填，账号上选「使用代理」）。
"""
import asyncio
import json
import time

import aiohttp

from .base import Channel, Msg, Muted
from .. import tls

API = "https://api.telegram.org"
MAX_TEXT = 4096
# 发送被拒、说没有发言权限：可能是被限制发言（禁言），也可能只是权限不够，所以要再查一次确认才算
NO_RIGHTS = ("have no rights to send", "not enough rights to send", "CHAT_WRITE_FORBIDDEN")


def split_text(text: str, size: int = MAX_TEXT) -> list[str]:
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text]


def to_msg(aid: str, m: dict, self_id: str, self_username: str) -> Msg | None:
    """Telegram message 对象 → 统一消息。"""
    sender = m.get("from") or {}
    if str(sender.get("id", "")) == self_id or sender.get("is_bot"):
        return None
    chat = m["chat"]
    text = m.get("text") or m.get("caption") or ""
    ents = (m.get("entities") or []) + (m.get("caption_entities") or [])
    at_ids, handle = [], f"@{self_username}".lower()
    for e in ents:
        if e.get("type") == "mention" and text[e["offset"]:e["offset"] + e["length"]].lower() == handle:
            at_ids.append(self_id)
        elif e.get("type") == "text_mention" and str((e.get("user") or {}).get("id", "")) == self_id:
            at_ids.append(self_id)
    if at_ids:
        text = text.replace(f"@{self_username}", "").replace(f"@{self_username.lower()}", "").strip()
    reply = m.get("reply_to_message") or {}
    images = [f"tg:{max(m['photo'], key=lambda p: p.get('file_size', 0))['file_id']}"] if m.get("photo") else []
    files = []
    if m.get("document"):
        d = m["document"]
        files.append({"name": d.get("file_name") or "文件", "size": d.get("file_size") or 0, "id": d["file_id"]})
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or sender.get("username") or "群友"
    private = chat.get("type") == "private"
    role = ""
    return Msg("telegram", aid, "private" if private else "group", str(chat["id"]), str(sender.get("id", "")), name, text.strip(),
               msg_id=str(m["message_id"]), chat_name="" if private else chat.get("title", ""), at_ids=at_ids,
               reply_to_uid=str((reply.get("from") or {}).get("id", "")), reply_to_id=str(reply.get("message_id", "")),
               images=images, files=files, sender_role=role, dedup=f"{chat['id']}|{m['message_id']}")


class TelegramChannel(Channel):
    platform = "telegram"
    overseas = True

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.session: aiohttp.ClientSession | None = None
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.username = ""

    def token(self) -> str:
        return self.engine.db.secret(f"acct:{self.aid}:token")

    def url(self, method: str) -> str:
        return f"{(self.account.get('api_base') or API).rstrip('/')}/bot{self.token()}/{method}"

    async def call(self, method: str, _timeout: float = 30, **params):
        if self.session is None:
            self.session = tls.session()
        for attempt in range(3):
            async with self.session.post(self.url(method), json=params, proxy=self.proxy(),
                                         timeout=aiohttp.ClientTimeout(total=_timeout)) as r:
                data = await r.json(content_type=None)
            if data.get("ok"):
                return data["result"]
            if data.get("error_code") == 429:  # 发太快：按平台说的秒数退避
                await asyncio.sleep(min(float((data.get("parameters") or {}).get("retry_after", 3)), 30) * (self.engine.pace or 0.01))
                continue
            raise RuntimeError(f"{method}：{data.get('description', data)}")
        raise RuntimeError(f"{method}：被限速，稍后再试")

    async def start(self):
        if not self.token():
            self.set_status("error", "还没填机器人令牌（token）")
            return
        self.stopping = False
        self.set_status("starting", "正在连接 Telegram")
        try:
            me = await self.call("getMe")
            await self.call("deleteWebhook")  # 设过 webhook 的话 getUpdates 会冲突
        except Exception as e:
            self.set_status("error", f"连不上 Telegram：{e}（国内需要代理）")
            return
        self.self_id, self.username, self.self_name = str(me["id"]), me.get("username", ""), me.get("first_name", "")
        self.set_status("online", f"已登录 @{self.username}")
        self.task = asyncio.create_task(self._loop())
        b = self.engine.bots.get(self.aid)
        if b:  # 上线时查一遍已知的群：这个号有没有被限制发言
            ids = [d["chat_id"] for d in self.engine.db.docs("chat") if d.get("platform") == "telegram" and str(d.get("chat_id", "")).startswith("-")]
            asyncio.create_task(self.engine.scan_bans(b, ids))

    async def stop(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.session:
            await self.session.close()
            self.session = None

    async def _loop(self):
        offset = 0
        fail = 0
        while not self.stopping:
            try:
                ups = await self.call("getUpdates", 45, offset=offset, timeout=25, allowed_updates=["message", "my_chat_member"])
                fail = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fail += 1
                if fail == 1 or fail % 10 == 0:
                    self.engine.log("warn", "telegram", f"拉消息出错：{e}")
                if "Unauthorized" in str(e):
                    self.set_status("error", "令牌失效了，请到 BotFather 重新取")
                    return
                await asyncio.sleep(min(5 * fail, 60) * (self.engine.pace or 0.01))
                continue
            for u in ups:
                offset = max(offset, u["update_id"] + 1)
                if u.get("my_chat_member"):
                    self._on_my_member(u["my_chat_member"])
                m = u.get("message")
                if m:
                    msg = to_msg(self.aid, m, self.self_id, self.username)
                    if msg:
                        await self.engine.on_message(msg)

    @staticmethod
    def member_ban(m: dict) -> dict:
        """getChatMember / my_chat_member 里的成员信息 → 禁言状态。status 为 restricted 且不能发消息才算；until_date 为 0 = 无限期。"""
        status = m.get("status")
        restricted = status == "restricted" and not m.get("can_send_messages", True)
        until = float(m.get("until_date") or 0)
        return {"member_until": (until or 4102444800.0) if restricted else None, "all": False,
                "role": {"creator": "owner", "administrator": "admin"}.get(status, "member")}

    def _on_my_member(self, ev: dict):
        cid = str((ev.get("chat") or {}).get("id", ""))
        if cid.startswith("-"):
            self.engine.ban_update(self.aid, f"telegram:{cid}", unknown=False, **self.member_ban(ev.get("new_chat_member") or {}))

    async def query_ban(self, chat_id, fresh=False):
        """getChatMember（这个号自己）+ getChat（群默认权限，全员禁言时不能发消息）。不往群里发任何东西。"""
        if not str(chat_id).startswith("-"):
            return None
        res = self.member_ban(await self.call("getChatMember", chat_id=int(chat_id), user_id=int(self.self_id)))
        chat = await self.call("getChat", chat_id=int(chat_id))
        res["all"] = (chat.get("permissions") or {}).get("can_send_messages", True) is False
        return res

    async def _confirm_muted(self, chat_id, ex: Exception):
        if not any(k in str(ex) for k in NO_RIGHTS) or not str(chat_id).startswith("-"):
            return
        try:
            res = await self.query_ban(chat_id)
        except Exception:
            return
        if res and self.engine.ban_active(res, time.time()):
            self.engine.ban_update(self.aid, f"telegram:{chat_id}", unknown=False, **res)
            raise Muted("这个号在群里被限制发言了") from ex

    async def send(self, chat_type, chat_id, text, reply_to=None):
        try:
            return await self._send(chat_type, chat_id, text, reply_to)
        except RuntimeError as ex:
            if not isinstance(ex, Muted) and chat_type == "group":
                await self._confirm_muted(chat_id, ex)
            raise

    async def _send(self, chat_type, chat_id, text, reply_to=None):
        out = []
        for i, part in enumerate(split_text(text)):
            params = {"chat_id": int(chat_id), "text": part}
            if reply_to and i == 0:
                params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
            r = await self.call("sendMessage", **params)
            out.append(str(r["message_id"]))
        return out

    supports_image = True

    async def send_image(self, chat_type, chat_id, data, reply_to=None):
        from ..stickers import ext_of
        if self.session is None:
            self.session = tls.session()
        form = aiohttp.FormData()
        form.add_field("chat_id", str(int(chat_id)))
        if reply_to:
            form.add_field("reply_parameters", json.dumps({"message_id": int(reply_to), "allow_sending_without_reply": True}))
        form.add_field("photo", data, filename="sticker." + ext_of(data))
        async with self.session.post(self.url("sendPhoto"), data=form, proxy=self.proxy(), timeout=aiohttp.ClientTimeout(total=60)) as r:
            res = await r.json(content_type=None)
        if not res.get("ok"):
            ex = RuntimeError(f"sendPhoto：{res.get('description', res)}")
            if chat_type == "group":
                await self._confirm_muted(chat_id, ex)
            raise ex
        return [str(res["result"]["message_id"])]

    async def recall(self, chat_type, chat_id, msg_id):
        await self.call("deleteMessage", chat_id=int(chat_id), message_id=int(msg_id))
        return True

    async def _file_bytes(self, file_id: str, max_bytes: int) -> bytes | None:
        info = await self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            return None
        base = (self.account.get("api_base") or API).rstrip("/")
        async with self.session.get(f"{base}/file/bot{self.token()}/{info['file_path']}", proxy=self.proxy(),
                                    timeout=aiohttp.ClientTimeout(total=120)) as r:
            return await r.read() if r.status == 200 else None

    async def fetch_image(self, url):
        return await self._file_bytes(url[3:], 20 << 20) if url.startswith("tg:") else None

    async def fetch_file(self, f, max_bytes):
        return await self._file_bytes(f["id"], max_bytes) if f.get("id") else None

    async def set_profile(self, nickname="", signature="", avatar=None):
        if nickname:
            await self.call("setMyName", name=nickname)
        if signature:
            await self.call("setMyDescription", description=signature)
        if avatar:
            raise NotImplementedError("Telegram 机器人的头像要在 BotFather 里改（/setuserpic）")
