"""记忆体系重做：群里的事分层合并写成后不改、人有印象、按人取回、不漏不丢、按模型能读多长摆空间、删除改动和撤销。全部用桩模型。"""
import json
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

import mb.memory as memory_mod
from mb.llm import LLM, ContextOverflow, overflow_info
from mb.memory import Unit, day_text, span_text
from mb.server import Server

HAITUN = "90001"
ROOM = "qq:100"
DAY = 86400


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def setup(e, seg=2, merge=3, window=5, **kw):
    setg(e, "proactive", False)
    setg(e, "memory_segment", seg)
    setg(e, "memory_merge", merge)
    setg(e, "context_window", window)
    for k, v in kw.items():
        setg(e, k, v)
    return e.add_bot("901")


class Clock:
    """把 time.time() 换成可以拨的钟：造出跨很多天的历史（消息的时间、前情单位的时间范围都跟着走）。"""

    def __init__(self, monkeypatch):
        self.t = time.time()
        real = time.time
        self.real = real
        monkeypatch.setattr(time, "time", lambda: self.t)
        from mb.engine import Engine
        orig = Engine.push
        clock = self

        def push(eng, room, e, kind, *a, **k):  # Entry 的时间默认值在定义时就绑死了真的 time.time：入库前改成钟上的时间
            if kind != "shadow":
                e.ts = clock.t
            return orig(eng, room, e, kind, *a, **k)
        monkeypatch.setattr(Engine, "push", push)


