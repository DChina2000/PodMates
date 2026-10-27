"""微信小号：界面自动化（wxauto4，支持微信 4.x），不注入、不经第三方服务器。

- wxauto4 没写许可证，不打进安装包；第一次开微信时从 PyPI（作者的官方发布渠道）下载到数据目录。
- 认人靠「小号给对方设的秘密备注」：微信界面上显示的是备注，人物表按备注对上人。
- 自动化要微信窗口在场：窗口不能关（可以挡在后面），所以和「后台不弹窗」冲突，面板里有说明。
- 微信撤回、改资料、改群昵称都做不了，面板里提示手动操作。
- 身份绑定：这台电脑上登录的可能是用户自己的大号，所以微信账号「首次绑定，之后核对」：
  第一次启动读出当前登录者的昵称/微信号，请用户在面板里确认「这是机器人小号吗」，确认了才开始监听并记下；
  之后每次启动、运行中每隔一会儿都核对，对不上就拒绝启动 / 立刻停止并发通知。绑定记在库里的 kv（wxbind:账号id）。
"""
import asyncio
import ctypes
import json
import re
import sys
import threading
import time
import uuid
import shutil
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..paths import data_dir
from ..winpos import Keeper, WinApi, recover, unhide
from .base import Channel, Msg
from .. import tls
from ..wxprobe import CHAT_TARGET
from ..wxpicture import EMOTION_TEXT, fetch as fetch_picture, is_emotion, is_picture
from ..wxsticker import SETTLE, PanelError, WxStickers
from .. import wxshot as shot_mod

# 微信版本范围。wxauto4 没有机器可读的「支持范围」，只在 PyPI 的说明里写了一句「适用最新4.1.8」；
# 下面的 4.0 ～ 4.1.8.107 是我们按这句话定的：4.0 起、到 4.1.8 这一大版本的末尾（.107 是我们估的 4.1.8 最后一个小版本号）。
SUPPORTED_MIN, SUPPORTED_MAX = (4, 0, 0, 0), (4, 1, 8, 107)
# 超出上面的范围、但我们自己在真机上实测过能用的版本（只写前三段，同一个大版本下的小版本都算）。4.1.15 用「主窗口轮询」收消息。
VERIFIED = [(4, 1, 15)]
WX_LIB = "wxauto4"

# wxauto4 的 GetMyInfo() 返回 dict[str, str]，文档没列键名；这里按常见叫法找，找不到再看别处，最后才用昵称
ID_KEYS = ("wxid", "wechat_id", "weixin_id", "wechatid", "weixinid", "alias", "account", "id", "微信号")
NICK_KEYS = ("nickname", "nick", "name", "display_name", "昵称")
RECHECK_SECS = 60  # 运行中，每次要切会话 / 发送时，距上次核对超过这么久就顺带重新读一遍登录的是不是绑定的号
IDLE_RECHECK_SECS = 600  # 一直没有切会话 / 发送时，隔这么久单独复核一次
POLL_TAIL = 40  # 轮询方式：记住每个聊天最后这么多条，下次读到时靠它找出哪些是新的
HEARD_MAX = 300  # 记着最近收到的这么多条消息的发送人和内容，引用时按 id 找不到可以按这两样找
PIC_TIMEOUT = 25  # 下载一张图最多等多久（秒），超了这张就写「[图片]」
PIC_KEEP = 20  # 下载好、还没交给看图的图片最多留几张（看图关了、用量到顶这类没人取的，旧的被挤掉）
POLL_MAX_NEW = 10  # 一次最多当作新消息处理多少条（找不到衔接时的保险，避免把一屏旧消息当成新的刷出去）
# 读不到界面（复核身份没连上、轮询读不到）连续这么多次，就不再算「界面没就绪、稍后再试」，把账号状态写成出错并通知。
# 6 次约 3 分钟：复核身份每 10 分钟一次、每次 41 秒，轮询 1 秒一次；跳过一次重试是 2 次，6 次够显示窗口后重试一次
# （第 1 次）和几轮自然重试，不会因为一次界面卡顿就报警，也不会像以前那样一直显示「在线」却收不到消息。
UI_FAIL_MAX = 6


def _wxid_from_dir(wx) -> str:
    """微信 4 的数据目录名里带 wxid（xwechat_files/wxid_xxx_yyyy）；取得到就多一个稳定标识。"""
    try:
        d = str(getattr(wx, "dir", "") or "")
    except Exception:
        return ""
    m = re.search(r"(wxid_[0-9A-Za-z]+)", d)
    return m.group(1) if m else ""


def read_identity(wx) -> dict:
    """读出当前登录微信的身份 {wxid, nick}，读不到的留空。只读，不发消息。"""
    info = {}
    for src in (lambda: wx.GetMyInfo(), lambda: getattr(wx, "myinfo", None)):
        try:
            got = src()
        except Exception:
            continue
        if isinstance(got, dict) and got:
            info = got
            break
    low = {str(k).strip().lower(): str(v).strip() for k, v in info.items() if v is not None}

    def pick(keys):
        return next((low[k] for k in keys if low.get(k) and low[k].lower() not in ("none", "null")), "")
    try:
        nick = pick(NICK_KEYS) or str(getattr(wx, "nickname", "") or "").strip()
    except Exception:
        nick = pick(NICK_KEYS)
    return {"wxid": pick(ID_KEYS) or _wxid_from_dir(wx), "nick": nick}


def who(i: dict | None) -> str:
    i = i or {}
    if i.get("nick") and i.get("wxid"):
        return f"昵称 {i['nick']}（微信号 {i['wxid']}）"
    if i.get("wxid"):
        return f"微信号 {i['wxid']}"
    return f"昵称 {i.get('nick', '')}（读不到微信号）"


def diff_tail(prev: list, cur: list) -> tuple[list, bool]:
    """轮询方式的核心：上次读到的这个聊天的最后几条 prev，和这次读到的 cur（都是 (发言人, 内容) 的列表，按时间顺序），
    找出 cur 里 prev 之后的新消息。做法：在 cur 里找 prev 的最长后缀（同样长度取最靠前的一处），它之后的就是新的。
    返回（新消息, 有没有衔接上）。完全衔接不上（新消息超过一屏、旧的都滚出去了）时只取最后 POLL_MAX_NEW 条，并标记没衔接上。"""
    if not prev:
        return [], True
    for m in range(min(len(prev), len(cur)), 0, -1):
        tail = prev[-m:]
        for i in range(0, len(cur) - m + 1):
            if cur[i:i + m] == tail:
                return cur[i + m:][-POLL_MAX_NEW:], True
    return cur[-POLL_MAX_NEW:], False


def new_since(prev: list, cur: list) -> list:
    return diff_tail(prev, cur)[0]


def session_sig(s) -> tuple:
    """会话列表里的一项「内容」变没变：最后一条的内容和时间（字段名因 wxauto4 版本而异，有哪个取哪个）。"""
    return tuple(str(getattr(s, k, "") or "") for k in ("content", "time"))


def session_unread(s) -> int:
    """会话列表里这一项的未读数（有的版本只有「有没有新消息」的标记）。读完切回别的会话后，没打开着的聊天来了新消息未读数就会大于 0；
    连发两条一模一样的消息、同一分钟内，内容和时间都不变，只有未读数能看出来。"""
    for k in ("new_count", "unread", "unread_count"):
        v = getattr(s, k, None)
        if v not in (None, "", False):
            return int(v) if str(v).isdigit() else 1
    return 1 if getattr(s, "isnew", False) is True else 0


def session_name(s) -> str:
    return str(getattr(s, "name", None) or getattr(s, "who", None) or "")


def judge(bound: dict | None, cur: dict) -> str:
    """现在登录的和绑定的是不是同一个。返回：
    first=还没绑定过；ok=一致；mismatch=不是同一个号；nick_changed=只靠昵称绑的而昵称变了；unreadable=什么都读不到。"""
    if not cur.get("wxid") and not cur.get("nick"):
        return "unreadable"
    if not bound:
        return "first"
    bid, cid = (bound.get("wxid") or "").strip().lower(), (cur.get("wxid") or "").strip().lower()
    if bid and cid:
        return "ok" if bid == cid else "mismatch"
    same_nick = (bound.get("nick") or "").strip() == (cur.get("nick") or "").strip()
    if bid:  # 绑的是微信号，这次读不到：只能拿昵称当凭证，对不上就算换号
        return "ok" if same_nick else "mismatch"
    return "ok" if same_nick else "nick_changed"


def pylib() -> Path:
    return data_dir() / "pylib"


