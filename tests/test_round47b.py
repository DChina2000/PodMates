"""微信别人发的表情也下载看图（「[表情：描述]」，开关「群友发的表情也看」）、QQ 的表情图也写「[表情：描述]」、同一张图只看一次（看图缓存）、
聊天记录保留天数 0 = 永久、看图的 data URL 用真实格式、验证命令的表情一步。全部用桩，不连真的微信 / NapCat，不往任何聊天发消息。"""
import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

from test_round35 import logs
from test_round41 import ctx_texts, setg, wx_channel, wx_feed
from test_round47a import FakeClip, FakeWeChat, FriendImageMessage, FriendOtherMessage, fakes, result_of, vision_env  # noqa: F401  (fakes 是自动生效的桩)
from test_round5b import png

from mb import wxpicture
from mb.channels import onebot
from mb.channels.base import Msg
from mb.channels.wechat import WeChatChannel

ROOT = Path(__file__).resolve().parent.parent


def gif(frames=3) -> bytes:
    from PIL import Image
    ims = [Image.new("RGB", (40, 40), (60 * i, 30, 200 - 50 * i)) for i in range(frames)]
    out = io.BytesIO()
    ims[0].save(out, "GIF", save_all=True, append_images=ims[1:], duration=80, loop=0)
    return out.getvalue()


class FriendEmotionMessage(FriendOtherMessage):
    """wxauto4 的 type == "emotion"（名字里的 Emotion 让 twin() 找到同一族的 FriendImageMessage）。"""
    type = "emotion"

    def __init__(self, tmp, mid="m1", content="[动画表情]", **kw):
        super().__init__(tmp, mid, content=content, **kw)


FriendEmotionMessage.__module__ = FriendImageMessage.__module__


def spy_observe(e):
    calls = []

    async def observe(bot, room, urls, uid):
        calls.append(list(urls))
    e.engine.stickers.observe = observe
    return calls


def vis_calls(e):
    return [c for c in e.llm.calls if c.get("_kind") == "vis"]


# ---------- 一、微信：别人发的表情也看（截屏，不再试 A、B；截屏本身见 test_round47d） ----------
def shot_with(png_bytes, monkeypatch):
    """把微信渠道的「截表情」换成直接给一张 PNG（真正的认子元素、查窗口、截图在 test_round47d 里测）。"""
    calls = []

    def fake(self, m):
        calls.append(m)
        return png_bytes
    monkeypatch.setattr(WeChatChannel, "_shot_emotion", fake)  # 每个测试结束后自动还原
    return calls


def test_wechat_emotion_messages_are_screenshotted_described_as_sticker_and_not_given_to_sticker_learning(env, tmp_path, monkeypatch):
    async def body(e):
        ch = vision_env(e)
        seen = spy_observe(e)
        for i, m in enumerate([FriendEmotionMessage(tmp_path, "e1", data=gif(2)), FriendOtherMessage(tmp_path, "e2", content="动画表情", data=gif(3))]):
            shot_with(png(10 + i), monkeypatch)  # 两张不一样的画面（同一张会命中 dhash 缓存，见 test_round47d）
            await wx_feed(e, ch, m)
            assert m.control.a_calls == 0 and m.options == []  # 表情不再试 A、B
            assert ctx_texts(e)[-1].startswith("[表情：") and "一只猫" in ctx_texts(e)[-1]
        assert ch.pics == {} and seen == [] and len(vis_calls(e)) == 2  # 微信的表情只看不学
        # 微信的图片照旧走 A、照旧交给学表情包（取件号）；上面的桩只换了表情那一条路，图片不受影响
        pic = FriendOtherMessage(tmp_path, "p1")
        await wx_feed(e, ch, pic)
        assert pic.control.a_calls == 1 and ctx_texts(e)[-1].startswith("[图片：") and len(seen) == 1 and seen[0][0].startswith("wxpic:")
    env.run(body)


