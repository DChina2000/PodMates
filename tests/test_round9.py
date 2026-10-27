"""补充八：微信账号「首次绑定，之后核对」。全程用模拟的 wxauto，不碰真的微信。"""
import asyncio
import threading
import types
from pathlib import Path

import pytest

from mb.channels.wechat import WeChatChannel, judge, read_identity, who


class Chat:
    def __init__(self, who_):
        self.who, self.sent = who_, []

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        self.sent.append(msg)
        return types.SimpleNamespace(is_success=True)


class Wx:
    """模拟的 wxauto4.WeChat。info 是类属性：测试里改它就等于「微信里换了号」。"""
    info = {"nickname": "小号", "wxid": "wxid_bot1"}
    dir = ""
    instances = []

    def __init__(self, *a, **k):
        self.cbs, self.chats, self.listening, self.removed = {}, {}, False, []
        self._info = dict(Wx.info)  # 和真的一样：构造时读一次、之后 GetMyInfo 返回缓存
        Wx.instances.append(self)

    def GetMyInfo(self):
        return dict(self._info)

    def AddListenChat(self, nickname, callback):
        self.cbs[nickname] = callback
        self.chats[nickname] = Chat(nickname)
        return self.chats[nickname]

    def StartListening(self):
        self.listening = True

    def RemoveListenChat(self, nickname, close_window=True):
        self.cbs.pop(nickname, None)
        self.removed.append(nickname)

    def IsOnline(self):
        return True


def reset_wx(**info):
    Wx.info = info or {"nickname": "小号", "wxid": "wxid_bot1"}
    Wx.instances = []


@pytest.fixture(autouse=True)
def fake_wxauto(monkeypatch):
    """本文件所有测试（包括接口里重启账号时新建的渠道）都只连模拟的 wxauto。"""
    monkeypatch.setattr(WeChatChannel, "lib", types.SimpleNamespace(WeChat=Wx))


def make(e, aid="ac_wx"):
    acc = e.core.db.put("account", {"id": aid, "platform": "wechat", "role_id": "r_haitun",
                                    "listen": [{"name": "测试群", "type": "group"}]})
    ch = WeChatChannel(e.engine, acc)
    e.engine.add_bot(acc, ch)
    return ch


def notes(e):
    out = []
    e.engine.notify_hook = out.append
    e.core.settings.set("global", "notify_windows", True)
    return out


# ---------- 读身份 ----------
def test_read_identity_variants():
    class W:
        def __init__(self, info=None, nickname="", dir_="", boom=False):
            self._i, self.nickname, self.dir, self.boom = info, nickname, dir_, boom

        def GetMyInfo(self):
            if self.boom:
                raise RuntimeError("界面忙")
            return self._i
    assert read_identity(W({"nickname": "小号", "wxid": "wxid_a"})) == {"wxid": "wxid_a", "nick": "小号"}
    assert read_identity(W({"昵称": "小号", "微信号": "abc123"})) == {"wxid": "abc123", "nick": "小号"}
    assert read_identity(W({"Nickname": "小号", "WeChat_ID": "xx"})) == {"wxid": "xx", "nick": "小号"}
    assert read_identity(W({"nickname": "小号"})) == {"wxid": "", "nick": "小号"}  # 只有昵称
    assert read_identity(W({"nickname": "小号", "wxid": "None"})) == {"wxid": "", "nick": "小号"}
    assert read_identity(W({"nickname": "小号"}, dir_=r"D:\Example\xwechat_files\wxid_zz9_ab12\db_storage"))["wxid"] == "wxid_zz9"
    assert read_identity(W(None, nickname="属性昵称")) == {"wxid": "", "nick": "属性昵称"}
    assert read_identity(W(boom=True, nickname="属性昵称")) == {"wxid": "", "nick": "属性昵称"}
    assert read_identity(W({})) == {"wxid": "", "nick": ""}
    assert who({"nick": "小号", "wxid": "w1"}) == "昵称 小号（微信号 w1）"
    assert who({"nick": "小号", "wxid": ""}) == "昵称 小号（读不到微信号）"