def file_version(path: str) -> tuple[int, ...] | None:
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    ver.GetFileVersionInfoW(path, 0, size, buf)
    p, n = ctypes.c_void_p(), ctypes.c_uint()
    ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n))
    ffi = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
    ms, ls = ffi[2], ffi[3]
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def classify(v: tuple[int, ...]) -> dict:
    """微信版本 → {version, ok（在 wxauto4 声明的范围内）, level, note}。
    level：ok 在范围内 / verified 超出范围但我们实测过能用 / unsupported 超出范围又没验证过 / old 太旧。"""
    vs = ".".join(map(str, v))
    if SUPPORTED_MIN <= v <= SUPPORTED_MAX:
        return {"version": vs, "ok": True, "level": "ok", "note": "版本可用"}
    if v < SUPPORTED_MIN:
        return {"version": vs, "ok": False, "level": "old", "note": f"微信 {vs} 太旧，请升级到 4.1.8 版"}
    if any(v[:len(p)] == p for p in VERIFIED):
        return {"version": vs, "ok": False, "level": "verified",
                "note": f"微信 {vs} 超出 wxauto4 自己声明的适用范围（到 4.1.8），但我们在这个版本上实测能用（用「主窗口轮询」收消息）。建议关闭微信的自动更新，免得升到没验证过的版本"}
    return {"version": vs, "ok": False, "level": "unsupported",
            "note": f"微信 {vs} 超出 wxauto4 声明的适用范围（到 4.1.8），我们也没在这个版本上验证过，很可能连不上或界面一改就失灵。"
                    "建议退回已验证的 4.1.15（或 4.1.8），并关闭微信的自动更新"}


def wechat_exe() -> Path | None:
    import winreg
    for sub, exe in ((r"Software\Tencent\Weixin", "Weixin.exe"), (r"Software\Tencent\WeChat", "WeChat.exe")):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                p = Path(winreg.QueryValueEx(k, "InstallPath")[0]) / exe
                if p.exists():
                    return p
        except OSError:
            continue
    return None


def check_version() -> dict:
    """看本机微信版本对不对。返回 {installed, version, ok, level, note}。"""
    path = wechat_exe()
    if not path:
        return {"installed": False, "version": "", "ok": False, "level": "missing", "note": "没找到微信。请先安装微信 4.x（建议 4.1.8 版）"}
    return {"installed": True, "path": str(path), **classify(file_version(str(path)) or (0,))}


def lib_installed() -> bool:
    try:
        import importlib.util
        if importlib.util.find_spec(WX_LIB):
            return True
    except Exception:
        pass
    return (pylib() / WX_LIB).exists()


def pypi_info() -> dict:
    """PyPI 上 wxauto4 的最新信息（JSON）。联网走统一的 SSL 上下文；测试里换成桩。"""
    with tls.urlopen(f"https://pypi.org/pypi/{WX_LIB}/json", timeout=60) as r:
        return json.loads(r.read())


def ver_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s or ""))


def installed_lib_version() -> str:
    """装在数据目录里的 wxauto4 版本（没装返回空）；有好几个 dist-info 时取最新的。"""
    vs = [m.group(1) for d in pylib().glob(f"{WX_LIB}-*.dist-info") if (m := re.match(rf"{WX_LIB}-(.+)\.dist-info$", d.name))]
    return max(vs, key=ver_tuple) if vs else ""


def parse_declared(description: str) -> str:
    """wxauto4 的 PyPI 说明里「适用最新4.1.8」这类句子 → 它声明适用到的微信版本（如 4.1.8）；找不到返回空。"""
    m = re.search(r"适用(?:于)?(?:最新)?\s*(?:微信\s*)?(\d+(?:\.\d+){1,3})", description or "")
    return m.group(1) if m else ""


def _fetch_wheel(info: dict, dest: Path) -> Path:
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    url = next(u for u in info["urls"] if f"-{tag}-" in u["filename"] and u["filename"].endswith("win_amd64.whl"))
    dest.mkdir(parents=True, exist_ok=True)
    whl = dest / url["filename"]
    with tls.urlopen(url["url"], timeout=120) as r, open(whl, "wb") as f:
        shutil.copyfileobj(r, f)
    return whl


def download_lib() -> str:
    """从 PyPI 下载与本程序 Python 版本对应的 wxauto4 轮子，解压到数据目录。"""
    info = pypi_info()
    target = pylib()
    whl = _fetch_wheel(info, target)
    with zipfile.ZipFile(whl) as z:
        z.extractall(target)
    whl.unlink()
    return info["info"]["version"]