def test_wechat_emotion_stays_plain_when_the_switch_vision_or_the_screenshot_is_off_or_fails(env, tmp_path, monkeypatch):
    async def body(e):
        ch = vision_env(e)
        setg(e, "vision_stickers", False)  # 开关关：不截屏，普通图照常
        a = FriendEmotionMessage(tmp_path, "a")
        b = FriendOtherMessage(tmp_path, "b", content="动画表情")
        pic = FriendOtherMessage(tmp_path, "c")
        for m in (a, b, pic):
            await wx_feed(e, ch, m)
        assert a.control.a_calls == b.control.a_calls == 0 and pic.control.a_calls == 1
        assert ctx_texts(e) == ["[表情]", "[表情]", ctx_texts(e)[2]] and ctx_texts(e)[2].startswith("[图片：")
        setg(e, "vision_stickers", True)  # 截屏没成：「[表情]」，记一条 warn，不重试
        shots = shot_with(b"", monkeypatch)
        f = FriendEmotionMessage(tmp_path, "f")
        await wx_feed(e, ch, f)
        assert len(shots) == 1 and ctx_texts(e)[-1] == "[表情]" and f.control.a_calls == 0
        monkeypatch.undo()
        setg(e, "vision_enabled", False)  # 看图关：不截屏
        g = FriendEmotionMessage(tmp_path, "g")
        await wx_feed(e, ch, g)
        assert g.control.a_calls == 0 and ctx_texts(e)[-1] == "[表情]"
    env.run(body)


def test_wechat_emotion_is_recognised_by_type_or_by_the_other_message_text_only():
    m = SimpleNamespace
    assert wxpicture.is_emotion(m(type="emotion", content="x"))
    assert wxpicture.is_emotion(m(type="other", content="动画表情")) and wxpicture.is_emotion(m(type="other", content="[表情]"))
    assert not wxpicture.is_emotion(m(type="other", content="图片")) and not wxpicture.is_emotion(m(type="text", content="[表情]"))
    assert not wxpicture.is_picture(m(type="other", content="动画表情"))


# ---------- 二、QQ：image 段里的表情 ----------
def test_qq_image_segments_that_are_stickers_are_marked_and_mface_is_not_an_image(env):
    async def body(e):
        acc = e.core.db.put("account", {"id": "ac_q", "platform": "qq", "role_id": "r_haitun", "qq": "900", "manage_napcat": False})
        ch = onebot.QQChannel(e.engine, acc)
        got = []

        async def on_message(msg):
            got.append(msg)
        ch.engine = SimpleNamespace(on_message=on_message)
        segs = [{"type": "image", "data": {"url": "http://i/a", "sub_type": 1}},
                {"type": "image", "data": {"url": "http://i/b", "sub_type": "0", "summary": "[动画表情]"}},
                {"type": "image", "data": {"url": "http://i/c", "sub_type": 0, "summary": "[图片]"}},
                {"type": "image", "data": {"url": "http://i/d"}},
                {"type": "mface", "data": {"url": "http://i/m"}}]
        await ch._on_message({"message": segs, "message_type": "group", "group_id": 100, "user_id": 111, "message_id": 5, "sender": {}})
        msg = got[0]
        assert msg.images == ["http://i/a", "http://i/b", "http://i/c", "http://i/d"] and msg.emotes == ["http://i/a", "http://i/b"]
        assert msg.stickers == ["http://i/m"] and msg.text == "[表情]"  # 商城表情照旧：不看，交给学表情包
    env.run(body)


def qq_msg(urls, emotes, seq=1):
    return Msg("qq", "ac_900", "group", "100", "111", "群友", "", msg_id=f"m{seq}", chat_name="测试群", images=list(urls), emotes=list(emotes), dedup=f"100|111|{seq}")


def test_qq_sticker_image_is_described_as_sticker_and_a_plain_image_as_picture_and_the_switch_applies(env):
    async def body(e):
        ch = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "vision_enabled", True)
        setg(e, "vis_model", "deepseek-flash")
        e.llm.vis = lambda content: {"desc": "一只猫"}
        ch.imgs.update({"http://i/s": png(1), "http://i/p": png(2)})
        seen = spy_observe(e)
        await e.engine.on_message(qq_msg(["http://i/s", "http://i/p"], ["http://i/s"]))
        await e.engine.idle()
        t = [x.text for x in e.engine.rooms["qq:100"].ctx][-1]
        assert t.startswith("[表情：") and "[图片：" in t and t.index("[表情：") < t.index("[图片：") and len(vis_calls(e)) == 2
        assert seen == [["http://i/s", "http://i/p"]]  # QQ 的图照旧交给学表情包
        setg(e, "vision_stickers", False)  # 关了：表情不看（也不下载给看图），普通图照看
        n = len(vis_calls(e))
        await e.engine.on_message(qq_msg(["http://i/s", "http://i/p"], ["http://i/s"], 2))
        await e.engine.idle()
        t = [x.text for x in e.engine.rooms["qq:100"].ctx][-1]
        assert t.startswith("[表情]") and "[图片：" in t and len(vis_calls(e)) == n  # 第二次 png(2) 命中缓存，没再调模型
        setg(e, "vision_enabled", False)  # 看图关着：各按各的标记
        await e.engine.on_message(qq_msg(["http://i/s", "http://i/p"], ["http://i/s"], 3))
        await e.engine.idle()
        assert [x.text for x in e.engine.rooms["qq:100"].ctx][-1] == "[表情][图片]"
    env.run(body)


