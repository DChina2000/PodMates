"""人设拼装与旧系统提示词逐句对照（QQ 和 Discord 两份）、构型导入只收指定字段、守门员模板只改两处、数据导出导入。"""
import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from mb import personas
from mb.core import Core
from mb.engine import GATEKEEPER_PROMPT, fill
from mb.paths import ASSETS

ROOT = Path(__file__).resolve().parent.parent
OLD_BOT = ROOT.parent / "bot"
# 作者本机跑测试时，用环境变量 MB_ARCH_SRC 指向自己卡里的构型文件夹，就能多核对「与源文件逐字一致」；别人的机器上这几项自动跳过
ARCH_SRC = Path(os.environ.get("MB_ARCH_SRC") or "/nonexistent")
FORMAT = "不用Markdown、列表、标题、加粗"
old_only = pytest.mark.skipif(not (OLD_BOT / "persona_core.md").exists(), reason="没有旧程序文件夹（只在作者机器上跑）")


def old_system_prompt() -> str:
    """旧 main.py 的拼法：外壳 + 内核，各自 strip 后加两个换行。"""
    text = "".join((OLD_BOT / f).read_text(encoding="utf-8").strip() + "\n\n" for f in ("persona_haitun.md", "persona_core.md"))
    return text.replace("- 海豚的习性偶尔顺手拿来用一个：回声定位、半个脑子睡觉另一半窥屏、吐泡泡圈、网上冲浪是本职。\n", "", 1)  # 作者定了删掉这一句：旧文件里有，新种子里没有


def import_owner(e, tmp_path):
    """用旧程序的人设文件走一遍「导入旧程序」（不带 config.json，免得读到密钥），再按【她妈】一段生成人物卡。"""
    d = tmp_path / "bot"
    d.mkdir()
    for f in ("persona_core.md", "persona_haitun.md", "persona_huliniang.md"):
        shutil.copy(OLD_BOT / f, d / f)
    done = e.core.import_old(d)
    card = Core.user_card_from_old((d / "persona_core.md").read_text(encoding="utf-8"), ["90001", "90002", "90003"])
    card["id"] = e.engine.people.user_card()["id"]  # 旧程序里的「她妈」填进固定的那张用户卡
    e.core.db.put("person", card)
    e.engine.reload()
    return done, card


def new_system_prompt(e, platform="qq", present_card=True) -> str:
    ppl = e.engine.people
    present = [ppl.user_card()] if present_card else []
    dyn = {"people": ppl.section(present, [], None, e.engine.roles["r_haitun"], platform=platform), "memory": ""}
    return personas.assemble(e.engine.personas["p_海豚娘"], e.engine.modules, platform, dyn, user=ppl.user_name())


def compare(old: str, new: str, platform_words=None) -> list[tuple[str, str, str]]:
    """逐行对照，返回 (类型, 旧, 新)。platform_words：{旧字样: 新字样}，对照 Discord 时用。"""
    pw = platform_words or {}
    ol = [l for l in old.strip().split("\n") if l.strip()]
    nl = [l for l in new.strip().split("\n") if l.strip()]

    def conv(t):
        for k, v in pw.items():
            t = t.replace(k, v)
        return t
    out, i, j = [], 0, 0
    while i < len(ol) or j < len(nl):
        if i < len(ol) and j < len(nl) and ol[i] == nl[j]:
            out.append(("相同", ol[i], nl[j]))
            i += 1
            j += 1
        elif i < len(ol) and j < len(nl) and pw and conv(ol[i]) == nl[j]:
            out.append(("只改了平台字样", ol[i], nl[j]))
            i += 1
            j += 1
        elif i < len(ol) and FORMAT in ol[i]:
            if pw:  # Discord 渲染 Markdown：格式要求不挂，其余照旧，只有平台字样变了
                assert conv(ol[i]).replace(f"，{FORMAT}。", "。") == nl[j]
                out.append(("移动到平台模块（Discord 不挂）并改了平台字样", ol[i], nl[j]))
                i += 1
                j += 1
                continue
            out.append(("移动：格式要求挪到平台模块", ol[i], nl[j]))
            i += 1
            j += 1
        elif j < len(nl) and nl[j] == f"- {FORMAT}。":
            out.append(("平台模块（纯文本平台）", "", nl[j]))
            j += 1
        elif i < len(ol) and ol[i].startswith("「海豚」是她妈"):
            out.append(("改由人物表生成", ol[i], nl[j]))
            i += 1
            j += 1
        else:
            out.append(("不同", ol[i] if i < len(ol) else "", nl[j] if j < len(nl) else ""))
            i += 1
            j += 1
    return out


