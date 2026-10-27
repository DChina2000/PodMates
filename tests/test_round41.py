"""思考强度按模型真实档位、微信看图、私聊发表情包、私聊等对方说完、表情包工具默认每次都给、微信表情发出确认。全部用桩，不往任何群、任何私聊发消息，不连真的微信。"""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from conftest import FakeChannel
from test_round35 import logs
from test_round5b import png
from wxworld import World, fp

from mb import providers
from mb.channels.base import Msg
from mb.engine import NO_STICKER_NOTE
from mb.settings import BY_KEY, Settings


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def ep(provider, **kw):
    return dict(id="x", provider=provider, protocol=kw.pop("protocol", "openai"), base_url="http://x", **kw)


MSGS = [{"role": "user", "content": "hi"}]


def req(llm, provider, model, effort, **kw):
    return llm.build(ep(provider, **kw), "KEY", model, MSGS, 0.5, effort, 0)[2]


# ---------- 一、思考强度 ----------
def test_glm_5_3_only_offers_low_high_max_and_sends_only_values_the_docs_support(env):
    async def body(e):
        llm = e.core.llm
        for model in ("glm-5.3", "glm-5.3-flash"):
            assert providers.effort_levels(ep("zhipuai"), model) == ["low", "high", "max"]
        high = req(llm, "zhipuai", "glm-5.3-flash", "high")
        assert high["thinking"]["type"] == "enabled" and high["reasoning_effort"] == "high"
        assert req(llm, "zhipuai", "glm-5.3-flash", "low")["reasoning_effort"] == "low"
        assert req(llm, "zhipuai", "glm-5.3-flash", "max")["reasoning_effort"] == "max"
        med = req(llm, "zhipuai", "glm-5.3-flash", "medium")  # 已存的「中」：模型不支持，换成「高」，不再发 medium
        assert med["reasoning_effort"] == "high" and "medium" not in json.dumps(med)
        none = req(llm, "zhipuai", "glm-5.3-flash", "")  # 不能关思考：保持不传
        assert "reasoning_effort" not in none and "thinking" not in none
        # 别的模型：没资料的照旧
        assert req(llm, "zhipuai", "glm-4.9-unknown", "medium")["reasoning_effort"] == "medium"
    env.run(body)


