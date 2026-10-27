"""NapCat 管理：下载（官方 GitHub）、识别目录结构、写 WebSocket 配置、启停进程、抓登录二维码。

每个 QQ 账号一个 NapCat 进程；配置按 QQ 号分文件（onebot11_<QQ号>.json），同一个 NapCat 目录可跑多个号。
NapCat 许可证是自定义的「有限再分发许可证」，不明确允许打进第三方安装包，所以不打包，首次使用时从官方 GitHub 下载。
"""
import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import aiohttp

from .. import APP_NAME, winproc
from ..paths import data_dir
from .. import tls

RELEASE_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
ASSET = "NapCat.Shell.zip"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
QQ_DOWNLOAD = "https://im.qq.com/pcqq/index.shtml"


def default_root() -> Path:
    return data_dir() / "napcat" / "NapCat.Shell"


def layout(root: Path) -> dict | None:
    """认 NapCat 目录：shell（需要系统装了 QQ）或 onekey（自带 QQ）。认不出返回 None。"""
    root = Path(root)
    boot = root / "NapCatWinBootMain.exe"
    if not boot.exists():
        return None
    if (root / "napcat.mjs").exists():
        return {"kind": "shell", "root": root, "boot": boot, "config": root / "config", "cache": root / "cache"}
    if (root / "QQ.exe").exists():
        for d in sorted((root / "versions").glob("*/resources/app/napcat"), reverse=True):
            return {"kind": "onekey", "root": root, "boot": boot, "config": d / "config", "cache": d / "cache"}
    return None


def system_qq() -> Path | None:
    import winreg
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ")):
        try:
            with winreg.OpenKey(hive, sub) as k:
                exe = Path(winreg.QueryValueEx(k, "UninstallString")[0].strip('"')).parent / "QQ.exe"
                if exe.exists():
                    return exe
        except OSError:
            continue
    return None


