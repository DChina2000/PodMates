"""补四十七之三：微信发表情后多按的 Esc 把主窗口藏进托盘，之后收不到消息。

两件事：
  一、点了表情格子以后，微信自己就把面板关掉了；这时不再按 Esc（多按的那一下会落到微信主窗口上，微信会把它藏进托盘）。
      面板还在（只翻着看、同步收藏）照旧按 Esc。
  二、复核身份 / 轮询读不到界面时，先看主窗口是不是被藏进托盘或最小化了：是就显示出来（不抢前台焦点），马上重试一次；
      连续几次还不行就把账号状态写成出错，面板不再一直显示「在线」。

全部用桩：world 里桩出来的表情面板和窗口接口，不碰真的微信、不操作任何真窗口，也不调 wxauto4 的界面函数。
"""
import asyncio
import types
from types import SimpleNamespace

import pytest

from test_round12 import until
from test_round35 import logs
from test_round41 import entry_for
from wxworld import World

from mb import winpos
from mb.channels import wechat
from mb.channels.wechat import UI_FAIL_MAX, WeChatChannel
from mb.winpos import WinApi, unhide


# ---------- 一、不再多按 Esc ----------
def test_when_wechat_closed_the_panel_by_itself_no_esc_is_pressed():
    """点了格子：微信马上把表情发出去并自己关掉面板 → 收尾不按 Esc，只把 opened 置成 False，日志写明。"""
    w = World([(f"s{i}", False) for i in range(5)])
    orig_pick = w.pick

    def pick_then_wechat_closes_the_panel(i):
        orig_pick(i)
        w.opened = w.tab = False  # 真机上就是这样：点完微信自己就把面板关了
        w.hover = None
    w.pick = pick_then_wechat_closes_the_panel
    log = []
    r = w.driver(log=log.append).send(w.wx(), entry_for(w, 2), "目标群", "")
    assert r["found"] and r["clicked"] and w.sent_index == [2]
    assert w.keys == []  # 一下 Esc 都没按（多按的那一下会落到主窗口上）
    assert any("表情面板已自己关上，不用按 Esc" in x for x in log)
    assert not any("已按 Esc" in x for x in log)


def test_when_the_panel_is_still_there_esc_is_pressed():
    """面板还开着（点的那一下没让它自己关掉）→ 照旧按 Esc 关掉。"""
    w = World([(f"s{i}", False) for i in range(5)])  # pick() 不会关面板
    log = []
    r = w.driver(log=log.append).send(w.wx(), entry_for(w, 2), "目标群", "")
    assert r["found"] and r["clicked"] and w.keys == ["{Esc}"]
    assert any("已按 Esc 关掉表情面板" in x for x in log)
    assert not any("已自己关上" in x for x in log)


def test_syncing_favourites_still_presses_esc_because_the_panel_is_open():
    """只翻着看、不点格子：面板一直开着，收尾照旧按 Esc。"""
    w = World([(f"s{i}", False) for i in range(8)])
    log = []
    w.driver(log=log.append).catalog(w.wx(), [], 0, "文件传输助手", "某个群")
    assert w.sent_index == [] and w.clicks[-2:] and w.keys == ["{Esc}"]
    assert any("已按 Esc 关掉表情面板" in x for x in log)


def test_close_does_nothing_when_the_panel_was_never_opened():
    """没打开过面板：什么都不按（原来就是这样，别改坏）。"""
    w = World([("s0", False)])
    p = w.driver_panel()
    p.opened = False
    p.close()
    assert w.keys == []


def test_panel_close_treats_an_unreadable_panel_as_already_closed():
    """面板读不出来（_popover 返回 None）时宁可留着面板，也不冒险按 Esc。"""
    w = World([("s0", False)])
    p = w.driver_panel()
    p.opened = True
    p._popover = lambda: None
    p.close()
    assert w.keys == []