def test_other_providers_keep_their_own_dialects_and_levels(env):
    async def body(e):
        llm = e.core.llm
        ds = req(llm, "deepseek", "deepseek-flash", "medium")  # 目录里 deepseek-flash 是 low / high / max：中 → 高
        assert ds["thinking"] == {"type": "enabled"} and ds["reasoning_effort"] == "high"
        assert req(llm, "deepseek", "deepseek-flash", "")["thinking"] == {"type": "disabled"}  # DeepSeek 能关，空值照旧是关
        assert req(llm, "deepseek", "deepseek-flash", "low")["reasoning_effort"] == "low"
        assert req(llm, "openai", "m", "medium")["reasoning_effort"] == "medium"
        a = req(llm, "anthropic", "claude-x", "high", protocol="anthropic")
        assert a["thinking"] == {"type": "adaptive"} and a["output_config"] == {"effort": "high"}
        g = req(llm, "google", "gemini-3-pro", "medium", protocol="gemini")
        assert g["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
        assert req(llm, "moonshotai-cn", "kimi-k3", "high").get("reasoning_effort") is None  # 不认识写法的不发
    env.run(body)


def test_nearest_supported_level_and_panel_options():
    z = ep("zhipuai")
    assert providers.fit_effort(z, "glm-5.3", "medium") == "high" and providers.fit_effort(z, "glm-5.3", "low") == "low"
    assert providers.fit_effort(z, "glm-5.3", "high") == "high" and providers.fit_effort(z, "glm-5.3", "") == ""
    assert providers.fit_effort(z, "glm-5.2", "low") == "high"  # glm-5.2 只有 high / max
    assert providers.fit_effort(ep("openai"), "my-model", "max") == "max"  # 没资料：不动
    assert providers.effort_options(z, "glm-5.3", "high") == [["low", "低"], ["high", "高"], ["max", "最高"]]
    assert providers.effort_options(z, "glm-5.3", "")[0] == ["", "最高（默认）"]  # 已存空值：显示成「最高（默认）」
    assert providers.effort_options(z, "glm-4.9-unknown", "") is None  # 没资料：用全套选项


def test_defaults_are_all_high_and_saved_values_are_converted_only_when_unsupported(env):
    async def body(e):
        for k in ("main_effort", "gk_effort", "mem_effort", "vis_effort"):
            assert BY_KEY[k]["default"] == "high"
        db = e.core.db
        db.put("endpoint", {"id": "zp", "name": "zp", "provider": "zhipuai", "protocol": "openai", "base_url": "http://x"})
        s = e.core.settings
        for p in ("main", "gk", "mem", "vis"):
            s.set("global", f"{p}_endpoint", "zp")
            s.set("global", f"{p}_model", "glm-5.3-flash")
        s.set("global", "main_effort", "medium")   # 不支持：显示换算成「高」
        s.set("global", "gk_effort", "low")         # 手动设的：不动
        s.set("global", "mem_effort", "")           # 不能关：保持空，显示「最高（默认）」
        s.set("global", "vis_effort", "high")
        view = {it["key"]: it for it in s.view("global")}
        assert view["main_effort"]["value"] == "high" and view["main_effort"]["options"] == [["low", "低"], ["high", "高"], ["max", "最高"]]
        assert view["gk_effort"]["value"] == "low" and view["vis_effort"]["value"] == "high"
        assert view["mem_effort"]["value"] == "" and view["mem_effort"]["options"][0] == ["", "最高（默认）"]
        assert s.get("main_effort") == "medium"  # 存的值本身没改
    env.run(body)


# ---------- 二、微信看图 ----------
class Pic:
    """wxauto4 的 ImageMessage 桩：download() 把图写成一个文件、返回路径。"""

    def __init__(self, tmp, mid="p1", type="image", content="[图片]", fail=False, sender="老王", data=None):
        self.attr, self.type, self.content, self.sender, self.id = "friend", type, content, sender, mid
        self.tmp, self.fail, self.downloads, self.path = tmp, fail, 0, None
        self.data = png(3) if data is None else data

    def download(self):
        self.downloads += 1
        if self.fail:
            raise RuntimeError("下载超时")
        self.path = Path(self.tmp) / f"dl{self.id}.png"
        self.path.write_bytes(self.data)
        return self.path


def wx_channel(e, conf):
    from mb.channels.wechat import WeChatChannel
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
    ch = WeChatChannel(e.engine, acc)
    ch.status, ch.wx, ch.loop = "online", object(), asyncio.get_running_loop()
    ch.conf = conf
    ch.self_name = ch.self_id = "海豚"
    e.engine.add_bot(acc, ch)
    return ch


async def wx_feed(e, ch, m, who="群A"):
    room = e.engine.rooms.get(f"wechat:{who}")
    n0 = len(room.ctx) if room else 0
    await asyncio.get_running_loop().run_in_executor(None, ch._handle, m, who)
    for _ in range(250):  # 独立窗口方式下载是排到微信线程里做的
        await asyncio.sleep(0.02)
        room = e.engine.rooms.get(f"wechat:{who}")
        if room and len(room.ctx) > n0:
            break
    await e.engine.idle()


def ctx_texts(e, who="群A"):
    return [x.text for x in e.engine.rooms[f"wechat:{who}"].ctx]


def test_wechat_picture_is_downloaded_described_and_the_file_is_deleted(env, tmp_path):
    async def body(e):
        ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
        setg(e, "proactive", False)
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "deepseek-flash")
        e.llm.vis = lambda content: {"desc": "一只猫"}
        pic = Pic(tmp_path)
        await wx_feed(e, ch, pic)
        assert pic.downloads == 1
        assert ctx_texts(e)[-1].startswith("[图片：") and "一只猫" in ctx_texts(e)[-1] and "[图片] " not in ctx_texts(e)[-1]
        assert not pic.path.exists()  # 下载下来的文件用完删掉
        assert ch.pics == {} and "vis" in e.llm.kinds()
    env.run(body)


