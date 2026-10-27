"""补四十七之七：PodMates 自己读完微信消息之后，对微信自己弹出的头像小菜单（「@某某／拍一拍」）发一次 Esc。

2026-10-27 job61 在服务器上查明：菜单是 wxauto4 的 GetAllMessage() 自己打开的（读 sender、sender_remark 都不会再开），
只对这个菜单的句柄发一次 Esc 就关掉了；以前只在截表情之前关，所以平时菜单一直开着。现在：
  · PodMates 自己调 GetAllMessage() 的每一处，读完之后马上做一次（轮询读消息 / 引用回复前找消息 / 发自定义表情前后读最后几条 /
    独立窗口方式的 _callback 处理完这一条之后）；
  · 只对菜单发 Esc（不发 WM_CLOSE，每轮硬关可能让微信以后弹不出这个菜单）；表情面板开着时不做；
  · 关不掉的留着、同一个句柄这次运行只记一条 warn；发完主窗口看不见了就记 warn 并 unhide()（不抢前台焦点）；
  · 这一步出任何错都只记日志（同一种错这次运行只记一次），不影响读消息、发消息、截表情。
  · 验证命令 --test-wechat-picture 的第 3 步改走这个函数；表情那一步记下点面板前后两次窗口列表。
全部用桩：假的窗口接口（发过什么、发完之后窗口还在不在、主窗口看不看得见），假的微信对象、假消息，不碰真的微信、不操作任何窗口。
"""
import types
from types import SimpleNamespace

import pytest

from test_round35 import logs
from test_round41 import ctx_texts, setg, wx_channel, wx_feed
from test_round47a import FakeWeChat, fakes, result_of  # noqa: F401  (fakes 是自动生效的桩)
from test_round47d import (CHAT_HWND, MAIN_HWND, MENU_CLS, MENU_HWND, POPOVER_HWND, Ctl, FakeApi,  # noqa: F401
                           grab_shot, message_control, shot_env, win, wx_windows)
from test_round47b import FriendEmotionMessage
from test_round47f import MenuWeChat, menu_api  # noqa: F401

from mb import wxshot
from mb.channels.wechat import WeChatChannel


@pytest.fixture(autouse=True)
def clean_trouble():
    wxshot.forget_trouble()
    yield
    wxshot.forget_trouble()


class Msg:
    """一条别人发的消息（有控件、能往上找到所在的窗口）。"""

    def __init__(self, mid="m1", hwnd=MAIN_HWND, sender="老王", content="在吗"):
        self.id, self.attr, self.type, self.sender, self.content = mid, "friend", "text", sender, content
        self.sender_remark, self.control = sender, message_control(hwnd=hwnd)


class PollWx:
    """主窗口轮询用的 wxauto 桩：ChatWith / ChatInfo / GetAllMessage / SendMsg。读消息时把菜单打开（和真机一样）。"""

    def __init__(self, api, msgs=None):
        self.api, self.msgs, self.cur, self.reads, self.sent = api, list(msgs or []), "某个群", [], []
        self.api.visible[MENU_HWND] = False  # 一开始没有菜单

    def ChatInfo(self):
        return {"chat_name": self.cur}

    def ChatWith(self, who, **k):
        self.cur = who

    def GetAllMessage(self):
        self.reads.append(self.cur)
        self.api.visible[MENU_HWND] = True  # wxauto4 读一次消息，微信就在最新一条别人的消息头像旁边弹出菜单
        return list(self.msgs)

    def SendMsg(self, msg, who=None, **k):
        self.sent.append((who, msg))
        return SimpleNamespace(is_success=True)


def poll_api(menu_at_start=False, close_ok=True):
    """主窗口方式：主窗口 + 一个菜单（盖在聊天区上）。close_ok 决定 Esc 关不关得掉。"""
    api = FakeApi(owner=MAIN_HWND, windows=wx_windows([win(MENU_HWND)]))
    api.visible.update({MENU_HWND: menu_at_start, POPOVER_HWND: True})
    if close_ok:
        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False
        api.post_key = post_key
    return api


def channel(e, api, wx=None, mode="poll", conf=None):
    """已上线的微信渠道桩：窗口接口换成假的（不会碰真窗口）。"""
    ch = wx_channel(e, conf or {"群A": {"name": "群A", "type": "group"}})
    ch.window_api = api
    ch.mode = mode
    ch._com = lambda: None
    ch._verify_if_due = lambda: None
    ch.stk_sleep = lambda _s: None
    e.engine.register_chat(ch.aid, "wechat", "群A", "群A")  # 和上线时一样登记群，引擎才会把消息放进上下文
    if wx is not None:
        ch.wx = wx
    return ch


def texts(e, who="群A"):
    return [x.text for x in e.engine.rooms[f"wechat:{who}"].ctx]


