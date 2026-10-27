# Building from Source and Development

Requirements: Windows 10/11, **Python 3.13** (don't use a newer version: wxauto4 only publishes cp313 wheels), [Inno Setup 6](https://jrsoftware.org/isinfo.php) (only needed to build the installer).

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt

# 1. Download the embedding model that ships with the installer (not committed; verified against MANIFEST.json)
.venv\Scripts\python tools\fetch_model.py

# 2. Run the tests (all offline: no real platform and no real model is called)
.venv\Scripts\python -m pytest tests -q

# 3. Build the program
.venv\Scripts\pyinstaller installer\PodMates.spec --noconfirm --distpath build\dist --workpath build\work

# 4. Build the installer
ISCC.exe installer\PodMates.iss      # output goes to dist\
```

During development, run only the backend and the panel:

```bash
set PODMATES_DATA=<an empty folder>
.venv\Scripts\python tools\dev_server.py 18799
# open http://127.0.0.1:18799/?t=dev in a browser
```

Simulated environment (3 simulated QQ accounts + a simulated model + continuous group messages, used for taking screenshots and measuring performance):

```bash
.venv\Scripts\python tools\simulate.py setup <data folder>
.venv\Scripts\python tools\simulate.py run 1 600
```

Other tools:

- `tools\shots.py`: panel screenshots (headless Edge + DevTools device emulation; desktop and 375px phone, light and dark themes);
- `tools\build_catalog.py <models.dev api.json>`: regenerates the provider catalog snapshot;
- `tools\i18n_extract.py`: extracts UI text and checks coverage of the English dictionary (`web\i18n\en.json`);
- `tools\dump_templates.py`: collects role template text into the template text file (`docs\模板文本.md`);
- `tools\perf.py`, `bench_*.py`, `eval_embed.py`: performance and embedding model evaluations.

Structure: `mb\` backend (`engine` message engine, `channels\` platforms, `llm` three protocols, `memory`, `embed`, `rules` duty scheduling, `server`/`api2` APIs), `web\` panel (plain HTML/JS, no build step), `assets\` templates and bundled data, `tests\` tests, `installer\` packaging config.