def test_judge_table():
    b_id = {"wxid": "WXID_A", "nick": "小号"}
    assert judge(None, {"wxid": "a", "nick": "x"}) == "first"
    assert judge(b_id, {"wxid": "wxid_a", "nick": "改名了"}) == "ok"  # 微信号一样，昵称变不要紧；不分大小写
    assert judge(b_id, {"wxid": "wxid_b", "nick": "小号"}) == "mismatch"  # 昵称一样也不行
    assert judge(b_id, {"wxid": "", "nick": "小号"}) == "ok"  # 这次读不到微信号，退到昵称
    assert judge(b_id, {"wxid": "", "nick": "大号"}) == "mismatch"
    b_nick = {"wxid": "", "nick": "小号"}
    assert judge(b_nick, {"wxid": "", "nick": "小号"}) == "ok"
    assert judge(b_nick, {"wxid": "", "nick": "小号2"}) == "nick_changed"
    assert judge(b_nick, {"wxid": "w", "nick": "小号"}) == "ok"
    assert judge(b_nick, {"wxid": "w", "nick": "大号"}) == "nick_changed"
    assert judge(b_id, {"wxid": "", "nick": ""}) == "unreadable"


# ---------- 首次确认：是 / 否 ----------
def test_first_start_confirm_yes(env):
    async def body(e):
        reset_wx()
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm" and ch.pending == {"nick": "小号", "wxid": "wxid_bot1", "reason": "first", "old_nick": "", "hits": []}
        assert "昵称 小号（微信号 wxid_bot1）" in ch.detail and "机器人小号" in ch.detail
        wx = Wx.instances[-1]
        assert not wx.listening and not wx.cbs  # 没确认之前不监听、不进群
        assert not e.core.db.kv("wxbind:ac_wx")
        assert not e.engine.bots["ac_wx"].online
        r = await ch.confirm_bind(True)
        assert r == {"ok": True} and ch.status == "online" and ch.pending is None
        b = e.core.db.kv("wxbind:ac_wx")
        assert b["wxid"] == "wxid_bot1" and b["nick"] == "小号"
        wx = Wx.instances[-1]  # 点「是」时为了真的重新读一遍身份，重新连了一次，监听挂在新连接上
        assert wx.listening and set(wx.cbs) == {"测试群"} and ch.wx is wx
        await ch.stop()
    env.run(body)


def test_first_start_confirm_no(env):
    async def body(e):
        reset_wx(nickname="作者大号", wxid="wxid_big")
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm"
        wx = Wx.instances[-1]
        r = await ch.confirm_bind(False)
        assert r == {"ok": False} and ch.status == "stopped" and "取消" in ch.detail
        assert not wx.listening and not wx.cbs and ch.wx is None  # 一个聊天都没监听过
        assert e.core.db.kv("wxbind:ac_wx") is None
        # 没在等确认时点确认会被拒
        try:
            await ch.confirm_bind(True)
            assert False
        except RuntimeError:
            pass
        await ch.stop()
    env.run(body)


def test_confirm_when_account_switched_meanwhile(env):
    async def body(e):
        reset_wx()
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm"
        reset_wx(nickname="作者大号", wxid="wxid_big")  # 用户还在看弹窗，微信里已经换成大号
        r = await ch.confirm_bind(True)
        assert r["ok"] is False and r["changed"] and ch.status == "error" and "变了" in ch.detail
        assert e.core.db.kv("wxbind:ac_wx") is None
        await ch.stop()
    env.run(body)


