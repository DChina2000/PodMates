"""B站（非官方渠道）：用网页登录态（cookie）收「@我的」「回复我的」评论和私信，回评论、回私信。

风险（面板上会标明，默认关闭，要勾「我知道有风险」才启动）：
  · 这是 B站没有公开的网页接口，不是官方开放接口；B站的用户协议不允许自动化操作，账号有被限制、封禁的风险。请用小号。
  · 遇到风控返回（HTTP 412/429，业务码 -412、-352、-509、-799）立即整体停止并发掉线通知，不原地重试；等 6～24 小时、
    在浏览器里正常登录一下再来。
做法：串行处理、动作前随机等待、成功后才记「已回复」、遇风控立即停机，评论 / 回复 / 私信分开设额度。直播弹幕没做：官方「直播开放平台」只能收自己直播间的弹幕且没有发弹幕接口，非官方直连有风控风险。
"""
import json
import re
import time
import uuid

from .base import Msg
from .social import AuthError, PollChannel, RiskError

API = "https://api.bilibili.com"
VC = "https://api.vc.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
RISK_CODES = {-412, -352, -509, -799}
FATAL_CODES = {-101, -102}


def strip_at(text: str) -> str:
    t = re.sub(r"^\s*回复\s*@[^:：\s]+\s*[:：]\s*", "", text)
    return re.sub(r"@\S+\s*", "", t).strip() or text


