"""表情包描述 250 字、写描述时联网搜索、群里看图时联网搜索、Coding 套餐的搜索 MCP、搜索密钥沿用同一家的模型密钥。全部用本机模拟服务，不连真的 Z.ai / 智谱。"""
import json
from contextlib import contextmanager

from aiohttp import web

from test_round35 import logs
from test_round36 import fav_bot, rows, setg
from test_round5b import png

from mb import search
from mb.settings import BY_KEY
from mb.stickers import FAV_PROMPT, GROUP_PROMPT, QUERY_PROMPT, SEARCH_HINT, SEARCH_REF, VISION_PROMPT


@contextmanager
def urls():
    """测试里把搜索服务的地址换成本机的，出来时恢复。"""
    old = dict(search.URLS)
    try:
        yield
    finally:
        search.URLS.clear()
        search.URLS.update(old)


def mock_search(e):
    search.URLS["deepseek"] = f"http://127.0.0.1:{e.port}/v4/tools/search"
    setg(e, "sticker_desc_search", True)


def img_calls(e):
    """所有带图片的看图调用（按先后）。"""
    return [c for c in e.llm.calls if isinstance(c["messages"][0]["content"], list) and c["messages"][0]["content"][0].get("type") == "image_url"]


def prompt_of(call):
    return call["messages"][0]["content"][-1]["text"]


def sync_one(e, ch, seed=11):
    ch.imgs[f"http://qq/{seed}.png"] = png(seed)
    ch.favs.append(f"http://qq/{seed}.png")
    return e.engine.stickers.sync_favorites(e.engine.bots["ac_900"])


def usage(e):
    return {r["purpose"]: r["calls"] for r in e.core.db.rows("SELECT purpose, SUM(calls) AS calls FROM usage GROUP BY purpose")}


# ---------- 一、250 字和新设置 ----------
def test_defaults_and_scopes_of_the_new_settings():
    assert BY_KEY["sticker_desc_max"]["default"] == 250
    for key, default, typ in (("sticker_desc_search", True, "bool"), ("sticker_desc_search_max", 5, "int"), ("sticker_desc_search_auto", False, "bool")):
        s = BY_KEY[key]
        assert (s["default"], s["type"], s["cat"], s["adv"], s["scopes"]) == (default, typ, "工具", True, ["G"])
    assert BY_KEY["sticker_desc_search"]["label"] == "给表情包写描述时联网搜索"
    assert BY_KEY["sticker_desc_search_max"]["label"] == "每张表情包最多搜几次"
    assert BY_KEY["sticker_desc_search_auto"]["label"] == "搜不搜由看图模型自己判断"
    assert BY_KEY["sticker_desc_search_auto"]["help"] == "关着：每张都至少搜一次。开着：看图模型认不出出处或含义时才搜"
    v = BY_KEY["vis_search"]
    assert (v["default"], v["type"], v["cat"], v["scopes"]) == (False, "bool", "模型", BY_KEY["vision_enabled"]["scopes"])
    assert v["label"] == "群里看图时联网搜索"
    assert v["help"] == "看图模型说一句图里是什么时，需要的话可以上网查一下。会让这次回复慢几秒，算进「每天最多搜索几次」"


def test_the_description_length_defaults_to_250(env):
    async def body(e):
        assert e.engine.stickers.desc_max() == 250
        setg(e, "sticker_desc_search", False)
        ch = fav_bot(e)
        e.llm.vis = lambda content: {"desc": "字" * 400}
        await sync_one(e, ch)
        assert FAV_PROMPT.replace("{n}", "250") == prompt_of(img_calls(e)[0]) and "（250字内）" in prompt_of(img_calls(e)[0])
        assert len(rows(e)[0]["descr"]) == 250 and rows(e)[0]["desc_max"] == 250
    env.run(body)


