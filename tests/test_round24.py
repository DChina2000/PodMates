"""管理指令不排队：收到就立刻执行（不受「两次回复最小间隔」限制），确认回复按执行后的角色署名；
队列里压着别的 @ 时先换角色、再由新角色回；合并 @ 时指令不算一次被叫到；闭嘴让还没发出的回复作废。"""
import asyncio
import time

from mb import customcmd
from mb.channels.base import Msg

HAITUN = "90001"  # 测试里的管理员（人物表里「她妈」）


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def fire(e, uid, text, chat="100", at=("900",)):
    """交给引擎但不等队列处理完（要看到「排队」和「立刻执行」）。"""
    e.seq = getattr(e, "seq", 0) + 1
    msg = Msg("qq", "ac_900", "group", chat, uid, f"群友{uid}", text, msg_id=f"ac_900-m{e.seq}", chat_name="测试群",
              at_ids=list(at), dedup=f"{chat}|{uid}|{e.seq}", sender_role="member")
    return e.engine.on_message(msg)


def mains(e):
    return [c for c in e.llm.calls if c["_kind"] == "main"]


async def settle(e, secs=0.3):
    await asyncio.sleep(secs)


def test_role_switch_inside_the_gap_takes_effect_at_once_and_the_queued_at_is_answered_by_the_new_role(env):
    async def body(e):
        bot = e.add_bot("900")
        e.add_role("r_乙", "乙角色")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 3)
        e.llm.main = lambda s, u: "回复"
        await e.say("111", "小棉袄", at=["900"])  # 第一次回复：之后进入 3 秒的间隔
        assert len(bot.sent) == 1
        await fire(e, "111", "再叫一声")  # 间隔内的 @：排队
        await settle(e)
        assert len(bot.sent) == 1
        t0 = time.time()
        await fire(e, HAITUN, "换乙角色")  # 管理指令：不排队，立刻生效
        await settle(e)
        assert len(bot.sent) == 2 and bot.sent[-1][2].startswith("好，换成「乙角色」") and time.time() - t0 < 1.5  # 间隔还没到，确认已经发出
        room = e.engine.rooms["qq:100"]
        confirm = [x for x in room.ctx if x.bot and "换成「乙角色」" in x.text][-1]
        assert "〔乙角色本人〕" in confirm.name  # 确认回复按执行后的角色署名
        assert e.engine.duty(e.engine.bots["ac_900"], room).role["name"] == "乙角色"
        n = len(mains(e))
        await e.engine.idle()  # 到点了，队列里那条旧 @ 由新角色来写
        assert len(mains(e)) == n + 1 and "你是乙角色。" in mains(e)[-1]["messages"][0]["content"]
        assert bot.sent[-1][2] == "回复"
        # 换回来：确认回复署回原来的角色，也是立刻
        await fire(e, HAITUN, "换回来")
        await settle(e)
        back = [x for x in room.ctx if x.bot and "按排班换回来了" in x.text][-1]
        assert "〔海豚娘本人〕" in back.name
    env.run(body)


def test_command_does_not_use_up_or_wait_for_the_reply_gap(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 30)
        e.llm.main = lambda s, u: "回复"
        await e.say("111", "问一句", at=["900"])
        room = e.engine.rooms["qq:100"]
        stamp = room.last_reply["ac_900"]
        n = len(bot.sent)
        t0 = time.time()
        for cmd in ("状态", "插话力度 2", "插话开", "插话关"):
            await e.say(HAITUN, cmd, at=["900"])
        assert len(bot.sent) == n + 4 and time.time() - t0 < 5  # 全都立刻回了，没等 30 秒
        assert room.last_reply["ac_900"] == stamp  # 指令的回复不更新「两次回复」计时
        # 普通 @ 仍然受间隔管，而且是从上一次真正的回复算起
        await fire(e, "111", "再问一句")
        await settle(e)
        assert len(bot.sent) == n + 4
    env.run(body)


