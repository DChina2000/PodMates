"""从 Claude Code 会话记录（jsonl）里按模型汇总 token 用量：输入、缓存写入、缓存读取、输出、合计；
并数「单次请求总上下文（输入 + 缓存写入 + 缓存读取）超过 10 万 token」的请求有几次。取自记录里的实际数，不估算。

用法：python tools/usage_from_jsonl.py <会话.jsonl> [--since 2026-10-24T12:00:00Z] [--agent <子任务.jsonl>…]
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def scan(path: Path, since: str = ""):
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") or {}
        u = m.get("usage")
        if d.get("type") != "assistant" or not u or not m.get("model") or m["model"] == "<synthetic>":
            continue
        if since and (d.get("timestamp") or "") < since:
            continue
        yield m.get("id") or d.get("uuid"), m["model"], u


def tally(path: Path, since: str = ""):
    last = {}
    for key, model, u in scan(path, since):
        last[key] = (model, u)  # 同一个 id 后出现的覆盖先出现的
    by = defaultdict(lambda: dict(input=0, cache_write=0, cache_read=0, output=0, calls=0, big=0))
    for model, u in last.values():
        r = by[model]
        i, cw, cr, o = (u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        r["input"] += i
        r["cache_write"] += cw
        r["cache_read"] += cr
        r["output"] += o
        r["calls"] += 1
        r["big"] += (i + cw + cr) > 100_000
    return by


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--since", default="")
    ap.add_argument("--agent", action="append", default=[])
    a = ap.parse_args()
    out = {"session": {k: v for k, v in tally(Path(a.session), a.since).items()}}
    for i, p in enumerate(a.agent, 1):
        out[f"agent{i}:{Path(p).stem}"] = dict(tally(Path(p)))
    for name, by in out.items():
        for model, r in by.items():
            tot = r["input"] + r["cache_write"] + r["cache_read"] + r["output"]
            print(f"{name}\t{model}\t请求{r['calls']}\t输入{r['input']}\t缓存写入{r['cache_write']}\t缓存读取{r['cache_read']}\t输出{r['output']}\t合计{tot}\t上下文超10万的请求{r['big']}")


if __name__ == "__main__":
    main()
