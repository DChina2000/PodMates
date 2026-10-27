"""角色分配规则：条件组合、跨午夜时段、优先级、轮换、临时覆盖到期、无规则命中回到默认角色、同群协调按在岗角色计算、换班。"""
import time
from datetime import datetime

from mb.rules import Duty, matches, pick, specificity

ACC = {"id": "ac_1", "platform": "qq", "role_id": "r_default"}


def R(**kw):
    kw.setdefault("id", "x")
    kw.setdefault("mode", "fixed")
    return kw


def test_condition_combinations():
    t = datetime(2026, 10, 23, 20, 30)  # 周五 20:30
    assert matches(R(), "qq", "ac_1", "qq:1", [], t)  # 什么都不填 = 不限
    assert matches(R(platforms=["qq"], accounts=["ac_1"], chats=["qq:1"]), "qq", "ac_1", "qq:1", [], t)
    assert not matches(R(platforms=["discord"]), "qq", "ac_1", "qq:1", [], t)
    assert not matches(R(accounts=["ac_2"]), "qq", "ac_1", "qq:1", [], t)
    assert not matches(R(chats=["qq:2"]), "qq", "ac_1", "qq:1", [], t)
    assert matches(R(chat_groups=["g_study"]), "qq", "ac_1", "qq:1", ["g_study"], t)  # 群分组
    assert not matches(R(chat_groups=["g_study"]), "qq", "ac_1", None, [], t)  # 私聊不算在任何群里
    assert matches(R(weekdays=[4], time_from="20:00", time_to="22:00"), "qq", "ac_1", "qq:1", [], t)
    assert not matches(R(weekdays=[5], time_from="20:00", time_to="22:00"), "qq", "ac_1", "qq:1", [], t)
    assert matches(R(date_from="2026-10-20", date_to="2026-10-25"), "qq", "ac_1", "qq:1", [], t)
    assert not matches(R(date_from="2026-10-24"), "qq", "ac_1", "qq:1", [], t)
    assert not matches(R(on=False), "qq", "ac_1", "qq:1", [], t)


def test_overnight_time_range():
    r = R(weekdays=[4], time_from="22:00", time_to="02:00")  # 周五晚上 22 点到次日 2 点
    assert matches(r, "qq", "a", None, [], datetime(2026, 10, 23, 23, 0))  # 周五 23:00
    assert matches(r, "qq", "a", None, [], datetime(2026, 10, 24, 1, 30))  # 周六凌晨 1:30，算周五晚上
    assert not matches(r, "qq", "a", None, [], datetime(2026, 10, 24, 2, 0))  # 2:00 结束（不含）
    assert not matches(r, "qq", "a", None, [], datetime(2026, 10, 23, 1, 30))  # 周五凌晨 1:30 属于周四晚上
    assert not matches(r, "qq", "a", None, [], datetime(2026, 10, 23, 21, 59))


def test_rotation():
    seq = R(id="rot", mode="rotate", roles=["a", "b", "c"], order="seq", every="day")
    days = [pick(seq, datetime(2026, 10, d, 12, 0)) for d in (20, 21, 22, 23)]
    assert days[0] != days[1] != days[2] and days[3] == days[0]  # 按天轮
    assert pick(seq, datetime(2026, 10, 20, 1, 0)) == pick(seq, datetime(2026, 10, 20, 23, 0))  # 一天之内不变
    slot = R(id="rot2", mode="rotate", roles=["a", "b"], order="seq", every="slot", slot_minutes=120)
    assert pick(slot, datetime(2026, 10, 20, 8, 0)) != pick(slot, datetime(2026, 10, 20, 10, 0))  # 每 2 小时换
    assert pick(slot, datetime(2026, 10, 20, 8, 0)) == pick(slot, datetime(2026, 10, 20, 9, 59))
    rnd = R(id="rnd", mode="rotate", roles=["a", "b", "c"], order="random", every="day")
    t = datetime(2026, 10, 20, 9, 0)
    assert len({pick(rnd, t.replace(hour=h)) for h in range(24)}) == 1  # 随机，但同一天固定
    assert len({pick(rnd, datetime(2026, 10, d, 9)) for d in range(1, 29)}) > 1


