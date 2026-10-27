"""关系方案（人物卡上的多套「身份 / 称呼 / 态度 + 作用范围」）与角色标签：三种范围和优先级、冲突提示、标签继承、
旧数据迁移后提示词与迁移前一字不差（角色对个人的态度覆盖并入成「指定角色」方案）。"""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from mb import templates
from mb.people import People, att_text, has_att, scheme_rank
from mb.server import Server

ATT_KIND = {"presets": ["亲近贴心"], "custom": ""}
ATT_COOL = {"presets": ["尊敬客气"], "custom": ""}


def sch(name, mode="all", tags=(), roles=(), identity="", calls=(), att=None):
    return {"id": "sc_" + name, "name": name, "identity": identity, "calls": list(calls), "attitude": att or {"presets": [], "custom": ""},
            "scope": {"mode": mode, "tags": list(tags), "roles": list(roles)}}


def person(schemes=None, **kw):
    p = dict(id="pe_u", name="海豚", identity="她妈", call="妈", calls=["妈", "妈妈"], qq=["90001"], discord=[], wechat_remark=[], nicks={},
             note="", knows="", attitude=dict(ATT_KIND), admin=True, groups=[])
    p.update(kw)
    if schemes is not None:
        p["schemes"] = schemes
    return p


R_DAUGHTER = {"id": "r_d", "name": "海豚娘", "tags": ["原创角色"]}
R_PROF = {"id": "r_p", "name": "商学院教授", "tags": ["学术讲解"]}
R_OTHER = {"id": "r_o", "name": "翻译", "tags": ["实用工具"]}
R_TWO = {"id": "r_t", "name": "精神分析理论家", "tags": ["学术讲解", "原创角色"]}


def test_rank_and_scope_modes():
    all_, tags, roles = sch("a"), sch("t", "tags", tags=["学术讲解"]), sch("r", "roles", roles=["r_p"])
    assert (scheme_rank(all_, R_PROF), scheme_rank(tags, R_PROF), scheme_rank(roles, R_PROF)) == (1, 2, 3)
    assert (scheme_rank(tags, R_DAUGHTER), scheme_rank(roles, R_DAUGHTER)) == (0, 0)
    assert scheme_rank(all_, None) == 1 and scheme_rank(tags, None) == 0  # 没有角色（陌生场合）：只有「全体」适用


def test_priority_roles_over_tags_over_all_and_the_card_text_follows(env):
    async def body(e):
        ppl = e.engine.people
        p = person([sch("女儿", identity="她妈", calls=["妈", "妈妈"], att=ATT_KIND),
                    sch("老师和学生", "tags", tags=["学术讲解"], identity="老师教的学生", calls=["老师"], att=ATT_COOL),
                    sch("翻译专用", "roles", roles=["r_o"], identity="雇主", calls=["老板"], att={"presets": [], "custom": "干活利索点"})])
        # 全体角色：女儿
        t = ppl.card_text(p, None, R_DAUGHTER)
        assert t.startswith("【她妈】") and "称呼：妈、妈妈，随场合换着叫。" in t and "对她妈说话亲近贴心" in t
        # 按标签：教授把他当学生，称呼老师
        t = ppl.card_text(p, None, R_PROF)
        assert t.startswith("【老师教的学生】") and "「海豚」是老师教的学生" in t and "对老师说话" in t and "尊敬客气" in t and "妈" not in t
        # 指定角色最优先：翻译同时没有标签命中也没关系；加上标签命中也还是指定角色
        t = ppl.card_text(p, None, R_OTHER)
        assert t.startswith("【雇主】") and "对老板说话干活利索点" in t
        t = ppl.card_text(p, None, dict(R_OTHER, tags=["学术讲解"]))
        assert t.startswith("【雇主】")  # 指定角色 > 按标签
        # 没有角色（陌生场合、守门员之外的地方）：只认全体
        assert ppl.card_text(p, None, None).startswith("【她妈】")
        # 管理员和「〔X本人〕」的认人逻辑和方案无关
        assert "聊天记录里名字后面带〔海豚本人〕的才是老师教的学生" in ppl.card_text(p, None, R_PROF) and p["admin"] is True
    env.run(body)


