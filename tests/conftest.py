"""测试公共件：临时数据目录、模拟的模型服务、模拟的平台渠道。全部离线，不连真号、不调真模型。"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mb.channels.base import Channel, Msg  # noqa: E402
from mb.channels.wechat import WeChatChannel  # noqa: E402


class _NoRealWeChat:
    """测试里永远不碰真的微信（这台电脑登录的是真人的号）：没换成模拟 wxauto 的微信渠道，一连微信就报错。"""

    @staticmethod
    def WeChat(*a, **k):
        raise RuntimeError("测试环境不允许连真的微信")


WeChatChannel.lib = _NoRealWeChat  # 各测试要用模拟的，就在渠道实例上再设自己的


class MockLLM:
    """模拟 /chat/completions。按提示词判断是守门员、记忆提取还是主模型，交给可替换的函数回答。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.gate = lambda prompt: {"reply": False, "brief": "没东西可接"}
        self.main = lambda system, user: "收到"
        self.mem = lambda prompt: {"recap": "", "people": [], "memes": []}
        self.fail_next = 0
        self.tool_plan = None  # 函数(body) -> (工具名, 参数) 或 None：模拟模型决定调用工具
        self.vis = None  # 函数(content列表) -> dict：模拟看图模型的 JSON 回答
        self.search_calls: list[dict] = []
        self.stale = lambda prompt: {"action": "send", "why": "还合适"}  # 过时回复的重新判断
        self.compress = lambda prompt: "压缩后的短回复。"  # 超过群聊字数上限时的压缩
        self.merge = lambda prompt: "合并后的前情"  # 前情往上并
        self.impression = lambda prompt: "印象：一个群友"  # 人物印象
        self.reject_over = None  # (字数, 报错里写的上限)：整条请求超过这个字数就按「超长」拒掉
        self.rejected = 0

    async def handle(self, req):
        body = await req.json()
        self.calls.append(body)
        if body.get("tools") and getattr(self, "reject_tools", False):
            return web.Response(status=400, text='{"error": {"message": "unknown parameter: tools"}}')
        if self.fail_next:
            self.fail_next -= 1
            return web.json_response({"error": {"code": "1113", "message": "余额不足"}}, status=429)
        msgs = body["messages"]
        last = msgs[-1]["content"]
        if self.reject_over and sum(len(str(m.get("content", ""))) for m in msgs) > self.reject_over[0]:
            self.rejected += 1
            body["_kind"] = "rejected"
            lim = self.reject_over[1]
            return web.json_response({"error": {"message": f"This model's maximum context length is {lim} tokens. However, your messages resulted in more tokens."}}, status=400)
        if body.get("tools") and self.tool_plan and msgs[-1]["role"] != "tool":
            plan = self.tool_plan(body)
            if plan:
                body["_kind"] = "main_tool"
                return web.json_response({"choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "先想想要不要查",
                                                                   "tool_calls": [{"id": "call_1", "type": "function",
                                                                                   "function": {"name": plan[0], "arguments": json.dumps(plan[1], ensure_ascii=False)}}]}}],
                                          "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})
        if isinstance(last, list) and getattr(self, "block_vis", None) and self.block_vis(last):
            body["_kind"] = "vis_blocked"  # 模拟智谱 / Z.ai 的内容审核拦截
            return web.json_response({"contentFilter": [{"level": 1, "role": "assistant"}],
                                      "error": {"code": "1301", "message": "System detected potentially unsafe or sensitive content"}}, status=400)
        if isinstance(last, list) and self.vis is not None:
            body["_kind"] = "vis"
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": json.dumps(self.vis(last), ensure_ascii=False)}}],
                                      "usage": {"prompt_tokens": 800, "completion_tokens": 40}})
        if isinstance(last, str) and "重新判断这条回复" in last:
            text, kind = json.dumps(self.stale(last), ensure_ascii=False), "stale"
        elif isinstance(last, str) and "超过了这个群的字数上限" in last:
            text, kind = self.compress(last), "compress"
        elif isinstance(last, str) and "只输出JSON：" in last:
            text, kind = json.dumps(self.gate(last), ensure_ascii=False), "gk"
        elif isinstance(last, str) and "请整理成以后聊天用得上的记忆" in last:
            text, kind = json.dumps(self.mem(last), ensure_ascii=False), "mem"
        elif isinstance(last, str) and "合并成一段更概括的前情" in last:
            text, kind = self.merge(last), "merge"
        elif isinstance(last, str) and "整理成这个人现在的印象" in last:
            text, kind = self.impression(last), "impression"
        else:
            system = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
            text, kind = self.main(system, last), "main"
        body["_kind"] = kind
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": text}}],
                                  "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                            "prompt_tokens_details": {"cached_tokens": 200}}})

    async def search(self, req):
        body = await req.json()
        self.search_calls.append(body)
        return web.json_response({"results": [{"title": "标题甲", "url": "http://example.test/a", "snippet": "短", "summary": "SEARCH-RESULT-SECRET 汤姆今天夺冠"}]})

    async def embeddings(self, req):
        """模拟 OpenAI 兼容的 /embeddings：按字计数的 64 维向量（只测机制，不测效果）。"""
        body = await req.json()
        self.calls.append({"_kind": "embed", **body})
        if getattr(self, "embed_fail", False):
            return web.Response(status=500, text='{"error": "boom"}')
        out = []
        for i, t in enumerate(body["input"]):
            v = [0.0] * 64
            for ch in t:
                v[ord(ch) % 64] += 1
            n = sum(x * x for x in v) ** 0.5 or 1
            out.append({"index": i, "embedding": [x / n for x in v]})
        return web.json_response({"data": out, "usage": {"prompt_tokens": sum(len(t) for t in body["input"])}})

    def kinds(self):
        return [c["_kind"] for c in self.calls if "_kind" in c]


