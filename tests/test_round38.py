"""私聊连发计数、微信 @ 只转行首、微信引用按 id 找不到时按「发送人 + 内容」找。全部用桩，不往任何群发消息。"""
import asyncio

from conftest import FakeChannel
from mb.channels.base import Msg
from test_round35 import SIX, WxChat, WxMain, WxMsg, logs, setg, wx_channel

THREE = "第一行\n第二行\n第三行"


def dm(e, uid="111", aid="ac_900"):
    eng = e.engine
    return eng.duty(eng.bots[aid], None), uid


def texts(bot, chat_id="111"):
    return [x[2] for x in bot.sent if x[0] == "private" and x[1] == chat_id]


# ---------- 一、私聊连发计数 ----------
def test_private_replies_share_the_run_count_and_the_last_reply_still_gets_one_bubble(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        b, uid = dm(e)
        setg(e, "split_max_parts", 4)
        await eng.send_text(b, "private", uid, THREE, None, None, mode="at")
        assert len(texts(bot)) == 3 and eng.run_left(b, None, uid) == 1
        await eng.send_text(b, "private", uid, THREE, None, None, mode="at")  # 剩 1 条：3 行并成 1 条
        got = texts(bot)
        assert len(got) == 4 and got[-1] == "第一行\n第二行\n第三行" and eng.run_left(b, None, uid) == 0
        assert await eng.send_text(b, "private", uid, THREE, None, None, mode="at") is not None  # 条数用完了也至少发一条，不丢回复
        assert len(texts(bot)) == 5 and eng.dm_run["ac_900:111"] == 5
    env.run(body)


def test_two_private_messages_in_a_row_answered_separately_second_reply_gets_one_bubble(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        setg(e, "split_max_parts", 4)
        e.llm.main = lambda s, u: THREE
        gate = asyncio.Event()
        orig = eng.complete

        async def slow(*a, **k):  # 两句话都到了，才让模型回
            await gate.wait()
            return await orig(*a, **k)
        eng.complete = slow
        for i in (1, 2):
            await eng.on_message(Msg("qq", "ac_900", "private", "111", "111", "群友", f"第{i}句", msg_id=f"m{i}", dedup=f"dm|111|{i}"))
        gate.set()
        await eng.idle()
        got = texts(bot)
        assert len(got) == 4 and sorted(x.count("\n") for x in got) == [0, 0, 0, 2]  # 第一次 3 条，第二次 3 行并成 1 条
        # 对方再说话：从 0 重新数
        assert eng.dm_run["ac_900:111"] == 4
        e.llm.main = lambda s, u: ""
        await e.say("111", "又说话了", private=True)
        assert "ac_900:111" not in eng.dm_run
    env.run(body)


def test_unlimited_kinds_are_sent_in_full_and_counted_each_person_and_account_counts_alone_and_zero_is_no_limit(env):
    async def body(e):
        bot = e.add_bot("900")
        bot2 = e.add_bot("901")
        eng = e.engine
        b, _ = dm(e)
        b2, _ = dm(e, aid="ac_901")
        setg(e, "split_max_parts", 4)
        await eng.send_text(b, "private", "111", THREE, None, None)  # 提醒 / 定时 / 手动 / 回执：不受限
        await eng.send_text(b, "private", "111", SIX, None, None, mode="")
        assert eng.dm_run["ac_900:111"] == 3 + 4 and eng.run_left(b, None, "111") == 0  # 每次回复本身仍按 4 条拆；照发、照计数
        assert eng.run_left(b, None, "222") == 4 and eng.run_left(b2, None, "111") == 4  # 别的人、别的账号各数各的
        await eng.send_text(b, "private", "222", THREE, None, None, mode="at")
        assert len(texts(bot, "222")) == 3 and eng.dm_run["ac_900:222"] == 3 and "ac_901:111" not in eng.dm_run
        await eng.send_text(b2, "private", "111", THREE, None, None, mode="at")
        assert len(texts(bot2, "111")) == 3
        setg(e, "split_max_parts", 0)  # 0 = 不限
        assert eng.run_left(b, None, "111") > 10 ** 6
        await eng.send_text(b, "private", "333", SIX, None, None, mode="at")
        assert len(texts(bot, "333")) == 6
    env.run(body)


def test_groups_are_not_affected_by_the_private_counter(env):
    async def body(e):
        e.add_bot("900")
        eng = e.engine
        b = eng.duty(eng.bots["ac_900"], None)
        room = eng.room("qq", "100", "测试群")
        await eng.send_text(b, "private", "111", THREE, None, None, mode="at")
        assert room.run == {} and eng.run_left(b, room) == 4
    env.run(body)


# ---------- 二、微信 @ 只转行首 ----------
def test_wechat_only_a_line_leading_at_name_becomes_a_real_at_and_mid_sentence_stays_text(env):
    async def body(e):
        log = []
        ch = wx_channel(e, WxMain(log, []))
        sends = lambda: [x for x in log if x[0] == "send"]
        await ch.send("group", "群A", "@老王 来了", mention=[("老王", "老王")])  # 行首：转 @，文字里那段去掉；发时不传 who
        assert sends() == [("send", "来了", ["老王"], None)]
        log.clear()
        await ch.send("group", "群A", "你好 @老王，早", mention=[("老王", "老王")])  # 句中：保持原样，不删不挪
        assert sends() == [("send", "你好 @老王，早", None, None)]
        log.clear()
        await ch.send("group", "群A", "我先说\n  @老王 你呢，@小李 也来", mention=[("老王", "老王"), ("小李", "小李")])  # 第二行行首的算；句中的小李不算
        assert sends() == [("send", "我先说\n  你呢，@小李 也来", ["老王"], None)]
        log.clear()
        await ch.send("group", "群A", "@老王\n@小李 都来", mention=[("老王", "老王"), ("小李", "小李")])
        assert sends() == [("send", "都来", ["老王", "小李"], None)]
        # 引用 + 行首 @ 一起
        log.clear()
        ch2 = wx_channel(e, WxMain(log, [WxMsg("m1", log)]))
        await ch2.send("group", "群A", "@老王 回你", reply_to="m1", mention=[("老王", "老王")])
        assert ("quote", "m1", "回你", ["老王"]) in log
        log.clear()
        await ch2.send("group", "群A", "回 @老王 你", reply_to="m1", mention=[("老王", "老王")])
        assert ("quote", "m1", "回 @老王 你", None) in log
    env.run(body)


def test_qq_mid_sentence_conversion_is_unchanged():
    from mb.channels.onebot import QQChannel
    segs = QQChannel.with_mentions("你好 @A，早", [("1", "A")], False)
    assert [s["type"] for s in segs].count("at") == 1 and segs[1]["type"] == "at" and segs[1]["data"]["qq"] == "1"  # 句中的 @ 照旧发成 @ 段
    assert not any("@" in s["data"].get("text", "") for s in segs)


# ---------- 三、微信引用：按 id，找不到再按发送人 + 内容 ----------
class WxText(WxMsg):
    def __init__(self, mid, log, sender, content):
        super().__init__(mid, log)
        self.sender, self.content = sender, content


def test_wechat_quote_by_id_is_logged_as_found_by_id(env):
    async def body(e):
        log = []
        ch = wx_channel(e, WxMain(log, [WxText("m1", log, "老王", "你好"), WxText("m2", log, "小李", "在吗")]))
        await ch.send("group", "群A", "回你", reply_to="m2")
        assert ("quote", "m2", "回你", None) in log and logs(e, "按 id 找到") and not logs(e, "按内容找到")
    env.run(body)


def test_wechat_quote_falls_back_to_sender_and_content_when_the_id_changed(env):
    async def body(e):
        log = []
        msgs = [WxText("x1", log, "小李", "今天吃啥"), WxText("x2", log, "老王", "今天吃啥"), WxText("x3", log, "老王", "别的")]
        ch = wx_channel(e, WxMain(log, msgs))
        ch.heard["old-id"] = ({"老王"}, "今天吃啥")  # 收到时记下的发送人和内容；现在窗口里这条的 id 变了
        await ch.send("group", "群A", "回你", reply_to="old-id")
        assert ("quote", "x2", "回你", None) in log and not any(x[0] == "send" for x in log)
        assert logs(e, "按内容找到") and not logs(e, "找不到")
        # 发送人用备注名也对得上
        log.clear()
        m = WxText("y1", log, "老王的昵称", "今天吃啥")
        m.sender_remark = "老王"
        ch2 = wx_channel(e, WxMain(log, [m]))
        ch2.heard["old-id"] = ({"老王"}, "今天吃啥")
        await ch2.send("group", "群A", "回你", reply_to="old-id")
        assert ("quote", "y1", "回你", None) in log
    env.run(body)


def test_wechat_quote_with_several_or_no_content_match_sends_plain_and_logs_what_it_needs(env):
    async def body(e):
        log = []
        msgs = [WxText(f"id-{i}", log, "老王", "重复的话") for i in range(2)] + [WxText("id-9", log, "小李", "第三条消息的内容很长很长很长很长")]
        ch = wx_channel(e, WxMain(log, msgs))
        ch.heard["gone"] = ({"老王"}, "重复的话")
        await ch.send("group", "群A", "回你", reply_to="gone")  # 对上两条：不用
        assert [x[0] for x in log if x[0] in ("quote", "send")] == ["send"]
        line = logs(e, "找不到")[0]
        for need in ("gone", "老王", "重复的话", "按内容对上 2 条", "读到 3 条", "id-0", "id-9", "小李", "第三条消息的内"):
            assert need in line, need
        assert "第三条消息的内容很长很长很长很长" not in line  # 内容开头截短
        # 一条都对不上，而且从没记到这条：照样普通发，日志写明
        log.clear()
        await ch.send("group", "群A", "回你", reply_to="never-heard")
        assert [x[0] for x in log if x[0] in ("quote", "send")] == ["send"]
        assert logs(e, "never-heard") and logs(e, "没记到这条")
        # 最后 5 条
        many = [WxText(f"k{i}", log, "甲", f"消息{i}") for i in range(8)]
        ch3 = wx_channel(e, WxMain(log, many))
        await ch3.send("group", "群A", "回你", reply_to="nope")
        line = logs(e, "nope")[0]
        assert "读到 8 条" in line and "k7" in line and "k3" in line and "k2" not in line
        # 独立窗口方式也一样
        log.clear()
        ch4 = wx_channel(e, WxMain(log, []), {"群B": WxChat(log, [WxText("w1", log, "老王", "重复的话")])})
        ch4.heard["w0"] = ({"老王"}, "重复的话")
        await ch4.send("group", "群B", "回你", reply_to="w0")
        assert ("quote", "w1", "回你", None) in log
    env.run(body)


def test_wechat_remembers_who_said_what_for_incoming_messages_and_keeps_the_memory_bounded(env):
    async def body(e):
        import mb.channels.wechat as wmod
        ch = wx_channel(e, WxMain([], []))
        ch.conf = {"群A": {"name": "群A", "type": "group"}}

        async def on_message(msg):
            pass
        e.engine.on_message = on_message
        m = type("M", (), {"attr": "friend", "type": "text", "content": "你好呀", "sender": "老王", "sender_remark": "王哥", "id": "wxid-1"})()
        await asyncio.get_running_loop().run_in_executor(None, ch._handle, m, "群A")
        assert ch.heard["wxid-1"] == ({"老王", "王哥"}, "你好呀")
        wmod.HEARD_MAX = 3
        try:
            for i in range(5):
                m2 = type("M", (), {"attr": "friend", "type": "text", "content": f"c{i}", "sender": "老王", "id": f"n{i}"})()
                await asyncio.get_running_loop().run_in_executor(None, ch._handle, m2, "群A")
            assert list(ch.heard) == ["n2", "n3", "n4"]
        finally:
            wmod.HEARD_MAX = 300
    env.run(body)
