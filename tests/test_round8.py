"""补充七：重置记忆（范围×内容只删该删的、先备份、撤销能完整恢复、不碰人物表/人设/设置/用量）。"""
import os
import time

import pytest

from mb.engine import Entry
from mb.reset import CATS


def seed(e):
    """两个群、两个记忆空间、一个自建空间；各类记忆、向量、表情包、提醒、聊天上下文。"""
    db, eng = e.core.db, e.engine
    for key in ("qq:100", "qq:200"):
        eng.room("qq", key.split(":")[1], "群" + key[-3:])
    mem = eng.memory
    ids = {}
    ids["p100"] = mem.add("role:r_haitun", "person", "person:pe_haitun", "海豚", "喜欢猫", "self", None, "qq:100", status="active")
    ids["p100b"] = mem.add("role:r_haitun", "person", "uid:qq:111", "路人甲", "在考研", "self", None, "qq:100", status="active")
    ids["p200"] = mem.add("role:r_haitun", "person", "uid:qq:222", "路人乙", "爱看番", "self", None, "qq:200", status="active")
    ids["r100"] = mem.add("chat:qq:100", "recap", "", "", "甲群前情", "inferred", None, "qq:100", status="active")
    ids["r200"] = mem.add("chat:qq:200", "recap", "", "", "乙群前情", "inferred", None, "qq:200", status="active")
    ids["m100"] = mem.add("chat:qq:100", "meme", "下班", "下班", "老王天天喊", "inferred", None, "qq:100", status="active")
    ids["d100"] = mem.add("chat:qq:100", "doc", "手册", "手册", "文件片段内容", "inferred", None, "qq:100", status="active")
    mem.set_style("qq:100", "句子很短")
    mem.set_style("qq:200", "句子很长")
    db.set_kv("style_n:qq:100", 40)
    for mid in ids.values():
        db.x("INSERT OR REPLACE INTO memvec VALUES (?,?,?)", (mid, "sig", b"\x00\x01\x02" * 4))
    folder = eng.stickers.folder()
    for i, room in enumerate(("qq:100", "qq:200")):
        fn = f"reset_test_{i}.png"
        (folder / fn).write_bytes(b"PNG" + bytes([i]) * 10)
        mid = mem.add(f"chat:{room}", "sticker", f"{i:016x}", "表情包", f"表情{i}", "inferred", None, room, status="active")
        db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, file, size, uses, mem_id, descr)
                VALUES (?,?,?,?,?,?,?,'learned',?,?,?,?,?)""", (f"chat:{room}", room, f"{i:016x}", 3, "[]", 1, 2, fn, 13, i, mid, f"表情{i}"))
    now = time.time()
    eng.reminders.add("qq", "group", "100", "ac_900", "111", "路人甲", "交作业", now + 600)
    eng.reminders.add("qq", "group", "200", "ac_900", "222", "路人乙", "开会", now + 900)
    eng.reminders.add("qq", "group", "100", "ac_900", "90001", "海豚", "喝水", now + 300)
    for key in ("qq:100", "qq:200"):
        for i in range(3):
            eng.rooms[key].ctx.append(Entry("111", "甲", f"{key}的话{i}"))
    eng.private_hist["ac_900:111"] = [{"role": "user", "content": "私聊"}]
    return ids


def untouched(e):
    """人物表、人设、设置、用量的快照：重置前后必须一样。"""
    db = e.core.db
    return (db.docs("person"), db.docs("persona"), db.docs("module"), db.docs("role"), e.core.settings.data,
            db.rows("SELECT * FROM usage"), db.rows("SELECT * FROM messages"))


def count(e, sql, *a):
    return e.core.db.x(sql, a).fetchone()[0]


def test_reset_all_everything_but_not_people_personas_settings_usage(env):
    async def body(e):
        e.add_bot("900")
        seed(e)
        e.core.db.add_usage("r_haitun", "qq", "qq:100", "main", "m", 100, 0, 10)
        before = untouched(e)
        pv = e.core.reset.preview({"type": "all"}, list(CATS))
        assert pv["counts"] == {"person": 3, "recap": 2, "meme": 1, "doc": 1, "style": 2, "sticker": 2, "reminder": 3, "context": 7}
        assert count(e, "SELECT COUNT(*) FROM memories") > 0  # 预览不删
        info = e.core.reset.run({"type": "all"}, list(CATS))
        assert info["counts"] == pv["counts"]
        db = e.core.db
        assert count(e, "SELECT COUNT(*) FROM memories") == 0 and count(e, "SELECT COUNT(*) FROM memvec") == 0
        assert count(e, "SELECT COUNT(*) FROM stickers") == 0 and count(e, "SELECT COUNT(*) FROM reminders") == 0
        assert not (e.engine.stickers.folder() / "reset_test_0.png").exists()
        assert all(not r.ctx for r in e.engine.rooms.values()) and not e.engine.private_hist
        assert db.kv("style_n:qq:100") == 0
        assert untouched(e) == before  # 人物表、人设、模块、角色、设置、用量、聊天记录一行不动
        # 备份：现有备份体系里有整库备份，另有可撤销的重置备份
        assert list((e.core.data / "backups").glob("pre-reset-*.db")) and (e.core.data / "backups" / info["folder"] / "data.json").exists()
    env.run(body)


def test_reset_room_scope_and_kind_subsets(env):
    async def body(e):
        e.add_bot("900")
        ids = seed(e)
        db = e.core.db
        # 某个群 + 只选群梗和提醒
        c = e.core.reset.run({"type": "room", "id": "qq:100"}, ["meme", "reminder"])["counts"]
        assert c == {"meme": 1, "reminder": 2}
        assert count(e, "SELECT COUNT(*) FROM memories WHERE kind='meme'") == 0 and count(e, "SELECT COUNT(*) FROM memvec WHERE mem_id=?", ids["m100"]) == 0
        assert count(e, "SELECT COUNT(*) FROM memories WHERE kind='recap'") == 2 and count(e, "SELECT COUNT(*) FROM memories WHERE kind='person'") == 3
        assert [r["chat_id"] for r in db.rows("SELECT chat_id FROM reminders")] == ["200"]
        assert len(e.engine.rooms["qq:100"].ctx) == 3  # 没选「聊天上下文」就不动
        # 某个群 + 全部内容：只动这个群，另一个群完好
        e.core.reset.run({"type": "room", "id": "qq:100"}, list(CATS))
        assert count(e, "SELECT COUNT(*) FROM memories WHERE room='qq:100'") == 0
        assert count(e, "SELECT COUNT(*) FROM memories WHERE room='qq:200'") == 4  # 乙群：人物备忘、前情、风格、表情包
        assert not e.engine.rooms["qq:100"].ctx and len(e.engine.rooms["qq:200"].ctx) == 3
        assert count(e, "SELECT COUNT(*) FROM stickers") == 1 and (e.engine.stickers.folder() / "reset_test_1.png").exists()
        assert e.engine.memory.style_text("qq:200") == "句子很长" and e.engine.memory.style_text("qq:100") == ""
        assert e.engine.private_hist  # 私聊上下文不属于某个群
    env.run(body)


def test_reset_space_and_person_scope(env):
    async def body(e):
        e.add_bot("900")
        ids = seed(e)
        db = e.core.db
        # 某个记忆空间：只删这个空间里的东西
        c = e.core.reset.run({"type": "space", "id": "chat:qq:200"}, list(CATS))["counts"]
        assert c["recap"] == 1 and c["style"] == 1 and c["sticker"] == 1 and c["person"] == 0 and c["reminder"] == 0
        assert count(e, "SELECT COUNT(*) FROM memories WHERE space='chat:qq:200'") == 0
        assert count(e, "SELECT COUNT(*) FROM memories WHERE space='chat:qq:100'") > 0 and count(e, "SELECT COUNT(*) FROM reminders") == 3
        # 某个人：他的人物备忘（人物 id 和他的各平台账号）、他挂着的提醒、他的私聊上下文；别人的都在
        c = e.core.reset.run({"type": "person", "id": "pe_haitun"}, list(CATS))["counts"]
        assert c["person"] == 1 and c["reminder"] == 1 and c.get("recap", 0) == 0
        assert db.rows("SELECT subject FROM memories WHERE kind='person'") != [] and not db.rows("SELECT 1 FROM memories WHERE subject='person:pe_haitun'")
        assert [r["user_id"] for r in db.rows("SELECT user_id FROM reminders")] == ["111", "222"]
        assert db.doc("person", "pe_haitun") is not None  # 人物卡本身还在
        # 没有选范围（空 id）：报错、不删
        with pytest.raises(ValueError):
            e.core.reset.run({"type": "room", "id": ""}, ["meme"])
    env.run(body)


def test_reset_undo_restores_everything(env):
    async def body(e):
        e.add_bot("900")
        seed(e)
        db, eng = e.core.db, e.engine
        snap = lambda: ([(r["kind"], r["space"], r["subject"], r["content"], r["room"]) for r in db.rows("SELECT * FROM memories ORDER BY kind, content")],
                        db.rows("SELECT platform, chat_id, user_id, text, status FROM reminders ORDER BY text"),
                        db.rows("SELECT space, room, phash, status, file, descr FROM stickers ORDER BY phash"),
                        [(k, [x.text for x in r.ctx]) for k, r in sorted(eng.rooms.items())], count(e, "SELECT COUNT(*) FROM memvec"), db.kv("style_n:qq:100"))
        before = snap()
        e.core.reset.run({"type": "all"}, list(CATS))
        assert snap() != before and count(e, "SELECT COUNT(*) FROM memories") == 0
        # 重置后新来了记忆（会占用被删掉的编号）：撤销照样能完整恢复
        eng.memory.add("chat:qq:100", "recap", "", "", "重置后的新前情", "inferred", None, "qq:100", status="active")
        info = e.core.reset.undo()
        assert info["undone"] is True
        after = snap()
        assert after[0] == sorted(before[0] + [("recap", "chat:qq:100", "", "重置后的新前情", "qq:100")], key=lambda x: (x[0], x[3]))
        assert after[1:] == before[1:]
        assert (eng.stickers.folder() / "reset_test_0.png").read_bytes() == b"PNG" + bytes([0]) * 10  # 图片文件也回来了
        # 向量跟着记忆换了新编号，仍然对得上
        assert count(e, "SELECT COUNT(*) FROM memvec v JOIN memories m ON m.id=v.mem_id") == count(e, "SELECT COUNT(*) FROM memvec")
        assert count(e, "SELECT COUNT(*) FROM stickers s JOIN memories m ON m.id=s.mem_id") == 2
        assert eng.private_hist["ac_900:111"] == [{"role": "user", "content": "私聊"}]
        # 同一次重置只能撤销一次；没有可撤销的就报错
        with pytest.raises(ValueError):
            e.core.reset.undo()
        # 下一次重置覆盖「刚才的重置」
        e.core.reset.run({"type": "room", "id": "qq:200"}, ["recap"])
        assert e.core.reset.last()["scope"] == {"type": "room", "id": "qq:200"} and not e.core.reset.last()["undone"]
    env.run(body)


def test_reset_api_and_old_backup_pruning(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        e.add_bot("900")
        seed(e)
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        body_ = {"scope": {"type": "room", "id": "qq:100"}, "kinds": ["recap", "meme"]}
        pv = await (await c.post("/api/memory/reset/preview", json=body_)).json()
        assert pv["counts"] == {"recap": 1, "meme": 1} and pv["labels"]["meme"].startswith("群梗")
        r = await c.post("/api/memory/reset", json=body_)  # 没确认不执行
        assert r.status == 400 and count(e, "SELECT COUNT(*) FROM memories WHERE kind='meme'") == 1
        r = await c.post("/api/memory/reset", json=dict(body_, confirm=True))
        assert r.status == 200 and count(e, "SELECT COUNT(*) FROM memories WHERE kind='meme'") == 0
        last = await (await c.get("/api/memory/reset/last")).json()
        assert last["scope_label"].startswith("群「") and last["counts"] == {"recap": 1, "meme": 1} and not last["undone"]
        assert (await c.post("/api/memory/reset/preview", json={"scope": {"type": "room", "id": ""}, "kinds": ["meme"]})).status == 400
        r = await c.post("/api/memory/reset/undo")
        assert r.status == 200 and count(e, "SELECT COUNT(*) FROM memories WHERE kind='meme'") == 1
        assert (await c.post("/api/memory/reset/undo")).status == 400
        await c.close()
        # 每日整理时：超过保留天数的旧重置备份被清掉，最近一次的留着
        e.core.reset.run({"type": "room", "id": "qq:100"}, ["recap"])
        old = e.core.data / "backups" / "reset-20200101-000000"
        old.mkdir()
        os.utime(old, (1, 1))
        e.core.maintain()
        assert not old.exists() and (e.core.data / "backups" / e.core.reset.last()["folder"]).exists()
    env.run(body)


def test_ui_has_select_all_and_reset_entry_and_docs():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    data = (root / "web" / "js" / "pages-data.js").read_text(encoding="utf-8")
    assert "全选当前筛选结果" in data and "data-mid" in data and "重置记忆…" in data and "撤销刚才的重置" in data
    tools = (root / "web" / "js" / "pages-tools.js").read_text(encoding="utf-8")
    assert "resetMemoryDialog" in tools and "将删掉" in tools
    for f, words in (("docs/使用说明.md", ("重置记忆", "撤销刚才的重置", "全选当前筛选结果")), ("docs/User-Guide.en.md", ("Reset Memory", "Undo the last reset", "Select all current results"))):
        t = (root / f).read_text(encoding="utf-8")
        assert all(w in t for w in words), f


# ---------- 数据位置：安装目录下的 data，或 %APPDATA%\PodMates ----------
def test_data_dir_resolution(tmp_path, monkeypatch):
    from mb import paths
    app, appdata = tmp_path / "App", tmp_path / "Roaming"
    app.mkdir()
    appdata.mkdir()
    monkeypatch.delenv("PODMATES_DATA", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths.sys, "executable", str(app / "PodMates.exe"))
    # 全新安装：安装目录下有 data（「仅为我安装」时安装程序建的）→ 用它
    (app / "data").mkdir()
    assert paths.data_dir() == app / "data"
    # 升级老安装：安装目录下的 data 是空的，老数据在 %APPDATA%\PodMates → 继续用老数据，不会「丢」
    (appdata / "PodMates").mkdir()
    (appdata / "PodMates" / "podmates.db").write_text("x")
    assert paths.data_dir() == appdata / "PodMates"
    # 数据拷到安装目录下的 data 之后，以那里为准（旧目录留着作回滚）
    (app / "data" / "podmates.db").write_text("y")
    assert paths.data_dir() == app / "data"
    # 旧名数据库也认
    (app / "data" / "podmates.db").unlink()
    (appdata / "PodMates" / "podmates.db").unlink()
    (app / "data" / "multibot.db").write_text("z")
    assert paths.data_dir() == app / "data"
    # 「为所有用户安装」：安装目录下没有 data → %APPDATA%\PodMates
    (app / "data" / "multibot.db").unlink()
    (app / "data").rmdir()
    assert paths.data_dir() == appdata / "PodMates"
    # 环境变量永远优先；不是打包版（开发时）只看 %APPDATA%
    monkeypatch.setenv("PODMATES_DATA", str(tmp_path / "env"))
    assert paths.data_dir() == tmp_path / "env"
    monkeypatch.delenv("PODMATES_DATA")
    monkeypatch.setattr(paths, "FROZEN", False)
    assert paths.data_dir() == appdata / "PodMates"


def test_installer_script_data_and_location():
    from pathlib import Path
    iss = (Path(__file__).resolve().parent.parent / "installer" / "PodMates.iss").read_text(encoding="utf-8-sig")
    assert "DisableDirPage=no" in iss and "PrivilegesRequiredOverridesAllowed=dialog" in iss  # 选安装位置页、仅为我 / 所有用户 对话框都在
    assert r'Name: "{app}\data"; Check: PerUserInstall' in iss and "not IsAdminInstallMode" in iss  # 仅为我安装才建 data
    assert 'Name: "{app}"' not in iss.split("[UninstallDelete]")[1].split("[Code]")[0]  # 卸载时不能整个删掉 {app}（里面有 data）
    assert r"{app}\data" in iss.split("usPostUninstall")[1] and r"{userappdata}\PodMates" in iss  # 卸载时问要不要删数据，两个位置都认
    assert 'ValueName: "PodMates"' in iss and "HKCU" in iss


# ---------- 启动时的登录页 / 向导判断（web/js/core.js 的 App.start）----------
LOGIN_JS = r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const dummy = () => new Proxy(function () {}, { get: (t, k) => k === Symbol.toPrimitive ? () => '' : dummy(), apply: () => dummy() });
async function run(state) {
  const calls = [];
  const resp = (data) => ({ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => data });
  const ctx = vm.createContext({ console, setTimeout, clearTimeout, document: dummy(), window: { addEventListener() {} }, location: { hash: '' }, localStorage: { getItem: () => null, setItem() {} },
    navigator: {}, matchMedia: () => ({ matches: false, addEventListener() {} }), EventSource: function () {}, FormData: function () {},
    I18N: { init: async () => {} }, Wizard: { open: () => calls.push('wizard') }, fetch: async (url) => resp(url === '/api/state' ? state : { needs_setup: state.needs_setup, roles: [], chats: [], settings: { global: {} } }) });
  vm.runInContext(src, ctx);
  vm.runInContext("App.showLogin = () => __calls.push('login'); App.layout = () => __calls.push('layout'); App.route = () => {}; connectEvents = () => {};", Object.assign(ctx, { __calls: calls }));
  await vm.runInContext('App.start()', ctx);
  return calls;
}
(async () => {
  const out = {};
  out.nopw_done = await run({ has_password: false, authed: false, needs_setup: false });
  out.pw_loggedout = await run({ has_password: true, authed: false, needs_setup: false });
  out.pw_loggedin = await run({ has_password: true, authed: true, needs_setup: false });
  out.nopw_setup = await run({ has_password: false, authed: false, needs_setup: true });
  console.log(JSON.stringify(out));
})();
"""