def test_wechat_emoji_messages_are_not_downloaded_when_the_sticker_switch_is_off(env, tmp_path):
    async def body(e):
        ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
        setg(e, "proactive", False)
        setg(e, "vision_stickers", False)  # 默认开的「群友发的表情也看」关掉才不下载表情（见 test_round47b）
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "deepseek-flash")
        e.llm.vis = lambda content: {"desc": "x"}
        emo = Pic(tmp_path, "e1", type="emotion", content="[表情]")
        await wx_feed(e, ch, emo)
        anim = Pic(tmp_path, "e2", type="image", content="[动画表情]")  # 内容是「[动画表情]」的，哪怕类型写成图片也不下载
        await wx_feed(e, ch, anim)
        assert emo.downloads == anim.downloads == 0 and "vis" not in e.llm.kinds()
        assert ctx_texts(e)[0] == "[表情]"
    env.run(body)


def test_wechat_download_failure_writes_the_plain_marker_once_and_does_not_retry(env, tmp_path):
    async def body(e):
        ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
        setg(e, "proactive", False)
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "deepseek-flash")
        e.llm.vis = lambda content: {"desc": "x"}
        pic = Pic(tmp_path, fail=True)
        await wx_feed(e, ch, pic)
        assert pic.downloads == 1 and ctx_texts(e) == ["[图片]"] and "vis" not in e.llm.kinds()
        assert logs(e, "下载图片失败")
    env.run(body)


def test_wechat_picture_is_not_downloaded_when_vision_is_off_or_has_no_model(env, tmp_path):
    async def body(e):
        ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
        setg(e, "proactive", False)
        pic = Pic(tmp_path, "a")
        await wx_feed(e, ch, pic)  # 看图默认关
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "")
        pic2 = Pic(tmp_path, "b")
        await wx_feed(e, ch, pic2)
        assert pic.downloads == pic2.downloads == 0 and ctx_texts(e) == ["[图片]", "[图片]"]
    env.run(body)


def test_wechat_download_goes_through_the_wechat_thread_queue(env, tmp_path):
    async def body(e):
        import threading
        ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
        setg(e, "proactive", False)
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "deepseek-flash")
        e.llm.vis = lambda content: {"desc": "x"}
        pic = Pic(tmp_path)
        names = []
        orig = pic.download
        pic.download = lambda: names.append(threading.current_thread().name) or orig()
        await wx_feed(e, ch, pic)
        assert names and names[0].startswith("wechat")  # 在微信线程里做，和收发消息排同一个队
    env.run(body)


# ---------- 三、私聊发表情包 ----------
class DmChannel(FakeChannel):
    """能发图的私聊渠道；微信那种按格子发的（sticker_by_row）也用它。"""

    def __init__(self, *a, by_row=False, **k):
        super().__init__(*a, **k)
        self.sticker_by_row = by_row
        self.sent_stickers = []

    async def send_image(self, chat_type, chat_id, data, reply_to=None, sticker=None):
        self.sent_stickers.append((chat_type, chat_id, len(data), sticker))
        if sticker is not None:
            sticker.update(pos=sticker["pos"], ticks=0, fps=sticker["fps"])
        return await super().send_image(chat_type, chat_id, data, reply_to)


def dm_bot(e, uid="900", platform="qq", by_row=False):
    acc = e.core.db.put("account", {"id": f"ac_{uid}", "platform": platform, "role_id": "r_haitun", "qq": uid})
    ch = DmChannel(e.engine, acc, uid, f"bot{uid}", by_row=by_row)
    e.engine.add_bot(acc, ch)
    return ch