def upgrade_lib() -> dict:
    """升级 wxauto4：先下载到临时目录，再把旧的备份、换上新的；任何一步出错都回退到原来的版本。返回 {old, new}。
    调用前要先停掉微信账号（正在用的 wx.pyd 删不掉）。"""
    target, tmp, bak = pylib(), data_dir() / "pylib.new", data_dir() / "pylib.bak"
    old = installed_lib_version()
    for d in (tmp, bak):
        shutil.rmtree(d, ignore_errors=True)
    try:
        info = pypi_info()
        whl = _fetch_wheel(info, tmp)
        with zipfile.ZipFile(whl) as z:
            z.testzip()
            if target.exists():
                shutil.copytree(target, bak)
            for old_item in list(target.glob(WX_LIB)) + list(target.glob(f"{WX_LIB}-*.dist-info")):
                shutil.rmtree(old_item) if old_item.is_dir() else old_item.unlink()
            target.mkdir(parents=True, exist_ok=True)
            z.extractall(target)
        new = installed_lib_version()
        if not new or ver_tuple(new) != ver_tuple(info["info"]["version"]):
            raise RuntimeError(f"装好后读到的版本是「{new}」，和 PyPI 上的 {info['info']['version']} 对不上")
    except Exception:
        if bak.exists():  # 回退：把备份放回去
            for item in list(target.glob(WX_LIB)) + list(target.glob(f"{WX_LIB}-*.dist-info")):
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
            shutil.copytree(bak, target, dirs_exist_ok=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(bak, ignore_errors=True)
    return {"old": old, "new": new}


def lib_update_notice(installed: str, info: dict, wechat_version: str = "") -> dict | None:
    """PyPI 上的 wxauto4 比装的新 → 提示内容（给面板和通知用）；没有新版返回 None。"""
    latest = info["info"]["version"]
    if not installed or ver_tuple(latest) <= ver_tuple(installed):
        return None
    declared = parse_declared(info["info"].get("description", ""))
    text = f"wxauto4 有新版 {latest}（现在装的是 {installed}）"
    text += f"，它声明适用到微信 {declared}" if declared else "（没有写明适用的微信版本）"
    out = {"installed": installed, "latest": latest, "declared": declared, "wechat_up_to": ""}
    if declared and ver_tuple(declared) > SUPPORTED_MAX[:len(ver_tuple(declared))]:
        out["wechat_up_to"] = declared  # 新版覆盖更高的微信版本
        text += f"，可以把微信升到 {declared} 了（升级 wxauto4 之后）"
    elif declared and wechat_version and ver_tuple(wechat_version) > SUPPORTED_MAX and ver_tuple(wechat_version)[:len(ver_tuple(declared))] <= ver_tuple(declared):
        text += f"，能覆盖你现在的微信 {wechat_version}"
    out["text"] = text + "。到「账号 → 微信」点「升级 wxauto4」（升级前先停掉微信账号）"
    return out


# 微信界面里的品牌名：简体中文版叫「微信」，有的机器上（比如服务器上英文品牌的）叫「WeChat」。wxauto4 里写死按「微信」找控件，
# 所以在我们这一层让这几类控件的名字统一读成「微信」（不改 wxauto4 的发行文件）。只动这三类控件，避免把一个叫 WeChat 的联系人当成品牌名。
BRAND_NAMES = ("WeChat", "Weixin")
BRAND_CLASSES = ("mmui::XTabBarItem", "mmui::MainWindow", "Qt51514QWindowIcon")
BRAND = "微信"


def apply_brand_compat(ua) -> bool:
    """给 wxauto4 的 uiautomation 模块打兼容：导航栏的标签、主窗口的名字是「WeChat」/「Weixin」时，读出来按「微信」。
    wxauto4 里所有按名字找控件的地方（导航栏的「微信」按钮、主窗口……）读的都是这个 Name 属性，所以一处补上全部兼容。已经打过就不重复打。"""
    ctl = ua.Control
    if getattr(ctl, "_podmates_brand_compat", False):
        return False
    orig = ctl.Name

    def name(self):
        n = orig.fget(self)
        if n in BRAND_NAMES:
            try:
                if self.ClassName in BRAND_CLASSES:
                    return BRAND
            except Exception:
                pass
        return n
    ctl.Name = property(name, doc=orig.__doc__)
    ctl._podmates_brand_compat = True
    return True


def tab_names(ua, hwnd: int, limit: int = 3000) -> list[str]:
    """微信主窗口里导航栏标签（XTabBarItem）的实际名字，连不上时写进报错，方便排查是哪个名字对不上。出错返回空。"""
    out, seen = [], 0
    try:
        root = ua.ControlFromHandle(hwnd)
        stack = [root] if root else []
        while stack and seen < limit:
            c = stack.pop()
            seen += 1
            if c.ClassName == "mmui::XTabBarItem":
                out.append(c.Name or "(空名)")
                continue
            stack.extend(reversed(c.GetChildren()))
    except Exception:
        return out
    return out


def load_lib():
    p = str(pylib())
    if p not in sys.path:
        sys.path.append(p)
    import importlib
    lib = importlib.import_module(WX_LIB)
    try:
        apply_brand_compat(importlib.import_module(f"{WX_LIB}.uia.uiautomation"))
    except Exception:
        pass  # 打不上就按原样用（简体中文品牌名的微信不需要它）
    return lib


class IdentityChanged(Exception):
    """运行中发现现在登录的微信不是绑定的小号。"""

    def __init__(self, verdict: str, cur: dict, bound: dict | None):
        super().__init__(verdict)
        self.verdict, self.cur, self.bound = verdict, cur, bound


def _short(s, n: int = 20) -> str:
    return str(s or "").replace("\n", " ")[:n]


class WeChatChannel(Channel):
    platform = "wechat"
    supports_recall = False
    supports_image = True  # 微信发的是账号「自定义表情」里的一格（点表情面板），不是图片文件：send_image 要的是目录里的位置和指纹
    sticker_by_row = True
    lib = None  # 测试时换成模拟的 wxauto
    window_api = None  # 测试时换成模拟的窗口接口；真运行（lib 是真的 wxauto4）时才会用真的 Win32，测试里绝不会碰真窗口
    stk_ui = stk_hwnd = stk_grab = stk_sleep = None  # 测试时换成模拟的界面自动化、窗口句柄、截图、等待；没换且 lib 不是真的 wxauto4 时不操作表情面板

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.ex = ThreadPoolExecutor(1, thread_name_prefix="wechat")  # 界面操作一律排队在同一线程
        self.wx = None
        self.held_orig = ""  # 分条发送中：发之前微信开着的那个会话（最后一条发完再切回去）
        self.chats = {}
        self.ident = {}
        self.verified_at = 0.0
        self.recheck_secs, self.watch_secs = RECHECK_SECS, 30
        self.connect_retry_secs = 1.5
        self.idle_recheck_secs = IDLE_RECHECK_SECS  # 没有切会话 / 发送时，多久单独复核一次（会激活窗口，所以间隔长）
        self.main_hwnd = None
        self.keeper = None  # 微信窗口移出屏幕的管家（winpos.Keeper）
        self.keeper_on = False  # 身份核对通过、上线之后才开始移窗口
        self.bring_ms: list[float] = []  # 最近每次「临时移回」持续了多久（毫秒），报告里要写
        self.lib_real = False
        self.mode = "window"  # 收消息方式：window=独立窗口，poll=主窗口轮询
        self.sess_sig: dict[str, tuple] = {}  # 轮询：每个监听聊天上次的会话列表签名
        self.tails: dict[str, list] = {}  # 轮询：每个监听聊天上次读到的最后几条
        self.seen_ids: dict[str, list] = {}  # 轮询：每个监听聊天已经见过的消息 id
        self.poll_n = 0
        self.pics: dict[str, bytes] = {}  # 下载好、还没交给看图的图片：取件号 → 内容
        self.heard: dict[str, tuple] = {}  # 消息 id → ({发送人, 备注}, 内容)
        self.switch_ms: list[float] = []  # 轮询：切过去读一个聊天（含切回）花了多久（毫秒）
        self.switch_sleep = time.sleep  # 切过去后等界面就绪再读的那一下（测试里换成立刻返回）
        self.loop = None
        self._stk_drv = None
        self.ui_fails = 0  # 复核身份读不到界面连续几次；复核通过一次就清零
        self.poll_fails = 0  # 轮询出错连续几次；有一轮读通就清零（零星的出错不能隔几天攒到上限、把账号误停掉）
        self.conf = {c["name"]: c for c in account.get("listen", []) if c.get("name")}

    def _offscreen(self) -> bool:
        return bool(self.engine.settings.get("wechat_offscreen", platform="wechat"))

    async def _run(self, fn, *a, ui=True, guard=True):
        """微信界面操作一律排在同一个线程里。
        ui：开了「移到屏幕外」时，操作前把窗口移回来、做完立刻再移出去。
        guard：这个操作会不会把微信顶到前台（真机实测 wxauto4 的 ChatWith 切到别的会话、GetAllMessage 读消息都会，GetSession 不会）：
        会的话，做完把前台还给原来的窗口、把微信放回原来的 Z 序（微信原来就在前台的话不动）。"""
        k = self.keeper if self.keeper_on else None
        if k is None or not (ui or guard):
            return await self.loop.run_in_executor(self.ex, fn, *a)

        def go():
            k.api.begin()
            snap = k.snapshot() if guard else None
            if ui:
                if self._offscreen():
                    k.bring()
                elif k.off:  # 刚把开关关了：放回原位
                    k.restore()
            t0 = time.perf_counter()
            try:
                return fn(*a)
            finally:
                if ui and self._offscreen():
                    k.hide()
                if ui:
                    self.bring_ms = (self.bring_ms + [(time.perf_counter() - t0) * 1000])[-50:]
                k.settle(snap)
        return await self.loop.run_in_executor(self.ex, go)

    async def _park_back(self):
        """停止、换号被拦下、退出时：把窗口放回原位。"""
        k, self.keeper_on = self.keeper, False
        if k is not None:
            def back():
                k.api.begin()
                k.restore()
            await self.loop.run_in_executor(None, back)

    def _com(self):
        if threading.current_thread().name.startswith("wechat"):
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                pass

    def _diagnose(self) -> str:
        """连不上时，把微信窗口导航栏里实际有哪些标签写进报错（真实的 wxauto4 才查）。永远不抛错。"""
        try:
            if not self.lib_real:
                return ""
            import importlib
            ua = importlib.import_module(f"{WX_LIB}.uia.uiautomation")
            api = self.keeper.api if self.keeper is not None else WinApi()
            hwnd = api.find_main()
            tabs = tab_names(ua, hwnd) if hwnd else []
            if tabs:
                return f"。微信界面里实际找到的导航标签：{'、'.join(tabs)}（程序要找的是「{BRAND}」，也认「{'」「'.join(BRAND_NAMES)}」）"
            return "。没在微信窗口里找到导航栏的标签（窗口可能还没加载完，或者微信界面和预期的不一样）"
        except Exception:
            return ""

    def announce_version(self, v: dict):
        """连接微信时核对版本：不在 wxauto4 声明的范围内就写日志、面板总览挂一条提示，并私聊通知作者（每个版本只通知一次）。
        已验证能用的版本（超出声明范围但我们实测过）措辞不同，不说成「不支持」。"""
        db, lvl = self.engine.db, v.get("level")
        if lvl == "ok":
            db.del_kv("wechat_notice")
            return
        if lvl == "missing":
            self.engine.log("warn", "system", f"微信：{v['note']}")
            return
        self.engine.log("info" if lvl == "verified" else "warn", "system", f"微信：{v['note']}")
        db.set_kv("wechat_notice", {"version": v["version"], "level": lvl, "note": v["note"], "ts": time.time()})
        told = db.kv("wechat_ver_notified") or {}
        if v["version"] not in told:
            told[v["version"]] = time.time()
            db.set_kv("wechat_ver_notified", told)
            self.engine.notify(v["note"], self.account)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        lib = self.lib
        real = lib is None  # 真的 wxauto4（不是测试里换进来的模拟库）
        self.lib_real = real
        if lib is None:
            v = check_version()
            self.announce_version(v)
            if not v["installed"]:
                self.set_status("error", v["note"])
                return
            if not lib_installed():
                self.set_status("error", "还没下载微信自动化组件（wxauto4），请在账号页点「下载组件」")
                return
            lib = load_lib()
        self.set_status("starting", "正在连接微信窗口")
        self.mode = self._choose_mode()  # 连接前就定下来：轮询方式连接时不让 wxauto 改窗口大小
        api = self.window_api or (WinApi() if (real and sys.platform == "win32") else None)
        if api is not None:
            self.keeper = Keeper(api, data_dir() / "wechat_window.json", log=lambda lv, tx: self.engine.log(lv, "system", "微信：" + tx))
            try:  # 上次程序被强行结束、窗口留在屏幕外了：先放回来（窗口看得见时什么都不动）
                if (data_dir() / "wechat_window.json").exists():
                    await self.loop.run_in_executor(None, lambda: (api.begin(), recover(api, data_dir() / "wechat_window.json")))
            except Exception as e:
                self.engine.log("warn", "system", f"微信：恢复上次留在屏幕外的窗口时出错：{e}")
        try:
            ident = await self._run(self._connect, lib)
        except Exception as e:
            hint = await self.loop.run_in_executor(None, self._diagnose)
            self.set_status("error", f"连不上微信：{e}。请先打开并登录微信小号，窗口别关{hint}")
            return
        await self._decide(ident)

    @property
    def bind_key(self) -> str:
        return f"wxbind:{self.aid}"

    def _construct(self, lib):
        """连一次微信窗口，得到一个新的 wxauto 对象（构造时会读一遍当前登录的身份，之后 GetMyInfo 返回的是这时缓存的）。"""
        try:  # 主窗口轮询不需要 wxauto 把微信窗口改成它的标准大小（真机 4.1.15 上不改也能用）：别动用户的窗口大小
            return lib.WeChat(resize=False) if self.mode == "poll" else lib.WeChat()
        except TypeError:
            return lib.WeChat()

    def _connect(self, lib) -> dict:
        self._com()
        self._lib = lib
        last = None
        for i in range(3):  # 连接时偶尔界面还没就绪（真机上见过 COMError）：隔一会儿再试，最多三次
            try:
                self.wx = self._construct(lib)
                return read_identity(self.wx)
            except Exception as e:
                last = e
                self.wx = None
                time.sleep(self.connect_retry_secs)
        raise last

    def _verify_now(self):
        """在微信线程里重新读一遍「现在登录的是谁」。wxauto4 的 GetMyInfo() 返回的是连接时缓存的字典（真机上 0 毫秒），读不出换号，
        所以要重新连一次（构造时会真去读头像资料卡）。通过：（轮询方式）换用这个新连接；不通过：抛 IdentityChanged，不做后面的操作。
        读不出来（界面没就绪）不当作换号，下次再试；先试一次，读不到就把被藏起来的主窗口显示出来再试一次。"""
        self._com()
        try:
            new = self._construct(self._lib)
            cur = read_identity(new)
        except Exception as e:
            first = e
            if not self._unhide_and_retry():
                self._ui_trouble(f"微信：复核登录身份时没连上（{type(first).__name__}: {first}），稍后再试")
                return
            try:
                new = self._construct(self._lib)
                cur = read_identity(new)
            except Exception as again:
                self._ui_trouble(f"微信：复核登录身份时没连上（主窗口藏起来后显示出来了，还是连不上：{type(again).__name__}: {again}），稍后再试")
                return
        if not cur["wxid"] and not cur["nick"]:
            self._ui_trouble("微信：复核登录身份时没读到昵称和微信号，稍后再试")
            return
        self.ui_fails = 0
        bound = self.engine.db.kv(self.bind_key)
        v = judge(bound, cur)
        if v != "ok":
            raise IdentityChanged(v, cur, bound)
        self.verified_at = time.time()
        self._refresh_bound(bound, cur)
        if self.mode == "poll" and new is not self.wx:  # 轮询方式没有挂监听，直接换用新连接；独立窗口方式的监听挂在老连接上，不换
            old, self.wx = self.wx, new
            ex = getattr(old, "_excutor", None)
            if ex is not None:
                ex.shutdown(wait=False)

    def _unhide_and_retry(self) -> bool:
        """读不到界面时（微信线程里）：主窗口被藏进托盘或最小化着就把它显示出来（不抢前台焦点）。做了返回 True，可以让调用方马上重试一次。"""
        api = self.keeper.api if self.keeper is not None else None
        if api is None:
            return False
        try:
            if not unhide(api):
                return False
        except Exception:
            return False
        self.engine.log("warn", "system", "微信：微信主窗口被藏起来了（看不见），已经重新显示")
        return True

    def _ui_trouble(self, text: str, counter: str = "ui_fails"):
        """界面读不到：平时照旧写一条 warn 稍后再试（读一次界面就清零）；连续 UI_FAIL_MAX 次还读不到，就把账号状态写成出错并通知，
        面板上不再一直显示「在线」却收不到消息。复核身份（ui_fails）和轮询（poll_fails）各数各的、各自读通一次就清零。"""
        n = getattr(self, counter) + 1
        setattr(self, counter, n)
        self.engine.log("warn", "system", text)
        if n < UI_FAIL_MAX or self.status != "online":
            return
        setattr(self, counter, 0)
        why = text.split("（")[0].replace("微信：", "")
        self.set_status("error", f"连续 {n} 次读不到微信界面（{why}），已经停止收消息。请打开并登录微信小号，窗口别关；也可以点账号页微信卡片上的「找回微信窗口」")
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if in_loop or self.loop is None:
            self.engine.notify(self.detail, self.account)
        else:  # 在微信线程里（复核身份时）：这里没有事件循环，通知交回事件循环线程去发
            self.loop.call_soon_threadsafe(self.engine.notify, self.detail, self.account)

    def _verify_if_due(self):
        """紧挨着要切会话 / 发消息的那一刻顺带复核（这时窗口本来就要被激活，不额外抢焦点）：距上次核对超过 recheck_secs 才读。"""
        if time.time() - self.verified_at >= self.recheck_secs:
            self._verify_now()

    async def _decide(self, ident: dict):
        """连上窗口、读到身份之后：和绑定的核对，决定上线、等用户确认还是拒绝。"""
        self.ident = ident
        bound = self.engine.db.kv(self.bind_key)
        verdict = judge(bound, ident)
        if verdict == "ok":
            self._refresh_bound(bound, ident)
            return await self._go_online()
        if verdict in ("first", "nick_changed"):
            if verdict == "first":
                msg = f"检测到当前登录的微信是：{who(ident)}，这是机器人小号吗？确认了才会开始工作"
            else:
                old, new = bound.get("nick", ""), ident["nick"]
                msg = f"绑定的机器人小号昵称是「{old}」，现在登录的昵称是「{new}」，昵称变了就对不上了。请确认现在登录的还是机器人小号"
            hits = self.engine.people.wechat_hits(ident["wxid"], ident["nick"])  # 是不是人物表里某个人的号
            self.pending = {"nick": ident["nick"], "wxid": ident["wxid"], "reason": verdict, "old_nick": (bound or {}).get("nick", ""), "hits": hits}
            self.set_status("confirm", msg)
            warn = "。注意：" + "；".join(f"这是人物表里「{h['name']}」的号" + ("（管理员）" if h["admin"] else "") for h in hits) + "，要再确认一次才能绑定" if hits else ""
            self.engine.notify("微信账号要先在面板里确认身份：" + msg + warn, self.account)
            return
        await self._release()
        msg = self._refusal(verdict, ident, bound)
        self.set_status("error", msg)
        self.engine.notify(msg, self.account)

    @staticmethod
    def _refusal(verdict: str, cur: dict, bound: dict | None) -> str:
        if verdict == "unreadable":
            return "读不到当前登录微信的昵称和微信号，没法核对身份，已停止"
        if verdict == "nick_changed":
            old, new = (bound or {}).get("nick", ""), cur.get("nick", "")
            return f"微信小号的昵称由「{old}」变成了「{new}」，对不上了，已停止。请在面板里重新启动并确认身份"
        if verdict == "first":
            return "这个微信账号的绑定被清除了，已停止。请在面板里重新启动并确认身份"
        return f"当前登录的是 {who(cur)}，不是绑定的机器人小号（{who(bound)}），已停止"

    def _save_bound(self, b: dict):
        self.engine.db.set_kv(self.bind_key, {"wxid": b.get("wxid", ""), "nick": b.get("nick", ""), "at": b.get("at") or time.time()})

    def _refresh_bound(self, bound: dict, cur: dict):
        """身份一致时，顺手更新记下的昵称、补上之前读不到的微信号。"""
        nick, wxid = cur.get("nick") or bound.get("nick", ""), cur.get("wxid") or bound.get("wxid", "")
        if (nick, wxid) != (bound.get("nick", ""), bound.get("wxid", "")):
            self._save_bound({**bound, "nick": nick, "wxid": wxid})

    async def confirm_bind(self, ok: bool, again: bool = False) -> dict:
        """用户在面板里点了「是 / 否」。点「是」之前再读一次身份，防止确认的这会儿微信已经换了号。"""
        if self.status != "confirm" or not self.pending or self.wx is None:
            raise RuntimeError("现在没有等着确认的微信身份")
        if not ok:
            await self._release()
            self.set_status("stopped", "没有确认这是机器人小号，已取消启动")
            return {"ok": False}
        if self.pending.get("hits") and not again:
            raise RuntimeError("这是人物表里某个人的号，要再确认一次才能绑定")
        def fresh():  # 真的重新连一次读（GetMyInfo 返回的是连接时缓存的）；还没挂监听，直接换用新连接
            self._com()
            new = self._construct(self._lib)
            self.wx, old = new, self.wx
            return read_identity(new)
        cur = await self._run(fresh)
        if (cur["wxid"], cur["nick"]) != (self.ident["wxid"], self.ident["nick"]):
            await self._release()
            self.set_status("error", f"确认的时候发现登录的微信变了（现在是 {who(cur)}），已停止。请重新启动再确认")
            return {"ok": False, "changed": True}
        self._save_bound({"wxid": cur["wxid"], "nick": cur["nick"]})
        self.engine.log("info", "system", f"微信账号已绑定：{who(cur)}")
        await self._go_online()
        return {"ok": True}

    async def _release(self):
        wx, self.wx = self.wx, None
        self.pending = None
        if wx is not None:
            await self._unlisten(wx)
        await self._park_back()

    async def _unlisten(self, wx):
        for name in list(self.chats):
            try:
                await self._run(lambda n=name: wx.RemoveListenChat(n))
            except Exception:
                pass
        self.chats = {}

    async def _go_online(self):
        try:
            await self._run(self._attach)
        except Exception as e:
            self.set_status("error", f"连不上微信：{e}。请先打开并登录微信小号，窗口别关")
            return
        self.pending = None
        self.verified_at = time.time()
        if self.keeper is not None:  # 身份核对通过了，才开始移窗口
            self.keeper_on = True
            self.main_hwnd = self.keeper.api.find_main()
            await self._run(lambda: None)
        self.set_status("online", f"已连接 {self.self_name}")
        b = self.engine.bots.get(self.aid)
        if b:  # 上线时对一次自定义表情（没开「用账号收藏的表情」就什么都不做；十分钟内刚同步过的不重复）
            self.engine.spawn(self.engine.stickers.sync_favorites(b, min_age=600))
        asyncio.create_task(self._watch())
        if self.mode == "poll":
            asyncio.create_task(self._poll())

    def _choose_mode(self) -> str:
        """设置「微信收消息方式」：auto 时，微信版本超出 wxauto4 支持范围就用主窗口轮询，否则用独立窗口。"""
        mode = self.engine.settings.get("wechat_recv_mode", platform="wechat")
        if mode in ("window", "poll"):
            return mode
        if self.lib_real:
            v = check_version()
            if v["installed"] and not v["ok"]:
                return "poll"
        return "window"

    def _attach(self):
        self._com()
        self.self_name = self.ident.get("nick") or "我"
        self.self_id = self.self_name
        if self.mode == "window":
            try:
                for name in self.conf:
                    chat = self.wx.AddListenChat(name, self._callback)
                    self.chats[name] = chat if hasattr(chat, "SendMsg") else None
                if hasattr(self.wx, "StartListening"):
                    self.wx.StartListening()
            except Exception as e:  # 独立窗口这条路走不通（比如新版微信拆窗口报错）：撤掉已拆出来的，改用轮询
                self.engine.log("warn", "system", f"微信：独立窗口方式失败（{type(e).__name__}: {e}），改用主窗口轮询")
                for name in list(self.chats):
                    try:
                        self.wx.RemoveListenChat(name)
                    except Exception:
                        pass
                self.chats = {}
                self.mode = "poll"
        if self.mode == "poll":
            self.engine.log("info", "system", "微信：用主窗口轮询收消息（不拆独立窗口）")
        for name in self.conf:
            if self.conf[name].get("type", "group") == "group":
                self.loop.call_soon_threadsafe(self.engine.register_chat, self.aid, "wechat", name, name)

    # ---------- 主窗口轮询 ----------
    def _peek(self) -> dict[str, tuple]:
        """只读会话列表（不点、不切）：每个监听聊天现在的 (内容签名, 未读数)。"""
        self._com()
        out = {}
        for s in self.wx.GetSession() or []:
            n = session_name(s)
            if n in self.conf:
                out[n] = (session_sig(s), session_unread(s))
        return out

    # ---------- 读完消息之后：关掉微信自己弹出的头像小菜单 ----------
    def _menu_api(self):
        """关菜单用的窗口接口：真运行时是 keeper 的那一份，测试里是 window_api。都没有（没连过窗口）就不做。"""
        k = self.keeper
        return k.api if k is not None else self.window_api

    def _menu_owner(self, api, msgs=(), control=None, main: bool = False) -> int:
        """「消息所在的窗口」：主窗口方式用主窗口；独立窗口方式从这条消息的控件往上找（owner_hwnd）。
        独立窗口方式拿不到就不返回主窗口（不退回「微信进程的都算」），调用方这次不做。"""
        try:
            if control is not None:
                h = shot_mod.owner_hwnd(control, api)
                if h:
                    return int(h)
            if not main:
                for m in reversed(list(msgs)):
                    h = shot_mod.owner_hwnd(getattr(m, "control", None), api)
                    if h:
                        return int(h)
                return 0
            try:
                return int(api.find_main() or 0)
            except TypeError:  # 桩 / 老接口不认 visible_only
                return int(self.main_hwnd or 0)
        except Exception:
            return 0

    def _esc_menus(self, msgs=(), control=None, main: bool = False):
        """PodMates 自己刚读完消息：对微信自己弹出的头像小菜单发一次 Esc（见 wxshot.esc_menus，只对菜单、不发 WM_CLOSE）。
        这一步出任何错都只记日志，不影响读消息、发消息、截表情。"""
        try:
            api = self._menu_api()
            if api is None:
                return
            self._com()
            owner = self._menu_owner(api, msgs, control, main)
            if not owner:
                return
            rect = api.rect(owner)
            if not rect or rect[2] <= rect[0] or rect[3] <= rect[1]:
                return
            shot_mod.esc_menus(rect, api, owner, log=lambda lv, tx: self.engine.log(lv, "system", tx),
                               sleep=self.stk_sleep or time.sleep)
        except Exception as ex:
            key = ("esc-menus", type(ex).__name__, str(ex))
            if shot_mod._once(key):
                self.engine.log("warn", "system", f"微信：读完消息后关小菜单出错（{type(ex).__name__}: {ex}），不影响收发")

    def _opened(self) -> tuple[str, str]:
        """现在开着的会话：（名字, chat_type）。读不出来就是空名字；chat_type 有的版本不给，给了才有。"""
        try:
            info = self.wx.ChatInfo() or {}
        except Exception:
            return "", ""
        return str(info.get("chat_name") or ""), str(info.get("chat_type") or "")

    def _switch_exact(self, name: str, want: str = "") -> tuple[bool, str, str]:
        """精确切到 name，再用 ChatInfo() 核对「现在开着的就是它」。返回（核对通过没有, 实际开着的名字, chat_type）。

        主窗口方式发东西之前的唯一入口：wxauto4 的 SendMsg 默认按名字模糊搜索，名字相近的会话会被发错
        （2026-10-27 通知就发进了名字相近的群），所以先精确切、再核对、核对通过才发；发的时候不再把 who 交给它搜。
        want="private" 时还要确认开着的不是群（ChatInfo 给了 chat_type 才能判；没给就只按名字核对）。"""
        opened, typ = self._opened()
        if opened == name and not (want == "private" and typ == "group"):
            return True, opened, typ  # 已经开着的就是它：不用再切（分条发送时少切好几次）
        try:
            self.wx.ChatWith(name)  # exact 默认 True，不传 force
        except Exception:
            pass  # 切不过去会用下面读到的名字据实记账，不在这里当成「切过去了」
        opened, typ = "", ""
        for _ in range(5):  # 刚切过去时界面可能还没就绪，读不到标题：稍等一下再读几次
            opened, typ = self._opened()
            if opened == name:
                break
            self.switch_sleep(0.25)
        ok = opened == name and not (want == "private" and typ == "group")
        return ok, opened, typ

    def _target(self, chat, chat_id: str, chat_type: str) -> tuple[bool, str, str]:
        """要发东西之前：独立窗口方式下子窗口本来就绑着那个会话，照旧；主窗口方式精确切过去并核对。
        返回（能不能发, 现在开着的名字, chat_type）。"""
        if chat is not None:
            return True, chat_id, ""
        return self._switch_exact(chat_id, "private" if chat_type == "private" else "")

    def _not_exact(self, chat_type: str, chat_id: str, opened: str, typ: str, what: str = ""):
        """核对不上：不发，记一条 warn（要发给谁、实际开着的是谁），抛现有的「没发」那类错，让调用方知道没发出去。"""
        self.engine.log("warn", "system", f"微信：{what or '要发的'}是「{chat_id}」，现在开着的却是「{opened}」"
                        f"{'（这是个群）' if typ == 'group' else ''}，对不上，没有发")
        raise RuntimeError(f"没能切到「{chat_id}」（现在打开的是「{opened}」），没有发")

    def _read_chat(self, name: str) -> int:
        """切到这个聊天、读出最后几条、把新的交给引擎，然后切回原来的会话。返回新消息条数。"""
        self._com()
        self._verify_if_due()  # 到点了就顺带重新读一遍登录身份（不对就抛 IdentityChanged，不读）
        t0 = time.perf_counter()
        try:
            orig = (self.wx.ChatInfo() or {}).get("chat_name") or ""  # 主窗口一个会话都没打开时是 {}：没法切回，读完就停在读的那个会话
        except Exception:
            orig = ""
        try:
            self.wx.ChatWith(name)
            opened = ""
            for _ in range(5):  # 刚切过去时界面可能还没就绪，读不到标题：稍等一下再读几次
                opened = (self.wx.ChatInfo() or {}).get("chat_name") or ""
                if opened == name:
                    break
                time.sleep(0.25)
            if opened != name:  # 没切过去（比如窗口在屏幕外时点不到）：绝不能把别的聊天的消息当成这个群的
                raise RuntimeError(f"没能切到「{name}」（现在打开的是「{opened}」）")
            try:
                msgs = [m for m in (self.wx.GetAllMessage() or []) if getattr(m, "attr", "") in ("friend", "self", "other")]
            finally:  # 读完马上对微信自己弹出的头像小菜单发一次 Esc（主窗口方式：消息所在的窗口就是主窗口）
                self._esc_menus(main=True)
            t1 = time.perf_counter()
            n = self._ingest(name, msgs)  # 新消息在这个聊天还开着的时候交给引擎（图片要在聊天窗口里下载）
            ing = time.perf_counter() - t1
        finally:
            if orig and orig != name:
                try:
                    self.wx.ChatWith(orig)
                except Exception:
                    pass
        self.switch_ms = (self.switch_ms + [(time.perf_counter() - t0 - ing) * 1000])[-50:]
        return n

    def _ingest(self, name: str, msgs: list) -> int:
        """轮询：读到的最后几条里挑出新的，交给引擎。返回新消息条数。"""
        sigs = [(str(getattr(m, "sender", "") or ""), str(getattr(m, "content", "") or "")) for m in msgs][-POLL_TAIL:]
        msgs = msgs[-POLL_TAIL:]
        prev = self.tails.get(name)
        self.tails[name] = sigs
        seen = self.seen_ids.setdefault(name, [])
        ids = [str(getattr(m, "id", "") or "") for m in msgs]
        if prev is None:  # 第一次：只记下现有的（连 id 一起），不当新消息（不重放历史）
            seen.extend(i for i in ids if i and i not in seen)
            return 0
        fresh, matched = diff_tail(prev, sigs)
        base = len(sigs) - len(fresh)
        n = 0
        for i, sg in enumerate(fresh):
            m = msgs[base + i]
            mid = ids[base + i]
            if not matched and mid and mid in seen:  # 衔接不上（只能猜）时：真机上同一条消息的 id 在不同次读取里一样，id 见过的不是新消息，别把旧消息刷出来
                continue
            if getattr(m, "attr", "") == "self":  # 自己（机器人小号）发的不处理
                continue
            self.poll_n += 1
            self._handle(m, name, dedup=f"{name}|poll{self.poll_n}")
            n += 1
        seen.extend(i for i in ids if i and i not in seen)
        del seen[:-200]
        return n

    async def _read_visible_or_not(self, name: str):
        """读一个聊天。真机（微信 4.1.15.56）上窗口在屏幕外也能切会话、读消息，所以先不移窗口（不闪）；
        没切成功（切换后打开的不是这个聊天）就把窗口移回来再读一次。"""
        if self.keeper is not None and self.keeper_on and self._offscreen():
            try:
                return await self._run(self._read_chat, name, ui=False, guard=True)
            except Exception as e:
                self.engine.log("info", "system", f"微信：窗口在屏幕外时没读成（{e}），移回来再读")
        return await self._run(self._read_chat, name)

    async def _poll(self):
        """主窗口轮询：隔 wechat_poll 秒看一眼会话列表，哪个监听的聊天有变化就切过去读。"""
        first = True
        while self.wx and self.status == "online" and self.mode == "poll":
            try:
                sigs = await self._run(self._peek, ui=False, guard=False)
                for name, (sig, unread) in sigs.items():
                    # 有未读、内容/时间变了、还没读过：切过去读。存的是读之前看到的内容签名：读的时候又来了新消息，下一轮看出内容变了，会再读
                    if first or unread > 0 or self.sess_sig.get(name) != sig or name not in self.tails:
                        self.sess_sig[name] = sig
                        await self._read_visible_or_not(name)
                first = False
                self.poll_fails = 0  # 这一轮读通了：连续出错从头数
            except IdentityChanged as ic:
                await self._halt(ic)
                return
            except Exception as e:
                # 读不到界面：先看主窗口是不是被藏进托盘 / 最小化了，是就显示出来马上重试这一次（只重试一次）
                if await self._unhide_and_retry_async():
                    try:
                        for name, (sig, unread) in (await self._run(self._peek, ui=False, guard=False)).items():
                            if unread > 0 or self.sess_sig.get(name) != sig or name not in self.tails:
                                self.sess_sig[name] = sig
                                await self._read_visible_or_not(name)
                        self.poll_fails = 0
                    except Exception as again:
                        self._ui_trouble(f"微信：轮询出错（{type(again).__name__}: {again}）", "poll_fails")
                else:
                    self._ui_trouble(f"微信：轮询出错（{type(e).__name__}: {e}）", "poll_fails")
            await asyncio.sleep(max(0.2, float(self.engine.settings.get("wechat_poll", platform="wechat") or 1.0)))

    async def _unhide_and_retry_async(self) -> bool:
        """轮询线程里：主窗口被藏起来 / 最小化着就显示出来（排在微信线程里做，不抢前台焦点）。做了返回 True。"""
        if self.keeper is None:
            return False
        return await self._run(self._unhide_and_retry, ui=False, guard=False)

    async def _halt(self, ic: IdentityChanged):
        """发现换号：立刻撤掉监听、停止、发通知，窗口放回原位。"""
        msg = self._refusal(ic.verdict, ic.cur, ic.bound)
        wx, self.wx = self.wx, None
        self.set_status("error", msg)
        self.engine.log("warn", "system", "微信：" + msg)
        self.engine.notify(msg, self.account)
        if wx is not None:
            await self._unlisten(wx)
        await self._park_back()

    async def recheck(self) -> bool:
        """立刻重新读一遍现在登录的是谁（真的重新读，不是读缓存），和绑定的比。不是就立刻停止、发通知，返回 False。
        读不出来（界面正忙）不当作换了号。运行中平时不单独做这件事（会激活窗口），而是在要切会话 / 发送的那一刻顺带做。"""
        if self.wx is None or self.status != "online":
            return False
        try:
            await self._run(self._verify_now)
        except IdentityChanged as ic:
            await self._halt(ic)
            return False
        return True

    async def _watch(self):
        while self.wx and self.status == "online":
            await asyncio.sleep(self.watch_secs)
            try:
                if self.wx is None or self.status != "online":
                    return
                if hasattr(self.wx, "IsOnline") and not await self._run(self.wx.IsOnline, ui=False, guard=False):
                    self.set_status("error", "微信掉线了")
                    self.engine.notify(f"微信 {self.self_name} 掉线了", self.account)
                    return
                api = self.keeper.api if self.keeper is not None else None
                new_win = api is not None and self.main_hwnd and api.find_main() not in (None, self.main_hwnd)  # 微信主窗口换了：退出再登录过
                if (new_win or time.time() - self.verified_at >= self.idle_recheck_secs) and not await self.recheck():
                    return
                if new_win:
                    self.main_hwnd = api.find_main()
                    if self.lib is None:  # 微信退出又登录了：可能是升级过，再核对一次版本
                        self.announce_version(check_version())
            except Exception:
                pass

    def _callback(self, m, chat):
        """wxauto 监听线程里调用（独立窗口方式）：处理完这一条之后，对微信自己弹出的头像小菜单发一次 Esc。
        消息是 wxauto4 自己的监听线程读的，这里只有这一条；它所在的窗口从它的控件往上找（拿不到就这次不做）。"""
        self._handle(m, getattr(chat, "who", "") or "")
        self._esc_menus([m])

    def _handle(self, m, who: str, dedup: str = ""):
        """一条新消息（独立窗口方式在监听线程里、轮询方式在微信界面线程里调用）。"""
        if self.status != "online" or self.wx is None:  # 停了（包括发现换号）就不再处理消息
            return
        if getattr(m, "attr", "") not in ("friend", "other"):
            return
        conf = self.conf.get(who, {})
        group = conf.get("type", "group") == "group"
        mtype = getattr(m, "type", "text")
        text = str(getattr(m, "content", "") or "").replace("\u2005", " ")  # 微信 @ 人后面跟的是 U+2005 空格
        pic = is_picture(m) and (mtype != "image" or callable(getattr(m, "download", None)))  # 微信 4.1.15 把图显示成「图片」二字，wxauto4 归成 other，也算图（见 wxpicture.py）
        emo = is_emotion(m)  # 别人发的表情（type == "emotion"，或 other + 「动画表情」）：截聊天里那一块，看完写「[表情：描述]」，截不到写「[表情]」
        if mtype == "other" and pic:
            text = "[图片]"
        elif emo:
            text = "[表情]"
        elif mtype == "other" and text.strip():  # 真机上动画表情等归在 other 里，内容就是「动画表情」这样的说明文字
            pass
        elif mtype not in ("text", "quote"):
            text = {"image": "[图片]", "voice": "[语音]", "emotion": "[表情]", "file": "[文件]", "link": "[链接]"}.get(mtype, f"[{mtype}]")
        sender = getattr(m, "sender_remark", None) or getattr(m, "sender", "") or who
        at = f"@{self.self_name}"
        msg = Msg("wechat", self.aid, "group" if group else "private", who, sender if group else who, sender,
                  text.replace(at, "").strip(), msg_id=str(getattr(m, "id", "") or ""), chat_name=who, at_ids=[self.self_id] if at in text else [],
                  reply_to_name=str(getattr(m, "quote_nickname", "") or "") if mtype == "quote" else "",  # wxauto4 自己从「引用 某某 的消息 : …」里拆出来的被引用人
                  dedup=dedup or f"{who}|{sender}|{getattr(m, 'id', '') or text}")
        mid = str(getattr(m, "id", "") or "")
        if mid:  # 记下发送人和内容：以后引用时按 id 找不到，可以按这两样找
            self.heard[mid] = ({str(getattr(m, "sender", "") or ""), str(getattr(m, "sender_remark", "") or "")} - {""}, str(getattr(m, "content", "") or ""))
            while len(self.heard) > HEARD_MAX:
                self.heard.pop(next(iter(self.heard)))
        if (pic or emo) and self._wants_pictures(who, group, emo):
            if threading.current_thread().name.startswith("wechat"):  # 轮询方式：聊天开着，下载 / 截图放到单独线程里，最多等 25 秒
                self._keep_picture(msg, self._shot_with_timeout(m) if emo else self._download_with_timeout(m), emo)
            else:  # 独立窗口方式：在监听线程里，下载 / 截图排到微信线程里和收发消息一起排队
                asyncio.run_coroutine_threadsafe(self._download_then_deliver(m, msg, emo), self.loop)
                return
        asyncio.run_coroutine_threadsafe(self.engine.on_message(msg), self.loop)

    def _wants_pictures(self, who: str, group: bool, emotion: bool = False) -> bool:
        """看图开着、配了看图模型才下载图片（和引擎里 describe_images 的条件一样）；关着就照旧只写「[图片]」。
        表情还要「群友发的表情也看」开着，关着就不截屏、只写「[表情]」。"""
        g = self.engine.settings.get
        chat = f"wechat:{who}" if group else None
        return bool(g("vision_enabled", platform="wechat", chat=chat) and g("vis_model", platform="wechat", chat=chat)
                    and (not emotion or g("vision_stickers", platform="wechat", chat=chat)))

    async def _download_then_deliver(self, m, msg, emo: bool = False):
        what = "表情" if emo else "图片"
        try:
            await asyncio.wait_for(self._run(self._attach_picture, m, msg, emo), PIC_TIMEOUT)
        except Exception as ex:  # 超时、微信线程出错：这张照旧写「[图片]」/「[表情]」，不重试
            how = "截取" if emo else "下载"
            self.engine.log("warn", "system", f"微信：{how}{what}没成（{type(ex).__name__}: {ex}），这张按「[{what}]」处理")
        await self.engine.on_message(msg)

    def _shot_with_timeout(self, m):
        """轮询方式截表情：和下载图片一样放到单独的线程里跑，最多等 PIC_TIMEOUT 秒；超时就不再等，这张按「[表情]」处理。
        返回 Shot（见 _shot_emotion），截不到返回 None。"""
        box: dict = {}

        def work():
            box["shot"] = self._shot_emotion(m)

        t = threading.Thread(target=work, name="wechat-pic", daemon=True)
        t.start()
        t.join(PIC_TIMEOUT)
        if t.is_alive():
            self.engine.log("warn", "system", f"微信：截取表情超过 {PIC_TIMEOUT} 秒，这张按「[表情]」处理")
            return None
        return box.get("shot")

    def _download_with_timeout(self, m) -> bytes:
        """轮询方式下载图片。wxauto4 的 ImageMessage.download() 没有超时参数，所以放到单独的线程里跑，这里最多等 PIC_TIMEOUT 秒；
        超时就不再等，这张按「[图片]」处理（晚到的结果直接丢掉，不会改已经交出去的消息），不会把收消息的轮询卡死超过 25 秒。"""
        box: dict = {}

        def work():
            box["data"] = self._fetch_picture(m)

        t = threading.Thread(target=work, name="wechat-pic", daemon=True)
        t.start()
        t.join(PIC_TIMEOUT)
        if t.is_alive():
            self.engine.log("warn", "system", f"微信：下载图片超过 {PIC_TIMEOUT} 秒，这张按「[图片]」处理")
            return b""
        return box.get("data") or b""

    def _fetch_picture(self, m) -> bytes:
        """下载这张图：先 A（ImageMessage.download），不成再 B（右键「复制」读剪贴板），见 wxpicture.fetch；读进内存后立刻删掉下载下来的文件。
        成功记 info（写明 A 还是 B），都不成记 warn 并返回空。
        独立窗口方式在微信线程里调；轮询方式在单独的下载线程里调（线程名也以 wechat 开头，_com() 会初始化 COM）。"""
        self._com()
        return fetch_picture(m, lambda level, text: self.engine.log(level, "system", text))

    def _keep_picture(self, msg, data, emo: bool = False, key: str = "", moving: bool = False):
        """把下载好 / 截好的图交给引擎的看图流程（msg.images 里放一个取件号，fetch_image 凭它取），文字清空；
        表情另记在 msg.emotes 里（写「[表情：…]」用），截屏来的还记在 msg.shots 里（看图缓存按 dhash 记）。
        data 可以是 Shot（截屏来的，key / moving / data 都在里面）或字节串（下载来的）。"""
        if data is None:
            return
        if isinstance(data, bytes):
            data_bytes = data
        else:
            data_bytes, key, moving = data.data, data.key, bool(data.emotion.get("moving"))
        if not data_bytes:
            return
        token = f"wxpic:{uuid.uuid4().hex[:12]}"
        self.pics[token] = data_bytes
        while len(self.pics) > PIC_KEEP:
            self.pics.pop(next(iter(self.pics)))
        msg.images, msg.text = [token], ""
        if emo:
            msg.emotes, msg.shots = [token], [token]
            if key:
                msg.shot_keys, msg.moving = {token: key}, moving

    def _attach_picture(self, m, msg, emo: bool = False):
        """在微信线程里下载这张图（表情是截屏）并挂到 msg 上（独立窗口方式用）。失败、没下到只记日志，文字照旧是「[图片]」/「[表情]」。"""
        self._keep_picture(msg, self._shot_emotion(m) if emo else self._fetch_picture(m), emo)

    def _shot_emotion(self, m):
        """截聊天里这条表情所在的那一块（别人发的表情）：窗口开着「移到屏幕外」时先移回来、截完再移出去（和 _run(ui=True) 的做法一样）。
        截之前确认那块确实是微信（没被别的窗口盖住）、够大、在显示器里，截完裁到表情本身、连截三帧（见 wxshot）。
        截不到、不准截、裁不出来都记一条 warn 并返回空（这张不再重试）。
        返回 Shot：data 是交给看图的 PNG（动图是拼好的那张），key 是第一帧裁出来的 dhash，moving 记在 emotion 里。
        独立窗口方式在微信线程里调；轮询方式在单独的下载线程里调（和下载图片一样）。"""
        if self.wx is None:
            return b""
        self._com()
        k = self.keeper
        api = k.api if k is not None else (self.window_api or WinApi())
        api.begin()
        # 只有窗口这会儿真在屏幕外、这里把它移回来了，截完才移出去（已经在屏幕上的——比如外层 _run 刚移回来——不动它）
        moved = bool(k is not None and self._offscreen() and k.off and k.bring())
        if moved:
            time.sleep(SETTLE)  # 刚移回来：等界面画好再截，免得截到空白
        try:
            shot_mod.scroll_in(m)
            snap = k.snapshot() if k is not None else None
            try:
                shot = shot_mod.capture(m.control, api, self.stk_grab or shot_mod.default_grab, api.monitors(),
                                        sleep=self.stk_sleep or time.sleep)
            finally:
                if k is not None:
                    k.settle(snap)
        except shot_mod.ShotError as ex:
            self.engine.log("warn", "system", f"微信：这张表情截不了（{ex}），按「[表情]」处理")
            return b""
        except Exception as ex:
            self.engine.log("warn", "system", f"微信：截取表情出错（{type(ex).__name__}: {ex}），按「[表情]」处理")
            return b""
        finally:
            if moved:
                k.hide()
        self.engine.log("info", "system", f"微信：表情已截屏（{len(shot.data)} 字节，"
                        f"{'动图，三帧拼成一张' if shot.emotion.get('moving') else '静图'}，dhash {shot.key}），交给看图")
        return shot

    async def fetch_image(self, url):
        return self.pics.pop(url, None)

    supports_more = True
    supports_mention = True

    def _find_message(self, chat, chat_id: str, reply_to: str):
        """在聊天窗口里找要引用的那条消息：先按 wxauto 给的消息 id；id 对不上（切走再切回来，界面元素重建，id 可能变）
        就按「发送人 + 内容」在读到的消息里找，正好对上一条才用。找不到返回 None，日志写够下次定位原因的信息。"""
        heard = self.heard.get(str(reply_to))
        who, text = heard if heard else (set(), "")
        opened, err, msgs = "", "", []
        try:
            if chat:
                try:
                    msgs = chat.GetAllMessage() or []
                finally:  # 独立窗口方式：消息所在的窗口从这个聊天窗口里的消息控件往上找
                    self._esc_menus(msgs)
            else:
                # 主窗口方式：先精确切到那个会话、核对「开着的就是它」（发完后面照旧切回原来的会话）。
                # 核对不上就不在这个会话里找消息，按「找不到要引用的那条」处理（免得在别的会话里翻）。
                ok, opened, typ = self._switch_exact(chat_id)
                if not ok:
                    self.engine.log("warn", "system", f"微信：引用回复前没切到「{chat_id}」（现在开着的却是「{opened}」"
                                    f"{'（这是个群）' if typ == 'group' else ''}），不在那里找消息，按找不到要引用的那条处理")
                    return None
                try:
                    msgs = self.wx.GetAllMessage() or []
                finally:  # 主窗口方式：消息所在的窗口就是主窗口
                    self._esc_menus(msgs, main=True)
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
        quotable = [m for m in msgs if callable(getattr(m, "quote", None))]
        for m in reversed(quotable):
            if str(getattr(m, "id", "") or "") == str(reply_to):
                self.engine.log("info", "system", f"微信：引用按 id 找到了要回的那条（{chat_id}，id {reply_to}）")
                return m
        hits = [m for m in reversed(quotable) if text and str(getattr(m, "content", "") or "") == text
                and ({str(getattr(m, "sender", "") or ""), str(getattr(m, "sender_remark", "") or "")} - {""}) & who]
        if len(hits) == 1:
            self.engine.log("info", "system", f"微信：引用按内容找到了要回的那条（{chat_id}，id {reply_to} 没对上，发送人「{_short(sorted(who)[0] if who else '')}」，内容「{_short(text)}」）")
            return hits[0]
        tail = "；".join(f"[{str(getattr(m, 'id', '') or '')[:24]}|{_short(getattr(m, 'sender', ''), 10)}|{_short(getattr(m, 'content', ''), 12)}]"
                        for m in msgs[-5:])
        self.engine.log("info", "system", f"微信：要引用的那条消息在聊天窗口里找不到（{chat_id}），按普通方式发。要找的 id {reply_to}，"
                        f"发送人「{_short('/'.join(sorted(who)), 20)}」{'' if heard else '（没记到这条）'}，内容「{_short(text)}」；"
                        f"按内容对上 {len(hits)} 条；读到 {len(msgs)} 条{f'，读取出错 {err}' if err else ''}"
                        f"{f'，当前打开的是「{opened}」' if opened and opened != chat_id else ''}；最后 5 条：{tail or '（无）'}")
        return None

    async def send(self, chat_type, chat_id, text, reply_to=None, more=False, mention=None):
        """more：这是一次回复分条里不是最后一条的那条——主窗口方式下先别切回原来的会话（记下原会话，最后一条发完再切回），
        6 条不用来回切 12 次。中途出错没切回的，下一次发送会接着把它切回去。
        mention：[(账号, 名字)]，用微信自己的 @（SendMsg / quote 的 at 参数）；文字里原来那段「@名字」去掉。
        reply_to：要引用的那条消息的 id（引用回复）；在聊天窗口里找不到就按普通方式发，日志记一笔。"""
        if self.wx is None or self.status != "online":
            raise RuntimeError("微信账号已停止（身份没核对通过或已掉线），没有发送")
        names, body = [], text
        for _, n in mention or []:  # 微信的 @ 只能在消息开头：只转换写在行首的「@名字」（去掉文字里那段），句中的保持原样
            body, k = re.subn(r"(?m)^([ \t]*)@" + re.escape(n) + r"[ \t\u2005]*", r"\1", body, count=1)
            if k and n not in names:
                names.append(n)
        if names:
            body = body.strip() or " "

        def go():
            self._com()
            self._verify_if_due()  # 发送本来就要激活窗口：到点了就在这一刻顺带重新读一遍登录身份，不对就不发
            chat = self.chats.get(chat_id)
            orig = self.held_orig if not chat else ""
            if not chat and not orig:  # 主窗口里发：切到目标聊天再发，发完切回原来的会话
                try:
                    orig = (self.wx.ChatInfo() or {}).get("chat_name") or ""
                except Exception:
                    orig = ""
            self.held_orig = orig if more else ""
            try:
                target = self._find_message(chat, chat_id, reply_to) if reply_to else None
                # 发之前确认「现在开着的就是它」（独立窗口方式的子窗口本来就绑着这个会话；主窗口方式精确切过去再核对）：
                # 对不上就不发。核对通过以后发的时候不再把 who 交给 SendMsg 去模糊搜索，就发给现在开着的这个会话。
                ok, opened, typ = self._target(chat, chat_id, chat_type)
                if not ok:
                    self._not_exact(chat_type, chat_id, opened, typ)
                r = None
                if target is not None:
                    try:
                        r = target.quote(body, at=names or None)
                    except Exception as ex:
                        self.engine.log("warn", "system", f"微信：引用回复没成功（{type(ex).__name__}: {ex}），按普通方式发")
                        r = None
                    if r is not None and not getattr(r, "is_success", True):
                        r = None
                if r is None:
                    kw = {"at": names} if names else {}
                    try:
                        r = chat.SendMsg(body, **kw) if chat else self.wx.SendMsg(body, **kw)
                    except TypeError:  # 这个版本的 wxauto 不认 at：退回去，@ 留作文字
                        r = chat.SendMsg(text) if chat else self.wx.SendMsg(text)
            finally:
                if orig and orig != chat_id and not more:
                    try:
                        self.wx.ChatWith(orig)
                    except Exception:
                        pass
            if r is not None and not getattr(r, "is_success", True) and not (isinstance(r, dict) and r.get("status")):
                raise RuntimeError(f"微信发送失败：{r}")
        try:
            await self._run(go)
        except IdentityChanged as ic:
            await self._halt(ic)
            raise RuntimeError("微信账号登录的不是绑定的小号，已停止，没有发送")
        return [""]

    # ---------- 自定义表情（见 wxsticker.py）----------
    def _stk(self) -> WxStickers:
        if self._stk_drv is None:
            if self.stk_ui is None and not self.lib_real:
                raise RuntimeError("这个微信是模拟的，没有真实的界面可操作")
            import importlib
            ui = self.stk_ui or importlib.import_module(f"{WX_LIB}.uia.uiautomation")
            hwnd = self.stk_hwnd or (self.keeper.api if self.keeper is not None else self.window_api or WinApi()).find_main
            kw = {k: v for k, v in (("grab", self.stk_grab), ("sleep", self.stk_sleep), ("api", self._menu_api())) if v}
            self._stk_drv = WxStickers(ui, hwnd, log=self._stk_log, debug=lambda t: self.engine.log("debug", "sticker", "微信：" + t), **kw)
        return self._stk_drv

    def _stk_log(self, text: str):
        """表情面板这一步的日志（一行字）；关小菜单那一步的 warn 也走这里。"""
        level = "warn" if text.startswith("warn：") else "info"
        self.engine.log(level, "sticker", "微信：" + text.removeprefix("warn："))

    def _orig_chat(self) -> str:
        """要切走之前开着的会话（分条发送中记下的优先）；用完 held_orig 清空。"""
        orig = self.held_orig
        if not orig:
            try:
                orig = (self.wx.ChatInfo() or {}).get("chat_name") or ""
            except Exception:
                orig = ""
        self.held_orig = ""
        return orig

    async def _stk_run(self, fn):
        if self.wx is None or self.status != "online":
            raise RuntimeError("微信账号已停止（身份没核对通过或已掉线），没有操作表情面板")
        self._stk()  # 先确认能操作表情面板（模拟的微信不行）：不行就在这里报错，不要先把微信窗口移回来
        try:
            return await self._run(fn)
        except IdentityChanged as ic:
            await self._halt(ic)
            raise RuntimeError("微信账号登录的不是绑定的小号，已停止，没有操作表情面板")

    async def fav_scan(self, known: list, budget: int) -> dict:
        """建「自定义表情」的目录：在「文件传输助手」里打开表情面板一屏一屏翻着看（不点任何格子）。known：[(键, 指纹列表)]；
        返回 wxsticker.Panel.catalog 的结果（新表情带放大预览的截图，最多 budget 张）。"""
        def go():
            self._com()
            self._verify_if_due()
            return self._stk().catalog(self.wx, known, budget, CHAT_TARGET, back_to=self._orig_chat())
        return await self._stk_run(go)

    async def send_image(self, chat_type, chat_id, data, reply_to=None, sticker=None):
        """发一张自定义表情：精确切到这个会话并核对「开着的就是它」（wxsticker._open_chat 的那套核对），再在表情面板里翻到
        sticker（目录里记的位置和指纹）那一格，指纹对上了才点；发完切回原来的会话。
        sticker 是 dict，找到后更新里面的位置、补上新指纹（由调用方存回目录）。找不到会抛错（不点任何格子）；点了但没确认发出只记日志，不重试。"""
        def go():
            self._com()
            self._verify_if_due()
            return self._stk().send(self.wx, sticker, chat_id, back_to=self._orig_chat(),
                                    want="private" if chat_type == "private" else "")
        res = await self._stk_run(go)
        if not res["found"]:
            raise RuntimeError("在「自定义表情」里翻到底也没找到这一张（指纹对不上），没有发")
        sticker.update(pos=res["pos"], ticks=res["ticks"], fps=res["fps"])
        return [""]

    async def stop(self):
        await self._release()
        self.ex.shutdown(wait=False)