# ---------- 二、给表情包写描述时联网搜索 ----------
def test_default_flow_asks_for_a_term_searches_once_then_writes_the_description_with_the_tool(env):
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            e.llm.vis = lambda content: "大佬 无语 表情" if content[-1]["text"] == QUERY_PROMPT else {"desc": "最终描述"}
            await sync_one(e, ch)
            calls = img_calls(e)
            assert len(calls) == 2
            assert prompt_of(calls[0]) == QUERY_PROMPT == "这是一张表情包。为了查清它的出处或含义，给出一个最合适的搜索词（图上有字就优先用图上的字）。只输出搜索词。"
            assert "tools" not in calls[0]
            assert [x["query"] for x in e.llm.search_calls] == ["大佬 无语 表情"]
            t = prompt_of(calls[1])
            assert t.startswith(FAV_PROMPT.replace("{n}", "250") + "\n\n下面是联网搜索的结果，只作参考；和画面对不上的不要用。\n搜索结果")
            assert "SEARCH-RESULT-SECRET" in t and SEARCH_REF == "\n\n下面是联网搜索的结果，只作参考；和画面对不上的不要用。\n"
            assert [x["function"]["name"] for x in calls[1]["tools"]] == ["web_search"]
            assert rows(e)[0]["descr"] == "最终描述"
    env.run(body)


def test_the_total_searches_never_exceed_the_per_sticker_maximum(env):
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            e.llm.vis = lambda content: "词" if content[-1]["text"] == QUERY_PROMPT else {"desc": "描述"}
            # 最多 1 次：词搜完就没了，写描述不带工具
            setg(e, "sticker_desc_search_max", 1)
            await sync_one(e, ch, 11)
            assert len(e.llm.search_calls) == 1 and "tools" not in img_calls(e)[1]
            # 最多 2 次：词搜一次，写描述时模型再搜一次，一共 2 次
            setg(e, "sticker_desc_search_max", 2)
            e.llm.search_calls.clear()
            e.llm.tool_plan = lambda b: ("web_search", {"query": "再搜"}) if any(t["function"]["name"] == "web_search" for t in b["tools"]) else None
            e.llm.main = lambda s, u: "工具之后的描述" if "SEARCH-RESULT-SECRET" in u else "没看到结果"
            await sync_one(e, ch, 12)
            assert [x["query"] for x in e.llm.search_calls] == ["词", "再搜"]
            assert [r["descr"] for r in rows(e)] == ["描述", "工具之后的描述"]
            # 工具循环里一口气搜很多次也只放行「最多搜几次」减去已搜的
            e.llm.search_calls.clear()
            seen = []

            async def fake(purpose, messages, tools, handler, *a, rounds=2, **k):
                seen.append([await handler("web_search", {"query": f"q{i}"}) for i in range(5)])
                return "好"
            e.core.llm.chat_tools = fake
            await e.engine.search.vis_chat([{"role": "user", "content": "x"}], "r_haitun", "qq", None, rounds=3, tag="sticker", search_tag="sticker_search", counted=False)
            assert len(e.llm.search_calls) == 3 and seen[0][3] == "搜索次数用完了，请直接回答" and "SEARCH-RESULT-SECRET" in seen[0][0]
    env.run(body)


def test_when_the_model_judges_for_itself_nothing_is_forced(env):
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            setg(e, "sticker_desc_search_auto", True)
            e.llm.vis = lambda content: {"desc": "认得出，不用搜"}
            await sync_one(e, ch, 11)
            calls = img_calls(e)
            assert len(calls) == 1 and not e.llm.search_calls  # 没有「先问搜索词」那一步，也没强制搜
            assert prompt_of(calls[0]) == FAV_PROMPT.replace("{n}", "250") + SEARCH_HINT
            assert SEARCH_HINT == "\n\n认不出这张图的出处或含义时，可以用 web_search 查一下；搜到的只作参考，和画面对不上的不要用。"
            assert [x["function"]["name"] for x in calls[0]["tools"]] == ["web_search"]
            assert rows(e)[0]["descr"] == "认得出，不用搜"
            # 认不出时它自己搜
            e.llm.tool_plan = lambda b: ("web_search", {"query": "这是什么梗"}) if any(t["function"]["name"] == "web_search" for t in b["tools"]) else None
            e.llm.main = lambda s, u: "搜完之后写的"
            await sync_one(e, ch, 12)
            assert [x["query"] for x in e.llm.search_calls] == ["这是什么梗"] and rows(e)[1]["descr"] == "搜完之后写的"
    env.run(body)


