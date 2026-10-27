"""NapCat 保活的测试公共件：桩 NapCat 的启动命令、认桩进程、假的渠道和引擎。测试进程和模拟「PodMates」的 napcat_holder.py 共用。"""
import os
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUB = str(HERE / "napcat_stub.py")


def install(napcat, ctl, mp=None):
    """把 NapCat 的启动命令换成桩，并让 owned() 认得桩（真的认法见 test_round27：按启动器的名字 / QQ 的命令行）。mp 是 monkeypatch，没有就直接改。"""
    def is_ours(name, exe, cmdline, root, uin):
        args = [str(a) for a in cmdline or []]
        return any(a.endswith("napcat_stub.py") for a in args) and str(uin) in args[2:3]

    def launch_plan(self):
        return [sys.executable, STUB, self.uin, str(self.port), self.token, str(ctl)], dict(os.environ)

    for obj, name, val in ((napcat, "is_ours", is_ours), (napcat.NapCatProcess, "launch_plan", launch_plan)):
        mp.setattr(obj, name, val) if mp else setattr(obj, name, val)


def fake_root(parent: Path, name="NapCat.Shell") -> Path:
    """一个认得出来的 NapCat 目录（shell 方式）：有启动器和 napcat.mjs 就行，桩不用它们。"""
    root = parent / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "NapCatWinBootMain.exe").write_bytes(b"")
    (root / "napcat.mjs").write_text("")
    return root


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeEngine:
    def __init__(self):
        self.logs: list[tuple] = []
        self.notices: list[str] = []

    def log(self, level, cat, text):
        self.logs.append((level, cat, text))

    def notify(self, text, account=None):
        self.notices.append(text)

    def texts(self, cat="napcat"):
        return [t for _, c, t in self.logs if c == cat]


class FakeChannel:
    """NapCatProcess 要用到渠道的那几样：引擎、账号、地址、二维码回调、状态。"""

    def __init__(self, port, uin="123456789"):
        self.engine = FakeEngine()
        self.account = {"id": "ac_stub", "qq": uin}
        self.stopping = False
        self.url = f"ws://127.0.0.1:{port}"
        self.qrs: list[str] = []
        self.statuses: list[tuple] = []

    def on_qr(self, url):
        self.qrs.append(url)

    def set_status(self, status, msg=""):
        self.statuses.append((status, msg))
