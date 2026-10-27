"""服务商目录：内置几十家，选一家填密钥就能用；也可以自定义 API。

数据来自 models.dev（MIT，Copyright (c) 2025 models.dev），精简成 assets/providers/catalog.json 随程序带一份，
也能联网更新（存在数据目录里，优先用新的）。models.dev 没有的几家（百度千帆、讯飞星火、腾讯混元、本机 Ollama 等）手工补在 SUPPLEMENT。
三种协议：openai（OpenAI 兼容 /chat/completions）、anthropic（/v1/messages）、gemini（generateContent）。
"""
import json
import time
from pathlib import Path

import aiohttp

from .paths import ASSETS, data_dir
from . import tls

CATALOG_BUNDLED = ASSETS / "providers" / "catalog.json"
MODELS_DEV_URL = "https://models.dev/api.json"

# 随程序带的精简目录里收哪几家（联网更新时会放宽到所有能用「填一个密钥」接入的）
CURATED = [
    "deepseek", "zhipuai", "zhipuai-coding-plan", "zai", "zai-coding-plan", "alibaba-cn", "alibaba",
    "moonshotai-cn", "moonshotai", "kimi-code-plan-cn", "siliconflow-cn", "siliconflow", "volcengine",
    "volcengine-coding-plan", "minimax-cn", "minimax", "stepfun", "stepfun-ai", "modelscope", "xiaomi", "longcat",
    "sensenova", "bailing", "tencent-tokenhub", "openai", "anthropic", "google", "xai", "mistral", "groq",
    "togetherai", "openrouter", "cerebras", "deepinfra", "fireworks-ai", "perplexity", "huggingface", "nvidia",
    "novita-ai", "sambanova", "nebius", "cohere", "302ai", "aihubmix", "poe", "lmstudio", "ollama-cloud",
]
# 不是「填一个密钥」就能接的（要项目、区域、多个字段），联网更新时也排除
EXCLUDE = {"azure", "azure-cognitive-services", "amazon-bedrock", "google-vertex", "google-vertex-anthropic", "sap-ai-core",
           "watsonx", "cloudflare-ai-gateway", "cloudflare-workers-ai", "gitlab", "databricks", "snowflake-cortex", "oci"}

# models.dev 里没有基础地址 / 没有这家的：手工补（地址来自各家公开文档，导入前请在面板里点「测试连接」核对）
SUPPLEMENT = {
    "openai": dict(name="OpenAI", api="https://api.openai.com/v1", env=["OPENAI_API_KEY"], protocol="openai"),
    "anthropic": dict(name="Anthropic", api="https://api.anthropic.com", env=["ANTHROPIC_API_KEY"], protocol="anthropic"),
    "google": dict(name="Google Gemini", api="https://generativelanguage.googleapis.com/v1beta",
                   env=["GEMINI_API_KEY", "GOOGLE_API_KEY"], protocol="gemini"),
    "xai": dict(name="xAI", api="https://api.x.ai/v1", env=["XAI_API_KEY"], protocol="openai"),
    "mistral": dict(name="Mistral", api="https://api.mistral.ai/v1", env=["MISTRAL_API_KEY"], protocol="openai"),
    "groq": dict(name="Groq", api="https://api.groq.com/openai/v1", env=["GROQ_API_KEY"], protocol="openai"),
    "togetherai": dict(name="Together AI", api="https://api.together.xyz/v1", env=["TOGETHER_API_KEY"], protocol="openai"),
    "cerebras": dict(name="Cerebras", api="https://api.cerebras.ai/v1", env=["CEREBRAS_API_KEY"], protocol="openai"),
    "deepinfra": dict(name="DeepInfra", api="https://api.deepinfra.com/v1/openai", env=["DEEPINFRA_API_KEY"], protocol="openai"),
    "perplexity": dict(name="Perplexity", api="https://api.perplexity.ai", env=["PERPLEXITY_API_KEY"], protocol="openai"),
    "cohere": dict(name="Cohere", api="https://api.cohere.ai/compatibility/v1", env=["COHERE_API_KEY"], protocol="openai"),
    "fireworks-ai": dict(name="Fireworks AI", api="https://api.fireworks.ai/inference/v1", env=["FIREWORKS_API_KEY"], protocol="openai"),
    "baidu-qianfan": dict(name="百度千帆", api="https://qianfan.baidubce.com/v2", env=["QIANFAN_API_KEY"], protocol="openai",
                          manual=True, doc="https://cloud.baidu.com/doc/qianfan-api/s/3m7of64lb"),
    "iflytek-spark": dict(name="讯飞星火", api="https://spark-api-open.xf-yun.com/v1", env=["SPARK_API_PASSWORD"], protocol="openai",
                          manual=True, doc="https://www.xfyun.cn/doc/spark/HTTP调用文档.html"),
    "tencent-hunyuan": dict(name="腾讯混元", api="https://api.hunyuan.cloud.tencent.com/v1", env=["HUNYUAN_API_KEY"], protocol="openai",
                            manual=True, doc="https://cloud.tencent.com/document/product/1729/111007"),
    "ollama-local": dict(name="Ollama（本机）", api="http://localhost:11434/v1", env=[], protocol="openai", manual=True, local=True,
                         doc="https://github.com/ollama/ollama/blob/main/docs/openai.md"),
}
# 在国内一般要走代理才能访问的
NEEDS_PROXY = {"openai", "anthropic", "google", "xai", "mistral", "groq", "togetherai", "cerebras", "deepinfra", "perplexity",
               "cohere", "fireworks-ai", "huggingface", "nvidia", "openrouter", "novita-ai", "sambanova", "nebius", "poe",
               "ollama-cloud", "moonshotai", "alibaba", "siliconflow", "stepfun-ai", "minimax", "zai", "zai-coding-plan"}
