"""消息引擎：所有平台的消息都汇到这里。

每个群（room）一个顺序队列：一条真人消息进来 → 先看谁被@/回复/叫名字（必回）和管理指令 →
其余机器人交给守门员判断要不要插话 → 最多 N 个机器人按顺序说，后说的能看到先说的 →
机器人之间可以接着互相接话，最多 M 轮，到了就停，等真人开口。
"""
import asyncio
import json
import random
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import base64
import hashlib

from . import cleanup, commands, customcmd, files, pacing, personas, prefilter, remind
from .channels import napcat as napcat_mod
from .channels.base import Msg, Muted
from .db import new_id
from .embed import Embedder
from .llm import ContextOverflow, UsageCapReached
from .rules import Duty
from .memory import CHARS_PER_TOKEN, Memory, parse_json, subject_key
from .modelwatch import ModelWatch
from .people import People
from .search import Search
from .search import tool_spec as search_tool
from .settings import COMMANDS, EAGER, LANG_NAME
from .stickers import FAV_PLATFORMS, Stickers, ext_of
from .stickers import tool_spec as sticker_tool

GATEKEEPER_PROMPT = """你替{平台群}里的一位群友判断：没人@她，看到最新这条消息，她此刻要不要主动开口。

【她】{summary}记录里"她（自己）"是她自己之前发的{marks}同一个号换班之前由别的角色说的话，会标成"某某（这个号之前的角色）"，那不是她说的。

【最近群聊】
{context}

【最新消息】
{speaker}: {text}

按一个真人群友的分寸判断，目标是让这个群聊得更好玩。
该开口：有人提到她，或话里明摆着是冲她来的但没@（只凭语气、时机猜的不算）；有人问了问题没人接；有她接得住的梗、补得上的新信息或新角度、看不下去要对线的说法；{user_line}群冷了而最新消息留了能接的口子。
不开口：别人正聊得热，她插进去只是打断；她刚说过而话头没再转向她；消息是@别人或回复别人的，话头没转向她；只能复读、附和、凑热闹；消息本身没东西可接（纯表情、签到、链接）。
只输出JSON：{"brief": "一句话理由；要开口就写她接哪个点", "reply": true 或 false}"""

# 插话力度：（问守门员的概率，冷却倍数）
EAGER_PARAMS = {1: (0.25, 3.0), 2: (0.5, 2.0), 3: (1.0, 1.0), 4: (1.0, 0.5), 5: (1.0, 0.25)}
EAGER_LABEL = dict(EAGER)
MODE_LABEL = {"at": "被叫到", "pro": "主动插话", "poke": "戳一戳", "force": "面板让她回", "cold": "冷场开话题",
              "schedule": "定时消息", "manual": "人工发言", "command": "指令", "repeat": "跟复读", "refuse": "拒绝指令",
              "remind": "提醒", "cap": "用量用完", "sticker": "表情包", "custom": "自定义指令"}
MENTION_END = r"(?=$|[\s,.!?;:，。！？；：、）)\]】」』…~～])"  # 「@名字」后面要跟结尾、空白或标点，名字才算对得精确
QUOTA_POLL = 30  # 排队等回复名额时，最多睡这么多秒就醒来看一眼（测试里调小）
QUOTA_MODES = ("at", "poke", "pro", "cold")  # 算一次「回复名额」、也受「中间没人说话时最多连着发几条」拦的几类（面板让她回、提醒、定时、指令和回执、用量用完那句不算）
MUTABLE_MODES = ("at", "pro", "poke", "cold")  # 闭嘴（或关掉说话）之后，这几类还没发出的回复一律作废
CAP_MODES = ("at", "poke", "force", "refuse")  # 用量硬上限生效时，这些也不调模型，只回固定的话
DEFAULT_CAP_MESSAGE = "今天的额度用完了，明天再来"
OBSERVE_SECS = 600  # 她刚说过话后这么久之内，才为「别说了」和反馈多问一次守门员

GATE_EXTRA_HEAD = "\n\n【另外的判断】在上面的 JSON 里再加这几项："
GATE_STOP = """
"stop": true 或 false。最新这条消息是不是在叫群里的机器人（泛指，不是某个具体的人）别说了、少说点、闭嘴、别刷屏。明确在叫机器人停才算 true；聊别的、开玩笑、冲着某个具体的人说的都是 false。"""
GATE_FEEDBACK = """
"feedback": 她{mins}分钟前主动插过一句「{text}」。看那之后的群聊，群友的反应是：warm（有人顺着接着聊）/ ignored（没人理）/ annoyed（被嫌烦、被叫停）/ unknown（还看不出来）。"""


SENT_END = re.compile(r"(?<=[。！？!?…~～\n])")
CLAUSE_END = re.compile(r"[，,、；;：:\s]")
GRAM_STOP = set("的了是我你他她它们在不有就也都这那吗呢吧啊嘛哦呀么和与或但而被把让给对从到把着过个一二三四五六七八九十")


def trim_sentences(text: str, cap: int) -> str:
    """硬压到 cap 字以内：按整句收，不在句子中间断；连第一句都放不下，才在最近的逗号处断。"""
    if len(text) <= cap:
        return text
    out = ""
    for part in SENT_END.split(text):
        if len(out) + len(part) > cap:
            break
        out += part
    if out.strip():
        return out.strip()
    head = text[:cap]
    cut = max((m.start() for m in CLAUSE_END.finditer(head)), default=-1)
    return (head[:cut] if cut >= cap // 3 else head).rstrip("，,、；;：: ")


def repeated_phrases(texts: list[str], top: int = 4) -> list[str]:
    """一组发言里反复出现的词句（梗、术语、比喻）：同一个说法出现在不同的几条里。纯统计，不调模型。
    2 个字的词要出现在 3 条以上才算；更长的出现在 2 条就算。去掉被更长的说法盖住的，和全是虚词的。"""
    df: dict[str, int] = {}
    for t in texts:
        seen = set()
        for seg in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", t):
            for n in range(2, 17):
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    if re.fullmatch(r"[0-9A-Za-z]+", g) and not (len(seg) == n):
                        continue  # 英文数字只收整词
                    seen.add(g)
        for g in seen:
            df[g] = df.get(g, 0) + 1
    cand = {g: c for g, c in df.items() if c >= (3 if len(g) == 2 else 2) and sum(ch not in GRAM_STOP for ch in g) >= 2}
    for g in sorted(cand, key=len, reverse=True):  # 被更长的、出现次数一样的说法盖住的，去掉
        if any(g != h and g in h and cand[h] >= cand[g] for h in cand if len(h) > len(g)):
            cand.pop(g, None)
    out: list[str] = []
    for g in sorted(cand, key=lambda g: (-(cand[g] * len(g)), g)):
        if not any(g in h or h in g or any(g[i:i + 3] in h for i in range(len(g) - 2)) for h in out):  # 和已选的重叠就不重复报
            out.append(g)
        if len(out) >= top:
            break
    return out


NO_STICKER_NOTE = "\n\n这次回复发不了表情包，只能发文字。"
STK_MARK = re.compile(r"【表情\s*(\d+)\s*】")  # 她从候选里挑表情包时在回复里写的标记


def _tool_hint(tools: list, dm: bool = False) -> str:
    lines = {"web_search": "- web_search：上网搜索。只在需要最新信息或要查证时才用，闲聊和你自己知道的不要搜；不要拿群友的名字、账号去搜；搜到的内容用自己的话说。",
             "send_sticker": f"- send_sticker：按心情找几张{'你收藏的' if dm else '这个群里常用的'}表情包给你挑，选中的跟在你的文字后面发。气氛合适、一张图比一句话更到位时才用。"}
    return "\n\n【你手边的工具】\n" + "\n".join(lines[t["name"]] for t in tools) + "\n用不用由你判断；不管用不用，都要给出这次的文字回复。"


def fill(tpl: str, **vals) -> str:
    return re.sub(r"\{(" + "|".join(vals) + r")\}", lambda m: str(vals[m.group(1)]), tpl)


def in_hours(spec: str, t: float | None = None) -> bool:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", spec or "")
    if not m:
        return False
    lt = time.localtime(t or time.time())
    now = lt.tm_hour * 60 + lt.tm_min
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    return a <= now < b if a <= b else (now >= a or now < b)


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"^#+\s*", "", text, flags=re.M)


def split_reply(text: str, threshold: int, max_parts: int) -> list[str]:
    """短句分条发：回复里的每一行单独一条（真人是一句一句打字的）。一行超过 threshold 字的，再按句子拆成不超过 threshold 的几块。
    条数超过 max_parts 时，不断把「合起来最短」的相邻两条并成一条（用换行连），直到不超过——这样最长的几行不会被硬塞在一起。"""
    text = text.strip()
    if not text:
        return [text]
    parts: list[str] = []
    for line in (p.strip() for p in text.split("\n")):
        if not line:
            continue
        if len(line) <= threshold:
            parts.append(line)
            continue
        chunk = ""
        for s in (x for x in re.split(r"(?<=[。！？!?…])", line) if x.strip()):
            if chunk and len(chunk) + len(s) > threshold:
                parts.append(chunk.strip())
                chunk = ""
            chunk += s
        if chunk.strip():
            parts.append(chunk.strip())
    max_parts = max(1, max_parts)
    while len(parts) > max_parts:
        i = min(range(len(parts) - 1), key=lambda k: len(parts[k]) + len(parts[k + 1]))
        parts[i:i + 2] = [parts[i] + "\n" + parts[i + 1]]
    return parts or [text]


@dataclass
class Entry:
    uid: str
    name: str  # 显示名（人物表里的人带〔X本人〕）
    text: str
    bot: str | None = None  # 机器人账号 id
    ids: dict = field(default_factory=dict)  # 账号 id → 该账号看到的消息 id
    at_ids: list = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    attach: str = ""  # 读文件功能抽出的文字（只给这一次回复用）
    note: str = ""  # 刚替他办完的事（记住、设提醒）：只给这一次回复用，让她用自己的口吻应一句
    reply_uid: str = ""  # 这条消息回复的是谁
    at_uids: list = field(default_factory=list)  # 这条消息 @ 了谁
    seq: int = 0  # 在本群的先后序号（判断「生成期间又来了几条新消息」）
    notes: dict = field(default_factory=dict)  # 账号 → 对这条消息办完的事（合并回复时要用）
    dbid: int = 0  # 在 messages 表里的编号（0 = 不在表里）：整理记忆按它算「原始消息要带到哪」
    role: str = ""  # 机器人发的消息：当时在岗的角色 id（换了角色之后，旧角色说的话不能当成新角色自己说的）
    pre_cmd: dict | None = None  # 消息一进来就办的指令的结果：{done, consumed, handled}；None = 还没办（或不是从正常入口进来的）
    cmd_task: object = None  # 正在办指令的任务；排队处理这条消息时先等它办完


class Room:
    def __init__(self, key: str, platform: str, chat_id: str, name: str):
        self.key, self.platform, self.chat_id, self.name = key, platform, chat_id, name
        self.ctx: deque[Entry] = deque(maxlen=200)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.accounts: set[str] = set()
        self.seen: OrderedDict[str, Entry] = OrderedDict()
        self.chain = 0
        self.mute: dict[str, float] = {}
        self.last_sent: dict[str, list[str]] = {}
        self.last_pro: dict[str, float] = {}
        self.hourly: dict[str, deque] = {}
        self.daily: dict[str, list] = {}  # 账号 → [日期, 次数]
        self.cold: list = ["", 0]
        self.last_human = time.time()
        self.mem_count = 0
        self.mem_busy = False
        self.mem_cov = 0  # 已经整理进前情的最后一条消息的编号：比它新的消息都要以原始消息的样子带给模型
        self.mem_retry_at = 0.0  # 整理没成功：到这个时刻之前不再试
        self.mem_fail = 0
        self.busy = False
        self.duty_last: dict[str, str] = {}  # 账号 → 上次看到的在岗角色（换班检测）
        self.files: OrderedDict[str, dict] = OrderedDict()  # 最近的文件消息：key → {ids, files}
        self.seq = 0
        self.stk_fetch: deque = deque()  # 最近一小时下载了几张图来算指纹（学表情包）
        self.last_spoke: dict[str, float] = {}  # 账号 → 上次真发出东西的时间
        self.last_reply: dict[str, float] = {}  # 账号 → 上次发完一次回复（speak）的时间；「两次回复最小间隔」从这里算
        self.pro_pending: dict[str, dict] = {}  # 账号 → 她上一次主动插话，还在等群友的反馈：{ts, text, after}
        self.pause_until = 0.0  # 有人叫停后，该群所有机器人暂停主动插话到这个时刻
        self.sched_done: set[str] = set()  # 今天已经发过的定时消息：「日期 时刻 序号」
        self.pro_log: dict[str, list[float]] = {}  # 账号 → 近一天发言（插话、表情包）的时间戳：存库，重启后每小时、每天的计数从这里接上
        self.stop_hist: list[float] = []
        self.last_stop = 0.0
        self.bot_rounds: dict[tuple, int] = {}  # （我们的账号, 别的机器人）→ 真人开口之后已经接了几轮
        self.quota: dict[str, list] = {}  # 账号 → [手里的回复名额数, 算到哪个时刻]：每小时最多回几次，存库，重启后接着算
        self.run: dict[str, int] = {}  # 账号 → 她连着发了几个气泡、中间没人说话（别人说话就清零）
        self.q_wait: dict[str, dict] = {}  # 账号 → 名额用完后排队等着回的 {items: [...], task}
        self.pro_wait: dict = {}  # 主动插话正在等发言人说完：{uid, start（这一串的第一条的时间）, task}；没在等是空的


class Bot:
    """一个机器人账号。duty_role 不为 None 时是「此刻在某个群里在岗」的视图（角色由排班规则决定）。"""

    def __init__(self, engine, account: dict, channel, duty_role: str | None = None, duty_persona: str | None = None):
        self.engine, self.account, self.channel = engine, account, channel
        self.duty_role, self.duty_persona = duty_role, duty_persona
        self.duty_src = "default"

    @property
    def aid(self) -> str:
        return self.account["id"]

    @property
    def platform(self) -> str:
        return self.account["platform"]

    @property
    def role(self) -> dict:
        rid = self.duty_role if self.duty_role is not None else self.account.get("role_id")
        return self.engine.roles.get(rid) or {"id": "", "name": "未绑定角色", "summary": ""}

    @property
    def rid(self) -> str:
        return self.role["id"]

    @property
    def uid(self) -> str:
        return self.channel.self_id if self.channel else ""

    @property
    def online(self) -> bool:
        return bool(self.channel and self.channel.status == "online")