def test_probe_command_does_not_press_esc_when_the_panel_already_closed(tmp_path):
    """探查命令也用同一套判断：面板已经不在了就不再按 Esc。"""
    from test_round37 import World as ProbeWorld, probe
    w = ProbeWorld()
    tab = next(c for c in w.panel.kids if c.ControlTypeName == "TabItemControl" and c.Name == "自定义表情")
    orig = tab.on_click

    def single_then_close():
        orig()
        w.root_kids.clear()  # 面板自己关掉了：进程里已经找不到那个顶层窗口
        w.root_kids.append(w.main)
    tab.on_click = single_then_close
    assert probe(tmp_path, w) == 0
    assert w.keys == []
    assert "表情面板已自己关上，不用按 Esc" in (tmp_path / "out" / "probe.log").read_text(encoding="utf-8")


def test_panel_roots_ignores_windows_that_are_hidden(monkeypatch):
    """面板关掉以后那些隐藏的窗口还挂在进程里：popup_roots 不能把它们当成「面板还在」。按 Win32 的可见性判断，不看窗口在不在屏幕里
    （微信窗口有时被挪到屏幕外，开着的面板也跟着在屏幕外，不能因此认丢）。"""
    from mb import wxprobe
    monkeypatch.setattr(wxprobe, "_vis", lambda h: {2: False, 3: True, 4: True}.get(h))
    main = SimpleNamespace(ProcessId=7, NativeWindowHandle=1)
    hidden = SimpleNamespace(ProcessId=7, NativeWindowHandle=2, ClassName="mmui::EmoticonPopover", Name="Weixin")
    shown = SimpleNamespace(ProcessId=7, NativeWindowHandle=3, ClassName="mmui::EmoticonPopover", Name="Weixin", IsOffscreen=True)
    other = SimpleNamespace(ProcessId=8, NativeWindowHandle=4, ClassName="x", Name="y")
    ui = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [main, hidden, shown, other]))
    assert [c.NativeWindowHandle for _, c in wxprobe.popup_roots(ui, main)] == [3]


def test_panel_roots_keeps_windows_whose_handle_is_not_a_real_window():
    """句柄读不到、或不是真窗口的控件（测试桩、老控件）照旧算进来，不因为新增的判断把面板认丢。"""
    from mb.wxprobe import popup_roots
    main = SimpleNamespace(ProcessId=7, NativeWindowHandle=1)
    pop = SimpleNamespace(ProcessId=7, NativeWindowHandle=2, ClassName="mmui::EmoticonPopover", Name="Weixin")
    ui = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [main, pop]))
    assert [c.NativeWindowHandle for _, c in popup_roots(ui, main)] == [2]


def test_panel_roots_still_returns_nothing_when_the_tree_cannot_be_read():
    from mb.wxprobe import popup_roots
    ui = SimpleNamespace(GetRootControl=lambda: 1 / 0)
    assert popup_roots(ui, SimpleNamespace(ProcessId=7, NativeWindowHandle=1)) == []


def test_panel_close_after_sending_logs_and_switches_back_normally():
    """收尾的顺序没变：关面板（或确认已经关了）之后照旧切回原来的会话。"""
    w = World([(f"s{i}", False) for i in range(4)])
    orig_pick = w.pick

    def pick_and_close(i):
        orig_pick(i)
        w.opened = w.tab = False
        w.hover = None
    w.pick = pick_and_close
    w.driver().send(w.wx(), entry_for(w, 1), "目标群", "某个群")
    assert w.keys == [] and w.chatwith == ["目标群", "某个群"] and w.chat == "某个群"


