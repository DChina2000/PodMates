"""群聊分寸（续）：表情包、发言占比、插话力度自动调节、「别说了」、用量硬上限，以及署名与仓库地址。"""
import hashlib
import io
import random
import re
import time
from pathlib import Path

from mb import pacing

ROOT = Path(__file__).resolve().parent.parent

# 参考项目黑名单：只存名字（统一转小写）的 sha256，不在仓库里写名字原文。
BAD = {
    "631009562f844ededac21c974891bdcbb6b201baa4bf3f60c7ec9ca357b94fe6",
    "8d9b71c514f6cd0a3bfe310a6cc43a53618d036101878469aa757189ef05df6a",
    "ab8ee38e9f142b447506f2636cf3c2a5df5221c5dd3a76b254712b04debdeca4",
    "6c205af5c749cc272d2dcd642563a293d0452b9203800cab87a0bfdbaacc8461",
    "315f002ee8d7fa948480eae332ac04eb848edf87db5eac477bff1bdd7664dbb7",
    "fee15a53420426ec5bce1b34f10fd8ecb2617dbf611960d8b7d674515a880891",
    "f5f0ffc6ab6ffda5589293ae9736312a14d5ce9fcb46b7c7478ba8505f62701e",
    "7a2dd78bda122ff207c41c9eb320e64e0818284d0200845fda8292218e1d4bd2",
    "f3f2184b56f946c6275818ac52ec46744944aa53a6bc8b65d6c796db83529fee",
    "cfc35d18e7332fde265d77541e0fc0165a811854f7158046053adb9674f9e866",
}


def bad_refs(text: str) -> set[str]:
    """文本里出现了哪些黑名单名字：按单词（只留字母、转小写）取 sha256 来对，对上的回哈希（不回原文）。"""
    out = set()
    for w in re.findall(r"[a-z]+", text.lower()):
        d = hashlib.sha256(w.encode()).hexdigest()
        if d in BAD:
            out.add(d)
    return out


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def latest(p: str) -> str:
    """守门员提示词里「最新消息」那一段（提示词后面的说明文字里也有「别说了」，不能整段去找）。"""
    return p.split("【最新消息】", 1)[1].split("按一个真人群友的分寸", 1)[0]


def kinds(e):
    return [c["_kind"] for c in e.llm.calls if "_kind" in c]


def png(seed: int, tweak: int = 0) -> bytes:
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGB", (72, 72), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x0, y0 = rnd.randrange(60), rnd.randrange(60)
        d.rectangle((x0, y0, x0 + rnd.randrange(4, 40), y0 + rnd.randrange(4, 40)), fill=(rnd.randrange(256),) * 3)
    for i in range(tweak):  # 近似图：只改几个像素
        im.putpixel((i * 5 % 72, 71), (1, 2, 3))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ---------- 五、学表情包 ----------