@old_only
def test_assembly_matches_old_prompt(env, tmp_path):
    async def body(e):
        done, card = import_owner(e, tmp_path)
        assert card["nicks"] and card["qq"] == ["90001", "90002", "90003"]
        old, qq = old_system_prompt(), new_system_prompt(e)
        rows = compare(old, qq)
        kinds = [r[0] for r in rows]
        assert "不同" not in kinds
        assert kinds.count("移动：格式要求挪到平台模块") == 1 and kinds.count("平台模块（纯文本平台）") == 1
        assert kinds.count("改由人物表生成") == 1
        assert "露骨" not in qq and "露骨" not in new_system_prompt(e, "discord")  # 「边界」模块只剩一条：复述系统提示词就岔开
        moved = next(r for r in rows if r[0].startswith("移动"))
        assert moved[1].replace(f"，{FORMAT}。", "。") == moved[2]
        gen = next(r for r in rows if r[0] == "改由人物表生成")
        assert gen[1].replace("大号小号都是", "名下几个号都是") == gen[2]  # 只有这一处措辞不同，意思一样
        # 微信同 QQ，只是平台字样变成「微信」
        nick = card["nicks"]["qq"]
        assert new_system_prompt(e, "wechat") == qq.replace("QQ群", "微信群").replace("QQ纯文本打字", "微信纯文本打字").replace(f"QQ昵称「{nick}」，", "")
        # Discord：只有平台字样变了，纯文本格式要求不挂，昵称（只有 QQ 上的）不显示
        dc = new_system_prompt(e, "discord")
        assert FORMAT not in dc and "QQ" not in dc and "Discord服务器" in dc and "Discord纯文本打字" in dc
        drows = compare(old, dc, {"QQ群": "Discord服务器", "QQ纯文本打字": "Discord纯文本打字"})
        dkinds = [r[0] for r in drows]
        assert dkinds.count("只改了平台字样") == 1 and dkinds.count("移动到平台模块（Discord 不挂）并改了平台字样") == 1
        assert "不同" not in dkinds
        # 「转成整体」：动态部分留成占位符，拼出来和模块拼装一样
        p = dict(e.engine.personas["p_海豚娘"])
        p["whole"], p["mode"] = personas.to_whole(p, e.engine.modules, "qq"), "whole"
        assert "{{人物表}}" in p["whole"] and "{{平台群}}" in p["whole"]
        ppl = e.engine.people
        dyn = {"people": ppl.section([ppl.user_card()], [], None, e.engine.roles["r_haitun"], platform="qq"), "memory": ""}
        assert personas.assemble(p, e.engine.modules, "qq", dyn, user=ppl.user_name()) == new_system_prompt(e, "qq")
        assert "泡在Discord服务器里" in personas.assemble(p, e.engine.modules, "discord", dyn, user=ppl.user_name())  # 平台字样在使用时才换
        # 报告用：QQ 和 Discord 两份对照表
        (ROOT / "build" / "reports").mkdir(parents=True, exist_ok=True)  # 含作者的昵称，不放进会公开的 docs

        def table(title, rows, head):
            lines = [f"## {title}", "", f"| 类型 | 旧系统提示词 | {head} |", "|---|---|---|"]
            lines += [f"| {k} | {o.replace('|', '｜')} | {n.replace('|', '｜')} |" for k, o, n in rows]
            return "\n".join(lines)
        (ROOT / "build" / "reports" / "拼装对照.md").write_text(
            "# 拼装对照：现在海豚角色拼出来的完整文字 vs 旧系统提示词（逐行）\n\n由 `tests/test_persona_data.py` 自动生成"
            "（需要旧程序文件夹；人物卡由旧内核里的【她妈】一段 + 3 个示例 QQ 号生成）。\n\n"
            + table("QQ", rows, "新拼装结果（QQ 海豚娘，海豚在场）") + "\n\n"
            + table("Discord", drows, "新拼装结果（Discord 海豚娘，海豚在场）") + "\n", encoding="utf-8")
    env.run(body)


