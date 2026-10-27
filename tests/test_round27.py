"""评审修复：向导用户卡不被清空、重启后接上状态、通知送到手、接口出错重试、守门员按用户卡生成、账号在群这一层、NapCat 进程清理、各处小修。"""
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import FakeChannel
from mb import customcmd, templates
from mb.channels import napcat
from mb.channels.base import Msg
from mb.db import since_day
from mb.engine import GATEKEEPER_PROMPT, Engine, Entry, fill
from mb.server import Server

ROOT = Path(__file__).resolve().parent.parent
HAITUN = "90001"


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def restart(e):
    """模拟程序重启：同一个库上起一个全新的引擎（内存里的东西都没了）。"""
    eng = Engine(e.core.db, e.core.settings, e.core.llm, emit=e.core.emit)
    eng.pace = 0
    return eng


def attach(eng, e, uid, role_id="r_haitun", platform="qq"):
    acc = e.core.db.doc("account", f"ac_{uid}")
    ch = FakeChannel(eng, acc, uid, f"bot{uid}")
    eng.add_bot(acc, ch)
    return ch


# ---------- 第 1 条：向导的用户卡 ----------
WIZ = r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const h = (tag, ...a) => { const n = { __n: true, tag, children: [] };
  for (const x of a.flat(Infinity)) { if (x == null || x === false) continue;
    if (typeof x === 'object' && x.__n) n.children.push(x); else if (typeof x === 'object') Object.assign(n, x); else n.children.push({ __n: true, tag: '#text', text: String(x), children: [] }); }
  return n; };
