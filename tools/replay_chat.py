"""在模拟群里重放真实的群聊记录，比较「改前 / 改后」的回复长度和延迟。

数据来自程序自己的数据库（messages 表，只读）；真人消息照原来的先后和间隔放进来（间隔过长的压到 2 分钟），
机器人当时真的回过话的那几条真人消息，作为「叫到她」的触发消息。模型用模拟的：
  · 主模型的输出就是她当时真实的回复（所以长度、行数和真群里一样）；
  · 改后有字数上限提示，模拟的模型有 75% 的概率听话写短，其余的写长、触发压缩；
  · 守门员只说「不插话」，所以只统计被叫到的那些回复；
  · 模型的生成时间、压缩时间用一个固定的分布（主模型约 7 秒，守门员 2 秒，压缩/重新判断约 5 秒）。
时间整体加速 K 倍（时钟打了补丁），所以一小时的群聊一两分钟就放完。

  python tools/replay_chat.py <数据库，即数据目录里的 podmates.db（默认在安装目录下的 data 文件夹）> <房间key,房间key> before|after [--k 60] [--stale-msgs N] [--stale-secs N] [--json 输出文件]
"""
import asyncio
import json
import math
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

BOT_UID = "900"
LAT = {"main": 7.0, "gk": 2.0, "compress": 5.0, "stale": 5.0}


