"""统一的 HTTPS 出口：所有对外的 HTTPS 连接（模型接口、服务商目录、模型下载、版本检查、搜索、各聊天平台……）都从这里拿 SSL 上下文。

为什么要统一：Windows 的根证书是「用到才下载」的。浏览器、PowerShell 访问网站会触发下载，Python 的 ssl 不会，
所以刚装好的新机器上，程序连 api.z.ai 这类站点会报 unable to get local issuer certificate。

上下文 = 先加载 Windows 系统证书库（企业或代理自签的根证书照样信任）+ 再加上随程序打包的 certifi 根证书。
证书校验始终开着，没有「忽略证书错误」这类选项。

新写的任何 HTTPS 出口都要用这里的 session() / connector() / urlopen()，不要直接 aiohttp.ClientSession() 或 urllib.request.urlopen()
（tests/test_round21.py 会扫源码，直接用会失败）。
"""
import ssl
import urllib.request

import aiohttp

_ctx: ssl.SSLContext | None = None
bundled = False  # 随程序打包的 certifi 根证书有没有加载成功（给测试和诊断看）


def build() -> ssl.SSLContext:
    """新建一个上下文：系统证书库 + certifi。certifi 的文件找不到时退回只用系统证书库（不关校验）。"""
    global bundled
    ctx = ssl.create_default_context()  # Windows 上会读系统的 ROOT 和 CA 证书库
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        bundled = True
    except (ImportError, OSError, ssl.SSLError):
        bundled = False
    return ctx


def context() -> ssl.SSLContext:
    global _ctx
    if _ctx is None:
        _ctx = build()
    return _ctx


def connector(**kw) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=context(), **kw)


def session(**kw) -> aiohttp.ClientSession:
    """aiohttp 的会话，HTTPS 校验用统一的上下文。其余参数（timeout 等）照 aiohttp.ClientSession 的写法。"""
    if "connector" not in kw:
        kw["connector"] = connector()
    return aiohttp.ClientSession(**kw)


def urlopen(url, timeout: float | None = None):
    """urllib 的 urlopen，HTTPS 校验用统一的上下文（http:// 的地址不受影响）。"""
    return urllib.request.urlopen(url, timeout=timeout, context=context())
