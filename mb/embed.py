"""语义回忆用的向量：本机模型（ONNX Runtime，默认）或 OpenAI 兼容的 embeddings 接口。

向量存在同一个 SQLite 文件的 memvec 表里，用 sqlite-vec 扩展的 vec_distance_cosine 检索。
每条向量记着「签名」（来源+模型+维度），换来源或换模型后旧向量不通用，要重建。
程序自己盯着：启动时和运行中每隔一会儿检查一次，只要有记忆没有「当前签名」的向量（换了模型、导入或恢复的记忆没算过、上次没算成），
就在后台自动补算；重建期间语义回忆照常可用（用已经算好的）。模型没下载好、接口没设好时不硬试，面板上写明原因，条件好了再补。
"""
import asyncio
import json
import os
import struct
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .paths import ASSETS, data_dir
from . import tls

BGE_PREFIX = "为这个句子生成表示以用于检索相关文章："
QWEN_PREFIX = "Instruct: 根据群聊里眼下的话题，找出意思相关的旧事\nQuery:"

# 本机模型预设：ONNX 导出版，文件来自 ModelScope / Hugging Face（同名仓库，文件逐字节相同）。
# retr = 公开的 C-MTEB 检索平均分；chat = 本程序的群聊记忆小评测命中率（tools/eval_embed.py）；
# mem/ms = 本机实测（加载后多占内存、单句检索耗时）；thr = 默认相似度门槛，按小评测取「闲聊噪声误召回约 1%～2%」处
LOCAL = {
    "bge-small-zh-v1.5": dict(name="BGE small 中文 v1.5（默认，随安装包）", repo="Xenova/bge-small-zh-v1.5",
                              file="onnx/model_int8.onnx", pooling="cls", prefix=BGE_PREFIX, dim=512, mb=24,
                              license="MIT", bundled=True, retr=61.77, chat=88, mem=49, ms=2, thr=0.48),
    "gte-multilingual-base": dict(name="GTE multilingual base（效果最好，内存大）", repo="onnx-community/gte-multilingual-base",
                                  file="onnx/model_quantized.onnx", pooling="cls", prefix="", dim=768, mb=340,
                                  license="Apache-2.0", retr=71.95, chat=100, mem=595, ms=8, thr=0.585),
    "qwen3-embedding-0.6b": dict(name="Qwen3-Embedding 0.6B", repo="onnx-community/Qwen3-Embedding-0.6B-ONNX",
                                 file="onnx/model_int8.onnx", pooling="last", prefix="", dim=1024, mb=614,
                                 license="Apache-2.0", retr=71.03, chat=88, mem=660, ms=47, thr=0.46),
    "jina-v2-base-zh": dict(name="Jina v2 base 中文", repo="jinaai/jina-embeddings-v2-base-zh",
                            file="onnx/model_quantized.onnx", pooling="mean", prefix="", dim=768, mb=162,
                            license="Apache-2.0", retr=69.40, chat=79, mem=197, ms=7, thr=0.34),
    "bge-base-zh-v1.5": dict(name="BGE base 中文 v1.5", repo="Xenova/bge-base-zh-v1.5",
                             file="onnx/model_int8.onnx", pooling="cls", prefix="", dim=768, mb=103,
                             license="MIT", retr=69.49, chat=58, mem=129, ms=12, thr=0.81),
}
# API 预设：OpenAI 兼容 /embeddings。price = 每百万 tokens（查价日期 2026-10-24），retr 同上；thr 为默认门槛（未实测，可在面板「试一试」后调）
API = {
    "siliconflow-qwen3-4b": dict(name="硅基流动 Qwen3-Embedding-4B（推荐）", base_url="https://api.siliconflow.cn/v1",
                                 model="Qwen/Qwen3-Embedding-4B", dim=2560, price="¥0.14", retr=77.03, thr=0.45),
    "siliconflow-qwen3-8b": dict(name="硅基流动 Qwen3-Embedding-8B", base_url="https://api.siliconflow.cn/v1",
                                 model="Qwen/Qwen3-Embedding-8B", dim=4096, price="¥0.28", retr=78.21, thr=0.45),
    "siliconflow-bge-m3": dict(name="硅基流动 BAAI/bge-m3（免费）", base_url="https://api.siliconflow.cn/v1",
                               model="BAAI/bge-m3", dim=1024, price="免费", retr=65.43, thr=0.50),
    "dashscope-v4": dict(name="阿里百炼 text-embedding-v4", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                         model="text-embedding-v4", dim=1024, price="¥0.5", retr=None, thr=0.5),
    "zhipu-embedding-3": dict(name="智谱 embedding-3", base_url="https://open.bigmodel.cn/api/paas/v4",
                              model="embedding-3", dim=2048, price="¥0.5", retr=None, thr=0.5),
    "openai-3-small": dict(name="OpenAI text-embedding-3-small", base_url="https://api.openai.com/v1",
                           model="text-embedding-3-small", dim=1536, price="$0.02", retr=None, thr=0.5),
}

