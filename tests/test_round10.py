"""补充十：微信窗口移到屏幕外。全程用模拟的窗口接口和模拟的 wxauto，不碰任何真窗口。"""
import json
import types
from pathlib import Path

import pytest

from mb import winpos
from mb.channels.wechat import WeChatChannel
from mb.winpos import Keeper, clamp_visible, intersects, offscreen_pos, recover, visible_enough

# 三块显示器：左边一块（负坐标、偏下）、主显示器、右上一块 4K
MONS = [(-1280, 200, 0, 1000), (0, 0, 1920, 1080), (1920, -400, 5760, 1760)]
WORK = (0, 0, 1920, 1040)


class FakeWin:
    """模拟的窗口接口：一个微信主窗口 + 一组显示器。"""

    def __init__(self, rect=(300, 100, 1300, 800), zoomed=False, iconic=False, mons=None):
        self.hwnd, self.r, self.z, self.i = 4242, rect, zoomed, iconic
        self.mons = mons or list(MONS)
        self.log = []
        self.fg, self.zp = 111, 222  # 用户现在在用的别的窗口（前台）；微信窗口在 Z 序里排在 222 后面
        self.others = {111, 222, 333}

    def begin(self):
        pass

    def monitors(self):
        return list(self.mons)

    def work_area(self):
        return WORK

    def rect(self, h):
        return self.r

    def exists(self, h):
        return h == self.hwnd or h in self.others

    def foreground(self):
        return self.fg

    def set_foreground(self, h):
        self.fg = h
        self.log.append(("fg", h))

    def zprev(self, h):
        return self.zp

    def set_zafter(self, h, after):
        self.zp = after
        self.log.append(("z", after))

    def is_wechat(self, h):
        return h == self.hwnd

    def steal(self):
        """模拟 wxauto4 的操作把微信激活到最前面。"""
        self.fg, self.zp = self.hwnd, 0

    def is_zoomed(self, h):
        return self.z

    def is_iconic(self, h):
        return self.i

    def show(self, h, how):
        self.log.append(how)
        if how == "restore":
            self.z = False
        elif how == "maximize":
            self.z = True

    def move(self, h, x, y, w, hh):
        self.r = (x, y, x + w, y + hh)
        self.log.append(("move", self.r))

    def find_main(self):
        return self.hwnd

    def seen(self):
        return visible_enough(self.r, self.mons)


# ---------- 几何：多显示器、高分屏 ----------
@pytest.mark.parametrize("mons", [
    MONS,
    [(0, 0, 1920, 1080)],
    [(0, 0, 3840, 2160)],  # 4K 单屏（物理像素）
    [(0, 0, 1920, 1080), (0, 1080, 1920, 2160)],  # 上下两块
    [(-1920, 0, 0, 1080), (0, 0, 1920, 1080), (1920, 0, 3840, 1080)],  # 三块横排
    [(0, 0, 2560, 1440), (2560, 360, 3584, 1128)],  # 一大一小、不同高度
])
def test_offscreen_position_misses_every_monitor(mons):
    for w, h in ((1000, 700), (1920, 1040), (400, 300)):
        x, y = offscreen_pos(mons, w, h)
        assert not any(intersects((x, y, x + w, y + h), m) for m in mons)
        assert all(abs(v) <= winpos.LIMIT for v in (x, y, x + w, y + h))
        assert not visible_enough((x, y, x + w, y + h), mons)


def test_offscreen_falls_back_to_other_side_when_right_is_out_of_range():
    mons = [(0, 0, 30000, 1080)]  # 右边放不下（超出 ±30000）：改放左边/下边
    x, y = offscreen_pos(mons, 1000, 700)
    assert not intersects((x, y, x + 1000, y + 700), mons[0]) and abs(x) <= winpos.LIMIT and abs(x + 1000) <= winpos.LIMIT