def add_fav(e, aid="ac_900", descr="无语", n=7):
    st = e.engine.stickers
    sp = st.fav_space(aid)
    name = f"fav{n}.png"
    (st.folder() / name).write_bytes(png(n))
    mid = e.engine.memory.add(sp, "sticker", str(n), "收藏的表情", descr, "inferred", None, f"fav:{aid}", status="active")
    cur = e.core.db.x("INSERT INTO stickers (space, room, phash, seen, users, status, file, size, uses, descr, mem_id, source, account) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sp, f"fav:{aid}", f"{n:016x}", 1, "[]", "learned", name, 1, 0, descr, mid, "fav", aid))
    return cur.lastrowid


async def dm(e, text, uid="111", account="ac_900", platform="qq", images=None, ts=None):
    e.seq = getattr(e, "seq", 0) + 1
    m = Msg(platform, account, "private", uid, uid, f"人{uid}", text, msg_id=f"{account}-d{e.seq}", images=list(images or []))
    if ts:
        m.ts = ts
    await e.engine.on_message(m)
    return m


def main_calls(e):
    return [c for c in e.llm.calls if c["_kind"] == "main"]


def want_sticker(e):
    e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None


def test_private_chat_gets_the_sticker_tool_from_the_accounts_favourites_and_sends_it(env):
    async def body(e):
        bot = dm_bot(e)
        add_fav(e)
        want_sticker(e)
        e.llm.main = lambda s, u: "行吧\n【表情1】"
        setg(e, "sticker_chance", 0)  # 概率 0：私聊每次回复按「被叫到」算，「被叫到时总给」开着就一定给
        await dm(e, "你好")
        await e.engine.idle()
        assert [t["function"]["name"] for t in main_calls(e)[0]["tools"]] == ["send_sticker"]
        assert [x[2] for x in bot.sent] == ["行吧"] and bot.sent[0][0] == "private"
        assert len(bot.images) == 1 and bot.images[0][0] == "private" and bot.images[0][1] == "111"
        assert e.engine.dm_run["ac_900:111"] == 2  # 文字一条 + 表情包一条，都算私聊里连着发的
        assert e.core.db.rows("SELECT uses FROM stickers WHERE status='learned'")[0]["uses"] == 1
        assert e.core.db.rows("SELECT 1 FROM messages WHERE room='dm:ac_900:111' AND kind='bot' AND text LIKE '[表情包：无语%'")
        # 「被叫到时总给」关掉：私聊也按概率，0 就不给，并且提示词末尾说明发不了
        setg(e, "sticker_call", False)
        await dm(e, "再来")
        await e.engine.idle()
        last = main_calls(e)[-1]
        assert "tools" not in last and last["messages"][0]["content"].endswith(NO_STICKER_NOTE.strip())
    env.run(body)


def test_private_chat_without_favourites_or_with_the_switch_off_gets_no_tool_and_no_note(env):
    async def body(e):
        bot = dm_bot(e)
        await dm(e, "没有收藏")
        await e.engine.idle()
        first = main_calls(e)[-1]
        assert "tools" not in first and "发不了表情包" not in first["messages"][0]["content"]  # 账号根本没有表情包：不加那句
        add_fav(e)
        setg(e, "sticker_use", False)
        await dm(e, "关了发表情包")
        await e.engine.idle()
        last = main_calls(e)[-1]
        assert "tools" not in last and "发不了表情包" not in last["messages"][0]["content"]  # 「发表情包」关了：不加
        setg(e, "sticker_use", True)
        bot.supports_image = False  # 平台不能发
        await dm(e, "平台不能发")
        await e.engine.idle()
        last = main_calls(e)[-1]
        assert "tools" not in last and last["messages"][0]["content"].endswith(NO_STICKER_NOTE.strip())
    env.run(body)


def test_private_sticker_does_not_go_out_when_the_consecutive_limit_is_used_up(env):
    async def body(e):
        bot = dm_bot(e)
        add_fav(e)
        want_sticker(e)
        e.llm.main = lambda s, u: "行吧\n【表情1】"
        setg(e, "split_max_parts", 1)  # 文字发完就满了
        await dm(e, "你好")
        await e.engine.idle()
        assert [x[2] for x in bot.sent] == ["行吧"] and bot.images == [] if hasattr(bot, "images") else True
        assert not getattr(bot, "images", [])
    env.run(body)


def test_wechat_private_sticker_goes_out_by_grid_cell_and_fingerprint(env):
    async def body(e):
        bot = dm_bot(e, "wx", platform="wechat", by_row=True)
        sid = add_fav(e, "ac_wx", n=5)
        e.core.db.set_kv("wxfav:ac_wx", {"entries": [{"sid": sid, "pos": 4, "ticks": 0, "fps": [f"{0x5:016x}"]}]})
        want_sticker(e)
        e.llm.main = lambda s, u: "好\n【表情1】"
        await dm(e, "在吗", uid="小王", account="ac_wx", platform="wechat")
        await e.engine.idle()
        assert [x[2] for x in bot.sent] == ["好"]
        assert len(bot.sent_stickers) == 1 and bot.sent_stickers[0][:2] == ("private", "小王") and bot.sent_stickers[0][3]["pos"] == 4
        assert e.engine.dm_run["ac_wx:小王"] == 2
    env.run(body)


# ---------- 四、私聊等对方说完 ----------
def test_wait_setting_is_in_the_pacing_group_next_to_the_private_gap():
    s, g = BY_KEY["private_wait"], BY_KEY["private_reply_gap"]
    assert (s["default"], s["cat"], s["scopes"]) == (5, "节奏", g["scopes"])
    assert "三成多" in s["help"] and "5 秒" in s["help"] and "0 = 不等" in s["help"] and "让她回这条" in s["help"]
    keys = [x["key"] for x in __import__("mb.settings", fromlist=["SCHEMA"]).SCHEMA]
    assert keys.index("private_wait") == keys.index("private_reply_gap") - 1


def test_three_bubbles_two_seconds_apart_get_one_reply_that_sees_all_three(env):
    async def body(e):
        bot = dm_bot(e)
        setg(e, "private_wait", 1)
        setg(e, "pro_wait_max", 10)
        t0 = time.time()
        for i in (1, 2, 3):
            await dm(e, f"第{i}条")
            await asyncio.sleep(0.4)
        await e.engine.idle()
        calls = main_calls(e)
        assert len(calls) == 1 and len(bot.sent) == 1  # 只回一次
        user = calls[0]["messages"][-1]["content"]
        assert user == "第1条\n第2条\n第3条"  # 写回复时三条都看得到
        assert time.time() - t0 >= 1.75  # 第三条（0.8 秒时）之后又等了约 1 秒
        assert bot.sent[0][3] is None
        hist = e.engine.private_hist["ac_900:111"]
        assert hist[0] == {"role": "user", "content": "第1条\n第2条\n第3条"} and hist[1]["role"] == "assistant"
    env.run(body)


def test_a_run_that_never_stops_is_answered_at_the_maximum_wait(env):
    async def body(e):
        bot = dm_bot(e)
        setg(e, "private_wait", 1)
        setg(e, "pro_wait_max", 2)
        t0 = time.time()
        for i in range(1, 6):
            await dm(e, f"连发{i}")
            await asyncio.sleep(0.6)
            if main_calls(e):
                break
        assert main_calls(e) and 1.9 <= time.time() - t0 <= 3.2
        user = main_calls(e)[0]["messages"][-1]["content"]
        assert user.startswith("连发1") and "连发3" in user
        await e.engine.idle()
    env.run(body)


def test_make_her_reply_to_this_does_not_wait_and_zero_means_every_message_is_answered(env):
    async def body(e):
        bot = dm_bot(e)
        setg(e, "private_wait", 30)
        t0 = time.time()
        b = e.engine.bots["ac_900"]
        await e.engine.handle_private(b, Msg("qq", "ac_900", "private", "111", "111", "人111", "马上回"), force=True)
        assert time.time() - t0 < 2 and [x[2] for x in bot.sent] == ["收到"]
        setg(e, "private_wait", 0)
        await dm(e, "甲")
        await e.engine.idle()
        await dm(e, "乙")
        await e.engine.idle()
        assert len(main_calls(e)) == 3 and len(bot.sent) == 3  # 0 = 来一条回一条
    env.run(body)


def slow_second(e, bot, extra, action):
    """主模型写回复的时候，对方又发来一条（桩：回复一写好就塞进去）。要等对方说完（>0）才有「合成一次回」和过时检查。"""
    setg(e, "private_wait", 1)
    state = {"n": 0}
    e.llm.stale = lambda p: action(p)

    def main(system, user):
        state["n"] += 1
        if state["n"] == 1:
            asyncio.get_running_loop().create_task(dm(e, extra))
        return f"回复{state['n']}"
    e.llm.main = main
    return state


def test_new_message_while_she_writes_is_handled_by_the_stale_check_not_a_separate_reply(env):
    async def body(e):
        bot = dm_bot(e)
        slow_second(e, bot, "补一句", lambda p: {"action": "send", "why": "还合适"})
        await dm(e, "先说")
        await e.engine.idle()
        assert len(main_calls(e)) == 1 and [x[2] for x in bot.sent] == ["回复1"]  # 照发，新来的那条不另外回
        assert [c for c in e.llm.calls if c["_kind"] == "stale"]
        assert "补一句" in [c for c in e.llm.calls if c["_kind"] == "stale"][0]["messages"][-1]["content"]
        assert e.engine.private_hist["ac_900:111"][0]["content"] == "先说\n补一句"
    env.run(body)


def test_stale_rewrite_sends_the_rewrite_and_drop_replies_again_to_everything_together(env):
    async def body(e):
        bot = dm_bot(e)
        slow_second(e, bot, "补一句", lambda p: {"action": "rewrite", "text": "改写后的话", "why": "顾上新的"})
        await dm(e, "先说")
        await e.engine.idle()
        assert [x[2] for x in bot.sent] == ["改写后的话"] and len(main_calls(e)) == 1
    env.run(body)

    async def body2(e):
        bot = dm_bot(e)
        slow_second(e, bot, "补一句", lambda p: {"action": "drop", "why": "对不上了"})
        await dm(e, "先说")
        await e.engine.idle()
        calls = main_calls(e)
        assert len(calls) == 2 and calls[1]["messages"][-1]["content"] == "先说\n补一句"  # 重新回时两条一起看
        assert [x[2] for x in bot.sent] == ["回复2"]  # 第一次写的没发
    env.run(body2)


def test_the_private_gap_and_the_wait_are_independent_and_the_stricter_one_wins(env):
    async def body(e):
        bot = dm_bot(e)
        setg(e, "private_wait", 1)
        setg(e, "private_reply_gap", 2)
        await dm(e, "一")
        await e.engine.idle()
        t = time.time()
        await dm(e, "二")
        await e.engine.idle()
        assert len(bot.sent) == 2 and time.time() - t >= 1.8  # 等对方停 1 秒，再补足到离上一次回复满 2 秒
    env.run(body)


def test_private_pictures_are_described_in_a_private_chat_too(env):
    async def body(e):
        bot = dm_bot(e)
        bot.imgs["http://img/1"] = png(1)
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "deepseek-flash")
        e.llm.vis = lambda content: {"desc": "一只猫"}
        await dm(e, "", images=["http://img/1"])
        await e.engine.idle()
        assert "vis" in e.llm.kinds()
        user = main_calls(e)[-1]["messages"][-1]["content"]
        assert user.startswith("[图片：") and "一只猫" in user
    env.run(body)


