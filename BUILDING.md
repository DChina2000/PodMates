# 从源码构建与开发

需要：Windows 10/11、**Python 3.13**（别用更新的：wxauto4 只发到 cp313 的轮子）、[Inno Setup 6](https://jrsoftware.org/isinfo.php)（只有打安装包才要）。

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt

# 1. 下载随安装包带的向量模型（不进仓库；按 MANIFEST.json 校验）
.venv\Scripts\python tools\fetch_model.py

# 2. 跑测试（全部离线，不连任何真实平台、不调真模型）
.venv\Scripts\python -m pytest tests -q

# 3. 打包程序
.venv\Scripts\pyinstaller installer\PodMates.spec --noconfirm --distpath build\dist --workpath build\work

# 4. 打安装包
ISCC.exe installer\PodMates.iss      # 输出在 dist\
```

开发时只跑后端和面板：

```bash
set PODMATES_DATA=某个空目录
.venv\Scripts\python tools\dev_server.py 18799
# 浏览器开 http://127.0.0.1:18799/?t=dev
```

模拟环境（3 个模拟 QQ 号 + 模拟模型 + 持续群消息，用来截图和测性能）：

```bash
.venv\Scripts\python tools\simulate.py setup <数据目录>
.venv\Scripts\python tools\simulate.py run 1 600
```

其他工具：

- `tools\shots.py` 面板截图（Edge 无头 + DevTools 设备模拟，桌面和 375px 手机、浅色深色）；
- `tools\build_catalog.py <models.dev 的 api.json>` 重新生成服务商目录快照；
- `tools\i18n_extract.py` 抽出界面文字并检查英文词典覆盖率（`web\i18n\en.json`）；
- `tools\dump_templates.py` 汇总角色模板文字到 `docs\模板文本.md`；
- `tools\perf.py`、`bench_*.py`、`eval_embed.py` 性能与向量模型评测。

结构：`mb\` 后端（`engine` 消息引擎、`channels\` 各平台、`llm` 三种协议、`memory`、`embed`、`rules` 排班、`server`/`api2` 接口），`web\` 面板（纯 HTML/JS，无构建步骤），`assets\` 模板和随包数据，`tests\` 测试，`installer\` 打包配置。