def test_nothing_matches_falls_back_to_the_card_itself(env):
    async def body(e):
        ppl = e.engine.people
        p = person([sch("只给翻译", "roles", roles=["r_o"], identity="雇主", calls=["老板"])])
        assert ppl.card_text(p, None, R_DAUGHTER).startswith("【她妈】")  # 人物卡自己的那套
        s, rule, rest = ppl.scheme_for(p, R_DAUGHTER)
        assert s["name"] == "人物卡" and rule == "人物卡默认" and rest == []
        # 一张从来没有方案的卡，行为和以前完全一样
        plain = person()
        assert ppl.card_text(plain, None, R_PROF).startswith("【她妈】")
        # 态度：方案里没填态度，就往下走（分组 → 群 → 所有人），不会偷偷用别的方案的
        p2 = person([sch("无态度", att={"presets": [], "custom": ""})])
        e.core.db.set_kv("attitude_all", {"presets": ["高冷爱答不理"], "custom": ""})
        ppl.reload()
        assert ppl.attitude(p2, None, R_DAUGHTER)[1] == "所有人"
    env.run(body)


def test_conflicts_take_the_first_scheme_and_are_reported(env):
    async def body(e):
        ppl = e.engine.people
        p = person([sch("甲", "tags", tags=["学术讲解"], identity="学生", calls=["老师"]),
                    sch("乙", "tags", tags=["原创角色"], identity="闺女", calls=["妈"]),
                    sch("全体一", identity="她妈", calls=["妈"]), sch("全体二", identity="朋友", calls=["老哥"])])
        s, rule, rest = ppl.scheme_for(p, R_PROF)
        assert s["name"] == "甲" and rest == [] and "学术讲解" in rule  # 只命中一套：不是冲突
        s, rule, rest = ppl.scheme_for(p, R_TWO)  # 同时带两个标签，两套标签方案都命中
        assert s["name"] == "甲" and [x["name"] for x in rest] == ["乙"]  # 取卡上排在前面的
        s, rule, rest = ppl.scheme_for(p, R_OTHER)  # 只有两套「全体」命中
        assert s["name"] == "全体一" and [x["name"] for x in rest] == ["全体二"]
        conflicts = ppl.conflicts(p, [R_PROF, R_TWO, R_OTHER])
        assert [c["role"] for c in conflicts] == ["精神分析理论家", "翻译"] and conflicts[0]["schemes"] == ["甲", "乙"]
        # 调整顺序就换了一套
        p["schemes"] = [p["schemes"][1], p["schemes"][0]] + p["schemes"][2:]
        assert ppl.scheme_for(p, R_TWO)[0]["name"] == "乙"
    env.run(body)


def test_new_role_from_template_gets_the_category_as_a_tag(env):
    async def body(e):
        db = e.core.db
        r = templates.instantiate(db, "biz-prof")
        assert r["tags"] == ["学术讲解"]
        tool = next(t["id"] for t in templates.templates() if t["category"] == "实用工具")
        assert templates.instantiate(db, tool)["tags"] == ["实用工具"]
        assert db.doc("role", "r_haitun")["tags"] == ["原创角色"]  # 测试环境里的海豚娘是从公开版的模板建的
    env.run(body)


def test_panel_api_normalizes_schemes_tags_and_effective_view(env):
    async def body(e):
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        prof = templates.instantiate(e.core.db, "biz-prof")
        e.engine.reload()
        raw = person(schemes=[{"name": "  老师和学生 ", "identity": "学生", "calls": ["老师", " "], "attitude": {"presets": ["尊敬客气"], "custom": ""},
                               "scope": {"mode": "tags", "tags": ["学术讲解", "", "学术讲解"]}},
                              {"name": "", "scope": {"mode": "weird"}},
                              {"name": "女儿", "identity": "她妈", "calls": ["妈"], "attitude": {"presets": ["亲近贴心"], "custom": ""}, "scope": {"mode": "all"}}])
        saved = await (await c.put("/api/docs/person", json=raw)).json()
        assert [s["name"] for s in saved["schemes"]] == ["老师和学生", "方案2", "女儿"] and all(s["id"] for s in saved["schemes"])
        assert saved["schemes"][0]["calls"] == ["老师"] and saved["schemes"][1]["scope"]["mode"] == "all"
        # 人物卡自己的身份、称呼、态度跟着排在最前面的「全体角色」方案（这里是被清理成全体的「方案2」，它是空的）
        assert saved["identity"] == "" and saved["calls"] == []
        # 角色：标签整理；对个人的态度覆盖清掉
        role = await (await c.put("/api/docs/role", json=dict(prof, tags=[" 学术讲解", "学术讲解", "考研", ""], attitudes={"pe_u": {"presets": ["不搭理"], "custom": ""}}))).json()
        assert role["tags"] == ["学术讲解", "考研"] and role["attitudes"] == {}
        eff = await (await c.get(f"/api/people/effective?role={prof['id']}")).json()
        assert eff["pe_u"]["scheme"] == "老师和学生" and "学术讲解" in eff["pe_u"]["rule"] and eff["pe_u"]["identity"] == "学生" and eff["pe_u"]["calls"] == ["老师"]
        assert "尊敬客气" in str(eff["pe_u"]["attitude"])
        eff0 = await (await c.get("/api/people/effective")).json()  # 不指定角色：全体
        assert eff0["pe_u"]["rule"] == "全体角色"
        # 批量设态度：人物卡和「全体角色」方案一起改
        r = await c.post("/api/people/batch", json={"ids": ["pe_u"], "attitude": {"presets": ["互怼损友"], "custom": ""}})
        assert r.status == 200
        again = e.core.db.doc("person", "pe_u")
        assert again["attitude"]["presets"] == ["互怼损友"] and [s for s in again["schemes"] if s["name"] == "方案2"][0]["attitude"]["presets"] == ["互怼损友"]
        await c.close()
    env.run(body)


