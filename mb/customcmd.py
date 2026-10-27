"""自定义指令：用户在面板里定义「群里有人这样说，就做这件事」。不提供脚本或代码执行。

每条指令 = 触发（要不要先 @ 她；完全相等 / 包含 / 正则）+ 动作（固定回复 / 让角色回答 / 执行内置指令 / 随机一句）
+ 谁能用（沿用管理指令的权限选项，另加「所有人」）+ 在哪生效（平台、角色、群，空 = 全部）+ 冷却。
匹配顺序：自定义指令 → 内置管理指令 → 正常聊天。默认一条都没有。存在 kv「custom_commands」里，面板可增删改、排序、启停、导入导出。
"""
import random
import re
import time

MATCHES = {"equal": "完全相等", "contains": "包含", "regex": "正则"}
ACTIONS = {"reply": "固定回复", "ask": "让角色回答", "builtin": "执行内置指令", "random": "随机发一句"}
PERM_MODES = {"follow": "跟随这条内置指令的权限", "all": "所有人", "owner": "只有我（管理员）", "people": "指定的人", "pgroup": "某个分组", "admins": "群主和群管理员"}
BUILTIN = ["mute", "unmute", "persona", "persona_back", "proactive", "eagerness", "status", "recall", "forget", "forget_me"]


def rules(db) -> list[dict]:
    r = db.kv("custom_commands")
    return r if isinstance(r, list) else []


def clean(r: dict, i: int = 0) -> dict:
    """把面板传来的一条规则整理成标准样子；不合法就抛 ValueError（带中文原因）。"""
    name = (r.get("name") or "").strip()[:40] or f"指令{i + 1}"
    match = r.get("match") if r.get("match") in MATCHES else "equal"
    pattern = str(r.get("pattern") or "").strip()
    if not pattern:
        raise ValueError(f"「{name}」没有填触发的文字")
    if match == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"「{name}」的正则不对：{e}")
    action = r.get("action") if r.get("action") in ACTIONS else "reply"
    mode = (r.get("perm") or {}).get("mode")
    if mode not in PERM_MODES or (mode == "follow" and action != "builtin"):  # 没填：执行内置指令默认跟随它自己的权限，别的动作默认所有人
        mode = "follow" if action == "builtin" else "all"
    out = {"id": r.get("id") or f"cc_{int(time.time() * 1000) % 10**9}_{i}", "name": name, "on": bool(r.get("on", True)),
           "at": bool(r.get("at", True)), "match": match, "pattern": pattern, "action": action,
           "text": str(r.get("text") or ""), "prompt": str(r.get("prompt") or "").strip(), "builtin": r.get("builtin") or "mute",
           "arg": str(r.get("arg") or "").strip(), "lines": [str(x).strip() for x in (r.get("lines") or []) if str(x).strip()],
           "perm": {"mode": mode,
                    "people": list((r.get("perm") or {}).get("people") or []), "pgroup": (r.get("perm") or {}).get("pgroup") or ""},
           "platforms": list(r.get("platforms") or []), "roles": list(r.get("roles") or []), "chats": list(r.get("chats") or []),
           "cooldown": max(0, int(r.get("cooldown") or 0))}
    if action == "reply" and not out["text"].strip():
        raise ValueError(f"「{name}」选了固定回复，但没写回复的文字")
    if action == "ask" and not out["prompt"]:
        raise ValueError(f"「{name}」选了让角色回答，但没写给她的提示")
    if action == "builtin" and out["builtin"] not in BUILTIN:
        raise ValueError(f"「{name}」要执行的内置指令不认识：{out['builtin']}")
    if action == "random" and not out["lines"]:
        raise ValueError(f"「{name}」选了随机发一句，但一句都没写")
    return out


def matches(rule: dict, text: str) -> bool:
    t = (text or "").strip()
    p = rule.get("pattern", "")
    if not t or not p:
        return False
    m = rule.get("match")
    if m == "equal":
        return t.lower() == p.lower()
    if m == "contains":
        return p.lower() in t.lower()
    try:
        return re.search(p, t) is not None
    except re.error:
        return False


def in_scope(rule: dict, platform: str, role_id: str, chat_key: str) -> bool:
    return ((not rule.get("platforms") or platform in rule["platforms"]) and (not rule.get("roles") or role_id in rule["roles"])
            and (not rule.get("chats") or chat_key in rule["chats"]))


def pick(rule: dict) -> str:
    return random.choice(rule.get("lines") or [""])
