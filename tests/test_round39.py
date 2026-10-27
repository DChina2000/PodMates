"""主动插话前等对方说完、微信标清「对谁说」、守门员模板两处修改。全部用桩，不往任何群发消息。"""
import asyncio
import collections
import re
import subprocess
import time
from pathlib import Path

from mb.channels.base import Msg
from mb.engine import GATEKEEPER_PROMPT
from mb.settings import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
BY_KEY = {s["key"]: s for s in SCHEMA}


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


async def feed(e, uid, text, name=None, at=(), platform="qq", chat="100", reply_to_name=""):
    """只把消息交给引擎、不等它处理完（e.say 会等到所有定时任务结束，量不出时间）。"""
    e.seq = getattr(e, "seq", 0) + 1
    acc = next(a for a, b in e.engine.bots.items() if b.platform == platform)
    await e.engine.on_message(Msg(platform, acc, "group", chat, uid, name or f"群友{uid}", text, msg_id=f"{acc}-m{e.seq}", chat_name="测试群",
                                  at_ids=list(at), reply_to_name=reply_to_name, dedup=f"{chat}|{uid}|{e.seq}"))


def gate_log(e):
    """守门员每次被问的时刻和提示词。"""
    got = []

    def gate(p):
        got.append((time.time(), p))
        return {"reply": False, "brief": "不接"}
    e.llm.gate = gate
    return got


def newest(prompt):
    return prompt.split("【最新消息】\n")[1].split("\n")[0]


def gk_calls(e):
    return [c for c in e.llm.calls if c["_kind"] == "gk"]


# ---------- 一、守门员模板 ----------
def test_gatekeeper_template_only_the_two_sentences_changed():
    prev = subprocess.run(["git", "show", "a778ce8:mb/engine.py"], cwd=ROOT, capture_output=True, encoding="utf-8", check=True).stdout
    old = re.search(r'GATEKEEPER_PROMPT = """(.*?)"""', prev, re.S).group(1)
    a, b = "有人提到她或冲着她来但没@；", "有人提到她，或话里明摆着是冲她来的但没@（只凭语气、时机猜的不算）；"
    c, d = "她刚说过而话头没再转向她；", "她刚说过而话头没再转向她；消息是@别人或回复别人的，话头没转向她；"
    assert old.count(a) == 1 and old.count(c) == 1
    assert GATEKEEPER_PROMPT == old.replace(a, b).replace(c, d)


# ---------- 二、设置 ----------
def test_wait_settings_are_in_the_chime_in_group_with_the_documented_defaults():
    w, mx = BY_KEY["pro_wait"], BY_KEY["pro_wait_max"]
    assert (w["default"], w["cat"], w["adv"]) == (15, "插话", False) and (mx["default"], mx["cat"], mx["adv"]) == (30, "插话", True)
    assert w["scopes"] == BY_KEY["cooldown_seconds"]["scopes"] == mx["scopes"]
    assert "69%" in w["help"] and "15 秒" in w["help"] and "90%" in w["help"] and "29 秒" in w["help"] and "0 = 不等" in w["help"]


# ---------- 三、等对方说完 ----------
def test_three_messages_apart_ask_the_gatekeeper_once_after_the_last(env):
    async def body(e):
        e.add_bot("900")
        setg(e, "cooldown_seconds", 0)
        setg(e, "pro_wait", 1)
        setg(e, "pro_wait_max", 10)
        got = gate_log(e)
        t0 = time.time()
        for i in (1, 2, 3):
            await feed(e, "111", f"第{i}句")
            last = time.time()
            if i < 3:
                await asyncio.sleep(0.4)
        await e.engine.idle()
        assert len(got) == 1  # 等的期间一次都没问
        at, prompt = got[0]
        assert at - last >= 0.95 and at - t0 < 2.5
        assert newest(prompt) == "群友111: 第3句"  # 看到的是最后一条
        assert "群友111: 第1句" in prompt and "群友111: 第2句" in prompt  # 上下文里有前两条
    env.run(body)


