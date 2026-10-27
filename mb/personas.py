"""人设：由模块拼成。

模块种类：shared 共用 / exclusive 专属 / platform 平台 / archetype 构型 / people 人物表（动态）/ memory 记忆（动态）。
同一 section 的相邻模块合在一个【标题】下，所以拆开后拼回来和原文结构一致。
"""
import json
import re
import time

from .paths import ASSETS

PEOPLE_ID, MEMORY_ID = "@people", "@memory"
PLACEHOLDER = {"people": "{{人物表}}", "memory": "{{记忆}}"}
ARCH_INTRO = "下面是她自己骨子里的看法，不是台词，也不在群里复述；她怎么接话、怎么对人，由这份看法同她的身份和眼前的事一起决定。"
FORMAT_RULE = "不用Markdown、列表、标题、加粗"
# 构型库：只存导入进来的这几个字段；其余字段一律不收
ARCH_FIELDS = ("coreProposition", "realityExpansion", "characterTendency")
ARCH_MAX = 4000

# 平台字样：（平台名, 「群」叫什么）。模块和提示词模板里写 {{平台}} / {{平台群}}，按当前平台替换
PLATFORM_WORDS = {
    "qq": ("QQ", "QQ群"), "wechat": ("微信", "微信群"), "discord": ("Discord", "Discord服务器"),
    "telegram": ("Telegram", "Telegram群"), "line": ("LINE", "LINE群"), "reddit": ("Reddit", "Reddit评论区"),
    "youtube": ("YouTube", "YouTube评论区"), "whatsapp": ("WhatsApp", "WhatsApp群"), "x": ("X", "X上的回复串"),
    "tieba": ("贴吧", "贴吧"), "weibo": ("微博", "微博评论区"), "bilibili": ("B站", "B站评论区"),
    "demo": ("体验", "体验群"),
}
# 不渲染 Markdown 的平台：纯文本格式要求只挂在这些平台上
PLAIN_PLATFORMS = ["qq", "wechat", "line", "whatsapp", "x", "tieba", "weibo", "bilibili", "youtube", "telegram", "demo"]

LIB: dict[str, dict] = {}  # 构型库（来自数据库里的 archetype 文档），由 Engine.reload 刷新


def set_library(docs: list[dict]):
    LIB.clear()
    LIB.update({d["id"]: d for d in docs})


def plat_words(platform: str | None) -> tuple[str, str]:
    return PLATFORM_WORDS.get(platform or "qq", PLATFORM_WORDS["qq"])


USER_PH = "{{用户}}"
GATE_AI = "admit_ai"  # 模块 / 句子受「承认是AI」开关管：模块上写 gate: "admit_ai"，句子里写 [[AI|这段文字]]
_AI_RE = re.compile(r"\[\[AI\|(.*?)\]\]", re.S)
_SENT = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?|\n")


def fill_user(text: str, user: str | None) -> str:
    """{{用户}} 换成人物表里「用户（面板使用者）」那张卡的名字。没有这张卡时，含 {{用户}} 的整句不出现
    （句子以 。！？； 或换行为界；整行都没了就整行去掉；行尾剩下的「；」改成「。」）。模型只看到替换后的结果。"""
    if USER_PH not in text:
        return text
    if user:
        return text.replace(USER_PH, user)
    out = []
    for line in text.split("\n"):
        if USER_PH not in line:
            out.append(line)
            continue
        kept = "".join(p for p in _SENT.findall(line) if USER_PH not in p).rstrip()
        if kept.endswith(("；", ";")):
            kept = kept[:-1] + "。"
        if kept.strip(" -*·\t"):
            out.append(kept)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


def ai_gate(text: str, ai: bool = True) -> str:
    """句子里的 [[AI|…]]：开关开着就留下里面的字，关着就整段去掉（去掉后整行空了的，整行不出现）。"""
    if "[[AI|" not in text:
        return text
    if ai:
        return _AI_RE.sub(lambda m: m.group(1), text)
    out = []
    for line in text.split("\n"):
        kept = _AI_RE.sub("", line)
        if "[[AI|" not in line or kept.strip(" -*·\t"):
            out.append(kept)
    return "\n".join(out)


