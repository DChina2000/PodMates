"""联网搜索：主模型通过工具调用发起，程序去查，结果只交给这一次回复，不写进记忆。

搜索服务可选。默认用 DeepSeek 官方的搜索接口（POST /v1/tools/search，和聊天共用同一个密钥，按成功请求计费）；
也可以换成 Kimi、智谱、Z.ai / 智谱 Coding 套餐自带的搜索（MCP）、Tavily、Brave 或自己搭的 SearXNG（各自的密钥存在本机，DPAPI 加密）。
没单独填密钥的，沿用同一家的模型服务商卡片里的密钥（DeepSeek、Kimi、智谱、两家 Coding 套餐）。
"""
import json
import time

import aiohttp
from . import tls

# id: (显示名, 需要单独的密钥, 要走代理, 说明)
PROVIDERS = {
    "deepseek": ("DeepSeek 搜索", True, False, "和 DeepSeek 聊天共用同一个密钥；按成功请求计费（见 DeepSeek 官方价格页）"),
    "kimi": ("Kimi 搜索", True, False, "Moonshot 的 /v1/tools/search，需要 Kimi 开放平台的密钥"),
    "zhipu": ("智谱搜索", True, False, "智谱 BigModel 的 web_search 接口，需要智谱的密钥"),
    "zai-mcp": ("Z.ai Coding 套餐搜索", True, False, "Coding 套餐自带的联网搜索（MCP），用套餐的同一个密钥，次数算在套餐额度里"),
    "zhipu-mcp": ("智谱 Coding 套餐搜索", True, False, "Coding 套餐自带的联网搜索（MCP），用套餐的同一个密钥，次数算在套餐额度里"),
    "tavily": ("Tavily", True, True, "海外服务，每月有免费额度；国内通常要走代理"),
    "brave": ("Brave Search", True, True, "海外服务，要绑信用卡；国内通常要走代理"),
    "searxng": ("自建 SearXNG", False, False, "自己搭的 SearXNG（要在它的设置里打开 json 输出），填它的地址"),
}
URLS = {"deepseek": "https://api.deepseek.com/v1/tools/search", "kimi": "https://api.moonshot.cn/v1/tools/search",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4/web_search", "tavily": "https://api.tavily.com/search",
        "zai-mcp": "https://api.z.ai/api/mcp/web_search_prime/mcp", "zhipu-mcp": "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp",
        "brave": "https://api.search.brave.com/res/v1/web/search"}  # 测试里换成本机的模拟服务
AUTO_ORDER = ["deepseek", "kimi", "zhipu", "zai-mcp", "zhipu-mcp", "tavily", "brave", "searxng"]
# 没单独填密钥时沿用的模型服务商：搜索服务 id → 服务商 id（deepseek 还认地址里带 deepseek.com 的卡片）
KEY_FROM = {"deepseek": ("deepseek",), "kimi": ("moonshotai", "moonshotai-cn"), "zhipu": ("zhipuai",),
            "zai-mcp": ("zai-coding-plan",), "zhipu-mcp": ("zhipuai-coding-plan",)}
MCP_VERSION = "2025-03-26"
TOOL_NAMES = ("websearchprime", "web_search_prime")  # 搜索工具优先用这两个名字（不分大小写，写法里的下划线不算）
MAX_CHARS = 2400  # 交给模型的搜索结果总字数上限
TIMEOUT = 20


class SearchError(Exception):
    pass