def recommended_local(ram_gb: float) -> str:
    """向导默认的本机模型：内存 ≥ 16 GB 用效果最好的 GTE（一键下载），否则用随包的 bge-small。"""
    return "gte-multilingual-base" if ram_gb >= 16 else "bge-small-zh-v1.5"


SOURCES = ["https://www.modelscope.cn/models/{repo}/resolve/master/{path}",
           "https://huggingface.co/{repo}/resolve/main/{path}",
           "https://hf-mirror.com/{repo}/resolve/main/{path}"]


def pack(v) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def model_dir(key: str) -> Path:
    p = LOCAL.get(key, {})
    if p.get("bundled"):
        return ASSETS / "models" / key
    return data_dir() / "models" / key


def model_ready(key: str, folder: str = "") -> bool:
    d = Path(folder) if folder else model_dir(key)
    return (d / "model.onnx").exists() and (d / "tokenizer.json").exists()


def download_model(key: str, progress=None) -> Path:
    """依次试 ModelScope、Hugging Face、hf-mirror。都失败时按说明手动放文件。"""
    p = LOCAL[key]
    d = model_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    errors = []
    for tpl in SOURCES:
        try:
            for path, dest in ((p["file"], "model.onnx"), ("tokenizer.json", "tokenizer.json")):
                url = tpl.format(repo=p["repo"], path=path)
                tmp = d / (dest + ".part")
                with tls.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                    total, done = int(r.headers.get("Content-Length") or 0), 0
                    while chunk := r.read(1 << 16):
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(dest, done, total)
                os.replace(tmp, d / dest)
            return d
        except Exception as e:
            errors.append(f"{tpl.split('/')[2]}：{e}")
    raise RuntimeError("下载失败（" + "；".join(errors) + f"）。可以手动下载 model.onnx 和 tokenizer.json 放进 {d}")