# 手工补的常用模型（目录里没有这几家的模型清单）
SUPPLEMENT_MODELS = {
    "baidu-qianfan": ["ernie-4.5-turbo-128k", "ernie-x1-turbo-32k", "deepseek-v3"],
    "iflytek-spark": ["generalv3.5", "4.0Ultra", "lite"],
    "tencent-hunyuan": ["hunyuan-turbos-latest", "hunyuan-t1-latest", "hunyuan-lite"],
    "ollama-local": [],
}

# 「思考强度」各家的写法不一样：GLM/DeepSeek 用 thinking.type + reasoning_effort；OpenAI 系只认 reasoning_effort；
# Anthropic 用 thinking + output_config.effort；Gemini 用 thinkingConfig。不认识的服务商一律不发思考参数（可在端点里改）。
DIALECT_BY_PROVIDER = {
    "deepseek": "deepseek", "zhipuai": "glm", "zhipuai-coding-plan": "glm", "zai": "glm", "zai-coding-plan": "glm",
    "openai": "effort", "xai": "effort", "groq": "effort", "openrouter": "effort", "togetherai": "effort",
    "mistral": "none", "anthropic": "anthropic", "google": "gemini",
}
DIALECTS = [["auto", "自动（按服务商）"], ["none", "不发思考参数"], ["glm", "智谱写法（thinking + reasoning_effort）"],
            ["deepseek", "DeepSeek 写法（thinking + reasoning_effort）"], ["effort", "OpenAI 写法（reasoning_effort）"]]
PROTOCOLS = [["openai", "OpenAI 兼容"], ["anthropic", "Anthropic"], ["gemini", "Gemini"]]


def protocol_of(npm: str) -> str:
    if npm == "@ai-sdk/anthropic":
        return "anthropic"
    if npm == "@ai-sdk/google":
        return "gemini"
    return "openai"


def _efforts(m: dict) -> list[str]:
    for o in m.get("reasoning_options") or []:
        if isinstance(o, dict) and o.get("type") == "effort":
            return list(o.get("values") or [])
    return []


def slim_model(m: dict) -> dict | None:
    if m.get("status") == "deprecated":
        return None
    lim = m.get("limit") or {}
    return {"id": m["id"], "name": m.get("name", m["id"]), "vision": "image" in ((m.get("modalities") or {}).get("input") or []),
            "reasoning": bool(m.get("reasoning")), "tools": bool(m.get("tool_call")), "efforts": _efforts(m),
            "ctx": lim.get("context", 0), "out": lim.get("output", 0)}


def slim(raw: dict, curated: bool = False) -> dict:
    """models.dev 的 api.json → 精简目录。"""
    out = {}
    for pid, p in raw.items():
        if pid in EXCLUDE or (curated and pid not in CURATED):
            continue
        sup = SUPPLEMENT.get(pid, {})
        api = p.get("api") or sup.get("api")
        if not api:
            continue
        models = [x for x in (slim_model(m) for m in (p.get("models") or {}).values()) if x]
        models.sort(key=lambda m: m["id"])
        out[pid] = {"id": pid, "name": sup.get("name") or p.get("name", pid), "api": api, "env": p.get("env") or sup.get("env") or [],
                    "protocol": sup.get("protocol") or protocol_of(p.get("npm", "")), "doc": p.get("doc", ""),
                    "proxy": pid in NEEDS_PROXY, "models": models}
    for pid, sup in SUPPLEMENT.items():
        if pid not in out and curated and (pid in CURATED or sup.get("manual")):
            out[pid] = {"id": pid, "name": sup["name"], "api": sup["api"], "env": sup["env"], "protocol": sup["protocol"],
                        "doc": sup.get("doc", ""), "proxy": pid in NEEDS_PROXY, "manual": bool(sup.get("manual")),
                        "local": bool(sup.get("local")), "models": [{"id": m, "name": m, "vision": False, "reasoning": False,
                                                                     "efforts": [], "ctx": 0, "out": 0} for m in SUPPLEMENT_MODELS.get(pid, [])]}
    return {"version": 1, "source": "models.dev (MIT)", "ts": int(time.time()), "providers": out}