# ---------- 之后每次启动都核对 ----------
def test_next_start_same_identity_goes_online_and_other_refused(env):
    async def body(e):
        reset_wx()
        out = notes(e)
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        await ch.stop()
        # 再启动：身份一致，不再问
        ch = make(e)
        await ch.start()
        assert ch.status == "online" and ch.pending is None
        await ch.stop()
        # 昵称改了、微信号没变：照常上线，记下新昵称
        reset_wx(nickname="改了昵称", wxid="wxid_bot1")
        ch = make(e)
        await ch.start()
        assert ch.status == "online" and e.core.db.kv("wxbind:ac_wx")["nick"] == "改了昵称"
        await ch.stop()
        # 登录的是别的号：拒绝启动，不监听，发通知，说清楚是谁
        reset_wx(nickname="作者大号", wxid="wxid_big")
        ch = make(e)
        await ch.start()
        assert ch.status == "error"
        assert "当前登录的是 昵称 作者大号（微信号 wxid_big）" in ch.detail and "不是绑定的机器人小号" in ch.detail and ch.detail.endswith("已停止")
        wx = Wx.instances[-1]
        assert not wx.listening and not wx.cbs and ch.wx is None
        assert out and "作者大号" in out[-1]
        assert e.core.db.kv("wxbind:ac_wx")["wxid"] == "wxid_bot1"  # 绑定不动
        await ch.stop()
        # 什么都读不到：也拒绝
        reset_wx(nickname="", wxid="")
        ch = make(e)
        await ch.start()
        assert ch.status == "error" and "没法核对" in ch.detail
        await ch.stop()
    env.run(body)


# ---------- 运行中换号 ----------
def test_account_switched_while_running_stops_immediately(env):
    async def body(e):
        reset_wx()
        out = notes(e)
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        wx = Wx.instances[-1]
        assert await ch.recheck() is True and ch.status == "online"
        reset_wx(nickname="作者大号", wxid="wxid_big")
        assert await ch.recheck() is False
        assert ch.status == "error" and "作者大号" in ch.detail and "已停止" in ch.detail
        assert ch.wx is None and wx.removed == ["测试群"]  # 监听撤掉
        assert out and "作者大号" in out[-1]
        # 停了之后：监听线程里再来消息不处理，也发不出去
        before = len(e.engine.rooms.get("wechat:测试群").ctx) if "wechat:测试群" in e.engine.rooms else 0
        m = types.SimpleNamespace(attr="friend", type="text", content="@小号 在吗", sender="谁", id="z1")
        threading.Thread(target=ch._callback, args=(m, Chat("测试群"))).start()
        await asyncio.sleep(0.2)
        await e.engine.idle()
        assert (len(e.engine.rooms["wechat:测试群"].ctx) if "wechat:测试群" in e.engine.rooms else 0) == before
        try:
            await ch.send("group", "测试群", "你好")
            assert False
        except RuntimeError as ex:
            assert "已停止" in str(ex)
        await ch.stop()
    env.run(body)


def test_periodic_recheck_in_watch_loop(env):
    async def body(e):
        reset_wx()
        ch = make(e)
        ch.watch_secs, ch.idle_recheck_secs = 0.05, 0.0  # 加快：每 50ms 看一次、每次都复核（空闲时的单独复核）
        await ch.start()
        await ch.confirm_bind(True)
        await asyncio.sleep(0.2)
        assert ch.status == "online"
        reset_wx(nickname="作者大号", wxid="wxid_big")
        for _ in range(60):
            if ch.status != "online":
                break
            await asyncio.sleep(0.05)
        assert ch.status == "error" and "作者大号" in ch.detail
        await ch.stop()
    env.run(body)


def test_unreadable_during_run_is_not_treated_as_switch(env):
    async def body(e):
        reset_wx()
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        reset_wx(nickname="", wxid="")  # 界面正忙读不出来
        assert await ch.recheck() is True and ch.status == "online"
        await ch.stop()
    env.run(body)


def test_send_checks_identity_when_last_check_is_old(env):
    async def body(e):
        reset_wx()
        ch = make(e)
        await ch.start()
        await ch.confirm_bind(True)
        wx = Wx.instances[-1]
        await ch.send("group", "测试群", "好的")
        assert wx.chats["测试群"].sent == ["好的"]
        ch.verified_at -= 1000  # 隔了很久（比如电脑睡眠）
        reset_wx(nickname="作者大号", wxid="wxid_big")
        try:
            await ch.send("group", "测试群", "不该发出去")
            assert False
        except RuntimeError as ex:
            assert "不是绑定的小号" in str(ex)
        assert wx.chats["测试群"].sent == ["好的"] and ch.status == "error"
        await ch.stop()
    env.run(body)