class BilibiliChannel(PollChannel):
    platform = "bilibili"
    risky = True

    def headers(self) -> dict:
        ck = f"SESSDATA={self.secret('SESSDATA')}; bili_jct={self.secret('bili_jct')}"
        if self.secret("buvid3"):
            ck += f"; buvid3={self.secret('buvid3')}"
        return {"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Cookie": ck}

    def host(self, which: str) -> str:
        b = self.account.get("api_base")
        return b.rstrip("/") if b else {"api": API, "vc": VC}[which]

    async def call(self, method: str, which: str, path: str, params=None, data=None) -> dict:
        async with self.http().request(method, self.host(which) + path, params=params, data=data, headers=self.headers(),
                                       proxy=self.proxy()) as r:
            if r.status in (412, 429):
                raise RiskError(f"HTTP {r.status}")
            text = await r.text()
        try:
            j = json.loads(text)
        except ValueError:
            raise RuntimeError(f"返回的不是 JSON：{text[:100]}")
        code = j.get("code", 0)
        if code in RISK_CODES:
            raise RiskError(f"返回码 {code}：{j.get('message', '')}")
        if code in FATAL_CODES:
            raise AuthError(f"登录态失效（{code}）：请重新扫码登录")
        return j

    async def login(self):
        if not (self.secret("SESSDATA") and self.secret("bili_jct")):
            raise AuthError("还没登录：在账号页点「扫码登录」，或粘贴 SESSDATA 和 bili_jct")
        j = await self.call("GET", "api", "/x/web-interface/nav")
        d = j.get("data") or {}
        if not d.get("isLogin"):
            raise AuthError("登录态失效，请重新扫码登录")
        self.self_id, self.self_name = str(d["mid"]), d.get("uname", "")

    # ---------- 收 ----------
    async def poll(self) -> list[Msg]:
        out = []
        for feed, replying in (("at", False), ("reply", True)):
            j = await self.call("GET", "api", f"/x/msgfeed/{feed}", {"build": 0, "mobi_app": "web", "platform": "web"})
            for it in ((j.get("data") or {}).get("items") or []):
                m = self.to_msg(it, replying)
                if m:
                    out.append(m)
        if self.account.get("enable_dm"):
            out += await self.poll_dm()
        return out

    def to_msg(self, it: dict, replying: bool) -> Msg | None:
        item, user = it.get("item") or {}, it.get("user") or {}
        if item.get("business_id") not in (1, "1") and item.get("business") not in ("视频", "video"):
            return None  # 动态、专栏暂不处理
        oid, rpid = item.get("subject_id"), item.get("source_id")
        if not (oid and rpid) or str(user.get("mid")) == self.self_id:
            return None
        root = item.get("root_id") or 0
        # 对方在楼中楼里回我：root=楼根，parent=对方那条；对方在视频下直接 @ 我：root=对方那条，parent 为空
        root_id, parent = (root, rpid) if root else (rpid, 0)
        return Msg("bilibili", self.aid, "group", f"av{oid}", str(user.get("mid", "")), user.get("nickname", "用户"),
                   strip_at(item.get("source_content", "")), msg_id=f"{oid}|{item.get('type', 1)}|{root_id}|{parent}",
                   chat_name=(item.get("title") or f"视频 av{oid}")[:40], at_ids=[self.self_id],
                   reply_to_uid=self.self_id if replying else "", dedup=f"av{oid}|{rpid}")

    async def poll_dm(self) -> list[Msg]:
        j = await self.call("GET", "vc", "/session_svr/v1/session_svr/get_sessions", {"session_type": 1, "group_fold": 1, "unfollow_fold": 0,
                                                                                      "sort_rule": 2, "build": 0, "mobi_app": "web"})
        out = []
        for s in ((j.get("data") or {}).get("session_list") or [])[:10]:
            last = s.get("last_msg") or {}
            if str(last.get("sender_uid")) == self.self_id or last.get("msg_type") != 1 or not s.get("talker_id"):
                continue
            try:
                text = json.loads(last.get("content", "{}")).get("content", "")
            except ValueError:
                text = ""
            if text.strip():
                key = last.get("msg_key") or last.get("timestamp")
                out.append(Msg("bilibili", self.aid, "private", str(s["talker_id"]), str(s["talker_id"]), f"用户{s['talker_id']}",
                               text, msg_id=str(key), dedup=f"dm|{s['talker_id']}|{key}"))
        return out

    # ---------- 发 ----------
    async def do_send(self, kind, chat_type, chat_id, text, reply_to):
        csrf = self.secret("bili_jct")
        if chat_type == "private":
            data = {"msg[sender_uid]": self.self_id, "msg[receiver_id]": chat_id, "msg[receiver_type]": 1, "msg[msg_type]": 1,
                    "msg[dev_id]": str(uuid.uuid4()).upper(), "msg[timestamp]": int(time.time()),
                    "msg[content]": json.dumps({"content": text}, ensure_ascii=False), "csrf": csrf, "csrf_token": csrf,
                    "build": 0, "mobi_app": "web"}
            j = await self.call("POST", "vc", "/web_im/v1/web_im/send_msg", data=data)
            if j.get("code") != 0:
                raise RuntimeError(f"发私信失败 {j.get('code')} {j.get('message')}")
            return [str((j.get("data") or {}).get("msg_key", ""))]
        oid, typ, root, parent = (reply_to or "").split("|")
        data = {"oid": oid, "type": typ, "message": text, "plat": 1, "csrf": csrf, "gaia_source": "main_web"}
        if root and root != "0":
            data["root"] = root
        if parent and parent != "0":
            data["parent"] = parent
        j = await self.call("POST", "api", "/x/v2/reply/add", data=data)
        if j.get("code") != 0:
            raise RuntimeError(f"发评论失败 {j.get('code')} {j.get('message')}")
        return [str(((j.get("data") or {}).get("rpid")) or "")]


# ---------- 扫码登录（面板的「扫码登录」按钮调用）----------
async def qr_generate(session, base: str = PASSPORT) -> dict:
    async with session.get(f"{base}/x/passport-login/web/qrcode/generate", headers={"User-Agent": UA}) as r:
        j = await r.json(content_type=None)
    return {"url": j["data"]["url"], "key": j["data"]["qrcode_key"]}


async def qr_poll(session, key: str, base: str = PASSPORT) -> dict:
    """返回 {"state": waiting/scanned/expired/ok, "cookies": {...}}。"""
    async with session.get(f"{base}/x/passport-login/web/qrcode/poll", params={"qrcode_key": key}, headers={"User-Agent": UA}) as r:
        j = await r.json(content_type=None)
        d = j.get("data") or {}
        code = d.get("code")
        if code == 0:
            ck = {k: v.value for k, v in r.cookies.items()}
            for part in re.split(r"[?&]", d.get("url", "")):  # 兼容把 cookie 放在返回地址里的写法
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in ("SESSDATA", "bili_jct", "DedeUserID"):
                        ck.setdefault(k, v)
            return {"state": "ok", "cookies": ck}
    return {"state": {86101: "waiting", 86090: "scanned", 86038: "expired"}.get(code, "waiting")}
