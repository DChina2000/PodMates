"""补四十七之四：微信里别人发的表情改成「截聊天里那一块」，不再试 A、B。

微信里表情点不开大图、也没法另存（真机上 A 报错、B 的右键菜单里没有「复制」），所以：
  · 表情（is_emotion）在看图开着、配了看图模型、「群友发的表情也看」开着时截屏，交给同一个看图流程，写「[表情：描述]」；
    截不到、截出来不对（被别的窗口盖住 / 太小 / 出界）写「[表情]」并记一条 warn，不重试。
  · 头像和名字那一侧不截：认得出画表情的子元素就截它，认不出退回整条消息的矩形再去掉头像那一侧。
  · 截之前窗口必须在屏幕上：开着「移到屏幕外」时先移回来，截完再移出去（和 _run(ui=True) 一样），不抢前台。
  · 截屏来的表情按 dhash 记看图缓存（完全相同才算同一张），普通图片、QQ 图片仍按 sha1。
  · 验证命令 --test-wechat-picture 的表情一步：往文件传输助手发一张自定义表情 → 找最新的表情 → 写子元素列表、按上面的办法截图。

全部用桩：假的控件树、假的窗口接口（WindowFromPoint 的桩）、假的截图函数，不碰真的微信、不截任何屏、不操作任何窗口。
"""
import io
import json
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

from PIL import Image, ImageDraw

from mb import wxshot
from mb.stickers import dhash
from test_round35 import logs
from test_round41 import setg, wx_feed
from test_round47a import FakeClip, FakeWeChat, FriendOtherMessage, fakes, result_of, vision_env  # noqa: F401  (fakes 是自动生效的桩)
from test_round47b import FriendEmotionMessage, vis_calls
from test_round5b import png

from mb import wxpicture
from mb.channels.wechat import WeChatChannel
from mb.winpos import Keeper

ROOT = Path(__file__).resolve().parent.parent


# ---------- 桩：控件树、窗口、截图 ----------
class Ctl:
    """一个界面控件：ClassName / Name / ControlTypeName / 矩形 / 子控件 / 自己那个窗口的句柄。"""

    def __init__(self, cls="", name="", type_="TextControl", rect=(0, 0, 10, 10), kids=None, hwnd=0):
        self.ClassName, self.Name, self.ControlTypeName = cls, name, type_
        self.rect, self._kids, self.NativeWindowHandle = rect, list(kids or []), hwnd
        self.BoundingRectangle = SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])

    def GetChildren(self):
        return list(self._kids)


MAIN_HWND = 11  # 主窗口方式：消息在微信主窗口里
CHAT_HWND = 33  # 独立窗口方式：消息在单独的聊天窗口里
MENU_HWND = 77  # 微信自己的小菜单（头像旁边的「@ 某某」「拍一拍」）：类名 Qt51514QWindowToolSaveBits
MENU_CLS = "Qt51514QWindowToolSaveBits"
POPOVER_HWND = 88  # 表情面板（mmui::EmoticonPopover）


def message_control(bubble_kids=None, row=(300, 200, 760, 280), head=True, hwnd=MAIN_HWND):
    """一条消息：mmui::ChatItemView →（头像）+ 气泡 → 气泡里的东西。hwnd 是它所在的那个顶层窗口。"""
    kids = [Ctl("mmui::ChatBubbleItemView", "", "PaneControl", (360, 200, 760, 280), bubble_kids or [])]
    if head:
        kids.insert(0, Ctl("mmui::ContactHeadView", "", "PaneControl", (300, 200, 344, 244)))
    return Ctl("mmui::ChatItemView", "", "ListItemControl", row, kids, hwnd=hwnd)


