"""记忆默认按群、按私聊隔离：A 群的 B 群看不到、私聊的群里看不到；语义回忆、「记住」、画像同样；绑进同一个空间才共享；
从旧版（人物备忘按角色共用）升级时按来源拆回去，认不出来源的不删。"""
import re
import json
import time

from mb.core import Core
from mb.memory import Memory

NOTE = "在考研"


def mains(e):
    return [c for c in e.llm.calls if c["_kind"] == "main"]


def last_system(e):
    return mains(e)[-1]["messages"][0]["content"]


def setup(e):
    e.add_bot("900")
    e.core.settings.set("global", "proactive", False)
    e.llm.main = lambda s, u: "嗯"
    return e.engine.memory


def test_default_is_isolated_for_a_fresh_install(env):
    async def body(e):
        mem = e.engine.memory
        assert "memory_isolated" not in __import__("mb.settings", fromlist=["BY_KEY"]).BY_KEY
        assert mem.space("person", "r1", "qq", "qq:100") == "iso:qq:100:person:r1" != mem.space("person", "r1", "qq", "qq:200")
        assert mem.space("recap", "r1", "qq", "qq:100") == "chat:qq:100" != mem.space("recap", "r1", "qq", "qq:200")
        assert mem.space("person", "r1", "qq", None, "dm:ac_900:111") == "iso:dm:ac_900:111:person:r1"
        assert mem.space("person", "r1", "qq", None, "dm:ac_900:111") != mem.space("person", "r1", "qq", None, "dm:ac_900:222")
        assert mem.space("person", "r1", "qq", None, "dm:ac_900:111") != mem.space("person", "r2", "qq", None, "dm:ac_900:111")
        assert not any(sp.startswith("role:") for sp in [mem.space(k, "r1", "qq", "qq:100") for k in ("person", "recap", "meme", "style", "sticker")])
    env.run(body)


def test_group_notes_stay_in_their_group_through_the_prompt(env):
    async def body(e):
        mem = setup(e)
        s = e.core.settings
        s.set("global", "memory_segment", 4)
        e.llm.mem = lambda p: {"recap": "群友111 说自己在考研", "people": [{"uid": "111", "name": "群友111", "note": NOTE, "source": "self"}],
                               "memes": [{"keyword": "上岸", "origin": "考研上岸梗"}]}
        for i in range(4):
            await e.say("111", f"闲聊{i}", chat="100")
        await e.engine.idle()
        await e.say("111", "在吗，上岸了吗", chat="100", at=["900"])
        a = last_system(e)
        assert NOTE in a and "【这个群以前的事】" in a and "上岸" in a
        await e.say("111", "在吗，上岸了吗", chat="200", at=["900"])
        b = last_system(e)
        assert NOTE not in b and "这个群以前的事" not in b and "考研上岸梗" not in b
    env.run(body)


def test_private_notes_stay_in_that_private_chat(env):
    async def body(e):
        mem = setup(e)
        await e.say("111", "记住我不吃香菜", private=True, at=["900"])
        row = e.core.db.rows("SELECT * FROM memories WHERE source='told'")[0]
        assert row["space"] == "iso:dm:ac_900:111:person:r_haitun" and row["room"] == "dm:ac_900:111"
        await e.say("111", "晚饭吃什么", private=True)
        assert re.search(r"本人要求记住）：我不吃香菜", last_system(e))  # 同一个私聊里记得
        await e.say("111", "晚饭吃什么", chat="100", at=["900"])
        assert "我不吃香菜" not in last_system(e)  # 任何群里都不出现
        await e.say("222", "晚饭吃什么", private=True)
        assert "我不吃香菜" not in last_system(e)  # 别人的私聊里也没有
    env.run(body)


def test_remember_command_in_a_group_is_isolated_too(env):
    async def body(e):
        mem = setup(e)
        await e.say("111", "记住我不吃香菜", chat="100", at=["900"])
        await e.say("111", "晚饭吃什么", chat="100", at=["900"])
        assert re.search(r"本人要求记住）：我不吃香菜", last_system(e))
        await e.say("111", "晚饭吃什么", chat="200", at=["900"])
        assert "我不吃香菜" not in last_system(e)
        await e.say("111", "晚饭吃什么", private=True)
        assert "我不吃香菜" not in last_system(e)
    env.run(body)


def test_style_profile_is_per_group(env):
    async def body(e):
        mem = setup(e)
        mem.set_style("qq:100", "短句，爱用哈哈")
        assert mem.style_text("qq:100") == "短句，爱用哈哈" and mem.style_text("qq:200") == ""
        assert mem.style_space("qq:100") == "chat:qq:100" != mem.style_space("qq:200")
    env.run(body)


