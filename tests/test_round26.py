"""换角色之后旧角色的话不再被当成「她（自己）」；回复开头漏进来的内部标注被去掉；括号里的动作描写被去掉（保守，可关）。"""
from mb import cleanup, templates
from mb.channels.base import Msg

HAITUN_UID = "90001"


def mains(e):
    return [c for c in e.llm.calls if c["_kind"] == "main"]


# ---------- 一、换角色之后的「她（自己）」----------
def test_after_a_role_switch_the_old_role_is_not_her_self(env):
    async def body(e):
        bot = e.add_bot("900")
        e.add_role("r_乙", "乙角色")
        e.core.settings.set("global", "proactive", False)
        e.llm.main = lambda s, u: "我是海豚娘说的这句"
        await e.say("111", "你好", at=["900"])
        room = e.engine.rooms["qq:100"]
        said = [x for x in room.ctx if x.bot][-1]
        assert said.role == "r_haitun" and "〔海豚娘本人〕" in said.name  # 当时是哪个角色，记下来
        eng = e.engine
        b_old = eng.duty(eng.bots["ac_900"], room)
        assert eng.ctx_lines(room, b_old, "你").endswith("你: 我是海豚娘说的这句")  # 还是海豚娘：是她自己说的
        # 换成乙角色（换班）
        await e.say(HAITUN_UID, "换乙角色", at=["900"])
        b_new = eng.duty(eng.bots["ac_900"], room)
        assert b_new.role["name"] == "乙角色"
        lines = eng.ctx_lines(room, b_new, "你")
        assert "海豚娘（这个号之前的角色）: 我是海豚娘说的这句" in lines and "你: 我是海豚娘说的这句" not in lines
        # 守门员那一份：标「她（自己）」的也只是当前角色
        assert "海豚娘（这个号之前的角色）: 我是海豚娘说的这句" in eng.ctx_lines(room, b_new, "她（自己）")
        # 新角色自己说的，标「你」
        e.llm.main = lambda s, u: "我是乙角色说的"
        await e.say("111", "再说一句", at=["900"])
        lines = eng.ctx_lines(room, b_new, "你")
        assert "你: 我是乙角色说的" in lines and "海豚娘（这个号之前的角色）: 我是海豚娘说的这句" in lines
        # 主模型看到的提示词里也是这样
        assert "海豚娘（这个号之前的角色）: 我是海豚娘说的这句" in mains(e)[-1]["messages"][1]["content"]
        # 换回海豚娘：乙角色说的反过来是「之前的角色」
        await e.say(HAITUN_UID, "换回来", at=["900"])
        b_back = eng.duty(eng.bots["ac_900"], room)
        lines = eng.ctx_lines(room, b_back, "你")
        assert "乙角色（这个号之前的角色）: 我是乙角色说的" in lines and "你: 我是海豚娘说的这句" in lines
    env.run(body)


def test_old_entries_without_a_role_are_inferred_from_the_name_or_keep_the_old_label(env):
    async def body(e):
        e.add_bot("900")
        e.add_role("r_乙", "乙角色")
        eng = e.engine
        from mb.engine import Entry
        room = eng.room("qq", "100", "群100")
        room.accounts.add("ac_900")
        other = eng.duty(eng.bots["ac_900"], room)
        eng.duty_rules.set_override("ac_900", "qq:100", 9e9, role="r_乙")
        other = eng.duty(eng.bots["ac_900"], room)
        assert other.role["name"] == "乙角色"
        no_role_haitun = Entry("900", "小号〔海豚娘本人〕", "老记录里海豚娘说的", bot="ac_900")  # 没有角色记录：名字里带着当时的角色
        no_role_other = Entry("900", "小号〔乙角色本人〕", "老记录里乙角色说的", bot="ac_900")
        unknown = Entry("900", "小号", "完全认不出是谁", bot="ac_900")
        assert eng.speaker_label(no_role_haitun, other, "你") == "海豚娘（这个号之前的角色）"
        assert eng.speaker_label(no_role_other, other, "你") == "你"
        assert eng.speaker_label(unknown, other, "你") == "你"  # 推断不了：沿用现在的标法
        human = Entry("111", "群友", "人说的")
        assert eng.speaker_label(human, other, "你") == "群友"
        # 面板里「恢复上下文」存的快照也带角色
        from mb.reset import Reset
        assert Reset._ent(Entry("900", "x", "y", bot="ac_900", role="r_haitun"))["role"] == "r_haitun"
    env.run(body)


def test_gatekeeper_prompt_explains_the_former_role_label(env):
    async def body(e):
        e.add_bot("900")
        e.llm.gate = lambda p: {"reply": False, "brief": "不接"}
        await e.say("111", "随便说一句")
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"][-1]["messages"][0]["content"]
        assert '记录里"她（自己）"是她自己之前发的' in gk and '"某某（这个号之前的角色）"，那不是她说的' in gk
    env.run(body)


