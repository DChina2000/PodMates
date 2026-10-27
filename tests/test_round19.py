"""向量索引过期时自动补算：换模型、没算过的记忆（绕过正常写入的）、迁移改了 space 之后；模型 / 接口用不了时不硬试、不刷日志、条件好了再补；
重建期间不阻塞回复，语义回忆照常用已经算好的。"""
import asyncio
import time


def use_api(e, key="k"):
    e.core.db.set_kv("embedding", {"source": "api", "preset": "bge-small-zh-v1.5", "api_preset": "siliconflow-qwen3-4b",
                                   "api_base": f"http://127.0.0.1:{e.port}/v4", "api_model": "mock-embed", "api_dim": 0})
    e.core.db.set_secret("embed:api", key)


async def add_mem(e, space, content, room="qq:100", kind="person", subject="uid:qq:1", name="阿明"):
    mid = e.engine.memory.add(space, kind, subject, name, content, "self", room=room, status="active")
    await e.engine.memory.flush_index()
    return mid


async def wait_task(emb):
    if emb._task:
        await emb._task


def warn_lines(e, needle):
    return [r for r in e.core.db.rows("SELECT level, text FROM logs") if needle in r["text"]]


def test_migration_changing_spaces_does_not_invalidate_vectors(env):
    # 向量只认 mem_id 和签名，不认 space：隔离迁移改了空间之后，向量照样有效，检索在新空间里能找到
    async def body(e):
        emb, mem = e.engine.embedder, e.engine.memory
        await add_mem(e, "role:r_haitun", "在准备考研，每天泡图书馆")
        before = emb.status()
        assert before["indexed"] == 1 and before["missing"] == 0 and not before["needs_rebuild"]
        e.core.db.del_kv("mem_isolated_v1")
        e.core.isolate_memory()
        new = e.core.db.rows("SELECT space FROM memories")[0]["space"]
        assert new == "iso:qq:100:person:r_haitun"
        after = emb.status()
        assert after["indexed"] == 1 and after["missing"] == 0 and not after["needs_rebuild"]
        hits = await emb.search("研究生入学考试复习得怎么样了", [new], 3, 0.0, set())
        assert hits and "考研" in hits[0]["content"]
    env.run(body)


