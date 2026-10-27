"""资源监控：每小时往日志（「资源」类别）记一行服务器的资源情况；内存或硬盘快不够时，用现有的通知提醒用户。

只在 PodMates 现有的定时循环里做（Core.periodic 每 30 秒走一圈，这里自己按间隔判断），不另开线程或常驻进程。
内存按「已用虚拟内存（commit）占上限的百分比」判断，不看「剩余物理内存」——Windows 会拿空闲内存做缓存，那个数平时就偏低，会误报。
"""
import asyncio
import ctypes
import os
import time
from pathlib import Path

import psutil

GB = 1024 ** 3
MEM_EVERY, DISK_EVERY, LINE_EVERY = 60, 3600, 3600  # 秒：内存每分钟查一次，硬盘和日志每小时一次


def read_memory() -> dict:
    """物理内存剩余 / 总量，虚拟内存（commit）已用 / 上限（字节）。Windows 上 commit 来自 GlobalMemoryStatusEx：ullTotalPageFile 就是 commit 上限。"""
    vm = psutil.virtual_memory()
    out = {"phys_avail": vm.available, "phys_total": vm.total}
    if os.name == "nt":
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        out.update(commit_limit=ms.ullTotalPageFile, commit_used=ms.ullTotalPageFile - ms.ullAvailPageFile)
    else:  # 别的系统：物理 + 交换分区当作上限
        sw = psutil.swap_memory()
        out.update(commit_limit=vm.total + sw.total, commit_used=(vm.total - vm.available) + sw.used)
    return out


def read_disk(path) -> dict:
    u = psutil.disk_usage(str(path))
    return {"free": u.free, "total": u.total, "drive": os.path.splitdrive(str(Path(path).resolve()))[0] or str(path)}


def top_processes(n: int = 3) -> list[tuple[str, int, int]]:
    """占内存最多的几个进程（名字, pid, 字节）。Windows 上 vms 就是进程的私有提交量，和 commit 对得上。"""
    rows = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            mi = p.info["memory_info"]
            rows.append((p.info["name"] or "?", p.pid, max(mi.vms, mi.rss) if mi else 0))
        except (psutil.Error, OSError):
            continue
    return sorted(rows, key=lambda r: -r[2])[:n]


def count_processes() -> dict:
    """NapCat 启动器、QQ、微信的进程各有几个。"""
    want = {"napcat": ("napcatwinbootmain.exe",), "qq": ("qq.exe",), "wechat": ("weixin.exe", "wechat.exe")}
    out = {k: 0 for k in want}
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info["name"] or "").lower()
        except (psutil.Error, OSError):
            continue
        for k, names in want.items():
            if n in names:
                out[k] += 1
    return out


