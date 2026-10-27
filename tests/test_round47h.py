"""补四十七之八：微信发消息只发给精确对上的那个会话，对不上就不发（通知绝不落到群里）。

一次事故：QQ 小号被踢下线，两条「【机器人通知】QQ … 被踢下线……」写的是 via: wechat，却发进了群
「示例群」。原因是主窗口方式的 send() 直接 SendMsg(body, who=备注)，而 wxauto4 的 SendMsg 默认按名字
模糊搜索（exact 默认 False）；用户卡上第一个秘密备注（半角 #）和小号里实际的备注（全角 ＃）对不上，模糊搜索的第一名
是不该收到通知的那个群。现在：

  一、微信上每一次往指定会话发东西（send 的普通发言 / 分条 / @ / 引用回复、send_image 的自定义表情）都先确认
      「现在开着的就是它」，确认不了就不发：
        · 主窗口方式：先 ChatWith 精确切换（exact 默认 True，不传 force），再用 ChatInfo() 核对 chat_name 必须正好
          等于目标名字；发的时候不再把 who 交给 SendMsg 去模糊搜索（要传就同时 exact=True）。私聊还要确认开着的不是群。
        · 独立窗口方式：子窗口本来就绑着那个会话，照旧（不看主窗口的 ChatInfo）。
        · 核对不上：不发，记一条 warn（要发给谁、实际开着的是谁），抛现有的「没发」那类错。
        · 引用回复：先切过去核对，核对不上就不在那个会话里找消息，按「找不到要引用的那条」处理。
        · 发完切回原来的会话，做法不变。
  二、出事通知：用户卡上这个平台的账号挨个试，第 1 条核对通过才发，发出去就停；都对不上就换 NOTIFY_ORDER 里下一个
      平台；都不行就只在面板留「未读通知」并记一条 warn。绝不发到任何群。面板通知记录里的 via 写实际发出去的平台，
      一个都没发出去就留空。
  三、验证命令 --test-wechat-picture 加一步 exact_switch：只切换、不发东西，查清精确切换在真机上的表现（每个备注
      试一次、再试一个肯定不存在的名字，记下 ChatInfo() 全部字段），出错不算整个命令失败。

全部用桩：假的 wxauto、假的消息、假的窗口接口，不碰真的微信、不操作任何真窗口、不截屏。
"""
import asyncio
from types import SimpleNamespace

import pytest

from test_round35 import logs, setg
from test_round41 import wx_channel
from test_round47a import FakeWeChat, result_of
from wxworld import World, fp

from mb import wxpicture, wxsticker
from mb.channels.wechat import WeChatChannel


# ---------- 桩 ----------
class WxStub:
    """主窗口方式的 wxauto 桩。ChatWith 真的切（ChatInfo 跟着变）；SendMsg 记下每一次调用，
    把 who / exact 一并记下来（测试要证明发送时不再把 who 交给它去模糊搜索）。"""

    def __init__(self, cur="原来的会话", msgs=None, names=None, raises=None, stuck=None, chat_type=None):
        self.cur, self.msgs = cur, list(msgs or [])
        self.names = None if names is None else set(names) | {cur}  # 能切到的会话（None = 什么名字都能切到；原来的会话总在）
        self.raises = set(raises or ())  # 切这些名字会报错
        self.stuck = stuck  # 切谁都会停在这个会话（真机上「精确对不上时留在原会话」的情形）
        self.chat_type = chat_type or {}  # 会话名 → ChatInfo 里的 chat_type
        self.sends: list[dict] = []
        self.switches: list[str] = []

    def ChatInfo(self):
        info = {"chat_name": self.cur}
        if self.cur in self.chat_type:
            info["chat_type"] = self.chat_type[self.cur]
        return info

    def ChatWith(self, who, **k):
        self.switches.append(who)
        if who in self.raises:
            raise RuntimeError(f"找不到「{who}」")
        if self.stuck is not None:
            self.cur = self.stuck
            return
        if self.names is None or who in self.names:
            self.cur = who

    def GetAllMessage(self):
        return list(self.msgs)

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        self.sends.append({"msg": msg, "who": who, "exact": exact, "at": at, "chat": self.cur})
        return SimpleNamespace(is_success=True)


