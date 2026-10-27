"""补充十五：群聊实测后的默认值（冷却 60、占比上限默认开、思考强度中）和「短句分条发」。"""
import asyncio

from conftest import FakeChannel
from mb.engine import split_reply
from mb.settings import BY_KEY

HAITUN = "90001"


def test_new_defaults():
    assert BY_KEY["cooldown_seconds"]["default"] == 60
    assert BY_KEY["share_limit_on"]["default"] is True and BY_KEY["share_window"]["default"] == 15 and BY_KEY["share_mode"]["default"] == "count" and BY_KEY["share_max_count"]["default"] == 7  # 最近 15 条里最多 7 条
    assert BY_KEY["share_max"]["default"] == 47 and BY_KEY["share_min_on"]["default"] is False and BY_KEY["share_min_n"]["default"] == 10
    assert BY_KEY["eager_auto"]["default"] is False and BY_KEY["stop_on"]["default"] is False  # 仍默认关
    assert BY_KEY["main_effort"]["default"] == "high"
    assert BY_KEY["gk_effort"]["default"] == "high"  # 四项思考强度默认都是高
    assert BY_KEY["split_enabled"]["default"] is True and BY_KEY["split_max_parts"]["default"] == 4  # 现在是「中间没人说话时最多连着发几条」，默认 4


REPLY = "草 接这个\n不是管得差 是大他者当场暴毙\n穆巴拉克最后几年政权早就空转了\n大家排的队是“国家还在管事”\n\n再来一句\n哈\n最后一句话收尾"


def test_every_short_line_is_its_own_message():
    parts = split_reply(REPLY, 80, 8)
    assert parts == ["草 接这个", "不是管得差 是大他者当场暴毙", "穆巴拉克最后几年政权早就空转了", "大家排的队是“国家还在管事”", "再来一句", "哈", "最后一句话收尾"]  # 空行丢掉，一行一条
    # 以前的写法会把 157 字 10 行的回复并成两条、每条 5 行：一行一条之后，条数 = 行数（不超过上限）
    ten = "\n".join(f"第{i}句话，不长。" for i in range(10))
    assert len(split_reply(ten, 80, 12)) == 10
    assert len(ten) > 80 and all("\n" not in p for p in split_reply(ten, 80, 12))


def test_overflow_merges_the_shortest_neighbours_not_the_whole_tail():
    parts = split_reply(REPLY, 80, 4)
    assert len(parts) == 4 and "".join(parts).replace("\n", "") == REPLY.replace("\n", "")  # 一个字不丢、顺序不变
    assert max(p.count("\n") + 1 for p in parts) <= 3  # 以前是前几条各塞几行、最后一条把剩下的全兜走
    assert len(split_reply(REPLY, 80, 1)) == 1 and split_reply(REPLY, 80, 1)[0].count("\n") == 6
    ten = "\n".join(f"第{i}句话，不长。" for i in range(10))
    assert [p.count("\n") + 1 for p in split_reply(ten, 80, 6)] == [2, 2, 2, 2, 1, 1] or max(p.count("\n") + 1 for p in split_reply(ten, 80, 6)) <= 2


def test_long_single_line_splits_by_sentence_and_short_text_stays():
    assert split_reply("好。", 80, 6) == ["好。"] and split_reply("一句话没有换行，也不长。第二句。", 80, 6) == ["一句话没有换行，也不长。第二句。"]
    long = "这是一句比较长的话。" * 30
    parts = split_reply(long, 40, 6)
    assert 1 < len(parts) <= 6 and all(len(p.replace("\n", "")) > 0 for p in parts) and "".join(parts).replace("\n", "") == long
    # 一行特别长、其他行很短：长的那行按句子拆，短的各一条
    mixed = "短一\n" + "这是一句比较长的话。" * 12 + "\n短二"
    p = split_reply(mixed, 40, 10)
    assert p[0] == "短一" and p[-1] == "短二" and len(p) >= 4
    assert split_reply("", 80, 6) == [""] and split_reply("  \n \n", 80, 6) == [""]


