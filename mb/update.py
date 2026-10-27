"""新版本提醒：查 GitHub Releases 的最新版本，有新版就提示；不自动下载、不自动更新。

仓库地址取自 mb.REPO（设置里的 update_repo 可覆盖，格式 owner/repo）；还没发布过版本时查不到，静默跳过。
"""
import re
import time

import aiohttp

from . import APP_NAME, REPO, VERSION
from . import tls

API = "https://api.github.com/repos/{repo}/releases/latest"


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0])[:4]) or (0,)


def newer(latest: str, current: str = VERSION) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


async def check(repo: str, proxy: str | None = None, api: str = API) -> dict:
    """返回 {latest, url, notes, newer}。查不到抛异常。"""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise ValueError("还没填仓库地址（owner/repo）")
    async with tls.session(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(api.format(repo=repo), proxy=proxy or None,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}/{VERSION}"}) as r:
            if r.status == 404:
                raise ValueError("这个仓库还没有发布过版本")
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            j = await r.json(content_type=None)
    tag = j.get("tag_name", "")
    return {"latest": tag.lstrip("vV"), "url": j.get("html_url", ""), "notes": (j.get("body") or "")[:600],
            "newer": newer(tag), "checked": time.time(), "current": VERSION}
