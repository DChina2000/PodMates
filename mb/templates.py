"""角色模板库：assets/templates/*.md，加上数据目录里的 templates/*.md，每个文件一个类别，模板之间用单独一行的 === 隔开。

本地模板：数据目录下的 templates 文件夹（没有就跳过），格式和自带的一样。两个文件夹合在一起按文件名排序，
同 id 的以本地的为准；类别名相同的并成一个类别。

模板文件格式：
    ---
    id: econ-prof
    category: 学术讲解
    name: 经济学教授
    summary: 给守门员看的一句话摘要
    blurb: 面板上的简介
    ---
    外壳文字（"你是……"）

    ## 小节标题
    小节正文……

选一个模板就复制成一个新角色：一个人设（外壳 + 人物表 + 各小节 + 平台格式 + 记忆）和一个角色。
写了 mods 的模板（原创角色）：外壳之外，挂共用内核里挑出来的几个模块（群友性格、拉康派、尼采……荷尔德林、AI身份、构型、说话风格、边界），
外壳在模板正文里，或 builtin 指向 assets/seed/ 里的外壳文件；没写 mods 的模板各有自己的内核模块，不带构型。
文字里的 [[AI|…]] 受面板首页的「承认是AI」开关管：开着照常出现，关着不出现。
"""
import re
from pathlib import Path

from . import personas
from .db import new_id
from .paths import ASSETS, data_dir

DIR = ASSETS / "templates"
# 带「以科普和给方向为主，不下诊断，大事找执业人士」声明的（医疗、营养、法律、心理一类）
DISCLAIMER = "以科普和给方向为主，不下诊断，大事找执业人士"
NEEDS_DISCLAIMER = {"nutritionist", "fitness", "sleep", "emotional-companion", "psych-pop", "law-pop", "psychoanalyst", "night-chat"}

# 外壳放在种子文件里的模板（海豚娘）：builtin 指向这里
BUILTIN_SHELLS = {"haitun": "persona_haitun.md"}

_cache: tuple | None = None


def mod_names(t: dict) -> list[str] | None:
    return [x.strip() for x in t["mods"].split(",") if x.strip()] if t.get("mods") else None


def _parse(chunk: str) -> dict:
    m = re.match(r"\s*---\n(.*?)\n---\n(.*)\Z", chunk.strip() + "\n", re.S)
    if not m:
        raise ValueError("模板格式不对：" + chunk[:40])
    meta = dict(re.findall(r"^(\w+):\s*(.*?)\s*$", m.group(1), re.M))
    body = m.group(2).strip()
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    shell, sections = parts[0].strip(), []
    for title, text in zip(parts[1::2], parts[2::2]):
        sections.append({"title": title, "body": text.strip()})
    return {**meta, "shell": shell, "sections": sections, "disclaimer": meta.get("id") in NEEDS_DISCLAIMER}


def _chunks(text: str) -> list[str]:
    return [c for c in re.split(r"^===\s*$", text, flags=re.M) if c.strip()]


def _sources(with_local: bool = True) -> list[tuple[bool, Path]]:
    """自带的和本地的模板文件合在一起，按文件名排序。"""
    files = [(False, f) for f in DIR.glob("*.md")]
    if with_local:
        local = data_dir() / "templates"
        if local.is_dir():
            files += [(True, f) for f in local.glob("*.md")]
    files.sort(key=lambda x: x[1].name)
    return files


def templates(with_local: bool = True) -> list[dict]:
    global _cache
    files = _sources(with_local)
    key = (with_local, tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for _, f in files))
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    raw = []
    for local, f in files:
        for chunk in _chunks(f.read_text(encoding="utf-8")):
            raw.append({**_parse(chunk), "_local": local, "file": f.name})
    local_ids = {t["id"] for t in raw if t["_local"]}  # 同 id 以本地的为准
    bundled = {t["id"] for t in raw if not t["_local"]}
    out, seen = [], set()
    for t in raw:
        if t["_local"]:
            if t["id"] in seen:  # 本地文件之间同 id：先出现的为准
                continue
            seen.add(t["id"])
        elif t["id"] in local_ids:
            continue
        out.append({**{k: v for k, v in t.items() if k != "_local"}, "local": t["_local"], "bundled": t["id"] in bundled})
    _cache = (key, out)
    return out


def get(tid: str) -> dict:
    t = next((t for t in templates() if t["id"] == tid), None)
    if not t:
        raise KeyError(tid)
    return t