class FakeChannel(Channel):
    """模拟渠道：记录发出去的消息和撤回。"""

    def __init__(self, engine, account, self_id, name="机器人"):
        super().__init__(engine, account)
        self.platform = account["platform"]
        self.self_id, self.self_name = self_id, name
        self.status = "online"
        self.sent: list[tuple] = []
        self.recalled: list[str] = []
        self.files: dict[str, bytes] = {}  # 文件 id → 内容（模拟「下载文件」）
        self.imgs: dict[str, bytes] = {}  # 图片地址 → 内容（模拟「下载图片」）
        self.n = 0

    async def start(self):
        pass

    supports_image = True
    supports_at = True

    async def send(self, chat_type, chat_id, text, reply_to=None, at=None):
        self.n += 1
        mid = f"{self.self_id}-{self.n}"
        self.sent.append((chat_type, chat_id, text, reply_to, mid))
        if at:
            self.ats = getattr(self, "ats", []) + [(mid, at)]
        return [mid]

    async def send_image(self, chat_type, chat_id, data, reply_to=None):
        self.n += 1
        mid = f"{self.self_id}-{self.n}"
        self.images = getattr(self, "images", []) + [(chat_type, chat_id, data, reply_to, mid)]
        return [mid]

    async def recall(self, chat_type, chat_id, msg_id):
        self.recalled.append(msg_id)
        return True

    async def fetch_image(self, url):
        return self.imgs.get(url)

    async def fetch_file(self, f, max_bytes):
        raw = self.files.get(f.get("id"))
        return raw if raw is not None and len(raw) <= max_bytes else None

    async def set_chat_card(self, chat_id, card):
        self.cards = getattr(self, "cards", []) + [(chat_id, card)]


