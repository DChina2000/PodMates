"""补充十四：拉康派那一行、固定的「用户」卡（默认带、不能删、名字为空不拼空句子）、界面上「用户 · 名字」、多个称呼。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mb.engine import Entry
from mb.server import Server
from test_round13 import make_role, put_user, run_wizard, text_of

ROOT = Path(__file__).resolve().parent.parent
LACAN = "- 拉康派：精神分析是她看人的眼睛，底子里一直在用，嘴上很少挂术语，真到一针见血的时候才点一个词；有人真想聊理论就认真聊，说人话。"


def test_lacan_line_is_verbatim_in_seed_and_assembly(env):
    seed = (ROOT / "assets/seed/persona_core.md").read_text(encoding="utf-8")
    assert LACAN in seed.split("\n") and "大他者" not in seed.split("【她看世界的根本方式】")[0] and "对线时尤其好使" not in seed

    async def body(e):
        make_role(e, "haitun", "r_ht")
        put_user(e, "海豚")
        t = text_of(e, "p_haitun")  # 海豚娘用的是共用的拉康派模块
        assert LACAN in t.split("\n") and "大他者" not in t and "能指链" not in t
        assert e.engine.modules["m_拉康派"]["body"] == LACAN
    env.run(body)


def test_fresh_install_has_one_fixed_empty_user_card(env):
    async def body(e):
        cards = [p for p in e.core.db.docs("person") if p.get("self")]
        assert len(cards) == 1
        c = cards[0]
        assert c["id"] == "pe_user" and c["admin"] is True and c["attitude"]["presets"] == ["亲近贴心"] and c["attitude"]["custom"] == ""
        for k in ("name", "identity", "call", "note", "knows"):
            assert c[k] == "", k
        for k in ("calls", "qq", "discord", "wechat_ids", "wechat_remark"):
            assert c[k] == [], k
        assert c["nicks"] == {}
        # 再检查一次不会多出第二张；已经有用户卡的老数据也不会再建
        e.core.ensure_user_card()
        assert len([p for p in e.core.db.docs("person") if p.get("self")]) == 1
    env.run(body)


def test_empty_name_never_builds_empty_sentences(env):
    async def body(e):
        make_role(e, "haitun", "r_ht")
        ppl = e.engine.people
        assert ppl.user_name() is None
        t = text_of(e, "p_haitun")
        assert t.startswith("你是一只泡在QQ群里的海豚娘。\n")  # 身份句始终保留，只有「谁的女儿」那句没了
        assert "「」" not in t and "女儿" not in t and "{{" not in t and "称呼：" not in t
        assert all(line.strip() not in ("-", "；", "。", "，") for line in t.split("\n"))
        # 空白用户卡在场也不生成人物卡段落、不进「名字带〔×本人〕」
        card = ppl.user_card()
        assert ppl.card_text(card) == "" and ppl.section([card], []) == "" and ppl.marks([card], []) == ""
        # 只填了账号没填名字：认得出人，但不会拼出「〔本人〕」
        card["qq"] = ["777"]
        e.core.db.put("person", card)
        e.engine.reload()
        name, p = e.engine.display_name("qq", "777", "某人")
        assert name == "某人"
        # 空名字不会因为「空串在任何话里都出现」而被当成被提到了
        room = e.engine.room("qq", "100", "群")
        room.ctx.append(Entry("999", "路人", "随便一句话"))
        persons, _, _, _ = e.engine.present(room, e.engine.duty(e.engine.bots[e.add_bot("900").account["id"]], room))
        assert not any(x["id"] == "pe_user" for x in persons)
        # 填完名字：拼装正确
        put_user(e, "老王")
        assert text_of(e, "p_haitun").startswith("你是一只泡在QQ群里的海豚娘。你是「老王」的女儿。")
    env.run(body)


def test_user_card_cannot_be_deleted_but_others_can(env):
    async def body(e):
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        e.core.db.put("person", dict(id="pe_x", name="路人", identity="", call="", qq=[], discord=[], wechat_remark=[], nicks={}, note="", knows="",
                                     attitude={"presets": [], "custom": ""}))
        r = await c.delete("/api/docs/person/pe_user")
        assert r.status == 400 and "不能删" in await r.text() and e.core.db.doc("person", "pe_user")
        assert (await c.delete("/api/docs/person/pe_x")).status == 200
        # 就算直接从库里删了，下次也会补回来
        e.core.db.delete("person", "pe_user")
        e.core.ensure_user_card()
        assert e.core.db.doc("person", "pe_user")
        await c.close()
    env.run(body)


def test_no_special_handling_for_any_specific_person():
    """代码、种子、模板里不能出现针对某个具体的人的东西：一切特殊性只在用户卡的数据里。
    判据：这几处不许写死某张具体人物卡的 id（`pe_` 开头、后面不是用户卡自己的 `pe_user`）。"""
    person_id = re.compile(r"\bpe_(?!user\b)[A-Za-z0-9_]+")
    hits = []
    for pat in ("mb/*.py", "mb/channels/*.py", "web/js/*.js", "web/index.html", "assets/seed/*", "assets/templates/*.md"):
        for f in ROOT.glob(pat):
            t = f.read_text(encoding="utf-8")
            hits += [(f.name, m.group(0)) for m in person_id.finditer(t)]
    assert not hits, hits
    assert not hasattr(__import__("mb.core", fromlist=["Core"]).Core, "owner_card")
    # 「海豚」这个字只在模板（是内容）和导入旧程序的文件名里出现，别处没有
    for f in list(ROOT.glob("mb/*.py")) + list(ROOT.glob("mb/channels/*.py")):
        if f.name in ("templates.py", "core.py"):
            continue
        assert "海豚" not in f.read_text(encoding="utf-8"), f.name


# ---------- 多个称呼 ----------
def test_multiple_calls_are_listed_for_the_model_and_single_call_still_works(env):
    async def body(e):
        ppl = e.engine.people
        base = dict(id="pe_c", name="海豚", identity="她妈", qq=["1"], discord=[], wechat_remark=[], nicks={}, note="", knows="",
                    attitude={"presets": ["亲近贴心"], "custom": ""})
        multi = ppl.card_text(dict(base, calls=["妈", "妈妈", "母上", "母上大人", "海豚妈妈"], call="妈"))
        assert "称呼：妈、妈妈、母上、母上大人、海豚妈妈，随场合换着叫。" in multi
        assert "对她妈说话" in multi and "对妈说话" not in multi  # 多个称呼时「对谁说话」用身份，不挑其中一个
        single_legacy = ppl.card_text(dict(base, call="妈"))  # 旧数据：只有一个称呼，写法和以前完全一样
        assert "称呼：" not in single_legacy and "对妈说话亲近贴心" in single_legacy
        assert ppl.card_text(dict(base, calls=["妈"], call="妈")) == single_legacy  # 新数据只填一个也一样
        no_att = ppl.card_text(dict(base, attitude={"presets": [], "custom": ""}, calls=["妈", "妈妈"], call=""))
        assert "称呼：妈、妈妈，随场合换着叫。" in no_att and "对她妈说话" not in no_att
        assert ppl.calls_of(dict(call=" 妈 ")) == ["妈"] and ppl.calls_of(dict(calls=["", " 爸 "])) == ["爸"] and ppl.calls_of({}) == []
        # 点名认人：任何一个称呼（两个字以上）出现在话里都算提到了她
        e.core.db.put("person", dict(base, id="pe_c", calls=["妈", "母上"], call="妈"))
        e.engine.reload()
        room = e.engine.room("qq", "100", "群")
        room.ctx.append(Entry("999", "路人", "母上大人今天好"))
        persons, _, _, _ = e.engine.present(room, e.engine.duty(e.engine.bots[e.add_bot("900").account["id"]], room))
        assert any(p["id"] == "pe_c" for p in persons)
    env.run(body)


JS_HELPERS = r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const i = src.indexOf('async function reloadBoot()'), j = src.indexOf('function callPresets');
const code = src.slice(i, src.indexOf('\n', j) + 1);
const S = { boot: null };
const ctx = vm.createContext({ S, GET: async () => ({ people: JSON.parse(process.argv[3]) }), fire: () => {}, console });
vm.runInContext(code, ctx);
(async () => {
  const out = {};
  await vm.runInContext('reloadBoot()', ctx);
  out.order = S.boot.people.map(p => p.id);
  const pl = vm.runInContext('pLabel', ctx), cp = vm.runInContext('callPresets', ctx);
  out.labels = S.boot.people.map(p => pl(p));
  out.presets = { mom: cp('她妈'), dad: cp(' 她爸 '), other: cp('老同学'), none: cp('') };
  out.disp = vm.runInContext('dispText', ctx)('小明〔海豚本人〕: 你好 / 路人〔路人甲本人〕');
  console.log(JSON.stringify(out));
})();
"""


