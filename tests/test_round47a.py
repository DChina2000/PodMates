"""微信 4.1.15 的图片显示成「图片」二字（wxauto4 归成 other）：认出来、先 A（ImageMessage.download）后 B（右键「复制」读剪贴板）、
都不成写「[图片]」、验证命令 --test-wechat-picture。全部用桩，不连真的微信，不往任何聊天发消息。"""
import io
import json
import struct
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_round35 import logs
from test_round41 import ctx_texts, setg, wx_channel, wx_feed
from test_round42b import _join, _run, _wait_ctx, _wx_picture_env
from test_round5b import png

from mb import app, singleton, wxpicture
from mb.channels import wechat

ROOT = Path(__file__).resolve().parent.parent
FAKE_MOD = "fake_wxauto4_friend"  # 桩消息类所在的模块名：twin() 靠消息类的模块找同一族的 ImageMessage


# ---------- 桩：wxauto4 的 FriendOtherMessage / FriendImageMessage ----------
class FriendImageMessage:
    def __init__(self, control, parent):
        self.control, self.parent = control, parent

    def download(self):
        c = self.control
        c.a_calls += 1
        if c.a_fail:
            raise RuntimeError("download 在这个微信版本上不能用")
        if c.a_hang:
            time.sleep(c.a_hang)
        c.a_path = Path(c.tmp) / f"a{c.a_calls}.png"
        c.a_path.write_bytes(c.data)
        return c.a_path


class FriendOtherMessage:
    type, attr = "other", "friend"

    def __init__(self, tmp, mid="o1", content="图片", a_fail=False, a_hang=0, data=None, menu=True, sender="老王"):
        self.control = SimpleNamespace(tmp=tmp, a_calls=0, a_fail=a_fail, a_hang=a_hang, a_path=None, data=png(3) if data is None else data)
        self.parent, self.content, self.id, self.sender = object(), content, mid, sender
        self.options, self.rolled = [], 0
        if not menu:
            self.select_option = None

    def roll_into_view(self):
        self.rolled += 1

    def select_option(self, name):
        self.options.append(name)
        return {"status": "成功"}


FriendImageMessage.__module__ = FriendOtherMessage.__module__ = FAKE_MOD


class FakeClip:
    """剪贴板桩：clear() 清空；点「复制」后 dump 里才有东西（由 fill 决定）。"""
    dump: dict = {}
    cleared = 0

    def clear(self):
        type(self).cleared += 1

    def read(self):
        return dict(type(self).dump)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    mod = types.ModuleType(FAKE_MOD)
    mod.FriendImageMessage, mod.FriendOtherMessage = FriendImageMessage, FriendOtherMessage
    monkeypatch.setitem(sys.modules, FAKE_MOD, mod)
    monkeypatch.setattr(wxpicture, "Clipboard", FakeClip)
    monkeypatch.setattr(wxpicture, "CLIP_WAIT", 0.3)
    monkeypatch.setattr(wxpicture, "CLIP_STEP", 0.05)
    FakeClip.dump, FakeClip.cleared = {}, 0


def vision_env(e):
    ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
    setg(e, "proactive", False)
    setg(e, "vision_enabled", True)
    setg(e, "vis_model", "deepseek-flash")
    e.llm.vis = lambda content: {"desc": "一只猫"}
    return ch


# ---------- 一、认出来、下载（A） ----------
def test_other_message_with_the_word_picture_is_downloaded_with_A_described_and_the_file_deleted(env, tmp_path):
    async def body(e):
        ch = vision_env(e)
        m = FriendOtherMessage(tmp_path)
        await wx_feed(e, ch, m)
        assert m.control.a_calls == 1 and m.options == []  # A 成了就不用 B
        assert ctx_texts(e)[-1].startswith("[图片：") and "一只猫" in ctx_texts(e)[-1]
        assert not m.control.a_path.exists()  # 下载下来的文件用完删掉
        assert ch.pics == {} and "vis" in e.llm.kinds()
        assert logs(e, "图片下载成功（方式 A")
    env.run(body)