def listing() -> list[dict]:
    """给面板：按类别分组。"""
    cats: dict[str, list] = {}
    for t in templates():
        cats.setdefault(t["category"], []).append({k: t[k] for k in ("id", "name", "blurb", "summary", "local", "bundled")}
                                                  | {"disclaimer": t["disclaimer"], "restorable": _restorable(t)})
    return [{"category": c, "items": items} for c, items in cats.items()]


def local_dir() -> Path:
    return data_dir() / "templates"


def _local_path(name: str) -> Path:
    """本地模板文件只能落在数据目录的 templates 文件夹里。"""
    d = local_dir().resolve()
    p = (d / name).resolve()
    if p.parent != d or p.suffix != ".md":
        raise ValueError("只能读写数据目录 templates 文件夹里的 .md 文件")
    return p


def _restorable(t: dict) -> bool:
    """本功能复制出来的本地文件（local-<id>.md，里面只有这一个模板），而且同 id 的自带模板还在。"""
    if not (t["local"] and t["bundled"] and t["file"] == f"local-{t['id']}.md"):
        return False
    return len(_chunks(_local_path(t["file"]).read_text(encoding="utf-8"))) == 1


def copy_to_local(tid: str) -> Path:
    """把自带模板原样写成数据目录 templates/local-<id>.md；builtin 展开成外壳原文。返回文件路径。"""
    t = get(tid)
    if t["local"]:
        raise ValueError("这个模板已经有本地版本了")
    if not re.fullmatch(r"[\w-]+", tid):
        raise ValueError("模板 id 里有不能用作文件名的字符")
    src = next(c for c in _chunks((DIR / t["file"]).read_text(encoding="utf-8")) if _parse(c)["id"] == tid)
    text = src.strip() + "\n"
    if t.get("builtin") in BUILTIN_SHELLS:  # 外壳在种子文件里：写进正文，去掉 builtin 那行
        head = re.match(r"\s*---\n(.*?)\n---\n", text, re.S).group(1)
        head = "\n".join(x for x in head.split("\n") if not x.startswith("builtin:"))
        text = f"---\n{head}\n---\n" + (ASSETS / "seed" / BUILTIN_SHELLS[t["builtin"]]).read_text(encoding="utf-8").strip() + "\n"
    p = _local_path(f"local-{tid}.md")
    if p.exists():
        raise ValueError(f"本地已经有 {p.name} 这个文件")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def read_local(tid: str) -> dict:
    t = get(tid)
    if not t["local"]:
        raise ValueError("这个模板不是本地模板")
    return {"file": t["file"], "text": _local_path(t["file"]).read_text(encoding="utf-8")}


def save_local(tid: str, text: str) -> None:
    """保存整个本地模板文件。每段都要能解析、有 id / name / category，而且原来的 id 还在。"""
    t = get(tid)
    if not t["local"]:
        raise ValueError("这个模板不是本地模板")
    text = text.replace("\r\n", "\n")
    chunks = _chunks(text)
    if not chunks:
        raise ValueError("内容是空的")
    ids = []
    for i, c in enumerate(chunks, 1):
        try:
            meta = _parse(c)
        except ValueError:
            raise ValueError(f"第 {i} 个模板的格式不对：开头要有 --- 包起来的 id、name、category 等几行")
        lack = [k for k in ("id", "name", "category") if not meta.get(k)]
        if lack:
            raise ValueError(f"第 {i} 个模板缺少：{'、'.join(lack)}")
        ids.append(meta["id"])
    if tid not in ids:
        raise ValueError(f"id 不能改：这个文件里要保留 id 为 {tid} 的模板")
    _local_path(t["file"]).write_bytes(text.encode("utf-8"))


def restore_bundled(tid: str) -> None:
    """删掉本功能复制出来的 local-<id>.md，回到自带的版本。别的本地文件不删。"""
    t = get(tid)
    if not _restorable(t):
        raise ValueError("这个本地模板不是用「复制到本地模板」复制出来的，不能删除；想改它请用「编辑」")
    _local_path(t["file"]).unlink()


