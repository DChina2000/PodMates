"""微信图片：认出来、下载下来，和验证命令。

微信 4.1.15.56 在聊天里把图片显示成「图片」两个字（不带方括号），wxauto4 靠「[图片]」认图，所以把它归成 OtherMessage
（type == "other"，content == "图片"）。这里把这种消息当成图片，并给两种下载办法：
  A：用这条消息的 control 和 parent 造一个 wxauto4 的 ImageMessage，调它的 download()；
  B：右键这条消息 →「复制」→ 读剪贴板里的图片（wxauto4 的 ReadClipboardData：{剪贴板格式号: 数据}）。
     剪贴板里可能是 PNG/JPEG 原始字节、一份文件路径（CF_HDROP，格式号 15）或位图（CF_DIB 8 / CF_DIBV5 17）三种；谁有取谁。
     复制前先清空剪贴板（免得读到上一次留下的），先把消息滚到看得见的地方，复制后每 0.3 秒读一次、最多等 3 秒。
A 抛错或没下到东西才试 B。
表情不走 A、B：微信里表情和图片的处理方式不同，表情点不开大图、也没法另存（真机上 A 报错、B 的右键菜单里没有「复制」），
所以表情改成截聊天里那一块交给看图流程，见 wxshot.py。

`PodMates.exe --test-wechat-picture <输出目录>`（PodMates 主程序没在运行时）在服务器上验证：只在「文件传输助手」里操作——
发一张测试小图 → 读最后几条消息 → 找出最新的图片 → A、B 各试一遍（各最多等 PIC_TIMEOUT 秒）
→ 再照 wxsticker 的现有办法往文件传输助手发一张自定义表情（面板第一屏的第一格），从最后几条里找最新的一条表情消息：
把这条消息控件往下三层子元素的 ClassName / Name / ControlType / 矩形写进 result.json，按 wxshot 的办法截图存成 emotion-shot.png，
整条消息的矩形另外截一张存成 emotion-row.png（最后几条里没有表情就写「没有表情消息」，不算失败）→ 切回原来的会话
→ 再做一步 menu：在原来那个会话里只读地查清头像旁边的右键菜单是谁打开的、哪种办法关得掉（每一步都列一次微信进程的顶层窗口），
主窗口的截图存成 menu-before.png / menu-after.png；
→ 最后一步 exact_switch：只切换、不发送——从数据目录读出用户卡上的微信秘密备注，每个都用精确匹配 ChatWith 试一次，
记下切完之后 ChatInfo() 的全部字段和算不算核对通过；再试一个肯定不存在的名字，看 ChatWith 是报错、返回什么、还是留在原会话，
记下之后 ChatInfo() 读到的是谁；最后切回原来的会话。这一步出错只记下来，不算命令失败。
每一步写进 probe.log 和 result.json。不取单实例互斥体，不启动引擎，不碰 NapCat 和数据库。
"""
import io
import json
import re
import struct
import sys
import threading
import time
from pathlib import Path

from . import wxshot as shot_mod
from .wxprobe import CHAT_TARGET, SafeWx
from .wxsticker import WxStickers

PIC_WORD = "图片"  # 微信 4.1.15.56 里图片消息显示的字
EMOTION_TEXT = re.compile(r"\[?(动画)?表情\]?")  # 内容是「[表情]」「[动画表情]」这类的不是图片，不下载（表情改成截屏）
COPY_ITEM = "复制"
IMG_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
IMG_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
CLIP_WAIT = 3.0  # 点了「复制」之后，最多等剪贴板里出现图片多久（秒）
CLIP_STEP = 0.3
TAIL = 8  # 验证命令：读聊天里最后几条


def is_picture(m) -> bool:
    """一条消息是不是图片：type == "image"（内容不是表情），或者微信 4.1.15 的写法 type == "other"、content == 「图片」。
    有人打字发的「图片」两个字是 type == "text"，不算。"""
    mtype = getattr(m, "type", "")
    content = str(getattr(m, "content", "") or "").strip()
    if mtype == "other":
        return content == PIC_WORD
    return mtype == "image" and not EMOTION_TEXT.fullmatch(content)


def is_emotion(m) -> bool:
    """一条消息是不是表情：type == "emotion"，或者 type == "other" 且内容是「动画表情」这类字。"""
    mtype = getattr(m, "type", "")
    return mtype == "emotion" or (mtype == "other" and bool(EMOTION_TEXT.fullmatch(str(getattr(m, "content", "") or "").strip())))


def twin(m):
    """OtherMessage / EmotionMessage → 同一个控件造出来的 ImageMessage（同属好友/自己那一族，带右键等方法）；本来就是图片的原样返回。"""
    if getattr(m, "type", "") == "image":
        return m
    name = type(m).__name__
    mod = sys.modules.get(type(m).__module__)
    cls = getattr(mod, re.sub("Other|Emotion", "Image", name), None) if re.search("Other|Emotion", name) else None
    if cls is None:
        import importlib
        cls = importlib.import_module("wxauto4.msgs.mtype").ImageMessage
    return cls(m.control, m.parent)


def fetch_a(m) -> bytes:
    """A：ImageMessage.download()。读进内存后立刻删掉下载下来的文件；没下到东西抛错。"""
    path, res = None, None
    try:
        res = twin(m).download()
        path = res if isinstance(res, (str, Path)) else (res.get("data") if hasattr(res, "get") else None)
        if isinstance(path, dict):
            path = path.get("path") or path.get("file")
        data = Path(path).read_bytes() if path else b""
    finally:
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
    if not data:
        raise RuntimeError(f"没下到图片（download 返回 {str(res)[:60]}）")
    return data