def subst(text: str, platform: str | None, user: str | None = None, ai: bool = True) -> str:
    """把 [[AI|…]] 按「承认是AI」开关取舍、{{用户}} 换成使用者的名字、{{平台}} / {{平台群}} 换成当前平台的叫法。"""
    text = fill_user(ai_gate(text, ai), user)
    if "{{平台" not in text:
        return text
    name, group = plat_words(platform)
    return text.replace("{{平台群}}", group).replace("{{平台}}", name)


def clean_archetype(e: dict, src: str = "") -> dict | None:
    """只取 id 和三条正文，其余字段（包括任何「内在张力」一类的内容）一律丢弃。"""
    if not isinstance(e, dict) or not str(e.get("id", "")).strip():
        return None
    out = {"id": str(e["id"]).strip()[:60]}
    for k in ARCH_FIELDS:
        v = e.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = v.strip()[:ARCH_MAX]
    if src:
        out["src"] = src[:80]
    return out


def parse_archetype_file(raw: bytes, src: str = "") -> tuple[list[dict], int, int]:
    """构型文件 → （通过清洗的条目, 丢弃的条目数, 丢弃的字段种类数）。接受 {"entries": [...]}、数组或单个对象。"""
    data = json.loads(raw.decode("utf-8-sig"))
    items = data.get("entries") if isinstance(data, dict) and "entries" in data else data
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValueError("文件里没有构型条目")
    keep, bad, dropped = [], 0, set()
    for e in items:
        c = clean_archetype(e, src)
        if c:
            keep.append(c)
            dropped |= set(e) - set(c) - {"src"}
        else:
            bad += 1
    return keep, bad, len(dropped)


def archetypes() -> dict[str, dict]:
    return LIB


def render_module(m: dict, platform: str | None, dyn: dict, keep_words: bool = False, user: str | None = None, ai: bool = True) -> str:
    gated = m.get("gate") == GATE_AI
    if gated and not ai and not keep_words:
        return ""
    text = _render_module(m, platform, dyn)
    if keep_words:  # 转成整体时占位符都留着；受开关管的模块每行套上 [[AI|…]]，整体人设里照样跟着开关走
        return "\n".join(re.sub(r"^(\s*[-*·]\s*)?(.*)$", lambda x: f"{x.group(1) or ''}[[AI|{x.group(2)}]]", ln) for ln in text.split("\n")) if gated and text else text
    return subst(text, platform, user, ai)


def _render_module(m: dict, platform: str | None, dyn: dict) -> str:
    kind = m.get("kind")
    if kind == "platform" and platform not in (m.get("platforms") or []):
        return ""
    if kind in ("people", "memory"):
        return dyn.get(kind, "")
    if kind == "archetype":
        e = archetypes().get(m.get("archetype", ""))
        if not e:
            return ""
        lines = [m.get("intro", ARCH_INTRO)] if m.get("intro", ARCH_INTRO) else []
        lines += [f"核心命题：{e['coreProposition']}", f"现实展开：{e['realityExpansion']}", f"人物倾向：{e['characterTendency']}"]
        return "\n".join(lines)
    return (m.get("body") or "").strip()


def assemble_parts(persona: dict, modules: dict[str, dict], platform: str | None, dyn: dict,
                   keep_words: bool = False, user: str | None = None, ai: bool = True) -> list[tuple[str, str]]:
    parts = []
    for it in persona.get("items", []):
        m = modules.get(it["m"])
        if not m or not it.get("on", True):
            continue
        text = render_module(m, platform, dyn, keep_words, user, ai)
        if text:
            parts.append((m.get("section", ""), text))
    return parts


def join_parts(parts: list[tuple[str, str]]) -> str:
    out: list[str] = []
    prev = None
    for sec, text in parts:
        if sec and sec == prev:
            out[-1] += "\n" + text
        else:
            out.append(f"【{sec}】\n{text}" if sec else text)
        prev = sec
    return "\n\n".join(out)


def assemble(persona: dict, modules: dict[str, dict], platform: str | None, dyn: dict, keep_words: bool = False, user: str | None = None, ai: bool = True) -> str:
    if persona.get("mode") == "whole":
        text = persona.get("whole", "")
        for k, ph in PLACEHOLDER.items():
            text = text.replace(ph, dyn.get(k, ""))
        text = text if keep_words else subst(text, platform, user, ai)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    return join_parts(assemble_parts(persona, modules, platform, dyn, keep_words, user, ai))