def test_no_search_service_a_failing_search_or_a_model_without_tools_just_looks_at_the_picture(env):
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            e.llm.vis = lambda content: "词" if content[-1]["text"] == QUERY_PROMPT else {"desc": "只看图"}
            # 搜索服务不可用：只调一次，不带工具
            e.engine.search.set_conf(provider="tavily")  # 没填密钥
            await sync_one(e, ch, 11)
            assert len(img_calls(e)) == 1 and "tools" not in img_calls(e)[0] and prompt_of(img_calls(e)[0]) == FAV_PROMPT.replace("{n}", "250")
            assert rows(e)[0]["descr"] == "只看图"
            # 搜索出错（地址 404）：问了词，搜不到，照常只看图，描述照样写出来
            e.engine.search.set_conf(provider="auto")
            search.URLS["deepseek"] = f"http://127.0.0.1:{e.port}/v4/没有这个地址"
            e.llm.calls.clear()
            await sync_one(e, ch, 12)
            calls = img_calls(e)
            assert [prompt_of(c) for c in calls] == [QUERY_PROMPT, FAV_PROMPT.replace("{n}", "250")] and "tools" not in calls[1]
            assert rows(e)[1]["descr"] == "只看图" and logs(e, "表情包搜索「词」没成功")
            # 模型不接受工具（自己判断开着）：照常只看图，记下这个看图模型不接受工具
            search.URLS["deepseek"] = f"http://127.0.0.1:{e.port}/v4/tools/search"
            setg(e, "sticker_desc_search_auto", True)
            e.llm.reject_tools = True
            e.llm.calls.clear()
            await sync_one(e, ch, 13)
            assert len(rows(e)) == 3 and rows(e)[2]["descr"] == "只看图"
            assert e.core.llm.tools_state(purpose="vis")["state"] == "no"
            e.llm.calls.clear()
            await sync_one(e, ch, 14)
            assert len(img_calls(e)) == 1 and "tools" not in img_calls(e)[0]  # 记住了：不再带工具
    env.run(body)


def test_sticker_searches_ignore_the_daily_limit_do_not_count_toward_it_and_are_recorded_under_their_own_purpose(env):
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            setg(e, "search_daily", 1)  # 主模型的联网搜索一天 1 次
            e.llm.vis = lambda content: "词" if content[-1]["text"] == QUERY_PROMPT else {"desc": "描述"}
            for seed in (11, 12, 13):
                await sync_one(e, ch, seed)
            assert len(e.llm.search_calls) == 3 and len(rows(e)) == 3  # 3 张都搜了，没被 1 次的上限拦住
            assert e.engine.search.today() == 0 and e.engine.search.available()[0]  # 不算进当天次数
            u = usage(e)
            assert u["sticker_search"] == 3 and "search" not in u
            assert logs(e, "表情包搜索「词」→")
            # 主模型自己的搜索照旧受上限、照旧记「搜索」
            await e.engine.search.run("普通搜索")
            assert e.engine.search.today() == 1 and not e.engine.search.available()[0] and usage(e)["search"] == 1
            # 日上限用完后表情包照样能搜
            e.llm.search_calls.clear()
            await sync_one(e, ch, 14)
            assert len(e.llm.search_calls) == 1
    env.run(body)


