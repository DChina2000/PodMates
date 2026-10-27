"""微博（非官方渠道）：用手机网页版（m.weibo.cn）的登录 cookie 收「@我的评论」「评论我的」和私信，回评论、回私信。

风险（面板上会标明，默认关闭，要勾「我知道有风险」才启动）：
  · 微博官方开放平台对个人开发者门槛高、机器人式调用会被限制，所以走的是网页版接口，不是官方开放接口；
  · 网页版有风控：访问太频繁会要验证码或要求重新登录，评论可能被折叠；请用小号、低频；
  · 返回「请求过于频繁」「验证码」一类内容立即整体停止并发掉线通知，不原地重试。
  · 接口细节依据公开资料整理（没有可直接借用的许可证兼容的现成库，全部自己用 aiohttp 实现），没有在真账号上验证过，请先用「影子模式」演练。
"""
import json
import re

from .base import Msg
from .social import AuthError, PollChannel, RiskError

BASE = "https://m.weibo.cn"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
RISK_TEXT = ("频繁", "验证码", "验证", "稍后再试", "限制")


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


class WeiboChannel(PollChannel):
    platform = "weibo"
    risky = True

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.st = ""

    def headers(self, write: bool = False) -> dict:
        h = {"User-Agent": UA, "Referer": "https://m.weibo.cn/", "Cookie": self.secret("cookie")}
        if write:
            h["X-XSRF-TOKEN"] = self.st
            h["X-Requested-With"] = "XMLHttpRequest"
        return h

    async def call(self, method: str, path: str, params=None, data=None, write=False) -> dict:
        base = (self.account.get("api_base") or BASE).rstrip("/")
        async with self.http().request(method, base + path, params=params, data=data, headers=self.headers(write), proxy=self.proxy()) as r:
            if r.status in (403, 418, 429):
                raise RiskError(f"HTTP {r.status}")
            text = await r.text()
        try:
            j = json.loads(text)
        except ValueError:
            raise RiskError("返回的不是 JSON（多半是验证码页面）")
        if j.get("ok") == 0 and any(t in str(j.get("msg", "")) for t in RISK_TEXT):
            raise RiskError(str(j.get("msg"))[:60])
        return j

    async def login(self):
        if not self.secret("cookie"):
            raise AuthError("还没填 cookie（浏览器登录 m.weibo.cn 后复制）")
        j = await self.call("GET", "/api/config")
        d = j.get("data") or {}
        if not d.get("login"):
            raise AuthError("cookie 失效了，请重新复制")
        self.st, self.self_id = d.get("st", ""), str(d.get("uid", ""))
        self.self_name = self.account.get("name", "") or self.self_id

    async def poll(self) -> list[Msg]:
        out = []
        for path in ("/message/mentionsCmt", "/message/cmt"):
            j = await self.call("GET", path, {"page": 1})
            for c in j.get("data") or []:
                user, status = c.get("user") or {}, c.get("status") or {}
                uid = str(user.get("id", ""))
                if not uid or uid == self.self_id or not c.get("id"):
                    continue
                text = strip_html(c.get("text", ""))
                text = re.sub(r"^回复@[^:：]+[:：]", "", text)
                text = re.sub(r"@\S+\s*", "", text).strip() or text
                sid, mid = str(status.get("id", "")), str(status.get("mid") or status.get("id", ""))
                out.append(Msg("weibo", self.aid, "group", mid, uid, user.get("screen_name", "微博用户"), text,
                               msg_id=f"{sid}|{mid}|{c['id']}", chat_name=f"微博 {strip_html(status.get('text', ''))[:20]}",
                               at_ids=[self.self_id], reply_to_uid=self.self_id if path.endswith("cmt") and "mentions" not in path else "",
                               dedup=f"{mid}|{c['id']}"))
        if self.account.get("enable_dm"):
            out += await self.poll_dm()
        return out

    async def poll_dm(self) -> list[Msg]:
        j = await self.call("GET", "/message/msglist", {"page": 1})
        out = []
        for s in (j.get("data") or [])[:10]:
            user = s.get("user") or {}
            uid = str(user.get("id", ""))
            if not uid or uid == self.self_id or str(s.get("sender_id", uid)) == self.self_id:
                continue
            text = strip_html(s.get("text", ""))
            if text:
                key = s.get("id") or s.get("created_at")
                out.append(Msg("weibo", self.aid, "private", uid, uid, user.get("screen_name", "微博用户"), text,
                               msg_id=str(key), dedup=f"dm|{uid}|{key}"))
        return out

    async def do_send(self, kind, chat_type, chat_id, text, reply_to):
        if chat_type == "private":
            j = await self.call("POST", "/api/chat/send", data={"uid": chat_id, "content": text, "st": self.st}, write=True)
        else:
            sid, mid, cid = (reply_to or "||").split("|")
            j = await self.call("POST", "/api/comments/reply", data={"id": sid, "mid": mid, "cid": cid, "reply": cid,
                                                                    "content": text, "withReply": 0, "st": self.st}, write=True)
        if j.get("ok") != 1:
            raise RuntimeError(f"发送失败：{str(j.get('msg', j))[:80]}")
        d = j.get("data") or {}
        return [str(d.get("id") or d.get("idstr") or "")]