# ---------- 只有昵称（读不到微信号）----------
def test_nickname_only_binding_and_nick_change(env):
    async def body(e):
        reset_wx(nickname="小号")  # 读不到微信号
        out = notes(e)
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm" and "读不到微信号" in ch.detail
        await ch.confirm_bind(True)
        assert e.core.db.kv("wxbind:ac_wx")["wxid"] == ""
        # 运行中昵称一变：当作对不上，立刻停
        reset_wx(nickname="小号改名")
        assert await ch.recheck() is False
        assert ch.status == "error" and "小号改名" in ch.detail and "重新启动并确认" in ch.detail and out
        await ch.stop()
        # 再启动：昵称对不上，要求用户重新确认（不是直接拒绝，也不是直接放行）
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm" and ch.pending["reason"] == "nick_changed" and ch.pending["old_nick"] == "小号"
        assert "昵称变了" in ch.detail
        assert not Wx.instances[-1].listening
        await ch.confirm_bind(True)
        assert ch.status == "online" and e.core.db.kv("wxbind:ac_wx")["nick"] == "小号改名"
        # 之后读到了微信号、昵称一致：放行并补记微信号
        reset_wx(nickname="小号改名", wxid="wxid_bot1")
        assert await ch.recheck() is True
        assert e.core.db.kv("wxbind:ac_wx")["wxid"] == "wxid_bot1"
        await ch.stop()
    env.run(body)