def test_learning_a_group_sticker_and_redescribing_search_too(env):
    async def body(e):
        with urls():
            bot = e.add_bot("900")
            mock_search(e)
            setg(e, "proactive", False)
            e.llm.vis = lambda content: "词" if content[-1]["text"] == QUERY_PROMPT else {"sticker": True, "ok": True, "desc": "群里的"}
            bot.imgs["http://img/0"] = png(1)
            for u in ("111", "112"):
                await e.say(u, "", images=["http://img/0"])
                await e.engine.idle()
            calls = img_calls(e)
            assert [prompt_of(c) for c in calls][0] == QUERY_PROMPT
            assert prompt_of(calls[1]).startswith(VISION_PROMPT.replace("{n}", "250") + SEARCH_REF) and len(e.llm.search_calls) == 1
            assert e.core.db.rows("SELECT descr FROM stickers WHERE status='learned'")[0]["descr"] == "群里的"
            # 重新描述：群里学来的用「群聊里常用」那句，同样先搜
            setg(e, "sticker_desc_max", 100)
            e.llm.vis = lambda content: "词" if content[-1]["text"] == QUERY_PROMPT else {"desc": "重描的"}
            e.llm.calls.clear()
            res = await e.engine.stickers.redescribe()
            assert res["done"] == 1
            calls = img_calls(e)
            assert prompt_of(calls[0]) == QUERY_PROMPT and prompt_of(calls[1]).startswith(GROUP_PROMPT.replace("{n}", "100") + SEARCH_REF)
            assert e.core.db.rows("SELECT descr FROM stickers WHERE status='learned'")[0]["descr"] == "重描的"
    env.run(body)


# ---------- 三、群里看图时联网搜索 ----------
def test_vision_search_is_off_by_default_and_on_it_gives_the_tool_and_counts(env):
    async def body(e):
        with urls():
            e.add_bot("900")
            b = e.engine.bots["ac_900"]
            room = e.engine.room("qq", "100", "群")
            search.URLS["deepseek"] = f"http://127.0.0.1:{e.port}/v4/tools/search"
            e.llm.vis = lambda content: "一只猫"  # 同一张图只看一次（有缓存），下面每次换一张新图，才能分别看到各次调用带什么
            assert (await e.engine.describe_bytes(b, room, png(101))).strip(chr(34)) == "一只猫"  # 桩把看图的回答包成了 JSON 字符串
            c = img_calls(e)[-1]
            assert "tools" not in c and prompt_of(c) == "用一两句中文说清这张图里是什么；有文字就把文字写出来。"
            # 开着：带 web_search 工具，提示末尾加那句话，由它自己决定搜不搜
            setg(e, "vis_search", True)
            await e.engine.describe_bytes(b, room, png(102))
            c = img_calls(e)[-1]
            assert [t["function"]["name"] for t in c["tools"]] == ["web_search"]
            assert prompt_of(c).endswith("需要时可以用 web_search 查图里的东西；不要拿图里的人名、账号、聊天内容去搜。") and not e.llm.search_calls
            e.llm.tool_plan = lambda body: ("web_search", {"query": "图里的梗"}) if any(t["function"]["name"] == "web_search" for t in body["tools"]) else None
            e.llm.main = lambda s, u: "搜过之后：一只猫" if "SEARCH-RESULT-SECRET" in u else "没看到结果"
            assert (await e.engine.describe_bytes(b, room, png(103))).strip(chr(34)) == "搜过之后：一只猫"
            assert e.engine.search.today() == 1 and usage(e)["search"] == 1  # 和普通搜索一样计数
            # 每天次数用完了：不给工具，也不加那句话
            setg(e, "search_daily", 1)
            n = len(e.llm.calls)
            await e.engine.describe_bytes(b, room, png(104))
            assert "tools" not in e.llm.calls[n] and "web_search" not in prompt_of(e.llm.calls[n])
            # 模型不接受工具：照常只看图
            setg(e, "search_daily", 100)
            e.llm.tool_plan = None
            e.llm.reject_tools = True
            e.llm.vis = lambda content: "只看图"
            assert (await e.engine.describe_bytes(b, room, png(105))).strip(chr(34)) == "只看图"
            # 私聊同样（room 是 None）
            e.llm.reject_tools = False
            e.core.db.set_kv("no_tools", {})
            await e.engine.describe_bytes(b, None, png(106))
            assert "tools" in img_calls(e)[-1]
    env.run(body)


