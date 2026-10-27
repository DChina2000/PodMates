"""开发用：只跑后端和面板（不开托盘、不开窗口）。用 PODMATES_DATA 指定数据目录。
用法：python tools/dev_server.py [端口] [--start]"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mb.core import Core  # noqa: E402
from mb.paths import data_dir  # noqa: E402
from mb.server import Server  # noqa: E402


async def main():
    port = int(next((a for a in sys.argv[1:] if a.isdigit()), 18799))
    core = Core(data_dir())
    srv = Server(core, launch_token="dev")
    await srv.start("127.0.0.1", port)
    print(f"http://127.0.0.1:{port}/?t=dev  数据目录 {data_dir()}", flush=True)
    asyncio.create_task(core.periodic())
    if "--start" in sys.argv:
        await core.start()
    await asyncio.Event().wait()


asyncio.run(main())
