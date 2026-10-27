"""模型调用。三种协议（OpenAI 兼容 / Anthropic / Gemini）各写一层很薄的转换，不引入第三方 SDK。

四处用途（主模型 main、守门员 gk、记忆提取 mem、看图 vis）分别取「服务商卡片（端点）」和模型；
主端点出错且开了备用时切到备用端点。每次调用都记用量（含缓存命中、缓存写入、思考 token）。
密钥：先取本机存的（DPAPI 加密），没填就读端点上指定的环境变量。
"""
import asyncio
import json
import os
import re
import time

import aiohttp

from . import providers
from . import tls

DEFAULT_ENDPOINTS = [
    dict(id="default", name="DeepSeek", provider="deepseek", protocol="openai", base_url="https://api.deepseek.com",
         key_env="DEEPSEEK_API_KEY", use_proxy=False),
]
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    pass


class ContextOverflow(LLMError):
    """请求因为超出模型能读的长度被拒。limit：报错里写明的上限（没写明就按这次请求估一个）。"""

    def __init__(self, msg: str, limit: int):
        super().__init__(msg)
        self.limit = limit


# 各家报「超长」的写法：先认是不是超长，再从里面抠上限
OVERFLOW_HINT = re.compile(r"context[ _]length|context[ _]window|maximum context|too long|exceeds? the (?:model'?s )?maximum|input length|prompt is too long|"
                           r"reduce the length|token limit|max(?:imum)? (?:input )?tokens?|上下文|超出.{0,8}长度|长度超", re.I)
OVERFLOW_LIMIT = [re.compile(p, re.I) for p in (
    r"maximum context length is (\d[\d,]*)", r"context length of (\d[\d,]*)", r"context window (?:of|is) (\d[\d,]*)",
    r">\s*(\d[\d,]*)\s*maximum", r"maximum number of tokens allowed \((\d[\d,]*)\)", r"range of input length should be \[\s*\d+\s*,\s*(\d[\d,]*)\s*\]",
    r"limit (?:is|of) (\d[\d,]*) tokens", r"(\d[\d,]*) tokens? (?:is the )?(?:maximum|max|limit)", r"最大(?:上下文)?长度(?:为|是)?\s*(\d[\d,]*)")]


def overflow_info(msg: str) -> tuple[bool, int | None]:
    """（是不是「请求超长被拒」, 报错里写明的上限）。只看 HTTP 400 / 413 这类。"""
    if not re.match(r"HTTP (400|413|422)", msg or "") or not OVERFLOW_HINT.search(msg):
        return False, None
    for rx in OVERFLOW_LIMIT:
        m = rx.search(msg)
        if m and int(m.group(1).replace(",", "")) >= 1000:
            return True, int(m.group(1).replace(",", ""))
    return True, None


class UsageCapReached(LLMError):
    """打开了「用量硬上限」且当天用量已到：所有调 API 的用途都暂停，第二天自动恢复。"""


class ToolsUnsupported(LLMError):
    """这个服务商或模型不接受工具调用的参数。"""


def _data_url(url: str) -> tuple[str, str] | None:
    if url.startswith("data:") and ";base64," in url:
        head, data = url[5:].split(";base64,", 1)
        return head or "image/jpeg", data
    return None


def to_openai(messages: list) -> list:
    """中立的工具消息 → OpenAI 写法。思考模式下（DeepSeek、智谱）工具循环必须把上一轮的 reasoning_content 原样带回去。"""
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            raw = m.get("raw") if isinstance(m.get("raw"), dict) else {}
            d = {k: raw[k] for k in ("reasoning_content",) if raw.get(k)}
            d.update(role="assistant", content=m.get("content") or None,
                     tool_calls=[{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in m["tool_calls"]])
            out.append(d)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
        else:
            out.append({k: v for k, v in m.items() if k not in ("raw", "tool_calls")} if "raw" in m else m)
    return out


def to_anthropic(messages: list) -> tuple[str, list]:
    system, out = [], []
    for m in messages:
        c = m["content"]
        if m["role"] == "system":
            system.append(c if isinstance(c, str) else "\n".join(p.get("text", "") for p in c))
            continue
        if m["role"] == "assistant" and m.get("tool_calls"):  # 带回原样的内容块（含思考块的签名）
            blocks = m["raw"] if isinstance(m.get("raw"), list) else (
                ([{"type": "text", "text": c}] if c else []) +
                [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]} for t in m["tool_calls"]])
            out.append({"role": "assistant", "content": blocks})
            continue
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["id"], "content": c}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if isinstance(c, list):
            parts = []
            for p in c:
                if p.get("type") == "image_url":
                    url = p["image_url"]["url"]
                    d = _data_url(url)
                    parts.append({"type": "image", "source": {"type": "base64", "media_type": d[0], "data": d[1]} if d
                                  else {"type": "url", "url": url}})
                else:
                    parts.append({"type": "text", "text": p.get("text", "")})
            c = parts
        out.append({"role": m["role"], "content": c})
    return "\n\n".join(system), out