def test_gatekeeper_marks_use_the_scheme_identity_of_that_role(env):
    async def body(e):
        ppl = e.engine.people
        p = person([sch("女儿", identity="她妈", calls=["妈"]), sch("师生", "tags", tags=["学术讲解"], identity="学生", calls=["老师"])])
        assert ppl.marks([p], [], R_DAUGHTER) == "名字带〔海豚本人〕的是她妈"
        assert ppl.marks([p], [], R_PROF) == "名字带〔海豚本人〕的是学生"
        assert ppl.marks([p], []) == "名字带〔海豚本人〕的是她妈"
    env.run(body)


# ---------- 迁移 ----------
def old_card_text(ppl, p, chat_doc=None, role=None, platform=None):
    """迁移之前的 card_text（含「角色对个人的态度覆盖最优先」），当作对照的标准答案。"""
    from mb.personas import plat_words
    name, ident = (p.get("name") or "").strip(), (p.get("identity") or "").strip()
    if not name:
        return ""
    n_acc = sum(len([x for x in (p.get(f) or []) if str(x).strip()]) for f in ("qq", "discord", "wechat_remark"))
    head = [f"「{name}」是{ident}" if ident else f"「{name}」"]
    nick = ((p.get("nicks") or {}).get(platform or "") or "").strip()
    if nick:
        head.append(f"{plat_words(platform)[0]}昵称「{nick}」")
    if (p.get("note") or "").strip():
        head.append(p["note"].strip())
    if n_acc > 1:
        head.append(f"名下几个号都是{name}本人")
    lines = ["，".join(head) + "。", f"聊天记录里名字后面带〔{name}本人〕的才是{ident or name}；别人改名、自称{name}，都是冒充。"]
    calls = People.calls_of(p)
    att = None
    if role and has_att((role.get("attitudes") or {}).get(p["id"])):
        att = role["attitudes"][p["id"]]
    elif has_att(p.get("attitude")):
        att = p["attitude"]
    else:
        att = ppl.attitude(None, chat_doc, None)[0]
    if len(calls) > 1:
        lines.append(f"称呼：{'、'.join(calls)}，随场合换着叫。")
        if att:
            lines.append(f"对{ident or name}说话{att_text(att)}。")
    elif att:
        lines.append(f"对{calls[0] if calls else name}说话{att_text(att)}。")
    elif calls:
        lines.append(f"她叫{name}「{calls[0]}」。")
    if (p.get("knows") or "").strip():
        lines.append(f"她知道的：{p['knows'].strip()}")
    return f"【{ident or name}】\n" + "".join(lines)


