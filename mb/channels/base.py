"""平台渠道的公共接口与统一消息结构。"""
import time
from dataclasses import dataclass, field


@dataclass
class Msg:
    platform: str
    account_id: str
    chat_type: str  # group / private
    chat_id: str
    user_id: str
    user_name: str
    text: str
    msg_id: str = ""
    chat_name: str = ""
    at_ids: list = field(default_factory=list)
    reply_to_uid: str = ""
    reply_to_id: str = ""  # 回复的是哪条消息（读文件功能用来找那条文件消息）
    reply_to_name: str = ""  # 回复的是谁（只有名字、没有账号的平台：微信的引用消息）：引擎对着群里的成员认出是谁，再填 reply_to_uid
    images: list = field(default_factory=list)
    emotes: list = field(default_factory=list)  # images 里哪几张是表情（看图时写「[表情：描述]」，受「群友发的表情也看」控制）
    shots: list = field(default_factory=list)  # images 里哪几张是截屏来的（微信表情）：看图缓存按 dhash 记，不按 sha1
    shot_keys: dict = field(default_factory=dict)  # 截屏来的那几张的缓存键（裁出来的第一帧的 dhash）：取件号 → 键
    moving: bool = False  # 截屏来的这张表情是不是动图（三帧不一样，交给看图的是拼好的三帧）
    files: list = field(default_factory=list)  # [{name, size, url|id}]：文件消息（读文件功能用）
    stickers: list = field(default_factory=list)  # 平台自带的表情包/贴纸图片地址（只给「学表情包」用，不送去看图）
    sender_role: str = ""  # owner / admin / member
    poke_target: str = ""
    dedup: str = ""  # 多个账号收到同一条消息时用来去重
    ts: float = field(default_factory=time.time)

    @property
    def room(self) -> str:
        return f"{self.platform}:{self.chat_id}"


class Muted(RuntimeError):
    """发送被平台拒了，原因是这个号在这个群里被禁言（只认真正的禁言，别的发送失败不要用它）。"""


class Channel:
    platform = ""
    supports_recall = True
    supports_more = False  # send 认 more=True（后面还有几条同一次回复的分条）：微信用它少切几次会话
    reply_only = False  # 评论区类平台：只在被@、被回复、收到私信时回，不主动插话、不冷场开话题
    overseas = False  # 海外平台默认走全局代理
    supports_image = False  # 能不能发图片（发表情包用）
    supports_at = False  # send 是否认 at=[(账号, 名字)]；不认的平台由引擎在开头写「@名字」
    supports_mention = False  # send 是否认 mention=[(账号, 名字)]：文字里写的「@名字」发成真正的 @（按先后顺序对上文字里的那几处）

    @staticmethod
    def at_prefix(uid: str, name: str) -> str:
        return f"@{name} "

    def __init__(self, engine, account: dict):
        self.engine, self.account = engine, account
        self.self_id = ""
        self.self_name = ""
        self.status = "stopped"  # stopped / starting / waiting_login / confirm / online / error
        self.detail = ""
        self.pending = None  # 等用户确认的东西（微信小号第一次绑定身份），随状态一起发给面板

    @property
    def aid(self) -> str:
        return self.account["id"]

    async def query_ban(self, chat_id: str, fresh: bool = False) -> dict | None:
        """主动查这个号在这个群里是不是被禁言（不往群里发任何东西）。返回 {member_until: 到期时间戳或 None, all: 是不是全员禁言, role: 这个号在群里的身份}；
        这个平台查不了返回 None；查的时候出错就抛出去。"""
        return None

    def set_status(self, status: str, detail: str = ""):
        self.status, self.detail = status, detail
        self.engine.emit("account", {"id": self.aid, "status": status, "detail": detail,
                                     "self_id": self.self_id, "self_name": self.self_name, "pending": self.pending})

    async def start(self):
        raise NotImplementedError

    async def stop(self):
        pass

    async def send(self, chat_type: str, chat_id: str, text: str, reply_to: str | None = None) -> list[str]:
        raise NotImplementedError

    async def send_image(self, chat_type: str, chat_id: str, data: bytes, reply_to: str | None = None) -> list[str]:
        raise NotImplementedError("这个平台不能发图片")

    async def recall(self, chat_type: str, chat_id: str, msg_id: str) -> bool:
        return False

    async def fetch_image(self, url: str) -> bytes | None:
        return None

    async def fetch_file(self, f: dict, max_bytes: int) -> bytes | None:
        """下载一个文件消息里的文件；不支持的平台返回 None。超过 max_bytes 不要下。"""
        return None

    def proxy(self) -> str | None:
        """这个账号走不走代理：全局代理地址 + 账号上的「使用代理」开关（海外平台默认开）。"""
        if self.account.get("use_proxy", self.overseas):
            return self.engine.settings.g("proxy") or None
        return None

    async def set_profile(self, nickname: str = "", signature: str = "", avatar: bytes | None = None):
        raise NotImplementedError("这个平台的资料需要手动修改")

    async def set_chat_card(self, chat_id: str, card: str):
        raise NotImplementedError("这个平台的群名片需要手动修改")

    async def answer_request(self, kind: str, flag: str, sub_type: str, approve: bool):
        pass

    async def list_chats(self) -> list[dict]:
        return []