# ---------- 二、主窗口被藏起来时自己恢复 ----------
class HidWin:
    """模拟的窗口接口：一个微信主窗口，能被藏起来（IsWindowVisible == False）、能最小化。

    和 test_round10.FakeWin 的区别：这个记得住「看得见」这个状态，并且把每个会动窗口的调用都记下来，
    用来证明恢复窗口时没抢前台焦点、没挪位置。"""

    def __init__(self, hidden=False, iconic=False, found=True):
        self.hwnd, self.hidden, self.iconic, self.found = 4242, hidden, iconic, found
        self.log: list = []
        self.fg = 111  # 用户现在在用的别的窗口

    def begin(self):
        pass

    def monitors(self):
        return [(0, 0, 1920, 1080)]

    def work_area(self):
        return (0, 0, 1920, 1040)

    def exists(self, h):
        return h == self.hwnd

    def is_iconic(self, h):
        return self.iconic

    def is_visible(self, h):
        return not self.hidden

    def show_quietly(self, h):
        self.hidden = self.iconic = False  # SW_SHOWNOACTIVATE：显示并还原
        self.log.append(("show_quietly", h))

    def show(self, h, how):
        self.log.append(("show", how))
        self.hidden = False
        self.iconic = False

    def move(self, h, x, y, w, hh):
        self.log.append(("move", (x, y, w, hh)))

    def set_foreground(self, h):
        self.log.append(("set_foreground", h))

    def foreground(self):
        return self.fg

    def zprev(self, h):
        return 0

    def set_zafter(self, h, after):
        self.log.append(("set_zafter", after))

    def is_wechat(self, h):
        return h == self.hwnd

    def find_main(self, visible_only=True):
        if not self.found:
            return None
        return None if (visible_only and self.hidden) else self.hwnd  # 藏起来时只有不看可见性才找得到


def test_unhide_finds_the_hidden_main_window_and_shows_it_without_taking_focus():
    api = HidWin(hidden=True)
    assert unhide(api) is True
    assert not api.hidden and api.log == [("show_quietly", 4242)]  # 只显示，不挪位置、不抢前台


def test_unhide_restores_a_minimized_main_window_too():
    api = HidWin(hidden=False, iconic=True)
    assert unhide(api) is True and not api.iconic


def test_unhide_does_nothing_when_the_window_is_already_visible():
    api = HidWin()
    assert unhide(api) is False and api.log == []


def test_unhide_does_nothing_when_there_is_no_wechat_window():
    assert unhide(HidWin(found=False)) is False


def test_find_main_still_ignores_hidden_windows_for_existing_callers():
    """原有调用方（find_main()）照旧只找看得见的：看不见的窗口仍然不返回。看不见的只有 find_main(visible_only=False) 才找得到。"""
    class U:
        def IsWindowVisible(self, h):
            return False

        def GetClassNameW(self, h, buf, n):
            buf.value = "Qt51514QWindowIcon"

        def GetWindowTextW(self, h, buf, n):
            buf.value = "WeChat"

        def GetWindowRect(self, h, ref):
            ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = 0, 0, 1000, 700

        def EnumWindows(self, cb, lp):
            cb(4242, 0)

        def IsWindow(self, h):
            return True

    api = WinApi()
    api.u, api.k = U(), SimpleNamespace()
    api._exe = lambda h: r"C:\Program Files\Tencent\Weixin\Weixin.exe"  # 不真的去查进程
    assert api.find_main() is None  # 原有行为：看不见的不返回
    assert api.find_main(visible_only=False) == 4242  # 只给「主窗口被藏起来了」的恢复用


def test_real_winapi_show_quietly_uses_sw_shownoactivate():
    """真实接口只检查它调的是 SW_SHOWNOACTIVATE（不激活）；用假的 user32 记参数，不操作任何真窗口。"""
    calls = []

    class U:
        def ShowWindow(self, h, how):
            calls.append((h, how))
            return True

    api = WinApi()
    api.u = U()
    api.show_quietly(4242)
    assert calls == [(4242, 4)]  # SW_SHOWNOACTIVATE


# ---------- 复核身份：先显示再重试 ----------
class FlakyWx:
    """模拟的 wxauto4：界面读不到时构造就抛错；窗口被显示出来以后就正常了。

    attempts 里记下每次构造时窗口是不是看得见（证明重试是在显示之后做的）。"""
    info = {"nickname": "小号", "wxid": "wxid_bot1"}
    api: "HidWin | None" = None
    attempts: list = []
    state = {"visible_ok": False}

    def __init__(self, *a, **k):
        self._info = dict(FlakyWx.info)
        seen = bool(FlakyWx.api and FlakyWx.api.is_visible(FlakyWx.api.hwnd))
        FlakyWx.attempts.append(seen)
        if not seen or not FlakyWx.state["visible_ok"]:
            raise LookupError("Find Control Timeout: {'ClassName': 'mmui::XTabBarItem', 'Name': '微信'}")

    def GetMyInfo(self):
        return dict(self._info)

    nickname = "小号"