async def say_n(e, n, start=0, uid="111", chat="100", day0=None, per_day=1, clock=None):
    """发 n 条群消息；给了 clock 和 day0，就把时间拨到从 day0 起、每 per_day 条隔一天。"""
    for i in range(n):
        if clock is not None and day0 is not None:
            clock.t = day0 + ((start + i) // per_day) * DAY + (start + i) % per_day
        await e.say(uid, f"消息{start + i}", chat=chat)


def l1(db, room=ROOM):
    return db.rows("SELECT * FROM memories WHERE kind='recap' AND room=? AND level=1 ORDER BY id", (room,))


def units(db, room=ROOM):
    return db.rows("SELECT * FROM memories WHERE kind='recap' AND room=? ORDER BY level, id", (room,))


def meta(r):
    return json.loads(r.get("meta") or "{}")


def bot_of(e, room_key=ROOM):
    eng = e.engine
    return eng.duty(next(iter(eng.bots.values())), eng.rooms[room_key])


def uncovered(e, room_key=ROOM):
    """群里每条消息不是在前情单位的覆盖范围里，就是在拼给模型的原始消息里：返回两边都不在的消息编号。"""
    eng, db = e.engine, e.core.db
    room = eng.rooms[room_key]
    ids = [r["id"] for r in db.rows("SELECT id FROM messages WHERE room=? AND kind IN ('human','bot')", (room_key,))]
    ranges = [(meta(r).get("m0"), meta(r).get("m1")) for r in l1(db, room_key)]
    raw = {x.dbid for x in eng.ctx_items(room, bot_of(e, room_key), cover=True)}
    return [i for i in ids if not any(a and a <= i <= b for a, b in ranges) and i not in raw]


def add_unit(mem, text, level=1, t0=None, t1=None, m0=None, m1=None, uids=(), parts=None, merged=0, space="chat:qq:100", room=ROOM, empty=False):
    t1 = t1 or time.time()
    m = {"t0": t0 or t1, "t1": t1, "m0": m0, "m1": m1, "uids": list(uids)}
    if parts:
        m["parts"] = list(parts)
    if empty:
        m["empty"] = 1
    uid = mem.add(space, "recap", "", "", text, "inferred", None, room, level, status="active", meta=m, ts=t1)
    if merged:
        mem.db.x("UPDATE memories SET merged=1 WHERE id=?", (uid,))
    return uid


# ---------- 1. 不漏 ----------
def test_every_message_is_either_in_a_recap_or_in_the_raw_context(env):
    async def body(e):
        setup(e, seg=4, merge=5, window=5)
        e.llm.mem = lambda p: {"recap": "这一段的事", "people": [], "memes": []}
        for i in range(1, 40):
            await e.say("111", f"消息{i}")
            assert uncovered(e) == [], f"第 {i} 条之后漏了"
        assert len(l1(e.core.db)) == 9
        # 前情的范围首尾相接：没有缝
        rs = [(meta(r)["m0"], meta(r)["m1"]) for r in l1(e.core.db)]
        assert all(rs[i][1] + 1 == rs[i + 1][0] for i in range(len(rs) - 1))
    env.run(body)


def test_a_segment_that_fails_to_consolidate_is_retried_and_never_lost(env):
    async def body(e):
        setup(e, seg=4, merge=5, window=5)
        state = {"fail": 0}

        def mem(p):
            if state["fail"] > 0:
                state["fail"] -= 1
                raise RuntimeError("接口出错")
            return {"recap": "整理好了", "people": [], "memes": []}
        e.llm.mem = mem
        state["fail"] = 2  # 接口出错会自动重试一次，所以连着两次才算这一段失败
        await say_n(e, 4)
        room = e.engine.rooms[ROOM]
        assert l1(e.core.db) == [] and room.mem_fail == 1 and uncovered(e) == []  # 失败了：没有前情，但原始消息一直带着
        room.mem_retry_at = 0
        await say_n(e, 1, start=4)
        await e.engine.idle()
        assert len(l1(e.core.db)) == 1 and room.mem_fail == 0 and uncovered(e) == []  # 重试成功，原来那一段整理进去了
        assert meta(l1(e.core.db)[0])["m0"] < meta(l1(e.core.db)[0])["m1"]
        # 连着失败 3 次：把原话存成第一层单位，不丢
        state["fail"] = 99
        for k in range(3):
            room.mem_retry_at = 0
            await say_n(e, 4, start=10 + k * 4)
        await e.engine.idle()
        raw = [r for r in l1(e.core.db) if meta(r).get("raw")]
        assert len(raw) == 1 and "没整理成功，以下是原话" in raw[0]["content"] and "消息" in raw[0]["content"]
        assert uncovered(e) == []
    env.run(body)


def test_raw_context_reaches_back_to_the_end_of_the_last_recap(env):
    async def body(e):
        setup(e, seg=10, merge=5, window=3)
        e.llm.mem = lambda p: {"recap": "事", "people": [], "memes": []}
        await say_n(e, 17)
        room, b = e.engine.rooms[ROOM], bot_of(e)
        items = e.engine.ctx_items(room, b, cover=True)
        assert len(items) == 7 and items[0].dbid == meta(l1(e.core.db)[0])["m1"] + 1  # 最近 3 条，往前一直带到前情结束的地方
        assert len(e.engine.ctx_items(room, b)) == 3  # 守门员等照旧只看最近几条
        # 重启后回填的上下文也一样
        from mb.engine import Engine
        eng2 = Engine(e.core.db, e.core.settings, e.core.llm, emit=e.core.emit)
        r2 = eng2.room("qq", "100", "测试群")
        assert [x.dbid for x in r2.ctx][-7:] == [x.dbid for x in items] and r2.mem_count == 7
        eng2.close()
    env.run(body)


# ---------- 2. 分层：第一层不截字，上限按实际字数之和 × 倍数，写成后不改 ----------
def test_layers_first_layer_not_truncated_upper_limit_by_actual_chars_times_ratio_and_never_rewritten(env):
    async def body(e):
        setup(e, seg=2, merge=3)
        k = {"n": 0}

        def mem(p):
            k["n"] += 1
            return {"recap": f"第{k['n']}段：" + "事" * (100 * k["n"]), "people": [], "memes": []}
        prompts = []
        e.llm.mem = mem
        e.llm.merge = lambda p: prompts.append(p) or "并" * 50
        await say_n(e, 6, uid="111")
        rows = l1(e.core.db)
        assert [len(r["content"]) for r in rows] == [len("第1段：") + 100, len("第2段：") + 200, len("第3段：") + 300]  # 第一层没被截字，也不设固定字数
        assert len(prompts) == 1
        total = sum(len(r["content"]) for r in rows)
        assert f"不超过 {int(total * 0.7)} 字" in prompts[0]  # 上限 = 实际字数之和 × 0.7
        up = units(e.core.db)[-1]
        m = meta(up)
        assert up["level"] == 2 and m["parts"] == [r["id"] for r in rows] and m["m0"] == meta(rows[0])["m0"] and m["m1"] == meta(rows[-1])["m1"]
        assert m["t0"] == meta(rows[0])["t0"] and m["t1"] == meta(rows[-1])["t1"] and set(m["uids"]) >= {"111"}
        assert all(r["merged"] == 1 for r in e.core.db.rows("SELECT merged FROM memories WHERE id IN (%s)" % ",".join(map(str, m["parts"]))))
        before = e.core.db.x("SELECT id, content FROM memories WHERE kind='recap' ORDER BY id").fetchall()
        # 改了倍数：只影响以后新并的，写成的单位一个字不动
        setg(e, "memory_ratio", 0.5)
        await say_n(e, 6, start=6)
        after = e.core.db.x("SELECT id, content FROM memories WHERE kind='recap' ORDER BY id").fetchall()
        assert [tuple(x) for x in after[:len(before)]] == [tuple(x) for x in before]
        total2 = sum(len(r["content"]) for r in l1(e.core.db)[3:6])
        assert f"不超过 {int(total2 * 0.5)} 字" in prompts[1]
        # 第三层：再并 3 个第二层
        await say_n(e, 12, start=12)
        assert any(r["level"] == 3 for r in units(e.core.db))
    env.run(body)


def test_merge_prompt_asks_who_did_what_and_how_things_changed(env):
    async def body(e):
        setup(e, seg=2, merge=2)
        prompts = []
        e.llm.mem = lambda p: {"recap": "甲给乙发了红包", "people": [], "memes": []}
        e.llm.merge = lambda p: prompts.append(p) or "合并"
        await say_n(e, 4)
        p = prompts[0]
        for need in ("每件事写明谁对谁做了什么", "写变了以后的样子", "怎么变的", "人名照原文", "不写感受", "不添原文里没有的事", "月", "日"):
            assert need in p, need
    env.run(body)


def test_empty_segments_count_as_covered_but_show_nothing(env):
    async def body(e):
        setup(e, seg=2, merge=3)
        e.llm.mem = lambda p: {"recap": "", "people": [], "memes": []}
        await say_n(e, 4)
        rs = l1(e.core.db)
        assert len(rs) == 2 and all(meta(r).get("empty") and meta(r)["m0"] for r in rs) and uncovered(e) == []
        assert e.engine.memory.compose("r_haitun", "qq", ROOM, [], "")[0] == []  # 没有值得记的事：不往提示词里放空行
    env.run(body)


# ---------- 3. 拼法 ----------
def build_history(e, clock, n_l1=15, merge=5, seg=2):
    """造一段长历史：n_l1 个第一层（每个一天），按 merge 并上去。"""
    async def go():
        k = {"n": 0}

        def mem(p):
            k["n"] += 1
            return {"recap": f"第{k['n']:02d}天的事" + "。" * 20, "people": [], "memes": []}
        e.llm.mem = mem
        e.llm.merge = lambda p: "概括" + "。" * 10 + "".join(re.findall(r"第\d\d天", p))
        now = clock.real()
        await say_n(e, n_l1 * seg, day0=now - n_l1 * DAY, per_day=seg, clock=clock)
        clock.t = now
    return go()


def test_layout_covers_all_history_coarsest_first_then_refines_newest_first_and_drops_earliest_when_tight(env, monkeypatch):
    async def body(e):
        setup(e, seg=2, merge=5)
        await build_history(e, Clock(monkeypatch), 15)
        mem = e.engine.memory
        sp = mem.space("recap", "r_haitun", "qq", ROOM)
        units_, roots = mem.load_units(sp)
        assert [u.level for u in roots] == [2, 2, 2]
        coarse = sum(u.cost for u in roots)
        text = lambda budget: "\n".join(mem.compose("r_haitun", "qq", ROOM, [], "", budget=budget)[0])
        # 空间刚好够最粗一层：全部历史都被盖住，3 行，越早越概括，每行带时间范围
        t = text(coarse)
        lines = [x for x in t.splitlines() if x.startswith("- ")]
        assert len(lines) == 3 and t.startswith("【这个群以前的事】（按时间先后，越早越概括）") and all(re.match(r"- \d+ 月 \d+ 日—\d+ 月 \d+ 日：概括", x) for x in lines)
        assert lines[0].split("：")[0] != lines[1].split("：")[0]  # 每个单位的时间范围不一样
        assert "第01天" in lines[0] and "第15天" in lines[2] and "只在相关时才提" not in t
        # 空间再多一点：从最新往前换成下一层，越新越细，换不下就停
        newest_children = sum(c.cost for c in roots[-1].children)
        t2 = text(coarse - roots[-1].cost + newest_children)
        l2 = [x for x in t2.splitlines() if x.startswith("- ")]
        assert len(l2) == 2 + 5 and "第15天的事" in t2 and "第14天的事" in t2 and "第01天的事" not in t2  # 最新那段换成了 5 个第一层，更早的还是概括
        # 空间够多：全部换成第一层
        t3 = text(10 ** 6)
        l3 = [x for x in t3.splitlines() if x.startswith("- ")]
        assert len(l3) == 15 and not any("概括" in x for x in l3)
        # 空间不够最粗一层：从最早的单位开始让
        t4 = text(coarse - 1)
        l4 = [x for x in t4.splitlines() if x.startswith("- ")]
        assert len(l4) == 2 and "第01天" not in t4 and "第15天" in t4
        # 设置里「记忆最多带多少」设小：同样先让最早的
        setg(e, "memory_max", coarse - 1)
        b = bot_of(e)
        budget = e.engine.memory_budget(b, "qq", ROOM, 1000, 1000)
        assert budget == coarse - 1
        sysmsg = await e.engine.system_prompt(b, e.engine.rooms[ROOM])
        assert "第01天" not in sysmsg and "第15天" in sysmsg
    env.run(body)


def test_upper_layer_stays_in_the_prompt_when_the_bottom_layer_piles_up(env, monkeypatch):
    """旧缺陷：底层攒到 4 段，上层就被挤出提示词。"""
    async def body(e):
        setup(e, seg=2, merge=5)
        await build_history(e, Clock(monkeypatch), 14)  # 2 个第二层 + 4 个没并的第一层
        mem = e.engine.memory
        units_, roots = mem.load_units(mem.space("recap", "r_haitun", "qq", ROOM))
        assert sorted(u.level for u in roots) == [1, 1, 1, 1, 2, 2]
        t = "\n".join(mem.compose("r_haitun", "qq", ROOM, [], "", budget=sum(u.cost for u in roots))[0])
        assert len([x for x in t.splitlines() if x.startswith("- ") and "概括" in x]) == 2 and all(f"第{d}天的事" in t for d in (11, 12, 13, 14)) and "第01天" in t
    env.run(body)


def test_prompt_memory_section_at_three_points_in_time(env, monkeypatch):
    """随着历史变长：前情一直盖住全部历史，越早越概括（把三个时间点的记忆一节贴进测试输出，-s 可看）。"""
    async def body(e):
        setup(e, seg=2, merge=5)
        clock = Clock(monkeypatch)
        k = {"n": 0}

        def mem_(p):
            k["n"] += 1
            return {"recap": f"第{k['n']:02d}天的事" + "。" * 20, "people": [], "memes": []}
        e.llm.mem = mem_
        e.llm.merge = lambda p: "概括" + "。" * 10 + "".join(re.findall(r"第\d\d天", p))
        mem = e.engine.memory
        outs = []
        now0 = clock.real()
        for n in (6, 25, 75):
            done = len(l1(e.core.db))
            await say_n(e, (n - done) * 2, start=done * 2, day0=now0 - 75 * DAY, per_day=2, clock=clock)
            sec = "\n\n".join(mem.compose("r_haitun", "qq", ROOM, [], "", budget=1200)[0])
            outs.append(sec)
            print(f"\n===== 第一层 {n} 段（{n * 2} 条消息）：记忆一节 {len(sec)} 字 =====\n{sec}")
        assert all(len(s) <= 1200 for s in outs)
        assert "第01天" in outs[1] or "概括" in outs[1]
        lens = [len([x for x in s.splitlines() if x.startswith("- ")]) for s in outs]
        assert lens[0] <= 6 and all(x > 0 for x in lens)
    env.run(body)


# ---------- 4. 印象 ----------
def test_impression_is_updated_when_notes_are_squeezed_out_once_for_all_roles_and_told_notes_are_not_folded(env):
    async def body(e):
        e.add_role("r_乙", "乙角色")
        e.add_bot("901")
        e.add_bot("902", role_id="r_乙")
        setg(e, "proactive", False)
        setg(e, "memory_segment", 2)
        setg(e, "memory_person_limit", 3)
        k = {"n": 0}

        def mem(p):
            k["n"] += 1
            return {"recap": "", "people": [{"uid": "111", "name": "群友111", "note": f"备忘{k['n']}", "source": "self"}], "memes": []}
        e.llm.mem = mem
        imps = []
        e.llm.impression = lambda p: imps.append(p) or f"印象{len(imps)}：他是个考研的人"
        m = e.engine.memory
        m.add("iso:qq:100:person:r_haitun", "person", "uid:qq:111", "群友111", "我不吃香菜", "told", room=ROOM, status="active")
        m.add("iso:qq:100:person:r_乙", "person", "uid:qq:111", "群友111", "我不吃香菜", "told", room=ROOM, status="active")
        await say_n(e, 6)  # 3 次整理，每次一条备忘（两个角色各一份）
        assert imps == []  # 3 条没超过 3：不更新印象
        await say_n(e, 2, start=6)  # 第 4 条把第 1 条挤出去
        assert len(imps) == 1 and "备忘1" in imps[0] and "备忘2" not in imps[0].split("【新增的备忘】")[1]
        assert len([c for c in e.llm.calls if c["_kind"] == "impression"]) == 1  # 两个角色的空间里备忘一样，只调一次模型
        for role in ("r_haitun", "r_乙"):
            imp = m.impression_row(f"iso:qq:100:person:{role}", "uid:qq:111")
            assert imp["content"] == "印象1：他是个考研的人" and meta(imp)["upto"] > 0
        assert "我不吃香菜" not in imps[0]  # 本人要求记住的不并进印象
        await say_n(e, 2, start=8)  # 第 5 条：挤出第 2 条，接在原来印象上更新
        assert len(imps) == 2 and "印象1：他是个考研的人" in imps[1] and "备忘2" in imps[1].split("【新增的备忘】")[1] and "备忘1" not in imps[1].split("【新增的备忘】")[1]
        # 提示词里：印象 + 最新 3 条备忘（带日期）+ 他要求记住的；被挤出的旧备忘不在
        sec = "\n".join(m.compose("r_haitun", "qq", ROOM, [("uid:qq:111", "群友111")], "")[0])
        assert "关于群友111，她目前的印象：印象2" in sec and sec.count("）：备忘") == 3 and "备忘5" in sec and "备忘3" in sec and "备忘2）" not in sec
        assert re.search(r"本人要求记住）：我不吃香菜", sec)
        assert re.search(r"（\d+ 月 \d+ 日，本人说的）：备忘5", sec)
        # 备忘本身永久留着
        assert e.core.db.x("SELECT COUNT(*) FROM memories WHERE kind='person' AND source='self' AND space LIKE '%r_haitun'").fetchone()[0] == 5
    env.run(body)


def test_person_limit_default_is_100_and_settings_exist_with_descriptions():
    from mb.settings import BY_KEY
    assert BY_KEY["memory_person_limit"]["default"] == 100 and "100" in BY_KEY["memory_person_limit"]["help"]
    r = BY_KEY["memory_ratio"]
    assert r["default"] == 0.7 and [o[0] for o in r["options"]] == [0.5, 0.6, 0.7, 0.75, 0.8] and r["help"]
    assert BY_KEY["memory_max"]["default"] == 0 and BY_KEY["memory_max"]["help"]
    assert not [k for k in BY_KEY if k.startswith("memory_") and any(w in BY_KEY[k]["label"] + BY_KEY[k]["help"] for w in ("固定字数", "300 字", "300字"))]  # 旧的每层固定字数没有了


# ---------- 5. 按人取回 ----------
def retrieval_setup(e):
    e.add_bot("901")
    mem = e.engine.memory
    now = time.time()
    return mem, now


def test_retrieval_scores_by_uid_sorts_by_score_then_recency_and_ignores_self(env):
    async def body(e):
        mem, now = retrieval_setup(e)
        pad = "。" * 100
        # 5 个已被并进上层的第一层：A 提到甲、B 提到乙、C 提到甲和乙、D 提到丙、E 提到甲
        ids = [add_unit(mem, f"{n}段的事{pad}", t1=now - (10 - i) * DAY, m0=i * 10 + 1, m1=i * 10 + 10, uids=u, merged=1)
               for i, (n, u) in enumerate([("A", ["jia"]), ("B", ["yi"]), ("C", ["jia", "yi"]), ("D", ["bing"]), ("E", ["jia"])])]
        mem.db.x("UPDATE memories SET content=content || ? WHERE content LIKE 'D段%'", ("。" * 400,))  # D 很长：空间不够把它换出来
        add_unit(mem, "上层的概括", level=2, t1=now, m0=1, m1=50, uids=["jia", "yi", "bing"], parts=ids)
        w = {"jia": 2, "yi": 1, "bing": 0, "bot": 5}  # 甲被这条消息提到（+2），乙说过话（+1），丙没有
        units_, roots = mem.load_units("chat:qq:100")
        root_cost = sum(u.cost for u in roots)
        item = len(f"- 10 月 25 日：A段的事{pad}") + 1
        pick = lambda budget: re.findall(r"([ABCDE])段的事", "\n".join(mem.compose("r_haitun", "qq", ROOM, [], "", weights=w, budget=budget)[0]).split("【和眼下这些人有关的旧事】")[-1])
        # 分：C=3，A=2，E=2，B=1，D=0（不取）。只放得下 2 条：先取分最高的 C，再从同分的 A、E 里取较新的 E；取出的按时间排
        assert pick(int((root_cost + 2 * item) / 0.75) + 4) == ["C", "E"]
        # 放得下 4 条：C、A、E、B（D 没分，不取）；按时间排 A、B、C、E
        assert pick(int(4 * item / 0.75) + root_cost + 8) == ["A", "B", "C", "E"]
    env.run(body)


def test_retrieval_caps_75_percent_and_1000_items_and_never_repeats_what_is_shown(env, monkeypatch):
    async def body(e):
        from mb.settings import BY_KEY
        assert BY_KEY["memory_retrieve_share"]["default"] == 75 and BY_KEY["memory_retrieve_max"]["default"] == 1000  # 默认值
        assert BY_KEY["memory_retrieve_share"]["adv"] and BY_KEY["memory_retrieve_max"]["adv"] and set(BY_KEY["memory_retrieve_max"]["scopes"]) == {"G", "C"}
        mem, now = retrieval_setup(e)
        ids = [add_unit(mem, f"事件{i:02d}" + "。" * 200, t1=now - (40 - i) * DAY, m0=i * 2 + 1, m1=i * 2 + 2, uids=["jia"], merged=1) for i in range(30)]
        roots = []
        for g in range(6):
            roots.append(add_unit(mem, f"概括{g}" + "。" * 10, level=2, t1=now - (40 - g * 5) * DAY, parts=ids[g * 5:(g + 1) * 5], uids=["jia"]))
        for r in ids:
            mem.db.x("UPDATE memories SET merged=1 WHERE id=?", (r,))
        coarse = sum(u.cost for u in mem.load_units("chat:qq:100")[1])
        w = {"jia": 2}
        budget = coarse + 1000
        out, used = mem.compose("r_haitun", "qq", ROOM, [], "", weights=w, budget=budget)
        text = "\n".join(out)
        assert len(text) - sum(len(x) for x in out[:0]) <= budget + 80
        retrieved = text.split("【和眼下这些人有关的旧事】")[1] if "【和眼下这些人有关的旧事】" in text else ""
        assert len(retrieved) <= 0.75 * budget + 30
        lines = [x for x in text.splitlines() if x.startswith("- ")]
        assert len(lines) == len(set(lines))  # 同一条只放一次
        # 取回不挤掉最粗一层：每一段历史要么还是概括的那一行，要么换成了它的 5 个下层（全在）
        for g in range(6):
            assert any(f"概括{g}" in x for x in lines) or all(any(f"事件{i:02d}" in x for x in lines) for i in range(g * 5, g * 5 + 5)), g
        # 两个比例 / 条数都是高级设置，全局和单个群都能调
        def n_retrieved(budget=budget):
            t = "\n".join(mem.compose("r_haitun", "qq", ROOM, [], "", weights=w, budget=budget)[0])
            return len([x for x in t.split("【和眼下这些人有关的旧事】")[1].splitlines() if x.startswith("- ")]) if "【和眼下这些人有关的旧事】" in t else 0
        base = n_retrieved()
        assert base >= 3
        setg(e, "memory_retrieve_max", 2)  # 一次至多 2 条
        assert n_retrieved() == 2
        setg(e, "memory_retrieve_max", 1, "chat:qq:100")  # 这个群单独设成 1：比全局优先
        assert n_retrieved() == 1
        e.core.settings.reset("chat:qq:100", "memory_retrieve_max")
        setg(e, "memory_retrieve_max", 1000)
        setg(e, "memory_retrieve_share", 0)  # 占 0%：不取回
        assert n_retrieved() == 0
        setg(e, "memory_retrieve_share", 100, "chat:qq:100")  # 这个群单独设成 100%
        assert n_retrieved() >= base
        view = {i["key"]: i for i in e.core.settings.view("chat:qq:100")}
        assert "memory_retrieve_share" in view and "memory_retrieve_max" in view and view["memory_retrieve_share"]["own"]
    env.run(body)


def test_tight_space_order_people_first_then_coarsest_then_retrieval_squeezed_notes_remain_retrievable(env):
    async def body(e):
        mem, now = retrieval_setup(e)
        key, name = "uid:qq:111", "群友111"
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "impression", key, name, "印象很长" * 5, "inferred", None, ROOM, status="active", meta={"upto": 0})
        mem.add(sp, "person", key, name, "要记住的事", "told", room=ROOM, status="active")
        notes = [mem.add(sp, "person", key, name, f"备忘{i:02d}" + "。" * 20, "self", room=ROOM, status="active", ts=now - (50 - i) * DAY) for i in range(10)]
        ids = [add_unit(mem, f"旧事{i}" + "。" * 20, t1=now - (80 - i) * DAY, m0=i + 1, m1=i + 1, uids=["111"], merged=1) for i in range(5)]
        add_unit(mem, "最粗一层" + "。" * 30, level=2, t1=now - 70 * DAY, parts=ids, uids=["111"])
        setg(e, "memory_person_limit", 10)
        coarse = sum(u.cost for u in mem.load_units("chat:qq:100")[1])
        mandatory = len("- 关于群友111，她目前的印象：" + "印象很长" * 5) + 1 + len(mem.note_line(mem.db.rows("SELECT * FROM memories WHERE source='told'")[0], name)) + 1
        notes_cost = sum(len(mem.note_line(r, name)) + 1 for r in mem.db.rows("SELECT * FROM memories WHERE source='self' ORDER BY id"))
        # 刚好放得下 印象 + 要记住的 + 最粗一层 + 一部分备忘
        budget = mandatory + coarse + notes_cost // 2
        out, _ = mem.compose("r_haitun", "qq", ROOM, [(key, name)], "", weights={"111": 2}, budget=budget)
        text = "\n".join(out)
        assert "印象很长" in text and "要记住的事" in text and "最粗一层" in text  # 必须放的、最粗一层都在
        shown = sorted(int(x) for x in re.findall(r"备忘(\d\d)", text.split("【和眼下这些人有关的旧事】")[0]))
        assert shown and shown[-1] == 9 and shown[0] > 0  # 备忘从最早的让，留下最新的
        part = text.split("【和眼下这些人有关的旧事】")[1] if "【和眼下这些人有关的旧事】" in text else ""
        assert len(text) <= budget + 100
        # 让掉的备忘进了取回的候选：空间多一点就能取回，而且不和摆出来的重复
        out2, _ = mem.compose("r_haitun", "qq", ROOM, [(key, name)], "", weights={"111": 2}, budget=budget + 600)
        t2 = "\n".join(out2)
        nums = re.findall(r"备忘(\d\d)", t2)
        assert len(nums) == len(set(nums)) and len(set(nums)) > len(shown)
    env.run(body)


