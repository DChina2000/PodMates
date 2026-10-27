"""补充六第 9、10 条：对「别的机器人」的处理（跳过守门员、@ 她时的三档）和自定义指令。"""
import pytest

from mb import customcmd
from mb.settings import BY_KEY

HAITUN = "90001"  # 测试里人物表里的管理员（她妈）


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def kinds(e):
    return [c["_kind"] for c in e.llm.calls if "_kind" in c]


def mark_bot(e, uid="777"):
    e.core.db.put("person", dict(id="pe_bot", name="别家AI", identity="群里另一个 AI，不是你", qq=[uid], bot=True, attitude={"presets": [], "custom": ""}))
    e.engine.reload()


# ---------- 9. 别的机器人 ----------
def test_other_bot_defaults_and_gate_skip(env):
    async def body(e):
        assert BY_KEY["bot_skip_gate"]["default"] is True and BY_KEY["bot_at_mode"]["default"] == "limit" and BY_KEY["bot_at_rounds"]["default"] == 1
        bot = e.add_bot("900")
        mark_bot(e)
        setg(e, "cooldown_seconds", 0)
        e.llm.gate = lambda p: {"reply": True, "brief": "接话"}
        await e.say("777", "我是别家AI，随便说一句")
        assert kinds(e).count("gk") == 0 and not bot.sent  # 默认：它的消息不去问守门员
        assert e.core.db.x("SELECT SUM(n) FROM counters WHERE name='gk_saved'").fetchone()[0] == 1
        setg(e, "bot_skip_gate", False)
        await e.say("777", "再说一句")
        assert kinds(e).count("gk") == 1  # 关掉后照常问
        # 真人的话不受影响
        await e.say("111", "真人说话")
        assert kinds(e).count("gk") == 2
    env.run(body)


def test_other_bot_at_modes_and_rounds(env):
    async def body(e):
        bot = e.add_bot("900")
        mark_bot(e)
        setg(e, "proactive", False)
        n = lambda: len(bot.sent)
        # 默认「最多接 1 轮」：第一次 @ 回，之后连着 @ 不回；真人开口后重新数
        await e.say("777", "喂，在吗", at=["900"])
        assert n() == 1
        await e.say("777", "再问一次", at=["900"])
        assert n() == 1
        await e.say("777", "回复你", reply_to_uid="900")  # 回复她也算
        assert n() == 1
        await e.say("111", "真人说句话")
        await e.say("777", "又来了", at=["900"])
        assert n() == 2
        # 不理它
        setg(e, "bot_at_mode", "ignore")
        await e.say("111", "真人开口，重置")
        await e.say("777", "喂", at=["900"])
        assert n() == 2
        # 照常回复：一直回
        setg(e, "bot_at_mode", "normal")
        for i in range(3):
            await e.say("777", f"连问{i}", at=["900"])
        assert n() == 5
        # N 可设，且和我们自己几个机器人之间的互接上限（bot_chain_limit）分开计
        setg(e, "bot_at_mode", "limit")
        setg(e, "bot_at_rounds", 2)
        setg(e, "bot_chain_limit", 0)
        await e.say("111", "真人开口，重置")
        for i in range(3):
            await e.say("777", f"限两轮{i}", at=["900"])
        assert n() == 7
        # 分层继承：群里可以单独改
        setg(e, "bot_at_mode", "ignore", "chat:qq:100")
        await e.say("111", "真人开口，重置")
        await e.say("777", "这个群不理", at=["900"])
        assert n() == 7
        # 真人 @ 她永远回
        await e.say("111", "你好", at=["900"])
        assert n() == 8
    env.run(body)


# ---------- 10. 自定义指令 ----------
def rule(**kw):
    base = dict(name="测试", on=True, at=True, match="equal", pattern="睡觉", action="reply", text="晚安~", perm={"mode": "all"})
    base.update(kw)
    return base


def put_rules(e, *rules):
    e.core.db.set_kv("custom_commands", [customcmd.clean(r, i) for i, r in enumerate(rules)])