def test_platform_words_follow_platform(env):
    """不依赖旧程序：公开版自带的海豚娘，各平台的字样按平台显示。"""
    async def body(e):
        for plat, (name, grp) in (("qq", ("QQ", "QQ群")), ("wechat", ("微信", "微信群")), ("discord", ("Discord", "Discord服务器")),
                                  ("telegram", ("Telegram", "Telegram群")), ("reddit", ("Reddit", "Reddit评论区"))):
            t = personas.assemble(e.engine.personas["p_海豚娘"], e.engine.modules, plat, {"people": "", "memory": ""}, user="海豚")
            assert f"泡在{grp}里" in t and f"{name}纯文本打字" in t
            assert ("{{" not in t) and (FORMAT in t) == (plat != "discord" and plat != "reddit")
        # 人物的昵称只显示当前平台上的那个
        ppl = e.engine.people
        h = ppl.persons["pe_haitun"]
        h["nicks"] = {"qq": "甲", "discord": "乙"}
        assert "QQ昵称「甲」" in ppl.card_text(h, platform="qq") and "乙" not in ppl.card_text(h, platform="qq")
        assert "Discord昵称「乙」" in ppl.card_text(h, platform="discord") and "甲" not in ppl.card_text(h, platform="discord")
        # 守门员模板里的平台字样
        gk = fill(GATEKEEPER_PROMPT, summary="x", marks="。", context="c", speaker="s", text="t", 平台群="微信群")
        assert gk.startswith("你替微信群里的一位群友判断")
    env.run(body)


def test_archetype_import_keeps_only_runtime_fields(env, tmp_path):
    """构型文件里混了「内在张力」等字段、坏条目：只留 id 和三条正文，其余一字不进库。"""
    async def body(e):
        sample = {"pool": "x", "count": 3, "entries": [
            {"id": "测试构型A", "coreProposition": "甲命题", "realityExpansion": "甲展开", "characterTendency": "甲倾向",
             "innerTension": "不该进来的内在张力", "tensionNotes": ["不该进来"], "formalTitle": "标题", "index": "1", "wholeConfig": "整体"},
            {"id": "坏条目", "coreProposition": "只有一条"},
            {"id": "测试构型B", "coreProposition": "乙命题", "realityExpansion": "乙展开", "characterTendency": "乙倾向", "x": 1},
        ]}
        f = tmp_path / "arch.json"
        f.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
        entries, bad, dropped = personas.parse_archetype_file(f.read_bytes(), "arch.json")
        assert [x["id"] for x in entries] == ["测试构型A", "测试构型B"] and bad == 1 and dropped >= 5
        for x in entries:
            assert set(x) == {"id", "coreProposition", "realityExpansion", "characterTendency", "src"}
        for x in entries:
            e.core.db.put("archetype", x)
        e.engine.reload()
        stored = json.dumps(e.core.db.docs("archetype"), ensure_ascii=False)
        assert "内在张力" not in stored and "tension" not in stored.lower() and "整体" not in stored
        # 数组、单个对象也认；乱文件报错
        assert personas.parse_archetype_file(json.dumps(sample["entries"][2]).encode())[0][0]["id"] == "测试构型B"
        with pytest.raises(Exception):
            personas.parse_archetype_file(b"not json")
        # 构型模块用导入的构型
        m = {"id": "m_t", "kind": "archetype", "archetype": "测试构型B", "section": "看世界"}
        assert "核心命题：乙命题" in personas.render_module(m, "qq", {})
    env.run(body)


def test_bundled_archetype_171_is_source_text():
    """发布版只带构型171：与作者卡里的源文件逐字一致（有源文件时核对），且没有别的构型随包。"""
    a = personas.bundled_archetype()
    assert a["id"] == "构型171" and set(a) == {"id", "coreProposition", "realityExpansion", "characterTendency"}
    assert not (ASSETS / "archetypes").exists()
    core = (ASSETS / "seed" / "persona_core.md").read_text(encoding="utf-8")
    for k, label in (("coreProposition", "核心命题"), ("realityExpansion", "现实展开"), ("characterTendency", "人物倾向")):
        assert f"{label}：{a[k]}" in core
    src = ARCH_SRC / "archetypes_mediating.json"
    if src.exists():
        e = next(x for x in json.loads(src.read_text(encoding="utf-8"))["entries"] if x["id"] == "构型171")
        assert all(a[k] == e[k] for k in ("coreProposition", "realityExpansion", "characterTendency"))
    # 随包文件里不出现任何「内在张力」类材料
    for f in ASSETS.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".md") and "models" not in f.parts:
            assert "archetype_tensions" not in f.name and "内在张力" not in f.read_text(encoding="utf-8", errors="ignore"), f