def test_wechat_and_qq_both_send_one_message_per_line(env):
    async def body(e):
        e.core.settings.set("global", "proactive", False)
        e.core.settings.set("global", "split_max_parts", 6)  # 这个测试看的是「每行一条」，条数上限单独设成 6
        qq = e.add_bot("900")
        wx = e.add_bot("901", platform="wechat")  # 微信的账号：splitting 不按平台关
        e.llm.main = lambda s, u: REPLY
        await e.say(HAITUN, "说点什么", at=["900"])
        assert [x[2] for x in qq.sent][:3] == ["草 接这个", "不是管得差 是大他者当场暴毙", "穆巴拉克最后几年政权早就空转了"] and len(qq.sent) == 6  # 默认最多 6 条，最短的相邻两行并在一起
        e.core.db.put("person", dict(id="pe_w", name="某人", identity="", call="", qq=[], discord=[], wechat_remark=["秘密备注妈"], nicks={}, note="", knows="",
                                     attitude={"presets": [], "custom": ""}))
        e.engine.reload()
        await e.say("秘密备注妈", "微信里说点什么", chat="群W", at=["901"], platform="wechat", account="ac_901")
        assert len(wx.sent) == 6 and wx.sent[0][2] == "草 接这个"
        # 评论区类平台和 LINE 默认不拆；设置里打开后就拆
        from mb.settings import Settings
        s = e.core.settings
        assert s.get("split_enabled", platform="wechat") is True and s.get("split_enabled", platform="qq") is True and s.get("split_enabled", platform="discord") is True
        assert all(s.get("split_enabled", platform=p) is False for p in ("x", "line", "reddit", "youtube", "bilibili", "tieba", "weibo"))
    env.run(body)


class SlowChannel(FakeChannel):
    """发一条要一会儿（像微信的界面自动化），发的过程中群里能来新消息。"""

    async def send(self, chat_type, chat_id, text, reply_to=None, at=None):
        await asyncio.sleep(0.05)
        return await super().send(chat_type, chat_id, text, reply_to, at)


def test_new_message_in_the_middle_does_not_cut_the_remaining_parts(env):
    async def body(e):
        e.core.settings.set("global", "proactive", False)
        e.core.settings.set("global", "split_max_parts", 6)  # 看的是「中途来新消息不切断剩下的几条」，条数上限单独设成 6
        acc = e.core.db.put("account", {"id": "ac_900", "platform": "qq", "role_id": "r_haitun", "qq": "900"})
        bot = SlowChannel(e.engine, acc, "900", "bot900")
        e.engine.add_bot(acc, bot)
        e.llm.main = lambda s, u: "收到新的" if u.rstrip().endswith("中途又来了一句，也在叫你") or "【最新消息】" in u and "中途又来了一句" in u.split("【最新消息】")[-1] else REPLY
        task = asyncio.create_task(e.say(HAITUN, "说点什么", at=["900"]))
        for _ in range(200):  # 等第一条发出去、后面还没发完
            if bot.sent:
                break
            await asyncio.sleep(0.01)
        assert 0 < len(bot.sent) < 6
        await e.say(HAITUN, "中途又来了一句，也在叫你", at=["900"])  # 拆出来的几条发送中途来了新消息
        await task
        await e.engine.idle()
        texts = [x[2] for x in bot.sent]
        assert texts[:6] == ["草 接这个", "不是管得差 是大他者当场暴毙", "穆巴拉克最后几年政权早就空转了", "大家排的队是“国家还在管事”", "再来一句\n哈", "最后一句话收尾"]  # 剩下的照常发完，顺序不乱
        assert texts.count("最后一句话收尾") == 1 and "收到新的" in texts  # 没重复；新消息也被处理了
    env.run(body)