def wx_env(monkeypatch, api):
    FlakyWx.api, FlakyWx.attempts = api, []
    FlakyWx.state["visible_ok"] = False
    monkeypatch.setattr(WeChatChannel, "lib", types.SimpleNamespace(WeChat=FlakyWx))
    monkeypatch.setattr(WeChatChannel, "window_api", api)


def online(e, api):
    """直接造一个已上线、绑好身份的微信渠道（不连窗口：这一组只测复核和恢复）。"""
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": []})
    ch = WeChatChannel(e.engine, acc)
    ch.loop, ch.status, ch.wx, ch.self_name, ch.self_id = asyncio.get_running_loop(), "online", object(), "海豚", "海豚"
    ch._lib = WeChatChannel.lib
    ch.keeper = winpos.Keeper(api, e.core.data / "wechat_window.json")
    ch.keeper_on = True
    ch.main_hwnd = api.hwnd
    ch._save_bound({"wxid": "wxid_bot1", "nick": "小号"})
    e.engine.add_bot(acc, ch)
    return ch


def test_verify_shows_the_hidden_window_and_retries_once_then_succeeds(env, monkeypatch):
    api = HidWin(hidden=True)
    wx_env(monkeypatch, api)

    async def body(e):
        ch = online(e, api)
        FlakyWx.state["visible_ok"] = True  # 窗口显示出来以后就能连上了
        await ch._run(ch._verify_now)
        assert FlakyWx.attempts == [False, True]  # 第一次没看见窗口，显示之后重试的那次看见了
        assert not api.hidden and [x for x in api.log if x[0] != "show_quietly"] == []  # 只显示，没挪位置、没抢前台
        assert logs(e, "被藏起来了（看不见），已经重新显示")
        assert not logs(e, "稍后再试")  # 重试成功了：不写「稍后再试」
        assert ch.ui_fails == 0
    env.run(body)


def test_verify_does_not_show_anything_when_the_window_is_fine(env, monkeypatch):
    api = HidWin()
    wx_env(monkeypatch, api)

    async def body(e):
        ch = online(e, api)
        FlakyWx.state["visible_ok"] = True
        await ch._run(ch._verify_now)
        assert api.log == [] and FlakyWx.attempts == [True]
        assert not logs(e, "被藏起来了（看不见）")
    env.run(body)


def test_verify_logs_one_warn_per_failure_and_never_warns_when_the_retry_works(env, monkeypatch):
    """显示后重试又失败：这一次写一条「稍后再试」；显示后重试成功的那次一条 warn 都不写。"""
    api = HidWin(hidden=True)
    wx_env(monkeypatch, api)

    async def body(e):
        ch = online(e, api)
        await ch._run(ch._verify_now)  # 显示之后还是连不上
        assert FlakyWx.attempts == [False, True]
        assert len(logs(e, "稍后再试")) == 1 and "显示出来了，还是连不上" in logs(e, "稍后再试")[0]
        assert ch.ui_fails == 1 and ch.status == "online"
    env.run(body)


def test_verify_does_not_wait_for_the_identity_check_after_unhiding_a_visible_window(env, monkeypatch):
    """窗口本来就看得见、失败也不是界面问题（比如换号）：照旧抛 IdentityChanged，不显示窗口、不重试。"""
    api = HidWin()
    wx_env(monkeypatch, api)

    async def body(e):
        ch = online(e, api)
        FlakyWx.info = {"nickname": "大号", "wxid": "wxid_big"}
        FlakyWx.state["visible_ok"] = True
        with pytest.raises(wechat.IdentityChanged):
            await ch._run(ch._verify_now)
        assert api.log == []
        FlakyWx.info = {"nickname": "小号", "wxid": "wxid_bot1"}
    env.run(body)


