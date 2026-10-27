"""微信界面里品牌名叫「WeChat」（而不是「微信」）时 wxauto4 也要能连上；连不上时报错里列出实际找到的标签名。全部用桩，不连微信。"""
import types

import pytest

from mb.channels import wechat
from mb.channels.wechat import WeChatChannel, apply_brand_compat, tab_names


class Elem:
    """UI 自动化元素的桩：只有名字、类名、控件类型。"""

    def __init__(self, name, cls, ctype=50000):
        self.CurrentName, self.CurrentClassName, self.CurrentControlType = name, cls, ctype


def fake_ua():
    """一个最小的 uiautomation 模块：Control 的 Name / ClassName 是只读属性，Name 读元素的名字（和 wxauto4 里一样的结构）。"""
    class Control:
        def __init__(self, element=None, **props):
            self._element, self.props = element, props

        @property
        def Element(self):
            return self._element

        @property
        def Name(self):
            """Property Name."""
            return self.Element.CurrentName or ""

        @property
        def ClassName(self):
            return self.Element.CurrentClassName

    class ButtonControl(Control):
        pass
    return types.SimpleNamespace(Control=Control, ButtonControl=ButtonControl)


def test_brand_name_variants_read_as_wechat_for_tabs_and_main_window():
    ua = fake_ua()
    assert apply_brand_compat(ua) is True
    for raw in ("微信", "WeChat", "Weixin"):
        assert ua.ButtonControl(element=Elem(raw, "mmui::XTabBarItem")).Name == "微信"  # 导航栏的标签
        assert ua.Control(element=Elem(raw, "mmui::MainWindow")).Name == "微信"  # 主窗口
        assert ua.Control(element=Elem(raw, "Qt51514QWindowIcon")).Name == "微信"
    # 其他控件不动：一个叫 WeChat 的联系人 / 聊天不能被当成品牌名
    assert ua.Control(element=Elem("WeChat", "mmui::ChatSessionCell")).Name == "WeChat"
    assert ua.ButtonControl(element=Elem("通讯录", "mmui::XTabBarItem")).Name == "通讯录"
    assert ua.Control(element=Elem(None, "mmui::XTabBarItem")).Name == ""
    assert ua.Control.Name.__doc__ == "Property Name."
    # 打过一次不重复打
    assert apply_brand_compat(ua) is False


def test_the_real_wxauto4_lookup_finds_both_names():
    ua = pytest.importorskip("wxauto4.uia.uiautomation")
    saved = ua.Control.Name, getattr(ua.Control, "_podmates_brand_compat", None)
    try:
        if hasattr(ua.Control, "_podmates_brand_compat"):
            del ua.Control._podmates_brand_compat
        # wxauto4 找「微信」按钮用的条件，原样：
        lookup = ua.ButtonControl(ClassName="mmui::XTabBarItem", Name="微信")
        cands = {n: ua.ButtonControl(element=Elem(n, "mmui::XTabBarItem", ua.ControlType.ButtonControl)) for n in ("微信", "WeChat", "通讯录")}
        assert lookup._CompareFunction(cands["微信"], 0) and not lookup._CompareFunction(cands["WeChat"], 0)  # 补丁之前：只认中文名
        assert apply_brand_compat(ua)
        assert lookup._CompareFunction(cands["微信"], 0) and lookup._CompareFunction(cands["WeChat"], 0)  # 补丁之后：两种都认
        assert not lookup._CompareFunction(cands["通讯录"], 0)
        # 子类（按钮、窗口……）继承的是同一个 Name
        assert ua.WindowControl.Name is ua.Control.Name and ua.ButtonControl.Name is ua.Control.Name
        main = ua.WindowControl(ClassName="mmui::MainWindow", Name="微信")
        assert main._CompareFunction(ua.WindowControl(element=Elem("WeChat", "mmui::MainWindow", ua.ControlType.WindowControl)), 0)
    finally:
        ua.Control.Name = saved[0]
        if saved[1] is None:
            if hasattr(ua.Control, "_podmates_brand_compat"):
                del ua.Control._podmates_brand_compat
        else:
            ua.Control._podmates_brand_compat = saved[1]


class Node:
    def __init__(self, name, cls, kids=()):
        self.Name, self.ClassName, self._kids = name, cls, list(kids)

    def GetChildren(self):
        return self._kids


def test_tab_names_lists_what_is_actually_on_screen():
    tree = Node("WeChat", "mmui::MainWindow", [
        Node("导航", "mmui::XToolBar", [Node(n, "mmui::XTabBarItem") for n in ("WeChat", "通讯录", "收藏", "发现")]),
        Node("会话", "mmui::ChatSessionList", [Node("WeChat", "mmui::ChatSessionCell")])])
    ua = types.SimpleNamespace(ControlFromHandle=lambda h: tree if h == 42 else None)
    assert tab_names(ua, 42) == ["WeChat", "通讯录", "收藏", "发现"]
    assert tab_names(ua, 0) == []
    assert tab_names(types.SimpleNamespace(ControlFromHandle=lambda h: 1 / 0), 42) == []  # 出错不抛


def make(e):
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": []})
    ch = WeChatChannel(e.engine, acc)
    e.engine.add_bot(acc, ch)
    return ch


def test_diagnose_names_the_tabs_found(env, monkeypatch):
    async def body(e):
        ch = make(e)
        assert ch._diagnose() == ""  # 不是真的 wxauto4（测试里的模拟库）：不查
        ch.lib_real = True
        monkeypatch.setattr(wechat, "WinApi", lambda: types.SimpleNamespace(find_main=lambda: 42))
        monkeypatch.setattr(wechat, "tab_names", lambda ua, hwnd: ["WeChat", "通讯录", "收藏", "发现"] if hwnd == 42 else [])
        msg = ch._diagnose()
        assert "WeChat、通讯录、收藏、发现" in msg and "「微信」" in msg and "「WeChat」" in msg
        monkeypatch.setattr(wechat, "tab_names", lambda ua, hwnd: [])
        assert "没在微信窗口里找到导航栏的标签" in ch._diagnose()
        monkeypatch.setattr(wechat, "WinApi", lambda: (_ for _ in ()).throw(RuntimeError("坏了")))
        assert ch._diagnose() == ""  # 诊断本身出错也不影响报错
    env.run(body)


def test_connect_failure_message_includes_the_diagnosis(env, monkeypatch):
    async def body(e):
        class Boom:
            @staticmethod
            def WeChat(*a, **k):
                raise LookupError("Find Control Timeout: {'ClassName': 'mmui::XTabBarItem', 'Name': '微信'}")
        monkeypatch.setattr(WeChatChannel, "_diagnose", lambda self: "。微信界面里实际找到的导航标签：WeChat、通讯录、收藏、发现")
        ch = make(e)
        ch.lib = Boom
        ch.connect_retry_secs = 0.01
        await ch.start()
        assert ch.status == "error" and "Find Control Timeout" in ch.detail and "WeChat、通讯录、收藏、发现" in ch.detail
    env.run(body)


def test_load_lib_applies_the_compat(monkeypatch):
    seen = {}
    monkeypatch.setattr(wechat, "apply_brand_compat", lambda ua: seen.setdefault("ua", ua))
    fake_lib = types.ModuleType("wxauto4")
    fake_ua_mod = types.ModuleType("wxauto4.uia.uiautomation")
    import importlib
    real_import = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a: {"wxauto4": fake_lib, "wxauto4.uia.uiautomation": fake_ua_mod}.get(name) or real_import(name, *a))
    assert wechat.load_lib() is fake_lib and seen["ua"] is fake_ua_mod
