"""表情包描述字数可调、改了字数自动重新描述、发表情包改成「给她看候选、她自己挑」、微信发完后的日志降级。全部用桩，不往任何群、任何私聊发消息。"""
import json
import time
from types import SimpleNamespace

from test_round35 import logs
from test_round36 import fav_bot, rows, setg
from test_round41 import add_fav, dm, dm_bot, want_sticker
from test_round5b import png
from wxworld import World

from mb import wxsticker
from mb.stickers import FAV_PROMPT, GROUP_PROMPT, VISION_PROMPT, OLD_DESC_MAX


def vis_texts(e):
    return [c["messages"][-1]["content"][-1]["text"] for c in e.llm.calls if c.get("_kind") == "vis"]


def add_group_sticker(e, room_key="qq:100", descr="旧描述", n=21, desc_max=None, file=True):
    """群里学来的、已学会的表情包（图片文件、记忆、向量都有）。"""
    st = e.engine.stickers
    sp = f"chat:{room_key}"
    name = f"grp{n}.png"
    if file:
        (st.folder() / name).write_bytes(png(n))
    mid = e.engine.memory.add(sp, "sticker", f"{n:016x}", "表情包", descr, "inferred", None, room_key, status="active")
    e.core.db.x("INSERT OR REPLACE INTO memvec VALUES (?,?,?)", (mid, "old-sig", b"\x00\x00\x00\x00"))
    cur = e.core.db.x("INSERT INTO stickers (space, room, phash, seen, users, status, file, size, uses, descr, mem_id, desc_max) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sp, room_key, f"{n:016x}", 3, "[]", "learned", name, 1, 0, descr, mid, desc_max))
    return cur.lastrowid


def row_of(e, sid):
    return e.core.db.rows("SELECT * FROM stickers WHERE id=?", (sid,))[0]


# ---------- 一、描述字数 ----------
def test_the_new_settings_are_global_advanced_tools_with_the_agreed_defaults():
    from mb.settings import BY_KEY
    for key, default, typ in (("sticker_desc_max", 250, "int"), ("sticker_desc_auto", True, "bool"), ("sticker_candidates", 10, "int")):
        s = BY_KEY[key]
        assert (s["default"], s["type"], s["cat"], s["adv"], s["scopes"]) == (default, typ, "工具", True, ["G"])
    assert BY_KEY["sticker_desc_max"]["label"] == "表情包描述最多多少字"
    assert BY_KEY["sticker_desc_auto"]["label"] == "改了字数后自动重新描述已有的"
    assert BY_KEY["sticker_candidates"]["label"] == "发表情包时给她看几张候选"


def test_prompts_carry_the_configured_length_and_new_descriptions_record_it(env):
    async def body(e):
        ch = fav_bot(e)
        st = e.engine.stickers
        assert st.desc_max() == 250  # 默认 250
        e.llm.vis = lambda content: {"desc": "长" * 300}  # 模型不听话写太长：按设置截
        ch.imgs["http://qq/a.png"] = png(11)
        ch.favs = ["http://qq/a.png"]
        await st.sync_favorites(e.engine.bots["ac_900"])
        assert vis_texts(e)[-1] == FAV_PROMPT.replace("{n}", "250")
        assert "用中文（250字内）写清：画面里是什么，图上的文字照原样写出（没有就不写），表达什么情绪，适合在什么场合发。只输出这段描述。" in vis_texts(e)[-1]
        r = rows(e)[0]
        assert len(r["descr"]) == 250 and r["desc_max"] == 250
        # 改成 120：新收的按 120；带内容检查的提示词也跟着
        setg(e, "sticker_desc_max", 120)
        setg(e, "sticker_fav_check", True)
        e.llm.vis = lambda content: {"sticker": True, "ok": True, "desc": "短" * 300}
        ch.imgs["http://qq/b.png"] = png(12)
        ch.favs = ["http://qq/a.png", "http://qq/b.png"]
        await st.sync_favorites(e.engine.bots["ac_900"])
        t = vis_texts(e)[-1]
        assert t == VISION_PROMPT.replace("{n}", "120") and "中文，120字内：画面里是什么，图上的文字照原样写出（没有就不写），表达什么情绪，适合在什么场合发" in t
        new = [x for x in rows(e) if x["descr"].startswith("短")][0]
        assert len(new["descr"]) == 120 and new["desc_max"] == 120
    env.run(body)


def test_a_group_learned_sticker_is_cut_and_marked_with_the_current_length(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "sticker_desc_max", 150)
        e.llm.vis = lambda content: {"sticker": True, "ok": True, "desc": "字" * 300}
        bot.imgs["http://img/0"] = png(1)
        for u in ("111", "112"):
            await e.say(u, "", images=["http://img/0"])
            await e.engine.idle()
        assert vis_texts(e)[-1] == VISION_PROMPT.replace("{n}", "150")
        r = e.core.db.rows("SELECT * FROM stickers WHERE status='learned'")[0]
        assert len(r["descr"]) == 150 and r["desc_max"] == 150
        assert e.core.db.rows("SELECT content FROM memories WHERE id=?", (r["mem_id"],))[0]["content"] == r["descr"]
    env.run(body)


# ---------- 二、自动重新描述 ----------
def test_only_stickers_described_under_another_length_are_picked_old_records_count_as_40(env):
    async def body(e):
        fav_bot(e)
        st = e.engine.stickers
        a = add_group_sticker(e, n=21, desc_max=None)  # 旧记录：按 40 算
        b = add_group_sticker(e, n=22, desc_max=250)
        c = add_group_sticker(e, n=23, desc_max=120)
        assert OLD_DESC_MAX == 40
        assert {r["id"] for r in st.redesc_todo()} == {a, c}
        setg(e, "sticker_desc_max", 40)
        assert {r["id"] for r in st.redesc_todo()} == {b, c}  # 旧记录正好和 40 相同：不用重描
        assert {r["id"] for r in st.redesc_todo(force=True)} == {a, b, c}
        # 没学会的（拒绝的）不动
        e.core.db.x("UPDATE stickers SET status='rejected' WHERE id=?", (b,))
        assert {r["id"] for r in st.redesc_todo(force=True)} == {a, c}
    env.run(body)


def test_the_minute_check_starts_a_round_only_when_auto_is_on_and_then_rewrites_everything_that_changed(env):
    async def body(e):
        fav_bot(e)
        setg(e, "sticker_fav_hours", 0)  # 不让每分钟检查顺带去同步收藏（桩的收藏是空的，会把手填的收藏删掉）
        e.llm.vis = lambda content: {"desc": "新描述：一只猫翻白眼，图上写着“无语”，表示无语，适合吐槽时发"}
        g = add_group_sticker(e, n=21, descr="旧", desc_max=None)
        f = add_fav(e, descr="旧的收藏描述", n=31)  # add_fav 不记 desc_max：旧记录
        ok = add_group_sticker(e, n=22, descr="不用动", desc_max=250)
        # 开关关着：不重描
        setg(e, "sticker_desc_auto", False)
        e.engine.fav_tick(time.time())
        await e.engine.idle()
        assert "vis" not in [c.get("_kind") for c in e.llm.calls] and row_of(e, g)["descr"] == "旧"
        # 开着：两张按 250 重描，已经是 250 的不动
        setg(e, "sticker_desc_auto", True)
        flushed = []
        orig = e.engine.memory.flush_index

        async def spy():
            flushed.append(list(e.engine.memory.pending))
            await orig()
        e.engine.memory.flush_index = spy
        mems = {i: row_of(e, i)["mem_id"] for i in (g, f, ok)}
        e.engine.memory.pending.clear()  # 手填这几条时排进去的，不算重描的
        e.engine.fav_tick(time.time())
        await e.engine.idle()
        texts = vis_texts(e)
        assert sorted(texts) == sorted([GROUP_PROMPT.replace("{n}", "250"), FAV_PROMPT.replace("{n}", "250")])
        assert GROUP_PROMPT.replace("{n}", "250").startswith("这是群聊里常用的一张表情包。用中文（250字内）写清：")
        for i in (g, f):
            r = row_of(e, i)
            assert r["descr"].startswith("新描述") and r["desc_max"] == 250 and r["status"] == "learned"
            assert e.core.db.rows("SELECT content FROM memories WHERE id=?", (r["mem_id"],))[0]["content"] == r["descr"]  # 记忆内容跟着换
            assert not e.core.db.rows("SELECT 1 FROM memvec WHERE mem_id=? AND sig='old-sig'", (r["mem_id"],))  # 旧向量删了
        assert {mems[g], mems[f]} <= set(flushed[0]) and mems[ok] not in flushed[0]  # 放进待算队列、随后算向量
        assert row_of(e, ok)["descr"] == "不用动"
        assert logs(e, "表情包重新描述：开始，共 2 张（按 250 字）") and logs(e, "表情包重新描述：完成 2 张，跳过 0 张（留到下一轮）")
        # 都对上了：再检查不会再起一轮
        n = len(vis_texts(e))
        e.engine.fav_tick(time.time())
        await e.engine.idle()
        assert len(vis_texts(e)) == n
        # 改了设置：下一次检查又自动来一轮
        setg(e, "sticker_desc_max", 80)
        e.engine.fav_tick(time.time())
        await e.engine.idle()
        assert len(vis_texts(e)) == n + 3 and {row_of(e, i)["desc_max"] for i in (g, f, ok)} == {80}
        assert VISION_PROMPT.replace("{n}", "80") not in vis_texts(e)  # 重描不再判断合不合适
    env.run(body)


def test_the_button_redoes_everything_ignoring_the_stored_length(env):
    async def body(e):
        fav_bot(e)
        e.llm.vis = lambda content: {"desc": "按钮重描的"}
        ids = [add_group_sticker(e, n=21 + i, desc_max=250) for i in range(3)]
        setg(e, "sticker_desc_auto", False)
        res = await e.engine.stickers.redescribe(force=True)
        assert res == {"total": 3, "done": 3, "skipped": 0}
        assert all(row_of(e, i)["descr"] == "按钮重描的" for i in ids)
        # 面板接口：后台起一轮，马上返回张数；已经有一轮在跑就说在跑
        from mb.api2 import Api2
        api = SimpleNamespace(eng=e.engine)
        e.llm.vis = lambda content: {"desc": "接口重描的"}
        out = json.loads((await Api2.stickers_redescribe(api, None)).text)
        assert out == {"running": False, "total": 3}
        await e.engine.idle()
        assert all(row_of(e, i)["descr"] == "接口重描的" for i in ids)
        e.engine.stickers.busy.add("redesc")
        assert json.loads((await Api2.stickers_redescribe(api, None)).text)["running"] is True
        e.engine.stickers.busy.discard("redesc")
    env.run(body)


def test_missing_image_a_failing_call_and_the_hard_cap_skip_that_sticker_and_leave_it_for_the_next_round(env):
    async def body(e):
        fav_bot(e)
        st = e.engine.stickers

        gone = add_group_sticker(e, n=21, descr="图没了", desc_max=40, file=False)
        bad = add_group_sticker(e, n=22, descr="会出错", desc_max=40)
        fine = add_group_sticker(e, n=23, descr="正常", desc_max=40)
        e.llm.vis = lambda content: (_ for _ in ()).throw(RuntimeError("看图接口出错"))
        res = await st.redescribe()
        assert res == {"total": 3, "done": 0, "skipped": 3}  # 图没了的、调用出错的：都跳过
        assert logs(e, "表情包重新描述：完成 0 张，跳过 3 张（留到下一轮）")
        assert all(row_of(e, i)["desc_max"] == 40 for i in (gone, bad, fine)) and row_of(e, bad)["descr"] == "会出错"  # 没动
        # 下一轮：接口好了，出错的补上；图没了的还是跳过
        e.llm.vis = lambda content: {"desc": "好了"}
        res = await st.redescribe()
        assert res == {"total": 3, "done": 2, "skipped": 1}
        assert row_of(e, bad)["desc_max"] == 250 and row_of(e, fine)["descr"] == "好了"
        assert row_of(e, gone)["descr"] == "图没了" and row_of(e, gone)["desc_max"] == 40
        # 引擎层：这一轮有跳过，十分钟内不再自动起新的一轮
        e.engine.redesc_next = 0.0
        await e.engine.redesc_round(1000.0)
        assert e.engine.redesc_next == 1600.0
        # 用量到硬上限：整轮跳过，不调看图
        e.engine.llm.hard_capped = lambda role=None: True
        e.core.db.x("UPDATE stickers SET desc_max=40")
        e.llm.vis = lambda content: (_ for _ in ()).throw(AssertionError("硬上限时不该调看图"))
        before = len(e.llm.calls)
        res = await st.redescribe()
        assert res["done"] == 0 and res["skipped"] == res["total"] and len(e.llm.calls) == before
    env.run(body)


def test_no_vision_model_means_no_round_and_no_log(env):
    async def body(e):
        fav_bot(e)
        add_group_sticker(e, n=21, desc_max=None)
        setg(e, "vis_model", "")
        res = await e.engine.stickers.redescribe()
        assert res == {"total": 0, "done": 0, "skipped": 0} and not logs(e, "表情包重新描述")
    env.run(body)


# ---------- 三、给她看候选，她自己挑 ----------
def make_favs(e, count=5):
    """按 uses 从大到小排好顺序的收藏：表情1 用得最多。"""
    ids = [add_fav(e, descr=f"描述{i}", n=40 + i) for i in range(1, count + 1)]
    for k, sid in enumerate(ids):
        e.core.db.x("UPDATE stickers SET uses=? WHERE id=?", (100 - k, sid))
    return ids


def talk(e, reply, mood="无语"):
    """群里被@一次：主模型先调 send_sticker，再回 reply。返回（给她的工具结果, 渠道）。"""
    seen = []
    e.llm.tool_plan = lambda b: ("send_sticker", {"mood": mood}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
    e.llm.main = lambda s, u: (seen.append(u), reply)[1]
    return seen


def test_the_tool_answers_with_numbered_descriptions_up_to_the_configured_count_most_used_first_never_random(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        ids = make_favs(e, 5)
        setg(e, "sticker_candidates", 3)
        seen = talk(e, "行吧")
        await e.say("111", "你觉得呢", at=["900"])
        assert seen[0] == "找到这几张：\n1. 描述1\n2. 描述2\n3. 描述3\n想发哪张，就在回复最后单独写一行【表情+编号】，例如【表情2】；都不合适就不写。"
        assert ch.sent[-1][2] == "行吧" and not getattr(ch, "images", [])  # 没写标记：只发文字
        # 默认 10：全部 5 张；稳定，不随机
        setg(e, "sticker_candidates", 10)
        room = e.engine.rooms["qq:100"]
        b = e.engine.bots["ac_900"]
        orders = {tuple(r["id"] for r in await e.engine.stickers.candidates(room, "无语", b)) for _ in range(6)}
        assert orders == {tuple(ids)}
    env.run(body)


def test_recently_sent_stickers_go_to_the_back_and_a_matching_mood_comes_first(env):
    async def body(e):
        fav_bot(e)
        ids = make_favs(e, 4)
        room = e.engine.rooms.get("qq:100") or e.engine.room("qq", "100", "群")
        b = e.engine.bots["ac_900"]
        st = e.engine.stickers
        e.core.db.x("UPDATE stickers SET last_used=? WHERE id=?", (time.time() - 30, ids[0]))  # 刚发过
        assert [r["id"] for r in await st.candidates(room, "无语", b, 10)] == ids[1:] + [ids[0]]
        e.core.db.x("UPDATE stickers SET last_used=? WHERE id=?", (time.time() - 3600, ids[0]))  # 过了十分钟：不用靠后
        assert [r["id"] for r in await st.candidates(room, "无语", b, 10)] == ids
        # 字面能找到的优先（向量用不了时）
        e.core.db.x("UPDATE stickers SET descr='开心鼓掌' WHERE id=?", (ids[2],))
        e.core.db.x("UPDATE memories SET content='开心鼓掌' WHERE id=(SELECT mem_id FROM stickers WHERE id=?)", (ids[2],))
        assert [r["id"] for r in await st.candidates(room, "鼓掌", b, 10)] == [ids[2]]
        assert await st.candidates(room, "无语", None, 10) == []  # 没有账号、本群又没有学来的：空
    env.run(body)


def test_the_tool_says_there_is_nothing_when_there_are_no_candidates_and_the_last_call_wins(env):
    async def body(e):
        fav_bot(e)
        make_favs(e, 4)
        room = e.engine.room("qq", "100", "群")
        b = e.engine.duty(e.engine.bots["ac_900"], room)
        tools, handler, _ = e.engine.tools_for(b, room, called=True)
        skey = (room.key, b.aid)
        setg(e, "sticker_candidates", 2)
        await handler("send_sticker", {"mood": "甲"})
        assert len(e.engine.stk_cand[skey]["rows"]) == 2
        setg(e, "sticker_candidates", 4)
        out = await handler("send_sticker", {"mood": "乙"})
        assert e.engine.stk_cand[skey]["mood"] == "乙" and len(e.engine.stk_cand[skey]["rows"]) == 4 and "4. 描述4" in out  # 以最后一次为准
        assert e.engine.take_sticker_choice(skey, "好【表情4】", "群", b) == "好" and e.engine.stk_out[skey][0]["descr"] == "描述4"
        # 一张都找不到：照旧那句话，也不留候选
        e.engine.stk_out.clear()
        await handler("send_sticker", {"mood": "丙"})
        setg(e, "sticker_fav_use", False)
        assert await handler("send_sticker", {"mood": "丁"}) == "这个群里没有合适的表情包，直接用文字吧"
        assert skey not in e.engine.stk_cand
        assert e.engine.take_sticker_choice(skey, "好【表情1】", "群", b) == "好" and skey not in e.engine.stk_out
    env.run(body)


def test_a_valid_number_sends_that_sticker_with_the_mood_and_position_in_the_log(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        setg(e, "sticker_candidates", 3)
        ids = make_favs(e, 5)
        talk(e, "行吧\n【表情2】")
        await e.say("111", "你觉得呢", at=["900"])
        assert ch.sent[-1][2] == "行吧" and "【表情" not in ch.sent[-1][2] and len(getattr(ch, "images", [])) == 1  # 标记删干净
        assert [r["uses"] for r in e.core.db.rows("SELECT uses FROM stickers ORDER BY id")] == [100, 100, 98, 97, 96]  # 第 2 张（本来 99）发过一次
        assert logs(e, "（表情包，心情「无语」，3 张里挑了第 2 张）：描述2")
        assert not logs(e, "没挑，只发文字")
        room = e.engine.rooms["qq:100"]
        assert len(room.hourly["ac_900"]) == 1
    env.run(body)


def test_several_markers_take_the_last_valid_one_and_all_are_removed(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        setg(e, "sticker_candidates", 3)
        make_favs(e, 5)
        talk(e, "好【表情1】呀\n【表情 9】\n【表情3】\n【表情7】")
        await e.say("111", "你觉得呢", at=["900"])
        assert "【" not in ch.sent[-1][2] and len(getattr(ch, "images", [])) == 1
        assert logs(e, "3 张里挑了第 3 张")
    env.run(body)


def test_out_of_range_or_missing_marker_sends_text_only_and_logs_that_she_looked(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        setg(e, "sticker_candidates", 3)
        make_favs(e, 5)
        talk(e, "行吧【表情9】")  # 越界
        await e.say("111", "你觉得呢", at=["900"])
        assert ch.sent[-1][2] == "行吧" and not getattr(ch, "images", [])
        assert logs(e, "看了 3 张表情包候选（心情「无语」），没挑，只发文字")
        talk(e, "行吧")  # 没写标记
        await e.say("111", "再问", at=["900"])
        assert ch.sent[-1][2] == "行吧" and not getattr(ch, "images", [])
        assert len(logs(e, "看了 3 张表情包候选（心情「无语」），没挑，只发文字")) == 2
    env.run(body)


def test_a_marker_without_calling_the_tool_is_only_removed(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        make_favs(e, 3)
        e.llm.tool_plan = None
        e.llm.main = lambda s, u: "行吧【表情1】"
        await e.say("111", "你觉得呢", at=["900"])
        assert ch.sent[-1][2] == "行吧" and not getattr(ch, "images", [])
        assert not logs(e, "看了")
    env.run(body)


def test_only_a_sticker_and_no_text_still_sends_the_sticker(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        make_favs(e, 3)
        talk(e, "【表情1】")
        await e.say("111", "你觉得呢", at=["900"])
        assert not ch.sent and len(getattr(ch, "images", [])) == 1
    env.run(body)


def test_shadow_mode_logs_the_pick_but_sends_nothing(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        setg(e, "shadow", True, "role:r_haitun")
        make_favs(e, 3)
        talk(e, "行吧\n【表情2】")
        await e.say("111", "影子里问", at=["900"])
        assert not getattr(ch, "images", [])
        assert logs(e, "（影子）本来会发表情包（心情「无语」，3 张里挑了第 2 张）：描述2")
    env.run(body)


def test_private_chat_works_the_same_way(env):
    async def body(e):
        bot = dm_bot(e)
        make_favs(e, 4)
        want_sticker(e)
        seen = []
        e.llm.main = lambda s, u: (seen.append(u), "行吧\n【表情3】")[1]
        await dm(e, "你好")
        await e.engine.idle()
        assert seen[0].startswith("找到这几张：\n1. 描述1\n") and "4. 描述4" in seen[0]
        assert [x[2] for x in bot.sent] == ["行吧"] and len(bot.images) == 1
        assert logs(e, "（表情包，心情「无语」，4 张里挑了第 3 张）：描述3") and logs(e, "[私聊 ")
        # 越界：只发文字，记一条
        e.llm.main = lambda s, u: "好的【表情8】"
        await dm(e, "再来")
        await e.engine.idle()
        assert bot.sent[-1][2] == "好的" and len(bot.images) == 1
        assert logs(e, "看了 4 张表情包候选（心情「无语」），没挑，只发文字")
        # 私聊里只有标记（没有文字）也发表情
        e.llm.main = lambda s, u: "【表情1】"
        await dm(e, "只要图")
        await e.engine.idle()
        assert len(bot.images) == 2
    env.run(body)


def test_tool_spec_and_hint_use_the_agreed_wording():
    from mb.engine import _tool_hint
    from mb.stickers import tool_spec
    assert tool_spec()["description"] == "按心情找几张这个群里常用的表情包给你挑，选中的跟在你的文字后面发出去。只在气氛合适、一张图比一句话更到位时才用，别每句都发。"
    t = [{"name": "send_sticker"}]
    assert "- send_sticker：按心情找几张这个群里常用的表情包给你挑，选中的跟在你的文字后面发。气氛合适、一张图比一句话更到位时才用。" in _tool_hint(t)
    assert "- send_sticker：按心情找几张你收藏的表情包给你挑，选中的跟在你的文字后面发。气氛合适、一张图比一句话更到位时才用。" in _tool_hint(t, dm=True)


# ---------- 四、微信发完后的日志 ----------
def test_wechat_unconfirmed_send_logs_one_plain_line_and_keeps_the_diagnosis_for_debug():
    from test_round41 import entry_for
    w = World([(f"s{i}", False) for i in range(5)], sends=False)
    w.msgs.append(SimpleNamespace(id="m0", type="text", attr="friend", content="之前的话"))
    info, debug = [], []
    drv = wxsticker.WxStickers(w.ui(), lambda: 11, grab=w.grab, sleep=lambda s: None, log=info.append, debug=debug.append)
    r = drv.send(w.wx(), entry_for(w, 2), "目标群", "")
    assert r["clicked"] and r["confirmed"] is False
    assert "表情已点击发送（微信不列出自己发的表情，发后没法再核对）" in info
    assert not any("没看到多出自己发的表情" in x or "没确认发出" in x for x in info)
    assert any("没确认发出：点之前最后几条" in x and "点之后最后几条" in x for x in debug)
    # 没给 debug 时跟 log 一样（验证命令、旧用法）
    both = []
    w.driver(log=both.append).send(w.wx(), entry_for(w, 2), "目标群", "")
    assert any("没确认发出" in x for x in both)


def test_wechat_channel_sends_the_diagnosis_at_debug_level(env):
    async def body(e):
        from mb.channels.wechat import WeChatChannel
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        ch = WeChatChannel(e.engine, acc)
        w = World([(f"s{i}", False) for i in range(3)], sends=False)
        ch.stk_ui, ch.stk_hwnd, ch.stk_grab, ch.stk_sleep = w.ui(), (lambda: 11), w.grab, (lambda s: None)
        drv = ch._stk()
        drv.log("表情已点击发送（微信不列出自己发的表情，发后没法再核对）")
        drv.debug("没确认发出：诊断")
        lv = {r["text"]: r["level"] for r in e.core.db.rows("SELECT text, level FROM logs")}
        assert lv["微信：表情已点击发送（微信不列出自己发的表情，发后没法再核对）"] == "info" and lv["微信：没确认发出：诊断"] == "debug"
    env.run(body)


def test_the_confirmation_result_is_only_used_for_the_log():
    """send_image 不看 confirmed：只看 found；发言次数、连着发的条数、用量统计都不读它。"""
    import inspect

    from mb.channels import wechat
    src = inspect.getsource(wechat.WeChatChannel.send_image)
    assert "confirmed" not in src and 'res["found"]' in src
