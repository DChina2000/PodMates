"""微信表情面板探查（只看不发）：用桩的微信和界面自动化跑通；证明它不发任何东西、不点表情、只点两个允许的控件、出错时停在哪一步写得清楚、不取单实例互斥体。"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mb import singleton, wxprobe

ROOT = Path(__file__).resolve().parent.parent


class Ctl:
    """桩控件：有 uiautomation 控件被探查命令用到的那几个属性和方法。"""

    def __init__(self, type_, cls, name, kids=(), rect=(0, 0, 10, 10), aid="", on_click=None, pid=7, hwnd=0):
        self.ControlTypeName, self.ClassName, self.Name, self.AutomationId = type_, cls, name, aid
        self.BoundingRectangle = SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
        self.kids, self.on_click, self.ProcessId, self.NativeWindowHandle = list(kids), on_click, pid, hwnd

    def GetChildren(self):
        return list(self.kids)

    def Click(self):
        CLICKS.append(self.Name)
        if self.on_click:
            self.on_click()


CLICKS: list[str] = []


class World:
    """一个桩出来的微信：主窗口、点「发送表情(Alt+E)」会弹出的面板、点「自定义表情」会换出的内容。"""

    def __init__(self, with_button=True, with_tab=True, switch_ok=True, orig="某个群"):
        CLICKS.clear()
        self.root_kids: list = []
        self.keys: list[str] = []
        self.chat = orig
        self.switch_ok = switch_ok
        self.wx_calls: list = []
        self.panel = Ctl("WindowControl", "mmui::EmojiPanel", "表情面板", rect=(100, 200, 500, 600), hwnd=22)
        tab = Ctl("TabItemControl", "mmui::XTabBarItem", "自定义表情", on_click=self.show_single, rect=(150, 210, 190, 250))
        near = [Ctl("TabItemControl", "mmui::EmoticonToolbarItem", "搜索表情"), Ctl("TabItemControl", "mmui::EmoticonToolbarItem", "默认表情"), Ctl("TabItemControl", "mmui::EmoticonToolbarItem", "收藏"),
                Ctl("ButtonControl", "mmui::XButton", "自定义表情")]  # 名字不完全一样的、或名字一样但不是 TabItemControl 的，都不能点（最后一个排在真正那一栏前面）
        self.panel.kids = near + ([tab] if with_tab else [])
        self.panel.kids.append(Ctl("ListItemControl", "mmui::EmojiItem", "[微笑]"))  # 表情本身：绝不能点
        btn = Ctl("ButtonControl", "mmui::XButton", "发送表情(Alt+E)", on_click=self.show_panel, rect=(10, 500, 40, 530))
        self.main = Ctl("WindowControl", "mmui::MainWindow", "微信", hwnd=11, kids=[
            Ctl("ListItemControl", "mmui::ChatTextItem", "[表情] 一条聊天记录（名字里也带表情二字）"),
            Ctl("PaneControl", "mmui::ChatInputField", "", kids=[Ctl("ButtonControl", "mmui::XButton", "发送文件"), Ctl("ButtonControl", "mmui::XButton", "发送收藏"), Ctl("ButtonControl", "mmui::XButton", "表情")] + ([btn] if with_button else [])),
        ])
        self.root_kids = [self.main]

    def show_panel(self):
        self.root_kids.append(self.panel)

    def show_single(self):
        self.panel.kids.append(Ctl("ListItemControl", "mmui::EmojiItem", "自己添加的一张"))

    # ---- 桩的 wxauto4 ----
    def lib(self):
        world = self

        class Wx:
            def __init__(self, debug=False, resize=True, **k):
                world.wx_calls.append(("init", {"resize": resize}))

            def ChatInfo(self):
                world.wx_calls.append(("ChatInfo",))
                return {"chat_name": world.chat}

            def ChatWith(self, who, **k):
                world.wx_calls.append(("ChatWith", who))
                if world.switch_ok or who != "文件传输助手":
                    world.chat = who

            def __getattr__(self, name):
                raise AssertionError(f"探查命令不该碰 wx.{name}")
        return SimpleNamespace(WeChat=Wx)

    def ui(self):
        world = self
        return SimpleNamespace(
            ControlFromHandle=lambda h: world.main,
            GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: list(world.root_kids)),
            SendKeys=lambda text: world.keys.append(text))


def grab(rect, path: Path):
    path.write_bytes(b"PNG:" + repr(rect).encode())


def probe(tmp_path, world, **kw):
    return wxprobe.run(tmp_path / "out", lib=world.lib(), ui=world.ui(), hwnd_finder=lambda: 11, grab=grab, sleep=lambda s: None, **kw)


def test_probe_walks_through_the_steps_writes_the_structure_and_screenshots_and_only_clicks_the_two_allowed_controls(tmp_path):
    w = World()
    assert probe(tmp_path, w) == 0
    out = tmp_path / "out"
    for f in ("probe.log", "result.json", "main-before-panel.txt", "emoji-panel.txt", "emoji-panel.png", "emoji-single.txt", "emoji-single.png"):
        assert (out / f).exists(), f
    res = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert res["ok"] is True and res["clicked"] == ["open-panel", "single-tab"] and res["stopped_at"] == ""
    assert CLICKS == ["发送表情(Alt+E)", "自定义表情"]  # 全程只点了这两个，一个表情都没点（「发送收藏」「表情」这些名字不完全一样的、聊天记录里带「表情」的那条、面板里的「[微笑]」都没碰）
    assert w.keys == ["{Esc}"] and w.chat == "某个群"  # 关了面板、切回了原来的会话
    assert [c for c in w.wx_calls if c[0] == "ChatWith"] == [("ChatWith", "文件传输助手"), ("ChatWith", "某个群")]
    assert ("init", {"resize": False}) in w.wx_calls  # 不改微信窗口的大小
    # 界面结构：层级缩进 + 控件类型 | 类名 | 名字 | 编号 | 位置
    before = (out / "main-before-panel.txt").read_text(encoding="utf-8")
    assert "ButtonControl | mmui::XButton | 发送表情(Alt+E) |  | 10,500,40,530" in before and "表情面板" not in before
    panel = (out / "emoji-panel.txt").read_text(encoding="utf-8")
    assert "表情面板" in panel and "[微笑]" in panel and "自定义表情" in panel and "自己添加的一张" not in panel
    single = (out / "emoji-single.txt").read_text(encoding="utf-8")
    assert "自己添加的一张" in single
    assert (out / "emoji-panel.png").read_bytes().startswith(b"PNG:(100, 200, 500, 600)")  # 截的是弹出的面板区域
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "点击 open-panel：「发送表情(Alt+E)」" in log and "点击 single-tab：「自定义表情」" in log and "已按 Esc 关掉表情面板" in log


def test_it_never_calls_anything_that_sends_and_the_wx_wrapper_refuses_them(tmp_path):
    w = World()
    probe(tmp_path, w)
    assert {c[0] for c in w.wx_calls} <= {"init", "ChatInfo", "ChatWith"}
    wx = wxprobe.SafeWx(w.lib().WeChat(), [])
    for name in wxprobe.FORBIDDEN_WX + ("AddListenChat", "GetAllMessage", "SwitchToContact"):
        with pytest.raises(PermissionError):
            getattr(wx, name)
    src = (ROOT / "mb" / "wxprobe.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(SendMsg|SendFiles|SendTypingText|quote|forward)\(", src)  # 代码里根本没有这些调用


def test_a_click_on_anything_but_the_two_allowed_controls_is_refused(tmp_path):
    log = []
    CLICKS.clear()
    emoji = Ctl("ListItemControl", "mmui::EmojiItem", "[微笑]")
    for purpose in ("open-panel", "single-tab", "send", ""):
        with pytest.raises(PermissionError):
            wxprobe.guarded_click(emoji, purpose, log.append)
    for bad in ("表情包搜索", "表情", "发送收藏", "发送表情", "发送表情(Alt+E) "):  # 名字里有「表情」但不是那个按钮：包含式匹配不行，要完全一样
        with pytest.raises(PermissionError):
            wxprobe.guarded_click(Ctl("ButtonControl", "mmui::XButton", bad), "open-panel", log.append)
    assert CLICKS == []  # 被拒的一个都没点出去
    ok = Ctl("ButtonControl", "mmui::XButton", "发送表情(Alt+E)")
    wxprobe.guarded_click(ok, "open-panel", log.append)
    assert CLICKS[-1] == "发送表情(Alt+E)"


def test_when_the_heart_tab_is_missing_it_stops_there_says_so_and_still_closes_the_panel(tmp_path):
    w = World(with_tab=False)
    assert probe(tmp_path, w) == 1
    res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert res["ok"] is False and res["stopped_at"].startswith("4 ") and "自定义表情" in res["why"]
    assert CLICKS == ["发送表情(Alt+E)"]  # 只开了面板，没有点任何别的
    assert w.keys == ["{Esc}"] and w.chat == "某个群"  # 收尾：关面板、切回
    assert (tmp_path / "out" / "emoji-panel.txt").exists() and not (tmp_path / "out" / "emoji-single.txt").exists()  # 面板的结构已经存下来了，方便对着改


def test_when_the_emoji_button_is_missing_nothing_is_clicked(tmp_path):
    w = World(with_button=False)
    assert probe(tmp_path, w) == 1
    res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert res["stopped_at"].startswith("3 ") and "找不到名字叫「发送表情(Alt+E)」的控件" in res["why"] and "发送文件" in res["why"] and "发送收藏" in res["why"]
    assert CLICKS == [] and w.keys == []  # 没点过任何东西、没开过面板：也就不用按 Esc
    assert w.chat == "某个群"
    assert (tmp_path / "out" / "main-before-panel.txt").exists()


def test_when_the_chat_does_not_switch_it_stops_at_step_two_and_goes_back(tmp_path):
    w = World(switch_ok=False)
    assert probe(tmp_path, w) == 1
    res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert res["stopped_at"].startswith("2 ") and "没切过去" in res["why"] and CLICKS == []
    assert w.chat == "某个群"


def test_when_wechat_cannot_be_reached_it_stops_at_step_one(tmp_path):
    w = World()

    def boom(**k):
        raise RuntimeError("没找到微信窗口")
    lib = SimpleNamespace(WeChat=boom)
    code = wxprobe.run(tmp_path / "out", lib=lib, ui=w.ui(), hwnd_finder=lambda: 11, grab=grab, sleep=lambda s: None)
    res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert code == 1 and res["stopped_at"].startswith("1 ") and "没找到微信窗口" in res["why"] and CLICKS == []


def test_the_command_does_not_take_the_single_instance_mutex_or_start_anything(monkeypatch, tmp_path):
    from mb import app
    monkeypatch.setattr(singleton, "acquire", lambda d: pytest.fail("探查命令不该取单实例互斥体"))
    monkeypatch.setattr(app, "Desktop", lambda b: pytest.fail("探查命令不该启动程序主体"))
    got = []
    monkeypatch.setattr(wxprobe, "main", lambda out: got.append(out) or 0)
    with pytest.raises(SystemExit) as ex:
        app.main(["--probe-wechat-emoji", str(tmp_path / "o")])
    assert ex.value.code == 0 and got == [str(tmp_path / "o")]
    with pytest.raises(SystemExit) as ex:
        app.main(["--probe-wechat-emoji"])
    assert ex.value.code == 2  # 没给输出目录：提示用法
    # 就算别的 PodMates 主程序正占着互斥体，探查也能跑（它不参与单实例）
    assert "probe" in (ROOT / "mb" / "app.py").read_text(encoding="utf-8")


def test_usage_is_written_in_the_module_docs():
    doc = wxprobe.__doc__
    assert "--probe-wechat-emoji" in doc and "emoji-single.png" in doc and "不点任何一个表情" in doc