def wx(e, stub=None):
    """已上线的微信渠道桩：微信线程跑在测试自己的事件循环里，不碰真界面。"""
    ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
    ch.wx = stub or WxStub()
    ch._com = lambda: None
    ch._verify_if_due = lambda: None
    ch.switch_sleep = lambda _s: None
    return ch


def only_sent_to(stub, chat):
    """每一次 SendMsg 都发给 chat：没有把 who 交给它搜，或者传了 who 就同时 exact=True。"""
    for s in stub.sends:
        assert s["chat"] == chat, s
        assert s["who"] is None or s["exact"] is True, s


# ---------- 一、这次的事故：通知第一个备注对不上、开着的是群 ----------
NOTIFY_TEXT = "QQ 123456789 被踢下线"  # 测试用的假 QQ 号（别把真号写进仓库）


def put_user(e, **kw):
    card = e.core.db.doc("person", "pe_user")
    e.core.db.put("person", {**card, "name": "用户", "self": True, **kw})
    e.engine.reload()


def test_the_incident_the_first_remark_lands_on_a_group_and_the_second_one_gets_the_dm(env):
    """第一个备注（半角 #）精确对不上、切完开着的还是那个群：绝不发到群里；接着试第二个备注，对上了就私聊发出。"""
    async def body(e):
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        # 全角那个备注才是真的；半角那个切不过去，界面停在群「示例群」上
        stub = WxStub(cur="示例群", names=["示例备注＃001"],
                      chat_type={"示例群": "group", "示例备注＃001": "private"})
        ch = WeChatChannel(e.engine, acc)
        ch.wx, ch.status, ch.loop = stub, "online", asyncio.get_running_loop()
        ch._com = lambda: None
        ch._verify_if_due = lambda: None
        ch.switch_sleep = lambda _s: None
        e.engine.add_bot(acc, ch)
        put_user(e, wechat_remark=["示例备注#001", "示例备注＃001"])

        e.engine.notify(NOTIFY_TEXT, e.core.db.doc("account", "ac_900"))
        await e.engine.idle()

        assert [s["msg"] for s in stub.sends] == [f"【机器人通知】{NOTIFY_TEXT}"]  # 只发出去了一条
        assert stub.sends[0]["chat"] == "示例备注＃001"  # 第二次核对通过才发，发在私聊上
        assert stub.sends[0]["who"] is None and stub.sends[0]["exact"] is False  # 没把 who 交给它模糊搜
        notes = e.core.db.kv("notices")
        assert len(notes) == 1 and notes[0]["via"] == "wechat"  # 真发出去了才写 via
        assert logs(e, "对不上，没有发") and "示例备注#001" in "\n".join(logs(e, "对不上，没有发"))
        assert not any(s["chat"] == "示例群" for s in stub.sends)  # 一条都没进群
    env.run(body)


def test_no_remark_matches_leaves_only_the_panel_notice_with_an_empty_via(env):
    async def body(e):
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        stub = WxStub(cur="示例群", names=[], chat_type={"示例群": "group"})
        ch = WeChatChannel(e.engine, acc)
        ch.wx, ch.status, ch.loop = stub, "online", asyncio.get_running_loop()
        ch._com = lambda: None
        ch._verify_if_due = lambda: None
        ch.switch_sleep = lambda _s: None
        e.engine.add_bot(acc, ch)
        put_user(e, wechat_remark=["示例备注#001", "示例备注＃001"])

        e.engine.notify(NOTIFY_TEXT)
        await e.engine.idle()
        assert stub.sends == []  # 一条都没发
        notes = e.core.db.kv("notices")
        assert len(notes) == 1 and notes[0]["via"] == "" and "被踢下线" in notes[0]["text"]
        warned = logs(e, "通知没能私聊到用户")
        assert warned and "示例备注#001" in warned[0] and "示例备注＃001" in warned[0]  # 试过哪些
    env.run(body)


