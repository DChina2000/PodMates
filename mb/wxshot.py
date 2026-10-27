"""微信表情：截聊天里那一条表情所在的一块，交给看图的流程。

微信里表情和图片的处理方式不同：表情点不开大图，也没法另存（wxauto4 的 ImageMessage.download 在表情上行不通，
右键菜单里也没有「复制」），所以表情不下载，改成「截屏」：

1. 找到这条消息控件里画表情的那个子元素（`emotion_box` 的规则见下），取它的屏幕矩形；认不出就退回整条消息的矩形，
   再去掉头像和名字那一侧（`row_rect` 的规则见下）。
2. 这条消息不在可见区域里（新消息多、被挤上去了），先让消息滚到看得见的地方（wxauto4 消息自带的 roll_into_view）。
3. 截之前确认那一块露着：用 Win32 的 `WindowFromPoint` 查矩形中心、再查四个角往里一点的位置，每个位置最上面的
   顶层窗口必须**就是这条消息所在的那个顶层窗口**（`owner_hwnd`：从消息控件自己的句柄往上找到顶层窗口）。
   微信自己的菜单、小窗、别的聊天窗口盖在上面都算被盖住，不截；拿不到消息所在的窗口也按截不到处理（不退回
   「是微信进程就算」，2026-10-27 头像上的右键菜单就是这样混过去的）。矩形宽或高小于 24 像素、跑出所有显示器的，
   也不截。不为了截图把微信强行拉到前台去盖住别的窗口。
   实际顺序：先看够不够大、在不在显示器里、拿不拿得到消息所在的窗口（这几样不过就不截、也不去动任何小窗），
   再按第 4 条关掉盖在上面的小菜单，关掉之后按本条逐点查一遍，然后才截。
4. 截之前先关掉盖在这块上的微信自己的小菜单（`close_menus`）：微信进程的、看得见的顶层窗口里，不是消息所在的窗口、
   不是聊天窗口（类名带 `QWindowIcon` 的，以及老的 `mmui::MainWindow`）、和矩形有重叠的，逐个只对它发 Esc（等
   `SETTLE`，看不见了就算关掉），还看得见就只发 `WM_CLOSE`（再等 `SETTLE`）；两种都不成就按截不到处理（warn 里写明
   句柄、类名、矩形）。`mmui::EmoticonPopover`（表情面板，PodMates 自己在操作）开着时一个小窗都不碰，这张不截。
   绝不对主窗口或任何聊天窗口发按键、不 `SetForegroundWindow`、不点鼠标（在主窗口上按 Esc 会把微信藏进托盘）。
5. 截屏用 wxshot.default_grab（和 wxsticker.default_grab 一样：PIL ImageGrab，all_screens=True 管多显示器）。
6. 截完在图片里裁到表情本身（`crop_emotion`：按背景色找出「有内容」的像素，按列切段取最宽的一段，再按行收紧、
   去掉群聊里昵称那一行），裁得太小或找不到内容就按截不到处理。
7. 同一块矩形再截 SHOTS-1 次（每次隔 SETTLE 秒，沿用 wxsticker 的设定）：三帧裁出来一样就是静图，只交第一帧；
   不一样就是动图，把三帧从左到右拼成一张交给看图。
8. 另外（`esc_menus`）：PodMates 自己每读完一次消息（`GetAllMessage()`），微信会在最新一条别人发的消息的头像旁边留下
   一个「@某某／拍一拍」小菜单。读完就对这个菜单发一次 Esc（只发给菜单，不发 `WM_CLOSE`；关不掉的留着、同一个句柄
   这次运行只记一条 warn；表情面板开着时不做；发完主窗口看不见了就记 warn 并 `unhide()`）。见 wxshot.esc_menus 的说明。

怎么认出表情子元素（依据 wxauto4 msgs/msg 里 parse_msg_type / _classify_by_classname 的写法，从 msg.pyd 的常量里读到的
类名和分类顺序：mmui::ChatItemView → mmui::ChatBubbleItemView / mmui::ChatBubbleReferItemView，头像那一支是
mmui::ContactHeadView；_classify_by_name_prefix 按控件名开头分「图片」和「动画表情」两类，图片的控件名带「图片」，表情的带
「表情」——但气泡控件自己的名字里也可能带着这两个字（比如引用了一条表情），所以只看**没有子控件**的名字里带「表情」的叶子节点。
真机上具体是哪个 ClassName 由验证命令写进 result.json 的 `tree`（往下三层子元素的全表）。按这个顺序认：
  a. 消息控件往下三层里，没有子控件、名字里带「表情」的节点（图片是「图片」，文字消息名里带「表情」的少见）；
  b. 气泡（mmui::ChatBubbleItemView）里名字带「表情」的节点；
  c. 气泡里尺寸在 24～512 像素之间、长宽比在 0.25～4 之间的 ImageControl / PaneControl / GroupControl（表情是张图，不是一行字）；
  d. 都没有就退回整条消息的矩形。
"""
import hashlib
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO

