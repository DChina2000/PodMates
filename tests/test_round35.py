"""回复节奏：每小时回复名额（含排队、用户本人豁免、重启后接着算）、中间没人说话时最多连着发几条、主动插话的引用、她写的「@名字」发成真正的 @。全部用桩，不往任何群发消息。"""
import asyncio
import time

import pytest

from conftest import FakeChannel
from mb import engine as engine_mod
from mb.engine import Entry, Engine
from mb.channels.onebot import QQChannel

HAITUN = "90001"
SIX = "第一行\n第二行\n第三行\n第四行\n第五行\n第六行"


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


async def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def view(e, uid="900", chat="100"):
    eng = e.engine
    room = eng.room("qq", chat, "测试群")
    return eng.duty(eng.bots[f"ac_{uid}"], room), room


def put_user(e, **kw):
    card = e.core.db.doc("person", "pe_user")
    e.core.db.put("person", {**card, "name": "用户", "self": True, **kw})
    e.engine.reload()


class MentionChannel(FakeChannel):
    """认 mention 的模拟渠道（像 QQ 和微信）：把每一条发出去的 mention 记下来。"""
    supports_mention = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.mentions = []

    async def send(self, chat_type, chat_id, text, reply_to=None, at=None, mention=None):
        self.mentions.append(mention)
        return await super().send(chat_type, chat_id, text, reply_to, at)


def add_mention_bot(e, uid="900"):
    acc = e.core.db.put("account", {"id": f"ac_{uid}", "platform": "qq", "role_id": "r_haitun", "qq": uid})
    ch = MentionChannel(e.engine, acc, uid, f"bot{uid}")
    e.engine.add_bot(acc, ch)
    return ch


def logs(e, needle):
    return [r["text"] for r in e.core.db.rows("SELECT text FROM logs ORDER BY id") if needle in r["text"]]


