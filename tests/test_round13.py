"""补充十三：她是「使用者本人」的女儿（{{用户}}）、人物表里的「用户」标记、向导的「她该怎么称呼你」。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mb import personas, templates
from mb.server import Server

ROOT = Path(__file__).resolve().parent.parent


def put_user(e, name="海豚", pid="pe_me"):
    for o in e.core.db.docs("person"):  # 全表只有一张用户卡（接口里会自动取消别的；这里直接写库，要自己取消）
        if o.get("self") and o["id"] != pid:
            o["self"] = False
            e.core.db.put("person", o)
    e.core.db.put("person", dict(id=pid, name=name, identity="她妈", call="妈", qq=[], discord=[], wechat_ids=[], wechat_remark=[], nicks={}, note="", knows="",
                                 attitude={"presets": [], "custom": ""}, admin=True, **{"self": True}))
    e.engine.reload()


def text_of(e, pid, platform="qq"):
    return personas.assemble(e.engine.personas[pid], e.engine.modules, platform, {"people": "", "memory": ""}, user=e.engine.people.user_name())


def make_role(e, tid, rid):
    role = templates.instantiate(e.core.db, tid, role_id=rid, persona_id="p_" + tid)
    e.engine.reload()
    return role


def test_placeholder_substitution_and_missing_user_card():
    t = "你是一只泡在{{平台群}}里的海豚娘。你是「{{用户}}」的女儿。\n\n【她是谁】\n- 自称。\n- 是AI，被问就认；是AI也是{{用户}}的女儿，不冲突。\n- 她怎么成了{{用户}}的家人，她自己也觉得离谱，被问就随口编个说法。"
    got = personas.subst(t, "wechat", "海豚")
    assert got.startswith("你是一只泡在微信群里的海豚娘。你是「海豚」的女儿。") and "是AI也是海豚的女儿，不冲突。" in got and "{{" not in got
    # 没有用户卡：含 {{用户}} 的整句不出现（句子以 。！？； 或换行为界），别的句子原样；行尾剩下的「；」改成「。」；整行都没了就整行去掉
    none = personas.subst(t, "wechat", None)
    assert none == "你是一只泡在微信群里的海豚娘。\n\n【她是谁】\n- 自称。\n- 是AI，被问就认。"  # 身份句留着，「谁的女儿」那句没了 and "女儿" not in none and "{{" not in none
    assert personas.subst("她是{{用户}}的女儿！但是她很乖。", "qq", None) == "但是她很乖。"
    assert personas.subst("没有占位符的一句话。", "qq", None) == "没有占位符的一句话。"
    # 保留占位符（转成整体文字、编辑时）：什么都不换
    assert "{{用户}}" in personas.assemble({"mode": "whole", "whole": t}, {}, "qq", {}, keep_words=True)


def test_only_one_user_card_in_the_table(env):
    async def body(e):
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        base = dict(name="x", identity="", call="", qq=[], discord=[], wechat_remark=[], nicks={}, note="", knows="", attitude={"presets": [], "custom": ""})
        a = await (await c.put("/api/docs/person", json=dict(base, id="pe_a", name="甲", **{"self": True}))).json()
        b = await (await c.put("/api/docs/person", json=dict(base, id="pe_b", name="乙"))).json()
        assert e.engine.people.user_name() == "甲"
        await c.put("/api/docs/person", json=dict(base, id="pe_b", name="乙", **{"self": True}))  # 再给乙打上：甲自动取消
        people = {p["id"]: p for p in e.core.db.docs("person")}
        assert [i for i, p in people.items() if p.get("self")] == ["pe_b"] and e.engine.people.user_name() == "乙"
        await c.close()
    env.run(body)


def test_import_old_owner_card_is_the_user_card(env):
    from mb.core import Core
    card = Core.user_card_from_old("【她妈】\n「海豚」是她妈，QQ昵称「某某」，她对妈说话亲近。\n\n", ["1001"])
    assert card["self"] is True and card["name"] == "海豚"


# ---------- 向导：她该怎么称呼你（可以跳过）----------
WIZ_JS = r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const h = (tag, ...a) => { const n = { __n: true, tag, children: [] };
  for (const x of a.flat(Infinity)) { if (x == null || x === false) continue;
    if (typeof x === 'object' && x.__n) n.children.push(x); else if (typeof x === 'object') Object.assign(n, x); else n.children.push({ __n: true, tag: '#text', text: String(x), children: [] }); }
  return n; };
const text = n => (n.text || '') + n.children.map(text).join('');
const find = (n, f, out = []) => { if (f(n)) out.push(n); n.children.forEach(c => find(c, f, out)); return out; };
(async () => {
  const calls = { put: [], toast: [] };
  const ctx = vm.createContext({ console, h, icon: () => null, callPresets: () => [], field: (l, el, help) => h('label', l, el), tagsInput: (v, cb) => h('tags'), toggle: () => null,
    add: (p, ...c) => { c.flat(Infinity).forEach(x => x && p.children.push(x)); }, clear: () => {},
    toast: (t, k) => calls.toast.push([t, k || '']), B: () => ({ people: JSON.parse(process.argv[3]) }), reloadBoot: async () => {},
    PUT: async (u, b) => { calls.put.push([u, b]); return b; }, S: {}, $: () => null });
  vm.runInContext(src, ctx);
  const W = vm.runInContext('Wizard', ctx);
  const out = { steps_src: /const steps = \[([^\]]+)\]/.exec(src)[1] };
  // 跳过
  W.step = 3; W.next = () => { W.step++; }; W.nav = n => n;
  let body = h('div'); await W.sYou.call(W, body);
  const skip = find(body, n => n.tag.startsWith('button') && text(n) === '跳过这一步')[0];
  skip.onclick(); out.after_skip = W.step; out.put_after_skip = calls.put.length;
  // 名字空：保存被拒
  W.step = 3; body = h('div'); await W.sYou.call(W, body);
  const save = find(body, n => n.tag.startsWith('button') && text(n).includes('保存并下一步'))[0];
  await save.onclick(); out.empty_name = [W.step, calls.put.length, calls.toast.length && calls.toast[0][1]];
  // 填了名字：建卡（带「用户」和管理员），进入下一步
  find(body, n => n.tag === 'input')[0].value = '海豚'; find(body, n => n.tag === 'input')[1].value = '她妈';
  await save.onclick(); out.saved = [W.step, calls.put.length && calls.put[0][0], calls.put.length && calls.put[0][1].self, calls.put[0] && calls.put[0][1].admin, calls.put[0] && calls.put[0][1].name, calls.put[0] && calls.put[0][1].identity];
  out.payload = calls.put[0] && calls.put[0][1];
  console.log(JSON.stringify(out));
})();
"""


def run_wizard(tmp_path, people="[]"):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    f = tmp_path / "w.js"
    f.write_text(WIZ_JS, encoding="utf-8")
    r = subprocess.run(["node", str(f), str(ROOT / "web/js/wizard.js"), people], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_wizard_you_step_can_be_skipped_and_creates_user_card(tmp_path):
    out = run_wizard(tmp_path)
    assert "'用户卡'" in out["steps_src"] and out["steps_src"].index("'角色'") < out["steps_src"].index("'用户卡'") < out["steps_src"].index("'记忆'")
    assert out["after_skip"] == 4 and out["put_after_skip"] == 0  # 跳过：进下一步，什么都不建
    assert out["empty_name"] == [3, 0, "err"]  # 名字空着点保存：提示，不建，不前进
    assert out["saved"] == [4, "/api/docs/person", True, True, "海豚", "她妈"]