class FakeApi:
    """窗口接口桩：显示器一块；covered 里的点被「别的窗口」盖住（返回 0，不是微信的），其余的点都是消息所在的那个窗口；
    没给 owner 的查点当作在 top 那个窗口里。windows() 给的是微信进程的顶层窗口表（菜单、聊天窗口……）。"""

    def __init__(self, mons=None, covered=(), owner=MAIN_HWND, top=None, windows=None, visible=None, iconic=()):
        self.mons, self.covered, self.owner = mons or [(0, 0, 1920, 1080)], set(covered), owner
        self.iconic = set(iconic)  # 最小化的窗口：IsWindowVisible 仍为真，IsIconic 为真
        self.top = top or {}  # (x, y) → 那一点最上面的顶层窗口
        # 默认只有消息所在的那个窗口（主窗口）；测试要菜单、聊天窗口就自己给 windows
        self.win_list = list(windows if windows is not None else [win(owner, "Qt51514QWindowIcon", "WeChat", (0, 0, 1000, 700))])
        self.visible = dict(visible or {})
        self.calls = []
        self.posts = []  # (hwnd, 消息号)

    def begin(self):
        pass

    def monitors(self):
        return list(self.mons)

    def root_of(self, hwnd):
        return self.owner if hwnd else 0

    def top_at(self, x, y):
        self.calls.append((x, y))
        if (x, y) in self.covered:
            return 0
        return self.top.get((x, y), self.owner)

    def is_wechat(self, hwnd):
        return bool(hwnd) and hwnd in {w["hwnd"] for w in self.win_list}

    def class_of(self, hwnd):
        return next((w.get("class") for w in self.win_list if w["hwnd"] == hwnd), "")

    def windows(self):
        return [dict(w, visible=self.visible.get(w["hwnd"], w.get("visible", True))) for w in self.win_list]

    def is_visible(self, hwnd):
        return bool(self.visible.get(hwnd, next((w.get("visible", True) for w in self.win_list if w["hwnd"] == hwnd), False)))

    def is_iconic(self, hwnd):
        return hwnd in self.iconic

    def post_key(self, hwnd, vk):
        self.posts.append((hwnd, "key", vk))

    def post_close(self, hwnd):
        self.posts.append((hwnd, "close"))

    def rect(self, hwnd):
        return next((tuple(w.get("rect") or (0, 0, 0, 0)) for w in self.win_list if w["hwnd"] == hwnd), (0, 0, 0, 0))

    def find_main(self, visible_only=True):
        return self.owner


def win(hwnd, cls=MENU_CLS, title="Weixin", rect=(540, 250, 750, 374), visible=True):
    """微信进程的一个顶层窗口（windows() / result.json 里的一行）。"""
    return {"hwnd": hwnd, "class": cls, "title": title, "rect": rect, "visible": visible}


def wx_windows(extra=(), owner=MAIN_HWND):
    """微信进程的窗口表：主窗口 + 别人一个独立聊天窗口 + extra 里的（菜单、表情面板……）。"""
    return [win(owner, "Qt51514QWindowIcon", "WeChat", (0, 0, 1000, 700)),
            win(CHAT_HWND, "Qt51514QWindowIcon", "OceanFamily", (1000, 0, 1600, 700))] + list(extra)


def grab_shot(color=(240, 240, 240), jitter=0):
    """假的截图：浅灰底上画一块表情（底色和四角不同，裁图才找得到内容）。
    带一点别的角：像素有细微差别时 dhash 不变、sha1 变。"""
    calls = []

    def grab(rect):
        calls.append(tuple(rect))
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        im = Image.new("RGB", (w, h), color)
        d = ImageDraw.Draw(im)
        d.rectangle((max(2, w // 4), max(2, h // 4), max(3, w * 3 // 4), max(3, h * 3 // 4)), fill=(220, 60, 60))
        if jitter:
            im.putpixel((0, 0), (jitter % 255, 0, 0))
        return im
    grab.calls = calls
    return grab


def shot_env(e, api=None, grab=None):
    """把微信渠道换成会截屏的桩：api 和 grab 都换成假的（两帧之间也不真等）。"""
    ch = vision_env(e)
    ch.keeper, ch.keeper_on = None, False
    ch.window_api = api or FakeApi()
    ch.stk_grab = grab or grab_shot()
    ch.stk_sleep = lambda _s: None
    return ch


def emotion(tmp_path, mid="e1", **kw):
    return FriendEmotionMessage(tmp_path, mid, **kw)


# ---------- 一、子元素认不出就退回整条消息的矩形 ----------
def test_the_emotion_leaf_is_found_in_the_message_by_its_name():
    face = Ctl("mmui::EmotionItemView", "动画表情", "ImageControl", (400, 210, 472, 282))
    ctl = message_control([Ctl("mmui::ChatTextItemView", "", "TextControl", rect=(360, 200, 460, 240)),
                           Ctl("mmui::EmotionItemView", "", "ImageControl", (400, 200, 760, 280), [face])])
    box, why = wxshot.emotion_box(ctl)
    assert box is face and why.startswith("a ")


def test_a_bare_image_inside_the_bubble_is_used_when_no_name_says_emotion():
    pic = Ctl("mmui::ImageItemView", "", "ImageControl", (400, 210, 472, 282))
    ctl = message_control([pic])
    box, why = wxshot.emotion_box(ctl)
    assert box is pic and why.startswith("c ")


def test_when_no_child_looks_like_an_emotion_the_whole_row_minus_the_avatar_is_used():
    ctl = message_control()  # 气泡里什么都没有
    assert wxshot.emotion_box(ctl)[0] is None
    r, why = wxshot.row_rect(ctl)
    assert r == (348, 200, 760, 280) and why.startswith("整条消息的矩形去掉头像")  # 头像右边 344 + 4
    assert wxshot.capture(ctl, FakeApi(), grab_shot(), [(0, 0, 1920, 1080)]).source == "row"

# ---------- 二、截图前的检查 ----------
def test_a_covered_area_is_not_screenshotted():
    api = FakeApi(covered={(560, 265)})  # 矩形中心被别的窗口盖住
    ctl = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))])
    try:
        wxshot.capture(ctl, api, grab_shot(), api.mons)
        assert False, "该抛错"
    except wxshot.ShotError as ex:
        assert "被别的窗口盖住了" in str(ex)


