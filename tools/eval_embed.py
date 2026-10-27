"""向量模型在「群聊记忆」上的小评测：每条旧记忆配一句意思相关、但换了说法也没提当事人的群聊；另有一批闲聊噪声。
指标：命中率（相关记忆排第一）、AUC（相关分数高于不相关分数的概率）、建议门槛。
用法：python tools/eval_embed.py <模型文件夹> <pooling> [前缀]"""
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mb.embed import LocalModel  # noqa: E402

PAIRS = [
    ("阿明：在准备考研，每天泡图书馆", "研究生入学考试复习得怎么样了"),
    ("小红：养了一只橘猫叫胖虎", "我家主子又把杯子推下桌了"),
    ("群梗「下班」：老王天天喊想下班", "好想早点回家不干了"),
    ("老王说周末要去爬泰山", "这周六有人一起登山吗"),
    ("小李：刚换了工作，去了一家游戏公司", "新公司做什么项目的"),
    ("群里约好下个月一起去成都吃火锅", "四川那边的麻辣锅到底有多辣"),
    ("阿强：最近在学吉他，手指头都磨破了", "弹琴好难啊，和弦按不住"),
    ("小美：对花生过敏，聚餐别点带花生的菜", "点菜要注意什么忌口"),
    ("海豚的女儿上周跟人吵架吵赢了", "上次那场对线最后谁赢了"),
    ("大刘：在减肥，晚上不吃饭", "饿得睡不着又不敢吃夜宵"),
    ("群里有人推荐过《葬送的芙莉莲》", "最近有什么好看的番"),
    ("小周：考了驾照，科目二挂了两次", "倒车入库太难了"),
    ("阿杰：家里刚添了个女儿", "当爸爸是什么感觉"),
    ("老陈：开了一家奶茶店", "想喝点甜的冷饮"),
    ("群里讨论过拉康的镜像阶段", "婴儿照镜子认出自己那个理论叫啥"),
    ("小雨：在日本留学，时差一小时", "东京现在几点了"),
    ("阿涛：腰椎间盘突出，久坐就疼", "坐久了后背不舒服怎么办"),
    ("群里有人说要攒钱买房", "首付到底要准备多少"),
    ("小静：喜欢养多肉植物", "阳台上的花草怎么浇水"),
    ("老赵：是个程序员，天天加班改bug", "代码又出问题了半夜还在修"),
    ("群梗「真香」：说不要最后又买了", "嘴上说不要身体很诚实"),
    ("阿亮：在学日语，准备考N2", "外语等级考试难不难"),
    ("小敏：失恋了，心情不好", "分手以后怎么走出来"),
    ("群里约过一起打王者荣耀", "晚上开黑吗"),
]
NOISE = ["今天天气不错", "哈哈哈哈", "有人在吗", "晚安", "收到", "这也太离谱了吧", "早上好", "笑死我了", "嗯嗯", "好的好的"]


def evaluate(folder: str, pooling: str, prefix: str):
    m = LocalModel(Path(folder), pooling, prefix)
    D = np.array(m.embed([d for d, _ in PAIRS]))
    Q = np.array(m.embed([q for _, q in PAIRS], query=True))
    N = np.array(m.embed(NOISE, query=True))
    S = Q @ D.T
    top1 = float(np.mean(np.argmax(S, axis=1) == np.arange(len(PAIRS))))
    pos = np.diag(S)
    neg = np.concatenate([S[~np.eye(len(PAIRS), dtype=bool)], (N @ D.T).ravel()])
    auc = float(np.mean(pos[:, None] > neg[None, :]))
    thr = (np.percentile(pos, 20) + np.percentile(neg, 97)) / 2
    return top1, auc, thr, float(np.mean(pos >= thr)), float(np.mean(neg >= thr))


if __name__ == "__main__":
    t1, auc, thr, recall, fp = evaluate(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    print(f"{Path(sys.argv[1]).name}（前缀{'有' if len(sys.argv) > 3 and sys.argv[3] else '无'}）：命中率 {t1:.0%}，AUC {auc:.3f}，"
          f"建议门槛 {thr:.2f}（相关的过线 {recall:.0%}，不相关的误过线 {fp:.1%}）")
