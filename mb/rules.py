"""角色分配规则：账号此刻在某个群里用哪个角色。

规则 = 若干条件（都可不填）+ 结果（固定一个角色，或在几个角色间轮换）。
- 条件：平台、账号、群（多选）或群分组、日期区间、星期、每天的时段（可跨午夜）。
- 跨午夜的时段，过了午夜那一截算前一天的（「周五 22:00-02:00」包含周六凌晨一点）。
- 规则按排序判定，排上面的先命中；都没命中用账号的默认角色。
- 「换人设」指令是临时覆盖，到期回到规则表的安排。
"""
import random
from datetime import date, datetime, timedelta

EPOCH = date(2026, 1, 1)
WEEK = "一二三四五六日"


def hm(s: str) -> int | None:
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _day_of(rule: dict, t: datetime) -> date | None:
    """时段条件下，这一刻算哪一天；不在时段里返回 None。"""
    a, b = hm(rule.get("time_from") or ""), hm(rule.get("time_to") or "")
    if a is None or b is None or a == b:
        return t.date()
    now = t.hour * 60 + t.minute
    if a < b:
        return t.date() if a <= now < b else None
    if now >= a:
        return t.date()
    if now < b:
        return t.date() - timedelta(days=1)  # 跨午夜：过了 0 点那一截算前一天
    return None


def matches(rule: dict, platform: str, aid: str, chat: str | None, chat_groups: list, t: datetime) -> bool:
    if not rule.get("on", True):
        return False
    if rule.get("platforms") and platform not in rule["platforms"]:
        return False
    if rule.get("accounts") and aid not in rule["accounts"]:
        return False
    if rule.get("chats") or rule.get("chat_groups"):
        if not chat:
            return False
        if chat not in (rule.get("chats") or []) and not set(rule.get("chat_groups") or []) & set(chat_groups or []):
            return False
    day = _day_of(rule, t)
    if day is None:
        return False
    if rule.get("weekdays") and day.weekday() not in rule["weekdays"]:
        return False
    if rule.get("date_from") and day.isoformat() < rule["date_from"]:
        return False
    if rule.get("date_to") and day.isoformat() > rule["date_to"]:
        return False
    return True