def to_gemini(messages: list) -> tuple[str, list]:
    system, out = [], []
    for m in messages:
        c = m["content"]
        if m["role"] == "system":
            system.append(c if isinstance(c, str) else "\n".join(p.get("text", "") for p in c))
            continue
        if m["role"] == "assistant" and m.get("tool_calls"):
            parts = m["raw"] if isinstance(m.get("raw"), list) else (
                ([{"text": c}] if c else []) + [{"functionCall": {"name": t["name"], "args": t["args"]}} for t in m["tool_calls"]])
            out.append({"role": "model", "parts": parts})
            continue
        if m["role"] == "tool":
            part = {"functionResponse": {"name": m["name"], "response": {"result": c}}}
            if out and out[-1]["role"] == "user" and out[-1]["parts"] and "functionResponse" in out[-1]["parts"][0]:
                out[-1]["parts"].append(part)
            else:
                out.append({"role": "user", "parts": [part]})
            continue
        parts = []
        for p in c if isinstance(c, list) else [{"type": "text", "text": c}]:
            if p.get("type") == "image_url":
                d = _data_url(p["image_url"]["url"])
                if d:
                    parts.append({"inline_data": {"mime_type": d[0], "data": d[1]}})
            else:
                parts.append({"text": p.get("text", "")})
        out.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})
    return "\n\n".join(system), out


