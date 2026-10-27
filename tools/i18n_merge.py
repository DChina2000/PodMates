"""把翻译好的 build/i18n/out_*.json 合并进 web/i18n/en.json，并检查：占位符一致、译文里没有汉字、没有空译文。
用法：python tools/i18n_merge.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r"[㐀-鿿]")
PH = re.compile(r"\{\d+\}")


def main():
    en_path = ROOT / "web" / "i18n" / "en.json"
    en_path.parent.mkdir(parents=True, exist_ok=True)
    en = json.loads(en_path.read_text(encoding="utf-8")) if en_path.exists() else {}
    problems = []
    for f in sorted((ROOT / "build" / "i18n").glob("out_*.json")):
        for zh, t in json.loads(f.read_text(encoding="utf-8")).items():
            if not isinstance(t, str) or not t.strip():
                problems.append(("空译文", zh))
                continue
            if sorted(PH.findall(zh)) != sorted(PH.findall(t)):
                problems.append(("占位符不一致", zh, t))
                continue
            if CJK.search(re.sub(r"\[[^\]]*\]|\{\{[^}]*\}\}", "", t)):  # 方括号里的示例（如 [斜眼笑]）和 {{占位}} 里的汉字允许
                problems.append(("译文里还有汉字", zh, t))
            en[zh.strip()] = t.strip()
    en_path.write_text(json.dumps(en, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    print(f"词典共 {len(en)} 条")
    for p in problems:
        print("  问题：", p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
