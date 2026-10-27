"""群禁言：认出被禁言（通知、发送报错、上线时主动查），禁言期间这个号在这个群里不为发言做任何事，到点 / 解除后恢复。全部用桩，不往任何群发消息。"""
import asyncio
import json
import time
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import FakeChannel
from mb.channels import discord_ch, telegram
from mb.channels.base import Muted
from mb.channels.discord_ch import DiscordChannel
from mb.channels.onebot import QQChannel, is_mute_error
from mb.engine import Engine
from mb.server import Server
from test_platforms import Mock, launch, tg_update, until

HAITUN = "90001"
RK = "qq:100"
NTEVENT_120 = ('EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:\n'
               '{\n    "result": 120,\n    "errMsg": ""\n}\n')


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


class MockQQ:
    """模拟 NapCat：记录动作；群成员信息、群信息、发送结果都能在测试里改。"""

    def __init__(self, self_id="900", token="tok"):
        self.self_id, self.token = self_id, token
        self.actions, self.ws, self.n = [], None, 0
        self.member = {"shut_up_timestamp": 0, "role": "member"}
        self.group_all_shut = 0
        self.send_error = None  # 发群消息时返回的失败 message
        self.query_fail = False

    async def handle(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws = ws
        async for m in ws:
            a = json.loads(m.data)
            self.actions.append(a)
            act, data, status, msg = a["action"], None, "ok", ""
            if act == "get_login_info":
                data = {"user_id": int(self.self_id), "nickname": "小号"}
            elif act == "get_group_list":
                data = [{"group_id": 100, "group_name": "测试群"}]
            elif act == "get_group_member_info":
                if self.query_fail:
                    status, msg = "failed", "查询失败"
                data = dict(self.member, group_id=a["params"]["group_id"], user_id=int(self.self_id))
            elif act == "get_group_info":
                data = {"group_id": a["params"]["group_id"], "group_all_shut": self.group_all_shut}
            elif act in ("send_group_msg", "send_private_msg"):
                if self.send_error and act == "send_group_msg":
                    status, msg = "failed", self.send_error
                else:
                    self.n += 1
                    data = {"message_id": 7000 + self.n}
            await ws.send_str(json.dumps({"status": status, "retcode": 0 if status == "ok" else 200, "message": msg, "data": data, "echo": a["echo"]}))
        return ws

    async def push(self, ev):
        await self.ws.send_str(json.dumps(ev, ensure_ascii=False))

    def sent(self, action="send_group_msg"):
        return [a for a in self.actions if a["action"] == action]


async def wait_for(cond, timeout=5):
    for _ in range(int(timeout * 100)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


def group_msg(uid, segs, seq, role="member"):
    return {"post_type": "message", "message_type": "group", "group_id": 100, "user_id": int(uid), "self_id": 900, "message_id": seq,
            "message_seq": seq, "message": segs, "sender": {"nickname": "群友", "role": role}}


def at_me(text="在吗"):
    return [{"type": "at", "data": {"qq": "900"}}, {"type": "text", "data": {"text": " " + text}}]


async def qq_up(e, mock, ready=True):
    app = web.Application()
    app.router.add_get("/", mock.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    acc = e.core.db.put("account", {"id": "ac_qq", "platform": "qq", "qq": "900", "role_id": "r_haitun", "manage_napcat": False, "ws_port": port})
    e.core.db.set_secret("onebot:ac_qq", "tok")
    e.core.db.set_kv("platforms", {"qq": True})
    e.engine.reload()
    await e.engine.start_account(acc)
    ch = e.engine.bots["ac_qq"].channel
    ch.headers_token = "tok"
    await wait_for(lambda: ch.status == "online")
    await wait_for(lambda: "qq:100" in e.engine.rooms)
    await asyncio.sleep(0.15)  # 上线后的禁言扫描
    return ch, runner


class Auth(MockQQ):
    async def handle(self, req):
        if req.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        return await super().handle(req)


# ---------- 引擎层：被禁言时什么都不做 ----------
def test_banned_account_does_nothing_for_speaking_and_resumes_when_it_ends(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        e.llm.gate = lambda p: {"reply": True, "brief": "接话"}
        setg(e, "cold_start", True)
        setg(e, "cold_minutes", 0)
        setg(e, "cold_hours", "00:00-23:59")
        setg(e, "shift_greeting", True)
        room = eng.room("qq", "100", "测试群")
        room.accounts.add("ac_900")
        b = eng.duty(eng.bots["ac_900"], room)
        eng.ban_update("ac_900", RK, member_until=time.time() + 3600)
        assert eng.muted_in("ac_900", RK) and not eng.can_talk(b, room)
        # 被 @、没 @（守门员 / 观察模式）、定时消息、冷场、换班招呼、提醒、贴纸：都不触发，模型桩一次都没调
        await e.say("111", "有人在吗", at=["900"])
        await e.say("111", "随便聊聊")
        doc = e.core.db.doc("chat", RK)
        hm = time.strftime("%H:%M")
        doc["schedules"] = [{"time": hm, "mode": "ai", "content": "说点什么", "on": True}, {"time": hm, "mode": "text", "content": "早安", "on": True}]
        e.core.db.put("chat", doc)
        room.ctx.append(type(room.ctx[-1])("111", "群友", "旧消息", ts=time.time() - 7200))
        e.core.db.x("INSERT INTO reminders (platform, chat_type, chat_id, account_id, user_id, user_name, text, due, created, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("qq", "group", "100", "ac_900", "111", "群友", "喝水", time.time() - 5, time.time() - 60, "pending"))
        await eng.tick()
        await eng.on_shift(b, room, "r_haitun")
        await eng.idle()
        assert await eng.deliver_sticker(b, room, {"id": 1, "descr": "x"}, None) is False
        assert len(e.llm.calls) == 0 and bot.sent == []
        assert e.core.db.x("SELECT status FROM reminders").fetchone()[0] == "pending"  # 提醒没被当成已发，等能说了再补
        with pytest.raises(Muted, match="被禁言，没有发出"):
            await eng.manual_send("ac_900", RK, "人工发的")
        # 到点：自动恢复
        eng.bans[("ac_900", RK)]["member_until"] = time.time() - 1
        assert not eng.muted_in("ac_900", RK) and ("ac_900", RK) not in eng.bans
        await e.say("111", "你好呀", at=["900"])
        assert bot.sent and e.llm.calls
    env.run(body)


def test_listening_continues_and_admin_commands_work_but_say_nothing_while_banned(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        setg(e, "proactive", False)
        room = eng.room("qq", "100", "测试群")
        room.accounts.add("ac_900")
        eng.ban_update("ac_900", RK, member_until=time.time() + 3600)
        n_mem = room.mem_count
        await e.say("111", "有人在群里说话")
        await e.say(HAITUN, "闭嘴 5", at=["900"])  # 管理指令照样生效，只是不回话
        assert room.mute["ac_900"] > time.time() and bot.sent == [] and e.llm.calls == []
        assert [x.text for x in room.ctx] == ["有人在群里说话", "闭嘴 5"]  # 收消息、存记录
        assert e.core.db.x("SELECT COUNT(*) FROM messages WHERE room=?", (RK,)).fetchone()[0] == 2
        assert room.mem_count == n_mem + 2  # 整理记忆的计数照走
    env.run(body)


def test_ban_is_per_account_and_the_other_account_still_speaks(env):
    async def body(e):
        b1, b2 = e.add_bot("900"), e.add_bot("901")
        setg(e, "proactive", False)
        room = e.engine.room("qq", "100", "测试群")
        room.accounts.update({"ac_900", "ac_901"})
        e.engine.ban_update("ac_900", RK, member_until=time.time() + 3600)
        await e.say("111", "你们俩谁在", at=["900", "901"])
        assert b1.sent == [] and len(b2.sent) == 1
    env.run(body)


def test_ban_state_persists_and_expired_ones_are_cleared_on_start(env):
    async def body(e):
        e.add_bot("900")
        eng = e.engine
        now = time.time()
        eng.ban_update("ac_900", RK, member_until=now + 3600)
        eng.ban_update("ac_900", "qq:200", unknown=True)  # 结束时间未知
        eng.ban_update("ac_900", "qq:300", member_until=now + 3600)
        eng.bans[("ac_900", "qq:300")]["member_until"] = now - 10  # 关机期间到期了
        eng.save_bans()
        eng2 = Engine(e.core.db, e.core.settings, e.core.llm, emit=e.core.emit)
        assert set(eng2.bans) == {("ac_900", RK), ("ac_900", "qq:200")}  # 到期的启动时清掉
        assert eng2.muted_in("ac_900", RK) and eng2.muted_in("ac_900", "qq:200") and not eng2.muted_in("ac_900", "qq:300")
        assert ("ac_900", "qq:300") not in {tuple(k.split("|", 1)) for k in e.core.db.kv("bans")}  # 存库的也清了
        eng2.close()
    env.run(body)


def test_panel_lists_bans_leaves_one_notice_per_ban_and_refuses_manual_send(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        room = eng.room("qq", "100", "测试群")
        room.accounts.add("ac_900")
        until = time.time() + 3600
        eng.ban_update("ac_900", RK, member_until=until)
        eng.ban_update("ac_900", RK, member_until=until + 600)  # 同一次禁言又来一条通知（延长）：不再留通知
        eng.ban_update("ac_900", "qq:200", unknown=True)
        notes = e.core.db.kv("notices")
        assert len(notes) == 2 and "测试群" in notes[1]["text"] and "结束时间未知" in notes[0]["text"] and "到 " in notes[1]["text"]
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        bans = {b["room"]: b for b in await (await c.get("/api/bans")).json()}
        assert abs(bans[RK]["until"] - (until + 600)) < 1 and bans["qq:200"]["until"] is None and bans[RK]["room_name"] == "测试群"
        r = await c.post(f"/api/rooms/{RK}/send", json={"account": "ac_900", "text": "我来说一句"})
        assert r.status == 409 and (await r.json())["error"] == "被禁言，没有发出" and bot.sent == []
        # 解除后再被禁言 = 新的一次禁言，再留一条
        eng.ban_update("ac_900", RK, member_until=None, unknown=False)
        eng.ban_update("ac_900", RK, member_until=until)
        assert len(e.core.db.kv("notices")) == 3
        eng.ban_update("ac_900", RK, member_until=None)
        ok = await c.post(f"/api/rooms/{RK}/send", json={"account": "ac_900", "text": "解除了，我来说"})
        assert ok.status == 200 and bot.sent[-1][2] == "解除了，我来说"
        await c.close()
    env.run(body)


# ---------- QQ（NapCat）：通知、发送报错、上线时查询 ----------
def test_qq_ban_notice_with_duration_and_lift_notice(env):
    async def body(e):
        setg(e, "proactive", False)
        mock = Auth()
        ch, runner = await qq_up(e, mock)
        eng = e.engine
        await mock.push({"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban", "group_id": 100, "user_id": 900, "operator_id": 1, "duration": 600})
        await wait_for(lambda: eng.muted_in("ac_qq", RK))
        rec = eng.bans[("ac_qq", RK)]
        assert 590 < rec["member_until"] - time.time() <= 600
        await mock.push({"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban", "group_id": 100, "user_id": 555, "operator_id": 1, "duration": 600})  # 别人被禁言：不管
        await mock.push(group_msg(HAITUN, at_me(), 1, role="owner"))
        await asyncio.sleep(0.3)
        await e.engine.idle()
        assert mock.sent() == [] and e.llm.calls == []
        await mock.push({"post_type": "notice", "notice_type": "group_ban", "sub_type": "lift_ban", "group_id": 100, "user_id": 900, "operator_id": 1, "duration": 0})  # 解除：立刻恢复
        await wait_for(lambda: not eng.muted_in("ac_qq", RK))
        await mock.push(group_msg(HAITUN, at_me("现在呢"), 2, role="owner"))
        await wait_for(lambda: mock.sent())
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


def test_qq_whole_group_ban_counts_for_members_but_not_for_owner_or_admin(env):
    async def body(e):
        setg(e, "proactive", False)
        mock = Auth()
        ch, runner = await qq_up(e, mock)
        eng = e.engine
        mock.group_all_shut = -1
        await mock.push({"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban", "group_id": 100, "user_id": 0, "operator_id": 1, "duration": -1})
        await wait_for(lambda: eng.muted_in("ac_qq", RK))
        assert eng.ban_end(eng.bans[("ac_qq", RK)], time.time()) is None  # 全员禁言：结束时间未知
        mock.group_all_shut = 0
        await mock.push({"post_type": "notice", "notice_type": "group_ban", "sub_type": "lift_ban", "group_id": 100, "user_id": 0, "operator_id": 1, "duration": 0})
        await wait_for(lambda: not eng.muted_in("ac_qq", RK))
        # 这个号是管理员：全员禁言不算被禁言
        mock.member = {"shut_up_timestamp": 0, "role": "admin"}
        mock.group_all_shut = -1
        await mock.push({"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban", "group_id": 100, "user_id": 0, "operator_id": 1, "duration": -1})
        await asyncio.sleep(0.3)
        assert not eng.muted_in("ac_qq", RK)
        await mock.push(group_msg(HAITUN, at_me(), 1, role="owner"))
        await wait_for(lambda: mock.sent())
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


def test_qq_send_error_120_marks_unknown_end_stops_trying_and_recheck_resumes_without_sending(env):
    async def body(e):
        setg(e, "proactive", False)
        mock = Auth()
        ch, runner = await qq_up(e, mock)
        eng = e.engine
        mock.send_error = NTEVENT_120
        await mock.push(group_msg(HAITUN, at_me(), 1, role="owner"))
        await wait_for(lambda: eng.muted_in("ac_qq", RK))
        assert len(mock.sent()) == 1 and eng.bans[("ac_qq", RK)]["unknown"] is True and eng.ban_end(eng.bans[("ac_qq", RK)], time.time()) is None
        assert e.core.db.x("SELECT COUNT(*) FROM messages WHERE room=? AND kind='bot'", (RK,)).fetchone()[0] == 0  # 没发出去的不记成她说过
        # 之后不再尝试
        n_calls = len(e.llm.calls)
        for i in range(3):
            await mock.push(group_msg(HAITUN, at_me(f"第{i}次"), 10 + i, role="owner"))
        await asyncio.sleep(0.3)
        await e.engine.idle()
        assert len(mock.sent()) == 1 and len(e.llm.calls) == n_calls
        # 复查：没到时间不查；到了时间，还被禁言就继续；解禁了就恢复。复查期间一条都没往群里发
        sent_before, q0 = len(mock.actions), len(mock.sent("get_group_member_info"))
        await eng.tick()
        assert len(mock.sent("get_group_member_info")) == q0  # 第一次复查隔 5 分钟，还没到
        mock.member = {"shut_up_timestamp": time.time() + 1800, "role": "member"}
        eng.bans[("ac_qq", RK)]["checked"] = time.time() - 400
        await eng.tick()
        assert eng.muted_in("ac_qq", RK) and 1700 < eng.bans[("ac_qq", RK)]["member_until"] - time.time() <= 1800 and not eng.bans[("ac_qq", RK)]["unknown"]  # 查到了结束时间
        eng.bans[("ac_qq", RK)].update(unknown=True, member_until=None, checked=time.time() - 400, tries=0)  # 回到「结束时间未知」，再查
        mock.member = {"shut_up_timestamp": 0, "role": "member"}
        await eng.tick()
        assert not eng.muted_in("ac_qq", RK)
        assert len(mock.sent()) == 1  # 一直只有最开始那一次失败的尝试
        assert not [a for a in mock.actions[sent_before:] if a["action"].startswith("send_")]
        await mock.push(group_msg(HAITUN, at_me("好了吗"), 99, role="owner"))
        mock.send_error = None
        await wait_for(lambda: len(mock.sent()) == 2)
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


def test_recheck_backs_off_and_gives_up_only_when_the_query_keeps_failing(env):
    async def body(e):
        bot = e.add_bot("900")
        eng = e.engine
        calls = []

        async def q(chat_id, fresh=False):
            calls.append(chat_id)
            raise RuntimeError("查不了")
        bot.query_ban = q
        eng.ban_update("ac_900", RK, unknown=True)
        rec = eng.bans[("ac_900", RK)]
        rec["checked"] = time.time() - 301
        await eng.recheck_bans()
        assert len(calls) == 1 and rec["tries"] == 1
        await eng.recheck_bans()  # 间隔翻倍：马上再来不查
        assert len(calls) == 1
        rec["checked"] = time.time() - 400
        await eng.recheck_bans()
        assert len(calls) == 1  # 第二次要隔 10 分钟
        rec["checked"] = time.time() - 1900
        rec["since"] = time.time() - 49 * 3600
        await eng.recheck_bans()
        assert not eng.muted_in("ac_900", RK)  # 一直查不出结果超过 48 小时：不再当作被禁言
    env.run(body)


def test_other_send_failures_are_not_mistaken_for_a_ban(env):
    async def body(e):
        assert is_mute_error(NTEVENT_120) and is_mute_error("你已被禁言")
        for msg in ('EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg EventRet:\n{"result": 1002, "errMsg": ""}', "消息过长", "网络错误 timeout",
                    "sendMsg 失败：result 1200", "风控：账号异常", '"result": 1120', ""):
            assert not is_mute_error(msg), msg
        setg(e, "proactive", False)
        mock = Auth()
        ch, runner = await qq_up(e, mock)
        eng = e.engine
        for i, err in enumerate(('EventRet:\n{"result": 1002}', "消息过长", "网络错误")):
            mock.send_error = err
            await mock.push(group_msg(HAITUN, at_me(f"试{i}"), 20 + i, role="owner"))
            await wait_for(lambda: len(mock.sent()) == i + 1)
            await asyncio.sleep(0.1)
            assert not eng.muted_in("ac_qq", RK) and ("ac_qq", RK) not in eng.bans  # 都没被当成禁言：下一条照样会再试
        mock.send_error = "风控：账号异常"
        await mock.push(group_msg(HAITUN, at_me("风控"), 40, role="owner"))
        await wait_for(lambda: len(mock.sent()) == 4)
        await asyncio.sleep(0.1)
        assert not eng.muted_in("ac_qq", RK)
        assert any("风控" in r["text"] for r in e.core.db.rows("SELECT text FROM logs WHERE cat='notify'"))  # 风控照旧走风控的通知
        # 私聊发送失败（就算带 120）也不是群禁言
        mock.send_error = None
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


def test_qq_connect_scan_finds_a_ban_that_started_while_offline(env):
    async def body(e):
        setg(e, "proactive", False)
        mock = Auth()
        mock.member = {"shut_up_timestamp": int(time.time()) + 900, "role": "member"}
        ch, runner = await qq_up(e, mock)
        eng = e.engine
        assert eng.muted_in("ac_qq", RK) and 800 < eng.bans[("ac_qq", RK)]["member_until"] - time.time() <= 900
        assert len(e.core.db.kv("notices")) == 1
        await mock.push(group_msg(HAITUN, at_me(), 1, role="owner"))
        await asyncio.sleep(0.3)
        assert mock.sent() == [] and e.llm.calls == []
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
        # 之前存下的禁言、现在查出来已经没事了：上线时清掉
        e.engine.ban_update("ac_qq", RK, unknown=True)
        mock2 = Auth()
        ch, runner = await qq_up(e, mock2)
        assert not e.engine.muted_in("ac_qq", RK)
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


def test_qq_connect_scan_whole_group_ban_member_and_admin(env):
    async def body(e):
        setg(e, "proactive", False)
        mock = Auth()
        mock.group_all_shut = -1
        ch, runner = await qq_up(e, mock)
        assert e.engine.muted_in("ac_qq", RK)
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
        e.engine.ban_update("ac_qq", RK, all=False, unknown=False, member_until=None)
        mock2 = Auth()
        mock2.group_all_shut = -1
        mock2.member = {"shut_up_timestamp": 0, "role": "owner"}
        ch, runner = await qq_up(e, mock2)
        assert not e.engine.muted_in("ac_qq", RK)  # 群主 / 管理员在全员禁言里照样能说话
        await e.engine.stop_account("ac_qq")
        await runner.cleanup()
    env.run(body)


# ---------- 其他平台 ----------
def test_telegram_restriction_detected_on_send_and_by_my_chat_member_update(env):
    async def body(e):
        mock = await Mock().start()
        tok = "/bot123:abc"
        state = {"status": "member"}

        def member(req, b):
            if state["status"] == "restricted":
                return {"ok": True, "result": {"status": "restricted", "can_send_messages": False, "until_date": int(time.time()) + 900}}
            return {"ok": True, "result": {"status": state["status"]}}
        mock.on("POST", f"{tok}/getMe", {"ok": True, "result": {"id": 900, "username": "MyBot", "first_name": "豚"}})
        mock.on("POST", f"{tok}/deleteWebhook", {"ok": True, "result": True})
        mock.on("POST", f"{tok}/getUpdates", {"ok": True, "result": []})
        mock.on("POST", f"{tok}/getChatMember", member)
        mock.on("POST", f"{tok}/getChat", {"ok": True, "result": {"id": -100, "permissions": {"can_send_messages": True}}})
        mock.on("POST", f"{tok}/sendMessage", lambda req, b: (400, {"ok": False, "error_code": 400, "description": "Bad Request: have no rights to send a message"}))
        ch = await launch(e, mock, "telegram", {"token": "123:abc"})
        eng = e.engine
        # 没被限制，只是别的原因发不出去：照旧报原来的错，不当禁言
        with pytest.raises(RuntimeError, match="no rights"):
            await ch.send("group", "-100", "你好")
        assert ("ac_telegram", "telegram:-100") not in eng.bans
        # 被限制发言了：发送被拒 → 查一次确认 → 记下禁言（带结束时间）
        state["status"] = "restricted"
        with pytest.raises(Muted):
            await ch.send("group", "-100", "你好")
        rec = eng.bans[("ac_telegram", "telegram:-100")]
        assert 800 < rec["member_until"] - time.time() <= 900
        # 通知：被限制 / 解除
        eng.ban_update("ac_telegram", "telegram:-100", member_until=None)
        ch._on_my_member({"chat": {"id": -100}, "new_chat_member": {"status": "restricted", "can_send_messages": False, "until_date": 0}})
        assert eng.muted_in("ac_telegram", "telegram:-100") and eng.ban_end(eng.bans[("ac_telegram", "telegram:-100")], time.time()) is None  # 无限期：结束时间未知
        ch._on_my_member({"chat": {"id": -100}, "new_chat_member": {"status": "member"}})
        assert not eng.muted_in("ac_telegram", "telegram:-100")
        # 全员禁言（群默认权限不能发消息），这个号是普通成员 = 被禁言；管理员不算
        mock.on("POST", f"{tok}/getChat", {"ok": True, "result": {"id": -100, "permissions": {"can_send_messages": False}}})
        res = await ch.query_ban("-100")
        assert res["all"] is True and eng.ban_active(res, time.time())
        state["status"] = "administrator"
        res = await ch.query_ban("-100")
        assert res["role"] == "admin" and not eng.ban_active(res, time.time())
        assert await ch.query_ban("12345") is None  # 私聊没有这回事
        await e.engine.stop_account("ac_telegram")
        await mock.stop()
    env.run(body)


def test_discord_timeout_is_a_ban_and_other_forbidden_errors_are_not(env):
    async def body(e):
        import discord
        e.core.settings.set("global", "proactive", False)
        acc = e.core.db.put("account", {"id": "ac_dc", "platform": "discord", "role_id": "r_haitun"})
        ch = DiscordChannel(e.engine, acc)
        me = types.SimpleNamespace(timed_out_until=None, guild_permissions=types.SimpleNamespace(administrator=False))
        guild = types.SimpleNamespace(name="服", me=me)
        sent = []

        class Ch:
            id, name = 555, "闲聊"

            def __init__(self):
                self.guild = guild

            async def send(self, *a, **k):
                sent.append(a)
                raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        dch = Ch()
        ch.client = types.SimpleNamespace(get_channel=lambda cid: dch if cid == 555 else None)
        ch.self_id, ch.status = "42", "online"
        e.engine.add_bot(acc, ch)
        eng = e.engine
        # 没被超时，只是权限不够：照旧抛原来的错，不当禁言
        with pytest.raises(discord.Forbidden):
            await ch.send("group", "555", "你好")
        assert ("ac_dc", "discord:555") not in eng.bans
        # 被超时了（有确切结束时间）
        import datetime
        me.timed_out_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
        with pytest.raises(Muted):
            await ch.send("group", "555", "你好")
        assert 500 < eng.bans[("ac_dc", "discord:555")]["member_until"] - time.time() <= 600
        # 管理员不会被超时；超时过了也就没了
        me.timed_out_until = None
        assert not eng.ban_active(await ch.query_ban("555"), time.time())
        await eng.scan_bans(eng.bots["ac_dc"], ["555"])
        assert not eng.muted_in("ac_dc", "discord:555")
    env.run(body)


# ---------- 界面 ----------
def test_panel_pages_show_the_ban():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "web/js/pages-main.js").read_text(encoding="utf-8")
    assert "GET('/api/bans')" in js and "结束时间未知" in js and "被禁言" in js
    assert js.count("GET('/api/bans')") >= 2  # 总览和群页都有