_cache: dict | None = None


def user_catalog_path() -> Path:
    return data_dir() / "providers_catalog.json"


def catalog(reload: bool = False) -> dict:
    """{provider_id: 条目}。数据目录里联网更新过的优先，没有的用随程序带的。"""
    global _cache
    if _cache is None or reload:
        cats = []
        for f in (CATALOG_BUNDLED, user_catalog_path()):
            try:
                cats.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        _cache = {}
        for c in cats:
            _cache.update(c.get("providers", {}))
    return _cache


def provider(pid: str) -> dict | None:
    return catalog().get(pid)


def catalog_list() -> list[dict]:
    """给面板下拉用：不带模型清单，国内常用的排前面。"""
    rows = [{k: v for k, v in p.items() if k != "models"} | {"n_models": len(p["models"])} for p in catalog().values()]
    first = [pid for pid in CURATED if pid in catalog()] + [p for p in SUPPLEMENT if p in catalog() and p not in CURATED]
    order = {pid: i for i, pid in enumerate(first)}
    rows.sort(key=lambda r: (order.get(r["id"], 999), r["name"]))
    return rows


def models_of(pid: str) -> list[dict]:
    p = provider(pid)
    return p["models"] if p else []


async def refresh(proxy: str | None = None, url: str = MODELS_DEV_URL) -> dict:
    """联网更新目录（models.dev，带 ETag 条件请求）。返回 {updated, providers, models}。"""
    etag_file = data_dir() / "providers_catalog.etag"
    headers = {}
    if etag_file.exists() and user_catalog_path().exists():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()
    async with tls.session(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.get(url, proxy=proxy or None, headers=headers) as r:
            if r.status == 304:
                return {"updated": False, "providers": len(catalog()), "models": sum(len(p["models"]) for p in catalog().values())}
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            raw = await r.json(content_type=None)
            etag = r.headers.get("ETag", "")
    cat = slim(raw)
    user_catalog_path().write_text(json.dumps(cat, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    etag_file.write_text(etag, encoding="utf-8")
    catalog(reload=True)
    return {"updated": True, "providers": len(cat["providers"]), "models": sum(len(p["models"]) for p in cat["providers"].values())}


def dialect(ep: dict, model: str = "") -> str:
    d = ep.get("dialect") or "auto"
    if d != "auto":
        return d
    proto = ep.get("protocol", "openai")
    if proto != "openai":
        return proto
    return DIALECT_BY_PROVIDER.get(ep.get("provider", ""), "none")


def model_info(provider_id: str, model: str) -> dict | None:
    return next((m for m in models_of(provider_id) if m["id"] == model), None)


# ---------- 思考档位：按模型目录里的 efforts 来 ----------
EFFORT_RANK = ["minimal", "low", "medium", "high", "xhigh", "max"]
EFFORT_LABEL = {"minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高"}


def effort_levels(ep: dict, model: str) -> list[str]:
    """这个模型真正支持的思考档位（目录里的 efforts，去掉 none，按强弱排）。目录里没有、或这家的写法本来就不发思考参数：返回 []（照老办法，不限制）。"""
    if dialect(ep, model) == "none":
        return []
    vals = ((model_info(ep.get("provider", ""), model) or {}).get("efforts")) or []
    return sorted({v for v in vals if v in EFFORT_RANK}, key=EFFORT_RANK.index)


def fit_effort(ep: dict, model: str, effort: str) -> str:
    """已设的档位换算成模型支持的：不支持就取最接近的（同样近取强的一档，比如 中 → 高）；支持的、空值（不传）一概不动。"""
    levels = effort_levels(ep, model)
    if not effort or not levels or effort in levels or effort not in EFFORT_RANK:
        return effort
    r = EFFORT_RANK.index(effort)
    return min(levels, key=lambda v: (abs(EFFORT_RANK.index(v) - r), -EFFORT_RANK.index(v)))


def effort_options(ep: dict, model: str, current: str) -> list | None:
    """面板上「思考强度」的选项；None = 用设置项自带的全套选项。
    GLM 的 5.x 系列是强制思考、不能关：不给「不思考」，已存的空值显示成「最高（默认）」（不选等于最高）。"""
    levels = effort_levels(ep, model)
    if not levels:
        return None
    opts = [[v, EFFORT_LABEL[v]] for v in levels]
    if dialect(ep, model) == "glm":
        return ([["", "最高（默认）"]] if not current else []) + opts
    return [["", "不思考/默认"]] + opts
