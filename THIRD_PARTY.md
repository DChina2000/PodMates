# 第三方组件与许可证 / Third-party notices

本项目以 MIT 许可证发布（见 `LICENSE`）。下面是它用到、下载或提到的第三方组件。
This project is MIT-licensed (see `LICENSE`). Below are the third-party components it uses, downloads or mentions.

## 不随安装包分发、运行时才下载的（Not bundled; fetched at runtime）

| 组件 | 用途 | 许可证 | 怎么处理 |
|---|---|---|---|
| **NapCat**（NapNeko/NapCatQQ） | QQ 登录与消息收发（OneBot v11） | 自定义的「有限再分发许可证」（Limited Redistribution License）：**不明确允许打进第三方安装包，且禁止商用** | **不随包分发。** 首次使用时由用户在面板里点「准备 NapCat」，从官方 GitHub Releases 下载。请你自己阅读并遵守它的许可证。 |
| **wxauto4**（cluic/wxauto4，PyPI） | 微信 4.x 界面自动化 | **上游（GitHub 与 PyPI）都没有声明许可证** | **不随包分发。** 只在用户点「下载微信组件」时从 PyPI 下载到本机数据目录，作为用户自己安装的依赖；本项目不复制它的代码。支持的微信版本以它的说明为准（写本文时到 4.1.8）。 |
| 向量模型 GTE multilingual base | 可选的语义回忆模型（效果更好、内存更大） | Apache-2.0 | 用户在面板里点下载（ModelScope / Hugging Face / hf-mirror）。 |
| 其他可选向量模型（Qwen3-Embedding 0.6B 等） | 同上 | Apache-2.0 | 同上，按预设里写的来源下载。 |

## 随安装包带的数据（Bundled data）

| 组件 | 许可证 | 说明 |
|---|---|---|
| **BAAI/bge-small-zh-v1.5**（Xenova 的 ONNX int8 转换版） | MIT（Copyright (c) BAAI） | 默认的本机向量模型。**文件不放在源码仓库里**，构建安装包前用 `python tools/fetch_model.py` 下载并校验（见 `assets/models/bge-small-zh-v1.5/MANIFEST.json`）。 |
| **models.dev** 的服务商/模型目录快照（`assets/providers/catalog.json`，由 `tools/build_catalog.py` 精简生成） | MIT（Copyright (c) 2025 models.dev，<https://github.com/anomalyco/models.dev>） | 面板里「第三方服务商」的下拉清单；支持联网更新。服务商的商标/Logo 不在 MIT 范围内，本项目没有使用任何 Logo。 |

> models.dev 的 MIT 许可全文（<https://github.com/anomalyco/models.dev>，`LICENSE`）：
>
> MIT License
>
> Copyright (c) 2025 models.dev
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Python 依赖（Python dependencies，随安装包以二进制形式带上）

| 包 | 许可证 |
|---|---|
| aiohttp, multidict, yarl, frozenlist, aiosignal, propcache | Apache-2.0 |
| aiohappyeyeballs, audioop-lts, typing-extensions | PSF-2.0 |
| discord.py | MIT |
| pywebview, proxy_tools, bottle, pythonnet, clr_loader | BSD-3-Clause / MIT |
| **pystray** | **LGPL-3.0** —— 以独立的 Python 包形式随包带在 `_internal` 目录里，用户可以自行替换为其他版本；本项目没有修改它。 |
| Pillow | MIT-CMU（HPND 类） |
| numpy | BSD-3-Clause 等 |
| onnxruntime | MIT |
| tokenizers | Apache-2.0 |
| sqlite-vec | MIT / Apache-2.0 双许可 |
| segno | BSD-3-Clause |
| certifi（Mozilla 根证书合集 cacert.pem） | MPL-2.0 —— 以原样的独立文件随包带在 `_internal/certifi` 里，没有修改 |
| pypdf | BSD-3-Clause |
| pywin32 | PSF |
| psutil | BSD-3-Clause |
| protobuf, flatbuffers | BSD-3-Clause / Apache-2.0 |
| comtypes, attrs, six, filelock, pyyaml | MIT |

构建工具（不进安装包）：PyInstaller（GPL-2.0-or-later，带允许打包任意程序的特殊例外）、Inno Setup（Inno Setup 许可证，允许免费用于打包分发）、pytest（MIT）。

安装界面的简体中文用的是 Inno Setup 社区维护的非官方简体中文翻译文件（`installer/ChineseSimplified.isl`），文件头保留译者署名，来源见文件头。
