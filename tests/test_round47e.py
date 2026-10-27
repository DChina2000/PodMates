"""补四十七之五：微信表情截图裁到表情本身，动图连截三帧。

微信 4.1.15 的消息控件没有任何界面子元素，认不出画表情的那一块，每次退回整条消息：718×152 的一长条，大部分是浅灰底。
一长条算出来的 dhash 几乎全是空白的信息，不同的表情很可能算出同一个指纹，看图缓存就把别人的描述拿来用。所以：
  · 截完在图片里裁到表情本身：四角同色就用那个背景色（不同色取出现最多的颜色），和背景色差够多的像素算「有内容」；
    按列切段取最宽的一段（头像是贴边的一个窄方块），再按行收紧；群聊里昵称那一行能干净切开就去掉；
    裁出来太小、整张图找不到内容就按截不到处理（写「[表情]」，记 warn）。
  · 同一块矩形连截 SHOTS 次（每次隔 SETTLE，沿用 wxsticker 的设定），三帧都按第一帧裁出来的位置取：
    三帧 dhash 一样是静图，只交一张；不一样是动图，三帧从左到右拼成一张，看图的提示语加一句说明。
  · 缓存键是裁出来的第一帧的 dhash；验证命令的 emotion 一节带上裁图的细节、三帧的 dhash、是不是动图。

全部用桩：PIL 画的假截图、假的控件树、假的窗口接口、假的截图函数，不碰真的微信、不截任何屏、不操作任何窗口。
"""
import io

import pytest

from PIL import Image, ImageDraw

from mb import wxshot
from mb.channels.wechat import WeChatChannel
from test_round35 import logs
from test_round41 import wx_feed
from test_round47a import FakeWeChat, fakes, result_of  # noqa: F401  (fakes 是自动生效的桩)
from test_round47b import FriendEmotionMessage, vis_calls

from mb import wxpicture
from test_round47d import Ctl, FakeApi, grab_shot, message_control, shot_env

BG = (240, 240, 240)


# ---------- 桩：PIL 画的假截图 ----------
def fake_shot(head_left=True, nick=False, face=(220, 60, 60), inner=True, box=(300, 40, 380, 120), size=(718, 152), label=""):
    """浅灰底 + 贴边的小方块头像 +（群聊）一行昵称 + 一张大图。头像在左是别人发的，在右是自己发的。
    label 非空时在表情里再画一块别的颜色（动图的后两帧用它让 dhash 不同）。"""
    w, h = size

    def make():
        im = Image.new("RGB", (w, h), BG)
        d = ImageDraw.Draw(im)
        if head_left:
            d.rectangle((6, 20, 44, 58), fill=(150, 170, 200))
        else:
            d.rectangle((w - 44, 20, w - 6, 58), fill=(150, 170, 200))
        if nick:
            d.rectangle((box[0], 6, box[0] + 66, 18), fill=(90, 90, 90))
        d.rectangle(box, fill=face)
        if inner and box[2] - box[0] > 28 and box[3] - box[1] > 28:
            d.ellipse((box[0] + 14, box[1] + 14, box[2] - 14, box[3] - 14), fill=(255, 220, 0))
        if label:
            d.rectangle((box[0] + 4, box[1] + 4, box[0] + 30, box[1] + 30), fill=(20, 20, 200))
        return im
    return make


def frames_grab(images: list):
    """按顺序一张一张给：第几次截图返回第几张；多余的（或给 None 的）算截图出错。"""
    calls = []

    def grab(rect):
        calls.append(tuple(rect))
        i = len(calls) - 1
        im = images[i] if i < len(images) else None
        if im is None:
            raise RuntimeError("截图出错")
        return im
    grab.calls = calls
    return grab


