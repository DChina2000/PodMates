"""体验模式：不连任何账号，用一个模拟的群来体验角色、守门员和面板。

· 虚拟平台 demo（只在面板里显示），群里有几个按剧本说话的模拟群友，你自己也能发言；
· 还没填服务商密钥：用预设的演示回复，不调模型；填了密钥：真调模型，用量记在「试聊」下；
· 体验群不写记忆；退出体验时把账号和群都清掉。
"""
import asyncio
import random

from .channels.base import Msg
from .people import DEMO_ME

ROOM = "demo-room"
ACCOUNT = "ac_demo"
CHAT_KEY = f"demo:{ROOM}"
ME = (DEMO_ME, "你")
SCRIPT = [
    ("阿明", "今天终于把论文初稿交了，累死我了"),
    ("小红", "恭喜恭喜！晚上吃点好的庆祝一下？"),
    ("老王", "我推荐火锅，这天气正合适"),
    ("阿明", "可以可以，不过我只想躺着"),
    ("小红", "你们看了最近那部新番吗，我追得停不下来"),
    ("老王", "哪部？我最近只看了点球赛"),
    ("阿明", "有人下周末想去爬山吗"),
    ("小红", "我想去，但怕起不来"),
    ("老王", "哈哈哈 你上次也这么说"),
    ("阿明", "话说你们觉得 AI 会不会把我们的工作都抢了"),
]


class Demo:
    def __init__(self, core):
        self.core = core
        self.task: asyncio.Task | None = None
        self.i = 0

    @property
    def on(self) -> bool:
        return bool(self.core.db.doc("account", ACCOUNT))

    def state(self) -> dict:
        acc = self.core.db.doc("account", ACCOUNT)
        ep_ok = True
        try:
            self.core.llm.endpoint(self.core.settings.get("main_endpoint", acc["role_id"] if acc else None, "demo", CHAT_KEY))
        except Exception:
            ep_ok = False
        return {"on": bool(acc), "role_id": (acc or {}).get("role_id", ""), "chatter": bool(self.task and not self.task.done()),
                "has_key": ep_ok, "room": CHAT_KEY}

    async def start(self, role_id: str, chatter: bool = True):
        await self.stop()
        db, eng = self.core.db, self.core.engine
        if not db.doc("role", role_id):
            raise KeyError(role_id)
        acc = db.put("account", {"id": ACCOUNT, "platform": "demo", "role_id": role_id, "name": "体验机器人", "enabled": False})
        self.core.settings.set(f"chat:{CHAT_KEY}", "memory_enabled", False)
        self.core.settings.set(f"chat:{CHAT_KEY}", "proactive", True)
        await eng.start_account(acc)
        for _ in range(100):  # 等渠道上线（self_id 填好）
            if eng.bots[ACCOUNT].channel.status == "online":
                break
            await asyncio.sleep(0.02)
        eng.register_chat(ACCOUNT, "demo", ROOM, "体验群")
        if chatter:
            self.chatter(True)

    def chatter(self, on: bool):
        if on and not (self.task and not self.task.done()):
            self.task = asyncio.create_task(self._chatter())
        elif not on and self.task:
            self.task.cancel()
            self.task = None

    async def _chatter(self):
        while True:
            await asyncio.sleep(random.uniform(6, 11) * (self.core.engine.pace or 0.01))
            name, text = SCRIPT[self.i % len(SCRIPT)]
            self.i += 1
            await self.say(name, text, uid=f"demo-{name}")

    async def say(self, name: str, text: str, uid: str | None = None, at: bool = False):
        bot = self.core.engine.bots.get(ACCOUNT)
        if not bot:
            raise RuntimeError("体验模式还没开")
        uid = uid or ME[0]
        self.core.engine.seq = getattr(self.core.engine, "seq", 0) + 1
        await self.core.engine.on_message(Msg("demo", ACCOUNT, "group", ROOM, uid, name, text, msg_id=f"dm{self.core.engine.seq}",
                                              chat_name="体验群", at_ids=[bot.uid] if at else [], sender_role="member",
                                              dedup=f"demo|{self.core.engine.seq}"))

    async def stop(self):
        self.chatter(False)
        eng, db = self.core.engine, self.core.db
        await eng.stop_account(ACCOUNT)
        db.delete("account", ACCOUNT)
        db.delete("chat", CHAT_KEY)
        self.core.settings.drop_scope(f"chat:{CHAT_KEY}")
        room = eng.rooms.pop(CHAT_KEY, None)
        if room and room.worker:
            room.worker.cancel()
        db.x("DELETE FROM messages WHERE room=?", (CHAT_KEY,))
