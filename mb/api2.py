"""补充四新增的面板接口：服务商目录、角色模板、构型导入、用量扩展、读文件、版本提醒、诊断包、体验模式……

和 server.py 里的接口一样受登录保护；这里只放新增的，免得 server.py 继续膨胀。
"""
import asyncio
import csv
import io
import json
import time
import zipfile

from aiohttp import web

from . import AUTHOR, AUTHOR_HOME, LICENSE_NAME, REPO, REPO_URL, VERSION, commands, customcmd, personas, prefilter, providers, templates
from . import search as search_mod
from .db import since_day
from .paths import RES
from .stickers import FAV_PLATFORMS
from . import tls



class Api2:
    def __init__(self, server):
        self.s = server

    @property
    def db(self):
        return self.s.db

    @property
    def core(self):
        return self.s.core

    @property
    def eng(self):
        return self.s.eng

    def routes(self, A):
        A("GET", "/api/providers", self.providers_list)
        A("POST", "/api/providers/refresh", self.providers_refresh)
        A("GET", "/api/providers/{pid}/models", self.provider_models)
        A("GET", "/api/endpoints", self.endpoints_list)
        A("POST", "/api/endpoints/{id}/test", self.endpoint_test)
        A("GET", "/api/endpoints/{id}/models", self.endpoint_models)
        A("POST", "/api/endpoints/{id}/vision_test", self.endpoint_vision_test)
        A("GET", "/api/models/watch", self.watch_get)
        A("POST", "/api/models/watch/check", self.watch_check)
        A("POST", "/api/models/watch/{action}", self.watch_act)
        A("GET", "/api/templates", self.templates_list)
        A("GET", "/api/templates/{tid}", self.template_preview)
        A("POST", "/api/templates/{tid}/use", self.template_use)
        A("POST", "/api/templates/{tid}/copy_local", self.template_copy_local)
        A("GET", "/api/templates/{tid}/local", self.template_local_get)
        A("PUT", "/api/templates/{tid}/local", self.template_local_save)
        A("POST", "/api/templates/{tid}/restore", self.template_restore)
        A("POST", "/api/archetypes/import", self.archetypes_import)
        A("GET", "/api/platform_specs", self.platform_specs)
        A("POST", "/api/accounts/{id}/wechat_confirm", self.wechat_confirm)
        A("POST", "/api/accounts/{id}/wechat_rebind", self.wechat_rebind)
        A("POST", "/api/accounts/{id}/line_register", self.line_register)
        A("POST", "/api/accounts/{id}/youtube_oauth", self.youtube_oauth)
        A("POST", "/api/accounts/{id}/bilibili_qr", self.bilibili_qr_start)
        A("GET", "/api/accounts/{id}/bilibili_qr", self.bilibili_qr_poll)
        A("GET", "/api/about", self.about)
        A("GET", "/api/update", self.update_info)
        A("POST", "/api/update/dismiss", self.update_dismiss)
        A("POST", "/api/diagnostics", self.diagnostics)
        A("GET", "/api/demo", self.demo_state)
        A("POST", "/api/demo/start", self.demo_start)
        A("POST", "/api/demo/stop", self.demo_stop)
        A("POST", "/api/demo/say", self.demo_say)
        A("POST", "/api/demo/chatter", self.demo_chatter)
        A("POST", "/api/memory/reset/preview", self.reset_preview)
        A("POST", "/api/memory/reset", self.reset_run)
        A("GET", "/api/memory/reset/last", self.reset_last)
        A("POST", "/api/memory/reset/undo", self.reset_undo)
        A("GET", "/api/commands", self.commands_get)
        A("PUT", "/api/commands", self.commands_put)
        A("POST", "/api/commands/test", self.commands_test)
        A("GET", "/api/tools", self.tools_info)
        A("PUT", "/api/search", self.search_put)
        A("GET", "/api/prefilter", self.prefilter_get)
        A("PUT", "/api/prefilter", self.prefilter_put)
        A("GET", "/api/reminders", self.reminders_list)
        A("DELETE", "/api/reminders/{id}", self.reminder_delete)
        A("GET", "/api/styles", self.styles_list)
        A("PUT", "/api/styles", self.style_put)
        A("GET", "/api/stickers", self.stickers_list)
        A("GET", "/api/stickers/fav", self.stickers_fav)
        A("POST", "/api/stickers/sync", self.stickers_sync)
        A("POST", "/api/stickers/redescribe", self.stickers_redescribe)
        A("DELETE", "/api/stickers/{id}", self.sticker_delete)
        A("GET", "/api/stickers/{id}/image", self.sticker_image)
        A("GET", "/api/tune", self.tune_list)
        A("GET", "/api/usage/summary", self.usage_summary_api)
        A("GET", "/api/usage/export.csv", self.usage_csv)

    # ---------- 服务商 ----------
    def endpoint_view(self, e: dict) -> dict:
        st = self.db.kv("ep_status", {}).get(e["id"]) or {}
        p = providers.provider(e.get("provider", "")) or {}
        return dict(e, **self.core.llm.key_state(e), status=st, doc=p.get("doc", ""))

    async def providers_list(self, req):
        return web.json_response({"providers": providers.catalog_list(), "dialects": providers.DIALECTS,
                                  "protocols": providers.PROTOCOLS, "updated": providers.catalog().get("deepseek") and True})

    async def providers_refresh(self, req):
        try:
            r = await providers.refresh(self.core.settings.g("proxy") or None)
        except Exception as e:
            return web.json_response({"error": f"更新失败：{e}"}, status=400)
        return web.json_response(r)

    async def provider_models(self, req):
        return web.json_response({"models": providers.models_of(req.match_info["pid"])})

    async def endpoints_list(self, req):
        return web.json_response([self.endpoint_view(e) for e in self.db.docs("endpoint")])

    async def endpoint_test(self, req):
        """GET /models：只列模型，不耗额度。结果记成连通状态点。"""
        eid = req.match_info["id"]
        st = self.db.kv("ep_status", {})
        try:
            models = await self.core.llm.list_models(eid)
            st[eid] = {"ok": True, "ts": time.time(), "n": len(models)}
            self.db.set_kv("ep_status", st)
            return web.json_response({"ok": True, "models": models})
        except Exception as e:
            st[eid] = {"ok": False, "ts": time.time(), "msg": str(e)[:200]}
            self.db.set_kv("ep_status", st)
            return web.json_response({"error": f"连不上：{e}"}, status=400)

    async def endpoint_models(self, req):
        """先问服务商，问不到就用内置目录里的清单。"""
        eid = req.match_info["id"]
        ep = self.db.doc("endpoint", eid) or {}
        live, err = [], ""
        try:
            live = await self.core.llm.list_models(eid)
        except Exception as e:
            err = str(e)[:150]
        cat = providers.models_of(ep.get("provider", ""))
        info = {m["id"]: m for m in cat}
        ids = live or [m["id"] for m in cat]
        return web.json_response({"live": bool(live), "error": err,
                                  "models": [dict(info.get(i, {"id": i, "name": i}), id=i) for i in ids]})

    async def endpoint_vision_test(self, req):
        """真调一次：让模型读一张写着三位数字的小图，看它到底能不能看图（一张小图，几百 token，记在用量的「试聊」下）。"""
        from . import visioncheck
        eid = req.match_info["id"]
        b = await req.json() if req.can_read_body else {}
        model = (b.get("model") or self.core.settings.g("vis_model") or "").strip()
        if not model:
            return web.json_response({"error": "先填一个要测的模型名"}, status=400)
        try:
            ep, key = self.core.llm.endpoint(eid)
            r = await visioncheck.check(self.core.llm, ep, key, model)
        except Exception as e:
            return web.json_response({"error": f"没测成：{e}"}, status=400)
        u = r["usage"]
        self.db.add_usage(None, "", "", "trial", model, u["prompt"], u["cached"], u["completion"], u["reasoning"], u["cache_write"])
        return web.json_response(dict(r, model=model))

    # ---------- 新模型提醒与替换 ----------
    async def watch_get(self, req):
        return web.json_response(self.eng.watch.view())

    async def watch_check(self, req):
        """现在检查新模型：立刻查一次，到期没提醒过的照常提醒。"""
        w = self.eng.watch
        report = await w.check(force=True)
        w.remind()
        return web.json_response(dict(text=w.summary(report)))

    async def watch_act(self, req):
        """一键替换 replace / 只换能换的 replace_able / 看图也换 replace_vision / 暂不换 snooze / 一直不换 never / 撤销 revoke。结果文字在 text 里，ok 说明成没成。"""
        b = await req.json()
        return web.json_response(await self.eng.watch.act(req.match_info["action"], b.get("key") or ""))

    # ---------- 角色模板与构型 ----------
    async def templates_list(self, req):
        return web.json_response(templates.listing())

    async def template_preview(self, req):
        try:
            return web.json_response(templates.preview(req.match_info["tid"]))
        except KeyError:
            raise web.HTTPNotFound(text="没有这个模板")

    async def template_use(self, req):
        b = await req.json() if req.can_read_body else {}
        try:
            role = templates.instantiate(self.db, req.match_info["tid"], name=(b.get("name") or "").strip() or None, settings=self.core.settings)
        except KeyError:
            raise web.HTTPNotFound(text="没有这个模板")
        self.eng.reload()
        return web.json_response(role)

    def _template_op(self, tid, fn, *args):
        try:
            return fn(tid, *args)
        except KeyError:
            raise web.HTTPNotFound(text="没有这个模板")
        except ValueError as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")

    async def template_copy_local(self, req):
        p = self._template_op(req.match_info["tid"], templates.copy_to_local)
        return web.json_response({"path": str(p), "file": p.name})

    async def template_local_get(self, req):
        return web.json_response(self._template_op(req.match_info["tid"], templates.read_local))

    async def template_local_save(self, req):
        b = await req.json()
        self._template_op(req.match_info["tid"], templates.save_local, b.get("text") or "")
        return web.json_response({"ok": True})

    async def template_restore(self, req):
        self._template_op(req.match_info["tid"], templates.restore_bundled)
        return web.json_response({"ok": True})

    async def archetypes_import(self, req):
        """导入构型文件（作者卡的构型 JSON 格式）：只取 id 和三条正文，其余字段一律丢弃。"""
        data = await req.post()
        f = data["file"]
        try:
            entries, bad, dropped = personas.parse_archetype_file(f.file.read(), getattr(f, "filename", "") or "")
        except Exception as e:
            return web.json_response({"error": f"读不懂这个文件：{e}"}, status=400)
        for e in entries:
            self.db.put("archetype", e)
        self.eng.reload()
        return web.json_response({"imported": len(entries), "skipped": bad, "dropped_field_kinds": dropped})

    # ---------- 重置记忆 ----------
    async def reset_preview(self, req):
        b = await req.json()
        try:
            return web.json_response(self.core.reset.preview(b.get("scope") or {}, b.get("kinds") or []))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def reset_run(self, req):
        b = await req.json()
        if not b.get("confirm"):
            return web.json_response({"error": "要先确认"}, status=400)
        try:
            return web.json_response(self.core.reset.run(b.get("scope") or {}, b.get("kinds") or []))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def reset_last(self, req):
        return web.json_response(self.core.reset.last() or {})

    async def reset_undo(self, req):
        try:
            return web.json_response(self.core.reset.undo())
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    # ---------- 自定义指令 ----------
    async def commands_get(self, req):
        from .settings import COMMANDS
        return web.json_response({"rules": customcmd.rules(self.db), "matches": customcmd.MATCHES, "actions": customcmd.ACTIONS,
                                  "perm_modes": customcmd.PERM_MODES, "builtin": {k: COMMANDS.get(k, "忘掉我") for k in customcmd.BUILTIN},
                                  "table": commands.all_rows()})

    async def commands_put(self, req):
        """整张表一起存（增删改、排序、启停、导入都是它）。有一条不合法就整张不存，把原因说清楚。"""
        raw = (await req.json()).get("rules")
        if not isinstance(raw, list) or len(raw) > 200:
            return web.json_response({"error": "指令表要是一个列表（最多 200 条）"}, status=400)
        try:
            rules = [customcmd.clean(r, i) for i, r in enumerate(raw) if isinstance(r, dict)]
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        seen = set()
        for r in rules:  # id 不能重
            while r["id"] in seen:
                r["id"] += "x"
            seen.add(r["id"])
        self.db.set_kv("custom_commands", rules)
        return web.json_response({"rules": rules})

    async def commands_test(self, req):
        """试一试：一句话会触发哪几条（只看触发方式，不执行、不看权限和范围）。"""
        b = await req.json()
        text, at = str(b.get("text", "")), bool(b.get("at", True))
        hits = [r["name"] for r in customcmd.rules(self.db) if r.get("on", True) and bool(r.get("at")) == at and customcmd.matches(r, text)]
        return web.json_response({"first": hits[0] if hits else "", "all": hits})

    # ---------- 群聊分寸与工具 ----------
    async def tools_info(self, req):
        """联网搜索、工具调用、表情包、用量硬上限的当前状态（设置页和首页用）。"""
        e = self.eng
        ts = self.core.llm.tools_state()
        ok, why = e.search.available()
        st = e.stickers
        return web.json_response({
            "tools": ts, "search": dict(e.search.status(), available=ok, why=why),
            "stickers": {"count": self.db.x("SELECT COUNT(*) FROM stickers WHERE status='learned'").fetchone()[0], "bytes": st.total_bytes(),
                         "fav": self.db.x("SELECT COUNT(*) FROM stickers WHERE status='learned' AND source='fav'").fetchone()[0]},
            "hard_cap": self.core.llm.hard_cap_state(),
        })

    async def search_put(self, req):
        b = await req.json()
        if b.get("provider") not in (None, "auto", *search_mod.PROVIDERS):
            return web.json_response({"error": "没有这个搜索服务"}, status=400)
        self.eng.search.set_conf(provider=b.get("provider"), searxng_url=b.get("searxng_url"))
        return web.json_response(self.eng.search.status())

    async def prefilter_get(self, req):
        return web.json_response({"rules": prefilter.rules(self.db), "defaults": prefilter.DEFAULT_RULES,
                                  "saved": self.db.x("SELECT COALESCE(SUM(n),0) FROM counters WHERE name='gk_saved'").fetchone()[0]})

    async def prefilter_put(self, req):
        import re
        rules = []
        for r in (await req.json()).get("rules") or []:
            kind = "builtin" if r.get("kind") == "builtin" else "regex"
            if kind == "regex":
                try:
                    re.compile(r.get("pattern", ""))
                except re.error as e:
                    return web.json_response({"error": f"规则「{r.get('name', '')}」的正则不对：{e}"}, status=400)
            rules.append({"id": r.get("id") or f"r{len(rules)}", "name": (r.get("name") or "")[:60], "kind": kind,
                          "pattern": r.get("pattern", ""), "on": bool(r.get("on", True))})
        self.db.set_kv("prefilter_rules", rules)
        return web.json_response({"rules": rules})

    async def reminders_list(self, req):
        rows = self.eng.reminders.listing(req.query.get("status", "pending"))
        names = {r.key: r.name for r in self.eng.rooms.values()}
        for r in rows:
            r["room_name"] = names.get(f"{r['platform']}:{r['chat_id']}", "") if r["chat_type"] == "group" else "私聊"
        return web.json_response(rows)

    async def reminder_delete(self, req):
        self.eng.reminders.delete(int(req.match_info["id"]))
        return web.json_response({"ok": True})

    async def styles_list(self, req):
        rows = self.db.rows("SELECT id, subject AS room, content, ts FROM memories WHERE kind='style' ORDER BY ts DESC")
        names = {r.key: r.name for r in self.eng.rooms.values()}
        need = int(self.core.settings.g("style_min_msgs"))
        out = [dict(r, room_name=names.get(r["room"], r["room"])) for r in rows]
        have = {r["room"] for r in out}
        for key, name in names.items():  # 还没生成的群：显示攒了多少条
            if key not in have:
                out.append({"id": None, "room": key, "room_name": name, "content": "", "ts": 0, "have": self.db.kv(f"style_n:{key}", 0), "need": need})
        return web.json_response(out)

    async def style_put(self, req):
        b = await req.json()
        room = b.get("room", "")
        if ":" not in room:
            return web.json_response({"error": "缺少群"}, status=400)
        self.eng.memory.set_style(room, b.get("text", ""))
        if not (b.get("text") or "").strip():
            self.db.set_kv(f"style_n:{room}", 0)
        return web.json_response({"ok": True})

    async def stickers_list(self, req):
        rows = self.eng.stickers.listing()
        names = {r.key: r.name for r in self.eng.rooms.values()}
        for r in rows:
            r["room_name"] = names.get(r["room"], r["room"])
        return web.json_response(rows)

    async def stickers_fav(self, req):
        """每个 QQ / 微信账号现有几张收藏的表情、上次同步的结果。"""
        return web.json_response(self.eng.stickers.fav_status())

    async def stickers_sync(self, req):
        """现在同步收藏：所有在线的 QQ、微信账号各同步一次（不看「用账号收藏的表情」开关），返回每个账号的结果。"""
        res = {}
        for aid, b in list(self.eng.bots.items()):
            if b.platform in FAV_PLATFORMS:
                res[aid] = await self.eng.stickers.sync_favorites(b, force=True)
        return web.json_response({"results": res, "accounts": self.eng.stickers.fav_status()})

    async def stickers_redescribe(self, req):
        """重新描述全部表情包：不看 desc_max，全部按现在的字数上限重来；在后台跑，马上返回要重描的张数（已经有一轮在跑就返回 running）。"""
        st = self.eng.stickers
        if "redesc" in st.busy:
            return web.json_response({"running": True, "total": 0})
        self.eng.spawn(st.redescribe(force=True))
        return web.json_response({"running": False, "total": len(st.redesc_todo(force=True))})

    async def sticker_delete(self, req):
        self.eng.stickers.delete(int(req.match_info["id"]))
        return web.json_response({"ok": True})

    async def sticker_image(self, req):
        rows = self.db.rows("SELECT file FROM stickers WHERE id=? AND status='learned'", (int(req.match_info["id"]),))
        data = self.eng.stickers.load(rows[0]) if rows else None
        if not data:
            raise web.HTTPNotFound()
        from .stickers import ext_of
        return web.Response(body=data, content_type={"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext_of(data), "image/jpeg"))

    async def tune_list(self, req):
        """每个机器人在每个群里此刻的插话力度和最近的调整原因。"""
        out = []
        for room in self.eng.rooms.values():
            for b in self.eng.room_bots(room):
                out.append(dict(self.eng.tune_info(b, room), room=room.key, room_name=room.name, account=b.aid, role=b.role["name"],
                                paused=max(0, int(room.pause_until - time.time()))))
        return web.json_response(out)

    # ---------- 用量扩展 ----------
    def usage_summary(self, days: int) -> dict:
        """缓存命中率、平均每条回复的 token、趋势。思考 token 和缓存写入只在接口返回时才有数。"""
        since = since_day(days)
        t = self.db.rows("""SELECT COALESCE(SUM(calls),0) calls, COALESCE(SUM(prompt),0) prompt, COALESCE(SUM(cached),0) cached,
                            COALESCE(SUM(completion),0) completion, COALESCE(SUM(reasoning),0) reasoning,
                            COALESCE(SUM(cache_write),0) cache_write FROM usage WHERE day>=?""", (since,))[0]
        m = self.db.rows("""SELECT COALESCE(SUM(calls),0) calls, COALESCE(SUM(prompt),0) prompt, COALESCE(SUM(completion),0) completion
                            FROM usage WHERE day>=? AND purpose IN ('main','trial')""", (since,))[0]
        trend = self.db.rows("""SELECT day, SUM(calls) calls, SUM(prompt) prompt, SUM(cached) cached, SUM(completion) completion,
                                SUM(reasoning) reasoning, SUM(cache_write) cache_write FROM usage WHERE day>=?
                                GROUP BY day ORDER BY day""", (since,))
        return {
            "total": t,
            "hit_rate": round(t["cached"] / t["prompt"], 4) if t["prompt"] else None,
            "avg_reply": {"calls": m["calls"], "prompt": round(m["prompt"] / m["calls"]) if m["calls"] else 0,
                          "completion": round(m["completion"] / m["calls"]) if m["calls"] else 0},
            "has_reasoning": t["reasoning"] > 0, "has_cache_write": t["cache_write"] > 0, "trend": trend,
            "gk_saved": self.db.x("SELECT COALESCE(SUM(n),0) FROM counters WHERE name='gk_saved' AND day>=?", (since,)).fetchone()[0],
            "gk_calls": self.db.x("SELECT COALESCE(SUM(calls),0) FROM usage WHERE purpose='gk' AND day>=?", (since,)).fetchone()[0],
            "searches": self.db.x("SELECT COALESCE(SUM(calls),0) FROM usage WHERE purpose='search' AND prompt=0 AND completion=0 AND day>=?", (since,)).fetchone()[0],
            "hard_cap": self.core.llm.hard_cap_state(),
        }

    async def usage_summary_api(self, req):
        return web.json_response(self.usage_summary(int(req.query.get("days", 30))))

    async def usage_csv(self, req):
        """导出 CSV（UTF-8 带 BOM，Excel 直接打开）。不换算成钱，只导 token 和次数。"""
        by, days = req.query.get("by", "day"), int(req.query.get("days", 30))
        if by not in self.s.USAGE_COLS:
            raise web.HTTPBadRequest(text="by 不对")
        names = {"day": "日期", "role": "角色", "room": "群", "model": "模型", "purpose": "用途", "platform": "平台"}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([names[by], "调用次数", "输入tokens", "缓存命中tokens", "缓存命中率", "缓存写入tokens", "输出tokens", "思考tokens",
                    "平均每次输入", "平均每次输出"])
        for r in self.s.usage_rows(by, days):
            n = r["calls"] or 1
            w.writerow([r["k"], r["calls"], r["prompt"], r["cached"], f"{r['cached'] / r['prompt']:.1%}" if r["prompt"] else "",
                        r["cache_write"] or "", r["completion"], r["reasoning"] or "", round(r["prompt"] / n), round(r["completion"] / n)])
        body = "\ufeff" + buf.getvalue()
        return web.Response(body=body.encode("utf-8"), content_type="text/csv", charset="utf-8",
                            headers={"Content-Disposition": f"attachment; filename=usage-{by}-{time.strftime('%Y%m%d')}.csv"})

    # ---------- 平台引导与授权 ----------
    async def platform_specs(self, req):
        from .platforms import PLATFORMS
        return web.json_response(PLATFORMS)

    def _channel(self, aid: str):
        b = self.eng.bots.get(aid)
        if not b or not b.channel:
            raise web.HTTPBadRequest(text="先启动这个账号")
        return b.channel

    async def wechat_confirm(self, req):
        """用户在面板里回答「检测到的这个微信是机器人小号吗」。"""
        ch = self._channel(req.match_info["id"])
        if getattr(ch, "platform", "") != "wechat":
            raise web.HTTPBadRequest(text="不是微信账号")
        body = await req.json()
        ok, again = bool(body.get("ok")), bool(body.get("again"))
        try:
            r = await ch.confirm_bind(ok, again)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(dict(r, account=self.s.account_view(self.db.doc("account", req.match_info["id"]))))

    async def wechat_rebind(self, req):
        """清掉绑定并重启这个账号：重启后会按当前登录的微信重新请用户确认。"""
        aid = req.match_info["id"]
        acc = self.db.doc("account", aid)
        if not acc or acc.get("platform") != "wechat":
            raise web.HTTPBadRequest(text="不是微信账号")
        self.db.del_kv(f"wxbind:{aid}")
        self.eng.log("info", "system", f"微信账号 {aid} 的绑定已清除，重新启动后会重新确认身份")
        await self.eng.start_account(acc)
        self.core.running = True
        return web.json_response(self.s.account_view(acc))

    async def line_register(self, req):
        ch = self._channel(req.match_info["id"])
        try:
            return web.json_response(await ch.register_webhook())
        except Exception as e:
            return web.json_response({"error": f"登记失败：{e}"}, status=400)

    async def youtube_oauth(self, req):
        """桌面应用 + 本机回环地址 + PKCE：返回要在浏览器里打开的授权地址，授权结果由本机的临时小服务接收。"""
        import base64
        import hashlib
        import secrets as sec
        from urllib.parse import urlencode
        aid = req.match_info["id"]
        acc = self.db.doc("account", aid)
        secret = self.db.secret(f"acct:{aid}:client_secret")
        if not (acc and acc.get("client_id") and secret):
            return web.json_response({"error": "先填好 client id 和 client secret"}, status=400)
        verifier = sec.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = sec.token_urlsafe(16)
        done = asyncio.Event()
        app = web.Application()

        async def callback(r: web.Request):
            if r.query.get("state") != state or not r.query.get("code"):
                return web.Response(status=400, text="授权没有完成（state 不对或被拒绝）")
            import aiohttp
            data = {"code": r.query["code"], "client_id": acc["client_id"], "client_secret": secret, "grant_type": "authorization_code",
                    "redirect_uri": redirect, "code_verifier": verifier}
            async with tls.session() as s:
                async with s.post(acc.get("token_url") or "https://oauth2.googleapis.com/token", data=data,
                                  proxy=(self.core.settings.g("proxy") or None) if acc.get("use_proxy", True) else None) as resp:
                    j = await resp.json(content_type=None)
            if not j.get("refresh_token"):
                return web.Response(status=400, text=f"换取令牌失败：{j.get('error_description') or j.get('error') or j}")
            self.db.set_secret(f"acct:{aid}:refresh_token", j["refresh_token"])
            self.eng.emit("account", {"id": aid, "status": "authorized", "detail": "YouTube 已授权"})
            done.set()
            return web.Response(content_type="text/html", charset="utf-8",
                                text="<meta charset=utf-8><h2>授权成功</h2><p>可以关掉这个页面，回到面板启动账号。</p>")
        app.router.add_get("/callback", callback)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        redirect = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/callback"

        async def reap():
            try:
                await asyncio.wait_for(done.wait(), 300)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            await runner.cleanup()
        self.eng.spawn(reap())
        q = urlencode({"client_id": acc["client_id"], "redirect_uri": redirect, "response_type": "code",
                       "scope": "https://www.googleapis.com/auth/youtube.force-ssl", "code_challenge": challenge,
                       "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent", "state": state})
        base = acc.get("auth_url") or "https://accounts.google.com/o/oauth2/v2/auth"
        return web.json_response({"url": f"{base}?{q}", "redirect": redirect})

    async def bilibili_qr_start(self, req):
        import aiohttp
        from .channels import bilibili
        acc = self.db.doc("account", req.match_info["id"]) or {}
        async with tls.session() as s:
            try:
                q = await bilibili.qr_generate(s, acc.get("passport_base") or bilibili.PASSPORT)
            except Exception as e:
                return web.json_response({"error": f"取不到二维码：{e}"}, status=400)
        return web.json_response(q)

    async def bilibili_qr_poll(self, req):
        import aiohttp
        from .channels import bilibili
        aid = req.match_info["id"]
        acc = self.db.doc("account", aid) or {}
        async with tls.session() as s:
            r = await bilibili.qr_poll(s, req.query.get("key", ""), acc.get("passport_base") or bilibili.PASSPORT)
        if r["state"] == "ok":
            ck = r["cookies"]
            if not (ck.get("SESSDATA") and ck.get("bili_jct")):
                return web.json_response({"state": "error", "error": "扫码成功但没拿到登录信息，改用手动粘贴"})
            for k, v in ck.items():
                if k in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3"):
                    self.db.set_secret(f"acct:{aid}:{k}", v)
            return web.json_response({"state": "ok"})
        return web.json_response({"state": r["state"]})

    # ---------- 关于 ----------
    async def about(self, req):
        def text(name):
            try:
                return (RES / name).read_text(encoding="utf-8")
            except OSError:
                return ""
        return web.json_response({"version": VERSION, "author": AUTHOR, "author_home": AUTHOR_HOME, "repo": REPO, "repo_url": REPO_URL,
                                  "license": LICENSE_NAME, "license_text": text("LICENSE"), "third_party": text("THIRD_PARTY.md")})

    # ---------- 新版本提醒 ----------
    async def update_info(self, req):
        from . import update
        repo = self.core.settings.g("update_repo") or REPO
        info = self.db.kv("update_info") or {}
        if req.query.get("refresh") or (not info and repo):
            try:
                info = await update.check(repo, self.core.settings.g("proxy") or None)
                self.db.set_kv("update_info", info)
            except Exception as e:  # 没发布过版本、没网：不当成出错弹窗，只告诉页面「没查到」
                return web.json_response({"error": str(e), "current": VERSION, "repo": repo, "newer": False})
        return web.json_response(dict(info, repo=repo, current=VERSION, dismissed=self.db.kv("update_dismissed") == info.get("latest")))

    async def update_dismiss(self, req):
        info = self.db.kv("update_info") or {}
        self.db.set_kv("update_dismissed", info.get("latest"))
        return web.json_response({"ok": True})

    # ---------- 诊断包 ----------
    async def diagnostics(self, req):
        from . import diagnostics
        data = await asyncio.get_running_loop().run_in_executor(None, diagnostics.build, self.core)
        return web.Response(body=data, content_type="application/zip",
                            headers={"Content-Disposition": f"attachment; filename=diagnostics-{time.strftime('%Y%m%d-%H%M')}.zip"})

    # ---------- 体验模式 ----------
    async def demo_state(self, req):
        return web.json_response(self.core.demo.state())

    async def demo_start(self, req):
        b = await req.json()
        try:
            await self.core.demo.start(b.get("role_id") or "", bool(b.get("chatter", True)))
        except KeyError:
            return web.json_response({"error": "没有这个角色，先到「角色」页建一个"}, status=400)
        return web.json_response(self.core.demo.state())

    async def demo_stop(self, req):
        await self.core.demo.stop()
        return web.json_response(self.core.demo.state())

    async def demo_say(self, req):
        b = await req.json()
        text = (b.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest()
        try:
            await self.core.demo.say("你", text, at=bool(b.get("at")))
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def demo_chatter(self, req):
        self.core.demo.chatter(bool((await req.json()).get("on")))
        return web.json_response(self.core.demo.state())