from .stickers import dhash
from .winpos import unhide
from .wxprobe import rect_of
from .wxsticker import SETTLE, SHOTS  # 同一块矩形连截几次、每次之间等多久：沿用同步自定义表情时现成的设定

MIN_SIDE = 24  # 宽或高比这还小：不截
CORNER = 3  # 查中心之外，四个角往里收这么多像素再查
HEAD = ("mmui::ContactHeadView",)  # 头像那一支：退回整条消息的矩形时，这一侧要去掉
BUBBLE = ("mmui::ChatBubbleItemView", "mmui::ChatBubbleReferItemView")
EMOTION_TYPES = ("ImageControl", "PaneControl", "GroupControl", "CustomControl")
BG_DIFF = 40  # 和背景色差得够多的像素才算「有内容」：微信浅灰底上表情的线条、色块差得远不止 40
GAP = 8  # 有内容的竖条之间空这么多列，才算「这里是两段」（表情包内部的留白不会这么宽）
STRIP_GAP = 8  # 动图三帧并排时中间留的缝（像素）
POPOVER = "mmui::EmoticonPopover"  # 表情面板（PodMates 自己在操作它的时候不关任何小窗）
VK_ESCAPE = 0x1B


def default_grab(rect):
    """截屏幕上的一块（和 wxsticker.default_grab 一样：PIL ImageGrab，all_screens=True 管多显示器）。"""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=tuple(rect), all_screens=True)


class ShotError(RuntimeError):
    """这一块不能截（盖住了 / 太小 / 出界 / 截图本身出错）：调用方写「[表情]」，记一条 warn，不重试。
    checks 是那一刻第 3 条每个检查点的结果（验证命令要写出来）。"""

    def __init__(self, why: str, checks: dict | None = None):
        super().__init__(why)
        self.checks = checks or {}


@dataclass
class Shot:
    """一张截下来的表情：data 是交给看图的 PNG（动图是拼好的三帧）；key 是看图缓存的键（第一帧的 dhash，完全相同才算同一张）；
    rect 是截图的位置；crop 是裁到表情本身以后、在截图里的矩形；emotion 是裁图的依据（背景色、阈值、切出来的各段……）。"""
    data: bytes
    rect: tuple
    source: str  # emotion / bubble / row：截的是子元素、气泡还是整条消息
    key: str
    rects: list = field(default_factory=list)  # 查过 WindowFromPoint 的几个点（验证命令要写出来）
    checks: dict = field(default_factory=dict)  # 第 3 条每个检查点的结果（验证命令要写出来）
    crop: tuple = ()  # 裁到表情本身的矩形（截图坐标）；三帧都按它取
    emotion: dict = field(default_factory=dict)  # 第 1、2 条的细节：背景色、阈值、切段、选中哪段、三帧的 dhash、是不是动图
    first: bytes = b""  # 裁好的第一帧 PNG（静图就是 data；动图的验证命令用它存 emotion-shot.png）
    strip: bytes = b""  # 动图拼好的那张 PNG（第一帧时是空的）


