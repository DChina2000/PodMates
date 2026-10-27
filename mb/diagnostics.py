"""一键打包诊断信息：日志、版本、去掉密钥的设置，打成一个 zip，用户报问题时附上。

不含：任何密钥、令牌、密码；聊天记录和记忆内容；人物表。日志里长数字（账号、群号）只留末 4 位，令牌样子的串整个抹掉。
"""
import io
import json
import platform
import re
import sys
import time
import zipfile

from . import APP_NAME, VERSION
from .channels.napcat import redact

SECRET_PAT = re.compile(r"(sk-[\w-]{8,}|Bearer\s+[\w.~+/=-]{8,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}|\d{6,}:[\w-]{30,}|"
                        r"(?i:(?:token|secret|password|passwd|api[_-]?key|sessdata|bduss|bili_jct|cookie|authorization)\s*[=:：]\s*)[^\s,;'\"]+)")
DIGITS = re.compile(r"\d{7,}")
KEY_WORDS = ("key", "secret", "token", "password", "cookie", "sessdata", "bduss", "jct")


def scrub(text: str, known: list[str] | None = None) -> str:
    text = redact(text, known or [])
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer 【已抹掉】", text)
    text = SECRET_PAT.sub("【已抹掉】", text)
    return DIGITS.sub(lambda m: "…" + m.group(0)[-4:], text)


def strip_url_auth(u: str) -> str:
    return re.sub(r"//[^/@]*@", "//", u or "")


def build(core, extra_known: list[str] | None = None) -> bytes:
    db = core.db
    known = [v for (_, v) in [(k, db.secret(k)) for k in [r[0] for r in db.x("SELECT k FROM secrets")]] if v] + (extra_known or [])
    logs = db.rows("SELECT ts, level, cat, text FROM logs ORDER BY id DESC LIMIT 1500")[::-1]
    lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))} [{r['level']}] {r['cat']}: {scrub(r['text'], known)}" for r in logs]
    settings = {}
    for r in db.rows("SELECT scope, key, value FROM settings"):
        if any(w in r["key"].lower() for w in KEY_WORDS):
            continue
        v = json.loads(r["value"])
        settings.setdefault(r["scope"], {})[r["key"]] = strip_url_auth(v) if isinstance(v, str) else v
    accounts = [{k: a.get(k) for k in ("id", "platform", "role_id", "enabled", "mode")} | {"status": (core.engine.bots.get(a["id"]).channel.status
                if core.engine.bots.get(a["id"]) and core.engine.bots[a["id"]].channel else "stopped")} for a in db.docs("account")]
    endpoints = [{"id": e["id"], "provider": e.get("provider", ""), "protocol": e.get("protocol", ""), "base_url": strip_url_auth(e.get("base_url", "")),
                  "has_key": core.llm.key_state(e)["has_key"]} for e in db.docs("endpoint")]
    info = {"app": APP_NAME, "version": VERSION, "python": sys.version.split()[0], "os": platform.platform(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "roles": len(db.docs("role")), "accounts": len(accounts),
            "unreadable_secrets": len(db.unreadable_secrets())}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version.json", json.dumps(info, ensure_ascii=False, indent=1))
        z.writestr("settings.json", json.dumps(settings, ensure_ascii=False, indent=1))
        z.writestr("accounts.json", json.dumps(accounts, ensure_ascii=False, indent=1))
        z.writestr("endpoints.json", json.dumps(endpoints, ensure_ascii=False, indent=1))
        z.writestr("logs.txt", "\n".join(lines))
        z.writestr("说明.txt", "这是群聊机器人面板生成的诊断包：版本、去掉密钥的设置、近期日志。\n"
                              "里面没有任何密钥、令牌和密码，也没有聊天记录和记忆内容；日志里的长数字只留了末 4 位。\n"
                              "发给别人之前可以自己解压看一遍。\n")
    return buf.getvalue()
