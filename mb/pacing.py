"""群聊分寸：发言占比、插话力度自动调节、「别说了」暂停。

三样都只管「主动插话」，被@、被回复照旧必回；和账号级上限、冷却、静默时段叠加，取最严。
"""
import time
from collections import deque

# 反馈 → 力度调整（档）。回升靠两条：接得好 +0.6；时间久了自己往设定值回（每小时 0.25 档），所以不会一路降到零
FEEDBACK_STEP = {"warm": 0.6, "ignored": -0.5, "annoyed": -1.0}
FEEDBACK_LABEL = {"warm": "有人顺着接了话", "ignored": "没人理", "annoyed": "被嫌烦了"}
RECOVER_PER_HOUR = 0.25
STOP_WINDOW = 3600  # 这段时间内再被叫停，算「反复」


def share(entries, window: int) -> tuple[int, int]:
    """最近 window 条消息里（只数条数）：（机器人发的条数，总条数）。机器人 = 我们所有的账号合计。"""
    items = list(entries)[-max(1, int(window)):]
    return sum(1 for e in items if e.bot), len(items)


def share_blocked(entries, window: int, mode: str = "count", max_count: int = 7, max_percent: int = 47,
                  min_on: bool = False, min_n: int = 10) -> bool:
    """发言占比到顶了没有。按条数（默认）：最近 window 条里机器人发言 >= max_count 条；按比例：占比（四舍五入到整数百分比）>= max_percent。
    min_on 开着时，样本不满 min(window, min_n) 条不拦（主要给按比例用：消息很少时一两条发言就是很高的比例）。"""
    n_bot, n = share(entries, window)
    if min_on and n < min(int(window), int(min_n)):
        return False
    if mode == "percent":
        return n > 0 and int(n_bot * 100 / n + 0.5) >= max_percent
    return n_bot >= max_count


class Tune:
    """一个机器人在一个群里的插话力度调节：adj 是相对设定力度的偏移（档），在 [-down, +up] 之内。"""

    def __init__(self, adj: float = 0.0, ts: float | None = None, hist: list | None = None):
        self.adj, self.ts = float(adj), ts or time.time()
        self.hist: deque = deque(hist or [], maxlen=12)

    def decay(self, now: float | None = None):
        now = now or time.time()
        dt = max(0.0, now - self.ts) / 3600
        step = RECOVER_PER_HOUR * dt
        if self.adj < 0:
            self.adj = min(0.0, self.adj + step)
        elif self.adj > 0:
            self.adj = max(0.0, self.adj - step)
        self.ts = now

    def apply(self, feedback: str, down: int, up: int, reason: str = "", now: float | None = None) -> float:
        now = now or time.time()
        self.decay(now)
        before = self.adj
        self.adj = max(-float(down), min(float(up), self.adj + FEEDBACK_STEP.get(feedback, 0.0)))
        self.hist.append([now, feedback, round(self.adj - before, 2), reason or FEEDBACK_LABEL.get(feedback, "")])
        return self.adj

    def level(self, base: int, down: int, up: int, now: float | None = None) -> int:
        self.decay(now)
        adj = max(-float(down), min(float(up), self.adj))
        return max(1, min(5, round(base + adj)))

    def dump(self) -> dict:
        return {"adj": round(self.adj, 3), "ts": self.ts, "hist": list(self.hist)}


def stop_minutes(history: list, now: float, base: int, cap: int) -> tuple[int, list]:
    """这次「别说了」该暂停多久：短时间内反复被叫停就翻倍递增，到 cap 为止。返回（分钟，更新后的历史）。"""
    recent = [t for t in history if now - t < STOP_WINDOW]
    n = len(recent)
    return int(min(max(1, base) * (2 ** n), max(base, cap))), recent + [now]
