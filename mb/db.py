"""单个 SQLite 文件保存全部数据（含语义回忆的向量）。密钥单独放 secrets 表，导出时可整表去掉。"""
import datetime
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import secretbox

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS secrets (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS settings (scope TEXT, key TEXT, value TEXT, PRIMARY KEY (scope, key));
CREATE TABLE IF NOT EXISTS docs (kind TEXT, id TEXT, ord REAL, data TEXT, PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS persona_history (id INTEGER PRIMARY KEY, persona_id TEXT, ts REAL, note TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, room TEXT, ts REAL, account_id TEXT, msg_id TEXT,
    user_id TEXT, name TEXT, text TEXT, role_id TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS messages_room ON messages (room, id);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, space TEXT, kind TEXT, subject TEXT, subject_name TEXT,
    content TEXT, source TEXT, src TEXT, level INTEGER DEFAULT 1, status TEXT, ts REAL, room TEXT, merged INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS memories_space ON memories (space, kind, subject);
CREATE TABLE IF NOT EXISTS usage (day TEXT, role_id TEXT, platform TEXT, room TEXT, purpose TEXT, model TEXT,
    calls INTEGER, prompt INTEGER, cached INTEGER, completion INTEGER,
    PRIMARY KEY (day, role_id, platform, room, purpose, model));
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, platform TEXT, chat_type TEXT, chat_id TEXT, account_id TEXT,
    user_id TEXT, user_name TEXT, text TEXT, due REAL, created REAL, status TEXT);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (status, due);
CREATE TABLE IF NOT EXISTS stickers (id INTEGER PRIMARY KEY, space TEXT, room TEXT, phash TEXT, seen INTEGER, users TEXT,
    first_ts REAL, last_ts REAL, status TEXT, file TEXT, size INTEGER DEFAULT 0, uses INTEGER DEFAULT 0, last_used REAL,
    mem_id INTEGER, descr TEXT);
CREATE INDEX IF NOT EXISTS stickers_space ON stickers (space, status);
CREATE TABLE IF NOT EXISTS vis_cache (sha TEXT, model TEXT, descr TEXT, ts REAL, PRIMARY KEY (sha, model));
CREATE TABLE IF NOT EXISTS counters (day TEXT, name TEXT, role_id TEXT, room TEXT, n INTEGER, PRIMARY KEY (day, name, role_id, room));
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, ts REAL, level TEXT, cat TEXT, text TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS logs_ts ON logs (ts);
"""


def since_day(days: int) -> str:
    """用量页「近 N 天」的起始日期：今天往前数 N-1 天（今天算第一天）。"""
    return (datetime.date.today() - datetime.timedelta(days=max(1, int(days)) - 1)).isoformat()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:10]


class Done:
    """已经取完的查询结果（行为像 cursor：fetchone / fetchall / 迭代 / lastrowid / rowcount）。"""

    def __init__(self, rows: list, lastrowid, rowcount):
        self._rows, self.lastrowid, self.rowcount = rows, lastrowid, rowcount

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()  # 一个连接被事件循环线程和微信线程等一起用：同一条 SQL 的预编译语句是共用的，同时走会串（读出 None、报 API misuse），所以执行和取结果整个串行
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        from .embed import load_vec
        load_vec(self.conn)  # 语义回忆：向量用 sqlite-vec 扩展存在同一个文件里
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.migrate()
        self.encrypt_legacy_secrets()

    def migrate(self):
        """给旧版本的库补列。"""
        mcols = {r[1] for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "meta" not in mcols:  # 前情单位的覆盖范围、所并各段、说话和被提到的人；印象折到了哪条备忘
            self.conn.execute("ALTER TABLE memories ADD COLUMN meta TEXT")
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(stickers)")}
        for c, t in (("source", "TEXT"), ("account", "TEXT"), ("desc_max", "INTEGER")):  # desc_max：这条描述是按多少字上限写的（空 = 旧记录，按 40 算）；source = 'fav'：QQ 账号收藏的表情（属于 account 这个账号，所有群都能用）；空 = 群里学来的
            if c not in scols:
                self.conn.execute(f"ALTER TABLE stickers ADD COLUMN {c} {t}")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(usage)")}
        for c in ("reasoning", "cache_write"):
            if c not in cols:
                self.conn.execute(f"ALTER TABLE usage ADD COLUMN {c} INTEGER DEFAULT 0")

    def x(self, sql: str, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            if cur is None:  # 测试里换成了假连接
                return None
            return Done(cur.fetchall() if cur.description else [], cur.lastrowid, cur.rowcount)

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    def close(self):
        self.conn.close()

    # ---- kv / secrets ----
    def kv(self, k: str, default=None):
        r = self.x("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_kv(self, k: str, v):
        self.x("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))

    def del_kv(self, k: str):
        self.x("DELETE FROM kv WHERE k=?", (k,))

    def secret(self, k: str) -> str:
        """读密钥（库里存的是按当前 Windows 用户加密的密文）。解不开（比如换了电脑）按没填处理。"""
        r = self.x("SELECT v FROM secrets WHERE k=?", (k,)).fetchone()
        if not r:
            return ""
        return secretbox.decrypt(r[0]) or ""

    def set_secret(self, k: str, v: str):
        if v:
            self.x("INSERT OR REPLACE INTO secrets VALUES (?,?)", (k, secretbox.encrypt(v)))
        else:
            self.x("DELETE FROM secrets WHERE k=?", (k,))

    def secret_state(self, k: str) -> str:
        """ok / missing / unreadable（有密文但解不开：换了电脑或 Windows 用户，需要重新填）。"""
        r = self.x("SELECT v FROM secrets WHERE k=?", (k,)).fetchone()
        if not r or not r[0]:
            return "missing"
        return "ok" if secretbox.decrypt(r[0]) is not None else "unreadable"

    def unreadable_secrets(self) -> list[str]:
        return [r[0] for r in self.x("SELECT k, v FROM secrets") if secretbox.decrypt(r[1]) is None]

    def encrypt_legacy_secrets(self) -> int:
        """旧版本留下的明文密钥，打开时一次性换成密文。"""
        n = 0
        for k, v in self.x("SELECT k, v FROM secrets").fetchall():
            if v and not secretbox.is_encrypted(v):
                self.x("UPDATE secrets SET v=? WHERE k=?", (secretbox.encrypt(v), k))
                n += 1
        return n

    # ---- 文档（角色、账号、人设、模块、人物、分组、群档案、记忆空间、端点……）----
    def docs(self, kind: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.x("SELECT data FROM docs WHERE kind=? ORDER BY ord, rowid", (kind,))]

    def doc(self, kind: str, id: str) -> dict | None:
        r = self.x("SELECT data FROM docs WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, kind: str, data: dict) -> dict:
        data.setdefault("id", new_id(kind[:2] + "_"))
        r = self.x("SELECT ord FROM docs WHERE kind=? AND id=?", (kind, data["id"])).fetchone()
        ord_ = r[0] if r else (self.x("SELECT COALESCE(MAX(ord),0)+1 FROM docs WHERE kind=?", (kind,)).fetchone()[0])
        self.x("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", (kind, data["id"], ord_, json.dumps(data, ensure_ascii=False)))
        return data

    def delete(self, kind: str, id: str):
        self.x("DELETE FROM docs WHERE kind=? AND id=?", (kind, id))

    def reorder(self, kind: str, ids: list[str]):
        for i, id in enumerate(ids):
            self.x("UPDATE docs SET ord=? WHERE kind=? AND id=?", (i + 1, kind, id))

    # ---- 用量 ----
    def add_usage(self, role_id, platform, room, purpose, model, prompt, cached, completion, reasoning=0, cache_write=0):
        day = time.strftime("%Y-%m-%d")
        self.x("""INSERT INTO usage (day, role_id, platform, room, purpose, model, calls, prompt, cached, completion,
                                    reasoning, cache_write) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)
                  ON CONFLICT (day, role_id, platform, room, purpose, model) DO UPDATE SET calls=calls+1,
                  prompt=prompt+excluded.prompt, cached=cached+excluded.cached, completion=completion+excluded.completion,
                  reasoning=reasoning+excluded.reasoning, cache_write=cache_write+excluded.cache_write""",
               (day, role_id or "", platform or "", room or "", purpose, model, prompt, cached, completion,
                reasoning or 0, cache_write or 0))

    # ---- 备份 ----
    def backup_to(self, path: Path, strip_secrets: bool = False):
        path = Path(path)
        path.unlink(missing_ok=True)
        dst = sqlite3.connect(path)
        self.conn.backup(dst)
        if strip_secrets:
            dst.execute("DELETE FROM secrets")
            dst.commit()
            dst.execute("VACUUM")
        dst.close()