def test_mem_weights_mentioned_plus_two_spoke_plus_one_self_excluded(env):
    async def body(e):
        b = e.add_bot("901")
        setg(e, "proactive", False)
        for uid, name in (("111", "小甲"), ("222", "小乙"), ("333", "小丙")):
            await e.say(uid, "说话", name=name)
        await e.say("111", "你们看小丙发的那个", name="小甲", at=["222", "901"])
        room = e.engine.rooms[ROOM]
        w = e.engine.mem_weights(room, bot_of(e), [x for x in room.ctx if not x.bot][-1])
        assert w["111"] == 1 and w["222"] == 1 + 2 and w["333"] == 1 + 2 and "901" not in w  # 小乙被 @、小丙被叫名字：+2；都说过话：+1
    env.run(body)


# ---------- 6. 空间：模型能读多长 ----------
def test_context_limit_rules(env, monkeypatch):
    async def body(e):
        llm, db = e.core.llm, e.core.db
        ep = db.doc("endpoint", "default")
        monkeypatch.setattr("mb.llm.providers.model_info", lambda prov, model: None)
        assert llm.context_limit("r_haitun", "qq", ROOM) == 1_000_000  # 什么都没有：按 1M
        monkeypatch.setattr("mb.llm.providers.model_info", lambda prov, model: {"ctx": 128000})
        assert llm.context_limit("r_haitun", "qq", ROOM) == 128000  # 目录里报的
        llm.learn_context(ep, e.core.settings.get("main_model", "r_haitun"), rejected=64000)
        assert llm.context_limit("r_haitun", "qq", ROOM) == 64000  # 被拒后记下的：取最小
        llm.learn_context(ep, e.core.settings.get("main_model", "r_haitun"), ok=90000)
        assert llm.context_limit("r_haitun", "qq", ROOM) == 90000  # 成功发出过的最大请求作为底
        db.put("endpoint", {**ep, "context_tokens": 32000})
        assert llm.context_limit("r_haitun", "qq", ROOM) == 32000  # 用户填的优先
        db.put("endpoint", {**ep, "context_tokens": 0})
    env.run(body)


