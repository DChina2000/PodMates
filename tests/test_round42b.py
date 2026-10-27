"""发布前修复（乙）：非官方渠道「预览版」标记、NapCat 始终写/读日志、单实例锁文件兜底、微信轮询看图 25 秒超时、说明与周边材料同步。"""
import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from test_round35 import logs
from test_round41 import Pic, ctx_texts, wx_channel

ROOT = Path(__file__).resolve().parent.parent


# ---------- 08 非官方渠道标「预览版」 ----------
def test_unofficial_channels_get_a_preview_badge_and_both_dictionaries_have_it():
    from mb.platforms import PLATFORMS
    assert {p["id"] for p in PLATFORMS if p["tier"] == "unofficial"} == {"bilibili", "tieba", "weibo"}
    js = (ROOT / "web/js/pages-new.js").read_text(encoding="utf-8")
    badge = next(line for line in js.splitlines() if "'有风险'" in line)
    assert "s.tier === 'unofficial'" in badge and "h('span.badge.bad', '有风险')" in badge  # 原来的风险标记保留
    assert "h('span.badge.warn', '预览版')" in badge
    en = json.loads((ROOT / "web/i18n/en.json").read_text(encoding="utf-8"))
    assert en["预览版"] == "Preview" and en["有风险"] == "Risky"


def test_guides_call_the_unofficial_channels_a_preview():
    cn = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    assert "非官方渠道（B站、贴吧、微博）——预览版，有风险，默认关闭" in cn
    assert "Unofficial Channels (Bilibili, Baidu Tieba, Weibo): Preview, Risky, Off by Default" in en


# ---------- 16 NapCat 日志 ----------
class _Eng:
    def __init__(self):
        self.lines, self.notes = [], []

    def log(self, level, cat, msg):
        self.lines.append(msg)

    def notify(self, msg, account):
        self.notes.append(msg)


class _Ch:
    account = "ac"

    def __init__(self):
        self.engine, self.qr, self.stopping = _Eng(), [], False

    def on_qr(self, url):
        self.qr.append(url)


def _napcat(tmp_path):
    from mb.channels.napcat import NapCatProcess
    ch = _Ch()
    p = NapCatProcess(ch, tmp_path, "12345", 3001, "tok", 6099, hide=False)
    p.lay = {"root": tmp_path, "kind": "shell", "boot": tmp_path / "boot.exe", "cache": tmp_path / "cache"}
    (tmp_path / "cache").mkdir(exist_ok=True)
    return p, ch


def test_napcat_writes_to_the_log_even_when_the_console_window_is_shown(tmp_path, monkeypatch):
    from mb.channels import napcat as nc
    lp = tmp_path / "napcat-logs" / "12345.log"
    monkeypatch.setattr(nc, "log_path", lambda uin: lp)
    monkeypatch.setattr(nc, "write_config", lambda *a, **k: None)
    monkeypatch.setattr(nc, "reap", lambda *a, **k: [])
    monkeypatch.setattr(nc, "save_pids", lambda *a, **k: None)
    saved = {}
    monkeypatch.setattr(nc, "save_run", lambda uin, meta: saved.update(meta or {}))
    calls = {}

    class Proc:
        pid, returncode = 424242, None

        def poll(self):
            return None

    def spawn(cmd, cwd, env, hide, fd):
        calls.update(fd=fd, hide=hide)
        return Proc()

    monkeypatch.setattr(nc, "spawn_detached", spawn)
    monkeypatch.setattr(nc.asyncio, "create_task", lambda coro: (coro.close(), None)[1])  # 不真跑 _watch
    p, _ = _napcat(tmp_path)
    p.launch_plan = lambda: (["fake"], {})

    async def no_attach():
        return False
    p.try_attach = no_attach

    async def go():
        await p.start()
        p.closed = True
    asyncio.run(go())
    assert calls["hide"] is False and calls["fd"] is not None  # 不弹黑窗口也把输出交给日志文件
    assert p.tail is not None and p.tail.path == lp  # 同一份日志也接着读
    assert saved["log"] == str(lp)


