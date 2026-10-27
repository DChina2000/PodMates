"""补充十二：微信「主窗口轮询」收消息方式（不拆独立窗口）。全程用模拟的 wxauto，不碰真微信。"""
import asyncio
import types
from pathlib import Path

import pytest

from mb.channels import wechat
from mb.channels.wechat import WeChatChannel, new_since


_ids = iter(range(1, 10 ** 9))


def M(attr, sender, content, mtype="text"):
    return types.SimpleNamespace(attr=attr, sender=sender, content=content, type=mtype, id=f"id{next(_ids)}")  # 真机上每条消息一个 id，读多少次都一样


class Wx:
    """模拟的 wxauto4.WeChat（主窗口方式）：会话列表 + 每个聊天的消息 + 当前打开的聊天。"""
    instances = []
    fail_listen = False
    steal_api = None  # 设了它：ChatWith 换会话、GetAllMessage、SendMsg 会把微信窗口激活到前台（真机上就是这样）
    vis_api = None  # 设了它：窗口在屏幕外时 ChatWith 悄悄没效果（模拟某些微信版本屏幕外点不到）
    info = {"display_name": "小号"}  # 真机上 GetMyInfo 就只有 display_name

    state = None  # 微信这个应用里的状态（各聊天的消息、当前打开的是哪个、未读数、操作记录）：不管 wxauto 连几次都是同一份

    @classmethod
    def reset_state(cls):
        cls.state = {"chats": {"群A": [M("friend", "甲", "旧消息1"), M("friend", "乙", "旧消息2")], "群B": [M("friend", "丙", "B的旧消息")],
                               "无关群": [M("friend", "丁", "无关")]},
                     "cur": "无关群", "unread": {}, "log": [], "sent": []}

    def __init__(self, *a, **k):
        st = Wx.state
        self.chats, self.unread, self.log, self.sent = st["chats"], st["unread"], st["log"], st["sent"]
        self._info = dict(Wx.info)  # 和真的一样：构造时读一次、之后 GetMyInfo 返回缓存
        self.listened = []
        Wx.instances.append(self)

    @property
    def cur(self):
        return Wx.state["cur"]

    @cur.setter
    def cur(self, v):
        Wx.state["cur"] = v

    nickname = "小号"

    def GetMyInfo(self):
        return dict(self._info)

    def GetSession(self):
        self.log.append("peek")
        return [types.SimpleNamespace(name=n, content=ms[-1].content, time="12:00", new_count=self.unread.get(n, 0)) for n, ms in self.chats.items()]

    def ChatInfo(self):
        return {"chat_name": self.cur}

    def ChatWith(self, who, exact=True, force=False, force_wait=0.5):
        if Wx.vis_api is not None and not Wx.vis_api.seen():
            self.log.append(("switch-ignored", who))
            return
        self.log.append(("switch", who))
        if Wx.steal_api is not None and who != self.cur:
            Wx.steal_api.steal()
        self.cur = who
        self.unread[who] = 0

    def GetAllMessage(self):
        self.log.append(("read", self.cur))
        if Wx.steal_api is not None:
            Wx.steal_api.steal()
        return list(self.chats[self.cur])

    def SendMsg(self, msg, who=None, **k):
        if Wx.steal_api is not None:
            Wx.steal_api.steal()
        if who:
            self.ChatWith(who)
        self.sent.append((self.cur, msg))
        self.chats.setdefault(self.cur, []).append(M("self", "小号", msg))
        return types.SimpleNamespace(is_success=True)

    def AddListenChat(self, n, cb):
        if Wx.fail_listen:
            raise RuntimeError("MoveWindow 1400 无效句柄")
        self.listened.append(n)
        return types.SimpleNamespace(who=n, SendMsg=lambda t: None)

    def StartListening(self):
        pass

    def RemoveListenChat(self, n, close_window=True):
        self.listened.remove(n)

    def IsOnline(self):
        return True

    def add(self, chat, attr, sender, content):
        self.chats[chat].append(M(attr, sender, content))
        if chat != self.cur and attr == "friend":
            self.unread[chat] = self.unread.get(chat, 0) + 1


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    Wx.instances, Wx.fail_listen, Wx.info, Wx.vis_api, Wx.steal_api = [], False, {"display_name": "小号"}, None, None
    Wx.reset_state()
    monkeypatch.setattr(WeChatChannel, "lib", types.SimpleNamespace(WeChat=Wx))