def test_overflow_error_parsing():
    cases = {"HTTP 400: {\"error\": \"This model's maximum context length is 131072 tokens. However, you requested 150000\"}": (True, 131072),
             "HTTP 400: prompt is too long: 250000 tokens > 200000 maximum": (True, 200000),
             "HTTP 400: Range of input length should be [1, 129024]": (True, 129024),
             "HTTP 400: input token count (1048577) exceeds the maximum number of tokens allowed (1048576)": (True, 1048576),
             "HTTP 400: the request is too long": (True, None), "HTTP 401: bad key": (False, None), "HTTP 429: slow down": (False, None)}
    for msg, want in cases.items():
        assert overflow_info(msg) == want, msg


def test_request_rejected_for_being_too_long_is_relaid_out_and_resent_without_losing_the_reply(env):
    async def body(e):
        bot = setup(e, seg=2, merge=50)
        e.llm.main = lambda s, u: "照常回复"
        now = time.time()
        mem = e.engine.memory
        for i in range(20):  # 一大堆第一层，空间不限时全会摆进提示词
            add_unit(mem, f"旧事{i:02d}" + "。" * 400, t1=now - (100 - i) * DAY, m0=i + 1, m1=i + 1, uids=[])
        room = e.engine.room("qq", "100", "测试群")
        room.accounts.add("ac_901")
        base = len(await e.engine.system_prompt(e.engine.duty(e.engine.bots["ac_901"], room), room))
        assert base > 8000
        e.llm.reject_over = (6000, 3000)  # 超过 6000 字就被拒，报错里写明上限是 3000 tokens
        await e.say("111", "你好", at=["901"])
        assert e.llm.rejected >= 1 and bot.sent and bot.sent[-1][2] == "照常回复"  # 重摆后重发，这一次回复不丢
        learned = e.core.db.kv("ctx_learn")
        assert list(learned.values())[0]["rejected"] == 3000
        main = [c for c in e.llm.calls if c["_kind"] == "main"][-1]
        assert len(main["messages"][0]["content"]) < base and "旧事00" not in main["messages"][0]["content"]
        # 记下了：下一次一上来就按新上限摆，不再被拒
        n = e.llm.rejected
        await e.say("111", "再来一次", at=["901"])
        assert e.llm.rejected == n and len(bot.sent) == 2
    env.run(body)


