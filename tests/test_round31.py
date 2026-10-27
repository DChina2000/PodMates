"""NapCat 保活：NapCat 不依附 PodMates，PodMates 被强行结束 / 重启后接回原来的；接不回就清掉重开；用户明确要关时才停；安装程序装新版时不停它、卸载时全停。
NapCat 用桩进程模拟（tests/napcat_stub.py），不启动真的 NapCat / QQ，不连任何账号。"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import napcat_support as support
from mb.channels import napcat

ROOT = Path(__file__).resolve().parent.parent
HOLDER = Path(__file__).resolve().parent / "napcat_holder.py"
UIN = "123456789"
psutil = pytest.importorskip("psutil")


def wait_until(cond, timeout=15.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


async def await_until(cond, timeout=15.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    return cond()


async def ws_ready(port, token, uin=UIN, timeout=20.0):
    """桩的 ws 起来了、get_login_info 返回这个号。"""
    end = time.time() + timeout
    while time.time() < end:
        got, _ = await napcat.probe_login(f"ws://127.0.0.1:{port}", token, timeout=2)
        if got == uin:
            return True
        await asyncio.sleep(0.2)
    return False


def alive(pid) -> bool:
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """每个测试一个临时数据目录、一个假的 NapCat 目录、一个控制目录；结束时只按记下来的 pid 清掉桩进程。"""
    data = tmp_path / "data"
    data.mkdir()
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    monkeypatch.setenv("PODMATES_DATA", str(data))
    support.install(napcat, ctl, monkeypatch)

    class Rig:
        pass

    r = Rig()
    r.tmp, r.data, r.ctl, r.root, r.port, r.token = tmp_path, data, ctl, support.fake_root(tmp_path), support.free_port(), "tok123"
    r.log = lambda uin=UIN: napcat.log_path(uin)
    r.mk = lambda root=None, port=None, uin=UIN, hide=True: napcat.NapCatProcess(r.ch, root or r.root, uin, port or r.port, r.token, (port or r.port) + 1, hide, "")
    r.ch = support.FakeChannel(r.port)
    yield r
    for uin in list(napcat.load_pids()) + [UIN]:
        for root in (r.root, r.tmp / "NapCat.Other"):
            napcat.kill_procs(napcat.leftovers(root, uin))
    for p in psutil.process_iter(["cmdline"]):  # 兜底：本测试目录里起的桩（命令行里带这次的控制目录）
        try:
            if str(ctl) in " ".join(p.info["cmdline"] or []):
                p.kill()
        except psutil.Error:
            pass


# ---------- 日志文件 ----------
def test_log_file_is_append_only_so_clearing_it_leaves_no_hole_and_the_child_keeps_writing(rig, tmp_path):
    """NapCat 的输出写进日志文件；文件太大时 PodMates 把它清空，NapCat 之后写的接着落在文件开头（不是中间留一大段 0）。"""
    script = tmp_path / "w.py"
    script.write_text("import sys, time\nfor i in range(40):\n    sys.stdout.write('第%d行\\n' % i); sys.stdout.flush(); time.sleep(0.1)\n", encoding="utf-8")
    lp = napcat.log_path("111")
    napcat.rotate_log(lp)
    fd = napcat.open_log_for_child(lp)
    p = napcat.spawn_detached([sys.executable, str(script)], str(tmp_path), dict(os.environ, PYTHONIOENCODING="utf-8"), True, fd)
    os.close(fd)
    tail = napcat.LogTail(lp, at_end=False)
    got: list[str] = []
    assert wait_until(lambda: got.extend(tail.read()) or len(got) >= 5, 10)
    for _ in range(100):  # 读完了而且超过上限：清空（NapCat 一直在写，读完到清空之间可能又来一行，那次不清，下一轮再试）
        got.extend(tail.read())
        tail.trim(limit=10)
        if tail.pos == 0:
            break
        time.sleep(0.02)
    assert tail.pos == 0
    p.wait(20)
    got.extend(tail.read())
    assert got[-1] == "第39行" and "第0行" in got
    raw = lp.read_bytes()
    assert b"\x00" not in raw  # 没有空洞
    assert raw.decode("utf-8").splitlines()[-1] == "第39行"


def test_log_tail_reads_complete_lines_only_and_starts_over_when_the_file_is_cleared(tmp_path):
    f = tmp_path / "x.log"
    f.write_bytes("老的一行\n".encode())
    tail = napcat.LogTail(f, at_end=True)
    assert tail.read() == []  # 接回时从末尾开始，不重放旧的
    with open(f, "ab") as w:
        w.write("\x1b[32m二维码解码URL: http://qr.test/1\x1b[0m\n半".encode())
    assert tail.read() == ["二维码解码URL: http://qr.test/1"]  # 颜色码去掉；半行留着
    with open(f, "ab") as w:
        w.write("行\n".encode())
    assert tail.read() == ["半行"]
    f.write_bytes(b"")  # 被清空
    assert tail.read() == []
    f.write_bytes("新的\n".encode())
    assert tail.read() == ["新的"]


# ---------- 强行结束 PodMates 后 NapCat 还在；重启后接回 ----------
def test_napcat_survives_podmates_being_force_killed_and_the_next_start_reattaches_without_relaunching(rig):
    ready = rig.tmp / "ready.json"
    holder = subprocess.Popen([sys.executable, str(HOLDER), str(rig.root), UIN, str(rig.port), rig.token, str(rig.ctl), str(ready)],
                              env=dict(os.environ, PODMATES_DATA=str(rig.data)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_until(ready.exists, 30)
        boot = json.loads(ready.read_text(encoding="utf-8"))["boot"]
        assert asyncio.run(ws_ready(rig.port, rig.token))
        assert wait_until(lambda: UIN in napcat.load_pids(), 15)  # 进程记下来了
        assert rig.log().exists() and "桩 NapCat 启动" in rig.log().read_text(encoding="utf-8")
        # 强行结束 PodMates（TerminateProcess，什么清理都来不及做）
        psutil.Process(holder.pid).kill()
        holder.wait(10)
        time.sleep(2)
        assert alive(boot)  # NapCat 桩还在
        assert asyncio.run(ws_ready(rig.port, rig.token, timeout=3))  # 还连得上、还登录着
        (rig.ctl / "emit.txt").write_text("PodMates 死了之后打出的一行\n", encoding="utf-8")
        assert wait_until(lambda: "PodMates 死了之后打出的一行" in rig.log().read_text(encoding="utf-8"), 10)  # 输出没因为管道断了而丢 / 崩：写的是文件
        before = set(napcat.owned(rig.root, UIN))
        assert boot in before

        async def restart():
            p = rig.mk()
            await p.start()
            assert p.attached and p.popen is None  # 接回，没有新拉起
            assert any("接回了已在运行的 NapCat，QQ 没有重新登录" in t for t in rig.ch.engine.texts())
            assert set(napcat.owned(rig.root, UIN)) == before  # 一个新进程都没有，旧的一个都没少
            (rig.ctl / "emit.txt").write_text("二维码解码URL: http://qr.test/abc\n[error] [KickedOffLine] 你的账号在别处登录\n", encoding="utf-8")
            assert await await_until(lambda: rig.ch.qrs and any("被踢下线" in n for n in rig.ch.engine.notices), 10)
            assert rig.ch.qrs == ["http://qr.test/abc"]  # 二维码、踢下线照常从日志文件认出来
            assert any("需要扫码登录" in t for t in rig.ch.engine.texts())
            await p.stop()

        asyncio.run(restart())
        assert wait_until(lambda: not alive(boot), 10)
        assert napcat.load_pids().get(UIN) is None and UIN not in napcat.load_run()
    finally:
        holder.kill()
        napcat.kill_procs(napcat.leftovers(rig.root, UIN))


def test_reattach_notices_when_the_napcat_it_picked_up_disappears(rig):
    async def body():
        p1 = rig.mk()
        await p1.start()
        assert await ws_ready(rig.port, rig.token)
        p1.closed = True  # 模拟 PodMates 没了：这个对象不再管它
        ch2 = support.FakeChannel(rig.port)
        p2 = napcat.NapCatProcess(ch2, rig.root, UIN, rig.port, rig.token, rig.port + 1, True, "")
        await p2.start()
        assert p2.attached
        napcat.kill_procs(napcat.leftovers(rig.root, UIN))
        assert await await_until(lambda: any("NapCat 停了" in n for n in ch2.engine.notices), 30)
        assert ch2.statuses[-1][0] == "error"
    asyncio.run(body())


# ---------- 接不回：清掉重开，日志写明原因 ----------
def test_nothing_running_just_starts_and_says_so(rig):
    async def body():
        p = rig.mk()
        await p.start()
        assert not p.attached and p.popen is not None
        assert any("没有在运行的 NapCat，直接启动" in t for t in rig.ch.engine.texts())
        assert await ws_ready(rig.port, rig.token)
        await p.stop()
        assert not alive(p.boot_pid)
    asyncio.run(body())


@pytest.mark.parametrize("case", ["dead", "unreachable", "other_account", "other_folder", "other_port"])
def test_when_the_old_napcat_cannot_be_picked_up_it_is_cleaned_and_started_again_with_the_reason(rig, case):
    async def body():
        if case == "unreachable":
            (rig.ctl / "nows").write_text("")  # 旧的进程在，但没有 ws
        if case == "other_account":
            (rig.ctl / "login.txt").write_text("999")  # 旧的 ws 在，登录的是别的号
        p1 = rig.mk()
        await p1.start()
        old_map = napcat.owned(rig.root, UIN)
        old = set(old_map)
        assert p1.boot_pid in old
        if case in ("unreachable", "other_account"):
            assert await await_until(lambda: "桩 NapCat 启动" in rig.log().read_text(encoding="utf-8"), 20)
            if case == "other_account":
                assert await ws_ready(rig.port, rig.token, uin="999")
            (rig.ctl / "nows").unlink(missing_ok=True)  # 新起的桩要有 ws、登录自己的号
            (rig.ctl / "login.txt").unlink(missing_ok=True)
        else:
            assert await ws_ready(rig.port, rig.token)
        p1.closed = True  # PodMates 没了
        if case == "dead":
            napcat.kill_procs(old_map)
            assert await await_until(lambda: not any(alive(x) for x in old), 10)
        ch2 = support.FakeChannel(rig.port)
        root2, port2 = rig.root, rig.port
        if case == "other_folder":
            root2 = support.fake_root(rig.tmp, "NapCat.Other")
        p2 = napcat.NapCatProcess(ch2, root2, UIN, port2, rig.token, port2 + 1, True, "")
        if case == "other_port":
            p2.port = rig.port  # 地址没变（监听端口是桩自己定的），但上一次记下的端口和现在设置的不一样
            run = napcat.load_run()
            run[UIN]["port"] = rig.port + 500
            (rig.data / napcat.RUN_FILE).write_text(json.dumps(run), encoding="utf-8")
        await p2.start()
        assert not p2.attached and p2.popen is not None, case
        texts = ch2.engine.texts()
        reasons = {"dead": "没有在运行的 NapCat", "unreachable": "连不上", "other_account": "登录的是 999，不是 " + UIN,
                   "other_folder": "别的文件夹", "other_port": "ws 端口"}
        assert any(reasons[case] in t for t in texts), (case, texts)
        if case != "dead":
            assert any("没能接回原来的 NapCat" in t and "清理后重新启动" in t for t in texts)
            assert await await_until(lambda: all(not alive(x) for x in old), 10)  # 旧的清掉了（结束进程不是瞬间完成的）
        assert p2.boot_pid not in old and alive(p2.boot_pid)
        assert await ws_ready(rig.port, rig.token)  # 新的是好的
        await p2.stop()
    asyncio.run(body())


def test_run_record_follows_the_current_napcat(rig):
    async def body():
        p = rig.mk()
        await p.start()
        run = napcat.load_run()[UIN]
        assert run["root"] == str(rig.root) and run["port"] == rig.port and run["log"] == str(rig.log())
        await p.stop()
        assert UIN not in napcat.load_run()
    asyncio.run(body())


def test_silent_log_file_is_called_out_but_a_talking_one_is_not(rig):
    """起来 45 秒了日志文件还是空的（比如这个 NapCat 不肯往标准输出写）：写一行警告，别让人以为二维码会自己出现；正常出声的不报。"""
    async def body():
        (rig.ctl / "silent").write_text("")
        p = rig.mk()
        await p.start()
        assert await ws_ready(rig.port, rig.token)  # 桩已经跑起来了，它不出声
        p.pump()
        assert rig.log().stat().st_size == 0 and not p.warned_silent  # 刚起来，还不到 45 秒
        p.started -= 60
        p.pump()
        assert p.warned_silent and any("还是空的" in t for t in rig.ch.engine.texts())
        await p.stop()
        (rig.ctl / "silent").unlink()
        rig.ch.engine.logs.clear()
        q = rig.mk()
        await q.start()
        assert await await_until(lambda: rig.log().exists() and rig.log().stat().st_size > 0, 15)
        q.started -= 60
        q.pump()
        assert not q.warned_silent and not any("还是空的" in t for t in rig.ch.engine.texts())
        await q.stop()
    asyncio.run(body())


def test_stale_napcat_of_an_account_that_is_not_started_is_cleaned_but_running_ones_are_not(rig):
    async def body():
        a, b = rig.mk(uin="111"), rig.mk(uin="222", port=support.free_port())
        await a.start()
        await b.start()
        pa, pb = a.boot_pid, b.boot_pid
        a.closed = b.closed = True
        gone = napcat.reap_unwanted({"222"})
        assert list(gone) == ["111"] and pa in gone["111"]
        assert await await_until(lambda: not alive(pa), 10) and alive(pb)  # 进程退出要一点时间：全量负载下当场查会偶发还活着
        assert "111" not in napcat.load_pids() and "222" in napcat.load_pids()
        napcat.reap_unwanted(set())
        assert await await_until(lambda: not alive(pb), 10)
    asyncio.run(body())


# ---------- 用户明确要关的时候：停 ----------
def _qq_env_account(e, rig, aid="ac_qq1"):
    e.core.db.set_kv("napcat_dir", str(rig.root))
    return e.core.db.put("account", {"id": aid, "platform": "qq", "qq": UIN, "role_id": "r_haitun", "manage_napcat": True, "ws_port": rig.port, "webui_port": rig.port + 1})


async def _running_account(e, rig, aid="ac_qq1"):
    acc = _qq_env_account(e, rig, aid)
    await e.engine.start_account(acc)
    ch = e.engine.bots[aid].channel
    assert await await_until(lambda: ch.napcat and ch.napcat.popen is not None, 20)
    assert await await_until(lambda: ch.status == "online", 30)
    return ch, ch.napcat.boot_pid


def test_panel_stop_account_stop_bot_and_tray_exit_all_stop_napcat(env, rig):
    from mb.app import Desktop

    async def body(e):
        # 面板：停止这个账号
        ch, boot = await _running_account(e, rig)
        await e.engine.stop_account("ac_qq1")
        assert await await_until(lambda: not alive(boot), 10) and not napcat.leftovers(rig.root, UIN)
        # 面板：停止整个机器人（/api/engine/stop → core.stop）
        ch, boot = await _running_account(e, rig)
        e.core.running = True
        await e.core.stop()
        assert await await_until(lambda: not alive(boot), 10) and not napcat.leftovers(rig.root, UIN)
        # 托盘「退出」
        ch, boot = await _running_account(e, rig)

        class Srv:
            stopped = False

            async def stop(self):
                Srv.stopped = True

        class Icon:
            stopped = False

            def stop(self):
                Icon.stopped = True

        d = Desktop(False)
        d.loop.close()
        d.loop, d.core, d.server, d.data = asyncio.get_running_loop(), e.core, Srv(), rig.tmp
        (rig.tmp / "runtime.json").write_text("{}")
        await asyncio.get_running_loop().run_in_executor(None, d.exit_app, Icon())
        assert Srv.stopped and Icon.stopped and not (rig.tmp / "runtime.json").exists()
        assert await await_until(lambda: not alive(boot), 10) and not napcat.leftovers(rig.root, UIN)
    env.run(body)


def test_restarting_podmates_reattaches_through_the_real_channel_and_the_account_comes_back_online(env, rig):
    async def body(e):
        ch, boot = await _running_account(e, rig)
        old = set(napcat.owned(rig.root, UIN))
        # 模拟 PodMates 进程没了：不走任何停止流程，只把这个渠道丢掉（NapCat 桩继续跑），再按启动流程来一遍
        ch.stopping = True
        ch.task.cancel()
        if ch.ws:
            await ch.ws.close()
        ch.napcat.closed = True
        await ch.session.close()
        e.engine.bots.pop("ac_qq1")
        e.core.db.put("account", {**e.core.db.doc("account", "ac_qq1"), "enabled": True})
        e.core.db.set_kv("platforms", {"qq": True})
        await e.engine.start_all()
        ch2 = e.engine.bots["ac_qq1"].channel
        assert await await_until(lambda: ch2.status == "online", 30)
        assert ch2.napcat.attached and ch2.napcat.popen is None
        assert set(napcat.owned(rig.root, UIN)) == old  # 没重开
        assert any("接回了已在运行的 NapCat，QQ 没有重新登录" in r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='napcat'"))
        await e.engine.stop_account("ac_qq1")
        assert await await_until(lambda: not alive(boot), 10)
    env.run(body)


def test_start_all_cleans_the_napcat_of_an_account_that_is_no_longer_started(env, rig):
    async def body(e):
        ch, boot = await _running_account(e, rig)
        ch.stopping = True
        ch.task.cancel()
        ch.napcat.closed = True
        await ch.session.close()
        e.engine.bots.pop("ac_qq1")
        e.core.db.put("account", {**e.core.db.doc("account", "ac_qq1"), "enabled": False})  # 停用了
        await e.engine.start_all()
        assert await await_until(lambda: not alive(boot), 10)
        assert any("这次不启动，清掉了" in r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='napcat'"))
    env.run(body)


# ---------- 安装程序 ----------
def _spawn(exe, args="-n 300 127.0.0.1", extra_cmd=None):
    """用 exe 的副本起一个会睡一阵的进程（ping 或 cmd 的副本，路径、进程名、命令行都可以按需要摆）。"""
    return subprocess.Popen(f'"{exe}" {args}', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def _orphan(exe, args):
    """起一个进程，让它的父进程马上退出：成了孤儿（一键包的 QQ 本体就是这样）。返回 pid。"""
    code = "import subprocess,sys; p=subprocess.Popen(sys.argv[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL); print(p.pid)"
    out = subprocess.run([sys.executable, "-c", code, f'"{exe}" {args}'], capture_output=True, text=True, timeout=30).stdout
    return int(out.strip())


@pytest.mark.skipif(not shutil.which("powershell"), reason="没有 powershell")
def test_installer_script_install_mode_stops_only_podmates_and_uninstall_mode_stops_everything(tmp_path):
    ping, cmdexe = Path(r"C:\Windows\System32\ping.exe"), Path(r"C:\Windows\System32\cmd.exe")
    app, other, own = tmp_path / "fake app", tmp_path / "other", tmp_path / "own qq"
    nap = app / "data" / "napcat" / "NapCat.Shell"
    for d in (app, nap / "versions" / "9", other / "qqbody", own):
        d.mkdir(parents=True)
    shutil.copy(ping, app / "PodMates.exe")
    shutil.copy(ping, app / "helper.exe")  # 面板窗口之类 PodMates 自己带的
    shutil.copy(ping, nap / "NapCatWinBootMain.exe")  # NapCat 启动器
    shutil.copy(cmdexe, nap / "versions" / "9" / "QQ.exe")  # 一键包的 QQ 本体（在数据目录里）
    shutil.copy(cmdexe, other / "qqbody" / "QQ.exe")  # 系统装的 QQ 被 NapCat 用着，启动器已退出成了孤儿（Shell 方式）
    shutil.copy(ping, other / "rec.exe")  # pid 文件里记着的 NapCat 进程（不在安装目录下）
    shutil.copy(cmdexe, own / "QQ.exe")  # 用户自己开的 QQ：既没有 --enable-logging 也没有 -q
    sleep = '/c "ping -n 300 127.0.0.1 >nul & rem {}"'
    procs = {
        "podmates": _spawn(app / "PodMates.exe"), "helper": _spawn(app / "helper.exe"), "launcher": _spawn(nap / "NapCatWinBootMain.exe"),
        "recorded": _spawn(other / "rec.exe"), "own": _spawn(own / "QQ.exe", sleep.format("")),
        "own_q": _spawn(own / "QQ.exe", sleep.format("-q 123456789")),  # 带了 -q 但没有 --enable-logging：也不是 NapCat 起的
    }
    pids = {k: p.pid for k, p in procs.items()}
    pids["onekey_qq"] = _orphan(nap / "versions" / "9" / "QQ.exe", sleep.format("--enable-logging -q 123456789"))
    pids["orphan_qq"] = _orphan(other / "qqbody" / "QQ.exe", sleep.format("--enable-logging -q 123456789"))
    try:
        time.sleep(1.5)
        pf = tmp_path / "pids.json"
        pf.write_text(json.dumps({"1": [[pids["recorded"], psutil.Process(pids["recorded"]).create_time()]]}), encoding="utf-8")
        script = ROOT / "installer" / "stop-podmates.ps1"
        base = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Roots", str(app), "-PidFiles", str(pf)]
        # 装新版：只停 PodMates 自己
        dry = subprocess.run([*base, "-KeepNapCat", "-ProtectDirs", str(app / "data"), "-DryRun"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert dry.returncode == 0 and all(alive(p) for p in pids.values())  # 只看不动
        assert str(pids["podmates"]) in dry.stdout and str(pids["launcher"]) not in dry.stdout
        r = subprocess.run([*base, "-KeepNapCat", "-ProtectDirs", str(app / "data")], capture_output=True)
        assert r.returncode == 0
        time.sleep(1)
        assert not alive(pids["podmates"]) and not alive(pids["helper"])
        for k in ("launcher", "onekey_qq", "orphan_qq", "recorded", "own", "own_q"):
            assert alive(pids[k]), f"装新版时不该停 {k}"
        # 卸载：NapCat 和它的 QQ 全部停掉，用户自己的 QQ 还是不碰
        r = subprocess.run(base, capture_output=True)
        assert r.returncode == 0
        time.sleep(1)
        for k in ("launcher", "onekey_qq", "orphan_qq", "recorded"):
            assert not alive(pids[k]), f"卸载时该停 {k}"
        assert alive(pids["own"]) and alive(pids["own_q"])
    finally:
        for pid in pids.values():
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def test_installer_uses_install_mode_for_new_versions_and_full_stop_for_uninstall():
    iss = (ROOT / "installer/PodMates.iss").read_text(encoding="utf-8-sig")
    assert "procedure StopApp(Script: String; KeepNapCat: Boolean);" in iss
    assert "StopApp(ExpandConstant('{tmp}\\stop-podmates.ps1'), True)" in iss  # 装新版：不停 NapCat
    assert "StopApp(ExpandConstant('{app}\\tools\\stop-podmates.ps1'), False)" in iss  # 卸载：全停
    assert "'/F /IM {#AppExe}'" in iss and "'/F /T /IM {#AppExe}'" in iss  # 装新版的 taskkill 不带 /T（/T 会连 NapCat 一起结束）
    assert "-KeepNapCat -ProtectDirs" in iss
    raw = (ROOT / "installer/stop-podmates.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"KeepNapCat" in raw


def test_installer_never_writes_into_the_data_folder():
    """装新版覆盖的文件：PodMates.exe、_internal、文档、tools 下的脚本、许可证——全在程序目录里；数据目录（NapCat 装在里面）只建空文件夹，卸载时也只在用户点「是」才删。"""
    iss = (ROOT / "installer/PodMates.iss").read_text(encoding="utf-8-sig")
    files = iss.split("[Files]")[1].split("[Icons]")[0]
    dests = [ln.split("DestDir:")[1].split(";")[0].strip().strip('"') for ln in files.splitlines() if "DestDir:" in ln]
    assert dests and all("data" not in d.replace("{app}", "").split("\\") for d in dests), dests
    assert "uninsneveruninstall" in iss.split("[Dirs]")[1].split("[Files]")[0]
    assert "Name: \"{app}\\_internal\"" in iss  # 卸载只删程序自己带的文件夹