def test_napcat_recognises_qr_kick_and_risk_control_from_the_log(tmp_path):
    from mb.channels.napcat import LogTail
    p, ch = _napcat(tmp_path)
    lp = tmp_path / "12345.log"
    lp.write_text("", encoding="utf-8")
    p.tail = LogTail(lp, at_end=False)
    with open(lp, "a", encoding="utf-8") as f:
        f.write("二维码解码URL：https://example.invalid/qr\n"
                "[KickedOffLine] 你已被踢下线\n"
                "账号可能触发风控\n")
    p.pump()
    assert ch.qr == ["https://example.invalid/qr"]
    assert any("被踢下线" in n for n in ch.engine.notes)
    assert any("风控" in n for n in ch.engine.notes)


# ---------- 18 单实例后备锁文件 ----------
def _release(s, n):
    for h in s._held[n:]:
        try:
            h.close()
        except Exception:
            pass
    del s._held[n:]


@pytest.mark.skipif(os.name != "nt", reason="命名互斥体和锁文件是 Windows 行为")
def test_fallback_lock_lets_only_one_through(tmp_path):
    from mb import singleton
    kept = len(singleton._held)
    try:
        assert singleton._fallback_lock(tmp_path) is True
        assert singleton._fallback_lock(tmp_path) is False
        assert (tmp_path / singleton.LOCK_FILE).exists()
    finally:
        _release(singleton, kept)


@pytest.mark.skipif(os.name != "nt", reason="命名互斥体和锁文件是 Windows 行为")
def test_acquire_does_not_pass_when_both_mutexes_cannot_be_created(tmp_path, monkeypatch):
    from mb import singleton
    kept = len(singleton._held)
    monkeypatch.setattr(singleton, "_create_mutex", lambda data, scope: (None, 2, None))  # 两种名字都建不了
    try:
        assert singleton.acquire(tmp_path) is True   # 不会再直接放行：用锁文件占住
        assert singleton.acquire(tmp_path) is False  # 同一份数据目录第二个不放行
    finally:
        _release(singleton, kept)


# ---------- 21 微信轮询看图超时 ----------
def _run(name, fn, *a):
    th = threading.Thread(target=fn, args=a, name=name)
    th.start()
    return th


async def _join(th):
    while th.is_alive():
        await asyncio.sleep(0.01)


async def _wait_ctx(e, who="群A", n0=0):
    for _ in range(250):
        room = e.engine.rooms.get(f"wechat:{who}")
        if room and len(room.ctx) > n0:
            return
        await asyncio.sleep(0.02)


def _wx_picture_env(e):
    ch = wx_channel(e, {"群A": {"name": "群A", "type": "group"}})
    s = e.core.settings
    s.set("global", "proactive", False)
    s.set("global", "vision_enabled", True)
    s.set("global", "vis_model", "deepseek-flash")
    e.llm.vis = lambda content: {"desc": "一只猫"}
    return ch


def test_polling_picture_download_has_a_hard_timeout_and_leaves_the_message_alone(env, tmp_path, monkeypatch):
    from mb.channels import wechat

    class Slow(Pic):
        def download(self):
            self.downloads += 1
            time.sleep(2)  # 模拟 download() 卡住（它没有超时参数）
            self.path = Path(tmp_path) / "slow.png"
            self.path.write_bytes(self.data)
            return self.path

    monkeypatch.setattr(wechat, "PIC_TIMEOUT", 0.2)

    async def body(e):
        ch = _wx_picture_env(e)
        pic = Slow(tmp_path)
        t0 = time.time()
        th = _run("wechat-poll", ch._handle, pic, "群A")
        await _join(th)
        assert time.time() - t0 < 1.5  # 最多等 PIC_TIMEOUT，不跟着下载一起卡住
        await _wait_ctx(e)
        await e.engine.idle()
        assert pic.downloads == 1
        assert ctx_texts(e) == ["[图片]"]  # 超时的这张按「[图片]」处理
        assert "vis" not in e.llm.kinds() and logs(e, "下载图片超过")
    env.run(body)


