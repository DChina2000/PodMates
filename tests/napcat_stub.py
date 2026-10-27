"""模拟 NapCat 的桩进程（只给测试用）：不碰任何真实账号、真实 QQ。

用法：python napcat_stub.py <号> <ws端口> <令牌> <控制目录>
  - 起一个正向 WebSocket（和 NapCat 一样要 Authorization: Bearer <令牌>），get_login_info 返回启动时 <控制目录>/login.txt 里写的号（没有这个文件就是 <号>）；
  - 控制目录里有 nows 文件：不起 WebSocket（模拟「进程在、但连不上」）；有 silent 文件：什么都不往标准输出写；
  - 往 <控制目录>/emit.txt 里写几行，桩就把这几行原样打到标准输出（模拟 NapCat 打出二维码、被踢下线这些日志），然后删掉这个文件；
  - 标准输出被 PodMates 重定向到日志文件，桩不管那头是什么。
"""
import asyncio
import json
import sys
from pathlib import Path

from aiohttp import web

sys.stdout.reconfigure(encoding="utf-8")  # NapCat（Node）写出去的永远是 UTF-8
uin, port, token, ctl = sys.argv[1], int(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
login = (ctl / "login.txt").read_text().strip() if (ctl / "login.txt").exists() else uin  # 启动时读一次


def say(text: str):
    if (ctl / "silent").exists():  # 模拟「不肯往标准输出写」的 NapCat
        return
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


async def ws_handler(req):
    if req.headers.get("Authorization") != f"Bearer {token}":
        return web.Response(status=401)
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    async for m in ws:
        a = json.loads(m.data)
        data = None
        if a["action"] == "get_login_info":
            data = {"user_id": int(login), "nickname": "桩"}
        elif a["action"] == "get_group_list":
            data = []
        await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": a.get("echo")}))
    return ws


async def emitter():
    f = ctl / "emit.txt"
    while True:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                say(line)
            f.unlink()
        await asyncio.sleep(0.2)


def probe_modules():
    """控制目录里有 probe_dll：试着按名字加载 pmfake.dll（它只在测试里「假装的程序目录」里有），再把自己加载过的模块全部写进 modules.txt。"""
    import ctypes
    import psutil
    try:
        ctypes.WinDLL("pmfake.dll")
        found = True
    except OSError:
        found = False
    mods = sorted({m.path for m in psutil.Process().memory_maps()})
    (ctl / "modules.txt").write_text(json.dumps({"found": found, "mods": mods}), encoding="utf-8")


async def main():
    if (ctl / "probe_dll").exists():
        probe_modules()
    say(f"[info] 桩 NapCat 启动 {uin}")
    say(f"正在快速登录 {uin}")
    if not (ctl / "nows").exists():
        app = web.Application()
        app.router.add_get("/", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    await emitter()


asyncio.run(main())