# ---------- 五、表情包工具默认每次都给 ----------
def test_sticker_chance_defaults_to_100_and_a_chime_in_gets_the_tool(env):
    async def body(e):
        assert BY_KEY["sticker_chance"]["default"] == 100
        assert "不等于每次都发" in BY_KEY["sticker_chance"]["help"] and "看气氛" in BY_KEY["sticker_chance"]["help"] and "调低" in BY_KEY["sticker_chance"]["help"]
        e.add_bot("900")
        add_fav(e)
        eng = e.engine
        await e.say("111", "你好")
        room = e.room()
        b = eng.duty(eng.bots["ac_900"], room)
        tools, _, hint = eng.tools_for(b, room, "main", False)  # 主动插话：没叫到，默认概率 100 也给
        assert [t["name"] for t in tools] == ["send_sticker"] and NO_STICKER_NOTE not in hint
    env.run(body)


def test_note_is_added_only_when_stickers_should_have_been_available_but_the_tool_was_not_given(env):
    async def body(e):
        bot = e.add_bot("900")
        add_fav(e)
        eng = e.engine
        await e.say("111", "你好")
        room = e.room()
        b = eng.duty(eng.bots["ac_900"], room)
        hint = lambda called=False: eng.tools_for(b, room, "main", called)[2]
        setg(e, "sticker_chance", 0)
        assert hint().endswith(NO_STICKER_NOTE) and "【你手边的工具】" not in hint()  # 概率调低没给：加那句
        assert "send_sticker" in hint(True) and NO_STICKER_NOTE not in hint(True)  # 被叫到时总给：给了就不加
        setg(e, "sticker_use", False)
        assert hint() == ""  # 「发表情包」关了：不加
        setg(e, "sticker_use", True)
        e.core.db.x("DELETE FROM stickers")
        assert hint() == ""  # 没有表情包：不加
    env.run(body)