# ---------- 四、Coding 套餐的搜索 MCP ----------
class McpServer:
    """本机模拟的 MCP 服务（streamable HTTP）。mode: json / sse。"""

    def __init__(self, mode="json", tools=None, text=None):
        self.mode, self.log, self.headers = mode, [], []
        self.sid, self.expire_after = "sess-1", None
        self.sessions = 0
        self.calls = 0
        self.tools = tools if tools is not None else [
            {"name": "get_time", "description": "时间", "inputSchema": {"type": "object", "properties": {"tz": {"type": "string"}}, "required": ["tz"]}},
            {"name": "webSearchPrime", "description": "搜索", "inputSchema": {"type": "object", "properties": {
                "count": {"type": "integer"}, "search_query": {"type": "string"}}, "required": ["search_query"]}}]
        self.text = text if text is not None else json.dumps([{"title": "标题甲", "link": "http://example.test/a", "content": "内容甲 MCP-RESULT"},
                                                              {"title": "标题乙", "link": "http://example.test/b", "content": "内容乙"}], ensure_ascii=False)
        self.arguments = None
        self.status_when_expired = 404

    def reply(self, req, msg, sid=None):
        h = {"Mcp-Session-Id": sid} if sid else {}
        if self.mode == "sse":
            return web.Response(text=f"event: message\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n", content_type="text/event-stream", headers=h)
        return web.json_response(msg, headers=h)

    async def handle(self, req):
        body = await req.json()
        method = body.get("method")
        self.log.append(method)
        self.headers.append(dict(req.headers))
        if method == "initialize":
            self.sessions += 1
            self.sid = f"sess-{self.sessions}"
            return self.reply(req, {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "mock"}}}, self.sid)
        if req.headers.get("Mcp-Session-Id") != self.sid or (self.expire_after is not None and self.calls >= self.expire_after and method != "tools/call_never"):
            if self.expire_after is not None and self.calls >= self.expire_after:
                self.expire_after = None  # 只失效一次：重新初始化之后就好了
                self.sid = None
            return web.Response(status=self.status_when_expired, text="session not found")
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            return self.reply(req, {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self.tools}})
        if method == "tools/call":
            self.calls += 1
            self.arguments = body["params"]
            return self.reply(req, {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": self.text}]}})
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "没有这个方法"}})


