"""引擎：守门员插话流程、多角色同群协调与互接上限、管理指令与权限、撤回、影子模式、用量上限。"""
from mb.channels.base import Msg

HAITUN = "90001"


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


# ---------- 守门员插话流程 ----------
def test_gatekeeper_flow(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.gate = lambda p: {"reply": True, "brief": "接她妈的话"}
        e.llm.main = lambda s, u: "妈你又在摸鱼"
        await e.say(HAITUN, "今天吃啥", name="测试昵称")
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"]
        assert len(gk) == 1
        p = gk[0]["messages"][0]["content"]
        # 原模板只改两处：摘要换成角色的一句话摘要、〔海豚本人〕推广到人物表标记
        assert '【她】贴吧出身、嘴毒能对线的群友，看人用拉康的眼光。记录里"她（自己）"是她自己之前发的；名字带〔海豚本人〕的是她妈。' in p
        assert "测试昵称〔海豚本人〕: 今天吃啥" in p
        assert gk[0]["temperature"] == 0.2 and gk[0]["reasoning_effort"] == "high"
        main = [c for c in e.llm.calls if c["_kind"] == "main"][0]
        assert "没人@你，你决定主动插一句话。插话的由头：接她妈的话。" in main["messages"][1]["content"]
        assert main["temperature"] == 0.8 and main["reasoning_effort"] == "high"
        assert bot.sent[-1][2] == "妈你又在摸鱼" and bot.sent[-1][3] is None  # 主动插话不引用
        # 冷却 30 秒内：普通消息不再问守门员
        n = len(e.llm.calls)
        await e.say("111", "哈哈哈")
        assert len(e.llm.calls) == n
        # 但被@必回，而且引用对方那条
        await e.say("111", "你说啥", at=["900"])
        assert bot.sent[-1][2] == "妈你又在摸鱼" and bot.sent[-1][3] is not None
        assert "群友111 @了你，说：你说啥" in e.llm.calls[-1]["messages"][1]["content"]
        # 守门员说不插：不回
        setg(e, "cooldown_seconds", 0)
        e.llm.gate = lambda p: {"reply": False, "brief": "别人聊得正热"}
        before = len(bot.sent)
        await e.say("111", "随便说点")
        assert len(bot.sent) == before
        logs = e.core.db.rows("SELECT text FROM logs WHERE cat='gatekeeper'")
        assert any("不插（别人聊得正热）" in r["text"] for r in logs)
    env.run(body)


def test_wake_word_and_reply_count_as_at(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "wake_words", ["豚豚"], "role:r_haitun")
        setg(e, "proactive", False)
        await e.say("111", "豚豚在吗")
        assert len(bot.sent) == 1 and "群友111 叫了你" in e.llm.calls[-1]["messages"][1]["content"]
        await e.say("111", "对", reply_to_uid="900")
        assert len(bot.sent) == 2 and "回复了你" in e.llm.calls[-1]["messages"][1]["content"]
        await e.say("111", "没叫她")
        assert len(bot.sent) == 2
    env.run(body)


# ---------- 多角色同群协调 ----------
def test_multi_bot_coordination(env):
    async def body(e):
        e.add_role("r_乙", "乙角色")
        e.add_role("r_third", "第三个")
        a, b, c = e.add_bot("901"), e.add_bot("902", "r_乙"), e.add_bot("903", "r_third")
        setg(e, "cooldown_seconds", 0)
        setg(e, "share_limit_on", False)  # 这个测试要的是机器人互接，不是发言占比
        setg(e, "max_bots_per_message", 2)
        setg(e, "bot_chain_limit", 2)
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        n = [0]

        def main(system, user):
            n[0] += 1
            return f"第{n[0]}句"
        e.llm.main = main
        await e.say("111", "大家好")
        bot_msgs = [x for x in e.core.db.rows("SELECT text, kind FROM messages ORDER BY id") if x["kind"] == "bot"]
        # 第 0 轮最多 2 个机器人回，之后机器人互接最多 2 轮（每轮最多 2 个）
        assert len(bot_msgs) == 6
        assert e.room().chain == 2
        mains = [c for c in e.llm.calls if c["_kind"] == "main"]
        # 后说的能看到先说的
        assert "第1句" in mains[1]["messages"][1]["content"]
        # 同群的其他机器人自动进人物表，默认关系「姐妹」
        sys0 = mains[0]["messages"][0]["content"]
        assert "【姐妹】" in sys0
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"][0]["messages"][0]["content"]
        assert "的是她的姐妹（也是机器人）" in gk
        # 真人一开口，计数清零
        await e.say("112", "又来了")
        assert e.room().chain == 2 and len([x for x in e.core.db.rows("SELECT kind FROM messages") if x["kind"] == "bot"]) == 12
    env.run(body)


def test_bot_chain_limit_with_wake_words(env):
    async def body(e):
        e.add_role("r_乙", "乙角色")
        a, b = e.add_bot("901"), e.add_bot("902", "r_乙")
        setg(e, "wake_words", ["豚豚"], "role:r_haitun")
        setg(e, "wake_words", ["乙乙"], "role:r_乙")
        setg(e, "bot_chain_limit", 3)
        setg(e, "proactive", False)
        e.llm.main = lambda s, u: "乙乙你说呢" if "一只泡在" in s else "豚豚你说呢"
        await e.say("111", "问个事", at=["901"])
        # 海豚娘回 1 句，之后互相叫名字接话 3 轮就停
        assert len(a.sent) == 2 and len(b.sent) == 2
        assert e.room().chain == 3
    env.run(body)


# ---------- 管理指令与权限 ----------
def test_commands_and_permissions(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        e.llm.main = lambda s, u: "才不听你的" if "没有这个权限" in u else "嗯"
        # 作者（人物表管理员）闭嘴 5 分钟
        await e.say(HAITUN, "闭嘴5分钟", at=["900"])
        assert bot.sent[-1][2] == "好，闭嘴5分钟。"
        n = len(bot.sent)
        await e.say("111", "在吗", at=["900"])
        assert len(bot.sent) == n  # 闭嘴中，@也不回
        # 没权限的人发指令：用她自己的方式拒绝（调模型），不执行
        await e.say("111", "恢复", at=["900"])
        assert len(bot.sent) == n  # 闭嘴中连拒绝也不说
        await e.say(HAITUN, "恢复", at=["900"])
        assert bot.sent[-1][2] == "我回来了。"
        await e.say("111", "闭嘴", at=["900"])
        assert bot.sent[-1][2] == "才不听你的"
        assert e.room().mute.get("ac_900", 0) == 0
        # 改成「群主和群管理员」也能用
        setg(e, "cmd_perms", {"mute": {"mode": "admins"}})
        await e.say("112", "闭嘴 1 分钟", at=["900"], role="admin")
        assert bot.sent[-1][2] == "好，闭嘴1分钟。"
        await e.say(HAITUN, "恢复", at=["900"])
        # 「我加指定的人」
        e.core.db.put("person", {"id": "pe_x", "name": "小明", "qq": ["113"], "attitude": {}, "groups": ["g1"]})
        e.engine.reload()
        setg(e, "cmd_perms", {"proactive": {"mode": "people", "people": ["pe_x"]}, "eagerness": {"mode": "pgroup", "pgroup": "g1"}})
        await e.say("113", "插话关", at=["900"])
        assert e.core.settings.get("proactive", "r_haitun", "qq", "qq:100", "ac_900") is False
        await e.say("113", "插话力度 5", at=["900"])
        assert e.core.settings.get("eagerness", "r_haitun", "qq", "qq:100", "ac_900") == 5
        # 换人设、状态
        await e.say(HAITUN, "换人设 乙角色", at=["900"])
        assert bot.sent[-1][2].startswith("好，换成「乙角色」，到")  # 临时覆盖，不带时长到当天结束
        await e.say(HAITUN, "在吗", at=["900"])
        assert "你是乙角色。" in e.llm.calls[-1]["messages"][0]["content"]
        await e.say(HAITUN, "状态", at=["900"])
        assert bot.sent[-1][2].startswith("状态：正常；插话关，力度话痨；此刻是「海豚娘」（默认角色），人设「乙角色」")
        # 识别不了的按普通聊天回
        before = len(e.llm.calls)
        await e.say(HAITUN, "闭嘴吧你这个笨蛋", at=["900"])
        assert e.llm.calls[before]["_kind"] == "main" and "闭嘴吧你这个笨蛋" in e.llm.calls[before]["messages"][1]["content"]
    env.run(body)


# ---------- 撤回 ----------
def test_recall(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        e.llm.main = lambda s, u: "说错话了"
        await e.say("111", "嗨", at=["900"])
        mid = bot.sent[-1][4]
        assert any(x.text == "说错话了" for x in e.room().ctx)
        await e.say(HAITUN, "撤回", at=["900"])
        assert bot.recalled == [mid]
        assert not any(x.text == "说错话了" for x in e.room().ctx)
        # 面板上的撤回按钮：没有可撤回的
        assert await e.engine.recall_last(e.engine.bots["ac_900"], e.room()) is False
    env.run(body)


# ---------- 影子模式 ----------
def test_shadow_mode(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "shadow", True, "role:r_haitun")
        setg(e, "cooldown_seconds", 0)
        e.llm.gate = lambda p: {"reply": True, "brief": "有梗"}
        e.llm.main = lambda s, u: "本来想说这句"
        await e.say("111", "你好", at=["900"])
        await e.say("111", "有人吗")
        assert bot.sent == []  # 只读不发
        rows = e.core.db.rows("SELECT text FROM messages WHERE kind='shadow'")
        assert len(rows) == 2 and rows[0]["text"] == "本来想说这句"
        assert not any(x.bot for x in e.room().ctx)  # 影子说的话不进上下文
        logs = e.core.db.rows("SELECT text FROM logs WHERE cat='shadow'")
        assert any("本来会说：本来想说这句" in r["text"] for r in logs)
    env.run(body)


# ---------- 用量上限 ----------
def test_daily_usage_limit(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "cooldown_seconds", 0)
        setg(e, "daily_token_limit", 1)  # 1 万 tokens
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        e.core.db.add_usage("r_haitun", "qq", "qq:100", "main", "m", 9000, 0, 1000)
        n = len(e.llm.calls)
        await e.say("111", "没人@她")
        assert len(e.llm.calls) == n and bot.sent == []  # 到上限：不再主动插话（守门员都不问）
        await e.say("111", "那@你呢", at=["900"])
        assert len(bot.sent) == 1  # 被@照常回
        await e.say("111", "私聊一下", private=True)
        assert len(bot.sent) == 2 and bot.sent[-1][0] == "private"  # 私聊照常回
        # 角色单独的上限
        setg(e, "daily_token_limit", 0)
        setg(e, "daily_token_limit", 2, "role:r_haitun")
        assert e.engine.llm.over_limit("r_haitun") is False
        e.core.db.add_usage("r_haitun", "qq", "qq:100", "main", "m", 10000, 0, 0)
        assert e.engine.llm.over_limit("r_haitun") is True
    env.run(body)


def test_backup_endpoint_on_error(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "error_notice", True)  # 默认不在群里发报错，这里打开
        e.llm.fail_next = 2  # 429 会自动重试一次，所以要连着失败两次才算出错
        await e.say("111", "在吗", at=["900"])
        assert bot.sent[-1][2].startswith("（断线了")  # 没开备用：报错
        setg(e, "backup_enabled", True)
        setg(e, "backup_endpoint", "backup")
        e.llm.fail_next = 2
        e.llm.main = lambda s, u: "备用端点回的"
        await e.say("111", "再试", at=["900"])
        assert bot.sent[-1][2] == "备用端点回的"
    env.run(body)


def test_private_chat_and_split(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.main = lambda s, u: "第一段话比较长，说了很多很多。\n第二段也不短，接着说。\n第三段收尾。"
        await e.say(HAITUN, "妈妈来了", private=True)
        assert [x[2] for x in bot.sent] == ["第一段话比较长，说了很多很多。", "第二段也不短，接着说。", "第三段收尾。"]  # 每一行单独一条
        sysmsg = e.llm.calls[-1]["messages"][0]["content"]
        assert sysmsg.endswith("这次私聊的是海豚本人。") and "【她妈】" in sysmsg
        setg(e, "private_scope", "list")
        n = len(bot.sent)
        await e.say("555", "陌生人私聊", private=True)
        assert len(bot.sent) == n
    env.run(body)


def test_blocked_words_blacklist_repeat(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "blocked_words", ["敏感"])
        setg(e, "blacklist", ["444"])
        e.llm.main = lambda s, u: "这个敏感词不能说"
        await e.say("111", "说点敏感的", at=["900"])
        assert bot.sent == []  # 含屏蔽词的消息不理
        await e.say("111", "说点别的", at=["900"])
        assert bot.sent[-1][2] == "这个□□词不能说"  # 她不说屏蔽词
        await e.say("444", "我是黑名单", at=["900"])
        assert len(bot.sent) == 1
        setg(e, "repeat_follow", True)
        await e.say("111", "+1")
        await e.say("112", "+1")
        assert bot.sent[-1][2] == "+1"
    env.run(body)


def test_shadow_mode_sends_nothing_even_on_model_error(env):
    """影子模式：群里、私聊里，连「模型出错」的抱歉都不发（真号演练时不能往真群里发任何东西）。"""
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "shadow", True)
        setg(e, "proactive", False)
        e.llm.fail_next = 3
        await e.say("111", "在吗", at=["900"])
        await e.say("111", "私聊在吗", private=True)
        e.llm.fail_next = 0
        await e.say("111", "再来", at=["900"])
        await e.say("111", "再私聊", private=True)
        assert bot.sent == []  # 一条都没发
        assert e.core.db.x("SELECT COUNT(*) FROM logs WHERE cat='shadow'").fetchone()[0] >= 2
    env.run(body)
