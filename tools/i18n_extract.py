"""抽出面板里所有要翻译的中文文字，写到 build/i18n/strings.json（给翻译用），并检查 web/i18n/en.json 覆盖了多少。

抽哪些：
  · web/js/*.js 和 web/index.html 里带汉字的字符串（模板字符串里的 ${…} 换成 {0}、{1}…）；
  · 服务端发给面板的文字：设置项的名字/说明/选项/分类、管理指令名、各平台开通引导、服务商目录里的中文名、角色模板的名字和简介、
    接口返回的错误提示和账号状态说明；
不抽：日志正文、人设模板的正文（那是写给模型看的提示词，不是界面）。

用法：python tools/i18n_extract.py            # 抽取 + 报告覆盖率
      python tools/i18n_extract.py --missing   # 只列没翻译的
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CJK = re.compile(r"[㐀-鿿＀-￯　-〿]")


def js_strings(text: str) -> list[str]:
    """从 JS 源码里抠出带汉字的字符串字面量（跳过注释）。"""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            i = text.find("*/", i) + 2
        elif c in "'\"":
            j, buf = i + 1, []
            while j < n and text[j] != c and text[j] != "\n":
                if text[j] == "\\" and j + 1 < n:
                    buf.append({"n": "\n", "t": "\t"}.get(text[j + 1], text[j + 1]))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            s = "".join(buf)
            if CJK.search(s):
                out.append(s)
            i = j + 1
        elif c == "`":
            j, buf, k = i + 1, [], 0
            while j < n and text[j] != "`":
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif text.startswith("${", j):
                    depth, j = 1, j + 2
                    while j < n and depth:
                        depth += {"{": 1, "}": -1}.get(text[j], 0)
                        j += 1
                    buf.append("{%d}" % k)
                    k += 1
                else:
                    buf.append(text[j])
                    j += 1
            s = "".join(buf)
            if CJK.search(s):
                out.append(s)
            i = j + 1
        else:
            i += 1
    return out


def py_strings(path: Path, pattern: str) -> list[str]:
    """从 Python 源码里按正则抠字符串（用于接口错误提示、账号状态说明）。"""
    out = []
    src = path.read_text(encoding="utf-8")
    for m in re.finditer(pattern, src):
        for g in m.groups():
            if g and CJK.search(g):
                out.append(re.sub(r"\{[^{}]*\}", lambda mm, c=iter(range(99)): "{%d}" % next(c), g))
    return out


def server_strings() -> list[str]:
    from mb import platforms, providers, templates
    from mb.settings import COMMANDS, EFFORTS, EAGER, LANGS, SCHEMA, SCOPE_LABEL
    out = []
    for s in SCHEMA:
        out += [s["label"], s["help"], s["cat"]]
        for o in s.get("options") or []:
            out.append(o[1])
    out += list(COMMANDS.values()) + [x[1] for x in EFFORTS + EAGER + LANGS] + list(SCOPE_LABEL.values()) if isinstance(SCOPE_LABEL, dict) else []
    for p in platforms.PLATFORMS:
        out += [p["name"], p["summary"], p.get("risk", "")] + p["steps"]
        for f in p["fields"]:
            out += [f["label"], f.get("help", ""), f.get("placeholder", "")]
            out += [c[1] for c in f.get("choices", [])]
        out += [f[1] for f in p["flags"]] + [a["label"] for a in p.get("actions", [])]
    out += [d[1] for d in providers.DIALECTS] + [d[1] for d in providers.PROTOCOLS]
    out += [v["name"] for v in __import__("mb.embed", fromlist=["LOCAL"]).LOCAL.values()]
    out += [v["name"] for v in __import__("mb.embed", fromlist=["API"]).API.values()]
    for t in templates.templates(with_local=False):  # 词典跟公开版自带的模板走，不收作者本地的
        out += [t["name"], t.get("blurb", ""), t.get("category", "")]
    for p in ("server", "api2", "engine", "core", "update", "llm"):
        out += py_strings(ROOT / "mb" / f"{p}.py", r'"error":\s*f?"([^"]*[一-鿿][^"]*)"')
        out += py_strings(ROOT / "mb" / f"{p}.py", r"HTTPBadRequest\(text=f?\"([^\"]+)\"\)")
    from mb import customcmd, reset, search
    out += list(reset.CATS.values())
    out += py_strings(ROOT / "mb" / "reset.py", r'ValueError\("([^"]+)"\)')
    out += py_strings(ROOT / "mb" / "templates.py", r'ValueError\(f?"([^"]+)"\)')
    out += list(customcmd.MATCHES.values()) + list(customcmd.ACTIONS.values()) + list(customcmd.PERM_MODES.values())
    out += py_strings(ROOT / "mb" / "customcmd.py", r'ValueError\(f?"([^"]+)"\)')
    for v in search.PROVIDERS.values():
        out += [v[0], v[3]]
    out += py_strings(ROOT / "mb" / "search.py", r'return False, f?"([^"]+)"')
    out += py_strings(ROOT / "mb" / "llm.py", r'"reason":\s*f?"([^"]+)"')
    wx = ROOT / "mb" / "channels" / "wechat.py"  # 微信身份绑定：状态说明是拼出来的，按写法单独抠
    for pat in (r'msg = f?"([^"]+)"', r'return f?"([^"]*[一-鿿][^"]*)"', r'RuntimeError\(f?"([^"]+)"\)'):
        out += py_strings(wx, pat)
    for f in (ROOT / "mb" / "channels").glob("*.py"):
        out += py_strings(f, r'set_status\("[a-z_]+",\s*f?"([^"]+)"\)')
        out += py_strings(f, r'AuthError\(f?"([^"]+)"\)')
    return out


def collect() -> list[str]:
    strings: list[str] = []
    for f in sorted((ROOT / "web" / "js").glob("*.js")):
        strings += js_strings(f.read_text(encoding="utf-8"))
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    strings += re.findall(r'(?:title|content)="([^"]*[一-鿿][^"]*)"', html) + re.findall(r">([^<>]*[一-鿿][^<>]*)<", html)
    strings += server_strings()
    seen, out = set(), []
    for s in strings:
        s = s.strip()
        if s and CJK.search(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def main():
    strings = collect()
    out = ROOT / "build" / "i18n"
    out.mkdir(parents=True, exist_ok=True)
    (out / "strings.json").write_text(json.dumps(strings, ensure_ascii=False, indent=0), encoding="utf-8")
    en_path = ROOT / "web" / "i18n" / "en.json"
    en = json.loads(en_path.read_text(encoding="utf-8")) if en_path.exists() else {}
    missing = [s for s in strings if s not in en]
    if "--missing" in sys.argv:
        print(json.dumps(missing, ensure_ascii=False, indent=0))
        return
    print(f"共 {len(strings)} 条界面文字，已翻译 {len(strings) - len(missing)}，没翻译 {len(missing)}（{100 * (len(strings) - len(missing)) / max(1, len(strings)):.1f}% 覆盖）")


if __name__ == "__main__":
    main()
