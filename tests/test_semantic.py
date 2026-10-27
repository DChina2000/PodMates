"""语义回忆：意思相关但不同词、不同人的旧记忆能取回；开关关掉不取；隔离的群取不到别处；同一条只放一次；
换向量来源后要重建，重建前暂停；API 来源走 OpenAI 兼容接口。本机模型用随安装包的 bge-small-zh（离线）。"""


async def add_mem(e, space, kind, subject, name, content, room="qq:100"):
    mid = e.engine.memory.add(space, kind, subject, name, content, "self", room=room, status="active")
    await e.engine.memory.flush_index()
    return mid


def test_semantic_recall_related_meaning(env):
    async def body(e):
        e.add_bot("901")
        e.core.settings.set("global", "proactive", False)
        await add_mem(e, "iso:qq:100:person:r_haitun", "person", "uid:qq:10001", "阿明", "在准备考研，每天泡图书馆")
        await add_mem(e, "iso:qq:100:person:r_haitun", "person", "uid:qq:10002", "小红", "养了一只橘猫叫胖虎")
        # 另一个人（222）说话，没提阿明、没出现「考研」这个词
        await e.say("222", "研究生入学考试复习得怎么样了", at=["901"])
        sysmsg = e.llm.calls[-1]["messages"][0]["content"]
        assert "【想起的旧事】" in sysmsg and "关于阿明（" in sysmsg and "）：在准备考研" in sysmsg  # 人物备忘带日期
        assert "胖虎" not in sysmsg  # 不相关的不带
        # 和哪条都不沾边的话（换个群，免得上一句还在最近几句里）：不硬凑
        await e.say("223", "今天天气不错", at=["901"], chat="300")
        assert "【想起的旧事】" not in e.llm.calls[-1]["messages"][0]["content"]
        # 同一条只放一次：阿明本人在场时，备忘由「在场的人」带上，语义回忆不再重复
        await e.say("10001", "考研真难", at=["901"])
        sysmsg = e.llm.calls[-1]["messages"][0]["content"]
        assert sysmsg.count("在准备考研，每天泡图书馆") == 1
    env.run(body)


def test_semantic_switches(env):
    async def body(e):
        e.add_bot("901")
        s = e.core.settings
        s.set("global", "proactive", False)
        await add_mem(e, "iso:qq:100:person:r_haitun", "person", "uid:qq:10001", "阿明", "在准备考研，每天泡图书馆")
        mem = e.engine.memory
        q = "研究生入学考试复习得怎么样了"
        assert "在准备考研" in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q)
        s.set("global", "semantic_enabled", False)  # 总开关
        assert "在准备考研" not in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q)
        s.set("global", "semantic_enabled", True)
        e.core.db.set_kv("semantic_off", ["iso:qq:100:person:r_haitun"])  # 单个记忆空间关掉
        assert "在准备考研" not in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q)
        e.core.db.set_kv("semantic_off", [])
        assert "在准备考研" in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q)
    env.run(body)


def test_semantic_respects_isolation(env):
    async def body(e):
        e.add_bot("901")
        s = e.core.settings
        mem = e.engine.memory
        await add_mem(e, "chat:qq:100", "recap", "", "", "群里大家约好周六一起去爬泰山")
        await add_mem(e, mem.space("recap", "r_haitun", "qq", "qq:400"), "recap", "", "", "隔离群里有人说下周去海边露营", room="qq:400")
        q1, q2 = "这周末登山的人几点集合", "去海边搭帐篷要带什么"
        assert "爬泰山" in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q1)
        # 隔离的群取不到别处的记忆，别处也取不到隔离群的
        assert "爬泰山" not in await mem.full_section("r_haitun", "qq", "qq:400", [], "", q1)
        assert "露营" in await mem.full_section("r_haitun", "qq", "qq:400", [], "", q2)
        assert "露营" not in await mem.full_section("r_haitun", "qq", "qq:100", [], "", q2)
        assert "露营" not in await mem.full_section("r_haitun", "qq", "qq:200", [], "", q2)
    env.run(body)


def test_switch_source_rebuild_and_api(env):
    async def body(e):
        e.add_bot("901")
        db, emb, mem = e.core.db, e.engine.embedder, e.engine.memory
        for i, t in enumerate(["在准备考研", "养了一只橘猫", "周末去爬山"]):
            await add_mem(e, "iso:qq:100:person:r_haitun", "person", f"uid:qq:{i}", f"人{i}", t)
        assert emb.status()["indexed"] == 3 and emb.usable()
        # 换成 API 来源：旧向量不通用 → 提示重建，重建前语义回忆暂停
        db.set_kv("embedding", {**emb.conf(), "source": "api", "api_base": f"http://127.0.0.1:{e.port}/v4",
                                "api_model": "mock-embed"})
        db.set_secret("embed:api", "k")
        st = emb.status()
        assert st["needs_rebuild"] and st["stale"] == 3 and emb.usable()  # 旧向量不通用，但语义回忆不会因此停摆：只是在补算完之前找不到而已
        assert "想起的旧事" not in await mem.full_section("r_haitun", "qq", "qq:100", [], "", "在准备考研")
        await emb.rebuild()
        st = emb.status()
        assert not st["needs_rebuild"] and st["indexed"] == 3 and emb.usable()
        assert any(c.get("_kind") == "embed" and c["model"] == "mock-embed" for c in e.llm.calls)
        assert "在准备考研" in await mem.full_section("r_haitun", "qq", "qq:100", [], "", "在准备考研吗")
        # 新记忆照样走 API 算向量；用量记在「向量」用途下
        await add_mem(e, "iso:qq:100:person:r_haitun", "person", "uid:qq:9", "人9", "喜欢打羽毛球")
        assert emb.status()["indexed"] == 4
        assert db.x("SELECT COUNT(*) FROM usage WHERE purpose='embed'").fetchone()[0] >= 1
        # 删除记忆后，清理时向量也一起清掉
        db.x("DELETE FROM memories")
        mem.prune()
        assert db.x("SELECT COUNT(*) FROM memvec").fetchone()[0] == 0
    env.run(body)
