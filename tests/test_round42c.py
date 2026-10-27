"""发布前修复（丙）：空间紧时按相关度让出备忘、保留天数与人物备忘、参考项目黑名单改用哈希、说明与面板提示。"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def note_row(e, text):
    return e.core.db.rows("SELECT * FROM memories WHERE kind='person' AND content=?", (text,))[0]


# ---------- 10 空间紧时：先让没被提到的人的备忘 ----------
def test_tight_space_gives_up_notes_of_unmentioned_people_first(env):
    async def body(e):
        e.add_bot("901")
        e.core.settings.set("global", "proactive", False)
        mem = e.engine.memory
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "person", "uid:qq:111", "甲", "甲的一条备忘", "self", room="qq:100")
        mem.add(sp, "person", "uid:qq:222", "乙", "乙的一条备忘", "self", room="qq:100")
        subs = [("uid:qq:111", "甲"), ("uid:qq:222", "乙")]
        # 预算只够放一条备忘（前情为空）
        one = len(mem.note_line(note_row(e, "甲的一条备忘"), "甲")) + 1
        parts, _ = mem.compose("r_haitun", "qq", "qq:100", subs, "", weights={"111": 2}, budget=one)
        text = "\n".join(parts)
        assert "甲的一条备忘" in text and "乙的一条备忘" not in text  # 这次提到的甲留下，没被提到的乙先让
        # 反过来：这次提到乙，让掉的是甲
        parts, _ = mem.compose("r_haitun", "qq", "qq:100", subs, "", weights={"222": 2}, budget=one)
        text = "\n".join(parts)
        assert "乙的一条备忘" in text and "甲的一条备忘" not in text
        # 相关分相同时从最早的开始让：丙的备忘比乙的新，先让乙的
        sp2 = "iso:qq:200:person:r_haitun"
        mem.add(sp2, "person", "uid:qq:222", "乙", "乙的旧备忘", "self", room="qq:200")
        mem.add(sp2, "person", "uid:qq:333", "丙", "丙的备忘", "self", room="qq:200")
        e.core.db.x("UPDATE memories SET ts=ts-1000 WHERE content='乙的旧备忘'")
        e.core.db.x("UPDATE memories SET ts=ts+1000 WHERE content='丙的备忘'")
        parts, _ = mem.compose("r_haitun", "qq", "qq:200", [("uid:qq:222", "乙"), ("uid:qq:333", "丙")], "", weights={},
                               budget=len(mem.note_line(note_row(e, "丙的备忘"), "丙")) + 1)
        text = "\n".join(parts)
        assert "丙的备忘" in text and "乙的旧备忘" not in text
        # 都放得下时一条都不让
        parts, _ = mem.compose("r_haitun", "qq", "qq:100", subs, "")
        text = "\n".join(parts)
        assert "甲的一条备忘" in text and "乙的一条备忘" in text
    env.run(body)


# ---------- 12 保留天数与人物备忘 ----------
def test_retention_rewrites_impression_from_remaining_notes_and_clears_when_none_left(env):
    async def body(e):
        e.add_bot("901")
        e.core.settings.set("global", "proactive", False)
        mem = e.engine.memory
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "person", "uid:qq:111", "甲", "甲的第一条", "self", room="qq:100")
        mem.add(sp, "person", "uid:qq:111", "甲", "甲的第二条", "self", room="qq:100")
        e.core.db.x("UPDATE memories SET ts=ts-200000 WHERE content='甲的第一条'")
        mem.save_impression(sp, "uid:qq:111", "甲", "旧印象", 2, "qq:100", None)
        # 另一个人的备忘全过期：印象清空
        mem.add(sp, "person", "uid:qq:222", "乙", "乙的唯一一条", "self", room="qq:100")
        e.core.db.x("UPDATE memories SET ts=ts-200000 WHERE content='乙的唯一一条'")
        mem.save_impression(sp, "uid:qq:222", "乙", "乙的旧印象", 3, "qq:100", None)
        seen = []
        e.llm.impression = lambda p: seen.append(p) or "只剩第二条的印象"
        e.core.settings.set("chat:qq:100", "memory_retention_days", 1)
        mem.prune()
        await e.engine.idle()
        imp = mem.impression_row(sp, "uid:qq:111")
        assert imp and imp["content"] == "只剩第二条的印象"  # 剩下的备忘重写了印象
        assert len(seen) == 1 and "甲的第二条" in seen[0] and "甲的第一条" not in seen[0]
        assert mem.impression_row(sp, "uid:qq:222") is None  # 一条备忘都不剩：清空印象
        assert not e.core.db.rows("SELECT 1 FROM memories WHERE kind='person' AND content IN ('甲的第一条','乙的唯一一条')")
        assert e.core.db.rows("SELECT 1 FROM memories WHERE content='甲的第二条'")
    env.run(body)


def test_retention_leaves_person_notes_alone_when_the_option_is_off(env):
    async def body(e):
        e.add_bot("901")
        mem = e.engine.memory
        sp = "iso:qq:100:person:r_haitun"
        mem.add(sp, "person", "uid:qq:111", "甲", "人物备忘留着", "self", room="qq:100")
        mem.add("chat:qq:100", "recap", "", "", "前情照旧过期", "inferred", room="qq:100")
        e.core.db.x("UPDATE memories SET ts=ts-200000")
        e.core.settings.set("chat:qq:100", "memory_retention_days", 1)
        e.core.settings.set("chat:qq:100", "memory_expire_person", False)
        mem.prune()
        assert e.core.db.rows("SELECT 1 FROM memories WHERE content='人物备忘留着'")
        assert not e.core.db.rows("SELECT 1 FROM memories WHERE content='前情照旧过期'")  # 其余照旧
        # 默认开：同一份数据再清一次，人物备忘就过期了
        e.core.settings.set("chat:qq:100", "memory_expire_person", True)
        mem.prune()
        await e.engine.idle()
        assert not e.core.db.rows("SELECT 1 FROM memories WHERE content='人物备忘留着'")
    env.run(body)


def test_retention_option_defaults_on_next_to_the_retention_days_setting():
    from mb.settings import BY_KEY, SCHEMA
    keys = [s["key"] for s in SCHEMA]
    i = keys.index("memory_retention_days")
    assert keys[i + 1] == "memory_expire_person"  # 摆在「记忆保留天数」旁边
    s = BY_KEY["memory_expire_person"]
    assert s["default"] is True and s["label"] == "保留天数也让人物备忘过期"
    assert s["scopes"] == BY_KEY["memory_retention_days"]["scopes"]  # 和它一个作用域
    assert "一条都不剩就清空印象" in s["help"]
    assert "群梗和语义回忆想起的旧事也算在里面" in BY_KEY["memory_max"]["help"]  # 说明跟着现行的先后走


# ---------- 26 参考项目黑名单只存哈希 ----------
def test_reference_blacklist_is_stored_as_hashes_and_still_catches_names(monkeypatch):
    import hashlib

    import test_round5b
    fake = "notarealreferenceproject"
    digest = hashlib.sha256(fake.encode()).hexdigest()
    assert digest not in test_round5b.BAD  # 名单里只有哈希，没有原文
    monkeypatch.setattr(test_round5b, "BAD", test_round5b.BAD | {digest})
    assert test_round5b.bad_refs(f"见 {fake.upper()} 的做法") == {digest}
    src = (ROOT / "tests/test_round5b.py").read_text(encoding="utf-8")
    assert "bad = [" not in src  # 不再把参考项目名写成列表
    assert len(test_round5b.BAD) >= 10


# ---------- 6 面板：模型不在这一层实际生效的服务商的列表里时提示 ----------
def test_panel_warns_when_the_model_is_missing_from_the_effective_provider():
    js = (ROOT / "web/js/widgets.js").read_text(encoding="utf-8")
    assert "function modelMismatchHint(model, eid)" in js
    assert "endpointModels(ep.id)" in js  # 沿用现成的接口和缓存
    assert "models.some(m => m.id === model)" in js
    assert "if (!models.length || models.some(m => m.id === model)) return;" in js  # 两者都没有：不提示
    assert "it.key.replace(/_model$/, '_endpoint')" in js  # 按这一层实际生效的服务商
    assert "r.live" in js  # 问到了服务商：第一句；只有内置目录：第二句
    en = json.loads((ROOT / "web/i18n/en.json").read_text(encoding="utf-8"))
    for zh in ("「{0}」现在提供的模型里没有「{1}」，这样调用会出错。点「选择…」换一个。",
               "内置目录里「{0}」下没有「{1}」。如果是刚出的新模型可以不管；不是的话，点「选择…」换一个。"):
        assert zh in en and en[zh] and not re.search(r"[㐀-鿿]", en[zh])
    # 面板上真的会重画：保存后 items 换了就 render()，提示跟着服务商 / 模型更新
    assert "items = r.items" in js and "render();" in js


# ---------- 4 / 5 说明写准 ----------
def test_guides_describe_the_current_memory_order_retention_and_panel_warning():
    cn = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    # 10 空间紧时的先后 + 理由 + 群梗 / 语义回忆也算在里面
    assert "放不下先让这次没被提到的人的、同一相关分里从最早的让" in cn
    assert "最粗一层是群里旧事唯一的完整覆盖；备忘的内容前情里大多也有，人的现状又有印象兜着" in cn
    assert "群梗和语义回忆想起的旧事也算在这一节的空间里（含「记忆最多带多少」）" in cn
    assert "the notes of people not mentioned this time give way first, earliest first within the same relevance score" in en
    assert "The coarsest layer is the only complete coverage of the chat's old events" in en
    assert "Group memes and old things recalled by semantic recall also count towards this section's room" in en
    # 12 保留天数也让人物备忘过期
    assert "「保留天数也让人物备忘过期」默认开" in cn and "一条都不剩就清空印象" in cn
    assert "Also expire notes about people with the retention days" in en and "the impression is cleared" in en
    # 14 30 字下限
    assert "算出来不足 30 字时按 30 字算，免得并得太短读不懂" in cn
    assert "the limit is 30, so a merge does not become too short to understand" in en
    # 15 禁言查询一直失败：48 / 6 小时自动恢复并写日志
    assert "查询一直失败满 48 小时" in cn and "自动恢复发言，并在日志里记一笔" in cn
    assert "the query keeps failing for 48 hours" in en and "resumes speaking, with a line in the log" in en
    # 19 重建向量期间只用已经算好的，不混用新旧模型
    assert "只用已经算好的那部分，不混用新旧模型" in cn
    assert "uses only the part already calculated during a rebuild, never mixing old and new models" in en
    # 5.1 删除备忘属于谁就重写谁的印象
    assert "删掉的备忘属于谁，就用这个人剩下的备忘重写谁的印象（不看印象里有没有同样的字）" in cn
    assert "whoever the deleted notes belong to, that person's impression is rewritten" in en
    # 5.3 语义取回的旧备忘带日期
    assert "语义取回的旧备忘和普通备忘一样带日期" in cn
    assert "old notes recalled by semantic recall carry a date just like ordinary notes" in en
    # 5.4 别的机器人按分层继承
    assert "和别的插话设置一样按分层继承（全局、角色、平台、群）；单个角色可以设成不跳过守门员，只影响这个角色" in cn
    assert "layered like any other chime-in setting (global, role, platform, chat); a single role can be set not to skip the gatekeeper" in en
    # 5.5 / 5.6 记忆空间与风格画像
    assert "文件片段按「文件片段」这个种类选空间；几个群的风格画像绑到同一个空间时共用一份" in cn
    assert "几个群绑到同一个记忆空间时共用一份，清空也一起清" in cn
    assert "File excerpts choose their space by the “file excerpts” kind" in en
    assert "they share one copy, and clearing it clears it for all of them" in en
    # 6 说明补一句面板会提示
    assert "模型不在所选服务商的列表里时，面板会在这一项下面提示" in cn
    assert "When the model is not in the selected provider's list, the panel shows a note under that field" in en
