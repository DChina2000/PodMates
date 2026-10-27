"""被叫到时总给表情包工具、删掉编出来的图片行、微信发「自定义表情」（建目录、发、挡板、计数、验证命令）。全部用桩，不往任何群发消息，不碰真的微信。"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from test_round36 import fav_bot, rows
from test_round35 import logs
from test_round5b import png
from wxworld import World, fp

from mb import cleanup, singleton, wxsticker
from mb.channels.base import Msg
from mb.settings import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
BY_KEY = {s["key"]: s for s in SCHEMA}


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def items(n=27, moving=(3, 12), dup=(20, 7)):
    out = [(f"s{i}", i in moving) for i in range(n)]
    if dup:
        out[dup[0]] = (f"s{dup[1]}", False)  # 收藏里有一张和第 7 张一模一样的
    return out


# ---------- 一、被叫到时总给工具 ----------
def add_fav_row(e, aid="ac_900"):
    sp = e.engine.stickers.fav_space(aid)
    mid = e.engine.memory.add(sp, "sticker", "7", "收藏的表情", "无语", "inferred", None, f"fav:{aid}", status="active")
    e.core.db.x("INSERT INTO stickers (space, room, phash, seen, users, status, file, size, uses, descr, mem_id, source, account) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sp, f"fav:{aid}", "0000000000000007", 1, "[]", "learned", "none.png", 1, 0, "无语", mid, "fav", aid))


def test_the_switch_is_on_by_default_in_the_tools_group_and_the_chance_help_says_who_it_still_governs():
    s, c = BY_KEY["sticker_call"], BY_KEY["sticker_chance"]
    assert s["default"] is True and s["cat"] == "工具" and s["scopes"] == BY_KEY["sticker_use"]["scopes"]
    assert "不等于每次都发" in s["help"] and "看气氛" in s["help"]
    assert "主动插话" in c["help"] and "冷场开话题" in c["help"] and c["default"] == 100


def test_called_always_gets_the_sticker_tool_but_chime_in_and_cold_still_go_by_chance(env):
    async def body(e):
        bot = e.add_bot("900")
        add_fav_row(e)
        eng = e.engine
        await e.say("111", "你好")
        room = e.room()
        b = eng.duty(eng.bots["ac_900"], room)
        setg(e, "sticker_chance", 0)
        names = lambda called: [t["name"] for t in eng.tools_for(b, room, "main", called)[0]]
        assert names(True) == ["send_sticker"]  # 开关开着：概率 0 也给
        assert names(False) == []  # 主动插话、冷场开话题：照旧按概率
        setg(e, "sticker_call", False)
        assert names(True) == []  # 开关关掉：被叫到也按概率
        setg(e, "sticker_chance", 100)
        assert names(True) == names(False) == ["send_sticker"]
        setg(e, "sticker_call", True)
        setg(e, "sticker_chance", 0)
        bot.supports_image = False  # 平台不能发
        assert names(True) == []
        bot.supports_image = True
        setg(e, "sticker_use", False)  # 「发表情包」本身关了
        assert names(True) == []
        setg(e, "sticker_use", True)
        e.core.db.x("DELETE FROM stickers")  # 这个账号没有可用的表情
        assert names(True) == []
    env.run(body)


def test_an_at_reaches_the_model_with_the_tool_even_at_zero_chance_and_a_chime_in_does_not(env):
    async def body(e):
        e.add_bot("900")
        add_fav_row(e)
        setg(e, "sticker_chance", 0)
        setg(e, "proactive", False)
        mains = lambda: [c for c in e.llm.calls if c["_kind"] == "main"]
        await e.say("111", "来个表情包", at=["900"])
        assert "tools" in mains()[-1]  # 被 @ 的这次回复：给了工具
        n = len(mains())
        await e.engine.generate(e.engine.duty(e.engine.bots["ac_900"], e.room()), e.room(), "pro", e.room().ctx[-1], brief="接话")
        assert len(mains()) == n + 1 and "tools" not in mains()[-1]  # 主动插话：概率 0 不给
        await e.engine.generate(e.engine.duty(e.engine.bots["ac_900"], e.room()), e.room(), "cold", e.room().ctx[-1])
        assert "tools" not in mains()[-1]
    env.run(body)


# ---------- 二、删掉编出来的图片行 ----------
def test_a_line_that_is_only_a_media_marker_is_dropped_and_nothing_else():
    t = "收到母上大人指示\n[图片：一只白色长发的海豚娘趴在泳池边，配字：上班如溺水]\n再送一张库存\n[表情包：同一只海豚娘仰面漂着]\n[表情]\n[图片]\n  [图片：a][表情包：b]  \n母上大人自取……"
    assert cleanup.strip_media(t) == "收到母上大人指示\n再送一张库存\n母上大人自取……"
    keep = ["看这个[图片：a]好玩", "[图片：a]在前面", "后面还有字 [表情]。", "[图片：描述里有]括号]", "[图片说明]", "（[图片]）", "[表情包]吧", "[语音]"]
    for k in keep:
        assert cleanup.strip_media(k) == k, k  # 夹在句子里的、标记后面还有字的、不是那几种的：一概不动
    assert cleanup.strip_media("[图片：a]") == ""
    assert cleanup.strip_media("好\n\n\n[图片]\n\n\n的") == "好\n\n的"


def test_the_switch_exists_next_to_strip_actions_with_the_same_scopes():
    s, a = BY_KEY["strip_fake_images"], BY_KEY["strip_actions"]
    assert s["default"] is True and s["cat"] == a["cat"] == "回复" and s["scopes"] == a["scopes"]
    keys = [x["key"] for x in SCHEMA]
    assert keys.index("strip_fake_images") == keys.index("strip_actions") + 1
    assert "宁可漏删" in s["help"] and "表情照发" in s["help"]


def test_postprocess_drops_fake_images_each_switch_on_its_own(env):
    async def body(e):
        bot = e.add_bot("900")
        await e.say("111", "你好")
        eng = e.engine
        b, room = eng.duty(eng.bots["ac_900"], e.room()), e.room()
        text = "好的\n[图片：一只海豚]\n（尾巴晃了晃）\n看[图片：a]这个"
        assert eng.postprocess(b, room, text) == "好的\n看[图片：a]这个"
        setg(e, "strip_fake_images", False)
        assert eng.postprocess(b, room, text) == "好的\n[图片：一只海豚]\n看[图片：a]这个"  # 动作描写还是照删
        setg(e, "strip_fake_images", True)
        setg(e, "strip_actions", False)
        assert eng.postprocess(b, room, text) == "好的\n（尾巴晃了晃）\n看[图片：a]这个"  # 「去掉括号里的动作描写」关掉，这里照删
    env.run(body)


def test_a_reply_that_was_only_fake_images_sends_no_text_but_the_sticker_still_goes(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        e.llm.vis = lambda content: {"desc": "无语，翻白眼"}
        ch.imgs["http://qq/s.png"] = png(61)
        ch.favs = ["http://qq/s.png"]
        await e.engine.stickers.sync_favorites(e.engine.bots["ac_900"])
        e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
        e.llm.main = lambda s, u: "[图片：一只海豚]\n[表情包：再来一张]\n【表情1】"
        setg(e, "sticker_chance", 0)
        await e.say("111", "来个表情包", at=["900"])
        assert not ch.sent and len(ch.images) == 1  # 文字全是编出来的图片，删空了不发文字；表情照发
        setg(e, "strip_fake_images", False)
        await e.say("111", "再来一个", at=["900"])
        assert any(s[2].startswith("[图片：一只海豚]") for s in ch.sent)  # 开关关掉：不删
    env.run(body)


# ---------- 三、微信建目录 ----------
def test_catalog_walks_the_virtual_list_screen_by_screen_without_clicking_a_cell():
    w = World(items())
    log = []
    d = w.driver(log=log.append)
    res = d.catalog(w.wx(), [], 40, wxsticker.CHAT_TARGET, "某个群")
    assert res["cells"] == 27 and res["screens"] >= 2  # 一屏只有 25 格完整（第六行只露半截），翻页之后接上
    gs = res["groups"]
    assert len(gs) == 26 and [g["pos"] for g in gs] == [i for i in range(27) if i != 20]  # 和第 7 张一样的那格（第 20 格）算同一张，位置记第一次出现的
    assert all(g["key"] is None and g["preview"] is not None and g["kind"] == "preview" for g in gs)
    moving = {g["pos"]: g["fps"] for g in gs if len(g["fps"]) > 1}
    assert set(moving) == {3, 12} and all(len(f) == 2 for f in moving.values())  # 会动的两格各被认成一张，指纹存了两个不同的
    assert set(moving[3]) == {fp("s3", 0), fp("s3", 1)}
    assert [g["fps"] for g in gs if g["pos"] == 5] == [[fp("s5")]]  # 不动的，一组里只有一个
    assert [g["ticks"] for g in gs if g["pos"] == 26][0] > 0 and [g["ticks"] for g in gs if g["pos"] == 0] == [0]  # 第几格 + 翻到它要滚几格
    assert w.clicks == [("ButtonControl", "mmui::XButton", "发送表情(Alt+E)"), ("TabItemControl", "mmui::EmoticonToolbarItem", "自定义表情")]  # 只点了按钮和那一栏
    assert d.guard.clicked == ["open-panel", "single-tab"] and w.sent_index == []  # 没点过任何格子
    assert sorted(set(w.hovered)) == [g["pos"] for g in gs]  # 只是把鼠标移上去看预览
    assert w.keys == ["{Esc}"] and w.chat == "某个群"  # 关了面板、切回原来的会话
    assert w.chatwith == [wxsticker.CHAT_TARGET, "某个群"]


def test_catalog_recognises_known_ones_and_only_new_ones_get_a_preview(tmp_path):
    w = World(items())
    d = w.driver()
    first = d.catalog(w.wx(), [], 40, wxsticker.CHAT_TARGET, "")["groups"]
    known = [(f"k{g['pos']}", g["fps"][:1]) for g in first if g["pos"] != 9]  # 目录里已经有了除第 9 张以外的
    w.hovered.clear()
    w.items.insert(0, ("new", False))  # 前面多了一张新的，后面的全往后挪了一格
    res = w.driver().catalog(w.wx(), known, 40, wxsticker.CHAT_TARGET, "")
    by = {g["key"]: g for g in res["groups"] if g["key"]}
    assert len(by) == 25 and by["k5"]["pos"] == 6 and by["k12"]["pos"] == 13  # 认出来了，位置更新
    new = [g for g in res["groups"] if g["key"] is None]
    assert sorted(g["pos"] for g in new) == [0, 10] and all(g["preview"] is not None for g in new)  # 新来的 + 原来没进目录的第 9 张（现在在第 10 格）
    assert sorted(set(w.hovered)) == [0, 10]  # 认得的不再去看预览
    assert len(by["k12"]["fps"]) == 2  # 会动的那张认出来之后，指纹也带上了


def test_catalog_budget_limits_previews_and_the_rest_are_pending():
    w = World(items(10, moving=(), dup=None))
    res = w.driver().catalog(w.wx(), [], 3, wxsticker.CHAT_TARGET, "")
    assert sum(1 for g in res["groups"] if g["preview"] is not None) == 3 and res["pending"] == 7
    assert sorted(set(w.hovered)) == [0, 1, 2]


def test_catalog_falls_back_to_the_thumbnail_when_no_big_preview_appears_and_parks_the_mouse():
    w = World(items(4, moving=(), dup=None))
    w.preview.rect = (0, 0, 0, 0)
    w.preview.BoundingRectangle.right = w.preview.BoundingRectangle.bottom = 0
    res = w.driver().catalog(w.wx(), [], 5, wxsticker.CHAT_TARGET, "")
    assert all(g["kind"] == "thumb" and g["preview"] is not None for g in res["groups"]) and w.hover is None


def test_the_wheel_falls_back_when_the_first_scroll_does_nothing():
    w = World(items(), api_wheel_works=False)
    res = w.driver().catalog(w.wx(), [], 0, wxsticker.CHAT_TARGET, "")
    assert res["cells"] == 27 and (2, False) in w.wheel_calls


def test_a_short_list_with_nothing_to_scroll_is_one_screen():
    w = World(items(8, moving=(), dup=None))
    res = w.driver().catalog(w.wx(), [], 0, wxsticker.CHAT_TARGET, "")
    assert res["cells"] == 8 and res["screens"] == 1


# ---------- 四、微信发表情 ----------
def entry_for(w, pos):
    sid, moving = w.items[pos]
    return {"fps": [fp(sid)] + ([fp(sid, 1)] if moving else []), "pos": pos, "ticks": 0}


def catalog_entry(w, pos):
    g = next(g for g in w.driver().catalog(w.wx(), [], 0, wxsticker.CHAT_TARGET, "")["groups"] if g["pos"] == pos)
    w.clicks.clear(), w.keys.clear(), w.chatwith.clear()
    return {"fps": g["fps"], "pos": g["pos"], "ticks": g["ticks"]}


def test_send_scrolls_to_the_recorded_place_confirms_the_fingerprint_and_clicks_that_one():
    w = World(items())
    e = catalog_entry(w, 26)
    assert e["ticks"] > 0
    w.offset = 0
    w.wheel_calls.clear()
    log = []
    r = w.driver(log=log.append).send(w.wx(), e, "目标群", "某个群")
    assert r["found"] and r["clicked"] and r["confirmed"] is True and r["how"] == "near"
    assert w.sent_index == [26] and w.clicks[-1][1] == wxsticker.CELL  # 点的就是第 26 格
    assert [c[0] for c in w.clicks] == ["ButtonControl", "TabItemControl", "TextControl"]
    assert w.wheel_calls == [(-wxsticker.TICK_CHUNK, True), (e["ticks"], True)]  # 回到顶、再直接翻到记下的位置，没有从头一屏屏找
    assert w.keys == ["{Esc}"] and w.chat == "某个群" and w.chatwith == ["目标群", "某个群"]  # 关面板、切回原来的会话
    assert any("确认发出" in x for x in log)


def test_a_moving_sticker_that_does_not_match_on_the_first_shot_still_goes_on_a_later_one():
    w = World(items())
    sid = w.items[3][0]
    e = {"fps": [fp(sid, 0)], "pos": 3, "ticks": 0}  # 目录里只记了它动起来的一个画面
    shots = []
    orig = w.grab
    w.grab = lambda rect: (shots.append((w.grab_n + 1 + w.phase0) % 2), orig(rect))[1]
    w.phase0 = 0
    r = w.driver().send(w.wx(), e, "目标群", "")
    # 回到顶的两次截图 + 找格子时的截图：画面 0 对得上，画面 1 对不上；第一次对不上就再截（最多 3 次）
    assert r["found"] and r["how"] == "near" and w.sent_index == [3]
    look = shots[2:]  # 前两张是回到顶部时截的
    assert look[-1] == 0 and (len(look) == 1 or look[0] == 1)


def test_a_frame_not_yet_in_the_group_is_added_when_it_is_seen_at_the_click():
    w = World(items())
    sid = w.items[3][0]
    r = w.driver().send(w.wx(), {"fps": [fp(sid, 0)], "pos": 3, "ticks": 0}, "目标群", "")
    assert r["found"] and r["fps"][0] == fp(sid, 0) and set(r["fps"]) <= {fp(sid, 0), fp(sid, 1)}


def test_when_the_place_changed_it_searches_from_the_top_clicks_and_reports_the_new_place():
    w = World(items())
    e = catalog_entry(w, 26)
    w.items.insert(0, w.items.pop(26))  # 作者把它挪到了最前面：记下的位置上已经不是它
    log = []
    r = w.driver(log=log.append).send(w.wx(), e, "目标群", "")
    assert r["found"] and r["how"] == "rescan" and r["pos"] == 0 and r["ticks"] == 0
    assert w.sent_index == [0]  # 点的是现在的第 0 格，不是原来记的位置上的那一张
    assert any("从头找" in x for x in log)


def test_not_found_means_no_cell_is_clicked_and_the_panel_is_closed():
    w = World(items())
    log = []
    r = w.driver(log=log.append).send(w.wx(), {"fps": [fp("not-here")], "pos": 3, "ticks": 0}, "目标群", "某个群")
    assert r["found"] is False and r["clicked"] is False
    assert w.sent_index == [] and [c[0] for c in w.clicks] == ["ButtonControl", "TabItemControl"]  # 没点任何格子
    assert w.keys == ["{Esc}"] and w.chat == "某个群"
    assert any("没有点任何格子" in x for x in log)


def test_after_the_click_it_reports_whether_a_sticker_of_its_own_appeared():
    w = World(items(), sends=False)
    r = w.driver().send(w.wx(), entry_for(w, 4), "目标群", "")
    assert r["clicked"] and r["confirmed"] is False and w.sent_index == [4]  # 点了，聊天里没多出自己发的表情：记一笔，不重试

    w2 = World(items())
    wx = w2.wx()
    wx.GetAllMessage = lambda: (_ for _ in ()).throw(RuntimeError("界面忙"))
    r = w2.driver().send(wx, entry_for(w2, 4), "目标群", "")
    assert r["clicked"] and r["confirmed"] is None  # 读不出来：不知道


def test_only_the_three_allowed_controls_can_be_clicked():
    w = World(items(5, moving=(), dup=None))
    log = []
    g = wxsticker.Guard(log.append)
    bad_tab = w.popover.GetChildren()[2]  # 名字也叫「自定义表情」但不是 TabItemControl
    cell = w._cells()[0]
    other = w._cells()[1]
    for c, purpose in ((bad_tab, "single-tab"), (cell, "cell"), (cell, "open-panel"), (w.main.GetChildren()[0], "open-panel"), (w.main.GetChildren()[1], "open-panel"), (cell, "")):
        with pytest.raises(PermissionError):
            g.click(c, purpose)
    g.allow_cell(cell)
    with pytest.raises(PermissionError):
        g.click(other, "cell")  # 只有指纹确认过的那一格
    with pytest.raises(PermissionError):
        g.click(w.heart, "cell")
    assert w.clicks == []
    g.click(w.btn, "open-panel")
    g.click(w.heart, "single-tab")
    g.click(cell, "cell")
    assert g.clicked == ["open-panel", "single-tab", "cell"] and w.sent_index == [0]
    # 一次发送里实际点过的控件
    w2 = World(items())
    d = w2.driver()
    d.send(w2.wx(), entry_for(w2, 10), "目标群", "")
    assert d.guard.clicked == ["open-panel", "single-tab", "cell"] and w2.sent_index == [10]


def test_a_missing_button_or_tab_raises_and_still_closes_and_switches_back():
    w = World(items())
    w.main._kids = [w.main.GetChildren()[0]]
    d = w.driver()
    with pytest.raises(wxsticker.PanelError, match="发送表情"):
        d.send(w.wx(), entry_for(w, 0), "目标群", "某个群")
    assert w.clicks == [] and w.chat == "某个群"


def test_the_wx_wrapper_for_the_test_command_refuses_anything_that_sends():
    w = World(items())
    wx = wxsticker.WxForTest(w.wx(), [])
    for name in ("SendMsg", "SendFiles", "SendTypingText", "AddListenChat", "Close", "ShutDown"):
        with pytest.raises(PermissionError):
            getattr(wx, name)
    assert wx.GetAllMessage() == [] and wx.ChatInfo()["chat_name"] == "某个群"


# ---------- 五、同步进库（描述一次、删掉的跟着删）和发出去的计数 ----------
def wx_channel(e, w, uid="海豚"):
    from mb.channels.wechat import WeChatChannel
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
    ch = WeChatChannel(e.engine, acc)
    ch.wx, ch.status, ch.loop = w.wx(), "online", asyncio.get_running_loop()
    ch.self_name = ch.self_id = uid
    ch._com = lambda: None
    ch._verify_if_due = lambda: None
    ch.stk_ui, ch.stk_hwnd, ch.stk_grab, ch.stk_sleep = w.ui(), (lambda: 11), w.grab, (lambda s: None)
    e.engine.add_bot(acc, ch)
    return ch


def wx_rows(e, **where):
    return rows(e, **where)


def test_sync_builds_the_catalog_describes_new_ones_once_and_follows_removals(env):
    async def body(e):
        w = World(items())
        ch = wx_channel(e, w)
        b = e.engine.bots["ac_wx"]
        st = e.engine.stickers
        described = []
        e.llm.vis = lambda content: described.append(1) or {"desc": f"第{len(described)}张"}
        assert BY_KEY["sticker_fav_use"]["platforms"] == ["qq", "wechat"] and BY_KEY["sticker_fav_hours"]["platforms"] == ["qq", "wechat"] and BY_KEY["sticker_fav_check"]["platforms"] == ["qq", "wechat"]
        out = await st.sync_favorites(b, force=True)
        assert out["error"] == "" and out["added"] == 26  # 全部新的：收藏里 27 格、26 张不同的（有一对一样的）
        assert out["pending"] == 0 and out["kept"] == 26 and len(described) == 26 and out["total"] == 27
        cat = e.core.db.kv("wxfav:ac_wx")["entries"]
        assert len(cat) == 26 and all(set(x) == {"sid", "pos", "ticks", "fps"} for x in cat)
        assert next(x for x in cat if x["pos"] == 3)["fps"] and len(next(x for x in cat if x["pos"] == 3)["fps"]) == 2
        assert all(r["source"] == "fav" and r["account"] == "ac_wx" and r["file"].endswith(".png") for r in wx_rows(e))
        assert w.sent_index == [] and st.available(type("R", (), {"platform": "wechat", "key": "wechat:群A"})(), b)
        # 再同步一次：什么都没变，不再描述
        out = await st.sync_favorites(b, force=True)
        assert out["added"] == 0 and out["removed"] == 0 and len(described) == 26
        # 作者在微信里删掉一张、加了一张：删的跟着删，加的描述一次，位置更新
        gone = e.core.db.kv("wxfav:ac_wx")["entries"][5]["sid"]
        del w.items[5]
        w.items.append(("brand-new", False))
        out = await st.sync_favorites(b, force=True)
        assert out["removed"] == 1 and out["added"] == 1 and len(described) == 27 and out["kept"] == 26
        assert not e.core.db.rows("SELECT 1 FROM stickers WHERE id=?", (gone,))
        cat2 = {x["sid"]: x for x in e.core.db.kv("wxfav:ac_wx")["entries"]}
        assert gone not in cat2 and len(cat2) == 26
        assert w.sent_index == [] and not any(c[2] == "自定义表情" and c[0] == "TextControl" for c in w.clicks)  # 建目录没点过任何格子
    env.run(body)


def test_sync_stops_cleanly_when_the_panel_cannot_be_read_and_changes_nothing(env):
    async def body(e):
        w = World(items(6, moving=(), dup=None))
        ch = wx_channel(e, w)
        b = e.engine.bots["ac_wx"]
        e.llm.vis = lambda content: {"desc": "一张"}
        await e.engine.stickers.sync_favorites(b, force=True)
        before = len(wx_rows(e))
        w.main._kids = []  # 找不到按钮了
        out = await e.engine.stickers.sync_favorites(b, force=True)
        assert "发送表情" in out["error"] and len(wx_rows(e)) == before == 6  # 出错就什么都不改，也不会把目录当成空的全删了
    env.run(body)


def test_without_a_vision_model_new_ones_wait_and_a_content_check_can_reject(env):
    async def body(e):
        w = World(items(4, moving=(), dup=None))
        ch = wx_channel(e, w)
        b = e.engine.bots["ac_wx"]
        setg(e, "vis_model", "")
        out = await e.engine.stickers.sync_favorites(b, force=True)
        assert out["added"] == 0 and out["pending"] == 4 and w.hovered == []  # 没配看图模型：不去看预览，先放着
        e.llm.vis = lambda content: {"sticker": False, "ok": False, "desc": "照片"}
        setg(e, "vis_model", "m")
        setg(e, "sticker_fav_check", True)
        out = await e.engine.stickers.sync_favorites(b, force=True)
        assert out["rejected"] == 4 and out["added"] == 0
        out = await e.engine.stickers.sync_favorites(b, force=True)  # 拒绝过的记着，不再重复描述
        assert out["rejected"] == 0 and w.hovered.count(0) == 1
    env.run(body)


def feed_at(e, text, chat="群A", uid="老王"):
    e.seq = getattr(e, "seq", 0) + 1
    return e.engine.on_message(Msg("wechat", "ac_wx", "group", chat, uid, uid, text, msg_id=f"m{e.seq}", chat_name=chat, at_ids=["海豚"], dedup=f"{chat}|{uid}|{e.seq}"))


def test_a_wechat_sticker_is_sent_from_the_catalog_and_counts_like_any_sticker(env):
    async def body(e):
        w = World(items(), orig="原来的会话")
        ch = wx_channel(e, w)
        setg(e, "proactive", False)
        setg(e, "reply_hourly", 5)
        setg(e, "sticker_chance", 0)
        e.llm.vis = lambda content: {"desc": "无语，翻白眼"}
        st = e.engine.stickers
        await st.sync_favorites(e.engine.bots["ac_wx"], force=True)
        w.clicks.clear(), w.chatwith.clear(), w.keys.clear()
        e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
        e.llm.main = lambda s, u: "行吧\n【表情1】"
        await feed_at(e, "来个表情包")
        await e.engine.idle()
        room = e.engine.rooms["wechat:群A"]
        assert w.sendmsg == [(None, "行吧")]  # 文字照发（发前已经切过去核对过，发时不把 who 交给 SendMsg 去搜）
        assert len(w.sent_index) == 1 and w.chat == "原来的会话"  # 点了一格；最后切回原来的会话
        assert w.clicks[-1][1] == wxsticker.CELL
        assert len(room.hourly["ac_wx"]) == 1  # 计入每小时上限
        assert room.run["ac_wx"] == 2  # 文字一条 + 表情一条，都算连着发的条数
        assert any(x.text.startswith("[表情包：无语") and x.bot for x in room.ctx)
        assert sum(r["uses"] for r in wx_rows(e)) == 1
        b = e.engine.duty(e.engine.bots["ac_wx"], room)
        assert 3.9 < e.engine.quota_tokens(b, room) < 4.1  # 一次回复一个名额
    env.run(body)


def test_a_wechat_sticker_that_cannot_be_found_logs_it_and_the_text_still_goes(env):
    async def body(e):
        w = World(items())
        ch = wx_channel(e, w)
        setg(e, "proactive", False)
        setg(e, "sticker_chance", 0)
        e.llm.vis = lambda content: {"desc": "无语"}
        await e.engine.stickers.sync_favorites(e.engine.bots["ac_wx"], force=True)
        w.items[:] = [(f"other{i}", False) for i in range(27)]  # 作者把自定义表情全换了，目录还没同步
        w.clicks.clear()
        e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
        e.llm.main = lambda s, u: "行吧\n【表情1】"
        await feed_at(e, "来个表情包")
        await e.engine.idle()
        room = e.engine.rooms["wechat:群A"]
        assert w.sendmsg == [(None, "行吧")] and w.sent_index == []
        assert not any(c[1] == wxsticker.CELL for c in w.clicks)
        assert logs(e, "发表情包没成功") and logs(e, "翻到底也没找到")
        assert not room.hourly.get("ac_wx") and room.run["ac_wx"] == 1  # 表情没发出去：不计发言，连发条数只有文字那一条
        assert w.keys[-1] == "{Esc}"
    env.run(body)


def test_a_rescan_updates_the_place_in_the_catalog(env):
    async def body(e):
        w = World(items())
        ch = wx_channel(e, w)
        e.llm.vis = lambda content: {"desc": "一张"}
        st = e.engine.stickers
        b = e.engine.bots["ac_wx"]
        await st.sync_favorites(b, force=True)
        row = next(r for r in wx_rows(e) if st.wx_entry(b, r)["pos"] == 26)
        w.items.insert(0, w.items.pop(26))
        ent = st.wx_entry(b, row)
        assert ent["pos"] == 26 and ent["ticks"] > 0
        ids = await ch.send_image("group", "群A", b"x", sticker=ent)
        assert ids == [""] and w.sent_index == [0]
        st.wx_save(b, ent)
        assert st.wx_entry(b, row)["pos"] == 0 and st.wx_entry(b, row)["ticks"] == 0  # 新位置写回了目录
    env.run(body)


def test_shadow_mode_does_not_click_anything(env):
    async def body(e):
        w = World(items(6, moving=(), dup=None))
        ch = wx_channel(e, w)
        setg(e, "proactive", False)
        setg(e, "shadow", True)
        setg(e, "sticker_chance", 0)
        e.llm.vis = lambda content: {"desc": "无语"}
        await e.engine.stickers.sync_favorites(e.engine.bots["ac_wx"], force=True)
        w.clicks.clear()
        e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
        await feed_at(e, "来个表情包")
        await e.engine.idle()
        assert w.clicks == [] and w.sendmsg == []
    env.run(body)


def test_the_channel_never_touches_a_simulated_wechat_panel_unless_the_stubs_are_set(env):
    async def body(e):
        from mb.channels.wechat import WeChatChannel
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        ch = WeChatChannel(e.engine, acc)
        ch.wx, ch.status, ch.loop = World(items()).wx(), "online", asyncio.get_running_loop()
        ch._com = lambda: None
        ch._verify_if_due = lambda: None
        with pytest.raises(RuntimeError, match="模拟"):
            await ch.fav_scan([], 0)
    env.run(body)


# ---------- 六、验证命令 ----------
def run_test(tmp_path, w, **kw):
    return wxsticker.run_test(tmp_path / "out", lib=w.lib(), ui=w.ui(), hwnd_finder=lambda: 11, grab=w.grab, sleep=lambda s: None, **kw)


def test_the_test_command_builds_the_catalog_sends_the_first_cell_and_only_works_in_the_file_helper(tmp_path):
    w = World(items(), orig="作者的群")
    assert run_test(tmp_path, w) == 0
    out = tmp_path / "out"
    res = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert res["ok"] is True and res["groups"] == 26 and len(res["cells"]) == 27 and res["moving_cells"] == 2
    c3 = next(c for c in res["cells"] if c["pos"] == 3)
    assert len(c3["shots"]) == 3 and len(set(c3["shots"])) == 2 and len(c3["fps"]) == 2  # 每一格 3 次截图的指纹都在里面；会动的看得出来
    c5 = next(c for c in res["cells"] if c["pos"] == 5)
    assert len(set(c5["shots"])) == 1 and c5["fps"] == c5["shots"][:1]
    assert res["send"]["found"] and res["send"]["clicked"] and res["send"]["confirmed"] is True
    assert res["catalog_clicked"] == ["open-panel", "single-tab"] and res["send_clicked"] == ["open-panel", "single-tab", "cell"]
    assert w.sent_index == [0]  # 第一格
    assert w.chatwith == ["文件传输助手", "文件传输助手", "作者的群"] and w.chat == "作者的群"  # 只在文件传输助手里操作；最后切回原来的会话
    for f in ("probe.log", "result.json", "catalog-screen-01.png", "catalog-screen-02.png", "preview-1.png", "preview-3.png"):
        assert (out / f).exists(), f
    assert not (out / "preview-4.png").exists()
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "目录建好了" in log and "确认发出" in log and "已切回原来的会话「作者的群」" in log


def test_the_test_command_stops_where_it_fails_and_still_cleans_up(tmp_path):
    w = World(items())
    w.main._kids = []
    assert run_test(tmp_path, w) == 1
    res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert res["stopped_at"].startswith("2 ") and "发送表情" in res["why"] and w.clicks == [] and w.chat == "某个群"


def test_the_test_command_reports_a_failed_confirmation_without_retrying(tmp_path):
    w = World(items(), sends=False)
    assert run_test(tmp_path, w) == 0
    res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert res["send"]["clicked"] is True and res["send"]["confirmed"] is False and w.sent_index == [0]


def test_the_command_does_not_take_the_single_instance_mutex_or_start_anything(monkeypatch, tmp_path):
    from mb import app
    monkeypatch.setattr(singleton, "acquire", lambda d: pytest.fail("验证命令不该取单实例互斥体"))
    monkeypatch.setattr(app, "Desktop", lambda b: pytest.fail("验证命令不该启动程序主体"))
    got = []
    monkeypatch.setattr(wxsticker, "main", lambda out: got.append(out) or 0)
    with pytest.raises(SystemExit) as ex:
        app.main(["--test-wechat-sticker", str(tmp_path / "o")])
    assert ex.value.code == 0 and got == [str(tmp_path / "o")]
    with pytest.raises(SystemExit) as ex:
        app.main(["--test-wechat-sticker"])
    assert ex.value.code == 2
    doc = wxsticker.__doc__
    assert "--test-wechat-sticker" in doc and "文件传输助手" in doc and "result.json" in doc
    src = (ROOT / "mb" / "wxsticker.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(SendMsg|SendFiles|SendTypingText|quote|forward)\(", src)  # 代码里根本没有发文字、发文件的调用


def test_a_sync_cancelled_at_shutdown_does_not_write_to_a_closed_database(env):
    async def body(e):
        w = World(items(6, moving=(), dup=None))
        ch = wx_channel(e, w)
        b = e.engine.bots["ac_wx"]
        gate = asyncio.Event()

        async def hang(known, budget):
            await gate.wait()
        ch.fav_scan = hang
        task = asyncio.ensure_future(e.engine.stickers.sync_favorites(b, force=True))
        await asyncio.sleep(0.05)
        e.core.db.conn.close()  # 停机：库先关了
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # 被取消就是被取消，收尾记录写不进已关的库也不另外报错
        e.core.db.conn = type("C", (), {"execute": lambda *a, **k: None, "commit": lambda *a: None, "close": lambda *a: None})()
    env.run(body)