class Engine:
    def __init__(self, db, settings, llm, emit=None):
        self.db, self.settings, self.llm = db, settings, llm
        self._emit = emit or (lambda *a: None)
        self.people = People(db)
        self.embedder = Embedder(db, self.log, self.emit)
        self.duty_rules = Duty(db)
        self.memory = Memory(db, settings, self.people, llm, self.log, self.embedder, self.spawn)
        self.bots: dict[str, Bot] = {}
        self.rooms: dict[str, Room] = {}
        self.reminders = remind.Reminders(db)
        self.search = Search(db, settings, llm, self.log)
        self.stickers = Stickers(self)
        self.tunes: dict[str, pacing.Tune] = {}
        self.cap_sent: dict[str, float] = {}  # 用量硬上限：哪里刚回过固定的话
        self.custom_last: dict[tuple, float] = {}  # （自定义指令, 群）→ 上次触发的时间（冷却用）
        self.stk_out: dict[tuple, list] = {}  # (群, 账号) → 这次回复里模型选好要发的表情包
        self.redesc_next = 0.0  # 自动重新描述表情包：这个时间之前不再启动新的一轮
        self.stk_cand: dict[tuple, dict] = {}  # (群, 账号) → 这次回复里 send_sticker 给她看的候选：{mood, rows}
        self.embedder.cap_check = lambda: self.llm.hard_capped()
        self.acct_hourly: dict[str, deque] = {}  # 账号 → 近一小时主动发言时间戳（所有群合计）
        self.private_hist: dict[str, list] = {}
        self.reset = None  # 重置记忆（备份、撤销）：Core 建好后挂上
        self.err_notice: dict[str, float] = {}  # 模型出错的提示：哪里刚提示过（群里那句和面板上那条都按时间限频）
        self.dm_locks: dict[str, asyncio.Lock] = {}  # 私聊「两次回复最小间隔」：账号:对方 → 排队用的锁
        self.dm_last: dict[str, float] = {}  # 账号:对方 → 上次私聊回复完的时间
        self.dm_run: dict[str, int] = {}  # 账号:对方 → 她在这个私聊里连着发了几个气泡、中间对方没说话（对方说话就清零）
        self.dm_pend: dict[str, dict] = {}  # 账号:对方 → {msgs: 还没回的几条, first: 第一条的时间, task: 等对方说完的定时任务, running: 正在写回复}
        self.roles: dict[str, dict] = {}
        self.pace = 1.0  # 测试里设 0：分条发送不等待
        self.notify_hook = None  # 托盘弹通知
        self.watch = ModelWatch(self)  # 定时查新模型、提醒、替换
        self.update_hook = None  # Core 挂上：查一次新版本（私聊「查更新」用）
        self.tasks: set[asyncio.Task] = set()
        self.reload()
        self.restore_counts()
        self.bans: dict[tuple, dict] = {}  # (账号, 群) → 这个号在这个群里的禁言状态；存库，重启后接着用
        self.restore_bans()

    # ---------- 基础 ----------
    def reload(self):
        self.roles = {r["id"]: r for r in self.db.docs("role")}
        self.modules = {m["id"]: m for m in self.db.docs("module")}
        self.personas = {p["id"]: p for p in self.db.docs("persona")}
        personas.set_library(self.db.docs("archetype"))
        self.people.reload()

    def emit(self, kind: str, data):
        self._emit(kind, data)

    def log(self, level: str, cat: str, text: str, data=None):
        ts = time.time()
        self.db.x("INSERT INTO logs (ts, level, cat, text, data) VALUES (?,?,?,?,?)",
                  (ts, level, cat, text, json.dumps(data, ensure_ascii=False) if data else None))
        self.emit("log", {"ts": ts, "level": level, "cat": cat, "text": text, "data": data})

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        t.add_done_callback(self._task_err)
        return t

    def _task_err(self, t: asyncio.Task):
        if not t.cancelled() and t.exception():
            e = t.exception()
            self.log("error", "system", f"{type(e).__name__}: {e}")

    def S(self, key, bot: Bot | None = None, room: Room | None = None, platform: str | None = None):
        return self.settings.get(key, bot.rid if bot else None,
                                 room.platform if room else (bot.platform if bot else platform),
                                 room.key if room else None, bot.aid if bot and room else None)

    def chat_doc(self, room: Room | None) -> dict | None:
        return self.db.doc("chat", room.key) if room else None

    def bot_uids(self, platform: str) -> dict[str, Bot]:
        return {b.uid: b for b in self.bots.values() if b.platform == platform and b.uid}

    # ---------- 账号 ----------
    def add_bot(self, account: dict, channel) -> Bot:
        b = Bot(self, account, channel)
        self.bots[account["id"]] = b
        return b

    async def start_account(self, account: dict):
        from .channels import make_channel
        await self.stop_account(account["id"])
        ch = make_channel(self, account)
        self.add_bot(account, ch)
        self.spawn(ch.start())

    async def stop_account(self, aid: str):
        b = self.bots.pop(aid, None)
        if b and b.channel:
            await b.channel.stop()
            b.channel.set_status("stopped")

    async def start_all(self):
        for line in napcat_mod.take_install_notes():  # 安装程序因为 NapCat / QQ 占着程序目录的文件而把它们停掉时留下的说明
            self.log("warn", "napcat", f"安装程序：{line}")
        wanted = set()
        for a in self.db.docs("account"):
            if a.get("enabled", True) and self.platform_on(a["platform"]):
                if a["platform"] == "qq" and a.get("manage_napcat", True):
                    wanted.add(str(a.get("qq")))
                await self.start_account(a)
        # NapCat 现在会在 PodMates 退出后继续挂着：账号被停用 / 删掉 / 改成不托管 NapCat 之后，上一次留下的那个没人接管了，在这里清掉（正在跑的账号不动）
        running = {str(b.channel.account.get("qq")) for b in self.bots.values() if getattr(b.channel, "napcat", None)}
        gone = await asyncio.get_running_loop().run_in_executor(None, napcat_mod.reap_unwanted, wanted | running)
        for uin, pids in gone.items():
            self.log("warn", "napcat", f"[{uin}] 这个号这次不启动，清掉了它上一次留下的 {len(pids)} 个 NapCat / QQ 进程")

    async def stop_all(self):
        for aid in list(self.bots):
            await self.stop_account(aid)

    def platform_on(self, platform: str) -> bool:
        return bool((self.db.kv("platforms") or {}).get(platform, False))

    NOTIFY_ORDER = ("qq", "discord", "telegram", "wechat")  # 找得到用户的几条路，按这个先后挑

    def notify_targets(self, account: dict | None = None) -> list[tuple[Bot, str]]:
        """出事（掉线、风控、版本不对……）时能一条条试的通知目标，按 NOTIFY_ORDER 的平台先后、用户卡上账号的先后排。
        每个目标 =（从哪个账号发, 发给用户卡上的哪个账号）：出事的那个账号除外。QQ 按 QQ 号，微信按秘密备注，Discord、Telegram 按 ID。"""
        user = self.people.user_card()
        if not user:
            return []
        out, seen = [], set()
        for plat in self.NOTIFY_ORDER:
            bots = [b for b in self.bots.values()
                    if b.platform == plat and b.online and not b.channel.reply_only and not (account and b.aid == account["id"])]
            for uid in People.uids(user, plat):
                for b in bots:
                    if (b.aid, uid) not in seen:
                        seen.add((b.aid, uid))
                        out.append((b, uid))
        return out

    def notify_target(self, account: dict | None = None) -> tuple[Bot, str] | None:
        """第一个能试的目标（和以前一样：挑一个能联系到用户的在线账号，出事的那个除外）；没有就是一个都联系不上。"""
        got = self.notify_targets(account)
        return got[0] if got else None

    async def _notify_send(self, b: Bot, uid: str, text: str) -> bool:
        """私聊发一条通知。发出去了返回 True；没发出去记一条 warn 返回 False，调用方接着试下一个目标。"""
        try:
            await b.channel.send("private", uid, f"【机器人通知】{text}")
            return True
        except Exception as ex:
            self.log("warn", "notify", f"通知没发出去（{b.account.get('name') or b.aid}）：{ex}")
            return False

    async def _notify_dm(self, notice_id: str, text: str, account: dict | None = None):
        """按顺序把用户卡上的账号挨个试：有一个私聊发出去了就停，via 写这个平台；都对不上就只留下面板的未读通知，记一条 warn。
        绝不发到任何群里（通知只按私聊发，微信渠道自己还会核对开着的不是群）。"""
        tried = []
        for b, uid in self.notify_targets(account):
            tried.append(f"{b.platform}「{uid}」")
            if await self._notify_send(b, uid, text):
                self._notice_via(notice_id, b.platform)
                return
        self.log("warn", "notify", f"通知没能私聊到用户（试过：{'、'.join(tried) or '没有能用的在线账号'}），"
                                   "只留在面板的「未读通知」里，没有发到任何群里")

    def notify(self, text: str, account: dict | None = None):
        """出事了：记日志、记一条「未读通知」（面板总览里看得到）、弹 Windows 气泡、再按用户卡上的账号挨个私聊他一句。
        via 只在真的私聊发出去以后才写（写实际发出去的那个平台）；一个都没发出去就空着。"""
        self.log("warn", "notify", text)
        if self.settings.g("notify_windows") and self.notify_hook:
            try:
                self.notify_hook(text)
            except Exception:
                pass
        notice = self.add_notice(text, "")
        if self.settings.g("notify_dm_owner"):
            if self.notify_targets(account):
                self.spawn(self._notify_dm(notice["id"], text, account))
            else:  # 一个能试的账号都没有：不等异步那一步，当场记下（这个函数也会在微信线程里被调用，那里没有事件循环）
                self.log("warn", "notify", "通知没能私聊到用户（试过：没有能用的在线账号），只留在面板的「未读通知」里，没有发到任何群里")

    def add_notice(self, text: str, via: str = "") -> dict:
        """面板总览里的「未读通知」（最多留 50 条）。返回这一条（id 给后面补 via 用）。"""
        notices = [{"id": new_id("nt_"), "ts": time.time(), "text": text, "read": False, "via": via}] + (self.db.kv("notices") or [])
        self.db.set_kv("notices", notices[:50])
        self.emit("notice", {"unread": sum(1 for n in notices if not n["read"])})
        return notices[0]

    def _notice_via(self, notice_id: str, via: str):
        """通知真的私聊发出去了：把面板上那一条的 via 写成实际发出去的平台。"""
        notices = self.db.kv("notices") or []
        for n in notices:
            if n.get("id") == notice_id:
                n["via"] = via
                self.db.set_kv("notices", notices)
                return

    def mark_notices_read(self, ids: list[str] | None = None):
        """把未读通知标成已读（ids 为空 = 全部）。"""
        notices = self.db.kv("notices") or []
        for n in notices:
            if not ids or n["id"] in ids:
                n["read"] = True
        self.db.set_kv("notices", notices)
        self.emit("notice", {"unread": sum(1 for n in notices if not n["read"])})

    # ---------- 入口 ----------
    def room(self, platform: str, chat_id: str, name: str = "") -> Room:
        key = f"{platform}:{chat_id}"
        r = self.rooms.get(key)
        if not r:
            r = self.rooms[key] = Room(key, platform, chat_id, name)
            self.restore_room(r)
            if not self.db.doc("chat", key):
                self.db.put("chat", {"id": key, "platform": platform, "chat_id": chat_id, "name": name,
                                     "desc": "", "attitude": None, "schedules": []})
            r.worker = asyncio.create_task(self._worker(r))
        if name and r.name != name:
            r.name = name
            d = self.db.doc("chat", key)
            if d and d.get("name") != name:
                d["name"] = name
                self.db.put("chat", d)
        return r

    # ---------- 重启后接上：群上下文、闭嘴 / 暂停、发言计数、私聊历史 ----------
    def ctx_floor(self, key: str) -> float:
        """「重置记忆」清掉上下文的时刻：回填只取这之后的消息，免得重启后又把清掉的找回来。"""
        return float((self.db.kv("ctx_floor") or {}).get(key, 0))

    def restore_room(self, r: Room):
        """群第一次建 Room 时：从 messages 表回填最近的上下文（带上当时的角色，按角色标注照常生效），并恢复没到期的闭嘴、暂停和发言计数。只读 messages。"""
        n = min(200, max(1, int(self.S("context_window", room=r) or 30)))
        r.mem_cov = int(self.db.kv(f"memcov:{r.key}", None) or self.db.kv(f"memlast:{r.key}", 0) or 0)
        rows = self.db.rows("SELECT id, ts, account_id, user_id, name, text, role_id, kind FROM messages WHERE room=? AND ts>? AND kind IN ('human','bot') "
                            "ORDER BY id DESC LIMIT 200", (r.key, self.ctx_floor(r.key)))[::-1]
        if self.memory.enabled(r.platform, r.key):  # 最近 n 条，加上还没整理进前情的（不漏）
            rows = [m for i, m in enumerate(rows) if i >= len(rows) - n or m["id"] > r.mem_cov]
        else:
            rows = rows[-n:]
        for m in rows:
            r.seq += 1
            r.ctx.append(Entry(m["user_id"], m["name"], m["text"], bot=m["account_id"] if m["kind"] == "bot" else None,
                               role=m["role_id"] if m["kind"] == "bot" else "", ts=m["ts"], seq=r.seq, dbid=m["id"]))
        r.mem_count = self.db.x("SELECT COUNT(*) FROM messages WHERE room=? AND id>? AND kind IN ('human','bot','import')", (r.key, r.mem_cov)).fetchone()[0]
        st = self.db.kv(f"room_state:{r.key}") or {}
        now = time.time()
        r.mute = {a: t for a, t in (st.get("mute") or {}).items() if t > now}
        r.pause_until = float(st["pause_until"]) if float(st.get("pause_until") or 0) > now else 0.0
        r.stop_hist = [t for t in (st.get("stop_hist") or []) if now - t < 86400]
        r.pro_log = {a: [t for t in ts if now - t < 86400] for a, ts in (self.db.kv(f"pro:{r.key}") or {}).items()}
        r.quota = {a: [float(v[0]), float(v[1])] for a, v in (self.db.kv(f"quota:{r.key}") or {}).items()}
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        day = time.strftime("%Y-%m-%d")
        for a, ts in r.pro_log.items():
            r.hourly[a] = deque(t for t in ts if now - t <= 3600)
            r.daily[a] = [day, sum(1 for t in ts if t >= midnight)]

    def save_room_state(self, r: Room):
        """闭嘴、暂停的到期时间存库（跨重启有效）。"""
        now = time.time()
        self.db.set_kv(f"room_state:{r.key}", {"mute": {a: t for a, t in r.mute.items() if t > now},
                                               "pause_until": r.pause_until if r.pause_until > now else 0, "stop_hist": r.stop_hist[-20:]})

    def restore_counts(self):
        """启动时把各群近一小时的发言时间戳并进「账号每小时」计数（风控按整个账号算，要所有群合计）。"""
        now = time.time()
        for row in self.db.rows("SELECT k, v FROM kv WHERE k LIKE 'pro:%'"):
            for aid, ts in (json.loads(row["v"]) or {}).items():
                self.acct_hourly.setdefault(aid, deque()).extend(sorted(t for t in ts if now - t <= 3600))
        for h in self.acct_hourly.values():
            h2 = sorted(h)
            h.clear()
            h.extend(h2)

    # ---------- 每小时回复名额、中间没人说话时最多连着发几条 ----------
    def quota_cfg(self, b: Bot, room: Room) -> tuple[float, float]:
        """（每小时几次, 最多攒几个）；每小时次数是 0 = 不设名额。"""
        n = int(self.S("reply_hourly", b, room) or 0)
        if n <= 0:
            return 0.0, 0.0
        burst = int(self.S("reply_hourly_burst", b, room) or 0)
        return float(n), float(burst if burst > 0 else n)

    def quota_tokens(self, b: Bot, room: Room, now: float | None = None) -> float:
        """这个账号在这个群里此刻手里有几个回复名额（含补回来的零头）。没设名额返回一个很大的数。"""
        n, cap = self.quota_cfg(b, room)
        if not n:
            return 1e9
        now = now or time.time()
        tokens, ts = room.quota.get(b.aid, [cap, now])
        return min(cap, tokens + max(0.0, now - ts) * n / 3600.0)

    def quota_wait(self, b: Bot, room: Room, now: float | None = None) -> float:
        """还要等几秒才有下一个名额（有就是 0）。"""
        n, _ = self.quota_cfg(b, room)
        if not n:
            return 0.0
        t = self.quota_tokens(b, room, now)
        return 0.0 if t >= 1 - 1e-9 else (1 - t) * 3600.0 / n

    def quota_use(self, b: Bot, room: Room):
        n, _ = self.quota_cfg(b, room)
        if not n:
            return
        now = time.time()
        room.quota[b.aid] = [max(0.0, self.quota_tokens(b, room, now) - 1), now]
        self.db.set_kv(f"quota:{room.key}", room.quota)

    def run_limit(self, b: Bot, room: Room | None) -> int:
        return int(self.S("split_max_parts", b, room) or 0)

    def run_left(self, b: Bot, room: Room | None, peer: str = "") -> int:
        """中间没人说话的话，她还能再连着发几条（设了 0 = 不限，返回一个很大的数）。私聊（room 是 None）按账号 × 对方（peer）各数各的。"""
        lim = self.run_limit(b, room)
        used = room.run.get(b.aid, 0) if room else self.dm_run.get(f"{b.aid}:{peer}", 0)
        return 10 ** 9 if lim <= 0 else max(0, lim - used)

    def run_add(self, b: Bot, room: Room | None, peer: str = ""):
        """她发出了一个气泡：她连着发的条数加一；群里对我们的别的账号来说，这是「有别人说话」，它们的条数清零。"""
        if room is None:
            k = f"{b.aid}:{peer}"
            self.dm_run[k] = self.dm_run.get(k, 0) + 1
            return
        room.run[b.aid] = room.run.get(b.aid, 0) + 1
        for a in [x for x in room.run if x != b.aid]:
            room.run.pop(a, None)

    def is_user(self, platform: str, uid: str) -> bool:
        """是不是固定的「用户」卡里的账号（用户本人）。"""
        card = self.people.user_card()
        return bool(card and uid and str(uid) in People.uids(card, platform))

    def user_free(self, b: Bot, room: Room, uid: str) -> bool:
        """用户本人叫她：这次回复不用名额，也不排队（设置里可以关）。"""
        return bool(self.S("reply_user_free", b, room) and self.is_user(room.platform, uid))

    def defer_reply(self, b: Bot, room: Room, mode: str, e: Entry, how, msg, group, wait: float):
        """名额用完：被@ / 被回复 / 被叫名字 / 戳一戳的不丢，排队到下一个名额补回来再回。"""
        st = room.q_wait.setdefault(b.aid, {"items": [], "task": None})
        st["items"].append((mode, e, how, msg, group))
        self.log("info", "reply", f"[{room.name}] {b.role['name']} 回复名额用完，约 {max(1, round(wait / 60))} 分钟后回（已排队）",
                 {"room": room.key, "mode": mode, "wait": int(wait)})
        if st["task"] is None or st["task"].done():
            st["task"] = asyncio.create_task(self._quota_waiter(room, b.aid))  # 不进 self.tasks：这个任务要等上好几分钟，别让 idle() 跟着等
            st["task"].add_done_callback(self._task_err)

    async def _quota_waiter(self, room: Room, aid: str):
        try:
            while True:
                st = room.q_wait.get(aid)
                bot = self.bots.get(aid)
                if not st or not st["items"] or not bot:
                    return
                wait = self.quota_wait(self.duty(bot, room), room)
                if wait > 0:
                    await asyncio.sleep(min(wait, QUOTA_POLL) + 0.05)  # 分段睡：中途改了设置、账号停了都能及时反应
                    continue
                items, st["items"] = st["items"], []
                room.queue.put_nowait(("job", self._reply_deferred(room, aid, items), None))
                return
        finally:
            st = room.q_wait.get(aid)
            if st and not st["items"]:
                room.q_wait.pop(aid, None)

    async def _reply_deferred(self, room: Room, aid: str, items: list):
        """排队等名额的回复到点了：期间被叫了好几次的合成一条（走现有的合并），写好时已过时的走现有的过时检查，期间被闭嘴、被禁言、被关掉的照现有逻辑作废。"""
        bot = self.bots.get(aid)
        if not bot:
            return
        b = self.duty(bot, room)
        ats = [it for it in items if it[0] == "at"]
        if not ats:
            await self.speak(b, room, "poke", items[-1][1], deferred=True)
            return
        es: list = []
        for it in ats:
            for x in (it[4] or [it[1]]):
                if all(x is not y for y in es):
                    es.append(x)
        last = ats[-1]
        e = last[1]
        e.note = e.notes.get(aid, "") if len(es) == 1 else ""
        try:
            await self.speak(b, room, "at", e, how=last[2], msg=last[3], group=es if len(es) > 1 else None, deferred=True)
        finally:
            e.note = ""

    def bump(self, b: Bot, room: Room, now: float):
        """记一次发言（插话 / 表情包）：每小时、每天、账号合计的计数，并存库。"""
        room.hourly.setdefault(b.aid, deque()).append(now)
        self.acct_hourly.setdefault(b.aid, deque()).append(now)
        day = time.strftime("%Y-%m-%d")
        d = room.daily.get(b.aid)
        room.daily[b.aid] = [day, (d[1] if d and d[0] == day else 0) + 1]
        log = room.pro_log.setdefault(b.aid, [])
        log.append(now)
        keep = now - 86400  # 至少留最近 24 小时：每小时和「今天」的计数重启后都接得上，不按固定条数截断
        room.pro_log[b.aid] = [t for t in log if t >= keep]
        self.db.set_kv(f"pro:{room.key}", room.pro_log)

    def dm_hist(self, aid: str, uid: str, before: float | None = None) -> list:
        """私聊的对话历史（给模型的）：内存里没有（刚重启）就从 messages 表回填最近 max_history 轮。before：只取这个时刻之前的，免得把正在回的这条算两遍。"""
        key = f"{aid}:{uid}"
        hist = self.private_hist.get(key)
        if hist is None:
            hist = self.private_hist[key] = []
            bot = self.bots.get(aid)
            n = int(self.S("max_history", bot) or 10) * 2 if bot else 20
            room = self.dm_key(aid, uid)
            rows = self.db.rows("SELECT ts, text, kind FROM messages WHERE room=? AND ts>? AND ts<? AND kind IN ('human','bot') ORDER BY id DESC LIMIT ?",
                                (room, self.ctx_floor(room), before or 9e18, n))[::-1]
            hist += [{"role": "user" if m["kind"] == "human" else "assistant", "content": m["text"]} for m in rows]
        return hist

    # ---------- 群禁言：认出来，禁言期间这个号在这个群里不为发言做任何事 ----------
    # 状态按（账号, 群）各算各的，和平台无关：渠道认出禁言后调 ban_update。记录里的字段：
    #   member_until  这个号被单独禁言到什么时候（时间戳）；None = 没有
    #   all, role     全员禁言开着没有，以及这个号在群里的身份（群主 / 管理员不受全员禁言影响）
    #   unknown       只知道发送被拒、报的是禁言错误，不知道到什么时候（要靠复查确认）
    BAN_RECHECK_FIRST, BAN_RECHECK_MAX = 300, 1800  # 结束时间未知的禁言：第一次复查隔 5 分钟，之后翻倍，最多隔 30 分钟
    BAN_UNKNOWN_GIVE_UP = 48 * 3600  # 一直查不出结果（查询出错）超过这么久就不再当作被禁言
    BAN_UNQUERYABLE_GIVE_UP = 6 * 3600  # 平台根本查不了的：这么久之后放开，让下一次正常发言去验证

    @staticmethod
    def ban_active(rec: dict, now: float) -> bool:
        return bool(rec.get("unknown") or (rec.get("all") and rec.get("role") not in ("owner", "admin")) or (rec.get("member_until") or 0) > now)

    @staticmethod
    def ban_end(rec: dict, now: float) -> float | None:
        """禁言到几点；None = 结束时间未知。"""
        if rec.get("unknown") or (rec.get("all") and rec.get("role") not in ("owner", "admin")):
            return None
        mu = rec.get("member_until")
        return None if mu and mu > now + 365 * 86400 else mu  # 平台给的是「无限期」（Telegram 的 until_date 为 0）：当作结束时间未知

    def muted_in(self, aid: str, room_key: str) -> bool:
        """这个号此刻在这个群里是不是被禁言。到期的在这里顺手清掉。"""
        rec = self.bans.get((aid, room_key))
        if not rec:
            return False
        if self.ban_active(rec, time.time()):
            return True
        self.ban_update(aid, room_key, member_until=None, all=False, unknown=False)  # 到点了：恢复
        return False

    def ban_update(self, aid: str, room_key: str, **fields):
        """更新（账号, 群）的禁言状态。变成被禁言时在面板总览留一条未读通知（同一次禁言只留一条）；变成没事了就清掉记录。"""
        now = time.time()
        key = (aid, room_key)
        old = self.bans.get(key)
        was = bool(old and self.ban_active(old, now))
        rec = dict(old or {"since": now, "tries": 0, "checked": now})
        rec.update(fields)
        if self.ban_active(rec, now):
            if not was:
                rec.update(since=now, tries=0, checked=now)
                b = self.bots.get(aid)
                room = self.rooms.get(room_key)
                end = self.ban_end(rec, now)
                acc, chat = self.db.doc("account", aid) or {}, self.db.doc("chat", room_key) or {}
                self.add_notice(f"账号「{(b.channel.self_name if b and b.channel else '') or acc.get('name') or acc.get('qq') or aid}」在群「{(room.name if room else '') or chat.get('name') or room_key}」被禁言，"
                                + (f"到 {time.strftime('%m-%d %H:%M', time.localtime(end))}" if end else "结束时间未知") + "：禁言期间她在这个群里不发言，解除后自动恢复。")
                self.log("warn", "system", f"[{room.name if room else room_key}] {aid} 被禁言（{'到 ' + time.strftime('%m-%d %H:%M', time.localtime(end)) if end else '结束时间未知'}）")
            self.bans[key] = rec
        else:
            if not old:
                return
            self.bans.pop(key, None)
            if was or old:
                room = self.rooms.get(room_key)
                self.log("info", "system", f"[{room.name if room else room_key}] {aid} 的禁言解除了，恢复发言")
        self.save_bans()
        self.emit("ban", {"account": aid, "room": room_key, "active": key in self.bans})

    def save_bans(self):
        self.db.set_kv("bans", {f"{a}|{r}": rec for (a, r), rec in self.bans.items()})

    def restore_bans(self):
        """启动时读回禁言状态，已经到期的清掉；结束时间未知的，等账号上线后复查。"""
        now = time.time()
        for k, rec in (self.db.kv("bans") or {}).items():
            aid, _, rk = k.partition("|")
            if self.ban_active(rec, now):
                rec["checked"] = 0
                self.bans[(aid, rk)] = rec
        self.save_bans()

    def ban_view(self) -> list[dict]:
        now = time.time()
        out = []
        for (aid, rk), rec in self.bans.items():
            if not self.ban_active(rec, now):
                continue
            b, room = self.bots.get(aid), self.rooms.get(rk)
            acc, chat = self.db.doc("account", aid) or {}, self.db.doc("chat", rk) or {}
            out.append({"account": aid, "account_name": (b.channel.self_name if b and b.channel else "") or acc.get("name") or acc.get("qq") or aid, "room": rk,
                        "room_name": (room.name if room else "") or chat.get("name") or rk, "until": self.ban_end(rec, now), "since": rec.get("since")})
        return out

    async def recheck_bans(self):
        """结束时间未知的禁言：隔一阵主动查一次（用渠道的查询接口，不往群里发任何东西），确认解禁了就恢复。"""
        now = time.time()
        for (aid, rk), rec in list(self.bans.items()):
            if self.ban_end(rec, now) is not None:
                continue
            if now - rec.get("checked", 0) < min(self.BAN_RECHECK_FIRST * 2 ** rec.get("tries", 0), self.BAN_RECHECK_MAX):
                continue
            b = self.bots.get(aid)
            if not (b and b.online):
                continue
            rec["checked"], rec["tries"] = now, rec.get("tries", 0) + 1
            platform, _, chat_id = rk.partition(":")
            try:
                res = await b.channel.query_ban(chat_id, fresh=True)
            except Exception as ex:
                self.log("debug", "system", f"[{rk}] 查禁言状态没成功：{ex}")
                if now - rec.get("since", now) > self.BAN_UNKNOWN_GIVE_UP:
                    self.ban_update(aid, rk, unknown=False, all=False, member_until=None)
                continue
            if res is None:  # 这个平台查不了
                if now - rec.get("since", now) > self.BAN_UNQUERYABLE_GIVE_UP:
                    self.ban_update(aid, rk, unknown=False, all=False, member_until=None)
                continue
            self.ban_update(aid, rk, unknown=False, **res)
        self.save_bans()

    async def scan_bans(self, b: Bot, chat_ids: list[str]):
        """账号上线时：把这个号所在的各群查一遍有没有被禁言（含全员禁言），查出来的存下来；以前存的、现在已经没事的清掉。"""
        for cid in chat_ids:
            try:
                res = await b.channel.query_ban(cid)
            except Exception as ex:
                self.log("debug", "system", f"[{b.platform}:{cid}] 查禁言状态没成功：{ex}")
                continue
            if res is None:
                return
            rk = f"{b.platform}:{cid}"
            if rk in {r for (a, r) in self.bans if a == b.aid} or self.ban_active({"member_until": res.get("member_until"), "all": res.get("all"), "role": res.get("role")}, time.time()):
                self.ban_update(b.aid, rk, unknown=False, **res)
            await asyncio.sleep(0.05)

    def duty(self, b: Bot, room: "Room | str | None") -> Bot:
        """这个账号此刻在这个群（或私聊）里的在岗视图：角色由排班规则和临时覆盖决定。"""
        chat = room.key if isinstance(room, Room) else room
        rid, src = self.duty_rules.resolve(b.account, chat)
        o = self.duty_rules.override(b.aid, chat) if chat else None
        v = Bot(self, b.account, b.channel, rid or "", (o or {}).get("persona"))
        v.duty_src = src
        return v

    def register_chat(self, account_id: str, platform: str, chat_id: str, name: str):
        self.room(platform, chat_id, name).accounts.add(account_id)

    def display_name(self, platform: str, uid: str, name: str, room: "Room | None" = None) -> tuple[str, dict | None]:
        p = self.people.find(platform, uid)
        if p and (p.get("name") or "").strip():
            return f"{name}〔{p['name']}本人〕", p
        b = self.bot_uids(platform).get(uid)
        if b:
            return f"{name}〔{self.duty(b, room).role['name']}本人〕", None
        return name, None

    def blocked(self, text: str, bot=None, room=None, platform=None) -> bool:
        return any(w and w in text for w in self.S("blocked_words", bot, room, platform) or [])

    async def on_message(self, msg: Msg):
        bot = self.bots.get(msg.account_id)
        if not bot:
            return
        if msg.user_id and msg.user_id == bot.uid:
            return
        if str(msg.user_id) in [str(x) for x in self.S("blacklist", platform=msg.platform) or []]:
            return
        if msg.chat_type == "private":
            if msg.user_id in self.bot_uids(msg.platform):
                return
            self.dm_run.pop(f"{bot.aid}:{msg.user_id}", None)  # 对方说话：她在这个私聊里连着发的条数从 0 重新数
            self.spawn(self.handle_private(bot, msg))
            return
        room = self.room(msg.platform, msg.chat_id, msg.chat_name)
        room.accounts.add(bot.aid)
        if msg.poke_target:
            if msg.poke_target in self.bot_uids(msg.platform):
                if self.S("poke_counts", room=room):
                    room.run.clear()  # 有人戳她也算有人说话
                room.queue.put_nowait(("poke", None, msg))
            return
        if msg.user_id in self.bot_uids(msg.platform):
            return  # 自己这边机器人发的，发送时已记进上下文
        key = msg.dedup or f"{msg.user_id}|{msg.text}|{int(msg.ts // 15)}"
        e = room.seen.get(key)
        if e:
            e.ids[bot.aid] = msg.msg_id
            return
        if room.platform == "wechat":
            self.wechat_addressing(bot, room, msg)
        text = msg.text
        if msg.images:
            text = (text + " " + await self.describe_images(bot, room, msg)).strip()
        if msg.files:
            text = (text + " " + "".join(f"[文件：{f.get('name', '文件')}]" for f in msg.files[:3])).strip()
        name, _ = self.display_name(msg.platform, msg.user_id, msg.user_name, room)
        e = Entry(msg.user_id, name, text[: self.S("context_max_chars", room=room)], ids={bot.aid: msg.msg_id},
                  at_ids=list(msg.at_ids), reply_uid=str(msg.reply_to_uid or ""), at_uids=[str(u) for u in msg.at_ids])
        learn = [u for u in msg.images if not (msg.platform == "wechat" and u in msg.emotes)]  # 微信的表情只看不学（微信里学来的表情能不能发出去另说）；QQ 的表情图照旧交给学表情包
        if (learn or msg.stickers) and not self.is_bot_person(msg.platform, msg.user_id):
            self.spawn(self.stickers.observe(bot, room, learn + list(msg.stickers), msg.user_id))
        room.seen[key] = e
        if msg.files or msg.reply_to_id or self.called_bot(room, msg):
            await self.handle_files(bot, room, msg, e)
        while len(room.seen) > 500:
            room.seen.popitem(last=False)
        self.push(room, e, "human", bot.aid, msg.msg_id)
        e.cmd_task = self.spawn(self.intake_commands(room, e, msg))  # 管理指令、自定义指令：收到就办，不排队、不受「两次回复最小间隔」限制
        room.queue.put_nowait(("human", e, msg))

    async def intake_commands(self, room: Room, e: Entry, msg: Msg):
        """消息进来的这一刻就把管理指令（闭嘴、恢复、换角色、换回来、插话开关、力度、状态、撤回、忘掉）和自定义指令办了，确认回复也立刻发。
        不排进回复队列，所以队列里压着别的消息、或者正在等「两次回复最小间隔」时，指令照样马上生效；
        之后排队处理的消息（包括队列里更早压着的 @）就都由换完之后的角色来写。办完的结果记在 e.pre_cmd 里，排到这条消息时不再重办。"""
        pre = e.pre_cmd = {"done": set(), "consumed": False, "handled": set()}
        try:
            if self.blocked(e.text, room=room):
                return
            bots = self.room_bots(room)
            sender_bot = self.is_bot_person(room.platform, e.uid)
            done, consumed = await self.run_custom(room, bots, e, msg, sender_bot)
            pre["done"], pre["consumed"] = set(done), consumed
            pre["handled"] = set(done)
            for b in bots:
                if not self.addressed(b, e, msg, room) or b.aid in done:
                    continue
                if sender_bot and not self.bot_may_reply(b, room, e):
                    continue
                cmd = commands.parse(e.text)
                if cmd and cmd[0] == "persona" and not self.switch_target(cmd[1])[0][1]:
                    cmd = None  # 「换××」里的××既不是角色也不是人设：当普通聊天
                if cmd:
                    pre["handled"].add(b.aid)
                    await self.run_command(b, room, e, msg, *cmd)
        except Exception as ex:
            self.log("error", "command", f"[{room.name or room.key}] 办指令出错 {type(ex).__name__}: {ex}")

    # ---------- 记住、提醒 ----------
    def strip_call(self, b: Bot, room: Room | None, text: str) -> str:
        """去掉开头叫她的名字（唤醒词）和标点，剩下才是要办的话。"""
        t = text.strip()
        for w in sorted([w for w in self.S("wake_words", b, room) or [] if w], key=len, reverse=True):
            if t.startswith(w):
                t = t[len(w):]
                break
        return t.lstrip(" ，,、:：~～").strip()

    async def act(self, b: Bot, room: Room | None, e: Entry, platform: str, chat_type: str, chat_id: str) -> str:
        """对她说的话里有没有「记住……」或提醒：有就办（纯代码，不花模型调用），返回一段交代给她回复用的说明。"""
        if self.S("shadow", b, room):
            return ""
        req = remind.parse_request(self.strip_call(b, room, e.text))
        if not req:
            return ""
        chat = room.key if room else None
        person = self.people.find(platform, e.uid)
        name = person["name"] if person else e.name.split("〔")[0]
        if req["kind"] == "remember":
            if not self.S("remember_on", b, room):
                return ""
            if not self.memory.enabled(platform, chat):
                return "【系统说明】这里没开长期记忆，没法记。用你自己的口吻告诉他一声。"
            dm = None if room else self.dm_key(b.aid, e.uid)  # 私聊里记的只在这个账号和这个人的私聊里用
            sp = self.memory.space("person", b.rid, platform, chat, dm)
            subject = subject_key(platform, e.uid, person)
            if not self.db.x("SELECT 1 FROM memories WHERE space=? AND kind='person' AND subject=? AND source='told' AND content=?",
                             (sp, subject, req["text"])).fetchone():
                self.memory.add(sp, "person", subject, name, req["text"], "told", [{"name": e.name, "text": e.text[:200]}], chat or dm, role=b.rid, status="active")
                self.spawn(self.memory.flush_index())
            self.log("info", "memory", f"{name} 要求记住：{req['text'][:40]}")
            return f"【刚办完的事】已把「{req['text']}」记进你对{name}的备忘（来源：他本人要求记住）。用你自己的口吻自然地应一句，不要用系统口吻。"
        if not self.S("remind_on", b, room):
            return ""
        now = time.time()
        due = req["due"]
        if due is None or due < now + 5 or due > now + 366 * 86400:
            return "【系统说明】他想让你提醒他，但没说清几点（或时间不对）。用你自己的口吻请他说个具体时间，比如「20 分钟后」「明天 8:30」。"
        if self.reminders.count(platform, e.uid) >= int(self.settings.g("remind_max") or 5):
            return f"【系统说明】他已经挂了 {self.settings.g('remind_max')} 条提醒，到上限了，先不加新的。用你自己的口吻告诉他，等有的提醒过了再来。"
        task = req["text"] or "（没说具体什么事）"
        self.reminders.add(platform, chat_type, chat_id, b.aid, e.uid, name, task, due)
        self.log("info", "system", f"设了提醒：{name} {remind.when_text(due)} 「{task[:30]}」")
        return f"【刚办完的事】已设好提醒：{remind.when_text(due)} 提醒他「{task}」。用你自己的口吻应一句，说一下几点会提醒他。"

    async def fire_reminders(self):
        now = time.time()
        for r in self.reminders.due(now):
            if now - r["due"] > 3600:
                self.reminders.mark(r["id"], "missed")
                continue
            if r["chat_type"] == "group":
                room = self.rooms.get(f"{r['platform']}:{r['chat_id']}")
                cands = self.room_bots(room) if room else []
                b = next((x for x in cands if x.aid == r["account_id"]), None) or (cands[0] if cands else None)
                if not b or self.muted_in(b.aid, room.key):
                    continue  # 现在没有机器人在线、或这个号在这个群被禁言：留着，能说了（一小时内）补发
                self.reminders.mark(r["id"], "fired")
                room.queue.put_nowait(("job", self.speak(b, room, "remind", Entry(r["user_id"], r["user_name"], ""), brief=r["text"],
                                                         at=[(r["user_id"], r["user_name"])]), None))
            else:
                bot = self.bots.get(r["account_id"])
                if not (bot and bot.online):
                    continue
                self.reminders.mark(r["id"], "fired")
                self.spawn(self.remind_private(self.duty(bot, None), r))

    async def remind_private(self, b: Bot, r: dict):
        person = self.people.find(r["platform"], r["user_id"])
        try:
            system = await self.system_prompt(b, None, person, r["user_id"], r["user_name"])
            msgs = [{"role": "system", "content": system},
                    {"role": "user", "content": f"（到点了：{r['user_name']} 之前让你提醒他：「{r['text']}」。用你自己的口吻提醒他一句，要把提醒的事说清楚。直接输出要发送的话。）"}]
            reply = self.postprocess(b, None, await self.llm.chat("main", msgs, b.rid, b.platform, None))
        except Exception as ex:
            self.log("error", "model", f"私聊提醒 {r['user_name']} 生成失败：{ex}")
            return
        if not reply:
            return
        if self.S("shadow", b):
            self.log("info", "shadow", f"[私聊 {r['user_name']}] {b.role['name']}（影子）本来会提醒：{reply}")
            return
        await self.send_text(b, "private", r["user_id"], reply, None, None)
        self.log("info", "reply", f"[私聊 {r['user_name']}] {b.role['name']}（提醒）：{reply[:80]}")

    # ---------- 读文件 ----------
    def called_bot(self, room: Room, msg: Msg) -> bool:
        uids = self.bot_uids(room.platform)
        return msg.reply_to_uid in uids or any(str(u) in uids for u in msg.at_ids)

    async def handle_files(self, bot: Bot, room: Room, msg: Msg, e: Entry):
        """文件消息：记下来（以后有人回复它时才读）；该读的现在读，抽出的文字挂在这一条上给这次回复用。"""
        if msg.files:
            room.files[msg.dedup or f"{msg.user_id}|{msg.msg_id}"] = {"ids": e.ids, "files": msg.files, "entry": e}
            while len(room.files) > 50:
                room.files.popitem(last=False)
        if not self.S("file_read", bot, room):
            return
        todo = list(msg.files)
        if not todo and msg.reply_to_id:  # 回复的是一条文件消息
            for rec in reversed(room.files.values()):
                if msg.reply_to_id in [str(v) for v in rec["ids"].values()]:
                    todo = rec["files"]
                    break
        if not todo and self.called_bot(room, msg):  # @她但没回复文件：取这个人最近发的文件（15 分钟内）
            for rec in reversed(room.files.values()):
                if rec["entry"].uid == msg.user_id and time.time() - rec["entry"].ts < 900:
                    todo = rec["files"]
                    break
        if not todo:
            return
        auto = self.S("file_mode", bot, room) == "auto"
        if not (auto or self.called_bot(room, msg) or msg.chat_type == "private"):
            return
        blocks = []
        for f in todo[:3]:
            text, note = await self.read_file(bot, room, f)
            blocks.append(f"【文件《{f.get('name', '文件')}》的内容】\n{text}" if text else f"【文件《{f.get('name', '文件')}》没读成：{note}】")
        e.attach = "\n\n".join(blocks)

    async def read_file(self, bot: Bot, room: Room, f: dict) -> tuple[str, str]:
        """下载 → 抽文字 → 删原文件 → 切片存记忆。返回（文字, 失败原因）。"""
        name = f.get("name") or "文件"
        kind = files.kind_of(name)
        allowed = set(self.S("file_formats", bot, room) or [])
        if not kind or kind not in allowed:
            return "", "这种格式没有开放读取"
        limit = int(self.S("file_max_mb", bot, room) or 10) << 20
        if f.get("size") and f["size"] > limit:
            return "", f"文件太大（上限 {limit >> 20} MB）"
        try:
            raw = await bot.channel.fetch_file(f, limit)
        except Exception as ex:
            self.log("warn", "file", f"下载文件失败：{ex}")
            raw = None
        if not raw:
            return "", "下载不了（或超过大小上限）"
        try:
            if kind == "image":
                text = await self.describe_bytes(bot, room, raw)
            else:
                text = await asyncio.get_running_loop().run_in_executor(None, files.extract, raw, name)
        except files.FileError as ex:
            return "", str(ex)
        keep = int(self.S("file_keep_days", bot, room) or 0)
        if keep:
            files.keep_file(raw, name)
            files.enforce_cache(int(self.S("file_cache_mb", bot, room) or 200), keep)
        fid = hashlib.sha1(raw).hexdigest()[:12]
        del raw  # 原文件不留（没设保留天数时从没落过盘）
        text, cut = files.clip_tokens(text, int(self.S("file_max_tokens", bot, room) or 6000))
        space = self.memory.space("doc", bot.rid, room.platform, room.key)  # 文件片段按 doc 种类选空间
        if not self.db.x("SELECT 1 FROM memories WHERE subject=? AND space=? LIMIT 1", (f"file:{fid}", space)).fetchone():
            for ch in files.chunks(text)[:200]:
                self.memory.add(space, "doc", f"file:{fid}", name, ch, "file", room=room.key, status="active", role=bot.rid)
            self.spawn(self.memory.flush_index())
        self.log("info", "file", f"[{room.name}] 读了文件《{name}》约 {files.est_tokens(text)} tokens" + ("（超出上限，后面没读）" if cut else ""))
        return text + ("\n（文件后面还有内容，超出单次读取上限，没读）" if cut else ""), ""

    def push(self, room: Room, e: Entry, kind: str, aid: str = "", msg_id: str = "", role_id: str = ""):
        room.seq += 1
        e.seq = room.seq
        if kind != "shadow":
            room.ctx.append(e)
        if kind == "human":
            room.run.clear()  # 有人说话（被@、被回复也是）：她们连着发的条数都从 0 重新数
        cur = self.db.x("INSERT INTO messages (room, ts, account_id, msg_id, user_id, name, text, role_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                        (room.key, e.ts, aid, msg_id, e.uid, e.name, e.text, role_id, kind))
        e.dbid = cur.lastrowid
        self.emit("msg", {"room": room.key, "room_name": room.name, "id": cur.lastrowid, "ts": e.ts, "name": e.name,
                          "text": e.text, "kind": kind, "uid": e.uid, "account": aid, "role": role_id})
        if kind != "shadow":
            room.mem_count += 1
            self.mem_tick(room)

    def reply_gap_left(self, b: Bot, room: Room) -> float:
        """距离「两次回复最小间隔」到点还有几秒（没设或已到点是 0）。"""
        gap = int(self.S("reply_min_gap", b, room) or 0)
        return max(0.0, room.last_reply.get(b.aid, 0) + gap - time.time()) if gap > 0 else 0.0

    def gap_wait(self, room: Room, e: Entry, msg: Msg) -> float:
        """这条消息要等多久才能回：被叫到的机器人里，最早能开口的那个还要等几秒。没人被叫到（或是指令）不用等。"""
        pre = e.pre_cmd or {}
        if commands.parse(e.text) or self.blocked(e.text, room=room) or pre.get("consumed"):
            return 0.0  # 指令（含自定义指令）已经在消息进来时办完了，不用等
        lefts = [self.reply_gap_left(b, room) for b in self.room_bots(room)
                 if self.addressed(b, e, msg, room) and self.can_talk(b, room) and not (b.channel and b.channel.reply_only)
                 and b.aid not in pre.get("handled", ())]
        return min(lefts) if lefts else 0.0

    async def _worker(self, room: Room):
        while True:
            kind, e, msg = await room.queue.get()
            room.busy = True
            try:
                if kind == "human":
                    if e.cmd_task is not None:
                        await e.cmd_task  # 先等这条消息的指令办完（办完才知道它算不算「被叫到」）
                    wait = self.gap_wait(room, e, msg)
                    if wait > 0:  # 间隔没到：消息留在队列里，到点再一起处理（期间同一个机器人被叫了好几次的，走合并）
                        self.log("info", "reply", f"[{room.name}] 离上一次回复不到 {int(self.S('reply_min_gap', room=room))} 秒，{int(wait) + 1} 秒后再回（消息已排队）")
                        await asyncio.sleep(wait)
                    batch = [(e, msg)]
                    while self.S("merge_at", room=room) and not room.queue.empty() and room.queue._queue[0][0] == "human":
                        _, e2, m2 = room.queue.get_nowait()
                        batch.append((e2, m2))
                    await self.process_batch(room, batch)
                elif kind == "poke":
                    b = self.bot_uids(room.platform).get(msg.poke_target)
                    b = b and self.duty(b, room)
                    if b and self.can_talk(b, room) and self.S("poke_reply", b, room):
                        name, _ = self.display_name(room.platform, msg.user_id, msg.user_name)
                        trig = Entry(msg.user_id, name, "（戳了戳你）")
                        await self.speak(b, room, "poke", trig, free=self.user_free(b, room, msg.user_id))
                elif kind == "force":
                    await self.speak(msg, room, "force", e)
                elif kind == "job":
                    await e
            except Exception as ex:
                self.log("error", "system", f"[{room.name or room.key}] 处理出错 {type(ex).__name__}: {ex}")
            finally:
                room.busy = False

    async def idle(self):
        """等所有群的队列处理完（测试、导入用）。"""
        while any(r.busy or not r.queue.empty() for r in self.rooms.values()) or self.tasks:
            await asyncio.sleep(0.01)

    def close(self):
        for r in self.rooms.values():
            if r.worker:
                r.worker.cancel()
            for st in r.q_wait.values():
                if st.get("task"):
                    st["task"].cancel()
            if r.pro_wait.get("task"):
                r.pro_wait["task"].cancel()

    # ---------- 群消息主流程 ----------
    def room_bots(self, room: Room) -> list[Bot]:
        """群里在线的机器人，按「此刻在岗的角色」（同群协调、姐妹关系都按这个算）。"""
        return [self.duty(b, room) for aid, b in self.bots.items()
                if aid in room.accounts and b.platform == room.platform and b.online]

    def can_talk(self, b: Bot, room: Room) -> bool:
        if not self.S("enabled", b, room):
            return False
        if self.S("only_listed_chats", b, room) and not (self.chat_doc(room) or {}).get("listed"):
            return False
        if room.mute.get(b.aid, 0) > time.time():
            return False
        if self.muted_in(b.aid, room.key):  # 被群禁言：为了往这个群发东西的事一律不做（守门员、写回复、插话、冷场、定时、提醒、换班招呼）
            return False
        if self.S("quiet_mode", b, room) == "silent" and in_hours(self.S("quiet_hours", b, room)):
            return False
        return True

    def addressed(self, b: Bot, e: Entry, msg: Msg | None, room: Room | None = None) -> str | None:
        """被@、被回复、被叫名字：返回说法（@了你 / 回复了你 / 叫了你），否则 None。"""
        if b.uid and b.uid in [str(x) for x in e.at_ids]:
            return "@了你"
        if msg and b.uid and msg.reply_to_uid == b.uid:
            return "回复了你"
        if any(w and w in e.text for w in self.S("wake_words", b, room) or []):
            return "叫了你"
        return None

    def eager_level(self, b: Bot, room: Room) -> int:
        """此刻的插话力度：设定的力度，开了自动调节就再加上这个群里的调整（在设定的上下档数之内）。"""
        base = int(self.S("eagerness", b, room) or 3)
        if not self.S("eager_auto", b, room):
            return base
        return self.tune(room, b.aid).level(base, int(self.S("eager_auto_down", b, room)), int(self.S("eager_auto_up", b, room)))

    def tune(self, room: Room, aid: str) -> pacing.Tune:
        k = f"{room.key}|{aid}"
        t = self.tunes.get(k)
        if t is None:
            d = self.db.kv(f"tune:{k}")
            t = self.tunes[k] = pacing.Tune(**d) if d else pacing.Tune()
        return t

    def save_tune(self, room: Room, aid: str):
        self.db.set_kv(f"tune:{room.key}|{aid}", self.tune(room, aid).dump())

    def tune_info(self, b: Bot, room: Room) -> dict:
        t = self.tune(room, b.aid)
        base = int(self.S("eagerness", b, room) or 3)
        return {"base": base, "level": self.eager_level(b, room), "auto": bool(self.S("eager_auto", b, room)),
                "adj": round(t.adj, 2), "history": [{"ts": h[0], "feedback": h[1], "delta": h[2], "reason": h[3]} for h in t.hist]}

    def count(self, name: str, role: str | None, room_key: str, n: int = 1):
        self.db.x("""INSERT INTO counters (day, name, role_id, room, n) VALUES (?,?,?,?,?)
                     ON CONFLICT (day, name, role_id, room) DO UPDATE SET n=n+excluded.n""", (time.strftime("%Y-%m-%d"), name, role or "", room_key, n))

    def share_blocked(self, b: Bot, room: Room) -> bool:
        S = lambda k: self.S(k, b, room)
        return pacing.share_blocked(room.ctx, int(S("share_window")), S("share_mode"), int(S("share_max_count")), int(S("share_max")),
                                    bool(S("share_min_on")), int(S("share_min_n")))

    def pro_state(self, b: Bot, room: Room) -> tuple[bool, bool]:
        """主动插话的两层条件：（硬条件：开着插话、不在静默时段、用量没超；软条件：冷却、各种上限、占比、被叫停）。"""
        if not self.S("proactive", b, room) or (b.channel and b.channel.reply_only):
            return False, False
        if self.S("quiet_hours", b, room) and in_hours(self.S("quiet_hours", b, room)):
            return False, False
        if self.llm.over_limit(b.rid):
            return False, False
        now = time.time()
        _, mult = EAGER_PARAMS.get(self.eager_level(b, room), (1.0, 1.0))
        if now - room.last_pro.get(b.aid, 0) < self.S("cooldown_seconds", b, room) * mult:
            return True, False
        if self.reply_gap_left(b, room) > 0:  # 两次回复最小间隔：和插话冷却各算各的，取更严的
            return True, False
        if self.quota_wait(b, room) > 0 or self.run_left(b, room) <= 0:  # 每小时回复名额用完了 / 中间没人说话已经连着发满了：不问守门员，不插
            return True, False
        # 每小时上限按整个账号、所有群合计（风控按账号算）；每个群另有自己的每小时、每天上限。叠加，取最严
        h = self.acct_hourly.setdefault(b.aid, deque())
        while h and now - h[0] > 3600:
            h.popleft()
        if len(h) >= self.S("hourly_limit", b, room):
            return True, False
        rh = room.hourly.setdefault(b.aid, deque())
        while rh and now - rh[0] > 3600:
            rh.popleft()
        per_chat = self.S("hourly_limit_chat", b, room)
        if per_chat and len(rh) >= per_chat:
            return True, False
        day = time.strftime("%Y-%m-%d")
        d = room.daily.get(b.aid)
        if d and d[0] == day and d[1] >= self.S("daily_limit", b, room):
            return True, False
        if now < room.pause_until:  # 有人说了「别说了」
            return True, False
        if self.S("share_limit_on", b, room) and self.share_blocked(b, room):
            return True, False
        return True, True

    def sample_ok(self, b: Bot, room: Room) -> bool:
        prob, _ = EAGER_PARAMS.get(self.eager_level(b, room), (1.0, 1.0))
        return random.random() < prob

    def proactive_ok(self, b: Bot, room: Room, sample: bool = True) -> bool:
        hard, soft = self.pro_state(b, room)
        return hard and soft and (not sample or self.sample_ok(b, room))

    def observing(self, b: Bot, room: Room) -> bool:
        """她刚说过话：即使这次不打算插话，也为「别说了」和上一次插话的反馈问一次守门员（同一次调用，只多这一次）。"""
        return bool((self.S("stop_on", b, room) or self.S("eager_auto", b, room)) and time.time() - room.last_spoke.get(b.aid, 0) < OBSERVE_SECS)

    def is_bot_person(self, platform: str, uid: str) -> bool:
        p = self.people.find(platform, uid)
        return bool(p and p.get("bot"))

    def resume(self, room: Room):
        if room.pause_until > time.time():
            room.pause_until = 0.0
            self.save_room_state(room)
            self.log("info", "reply", f"[{room.name}] 有人叫了她，提前恢复主动插话")

    def on_stop(self, room: Room, e: Entry):
        now = time.time()
        if now - room.last_stop < 30:
            return
        room.last_stop = now
        any_bot = next(iter(self.room_bots(room)), None)
        base = int(self.S("stop_minutes", any_bot, room) or 30)
        mins, room.stop_hist = pacing.stop_minutes(room.stop_hist, now, base, int(self.S("stop_max_minutes", any_bot, room) or 480))
        room.pause_until = now + mins * 60
        self.save_room_state(room)
        self.log("info", "reply", f"[{room.name}] {e.name} 叫停：该群机器人暂停主动插话 {mins} 分钟", {"room": room.key, "minutes": mins})
        for b in self.room_bots(room):
            pend = room.pro_pending.pop(b.aid, None)
            if pend and self.S("eager_auto", b, room):
                self.tune(room, b.aid).apply("annoyed", int(self.S("eager_auto_down", b, room)), int(self.S("eager_auto_up", b, room)), "被叫停")
                self.save_tune(room, b.aid)

    def on_feedback(self, b: Bot, room: Room, fb: str, pend: dict):
        down, up = int(self.S("eager_auto_down", b, room)), int(self.S("eager_auto_up", b, room))
        age = time.time() - pend["ts"]
        if fb in pacing.FEEDBACK_STEP:
            reason = pacing.FEEDBACK_LABEL[fb] + f"：「{pend['text'][:20]}」"
        elif age > 900 and pend["after"] >= 1:  # 过了一阵，群里还在聊别的，没人接她的话
            fb, reason = "ignored", f"过了一阵没人接：「{pend['text'][:20]}」"
        else:
            return
        room.pro_pending.pop(b.aid, None)
        self.tune(room, b.aid).apply(fb, down, up, reason)
        self.save_tune(room, b.aid)
        self.log("info", "gatekeeper", f"[{room.name}] {b.role['name']} 插话力度调整：{reason} → 现在是 {EAGER_LABEL.get(self.eager_level(b, room))}",
                 {"room": room.key, "role": b.rid, "feedback": fb})

    def mark_proactive(self, b: Bot, room: Room):
        now = time.time()
        room.last_pro[b.aid] = now
        self.bump(b, room, now)

    def bot_may_reply(self, b: Bot, room: Room, e: Entry) -> bool:
        """别的机器人 @ 她或回复她：按设置不理、最多接 N 轮（和同一个机器人连续互接，真人开口后重新数）、或照常回复。"""
        mode = self.S("bot_at_mode", b, room)
        if mode == "ignore":
            return False
        if mode == "normal":
            return True
        return room.bot_rounds.get((b.aid, e.uid), 0) < int(self.S("bot_at_rounds", b, room) or 1)

    async def run_custom(self, room: Room, bots: list, e: Entry, msg: Msg, sender_bot: bool) -> tuple[set, bool]:
        """自定义指令：按列表顺序找第一条命中的规则并执行。返回（已经被它处理的机器人账号，这条消息是否已被消耗掉——不用再进守门员）。"""
        if sender_bot:
            return set(), False
        person = self.people.find(room.platform, msg.user_id)
        now = time.time()
        for r in customcmd.rules(self.db):
            if not r.get("on", True):
                continue
            who = []
            for b in bots:
                if not customcmd.in_scope(r, room.platform, b.rid, room.key):
                    continue
                if r.get("at"):
                    if not self.addressed(b, e, msg, room) or not customcmd.matches(r, self.strip_call(b, room, e.text)):
                        continue
                elif not customcmd.matches(r, e.text) or (self.S("quiet_hours", b, room) and in_hours(self.S("quiet_hours", b, room))):
                    continue
                if r["action"] != "builtin" and not self.can_talk(b, room):
                    continue
                who.append(b)
                if not r.get("at"):
                    break  # 没有 @ 的规则只由一个机器人回
            if not who:
                continue
            perm = r.get("perm") or {"mode": "all"}
            is_owner = bool(person and person.get("admin"))
            if perm.get("mode") == "follow" and r["action"] == "builtin":  # 别名：权限就是这条内置指令现在的设置
                ok = commands.allowed(r["builtin"], self.S("cmd_perms", who[0]), is_owner=is_owner, person=person, sender_role=msg.sender_role)
            else:
                ok = commands.allowed("custom", {"custom": perm}, is_owner=is_owner, person=person, sender_role=msg.sender_role)
            if not ok:
                continue  # 没权限：当没触发，接着往下走
            key = (r["id"], room.key)
            if r.get("cooldown") and now - self.custom_last.get(key, 0) < int(r["cooldown"]):
                return {b.aid for b in who}, True  # 冷却中：静静地不理，也不当普通聊天
            self.custom_last[key] = now
            self.log("info", "command", f"[{room.name}] {e.name} 触发自定义指令「{r['name']}」（{customcmd.ACTIONS[r['action']]}）")
            for b in who:
                try:
                    await self.exec_custom(r, b, room, e, msg)
                except Exception as ex:
                    self.log("error", "command", f"[{room.name}] 自定义指令「{r['name']}」出错：{ex}")
            return {b.aid for b in who}, True
        return set(), False

    async def exec_custom(self, r: dict, b: Bot, room: Room, e: Entry, msg: Msg):
        quote = e.ids.get(b.aid) if r.get("at") and self.S("quote_reply", b, room) else None
        act = r["action"]
        if act == "reply":
            await self.deliver(b, room, r["text"].replace("{name}", e.name.split("〔")[0]), quote, "custom")
        elif act == "random":
            await self.deliver(b, room, customcmd.pick(r).replace("{name}", e.name.split("〔")[0]), quote, "custom")
        elif act == "builtin":
            await self.run_command(b, room, e, msg, r["builtin"], r.get("arg", ""), force=True)
        elif act == "ask":
            if self.llm.hard_capped(b.rid):
                self.log("info", "command", f"[{room.name}] 用量硬上限生效中，「{r['name']}」（让角色回答）不执行")
                return
            await self.speak(b, room, "custom", e, brief=r["prompt"], msg=msg)

    async def process_batch(self, room: Room, batch: list):
        """队列里积压了几条真人消息：被同一个机器人叫到不止一次的，合成一次回复；其余照旧一条条处理。"""
        merged: dict[str, list[int]] = {}
        if len(batch) > 1:
            for i, (e, msg) in enumerate(batch):
                for b in self.room_bots(room):
                    cmd = commands.parse(e.text)
                    if (self.addressed(b, e, msg, room) and not cmd and self.can_talk(b, room) and not self.blocked(e.text, room=room)
                            and not (e.pre_cmd and (e.pre_cmd["consumed"] or b.aid in e.pre_cmd["handled"]))  # 办过的指令不算「被叫到」一次
                            and not self.is_bot_person(room.platform, e.uid) and not any(customcmd.matches(r, e.text) for r in customcmd.rules(self.db) if r.get("on", True))
                            and not (b.channel and b.channel.reply_only)):  # 评论区类平台每条评论各回各的，不合并
                        merged.setdefault(b.aid, []).append(i)
            merged = {aid: ix for aid, ix in merged.items() if len(ix) >= 2}
        for i, (e, msg) in enumerate(batch):
            skip = frozenset(aid for aid, ix in merged.items() if i in ix)
            await self.process_human(room, e, msg, skip=skip, last=i == len(batch) - 1)
        spoke = False
        for aid, ix in merged.items():
            b = self.duty(self.bots[aid], room)
            es = [batch[i][0] for i in ix]
            last_e, last_msg = es[-1], batch[ix[-1]][1]
            self.log("info", "reply", f"[{room.name}] {b.role['name']} 被叫了 {len(es)} 次，合成一条回复")
            spoke |= await self.speak(b, room, "at", last_e, how=self.addressed(b, last_e, last_msg, room), msg=last_msg, group=es,
                                      free=any(self.user_free(b, room, x.uid) for x in es))
        if spoke:
            await self.chain_rounds(room)

    async def process_human(self, room: Room, e: Entry, msg: Msg, skip=frozenset(), last: bool = True):
        room.chain = 0
        room.last_human = time.time()
        if self.blocked(e.text, room=room):
            self.log("info", "reply", f"[{room.name}] 消息含屏蔽词，不理：{e.text[:30]}")
            return
        bots = self.room_bots(room)
        sender_bot = self.is_bot_person(room.platform, e.uid)
        if not sender_bot:
            room.bot_rounds.clear()  # 真人开口了：和别的机器人的互接重新计数
        for b in bots:
            if b.aid in room.pro_pending:
                room.pro_pending[b.aid]["after"] += 1
        if e.cmd_task is not None:
            await e.cmd_task  # 指令在消息进来时就办了，这里只等它办完
        pre = e.pre_cmd
        if pre is None:  # 不是从正常入口进来的（比如面板上的「让她回这条」）：照老办法在这里办
            done, consumed = await self.run_custom(room, bots, e, msg, sender_bot)  # 自定义指令先于内置指令
        else:
            done, consumed = pre["done"], pre["consumed"]
        must, handled = [], set(done)
        for b in bots:
            how = self.addressed(b, e, msg, room)
            if not how or b.aid in done:
                continue
            if sender_bot and not self.bot_may_reply(b, room, e):
                continue  # 别的机器人 @ 她：按设置不理、或已经接够了 N 轮
            cmd = commands.parse(e.text)
            if cmd and cmd[0] == "persona" and not self.switch_target(cmd[1])[0][1]:
                cmd = None  # 「换××」里的××既不是角色也不是人设：当普通聊天
            if cmd:
                handled.add(b.aid)
                if pre is None:
                    await self.run_command(b, room, e, msg, *cmd)
            elif self.can_talk(b, room):
                e.notes[b.aid] = await self.act(b, room, e, room.platform, "group", room.chat_id)
                if b.aid not in skip:  # 被同一个机器人叫了好几次的，稍后合成一条回复
                    must.append((b, how))
        if must and not sender_bot:
            self.resume(room)
        if self.S("repeat_follow", room=room):
            await self.maybe_repeat(room, bots, handled | {b.aid for b, _ in must})
        pro = []
        if last and room.queue.empty() and not consumed:  # 后面还有新消息就只管必回，插话留给最新那条
            taken = handled | {b.aid for b, _ in must} | set(skip)
            if not self.defer_pro(room, e, msg, bots, taken, sender_bot):
                pro = await self.pro_pick(room, e, bots, taken, sender_bot)
        responders = self.cap_responders([(b, "at", how, None) for b, how in must], [(b, "pro", None, br) for b, br in pro], room)
        spoke = False
        for b, mode, how, brief in responders:
            e.note = e.notes.get(b.aid, "")
            try:
                ok = await self.speak(b, room, mode, e, how=how, brief=brief, msg=msg, free=mode == "at" and self.user_free(b, room, e.uid))
                spoke |= ok
                if ok and sender_bot and mode == "at":
                    room.bot_rounds[(b.aid, e.uid)] = room.bot_rounds.get((b.aid, e.uid), 0) + 1
            finally:
                e.note = ""
        if spoke and last and not skip:
            await self.chain_rounds(room)
        return spoke

    async def pro_pick(self, room: Room, e: Entry, bots: list[Bot], taken: set, sender_bot: bool) -> list:
        """这条消息主动插话：把各项条件过一遍，过得去的问守门员，返回[(机器人, 她接哪个点)]。"""
        pre_hit = prefilter.check(prefilter.rules(self.db), e.text, False) if self.S("prefilter_on", room=room) else None
        cands, observers = [], []
        for b in bots:
            if b.aid in taken or not self.can_talk(b, room):
                continue
            hit = "别的机器人发的消息" if sender_bot and self.S("bot_skip_gate", b, room) else pre_hit  # 按这个角色自己的设置
            hard, soft = self.pro_state(b, room)
            if not hard:
                continue
            if soft and self.sample_ok(b, room):
                if hit:  # 明摆着没话可接：不花这次守门员调用
                    self.count("gk_saved", b.rid, room.key)
                else:
                    cands.append(b)
            elif not hit and self.observing(b, room):
                observers.append(b)
        briefs = await asyncio.gather(*(self.gatekeeper(b, room, e) for b in cands),
                                      *(self.gatekeeper(b, room, e, observe=True) for b in observers))
        if time.time() < room.pause_until:
            return []
        pro = [(b, br) for b, br in zip(cands, briefs[:len(cands)]) if br is not None]
        pro.sort(key=lambda x: -self.eager_level(x[0], room))
        return pro

    def defer_pro(self, room: Room, e: Entry, msg: Msg, bots: list[Bot], taken: set, sender_bot: bool) -> bool:
        """主动插话前等发言人说完：这条消息的发言人停下来够「主动插话前等对方停几秒」，才去问守门员；这期间他又发了，重新计时，
        从这一串的第一条算起到了「最多等几秒」就不再等。等的是一个独立的定时任务，不占群的消息队列（被@的消息、别人的消息照常处理）；
        到点才把「问守门员」排回队列（期间群里有了新消息就作废，留给最新那条）。返回 True = 已经排上等待，这次不用现在问。"""
        cur, room.pro_wait = room.pro_wait, {}
        if cur.get("task"):
            cur["task"].cancel()  # 同一个人又发了（重新计时），或者别人发了（那一条自己会去等）：上一次的等待作废
        if sender_bot:
            return False
        live = [b for b in bots if b.aid not in taken and self.can_talk(b, room) and self.pro_state(b, room)[0]]
        wait = max((int(self.S("pro_wait", b, room) or 0) for b in live), default=0)
        if wait <= 0:
            return False
        cap = max(int(self.S("pro_wait_max", b, room) or 0) for b in live)
        start = cur["start"] if cur.get("uid") == e.uid else e.ts
        delay = min(e.ts + wait, start + cap) - time.time()
        if delay <= 0:
            return False
        self.log("info", "gatekeeper", f"[{room.name}] {e.name} 的话可能还没说完，{delay:.0f} 秒后没有新消息再判断要不要插话（等的期间不卡别的消息）")
        room.pro_wait = {"uid": e.uid, "start": start, "task": self.spawn(self.pro_timer(room, e, msg, taken, delay))}
        return True

    async def pro_timer(self, room: Room, e: Entry, msg: Msg, taken: set, delay: float):
        await asyncio.sleep(delay)
        if room.pro_wait.get("task") is asyncio.current_task():
            room.pro_wait = {}
        if room.seq == e.seq:  # 这期间群里没有新消息才轮得到这一条；有了就留给最新那条
            room.queue.put_nowait(("job", self.pro_late(room, e, msg, taken), None))

    async def pro_late(self, room: Room, e: Entry, msg: Msg, taken: set):
        """等够了之后的主动插话：各项条件现在重新过一遍，和不等时一样。"""
        if room.seq != e.seq:
            return
        pro = await self.pro_pick(room, e, self.room_bots(room), taken, False)
        spoke = False
        for b, mode, how, brief in self.cap_responders([], [(b, "pro", None, br) for b, br in pro], room):
            spoke |= await self.speak(b, room, mode, e, how=how, brief=brief, msg=msg)
        if spoke:
            await self.chain_rounds(room)

    def cap_responders(self, called: list, chimed: list, room: Room) -> list:
        """一条消息最多几个机器人回：被叫到的都回，上限只限制主动插话的（被叫到的先占名额，剩下的给插话的）。"""
        return called + chimed[: max(0, int(self.S("max_bots_per_message", room=room)) - len(called))]

    async def chain_rounds(self, room: Room):
        limit = int(self.S("bot_chain_limit", room=room))
        while room.chain < limit and room.queue.empty():
            last = room.ctx[-1] if room.ctx else None
            if not last or not last.bot:
                return
            room.chain += 1
            bots = [b for b in self.room_bots(room) if b.aid != last.bot and self.can_talk(b, room)]
            must = [(b, h) for b in bots if (h := self.addressed(b, last, None, room))]
            cands = [b for b in bots if b not in [m[0] for m in must] and self.proactive_ok(b, room)]
            briefs = await asyncio.gather(*(self.gatekeeper(b, room, last) for b in cands))
            responders = self.cap_responders([(b, "at", h, None) for b, h in must], [(b, "pro", None, br) for b, br in zip(cands, briefs) if br is not None], room)
            if not responders:
                return
            spoke = False
            for b, mode, how, brief in responders:
                spoke |= await self.speak(b, room, mode, last, how=how, brief=brief)
            if not spoke:
                return

    async def maybe_repeat(self, room: Room, bots: list[Bot], busy: set):
        last = list(room.ctx)[-3:]
        if len(last) < 2 or last[-1].bot:
            return
        same = [x for x in reversed(last) if x.text == last[-1].text and not x.bot]
        if len(same) >= 2 and len({x.uid for x in same}) >= 2 and len(last[-1].text) <= 30:
            for b in bots:
                if b.aid not in busy and self.can_talk(b, room):
                    if any(x.bot for x in list(room.ctx)[-6:] if x.text == last[-1].text):
                        return
                    await self.deliver(b, room, last[-1].text, None, "repeat")
                    return

    # ---------- 守门员 ----------
    def uid_names(self, room: Room) -> dict[str, str]:
        names = {}
        for x in room.ctx:
            if x.uid:
                names[x.uid] = x.name
        return names

    def who(self, uid: str, b: Bot, self_label: str, names: dict) -> str:
        if uid and uid == b.uid:
            return self_label
        return names.get(uid) or "某人"

    def role_of(self, e: Entry) -> str | None:
        """这条机器人消息是哪个角色说的：记录里有就用；老记录没有，就从名字里的「〔X本人〕」认；认不出返回 None。"""
        if e.role:
            return e.role
        m = re.search(r"〔(.+?)本人〕", e.name or "")
        return next((r["id"] for r in self.roles.values() if m and r["name"] == m.group(1)), None)

    def speaker_label(self, e: Entry, b: Bot, self_label: str) -> str:
        """只有当前在岗的角色自己说的才标「你」/「她（自己）」；同一个号上换班之前别的角色说的，标成「某某（这个号之前的角色）」。"""
        if e.bot != b.aid:
            return e.name
        rid = self.role_of(e)
        if rid and rid != b.rid:
            m = re.search(r"〔(.+?)本人〕", e.name or "")
            name = (self.roles.get(rid) or {}).get("name") or (m.group(1) if m else "") or "别的角色"
            return f"{name}（这个号之前的角色）"
        return self_label

    def fmt(self, room: Room, b: Bot, e: Entry, self_label: str, names: dict | None = None, hide_self_at: bool = False) -> tuple[str, str]:
        """一条消息给模型看的样子：（名字[（回复 谁）]，正文[前面加（@谁）]）。"""
        names = names if names is not None else self.uid_names(room)
        spk = self.speaker_label(e, b, self_label)
        if e.reply_uid:
            spk += f"（回复 {self.who(e.reply_uid, b, self_label, names)}）"
        text = e.text
        ats = [u for u in e.at_uids if not (hide_self_at and u == b.uid)]
        if ats:
            text = "（" + "、".join("@" + self.who(u, b, self_label, names) for u in ats) + "）" + text
        return spk, text

    def ctx_items(self, room: Room, b: Bot, n: int | None = None, cover: bool = False) -> list[Entry]:
        """拼给模型的最近原始消息：至少最近 n 条；cover=True 时再往前带到「最近一段已整理的前情」结束的地方，
        这样群里每一条消息不是在前情里，就是在原始消息里（整理没跟上时不漏）。内存里的上下文只留最近 200 条，
        积压的未整理消息超出这个数时，从 messages 表把中间缺的那一段补回来（和重启回填的结果一致）。"""
        n = n or self.S("context_window", b, room)
        items = list(room.ctx)
        start = max(0, len(items) - n)
        if cover and self.memory.enabled(room.platform, room.key):
            first = next((i for i, x in enumerate(items) if x.dbid and x.dbid > room.mem_cov), None)
            if first is not None:
                start = min(start, first)
            earliest = min((x.dbid for x in items if x.dbid), default=0)
            if earliest > room.mem_cov:  # 内存里最早的一条之前还有没整理的消息：从库里补齐
                extra = self._ctx_rows(room, room.mem_cov, earliest)
                if extra:
                    items = extra + items
                    start = 0  # 补回来的这段必须带上（每条消息不是在前情里，就是在原始消息里）
        return items[start:]

    def _ctx_rows(self, room: Room, after_id: int, before_id: int) -> list[Entry]:
        """messages 表里编号在 (after_id, before_id) 之间的真人 / 机器人消息（模型看的样子），补内存上下文的缺口用。"""
        rows = self.db.rows("SELECT id, ts, account_id, user_id, name, text, role_id, kind FROM messages WHERE room=? AND id>? AND id<? "
                            "AND ts>? AND kind IN ('human','bot') ORDER BY id",
                            (room.key, after_id, before_id, self.ctx_floor(room.key)))
        return [Entry(m["user_id"], m["name"], m["text"], bot=m["account_id"] if m["kind"] == "bot" else None,
                      role=m["role_id"] if m["kind"] == "bot" else "", ts=m["ts"], dbid=m["id"]) for m in rows]

    def ctx_lines(self, room: Room, b: Bot, self_label: str, n: int | None = None, cover: bool = False) -> str:
        items = self.ctx_items(room, b, n, cover)
        if not items:
            return "（暂时没有上下文）"
        names = self.uid_names(room)
        return "\n".join("%s: %s" % self.fmt(room, b, e, self_label, names) for e in items)

    def present(self, room: Room, b: Bot) -> tuple[list[dict], list[tuple[str, str]], list[tuple[str, str]], bool]:
        """当前对话里出现的人：（人物卡，同群机器人，记忆主体，有没有人物表外的人）。"""
        items = list(room.ctx)[-self.S("context_window", b, room):]
        text = "\n".join(e.text for e in items)
        persons, subjects, strangers = {}, {}, False
        bots = self.bot_uids(room.platform)
        for e in items:
            if e.bot:
                continue
            p = self.people.find(room.platform, e.uid)
            if p:
                persons[p["id"]] = p
                subjects[f"person:{p['id']}"] = p["name"]
            elif e.uid not in bots:
                strangers = True
                subjects[subject_key(room.platform, e.uid, None)] = e.name
        for p in self.people.persons.values():
            if p["id"] not in persons and (p.get("name") or "").strip() and (p["name"] in text or any(len(c) > 1 and c in text for c in self.people.calls_of(p))):
                persons[p["id"]] = p
        sib = []
        rel = (b.role.get("relations") or {})
        for ob in self.room_bots(room):
            if ob.aid != b.aid and ob.rid != b.rid:
                sib.append((ob.role["name"], rel.get(ob.rid) or self.S("sibling_relation", b, room)))
        return list(persons.values()), sib, list(subjects.items()), strangers

    def gate_extra(self, b: Bot, room: Room) -> tuple[str, bool, dict | None]:
        """守门员提示词末尾的附加判断（同一次调用里一起问）：有人叫停吗？她上一次插话的反馈如何？"""
        want_stop = bool(self.S("stop_on", b, room))
        pend = room.pro_pending.get(b.aid) if self.S("eager_auto", b, room) else None
        if pend and time.time() - pend["ts"] > 1800:
            room.pro_pending.pop(b.aid, None)
            pend = None
        if not (want_stop or pend):
            return "", False, None
        out = GATE_EXTRA_HEAD
        if want_stop:
            out += GATE_STOP
        if pend:
            out += GATE_FEEDBACK.format(mins=max(1, int((time.time() - pend["ts"]) // 60)), text=pend["text"][:60])
        return out, want_stop, pend

    async def model_failed(self, b: Bot, room: Room | None, ex: Exception, chat_type: str, chat_id: str, asked: bool):
        """模型接口出错（llm 已经自动重试过一次）：日志里已经记了；面板总览留一条未读通知（十分钟内同类的只留一条）；
        「模型出错时在群里发一句」打开了才发，同一个群 / 私聊十分钟内最多一次。影子模式什么都不发。"""
        now = time.time()
        if now - self.err_notice.get("panel", 0) > 600:
            self.err_notice["panel"] = now
            self.add_notice(f"模型接口出错（{b.role['name']}）：{str(ex)[:100]}。已经自动重试过一次，仍然失败；请检查「设置 → 模型与接口」里的 Key、网络和额度。")
        k = f"{chat_type}:{chat_id}"
        if asked and b.online and self.S("error_notice", b, room) and not self.S("shadow", b, room) and now - self.err_notice.get(k, 0) > 600 \
                and not (room and self.muted_in(b.aid, room.key)):
            self.err_notice[k] = now
            await b.channel.send(chat_type, chat_id, "（断线了……模型接口出了点问题，稍后再试试）")

    def user_line(self, b: Bot) -> str:
        """守门员提示词里「该开口」一条里关于用户的那句：按用户卡上这个角色用的称呼生成（「她妈说了她有得接的话；」）；没有用户卡、或没填称呼，整句不写。"""
        user = self.people.user_card()
        calls = self.people.scheme_for(user, b.role)[0].get("calls") if user else None
        return f"她{calls[0]}说了她有得接的话；" if calls else ""

    async def gatekeeper(self, b: Bot, room: Room, e: Entry, observe: bool = False) -> str | None:
        persons, sib, _, _ = self.present(room, b)
        marks = self.people.marks(persons, sib, b.role)
        tpl = self.settings.g("gatekeeper_prompt") or GATEKEEPER_PROMPT
        prompt = fill(tpl, summary=b.role.get("summary", ""), marks=f"；{marks}。" if marks else "。", user_line=self.user_line(b),
                      context=self.ctx_lines(room, b, "她（自己）"), speaker=self.fmt(room, b, e, "她（自己）")[0],
                      text=self.fmt(room, b, e, "她（自己）")[1], 平台群=personas.plat_words(room.platform)[1])
        extra, want_stop, pend = self.gate_extra(b, room)
        prompt += extra
        try:
            out = await self.llm.chat("gk", [{"role": "user", "content": prompt}], b.rid, room.platform, room.key)
            d = parse_json(out)
            allow, brief = bool(d.get("reply")), str(d.get("brief", ""))
        except Exception as ex:
            self.log("warn", "gatekeeper", f"[{room.name}] {b.role['name']} 守门员出错（默认不插话）：{ex}")
            return None
        if want_stop and d.get("stop") is True:
            self.on_stop(room, e)
        if pend and b.aid in room.pro_pending:
            self.on_feedback(b, room, str(d.get("feedback", "")).strip().lower(), room.pro_pending[b.aid])
        if observe:
            return None
        self.log("info", "gatekeeper", f"[{room.name}] {b.role['name']} ← {e.name}: {e.text[:40]} → {'插话' if allow else '不插'}（{brief}）",
                 {"room": room.key, "role": b.rid, "allow": allow, "brief": brief})
        return brief if allow else None

    # ---------- 生成与发送 ----------
    def persona_for(self, b: Bot, room: Room | None) -> dict:
        pid = b.duty_persona or self.S("persona", b, room) or b.role.get("persona")
        return self.personas.get(pid) or next(iter(self.personas.values()), {"items": []})

    def mem_weights(self, room: Room, b: Bot, trigger: Entry | None) -> dict[str, int]:
        """按人取回旧记录用的分：这条消息里提到的人 +2，最近原始消息里说过话的人 +1，她自己（我们的机器人）不算。按编号对，不调模型。"""
        w: dict[str, int] = {}
        for x in list(room.ctx)[-self.S("context_window", b, room):]:
            if x.uid and not x.bot:
                w[x.uid] = 1
        trigger = trigger or (room.ctx[-1] if room.ctx else None)
        if trigger and not trigger.bot:
            hit = {str(u) for u in trigger.at_uids} | ({trigger.reply_uid} if trigger.reply_uid else set())
            text = trigger.text or ""
            for uid, name in self.uid_names(room).items():
                n = re.sub(r"〔.*?〕", "", name or "").strip()
                if len(n) >= 2 and n in text:
                    hit.add(uid)
            for p in self.people.persons.values():
                if any(len(n) >= 2 and n in text for n in [p.get("name") or ""] + self.people.calls_of(p)):
                    hit.update(self.people.uids(p, room.platform))
            for uid in hit:
                w[uid] = w.get(uid, 0) + 2
        for uid in self.bot_uids(room.platform):
            w.pop(uid, None)
        return w

    def memory_budget(self, b: Bot, platform: str, chat: str | None, base_chars: int, extra_chars: int) -> int:
        """记忆一节能用多少字：模型能读多长，减去回复长度和人设、人物表、原始消息等其余部分（再留一点余量）；用户填了「记忆最多带多少」就再封个顶。"""
        lim = self.llm.context_limit(b.rid, platform, chat)
        reply = int(self.settings.g("max_tokens") or 0) or 8192
        avail = int((lim - reply) * CHARS_PER_TOKEN) - base_chars - extra_chars - 3000
        cap = int(self.settings.g("memory_max") or 0)
        return max(0, min(avail, cap) if cap > 0 else avail)

    def shared_account_note(self, b: Bot, room: Room | None, private_uid: str = "") -> str:
        """一个号上轮流在岗的不止一个角色（换班、临时换人）时，在提示词里陈述事实：这个号在群里看到的昵称是几个角色共用的，现在在岗的是谁，
        之前在这个号上说过话的是谁——记录和记忆里写到这个昵称的地方，指的是当时在岗的那个角色，她们的自称、口头禅、做过的事不是现在这位的。
        只陈述，不下禁令；这个号上只出现过一个角色时什么都不加。人设里不写这类限制，因为它不属于任何一个人设。"""
        key = room.key if room else self.dm_key(b.aid, private_uid)
        rows = self.db.rows("SELECT role_id, MAX(id) AS last, name FROM messages WHERE room=? AND account_id=? AND kind='bot' AND role_id<>'' "
                            "GROUP BY role_id ORDER BY last DESC LIMIT 8", (key, b.aid))
        before = []
        for r in rows:
            if r["role_id"] == b.rid:
                continue
            m = re.search(r"〔(.+?)本人〕", r["name"] or "")
            name = (self.roles.get(r["role_id"]) or {}).get("name") or (m.group(1) if m else "")
            if name and name not in before and name != b.role["name"]:
                before.append(name)
        if not before:
            return ""
        nick = (b.channel.self_name if b.channel else "") or ""
        head = f"群里看到的这个号叫「{nick}」，这个昵称是几个角色轮流用的，不是哪一个角色专有的" if nick else "这个号是几个角色轮流用的"
        who = "、".join(f"「{n}」" for n in before)
        what = f"「{nick}」" if nick else "这个号"
        return (f"\n\n【这个号上的几个角色】\n{head}：现在在岗的是「{b.role['name']}」；之前在这个号上说过话的还有{who}。"
                f"聊天记录和记忆里写到{what}的地方，指的是当时在这个号上说话的那个角色，不一定是现在的「{b.role['name']}」；"
                f"{who}各自的自称、口头禅、做过的事，是她们自己的。")

    async def system_prompt(self, b: Bot, room: Room | None, private_person: dict | None = None, private_uid: str = "",
                            private_name: str = "", query: str = "", trigger: Entry | None = None, extra_chars: int = 0) -> str:
        platform = room.platform if room else b.platform
        if room:
            persons, sib, subjects, strangers = self.present(room, b)
            ctx_text = "\n".join(e.text for e in list(room.ctx)[-30:])
            query = "\n".join(e.text for e in list(room.ctx)[-4:])  # 语义回忆按眼下最近几句找
            weights = self.mem_weights(room, b, trigger)
            extra_chars = extra_chars or len(self.ctx_lines(room, b, "你", cover=True)) + 800
        else:
            persons = [private_person] if private_person else []
            sib, strangers, ctx_text = [], not private_person, ""
            subjects = [(subject_key(platform, private_uid, private_person), private_person["name"] if private_person else private_name)]
            weights = {u: 1 for u in self.memory.subject_uids(subjects[0][0], platform)}
        chat_doc = self.chat_doc(room)
        dyn = {"people": self.people.section(persons, sib, chat_doc, b.role, strangers, platform), "memory": ""}
        persona = self.persona_for(b, room)
        user, ai = self.people.user_name(), bool(self.S("admit_ai", b, room))
        base = personas.assemble(persona, self.modules, platform, dyn, user=user, ai=ai)
        note = self.shared_account_note(b, room, private_uid)
        budget = self.memory_budget(b, platform, room.key if room else None, len(base), extra_chars + len(note))
        dyn["memory"] = await self.memory.full_section(b.rid, platform, room.key if room else None, subjects, ctx_text, query,
                                                       dm=None if room else self.dm_key(b.aid, private_uid), weights=weights, budget=budget)
        text = personas.assemble(persona, self.modules, platform, dyn, user=user, ai=ai) + note
        if room and self.S("style_follow", b, room):
            style = self.memory.style_text(room.key)
            if style:
                text += ("\n\n【这个群的说话习惯】\n" + style + "\n（只是群友平时说话的样子。你可以在句子长短、标点、口头禅、表情用法上稍微贴近一点；"
                         "你的人设、身份与对每个人的态度、回复语言、平台格式要求永远优先，冲突时以它们为准，不要因此变成另一个人。）"
                         + ("\n（画像是从群里所有人的发言归纳的，其中写到这个号的昵称或这个号上之前几个角色的口头禅、用语，是她们的，不是你的。）" if note else ""))
        lang = self.S("reply_lang", b, room)
        if lang == "follow":
            text += "\n\n【回复语言】\n用对方说话所用的语言回复；一条消息里混着几种语言时，用占多数的那种。"
        elif lang and lang != "zh":
            name = LANG_NAME.get(lang, lang)
            text += f"\n\n【回复语言】\n用{name}回复，群里别人用别的语言说话也照常用{name}。"
        return text

    def length_hint(self, b: Bot, room: Room) -> str:
        cap = int(self.S("group_reply_max", b, room) or 0)
        return f"\n（这次回复控制在 {cap} 字以内。）" if cap > 0 else ""

    def repeat_hint(self, b: Bot, room: Room) -> str:
        """她最近几条发言里反复出现的梗、术语、比喻：提醒她这次换别的或不用。"""
        n = int(self.S("repeat_hint_n", b, room) or 0)
        if n <= 0:
            return ""
        texts = [x.text for x in list(room.ctx) if x.bot == b.aid and self.role_of(x) in (None, b.rid)][-n:]  # 只看现在这个角色自己说过的
        ph = repeated_phrases(texts)
        return ("\n【提醒】你最近的发言里反复出现：" + "".join(f"「{p}」" for p in ph) + "。这次换别的说法，或者干脆不用。") if ph else ""

    def user_prompt(self, b: Bot, room: Room, mode: str, e: Entry, how: str | None, brief: str | None, group: list | None = None) -> str:
        desc = ((self.chat_doc(room) or {}).get("desc") or "").strip()
        head = f"【当前群】{room.name}" + (f"（{desc}）" if desc else "") + f"\n【最近群聊】\n{self.ctx_lines(room, b, '你', cover=True)}\n\n"
        if e.attach:
            head += e.attach + "\n\n"
        if e.note:
            head += e.note + "\n\n"
        spk, txt = self.fmt(room, b, e, "你", hide_self_at=mode in ("at", "force", "refuse"))
        tail = "" if mode in ("remind", "refuse") else self.length_hint(b, room) + self.repeat_hint(b, room)
        if mode == "remind":
            body = f"到点了：{e.name} 之前让你提醒他：「{brief}」。用你自己的口吻提醒他一句，要把提醒的事说清楚。直接输出要发送的话。"
        elif mode == "at" and group and len(group) > 1:
            names = self.uid_names(room)
            lines = []
            for x in group:
                sp, tx = self.fmt(room, b, x, "你", names, hide_self_at=True)
                lines.append(f"{sp}: {tx}" + (f"\n  {x.notes[b.aid]}" if x.notes.get(b.aid) else ""))
            body = ("这几个人先后@了你（或叫了你），你一起回一条，回复里分别照顾到每个提问的人（直接叫名字，别漏人，每个人答得简短些）：\n"
                    + "\n".join(lines) + "\n\n直接输出要发送的话。")
        elif mode == "at":
            body = f"{spk} {how or '@了你'}，说：{txt}"
        elif mode == "pro":
            hook = f"插话的由头：{brief}。" if brief else ""
            body = f"没人@你，你决定主动插一句话。{hook}接着最新的话头说，不重复别人说过的。直接输出要发送的话本身，开头不要加「主动插话」「由头」这类标注或说明。"
        elif mode == "poke":
            body = f"{e.name} 戳了戳你。直接输出要发送的话。"
        elif mode == "force":
            body = f"{spk} 说：{txt}\n\n你决定接这句话。直接输出要发送的话。"
        elif mode == "cold":
            body = "群里已经冷了一阵子。你想开个话题，接着这个群平时聊的东西或你自己感兴趣的事说一句。直接输出要发送的话。"
        elif mode == "shift":
            body = f"你刚接班（之前在这个群的是「{brief}」）。在群里简单打个招呼，一两句，别太正式。直接输出要发送的话。"
        elif mode == "refuse":
            body = f"{spk} @了你，想让你「{brief}」，可对方没有这个权限。用你自己的方式拒绝，一两句。直接输出要发送的话。"
        elif mode == "custom":
            body = f"{spk} 说：{txt}\n\n群里定了个规矩：有人这样说，你就这样做——{brief}\n用你自己的口吻照做。直接输出要发送的话。"
        else:
            body = f"{brief}。直接输出要发送的话。"
        return head + body + tail

    def take_sticker_choice(self, skey: tuple, text: str, where: str, b: Bot) -> str:
        """从回复里取她挑的表情包：【表情N】的 N 在这次 send_sticker 给的候选范围内，就放进 stk_out（有好几个取最后一个有效的）；
        看了候选没挑成只记一条日志。不管挑没挑成，文字里的【表情N】标记都删掉（没调工具却写了标记的，也只是删标记）。"""
        cand = self.stk_cand.pop(skey, None)
        if cand:
            n = len(cand["rows"])
            ok = [int(m) for m in STK_MARK.findall(text) if 1 <= int(m) <= n]
            if ok:
                self.stk_out[skey] = [dict(cand["rows"][ok[-1] - 1], pick=(cand["mood"], ok[-1], n))]
            else:
                self.log("info", "sticker", f"[{where}] {b.role['name']} 看了 {n} 张表情包候选（心情「{cand['mood']}」），没挑，只发文字")
        return STK_MARK.sub("", text)

    @staticmethod
    def pick_note(row: dict) -> str:
        """日志里跟在「表情包」后面的补充：心情和第几张（候选里挑的才有）。"""
        mood, i, n = row.get("pick") or (None, 0, 0)
        return f"心情「{mood}」，{n} 张里挑了第 {i} 张" if i else ""

    def postprocess(self, b: Bot, room: Room | None, text: str) -> str:
        text = cleanup.strip_labels(text)  # 开头漏进来的内部标注（「主动插话（……）：」）
        if self.S("strip_fake_images", b, room):
            text = cleanup.strip_media(text)  # 编出来冒充图片的整行「[图片：……]」
        if self.S("strip_actions", b, room):
            text = cleanup.strip_actions(text)  # 括号里的动作描写
        if self.S("output_format", b, room) == "plain":
            text = strip_markdown(text)
        for w in self.S("blocked_words", b, room) or []:
            if w:
                text = text.replace(w, "□" * len(w))
        mx = self.S("reply_max_chars", b, room)
        if len(text) > mx:
            text = text[:mx] + "……"
        return text.strip()

    def tools_for(self, b: Bot, room: Room | None, tag: str = "main", called: bool = False, peer: str = ""):
        """这次回复给主模型哪些工具：联网搜索、发表情包。返回（工具说明列表, 处理函数, 系统提示词里的补充）。
        called：这次是被叫到的（@、回复她、叫名字，私聊每次回复也算）；「被叫到时总给她表情包工具」开着就不看概率。
        私聊（room 是 None）：表情包只从这个账号自己的收藏里挑。本该有表情包（「发表情包」开着、有可用的）这次却没给她工具时，补充里写一句事实说明。"""
        if tag != "main":
            return [], None, ""
        platform = room.platform if room else b.platform
        chat = room.key if room else None
        want = bool(self.S("sticker_use", b, room) and self.stickers.available(room, b))  # 本该有表情包
        note = NO_STICKER_NOTE if want else ""
        state = self.llm.tools_state(b.rid, platform, chat)["state"]
        if state == "no":
            return [], None, note
        tools = []
        if self.S("search_on", b, room) and self.search.available()[0]:
            tools.append(search_tool())
        if (want and b.channel and b.channel.supports_image
                and ((called and self.S("sticker_call", b, room)) or random.random() * 100 < float(self.S("sticker_chance", b, room)))):
            tools.append(sticker_tool())
            note = ""
        if not tools:
            return [], None, note
        skey = (room.key if room else self.dm_key(b.aid, peer), b.aid)

        async def handler(name, args):
            if name == "web_search":
                return await self.search.run(str(args.get("query", "")), b.rid, platform, chat)
            if name == "send_sticker":
                mood = str(args.get("mood", ""))
                rows = await self.stickers.candidates(room, mood, b, max(1, int(self.settings.g("sticker_candidates") or 10)))
                if not rows:
                    self.stk_cand.pop(skey, None)
                    return "这个群里没有合适的表情包，直接用文字吧" if room else "没有合适的表情包，直接用文字吧"
                self.stk_cand[skey] = {"mood": mood, "rows": rows}  # 同一次回复里调了不止一次：以最后一次为准
                return ("找到这几张：\n" + "\n".join(f"{i}. {r.get('descr') or ''}" for i, r in enumerate(rows, 1))
                        + "\n想发哪张，就在回复最后单独写一行【表情+编号】，例如【表情2】；都不合适就不写。")
            return "没有这个工具"
        return tools, handler, _tool_hint(tools, dm=room is None) + note

    async def complete(self, b: Bot, room: Room | None, msgs: list, tag: str = "main", rebuild=None, called: bool = False, peer: str = "") -> str:
        """rebuild：重新拼系统提示词的函数。请求因为超长被拒（llm 已经记下上限）就按新上限重新摆一次、再发一次，这一次的回复不丢。"""
        try:
            return await self._complete(b, room, msgs, tag, called, peer)
        except ContextOverflow as ex:
            if rebuild is None:
                raise
            self.log("warn", "model", f"请求超出模型能读的长度（上限 {ex.limit}），按新上限重新摆记忆再发一次")
            return await self._complete(b, room, [dict(msgs[0], content=await rebuild())] + list(msgs[1:]), tag, called, peer)

    async def _complete(self, b: Bot, room: Room | None, msgs: list, tag: str = "main", called: bool = False, peer: str = "") -> str:
        tools, handler, hint = self.tools_for(b, room, tag, called, peer)
        platform = room.platform if room else b.platform
        chat = room.key if room else None
        if hint:
            msgs = [dict(msgs[0], content=msgs[0]["content"] + hint)] + msgs[1:]
        if not tools:
            return await self.llm.chat("main", msgs, b.rid, platform, chat, tag=tag)
        return await self.llm.chat_tools("main", msgs, tools, handler, b.rid, platform, chat, tag=tag,
                                         rounds=max(1, int(self.settings.g("search_rounds") or 2)), tool_tags={"web_search": "search"})

    async def fit(self, b: Bot, room: Room, text: str, system: str) -> str:
        """群聊单次回复的字数上限：超了就让模型自己压缩（最多两次），还超才按整句收，不在句子中间断。"""
        cap = int(self.S("group_reply_max", b, room) or 0)
        if not cap or len(text) <= cap:
            return text
        for _ in range(2):
            self.count("compress", b.rid, room.key)
            msgs = [{"role": "system", "content": system},
                    {"role": "user", "content": f"你刚写好的这条回复有 {len(text)} 字，超过了这个群的字数上限 {cap} 字。保持你的口吻和要点，把它压缩到 {cap} 字以内，"
                                                f"不要在句子中间断开，不要解释。直接输出压缩后的回复。\n\n{text}"}]
            out = self.postprocess(b, room, await self.llm.chat("main", msgs, b.rid, room.platform, room.key, tag="compress"))
            if out:
                text = out
            if len(text) <= cap:
                return text
        self.count("compress_cut", b.rid, room.key)
        return trim_sentences(text, cap)

    async def generate(self, b: Bot, room: Room, mode: str, e: Entry, how=None, brief=None, tag="main", group=None) -> str:
        system = await self.system_prompt(b, room, trigger=e)
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": self.user_prompt(b, room, mode, e, how, brief, group)}]
        raw = await self.complete(b, room, msgs, tag, rebuild=lambda: self.system_prompt(b, room, trigger=e), called=mode == "at")
        text = self.postprocess(b, room, self.take_sticker_choice((room.key, b.aid), raw, room.name, b))
        return await self.fit(b, room, text, system) if text and mode not in ("remind",) else text

    async def stale_check(self, b: Bot, room: Room, mode: str, e: Entry, text: str, t0: float, group=None) -> str:
        """生成回复期间群里又来了新消息：发送前用最新的上下文重新判断，照发、改写或不发（返回空串）。"""
        if mode not in ("at", "pro", "force") or not text or not self.S("stale_on", b, room):
            return text
        new = [x for x in room.ctx if x.seq > e.seq and x.bot != b.aid]
        took = time.time() - t0
        if not new or (len(new) < int(self.S("stale_msgs", b, room)) and took < int(self.S("stale_secs", b, room))):
            return text
        self.count("stale_check", b.rid, room.key)
        spk, txt = self.fmt(room, b, e, "你")
        guide = ("被@的话一般还是要回；但如果问题已经被别人答了、话题已经过去，就改写成顺着现在的话头说，或不发。" if mode == "at"
                 else "这是你主动插的一句：如果话头已经过去、再说显得答非所问或在重复别人，就别发了。")
        system = await self.system_prompt(b, room, trigger=e)
        prompt = (f"【当前群】{room.name}\n【最近群聊】\n{self.ctx_lines(room, b, '你', cover=True)}\n\n"
                  f"【重新判断这条回复】你刚写好了一条回复，是针对 {spk}：「{txt}」写的：\n「{text}」\n"
                  f"写它花了 {int(took)} 秒，期间群里又来了 {len(new)} 条新消息（上面「最近群聊」已经包含）。按最新的情况重新判断：{guide}\n"
                  '只输出JSON：{"action": "send（照发）或 rewrite（改写）或 drop（不发了）", "text": "改写后的回复，只有 rewrite 才写", "why": "一句话理由"}')
        try:
            d = parse_json(await self.llm.chat("main", [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                                               b.rid, room.platform, room.key, tag="stale"))
        except UsageCapReached:
            return text
        except Exception as ex:
            self.log("warn", "model", f"[{room.name}] 过时回复的重新判断出错（照发）：{ex}")
            return text
        action = str(d.get("action", "send")).split("（")[0].strip().lower()
        self.log("info", "reply", f"[{room.name}] {b.role['name']} 回复写好时已过时（{len(new)} 条新消息，{int(took)} 秒）→ "
                 f"{ {'rewrite': '改写', 'drop': '不发了'}.get(action, '照发') }：{str(d.get('why', ''))[:40]}")
        if action == "drop":
            self.count("stale_drop", b.rid, room.key)
            return ""
        if action == "rewrite" and str(d.get("text", "")).strip():
            self.count("stale_rewrite", b.rid, room.key)
            return await self.fit(b, room, self.postprocess(b, room, str(d["text"])), system)
        self.count("stale_send", b.rid, room.key)
        return text

    async def cap_reply(self, b: Bot, room: Room | None, e: Entry | None = None, user_id: str = "") -> bool:
        """用量硬上限生效：不调模型，只回一句固定的话（按角色可设；同一处一分钟内只回一次，免得刷屏）。"""
        now = time.time()
        k = f"{room.key if room else 'dm:' + user_id}|{b.aid}"
        if now - self.cap_sent.get(k, 0) < 60:
            return False
        self.cap_sent[k] = now
        text = (self.S("hard_cap_message", b, room) or DEFAULT_CAP_MESSAGE).strip()
        if room:
            return await self.deliver(b, room, text, e.ids.get(b.aid) if e else None, "cap")
        if self.S("shadow", b):
            self.log("info", "shadow", f"[私聊 {user_id}] {b.role['name']}（影子）本来会说：{text}")
            return False
        await self.send_text(b, "private", user_id, text, None, None)
        self.log("info", "reply", f"[私聊 {user_id}] {b.role['name']}（用量用完）：{text}")
        return True

    async def speak(self, b: Bot, room: Room, mode: str, e: Entry, how=None, brief=None, msg: Msg | None = None, at=None, group=None,
                    free: bool = False, deferred: bool = False) -> bool:
        """free：不用回复名额（用户本人叫她）。deferred：这是排队等名额、现在到点了的那次，不再检查名额。"""
        if self.muted_in(b.aid, room.key):
            return False  # 被禁言：什么模式都不写、不调模型
        if mode in CAP_MODES and self.llm.hard_capped(b.rid):
            return await self.cap_reply(b, room, e)
        counted = mode in QUOTA_MODES and not free
        if counted and not deferred:
            wait = self.quota_wait(b, room)
            if wait > 0:  # 每小时回复名额用完了
                if mode in ("pro", "cold") or e.bot:  # 主动开口的、机器人之间的接话：直接不发
                    self.log("debug", "reply", f"[{room.name}] {b.role['name']} 回复名额用完，这次{MODE_LABEL.get(mode, mode)}不发")
                    return False
                self.defer_reply(b, room, mode, e, how, msg, group, wait)  # 被叫到、被戳：排队到下一个名额再回
                return False
        if mode in ("pro", "cold", "poke") and self.run_left(b, room) <= 0:  # 中间没人说话，已经连着发满了
            self.log("debug", "reply", f"[{room.name}] {b.role['name']} 中间没人说话已经连着发了 {self.run_limit(b, room)} 条，这次{MODE_LABEL.get(mode, mode)}不发")
            return False
        left = self.reply_gap_left(b, room)
        if left > 0 and mode in ("at", "poke", "cold"):  # 两次回复最小间隔；面板让她回、提醒、定时、指令这些不受限
            if mode != "at":
                return False
            await asyncio.sleep(left)
        if mode in MUTABLE_MODES and not self.can_talk(b, room):
            return False  # 等的这段时间里被叫停（闭嘴）了：还没发出的回复作废
        self.stk_out.pop((room.key, b.aid), None)
        self.stk_cand.pop((room.key, b.aid), None)
        t0 = time.time()
        try:
            text = await self.generate(b, room, mode, e, how, brief, group=group)
        except UsageCapReached as ex:
            self.log("info", "model", f"[{room.name}] {b.role['name']} {ex}")
            return False
        except Exception as ex:
            self.log("error", "model", f"[{room.name}] {b.role['name']} 生成失败：{ex}")
            await self.model_failed(b, room, ex, "group", room.chat_id, mode == "at")
            return False
        stickers = self.stk_out.pop((room.key, b.aid), [])
        if text:
            kept = await self.stale_check(b, room, mode, e, text, t0, group)
            if not kept:
                stickers = []
            text = kept
        if not text and not stickers:
            return False
        if mode in MUTABLE_MODES and not self.can_talk(b, room):  # 生成回复的这段时间里被闭嘴了：作废，不发
            self.log("info", "reply", f"[{room.name}] {b.role['name']} 生成好的回复作废了（现在不能说话：闭嘴中或已关闭）")
            return False
        if mode == "pro":
            self.mark_proactive(b, room)
        quote = e.ids.get(b.aid) if mode == "at" and (self.S("quote_reply", b, room) or b.channel.reply_only) else None
        if mode == "pro" and self.pro_should_quote(b, room, e):
            quote = e.ids.get(b.aid) or None
        sent = False
        if text:
            sent = await self.deliver(b, room, text, quote, mode, at)
        for row in stickers[:1]:
            if await self.deliver_sticker(b, room, row, None if text else quote, mode):
                sent = True
                if not text and mode == "pro":
                    pass  # mark_proactive 上面已经记过一次
                else:
                    self.count_speech(b, room)
        if sent:
            room.last_reply[b.aid] = time.time()
            if counted:
                self.quota_use(b, room)  # 一次回复用一个名额，不管拆成几条、后面跟没跟表情包
        return sent

    def pro_should_quote(self, b: Bot, room: Room, e: Entry) -> bool:
        """主动插话：她要回的那条后面已经有足够多条别人的消息（被隔开了）就引用它；那条就是群里最新的就不引用。"""
        if not self.S("pro_quote", b, room) or not e.seq or not e.ids.get(b.aid):
            return False
        after = sum(1 for x in room.ctx if x.seq > e.seq and x.bot != b.aid)
        return after >= int(self.S("pro_quote_min", b, room) or 0)
    def count_speech(self, b: Bot, room: Room):
        """发一张表情包算一条发言：计入每小时、每天的上限（不动冷却计时）。"""
        self.bump(b, room, time.time())

    async def sticker_send(self, b: Bot, chat_type: str, chat_id: str, row: dict, quote: str | None, where: str) -> list | None:
        """把一张表情包发出去（群、私聊共用）。返回消息 id 列表；没发成返回 None，原因写日志。"""
        data = self.stickers.load(row)
        if not data or not b.channel or not b.channel.supports_image:
            return None
        kw = {}
        if getattr(b.channel, "sticker_by_row", False):  # 微信：不发图片文件，发的是账号「自定义表情」里的这一格（要目录里记的位置和指纹）
            kw["sticker"] = self.stickers.wx_entry(b, row)
            if kw["sticker"] is None:
                self.log("warn", "sticker", f"[{where}] 发表情包没成功：「自定义表情」目录里没有这一张")
                return None
        ids = await b.channel.send_image(chat_type, chat_id, data, reply_to=quote, **kw)
        if kw:
            self.stickers.wx_save(b, kw["sticker"])  # 重找过的，更新位置、补上新指纹
        return ids

    async def deliver_sticker(self, b: Bot, room: Room, row: dict, quote: str | None, mode: str = "") -> bool:
        if self.muted_in(b.aid, room.key):
            return False
        if mode in QUOTA_MODES and self.run_left(b, room) <= 0:  # 连着发的条数用完了：表情包也算一条
            self.log("debug", "reply", f"[{room.name}] {b.role['name']} 连着发的条数用完了，表情包不发")
            return False
        data = self.stickers.load(row)
        if not data or not b.channel or not b.channel.supports_image:
            return False
        self_name = b.channel.self_name or b.role["name"]
        e = Entry(b.uid, f"{self_name}〔{b.role['name']}本人〕", f"[表情包：{row.get('descr') or ''}]", bot=b.aid, role=b.rid)
        if self.S("shadow", b, room):
            self.push(room, e, "shadow", b.aid, "", b.rid)
            note = self.pick_note(row)
            self.log("info", "shadow", f"[{room.name}] {b.role['name']}（影子）本来会发表情包{f'（{note}）' if note else ''}：{row.get('descr')}", {"room": room.key, "mode": "sticker"})
            return False
        try:
            ids = await self.sticker_send(b, "group", room.chat_id, row, quote, room.name)
        except Muted:
            return False
        except Exception as ex:
            self.log("warn", "sticker", f"[{room.name}] 发表情包没成功：{ex}")
            return False
        if ids is None:
            return False
        e.ids = {b.aid: ids[0] if ids else ""}
        room.last_spoke[b.aid] = time.time()
        self.run_add(b, room)
        self.stickers.used(row["id"])
        self.push(room, e, "bot", b.aid, ",".join(ids), b.rid)
        note = self.pick_note(row)
        self.log("info", "reply", f"[{room.name}] {b.role['name']}（表情包{'，' + note if note else ''}）：{row.get('descr')}", {"room": room.key, "mode": "sticker"})
        return True

    async def deliver_sticker_dm(self, b: Bot, uid: str, name: str, row: dict, force: bool = False) -> bool:
        """私聊里发一张账号自己收藏的表情：发出去算连着发的一条（dm_run）；条数用完了就不发（回复文字至少已经发了一条）。"""
        if not force and self.run_left(b, None, uid) <= 0:
            self.log("debug", "reply", f"[私聊 {name}] {b.role['name']} 连着发的条数用完了，表情包不发")
            return False
        if self.S("shadow", b):
            self.push_dm(b.aid, uid, b.role["name"], f"[表情包：{row.get('descr') or ''}]", "shadow", b.rid)
            note = self.pick_note(row)
            self.log("info", "shadow", f"[私聊 {name}] {b.role['name']}（影子）本来会发表情包{f'（{note}）' if note else ''}：{row.get('descr')}")
            return False
        try:
            ids = await self.sticker_send(b, "private", uid, row, None, f"私聊 {name}")
        except Exception as ex:
            self.log("warn", "sticker", f"[私聊 {name}] 发表情包没成功：{ex}")
            return False
        if ids is None:
            return False
        self.run_add(b, None, uid)
        self.stickers.used(row["id"])
        self.push_dm(b.aid, uid, b.role["name"], f"[表情包：{row.get('descr') or ''}]", "bot", b.rid)
        note = self.pick_note(row)
        self.log("info", "reply", f"[私聊 {name}] {b.role['name']}（表情包{'，' + note if note else ''}）：{row.get('descr')}")
        return True

    async def deliver(self, b: Bot, room: Room, text: str, quote: str | None, mode: str, at=None, whole: bool = False) -> bool:
        self_name = b.channel.self_name if b.channel else b.role["name"]
        if self.muted_in(b.aid, room.key):
            self.log("debug", "reply", f"[{room.name}] {b.role['name']} 被禁言，没有发：{text[:40]}")
            return False
        e = Entry(b.uid, f"{self_name}〔{b.role['name']}本人〕", text, bot=b.aid, role=b.rid)
        if self.S("shadow", b, room) and mode != "manual":  # 面板里手动发言是人发的，不受影子模式限制
            self.push(room, e, "shadow", b.aid, "", b.rid)
            self.log("info", "shadow", f"[{room.name}] {b.role['name']}（影子）本来会说：{text}", {"room": room.key, "mode": mode})
            return False
        mention = None
        if mode != "manual" and getattr(b.channel, "supports_mention", False) and self.S("at_convert", b, room):
            mention = self.mention_candidates(room, b)  # 她写的「@名字」能和群里的成员对上的，发成真正的 @
        try:
            ids = await self.send_text(b, "group", room.chat_id, text, quote, room, at, mode=mode, mention=mention, whole=whole)
        except Muted:  # 平台说被禁言了（渠道已经记下禁言状态）：不当作发出去了，也不记进上下文
            self.log("info", "reply", f"[{room.name}] {b.role['name']} 发送被拒：被禁言了，不再尝试发言")
            return False
        if ids is None:  # 中间没人说话，连着发的条数用完了
            self.log("info", "reply", f"[{room.name}] {b.role['name']} 中间没人说话已经连着发了 {self.run_limit(b, room)} 条，这次{MODE_LABEL.get(mode, mode)}不发")
            return False
        e.ids = {b.aid: ids[0] if ids else ""}
        room.last_sent[b.aid] = ids
        room.last_spoke[b.aid] = time.time()
        if mode == "pro":
            room.pro_pending[b.aid] = {"ts": time.time(), "text": text, "after": 0}
        self.push(room, e, "bot", b.aid, ",".join(ids), b.rid)
        self.log("info", "reply", f"[{room.name}] {b.role['name']}（{MODE_LABEL.get(mode, mode)}）：{text[:80]}", {"room": room.key, "mode": mode})
        return True

    async def send_text(self, b: Bot, chat_type: str, chat_id: str, text: str, quote: str | None, room: Room | None, at=None,
                        mode: str = "", mention: dict | None = None, whole: bool = False) -> list[str] | None:
        """at: [(账号, 显示名)]，提醒用：QQ 发成真正的 @，别的平台在开头写「@名字」。
        群里的回复按「中间没人说话时最多连着发几条」算：剩下的条数不够就把最短的相邻几行并起来；剩下 0 条返回 None（没发）。
        私聊同理，按账号 × 对方各数各的；私聊里受限的回复（mode="at"）条数用完了也至少发一条，不返回 None。
        mention：{名字: 账号}，文字里写的「@名字」能对上的发成真正的 @（渠道支持时）。whole：整段当一条发，不按行拆（指令清单用）。"""
        extra = {}
        if at:
            if b.channel.supports_at:
                extra["at"] = at
            else:
                text = "".join(b.channel.at_prefix(u, n) for u, n in at) + text
        peer = chat_id if room is None else ""
        limited = mode in QUOTA_MODES
        cap = self.run_limit(b, room) or 10 ** 6  # 0 = 不限
        if limited:
            left = self.run_left(b, room, peer)
            if left <= 0 and mode != "at":
                return None
            cap = max(1, min(cap, left))  # 被@时哪怕条数用完了也回一条（别人@她就是有人说话，正常情况下条数早已清零）
        parts = [text]
        if self.S("split_enabled", b, room) and not whole:
            parts = split_reply(text, self.S("split_threshold", b, room), cap)
        ids = []
        for i, p in enumerate(parts):
            if i:
                wait = max(self.S("split_interval", b, room), len(p) / max(self.S("typing_speed", b, room), 0.1))
                await asyncio.sleep(min(wait, 8) * self.pace)
            kw = dict(extra) if i == 0 else {}
            if b.channel.supports_more:
                kw["more"] = i < len(parts) - 1  # 后面还有就先别切回原来的会话
            if mention:
                plan = self.mention_plan(p, mention)
                if plan:
                    kw["mention"] = plan
            ids += await b.channel.send(chat_type, chat_id, p, reply_to=quote if i == 0 else None, **kw)
            self.run_add(b, room, peer)
        return ids

    # ---------- 她写的「@名字」发成真正的 @ ----------
    def mention_candidates(self, room: Room, b: Bot) -> dict[str, str]:
        """群里能对上的成员：{名字: 账号}。来源是最近上下文里说过话的人（显示名，去掉〔X本人〕）和人物表里这些人的昵称 / 微信备注。
        我们自己的账号不算；一个名字对着不止一个人的不要（对不上就不转）。"""
        mine = set(self.bot_uids(room.platform))
        names: dict[str, set] = {}
        present: dict[str, str] = {}
        for x in room.ctx:
            if not x.uid or x.bot or str(x.uid) in mine:
                continue
            n = re.sub(r"〔.*?〕", "", x.name or "").strip()
            present[str(x.uid)] = n
            if n:
                names.setdefault(n, set()).add(str(x.uid))
        for p in self.people.persons.values():
            ours = {u for u in People.uids(p, room.platform) if u in present}
            if not ours:
                continue
            nick = ((p.get("nicks") or {}).get(room.platform) or "").strip()
            for n in ([nick] if nick else []) + ([str(r).strip() for r in p.get("wechat_remark") or []] if room.platform == "wechat" else []):
                if n:
                    names.setdefault(n, set()).update(ours)
        return {n: next(iter(u)) for n, u in names.items() if len(u) == 1 and n}

    @staticmethod
    def lead_mentions(text: str, cands: dict[str, str]) -> tuple[list[str], str]:
        """消息开头（前面只允许空白）连着的「@名字」，名字精确对上成员的：返回（这些人的账号，去掉这几段之后的正文）。对不上的停在那里，保持原样。"""
        uids, rest = [], text
        while True:
            for name in sorted(cands, key=len, reverse=True):
                m = re.match(r"\s*@" + re.escape(name) + MENTION_END, rest)
                if m:
                    uids.append(cands[name])
                    rest = rest[m.end():]
                    break
            else:
                return list(dict.fromkeys(uids)), rest.strip() if uids else text

    def wechat_addressing(self, bot: Bot, room: Room, msg: Msg):
        """微信的消息没有结构化的 @ 和回复：把开头的「@名字」认成 @ 了谁（记进 at_ids、从正文里去掉，和 QQ 一样显示成「（@某某）正文」），
        引用消息里被引用的人认成回复了谁（reply_to_uid）。名字要精确对上群里的成员；对不上、同名对着不止一个人的保持原样。"""
        cands = self.mention_candidates(room, bot)
        uids, rest = self.lead_mentions(msg.text, cands)
        if uids:
            msg.text, msg.at_ids = rest, [*msg.at_ids, *uids]
        name = (getattr(msg, "reply_to_name", "") or "").strip()
        if name and not msg.reply_to_uid:
            msg.reply_to_uid = bot.uid if name == getattr(bot.channel, "self_name", None) else cands.get(name, "")

    @staticmethod
    def mention_plan(text: str, cands: dict[str, str]) -> list[tuple[str, str]]:
        """文字里按先后顺序出现的「@名字」，名字精确对上成员、后面是结尾 / 空白 / 标点的，给出 [(账号, 名字)]。"""
        hits = []
        taken: list[tuple[int, int]] = []
        for name in sorted(cands, key=len, reverse=True):
            for m in re.finditer("@" + re.escape(name) + MENTION_END, text):
                if any(m.start() < b and a < m.end() for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                hits.append((m.start(), cands[name], name))
        return [(uid, name) for _, uid, name in sorted(hits)]

    async def describe_bytes(self, b: Bot, room: Room | None, data: bytes) -> str:
        """看图模型说一句图里是什么。同一张图（内容的 sha1）+ 同一个看图模型只看一次，描述记在 vis_cache 里，和聊天记录一起清；只记成功的。"""
        return await self._describe_cached(b, room, data, hashlib.sha1(data).hexdigest())

    async def describe_shot(self, b: Bot, room: Room | None, data: bytes, key: str = "", moving: bool = False) -> str:
        """截屏来的表情（微信）：缓存键改用 dhash（`dh:` 开头，见 wxshot.cache_key；用裁出来的第一帧算），只按完全相同命中，
        不做「相近也算」。截屏的背景和位置每次都可能有点差别，按 sha1 存缓存几乎永远对不上；普通图片、QQ 图片仍按 sha1。
        moving：是动图（交给看图的是三帧拼好的一张），提示语里加一句说明。"""
        from .wxshot import cache_key
        return await self._describe_cached(b, room, data, key or cache_key(data), moving)

    async def _describe_cached(self, b: Bot, room: Room | None, data: bytes, key: str, moving: bool = False) -> str:
        platform = room.platform if room else b.platform
        chat = room.key if room else None
        model = self.settings.get("vis_model", b.rid, platform, chat)
        row = self.db.x("SELECT descr FROM vis_cache WHERE sha=? AND model=?", (key, model)).fetchone()
        if row:
            return row[0]
        descr = await self._describe_bytes(b, room, data, platform, chat, moving)
        if isinstance(descr, str) and descr.strip():
            self.db.x("INSERT OR REPLACE INTO vis_cache (sha, model, descr, ts) VALUES (?,?,?,?)", (key, model, descr, time.time()))
        return descr

    async def _describe_bytes(self, b: Bot, room: Room | None, data: bytes, platform: str, chat, moving: bool = False) -> str:
        durl = f"data:image/{ext_of(data).replace('jpg', 'jpeg')};base64," + base64.b64encode(data).decode()
        search = bool(self.S("vis_search", room=room, platform=platform)) and self.search.available()[0]  # 开着（且搜索服务能用）：看图模型自己决定要不要上网查，算进每天搜索次数
        text = "用一两句中文说清这张图里是什么；有文字就把文字写出来。" + ("需要时可以用 web_search 查图里的东西；不要拿图里的人名、账号、聊天内容去搜。" if search else "")
        if moving:
            text += "这是一张动图按时间先后截的三帧，描述它的内容和动作。"
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": durl}}, {"type": "text", "text": text}]}]
        if search:
            return await self.search.vis_chat(msgs, b.rid, platform, chat, rounds=max(1, int(self.settings.g("search_rounds") or 2)))
        return await self.llm.chat("vis", msgs, b.rid, platform, chat)

    async def describe_images(self, b: Bot, room: Room | None, msg: Msg) -> str:
        """room 是 None：私聊。"""
        tag = lambda url: "表情" if url in msg.emotes else "图片"
        if not (self.S("vision_enabled", room=room, platform=msg.platform) and self.S("vis_model", room=room, platform=msg.platform)) or self.llm.hard_capped():
            return "".join(f"[{tag(u)}]" for u in msg.images[:3])
        stickers_too = bool(self.S("vision_stickers", room=room, platform=msg.platform))  # 「群友发的表情也看」关着：表情不看，只写「[表情]」
        out = []
        for url in msg.images[:2]:
            if tag(url) == "表情" and not stickers_too:
                out.append("[表情]")
                continue
            try:
                data = await b.channel.fetch_image(url)
                if not data:
                    out.append(f"[{tag(url)}]")
                    continue
                if url in msg.shots:  # 截屏来的微信表情按裁出来的第一帧 dhash 缓存，动图在提示语里说明是三帧
                    descr = await self.describe_shot(b, room, data, msg.shot_keys.get(url, ""), bool(msg.moving))
                else:
                    descr = await self.describe_bytes(b, room, data)
                out.append(f"[{tag(url)}：{descr}]")
            except Exception as ex:
                self.log("warn", "model", f"看图失败：{ex}")
                out.append(f"[{tag(url)}]")
        return "".join(out)

    # ---------- 私聊 ----------
    # ---------- 私聊记录（消息页用）----------
    # 私聊也存进 messages，房间名是 dm:账号id:对方id。只用来显示和手动接管；模型看的私聊上下文仍是 private_hist，
    # 不进群聊的 ctx、不占群聊的上下文条数，也不进记忆整理。
    @staticmethod
    def dm_key(aid: str, uid: str) -> str:
        return f"dm:{aid}:{uid}"

    def push_dm(self, aid: str, uid: str, name: str, text: str, kind: str, role_id: str = "", ts: float | None = None):
        key, ts = self.dm_key(aid, uid), ts or time.time()
        cur = self.db.x("INSERT INTO messages (room, ts, account_id, msg_id, user_id, name, text, role_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                        (key, ts, aid, "", uid, name, text, role_id, kind))
        self.emit("msg", {"room": key, "room_name": name, "id": cur.lastrowid, "ts": ts, "name": name, "text": text,
                          "kind": kind, "uid": uid, "account": aid, "role": role_id})

    async def handle_private(self, b: Bot, msg: Msg, force: bool = False):
        """私聊来了一条。force（面板上「让她回这条」）：马上回，不等。
        别的：先记下这条；对方停下来够「私聊里等对方停几秒再回」才开始回（这期间他又发了就重新计时，从这一串第一条算起到了「最多等几秒」就不再等），
        对方这一串没回过的几条合成一次回。"私聊里两次回复最小间隔"照旧，和等对方说完各算各的，取更严的。"""
        if force:
            return await self._handle_private(b, [msg], True)
        if msg.images and self.S("vision_enabled", platform=msg.platform) and self.S("vis_model", platform=msg.platform):
            msg.text = (msg.text + " " + await self.describe_images(b, None, msg)).strip()
        if msg.text.strip():
            self.push_dm(b.aid, msg.user_id, msg.user_name, msg.text, "human", ts=msg.ts)
            if await self.private_command(b, msg):
                return
        key = f"{b.aid}:{msg.user_id}"
        wait = int(self.S("private_wait", b) or 0)
        if wait <= 0:  # 不等：来一条回一条（和以前一样）；「两次回复最小间隔」照旧排队
            gap = int(self.S("private_reply_gap", b) or 0)
            if gap <= 0:
                return await self._handle_private(b, [msg])
            async with self.dm_locks.setdefault(key, asyncio.Lock()):
                left = self.dm_last.get(key, 0) + gap - time.time()
                if left > 0:
                    await asyncio.sleep(left)
                await self._handle_private(b, [msg])
                self.dm_last[key] = time.time()
            return
        st = self.dm_pend.setdefault(key, {"msgs": [], "first": msg.ts, "task": None, "running": False})
        if not st["msgs"]:
            st["first"] = msg.ts
        st["msgs"].append(msg)
        if st["running"]:  # 正在写上一次的回复：写好时看有没有新的，不为它另起一次
            return
        if st["task"]:
            st["task"].cancel()  # 对方又发了：重新计时
        delay = min(msg.ts + wait, st["first"] + int(self.S("pro_wait_max", b) or 0)) - time.time()
        if delay > 0:
            self.log("info", "reply", f"[私聊 {msg.user_name}] 对方的话可能还没说完，{delay:.0f} 秒后没有新消息再回")
        st["task"] = self.spawn(self._dm_go(b, key, delay))

    async def private_command(self, b: Bot, msg: Msg) -> bool:
        """私聊里的指令：换、换能换的、看图也换、不换、一直不换、换模型、查更新只认用户卡本人，指令、帮助谁都能问；
        办了返回 True（不当聊天）。没有可办的（比如没有待回复的提议）、别人说同样的话：返回 False，照常聊天。"""
        cmd = commands.parse_private(msg.text)
        if not cmd:
            return False
        name, args = cmd
        person = self.people.find(msg.platform, msg.user_id)
        user = self.people.user_card()
        owner = bool(person and user and person["id"] == user["id"])
        if name == "help":
            scope = self.S("private_scope", b)
            if not owner and (scope == "off" or (scope == "list" and not person)):
                return False
            reply = commands.help_text(self.S("cmd_perms", b), is_owner=False, person=person, sender_role="", private=True, user_card=owner)
        elif not owner:
            return False
        else:
            reply = await self.owner_command(name, args)
            if reply is None:
                return False
        self.log("info", "command", f"[私聊 {msg.user_name}] {name}")
        b = self.duty(b, None)
        await self.send_text(b, "private", msg.user_id, reply, None, None, whole=True)
        self.push_dm(b.aid, msg.user_id, b.role["name"], reply, "bot", b.rid)
        return True

    async def owner_command(self, name: str, args: tuple) -> str | None:
        w = self.watch
        if name == "replace":
            p = w.pending()
            return (await w.replace(p["eid"], p["old"], p["new"]))["text"] if p else None
        if name == "replace_able":
            part = w.st()["partial"]
            return (await w.replace(part["eid"], part["old"], part["new"], only_able=True))["text"] if part else None
        if name == "vision_too":
            p = w.latest()
            return (await w.replace_vision(p))["text"] if p and w.vision_ok_for(p) else None
        if name == "snooze":
            p = w.pending()
            return w.snooze(p, int(args[0]) if args[0] else None) if p else None
        if name == "never":
            p = w.pending()
            return w.never(p) if p else None
        if name == "swap":
            return await self.swap_model(*args)
        if name == "check":
            return await self.check_updates()
        return None

    async def swap_model(self, old: str, new: str) -> str:
        """私聊「换模型 旧 新」：在用旧模型的那家服务商上，换成新模型（新模型要在那家的模型列表里）。"""
        w = self.watch
        eids = list(dict.fromkeys(u["eid"] for u in w.uses() if u["model"] == old))
        if not eids:
            return f"没换：现在没有地方在用 {old}。"
        for eid in eids:
            if new in await w.model_ids(eid):
                return (await w.replace(eid, old, new))["text"]
        return f"没换：{new} 不在「{w.ep_name(eids[0])}」的模型列表里。"

    async def check_updates(self) -> str:
        """私聊「查更新」：立刻查一次新模型和新版本；新版本照旧只提醒。"""
        w = self.watch
        report = await w.check(force=True)
        lines = [w.summary(report)]
        now = time.time()
        s = w.st()
        fresh = [p for p in s["props"].values() if p["state"] == "" and not p["notified"]]
        for p in fresh:  # 这次新发现的提议直接写在回复里，不再另发一条通知
            lines.append(w.text_of(p))
            p["notified"] = now
        waiting = sum(1 for p in s["props"].values() if p["state"] == "" and p["notified"] and p not in fresh)
        if waiting:
            lines.append(f"还有 {waiting} 条新模型提议等你回复。")
        w.save(s)
        info = await self.update_hook(True) if self.update_hook else None
        if not info:
            lines.append("新版本：没查（设置里关了提醒，或没有可查的仓库）。")
        elif info.get("error"):
            lines.append(f"新版本：没查到（{info['error']}）。")
        elif info.get("newer"):
            lines.append(f"新版本：有新版本 {info['latest']}（当前 {info['current']}）：{info['url']}（只提醒，不会自动下载或安装）")
        else:
            lines.append(f"新版本：已是最新（{info['current']}）。")
        return "\n".join(lines)

    async def _dm_go(self, b: Bot, key: str, delay: float):
        st = self.dm_pend[key]
        if delay > 0:
            await asyncio.sleep(delay)
        st["task"], st["running"] = None, True
        try:
            while st["msgs"]:
                gap = int(self.S("private_reply_gap", b) or 0)
                if gap > 0:
                    left = self.dm_last.get(key, 0) + gap - time.time()
                    if left > 0:
                        await asyncio.sleep(left)  # 这期间对方又发的，也并进这一次
                batch, st["msgs"] = st["msgs"], []
                await self._handle_private(b, batch, False, st)
                self.dm_last[key] = time.time()
        finally:
            st["running"] = False
            if not st["msgs"] and not st["task"]:
                self.dm_pend.pop(key, None)

    async def dm_stale(self, b: Bot, system: str, hist: list, text: str, reply: str, new: list, took: float) -> tuple[str, bool]:
        """私聊写回复期间对方又发了：发送前按最新的情况重新判断，照发、改写或不发（和群里「回复写好时已过时」一个办法）。
        返回（回复, 新来的几条算不算已经顾到了）：照发、改写 = 顾到了，不再另外回；不发了 = 没顾到，连同前面的话一起重新回；判断出错照发（新的另外回）。"""
        prompt = (f"【重新判断这条回复】你刚写好了一条回复，是针对对方发来的话：「{text}」写的：\n「{reply}」\n写它花了 {int(took)} 秒，期间对方又发来了 {len(new)} 条：\n"
                  + "\n".join(m.text for m in new)
                  + "\n按最新的情况重新判断：照发（新的这几条就不另外回了，所以这条回复要顾得到它们）、改写（把新的话也顾到）、或者不发了（新的话和这条回复对不上，之后会连同前面的话一起重新回）。\n"
                  '只输出JSON：{"action": "send（照发）或 rewrite（改写）或 drop（不发了）", "text": "改写后的回复，只有 rewrite 才写", "why": "一句话理由"}')
        try:
            d = parse_json(await self.llm.chat("main", [{"role": "system", "content": system}] + hist + [{"role": "user", "content": prompt}],
                                               b.rid, b.platform, None, tag="stale"))
        except UsageCapReached:
            return reply, False
        except Exception as ex:
            self.log("warn", "model", f"[私聊] 过时回复的重新判断出错（照发）：{ex}")
            return reply, False
        action = str(d.get("action", "send")).split("（")[0].strip().lower()
        self.log("info", "reply", f"[私聊] {b.role['name']} 回复写好时已过时（{len(new)} 条新消息，{int(took)} 秒）→ "
                 f"{ {'rewrite': '改写', 'drop': '不发了'}.get(action, '照发') }：{str(d.get('why', ''))[:40]}")
        if action == "drop":
            return "", False
        if action == "rewrite" and str(d.get("text", "")).strip():
            return self.postprocess(b, None, str(d["text"])), True
        return reply, True

    async def _handle_private(self, b: Bot, batch: list, force: bool = False, st: dict | None = None):
        """batch：对方这一串没回过的几条，合成一次回。force：面板上点了「让她回这条」——照常回，不看「私聊对象」范围，也不再把这句话重复记一遍。
        st：这个私聊的等待状态；写回复期间又来的新消息在 st["msgs"] 里。"""
        msg = batch[-1]
        hist = self.dm_hist(b.aid, msg.user_id, batch[0].ts)  # 先取历史（刚重启时从库里回填）；这一串已经记进库里，所以只取这一串之前的
        b = self.duty(b, None)
        scope = self.S("private_scope", b)
        person = self.people.find(msg.platform, msg.user_id)
        batch = [m for m in batch if not self.blocked(m.text, b)]
        text = "\n".join(m.text for m in batch if m.text.strip())
        if (not force and (scope == "off" or (scope == "list" and not person))) or not text.strip():
            return
        if self.llm.hard_capped(b.rid):
            await self.cap_reply(b, None, None, msg.user_id)
            return
        n = self.S("max_history", b) * 2
        notes = [await self.act(b, None, Entry(msg.user_id, msg.user_name, m.text), msg.platform, "private", msg.user_id) for m in batch]
        note = "\n".join(x for x in notes if x)

        async def build_system() -> str:
            s = await self.system_prompt(b, None, person, msg.user_id, msg.user_name, query=text,
                                         extra_chars=sum(len(h["content"]) for h in hist[-n:]) + len(text) + 800)
            if person and self.S("private_owner_note", b):
                s += f"\n\n这次私聊的是{person['name']}本人。"
            return s + ("\n\n" + note if note else "")
        system = await build_system()
        messages = [{"role": "system", "content": system}] + hist[-n:] + [{"role": "user", "content": text}]
        skey = (self.dm_key(b.aid, msg.user_id), b.aid)
        self.stk_out.pop(skey, None)
        self.stk_cand.pop(skey, None)
        t0 = time.time()
        try:
            raw = await self.complete(b, None, messages, rebuild=build_system, called=True, peer=msg.user_id)
            reply = self.postprocess(b, None, self.take_sticker_choice(skey, raw, f"私聊 {msg.user_name}", b))
        except UsageCapReached:
            await self.cap_reply(b, None, None, msg.user_id)
            return
        except Exception as ex:
            self.log("error", "model", f"私聊 {msg.user_name} 生成失败：{ex}")
            await self.model_failed(b, None, ex, "private", msg.user_id, True)
            return
        stickers = self.stk_out.pop(skey, [])
        if not reply and not stickers:
            return
        new = st["msgs"] if st is not None else []  # 写回复期间对方又发的
        if reply and new and self.S("stale_on", b):
            reply, covered = await self.dm_stale(b, system, hist[-n:], text, reply, list(new), time.time() - t0)
            if covered:
                text += "\n" + "\n".join(m.text for m in new if m.text.strip())
                new.clear()  # 照发 / 改写：新来的已经顾到了，不再为它们另外回
            elif not reply:
                st["msgs"] = list(batch) + new  # 不发了：连同这一串一起重新回
                return
        if not reply and not stickers:
            return
        mark = reply or f"[表情包：{stickers[0].get('descr') or ''}]"
        hist += [{"role": "user", "content": text}, {"role": "assistant", "content": mark}]
        del hist[:-n]
        if self.S("shadow", b):
            self.log("info", "shadow", f"[私聊 {msg.user_name}] {b.role['name']}（影子）本来会说：{reply}")
            if reply:
                self.push_dm(b.aid, msg.user_id, b.role["name"], reply, "shadow", b.rid)
        elif reply:
            await self.send_text(b, "private", msg.user_id, reply, None, None, mode="" if force else "at")  # 回对方的话：按群里被@的规则，条数用完也发一条；面板让她回的不受限
            self.push_dm(b.aid, msg.user_id, b.role["name"], reply, "bot", b.rid)
            self.log("info", "reply", f"[私聊 {msg.user_name}] {b.role['name']}：{reply[:80]}")
        for row in stickers[:1]:
            await self.deliver_sticker_dm(b, msg.user_id, msg.user_name, row, force)

    # ---------- 管理指令 ----------
    async def run_command(self, b: Bot, room: Room, e: Entry, msg: Msg, cmd: str, arg: str, force: bool = False):
        person = self.people.find(room.platform, msg.user_id)
        perms = self.S("cmd_perms", b)
        if cmd == "help":  # 谁都能问，只列他在这里能用的
            reply = commands.help_text(perms, is_owner=bool(person and person.get("admin")), person=person, sender_role=msg.sender_role, private=False)
            self.log("info", "command", f"[{room.name}] {e.name} → {b.role['name']}：指令清单")
            await self.deliver(b, room, reply, e.ids.get(b.aid), "command", whole=True)
            return
        ok = force or commands.allowed(cmd, perms, is_owner=bool(person and person.get("admin")), person=person,
                                       sender_role=msg.sender_role)  # force：自定义指令已经按它自己的权限判断过了
        if cmd == "forget_me" and person and person.get("admin"):
            ok = False
        label = COMMANDS.get(cmd, "忘掉我")
        if ok and cmd == "forget" and len(arg.strip()) < 2:  # 太短会匹配到几乎所有记忆：不执行，让她反问
            self.log("info", "command", f"[{room.name}] {e.name} → {b.role['name']}：{label} {arg}（内容太短，没执行）")
            if self.can_talk(b, room):
                await self.speak(b, room, "refuse", e, brief="想让她忘掉的内容没说清楚（太短了，会误伤很多记忆）；请她反问一句：具体要忘掉什么？")
            return
        self.log("info", "command", f"[{room.name}] {e.name} → {b.role['name']}：{label} {arg}（{'执行' if ok else '无权限'}）")
        if not ok:
            if self.can_talk(b, room):
                await self.speak(b, room, "refuse", e, brief=label if cmd != "forget_me" else "忘掉关于自己的记忆（管理员的人物卡只能在面板里改）")
            return
        scope = f"acct:{b.aid}|chat:{room.key}"  # 「这个账号在本群」：不管谁在岗都生效，换班后照样有效，别的账号不受影响
        reply = ""
        if cmd == "mute":
            mins = int(arg or 30)
            room.mute[b.aid] = time.time() + mins * 60
            self.save_room_state(room)
            reply = f"好，闭嘴{mins}分钟。"
        elif cmd == "unmute":
            room.mute.pop(b.aid, None)
            self.save_room_state(room)
            reply = "我回来了。"
        elif cmd == "persona":
            (kind, target), mins = self.switch_target(arg)
            if mins:
                until = time.time() + mins * 60
            else:  # 不带时长：到当天结束
                lt = time.localtime()
                until = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
            self.duty_rules.set_override(b.aid, room.key, until, role=target["id"] if kind == "role" else None,
                                         persona=target["id"] if kind == "persona" else None)
            b = self.duty(self.bots[b.aid], room)  # 确认回复按换完之后的角色署名
            reply = f"好，换成「{target['name']}」，到{time.strftime('%m-%d %H:%M', time.localtime(until))}。到时候按排班换回来。"
        elif cmd == "persona_back":
            self.duty_rules.clear_override(b.aid, room.key)
            b = self.duty(self.bots[b.aid], room)
            reply = "好，按排班换回来了。"
        elif cmd == "proactive":
            on = arg in ("开", "打开", "开启")
            self.settings.set(scope, "proactive", on)
            reply = f"本群插话已{'打开' if on else '关闭'}（只管我这个账号在本群，换班后照样有效，别的账号不受影响；面板的群页里能清除）。"
        elif cmd == "eagerness":
            self.settings.set(scope, "eagerness", int(arg))
            reply = f"插话力度：{EAGER_LABEL[int(arg)]}（只管我这个账号在本群，换班后照样有效；面板的群页里能清除）。"
        elif cmd == "status":
            left = room.mute.get(b.aid, 0) - time.time()
            reply = (f"状态：{'闭嘴中，还剩' + str(int(left // 60) + 1) + '分钟' if left > 0 else '正常'}；"
                     f"插话{'开' if self.S('proactive', b, room) else '关'}，力度{EAGER_LABEL.get(self.eager_level(b, room))}"
                     f"{'，被叫停了还剩' + str(int((room.pause_until - time.time()) // 60) + 1) + '分钟' if room.pause_until > time.time() else ''}；"
                     f"此刻是「{b.role['name']}」（{ {'override': '临时换的', 'default': '默认角色'}.get(b.duty_src, '排班规则')}），"
                     f"人设「{self.persona_for(b, room).get('name', '')}」；今天用了 {self.llm.tokens_today(b.rid)} tokens。")
        elif cmd == "recall":
            ok2 = await self.recall_last(b, room)
            if ok2 is None:
                reply = "这个平台撤回不了。"
            elif not ok2:
                reply = "没有可撤回的。"
            else:
                return
        elif cmd == "forget":
            spaces = sorted({self.memory.space(k, b.rid, room.platform, room.key) for k in ("person", "recap", "meme")})
            if self.reset:  # 删之前复用「重置记忆」的备份：面板记忆页能撤销
                info = self.reset.run({"type": "text", "id": arg, "spaces": spaces}, ["person", "recap", "meme"])
                n = info["counts"] and sum(info["counts"].values())
            else:
                n = self.memory.forget_text(arg, spaces)
            reply = f"忘掉了 {n} 条关于「{arg}」的记忆。" + ("（删之前备份过，管理员可以在面板的记忆页撤销）" if n else "")
        elif cmd == "forget_me":
            # 他的备忘、印象，和前情里含他的单位（连同覆盖这些单位的上层）一起删；走「重置记忆」的备份，管理员能在记忆页撤销
            key = subject_key(room.platform, msg.user_id, person)
            if person:
                subs, accts = self.reset.person_keys(person["id"]) if self.reset else ([key], [(room.platform, msg.user_id)])
                names = [person.get("name") or ""] + self.people.calls_of(person)
            else:
                subs, accts, names = [key], [(room.platform, msg.user_id)], [re.sub(r"〔.*?〕", "", e.name).strip()]
            if self.reset:
                info = self.reset.run({"type": "subject", "id": key, "subjects": subs, "accts": accts, "names": names, "label": names[0]}, ["person", "recap"])
                n = sum(info["counts"].values())
            else:
                n = self.memory.forget_subject([key])
            self.reminders.forget_user(room.platform, msg.user_id)
            reply = f"好，关于你的 {n} 条记忆（含前情里有你的部分）和你挂的提醒都删了。"
        if reply:
            await self.deliver(b, room, reply, e.ids.get(b.aid), "command")

    def switch_target(self, arg: str):
        """「换××（时长）」：先找角色，再找人设。返回（(种类, 目标), 分钟）；找不到时目标为 None。"""
        m = re.match(r"^(.+?)\s*(\d+(?:\.\d+)?)\s*(个?小时|h|分钟|min)$", arg.strip())
        name, mins = (m.group(1).strip(), float(m.group(2)) * (60 if m.group(3) in ("小时", "个小时", "h") else 1)) if m else (arg.strip(), 0)
        if not name:
            return (None, None), 0
        for kind, pool in (("role", self.roles.values()), ("persona", self.personas.values())):
            hit = next((x for x in pool if x["name"] == name), None) or next((x for x in pool if name in x["name"]), None)
            if hit:
                return (kind, hit), mins
        return (None, None), 0

    async def recall_last(self, b: Bot, room: Room) -> bool | None:
        if not b.channel.supports_recall:
            return None
        ids = room.last_sent.pop(b.aid, None)
        if not ids:
            return False
        for i in ids:
            await b.channel.recall("group", room.chat_id, i)
        for e in reversed(room.ctx):
            if e.bot == b.aid:
                room.ctx.remove(e)
                break
        self.log("info", "reply", f"[{room.name}] {b.role['name']} 撤回了上一条")
        return True

    # ---------- 面板：人工接管 ----------
    async def manual_send(self, aid: str, room_key: str, text: str):
        if room_key.startswith("dm:"):  # 私聊：以这个账号当前的角色身份直接发
            uid = room_key.split(":", 2)[2]
            b = self.duty(self.bots[aid], None)
            await self.send_text(b, "private", uid, text, None, None)
            self.push_dm(aid, uid, b.role["name"], text, "bot", b.rid)
            self.log("info", "reply", f"[私聊 {uid}] {b.role['name']}（手动）：{text[:80]}")
            return
        room = self.rooms[room_key]
        if self.muted_in(aid, room_key):
            raise Muted("被禁言，没有发出")
        if not await self.deliver(self.duty(self.bots[aid], room), room, text, None, "manual") and self.muted_in(aid, room_key):
            raise Muted("被禁言，没有发出")

    def force_reply(self, aid: str, room_key: str, msg_db_id: int):
        if room_key.startswith("dm:"):  # 私聊：让这个账号照这句话回一次
            uid = room_key.split(":", 2)[2]
            b = self.bots[aid]
            r = self.db.x("SELECT name, text FROM messages WHERE id=? AND room=?", (msg_db_id, room_key)).fetchone()
            if not r:
                raise KeyError(msg_db_id)
            self.spawn(self.handle_private(b, Msg(b.platform, aid, "private", uid, uid, r["name"], r["text"]), force=True))
            return
        room = self.rooms[room_key]
        b = self.duty(self.bots[aid], room)
        r = self.db.x("SELECT user_id, name, text FROM messages WHERE id=?", (msg_db_id,)).fetchone()
        e = next((x for x in room.ctx if x.text == r["text"] and x.uid == r["user_id"]), None) or Entry(r["user_id"], r["name"], r["text"])
        room.queue.put_nowait(("force", e, b))

    # ---------- 试聊 ----------
    async def trial(self, role_id: str, platform: str, lines: list[dict], mode: str) -> dict:
        """lines: [{name, person_id?, text}]，最后一条是要回应的消息。mode: at / auto。"""
        acc = {"id": "_trial", "platform": platform, "role_id": role_id}
        b = Bot(self, acc, None)
        room = Room(f"{platform}:_trial", platform, "_trial", "试聊群")
        for ln in lines:
            p = self.people.persons.get(ln.get("person_id") or "")
            uid = (self.people.uids(p, platform) or [""])[0] if p else f"u{abs(hash(ln['name'])) % 10 ** 6}"
            name = f"{ln['name']}〔{p['name']}本人〕" if p else ln["name"]
            room.ctx.append(Entry(str(uid), name, ln["text"]))
        e = room.ctx[-1]
        before = self.usage_snapshot()
        out = {"gatekeeper": None, "reply": ""}
        if mode == "auto":
            persons, sib, _, _ = self.present(room, b)
            marks = self.people.marks(persons, sib, b.role)
            prompt = fill(self.settings.g("gatekeeper_prompt") or GATEKEEPER_PROMPT, summary=b.role.get("summary", ""), user_line=self.user_line(b),
                          marks=f"；{marks}。" if marks else "。", context=self.ctx_lines(room, b, "她（自己）"),
                          speaker=e.name, text=e.text, 平台群=personas.plat_words(platform)[1])
            d = parse_json(await self.llm.chat("gk", [{"role": "user", "content": prompt}], role_id, platform, None, tag="trial"))
            out["gatekeeper"] = {"reply": bool(d.get("reply")), "brief": str(d.get("brief", ""))}
            if not out["gatekeeper"]["reply"]:
                out["usage"] = self.usage_diff(before)
                return out
            out["reply"] = await self.generate(b, room, "pro", e, brief=out["gatekeeper"]["brief"], tag="trial")
        else:
            out["reply"] = await self.generate(b, room, "at", e, how="@了你", tag="trial")
        out["system"] = await self.system_prompt(b, room)
        out["usage"] = self.usage_diff(before)
        return out

    def usage_snapshot(self):
        return self.db.x("SELECT COALESCE(SUM(calls),0), COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0), COALESCE(SUM(completion),0) FROM usage WHERE purpose='trial'").fetchone()

    def usage_diff(self, before):
        now = self.usage_snapshot()
        return dict(zip(["calls", "prompt", "cached", "completion"], [a - b for a, b in zip(now, before)]))

    # ---------- 记忆 ----------
    def mem_tick(self, room: Room):
        """每攒够 memory_segment 条消息整理一段。整理没成功的这一段留着重试（隔一会儿再试），连着失败 3 次就把原话存成第一层单位——不漏、不丢。"""
        seg = self.S("memory_segment", room=room)
        if (room.mem_busy or room.mem_count < seg or not self.memory.enabled(room.platform, room.key) or self.llm.hard_capped()
                or time.time() < room.mem_retry_at):
            return
        last = self.db.kv(f"memlast:{room.key}", 0)
        rows = self.db.rows("""SELECT id, ts, user_id, name, text, role_id FROM messages WHERE room=? AND id>?
                               AND kind IN ('human','bot','import') ORDER BY id LIMIT ?""", (room.key, last, seg))
        if len(rows) < seg:
            room.mem_count = len(rows)
            return
        room.mem_busy = True
        room.mem_count = 0
        self.db.set_kv(f"memlast:{room.key}", rows[-1]["id"])
        roles = sorted({b.rid for b in self.room_bots(room) if b.rid} | {r["role_id"] for r in rows if r["role_id"]})

        def covered():
            room.mem_cov, room.mem_fail = rows[-1]["id"], 0
            self.db.set_kv(f"memcov:{room.key}", rows[-1]["id"])

        async def job():
            try:
                await self.memory.extract(room.key, room.name, rows, roles)
                covered()
            except UsageCapReached:
                self.db.set_kv(f"memlast:{room.key}", last)  # 今天的用量到上限了：不算失败，留着明天再整理
                room.mem_count, room.mem_retry_at = seg, time.time() + 1800
            except Exception as ex:
                room.mem_fail += 1
                self.log("warn", "memory", f"[{room.name}] 整理记忆没成功（第 {room.mem_fail} 次）：{ex}")
                if room.mem_fail >= 3:
                    self.memory.store_raw_segment(room.key, rows)
                    covered()
                    self.log("warn", "memory", f"[{room.name}] 这一段连着 3 次没整理成，已把原话存成前情，不会丢")
                else:
                    self.db.set_kv(f"memlast:{room.key}", last)
                    room.mem_count, room.mem_retry_at = seg, time.time() + 300 * room.mem_fail
            finally:
                room.mem_busy = False
        self.spawn(job())

    async def import_chatlog(self, room_key: str, items: list[dict], role_ids: list[str]):
        platform, chat_id = room_key.split(":", 1)
        room = self.room(platform, chat_id)
        seg = self.S("memory_segment", room=room)
        ts = time.time() - len(items)
        for i, m in enumerate(items):
            name, _ = self.display_name(platform, m["user_id"], m["name"])
            self.db.x("INSERT INTO messages (room, ts, account_id, msg_id, user_id, name, text, role_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                      (room_key, ts + i, "", "", m["user_id"], name, m["text"][:500], "", "import"))
        rows = self.db.rows("SELECT id, ts, user_id, name, text, role_id FROM messages WHERE room=? AND kind='import' ORDER BY id DESC LIMIT ?",
                            (room_key, len(items)))[::-1]
        done = 0
        for i in range(0, len(rows), seg):
            await self.memory.extract(room_key, room.name or room_key, rows[i:i + seg], role_ids)
            done += 1
            self.emit("import", {"room": room_key, "done": min(len(rows), i + seg), "total": len(rows)})
        self.embedder.kick()
        return done

    # ---------- 定时：冷场开话题、定时消息 ----------
    async def tick(self):
        now = time.time()
        lt = time.localtime(now)
        hm = time.strftime("%H:%M", lt)
        day = time.strftime("%Y-%m-%d", lt)
        await self.fire_reminders()
        await self.recheck_bans()
        self.fav_tick(now)
        for room in list(self.rooms.values()):
            for aid in [a for a, p in room.pro_pending.items() if now - p["ts"] > 1800]:
                room.pro_pending.pop(aid, None)
            doc = self.chat_doc(room) or {}
            room.sched_done = {k for k in room.sched_done if k.startswith(day)}  # 跨天清掉
            for i, s in enumerate(doc.get("schedules") or []):
                if not s.get("on", True) or s.get("time") != hm:
                    continue
                if s.get("days") and lt.tm_wday not in s["days"]:
                    continue
                k = f"{day} {hm} {i}"
                if k in room.sched_done:  # 同一分钟里 tick 会跑两轮；每条各记各的，发过的不再发
                    continue
                room.sched_done.add(k)
                b = self.bots.get(s.get("account"))
                b = self.duty(b, room) if b else next(iter(self.room_bots(room)), None)
                if b and b.online and self.can_talk(b, room):
                    if s.get("mode") == "ai":
                        room.queue.put_nowait(("job", self._sched_ai(b, room, s.get("content", "")), None))
                    else:
                        room.queue.put_nowait(("job", self.deliver(b, room, s.get("content", ""), None, "schedule"), None))
            for b in self.room_bots(room):  # 换班：在岗角色变了
                last_rid = room.duty_last.get(b.aid)
                room.duty_last[b.aid] = b.rid
                if last_rid is not None and last_rid != b.rid:
                    self.spawn(self.on_shift(b, room, last_rid))
            for b in self.room_bots(room):
                if b.channel.reply_only or not self.S("cold_start", b, room) or not in_hours(self.S("cold_hours", b, room)):
                    continue
                last = room.ctx[-1] if room.ctx else None
                if not last or last.bot or now - last.ts < self.S("cold_minutes", b, room) * 60:
                    continue
                if room.cold[0] != day:
                    room.cold = [day, 0]
                if room.cold[1] >= self.S("cold_daily_max", b, room) or not self.can_talk(b, room) or self.llm.over_limit(b.rid):
                    continue
                if self.quota_wait(b, room) > 0 or self.run_left(b, room) <= 0:
                    continue
                room.cold[1] += 1
                room.queue.put_nowait(("job", self.speak(b, room, "cold", last), None))
                break

    def fav_tick(self, now: float):
        """QQ / 微信账号收藏的表情：每隔「多久对一次收藏」同步一次（上线时已经对过一次，那次也记了时间）。
        顺带看一眼表情包描述：设置里的字数改了、有已学会的按别的字数描述，就在后台重新描述一轮（这一轮有跳过的，十分钟后再来）。"""
        if (self.settings.g("sticker_desc_auto") and now >= self.redesc_next and "redesc" not in self.stickers.busy
                and self.stickers.redesc_todo()):
            self.spawn(self.redesc_round(now))
        hours = float(self.settings.g("sticker_fav_hours") or 0)
        if hours <= 0:
            return
        for aid, b in self.bots.items():
            if b.platform not in FAV_PLATFORMS or not b.online or aid in self.stickers.busy or not self.S("sticker_fav_use", b):
                continue
            last = float((self.db.kv(f"favsync:{aid}") or {}).get("ts") or 0)
            if now - last >= hours * 3600:
                self.spawn(self.stickers.sync_favorites(b))

    async def redesc_round(self, now: float):
        res = await self.stickers.redescribe()
        self.redesc_next = now + (600 if res["skipped"] else 0)

    async def on_shift(self, b: Bot, room: Room, old_rid: str):
        old = (self.roles.get(old_rid) or {}).get("name", "?")
        self.log("info", "system", f"[{room.name}] 换班：{old} → {b.role['name']}")
        self.emit("duty", {"room": room.key, "account": b.aid, "role": b.rid})
        if self.S("shift_card", b, room) and room.platform == "qq" and b.online:
            try:
                await b.channel.set_chat_card(room.chat_id, b.role.get("card") or b.role["name"])
            except Exception as e:
                self.log("warn", "system", f"[{room.name}] 改群名片失败：{e}")
        if self.S("shift_greeting", b, room) and self.can_talk(b, room) and b.online:
            last = room.ctx[-1] if room.ctx else Entry("", "", "")
            room.queue.put_nowait(("job", self.speak(b, room, "shift", last, brief=old), None))

    async def _sched_ai(self, b: Bot, room: Room, instruction: str):
        if self.muted_in(b.aid, room.key):
            return
        e = room.ctx[-1] if room.ctx else Entry("", "", "")
        text = await self.generate(b, room, "task", e, brief=instruction)
        if text:
            await self.deliver(b, room, text, None, "schedule")

    # ---------- 好友申请、入群邀请 ----------
    async def on_request(self, b: Bot, kind: str, user_id: str, flag: str, sub_type: str = ""):
        key = "friend_request" if kind == "friend" else "group_invite"
        if kind == "group" and sub_type != "invite":
            return
        mode = self.S(key, b)
        approve = mode == "accept" or (mode == "list" and self.people.find(b.platform, user_id) is not None)
        if mode == "list" and not approve:
            self.log("info", "system", f"{'好友申请' if kind == 'friend' else '入群邀请'}来自 {user_id}，不在人物表里，先不处理")
            return
        await b.channel.answer_request(kind, flag, sub_type, approve)
        self.log("info", "system", f"{'好友申请' if kind == 'friend' else '入群邀请'}来自 {user_id}：{'同意' if approve else '拒绝'}")
