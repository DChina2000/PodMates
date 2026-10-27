"""模拟「PodMates」的进程（只给测试用）：像 PodMates 一样用 NapCatProcess 拉起桩 NapCat，然后一直挂着等被测试强行结束。

用法：python napcat_holder.py <NapCat目录> <号> <ws端口> <令牌> <控制目录> <就绪文件>
数据目录用环境变量 PODMATES_DATA。拉起之后把桩的启动器 pid 写进就绪文件。
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import napcat_support as support  # noqa: E402
from mb.channels import napcat  # noqa: E402

root, uin, port, token, ctl, ready = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], Path(sys.argv[5]), Path(sys.argv[6])


async def main():
    support.install(napcat, ctl)
    ch = support.FakeChannel(port, uin)
    p = napcat.NapCatProcess(ch, Path(root), uin, port, token, port + 1, True, "")
    await p.start()
    ready.write_text(json.dumps({"boot": p.boot_pid}), encoding="utf-8")
    await asyncio.sleep(3600)


asyncio.run(main())
