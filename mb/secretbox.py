"""密钥加密：Windows 的 DPAPI，按当前 Windows 用户加密（CryptProtectData）。

数据库里存的是 "dpapi:<base64>"，换一台电脑、换一个 Windows 用户就解不开，需要重新填。
不是 Windows（只有开发测试才会遇到）时存 "plain:<原文>"，这是明说的降级，不假装加密。
"""
import base64
import ctypes
import sys
from ctypes import wintypes

# 固定值：程序改名也不能变，否则已加密的密钥读不出来
ENTROPY = b"MultiBot.secrets.v1"
PREFIX_DPAPI, PREFIX_PLAIN = "dpapi:", "plain:"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(fn_name: str, data: bytes) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, keep1 = _blob(data)
    ent, keep2 = _blob(ENTROPY)
    out = _Blob()
    fn = getattr(crypt32, fn_name)
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.GetLastError() or 1, "DPAPI 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def available() -> bool:
    return sys.platform == "win32"


def encrypt(text: str) -> str:
    if not text:
        return ""
    if available():
        return PREFIX_DPAPI + base64.b64encode(_call("CryptProtectData", text.encode("utf-8"))).decode("ascii")
    return PREFIX_PLAIN + text


def decrypt(stored: str) -> str | None:
    """返回明文；解不开（换了电脑/用户，或数据损坏）返回 None。"""
    if not stored:
        return ""
    try:
        if stored.startswith(PREFIX_DPAPI):
            return _call("CryptUnprotectData", base64.b64decode(stored[len(PREFIX_DPAPI):])).decode("utf-8")
        if stored.startswith(PREFIX_PLAIN):
            return stored[len(PREFIX_PLAIN):]
    except Exception:
        return None
    return None


def is_encrypted(stored: str) -> bool:
    return stored.startswith((PREFIX_DPAPI, PREFIX_PLAIN))