# ---------- 三、同一张图只看一次 ----------
def bot_room(e):
    e.add_bot("900")
    return e.engine.bots["ac_900"], e.engine.room("qq", "100", "群")


def test_the_same_picture_is_described_once_and_a_new_vision_model_looks_again(env):
    async def body(e):
        b, room = bot_room(e)
        e.llm.vis = lambda content: "一只猫"
        d1 = await e.engine.describe_bytes(b, room, png(1))
        d2 = await e.engine.describe_bytes(b, room, png(1))
        assert d1 == d2 and len(vis_calls(e)) == 1
        await e.engine.describe_bytes(b, room, png(2))  # 另一张图
        assert len(vis_calls(e)) == 2
        await e.engine.describe_bytes(b, None, png(1))  # 私聊（没有 room）也命中同一条
        assert len(vis_calls(e)) == 2
        setg(e, "vis_model", "other-vision-model")
        await e.engine.describe_bytes(b, room, png(1))  # 换了看图模型：重看
        assert len(vis_calls(e)) == 3
        rows = e.core.db.rows("SELECT sha, model FROM vis_cache")
        assert len(rows) == 3 and {r["model"] for r in rows} == {"deepseek-flash", "other-vision-model"}
    env.run(body)


def test_failures_and_content_blocks_are_not_cached(env):
    async def body(e):
        b, room = bot_room(e)
        e.llm.vis = lambda content: "一只猫"
        e.llm.block_vis = lambda content: True  # 服务商内容审核拦下
        try:
            await e.engine.describe_bytes(b, room, png(1))
            assert False, "该抛错"
        except Exception as ex:
            assert "1301" in str(ex) or "contentFilter" in str(ex) or "unsafe" in str(ex)
        assert e.core.db.rows("SELECT * FROM vis_cache") == []
        e.llm.block_vis = None
        await e.engine.describe_bytes(b, room, png(1))  # 之后能看了：这次才调模型、才记下
        assert len(e.core.db.rows("SELECT * FROM vis_cache")) == 1
        e.llm.fail_next = 30  # 服务商一直报错
        try:
            await e.engine.describe_bytes(b, room, png(2))
            assert False, "该抛错"
        except Exception:
            pass
        assert len(e.core.db.rows("SELECT * FROM vis_cache")) == 1
    env.run(body)


def test_maintain_cleans_old_cache_with_the_chat_history_and_zero_keeps_everything(env):
    async def body(e):
        db, now = e.core.db, time.time()
        long_ago = now - 40 * 86400

        def fill():
            db.x("DELETE FROM vis_cache")
            db.x("DELETE FROM messages")
            db.x("INSERT INTO vis_cache (sha, model, descr, ts) VALUES ('old','m','旧',?), ('new','m','新',?)", (long_ago, now))
            for kind, ts in (("human", long_ago), ("human", now), ("import", long_ago)):
                db.x("INSERT INTO messages (room, ts, kind, text) VALUES ('qq:1', ?, ?, 'x')", (ts, kind))
        fill()
        e.core.maintain()  # 默认 30 天：旧缓存、旧聊天记录清掉，导入的和新的留着
        assert [r["sha"] for r in db.rows("SELECT sha FROM vis_cache")] == ["new"]
        assert sorted((r["kind"], r["ts"] > now - 86400) for r in db.rows("SELECT kind, ts FROM messages")) == [("human", True), ("import", False)]
        assert e.core.settings.g("message_keep_days") == 30
        setg(e, "message_keep_days", 0)  # 0 = 永久：什么都不清
        fill()
        e.core.maintain()
        assert len(db.rows("SELECT * FROM vis_cache")) == 2 and len(db.rows("SELECT * FROM messages")) == 3
    env.run(body)