async def settle(e, want, who="群A", secs=2.0):
    """轮询方式交给引擎的消息是异步处理的：等它进上下文。"""
    import asyncio
    for _ in range(int(secs / 0.02)):
        if texts(e, who) == want:
            return True
        await asyncio.sleep(0.02)
    return texts(e, who) == want


# ---------- 一、轮询读完一个聊天以后 ----------
def test_after_reading_a_chat_only_the_menu_gets_esc_and_never_wm_close(env):
    async def body(e):
        api = poll_api()
        wx = PollWx(api, [Msg("m1"), Msg("m2", sender="示例甲", content="在")])
        ch = channel(e, api, wx)
        ch._read_chat("群A")  # 第一次读：只记下现有的，不当新消息
        assert wx.reads == ["群A"]
        assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]  # 只对菜单发了 Esc，没发 WM_CLOSE
        assert MAIN_HWND not in {p[0] for p in api.posts} and CHAT_HWND not in {p[0] for p in api.posts}
        assert api.visible[MENU_HWND] is False

        api.posts.clear()
        wx.msgs.append(Msg("m3", content="新消息"))
        assert ch._read_chat("群A") == 1
        assert await settle(e, ["新消息"])  # 读到的消息照常交给引擎
        assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]
    env.run(body)


def test_no_menu_means_nothing_is_posted(env):
    async def body(e):
        api = poll_api()
        wx = PollWx(api)
        wx.GetAllMessage = lambda: []  # 读了消息，但微信没弹菜单（私聊、没有别人的消息）
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        assert api.posts == []
    env.run(body)


def test_a_menu_that_does_not_close_stays_and_is_warned_once_per_handle(env):
    async def body(e):
        api = poll_api(close_ok=False)  # Esc 关不掉
        wx = PollWx(api, [Msg()])
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        ch._read_chat("群A")
        ch._read_chat("群A")
        lines = [x for x in logs(e, "关不掉") if str(MENU_HWND) in x]
        assert len(lines) == 1  # 同一个句柄这次运行只记一条
        assert MENU_CLS in lines[0] and "540,250,750,374" in lines[0]  # 句柄、类名、矩形都写上
        assert [p[1] for p in api.posts] == ["key"] * 3  # 每轮照旧只发 Esc，没发 WM_CLOSE
        assert api.visible[MENU_HWND] is True  # 留着不管
    env.run(body)


def test_only_menu_windows_are_posted_to_never_chat_windows_or_far_small_windows(env):
    async def body(e):
        far = win(99, MENU_CLS, "Weixin", (1500, 800, 1560, 860))  # 和主窗口不重叠的小窗
        api = FakeApi(owner=MAIN_HWND, windows=wx_windows([win(MENU_HWND), far]))
        api.visible.update({MENU_HWND: True, 99: True})
        wx = PollWx(api, [Msg()])
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        assert {p[0] for p in api.posts} == {MENU_HWND}
        assert 99 not in {p[0] for p in api.posts} and MAIN_HWND not in {p[0] for p in api.posts}
        assert CHAT_HWND not in {p[0] for p in api.posts}
    env.run(body)


def test_the_emoji_panel_being_open_means_nothing_is_posted(env):
    async def body(e):
        api = FakeApi(owner=MAIN_HWND, windows=wx_windows([win(MENU_HWND),
                                                           win(POPOVER_HWND, "mmui::EmoticonPopover", "Weixin", (74, 185, 588, 699))]))
        api.visible.update({MENU_HWND: True, POPOVER_HWND: True})
        wx = PollWx(api, [Msg()])
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        assert api.posts == []  # 表情面板开着：一个小窗都不碰
    env.run(body)


def test_the_main_window_hidden_by_the_esc_is_warned_about_and_unhidden(env):
    async def body(e):
        api = poll_api()

        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False
            api.visible[MAIN_HWND] = False  # 这一下把主窗口藏进了托盘（2026-10-27 出过事）
        api.post_key = post_key
        shown = []

        def show_quietly(hwnd):
            shown.append(hwnd)
            api.visible[hwnd] = True
        api.show_quietly = show_quietly
        wx = PollWx(api, [Msg()])
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        assert shown == [MAIN_HWND]  # 用现有 unhide() 显示出来（不抢前台焦点）
        assert logs(e, "对菜单发 Esc 之后主窗口看不见了")
        assert api.visible[MAIN_HWND] is True
    env.run(body)


def test_no_message_window_means_this_time_nothing_is_done_and_no_error(env):
    async def body(e):
        api = poll_api()
        api.find_main = lambda **k: 0  # 拿不到主窗口（也就拿不到消息所在的窗口）
        wx = PollWx(api, [Msg()])
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        assert api.posts == []  # 不退回「微信进程的都算」
    env.run(body)


