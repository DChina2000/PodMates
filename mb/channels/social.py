"""评论区、帖子、视频类平台的渠道公共件（B站、贴吧、微博、Reddit、YouTube、X）。

和聊天群不一样：这里的「房间」是一个帖子 / 视频 / 话题，别人在底下 @她、回复她、给她发私信，她才回；
不主动插话、不冷场开话题（reply_only）。公共件管这些事：
  · 轮询新通知（没有公网地址，全靠定时拉）；
  · 本地去重：处理过的通知记下来，重启不重复回；发送成功才算「回过」，失败最多重试 3 次；
  · 节奏：评论、回复、私信分开设「两次之间等多久（随机）」和「每日额度」；
  · 风控：平台返回风控（B站 412 之类）立刻停止，发掉线通知，不原地重试；
  · 演练：用现有的影子模式（影子模式下引擎只记「本来会说什么」，不会调用 send）。
"""
import asyncio
import random
import time

import aiohttp

from .base import Channel, Msg
from .. import tls


class RiskError(Exception):
    """平台风控或封禁迹象：立刻停止，不重试。"""


class AuthError(Exception):
    """登录态失效 / 密钥不对：停止并提示重新登录。"""


class QuotaError(Exception):
    """今天这一类动作的额度用完了。"""


def parse_range(spec, default=(15, 45)) -> tuple[float, float]:
    try:
        a, b = str(spec).replace("~", "-").split("-")[:2]
        a, b = float(a), float(b)
        return (min(a, b), max(a, b))
    except Exception:
        return default