def tool_spec() -> dict:
    return {"name": "web_search",
            "description": "上网搜索。只在需要最新的信息（新闻、赛事、价格、版本）或要查证的事实时才用；闲聊、常识、你自己知道的东西不要搜。搜到的内容用自己的话说，别贴一串链接。",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词，简短，最好不超过 30 个字"}}, "required": ["query"]}}


class SessionLost(Exception):
    """MCP 会话失效（服务端回了 4xx）：重新初始化再试一次。"""


class Search:
    def __init__(self, db, settings, llm, log):
        self.db, self.settings, self.llm, self.log = db, settings, llm, log
        self.mcp: dict[str, dict] = {}  # 搜索服务 id → {sid: 会话, ver: 协议版本, init: 已初始化, tool: 搜索工具名, arg: 查询词参数名}

    # ---------- 配置与状态 ----------
    def conf(self) -> dict:
        return self.db.kv("search_conf") or {}

    def set_conf(self, provider: str | None = None, searxng_url: str | None = None):
        c = self.conf()
        if provider is not None:
            c["provider"] = provider
        if searxng_url is not None:
            c["searxng_url"] = searxng_url.strip()
        self.db.set_kv("search_conf", c)

    def key_info(self, pid: str) -> tuple[str, str]:
        """（密钥, 来源）。来源：'own'（这家搜索服务自己填的）/ 沿用的那张服务商卡片的名字 / ''（没有）。"""
        k = self.db.secret(f"search:{pid}")
        if k:
            return k, "own"
        for ep in self.db.docs("endpoint"):  # 没填：沿用同一家模型服务商卡片里的密钥
            if ep.get("provider") in KEY_FROM.get(pid, ()) or (pid == "deepseek" and "deepseek.com" in (ep.get("base_url") or "")):
                key, _ = self.llm.api_key(ep)
                if key:
                    return key, ep.get("name") or ep.get("id") or ""
        return "", ""

    def key_for(self, pid: str) -> str:
        return self.key_info(pid)[0]

    def ready(self, pid: str) -> bool:
        if pid == "searxng":
            return bool(self.conf().get("searxng_url"))
        return bool(self.key_for(pid))

    def chosen(self) -> str | None:
        want = self.conf().get("provider") or "auto"
        if want != "auto":
            return want if want in PROVIDERS else None
        return next((p for p in AUTO_ORDER if self.ready(p)), None)

    def status(self) -> dict:
        pid = self.chosen()
        return {"provider": pid, "name": PROVIDERS[pid][0] if pid else "", "ready": bool(pid and self.ready(pid)),
                "choice": self.conf().get("provider") or "auto", "searxng_url": self.conf().get("searxng_url", ""),
                "providers": [{"id": p, "name": v[0], "key": v[1], "help": v[3], "has_key": self.ready(p), "key_from": self.key_info(p)[1]} for p, v in PROVIDERS.items()],
                "today": self.today(), "limit": int(self.settings.g("search_daily") or 0)}

    def today(self) -> int:
        return self.db.x("SELECT COALESCE(SUM(calls),0) FROM usage WHERE day=? AND purpose='search' AND prompt=0 AND completion=0",
                         (time.strftime("%Y-%m-%d"),)).fetchone()[0]

    def available(self, counted: bool = True) -> tuple[bool, str]:
        """能不能用：（是否可用, 不能用的原因）。counted=False（给表情包写描述时搜）不看「每天最多搜索几次」。"""
        pid = self.chosen()
        if not pid or not self.ready(pid):
            return False, "还没有可用的搜索服务（设置 → 模型与接口 里选一个并填密钥）"
        lim = int(self.settings.g("search_daily") or 0)
        if counted and lim and self.today() >= lim:
            return False, f"今天的搜索次数已到上限（{lim} 次）"
        return True, ""

    # ---------- 查 ----------
    async def run(self, query: str, role=None, platform=None, chat=None, tag: str = "search", counted: bool = True) -> str:
        """返回给模型看的文字（出错时也返回一句话，让模型自己圆过去）。"""
        return (await self.lookup(query, role, platform, chat, tag, counted))[1]

    async def lookup(self, query: str, role=None, platform=None, chat=None, tag: str = "search", counted: bool = True) -> tuple[bool, str]:
        """（搜成功了吗, 给模型看的文字）。tag：用量里记的用途（给表情包写描述时搜的记 sticker_search，不进「今天已搜」的次数）；
        counted=False：不受「每天最多搜索几次」限制。"""
        query = (query or "").strip()[:128]
        if not query:
            return False, "没有给出搜索关键词"
        ok, why = self.available(counted)
        if not ok:
            return False, "搜索现在不能用：" + why
        if self.llm.hard_capped(role):
            return False, "搜索现在不能用：今天的用量已到上限"
        pid = self.chosen()
        what = "表情包搜索" if tag == "sticker_search" else "搜索"
        try:
            results = await self._query(pid, query)
        except SearchError as e:
            self.log("warn", "search", f"{PROVIDERS[pid][0]} {what}「{query}」没成功：{e}")
            return False, f"搜索没成功：{e}"
        except Exception as e:
            self.log("warn", "search", f"{PROVIDERS[pid][0]} {what}「{query}」出错：{e}")
            return False, f"搜索出错了：{type(e).__name__}"
        self.db.add_usage(role, platform, chat, tag, pid, 0, 0, 0)
        self.log("info", "search", f"{what}「{query}」→ {len(results)} 条（{PROVIDERS[pid][0]}）")
        return True, self.format(results)

    async def vis_chat(self, messages: list, role, platform, chat, *, rounds: int, tag: str = "vis", search_tag: str = "search", counted: bool = True) -> str:
        """看图模型带 web_search 工具问一次：它自己决定搜不搜，一共最多搜 rounds 次。
        搜索服务不可用、看图模型不接受工具（或目录里标明不支持）、rounds 为 0：照常只看图，不算出错。"""
        if rounds < 1 or not self.available(counted)[0] or self.llm.tools_state(role, platform, chat, "vis")["state"] == "no":
            return await self.llm.chat("vis", messages, role, platform, chat, tag=tag)
        left = [rounds]

        async def handler(name, args):
            if name != "web_search":
                return "没有这个工具"
            if left[0] < 1:
                return "搜索次数用完了，请直接回答"
            left[0] -= 1
            return await self.run(str(args.get("query", "")), role, platform, chat, search_tag, counted)
        return await self.llm.chat_tools("vis", messages, [tool_spec()], handler, role, platform, chat, tag=tag, rounds=rounds,
                                         tool_tags={"web_search": tag})

    @staticmethod
    def format(results: list[dict]) -> str:
        if not results:
            return "没有搜到相关结果"
        out, total = [], 0
        for i, r in enumerate(results[:6], 1):
            body = (r.get("text") or "").strip().replace("\n", " ")[:360]
            line = f"{i}. {r.get('title', '').strip()[:80]}（{r.get('url', '')[:100]}）\n   {body}"
            if total + len(line) > MAX_CHARS:
                break
            out.append(line)
            total += len(line)
        return "搜索结果（只用于这次回复）：\n" + "\n".join(out)

    async def _query(self, pid: str, q: str) -> list[dict]:
        proxy = (self.settings.g("proxy") or None) if PROVIDERS[pid][2] else None
        key = self.key_for(pid)
        async with tls.session(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
            if pid == "deepseek":
                j = await self._post(s, URLS["deepseek"], {"Authorization": f"Bearer {key}"},
                                     {"query": q, "count": 5}, proxy)
                return [{"title": r.get("title", ""), "url": r.get("url", ""), "text": r.get("summary") or r.get("snippet") or ""}
                        for r in j.get("results") or []]
            if pid == "kimi":
                j = await self._post(s, URLS["kimi"], {"Authorization": f"Bearer {key}"},
                                     {"text_query": q, "limit": 5}, proxy)
                return [{"title": r.get("title", ""), "url": r.get("url", ""), "text": r.get("snippet", "")} for r in j.get("search_results") or []]
            if pid == "zhipu":
                j = await self._post(s, URLS["zhipu"], {"Authorization": f"Bearer {key}"},
                                     {"search_query": q, "search_engine": "search_std", "search_intent": False, "count": 5}, proxy)
                return [{"title": r.get("title", ""), "url": r.get("link", ""), "text": r.get("content", "")} for r in j.get("search_result") or []]
            if pid in ("zai-mcp", "zhipu-mcp"):
                return await self._mcp(s, pid, key, q, proxy)
            if pid == "tavily":
                j = await self._post(s, URLS["tavily"], {"Authorization": f"Bearer {key}"}, {"query": q, "max_results": 5}, proxy)
                return [{"title": r.get("title", ""), "url": r.get("url", ""), "text": r.get("content", "")} for r in j.get("results") or []]
            if pid == "brave":
                async with s.get(URLS["brave"], params={"q": q, "count": 5}, proxy=proxy,
                                 headers={"X-Subscription-Token": key, "Accept": "application/json"}) as r:
                    j = await self._json(r)
                return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("description", "")} for x in (j.get("web") or {}).get("results") or []]
            base = self.conf().get("searxng_url", "").rstrip("/")
            async with s.get(base + "/search", params={"q": q, "format": "json"}) as r:
                j = await self._json(r)
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("content", "")} for x in j.get("results") or []]

    async def _post(self, s, url, headers, body, proxy) -> dict:
        async with s.post(url, json=body, headers=headers, proxy=proxy) as r:
            return await self._json(r)

    @staticmethod
    async def _json(r) -> dict:
        text = await r.text()
        if r.status == 402:
            raise SearchError("搜索服务的账户余额不足")
        if r.status in (401, 403):
            raise SearchError("搜索服务的密钥不对或没有权限")
        if r.status == 429:
            raise SearchError("搜索太频繁了，稍后再试")
        if r.status != 200:
            raise SearchError(f"HTTP {r.status}: {text[:120]}")
        try:
            return json.loads(text)
        except ValueError:
            raise SearchError("搜索服务返回的不是 JSON")

    # ---------- Coding 套餐的搜索 MCP（streamable HTTP） ----------
    async def _mcp(self, s, pid: str, key: str, q: str, proxy) -> list[dict]:
        """initialize → notifications/initialized → tools/list → tools/call。会话、搜索工具名、查询词参数名按服务缓存；会话失效（4xx）重新初始化一次再试。"""
        st = self.mcp.setdefault(pid, {})
        for attempt in (0, 1):
            try:
                if not st.get("init"):
                    st.pop("sid", None)
                    r = await self._rpc(s, pid, key, st, proxy, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": MCP_VERSION, "capabilities": {}, "clientInfo": {"name": "PodMates", "version": "1.4.3"}}})
                    st["ver"] = (r.get("result") or {}).get("protocolVersion") or MCP_VERSION
                    await self._rpc(s, pid, key, st, proxy, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                    st["init"] = True
                if not st.get("tool"):
                    r = await self._rpc(s, pid, key, st, proxy, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                    st["tool"], st["arg"] = self._pick_tool((r.get("result") or {}).get("tools") or [])
                r = await self._rpc(s, pid, key, st, proxy, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                             "params": {"name": st["tool"], "arguments": {st["arg"]: q}}})
                return self._mcp_results(r.get("result") or {})
            except SessionLost:
                st.pop("init", None)
                if attempt:
                    raise SearchError("搜索服务的会话失效了，重新连接也不行")
        return []

    @staticmethod
    def _pick_tool(tools: list) -> tuple[str, str]:
        """搜索工具：名字含 search 的，优先 webSearchPrime / web_search_prime。查询词填进 inputSchema 里必填的那个字符串参数，没有必填项就填第一个字符串参数。"""
        named = [t for t in tools if "search" in str(t.get("name", "")).lower()]
        if not named:
            raise SearchError("搜索服务没有提供搜索工具")
        tool = next((t for t in named if str(t["name"]).lower().replace("_", "") in TOOL_NAMES), named[0])
        schema = tool.get("inputSchema") or {}
        strs = [k for k, v in (schema.get("properties") or {}).items() if (v or {}).get("type") == "string"]
        arg = next((k for k in schema.get("required") or [] if k in strs), None) or (strs[0] if strs else "query")
        return tool["name"], arg

    async def _rpc(self, s, pid: str, key: str, st: dict, proxy, payload: dict) -> dict:
        """发一条 JSON-RPC。通知（没有 id）不读内容。响应可能是 JSON，也可能是 text/event-stream。"""
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if st.get("sid"):
            headers["Mcp-Session-Id"] = st["sid"]
        if payload.get("method") != "initialize" and st.get("ver"):
            headers["MCP-Protocol-Version"] = st["ver"]
        async with s.post(URLS[pid], json=payload, headers=headers, proxy=proxy) as r:
            text = await r.text()
            if r.status in (401, 402, 403, 429):
                await self._json(_Status(r.status, text))
            if 400 <= r.status < 500 and st.get("sid") and payload.get("method") != "initialize":
                raise SessionLost()
            if r.status >= 300:
                raise SearchError(f"HTTP {r.status}: {text[:120]}")
            if r.headers.get("Mcp-Session-Id"):
                st["sid"] = r.headers["Mcp-Session-Id"]
            if "id" not in payload:
                return {}
            msg = self._mcp_body(text, r.headers.get("Content-Type", ""), payload["id"])
        if msg.get("error"):
            raise SearchError("搜索服务返回错误：" + str((msg["error"] or {}).get("message") or msg["error"])[:120])
        return msg

    @staticmethod
    def _mcp_body(text: str, ctype: str, rid) -> dict:
        try:
            if "text/event-stream" in ctype:
                msgs = []
                for ev in text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(ln[5:].lstrip() for ln in ev.split("\n") if ln.startswith("data:"))
                    if data.strip():
                        msgs.append(json.loads(data))
                return next((m for m in msgs if m.get("id") == rid), None) or (msgs[-1] if msgs else {})
            return json.loads(text)
        except ValueError:
            raise SearchError("搜索服务返回的不是 JSON 或 event-stream")

    @staticmethod
    def _mcp_results(result: dict) -> list[dict]:
        """tools/call 的返回（文字或 JSON）整理成 [{title, url, text}]。"""
        texts = [str(c.get("text") or "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type", "text") == "text"]
        if result.get("isError"):
            raise SearchError("搜索服务说这次搜索没成功：" + " ".join(texts)[:120])
        data = result.get("structuredContent")
        if data is None:
            raw = "\n".join(texts).strip()
            try:
                data = json.loads(raw)
                if isinstance(data, str):  # 有的服务把 JSON 又包了一层字符串
                    data = json.loads(data)
            except ValueError:
                return [{"title": "", "url": "", "text": raw}] if raw else []
        items = data
        if isinstance(data, dict):
            items = next((v for k in ("results", "search_result", "data", "items") if isinstance((v := data.get(k)), list)), [data])
        out = []
        for x in items if isinstance(items, list) else []:
            if isinstance(x, dict):
                out.append({"title": str(x.get("title") or x.get("name") or ""), "url": str(x.get("url") or x.get("link") or x.get("href") or ""),
                            "text": str(x.get("content") or x.get("snippet") or x.get("summary") or x.get("text") or x.get("description") or "")})
        return out


class _Status:
    """把 aiohttp 响应的状态码和内容交给 Search._json 统一翻成人话。"""

    def __init__(self, status: int, text: str):
        self.status, self._text = status, text

    async def text(self) -> str:
        return self._text