def load(db: str, room: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, kind, user_id, name, text FROM messages WHERE room=? AND kind IN ('human','bot') ORDER BY id", (room,)).fetchall()
    con.close()
    return rows


def triggers(rows):
    from mb import commands
    """机器人真的回过话的那些：回复前最近的一条真人消息（180 秒内）算触发消息。"""
    trig, used = {}, set()
    for i, (ts, kind, uid, name, text) in enumerate(rows):
        if kind != "bot" or (len(text) < 20 and text.startswith(("好，闭嘴", "我回来了", "好，换", "本群插话", "插话力度", "状态：", "忘掉了", "好，按排班"))):
            continue  # 管理指令的回应不算（重放时不当触发消息）
        for j in range(i - 1, -1, -1):
            if rows[j][1] == "human" and j not in used and ts - rows[j][0] <= 180 and not commands.parse(rows[j][4]):
                used.add(j)
                trig[j] = text
                break
    return trig


async def replay(db: str, room_key: str, mode: str, K: float, stale_msgs: int | None, stale_secs: int | None, squeeze: float = 1.0, obey: float = 0.75, seed: int = 7):
    from conftest import FakeChannel
    from mb import templates
    from mb.channels.base import Msg
    from mb.core import Core
    from mb.engine import trim_sentences
    rnd = random.Random(seed)
    rows = load(db, room_key)
    trig = triggers(rows)
    real = time.time
    t0, r0 = rows[0][0] - 5, time.monotonic()
    time.time = lambda: t0 + (time.monotonic() - r0) * K
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    import os
    os.environ["PODMATES_DATA"] = tmp.name
    try:
        core = Core(Path(tmp.name))
        templates.instantiate(core.db, "haitun", name="海豚娘", role_id="r_haitun", persona_id="p_海豚娘")
        core.engine.reload()
        eng, s = core.engine, core.settings
        eng.pace = 1.0 / K
        for k, v in dict(memory_enabled=False, prefilter_on=False, search_on=False, sticker_use=False, sticker_learn=False, proactive=False,
                         style_follow=False, remind_on=False, remember_on=False).items():
            s.set("global", k, v)
        if mode == "before":  # 改前：打字 8 字/秒、不限字数、不查过时、不合并、不提示重复
            for k, v in dict(typing_speed=8.0, group_reply_max=0, stale_on=False, merge_at=False, repeat_hint_n=0).items():
                s.set("global", k, v)
        else:
            if stale_msgs is not None:
                s.set("global", "stale_msgs", stale_msgs)
            if stale_secs is not None:
                s.set("global", "stale_secs", stale_secs)
        cap = int(s.g("group_reply_max") or 0)
        by_text = {rows[j][4]: rep for j, rep in trig.items()}
        calls = {"main": 0, "gk": 0, "compress": 0, "stale": 0}
        last_real = {}

        async def chat(purpose, messages, role=None, platform=None, chat=None, tag=None, model=None):
            kind = "compress" if tag == "compress" else "stale" if tag == "stale" else purpose
            calls[kind] = calls.get(kind, 0) + 1
            await asyncio.sleep(max(0.5, rnd.lognormvariate(math.log(LAT.get(kind, 5.0)), 0.4)) / K)
            if kind == "gk":
                return json.dumps({"reply": False, "brief": "x"})
            if kind == "stale":
                return json.dumps({"action": rnd.choices(["send", "rewrite", "drop"], [0.4, 0.4, 0.2])[0], "text": "（改写后的更短回复。）", "why": "模拟"})
            last = messages[-1]["content"]
            if kind == "compress":
                return trim_sentences(last.split("\n\n", 1)[-1], int(cap * 0.9))
            tail = last.rsplit("，说：", 1)[-1].split("\n（这次回复控制在")[0].strip()  # 触发消息本身，不是上下文里的别的消息
            txt = next((rep for t, rep in sorted(by_text.items(), key=lambda kv: -len(kv[0])) if t and (t == tail or tail.endswith(t))), "好的")
            last_real["x"] = txt
            if cap and "控制在" in last and rnd.random() < obey:  # 模拟的模型有 obey（默认 75%）的概率听话写短
                return trim_sentences(txt, int(cap * 0.9))
            return txt
        core.llm.chat = chat
        acc = core.db.put("account", {"id": "ac_900", "platform": "qq", "role_id": "r_haitun", "qq": BOT_UID})
        ch = FakeChannel(eng, acc, BOT_UID, "海豚娘")
        eng.add_bot(acc, ch)
        sent = []
        orig = ch.send

        async def send(chat_type, chat_id, text, reply_to=None, at=None):
            sent.append((time.time(), text, reply_to))
            return await orig(chat_type, chat_id, text, reply_to, at)
        ch.send = send
        platform, chat_id = room_key.split(":", 1)
        arrived: dict[str, tuple[float, int]] = {}
        all_t: list[float] = []
        prev = rows[0][0]
        n_human = 0
        for j, (ts, kind, uid, name, text) in enumerate(rows):
            if kind != "human":
                continue
            await asyncio.sleep(min(ts - prev, 120) / K / squeeze)
            prev = ts
            n_human += 1
            all_t.append(time.time())
            mid = f"m{n_human}"
            arrived[f"{acc['id']}-{mid}"] = (time.time(), n_human) if j in trig else None
            await eng.on_message(Msg(platform, acc["id"], "group", chat_id, uid, name, text, msg_id=f"{acc['id']}-{mid}", chat_name=room_key,
                                     at_ids=[BOT_UID] if j in trig else [], dedup=f"{chat_id}|{uid}|{mid}"))
        await eng.idle()
        ctr = {r[0]: r[1] for r in core.db.x("SELECT name, SUM(n) FROM counters GROUP BY name")}
        merged_n = core.db.x("SELECT COUNT(*) FROM logs WHERE text LIKE '%合成一条回复%'").fetchone()[0]
        eng.close()
        await core.llm.close()
        core.db.close()
    finally:
        time.time = real
        tmp.cleanup()
    arrived = {k: v for k, v in arrived.items() if v}
    if "--dump" in sys.argv:
        for t, text, rt in sent[:6]:
            print("  SENT", rt, repr(text[:50]))
    # 一条回复 = 同一个 reply_to 引的那几段；合并回复引的是最后一条触发消息
    reps, cur = {}, None
    for t, text, rt in sent:
        if rt:  # 只有第一段引用触发消息，后面几段接在它后面
            cur = rt
        reps.setdefault(cur or "free", []).append((t, text))
    lat_first, lat_last, lens, parts, newer_first, newer_last = [], [], [], [], [], []
    for rt, items in reps.items():
        trig_t = arrived.get(rt, (None,))[0]
        if trig_t is None:
            continue
        newer_first.append(sum(1 for x in all_t if trig_t < x <= items[0][0]))
        newer_last.append(sum(1 for x in all_t if trig_t < x <= items[-1][0]))
        lat_first.append(items[0][0] - trig_t)
        lat_last.append(items[-1][0] - trig_t)
        lens.append(sum(len(x[1]) for x in items))
        parts.append(len(items))
    avg = lambda a: round(sum(a) / len(a), 1) if a else None
    pct = lambda a, p: round(sorted(a)[min(len(a) - 1, int(len(a) * p))], 1) if a else None
    return {"room": room_key, "mode": mode, "triggers": len(arrived), "replies": len(lens), "bot_messages": len(sent), "calls": calls,
            "len_avg": avg(lens), "len_max": max(lens, default=None), "parts_avg": avg(parts),
            "first_avg": avg(lat_first), "first_p50": pct(lat_first, 0.5), "first_p90": pct(lat_first, 0.9),
            "last_avg": avg(lat_last), "last_p90": pct(lat_last, 0.9), "real_len_avg": avg([len(v) for v in trig.values()]),
            "real_replies": len(trig), "counters": ctr, "merged": merged_n,
            "newer_first_avg": avg(newer_first), "newer_last_avg": avg(newer_last),
            "stale_first_pct": round(100 * sum(1 for x in newer_first if x >= 3) / max(1, len(newer_first))), "stale_last_pct": round(100 * sum(1 for x in newer_last if x >= 3) / max(1, len(newer_last)))}


def main():
    a = sys.argv[1:]
    opt = lambda name, default=None: (a.pop(a.index(name) + 1), a.remove(name))[0] if name in a else default
    K = float(opt("--k", 60))
    sm, ss = opt("--stale-msgs"), opt("--stale-secs")
    out = opt("--json")
    squeeze = float(opt("--squeeze", 1))
    obey = float(opt("--obey", 0.75))
    dump = "--dump" in a
    a = [x for x in a if x != "--dump"]
    db, rooms, mode = a[0], a[1].split(","), a[2]
    res = [asyncio.run(replay(db, r, mode, K, int(sm) if sm else None, int(ss) if ss else None, squeeze, obey)) for r in rooms]
    for r in res:
        print(json.dumps(r, ensure_ascii=False))
    if out:
        Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
