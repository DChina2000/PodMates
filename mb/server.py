"""面板后端：aiohttp。REST 接口 + /api/events 事件推送（WebSocket，不轮询）。

访问控制：默认只监听 127.0.0.1；开了「局域网访问」才监听 0.0.0.0。面板有密码；
桌面窗口用启动时生成的一次性令牌免输密码。还没设密码时只允许本机访问（首次设置向导）。
开了局域网访问之后，每个请求都看来源地址：本机、局域网私有地址、Tailscale 这类 CGNAT 地址放行，别的（公网）一律 403，
除非用户另外打开「也允许公网访问面板」。
"""
import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import VERSION, commands, personas
from .channels import napcat as napcat_mod
from .channels.base import Muted
from .db import new_id, since_day
from .memory import KINDS, parse_chatlog
from .paths import RES, WEB, data_dir
from .people import PRESETS
from .settings import COMMANDS, SCHEMA, SCOPE_LABEL, scope_type
from . import system
from .api2 import Api2

PUBLIC = {"/api/state", "/api/login", "/api/setup/password", "/api/_show"}
DOC_KINDS = {"role", "account", "persona", "module", "person", "pgroup", "chat", "space", "endpoint", "rule", "cgroup", "archetype"}
SESSION_DAYS = 30
OLD_BOT_GUESS = [] if os.environ.get("PODMATES_NO_GUESS") else [RES.parent / "bot"]


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"pbkdf2${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, _h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)


PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10")]
CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale 等组网工具用的地址段


def addr_kind(remote: str | None) -> str:
    """请求来源地址分类：local（127.0.0.0/8、::1）/ private（局域网）/ cgnat（100.64.0.0/10，Tailscale）/ public（其余，认不出的也算）。"""
    try:
        ip = ipaddress.ip_address((remote or "").split("%")[0])
    except ValueError:
        return "public"
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "local"
    if any(ip.version == n.version and ip in n for n in PRIVATE_NETS):
        return "private"
    if ip.version == 4 and ip in CGNAT_NET:
        return "cgnat"
    return "public"


def is_local(req: web.Request) -> bool:
    return addr_kind(req.remote) == "local"