# ---------- 7. 删除、修改、撤销 ----------
def history_with_people(e):
    """5 个第一层并成 1 个第二层；甲（uid 111）出现在第 2、4 段，乙（222）只出现在第 1 段。"""
    mem = e.engine.memory
    now = time.time()
    uids = [["222"], ["111", "333"], ["333"], ["111"], ["333"]]
    ids = [add_unit(mem, f"第{i + 1}段 {'甲' if '111' in u else '别人'}的事", t1=now - (10 - i) * DAY, m0=i * 10 + 1, m1=i * 10 + 10, uids=u, merged=1) for i, u in enumerate(uids)]
    up = add_unit(mem, "第二层概括", level=2, t1=now, m0=1, m1=50, uids=["111", "222", "333"], parts=ids)
    return ids, up


def test_forget_me_removes_notes_impression_units_and_their_uppers_and_can_be_undone(env):
    async def body(e):
        bot = e.add_bot("901")
        setg(e, "proactive", False)
        mem = e.engine.memory
        ids, up = history_with_people(e)
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "person", "uid:qq:111", "群友111", "喜欢猫", "self", room=ROOM, status="active")
        mem.add(sp, "impression", "uid:qq:111", "群友111", "印象文字", "inferred", None, ROOM, status="active", meta={"upto": 1})
        mem.add(sp, "person", "uid:qq:333", "群友333", "别人的备忘", "self", room=ROOM, status="active")
        before = {r["id"]: dict(r) for r in e.core.db.rows("SELECT * FROM memories")}
        await e.say("111", "忘掉我", at=["901"])
        db = e.core.db
        assert db.x("SELECT COUNT(*) FROM memories WHERE subject='uid:qq:111'").fetchone()[0] == 0  # 备忘和印象
        left = {r["id"] for r in db.rows("SELECT id FROM memories WHERE kind='recap'")}
        assert left == {ids[0], ids[2], ids[4]}  # 含他的第 2、4 段，和覆盖它们的第二层一起删了
        assert all(r["merged"] == 0 for r in db.rows(f"SELECT merged FROM memories WHERE id IN ({ids[0]},{ids[2]},{ids[4]})"))  # 剩下的下层变回没被并，以后重新并
        assert db.x("SELECT COUNT(*) FROM memories WHERE subject='uid:qq:333'").fetchone()[0] == 1  # 别人的不动
        assert "前情里有你的部分" in bot.sent[-1][2]
        # 走现有的备份，能撤销：编号、范围、所并各段都回来
        info = e.core.reset.undo()
        after = {r["id"]: dict(r) for r in db.rows("SELECT * FROM memories")}
        assert set(after) == set(before) and all(after[i]["content"] == before[i]["content"] and after[i]["merged"] == before[i]["merged"] and after[i]["meta"] == before[i]["meta"] for i in before)
    env.run(body)


