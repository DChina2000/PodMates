"""补四十七之六：微信头像上的右键菜单挡住表情截图。

2026-10-27 服务器上：微信聊天区里最新一条别人发的消息的头像旁边一直开着微信自己的一个右键菜单
（类名 Qt51514QWindowToolSaveBits，标题 Weixin，约 210×124，两项「@ 某某」「拍一拍」），截表情时把菜单一起截进去了，
看图模型就写成「图片上方写有文字『@…』和『拍一拍』」、动作说成「拍一拍」。原因是 check_rect / _covered 只看
「最顶层的窗口是不是微信进程的」——菜单也是微信进程的，所以算没被盖住。现在：
  · 截图前的检查只认消息所在的那个顶层窗口（从消息控件自己的句柄往上取 GA_ROOT；拿不到就不截）；
    微信自己的菜单、别的聊天窗口盖在上面都算被盖住；
  · 截之前先关掉盖在那块上的微信自己的小窗：只发 Esc，还看得见再发 WM_CLOSE，两种都不成就按截不到处理；
    只对菜单窗口发消息（不对主窗口 / 聊天窗口发按键）、不 SetForegroundWindow、不点鼠标；
    表情面板（mmui::EmoticonPopover）开着时一个小窗都不碰（PodMates 自己在操作它）；
  · 验证命令新加 menu 一节：一步步查清菜单是谁打开的、哪种办法关得掉。
全部用桩：假的窗口接口、假的控件树、PIL 画的假截图，不碰真的微信、不截任何屏、不操作任何窗口。
"""
from test_round47a import FakeWeChat, fakes, result_of  # noqa: F401  (fakes 是自动生效的桩)
from test_round47d import (CHAT_HWND, MAIN_HWND, MENU_CLS, MENU_HWND, POPOVER_HWND, Ctl, FakeApi,  # noqa: F401
                           grab_shot, message_control, shot_env, win, wx_windows)
from test_round35 import logs
from test_round41 import ctx_texts, wx_feed
from test_round47b import FriendEmotionMessage, vis_calls

from mb import wxpicture, wxshot


def emotion(tmp_path, mid="e1", **kw):
    return FriendEmotionMessage(tmp_path, mid, **kw)


def face_ctl(hwnd=MAIN_HWND, box=(400, 210, 472, 282)):
    """一条别人发的表情消息：画表情的子元素在 box，消息在自己的窗口 hwnd 里。"""
    return message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", box)], hwnd=hwnd)


# ---------- 一、查点落在微信的菜单窗口上：算被盖住，不截 ----------
def test_a_menu_window_over_the_sticker_is_covered_and_the_sticker_is_not_looked_at(env, tmp_path):
    async def body(e):
        grab = grab_shot()
        api = FakeApi(windows=wx_windows([win(MENU_HWND)]), top={(560, 265): MENU_HWND})
        api.visible[MENU_HWND] = False  # 菜单关不掉（Esc、WM_CLOSE 都没用）
        ch = shot_env(e, api=api, grab=grab)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))  # 中心 (560, 265) 正好被菜单盖住
        await wx_feed(e, ch, m)
        assert ctx_texts(e) == ["[表情]"] and vis_calls(e) == [] and grab.calls == []
        assert logs(e, "截不了") and logs(e, "微信的另一个窗口")
    env.run(body)


def test_another_chat_window_over_the_sticker_is_covered_too(env, tmp_path):
    async def body(e):
        grab = grab_shot()
        other = win(CHAT_HWND, "Qt51514QWindowIcon", "OceanFamily", (400, 200, 760, 300))
        api = FakeApi(windows=wx_windows([other]), top={(560, 265): CHAT_HWND})
        ch = shot_env(e, api=api, grab=grab)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert ctx_texts(e) == ["[表情]"] and vis_calls(e) == [] and grab.calls == []
        assert logs(e, "微信的另一个窗口") and not logs(e, "已截屏")
    env.run(body)