class Server:
    def __init__(self, core, launch_token: str = ""):
        self.core = core
        self.launch_token = launch_token or secrets.token_urlsafe(24)
        self.app = web.Application(middlewares=[self.auth], client_max_size=200 << 20)
        self.denied_at: dict[str, float] = {}  # 被拒绝的来源地址 → 上次记日志的时间（同一个地址十分钟内只记一条，免得被扫描时刷屏）
        self.app.on_response_prepare.append(self._no_cache)
        self.api2 = Api2(self)
        self.routes()
        self.runner = None
        self.fail = {}
        self.on_show = None

    @staticmethod
    async def _no_cache(req, resp):
        # 面板文件每次都向后端核对（有 ETag，没改就 304），重装新版本后不会用到旧脚本
        resp.headers.setdefault("Cache-Control", "no-cache")

    @property
    def db(self):
        return self.core.db

    @property
    def eng(self):
        return self.core.engine

    # ---------- 认证 ----------
    def sessions(self) -> dict:
        s = self.db.kv("sessions") or {}
        now = time.time()
        return {k: v for k, v in s.items() if v > now}

    def new_session(self, resp: web.Response):
        tok = secrets.token_urlsafe(32)
        s = self.sessions()
        s[hashlib.sha256(tok.encode()).hexdigest()] = time.time() + SESSION_DAYS * 86400
        self.db.set_kv("sessions", s)
        resp.set_cookie("mb_session", tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Strict")

    def authed(self, req) -> bool:
        tok = req.cookies.get("mb_session", "")
        return bool(tok) and hashlib.sha256(tok.encode()).hexdigest() in self.sessions()

    @web.middleware
    async def auth(self, req: web.Request, handler):
        has_pw = bool(self.db.secret("panel_password"))
        kind = addr_kind(req.remote)
        if kind != "local" and (not has_pw or not self.core.settings.g("panel_lan")):
            return web.Response(status=403, text="面板只允许本机访问")
        if kind == "public" and not self.core.settings.g("panel_public"):  # 开了局域网访问，也只接受本机 / 局域网 / Tailscale 来的
            ip = req.remote or "?"
            if time.time() - self.denied_at.get(ip, 0) > 600:
                self.denied_at[ip] = time.time()
                self.eng.log("warn", "server", f"拒绝了来自 {ip} 的面板访问：不是本机、局域网或 Tailscale 地址（要允许公网访问，在设置里打开「也允许公网访问面板」，不建议）")
            return web.Response(status=403, text="面板只接受本机、局域网和 Tailscale 来的访问")
        if req.path == "/" and req.query.get("t"):
            resp = web.Response(status=302, headers={"Location": "/"})
            if hmac.compare_digest(req.query["t"], self.launch_token):
                self.new_session(resp)
            return resp
        if req.path == "/api/_show":
            if is_local(req) and hmac.compare_digest(req.headers.get("X-Launch", ""), self.launch_token) and self.on_show:
                self.on_show()
            return web.json_response({"ok": True})
        if req.path.startswith("/api/") and req.path not in PUBLIC and not self.authed(req):
            if has_pw or not is_local(req):
                return web.json_response({"error": "请先登录"}, status=401)
        try:
            return await handler(req)
        except web.HTTPException:
            raise
        except Exception as e:  # 接口里没处理的异常：记进日志（诊断包里有），给页面一个说得清的提示
            import traceback
            self.eng.log("error", "server", f"{req.method} {req.path} 出错：{type(e).__name__}: {e}",
                         {"trace": traceback.format_exc()[-1500:]})
            return web.json_response({"error": f"程序内部出错了：{type(e).__name__}: {e}（详情在日志页）"}, status=500)

    async def state(self, req):
        return web.json_response({"version": VERSION, "has_password": bool(self.db.secret("panel_password")),
                                  "needs_setup": self.core.needs_setup(), "authed": self.authed(req),
                                  "theme": self.core.settings.g("theme"), "lang": self.core.settings.g("ui_lang")})

    async def login(self, req):
        ip = req.remote or ""
        if self.fail.get(ip, 0) >= 5:
            await asyncio.sleep(3)
        body = await req.json()
        if not check_pw(body.get("password", ""), self.db.secret("panel_password")):
            self.fail[ip] = self.fail.get(ip, 0) + 1
            await asyncio.sleep(1)
            return web.json_response({"error": "密码不对"}, status=403)
        self.fail.pop(ip, None)
        resp = web.json_response({"ok": True})
        self.new_session(resp)
        return resp

    async def logout(self, req):
        tok = req.cookies.get("mb_session", "")
        s = self.sessions()
        s.pop(hashlib.sha256(tok.encode()).hexdigest(), None)
        self.db.set_kv("sessions", s)
        resp = web.json_response({"ok": True})
        resp.del_cookie("mb_session")
        return resp

    async def set_password(self, req):
        body = await req.json()
        cur = self.db.secret("panel_password")
        if cur and not (self.authed(req) or check_pw(body.get("old", ""), cur)):
            return web.json_response({"error": "原密码不对"}, status=403)
        if not cur and not is_local(req):
            return web.json_response({"error": "首次设置只能在本机进行"}, status=403)
        if len(body.get("password", "")) < 4:
            return web.json_response({"error": "密码至少 4 位"}, status=400)
        self.db.set_secret("panel_password", hash_pw(body["password"]))
        self.db.set_kv("sessions", {})
        resp = web.json_response({"ok": True})
        self.new_session(resp)
        return resp

    # ---------- 事件推送 ----------
    async def events(self, req):
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(req)
        q: asyncio.Queue = asyncio.Queue()
        self.core.subscribers.add(q)
        try:
            sender = asyncio.create_task(self._pump(ws, q))
            async for _ in ws:
                pass
            sender.cancel()
        finally:
            self.core.subscribers.discard(q)
        return ws

    async def _pump(self, ws, q):
        while True:
            ev = await q.get()
            await ws.send_str(json.dumps(ev, ensure_ascii=False, default=str))

    # ---------- 总数据 ----------
    @staticmethod
    def secret_fields(platform: str) -> list[str]:
        from .platforms import BY_ID
        return [f["key"] for f in (BY_ID.get(platform) or {}).get("fields", []) if f.get("secret")]

    def account_view(self, a: dict) -> dict:
        b = self.eng.bots.get(a["id"])
        ch = b.channel if b else None
        v = dict(a, status=ch.status if ch else "stopped", detail=ch.detail if ch else "",
                 self_id=ch.self_id if ch else "", self_name=ch.self_name if ch else "",
                 has_token=bool(self.db.secret(f"discord:{a['id']}")),
                 secrets_set={k: self.db.secret_state(f"acct:{a['id']}:{k}") for k in self.secret_fields(a["platform"])})
        if a["platform"] == "wechat":
            v["pending"] = ch.pending if ch else None
            v["bound"] = self.db.kv(f"wxbind:{a['id']}")  # 绑定的小号身份（昵称、微信号）；不放进账号文档，免得编辑账号时被旧值盖掉
        if a["platform"] == "qq" and ch:
            v["qr"] = getattr(ch, "qr", "")
            v["webui"] = f"http://127.0.0.1:{a.get('webui_port') or 6101}/webui?token={ch.webui_token()}"
        return v

    async def bootstrap(self, req):
        db, s = self.db, self.core.settings
        arch = [{"id": e["id"], "title": e["id"], "src": e.get("src", ""),
                 "core": e["coreProposition"], "reality": e["realityExpansion"], "tendency": e["characterTendency"]}
                for e in db.docs("archetype")]
        return web.json_response({
            "version": VERSION, "data_dir": str(data_dir()),
            "schema": SCHEMA, "scope_label": SCOPE_LABEL, "commands": COMMANDS, "command_table": commands.all_rows(),
            "platforms": db.kv("platforms") or {}, "roles": db.docs("role"),
            "accounts": [self.account_view(a) for a in db.docs("account")],
            "personas": db.docs("persona"), "modules": db.docs("module"), "people": db.docs("person"),
            "pgroups": db.docs("pgroup"), "chats": db.docs("chat"), "spaces": db.docs("space"), "cgroups": db.docs("cgroup"),
            "endpoints": [self.api2.endpoint_view(e) for e in db.docs("endpoint")],
            "archetypes": arch, "presets": PRESETS, "attitude_all": db.kv("attitude_all"),
            "settings": s.data, "running": self.core.running, "autostart": system.get_autostart(),
            "napcat_dir": db.kv("napcat_dir") or "", "memory_kinds": KINDS, "hard_cap": self.core.llm.hard_cap_state(),
            "wechat_notice": db.kv("wechat_notice"), "wechat_lib_update": db.kv("wxlib_update"),
            "lan_ips": system.lan_ips(), "needs_setup": self.core.needs_setup(),
            "old_bot_guess": next((str(p) for p in OLD_BOT_GUESS if (p / "config.json").exists()), ""),
        })

    async def status(self, req):
        day = time.strftime("%Y-%m-%d")
        u = self.db.x("SELECT COALESCE(SUM(calls),0), COALESCE(SUM(prompt),0), COALESCE(SUM(completion),0) FROM usage WHERE day=?", (day,)).fetchone()
        return web.json_response({"running": self.core.running,
                                  "accounts": [self.account_view(a) for a in self.db.docs("account")],
                                  "today": {"calls": u[0], "prompt": u[1], "completion": u[2]},
                                  "rooms": len(self.eng.rooms)})

    async def bans(self, req):
        return web.json_response(self.eng.ban_view())

    async def notices(self, req):
        return web.json_response(self.db.kv("notices") or [])

    async def notices_read(self, req):
        body = await req.json() if req.can_read_body else {}
        self.eng.mark_notices_read(body.get("ids") or None)
        return web.json_response({"ok": True})

    # ---------- 文档 ----------
    async def list_docs(self, req):
        kind = req.match_info["kind"]
        if kind not in DOC_KINDS:
            raise web.HTTPNotFound()
        if kind == "account":
            return web.json_response([self.account_view(a) for a in self.db.docs(kind)])
        return web.json_response(self.db.docs(kind))

    async def put_doc(self, req):
        kind = req.match_info["kind"]
        if kind not in DOC_KINDS:
            raise web.HTTPNotFound()
        doc = await req.json()
        if kind == "account":
            for k in ("pending", "bound"):  # 这两项是面板看的，不存进账号文档
                doc.pop(k, None)
        old = self.db.doc(kind, doc.get("id", "")) if doc.get("id") else None
        if kind == "account" and not old:
            doc = self.new_account(doc)
        note = doc.pop("_note", "")
        if kind == "person":  # 关系方案整理成标准样子；人物卡自己的身份、称呼、态度跟着「全体角色」那套走
            doc["schemes"] = self.eng.people.clean_schemes(doc.get("schemes"))
            self.eng.people.sync_mirror(doc)
        if kind == "role":  # 角色标签；对个人的态度现在只由人物卡上的关系方案决定，不再有角色自己的覆盖
            doc["tags"] = list(dict.fromkeys(str(t).strip() for t in (doc.get("tags") or []) if str(t).strip()))
            doc["attitudes"] = {}
        if kind == "persona" and not old:  # 新建的人设和已有的重名，自动加后缀 2、3
            doc["name"] = personas.unique_name(self.db, doc.get("name") or "人设", doc.get("id"))
        if kind == "person" and doc.get("self"):  # 「用户（面板使用者）」全表只能有一张：新打上的这张生效，别的自动取消
            for o in self.db.docs("person"):
                if o.get("self") and o["id"] != doc.get("id"):
                    o["self"] = False
                    self.db.put("person", o)
        doc = self.db.put(kind, doc)
        if kind == "persona":
            personas.save_history(self.db, doc["id"], note)
        elif kind == "module":
            personas.save_history(self.db, f"module:{doc['id']}")
        self.eng.reload()
        if kind == "account" and doc["id"] in self.eng.bots:
            self.eng.bots[doc["id"]].account = doc
        return web.json_response(self.account_view(doc) if kind == "account" else doc)

    def new_account(self, doc: dict) -> dict:
        doc["id"] = new_id("ac_")
        doc.setdefault("enabled", True)
        if doc["platform"] == "qq":
            used = {a.get("ws_port") for a in self.db.docs("account")}
            port = next(p for p in range(3101, 3200) if p not in used)
            doc.update(ws_port=port, webui_port=port + 3000, manage_napcat=doc.get("manage_napcat", True))
        if doc["platform"] == "wechat":
            doc.setdefault("listen", [])
        return doc

    async def del_doc(self, req):
        kind, id = req.match_info["kind"], req.match_info["id"]
        if kind not in DOC_KINDS or id.startswith("@"):
            raise web.HTTPBadRequest(text="内置项不能删除")
        if kind == "person" and (self.db.doc("person", id) or {}).get("self"):
            raise web.HTTPBadRequest(text="用户卡是系统里固定的一张，不能删（可以改里面的内容）")
        if kind == "account":
            await self.eng.stop_account(id)
            self.db.del_kv(f"wxbind:{id}")
            self.db.set_secret(f"discord:{id}", "")
            self.db.set_secret(f"onebot:{id}", "")
        if kind == "persona":
            for m in self.db.docs("module"):
                if m.get("owner") == id:
                    self.db.delete("module", m["id"])
        if kind == "module":
            for p in self.db.docs("persona"):
                if any(it["m"] == id for it in p.get("items", [])):
                    p["items"] = [it for it in p["items"] if it["m"] != id]
                    self.db.put("persona", p)
        if kind == "role":
            self.core.settings.drop_scope(f"role:{id}")
        if kind == "endpoint":
            self.db.set_secret(f"endpoint:{id}", "")
        self.db.delete(kind, id)
        self.eng.reload()
        return web.json_response({"ok": True})

    async def order_docs(self, req):
        kind = req.match_info["kind"]
        self.db.reorder(kind, (await req.json())["ids"])
        self.eng.reload()
        return web.json_response({"ok": True})

    async def put_secret(self, req):
        body = await req.json()
        k = body.get("k", "")
        if not (k.startswith(("endpoint:", "discord:", "acct:", "search:")) or k == "embed:api"):
            raise web.HTTPBadRequest(text="不支持的密钥")
        self.db.set_secret(k, (body.get("v") or "").strip())
        return web.json_response({"ok": True})

    # ---------- 设置 ----------
    async def get_settings(self, req):
        scope = req.query.get("scope", "global")
        return web.json_response({"scope": scope, "type": scope_type(scope), "items": self.core.settings.view(scope)})

    async def put_setting(self, req):
        b = await req.json()
        if b.get("reset"):
            self.core.settings.reset(b["scope"], b["key"])
        else:
            self.core.settings.set(b["scope"], b["key"], b["value"])
        if b["key"] == "autostart" and b["scope"] == "global":
            system.set_autostart(bool(b.get("value")) and not b.get("reset"))
        return web.json_response({"scope": b["scope"], "items": self.core.settings.view(b["scope"])})

    async def put_platforms(self, req):
        p = self.db.kv("platforms") or {}
        p.update(await req.json())
        self.db.set_kv("platforms", p)
        return web.json_response(p)

    async def put_kv(self, req):
        b = await req.json()
        if b["k"] not in ("attitude_all", "napcat_dir", "setup_done"):
            raise web.HTTPBadRequest()
        if b["k"] == "napcat_dir" and b["v"] and not napcat_mod.layout(Path(b["v"])):
            return web.json_response({"error": "这个文件夹里没找到 NapCatWinBootMain.exe"}, status=400)
        self.db.set_kv(b["k"], b["v"])
        self.eng.reload()
        return web.json_response({"ok": True})

    # ---------- 运行控制 ----------
    async def engine_start(self, req):
        await self.core.start()
        return web.json_response({"running": True})

    async def engine_stop(self, req):
        await self.core.stop()
        return web.json_response({"running": False})

    async def account_action(self, req):
        aid, act = req.match_info["id"], req.match_info["act"]
        a = self.db.doc("account", aid)
        if not a:
            raise web.HTTPNotFound()
        if act == "start":
            await self.eng.start_account(a)
            self.core.running = True
        elif act == "stop":
            await self.eng.stop_account(aid)
        return web.json_response(self.account_view(a))

    async def qr_svg(self, req):
        import segno
        text = req.query.get("text", "")
        if not text:
            raise web.HTTPBadRequest()
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    async def qr_png(self, req):
        """没开「隐藏黑窗口」时，NapCat 只把二维码存成图片。"""
        root = Path(self.db.kv("napcat_dir") or napcat_mod.default_root())
        lay = napcat_mod.layout(root)
        p = lay["cache"] / "qrcode.png" if lay else None
        if not p or not p.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-store"})

    async def napcat_info(self, req):
        root = Path(self.db.kv("napcat_dir") or napcat_mod.default_root())
        lay = napcat_mod.layout(root)
        ver = (root / "版本.txt").read_text(encoding="utf-8") if (root / "版本.txt").exists() else ""
        qq = napcat_mod.system_qq()
        return web.json_response({"dir": str(root), "found": bool(lay), "kind": lay["kind"] if lay else "",
                                  "version": ver, "system_qq": str(qq) if qq else "", "qq_download": napcat_mod.QQ_DOWNLOAD})

    async def napcat_download(self, req):
        async def job():
            try:
                last = [0.0]

                def prog(done, total):
                    if time.time() - last[0] > 0.5:
                        last[0] = time.time()
                        self.core.emit("download", {"what": "napcat", "done": done, "total": total})
                root = await napcat_mod.download(prog, self.core.settings.g("proxy") or None)
                self.db.set_kv("napcat_dir", "")
                self.core.emit("download", {"what": "napcat", "finished": True, "dir": str(root)})
                self.eng.log("info", "napcat", f"NapCat 已下载到 {root}")
            except Exception as e:
                self.core.emit("download", {"what": "napcat", "error": str(e)})
                self.eng.log("error", "napcat", f"下载 NapCat 失败：{e}")
        self.eng.spawn(job())
        return web.json_response({"started": True})

    async def wechat_recover(self, req):
        """找回微信窗口：窗口留在屏幕外（比如程序被强行结束）时，把它放回可见位置。只按窗口类名找微信主窗口，不用微信自动化组件。"""
        from .channels import wechat
        from .winpos import WinApi, recover
        api = wechat.WeChatChannel.window_api or WinApi()

        def go():
            api.begin()
            return recover(api, data_dir() / "wechat_window.json")
        r = await asyncio.get_running_loop().run_in_executor(None, go)
        for b in self.eng.bots.values():  # 在跑的微信账号：管家记的位置作废，下次需要时重新记
            k = getattr(b.channel, "keeper", None)
            if k is not None and r.get("moved"):
                k.off, k.orig = False, None
        return web.json_response(r)

    async def wechat_check(self, req):
        from .channels import wechat
        out = dict(wechat.check_version(), lib=wechat.lib_installed(), lib_error="", lib_version=wechat.installed_lib_version(),
                   lib_update=self.db.kv("wxlib_update"))
        if out["lib"]:
            try:
                await asyncio.get_running_loop().run_in_executor(None, wechat.load_lib)
            except Exception as e:
                out["lib_error"] = f"{type(e).__name__}: {e}"
        return web.json_response(out)

    async def wechat_upgrade(self, req):
        """升级 wxauto4（装到 data\\pylib）。要先停掉微信账号；失败自动回退到原来的版本。"""
        from .channels import wechat
        if any(b.platform == "wechat" and b.channel and b.channel.status not in ("stopped", "error") for b in self.eng.bots.values()):
            return web.json_response({"error": "请先在账号页停止微信账号，再升级 wxauto4"}, status=400)
        try:
            r = await asyncio.get_running_loop().run_in_executor(None, wechat.upgrade_lib)
        except Exception as e:
            old = wechat.installed_lib_version()
            self.eng.log("warn", "system", f"升级 wxauto4 失败，已回退到原来的版本 {old}：{type(e).__name__}: {e}")
            return web.json_response({"error": f"升级失败，已回退到原来的版本 {old}：{e}"}, status=500)
        self.db.del_kv("wxlib_update")
        self.eng.log("info", "system", f"wxauto4 已从 {r['old']} 升级到 {r['new']}（重启程序后生效）")
        return web.json_response({"ok": True, **r})

    async def wechat_download(self, req):
        from .channels import wechat
        try:
            v = await asyncio.get_running_loop().run_in_executor(None, wechat.download_lib)
        except Exception as e:
            return web.json_response({"error": f"下载失败：{e}"}, status=500)
        return web.json_response({"ok": True, "version": v})

    async def discord_invite(self, req):
        from .channels.discord_ch import app_id_from_token, invite_url
        a = self.db.doc("account", req.match_info["id"])
        app_id = (a or {}).get("app_id") or app_id_from_token(self.db.secret(f"discord:{req.match_info['id']}"))
        return web.json_response({"app_id": app_id, "url": invite_url(app_id) if app_id else ""})

    async def profile(self, req):
        b = self.eng.bots.get(req.match_info["id"])
        if not b or not b.online:
            return web.json_response({"error": "账号没在线"}, status=400)
        body = await req.json()
        try:
            if body.get("chat_id"):
                await b.channel.set_chat_card(body["chat_id"], body.get("card", ""))
            else:
                avatar = base64.b64decode(body["avatar"].split(",")[-1]) if body.get("avatar") else None
                await b.channel.set_profile(body.get("nickname", ""), body.get("signature", ""), avatar)
        except NotImplementedError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": f"修改失败：{e}"}, status=500)
        return web.json_response({"ok": True})

    # ---------- 人设 ----------
    def sample_dyn(self, people: str, platform: str | None = None) -> dict:
        ppl = self.eng.people
        present = list(ppl.persons.values()) if people == "all" else []
        return {"people": ppl.section(present, [], None, None, platform=platform), "memory": ""}

    async def persona_preview(self, req):
        b = await req.json()
        p = b.get("persona") or self.db.doc("persona", b.get("id", ""))
        mods = dict(self.eng.modules)
        for m in b.get("modules") or []:
            mods[m["id"]] = m
        plat = b.get("platform") or "qq"
        text = personas.assemble(p, mods, plat, self.sample_dyn(b.get("people", "all"), plat), user=self.eng.people.user_name(),
                                 ai=bool(b["ai"]) if "ai" in b else bool(self.core.settings.g("admit_ai")))
        return web.json_response({"text": text, "chars": len(text)})

    async def persona_to_whole(self, req):
        p = self.db.doc("persona", req.match_info["id"])
        b = await req.json()
        whole = personas.to_whole(p, self.eng.modules, b.get("platform") or "qq")
        p.update(mode="whole", whole=whole)
        self.db.put("persona", p)
        personas.save_history(self.db, p["id"], "转成整体")
        self.eng.reload()
        return web.json_response(p)

    async def persona_copy(self, req):
        src = self.db.doc("persona", req.match_info["id"])
        nid = new_id("p_")
        items = []
        for it in src.get("items", []):
            m = self.db.doc("module", it["m"])
            if m and m.get("kind") == "exclusive":
                nm = dict(m, id=new_id("m_"), owner=nid)
                self.db.put("module", nm)
                items.append(dict(it, m=nm["id"]))
            else:
                items.append(dict(it))
        src = {k: v for k, v in src.items() if k != "template"}  # 副本是改过的变体，不再算「某模板的人设」
        p = self.db.put("persona", dict(src, id=nid, name=personas.unique_name(self.db, src["name"]), items=items))
        personas.save_history(self.db, nid, "复制")
        self.eng.reload()
        return web.json_response(p)

    async def history(self, req):
        pid = req.match_info["id"]
        rows = self.db.rows("SELECT id, ts, note FROM persona_history WHERE persona_id=? ORDER BY id DESC LIMIT 100", (pid,))
        return web.json_response(rows)

    async def history_get(self, req):
        r = self.db.x("SELECT data FROM persona_history WHERE id=?", (int(req.match_info["hid"]),)).fetchone()
        return web.json_response(json.loads(r[0]) if r else {})

    async def history_restore(self, req):
        personas.restore_history(self.db, int(req.match_info["hid"]))
        self.eng.reload()
        return web.json_response({"ok": True})

    async def trial(self, req):
        b = await req.json()
        try:
            out = await self.eng.trial(b["role_id"], b.get("platform", "qq"), b["lines"], b.get("mode", "at"))
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
        return web.json_response(out)

    # ---------- 人物 ----------
    async def people_effective(self, req):
        ppl = self.eng.people
        chat = self.db.doc("chat", req.query.get("chat", "")) if req.query.get("chat") else None
        role = self.db.doc("role", req.query.get("role", "")) if req.query.get("role") else None
        out = {}
        for p in ppl.persons.values():
            att, src = ppl.attitude(p, chat, role)
            s, rule, rest = ppl.scheme_for(p, role)
            out[p["id"]] = {"attitude": att, "from": src, "scheme": s["name"], "rule": rule,
                            "identity": s.get("identity") or "", "calls": ppl.calls_of(s), "conflict": [x["name"] for x in rest],
                            "conflicts": ppl.conflicts(p, self.db.docs("role"))}
        return web.json_response(out)

    async def people_batch(self, req):
        b = await req.json()
        for pid in b.get("ids", []):
            p = self.db.doc("person", pid)
            if not p:
                continue
            if "attitude" in b:
                self.eng.people.set_default_attitude(p, b["attitude"])
            if b.get("add_group"):
                p["groups"] = list(dict.fromkeys((p.get("groups") or []) + [b["add_group"]]))
            if b.get("remove_group"):
                p["groups"] = [g for g in p.get("groups") or [] if g != b["remove_group"]]
            self.db.put("person", p)
        self.eng.reload()
        return web.json_response({"ok": True})

    # ---------- 群与消息（人工接管）----------
    def _bot_view(self, a: dict) -> list[dict]:
        b = self.eng.bots.get(a["id"])
        return [{"id": b.aid, "role": self.eng.duty(b, None).role["name"], "platform": b.platform}] if b and b.online else []

    async def rooms(self, req):
        """群列表。带 ?all=1（消息页用）时再加上：已经配置但还没收到过消息的群和私聊（标 empty），以及所有私聊对象。"""
        out = []
        for d in self.db.docs("chat"):
            r = self.eng.rooms.get(d["id"])
            bots = [{"id": b.aid, "role": b.role["name"], "platform": b.platform}
                    for b in self.eng.room_bots(r)] if r else []
            last = self.db.x("SELECT ts, name, text FROM messages WHERE room=? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
            out.append(dict(d, bots=bots, last=dict(last) if last else None, type="group"))
        if req.query.get("all"):
            have = {x["id"] for x in out}
            accounts = {a["id"]: a for a in self.db.docs("account")}
            for a in accounts.values():  # 监听列表里填了、但还没有消息的群和私聊
                for c in a.get("listen") or []:
                    name = (c.get("name") or "").strip()
                    if not name:
                        continue
                    private = c.get("type", "group") == "private"
                    key = self.eng.dm_key(a["id"], name) if private else f"{a['platform']}:{name}"
                    if key in have:
                        continue
                    have.add(key)
                    out.append({"id": key, "platform": a["platform"], "chat_id": name, "name": name, "desc": "", "bots": self._bot_view(a),
                                "last": None, "empty": True, "type": "private" if private else "group", "account": a["id"]})
            for r in self.db.rows("SELECT room, MAX(id) AS mid FROM messages WHERE room LIKE 'dm:%' GROUP BY room"):
                _, aid, uid = r["room"].split(":", 2)
                a = accounts.get(aid)
                last = self.db.x("SELECT ts, name, text FROM messages WHERE id=?", (r["mid"],)).fetchone()
                who = self.db.x("SELECT name FROM messages WHERE room=? AND kind='human' ORDER BY id DESC LIMIT 1", (r["room"],)).fetchone()
                have.add(r["room"])
                out = [x for x in out if x["id"] != r["room"]]
                out.append({"id": r["room"], "platform": a["platform"] if a else "", "chat_id": uid, "name": who["name"] if who else uid,
                            "desc": "", "bots": self._bot_view(a) if a else [], "last": dict(last), "type": "private", "account": aid})
        out.sort(key=lambda x: -(x["last"]["ts"] if x["last"] else 0))
        return web.json_response(out)

    async def room_messages(self, req):
        key = req.match_info["key"]
        before = int(req.query.get("before") or 1 << 62)
        rows = self.db.rows("SELECT * FROM messages WHERE room=? AND id<? ORDER BY id DESC LIMIT 80", (key, before))
        return web.json_response(rows[::-1])

    async def room_send(self, req):
        b = await req.json()
        key = req.match_info["key"]
        try:
            await self.eng.manual_send(b["account"], key, b["text"])
        except Muted as ex:
            return web.json_response({"error": str(ex)}, status=409)
        except KeyError:
            return web.json_response({"error": "这个机器人现在不在这个群里或没在线"}, status=400)
        return web.json_response({"ok": True})

    async def room_reply(self, req):
        b = await req.json()
        try:
            self.eng.force_reply(b["account"], req.match_info["key"], int(b["msg_id"]))
        except KeyError:
            return web.json_response({"error": "这个机器人现在不在这个群里或没在线"}, status=400)
        return web.json_response({"ok": True})

    async def room_recall(self, req):
        b = await req.json()
        if req.match_info["key"].startswith("dm:"):
            return web.json_response({"ok": False, "msg": "私聊暂时不支持撤回"})
        bot, room = self.eng.bots.get(b["account"]), self.eng.rooms.get(req.match_info["key"])
        if not bot or not room:
            return web.json_response({"error": "机器人不在线"}, status=400)
        ok = await self.eng.recall_last(bot, room)
        return web.json_response({"ok": bool(ok), "msg": {None: "这个平台不支持撤回", False: "没有可撤回的"}.get(ok, "已撤回")})

    # ---------- 记忆 ----------
    async def memories(self, req):
        q = req.query
        sql, args = "SELECT * FROM memories WHERE 1=1", []
        for k in ("space", "kind", "status", "room"):
            if q.get(k):
                sql += f" AND {k}=?"
                args.append(q[k])
        if not q.get("kind"):
            sql += " AND kind NOT IN ('style','sticker')"
        if q.get("q"):
            sql += " AND (content LIKE ? OR subject_name LIKE ?)"
            args += [f"%{q['q']}%"] * 2
        sql += " ORDER BY id DESC LIMIT 300"
        rows = self.db.rows(sql, args)
        for r in rows:
            r["src"] = json.loads(r["src"] or "[]")
            r["meta"] = json.loads(r["meta"] or "{}")  # 前情单位：覆盖的消息范围、时间范围、所并各段、说话和被提到的人；印象：折到了哪条备忘
        spaces = [r["space"] for r in self.db.rows("SELECT DISTINCT space FROM memories")]
        return web.json_response({"items": rows, "spaces": spaces})

    async def memory_put(self, req):
        b = await req.json()
        mid = int(req.match_info["id"])
        for k in ("content", "status", "subject_name"):
            if k in b:
                self.db.x(f"UPDATE memories SET {k}=? WHERE id=?", (b[k], mid))
        if "content" in b or "subject_name" in b:
            row = self.db.rows("SELECT kind FROM memories WHERE id=?", (mid,))
            if row and row[0]["kind"] != "impression":
                self.eng.memory.pending.append(mid)
                self.eng.spawn(self.eng.memory.flush_index())
            if row and row[0]["kind"] == "recap" and "content" in b:  # 改过的单位：覆盖它的上层之后照改过的重写
                self.eng.spawn(self.eng.memory.rewrite_ancestors(mid))
        return web.json_response({"ok": True})

    # ---------- 排班：角色分配规则 ----------
    def duty_names(self):
        return {"roles": {r["id"]: r["name"] for r in self.db.docs("role")},
                "accounts": {a["id"]: a.get("name") or a.get("qq") or a["id"] for a in self.db.docs("account")},
                "chats": {c["id"]: c.get("name") or c["id"] for c in self.db.docs("chat")},
                "cgroups": {g["id"]: g["name"] for g in self.db.docs("cgroup")}}

    async def duty_overview(self, req):
        from . import rules as R
        duty, names = self.eng.duty_rules, self.duty_names()
        accounts, chats = self.db.docs("account"), self.db.docs("chat")
        rows = []
        for c in chats:
            room = self.eng.rooms.get(c["id"])
            accs = [a for a in accounts if a["platform"] == c["platform"] and (not room or a["id"] in room.accounts or not room.accounts)]
            items = []
            for a in accs:
                nn = duty.now_next(a, c["id"])
                o = duty.override(a["id"], c["id"])
                items.append(dict(nn, account=a["id"], override_until=o["until"] if o else None))
            if items:
                rows.append({"chat": c["id"], "name": c.get("name") or c["id"], "platform": c["platform"], "items": items})
        duty.rules_snapshot = duty.rules()
        rules = [dict(r, desc=R.describe(r, names)) for r in duty.rules_snapshot]
        loop = asyncio.get_running_loop()
        warnings = await loop.run_in_executor(None, lambda: duty.check(accounts, chats, 28, duty.rules_snapshot))
        return web.json_response({"rules": rules, "now": rows, "warnings": warnings, "names": names})

    async def duty_schedule(self, req):
        from datetime import datetime, timedelta
        q = req.query
        a = self.db.doc("account", q["account"])
        if not a:
            raise web.HTTPNotFound()
        start = datetime.strptime(q.get("start") or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
        days = min(int(q.get("days", 7)), 62)
        segs = self.eng.duty_rules.timeline(a, q.get("chat") or None, start, days * 1440, 15)  # 一个月约 3000 个采样点，很快
        return web.json_response({"start": start.strftime("%Y-%m-%d"), "days": days, "segments": segs})

    async def duty_autosort(self, req):
        from .rules import specificity
        rules = sorted(self.eng.duty_rules.rules(), key=specificity)
        self.db.reorder("rule", [r["id"] for r in rules])
        return web.json_response({"ids": [r["id"] for r in rules]})

    async def duty_clear(self, req):
        b = await req.json()
        self.eng.duty_rules.clear_override(b["account"], b["chat"])
        return web.json_response({"ok": True})

    # ---------- 语义回忆：向量来源、重建、各记忆空间开关 ----------
    async def embed_info(self, req):
        from . import embed
        emb = self.eng.embedder
        local = {k: dict(v, ready=embed.model_ready(k)) for k, v in embed.LOCAL.items()}
        off = set(self.db.kv("semantic_off") or [])
        spaces = {r["space"]: r["n"] for r in self.db.rows("SELECT space, COUNT(*) n FROM memories GROUP BY space")}
        for sp in self.db.docs("space"):
            spaces.setdefault(f"sp:{sp['id']}", 0)
        return web.json_response({"conf": emb.conf(), "status": emb.status(), "local": local, "api": embed.API,
                                  "has_key": bool(self.db.secret("embed:api")),
                                  "spaces": [{"space": k, "count": n, "on": k not in off} for k, n in sorted(spaces.items())],
                                  "models_dir": str(data_dir() / "models"),
                                  "ram_gb": system.total_ram_gb(),
                                  "recommended": embed.recommended_local(system.total_ram_gb())})

    async def embed_config(self, req):
        b = await req.json()
        conf = self.eng.embedder.conf()
        conf.update({k: v for k, v in b.items() if k in conf})
        self.db.set_kv("embedding", conf)
        self.eng.embedder.kick()  # 换了来源 / 模型：马上检查，需要的话立刻开始自动重建
        return web.json_response({"conf": conf, "status": self.eng.embedder.status()})

    async def embed_rebuild(self, req):
        self.eng.spawn(self.eng.embedder.rebuild())
        return web.json_response({"started": True})

    async def embed_download(self, req):
        from . import embed
        key = (await req.json())["preset"]
        loop = asyncio.get_running_loop()
        last = [0.0]

        def prog(name, done, total):
            if time.time() - last[0] > 0.5:
                last[0] = time.time()
                loop.call_soon_threadsafe(self.core.emit, "download", {"what": "embed", "file": name, "done": done, "total": total})

        async def job():
            try:
                await loop.run_in_executor(None, embed.download_model, key, prog)
                self.core.emit("download", {"what": "embed", "finished": True, "preset": key})
                self.eng.embedder.kick()  # 模型下好了：之前因为没模型而没算的，现在补
                self.eng.log("info", "memory", f"向量模型 {key} 已下载")
            except Exception as e:
                self.core.emit("download", {"what": "embed", "error": str(e)})
                self.eng.log("error", "memory", f"向量模型下载失败：{e}")
        self.eng.spawn(job())
        return web.json_response({"started": True})

    async def embed_test(self, req):
        b = await req.json()
        try:
            t = time.time()
            q = (await self.eng.embedder.embed([b.get("a", "")], query=True))[0]
            d = (await self.eng.embedder.embed([b.get("b", "")]))[0]
            ms = (time.time() - t) * 1000
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)
        sim = sum(x * y for x, y in zip(q, d)) / ((sum(x * x for x in q) ** 0.5) * (sum(y * y for y in d) ** 0.5) or 1)
        return web.json_response({"similarity": round(sim, 3), "ms": round(ms, 1), "dim": len(q)})

    async def semantic_off(self, req):
        b = await req.json()
        off = set(self.db.kv("semantic_off") or [])
        (off.add if b.get("off") else off.discard)(b["space"])
        self.db.set_kv("semantic_off", sorted(off))
        return web.json_response({"ok": True})

    def delete_memories(self, ids: list[int]):
        recap = [r["id"] for r in self.db.rows(f"SELECT id FROM memories WHERE kind='recap' AND id IN ({','.join(str(i) for i in ids) or '0'})")]
        if recap:
            self.eng.memory.delete_units(recap)  # 前情单位：连同覆盖它的上层一起删
        rest = [i for i in ids if i not in recap]
        if rest:
            rows = self.db.rows(f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(rest))})", rest)
            self.db.x(f"DELETE FROM memories WHERE id IN ({','.join('?' * len(rest))})", rest)
            self.core.reset.rebuild_affected(rows)  # 删掉的普通备忘属于谁，就用剩下的备忘重写谁的印象

    async def memory_del(self, req):
        self.delete_memories([int(req.match_info["id"])])
        return web.json_response({"ok": True})

    async def memory_bulk(self, req):
        b = await req.json()
        ids = [int(i) for i in b.get("ids", [])]
        if ids:
            q = ",".join("?" * len(ids))
            if b.get("action") == "approve":
                self.db.x(f"UPDATE memories SET status='active' WHERE id IN ({q})", ids)
            elif b.get("action") == "delete":
                self.delete_memories(ids)
        return web.json_response({"ok": True})

    async def memory_import(self, req):
        data = await req.post()
        f = data["file"]
        raw = f.file.read()
        text = raw.decode("utf-8-sig", "replace") if not raw.startswith(b"\xff\xfe") else raw.decode("utf-16")
        items = parse_chatlog(text)
        if not items:
            return web.json_response({"error": "没认出聊天记录格式（支持 QQ 聊天记录导出插件的 JSON/TXT 和 QQ 自带的 TXT 导出）"}, status=400)
        room = data["room"]
        roles = [r for r in (data.get("roles") or "").split(",") if r]
        if data.get("dry"):
            seg = self.core.settings.get("memory_segment", platform=room.split(":")[0], chat=room)
            return web.json_response({"messages": len(items), "calls": -(-len(items) // seg)})
        self.eng.spawn(self.eng.import_chatlog(room, items, roles))
        return web.json_response({"started": True, "messages": len(items)})

    # ---------- 用量、日志 ----------
    USAGE_COLS = {"day": "day", "role": "role_id", "room": "room", "model": "model", "purpose": "purpose", "platform": "platform"}

    def usage_rows(self, by: str, days: int) -> list[dict]:
        col = self.USAGE_COLS[by]
        since = since_day(days)
        return self.db.rows(f"""SELECT {col} AS k, SUM(calls) calls, SUM(prompt) prompt, SUM(cached) cached, SUM(completion) completion,
                                SUM(reasoning) reasoning, SUM(cache_write) cache_write
                                FROM usage WHERE day>=? GROUP BY {col} ORDER BY {col} {'DESC' if by == 'day' else ''}""", (since,))

    async def usage(self, req):
        return web.json_response(self.usage_rows(req.query.get("by", "day"), int(req.query.get("days", 30))))

    async def logs(self, req):
        q = req.query
        sql, args = "SELECT * FROM logs WHERE 1=1", []
        if q.get("cat"):
            sql += " AND cat=?"
            args.append(q["cat"])
        if q.get("level"):
            sql += " AND level=?"
            args.append(q["level"])
        else:
            sql += " AND level!='debug'" if not q.get("debug") else ""
        if q.get("q"):
            sql += " AND text LIKE ?"
            args.append(f"%{q['q']}%")
        if q.get("before"):
            sql += " AND id<?"
            args.append(int(q["before"]))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(q.get("limit", 200)))
        rows = self.db.rows(sql, args)
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else None
        return web.json_response(rows)

    # ---------- 数据 ----------
    async def export(self, req):
        b = await req.json()
        name = f"群聊机器人数据-{time.strftime('%Y%m%d-%H%M')}{'-含密钥' if b.get('with_secrets') else ''}.zip"
        tmp = Path(tempfile.gettempdir()) / f"mb-export-{secrets.token_hex(4)}.zip"
        self.core.export(tmp, bool(b.get("with_secrets")))
        data = tmp.read_bytes()
        tmp.unlink()
        from urllib.parse import quote
        return web.Response(body=data, content_type="application/zip",
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    async def import_data(self, req):
        data = await req.post()
        tmp = Path(tempfile.gettempdir()) / f"mb-import-{secrets.token_hex(4)}.zip"
        tmp.write_bytes(data["file"].file.read())
        try:
            m = await self.core.import_zip(tmp)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)
        finally:
            tmp.unlink(missing_ok=True)
        return web.json_response({"ok": True, "manifest": m})

    async def import_old(self, req):
        b = await req.json()
        p = Path(b.get("path") or "")
        if not (p / "config.json").exists() and not (p / "persona_core.md").exists():
            return web.json_response({"error": "这个文件夹里没有旧程序的 config.json 或 persona_core.md"}, status=400)
        return web.json_response({"done": self.core.import_old(p)})

    async def backup_now(self, req):
        p = self.core.backup("manual")
        return web.json_response({"path": str(p)})

    async def backups(self, req):
        d = data_dir() / "backups"
        return web.json_response([{"name": f.name, "size": f.stat().st_size, "ts": f.stat().st_mtime}
                                  for f in sorted(d.glob("*.db"), reverse=True)] if d.exists() else [])

    async def lan(self, req):
        port = self.core.settings.g("panel_port")
        return web.json_response({"ips": system.lan_ips(), "port": port, "enabled": self.core.settings.g("panel_lan")})

    async def index(self, req):
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    # ---------- 路由 ----------
    def routes(self):
        r = self.app.router
        A = r.add_route
        self.api2.routes(A)  # 先登记：要排在 /api/accounts/{id}/{act} 这类通配路由前面
        A("GET", "/api/state", self.state)
        A("POST", "/api/_show", self.state)
        A("POST", "/api/login", self.login)
        A("POST", "/api/logout", self.logout)
        A("POST", "/api/setup/password", self.set_password)
        A("GET", "/api/events", self.events)
        A("GET", "/api/bootstrap", self.bootstrap)
        A("GET", "/api/status", self.status)
        A("GET", "/api/notices", self.notices)
        A("GET", "/api/bans", self.bans)
        A("POST", "/api/notices/read", self.notices_read)
        A("GET", "/api/docs/{kind}", self.list_docs)
        A("PUT", "/api/docs/{kind}", self.put_doc)
        A("DELETE", "/api/docs/{kind}/{id}", self.del_doc)
        A("POST", "/api/docs/{kind}/order", self.order_docs)
        A("PUT", "/api/secret", self.put_secret)
        A("GET", "/api/settings", self.get_settings)
        A("PUT", "/api/settings", self.put_setting)
        A("PUT", "/api/platforms", self.put_platforms)
        A("PUT", "/api/kv", self.put_kv)
        A("POST", "/api/engine/start", self.engine_start)
        A("POST", "/api/engine/stop", self.engine_stop)
        A("POST", "/api/accounts/{id}/profile", self.profile)
        A("POST", "/api/accounts/{id}/{act}", self.account_action)
        A("GET", "/api/qr.svg", self.qr_svg)
        A("GET", "/api/napcat/qrcode.png", self.qr_png)
        A("GET", "/api/napcat/info", self.napcat_info)
        A("POST", "/api/napcat/download", self.napcat_download)
        A("POST", "/api/wechat/recover", self.wechat_recover)
        A("GET", "/api/wechat/check", self.wechat_check)
        A("POST", "/api/wechat/upgrade", self.wechat_upgrade)
        A("POST", "/api/wechat/download", self.wechat_download)
        A("GET", "/api/discord/invite/{id}", self.discord_invite)
        A("POST", "/api/personas/preview", self.persona_preview)
        A("POST", "/api/personas/{id}/to_whole", self.persona_to_whole)
        A("POST", "/api/personas/{id}/copy", self.persona_copy)
        A("GET", "/api/history/{id}", self.history)
        A("GET", "/api/history/item/{hid}", self.history_get)
        A("POST", "/api/history/item/{hid}/restore", self.history_restore)
        A("POST", "/api/trial", self.trial)
        A("GET", "/api/people/effective", self.people_effective)
        A("POST", "/api/people/batch", self.people_batch)
        A("GET", "/api/rooms", self.rooms)
        A("GET", "/api/rooms/{key}/messages", self.room_messages)
        A("POST", "/api/rooms/{key}/send", self.room_send)
        A("POST", "/api/rooms/{key}/reply", self.room_reply)
        A("POST", "/api/rooms/{key}/recall", self.room_recall)
        A("GET", "/api/memories", self.memories)
        A("GET", "/api/duty", self.duty_overview)
        A("GET", "/api/duty/schedule", self.duty_schedule)
        A("POST", "/api/duty/autosort", self.duty_autosort)
        A("POST", "/api/duty/clear", self.duty_clear)
        A("GET", "/api/embed", self.embed_info)
        A("PUT", "/api/embed/config", self.embed_config)
        A("POST", "/api/embed/rebuild", self.embed_rebuild)
        A("POST", "/api/embed/download", self.embed_download)
        A("POST", "/api/embed/test", self.embed_test)
        A("PUT", "/api/memory/semantic_off", self.semantic_off)
        A("PUT", "/api/memories/{id}", self.memory_put)
        A("DELETE", "/api/memories/{id}", self.memory_del)
        A("POST", "/api/memories/bulk", self.memory_bulk)
        A("POST", "/api/memory/import", self.memory_import)
        A("GET", "/api/usage", self.usage)
        A("GET", "/api/logs", self.logs)
        A("POST", "/api/export", self.export)
        A("POST", "/api/import", self.import_data)
        A("POST", "/api/import_old", self.import_old)
        A("POST", "/api/backup", self.backup_now)
        A("GET", "/api/backups", self.backups)
        A("GET", "/api/lan", self.lan)
        A("GET", "/", self.index)
        r.add_static("/", WEB, show_index=False)

    async def start(self, host: str, port: int):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