# ---------- 轮询：读不到界面时先显示再重试 ----------
class PeekFails:
    """GetSession 读不到界面；窗口显示出来以后能读了。"""
    instances: list = []
    api: "HidWin | None" = None
    state = {"peek_calls": 0, "visible_ok": False}

    def __init__(self, *a, **k):
        PeekFails.instances.append(self)

    def GetSession(self):
        PeekFails.state["peek_calls"] += 1
        if not (PeekFails.api and PeekFails.api.is_visible(PeekFails.api.hwnd)) or not PeekFails.state["visible_ok"]:
            raise LookupError("Find Control Timeout: {'ClassName': 'mmui::XTabBarItem', 'Name': '微信'}")
        return []

    def IsOnline(self):
        return True


def poll_env(monkeypatch, api, poll=0.05):
    PeekFails.instances, PeekFails.api = [], api
    PeekFails.state.update(peek_calls=0, visible_ok=False)
    monkeypatch.setattr(WeChatChannel, "lib", types.SimpleNamespace(WeChat=PeekFails))
    monkeypatch.setattr(WeChatChannel, "window_api", api)
    return poll


def test_poll_shows_the_hidden_window_and_retries_once_in_the_same_round(env, monkeypatch):
    api = HidWin(hidden=True)

    async def body(e):
        poll = poll_env(monkeypatch, api)
        e.core.settings.set("global", "proactive", False)
        e.core.settings.set("platform:wechat", "wechat_recv_mode", "poll")
        e.core.settings.set("platform:wechat", "wechat_poll", poll)
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": []})
        ch = WeChatChannel(e.engine, acc)
        ch.loop, ch.status, ch.wx, ch.mode, ch.self_name, ch.self_id = (
            asyncio.get_running_loop(), "online", PeekFails(), "poll", "海豚", "海豚")
        ch.keeper, ch.keeper_on, ch.main_hwnd = winpos.Keeper(api, e.core.data / "wechat_window.json"), True, api.hwnd
        e.engine.add_bot(acc, ch)
        PeekFails.state["visible_ok"] = True
        ch._poll_task = asyncio.create_task(ch._poll())
        assert await until(lambda: not api.hidden and ch.poll_fails == 0 and PeekFails.state["peek_calls"] >= 2, 3)
        assert logs(e, "被藏起来了（看不见），已经重新显示")
        assert not logs(e, "轮询出错")  # 显示之后这一次就读通了：不报轮询出错
        ch.status = "stopped"
        await ch._poll_task
    env.run(body)


def test_poll_reports_each_failed_round_once(env, monkeypatch):
    """显示了还读不通：这一轮写一条「轮询出错」，失败次数跟着涨。"""
    api = HidWin(hidden=True)

    async def body(e):
        poll = poll_env(monkeypatch, api)
        e.core.settings.set("global", "proactive", False)
        e.core.settings.set("platform:wechat", "wechat_recv_mode", "poll")
        e.core.settings.set("platform:wechat", "wechat_poll", poll)
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": []})
        ch = WeChatChannel(e.engine, acc)
        ch.loop, ch.status, ch.wx, ch.mode, ch.self_name, ch.self_id = (
            asyncio.get_running_loop(), "online", PeekFails(), "poll", "海豚", "海豚")
        ch.keeper, ch.keeper_on, ch.main_hwnd = winpos.Keeper(api, e.core.data / "wechat_window.json"), True, api.hwnd
        e.engine.add_bot(acc, ch)
        ch._poll_task = asyncio.create_task(ch._poll())
        assert await until(lambda: ch.poll_fails >= 1, 3)
        assert logs(e, "轮询出错") and ch.ui_fails == 0  # 轮询和复核身份各数各的
        ch.status = "stopped"
        await ch._poll_task
    env.run(body)