def test_when_wechat_cannot_be_reached_the_next_account_in_the_notify_order_is_tried(env):
    async def body(e):
        from conftest import FakeChannel
        qq = e.add_bot("900")
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        ch = WeChatChannel(e.engine, acc)
        ch.wx, ch.status, ch.loop = WxStub(cur="某个群", names=[]), "online", asyncio.get_running_loop()
        ch._com = lambda: None
        ch._verify_if_due = lambda: None
        ch.switch_sleep = lambda _s: None
        e.engine.add_bot(acc, ch)
        put_user(e, qq=["12345"], wechat_remark=["对不上的备注"])
        e.engine.notify("微信掉线了", acc)  # 出事的是这个微信账号
        await e.engine.idle()
        assert [x[2] for x in qq.sent] == ["【机器人通知】微信掉线了"]  # 微信不行：换 NOTIFY_ORDER 里下一个
        assert qq.sent[0][0] == "private" and qq.sent[0][1] == "12345"
        assert ch.wx.sends == []  # 微信上一次都没发成
        assert e.core.db.kv("notices")[0]["via"] == "qq"  # via 写实际发出去的平台
    env.run(body)


# ---------- 二、主窗口方式群发：切过去核对通过才发 ----------
def test_a_group_message_is_sent_only_after_the_exact_switch_was_checked(env):
    async def body(e):
        stub = WxStub(cur="原来的会话", names=["群A"])
        ch = wx(e, stub)
        await ch.send("group", "群A", "我回一句")
        assert stub.switches == ["群A", "原来的会话"] and stub.cur == "原来的会话"  # 先精确切到「群A」，发完切回原来的会话
        only_sent_to(stub, "群A")
        assert [s["msg"] for s in stub.sends] == ["我回一句"]
        assert stub.sends[0]["chat"] == "群A"  # 发的时候开着的就是「群A」
    env.run(body)


@pytest.mark.parametrize("stub_kw", [
    {"names": ["群A（2）"]},  # 名字只是相近：切不过去，留在原会话
    {"stuck": "群A（2）"},  # 精确对不上，微信留在了名字相近的那个会话
    {"raises": ["群A"]},  # 切过去报错
])
def test_a_group_message_is_not_sent_when_the_switch_cannot_be_checked(env, stub_kw):
    async def body(e):
        stub = WxStub(cur="原来的会话", **stub_kw)
        ch = wx(e, stub)
        with pytest.raises(RuntimeError) as ex:
            await ch.send("group", "群A", "我回一句")
        assert "没有发" in str(ex.value)
        assert stub.sends == []  # 一条都没发
        line = "\n".join(logs(e, "对不上，没有发"))
        assert "群A" in line and ("群A（2）" in line or "原来的会话" in line)  # warn 里有目标和实际会话
    env.run(body)


def test_a_private_message_into_a_chat_info_group_is_not_sent(env):
    """私聊发到一个 chat_type == "group" 的会话：不发（名字对上了也要拦）。"""
    async def body(e):
        stub = WxStub(cur="原来的会话", names=["小海"], chat_type={"小海": "group"})
        ch = wx(e, stub)
        with pytest.raises(RuntimeError) as ex:
            await ch.send("private", "小海", "在吗")
        assert "没有发" in str(ex.value) and stub.sends == []
        assert logs(e, "这是个群")
    env.run(body)


def test_a_private_message_without_chat_type_is_checked_by_name_only(env):
    """ChatInfo 没给 chat_type 的版本：只按名字核对。"""
    async def body(e):
        stub = WxStub(cur="原来的会话", names=["小海"])  # 没有任何 chat_type
        ch = wx(e, stub)
        await ch.send("private", "小海", "在吗")
        only_sent_to(stub, "小海")
        assert [s["msg"] for s in stub.sends] == ["在吗"]
    env.run(body)