# ---------- 二、回复开头漏进来的内部标注 ----------
def test_strip_labels():
    s = cleanup.strip_labels
    assert s("主动插话（接她妈那句）：\n\n友善是品种问题") == "友善是品种问题"
    assert s("主动插话：好呀") == "好呀" and s("【被叫到】好呀") == "好呀" and s("[主动插话] 好呀") == "好呀"
    assert s("被叫到（@了你）: 在") == "在"
    # 正常的话不动
    for ok in ("主动插话这个功能挺好用的", "我被叫到了：怎么回事", "回复：收到", "他说：主动插话（笑）", "好呀 主动插话（接话）：不是开头"):
        assert s(ok) == ok, ok


def test_the_prompt_no_longer_has_a_label_shaped_hook_and_tells_her_not_to_write_one(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.gate = lambda p: {"reply": True, "brief": "接她妈那句"}
        e.llm.main = lambda s, u: "主动插话（接她妈那句）：好呀"
        await e.say("111", "随便聊聊")
        user = mains(e)[-1]["messages"][1]["content"]
        assert "（由头：" not in user and "插话的由头：接她妈那句。" in user and "开头不要加「主动插话」「由头」这类标注" in user
        assert bot.sent[-1][2] == "好呀"  # 就算模型抄了，发送前也去掉
    env.run(body)


# ---------- 三、括号里的动作描写 ----------
def test_strip_actions_removes_whole_line_head_and_tail_segments():
    s = cleanup.strip_actions
    assert s("（尾巴晃了晃）\n好呀") == "好呀"
    assert s("好呀\n（抱着书想了想）") == "好呀"
    assert s("（耳朵抖了一下）") == ""
    assert s("（尾巴甩到某某脸上）好家伙 你过来") == "好家伙 你过来"
    assert s("这句话天生就这样（耳朵抖一下）") == "这句话天生就这样"
    assert s("我也想问问母上大人（尾巴卷住妈的胳膊）") == "我也想问问母上大人"
    assert s("（耳朵抖了一下）（尾巴晃了晃）好呀") == "好呀"  # 连着好几段
    assert s("（清清嗓子 站好）\n我 图书管理员") == "我 图书管理员"
    assert s("(尾巴晃了晃) ok then") == "ok then"  # 半角括号
    assert s("第一行\n\n（尾巴慢悠悠摇）\n\n第二行") == "第一行\n\n第二行"


def test_strip_actions_keeps_explanations_and_anything_unsure():
    s = cleanup.strip_actions
    for keep in ("这个价格是 100 元（含税）", "他是 2019 年入职的（2019 年）", "以上仅供参考（以上仅供参考）", "（含税）", "（2019 年）", "（见下图）",
                 "（注：只在本群有效）", "（不含运费，拍下即发货）", "（笑）", "（小声）", "（凑近）", "（先放一放）", "（参见 FAQ 第 3 条）",
                 "我（抱着书）觉得行", "好的（尾巴晃了晃）然后继续说下去", "他说（尾巴晃了晃）这个动作很可爱 然后走了",
                 "价格（含税）是 100", "A（甲）B（乙）"):
        assert s(keep) == keep, keep
    assert s("") == "" and s("没有括号的一句话") == "没有括号的一句话"


def test_engine_applies_it_before_sending_and_the_switch_works_per_scope(env):
    async def body(e):
        bot = e.add_bot("900")
        e.core.settings.set("global", "proactive", False)
        e.llm.main = lambda s, u: "（尾巴晃了晃）\n好呀\n价格是 100 元（含税）\n（抱着书想了想）"
        await e.say("111", "问一下", at=["900"])
        assert bot.sent[-1][2] == "好呀\n价格是 100 元（含税）" or [m[2] for m in bot.sent][-2:] == ["好呀", "价格是 100 元（含税）"]
        sent = "".join(m[2] for m in bot.sent)
        assert "尾巴" not in sent and "抱着书" not in sent and "（含税）" in sent
        # 全局关掉：原样发
        n = len(bot.sent)
        e.core.settings.set("global", "strip_actions", False)
        await e.say("111", "再问一下", at=["900"], chat="200")
        assert "（尾巴晃了晃）" in "".join(m[2] for m in bot.sent[n:])
        # 角色、群各层都能设：全局关着，这个群单独开
        n = len(bot.sent)
        e.core.settings.set("chat:qq:300", "strip_actions", True)
        await e.say("111", "又问", at=["900"], chat="300")
        assert "尾巴" not in "".join(m[2] for m in bot.sent[n:])
        n = len(bot.sent)
        e.core.settings.set("role:r_haitun", "strip_actions", False)
        await e.say("111", "又又问", at=["900"], chat="300")  # 角色在群里，比「群」那层更具体吗？同一群里角色层不如群层具体：群开着就是开
        assert "尾巴" not in "".join(m[2] for m in bot.sent[n:])
    env.run(body)


def test_setting_defaults():
    from mb.settings import BY_KEY
    s = BY_KEY["strip_actions"]
    assert s["default"] is True and set(s["scopes"]) == {"G", "P", "R", "RP", "C", "RC"}
