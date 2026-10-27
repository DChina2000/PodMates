"""重置记忆：按范围（全部 / 某个记忆空间 / 某个群 / 某个人）和内容（人物备忘、群前情、群梗、文件片段、群风格画像、表情包、提醒、最近的聊天上下文）删。

删之前先备份所选范围的内容（一个文件夹：data.json + 表情包图片），另外照现有的备份体系做一份整库备份；
面板上可以「撤销刚才的重置」，在下一次重置之前一直有效。人物表、人设、设置、用量不在重置范围内，这里一行都不碰。
"""
import base64
import json
import shutil
import time
from pathlib import Path

CATS = {"person": "人物备忘", "recap": "群前情", "meme": "群梗（连同向量）", "doc": "文件片段", "style": "群风格画像", "sticker": "表情包",
        "reminder": "提醒", "context": "最近的聊天上下文"}
MEM_KINDS = ("person", "recap", "meme", "doc", "style", "sticker")
SCOPES = ("all", "space", "room", "person", "text", "subject")  # text：群里「忘掉××」——包含这段文字的记忆（只在给定的记忆空间里）


class Reset:
    def __init__(self, core):
        self.core = core

    @property
    def db(self):
        return self.core.db

    @property
    def eng(self):
        return self.core.engine

    # ---------- 选出要删的 ----------
    def person_keys(self, pid: str) -> tuple[list[str], list[tuple[str, str]]]:
        """这个人的记忆主体（人物 id、各平台账号）和账号（平台, 账号）。"""
        p = self.eng.people.persons.get(pid)
        if not p:
            return [], []
        accts = []
        for plat in ("qq", "discord", "wechat"):
            accts += [(plat, u) for u in self.eng.people.uids(p, plat)]
        for plat, us in (p.get("ids") or {}).items():
            accts += [(plat, str(u)) for u in us or [] if str(u).strip()]
        return [f"person:{pid}"] + [f"uid:{pl}:{u}" for pl, u in accts], accts

    def select(self, scope: dict, kinds: list[str]) -> dict:
        """返回 {类别: 要删的东西}：记忆类别 → 记忆行；sticker → 记忆行 + 表情包行；reminder → 提醒行；context → {群: 条数}。"""
        typ, sid = scope.get("type", "all"), str(scope.get("id") or "")
        if typ not in SCOPES or (typ != "all" and not sid):
            raise ValueError("要选一个范围")
        out: dict = {}
        subjects, accts = self.person_keys(sid) if typ == "person" else ([], [])
        names: list[str] = []
        if typ == "person":
            p = self.eng.people.persons.get(sid) or {}
            names = [p.get("name") or ""] + self.eng.people.calls_of(p)
        elif typ == "subject":  # 群里「忘掉我」：这个人的备忘和印象，加上前情里含他的单位（连同覆盖它们的上层）
            subjects = list(scope.get("subjects") or [sid])
            accts = [tuple(x) for x in scope.get("accts") or []]
            names = list(scope.get("names") or [])
        by_plat: dict[str, set] = {}
        for plat, u in accts:
            by_plat.setdefault(plat, set()).add(str(u))
        for kind in MEM_KINDS:
            if kind not in kinds:
                continue
            sql, args = ("SELECT * FROM memories WHERE kind IN ('person','impression')", []) if kind == "person" else ("SELECT * FROM memories WHERE kind=?", [kind])
            if typ == "space":
                sql, args = sql + " AND space=?", args + [sid]
            elif typ == "room":
                sql, args = sql + " AND room=?", args + [sid]
            elif typ == "text":
                sp = scope.get("spaces") or [""]
                sql, args = sql + f" AND space IN ({','.join('?' * len(sp))}) AND (subject_name=? OR subject=? OR content LIKE ?)", args + list(sp) + [sid, sid, f"%{sid}%"]
            elif typ in ("person", "subject"):
                if kind == "recap":
                    out[kind] = {"mem": [r for r in self.db.rows(sql, args) if self.eng.memory.unit_matches_person(r, by_plat, names)]}
                    continue
                if kind != "person" or not subjects:
                    out[kind] = {"mem": []}
                    continue
                sql, args = sql + f" AND subject IN ({','.join('?' * len(subjects))})", args + subjects
            out[kind] = {"mem": self.db.rows(sql, args)}
        if out.get("recap") and out["recap"]["mem"]:  # 覆盖这些单位的上层一起删（删掉的上层之后由剩下的下层重新并）
            have = {r["id"] for r in out["recap"]["mem"]}
            extra = sorted(self.eng.memory.ancestors(have) - have)
            if extra:
                out["recap"]["mem"] += self.db.rows(f"SELECT * FROM memories WHERE id IN ({','.join(map(str, extra))})")
        if "sticker" in kinds:
            sql, args = "SELECT * FROM stickers WHERE 1=1", []
            if typ == "space":
                sql, args = sql + " AND space=?", [sid]
            elif typ == "room":
                sql, args = sql + " AND room=?", [sid]
            elif typ == "person":
                sql = "SELECT * FROM stickers WHERE 0"
            out.setdefault("sticker", {"mem": []})["stk"] = self.db.rows(sql, args)
        if "reminder" in kinds:
            sql, args = "SELECT * FROM reminders WHERE 1=1", []
            if typ == "space":
                sql = "SELECT * FROM reminders WHERE 0"
            elif typ == "room":
                plat, _, cid = sid.partition(":")
                sql, args = "SELECT * FROM reminders WHERE platform=? AND chat_type='group' AND chat_id=?", [plat, cid]
            elif typ == "person":
                rows = []
                for plat, u in accts:
                    rows += self.db.rows("SELECT * FROM reminders WHERE platform=? AND user_id=?", (plat, u))
                out["reminder"] = {"rows": rows}
            if typ != "person":
                out["reminder"] = {"rows": self.db.rows(sql, args)}
        if "context" in kinds:
            rooms, priv = self.context_keys(typ, sid, accts)
            ctx = {k: list(self.eng.rooms[k].ctx) for k in rooms if k in self.eng.rooms and self.eng.rooms[k].ctx}
            hist = {k[3:]: list(self.eng.private_hist[k[3:]]) for k in priv if self.eng.private_hist.get(k[3:])}
            out["context"] = {"ctx": ctx, "priv": hist, "floor": sorted(set(rooms) | set(priv))}
        return out

    def context_keys(self, typ: str, sid: str, accts: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """「最近聊天上下文」要覆盖的群和私聊：库里的会话和内存里的都算，没加载进内存的也算。
        按数据库里的会话写 ctx_floor，重启后第一次收到消息时也读不回重置前的历史。"""
        rooms: set[str] = set()
        priv: set[str] = set()
        if typ in ("all", "room"):
            rooms |= {r["id"] for r in self.db.docs("chat")}  # docs 表里的群会话
            rooms |= {r["room"] for r in self.db.rows("SELECT DISTINCT room FROM messages WHERE room NOT LIKE 'dm:%'")}
            rooms |= {k for k in self.eng.rooms if not k.startswith("dm:")}
            if typ == "room":
                rooms &= {sid}
        if typ in ("all", "person"):
            uids = {str(u) for _, u in accts}
            for r in self.db.rows("SELECT DISTINCT room FROM messages WHERE room LIKE 'dm:%'"):
                _, _, uid = r["room"][3:].partition(":")
                if typ == "all" or uid in uids:
                    priv.add(r["room"])
            for k in self.eng.private_hist:
                _, _, uid = k.partition(":")
                if typ == "all" or uid in uids:
                    priv.add(f"dm:{k}")
        return sorted(rooms), sorted(priv)

    @staticmethod
    def counts(sel: dict) -> dict:
        c = {}
        for k in CATS:
            v = sel.get(k)
            if v is None:
                continue
            if k == "context":
                c[k] = sum(len(x) for x in v["ctx"].values()) + sum(len(x) for x in v["priv"].values())
            elif k == "reminder":
                c[k] = sum(1 for r in v["rows"] if r["status"] == "pending")
            elif k == "sticker":
                c[k] = sum(1 for r in v.get("stk", []) if r["status"] == "learned")
            elif k == "person":
                c[k] = sum(1 for r in v["mem"] if r["kind"] == "person")  # 印象跟着人物备忘一起删，不另外数
            else:
                c[k] = len(v["mem"])
        return c

    def rebuild_affected(self, deleted_rows: list[dict]):
        """删掉普通备忘之后：按「被删的备忘属于谁」找出受影响的人，用这个人剩下的备忘重写他的印象。
        同一次删掉的印象行带着它折到了哪条备忘（upto），印象行本身也被删时照它重写。"""
        imps = {(r["space"], r["subject"]): r for r in deleted_rows if r["kind"] == "impression"}
        seen = set()
        for r in deleted_rows:
            if r["kind"] != "person" or r.get("source") == "told":  # 「要求记住的」原样带、不并进印象
                continue
            key = (r["space"], r["subject"])
            if key in seen:
                continue
            seen.add(key)
            imp = imps.get(key)
            meta = json.loads(imp.get("meta") or "{}") if imp else {}
            self.eng.spawn(self.eng.memory.rebuild_impression(r["space"], r["subject"], r["subject_name"], r.get("room"),
                                                             upto=meta.get("upto")))

    def preview(self, scope: dict, kinds: list[str]) -> dict:
        return {"counts": self.counts(self.select(scope, kinds)), "labels": CATS}

    # ---------- 备份、重置、撤销 ----------
    def folder(self, ts: float) -> Path:
        return self.core.data / "backups" / f"reset-{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}"

    def run(self, scope: dict, kinds: list[str]) -> dict:
        typ = scope.get("type", "all")
        sel = self.select(scope, kinds)
        counts = self.counts(sel)
        ts = time.time()
        self.core.backup("pre-reset")  # 现有备份体系里的整库备份
        fol = self.folder(ts)
        fol.mkdir(parents=True, exist_ok=True)
        mem_ids = sorted({r["id"] for v in sel.values() for r in v.get("mem", [])})
        stk_rows = sel.get("sticker", {}).get("stk", [])
        stk_files = sorted({r["file"] for r in stk_rows if r.get("file")})
        vec = []
        for i in range(0, len(mem_ids), 500):
            chunk = mem_ids[i:i + 500]
            for r in self.db.x(f"SELECT mem_id, sig, emb FROM memvec WHERE mem_id IN ({','.join('?' * len(chunk))})", chunk):
                vec.append({"mem_id": r[0], "sig": r[1], "emb": base64.b64encode(r[2]).decode()})
        kvs = {}
        for room in {r["room"] for r in [m for v in sel.values() for m in v.get("mem", [])] if r.get("room")} | set(sel.get("context", {}).get("ctx", {})):
            for k in (f"style_n:{room}",):
                if self.db.kv(k) is not None and ("style" in kinds):
                    kvs[k] = self.db.kv(k)
        data = {"ts": ts, "scope": scope, "kinds": kinds, "counts": counts,
                "memories": [r for k in MEM_KINDS if k in sel for r in sel[k]["mem"]], "memvec": vec, "stickers": stk_rows, "sticker_files": stk_files,
                "reminders": sel.get("reminder", {}).get("rows", []), "kv": kvs,
                "context": {k: [self._ent(e) for e in v] for k, v in sel.get("context", {}).get("ctx", {}).items()},
                "private": sel.get("context", {}).get("priv", {}), "floor": sel.get("context", {}).get("floor", [])}
        if stk_files:
            (fol / "stickers").mkdir(exist_ok=True)
            for f in stk_files:
                src = self.eng.stickers.folder() / f
                if src.exists():
                    shutil.copy2(src, fol / "stickers" / f)
        (fol / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # ---- 真删 ----
        if mem_ids:
            for i in range(0, len(mem_ids), 500):
                chunk = mem_ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                self.db.x(f"DELETE FROM memvec WHERE mem_id IN ({q})", chunk)
                self.db.x(f"DELETE FROM memories WHERE id IN ({q})", chunk)
        if stk_rows:
            for r in stk_rows:
                self.db.x("DELETE FROM stickers WHERE id=?", (r["id"],))
            for f in stk_files:
                (self.eng.stickers.folder() / f).unlink(missing_ok=True)
        recap_rows = sel.get("recap", {}).get("mem", [])
        self.eng.memory.unmerge_survivors(recap_rows, {r["id"] for r in recap_rows})  # 上层没了，它所并的、还在的下层变回「没被并」
        if typ == "text":  # 删掉的普通备忘属于谁，就用剩下的备忘重写谁的印象（不靠印象里有没有同样的字）
            self.rebuild_affected(sel.get("person", {}).get("mem", []))
        for r in data["reminders"]:
            self.db.x("DELETE FROM reminders WHERE id=?", (r["id"],))
        for k in kvs:
            self.db.set_kv(k, 0)
        floor = self.db.kv("ctx_floor") or {}
        for key in data["context"]:
            self.eng.rooms[key].ctx.clear()
        for k in data["private"]:
            self.eng.private_hist.pop(k, None)
        for key in data["floor"]:  # 库里所有的会话都记下重置时刻（包括没加载进内存的）
            floor[key] = ts
        if data["floor"]:
            self.db.set_kv("ctx_floor", floor)
        info = {"ts": ts, "folder": fol.name, "scope": scope, "scope_label": self.scope_label(scope), "counts": counts, "undone": False}
        self.db.set_kv("last_reset", info)
        self.eng.log("warn", "memory", f"重置记忆：{info['scope_label']}，" + "、".join(f"{CATS[k]} {n}" for k, n in counts.items() if n) + "（可在记忆页撤销）")
        return info

    def scope_label(self, scope: dict) -> str:
        t, i = scope.get("type", "all"), str(scope.get("id") or "")
        if t == "all":
            return "全部记忆"
        if t == "room":
            d = self.db.doc("chat", i)
            return f"群「{(d or {}).get('name') or i}」"
        if t == "text":
            return f"群里让她忘掉「{i}」"
        if t == "subject":
            return f"群里让她忘掉「{scope.get('label') or i}」（忘掉我）"
        if t == "space":
            sp = self.db.doc("space", i.split(":", 1)[-1]) if i.startswith("sp:") else None
            return f"记忆空间「{(sp or {}).get('name') or i}」"
        p = self.eng.people.persons.get(i)
        return f"人物「{(p or {}).get('name') or i}」"

    @staticmethod
    def _ent(e) -> dict:
        return {"uid": e.uid, "name": e.name, "text": e.text, "bot": e.bot, "role": e.role, "ts": e.ts, "reply_uid": e.reply_uid, "at_uids": list(e.at_uids)}

    def last(self) -> dict | None:
        return self.db.kv("last_reset")

    def undo(self) -> dict:
        info = self.last()
        if not info or info.get("undone"):
            raise ValueError("没有可以撤销的重置")
        fol = self.core.data / "backups" / info["folder"]
        if not (fol / "data.json").exists():
            raise ValueError("备份文件找不到了，撤销不了")
        data = json.loads((fol / "data.json").read_text(encoding="utf-8"))
        idmap: dict[int, int] = {}
        for r in data["memories"]:  # 重新插入：原来的编号还空着就沿用（前情单位靠编号互相指着）；被占了才换新编号，向量和表情包跟着换
            free = not self.db.x("SELECT 1 FROM memories WHERE id=?", (r["id"],)).fetchone()
            cur = self.db.x("""INSERT INTO memories (id, space, kind, subject, subject_name, content, source, src, level, status, ts, room, merged, meta)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (r["id"] if free else None, r["space"], r["kind"], r["subject"], r["subject_name"], r["content"],
                                                                         r["source"], r["src"], r["level"], r["status"], r["ts"], r["room"], r["merged"], r.get("meta")))
            idmap[r["id"]] = cur.lastrowid
        restored = set(idmap.values())
        for r in data["memories"]:
            nid, meta = idmap[r["id"]], json.loads(r.get("meta") or "{}")
            if meta.get("parts"):
                meta["parts"] = [idmap.get(p, p) for p in meta["parts"]]
                self.db.x("UPDATE memories SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), nid))
                self.db.x(f"UPDATE memories SET merged=1 WHERE id IN ({','.join(str(p) for p in meta['parts'])})")
            if r["kind"] == "impression":  # 这期间重写出来的新印象和恢复回来的只能留一个
                self.db.x("DELETE FROM memories WHERE kind='impression' AND space=? AND subject=? AND id!=?", (r["space"], r["subject"], nid))
        self.eng.memory.drop_conflicting_uppers(restored)
        for v in data["memvec"]:
            if v["mem_id"] in idmap:
                self.db.x("INSERT OR REPLACE INTO memvec VALUES (?,?,?)", (idmap[v["mem_id"]], v["sig"], base64.b64decode(v["emb"])))
        for r in data["stickers"]:
            self.db.x("""INSERT INTO stickers (space, room, phash, seen, users, first_ts, last_ts, status, file, size, uses, last_used, mem_id, descr, desc_max)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (r["space"], r["room"], r["phash"], r["seen"], r["users"], r["first_ts"], r["last_ts"], r["status"],
                                                                   r["file"], r["size"], r["uses"], r["last_used"], idmap.get(r["mem_id"]), r["descr"], r.get("desc_max")))
        for f in data["sticker_files"]:
            src = fol / "stickers" / f
            if src.exists():
                shutil.copy2(src, self.eng.stickers.folder() / f)
        for r in data["reminders"]:
            self.db.x("""INSERT INTO reminders (platform, chat_type, chat_id, account_id, user_id, user_name, text, due, created, status)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""", (r["platform"], r["chat_type"], r["chat_id"], r["account_id"], r["user_id"], r["user_name"], r["text"],
                                                           r["due"], r["created"], r["status"]))
        for k, v in data["kv"].items():
            self.db.set_kv(k, v)
        from .engine import Entry
        for key, ents in data["context"].items():
            room = self.eng.rooms.get(key)
            if room:
                for d in ents:
                    room.ctx.append(Entry(d["uid"], d["name"], d["text"], bot=d["bot"], role=d.get("role", ""), ts=d["ts"], reply_uid=d.get("reply_uid", ""), at_uids=d.get("at_uids", [])))
        for k, h in data["private"].items():
            self.eng.private_hist.setdefault(k, []).extend(h)
        floor = self.db.kv("ctx_floor") or {}
        for key in set(data.get("floor") or []) | set(data["context"]) | {self.eng.dm_key(*k.split(":", 1)) for k in data["private"]}:
            floor.pop(key, None)  # 撤销了：重启后的回填不再避开这些
        self.db.set_kv("ctx_floor", floor)
        info["undone"] = True
        self.db.set_kv("last_reset", info)
        self.eng.log("info", "memory", f"撤销了重置：{info['scope_label']}")
        return info

    def prune(self, keep_days: int):
        """过期的重置备份文件夹（撤销只认最近一次，更早的留着也没用）：超过保留天数、且不是最近一次的就删。"""
        last = (self.last() or {}).get("folder")
        for f in (self.core.data / "backups").glob("reset-*"):
            if f.is_dir() and f.name != last and time.time() - f.stat().st_mtime > keep_days * 86400:
                shutil.rmtree(f, ignore_errors=True)