def test_a_long_run_stops_waiting_at_the_maximum_and_asks_once(env):
    async def body(e):
        e.add_bot("900")
        setg(e, "cooldown_seconds", 0)
        setg(e, "pro_wait", 1)
        setg(e, "pro_wait_max", 2)
        got = gate_log(e)
        t0 = time.time()
        for i in range(1, 6):  # 每 0.6 秒一条，一直不停
            await feed(e, "111", f"第{i}条")
            await asyncio.sleep(0.6)
            if got:
                break
        assert len(got) == 1  # 到 2 秒就不再等，问了一次
        at, prompt = got[0]
        assert 1.9 <= at - t0 < 2.4
        assert newest(prompt).startswith("群友111: 第") and "第1条" in prompt
        await e.engine.idle()
    env.run(body)


def test_an_at_during_the_wait_is_handled_at_once_and_the_earlier_message_is_dropped(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "cooldown_seconds", 0)
        setg(e, "pro_wait", 2)
        got = gate_log(e)
        await feed(e, "111", "说到一半")
        await asyncio.sleep(0.3)
        t1 = time.time()
        await feed(e, "222", "喂", at=["900"])
        while not bot.sent and time.time() - t1 < 1.5:
            await asyncio.sleep(0.02)
        assert bot.sent and time.time() - t1 < 1.0  # 被@的马上回，没被 2 秒的等待卡住
        await e.engine.idle()
        assert got == []  # 前一条不再插（交给最新那条，最新那条是被@的，已经回了）
        assert len(bot.sent) == 1
    env.run(body)


def test_another_person_speaking_hands_the_chime_in_to_the_newest_message(env):
    async def body(e):
        e.add_bot("900")
        setg(e, "cooldown_seconds", 0)
        setg(e, "pro_wait", 1)
        got = gate_log(e)
        await feed(e, "111", "甲说的")
        await asyncio.sleep(0.3)
        await feed(e, "222", "乙说的")
        await e.engine.idle()
        assert len(got) == 1 and newest(got[0][1]) == "群友222: 乙说的"
    env.run(body)