def test_semantic_recall_stays_in_scope(env):
    async def body(e):
        mem = setup(e)
        a = mem.space("person", "r_haitun", "qq", "qq:100")
        mem.add(a, "person", "uid:qq:10001", "阿明", "在准备考研，每天泡图书馆", "self", room="qq:100", status="active")
        mem.add(mem.space("recap", "r_haitun", "qq", "qq:100"), "recap", "", "", "群里约好周六去爬泰山", "inferred", room="qq:100", status="active")
        await mem.flush_index()
        q = "研究生入学考试复习得怎么样了"
        assert "在准备考研" in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q)
        assert "在准备考研" not in await mem.full_section("r_haitun", "qq", "qq:200", [], "", q)  # B 群找不到 A 群的
        assert "爬泰山" not in await mem.full_section("r_haitun", "qq", "qq:200", [], "", "这周末登山几点集合")
        assert "在准备考研" not in await mem.full_section("r_haitun", "qq", None, [], "", q, dm="dm:ac_900:111")  # 私聊也找不到群里的
        # 私聊里记下的，群里语义回忆也找不到
        d = mem.space("person", "r_haitun", "qq", None, "dm:ac_900:111")
        mem.add(d, "person", "uid:qq:111", "阿华", "在学吉他，每天练琴", "told", room="dm:ac_900:111", status="active")
        await mem.flush_index()
        q2 = "弹奏乐器的练习进度"
        assert "练琴" in await mem.full_section("r_haitun", "qq", None, [], "", q2, dm="dm:ac_900:111")
        assert "练琴" not in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q2)
    env.run(body)


def test_user_made_space_shares_groups_and_private_chats(env):
    async def body(e):
        mem = setup(e)
        db = e.core.db
        a = mem.space("person", "r_haitun", "qq", "qq:100")
        mem.add(a, "person", "uid:qq:111", "群友111", NOTE, "self", room="qq:100", status="active")
        sec = lambda chat, dm=None: mem.section("r_haitun", "qq", chat, [("uid:qq:111", "群友111")], "", dm)
        assert NOTE in sec("qq:100") and NOTE not in sec("qq:200")
        # 把 A、B 两群绑进同一个空间：从此共用（空间里的记忆是绑定之后写进去的）
        db.put("space", {"id": "s1", "name": "AB共用", "kinds": ["person", "recap", "meme"], "bindings": [{"chat": "qq:100"}, {"chat": "qq:200"}]})
        assert mem.space("person", "r_haitun", "qq", "qq:100") == mem.space("person", "r_haitun", "qq", "qq:200") == "sp:s1"
        mem.add(mem.space("person", "r_haitun", "qq", "qq:100"), "person", "uid:qq:111", "群友111", "绑定后记的事", "self", room="qq:100", status="active")
        assert "绑定后记的事" in sec("qq:200") and "绑定后记的事" in sec("qq:100") and "绑定后记的事" not in sec("qq:300")
        # 私聊也能绑进去
        db.put("space", {"id": "s2", "name": "群和私聊", "kinds": ["person"], "bindings": [{"chat": "qq:300"}, {"chat": "dm:ac_900:111"}]})
        mem.add(mem.space("person", "r_haitun", "qq", None, "dm:ac_900:111"), "person", "uid:qq:111", "群友111", "私聊里说的", "told", room="dm:ac_900:111", status="active")
        assert "私聊里说的" in sec("qq:300") and "私聊里说的" in sec(None, "dm:ac_900:111") and "私聊里说的" not in sec("qq:400")
    env.run(body)


