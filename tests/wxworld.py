"""桩出来的微信表情面板：多页的虚拟列表（只有看得到的格子存在）、会动的缩略图、放大预览、点格子会发出一条表情。测试用，不碰真的微信。"""
import random
from types import SimpleNamespace

from PIL import Image

from mb import wxsticker as ws

COLS, CELL, TICK_PX = 5, 88, 60


def art(sid, phase=0, size=72):
    """一张表情的画面：每个 sid 一个固定的 9×8 灰度图样放大（算出来的指纹互不相同），phase 不同画面不同（模拟会动）。"""
    r = random.Random(f"{sid}/{phase}")
    return Image.frombytes("L", (9, 8), bytes(r.randrange(256) for _ in range(72))).resize((size, size), Image.NEAREST).convert("RGB")


def fp(sid, phase=0):
    return ws.fp_of(art(sid, phase).crop((0, 0, 72, 72)))


class Node:
    def __init__(self, world, type_, cls, name, rect=(0, 0, 10, 10), kids=None, hwnd=0, on_click=None):
        self.world, self.ControlTypeName, self.ClassName, self.Name, self.AutomationId = world, type_, cls, name, ""
        self.rect, self.NativeWindowHandle, self.ProcessId, self._kids, self._on_click = rect, hwnd, 7, kids, on_click
        self.BoundingRectangle = SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])

    def GetChildren(self):
        return list(self._kids() if callable(self._kids) else (self._kids or []))

    def Click(self):
        self.world.clicks.append((self.ControlTypeName, self.ClassName, self.Name))
        if self._on_click:
            self._on_click()


class Cell(Node):
    def __init__(self, world, index, rect):
        super().__init__(world, "TextControl", ws.CELL, "自定义表情", rect, on_click=lambda: world.pick(index))
        self.index = index

    def MoveCursorToMyCenter(self, simulateMove=True):
        self.world.hover = self.index
        self.world.hovered.append(self.index)


class Grid(Node):
    def WheelDown(self, wheelTimes=1, api=True, **k):
        self.world.wheel(wheelTimes, api)

    def WheelUp(self, wheelTimes=1, api=True, **k):
        self.world.wheel(-wheelTimes, api)


