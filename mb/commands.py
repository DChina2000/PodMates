"""管理指令：@她 + 固定说法，由程序识别。识别不了的按普通聊天回。"""
import re

PATTERNS = [
    ("mute", re.compile(r"^(?:闭嘴|安静|别说话|禁言)\s*(\d+)?\s*(?:分钟|min)?\s*$")),
    ("unmute", re.compile(r"^(?:恢复|解除闭嘴|可以说话了|说话吧)\s*$")),
    ("persona_back", re.compile(r"^(?:换回来?|换回去|取消换人设)\s*$")),
    ("persona", re.compile(r"^(?:换人设|换角色|换成|换)\s*[:：]?\s*(.+?)\s*$")),
    ("proactive", re.compile(r"^(?:本群)?插话\s*(开|关|打开|关闭|开启)\s*$")),
    ("eagerness", re.compile(r"^插话力度\s*[:：]?\s*([1-5])\s*$")),
    ("status", re.compile(r"^状态\s*$")),
    ("recall", re.compile(r"^撤回(?:上一条)?\s*$")),
    ("forget_me", re.compile(r"^忘掉\s*我\s*$")),  # 「忘掉 我」中间多个空格也是忘掉我，不是忘掉叫「我」的东西
    ("forget", re.compile(r"^忘掉\s*(.+?)\s*$")),
    ("help", re.compile(r"^(?:指令|帮助)\s*$")),
]

# 私聊指令：只认用户卡本人（「指令」「帮助」谁都能问）
PRIVATE_PATTERNS = [
    ("replace_able", re.compile(r"^换能换的$")),
    ("replace", re.compile(r"^换$")),
    ("vision_too", re.compile(r"^看图也换$")),
    ("snooze", re.compile(r"^不换(?:\s*(\d+)\s*天)?$")),
    ("never", re.compile(r"^(?:一直不换|别再提)$")),
    ("swap", re.compile(r"^换模型\s+(\S+)\s+(\S+)$")),
    ("check", re.compile(r"^查更新$")),
    ("help", re.compile(r"^(?:指令|帮助)$")),
]

# 「指令」总表：(指令名, 说法, 用途)。群里的按 cmd_perms 列，私聊的只列给用户卡本人
GROUP_HELP = [
    ("mute", "闭嘴 N 分钟", "让她安静 N 分钟（不写就是 30 分钟）"),
    ("unmute", "恢复", "让她恢复说话"),
    ("persona", "换 角色名或人设名（可带时长，如「换 某某 2小时」）", "临时换角色或人设，不写时长就到当天结束"),
    ("persona", "换回来", "按排班换回来"),
    ("proactive", "本群插话 开 / 本群插话 关", "打开或关闭本群的主动插话"),
    ("eagerness", "插话力度 1～5", "调本群插话的多少（1 很少，5 话痨）"),
    ("status", "状态", "看她现在的状态"),
    ("recall", "撤回", "撤回她上一条消息"),
    ("forget", "忘掉 某事", "删掉关于某事的记忆（删之前备份过，面板记忆页能撤销）"),
    ("forget_me", "忘掉我", "删掉关于你自己的记忆和你挂的提醒"),
]
PRIVATE_HELP = [
    ("换", "把最近一条新模型提醒里的旧模型换成新的（先真调一次确认能用）"),
    ("换能换的", "上一次因为新模型看不了图没换成时，只换看图以外的几处"),
    ("看图也换", "新模型能看图时，把看图也换成它"),
    ("不换 / 不换 N 天", "先不换，过几天再提醒（不写天数就按设置里的）"),
    ("一直不换 / 别再提", "不再提醒这个新模型"),
    ("换模型 旧 新", "把这家服务商上的旧模型换成新模型"),
    ("查更新", "现在查一次新模型和新版本"),
]


def parse_private(text: str) -> tuple[str, tuple] | None:
    t = text.strip()
    for name, rx in PRIVATE_PATTERNS:
        m = rx.match(t)
        if m:
            return name, m.groups()
    return None


def parse(text: str) -> tuple[str, str] | None:
    t = text.strip()
    for name, rx in PATTERNS:
        m = rx.match(t)
        if m:
            return name, (m.group(1) or "") if m.groups() else ""
    return None


def allowed(cmd: str, perms: dict, *, is_owner: bool, person: dict | None, sender_role: str) -> bool:
    """模式：owner 只有我（默认）/ people 我加指定的人 / pgroup 我加某分组 / admins 我加群主和群管理员。"""
    if cmd in ("forget_me", "help") or is_owner:
        return True
    conf = (perms or {}).get("persona" if cmd == "persona_back" else cmd) or {}
    mode = conf.get("mode", "owner")
    if mode == "all":  # 自定义指令的「所有人」
        return True
    if mode == "people":
        return bool(person and person["id"] in (conf.get("people") or []))
    if mode == "pgroup":
        return bool(person and conf.get("pgroup") in (person.get("groups") or []))
    if mode == "admins":
        return sender_role in ("owner", "admin")
    return False


def help_text(perms: dict, *, is_owner: bool, person: dict | None, sender_role: str, private: bool, user_card: bool = False) -> str:
    """这个人在这里能用的指令清单：群里按 cmd_perms 只列他有权限的；私聊只有用户卡本人才有指令可列。"""
    if private:
        lines = [f"{say}：{use}" for say, use in PRIVATE_HELP] if user_card else []
    else:
        lines = [f"{say}：{use}" for cmd, say, use in GROUP_HELP
                 if (not (cmd == "forget_me" and person and person.get("admin")))
                 and allowed(cmd, perms, is_owner=is_owner, person=person, sender_role=sender_role)]
    return "你能用的指令：\n" + "\n".join(lines) if lines else "你在这里没有可用的指令"


def all_rows() -> list[dict]:
    """面板上的总表：全部指令，各自在哪说、谁能用。"""
    perm = "按「管理指令权限」设置（默认只有用户本人）"
    rows = [dict(say=say, use=use, where="群里 @她 说", who="所有人（人物表里的管理员除外）" if cmd == "forget_me" else perm) for cmd, say, use in GROUP_HELP]
    rows.append(dict(say="指令 / 帮助", use="列出你在这里能用的指令", where="群里 @她 说，或私聊她", who="所有人"))
    rows += [dict(say=say, use=use, where="私聊她", who="只有用户卡本人") for say, use in PRIVATE_HELP]
    return rows