# ---------- 从旧版升级 ----------
def old_db(env_core, rows):
    """旧版留下的库：这些记忆行，没跑过隔离迁移。"""
    db = env_core.db
    db.del_kv("mem_isolated_v1")
    for r in rows:
        db.x("INSERT INTO memories (space, kind, subject, subject_name, content, source, src, level, status, ts, room) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (r["space"], r.get("kind", "person"), r.get("subject", "uid:qq:111"), "x", r["content"], r.get("source", "self"), "[]", 1, "active", time.time(), r.get("room")))


def test_migration_splits_shared_notes_by_room_and_keeps_unknown(env):
    async def body(e):
        core = e.core
        db = core.db
        mem = e.engine.memory
        db.put("account", {"id": "ac_q1", "platform": "qq", "role_id": "r_haitun", "qq": "900"})
        db.put("person", {"id": "pe_x", "name": "小明", "qq": ["777"], "discord": [], "wechat_ids": [], "wechat_remark": [], "nicks": {}})
        rows = [dict(space="role:r_haitun", content="A群里的", room="qq:100"), dict(space="role:r_haitun", content="B群里的", room="qq:200"),
                dict(space="role:r_haitun", content="微信群的", room="wechat:某群"),
                dict(space="role:r_haitun", content="私聊里记的", subject="uid:qq:777", source="told", room=None),
                dict(space="role:r_haitun", content="私聊里记的（按人物卡）", subject="person:pe_x", source="told", room=None),
                dict(space="role:r_haitun", content="认不出来源的", subject="name:匿名", source="told", room=None),
                dict(space="role:r_乙", content="别的角色没有账号", subject="uid:qq:555", source="told", room=None),
                dict(space="iso:qq:300:recap", kind="recap", subject="", content="隔离群的前情", room="qq:300"),
                dict(space="chat:qq:100", kind="recap", subject="", content="本来就按群的前情", room="qq:100")]
        old_db(core, rows)
        db.set_kv("semantic_off", ["role:r_haitun"])
        before = db.x("SELECT COUNT(*) FROM memories").fetchone()[0]
        res = core.isolate_memory()
        assert res == {"split": 5, "legacy": 2, "merged_iso": 1}
        assert db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == before  # 一条没删
        sp = lambda text: db.rows("SELECT space, room FROM memories WHERE content=?", (text,))[0]
        assert sp("A群里的") == {"space": "iso:qq:100:person:r_haitun", "room": "qq:100"}
        assert sp("B群里的")["space"] == "iso:qq:200:person:r_haitun"
        assert sp("微信群的")["space"] == "iso:wechat:某群:person:r_haitun"
        assert sp("私聊里记的") == {"space": "iso:dm:ac_q1:777:person:r_haitun", "room": "dm:ac_q1:777"}
        assert sp("私聊里记的（按人物卡）")["space"] == "iso:dm:ac_q1:777:person:r_haitun"
        assert sp("认不出来源的") == {"space": "legacy:r_haitun", "room": None} and sp("别的角色没有账号")["space"] == "legacy:r_乙"
        assert sp("隔离群的前情")["space"] == "chat:qq:300" and sp("本来就按群的前情")["space"] == "chat:qq:100"
        assert not db.rows("SELECT 1 FROM memories WHERE space LIKE 'role:%'")
        # 之后读取：A 群看得到自己的、B 群看不到
        assert "A群里的" in mem.section("r_haitun", "qq", "qq:100", [("uid:qq:111", "x")], "")
        assert "A群里的" not in mem.section("r_haitun", "qq", "qq:200", [("uid:qq:111", "x")], "")
        assert "私聊里记的" in mem.section("r_haitun", "qq", None, [("uid:qq:777", "x")], "", dm="dm:ac_q1:777")
        assert "私聊里记的" not in mem.section("r_haitun", "qq", "qq:100", [("uid:qq:777", "x")], "")
        # 语义回忆单个空间的开关跟着走；只跑一次
        assert "iso:qq:100:person:r_haitun" in db.kv("semantic_off")
        assert core.isolate_memory() is None
    env.run(body)


def test_migration_with_a_user_space_and_old_isolated_setting(env):
    async def body(e):
        core = e.core
        db = core.db
        db.put("space", {"id": "s1", "name": "AB", "kinds": ["person"], "bindings": [{"chat": "qq:100"}, {"chat": "qq:200"}]})
        db.x("INSERT OR REPLACE INTO settings VALUES ('chat:qq:400','memory_isolated','true')")
        core.settings.data.setdefault("chat:qq:400", {})["memory_isolated"] = True
        old_db(core, [dict(space="role:r_haitun", content="A群里的", room="qq:100"), dict(space="role:r_haitun", content="C群里的", room="qq:300")])
        core.isolate_memory()
        # 用户自己建的空间优先：A 群的并进 AB 共用的空间
        assert db.rows("SELECT space FROM memories WHERE content='A群里的'")[0]["space"] == "sp:s1"
        assert db.rows("SELECT space FROM memories WHERE content='C群里的'")[0]["space"] == "iso:qq:300:person:r_haitun"
        assert not db.rows("SELECT 1 FROM settings WHERE key='memory_isolated'") and "memory_isolated" not in core.settings.data.get("chat:qq:400", {})
    env.run(body)


def test_the_real_core_open_runs_the_migration_once(env, tmp_path):
    # 全新的库：打开一次就记下「已隔离」，没有旧数据可拆
    core = Core(tmp_path)
    try:
        assert core.db.kv("mem_isolated_v1") == {"split": 0, "legacy": 0, "merged_iso": 0, "ts": core.db.kv("mem_isolated_v1")["ts"]}
    finally:
        core.engine.close()
        core.db.close()