class Env:
    def __init__(self, core, llm):
        self.core, self.llm = core, llm
        self.engine = core.engine
        self.engine.pace = 0

    def add_bot(self, uid, role_id="r_haitun", platform="qq", name=None):
        acc = self.core.db.put("account", {"id": f"ac_{uid}", "platform": platform, "role_id": role_id, "qq": uid})
        ch = FakeChannel(self.engine, acc, uid, name or f"bot{uid}")
        self.engine.add_bot(acc, ch)
        return ch

    def add_role(self, rid, name, summary="测试角色"):
        self.core.db.put("role", {"id": rid, "name": name, "persona": "p_乙", "summary": summary, "attitudes": {}, "relations": {}})
        self.engine.reload()

    async def say(self, uid, text, chat="100", name=None, at=(), platform="qq", account=None, reply_to_uid="", role="member", private=False, files=None, reply_to_seq=0, images=None):
        """像真实情况一样：群里每个机器人账号都会收到同一条消息（引擎负责去重）。"""
        self.seq = getattr(self, "seq", 0) + 1
        accs = [account] if account or private else [a for a, b in self.engine.bots.items() if b.platform == platform]
        if private and not account:
            accs = [next(iter(self.engine.bots))]
        for acc in accs:
            await self.engine.on_message(Msg(platform, acc, "private" if private else "group", uid if private else chat, uid,
                                             name or f"群友{uid}", text, msg_id=f"{acc}-m{self.seq}",
                                             chat_name="测试群", at_ids=list(at), reply_to_uid=reply_to_uid, sender_role=role,
                                             files=list(files or []), images=list(images or []), reply_to_id=f"{acc}-m{reply_to_seq}" if reply_to_seq else "",
                                             dedup=f"{chat}|{uid}|{self.seq}"))
        await self.engine.idle()

    def room(self, chat="100", platform="qq"):
        return self.engine.rooms[f"{platform}:{chat}"]


@pytest.fixture
def env(monkeypatch):
    """用法：env.run(async def body(e): ...)。每个测试一个干净的数据目录 + 模拟模型服务。"""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    monkeypatch.setenv("PODMATES_DATA", tmp.name)

    class Runner:
        @staticmethod
        def run(body):
            async def main():
                from mb.core import Core
                llm = MockLLM()
                app = web.Application()
                app.router.add_post("/v4/chat/completions", llm.handle)
                app.router.add_post("/v4/embeddings", llm.embeddings)
                app.router.add_post("/v4/tools/search", llm.search)
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                port = site._server.sockets[0].getsockname()[1]
                core = Core(Path(tmp.name))
                core.llm.RETRY_DELAY = 0.01  # 429 / 5xx / 超时的自动重试不真等
                core.db.put("endpoint", {"id": "default", "name": "mock", "provider": "deepseek", "protocol": "openai", "base_url": f"http://127.0.0.1:{port}/v4"})
                core.db.put("endpoint", {"id": "backup", "name": "mock2", "provider": "deepseek", "protocol": "openai", "key_from": "default", "base_url": f"http://127.0.0.1:{port}/v4"})
                # 测试用的角色与人物卡（公开版的默认数据里没有这些）
                from mb import templates
                templates.instantiate(core.db, "haitun", name="海豚娘", role_id="r_haitun", persona_id="p_海豚娘")
                other = templates.instantiate(core.db, "gentle-friend", name="乙角色", persona_id="p_乙")
                core.db.delete("role", other["id"])
                shell = core.db.doc("module", "m_外壳_乙")
                shell["body"] = "你是乙角色。"  # 测试用的标记：能分辨在岗的是哪个人设
                core.db.put("module", shell)
                core.db.put("person", dict(id="pe_haitun", name="海豚", identity="她妈", call="妈", qq=["90001"], discord=["777000111222333444"],
                                           wechat_remark=[], nicks={"qq": "测试昵称"}, note="", knows="",
                                           attitude={"presets": ["亲近贴心"], "custom": ""}, admin=True, groups=[]))
                core.engine.reload()
                core.db.set_secret("endpoint:default", "test-key")
                core.settings.set("global", "group_reply_max", 0)  # 旧测试不限字数；字数上限的测试自己打开
                core.settings.set("global", "reply_min_gap", 0)  # 旧测试不隔着等；「两次回复最小间隔」的测试自己打开
                core.settings.set("global", "pro_wait", 0)  # 旧测试主动插话不等对方说完；「主动插话前等对方停几秒」的测试自己打开
                core.settings.set("global", "private_wait", 0)  # 旧测试私聊来一条回一条；「私聊里等对方停几秒再回」的测试自己打开
                core.settings.set("global", "search_on", False)  # 旧测试不带工具；联网搜索的测试自己打开
                core.settings.set("global", "sticker_desc_search", False)  # 旧测试里每张表情包只调一次看图；写描述时联网搜索的测试自己打开
                core.db.set_kv("setup_done", True)
                e = Env(core, llm)
                e.port = port
                try:
                    await body(e)
                finally:
                    e.core.engine.close()
                    await e.core.llm.close()
                    await runner.cleanup()
                    e.core.db.close()
            asyncio.run(main())

    yield Runner
    tmp.cleanup()
