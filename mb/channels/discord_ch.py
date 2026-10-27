"""Discord：官方机器人账号（每个 Discord 账号一个令牌），可走代理。"""
import asyncio
import base64
import time

from .base import Channel, Msg, Muted
from .. import tls

# 查看频道 + 发消息 + 读历史 + 改昵称 + 在子区发消息
INVITE_PERMS = 1024 + 2048 + 65536 + 67108864 + 274877906944


def app_id_from_token(token: str) -> str:
    try:
        part = token.split(".")[0]
        return base64.b64decode(part + "=" * (-len(part) % 4)).decode()
    except Exception:
        return ""


def invite_url(app_id: str) -> str:
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions={INVITE_PERMS}"


def to_msg(aid: str, m, self_id: str) -> Msg | None:
    """discord.Message → 统一消息。单独拿出来，测试时可直接喂假对象。"""
    if m.author.bot and str(m.author.id) == self_id:
        return None
    guild = getattr(m, "guild", None)
    reply_uid = ""
    ref = getattr(m, "reference", None)
    if ref is not None and getattr(ref, "resolved", None) is not None and getattr(ref.resolved, "author", None):
        reply_uid = str(ref.resolved.author.id)
    text = m.content or ""
    for u in m.mentions:
        text = text.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
    images = [a.url for a in getattr(m, "attachments", []) if (getattr(a, "content_type", "") or "").startswith("image/")]
    return Msg("discord", aid, "group" if guild else "private", str(m.channel.id) if guild else str(m.author.id),
               str(m.author.id), getattr(m.author, "display_name", None) or m.author.name, text.strip(),
               msg_id=str(m.id), chat_name=(f"{guild.name} #{m.channel.name}" if guild else ""),
               at_ids=[str(u.id) for u in m.mentions], reply_to_uid=reply_uid, images=images,
               sender_role="admin" if guild and getattr(getattr(m.author, "guild_permissions", None), "administrator", False) else "",
               dedup=str(m.id))


