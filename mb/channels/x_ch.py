"""X（Twitter）：官方 API v2，按量计费（没有免费档，要在 console.x.com 先买积分）。

只做一件事：轮询「@我的帖子」，被 @ 了就回复。自 2026-02-23 起 API 只允许回复「@ 了你或引用了你」的帖子，
所以这个形态正好合规；主动去回别人没 @ 你的帖子会被拒。鉴权用 OAuth 1.0a 用户令牌（API Key/Secret + Access Token/Secret，
都在开发者控制台里复制），签名自己算，不引入依赖。国内需要代理。
"""
import base64
import hashlib
import hmac
import re
import time
import uuid
from urllib.parse import parse_qsl, quote, urlsplit

from .base import Msg
from .social import AuthError, PollChannel, RiskError

API = "https://api.x.com"
MAX_TWEET = 280
PRICE = {"read": 0.005, "write": 0.015}  # 美元，官方按量价目（2026-10-24 核对）；仅用来估算


def oauth1_signature(method: str, url: str, params: dict, oauth: dict, consumer_secret: str, token_secret: str) -> str:
    """OAuth 1.0a HMAC-SHA1 签名。params：查询串和表单里的参数（JSON 请求体不参与签名）。"""
    enc = lambda s: quote(str(s), safe="~")
    allp = sorted((enc(k), enc(v)) for k, v in {**params, **oauth}.items())
    base_url = url.split("?")[0]
    base = "&".join([method.upper(), enc(base_url), enc("&".join(f"{k}={v}" for k, v in allp))])
    key = f"{enc(consumer_secret)}&{enc(token_secret)}"
    return base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()


def oauth1_header(method: str, url: str, params: dict, ck: str, cs: str, tk: str, ts: str, nonce=None, stamp=None) -> str:
    oauth = {"oauth_consumer_key": ck, "oauth_nonce": nonce or uuid.uuid4().hex, "oauth_signature_method": "HMAC-SHA1",
             "oauth_timestamp": str(stamp or int(time.time())), "oauth_token": tk, "oauth_version": "1.0"}
    oauth["oauth_signature"] = oauth1_signature(method, url, params, {k: v for k, v in oauth.items()}, cs, ts)
    return "OAuth " + ", ".join(f'{quote(k, safe="~")}="{quote(v, safe="~")}"' for k, v in sorted(oauth.items()))


def split_tweet(text: str) -> list[str]:
    out = []
    while len(text) > MAX_TWEET:
        cut = max(text.rfind(c, 0, MAX_TWEET - 1) for c in "。！？.!?\n ")
        cut = cut + 1 if cut > MAX_TWEET // 2 else MAX_TWEET
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return out + [text]


class XChannel(PollChannel):
    platform = "x"
    overseas = True
    risky = False

    def sign_headers(self, method: str, url: str, params: dict | None = None) -> dict:
        h = oauth1_header(method, url, {**dict(parse_qsl(urlsplit(url).query)), **(params or {})}, self.secret("api_key"),
                          self.secret("api_secret"), self.secret("access_token"), self.secret("access_secret"))
        return {"Authorization": h}

    async def api(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base(API) + path
        if params:
            url += "?" + "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in params.items())
        async with self.http().request(method, url, json=body, headers=self.sign_headers(method, url), proxy=self.proxy()) as r:
            text = await r.text()
            if r.status == 401:
                raise AuthError("X 的密钥或令牌不对（401），请重新复制")
            if r.status == 402:
                raise AuthError("X 的积分用完了（402），到 console.x.com 充值")
            if r.status == 429:
                raise RiskError("X 限速（429），稍后再来")
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status} {text[:200]}")
            import json
            return json.loads(text)

    def usage(self) -> dict:
        return self.engine.db.kv(f"x_usage:{self.aid}:{time.strftime('%Y-%m-%d')}", {"reads": 0, "writes": 0})

    def count(self, kind: str, n: int = 1):
        u = self.usage()
        u[kind] += n
        self.engine.db.set_kv(f"x_usage:{self.aid}:{time.strftime('%Y-%m-%d')}", u)

    async def login(self):
        if not all(self.secret(k) for k in ("api_key", "api_secret", "access_token", "access_secret")):
            raise AuthError("还没填完 4 个密钥（API Key / API Secret / Access Token / Access Secret）")
        me = (await self.api("GET", "/2/users/me"))["data"]
        self.self_id, self.self_name = me["id"], me.get("username", "")

    async def poll(self) -> list[Msg]:
        since = self.engine.db.kv(f"x_since:{self.aid}")
        params = {"max_results": 20, "expansions": "author_id", "tweet.fields": "conversation_id,created_at",
                  "user.fields": "username,name"}
        if since:
            params["since_id"] = since
        d = await self.api("GET", f"/2/users/{self.self_id}/mentions", params)
        items = d.get("data") or []
        self.count("reads", len(items))
        newest = (d.get("meta") or {}).get("newest_id")
        self.engine.db.set_kv(f"x_since:{self.aid}", newest or since or "0")
        if since is None:  # 第一次：只记下位置，不回启动之前的老帖子
            return []
        users = {u["id"]: u for u in (d.get("includes") or {}).get("users", [])}
        out = []
        for t in sorted(items, key=lambda x: int(x["id"])):
            if t["author_id"] == self.self_id:
                continue
            u = users.get(t["author_id"], {})
            text = re.sub(r"^(@\w+\s+)+", "", t["text"]).strip() or t["text"]
            out.append(Msg("x", self.aid, "group", t.get("conversation_id") or t["id"], t["author_id"],
                           u.get("name") or u.get("username") or "用户", text, msg_id=t["id"],
                           chat_name=f"@{u.get('username', '')} 的对话", at_ids=[self.self_id],
                           dedup=f"{t.get('conversation_id') or t['id']}|{t['id']}"))
        return out

    async def do_send(self, kind, chat_type, chat_id, text, reply_to):
        if not reply_to:
            raise RuntimeError("X 只能回复 @ 了机器人的帖子")
        ids, parent = [], reply_to
        for part in split_tweet(text):  # 超过 280 字的拆成一串回复
            r = await self.api("POST", "/2/tweets", body={"text": part, "reply": {"in_reply_to_tweet_id": parent}})
            parent = r["data"]["id"]
            ids.append(parent)
            self.count("writes")
        return ids

    def cost_estimate(self) -> str:
        u = self.usage()
        return f"今日约 ${u['reads'] * PRICE['read'] + u['writes'] * PRICE['write']:.3f}（读 {u['reads']} 条、发 {u['writes']} 条，估算）"