def test_priority_order_and_autosort(env):
    async def body(e):
        db = e.core.db
        d = Duty(db)
        acc = {"id": "ac_1", "platform": "qq", "role_id": "r_default"}
        db.put("rule", R(id="r_all", role="r_any"))
        db.put("rule", R(id="r_night", role="r_night", time_from="22:00", time_to="06:00"))
        db.put("rule", R(id="r_fri", role="r_fri", weekdays=[4], time_from="20:00", time_to="23:59"))
        db.put("rule", R(id="r_date", role="r_date", date_from="2026-10-23", date_to="2026-10-23"))
        t = datetime(2026, 10, 23, 22, 30)
        assert d.resolve(acc, "qq:1", t) == ("r_any", "rule:r_all")  # 按拖动顺序，上面的先命中
        # 按具体程度自动排序：指定日期 > 星期加时段 > 每天时段 > 不限
        rules = sorted(d.rules(), key=specificity)
        assert [r["id"] for r in rules] == ["r_date", "r_fri", "r_night", "r_all"]
        db.reorder("rule", [r["id"] for r in rules])
        assert d.resolve(acc, "qq:1", t)[0] == "r_date"
        assert d.resolve(acc, "qq:1", datetime(2026, 10, 30, 22, 30))[0] == "r_fri"
        assert d.resolve(acc, "qq:1", datetime(2026, 10, 29, 23, 0))[0] == "r_night"
        assert d.resolve(acc, "qq:1", datetime(2026, 10, 29, 12, 0))[0] == "r_any"
        # 一条都没命中：用账号的默认角色
        db.delete("rule", "r_all")
        assert d.resolve(acc, "qq:1", datetime(2026, 10, 29, 12, 0)) == ("r_default", "default")
        # 检查：被挡住永远轮不到的、已过期的、没有任何角色的时段
        db.put("rule", R(id="r_shadowed", role="r_x", time_from="23:00", time_to="23:30", name="被挡住的"))  # 被 r_night 全挡
        db.put("rule", R(id="r_old", role="r_x", date_to="2020-01-01", name="过期的"))
        warn = d.check([acc], [{"id": "qq:1", "platform": "qq", "name": "群1"}], days=7)
        texts = " ".join(w["text"] for w in warn)
        assert "「被挡住的」未来 7 天里永远轮不到" in texts and "「过期的」的日期已经过了" in texts
        assert "没有任何角色" not in texts  # 有默认角色兜底
        warn = d.check([dict(acc, role_id="")], [{"id": "qq:1", "platform": "qq", "name": "群1"}], days=7)
        assert any("没有任何角色" in w["text"] for w in warn)
        # 预览：现在是谁、下次几点换成谁
        segs = d.timeline(acc, "qq:1", datetime(2026, 10, 29, 21, 0), 120)
        assert [s["role"] for s in segs] == ["r_default", "r_night"] and segs[1]["start"].endswith("22:00")
    env.run(body)


def test_override_expiry_and_engine_duty(env):
    async def body(e):
        db, eng = e.core.db, e.engine
        e.add_role("r_乙", "乙角色")
        a, b = e.add_bot("901"), e.add_bot("902")  # 两个账号默认都是海豚娘
        e.core.settings.set("global", "proactive", False)
        # 同群两个账号同一个角色：不算「姐妹」
        await e.say("111", "在吗", at=["901"])
        assert "【姐妹】" not in e.llm.calls[-1]["messages"][0]["content"]
        # 规则：902 在群 100 当乙角色 → 同群协调按在岗角色算，901 看到的是姐妹乙角色
        db.put("rule", {"id": "rr", "accounts": ["ac_902"], "chats": ["qq:100"], "mode": "fixed", "role": "r_乙"})
        await e.say("111", "在吗", at=["901"])
        sysmsg = e.llm.calls[-1]["messages"][0]["content"]
        assert "「乙角色」是你的姐妹" in sysmsg
        await e.say("111", "你呢", at=["902"])
        assert "你是乙角色。" in e.llm.calls[-1]["messages"][0]["content"]  # 902 用乙角色的人设
        # 别的群没规则：回到默认角色
        await e.say("111", "你呢", at=["902"], chat="200")
        assert "一只泡在" in e.llm.calls[-1]["messages"][0]["content"]  # 海豚娘的外壳（乙角色用的是「你是乙角色。」）
        # 临时覆盖（带时长）
        await e.say("90001", "换海豚娘 2小时", at=["902"])
        assert b.sent[-1][2].startswith("好，换成「海豚娘」，到")
        assert eng.duty(eng.bots["ac_902"], eng.rooms["qq:100"]).rid == "r_haitun"
        o = eng.duty_rules.override("ac_902", "qq:100")
        assert 7100 < o["until"] - time.time() <= 7200
        # 到期后回到规则表的安排
        ov = db.kv("role_overrides")
        ov["ac_902|qq:100"]["until"] = time.time() - 1
        db.set_kv("role_overrides", ov)
        assert eng.duty(eng.bots["ac_902"], eng.rooms["qq:100"]).rid == "r_乙"
        # 「换××」里的××不是角色也不是人设：按普通聊天
        n = len(b.sent)
        await e.say("90001", "换个话题吧", at=["902"])
        assert "换个话题吧" in e.llm.calls[-1]["messages"][1]["content"] and len(b.sent) == n + 1
        # 换回
        await e.say("90001", "换猫猫娘", at=["902"])  # 没有这个角色 → 普通聊天
        await e.say("90001", "换海豚 30分钟", at=["902"])
        assert eng.duty(eng.bots["ac_902"], eng.rooms["qq:100"]).rid == "r_haitun"
        await e.say("90001", "换回", at=["902"])
        assert eng.duty(eng.bots["ac_902"], eng.rooms["qq:100"]).rid == "r_乙"
    env.run(body)


def test_shift_change_greeting_and_card(env):
    async def body(e):
        db, eng = e.core.db, e.engine
        e.add_role("r_乙", "乙角色")
        a = e.add_bot("901")
        s = e.core.settings
        s.set("global", "proactive", False)
        await e.say("111", "大家好")
        await eng.tick()  # 第一次只记下在岗角色
        assert not getattr(a, "cards", [])
        s.set("global", "shift_greeting", True)
        s.set("global", "shift_card", True)
        e.llm.main = lambda sys_, u: "乙角色来接班啦" if "你刚接班" in u else "嗯"
        db.put("rule", {"id": "rr", "chats": ["qq:100"], "mode": "fixed", "role": "r_乙"})
        await eng.tick()
        await eng.idle()
        assert a.cards == [("100", "乙角色")]  # 群名片随角色改
        assert a.sent[-1][2] == "乙角色来接班啦"  # 新角色打招呼
        assert "之前在这个群的是「海豚娘」" in e.llm.calls[-1]["messages"][1]["content"]
    env.run(body)
