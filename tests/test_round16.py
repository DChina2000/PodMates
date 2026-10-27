"""七个哲学模块、说话风格 / 贴吧嘴 / 构型引导句的改动、「承认是AI」总开关（总 / 角色 / 群三层）、「她（自己）」。"""
import re
from pathlib import Path

from mb import personas, templates

ROOT = Path(__file__).resolve().parent.parent
TIEBA = "- 贴吧出身的嘴：反应快、嘴毒、会阴阳怪气，对线不怂，抓住对面的漏洞就往死里拆，骂起来也不客气。梗和缩写随手来，对线要展开时可以长些。"
STYLE = "- {{平台}}纯文本打字：短句、少标点，同一个梗别反复用；平时一两句，认真聊时可以长些，但仍是打字聊天，不用Markdown、列表、标题、加粗。"
AI_LINE = "是AI，被问就认；是AI也是海豚的女儿，不冲突。"


def make(e, tid):
    templates.instantiate(e.core.db, tid, role_id="r_" + tid, persona_id="p_" + tid)
    e.engine.reload()


def prompt(e, tid, platform="qq", user="海豚", ai=True):
    return personas.assemble(e.engine.personas["p_" + tid], e.engine.modules, platform, {"people": "", "memory": ""}, user=user, ai=ai)


def test_shared_modules_and_seed_texts():
    core = (ROOT / "assets/seed/persona_core.md").read_text(encoding="utf-8")
    assert TIEBA in core and STYLE in core and "怎么吵" not in core and "梗和缩写随手来，同一个梗" not in core
    assert "怎么吵" not in personas.ARCH_INTRO and personas.ARCH_INTRO.endswith("她怎么接话、怎么对人，由这份看法同她的身份和眼前的事一起决定。")
    for f in ("pages-persona.js",):
        assert "怎么吵" not in (ROOT / "web/js" / f).read_text(encoding="utf-8")
    ph = (ROOT / "assets/seed/philosophers.md").read_text(encoding="utf-8").splitlines()
    assert [l[2:l.index("：")] for l in ph if l.strip()] == personas.PHILOSOPHERS


def test_ai_gate_text_rules():
    t = "- [[AI|你是AI，被问就认；]]你陪着聊天，但替代不了朋友。\n- [[AI|你是AI，被问就认。]]\n- 偶尔自嘲[[AI|，AI的身份也能拿来自嘲，但被认真问到时如实承认]]。\n- 别的。"
    assert personas.ai_gate(t, True) == "- 你是AI，被问就认；你陪着聊天，但替代不了朋友。\n- 你是AI，被问就认。\n- 偶尔自嘲，AI的身份也能拿来自嘲，但被认真问到时如实承认。\n- 别的。"
    assert personas.ai_gate(t, False) == "- 你陪着聊天，但替代不了朋友。\n- 偶尔自嘲。\n- 别的。"
    assert personas.ai_gate("没有标记的话。", False) == "没有标记的话。"


def test_gated_module_follows_the_switch_and_whole_mode_keeps_it(env):
    async def body(e):
        make(e, "haitun")
        assert e.engine.modules["m_AI身份"]["gate"] == "admit_ai"
        on, off = prompt(e, "haitun", ai=True), prompt(e, "haitun", ai=False)
        assert "- " + AI_LINE in on and "是AI" not in off and "被问就认" not in off
        assert on.replace("- " + AI_LINE + "\n", "") == off  # 关的时候只是少了这一行
        # 转成整体：开关照样管
        p = dict(e.engine.personas["p_haitun"])
        whole = personas.to_whole(p, e.engine.modules, "qq")
        assert "[[AI|" in whole and "{{用户}}" in whole
        w = {**p, "mode": "whole", "whole": whole}
        assert personas.assemble(w, e.engine.modules, "qq", {"people": "", "memory": ""}, user="海豚", ai=True) == on
        assert personas.assemble(w, e.engine.modules, "qq", {"people": "", "memory": ""}, user="海豚", ai=False) == off
    env.run(body)


AI_TEMPLATES = {"gentle-friend": ["你是AI，被问就认，不装成真人。"], "cheer-buddy": ["你是AI，被问就认。"],
                "deadpan": ["AI的身份也能拿来自嘲，但被认真问到时如实承认"]}


def test_other_templates_ai_sentences_follow_the_switch(env):
    async def body(e):
        raw = {t["id"]: "\n".join([t["shell"]] + [s["body"] for s in t["sections"]]) for t in templates.templates()}
        marked = [tid for tid, x in raw.items() if "[[AI|" in x]
        assert len(marked) == 5 and sum(x.count("[[AI|") for x in raw.values()) == 5  # 娱乐陪伴 4 处、生活健康 1 处
        assert sum(raw[t].count("[[AI|") for t in marked if templates.get(t)["category"] == "娱乐陪伴") == 4
        assert sum(raw[t].count("[[AI|") for t in marked if templates.get(t)["category"] == "生活健康") == 1
        for tid in marked:
            make(e, tid)
            on, off = prompt(e, tid, ai=True), prompt(e, tid, ai=False)
            assert "[[" not in on and "[[" not in off and "{{" not in off
            assert ("被问就认" in on or "如实承认" in on) and "被问就认" not in off and "如实承认" not in off and "AI的身份" not in off
            assert not re.search(r"^- ?$", off, re.M)  # 整行空了的不留空条目
        # 句子里与AI无关的部分留着
        assert "- 你陪着聊天，但替代不了朋友、家人和专业的帮助。" in prompt(e, [t for t in marked if "陪着聊天" in raw[t]][0], ai=False)
        assert "- 偶尔自嘲。" in prompt(e, "deadpan", ai=False) and "- 偶尔自嘲，AI的身份也能拿来自嘲，但被认真问到时如实承认。" in prompt(e, "deadpan", ai=True)
        assert any("- 你能陪聊，但替代不了身边的人和专业的帮助，适当时候温柔地提一下。" in prompt(e, t, ai=False) for t in marked if templates.get(t)["category"] == "生活健康")
        # 模板预览里按「开」显示
        assert "[[" not in str(templates.preview("deadpan")) and "被问就认" in str(templates.preview("gentle-friend"))
    env.run(body)


