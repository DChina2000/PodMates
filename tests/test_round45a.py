"""模板库：复制到本地模板、编辑本地模板、恢复成自带的。"""
import re
from pathlib import Path

import pytest

from mb import templates
from mb.paths import data_dir

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = "econ-prof"


def local(name: str = "") -> Path:
    return data_dir() / "templates" / name if name else data_dir() / "templates"


def original_chunk(tid: str) -> str:
    for f in (ROOT / "assets" / "templates").glob("*.md"):
        for c in re.split(r"^===\s*$", f.read_text(encoding="utf-8"), flags=re.M):
            if c.strip() and re.search(rf"^id: {tid}$", c, re.M):
                return c
    raise AssertionError(tid)


def item(tid: str) -> dict:
    return next(x for c in templates.listing() for x in c["items"] if x["id"] == tid)


def test_copy_writes_the_original_chunk(env):
    assert item(BUNDLED)["local"] is False and item(BUNDLED)["bundled"] is True and item(BUNDLED)["restorable"] is False
    before = {c["category"]: sorted(x["id"] for x in c["items"]) for c in templates.listing()}
    cat_order = list(before)
    p = templates.copy_to_local(BUNDLED)
    assert p == (local() / f"local-{BUNDLED}.md").resolve() or p == local(f"local-{BUNDLED}.md")
    assert p.read_bytes().decode("utf-8") == original_chunk(BUNDLED).strip() + "\n"
    assert b"\r" not in p.read_bytes()
    t = item(BUNDLED)  # 复制后这个 id 用本地版本，仍在原来的类别里（类别顺序照旧；文件名 local- 排在后面，卡片排到类别末尾）
    assert t["local"] is True and t["bundled"] is True and t["restorable"] is True
    after = templates.listing()
    assert {c["category"]: sorted(x["id"] for x in c["items"]) for c in after} == before and [c["category"] for c in after] == cat_order
    assert next(c for c in after if c["category"] == templates.get(BUNDLED)["category"])["items"][-1]["id"] == BUNDLED
    assert [x["id"] for x in templates.templates()].count(BUNDLED) == 1
    with pytest.raises(ValueError):  # 已有本地版本：不再复制
        templates.copy_to_local(BUNDLED)
    with pytest.raises(KeyError):
        templates.copy_to_local("没有这个")


def test_copy_expands_builtin_shell(env):
    seed = (ROOT / "assets/seed/persona_haitun.md").read_text(encoding="utf-8").strip()
    pv = templates.preview("haitun")
    p = templates.copy_to_local("haitun")
    text = p.read_text(encoding="utf-8")
    assert "builtin" not in text and "mods: 群友性格" in text and text.rstrip().endswith(seed) and "id: haitun" in text
    t = templates.get("haitun")
    assert not t.get("builtin") and t["shell"] == seed and t["local"] and t["bundled"]
    assert templates.preview("haitun") == pv  # 预览一字不差


def test_edit_validates_and_applies(env):
    templates.copy_to_local(BUNDLED)
    d = templates.read_local(BUNDLED)
    assert d["file"] == f"local-{BUNDLED}.md" and d["text"] == original_chunk(BUNDLED).strip() + "\n"
    ok = d["text"].replace("name: 经济学教授", "name: 我的经济学教授")
    assert ok != d["text"]
    bad = {
        "格式": "随便写点什么",
        "空": "   \n",
        "缺 name": re.sub(r"^name: .*\n", "", d["text"], flags=re.M),
        "缺 category": re.sub(r"^category: .*\n", "", d["text"], flags=re.M),
        "改了 id": d["text"].replace(f"id: {BUNDLED}", "id: other-id"),
    }
    for why, text in bad.items():
        with pytest.raises(ValueError):
            templates.save_local(BUNDLED, text)
        assert templates.read_local(BUNDLED)["text"] == d["text"], why  # 不合格：文件没动
    with pytest.raises(ValueError, match="name"):
        templates.save_local(BUNDLED, bad["缺 name"])
    templates.save_local(BUNDLED, ok.replace("\n", "\r\n"))  # 浏览器可能给 \r\n
    assert templates.get(BUNDLED)["name"] == "我的经济学教授" and item(BUNDLED)["name"] == "我的经济学教授"
    assert b"\r" not in local(f"local-{BUNDLED}.md").read_bytes()
    with pytest.raises(ValueError):  # 自带的不是本地模板：不能读也不能写
        templates.read_local("translator")
    with pytest.raises(ValueError):
        templates.save_local("translator", ok)