def test_remerge_after_a_delete_uses_the_remaining_lower_units(env):
    async def body(e):
        setup(e, seg=2, merge=3)
        e.llm.mem = lambda p: {"recap": "一段", "people": [], "memes": []}
        e.llm.merge = lambda p: "并"
        await say_n(e, 6)
        mem = e.engine.memory
        first = l1(e.core.db)
        assert len(units(e.core.db)) == 4
        up = units(e.core.db)[-1]["id"]
        assert mem.delete_units([up]) == 1  # 删上层：下层变回没被并
        assert [r["merged"] for r in l1(e.core.db)] == [0, 0, 0]
        await mem.merge_up(mem.space("recap", None, "qq", ROOM), ROOM, "测试群")  # 之后由剩下的下层重新并
        assert [r["level"] for r in units(e.core.db)] == [1, 1, 1, 2] and all(r["merged"] == 1 for r in l1(e.core.db))
    env.run(body)


def test_forget_text_deletes_units_with_it_and_rewrites_impressions_that_mention_it(env):
    async def body(e):
        bot = e.add_bot("901")
        setg(e, "proactive", False)
        mem = e.engine.memory
        now = time.time()
        a = add_unit(mem, "大家聊了披萨店", t1=now - 3 * DAY, m0=1, m1=5, uids=["111"], merged=1)
        b = add_unit(mem, "别的事", t1=now - 2 * DAY, m0=6, m1=9, uids=["111"], merged=1)
        up = add_unit(mem, "概括", level=2, t1=now, m0=1, m1=9, uids=["111"], parts=[a, b])
        sp = "iso:qq:100:person:r_haitun"
        n1 = mem.add(sp, "person", "uid:qq:111", "群友111", "开了披萨店", "self", room=ROOM, status="active")
        n2 = mem.add(sp, "person", "uid:qq:111", "群友111", "喜欢猫", "self", room=ROOM, status="active")
        mem.add(sp, "impression", "uid:qq:111", "群友111", "他开披萨店、养猫", "inferred", None, ROOM, status="active", meta={"upto": n2})
        e.llm.impression = lambda p: "他养猫" if "披萨" not in p.split("【新增的备忘】")[1] else "错"
        await e.say(HAITUN, "忘掉披萨", at=["901"])
        await e.engine.idle()
        db = e.core.db
        assert {r["id"] for r in db.rows("SELECT id FROM memories WHERE kind='recap'")} == {b}  # 含「披萨」的第一层和它的上层删了，另一个下层变回没被并
        assert db.x("SELECT merged FROM memories WHERE id=?", (b,)).fetchone()[0] == 0
        assert db.x("SELECT COUNT(*) FROM memories WHERE content LIKE '%披萨%' AND kind!='impression'").fetchone()[0] == 0
        imp = mem.impression_row(sp, "uid:qq:111")
        assert imp and imp["content"] == "他养猫"  # 印象里涉及被删内容的，用剩下的备忘重写
        assert "备份" in bot.sent[-1][2]
        e.core.reset.undo()  # 能撤销，原来的印象回来，重写出来的那份不留两份
        rows = db.rows("SELECT content FROM memories WHERE kind='impression'")
        assert [r["content"] for r in rows] == ["他开披萨店、养猫"] and len(db.rows("SELECT id FROM memories WHERE kind='recap'")) == 3
    env.run(body)


