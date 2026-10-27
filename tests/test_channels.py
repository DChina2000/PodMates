"""三个平台渠道：模拟的 OneBot（NapCat）、模拟的 Discord 对象、模拟的 wxauto。另测 NapCat 配置写入。"""
import asyncio
import json
import threading
import types
from pathlib import Path

from aiohttp import web

from mb.channels import napcat
from mb.channels.discord_ch import DiscordChannel, app_id_from_token, invite_url, to_msg
from mb.channels.onebot import QQChannel, to_segments
from mb.channels.wechat import WeChatChannel

HAITUN = "90001"


class MockOneBot:
    """模拟 NapCat 的正向 WebSocket：记录收到的动作，能推事件。"""

    def __init__(self, self_id="900", token="tok"):
        self.self_id, self.token = self_id, token
        self.actions: list[dict] = []
        self.ws = None
        self.connected = asyncio.Event()
        self.n = 0

    async def handle(self, req):
        if req.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws = ws
        self.connected.set()
        async for m in ws:
            a = json.loads(m.data)
            self.actions.append(a)
            data = None
            act = a["action"]
            if act == "get_login_info":
                data = {"user_id": int(self.self_id), "nickname": "海豚娘小号"}
            elif act == "get_group_list":
                data = [{"group_id": 100, "group_name": "测试群"}]
            elif act in ("send_group_msg", "send_private_msg"):
                self.n += 1
                data = {"message_id": 5000 + self.n}
            elif act == "get_msg":
                data = {"sender": {"user_id": int(self.self_id)}}
            await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": a["echo"]}))
        return ws

    async def push(self, ev):
        await self.ws.send_str(json.dumps(ev, ensure_ascii=False))

    def sent(self, action):
        return [a for a in self.actions if a["action"] == action]


