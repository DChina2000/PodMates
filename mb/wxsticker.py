"""微信「自定义表情」：建目录（翻着看、记画面指纹）和发（翻到那一格、比对指纹、点）。

wxauto4 没有发表情的接口，这里直接操作微信的表情面板（界面结构见 wxprobe.py 的探查）：
  按钮「发送表情(Alt+E)」→ 弹出窗口 mmui::EmoticonPopover → 底部「自定义表情」那一栏 → 格子 mmui::FavEmoticonItemView
  （格子的名字全是「自定义表情」，没有各自的名字，只有位置；格子里的图 72×72，一行 5 格；外层是虚拟列表，只有看得到的格子存在）。

- 建目录：从头往下翻，每翻到一屏隔约 0.3 秒把整屏截 3 次，每一格从 3 张里各裁一次算画面指纹（stickers.dhash）。
  一张表情存一组指纹（3 次一样就只有一个；缩略图会动的存几个不同的）。第几格靠相邻两屏的重叠对出来。
  新出现的格子：鼠标移上去、等放大预览出来再截预览图（给看图模型写描述用），然后把鼠标移开。全程不点任何格子。
- 发：切到目标会话 → 打开面板 → 切到「自定义表情」→ 按记下的位置翻到那一格附近 → 截图比对指纹，对上了才点；
  对不上就从头找一遍；翻到底也没有就不点。点完读聊天里最后几条，看有没有多出一条自己发的表情。
- 挡板：只允许点「发送表情(Alt+E)」「自定义表情」和指纹已经确认的那一格（建目录时只许前两个）；别的一律拒绝。
- 翻页用滚轮（控件的 WheelDown/WheelUp）；截图在本机做，不调模型。

`PodMates.exe --test-wechat-sticker <输出目录>`（PodMates 主程序没在运行时）在服务器上验证：只在「文件传输助手」里建一遍目录、
挑第一格发出去、记下是否确认发出；每一步写进 probe.log 和 result.json（含每一格 3 次截图的指纹），截图存在输出目录。
不取单实例互斥体，不启动引擎，不碰 NapCat 和数据库。
"""
import io
import json
import time
from collections import Counter
from pathlib import Path

from .stickers import NEAR, dhash, hamming
from .wxprobe import CHAT_TARGET, EMOJI_BUTTON, SINGLE_TAB, SafeWx, find_exact, near_names, popup_roots, rect_of, walk

POPOVER = "mmui::EmoticonPopover"
GRID = "mmui::EmoticonGridView"
CELL = "mmui::FavEmoticonItemView"
PREVIEW = "mmui::PreviewEmoticonView"
SHOTS = 3  # 每屏截几次
SHOT_GAP = 0.3  # 两次截图的间隔（秒）
SETTLE = 0.4  # 翻页、打开面板之后等界面稳下来
STEP_TICKS = 2  # 建目录时每次往下翻几格滚轮
TICK_CHUNK = 30  # 一次最多滚几格（回到顶部、翻到记下的位置时分段滚）
INSET = 8  # 格子 88×88，里面的图 72×72：每边去掉 8 像素再算指纹
MAX_SCREENS = 400
GROUP_MAX = 8  # 一张表情最多记几个指纹
MATCH = 0.7  # 相邻两屏重叠的格子里，指纹对得上的要占这么多才算接上
TEST_ALLOWED = ("ChatInfo", "ChatWith", "GetAllMessage")


class PanelError(RuntimeError):
    """表情面板这一步没走成（找不到控件、翻页接不上……）。"""


class WxForTest(SafeWx):
    """验证命令用的微信包装：只放行读当前会话、切会话、读聊天里的消息；发消息、发文件、监听……一概不让碰。"""
    ALLOWED = TEST_ALLOWED


def default_grab(rect):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=rect, all_screens=True)


def fp_of(im) -> int | None:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return dhash(buf.getvalue())


def dist(a: list[int], b: list[int]) -> int:
    """两组指纹之间最近的一对相差几位；有一组是空的返回很大的数。"""
    return min((hamming(x, y) for x in a for y in b), default=64)


