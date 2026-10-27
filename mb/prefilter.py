"""先用代码筛一遍，再问守门员：只用于主动插话。

没人@她的消息，如果明摆着没有话可接（纯表情、纯链接、签到、别的机器人的指令……），就不必花一次守门员调用去问。
被@、被回复照旧必回，不走这里。规则存在 kv 里，面板可改；默认几条内置。
"""
import re

EMOJI_ONLY = re.compile(
    r"^(?:\s|\[[^\[\]]{1,8}\]|\[图片\]|\[表情\]|[\U0001F000-\U0001FAFF←-⇿⌀-➿⬀-⯿️‍]|[~～!！?？。.…,，]+)+$")
LINK_ONLY = re.compile(r"^\s*(?:(?:https?://|www\.)\S+\s*)+$", re.I)

DEFAULT_RULES = [
    {"id": "emoji", "name": "纯表情", "kind": "builtin", "pattern": "emoji", "on": True},
    {"id": "link", "name": "纯链接", "kind": "builtin", "pattern": "link", "on": True},
    {"id": "checkin", "name": "签到打卡", "kind": "regex", "pattern": r"^\s*(?:每日)?(?:签到|打卡)\s*[!！~～。.]*\s*$", "on": True},
    {"id": "command", "name": "以 / 或 # 开头的指令（多半是别的机器人的）", "kind": "regex", "pattern": r"^\s*[/#／＃]\S", "on": True},
]  # 别的机器人发的消息改由设置「别的机器人的消息跳过守门员」控制


def rules(db) -> list[dict]:
    r = db.kv("prefilter_rules")
    return r if isinstance(r, list) else [dict(x) for x in DEFAULT_RULES]


def check(rule_list: list[dict], text: str, sender_is_bot: bool = False) -> str | None:
    """命中返回规则名，没命中返回 None。"""
    t = (text or "").strip()
    if not t:
        return "空消息"
    for r in rule_list:
        if not r.get("on", True):
            continue
        if r.get("kind") == "builtin":
            p = r.get("pattern")
            if (p == "emoji" and EMOJI_ONLY.match(t)) or (p == "link" and LINK_ONLY.match(t)) or (p == "bot" and sender_is_bot):
                return r.get("name") or p
        else:
            try:
                if re.search(r["pattern"], t):
                    return r.get("name") or r["pattern"]
            except (re.error, KeyError, TypeError):
                continue
    return None
