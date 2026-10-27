"""面板窗口进程：用系统自带的 WebView2 打开面板（不打包 Chromium）。没有 WebView2 时退回默认浏览器。"""
import webbrowser

from . import WINDOW_TITLE
from .paths import data_dir


def run(url: str):
    try:
        import webview
        webview.create_window(WINDOW_TITLE, url, width=1240, height=820, min_size=(380, 560),
                              background_color="#F4F7FB", text_select=True)
        # 浏览器资料放在本程序的数据目录里（卸载时选删除数据会一起删掉）
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(data_dir() / "webview"))
    except Exception:
        webbrowser.open(url)