def close(a: list[int], b: list[int]) -> bool:
    return dist(a, b) <= NEAR


def merge_fps(group: list[int], more) -> list[int]:
    """把新见到的指纹补进一组（已有的不重复；超过上限留第一个和最近的）。"""
    out = list(group)
    for f in more:
        if f is not None and f not in out:
            out.append(f)
    return out if len(out) <= GROUP_MAX else out[:1] + out[-(GROUP_MAX - 1):]


class Guard:
    """点击的挡板：只放行「发送表情(Alt+E)」按钮、「自定义表情」那一栏，和（发表情时）指纹已经确认的那一格。"""

    def __init__(self, log):
        self.log = log
        self.clicked: list[str] = []
        self.cell = None  # 指纹确认过的那一格（发表情时才有）

    def allow_cell(self, c):
        self.cell = c

    def click(self, c, purpose: str):
        name = str(getattr(c, "Name", "") or "")
        typ, cls = str(getattr(c, "ControlTypeName", "") or ""), str(getattr(c, "ClassName", "") or "")
        if purpose == "open-panel":
            ok = name in EMOJI_BUTTON and typ == "ButtonControl"
        elif purpose == "single-tab":
            ok = name in SINGLE_TAB and typ == "TabItemControl"
        elif purpose == "cell":
            ok = self.cell is not None and c is self.cell and cls == CELL
        else:
            ok = False
        if not ok:
            raise PermissionError(f"拒绝点击「{name}」（{purpose}，{typ}/{cls}）：只允许点 发送表情(Alt+E)、自定义表情 和指纹已经确认的那一格")
        self.log(f"点击 {purpose}：「{name}」")
        c.Click()
        self.clicked.append(purpose)


def find_class(root, cls: str):
    for _, c in walk(root):
        try:
            if str(c.ClassName or "") == cls:
                return c
        except Exception:
            continue
    return None


