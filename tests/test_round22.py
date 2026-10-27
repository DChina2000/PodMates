"""微信版本核对（超出 wxauto4 声明范围：通知一次；已验证能用的版本措辞不同）、每天查 wxauto4 新版（提醒不自动升级、可以关）、
升级按钮（成功 / 失败回退）。全部用桩：不连微信、不连 PyPI、不碰真的 data 目录。"""
import io
import zipfile
from pathlib import Path

import pytest

from mb.channels import wechat
from mb.channels.wechat import WeChatChannel, classify, parse_declared


import sys
WHEEL = f"wxauto4-41.2.0-cp{sys.version_info.major}{sys.version_info.minor}-cp{sys.version_info.major}{sys.version_info.minor}-win_amd64.whl"


def make(e):
    acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun", "listen": []})
    ch = WeChatChannel(e.engine, acc)
    e.engine.add_bot(acc, ch)
    return ch


def test_classification_and_wording():
    assert classify((4, 1, 8, 5)) | {} and classify((4, 1, 8, 5))["level"] == "ok" and classify((4, 1, 8, 5))["ok"]
    assert classify((4, 0, 3, 36))["level"] == "ok"
    v = classify((4, 1, 15, 56))  # 超出声明的范围，但实测能用
    assert v["level"] == "verified" and not v["ok"] and "实测能用" in v["note"] and "不支持" not in v["note"] and "没在这个版本上验证过" not in v["note"]
    u = classify((4, 2, 0, 8))  # 超出又没验证过
    assert u["level"] == "unsupported" and "没在这个版本上验证过" in u["note"] and "4.1.15" in u["note"]
    assert classify((4, 1, 8, 200))["level"] == "unsupported"
    assert classify((3, 9, 12, 0))["level"] == "old"
    # 已验证只认 4.1.15 这一系：4.1.16 没验证过
    assert classify((4, 1, 16, 1))["level"] == "unsupported"


def test_declared_range_is_read_from_the_pypi_description():
    assert parse_declared("# wxauto\n\n适用最新4.1.8\n\n## 获取实例") == "4.1.8"
    assert parse_declared("适用微信 4.2.0.8 及以下") == "4.2.0.8"
    assert parse_declared("没写") == "" and parse_declared("") == ""


def test_out_of_range_version_notifies_once_per_version_and_hangs_a_notice(env, monkeypatch):
    async def body(e):
        notes = []
        e.engine.notify_hook = notes.append
        ch = make(e)
        for _ in range(3):  # 连了好几次（重连、重启）
            ch.announce_version(dict(classify((4, 2, 0, 8)), installed=True))
        assert len(notes) == 1 and "4.2.0.8" in notes[0] and "没在这个版本上验证过" in notes[0]
        n = e.core.db.kv("wechat_notice")
        assert n["version"] == "4.2.0.8" and n["level"] == "unsupported"
        b = e.core.db.kv("wechat_notice")
        assert b is not None
        ch.announce_version(dict(classify((4, 2, 1, 0)), installed=True))  # 又换了个版本：再通知一次
        assert len(notes) == 2 and "4.2.1.0" in notes[1]
        # 在范围内：不通知、提示撤掉
        ch.announce_version(dict(classify((4, 1, 8, 20)), installed=True))
        assert len(notes) == 2 and e.core.db.kv("wechat_notice") is None
    env.run(body)


def test_verified_version_gets_a_different_message(env):
    async def body(e):
        notes = []
        e.engine.notify_hook = notes.append
        ch = make(e)
        ch.announce_version(dict(classify((4, 1, 15, 56)), installed=True))
        ch.announce_version(dict(classify((4, 1, 15, 56)), installed=True))
        assert len(notes) == 1 and "实测能用" in notes[0] and "不支持" not in notes[0]
        assert e.core.db.kv("wechat_notice")["level"] == "verified"
        logs = [r for r in e.core.db.rows("SELECT level, text FROM logs WHERE cat='system'") if "4.1.15.56" in r["text"]]
        assert logs and all(r["level"] == "info" for r in logs)  # 已验证的不是警告（私聊通知那条另算，是通知通道自己的日志）
    env.run(body)


def test_connecting_checks_the_version(env, monkeypatch):
    async def body(e):
        notes = []
        e.engine.notify_hook = notes.append
        monkeypatch.setattr(wechat, "check_version", lambda: dict(classify((4, 2, 0, 8)), installed=True, path="x"))
        monkeypatch.setattr(wechat, "lib_installed", lambda: False)
        ch = make(e)
        ch.lib = None  # 走真实的版本核对（测试里默认换成了拒绝连接的假库）
        await ch.start()
        assert len(notes) == 1 and "4.2.0.8" in notes[0]
        await ch.start()
        assert len(notes) == 1  # 每个版本只通知一次
    env.run(body)


