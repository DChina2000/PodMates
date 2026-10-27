"""把所有角色模板的文字汇总成 docs/模板文本.md（交给 Planner 审）。模板源文件在 assets/templates/*.md。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mb import templates  # noqa: E402


def main():
    lines = ["# 角色模板文本", "",
             "由 `python tools/dump_templates.py` 从 `assets/templates/*.md` 汇总生成，面板里「角色 → 从模板新建」用的就是这些文字。",
             "「原创角色」类别的 1 个角色（外壳 + 从共用模块里挑几个：群友性格、拉康派、AI身份、构型171、说话风格、边界）见最后的「共用模块原文」；其余模板各有自己的内核模块，不套用海豚娘的贴吧内核；",
             "平台字样 `{{平台}}` / `{{平台群}}` 按当前平台显示（QQ群 / 微信群 / Discord服务器……）；`{{用户}}` 换成人物表里「用户」卡的名字（没有名字时含它的整句不出现）；",
             "文字里的 `[[AI|…]]` 受面板「总览」页的「承认是AI」总开关管（角色、群可以单独设）：开着照常出现（去掉标记），关着整段不出现；",
             "「说话」小节后面会自动挂上共用的「纯文本格式」平台模块（不用Markdown、列表、标题、加粗，只挂在不渲染 Markdown 的平台上）。",
             "医疗、营养、法律、心理一类模板都写明「以科普和给方向为主，不下诊断，大事找执业人士」。除「原创角色」外，模板默认不带构型。", ""]
    cat = None
    for t in templates.templates(with_local=False):  # 公开文档只收自带的模板，不收作者本地的
        if t["category"] != cat:
            cat = t["category"]
            lines += [f"## {cat}", ""]
        lines += [f"### {t['name']}", "", f"- 模板 id：`{t['id']}`", f"- 一句话摘要（给守门员）：{t['summary']}", f"- 面板简介：{t['blurb']}"]
        if t.get("mods"):
            shell = (ROOT / "assets" / "seed" / templates.BUILTIN_SHELLS[t["builtin"]]).read_text(encoding="utf-8").strip() if t.get("builtin") else t["shell"]
            lines += [f"- 挂的共用模块：外壳 + {' + '.join(templates.mod_names(t))}（另有人物表、记忆、纯文本格式、QQ表情这些自动挂的）", "",
                      "**外壳**", "", "> " + shell.replace("\n", "\n> "), ""]
            continue
        if t["disclaimer"]:
            lines.append("- 带声明：以科普和给方向为主，不下诊断，大事找执业人士")
        lines += ["", "**外壳**", "", "> " + t["shell"].replace("\n", "\n> "), ""]
        for s in t["sections"]:
            lines += [f"**【{s['title']}】**", "", s["body"], ""]
    lines += ["## 共用模块原文", "",
              "「原创角色」里的海豚娘按各自的模块清单从这里挑；改一处，挂了它的角色一起变。`AI身份` 受「承认是AI」总开关管。", ""]
    for name, body in templates.core_texts().items():
        lines += [f"**{name}**", "", "```", body, "```", ""]
    out = ROOT / "docs" / "模板文本.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(templates.templates(with_local=False))} 个模板，{out.stat().st_size // 1024} KB → {out}")


if __name__ == "__main__":
    main()