def test_a_tiny_or_offscreen_area_is_not_screenshotted():
    api = FakeApi()
    tiny = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 410, 220))])
    for ctl, want in ((tiny, "太小"), (message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (900, 210, 1200, 320))]), "跑出了所有显示器")):
        try:
            wxshot.capture(ctl, api, grab_shot(), [(0, 0, 1000, 700)])
            assert False, "该抛错"
        except wxshot.ShotError as ex:
            assert want in str(ex) and ex.checks.get("why")  # 验证命令要写出来是哪一项没过


def test_every_probe_point_must_belong_to_wechat():
    """四个角里有一个不是微信的窗口：整块就不截（宁可这一张不看）。"""
    api = FakeApi(covered={(403, 213)})
    checks = wxshot.check_rect((400, 210, 720, 320), api, api.mons, owner=MAIN_HWND)
    assert not all(p["owner"] for p in checks["points"]) and len(checks["points"]) == 2
    api2 = FakeApi()
    full = wxshot.check_rect((400, 210, 720, 320), api2, api2.mons, owner=MAIN_HWND)
    assert all(p["owner"] for p in full["points"]) and len(full["points"]) == 5


def test_a_probe_point_that_is_another_wechat_windows_is_covered_too():
    """查点上是微信的另一个窗口（菜单、别的聊天窗口）：以前「是微信进程就算」，现在算被盖住。"""
    api = FakeApi(windows=wx_windows([win(MENU_HWND)]), top={(560, 265): MENU_HWND})
    checks = wxshot.check_rect((400, 210, 720, 320), api, api.mons, owner=MAIN_HWND)
    assert not all(p["owner"] for p in checks["points"])
    assert checks["points"][0]["wechat"] is True and checks["points"][0]["hwnd"] == MENU_HWND
    assert checks["points"][0]["class"] == MENU_CLS


def test_no_message_window_means_no_shot():
    """拿不到消息所在的窗口：按截不到处理，不退回「是微信进程就算」。"""
    ctl = Ctl("mmui::ChatItemView", "", "ListItemControl", (300, 200, 760, 280),
              [Ctl("mmui::ChatBubbleItemView", "", "PaneControl", (360, 200, 760, 280),
                   [Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))])])  # 没有句柄
    grab = grab_shot()
    with pytest.raises(wxshot.ShotError) as ex:
        wxshot.capture(ctl, FakeApi(), grab, [(0, 0, 1920, 1080)])
    assert "拿不到这条消息所在的窗口" in str(ex.value) and grab.calls == []
    assert ex.value.checks.get("owner") == 0


def test_the_window_of_the_message_is_the_root_of_its_own_handle():
    """独立窗口方式：消息在别人的聊天窗口里，就从它自己的句柄往上取那个窗口，不是主窗口。"""
    api = FakeApi(owner=CHAT_HWND, windows=wx_windows(owner=CHAT_HWND))
    ctl = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))], hwnd=CHAT_HWND)
    assert wxshot.owner_hwnd(ctl, api) == CHAT_HWND
    assert wxshot.capture(ctl, api, grab_shot(), api.mons).checks["owner"] == CHAT_HWND


