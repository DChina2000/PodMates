"""公开版角色库：原创角色挪到最后、只留海豚娘、海豚娘外貌、向导推荐、本地模板文件夹。"""
from pathlib import Path

from mb import templates
from mb.paths import data_dir

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_CATS = ["娱乐陪伴", "学术讲解", "生活健康", "实用工具", "游戏主持", "原创角色"]
HAITUN_LOOK = "- 一头白色长发，眼睛是海水那样清透的蓝，发间别着一枚小小的背鳍发卡。"


def put_local(name: str, text: str) -> None:
    d = data_dir() / "templates"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def tpl(cat: str, name: str, tid: str) -> str:
    return f"---\nid: {tid}\ncategory: {cat}\nname: {name}\nsummary: 摘要\nblurb: 简介\n---\n外壳。\n"


def test_public_library_renamed_and_moved_to_last(env):
    lst = templates.listing()
    assert [c["category"] for c in lst] == PUBLIC_CATS
    assert [x["name"] for x in lst[-1]["items"]] == ["海豚娘"]  # 公开版只留海豚娘一个原创角色
    assert templates.get("haitun")["category"] == "原创角色"
    assert (ROOT / "assets/templates/9原创角色.md").exists()
    assert templates.BUILTIN_SHELLS == {"haitun": "persona_haitun.md"}


def test_haitun_look_without_body_description(env):
    seed = (ROOT / "assets/seed/persona_haitun.md").read_text(encoding="utf-8")
    assert HAITUN_LOOK in seed
    assert "- 滑溜、好动、好奇心重，脑子转得比谁都快。" in seed  # 外壳其余文字不动
    pv = templates.preview("haitun")
    assert HAITUN_LOOK in pv["shell"] and "你是「{{用户}}」的女儿" in pv["shell"]


def test_wizard_no_longer_recommends_haitun(env):
    src = (ROOT / "web/js/wizard.js").read_text(encoding="utf-8")
    feat = src.split("const feat = [", 1)[1].split("];", 1)[0]
    assert "haitun" not in feat and "海豚娘" not in feat
    for name in ("温和版群友", "翻译", "群聊总结与答疑"):
        assert name in feat


def test_local_templates_folder_read_merged_and_overrides(env):
    assert sum(len(c["items"]) for c in templates.listing()) == 28  # 没有 templates 文件夹：不报错
    put_local("0首.md", tpl("首类", "第一个", "local-first"))
    put_local("z尾.md", tpl("尾类", "最后一个", "local-last"))
    lst = templates.listing()
    assert [c["category"] for c in lst][0] == "首类" and [c["category"] for c in lst][-1] == "尾类"  # 按文件名排序
    assert sum(len(c["items"]) for c in lst) == 30
    assert templates.preview("local-first")["shell"] == "外壳。"
    # 同类别的并成一个类别
    put_local("8合并.md", tpl("实用工具", "本地工具", "local-tool"))
    util = [c for c in templates.listing() if c["category"] == "实用工具"]
    assert len(util) == 1 and "local-tool" in [x["id"] for x in util[0]["items"]]
    # 同 id 以本地的为准，只有一个
    put_local("7覆盖.md", tpl("原创角色", "本地的海豚", "haitun"))
    assert templates.get("haitun")["name"] == "本地的海豚"
    assert [t["id"] for t in templates.templates()].count("haitun") == 1


def test_help_and_guides_explain_the_local_templates_folder(env):
    assert "在数据目录里建一个 templates 文件夹" in (ROOT / "web/js/pages-new.js").read_text(encoding="utf-8")
    for f in ("docs/使用说明.md", "docs/User-Guide.en.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert "templates" in text and "assets/templates" in text, f