def test_polling_mode_downloads_it_on_its_own_thread_too(env, tmp_path):
    async def body(e):
        import threading
        ch = _wx_picture_env(e)
        m = FriendOtherMessage(tmp_path)
        names = []
        orig = FriendImageMessage.download
        FriendImageMessage.download = lambda self: names.append(threading.current_thread().name) or orig(self)
        try:
            th = _run("wechat-poll", ch._handle, m, "群A")
            await _join(th)
            await _wait_ctx(e)
            await e.engine.idle()
        finally:
            FriendImageMessage.download = orig
        assert names and names[0].startswith("wechat-pic")
        assert ctx_texts(e)[-1].startswith("[图片：")
    env.run(body)


# ---------- 二、A 不成用 B ----------
def test_when_A_raises_B_copies_through_the_menu_and_is_described_and_logged_as_B(env, tmp_path):
    async def body(e):
        ch = vision_env(e)
        FakeClip.dump = {"49361": b"x" * 10, "49645": png(3)}  # 剪贴板里有 PNG 原始字节
        m = FriendOtherMessage(tmp_path, a_fail=True)
        await wx_feed(e, ch, m)
        assert m.control.a_calls == 1 and m.options == ["复制"] and m.rolled == 1 and FakeClip.cleared == 1
        assert ctx_texts(e)[-1].startswith("[图片：") and "一只猫" in ctx_texts(e)[-1]
        assert logs(e, "图片下载成功（方式 B") and not logs(e, "下载图片失败")
    env.run(body)


def test_clipboard_images_are_read_from_raw_bytes_file_paths_and_bitmaps(tmp_path):
    from PIL import Image
    data = png(5)
    assert wxpicture.image_from_clipboard({"1": b"abc", "49332": data}) == data  # 注册格式里的 PNG 字节（格式号不固定，认字节头）
    f = tmp_path / "x.png"
    f.write_bytes(data)
    assert wxpicture.image_from_clipboard({"15": (str(f),)}) == data  # CF_HDROP：文件路径
    assert wxpicture.image_from_clipboard({"15": [str(tmp_path / "missing.png")]}) == b""
    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buf, "BMP")
    got = wxpicture.image_from_clipboard({"8": buf.getvalue()[14:]})  # CF_DIB = 去掉 14 字节文件头的 BMP
    assert Image.open(io.BytesIO(got)).convert("RGB").tobytes() == Image.open(io.BytesIO(data)).convert("RGB").tobytes()
    assert wxpicture.image_from_clipboard({"13": "文字", "1": b"abc"}) == b"" and wxpicture.image_from_clipboard({}) == b""


# ---------- 三、都不成 ----------
def test_when_A_and_B_both_fail_the_marker_is_written_and_each_is_tried_once(env, tmp_path):
    async def body(e):
        ch = vision_env(e)
        m = FriendOtherMessage(tmp_path, a_fail=True)  # 剪贴板一直是空的
        await wx_feed(e, ch, m)
        assert m.control.a_calls == 1 and m.options == ["复制"]  # 各一次，不重试
        assert ctx_texts(e) == ["[图片]"] and "vis" not in e.llm.kinds()
        bad = logs(e, "下载图片失败")
        assert len(bad) == 1 and "A：" in bad[0] and "B：" in bad[0] and "剪贴板里没有图片" in bad[0]
    env.run(body)


def test_without_a_menu_B_fails_cleanly(env, tmp_path):
    async def body(e):
        ch = vision_env(e)
        m = FriendOtherMessage(tmp_path, a_fail=True, menu=False)
        await wx_feed(e, ch, m)
        assert ctx_texts(e) == ["[图片]"] and "select_option" in logs(e, "下载图片失败")[0]
    env.run(body)


