"""单实例：同一份数据目录，任何时候只能有一个 PodMates 主程序在运行。

靠一个跟数据目录绑定的系统级互斥体（Windows 命名互斥体，名字里带数据目录路径的哈希，不同数据目录互不影响）。程序一启动就先取它：取到了就是第一个，
取不到说明已经有一个在运行（或者刚好在启动、面板还没起来）。进程崩溃或被强行结束时系统会自动释放它，下一次启动照常取得——不靠文件，不会留下「锁死」。
以前靠读 runtime.json 再访问里面的端口来判断：两个进程几乎同时启动时，第一个还没写出文件、还没开始监听，第二个检查不到它，就也启动了。

极少见的情况下两个命名互斥体都建不了（安全策略、会话限制等），这时不直接放行，改用数据目录里的锁文件兜底：一样是进程活着就一直占着、
进程结束时由系统放掉，同一份数据目录仍然只有一个。
"""
import hashlib
import os
from pathlib import Path

ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED = 183, 5
LOCK_FILE = "podmates.lock"  # 互斥体都建不了时用的后备锁文件（放在数据目录里）
_held: list = []  # 取到的互斥体句柄 / 锁文件：进程活着就一直攥着（句柄不关，系统在进程结束时释放）


def mutex_name(data: Path, scope: str = "Global") -> str:
    """名字里带数据目录（真实路径、不分大小写）的哈希：同一份数据目录同一个名字，不同数据目录不同名字。"""
    key = os.path.normcase(os.path.realpath(str(data)))
    return f"{scope}\\PodMates-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}"


def _create_mutex(data: Path, scope: str) -> tuple:
    """建一个命名互斥体，返回（句柄, 错误码, kernel32）。句柄为空时看错误码：ACCESS_DENIED 说明别的账户占着。"""
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    h = k.CreateMutexW(None, False, mutex_name(data, scope))
    return h, ctypes.get_last_error(), k


def _fallback_lock(data: Path) -> bool:
    """互斥体建不了时的后备：在数据目录里锁住 lock 文件的第一个字节。进程结束（包括崩溃）时系统自动释放；
    同一个进程再开一个句柄来锁同一段也会失败，所以同一份数据目录仍只放行一个。"""
    import msvcrt
    path = data / LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+b")
    except OSError:
        return False
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return False
    _held.append(f)
    return True


def acquire(data: Path) -> bool:
    """取这份数据目录的互斥体。True = 我是第一个；False = 已经有一个在运行（或刚好在启动）。
    先试 Global\\（不同登录会话之间也只留一个），建不了再退到 Local\\；别的账户已经占着 Global 的名字（拒绝访问）也算已有一个在运行。
    两种互斥体都建不了时用数据目录里的锁文件兜底：锁文件也锁不上就是已经有一个在运行。"""
    if os.name != "nt":
        return True
    for scope in ("Global", "Local"):
        h, err, k = _create_mutex(data, scope)
        if not h:
            if err == ERROR_ACCESS_DENIED:
                return False  # 别的账户建的、我们没权限打开：同一份数据目录已经有人在用
            continue
        if err == ERROR_ALREADY_EXISTS:
            k.CloseHandle(h)
            return False
        _held.append(h)
        return True
    return _fallback_lock(data)  # 两种名字都建不了（极少见）：不直接放行，用锁文件保证同一份数据目录只跑一个