class PollChannel(Channel):
    reply_only = True
    supports_recall = False
    risky = False  # 非官方渠道：默认关闭、面板上标明风险
    kinds = ("comment", "reply", "dm")

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.session: aiohttp.ClientSession | None = None
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.inflight: set[str] = set()
        self.fails: dict[str, int] = {}
        self.last_action = 0.0
        self.last_dm: dict[str, str] = {}
        self.by_target: dict[str, str] = {}  # 「群|回复目标」→ 去重记号，发送失败时用来擦掉记号

    # ---------- 设置 ----------
    def S(self, key):
        return self.engine.settings.get(key, platform=self.platform)

    def secret(self, name: str) -> str:
        return self.engine.db.secret(f"acct:{self.aid}:{name}")

    def set_secret(self, name: str, value: str):
        self.engine.db.set_secret(f"acct:{self.aid}:{name}", value)

    def base(self, default: str) -> str:
        """接口基础地址：测试时账号上可以改成模拟服务的地址。"""
        return (self.account.get("api_base") or default).rstrip("/")

    # ---------- 生命周期 ----------
    async def login(self) -> None:
        """登录并填好 self_id / self_name；失败抛 AuthError。"""
        raise NotImplementedError

    async def poll(self) -> list[Msg]:
        """拉一次新通知，返回消息（已去重）。"""
        raise NotImplementedError

    async def do_send(self, kind: str, chat_type: str, chat_id: str, text: str, reply_to: str | None) -> list[str]:
        raise NotImplementedError

    def kind_for(self, chat_type: str, reply_to: str | None) -> str:
        if chat_type == "private":
            return "dm"
        return "reply" if reply_to else "comment"

    def http(self) -> aiohttp.ClientSession:
        if self.session is None:
            self.session = tls.session(timeout=aiohttp.ClientTimeout(total=30))
        return self.session

    async def start(self):
        self.stopping = False
        if self.risky and not self.account.get("risk_ack"):
            self.set_status("error", "还没勾选「我知道有风险」，不会启动")
            return
        self.set_status("starting", "正在登录")
        try:
            await self.login()
        except AuthError as e:
            self.set_status("error", str(e))
            return
        except Exception as e:
            self.set_status("error", f"登录失败：{e}")
            return
        self.set_status("online", f"已登录 {self.self_name or self.self_id}")
        self.task = asyncio.create_task(self._loop())

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
        backoff = 0
        while not self.stopping:
            try:
                msgs = await self.poll()
                if not self.engine.db.kv(f"baseline:{self.aid}"):
                    # 第一次拉到的是启动之前就在的老通知：记下来但不回（账号上勾了「回补」才回）
                    self.engine.db.set_kv(f"baseline:{self.aid}", True)
                    if not self.account.get("reply_backlog"):
                        for m in msgs:
                            self.mark_seen(m.dedup)
                        msgs = []
                for m in msgs:
                    if self.stopping:
                        break
                    await self.dispatch(m)
                backoff = 0
            except asyncio.CancelledError:
                raise
            except RiskError as e:
                await self.halt(f"触发平台风控，已立即停止：{e}")
                return
            except AuthError as e:
                self.set_status("error", str(e))
                self.engine.notify(f"{self.platform} 账号 {self.account.get('name', '')} 登录失效：{e}", self.account)
                return
            except Exception as e:
                backoff = min(backoff * 2 or 30, 600)
                self.engine.log("warn", self.platform, f"拉通知出错（{backoff} 秒后再试）：{type(e).__name__}: {e}")
            await asyncio.sleep(max(backoff, float(self.S("poll_seconds") or 60)) * (self.engine.pace or 0.01))

    async def halt(self, reason: str):
        self.stopping = True
        self.set_status("error", reason)
        self.engine.notify(f"{self.platform} 机器人 {self.account.get('name', '') or self.self_name} 已停止：{reason}", self.account)

    # ---------- 去重 ----------
    # 通知一投给引擎就记下来（重启也不会重复回）；发送失败（不是风控）最多重试 3 次：把记号擦掉，下次拉到再投一遍。
    def _seen(self) -> list:
        return self.engine.db.kv(f"seen:{self.aid}", [])

    def is_seen(self, key: str) -> bool:
        return key in self.inflight or key in self._seen()

    def mark_seen(self, key: str):
        seen = self._seen()
        if key not in seen:
            seen.append(key)
            self.engine.db.set_kv(f"seen:{self.aid}", seen[-3000:])
        self.inflight.discard(key)

    def unmark(self, key: str):
        self.inflight.discard(key)
        self.engine.db.set_kv(f"seen:{self.aid}", [k for k in self._seen() if k != key])

    def retry(self, key: str | None):
        if not key:
            return
        self.fails[key] = self.fails.get(key, 0) + 1
        if self.fails[key] < 3:
            self.unmark(key)

    async def dispatch(self, m: Msg):
        key = m.dedup
        if not key or self.is_seen(key):
            return
        self.mark_seen(key)
        if self.fails.get(key):
            m.dedup = f"{key}#{self.fails[key]}"  # 重试：换个记号，引擎自己的去重不把它当成重复
        if m.chat_type == "private":
            self.last_dm[m.chat_id] = key
        else:
            self.by_target[f"{m.chat_id}|{m.msg_id}"] = key  # 记的是不带重试后缀的原记号
            if len(self.by_target) > 500:
                self.by_target.pop(next(iter(self.by_target)))
        await self.engine.on_message(m)

    # ---------- 节奏与额度 ----------
    def quota_key(self, kind: str) -> str:
        return f"quota:{self.aid}:{time.strftime('%Y-%m-%d')}:{kind}"

    def quota_left(self, kind: str) -> int:
        return int(self.S(f"quota_{kind}") or 0) - int(self.engine.db.kv(self.quota_key(kind), 0))

    def use_quota(self, kind: str):
        self.engine.db.set_kv(self.quota_key(kind), int(self.engine.db.kv(self.quota_key(kind), 0)) + 1)

    async def pace(self, kind: str):
        lo, hi = parse_range(self.S(f"pace_{kind}"))
        wait = random.uniform(lo, hi) * self.engine.pace
        wait = max(0.0, wait - (time.time() - self.last_action))
        # 分小段睡，停止时能立刻醒
        end = time.time() + wait
        while not self.stopping and time.time() < end:
            await asyncio.sleep(min(0.25, max(0.0, end - time.time())))

    async def send(self, chat_type, chat_id, text, reply_to=None):
        kind = self.kind_for(chat_type, reply_to)
        if self.stopping or self.status != "online":
            raise AuthError("账号已停止，没有发送")
        if self.quota_left(kind) <= 0:
            self.engine.log("warn", self.platform, f"今天的{ {'comment': '评论', 'reply': '回复', 'dm': '私信'}[kind] }额度用完了，不再发")
            raise QuotaError("今日额度用完")
        await self.pace(kind)
        if self.stopping:
            raise AuthError("账号已停止，没有发送")
        try:
            ids = await self.do_send(kind, chat_type, chat_id, text, reply_to)
        except RiskError as e:
            await self.halt(f"触发平台风控，已立即停止：{e}")
            raise
        except Exception:
            self.retry(self.by_target.get(f"{chat_id}|{reply_to}") if reply_to else self.last_dm.get(chat_id))
            raise
        self.last_action = time.time()
        self.use_quota(kind)
        return ids


def msg_key(*parts) -> str:
    return "|".join(str(p) for p in parts)