# ---------- 四、不下载的情形 ----------
def test_no_download_when_vision_is_off_or_has_no_model_and_the_text_is_the_marker_not_the_bare_word(env, tmp_path):
    async def body(e):
        ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
        setg(e, "proactive", False)
        a = FriendOtherMessage(tmp_path, "a")
        await wx_feed(e, ch, a)  # 看图默认关
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "")
        b = FriendOtherMessage(tmp_path, "b")
        await wx_feed(e, ch, b)
        assert a.control.a_calls == b.control.a_calls == 0 and a.options == b.options == []
        assert ctx_texts(e) == ["[图片]", "[图片]"]
    env.run(body)


def test_typed_word_picture_stays_text_and_is_not_downloaded(env, tmp_path):
    async def body(e):
        ch = vision_env(e)
        m = FriendOtherMessage(tmp_path, "t1")
        m.type = "text"  # 有人打字发了「图片」两个字
        await wx_feed(e, ch, m)
        assert m.control.a_calls == 0 and m.options == [] and ctx_texts(e) == ["图片"] and "vis" not in e.llm.kinds()
        assert not wxpicture.is_picture(m)
    env.run(body)


def test_other_messages_that_are_emotions_are_not_downloaded_when_the_sticker_switch_is_off(env, tmp_path):
    async def body(e):
        ch = vision_env(e)
        setg(e, "vision_stickers", False)  # 默认开的「群友发的表情也看」关掉才不下载表情（见 test_round47b）
        for i, word in enumerate(["动画表情", "[表情]"]):
            m = FriendOtherMessage(tmp_path, f"e{i}", content=word)
            await wx_feed(e, ch, m)
            assert m.control.a_calls == 0 and m.options == [] and not wxpicture.is_picture(m)
        assert ctx_texts(e) == ["[表情]", "[表情]"] and "vis" not in e.llm.kinds()
        assert wechat.EMOTION_TEXT.fullmatch("[动画表情]")
    env.run(body)


def test_twin_builds_the_image_message_of_the_same_family_from_control_and_parent(tmp_path):
    m = FriendOtherMessage(tmp_path)
    t = wxpicture.twin(m)
    assert type(t) is FriendImageMessage and t.control is m.control and t.parent is m.parent
    img = SimpleNamespace(type="image")
    assert wxpicture.twin(img) is img


# ---------- 五、验证命令 ----------
class FakeWeChat:
    """文件传输助手里的微信桩：SendFiles 之后聊天里多出一条 type=other、content=图片 的自己发的消息。"""

    def __init__(self, tmp, a_fail=False, a_hang=0, clip_ok=True):
        self.tmp, self.a_fail, self.a_hang, self.clip_ok = tmp, a_fail, a_hang, clip_ok
        self.chat, self.chats, self.sent, self.msgs = "某个群", [], [], []

    def ChatInfo(self):
        return {"chat_name": self.chat}

    def ChatWith(self, who, **k):
        self.chat = who
        self.chats.append(who)

    def GetAllMessage(self):
        return list(self.msgs)

    def SendFiles(self, filepath, who=None, **k):
        assert who == "文件传输助手"
        self.sent.append(filepath)
        m = FriendOtherMessage(self.tmp, "self1", a_fail=self.a_fail, a_hang=self.a_hang, data=Path(filepath).read_bytes())
        m.attr, m.sender = "self", "我"
        self.msgs = [SimpleNamespace(type="text", attr="friend", content="早", id="t0"), m]
        if self.clip_ok:
            FakeClip.dump = {"49361": b"meta", "49645": Path(filepath).read_bytes()}

    def lib(self):
        return SimpleNamespace(WeChat=lambda resize=False: self)


def result_of(out):
    return json.loads((out / "result.json").read_text(encoding="utf-8"))