class LLM:
    def __init__(self, db, settings, log=None):
        self.db, self.settings = db, settings
        self.log = log or (lambda *a, **k: None)
        self.session: aiohttp.ClientSession | None = None
        self.calls = 0

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    # ---------- 端点与密钥 ----------
    def endpoint_doc(self, eid: str) -> dict:
        ep = self.db.doc("endpoint", eid) or self.db.doc("endpoint", "default")
        if not ep:
            raise LLMError("还没有设置服务商")
        return ep

    def api_key(self, ep: dict) -> tuple[str, str]:
        """（密钥, 来源）。来源：'本机' / '环境变量 NAME' / ''。"""
        k = self.db.secret(f"endpoint:{ep['id']}")
        if not k and ep.get("key_from"):
            k = self.db.secret(f"endpoint:{ep['key_from']}")
        if k:
            return k, "本机"
        env = (ep.get("key_env") or "").strip()
        if env and os.environ.get(env):
            return os.environ[env], f"环境变量 {env}"
        return "", ""

    def key_state(self, ep: dict) -> dict:
        key, src = self.api_key(ep)
        env = (ep.get("key_env") or "").strip()
        return {"has_key": bool(key), "source": src, "env": env, "env_set": bool(env and os.environ.get(env)),
                "unreadable": self.db.secret_state(f"endpoint:{ep['id']}") == "unreadable",
                "keyless": bool((providers.provider(ep.get("provider", "")) or {}).get("local"))}

    def endpoint(self, eid: str) -> tuple[dict, str]:
        ep = self.endpoint_doc(eid)
        key, _ = self.api_key(ep)
        if not key and not self.key_state(ep)["keyless"]:
            raise LLMError("还没有填 API Key" + (f"（也没读到环境变量 {ep['key_env']}）" if ep.get("key_env") else ""))
        return ep, key

    # ---------- 模型能读多长 ----------
    DEFAULT_CONTEXT = 1_000_000  # 什么都没查到时按 1M

    def context_limit(self, role=None, platform=None, chat=None, purpose: str = "main") -> int:
        """这个用途的模型一次能读多少 token：用户在接口上填的优先；没填就取目录里报的、报错里写明的、被拒后记下的里最小的；都没有按 1M；成功发出过的最大请求作为底。"""
        eid = self.settings.get(f"{purpose}_endpoint", role, platform, chat)
        model = self.settings.get(f"{purpose}_model", role, platform, chat)
        ep = self.db.doc("endpoint", eid) or self.db.doc("endpoint", "default") or {}
        try:
            user = int(ep.get("context_tokens") or 0)
        except (TypeError, ValueError):
            user = 0
        if user > 0:
            return user
        rec = (self.db.kv("ctx_learn") or {}).get(f"{ep.get('id', eid)}|{model}") or {}
        cands = [c for c in ((providers.model_info(ep.get("provider", ""), model) or {}).get("ctx"), rec.get("rejected")) if c]
        return max(min(cands) if cands else self.DEFAULT_CONTEXT, int(rec.get("ok") or 0))

    def learn_context(self, ep: dict, model: str, rejected: int | None = None, ok: int | None = None):
        d = self.db.kv("ctx_learn") or {}
        key = f"{ep.get('id')}|{model}"
        rec = d.setdefault(key, {})
        if rejected and rejected < (rec.get("rejected") or 1 << 60):
            rec["rejected"] = int(rejected)
        elif ok and ok > (rec.get("ok") or 0):
            rec["ok"] = int(ok)
        else:
            return
        self.db.set_kv("ctx_learn", d)

    def proxy_for(self, ep: dict) -> str | None:
        return (self.settings.g("proxy") or None) if ep.get("use_proxy") else None

    # ---------- 请求 ----------
    def build(self, ep: dict, key: str, model: str, messages: list, temperature, effort: str, max_tokens: int, tools: list | None = None):
        """返回 (url, headers, body)。tools：[{name, description, parameters}]（JSON Schema）。"""
        base = ep["base_url"].rstrip("/")
        proto = ep.get("protocol", "openai")
        dia = providers.dialect(ep, model)
        effort = providers.fit_effort(ep, model, effort)  # 已设的档位模型不支持就换成最接近的（中 → 高），不把不认识的值发出去
        extra = {}
        if (ep.get("extra") or "").strip():
            try:
                extra = json.loads(ep["extra"])
            except ValueError:
                raise LLMError("端点的「额外请求参数」不是合法 JSON")
        if proto == "anthropic":
            system, msgs = to_anthropic(messages)
            body = {"model": model, "max_tokens": max_tokens or 8192, "messages": msgs}
            if system:
                body["system"] = system
            if effort:
                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {"effort": effort}
            elif temperature is not None:
                body["temperature"] = min(float(temperature), 1.0)
            if tools:
                body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
            headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        elif proto == "gemini":
            system, contents = to_gemini(messages)
            gc = {}
            if temperature is not None:
                gc["temperature"] = float(temperature)
            if max_tokens:
                gc["maxOutputTokens"] = max_tokens
            if effort:
                if "gemini-3" in model or "gemini-4" in model:
                    gc["thinkingConfig"] = {"thinkingLevel": {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}.get(effort, "MEDIUM")}
                else:
                    gc["thinkingConfig"] = {"thinkingBudget": {"minimal": 512, "low": 1024, "medium": 8192, "high": 24576, "xhigh": 24576, "max": 24576}.get(effort, 8192)}
            body = {"contents": contents, "generationConfig": gc}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if tools:
                body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in tools]}]
            url = f"{base}/models/{model}:generateContent"
            headers = {"x-goog-api-key": key, "content-type": "application/json"}
        else:
            body = {"model": model, "messages": to_openai(messages)}
            if tools:
                body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
            if temperature is not None:
                body["temperature"] = temperature
            if max_tokens:
                body["max_tokens"] = max_tokens
            if dia == "glm":
                if effort:
                    body["thinking"] = {"type": "enabled", "clear_thinking": True}
                    body["reasoning_effort"] = effort
            elif dia == "deepseek":
                if effort:
                    body["thinking"] = {"type": "enabled"}
                    body["reasoning_effort"] = effort
                else:
                    body["thinking"] = {"type": "disabled"}
            elif dia == "effort" and effort:
                body["reasoning_effort"] = effort
            url = base + "/chat/completions"
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {"Content-Type": "application/json"}
        body.update(extra)
        return url, headers, body

    @staticmethod
    def parse(proto: str, data: dict) -> tuple[str, dict]:
        """返回 (文本, 用量)。用量统一成：prompt（总输入）/ cached（命中缓存）/ cache_write / completion（含思考）/ reasoning。"""
        if proto == "anthropic":
            text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
            u = data.get("usage") or {}
            rd, cw = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
            return text.strip(), dict(prompt=(u.get("input_tokens") or 0) + rd + cw, cached=rd, cache_write=cw,
                                      completion=u.get("output_tokens") or 0, reasoning=0)
        if proto == "gemini":
            cands = data.get("candidates") or [{}]
            text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts") or [] if not p.get("thought"))
            u = data.get("usageMetadata") or {}
            th = u.get("thoughtsTokenCount") or 0
            return text.strip(), dict(prompt=u.get("promptTokenCount") or 0, cached=u.get("cachedContentTokenCount") or 0,
                                      cache_write=0, completion=(u.get("candidatesTokenCount") or 0) + th, reasoning=th)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0
        cw = (u.get("prompt_tokens_details") or {}).get("cache_write_tokens") or u.get("cache_creation_input_tokens") or 0
        rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        return (msg.get("content") or "").strip(), dict(prompt=u.get("prompt_tokens") or 0, cached=cached, cache_write=cw,
                                                       completion=u.get("completion_tokens") or 0, reasoning=rt)

    @staticmethod
    def parse_calls(proto: str, data: dict) -> tuple[list[dict], object]:
        """返回 ([{id, name, args}], raw)。raw 是这一轮模型回复里要原样带回去的部分（思考内容、签名等）。"""
        calls, raw = [], None
        if proto == "anthropic":
            raw = data.get("content") or []
            calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}} for b in raw if b.get("type") == "tool_use"]
        elif proto == "gemini":
            raw = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            calls = [{"id": f"call_{i}", "name": p["functionCall"]["name"], "args": p["functionCall"].get("args") or {}}
                     for i, p in enumerate(raw) if p.get("functionCall")]
        else:
            raw = (data.get("choices") or [{}])[0].get("message") or {}
            for i, t in enumerate(raw.get("tool_calls") or []):
                fn = t.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls.append({"id": t.get("id") or f"call_{i}", "name": fn.get("name", ""), "args": args if isinstance(args, dict) else {}})
        return calls, raw

    # ---------- 工具调用状态 ----------
    def tools_state(self, role=None, platform=None, chat=None, purpose: str = "main") -> dict:
        """purpose 这类调用（主模型 / 看图）的模型能不能用工具调用：yes（目录标了支持）/ no（试过被拒绝，或目录标了不支持）/ unknown（自定义模型，试了才知道）。"""
        eid = self.settings.get(f"{purpose}_endpoint", role, platform, chat)
        model = self.settings.get(f"{purpose}_model", role, platform, chat)
        ep = self.db.doc("endpoint", eid) or self.db.doc("endpoint", "default") or {}
        if (self.db.kv("no_tools") or {}).get(self._no_tools_key(purpose, ep, model)):
            return {"state": "no", "reason": f"{model} 这次试着用工具调用被服务商拒绝了（换个模型或服务商再试）", "model": model}
        info = providers.model_info(ep.get("provider", ""), model)
        if info is None:
            return {"state": "unknown", "reason": f"目录里没有 {model} 的资料，用到时试一试，被拒绝就自动停用", "model": model}
        if info.get("tools"):
            return {"state": "yes", "reason": "", "model": model}
        return {"state": "no", "reason": f"目录里标明 {model} 不支持工具调用", "model": model}

    def mark_no_tools(self, role=None, platform=None, chat=None, purpose: str = "main"):
        eid = self.settings.get(f"{purpose}_endpoint", role, platform, chat)
        model = self.settings.get(f"{purpose}_model", role, platform, chat)
        ep = self.db.doc("endpoint", eid) or self.db.doc("endpoint", "default") or {}
        d = self.db.kv("no_tools") or {}
        d[self._no_tools_key(purpose, ep, model)] = int(time.time())
        self.db.set_kv("no_tools", d)

    @staticmethod
    def _no_tools_key(purpose: str, ep: dict, model: str) -> str:
        """「这个模型不接受工具」按用途分开记：看图时带图又带工具被拒，不连累同一个模型做主模型时的工具（发表情包、联网搜索）。"""
        return f"{ep.get('id')}|{model}" if purpose == "main" else f"{purpose}:{ep.get('id')}|{model}"

    async def chat(self, purpose: str, messages: list, role=None, platform=None, chat=None,
                   tag: str | None = None, model: str | None = None) -> str:
        """purpose: main / gk / mem / vis。chat 是 '<平台>:<群号>'。tag 写进用量的用途（默认同 purpose）。"""
        return (await self._once(purpose, messages, role, platform, chat, tag, model, None))["text"]

    async def chat_tools(self, purpose: str, messages: list, tools: list, handler, role=None, platform=None, chat=None,
                         tag: str | None = None, rounds: int = 2, tool_tags: dict | None = None) -> str:
        """带工具调用的对话：模型想调工具就调 handler(name, args) -> str，把结果交回去，直到它给出最终回复。
        最多 rounds 轮工具调用，最后一轮不再提供工具，逼它给出回复。工具循环里第二次起的模型调用，按上一轮用的工具记用量用途（tool_tags：工具名 → 用途，默认同 tag）。
        服务商拒绝工具参数时，记下来并改成不带工具的普通对话。"""
        msgs = list(messages)
        use_tag = tag
        for rnd in range(rounds + 1):
            try:
                r = await self._once(purpose, msgs, role, platform, chat, use_tag, None, tools if rnd < rounds else None)
            except ToolsUnsupported as e:
                self.log("warn", "model", f"当前模型不接受工具调用，这次先不用工具：{e}")
                self.mark_no_tools(role, platform, chat, purpose)
                return await self.chat(purpose, messages, role, platform, chat, tag)
            if not r["calls"] or rnd >= rounds:
                return r["text"]
            use_tag = (tool_tags or {}).get(r["calls"][0]["name"], tag)
            msgs.append({"role": "assistant", "content": r["text"], "tool_calls": r["calls"], "raw": r["raw"]})
            for c in r["calls"]:
                try:
                    out = await handler(c["name"], c["args"])
                except Exception as e:  # 工具出错不拖垮这次回复：把错误告诉模型，让它自己圆过去
                    out = f"工具出错：{e}"
                msgs.append({"role": "tool", "id": c["id"], "name": c["name"], "content": str(out)})
        return ""

    async def _once(self, purpose, messages, role, platform, chat, tag, model, tools) -> dict:
        g = lambda k: self.settings.get(f"{purpose}_{k}", role, platform, chat)
        if self.hard_capped(role):
            raise UsageCapReached("今天的用量已到上限（用量硬上限），明天自动恢复")
        model = model or g("model")
        if not model:
            raise LLMError(f"没有设置{purpose}模型")
        eid = g("endpoint")
        if platform == "demo":  # 体验模式：用量记在「试聊」下；还没填服务商密钥就用预设的演示回复，不调模型
            tag = "trial"
            try:
                self.endpoint(eid)
            except LLMError:
                return {"text": self.canned(purpose, messages), "calls": [], "raw": None}
        args = (messages, g("temperature"), g("effort") or "", int(self.settings.g("max_tokens") or 0), tools)
        try:
            res = await self._call_full(eid, model, *args)
        except (ToolsUnsupported, ContextOverflow):
            raise
        except Exception as e:
            backup = self.settings.g("backup_endpoint")
            if not self.settings.g("backup_enabled") or not backup or eid == backup:
                raise
            self.log("warn", "model", f"主端点出错，切到备用端点：{e}")
            res = await self._call_full(backup, self.settings.g("backup_model") or model, *args)
        usage = res["usage"]
        if usage.get("prompt"):
            self.learn_context(self.db.doc("endpoint", eid) or self.db.doc("endpoint", "default") or {"id": eid}, model, ok=usage["prompt"])
        self.db.add_usage(role, platform, chat, tag or purpose, model, usage["prompt"], usage["cached"], usage["completion"],
                          usage["reasoning"], usage["cache_write"])
        return res

    @staticmethod
    def canned(purpose: str, messages: list) -> str:
        """体验模式没填密钥时的演示输出（不调模型、不花钱）。"""
        last = messages[-1]["content"]
        last = last if isinstance(last, str) else ""
        if purpose == "gk":
            return json.dumps({"reply": len(last) % 3 == 0, "brief": "（演示）这句有得接"}, ensure_ascii=False)
        if purpose == "mem":
            return json.dumps({"recap": "", "people": [], "memes": []})
        return "（演示回复：还没填服务商密钥，这是预设的一句话。填好密钥后，我会用真的模型回你。）"

    async def _call(self, eid: str, model: str, messages, temperature, effort, max_tokens):
        r = await self._call_full(eid, model, messages, temperature, effort, max_tokens, None)
        return r["text"], r["usage"]

    async def _call_full(self, eid: str, model: str, messages, temperature, effort, max_tokens, tools=None) -> dict:
        ep, key = self.endpoint(eid)
        url, headers, body = self.build(ep, key, model, messages, temperature, effort, max_tokens, tools)
        try:
            data = await self._post(url, headers, body, self.proxy_for(ep))
        except LLMError as e:
            over, lim = overflow_info(str(e))
            if over:  # 超长被拒：记下报错里的上限（没写明就按这次请求的大小往下估），让调用方按新上限重新摆一次再发
                est = int(len(json.dumps(messages, ensure_ascii=False)) * 0.85)
                self.learn_context(ep, model, rejected=lim or est)
                raise ContextOverflow(str(e)[:300], lim or est)
            if tools and re.search(r"HTTP (400|404|422)", str(e)) and re.search(r"tool|function", str(e), re.I):
                raise ToolsUnsupported(str(e)[:160])
            raise
        proto = ep.get("protocol", "openai")
        text, usage = self.parse(proto, data)
        calls, raw = self.parse_calls(proto, data) if tools else ([], None)
        return {"text": text, "usage": usage, "calls": calls, "raw": raw}

    RETRY_DELAY = 1.5  # 临时性错误（429、5xx、超时）自动重试一次前等几秒

    async def _post(self, url: str, headers: dict, body: dict, proxy: str | None = None) -> dict:
        try:
            return await self._post_once(url, headers, body, proxy)
        except (LLMError, asyncio.TimeoutError) as e:
            if not (isinstance(e, asyncio.TimeoutError) or re.match(r"HTTP (429|5\d\d)\b", str(e))):
                raise
            self.log("warn", "model", f"模型接口临时出错，{self.RETRY_DELAY:g} 秒后重试一次：{str(e)[:120] or '超时'}")
            await asyncio.sleep(self.RETRY_DELAY)
            return await self._post_once(url, headers, body, proxy)

    async def _post_once(self, url: str, headers: dict, body: dict, proxy: str | None = None) -> dict:
        if self.session is None:
            self.session = tls.session(timeout=aiohttp.ClientTimeout(total=180))
        self.calls += 1
        t = time.time()
        async with self.session.post(url, json=body, headers=headers, proxy=proxy) as r:
            text = await r.text()
            if r.status != 200:
                raise LLMError(f"HTTP {r.status}: {text[:300]}")
        data = json.loads(text)
        if "error" in data and not (data.get("choices") or data.get("content") or data.get("candidates")):
            raise LLMError(str(data["error"])[:300])
        data["_secs"] = round(time.time() - t, 2)
        return data

    # ---------- 模型列表与连通测试 ----------
    async def list_models(self, eid: str) -> list[str]:
        ep, key = self.endpoint(eid)
        base, proto = ep["base_url"].rstrip("/"), ep.get("protocol", "openai")
        if proto == "anthropic":
            url = base + ("/models" if base.endswith("/v1") else "/v1/models") + "?limit=1000"
            headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        elif proto == "gemini":
            url, headers = base + "/models?pageSize=1000", {"x-goog-api-key": key}
        else:
            url, headers = base + "/models", ({"Authorization": f"Bearer {key}"} if key else {})
        if self.session is None:
            self.session = tls.session(timeout=aiohttp.ClientTimeout(total=180))
        async with self.session.get(url, headers=headers, proxy=self.proxy_for(ep), timeout=aiohttp.ClientTimeout(total=30)) as r:
            text = await r.text()
            if r.status != 200:
                raise LLMError(f"HTTP {r.status}: {text[:200]}")
        data = json.loads(text)
        if proto == "gemini":
            return sorted(m["name"].split("/", 1)[-1] for m in data.get("models", [])
                          if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"]))
        return sorted(m["id"] for m in data.get("data", []))

    # ---------- 用量上限 ----------
    def tokens_today(self, role: str | None = None) -> int:
        day = time.strftime("%Y-%m-%d")
        sql = "SELECT COALESCE(SUM(prompt+completion),0) FROM usage WHERE day=?"
        args = [day]
        if role:
            sql += " AND role_id=?"
            args.append(role)
        return self.db.x(sql, args).fetchone()[0]

    def hard_capped(self, role: str | None = None) -> bool:
        """打开了「用量硬上限」并且当天用量已到上限。"""
        return bool(self.settings.get("usage_hard_cap", role) and self.over_limit(role))

    def hard_cap_state(self) -> dict:
        g = self.settings.g("daily_token_limit")
        used = self.tokens_today()
        return {"on": bool(self.settings.g("usage_hard_cap")), "limit": g * 10000, "used": used,
                "active": bool(self.settings.g("usage_hard_cap") and g and used >= g * 10000)}

    def over_limit(self, role: str | None) -> bool:
        g = self.settings.g("daily_token_limit")
        if g and self.tokens_today() >= g * 10000:
            return True
        r = self.settings.data.get(f"role:{role}", {}).get("daily_token_limit") if role else 0
        return bool(r and self.tokens_today(role) >= r * 10000)