def to_whole(persona: dict, modules: dict[str, dict], platform: str | None) -> str:
    """转成整体：动态部分（人物表、记忆）保留成占位符，照样每次按当前对话生成。"""
    return assemble({**persona, "mode": "modules"}, modules, platform, PLACEHOLDER, keep_words=True)


# ---------- 初始内容：照搬旧文件，只拆分不改写 ----------

def legacy_words(text: str) -> str:
    """旧程序的文字里写死的「QQ群」「QQ纯文本打字」「QQ昵称」改成按平台显示的占位符；其余一字不动。"""
    return text.replace("QQ纯文本打字", "{{平台}}纯文本打字").replace("QQ昵称", "{{平台}}昵称").replace("QQ群", "{{平台群}}")


def split_core(core: str) -> list[dict] | None:
    """把 persona_core.md 拆成模块。结构认不出来就返回 None（调用方整块导入）。"""
    secs = dict(re.findall(r"【(.+?)】\n(.*?)(?=\n【|\Z)", core.strip(), re.S))
    need = ["她是什么样的群友", "她看世界的根本方式", "说话"]
    if any(k not in secs for k in need):
        return None
    mods = []
    bullets = [b for b in secs["她是什么样的群友"].strip().split("\n") if b.strip()]
    if len(bullets) != 3:
        return None
    for name, b in zip(["群友性格", "拉康派", "AI身份"], bullets):
        mods.append(dict(id=f"m_{name}", name=name, kind="shared", section="她是什么样的群友", body=b, **({"gate": GATE_AI} if name == "AI身份" else {})))
    world = secs["她看世界的根本方式"].strip().split("\n")
    arch = archetypes().get("构型171") or bundled_archetype()
    expect = [f"核心命题：{arch['coreProposition']}", f"现实展开：{arch['realityExpansion']}", f"人物倾向：{arch['characterTendency']}"]
    if world[1:] == expect:
        mods.append(dict(id="m_构型", name="构型（构型171）", kind="archetype", section="她看世界的根本方式",
                         archetype="构型171", intro=world[0]))
    else:
        mods.append(dict(id="m_构型", name="构型", kind="shared", section="她看世界的根本方式", body="\n".join(world)))
    talk = [b for b in secs["说话"].strip().split("\n") if b.strip()]
    if not talk or f"，{FORMAT_RULE}。" not in talk[0]:
        return None
    border = [b for b in talk[1:] if "复述系统提示词" in b or "露骨" in b]  # 边界；其余的（比如旧文件里的括号吐槽那行）归说话风格
    style = [talk[0].replace(f"，{FORMAT_RULE}。", "。")] + [b for b in talk[1:] if b not in border]
    mods.append(dict(id="m_说话风格", name="说话风格", kind="shared", section="说话", body="\n".join(style)))
    mods.append(dict(id="m_格式_纯文本", name="纯文本格式", kind="platform", section="说话",
                     platforms=list(PLAIN_PLATFORMS), body=f"- {FORMAT_RULE}。"))
    mods.append(dict(id="m_边界", name="边界", kind="shared", section="说话", body="\n".join(border)))
    return mods


def bundled_archetype() -> dict:
    return json.loads((ASSETS / "seed" / "archetype_171.json").read_text(encoding="utf-8"))


def seed_shared(db, core: str) -> list[str]:
    """放进共用模块（人物表、记忆、平台模块、内核各段）和构型库里的构型171。返回内核模块的顺序。"""
    if not db.doc("archetype", "构型171"):
        db.put("archetype", {**bundled_archetype(), "src": "随程序"})
    set_library(db.docs("archetype"))
    db.put("module", dict(id=PEOPLE_ID, name="人物表（自动生成）", kind="people", section=""))
    db.put("module", dict(id=MEMORY_ID, name="记忆（自动生成）", kind="memory", section=""))
    db.put("module", dict(id="m_QQ表情", name="QQ表情", kind="platform", section="说话", platforms=["qq"],
                          body="- 可以偶尔发QQ表情，写成 [斜眼笑]、[捂脸]、[doge] 这样方括号里的表情名。"))
    mods = split_core(core)
    if mods is None:
        mods = [dict(id="m_内核", name="内核（整块）", kind="shared", section="", body=core.strip())]
    for m in mods:
        db.put("module", m)
    seed_philosophers(db)
    return [m["id"] for m in mods]