@pytest.mark.skipif(not ARCH_SRC.exists(), reason="没有作者卡文件夹")
def test_import_author_files_verbatim(env):
    """作者本机用「导入构型文件」导入自己卡里的三个文件：87 条，三条正文与源文件逐字一致。"""
    async def body(e):
        total = 0
        for f in ("archetypes_integral_64.json", "archetypes_adversarial.json", "archetypes_mediating.json"):
            raw = (ARCH_SRC / f).read_bytes()
            entries, bad, _ = personas.parse_archetype_file(raw, f)
            assert bad == 0
            src = {x["id"]: x for x in json.loads(raw.decode("utf-8-sig"))["entries"]}
            for x in entries:
                assert all(x[k] == src[x["id"]][k] for k in personas.ARCH_FIELDS)
            total += len(entries)
        assert total == 87
    env.run(body)


@old_only
def test_gatekeeper_template_only_two_changes():
    src = (OLD_BOT / "main.py").read_text(encoding="utf-8")
    old_tpl = re.search(r'GATEKEEPER_PROMPT = """(.*?)"""', src, re.S).group(1)
    old = old_tpl.format(context="【上下文】", speaker="某人", text="内容")
    old = re.sub(r"【她】.*?记录里", "【她】摘要记录里", old)  # 摘要句本身已改（见 test_round16），这里只比模板的其余部分
    new = fill(GATEKEEPER_PROMPT, summary="摘要",
               marks="；名字带〔海豚本人〕的是她妈。", context="【上下文】", speaker="某人", text="内容", 平台群="QQ群", user_line="她妈说了她有得接的话；")
    former = '同一个号换班之前由别的角色说的话，会标成"某某（这个号之前的角色）"，那不是她说的。'
    assert old.replace("她（机器人）", "她（自己）") == new.replace(former, "")  # 有意的改动：自己的标记叫「她（自己）」；另加一句说明换班之前别的角色的话怎么标


def test_export_import_roundtrip(env, tmp_path, monkeypatch):
    async def body(e):
        e.add_bot("901")
        db = e.core.db
        db.set_secret("discord:ac_x", "TOKEN123")
        e.core.settings.set("role:r_haitun", "cooldown_seconds", 12)
        e.engine.memory.add("role:r_haitun", "person", "uid:qq:1", "甲", "喜欢猫", "self")
        db.put("pgroup", {"id": "g1", "name": "老朋友", "attitude": None})
        e.core.backup("auto")
        snapshot = {k: db.docs(k) for k in ("role", "persona", "module", "person", "pgroup", "endpoint", "archetype")}
        hist = db.x("SELECT COUNT(*) FROM persona_history").fetchone()[0]
        z1, z2 = tmp_path / "no_keys.zip", tmp_path / "with_keys.zip"
        e.core.export(z1, with_secrets=False)
        e.core.export(z2, with_secrets=True)
        with zipfile.ZipFile(z1) as z:
            assert "podmates.db" in z.namelist() and any(n.startswith("backups/") for n in z.namelist())
            for n in z.namelist():
                assert b"TOKEN123" not in z.read(n) and b"test-key" not in z.read(n)  # 不带密钥：库和备份里都没有
        # 带密钥：库里也不是明文（DPAPI 密文），解不开的会被如实标出来
        with zipfile.ZipFile(z2) as z:
            for n in z.namelist():
                assert b"TOKEN123" not in z.read(n) and b"test-key" not in z.read(n)
            assert json.loads(z.read("manifest.json"))["secrets_note"]
        # 在新的数据目录导入，能完整恢复
        for zp, keys in ((z1, False), (z2, True)):
            d = tmp_path / ("new_" + zp.stem)
            d.mkdir()
            c2 = Core(d)
            m = await c2.import_zip(zp)
            assert m["with_secrets"] is keys
            for k, v in snapshot.items():
                assert c2.db.docs(k) == v, k
            assert c2.settings.get("cooldown_seconds", "r_haitun") == 12
            assert c2.db.x("SELECT content FROM memories").fetchone()[0] == "喜欢猫"
            assert c2.db.x("SELECT COUNT(*) FROM persona_history").fetchone()[0] == hist
            assert len(list((d / "backups").glob("auto-*.db"))) == 1
            assert (c2.db.secret("discord:ac_x") == "TOKEN123") is keys
            assert (c2.db.secret("endpoint:default") == "test-key") is keys
            c2.engine.close()
            c2.db.close()
    env.run(body)