def test_when_who_is_handed_over_at_all_it_is_exact(env):
    """假对象记下的 SendMsg 调用里，要么没有 who，要么 exact=True（这里的桩：一个 who 都没有）。"""
    async def body(e):
        stub = WxStub(cur="原来的会话", names=["群A"])
        ch = wx(e, stub)
        await ch.send("group", "群A", "你好")
        assert stub.sends and all(s["who"] is None for s in stub.sends)
    env.run(body)


# ---------- 三、分条、@、引用回复 ----------
THREE = "第一行\n第二行\n第三行"


def test_every_split_part_and_the_at_go_only_to_the_checked_chat(env):
    async def body(e):
        stub = WxStub(cur="原来的会话", names=["群A"])
        ch = wx(e, stub)
        setg(e, "split_max_parts", 0)
        await e.engine.send_text(e.engine.bots[ch.aid], "group", "群A", THREE, None, None, mention={"老王": "老王"})
        assert len(stub.sends) == 3 and [s["msg"] for s in stub.sends] == ["第一行", "第二行", "第三行"]
        for s in stub.sends:
            assert s["chat"] == "群A" and (s["who"] is None or s["exact"] is True)
        assert stub.cur == "原来的会话"  # 最后一条发完才切回原来的会话，只切一次
        assert stub.switches == ["群A", "原来的会话"]
    env.run(body)


class QuoteMsg:
    """能引用的消息桩。"""

    def __init__(self, mid, sender, content, log):
        self.id, self.sender, self.content, self.attr, self.type = mid, sender, content, "friend", "text"
        self.log = log

    def quote(self, text, at=None):
        self.log.append(("quote", self.id, text, at))
        return SimpleNamespace(is_success=True)


def test_a_quote_reply_switches_and_checks_before_it_even_looks_for_the_message(env):
    async def body(e):
        log = []
        stub = WxStub(cur="原来的会话", names=["群A"], msgs=[QuoteMsg("m1", "老王", "在吗", log)])
        ch = wx(e, stub)
        await ch.send("group", "群A", "回你", reply_to="m1")
        assert ("quote", "m1", "回你", None) in log and stub.sends == []
        assert stub.switches[0] == "群A"  # 先切过去、核对，再在会话里找那条
        assert stub.cur == "原来的会话"
    env.run(body)


def test_a_quote_that_cannot_be_switched_to_is_treated_as_not_found_and_the_plain_send_still_checks(env):
    async def body(e):
        log = []
        stub = WxStub(cur="原来的会话", names=["群A（2）"], msgs=[QuoteMsg("m1", "老王", "在吗", log)])
        ch = wx(e, stub)
        with pytest.raises(RuntimeError):
            await ch.send("group", "群A", "回你", reply_to="m1")
        assert log == [] and stub.sends == []  # 不在那个会话里找消息，也没发
        assert logs(e, "不在那里找消息")
        # 切对得上的时候：找不到那条就按普通方式发，普通发送照样过核对
        log.clear()
        stub2 = WxStub(cur="原来的会话", names=["群A"])
        ch2 = wx(e, stub2)
        await ch2.send("group", "群A", "回你", reply_to="nope")
        assert [s["msg"] for s in stub2.sends] == ["回你"] and stub2.cur == "原来的会话"
        only_sent_to(stub2, "群A")
    env.run(body)


# ---------- 四、发自定义表情 ----------
def sticker_env(w, e):
    """把 World 桩成渠道的表情面板，返回渠道。"""
    ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
    ch.wx = w.wx()
    ch._com = lambda: None
    ch._verify_if_due = lambda: None
    ch.switch_sleep = lambda _s: None
    ch.stk_ui = w.ui()
    ch.stk_hwnd = lambda: 11
    ch.stk_grab = w.grab
    ch.stk_sleep = lambda _s: None
    return ch