def test_an_error_in_this_step_is_logged_once_and_reading_still_works(env):
    async def body(e):
        api = poll_api()

        def boom():
            raise RuntimeError("列窗口出错")
        wx = PollWx(api, [Msg("m1")])
        ch = channel(e, api, wx)
        api.windows = boom
        ch._read_chat("群A")
        wx.msgs.append(Msg("m2", content="照常收到"))
        ch._read_chat("群A")
        assert await settle(e, ["照常收到"])  # 读消息照常
        assert len(logs(e, "列窗口出错")) == 1  # 同一种错只记一次
        # 发送照常
        await ch.send("group", "群A", "我回一句")
        assert wx.sent == [(None, "我回一句")]  # 发时不传 who（发前已经切过去核对过）
    env.run(body)


# ---------- 二、其他调用处 ----------
def test_finding_the_quoted_message_in_the_main_window_also_closes_the_menu(env):
    async def body(e):
        api = poll_api()
        wx = PollWx(api)
        ch = channel(e, api, wx)
        wx.GetAllMessage = lambda: (api.visible.__setitem__(MENU_HWND, True), [])[1]  # 读一次就弹菜单
        assert ch._find_message(None, "群A", "nope") is None
        assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]
    env.run(body)


def test_finding_the_quoted_message_in_a_separate_chat_window_closes_the_menu_of_that_window(env):
    async def body(e):
        api = FakeApi(owner=CHAT_HWND, windows=wx_windows([win(MENU_HWND, rect=(0, 250, 210, 374))], owner=CHAT_HWND))
        api.visible.update({MENU_HWND: True, POPOVER_HWND: True})

        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False
        api.post_key = post_key
        chat = SimpleNamespace(GetAllMessage=lambda: [Msg("m1", hwnd=CHAT_HWND)],
                               SendMsg=lambda t, **k: SimpleNamespace(is_success=True))
        ch = channel(e, api, SimpleNamespace(), mode="window")
        ch.chats = {"群A": chat}
        ch._find_message(chat, "群A", "nope")
        assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]  # 消息在独立窗口里：菜单和那个窗口重叠
        assert MAIN_HWND not in {p[0] for p in api.posts}
    env.run(body)


def _sticker_driver(api):
    from mb.wxsticker import WxStickers
    return WxStickers(SimpleNamespace(), lambda: MAIN_HWND, grab=grab_shot(), sleep=lambda s: None, log=lambda t: None, api=api)


def test_reading_the_tail_before_and_after_sending_a_sticker_closes_the_menu():
    api = poll_api()
    api.visible[MENU_HWND] = True

    def post_key(hwnd, vk):
        api.posts.append((hwnd, "key", vk))
        api.visible[hwnd] = False
    api.post_key = post_key
    d = _sticker_driver(api)
    wx = SimpleNamespace(GetAllMessage=lambda: [Msg("m1")])
    d._tail(wx, api)
    assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]


def test_the_tail_step_does_nothing_without_a_window_api():
    """没有窗口接口（真跑时渠道没挂上、测试里就是这么用的）：这一步不做，也不报错。"""
    api = poll_api()
    api.visible[MENU_HWND] = True
    d = _sticker_driver(None)
    wx = SimpleNamespace(GetAllMessage=lambda: [Msg("m1")])
    assert len(d._tail(wx)) == 1 and api.posts == []


def test_the_listener_callback_closes_the_menu_after_handling_the_message(env):
    async def body(e):
        api = FakeApi(owner=CHAT_HWND, windows=wx_windows([win(MENU_HWND, rect=(0, 250, 210, 374))], owner=CHAT_HWND))
        api.visible.update({MENU_HWND: True, POPOVER_HWND: True})

        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False
        api.post_key = post_key
        ch = channel(e, api, SimpleNamespace(), mode="window")
        m = Msg("m1", hwnd=CHAT_HWND)
        import threading
        t = threading.Thread(target=ch._callback, args=(m, SimpleNamespace(who="群A")))
        t.start()
        t.join(5)
        assert await settle(e, ["在吗"])  # 处理完这一条
        assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]  # 之后对菜单发了一次 Esc
    env.run(body)


# ---------- 三、截表情之前那套没变 ----------
def test_capture_still_closes_menus_with_esc_then_close():
    """截表情之前那一套（先 Esc，关不掉再 WM_CLOSE）没变。"""
    face = Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))
    api = FakeApi(windows=wx_windows([win(MENU_HWND)]))
    api.visible[MENU_HWND] = True

    def post_close(hwnd):
        api.posts.append((hwnd, "close"))
        api.visible[hwnd] = False
    api.post_close = post_close
    shot = wxshot.capture(message_control([face]), api, grab_shot(), api.mons)
    assert [(p[0], p[1]) for p in api.posts] == [(MENU_HWND, "key"), (MENU_HWND, "close")]
    assert shot.checks["menus"][0]["how"] == "close"


