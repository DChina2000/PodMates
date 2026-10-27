"""共用一个号的角色认错自己：提示词里补上事实（这个号的昵称是几个角色共用的、现在在岗的是谁、之前在岗的是谁），记忆整理时把机器人的话按角色分开。全部用造出来的数据。"""
from mb import memory as memory_mod

HAITUN_UID = "90001"
NOTE_HEAD = "【这个号上的几个角色】"


async def switch(e, to, platform="qq"):
    await e.say(HAITUN_UID, f"换{to}", at=["900"])


def test_system_prompt_states_the_nickname_is_shared_who_is_on_duty_and_who_was_before(env):
    async def body(e):
        e.add_bot("900", name="共用昵称🐬")
        e.add_role("r_乙", "乙角色")
        e.add_role("r_丙", "丙角色")
        e.core.settings.set("global", "proactive", False)
        eng = e.engine
        e.llm.main = lambda s, u: "本豚来也"
        await e.say("111", "你好", at=["900"])
        room = eng.rooms["qq:100"]
        b = eng.duty(eng.bots["ac_900"], room)
        # 这个号上只出现过一个角色：什么都不加
        assert NOTE_HEAD not in await eng.system_prompt(b, room)
        # 换班之后：新角色的提示词里有这段事实
        await switch(e, "乙角色")
        e.llm.main = lambda s, u: "乙角色的话"
        await e.say("111", "再说一句", at=["900"])
        await switch(e, "丙角色")
        b = eng.duty(eng.bots["ac_900"], room)
        assert b.role["name"] == "丙角色"
        text = await eng.system_prompt(b, room)
        note = text.split(NOTE_HEAD)[1].split("\n\n")[0]
        assert "「共用昵称🐬」" in note and "不是哪一个角色专有" in note  # 昵称是几个角色共用的
        assert "现在在岗的是「丙角色」" in note  # 现在是谁
        assert "海豚娘" in note and "乙角色" in note and "丙角色」；" not in note.split("之前在这个号上说过话的还有")[1].split("。")[0]  # 之前是谁，不含现在这位
        assert "各自的自称、口头禅、做过的事，是她们自己的" in note
        assert "不一定是现在的「丙角色」" in note
        # 只陈述，不下禁令：没有「你不是」「不要」「禁止」这类话；人设本身也没被动过
        for banned in ("你不是", "不要", "禁止", "不许", "别再"):
            assert banned not in note
        assert "共用昵称" not in text.split(NOTE_HEAD)[0]
        # 上一轮修过的标注还在：之前角色说的话标成「之前的角色」，不是「你」
        lines = eng.ctx_lines(room, b, "你")
        assert "海豚娘（这个号之前的角色）: 本豚来也" in lines and "乙角色（这个号之前的角色）: 乙角色的话" in lines
        # 换回海豚娘：之前在岗的变成乙角色和丙角色
        await switch(e, "海豚娘")
        b = eng.duty(eng.bots["ac_900"], room)
        note = (await eng.system_prompt(b, room)).split(NOTE_HEAD)[1].split("\n\n")[0]
        assert "现在在岗的是「海豚娘」" in note and "乙角色" in note
        # 主模型真正收到的系统提示词也带着
        e.llm.main = lambda s, u: "回来了"
        await e.say("111", "你是谁", at=["900"])
        main = [c for c in e.llm.calls if c["_kind"] == "main"][-1]
        assert NOTE_HEAD in main["messages"][0]["content"] and "共用昵称🐬" in main["messages"][0]["content"]
    env.run(body)


def test_style_profile_footer_says_whose_catchphrases_those_are_only_when_roles_were_shared(env):
    async def body(e):
        e.add_bot("900", name="共用昵称🐬")
        e.add_role("r_乙", "乙角色")
        eng = e.engine
        e.core.settings.set("global", "proactive", False)
        e.llm.main = lambda s, u: "嗯"
        await e.say("111", "你好", at=["900"])
        room = eng.rooms["qq:100"]
        eng.memory.set_style(room.key, "共用昵称发言爱用‘本豚’")
        b = eng.duty(eng.bots["ac_900"], room)
        assert "这个号的昵称或这个号上之前几个角色的口头禅" not in await eng.system_prompt(b, room)
        await switch(e, "乙角色")
        b = eng.duty(eng.bots["ac_900"], room)
        text = await eng.system_prompt(b, room)
        assert "共用昵称发言爱用‘本豚’" in text and "写到这个号的昵称或这个号上之前几个角色的口头禅、用语，是她们的，不是你的" in text
    env.run(body)