# ---------- 四、看图的 data URL 用真实格式 ----------
def test_the_vision_data_url_uses_the_real_image_format(env):
    async def body(e):
        b, room = bot_room(e)
        e.llm.vis = lambda content: "x"
        from PIL import Image
        jpg = io.BytesIO()
        Image.new("RGB", (30, 30), (9, 99, 199)).save(jpg, "JPEG")
        for data, want in ((png(1), "png"), (gif(), "gif"), (jpg.getvalue(), "jpeg")):
            await e.engine.describe_bytes(b, room, data)
            url = vis_calls(e)[-1]["messages"][-1]["content"][0]["image_url"]["url"]
            assert url.startswith(f"data:image/{want};base64,"), url[:30]
    env.run(body)


# ---------- 五、验证命令的表情一步（截屏；细节见 test_round47d） ----------
class EmotionWeChat(FakeWeChat):
    """文件传输助手里除了测试图，还有一条表情消息（自己发的）。"""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)

    def SendFiles(self, filepath, who=None, **k):
        super().SendFiles(filepath, who=who, **k)
        emo = FriendEmotionMessage(self.tmp, "emo1", data=gif(3))
        emo.attr = "self"
        self.msgs = [self.msgs[0], emo, self.msgs[1]]  # 表情在测试图前面：最新的表情不是最后一条


def test_the_command_finds_the_newest_emotion_and_keeps_the_picture_step(tmp_path):
    out = tmp_path / "out"
    w = EmotionWeChat(tmp_path)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None) == 0
    res = result_of(out)
    assert res["A"]["ok"] and res["B"]["ok"] and w.chat == "某个群"  # 图片那一步照旧
    emo = res["emotion"]
    assert emo["found"] is True and emo["picked"] == {"index": 1, "type": "emotion", "attr": "self", "content": "[动画表情]"}
    assert "A" not in emo and "B" not in emo  # 表情不再试 A、B
    assert res["sent_emotion"] is not None  # 发表情那一步也跑了（这里的面板是桩，发不出去，但不影响命令走完）
    log = (out / "probe.log").read_text(encoding="utf-8")
    assert "表情" in log and "没有表情消息" not in log


def test_the_command_says_there_is_no_emotion_and_still_returns_zero(tmp_path):
    out = tmp_path / "out"
    w = FakeWeChat(tmp_path)
    assert wxpicture.run_test(out, lib=w.lib(), timeout=2, sleep=lambda s: None) == 0
    res = result_of(out)
    assert res["ok"] is True and res["emotion"] == {"found": False, "note": "没有表情消息"}
    assert "没有表情消息" in json.dumps(res, ensure_ascii=False) and "表情这一步跳过" in (out / "probe.log").read_text(encoding="utf-8")


# ---------- 六、设置、词典、说明 ----------
def test_the_new_setting_the_renamed_one_and_their_translations_and_guides():
    from mb import settings
    s = next(x for x in settings.SCHEMA if x["key"] == "vision_stickers")
    en = json.loads((ROOT / "web/i18n/en.json").read_text(encoding="utf-8"))
    cn = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    ed = (ROOT / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    assert "群友发的表情也看" in en and "聊天记录保留天数（0 为永久）" in en and "聊天记录保留天数" not in en
    assert "vision_stickers" in cn and "vision_stickers" in ed and "同一张图只看一次" in cn and "looked at only once" in ed
    assert "0 = 永久" in cn and "0 = permanent" in ed and "表情消息" in cn and "sticker message" in ed
    assert s["default"] is True and s["when"] == {"vision_enabled": True}
    assert s["scopes"] == next(x for x in settings.SCHEMA if x["key"] == "vision_enabled")["scopes"]  # 作用范围和「看图」一样


def test_the_new_setting_defaults_on_with_the_vision_scopes_and_hangs_under_vision(env):
    async def body(e):
        sp = e.core.settings
        assert sp.get("vision_stickers", platform="wechat", chat="wechat:群A") is True and sp.get("vision_stickers", platform="qq") is True
        assert sp.g("message_keep_days") == 30
    env.run(body)