# ---------- 每天查 wxauto4 新版 ----------
def pypi(version, desc="适用最新4.1.8", files=()):
    return {"info": {"version": version, "description": desc}, "urls": list(files)}


def fake_install(pylib: Path, version="41.1.7", marker="old"):
    (pylib / "wxauto4").mkdir(parents=True, exist_ok=True)
    (pylib / "wxauto4" / "__init__.py").write_text(f"VERSION = '{marker}'", encoding="utf-8")
    d = pylib / f"wxauto4-{version}.dist-info"
    d.mkdir(exist_ok=True)
    (d / "METADATA").write_text(f"Name: wxauto4\nVersion: {version}\n", encoding="utf-8")


def make_wheel(path: Path, version: str, marker="new", broken=False):
    if broken:
        path.write_bytes(b"this is not a zip")
        return
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("wxauto4/__init__.py", f"VERSION = '{marker}'")
        z.writestr(f"wxauto4-{version}.dist-info/METADATA", f"Name: wxauto4\nVersion: {version}\n")


def test_new_version_is_reported_once_with_declared_range(env, monkeypatch, tmp_path):
    async def body(e):
        notes = []
        e.engine.notify_hook = notes.append
        fake_install(wechat.pylib())
        monkeypatch.setattr(wechat, "pypi_info", lambda: pypi("41.2.0", "适用最新4.2.0"))
        monkeypatch.setattr(wechat, "check_version", lambda: dict(classify((4, 1, 15, 56)), installed=True))
        await e.core.check_wxauto()
        u = e.core.db.kv("wxlib_update")
        assert u["latest"] == "41.2.0" and u["installed"] == "41.1.7" and u["declared"] == "4.2.0"
        assert len(notes) == 1 and "41.2.0" in notes[0] and "声明适用到微信 4.2.0" in notes[0] and "可以把微信升到 4.2.0 了" in notes[0]
        assert "升级 wxauto4" in notes[0]
        await e.core.check_wxauto()  # 同一个新版不再通知
        assert len(notes) == 1
        # 再出更新的版本：再通知一次
        monkeypatch.setattr(wechat, "pypi_info", lambda: pypi("41.3.0", ""))
        await e.core.check_wxauto()
        assert len(notes) == 2 and "没有写明适用的微信版本" in notes[1]
        # 没有新版：提示撤掉
        monkeypatch.setattr(wechat, "pypi_info", lambda: pypi("41.1.7"))
        await e.core.check_wxauto()
        assert e.core.db.kv("wxlib_update") is None
        # 只提醒，不自动升级：装的还是旧的
        assert wechat.installed_lib_version() == "41.1.7" and "old" in (wechat.pylib() / "wxauto4" / "__init__.py").read_text(encoding="utf-8")
    env.run(body)


def test_new_version_covering_the_current_wechat_is_mentioned(env, monkeypatch):
    async def body(e):
        fake_install(wechat.pylib())
        monkeypatch.setattr(wechat, "pypi_info", lambda: pypi("41.1.9", "适用最新4.1.20"))
        monkeypatch.setattr(wechat, "check_version", lambda: dict(classify((4, 1, 15, 56)), installed=True))
        await e.core.check_wxauto()
        u = e.core.db.kv("wxlib_update")
        assert u["wechat_up_to"] == "4.1.20" and "可以把微信升到 4.1.20 了" in u["text"]
    env.run(body)


def test_the_check_can_be_turned_off_and_is_not_run_without_the_lib(env, monkeypatch):
    async def body(e):
        calls = []

        def stub():
            calls.append(1)
            return pypi("41.2.0")
        monkeypatch.setattr(wechat, "pypi_info", stub)
        fake_install(wechat.pylib())
        e.core.settings.set("platform:wechat", "wechat_lib_check", False)
        await e.core.check_wxauto()
        assert not calls and e.core.db.kv("wxlib_update") is None  # 关了：不联网
        e.core.settings.set("platform:wechat", "wechat_lib_check", True)
        await e.core.check_wxauto()
        assert calls
        # 没装 wxauto4（还没下载组件）：不查
        import shutil
        shutil.rmtree(wechat.pylib())
        calls.clear()
        await e.core.check_wxauto()
        assert not calls
        # 联网失败：安静地跳过，不报错
        fake_install(wechat.pylib())
        monkeypatch.setattr(wechat, "pypi_info", lambda: (_ for _ in ()).throw(OSError("断网")))
        await e.core.check_wxauto()
    env.run(body)