class Clipboard:
    def clear(self):
        import win32clipboard as w
        w.OpenClipboard()
        try:
            w.EmptyClipboard()
        finally:
            w.CloseClipboard()

    def read(self) -> dict:
        import importlib
        return importlib.import_module("wxauto4.utils.win32").ReadClipboardData() or {}


def dib_to_png(dib: bytes) -> bytes:
    """剪贴板里的 CF_DIB / CF_DIBV5（没有 14 字节文件头的 BMP）→ PNG 字节。"""
    from PIL import Image
    hsize = struct.unpack_from("<I", dib, 0)[0]
    bits, comp, used = struct.unpack_from("<H", dib, 14)[0], struct.unpack_from("<I", dib, 16)[0], struct.unpack_from("<I", dib, 32)[0]
    masks = {3: 12, 6: 16}.get(comp, 0) if hsize == 40 else 0
    palette = used * 4 if used else (4 << bits if bits <= 8 else 0)
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + hsize + masks + palette) + dib
    out = io.BytesIO()
    Image.open(io.BytesIO(bmp)).convert("RGB").save(out, "PNG")
    return out.getvalue()


def image_from_clipboard(dump: dict) -> bytes:
    """ReadClipboardData() 的结果 → 图片字节；没有图返回空。先找 PNG/JPEG/GIF 原始字节，再找文件路径，最后是位图。"""
    items = {str(k): v for k, v in (dump or {}).items()}
    for v in items.values():
        if isinstance(v, (bytes, bytearray)) and bytes(v[:8]).startswith(IMG_MAGIC):
            return bytes(v)
    files = items.get("15")
    for p in ([files] if isinstance(files, (str, Path)) else list(files or []) if isinstance(files, (list, tuple)) else []):
        if str(p).lower().endswith(IMG_SUFFIX) and Path(p).is_file():
            return Path(p).read_bytes()
    for k in ("8", "17"):
        v = items.get(k)
        if isinstance(v, (bytes, bytearray)) and len(v) > 40:
            try:
                return dib_to_png(bytes(v))
            except Exception:
                continue
    return b""


def clip_summary(dump: dict) -> str:
    return "、".join(f"{k}（{type(v).__name__} {len(v) if hasattr(v, '__len__') else v}）" for k, v in (dump or {}).items()) or "空的"


def fetch_b(m, clip=None, sleep=time.sleep, wait: float | None = None) -> bytes:
    """B：右键 →「复制」→ 读剪贴板。没复制成、剪贴板里没有图都抛错（错误里带上剪贴板里实际有哪些格式）。"""
    clip = clip or Clipboard()
    if not callable(getattr(m, "select_option", None)):
        raise RuntimeError("这条消息没有右键菜单的接口（select_option）")
    try:
        m.roll_into_view()
    except Exception:
        pass  # 滚不动也照样试：右键时 wxauto4 自己也会找位置
    clip.clear()
    resp = m.select_option(COPY_ITEM)
    if resp is not None and not resp:
        raise RuntimeError(f"右键菜单里没点到「{COPY_ITEM}」：{str(resp)[:80]}")
    end, dump, err = time.time() + (CLIP_WAIT if wait is None else wait), {}, ""
    while True:
        try:
            dump = clip.read()
            data = image_from_clipboard(dump)
            if data:
                return data
        except Exception as ex:
            err = f"；读剪贴板出错 {type(ex).__name__}: {ex}"
        if time.time() >= end:
            raise RuntimeError(f"点了「{COPY_ITEM}」之后剪贴板里没有图片（有这些格式：{clip_summary(dump)}{err}）")
        sleep(CLIP_STEP)


def fetch(m, log, clip=None) -> bytes:
    """图片：先 A 后 B；每次成功在 info 日志里写明用的是哪种。两种都不成记一条 warn 并返回空（这张不再重试）。
    表情不走这里：微信里表情点不开大图、也没法另存（真机上 A、B 都下不了），改用截屏（见 wxshot.py）。"""
    errs = []
    for way, fn in (("A", fetch_a), ("B", lambda x: fetch_b(x, clip))):
        try:
            data = fn(m)
        except Exception as ex:
            errs.append(f"{way}：{type(ex).__name__}: {ex}")
            continue
        log("info", f"微信：图片下载成功（方式 {way}，{len(data)} 字节）")
        return data
    log("warn", f"微信：下载图片失败（{'；'.join(errs)}），这张按「[图片]」处理")
    return b""


# ---------- 验证命令 ----------
class WxForPicture(SafeWx):
    """验证命令用的微信包装：读当前会话、切会话、读消息，和只往「文件传输助手」发文件；别的一概不让碰。"""
    ALLOWED = ("ChatInfo", "ChatWith", "GetAllMessage")

    def SendFiles(self, path, who):
        if who != CHAT_TARGET:
            raise PermissionError(f"验证命令只许往「{CHAT_TARGET}」发文件，不许发给「{who}」")
        self._calls.append(("SendFiles", (path, who)))
        return self._wx.SendFiles(path, who=who)