def test_clamp_visible():
    r = (300, 100, 1300, 800)
    assert clamp_visible(r, MONS, WORK) == r  # 看得见：原样
    gone = (-3000, 100, -2000, 800)  # 那块显示器拔掉了：放到主显示器工作区左上
    assert clamp_visible(gone, MONS, WORK) == (40, 40, 1040, 740)
    huge = (-9000, -9000, -2000, -2000)  # 比工作区还大：缩到工作区里
    x1, y1, x2, y2 = clamp_visible(huge, MONS, WORK)
    assert x2 - x1 <= 1840 and y2 - y1 <= 960 and (x1, y1) == (40, 40)
    edge = (1900, 1000, 2900, 1700)  # 只有一个角在屏幕里（<120px）：算看不见
    assert not visible_enough(edge, [(0, 0, 1920, 1080)])


# ---------- 管家：移出 → 临时移回 → 再移出 → 恢复 ----------
def test_keeper_cycle_and_state_file(tmp_path):
    api = FakeWin()
    st = tmp_path / "w.json"
    k = Keeper(api, st)
    orig = api.r
    assert k.hide() and not api.seen() and api.r[2] - api.r[0] == 1000 and api.r[3] - api.r[1] == 700  # 大小不变
    saved = json.loads(st.read_text(encoding="utf-8"))
    assert saved["off"] is True and tuple(saved["orig"]) == orig  # 移之前就记下了原位置
    assert k.hide()  # 已在外面：不重复记，不乱动
    assert tuple(json.loads(st.read_text(encoding="utf-8"))["orig"]) == orig
    assert k.bring() and api.r == orig and api.seen()
    assert k.hide() and not api.seen()  # 再移出去，原位置还是最初那个
    assert k.bring() and api.r == orig
    assert k.hide()
    assert k.restore() and api.r == orig and not st.exists() and not k.off
    assert k.moves == 6


def test_keeper_maximized_minimized_and_missing_monitor(tmp_path):
    api = FakeWin(rect=(0, 0, 1920, 1040), zoomed=True)
    k = Keeper(api, tmp_path / "w.json")
    assert k.hide() and not api.z and not api.seen()  # 最大化的先还原再移
    assert k.restore() and api.z is True and api.r[:2] == (0, 0)  # 恢复时重新最大化
    # 最小化着的不动
    api2 = FakeWin(iconic=True)
    k2 = Keeper(api2, tmp_path / "w2.json", log=lambda *a: None)
    before = api2.r
    assert k2.hide() is False and api2.r == before
    # 临时移回时原位置所在显示器没了：放到主显示器
    api3 = FakeWin(rect=(-1200, 300, -300, 900))
    k3 = Keeper(api3, tmp_path / "w3.json")
    k3.hide()
    api3.mons = [(0, 0, 1920, 1080)]  # 拔掉了左边那块
    assert k3.bring() and api3.seen() and api3.r[0] >= 0
    # 没找到窗口
    api4 = FakeWin()
    api4.hwnd = None
    api4.find_main = lambda: None
    assert Keeper(api4, tmp_path / "w4.json").hide() is False


# ---------- 找回窗口 ----------
def test_recover(tmp_path):
    st = tmp_path / "w.json"
    api = FakeWin()
    k = Keeper(api, st)
    k.hide()
    assert not api.seen()
    k.off = False  # 模拟程序被强行结束：管家没了，只剩状态文件
    r = recover(api, st)
    assert r["found"] and r["moved"] and api.r == (300, 100, 1300, 800) and not st.exists()
    # 没有记录、窗口在屏幕外：放到主显示器工作区里
    api2 = FakeWin(rect=(9000, 0, 10000, 700))
    r = recover(api2, tmp_path / "none.json")
    assert r["moved"] and api2.seen() and WORK[0] <= api2.r[0] and api2.r[2] <= WORK[2]
    # 本来就看得见：什么都不动
    api3 = FakeWin()
    r = recover(api3, tmp_path / "none2.json")
    assert r == {"found": True, "moved": False, "rect": (300, 100, 1300, 800)} and api3.log == []
    # 找不到窗口
    api4 = FakeWin()
    api4.hwnd, api4.find_main = None, (lambda: None)
    assert recover(api4, tmp_path / "x.json") == {"found": False, "moved": False}