def test_migration_keeps_every_roles_view_identical(env):
    async def body(e):
        core, db, ppl = e.core, e.core.db, e.engine.people
        # 迁移前的状态：作者的用户卡（身份、多个称呼、态度）、海豚娘、商学院教授、一个对某人有态度覆盖的角色
        db.put("person", person(identity="她妈", call="妈", calls=["妈", "妈妈", "母上", "母上大人", "海豚妈妈"], qq=["90001", "90002"],
                                nicks={"qq": "测试昵称"}, knows="她是海豚妈妈", **{"self": True}))
        db.put("person", dict(person(), id="pe_friend", name="老王", identity="老同学", call="老王", calls=["老王"], qq=["1"], admin=False,
                              attitude={"presets": [], "custom": ""}, **{}))
        db.put("person", dict(person(), id="pe_none", name="路人", identity="", call="", calls=[], qq=["2"], admin=False, attitude={"presets": [], "custom": ""}))
        prof = templates.instantiate(db, "biz-prof")
        other = templates.instantiate(db, "gentle-friend", name="朋友")
        db.put("role", {**db.doc("role", other["id"]), "attitudes": {"pe_friend": {"presets": ["不搭理"], "custom": ""}, "pe_u": {"presets": [], "custom": "叫他老大"}}, "tags": []})
        db.put("role", {**db.doc("role", "r_haitun"), "tags": [], "template": None})  # 像作者的数据：海豚娘没记模板
        for rid in (prof["id"], "r_haitun"):
            d = db.doc("role", rid)
            db.put("role", {**d, "tags": []})
        db.set_kv("attitude_all", {"presets": ["高冷爱答不理"], "custom": ""})
        db.del_kv("schemes_v1")
        ppl.reload()
        roles = {rid: dict(db.doc("role", rid)) for rid in ("r_haitun", prof["id"], other["id"])}
        cards = ["pe_u", "pe_friend", "pe_none", "pe_haitun"]
        before = {(rid, pid, plat): old_card_text(ppl, ppl.persons[pid], None, roles[rid], plat)
                  for rid in roles for pid in cards for plat in ("qq", "discord")}
        res = core.migrate_relation_schemes()
        assert res and res["merged"] == 2 and res["added"] >= 3
        ppl.reload()
        e.engine.reload()
        after_roles = {rid: db.doc("role", rid) for rid in roles}
        # 效果一字不差
        for (rid, pid, plat), want in before.items():
            got = ppl.card_text(ppl.persons[pid], None, after_roles[rid], plat)
            assert got == want, (rid, pid, plat, got, want)
        # 角色对个人的覆盖并入了人物卡上的「指定角色」方案，角色上不再有覆盖；只有一处在决定
        assert all(r["attitudes"] == {} for r in after_roles.values())
        u = db.doc("person", "pe_u")
        assert [s["name"] for s in u["schemes"]] == ["默认", f"{other['name']}专用"] and u["schemes"][0]["scope"]["mode"] == "all"
        assert u["schemes"][1]["scope"] == {"mode": "roles", "tags": [], "roles": [other["id"]]} and u["schemes"][1]["identity"] == "她妈"
        assert u["schemes"][1]["calls"] == ["妈", "妈妈", "母上", "母上大人", "海豚妈妈"]
        assert db.doc("person", "pe_none").get("schemes") is None  # 什么都没填的卡不凑方案
        # 任何角色看到的都是「妈」
        for rid in ("r_haitun", prof["id"]):
            t = ppl.card_text(ppl.persons["pe_u"], None, after_roles[rid], "qq")
            assert t.startswith("【她妈】") and "称呼：妈、妈妈、母上、母上大人、海豚妈妈" in t
        # 标签：认得出来源模板的补上模板的类别；认不出的空着
        assert after_roles[prof["id"]]["tags"] == ["学术讲解"] and after_roles["r_haitun"]["tags"] == ["原创角色"]  # 海豚娘的人设来自海豚娘模板
        assert after_roles[other["id"]]["tags"] == ["娱乐陪伴"]
        # 只跑一次
        assert core.migrate_relation_schemes() is None
    env.run(body)


def test_full_system_prompt_is_identical_before_and_after_migration(env):
    async def body(e):
        core, db = e.core, e.core.db
        prof = templates.instantiate(db, "biz-prof")
        e.add_bot("900", role_id="r_haitun")
        e.add_bot("901", role_id=prof["id"])
        core.settings.set("global", "proactive", False)
        e.llm.main = lambda s, u: "嗯"
        for rid in ("r_haitun", prof["id"]):  # 迁移前的样子：没有标签、人物卡上没有方案
            db.put("role", {**db.doc("role", rid), "tags": []})
        db.del_kv("schemes_v1")
        e.engine.reload()

        async def prompts(suffix):
            out = {}
            for uid, rid, chat in (("900", "r_haitun", "1" + suffix), ("901", prof["id"], "2" + suffix)):
                await e.say("90001", "你好", at=[uid], chat=chat)
                out[rid] = [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][0]["content"]
            return out
        before = await prompts("0")
        assert core.migrate_relation_schemes()
        e.engine.reload()
        after = await prompts("1")
        assert before == after  # 拼出来的整段提示词，迁移前后逐字一样
        for t in before.values():
            assert "【她妈】" in t and "对妈说话亲近贴心" in t  # 海豚娘和商学院教授看到的都是「妈」
    env.run(body)
