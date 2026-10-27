import os

# numpy 自带的 OpenBLAS 默认按 CPU 线程数各预留一块内存（24 线程的机器上多占约 750 MB 提交内存）；
# 本程序不用矩阵多线程运算，限成 1 个线程。必须在任何地方导入 numpy 之前设好。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

VERSION = "1.4.3"
APP_NAME = "PodMates"
DISPLAY_NAME = "群聊机器人面板"
WINDOW_TITLE = f"{DISPLAY_NAME} · {APP_NAME}"
# 作者署名与主页（只放名字和公开主页，不放邮箱等联系方式）
AUTHOR = "本昵称不代表本人"
AUTHOR_HOME = "https://space.bilibili.com/10228715"
# 仓库名只在这里写一次；新版本提醒、README、「关于」页都从这里取
REPO_OWNER = "DChina2000"
REPO_NAME = "PodMates"
REPO = f"{REPO_OWNER}/{REPO_NAME}"
REPO_URL = f"https://github.com/{REPO}"
LICENSE_NAME = "MIT"