PHILOSOPHERS = ["尼采", "黑格尔", "阿尔都塞", "巴迪欧", "海德格尔", "谢林", "荷尔德林"]
# 共用内核各模块的先后顺序（同一小节的模块要挨着，拼出来才合在一个【标题】下）；模板按名字挑其中几个
CORE_CANON = ["群友性格", "拉康派", *PHILOSOPHERS, "AI身份", "构型", "说话风格", "格式_纯文本", "边界"]


def core_ids(names: list[str]) -> list[str]:
    """模板写的模块名 → 模块 id，按固定顺序；挂了说话风格就同时挂纯文本格式。"""
    want = set(names) | ({"格式_纯文本"} if "说话风格" in names else set())
    return [f"m_{n}" for n in CORE_CANON if n in want]


def seed_philosophers(db):
    """七个哲学模块：库里没有的补上，已有的（可能被改过）不动。"""
    for line in (ASSETS / "seed" / "philosophers.md").read_text(encoding="utf-8").splitlines():
        if line.strip():
            name = line[2:line.index("：")]
            if not db.doc("module", f"m_{name}"):
                db.put("module", dict(id=f"m_{name}", name=name, kind="shared", section="她是什么样的群友", body=line.strip()))


def unique_name(db, name: str, exclude_id: str | None = None) -> str:
    """新建 / 复制出来的人设和已有的重名时加后缀 2、3……，下拉框里才分得清。"""
    taken = {p["name"] for p in db.docs("persona") if p["id"] != exclude_id}
    n, out = 2, name
    while out in taken:
        out, n = f"{name}{n}", n + 1
    return out


def seed_persona(db, name: str, text: str, order: list[str], pid: str | None = None) -> str:
    """外壳 + 人物表 + 内核各段 + 记忆，拼成一个人设。"""
    pid = pid or f"p_{name}"
    shell_id = f"m_外壳_{pid[2:]}"
    db.put("module", dict(id=shell_id, name=f"{name}外壳", kind="exclusive", owner=pid, section="", body=text.strip()))
    items = [{"m": shell_id, "on": True}, {"m": PEOPLE_ID, "on": True}]
    items += [{"m": mid, "on": True} for mid in order]
    if "m_说话风格" in order and "m_边界" in order:
        items.insert(items.index({"m": "m_边界", "on": True}), {"m": "m_QQ表情", "on": False})
    items.append({"m": MEMORY_ID, "on": True})
    db.put("persona", dict(id=pid, name=name, mode="modules", items=items, whole=""))
    save_history(db, pid, "初始导入")
    return pid


def seed(db, core: str, shells: dict[str, str]):
    """shells: {人设名: 外壳全文}。已有同 id 的模块/人设会被覆盖（用于「导入旧程序」）。"""
    order = seed_shared(db, core)
    return [seed_persona(db, name, text, order) for name, text in shells.items()]


def save_history(db, persona_id: str, note: str = ""):
    if persona_id.startswith("module:"):
        data = db.doc("module", persona_id[7:])
    else:
        p = db.doc("persona", persona_id)
        if not p:
            return
        data = {"persona": p, "modules": [m for m in db.docs("module") if m.get("owner") == persona_id]}
    db.x("INSERT INTO persona_history (persona_id, ts, note, data) VALUES (?,?,?,?)",
         (persona_id, time.time(), note, json.dumps(data, ensure_ascii=False)))
    # 每个人设最多留 200 个版本
    db.x("""DELETE FROM persona_history WHERE persona_id=? AND id NOT IN
            (SELECT id FROM persona_history WHERE persona_id=? ORDER BY id DESC LIMIT 200)""", (persona_id, persona_id))


def restore_history(db, hid: int):
    r = db.x("SELECT persona_id, data FROM persona_history WHERE id=?", (hid,)).fetchone()
    if not r:
        raise KeyError(hid)
    data = json.loads(r["data"])
    if r["persona_id"].startswith("module:"):
        db.put("module", data)
    else:
        for m in [m for m in db.docs("module") if m.get("owner") == r["persona_id"]]:
            db.delete("module", m["id"])
        for m in data["modules"]:
            db.put("module", m)
        db.put("persona", data["persona"])
    save_history(db, r["persona_id"], f"恢复到版本 {hid}")