def test_polling_picture_downloads_on_its_own_thread_and_still_gets_described(env, tmp_path):
    async def body(e):
        ch = _wx_picture_env(e)
        pic = Pic(tmp_path)
        orig, names = pic.download, []
        pic.download = lambda: names.append(threading.current_thread().name) or orig()
        th = _run("wechat-poll", ch._handle, pic, "群A")
        await _join(th)
        await _wait_ctx(e)
        await e.engine.idle()
        assert names and names[0].startswith("wechat-pic")  # 下载在单独的线程，收消息的轮询只等结果
        assert pic.downloads == 1 and ctx_texts(e)[-1].startswith("[图片：") and "一只猫" in ctx_texts(e)[-1]
        assert not pic.path.exists()  # 下载下来的文件用完立刻删掉
    env.run(body)


# ---------- 22 中英文说明与设置帮助 ----------
def test_settings_help_says_private_chats_wait_5_seconds():
    src = (ROOT / "mb/settings.py").read_text(encoding="utf-8")
    assert "私聊照旧马上处理" not in src
    assert "私聊默认先等对方说完 5 秒" in src


def test_guides_are_in_sync_with_the_current_behaviour():
    cn = (ROOT / "docs/使用说明.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/User-Guide.en.md").read_text(encoding="utf-8")
    assert "私聊默认先等对方停 5 秒" in cn
    assert "private chats wait 5 s for the other person to stop by default" in en
    assert "任意一条新消息就算成功" in cn
    assert "any new message of her own counts as success" in en
    assert "按需打开的（发言占比上限默认是开的）" in cn
    assert "**Optional (the share-of-conversation cap is on by default)**" in en
    assert "关系按你填的来" in cn and "only the template's default wording" in en
    assert "attitude toward each person" not in en
    # 甲修的：导出带表情图片和撤销重置的备份；重置上下文覆盖所有会话
    assert "表情包图片" in cn and "Undo the last reset" in en
    assert "覆盖所有会话，包括还没加载进内存的" in cn
    assert "including ones not loaded into memory yet" in en
    # 02 原本就写着「每条消息不在前情就在原始消息里」，这次确认仍然一致
    assert "每一条消息不是在前情里，就是在原始消息里" in cn
    assert "every message in a chat is either in a recap or in the raw messages" in en


# ---------- 23 / 24 / 25 周边材料 ----------
def test_readmes_no_longer_embed_the_old_screenshots():
    for name, sentence, bad in (("README.md", "打开界面", "docs/截图/"), ("README.en.md", "Opening the panel", "docs/截图/")):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert bad not in text and "![" not in text
        assert sentence in text


def test_third_party_lists_psutil_and_the_full_models_dev_mit_text():
    text = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    assert "| psutil | BSD-3-Clause |" in text
    assert "see the upstream LICENSE file" not in text
    for phrase in ("MIT License", "Copyright (c) 2025 models.dev", "MERCHANTABILITY",
                   "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE"):
        assert phrase in text


def test_every_requirements_entry_appears_in_the_third_party_table():
    table = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8").lower()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~]", line)[0].strip()
        assert name.lower() in table, name


def test_installer_example_uses_a_generic_qq_path():
    text = (ROOT / "installer/stop-podmates.ps1").read_text(encoding="utf-8-sig")
    # 脚本里只许出现通用的安装位置示例，不能写死某台机器上的 QQ 路径
    assert re.findall(r"[A-Za-z]:\\[^\r\n，。、）)]*", text) == ["C:\\Program Files\\Tencent\\QQ\\QQ.exe"]
    assert "C:\\Program Files\\Tencent\\QQ\\QQ.exe" in text
