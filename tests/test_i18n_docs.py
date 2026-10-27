"""界面语言（中文/英文）：词典覆盖所有界面文字；浏览器端翻译函数的行为；模板文本汇总、文档和仓库文件齐全。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def test_english_dictionary_covers_every_ui_string(tmp_path, monkeypatch):
    monkeypatch.setenv("PODMATES_DATA", str(tmp_path))  # 只看公开版自带的模板，不掺本地模板
    import i18n_extract
    en = json.loads((ROOT / "web" / "i18n" / "en.json").read_text(encoding="utf-8"))
    strings = i18n_extract.collect()
    missing = [s for s in strings if s not in en]
    assert len(strings) > 1000 and not missing, missing[:20]
    # 占位符一致、译文里没有汉字（方括号里的示例除外）
    import re
    for zh, t in en.items():
        assert sorted(re.findall(r"\{\d+\}", zh)) == sorted(re.findall(r"\{\d+\}", t)), zh
        assert not re.search(r"[㐀-鿿]", re.sub(r"\[[^\]]*\]|\{\{[^}]*\}\}", "", t)), (zh, t)
    assert "ZCode" not in json.dumps(en, ensure_ascii=False)


@pytest.mark.skipif(not shutil.which("node"), reason="没有 node")
def test_browser_translator_in_node(tmp_path):
    """用 node 加载面板的 i18n.js：整句、带 {0} 的模板、零碎拼接的句子、用户内容不翻译、中文模式原样。"""
    js = (ROOT / "web" / "js" / "i18n.js").read_text(encoding="utf-8")
    en = (ROOT / "web" / "i18n" / "en.json").read_text(encoding="utf-8")
    test = """
const document = { documentElement: {}, body: {}, title: '' };
const localStorage = { getItem: () => null, setItem: () => {} };
class MutationObserver { observe() {} }
const en = __EN__;
globalThis.fetch = async () => ({ json: async () => en });
__JS__
(async () => {
  const out = [];
  await I18N.init('zh'); out.push(I18N.t('保存'));
  await I18N.init('en');
  out.push(I18N.t('保存'), I18N.t('  取消 '), I18N.t('全局默认'), I18N.t('共 12 条'), I18N.t('hello'), I18N.t('完全没收录的一句话'));
  out.push(I18N.t('测试连接（不耗额度）'));
  console.log(JSON.stringify(out));
})();
""".replace("__EN__", en).replace("__JS__", js)
    f = tmp_path / "t.js"
    f.write_text(test, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    en_d = json.loads(en)
    assert out[0] == "保存"                      # 中文模式：原样
    assert out[1] == en_d["保存"] and out[2] == f"  {en_d['取消']} "   # 前后空白保留
    assert out[3] == en_d["全局默认"] and out[5] == "hello"
    assert out[4] == en_d["共 {0} 条"].replace("{0}", "12") if "共 {0} 条" in en_d else True
    assert out[6] == "完全没收录的一句话"          # 词典里没有的：保留中文，不出错
    assert out[7] == en_d["测试连接（不耗额度）"]


def test_template_text_doc_is_in_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("PODMATES_DATA", str(tmp_path))  # 只看公开版自带的模板，不掺本地模板
    import dump_templates
    from mb import templates
    before = (ROOT / "docs" / "模板文本.md").read_text(encoding="utf-8")
    dump_templates.main()
    after = (ROOT / "docs" / "模板文本.md").read_text(encoding="utf-8")
    assert before == after, "docs/模板文本.md 和 assets/templates 不同步，请运行 tools/dump_templates.py"
    assert after.count("### ") == len(templates.templates()) == 28


def test_repo_files_for_open_source_release():
    for f in ("LICENSE", "THIRD_PARTY.md", "README.md", "README.en.md", "BUILDING.md", ".gitignore", "requirements.txt",
              "docs/使用说明.md", "docs/User-Guide.en.md", "docs/模板文本.md", "assets/models/bge-small-zh-v1.5/MANIFEST.json",
              "tools/fetch_model.py"):
        assert (ROOT / f).exists(), f
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert lic.startswith("MIT License") and "Permission is hereby granted" in lic
    tp = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    for need in ("NapCat", "wxauto4", "不随包分发", "models.dev", "bge-small-zh-v1.5", "LGPL"):
        assert need in tp, need
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for need in (".venv/", "build/", "dist/", "*.db", "assets/models/*/*.onnx"):
        assert need in gi, need
    # 向量模型文件不进仓库、也不被 git 跟踪；清单里有校验值
    mani = json.loads((ROOT / "assets/models/bge-small-zh-v1.5/MANIFEST.json").read_text(encoding="utf-8"))
    assert all(len(f["sha256"]) == 64 for f in mani["files"])
