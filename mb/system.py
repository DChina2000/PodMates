"""Windows 相关：开机自启（当前用户 Run 键）、局域网地址。"""
import socket
import subprocess
import winreg

from . import APP_NAME
from .paths import exe_command

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME


def get_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(exe_command("--background")))
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


def lan_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def total_ram_gb() -> float:
    """本机物理内存（GB）。"""
    import ctypes

    class MEMSTATUS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMSTATUS()
    m.dwLength = ctypes.sizeof(MEMSTATUS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return round(m.ullTotalPhys / 2 ** 30, 1)
