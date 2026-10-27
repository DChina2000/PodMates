"""Reddit：官方 Data API（OAuth2 脚本应用，用户名 + 密码取令牌）。实验性。

按「帖子 + 评论串」互动：收件箱里别人 @ 她（u/用户名）、回复她的评论、给她发的私信，她才回；也可以填几个帖子 ID 让她读着串里的新评论（只当上下文）。
注意：Reddit 在 2026-09-30 宣布 2026-10-31 起不再接受新的公共 API 申请、2027 年 3 月关闭剩余公共 API（二手转述，见使用说明）。
所以这个适配器只对「已有已批准凭据」的用户有用，且最晚用到 2027-03。
"""
import re
import time

from .base import Msg
from .social import AuthError, PollChannel, RiskError

API = "https://oauth.reddit.com"
AUTH = "https://www.reddit.com"


class RedditChannel(PollChannel):
    platform = "reddit"
    overseas = True
    risky = False

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.token = ""
        self.token_exp = 0.0

    def ua(self) -> str:
        from .. import VERSION
        return f"windows:podmates:{VERSION} (by /u/{self.account.get('username', '')})"

    async def ensure_token(self):
        if self.token and time.time() < self.token_exp - 60:
            return
        import aiohttp
        basic = aiohttp.helpers.BasicAuth(self.account.get("client_id", ""), self.secret("client_secret")).encode()
        data = {"grant_type": "password", "username": self.account.get("username", ""), "password": self.secret("password")}
        async with self.http().post(self.account.get("auth_base", AUTH).rstrip("/") + "/api/v1/access_token", data=data,
                                    headers={"User-Agent": self.ua(), "Authorization": basic}, proxy=self.proxy()) as r:
            j = await r.json(content_type=None)
        if r.status != 200 or "access_token" not in j:
            raise AuthError(f"Reddit 登录失败：{j.get('error', r.status)}（用户名、密码、client id/secret 都要对；开了两步验证的号不能用密码方式）")
        self.token, self.token_exp = j["access_token"], time.time() + int(j.get("expires_in", 3600))

    async def api(self, method: str, path: str, params=None, data=None):
        await self.ensure_token()
        h = {"Authorization": f"bearer {self.token}", "User-Agent": self.ua()}
        async with self.http().request(method, self.base(API) + path, params=params, data=data, headers=h, proxy=self.proxy()) as r:
            remaining = r.headers.get("X-Ratelimit-Remaining")
            if r.status == 429 or (remaining and float(remaining) < 2):
                raise RiskError("Reddit 限速了，等一会儿")
            if r.status == 401:
                self.token = ""
                raise AuthError("Reddit 令牌失效")
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status} {(await r.text())[:200]}")
            return await r.json(content_type=None)

    async def login(self):
        if not (self.account.get("client_id") and self.account.get("username") and self.secret("client_secret") and self.secret("password")):
            raise AuthError("还没填完 client id / client secret / 用户名 / 密码")
        me = await self.api("GET", "/api/v1/me")
        self.self_id, self.self_name = me["name"], me["name"]

    @staticmethod
    def clean(body: str, me: str) -> str:
        return re.sub(rf"/?u/{re.escape(me)}\b", "", body, flags=re.I).strip() or body

    async def poll(self) -> list[Msg]:
        out, names = [], []
        d = await self.api("GET", "/message/unread", {"limit": 25})
        for ch in (d.get("data") or {}).get("children", []):
            x = ch["data"]
            names.append(x["name"])
            if x.get("author") in (None, self.self_name, "[deleted]"):
                continue
            if ch["kind"] == "t4":  # 私信
                out.append(Msg("reddit", self.aid, "private", x["author"], x["author"], x["author"], x.get("body", ""),
                               msg_id=x["name"], dedup=f"dm|{x['name']}"))
            elif ch["kind"] == "t1":  # 评论回复 / @提及
                link = x.get("link_id") or x.get("parent_id", "")
                out.append(Msg("reddit", self.aid, "group", link, x["author"], x["author"], self.clean(x.get("body", ""), self.self_name),
                               msg_id=x["name"], chat_name=f"r/{x.get('subreddit', '')}：{x.get('link_title', '')}"[:60],
                               at_ids=[self.self_id], reply_to_uid=self.self_id if x.get("subject") == "comment reply" else "",
                               dedup=f"{link}|{x['name']}"))
        if names:
            await self.api("POST", "/api/read_message", data={"id": ",".join(names)})
        return out

    async def do_send(self, kind, chat_type, chat_id, text, reply_to):
        if chat_type == "private":
            r = await self.api("POST", "/api/compose", data={"api_type": "json", "to": chat_id, "subject": "Re:", "text": text})
        else:
            r = await self.api("POST", "/api/comment", data={"api_type": "json", "thing_id": reply_to, "text": text})
        errs = (r.get("json") or {}).get("errors") or []
        if errs:
            code = str(errs[0][0])
            if code in ("RATELIMIT", "USER_REQUIRED"):
                raise RiskError(f"Reddit：{errs[0]}")
            raise RuntimeError(f"Reddit：{errs[0]}")
        things = ((r.get("json") or {}).get("data") or {}).get("things") or []
        return [things[0]["data"]["name"]] if things else [""]
