"""微信窗口「移到屏幕外」：运行期间把微信主窗口移到所有显示器合起来的可视范围之外（不是最小化，最小化后界面自动化读不到内容）。

- 需要点界面（切换会话、发送、读身份）时，短暂移回原位置，做完立刻再移出去；
- 停止机器人、退出程序时恢复原位置和大小；
- 原位置记在数据目录的 wechat_window.json 里：程序被强行结束、窗口留在屏幕外时，下次启动自动恢复，
  面板上的「找回微信窗口」按钮也能用（不需要微信自动化组件，只按窗口类名找到微信主窗口、把它放回主显示器）；
- 多显示器、高分屏：坐标全部用「每显示器 DPI 感知」的物理像素，显示器矩形取自系统的显示器列表；
  目标位置选在所有显示器外接矩形的外面，并且和每一个显示器都不相交（右、左、下、上依次试，保证在 ±32000 的窗口坐标范围内）。

窗口操作都走 WinApi 这个小接口：真实现用 ctypes 调 Win32，测试里换成模拟的，不碰任何真窗口。
"""
import atexit
import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path

WECHAT_CLASS = "mmui::MainWindow"  # 微信 4.0 主窗口的类名
WECHAT_TITLES = ("微信", "Weixin", "WeChat")  # 微信 4.1.x：类名 Qt51514QWindowIcon，标题是这几个之一
LIMIT = 30000  # Win32 窗口坐标的安全范围（实际是 ±32767）
GAP = 300  # 离最近的显示器边缘至少隔这么远
MIN_VISIBLE = 120  # 原位置至少有这么大一块在某个显示器里，才算「看得见」
Rect = tuple[int, int, int, int]  # left, top, right, bottom


# ---------- 纯几何 ----------
def intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def union(mons: list[Rect]) -> Rect:
    return (min(m[0] for m in mons), min(m[1] for m in mons), max(m[2] for m in mons), max(m[3] for m in mons))


def offscreen_pos(mons: list[Rect], w: int, h: int) -> tuple[int, int]:
    """窗口放哪里才完全看不见：先试所有显示器外接矩形的右边、左边、下边、上边，要求和每个显示器都不相交，且在坐标安全范围内。"""
    l, t, r, b = union(mons)
    for x, y in ((r + GAP, t), (l - GAP - w, t), (l, b + GAP), (l, t - GAP - h)):
        if abs(x) <= LIMIT and abs(y) <= LIMIT and abs(x + w) <= LIMIT and abs(y + h) <= LIMIT \
                and not any(intersects((x, y, x + w, y + h), m) for m in mons):
            return x, y
    raise RuntimeError("找不到能把窗口完全移出所有显示器的位置")


def visible_enough(rect: Rect, mons: list[Rect]) -> bool:
    for m in mons:
        w = min(rect[2], m[2]) - max(rect[0], m[0])
        h = min(rect[3], m[3]) - max(rect[1], m[1])
        if w >= MIN_VISIBLE and h >= MIN_VISIBLE:
            return True
    return False


def clamp_visible(rect: Rect, mons: list[Rect], work: Rect) -> Rect:
    """原位置还在某个显示器里就原样用；那块显示器没了（拔掉了、换了分辨率）就放到主显示器工作区左上角，大小不超过工作区。"""
    if visible_enough(rect, mons):
        return rect
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    w, h = min(w, work[2] - work[0] - 80), min(h, work[3] - work[1] - 80)
    x, y = work[0] + 40, work[1] + 40
    return (x, y, x + w, y + h)