def user_wechat_remarks() -> list[str]:
    """从数据目录读出作者（「用户」那张人物卡）的微信秘密备注，按卡上的顺序。和 PodMates 读人物表一样，
    读的是数据目录里同一个库（只读打开，不迁移、不写任何东西、不另存一份）。读不出来返回空表。"""
    try:
        import json
        import sqlite3
        from .paths import data_dir
        conn = sqlite3.connect(f"file:{(data_dir() / 'podmates.db').as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT data FROM docs WHERE kind='person' ORDER BY ord, rowid").fetchall()
        finally:
            conn.close()
        cards = [json.loads(r[0]) for r in row]
        p = next((c for c in cards if c.get("self")), None)
        return [str(x) for x in (p.get("wechat_remark") or []) if str(x).strip()] if p else []
    except Exception:
        return []


def exact_switch(wx, remarks: list[str], log, sleep=time.sleep) -> dict:
    """验证命令最后一步：只切换、读 ChatInfo()，不发任何东西、不点任何消息，查清精确切换在真机上的表现。
    每个备注都用精确匹配 ChatWith 试一次；再试一个肯定不存在的名字，看 ChatWith 是报错、返回什么、还是留在原会话。
    结果写进 result.json 的 exact_switch 一节，probe.log 每次切换写一行中文；出错只记下来，不算整个命令失败。
    wx 是 WxForPicture（只放行 ChatInfo / ChatWith / GetAllMessage，发不出去）。"""
    r = {"skip": "", "remarks": [], "missing": None, "errors": []}

    def oops(where: str, ex: Exception):
        r["errors"].append({"where": where, "error": f"{type(ex).__name__}: {ex}"})
        log(f"精确切换这一步：{where}出错（{type(ex).__name__}: {ex}），不算命令失败")

    def try_one(name: str, kind: str) -> dict:
        """切到 name，记下切完之后 ChatInfo() 的全部字段和按第 1 条的规则算不算核对通过。"""
        row = {"kind": kind, "want": name, "chat_with": "", "why": "", "opened": "", "chat_type": "",
               "info": {}, "matched": False}
        try:
            try:
                ret = wx.ChatWith(name)
                row["chat_with"] = "返回了" if ret is None else f"返回了 {ret!r}"[:200]
            except Exception as ex:
                row["chat_with"] = f"报错 {type(ex).__name__}: {ex}"[:300]
            for _ in range(5):
                try:
                    info = wx.ChatInfo() or {}
                except Exception as ex:
                    info = {}
                    row["why"] = f"读 ChatInfo 出错 {type(ex).__name__}: {ex}"[:300]
                row["info"] = dict(info)
                row["opened"] = str(info.get("chat_name") or "")
                row["chat_type"] = str(info.get("chat_type") or "")
                if row["opened"] == name:
                    break
                sleep(0.25)
            # 备注和真发消息时一样：名字一字不差，且不能是群；不存在的名字那一次只比名字
            row["matched"] = row["opened"] == name and (kind == "missing" or row["chat_type"] != "group")
        except Exception as ex:  # 这一步出错只记下来
            oops(f"切到「{name}」", ex)
        typ = f"（chat_type={row['chat_type']}）" if row["chat_type"] else "（没有 chat_type）"
        said = (f"精确切到「{name}」：ChatWith {row['chat_with'] or '没有记录'}；切完开着的是「{row['opened']}」{typ}；"
                f"按第 1 条的规则{'核对通过' if row['matched'] else '核对不上'}")
        log(said)
        return row

    try:
        if wx is None:
            r["skip"] = "没连上微信，跳过"
            log(f"精确切换这一步：{r['skip']}")
            return r
        if not remarks:
            r["skip"] = "用户卡上没有微信秘密备注，跳过"
            log(f"精确切换这一步：{r['skip']}")
            return r
        for name in remarks:
            r["remarks"].append(try_one(name, "remark"))
        missing = f"PodMates-不存在的会话-{int(time.time())}"
        r["missing"] = try_one(missing, "missing")
        log(f"精确切换这一步：试了 {len(r['remarks'])} 个备注和一个不存在的名字「{missing}」，只切换、读 ChatInfo()，什么都没发")
    except Exception as ex:  # 这一步出任何错都只记下来，不算整个命令失败
        oops("整步", ex)
    return r


def call_with_timeout(fn, timeout: float, com: bool = True):
    """在单独的线程里跑 fn，最多等 timeout 秒。返回 (成了没, 结果或错误文字, 用了几秒)。"""
    box: dict = {}

    def work():
        if com:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                pass
        try:
            box["v"] = fn()
        except Exception as ex:
            box["e"] = f"{type(ex).__name__}: {ex}"

    t0 = time.time()
    t = threading.Thread(target=work, name="wechat-pic-test", daemon=True)
    t.start()
    t.join(timeout)
    took = round(time.time() - t0, 2)
    if t.is_alive():
        return False, f"超过 {timeout} 秒没有结果", took
    return ("v" in box), box.get("v", box.get("e", "")), took


