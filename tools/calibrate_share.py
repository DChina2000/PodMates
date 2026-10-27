"""在模拟群里测「发言占比上限」的默认值：统计窗口条数 × 占比上限，哪一组既不刷屏、也不冷场。

用真的引擎、模拟的时钟（人类消息按泊松到达）和模拟的守门员（以概率 q 说「插话」）。
结果写到 build/research/share_calibration.json 并打印表格。

  python tools/calibrate_share.py 45 40 35     # 模拟 45 分钟，窗口 40 条、占比上限 35%（窗口、上限填 0 0 = 不限制）
  python tools/calibrate_share.py report       # 汇总各组结果
"""
import asyncio
import json
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

WINDOWS = [20, 40, 80]
MAXES = [25, 35, 50]
RATES = [2, 6, 15]          # 真人每分钟几条
QS = [0.15, 0.5, 0.9]       # 守门员说「插话」的概率：保守 / 中等 / 话痨
BOTS = [1, 3]
AT_RATE = 0.08              # 真人消息里有多少是 @ 机器人的


async def run(window, mx, rate, q, n_bots, minutes, seed):
    from mb.core import Core
    from mb import templates
    from conftest import FakeChannel
    rnd = random.Random(seed)
    clock = {"t": 1_800_000_000.0}
    real_time = time.time
    time.time = lambda: clock["t"]
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    import os
    os.environ["PODMATES_DATA"] = tmp.name
    try:
        core = Core(Path(tmp.name))
        templates.instantiate(core.db, "haitun", name="海豚娘", role_id="r_haitun", persona_id="p_海豚娘")
        core.engine.reload()
        eng, s = core.engine, core.settings
        eng.pace = 0
        s.set("global", "memory_enabled", False)
        s.set("global", "prefilter_on", False)
        s.set("global", "search_on", False)
        s.set("global", "sticker_use", False)
        s.set("global", "sticker_learn", False)
        s.set("global", "max_bots_per_message", 2)
        if mx:
            s.set("global", "share_limit_on", True)
            s.set("global", "share_window", window)
            s.set("global", "share_max", mx)

        async def chat(purpose, messages, role=None, platform=None, chat=None, tag=None, model=None):
            if purpose == "gk":
                return json.dumps({"reply": rnd.random() < q, "brief": "接话"})
            return "嗯嗯"
        core.llm.chat = chat
        chans = []
        for i in range(n_bots):
            uid = str(900 + i)
            acc = core.db.put("account", {"id": f"ac_{uid}", "platform": "qq", "role_id": "r_haitun", "qq": uid})
            ch = FakeChannel(eng, acc, uid, f"bot{uid}")
            eng.add_bot(acc, ch)
            chans.append(ch)
        seq = []
        orig_push = eng.push

        def push(room, e, kind, *a, **k):
            if kind in ("human", "bot"):
                seq.append((clock["t"], "b" if e.bot else "h"))
            return orig_push(room, e, kind, *a, **k)
        eng.push = push
        from mb.channels.base import Msg
        t_end = clock["t"] + minutes * 60
        n = 0
        while clock["t"] < t_end:
            clock["t"] += rnd.expovariate(rate / 60.0)
            n += 1
            at = [rnd.choice(chans).self_id] if rnd.random() < AT_RATE else []
            uid = str(100 + rnd.randrange(8))
            for ch in chans:
                await eng.on_message(Msg("qq", ch.aid, "group", "100", uid, f"群友{uid}", "今天天气真不错啊", msg_id=f"{ch.aid}-{n}",
                                         chat_name="模拟群", at_ids=at, dedup=f"100|{uid}|{n}"))
            await eng.idle()
        eng.close()
        await core.llm.close()
        core.db.close()
    finally:
        time.time = real_time
        tmp.cleanup()
    nb = sum(1 for _, k in seq if k == "b")
    share = nb / max(1, len(seq))
    # 滚动 40 条里机器人占比的第 95 百分位
    roll = [sum(1 for _, k in seq[i:i + 40] if k == "b") / 40 for i in range(0, max(1, len(seq) - 40), 5)]
    roll.sort()
    p95 = roll[int(len(roll) * 0.95)] if roll else 0
    cold = tot = 0
    for start in range(int(seq[0][0]), int(t_end), 600):
        chunk = [k for t, k in seq if start <= t < start + 600]
        if chunk.count("h") >= 3:
            tot += 1
            cold += chunk.count("b") == 0
    return {"share": round(share, 3), "p95": round(p95, 3), "cold": round(cold / tot, 3) if tot else 0.0, "bots_msgs": nb, "msgs": len(seq)}


async def one_config(w, m, minutes):
    out = []
    for rate in RATES:
        for q in QS:
            for nb in BOTS:
                r = await run(w, m, rate, q, nb, minutes, seed=rate * 1000 + int(q * 100) * 10 + nb)
                out.append(dict(window=w, max=m, rate=rate, q=q, bots=nb, **r))
    p = ROOT / "build" / "research" / f"share_cfg_{w}_{m}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", p, flush=True)


def report():
    rows = []
    for f in sorted((ROOT / "build" / "research").glob("share_cfg_*.json")):
        rows += json.loads(f.read_text(encoding="utf-8"))
    cfgs = sorted({(r["window"], r["max"]) for r in rows})
    print("窗口 上限 | 平均占比 p95占比 冷场率（按守门员 q=0.15 / 0.5 / 0.9 分列）")
    for w, m in cfgs:
        cells = []
        for q in QS:
            sel = [r for r in rows if (r["window"], r["max"]) == (w, m) and r["q"] == q]
            cells.append("%4.0f%% %4.0f%% %4.0f%%" % (100 * sum(r["share"] for r in sel) / len(sel), 100 * sum(r["p95"] for r in sel) / len(sel), 100 * sum(r["cold"] for r in sel) / len(sel)))
        print(f"{w:>4} {m:>4}% | " + " | ".join(cells))


async def main():
    """用法：calibrate_share.py 分钟数 窗口 上限（跑一组，各组可并行）；calibrate_share.py report（汇总打印）。"""
    if sys.argv[1] == "report":
        return report()
    await one_config(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[1]))


if __name__ == "__main__":
    asyncio.run(main())
