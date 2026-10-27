"""单实例：同一份数据目录只能有一个主程序在运行（系统级互斥体，跟数据目录绑定）。用真的进程测：走真的 mb.app.main，Desktop 换成桩（tests/singleton_probe.py）。"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mb import singleton

psutil = pytest.importorskip("psutil")
PROBE = Path(__file__).resolve().parent / "singleton_probe.py"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="命名互斥体只在 Windows 上")


def wait_until(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class Probes:
    """起几个模拟的主程序，记下它们的 pid，结束时只按记下的 pid 清掉。"""

    def __init__(self, tmp_path):
        self.tmp, self.procs = tmp_path, []

    def start(self, data: Path, mark: Path, start_at: float, hold: float, *args) -> subprocess.Popen:
        data.mkdir(parents=True, exist_ok=True)
        mark.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([sys.executable, str(PROBE), str(mark), str(start_at), str(hold), *args], env=dict(os.environ, PODMATES_DATA=str(data)),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def kill_all(self):
        for p in self.procs:
            try:
                parent = psutil.Process(p.pid)
                for c in parent.children(recursive=True):
                    c.kill()
                parent.kill()
            except psutil.Error:
                pass


@pytest.fixture
def probes(tmp_path):
    pr = Probes(tmp_path)
    yield pr
    pr.kill_all()


def names(mark: Path, kind: str):
    return sorted(p.name for p in mark.glob(f"{kind}-*"))


def test_two_programs_started_in_the_same_instant_leave_exactly_one_running_every_time(probes, tmp_path):
    for i in range(12):  # 重复多次，每次一份全新的数据目录
        data, mark = tmp_path / f"d{i}", tmp_path / f"m{i}"
        t0 = time.time() + 1.5  # 约好同一时刻进 main
        a = probes.start(data, mark, t0, 3, "--background")
        b = probes.start(data, mark, t0, 3, "--background")
        a.wait(30)  # 等两个进程都结束、标记文件写全以后再数（不在中途数：一个已写出 exit，另一个可能还没写出 ran）
        b.wait(30)
        assert wait_until(lambda: len(names(mark, "exit")) == 2, 30), i
        assert len(names(mark, "ran")) == 1 and len(names(mark, "exit")) == 2, i  # 另一个很快就退出了，没起任何东西
    # 三个同时来也一样
    data, mark = tmp_path / "three", tmp_path / "m_three"
    t0 = time.time() + 1.5
    ps = [probes.start(data, mark, t0, 3, "--background") for _ in range(3)]
    for p in ps:
        p.wait(30)
    assert len(names(mark, "ran")) == 1 and len(names(mark, "exit")) == 3


def test_the_second_one_brings_the_first_ones_window_to_the_front_unless_it_is_a_background_start(probes, tmp_path):
    data, mark = tmp_path / "d", tmp_path / "m"
    first = probes.start(data, mark, 0, 10)
    assert wait_until(lambda: len(names(mark, "ran")) == 1)
    # 双击图标式的再次启动：调出第一个的面板，然后退出，不另起
    second = probes.start(data, mark, 0, 10)
    assert second.wait(30) == 0
    ran = names(mark, "ran")  # 标记里记的是真正跑代码的那个 pid（venv 的 python.exe 只是个转发的壳，Popen 拿到的是壳的 pid）
    assert len(ran) == 1 and names(mark, "shown") == ["shown-" + ran[0].split("-")[1]]
    # --background（开机自启、定时任务拉起）：什么都不调，立刻退出
    t = time.time()
    third = probes.start(data, mark, 0, 10, "--background")
    assert third.wait(30) == 0
    assert len(list(mark.glob("shown-*"))) == 1 and names(mark, "ran") == ran


def test_a_start_that_comes_while_the_first_is_still_starting_waits_for_it_then_gives_up_without_starting_anything(probes, tmp_path, monkeypatch):
    from mb import app
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("PODMATES_DATA", str(data))
    shown = []
    calls = {"n": 0}

    def fake_show(show):
        calls["n"] += 1
        if calls["n"] == 3:  # 前两次面板还没起来，第三次起来了
            shown.append(show)
            return True
        return False
    monkeypatch.setattr(app, "show_running", fake_show)
    assert app.hand_over_to_running(True, wait=5, step=0.01) is True and shown == [True] and calls["n"] == 3
    calls["n"] = 0
    monkeypatch.setattr(app, "show_running", lambda show: calls.__setitem__("n", calls["n"] + 1) or False)
    t = time.time()
    assert app.hand_over_to_running(True, wait=0.3, step=0.05) is False and calls["n"] >= 3  # 一直等不到：重试到期就返回，由调用方退出
    assert time.time() - t < 3


def test_after_the_first_is_force_killed_the_next_start_takes_over(probes, tmp_path):
    data, mark = tmp_path / "d", tmp_path / "m"
    first = probes.start(data, mark, 0, 60, "--background")
    assert wait_until(lambda: len(names(mark, "ran")) == 1)
    (data / "runtime.json").write_text("{\"pid\": 1, \"port\": 1, \"token\": \"stale\"}")  # 模拟崩溃留下的旧文件
    psutil.Process(first.pid).kill()  # 强行结束，什么清理都来不及做
    first.wait(10)
    probes.start(data, mark, 0, 5, "--background")
    assert wait_until(lambda: len(names(mark, "ran")) == 2), names(mark, "ran")  # 互斥体已被系统释放：第二个正常起来
    # 第二个起来后，它不会被旧的 runtime.json（崩溃留下的）误导：文件已被它换成自己的
    import json
    new_pid = json.loads((data / "runtime.json").read_text())["pid"]
    assert new_pid != 1 and f"ran-{new_pid}" in names(mark, "ran") and first.poll() is not None


def test_different_data_folders_run_side_by_side(probes, tmp_path):
    t0 = time.time() + 1.5
    a = probes.start(tmp_path / "da", tmp_path / "ma", t0, 4, "--background")
    b = probes.start(tmp_path / "db", tmp_path / "mb_", t0, 4, "--background")
    assert wait_until(lambda: len(names(tmp_path / "ma", "ran")) == 1 and len(names(tmp_path / "mb_", "ran")) == 1, 30)  # 互不影响，各跑各的
    a.wait(30)
    b.wait(30)


def test_a_normal_exit_also_lets_the_next_one_start(probes, tmp_path):
    data, mark = tmp_path / "d", tmp_path / "m"
    probes.start(data, mark, 0, 0.5, "--background").wait(30)
    probes.start(data, mark, 0, 0.5, "--background").wait(30)
    assert len(names(mark, "ran")) == 2


def test_mutex_name_is_bound_to_the_data_folder(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    assert singleton.mutex_name(a) == singleton.mutex_name(a) and singleton.mutex_name(a) != singleton.mutex_name(b)
    assert singleton.mutex_name(a) == singleton.mutex_name(Path(str(a).upper())) == singleton.mutex_name(a / ".." / "A")  # 大小写、绕路写法都是同一份
    assert singleton.mutex_name(a).startswith("Global\\PodMates-") and singleton.mutex_name(a, "Local").startswith("Local\\PodMates-")
    assert str(tmp_path) not in singleton.mutex_name(a)  # 名字里只有哈希，不带路径


def test_window_mode_and_other_paths_do_not_touch_the_mutex(monkeypatch):
    from mb import app, window
    ran = []
    monkeypatch.setattr(window, "run", lambda url: ran.append(url))
    monkeypatch.setattr(singleton, "acquire", lambda d: pytest.fail("面板窗口进程不该取互斥体"))
    app.main(["--window", "http://127.0.0.1:1/?t=x"])
    assert ran == ["http://127.0.0.1:1/?t=x"]


def test_main_takes_the_mutex_before_anything_else_and_clears_a_stale_runtime_file(tmp_path, monkeypatch):
    from mb import app
    monkeypatch.setenv("PODMATES_DATA", str(tmp_path))
    (tmp_path / "runtime.json").write_text("stale")
    order = []
    monkeypatch.setattr(singleton, "acquire", lambda d: order.append("acquire") or True)

    class D:
        def __init__(self, background):
            order.append("desktop")
            assert not (tmp_path / "runtime.json").exists()

        def run(self):
            order.append("run")
    monkeypatch.setattr(app, "Desktop", D)
    app.main([])
    assert order == ["acquire", "desktop", "run"]
    # 取不到：不启动 Desktop
    monkeypatch.setattr(singleton, "acquire", lambda d: False)
    monkeypatch.setattr(app, "Desktop", lambda b: pytest.fail("已经有一个在运行，不该再起"))
    shown = []
    monkeypatch.setattr(app, "hand_over_to_running", lambda show, **k: shown.append(show))
    app.main([])
    app.main(["--background"])
    assert shown == [True]  # --background 不调窗口，也不用等
