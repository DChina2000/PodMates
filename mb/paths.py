r"""程序资源目录与数据目录。

数据放哪里（按这个顺序，找到已经有数据库的那一处就用它）：
  1. 环境变量 PODMATES_DATA（测试、迁移验证用）；
  2. 安装目录下的 data 文件夹（「仅为我安装」时安装程序会建；程序和数据放在一起）；
  3. %APPDATA%\PodMates（「为所有用户安装」装在 Program Files 里普通用户写不进去，或老版本留下的数据）。
都还没有数据库（第一次运行）时：安装目录下有 data 文件夹就用它，否则用 %APPDATA%\PodMates。
开机自启、诊断包、导出导入、备份都从 data_dir() 取，跟着走。
"""
import os
import sys
from pathlib import Path

from . import APP_NAME

FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
WEB = RES / "web"
ASSETS = RES / "assets"


def app_dir() -> Path | None:
    """安装目录（打包版才有）。"""
    return Path(sys.executable).parent if FROZEN else None


def data_candidates() -> list[Path]:
    out = []
    if app_dir():
        out.append(app_dir() / "data")
    out.append(Path(os.environ.get("APPDATA", Path.home())) / APP_NAME)
    return out


def data_dir() -> Path:
    env = os.environ.get("PODMATES_DATA")
    if env:
        d = Path(env)
    else:
        cands = data_candidates()
        has_db = lambda p: (p / "podmates.db").exists() or (p / "multibot.db").exists()
        d = next((c for c in cands if has_db(c)), None) or (cands[0] if cands[0].is_dir() else cands[-1])
    d.mkdir(parents=True, exist_ok=True)
    return d


def exe_command(*args: str) -> list[str]:
    """重新启动本程序（窗口进程、开机自启）用的命令行。"""
    if FROZEN:
        return [sys.executable, *args]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else sys.executable), "-m", "mb", *args]