def entry_for(w, pos):
    sid, moving = w.items[pos]
    return {"fps": [fp(sid)] + ([fp(sid, 1)] if moving else []), "pos": pos, "ticks": 0}


def test_a_sticker_is_clicked_only_after_the_switch_was_checked(env):
    async def body(e):
        w = World([(f"s{i}", False) for i in range(6)], orig="原来的会话")
        ch = sticker_env(w, e)
        await ch.send_image("group", "群A", b"x", sticker=entry_for(w, 2))
        assert w.sent_index == [2] and w.chatwith == ["群A", "原来的会话"]
    env.run(body)


def test_a_sticker_is_not_clicked_when_the_switch_cannot_be_checked(env):
    async def body(e):
        class Stuck(World):
            """切谁都会留在原来的会话（真机上「精确对不上时留在原会话」的情形）。"""
            def wx(self):
                ww, world = super().wx(), self

                def chatwith(who, **k):
                    world.chatwith.append(who)
                ww.ChatWith = chatwith  # 装在实例上（类里的 __getattr__ 只为不认识的名字兜底）
                return ww
        ws = Stuck([(f"s{i}", False) for i in range(6)], orig="原来的会话")
        ch = sticker_env(ws, e)
        with pytest.raises(wxsticker.PanelError):
            await ch.send_image("group", "群A", b"x", sticker=entry_for(ws, 2))
        assert ws.sent_index == [] and ws.clicks == []  # 一个格子都没点
        assert ws.chatwith[0] == "群A" and ws.chat == "原来的会话"
    env.run(body)


def test_a_sticker_for_a_private_chat_is_not_clicked_when_the_open_chat_is_a_group(env):
    async def body(e):
        class Groupy(World):
            """ChatInfo 说开着的这个会话是群（名字对上了，但私聊不该发进群）。"""
            def wx(self):
                ww = super().wx()
                orig = ww.ChatInfo
                ww.ChatInfo = lambda: {**orig(), "chat_type": "group"}
                return ww
        w = Groupy([(f"s{i}", False) for i in range(6)], orig="原来的会话")
        ch = sticker_env(w, e)
        with pytest.raises(wxsticker.PanelError):
            await ch.send_image("private", "群A", b"x", sticker=entry_for(w, 2))
        assert w.sent_index == [] and w.clicks == []  # 一个格子都没点
    env.run(body)


# ---------- 五、独立窗口方式照旧能发 ----------
def test_the_separate_window_way_still_sends_without_looking_at_the_main_window(env):
    async def body(e):
        sent = []

        class Sub:
            """独立窗口方式的子窗口：本来就绑着那个会话，SendMsg 不认 who。"""
            def SendMsg(self, msg, **k):
                sent.append((msg, k))
                return SimpleNamespace(is_success=True)

            def GetAllMessage(self):
                return []

        stub = WxStub(cur="别的会话")
        ch = wx(e, stub)
        ch.chats = {"群A": Sub()}
        await ch.send("group", "群A", "我回一句")
        assert [m for m, _ in sent] == ["我回一句"] and stub.switches == []  # 没去主窗口切来切去
        assert stub.sends == []
    env.run(body)


# ---------- 六、验证命令的 exact_switch 一节 ----------
class ProbeWeChat(FakeWeChat):
    """验证命令用的微信桩：ChatWith 真的切（不存在的名字留在原会话）；记住试过哪些名字；ChatInfo 给 chat_type。"""

    def __init__(self, tmp, known=(), stuck_missing=False, boom_missing=False):
        super().__init__(tmp)
        self.known = set(known)  # 能切到的会话（都当私聊）
        self.group_chats = {"某个群"}  # 这几个是群：ChatInfo 的 chat_type 返回 group
        self.stuck_missing = stuck_missing  # 不存在的名字：留在原会话
        self.boom_missing = boom_missing  # 不存在的名字：抛错

    def ChatInfo(self):
        return {"chat_name": self.chat, "chat_type": "group" if self.chat in self.group_chats else "private"}

    def ChatWith(self, who, **k):
        self.chats.append(who)
        if who in self.known or who in ("文件传输助手", "某个群"):
            self.chat = who
            return
        if self.boom_missing:
            raise RuntimeError("找不到这个会话")
        return  # 默认和真机上一样：精确对不上就留在原会话


