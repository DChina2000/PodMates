"""构建前下载随安装包带的向量模型（bge-small-zh-v1.5，MIT）。模型文件不进仓库，这里按 MANIFEST.json 下载并校验。

依次试 ModelScope、Hugging Face、hf-mirror；校验 sha256，对不上就删掉重来。
用法：python tools/fetch_model.py
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "assets" / "models" / "bge-small-zh-v1.5"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main():
    m = json.loads((DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    for f in m["files"]:
        dest = DIR / f["dest"]
        if dest.exists() and sha(dest) == f["sha256"]:
            print(f"{f['dest']} 已就绪")
            continue
        for tpl in m["sources"]:
            url = tpl.format(repo=m["repo"], path=f["path"])
            try:
                print("下载", url, flush=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as out:
                    while chunk := r.read(1 << 16):
                        out.write(chunk)
                if sha(tmp) != f["sha256"]:
                    tmp.unlink()
                    print("  校验不对，换下一个源")
                    continue
                tmp.replace(dest)
                break
            except Exception as e:
                print("  失败：", e)
        else:
            sys.exit(f"{f['dest']} 下载失败：所有源都试过了。可以手动下载放进 {DIR}")
    print("向量模型已就绪")


if __name__ == "__main__":
    main()