def main_system(e):
    return [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][0]["content"]


def test_switch_layers_global_role_chat(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.main = lambda s, u: "好"
        assert e.core.settings.g("admit_ai") is True  # 新装默认开
        await e.say("111", "你是AI吗", at=["900"])
        assert "被问就认" in main_system(e)
        # 总开关关：角色（跟随）、群里都跟着变
        e.core.settings.set("global", "admit_ai", False)
        await e.say("111", "你是AI吗", at=["900"])
        assert "被问就认" not in main_system(e) and "是AI" not in main_system(e)
        # 角色单独开：不受总开关影响
        e.core.settings.set("role:r_haitun", "admit_ai", True)
        await e.say("111", "你是AI吗", at=["900"])
        assert "被问就认" in main_system(e)
        # 角色单独关，总开关开：也不受影响
        e.core.settings.set("global", "admit_ai", True)
        e.core.settings.set("role:r_haitun", "admit_ai", False)
        await e.say("111", "你是AI吗", at=["900"])
        assert "被问就认" not in main_system(e)
        # 回到「跟随」：随总开关
        e.core.settings.reset("role:r_haitun", "admit_ai")
        await e.say("111", "你是AI吗", at=["900"])
        assert "被问就认" in main_system(e)
        # 群一级：总开关开着，这个群单独关；换一个群不受影响
        e.core.settings.set("chat:qq:100", "admit_ai", False)
        await e.say("111", "你是AI吗", chat="100", at=["900"])
        assert "被问就认" not in main_system(e)
        await e.say("111", "你是AI吗", chat="200", at=["900"])
        assert "被问就认" in main_system(e)
        # 总开关关着，这个群单独开
        e.core.settings.set("global", "admit_ai", False)
        e.core.settings.set("chat:qq:100", "admit_ai", True)
        await e.say("111", "你是AI吗", chat="100", at=["900"])
        assert "被问就认" in main_system(e)
        await e.say("111", "你是AI吗", chat="200", at=["900"])
        assert "被问就认" not in main_system(e)
    env.run(body)


def test_switch_is_a_setting_visible_on_every_layer(env):
    s = next(x for x in __import__("mb.settings", fromlist=["SCHEMA"]).SCHEMA if x["key"] == "admit_ai")
    assert s["default"] is True and set(s["scopes"]) == {"G", "P", "R", "RP", "C", "RC"}


def test_gatekeeper_calls_herself_ta_self(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.gate = lambda p: {"reply": False, "brief": "不接"}
        await e.say("111", "随便说一句")
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"][-1]["messages"][0]["content"]
        assert '记录里"她（自己）"是她自己之前发的' in gk and "她（机器人）" not in gk
    env.run(body)


def test_context_lines_label_for_herself_in_prompts(env):
    async def body(e):
        bot = e.add_bot("900")
        e.llm.gate = lambda p: {"reply": True, "brief": "接"}
        e.llm.main = lambda s, u: "我说了一句"
        await e.say("111", "聊聊", at=["900"])
        await e.say("112", "再来一句", name="小红")
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"][-1]["messages"][0]["content"]
        assert "她（自己）: 我说了一句" in gk and "她（机器人）" not in gk
    env.run(body)


# ---------- 从模板建角色不重复复制人设；复制人设；重名加后缀 ----------
def test_duplicate_persona_is_independent_and_names_get_suffixes(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        db = e.core.db
        role = templates.instantiate(db, "gentle-friend")
        pid = role["persona"]
        orig = db.doc("persona", pid)
        c1 = await (await c.post(f"/api/personas/{pid}/copy")).json()
        c2 = await (await c.post(f"/api/personas/{pid}/copy")).json()
        assert [c1["name"], c2["name"]] == [orig["name"] + "2", orig["name"] + "3"] and "template" not in c1
        # 独立的副本：改副本的专属模块不影响原件
        orig_mods = [i["m"] for i in orig["items"] if db.doc("module", i["m"]).get("kind") == "exclusive"]
        copy_mods = [i["m"] for i in c1["items"] if db.doc("module", i["m"]).get("kind") == "exclusive"]
        assert orig_mods and not set(orig_mods) & set(copy_mods) and len(orig_mods) == len(copy_mods)
        before = db.doc("module", orig_mods[0])["body"]
        m = db.doc("module", copy_mods[0])
        db.put("module", dict(m, body="改过了"))
        assert db.doc("module", orig_mods[0])["body"] == before and db.doc("module", copy_mods[0])["body"] == "改过了"
        # 再从这个模板建角色，用的还是原件，不是副本
        assert templates.instantiate(db, "gentle-friend")["persona"] == pid
        # 人设页新建一个重名的：自动加后缀
        n1 = await (await c.put("/api/docs/persona", json={"name": "手写", "mode": "modules", "items": [], "whole": ""})).json()
        n2 = await (await c.put("/api/docs/persona", json={"name": "手写", "mode": "modules", "items": [], "whole": ""})).json()
        assert [n1["name"], n2["name"]] == ["手写", "手写2"]
        # 改已有人设的名字不会被当成新建、也不加后缀
        n2["name"] = "手写"
        assert (await (await c.put("/api/docs/persona", json=n2)).json())["name"] == "手写"
        await c.close()
    env.run(body)