def test_start_does_not_stick_on_login_page(tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        pytest.skip("没有 node")
    f = tmp_path / "t.js"
    f.write_text(LOGIN_JS, encoding="utf-8")
    core = Path(__file__).resolve().parent.parent / "web" / "js" / "core.js"
    r = subprocess.run(["node", str(f), str(core)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["nopw_done"] == ["layout"]            # 没设密码 + 已设置好：直接进面板，不出登录页
    assert out["pw_loggedout"] == ["login"]          # 有密码 + 没登录：出登录页
    assert out["pw_loggedin"] == ["layout"]          # 有密码 + 已登录：进面板
    assert out["nopw_setup"] == ["wizard"]           # 没设密码 + 还没设置：进向导


# ---------- 总览页「还没填 API Key」：看主模型实际用的那家，不是固定的 default ----------
KEY_JS = r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const dummy = () => new Proxy(function () {}, { get: (t, k) => k === Symbol.toPrimitive ? () => '' : dummy(), apply: () => dummy() });
const ctx = vm.createContext({ console, setTimeout, clearTimeout, document: dummy(), window: { addEventListener() {} }, location: { hash: '' }, localStorage: { getItem: () => null, setItem() {} },
  navigator: {}, matchMedia: () => ({ matches: false, addEventListener() {} }), EventSource: function () {}, FormData: function () {}, fetch: async () => ({}) });
vm.runInContext(src, ctx);
const f = vm.runInContext('mainKeyMissing', ctx);
const eps = [{ id: 'default', has_key: false, env_set: false, keyless: false }, { id: 'zai', has_key: true }, { id: 'env', has_key: false, env_set: true }, { id: 'local', keyless: true }, { id: 'bare' }];
const items = id => [{ key: 'main_endpoint', value: id }, { key: 'x', value: 1 }];
console.log(JSON.stringify({
  zai: f(eps, items('zai')), default_nokey: f(eps, items('default')), env: f(eps, items('env')), local: f(eps, items('local')), bare: f(eps, items('bare')),
  gone: f(eps, items('deleted')), unset: f(eps, []), unset_has_default_key: f([{ id: 'default', has_key: true }], []),
}));
"""


def test_home_key_warning_checks_the_endpoint_actually_in_use(tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        pytest.skip("没有 node")
    f = tmp_path / "k.js"
    f.write_text(KEY_JS, encoding="utf-8")
    root = Path(__file__).resolve().parent.parent / "web" / "js"
    r = subprocess.run(["node", str(f), str(root / "core.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"zai": False, "default_nokey": True, "env": False, "local": False, "bare": True, "gone": True, "unset": True, "unset_has_default_key": False}
    # 总览页用的就是这个函数；向导首次设置时用 default 是对的，保留
    assert "mainKeyMissing(" in (root / "pages-main.js").read_text(encoding="utf-8")
    assert "'default'" in (root / "wizard.js").read_text(encoding="utf-8")
