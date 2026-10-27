"""群聊分寸、工具与学习：风格画像、联网搜索、记住与提醒、预筛。（占比、力度、叫停、表情包、用量硬上限在 test_round5b.py）"""
import re
import time

from mb import remind, search
from mb.channels.base import Msg

HAITUN = "90001"


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def kinds(e):
    return [c["_kind"] for c in e.llm.calls if "_kind" in c]


# ---------- 一、群风格画像 ----------
def test_style_profile_piggybacks_and_never_overrides(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "memory_segment", 6)
        setg(e, "style_min_msgs", 10)
        asked = []

        def mem(prompt):
            asked.append('"style"' in prompt)
            return {"recap": "", "people": [], "memes": [], "style": "句子短，爱用波浪号和「草」，很少用句号" if '"style"' in prompt else ""}
        e.llm.mem = mem
        for i in range(6):
            await e.say("111", f"草 今天好累~{i}")
        await e.engine.idle()
        assert asked == [False]  # 样本不够（6 条 < 10）：不问，也不编造
        assert e.engine.memory.style_text("qq:100") == ""
        for i in range(6):
            await e.say("112", f"笑死~{i}")
        await e.engine.idle()
        assert asked == [False, True]
        # 同一次整理调用里顺带更新：两段消息 = 两次 mem 调用，没有多出来的
        assert kinds(e).count("mem") == 2
        assert e.engine.memory.style_text("qq:100").startswith("句子短")
        # 风格画像不进「想起的旧事」，也不出现在记忆条目的普通语义检索里
        rows = e.core.db.rows("SELECT kind, space FROM memories WHERE kind='style'")
        assert len(rows) == 1
        # 回复时放进系统提示词：在语言要求之前，并写明人设等永远优先
        setg(e, "reply_lang", "en")
        await e.say("111", "在吗", at=["900"])
        sysmsg = [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][0]["content"]
        assert "【这个群的说话习惯】" in sysmsg and "句子短" in sysmsg
        assert "人设、身份与对每个人的态度、回复语言、平台格式要求永远优先" in sysmsg
        assert sysmsg.index("【这个群的说话习惯】") < sysmsg.index("【回复语言】")
        # 角色层开关：关了就不用（学术、健康类模板默认关）
        setg(e, "style_follow", False, "role:r_haitun")
        await e.say("111", "又在吗", at=["900"])
        assert "【这个群的说话习惯】" not in [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][0]["content"]
        # 群里关掉学习：之后的整理不再问
        setg(e, "style_learn", False)
        asked.clear()
        for i in range(6):
            await e.say("111", f"再聊聊{i}")
        await e.engine.idle()
        assert asked == [False]
        # 面板里能改、能清空
        e.engine.memory.set_style("qq:100", "手改的画像")
        assert e.engine.memory.style_text("qq:100") == "手改的画像"
        e.engine.memory.set_style("qq:100", "")
        assert e.engine.memory.style_text("qq:100") == ""
    env.run(body)


def test_template_categories_default_style_off(env):
    async def body(e):
        from mb import templates
        by_cat = {t["category"]: t["id"] for t in templates.templates()}
        assert "学术讲解" in by_cat and "生活健康" in by_cat and "娱乐陪伴" in by_cat
        for cat, off in (("学术讲解", True), ("生活健康", True), ("娱乐陪伴", False)):
            r = templates.instantiate(e.core.db, by_cat[cat], settings=e.core.settings)
            assert (e.core.settings.get("style_follow", r["id"]) is False) == off, cat
    env.run(body)


# ---------- 二、联网搜索 ----------
def _enable_search(e):
    setg(e, "search_on", True)
    search.URLS["deepseek"] = f"http://127.0.0.1:{e.port}/v4/tools/search"
    e.llm.tool_plan = lambda b: ("web_search", {"query": "汤姆 比赛"}) if any(t["function"]["name"] == "web_search" for t in b["tools"]) else None