def test_real_winapi_reads_monitors_only():
    """真接口只测读显示器信息（不找窗口、不移任何窗口）。"""
    api = winpos.WinApi()
    api.begin()
    mons = api.monitors()
    assert mons and all(m[2] > m[0] and m[3] > m[1] for m in mons)
    w = api.work_area()
    assert w[2] > w[0]


# ---------- 渠道集成 ----------
class Chat:
    def __init__(self, who, api):
        self.who, self.sent, self.api, self.seen_when_sent = who, [], api, []

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        self.seen_when_sent.append(self.api.seen())  # 发送的这一刻窗口必须在屏幕里
        self.sent.append(msg)
        return types.SimpleNamespace(is_success=True)


class Wx:
    info = {"nickname": "小号", "wxid": "wxid_bot1"}
    api = None
    instances = []

    def __init__(self, *a, **k):
        self.cbs, self.chats, self.listening, self.reads_seen = {}, {}, False, []
        self._info = dict(Wx.info)  # 和真的一样：构造时读一次、之后 GetMyInfo 返回缓存
        Wx.instances.append(self)

    def GetMyInfo(self):
        self.reads_seen.append(Wx.api.seen())  # 读身份时窗口也得在屏幕里
        return dict(self._info)

    def AddListenChat(self, n, cb):
        self.cbs[n] = cb
        self.chats[n] = Chat(n, Wx.api)
        return self.chats[n]

    def StartListening(self):
        self.listening = True

    def RemoveListenChat(self, n, close_window=True):
        self.cbs.pop(n, None)

    def IsOnline(self):
        return True


@pytest.fixture
def setup(monkeypatch, tmp_path):
    api = FakeWin()
    Wx.api, Wx.instances, Wx.info = api, [], {"nickname": "小号", "wxid": "wxid_bot1"}
    monkeypatch.setattr(WeChatChannel, "lib", types.SimpleNamespace(WeChat=Wx))
    monkeypatch.setattr(WeChatChannel, "window_api", api)
    return api


def make(e):
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": [{"name": "测试群", "type": "group"}]})
    ch = WeChatChannel(e.engine, acc)
    e.engine.add_bot(acc, ch)
    return ch


def test_channel_moves_window_only_after_identity_ok(env, setup):
    api = setup
    orig = api.r

    async def body(e):
        e.core.settings.set("platform:wechat", "wechat_offscreen", True)
        # 首次：等确认期间窗口不动；点「不是」后也不动、不启动
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm" and api.r == orig and api.seen()
        await ch.confirm_bind(False)
        assert api.r == orig and ch.status == "stopped"
        # 点「是」：绑定、上线，窗口才移出去
        ch = make(e)
        await ch.start()
        assert api.r == orig
        await ch.confirm_bind(True)
        assert ch.status == "online" and not api.seen()
        wx = Wx.instances[-1]
        # 发送：这一刻窗口在屏幕里，做完又在外面
        await ch.send("group", "测试群", "你好")
        assert wx.chats["测试群"].sent == ["你好"] and wx.chats["测试群"].seen_when_sent == [True] and not api.seen()
        assert ch.bring_ms and ch.bring_ms[-1] < 500
        # 复核身份（要点界面）：同样先移回再移出
        assert await ch.recheck() is True
        assert Wx.instances[-1].reads_seen[-1] is True and not api.seen()  # 复核是重新连一次读的，读的时候窗口在屏幕里、读完又移出去
        # 停止：放回原位
        await ch.stop()
        assert api.r == orig and not (Path(e.core.data) / "wechat_window.json").exists()
    env.run(body)