def test_memories_without_vectors_are_found_and_filled_in_automatically(env):
    async def body(e):
        emb = e.engine.embedder
        await add_mem(e, "iso:qq:100:person:r_haitun", "养了一只橘猫")
        # 绕过正常写入的记忆（比如恢复、导入、上次没算成的）：没有向量
        e.core.db.x("INSERT INTO memories (space, kind, subject, subject_name, content, source, src, level, status, ts, room) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("iso:qq:100:person:r_haitun", "person", "uid:qq:2", "小红", "周末去爬山", "self", "[]", 1, "active", time.time(), "qq:100"))
        st = emb.status()
        assert st["missing"] == 1 and st["needs_rebuild"] and st["indexed"] == 1
        await emb.ensure()
        await wait_task(emb)
        st = emb.status()
        assert st["missing"] == 0 and st["indexed"] == 2 and not st["needs_rebuild"] and not st["blocked"]
        n = len([c for c in e.llm.calls if c.get("_kind") == "embed"])
        await emb.ensure()  # 没有要补的：什么都不做
        assert emb._task is None or emb._task.done() and len([c for c in e.llm.calls if c.get("_kind") == "embed"]) == n
    env.run(body)


def test_switching_model_triggers_a_full_automatic_rebuild(env):
    async def body(e):
        emb = e.engine.embedder
        for i, t in enumerate(["在准备考研", "养了一只橘猫", "周末去爬山"]):
            await add_mem(e, "iso:qq:100:person:r_haitun", t, subject=f"uid:qq:{i}")
        assert emb.status()["indexed"] == 3
        use_api(e)  # 换成另一个来源 → 旧向量不通用
        st = emb.status()
        assert st["stale"] == 3 and st["needs_rebuild"]
        await emb.ensure()  # 定期检查（或换模型时的 kick）发现了，自己在后台重建
        assert emb._task is not None
        await wait_task(emb)
        st = emb.status()
        assert st["stale"] == 0 and st["indexed"] == 3 and not st["needs_rebuild"] and not st["rebuilding"]
        assert any(c.get("_kind") == "embed" and c["model"] == "mock-embed" for c in e.llm.calls)
        assert e.core.db.x("SELECT COUNT(*) FROM memvec WHERE sig!=?", (emb.sig(),)).fetchone()[0] == 0
    env.run(body)


def test_unavailable_model_or_api_is_not_retried_in_a_loop_and_recovers(env):
    async def body(e):
        emb = e.engine.embedder
        await add_mem(e, "iso:qq:100:person:r_haitun", "在准备考研")
        # 本机模型没下载：不硬试，面板上有原因，只记一条警告
        e.core.db.set_kv("embedding", {"source": "local", "preset": "gte-multilingual-base"})
        assert emb.status()["model_ready"] is False
        calls = len(e.llm.calls)
        for _ in range(6):
            await emb.ensure()
        st = emb.status()
        assert not emb.rebuilding and emb._task is None and "没下载" in st["blocked"] and st["missing"] == 1
        assert len(warn_lines(e, "还没算")) == 1 and len(e.llm.calls) == calls
        assert not emb.usable()
        mem = e.engine.memory
        assert "想起的旧事" not in await mem.full_section("r_haitun", "qq", "qq:100", [], "", "研究生考试")  # 语义回忆安静地跳过，不报错
        assert not warn_lines(e, "语义回忆出错")
        # 接口没设好：同样
        use_api(e, key="")
        for _ in range(3):
            await emb.ensure()
        assert "还没设好" in emb.status()["blocked"] and emb._task is None
        # 条件好了（填了密钥）→ 自动补算
        use_api(e, key="k")
        await emb.ensure()
        await wait_task(emb)
        st = emb.status()
        assert st["indexed"] == 1 and st["missing"] == 0 and st["blocked"] == "" and emb.usable()
    env.run(body)


def test_failed_rebuild_backs_off_instead_of_hammering(env):
    async def body(e):
        emb = e.engine.embedder
        await add_mem(e, "iso:qq:100:person:r_haitun", "在准备考研")
        use_api(e)
        e.llm.embed_fail = True
        await emb.ensure()
        await wait_task(emb)
        n = len([c for c in e.llm.calls if c.get("_kind") == "embed"])
        st = emb.status()
        assert n >= 1 and st["missing"] == 1 and "没成功" in st["blocked"]
        for _ in range(5):  # 条件没变：不重试
            await emb.ensure()
        assert len([c for c in e.llm.calls if c.get("_kind") == "embed"]) == n and emb._task.done()
        # 隔了一阵（这里把失败时间往前拨）再试；仍然失败就隔得更久
        fp, t, k = emb._fail
        emb._fail = (fp, t - 601, k)
        await emb.ensure()
        await wait_task(emb)
        assert len([c for c in e.llm.calls if c.get("_kind") == "embed"]) == n + 1 and emb._fail[2] == 2
        emb._fail = (emb._fail[0], emb._fail[1] - 601, 2)  # 第二次失败后要等 1200 秒：601 秒不够
        await emb.ensure()
        assert len([c for c in e.llm.calls if c.get("_kind") == "embed"]) == n + 1
        # 接口好了、隔够久：补上
        e.llm.embed_fail = False
        emb._fail = (emb._fail[0], emb._fail[1] - 2000, 2)
        await emb.ensure()
        await wait_task(emb)
        st = emb.status()
        assert st["missing"] == 0 and st["blocked"] == "" and emb._fail is None
        # 面板上手动点「一键重建」随时可以，不受退避限制
        e.llm.embed_fail = True
        await emb.rebuild()
        assert emb.status()["blocked"]
    env.run(body)


def test_rebuild_does_not_block_replies_and_recall_keeps_working(env):
    async def body(e):
        bot = e.add_bot("900")
        e.core.settings.set("global", "proactive", False)
        emb = e.engine.embedder
        for i in range(70):
            await add_mem(e, "iso:qq:100:person:r_haitun", f"第{i}条备忘" if i else "在准备考研，每天泡图书馆", subject=f"uid:qq:{i}")
        n_before = emb.status()["indexed"]
        assert n_before == 70
        use_api(e)
        real = emb.embed

        async def slow(texts, query=False):
            if not query:
                await asyncio.sleep(0.4)  # 重建的每一批都慢一点
            return await real(texts, query)
        emb.embed = slow
        await emb.ensure()
        await asyncio.sleep(0.05)
        assert emb.rebuilding
        t0 = time.time()
        e.llm.main = lambda s, u: "回了"
        await e.say("222", "在吗", at=["900"])  # 重建还在跑，回复照常出去
        assert bot.sent and bot.sent[-1][2] == "回了" and time.time() - t0 < 1.5 and emb.rebuilding
        assert emb.usable()  # 重建期间语义回忆没停摆（检索只认已经算好的新向量）
        await wait_task(emb)
        st = emb.status()
        assert st["indexed"] == 70 and not st["needs_rebuild"]
        # 重建完，用新模型的向量检索得到
        hits = await emb.search("在准备考研，每天泡图书馆", ["iso:qq:100:person:r_haitun"], 3, 0.0, set())
        assert hits and "考研" in hits[0]["content"]
    env.run(body)


def test_status_reports_missing_and_blocked_for_the_panel(env):
    async def body(e):
        emb = e.engine.embedder
        st = emb.status()
        assert {"missing", "blocked", "needs_rebuild", "rebuilding", "progress"} <= set(st) and st["missing"] == 0 and st["blocked"] == ""
        # 风格画像不建向量，不算「缺」
        e.engine.memory.set_style("qq:100", "短句，爱用哈哈")
        assert emb.status()["missing"] == 0 and not emb.status()["needs_rebuild"]
    env.run(body)