def test_a_sticker_in_the_messages_own_window_is_still_screenshotted(env, tmp_path):
    async def body(e):
        api = FakeApi(windows=wx_windows([]))  # 只有主窗口，没有菜单
        ch = shot_env(e, api=api)
        m = emotion(tmp_path)
        m.control = face_ctl()
        await wx_feed(e, ch, m)
        assert ctx_texts(e)[0].startswith("[表情：") and len(vis_calls(e)) == 1
        assert logs(e, "已截屏")
    env.run(body)


def test_a_sticker_in_another_chat_window_is_screenshotted_when_that_window_is_on_top(env, tmp_path):
    """独立窗口方式：消息在别人的聊天窗口里，那个窗口在最上面就照常截。"""
    async def body(e):
        api = FakeApi(owner=CHAT_HWND, windows=wx_windows(owner=CHAT_HWND))
        ch = shot_env(e, api=api)
        m = emotion(tmp_path)
        m.control = face_ctl(hwnd=CHAT_HWND)
        await wx_feed(e, ch, m)
        assert ctx_texts(e)[0].startswith("[表情：") and len(vis_calls(e)) == 1
    env.run(body)


# ---------- 二、拿不到消息所在的窗口：按截不到处理 ----------
def test_no_message_window_means_no_shot(env, tmp_path):
    async def body(e):
        grab = grab_shot()
        ch = shot_env(e, api=FakeApi(windows=wx_windows([])), grab=grab)
        m = emotion(tmp_path)
        m.control = face_ctl()
        m.control.NativeWindowHandle = 0  # 读不到消息控件所在的窗口
        await wx_feed(e, ch, m)
        assert ctx_texts(e) == ["[表情]"] and vis_calls(e) == [] and grab.calls == []
        assert logs(e, "拿不到这条消息所在的窗口")
    env.run(body)


# ---------- 三、截之前先关掉微信自己的小菜单 ----------
def _menu_api(**kw):
    """带一个盖在表情上的菜单的窗口接口：默认 Esc、WM_CLOSE 都关不掉（测试自己换掉 post_key / post_close）。"""
    api = FakeApi(windows=wx_windows([win(MENU_HWND)]), **kw)
    api.visible[MENU_HWND] = True
    return api


def test_esc_closes_the_menu_and_only_esc_is_sent(env, tmp_path):
    async def body(e):
        api = _menu_api()

        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False  # Esc 关掉了
        api.post_key = post_key
        ch = shot_env(e, api=api)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert ctx_texts(e)[0].startswith("[表情：")
        assert api.posts == [(MENU_HWND, "key", wxshot.VK_ESCAPE)]  # 只对菜单发了 Esc，没再发 WM_CLOSE
    env.run(body)


def test_esc_fails_but_close_works_and_the_sticker_is_screenshotted(env, tmp_path):
    async def body(e):
        api = _menu_api()

        def post_close(hwnd):
            api.posts.append((hwnd, "close"))
            api.visible[hwnd] = False  # 只有 WM_CLOSE 关得掉
        api.post_close = post_close
        ch = shot_env(e, api=api)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert ctx_texts(e)[0].startswith("[表情：")
        assert [(p[0], p[1]) for p in api.posts] == [(MENU_HWND, "key"), (MENU_HWND, "close")]
    env.run(body)


def test_a_menu_that_neither_esc_nor_close_closes_means_the_plain_marker_and_a_warning(env, tmp_path):
    async def body(e):
        api = _menu_api()  # 两种都没把窗口关掉
        grab = grab_shot()
        ch = shot_env(e, api=api, grab=grab)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert ctx_texts(e) == ["[表情]"] and vis_calls(e) == [] and grab.calls == []
        assert [(p[0], p[1]) for p in api.posts] == [(MENU_HWND, "key"), (MENU_HWND, "close")]
        line = next(x for x in logs(e, "截不了") if MENU_CLS in x)
        assert str(MENU_HWND) in line and "540,250,750,374" in line
    env.run(body)