# ---------- Win32 ----------
class WinApi:
    """真实的窗口接口（只在 Windows 上用）。线程里用之前先 begin()，让这个线程用每显示器 DPI 感知的物理像素。"""

    def __init__(self):
        self.u = ctypes.windll.user32
        # 要声明原型的函数用自己的一份 kernel32：ctypes.windll.kernel32 是全进程共用的，在上面改 argtypes 会改坏别人
        self.k = ctypes.WinDLL("kernel32")
        self.k.OpenProcess.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        # 要声明参数类型的函数用自己的一份 user32：ctypes.windll.user32 是全进程共用的，在上面改 argtypes 会改坏别人
        # （wxauto4 / uiautomation 用 c_long 调 GetAncestor，2026-10-27 真机上因此连不上微信）
        own = ctypes.WinDLL("user32")
        self._from_point = own.WindowFromPoint
        self._from_point.restype = wintypes.HWND
        self._from_point.argtypes = [wintypes.POINT]
        self._ancestor = own.GetAncestor
        self._ancestor.restype = wintypes.HWND
        self._ancestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        # 下面这几个也只在自己这份 user32 上声明：EnumWindows 的回调类型、PostMessage、可见性、类名、标题
        self._proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._enum = own.EnumWindows
        self._enum.restype = wintypes.BOOL
        self._enum.argtypes = [self._proc, wintypes.LPARAM]
        self._post = own.PostMessageW
        self._post.restype = wintypes.BOOL
        self._post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._is_visible = own.IsWindowVisible
        self._is_visible.restype = wintypes.BOOL
        self._is_visible.argtypes = [wintypes.HWND]
        self._class_name = own.GetClassNameW
        self._class_name.restype = ctypes.c_int
        self._class_name.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._title = own.GetWindowTextW
        self._title.restype = ctypes.c_int
        self._title.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def begin(self):
        try:
            self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            pass

    def monitors(self) -> list[Rect]:
        out = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(hmon, hdc, lprc, lparam):
            r = lprc.contents
            out.append((r.left, r.top, r.right, r.bottom))
            return 1
        self.u.EnumDisplayMonitors(None, None, proto(cb), 0)
        return out

    def work_area(self) -> Rect:
        r = wintypes.RECT()
        self.u.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA（主显示器）
        return (r.left, r.top, r.right, r.bottom)

    def rect(self, hwnd) -> Rect:
        r = wintypes.RECT()
        self.u.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)

    def exists(self, hwnd) -> bool:
        return bool(hwnd) and bool(self.u.IsWindow(hwnd))

    def is_zoomed(self, hwnd) -> bool:
        return bool(self.u.IsZoomed(hwnd))

    def is_iconic(self, hwnd) -> bool:
        return bool(self.u.IsIconic(hwnd))

    def show(self, hwnd, how: str):
        self.u.ShowWindow(hwnd, {"restore": 9, "maximize": 3}[how])

    def show_quietly(self, hwnd):
        """把窗口显示出来并还原（被藏进托盘、最小化的都管），不激活、不抢前台焦点、不挪位置。"""
        self.u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：按最近的大小和位置显示 / 还原，但不激活

    def is_visible(self, hwnd) -> bool:
        return bool(hwnd) and bool(self._is_visible(hwnd))

    def move(self, hwnd, x: int, y: int, w: int, h: int):
        # SWP_NOZORDER | SWP_NOACTIVATE：挪位置不抢焦点
        self.u.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)

    def foreground(self):
        return self.u.GetForegroundWindow()

    def set_foreground(self, hwnd):
        """把前台还给 hwnd。Windows 不让后台进程随便抢前台，借当前前台窗口所在线程的输入队列（AttachThreadInput）绕过去。"""
        cur = self.u.GetForegroundWindow()
        t_fg = self.u.GetWindowThreadProcessId(cur, None)
        t_me = self.k.GetCurrentThreadId()
        self.u.AttachThreadInput(t_me, t_fg, True)
        try:
            self.u.SetForegroundWindow(hwnd)
        finally:
            self.u.AttachThreadInput(t_me, t_fg, False)

    def zprev(self, hwnd):
        """Z 序里排在这个窗口前面（盖在它上面）的那个窗口；0 表示它在最上面。"""
        return self.u.GetWindow(hwnd, 3) or 0  # GW_HWNDPREV

    def set_zafter(self, hwnd, after):
        """把窗口放回 after 的正后面（不挪位置、不改大小、不激活）。"""
        self.u.SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE | NOMOVE | NOACTIVATE

    def is_wechat(self, hwnd) -> bool:
        return bool(hwnd) and self._exe(hwnd).lower().endswith(("weixin.exe", "wechat.exe"))

    def root_of(self, hwnd) -> int:
        """hwnd 所在的那个顶层窗口（GA_ROOT）；读不到返回 0。消息控件往上取顶层窗口就用这个。"""
        if not hwnd:
            return 0
        return int(self._ancestor(wintypes.HWND(hwnd), 2) or 0)

    def class_of(self, hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self._class_name(hwnd, buf, 256)
        return buf.value

    def title_of(self, hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self._title(hwnd, buf, 256)
        return buf.value

    def post_key(self, hwnd, vk: int):
        """只给这个窗口发一次按键：PostMessageW WM_KEYDOWN + WM_KEYUP（不进别的窗口的消息队列）。"""
        self._post(hwnd, 0x0100, vk, 0)  # WM_KEYDOWN
        self._post(hwnd, 0x0101, vk, 0xC0000001)  # WM_KEYUP（松开，扫描码 0、重复 1）

    def post_close(self, hwnd):
        """只给这个窗口发 WM_CLOSE。"""
        self._post(hwnd, 0x0010, 0, 0)

    def windows(self) -> list[dict]:
        """微信进程的所有顶层窗口（看得见、看不见的都列）：句柄、类名、标题、矩形、可见。
        菜单这类小窗也在这里；进程按窗口的 exe 判断（同一个 pid 只查一次），不是微信的一概不算。"""
        exe: dict[int, bool] = {}
        out: list[dict] = []

        def cb(hwnd, lparam):
            pid = wintypes.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            ok = exe.get(pid.value)
            if ok is None:
                ok = exe[pid.value] = self._exe_pid(pid.value).lower().endswith(("weixin.exe", "wechat.exe"))
            if ok:
                out.append({"hwnd": int(hwnd), "class": self.class_of(hwnd), "title": self.title_of(hwnd),
                            "rect": self.rect(hwnd), "visible": self.is_visible(hwnd)})
            return 1
        self._enum(self._proc(cb), 0)
        return out

    def top_at(self, x: int, y: int):
        """屏幕上这一点最上面的那个顶层窗口（WindowFromPoint 之后取它的顶层祖先；点到看不见的窗口也算它）。
        WindowFromPoint 只找看得见的窗口，被别的窗口盖住的地方返回的是盖住它的那个窗口。"""
        h = self._from_point(wintypes.POINT(int(x), int(y)))
        if not h:
            return 0
        top = self._ancestor(h, 2)  # GA_ROOT
        return top or h

    def find_main(self, visible_only: bool = True):
        """找微信主窗口：属于 Weixin.exe、够大；窗口类名是 Qt 的（微信 4.1.x 是 Qt51514QWindowIcon，标题「微信」/Weixin），
        老一点的 4.0 是 mmui::MainWindow。几个都符合时取面积最大的。
        visible_only=False 时把藏进托盘（看不见）的窗口也找出来（只给「主窗口被藏起来了」的恢复用；原有调用方照旧只找看得见的）。"""
        found = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, lparam):
            if visible_only and not self.u.IsWindowVisible(hwnd):
                return 1
            cls, title = ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(128)
            self.u.GetClassNameW(hwnd, cls, 128)
            self.u.GetWindowTextW(hwnd, title, 128)
            if not (cls.value == WECHAT_CLASS or (cls.value.startswith("Qt5") and "QWindowIcon" in cls.value and title.value in WECHAT_TITLES)):
                return 1
            if not self._exe(hwnd).lower().endswith(("weixin.exe", "wechat.exe")):
                return 1
            r = self.rect(hwnd)
            if (r[2] - r[0]) * (r[3] - r[1]) >= 300 * 300:
                found.append((hwnd, (r[2] - r[0]) * (r[3] - r[1])))
            return 1
        self.u.EnumWindows(proto(cb), 0)
        return max(found, key=lambda x: x[1])[0] if found else None

    def _exe(self, hwnd) -> str:
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return self._exe_pid(pid.value)

    def _exe_pid(self, pid: int) -> str:
        h = self.k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            return buf.value if self.k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
        finally:
            self.k.CloseHandle(h)


# ---------- 保管窗口位置 ----------
class Keeper:
    """管一个微信主窗口：hide() 移出去，bring() 临时移回，restore() 恢复原位并清掉记录。线程安全（微信界面操作本来就排在一个线程里，这里再加一把锁）。"""

    def __init__(self, api, state_path: Path, hwnd=None, log=None):
        self.api, self.state_path, self.log = api, Path(state_path), log or (lambda *a: None)
        self.hwnd = hwnd
        self.orig: Rect | None = None
        self.zoomed = False
        self.off = False
        self.lock = threading.RLock()
        self.moves = 0  # 移出/移回的总次数（测试和报告用）
        self.restores = 0  # 操作把微信顶到前台、又被还回去的次数
        atexit.register(self.restore)

    def _find(self):
        if not self.api.exists(self.hwnd):
            self.hwnd = self.api.find_main()
        return self.hwnd

    def _save(self):
        try:
            self.state_path.write_text(json.dumps({"hwnd": self.hwnd, "orig": self.orig, "zoomed": self.zoomed, "off": self.off}), encoding="utf-8")
        except OSError:
            pass

    def snapshot(self) -> dict | None:
        """做会抢前台的操作之前：记下现在的前台窗口，和微信窗口在 Z 序里排在谁后面。"""
        with self.lock:
            h = self._find()
            if not h:
                return None
            return {"fg": self.api.foreground(), "prev": self.api.zprev(h)}

    def settle(self, snap: dict | None) -> bool:
        """操作做完：如果微信被顶到了前台（wxauto4 切会话、读消息都会把窗口激活），把前台还给原来的窗口，
        并把微信窗口放回原来的 Z 序。用户本来就在用微信（它原来就是前台）、或者操作期间用户自己切到了别的窗口，都不动。"""
        if not snap:
            return False
        with self.lock:
            h = self.hwnd
            if not h or not self.api.exists(h):
                return False
            acted = False
            now, fg0 = self.api.foreground(), snap["fg"]
            if fg0 and now != fg0 and self.api.is_wechat(now) and not self.api.is_wechat(fg0):
                self.api.set_foreground(fg0)
                acted = True
            if not self.off and snap["prev"] and self.api.zprev(h) != snap["prev"] and self.api.exists(snap["prev"]):
                self.api.set_zafter(h, snap["prev"])
                acted = True
            self.restores += acted
            return acted

    def hide(self) -> bool:
        """移到所有显示器之外。已经在外面就什么都不做。最小化着的不动（最小化后读不到界面，也没法移）。"""
        with self.lock:
            if self.off:
                return True
            hwnd = self._find()
            if not hwnd:
                return False
            if self.api.is_iconic(hwnd):
                self.log("warn", "微信窗口被最小化了，没法移到屏幕外（最小化后读不到界面，请把它还原）")
                return False
            self.zoomed = self.api.is_zoomed(hwnd)
            if self.zoomed:
                self.api.show(hwnd, "restore")
            self.orig = self.api.rect(hwnd)
            w, h = self.orig[2] - self.orig[0], self.orig[3] - self.orig[1]
            x, y = offscreen_pos(self.api.monitors(), w, h)
            self.off = True
            self._save()  # 先记下再移：移的时候程序被结束也找得回来
            self.api.move(hwnd, x, y, w, h)
            self.moves += 1
            return True

    def bring(self) -> bool:
        """临时移回原位置（原位置的显示器没了就放到主显示器）。做完要自己再 hide()。"""
        with self.lock:
            if not self.off:
                return True
            hwnd = self._find()
            if not hwnd or not self.orig:
                return False
            x1, y1, x2, y2 = clamp_visible(self.orig, self.api.monitors(), self.api.work_area())
            self.api.move(hwnd, x1, y1, x2 - x1, y2 - y1)
            self.off = False
            self.moves += 1
            return True

    def restore(self) -> bool:
        """恢复原位置和大小（停止机器人、退出程序时）。"""
        with self.lock:
            if self.orig is None and not self.off:
                return True
            hwnd = self._find()
            if hwnd and self.orig:
                x1, y1, x2, y2 = clamp_visible(self.orig, self.api.monitors(), self.api.work_area())
                self.api.move(hwnd, x1, y1, x2 - x1, y2 - y1)
                if self.zoomed:
                    self.api.show(hwnd, "maximize")
                self.moves += 1
            self.off, self.orig, self.zoomed = False, None, False
            try:
                self.state_path.unlink()
            except OSError:
                pass
            return True


def recover(api, state_path: Path) -> dict:
    """找回微信窗口：不依赖微信自动化组件。按类名找到微信主窗口，不在任何显示器里（或记录说还留在屏幕外）就放回原位置，
    没有记录就放到主显示器工作区里。窗口本来就看得见时什么都不动。"""
    state_path = Path(state_path)
    st = {}
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    api.begin()
    hwnd = st.get("hwnd") if api.exists(st.get("hwnd")) else api.find_main()
    if not hwnd:
        return {"found": False, "moved": False}
    mons, work = api.monitors(), api.work_area()
    cur = api.rect(hwnd)
    if visible_enough(cur, mons) and not st.get("off"):
        return {"found": True, "moved": False, "rect": cur}
    orig = tuple(st["orig"]) if st.get("orig") else None
    w, h = (orig[2] - orig[0], orig[3] - orig[1]) if orig else (min(1100, work[2] - work[0] - 80), min(760, work[3] - work[1] - 80))
    target = clamp_visible(orig or (work[0] + 40, work[1] + 40, work[0] + 40 + w, work[1] + 40 + h), mons, work)
    api.move(hwnd, target[0], target[1], target[2] - target[0], target[3] - target[1])
    if st.get("zoomed"):
        api.show(hwnd, "maximize")
    try:
        state_path.unlink()
    except OSError:
        pass
    return {"found": True, "moved": True, "rect": target}


def unhide(api) -> bool:
    """微信主窗口被藏进托盘（IsWindowVisible == False）或最小化时，把它显示出来并还原：不激活、不抢前台焦点。
    窗口本来就看得见时什么都不做。找不到主窗口（微信退出了）也什么都不做。返回这一步做没做事。"""
    hwnd = api.find_main(visible_only=False)  # 藏起来的窗口只有不看可见性才找得到
    if not hwnd:
        return False
    if api.is_visible(hwnd) and not api.is_iconic(hwnd):
        return False
    api.show_quietly(hwnd)
    return True