async def serve(mcp):
    app = web.Application()
    app.router.add_post("/mcp", mcp.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_the_two_coding_plan_services_are_listed_with_their_addresses_after_zhipu_in_the_auto_order():
    from mb.search import AUTO_ORDER, PROVIDERS, URLS
    assert URLS["zai-mcp"] == "https://api.z.ai/api/mcp/web_search_prime/mcp"
    assert URLS["zhipu-mcp"] == "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
    assert PROVIDERS["zai-mcp"][0] == "Z.ai Coding 套餐搜索" and PROVIDERS["zhipu-mcp"][0] == "智谱 Coding 套餐搜索"
    for pid in ("zai-mcp", "zhipu-mcp"):
        assert PROVIDERS[pid][3] == "Coding 套餐自带的联网搜索（MCP），用套餐的同一个密钥，次数算在套餐额度里" and PROVIDERS[pid][1] is True
    assert AUTO_ORDER.index("zhipu") + 1 == AUTO_ORDER.index("zai-mcp") and AUTO_ORDER.index("zai-mcp") + 1 == AUTO_ORDER.index("zhipu-mcp")


def test_mcp_handshake_finds_the_tool_and_argument_in_tools_list_reads_json_and_caches_the_session(env):
    async def body(e):
        with urls():
            mcp = McpServer("json")
            runner, port = await serve(mcp)
            try:
                search.URLS["zai-mcp"] = f"http://127.0.0.1:{port}/mcp"
                e.core.db.set_secret("search:zai-mcp", "套餐key")
                e.engine.search.set_conf(provider="zai-mcp")
                out = await e.engine.search.run("汤姆 比赛")
                assert mcp.log == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
                assert mcp.headers[0]["Authorization"] == "Bearer 套餐key"
                assert mcp.headers[0].get("Mcp-Session-Id") is None and all(h.get("Mcp-Session-Id") == "sess-1" for h in mcp.headers[1:])  # 后面几步都带服务端回的会话号
                assert mcp.arguments == {"name": "webSearchPrime", "arguments": {"search_query": "汤姆 比赛"}}  # 工具和必填的字符串参数从 tools/list 里挑
                assert out.startswith("搜索结果（只用于这次回复）：") and "1. 标题甲（http://example.test/a）" in out and "内容甲 MCP-RESULT" in out
                # 第二次：会话、工具名、参数名都用缓存，只发 tools/call
                mcp.log.clear()
                await e.engine.search.run("第二次")
                assert mcp.log == ["tools/call"] and mcp.arguments["arguments"] == {"search_query": "第二次"}
                assert usage(e)["search"] == 2
            finally:
                await runner.cleanup()
    env.run(body)


def test_mcp_reads_event_stream_responses_too(env):
    async def body(e):
        with urls():
            mcp = McpServer("sse")
            runner, port = await serve(mcp)
            try:
                search.URLS["zhipu-mcp"] = f"http://127.0.0.1:{port}/mcp"
                e.core.db.set_secret("search:zhipu-mcp", "k")
                e.engine.search.set_conf(provider="zhipu-mcp")
                out = await e.engine.search.run("查一下")
                assert mcp.log == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
                assert "标题乙" in out and "内容甲 MCP-RESULT" in out
            finally:
                await runner.cleanup()
    env.run(body)


def test_mcp_tool_and_argument_are_not_hard_coded(env):
    async def body(e):
        with urls():
            tools = [{"name": "ping", "inputSchema": {"type": "object", "properties": {}}},
                     {"name": "my_search_tool", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}, "q": {"type": "string"}, "lang": {"type": "string"}}}}]
            mcp = McpServer("json", tools=tools, text="只是一段纯文字的结果")
            runner, port = await serve(mcp)
            try:
                search.URLS["zai-mcp"] = f"http://127.0.0.1:{port}/mcp"
                e.core.db.set_secret("search:zai-mcp", "k")
                e.engine.search.set_conf(provider="zai-mcp")
                out = await e.engine.search.run("词")
                assert mcp.arguments == {"name": "my_search_tool", "arguments": {"q": "词"}}  # 没有必填项：填第一个字符串参数
                assert "只是一段纯文字的结果" in out
                # web_search_prime 写法也认；多个候选时优先它
                mcp2 = McpServer("json", tools=[{"name": "other_search", "inputSchema": {"properties": {"x": {"type": "string"}}}},
                                                {"name": "web_search_prime", "inputSchema": {"properties": {"search_query": {"type": "string"}}, "required": ["search_query"]}}])
                from mb.search import Search
                assert Search._pick_tool(mcp2.tools) == ("web_search_prime", "search_query")
                assert Search._pick_tool([{"name": "webSearchPrime"}, {"name": "x_search"}])[0] == "webSearchPrime"
                # 没有搜索工具：一句人话
                mcp.tools[:] = [{"name": "ping", "inputSchema": {}}]
                e.engine.search.mcp.clear()
                out = await e.engine.search.run("词")
                assert out == "搜索没成功：搜索服务没有提供搜索工具"
            finally:
                await runner.cleanup()
    env.run(body)


