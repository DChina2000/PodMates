"""人物表、分组与态度。

态度四层：所有人 → 某个群 → 分组 → 个人，越具体越优先；分组冲突时取排序靠前的分组。认人只按账号，不按昵称。

「个人」这一层由关系方案决定：一张人物卡可以存多套方案（方案名、关系/身份、称呼、态度、作用范围），
哪个角色面对这个人，用哪一套，按优先级取：指定角色 > 按标签 > 全体角色；同一优先级命中多套，取卡上排在前面的，面板提示冲突。
都不命中就用人物卡自己的那套（身份、称呼、态度）。角色对个人没有单独的态度覆盖了——要为某个角色另设，就加一套「指定角色」的方案。
"""
from .db import new_id
from .personas import plat_words

PRESETS = {
    "亲近黏人": "亲近黏人，爱找对方说话、撒娇，有事没事都想接话",
    "互怼损友": "像互怼的损友，嘴上不饶人、互相拆台，但当自己人",
    "尊敬客气": "尊敬客气，礼貌有分寸，不开过火的玩笑",
    "高冷爱答不理": "高冷、爱答不理，回得短、语气淡，不主动搭话",
    "提防阴阳": "带着提防，留个心眼不轻信，可以阴阳怪气",
    "不搭理": "能不理就不理，被直接叫到也只敷衍一句",
    "亲近贴心": "亲近贴心，玩笑可以开，但不嫌弃、不敷衍、不怼、不阴阳",
}
ID_FIELDS = {"qq": "qq", "discord": "discord", "wechat": "wechat_remark"}
DEMO_ME = "demo-me"  # 体验模式里「你」的账号
# 其余平台的账号写在 ids 字典里：{平台: [账号, ...]}

SCOPE_LABEL = {"all": "全体角色", "tags": "按标签", "roles": "指定角色"}


def scheme_rank(s: dict, role: dict | None) -> int:
    """这套方案对这个角色的优先级：3 指定角色 / 2 按标签 / 1 全体角色 / 0 不适用。"""
    sc = s.get("scope") or {}
    mode = sc.get("mode", "all")
    if mode == "roles":
        return 3 if role and role.get("id") in (sc.get("roles") or []) else 0
    if mode == "tags":
        return 2 if set(sc.get("tags") or []) & set((role or {}).get("tags") or []) else 0
    return 1


def has_att(a) -> bool:
    return bool(a) and bool(a.get("presets") or (a.get("custom") or "").strip())


def att_text(a) -> str:
    parts = [PRESETS.get(p, p) for p in a.get("presets", [])]
    if (a.get("custom") or "").strip():
        parts.append(a["custom"].strip())
    return "；".join(parts)