class Panel:
    """一次表情面板操作：打开、读格子、翻页、建目录、发。用完调 close()。"""

    def __init__(self, ui, main, grab, sleep, log, guard):
        self.ui, self.main, self.grab, self.sleep, self.log, self.guard = ui, main, grab, sleep, log, guard
        self.pop = self.grid = self.tab = None
        self.opened = False
        self.wheel_api = True
        self.screen_no = 0
        self.hook = None  # 验证命令用：每截一张图调一次 hook(第几屏, 第几次, 图片)
        self.seq: list[dict] = []  # 翻过的所有格子（按位置）
        self.rect = None

    # ---------- 打开 ----------
    def open(self):
        btn = find_exact(self.main, EMOJI_BUTTON, "ButtonControl")
        if btn is None:
            raise PanelError(f"找不到名字叫「发送表情(Alt+E)」的按钮；名字里带「表情」的有：{'、'.join(near_names(self.main, ('表情',)))}")
        self.guard.click(btn, "open-panel")
        self.opened = True
        for _ in range(6):  # 弹出窗口出来要一会儿
            self.sleep(0.5)
            self.pop = self._popover()
            if self.pop is not None:
                break
        if self.pop is None:
            raise PanelError("点了「发送表情(Alt+E)」之后没等到表情面板的弹出窗口")
        self.tab = find_exact(self.pop, SINGLE_TAB, "TabItemControl")
        if self.tab is None:
            raise PanelError(f"在表情面板里找不到名字叫「自定义表情」的那一栏；带这几个字的有：{'、'.join(near_names(self.pop, ('自定义', '收藏', '默认', '搜索', '表情')))}")
        self.guard.click(self.tab, "single-tab")
        for _ in range(6):
            self.sleep(0.5)
            self.grid = find_class(self.pop, GRID)
            if self.grid is not None and self.rect_ok():
                break
        if self.grid is None or not self.rect_ok():
            raise PanelError("切到「自定义表情」之后找不到格子列表（mmui::EmoticonGridView）")
        self.park()

    def rect_ok(self) -> bool:
        self.rect = rect_of(self.grid)
        return bool(self.rect and self.rect[2] > self.rect[0] and self.rect[3] > self.rect[1])

    def _popover(self):
        roots = popup_roots(self.ui, self.main)
        for _, c in roots:
            if str(getattr(c, "ClassName", "") or "") == POPOVER:
                return c
        for _, c in roots:
            if find_exact(c, SINGLE_TAB, "TabItemControl") is not None:
                return c
        return None

    def still_open(self) -> bool:
        """表情面板的弹出窗口现在还在不在。读不出来就当它已经关了：宁可留着面板，也不多按一次 Esc
        （面板已经关了还按 Esc，这一下会落到微信主窗口上，微信会把它藏进托盘）。"""
        return self._popover() is not None

    def close(self):
        if not self.opened:
            return
        self.opened = False
        if not self.still_open():  # 点了格子以后微信自己就把面板关掉了
            self.log("表情面板已自己关上，不用按 Esc")
            return
        try:
            self.ui.SendKeys("{Esc}")
            self.log("已按 Esc 关掉表情面板")
        except Exception as ex:
            self.log(f"按 Esc 没成功：{type(ex).__name__}: {ex}")

    def park(self):
        """把鼠标挪到「自定义表情」那一栏上（不点）：不停在格子上，免得弹出放大预览盖住别的格子。"""
        r = rect_of(self.tab) if self.tab is not None else None
        if r:
            try:
                self.ui.SetCursorPos((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
            except Exception:
                pass

    # ---------- 读一屏 ----------
    def cells(self) -> tuple[list[dict], int]:
        """现在看得到的、完整的格子（按行、行内从左到右），和一行几格。"""
        gr = self.rect
        found = []
        for _, c in walk(self.grid):
            try:
                if str(c.ClassName or "") != CELL:
                    continue
            except Exception:
                continue
            r = rect_of(c)
            if r and r[2] - r[0] > 2 * INSET and r[3] - r[1] > 2 * INSET and r[0] >= gr[0] - 2 and r[1] >= gr[1] - 2 and r[2] <= gr[2] + 2 and r[3] <= gr[3] + 2:
                found.append({"ctl": c, "rect": r, "fps": [], "shots": []})
        found.sort(key=lambda x: (x["rect"][1], x["rect"][0]))
        rows: list[list[dict]] = []
        for c in found:
            if rows and abs(c["rect"][1] - rows[-1][0]["rect"][1]) <= 8:
                rows[-1].append(c)
            else:
                rows.append([c])
        ordered = [c for row in rows for c in sorted(row, key=lambda x: x["rect"][0])]
        return ordered, max((len(r) for r in rows), default=0)

    def read_screen(self, shots: int = SHOTS) -> tuple[list[dict], int]:
        """读一屏的格子，隔 SHOT_GAP 截 shots 次，每格每次算一个指纹（c["shots"]），不同的存进 c["fps"]。"""
        if not self.rect_ok():
            raise PanelError("格子列表的位置读不到了")
        cells, cols = self.cells()
        gr = self.rect
        for i in range(shots):
            if i:
                self.sleep(SHOT_GAP)
            im = self.grab(gr)
            if self.hook:
                self.hook(self.screen_no, i, im)
            for c in cells:
                l, t, r, b = c["rect"]
                fp = fp_of(im.crop((l - gr[0] + INSET, t - gr[1] + INSET, r - gr[0] - INSET, b - gr[1] - INSET)))
                c["shots"].append(fp)
                c["fps"] = merge_fps(c["fps"], [fp])
        return cells, cols

    # ---------- 翻页 ----------
    def wheel(self, up: bool, n: int):
        while n > 0:
            k = min(n, TICK_CHUNK)
            getattr(self.grid, "WheelUp" if up else "WheelDown")(wheelTimes=k, api=self.wheel_api)
            n -= k

    @staticmethod
    def shift(prev: list[dict], cur: list[dict], cols: int) -> int | None:
        """cur 这一屏比 prev 往下翻了几格（0=没动；只会是一行格数的整数倍）；接不上返回 None。"""
        step = max(1, cols)
        for d in range(0, len(prev), step):
            ov = min(len(prev) - d, len(cur))
            if ov < 1 or (d == 0 and len(prev) != len(cur)):
                continue
            hit = sum(1 for j in range(ov) if close(prev[d + j]["fps"], cur[j]["fps"]))
            if hit / ov >= MATCH:
                return d
        return None

    def to_top(self):
        prev = None
        for _ in range(50):
            cur, cols = self.read_screen(1)
            if prev is not None and self.shift(prev, cur, cols) == 0:
                return
            prev = cur
            self.wheel(True, TICK_CHUNK)
            self.sleep(SETTLE)
        self.log("往上翻了很多次还没到顶，就从现在的位置开始")

    def pages(self, shots: int = SHOTS):
        """从最上面开始一屏一屏往下翻，直到翻不动。每屏产出 (这一屏的格子, 其中第一次见到的格子, 现在累计滚了几格)。
        格子是同一个对象贯穿始终：c["pos"]=第几格，c["ticks"]=第一次见到它时累计滚了几格，c["fps"]=各次见到的指纹，c["ctl"]/c["rect"]=最近一次的控件和位置。"""
        self.to_top()
        self.seq, prev, ticks = [], None, 0
        for n in range(MAX_SCREENS):
            self.screen_no = n
            if prev is not None:
                self.wheel(False, STEP_TICKS)
                ticks += STEP_TICKS
                self.sleep(SETTLE)
            cells, cols = self.read_screen(shots)
            if not cells:
                raise PanelError("「自定义表情」这一栏里一个完整的格子都读不到")
            if prev is None:
                new = cells
            else:
                d = self.shift(prev, cells, cols)
                if d == 0 and n == 1 and self.wheel_api:  # 第一次往下翻就纹丝没动：换一种滚法再试一次（不动鼠标的滚法可能不管用）
                    self.wheel_api = False
                    self.log("第一次往下翻没动，改用移动鼠标的滚法再试")
                    self.wheel(False, STEP_TICKS)
                    self.sleep(SETTLE)
                    cells, cols = self.read_screen(shots)
                    d = self.shift(prev, cells, cols)
                if d is None:
                    raise PanelError(f"翻到第 {n + 1} 屏时和上一屏接不上（一次翻 {STEP_TICKS} 格滚轮翻过头了，或者画面在变）")
                if d == 0:
                    return
                for j in range(len(prev) - d):  # 重叠的部分：补上新见到的指纹，控件和位置换成最新的
                    old = prev[d + j]
                    old["fps"] = merge_fps(old["fps"], cells[j]["fps"])
                    old["ctl"], old["rect"] = cells[j]["ctl"], cells[j]["rect"]
                    cells[j] = old
                new = cells[len(prev) - d:]
            for c in new:
                c["pos"], c["ticks"] = len(self.seq), ticks
                self.seq.append(c)
            prev = cells
            yield cells, new, ticks

    # ---------- 预览 ----------
    def preview(self, cell: dict):
        """鼠标移到这一格上，等放大预览出来再截；然后把鼠标移开。返回 (图片, 是预览还是退而求其次的缩略图)。"""
        img, kind = None, "thumb"
        try:
            cell["ctl"].MoveCursorToMyCenter(simulateMove=False)
            for _ in range(6):
                self.sleep(0.3)
                for _, root in popup_roots(self.ui, self.main):
                    pv = find_class(root, PREVIEW)
                    r = rect_of(pv) if pv is not None else None
                    if r and r[2] > r[0] and r[3] > r[1]:
                        img, kind = self.grab(r), "preview"
                        break
                if img is not None:
                    break
        finally:
            self.park()
        if img is None:
            l, t, r, b = cell["rect"]
            img = self.grab((l + INSET, t + INSET, r - INSET, b - INSET))
        return img, kind

    # ---------- 建目录 ----------
    def catalog(self, known: list, budget: int) -> dict:
        """known：[(key, 指纹列表)]，目录里已有的。返回 {groups, cells, screens, pending}。
        groups：按位置的每张表情 {key（认出是目录里哪张，新的为 None）, pos, ticks, fps, preview（新的才有）, kind}。
        鼠标只移到新格子上看预览，不点；预览最多截 budget 张，其余新的算 pending。"""
        self.open()
        groups: list[dict] = []
        left, screens = budget, 0
        for _, new, _ in self.pages():
            screens += 1
            for c in new:
                g = next((g for g in groups if close(g["fps"], c["fps"])), None)
                if g is not None:  # 收藏里有两张几乎一样的：算同一张
                    g["fps"] = merge_fps(g["fps"], c["fps"])
                    g["cells"].append(c)
                    continue
                best = min(((dist(f, c["fps"]), k) for k, f in known), default=(99, None), key=lambda x: x[0])
                g = {"key": best[1] if best[0] <= NEAR else None, "pos": c["pos"], "ticks": c["ticks"], "fps": list(c["fps"]), "preview": None, "kind": "", "pending": False, "cells": [c]}
                if g["key"] is None:
                    if left > 0:
                        left -= 1
                        g["preview"], g["kind"] = self.preview(c)
                    else:
                        g["pending"] = True
                groups.append(g)
        for g in groups:  # 翻页时后面几屏又见到的指纹（会动的缩略图）补进这一组
            for c in g.pop("cells"):
                g["fps"] = merge_fps(g["fps"], c["fps"])
        return {"groups": groups, "cells": len(self.seq), "screens": screens, "pending": sum(1 for g in groups if g["pending"])}

    # ---------- 发第一屏的第一格（验证命令用） ----------
    def send_first(self) -> dict:
        """打开面板、读第一屏、点第一格（不翻页、不找指纹）。没读到格子返回 {found: False}。"""
        self.open()
        cells, _ = self.read_screen(1)
        if not cells:
            return {"found": False}
        cell = cells[0]
        self.guard.allow_cell(cell["ctl"])
        self.guard.click(cell["ctl"], "cell")
        return {"found": True, "pos": 0, "ticks": 0, "fps": list(cell["fps"])}

    def look(self, fps: list[int]):
        """现在这一屏里找指纹对得上的格子：最多截 3 次，每次隔 SHOT_GAP。"""
        for i in range(SHOTS):
            if i:
                self.sleep(SHOT_GAP)
            cells, _ = self.read_screen(1)
            for c in cells:
                if close(fps, c["fps"]):
                    return c
        return None

    def send(self, entry: dict) -> dict:
        """entry：{fps, pos, ticks}。找到并点了返回 {found: True, how, pos, ticks, fps}，没找到 {found: False}。"""
        self.open()
        fps = list(entry.get("fps") or [])
        self.to_top()
        ticks = int(entry.get("ticks") or 0)
        if ticks:
            self.wheel(False, ticks)
            self.sleep(SETTLE)
        hit, how, pos = self.look(fps), "near", entry.get("pos")
        if hit is None:
            self.log("记下的位置附近对不上，从头找一遍")
            how = "rescan"
            for cells, _, t in self.pages():
                hit = next((c for c in cells if close(fps, c["fps"])), None)
                if hit is not None:
                    ticks, pos = t, hit["pos"]
                    break
        if hit is None:
            return {"found": False}
        self.guard.allow_cell(hit["ctl"])
        self.guard.click(hit["ctl"], "cell")
        return {"found": True, "how": how, "pos": pos, "ticks": ticks, "fps": merge_fps(fps, hit["fps"])}


class WxStickers:
    """给微信渠道和验证命令用：连主窗口、切会话、跑一遍 Panel、收尾（按 Esc、切回原来的会话）。"""

    def __init__(self, ui, hwnd_finder, grab=default_grab, sleep=time.sleep, log=print, debug=None, api=None):
        self.ui, self.hwnd_finder, self.grab, self.sleep, self.log = ui, hwnd_finder, grab, sleep, log
        self.debug = debug or log  # 只在调试级别记的诊断；没单独给就跟 log 一样（验证命令、测试里都要看到）
        self.api = api  # 窗口接口：读完消息之后对微信自己弹出的头像小菜单发 Esc 用；不传就不做这一步
        self.guard = None
        self.hook = None
        self.panel = None

    def _panel(self) -> Panel:
        hwnd = self.hwnd_finder()
        if not hwnd:
            raise PanelError("找不到微信主窗口")
        main = self.ui.ControlFromHandle(hwnd)
        if main is None:
            raise PanelError("读不到微信主窗口的界面")
        self.guard = Guard(self.log)
        self.panel = Panel(self.ui, main, self.grab, self.sleep, self.log, self.guard)
        self.panel.hook = self.hook
        return self.panel

    def _open_chat(self, wx, chat: str, again: bool = True, want: str = ""):
        """切到 chat，并用 ChatInfo() 核对「开着的就是它」：名字要正好相等；want="private" 时开着的还不能是群
        （ChatInfo 给了 chat_type 才判得了）。again=False：已经开着这个聊天就不再多切一次（验证命令里刚切过来、马上又要发）。"""
        info = {}
        if not again:
            try:
                info = wx.ChatInfo() or {}
            except Exception:
                info = {}
            if (info.get("chat_name") or "") == chat and not (want == "private" and str(info.get("chat_type") or "") == "group"):
                return
        wx.ChatWith(chat)
        opened, typ = "", ""
        for _ in range(5):
            try:
                info = wx.ChatInfo() or {}
            except Exception:
                info = {}
            opened, typ = str(info.get("chat_name") or ""), str(info.get("chat_type") or "")
            if opened == chat:  # 名字对上了：不再等（开着的不是群这一点再看下面一次判断）
                break
            self.sleep(0.25)
        if opened == chat and not (want == "private" and typ == "group"):
            return
        raise PanelError(f"没能切到「{chat}」（现在打开的是「{opened}」{'，这是个群' if want == 'private' and typ == 'group' else ''}）")

    def _finish(self, wx, panel, chat: str, back_to: str):
        if panel is not None:
            panel.close()
        if back_to and back_to != chat:
            try:
                wx.ChatWith(back_to)
            except Exception as ex:
                self.log(f"切回原来的会话「{back_to}」没成功：{type(ex).__name__}: {ex}")

    def catalog(self, wx, known: list, budget: int, chat: str = CHAT_TARGET, back_to: str = "") -> dict:
        panel = None
        try:
            self._open_chat(wx, chat)
            panel = self._panel()
            return panel.catalog(known, budget)
        finally:
            self._finish(wx, panel, chat, back_to)

    def _tail(self, wx, api=None) -> list:
        """读聊天里最后几条（发自定义表情前后各读一次，看有没有多出自己发的表情）。
        读完马上对微信自己弹出的头像小菜单发一次 Esc（见 wxshot.esc_menus）：消息所在的窗口从这几条消息的控件往上找，
        拿不到就这次不做。这一步出任何错都只记日志，不影响发表情。api 是窗口接口（不传就不做这一步）。"""
        msgs = []
        try:
            msgs = (wx.GetAllMessage() or [])[-10:]
        finally:
            if api is not None:
                from . import wxshot
                try:
                    owner = next((h for h in (wxshot.owner_hwnd(getattr(m, "control", None), api) for m in reversed(list(msgs))) if h), 0)
                    rect = api.rect(owner) if owner else None
                    if rect and rect[2] > rect[0] and rect[3] > rect[1]:
                        wxshot.esc_menus(rect, api, owner, log=self._menu_log, sleep=self.sleep)
                except Exception as ex:
                    if wxshot._once(("sticker-tail", type(ex).__name__, str(ex))):
                        self.log(f"读最后几条之后关小菜单出错（{type(ex).__name__}: {ex}），不影响发表情")
        return [(str(getattr(m, "id", "") or ""), str(getattr(m, "type", "") or ""), str(getattr(m, "attr", "") or ""), str(getattr(m, "content", "") or "")) for m in msgs]

    def _menu_log(self, level: str, text: str):
        """关小菜单这一步的日志：warn 也走这个通道（渠道给的 log 只收一行字）。"""
        self.log(text if level != "warn" else f"warn：{text}")

    @staticmethod
    def _emoji_added(before: list, after: list) -> bool:
        """点表情是唯一做过的操作：最后几条里多出任何一条自己发的消息（不看类型、内容），就是它发出去了。"""
        return any(n > 0 and attr == "self" for (_, _, attr, _), n in (Counter(after) - Counter(before)).items())

    @staticmethod
    def _rows(rows: list, n: int = 5) -> str:
        return "；".join(f"[type={typ}|attr={attr}|{content[:20]}]" for _, typ, attr, content in rows[-n:]) or "（无）"

    def send(self, wx, entry: dict, chat: str, back_to: str = "", want: str = "") -> dict:
        """切到 chat 发这一张。返回 {found, clicked, confirmed(True/False/None=没读出来), ...}；找不到只是 found=False，不抛错。
        面板、界面上的问题抛 PanelError；切过去核对不上（名字对不上、私聊却开着群）抛 PanelError，一个格子都不点。
        want="private"：私聊，切过去核对时还要确认开着的不是群。"""
        return self._send(wx, chat, back_to, lambda p: p.send(entry), "翻到底也没有对上这一张，没有点任何格子", want=want)

    def send_first(self, wx, chat: str, back_to: str = "") -> dict:
        """切到 chat，发面板第一屏的第一格（验证命令用：不知道是哪一张，就是第一格）。返回同 send。
        刚切过去的聊天不用再切一次（不改「切会话」的次数）。"""
        return self._send(wx, chat, back_to, lambda p: p.send_first(), "表情面板里一个格子都没读到，没有点任何格子", again=False)

    def _send(self, wx, chat: str, back_to: str, pick, missing: str, again: bool = True, want: str = "") -> dict:
        panel, res = None, {"found": False, "clicked": False, "confirmed": None}
        try:
            self._open_chat(wx, chat, again, want)
            try:
                before = self._tail(wx, self.api)
            except Exception as ex:
                before = None
                self.log(f"点之前读聊天里最后几条没成功：{type(ex).__name__}: {ex}")
            panel = self._panel()
            res.update(pick(panel))
            if not res["found"]:
                self.log(missing)
                return res
            res["clicked"] = True
            self.sleep(1.0)
            panel.close()
            after = None
            try:
                after = self._tail(wx, self.api)
                res["confirmed"] = self._emoji_added(before, after) if before is not None else None
            except Exception as ex:
                self.log(f"点完读聊天里最后几条没成功：{type(ex).__name__}: {ex}")
            if res["confirmed"] is False:  # 对不上时把前后最后几条的字段都写出来，在服务器上能看出是哪里对不上
                self.debug(f"没确认发出：点之前最后几条 {self._rows(before)}；点之后最后几条 {self._rows(after)}")
            self.log("聊天里多了一条自己发的表情，确认发出" if res["confirmed"] else "表情已点击发送（微信不列出自己发的表情，发后没法再核对）" if res["confirmed"] is False else "点完之后没能确认是否发出")
            return res
        finally:
            self._finish(wx, panel, chat, back_to)


# ---------- 验证命令 ----------
def run_test(out_dir, lib=None, ui=None, hwnd_finder=None, grab=default_grab, sleep=time.sleep) -> int:
    """服务器上的验证：连微信 → 切到「文件传输助手」→ 建一遍目录（截图、指纹存下来；看图描述跳过）→ 挑第一格发到「文件传输助手」→ 记下是否确认发出。
    返回 0（走完）或 1（停在某一步）。只在「文件传输助手」里操作。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    steps: list[str] = []
    result = {"ok": False, "stopped_at": "", "why": "", "out": str(out), "cells": [], "groups": 0, "send": None}
    state = {"step": "开始", "orig": ""}

    def log(text: str):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        steps.append(text)
        with open(out / "probe.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    drv = None
    wx = None
    try:
        state["step"] = "1 连上微信"
        if lib is None or ui is None:
            from .channels import wechat as wxmod
            lib = wxmod.load_lib()
            import importlib
            ui = importlib.import_module(f"{wxmod.WX_LIB}.uia.uiautomation")
        if hwnd_finder is None:
            from .winpos import WinApi
            hwnd_finder = WinApi().find_main
        wx = WxForTest(lib.WeChat(resize=False), [])
        try:
            state["orig"] = (wx.ChatInfo() or {}).get("chat_name") or ""
        except Exception:
            state["orig"] = ""
        log(f"已连上微信，现在开着的会话：「{state['orig']}」")

        def shot(n, i, im):
            if i == 0:
                im.save(out / f"catalog-screen-{n + 1:02d}.png")
        drv = WxStickers(ui, hwnd_finder, grab, sleep, log)
        drv.hook = shot

        state["step"] = "2 建目录（在「文件传输助手」里）"
        cat = drv.catalog(wx, [], 3, CHAT_TARGET, "")
        cells = sorted(drv.panel.seq, key=lambda c: c["pos"])
        result["cells"] = [{"pos": c["pos"], "ticks": c["ticks"], "shots": [f"{f:016x}" if f is not None else None for f in c["shots"]],
                            "fps": [f"{f:016x}" for f in c["fps"]]} for c in cells]
        result["groups"] = len(cat["groups"])
        result["screens"] = cat["screens"]
        result["wheel_api"] = drv.panel.wheel_api
        n_prev = 0
        for g in cat["groups"]:
            if g["preview"] is not None:
                n_prev += 1
                g["preview"].save(out / f"preview-{n_prev}.png")
                log(f"第 {g['pos']} 格的预览已存成 preview-{n_prev}.png（{'放大预览' if g['kind'] == 'preview' else '没等到放大预览，截的是格子里的缩略图'}）")
        result["previews"] = [g["kind"] for g in cat["groups"] if g["preview"] is not None]
        log(f"目录建好了：共 {cat['cells']} 格、{len(cat['groups'])} 张不同的表情、{cat['screens']} 屏；点过的控件：{drv.guard.clicked}")
        if not cat["groups"]:
            raise PanelError("「自定义表情」里一个格子都没读到")
        moving = sum(1 for c in cells if len({f for f in c["shots"] if f is not None}) > 1)
        log(f"其中 3 次截图指纹不一样的（可能是会动的）有 {moving} 格")
        result["moving_cells"] = moving
        result["catalog_clicked"] = list(drv.guard.clicked)

        state["step"] = "3 发第一格到「文件传输助手」"
        g = cat["groups"][0]
        drv.hook = None
        r = drv.send(wx, {"fps": g["fps"], "pos": g["pos"], "ticks": g["ticks"]}, CHAT_TARGET, "")
        result["send"] = {k: v for k, v in r.items() if k != "fps"}
        result["send_clicked"] = list(drv.guard.clicked)
        if not r["found"]:
            raise PanelError("发表情时没在面板里找到第一格（指纹对不上）")
        result["ok"] = True
    except PermissionError as pe:
        result.update(stopped_at=state["step"], why=f"挡板拦下了：{pe}")
        log(f"停在第 {state['step']}：挡板拦下了：{pe}")
    except Exception as ex:
        result.update(stopped_at=state["step"], why=f"{type(ex).__name__}: {ex}")
        log(f"停在第 {state['step']}：{type(ex).__name__}: {ex}")
    finally:
        if wx is not None and state["orig"] and state["orig"] != CHAT_TARGET:
            try:
                wx.ChatWith(state["orig"])
                log(f"已切回原来的会话「{state['orig']}」")
            except Exception as ex:
                log(f"切回原来的会话没成功：{type(ex).__name__}: {ex}")
        result["steps"] = steps
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(out_dir: str) -> int:
    code = run_test(out_dir)
    print("验证完成" if code == 0 else "验证没走完，看 probe.log / result.json", flush=True)
    return code