def test_the_command_sends_a_test_picture_tries_both_ways_and_reports_everything(tmp_path):
    w = FakeWeChat(tmp_path)
    out = tmp_path / "out"
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None) == 0
    res = result_of(out)
    assert w.chats == ["文件传输助手", "某个群"] and w.chat == "某个群"  # 只进了文件传输助手，最后切回原来的会话
    assert w.sent == [str(out / "test-picture.png")]
    assert [r["type"] for r in res["messages"]] == ["text", "other"] and res["messages"][1]["content"] == "图片" and res["messages"][1]["attr"] == "self"
    assert res["picked"]["index"] == 1 and res["picked"]["is_new"] is True
    for way in "AB":  # A 成了 B 也照样试
        r = res[way]
        assert r["ok"] is True and r["bytes"] > 0 and (out / r["file"]).read_bytes() and r["error"] == "" and "seconds" in r
        assert res["compare"][way]["same_size"] is True and res["compare"][way]["same_pixels"] is True
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "方式 A" in log and "方式 B" in log and "已切回原来的会话" in log


def test_the_command_reports_A_failing_and_hanging_without_skipping_B(tmp_path):
    out = tmp_path / "o1"
    w = FakeWeChat(tmp_path, a_fail=True)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None) == 0
    res = result_of(out)
    assert res["A"]["ok"] is False and "不能用" in res["A"]["error"] and res["A"]["file"] == "" and res["B"]["ok"] is True

    out = tmp_path / "o2"
    FakeClip.dump = {}
    w = FakeWeChat(tmp_path, a_hang=2.0, clip_ok=False)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=1.0, sleep=lambda s: None) == 0
    res = result_of(out)
    assert res["A"]["ok"] is False and "超过" in res["A"]["error"]  # A 卡住：最多等 timeout 秒；B 另算一份时间
    assert res["B"]["ok"] is False and "剪贴板里没有图片" in res["B"]["error"]  # B 没复制出东西：照实写出来


def test_the_command_stops_and_still_switches_back_when_no_picture_shows_up(tmp_path):
    out = tmp_path / "o"
    w = FakeWeChat(tmp_path)
    w.SendFiles = lambda filepath, who=None, **k: w.sent.append(filepath)  # 发了但聊天里没多出图片
    assert wxpicture.run_test(out, lib=w.lib(), timeout=1, sleep=lambda s: None) == 1
    res = result_of(out)
    assert res["stopped_at"].startswith("3 ") and "认不出图片" in res["why"] and w.chat == "某个群"


def test_the_command_wrapper_only_sends_files_to_the_file_helper_chat(tmp_path):
    w = wxpicture.WxForPicture(SimpleNamespace(SendFiles=lambda *a, **k: "ok"), [])
    assert w.SendFiles("x.png", "文件传输助手") == "ok"
    with pytest.raises(PermissionError):
        w.SendFiles("x.png", "某个群")
    with pytest.raises(PermissionError):
        w.SendMsg("hi")  # 别的方法一概不让碰


def test_the_command_does_not_take_the_mutex_and_prints_usage_without_a_folder(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(singleton, "acquire", lambda d: pytest.fail("验证命令不该取单实例互斥体"))
    monkeypatch.setattr(app, "Desktop", lambda b: pytest.fail("验证命令不该启动程序主体"))
    got = []
    monkeypatch.setattr(wxpicture, "main", lambda out: got.append(out) or 0)
    with pytest.raises(SystemExit) as ex:
        app.main(["--test-wechat-picture", str(tmp_path / "o")])
    assert ex.value.code == 0 and got == [str(tmp_path / "o")]
    with pytest.raises(SystemExit) as ex:
        app.main(["--test-wechat-picture"])
    assert ex.value.code == 2 and "用法：PodMates.exe --test-wechat-picture <输出目录>" in capsys.readouterr().out
    doc = wxpicture.__doc__
    assert "--test-wechat-picture" in doc and "文件传输助手" in doc and "result.json" in doc


def test_the_guides_describe_the_command_and_the_new_way_pictures_are_recognised():
    cn = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    assert "--test-wechat-picture" in cn and "文件传输助手" in cn and "右键" in cn and "「图片」两个字" in cn
    assert "--test-wechat-picture" in en and "文件传输助手" in en and "right-click" in en and "“图片”" in en