# ---------- 接口：账号视图、确认、重新绑定 ----------
def test_api_view_confirm_rebind_and_delete(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        reset_wx()
        ch = make(e)
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        await ch.start()
        accs = {a["id"]: a for a in (await (await c.get("/api/docs/account")).json())}
        v = accs["ac_wx"]
        assert v["status"] == "confirm" and v["pending"]["nick"] == "小号" and v["bound"] is None
        r = await c.post("/api/accounts/ac_wx/wechat_confirm", json={"ok": True})
        assert r.status == 200 and (await r.json())["ok"] is True
        v = (await (await c.get("/api/docs/account")).json())[0]
        assert v["status"] == "online" and v["bound"]["wxid"] == "wxid_bot1" and v["pending"] is None
        # 编辑账号时面板把 bound/pending 带回来：不会写进账号文档，也不会盖掉绑定
        v["bound"] = {"wxid": "旧", "nick": "旧"}
        v["name"] = "改个名"
        assert (await c.put("/api/docs/account", json=v)).status == 200
        assert "bound" not in e.core.db.doc("account", "ac_wx") and e.core.db.kv("wxbind:ac_wx")["wxid"] == "wxid_bot1"
        # 没在等确认时调用 → 400
        assert (await c.post("/api/accounts/ac_wx/wechat_confirm", json={"ok": True})).status == 400
        # 重新绑定：清掉绑定、重启账号，按当前登录的重新问
        reset_wx(nickname="新小号", wxid="wxid_bot2")
        r = await c.post("/api/accounts/ac_wx/wechat_rebind")
        assert r.status == 200
        await asyncio.sleep(0.3)
        v = (await (await c.get("/api/docs/account")).json())[0]
        assert v["status"] == "confirm" and v["bound"] is None and v["pending"]["wxid"] == "wxid_bot2"
        r = await c.post("/api/accounts/ac_wx/wechat_confirm", json={"ok": False})
        assert (await r.json())["ok"] is False
        v = (await (await c.get("/api/docs/account")).json())[0]
        assert v["status"] == "stopped" and v["bound"] is None
        # 删账号时绑定一起清
        e.core.db.set_kv("wxbind:ac_wx", {"wxid": "x", "nick": "y"})
        assert (await c.delete("/api/docs/account/ac_wx")).status == 200
        assert e.core.db.kv("wxbind:ac_wx") is None
        await c.close()
    env.run(body)


# ---------- 界面和文档里有对应入口 ----------
def test_ui_and_docs_mention_binding():
    root = Path(__file__).resolve().parent.parent
    js = (root / "web/js/pages-main.js").read_text(encoding="utf-8")
    for s in ("已绑定：", "重新绑定", "这是机器人小号吗", "wechat_confirm", "wechat_rebind"):
        assert s in js, s
    assert "confirm:" in (root / "web/js/core.js").read_text(encoding="utf-8")
    for doc in ("docs/使用说明.md", "docs/User-Guide.en.md"):
        t = (root / doc).read_text(encoding="utf-8")
        assert ("绑定" in t) if doc.endswith("说明.md") else ("bound" in t.lower())


# ---------- 补充八第 5 条：绑定时对照人物表 ----------
def put_person(e, pid, **kw):
    p = dict(id=pid, name=pid, identity="", call="", qq=[], discord=[], wechat_ids=[], wechat_remark=[], nicks={}, note="", knows="",
             attitude={"presets": [], "custom": ""})
    p.update(kw)
    e.core.db.put("person", p)
    e.engine.reload()


def test_people_wechat_hits(env):
    async def body(e):
        put_person(e, "pe_a", name="海豚", admin=True, wechat_ids=["Boss_Main_2000"])
        put_person(e, "pe_old", name="旧卡", wechat_id="OldId_1")  # 旧版的单个「微信号（备忘）」也认
        put_person(e, "pe_r", name="妈", wechat_remark=["秘密备注妈"])
        put_person(e, "pe_n", name="某人", nicks={"wechat": "某人的昵称"})
        put_person(e, "pe_bot", name="别的机器人", bot=True, wechat_ids=["wxid_other_bot"], nicks={"wechat": "小号"})
        h = e.engine.people.wechat_hits
        assert h("boss_main_2000", "") == [{"id": "pe_a", "name": "海豚", "admin": True, "by": ["微信号"]}]  # 不分大小写
        assert [x["name"] for x in h("oldid_1", "")] == ["旧卡"]
        assert h("", "秘密备注妈") == [{"id": "pe_r", "name": "妈", "admin": False, "by": ["秘密备注"]}]
        assert h("", "某人的昵称")[0]["by"] == ["昵称"]
        assert h("wxid_other_bot", "小号") == []  # 别的机器人自己的卡不算
        assert h("wxid_nobody", "没人认识") == [] and h("", "") == []
    env.run(body)


def test_binding_hit_needs_second_confirm_miss_needs_one(env):
    async def body(e):
        put_person(e, "pe_a", name="海豚", admin=True, wechat_ids=["Boss_Main_2000"])
        put_person(e, "pe_m", name="路人", wechat_remark=["某备注"])
        out = notes(e)
        # 命中（管理员的微信号）：带警告，只点一次「是」不行，必须再确认；点「不是」随时可以取消
        reset_wx(nickname="作者大号", wxid="Boss_Main_2000")
        ch = make(e)
        await ch.start()
        assert ch.status == "confirm"
        assert ch.pending["hits"] == [{"id": "pe_a", "name": "海豚", "admin": True, "by": ["微信号"]}]
        assert "这是人物表里「海豚」的号（管理员）" in out[-1] and "再确认一次" in out[-1]  # 警告在通知和确认框里；状态说明只写读到了谁
        try:
            await ch.confirm_bind(True)
            assert False
        except RuntimeError as ex:
            assert "再确认一次" in str(ex)
        assert ch.status == "confirm" and e.core.db.kv("wxbind:ac_wx") is None and not Wx.instances[-1].listening
        r = await ch.confirm_bind(True, again=True)
        assert r["ok"] and ch.status == "online" and e.core.db.kv("wxbind:ac_wx")["wxid"] == "Boss_Main_2000"
        await ch.stop()
        # 命中后点「取消」：什么都不记
        e.core.db.del_kv("wxbind:ac_wx")
        ch = make(e)
        await ch.start()
        assert (await ch.confirm_bind(False))["ok"] is False and ch.status == "stopped" and e.core.db.kv("wxbind:ac_wx") is None
        await ch.stop()
        # 命中秘密备注（非管理员）：同样要二次确认
        reset_wx(nickname="某备注", wxid="")
        ch = make(e)
        await ch.start()
        assert ch.pending["hits"][0]["by"] == ["秘密备注"] and not ch.pending["hits"][0]["admin"]
        await ch.stop()
        # 不命中：照常只确认一次，没有警告
        reset_wx(nickname="全新的小号", wxid="wxid_new")
        ch = make(e)
        await ch.start()
        assert ch.pending["hits"] == [] and "注意" not in out[-1]
        r = await ch.confirm_bind(True)
        assert r["ok"] and ch.status == "online"
        await ch.stop()
    env.run(body)


def test_api_second_confirm(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb.server import Server
        put_person(e, "pe_a", name="海豚", admin=True, wechat_ids=["Boss_Main_2000"])
        reset_wx(nickname="作者大号", wxid="Boss_Main_2000")
        ch = make(e)
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        await ch.start()
        v = (await (await c.get("/api/docs/account")).json())[0]
        assert v["pending"]["hits"][0]["name"] == "海豚"
        r = await c.post("/api/accounts/ac_wx/wechat_confirm", json={"ok": True})
        assert r.status == 400 and "再确认" in (await r.json())["error"]
        r = await c.post("/api/accounts/ac_wx/wechat_confirm", json={"ok": True, "again": True})
        assert r.status == 200 and (await r.json())["ok"] is True
        await c.close()
        await ch.stop()
    env.run(body)


def test_ui_has_warning_and_person_card_field():
    root = Path(__file__).resolve().parent.parent
    js = (root / "web/js/pages-main.js").read_text(encoding="utf-8")
    for s in ("这是人物表里「${x.name}」的号，确定要让机器人用它说话吗？", "（管理员），确定要让机器人用它说话吗？", "仍然绑定", "var(--bad)"):
        assert s in js, s
    ppl = (root / "web/js/pages-people.js").read_text(encoding="utf-8")
    assert "wechat_ids" in ppl and "绑定微信账号时的保险" in ppl


# ---------- 补充九：角色页常驻模板库 ----------
def test_template_library_api_and_roles_page(env):
    async def body(e):
        from aiohttp.test_utils import TestClient, TestServer

        from mb import templates
        from mb.server import Server
        c = TestClient(TestServer(Server(e.core).app))
        await c.start_server()
        lst = await (await c.get("/api/templates")).json()
        assert len(lst) == 6 and sum(len(x["items"]) for x in lst) == 28
        t = lst[1]["items"][0]
        pv = await (await c.get("/api/templates/" + t["id"])).json()  # 展开预览：人设全文
        assert pv["name"] == t["name"] and pv["shell"].strip() and pv["sections"] and all(s["title"] and s["body"] for s in pv["sections"])
        hp = await (await c.get("/api/templates/haitun")).json()  # 海豚娘的人设在种子文件里，预览也要有内容
        assert hp["shell"].strip() and hp["sections"] and hp["sections"][0]["body"].strip()
        for tpl in (x for cat in lst for x in cat["items"]):  # 28 个模板预览都不空
            q = await (await c.get("/api/templates/" + tpl["id"])).json()
            assert q["shell"].strip() or q["sections"], tpl["id"]
        assert (await c.get("/api/templates/不存在")).status == 404
        r = await c.post(f"/api/templates/{t['id']}/use", json={"name": "新来的"})  # 「用这个」：照现有逻辑复制成新角色
        assert r.status == 200 and (await r.json())["name"] == "新来的"
        assert e.core.db.doc("role", (await r.json())["id"])
        await c.close()
    env.run(body)


def test_roles_page_shows_library():
    root = Path(__file__).resolve().parent.parent
    main = (root / "web/js/pages-main.js").read_text(encoding="utf-8")
    new = (root / "web/js/pages-new.js").read_text(encoding="utf-8")
    assert "templateLibrary(" in main and "我的角色" in main and "scrollIntoView" in main  # 「从模板新建」滚动到模板库
    for s in ("角色模板库", "按名字搜索模板", "展开预览", "收起预览", "用这个", "没有符合的模板"):
        assert s in new, s