def test_private_chat_gets_the_same_note(env):
    async def body(e):
        e.add_bot("900", name="共用昵称🐬")
        e.add_role("r_乙", "乙角色")
        eng = e.engine
        eng.push_dm("ac_900", "111", "海豚娘", "海豚娘在私聊里说的", "bot", "r_haitun")
        b = eng.duty(eng.bots["ac_900"], None)
        assert NOTE_HEAD not in await eng.system_prompt(b, None, None, "111", "群友")
        acc = e.core.db.doc("account", "ac_900")
        e.core.db.put("account", {**acc, "role_id": "r_乙"})
        eng.reload()
        eng.bots["ac_900"].account = e.core.db.doc("account", "ac_900")
        b = eng.duty(eng.bots["ac_900"], None)
        assert b.role["name"] == "乙角色"
        note = (await eng.system_prompt(b, None, None, "111", "群友")).split(NOTE_HEAD)[1]
        assert "现在在岗的是「乙角色」" in note and "「海豚娘」" in note
        # 另一个人的私聊里没有换过人：不加
        assert NOTE_HEAD not in await eng.system_prompt(b, None, None, "222", "别的群友")
    env.run(body)


def test_memory_extraction_names_bot_lines_by_role_not_by_the_shared_nickname(env):
    async def body(e):
        e.add_bot("900", name="共用昵称🐬")
        e.add_role("r_丙", "丙角色")
        mem = e.engine.memory
        msgs = [
            {"id": 1, "ts": 1.0, "user_id": "111", "name": "群友甲", "text": "你好呀", "role_id": ""},
            {"id": 2, "ts": 2.0, "user_id": "900", "name": "共用昵称🐬〔海豚娘本人〕", "text": "我是海豚娘，本豚上线", "role_id": "r_haitun"},
            {"id": 3, "ts": 3.0, "user_id": "900", "name": "共用昵称🐬〔丙角色本人〕", "text": "我是丙角色", "role_id": "r_丙"},
            {"id": 4, "ts": 4.0, "user_id": "900", "name": "共用昵称🐬〔已删角色本人〕", "text": "老记录", "role_id": "r_gone"},
        ]
        await mem.extract("qq:100", "测试群", msgs, ["r_haitun"])
        prompt = [c for c in e.llm.calls if "请整理成以后聊天用得上的记忆" in str(c.get("messages"))][-1]["messages"][0]["content"]
        assert "[111] 群友甲: 你好呀" in prompt  # 真人的行不变
        assert "[900] 海豚娘（机器人角色）: 我是海豚娘，本豚上线" in prompt and "[900] 丙角色（机器人角色）: 我是丙角色" in prompt
        assert "[900] 已删角色（机器人角色）: 老记录" in prompt  # 角色已经删了：从名字里认
        assert "共用昵称🐬〔" not in prompt  # 账号昵称不再当说话人
        assert "写成同一个人" in prompt and "不要用账号的昵称代替角色名" in prompt
        assert "机器人角色" in memory_mod.STYLE_FIELD and "口头禅和用语不算群友的习惯" in memory_mod.STYLE_FIELD
    env.run(body)


def test_extraction_leaves_existing_memories_alone(env):
    """已有数据只增不改：整理只往后加新的一段，旧的前情和人物备忘一条不动。"""
    async def body(e):
        e.add_bot("900", name="共用昵称🐬")
        mem = e.engine.memory
        sp = mem.space("recap", None, "qq", "qq:100")
        mem.add(sp, "recap", "", "", "老前情：示例小号说了本豚", "inferred", [], "qq:100", meta={"m0": 1, "m1": 1})
        before = [dict(r) for r in e.core.db.rows("SELECT * FROM memories")]
        e.llm.mem = lambda p: {"recap": "新的一段", "people": [], "memes": []}
        await mem.extract("qq:100", "测试群", [{"id": 5, "ts": 5.0, "user_id": "111", "name": "群友甲", "text": "新话", "role_id": ""}], ["r_haitun"])
        after = {r["id"]: dict(r) for r in e.core.db.rows("SELECT * FROM memories")}
        for old in before:
            assert after[old["id"]]["content"] == old["content"]
        assert any(r["content"] == "新的一段" for r in after.values())
    env.run(body)