# ---------- 连续失败到次数：状态不再「在线」 ----------
def test_consecutive_failures_turn_the_status_into_an_error_that_the_panel_can_show(env, monkeypatch):
    api = HidWin(hidden=True)
    wx_env(monkeypatch, api)

    async def body(e):
        ch = online(e, api)
        for _ in range(UI_FAIL_MAX - 1):
            await ch._run(ch._verify_now)
            assert ch.status == "online"  # 还没到次数：照旧稍后再试
        assert len(logs(e, "稍后再试")) == UI_FAIL_MAX - 1 and ch.ui_fails == UI_FAIL_MAX - 1
        await ch._run(ch._verify_now)  # 第 UI_FAIL_MAX 次
        assert ch.status == "error" and ch.detail and "读不到微信界面" in ch.detail
        assert "找回微信窗口" in ch.detail  # 面板上看得到原因和处理办法
        assert ch.ui_fails == 0  # 报过之后重新数
        assert logs(e, "连续") and "读不到微信界面" in logs(e, "连续")[0]
    env.run(body)


def test_a_successful_read_resets_the_consecutive_failure_count(env, monkeypatch):
    api = HidWin(hidden=True)
    wx_env(monkeypatch, api)

    async def body(e):
        ch = online(e, api)
        for _ in range(UI_FAIL_MAX - 1):
            await ch._run(ch._verify_now)
        FlakyWx.state["visible_ok"] = True
        await ch._run(ch._verify_now)  # 这次显示之后重试成功
        assert ch.ui_fails == 0 and ch.status == "online"
        FlakyWx.state["visible_ok"] = False
        api.hidden = True
        for _ in range(UI_FAIL_MAX - 1):
            await ch._run(ch._verify_now)
        assert ch.status == "online"  # 重新数：没到次数就不报警
    env.run(body)


def test_sporadic_poll_errors_between_good_rounds_do_not_add_up(env):
    """零星的轮询出错中间隔着读通的轮次：每次读通都清零，隔几天攒起来也到不了上限、不会把账号误停掉。"""
    async def body(e):
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": []})
        ch = WeChatChannel(e.engine, acc)
        ch.status = "online"
        for _ in range(UI_FAIL_MAX * 2):
            ch._ui_trouble("微信：轮询出错（x）", "poll_fails")
            ch.poll_fails = 0  # 下一轮读通（_poll 里成功的一轮就是这样清零的）
        assert ch.status == "online"
    env.run(body)


def test_the_status_text_reaches_the_panel_through_the_normal_account_event(env, monkeypatch):
    """账号状态走 set_status → emit("account")，面板照旧收得到（不用另外加东西）。"""
    api = HidWin(hidden=True)
    wx_env(monkeypatch, api)
    got = []

    async def body(e):
        e.engine._emit = lambda kind, data: got.append((kind, data)) if kind == "account" else None
        ch = online(e, api)
        for _ in range(UI_FAIL_MAX):
            await ch._run(ch._verify_now)
        acc = [d for k, d in got if k == "account" and d.get("status") == "error"]
        assert acc and "读不到微信界面" in acc[-1]["detail"]
    env.run(body)


def test_failure_count_is_not_added_as_a_setting():
    """次数是代码里定的常量，不另加设置（作者这边不用动）。"""
    from mb.settings import BY_KEY
    assert UI_FAIL_MAX == 6
    assert not any("ui_fail" in k or "藏" in k for k in BY_KEY)


# ---------- 三、单独的聊天窗口：这次不动，只确认我们没有把它藏起来/关掉 ----------
def test_we_never_close_or_hide_chat_windows_and_only_show_the_main_window():
    """恢复窗口时只碰主窗口（find_main 的结果），不关、不藏任何别的窗口；代码里没有 Close/Hide 聊天窗口的地方。"""
    import inspect
    src = inspect.getsource(unhide)
    assert "find_main(visible_only=False)" in src and "show_quietly" in src
    assert "Close" not in src and "Hide" not in src
    from mb import winpos as w
    assert "CloseWindow" not in inspect.getsource(w)
