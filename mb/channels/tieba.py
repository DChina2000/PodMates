"""百度贴吧（非官方渠道）：用登录 cookie（BDUSS）读「@我」，在帖子里回复。

风险（面板上会标明，默认关闭，要勾「我知道有风险」才启动）：
  · 贴吧没有官方机器人接口。读通知用的是 App 客户端协议的 JSON 接口，回帖用的是网页接口；
  · 高频回帖有被永久屏蔽、封号的先例；请用小号、低频；
  · 遇到要验证码、"操作太频繁"一类返回立即整体停止并发掉线通知，不原地重试。
  · 本适配器的接口细节依据公开资料整理，没有在真账号上验证过，请先用「影子模式」演练。
回复范围：只回 @ 她的帖子 / 楼层，回在对应主题帖下（楼中楼带上被回复楼层的 pid）。
"""
import json
import re

from .base import Msg
from .social import AuthError, PollChannel, RiskError

APP = "https://tiebac.baidu.com"
WEB = "https://tieba.baidu.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
RISK_TEXT = ("验证码", "vcode", "太频繁", "过于频繁", "频繁", "屏蔽", "封禁")
RISK_CODES = {40, 220034, 238, 340001, 110003}


class TiebaChannel(PollChannel):
    platform = "tieba"
    risky = True

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.tbs = ""

    def cookies(self) -> str:
        ck = f"BDUSS={self.secret('BDUSS')}"
        return ck + (f"; STOKEN={self.secret('STOKEN')}" if self.secret("STOKEN") else "")

    def host(self, which: str) -> str:
        b = self.account.get("api_base")
        return b.rstrip("/") if b else {"app": APP, "web": WEB}[which]

    async def call(self, method: str, which: str, path: str, params=None, data=None) -> dict:
        async with self.http().request(method, self.host(which) + path, params=params, data=data, proxy=self.proxy(),
                                       headers={"User-Agent": UA, "Cookie": self.cookies(), "Referer": WEB + "/"}) as r:
            if r.status in (403, 412, 429):
                raise RiskError(f"HTTP {r.status}")
            text = await r.text()
        try:
            return json.loads(text)
        except ValueError:
            raise RuntimeError(f"返回的不是 JSON：{text[:100]}")

    async def login(self):
        if not self.secret("BDUSS"):
            raise AuthError("还没填 BDUSS（浏览器登录贴吧后，在 Cookie 里复制）")
        j = await self.call("GET", "web", "/dc/common/tbs")
        if not j.get("is_login"):
            raise AuthError("BDUSS 失效了，请重新复制")
        self.tbs = j.get("tbs", "")
        u = await self.call("GET", "web", "/f/user/json_userinfo")
        d = u.get("data") or {}
        self.self_id, self.self_name = str(d.get("id") or d.get("uid") or ""), d.get("name_show") or d.get("user_name_weak") or ""
        if not self.self_id:
            raise AuthError("取不到自己的用户 ID，BDUSS 可能不对")

    async def poll(self) -> list[Msg]:
        j = await self.call("POST", "app", "/c/u/feed/atme", data={"BDUSS": self.secret("BDUSS"), "_client_version": "12.35.1.0", "pn": 1})
        if str(j.get("error_code", "0")) not in ("0", ""):
            raise RuntimeError(f"读 @我 失败：{j.get('error_msg', j.get('error_code'))}")
        out = []
        for a in j.get("at_list") or []:
            rep = a.get("replyer") or {}
            uid = str(rep.get("id", ""))
            if not uid or uid == self.self_id or not a.get("post_id"):
                continue
            text = re.sub(r"@\S+\s*", "", re.sub(r"^回复\s*[^:：]*[:：]", "", a.get("content", ""))).strip() or a.get("content", "")
            tid, pid = a.get("thread_id"), a.get("post_id")
            out.append(Msg("tieba", self.aid, "group", str(tid), uid, rep.get("name_show") or rep.get("name") or "吧友", text,
                           msg_id=f"{a.get('fname', '')}|{a.get('fid', '')}|{tid}|{pid}|{a.get('quote_pid', '') if a.get('is_floor') else ''}",
                           chat_name=f"{a.get('fname', '')}吧 帖子{tid}", at_ids=[self.self_id], dedup=f"{tid}|{pid}"))
        return out

    async def do_send(self, kind, chat_type, chat_id, text, reply_to):
        if chat_type == "private":
            raise RuntimeError("贴吧私信暂不支持")
        fname, fid, tid, pid, quote = (reply_to or "||||").split("|")
        data = {"ie": "utf-8", "kw": fname, "fid": fid, "tid": tid, "tbs": self.tbs, "content": text, "vcode_md5": "", "vcode": ""}
        if pid:  # 回到被 @ 的那一层
            data.update({"quote_id": pid, "repostid": pid, "rich_text": 1, "floor_num": 1})
        j = await self.call("POST", "web", "/f/commit/post/add", data=data)
        err = int(j.get("no", j.get("err_code", 0)) or 0)
        if err:
            msg = str(j.get("error") or j.get("err_msg") or j.get("data") or "")
            if err in RISK_CODES or any(t in msg for t in RISK_TEXT) or (j.get("data") or {}).get("need_vcode"):
                raise RiskError(f"贴吧要求验证码或限制了操作（{err} {msg[:40]}）")
            raise RuntimeError(f"回帖失败 {err} {msg[:60]}")
        return [str((j.get("data") or {}).get("pid", ""))]