# ---------- 三、截之前先关掉微信自己的小菜单 ----------
def test_a_menu_over_the_area_is_closed_with_esc_and_then_the_shot_is_taken():
    face = Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))
    api = FakeApi(windows=wx_windows([win(MENU_HWND)]))
    api.visible[MENU_HWND] = True

    def post_key(hwnd, vk):
        api.posts.append((hwnd, "key", vk))
        api.visible[hwnd] = False  # Esc 一按菜单就关了
    api.post_key = post_key
    shot = wxshot.capture(message_control([face]), api, grab_shot(), api.mons)
    assert [p[:2] for p in api.posts] == [(MENU_HWND, "key")]  # 只对菜单发了 Esc，关掉就没再发 WM_CLOSE
    assert shot.checks["menus"][0]["how"] == "esc" and shot.checks["menus"][0]["closed"] is True


def test_esc_does_not_close_the_menu_so_close_is_sent_and_the_shot_is_taken():
    face = Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))
    api = FakeApi(windows=wx_windows([win(MENU_HWND)]))
    api.visible[MENU_HWND] = True

    def post_close(hwnd):
        api.posts.append((hwnd, "close"))
        api.visible[hwnd] = False  # WM_CLOSE 才关得掉
    api.post_close = post_close
    shot = wxshot.capture(message_control([face]), api, grab_shot(), api.mons)
    assert [(p[0], p[1]) for p in api.posts] == [(MENU_HWND, "key"), (MENU_HWND, "close")]
    assert shot.checks["menus"][0]["how"] == "close" and shot.checks["menus"][0]["closed"] is True


def test_a_menu_that_neither_esc_nor_close_can_close_it_means_no_shot():
    face = Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))
    api = FakeApi(windows=wx_windows([win(MENU_HWND)]))
    grab = grab_shot()
    with pytest.raises(wxshot.ShotError) as ex:
        wxshot.capture(message_control([face]), api, grab, api.mons)
    msg = str(ex.value)
    assert str(MENU_HWND) in msg and MENU_CLS in msg and "540,250,750,374" in msg  # warn 里写清句柄、类名、矩形
    assert [(p[0], p[1]) for p in api.posts] == [(MENU_HWND, "key"), (MENU_HWND, "close")]  # 两种都试了
    assert grab.calls == []  # 没截


def test_only_the_menu_window_is_posted_to_never_the_main_or_a_chat_window():
    face = Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))
    small = win(99, MENU_CLS, "Weixin", (1500, 800, 1560, 860))  # 和矩形不重叠的小窗：不碰
    api = FakeApi(windows=wx_windows([win(MENU_HWND), small]))
    with pytest.raises(wxshot.ShotError):
        wxshot.capture(message_control([face]), api, grab_shot(), api.mons)
    assert {p[0] for p in api.posts} == {MENU_HWND}  # 主窗口、聊天窗口、不重叠的小窗一个都没发


def test_the_sticker_panel_being_open_means_no_small_window_is_touched_at_all():
    """表情面板开着的时候（PodMates 自己在操作它）：一个小窗都不关，这张按截不到处理。"""
    face = Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))
    api = FakeApi(windows=wx_windows([win(MENU_HWND), win(POPOVER_HWND, "mmui::EmoticonPopover", "Weixin", (74, 185, 588, 699))]))
    grab = grab_shot()
    with pytest.raises(wxshot.ShotError) as ex:
        wxshot.capture(message_control([face]), api, grab, api.mons)
    assert "表情面板开着" in str(ex.value) and api.posts == [] and grab.calls == []


# ---------- 三、缓存键 ----------
def test_a_screenshot_whose_pixels_differ_a_little_keeps_the_same_dhash():
    a = grab_shot(jitter=0)((0, 0, 216, 72))
    b = grab_shot(jitter=200)((0, 0, 216, 72))

    def png_of(im):
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    assert png_of(a) != png_of(b)  # 像素有细微差别
    assert wxshot.cache_key(png_of(a)) == wxshot.cache_key(png_of(b))  # dhash 相同
    assert wxshot.cache_key(png_of(a)).startswith("dh:") and len(wxshot.cache_key(png_of(a))) == 19