def test_user_card_display_and_call_presets_in_the_panel(tmp_path):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    f = tmp_path / "h.js"
    f.write_text(JS_HELPERS, encoding="utf-8")
    people = [{"id": "a", "name": "路人甲"}, {"id": "u", "name": "海豚", "self": True}, {"id": "b", "name": "路人乙"}]
    r = subprocess.run(["node", str(f), str(ROOT / "web/js/core.js"), json.dumps(people, ensure_ascii=False)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["order"] == ["u", "a", "b"]  # 用户卡排最前，别的卡顺序不变
    assert out["labels"] == ["用户 · 海豚", "路人甲", "路人乙"]
    assert out["presets"]["mom"] == ["妈", "妈妈", "老妈", "母上", "母上大人"] and out["presets"]["dad"] == ["爸", "爸爸", "老爸", "父上", "父上大人"]
    assert out["presets"]["other"] == [] and out["presets"]["none"] == []
    assert out["disp"] == "小明〔用户 · 海豚〕: 你好 / 路人〔路人甲本人〕"  # 只有用户卡的名字换成「用户 · 名字」
    r = subprocess.run(["node", str(f), str(ROOT / "web/js/core.js"), json.dumps([{"id": "u", "name": "", "self": True}])], capture_output=True, text=True, encoding="utf-8")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["labels"] == ["用户"] and out["disp"] == "小明〔海豚本人〕: 你好 / 路人〔路人甲本人〕"  # 名字没填：只显示「用户」；不会乱换
    # 列出人物的地方都用了 pLabel / dispText
    for f_, s in (("pages-people.js", "pLabel(p)"), ("pages-main.js", "pLabel(p)"), ("pages-commands.js", "pLabel(p)"), ("widgets.js", "pLabel(p)"),
                  ("pages-persona.js", "pLabel(x)"), ("pages-tools.js", "pLabel(p)"), ("pages-main.js", "dispText("), ("pages-data.js", "dispText(")):
        assert s in (ROOT / "web/js" / f_).read_text(encoding="utf-8"), (f_, s)


def test_wizard_fills_the_seeded_card_keeping_attitude(tmp_path):
    seeded = {"id": "pe_user", "name": "", "identity": "", "call": "", "calls": [], "qq": [], "discord": [], "wechat_remark": [], "nicks": {}, "note": "", "knows": "",
              "attitude": {"presets": ["亲近贴心"], "custom": ""}, "admin": True, "self": True, "groups": []}
    out = run_wizard(tmp_path, json.dumps([seeded]))
    pl = out["payload"]
    assert pl["id"] == "pe_user" and pl["name"] == "海豚" and pl["identity"] == "她妈" and pl["self"] and pl["admin"]  # 填的是那张固定的卡，不另建
    assert pl["attitude"] == {"presets": ["亲近贴心"], "custom": ""}  # 「亲近贴心」默认勾选，向导不动它
    wiz = (ROOT / "web/js/wizard.js").read_text(encoding="utf-8")
    assert "用户卡" in wiz and "callPresets(ident.value)" in wiz and "calls = calls.includes(c)" in wiz