const text = n => (n.text || '') + n.children.map(text).join('');
const find = (n, f, out = []) => { if (f(n)) out.push(n); n.children.forEach(c => find(c, f, out)); return out; };
(async () => {
  const calls = { put: [] };
  let callsCb = null;
  const ctx = vm.createContext({ console, h, icon: () => null, callPresets: () => ['妈', '母上'], field: (l, el) => h('label', l, el),
    tagsInput: (v, cb) => { if (!callsCb) callsCb = cb; return h('tags'); }, toggle: () => null,
    add: (p, ...c) => { c.flat(Infinity).forEach(x => x && p.children.push(x)); }, clear: () => {}, toast: () => {},
    B: () => ({ people: JSON.parse(process.argv[3]) }), reloadBoot: async () => {},
    PUT: async (u, b) => { calls.put.push([u, b]); return b; }, S: {}, $: () => null });
  vm.runInContext(src, ctx);
  const W = vm.runInContext('Wizard', ctx);
  W.step = 3; W.next = () => { W.step++; }; W.nav = n => n;
  const body = h('div'); await W.sYou.call(W, body);
  const inputs = find(body, n => n.tag === 'input');
  inputs[0].value = '测试员'; inputs[1].value = '她妈';
  callsCb(['妈', '母上']);  // 点选了两个常用称呼
  await find(body, n => n.tag.startsWith('button') && text(n).includes('保存并下一步'))[0].onclick();
  console.log(JSON.stringify(calls.put[0][1]));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="没有 node")
def test_wizard_user_card_survives_on_a_new_database(env, tmp_path):
    """新库：种子里不跑关系方案迁移；向导把身份和称呼写进「全体角色」方案；保存后读回库里和拼出的人设，身份、称呼都在。"""
    async def body(e):
        db = e.core.db
        assert db.kv("schemes_v1")["new_db"] is True  # 新库不跑迁移
        card = next(p for p in db.docs("person") if p.get("self"))
        assert not card.get("schemes")  # 没有被迁移悄悄加上一套空方案
        f = tmp_path / "w.js"
        f.write_text(WIZ, encoding="utf-8")
        r = subprocess.run(["node", str(f), str(ROOT / "web/js/wizard.js"), json.dumps([card])], capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout.strip().splitlines()[-1])
        assert payload["identity"] == "她妈" and payload["calls"] == ["妈", "母上"]
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        saved = await (await c.put("/api/docs/person", json=payload)).json()
        got = next(p for p in e.core.db.docs("person") if p.get("self"))
        assert saved["identity"] == got["identity"] == "她妈" and got["calls"] == ["妈", "母上"] and got["name"] == "测试员"
        pv = await (await c.post("/api/personas/preview", json={"id": "p_海豚娘", "platform": "qq"})).json()
        text = json.dumps(pv, ensure_ascii=False)
        assert "她妈" in text and "妈、母上" in text, text[:300]
        await c.close()
    env.run(body)


def test_wizard_writes_into_the_all_roles_scheme_when_the_card_already_has_schemes(env, tmp_path):
    """旧数据：卡上已经有「全体角色」方案时，向导改的是那套方案（服务端会拿它盖回顶层字段），不会被盖回旧值。"""
    async def body(e):
        db = e.core.db
        card = next(p for p in db.docs("person") if p.get("self"))
        card["schemes"] = [{"id": "sc_1", "name": "默认", "identity": "", "calls": [], "attitude": {"presets": ["亲近贴心"], "custom": ""},
                            "scope": {"mode": "all", "tags": [], "roles": []}}]
        f = tmp_path / "w.js"
        f.write_text(WIZ, encoding="utf-8")
        if not shutil.which("node"):
            pytest.skip("没有 node")
        r = subprocess.run(["node", str(f), str(ROOT / "web/js/wizard.js"), json.dumps([card])], capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(r.stdout.strip().splitlines()[-1])
        assert payload["schemes"][0]["identity"] == "她妈" and payload["schemes"][0]["calls"] == ["妈", "母上"]
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        await c.put("/api/docs/person", json=payload)
        got = next(p for p in e.core.db.docs("person") if p.get("self"))
        assert got["identity"] == "她妈" and got["calls"] == ["妈", "母上"] and got["schemes"][0]["identity"] == "她妈"
        await c.close()
    env.run(body)


def test_old_database_still_gets_the_scheme_migration(env):
    """老库（没有标记）照常迁移一次，不改已有的东西。"""
    async def body(e):
        db = e.core.db
        db.del_kv("schemes_v1")
        card = next(p for p in db.docs("person") if p.get("self"))
        db.put("person", {**card, "identity": "她爸", "calls": ["爸"]})
        e.core.migrate_relation_schemes()
        got = next(p for p in db.docs("person") if p.get("self"))
        assert got["schemes"][0]["identity"] == "她爸" and got["identity"] == "她爸" and db.kv("schemes_v1").get("new_db") is None
    env.run(body)


# ---------- 第 2 条：重启后接上状态 ----------
def test_restart_restores_context_with_roles_mute_pause_counts_and_private_history(env):
    async def body(e):
        bot = e.add_bot("900")
        e.add_role("r_乙", "乙角色")
        eng = e.engine
        setg(e, "proactive", False)
        e.llm.main = lambda s, u: "我是海豚娘说的这句"
        await e.say("111", "你好", at=["900"])
        await e.say(HAITUN, "换乙角色", at=["900"])
        room = eng.rooms["qq:100"]
        # 管理员让她闭嘴 480 分钟；有人叫停；插话计数
        await e.say(HAITUN, "闭嘴 480", at=["900"])
        eng.on_stop(room, room.ctx[-1])
        b = eng.duty(eng.bots["ac_900"], room)
        for _ in range(3):
            eng.mark_proactive(b, room)
        # 私聊
        e.llm.main = lambda s, u: "私聊回复"
        await e.say("222", "私聊一句", private=True)
        before_hist = list(eng.private_hist["ac_900:222"])
        assert len(before_hist) == 2
        # ---- 重启 ----
        eng2 = restart(e)
        ch2 = attach(eng2, e, "900")
        r2 = eng2.room("qq", "100", "测试群")
        r2.accounts.add("ac_900")
        assert [x.text for x in r2.ctx if x.bot] and any(x.text == "你好" for x in r2.ctx)  # 上下文回填了
        said = [x for x in r2.ctx if x.bot and x.text == "我是海豚娘说的这句"]
        assert said and said[0].role == "r_haitun"  # 带着当时的角色
        other = eng2.duty(eng2.bots["ac_900"], r2)
        assert other.role["name"] == "乙角色"  # 临时换的角色本来就存库
        assert "海豚娘（这个号之前的角色）: 我是海豚娘说的这句" in eng2.ctx_lines(r2, other, "你")  # 按角色标注照常生效
        # 闭嘴、暂停跨重启有效
        assert r2.mute["ac_900"] > time.time() + 400 * 60 and not eng2.can_talk(other, r2)
        assert r2.pause_until > time.time()
        # 计数接得上（每小时、每天、账号合计）
        assert len(r2.hourly["ac_900"]) == 3 and r2.daily["ac_900"] == [time.strftime("%Y-%m-%d"), 3] and len(eng2.acct_hourly["ac_900"]) == 3
        # 私聊历史从库里回填
        assert eng2.dm_hist("ac_900", "222") == before_hist
        # 到期的不恢复
        eng2.db.set_kv("room_state:qq:999", {"mute": {"ac_900": time.time() - 5}, "pause_until": time.time() - 5})
        assert eng2.room("qq", "999", "旧群").mute == {} and eng2.rooms["qq:999"].pause_until == 0
        eng2.close()
    env.run(body)


def test_context_reset_is_not_undone_by_a_restart(env):
    async def body(e):
        e.add_bot("900")
        setg(e, "proactive", False)
        await e.say("111", "一句话", at=["900"])
        assert e.engine.rooms["qq:100"].ctx
        e.core.reset.run({"type": "room", "id": "qq:100"}, ["context"])
        eng2 = restart(e)
        assert not eng2.room("qq", "100", "测试群").ctx  # 清掉的上下文，重启后不会又被找回来
        e.core.reset.undo()
        eng3 = restart(e)
        assert eng3.room("qq", "100", "测试群").ctx
        eng2.close(); eng3.close()
    env.run(body)


def test_counts_are_pruned_and_persisted(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        room = eng.room("qq", "100", "测试群")
        b = eng.duty(eng.bots["ac_900"], room)
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        old = max(midnight, now - 5 * 3600)  # 5 小时前（今天）；刚过零点就取零点（要比一小时前早才测得出「每小时」不算它）
        if now - old <= 3600:
            pytest.skip("刚过零点不到一小时：造不出「今天、但在一小时前」的时刻")
        eng.bump(b, room, old)
        eng.bump(b, room, time.time())
        saved = e.core.db.kv("pro:qq:100")["ac_900"]
        assert len(saved) == 2
        eng2 = restart(e)
        r2 = eng2.room("qq", "100", "测试群")
        assert len(r2.hourly["ac_900"]) == 1 and len(eng2.acct_hourly["ac_900"]) == 1  # 一小时内的才算「每小时」
        eng2.close()
    env.run(body)


# ---------- 第 3 条：通知 ----------
def put_user(e, **kw):
    card = e.core.db.doc("person", "pe_user")
    e.core.db.put("person", {**card, "name": "用户", "self": True, **kw})
    e.engine.reload()


def test_notify_goes_to_the_user_card_via_another_account(env):
    async def body(e):
        qq = e.add_bot("900")
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        wx = FakeChannel(e.engine, acc, "wxid", "微信小号")
        e.engine.add_bot(acc, wx)
        put_user(e, qq=["12345"], wechat_remark=["小海"])
        e.engine.notify("QQ 123456789 被踢下线", e.core.db.doc("account", "ac_900"))
        await e.engine.idle()
        assert wx.sent and wx.sent[-1][:3] == ("private", "小海", "【机器人通知】QQ 123456789 被踢下线")  # 出事的是 QQ：走微信，按秘密备注
        assert qq.sent == []  # 出事的账号自己不发
        notes = e.core.db.kv("notices")
        assert len(notes) == 1 and notes[0]["read"] is False and notes[0]["via"] == "wechat"
        # 面板总览能看到未读通知；标成已读
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        got = await (await c.get("/api/notices")).json()
        assert [n["text"] for n in got] == ["QQ 123456789 被踢下线"] and not got[0]["read"]
        await c.post("/api/notices/read", json={})
        assert all(n["read"] for n in await (await c.get("/api/notices")).json())
        await c.close()
        # 微信出事、QQ 在线：走 QQ，按 QQ 号
        e.engine.notify("微信掉线了", e.core.db.doc("account", "ac_wx"))
        await e.engine.idle()
        assert qq.sent[-1][:3] == ("private", "12345", "【机器人通知】微信掉线了")
    env.run(body)


def test_notify_with_no_way_to_reach_the_user_still_leaves_a_panel_notice(env):
    async def body(e):
        qq = e.add_bot("900")
        put_user(e, qq=["12345"])
        e.engine.notify("只有一个号，出事的就是它", e.core.db.doc("account", "ac_900"))
        await e.engine.idle()
        assert qq.sent == [] and e.core.db.kv("notices")[0]["via"] == ""
        # 用户卡没填任何账号：不发
        put_user(e, qq=[])
        e.engine.notify("没填账号")
        await e.engine.idle()
        assert qq.sent == []
        # 关掉「发消息给我」：不发，面板上照样有
        put_user(e, qq=["12345"])
        setg(e, "notify_dm_owner", False)
        e.engine.notify("关了")
        await e.engine.idle()
        assert qq.sent == [] and len(e.core.db.kv("notices")) == 3
    env.run(body)


# ---------- 第 4、5、6、7、9、14 条 ----------
def test_two_schedules_in_the_same_minute_each_send_once(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        room = e.engine.room("qq", "100", "测试群")
        room.accounts.add("ac_900")
        hm = time.strftime("%H:%M")
        doc = e.core.db.doc("chat", "qq:100")
        doc["schedules"] = [{"time": hm, "mode": "text", "content": "早安", "on": True}, {"time": hm, "mode": "text", "content": "天气", "on": True}]
        e.core.db.put("chat", doc)
        await e.engine.tick()
        await e.engine.idle()
        await e.engine.tick()  # 同一分钟里 tick 会跑两轮
        await e.engine.idle()
        assert sorted(m[2] for m in bot.sent) == ["天气", "早安"]
    env.run(body)


def test_forget_command_needs_two_chars_and_is_backed_up(env):
    async def body(e):
        from mb import commands
        assert commands.parse("忘掉 我") == ("forget_me", "") and commands.parse("忘掉我") == ("forget_me", "")
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        mem = e.engine.memory
        sp = mem.space("recap", "r_haitun", "qq", "qq:100")
        mem.add(sp, "recap", "chat:qq:100", "群", "我们聊过披萨", "self", room="qq:100")
        mem.add(sp, "recap", "chat:qq:100", "群", "大家喜欢猫", "self", room="qq:100")
        n0 = e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0]
        await e.say(HAITUN, "忘掉 我", at=["900"])  # 中间多一个空格 = 忘掉我：管理员不执行，更不会按「我」去删
        assert e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == n0
        await e.say(HAITUN, "忘掉 的", at=["900"])  # 一个字：不执行，让她反问
        assert e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == n0
        assert any("太短" in r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='command'"))
        await e.say(HAITUN, "忘掉 披萨", at=["900"])
        assert e.core.db.x("SELECT COUNT(*) FROM memories WHERE content LIKE '%披萨%'").fetchone()[0] == 0
        assert e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == n0 - 1
        assert "备份" in bot.sent[-1][2]
        e.core.reset.undo()  # 面板上能撤销
        assert e.core.db.x("SELECT COUNT(*) FROM memories WHERE content LIKE '%披萨%'").fetchone()[0] == 1
    env.run(body)


def test_alias_of_a_builtin_command_follows_its_permission_by_default(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        r = customcmd.clean({"name": "睡觉", "pattern": "睡觉", "action": "builtin", "builtin": "mute", "arg": "480"})
        assert r["perm"]["mode"] == "follow"  # 不填权限：跟随内置指令（默认只有管理员）
        assert customcmd.clean({"name": "a", "pattern": "x", "text": "y"})["perm"]["mode"] == "all"  # 固定回复默认所有人
        assert customcmd.clean({"name": "a", "pattern": "x", "text": "y", "perm": {"mode": "follow"}})["perm"]["mode"] == "all"  # 「跟随」只对内置指令别名有意义
        e.core.db.set_kv("custom_commands", [r])
        room = e.engine.room("qq", "100", "测试群")
        await e.say("333", "睡觉", at=["900"])  # 普通群友：不执行
        assert room.mute == {}
        await e.say(HAITUN, "睡觉", at=["900"])  # 管理员：执行
        assert room.mute["ac_900"] > time.time() + 400 * 60
        # 明确选了「所有人」的，照旧所有人都能用
        room.mute.clear()
        e.core.db.set_kv("custom_commands", [dict(r, perm={"mode": "all", "people": [], "pgroup": ""})])
        await e.say("333", "睡觉", at=["900"])
        assert room.mute["ac_900"] > time.time()
    env.run(body)


def test_model_errors_are_retried_once_and_the_group_message_is_opt_in(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        e.llm.fail_next = 1  # 429 一次：自动重试，回复照常
        await e.say("111", "在吗", at=["900"])
        assert bot.sent[-1][2] == "收到"
        assert any("重试一次" in r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='model'"))
        n = len(bot.sent)
        e.llm.fail_next = 2  # 重试后仍然失败：默认不在群里发；面板上有一条通知
        await e.say("111", "再问", at=["900"])
        assert len(bot.sent) == n
        assert "模型接口出错" in e.core.db.kv("notices")[0]["text"]
        # 打开选项：发一句，同一个群十分钟内最多一次
        setg(e, "error_notice", True)
        e.llm.fail_next = 2
        await e.say("111", "又问", at=["900"])
        assert bot.sent[-1][2].startswith("（断线了") and len(bot.sent) == n + 1
        e.llm.fail_next = 2
        await e.say("111", "还问", at=["900"])
        assert len(bot.sent) == n + 1
        # 面板上的通知十分钟内同类只留一条
        assert len([x for x in e.core.db.kv("notices") if "模型接口出错" in x["text"]]) == 1
    env.run(body)


def test_retry_only_for_transient_errors():
    from mb.llm import LLM, LLMError

    class FakeLLM(LLM):
        RETRY_DELAY = 0

        def __init__(self, errors):
            self.errors, self.n, self.log = list(errors), 0, lambda *a, **k: None

        async def _post_once(self, url, headers, body, proxy=None):
            self.n += 1
            if self.errors:
                raise self.errors.pop(0)
            return {"ok": True}

    async def go():
        for err, calls in ((LLMError("HTTP 429: x"), 2), (LLMError("HTTP 503: x"), 2), (asyncio.TimeoutError(), 2), (LLMError("HTTP 401: key"), 1), (LLMError("HTTP 400: bad"), 1)):
            m = FakeLLM([err])
            try:
                await m._post("u", {}, {})
            except Exception:
                pass
            assert m.n == calls, (err, m.n)
        m = FakeLLM([LLMError("HTTP 500: x"), LLMError("HTTP 500: y")])  # 第二次还失败：抛出去，不再试
        with pytest.raises(LLMError):
            await m._post("u", {}, {})
        assert m.n == 2
    asyncio.run(go())


def test_usage_since_counts_today_as_the_first_day(env):
    async def body(e):
        import datetime
        today = datetime.date.today()
        assert since_day(1) == today.isoformat() and since_day(7) == (today - datetime.timedelta(days=6)).isoformat() and since_day(30) == (today - datetime.timedelta(days=29)).isoformat()
        db = e.core.db
        for d, n in ((today, 5), (today - datetime.timedelta(days=1), 7), (today - datetime.timedelta(days=6), 11), (today - datetime.timedelta(days=7), 13)):
            db.x("INSERT INTO usage (day, role_id, platform, room, purpose, model, calls, prompt, cached, completion) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (d.isoformat(), "r_haitun", "qq", "qq:100", "main", "m", n, 0, 0, 0))
        s = Server(e.core)
        assert sum(r["calls"] for r in s.usage_rows("day", 1)) == 5  # 「今天」只有今天
        assert sum(r["calls"] for r in s.usage_rows("day", 7)) == 5 + 7 + 11  # 「近 7 天」是 7 天，不是 8 天
        assert s.api2.usage_summary(1)["total"]["calls"] == 5 and s.api2.usage_summary(7)["total"]["calls"] == 23
    env.run(body)


def test_manual_send_is_not_blocked_by_shadow_mode(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "shadow", True, "role:r_haitun")
        room = e.engine.room("qq", "100", "测试群")
        room.accounts.add("ac_900")
        await e.say("111", "在吗", at=["900"])
        assert bot.sent == []  # 她自己要说的话被影子模式拦下
        await e.engine.manual_send("ac_900", "qq:100", "人工发的")
        assert bot.sent[-1][2] == "人工发的"  # 面板里手动发言照常发出去
    env.run(body)


def test_demo_you_is_the_user_card(env):
    async def body(e):
        await e.core.demo.start("r_haitun", chatter=False)
        setg(e, "proactive", False, "chat:demo:demo-room")
        eng = e.engine
        assert eng.people.find("demo", "demo-me") is eng.people.user_card() and eng.people.is_admin("demo", "demo-me")
        await e.core.demo.say("你", "闭嘴 5", at=True)
        await eng.idle()
        logs = [r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='command'")]
        assert any("（执行）" in t for t in logs) and not any("无权限" in t for t in logs)
        await e.core.demo.stop()
    env.run(body)


# ---------- 守门员模板按用户卡生成 ----------
OLD_GATEKEEPER = """你替{平台群}里的一位群友判断：没人@她，看到最新这条消息，她此刻要不要主动开口。

【她】{summary}记录里"她（自己）"是她自己之前发的{marks}同一个号换班之前由别的角色说的话，会标成"某某（这个号之前的角色）"，那不是她说的。

【最近群聊】
{context}

【最新消息】
{speaker}: {text}

按一个真人群友的分寸判断，目标是让这个群聊得更好玩。
该开口：有人提到她，或话里明摆着是冲她来的但没@（只凭语气、时机猜的不算）；有人问了问题没人接；有她接得住的梗、补得上的新信息或新角度、看不下去要对线的说法；她妈说了她有得接的话；群冷了而最新消息留了能接的口子。
不开口：别人正聊得热，她插进去只是打断；她刚说过而话头没再转向她；消息是@别人或回复别人的，话头没转向她；只能复读、附和、凑热闹；消息本身没东西可接（纯表情、签到、链接）。
只输出JSON：{"brief": "一句话理由；要开口就写她接哪个点", "reply": true 或 false}"""


def test_gatekeeper_sentence_comes_from_the_user_card(env):
    async def body(e):
        e.add_bot("900")
        eng = e.engine
        b = eng.duty(eng.bots["ac_900"], None)
        vals = dict(summary="摘要", marks="。", context="c", speaker="s", text="t", 平台群="QQ群")
        # 作者那样的用户卡：第一个称呼是「妈」→ 和原来的原文一字不差
        put_user(e, identity="她妈", call="妈", calls=["妈", "母上"])
        assert eng.user_line(b) == "她妈说了她有得接的话；"
        assert fill(GATEKEEPER_PROMPT, user_line=eng.user_line(b), **vals) == fill(OLD_GATEKEEPER, **vals)
        # 用别的称呼：跟着变
        put_user(e, identity="她爸", call="爸", calls=["爸"])
        assert "她爸说了她有得接的话；" in fill(GATEKEEPER_PROMPT, user_line=eng.user_line(b), **vals)
        # 没填称呼：整句去掉，前后衔接得上
        put_user(e, identity="", call="", calls=[])
        t = fill(GATEKEEPER_PROMPT, user_line=eng.user_line(b), **vals)
        assert eng.user_line(b) == "" and "说了她有得接的话" not in t and "看不下去要对线的说法；群冷了而最新消息" in t
        # 没有用户卡
        for p in e.core.db.docs("person"):
            if p.get("self"):
                e.core.db.put("person", {**p, "self": False})
        e.engine.reload()
        assert eng.user_line(b) == ""
        # 实际发给守门员的提示词里也是这样
        put_user(e, identity="她妈", call="妈", calls=["妈"])
        e.llm.gate = lambda p: {"reply": False, "brief": ""}
        await e.say("111", "随便说一句")
        gk = [c for c in e.llm.calls if c["_kind"] == "gk"][-1]["messages"][0]["content"]
        assert "她妈说了她有得接的话；" in gk
    env.run(body)


def test_neutral_wording_about_the_user_card():
    for f in ("web/js/wizard.js", "web/js/pages-people.js"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert "她是你的女儿" not in t and "关系方案" in t, f


# ---------- 「本群插话开关 / 力度」写到「这个账号在本群」----------
def test_proactive_command_is_per_account_in_chat_and_survives_shifts_and_restarts(env):
    async def body(e):
        b1, b2 = e.add_bot("900"), e.add_bot("901")
        e.add_role("r_乙", "乙角色")
        eng = e.engine
        await e.say(HAITUN, "插话关", at=["900"], account="ac_900")
        assert "只管我这个账号在本群" in b1.sent[-1][2]
        room = eng.rooms["qq:100"]
        a1, a2 = eng.duty(eng.bots["ac_900"], room), eng.duty(eng.bots["ac_901"], room)
        assert eng.S("proactive", a1, room) is False and eng.S("proactive", a2, room) is True  # 别的账号不受影响
        # 换班后照样有效（不管谁在岗）
        eng.duty_rules.set_override("ac_900", "qq:100", 9e9, role="r_乙")
        a1 = eng.duty(eng.bots["ac_900"], room)
        assert a1.role["name"] == "乙角色" and eng.S("proactive", a1, room) is False
        # 别的群不受影响
        assert eng.S("proactive", a1, eng.room("qq", "200", "另一个群")) is True
        # 存库：重启后还在；群页能看到（设置系统里有这一层）、能清除
        from mb.settings import Settings
        st2 = Settings(e.core.db)
        assert st2.get("proactive", "r_乙", "qq", "qq:100", "ac_900") is False
        view = {i["key"]: i for i in st2.view("acct:ac_900|chat:qq:100")}
        assert view["proactive"]["own"] is True and set(view) == {"proactive", "eagerness"}
        # 优先级：这一层比面板上「群」「角色在群」的设置都具体
        setg(e, "proactive", True, "chat:qq:100")
        setg(e, "proactive", True, "role:r_乙|chat:qq:100")
        assert eng.S("proactive", a1, room) is False
        # 清除后恢复按面板设置
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        await c.put("/api/settings", json={"scope": "acct:ac_900|chat:qq:100", "key": "proactive", "reset": True})
        assert eng.S("proactive", a1, room) is True
        await c.close()
        # 力度同理
        await e.say(HAITUN, "插话力度 5", at=["900"], account="ac_900")
        assert eng.S("eagerness", a1, room) == 5 and eng.S("eagerness", a2, room) == 3
    env.run(body)


# ---------- 一条消息最多几个机器人回：只限制主动插话的 ----------
def test_max_bots_per_message_only_limits_proactive_ones(env):
    async def body(e):
        eng = e.engine
        room = eng.room("qq", "100", "测试群")
        bots = [e.add_bot(u) for u in ("900", "901", "902", "903")]
        bs = [eng.duty(eng.bots[f"ac_{u}"], room) for u in ("900", "901", "902", "903")]
        setg(e, "max_bots_per_message", 2)
        called = [(b, "at", "at", None) for b in bs[:3]]
        chimed = [(b, "pro", None, "x") for b in bs[3:]]
        assert eng.cap_responders(called, chimed, room) == called  # 3 个被 @ 的都回，上限 2 也不截；没有名额给插话的
        assert eng.cap_responders(called[:1], [(b, "pro", None, "x") for b in bs[1:]], room) == called[:1] + [(bs[1], "pro", None, "x")]  # 1 个被 @：还剩 1 个名额给插话的
        assert eng.cap_responders([], [(b, "pro", None, "x") for b in bs], room) == [(b, "pro", None, "x") for b in bs[:2]]
        # 整条流程：同时 @ 三个，上限 1，三个都回
        setg(e, "max_bots_per_message", 1)
        setg(e, "proactive", False)
        await e.say("111", "你们三个都来", at=["900", "901", "902"])
        assert [len(b.sent) for b in bots] == [1, 1, 1, 0]
    env.run(body)


def test_settings_page_shows_old_program_import_only_when_detected():
    t = (ROOT / "web/js/pages-settings.js").read_text(encoding="utf-8")
    assert "!B().old_bot_guess ? null : h('div.card.form.mt16', h('h3', '从旧程序一键导入')" in t


# ---------- 文字和现行做法一致 ----------
def test_ui_and_docs_wording_matches_the_current_behaviour():
    js = {f: (ROOT / "web/js" / f).read_text(encoding="utf-8") for f in ("pages-new.js", "pages-people.js", "pages-main.js", "pages-data.js", "widgets.js", "wizard.js")}
    py = (ROOT / "mb/settings.py").read_text(encoding="utf-8") + (ROOT / "mb/engine.py").read_text(encoding="utf-8")
    docs = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    assert "连人设一起复制" not in js["pages-new.js"] and "不带构型" not in js["pages-new.js"] and "直接共用" in js["pages-new.js"]
    assert "角色还能对个人再覆盖" not in js["pages-people.js"] and "对各人的态度" not in js["pages-main.js"]
    for t in list(js.values()) + [py]:
        assert "作者的人物卡" not in t and "作者的记忆" not in t and "私聊作者" not in t and "旧程序原文" not in t and "沿用旧程序" not in t and "作者大号" not in t
    assert "GLM" not in js["pages-data.js"]
    assert "立刻私聊通知你" not in docs and "未读通知" in docs


# ---------- NapCat：认出来、记下来、停干净 ----------
def test_napcat_is_ours_only_for_this_account_and_never_the_users_own_qq():
    root = Path(r"C:\PodMates\data\napcat\NapCat.52230.Shell")
    boot = str(root / "NapCatWinBootMain.exe")
    assert napcat.is_ours("NapCatWinBootMain.exe", boot, [boot, "123456789"], root, "123456789")  # 一键包：启动器 + 号
    assert napcat.is_ours("NapCatWinBootMain.exe", boot, [boot, r"C:\QQ\QQ.exe", "hook.dll", "-q", "123456789"], root, "123456789")
    assert not napcat.is_ours("NapCatWinBootMain.exe", boot, [boot, "987654321"], root, "123456789")  # 同一个目录里别的号
    assert not napcat.is_ours("NapCatWinBootMain.exe", r"D:\Example\NapCatWinBootMain.exe", ["x", "123456789"], root, "123456789")  # 不在这个 NapCat 目录下
    qq = [str(root / "versions/9.9.33-52230/QQ.exe"), "--enable-logging", "-q", "123456789"]
    assert napcat.is_ours("QQ.exe", qq[0], qq, root, "123456789")  # 一键包的 QQ 本体（启动器已退出，不在进程树里）
    assert not napcat.is_ours("QQ.exe", qq[0], [qq[0], "--enable-logging", "-q", "987654321"], root, "123456789")  # 别的号
    own = [r"D:\Example\QQ\QQ.exe"]
    assert not napcat.is_ours("QQ.exe", own[0], own, root, "123456789")  # 用户自己开的 QQ：没有 --enable-logging 也没有 -q
    assert not napcat.is_ours("QQ.exe", own[0], own + ["-q", "123456789"], root, "123456789")  # 就算带了 -q，没有启动器加的 --enable-logging 也不算
    assert not napcat.is_ours("notepad.exe", r"C:\Windows\notepad.exe", ["notepad.exe"], root, "123456789")


def test_napcat_pid_file_records_and_reap_kills_only_matching_processes(tmp_path, monkeypatch):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(napcat, "data_dir", lambda: tmp_path)
    import sys
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    a, b = subprocess.Popen(sleeper), subprocess.Popen(sleeper)
    try:
        time.sleep(0.5)
        ta, tb = psutil.Process(a.pid).create_time(), psutil.Process(b.pid).create_time()
        napcat.save_pids("111", {a.pid: ta})
        assert napcat.load_pids() == {"111": [[a.pid, ta]]}
        napcat.save_pids("222", {b.pid: tb + 500})  # 创建时间对不上 = pid 被别的进程复用了：不能碰
        gone = napcat.reap(Path(tmp_path / "x"), "111")
        assert gone == [a.pid] and a.wait(5) is not None and b.poll() is None
        assert "111" not in napcat.load_pids()
        napcat.reap(Path(tmp_path / "x"), "222")
        assert b.poll() is None  # 创建时间对不上，没被结束
        napcat.save_pids("222", {})
        assert napcat.load_pids() == {}
    finally:
        for p in (a, b):
            p.kill()


def test_napcat_login_hints_name_the_step_that_failed():
    h = napcat.login_hint
    assert h("10-25 00:49:06 [info] 正在快速登录  123456789")[0] == "info"
    lvl, msg = h("快速登录失败，未找到该 QQ 历史登录记录，将尝试密码回退登录")
    assert lvl == "warn" and "Windows 用户" in msg
    assert h("没有 -q 指令指定快速登录，将使用二维码登录方式")[0] == "warn"
    assert "已经在另一个 QQ 进程里登录" in h("当前账号(123456789)已登录,无法重复登录")[1]
    assert h("1. 123456789 示例小号")[1].startswith("这台电脑的 QQ 里可以快速登录的号")
    assert h("本账号数据/缓存目录： D:\\ExampleData\\QQChatData\\Tencent Files\\NapCat\\data")[1].endswith("Tencent Files\\NapCat\\data")
    assert h("[info] [PacketHandler] 初始化成功") is None


@pytest.mark.skipif(not shutil.which("powershell"), reason="没有 powershell")
def test_installer_stop_script_only_kills_what_is_under_the_roots_or_recorded(tmp_path):
    psutil = pytest.importorskip("psutil")
    ping = Path(r"C:\Windows\System32\ping.exe")
    app, other = tmp_path / "fake app", tmp_path / "other"
    app.mkdir(); other.mkdir()
    shutil.copy(ping, app / "helper.exe"); shutil.copy(ping, other / "mine.exe"); shutil.copy(ping, other / "rec.exe"); shutil.copy(ping, other / "stale.exe")
    args = ["-n", "300", "127.0.0.1"]
    ps = {n: subprocess.Popen([str(d / f"{n}.exe"), *args], stdout=subprocess.DEVNULL) for n, d in (("helper", app), ("mine", other), ("rec", other), ("stale", other))}
    try:
        time.sleep(1)
        pf = tmp_path / "pids.json"
        pf.write_text(json.dumps({"1": [[ps["rec"].pid, psutil.Process(ps["rec"].pid).create_time()],
                                        [ps["stale"].pid, psutil.Process(ps["stale"].pid).create_time() + 500]]}), encoding="utf-8")
        script = ROOT / "installer" / "stop-podmates.ps1"
        dry = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Roots", str(app), "-PidFiles", str(pf), "-DryRun"], capture_output=True)
        assert dry.returncode == 0 and all(p.poll() is None for p in ps.values())  # 只看不动
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Roots", str(app), "-PidFiles", str(pf)], capture_output=True)
        assert r.returncode == 0
        time.sleep(1)
        assert ps["helper"].poll() is not None and ps["rec"].poll() is not None  # 安装目录下的、记下来的：结束了
        assert ps["mine"].poll() is None and ps["stale"].poll() is None  # 目录外的、创建时间对不上的：没碰
    finally:
        for p in ps.values():
            p.kill()


def test_installer_runs_the_stop_script_before_installing_and_uninstalling():
    iss = (ROOT / "installer/PodMates.iss").read_text(encoding="utf-8-sig")
    assert "ExtractTemporaryFile('stop-podmates.ps1')" in iss and "stop-podmates.ps1" in iss.split("[Files]")[1].split("[Icons]")[0]
    assert iss.count("StopApp(") >= 3 and "-Roots" in iss and "napcat-pids.json" in iss
    raw = (ROOT / "installer/stop-podmates.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # 带 BOM：Windows PowerShell 5.1 才会按 UTF-8 读中文


# ---------- 微信：一次回复分几条发，只切一次会话 ----------
def test_requirements_list_psutil():
    assert "psutil" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