# ---------- 四、别人发的表情：截屏 → [表情：…]，不调 A、B ----------
def ctx_texts_of(e, who="群A"):
    return [x.text for x in e.engine.rooms[f"wechat:{who}"].ctx]


def is_sticker_text(text: str) -> bool:
    return text.startswith("[表情：") and "一只猫" in text


def test_an_emotion_from_someone_else_is_screenshotted_described_and_never_downloaded(env, tmp_path):
    async def body(e):
        ch = shot_env(e)
        face = Ctl("mmui::EmotionItemView", "动画表情", "ImageControl", (400, 210, 472, 282))
        m = emotion(tmp_path)
        m.control = message_control([face])
        await wx_feed(e, ch, m)
        assert is_sticker_text(ctx_texts_of(e)[0]) and len(vis_calls(e)) == 1
        assert m.options == [] and getattr(m.control, "a_calls", 0) == 0  # 没试 A、B
        assert logs(e, "表情已截屏")
    env.run(body)


def test_a_screenshot_blocked_by_another_window_is_written_as_the_plain_marker_with_a_warning(env, tmp_path):
    async def body(e):
        grab = grab_shot()
        ch = shot_env(e, api=FakeApi(covered={(560, 265)}), grab=grab)
        m = emotion(tmp_path)
        m.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))])
        await wx_feed(e, ch, m)
        assert ctx_texts_of(e) == ["[表情]"] and vis_calls(e) == [] and grab.calls == []
        assert logs(e, "截不了") and logs(e, "不是微信的窗口")
    env.run(body)


def test_nothing_is_screenshotted_when_a_switch_is_off(env, tmp_path):
    async def body(e):
        grab = grab_shot()
        ch = shot_env(e, grab=grab)
        m = emotion(tmp_path)
        m.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))])
        setg(e, "vision_stickers", False)  # 「群友发的表情也看」关着
        await wx_feed(e, ch, m)
        setg(e, "vision_stickers", True)
        setg(e, "vision_enabled", False)  # 看图关着
        n = emotion(tmp_path, "e2")
        n.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 720, 320))])
        await wx_feed(e, ch, n)
        assert ctx_texts_of(e) == ["[表情]", "[表情]"] and grab.calls == [] and vis_calls(e) == []
    env.run(body)


