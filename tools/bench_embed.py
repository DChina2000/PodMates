"""本机向量模型实测：加载后多占的内存、加载耗时、单句检索耗时、32 条批量写入耗时、一个小的中文检索样例。
用法：python tools/bench_embed.py <模型文件夹> <pooling: cls/last> [前缀]"""
import os
import sys
import time

import numpy as np
import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mb.embed import LocalModel  # noqa: E402

DOCS = ["阿明：在准备考研，每天泡图书馆", "群梗「下班」：老王天天喊想下班，成了群梗", "小红：养了一只橘猫叫胖虎",
        "老王说周末要去爬泰山", "小李：刚换了工作，去了一家游戏公司", "群里约好下个月一起去成都吃火锅",
        "阿强：最近在学吉他，手指头都磨破了", "小美：对花生过敏，聚餐别点带花生的菜"]
QUERIES = [("研究生入学考试复习得怎么样了", 0), ("我家猫又胖了", 2), ("真想早点回家不干了", 1), ("这周六有人一起登山吗", 3),
           ("新公司做什么项目的", 4), ("四川那边的麻辣锅到底有多辣", 5), ("弹琴好难啊", 6), ("点菜要注意什么忌口", 7)]


def main():
    folder, pooling = sys.argv[1], sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else ""
    proc = psutil.Process()
    base = proc.memory_info().private
    t = time.time()
    m = LocalModel(__import__("pathlib").Path(folder), pooling, prefix)
    m.embed(["预热"])
    load = time.time() - t
    mem = (proc.memory_info().private - base) / 1048576
    t = time.time()
    for _ in range(20):
        m.embed(["这周六有人一起登山吗"], query=True)
    q_ms = (time.time() - t) / 20 * 1000
    batch = [DOCS[i % len(DOCS)] + f"（第{i}条）" for i in range(32)]
    t = time.time()
    m.embed(batch)
    w_ms = (time.time() - t) * 1000
    D = np.array(m.embed(DOCS))
    Q = np.array(m.embed([q for q, _ in QUERIES], query=True))
    hits = sum(int(np.argmax(Q[i] @ D.T) == gold) for i, (_, gold) in enumerate(QUERIES))
    print(f"{os.path.basename(folder.rstrip('/'))}: 内存 +{mem:.0f} MB，加载 {load:.1f} 秒，单句检索 {q_ms:.1f} 毫秒，"
          f"32 条写入 {w_ms:.0f} 毫秒，小样例命中 {hits}/{len(QUERIES)}")


if __name__ == "__main__":
    main()
