"""补充十一：消息页加入私聊；已配置但还没收到消息的群和私聊也列出来。"""
import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from mb.server import Server


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


async def client(e):
    c = TestClient(TestServer(Server(e.core).app))
    await c.start_server()
    return c


def test_private_chat_is_stored_listed_and_does_not_touch_group_context(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        await e.say("100", "群里说一句", chat="100", at=["900"])
        group = e.engine.rooms["qq:100"]
        ctx_before, seq_before, mem_before = len(group.ctx), group.seq, group.mem_count
        await e.say("111", "私聊在吗", private=True, name="阿明")
        await e.engine.idle()
        assert bot.sent[-1][0] == "private"
        # 记录：对方的话和她的回复都存了，房间名 dm:账号:对方
        key = "dm:ac_900:111"
        rows = e.core.db.rows("SELECT kind, name, text FROM messages WHERE room=? ORDER BY id", (key,))
        assert [r["kind"] for r in rows] == ["human", "bot"] and rows[0]["name"] == "阿明" and rows[0]["text"] == "私聊在吗"
        # 不占群聊的上下文条数、不进群的记忆计数；私聊不建「群」
        assert (len(group.ctx), group.seq, group.mem_count) == (ctx_before, seq_before, mem_before)
        assert key not in e.engine.rooms and not e.core.db.doc("chat", key)
        # 列表：加 ?all=1 才带私聊；对方名字、平台、最后一条都有
        c = await client(e)
        plain = await (await c.get("/api/rooms")).json()
        assert all(not r["id"].startswith("dm:") and r["type"] == "group" for r in plain)
        allr = await (await c.get("/api/rooms?all=1")).json()
        dm = next(r for r in allr if r["id"] == key)
        assert dm["type"] == "private" and dm["platform"] == "qq" and dm["name"] == "阿明" and dm["last"]["text"] == bot.sent[-1][2]
        assert dm["bots"] and dm["bots"][0]["id"] == "ac_900"
        assert any(r["id"] == "qq:100" and r["type"] == "group" for r in allr)
        # 能看记录
        msgs = await (await c.get(f"/api/rooms/{key}/messages")).json()
        assert [m["kind"] for m in msgs] == ["human", "bot"]
        await c.close()
    env.run(body)


def test_private_manual_send_and_force_reply(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "private_scope", "list")  # 只回人物表里的人：陌生人私聊平时不回，但记录照存
        await e.say("555", "陌生人私聊", private=True, name="陌生人")
        await e.engine.idle()
        key = "dm:ac_900:555"
        assert bot.sent == []
        human = e.core.db.rows("SELECT id, kind FROM messages WHERE room=?", (key,))
        assert [r["kind"] for r in human] == ["human"]
        c = await client(e)
        # 面板上手动发言：以账号当前角色发出，也记进记录
        r = await c.post(f"/api/rooms/{key}/send", json={"account": "ac_900", "text": "我是人工"})
        assert r.status == 200 and bot.sent[-1][:3] == ("private", "555", "我是人工")
        assert e.core.db.rows("SELECT kind, text FROM messages WHERE room=? ORDER BY id DESC LIMIT 1", (key,))[0] == {"kind": "bot", "text": "我是人工"}
        # 点某条消息让她回：不看「只回人物表里的人」，也不把这句话再记一遍
        e.llm.main = lambda s, u: "好的我回"
        r = await c.post(f"/api/rooms/{key}/reply", json={"account": "ac_900", "msg_id": human[0]["id"]})
        assert r.status == 200
        for _ in range(50):
            if bot.sent[-1][2] == "好的我回":
                break
            await asyncio.sleep(0.05)
        assert bot.sent[-1][:3] == ("private", "555", "好的我回")
        kinds = [x["kind"] for x in e.core.db.rows("SELECT kind FROM messages WHERE room=? ORDER BY id", (key,))]
        assert kinds == ["human", "bot", "bot"]
        # 账号不在线 → 400；私聊撤回暂不支持，不报错
        assert (await c.post(f"/api/rooms/{key}/send", json={"account": "ac_nobody", "text": "x"})).status == 400
        rc = await (await c.post(f"/api/rooms/{key}/recall", json={"account": "ac_900"})).json()
        assert rc["ok"] is False
        await c.close()
    env.run(body)


def test_shadow_private_reply_is_recorded_as_shadow(env):
    async def body(e):
        bot = e.add_bot("900")
        setg(e, "proactive", False)
        setg(e, "shadow", True)
        await e.say("111", "私聊在吗", private=True, name="阿明")
        await e.engine.idle()
        assert bot.sent == []
        kinds = [r["kind"] for r in e.core.db.rows("SELECT kind FROM messages WHERE room='dm:ac_900:111' ORDER BY id")]
        assert kinds == ["human", "shadow"]
    env.run(body)


def test_configured_but_silent_chats_are_listed(env):
    async def body(e):
        # 微信账号监听里填了一个群、一个私聊：还没收到过消息，也要列出来
        e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun",
                                  "listen": [{"name": "示例家庭群", "type": "group"}, {"name": "秘密备注妈", "type": "private"}, {"name": "  ", "type": "group"}]})
        c = await client(e)
        allr = await (await c.get("/api/rooms?all=1")).json()
        g = next(r for r in allr if r["id"] == "wechat:示例家庭群")
        p = next(r for r in allr if r["id"] == "dm:ac_wx:秘密备注妈")
        assert g["empty"] and g["type"] == "group" and g["platform"] == "wechat" and g["last"] is None
        assert p["empty"] and p["type"] == "private" and p["name"] == "秘密备注妈" and p["platform"] == "wechat"
        assert len([r for r in allr if r["platform"] == "wechat"]) == 2  # 空名字的不列
        # 不带 all：群页面不受影响（不出现没有群文档的占位）
        plain = await (await c.get("/api/rooms")).json()
        assert not any(r["id"].startswith("wechat:") or r["id"].startswith("dm:") for r in plain)
        # 收到消息之后：占位被真实的项取代，不重复
        e.engine.push_dm("ac_wx", "秘密备注妈", "秘密备注妈", "在吗", "human")
        e.engine.room("wechat", "示例家庭群", "示例家庭群")
        allr = await (await c.get("/api/rooms?all=1")).json()
        assert [r["id"] for r in allr].count("dm:ac_wx:秘密备注妈") == 1 and [r["id"] for r in allr].count("wechat:示例家庭群") == 1
        p = next(r for r in allr if r["id"] == "dm:ac_wx:秘密备注妈")
        assert not p.get("empty") and p["last"]["text"] == "在吗"
        await c.close()
    env.run(body)


def test_ui_has_filter_and_empty_label():
    root = Path(__file__).resolve().parent.parent
    js = (root / "web/js/pages-main.js").read_text(encoding="utf-8")
    for s in ("/api/rooms?all=1", "['group', '群聊']", "['private', '私聊']", "暂无消息", "chatFilter"):
        assert s in js, s