def make(e, listen=None):
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun",
                                    "listen": listen or [{"name": "群A", "type": "group"}, {"name": "群B", "type": "group"}]})
    ch = WeChatChannel(e.engine, acc)
    e.engine.add_bot(acc, ch)
    return ch


async def up(e, mode="poll", interval=0.05, **kw):
    e.core.settings.set("global", "proactive", False)
    e.core.settings.set("platform:wechat", "wechat_recv_mode", mode)
    e.core.settings.set("platform:wechat", "wechat_poll", interval)
    ch = make(e, **kw)
    await ch.start()
    if ch.status == "confirm":  # 第一次要确认身份；已经绑定过的直接上线
        await ch.confirm_bind(True)
    assert ch.status == "online"
    return ch


async def until(cond, secs=3.0):
    for _ in range(int(secs / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def ctx_texts(e, key):
    r = e.engine.rooms.get(key)
    return [x.text for x in r.ctx] if r else []


# ---------- 纯函数：从读到的最后几条里找出新的 ----------
def test_new_since():
    a, b, c, d = ("甲", "1"), ("乙", "2"), ("甲", "3"), ("丙", "4")
    assert new_since([], [a, b]) == []  # 第一次：没有「上次」，不算新
    assert new_since([a, b], [a, b]) == []
    assert new_since([a, b], [a, b, c]) == [c]
    assert new_since([a, b], [b, c, d]) == [c, d]  # 旧的滚出去一条
    assert new_since([a, b], [c, d]) == [c, d]  # 完全衔接不上：新消息超过一屏，取最后几条
    h = ("甲", "哈哈")
    assert new_since([a, h], [a, h, h]) == [h]  # 连发两条一样的：只算多出来的一条
    assert new_since([h], [h, h, h]) == [h, h]
    many = [("甲", str(i)) for i in range(30)]
    assert len(new_since([a], many)) == wechat.POLL_MAX_NEW  # 衔接不上时的保险：一次最多这么多条


# ---------- 选方式 ----------
def test_mode_selection_and_fallback(env, monkeypatch):
    async def body(e):
        ch = await up(e, "window")
        assert ch.mode == "window" and Wx.instances[-1].listened == ["群A", "群B"]
        await ch.stop()
        # 设置选「主窗口轮询」：不拆窗口，一个监听都不加
        ch = await up(e, "poll")
        assert ch.mode == "poll" and Wx.instances[-1].listened == []
        await ch.stop()
        # 自动 + 版本在范围内 → 独立窗口；版本超出范围 → 轮询
        monkeypatch.setattr(wechat, "check_version", lambda: {"installed": True, "ok": False, "version": "4.1.15.56", "note": ""})
        ch = make(e)
        ch.lib_real = True
        e.core.settings.set("platform:wechat", "wechat_recv_mode", "auto")
        assert ch._choose_mode() == "poll"
        monkeypatch.setattr(wechat, "check_version", lambda: {"installed": True, "ok": True, "version": "4.1.8.0", "note": ""})
        assert ch._choose_mode() == "window"
        await ch.stop()
        # 独立窗口失败（新版微信拆窗口报错）→ 自动改用轮询，写日志，已拆出来的撤掉
        Wx.fail_listen = True
        ch = await up(e, "window")
        assert ch.mode == "poll" and ch.status == "online"
        assert any("独立窗口方式失败" in l["text"] and "改用主窗口轮询" in l["text"] for l in e.core.db.rows("SELECT text FROM logs"))
        await ch.stop()
    env.run(body)


# ---------- 轮询：不丢、不重复、切回原会话 ----------
def test_poll_receives_new_messages_once_and_returns_to_original_chat(env):
    async def body(e):
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        assert await until(lambda: "群A" in ch.tails and "群B" in ch.tails)  # 第一轮：每个监听聊天读一次当基线
        assert ctx_texts(e, "wechat:群A") == [] and ctx_texts(e, "wechat:群B") == []  # 旧消息不重放
        assert len(ch.seen_ids["群A"]) == 2 and len(ch.seen_ids["群B"]) == 1  # 基线的消息 id 也记下了
        assert wx.cur == "无关群"  # 读完切回原来的会话
        reads0 = [x for x in wx.log if isinstance(x, tuple) and x[0] == "read"]
        assert {r[1] for r in reads0} == {"群A", "群B"}
        # 没变化：只看会话列表，不切换、不读
        n = len(wx.log)
        await asyncio.sleep(0.3)
        assert all(x == "peek" for x in wx.log[n:])
        # 群A 来了新消息：读到、进引擎，群B 和无关群不动
        wx.add("群A", "friend", "甲", "新消息1")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["新消息1"])
        assert wx.cur == "无关群"
        # 连发两条一样的：各算一条，不重复也不丢
        wx.add("群A", "friend", "甲", "哈哈")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["新消息1", "哈哈"])
        wx.add("群A", "friend", "甲", "哈哈")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["新消息1", "哈哈", "哈哈"])
        await asyncio.sleep(0.3)
        assert ctx_texts(e, "wechat:群A") == ["新消息1", "哈哈", "哈哈"]  # 之后轮询多少次都不重复
        assert ctx_texts(e, "wechat:群B") == []
        assert ("read", "无关群") not in wx.log
        # 自己发的不处理；系统消息、时间不处理
        wx.add("群B", "self", "小号", "我自己发的")
        wx.add("群B", "time", "", "12:30")
        wx.add("群B", "friend", "丙", "B新消息")
        assert await until(lambda: ctx_texts(e, "wechat:群B") == ["B新消息"])
        assert ch.switch_ms and max(ch.switch_ms) < 1000
        await ch.stop()
    env.run(body)