class LocalModel:
    def __init__(self, folder: Path, pooling: str, prefix: str):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # 机器人旁边还有别的程序，不占满 CPU
        opts.inter_op_num_threads = 1
        # 句子长短不一，开着内存池会按每种长度各留一份缓冲，越跑越大（实测 60 批涨 280 MB）；关掉几乎不影响速度
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        # 默认算完后线程会空转等下一次（实测空闲时一直占将近一个核）；关掉后每次只慢不到 1 毫秒
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.sess = ort.InferenceSession(str(folder / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.outputs = {o.name for o in self.sess.get_outputs()}
        self.past = [i for i in self.sess.get_inputs() if i.name.startswith("past_key_values")]
        self.tok = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tok.enable_truncation(256)
        self.pooling, self.prefix = pooling, prefix
        pad_id = self.tok.token_to_id("<|endoftext|>") or self.tok.token_to_id("[PAD]") or 0
        self.tok.enable_padding(pad_id=pad_id, direction="left" if pooling == "last" else "right")

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        np = self.np
        if query and self.prefix:
            texts = [self.prefix + t for t in texts]
        if self.pooling == "last":  # Qwen3-Embedding：句末加结束符，取最后一个位置
            texts = [t + "<|endoftext|>" for t in texts]
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        if "position_ids" in self.inputs:
            feed["position_ids"] = np.clip(np.cumsum(mask, axis=1) - 1, 0, None)
        for i in self.past:  # 解码器导出（如 Qwen3）带 KV 缓存输入：给空的
            feed[i.name] = np.zeros((len(texts), i.shape[1], 0, i.shape[3]), dtype=np.float16 if "float16" in i.type else np.float32)
        out = self.sess.run(["last_hidden_state"] if "last_hidden_state" in self.outputs else None,
                            {k: v for k, v in feed.items() if k in self.inputs})[0]
        if self.pooling == "cls":
            vec = out[:, 0]
        elif self.pooling == "last":
            vec = out[:, -1]
        else:
            m = mask[..., None].astype(out.dtype)
            vec = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        vec = vec / np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
        return vec.astype("float32").tolist()


class Paused(RuntimeError):
    """用量硬上限生效期间，通过 API 算向量的事暂停（本机向量不受影响）。"""


RETRY_SECS = 600  # 自动补算失败后第一次重试的间隔；条件不变就越等越久（翻倍，最长 6 小时），免得刷日志


class Embedder:
    cap_check = None  # 由引擎设：返回 True 表示用量硬上限已到

    def __init__(self, db, log=None, emit=None):
        self.db, self.log, self.emit = db, log or (lambda *a, **k: None), emit or (lambda *a: None)
        self.ex = ThreadPoolExecutor(1, thread_name_prefix="embed")
        self.model = None
        self.model_sig = ""
        self.rebuilding = False
        self.progress = (0, 0)
        self.session = None
        self.blocked = ""  # 为什么现在没法自动补算（给面板看）；空 = 没问题
        self._fail: tuple | None = None  # （失败时的条件指纹, 时间, 第几次失败）
        self._task: asyncio.Task | None = None
        self.db.x("CREATE TABLE IF NOT EXISTS memvec (mem_id INTEGER PRIMARY KEY, sig TEXT, emb BLOB)")

    # ---------- 配置 ----------
    def conf(self) -> dict:
        c = self.db.kv("embedding") or {}
        return {"source": c.get("source", "local"), "preset": c.get("preset", "bge-small-zh-v1.5"),
                "local_folder": c.get("local_folder", ""), "local_pooling": c.get("local_pooling", "cls"),
                "local_prefix": c.get("local_prefix", ""), "api_preset": c.get("api_preset", "siliconflow-qwen3-4b"),
                "api_base": c.get("api_base", ""), "api_model": c.get("api_model", ""), "api_dim": c.get("api_dim", 0)}

    def sig(self) -> str:
        c = self.conf()
        if c["source"] == "api":
            a = API.get(c["api_preset"], {})
            return f"api:{c['api_base'] or a.get('base_url', '')}:{c['api_model'] or a.get('model', '')}:{c['api_dim'] or a.get('dim', '')}"
        if c["preset"] == "custom":
            return f"local:custom:{c['local_folder']}:{c['local_pooling']}"
        return f"local:{c['preset']}"

    def status(self) -> dict:
        total = self.db.x("SELECT COUNT(*) FROM memories").fetchone()[0]
        ok = self.db.x("SELECT COUNT(*) FROM memvec v JOIN memories m ON m.id=v.mem_id WHERE v.sig=?", (self.sig(),)).fetchone()[0]
        stale = self.db.x("SELECT COUNT(*) FROM memvec v JOIN memories m ON m.id=v.mem_id WHERE v.sig!=?", (self.sig(),)).fetchone()[0]
        missing = len(self.missing_ids())
        c = self.conf()
        ready = True
        if c["source"] == "local":
            ready = model_ready(c["preset"], c["local_folder"] if c["preset"] == "custom" else "")
        return {"sig": self.sig(), "total": total, "indexed": ok, "stale": stale, "missing": missing, "needs_rebuild": stale > 0 or missing > 0,
                "rebuilding": self.rebuilding, "progress": self.progress, "model_ready": ready, "blocked": self.blocked}

    def missing_ids(self) -> list[int]:
        """该有向量、却没有「当前签名」向量的记忆（风格画像不建向量）：换了模型的旧记忆、绕过正常写入的、上次没算成的。"""
        return [r["id"] for r in self.db.rows("""SELECT m.id FROM memories m WHERE m.kind NOT IN ('style','impression')
                AND NOT EXISTS (SELECT 1 FROM memvec v WHERE v.mem_id=m.id AND v.sig=?) ORDER BY m.id""", (self.sig(),))]

    def unavailable(self) -> str:
        """现在算不了向量的原因（空 = 能算）：本机模型没下载好、接口没设好、用量硬上限。"""
        c = self.conf()
        if c["source"] == "api":
            a = API.get(c["api_preset"], {})
            if not ((c["api_base"] or a.get("base_url")) and (c["api_model"] or a.get("model")) and self.db.secret("embed:api")):
                return "向量接口还没设好（地址、模型名、密钥）；设好后会自动补算"
            if self.cap_check and self.cap_check():
                return "用量硬上限生效中，接口向量先暂停；恢复后会自动补算"
            return ""
        if not model_ready(c["preset"], c["local_folder"] if c["preset"] == "custom" else ""):
            return "本机向量模型还没下载好（到「记忆 → 语义回忆」下载）；下好后会自动补算"
        return ""

    def threshold(self) -> float:
        """模型自带的默认门槛（各模型分数分布不同）；设置里填了大于 0 的数就用设置的。"""
        c = self.conf()
        if c["source"] == "api":
            return API.get(c["api_preset"], {}).get("thr", 0.5)
        return LOCAL.get(c["preset"], {}).get("thr", 0.5)

    def usable(self) -> bool:
        """能不能做语义回忆：只要向量模型能用就行。重建期间、还没重建完时照常用已经算好的（检索只认当前签名的向量，不会混着旧模型的）。"""
        return not self.unavailable()

    # ---------- 计算向量 ----------
    def _load(self, c: dict, sig: str, loaded: list):
        """在向量线程里跑：不碰数据库（数据库只在主循环里用）。"""
        if self.model is not None and self.model_sig == sig:
            return self.model
        if c["preset"] == "custom":
            folder, pooling, prefix = Path(c["local_folder"]), c["local_pooling"], c["local_prefix"]
        else:
            p = LOCAL[c["preset"]]
            folder, pooling, prefix = model_dir(c["preset"]), p["pooling"], p["prefix"]
        if not model_ready(c["preset"], str(folder)):
            raise RuntimeError("本机向量模型还没下载，到「记忆 → 语义回忆」里点下载")
        t = time.time()
        self.model = LocalModel(folder, pooling, prefix)
        self.model_sig = sig
        loaded.append(time.time() - t)
        return self.model

    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        c = self.conf()
        if c["source"] == "api":
            return await self._api(texts, c)
        sig, loaded = self.sig(), []
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(self.ex, lambda: self._load(c, sig, loaded).embed(texts, query))
        if loaded:
            self.log("info", "memory", f"向量模型已加载（{c['preset']}，{loaded[0]:.1f} 秒）")
        return out

    async def _api(self, texts, c):
        import aiohttp
        if self.cap_check and self.cap_check():
            raise Paused("用量硬上限生效中，接口向量先暂停")
        a = API.get(c["api_preset"], {})
        base = (c["api_base"] or a.get("base_url", "")).rstrip("/")
        model = c["api_model"] or a.get("model", "")
        key = self.db.secret("embed:api")
        if not (base and model and key):
            raise RuntimeError("向量接口还没设好（地址、模型名、密钥）")
        body = {"model": model, "input": texts}
        if c["api_dim"]:
            body["dimensions"] = int(c["api_dim"])
        if self.session is None:
            self.session = tls.session(timeout=aiohttp.ClientTimeout(total=60))
        async with self.session.post(f"{base}/embeddings", json=body, headers={"Authorization": f"Bearer {key}"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
        u = data.get("usage") or {}
        self.db.add_usage(None, None, None, "embed", model, u.get("prompt_tokens") or u.get("total_tokens") or 0, 0, 0)
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]

    # ---------- 写入、检索 ----------
    async def index(self, ids: list[int]):
        if not ids or self.rebuilding or self.unavailable():
            return  # 这几条没算上也不要紧：它们会被当成「缺向量」，自动补算
        q = ",".join("?" * len(ids))
        rows = self.db.rows(f"SELECT id, kind, subject_name, content FROM memories WHERE id IN ({q}) AND kind NOT IN ('style','impression')", ids)
        if not rows:
            return
        vecs = await self.embed([doc_text(r) for r in rows])
        sig = self.sig()
        for r, v in zip(rows, vecs):
            self.db.x("INSERT OR REPLACE INTO memvec VALUES (?,?,?)", (r["id"], sig, pack(v)))

    async def search(self, query: str, spaces: list[str], k: int, min_sim: float, exclude: set, kinds: tuple | None = None) -> list[dict]:
        """kinds 不填 = 语义回忆用：排除风格画像和表情包；找表情包时 kinds=("sticker",)。"""
        if not spaces or not query.strip() or not self.usable():
            return []
        v = (await self.embed([query], query=True))[0]
        q = ",".join("?" * len(spaces))
        kind_sql = f"m.kind IN ({','.join('?' * len(kinds))})" if kinds else "m.kind NOT IN ('style','sticker','impression')"
        rows = self.db.rows(f"""SELECT m.id, m.kind, m.subject_name, m.content, m.source, m.ts, vec_distance_cosine(v.emb, ?) AS d
                                FROM memvec v JOIN memories m ON m.id = v.mem_id
                                WHERE v.sig=? AND m.status='active' AND m.space IN ({q}) AND {kind_sql}
                                ORDER BY d LIMIT ?""", (pack(v), self.sig(), *spaces, *(kinds or ()), k + len(exclude)))
        min_sim = min_sim if min_sim and min_sim > 0 else self.threshold()
        return [r for r in rows if r["id"] not in exclude and 1 - r["d"] >= min_sim][:k]

    def kick(self):
        """相关操作之后（换模型、下载完、导入数据）马上检查一次，不等下一轮定时检查。"""
        try:
            asyncio.get_running_loop().create_task(self.ensure())
        except RuntimeError:
            pass

    def _fingerprint(self) -> tuple:
        return (self.sig(), not self.unavailable())

    async def ensure(self):
        """索引过期（换了模型 / 有记忆没有当前向量）就在后台自动补算，不阻塞回复。启动时和运行中定期调用。
        算不了（模型没下载、接口没设好、用量硬上限）：记一条警告、面板上写明原因，不重试；条件变了（下好了、填了密钥）再补。
        算失败了：同样条件下隔一阵再试，越试隔得越久。"""
        if self.rebuilding:
            return
        st = self.status()
        if not st["needs_rebuild"]:
            self.blocked, self._fail = "", None
            return
        reason = self.unavailable()
        if reason:
            if self.blocked != reason:
                self.log("warn", "memory", f"语义回忆的向量有 {st['missing']} 条记忆还没算：{reason}")
            self.blocked = reason
            return
        if self._fail and self._fail[0] == self._fingerprint() and time.time() - self._fail[1] < min(RETRY_SECS * 2 ** (self._fail[2] - 1), 21600):
            return
        self.blocked = ""
        self.log("info", "memory", f"语义回忆的向量过期（{st['stale']} 条是别的模型算的，共 {st['missing']} 条缺当前向量），在后台自动重建")
        self._task = asyncio.get_running_loop().create_task(self.rebuild(full=False))

    async def rebuild(self, full: bool = True):
        """在后台给记忆重新算向量。full=True（面板上点「一键重建」）：全部重算；full=False（自动）：只补缺的，上次算到哪接着算。
        换了模型留下的旧向量先清掉（检索本来就只认当前签名的）。"""
        if self.rebuilding:
            return
        self.rebuilding = True
        fp, ok = self._fingerprint(), False
        try:
            self.db.x("DELETE FROM memvec WHERE sig!=?", (self.sig(),))
            ids = [r["id"] for r in self.db.rows("SELECT id FROM memories ORDER BY id")] if full else self.missing_ids()
            done = 0
            self.progress = (0, len(ids))
            for i in range(0, len(ids), 32):
                chunk = ids[i:i + 32]
                rows = self.db.rows(f"SELECT id, kind, subject_name, content FROM memories WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                vecs = await self.embed([doc_text(r) for r in rows])
                sig = self.sig()
                for r, v in zip(rows, vecs):
                    self.db.x("INSERT OR REPLACE INTO memvec VALUES (?,?,?)", (r["id"], sig, pack(v)))
                done += len(chunk)
                self.progress = (done, len(ids))
                self.emit("embed", {"done": done, "total": len(ids)})
            self.log("info", "memory", f"向量重建完成，共 {len(ids)} 条记忆")
            ok = True
        except Paused as e:
            self.blocked = f"{e}；恢复后会自动补算"
            self.log("warn", "memory", f"向量重建先暂停：{e}")
        except Exception as e:
            n = (self._fail[2] + 1) if self._fail and self._fail[0] == fp else 1
            self._fail = (fp, time.time(), n)
            self.blocked = f"上次重建没成功：{e}；过一阵会自动再试"
            self.log("warn" if n > 1 else "error", "memory", f"向量重建失败：{e}")
            self.emit("embed", {"error": str(e)})
        finally:
            if ok:
                self.blocked, self._fail = "", None
            self.rebuilding = False
            self.emit("embed", {"finished": True, **self.status()})

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


def doc_text(r: dict) -> str:
    if r["kind"] == "sticker":
        return f"表情包：{r['content']}"
    if r["kind"] == "doc":
        return f"文件《{r['subject_name']}》：{r['content']}"
    if r["kind"] == "meme":
        return f"群梗「{r['subject_name']}」：{r['content']}"
    if r["kind"] == "person" and r.get("subject_name"):
        return f"{r['subject_name']}：{r['content']}"
    return r["content"]


def load_vec(conn):
    """给 SQLite 连接加载 sqlite-vec 扩展。"""
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
