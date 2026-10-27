"""微信表情面板探查（只看不发）：为以后让她发微信的表情铺路，先把表情面板的界面结构摸清楚。

用法（在装 PodMates 的那台机器上，**PodMates 主程序没在运行的时候**，微信已登录）：
    PodMates.exe --probe-wechat-emoji <输出目录>

做的事，一步一步（每一步的结果写进 <输出目录>\\probe.log，停在哪一步一眼看得出来）：
  1. 连上已登录的微信（用数据目录里已装的 wxauto4，不改窗口大小）。
  2. 记下现在开着的会话，切到「文件传输助手」。
  3. 点聊天输入栏上的「发送表情(Alt+E)」按钮，打开表情面板；把面板的界面结构写成文本、截图存成图片。
  4. 点面板里「自定义表情」那一栏（心形）；再把界面结构写成文本、截图存成图片。
  5. 按 Esc 关掉面板，切回原来的会话。

硬约束（代码里有挡板，测试里有测试）：不点任何一个表情，不往任何会话发任何东西，不改微信的设置。
全程只允许两个点击：「发送表情(Alt+E)」按钮、「自定义表情」那一栏；碰到别的控件一律拒绝。任何一步失败都直接停下并写明停在哪一步（失败后只做收尾：按 Esc、切回原来的会话）。
不取单实例互斥体，不启动引擎，不碰 NapCat，也不碰 PodMates 的数据库。

输出的文件（都在 <输出目录> 里）：
  probe.log                  每一步的时间、结果（文本）
  result.json                {"ok": 是否走完, "stopped_at": 停在哪一步, "steps": [...]}
  main-before-panel.txt      点「发送表情(Alt+E)」之前，微信主窗口的界面结构（每行：缩进=层级 | 控件类型 | 类名 | 名字 | 自动化编号 | 位置）
  emoji-panel.txt            点「发送表情(Alt+E)」之后，主窗口和同进程弹出的窗口的界面结构
  emoji-panel.png            表情面板区域的截图（找不到面板区域就截整个主窗口）
  emoji-single.txt           点「自定义表情」那一栏之后的界面结构
  emoji-single.png           同上，截图
"""
import json
import sys
import time
from pathlib import Path

CHAT_TARGET = "文件传输助手"
EMOJI_BUTTON = ("发送表情(Alt+E)",)  # 聊天输入栏上的按钮名字（服务器微信 4.1.15.56 简体中文上叫这个）（找不到时在日志里列出输入栏附近所有按钮的名字）
SINGLE_TAB = ("自定义表情",)  # 面板底部三栏（搜索表情 / 默认表情 / 自定义表情）里心形那一栏的名字，类型是 TabItemControl
ALLOWED_CLICKS = {"open-panel": EMOJI_BUTTON, "single-tab": SINGLE_TAB}
FORBIDDEN_WX = ("SendMsg", "SendFiles", "SendTypingText", "AddListenChat", "SetSetting", "Close", "ShutDown")
MAX_NODES = 6000


class Stop(Exception):
    """一步没走成：停在这一步。"""

    def __init__(self, step: str, why: str):
        super().__init__(f"{step}：{why}")
        self.step, self.why = step, why


class SafeWx:
    """给 wxauto4 的 WeChat 对象套一层：只放行「读当前会话」和「切会话」，发消息、发文件、监听、关窗口……一概不让碰。"""
    ALLOWED = ("ChatInfo", "ChatWith")

    def __init__(self, wx, calls: list):
        self._wx, self._calls = wx, calls

    def __getattr__(self, name):
        if name not in self.ALLOWED:
            raise PermissionError(f"探查命令不允许调用 {name}")
        fn = getattr(self._wx, name)

        def call(*a, **k):
            self._calls.append((name, a))
            return fn(*a, **k)
        return call


def rect_of(c):
    """控件的位置 (left, top, right, bottom)，读不出返回 None。"""
    try:
        r = c.BoundingRectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def describe(c) -> list:
    out = []
    for attr in ("ControlTypeName", "ClassName", "Name", "AutomationId"):
        try:
            out.append(str(getattr(c, attr, "") or ""))
        except Exception:
            out.append("?")
    r = rect_of(c)
    out.append(f"{r[0]},{r[1]},{r[2]},{r[3]}" if r else "?")
    return out


def walk(root, limit: int = MAX_NODES):
    """深度优先走一遍控件树，产出 (层级, 控件)；总数封顶，免得卡死。"""
    stack, n = [(0, root)], 0
    while stack and n < limit:
        depth, c = stack.pop()
        n += 1
        yield depth, c
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        stack.extend((depth + 1, k) for k in reversed(kids))