def run_picture(out, w, remarks, monkeypatch):
    monkeypatch.setattr(wxpicture, "user_wechat_remarks", lambda: list(remarks))
    from test_round47d import FakeApi, MAIN_HWND, grab_shot, win, wx_windows
    api = FakeApi(owner=MAIN_HWND, windows=wx_windows([win(MAIN_HWND)]))
    code = wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=api, grab=grab_shot(),
                              hwnd_finder=lambda: MAIN_HWND)
    return code, result_of(out)


def test_the_command_records_every_remark_and_the_missing_name_in_exact_switch(tmp_path, monkeypatch):
    out = tmp_path / "out"
    w = ProbeWeChat(tmp_path, known={"某个群", "示例备注#001"})
    code, res = run_picture(out, w, ["示例备注#001", "示例备注＃001"], monkeypatch)
    assert code == 0 and res["ok"] is True
    sec = res["exact_switch"]
    assert sec["skip"] == "" and len(sec["remarks"]) == 2
    assert sec["remarks"][0]["matched"] is True and sec["remarks"][0]["chat_type"] == "private"  # 私聊在真机上返回什么：照实记
    # 全角那个备注精确对不上：真机上切不过去，界面停在刚才开着的那个（第一个备注的私聊）
    assert sec["remarks"][1]["matched"] is False and sec["remarks"][1]["opened"] == "示例备注#001"
    assert sec["remarks"][1]["info"]["chat_name"] == "示例备注#001"
    for row in sec["remarks"] + [sec["missing"]]:
        assert {"want", "chat_with", "opened", "chat_type", "info", "matched"} <= set(row)
        assert row["info"] and "chat_name" in row["info"]  # ChatInfo() 的全部字段
    assert sec["missing"]["kind"] == "missing" and sec["missing"]["want"].startswith("PodMates-不存在的会话-")
    assert sec["missing"]["matched"] is False
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert log.count("精确切到「") >= 3 and "不存在的会话" in log and "只切换、读 ChatInfo()" in log
    assert res["exact_switch"]["missing"]["opened"]  # 记下之后 ChatInfo() 读到的是谁


@pytest.mark.parametrize("kw", [{"stuck_missing": True}, {"boom_missing": True}])
def test_the_missing_name_is_recorded_whether_it_errors_or_stays(tmp_path, monkeypatch, kw):
    out = tmp_path / "out"
    w = ProbeWeChat(tmp_path, known={"某个群"}, **kw)
    code, res = run_picture(out, w, ["某个群"], monkeypatch)
    assert code == 0 and res["ok"] is True  # 这一步出错不算整个命令失败
    missing = res["exact_switch"]["missing"]
    assert missing["matched"] is False
    if kw.get("boom_missing"):
        assert "报错" in missing["chat_with"]
    else:
        assert missing["opened"] == "某个群" and "返回了" in missing["chat_with"]


def test_exact_switch_never_sends_anything(tmp_path):
    out = tmp_path / "out"

    class Spy(ProbeWeChat):
        def SendMsg(self, *a, **k):
            raise AssertionError("精确切换这一步不许发消息")

    w = Spy(tmp_path, known={"某个群", "某个群2"})
    ch = wxpicture.WxForPicture(w, [])
    assert wxpicture.exact_switch(ch, ["某个群", "某个群2"], lambda t: None, sleep=lambda s: None)
    assert w.sent == []  # 一条都没发
    assert not any(name in ("SendMsg", "SendFiles") for name, _ in ch._calls)