def test_conditions_are_checked_again_after_the_wait(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        setg(e, "pro_wait", 1)
        setg(e, "cooldown_seconds", 600)
        await feed(e, "111", "先说一句")
        room = e.room()
        await asyncio.sleep(0.2)
        room.last_pro["ac_900"] = time.time()  # 等的期间她插过话了：冷却
        await e.engine.idle()
        assert bot.sent == [] and not gk_calls(e)
        setg(e, "cooldown_seconds", 0)
        await feed(e, "111", "再说一句")
        await asyncio.sleep(0.2)
        room.mute["ac_900"] = time.time() + 999  # 等的期间有人叫她闭嘴
        await e.engine.idle()
        assert bot.sent == [] and not gk_calls(e)
        room.mute.clear()
        setg(e, "hourly_limit_chat", 1)
        room.hourly["ac_900"] = collections.deque([time.time()])  # 本群每小时名额用完
        await feed(e, "111", "三说一句")
        await e.engine.idle()
        assert bot.sent == [] and not gk_calls(e)
        setg(e, "hourly_limit_chat", 90)
        room.hourly.clear()
        await feed(e, "111", "四说一句")  # 条件都过得去：等完就插
        await e.engine.idle()
        assert len(bot.sent) == 1 and len(gk_calls(e)) == 1
    env.run(body)


def test_zero_means_no_wait_and_behaves_as_before(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        setg(e, "cooldown_seconds", 0)
        assert e.core.settings.get("pro_wait") == 0  # 测试环境默认关掉（真实默认 15，见上面的设置测试）
        t0 = time.time()
        await e.say("111", "哈哈")
        assert time.time() - t0 < 1 and len(bot.sent) == 1 and not e.room().pro_wait
    env.run(body)


# ---------- 四、微信：开头的 @ 和引用 ----------
async def wechat_group(e):
    bot = e.add_bot("海豚", platform="wechat", name="海豚")
    setg(e, "proactive", False)
    for n in ("示例群友A", "示例群友B", "路人甲"):
        await e.say(n, "在", name=n, platform="wechat")
    return bot


def last_entry(e):
    return [x for x in e.room(platform="wechat").ctx if not x.bot][-1]


def test_wechat_leading_at_becomes_a_real_at(env):
    async def body(e):
        await wechat_group(e)
        await e.say("示例群友B", "@示例群友A 能买吗？", name="示例群友B", platform="wechat")
        en = last_entry(e)
        assert en.text == "能买吗？" and en.at_uids == ["示例群友A"] and en.at_ids == ["示例群友A"]
        assert e.engine.fmt(e.room(platform="wechat"), e.engine.bots["ac_海豚"], en, "她（自己）") == ("示例群友B", "（@示例群友A）能买吗？")
        # 开头连着几个 @，前面有空白
        await e.say("示例群友B", "  @示例群友A @路人甲 来看", name="示例群友B", platform="wechat")
        en = last_entry(e)
        assert en.text == "来看" and en.at_uids == ["示例群友A", "路人甲"]
    env.run(body)


def test_wechat_at_that_does_not_match_or_is_mid_sentence_stays_text(env):
    async def body(e):
        await wechat_group(e)
        for text in ("@不认识的人 你好", "你看 @示例群友A 能买吗", "@示例群友 能买吗"):  # 对不上 / 句中 / 只是名字的一部分
            await e.say("示例群友B", text, name="示例群友B", platform="wechat")
            en = last_entry(e)
            assert en.text == text and en.at_uids == []
        # 同名对着两个人：不转
        await e.say("u1", "在", name="重名", platform="wechat")
        await e.say("u2", "在", name="重名", platform="wechat")
        await e.say("示例群友B", "@重名 在吗", name="示例群友B", platform="wechat")
        assert last_entry(e).text == "@重名 在吗" and last_entry(e).at_uids == []
        # 对上的在前、对不上的在后：只转前面的
        await e.say("示例群友B", "@示例群友B @谁 嗯", name="示例群友B", platform="wechat")
        assert last_entry(e).text == "@谁 嗯" and last_entry(e).at_uids == ["示例群友B"]
    env.run(body)


def test_wechat_at_to_her_still_counts_as_calling_her_and_other_ats_are_kept(env):
    async def body(e):
        bot = await wechat_group(e)
        setg(e, "proactive", True)
        n0 = len(bot.sent)
        await e.say("示例群友B", "@示例群友A 你呢", name="示例群友B", platform="wechat", at=["海豚"])  # 渠道已经认出 @她（self_id 就是她的昵称）
        en = last_entry(e)
        assert en.at_uids == ["海豚", "示例群友A"] and en.text == "你呢"
        assert len(bot.sent) == n0 + 1  # 被叫到，照回
    env.run(body)


def test_wechat_quote_names_who_was_quoted(env):
    async def body(e):
        bot = await wechat_group(e)
        room = e.room(platform="wechat")
        await feed(e, "示例群友B", "回他", name="示例群友B", platform="wechat", reply_to_name="示例群友A")
        await e.engine.idle()
        en = room.ctx[-1]
        assert en.reply_uid == "示例群友A" and e.engine.fmt(room, e.engine.bots["ac_海豚"], en, "她（自己）")[0] == "示例群友B（回复 示例群友A）"
        await feed(e, "示例群友B", "回一个不认识的", name="示例群友B", platform="wechat", reply_to_name="陌生人")
        await e.engine.idle()
        assert room.ctx[-1].reply_uid == ""  # 认不出就不做
        setg(e, "proactive", True)
        n0 = len(bot.sent)
        await feed(e, "示例群友B", "引用了你", name="示例群友B", platform="wechat", reply_to_name="海豚")  # 被引用的是她自己：和 QQ 一样算回复了她
        await e.engine.idle()
        assert room.ctx[-2].reply_uid == "海豚" and len(bot.sent) == n0 + 1
    env.run(body)


def test_wechat_channel_passes_the_quoted_name_from_wxauto(env):
    async def body(e):
        from mb.channels.wechat import WeChatChannel
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        ch = WeChatChannel(e.engine, acc)
        ch.status, ch.wx, ch.loop = "online", object(), asyncio.get_running_loop()
        ch.conf = {"群A": {"name": "群A", "type": "group"}}
        ch.self_name = ch.self_id = "海豚"
        got = []

        async def on_message(msg):
            got.append(msg)
        e.engine.on_message = on_message
        q = type("M", (), {"attr": "friend", "type": "quote", "content": "同意", "sender": "老王", "id": "1", "quote_nickname": "小李", "quote_content": "吃饭吗"})()
        t = type("M", (), {"attr": "friend", "type": "text", "content": "你好", "sender": "老王", "id": "2"})()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ch._handle, q, "群A")
        await loop.run_in_executor(None, ch._handle, t, "群A")
        for _ in range(100):
            if len(got) == 2:
                break
            await asyncio.sleep(0.02)
        assert [m.reply_to_name for m in got] == ["小李", ""]
    env.run(body)