def test_commands_are_not_counted_as_being_called_when_merging(env):
    async def body(e):
        bot = e.add_bot("900")
        e.add_role("r_乙", "乙角色")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 2)
        e.llm.main = lambda s, u: "合并回复"
        await e.say("111", "第一问", at=["900"])
        n_main, n_sent = len(mains(e)), len(bot.sent)
        await fire(e, "111", "插队一")
        await fire(e, HAITUN, "换乙角色")
        await fire(e, "112", "插队二")
        await e.engine.idle()
        # 两条普通 @ 合成一次回复（只调一次模型），由新角色写；指令那条没有再让旧角色回一条
        assert len(mains(e)) == n_main + 1 and "你是乙角色。" in mains(e)[-1]["messages"][0]["content"]
        assert len(bot.sent) == n_sent + 2  # 一条换角色的确认 + 一条合并回复
        assert bot.sent[-2][2].startswith("好，换成「乙角色」") and bot.sent[-1][2] == "合并回复"
    env.run(body)


def test_mute_executes_at_once_and_voids_replies_that_are_not_sent_yet(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 3)
        e.llm.main = lambda s, u: "回复"
        await e.say("111", "第一问", at=["900"])
        assert len(bot.sent) == 1
        await fire(e, "111", "排队一")
        await fire(e, "112", "排队二")
        await settle(e)
        await fire(e, HAITUN, "闭嘴")  # 立刻生效
        await settle(e)
        assert bot.sent[-1][2].startswith("好，闭嘴") and len(bot.sent) == 2
        n_main = len(mains(e))
        await e.engine.idle()
        assert len(bot.sent) == 2 and len(mains(e)) == n_main  # 队列里压着的 @ 都作废了：不发，也不白调模型
        # 恢复之后又能说话
        await e.say(HAITUN, "恢复", at=["900"])
        await e.say("111", "还在吗", at=["900"])
        assert bot.sent[-1][2] == "回复"
    env.run(body)


def test_mute_during_generation_drops_the_reply(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        real = e.engine.generate

        async def slow(*a, **k):
            await asyncio.sleep(0.6)
            return await real(*a, **k)
        e.engine.generate = slow
        e.llm.main = lambda s, u: "回复"
        await fire(e, "111", "快回我")
        await settle(e, 0.15)  # 她正在写
        await fire(e, HAITUN, "闭嘴")
        await e.engine.idle()
        assert [m[2] for m in bot.sent] and all(not m[2] == "回复" for m in bot.sent)  # 只有闭嘴的确认，写好的回复没发出去
        assert any("作废" in r["text"] for r in e.core.db.rows("SELECT text FROM logs"))
    env.run(body)


def test_custom_commands_also_run_at_once(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 30)
        e.core.db.set_kv("custom_commands", [customcmd.clean({"id": "c1", "name": "签到", "match": "equal", "pattern": "签到", "action": "reply",
                                                              "text": "签到成功", "at": True, "perm": {"mode": "all"}})])
        e.llm.main = lambda s, u: "回复"
        await e.say("111", "先说一句", at=["900"])
        n = len(bot.sent)
        t0 = time.time()
        await e.say("112", "签到", at=["900"])
        assert bot.sent[-1][2] == "签到成功" and len(bot.sent) == n + 1 and time.time() - t0 < 5  # 间隔内照样立刻回
    env.run(body)


def test_non_admin_command_is_refused_without_waiting_and_unknown_switch_is_plain_chat(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "reply_min_gap", 3)
        e.llm.main = lambda s, u: "不行" if "没有这个权限" in u else "普通回复"
        await e.say("111", "问一句", at=["900"])
        n = len(bot.sent)
        t0 = time.time()
        await e.say("333", "闭嘴", at=["900"])  # 没权限的人：她用自己的方式拒绝（这是一次生成的回复，仍受间隔管）
        assert "mute" not in str(e.engine.rooms["qq:100"].mute) and not e.engine.rooms["qq:100"].mute
        # 「换××」里的××既不是角色也不是人设：当普通聊天，而不是指令
        await e.say(HAITUN, "换成不存在的人", at=["900"])
        assert not e.engine.rooms["qq:100"].mute
    env.run(body)