def png_of(im) -> bytes:
    buf = BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def background(im) -> tuple:
    """截图的背景色：取四个角的颜色，四角一致就用它；不一致就用整张图里出现最多的那个颜色。
    返回 (RGB, 依据)。"""
    w, h = im.size
    corners = [im.getpixel((x, y))[:3] for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    if len(set(corners)) == 1:
        return corners[0], "四角同色"
    counts = im.convert("RGB").getcolors(maxcolors=1 << 24)
    if counts:
        return max(counts)[1], "四角不同色，取出现最多的颜色"
    return im.convert("RGB").getpixel((w // 2, h // 2)), "四角不同色，颜色太多，取中心的颜色"


def content_cols(im, bg: tuple, diff: int = BG_DIFF) -> list:
    """按列看有没有「有内容」的像素：和背景色三通道都差不到 diff 的才算背景。返回每一列有没有内容。"""
    w, h = im.size
    px = im.convert("RGB").load()
    br, bgc, bb = bg
    cols = [False] * w
    for x in range(w):
        for y in range(h):
            r, g, bl = px[x, y]
            if abs(r - br) > diff or abs(g - bgc) > diff or abs(bl - bb) > diff:
                cols[x] = True
                break
    return cols


def runs(flags: list, gap: int) -> list:
    """flags 里连续的 True 段（中间空不到 gap 个 False 的并成一段），返回 [(起, 止)]（含两端）。"""
    out, start, last = [], None, -1
    for i, on in enumerate(flags):
        if not on:
            continue
        if start is None:
            start = i
        elif i - last - 1 >= gap:
            out.append((start, last))
            start = i
        last = i
    if start is not None:
        out.append((start, last))
    return out


def tight_rows(im, box: tuple, bg: tuple, diff: int = BG_DIFF) -> tuple:
    """box 里按行收紧到有内容的上下边（返回新的矩形）；一行内容都没有就原样返回 box。"""
    l, t, r, b = box
    px = im.convert("RGB").load()
    br, bgc, bb = bg
    rows = [any(abs(px[x, y][0] - br) > diff or abs(px[x, y][1] - bgc) > diff or abs(px[x, y][2] - bb) > diff
                for x in range(l, r)) for y in range(t, b)]
    hit = [y for y, on in enumerate(rows) if on]
    return (l, t + hit[0], r, t + hit[-1] + 1) if hit else box


def split_nick(im, box: tuple, bg: tuple, diff: int = BG_DIFF):
    """群聊里昵称那一行在表情上方、和表情同一列范围：中间有一条空白就按行切开，把上半段（昵称）去掉。
    切不开返回 (box, False)。只切上下两段（再多就不动），而且上半段要比下半段矮一半以上才当昵称
    （昵称是一行小字，表情本身高得多）：这样表情自己中间的一条留白不会被误当成昵称。"""
    l, t, r, b = box
    px = im.convert("RGB").load()
    br, bgc, bb = bg
    blanks = [not any(abs(px[x, y][0] - br) > diff or abs(px[x, y][1] - bgc) > diff or abs(px[x, y][2] - bb) > diff
                      for x in range(l, r)) for y in range(t, b)]
    bands = runs([not z for z in blanks], 1)  # 有内容的行段（空一行就算断开：昵称和表情之间通常空好几行）
    if len(bands) != 2 or bands[0][0] != 0:
        return box, False
    top_h, low_h = bands[0][1] - bands[0][0] + 1, bands[1][1] - bands[1][0] + 1
    gap_h = bands[1][0] - bands[0][1] - 1
    if gap_h < 3 or top_h * 2 > low_h:  # 空白太窄、或者上半段不比下半段矮：不像昵称，留着
        return box, False
    cut = tight_rows(im, (l, t + bands[1][0], r, t + bands[1][1] + 1), bg, diff)
    return cut, True


def crop_emotion(im, min_side: int = MIN_SIDE, diff: int = BG_DIFF, gap: int = GAP) -> tuple:
    """裁到表情本身：返回 (裁好的图, 依据 dict)；找不到内容、裁出来太小返回 (None, 依据 dict)。
    依据里写背景色、阈值、切出来的各段（左右边）、选中哪一段、裁出来的矩形和尺寸、昵称那一行怎么处理了。"""
    bg, how = background(im)
    cols = content_cols(im, bg, diff)
    segs = runs(cols, gap)
    info = {"background": list(bg), "background_how": how, "diff": diff, "gap": gap, "segments": [list(s) for s in segs],
            "picked": None, "rect": None, "size": None, "nick": False, "why": ""}
    if not segs:
        info["why"] = "整张图找不到内容（全是背景色）"
        return None, info
    picked = max(segs, key=lambda s: s[1] - s[0])  # 头像是贴边的一个窄方块，表情包更宽
    info["picked"] = list(picked)
    box = tight_rows(im, (picked[0], 0, picked[1] + 1, im.size[1]), bg, diff)
    box, info["nick"] = split_nick(im, box, bg, diff)
    if info["nick"]:  # 昵称比表情宽时，选中的那段是按昵称的宽度切的：去掉昵称后左右也收紧到表情本身
        hit = [i for i, on in enumerate(content_cols(im.crop(box), bg, diff)) if on]
        box = (box[0] + hit[0], box[1], box[0] + hit[-1] + 1, box[3]) if hit else box
    w, h = box[2] - box[0], box[3] - box[1]
    info["rect"], info["size"] = list(box), [w, h]
    if w < min_side or h < min_side:
        info["why"] = f"裁出来太小（{w}×{h}，宽或高小于 {min_side} 像素）"
        return None, info
    return im.crop(box), info


def strip_of(frames: list, gap: int = STRIP_GAP):
    """把三帧从左到右拼成一张（中间留一条窄缝，缝用第一帧的背景色填）。"""
    h = max(f.size[1] for f in frames)
    w = sum(f.size[0] for f in frames) + gap * (len(frames) - 1)
    out = None
    x = 0
    for f in frames:
        if out is None:
            from PIL import Image
            out = Image.new("RGB", (w, h), f.convert("RGB").getpixel((0, 0)) if f.size[0] and f.size[1] else (240, 240, 240))
        out.paste(f.convert("RGB"), (x, 0))
        x += f.size[0] + gap
    return out


def shoot(rect: tuple, grab, min_side: int = MIN_SIDE, sleep=None, covered=None) -> Shot:
    """按同一个矩形连截 SHOTS 次（每次之间等 SETTLE）：每一帧都裁到表情本身，三帧都按第一帧裁出来的位置取，
    好让三帧对得上。三帧的 dhash 都一样是静图，只交第一帧；不一样就是动图，拼成一张交给看图。
    后两帧截图出错、被别的窗口盖住（covered() 说盖住了）、裁不出来，就当静图，只用第一帧。
    covered：截后两帧之前看一眼那块还是不是微信的（第 3 条只在第一帧前完整查一次，这里只看矩形中心一个点）。
    sleep 不传就不等（测试、验证命令用）。"""
    if sleep is None:
        sleep = lambda _s: None  # noqa: E731
    first = grab(rect)
    if first is None:
        raise ShotError("截图没有结果")
    cut, info = crop_emotion(first, min_side)
    if cut is None:
        raise ShotError(info.get("why") or "裁不到表情", {"crop": info})
    box = tuple(info["rect"])
    frames, hashes = [cut], [dhash(png_of(cut))]
    for i in range(1, SHOTS):
        sleep(SETTLE)
        if covered is not None and covered():
            break  # 窗口被别的窗口盖住了：当静图，只用第一帧
        try:
            im = grab(rect)
            one = im.crop(box) if im is not None else None
        except Exception:
            one = None
        if one is None or one.size != cut.size:
            break  # 后两帧截不到：当静图，只用第一帧
        frames.append(one)
        hashes.append(dhash(png_of(one)))
    moving = len(frames) > 1 and any(h != hashes[0] for h in hashes)
    first_png, data, strip = png_of(cut), png_of(cut), b""
    if moving:
        strip = png_of(strip_of(frames))
        data = strip
    info = {**info, "frames": [f"{h:016x}" if h is not None else None for h in hashes], "shots": len(frames), "moving": moving}
    return Shot(data=data, rect=tuple(rect), source="", key=cache_key(first_png), crop=box, emotion=info,
                first=first_png, strip=strip)


def cache_key(data: bytes) -> str:
    """截屏来的表情用它当看图缓存的键：dhash 完全相同才算同一张（截屏的背景、位置每次都有细微差别，sha1 几乎永远对不上）。
    dhash 算不出来（打不开的图）就退回 sha1，免得所有截坏的图挤成一条缓存。"""
    h = dhash(data)
    return f"dh:{h:016x}" if h is not None else "sha1:" + hashlib.sha1(data).hexdigest()


def kids(c) -> list:
    try:
        return list(c.GetChildren() or [])
    except Exception:
        return []


def walk3(c, depth: int = 3):
    """往下的前三层子控件（产出 (第几层, 控件)）；控件读不出来就当没有。"""
    stack, n = [(1, k) for k in reversed(kids(c))], 0
    while stack and n < 400:
        d, k = stack.pop()
        n += 1
        yield d, k
        if d < depth:
            stack.extend((d + 1, x) for x in reversed(kids(k)))


def cls_of(c) -> str:
    try:
        return str(c.ClassName or "")
    except Exception:
        return ""


def name_of(c) -> str:
    try:
        return str(c.Name or "")
    except Exception:
        return ""


def type_of(c) -> str:
    try:
        return str(c.ControlTypeName or "")
    except Exception:
        return ""


def size_ok(r, min_side: int = MIN_SIDE, max_side: int = 512) -> bool:
    w, h = r[2] - r[0], r[3] - r[1]
    return w >= min_side and h >= min_side and w <= max_side and h <= max_side and 0.25 <= w / max(1, h) <= 4


def describe(c) -> dict:
    """一个控件写成 dict（验证命令的 result.json 用）。"""
    r = rect_of(c)
    return {"class": cls_of(c), "name": name_of(c), "type": type_of(c), "rect": list(r) if r else None}


def emotion_box(control, min_side: int = MIN_SIDE):
    """消息控件里画表情的那个子元素（返回 (控件, 依据)）；认不出返回 (None, 原因)。规则见模块开头的 a～c。"""
    if control is None:
        return None, "这条消息没有控件"
    under = list(walk3(control, 3))
    for _, c in under:  # a
        if not kids(c) and "表情" in name_of(c) and rect_of(c):
            return c, "a 消息控件往下三层里，没有子控件、名字里带「表情」的节点"
    for _, c in under:  # b
        if cls_of(c) in BUBBLE:
            for _, k in [(0, c)] + list(walk3(c, 3)):
                if "表情" in name_of(k) and rect_of(k):
                    return k, "b 气泡里名字带「表情」的节点"
    for _, c in under:  # c
        if cls_of(c) in BUBBLE:
            for _, k in walk3(c, 2):
                if type_of(k) in EMOTION_TYPES and size_ok(rect_of(k) or (0, 0, 0, 0), min_side) and "头像" not in name_of(k):
                    return k, "c 气泡里像张图的控件（ImageControl / PaneControl，尺寸和长宽比都像表情）"
    return None, "认不出画表情的子元素"


def row_rect(control, min_side: int = MIN_SIDE):
    """退回整条消息的矩形，并去掉头像和名字那一侧（返回 (矩形, 依据)）；读不出来返回 (None, 原因)。
    整条消息是宽扁的一条，只要求宽高都不小于 min_side。"""
    r = rect_of(control)
    if not r:
        return None, "读不到这条消息的位置"
    left = r[0]
    for _, c in walk3(control, 2):  # 头像是贴着消息边的一小块：去掉它，剩下的就是气泡那一侧
        if cls_of(c) in HEAD:
            hr = rect_of(c)
            if hr and hr[0] <= r[0] + 2 and hr[2] > left:
                left = hr[2] + 4
                break
    out = (left, r[1], r[2], r[3])
    return (out if out[2] - out[0] >= min_side and out[3] - out[1] >= min_side else None), "整条消息的矩形去掉头像那一侧"


def probe_points(r: tuple) -> list:
    """查矩形中心，再查四个角往里收 CORNER 像素的位置。"""
    l, t, rr, b = r
    cx, cy = (l + rr) // 2, (t + b) // 2
    return [(cx, cy), (l + CORNER, t + CORNER), (rr - CORNER, t + CORNER), (l + CORNER, b - CORNER), (rr - CORNER, b - CORNER)]


def owner_hwnd(control, api):
    """这条消息所在的那个顶层窗口的句柄：从消息控件自己的句柄用 GetAncestor(GA_ROOT) 往上找；控件自己没有句柄
    就顺着父控件往上找（和 uiautomation 的 Control.GetTopLevelControl 一样：NativeWindowHandle 空了就 GetParentControl，
    拿到句柄再取顶层）。拿不到（没有句柄、接口读不出来、结果不是顶层窗口）返回 0：调用方按截不到处理，
    不退回「是微信进程就算」。"""
    c = control
    for _ in range(100):  # 控件自己 → 一直往上找到有句柄的那一层（微信 4.1 里消息控件自己没有句柄，离顶层窗口不止几层；100 只防死循环）
        if c is None:
            break
        try:
            h = int(getattr(c, "NativeWindowHandle", 0) or 0)
        except Exception:
            h = 0
        if h:
            try:
                top = int(api.root_of(h) or 0)
            except Exception:
                top = 0
            return top
        try:
            c = c.GetParentControl()
        except Exception:
            break
    return 0


def check_rect(r, api, mons, min_side: int = MIN_SIDE, owner: int = 0) -> dict:
    """第 3 条：够大、没跑出显示器、每个查点最上面的顶层窗口就是消息所在的那个窗口（owner）。
    返回 {size, size_ok, inside, owner, points, why}；每一点写 wechat（是不是微信进程的）和 hwnd / class / owner（是不是消息所在的窗口）。
    owner 为 0（拿不到消息所在的窗口）直接算不能截。"""
    w, h = r[2] - r[0], r[3] - r[1]
    res = {"size": [w, h], "size_ok": w >= min_side and h >= min_side, "inside": False, "owner": int(owner or 0),
           "points": [], "why": ""}
    if not res["size_ok"]:
        res["why"] = f"太小（{w}×{h}，宽或高小于 {min_side} 像素）"
        return res
    res["inside"] = any(r[0] >= m[0] and r[1] >= m[1] and r[2] <= m[2] and r[3] <= m[3] for m in mons)
    if not res["inside"]:
        res["why"] = "跑出了所有显示器"
        return res
    if not owner:
        res["why"] = "拿不到这条消息所在的窗口"
        return res
    for x, y in probe_points(r):
        try:
            hwnd = int(api.top_at(x, y) or 0)
            ok = bool(api.is_wechat(hwnd))
            cls = str(api.class_of(hwnd) or "") if hwnd else ""
        except Exception as ex:
            res["points"].append({"xy": [x, y], "wechat": False, "hwnd": 0, "class": "", "owner": False,
                                  "error": f"{type(ex).__name__}: {ex}"})
            res["why"] = f"查不到（{x},{y}）那里是谁的窗口"
            return res
        point = {"xy": [x, y], "wechat": ok, "hwnd": hwnd, "class": cls, "owner": hwnd == int(owner)}
        res["points"].append(point)
        if not point["owner"]:
            if not ok:
                res["why"] = f"（{x},{y}）那里不是微信的窗口（被别的窗口盖住了）"
            else:
                res["why"] = f"（{x},{y}）那里是微信的另一个窗口（{cls or '?'}，句柄 {hwnd}），不是这条消息所在的窗口"
            return res
    return res


def scroll_in(m):
    """这条消息不在可见区域里：让它滚到看得见的地方（wxauto4 的消息对象自带这个方法）。滚不动不抛错。"""
    fn = getattr(m, "roll_into_view", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def matches(a, b) -> bool:
    """两个矩形有没有重叠。"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _menu_like(win: dict, owner: int) -> bool:
    """这个窗口是不是「微信自己的小窗」：不是消息所在的窗口；类名带 QWindowIcon 的都不动（主窗口、独立聊天窗口），
    老的 mmui::MainWindow 也不动。"""
    cls = str(win.get("class") or "")
    return int(win.get("hwnd") or 0) != int(owner or 0) and "QWindowIcon" not in cls and cls != "mmui::MainWindow"


def menus(rect, api, owner: int, win_list=None) -> list[dict]:
    """盖在 rect 上的微信自己的小窗（看得见的、不是消息所在的窗口、不是聊天窗口、和矩形有重叠）。
    win_list 不传就现查一次微信进程的顶层窗口（测试里换成桩）。"""
    out = []
    for win in win_list if win_list is not None else api.windows():
        if not win.get("visible") or not _menu_like(win, owner):
            continue
        if not matches(win.get("rect") or (0, 0, 0, 0), rect):
            continue
        out.append(win)
    return out


def popover_open(win_list) -> bool:
    """表情面板（mmui::EmoticonPopover）开着没有：有看得见的这种窗口就算开着（面板自己关掉以后窗口还在进程里，
    所以只看可见性，和 wxprobe.popup_roots 一样）。"""
    return any(str(w.get("class") or "") == POPOVER and w.get("visible") for w in win_list or [])


def close_menus(rect, api, owner: int, sleep=None):
    """截之前先关掉盖在 rect 上的微信自己的小窗：每个窗口先只发 Esc、等 SETTLE，看不见了就不试下一种；
    还看得见就只发 WM_CLOSE、再等 SETTLE。两种都关不掉的写进返回值的 stuck，调用方按截不到处理。
    返回 {"tried": [...], "stuck": [...]}；tried 里每一项写句柄、类名、矩形、用了哪种办法、关掉没有。
    和 esc_menus 共用一把锁：读完消息关菜单（微信线程 / 监听线程）和这里截表情前关菜单（微信线程 / 下载线程）
    不会同时往窗口里发东西。"""
    if sleep is None:
        sleep = lambda _s: None  # noqa: E731
    with _ESC_LOCK:
        win_list = api.windows()
        res = {"tried": [], "stuck": []}
        if popover_open(win_list):  # 表情面板是 PodMates 自己在操作：这时不碰任何小窗，这张按截不到处理
            res["stuck"].append({"hwnd": 0, "class": POPOVER, "rect": [], "how": "", "closed": False,
                                 "why": "表情面板开着（PodMates 自己在操作它），不关任何小窗"})
            return res
        for win in menus(rect, api, owner, win_list):
            item = {"hwnd": win["hwnd"], "class": win.get("class") or "", "rect": list(win.get("rect") or (0, 0, 0, 0)),
                    "how": "", "closed": False}
            for how in ("esc", "close"):
                try:
                    if how == "esc":
                        api.post_key(win["hwnd"], VK_ESCAPE)
                    else:
                        api.post_close(win["hwnd"])
                except Exception as ex:
                    item["error"] = f"{type(ex).__name__}: {ex}"
                sleep(SETTLE)
                try:
                    gone = not api.is_visible(win["hwnd"])
                except Exception:
                    gone = False
                item["how"], item["closed"] = how, bool(gone)
                if gone:
                    break
            res["tried"].append(item)
            if not item["closed"]:
                res["stuck"].append(item)
        return res


_TROUBLE: dict = {}  # 读完消息关菜单这一步：同一种错、同一个关不掉的菜单，这次运行里只记一次
_TROUBLE_LOCK = threading.Lock()
_ESC_LOCK = threading.RLock()  # 微信线程、下载线程、wxauto4 监听线程都可能来关菜单：一次只让一个线程碰窗口


def _once(key) -> bool:
    """这个键是不是第一次见（是就返回 True，调用方去记日志）。"""
    with _TROUBLE_LOCK:
        if key in _TROUBLE:
            return False
        _TROUBLE[key] = True
        return True


def forget_trouble():
    """下一次运行从零开始记（测试用）。"""
    with _TROUBLE_LOCK:
        _TROUBLE.clear()


def esc_menus(rect, api, owner: int, log=None, sleep=None, win_list=None) -> dict:
    """读完微信消息之后，对微信自己弹出来的小菜单（头像旁边的「@某某／拍一拍」）发一次 Esc，只发给菜单。

    - 哪些窗口：和 menus() 一样的认法（微信进程的、看得见的顶层窗口，不是消息所在的窗口 owner，类名不带 QWindowIcon、
      不是 mmui::MainWindow，和 rect 有重叠）；owner 拿不到（0）就这次不做，不退回「微信进程的都算」。
    - 只发 Esc（WM_KEYDOWN/WM_KEYUP VK_ESCAPE），等 SETTLE 再看它是不是看不见了；不发 WM_CLOSE（每轮都硬关，
      可能让微信以后弹不出这个菜单）。关不掉的留着，同一个句柄这次运行只记一条 warn（句柄、类名、矩形）。
    - 表情面板（mmui::EmoticonPopover）开着时不做：PodMates 自己在操作它。
    - 发完之后主窗口看不见了（被藏进托盘 / 最小化了）：记一条 warn，并用现有 unhide() 显示出来（不抢前台焦点）。
    - 任何错只记日志（同一种错这次运行只记一次），不往外抛：不能影响读消息、发消息、截表情。
    返回 {"skipped": 原因, "menus": [{hwnd, class, rect, closed}], "main": {hwnd, visible, unhidden}}（验证命令写进 result.json）。
    win_list 不传就现查一次微信进程的顶层窗口（测试里换成桩）。"""
    if sleep is None:
        sleep = lambda _s: None  # noqa: E731
    out = {"skipped": "", "menus": [], "main": {}}
    if not owner:
        out["skipped"] = "拿不到消息所在的窗口"
        return out
    with _ESC_LOCK:  # 微信线程、下载线程、监听线程都可能来这一步：一次只让一个线程碰窗口
        return _esc_menus(rect, api, owner, log, sleep, win_list, out)


def _esc_menus(rect, api, owner: int, log, sleep, win_list, out: dict) -> dict:
    """esc_menus 的本体（已拿着 _ESC_LOCK）。"""
    try:
        win_list = win_list if win_list is not None else api.windows()
    except Exception as ex:
        if log is not None and _once(("windows", type(ex).__name__, str(ex))):
            log("warn", f"微信：读完消息后没列出窗口（{type(ex).__name__}: {ex}），这次不关小菜单")
        out["skipped"] = f"列不出窗口（{type(ex).__name__}: {ex}）"
        return out
    if popover_open(win_list):
        out["skipped"] = "表情面板开着（PodMates 自己在操作它）"
        return out
    try:
        found = menus(rect, api, owner, win_list)
    except Exception as ex:
        if log is not None and _once(("menus", type(ex).__name__, str(ex))):
            log("warn", f"微信：读完消息后查小菜单出错（{type(ex).__name__}: {ex}），这次不关")
        out["skipped"] = f"查小菜单出错（{type(ex).__name__}: {ex}）"
        return out
    for win in found:
        hwnd = int(win.get("hwnd") or 0)
        item = {"hwnd": hwnd, "class": win.get("class") or "", "rect": list(win.get("rect") or (0, 0, 0, 0)), "how": "esc",
                "closed": False}
        try:
            api.post_key(hwnd, VK_ESCAPE)
            sleep(SETTLE)
            item["closed"] = not api.is_visible(hwnd)
        except Exception as ex:
            item["error"] = f"{type(ex).__name__}: {ex}"
            if log is not None and _once(("esc", hwnd, type(ex).__name__, str(ex))):
                log("warn", f"微信：对小菜单句柄 {hwnd} 发 Esc 出错（{type(ex).__name__}: {ex}），留着不管")
        out["menus"].append(item)
        if not item["closed"] and "error" not in item and log is not None and _once(("stuck", hwnd)):
            r = item["rect"]
            rect_txt = f"{r[0]},{r[1]},{r[2]},{r[3]}" if len(r) == 4 else "?"
            log("warn", f"微信：读完消息后对菜单发了 Esc 还是关不掉（句柄 {hwnd}，类名 {item['class'] or '?'}，矩形 {rect_txt}），留着不管")
    if found:  # 只有真发过 Esc 才看主窗口：没动过它就不用查
        try:
            try:
                hwnd = api.find_main(visible_only=False)  # 藏进托盘的窗口只有不看可见性才找得到
            except TypeError:  # 桩 / 老接口不认这个参数
                hwnd = api.find_main()
            visible = bool(hwnd) and api.is_visible(hwnd) and not api.is_iconic(hwnd)  # 最小化的窗口 IsWindowVisible 仍为真
            out["main"] = {"hwnd": hwnd, "visible": visible, "unhidden": False}
            if hwnd and not visible:
                err = ""
                try:
                    out["main"]["unhidden"] = bool(unhide(api))  # 只显示、还原，不抢前台焦点
                except Exception as ex:
                    err = f"（重新显示时出错：{type(ex).__name__}: {ex}）"
                if _once(("main_hidden", int(hwnd))) and log is not None:
                    log("warn", "微信：对菜单发 Esc 之后主窗口看不见了（被藏进托盘或最小化了），"
                        + ("已经重新显示" if out["main"]["unhidden"] else "没能重新显示") + err)
        except Exception as ex:
            if log is not None and _once(("main", type(ex).__name__, str(ex))):
                log("warn", f"微信：对菜单发 Esc 之后没查出主窗口看不看得见（{type(ex).__name__}: {ex}）")
    return out


def _covered(rect: tuple, api, owner: int = 0):
    """截后两帧之前看一眼那块还是消息所在的那个窗口：只看矩形中心一个点，不是了就返回 True（当静图，只用第一帧）。
    查不出来（接口出错）就当没盖住：这一点只用来决定要不要接着截，不值得为此丢掉已经截好的第一帧。"""
    x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2

    def check() -> bool:
        try:
            return int(api.top_at(x, y) or 0) != int(owner or 0)
        except Exception:
            return False
    return check


def capture(control, api, grab, mons, min_side: int = MIN_SIDE, sleep=None) -> Shot:
    """截这条消息里的表情：认子元素、关掉盖在上面的微信小菜单、查第 3 条、截图、裁到表情本身、连截三帧。
    不准截就抛 ShotError（带原因），调用方写「[表情]」并记一条 warn。
    第 3 条只在截第一帧前查一次；后两帧被盖住、截不到都当静图，只用第一帧。
    api：窗口接口（要知道某个点上是哪个窗口、消息所在的窗口是哪个、有哪些顶层窗口）；grab：截图函数（验证命令和测试里换成桩）；
    sleep：关小窗之后、两帧之间等多久用（关小窗的等待用 SETTLE）。"""
    owner = owner_hwnd(control, api)
    box, why = emotion_box(control, min_side)
    src, rect = ("emotion", rect_of(box)) if box is not None else (None, None)
    if rect is None:
        rect, why = row_rect(control, min_side)
        if rect is None:
            raise ShotError(why)
        src = "row"
    checks = check_rect(rect, api, mons, min_side, owner)
    if not checks.get("size_ok") or not checks.get("inside") or not checks.get("owner"):
        raise ShotError(checks.get("why") or "这一块不能截", checks)
    try:
        closed = close_menus(rect, api, owner, sleep)
    except Exception as ex:
        raise ShotError(f"关不掉盖在上面的微信小窗（{type(ex).__name__}: {ex}），这一块不截", checks) from ex
    checks["menus"] = closed["tried"]
    if closed["stuck"]:
        win = closed["stuck"][0]
        if not win.get("hwnd"):
            raise ShotError(str(win.get("why") or "表情面板开着，不关小窗"), checks)
        r = win.get("rect") or []
        rect_txt = f"{r[0]},{r[1]},{r[2]},{r[3]}" if len(r) == 4 else "?"
        raise ShotError(f"盖在上面的微信小窗关不掉（句柄 {win.get('hwnd')}，类名 {win.get('class') or '?'}，"
                        f"矩形 {rect_txt}），这一块不截", checks)
    checks = check_rect(rect, api, mons, min_side, owner)  # 小窗关掉之后再查一遍
    checks["menus"] = closed["tried"]
    if not all(p.get("owner") for p in checks["points"]):
        raise ShotError(checks.get("why") or "这一块不能截", checks)
    try:
        shot = shoot(rect, grab, min_side, sleep, covered=_covered(rect, api, owner))
    except ShotError as ex:
        raise ShotError(str(ex), {**checks, **ex.checks}) from ex
    except Exception as ex:
        raise ShotError(f"截图出错（{type(ex).__name__}: {ex}）") from ex
    shot.source, shot.checks = src, checks
    shot.rects = [list(p["xy"]) for p in checks.get("points") or []]
    return shot