def preview(tid: str) -> dict:
    """给面板展开预览：模板的人设全文（外壳 + 各小节）。"""
    t = get(tid)
    shell, sections = t["shell"], t["sections"]
    if t.get("builtin") in BUILTIN_SHELLS:  # 海豚娘的外壳在种子文件里
        shell = (ASSETS / "seed" / BUILTIN_SHELLS[t["builtin"]]).read_text(encoding="utf-8").strip()
    if mod_names(t):  # 外壳之外，列出挂了哪些共用模块，各自的原文
        lib = core_texts()
        sections = [{"title": f"模块：{n}", "body": lib[n]} for n in mod_names(t) if n in lib]
    shell, sections = personas.ai_gate(shell), [{**x, "body": personas.ai_gate(x["body"])} for x in sections]
    return {"id": t["id"], "name": t["name"], "category": t["category"], "blurb": t["blurb"], "summary": t["summary"],
            "shell": shell, "sections": sections, "disclaimer": t["disclaimer"]}


def core_texts() -> dict[str, str]:
    """共用内核各模块的原文（给模板预览看）：来自种子文件，不是作者库里可能改过的版本。键是模块名（构型、格式_纯文本……）。"""
    personas.LIB.setdefault("构型171", personas.bundled_archetype())  # 库还没加载时（比如汇总文档的小工具）用随程序的那份
    mods = personas.split_core((ASSETS / "seed" / "persona_core.md").read_text(encoding="utf-8")) or []
    out = {m["id"][2:]: personas.render_module(m, "qq", {}, keep_words=True) for m in mods}
    for line in (ASSETS / "seed" / "philosophers.md").read_text(encoding="utf-8").splitlines():
        if line.strip():
            out[line[2:line.index("：")]] = line.strip()
    return out


def ensure_shared(db):
    """共用模块（人物表、记忆、平台格式、海豚娘的内核）不在库里就补上。"""
    if db.doc("module", "m_格式_纯文本") and db.doc("module", personas.PEOPLE_ID) and db.doc("archetype", "构型171"):
        return db.kv("core_order") or []
    core = (ASSETS / "seed" / "persona_core.md").read_text(encoding="utf-8")
    order = personas.seed_shared(db, core)
    db.set_kv("core_order", order)
    return order


# 这两类默认不学群里的说话习惯（学术讲解、生活健康要保持自己的口径）
STYLE_OFF_CATEGORIES = ("学术讲解", "生活健康")


def instantiate(db, tid: str, name: str | None = None, role_id: str | None = None, persona_id: str | None = None, settings=None) -> dict:
    """复制一个模板成新角色（含人设）。返回角色文档。传 settings 时，按类别设好这个角色的默认开关。"""
    t = get(tid)
    name = name or t["name"]
    ensure_shared(db)
    # 库里已经有这个模板建出来的人设（人设上记着 template）就直接用，不再复制一份；想要改过的变体用人设页的「复制」
    have = None if persona_id else next((p for p in db.docs("persona") if p.get("template") == tid), None)
    pid = have["id"] if have else persona_id or f"p_{new_id()}"
    pname = personas.unique_name(db, name, pid)
    if have:
        pass
    elif t.get("mods"):
        shell = (ASSETS / "seed" / BUILTIN_SHELLS[t["builtin"]]).read_text(encoding="utf-8") if t.get("builtin") in BUILTIN_SHELLS else t["shell"]
        personas.seed_philosophers(db)
        personas.seed_persona(db, pname, shell, personas.core_ids(mod_names(t)), pid=pid)
    else:
        shell_id = f"m_外壳_{pid[2:]}"
        db.put("module", dict(id=shell_id, name=f"{pname}外壳", kind="exclusive", owner=pid, section="", body=t["shell"]))
        items = [{"m": shell_id, "on": True}, {"m": personas.PEOPLE_ID, "on": True}]
        for i, sec in enumerate(t["sections"]):
            mid = f"m_{pid[2:]}_{i}"
            db.put("module", dict(id=mid, name=sec["title"], kind="exclusive", owner=pid, section=sec["title"], body=sec["body"]))
            items.append({"m": mid, "on": True})
            if sec["title"] == "说话":  # 纯文本格式要求紧跟在「说话」后面，合在同一个小节下
                items.append({"m": "m_格式_纯文本", "on": True})
        items.append({"m": personas.MEMORY_ID, "on": True})
        db.put("persona", dict(id=pid, name=pname, mode="modules", items=items, whole=""))
        personas.save_history(db, pid, "来自角色模板")
    if not have:
        db.put("persona", {**db.doc("persona", pid), "template": tid})
    role = db.put("role", dict(id=role_id or new_id("ro_"), name=name, persona=pid, summary=t["summary"],
                               attitudes={}, relations={}, template=tid, tags=[t["category"]] if t.get("category") else []))
    if settings is not None and t.get("category") in STYLE_OFF_CATEGORIES:
        settings.set(f"role:{role['id']}", "style_follow", False)
    return role