def test_an_error_in_exact_switch_is_recorded_and_does_not_fail_the_command(tmp_path, monkeypatch):
    out = tmp_path / "out"

    class Boom(ProbeWeChat):
        """切到某个备注时 ChatInfo 读不出来：这一行照实记，命令照走完。"""
        def ChatInfo(self):
            if self.chat == "某个群":
                raise RuntimeError("界面忙")
            return super().ChatInfo()

    w = Boom(tmp_path, known={"某个群"})
    code, res = run_picture(out, w, ["某个群"], monkeypatch)
    assert code == 0 and res["ok"] is True
    sec = res["exact_switch"]
    assert sec["remarks"] and any("界面忙" in row.get("why", "") for row in sec["remarks"])
    assert (out / "result.json").exists()


def test_the_command_without_any_remark_says_it_skipped(tmp_path, monkeypatch):
    out = tmp_path / "out"
    w = ProbeWeChat(tmp_path, known={"某个群"})
    code, res = run_picture(out, w, [], monkeypatch)
    assert code == 0 and "秘密备注" in res["exact_switch"]["skip"]


# ---------- 七、说明 ----------
def test_the_guides_say_notifications_never_reach_a_group_and_messages_are_checked_first():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cn = (root / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (root / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    # 出事通知那一段（原样用令里的中文）
    assert ("通知只私聊你本人：按人物卡上你在这个平台的账号挨个试，微信上要切过去后确认打开的正好是你那个私聊才发，"
            "对不上就换下一个；都发不出去就只留在面板的「未读通知」里，绝不会发到群里") in cn
    assert ("Notifications only ever go to you in private: the accounts on your person card for that platform are tried"
            " one after another, and on WeChat the message is sent only after switching and confirming that the chat now"
            " open is exactly your private chat") in en
    # 微信收发那一段
    assert "往群里或私聊发消息前，都先确认打开的正好是那个会话，对不上就不发，日志里记一笔" in cn
    assert "Before sending anything into a group or a private chat, the program first confirms that the chat now open is exactly the right one" in en
    # 验证命令那一段写了新加的一步和 exact_switch 一节
    assert "exact_switch" in cn and "只切换、不发送" in cn and "PodMates-不存在的会话-" in cn
    assert "exact_switch" in en and "only switches and never sends" in en and "PodMates-不存在的会话-" in en


# ---------- Planner 补：微信线程里连续读不到界面时的通知交回事件循环线程去发 ----------
def test_the_ui_failure_notice_raised_on_the_wechat_thread_is_sent_from_the_event_loop():
    """复核身份在微信线程里跑，那里没有事件循环：以前 _ui_trouble 直接调 engine.notify，notify 里的 spawn
    （asyncio.create_task）在那个线程里会抛 RuntimeError，私聊通知发不出去。现在交回事件循环线程去发。"""
    import threading
    from mb.channels.wechat import UI_FAIL_MAX

    async def body():
        loop = asyncio.get_running_loop()
        seen = []
        eng = SimpleNamespace(log=lambda *a: None,
                              notify=lambda text, acc: seen.append((text, threading.current_thread() is threading.main_thread())))
        ch = SimpleNamespace(engine=eng, ui_fails=UI_FAIL_MAX - 1, status="online", detail="", account={"id": "w"}, loop=loop)
        ch.set_status = lambda st, d: (setattr(ch, "status", st), setattr(ch, "detail", d))
        errs = []

        def on_wechat_thread():
            try:
                WeChatChannel._ui_trouble(ch, "微信：复核登录身份时没连上（x），稍后再试")
            except Exception as ex:  # 以前在这里抛 RuntimeError
                errs.append(ex)
        t = threading.Thread(target=on_wechat_thread, name="wechat-test")
        t.start()
        await loop.run_in_executor(None, t.join)
        await asyncio.sleep(0)
        assert not errs
        assert ch.status == "error"
        assert len(seen) == 1 and "读不到微信界面" in seen[0][0] and seen[0][1]  # 在事件循环（主）线程里发
    asyncio.run(body())

