"""YouTube：官方 Data API v3。两种用法（账号上选一种）：
  · 评论区：盯着几个视频，有人 @ 频道或回复频道的评论，就在那条评论底下回复；
  · 直播聊天：盯着一场直播的聊天，被 @ 才回。
发评论必须 OAuth 授权（桌面应用 + 本机回环地址，面板里有「授权」按钮）；每天默认只有 10000 点配额：
读评论 1 点、回一条评论 50 点（约 200 条/天）、读直播聊天 5 点、发一条直播聊天 20 点。程序自己记账，到上限就停。
streamList（gRPC）要大依赖，不用。国内需要代理。
"""
import re
import time
from datetime import datetime

from .base import Msg
from .social import AuthError, PollChannel, RiskError

API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
COST = {"list": 1, "comment_insert": 50, "live_list": 5, "live_insert": 20}


class YouTubeChannel(PollChannel):
    platform = "youtube"
    overseas = True
    risky = False

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.token = ""
        self.token_exp = 0.0
        self.handle = ""
        self.live_chat_id = ""
        self.page_token = ""
        self.next_live = 0.0

    # ---------- 配额记账 ----------
    def used_key(self) -> str:
        return f"yt_quota:{self.aid}:{time.strftime('%Y-%m-%d')}"

    def used(self) -> int:
        return int(self.engine.db.kv(self.used_key(), 0))

    def charge(self, what: str, n: int = 1):
        self.engine.db.set_kv(self.used_key(), self.used() + COST[what] * n)

    def budget_left(self, need: int) -> bool:
        return self.used() + need <= int(self.account.get("quota_cap") or 9000)

    # ---------- 令牌与请求 ----------
    async def ensure_token(self):
        if self.token and time.time() < self.token_exp - 60:
            return
        data = {"client_id": self.account.get("client_id", ""), "client_secret": self.secret("client_secret"),
                "refresh_token": self.secret("refresh_token"), "grant_type": "refresh_token"}
        async with self.http().post(self.account.get("token_url", TOKEN_URL), data=data, proxy=self.proxy()) as r:
            j = await r.json(content_type=None)
        if "access_token" not in j:
            raise AuthError(f"YouTube 授权失效：{j.get('error', r.status)}。测试状态下的授权 7 天就过期，到面板里重新授权")
        self.token, self.token_exp = j["access_token"], time.time() + int(j.get("expires_in", 3600))

    async def api(self, method: str, path: str, params=None, body=None):
        await self.ensure_token()
        async with self.http().request(method, self.base(API) + path, params=params, json=body,
                                       headers={"Authorization": f"Bearer {self.token}"}, proxy=self.proxy()) as r:
            j = await r.json(content_type=None)
            if r.status == 403 and any(e.get("reason") in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded")
                                       for e in (j.get("error") or {}).get("errors", [])):
                raise RiskError("YouTube 配额用完了（今天不再发请求）")
            if r.status == 401:
                self.token = ""
                raise AuthError("YouTube 授权失效，请重新授权")
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status} {str(j)[:200]}")
            return j

    async def login(self):
        if not (self.account.get("client_id") and self.secret("client_secret") and self.secret("refresh_token")):
            raise AuthError("还没完成授权（填 client id / secret，再点「授权」）")
        j = await self.api("GET", "/channels", {"part": "snippet", "mine": "true"})
        self.charge("list")
        ch = (j.get("items") or [None])[0]
        if not ch:
            raise AuthError("这个 Google 账号下没有 YouTube 频道")
        self.self_id, self.self_name = ch["id"], ch["snippet"].get("title", "")
        self.handle = (ch["snippet"].get("customUrl") or "").lstrip("@").lower()
        if self.account.get("mode") == "live":
            vid = self.account.get("live_video_id", "")
            v = await self.api("GET", "/videos", {"part": "liveStreamingDetails", "id": vid})
            self.charge("list")
            self.live_chat_id = (((v.get("items") or [{}])[0]).get("liveStreamingDetails") or {}).get("activeLiveChatId", "")
            if not self.live_chat_id:
                raise AuthError("这个视频现在没有进行中的直播聊天")

    def addressed(self, text: str) -> bool:
        return bool(self.handle) and f"@{self.handle}" in text.lower()

    # ---------- 收 ----------
    async def poll(self) -> list[Msg]:
        if self.account.get("mode") == "live":
            return await self.poll_live()
        out = []
        for vid in [v.strip() for v in re.split(r"[,\s]+", self.account.get("video_ids", "")) if v.strip()]:
            if not self.budget_left(COST["list"]):
                self.set_status("online", "今天的配额用完了，明天自动继续")
                return out
            j = await self.api("GET", "/commentThreads", {"part": "snippet,replies", "videoId": vid, "order": "time", "maxResults": 50})
            self.charge("list")
            for th in j.get("items", []):
                top = th["snippet"]["topLevelComment"]
                top_mine = top["snippet"].get("authorChannelId", {}).get("value") == self.self_id
                for c in [top] + (th.get("replies") or {}).get("comments", []):
                    sn = c["snippet"]
                    uid = (sn.get("authorChannelId") or {}).get("value", "")
                    text = sn.get("textOriginal") or sn.get("textDisplay", "")
                    if uid == self.self_id:
                        continue
                    is_reply = c is not top
                    at = self.addressed(text) or (is_reply and top_mine)
                    out.append(Msg("youtube", self.aid, "group", vid, uid, sn.get("authorDisplayName", "观众"),
                                   re.sub(rf"@{re.escape(self.handle)}", "", text, flags=re.I).strip() if self.handle else text,
                                   msg_id=top["id"], chat_name=f"视频 {vid}", at_ids=[self.self_id] if at else [],
                                   reply_to_uid=self.self_id if is_reply and top_mine else "", dedup=f"{vid}|{c['id']}"))
        out.sort(key=lambda m: m.dedup)
        return [m for m in out if m.at_ids]  # 只回被叫到的；没叫她的评论不进上下文（省配额和篇幅）

    async def poll_live(self) -> list[Msg]:
        wait = self.next_live - time.time()
        if wait > 0:
            return []
        if not self.budget_left(COST["live_list"]):
            self.set_status("online", "今天的配额用完了，明天自动继续")
            return []
        params = {"liveChatId": self.live_chat_id, "part": "snippet,authorDetails", "maxResults": 200}
        if self.page_token:
            params["pageToken"] = self.page_token
        j = await self.api("GET", "/liveChat/messages", params)
        self.charge("live_list")
        self.page_token = j.get("nextPageToken", "")
        self.next_live = time.time() + max(float(j.get("pollingIntervalMillis", 5000)) / 1000, 5) * self.engine.pace
        first = not hasattr(self, "_live_seen_first")
        self._live_seen_first = True
        out = []
        for it in j.get("items", []):
            sn, au = it["snippet"], it.get("authorDetails", {})
            if sn.get("type") != "textMessageEvent" or au.get("channelId") == self.self_id or first:
                continue  # 第一次拉到的是历史记录，不回
            text = sn.get("displayMessage", "")
            out.append(Msg("youtube", self.aid, "group", self.live_chat_id, au.get("channelId", ""), au.get("displayName", "观众"),
                           re.sub(rf"@{re.escape(self.handle)}", "", text, flags=re.I).strip() if self.handle else text,
                           msg_id=it["id"], chat_name="直播聊天", at_ids=[self.self_id] if self.addressed(text) else [],
                           dedup=f"{self.live_chat_id}|{it['id']}"))
        return [m for m in out if m.at_ids]

    # ---------- 发 ----------
    async def do_send(self, kind, chat_type, chat_id, text, reply_to):
        live = self.account.get("mode") == "live"
        cost = COST["live_insert"] if live else COST["comment_insert"]
        if not self.budget_left(cost):
            raise RiskError("今天的配额不够再发一条了")
        if live:
            body = {"snippet": {"liveChatId": self.live_chat_id, "type": "textMessageEvent",
                                "textMessageDetails": {"messageText": text[:190]}}}
            j = await self.api("POST", "/liveChat/messages", {"part": "snippet"}, body)
            self.charge("live_insert")
        else:
            body = {"snippet": {"parentId": reply_to, "textOriginal": text[:9000]}}
            j = await self.api("POST", "/comments", {"part": "snippet"}, body)
            self.charge("comment_insert")
        return [j.get("id", "")]

    def quota_text(self) -> str:
        return f"今日已用约 {self.used()} / {int(self.account.get('quota_cap') or 9000)} 点配额"