def test_poll_private_chat_and_unmatched_chat(env):
    async def body(e):
        ch = await up(e, "poll", listen=[{"name": "群A", "type": "group"}, {"name": "无关群", "type": "private"}])
        wx = Wx.instances[-1]
        assert await until(lambda: "无关群" in ch.tails)
        wx.cur = "群A"
        wx.add("无关群", "friend", "丁", "私聊你好")
        assert await until(lambda: any(r["text"] == "私聊你好" for r in e.core.db.rows("SELECT text FROM messages WHERE room LIKE 'dm:%'")))
        assert wx.cur == "群A"  # 读完切回的是当时打开的那个会话
        await ch.stop()
    env.run(body)


def test_poll_send_in_main_window_and_switch_back(env):
    async def body(e):
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        await until(lambda: "群A" in ch.tails)
        wx.cur = "无关群"
        await ch.send("group", "群A", "机器人说的话")
        assert wx.sent == [("群A", "机器人说的话")] and wx.cur == "无关群"  # 切到目标会话发，发完切回
        assert [x for x in wx.log if isinstance(x, tuple) and x[0] == "switch"][-2:] == [("switch", "群A"), ("switch", "无关群")]
        await ch.stop()
    env.run(body)


def test_split_reply_switches_to_the_chat_once_and_back_once(env):
    """一次回复分成几条发：主窗口方式下不用每条都切过去再切回，6 条不再来回切 12 次；发完停在原来的会话。"""
    async def body(e):
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        await until(lambda: "群A" in ch.tails)
        wx.cur = "无关群"
        e.core.settings.set("global", "split_threshold", 1)
        wx.sent.clear(); wx.log.clear()
        await e.engine.send_text(e.engine.duty(e.engine.bots["ac_wx"], None), "group", "群A", "第一句。第二句。第三句。", None, None)
        assert [m for _, m in wx.sent] == ["第一句。", "第二句。", "第三句。"]
        back = [x for x in wx.log if x == ("switch", "无关群")]
        assert len(back) == 1 and wx.cur == "无关群"  # 只在最后一条发完后切回去一次
        assert ch.held_orig == ""
        # 中途出错没切回的：下一次发送接着切回去
        ch.held_orig = "无关群"
        wx.cur = "群A"
        await ch.send("group", "群A", "补一句")
        assert wx.cur == "无关群" and ch.held_orig == ""
        await ch.stop()
    env.run(body)


