"""发送前整理回复：去掉开头漏进来的内部标注（「主动插话（接她妈那句）：」这类），去掉括号里的动作描写（「（尾巴晃了晃）」这类），
删掉整行只是一个媒体标记的行（她照上下文里的写法编出来冒充图片的「[图片：……]」这类）。

三处都写得保守：宁可漏删，不要误删。「（含税）」「（2019 年）」「（以上仅供参考）」这类补充说明一概不动。
"""
import re

# 程序内部给回复方式起的名字（日志、面板里用）：模型有时会把它抄到回复开头
LABELS = ("主动插话", "被叫到", "戳一戳", "冷场开话题", "面板让她回", "跟复读", "由头", "定时消息", "拒绝指令", "自定义指令")
_NAME = "(?:" + "|".join(LABELS) + r")(?:[（(][^）)\n]{0,40}[）)])?"
# 两种写法：带方括号的（后面的冒号可有可无），或者直接写名字、后面必须跟冒号
_LABEL = re.compile(r"^\s*(?:[【\[]" + _NAME + r"[】\]]\s*[:：]?|" + _NAME + r"\s*[:：])\s*")


def strip_labels(text: str) -> str:
    """回复开头是「主动插话（……）：」「【被叫到】」这类内部标注，去掉它（只看整段回复的开头，每次最多去一层再看一遍）。"""
    for _ in range(3):
        new = _LABEL.sub("", text, count=1)
        if new == text:
            break
        text = new
    return text


# ---- 括号里的动作描写 ----
_BODY = r"(?:尾巴|耳朵|狐耳|猫耳|耳尖|脑袋|眼睛|嘴角|爪子|爪|背鳍|头发|翅膀|龙角|尾尖|脸颊|鼻子)"
_MOVE = r"[晃抖甩摇竖垂耷动翘卷缠绕蹭拍扫炸僵立软耸弯扬抽扇抿]"
_GESTURE = re.compile(
    r"晃了晃|晃一晃|晃晃|抖了抖|抖一下|甩了甩|摇了摇|点点头|点了点头|摇摇头|歪了歪头|歪头|眨眨眼|眨了眨眼|眯起眼|挠了挠|揉了揉|摸了摸|戳了戳|拍了拍|拍拍|"
    r"捂住|抱着|抱住|抱紧|趴在|蹭了蹭|伸出|缩了缩|扭过头|转过身|转身|低下头|抬起头|叹了口气|叹口气|吐了吐|清清嗓子|清了清嗓子|耸耸肩|耸了耸肩|摊摊手|摊了摊手|"
    r"托着腮|托腮|扶额|推了推|合上|放下|拿起|端起|脸红|撇嘴|鼓起|皱眉|皱了皱眉|瞪着|盯着|看了看|想了想|愣了愣|探出|冒出|凑过来|靠过来|竖起|垂下|耷拉|"
    r"打了个哈欠|打哈欠|伸了个懒腰|深呼吸|咳了咳|吐个|吐出|站好|坐好|坐直|眨巴")
_BAD = re.compile(r"[0-9０-９A-Za-z＃#@:：/\\]")  # 有数字、英文、网址、冒号的，不是动作描写（「（2019 年）」「（见 FAQ）」）
_SEG = r"[（(]([^（）()\n]{2,30})[）)]"
_HEAD = re.compile(r"^\s*" + _SEG + r"[ \t]*")
_TAIL = re.compile(r"[ \t]*" + _SEG + r"\s*$")
_WHOLE = re.compile(r"^\s*" + _SEG + r"\s*$")


def is_action(inner: str) -> bool:
    """括号里的话像不像一句动作描写：不长、没有数字英文冒号、至多一个逗号，并且是「身体部位 + 动作」或者明确的动作短语。"""
    s = inner.strip()
    if not (2 <= len(s) <= 30) or _BAD.search(s) or len(re.findall(r"[，,；;。！？!?]", s)) > 1:
        return False
    if re.search(_BODY + r"[^，,；;。]{0,6}" + _MOVE, s):
        return True
    return bool(_GESTURE.search(s)) and len(s) >= 3


def strip_actions(text: str) -> str:
    """去掉括号里的动作描写：整行只有这一段的，整行删掉；行首、行尾单独一段的，只去掉这一段。夹在正文中间的括号一律保留。"""
    out = []
    for line in text.split("\n"):
        cur = line
        for _ in range(6):  # 行首、行尾可能连着好几段
            m = _WHOLE.match(cur)
            if m and is_action(m.group(1)):
                cur = ""
                break
            m = _HEAD.match(cur)
            if m and is_action(m.group(1)):
                cur = cur[m.end():]
                continue
            m = _TAIL.search(cur)
            if m and is_action(m.group(1)):
                cur = cur[:m.start()]
                continue
            break
        if cur.strip() or not line.strip():  # 原来就是空行的留着；被删空的行整行去掉
            out.append(cur)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---- 编出来的图片行 ----
# 程序给上下文写的媒体标记：别人发的图「[图片：描述]」「[图片]」、她自己发的表情包「[表情包：描述]」、平台的动画表情「[表情]」
_MARK = r"\[(?:图片|表情包|表情)(?:[：:][^\[\]\n]*)?\]"
_MEDIA_LINE = re.compile(r"^\s*(?:" + _MARK + r"\s*)+$")


def strip_media(text: str) -> str:
    """整行只有媒体标记（一个或几个连着）的，整行删掉；夹在句子里的、标记后面还有别的字的，一律不动。"""
    out = [ln for ln in text.split("\n") if not _MEDIA_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
