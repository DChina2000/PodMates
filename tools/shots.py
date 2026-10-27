"""面板截图：系统自带的 Edge 无头模式 + DevTools 设备模拟（真 375px 手机宽度），浅色与深色。
用法：python tools/shots.py <面板地址> <输出目录> [页面…]
需要后端用 launch_token=dev 启动（tools/dev_server.py）。"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

EDGE = os.environ.get("MB_BROWSER") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"  # 也可以用 Chrome：MB_BROWSER 指向 chrome.exe
PAGES = ["home", "chat", "accounts", "roles", "personas", "people", "chats", "memory", "logs", "usage", "settings"]
SIZES = {"desktop": (1366, 900, False), "mobile": (375, 812, True)}


def scrub_overlay(png: bytes) -> bytes:
    """本机装了浏览器翻译类扩展时，它会在页面右边缘画一个红色圆形悬浮按钮，不属于面板。截图里发现了就把那一块用正上方的像素盖掉。"""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    px = im.load()
    pts = [(x, y) for x in range(w - 50, w) for y in range(min(h, 320), min(h, 395)) if px[x, y][0] > 170 and px[x, y][1] < 135 and px[x, y][0] - px[x, y][1] > 70 and px[x, y][2] > 60]
    if len(pts) < 30:
        return png
    x0, x1 = min(p[0] for p in pts) - 8, w
    y0, y1 = min(p[1] for p in pts) - 8, max(p[1] for p in pts) + 8
    for y in range(max(1, y0), min(h, y1 + 1)):
        for x in range(max(0, x0), x1):
            px[x, y] = px[x, y0 - 1]
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


class CDP:
    def __init__(self, ws):
        self.ws, self.n = ws, 0
        self.errors = []

    async def __call__(self, method, **params):
        self.n += 1
        await self.ws.send_str(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads((await self.ws.receive()).data)
            if m.get("method") == "Runtime.exceptionThrown":
                self.errors.append(m["params"]["exceptionDetails"].get("exception", {}).get("description", "")[:300])
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})


async def capture(base: str, out: Path, pages: list[str], themes=("light", "dark"), sizes=("desktop", "mobile"), full=False):
    out.mkdir(parents=True, exist_ok=True)
    port = 19222
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
        proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--lang=zh-CN", "--disable-extensions",
                                 "--disable-features=Translate,TranslateUI,msEdgeTranslate",
                                 f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "about:blank"])
        try:
            async with aiohttp.ClientSession() as s:
                for _ in range(50):
                    try:
                        async with s.get(f"http://127.0.0.1:{port}/json") as r:
                            targets = await r.json()
                            break
                    except aiohttp.ClientError:
                        await asyncio.sleep(0.2)
                page = next(t for t in targets if t["type"] == "page")
                async with s.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                    cdp = CDP(ws)
                    await cdp("Page.enable")
                    await cdp("Runtime.enable")
                    await cdp("Page.navigate", url=base + "/?t=dev")  # 拿到登录 cookie
                    await asyncio.sleep(1.5)
                    for theme in themes:
                        await cdp("Runtime.evaluate", awaitPromise=True, expression=(
                            "fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},"
                            f"body:JSON.stringify({{scope:'global',key:'theme',value:'{theme}'}})}}).then(r=>r.status)"))
                        for size in sizes:
                            w, h, mobile = SIZES[size]
                            await cdp("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=mobile)
                            for p in pages:
                                # 页面参数可以写成 路由::文件名标签::要在页面里执行的 JS（用来截弹窗，比如 roles::templates::templatePicker()）
                                route, label, script = (p.split("::", 2) + ["", ""])[:3] if "::" in p else (p, "", "")
                                p = f"{route}-{label}" if label else route
                                await cdp("Page.navigate", url="about:blank")
                                await cdp("Page.navigate", url=f"{base}/#/{route}")
                                for _ in range(60):  # 等页面真正渲染完（有内容、不再是「加载中」）
                                    await asyncio.sleep(0.25)
                                    r = await cdp("Runtime.evaluate", returnByValue=True, expression=(
                                        "!!document.querySelector('#root > *') && !document.querySelector('.view > .empty.faint')"))
                                    if r.get("result", {}).get("value"):
                                        break
                                await asyncio.sleep(1.0)
                                if script:
                                    await cdp("Runtime.evaluate", awaitPromise=True, expression=script)
                                    await asyncio.sleep(1.2)
                                # 偶尔页面拿到的是切换前的主题，截图前按本轮主题再套一次
                                await cdp("Runtime.evaluate", expression=f"applyTheme('{theme}')")
                                await asyncio.sleep(0.3)
                                shot = await cdp("Page.captureScreenshot", format="png", captureBeyondViewport=full)
                                f = out / f"{p.replace('/', '_').replace(':', '_')}-{size}-{theme}.png"
                                f.write_bytes(scrub_overlay(base64.b64decode(shot["data"])))
                                print(f.name, flush=True)
                                if cdp.errors:
                                    print("  页面报错：", cdp.errors, flush=True)
                                    cdp.errors.clear()
                    await cdp("Runtime.evaluate", awaitPromise=True, expression=(
                        "fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},"
                        "body:JSON.stringify({scope:'global',key:'theme',value:'system'})}).then(r=>r.status)"))
        finally:
            proc.terminate()
            proc.wait(15)


if __name__ == "__main__":
    args = sys.argv[1:]
    themes = ("light", "dark")
    sizes = ("desktop", "mobile")
    full = False
    for a in list(args):
        if a.startswith("--theme="):
            themes = tuple(a[8:].split(","))
            args.remove(a)
        elif a == "--full":
            full = True
            args.remove(a)
        elif a.startswith("--size="):
            sizes = tuple(a[7:].split(","))
            args.remove(a)
    asyncio.run(capture(args[0].rstrip("/"), Path(args[1]), args[2:] or PAGES, themes, sizes, full))
