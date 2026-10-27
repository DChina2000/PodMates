"""桌面程序入口：后台线程跑后端（面板服务 + 机器人），主线程跑托盘；面板窗口是单独的进程（WebView2），
关掉窗口就整个释放，机器人照常在托盘里跑。再次双击桌面图标只会把已开的面板调到前面。"""
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from . import APP_NAME, DISPLAY_NAME, WINDOW_TITLE
from .paths import ASSETS, data_dir, exe_command
from . import tls


def _post(url: str, headers: dict | None = None, timeout: float = 2) -> bool:
    try:
        req = urllib.request.Request(url, data=b"{}", headers=headers or {}, method="POST")
        tls.urlopen(req, timeout=timeout).read()
        return True
    except Exception:
        return False


def focus_window(pid: int) -> bool:
    """按进程找面板窗口（同名的还有任务栏缩略图之类的系统窗口，不能只按标题找）。"""
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        p = wt.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u.IsWindowVisible(h) and u.GetWindowTextLengthW(h):
            found.append(h)
        return True
    u.EnumWindows(cb, 0)
    if not found:
        return False
    h = found[0]
    u.ShowWindow(h, 9)  # 最小化了就还原
    u.SetForegroundWindow(h)
    return True


class Desktop:
    def __init__(self, background: bool):
        self.background = background
        self.data = data_dir()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.window: subprocess.Popen | None = None
        self.icon = None
        self.core = self.server = None
        self.port = 0

    # ---------- 后端 ----------
    def backend(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._boot())
        self.ready.set()
        self.loop.run_forever()

    async def _boot(self):
        from . import system
        from .core import Core
        from .server import Server
        self.core = Core(self.data)
        self.server = Server(self.core)
        self.server.on_show = lambda: threading.Thread(target=self.show, daemon=True).start()
        s = self.core.settings
        host = "0.0.0.0" if s.g("panel_lan") and self.core.db.secret("panel_password") else "127.0.0.1"
        port = s.g("panel_port")
        for p in range(port, port + 20):
            try:
                await self.server.start(host, p)
                self.port = p
                break
            except OSError:
                continue
        (self.data / "runtime.json").write_text(json.dumps({"pid": os.getpid(), "port": self.port,
                                                            "token": self.server.launch_token}), encoding="utf-8")
        if bool(s.g("autostart")) != system.get_autostart() or s.g("autostart"):
            try:
                system.set_autostart(bool(s.g("autostart")))  # 重装后路径可能变了，按设置重写一次
            except OSError:
                pass
        self.core.engine.notify_hook = self.toast
        asyncio.create_task(self.core.periodic())
        if not self.core.needs_setup():
            await self.core.start()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    # ---------- 窗口 ----------
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.server.launch_token}"

    def show(self):
        if self.window and self.window.poll() is None and focus_window(self.window.pid):
            return
        self.window = subprocess.Popen(exe_command("--window", self.url()))

    def toast(self, text: str):
        if self.icon:
            self.icon.notify(text[:200], DISPLAY_NAME)

    # ---------- 托盘 ----------
    def exit_app(self, icon):
        """托盘点「退出」：用户明确要关，所以连 NapCat / QQ 一起停（core.stop → 各账号 stop）。PodMates 崩溃、被强行结束、装新版时走不到这里，NapCat 照样挂着。"""
        try:
            self.call(self.core.stop())
            self.call(self.server.stop())
        except Exception:
            pass
        if self.window and self.window.poll() is None:
            self.window.terminate()
        (self.data / "runtime.json").unlink(missing_ok=True)
        icon.stop()

    def run(self):
        import pystray
        from PIL import Image
        threading.Thread(target=self.backend, daemon=True).start()
        self.ready.wait(60)
        if not self.background or self.core.needs_setup():
            self.show()

        def toggle(icon, item):
            if self.core.running:
                self.call(self.core.stop())
            else:
                self.call(self.core.start())

        def quit_(icon, item):
            self.exit_app(icon)

        menu = pystray.Menu(
            pystray.MenuItem("打开面板", lambda i, it: self.show(), default=True),
            pystray.MenuItem(lambda it: "停止机器人" if self.core and self.core.running else "启动机器人", toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", quit_),
        )
        self.icon = pystray.Icon(APP_NAME, Image.open(ASSETS / "icon.ico"), DISPLAY_NAME, menu)
        self.icon.run()


def show_running(show: bool) -> bool:
    """让已经在运行的那个 PodMates 把面板调到前面（show 为假时只确认它在）。靠 runtime.json 找到它的面板端口；
    它还没把面板起来（刚好在启动）、或者 runtime.json 是上一个崩溃的留下的，就返回 False。这里只用来「调窗口」，不再用来判断是不是已经有一个在运行。"""
    rt = data_dir() / "runtime.json"
    if not rt.exists():
        return False
    try:
        info = json.loads(rt.read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{info['port']}"
        tls.urlopen(base + "/api/state", timeout=2).read()
    except Exception:
        return False
    if show:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # 让已在运行的那个程序能把窗口调到最前
        _post(base + "/api/_show", {"X-Launch": info["token"], "Content-Type": "application/json"})
    return True


def hand_over_to_running(show: bool, wait: float = 8.0, step: float = 0.4) -> bool:
    """已经有一个在运行了：把它的窗口调到前面然后退出。前一个刚好在启动、面板还没起来时，多等它几秒（重试）；等不到也只是退出，绝不另起一套。"""
    end = time.time() + wait
    while True:
        if show_running(show):
            return True
        if time.time() >= end:
            return False
        time.sleep(step)


def main(argv: list[str]):
    if "--window" in argv:
        from .window import run
        run(argv[argv.index("--window") + 1])
        return
    if "--probe-wechat-emoji" in argv:  # 微信表情面板探查（只看不发）：不取单实例互斥体，不启动引擎，不碰 NapCat
        i = argv.index("--probe-wechat-emoji")
        if len(argv) <= i + 1 or argv[i + 1].startswith("--"):
            print("用法：PodMates.exe --probe-wechat-emoji <输出目录>", flush=True)
            sys.exit(2)
        from .wxprobe import main as probe
        sys.exit(probe(argv[i + 1]))
    if "--test-wechat-sticker" in argv:  # 微信自定义表情验证（只在「文件传输助手」里操作）：不取单实例互斥体，不启动引擎，不碰 NapCat
        i = argv.index("--test-wechat-sticker")
        if len(argv) <= i + 1 or argv[i + 1].startswith("--"):
            print("用法：PodMates.exe --test-wechat-sticker <输出目录>", flush=True)
            sys.exit(2)
        from .wxsticker import main as sticker_test
        sys.exit(sticker_test(argv[i + 1]))
    if "--test-wechat-picture" in argv:  # 微信收图验证（只在「文件传输助手」里操作）：不取单实例互斥体，不启动引擎，不碰 NapCat
        i = argv.index("--test-wechat-picture")
        if len(argv) <= i + 1 or argv[i + 1].startswith("--"):
            print("用法：PodMates.exe --test-wechat-picture <输出目录>", flush=True)
            sys.exit(2)
        from .wxpicture import main as picture_test
        sys.exit(picture_test(argv[i + 1]))
    background = "--background" in argv
    from . import singleton
    if not singleton.acquire(data_dir()):  # 最早一步：同一份数据目录只留一个主程序（两个几乎同时启动也只有一个取得）
        if not background:  # 开机自启 / 定时任务拉起（--background）不调窗口，也就不用等
            hand_over_to_running(show=True)
        return
    (data_dir() / "runtime.json").unlink(missing_ok=True)  # 取到了就是唯一的主程序：前一个崩溃留下的 runtime.json 作废，免得别人按它找到不相干的端口
    Desktop(background).run()