def png_bytes(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def crop_of(make):
    im = make() if callable(make) else make
    cut, info = wxshot.crop_emotion(im)
    assert cut is not None, info
    return cut, info


def _tiny():
    """一张只有 11×11 内容点的截图（裁出来比 MIN_SIDE 小）。"""
    im = Image.new("RGB", (718, 152), BG)
    ImageDraw.Draw(im).rectangle((300, 40, 310, 50), fill=(220, 60, 60))
    return im


def face_ctl():
    """一条消息的矩形；截图 718×152（真机上量到的那一长条），画的表情在 (300, 40)-(380, 120)。"""
    return message_control([Ctl("mmui::EmotionItemView", "表情", "ImageControl", (300, 0, 1000, 152))])


# ---------- 一、裁到表情本身 ----------
def test_someone_elses_sticker_with_a_nickname_is_cropped_to_the_sticker_without_avatar_or_nickname():
    cut, info = crop_of(fake_shot(head_left=True, nick=True))
    assert info["nick"] is True  # 昵称那一行干净地切掉了
    assert info["background"] == list(BG) and info["background_how"] == "四角同色" and info["diff"] == wxshot.BG_DIFF
    assert len(info["segments"]) == 2  # 头像一段、表情一段
    assert info["picked"] == info["segments"][1] and info["picked"][0] > 44  # 选了最宽的那一段，不是头像
    assert cut.size == (81, 81) and info["size"] == [81, 81]
    im = cut.convert("RGB")
    assert im.getpixel((0, 0)) == (220, 60, 60)  # 左上角就是表情本身：头像、昵称都不在里面
    assert (150, 170, 200) not in {im.getpixel((x, y)) for x in range(0, 81, 4) for y in range(0, 81, 4)}


def test_your_own_sticker_with_the_avatar_on_the_right_is_cropped_to_the_sticker_too():
    cut, info = crop_of(fake_shot(head_left=False))
    assert cut.size == (81, 81)
    assert info["segments"][0] == [300, 380] and info["picked"] == [300, 380]  # 头像在右边、窄，没被选中
    im = cut.convert("RGB")
    assert im.getpixel((0, 0)) == (220, 60, 60)
    assert (150, 170, 200) not in {im.getpixel((x, y)) for x in range(0, 81, 4) for y in range(0, 81, 4)}


def test_a_screenshot_with_no_content_at_all_or_one_cropped_too_small_counts_as_no_shot():
    blank = Image.new("RGB", (718, 152), BG)  # 整张图都是背景色
    cut, info = wxshot.crop_emotion(blank)
    assert cut is None and "找不到内容" in info["why"]
    tiny = Image.new("RGB", (718, 152), BG)  # 有内容，但只有一个 11×11 的点（比 MIN_SIDE 小）
    ImageDraw.Draw(tiny).rectangle((300, 40, 310, 50), fill=(220, 60, 60))
    cut, info = wxshot.crop_emotion(tiny)
    assert cut is None and "裁出来太小" in info["why"] and info["size"] == [11, 11]


def test_a_sticker_cropped_too_small_is_written_as_the_plain_marker_with_a_warning(env, tmp_path):
    async def body(e):
        ch = shot_env(e, grab=lambda rect: _tiny())
        m = FriendEmotionMessage(tmp_path)
        m.control = face_ctl()
        await wx_feed(e, ch, m)
        assert [x.text for x in e.engine.rooms["wechat:群A"].ctx] == ["[表情]"] and vis_calls(e) == []
        assert logs(e, "截不了") and logs(e, "裁出来太小")
    env.run(body)


# ---------- 二、两个不同的表情放进同样大小的空白长条：裁完 dhash 要不一样 ----------
def test_two_different_stickers_in_the_same_blank_strip_no_longer_share_a_fingerprint():
    a, _ = crop_of(fake_shot(head_left=True, nick=True, face=(220, 60, 60)))
    b, _ = crop_of(fake_shot(head_left=True, nick=True, face=(40, 80, 200), inner=False))
    ha, hb = wxshot.dhash(wxshot.png_of(a)), wxshot.dhash(wxshot.png_of(b))
    assert ha != hb
    # 以前整条消息算出来是 dh:0707060606060606（几乎全是空白）；裁到表情本身以后不再是那样
    assert f"dh:{ha:016x}" not in ("dh:0707060606060606", "dh:0000000000000000")


def test_the_same_sticker_cropped_from_two_positions_still_has_one_fingerprint():
    a, _ = crop_of(fake_shot(box=(300, 40, 380, 120)))
    b, _ = crop_of(fake_shot(box=(330, 44, 410, 124)))  # 截图时位置差一点
    assert wxshot.cache_key(wxshot.png_of(a)) == wxshot.cache_key(wxshot.png_of(b))


# ---------- 三、三帧：一样只交一张，不一样交拼好的图 ----------
def test_three_identical_frames_hand_over_one_picture_and_the_prompt_does_not_mention_frames():
    same = fake_shot()
    shot = wxshot.capture(face_ctl(), FakeApi(), frames_grab([same(), same(), same()]), [(0, 0, 1920, 1080)])
    assert shot.emotion["shots"] == 3 and shot.emotion["moving"] is False
    assert len(set(shot.emotion["frames"])) == 1 and shot.strip == b""
    assert shot.data == shot.first  # 静图：交的就是裁好的第一帧


def test_three_different_frames_are_stitched_left_to_right_and_the_prompt_says_three_frames():
    one, two, three = fake_shot(), fake_shot(label="b"), fake_shot(label="c", inner=False)
    shot = wxshot.capture(face_ctl(), FakeApi(), frames_grab([one(), two(), three()]), [(0, 0, 1920, 1080)],
                          sleep=lambda _s: None)
    assert shot.emotion["moving"] is True and shot.emotion["shots"] == 3
    assert len(set(shot.emotion["frames"])) == 3 and shot.strip and shot.data == shot.strip
    strip = Image.open(io.BytesIO(shot.strip))
    w, h = Image.open(io.BytesIO(shot.first)).size
    assert strip.size == (w * 3 + wxshot.STRIP_GAP * 2, h)  # 三帧从左到右，中间留一条窄缝
    assert shot.key == wxshot.cache_key(shot.first)  # 缓存键仍是第一帧


def test_the_last_two_frames_failing_falls_back_to_the_first_frame_only():
    one = fake_shot()
    shot = wxshot.capture(face_ctl(), FakeApi(), frames_grab([one(), None, None]), [(0, 0, 1920, 1080)])
    assert shot.emotion["shots"] == 1 and shot.emotion["moving"] is False
    assert shot.data == shot.first and shot.strip == b""


def test_the_three_frame_checks_happen_only_before_the_first_frame():
    api = FakeApi(covered={(650, 76)})  # 第一帧前中心被盖住：整块不截，一次都不截
    grab = frames_grab([fake_shot()(), fake_shot()(), fake_shot()()])
    with pytest.raises(wxshot.ShotError):
        wxshot.capture(face_ctl(), api, grab, api.mons)
    assert grab.calls == []


def test_a_window_covered_while_the_later_frames_are_taken_falls_back_to_the_first_frame():
    """第 3 条只在第一帧前查一次：后两帧截之前中心被盖住，就当静图，只用第一帧（已经截好的不丢）。"""
    api = FakeApi()
    one = fake_shot()
    grab = frames_grab([one(), fake_shot(label="b")(), fake_shot(label="c", inner=False)()])
    real = grab

    def grab_and_cover(rect):
        im = real(rect)
        if len(real.calls) == 1:  # 第一帧截完，窗口就被别的窗口盖住了
            api.covered.add((650, 76))
        return im
    shot = wxshot.capture(face_ctl(), api, grab_and_cover, api.mons)
    assert len(real.calls) == 1 and shot.emotion["shots"] == 1 and shot.emotion["moving"] is False
    assert shot.data == shot.first


# ---------- 四、看图：动图与静图的提示语 ----------
def prompt_of(call) -> str:
    return call["messages"][-1]["content"][-1]["text"]


def test_an_animated_sticker_reaches_the_vision_model_as_a_strip_with_the_frame_hint(env, tmp_path):
    async def body(e):
        ch = shot_env(e)
        ch.stk_grab = frames_grab([fake_shot()(), fake_shot(label="b")(), fake_shot(label="c", inner=False)()])
        m = FriendEmotionMessage(tmp_path)
        m.control = face_ctl()
        await wx_feed(e, ch, m)
        calls = vis_calls(e)
        assert len(calls) == 1 and "这是一张动图按时间先后截的三帧，描述它的内容和动作" in prompt_of(calls[0])
        assert logs(e, "动图，三帧拼成一张")
    env.run(body)


def test_a_still_sticker_is_described_without_the_frame_hint(env, tmp_path):
    async def body(e):
        ch = shot_env(e, grab=grab_shot())
        m = FriendEmotionMessage(tmp_path)
        m.control = face_ctl()
        await wx_feed(e, ch, m)
        calls = vis_calls(e)
        assert len(calls) == 1 and "三帧" not in prompt_of(calls[0])
    env.run(body)


# ---------- 五、缓存键是第一帧裁出来的 dhash ----------
def test_the_cache_key_is_the_dhash_of_the_cropped_first_frame(env, tmp_path):
    async def body(e):
        ch = shot_env(e)
        one, two, three = fake_shot(), fake_shot(label="b"), fake_shot(label="c", inner=False)
        ch.stk_grab = frames_grab([one(), two(), three()])
        m = FriendEmotionMessage(tmp_path)
        m.control = face_ctl()
        await wx_feed(e, ch, m)
        cut, _ = crop_of(fake_shot())
        want = wxshot.cache_key(wxshot.png_of(cut))
        rows = e.core.db.rows("SELECT sha FROM vis_cache")
        assert len(rows) == 1 and rows[0]["sha"] == want and want.startswith("dh:")  # 不是整条消息、也不是拼好的那张
    env.run(body)


# ---------- 六、验证命令的 emotion 一节 ----------
class EmoWeChat(FakeWeChat):
    """文件传输助手里有一条表情消息（自己发的），控件里有画表情的子元素。"""

    def SendFiles(self, filepath, who=None, **k):
        super().SendFiles(filepath, who=who, **k)
        emo = FriendEmotionMessage(self.tmp, "emo1")
        emo.attr = "self"
        emo.control = face_ctl()
        self.msgs = [self.msgs[0], emo, self.msgs[1]]


def test_the_command_writes_the_new_emotion_fields_and_the_strip_for_an_animated_sticker(tmp_path):
    out = tmp_path / "out"
    w = EmoWeChat(tmp_path)
    grab = frames_grab([fake_shot()(), fake_shot(label="b")(), fake_shot(label="c", inner=False)()])
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=FakeApi(), grab=grab,
                              hwnd_finder=lambda: 11) == 0
    emo = result_of(out)["emotion"]
    for key in ("background", "diff", "segments", "segment", "crop", "size", "frames", "moving", "nick"):
        assert key in emo
    assert emo["background"] == list(BG) and emo["diff"] == wxshot.BG_DIFF and len(emo["segments"]) >= 2
    assert emo["segment"] in emo["segments"] and emo["crop"] and emo["size"] == [81, 81]
    assert emo["moving"] is True and len(emo["frames"]) == 3 and len(set(emo["frames"])) == 3
    assert emo["strip"] == "emotion-strip.png" and (out / "emotion-strip.png").read_bytes()[:4] == b"\x89PNG"
    assert emo["shot"] == "emotion-shot.png" and Image.open(out / "emotion-shot.png").size == (81, 81)  # 存的是裁好的第一帧
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "emotion-strip.png" in log and "裁到" in log