def test_mcp_lost_session_is_reinitialised_once_and_the_search_is_retried(env):
    async def body(e):
        with urls():
            mcp = McpServer("json")
            runner, port = await serve(mcp)
            try:
                search.URLS["zai-mcp"] = f"http://127.0.0.1:{port}/mcp"
                e.core.db.set_secret("search:zai-mcp", "k")
                e.engine.search.set_conf(provider="zai-mcp")
                await e.engine.search.run("第一次")
                mcp.log.clear()
                mcp.expire_after = mcp.calls  # 服务端把会话作废了：下一次请求回 404
                out = await e.engine.search.run("第二次")
                assert mcp.log == ["tools/call", "initialize", "notifications/initialized", "tools/call"]  # 失效 → 重新初始化 → 再调一次（工具名用缓存，不再 tools/list）
                assert "标题甲" in out and mcp.arguments["arguments"] == {"search_query": "第二次"} and mcp.sessions == 2
                # 重新连了还是 4xx：只重试这一次，给一句人话
                mcp.log.clear()
                mcp.sid = "别的会话"  # 之后每次都对不上
                mcp.sessions = 100

                async def always_404(req):
                    mcp.log.append((await req.json()).get("method"))
                    return web.Response(status=404, text="gone")
                await runner.cleanup()
                app = web.Application()
                app.router.add_post("/mcp", always_404)
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", port)
                await site.start()
                out = await e.engine.search.run("第三次")
                assert out.startswith("搜索没成功：") and mcp.log.count("initialize") == 1
            finally:
                await runner.cleanup()
    env.run(body)


