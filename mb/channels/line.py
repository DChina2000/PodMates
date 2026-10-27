"""LINE：官方 Messaging API。

LINE 只有 webhook（它主动 POST 到你给的 HTTPS 地址），没有轮询。家里的电脑没有公网地址，要靠隧道（ngrok 免费固定域名，
或 cloudflared 快速隧道）把本机的一个端口露出去；程序在本机开一个只收 LINE 请求的小服务，面板里有「把地址登记给 LINE」的按钮。
回复（Reply）免费，主动推送（Push）算每月额度，所以优先用 Reply；Reply 令牌一次性、1 分钟内有效。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time

import aiohttp
from aiohttp import web

from .base import Channel, Msg
from .. import tls

API = "https://api.line.me"
DATA_API = "https://api-data.line.me"
MAX_UNITS = 4800  # LINE 文本上限 5000，按 UTF-16 码元算


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def split_text(text: str, limit: int = MAX_UNITS) -> list[str]:
    out, cur, units = [], [], 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > limit:
            out.append("".join(cur))
            cur, units = [], 0
        cur.append(ch)
        units += w
    return out + ["".join(cur)]


def sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class LineChannel(Channel):
    platform = "line"
    overseas = True
    supports_recall = False

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.session: aiohttp.ClientSession | None = None
        self.runner: web.AppRunner | None = None
        self.tokens: dict[str, tuple[str, float]] = {}  # 消息 id → (replyToken, 收到时间)
        self.sent_ids: set[str] = set()
        self.names: dict[str, str] = {}

    # ---------- 基本 ----------
    def secret(self, name: str) -> str:
        return self.engine.db.secret(f"acct:{self.aid}:{name}")

    def api(self, path: str, data: bool = False) -> str:
        base = (self.account.get("api_base") or (DATA_API if data else API)).rstrip("/")
        return base + path

    @property
    def port(self) -> int:
        return int(self.account.get("webhook_port") or 8790)

    def webhook_path(self) -> str:
        return f"/line/{self.aid}"

    def webhook_url(self) -> str:
        pub = (self.account.get("public_url") or "").rstrip("/")
        return f"{pub}{self.webhook_path()}" if pub else ""

    async def call(self, method: str, path: str, body=None, data_api: bool = False, raw: bool = False):
        if self.session is None:
            self.session = tls.session(timeout=aiohttp.ClientTimeout(total=30))
        headers = {"Authorization": f"Bearer {self.secret('channel_token')}"}
        async with self.session.request(method, self.api(path, data_api), json=body, headers=headers, proxy=self.proxy()) as r:
            if raw:
                return await r.read() if r.status == 200 else None
            text = await r.text()
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status} {text[:200]}")
            return json.loads(text) if text else {}

    async def start(self):
        if not (self.secret("channel_secret") and self.secret("channel_token")):
            self.set_status("error", "还没填 Channel secret 和 Channel access token")
            return
        self.set_status("starting", "正在连接 LINE")
        try:
            info = await self.call("GET", "/v2/bot/info")
        except Exception as e:
            self.set_status("error", f"连不上 LINE：{e}")
            return
        self.self_id, self.self_name = info.get("userId", ""), info.get("displayName", "")
        app = web.Application()
        app.router.add_post(self.webhook_path(), self._hook)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        except OSError as e:
            self.set_status("error", f"本机端口 {self.port} 被占用：{e}")
            return
        hint = self.webhook_url() or "（还没填公网地址，LINE 收不到）"
        self.set_status("online", f"已登录 {self.self_name}；webhook {hint}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.session:
            await self.session.close()
            self.session = None

    async def register_webhook(self) -> dict:
        """把公网地址登记给 LINE，并让 LINE 测一次。"""
        url = self.webhook_url()
        if not url.startswith("https://"):
            raise ValueError("公网地址必须是 https:// 开头（隧道给的地址）")
        await self.call("PUT", "/v2/bot/channel/webhook/endpoint", {"endpoint": url})
        res = await self.call("POST", "/v2/bot/channel/webhook/test", {"endpoint": url})
        return {"endpoint": url, "success": bool(res.get("success", True)), "detail": res.get("detail", "")}

    # ---------- 收 ----------
    async def _hook(self, req: web.Request):
        body = await req.read()
        if not hmac.compare_digest(req.headers.get("X-Line-Signature", ""), sign(self.secret("channel_secret"), body)):
            return web.Response(status=400, text="bad signature")
        try:
            events = json.loads(body).get("events", [])
        except ValueError:
            return web.Response(status=400)
        for ev in events:
            asyncio.create_task(self._event(ev))
        return web.Response(text="ok")

    async def _name(self, src: dict) -> str:
        uid, gid = src.get("userId", ""), src.get("groupId") or src.get("roomId") or ""
        key = f"{gid}|{uid}"
        if key not in self.names:
            try:
                path = (f"/v2/bot/group/{gid}/member/{uid}" if src.get("groupId") else f"/v2/bot/room/{gid}/member/{uid}") if gid else f"/v2/bot/profile/{uid}"
                self.names[key] = (await self.call("GET", path)).get("displayName") or "群友"
            except Exception:
                self.names[key] = "群友"
        return self.names[key]

    async def _event(self, ev: dict):
        try:
            if ev.get("type") == "join":
                src = ev.get("source", {})
                gid = src.get("groupId") or src.get("roomId")
                if gid:
                    name = ""
                    try:
                        name = (await self.call("GET", f"/v2/bot/group/{gid}/summary")).get("groupName", "")
                    except Exception:
                        pass
                    self.engine.register_chat(self.aid, "line", gid, name)
                return
            if ev.get("type") != "message":
                return
            m, src = ev["message"], ev.get("source", {})
            gid = src.get("groupId") or src.get("roomId")
            private = not gid
            mid = str(m.get("id", ""))
            if ev.get("replyToken"):
                self.tokens[mid] = (ev["replyToken"], time.time())
                if len(self.tokens) > 500:
                    self.tokens.pop(next(iter(self.tokens)))
            at_ids = [self.self_id for t in (m.get("mention") or {}).get("mentionees", []) if t.get("isSelf")]
            text = m.get("text", "") if m.get("type") == "text" else ""
            files = [{"name": m.get("fileName", "文件"), "size": m.get("fileSize", 0), "id": mid}] if m.get("type") == "file" else []
            images = [f"line:{mid}"] if m.get("type") == "image" else []
            quoted = str(m.get("quotedMessageId", ""))
            await self.engine.on_message(Msg(
                "line", self.aid, "private" if private else "group", gid or src.get("userId", ""), src.get("userId", ""),
                await self._name(src), text.strip(), msg_id=mid, at_ids=at_ids,
                reply_to_uid=self.self_id if quoted and quoted in self.sent_ids else "", reply_to_id=quoted,
                images=images, files=files, dedup=f"{gid or src.get('userId')}|{mid}"))
        except Exception as e:
            self.engine.log("warn", "line", f"处理事件出错：{type(e).__name__}: {e}")

    # ---------- 发 ----------
    async def send(self, chat_type, chat_id, text, reply_to=None):
        out = []
        parts = split_text(text)
        for i, part in enumerate(parts):
            msgs = [{"type": "text", "text": part}]
            tok = self.tokens.pop(reply_to, None) if (reply_to and i == 0) else None
            if tok and time.time() - tok[1] < 50:
                r = await self.call("POST", "/v2/bot/message/reply", {"replyToken": tok[0], "messages": msgs})
            else:  # 令牌过期或没有：只能推送（计入每月额度）
                r = await self.call("POST", "/v2/bot/message/push", {"to": chat_id, "messages": msgs})
            for sm in r.get("sentMessages", []):
                out.append(str(sm["id"]))
                self.sent_ids.add(str(sm["id"]))
        return out

    async def _content(self, mid: str, max_bytes: int):
        data = await self.call("GET", f"/v2/bot/message/{mid}/content", data_api=True, raw=True)
        return data if data is not None and len(data) <= max_bytes else None

    async def fetch_image(self, url):
        return await self._content(url[5:], 20 << 20) if url.startswith("line:") else None

    async def fetch_file(self, f, max_bytes):
        return await self._content(f["id"], max_bytes)