def test_the_command_writes_no_strip_for_a_still_sticker(tmp_path):
    out = tmp_path / "out"
    w = EmoWeChat(tmp_path)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None, api=FakeApi(), grab=grab_shot(),
                              hwnd_finder=lambda: 11) == 0
    emo = result_of(out)["emotion"]
    assert emo["moving"] is False and emo["strip"] == "" and not (out / "emotion-strip.png").exists()


# ---------- Planner 补：昵称比表情宽、验证命令三帧之间要等 ----------
def test_a_nickname_wider_than_the_sticker_does_not_leave_blank_columns_in_the_crop():
    def make():
        im = fake_shot(head_left=True, nick=False)()
        ImageDraw.Draw(im).rectangle((300, 6, 520, 18), fill=(90, 90, 90))  # 昵称一行比表情（80 宽）宽得多
        return im
    cut, info = crop_of(make)
    assert info["nick"] is True and info["picked"][1] >= 520  # 选中的那段是按昵称的宽度切的
    assert cut.size == (81, 81) and info["rect"][0] == 300 and info["rect"][2] == 381  # 去掉昵称后左右也收紧到表情


def test_the_command_waits_between_the_three_frames_like_the_real_capture(tmp_path):
    out = tmp_path / "out"
    w = EmoWeChat(tmp_path)
    waits = []
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=waits.append, api=FakeApi(), grab=grab_shot(),
                              hwnd_finder=lambda: 11) == 0
    assert waits.count(wxshot.SETTLE) >= wxshot.SHOTS - 1