def test_setting_defaults():
    from mb.settings import BY_KEY
    s = BY_KEY["wechat_lib_check"]
    assert s["default"] is True and s["scopes"] == ["P"] and s["platforms"] == ["wechat"]


# ---------- 升级按钮 ----------
def test_upgrade_success_replaces_the_old_version(env, monkeypatch, tmp_path):
    async def body(e):
        pylib = wechat.pylib()
        fake_install(pylib)
        whl = tmp_path / WHEEL
        make_wheel(whl, "41.2.0")
        monkeypatch.setattr(wechat, "pypi_info", lambda: pypi("41.2.0", files=[{"filename": whl.name, "url": whl.as_uri()}]))
        r = wechat.upgrade_lib()
        assert r == {"old": "41.1.7", "new": "41.2.0"}
        assert wechat.installed_lib_version() == "41.2.0"
        assert "new" in (pylib / "wxauto4" / "__init__.py").read_text(encoding="utf-8")
        assert not list(pylib.glob("wxauto4-41.1.7.dist-info"))  # 旧的清掉了
        assert not (wechat.data_dir() / "pylib.bak").exists() and not (wechat.data_dir() / "pylib.new").exists()
    env.run(body)


@pytest.mark.parametrize("how", ["broken_download", "extract_fails", "wrong_version"])
def test_upgrade_failure_rolls_back_to_the_original_version(env, monkeypatch, tmp_path, how):
    async def body(e):
        pylib = wechat.pylib()
        fake_install(pylib)
        whl = tmp_path / WHEEL
        make_wheel(whl, "41.2.0" if how != "wrong_version" else "40.0.0", broken=how == "broken_download")
        monkeypatch.setattr(wechat, "pypi_info", lambda: pypi("41.2.0", files=[{"filename": whl.name, "url": whl.as_uri()}]))
        if how == "extract_fails":  # 旧的已经删掉、解压到一半出错
            def boom(self, path=None, members=None, pwd=None):
                (Path(path) / "wxauto4").mkdir(parents=True, exist_ok=True)
                (Path(path) / "wxauto4" / "__init__.py").write_text("half", encoding="utf-8")
                raise OSError("磁盘满了")
            monkeypatch.setattr(zipfile.ZipFile, "extractall", boom)
        with pytest.raises(Exception):
            wechat.upgrade_lib()
        assert wechat.installed_lib_version() == "41.1.7"
        assert "old" in (pylib / "wxauto4" / "__init__.py").read_text(encoding="utf-8")
        assert not (wechat.data_dir() / "pylib.bak").exists() and not (wechat.data_dir() / "pylib.new").exists()
    env.run(body)


def test_upgrade_endpoint_refuses_while_wechat_runs_and_logs_a_rollback(env, monkeypatch, tmp_path):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        ch = make(e)
        ch.status = "online"
        r = await c.post("/api/wechat/upgrade")
        assert r.status == 400 and "先在账号页停止微信账号" in (await r.json())["error"]
        ch.status = "stopped"
        fake_install(wechat.pylib())
        monkeypatch.setattr(wechat, "upgrade_lib", lambda: (_ for _ in ()).throw(RuntimeError("网络断了")))
        r = await c.post("/api/wechat/upgrade")
        body_ = await r.json()
        assert r.status == 500 and "已回退到原来的版本 41.1.7" in body_["error"]
        assert [x for x in e.core.db.rows("SELECT level, text FROM logs") if "升级 wxauto4 失败，已回退" in x["text"]]
        monkeypatch.setattr(wechat, "upgrade_lib", lambda: {"old": "41.1.7", "new": "41.2.0"})
        e.core.db.set_kv("wxlib_update", {"latest": "41.2.0"})
        r = await c.post("/api/wechat/upgrade")
        assert r.status == 200 and (await r.json())["new"] == "41.2.0" and e.core.db.kv("wxlib_update") is None
        await c.close()
    env.run(body)


def test_bootstrap_carries_the_notices(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        e.core.db.set_kv("wechat_notice", {"version": "4.2.0.8", "level": "unsupported", "note": "x"})
        e.core.db.set_kv("wxlib_update", {"text": "有新版"})
        b = await (await c.get("/api/bootstrap")).json()
        assert b["wechat_notice"]["version"] == "4.2.0.8" and b["wechat_lib_update"]["text"] == "有新版"
        await c.close()
    env.run(body)