def test_stickers_learn_only_common_ones_dedupe_and_cap(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        e.llm.vis = lambda content: {"sticker": True, "ok": True, "desc": "狗头，表示调侃"}
        a, b, c = png(1), png(2), png(3)
        for i, img in enumerate((a, b, c)):
            bot.imgs[f"http://img/{i}"] = img
        bot.imgs["http://img/a2"] = png(1, tweak=2)  # 和 a 近似
        e.llm.main = lambda s, u: "嗯"

        async def send(uid, url):
            await e.say(uid, "", images=[url])
            await e.engine.idle()

        def learned():
            return e.core.db.rows("SELECT * FROM stickers WHERE status='learned'")
        # 只发一次、一个人：不学
        await send("111", "http://img/0")
        assert not learned() and "vis" not in kinds(e)
        # 另一个人也发了（近似图算同一张）：多人发过，学
        await send("112", "http://img/a2")
        assert len(learned()) == 1 and kinds(e).count("vis") == 1
        row = learned()[0]
        assert row["descr"] == "狗头，表示调侃" and (Path(__import__("mb.stickers", fromlist=["x"]).Stickers.folder()) / row["file"]).exists()
        mem = e.core.db.rows("SELECT kind, content, status FROM memories WHERE kind='sticker'")
        assert mem == [{"kind": "sticker", "content": "狗头，表示调侃", "status": "active"}]
        # 同一个人发了很多次的也算「多次」
        for _ in range(3):
            await send("111", "http://img/1")
        assert len(learned()) == 2
        # 看图判断不宜内容：不学，而且记下来不再重复问
        e.llm.vis = lambda content: {"sticker": True, "ok": False, "desc": "不宜"}
        await send("111", "http://img/2")
        await send("112", "http://img/2")
        assert len(learned()) == 2
        n = kinds(e).count("vis")
        await send("113", "http://img/2")
        assert kinds(e).count("vis") == n
        # 每个群每天最多学几张
        e.llm.vis = lambda content: {"sticker": True, "ok": True, "desc": "又一张"}
        setg(e, "sticker_daily_learn", 2)
        bot.imgs["http://img/d"] = png(9)
        await send("111", "http://img/d")
        await send("112", "http://img/d")
        assert len(learned()) == 2
        setg(e, "sticker_daily_learn", 10)
        await send("113", "http://img/d")
        assert len(learned()) == 3
        # 容量上限：满了先删用得最少的
        st = e.engine.stickers
        e.core.db.x("UPDATE stickers SET uses=5 WHERE id=?", (learned()[0]["id"],))
        setg(e, "sticker_max_count", 2)
        st.trim(e.room())
        left = learned()
        assert len(left) == 2 and left[0]["uses"] == 5 or any(x["uses"] == 5 for x in left)
        assert not e.core.db.rows("SELECT 1 FROM memories m WHERE m.kind='sticker' AND m.id NOT IN (SELECT mem_id FROM stickers WHERE status='learned')")
        # 总容量上限
        setg(e, "sticker_cache_mb", 0)
        before = len(learned())
        st.trim(e.room())
        assert len(learned()) == before  # 0 = 不设上限
        e.core.settings.set("global", "sticker_cache_mb", 1)
        e.core.db.x("UPDATE stickers SET size=900000 WHERE status='learned'")
        st.trim(e.room())
        assert len(learned()) == 1
        # 关掉学习：不再下载、不再问
        setg(e, "sticker_learn", False)
        n = kinds(e).count("vis")
        await send("111", "http://img/0")
        await send("112", "http://img/0")
        assert kinds(e).count("vis") == n
    env.run(body)


def test_sticker_send_is_a_tool_and_counts_as_a_message(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "sticker_chance", 100)
        e.llm.vis = lambda content: {"sticker": True, "ok": True, "desc": "无语，翻白眼"}
        bot.imgs["http://img/0"] = png(1)
        for u in ("111", "112"):
            await e.say(u, "", images=["http://img/0"])
            await e.engine.idle()
        assert e.engine.stickers.count(e.engine.stickers.space(e.room())) == 1
        e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
        e.llm.main = lambda s, u: "行吧\n【表情1】"
        await e.say("111", "你觉得呢", at=["900"])
        assert bot.sent[-1][2] == "行吧" and len(bot.images) == 1 and bot.images[0][1] == "100"
        assert any(x.text.startswith("[表情包：无语") and x.bot for x in e.room().ctx)
        # 发一张算一条发言，计入每小时上限
        assert len(e.room().hourly["ac_900"]) == 1
        assert e.core.db.rows("SELECT uses FROM stickers WHERE status='learned'")[0]["uses"] == 1
        # 概率为 0：不给她这个工具（「被叫到时总给她表情包工具」关掉，被叫到也按概率）
        setg(e, "sticker_chance", 0)
        setg(e, "sticker_call", False)
        await e.say("111", "再问问", at=["900"])
        assert "tools" not in [c for c in e.llm.calls if c["_kind"] == "main"][-1]
        # 平台不能发图：自动跳过
        setg(e, "sticker_chance", 100)
        setg(e, "sticker_call", True)
        bot.supports_image = False
        await e.say("111", "还有呢", at=["900"])
        assert "tools" not in [c for c in e.llm.calls if c["_kind"] == "main"][-1]
        bot.supports_image = True
        # 影子模式只记不发
        setg(e, "shadow", True, "role:r_haitun")
        await e.say("111", "影子里问", at=["900"])
        assert len(bot.images) == 1
        assert e.core.db.rows("SELECT 1 FROM messages WHERE kind='shadow' AND text LIKE '[表情包%'")
    env.run(body)


# ---------- 六、发言占比 ----------
def test_share_limit_counts_all_bots_together(env):
    async def body(e):
        a, b = e.add_bot("900"), e.add_bot("901")
        eng = e.engine
        setg(e, "cooldown_seconds", 0)
        room = eng.room("qq", "100", "群100")
        room.accounts |= {"ac_900", "ac_901"}
        ba, bb = eng.bots["ac_900"], eng.bots["ac_901"]
        # 默认开（最近 15 条、上限 4 成）；这个测试要看「关掉就不限制」
        assert e.core.settings.g("share_limit_on") and e.core.settings.g("share_window") == 15 and e.core.settings.g("share_max") == 47
        setg(e, "share_limit_on", False)
        from mb.engine import Entry
        for i in range(30):
            room.ctx.append(Entry("111", "群友", f"话{i}"))
        for i in range(12):  # 两个机器人各 6 条 = 合计 12 / 42
            room.ctx.append(Entry("900" if i % 2 else "901", "bot", f"回{i}", bot="ac_900" if i % 2 else "ac_901"))
        assert eng.proactive_ok(ba, room, sample=False)
        setg(e, "share_limit_on", True)
        setg(e, "share_mode", "percent")  # 这个测试看的是按比例
        setg(e, "share_min_on", True)
        setg(e, "share_window", 40)
        setg(e, "share_max", 25)
        n_bot, n = pacing.share(room.ctx, 40)
        assert (n_bot, n) == (12, 40)  # 12/40 = 30% > 25%：两个机器人合计
        assert not eng.proactive_ok(ba, room, sample=False) and not eng.proactive_ok(bb, room, sample=False)
        setg(e, "share_max", 35)
        assert eng.proactive_ok(ba, room, sample=False)
        # 被@照回：占比只管主动插话
        setg(e, "share_max", 10)
        await e.say("111", "你们俩在吗", at=["900"])
        assert a.sent and a.sent[-1][2] == "收到"
        # 窗口里样本太少不算
        room.ctx.clear()
        room.ctx.append(Entry("900", "bot", "x", bot="ac_900"))
        assert eng.proactive_ok(ba, room, sample=False)
    env.run(body)


def test_share_window_is_independent_of_context_window():
    # 统计窗口和给模型看的上下文条数是两个设置，都可以按层继承
    from mb.settings import BY_KEY, G, P, C
    assert BY_KEY["share_window"]["default"] == 15 and BY_KEY["share_max"]["default"] == 47 and BY_KEY["share_limit_on"]["default"] is True and BY_KEY["context_window"]["default"] == 30
    assert BY_KEY["context_max_chars"]["default"] == 3000
    from mb.settings import ALL
    assert set(BY_KEY["share_window"]["scopes"]) == set(ALL) and set(BY_KEY["share_max"]["scopes"]) == set(ALL)


# ---------- 七、插话力度自动调节 ----------
def test_tune_bounds_recovery_and_strictest_wins(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        setg(e, "cooldown_seconds", 0)
        room = eng.room("qq", "100", "群100")
        b = eng.bots["ac_900"]
        setg(e, "eagerness", 3)
        assert eng.eager_level(b, room) == 3
        setg(e, "eager_auto", True)
        setg(e, "eager_auto_down", 2)
        setg(e, "eager_auto_up", 1)
        t = eng.tune(room, "ac_900")
        for _ in range(10):  # 一路被无视：最多比设定低 2 档，不会降到零
            t.apply("annoyed", 2, 1, "x")
        assert eng.eager_level(b, room) == 1 and abs(t.adj + 2.0) < 0.01
        for _ in range(10):
            t.apply("warm", 2, 1)
        assert eng.eager_level(b, room) == 4 and abs(t.adj - 1.0) < 0.01  # 上限比设定高 1 档
        # 会回升：时间久了自己往设定值回
        t.apply("annoyed", 2, 1)
        t.apply("annoyed", 2, 1)
        low = t.adj
        t.ts -= 3600 * 4
        assert t.level(3, 2, 1) >= 2 and t.adj > low
        t.ts -= 3600 * 20
        t.decay()
        assert t.adj == 0.0
        # 守门员顺带给反馈（同一次调用）：被无视→调低；有人接→回升
        e.llm.gate = lambda p: {"reply": True, "brief": "接", "feedback": "ignored" if "她1分钟前主动插过" in p else "unknown"}
        e.llm.main = lambda s, u: "我插一句"
        setg(e, "eagerness", 3)
        t.adj = 0.0
        await e.say("111", "随便聊聊天气")
        assert bot.sent[-1][2] == "我插一句"
        assert room.pro_pending["ac_900"]["text"] == "我插一句"
        setg(e, "cooldown_seconds", 0)
        n_gk = kinds(e).count("gk")
        await e.say("111", "没人理她的话题 另一件事")
        assert kinds(e).count("gk") == n_gk + 1  # 反馈是和守门员同一次调用里问的，没有多出调用
        assert t.adj < 0 and t.hist[-1][1] == "ignored"  # 上一次的反馈处理完了（这一条又是新的一次插话）
        info = eng.tune_info(b, room)
        assert info["base"] == 3 and info["level"] <= 3 and info["history"][-1]["feedback"] == "ignored"
        # 关掉自动调节：就是设定值
        setg(e, "eager_auto", False)
        assert eng.eager_level(b, room) == 3
    env.run(body)


def test_pacing_limits_stack_strictest_wins(env):
    async def body(e):
        e.add_bot("900")
        eng = e.engine
        setg(e, "cooldown_seconds", 0)
        room = eng.room("qq", "100", "群100")
        b = eng.bots["ac_900"]
        from mb.engine import Entry
        assert eng.proactive_ok(b, room, sample=False)
        setg(e, "hourly_limit_chat", 2)
        eng.mark_proactive(b, room)
        eng.mark_proactive(b, room)
        assert not eng.proactive_ok(b, room, sample=False)  # 每群每小时先到
        setg(e, "hourly_limit_chat", 90)
        assert eng.proactive_ok(b, room, sample=False)
        setg(e, "hourly_limit", 2)
        assert not eng.proactive_ok(b, room, sample=False)  # 账号级先到
        setg(e, "hourly_limit", 360)
        setg(e, "daily_limit", 2)
        assert not eng.proactive_ok(b, room, sample=False)  # 每天上限先到
        setg(e, "daily_limit", 600)
        room.pause_until = time.time() + 600
        assert not eng.proactive_ok(b, room, sample=False)  # 被叫停
        room.pause_until = 0
        setg(e, "share_limit_on", True)
        setg(e, "share_window", 10)
        setg(e, "share_max", 20)
        for i in range(10):
            room.ctx.append(Entry("900", "bot", "x", bot="ac_900"))
        assert not eng.proactive_ok(b, room, sample=False)  # 占比
    env.run(body)


# ---------- 八、「别说了」 ----------
def test_stop_is_judged_in_the_same_gatekeeper_call(env):
    async def body(e):
        a, b = e.add_bot("900"), e.add_bot("901")
        eng = e.engine
        setg(e, "cooldown_seconds", 0)
        setg(e, "stop_on", True)
        setg(e, "stop_minutes", 30)
        setg(e, "stop_max_minutes", 100)
        setg(e, "bot_chain_limit", 0)
        e.llm.main = lambda s, u: "好的"
        stop_now = {"v": False}
        e.llm.gate = lambda p: {"reply": True, "brief": "接", "stop": stop_now["v"] and "别说了" in latest(p)}
        e.llm.main = lambda s, u: "我来说一句"
        room = eng.room("qq", "100", "群100")
        room.accounts |= {"ac_900", "ac_901"}
        await e.say("111", "今天好热")
        assert a.sent or b.sent
        n_gk = kinds(e).count("gk")
        stop_now["v"] = True
        await e.say("111", "你们别说了")
        # 叫停由守门员判断，每个机器人只问了一次（同一次调用里），没有关键词匹配
        assert kinds(e).count("gk") - n_gk == 2
        assert room.pause_until > time.time() + 25 * 60  # 暂停 30 分钟
        sent = (len(a.sent), len(b.sent))
        stop_now["v"] = False
        await e.say("112", "继续聊点别的吧")
        assert (len(a.sent), len(b.sent)) == sent  # 该群所有机器人都暂停主动插话
        # 被@照样回，并且提前恢复
        await e.say("112", "你怎么看", at=["900"])
        assert len(a.sent) == sent[0] + 1 and room.pause_until == 0
        # 短时间内反复叫停：时长递增，有上限
        durs = []
        for _ in range(4):
            room.last_stop = 0
            stop_now["v"] = True
            await e.say("111", "别说了！")
            durs.append(round((room.pause_until - time.time()) / 60))
            room.pause_until = 0
            stop_now["v"] = False
        assert durs[0] in (59, 60) and durs[1] in (59, 60, 100, 99) and durs[-1] == 100
        # 关键词不算：没有守门员判断，说了也不暂停
        setg(e, "stop_on", False)
        room.stop_hist.clear()
        stop_now["v"] = True
        await e.say("111", "别说了")
        assert room.pause_until == 0
        # 管理员的「闭嘴」仍然更强：完全静音
        await e.say(HAITUN, "闭嘴 5", at=["900"], name="测试昵称")
        assert e.room().mute.get("ac_900", 0) > time.time()
    env.run(body)


HAITUN = "90001"


def test_observing_after_she_spoke_even_in_cooldown(env):
    async def body(e):
        a = e.add_bot("900")
        eng = e.engine
        setg(e, "cooldown_seconds", 600)  # 她刚说过话，还在冷却里
        setg(e, "stop_on", True)
        e.llm.gate = lambda p: {"reply": True, "brief": "接", "stop": "别说了" in latest(p)}
        e.llm.main = lambda s, u: "我插一句"
        await e.say("111", "今天好热")
        assert a.sent and kinds(e).count("gk") == 1
        await e.say("111", "别说了")  # 冷却中：不会插话，但要听得出叫停（同一次守门员调用，不多不少）
        assert kinds(e).count("gk") == 2 and e.room().pause_until > time.time()
        # 没开这两项时，冷却中完全不问守门员（和以前一样）
        setg(e, "stop_on", False)
        e.room().pause_until = 0
        await e.say("111", "又来一句")
        assert kinds(e).count("gk") == 2
    env.run(body)


# ---------- 用量硬上限 ----------
def test_usage_hard_cap_pauses_everything_and_resumes_next_day(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "memory_segment", 3)
        setg(e, "daily_token_limit", 1)  # 1 万 tokens
        e.llm.main = lambda s, u: "正常回复"
        # 只开软上限：用量到了，被@和私聊照常回
        e.core.db.add_usage("r_haitun", "qq", "qq:100", "main", "m", 20000, 0, 0)
        await e.say("111", "在吗", at=["900"])
        assert bot.sent[-1][2] == "正常回复"
        # 硬上限默认关；打开后：被@和私聊只回一句固定的话，不调模型
        setg(e, "usage_hard_cap", True)
        n = len(e.llm.calls)
        await e.say("111", "在吗2", at=["900"])
        assert bot.sent[-1][2] == "今天的额度用完了，明天再来" and len(e.llm.calls) == n
        await e.say("111", "私聊一下", private=True)
        assert bot.sent[-1][:3] == ("private", "111", "今天的额度用完了，明天再来") and len(e.llm.calls) == n
        # 一分钟内同一处不重复刷屏
        k = len(bot.sent)
        await e.say("111", "在吗3", at=["900"])
        assert len(bot.sent) == k
        # 按角色用自己的口吻
        setg(e, "hard_cap_message", "呜呜今天的豚豚电量耗尽啦，明天见~", "role:r_haitun")
        e.engine.cap_sent.clear()
        await e.say("111", "在吗4", at=["900"])
        assert bot.sent[-1][2] == "呜呜今天的豚豚电量耗尽啦，明天见~"
        # 所有调 API 的用途一律暂停：记忆整理、看图、搜索、学表情包、接口向量
        for i in range(6):
            await e.say("111", f"聊点什么{i}")
        await e.engine.idle()
        assert "mem" not in kinds(e) and len(e.llm.calls) == n
        setg(e, "vision_enabled", True)
        await e.say("111", "", images=["http://x"])
        assert len(e.llm.calls) == n
        assert (await e.engine.search.run("天气", "r_haitun", "qq", "qq:100")).startswith("搜索现在不能用")
        e.engine.embedder.cap_check = lambda: e.core.llm.hard_capped()
        from mb.embed import Paused
        e.core.db.set_kv("embedding", {"source": "api", "api_preset": "custom", "api_base": f"http://127.0.0.1:{e.port}/v4", "api_model": "x"})
        e.core.db.set_secret("embed:api", "k")
        try:
            await e.engine.embedder.embed(["你好"])
            raise AssertionError("应该暂停")
        except Paused:
            pass
        assert "embed" not in kinds(e)
        # 面板显示当前状态
        st = e.core.llm.hard_cap_state()
        assert st["on"] and st["active"] and st["limit"] == 10000 and st["used"] >= 20000
        # 第二天自动恢复（用量按日计）
        e.core.db.x("UPDATE usage SET day='2000-01-01'")
        assert not e.core.llm.hard_cap_state()["active"]
        e.engine.cap_sent.clear()
        await e.say("111", "新的一天", at=["900"])
        assert bot.sent[-1][2] == "正常回复"
        # 本机向量不受影响：硬上限只拦走 API 的
        e.core.db.set_kv("embedding", {"source": "local"})
    env.run(body)


def test_hard_cap_off_by_default_and_wizard_hint():
    from mb.settings import BY_KEY
    assert BY_KEY["usage_hard_cap"]["default"] is False and BY_KEY["daily_token_limit"]["default"] == 0
    assert BY_KEY["hard_cap_message"]["default"] == "今天的额度用完了，明天再来"
    assert "R" in BY_KEY["hard_cap_message"]["scopes"]
    wiz = (ROOT / "web" / "js" / "wizard.js").read_text(encoding="utf-8")
    assert "daily_token_limit" in wiz


# ---------- 署名与仓库地址 ----------
def test_attribution_and_single_repo_constant(env):
    import mb
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8").splitlines()
    assert lic[0] == "MIT License" and "Copyright (c) 2026 本昵称不代表本人" in lic[2]
    for f in ("README.md", "README.en.md"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert "本昵称不代表本人" in t and "https://space.bilibili.com/10228715" in t and "https://github.com/DChina2000/PodMates" in t
    iss = (ROOT / "installer" / "PodMates.iss").read_text(encoding="utf-8-sig")
    assert "AppPublisher=本昵称不代表本人" in iss and "AppName" in iss and "PodMates" in iss and "MultiBot" not in iss
    # 仓库名只写在一个常量里
    assert mb.REPO == "DChina2000/PodMates" and mb.REPO_URL == "https://github.com/DChina2000/PodMates" and mb.APP_NAME == "PodMates"
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "mb").rglob("*.py") if "DChina2000" in p.read_text(encoding="utf-8")]
    assert hits == [str(Path("mb/__init__.py"))]
    # 不写作者的邮箱或其他联系方式
    for p in [ROOT / "LICENSE", ROOT / "README.md", ROOT / "README.en.md", ROOT / "installer" / "PodMates.iss", ROOT / "web" / "js" / "pages-settings.js"]:
        t = p.read_text(encoding="utf-8-sig")
        assert "@gmail" not in t and "@qq.com" not in t, p

    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer
        from mb.server import Server
        srv = Server(e.core)
        c = TestClient(TestServer(srv.app))
        await c.start_server()
        j = await (await c.get("/api/about")).json()
        assert j["version"] == mb.VERSION and j["author"] == "本昵称不代表本人" and j["author_home"] == "https://space.bilibili.com/10228715"
        assert j["repo_url"] == mb.REPO_URL and j["license"] == "MIT" and "Permission is hereby granted" in j["license_text"]
        assert "NapCat" in j["third_party"]
        await c.close()
    env.run(body)


def test_repo_mentions_no_reference_projects():
    """仓库、文档里不提任何参考项目；THIRD_PARTY 只列程序实际依赖的库、模型和外部程序。
    黑名单只存名字（统一转小写）的 sha256，不在仓库里写名字原文。"""
    skip = {".venv", "build", "dist", ".git", "__pycache__"}
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or skip & set(p.relative_to(ROOT).parts) or p.name == "REPORT.md" or p.suffix in {".onnx", ".png", ".ico", ".zip", ".exe", ".woff2"}:
            continue
        if "assets/models" in p.as_posix() and p.name != "MANIFEST.json":
            continue
        try:
            t = p.read_text(encoding="utf-8-sig").lower()
        except (UnicodeDecodeError, OSError):
            continue
        found = bad_refs(t)
        if found:
            hits.append(f"{p.relative_to(ROOT)}: 命中 {len(found)} 个参考项目名（{', '.join(sorted(x[:8] for x in found))}）")
    assert not hits, hits
    third = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    assert "参考过的项目" not in third and "models.dev" in third


def test_legacy_database_name_is_migrated(tmp_path):
    """程序改名前的数据目录里只有 multibot.db：打开时改成 podmates.db，数据照旧；旧名的导出包也能导入。"""
    import asyncio
    import zipfile

    from mb.core import Core
    c = Core(tmp_path / "a")
    c.db.set_kv("marker", "旧数据")
    c.db.close()
    d = tmp_path / "b"
    d.mkdir()
    for suffix in ("", "-wal", "-shm"):
        f = tmp_path / "a" / f"podmates.db{suffix}"
        if f.exists():
            f.replace(d / f"multibot.db{suffix}")
    c2 = Core(d)
    assert c2.db.kv("marker") == "旧数据" and (d / "podmates.db").exists() and not (d / "multibot.db").exists()
    # 旧名的导出包
    z = tmp_path / "old.zip"
    c2.db.close()
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(d / "podmates.db", "multibot.db")
        zf.writestr("manifest.json", "{}")
    c3 = Core(tmp_path / "c")
    asyncio.run(c3.import_zip(z))
    assert c3.db.kv("marker") == "旧数据"
    c3.db.close()