def write_config(lay: dict, uin: str, port: int, token: str):
    """把本程序用的正向 WebSocket 写进 onebot11_<QQ号>.json；其他已有的网络配置原样保留。"""
    cfg_dir = lay["config"]
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / f"onebot11_{uin}.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    net = data.setdefault("network", {})
    for k in ("httpServers", "httpSseServers", "httpClients", "websocketServers", "websocketClients", "plugins"):
        net.setdefault(k, [])
    net["websocketServers"] = [s for s in net["websocketServers"] if s.get("name") not in (APP_NAME, "MultiBot")] + [{
        "enable": True, "name": APP_NAME, "host": "127.0.0.1", "port": port, "reportSelfMessage": False,
        "enableForcePushEvent": True, "messagePostFormat": "array", "token": token, "debug": False, "heartInterval": 30000}]
    data.setdefault("musicSignUrl", "")
    data.setdefault("enableLocalFile2Url", False)
    data.setdefault("parseMultMsg", False)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def download(progress=None, proxy: str | None = None) -> Path:
    """从官方 GitHub Releases 下载最新 NapCat.Shell.zip 并解压到数据目录。"""
    root = default_root()
    base = root.parent
    base.mkdir(parents=True, exist_ok=True)
    async with tls.session(timeout=aiohttp.ClientTimeout(total=1800)) as s:
        async with s.get(RELEASE_API, proxy=proxy or None, headers={"Accept": "application/vnd.github+json"}) as r:
            rel = await r.json()
        asset = next(a for a in rel["assets"] if a["name"] == ASSET)
        zpath = base / ASSET
        async with s.get(asset["browser_download_url"], proxy=proxy or None) as r:
            r.raise_for_status()
            total, done = int(r.headers.get("Content-Length") or asset.get("size") or 0), 0
            with open(zpath, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    tmp = base / "_extract"
    if tmp.exists() and tmp.resolve().parent == base.resolve():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    inner = next((p.parent for p in tmp.rglob("NapCatWinBootMain.exe")), tmp)
    if root.exists() and root.resolve().parent == base.resolve():
        # 保留已有的登录配置
        keep = root / "config"
        if keep.exists():
            shutil.copytree(keep, inner / "config", dirs_exist_ok=True)
        shutil.rmtree(root)
    shutil.move(str(inner), str(root))
    if tmp.exists() and tmp.resolve().parent == base.resolve():
        shutil.rmtree(tmp)
    zpath.unlink(missing_ok=True)
    (root / "版本.txt").write_text(rel.get("tag_name", ""), encoding="utf-8")
    return root


# ---------- NapCat 独立运行：日志文件、接回 ----------
# NapCat 不挂在 PodMates 进程上：用 CREATE_NEW_PROCESS_GROUP（尽量再加 BREAKAWAY_FROM_JOB）拉起，输出写进数据目录的日志文件而不是管道——
# 管道的另一头是 PodMates，它一死管道就断，NapCat 再往标准输出写就是 EPIPE（Node 里没人接的话会直接崩，见 BUILDING / 报告里的实验）。
# PodMates 重新启动时先看这个号原来的 NapCat 还在不在、连不连得上、登录的是不是这个号，是就接回去，QQ 不用重新登录。
CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB = 0x00000200, 0x01000000
LOG_DIR = "napcat-logs"
LOG_LIMIT = 24 << 20  # 日志文件超过这个大小、并且读完了，就清空重来（NapCat 每收一条消息都会打一行）
RUN_FILE = "napcat-run.json"


def log_path(uin: str) -> Path:
    return data_dir() / LOG_DIR / f"{uin}.log"


def open_log_for_child(path: Path):
    """打开日志文件，交给 NapCat 当标准输出 / 标准错误。Windows 上只给「追加」权限（FILE_APPEND_DATA）：不管谁清空过文件，NapCat 写的都落在文件末尾，
    PodMates 才能在文件太大时把它清空（普通的追加打开，写入位置记在句柄里，清空后会在文件中间留一大段 0）。返回可以交给 Popen 的 fd。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return open(path, "ab").fileno()
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID), ("bInheritHandle", wintypes.BOOL)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    FILE_APPEND_DATA, FILE_READ_ATTRIBUTES, SHARE_ALL, OPEN_ALWAYS, NORMAL = 0x4, 0x80, 0x7, 4, 0x80
    h = k32.CreateFileW(str(path), FILE_APPEND_DATA | FILE_READ_ATTRIBUTES, SHARE_ALL, ctypes.byref(sa), OPEN_ALWAYS, NORMAL, None)
    if h in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(h, os.O_APPEND)


def rotate_log(path: Path):
    """重新拉起 NapCat 之前：上一轮的日志留成 .1（给排查用），这一轮从空文件开始。上一轮的进程已经清掉了，文件没人占。"""
    if not path.exists():
        return
    try:
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        try:
            path.write_bytes(b"")
        except OSError:
            pass


class LogTail:
    """从日志文件往后读新增的行（认二维码、踢下线、风控）。文件被清空（变小）了就从头读；半行留着下次接着读。"""

    def __init__(self, path: Path, at_end: bool):
        self.path, self.buf = path, b""
        try:
            self.pos = path.stat().st_size if at_end else 0
        except OSError:
            self.pos = 0

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def read(self) -> list[str]:
        size = self.size()
        if size < self.pos:
            self.pos, self.buf = 0, b""
        if size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(min(size - self.pos, 4 << 20))
        except OSError:
            return []
        self.pos += len(data)
        *lines, self.buf = (self.buf + data).split(b"\n")
        return [ANSI.sub("", ln.decode("utf-8", "replace")).rstrip() for ln in lines]

    def trim(self, limit: int = LOG_LIMIT):
        """读完了而且文件太大：清空。"""
        size = self.size()
        if size > limit and self.pos >= size:
            try:
                with open(self.path, "r+b") as f:
                    f.truncate(0)
                self.pos, self.buf = 0, b""
            except OSError:
                pass


def load_run() -> dict:
    try:
        return json.loads((data_dir() / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_run(uin: str, meta: dict | None):
    """记下每个号的 NapCat 是在哪个文件夹、哪个端口起的（接回时核对，别接到别的 NapCat 上）。"""
    allr = load_run()
    if meta:
        allr[str(uin)] = meta
    else:
        allr.pop(str(uin), None)
    try:
        if allr:
            (data_dir() / RUN_FILE).write_text(json.dumps(allr, ensure_ascii=False), encoding="utf-8")
        else:
            (data_dir() / RUN_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


async def probe_login(url: str, token: str, timeout: float = 6.0) -> tuple[str, str]:
    """连一下 NapCat 的正向 ws，问 get_login_info：返回（登录着的号, 失败原因）。能连上就说明 OneBot 服务起来了，也就是登录过了。"""
    try:
        async with tls.session(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.ws_connect(url, headers={"Authorization": f"Bearer {token}"}) as ws:
                await ws.send_str(json.dumps({"action": "get_login_info", "params": {}, "echo": "mbprobe"}))
                while True:
                    m = await ws.receive(timeout=timeout)
                    if m.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(m.data)
                        if d.get("echo") == "mbprobe":
                            uid = str((d.get("data") or {}).get("user_id") or "")
                            return (uid, "") if uid else ("", f"get_login_info 没有返回账号：{d.get('message') or d.get('wording') or d.get('retcode')}")
                    elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        return "", "连上之后被对方关掉了（令牌不对，或者 NapCat 还没登录好）"
    except Exception as e:
        return "", f"连不上 {url}（{type(e).__name__}：{str(e)[:80]}）"


class NapCatProcess:
    def __init__(self, channel, root: Path, uin: str, port: int, token: str, webui_port: int, hide: bool, webui_token: str = ""):
        self.ch, self.uin, self.port, self.token, self.webui_port, self.hide = channel, uin, port, token, webui_port, hide
        self.webui_token = webui_token
        self.lay = layout(root)
        self.popen: subprocess.Popen | None = None  # 这次是 PodMates 拉起来的；接回的是 None
        self.attached = False  # 接回了上一次 PodMates 留下的 NapCat
        self.closed = False
        self.boot_pid: int | None = None
        self.pids: set[int] = set()
        self.times: dict[int, float] = {}  # pid → 创建时间
        self.tail: LogTail | None = None
        self.qr_url = ""
        self.qr_png: Path | None = None
        self.started = 0.0
        self.warned_silent = False

    # ---- 启动 / 接回 ----
    def launch_plan(self) -> tuple[list[str], dict]:
        """启动命令和环境变量。"""
        root = self.lay["root"]
        env = dict(os.environ, NAPCAT_WEBUI_PREFERRED_PORT=str(self.webui_port))
        if self.webui_token:  # 备用网页的登录令牌：所有账号共用一个（NapCat 的 webui.json 是共用的）
            env["NAPCAT_WEBUI_SECRET_KEY"] = self.webui_token
        if self.lay["kind"] == "shell":
            qq = system_qq()
            if not qq:
                raise RuntimeError(f"这台电脑没装 QQ。NapCat 要借用电脑上的 QQ，请先到官网安装：{QQ_DOWNLOAD}")
            env.update(NAPCAT_PATCH_PACKAGE=str(root / "qqnt.json"), NAPCAT_LOAD_PATH=str(root / "loadNapCat.js"),
                       NAPCAT_INJECT_PATH=str(root / "NapCatWinBootHook.dll"), NAPCAT_LAUNCHER_PATH=str(self.lay["boot"]),
                       NAPCAT_MAIN_PATH=str(root / "napcat.mjs"))
            return [str(self.lay["boot"]), str(qq), str(root / "NapCatWinBootHook.dll"), "-q", self.uin], env
        return [str(self.lay["boot"]), self.uin], env

    async def start(self):
        if not self.lay:
            raise RuntimeError("找不到 NapCat：请在账号页点「下载 NapCat」，或选择已有的 NapCat 文件夹")
        eng, uin, root = self.ch.engine, self.uin, self.lay["root"]
        self.qr_png = self.lay["cache"] / "qrcode.png"
        self.started = time.time()
        if await self.try_attach():
            return
        write_config(self.lay, uin, self.port, self.token)
        gone = reap(root, uin)  # 接不回的残留（进程在但连不上 / 登录的不是这个号 / 不在同一个文件夹）：先清掉，不然新的 QQ 登不上同一个号
        if gone:
            eng.log("warn", "napcat", f"[{uin}] 清掉了上一次遗留的 {len(gone)} 个 NapCat / QQ 进程")
        cmd, env = self.launch_plan()
        eng.log("info", "napcat", f"[{uin}] 启动 NapCat（{self.lay['kind']}）：{' '.join(Path(c).name if i < 2 else c for i, c in enumerate(cmd))}；"
                f"启动时的 Windows 用户 {os.environ.get('USERNAME', '?')}，USERPROFILE {os.environ.get('USERPROFILE', '?')}，APPDATA {os.environ.get('APPDATA', '?')}")
        lp = log_path(uin)  # 不论弹不弹黑窗口，输出都进日志：二维码、踢下线、风控都从这里认
        rotate_log(lp)
        fd = open_log_for_child(lp)
        try:
            self.popen = await asyncio.get_running_loop().run_in_executor(None, spawn_detached, cmd, str(root), env, self.hide, fd)
        finally:
            if fd is not None:
                os.close(fd)
        self.boot_pid = self.popen.pid
        self.pids = {self.boot_pid}
        try:  # 马上记下启动器的 pid（不等 6 秒一轮的扫描）：这几秒里 PodMates 没了，下次启动也认得它
            self.times[self.boot_pid] = _psutil().Process(self.boot_pid).create_time()
            save_pids(uin, dict(self.times))
        except Exception:
            pass
        self.tail = LogTail(log_path(uin), at_end=False)
        self.write_run()
        asyncio.create_task(self._watch())

    def write_run(self):
        save_run(self.uin, {"root": str(self.lay["root"]), "port": self.port, "kind": self.lay["kind"], "started": self.started, "log": str(log_path(self.uin))})

    async def try_attach(self) -> bool:
        """这个号原来的 NapCat 还活着、连得上、登录的还是这个号，就接回去（不重启、QQ 不用重新登录）；否则返回 False，由调用方清理后重开，并在日志里写明原因。"""
        eng, uin, root = self.ch.engine, self.uin, self.lay["root"]
        loop = asyncio.get_running_loop()
        mine = await loop.run_in_executor(None, leftovers, root, uin)
        if not mine:
            eng.log("info", "napcat", f"[{uin}] 没有在运行的 NapCat，直接启动")
            return False
        run, why = load_run().get(uin) or {}, ""
        if run.get("root") and not same_path(run["root"], root):
            why = f"原来的 NapCat 是在别的文件夹起的（{run['root']}），现在用的是 {root}"
        elif run.get("port") and int(run["port"]) != int(self.port):
            why = f"原来的 NapCat 用的 ws 端口是 {run['port']}，现在设置的是 {self.port}"
        else:
            got, err = await probe_login(self.ch.url, self.token)
            if err:
                why = err
            elif got != uin:
                why = f"那个 NapCat 登录的是 {got}，不是 {uin}"
        if why:
            eng.log("warn", "napcat", f"[{uin}] 没能接回原来的 NapCat（{why}），清理后重新启动")
            return False
        self.attached = True
        self.pids, self.times = set(mine), dict(mine)
        save_pids(uin, mine)
        lp = log_path(uin)
        have_log = bool(run.get("log")) and lp.exists()
        self.tail = LogTail(lp, at_end=True) if have_log else None
        run = {"root": str(root), "port": self.port, "kind": self.lay["kind"], "started": run.get("started") or self.started, "log": str(lp) if have_log else ""}
        save_run(uin, run)
        eng.log("info", "napcat", f"[{uin}] 接回了已在运行的 NapCat，QQ 没有重新登录（进程 {'、'.join(map(str, sorted(mine)))}）"
                + ("" if have_log else "；它不是由带日志的方式拉起的（旧版本拉的），二维码 / 踢下线这类提示看不到"))
        asyncio.create_task(self._watch())
        return True

    # ---- 盯着 ----
    def _line(self, text: str):
        if not text:
            return
        m = re.search(r"二维码解码URL[:：]\s*(\S+)", text)
        if m:
            self.qr_url = m.group(1)
            self.ch.on_qr(self.qr_url)
        if "[KickedOffLine]" in text:
            self.ch.engine.notify(f"QQ {self.uin} 被踢下线：{text[-80:]}", self.ch.account)
        elif "风控" in text:
            self.ch.engine.notify(f"QQ {self.uin} 可能触发风控：{text[-80:]}", self.ch.account)
        text = redact(text, (self.webui_token, self.token))
        self.ch.engine.log("debug", "napcat", f"[{self.uin}] {text[:300]}")
        hint = login_hint(text)
        if hint:
            self.ch.engine.log(hint[0], "napcat", f"[{self.uin}] {hint[1]}")

    def pump(self):
        """读日志文件里新增的行。"""
        if not self.tail:
            return
        for text in self.tail.read():
            self._line(text)
        self.tail.trim()
        if self.popen is not None and not self.warned_silent and self.tail.pos == 0 and time.time() - self.started > 45:
            self.warned_silent = True
            self.ch.engine.log("warn", "napcat", f"[{self.uin}] NapCat 启动 45 秒了，日志文件 {self.tail.path} 还是空的：二维码、被踢下线这类提示可能认不出来")

    async def _watch(self):
        """记下 NapCat 拉起的 QQ 进程（停止时一起结束）、读日志文件认事件；进程没了就通知。另外也从 qrcode.png 认二维码。"""
        seen_png, tick, empty = 0.0, 0, 0
        loop = asyncio.get_running_loop()
        while not self.closed:
            if tick % 3 == 0:  # 每 6 秒扫一遍：启动器、QQ 本体（启动器退出后它不在进程树里了）、它们的子进程
                found = await loop.run_in_executor(None, owned, self.lay["root"], self.uin, self.boot_pid)
                if found:
                    empty = 0
                    if set(found) - self.pids:
                        self.pids |= set(found)
                        self.times.update(found)
                        save_pids(self.uin, {p: self.times[p] for p in self.pids if p in self.times})
                else:
                    empty += 1
            tick += 1
            self.pump()
            if self.popen is not None:
                if self.popen.poll() is not None:
                    break
            elif empty >= 2:  # 接回来的：没有进程句柄，连着两轮（12 秒）扫不到任何一个才算没了
                break
            if not self.hide and self.qr_png and self.qr_png.exists() and self.qr_png.stat().st_mtime > max(seen_png, self.started):
                seen_png = self.qr_png.stat().st_mtime
                self.ch.on_qr("")
            await asyncio.sleep(2)
        if not self.closed and not self.ch.stopping:
            self.pump()
            code = self.popen.returncode if self.popen is not None else None
            self.ch.engine.notify(f"QQ {self.uin} 的 NapCat 停了（退出码 {code if code is not None else '不详'}）", self.ch.account)
            self.ch.set_status("error", "NapCat 已退出")

    async def stop(self):
        """用户明确要关的时候才走到这里（面板停止账号 / 停止机器人、托盘退出、卸载）；PodMates 崩溃、被强行结束、装新版都不走这里，NapCat 照样挂着。"""
        if self.closed or not (self.popen or self.attached):
            return
        self.closed = True
        loop = asyncio.get_running_loop()
        procs = {p: self.times.get(p, 0.0) for p in self.pids}
        procs.update(await loop.run_in_executor(None, owned, self.lay["root"], self.uin, self.boot_pid))  # 此刻还活着的（包括不在进程树里的 QQ 本体）
        kill_procs(procs)
        if self.popen is not None:
            try:
                await asyncio.wait_for(loop.run_in_executor(None, self.popen.wait), 10)
            except asyncio.TimeoutError:
                pass
        left = await loop.run_in_executor(None, owned, self.lay["root"], self.uin)  # 再扫一遍：结束了一批之后，启动器可能又拉起了谁
        if left:
            kill_procs(left)
        self.pids.clear()
        save_pids(self.uin, {})
        save_run(self.uin, None)


def spawn_detached(cmd: list, cwd: str, env: dict, hide: bool, fd: int | None) -> subprocess.Popen:
    """拉起 NapCat，让它不依附 PodMates：自己一个进程组；尽量脱离 PodMates 所在的作业（Windows 的任务计划、服务管理器有时会在作业结束时带走所有子进程，
    作业不让脱离就退回不脱离）；输出直接写日志文件，不经过管道。"""
    flags = (subprocess.CREATE_NO_WINDOW if hide else subprocess.CREATE_NEW_CONSOLE) | CREATE_NEW_PROCESS_GROUP
    kw = dict(stdin=subprocess.DEVNULL, stdout=fd, stderr=fd) if fd is not None else {}
    env = winproc.child_env(env)
    with winproc.private_dll_directory():  # 不让 NapCat / QQ 继承本程序的 DLL 搜索目录（_internal），不然它们会一直占着程序目录里的运行库，装新版覆盖不了
        try:
            return subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True, **kw)
        except OSError as e:
            if getattr(e, "winerror", 0) != 5:  # 5 = 拒绝访问：所在的作业不允许脱离
                raise
            return subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=flags, close_fds=True, **kw)


def redact(text: str, known: tuple) -> str:
    """NapCat 会打印令牌，写进日志前抹掉。"""
    for s in known:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(?i)(token\s*(?:[=:：]|为)\s*)[\w-]{6,}", r"\1***", text)


def new_token() -> str:
    return secrets.token_hex(16)


# ---------- NapCat 启动的所有进程：认出来、记下来、停干净 ----------
# 一键包里 QQ.exe 只是个启动器，它拉起 versions\…\QQ.exe 之后自己就退出了，真正的 QQ 本体和它的子进程不再挂在启动器 → PodMates 这棵进程树下，
# 只按进程树结束是结束不掉它们的。所以按「这个号」认：① NapCat 启动器（NapCatWinBootMain.exe，在这个 NapCat 目录下，命令行里带这个号）；
# ② QQ.exe，命令行带 NapCat 启动器加的 --enable-logging 和 -q <这个号>（一键包的 QQ 本体，或 Shell 方式用的电脑上装的 QQ）；
# 再加上它们的所有子孙。同一个 NapCat 目录里别的号、用户自己开的 QQ（没有 --enable-logging，也没有 -q）都不沾边。
PID_FILE = "napcat-pids.json"


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def is_ours(name: str, exe: str, cmdline: list, root: Path, uin: str) -> bool:
    """这个进程是不是这个号的 NapCat 启动器 / QQ 本体（子孙另算）。"""
    name, exe, args = (name or "").lower(), (exe or "").lower(), [str(a) for a in cmdline or []]
    if name == "napcatwinbootmain.exe":
        return exe.startswith(str(Path(root)).lower().rstrip("\\") + "\\") and str(uin) in args[1:]
    if name == "qq.exe":
        return "--enable-logging" in args and any(a == "-q" and b == str(uin) for a, b in zip(args, args[1:]))
    return False


def owned(root: Path, uin: str, boot_pid: int | None = None) -> dict[int, float]:
    """属于这个账号的 NapCat 的全部进程：{pid: 创建时间}。包含这些进程的子孙；不含 PodMates 自己。"""
    ps = _psutil()
    out: dict[int, float] = {}
    if not ps:
        return out
    for p in ps.process_iter(["pid", "name", "exe", "cmdline", "create_time"]):
        try:
            i = p.info
            if is_ours(i.get("name"), i.get("exe"), i.get("cmdline"), root, uin):
                out[i["pid"]] = i["create_time"]
        except (ps.Error, OSError):
            continue
    if boot_pid:
        try:
            out[boot_pid] = ps.Process(boot_pid).create_time()
        except ps.Error:
            pass
    for pid in list(out):
        try:
            for c in ps.Process(pid).children(recursive=True):
                out.setdefault(c.pid, c.create_time())
        except ps.Error:
            continue
    out.pop(os.getpid(), None)
    return out


def _pid_path() -> Path:
    return data_dir() / PID_FILE


def load_pids() -> dict[str, list]:
    try:
        return json.loads(_pid_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_pids(uin: str, procs: dict[int, float]):
    """把这个账号现在的 NapCat 进程记进 data\\napcat-pids.json（pid + 创建时间，防止 pid 被别的进程复用）。接回原来的 NapCat、卸载、清理没人管的 NapCat 时靠它认进程。"""
    allp = load_pids()
    if procs:
        allp[str(uin)] = [[pid, t] for pid, t in sorted(procs.items())]
    else:
        allp.pop(str(uin), None)
    try:
        if allp:
            _pid_path().write_text(json.dumps(allp), encoding="utf-8")
        else:
            _pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def kill_procs(procs: dict[int, float]) -> list[int]:
    """结束这些进程（先子孙后祖先）；pid 的创建时间对不上的（已经被别的进程复用）不碰。返回真的结束了的 pid。"""
    ps = _psutil()
    done = []
    for pid, created in sorted(procs.items(), reverse=True):
        try:
            if ps:
                p = ps.Process(pid)
                if created and abs(p.create_time() - created) > 2:
                    continue
                p.kill()
            else:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            done.append(pid)
        except Exception:
            continue
    return done


def leftovers(root: Path, uin: str) -> dict[int, float]:
    """这个号上一次留下的、现在还活着的 NapCat / QQ 进程：按路径 / 命令行认出来的 + 记下来的 pid（创建时间对得上才算，防止 pid 被别的进程复用）。"""
    found = owned(root, uin)
    ps = _psutil()
    for pid, t in load_pids().get(str(uin), []):
        try:
            if ps and (not t or abs(ps.Process(pid).create_time() - t) <= 2):
                found.setdefault(pid, t)
        except Exception:
            continue
    found.pop(os.getpid(), None)
    return found


def reap(root: Path, uin: str) -> list[int]:
    """接不回原来的 NapCat、要重新启动前：清掉它留下的进程。"""
    stale = leftovers(root, uin)
    for pid, t in load_pids().get(str(uin), []):
        stale.setdefault(pid, t)
    stale.pop(os.getpid(), None)
    done = kill_procs(stale)
    save_pids(uin, {})
    save_run(uin, None)
    return done


def reap_unwanted(keep: set[str]) -> dict[str, list[int]]:
    """PodMates 启动时：记下来的 NapCat 里，不属于这次要启动 / 正在跑的账号的（账号被停用、删掉、改成不托管 NapCat……）一并清掉，不然没人管它们了。"""
    out: dict[str, list[int]] = {}
    for uin, rec in list(load_pids().items()):
        if uin in keep:
            continue
        stale = {pid: t for pid, t in rec}
        root = (load_run().get(uin) or {}).get("root")
        if root:
            stale.update(owned(Path(root), uin))
        stale.pop(os.getpid(), None)
        done = kill_procs(stale)
        save_pids(uin, {})
        save_run(uin, None)
        if done:
            out[uin] = done
    return out


NOTES_FILE = "install-notes.txt"


def take_install_notes() -> list[str]:
    """安装程序装新版时，如果因为 NapCat / QQ 占着程序目录里的文件而把它们也停掉了，会在数据目录留一份说明：读出来（调用方记进日志），读完就删。"""
    path = data_dir() / NOTES_FILE
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        path.unlink(missing_ok=True)
        return lines
    except OSError:
        return []


# NapCat 的输出里和登录有关的几行：读出来换成一句人话记进日志（原文照旧留在 debug 里）。重启后又要扫码时，看这几行就知道卡在哪一步。
LOGIN_HINTS = [
    ("没有 -q 指令", "warn", "NapCat 没收到快速登录的账号参数（-q），所以走扫码登录。检查启动命令里有没有 -q / 账号"),
    ("正在快速登录", "info", "NapCat 正在用这台电脑上 QQ 的登录记录快速登录"),
    ("未找到该 QQ 历史登录记录", "warn", "快速登录没成功：这台电脑的 QQ 里没有这个号的登录记录——这次启动用的 Windows 用户 / QQ 数据目录，"
     "和上次扫码登录时不是同一个，或者上次登录的记录没保存下来（对照 启动时的 Windows 用户 和 QQ 数据目录 两行）"),
    ("快速登录错误", "warn", "快速登录被 QQ 拒绝了"),
    ("快速登录异常", "warn", "快速登录过程出错"),
    ("无法重复登录", "warn", "这个号已经在另一个 QQ 进程里登录着（多半是上一次遗留的 QQ 没退出），新的登不上"),
    ("EADDRINUSE", "warn", "端口被占用了（多半是上一次遗留的 NapCat / QQ 还在）"),
    ("二维码解码URL", "warn", "需要扫码登录（快速登录没成功或没有登录记录）"),
    ("本账号数据/缓存目录", "info", "QQ 数据目录"),
    ("已通知主进程登录成功", "info", "NapCat 登录成功"),
    ("Worker进程意外退出", "warn", "NapCat 的工作进程意外退出，正在自动重拉"),
]


def login_hint(text: str) -> tuple[str, str] | None:
    """NapCat 输出的一行 → (日志级别, 一句人话)；和登录无关的返回 None。"""
    for key, level, msg in LOGIN_HINTS:
        if key in text:
            detail = text.split("]", 1)[-1].strip() if key in ("快速登录错误", "快速登录异常", "本账号数据/缓存目录") else ""
            return level, msg + (f"：{detail[:160]}" if detail else "")
    m = re.match(r"^\s*\d+\.\s+(\d{5,12})\s*(.*)$", text.split("] ", 1)[-1]) if "可用于快速登录" not in text else None
    if m:
        return "info", f"这台电脑的 QQ 里可以快速登录的号：{m.group(1)} {m.group(2)[:20]}"
    if "可用于快速登录" in text:
        return "info", "这台电脑的 QQ 里可以快速登录的号（下面几行）"
    return None
