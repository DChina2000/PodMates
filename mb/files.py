"""读文件：下载到临时目录 → 抽出文字 → 删掉原文件 → 文字切片存进记忆（建向量，以后能被想起）。

只解析，不运行：PDF 用 pypdf 抽文字，Word/Excel/PowerPoint 只读 zip 里的 XML，文本直接解码；
不执行宏、不打开链接、不解压到磁盘。zip 类文件有解压后体积上限，PDF 有页数上限。
图片不在这里处理，交给看图模型（见 Engine.describe_images）。
"""
import io
import re
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .paths import data_dir

# 面板里「允许的格式」的选项 → 扩展名
FORMATS = {
    "pdf": [".pdf"], "word": [".docx"], "excel": [".xlsx"], "ppt": [".pptx"],
    "text": [".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs", ".sql"],
    "image": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"],
}
FORMAT_LABEL = [["pdf", "PDF"], ["word", "Word（docx）"], ["excel", "Excel（xlsx）"], ["ppt", "PowerPoint（pptx）"], ["text", "文本与代码（txt、md、csv、json……）"], ["image", "图片（交给看图模型）"]]
MAX_UNZIPPED = 80 << 20
MAX_PDF_PAGES = 300
CHUNK = 600


class FileError(Exception):
    pass


def kind_of(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    return next((k for k, exts in FORMATS.items() if ext in exts), None)


def est_tokens(text: str) -> int:
    """粗估 token 数：汉字约 0.8 个，其他字符约 0.25 个。只用来限量，不求精确。"""
    cjk = len(re.findall(r"[㐀-鿿　-〿＀-￯]", text))
    return int(cjk * 0.8 + (len(text) - cjk) * 0.25)


def clip_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    if est_tokens(text) <= max_tokens:
        return text, False
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if est_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], True


def _zip_guard(z: zipfile.ZipFile):
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise FileError("文件解压后太大，不读")


def _xml_text(data: bytes, tag_suffix: str, sep: str = "") -> str:
    out = []
    for ev, el in ET.iterparse(io.BytesIO(data)):
        if el.tag.endswith(tag_suffix) and el.text:
            out.append(el.text)
        elif el.tag.endswith("}p") or el.tag.endswith("}br"):
            out.append("\n")
    return sep.join(out)


def _docx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        _zip_guard(z)
        data = z.read("word/document.xml")
    paras, cur = [], []
    for ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag.endswith("}t") and el.text:
            cur.append(el.text)
        elif el.tag.endswith("}tab"):
            cur.append("\t")
        elif el.tag.endswith("}p"):
            paras.append("".join(cur))
            cur = []
    return "\n".join(p for p in paras if p.strip())


def _xlsx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        _zip_guard(z)
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for ev, el in ET.iterparse(io.BytesIO(z.read("xl/sharedStrings.xml")), events=("end",)):
                if el.tag.endswith("}si"):
                    shared.append("".join(t.text or "" for t in el.iter() if t.tag.endswith("}t")))
        sheet_names = {}
        if "xl/workbook.xml" in names:
            for ev, el in ET.iterparse(io.BytesIO(z.read("xl/workbook.xml")), events=("end",)):
                if el.tag.endswith("}sheet"):
                    sheet_names[len(sheet_names) + 1] = el.attrib.get("name", "")
        out = []
        for n in sorted(x for x in names if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
            idx = int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1))
            out.append(f"【工作表 {sheet_names.get(idx, idx)}】")
            row = []
            for ev, el in ET.iterparse(io.BytesIO(z.read(n)), events=("end",)):
                if el.tag.endswith("}c"):
                    v = next((c.text for c in el if c.tag.endswith("}v")), None)
                    if el.attrib.get("t") == "s" and v is not None and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    elif el.attrib.get("t") == "inlineStr":
                        v = "".join(t.text or "" for t in el.iter() if t.tag.endswith("}t"))
                    row.append(v or "")
                elif el.tag.endswith("}row"):
                    if any(x.strip() for x in row):
                        out.append("\t".join(row))
                    row = []
    return "\n".join(out)


def _pptx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        _zip_guard(z)
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        out = []
        for i, n in enumerate(slides, 1):
            texts = [el.text for ev, el in ET.iterparse(io.BytesIO(z.read(n)), events=("end",)) if el.tag.endswith("}t") and el.text]
            out.append(f"【第 {i} 页】\n" + "\n".join(texts))
    return "\n".join(out)


def _pdf(raw: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(raw))
    if r.is_encrypted:
        raise FileError("PDF 有密码，读不了")
    out = []
    for i, page in enumerate(r.pages):
        if i >= MAX_PDF_PAGES:
            out.append(f"（只读前 {MAX_PDF_PAGES} 页）")
            break
        out.append(page.extract_text() or "")
    return "\n".join(out)


def _text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def extract(raw: bytes, name: str) -> str:
    """抽出文字。读不出来（扫描版 PDF 没有文字层、格式坏了）就抛 FileError。"""
    k = kind_of(name)
    try:
        text = {"pdf": _pdf, "word": _docx, "excel": _xlsx, "ppt": _pptx, "text": _text}[k](raw)
    except FileError:
        raise
    except KeyError:
        raise FileError("不支持这种格式")
    except Exception as e:
        raise FileError(f"文件读不出来（{type(e).__name__}）")
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise FileError("文件里没有可读的文字（扫描件需要先做文字识别）")
    return text


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """按段落切成 600 字左右的片段。"""
    out, cur = [], ""
    for para in re.split(r"\n+", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


# ---------- 保留原文件（可选）与容量上限 ----------
def keep_dir() -> Path:
    d = data_dir() / "files"
    d.mkdir(exist_ok=True)
    return d


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(name).name)[:80] or "file"


def keep_file(raw: bytes, name: str) -> Path:
    p = keep_dir() / f"{int(time.time())}-{safe_name(name)}"
    p.write_bytes(raw)
    return p


def enforce_cache(max_mb: int, keep_days: int):
    """过期的删掉；总量超过上限就从最旧的开始删。"""
    d = keep_dir()
    now = time.time()
    files = sorted((f for f in d.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
    for f in list(files):
        if keep_days and now - f.stat().st_mtime > keep_days * 86400:
            f.unlink(missing_ok=True)
            files.remove(f)
    total = sum(f.stat().st_size for f in files)
    while files and total > max_mb * (1 << 20):
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def cache_size() -> int:
    return sum(f.stat().st_size for f in keep_dir().iterdir() if f.is_file())