def test_posts_only_ever_go_to_the_menu_window(env, tmp_path):
    """主窗口、聊天窗口、和矩形不重叠的小窗：一个都不碰。"""
    async def body(e):
        far = win(99, MENU_CLS, "Weixin", (1500, 800, 1560, 860))
        api = FakeApi(windows=wx_windows([win(MENU_HWND), far]))
        api.visible.update({MENU_HWND: True, 99: True})
        ch = shot_env(e, api=api)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert {p[0] for p in api.posts} == {MENU_HWND}
        assert 99 not in {p[0] for p in api.posts}  # 和矩形不重叠的小窗不动
        assert MAIN_HWND not in {p[0] for p in api.posts} and CHAT_HWND not in {p[0] for p in api.posts}
    env.run(body)


def test_no_small_window_is_touched_while_the_sticker_panel_is_open(env, tmp_path):
    """表情面板开着（PodMates 自己在操作它）：一个小窗都不关，这张按截不到处理。"""
    async def body(e):
        api = FakeApi(windows=wx_windows([win(MENU_HWND),
                                          win(POPOVER_HWND, "mmui::EmoticonPopover", "Weixin", (74, 185, 588, 699))]))
        api.visible.update({MENU_HWND: True, POPOVER_HWND: True})
        grab = grab_shot()
        ch = shot_env(e, api=api, grab=grab)
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert ctx_texts(e) == ["[表情]"] and vis_calls(e) == [] and grab.calls == []
        assert api.posts == []  # 一个窗口都没发消息
        assert logs(e, "表情面板开着")
    env.run(body)


# ---------- 四、截后两帧前被菜单盖住：当静图，只用第一帧 ----------
def test_a_menu_covering_the_area_while_later_frames_are_taken_falls_back_to_the_first_frame(env, tmp_path):
    async def body(e):
        api = FakeApi(windows=wx_windows([win(MENU_HWND)]))
        api.visible[MENU_HWND] = False  # 一开始没有菜单
        ch = shot_env(e, api=api, grab=grab_shot())
        captured = []

        def grab(rect):
            im = grab_shot()(rect)
            captured.append(1)
            if len(captured) == 1:  # 第一帧截完，菜单又冒出来盖在中心上
                api.top[(560, 265)] = MENU_HWND
            return im
        ch.stk_grab = grab
        m = emotion(tmp_path)
        m.control = face_ctl(box=(400, 210, 720, 320))
        await wx_feed(e, ch, m)
        assert ctx_texts(e)[0].startswith("[表情：") and len(captured) == 1  # 后两帧没截
    env.run(body)


# ---------- 五、验证命令的 menu 一节 ----------
class MenuWeChat(FakeWeChat):
    """原来的会话是个群，群里有别人发的消息；文件传输助手里有一条表情（自己发的）。"""

    def __init__(self, tmp, chat="某个群", **kw):
        super().__init__(tmp, **kw)
        self.chat, self.chats = chat, []

    def ChatInfo(self):
        return {"chat_name": self.chat, "chat_type": "group"}

    def SendFiles(self, filepath, who=None, **k):
        super().SendFiles(filepath, who=who, **k)
        emo = FriendEmotionMessage(self.tmp, "emo1")
        emo.attr = "self"
        emo.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))])
        other = FriendEmotionMessage(self.tmp, "other1")
        other.attr, other.sender, other.sender_remark = "friend", "老王", "王哥"
        other.control = message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (400, 210, 472, 282))])
        self.msgs = [self.msgs[0], emo, self.msgs[1], other]


def menu_api(pop=(), menu_at_start=True, close_ok=True):
    """验证命令用的窗口接口：一个菜单、主窗口；close_ok 决定 Esc / WM_CLOSE 关不关得掉。"""
    extra = [win(MENU_HWND)] + list(pop)
    api = FakeApi(owner=MAIN_HWND, windows=wx_windows(extra))
    api.visible.update({MENU_HWND: menu_at_start, POPOVER_HWND: True} if pop else {MENU_HWND: menu_at_start})
    if close_ok:
        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False
        api.post_key = post_key
    return api


