"""体验模式用的虚拟平台：不连任何账号，机器人说的话只显示在面板里。"""
from .base import Channel


class DemoChannel(Channel):
    platform = "demo"
    supports_recall = True

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.n = 0

    async def start(self):
        self.self_id = "demo-bot"
        self.self_name = self.account.get("name") or "体验机器人"
        self.set_status("online", "体验模式（不连任何账号）")

    async def send(self, chat_type, chat_id, text, reply_to=None):
        self.n += 1
        return [f"demo-{self.n}"]

    async def recall(self, chat_type, chat_id, msg_id):
        return True