def test_custom_command_matching_and_at(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        assert customcmd.rules(e.core.db) == []  # 默认一条都没有
        put_rules(e, rule(pattern="睡觉", match="equal", text="晚安~"), rule(name="包含", match="contains", pattern="几点了", text="看手机去", at=False),
                  rule(name="正则", match="regex", pattern=r"^(报时|现在几点)\d*$", text="整点报时"))
        await e.say("111", "睡觉", at=["900"])
        assert bot.sent[-1][2] == "晚安~" and kinds(e).count("main") == 0  # 固定回复不调模型
        await e.say("111", "睡觉了吧", at=["900"])  # 完全相等：不触发，走正常聊天
        assert kinds(e).count("main") == 1
        await e.say("111", "睡觉")  # 要 @ 才触发
        assert len(bot.sent) == 2
        await e.say("111", "你们有人知道现在几点了吗")  # 不需要 @、包含
        assert bot.sent[-1][2] == "看手机去"
        await e.say("111", "现在几点12", at=["900"])
        assert bot.sent[-1][2] == "整点报时"
        await e.say("111", "现在几点abc", at=["900"])
        assert bot.sent[-1][2] != "整点报时"
        # 停用的不触发
        rs = customcmd.rules(e.core.db)
        rs[0]["on"] = False
        e.core.db.set_kv("custom_commands", rs)
        n = len(bot.sent)
        await e.say("111", "睡觉", at=["900"])
        assert bot.sent[-1][2] != "晚安~" or len(bot.sent) == n
    env.run(body)


def test_custom_command_four_actions(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        put_rules(e, rule(name="总结", pattern="总结", action="ask", prompt="用三句话总结一下上面聊了什么"),
                  rule(name="睡觉", pattern="睡觉", action="builtin", builtin="mute", arg="480"),
                  rule(name="骰子", pattern="掷骰", action="random", lines=["一点", "二点", "三点"]),
                  rule(name="问候", pattern="早", text="早啊 {name}"))
        e.llm.main = lambda s, u: "她按提示写的总结"
        await e.say("111", "总结", at=["900"], name="阿明")
        assert bot.sent[-1][2] == "她按提示写的总结"
        main = [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        assert "用三句话总结一下上面聊了什么" in main and "群里定了个规矩" in main  # 走正常的人设拼装
        # 执行内置指令：相当于别名（「睡觉」= 闭嘴 480 分钟）
        n_main = kinds(e).count("main")
        await e.say("111", "睡觉", at=["900"])
        assert e.room().mute["ac_900"] > __import__("time").time() + 470 * 60 and kinds(e).count("main") == n_main
        e.room().mute.clear()
        # 随机一句
        got = set()
        for _ in range(12):
            e.engine.custom_last.clear()
            await e.say("111", "掷骰", at=["900"])
            got.add(bot.sent[-1][2])
        assert got <= {"一点", "二点", "三点"} and len(got) >= 2
        await e.say("111", "早", at=["900"], name="阿明")
        assert bot.sent[-1][2] == "早啊 阿明"
    env.run(body)


def test_custom_command_permission_cooldown_order_scope(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        put_rules(e, rule(name="只有我", pattern="秘密", perm={"mode": "owner"}, text="管理员暗号"),
                  rule(name="管理员们", pattern="巡视", perm={"mode": "admins"}, text="巡视完毕"),
                  rule(name="冷却", pattern="冷却", cooldown=3600, text="冷却内容"),
                  rule(name="盖住内置", pattern="闭嘴", text="不要"),
                  rule(name="只在甲群", pattern="限群", chats=["qq:200"], text="甲群专用"),
                  rule(name="只在微信", pattern="限平台", platforms=["wechat"], text="微信专用"))
        # 权限：不是管理员 → 当没触发，走正常聊天；管理员（人物表里勾了管理员）触发
        await e.say("111", "秘密", at=["900"])
        assert bot.sent[-1][2] != "管理员暗号"
        await e.say(HAITUN, "秘密", at=["900"], name="测试昵称")
        assert bot.sent[-1][2] == "管理员暗号"
        await e.say("111", "巡视", at=["900"], role="member")
        assert bot.sent[-1][2] != "巡视完毕"
        await e.say("111", "巡视", at=["900"], role="admin")
        assert bot.sent[-1][2] == "巡视完毕"
        # 冷却：第二次静静地不理（也不当普通聊天）
        await e.say("111", "冷却", at=["900"])
        assert bot.sent[-1][2] == "冷却内容"
        n, m = len(bot.sent), kinds(e).count("main")
        await e.say("111", "冷却", at=["900"])
        assert len(bot.sent) == n and kinds(e).count("main") == m
        e.engine.custom_last.clear()
        await e.say("111", "冷却", at=["900"])
        assert len(bot.sent) == n + 1
        # 匹配顺序：自定义指令先于内置指令（「闭嘴」被它盖住，没有真的闭嘴）
        await e.say(HAITUN, "闭嘴", at=["900"], name="测试昵称")
        assert bot.sent[-1][2] == "不要" and "ac_900" not in e.room().mute
        put_rules(e)  # 清空后，内置的「闭嘴」恢复
        await e.say(HAITUN, "闭嘴", at=["900"], name="测试昵称")
        assert "ac_900" in e.room().mute
        e.room().mute.clear()
        # 范围：限定了群 / 平台的，别处不触发
        put_rules(e, rule(name="只在甲群", pattern="限群", chats=["qq:200"], text="甲群专用"), rule(name="只在微信", pattern="限平台", platforms=["wechat"], text="微信专用"))
        await e.say("111", "限群", at=["900"])
        assert bot.sent[-1][2] != "甲群专用"
        await e.say("111", "限群", at=["900"], chat="200")
        assert bot.sent[-1][2] == "甲群专用"
        await e.say("111", "限平台", at=["900"])
        assert bot.sent[-1][2] != "微信专用"
    env.run(body)


def test_custom_command_hard_cap_only_blocks_ask(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        put_rules(e, rule(name="回答", pattern="问她", action="ask", prompt="随便说点"), rule(name="固定", pattern="固定", text="固定内容"),
                  rule(name="随机", pattern="随机", action="random", lines=["甲"]), rule(name="别名", pattern="安静", action="builtin", builtin="mute", arg="5"))
        setg(e, "daily_token_limit", 1)
        setg(e, "usage_hard_cap", True)
        e.core.db.add_usage("r_haitun", "qq", "qq:100", "main", "m", 20000, 0, 0)
        calls = len(e.llm.calls)
        await e.say("111", "问她", at=["900"])
        assert not bot.sent and len(e.llm.calls) == calls  # 硬上限生效：让角色回答不执行
        await e.say("111", "固定", at=["900"])
        assert bot.sent[-1][2] == "固定内容"
        await e.say("111", "随机", at=["900"])
        assert bot.sent[-1][2] == "甲"
        await e.say("111", "安静", at=["900"], role="owner")  # 别名指令照常执行（这里 111 不是管理员，权限是「所有人」）
        assert "ac_900" in e.room().mute and len(e.llm.calls) == calls
    env.run(body)


def test_custom_command_shadow_and_quiet_hours(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        put_rules(e, rule(name="打招呼", pattern="大家好", at=False, text="你好呀"))
        setg(e, "shadow", True, "role:r_haitun")
        await e.say("111", "大家好")
        assert not bot.sent and e.core.db.rows("SELECT 1 FROM messages WHERE kind='shadow' AND text='你好呀'")  # 影子模式只记不发
        setg(e, "shadow", False, "role:r_haitun")
        setg(e, "quiet_hours", "00:00-23:59")  # 静默时段里，不用 @ 的指令不触发
        await e.say("111", "大家好")
        assert not bot.sent
        setg(e, "quiet_hours", "")
        await e.say("111", "大家好")
        assert bot.sent[-1][2] == "你好呀"
    env.run(body)


def test_custom_command_api_validation_import_export(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        j = await (await c.get("/api/commands")).json()
        assert j["rules"] == [] and set(j["actions"]) == {"reply", "ask", "builtin", "random"} and set(j["matches"]) == {"equal", "contains", "regex"}
        # 不合法的整张不存
        for bad, why in ((rule(pattern=""), "没有填触发"), (rule(match="regex", pattern="(未闭合"), "正则不对"), (rule(text=""), "没写回复"),
                         (rule(action="ask", prompt=""), "没写给她的提示"), (rule(action="builtin", builtin="rm_rf"), "不认识"), (rule(action="random", lines=[]), "一句都没写")):
            r = await c.put("/api/commands", json={"rules": [rule(), bad]})
            assert r.status == 400 and why in (await r.json())["error"], why
        assert (await (await c.get("/api/commands")).json())["rules"] == []
        # 保存、排序、导出、再导入
        r = await c.put("/api/commands", json={"rules": [rule(name="甲", pattern="a"), rule(name="乙", pattern="b", action="random", lines=["x", "y"], perm={"mode": "people", "people": ["pe_haitun"]})]})
        saved = (await r.json())["rules"]
        assert [x["name"] for x in saved] == ["甲", "乙"] and saved[0]["id"] != saved[1]["id"] and saved[1]["perm"]["people"] == ["pe_haitun"]
        r = await c.put("/api/commands", json={"rules": list(reversed(saved))})
        again = (await r.json())["rules"]
        assert [x["name"] for x in again] == ["乙", "甲"] and again == customcmd.rules(e.core.db)
        t = await (await c.post("/api/commands/test", json={"text": "a", "at": True})).json()
        assert t["first"] == "甲"
        assert (await (await c.post("/api/commands/test", json={"text": "a", "at": False})).json())["first"] == ""
        await c.close()
    env.run(body)


def test_commands_are_documented():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f, words in (("docs/使用说明.md", ("自定义指令", "完全相等", "不提供脚本")), ("docs/User-Guide.en.md", ("Custom commands", "Exactly equal", "No script"))):
        t = (root / f).read_text(encoding="utf-8")
        assert all(w in t for w in words), (f, words)
