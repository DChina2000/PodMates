"""把 models.dev 的 api.json 精简成随程序带的服务商目录 assets/providers/catalog.json。

用法：python tools/build_catalog.py <api.json 路径>
models.dev 是 MIT 许可（Copyright (c) 2025 models.dev），见 THIRD_PARTY.md。只留面板用得到的字段，去掉已弃用的模型。
联网更新（mb/providers.py: refresh）用的是同一套精简函数。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mb import providers  # noqa: E402

if __name__ == "__main__":
    src = Path(sys.argv[1])
    raw = json.loads(src.read_text(encoding="utf-8"))
    cat = providers.slim(raw, curated=True)
    out = ROOT / "assets" / "providers" / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cat, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = sum(len(p["models"]) for p in cat["providers"].values())
    print(f"{len(cat['providers'])} 家服务商，{n} 个模型，{out.stat().st_size // 1024} KB")