def dump_tree(roots: list, path: Path) -> int:
    """把几棵控件树写成文本，返回节点数。"""
    lines, total = [], 0
    for title, root in roots:
        lines.append(f"==== {title} ====")
        for depth, c in walk(root):
            lines.append("  " * depth + " | ".join(describe(c)))
            total += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return total


def find_exact(root, names: tuple, ctype: str = ""):
    """按名字精确找控件（要点击的只认名字完全一样的：聊天记录里的消息、表情条目的名字里也可能带「表情」二字，包含式匹配会点错）。ctype 不空时控件类型也要对上。"""
    for _, c in walk(root):
        try:
            if str(c.Name or "") in names and (not ctype or str(getattr(c, "ControlTypeName", "")) == ctype):
                return c
        except Exception:
            continue
    return None


def near_names(root, words: tuple, limit: int = 40) -> list[str]:
    """名字里带这几个字的控件（类型/名字），找不到要点的控件时写进失败说明，方便对着改名字。"""
    out = []
    for _, c in walk(root):
        try:
            n = str(c.Name or "")
            if n and any(w in n for w in words):
                out.append(f"{getattr(c, 'ControlTypeName', '?')}/{n}")
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def guarded_click(c, purpose: str, log):
    """点一下控件。只放行两种：打开表情面板的「发送表情(Alt+E)」按钮、面板里的「自定义表情」那一栏；名字对不上的一律拒绝。"""
    allowed = ALLOWED_CLICKS.get(purpose)
    name = str(getattr(c, "Name", "") or "")
    if not allowed or name not in allowed:
        raise PermissionError(f"拒绝点击「{name}」（{purpose}）：探查命令只允许点 {ALLOWED_CLICKS}")
    log(f"点击 {purpose}：「{name}」")
    c.Click()


def default_grab(rect, path: Path):
    from PIL import ImageGrab
    ImageGrab.grab(bbox=rect, all_screens=True).save(path)