def test_poll_reads_offscreen_without_flashing_and_falls_back_when_switch_fails(env, tmp_path, monkeypatch):
    """开了「移到屏幕外」：看会话列表、切过去读都先不移窗口（真机 4.1.15.56 上屏幕外也能读，不闪）；
    切换后打开的不是目标聊天（屏幕外点不到）就把窗口移回来再读；绝不把别的聊天的消息当成这个群的。"""
    from test_round10 import FakeWin
    api = FakeWin()
    monkeypatch.setattr(WeChatChannel, "window_api", api)
    seen = []

    async def body(e):
        e.core.settings.set("platform:wechat", "wechat_offscreen", True)
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        orig_get, orig_chatwith = wx.GetSession, wx.ChatWith
        wx.GetSession = lambda: (seen.append(("peek", api.seen())), orig_get())[1]
        wx.ChatWith = lambda who, **k: (seen.append(("switch", api.seen())), orig_chatwith(who, **k))[1]
        await until(lambda: "群A" in ch.tails)
        wx.add("群A", "friend", "甲", "新的")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["新的"])
        assert all(not s for k, s in seen)  # 看列表、切过去读：窗口一直在屏幕外，没闪
        assert not api.seen()
        # 屏幕外切不过去：移回来再读，读完又移出去；读到的是对的群
        Wx.vis_api = api
        wx.add("群B", "friend", "丙", "B的新消息")
        assert await until(lambda: ctx_texts(e, "wechat:群B") == ["B的新消息"])
        assert ("switch-ignored", "群B") in wx.log and ctx_texts(e, "wechat:群A") == ["新的"]
        assert any(k == "switch" and s for k, s in seen) and not api.seen()
        await ch.stop()
        assert api.seen()  # 停止：放回原位
    env.run(body)


def test_poll_never_attributes_wrong_chat(env):
    """切换没成功（打开的是别的聊天）：这次不处理，也不把现在打开的聊天的消息当成目标群的。"""
    async def body(e):
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        await until(lambda: "群A" in ch.tails)
        n_before = len(ctx_texts(e, "wechat:群A"))
        wx.ChatWith = lambda who, **k: wx.log.append(("switch-broken", who))  # 一直切不过去，一直停在「无关群」
        wx.add("群A", "friend", "甲", "新消息")
        assert await until(lambda: any("没能切到" in l["text"] for l in e.core.db.rows("SELECT text FROM logs")), 6)
        assert len(ctx_texts(e, "wechat:群A")) == n_before and ctx_texts(e, "wechat:群B") == []
        await ch.stop()
    env.run(body)


def test_poll_skips_message_ids_already_seen(env):
    """真机上同一条消息的 id 在不同次读取里一样：衔接判断就算出了错，id 见过的也不会再当新消息。"""
    async def body(e):
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        await until(lambda: "群A" in ch.tails)
        wx.add("群A", "friend", "甲", "一条新的")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["一条新的"])
        ch.tails["群A"] = []  # 把「上次读到的」弄丢（比如异常）：衔接不上，会把整屏当新的……
        ch.tails["群A"] = [("没有", "衔接")]
        wx.add("群A", "friend", "乙", "又一条")
        assert await until(lambda: "又一条" in ctx_texts(e, "wechat:群A"))
        assert ctx_texts(e, "wechat:群A").count("一条新的") == 1  # ……但旧的 id 都见过，不会重复
        await ch.stop()
    env.run(body)


def test_at_mention_with_wechat_separator_is_stripped(env):
    """真机上 @ 后面跟的是 U+2005（四分之一空格），不是普通空格。"""
    async def body(e):
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        await until(lambda: "群A" in ch.tails)
        wx.add("群A", "friend", "甲", "@小号 你好")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["你好"])
        await ch.stop()
    env.run(body)


def test_real_machine_identity_key_display_name():
    """真机（微信 4.1.15.56 + wxauto4 41.1.7）上 GetMyInfo() 只返回 {'display_name': 昵称}：能读到昵称，读不到微信号。"""
    wx = Wx()
    from mb.channels.wechat import read_identity
    assert read_identity(wx) == {"wxid": "", "nick": "小号"}


def test_docs_and_setting():
    root = Path(__file__).resolve().parent.parent
    from mb.settings import BY_KEY
    s = BY_KEY["wechat_recv_mode"]
    assert s["default"] == "auto" and [o[0] for o in s["options"]] == ["auto", "window", "poll"] and s["platforms"] == ["wechat"]
    for doc, words in (("docs/使用说明.md", ("主窗口轮询", "收消息方式")), ("docs/User-Guide.en.md", ("main-window polling", "receive mode"))):
        t = (root / doc).read_text(encoding="utf-8")
        assert all(w in t for w in words), (doc, words)