def test_mcp_wrong_key_gives_the_usual_one_line(env):
    async def body(e):
        with urls():
            async def deny(req):
                return web.Response(status=401, text="no")
            app = web.Application()
            app.router.add_post("/mcp", deny)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            try:
                search.URLS["zai-mcp"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp"
                e.core.db.set_secret("search:zai-mcp", "错的")
                e.engine.search.set_conf(provider="zai-mcp")
                assert await e.engine.search.run("词") == "搜索没成功：搜索服务的密钥不对或没有权限"
            finally:
                await runner.cleanup()
    env.run(body)


# ---------- 五、密钥沿用同一家的模型密钥 ----------
def card(e, cid, provider, name, key="", base_url="http://x"):
    e.core.db.put("endpoint", {"id": cid, "name": name, "provider": provider, "protocol": "openai", "base_url": base_url})
    if key:
        e.core.db.set_secret(f"endpoint:{cid}", key)


def test_each_search_service_follows_the_key_of_the_model_card_of_the_same_vendor(env):
    async def body(e):
        s = e.engine.search
        # 测试环境里默认就有一张 DeepSeek 卡片（名字 mock，密钥 test-key）
        assert s.key_info("deepseek") == ("test-key", "mock") and s.key_for("deepseek") == "test-key"
        for pid in ("kimi", "zhipu", "zai-mcp", "zhipu-mcp"):
            assert s.key_info(pid) == ("", "") and not s.ready(pid)
        card(e, "ep_k", "moonshotai-cn", "月之暗面", "kimi-model-key")
        card(e, "ep_z", "zhipuai", "智谱聊天", "zhipu-model-key")
        card(e, "ep_zc", "zai-coding-plan", "Z.ai 套餐", "zai-plan-key")
        card(e, "ep_bc", "zhipuai-coding-plan", "智谱套餐", "zhipu-plan-key")
        assert s.key_info("kimi") == ("kimi-model-key", "月之暗面")
        assert s.key_info("zhipu") == ("zhipu-model-key", "智谱聊天")
        assert s.key_info("zai-mcp") == ("zai-plan-key", "Z.ai 套餐")
        assert s.key_info("zhipu-mcp") == ("zhipu-plan-key", "智谱套餐")
        assert all(s.ready(p) for p in ("kimi", "zhipu", "zai-mcp", "zhipu-mcp"))
        # 别家的卡片不串：moonshotai（国际站）也算 Kimi，zai（不是 Coding 套餐）不算 Z.ai Coding 套餐搜索
        card(e, "ep_m", "moonshotai", "Kimi 国际", "kimi-intl-key")
        assert s.key_info("kimi")[0] in ("kimi-model-key", "kimi-intl-key")
        e.core.db.delete("endpoint", "ep_zc")
        card(e, "ep_zz", "zai", "Z.ai 普通", "zai-plain-key")
        assert s.key_info("zai-mcp") == ("", "")
        # 卡片没填密钥：不算
        card(e, "ep_empty", "zai-coding-plan", "空卡片")
        assert s.key_info("zai-mcp") == ("", "")
        # 自动选服务：按顺序，第一家有密钥的
        e.core.db.delete("endpoint", "default")
        e.core.db.delete("endpoint", "backup")
        assert s.chosen() == "kimi"
    env.run(body)


def test_a_key_entered_for_the_search_service_wins_and_clearing_it_goes_back_to_following(env):
    async def body(e):
        s = e.engine.search
        card(e, "ep_k", "moonshotai", "月之暗面", "model-key")
        e.core.db.set_secret("search:kimi", "own-key")
        assert s.key_info("kimi") == ("own-key", "own") and s.key_for("kimi") == "own-key"
        e.core.db.set_secret("search:kimi", "")  # 清空：回到沿用
        assert s.key_info("kimi") == ("model-key", "月之暗面")
        # DeepSeek 也能单独填
        e.core.db.set_secret("search:deepseek", "ds-own")
        assert s.key_info("deepseek") == ("ds-own", "own")
    env.run(body)


def test_status_tells_where_each_key_comes_from(env):
    async def body(e):
        s = e.engine.search
        card(e, "ep_zc", "zai-coding-plan", "Z.ai 套餐", "zai-plan-key")
        e.core.db.set_secret("search:zhipu", "mine")
        by = {p["id"]: p for p in s.status()["providers"]}
        assert by["deepseek"]["key_from"] == "mock" and by["zai-mcp"]["key_from"] == "Z.ai 套餐"
        assert by["zhipu"]["key_from"] == "own" and by["kimi"]["key_from"] == ""
        assert by["zai-mcp"]["has_key"] is True and by["kimi"]["has_key"] is False
        assert all(by[p]["key"] is True for p in ("deepseek", "kimi", "zhipu", "zai-mcp", "zhipu-mcp"))  # 每家都能单独填自己的密钥
        assert "zai-mcp" in by and "zhipu-mcp" in by
    env.run(body)


def test_a_vision_call_rejecting_tools_does_not_switch_off_the_main_models_tools(env):
    """看图带工具被拒：只记在看图名下；同一个模型做主模型时照样能用工具（发表情包、联网搜索）。"""
    async def body(e):
        llm = e.engine.llm
        setg(e, "vis_model", e.core.settings.g("main_model"))
        setg(e, "vis_endpoint", e.core.settings.g("main_endpoint"))
        e.core.db.set_kv("no_tools", {})
        before = llm.tools_state(purpose="main")["state"]
        llm.mark_no_tools(purpose="vis")
        assert llm.tools_state(purpose="vis")["state"] == "no"
        assert llm.tools_state(purpose="main")["state"] == before != "no"
        llm.mark_no_tools(purpose="main")
        assert llm.tools_state(purpose="main")["state"] == "no"
    env.run(body)


def test_the_search_term_is_cleaned_of_markdown_marks(env):
    """服务器上实测：模型给搜索词套了 **粗体**；去掉记号再搜。"""
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            e.llm.vis = lambda content: "**丰川祥子 假笑 表情包**" if content[-1]["text"] == QUERY_PROMPT else {"desc": "描述"}
            await sync_one(e, ch)
            assert [x["query"] for x in e.llm.search_calls] == ["丰川祥子 假笑 表情包"]
    env.run(body)


def test_the_installer_clears_the_old_program_files_before_installing():
    """升级时先清掉旧版的 _internal（去掉的模板、外壳不残留）；数据目录不在里面。"""
    from pathlib import Path
    iss = (Path(__file__).resolve().parent.parent / "installer" / "PodMates.iss").read_text(encoding="utf-8-sig")
    sec = iss.split("[InstallDelete]", 1)[1].split("\n[", 1)[0]
    assert [ln for ln in sec.splitlines() if ln.startswith("Type:")] == ['Type: filesandordirs; Name: "{app}\\_internal"']