async def wait_for(cond, timeout=5):
    for _ in range(int(timeout * 100)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


def group_msg(uid, segs, seq, role="member", name="群友"):
    return {"post_type": "message", "message_type": "group", "group_id": 100, "user_id": int(uid), "self_id": 900,
            "message_id": seq, "message_seq": seq, "message": segs, "sender": {"nickname": name, "role": role}}


def test_qq_onebot_channel(env):
    async def body(e):
        e.core.settings.set("global", "proactive", False)
        mock = MockOneBot()
        app = web.Application()
        app.router.add_get("/", mock.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        acc = e.core.db.put("account", {"id": "ac_qq", "platform": "qq", "qq": "900", "role_id": "r_haitun",
                                        "manage_napcat": False, "ws_port": port})
        e.core.db.set_secret("onebot:ac_qq", "tok")
        e.engine.reload()
        e.core.db.set_kv("platforms", {"qq": True})
        await e.engine.start_account(acc)
        ch = e.engine.bots["ac_qq"].channel
        await wait_for(lambda: ch.status == "online")
        assert ch.self_id == "900" and "qq:100" in e.engine.rooms  # 连上后登记了群
        e.llm.main = lambda s, u: "妈 [斜眼笑]"
        # 群里@她 → 回复并引用那条
        await mock.push(group_msg(HAITUN, [{"type": "at", "data": {"qq": "900"}}, {"type": "text", "data": {"text": " 在干嘛"}}], 1, name="海豚"))
        await wait_for(lambda: mock.sent("send_group_msg"))
        await e.engine.idle()
        m = mock.sent("send_group_msg")[0]["params"]
        assert m["group_id"] == 100
        assert m["message"][0] == {"type": "reply", "data": {"id": "1"}}
        assert m["message"][1:] == [{"type": "text", "data": {"text": "妈 "}}, {"type": "face", "data": {"id": "178"}}]
        assert "海豚〔海豚本人〕 @了你，说：在干嘛" in e.llm.calls[-1]["messages"][1]["content"]
        # 回复她的消息（reply 段 → get_msg 查到是她发的）也算@
        await mock.push(group_msg("111", [{"type": "reply", "data": {"id": "5001"}}, {"type": "text", "data": {"text": "真的假的"}}], 2))
        await wait_for(lambda: len(mock.sent("send_group_msg")) == 2)
        # 戳一戳
        await mock.push({"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "group_id": 100,
                         "user_id": 111, "target_id": 900, "time": 1})
        await wait_for(lambda: len(mock.sent("send_group_msg")) == 3)
        await e.engine.idle()
        assert "戳了戳你" in e.llm.calls[-1]["messages"][1]["content"]
        # 撤回 → delete_msg
        await mock.push(group_msg(HAITUN, [{"type": "at", "data": {"qq": "900"}}, {"type": "text", "data": {"text": "撤回"}}], 3))
        await wait_for(lambda: mock.sent("delete_msg"))
        assert mock.sent("delete_msg")[0]["params"] == {"message_id": 5003}
        # 好友申请：仅人物表里的人同意
        await mock.push({"post_type": "request", "request_type": "friend", "user_id": int(HAITUN), "flag": "f1"})
        await wait_for(lambda: mock.sent("set_friend_add_request"))
        assert mock.sent("set_friend_add_request")[0]["params"] == {"flag": "f1", "approve": True}
        # 被踢下线 → 通知（日志 + 私聊作者大号由另一个在线 QQ 号发；这里只有一个号，只记日志）
        await mock.push({"post_type": "notice", "notice_type": "bot_offline", "message": "账号在别处登录"})
        await wait_for(lambda: ch.status == "error")
        assert any("被踢下线" in r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='notify'"))
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


def test_napcat_log_redaction():
    assert napcat.redact("已更新 WebUI Token 为 abcdef123", ("abcdef123",)) == "已更新 WebUI Token 为 ***"
    assert napcat.redact("WebUi Token: zz99xx88 url?token=zz99xx88", ()) == "WebUi Token: *** url?token=***"


def test_qq_face_segments():
    assert to_segments("你好[doge]", True) == [{"type": "text", "data": {"text": "你好"}}, {"type": "face", "data": {"id": "179"}}]
    assert to_segments("你好[doge]", False) == [{"type": "text", "data": {"text": "你好[doge]"}}]


def test_napcat_config_and_layout(tmp_path):
    shell = tmp_path / "NapCat.Shell"
    (shell / "config").mkdir(parents=True)
    (shell / "NapCatWinBootMain.exe").write_bytes(b"")
    (shell / "napcat.mjs").write_text("")
    lay = napcat.layout(shell)
    assert lay["kind"] == "shell" and lay["config"] == shell / "config"
    # 已有的配置（比如旧程序用的 3001）原样保留，只加/替换本程序那一条
    old = {"network": {"websocketServers": [{"name": "旧的", "port": 3001, "enable": True}]}, "musicSignUrl": "x"}
    (shell / "config" / "onebot11_123.json").write_text(json.dumps(old), encoding="utf-8")
    napcat.write_config(lay, "123", 3101, "secret")
    napcat.write_config(lay, "123", 3102, "secret2")
    cfg = json.loads((shell / "config" / "onebot11_123.json").read_text(encoding="utf-8"))
    ws = cfg["network"]["websocketServers"]
    assert [s["port"] for s in ws] == [3001, 3102]
    mine = ws[1]
    assert mine["host"] == "127.0.0.1" and mine["token"] == "secret2" and mine["messagePostFormat"] == "array" and mine["enable"]
    assert cfg["musicSignUrl"] == "x"
    one = tmp_path / "NapCat.52230.Shell"
    (one / "versions" / "9.9.33" / "resources" / "app" / "napcat" / "config").mkdir(parents=True)
    (one / "NapCatWinBootMain.exe").write_bytes(b"")
    (one / "QQ.exe").write_bytes(b"")
    lay = napcat.layout(one)
    assert lay["kind"] == "onekey" and lay["config"].name == "config" and "napcat" in str(lay["config"])
    assert napcat.layout(tmp_path) is None


# ---------- Discord ----------
class O(types.SimpleNamespace):
    pass


class FakeDiscordChannel:
    def __init__(self, cid, guild):
        self.id, self.guild, self.name = cid, guild, "闲聊"
        self.sent, self.deleted = [], []

    async def send(self, text, reference=None, mention_author=True):
        self.sent.append((text, reference))
        return O(id=9000 + len(self.sent))

    async def fetch_message(self, mid):
        async def delete():
            self.deleted.append(mid)
        return O(delete=delete)


def test_discord_channel(env):
    async def body(e):
        e.core.settings.set("global", "proactive", False)
        acc = e.core.db.put("account", {"id": "ac_dc", "platform": "discord", "role_id": "r_haitun"})
        ch = DiscordChannel(e.engine, acc)
        guild = O(name="海豚服", me=None)
        dch = FakeDiscordChannel(555, guild)
        ch.client = O(get_channel=lambda cid: dch if cid == 555 else None)
        ch.self_id, ch.status = "42", "online"
        e.engine.add_bot(acc, ch)
        e.llm.main = lambda s, u: "**你好**呀"
        me = O(id=42, name="bot", display_name="bot", bot=True)
        author = O(id=777000111222333444, name="haitun", display_name="海豚", bot=False, guild_permissions=O(administrator=True))
        m = O(id=777, author=author, guild=guild, channel=dch, content="<@42> 你好", mentions=[me], reference=None, attachments=[])
        msg = to_msg("ac_dc", m, "42")
        assert msg.chat_type == "group" and msg.chat_id == "555" and msg.text == "你好" and msg.at_ids == ["42"]
        assert msg.sender_role == "admin" and msg.chat_name == "海豚服 #闲聊"
        await e.engine.on_message(msg)
        await e.engine.idle()
        text, ref = dch.sent[0]
        assert text == "**你好**呀"  # Discord 默认可用 Markdown
        assert ref is not None and ref.message_id == 777
        # 认人：Discord ID 在人物表里
        assert "海豚〔海豚本人〕 @了你" in e.llm.calls[-1]["messages"][1]["content"]
        # 自己发的消息不处理
        assert to_msg("ac_dc", O(id=1, author=me, guild=guild, channel=dch, content="x", mentions=[], reference=None, attachments=[]), "42") is None
        await ch.recall("group", "555", "9001")
        assert dch.deleted == [9001]
        # 邀请链接：令牌第一段就是应用 ID
        import base64
        token = base64.b64encode(b"1234567890").decode().rstrip("=") + ".abc.def"
        assert app_id_from_token(token) == "1234567890"
        assert "client_id=1234567890" in invite_url("1234567890") and "scope=bot" in invite_url("1")
    env.run(body)


# ---------- 微信（模拟 wxauto4）----------
class FakeWxChat:
    def __init__(self, who):
        self.who, self.sent = who, []

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        self.sent.append(msg)
        return O(is_success=True)


class FakeWeChat:
    instances = []

    def __init__(self, *a, **k):
        self.cbs, self.chats, self.listening = {}, {}, False
        FakeWeChat.instances.append(self)

    def GetMyInfo(self):
        return {"nickname": "海豚娘小号"}

    def AddListenChat(self, nickname, callback):
        self.cbs[nickname] = callback
        self.chats[nickname] = FakeWxChat(nickname)
        return self.chats[nickname]

    def StartListening(self):
        self.listening = True

    def RemoveListenChat(self, nickname, close_window=True):
        self.cbs.pop(nickname, None)

    def IsOnline(self):
        return True


def test_wechat_channel(env):
    async def body(e):
        e.core.settings.set("global", "proactive", False)
        p = e.core.db.doc("person", "pe_haitun")
        p["wechat_remark"] = ["秘密备注妈"]
        e.core.db.put("person", p)
        e.engine.reload()
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun",
                                        "listen": [{"name": "相亲相爱一家人", "type": "group"}, {"name": "秘密备注妈", "type": "private"}]})
        ch = WeChatChannel(e.engine, acc)
        ch.lib = types.SimpleNamespace(WeChat=FakeWeChat)
        e.engine.add_bot(acc, ch)
        await ch.start()
        assert ch.status == "confirm"  # 第一次：先请用户确认「这是机器人小号吗」
        assert (await ch.confirm_bind(True))["ok"]
        assert ch.status == "online" and ch.self_id == "海豚娘小号"
        wx = FakeWeChat.instances[-1]
        assert wx.listening and set(wx.cbs) == {"相亲相爱一家人", "秘密备注妈"}
        await e.engine.idle()
        assert "wechat:相亲相爱一家人" in e.engine.rooms
        e.llm.main = lambda s, u: "**在呢**"
        # 监听线程里来消息：发消息的人显示成小号给她设的秘密备注
        m = O(attr="friend", type="text", content="@海豚娘小号 吃饭没", sender="秘密备注妈", id="x1")
        threading.Thread(target=wx.cbs["相亲相爱一家人"], args=(m, wx.chats["相亲相爱一家人"])).start()
        await wait_for(lambda: wx.chats["相亲相爱一家人"].sent)
        assert wx.chats["相亲相爱一家人"].sent == ["在呢"]  # 微信纯文本：去掉加粗
        await e.engine.idle()
        assert "秘密备注妈〔海豚本人〕 @了你，说：吃饭没" in [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][1]["content"]
        # 私聊
        m2 = O(attr="friend", type="text", content="在吗", sender="秘密备注妈", id="x2")
        threading.Thread(target=wx.cbs["秘密备注妈"], args=(m2, wx.chats["秘密备注妈"])).start()
        await wait_for(lambda: wx.chats["秘密备注妈"].sent)
        await e.engine.idle()
        assert [c for c in e.llm.calls if c["_kind"] == "main"][-1]["messages"][0]["content"].endswith("这次私聊的是海豚本人。")
        # 自己发的、系统消息不处理；微信不能撤回
        threading.Thread(target=wx.cbs["相亲相爱一家人"], args=(O(attr="self", type="text", content="x", sender="me"), None)).start()
        assert ch.supports_recall is False
        await ch.stop()
    env.run(body)