# ---------- 五、窗口在屏幕外：截之前移回来，截完移出去 ----------
class KeepApi(FakeApi):
    """窗口接口桩：还能当 Keeper 用（记下移进移出的次数）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.hwnd, self.rect_, self.off = 4242, (300, 100, 1300, 800), False
        self.moves = []

    def work_area(self):
        return (0, 0, 1920, 1040)

    def exists(self, h):
        return h == self.hwnd

    def is_iconic(self, h):
        return False

    def is_zoomed(self, h):
        return False

    def show(self, h, how):
        pass

    def rect(self, h):
        return self.rect_

    def move(self, h, x, y, w, hh):
        self.moves.append((x, y, w, hh))
        self.rect_ = (x, y, x + w, y + hh)
        self.off = not (0 <= x and 0 <= y and x + w <= 1920 and y + hh <= 1080)  # 移到所有显示器外面去了

    def foreground(self):
        return 1

    def set_foreground(self, h):
        pass

    def zprev(self, h):
        return 0

    def set_zafter(self, h, after):
        pass

    def is_wechat(self, h):
        return True

    def find_main(self, visible_only=True):
        return self.hwnd


def test_the_window_is_moved_back_before_the_shot_and_out_again_afterwards(env, tmp_path):
    async def body(e):
        api = KeepApi()
        ch = shot_env(e, api=api)
        ch.keeper = Keeper(api, e.core.data / "wechat_window.json")
        ch.keeper_on = True
        setg(e, "wechat_offscreen", True)
        ch.keeper.hide()  # 窗口先被移到屏幕外
        grab = grab_shot()

        def shot(rect):
            assert not ch.keeper.off  # 截的时候窗口在屏幕上
            return grab(rect)
        ch.stk_grab = shot
        m = emotion(tmp_path)
        m.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))])
        await wx_feed(e, ch, m)
        assert is_sticker_text(ctx_texts_of(e)[0]) and len(vis_calls(e)) == 1
        assert ch.keeper.off  # 截完又移出去了
        assert len(api.moves) == 3  # 移出去、移回来、再移出去
    env.run(body)


def test_a_window_already_brought_back_is_not_sent_offscreen_by_the_shot(env, tmp_path):
    """开着「移到屏幕外」、但窗口这会儿已经在屏幕上（外层 _run 刚移回来）：截完不把它移出去，留给外层收尾。"""
    async def body(e):
        api = KeepApi()
        ch = shot_env(e, api=api)
        ch.keeper = Keeper(api, e.core.data / "wechat_window.json")
        ch.keeper_on = True
        setg(e, "wechat_offscreen", True)
        ch.keeper.hide()
        ch.keeper.bring()  # 外层已经移回来了
        ch.stk_grab = grab_shot()
        m = emotion(tmp_path)
        m.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))])
        assert ch._shot_emotion(m)  # 截到了
        assert not ch.keeper.off and len(api.moves) == 2  # 只有前面的移出去、移回来，截图本身没再挪窗口
    env.run(body)


def test_the_shot_is_taken_without_moving_the_window_while_offscreen_is_off(env, tmp_path):
    async def body(e):
        api = KeepApi()
        ch = shot_env(e, api=api)
        ch.keeper = Keeper(api, e.core.data / "wechat_window.json")
        ch.keeper_on = True
        setg(e, "wechat_offscreen", False)
        m = emotion(tmp_path)
        m.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))])
        await wx_feed(e, ch, m)
        assert is_sticker_text(ctx_texts_of(e)[0]) and api.moves == []
    env.run(body)


# ---------- 六、dhash 缓存：同一张表情截两次只调一次模型；普通图片仍按 sha1 ----------
def test_the_same_emotion_screenshotted_twice_is_described_once(env, tmp_path):
    async def body(e):
        ch = shot_env(e)
        for i, jitter in enumerate((0, 137)):  # 两次截图像素有细微差别
            ch.stk_grab = grab_shot(jitter=jitter)
            m = emotion(tmp_path, f"e{i}")
            m.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))])
            await wx_feed(e, ch, m)
        assert [is_sticker_text(t) for t in ctx_texts_of(e)] == [True, True]
        assert len(vis_calls(e)) == 1  # 第二次命中 dhash 缓存，没再调模型
        rows = e.core.db.rows("SELECT sha FROM vis_cache")
        assert len(rows) == 1 and rows[0]["sha"].startswith("dh:") and len(rows[0]["sha"]) == 19
    env.run(body)


def test_a_picture_is_still_cached_by_sha1(env, tmp_path):
    async def body(e):
        ch = shot_env(e)
        m = FriendOtherMessage(tmp_path, "p1")
        await wx_feed(e, ch, m)
        assert ctx_texts_of(e)[0].startswith("[图片：") and m.control.a_calls == 1
        row = e.core.db.rows("SELECT sha FROM vis_cache")[0]["sha"]
        assert not row.startswith("dh:") and len(row) == 40  # sha1
    env.run(body)


# ---------- 七、微信图片仍走 A、B ----------
def test_wechat_pictures_still_use_A_and_B(env, tmp_path):
    async def body(e):
        ch = shot_env(e)
        grab = ch.stk_grab
        m = FriendOtherMessage(tmp_path, "p1", a_fail=True)
        FakeClip.dump = {"49645": png(3)}
        await wx_feed(e, ch, m)
        assert m.control.a_calls == 1 and m.options == ["复制"] and ctx_texts_of(e)[0].startswith("[图片：")
        assert grab.calls == []  # 图片不截屏
    env.run(body)


# ---------- 八、验证命令的表情一步 ----------
class EmoWeChat(FakeWeChat):
    """文件传输助手里有一条表情消息（自己发的），控件里有画表情的子元素。"""

    def SendFiles(self, filepath, who=None, **k):
        super().SendFiles(filepath, who=who, **k)
        emo = FriendEmotionMessage(self.tmp, "emo1", data=png(3))
        emo.attr = "self"
        emo.control = message_control([Ctl("mmui::EmotionItemView", "动画表情", "ImageControl", (400, 210, 472, 282))])
        self.msgs = [self.msgs[0], emo, self.msgs[1]]


def test_the_command_writes_the_child_list_the_checks_and_the_shot_files(tmp_path):
    out = tmp_path / "out"
    w = EmoWeChat(tmp_path)
    api, grab = FakeApi(), grab_shot()
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=api, grab=grab,
                              hwnd_finder=lambda: 11) == 0
    res = result_of(out)
    emo = res["emotion"]
    assert emo["found"] is True and emo["tree"]
    assert [x["class"] for x in emo["tree"]] == ["mmui::ContactHeadView", "mmui::ChatBubbleItemView", "mmui::EmotionItemView"]
    assert {"depth", "class", "name", "type", "rect", "children"} <= set(emo["tree"][0])
    assert emo["shot"] == "emotion-shot.png" and (out / "emotion-shot.png").read_bytes()[:4] == b"\x89PNG"
    assert emo["row"] == "emotion-row.png" and (out / "emotion-row.png").exists()
    assert emo["checks"]["size_ok"] and emo["checks"]["inside"] and len(emo["checks"]["points"]) == 5
    assert emo["box"]["class"] == "mmui::EmotionItemView" and emo["source"] == "emotion"  # 写明选中的是哪个子元素
    assert emo["dhash"].startswith("dh:") and emo["crop"] and emo["size"] and emo["size"] != [72, 72]  # 裁到表情本身，比子元素小一圈
    assert emo["background"] and emo["frames"] and emo["moving"] is False  # 三帧一样：静图
    assert "emotion-shot.png" in (out / "probe.log").read_text(encoding="utf-8")


def test_the_command_returns_zero_when_there_is_no_emotion(tmp_path):
    out = tmp_path / "out"
    w = FakeWeChat(tmp_path)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=FakeApi(), grab=grab_shot(),
                              hwnd_finder=lambda: 11) == 0
    res = result_of(out)
    assert res["ok"] is True and res["emotion"] == {"found": False, "note": "没有表情消息"}


# ---------- 九、说明和词典 ----------
def test_the_guides_say_the_sticker_is_screenshotted_and_looked_at_but_not_when_covered():
    cn = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    assert "截聊天里这条表情所在的那一块" in cn and "拿不到那个窗口就不看" in cn and "动图只看截下来的那一帧" not in cn
    assert "先关掉盖在那块上的微信自己的小菜单" in cn and "关不掉、或者被别的窗口盖住就不截" in cn
    assert "裁到表情本身" in cn and "动图连截三帧" in cn
    assert "emotion-shot.png" in cn and "emotion-row.png" in cn and "往下三层子元素" in cn
    assert "menu-before.png" in cn and "menu-after.png" in cn and "`menu`" in cn
    assert "screenshots the part of the chat" in en and "closes WeChat" in en and "is not screenshotted" in en
    assert "cropped down to the sticker itself" in en and "three frames" in en
    assert "emotion-shot.png" in en and "emotion-row.png" in en
    assert "menu-before.png" in en and "menu-after.png" in en and "`menu`" in en
    # 面板上看得到的说明（设置项的一行小字）也跟着说了截屏和裁到表情本身，词典两种语言都有
    import json
    from mb import settings
    s = next(x for x in settings.SCHEMA if x["key"] == "vision_stickers")
    assert "截聊天里那一块" in s["help"] and "裁到表情本身" in s["help"]
    assert s["help"] in json.loads((ROOT / "web/i18n/en.json").read_text(encoding="utf-8"))
    assert "小菜单" in s["help"]  # 面板上的小字也说了截之前先关微信自己的小菜单


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有 user32")
def test_winapi_does_not_change_the_shared_user32_prototypes():
    """ctypes.windll.user32 是全进程共用的：WinApi 不能在上面改 argtypes（wxauto4 / uiautomation 用 c_long 调 GetAncestor，
    改了以后真机上连不上微信：argument 2: TypeError: 'c_long' object cannot be interpreted as an integer）。"""
    import ctypes
    from mb.winpos import WinApi
    u = ctypes.windll.user32
    before = (u.GetAncestor.argtypes, u.WindowFromPoint.argtypes)
    WinApi()
    assert (u.GetAncestor.argtypes, u.WindowFromPoint.argtypes) == before
    assert u.GetAncestor(ctypes.c_void_p(0), ctypes.c_long(2)) == 0  # 别人照旧用 c_long 调得通（句柄 0：只查不动任何窗口）