def test_reset_memory_takes_units_uppers_and_impressions_and_undo_restores_them(env):
    async def body(e):
        e.add_bot("901")
        mem = e.engine.memory
        ids, up = history_with_people(e)
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "impression", "uid:qq:111", "群友111", "印象", "inferred", None, ROOM, status="active", meta={"upto": 1})
        before = e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0]
        e.core.reset.run({"type": "room", "id": ROOM}, ["person", "recap"])
        assert e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        e.core.reset.undo()
        assert e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == before
        assert meta(e.core.db.rows("SELECT * FROM memories WHERE id=?", (up,))[0])["parts"] == ids
    env.run(body)


def test_panel_can_view_edit_and_delete_units_and_impressions(env):
    async def body(e):
        bot = e.add_bot("901")
        mem = e.engine.memory
        ids, up = history_with_people(e)
        sp = "iso:qq:100:person:r_haitun"
        imp = mem.add(sp, "impression", "uid:qq:111", "群友111", "印象", "inferred", None, ROOM, status="active", meta={"upto": 1})
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        items = (await (await c.get("/api/memories?kind=recap")).json())["items"]
        top = next(i for i in items if i["id"] == up)
        assert top["level"] == 2 and top["meta"]["parts"] == ids and top["meta"]["t0"] and top["meta"]["m0"] == 1  # 层级、范围、所并各段都看得到
        assert next(i for i in (await (await c.get("/api/memories?kind=impression")).json())["items"])["id"] == imp
        e.llm.merge = lambda p: "按改过的重写：" + ("甲改过了" if "甲改过了" in p else "没改")
        r = await c.put(f"/api/memories/{ids[1]}", json={"content": "甲改过了"})
        assert r.status == 200
        await e.engine.idle()
        assert e.core.db.rows("SELECT content FROM memories WHERE id=?", (up,))[0]["content"] == "按改过的重写：甲改过了"  # 改过的单位，覆盖它的上层照改过的重写
        await c.put(f"/api/memories/{imp}", json={"content": "新印象"})
        assert e.core.db.rows("SELECT content FROM memories WHERE id=?", (imp,))[0]["content"] == "新印象"
        await c.delete(f"/api/memories/{ids[0]}")  # 删一个下层：覆盖它的上层一起删，其余下层变回没被并
        assert not e.core.db.rows("SELECT id FROM memories WHERE id=?", (up,)) and all(r["merged"] == 0 for r in e.core.db.rows(f"SELECT merged FROM memories WHERE id IN ({','.join(map(str, ids[1:]))})"))
        await c.delete(f"/api/memories/{imp}")
        assert not e.core.db.rows("SELECT id FROM memories WHERE id=?", (imp,))
        await c.close()
    env.run(body)


