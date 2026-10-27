"""Windows 上拉起外部程序（NapCat 等）时，不让它们继承本程序的 DLL 搜索目录。

PyInstaller 打包的程序，启动时用 SetDllDirectoryW 把 _internal 加进自己的 DLL 搜索路径。这个设置会被子进程继承（进程参数里的 DllPath；
用 PodMates 之外的程序实验过：父进程 SetDllDirectory，子进程不改 PATH 也能按名字加载那个目录里的 DLL，孙进程同样）。NapCat 的启动器和它拉起的 QQ
于是从 _internal 里加载 VCRUNTIME140.dll 等运行库，而且 NapCat 现在比 PodMates 活得久，装新版覆盖这些文件时就被它们占着（退出码 5）。
所以拉起之前把这个目录临时清掉、拉起之后立刻还原：外部程序用自己目录和系统的运行库。
"""
import contextlib
import os
import sys
import threading

_lock = threading.Lock()
_registered: set[str] = set()


def _k32():
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    k.SetDllDirectoryW.restype = ctypes.c_int
    return k


def current_dll_directory() -> str:
    """现在用 SetDllDirectory 设着的目录；没设或读不出来返回空串。"""
    if os.name != "nt":
        return ""
    import ctypes
    k = _k32()
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = k.GetDllDirectoryW(len(buf), buf)
        if n:
            return buf.value
    except (AttributeError, OSError):
        pass
    return getattr(sys, "_MEIPASS", "") if getattr(sys, "frozen", False) else ""


@contextlib.contextmanager
def private_dll_directory():
    """with 里面拉起的子进程（以及它们再拉起的）不继承本程序的 DLL 搜索目录。别的线程在这几毫秒里加载本程序自己的扩展模块不受影响：
    先把同一个目录登记成 AddDllDirectory 的目录（只对本进程有效、不会被子进程继承），Python 加载扩展模块用的就是这个范围。"""
    if os.name != "nt":
        yield
        return
    with _lock:
        prev = current_dll_directory()
        if not prev or not os.path.isdir(prev):
            yield
            return
        if prev not in _registered:
            try:
                os.add_dll_directory(prev)
                _registered.add(prev)
            except OSError:
                pass
        k = _k32()
        k.SetDllDirectoryW(None)
        try:
            yield
        finally:
            k.SetDllDirectoryW(prev)


def child_env(env: dict, home: str | None = None) -> dict:
    """给外部程序的环境变量：去掉 PATH 里指向本程序打包目录（_internal）的几项，以及 PyInstaller 留下的 _PYI_* 变量。
    外部程序的运行库要从它自己的目录和系统里找，不该跑到本程序的目录里去（那里的文件装新版时要被覆盖）。"""
    home = home or (getattr(sys, "_MEIPASS", "") if getattr(sys, "frozen", False) else "")
    out = {k: v for k, v in env.items() if not k.upper().startswith("_PYI_") and k.upper() != "_MEIPASS2"}
    if home:
        base = os.path.normcase(os.path.abspath(home)).rstrip("\\") + "\\"
        for k in list(out):
            if k.upper() == "PATH":
                keep = [p for p in out[k].split(os.pathsep) if p and not (os.path.normcase(os.path.abspath(p)).rstrip("\\") + "\\").startswith(base)]
                out[k] = os.pathsep.join(keep)
    return out
