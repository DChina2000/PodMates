"""给本机向量模型定默认相似度门槛：样例里「相关」的分数和「不相关」的分数，取中间。
用法：python tools/calib_embed.py <模型文件夹> <pooling> [前缀]"""
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mb.embed import LocalModel  # noqa: E402
from tools.bench_embed import DOCS, QUERIES  # noqa: E402

NOISE = ["今天天气不错", "哈哈哈哈", "有人在吗", "晚安", "收到", "这也太离谱了吧"]


def main():
    m = LocalModel(Path(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    D = np.array(m.embed(DOCS))
    Q = np.array(m.embed([q for q, _ in QUERIES], query=True))
    N = np.array(m.embed(NOISE, query=True))
    S = Q @ D.T
    pos = [S[i, g] for i, (_, g) in enumerate(QUERIES)]
    neg = [S[i, j] for i in range(len(QUERIES)) for j in range(len(DOCS)) if j != QUERIES[i][1]] + list((N @ D.T).ravel())
    p10, n95 = np.percentile(pos, 10), np.percentile(neg, 95)
    print(f"{Path(sys.argv[1]).name}: 相关 最低 {min(pos):.3f} / 10% {p10:.3f}；不相关 95% {n95:.3f} / 最高 {max(neg):.3f}；建议门槛 {(p10 + n95) / 2:.2f}")


if __name__ == "__main__":
    main()