# ---------- 一、每小时回复名额 ----------
def test_quota_is_off_by_default_and_behaves_as_before(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        assert e.core.settings.get("reply_hourly") == 0 and e.core.settings.get("reply_hourly_burst") == 0 and e.core.settings.get("reply_user_free") is True
        for i in range(8):
            await e.say("111", f"问{i}", at=["900"])
        assert len(bot.sent) == 8 and not e.engine.rooms["qq:100"].q_wait and "回复名额用完" not in "".join(r["text"] for r in e.core.db.rows("SELECT text FROM logs"))
    env.run(body)


def test_burst_then_one_every_sixty_over_n_minutes_and_calls_made_meanwhile_merge_into_one_reply(env, monkeypatch):
    async def body(e):
        monkeypatch.setattr(engine_mod, "QUOTA_POLL", 0.02)
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_hourly", 2)  # 每小时 2 次、最多攒 2 次：一口气 2 个，之后每 30 分钟补回一个
        for i in range(3):
            await e.say("111", f"问{i}", at=["900"])
        assert len(bot.sent) == 2  # 一口气最多 M=2 次
        eng = e.engine
        b, room = view(e)
        assert len(room.q_wait["ac_900"]["items"]) == 1 and eng.quota_wait(b, room) > 1700
        assert logs(e, "回复名额用完，约 30 分钟后回（已排队）")
        await e.say("111", "又问", at=["900"])  # 排队期间被叫了好几次
        await e.say("222", "再问", at=["900"])
        assert len(bot.sent) == 2 and len(room.q_wait["ac_900"]["items"]) == 3
        # 过去 31 分钟：补回一个，排队的三条合成一条回复
        room.quota["ac_900"][1] -= 1900
        assert await wait_for(lambda: len(bot.sent) == 3)
        await eng.idle()
        assert len(bot.sent) == 3 and "ac_900" not in room.q_wait
        main = [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        assert "这几个人先后@了你" in main and "问2" in main and "又问" in main and "再问" in main
        assert eng.quota_wait(b, room) > 0  # 又用掉了
    env.run(body)


def test_never_more_than_n_plus_m_in_any_sixty_minutes_and_refills_at_the_set_rate(env):
    async def body(e):
        e.add_bot("900")
        setg(e, "reply_hourly", 20)  # 每小时 20 次、最多攒 5 次：一口气 5 次，之后每 3 分钟补回一个
        setg(e, "reply_hourly_burst", 5)
        eng = e.engine
        b, room = view(e)
        t0 = 1_000_000.0
        assert eng.quota_cfg(b, room) == (20.0, 5.0)
        assert eng.quota_tokens(b, room, t0) == 5.0  # 刚开始手里有 5 个
        room.quota[b.aid] = [0.0, t0]
        assert abs(eng.quota_wait(b, room, t0) - 180) < 1e-6  # 3 分钟补回一个
        assert eng.quota_wait(b, room, t0 + 180) == 0
        assert eng.quota_tokens(b, room, t0 + 3600 * 5) == 5.0  # 攒满为止
        # 模拟一小时：能回就回，数 60 分钟里一共回了几次
        used, t, room.quota[b.aid] = 0, t0, [5.0, t0]
        while t < t0 + 3600:
            if eng.quota_wait(b, room, t) == 0:
                tok = eng.quota_tokens(b, room, t)
                room.quota[b.aid] = [tok - 1, t]
                used += 1
            t += 7
        assert used <= 20 + 5 and used >= 20  # 任意 60 分钟里不超过 N + M，也确实按速度补回来
        # burst 为 0 = 和每小时次数一样
        setg(e, "reply_hourly_burst", 0)
        assert eng.quota_cfg(b, room) == (20.0, 20.0)
    env.run(body)


def test_proactive_is_not_sent_when_the_quota_is_used_up_and_the_gatekeeper_is_not_even_asked(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "eagerness", 5)
        setg(e, "cooldown_seconds", 0)
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        e.llm.main = lambda s, u: "插一句"
        await e.say("111", "随便聊聊")
        assert len(bot.sent) == 1 and "gk" in [c["_kind"] for c in e.llm.calls]
        setg(e, "reply_hourly", 1)
        eng = e.engine
        b, room = view(e)
        room.quota[b.aid] = [0.0, time.time()]
        calls = len(e.llm.calls)
        await e.say("111", "又聊聊")
        assert len(bot.sent) == 1 and len(e.llm.calls) == calls  # 没问守门员，也没调模型
        assert eng.proactive_ok(b, room, sample=False) is False
        room.quota[b.aid] = [1.0, time.time()]  # 有名额了：照常插话，并用掉一个
        await e.say("111", "再聊聊")
        assert len(bot.sent) == 2 and eng.quota_tokens(b, room) < 1
    env.run(body)


def test_exempt_modes_do_not_use_or_wait_for_the_quota(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "reply_hourly", 1)
        eng = e.engine
        b, room = view(e)
        room.quota[b.aid] = [0.0, time.time()]
        trig = Entry("111", "群友", "来一句")
        for mode in ("force", "remind", "schedule", "command", "refuse", "custom", "cap"):
            before = len(bot.sent)
            await eng.speak(b, room, mode, trig, brief="x") if mode in ("force", "remind", "refuse", "custom") else await eng.deliver(b, room, "固定的话", None, mode)
            assert len(bot.sent) > before, mode
        assert eng.quota_tokens(b, room) < 1 and room.quota[b.aid][0] == 0.0  # 一个名额都没动
        assert not room.q_wait
    env.run(body)


def test_the_user_calling_her_uses_no_quota_and_is_not_queued_but_her_proactive_after_the_user_speaks_counts(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        put_user(e, qq=["777"])
        setg(e, "reply_hourly", 1)
        eng = e.engine
        b, room = view(e)
        await e.say("111", "问一", at=["900"])
        await e.say("111", "问二", at=["900"])  # 名额只有 1 个：第二条排队
        assert len(bot.sent) == 1 and room.q_wait.get("ac_900")
        tokens = room.quota[b.aid][0]
        await e.say("777", "我是用户本人，叫你", at=["900"])  # 用户本人：不用名额，也不排队
        assert len(bot.sent) == 2 and room.quota[b.aid][0] == tokens
        # 关掉「用户本人叫她不算进上限」：他也要排队
        setg(e, "reply_user_free", False)
        await e.say("777", "再叫你", at=["900"])
        assert len(bot.sent) == 2
        # 戳一戳也一样（用户本人戳她不算）
        setg(e, "reply_user_free", True)
        from mb.channels.base import Msg
        await e.engine.on_message(Msg("qq", "ac_900", "group", "100", "777", "用户", "", poke_target="900", dedup="poke-user-1"))
        await e.engine.idle()
        assert len(bot.sent) == 3
    env.run(body)


def test_quota_state_survives_a_restart(env):
    async def body(e):
        e.add_bot("900")
        setg(e, "reply_hourly", 4)
        eng = e.engine
        b, room = view(e)
        eng.quota_use(b, room)
        eng.quota_use(b, room)
        eng.quota_use(b, room)  # 4 个用掉 3 个
        assert 0.9 < eng.quota_tokens(b, room) < 1.1
        eng2 = Engine(e.core.db, e.core.settings, e.core.llm, emit=e.core.emit)
        eng2.pace = 0
        acc = e.core.db.doc("account", "ac_900")
        eng2.add_bot(acc, FakeChannel(eng2, acc, "900", "bot900"))
        r2 = eng2.room("qq", "100", "测试群")
        b2 = eng2.duty(eng2.bots["ac_900"], r2)
        assert 0.9 < eng2.quota_tokens(b2, r2) < 1.1  # 重启后接着算，不是又攒满
        eng2.close()
    env.run(body)


def test_a_queued_reply_is_dropped_if_she_was_told_to_shut_up_meanwhile(env, monkeypatch):
    async def body(e):
        monkeypatch.setattr(engine_mod, "QUOTA_POLL", 0.02)
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_hourly", 1)
        await e.say("111", "问一", at=["900"])
        await e.say("111", "问二", at=["900"])
        b, room = view(e)
        assert room.q_wait.get("ac_900") and len(bot.sent) == 1
        room.mute["ac_900"] = time.time() + 600  # 等的期间被叫停
        room.quota["ac_900"][1] -= 4000
        assert await wait_for(lambda: "ac_900" not in room.q_wait)
        await e.engine.idle()
        assert len(bot.sent) == 1  # 照现有逻辑作废
    env.run(body)


# ---------- 二、中间没人说话时最多连着发几条 ----------
def test_default_is_four_a_six_line_reply_becomes_four_bubbles_and_zero_means_no_limit(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        assert e.core.settings.get("split_max_parts") == 4
        e.llm.main = lambda s, u: SIX
        await e.say("111", "说点什么", at=["900"])
        assert len(bot.sent) == 4 and "\n" in bot.sent[-1][2] or len(bot.sent) == 4  # 6 行并成 4 条
        assert "".join(x[2] for x in bot.sent).replace("\n", "") == SIX.replace("\n", "")
        setg(e, "split_max_parts", 0)
        bot.sent.clear()
        await e.say("111", "再说点", at=["900"])
        assert len(bot.sent) == 6  # 0 = 不限
    env.run(body)


def test_after_three_bubbles_with_nobody_else_talking_the_next_proactive_reply_gets_one_bubble_and_then_none(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        eng = e.engine
        b, room = view(e)
        for i in range(3):
            await eng.deliver(b, room, f"自己说的第{i}条", None, "manual")  # 人工发言也算她发出的气泡
        assert room.run["ac_900"] == 3 and eng.run_left(b, room) == 1
        e.llm.main = lambda s, u: SIX
        trig = Entry("111", "群友", "话题")
        n = len(bot.sent)
        assert await eng.speak(b, room, "pro", trig, brief="接")
        assert len(bot.sent) == n + 1 and bot.sent[-1][2].count("\n") == 5  # 剩 1 条：6 行并成 1 条
        calls = len(e.llm.calls)
        assert room.run["ac_900"] == 4 and eng.run_left(b, room) == 0
        assert await eng.speak(b, room, "pro", trig, brief="接") is False and len(e.llm.calls) == calls  # 发满 4 条：主动插话不发，也不调模型
        assert await eng.speak(b, room, "cold", trig) is False
        setg(e, "proactive", True)
        assert eng.pro_state(b, room) == (True, False) and not eng.proactive_ok(b, room, sample=False)
        setg(e, "proactive", False)
        # 别人说话：从 0 重新数
        await e.say("111", "有人说话了")
        assert room.run == {} and eng.run_left(b, room) == 4
        assert await eng.speak(b, room, "pro", trig, brief="接")
    env.run(body)


def test_being_at_replied_to_and_pokes_restart_the_count_and_the_poke_switch_decides(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "poke_reply", False)
        eng = e.engine
        b, room = view(e)
        for i in range(4):
            await eng.deliver(b, room, f"x{i}", None, "manual")
        assert eng.run_left(b, room) == 0
        from mb.channels.base import Msg
        poke = Msg("qq", "ac_900", "group", "100", "111", "群友", "", poke_target="900", dedup="p1")
        setg(e, "poke_counts", False)
        await eng.on_message(poke)
        assert room.run["ac_900"] == 4  # 开关关着：戳一戳不算有人说话
        setg(e, "poke_counts", True)
        await eng.on_message(Msg("qq", "ac_900", "group", "100", "111", "群友", "", poke_target="900", dedup="p2"))
        assert room.run == {}
        for i in range(4):
            await eng.deliver(b, room, f"y{i}", None, "manual")
        e.llm.main = lambda s, u: SIX
        await e.say("111", "被@了", at=["900"])  # 被@也是别人说话：回复不受之前那 4 条影响
        assert len(bot.sent) == 8 + 4
    env.run(body)


def test_a_sticker_counts_as_one_bubble_and_the_run_is_per_account(env):
    async def body(e):
        a = e.add_bot("900")
        bb = e.add_bot("901")
        setg(e, "proactive", False)
        eng = e.engine
        ba, room = view(e, "900")
        bb_view, _ = view(e, "901")
        for i in range(3):
            await eng.deliver(ba, room, f"a{i}", None, "manual")
        assert room.run == {"ac_900": 3}
        await eng.deliver(bb_view, room, "b0", None, "manual")  # 我们的另一个账号开口：对 900 来说是有别人说话
        assert room.run == {"ac_901": 1}
        # 表情包算一条
        from test_round5b import png
        from mb.stickers import Stickers
        sp = eng.stickers.space(room)
        data = png(5)
        path = Stickers.folder() / "t.png"
        path.write_bytes(data)
        mid = eng.memory.add(sp, "sticker", "0", "表情包", "无语", "inferred", None, room.key, status="active")
        sid = e.core.db.x("INSERT INTO stickers (space, room, phash, seen, users, status, file, size, uses, descr, mem_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (sp, room.key, "0", 3, "[]", "learned", "t.png", len(data), 0, "无语", mid)).lastrowid
        row = e.core.db.rows("SELECT * FROM stickers WHERE id=?", (sid,))[0]
        assert await eng.deliver_sticker(ba, room, row, None, "pro")
        assert room.run == {"ac_900": 1}
        for _ in range(3):
            assert await eng.deliver_sticker(ba, room, row, None, "pro")
        assert not await eng.deliver_sticker(ba, room, row, None, "pro")  # 满 4 条了：表情包也不发
        assert len(a.images) == 4
        assert await eng.deliver_sticker(ba, room, row, None, "force")  # 不受这个上限拦的几类照常发
    env.run(body)


def test_forced_and_other_exempt_sends_are_not_blocked_but_still_counted(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        eng = e.engine
        b, room = view(e)
        for i in range(4):
            await eng.deliver(b, room, f"x{i}", None, "manual")
        e.llm.main = lambda s, u: SIX
        assert await eng.speak(b, room, "force", Entry("111", "群友", "让她回"))
        assert room.run["ac_900"] > 4  # 照常发，气泡照样计入
        assert await eng.deliver(b, room, "提醒", None, "schedule")
        # 被@时哪怕条数用完了也回一条
        room.run["ac_900"] = 4
        n = len(bot.sent)
        assert await eng.speak(b, room, "at", Entry("111", "群友", "@你"), how="@了你")
        assert len(bot.sent) == n + 1
    env.run(body)


def test_a_platform_that_does_not_split_counts_one_bubble_per_reply(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "split_enabled", False)
        e.llm.main = lambda s, u: SIX
        await e.say("111", "说点什么", at=["900"])
        assert len(bot.sent) == 1 and e.engine.rooms["qq:100"].run["ac_900"] == 1
    env.run(body)


# ---------- 三、主动插话的引用 ----------
def test_proactive_quotes_the_message_it_replies_to_when_others_spoke_in_between(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "split_max_parts", 0)  # 这个测试连发好几次，不让连发条数上限插手
        eng = e.engine
        for t in ("话题A", "别的话B", "别的话C"):
            await e.say("111", t)
        b, room = view(e)
        first, last = room.ctx[0], room.ctx[-1]
        e.llm.main = lambda s, u: "接话"
        assert await eng.speak(b, room, "pro", first, brief="接A")
        assert bot.sent[-1][3] == first.ids["ac_900"]  # 后面有别人的消息：引用她要回的那条
        assert await eng.speak(b, room, "pro", last, brief="接C")
        assert bot.sent[-1][3] is None  # 那条就是最新的：不引用
        setg(e, "pro_quote_min", 3)  # 门槛调到 3 条
        await eng.speak(b, room, "pro", first, brief="接A")
        assert bot.sent[-1][3] is None
        setg(e, "pro_quote_min", 2)
        await eng.speak(b, room, "pro", first, brief="接A")
        assert bot.sent[-1][3] == first.ids["ac_900"]
        setg(e, "pro_quote", False)  # 开关关掉：不引用
        await eng.speak(b, room, "pro", first, brief="接A")
        assert bot.sent[-1][3] is None
        # 被@的引用照旧由「回@时引用对方那条消息」管
        setg(e, "pro_quote", True)
        await e.say("111", "@你", at=["900"])
        assert bot.sent[-1][3] is not None
        setg(e, "quote_reply", False)
        await e.say("111", "@你 再来", at=["900"])
        assert bot.sent[-1][3] is None
    env.run(body)


# ---------- 四、她写的「@名字」发成真正的 @ ----------
def test_names_in_at_form_that_match_group_members_become_real_mentions_and_the_rest_stay_text(env):
    async def body(e):
        bot = add_mention_bot(e)
        setg(e, "proactive", False)
        await e.say("111", "我是甲", name="ExampleUser")
        await e.say("112", "我是乙", name="小明")
        await e.say("113", "我也是同名", name="小明")  # 同名对着两个人：不转
        eng = e.engine
        b, room = view(e)
        cands = eng.mention_candidates(room, b)
        assert cands == {"ExampleUser": "111"}
        e.llm.main = lambda s, u: "@ExampleUser 这就答\n@没这个人 你好\n@小明 在吗\n@ExampleUser这就答没有分隔"
        await e.say("114", "问", at=["900"])
        sent = [x[2] for x in bot.sent]
        assert sent == ["@ExampleUser 这就答", "@没这个人 你好", "@小明 在吗", "@ExampleUser这就答没有分隔"]  # 文字原样交给渠道，由渠道把对上的那段换成 @ 段
        assert bot.mentions == [[("111", "ExampleUser")], None, None, None]
        # 开关关掉：不转
        setg(e, "at_convert", False)
        bot.mentions.clear()
        await e.say("114", "再问", at=["900"])
        assert all(m is None for m in bot.mentions)
        # 面板手动发言不转
        setg(e, "at_convert", True)
        bot.mentions.clear()
        await eng.deliver(b, room, "@ExampleUser 人工", None, "manual")
        assert bot.mentions == [None]
    env.run(body)


def test_person_card_nicknames_and_our_own_accounts(env):
    async def body(e):
        add_mention_bot(e)
        setg(e, "proactive", False)
        p = e.core.db.doc("person", "pe_haitun")
        e.core.db.put("person", {**p, "qq": ["222"], "nicks": {"qq": "小海豚"}})
        e.engine.reload()
        await e.say("222", "我在", name="某个很长的群名片")
        await e.say("90001", "我在", name="海豚妈妈")  # 另一个人，和人物卡无关
        eng = e.engine
        b, room = view(e)
        cands = eng.mention_candidates(room, b)
        assert cands.get("小海豚") == "222" and "bot900" not in cands and "900" not in cands.values()
        assert eng.mention_plan("你好 @小海豚 来了，@小海豚。", cands) == [("222", "小海豚"), ("222", "小海豚")]
        assert eng.mention_plan("@小海豚们 不算", cands) == []
    env.run(body)


def test_qq_channel_turns_the_matched_text_into_an_at_segment_without_repeating_it():
    segs = QQChannel.with_mentions("@ExampleUser 这就答", [("111", "ExampleUser")], False)
    assert segs == [{"type": "at", "data": {"qq": "111"}}, {"type": "text", "data": {"text": " 这就答"}}]
    segs = QQChannel.with_mentions("你好@A，早 @B", [("1", "A"), ("2", "B")], False)
    assert [s["type"] for s in segs] == ["text", "at", "text", "text", "at", "text"] or [s["type"] for s in segs] == ["text", "at", "text", "text", "at"]
    assert not any("@" in s["data"].get("text", "") for s in segs) and [s["data"]["qq"] for s in segs if s["type"] == "at"] == ["1", "2"]
    assert QQChannel.with_mentions("没有这个人", [("1", "A")], False) == [{"type": "text", "data": {"text": "没有这个人"}}]


def test_qq_send_puts_the_reply_the_at_segments_and_the_text_in_one_message(env):
    async def body(e):
        acc = e.core.db.put("account", {"id": "ac_q", "platform": "qq", "role_id": "r_haitun", "qq": "900", "manage_napcat": False})
        ch = QQChannel(e.engine, acc)
        sent = []

        async def call(action, **kw):
            sent.append((action, kw))
            return {"message_id": 7}
        ch.call = call
        await ch.send("group", "100", "@ExampleUser 这就答", reply_to="55", mention=[("111", "ExampleUser")])
        action, kw = sent[-1]
        assert action == "send_group_msg" and kw["group_id"] == 100
        assert [s["type"] for s in kw["message"]] == ["reply", "at", "text"] and kw["message"][1]["data"]["qq"] == "111" and kw["message"][2]["data"]["text"] == " 这就答"
    env.run(body)


# ---------- 五、微信：真正的 @ 和引用（wxauto4 的 SendMsg(at=…) 与 Message.quote()，用桩）----------
class WxMsg:
    def __init__(self, mid, log, with_quote=True):
        self.id, self.log = mid, log
        if with_quote:
            self.quote = self._quote

    def _quote(self, text, at=None, timeout=3):
        self.log.append(("quote", self.id, text, at))
        return type("R", (), {"is_success": True})()


class WxChat:
    """独立窗口方式的聊天窗口。"""

    def __init__(self, log, msgs):
        self.log, self.msgs = log, msgs

    def GetAllMessage(self):
        return self.msgs

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        self.log.append(("send", msg, at))
        return type("R", (), {"is_success": True})()


class WxMain(WxChat):
    """主窗口方式。切会话会真的切（ChatInfo 跟着变）：发送前「切过去再核对」才有意义；SendMsg 不再拿到 who。"""

    def __init__(self, log, msgs):
        super().__init__(log, msgs)
        self.cur = "原来的会话"

    def ChatInfo(self):
        return {"chat_name": self.cur}

    def ChatWith(self, who, **kw):
        self.log.append(("chatwith", who))
        self.cur = who

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        self.log.append(("send", msg, at, who))
        return type("R", (), {"is_success": True})()


def wx_channel(e, wx, chats=None):
    from mb.channels.wechat import WeChatChannel
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
    ch = WeChatChannel(e.engine, acc)
    ch.wx, ch.status, ch.loop = wx, "online", asyncio.get_running_loop()
    ch.chats = chats or {}
    ch._com = lambda: None
    ch._verify_if_due = lambda: None
    return ch


def test_wechat_real_at_uses_the_library_at_argument_and_drops_the_typed_name(env):
    async def body(e):
        log = []
        ch = wx_channel(e, WxMain(log, []))
        assert ch.supports_mention
        await ch.send("group", "群A", "@老王 来了", mention=[("老王", "老王")])
        # 用微信自己的 @，文字里原来那段去掉；发的时候不再把 who 交给 SendMsg 去搜（发前已经切过去核对过）
        assert ("send", "来了", ["老王"], None) in log and ("chatwith", "群A") in log
        log.clear()
        await ch.send("group", "群A", "没有@任何人")
        assert [x for x in log if x[0] == "send"] == [("send", "没有@任何人", None, None)]
        # 独立窗口方式
        log.clear()
        ch2 = wx_channel(e, WxMain(log, []), {"群B": WxChat(log, [])})
        await ch2.send("group", "群B", "你好 @老王，早", mention=[("老王", "老王")])
        assert log == [("send", "你好 @老王，早", None)]  # 微信的 @ 只能在开头：句中的保持原样文字
        log.clear()
        await ch2.send("group", "群B", "@老王 你好，早", mention=[("老王", "老王")])
        assert log == [("send", "你好，早", ["老王"])]
        # 这个版本的 wxauto 不认 at：退回去，@ 留作文字
        log.clear()

        class OldMain(WxMain):
            def SendMsg(self, msg, who=None, clear=True, exact=False):
                log.append(("send", msg, who))
        ch3 = wx_channel(e, OldMain(log, []))
        await ch3.send("group", "群A", "@老王 来了", mention=[("老王", "老王")])
        assert [x for x in log if x[0] == "send"] == [("send", "@老王 来了", None)]  # 不认 at 的版本：@ 留作文字，who 也不传
    env.run(body)


def test_wechat_quote_finds_the_message_by_id_and_falls_back_to_a_plain_send(env):
    async def body(e):
        log = []
        msgs = [WxMsg("m1", log), WxMsg("m2", log), WxMsg("m3", log, with_quote=False)]
        ch = wx_channel(e, WxMain(log, msgs))
        # 主窗口方式：先切到那个会话，再在里面找
        await ch.send("group", "群A", "回你", reply_to="m2")
        assert ("chatwith", "群A") in log and ("quote", "m2", "回你", None) in log and not any(x[0] == "send" for x in log)
        # 引用 + @ 一起
        log.clear()
        await ch.send("group", "群A", "@老王 回你", reply_to="m1", mention=[("老王", "老王")])
        assert ("quote", "m1", "回你", ["老王"]) in log
        # 那条找不到（滚出可见范围了）：照普通方式发，日志记一笔
        log.clear()
        await ch.send("group", "群A", "回你", reply_to="m9")
        assert [x[0] for x in log if x[0] in ("quote", "send")] == ["send"] and logs(e, "要引用的那条消息在聊天窗口里找不到")
        # 找到了但没有 quote 方法（不是别人发的消息）：也照普通方式发
        log.clear()
        await ch.send("group", "群A", "回你", reply_to="m3")
        assert [x[0] for x in log if x[0] in ("quote", "send")] == ["send"]
        # 引用出错：照普通方式发，不丢这条回复
        log.clear()

        class Boom(WxMsg):
            def _quote(self, text, at=None, timeout=3):
                raise RuntimeError("界面忙")
        ch2 = wx_channel(e, WxMain(log, [Boom("b1", log)]))
        await ch2.send("group", "群A", "回你", reply_to="b1")
        assert [x[0] for x in log if x[0] in ("quote", "send")] == ["send"] and logs(e, "引用回复没成功")
        # 独立窗口方式：在那个聊天窗口里找
        log.clear()
        ch3 = wx_channel(e, WxMain(log, []), {"群B": WxChat(log, [WxMsg("w1", log)])})
        await ch3.send("group", "群B", "回你", reply_to="w1")
        assert ("quote", "w1", "回你", None) in log
    env.run(body)


def test_wechat_incoming_messages_carry_their_wxauto_id_so_they_can_be_quoted(env):
    async def body(e):
        ch = wx_channel(e, WxMain([], []))
        ch.conf = {"群A": {"name": "群A", "type": "group"}}
        got = []

        async def on_message(msg):
            got.append(msg)
        e.engine.on_message = on_message
        m = type("M", (), {"attr": "friend", "type": "text", "content": "@bot 你好", "sender": "老王", "id": "wxid-77"})()
        await asyncio.get_running_loop().run_in_executor(None, ch._handle, m, "群A")
        await wait_for(lambda: got)
        assert got and got[0].msg_id == "wxid-77"
    env.run(body)
