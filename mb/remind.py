"""「记住……」和提醒：从一句话里认出意图和时间（纯代码，不花模型调用），提醒存数据库，重启不丢。

只在这条消息是对她说的（@、回复、叫名字）时才认。提醒到点后，由当时在该群值班的角色用自己的口吻 @ 这个人提醒。
"""
import re
import time

NUM = r"[0-9]+|[零〇一二两三四五六七八九十]+"
CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
UNIT_SECS = {"秒": 1, "分": 60, "小时": 3600, "钟头": 3600, "天": 86400}
PER_HOUR = {"凌晨": 6, "清晨": 7, "早上": 8, "早晨": 8, "上午": 9, "中午": 12, "下午": 15, "傍晚": 18, "晚上": 20, "夜里": 22, "半夜": 23}
DAY_PER = {"明早": "早上", "明晨": "清晨", "明晚": "晚上", "今早": "早上", "今晨": "清晨", "今晚": "晚上"}

REL = re.compile(rf"(?:(?P<half>半)\s*个?\s*小时|(?P<n>{NUM})\s*个半\s*小时|(?P<m>{NUM})\s*(?P<u>秒钟?|分钟|分|个?小时|钟头|天))\s*(?:之后|以后|后)")
DAY = re.compile(r"(?P<day>大后天|后天|明天|明日|明早|明晨|明晚|今天|今日|今早|今晨|今晚|(?P<nx>下)?(?:周|星期|礼拜)(?P<wd>[一二三四五六日天])|(?P<mo>\d{1,2})月(?P<dd>\d{1,2})[日号])")
PER = re.compile(r"(?P<per>凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)")
TM = re.compile(rf"(?P<h>{NUM})\s*[点时]\s*(?:(?P<half>半)|(?P<min>[0-9]{{1,2}}|[零〇一二三四五六七八九十]+)\s*分?)?")
TM_COLON = re.compile(r"(?P<h>\d{1,2})\s*[:：]\s*(?P<min>\d{2})")
REMIND = re.compile(r"提醒(?:一下|一声|下)?(?:我|大家)?|(?:叫|喊|通知|告诉)我")
REMEMBER = re.compile(r"^(?:好的?[，,]?|那|你|请|麻烦|帮我|给我|要|得|先|能不能|可以|都)*\s*(?:记住|记着|记一下|记下来|记下|牢记)\s*[:：,，\s]*(?P<t>.+)$", re.S)


def cn_num(s: str) -> int | None:
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s:
        return None
    if "十" in s:
        a, _, b = s.partition("十")
        tens = CN.get(a, 1) if a else 1
        ones = CN.get(b, 0) if b else 0
        return tens * 10 + ones
    n = 0
    for ch in s:
        if ch not in CN:
            return None
        n = n * 10 + CN[ch]
    return n


def _mk(y, mo, d, h, mi) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def _strip(text: str, spans: list[tuple[int, int]]) -> str:
    for a, b in sorted(spans, reverse=True):
        text = text[:a] + " " + text[b:]
    return text


def parse_when(text: str, now: float | None = None) -> tuple[float | None, str]:
    """认出文字里的时间，返回（到点的时间戳或 None，去掉时间说法后的文字）。"""
    now = now or time.time()
    m = REL.search(text)
    if m:
        if m["half"]:
            secs = 1800
        elif m["n"]:
            secs = (cn_num(m["n"]) or 0) * 3600 + 1800
        else:
            n = cn_num(m["m"]) or 0
            raw = m["u"]
            u = "小时" if "小时" in raw else "钟头" if "钟头" in raw else "天" if "天" in raw else "秒" if raw.startswith("秒") else "分"
            secs = n * UNIT_SECS[u]
        return (now + secs if secs > 0 else None), _strip(text, [m.span()])
    spans, lt = [], time.localtime(now)
    day = DAY.search(text)
    per = PER.search(text)
    tm = TM_COLON.search(text) or TM.search(text)
    if not (day or per or tm):
        return None, text
    y, mo, d = lt.tm_year, lt.tm_mon, lt.tm_mday
    date_set = False
    perword = per["per"] if per else None
    if day:
        spans.append(day.span())
        w = day["day"]
        perword = perword or DAY_PER.get(w)
        off = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明早": 1, "明晨": 1, "明晚": 1}.get(w)
        if off is not None:
            t = time.localtime(now + off * 86400)
            y, mo, d, date_set = t.tm_year, t.tm_mon, t.tm_mday, True
        elif w in ("今天", "今日", "今早", "今晨", "今晚"):
            date_set = True
        elif day["wd"]:
            wd = WEEK[day["wd"]]
            if day["nx"]:
                base = now + ((7 - lt.tm_wday) + wd) * 86400
            else:
                base = now + ((wd - lt.tm_wday) % 7) * 86400
            t = time.localtime(base)
            y, mo, d, date_set = t.tm_year, t.tm_mon, t.tm_mday, True
        elif day["mo"]:
            mo, d, date_set = int(day["mo"]), int(day["dd"]), True
            if _mk(y, mo, d, 23, 59) < now:
                y += 1
    if per:
        spans.append(per.span())
    hour = minute = None
    if tm:
        spans.append(tm.span())
        hour = cn_num(tm["h"])
        if "half" in tm.groupdict() and tm["half"]:
            minute = 30
        else:
            minute = cn_num(tm["min"]) if tm["min"] else 0
        if hour is None or hour > 24 or minute is None or minute > 59:
            return None, text
        if perword in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
            hour += 12
        elif perword == "中午" and hour < 10:
            hour += 12
        elif perword in ("凌晨", "半夜") and hour == 12:
            hour = 0
    elif perword:
        hour, minute = PER_HOUR[perword], 0
    else:  # 只有日期：默认当天上午九点
        hour, minute = 9, 0
    if hour == 24:
        hour = 23
        minute = 59
    due = _mk(y, mo, d, hour, minute)
    if not date_set:  # 没说哪天：取下一次到这个钟点
        if due <= now and tm and hour < 12 and not perword:
            due = _mk(y, mo, d, hour + 12, minute)
        if due <= now:
            due += 86400
    elif day and due <= now and day["day"] in ("今天", "今日") and not tm and not per:
        due += 86400
    return due, _strip(text, spans)