def test_mismatch_never_moves_window_and_midrun_switch_restores_it(env, setup):
    api = setup
    orig = api.r

    async def body(e):
        e.core.settings.set("platform:wechat", "wechat_offscreen", True)
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        assert not api.seen()
        await ch.stop()
        # 再启动时登录的是别的号：拒绝启动，窗口不动
        Wx.info = {"nickname": "大号", "wxid": "wxid_big"}
        assert api.r == orig
        ch = make(e)
        await ch.start()
        assert ch.status == "error" and api.r == orig and api.seen()
        await ch.stop()
        # 运行中换号：停止监听、窗口放回原位
        Wx.info = {"nickname": "小号", "wxid": "wxid_bot1"}
        ch = make(e)
        await ch.start()
        assert ch.status == "online" and not api.seen()
        Wx.info = {"nickname": "大号", "wxid": "wxid_big"}
        assert await ch.recheck() is False
        assert ch.status == "error" and api.r == orig and api.seen()
        await ch.stop()
    env.run(body)


def test_option_off_by_default_and_toggle_while_running(env, setup):
    api = setup
    orig = api.r

    async def body(e):
        assert e.core.settings.get("wechat_offscreen", platform="wechat") is False  # 默认关
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        assert ch.status == "online" and api.r == orig and api.log == []  # 关着：一次都没动过窗口
        await ch.send("group", "测试群", "a")
        assert api.log == []
        e.core.settings.set("platform:wechat", "wechat_offscreen", True)
        await ch.send("group", "测试群", "b")  # 开了：这次操作之后窗口移出去
        assert not api.seen()
        e.core.settings.set("platform:wechat", "wechat_offscreen", False)
        await ch.send("group", "测试群", "c")  # 关了：放回原位
        assert api.r == orig
        await ch.stop()
    env.run(body)


def test_stale_offscreen_window_recovered_on_start(env, setup, tmp_path):
    api = setup
    orig = api.r

    async def body(e):
        e.core.settings.set("platform:wechat", "wechat_offscreen", True)
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        assert not api.seen()
        # 程序被强行结束：没有 stop，窗口留在屏幕外，状态文件还在
        assert (e.core.data / "wechat_window.json").exists()
        ch.keeper_on = False
        ch2 = make(e)  # 重新启动：先把窗口放回来，再读身份
        await ch2.start()
        assert Wx.instances[-1].reads_seen[0] is True  # 读身份的时候窗口已经放回屏幕里了
        assert ch2.status == "online" and not api.seen()  # 核对通过后又按设置移出去
        await ch2.stop()
        assert api.r == orig
    env.run(body)


def test_recover_api(env, setup):
    api = setup

    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        api.r = (9000, 0, 10000, 700)
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        r = await c.post("/api/wechat/recover")
        j = await r.json()
        assert r.status == 200 and j["found"] and j["moved"] and api.seen()
        j = await (await c.post("/api/wechat/recover")).json()
        assert j["found"] and not j["moved"]
        await c.close()
    env.run(body)


def test_ui_and_docs_mention_offscreen():
    root = Path(__file__).resolve().parent.parent
    js = (root / "web/js/pages-main.js").read_text(encoding="utf-8")
    assert "找回微信窗口" in js and "/api/wechat/recover" in js
    from mb.settings import BY_KEY
    s = BY_KEY["wechat_offscreen"]
    assert s["default"] is False and s["platforms"] == ["wechat"]
    for must in ("运行中别用小号手动聊天", "切换会话时窗口会闪一下", "仍然不能最小化"):
        assert must in s["help"], must
    for doc, words in (("docs/使用说明.md", ("运行中别用小号手动聊天", "闪一下", "不能最小化")), ("docs/User-Guide.en.md", ("do not chat", "flash", "minimi"))):
        t = (root / doc).read_text(encoding="utf-8")
        assert all(w in t for w in words), (doc, words)