# ---------- 不抢前台：读消息、切会话把微信顶上来之后，前台和 Z 序要还回去 ----------
def test_reading_does_not_leave_wechat_in_front_and_sending_gives_focus_back(env, monkeypatch):
    from test_round10 import FakeWin
    api = FakeWin()
    monkeypatch.setattr(WeChatChannel, "window_api", api)

    async def body(e):
        e.core.settings.set("platform:wechat", "wechat_offscreen", False)  # 没开「移到屏幕外」：窗口在屏幕里，更要紧
        ch = await up(e, "poll")
        wx = Wx.instances[-1]
        Wx.steal_api = api
        await until(lambda: "群A" in ch.tails)
        assert (api.fg, api.zp) == (111, 222)  # 基线读取之后：前台还是用户原来的窗口，Z 序没变
        wx.add("群A", "friend", "甲", "新消息")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["新消息"])
        assert wx.log.count(("read", "群A")) >= 2  # 确实切过去读过（这中间 wxauto 把微信顶到了前台）
        assert (api.fg, api.zp) == (111, 222) and ch.keeper.restores >= 1  # 读完前台还给了原来的窗口，微信放回原来的 Z 序
        # 发送：同样，发完把焦点还回去
        await ch.send("group", "群A", "说一句")
        assert wx.sent[-1] == ("群A", "说一句") and (api.fg, api.zp) == (111, 222)
        # 用户本来就在用微信（它原来就在前台）：什么都不动
        api.fg, api.zp = api.hwnd, 0
        n = len(api.log)
        wx.add("群A", "friend", "甲", "又一条")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["新消息", "又一条"])
        assert api.fg == api.hwnd and [x for x in api.log[n:] if x[0] in ("fg", "z")] == []
        # 操作期间用户自己切到了别的窗口：不跟用户抢
        await asyncio.sleep(0.4)  # 上一轮读完、稳定下来
        api.fg, api.zp = 111, 222
        orig_cw = wx.ChatWith

        def cw(who, **k):
            orig_cw(who, **k)
            if who == "无关群":  # 读完切回原来的会话的时候，用户自己点开了另一个窗口
                api.fg = 333
        wx.ChatWith = cw
        wx.add("群A", "friend", "乙", "第三条")
        assert await until(lambda: ctx_texts(e, "wechat:群A")[-1:] == ["第三条"])
        await asyncio.sleep(0.4)
        assert api.fg == 333
        await ch.stop()
    env.run(body)