def clean_task(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" ，,。.！!~～：:、")
    s = re.sub(r"^(?:请|麻烦|帮我|给我|记得|到时候|的时候|时|再|去|一下|吧)+", "", s).strip(" ，,。.！!~～：:、")
    s = re.sub(r"(?:吧|哦|啊|呀|哈|谢谢|拜托|可以吗|好吗)+[。.！!~～]*$", "", s).strip(" ，,。.！!~～：:、")
    return s


def parse_request(text: str, now: float | None = None) -> dict | None:
    """认「记住……」和提醒。返回 None（不是）/ {kind:"remember", text} / {kind:"remind", due:时间戳或None, text}。"""
    t = (text or "").strip()
    if not t or len(t) > 300:
        return None
    r = REMIND.search(t)
    if r:
        due, rest = parse_when(t, now)
        # 去掉触发词，留下要提醒的事
        m = REMIND.search(rest)
        after = rest[m.end():] if m else rest
        before = rest[:m.start()] if m else ""
        task = clean_task(after) or clean_task(before)
        if due is None and "提醒" not in r.group(0):
            return None  # 「叫我妈」之类，没有时间就不是提醒
        return {"kind": "remind", "due": due, "text": task}
    m = REMEMBER.match(t)
    if m:
        body = m["t"].strip()
        if body and not re.search(r"[吗么嘛]\s*[?？]?$|[?？]$", body) and len(body) >= 2:
            return {"kind": "remember", "text": body[:200]}
    return None


def when_text(ts: float) -> str:
    lt, now = time.localtime(ts), time.localtime()
    day = "今天" if lt.tm_yday == now.tm_yday and lt.tm_year == now.tm_year else time.strftime("%m月%d日", lt)
    return f"{day} {time.strftime('%H:%M', lt)}"


class Reminders:
    def __init__(self, db):
        self.db = db

    def add(self, platform: str, chat_type: str, chat_id: str, account_id: str, user_id: str, user_name: str, text: str, due: float) -> int:
        return self.db.x("""INSERT INTO reminders (platform, chat_type, chat_id, account_id, user_id, user_name, text, due, created, status)
                            VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
                         (platform, chat_type, chat_id, account_id, user_id, user_name, text, due, time.time())).lastrowid

    def count(self, platform: str, user_id: str) -> int:
        return self.db.x("SELECT COUNT(*) FROM reminders WHERE platform=? AND user_id=? AND status='pending'", (platform, user_id)).fetchone()[0]

    def due(self, now: float) -> list[dict]:
        return self.db.rows("SELECT * FROM reminders WHERE status='pending' AND due<=? ORDER BY due", (now,))

    def mark(self, rid: int, status: str):
        self.db.x("UPDATE reminders SET status=? WHERE id=?", (status, rid))

    def listing(self, status: str = "pending") -> list[dict]:
        if status == "all":
            return self.db.rows("SELECT * FROM reminders ORDER BY due DESC LIMIT 300")
        return self.db.rows("SELECT * FROM reminders WHERE status=? ORDER BY due", (status,))

    def delete(self, rid: int):
        self.db.x("DELETE FROM reminders WHERE id=?", (rid,))

    def forget_user(self, platform: str, user_id: str) -> int:
        return self.db.x("DELETE FROM reminders WHERE platform=? AND user_id=?", (platform, user_id)).rowcount
