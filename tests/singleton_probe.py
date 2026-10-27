"""模拟 PodMates 主程序的进程（只给测试用）：走真的 mb.app.main（单实例检查原样），只是把 Desktop 换成不起引擎、不起托盘的桩。

用法：python singleton_probe.py <标记目录> <到点才开始(时间戳)> <取到后挂几秒> [main 的参数，如 --background]    数据目录用环境变量 PODMATES_DATA
标记目录里：ran-<pid>（这个进程成了唯一的主程序）、exit-<pid>（main 返回了）、shown-<pid>（它收到了「把面板调到前面」的请求，pid 是被调的那个）。
桩会在本机起一个小 HTTP 服务当「面板」，把端口和令牌写进 runtime.json，像真的一样。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import mb.app as app  # noqa: E402
from mb.paths import data_dir  # noqa: E402

mark, start_at, hold = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
TOKEN = "tok-" + str(os.getpid())


class Panel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        if self.path == "/api/_show" and self.headers.get("X-Launch") == TOKEN:
            (mark / f"shown-{os.getpid()}").write_text("1")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


class StubDesktop:
    def __init__(self, background):
        self.background = background

    def run(self):
        srv = HTTPServer(("127.0.0.1", 0), Panel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        (data_dir() / "runtime.json").write_text(json.dumps({"pid": os.getpid(), "port": srv.server_address[1], "token": TOKEN}), encoding="utf-8")
        (mark / f"ran-{os.getpid()}").write_text(str(time.time()))
        time.sleep(hold)
        (data_dir() / "runtime.json").unlink(missing_ok=True)


app.Desktop = StubDesktop
while time.time() < start_at:  # 几个进程约好同一时刻开始，尽量同时进到 main
    pass
app.main(sys.argv[4:])
(mark / f"exit-{os.getpid()}").write_text(str(time.time()))