def run(out_dir, lib=None, ui=None, hwnd_finder=None, grab=default_grab, sleep=time.sleep, wait: float = 1.0) -> int:
    """跑一遍探查，返回 0（走完）或 1（停在某一步）。lib：wxauto4 模块；ui：它的 uiautomation 模块；hwnd_finder：返回微信主窗口句柄。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    wx_calls: list = []
    clicked: list[str] = []

    def log(text: str):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        steps.append({"t": time.time(), "text": text})
        with open(out / "probe.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    state = {"orig": "", "switched": False, "panel": False, "step": "开始"}
    result = {"ok": False, "stopped_at": "", "why": "", "out": str(out)}
    wx = None
    ctrl_click = guarded_click

    def click(c, purpose):
        ctrl_click(c, purpose, log)
        clicked.append(purpose)

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
        raw = lib.WeChat(resize=False)  # 不改微信窗口的大小
        wx = SafeWx(raw, wx_calls)
        log("已连上微信")

        state["step"] = "2 切到「文件传输助手」"
        try:
            state["orig"] = (wx.ChatInfo() or {}).get("chat_name") or ""
        except Exception:
            state["orig"] = ""
        log(f"现在开着的会话：「{state['orig']}」")
        wx.ChatWith(CHAT_TARGET)
        state["switched"] = True
        now = (wx.ChatInfo() or {}).get("chat_name") or ""
        if now != CHAT_TARGET:
            raise Stop(state["step"], f"没切过去（现在是「{now}」）")
        log(f"已切到「{CHAT_TARGET}」")

        state["step"] = "3 打开表情面板"
        hwnd = hwnd_finder()
        if not hwnd:
            raise Stop(state["step"], "找不到微信主窗口")
        main = ui.ControlFromHandle(hwnd)
        if main is None:
            raise Stop(state["step"], "读不到微信主窗口的界面")
        n = dump_tree([("微信主窗口（点「发送表情(Alt+E)」之前）", main)], out / "main-before-panel.txt")
        log(f"主窗口界面结构已写入 main-before-panel.txt（{n} 个控件）")
        btn = find_exact(main, EMOJI_BUTTON)
        if btn is None:
            names = sorted({str(c.Name) for _, c in walk(main) if str(getattr(c, 'ControlTypeName', '')) == "ButtonControl" and getattr(c, "Name", "")})
            raise Stop(state["step"], f"找不到名字叫「发送表情(Alt+E)」的控件；名字里带「表情」的有：{'、'.join(near_names(main, ('表情',)))}；主窗口里的按钮有：{'、'.join(names[:60])}")
        click(btn, "open-panel")
        state["panel"] = True
        sleep(wait)
        roots = [("微信主窗口（点「发送表情(Alt+E)」之后）", main)] + popup_roots(ui, main)
        n = dump_tree(roots, out / "emoji-panel.txt")
        area = panel_rect(roots, main)
        grab(area, out / "emoji-panel.png")
        log(f"表情面板界面结构已写入 emoji-panel.txt（{n} 个控件），截图 emoji-panel.png（区域 {area}）")

        state["step"] = "4 切到「自定义表情」"
        tab = None
        for _, root in roots:
            tab = find_exact(root, SINGLE_TAB, "TabItemControl")
            if tab:
                break
        if tab is None:
            near = [x for _, root in roots for x in near_names(root, ("自定义", "收藏", "默认", "搜索", "表情"))]
            raise Stop(state["step"], f"在表情面板里找不到名字叫「自定义表情」的那一栏；名字里带这几个字的有：{'、'.join(near[:40])}（完整界面结构在 emoji-panel.txt）")
        click(tab, "single-tab")
        sleep(wait)
        roots = [("微信主窗口（切到自定义表情那一栏之后）", main)] + popup_roots(ui, main)
        n = dump_tree(roots, out / "emoji-single.txt")
        area = panel_rect(roots, main)
        grab(area, out / "emoji-single.png")
        log(f"「自定义表情」这一栏的界面结构已写入 emoji-single.txt（{n} 个控件），截图 emoji-single.png（区域 {area}）")
        result["ok"] = True
    except Stop as s:
        result.update(stopped_at=s.step, why=s.why)
        log(f"停在第 {s.step}：{s.why}")
    except PermissionError as pe:
        result.update(stopped_at=state["step"], why=f"挡板拦下了：{pe}")
        log(f"停在第 {state['step']}：挡板拦下了：{pe}")
    except Exception as ex:
        result.update(stopped_at=state["step"], why=f"{type(ex).__name__}: {ex}")
        log(f"停在第 {state['step']}：{type(ex).__name__}: {ex}")
    finally:
        # 收尾（成功失败都做）：关掉面板、切回原来的会话。只做这两件事。
        # 面板已经自己关上了就不按 Esc：那一下会落到微信主窗口上，微信会把它藏进托盘。
        if state["panel"] and popup_roots(ui, main):
            try:
                ui.SendKeys("{Esc}")
                log("已按 Esc 关掉表情面板")
            except Exception as ex:
                log(f"按 Esc 没成功：{type(ex).__name__}: {ex}")
        elif state["panel"]:
            log("表情面板已自己关上，不用按 Esc")
        if state["switched"] and wx is not None and state["orig"] and state["orig"] != CHAT_TARGET:
            try:
                wx.ChatWith(state["orig"])
                log(f"已切回原来的会话「{state['orig']}」")
            except Exception as ex:
                log(f"切回原来的会话没成功：{type(ex).__name__}: {ex}")
        result["steps"] = [s["text"] for s in steps]
        result["clicked"] = clicked
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def _vis(h):
    """Win32：h 不是一个真窗口返回 None，否则返回它是不是显示着（IsWindowVisible）。"""
    if not h or sys.platform != "win32":
        return None
    import ctypes
    u = ctypes.windll.user32
    return bool(u.IsWindowVisible(h)) if u.IsWindow(h) else None


def _shown(c) -> bool:
    """窗口是不是显示着（Win32 IsWindowVisible）。不用 UIA 的 IsOffscreen：微信窗口有时被挪到屏幕外，那时开着的面板也算「屏幕外」。
    读不到句柄、句柄不是真窗口的（测试桩、老控件）照旧算显示着。"""
    return _vis(getattr(c, "NativeWindowHandle", 0)) is not False


def popup_roots(ui, main) -> list:
    """点开面板后，同一个微信进程弹出来的别的顶层窗口（表情面板有时是独立的小窗口）。读不出来返回空。
    只算看得见的窗口：面板自己关掉以后那些隐藏的窗口还挂在进程里，不能当成「面板还在」。"""
    try:
        pid = main.ProcessId
        root = ui.GetRootControl()
        return [(f"同进程的另一个顶层窗口：{c.ClassName}/{c.Name}", c) for c in root.GetChildren()
                if c.ProcessId == pid and c.NativeWindowHandle != main.NativeWindowHandle
                and _shown(c)]
    except Exception:
        return []


def panel_rect(roots: list, main):
    """表情面板的区域：优先取名字里带「表情」的弹出窗口；没有就取整个主窗口。"""
    for title, c in roots[1:]:
        r = rect_of(c)
        if r and "表情" in title:
            return r
    for title, c in roots[1:]:
        r = rect_of(c)
        if r:
            return r
    return rect_of(main) or (0, 0, 800, 600)


def main(out_dir: str) -> int:
    code = run(out_dir)
    print("探查完成" if code == 0 else "探查没走完，看 probe.log / result.json", flush=True)
    return code