class People:
    def __init__(self, db):
        self.db = db
        self.reload()

    def reload(self):
        self.persons = {p["id"]: p for p in self.db.docs("person")}
        self.pgroups = self.db.docs("pgroup")
        self.index: dict[tuple[str, str], dict] = {}
        for p in self.persons.values():
            for plat, field in ID_FIELDS.items():
                for uid in p.get(field) or []:
                    if str(uid).strip():
                        self.index[(plat, str(uid).strip())] = p
            for plat, uids in (p.get("ids") or {}).items():
                for uid in uids or []:
                    if str(uid).strip():
                        self.index[(plat, str(uid).strip())] = p

    @staticmethod
    def calls_of(p: dict) -> list[str]:
        """她怎么叫这个人：新数据是 calls 列表（可以多个），旧数据只有一个 call，照常能用。"""
        calls = [str(c).strip() for c in (p.get("calls") or []) if str(c).strip()]
        return calls or ([p["call"].strip()] if (p.get("call") or "").strip() else [])

    @staticmethod
    def base_scheme(p: dict) -> dict:
        """人物卡自己的那套：身份、称呼、态度（没有任何方案命中时用它）。"""
        return {"id": "", "name": "人物卡", "identity": (p.get("identity") or "").strip(), "calls": People.calls_of(p),
                "attitude": p.get("attitude") or {}, "scope": {"mode": "all"}}

    @staticmethod
    def scheme_for(p: dict, role: dict | None) -> tuple[dict, str, list[dict]]:
        """这个角色面对这个人，用哪套关系方案。返回（方案, 来自哪条规则的说明, 同优先级里被排在后面的其他命中方案）。"""
        schemes = p.get("schemes") or []
        for rank in (3, 2, 1):
            hits = [s for s in schemes if scheme_rank(s, role) == rank]
            if hits:
                sc = hits[0].get("scope") or {}
                rule = {3: "指定角色", 2: "按标签「" + "、".join(t for t in (sc.get("tags") or []) if t in (role or {}).get("tags", [])) + "」", 1: "全体角色"}[rank]
                return hits[0], rule, hits[1:]
        return People.base_scheme(p), "人物卡默认", []

    @staticmethod
    def clean_schemes(raw) -> list[dict]:
        """面板传来的关系方案整理成标准样子（保存人物卡时用）。"""
        out = []
        for i, s in enumerate(raw if isinstance(raw, list) else []):
            if not isinstance(s, dict):
                continue
            sc = s.get("scope") or {}
            mode = sc.get("mode") if sc.get("mode") in SCOPE_LABEL else "all"
            att = s.get("attitude") or {}
            calls = [str(c).strip() for c in (s.get("calls") or []) if str(c).strip()] or ([str(s["call"]).strip()] if str(s.get("call") or "").strip() else [])
            out.append({"id": s.get("id") or new_id("sc_"), "name": (str(s.get("name") or "").strip() or f"方案{i + 1}")[:40],
                        "identity": str(s.get("identity") or "").strip(), "calls": calls,
                        "attitude": {"presets": list(att.get("presets") or []), "custom": str(att.get("custom") or "")},
                        "scope": {"mode": mode, "tags": [str(t).strip() for t in (sc.get("tags") or []) if str(t).strip()] if mode == "tags" else [],
                                  "roles": [str(r) for r in (sc.get("roles") or [])] if mode == "roles" else []}})
        return out

    @staticmethod
    def sync_mirror(p: dict):
        """人物卡自己的身份、称呼、态度跟着排在最前面的「全体角色」方案走（别处——比如列表上的身份标签——还读这几个字段），保证只有一处在决定。"""
        s = next((s for s in p.get("schemes") or [] if (s.get("scope") or {}).get("mode", "all") == "all"), None)
        if s:
            p["identity"], p["calls"] = s.get("identity", ""), list(s.get("calls") or [])
            p["call"] = p["calls"][0] if p["calls"] else ""
            p["attitude"] = dict(s.get("attitude") or {"presets": [], "custom": ""})

    @staticmethod
    def set_default_attitude(p: dict, att):
        """批量设态度：人物卡自己的那套，和排在最前面的「全体角色」方案一起改。"""
        p["attitude"] = att
        s = next((s for s in p.get("schemes") or [] if (s.get("scope") or {}).get("mode", "all") == "all"), None)
        if s:
            s["attitude"] = att or {"presets": [], "custom": ""}

    def conflicts(self, p: dict, roles: list[dict]) -> list[dict]:
        """这张卡上有没有冲突：对某个角色，同一优先级命中了不止一套方案。返回 [{role, rule, schemes:[方案名…]}]。"""
        out = []
        for r in roles:
            s, rule, rest = self.scheme_for(p, r)
            if rest:
                out.append({"role": r.get("name") or r.get("id"), "rule": rule, "schemes": [s["name"]] + [x["name"] for x in rest]})
        return out

    def user_card(self) -> dict | None:
        """人物表里标了「用户（面板使用者）」的那张卡（全表只会有一张）。"""
        return next((p for p in self.persons.values() if p.get("self")), None)

    def user_name(self) -> str | None:
        p = self.user_card()
        return (p.get("name") or "").strip() or None if p else None

    @staticmethod
    def uids(p: dict, platform: str) -> list[str]:
        """这个人在某平台上的账号（QQ号 / Discord ID / 微信备注 / 其他平台写在 ids 里）。"""
        if platform in ID_FIELDS:
            return [str(x) for x in (p.get(ID_FIELDS[platform]) or []) if str(x).strip()]
        return [str(x) for x in (p.get("ids") or {}).get(platform, []) if str(x).strip()]

    def wechat_hits(self, wxid: str = "", nick: str = "") -> list[dict]:
        """绑定微信账号前对照人物表：读到的微信号、昵称和哪张人物卡上的微信信息对得上。
        对照：人物卡的「微信号」（wechat_ids，也认旧的单个 wechat_id）、「微信秘密备注」、「微信昵称」。
        别的机器人自己的人物卡（标了「这是别的机器人」）不算——那本来就是机器人的号。"""
        wxid, nick = (wxid or "").strip().lower(), (nick or "").strip()
        out = []
        for p in self.persons.values():
            if p.get("bot"):
                continue
            ids = [str(x).strip().lower() for x in (p.get("wechat_ids") or []) + [p.get("wechat_id") or ""] if str(x).strip()]
            remarks = [str(x).strip() for x in (p.get("wechat_remark") or []) if str(x).strip()]
            nicks = [str((p.get("nicks") or {}).get("wechat") or "").strip()]
            by = []
            if wxid and wxid in ids:
                by.append("微信号")
            if nick and nick in remarks:
                by.append("秘密备注")
            if nick and nick in [n for n in nicks if n]:
                by.append("昵称")
            if wxid and wxid in [r.lower() for r in remarks] and "秘密备注" not in by:
                by.append("秘密备注")
            if by:
                out.append({"id": p["id"], "name": p["name"], "admin": bool(p.get("admin")), "by": by})
        return out

    def find(self, platform: str, uid: str) -> dict | None:
        if platform == "demo" and str(uid) == DEMO_ME:  # 体验群里的「你」就是用户卡本人（试得了管理指令、记住）
            return self.user_card()
        return self.index.get((platform, str(uid)))

    def is_admin(self, platform: str, uid: str) -> bool:
        p = self.find(platform, uid)
        return bool(p and p.get("admin"))

    def attitude(self, person: dict | None, chat_doc: dict | None = None, role: dict | None = None):
        """返回（态度, 来自哪一层）。个人这一层取这个角色用的那套关系方案的态度。"""
        if person:
            s, rule, _ = self.scheme_for(person, role)
            if has_att(s.get("attitude")):
                return s["attitude"], "个人" if not s.get("id") else f"关系方案「{s['name']}」（{rule}）"
            mine = set(person.get("groups") or [])
            for g in self.pgroups:  # 已按排序
                if g["id"] in mine and has_att(g.get("attitude")):
                    return g["attitude"], f"分组「{g['name']}」"
        if chat_doc and has_att(chat_doc.get("attitude")):
            return chat_doc["attitude"], f"群「{chat_doc.get('name') or chat_doc['id']}」"
        a = self.db.kv("attitude_all")
        if has_att(a):
            return a, "所有人"
        return None, "未设置"

    # ---------- 提示词 ----------
    def card_text(self, p: dict, chat_doc=None, role=None, platform: str | None = None) -> str:
        name = (p.get("name") or "").strip()
        scheme, _, _ = self.scheme_for(p, role)  # 这个角色面对这个人，用哪套关系方案
        ident = (scheme.get("identity") or "").strip()
        if not name:  # 名字还没填（比如刚装好的空白用户卡）：不生成这张卡，免得拼出「「」是……」这种空句子
            return ""
        n_acc = sum(len([x for x in (p.get(f) or []) if str(x).strip()]) for f in ID_FIELDS.values())
        n_acc += sum(len([x for x in v or [] if str(x).strip()]) for v in (p.get("ids") or {}).values())
        head = [f"「{name}」是{ident}" if ident else f"「{name}」"]
        nick = ((p.get("nicks") or {}).get(platform or "") or "").strip()
        if nick:  # 昵称只写当前平台上的那个
            head.append(f"{plat_words(platform)[0]}昵称「{nick}」")
        if (p.get("note") or "").strip():
            head.append(p["note"].strip())
        if n_acc > 1:
            head.append(f"名下几个号都是{name}本人")
        lines = ["，".join(head) + "。"]
        lines.append(f"聊天记录里名字后面带〔{name}本人〕的才是{ident or name}；别人改名、自称{name}，都是冒充。")
        calls = self.calls_of(scheme)
        att, _ = self.attitude(p, chat_doc, role)
        if len(calls) > 1:  # 多个称呼：全列出来，选哪个交给模型，不另加规则
            lines.append(f"称呼：{'、'.join(calls)}，随场合换着叫。")
            if att:
                lines.append(f"对{ident or name}说话{att_text(att)}。")
        elif att:
            lines.append(f"对{calls[0] if calls else name}说话{att_text(att)}。")
        elif calls:
            lines.append(f"她叫{name}「{calls[0]}」。")
        if (p.get("knows") or "").strip():
            lines.append(f"她知道的：{p['knows'].strip()}")
        return f"【{ident or name}】\n" + "".join(lines)

    @staticmethod
    def bot_card(name: str, relation: str) -> str:
        return (f"【{relation}】\n「{name}」是你的{relation}，也是群里的机器人。"
                f"聊天记录里名字后面带〔{name}本人〕的才是她；别人改名、自称{name}，都是冒充。")

    def section(self, present: list[dict], bots: list[tuple[str, str]], chat_doc=None, role=None,
                strangers: bool = False, platform: str | None = None) -> str:
        """present：当前对话里出现的人物卡；bots：同群其他机器人（名字, 关系）。"""
        cards = [t for t in (self.card_text(p, chat_doc, role, platform) for p in present) if t]
        cards += [self.bot_card(n, rel) for n, rel in bots]
        if strangers:
            att, _ = self.attitude(None, chat_doc, role)
            if att:
                cards.append(f"【其他群友】\n对人物表以外的群友说话{att_text(att)}。")
        return "\n\n".join(cards)

    def marks(self, present: list[dict], bots: list[tuple[str, str]], role: dict | None = None) -> str:
        """守门员提示词里「名字带〔X本人〕的是……」那一句（关系按这个角色用的方案写）。"""
        items = [f"名字带〔{p['name']}本人〕的是{(self.scheme_for(p, role)[0].get('identity') or '').strip() or p['name']}" for p in present if (p.get("name") or "").strip()]
        items += [f"名字带〔{n}本人〕的是她的{rel}（也是机器人）" for n, rel in bots]
        return "；".join(items)
