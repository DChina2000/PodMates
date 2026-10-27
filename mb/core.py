"""把数据库、设置、模型、引擎装到一起；首次运行的默认内容、旧程序导入、备份、导出导入。"""
import asyncio
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import APP_NAME, REPO, VERSION, files, personas, templates
from .db import DB, new_id
from .demo import Demo
from .engine import Engine
from .reset import Reset
from .llm import DEFAULT_ENDPOINTS, LLM
from .monitor import Monitor
from .paths import ASSETS
from .settings import BY_KEY, Settings

DB_NAME = "podmates.db"
LEGACY_DB_NAME = "multibot.db"  # 改名前的数据库文件名：数据目录里只有旧名时，打开前改成新名（备份包里的旧名也认）
SECRETS_NOTE = "密钥按导出这台电脑的 Windows 用户加密。换电脑（或换 Windows 用户）后读不出来，需要在面板里重新填一遍。"
SHELLS = {"海豚娘": "persona_haitun.md", "狐狸娘": "persona_huliniang.md"}  # 旧程序的外壳文件名（只在导入旧程序时读）


class Core:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        if not (self.data / DB_NAME).exists() and (self.data / LEGACY_DB_NAME).exists():
            for suffix in ("", "-wal", "-shm"):
                old = self.data / f"{LEGACY_DB_NAME}{suffix}"
                if old.exists():
                    old.replace(self.data / f"{DB_NAME}{suffix}")
        self.subscribers: set[asyncio.Queue] = set()
        self.running = False
        self._pre_migrated = False  # 这次启动已经在迁移前备份过：后面的迁移不再重复备一份
        self.open()

    def open(self):
        self.db = DB(self.data / DB_NAME)
        self.settings = Settings(self.db)
        self.llm = LLM(self.db, self.settings, log=lambda *a, **k: self.engine.log(*a, **k))
        self.engine = Engine(self.db, self.settings, self.llm, emit=self.emit)
        self.engine.update_hook = self.check_update
        self.demo = Demo(self)
        self.reset = Reset(self)
        self.engine.reset = self.reset
        self.monitor = Monitor(self)
        if not self.db.kv("seeded"):
            self.seed()
            self.engine.reload()
        self.ensure_user_card()
        self.tag_persona_sources()
        self._pre_migrated = self._migrations_pending()
        if self._pre_migrated:  # 这一次启动有迁移要跑：在第一项写库之前先做一份整库备份（只做一次）
            self.backup("pre-migrate")
        self.isolate_memory()
        self.migrate_share_mode()
        self.migrate_relation_schemes()
        self.migrate_memory_v2()

    def _migrations_pending(self) -> bool:
        """这次启动有没有迁移真的要改东西（已经跑过的、新库里没有旧数据可迁的都不算）：用来决定要不要先备份。
        只读判断，比「标记没写过」更严：新装第一次启动也有几项标记还没写，但库里没有可迁的东西，不做空备份。"""
        from .people import People, has_att
        db = self.db
        if not db.kv("mem_isolated_v1") and (
                db.x("SELECT 1 FROM memories WHERE space LIKE 'iso:%' OR space LIKE 'role:%' LIMIT 1").fetchone()
                or db.x("SELECT 1 FROM settings WHERE key='memory_isolated' LIMIT 1").fetchone()
                or db.x("SELECT 1 FROM memories WHERE kind='style' GROUP BY space, subject HAVING COUNT(*)>1 LIMIT 1").fetchone()):
            return True
        if not db.kv("share_mode_v1") and any("share_max" in d or "share_window" in d for d in self.settings.data.values() if "share_mode" not in d):
            return True
        if not db.kv("schemes_v1"):
            if any((r.get("attitudes") or {}) for r in db.docs("role")):
                return True
            for p in db.docs("person"):
                base = People.base_scheme(p)
                if not p.get("schemes") and (base["identity"] or base["calls"] or has_att(base["attitude"])):
                    return True
            for r in db.docs("role"):  # 老角色缺来源模板的标签
                tid = r.get("template") or (db.doc("persona", r.get("persona") or "") or {}).get("template")
                if tid and not r.get("tags"):
                    return True
        if not db.kv("memory_v2") and db.x("SELECT 1 FROM memories WHERE kind='recap' AND meta IS NULL LIMIT 1").fetchone():
            return True
        return False

    def migrate_memory_v2(self) -> dict | None:
        """记忆分层：老前情没有覆盖范围，能从库里推出来的补上（推不出的照原样用）。只增不改，不调模型；有东西要补时先做一份整库备份。只跑一次。"""
        if self.db.kv("memory_v2"):
            return None
        todo = self.db.x("SELECT COUNT(*) FROM memories WHERE kind='recap' AND meta IS NULL").fetchone()[0]
        if todo and not self._pre_migrated:
            self.backup("pre-memory-v2")
        done = self.engine.memory.migrate_meta() if todo else {"l1": 0, "upper": 0}
        self.db.set_kv("memory_v2", {**done, "ts": time.time()})
        return done

    def migrate_relation_schemes(self) -> dict | None:
        """关系方案：人物卡现有的身份、称呼、态度原样成为一套「默认」方案（全体角色）；角色对个人的态度覆盖并入成「指定角色」方案
        （身份、称呼照抄默认那套，只态度不同，效果和以前一样），然后清掉角色上的覆盖，从此只有人物卡上这一处决定态度。
        角色补上标签：认得出来源模板的，标签就是模板的类别。只跑一次。"""
        db = self.db
        if db.kv("schemes_v1"):
            return None
        from .people import People, has_att
        persons = {p["id"]: p for p in db.docs("person")}
        added = merged = tagged = 0
        for pid, p in persons.items():
            base = People.base_scheme(p)
            if not p.get("schemes") and (base["identity"] or base["calls"] or has_att(base["attitude"])):
                p["schemes"] = [{"id": new_id("sc_"), "name": "默认", "identity": base["identity"], "calls": base["calls"],
                                 "attitude": dict(base["attitude"] or {"presets": [], "custom": ""}), "scope": {"mode": "all", "tags": [], "roles": []}}]
                added += 1
        for r in db.docs("role"):
            for pid, att in (r.get("attitudes") or {}).items():
                p = persons.get(pid)
                if not p or not has_att(att):
                    continue
                base = People.base_scheme(p)
                p.setdefault("schemes", []).append({"id": new_id("sc_"), "name": f"{r.get('name') or r['id']}专用", "identity": base["identity"], "calls": base["calls"],
                                                    "attitude": dict(att), "scope": {"mode": "roles", "tags": [], "roles": [r["id"]]}})
                merged += 1
            personas_doc = db.doc("persona", r.get("persona") or "") or {}
            tid = r.get("template") or personas_doc.get("template")
            cat = next((t.get("category") for t in templates.templates() if t["id"] == tid), None) if tid else None
            tags = list(r.get("tags") or ([cat] if cat else []))
            if cat and cat not in tags:
                tags.append(cat)
            if tags != (r.get("tags") or []) or r.get("attitudes"):
                tagged += bool(tags != (r.get("tags") or []))
                db.put("role", {**r, "tags": tags, "attitudes": {}})
        for p in persons.values():
            if p.get("schemes"):
                db.put("person", p)
        self.engine.reload()
        db.set_kv("schemes_v1", {"added": added, "merged": merged, "tagged": tagged, "ts": time.time()})
        return {"added": added, "merged": merged, "tagged": tagged}

    def migrate_share_mode(self):
        """发言占比以前只有「按百分比」一种。升级时：设过占比百分比或统计窗口的那一层，改成明确的「按比例」，百分比沿用原来的值
        （没设过百分比就写上原来的默认 40），并保持原来「样本不满 10 条不拦」的规则，这样效果不变。没设过的人用新默认（15 条里最多 7 条）。只跑一次。"""
        if self.db.kv("share_mode_v1"):
            return
        s = self.settings
        n = 0
        for scope, d in list(s.data.items()):
            if ("share_max" in d or "share_window" in d) and "share_mode" not in d:
                s.set(scope, "share_mode", "percent")
                s.set(scope, "share_min_on", True)
                if "share_max" not in d:
                    s.set(scope, "share_max", 40)
                n += 1
        self.db.set_kv("share_mode_v1", {"scopes": n, "ts": time.time()})

    def isolate_memory(self) -> dict | None:
        """旧版的人物备忘按角色共用（空间 role:<角色>），所有群共用一份，私聊里得来的也在里面；现在每个群、每个私聊各记各的。
        按每条记录的 room 拆回去：群里的 → 那个群的人物备忘空间；私聊来的（room 为空）只在能确定是哪个账号 × 哪个人时才认领，
        认不出的不删，放进 legacy:<角色> 空间（记忆页里看得到），由用户决定去处。以前设成「隔离」的群，前情 / 群梗 / 画像 / 表情包并回 chat:<群>。
        只跑一次；返回做了什么（没跑返回 None）。"""
        db = self.db
        if db.kv("mem_isolated_v1"):
            return None
        mem = self.engine.memory
        off = set(db.kv("semantic_off") or [])
        merged = 0
        for sp in [r["space"] for r in db.rows("SELECT DISTINCT space FROM memories WHERE space LIKE 'iso:%'")]:
            m = re.fullmatch(r"iso:(.+):(recap|meme|doc|style|sticker)", sp)
            if m:
                new = "chat:" + m.group(1)
                merged += db.x("UPDATE memories SET space=? WHERE space=?", (new, sp)).rowcount
                db.x("UPDATE stickers SET space=? WHERE space=?", (new, sp))
                if sp in off:
                    off.add(new)
        for r in db.rows("SELECT subject, space, COUNT(*) n, MAX(ts) t FROM memories WHERE kind='style' GROUP BY space, subject HAVING n > 1"):
            db.x("DELETE FROM memories WHERE kind='style' AND space=? AND subject=? AND ts<?", (r["space"], r["subject"], r["t"]))
        accounts, people = db.docs("account"), db.docs("person")
        split = legacy = 0
        counts: dict[str, int] = {}
        for r in db.rows("SELECT id, space, subject, room FROM memories WHERE space LIKE 'role:%'"):
            role, room, platform, dm = r["space"][5:], r["room"], None, None
            if room and room.startswith("dm:"):
                dm = room
                platform = next((a["platform"] for a in accounts if a["id"] == room.split(":")[1]), None)
            elif room:
                platform = room.split(":")[0]
            else:
                dm, platform = self._private_peer(r["subject"], role, accounts, people)
            if room or dm:
                new = mem.space("person", role, platform, None if dm else room, dm)
                db.x("UPDATE memories SET space=?, room=COALESCE(room, ?) WHERE id=?", (new, dm, r["id"]))
                split += 1
            else:
                new = f"legacy:{role}"
                db.x("UPDATE memories SET space=? WHERE id=?", (new, r["id"]))
                legacy += 1
            counts[new] = counts.get(new, 0) + 1
            if r["space"] in off:
                off.add(new)
        db.set_kv("semantic_off", sorted(off))
        db.x("DELETE FROM settings WHERE key='memory_isolated'")  # 已经默认隔离，这一项没有了
        for d in self.settings.data.values():
            d.pop("memory_isolated", None)
        db.set_kv("mem_isolated_v1", {"split": split, "legacy": legacy, "merged_iso": merged, "ts": time.time()})
        if split or legacy or merged:
            self.engine.log("info", "memory", f"记忆默认按群、按私聊隔离：把共用的人物备忘拆回各自的群 / 私聊 {split} 条，认不出来源的 {legacy} 条放在待认领里，隔离群的前情等并回 {merged} 条")
        return {"split": split, "legacy": legacy, "merged_iso": merged}

    @staticmethod
    def _private_peer(subject: str, role: str, accounts: list[dict], people: list[dict]) -> tuple[str | None, str | None]:
        """私聊里「记住」存下的（room 为空）：说话的人就是备忘的主体。只有恰好认出一个（这个角色的账号 × 这个人）才认领，否则 (None, None)。"""
        ids: dict[str, list] = {}
        if subject.startswith("uid:"):
            _, plat, uid = subject.split(":", 2)
            ids = {plat: [uid]}
        elif subject.startswith("person:"):
            p = next((x for x in people if x["id"] == subject[7:]), None)
            if p:
                ids = {"qq": p.get("qq") or [], "discord": p.get("discord") or [], "wechat": (p.get("wechat_ids") or []) + (p.get("wechat_remark") or [])}
        cands = {(f"dm:{a['id']}:{u}", a["platform"]) for a in accounts if a.get("role_id") == role for u in ids.get(a["platform"], [])}
        return next(iter(cands)) if len(cands) == 1 else (None, None)

    def tag_persona_sources(self):
        """人设上记着它来自哪个模板（template），从模板再建角色时才知道可以直接用它。老数据里没记的补上：
        角色是从模板建的，它用的人设就是那个模板的；旧机器人迁移来的 p_海豚娘、p_狐狸娘 是 builtin haitun、fox。"""
        roles = self.db.docs("role")
        for p in self.db.docs("persona"):
            if p.get("template"):
                continue
            tid = next((r["template"] for r in roles if r.get("persona") == p["id"] and r.get("template")), None) or {"p_海豚娘": "haitun", "p_狐狸娘": "fox"}.get(p["id"])
            if tid and any(t["id"] == tid for t in templates.templates()):
                self.db.put("persona", {**p, "template": tid})

    def ensure_user_card(self):
        """「用户」是系统里固定的一张人物卡（打了「用户（面板使用者）」标记，管理员，态度预设「亲近贴心」），所有人装上都有。
        名字、身份、称呼、各平台账号都留空，由设置向导「用户卡」那一步填；每个使用者的特殊性只存在这张卡的数据里，代码不针对任何具体的人。"""
        if any(p.get("self") for p in self.db.docs("person")):
            return
        self.db.put("person", dict(id="pe_user", name="", identity="", call="", calls=[], qq=[], discord=[], wechat_ids=[], wechat_remark=[], nicks={},
                                   note="", knows="", attitude={"presets": ["亲近贴心"], "custom": ""}, admin=True, groups=[], **{"self": True}))
        self.engine.reload()

    def emit(self, kind: str, data):
        for q in list(self.subscribers):
            if q.qsize() < 1000:
                q.put_nowait({"type": kind, "data": data})

    # ---------- 首次运行 ----------
    def seed(self):
        """公开版的默认数据：不含任何个人信息。要恢复作者自己的设置，用「导入旧程序」。"""
        db = self.db
        for ep in DEFAULT_ENDPOINTS:
            if not db.doc("endpoint", ep["id"]):
                db.put("endpoint", dict(ep))
        core = (ASSETS / "seed" / "persona_core.md").read_text(encoding="utf-8")
        order = personas.seed_shared(db, core)
        db.set_kv("core_order", order)
        db.set_kv("platforms", db.kv("platforms") or {})
        db.set_kv("schemes_v1", {"added": 0, "merged": 0, "tagged": 0, "new_db": True, "ts": time.time()})  # 新库没有旧数据可迁，不跑关系方案迁移
        db.set_kv("seeded", time.time())

    def needs_setup(self) -> bool:
        return not self.db.kv("setup_done")

    def import_old(self, bot_dir: Path) -> list[str]:
        """一键导入旧程序 bot 文件夹的配置和人设（只读旧目录）：模型设置、人设各模块、她妈那张人物卡的账号。"""
        bot_dir = Path(bot_dir)
        done = []
        cfg_path = bot_dir / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        if cfg:
            key = cfg.get("glm_api_key", "")
            url = (cfg.get("glm_base_url") or "").rstrip("/")
            ep = self.db.doc("endpoint", "zai") or {"id": "zai", "name": "智谱 Z.ai（导入自旧程序）", "provider": "zai-coding-plan",
                                                    "protocol": "openai"}
            if url:
                ep["base_url"] = url
            self.db.put("endpoint", ep)
            if key and "在这里填入" not in key:
                self.db.set_secret("endpoint:zai", key)
                done.append("API Key")
            for purpose in ("main", "gk", "mem", "vis"):  # 导入就沿用作者现在的 GLM，不受新的默认模型影响
                self.settings.set("global", f"{purpose}_endpoint", "zai")
            self.settings.set("global", "vis_model", "glm-5.3-flash")
            self.settings.set("global", "mem_model", cfg.get("gatekeeper_model") or cfg.get("glm_model") or "glm-5.1")
            mapping = {"glm_model": "main_model", "reasoning_effort": "main_effort", "temperature": "main_temperature",
                       "gatekeeper_model": "gk_model", "gatekeeper_effort": "gk_effort", "auto_reply": "proactive",
                       "reply_cooldown_seconds": "cooldown_seconds", "hourly_proactive_limit": "hourly_limit",
                       "context_window": "context_window", "context_max_chars": "context_max_chars",
                       "max_history": "max_history", "reply_max_chars": "reply_max_chars"}
            old_defaults = {"context_max_chars": 500, "hourly_limit": 90}  # 旧程序的默认值：按新默认值处理，不当成作者改过的
            for old, new in mapping.items():
                if old in cfg and cfg[old] != BY_KEY[new]["default"] and cfg[old] != old_defaults.get(new):
                    self.settings.set("global", new, cfg[old])
            done.append("模型与行为设置")
        core_path = bot_dir / "persona_core.md"
        if core_path.exists():
            core = core_path.read_text(encoding="utf-8")
            shells = {n: personas.legacy_words((bot_dir / f).read_text(encoding="utf-8"))
                      for n, f in SHELLS.items() if (bot_dir / f).exists()}
            order = personas.seed_shared(self.db, personas.legacy_words(core))
            self.db.set_kv("core_order", order)
            for n, text in shells.items():
                personas.seed_persona(self.db, n, text, order)
            pf = cfg.get("persona_file", "")
            name = next((n for n, f in SHELLS.items() if f == pf), None) or next(iter(shells), "")
            if name and not self.db.doc("role", "r_haitun"):
                self.db.put("role", dict(id="r_haitun", name=name, persona=f"p_{name}", summary=templates.get("haitun")["summary"],
                                         attitudes={}, relations={}))
            elif name:
                r = self.db.doc("role", "r_haitun")
                r["persona"] = f"p_{name}"
                self.db.put("role", r)
            done.append("人设（外壳与内核模块）")
            card = self.user_card_from_old(core, [str(x) for x in cfg.get("haitun_qq_ids", [])])
            if card:
                mine = next((p for p in self.db.docs("person") if p.get("self")), None)
                if mine:  # 旧程序里的「她妈」填进用户卡（不另建一张）
                    card["id"] = mine["id"]
                self.db.put("person", card)
                done.append(f"人物卡「{card['name']}」（{len(card['qq'])} 个 QQ 号）")
        self.engine.reload()
        return done

    @staticmethod
    def user_card_from_old(core: str, qq_ids: list[str]) -> dict | None:
        """导入旧程序用：旧内核里的【她妈】一段 + 配置里的 QQ 号 → 用户卡的内容（只是读旧文件的格式，不是针对某个人）。"""
        m = re.search(r"【她妈】\n(.+?)(?:\n\n|\Z)", core, re.S)
        text = m.group(1) if m else ""
        if not qq_ids and not text:
            return None
        name = (re.search(r"「(.+?)」", text) or [None, "主人"])[1]
        ident = (re.search(r"」是(.+?)[，。]", text) or [None, "她妈"])[1]
        nick = (re.search(r"QQ昵称「(.+?)」", text) or [None, ""])[1]
        call = (re.search(r"对(.{1,4}?)说话", text) or [None, "妈"])[1]
        presets = ["亲近贴心"] if "亲近贴心" in text or not text else []
        return dict(id="pe_user", name=name, identity=ident, call=call, calls=[call] if call else [], qq=list(dict.fromkeys(qq_ids)), discord=[],
                    wechat_remark=[], nicks={"qq": nick} if nick else {}, note="", knows="",
                    attitude={"presets": presets, "custom": ""}, admin=True, groups=[], self=True)

    # ---------- 运行 ----------
    async def start(self):
        self.running = True
        await self.engine.start_all()

    async def stop(self):
        self.running = False
        await self.engine.stop_all()

    async def periodic(self):
        last_day = ""
        while True:
            try:
                await self.engine.tick()
                await self.monitor.step()  # 资源监控：到点才真查（内存每分钟、硬盘和日志每小时）
                await self.engine.watch.step()  # 新模型：到点才查（多久一次见设置），到期的提议提醒用户
                await self.engine.embedder.ensure()  # 语义回忆的向量过期 / 缺了就在后台补算（启动后第一轮就查一次）
                day = time.strftime("%Y-%m-%d")
                if day != last_day:
                    last_day = day
                    self.maintain()  # 数据库只在主循环里用（单连接）；备份几十 MB 以内很快
                    await self.clean_napcat()
                    await self.check_update()
                    await self.check_wxauto()
            except Exception as e:
                self.engine.log("error", "system", f"定时任务出错：{e}")
            await asyncio.sleep(30)

    async def check_wxauto(self):
        """每天查一次 PyPI 上 wxauto4 有没有新版：只提醒（面板总览 + 私聊通知，每个新版只通知一次），不自动升级。设置里可以关。"""
        from .channels import wechat
        if not self.settings.get("wechat_lib_check", platform="wechat"):
            return
        installed = wechat.installed_lib_version()
        if not installed:
            self.db.del_kv("wxlib_update")
            return
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, wechat.pypi_info)
        except Exception as e:
            self.engine.log("debug", "update", f"查 wxauto4 新版本没成功：{e}")
            return
        try:
            wv = wechat.check_version().get("version", "")
        except Exception:
            wv = ""
        notice = wechat.lib_update_notice(installed, info, wv)
        if not notice:
            self.db.del_kv("wxlib_update")
            return
        self.db.set_kv("wxlib_update", {**notice, "ts": time.time()})
        if self.db.kv("wxlib_notified") != notice["latest"]:
            self.db.set_kv("wxlib_notified", notice["latest"])
            self.engine.notify(notice["text"])

    async def check_update(self, force: bool = False) -> dict | None:
        """每天查一次有没有新版本：只提醒，不下载、不更新。force（私聊「查更新」）：不看「有新版本时提醒我」开没开，立刻查；返回查到的信息，没查到是 {"error": …}。"""
        from . import update
        repo = self.settings.g("update_repo") or REPO
        if not force and not self.settings.g("update_check"):
            return None
        try:
            info = await update.check(repo, self.settings.g("proxy") or None)
        except Exception as e:
            self.engine.log("debug", "update", f"查新版本没成功：{e}")
            return {"error": str(e)[:100]}
        self.db.set_kv("update_info", info)
        if info["newer"] and self.db.kv("update_notified") != info["latest"]:
            self.db.set_kv("update_notified", info["latest"])
            self.engine.notify(f"有新版本 {info['latest']}（当前 {info['current']}）：{info['url']}")
        return info

    def maintain(self):
        self.backup("auto")
        keep = self.settings.g("backup_keep_days")
        for f in list((self.data / "backups").glob("auto-*.db")) + list((self.data / "backups").glob("pre-reset-*.db")):
            if time.time() - f.stat().st_mtime > keep * 86400:
                f.unlink()
        self.reset.prune(keep)
        now = time.time()
        self.db.x("DELETE FROM logs WHERE ts<?", (now - self.settings.g("log_keep_days") * 86400,))
        keep_msgs = self.settings.g("message_keep_days")
        if keep_msgs:  # 0 = 永久
            self.db.x("DELETE FROM messages WHERE ts<? AND kind!='import'", (now - keep_msgs * 86400,))
            self.db.x("DELETE FROM vis_cache WHERE ts<?", (now - keep_msgs * 86400,))  # 看图缓存和聊天记录一起清
        self.engine.memory.prune()
        files.enforce_cache(self.settings.g("file_cache_mb"), self.settings.g("file_keep_days"))

    async def clean_napcat(self):
        days = self.settings.get("napcat_clean_days", platform="qq")
        last = self.db.kv("napcat_cleaned", 0)
        if not days or time.time() - last < days * 86400:
            return
        for b in self.engine.bots.values():
            if b.platform == "qq" and b.online:
                try:
                    await b.channel.clean_cache()
                    self.engine.log("info", "napcat", f"已清理 QQ {b.uid} 的缓存")
                except Exception as e:
                    self.engine.log("warn", "napcat", f"清理缓存失败：{e}")
        self.db.set_kv("napcat_cleaned", time.time())

    def backup(self, prefix: str = "manual") -> Path:
        d = self.data / "backups"
        d.mkdir(exist_ok=True)
        path = d / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.db"
        if prefix == "auto":
            path = d / f"auto-{time.strftime('%Y%m%d')}.db"
        self.db.backup_to(path)
        return path

    # ---------- 导出 / 导入（换电脑迁移）----------
    def export(self, path: Path, with_secrets: bool) -> Path:
        tmp = self.data / "_export"
        if tmp.exists() and tmp.parent == self.data:
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            self.db.backup_to(tmp / DB_NAME, strip_secrets=not with_secrets)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(tmp / DB_NAME, DB_NAME)
                for f in sorted((self.data / "backups").glob("*.db")):
                    if with_secrets:
                        z.write(f, f"backups/{f.name}")
                    else:
                        DB(f).backup_to(tmp / f.name, strip_secrets=True)
                        z.write(tmp / f.name, f"backups/{f.name}")
                for f in sorted(self.engine.stickers.folder().glob("*")):  # 表情包图片：和库里的表情记录一起带走
                    if f.is_file():
                        z.write(f, f"stickers/{f.name}")
                for d in sorted((self.data / "backups").glob("reset-*")):  # 可撤销的重置：撤销要看这里的 data.json 和图片
                    if d.is_dir():
                        for f in sorted(d.rglob("*")):
                            if f.is_file():
                                z.write(f, f"backups/{d.name}/{f.relative_to(d).as_posix()}")
                z.writestr("manifest.json", json.dumps({"app": APP_NAME, "version": VERSION, "ts": time.time(),
                                                        "with_secrets": with_secrets,
                                                        "secrets_note": SECRETS_NOTE if with_secrets else ""}, ensure_ascii=False))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return path

    async def import_zip(self, path: Path) -> dict:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            src = DB_NAME if DB_NAME in names else LEGACY_DB_NAME
            if src not in names or "manifest.json" not in names:
                raise ValueError("这不是本程序导出的数据包")
            manifest = json.loads(z.read("manifest.json"))
            was_running = self.running
            await self.stop()
            await self.llm.close()
            self.engine.close()
            self.backup("before-import")
            self.db.close()
            for suffix in ("", "-wal", "-shm"):
                (self.data / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
            z.extract(src, self.data)
            if src != DB_NAME:
                (self.data / src).replace(self.data / DB_NAME)
            for n in names:
                if n.startswith("backups/") and n.endswith(".db"):
                    z.extract(n, self.data)
                elif n.startswith("stickers/") or n.startswith("backups/reset-"):  # 表情图片、可撤销的重置：换电脑后照样能用
                    if not n.endswith("/"):
                        z.extract(n, self.data)
        self.open()
        if was_running:
            await self.start()
        return manifest
