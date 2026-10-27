"""检查一个服务商的某个模型到底能不能看图：画一张写着三位数字的小图，让模型把数字读出来。会真调一次接口（一张小图，几百 token）。
面板里也有同样的功能（设置 → 模型与接口 → 服务商卡片上的「测试看图」）。

用法：
  python tools/vision_check.py --provider deepseek --model deepseek-flash --key-env DEEPSEEK_API_KEY
  python tools/vision_check.py --provider openai --model gpt-5 --key-env OPENAI_API_KEY --proxy http://127.0.0.1:7890
  python tools/vision_check.py --base-url http://127.0.0.1:11434/v1 --protocol openai --model llava   # 本机 Ollama，不需要密钥

密钥只从环境变量读，不打印、不写文件。
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mb import providers, visioncheck  # noqa: E402
from mb.llm import LLM  # noqa: E402


async def check(provider: str, model: str, key: str, base_url: str = "", protocol: str = "", proxy: str = "", digits: str = "") -> dict:
    class Stub:  # 只用 LLM 的请求拼装和发送，不碰数据库
        def g(self, k):
            return proxy or None
    llm = LLM(None, Stub())
    p = providers.provider(provider) or {}
    ep = {"id": "check", "provider": provider, "protocol": protocol or p.get("protocol", "openai"), "base_url": base_url or p.get("api", ""),
          "use_proxy": bool(proxy)}
    try:
        return await visioncheck.check(llm, ep, key, model, digits)
    finally:
        await llm.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="")
    ap.add_argument("--model", required=True)
    ap.add_argument("--key-env", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--protocol", default="")
    ap.add_argument("--proxy", default="")
    a = ap.parse_args()
    key = os.environ.get(a.key_env, "") if a.key_env else ""
    if a.key_env and not key:
        sys.exit(f"环境变量 {a.key_env} 没设")
    r = asyncio.run(check(a.provider, a.model, key, a.base_url, a.protocol, a.proxy))
    print(("能看图 ✓" if r["ok"] else "没读对 ✗") + f"：应该是 {r['expected']}，模型回答 {r['answer']!r}；用量 {r['usage']}")
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