def test_restore_deletes_only_the_copied_file(env):
    templates.copy_to_local(BUNDLED)
    (local() / "0-local-originals.md").write_text(
        "---\nid: translator\ncategory: 实用工具\nname: 我的翻译\nsummary: s\nblurb: b\n---\n外壳。\n", encoding="utf-8")
    assert item("translator")["local"] and item("translator")["bundled"] and item("translator")["restorable"] is False
    with pytest.raises(ValueError):  # 作者自己放的文件：不给删
        templates.restore_bundled("translator")
    assert (local() / "0-local-originals.md").exists() and templates.get("translator")["name"] == "我的翻译"
    templates.save_local("translator", (local() / "0-local-originals.md").read_text(encoding="utf-8").replace("我的翻译", "改过的翻译"))  # 但能编辑
    assert templates.get("translator")["name"] == "改过的翻译"
    templates.restore_bundled(BUNDLED)
    assert not local(f"local-{BUNDLED}.md").exists() and (local() / "0-local-originals.md").exists()
    assert item(BUNDLED)["local"] is False and item(BUNDLED)["restorable"] is False
    with pytest.raises(ValueError):  # 已经是自带的了
        templates.restore_bundled(BUNDLED)
    # 复制出来的文件里被加了别的模板：不当作「一个模板的副本」，不给删
    templates.copy_to_local(BUNDLED)
    f = local(f"local-{BUNDLED}.md")
    f.write_text(f.read_text(encoding="utf-8") + "===\n---\nid: extra\ncategory: 实用工具\nname: 额外\nsummary: s\nblurb: b\n---\n外壳。\n", encoding="utf-8")
    assert item(BUNDLED)["restorable"] is False
    with pytest.raises(ValueError):
        templates.restore_bundled(BUNDLED)
    assert f.exists()
    # 只存在于本地、没有自带版本的模板：不给删
    (local() / "local-mine.md").write_text("---\nid: mine\ncategory: 我的\nname: 我的\nsummary: s\nblurb: b\n---\n外壳。\n", encoding="utf-8")
    assert item("mine")["bundled"] is False and item("mine")["restorable"] is False
    with pytest.raises(ValueError):
        templates.restore_bundled("mine")


def test_paths_stay_inside_the_local_templates_folder(env):
    for bad in ("../x.md", "sub/../../x.md", "a/b.md", "x.txt", "..", "/etc/x.md"):
        with pytest.raises(ValueError):
            templates._local_path(bad)
    assert templates._local_path("ok.md").parent == local().resolve()
    # 本地模板 id 里带路径也复制不出去（它已经是本地的，不会走复制）
    local().mkdir(parents=True, exist_ok=True)
    (local() / "x.md").write_text("---\nid: ../evil\ncategory: 我的\nname: 坏\nsummary: s\nblurb: b\n---\n外壳。\n", encoding="utf-8")
    with pytest.raises(ValueError):
        templates.copy_to_local("../evil")
    assert not (data_dir() / "evil.md").exists() and not (data_dir() / "templates" / ".." / "local-..").exists()


def test_template_api(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        lst = await (await c.get("/api/templates")).json()
        it = next(x for cat in lst for x in cat["items"] if x["id"] == BUNDLED)
        assert it["local"] is False and it["bundled"] is True
        r = await c.post(f"/api/templates/{BUNDLED}/copy_local")
        j = await r.json()
        assert r.status == 200 and j["file"] == f"local-{BUNDLED}.md" and Path(j["path"]).is_file() and Path(j["path"]).parent.name == "templates"
        r = await c.post(f"/api/templates/{BUNDLED}/copy_local")  # 已经有本地版本
        assert r.status == 400 and (await r.json())["error"]
        lst = await (await c.get("/api/templates")).json()
        it = next(x for cat in lst for x in cat["items"] if x["id"] == BUNDLED)
        assert it["local"] and it["bundled"] and it["restorable"]
        g = await (await c.get(f"/api/templates/{BUNDLED}/local")).json()
        assert g["file"] == f"local-{BUNDLED}.md" and f"id: {BUNDLED}" in g["text"]
        r = await c.put(f"/api/templates/{BUNDLED}/local", json={"text": g["text"].replace(f"id: {BUNDLED}", "id: zzz")})
        assert r.status == 400 and "id" in (await r.json())["error"]
        r = await c.put(f"/api/templates/{BUNDLED}/local", json={"text": g["text"].replace("name: 经济学教授", "name: 改名了")})
        assert r.status == 200
        assert (await (await c.get(f"/api/templates/{BUNDLED}")).json())["name"] == "改名了"
        assert (await c.get("/api/templates/translator/local")).status == 400  # 自带的，不是本地模板
        assert (await c.post("/api/templates/不存在/copy_local")).status == 404
        assert (await c.get("/api/templates/..%2Fx/local")).status == 404
        r = await c.post(f"/api/templates/{BUNDLED}/restore")
        assert r.status == 200 and not (data_dir() / "templates" / f"local-{BUNDLED}.md").exists()
        assert (await c.post(f"/api/templates/{BUNDLED}/restore")).status == 400
        await c.close()
    env.run(body)


def test_panel_has_the_three_buttons_and_texts():
    js = (ROOT / "web/js/pages-new.js").read_text(encoding="utf-8")
    for s in ("复制到本地模板", "恢复成自带的", "'编辑'", "已复制到本地模板：${r.path}。以后升级不会覆盖它；改它不会改到已经用这个模板建好的人设。"):
        assert s in js, s
    assert js.count("= tplLocal(t,") == 2  # 模板库和模板选择弹窗两处
    for f in ("docs/使用说明.md", "docs/User-Guide.en.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert "local-<" in text, f