def dir_size(path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def gb(n: float) -> str:
    return f"{n / GB:.1f} GB"


class Monitor:
    def __init__(self, core):
        self.core = core
        self.last = {"mem": 0.0, "disk": 0.0, "line": 0.0}
        self.over_since: float | None = None  # commit 连续超线从什么时候开始
        st = core.db.kv("monitor_alert") or {}
        self.alerted: dict[str, float] = {k: float(st.get(k) or 0) for k in ("mem", "disk")}  # 这一种提醒上一次发在什么时候（0 = 现在没有未恢复的提醒）

    @property
    def s(self):
        return self.core.settings

    def save(self):
        self.core.db.set_kv("monitor_alert", self.alerted)

    def new_today(self, kind: str, now: float) -> bool:
        """同一种提醒每天最多发一次；恢复正常后再出问题（alerted 被清零）算新的一次。"""
        t = self.alerted.get(kind) or 0
        return not t or time.strftime("%Y-%m-%d", time.localtime(t)) != time.strftime("%Y-%m-%d", time.localtime(now))

    def alert(self, kind: str, text: str, now: float):
        if not self.s.g("resource_alert"):
            return  # 总开关关了：不提醒（照常记录）
        self.alerted[kind] = now
        self.save()
        self.core.engine.notify(text)  # 日志（通知类别）、面板「未读通知」、发消息给用户卡本人、Windows 气泡都在这里一起做

    def recover(self, kind: str):
        if self.alerted.get(kind):
            self.alerted[kind] = 0
            self.save()

    def procs_text(self) -> str:
        return "、".join(f"{n}（{gb(b)}）" for n, _, b in top_processes(3)) or "（读不到）"

    async def step(self, now: float | None = None):
        """走一步：到点了才真查。返回做了什么（测试用）。"""
        now = now or time.time()
        did = []
        if now - self.last["mem"] >= MEM_EVERY:
            self.last["mem"] = now
            self.check_memory(now)
            did.append("mem")
        if now - self.last["disk"] >= DISK_EVERY:
            self.last["disk"] = now
            await self.check_disk(now)
            did.append("disk")
        if now - self.last["line"] >= LINE_EVERY:
            self.last["line"] = now
            self.log_line()
            did.append("line")
        return did

    def check_memory(self, now: float):
        m = read_memory()
        pct = m["commit_used"] * 100 / m["commit_limit"] if m["commit_limit"] else 0
        line, minutes = float(self.s.g("resource_mem_pct")), float(self.s.g("resource_mem_minutes"))
        if pct < line:
            self.over_since = None  # 跌回去了：重新计时，之前发过的提醒也算恢复
            self.recover("mem")
            return
        if self.over_since is None:
            self.over_since = now
        if now - self.over_since >= minutes * 60 and self.new_today("mem", now):
            self.alert("mem", f"内存告急：已用虚拟内存（commit）{pct:.0f}%（{gb(m['commit_used'])} / {gb(m['commit_limit'])}），已经连续 {int((now - self.over_since) // 60)} 分钟"
                              f" ≥ {line:g}%；剩余物理内存 {gb(m['phys_avail'])}。占内存最多的进程：{self.procs_text()}", now)

    async def check_disk(self, now: float):
        from .paths import data_dir
        d = read_disk(data_dir())
        limit = float(self.s.g("resource_disk_gb"))
        if d["free"] >= limit * GB:
            self.recover("disk")
            return
        if self.new_today("disk", now):
            size = await asyncio.get_running_loop().run_in_executor(None, dir_size, data_dir())
            self.alert("disk", f"硬盘告急：系统盘 {d['drive']} 只剩 {gb(d['free'])}（共 {gb(d['total'])}，提醒线 {limit:g} GB）；PodMates 的数据目录占 {gb(size)}。"
                               f"占内存最多的进程：{self.procs_text()}", now)

    def log_line(self):
        """每小时一行：剩余物理内存、commit 占比、系统盘剩余、各账号在不在线、NapCat / QQ / 微信进程在不在跑。"""
        from .paths import data_dir
        m, d, c = read_memory(), read_disk(data_dir()), count_processes()
        eng = self.core.engine
        accts = []
        for a in self.core.db.docs("account"):
            b = eng.bots.get(a["id"])
            st = b.channel.status if b and b.channel else "stopped"
            accts.append(f"{a.get('name') or a.get('qq') or a['id']}（{a['platform']}）{'在线' if st == 'online' else '没在线（' + st + '）'}")
        pct = m["commit_used"] * 100 / m["commit_limit"] if m["commit_limit"] else 0
        eng.log("info", "resource", f"资源：剩余物理内存 {gb(m['phys_avail'])}；已用虚拟内存 {pct:.0f}%（{gb(m['commit_used'])} / {gb(m['commit_limit'])}）；"
                f"系统盘 {d['drive']} 剩余 {gb(d['free'])}；账号：{'、'.join(accts) or '（没有）'}；进程：NapCat 启动器 {c['napcat']} 个、QQ {c['qq']} 个、微信 {c['wechat']} 个")