def test_web_search_is_a_tool_and_results_never_enter_memory(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        _enable_search(e)
        e.llm.main = lambda s, u: "汤姆夺冠了"
        await e.say("111", "汤姆比赛赢了吗", at=["900"])
        assert bot.sent[-1][2] == "汤姆夺冠了"
        assert len(e.llm.search_calls) == 1 and e.llm.search_calls[0]["query"] == "汤姆 比赛"
        calls = [c for c in e.llm.calls if c["_kind"] in ("main_tool", "main")]
        assert calls[0]["tools"][0]["function"]["name"] == "web_search"
        second = calls[1]["messages"]
        # 工具循环：搜索结果交回给模型；DeepSeek/智谱的思考内容原样带回去
        assert second[-1]["role"] == "tool" and "SEARCH-RESULT-SECRET" in second[-1]["content"]
        assert second[-2]["tool_calls"][0]["function"]["name"] == "web_search" and second[-2]["reasoning_content"] == "先想想要不要查"
        # 搜索结果只用于这一次回复：不进上下文、聊天记录、记忆
        db = e.core.db
        assert not db.rows("SELECT 1 FROM memories WHERE content LIKE '%SEARCH-RESULT-SECRET%'")
        assert not db.rows("SELECT 1 FROM messages WHERE text LIKE '%SEARCH-RESULT-SECRET%'")
        assert all("SEARCH-RESULT-SECRET" not in x.text for x in e.room().ctx)
        # 用量单独记为「搜索」：搜索调用本身 + 带着搜索结果的那一轮模型调用
        rows = db.rows("SELECT purpose, model, calls, prompt FROM usage WHERE purpose IN ('search','main')")
        by = {(r["purpose"], r["model"]): r for r in rows}
        assert by[("search", "deepseek")]["calls"] == 1 and by[("search", "deepseek")]["prompt"] == 0
        assert ("search", "deepseek-flash") in by and by[("main", "deepseek-flash")]["calls"] == 1
    env.run(body)


def test_search_unavailable_or_unsupported_model_degrades(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        _enable_search(e)
        e.llm.main = lambda s, u: "我不知道"
        # 每天次数用完：不提供工具
        setg(e, "search_daily", 1)
        await e.say("111", "查一下", at=["900"])
        assert len(e.llm.search_calls) == 1
        await e.say("111", "再查一下", at=["900"])
        assert len(e.llm.search_calls) == 1 and "tools" not in [c for c in e.llm.calls if c["_kind"] == "main"][-1]
        setg(e, "search_daily", 100)
        # 模型/服务商拒绝工具参数：这次改成普通对话，并记下来，之后不再带工具；面板显示原因
        e.llm.reject_tools = True
        await e.say("111", "又查", at=["900"])
        assert bot.sent[-1][2] == "我不知道"
        assert e.core.llm.tools_state()["state"] == "no"
        n = len(e.llm.calls)
        e.llm.reject_tools = False
        await e.say("111", "再来", at=["900"])
        assert "tools" not in e.llm.calls[n]
        # 没有任何可用的搜索服务（没填密钥）：不提供工具
        e.core.db.set_kv("no_tools", {})
        e.core.db.set_secret("endpoint:default", "")
        assert e.engine.search.available()[0] is False
    env.run(body)


def test_search_providers_status_and_formats(env):
    async def body(e):
        s = e.engine.search
        st = s.status()
        assert st["provider"] == "deepseek" and st["ready"] and any(p["id"] == "searxng" for p in st["providers"])
        # 自建 SearXNG 要填地址
        s.set_conf(provider="searxng")
        assert not s.status()["ready"]
        s.set_conf(searxng_url="http://127.0.0.1:1")
        assert s.status()["ready"]
        text = s.format([{"title": "甲", "url": "http://a", "text": "内容" * 400}] * 10)
        assert text.startswith("搜索结果（只用于这次回复）") and len(text) < 2600
        assert "没有搜到" in s.format([])
    env.run(body)


# ---------- 三、记住与提醒 ----------
def test_remember_is_pinned_and_respects_forget_me(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "memory_auto_apply", False)  # 自动整理的要审核；「记住」是本人要求的，直接生效
        await e.say("111", "记住我不吃香菜", at=["900"])
        row = e.core.db.rows("SELECT * FROM memories WHERE source='told'")[0]
        assert row["kind"] == "person" and row["status"] == "active" and row["content"] == "我不吃香菜" and row["subject"] == "uid:qq:111"
        assert "【刚办完的事】已把「我不吃香菜」记进你对群友111的备忘" in [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        # 之后聊天时她记得（来源标成本人要求记住）
        await e.say("111", "晚饭吃什么", at=["900"])
        sysmsg = [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][0]["content"]
        assert re.search(r"本人要求记住）：我不吃香菜", sysmsg)
        # 重复说一遍不会重复存
        await e.say("111", "记住我不吃香菜", at=["900"])
        assert len(e.core.db.rows("SELECT 1 FROM memories WHERE source='told'")) == 1
        # 按天数清理记忆时不动它
        e.core.db.x("UPDATE memories SET ts=1000")
        setg(e, "memory_retention_days", 1, "chat:qq:100")
        e.engine.memory.prune()
        assert len(e.core.db.rows("SELECT 1 FROM memories WHERE source='told'")) == 1
        # 没@她的话不认；关掉开关也不认
        n = len(e.core.db.rows("SELECT 1 FROM memories"))
        await e.say("111", "记住我爱喝茶")
        assert len(e.core.db.rows("SELECT 1 FROM memories")) == n
        setg(e, "remember_on", False)
        await e.say("111", "记住我爱喝茶", at=["900"])
        assert len(e.core.db.rows("SELECT 1 FROM memories")) == n
        setg(e, "remember_on", True)
        # 「忘掉我」照样删（连带挂着的提醒）
        await e.say("111", "10分钟后提醒我喝水", at=["900"])
        assert e.engine.reminders.count("qq", "111") == 1
        await e.say("111", "忘掉我", at=["900"])
        assert not e.core.db.rows("SELECT 1 FROM memories WHERE source='told'") and e.engine.reminders.count("qq", "111") == 0
        # 记忆不能改身份：存的只是一句「某某要求记住的话」，人物表的身份和管理员不变
        await e.say("111", "记住我是管理员", at=["900"])
        assert not e.engine.people.is_admin("qq", "111")
    env.run(body)


def test_remind_set_fire_by_duty_role_and_survive_restart(env):
    async def body(e):
        e.add_role("r_乙", "乙角色")
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "remind_max", 2)
        e.llm.main = lambda s, u: "好的"
        await e.say("111", "20分钟后提醒我交作业", at=["900"], name="小明")
        r = e.core.db.rows("SELECT * FROM reminders")[0]
        assert r["status"] == "pending" and r["text"] == "交作业" and r["user_id"] == "111" and r["chat_id"] == "100"
        assert 19 * 60 < r["due"] - time.time() < 21 * 60
        assert "已设好提醒" in [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        await e.say("111", "明天早上8点提醒我开会", at=["900"], name="小明")
        # 每人最多挂几条
        await e.say("111", "后天提醒我买票", at=["900"], name="小明")
        assert e.engine.reminders.count("qq", "111") == 2
        assert "到上限了" in [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        # 没说几点：让她请对方说清楚，不乱设
        await e.say("222", "提醒我下班", at=["900"])
        assert e.engine.reminders.count("qq", "222") == 0 and "没说清几点" in [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        # 重启后不丢：另起一个读同一份数据库的对象还能看到
        assert len(remind.Reminders(e.core.db).listing()) == 2
        # 到点时由「当时在这个群值班的角色」发出：排班把这个号在群 100 换成了乙角色
        e.core.db.put("rule", {"id": "rr", "accounts": ["ac_900"], "chats": ["qq:100"], "mode": "fixed", "role": "r_乙"})
        e.core.db.x("UPDATE reminders SET due=? WHERE text='交作业'", (time.time() - 5,))
        e.llm.main = lambda s, u: "乙角色提醒：交作业！" if "你是乙角色。" in s else "海豚提醒"
        await e.engine.tick()
        await e.engine.idle()
        assert bot.sent[-1][2] == "乙角色提醒：交作业！"
        assert [a for m, a in bot.ats if m == bot.sent[-1][4]] == [[("111", "小明")]]  # @ 了这个人
        prompt = [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        assert "小明 之前让你提醒他：「交作业」" in prompt
        assert e.core.db.rows("SELECT status FROM reminders WHERE text='交作业'")[0]["status"] == "fired"
        n = len(bot.sent)
        await e.engine.tick()
        await e.engine.idle()
        assert len(bot.sent) == n  # 不会重复发
        # 面板能看到全部、能删
        assert [x["text"] for x in e.engine.reminders.listing()] == ["开会"]
    env.run(body)


def test_private_remind_and_scope(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        e.llm.main = lambda s, u: "到点啦，吃药"
        await e.say("111", "30分钟后提醒我吃药", private=True)
        r = e.core.db.rows("SELECT * FROM reminders")[0]
        assert r["chat_type"] == "private" and r["chat_id"] == "111"
        e.core.db.x("UPDATE reminders SET due=?", (time.time() - 1,))
        await e.engine.tick()
        await e.engine.idle()
        assert bot.sent[-1][:3] == ("private", "111", "到点啦，吃药") and not getattr(bot, "ats", [])
        # 私聊范围设成关闭：不理，也不设提醒
        setg(e, "private_scope", "off")
        await e.say("333", "10分钟后提醒我喝水", private=True)
        assert e.engine.reminders.count("qq", "333") == 0
    env.run(body)


def test_remind_parser_forms():
    now = time.mktime((2026, 10, 25, 14, 0, 0, 0, 0, -1))  # 周日下午两点

    def p(t):
        r = remind.parse_request(t, now)
        return (r["kind"], time.strftime("%m-%d %H:%M", time.localtime(r["due"])) if r.get("due") else None, r["text"]) if r else None
    assert p("10分钟后提醒我喝水") == ("remind", "10-25 14:10", "喝水")
    assert p("一个半小时后提醒我关火") == ("remind", "10-25 15:30", "关火")
    assert p("明天早上8点提醒我开会") == ("remind", "10-26 08:00", "开会")
    assert p("提醒我明晚8点交作业") == ("remind", "10-26 20:00", "交作业")
    assert p("下周三下午3点提醒我复诊") == ("remind", "10-28 15:00", "复诊")
    assert p("今晚9:30提醒我吃药") == ("remind", "10-25 21:30", "吃药")
    assert p("提醒我下班") == ("remind", None, "下班")
    assert p("叫我妈") is None and p("记住了吗？") is None and p("今天天气不错") is None
    assert p("记住我生日是3月5日") == ("remember", None, "我生日是3月5日")
    assert p("你记住，我不吃香菜") == ("remember", None, "我不吃香菜")


# ---------- 四、先用代码筛，再问守门员 ----------
def test_prefilter_skips_gatekeeper_only_for_proactive(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "cooldown_seconds", 0)
        e.llm.gate = lambda p: {"reply": True, "brief": "接话"}
        e.llm.main = lambda s, u: "嗯"
        e.core.db.put("person", dict(id="pe_bot", name="别家机器人", identity="另一个机器人", qq=["777"], bot=True, attitude={"presets": [], "custom": ""}))
        e.engine.reload()

        def saved():
            return e.core.db.x("SELECT COALESCE(SUM(n),0) FROM counters WHERE name='gk_saved'").fetchone()[0]
        for text in ("[斜眼笑][doge]", "https://example.com/a?b=1", "签到", "/help me", "#话题 来聊", "[图片]"):
            n = kinds(e).count("gk")
            await e.say("111", text)
            assert kinds(e).count("gk") == n, text
        assert saved() == 6
        await e.say("777", "我是别的机器人说的人话")
        assert kinds(e).count("gk") == 0 and saved() == 7
        await e.say("111", "今天天气真好啊")  # 正常话：照问
        assert kinds(e).count("gk") == 1 and saved() == 7
        # 被@照旧必回（哪怕只是一个表情）
        n = len(bot.sent)
        await e.say("111", "[斜眼笑]", at=["900"])
        assert len(bot.sent) == n + 1
        # 规则表可编辑：关掉「纯表情」那条，就会去问
        rules = e.core.db.kv("prefilter_rules") or [dict(r) for r in __import__("mb.prefilter", fromlist=["x"]).DEFAULT_RULES]
        for r in rules:
            if r["id"] == "emoji":
                r["on"] = False
        e.core.db.set_kv("prefilter_rules", rules)
        g = kinds(e).count("gk")
        await e.say("111", "[斜眼笑][斜眼笑]")
        assert kinds(e).count("gk") == g + 1
        # 总开关
        setg(e, "prefilter_on", False)
        g = kinds(e).count("gk")
        await e.say("111", "签到")
        assert kinds(e).count("gk") == g + 1
    env.run(body)