# ---------- 8. 隔离 ----------
def test_nothing_leaks_between_chats_and_private_chats(env):
    async def body(e):
        e.add_bot("901")
        mem = e.engine.memory
        now = time.time()
        add_unit(mem, "A群的前情", t1=now, m0=1, m1=2, uids=["111"])
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "person", "uid:qq:111", "群友111", "A群的备忘", "self", room=ROOM, status="active")
        mem.add(sp, "impression", "uid:qq:111", "群友111", "A群的印象", "inferred", None, ROOM, status="active", meta={"upto": 0})
        w = {"111": 3}
        subj = [("uid:qq:111", "群友111")]
        a = "\n".join(mem.compose("r_haitun", "qq", ROOM, subj, "", weights=w, budget=10 ** 6)[0])
        assert "A群的前情" in a and "A群的备忘" in a and "A群的印象" in a
        for chat, dm in (("qq:200", None), (None, "dm:ac_901:111")):
            other = "\n".join(mem.compose("r_haitun", "qq", chat, subj, "", dm=dm, weights=w, budget=10 ** 6)[0])
            assert "A群" not in other, (chat, dm)
        # 同一个人在 B 群有自己的印象，A 群看不到
        mem.add("iso:qq:200:person:r_haitun", "impression", "uid:qq:111", "群友111", "B群的印象", "inferred", None, "qq:200", status="active", meta={"upto": 0})
        assert "B群的印象" not in "\n".join(mem.compose("r_haitun", "qq", ROOM, subj, "", weights=w, budget=10 ** 6)[0])
        # 自建空间绑起来才共享
        e.core.db.put("space", {"id": "s1", "name": "共用", "kinds": ["recap", "person"], "bindings": [{"chat": "qq:100"}, {"chat": "qq:200"}]})
        shared = "\n".join(mem.compose("r_haitun", "qq", "qq:200", subj, "", weights=w, budget=10 ** 6)[0])
        assert mem.space("recap", "r_haitun", "qq", "qq:200") == "sp:s1" and "A群的前情" not in shared  # 绑进去之后读的是共用空间里的（还没有东西）
    env.run(body)


# ---------- 9. 老数据迁移 ----------
def test_migration_fills_ranges_it_can_derive_and_leaves_the_rest(env):
    async def body(e):
        e.add_bot("901")
        db, mem = e.core.db, e.engine.memory
        now = time.time()
        for i in range(10):  # 老的消息表（还没过期的）
            db.x("INSERT INTO messages (room, ts, account_id, msg_id, user_id, name, text, role_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                 (ROOM, now - (10 - i) * 3600, "", "", "111" if i % 2 else "222", "x", f"m{i}", "", "human"))
        mids = [r["id"] for r in db.rows("SELECT id FROM messages ORDER BY id")]

        def old(content, level, ts, src=None, merged=0):
            return db.x("""INSERT INTO memories (space, kind, subject, subject_name, content, source, src, level, status, ts, room, merged) VALUES
                           ('chat:qq:100','recap','','',?,'inferred',?,?,'active',?,?,?)""", (content, json.dumps(src or []), level, ts, ROOM, merged)).lastrowid
        a = old("老的第一段", 1, now - int(5.5 * 3600), merged=1)
        b = old("老的第二段", 1, now, merged=1)
        c = old("没有来历的老上层", 2, now)  # src 空：推不出所并各段，照原样用
        d = old("可以对回去的老上层", 2, now, src=[{"name": "下层摘要", "text": "老的第一段"}, {"name": "下层摘要", "text": "老的第二段"}])
        db.del_kv("memory_v2")
        out = e.core.migrate_memory_v2()
        assert out["l1"] == 2 and out["upper"] == 1
        ma, mb_, md, mc = (meta(db.rows("SELECT * FROM memories WHERE id=?", (i,))[0]) for i in (a, b, d, c))
        assert ma["m1"] == mids[4] and ma["uids"] == ["111", "222"] and mb_["m0"] == mids[5] and mb_["m1"] == mids[9] and ma["t1"] < mb_["t1"]
        assert md["parts"] == [a, b] and md["m0"] == ma["m0"] and md["m1"] == mb_["m1"] and md["t0"] == ma["t0"]
        assert mc == {}  # 对不上的：不动
        # 老的上层照样能摆进提示词（用创建时间当时间范围）
        text = "\n".join(mem.compose("r_haitun", "qq", ROOM, [], "", budget=10 ** 6)[0])
        assert "老的第一段" in text and "老的第二段" in text
        assert e.core.migrate_memory_v2() is None  # 只跑一次
        assert any(p.name.startswith("pre-memory-v2-") for p in (e.core.data / "backups").glob("*.db"))  # 跑之前备份了整库
        # 数据一条没少、没删：只是补了范围
        assert db.x("SELECT COUNT(*) FROM memories WHERE kind='recap'").fetchone()[0] == 4
    env.run(body)


def test_endpoint_context_tokens_is_saved_and_settings_have_english(env):
    async def body(e):
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        ep = e.core.db.doc("endpoint", "default")
        await c.put("/api/docs/endpoint", json={**ep, "context_tokens": 65536})
        assert e.core.llm.context_limit("r_haitun", "qq", ROOM) == 65536
        await c.close()
        import json as _j
        from pathlib import Path
        en = _j.loads((Path(__file__).resolve().parent.parent / "web/i18n/en.json").read_text(encoding="utf-8"))
        from mb.settings import BY_KEY
        for k in ("memory_ratio", "memory_max", "memory_person_limit"):
            assert BY_KEY[k]["label"] in en and BY_KEY[k]["help"] in en
        assert "模型能读多长（tokens，可不填）" in en
    env.run(body)
