"""发布前修复（甲）：换电脑导出、上下文不漏、删备忘重写印象、重置覆盖所有会话、迁移前备份、
插话计数跨重启、记忆预算、语义回忆带日期、别的机器人按角色跳过守门员、按记忆种类选空间。"""
import sqlite3
import time
import zipfile

import pytest

from mb.core import Core
from mb.engine import Entry


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def restart(e):
    """模拟程序重启：同一个库上起一个全新的引擎（内存里的东西都没了）。"""
    from mb.engine import Engine
    eng = Engine(e.core.db, e.core.settings, e.core.llm, emit=e.core.emit)
    eng.pace = 0
    return eng


def use_api_embed(e):
    e.core.db.set_kv("embedding", {"source": "api", "preset": "bge-small-zh-v1.5", "api_preset": "siliconflow-qwen3-4b",
                                   "api_base": f"http://127.0.0.1:{e.port}/v4", "api_model": "mock-embed", "api_dim": 0})
    e.core.db.set_secret("embed:api", "k")


def add_row(e, room, text, kind="human", ts=None, uid="111", name="甲"):
    cur = e.core.db.x("INSERT INTO messages (room, ts, account_id, msg_id, user_id, name, text, role_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                      (room, ts if ts is not None else time.time(), "", "", uid, name, text, "", kind))
    return cur.lastrowid


# ---------- 01 换电脑导出：表情图片和可撤销的重置 ----------
def test_export_carries_stickers_and_reset_backups(env, tmp_path, monkeypatch):
    async def body(e):
        e.add_bot("900")
        mem, db = e.engine.memory, e.core.db
        folder = e.engine.stickers.folder()
        (folder / "sticker_a.png").write_bytes(b"PNG-A")
        (folder / "live.png").write_bytes(b"PNG-LIVE")  # 这次没被重置删掉的表情图片
        mem.add("chat:qq:100", "sticker", "0000000000000001", "表情包", "表情甲", "inferred", None, "qq:100", status="active")
        db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, file, size, uses, mem_id, descr)
                VALUES (?,?,?,?,?,?,?,'learned',?,?,?,?,?)""",
             ("chat:qq:100", "qq:100", "0000000000000001", 1, "[]", 1, 2, "sticker_a.png", 5, 0, None, "表情甲"))
        mem.add("iso:qq:100:person:r_haitun", "person", "uid:qq:111", "甲", "喜欢猫", "self", None, "qq:100", status="active")
        info = e.core.reset.run({"type": "room", "id": "qq:100"}, ["sticker", "person"])
        assert info["counts"] == {"person": 1, "sticker": 1}
        assert not (folder / "sticker_a.png").exists()
        zp = tmp_path / "move.zip"
        e.core.export(zp, with_secrets=True)
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
            assert "stickers/live.png" in names  # 现用的表情图片
            assert f"backups/{info['folder']}/data.json" in names
            assert f"backups/{info['folder']}/stickers/sticker_a.png" in names
        # 新电脑：导入后图片和撤销用的文件都在，撤销也能用
        d2 = tmp_path / "pc2"
        d2.mkdir()
        monkeypatch.setenv("PODMATES_DATA", str(d2))
        c2 = Core(d2)
        try:
            await c2.import_zip(zp)
            assert (d2 / "stickers" / "live.png").read_bytes() == b"PNG-LIVE"
            assert (d2 / "backups" / info["folder"] / "data.json").exists()
            c2.reset.undo()
            assert c2.db.x("SELECT COUNT(*) FROM memories WHERE kind='person'").fetchone()[0] == 1
            assert c2.db.x("SELECT COUNT(*) FROM stickers").fetchone()[0] == 1
            assert (d2 / "stickers" / "sticker_a.png").read_bytes() == b"PNG-A"  # 撤销要用的图片也回来了
        finally:
            c2.engine.close()
            c2.db.close()
    env.run(body)


# ---------- 02 每条消息不是在前情里，就是在原始消息里 ----------
def test_unconsolidated_messages_beyond_200_still_reach_the_prompt(env):
    async def body(e):
        e.add_bot("900")
        eng = e.engine
        setg(e, "memory_segment", 100000)  # 这次不整理：所有消息都还没进前情
        setg(e, "proactive", False)
        room = eng.room("qq", "100", "测试群")
        bot = eng.duty(eng.bots["ac_900"], room)
        for i in range(250):
            eng.push(room, Entry("111", "甲", f"第{i}条"), "human")
        assert len(room.ctx) == 200  # 内存里的上下文只有最近 200 条
        lines = eng.ctx_lines(room, bot, "你", 30, cover=True)
        assert "第0条" in lines and "第249条" in lines and lines.count("第") == 250
        # 重启回填也照这个办法：250 条一条不少
        eng2 = restart(e)
        try:
            r2 = eng2.room("qq", "100", "测试群")
            b2 = e.engine.duty(e.engine.bots["ac_900"], r2)  # ctx_lines 只用它的角色 / 账号 id
            lines2 = eng2.ctx_lines(r2, b2, "你", 30, cover=True)
            assert "第0条" in lines2 and "第249条" in lines2 and lines2.count("第") == 250
        finally:
            eng2.close()
    env.run(body)


# ---------- 03 删备忘后用剩下的备忘重写印象 ----------
def test_deleting_a_note_rewrites_the_impression_of_its_owner(env):
    async def body(e):
        from mb.server import Server
        e.add_bot("900")
        mem, db = e.engine.memory, e.core.db
        sp = "iso:qq:100:person:r_haitun"
        n1 = mem.add(sp, "person", "person:pe_haitun", "海豚", "喜欢猫", "self", None, "qq:100", status="active")
        n2 = mem.add(sp, "person", "person:pe_haitun", "海豚", "养了一只狗", "self", None, "qq:100", status="active")
        n3 = mem.add(sp, "person", "person:pe_haitun", "海豚", "讨厌下雨", "self", None, "qq:100", status="active")
        mem.save_impression(sp, "person:pe_haitun", "海豚", "她喜欢猫，养了一只狗", n3, "qq:100", None)
        prompts = []
        e.llm.impression = lambda p: (prompts.append(p), "她的情况变了")[1]
        srv = Server(e.core)
        srv.delete_memories([n1])  # 面板上删掉一条旧备忘
        await e.engine.idle()
        assert prompts and "养了一只狗" in prompts[-1] and "讨厌下雨" in prompts[-1]
        assert "喜欢猫" not in prompts[-1]  # 不按字面：被删的那条不在新印象的资料里
        assert mem.impression_row(sp, "person:pe_haitun")["content"] == "她的情况变了"
        # 文字「忘掉××」：删的是备忘，印象里没有同样的字也要按它的主人重写
        info = e.core.reset.run({"type": "text", "id": "狗", "spaces": [sp]}, ["person"])
        await e.engine.idle()
        assert info["counts"]["person"] == 1  # 只删了「养了一只狗」这条备忘（印象里没有「狗」）
        assert "讨厌下雨" in prompts[-1] and "养了一只狗" not in prompts[-1]
        assert mem.impression_row(sp, "person:pe_haitun")["content"] == "她的情况变了"
    env.run(body)


# ---------- 04 重置上下文覆盖所有会话（含没加载进内存的） ----------
def test_context_reset_covers_sessions_not_loaded_yet(env):
    async def body(e):
        e.add_bot("900")
        eng, db = e.engine, e.core.db
        for i in range(3):
            add_row(e, "dm:ac_900:111", f"私聊{i}", ts=time.time() - 100 + i)
            add_row(e, "qq:777", f"群777-{i}", ts=time.time() - 100 + i)
        assert "ac_900:111" not in eng.private_hist and "qq:777" not in eng.rooms
        e.core.reset.run({"type": "all"}, ["context"])
        floors = db.kv("ctx_floor") or {}
        assert "dm:ac_900:111" in floors and "qq:777" in floors  # 按库里的会话写
        eng2 = restart(e)
        try:
            assert eng2.dm_hist("ac_900", "111") == []
            assert not eng2.room("qq", "777", "旧群").ctx
        finally:
            eng2.close()
        e.core.reset.undo()  # 撤销：库里的历史不再被挡住
        eng3 = restart(e)
        try:
            assert [h["content"] for h in eng3.dm_hist("ac_900", "111")] == ["私聊0", "私聊1", "私聊2"]
            assert len(eng3.room("qq", "777", "旧群").ctx) == 3
        finally:
            eng3.close()
    env.run(body)


# ---------- 05 迁移前先备份 ----------
def test_pending_migration_is_backed_up_before_the_first_write(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("PODMATES_DATA", str(d))
    c = Core(d)
    try:
        assert not list((d / "backups").glob("pre-migrate-*.db"))
        c.db.del_kv("mem_isolated_v1")
        c.db.del_kv("memory_v2")
        c.db.x("""INSERT INTO memories (space, kind, subject, subject_name, content, source, src, level, status, ts, room)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               ("role:r_haitun", "person", "uid:qq:111", "甲", "旧库里共用的备忘", "self", "[]", 1, "active", time.time(), "qq:100"))
        c.db.x("""INSERT INTO memories (space, kind, subject, subject_name, content, source, src, level, status, ts, room)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               ("chat:qq:100", "recap", "", "", "老前情", "inferred", "[]", 1, "active", time.time(), "qq:100"))
    finally:
        c.engine.close()
        c.db.close()
    c2 = Core(d)  # 升级：有迁移要跑，先备份
    try:
        backups = list((d / "backups").glob("pre-migrate-*.db"))
        assert len(backups) == 1
        assert not list((d / "backups").glob("pre-memory-v2-*.db"))  # 一次启动只备一次，后面的迁移不再重复备
        con = sqlite3.connect(backups[0])
        try:
            assert con.execute("SELECT space FROM memories WHERE kind='person'").fetchone()[0] == "role:r_haitun"  # 备份是迁移前的内容
        finally:
            con.close()
        assert c2.db.kv("mem_isolated_v1") is not None and c2.db.kv("memory_v2") is not None
        assert c2.db.x("SELECT COUNT(*) FROM memories WHERE kind='recap' AND meta IS NULL").fetchone()[0] == 0
        assert c2.db.x("SELECT space FROM memories WHERE kind='person'").fetchone()[0].startswith("iso:")
    finally:
        c2.engine.close()
        c2.db.close()
    n = len(list((d / "backups").glob("pre-migrate-*.db")))
    c3 = Core(d)  # 已经跑过的迁移：不再跑，也不再备份
    try:
        assert len(list((d / "backups").glob("pre-migrate-*.db"))) == n
    finally:
        c3.engine.close()
        c3.db.close()


# ---------- 09 每天插话计数跨重启 ----------
def test_daily_interjection_count_survives_a_restart_without_a_fixed_cap(env):
    async def body(e):
        e.add_bot("900")
        eng, db = e.engine, e.core.db
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        early = now - 3600
        if early <= midnight:
            pytest.skip("刚过零点不到一小时：造不出「今天、但一小时以前」的时刻")
        ts = [midnight + (early - midnight) * i / 550 for i in range(550)]
        db.set_kv("pro:qq:100", {"ac_900": ts})
        eng2 = restart(e)
        try:
            r2 = eng2.room("qq", "100", "测试群")
            assert r2.daily["ac_900"] == [time.strftime("%Y-%m-%d"), 550]  # 当天 550 条全部接上
            b2 = e.engine.duty(e.engine.bots["ac_900"], r2)  # bump 只看账号 id
            eng2.bump(b2, r2, now)
            assert len(db.kv("pro:qq:100")["ac_900"]) == 551  # 不按固定 500 条截断
            old = now - 23 * 3600
            eng2.bump(b2, r2, old)
            assert min(db.kv("pro:qq:100")["ac_900"]) <= old  # 保存的时间戳至少覆盖最近 24 小时
        finally:
            eng2.close()
        eng3 = restart(e)
        try:
            assert eng3.room("qq", "100", "测试群").daily["ac_900"][1] == 551
        finally:
            eng3.close()
    env.run(body)


# ---------- 11 记忆预算 + 13 语义回忆带日期 ----------
def test_memes_and_semantic_recall_are_inside_the_memory_budget(env):
    async def body(e):
        e.add_bot("900")
        mem = e.engine.memory
        mem.add("chat:qq:100", "meme", "下班", "下班", "老王天天喊下班，喊了半年", "inferred", None, "qq:100", status="active")
        parts, _ = mem.compose("r_haitun", "qq", "qq:100", [], "今天下班真早", budget=8)
        assert not any("【群梗】" in p for p in parts)  # 超出预算：不放
        parts, _ = mem.compose("r_haitun", "qq", "qq:100", [], "今天下班真早", budget=500)
        assert any("【群梗】" in p for p in parts)

        class Fake:
            async def search(self, *a, **k):
                return [{"id": 999, "kind": "person", "subject_name": "阿明", "content": "在准备考研，每天泡图书馆",
                         "source": "self", "ts": 1700000000.0}]

        from mb.memory import day_text
        old = mem.embedder
        mem.embedder = Fake()
        try:
            out = await mem.full_section("r_haitun", "qq", "qq:100", [], "", "考研", budget=5)
            assert "【想起的旧事】" not in out  # 超出预算：不放
            out = await mem.full_section("r_haitun", "qq", "qq:100", [], "", "考研", budget=500)
            assert "【想起的旧事】" in out and "在准备考研" in out
            assert day_text(1700000000.0) in out  # 语义回忆的人物备忘也带日期
        finally:
            mem.embedder = old
    env.run(body)


def test_semantic_note_date_comes_from_the_database(env):
    async def body(e):
        e.add_bot("900")
        use_api_embed(e)
        mem, emb = e.engine.memory, e.engine.embedder
        ts = 1700000000.0
        mem.add("iso:qq:100:person:r_haitun", "person", "uid:qq:111", "阿明", "在准备考研，每天泡图书馆", "self", None, "qq:100",
                status="active", ts=ts)
        await mem.flush_index()
        hits = await emb.search("研究生入学考试复习得怎么样了", ["iso:qq:100:person:r_haitun"], 3, 0.3, set())
        assert hits and hits[0]["ts"] == ts  # 检索就把日期带回来了
        from mb.memory import day_text
        setg(e, "semantic_min_score", 0.3)  # 模拟向量只有 64 维：门槛调低，测的是日期有没有带回来
        out = await mem.full_section("r_haitun", "qq", "qq:100", [], "", "研究生入学考试复习得怎么样了", budget=500)
        assert f"关于阿明（{day_text(ts)}" in out
    env.run(body)


# ---------- 17 别的机器人按角色取设置 ----------
def test_bot_skip_gate_follows_each_role(env):
    async def body(e):
        e.add_role("r_乙", "乙角色")
        e.add_bot("900", role_id="r_haitun")
        e.add_bot("901", role_id="r_乙")
        e.core.db.put("person", dict(id="pe_bot", name="别家AI", identity="群里另一个 AI，不是你", qq=["777"], bot=True,
                                     attitude={"presets": [], "custom": ""}))
        e.engine.reload()
        setg(e, "cooldown_seconds", 0)
        e.core.settings.set("role:r_乙", "bot_skip_gate", False)  # 只有乙角色不跳过
        e.llm.gate = lambda p: {"reply": False, "brief": "不接"}
        await e.say("777", "我是别家 AI，随便说一句")
        gk = [c for c in e.llm.calls if c.get("_kind") == "gk"]
        assert len(gk) == 1
        e.core.settings.set("role:r_haitun", "bot_skip_gate", False)
        e.core.settings.set("role:r_乙", "bot_skip_gate", True)
        await e.say("777", "再说一句")
        gk = [c for c in e.llm.calls if c.get("_kind") == "gk"]
        assert len(gk) == 2  # 换了角色：换成海豚娘被问
    env.run(body)


# ---------- 20 按记忆种类选空间 ----------
def test_memory_kind_bindings_pick_the_right_space(env):
    async def body(e):
        e.add_bot("900")
        eng, mem, db = e.engine, e.engine.memory, e.core.db
        db.put("space", {"id": "d1", "name": "只看文件", "kinds": ["doc"], "bindings": [{"chat": "qq:100"}]})
        db.put("space", {"id": "s1", "name": "共用画像", "kinds": ["style"], "bindings": [{"chat": "qq:100"}, {"chat": "qq:200"}]})
        assert mem.space("doc", "r_haitun", "qq", "qq:100") == "sp:d1"
        room = eng.room("qq", "100", "测试群")
        bot = eng.duty(eng.bots["ac_900"], room)
        eng.bots["ac_900"].channel.files["f1"] = "你好世界".encode()
        await eng.read_file(bot, room, {"id": "f1", "name": "a.txt", "size": 12})
        rows = db.rows("SELECT space FROM memories WHERE kind='doc'")
        assert rows and all(r["space"] == "sp:d1" for r in rows)  # 文件片段按 doc 种类选空间
        mem.set_style("qq:100", "句子很短")
        assert mem.style_text("qq:200") == "句子很短"  # 绑到同一个空间：各群共用一份画像
        mem.set_style("qq:200", "句子很长")
        assert mem.style_text("qq:100") == "句子很长"
        assert db.x("SELECT COUNT(*) FROM memories WHERE kind='style'").fetchone()[0] == 1  # 只留一份
        mem.set_style("qq:200", "")
        assert mem.style_text("qq:100") == ""  # 清空也一起清掉
    env.run(body)