class DiscordChannel(Channel):
    platform = "discord"
    overseas = True

    def __init__(self, engine, account):
        super().__init__(engine, account)
        self.client = None
        self.task = None
        self.stopping = False

    def token(self) -> str:
        return self.engine.db.secret(f"discord:{self.aid}")

    async def start(self):
        import discord
        token = self.token()
        if not token:
            self.set_status("error", "还没填机器人令牌")
            return
        self.stopping = False
        proxy = self.proxy()
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = client = discord.Client(intents=intents, proxy=proxy, connector=tls.connector())  # discord.py 自己的 HTTP / 网关连接也用统一的证书

        @client.event
        async def on_ready():
            self.self_id, self.self_name = str(client.user.id), client.user.display_name
            self.set_status("online", f"已登录 {client.user}")
            for g in client.guilds:
                for c in g.text_channels:
                    self.engine.register_chat(self.aid, "discord", str(c.id), f"{g.name} #{c.name}")
            b = self.engine.bots.get(self.aid)
            if b:  # 上线时查一遍：这个号在各服务器里有没有被「超时」（Discord 的禁言）
                asyncio.create_task(self.engine.scan_bans(b, [str(c.id) for g in client.guilds for c in g.text_channels]))

        @client.event
        async def on_member_update(before, after):
            """机器人自己被超时 / 解除超时：重新查这个服务器的各频道。"""
            if str(after.id) == self.self_id and getattr(before, "timed_out_until", None) != getattr(after, "timed_out_until", None):
                b = self.engine.bots.get(self.aid)
                if b:
                    asyncio.create_task(self.engine.scan_bans(b, [str(c.id) for c in after.guild.text_channels]))

        @client.event
        async def on_message(m):
            msg = to_msg(self.aid, m, self.self_id)
            if msg:
                await self.engine.on_message(msg)

        @client.event
        async def on_disconnect():
            if not self.stopping:
                self.set_status("starting", "连接断开，正在重连")

        self.set_status("starting", "正在连接 Discord")
        try:
            await client.start(token)
        except discord.LoginFailure:
            self.set_status("error", "令牌不对，请到开发者后台重新复制")
        except discord.PrivilegedIntentsRequired:
            self.set_status("error", "没打开 Message Content Intent，见账号页的步骤 4")
        except Exception as e:
            if not self.stopping:
                self.set_status("error", f"连不上 Discord：{e}（国内需要在平台设置里填代理）")
                self.engine.notify(f"Discord 机器人 {self.account.get('name', '')} 停了：{e}", self.account)

    async def stop(self):
        self.stopping = True
        if self.client:
            await self.client.close()

    async def _channel(self, chat_type, chat_id):
        if chat_type == "group":
            return self.client.get_channel(int(chat_id)) or await self.client.fetch_channel(int(chat_id))
        user = self.client.get_user(int(chat_id)) or await self.client.fetch_user(int(chat_id))
        return user.dm_channel or await user.create_dm()

    async def query_ban(self, chat_id, fresh=False):
        """Discord 的禁言是「超时」：机器人自己在这个服务器里的 timed_out_until（有确切的结束时间）。管理员不会被超时。"""
        ch = self.client.get_channel(int(chat_id)) if self.client else None
        me = getattr(getattr(ch, "guild", None), "me", None)
        if me is None:
            return {"member_until": None, "all": False, "role": "member"}
        until = getattr(me, "timed_out_until", None)
        admin = bool(getattr(getattr(me, "guild_permissions", None), "administrator", False))
        return {"member_until": until.timestamp() if until and until.timestamp() > time.time() else None, "all": False,
                "role": "admin" if admin else "member"}

    async def _confirm_muted(self, chat_id):
        """发送被拒（Forbidden）：先查是不是被超时了；是才算禁言（权限不够等别的原因照旧抛原来的错）。"""
        try:
            res = await self.query_ban(chat_id)
        except Exception:
            return
        if res and self.engine.ban_active(res, time.time()):
            self.engine.ban_update(self.aid, f"discord:{chat_id}", unknown=False, **res)
            raise Muted("这个号在这个服务器里被超时（禁言）了")

    async def send(self, chat_type, chat_id, text, reply_to=None):
        import discord
        try:
            return await self._send(chat_type, chat_id, text, reply_to)
        except discord.Forbidden:
            if chat_type == "group":
                await self._confirm_muted(chat_id)
            raise

    async def _send(self, chat_type, chat_id, text, reply_to=None):
        import discord
        ch = await self._channel(chat_type, chat_id)
        ref = discord.MessageReference(message_id=int(reply_to), channel_id=ch.id, fail_if_not_exists=False) if reply_to else None
        out = []
        for i in range(0, len(text), 1900):
            m = await ch.send(text[i:i + 1900], reference=ref if i == 0 else None, mention_author=False)
            out.append(str(m.id))
        return out

    supports_image = True

    @staticmethod
    def at_prefix(uid, name):
        return f"<@{uid}> "

    async def send_image(self, chat_type, chat_id, data, reply_to=None):
        import discord
        try:
            return await self._send_image(chat_type, chat_id, data, reply_to)
        except discord.Forbidden:
            if chat_type == "group":
                await self._confirm_muted(chat_id)
            raise

    async def _send_image(self, chat_type, chat_id, data, reply_to=None):
        import io

        import discord
        from ..stickers import ext_of
        ch = await self._channel(chat_type, chat_id)
        ref = discord.MessageReference(message_id=int(reply_to), channel_id=ch.id, fail_if_not_exists=False) if reply_to else None
        m = await ch.send(file=discord.File(io.BytesIO(data), filename="sticker." + ext_of(data)), reference=ref, mention_author=False)
        return [str(m.id)]

    async def recall(self, chat_type, chat_id, msg_id):
        ch = await self._channel(chat_type, chat_id)
        await (await ch.fetch_message(int(msg_id))).delete()
        return True

    async def fetch_image(self, url):
        import aiohttp
        async with tls.session() as s:
            async with s.get(url, proxy=self.proxy()) as r:
                return await r.read() if r.status == 200 else None

    async def set_profile(self, nickname="", signature="", avatar=None):
        kw = {}
        if nickname:
            kw["username"] = nickname
        if avatar:
            kw["avatar"] = avatar
        if kw:
            await self.client.user.edit(**kw)
        if signature:
            raise NotImplementedError("Discord 机器人的「关于我」要在开发者后台的 Bot 页面里改")

    async def set_chat_card(self, chat_id, card):
        ch = await self._channel("group", chat_id)
        await ch.guild.me.edit(nick=card or None)