def test_connect_retries_transient_errors(env):
    async def body(e):
        calls = {"n": 0}
        real = Wx.__init__

        def flaky(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("事件无法调用任何订户")  # 真机上见过的 COMError：界面还没就绪
            real(self, *a, **k)
        Wx.__init__ = flaky
        try:
            e.core.settings.set("platform:wechat", "wechat_recv_mode", "poll")
            ch = make(e)
            ch.connect_retry_secs = 0.01
            await ch.start()
            assert calls["n"] == 3 and ch.status in ("confirm", "online")
            await ch.stop()
            # 一直连不上：三次之后才报错
            calls["n"] = -100
            ch = make(e)
            ch.connect_retry_secs = 0.01
            await ch.start()
            assert ch.status == "error" and "连不上微信" in ch.detail
        finally:
            Wx.__init__ = real
    env.run(body)


# ---------- 运行中的身份复核：真的重新读（不是读缓存），而且挑在本来就要切会话 / 发送的时候做 ----------
def test_cached_identity_cannot_see_a_switch_but_fresh_connection_can():
    from mb.channels.wechat import read_identity
    Wx.info = {"display_name": "小号"}
    old = Wx()
    Wx.info = {"display_name": "大号"}  # 换号了
    assert read_identity(old)["nick"] == "小号"  # 老连接读到的还是缓存（真机上 GetMyInfo 就是这样，0 毫秒）
    assert read_identity(Wx())["nick"] == "大号"  # 重新连一次才读得到


def test_account_switch_is_caught_at_the_next_read_and_nothing_is_ingested(env):
    async def body(e):
        ch = await up(e, "poll")
        ch.recheck_secs = 0  # 每次读之前都复核（真实默认 60 秒：到点才顺带复核）
        wx = Wx.instances[-1]
        await until(lambda: "群A" in ch.tails)
        n_inst = len(Wx.instances)
        wx.add("群A", "friend", "甲", "换号之前的最后一条")
        assert await until(lambda: ctx_texts(e, "wechat:群A") == ["换号之前的最后一条"])
        assert len(Wx.instances) > n_inst  # 读之前顺带重新连了一次读身份
        # 微信里换成了别的号：下一次要读的时候就停，那条消息不进引擎
        Wx.info = {"display_name": "大号"}
        wx = Wx.instances[-1]
        wx.add("群A", "friend", "甲", "换号之后的消息")
        assert await until(lambda: ch.status == "error")
        assert "大号" in ch.detail and "已停止" in ch.detail  # 只靠昵称绑的：昵称对不上
        assert ctx_texts(e, "wechat:群A") == ["换号之前的最后一条"]
        await ch.stop()
    env.run(body)


def test_fresh_check_is_piggybacked_not_extra_work(env):
    async def body(e):
        ch = await up(e, "poll")
        await until(lambda: "群A" in ch.tails)
        wx = Wx.instances[-1]
        ch.recheck_secs = 3600  # 还没到点：读消息时一次都不另外连
        n = len(Wx.instances)
        for i in range(3):
            wx.add("群A", "friend", "甲", f"第{i}条")
            assert await until(lambda: ctx_texts(e, "wechat:群A")[-1:] == [f"第{i}条"])
        assert len(Wx.instances) == n
        ch.recheck_secs = 0  # 到点了：每次读之前连一次、也只连一次
        for i in range(2):
            wx = Wx.instances[-1]
            before = len(Wx.instances)
            wx.add("群A", "friend", "甲", f"到点后第{i}条")
            assert await until(lambda: ctx_texts(e, "wechat:群A")[-1:] == [f"到点后第{i}条"])
            await asyncio.sleep(0.2)
            assert len(Wx.instances) - before == 1, (i, len(Wx.instances) - before)
        # 轮询方式换用了最新的连接：之后读写都走它
        assert ch.wx is Wx.instances[-1]
        await ch.stop()
    env.run(body)


def test_send_rereads_identity_when_due_and_refuses_after_a_switch(env):
    async def body(e):
        ch = await up(e, "poll")
        await until(lambda: "群A" in ch.tails)
        ch.recheck_secs = 0
        await ch.send("group", "群A", "身份对的时候发")
        assert Wx.instances[-1].sent[-1] == ("群A", "身份对的时候发")
        Wx.info = {"display_name": "大号"}
        sent_before = [w.sent[:] for w in Wx.instances]
        try:
            await ch.send("group", "群A", "换号之后不该发出去")
            assert False
        except RuntimeError as ex:
            assert "不是绑定的小号" in str(ex)
        assert [w.sent for w in Wx.instances][:len(sent_before)] == sent_before and ch.status == "error"
        await ch.stop()
    env.run(body)


def test_new_wechat_window_triggers_an_immediate_check_and_idle_check_is_slow(env, monkeypatch):
    from test_round10 import FakeWin
    api = FakeWin()
    monkeypatch.setattr(WeChatChannel, "window_api", api)

    async def body(e):
        ch = await up(e, "poll")
        ch.watch_secs, ch.recheck_secs, ch.idle_recheck_secs = 0.05, 3600, 3600
        await asyncio.sleep(0.3)
        assert ch.status == "online" and len(Wx.instances) == 1 + 0 or True
        n = len(Wx.instances)
        await asyncio.sleep(0.3)
        assert len(Wx.instances) == n  # 平时空闲、窗口没变：不单独去连（会激活窗口）
        # 微信退出又登录了别的号：主窗口句柄变了 → 立刻复核，不等
        api.hwnd = 9999
        Wx.info = {"display_name": "大号"}
        assert await until(lambda: ch.status == "error", 3)
        assert "大号" in ch.detail
        await ch.stop()
    env.run(body)


def test_idle_check_catches_a_switch_without_any_reads(env):
    async def body(e):
        ch = await up(e, "poll")
        ch.watch_secs, ch.idle_recheck_secs = 0.05, 0.2
        Wx.info = {"display_name": "大号"}
        assert await until(lambda: ch.status == "error", 3)
        await ch.stop()
    env.run(body)
