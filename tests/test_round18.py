"""两次回复最小间隔（reply_min_gap）：同一个群里不到这么多秒不发下一次回复；必回的排队、积压的合并；主动插话直接不插；
别的群、私聊（默认）不受限；0 = 不限；同一次回复拆出来的几行照常连发。"""
import time

from mb.channels.base import Msg


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def fire(e, uid, text, chat="100", at=("900",), private=False):
    """像 e.say 那样把消息交给引擎，但不等队列处理完（测试要看到「排队」）。"""
    e.seq = getattr(e, "seq", 0) + 1
    msg = Msg("qq", "ac_900", "private" if private else "group", uid if private else chat, uid, f"群友{uid}", text, msg_id=f"ac_900-m{e.seq}",
              chat_name="测试群", at_ids=list(at), dedup=f"{chat}|{uid}|{e.seq}")
    return e.engine.on_message(msg)


def replies(bot):
    return [m[2] for m in bot.sent]


def test_second_at_in_the_same_group_waits_for_the_gap(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 2)
        e.llm.main = lambda s, u: "回复:" + u[-12:]
        await e.say("111", "第一问", at=["900"])
        t1 = e.engine.rooms["qq:100"].last_reply["ac_900"]
        assert len(bot.sent) == 1
        await fire(e, "111", "第二问", chat="100")
        await __import__("asyncio").sleep(0.3)
        assert len(bot.sent) == 1  # 间隔内不回，也没丢：在队列里等着
        await e.engine.idle()
        assert len(bot.sent) == 2 and "第二问" in bot.sent[1][2]
        assert time.time() - t1 >= 1.9  # 到点才回
    env.run(body)


def test_queued_at_messages_are_merged_into_one_reply(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 2)
        e.llm.main = lambda s, u: "合并回复"
        await e.say("111", "第一问", at=["900"])
        n_main = len(mains(e))
        await fire(e, "111", "插队一", chat="100")
        await fire(e, "112", "插队二", chat="100")
        await fire(e, "111", "插队三", chat="100")
        await e.engine.idle()
        assert len(bot.sent) == 2 and len(mains(e)) == n_main + 1  # 三条积压合成一次回复（只调一次模型）
    env.run(body)


def mains(e):
    return [c for c in e.llm.calls if c["_kind"] == "main"]


def test_other_groups_and_zero_are_not_limited(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 30)
        e.llm.main = lambda s, u: "好"
        t0 = time.time()
        await e.say("111", "A群问", chat="100", at=["900"])
        await e.say("111", "B群问", chat="200", at=["900"])  # 另一个群：各自计时，不受 A 群影响
        assert len(bot.sent) == 2 and time.time() - t0 < 5
        # 0 = 不限
        setg(e, "reply_min_gap", 0)
        await e.say("111", "A群又问", chat="100", at=["900"])
        assert len(bot.sent) == 3 and time.time() - t0 < 5
        # 群里单独设也行（比如全局 30，这个群设 0）
        setg(e, "reply_min_gap", 30)
        setg(e, "reply_min_gap", 0, "chat:qq:100")
        await e.say("111", "A群再问", chat="100", at=["900"])
        assert len(bot.sent) == 4 and time.time() - t0 < 5
    env.run(body)


def test_proactive_replies_are_skipped_inside_the_gap_without_asking_the_gatekeeper(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", True)
        setg(e, "cooldown_seconds", 0)
        setg(e, "prefilter_on", False)
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        e.llm.main = lambda s, u: "插一句"
        setg(e, "reply_min_gap", 0)
        await e.say("111", "随便聊聊天", chat="100")
        assert len(bot.sent) == 1
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"]
        setg(e, "reply_min_gap", 30)
        await e.say("112", "再聊一句别的话题", chat="100")
        assert len(bot.sent) == 1 and len([c for c in e.llm.calls if c["_kind"] == "gk"]) == len(gk)  # 间隔内不插，连守门员都没问
        assert e.engine.pro_state(e.engine.bots["ac_900"], e.engine.rooms["qq:100"]) == (True, False)
        await e.say("112", "另一个群聊聊", chat="200")  # 别的群：照常
        assert len(bot.sent) == 2
    env.run(body)


def test_private_chat_is_not_limited_by_default_but_can_be(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.main = lambda s, u: "私聊回复"
        setg(e, "reply_min_gap", 30)
        t0 = time.time()
        await e.say("111", "私聊一", private=True)
        await e.say("111", "私聊二", private=True)
        assert len(bot.sent) == 2 and time.time() - t0 < 5  # 默认私聊不限
        setg(e, "private_reply_gap", 2, "role:r_haitun")  # 私聊那一层单独设
        await e.say("111", "私聊三", private=True)
        t1 = time.time()
        await e.say("111", "私聊四", private=True)
        assert len(bot.sent) == 4 and time.time() - t1 >= 1.9
    env.run(body)


def test_split_lines_of_one_reply_are_still_sent_back_to_back(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 30)
        setg(e, "split_interval", 0.05)
        setg(e, "typing_speed", 1000)
        e.llm.main = lambda s, u: "第一行\n第二行\n第三行"
        t0 = time.time()
        await e.say("111", "问", at=["900"])
        assert replies(bot) == ["第一行", "第二行", "第三行"] and time.time() - t0 < 5
    env.run(body)


def test_new_setting_defaults(env):
    from mb.settings import BY_KEY
    s = BY_KEY["reply_min_gap"]
    assert s["default"] == 10 and s["cat"] == "节奏" and set(s["scopes"]) == {"G", "P", "R", "RP", "C", "RC"}
    assert s["pdef"]["demo"] == 0  # 体验模式不隔着等
    p = BY_KEY["private_reply_gap"]
    assert p["default"] == 0
