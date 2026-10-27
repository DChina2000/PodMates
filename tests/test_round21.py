"""HTTPS 出口统一用 mb/tls.py 的 SSL 上下文（系统证书库 + 随程序打包的 certifi 根证书）：
新装的 Windows 还没下载根证书时也能连；系统证书库里的自签根证书照样信任；校验始终开着；以后新加的出口不会漏。"""
import asyncio
import re
import shutil
import ssl
import subprocess
from pathlib import Path

import aiohttp
import pytest

from mb import tls

ROOT = Path(__file__).resolve().parent.parent
SRC = sorted(p for p in (ROOT / "mb").rglob("*.py") if p.name != "tls.py")


# ---------- 静态检查：每个出口都走 tls，没有关校验的口子 ----------
def code(p: Path) -> str:
    """去掉注释和三引号文档字符串之外的内容不好做，这里只去掉整行注释；足够抓直接调用。"""
    return "\n".join(l for l in p.read_text(encoding="utf-8").split("\n") if not l.lstrip().startswith("#"))


def test_every_https_exit_goes_through_tls():
    bad = []
    for p in SRC:
        s = code(p)
        for pat in (r"aiohttp\.ClientSession\(", r"urllib\.request\.urlopen\(", r"urlretrieve\(", r"HTTPSConnection", r"\bhttpx\b", r"^import requests|^\s+import requests|requests\.(get|post)\("):
            if re.search(pat, s, re.M):
                bad.append((p.name, pat))
        for m in re.finditer(r"discord\.Client\(([^)]*)\)", s):
            if "connector=" not in m.group(1):
                bad.append((p.name, "discord.Client 没有传 connector"))
    assert not bad, f"这些地方没走 mb/tls.py（用 tls.session() / tls.urlopen() / tls.connector()）：{bad}"
    # 反过来：确实有人在用，不是空检查
    assert any("tls.session(" in code(p) for p in SRC) and any("tls.urlopen(" in code(p) for p in SRC)


def test_no_switch_to_skip_certificate_checks():
    bad = []
    for p in list(SRC) + [ROOT / "mb" / "tls.py"]:
        s = code(p)
        for pat in (r"ssl\s*=\s*False", r"verify_ssl", r"CERT_NONE", r"check_hostname\s*=\s*False", r"_create_unverified_context", r"verify\s*=\s*False"):
            if re.search(pat, s):
                bad.append((p.name, pat))
    assert not bad, bad
    for key in ("ignore_cert", "skip_ssl", "insecure", "verify_cert"):  # 设置项里也没有这类开关
        from mb.settings import BY_KEY
        assert key not in BY_KEY


def test_certifi_is_bundled_and_listed():
    spec = (ROOT / "installer" / "PodMates.spec").read_text(encoding="utf-8")
    assert 'collect_data_files("certifi")' in spec and '"certifi"' in spec and '"mb.tls"' in spec
    assert "certifi" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    import certifi
    pem = Path(certifi.where())
    assert pem.exists() and pem.read_text(encoding="utf-8").count("BEGIN CERTIFICATE") > 100
    assert "certifi" in (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")


# ---------- 系统证书库为空时，只靠自带证书 ----------
def test_context_has_the_bundled_roots_even_when_the_system_store_is_empty(monkeypatch):
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=ssl.Purpose.SERVER_AUTH: None)  # 模拟新装 Windows：系统里一个根证书都没有
    ctx = tls.build()
    assert tls.bundled is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True  # 校验是开着的
    import certifi
    n_pem = Path(certifi.where()).read_text(encoding="utf-8").count("BEGIN CERTIFICATE")
    assert ctx.cert_store_stats()["x509_ca"] >= n_pem * 0.95


def test_missing_bundle_falls_back_to_system_store_without_turning_verification_off(monkeypatch):
    import certifi
    monkeypatch.setattr(certifi, "where", lambda: str(ROOT / "nonexistent-cacert.pem"))
    ctx = tls.build()
    assert tls.bundled is False and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


# ---------- 真的握手一次：自己造一个根证书 + 服务器证书 ----------
def make_pki(d: Path):
    if not shutil.which("openssl"):
        pytest.skip("本机没有 openssl，跳过握手测试")

    def run(*a):
        subprocess.run(["openssl", *a], cwd=d, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=PodMates Test Root",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=localhost")
    (d / "ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\n", encoding="utf-8")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem", "-days", "2", "-extfile", "ext.cnf")
    return d / "ca.pem", d / "srv.pem", d / "srv.key"


async def serve(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return srv, srv.sockets[0].getsockname()[1]


async def get_via_tls(port):
    async with tls.session() as s:
        async with s.get(f"https://localhost:{port}/") as r:
            return r.status, await r.text()


def test_handshake_trusts_bundled_root_when_system_store_is_empty(monkeypatch, tmp_path):
    ca, cert, key = make_pki(tmp_path)

    async def body():
        srv, port = await serve(cert, key)
        try:
            # 对照：aiohttp 自己的默认上下文（没有这个根证书）连不上 —— 就是服务器上看到的 unable to get local issuer certificate 这一类错误
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                async with aiohttp.ClientSession() as s:
                    await s.get(f"https://localhost:{port}/")
            # 系统证书库是空的，随程序带的根证书里有它：通过
            import certifi
            monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=ssl.Purpose.SERVER_AUTH: None)
            monkeypatch.setattr(certifi, "where", lambda: str(ca))
            monkeypatch.setattr(tls, "_ctx", None)
            assert await get_via_tls(port) == (200, "ok")
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(body())
    monkeypatch.setattr(tls, "_ctx", None)


def test_handshake_still_trusts_a_self_signed_root_found_only_in_the_system_store(monkeypatch, tmp_path):
    ca, cert, key = make_pki(tmp_path)
    pem = ca.read_text(encoding="utf-8")

    async def body():
        srv, port = await serve(cert, key)
        try:
            real_load = ssl.SSLContext.load_default_certs

            def system_has_it(self, purpose=ssl.Purpose.SERVER_AUTH):  # 企业 / 代理的自签根证书只装在系统证书库里
                real_load(self, purpose)
                self.load_verify_locations(cadata=pem)
            monkeypatch.setattr(ssl.SSLContext, "load_default_certs", system_has_it)
            monkeypatch.setattr(tls, "_ctx", None)
            assert tls.build().cert_store_stats()["x509_ca"] > 100  # 自带的也在
            assert await get_via_tls(port) == (200, "ok")
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(body())
    monkeypatch.setattr(tls, "_ctx", None)


def test_handshake_refuses_an_unknown_root(monkeypatch, tmp_path):
    ca, cert, key = make_pki(tmp_path)

    async def body():
        srv, port = await serve(cert, key)
        try:
            monkeypatch.setattr(tls, "_ctx", None)
            with pytest.raises(aiohttp.ClientConnectorCertificateError):  # 两边都没有这个根证书：照样拒绝，校验没有被绕过
                await get_via_tls(port)
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(body())
    monkeypatch.setattr(tls, "_ctx", None)


def test_urlopen_uses_the_same_context(monkeypatch):
    seen = {}

    def fake(url, timeout=None, context=None):
        seen["ctx"] = context
        raise OSError("stop")
    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(OSError):
        tls.urlopen("https://example.com/", timeout=3)
    assert seen["ctx"] is tls.context() and tls.context() is tls.context()