class World:
    """items：[(sid, 会不会动)]，从左到右从上到下排。列表区域 5.5 行高：最下面一行只露半截，半截的格子控件存在但不完整。"""

    def __init__(self, items, orig="某个群", api_wheel_works=True, sends=True, screen_dpi=1):
        self.items = list(items)
        self.gr = (80, 200, 80 + COLS * CELL, 200 + int(CELL * 5.5))
        self.offset = 0
        self.chat, self.orig_chat = orig, orig
        self.opened = self.tab = False
        self.hover = None
        self.hovered: list[int] = []
        self.clicks: list[tuple] = []
        self.keys: list[str] = []
        self.chatwith: list[str] = []
        self.sent_index: list[int] = []
        self.msgs: list = []
        self.sends = sends
        self.api_wheel_works = api_wheel_works
        self.grab_n = 0
        self.phase0 = 0
        self.wheel_calls: list[tuple] = []
        self.sendmsg: list[tuple] = []
        self.btn = Node(self, "ButtonControl", "mmui::XButton", "发送表情(Alt+E)", (10, 500, 40, 530), on_click=self._open)
        self.main = Node(self, "WindowControl", "mmui::MainWindow", "微信", hwnd=11, kids=[
            Node(self, "ButtonControl", "mmui::XButton", "发送文件"), Node(self, "ButtonControl", "mmui::XButton", "表情"), self.btn,
            Node(self, "ListItemControl", "mmui::ChatTextItem", "[表情] 聊天记录（名字里也带表情二字）")])
        self.grid = Grid(self, "ListControl", ws.GRID, "", self.gr, kids=self._cells)
        self.heart = Node(self, "TabItemControl", "mmui::EmoticonToolbarItem", "自定义表情", (150, 650, 190, 690), on_click=self._tab)
        self.popover = Node(self, "WindowControl", ws.POPOVER, "Weixin", (74, 185, 588, 699), hwnd=22, kids=self._pop_kids)
        self.preview = Node(self, "WindowControl", ws.PREVIEW, "", (600, 200, 760, 360), hwnd=33)

    # ---- 状态 ----
    def _open(self):
        self.opened = True

    def _tab(self):
        self.tab = True

    def _pop_kids(self):
        kids = [Node(self, "TabItemControl", "mmui::EmoticonToolbarItem", "搜索表情"), Node(self, "TabItemControl", "mmui::EmoticonToolbarItem", "默认表情"),
                Node(self, "ButtonControl", "mmui::XButton", "自定义表情"), self.heart]
        return kids + ([self.grid] if self.tab else [])

    def _cells(self):
        out = []
        for i in range(len(self.items)):
            top = self.gr[1] + (i // COLS) * CELL - self.offset
            if top + CELL > self.gr[1] and top < self.gr[3]:
                left = self.gr[0] + (i % COLS) * CELL
                out.append(Cell(self, i, (left, top, left + CELL, top + CELL)))
        return out

    def wheel(self, n, api):
        self.wheel_calls.append((n, api))
        if api and not self.api_wheel_works:
            return
        rows = -(-len(self.items) // COLS)
        self.offset = max(0, min(max(0, rows * CELL - (self.gr[3] - self.gr[1])), self.offset + n * TICK_PX))

    def pick(self, index):
        self.sent_index.append(index)
        if self.sends:
            self.msgs.append(SimpleNamespace(id=f"s{len(self.msgs)}", type="emotion", attr="self", content="[动画表情]"))

    # ---- 桩的界面自动化、截图、微信 ----
    def roots(self):
        out = [self.main]
        if self.opened:
            out.append(self.popover)
        if self.opened and self.hover is not None:
            out.append(self.preview)
        return out

    def ui(self):
        w = self

        def esc(text):
            w.keys.append(text)
            if text == "{Esc}":
                w.opened = w.tab = False
                w.hover = None
        return SimpleNamespace(ControlFromHandle=lambda h: w.main, GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: w.roots()),
                               SendKeys=esc, SetCursorPos=lambda x, y: setattr(w, "hover", None))

    def grab(self, rect):
        self.grab_n += 1
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        im = Image.new("RGB", (w, h), (255, 255, 255))
        if self.hover is not None and tuple(rect) == self.preview.rect:
            sid, _ = self.items[self.hover]
            return art(sid, 0, 160)
        phase = (self.grab_n + self.phase0) % 2
        for c in self._cells():
            sid, moving = self.items[c.index]
            im.paste(art(sid, phase if moving else 0), (c.rect[0] - rect[0] + ws.INSET, c.rect[1] - rect[1] + ws.INSET))
        return im

    def wx(self):
        w = self

        class Wx:
            def __init__(self, **k):
                pass

            def ChatInfo(self):
                return {"chat_name": w.chat}

            def ChatWith(self, who, **k):
                w.chatwith.append(who)
                w.chat = who

            def GetAllMessage(self):
                return list(w.msgs)

            def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
                if who is not None:  # 新版发送前已经切过去核对过：不再把 who 交给 SendMsg 去模糊搜索
                    raise AssertionError(f"不该把 who 交给 SendMsg：{who}")
                w.sendmsg.append((who, msg))
                return SimpleNamespace(is_success=True)

            def __getattr__(self, name):
                raise AssertionError(f"不该碰 wx.{name}")
        return Wx()

    def lib(self):
        return SimpleNamespace(WeChat=self.wx().__class__)

    def driver(self, **kw):
        return ws.WxStickers(self.ui(), lambda: 11, grab=self.grab, sleep=lambda s: None, log=kw.pop("log", lambda t: None))

    def driver_panel(self):
        """只要一个能打开 / 关闭的面板对象（测收尾按不按 Esc 用），不跑建目录、不发表情。"""
        return self.driver()._panel()
