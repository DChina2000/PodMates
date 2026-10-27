"""发布修复（丁）的回归测试：重复日志、微信验证命令的核对规则、主窗口最小化、私有 kernel32、
新模型提醒的能力差别与「只改服务商」的作用域。全部用桩，不碰真的微信、不操作任何窗口、不截屏。"""
import ctypes
import sys
from ctypes import wintypes

import pytest

from mb import modelwatch, providers, wxpicture
from test_round35 import WxMain, WxMsg, logs, wx_channel
from test_round44a import Prov, fixtures, seed_catalog, serve, setg, setup
from test_round47g import Msg, PollWx, channel, poll_api
from test_round47h import ProbeWeChat

MAIN_HWND = 11


# ---------- 第 3 项：通知日志写「用户」 ----------
def test_the_notice_log_says_user_not_author(env):
    async def body(e):
        e.engine.notify("测试：没有可用的账号")
        assert logs(e, "通知没能私聊到用户")
        assert not logs(e, "作者")
    env.run(body)


# ---------- 第 4 项：引用找不到只记一条日志 ----------
def test_a_missing_quoted_message_is_logged_only_once(env):
    async def body(e):
        log = []
        ch = wx_channel(e, WxMain(log, [WxMsg("m1", log)]))
        await ch.send("group", "群A", "回你", reply_to="m9")
        assert len(logs(e, "要引用的那条消息在聊天窗口里找不到")) == 1  # _find_message 的详细那条，不再补旧通用句
    env.run(body)


# ---------- 第 5 项：备注名字对上但是群，算核对不上 ----------
def test_an_exact_remark_name_that_is_a_group_does_not_pass_the_check(tmp_path):
    w = ProbeWeChat(tmp_path, known={"某个群"})
    ch = wxpicture.WxForPicture(w, [])
    r = wxpicture.exact_switch(ch, ["某个群"], lambda t: None, sleep=lambda s: None)
    row = r["remarks"][0]
    assert row["opened"] == "某个群" and row["chat_type"] == "group"
    assert row["matched"] is False  # 名字一字不差，但是群：和真发消息时一样不算核对通过


def test_the_missing_name_is_still_compared_by_name_only(tmp_path):
    class AllThere(ProbeWeChat):
        def ChatWith(self, who, **k):
            self.chats.append(who)
            self.chat = who  # 连不存在的名字也「切」到了
        def ChatInfo(self):
            return {"chat_name": self.chat, "chat_type": "group"}  # 一律当群

    w = AllThere(tmp_path)
    ch = wxpicture.WxForPicture(w, [])
    r = wxpicture.exact_switch(ch, ["某个群"], lambda t: None, sleep=lambda s: None)
    assert r["remarks"][0]["matched"] is False
    assert r["missing"]["chat_type"] == "group" and r["missing"]["matched"] is True  # 不存在的名字那一次只比名字


# ---------- 第 6 项：主窗口最小化也算看不见 ----------
def test_a_minimized_main_window_is_warned_about_and_unhidden(env):
    async def body(e):
        api = poll_api()

        def post_key(hwnd, vk):
            api.posts.append((hwnd, "key", vk))
            api.visible[hwnd] = False
        api.post_key = post_key
        api.iconic.add(MAIN_HWND)  # 主窗口最小化：IsWindowVisible 仍为真
        shown = []

        def show_quietly(hwnd):
            shown.append(hwnd)
            api.iconic.discard(hwnd)
        api.show_quietly = show_quietly
        wx = PollWx(api, [Msg()])
        ch = channel(e, api, wx)
        ch._read_chat("群A")
        assert logs(e, "对菜单发 Esc 之后主窗口看不见了")
        assert shown == [MAIN_HWND]  # 调了现有的 unhide()（show_quietly 不抢前台）
    env.run(body)


# ---------- 第 7 项：WinApi 不再改共用的 kernel32 ----------
@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有 kernel32")
def test_winapi_does_not_change_the_shared_kernel32_prototypes():
    from mb.winpos import WinApi
    k = ctypes.windll.kernel32
    before = (k.OpenProcess.restype, k.CloseHandle.argtypes)
    api = WinApi()
    assert api.k is not k  # 自己的一份 WinDLL
    assert (k.OpenProcess.restype, k.CloseHandle.argtypes) == before  # 共用的那份没被改
    assert api.k.OpenProcess.restype is wintypes.HANDLE
    assert api.k.CloseHandle.argtypes == [wintypes.HANDLE]
    assert api.k.GetCurrentThreadId() == k.GetCurrentThreadId()  # 用到 self.k 的地方照常工作


# ---------- 第 8 项：目录缺上下文长度时，看图 / 工具的差别照写 ----------
def test_abilities_still_writes_vision_and_tools_when_context_length_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PODMATES_DATA", str(tmp_path))
    monkeypatch.setattr(providers, "_cache", None)
    seed_catalog({"a": dict(vision=False, tools=True, ctx=128000),
                  "b": dict(vision=True, tools=True, ctx=0),
                  "c": dict(vision=False, tools=False, ctx=0)})
    ab = modelwatch.abilities
    assert ab("testprov", "a", "b") == "新模型还能看图（要拿它看图就回「看图也换」）。"  # 缺 ctx：只跳过长度这一项
    assert ab("testprov", "a", "c") == "注意：新模型不能用工具。"
    assert ab("testprov", "a", "a") == ""


# ---------- 第 9 项：只改服务商、模型沿用上层的作用域也进检查和替换 ----------
def test_a_scope_that_only_overrides_the_endpoint_is_checked_and_replaced(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            setup(e, prov)
            e.core.db.put("endpoint", {"id": "glm2", "name": "智谱2", "provider": "testprov", "protocol": "openai", "base_url": prov.url})
            e.core.db.set_secret("endpoint:glm2", "k")
            setg(e, "main_endpoint", "glm2", "platform:qq")  # 只改这一层的服务商，模型沿用全局的 glm-5.3-flash
            w = e.engine.watch
            hits = [u for u in w.uses() if u["scope"] == "platform:qq"]
            assert [(u["key"], u["purpose"], u["model"], u["eid"]) for u in hits] == [("main_model", "主模型", "glm-5.3-flash", "glm2")]
            prov.vision.add("glm-5.5-flash")
            r = await w.replace("glm2", "glm-5.3-flash", "glm-5.5-flash")
            assert r["ok"]
            assert e.core.settings.data["platform:qq"]["main_model"] == "glm-5.5-flash"  # 在这一层写上新模型
            assert e.core.settings.g("main_model") == "glm-5.3-flash"  # 上层没动，不影响别的作用域
        finally:
            await prov.runner.cleanup()
    env.run(body)