def test_a_chance_set_by_hand_is_left_alone_and_unset_ones_follow_the_new_default(env):
    async def body(e):
        s = e.core.settings
        s.set("global", "sticker_chance", 40)
        s.set("platform:qq", "sticker_chance", 10)
        assert s.get("sticker_chance") == 40 and s.get("sticker_chance", platform="qq") == 10
        assert s.get("sticker_chance", platform="wechat") == 40
        s.reset("global", "sticker_chance")
        assert s.get("sticker_chance", platform="wechat") == 100  # 没手动设过的：跟着新默认
    env.run(body)


# ---------- 六、微信表情发出确认 ----------
def entry_for(w, i):
    return {"pos": i, "ticks": 0, "fps": [fp(w.items[i][0])]}


def test_any_new_message_of_her_own_confirms_the_send_whatever_its_type():
    from mb.wxsticker import WxStickers
    before = [("1", "text", "friend", "在吗"), ("2", "text", "self", "好")]
    for typ, content in (("emotion", "[动画表情]"), ("image", "[图片]"), ("other", "x"), ("text", "")):
        after = before + [("3", typ, "self", content)]
        assert WxStickers._emoji_added(before, after) is True
    assert WxStickers._emoji_added(before, before + [("3", "emotion", "friend", "[表情]")]) is False  # 别人发的不算
    assert WxStickers._emoji_added(before, before) is False


def test_when_not_confirmed_the_log_carries_the_type_attr_and_content_before_and_after():
    w = World([(f"s{i}", False) for i in range(5)], sends=False)
    w.msgs.append(SimpleNamespace(id="m0", type="text", attr="friend", content="之前的话"))
    log = []
    r = w.driver(log=log.append).send(w.wx(), entry_for(w, 2), "目标群", "")
    assert r["clicked"] and r["confirmed"] is False
    line = next(x for x in log if "没确认发出" in x)
    assert "type=text|attr=friend|之前的话" in line and "点之前" in line and "点之后" in line

    w2 = World([(f"s{i}", False) for i in range(5)], sends=False)
    orig_pick = w2.pick
    w2.pick = lambda i: (orig_pick(i), w2.msgs.append(SimpleNamespace(id="n1", type="other", attr="self", content="x")))  # 真机上发出的是什么类型都行
    r = w2.driver().send(w2.wx(), entry_for(w2, 1), "目标群", "")
    assert r["confirmed"] is True