def pick(rule: dict, t: datetime) -> str | None:
    if rule.get("mode") != "rotate":
        return rule.get("role") or None
    roles = [r for r in rule.get("roles") or [] if r]
    if not roles:
        return None
    day = _day_of(rule, t) or t.date()
    idx = (day - EPOCH).days
    if rule.get("every") == "slot":
        slot = max(15, int(rule.get("slot_minutes") or 120))
        idx = idx * (1440 // slot + 1) + (t.hour * 60 + t.minute) // slot
    if rule.get("order") == "random":
        return random.Random(f"{rule.get('id')}:{idx}").choice(roles)  # 同一时段内固定，不会每条消息都变
    return roles[idx % len(roles)]


def specificity(rule: dict) -> tuple:
    """自动排序：指定日期 > 星期加时段 > 每天时段 > 不限；同档里范围越小越靠前。"""
    has_time = hm(rule.get("time_from") or "") is not None and hm(rule.get("time_to") or "") is not None
    if rule.get("date_from") or rule.get("date_to"):
        tier = 4
    elif rule.get("weekdays") and has_time:
        tier = 3
    elif rule.get("weekdays"):
        tier = 2
    elif has_time:
        tier = 1
    else:
        tier = 0
    scope = 3 if rule.get("chats") else 2 if rule.get("chat_groups") else 1 if rule.get("accounts") else 0
    return (-tier, -scope, -(1 if rule.get("platforms") else 0))


class Duty:
    def __init__(self, db):
        self.db = db

    def rules(self) -> list[dict]:
        return self.db.docs("rule")

    def overrides(self) -> dict:
        return self.db.kv("role_overrides") or {}

    def resolve(self, account: dict, chat: str | None, t: datetime | None = None, chat_doc: dict | None = None,
                rules=None, use_override=True, ov: dict | None = None) -> tuple[str | None, str]:
        """返回（角色 id, 来自哪里：override / rule:<id> / default）。rules/chat_doc/ov 传进来时不再读库。"""
        t = t or datetime.now()
        if use_override and chat:
            o = (self.overrides() if ov is None else ov).get(f"{account['id']}|{chat}")
            if o and o.get("role") and o.get("until", 0) > t.timestamp():
                return o["role"], "override"
        groups = (chat_doc if chat_doc is not None else (self.db.doc("chat", chat) if chat else None) or {}).get("cgroups") or []
        for r in rules if rules is not None else self.rules():
            if matches(r, account["platform"], account["id"], chat, groups, t):
                rid = pick(r, t)
                if rid:
                    return rid, f"rule:{r['id']}"
        return account.get("role_id") or None, "default"

    def set_override(self, aid: str, chat: str, until: float, role: str | None = None, persona: str | None = None):
        o = self.overrides()
        o[f"{aid}|{chat}"] = {"role": role, "persona": persona, "until": until}
        now = datetime.now().timestamp()
        self.db.set_kv("role_overrides", {k: v for k, v in o.items() if v.get("until", 0) > now})

    def clear_override(self, aid: str, chat: str):
        o = self.overrides()
        o.pop(f"{aid}|{chat}", None)
        self.db.set_kv("role_overrides", o)

    def override(self, aid: str, chat: str) -> dict | None:
        o = self.overrides().get(f"{aid}|{chat}")
        return o if o and o.get("until", 0) > datetime.now().timestamp() else None

    # ---------- 预览 ----------
    def timeline(self, account: dict, chat: str | None, start: datetime, minutes: int, step: int = 15) -> list[dict]:
        """按 step 分钟采样，合并成 [{start, end, role, src}]。临时覆盖只算到它到期为止。"""
        rules, chat_doc, ov = self.rules(), (self.db.doc("chat", chat) if chat else None) or {}, self.overrides()
        segs = []
        t = start
        end = start + timedelta(minutes=minutes)
        while t < end:
            rid, src = self.resolve(account, chat, t, chat_doc, rules, ov=ov)
            if segs and segs[-1]["role"] == rid and segs[-1]["src"] == src:
                segs[-1]["end"] = (t + timedelta(minutes=step)).isoformat(timespec="minutes")
            else:
                segs.append({"start": t.isoformat(timespec="minutes"), "end": (t + timedelta(minutes=step)).isoformat(timespec="minutes"),
                             "role": rid, "src": src})
            t += timedelta(minutes=step)
        return segs

    def now_next(self, account: dict, chat: str | None) -> dict:
        now = datetime.now().replace(second=0, microsecond=0)
        rid, src = self.resolve(account, chat, now)
        start = now - timedelta(minutes=now.minute % 5)
        for seg in self.timeline(account, chat, start, 8 * 1440, step=5)[1:]:
            if seg["role"] != rid:
                return {"role": rid, "src": src, "next_at": seg["start"], "next_role": seg["role"]}
        return {"role": rid, "src": src, "next_at": None, "next_role": None}

    def check(self, accounts: list[dict], chats: list[dict], days: int = 28, rules: list[dict] | None = None) -> list[dict]:
        """找问题：永远轮不到的规则、过期的规则、没有任何角色的时段。传入 rules 时不读库（可放到后台线程算）。"""
        rules = [r for r in (self.rules() if rules is None else rules) if r.get("on", True)]
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        won = {r["id"]: False for r in rules}
        gaps = []
        out = []
        for r in rules:
            if r.get("date_to") and r["date_to"] < today.date().isoformat():
                out.append({"level": "warn", "rule": r["id"], "text": f"规则「{r.get('name') or '未命名'}」的日期已经过了"})
                won[r["id"]] = True
            elif r.get("date_from") and r["date_from"] > (today + timedelta(days=days)).date().isoformat():
                won[r["id"]] = True  # 还没到日子，不算轮不到
        for a in accounts:
            for c in [d for d in chats if d.get("platform") == a["platform"]] or [None]:
                key = c["id"] if c else None
                gap_n = 0
                for i in range(days * 48):
                    t = today + timedelta(minutes=30 * i)
                    groups = (c or {}).get("cgroups") or []
                    for r in rules:
                        if matches(r, a["platform"], a["id"], key, groups, t) and pick(r, t):
                            won[r["id"]] = True
                            break
                    else:
                        if not a.get("role_id"):
                            gap_n += 1
                if gap_n:
                    gaps.append(f"账号「{a.get('name') or a.get('qq') or a['id']}」在「{(c or {}).get('name') or key or '私聊'}」有 {gap_n / 2:.0f} 小时没有任何角色（没规则命中、也没设默认角色）")
        for r in rules:
            if not won[r["id"]]:
                out.append({"level": "warn", "rule": r["id"],
                            "text": f"规则「{r.get('name') or '未命名'}」未来 {days} 天里永远轮不到（被排在上面的规则全挡住了，或条件对不上任何账号和群）"})
        out += [{"level": "bad", "text": g} for g in gaps[:20]]
        return out


def describe(rule: dict, names: dict) -> str:
    """一句话描述规则的条件，给面板列表用。names: {roles, accounts, chats, cgroups} → {id: 名字}"""
    parts = []
    if rule.get("platforms"):
        parts.append("/".join({"qq": "QQ", "wechat": "微信", "discord": "Discord"}.get(p, p) for p in rule["platforms"]))
    if rule.get("accounts"):
        parts.append("账号 " + "、".join(names["accounts"].get(a, a) for a in rule["accounts"]))
    if rule.get("chats") or rule.get("chat_groups"):
        parts.append("群 " + "、".join([names["chats"].get(c, c) for c in rule.get("chats") or []] +
                                       [f"[{names['cgroups'].get(g, g)}]" for g in rule.get("chat_groups") or []]))
    if rule.get("date_from") or rule.get("date_to"):
        parts.append(f"{rule.get('date_from') or '…'} 至 {rule.get('date_to') or '…'}")
    if rule.get("weekdays"):
        parts.append("周" + "".join(WEEK[d] for d in sorted(rule["weekdays"])))
    if rule.get("time_from") and rule.get("time_to"):
        parts.append(f"{rule['time_from']}-{rule['time_to']}")
    return "，".join(parts) or "任何时候"
