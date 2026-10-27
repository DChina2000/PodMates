"""NapCat 保活的补充：外部程序不继承本程序的 DLL 搜索目录（不然它们一直占着程序目录里的运行库，装新版覆盖不了）；安装脚本装新版前把占着程序目录文件的 NapCat 进程也停掉。
全部用桩进程；DLL 用系统 DLL 的副本冒充「程序目录（_internal）里的文件」。"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import napcat_support as support
from mb import winproc
from mb.channels import napcat

psutil = pytest.importorskip("psutil")
ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name != "nt", reason="只有 Windows 上才有 DLL 搜索目录")


def wait_until(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def kill_tree(pid):
    try:
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            c.kill()
        p.kill()
    except psutil.Error:
        pass


@pytest.fixture
def dlldir(tmp_path):
    """假装的程序目录（_internal）：里面有一个只有它那里才有的 DLL；把它设成本测试进程的 DLL 搜索目录（和 PyInstaller 做的一样），结束时还原。"""
    internal = tmp_path / "fake app" / "_internal"
    internal.mkdir(parents=True)
    shutil.copy(r"C:\Windows\System32\version.dll", internal / "pmfake.dll")
    before = winproc.current_dll_directory()
    winproc._k32().SetDllDirectoryW(str(internal))
    yield internal
    winproc._k32().SetDllDirectoryW(before or None)


def run_stub(tmp_path, spawn):
    ctl = tmp_path / "ctl"
    ctl.mkdir(exist_ok=True)
    (ctl / "probe_dll").write_text("")
    cmd = [sys.executable, support.STUB, "123456789", str(support.free_port()), "tok", str(ctl)]
    fd = napcat.open_log_for_child(tmp_path / "out.log")
    try:
        p = spawn(cmd, str(tmp_path), dict(os.environ), True, fd)
    finally:
        os.close(fd)
    try:
        assert wait_until((ctl / "modules.txt").exists, 30)
        return json.loads((ctl / "modules.txt").read_text(encoding="utf-8"))
    finally:
        kill_tree(p.pid)


def plain_spawn(cmd, cwd, env, hide, fd):
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, creationflags=subprocess.CREATE_NO_WINDOW)


def test_without_the_fix_a_child_does_load_dlls_from_the_program_directory(tmp_path, dlldir):
    """对照：直接拉起的子进程，会按名字从 SetDllDirectory 设的目录里加载 DLL（所以这个测试能抓到问题）。"""
    got = run_stub(tmp_path, plain_spawn)
    assert got["found"] is True
    assert any(str(dlldir).lower() in m.lower() for m in got["mods"])


def test_napcat_children_do_not_load_anything_from_the_program_directory(tmp_path, dlldir):
    got = run_stub(tmp_path, napcat.spawn_detached)
    assert got["found"] is False  # 按名字找不到程序目录里的 DLL
    assert not [m for m in got["mods"] if str(dlldir.parent).lower() in m.lower()]  # 模块列表里没有任何一个来自程序目录
    # 拉起之后，本进程自己的 DLL 搜索目录还原了，自己还能加载那里的文件
    assert winproc.current_dll_directory().lower() == str(dlldir).lower()
    import ctypes
    ctypes.WinDLL("pmfake.dll")


def test_dll_directory_is_restored_even_when_the_spawn_fails(tmp_path, dlldir):
    with pytest.raises(RuntimeError):
        with winproc.private_dll_directory():
            assert winproc.current_dll_directory() == ""
            raise RuntimeError("拉起失败")
    assert winproc.current_dll_directory().lower() == str(dlldir).lower()
    with pytest.raises(FileNotFoundError):
        napcat.spawn_detached([str(tmp_path / "不存在.exe")], str(tmp_path), dict(os.environ), True, None)
    assert winproc.current_dll_directory().lower() == str(dlldir).lower()


def test_child_env_drops_the_program_directory_from_path_and_pyinstaller_variables(tmp_path):
    home = tmp_path / "app" / "_internal"
    env = {"PATH": os.pathsep.join([str(home), str(home / "pywin32_system32"), r"C:\Windows\system32", str(tmp_path / "别处")]),
           "_PYI_APPLICATION_HOME_DIR": str(home), "_PYI_ARCHIVE_FILE": "x", "_MEIPASS2": "y", "NAPCAT_X": "1", "Path_like": "keep"}
    out = winproc.child_env(env, home=str(home))
    assert out["PATH"].split(os.pathsep) == [r"C:\Windows\system32", str(tmp_path / "别处")]
    assert "_PYI_APPLICATION_HOME_DIR" not in out and "_PYI_ARCHIVE_FILE" not in out and "_MEIPASS2" not in out
    assert out["NAPCAT_X"] == "1" and out["Path_like"] == "keep"
    assert env["PATH"].startswith(str(home))  # 不改传进来的
    assert winproc.child_env({"PATH": r"C:\a;C:\b"}, home="")["PATH"] == r"C:\a;C:\b"  # 不是打包版：原样


def test_every_external_launch_of_napcat_goes_through_the_clean_launcher():
    src = (ROOT / "mb/channels/napcat.py").read_text(encoding="utf-8")
    body = src.split("def spawn_detached")[1].split("def redact")[0]
    assert "winproc.private_dll_directory()" in body and "winproc.child_env(env)" in body
    # 本程序里拉起外部程序的地方只有这一处会长期留着子进程（别的是 taskkill 这类一闪而过的，和本程序自己的面板窗口）
    assert src.count("subprocess.Popen(") == 2 and "create_subprocess_exec" not in src


# ---------- 安装脚本的保险：占着程序目录文件的 NapCat 进程也停掉 ----------
def _py_stub(dst_dir: Path, name: str, args: list) -> subprocess.Popen:
    """用本机 Python 的副本（放在 dst_dir 里，改名成 name）起一个会睡一阵的进程，args 原样当命令行参数。"""
    base = Path(sys.base_prefix)
    dst_dir.mkdir(parents=True, exist_ok=True)
    if not (dst_dir / name).exists():
        shutil.copy(base / "python.exe", dst_dir / name)
        for d in base.glob("*.dll"):
            shutil.copy(d, dst_dir / d.name)
    return subprocess.Popen([str(dst_dir / name), *args], env=dict(os.environ, PYTHONHOME=str(base)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


@pytest.mark.skipif(not shutil.which("powershell"), reason="没有 powershell")
def test_install_script_also_stops_napcat_processes_that_hold_files_in_the_program_directory(tmp_path, monkeypatch):
    app = tmp_path / "fake app"
    internal = app / "_internal"
    internal.mkdir(parents=True)
    shutil.copy(r"C:\Windows\System32\version.dll", internal / "pmfake.dll")
    nap = app / "data" / "napcat" / "NapCat.Shell"
    shutil.copy(r"C:\Windows\System32\ping.exe", app / "PodMates.exe")  # PodMates 自己
    sleep = "import time; time.sleep(300)"
    hold = f"import ctypes, time; ctypes.WinDLL(r'{internal / 'pmfake.dll'}'); time.sleep(300)"  # 从程序目录里加载了一个 DLL
    procs = {
        "podmates": subprocess.Popen([str(app / "PodMates.exe"), "-n", "300", "127.0.0.1"], stdout=subprocess.DEVNULL),
        "qq_hold": _py_stub(nap / "versions", "QQ.exe", ["-c", hold, "--enable-logging", "-q", "111111"]),  # 号 111111 的 QQ，占着程序目录的文件
        "launcher_same": _py_stub(nap, "NapCatWinBootMain.exe", ["-c", sleep, "111111"]),  # 同一个号的启动器，自己没占
        "launcher_other": _py_stub(nap, "NapCatWinBootMain.exe", ["-c", sleep, "222222"]),  # 别的号：干净的，要保留
        "qq_other": _py_stub(nap / "versions", "QQ.exe", ["-c", sleep, "--enable-logging", "-q", "222222"]),
    }
    try:
        assert wait_until(lambda: any(str(internal).lower() in m.path.lower() for m in psutil.Process(procs["qq_hold"].pid).memory_maps()), 30)
        script = ROOT / "installer" / "stop-podmates.ps1"
        notes = app / "data" / "install-notes.txt"
        base = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Roots", str(app), "-PidFiles", str(tmp_path / "none.json"),
                "-KeepNapCat", "-ProtectDirs", str(app / "data"), "-AppDir", str(app), "-NoteFiles", str(notes)]
        dry = subprocess.run([*base, "-DryRun"], capture_output=True, text=True, encoding="gbk", errors="replace")  # 控制台是 GBK
        assert dry.returncode == 0 and all(p.poll() is None for p in procs.values()) and not notes.exists()  # 只看不动，也不留说明
        assert "因占用程序目录文件而停掉" in dry.stdout
        r = subprocess.run(base, capture_output=True, text=True, encoding="gbk", errors="replace")
        assert r.returncode == 0, r.stdout
        assert wait_until(lambda: procs["qq_hold"].poll() is not None and procs["launcher_same"].poll() is not None and procs["podmates"].poll() is not None, 15)
        assert procs["launcher_other"].poll() is None and procs["qq_other"].poll() is None  # 别的号干净的，照旧保留
        # 说明留在数据目录里，PodMates 下次启动读出来记进日志
        text = notes.read_text(encoding="utf-8-sig")
        assert "因占用程序目录文件而停掉" in text and "pmfake.dll" in text and "QQ.exe" in text and "NapCatWinBootMain.exe" in text and "需要重新登录" in text
        monkeypatch.setenv("PODMATES_DATA", str(app / "data"))
        got = napcat.take_install_notes()
        assert len(got) == 2 and all("因占用程序目录文件而停掉" in ln for ln in got) and not notes.exists()  # 111111 的 QQ 和它的启动器各一行；读完就删
        assert napcat.take_install_notes() == []
        # 再跑一遍：没有谁占着了，什么都不停、也不留说明
        r = subprocess.run(base, capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0 and procs["launcher_other"].poll() is None and procs["qq_other"].poll() is None and not notes.exists()
    finally:
        for p in procs.values():
            kill_tree(p.pid)


def test_start_logs_what_the_installer_left_in_the_notes_file(env):
    async def body(e):
        (Path(os.environ["PODMATES_DATA"]) / napcat.NOTES_FILE).write_text("2026-10-26 00:00:00 因占用程序目录文件而停掉 QQ.exe（pid 1）：x.dll。\n", encoding="utf-8")
        await e.engine.start_all()
        logs = [r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='napcat'")]
        assert any("安装程序：" in t and "因占用程序目录文件而停掉" in t for t in logs)
        assert not (Path(os.environ["PODMATES_DATA"]) / napcat.NOTES_FILE).exists()
    env.run(body)


def test_installer_passes_the_program_directory_and_a_note_file_in_install_mode_only():
    iss = (ROOT / "installer/PodMates.iss").read_text(encoding="utf-8-sig")
    keep = iss.split("if KeepNapCat then\n    Args := Args")[1].split("if FileExists(Script)")[0]
    assert "-KeepNapCat -ProtectDirs" in keep and "-AppDir" in keep and "-NoteFiles" in keep and "install-notes.txt" in keep
    ps1 = (ROOT / "installer/stop-podmates.ps1").read_text(encoding="utf-8-sig")
    assert "因占用程序目录文件而停掉" in ps1 and "$KeepNapCat -and $AppDir" in ps1