def test_image(path: Path):
    """生成命令自带的测试小图（有明显的渐变和一道斜线，便于对像素）。"""
    from PIL import Image
    im = Image.new("RGB", (96, 64))
    im.putdata([((x * 255) // 95, (y * 255) // 63, (x * 7 + y * 13) % 256) for y in range(64) for x in range(96)])
    for i in range(48):
        im.putpixel((i, i), (255, 255, 255))
    im.save(path, "PNG")
    return im


def compare(sent_path: Path, got: bytes) -> dict:
    """下到的图和发出去的测试图：尺寸、像素是否一致。"""
    from PIL import Image
    a = Image.open(sent_path).convert("RGB")
    b = Image.open(io.BytesIO(got)).convert("RGB")
    return {"sent_size": list(a.size), "got_size": list(b.size), "same_size": a.size == b.size, "same_pixels": a.size == b.size and a.tobytes() == b.tobytes()}


def ext_of(data: bytes) -> str:
    return ".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:3] == b"\xff\xd8\xff" else ".gif" if data[:3] == b"GIF" else ".bin"


def frames_of(data: bytes) -> int:
    """图片有几帧；打不开返回 0。"""
    try:
        from PIL import Image
        return int(getattr(Image.open(io.BytesIO(data)), "n_frames", 1))
    except Exception:
        return 0


def rows_of(msgs) -> list:
    return [{"type": str(getattr(m, "type", "") or ""), "attr": str(getattr(m, "attr", "") or ""), "content": str(getattr(m, "content", "") or "")[:60]} for m in msgs]


def message_tree(control, depth: int = 3) -> list:
    """这条消息控件往下三层子元素的 ClassName、Name、ControlType、矩形（验证命令写进 result.json）。"""
    out = []
    for d, c in shot_mod.walk3(control, depth):
        r = shot_mod.rect_of(c)
        out.append({"depth": d, "class": shot_mod.cls_of(c), "name": shot_mod.name_of(c), "type": shot_mod.type_of(c),
                    "rect": list(r) if r else None, "children": len(shot_mod.kids(c))})
    return out


def emotion_step(emo, out: Path, api, grab, log, min_side: int = shot_mod.MIN_SIDE, sleep=None) -> dict:
    """验证命令的表情一步：把这条消息往下三层子元素写出来，按第 2、3 条截图、裁到表情本身、连截三帧
    （emotion-shot.png 存裁好的第一帧，动图另存拼好的 emotion-strip.png），整条消息的矩形再截一张（emotion-row.png）。"""
    r = {"found": True, "tree": message_tree(emo.control), "source": "", "why": "", "checks": {}, "shot": "", "row": "",
         "strip": "", "crop": None, "size": None, "background": None, "diff": shot_mod.BG_DIFF, "segments": [],
         "segment": None, "frames": [], "moving": False, "nick": False}
    box, why = shot_mod.emotion_box(emo.control, min_side)
    r["source"], r["why"] = ("emotion", why) if box is not None else ("row", f"{why}：退回整条消息的矩形")
    try:
        shot = shot_mod.capture(emo.control, api, grab, api.monitors(), min_side, sleep)  # 三帧之间照真用时一样等
        (out / "emotion-shot.png").write_bytes(shot.first or shot.data)
        e = shot.emotion
        r.update(shot="emotion-shot.png", source=shot.source, rect=list(shot.rect), crop=list(shot.crop),
                 size=e.get("size") or [], background=e.get("background"), background_how=e.get("background_how") or "",
                 diff=e.get("diff"), gap=e.get("gap"), segments=e.get("segments") or [], segment=e.get("picked"),
                 nick=bool(e.get("nick")), frames=e.get("frames") or [], shots=e.get("shots") or 0, moving=bool(e.get("moving")),
                 dhash=shot.key, checks=shot.checks, points=shot.rects, box=shot_mod.describe(box) if box is not None else None)
        if shot.strip:
            (out / "emotion-strip.png").write_bytes(shot.strip)
            r["strip"] = "emotion-strip.png"
        log(f"表情已截成 emotion-shot.png（截图 {r['rect'][2] - r['rect'][0]}×{r['rect'][3] - r['rect'][1]}，裁到 {r['size'][0]}×{r['size'][1]}，"
            f"截的是{'子元素' if shot.source == 'emotion' else '整条消息去掉头像那一侧'}，背景 {'、'.join(str(x) for x in r['background'])}"
            f"（{r['background_how']}），阈值 {r['diff']}，切出 {len(r['segments'])} 段，选第 {r['segments'].index(r['segment']) + 1 if r['segment'] in r['segments'] else '?'} 段"
            f"{'，去掉了昵称那一行' if r['nick'] else ''}；{'动图，三帧拼成 emotion-strip.png' if r['moving'] else '静图'}，"
            f"dhash {shot.key}，三帧 {'、'.join(str(x) for x in r['frames'])}）")
    except shot_mod.ShotError as ex:
        r.update(why=f"{ex}", checks=getattr(ex, "checks", {}))
        e = (getattr(ex, "checks", {}) or {}).get("crop") or {}
        r.update(background=e.get("background"), background_how=e.get("background_how") or "", diff=e.get("diff") or shot_mod.BG_DIFF,
                 segments=e.get("segments") or [], segment=e.get("picked"), size=e.get("size") or [])
        log(f"表情截不了：{ex}")
    row, row_why = shot_mod.row_rect(emo.control, min_side)
    owner = shot_mod.owner_hwnd(emo.control, api)
    rc = shot_mod.check_rect(row, api, api.monitors(), min_side, owner) if row else {"why": row_why}
    r["row_checks"] = rc
    if row and rc.get("size_ok") and rc.get("inside") and all(p.get("owner") for p in rc["points"]):
        try:
            im = grab(row)
            im.convert("RGB").save(out / "emotion-row.png")
            r["row"] = "emotion-row.png"
            log(f"整条消息的矩形已截成 emotion-row.png（{row[2] - row[0]}×{row[3] - row[1]}）")
        except Exception as ex:
            r["row_why"] = f"截图出错（{type(ex).__name__}: {ex}）"
            log(f"整条消息的矩形截不了：{ex}")
    else:
        r["row_why"] = rc.get("why") or row_why
        log(f"整条消息的矩形截不了：{r['row_why']}")
    return r


def _win_rows(api, errors: list | None = None) -> list:
    """微信进程的顶层窗口写成 result.json 的行（句柄、类名、标题、矩形、可见）；读不出来照实写。"""
    try:
        rows = api.windows()
    except Exception as ex:
        err = f"{type(ex).__name__}: {ex}"
        if errors is not None:
            errors.append({"where": "列出微信进程的顶层窗口", "error": err})
        return [{"error": err}]
    return [{"hwnd": w.get("hwnd"), "class": w.get("class") or "", "title": w.get("title") or "",
             "rect": list(w.get("rect") or (0, 0, 0, 0)), "visible": bool(w.get("visible"))} for w in rows]


def _win_brief(w: dict) -> str:
    r = w.get("rect") or [0, 0, 0, 0]
    return f"句柄 {w.get('hwnd')} {w.get('class') or '?'}「{w.get('title') or ''}」{r[0]},{r[1]},{r[2]},{r[3]}{'（看得见）' if w.get('visible') else '（看不见）'}"


def _menu_area(api, hwnd_finder, msg=None):
    """查菜单时用它当「聊天区」：有最新一条别人发的消息就用它的整条矩形，没有就用主窗口的矩形。"""
    if msg is not None:
        r = shot_mod.rect_of(getattr(msg, "control", None))
        if r:
            return r, "最新一条别人发的消息的整条矩形"
    try:
        hwnd = hwnd_finder() if hwnd_finder else api.find_main()
        if hwnd:
            return api.rect(hwnd), "微信主窗口的矩形"
    except Exception:
        pass
    try:
        mons = api.monitors()
        if mons:
            return mons[0], "第一块显示器"
    except Exception:
        pass
    return (0, 0, 0, 0), "读不到聊天区（用 0,0,0,0）"


def menu_step(wx, orig: str, api, out: Path, grab, log, sleep=time.sleep, hwnd_finder=None, restored: bool = True) -> dict:
    """验证命令新加的一步：查清头像上那个右键菜单是谁打开的、哪种办法关得掉。
    只在命令开始时原来开着的那个会话里做，只读：不发任何东西、不点任何消息（别的方法调用不到，WxForPicture 只放行读会话和读消息）。
    没切回原来的会话（restored=False）、读不到原来的会话、原来是「文件传输助手」或私聊，都写明跳过。
    每一步之后都列一次微信进程的顶层窗口；第 1 步和最后各截一张主窗口的图（menu-before.png / menu-after.png）。
    第 3 步（GetAllMessage() 之后）走真跑时新加的那个函数（wxshot.esc_menus）：只对菜单发 Esc，不发 WM_CLOSE，
    记下用的是哪种办法、关没关掉、发完之后主窗口看不看得见。
    出错只记在这一节里，不算整个命令失败。"""
    r = {"skip": "", "area": "", "owner": 0, "steps": [], "closed": [], "shots": [], "errors": []}

    def oops(where: str, ex: Exception):
        """这一步里的错只记下来，不算整个命令失败。"""
        r["errors"].append({"where": where, "error": f"{type(ex).__name__}: {ex}"})
        log(f"菜单这一步：{where}出错（{type(ex).__name__}: {ex}），不算命令失败")

    def owner():
        try:
            return int(hwnd_finder() if hwnd_finder else api.find_main() or 0)
        except Exception:
            return 0

    def snap(name: str):
        """主窗口矩形的截图。"""
        try:
            hwnd = owner()
            rect = api.rect(hwnd) if hwnd else _menu_area(api, hwnd_finder)[0]
            im = grab(rect)
            if im is not None:
                im.convert("RGB").save(out / name)
                r["shots"].append(name)
                log(f"菜单这一步：主窗口的截图存成 {name}")
        except Exception as ex:
            r.setdefault("shot_errors", []).append({"file": name, "error": f"{type(ex).__name__}: {ex}"})
            log(f"菜单这一步：{name} 截图没成（{type(ex).__name__}: {ex}）")

    def esc_after(area: tuple) -> dict:
        """第 3 步：走真跑时新加的那个函数（wxshot.esc_menus），记下用的是哪种办法、关没关掉、发完之后主窗口看不看得见。"""
        try:
            got = shot_mod.esc_menus(area, api, r["owner"], log=lambda lv, tx: log(f"菜单这一步：{tx}"), sleep=sleep)
        except Exception as ex:
            oops("走新加的关菜单那一步", ex)
            return {"skip": f"出错（{type(ex).__name__}: {ex}）", "menus": [], "main": {}}
        r["closed"].extend({"when": "GetAllMessage() 之后（新加的那一步）", **it} for it in got["menus"])
        main = got.get("main") or {}
        log("菜单这一步：GetAllMessage() 之后对菜单发 Esc（新加的那一步）："
            + ("这次没做：" + got["skipped"] if got["skipped"] else
               ("没有要关的菜单" if not got["menus"] else
                "；".join(f"句柄 {i['hwnd']} {i['class']} {'关掉了' if i['closed'] else '没关掉'}" for i in got["menus"])))
            + (f"；发完之后主窗口{'看得见' if main.get('visible') else '看不见'}"
               + (f"（句柄 {main.get('hwnd')}，{'已经重新显示' if main.get('unhidden') else '没重新显示'}）" if main else "") if main else ""))
        return got

    def step(n: int, what: str, **kw):
        item = {"n": n, "what": what, "windows": _win_rows(api, r["errors"]), **kw}
        r["steps"].append(item)
        try:
            wins = [w for w in item["windows"] if w.get("visible")]
            log(f"菜单第 {n} 步（{what}）：微信进程看得见的顶层窗口 {len(wins)} 个"
                + ("" if not wins else "：" + "；".join(_win_brief(w) for w in wins)))
        except Exception:
            pass
        return item

    def shut(area: tuple, why: str):
        """用第 2 条的办法关掉盖在 area 上的小窗，记下用了哪种、关没关掉。"""
        try:
            got = shot_mod.close_menus(area, api, owner(), sleep)
        except Exception as ex:
            oops(f"{why}时关小窗", ex)
            return
        for it in got["tried"]:
            r["closed"].append({"when": why, **it})
        for it in got["stuck"]:
            if it.get("why"):
                r["closed"].append({"when": why, **it})
        log(f"菜单这一步：{why}时" + ("没有要关的小窗" if not got["tried"] else
                                   "关了 " + "；".join(f"句柄 {i['hwnd']} {i['class']} 用 {i['how']} "
                                                      f"{'关掉了' if i['closed'] else '没关掉'}" for i in got["tried"])))

    try:
        if wx is None:
            r["skip"] = "没连上微信，跳过"
            log(f"菜单这一步：{r['skip']}")
            r["steps"].append({"n": 0, "what": r["skip"], "windows": []})
            return r
        try:
            info = wx.ChatInfo() or {}
        except Exception as ex:
            info = {}
            r["chat_info_error"] = f"{type(ex).__name__}: {ex}"
        typ = str(info.get("chat_type") or "")
        if not orig:
            r["skip"] = "没记下命令开始时开着的会话，跳过"
        elif not restored:
            r["skip"] = f"没能切回原来的会话「{orig}」，跳过（只按截不到处理，不在这里动别的会话）"
        elif orig == CHAT_TARGET:
            r["skip"] = "原来的会话是「文件传输助手」，跳过"
        elif typ and typ != "group":
            r["skip"] = f"原来的会话是私聊（chat_type={typ}），跳过"
        if r["skip"]:
            log(f"菜单这一步：{r['skip']}")
            r["steps"].append({"n": 0, "what": r["skip"], "windows": []})
            snap("menu-before.png")  # 跳过也留两张图：看得出当时微信是原样
            snap("menu-after.png")
            return r

        r["owner"] = owner()
        snap("menu-before.png")
        step(1, "一开始（还没读任何消息）")

        # 2. 一开始就有菜单盖在聊天区上：用第 2 条的办法关
        area, how = _menu_area(api, hwnd_finder)
        r["area"] = f"{how} {list(area)}"
        try:
            found = shot_mod.menus(area, api, r["owner"])
        except Exception as ex:
            found = []
            log(f"菜单这一步：查一开始有没有菜单出错（{type(ex).__name__}: {ex}）")
        if found:
            shut(area, "一开始盖在聊天区上的菜单")
            step(2, "关掉一开始就盖在聊天区上的菜单", closed=[i for i in r["closed"]])
        else:
            step(2, "一开始没有菜单盖在聊天区上")
        # 3. GetAllMessage() 之后：和真跑时一样，先列一次窗口，再对新加的那个函数关菜单，然后再列一次窗口
        msgs = []
        try:
            try:
                msgs = list(wx.GetAllMessage() or [])
            finally:
                try:
                    r["owner"] = r["owner"] or owner()
                    area, how = _menu_area(api, hwnd_finder)
                    r["area"] = f"{how} {list(area)}"
                    r["menu_after_read"] = esc_after(area)
                except Exception as ex:  # 关菜单这一步出错只记下来，不算整个命令失败
                    r["menu_after_read_error"] = f"{type(ex).__name__}: {ex}"
                    log(f"菜单这一步：GetAllMessage() 之后关菜单出错（{type(ex).__name__}: {ex}），不算命令失败")
                step(3, "GetAllMessage() 之后（走新加的关菜单那一步）", messages=len(msgs))
        except Exception as ex:
            r["get_all_error"] = f"{type(ex).__name__}: {ex}"
            log(f"菜单这一步：读消息没成（{type(ex).__name__}: {ex}）")
            step(3, "GetAllMessage() 之后（没读成，没走关菜单那一步）", messages=0)

        others = [m for m in msgs[-TAIL:] if str(getattr(m, "attr", "") or "") in ("friend", "other")]
        if not others:
            r["skip"] = r["skip"] or "最后几条里没有别人发的消息，跳过（只读了消息，没取发送人）"
            log(f"菜单这一步：{r['skip']}")
            step(4, "最后几条里没有别人发的消息，跳过")
        else:
            last = others[-1]
            # 4. 读 sender 之后
            try:
                who = str(getattr(last, "sender", "") or "")
                r["sender"] = who
            except Exception as ex:
                r["sender_error"] = f"{type(ex).__name__}: {ex}"
                log(f"菜单这一步：读发送人出错（{type(ex).__name__}: {ex}）")
            step(4, "读最新一条别人发的消息的 sender 之后")
            # 5. 再读 sender_remark 之后
            try:
                r["sender_remark"] = str(getattr(last, "sender_remark", "") or "")
            except Exception as ex:
                r["sender_remark_error"] = f"{type(ex).__name__}: {ex}"
                log(f"菜单这一步：读备注名出错（{type(ex).__name__}: {ex}）")
            step(5, "再读 sender_remark 之后")
            # 6. 第 3～5 步里出现了新的菜单：关
            area, how = _menu_area(api, hwnd_finder, last)
            r["area"] = f"{how} {list(area)}"
            try:
                found = shot_mod.menus(area, api, r["owner"])
            except Exception as ex:
                found = []
                log(f"菜单这一步：查读消息之后有没有菜单出错（{type(ex).__name__}: {ex}）")
            if found:
                shut(area, "读消息之后出现的菜单")
                step(6, "关掉读消息之后出现的菜单", closed=[i for i in r["closed"]])
            else:
                step(6, "读消息之后没有出现菜单")
        snap("menu-after.png")
    except Exception as ex:  # 这一步出任何错都只记下来，不算整个命令失败
        r["error"] = f"{type(ex).__name__}: {ex}"
        log(f"菜单这一步出错（{type(ex).__name__}: {ex}），不算命令失败")
    return r


def run_test(out_dir, lib=None, timeout=None, sleep=time.sleep, clip=None, ui=None, hwnd_finder=None, grab=None, api=None) -> int:
    """服务器上的验证：连微信 → 切到「文件传输助手」→ 发测试图 → 读最后几条 → 找出最新的图片 → A、B 各试一遍
    → 往文件传输助手发一张自定义表情、找出最新的表情消息、按 wxshot 的办法截图 → 切回原来的会话
    → 在原来那个会话里只读地做 menu 一步（查菜单是谁打开的、哪种办法关得掉；出错不算命令失败）。
    返回 0（走完；A、B、表情截没截成看 result.json）或 1（停在某一步）。只在「文件传输助手」里操作。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    steps: list[str] = []
    result = {"ok": False, "stopped_at": "", "why": "", "out": str(out), "messages": [], "picked": None, "A": None, "B": None, "compare": {},
              "emotion": None, "sent_emotion": None, "menu": None, "exact_switch": None, "panel_windows": None}
    state = {"step": "开始", "orig": ""}

    def log(text: str):
        steps.append(text)
        with open(out / "probe.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
        print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)

    wx = None
    try:
        state["step"] = "1 连上微信"
        if lib is None:
            from .channels import wechat as wxmod
            lib = wxmod.load_lib()
        if timeout is None:
            from .channels import wechat as wxmod
            timeout = wxmod.PIC_TIMEOUT
        wx = WxForPicture(lib.WeChat(resize=False), [])
        try:
            state["orig"] = (wx.ChatInfo() or {}).get("chat_name") or ""
        except Exception:
            state["orig"] = ""
        log(f"已连上微信，现在开着的会话：「{state['orig']}」")

        state["step"] = f"2 切到「{CHAT_TARGET}」发测试图"
        wx.ChatWith(CHAT_TARGET)
        now = (wx.ChatInfo() or {}).get("chat_name") or ""
        if now != CHAT_TARGET:
            raise RuntimeError(f"没能切到「{CHAT_TARGET}」（现在打开的是「{now}」）")
        sent = out / "test-picture.png"
        test_image(sent)
        before = {str(getattr(m, "id", "") or "") for m in (wx.GetAllMessage() or [])[-TAIL:] if is_picture(m)}
        wx.SendFiles(str(sent), CHAT_TARGET)
        log(f"已往「{CHAT_TARGET}」发了测试图 {sent.name}")

        state["step"] = "3 读最后几条消息、找最新的图片"
        tail, pics = [], []
        for _ in range(10):
            sleep(1.0)
            tail = (wx.GetAllMessage() or [])[-TAIL:]
            pics = [m for m in tail if is_picture(m)]
            if any(str(getattr(m, "id", "") or "") not in before for m in pics):
                break
        result["messages"] = rows_of(tail)
        for r in result["messages"]:
            log(f"最后几条：type={r['type']} attr={r['attr']} content={r['content']}")
        if not pics:
            raise RuntimeError("最后几条里认不出图片（type 是 image，或 type 是 other 且内容是「图片」）")
        pick = pics[-1]
        result["picked"] = {"index": tail.index(pick), **rows_of([pick])[0], "is_new": str(getattr(pick, "id", "") or "") not in before}
        log(f"认出的图片是最后几条里的第 {result['picked']['index'] + 1} 条（type={result['picked']['type']}）")

        def attempt(way, fn, msg, prefix):
            ok, val, took = call_with_timeout(lambda: fn(msg), timeout)
            r = {"ok": ok, "seconds": took, "error": "" if ok else val, "file": "", "bytes": 0}
            if ok:
                f = out / f"{prefix}-{way}{ext_of(val)}"
                f.write_bytes(val)
                r.update(file=f.name, bytes=len(val))
            log(f"方式 {way}：" + (f"成功，{r['file']}（{r['bytes']} 字节）" if ok else f"没成：{r['error']}") + f"，用了 {took} 秒")
            return r, (val if ok else b"")

        ways = (("A", fetch_a), ("B", lambda m: fetch_b(m, clip, sleep)))
        for way, fn in ways:
            state["step"] = f"4 试方式 {way}"
            r, val = attempt(way, fn, pick, "picture")
            if val:
                try:
                    result["compare"][way] = compare(sent, val)
                except Exception as ex:
                    result["compare"][way] = {"error": f"{type(ex).__name__}: {ex}"}
            result[way] = r

        state["step"] = "5 往「文件传输助手」发一张自定义表情"
        if ui is None or hwnd_finder is None or api is None:
            from .channels import wechat as wxmod
            import importlib
            ui = ui or importlib.import_module(f"{wxmod.WX_LIB}.uia.uiautomation")
            from .winpos import WinApi
            win = WinApi()
            hwnd_finder = hwnd_finder or win.find_main
            api = api or win
        grab = grab or shot_mod.default_grab
        driver = WxStickers(ui, hwnd_finder, grab, sleep, log)  # 这一步在「文件传输助手」里，不会弹菜单；关菜单由第 7 步的 menu 一节做
        # 点开表情面板之前、面板关掉之后各记一次微信进程看得见的顶层窗口：下次能对照看出面板自己关上的时候旁边有没有菜单
        try:
            result["panel_windows"] = {"before": _win_rows(api), "after": []}
            log(f"点开表情面板之前：微信进程的顶层窗口 {len(result['panel_windows']['before'])} 个"
                + ("" if not result["panel_windows"]["before"] else "：" + "；".join(_win_brief(w) for w in result["panel_windows"]["before"])))
        except Exception as ex:
            result["panel_windows"] = {"before": [], "after": [], "error": f"{type(ex).__name__}: {ex}"}
            log(f"点开表情面板之前没列出窗口（{type(ex).__name__}: {ex}）")
        try:
            sent_emo = driver.send_first(wx, CHAT_TARGET, "")  # 只切到文件传输助手，点面板第一屏的第一格
            result["sent_emotion"] = {k: v for k, v in sent_emo.items() if k != "fps"}
            log("已往「文件传输助手」发了一张自定义表情" if sent_emo.get("found") else "表情面板里没有可发的表情，表情这一步跳过发送，只找已有的表情消息")
        except Exception as ex:  # 发不出去也要接着找已有的表情消息：这一步不算停住
            result["sent_emotion"] = {"found": False, "error": f"{type(ex).__name__}: {ex}"}
            log(f"往「文件传输助手」发表情没成（{type(ex).__name__}: {ex}），接着找已有的表情消息")
        try:
            rows = _win_rows(api)
            result.setdefault("panel_windows", {"before": [], "after": []})["after"] = rows
            log(f"表情面板关掉之后：微信进程的顶层窗口 {len(rows)} 个"
                + ("" if not rows else "：" + "；".join(_win_brief(w) for w in rows)))
        except Exception as ex:
            result.setdefault("panel_windows", {"before": [], "after": []}).setdefault("errors", []).append(
                {"where": "面板关掉之后列窗口", "error": f"{type(ex).__name__}: {ex}"})
            log(f"表情面板关掉之后没列出窗口（{type(ex).__name__}: {ex}）")

        state["step"] = "6 找最新的表情消息、按第 2、3 条截图"
        tail = (wx.GetAllMessage() or [])[-TAIL:]
        emos = [m for m in tail if is_emotion(m)]
        if not emos:
            result["emotion"] = {"found": False, "note": "没有表情消息"}
            log("最后几条里没有表情消息，表情这一步跳过")
        else:
            emo = emos[-1]
            result["emotion"] = {"found": True, "picked": {"index": tail.index(emo), **rows_of([emo])[0]}}
            log(f"认出的表情是最后几条里的第 {result['emotion']['picked']['index'] + 1} 条（type={result['emotion']['picked']['type']}）")
            result["emotion"].update(emotion_step(emo, out, api, grab, log, sleep=sleep))
        result["ok"] = True
    except Exception as ex:
        result.update(stopped_at=state["step"], why=f"{type(ex).__name__}: {ex}")
        log(f"停在第 {state['step']}：{type(ex).__name__}: {ex}")
    finally:
        restored = True
        if wx is not None and state["orig"] and state["orig"] != CHAT_TARGET:
            try:
                wx.ChatWith(state["orig"])
                log(f"已切回原来的会话「{state['orig']}」")
            except Exception as ex:
                restored = False
                log(f"切回原来的会话没成功：{type(ex).__name__}: {ex}")
        # 7. 查菜单是谁打开的、哪种办法关得掉：切回原来的会话以后、在原来那个会话里只读地做
        state["step"] = "7 查菜单是谁打开的"
        try:
            if api is None:
                from .winpos import WinApi
                api = WinApi()
            hwnd_finder = hwnd_finder or api.find_main
            result["menu"] = menu_step(wx, state["orig"], api, out, grab or shot_mod.default_grab, log, sleep, hwnd_finder,
                                       restored)
        except Exception as ex:  # 这一步出任何错都只记下来，不算整个命令失败
            result["menu"] = {"error": f"{type(ex).__name__}: {ex}"}
            log(f"菜单这一步出错（{type(ex).__name__}: {ex}），不算命令失败")
        # 8. 只切换、不发送：查清精确切换在真机上的表现（每个备注试一次，再试一个不存在的名字），最后切回原来的会话
        state["step"] = "8 精确切换（只切换、不发送）"
        try:
            result["exact_switch"] = exact_switch(wx, user_wechat_remarks(), log, sleep)
        except Exception as ex:  # 这一步出任何错都只记下来，不算整个命令失败
            result["exact_switch"] = {"error": f"{type(ex).__name__}: {ex}"}
            log(f"精确切换这一步出错（{type(ex).__name__}: {ex}），不算命令失败")
        if wx is not None and state["orig"]:
            try:
                now = (wx.ChatInfo() or {}).get("chat_name") or ""
            except Exception:
                now = ""
            if now != state["orig"]:  # 已经开着原来的会话就不用再切
                try:
                    wx.ChatWith(state["orig"])
                    log(f"精确切换之后又切回原来的会话「{state['orig']}」")
                except Exception as ex:
                    log(f"精确切换之后切回原来的会话没成功：{type(ex).__name__}: {ex}")
        result["steps"] = steps
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(out_dir: str) -> int:
    code = run_test(out_dir)
    print("验证完成" if code == 0 else "验证没走完，看 probe.log / result.json", flush=True)
    return code
