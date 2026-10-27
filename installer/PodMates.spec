# -*- mode: python ; coding: utf-8 -*-
# 构建：.venv\Scripts\pyinstaller installer\PodMates.spec --noconfirm --distpath build\dist --workpath build\work
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
# wxauto4 本身不打包（没有许可证，运行时从 PyPI 下载），但它要用的库要带上
wx_deps = [m for m in (ROOT / "tools" / "wx_deps.txt").read_text().split() if m != "__mp_main__"]
# mb 下所有模块都明确列出（各平台渠道是按名字动态导入的，PyInstaller 静态分析看不到；collect_submodules 在这里也没收全）
own = [f"mb.{p.stem}" for p in (ROOT / "mb").glob("*.py") if p.stem != "__init__"] +       [f"mb.channels.{p.stem}" for p in (ROOT / "mb" / "channels").glob("*.py") if p.stem != "__init__"] + ["mb", "mb.channels"]
hidden = wx_deps + collect_submodules("comtypes") + own + [
    "pypdf",
    "segno", "webview", "webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "pystray._win32",
    "mb.embed", "mb.rules", "mb.tls", "certifi", "sqlite_vec", "onnxruntime", "tokenizers", "numpy",
]

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "web"), "web"), (str(ROOT / "assets"), "assets"), (str(ROOT / "LICENSE"), "."), (str(ROOT / "THIRD_PARTY.md"), ".")] + collect_data_files("sqlite_vec") + collect_data_files("certifi"),  # certifi 的 cacert.pem：mb/tls.py 靠它在没有系统根证书时也能校验
    binaries=collect_dynamic_libs("sqlite_vec"),
    hiddenimports=hidden,
    excludes=["pytest", "_pytest", "wxauto4", "wxautox", "huggingface_hub", "torch", "transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PodMates", icon=str(ROOT / "assets" / "icon.ico"),
          console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="PodMates", upx=False)
