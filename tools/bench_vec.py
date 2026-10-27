"""语义回忆性能实测：向量模型加载后多占的内存、写入（算向量+存库）耗时、检索耗时（1 千 / 1 万条记忆）。
用法：python tools/bench_vec.py <临时数据目录>"""
import asyncio
import os
import random
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PODMATES_DATA"] = sys.argv[1]

from mb.core import Core  # noqa: E402

SUBJ = ["考研", "猫", "加班", "爬山", "火锅", "吉他", "减肥", "动漫", "驾照", "奶茶", "日语", "失恋", "游戏", "租房", "跑步"]
VERB = ["最近在", "说自己讨厌", "打算周末去", "天天抱怨", "推荐过", "刚开始学", "放弃了", "又提起了"]


async def main():
    core = Core(Path(sys.argv[1]))
    mem, emb = core.engine.memory, core.engine.embedder
    proc = psutil.Process()
    base = proc.memory_info().private
    t = time.time()
    await emb.embed(["预热"])
    print(f"模型加载：{time.time() - t:.2f} 秒，内存多占 {(proc.memory_info().private - base) / 1048576:.0f} MB")
    for total in (1000, 10000):
        have = core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0]
        t = time.time()
        while have < total:
            for _ in range(32):
                mem.add("role:r_haitun", "person", f"uid:qq:{random.randint(1, 500)}", "群友",
                        f"{random.choice(VERB)}{random.choice(SUBJ)}，{random.randint(1, 99)}号说的", "self", status="active")
            await mem.flush_index()
            have += 32
        n = have - (0 if total == 1000 else 1000)
        w = (time.time() - t) / max(1, n) * 1000
        qs = ["最近有人去爬山吗", "猫又不吃饭了", "工作好累不想干了", "哪家火锅好吃", "学外语有什么技巧"]
        t = time.time()
        for q in qs * 4:
            await emb.search(q, ["role:r_haitun"], 3, 0, set())
        s = (time.time() - t) / 20 * 1000
        print(f"{have} 条记忆：写入平均每条 {w:.1f} 毫秒（含算向量、存库，按 32 条一批）；一次语义检索 {s:.1f} 毫秒（含算查询向量）；"
              f"进程专用内存 {proc.memory_info().private / 1048576:.0f} MB；数据库文件 {(Path(sys.argv[1]) / 'podmates.db').stat().st_size / 1048576:.1f} MB")
    core.db.close()


asyncio.run(main())