# ---------- 四、验证命令 ----------
def test_the_command_step_three_goes_through_the_new_function_and_reports_it(tmp_path):
    out = tmp_path / "out"
    api = menu_api()
    w = MenuWeChat(tmp_path)
    assert wxpicture_run(out, w, api) == 0
    res = result_of(out)
    menu = res["menu"]
    assert [s["n"] for s in menu["steps"]] == [1, 2, 3, 4, 5, 6]
    step3 = menu["steps"][2]
    assert "走新加的关菜单那一步" in step3["what"]
    got = menu["menu_after_read"]
    assert "skipped" in got and "menus" in got and "main" in got  # 用的是哪种办法、关没关掉、主窗口看不看得见
    # 一开始那个菜单在第 2 步已经关掉了：第 3 步确实走了新函数，这次没有可关的菜单
    assert got["skipped"] == "" and got["menus"] == []
    assert "一开始盖在聊天区上的菜单" in {c.get("when") for c in menu["closed"]}
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "GetAllMessage() 之后对菜单发 Esc（新加的那一步）" in log and "主窗口" in log


def test_the_command_step_three_closes_a_menu_left_open_and_records_it(tmp_path):
    """读消息之后又冒出菜单：第 3 步的新函数只发 Esc、就把它关掉，写进 closed 和 probe.log。"""
    out = tmp_path / "out"
    api = menu_api(menu_at_start=False)
    api.visible[MENU_HWND] = False

    w = MenuWeChat(tmp_path)
    orig_read = w.GetAllMessage

    def read_all():
        api.visible[MENU_HWND] = True  # 读消息把菜单弹出来（真机上就是这样）
        return orig_read()
    w.GetAllMessage = read_all
    assert wxpicture_run(out, w, api) == 0
    menu = result_of(out)["menu"]
    got = menu["menu_after_read"]
    assert got["menus"] and got["menus"][0]["closed"] is True  # 只发 Esc 就关掉了
    assert got["menus"][0]["how"] == "esc"  # 用的是哪种办法：Esc（这里不发 WM_CLOSE）
    assert got["main"] and got["main"]["visible"] is True  # 发完之后主窗口还看得见
    assert any(c.get("when") == "GetAllMessage() 之后（新加的那一步）" for c in menu["closed"])
    assert [p for p in api.posts if p[0] == MENU_HWND]  # 只对菜单发过东西
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "GetAllMessage() 之后对菜单发 Esc（新加的那一步）：句柄 77" in log


def test_the_command_records_the_window_list_before_and_after_the_panel(tmp_path):
    out = tmp_path / "out"
    api = menu_api()
    w = MenuWeChat(tmp_path)
    assert wxpicture_run(out, w, api) == 0
    res = result_of(out)
    pw = res["panel_windows"]
    assert pw["before"] and pw["after"]  # 点面板之前、面板关掉之后各记一次
    for rows in (pw["before"], pw["after"]):
        for row in rows:
            assert {"hwnd", "class", "title", "rect", "visible"} <= set(row)
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "点开表情面板之前" in log and "表情面板关掉之后" in log


def test_an_error_while_listing_windows_before_the_panel_does_not_fail_the_command(tmp_path):
    out = tmp_path / "out"
    w = MenuWeChat(tmp_path)

    class BadApi(FakeApi):
        def windows(self):
            raise RuntimeError("查窗口出错")
    api = BadApi(owner=MAIN_HWND, windows=wx_windows([]))
    assert wxpicture_run(out, w, api) == 0  # 命令照旧返回 0
    res = result_of(out)
    assert res["ok"] is True
    assert all("error" in row for row in res["panel_windows"]["before"])  # 列不出窗口也照实写
    assert "点开表情面板之前" in (out / "probe.log").read_text(encoding="utf-8")


def wxpicture_run(out, w, api):
    from mb import wxpicture
    return wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=api, grab=grab_shot(),
                              hwnd_finder=lambda: MAIN_HWND)


# ---------- 五、说明 ----------
def test_the_guides_say_every_read_closes_the_menu():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cn = (root / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (root / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    assert "PodMates 每次读完消息都会对这个菜单发一次 Esc" in cn
    assert "关不掉就留着，记一条日志" in cn
    assert "every time PodMates finishes reading messages" in en
    assert "left alone and logged" in en
    # 验证命令那一段写了第 3 条新加的记录
    assert "走新加的关菜单那一步" in cn and "点开表情面板之前" in cn and "面板关掉之后" in cn
    assert "goes through the new close-the-menu step" in en
    assert "before the emoji panel is opened" in en and "after it closes" in en