def test_the_command_writes_the_menu_section_with_window_lists_and_both_shots(tmp_path):
    out = tmp_path / "out"
    api = menu_api()
    w = MenuWeChat(tmp_path)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=api, grab=grab_shot(),
                              hwnd_finder=lambda: MAIN_HWND) == 0
    res = result_of(out)
    menu = res["menu"]
    assert menu["skip"] == "" and menu["owner"] == MAIN_HWND
    assert [s["n"] for s in menu["steps"]] == [1, 2, 3, 4, 5, 6]
    for s in menu["steps"]:
        assert "windows" in s and any(x.get("hwnd") == MAIN_HWND for x in s["windows"])
    assert menu["steps"][0]["windows"][0]["title"] == "WeChat" and "rect" in menu["steps"][0]["windows"][0]
    assert any(c.get("how") == "esc" and c.get("closed") is True for c in menu["closed"])
    assert menu["shots"] == ["menu-before.png", "menu-after.png"]
    assert (out / "menu-before.png").read_bytes()[:4] == b"\x89PNG"
    assert (out / "menu-after.png").read_bytes()[:4] == b"\x89PNG"
    log = (out / "probe.log").read_text(encoding="utf-8")
    for i in (1, 2, 3, 4, 5, 6):
        assert f"菜单第 {i} 步" in log
    assert "menu-before.png" in log and "menu-after.png" in log
    # 查点里新加了句柄、类名字段
    assert any("hwnd" in p and "class" in p for p in res["emotion"]["checks"]["points"])


def test_the_command_says_it_skipped_when_the_original_chat_is_file_transfer(tmp_path):
    out = tmp_path / "out"
    w = MenuWeChat(tmp_path, chat="文件传输助手")
    api = menu_api()
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=api, grab=grab_shot(),
                              hwnd_finder=lambda: MAIN_HWND) == 0
    menu = result_of(out)["menu"]
    assert "文件传输助手" in menu["skip"] and menu["steps"][0]["n"] == 0
    assert (out / "menu-before.png").exists() and (out / "menu-after.png").exists()
    assert "跳过" in (out / "probe.log").read_text(encoding="utf-8")


def test_a_failure_in_the_menu_step_does_not_fail_the_whole_command(tmp_path):
    """菜单这一步出任何错都只记下来：命令照走完，emotion 一节照旧。"""
    out = tmp_path / "out"
    w = MenuWeChat(tmp_path)

    class BadApi(FakeApi):
        def windows(self):
            self.n = getattr(self, "n", 0) + 1
            if self.n > 4:  # 前面截图那一步（含第 3 步新加的窗口列表）查得通，菜单这一步的第 2 步开始出错
                raise RuntimeError("查窗口出错")
            return super().windows()
    api = BadApi(owner=MAIN_HWND, windows=wx_windows([]))
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=api, grab=grab_shot(),
                              hwnd_finder=lambda: MAIN_HWND) == 0
    res = result_of(out)
    assert res["ok"] is True
    assert any("查窗口出错" in e.get("error", "") for e in res["menu"]["errors"])  # 只记在这一节里
    assert res["emotion"]["found"] and res["emotion"]["shot"] == "emotion-shot.png"


# ---------- Planner 补：消息控件自己没有句柄、离顶层窗口很多层 ----------
def test_the_message_window_is_found_through_many_parent_levels_without_handles():
    class Node:
        def __init__(self, parent=None, hwnd=0):
            self.parent, self.NativeWindowHandle = parent, hwnd

        def GetParentControl(self):
            return self.parent

    top = Node(hwnd=MAIN_HWND)
    c = top
    for _ in range(20):  # 真机上消息控件到顶层窗口之间隔着好多层没有句柄的控件
        c = Node(parent=c)
    assert wxshot.owner_hwnd(c, FakeApi()) == MAIN_HWND
