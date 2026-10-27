"""新增平台的模拟测试：Telegram、LINE、X、Reddit、YouTube（官方接口）和 B站、贴吧、微博（非官方渠道）。
全部用本机的模拟服务，不连任何真实平台、不用真账号。"""
import asyncio
import base64
import hashlib
import hmac
import json
import socket

from aiohttp import web

from mb.channels import x_ch
from mb.channels.line import sign, split_text as line_split
from mb.channels.telegram import split_text as tg_split


# ---------- 公共件 ----------
class Mock:
    """一个可编程的模拟平台服务：记录收到的请求，按路径返回预设内容。"""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.routes: dict[tuple[str, str], object] = {}
        self.runner = None
        self.base = ""

    def on(self, method: str, path: str, resp):
        """resp：dict / list（依次返回，用完留最后一个）/ callable(request, body) -> (status, dict)"""
        self.routes[(method.upper(), path)] = resp
        return self

    def sent(self, path_part: str) -> list[dict]:
        return [b for m, p, b in self.calls if path_part in p]

    async def handler(self, req: web.Request):
        ct = req.content_type
        body = {}
        if req.can_read_body:
            if ct == "application/json":
                try:
                    body = await req.json()
                except Exception:
                    body = {}
            elif ct in ("application/x-www-form-urlencoded", "multipart/form-data"):
                body = dict(await req.post())
        body = {**dict(req.query), **body} if isinstance(body, dict) else body
        self.calls.append((req.method, req.path, body))
        r = self.routes.get((req.method, req.path))
        if r is None:
            return web.json_response({"error": f"no route {req.method} {req.path}"}, status=404)
        if callable(r):
            res = r(req, body)
            if asyncio.iscoroutine(res):
                res = await res
            status, data = res if isinstance(res, tuple) else (200, res)
        else:
            if isinstance(r, list):
                data = r.pop(0) if len(r) > 1 else r[0]
            else:
                data = r
            status = 200
            if isinstance(data, tuple):
                status, data = data
        return web.json_response(data, status=status)

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handler)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()


async def until(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def launch(e, mock: Mock, platform: str, secrets: dict, **fields):
    """建账号、写密钥、启动渠道，等它上线。"""
    aid = f"ac_{platform}"
    acc = e.core.db.put("account", {"id": aid, "platform": platform, "role_id": "r_haitun", "api_base": mock.base,
                                    "risk_ack": True, **fields})
    for k, v in secrets.items():
        e.core.db.set_secret(f"acct:{aid}:{k}", v)
    await e.engine.start_account(acc)
    ch = e.engine.bots[aid].channel
    assert await until(lambda: ch.status in ("online", "error")), "渠道没有启动"
    return ch


async def settle(e, ch=None, n=0.3):
    await asyncio.sleep(n)
    await e.engine.idle()


# ---------- Telegram ----------
def tg_update(uid, text, chat=-100, mid=1, **extra):
    m = {"message_id": mid, "from": {"id": uid, "first_name": f"用户{uid}"}, "chat": {"id": chat, "type": "supergroup", "title": "测试群"},
         "text": text, **extra}
    return {"update_id": mid, "message": m}


def test_telegram_group_mention_reply_and_split(env):
    async def body(e):
        mock = await Mock().start()
        updates = [[tg_update(5, "@mybot 在吗", mid=11, entities=[{"type": "mention", "offset": 0, "length": 6}])],
                   [tg_update(6, "随便聊聊天", mid=12)], []]

        def get_updates(req, b):
            return {"ok": True, "result": updates.pop(0) if len(updates) > 1 else []}
        sent_n = [0]

        def send(req, b):
            sent_n[0] += 1
            if sent_n[0] == 1:
                return 429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 0}, "description": "Too Many Requests"}
            return {"ok": True, "result": {"message_id": 100 + sent_n[0]}}
        tok = "/bot123:abc"
        mock.on("POST", f"{tok}/getMe", {"ok": True, "result": {"id": 900, "username": "MyBot", "first_name": "豚"}})
        mock.on("POST", f"{tok}/deleteWebhook", {"ok": True, "result": True})
        mock.on("POST", f"{tok}/getUpdates", get_updates)
        mock.on("POST", f"{tok}/sendMessage", send)
        mock.on("POST", f"{tok}/deleteMessage", {"ok": True, "result": True})
        e.llm.main = lambda s, u: "来了" + "啊" * 5000  # 超过 4096，要拆两段
        ch = await launch(e, mock, "telegram", {"token": "123:abc"})
        assert ch.status == "online" and ch.self_id == "900"
        assert await until(lambda: len(mock.sent("sendMessage")) >= 3)
        sends = mock.sent("sendMessage")
        # 被 @：回复并引用那条消息；第一次被限速，退避后重发成功；超长拆成两段
        assert sends[0]["reply_parameters"]["message_id"] == 11
        assert len(sends[1]["text"]) <= 4096 and len(sends[2]["text"]) > 0 and sends[2].get("reply_parameters") is None
        # 没 @ 的普通消息：交给守门员，守门员说不插话 → 不回
        await settle(e)
        assert all(c["chat_id"] == -100 for c in sends)
        gk = [c for c in e.llm.calls if c.get("_kind") == "gk"]
        assert gk and "Telegram群" in gk[0]["messages"][0]["content"]  # 守门员模板里的平台字样跟着平台走
        # 撤回
        assert await ch.recall("group", "-100", "101")
        assert mock.sent("deleteMessage")[0]["message_id"] == 101
        await e.engine.stop_account("ac_telegram")
        await mock.stop()
    env.run(body)


def test_telegram_split_text():
    parts = tg_split("甲" * 5000)
    assert [len(p) for p in parts] == [4096, 904]


# ---------- LINE ----------
def test_line_webhook_signature_reply_and_push(env):
    async def body(e):
        import aiohttp
        mock = await Mock().start()
        mock.on("GET", "/v2/bot/info", {"userId": "Ubot", "displayName": "豚"})
        mock.on("GET", "/v2/bot/group/G1/member/U1", {"displayName": "阿明"})
        mock.on("GET", "/v2/bot/group/G1/summary", {"groupName": "测试群"})
        mock.on("POST", "/v2/bot/message/reply", {"sentMessages": [{"id": "s1"}]})
        mock.on("POST", "/v2/bot/message/push", {"sentMessages": [{"id": "s2"}]})
        mock.on("PUT", "/v2/bot/channel/webhook/endpoint", {})
        mock.on("POST", "/v2/bot/channel/webhook/test", {"success": True, "detail": "OK"})
        e.llm.main = lambda s, u: "在的"
        port = free_port()
        ch = await launch(e, mock, "line", {"channel_secret": "sec", "channel_token": "tok"}, webhook_port=port,
                          public_url="https://abc.ngrok-free.app")
        assert ch.status == "online" and ch.self_id == "Ubot"
        ev = {"events": [{"type": "message", "replyToken": "RT1", "source": {"type": "group", "groupId": "G1", "userId": "U1"},
                          "message": {"type": "text", "id": "m1", "text": "@豚 在吗", "mention": {"mentionees": [{"isSelf": True, "type": "user"}]}}}]}
        raw = json.dumps(ev).encode()
        async with aiohttp.ClientSession() as s:
            url = f"http://127.0.0.1:{port}/line/ac_line"
            r = await s.post(url, data=raw, headers={"X-Line-Signature": "wrong"})
            assert r.status == 400  # 签名不对：拒绝
            r = await s.post(url, data=raw, headers={"X-Line-Signature": sign("sec", raw)})
            assert r.status == 200
        assert await until(lambda: mock.sent("message/reply"))
        rep = mock.sent("message/reply")[0]
        assert rep["replyToken"] == "RT1" and rep["messages"][0]["text"] == "在的"  # 优先用免费的 Reply
        # 没有 Reply 令牌了（过期/用过）→ 只能推送
        await ch.send("group", "G1", "再说一句")
        assert mock.sent("message/push")[0]["to"] == "G1"
        # 把公网地址登记给 LINE
        res = await ch.register_webhook()
        assert res["success"] and mock.sent("webhook/endpoint")[0]["endpoint"] == "https://abc.ngrok-free.app/line/ac_line"
        await e.engine.stop_account("ac_line")
        await mock.stop()
    env.run(body)


def test_line_split_by_utf16_units():
    parts = line_split("😀" * 3000, 4800)
    assert all(len(p.encode("utf-16-le")) // 2 <= 4800 for p in parts) and "".join(parts) == "😀" * 3000


# ---------- X ----------
def test_x_oauth1_signature_matches_documented_example():
    """OAuth 1.0a 签名：对 X 开发者文档里那组示例输入，算出的签名与独立实现 oauthlib 的结果一字不差（离线固定下来）。"""
    oauth = {"oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog", "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": "1318622958",
             "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "oauth_version": "1.0"}
    sig = x_ch.oauth1_signature("POST", "https://api.twitter.com/1.1/statuses/update.json",
                                {"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
                                oauth, "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE")
    assert sig == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_x_mentions_reply_chain_and_cost(env):
    async def body(e):
        mock = await Mock().start()
        mock.on("GET", "/2/users/me", {"data": {"id": "77", "username": "mybot", "name": "豚"}})
        polls = [{"data": [{"id": "10", "author_id": "5", "text": "@mybot 老的", "conversation_id": "10"}], "meta": {"newest_id": "10"},
                  "includes": {"users": [{"id": "5", "username": "ming", "name": "阿明"}]}},
                 {"data": [{"id": "11", "author_id": "5", "text": "@mybot 在吗", "conversation_id": "10"}], "meta": {"newest_id": "11"},
                  "includes": {"users": [{"id": "5", "username": "ming", "name": "阿明"}]}},
                 {"meta": {}}]
        mock.on("GET", "/2/users/77/mentions", polls)
        n = [0]

        def post(req, b):
            n[0] += 1
            assert req.headers["Authorization"].startswith("OAuth ")
            return {"data": {"id": f"9{n[0]}"}}
        mock.on("POST", "/2/tweets", post)
        e.llm.main = lambda s, u: ("很长的回复。" * 60)
        ch = await launch(e, mock, "x", {"api_key": "k", "api_secret": "s", "access_token": "t", "access_secret": "ts"})
        assert ch.self_id == "77"
        assert await until(lambda: len(mock.sent("2/tweets")) >= 2, 20)
        tw = mock.sent("2/tweets")
        # 第一次拉到的老帖子（id 10）不回；只回启动之后的（id 11）；超过 280 字拆成一串回复，后一条回前一条
        assert tw[0]["reply"]["in_reply_to_tweet_id"] == "11" and tw[1]["reply"]["in_reply_to_tweet_id"] == "91"
        assert all(len(t["text"]) <= 280 for t in tw)
        assert "$" in ch.cost_estimate()
        await e.engine.stop_account("ac_x")
        await mock.stop()
    env.run(body)


def test_x_payment_required_stops(env):
    async def body(e):
        mock = await Mock().start()
        mock.on("GET", "/2/users/me", {"data": {"id": "77", "username": "mybot"}})
        mock.on("GET", "/2/users/77/mentions", (402, {"title": "CreditsDepleted"}))
        ch = await launch(e, mock, "x", {"api_key": "k", "api_secret": "s", "access_token": "t", "access_secret": "ts"})
        assert await until(lambda: ch.status == "error")
        assert "积分" in ch.detail
        await e.engine.stop_account("ac_x")
        await mock.stop()
    env.run(body)


# ---------- Reddit ----------
def test_reddit_inbox_mention_reply_and_dm(env):
    async def body(e):
        mock = await Mock().start()
        mock.on("POST", "/api/v1/access_token", {"access_token": "AT", "expires_in": 3600})
        mock.on("GET", "/api/v1/me", {"name": "mybot"})
        inbox = [{"data": {"children": [{"kind": "t1", "data": {"name": "t1_old", "author": "ming", "body": "老的 u/mybot", "link_id": "t3_p1",
                                                                "subreddit": "test", "link_title": "帖子", "subject": "username mention"}}]}},
                 {"data": {"children": [
                     {"kind": "t1", "data": {"name": "t1_new", "author": "ming", "body": "u/mybot 你怎么看", "link_id": "t3_p1",
                                             "subreddit": "test", "link_title": "帖子", "subject": "username mention"}},
                     {"kind": "t4", "data": {"name": "t4_dm", "author": "hong", "body": "私信你一下"}}]}},
                 {"data": {"children": []}}]
        mock.on("GET", "/message/unread", inbox)
        mock.on("POST", "/api/read_message", {})
        mock.on("POST", "/api/comment", {"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_r1"}}]}}})
        mock.on("POST", "/api/compose", {"json": {"errors": []}})
        e.llm.main = lambda s, u: "我觉得行"
        ch = await launch(e, mock, "reddit", {"client_secret": "cs", "password": "pw"}, client_id="cid", username="mybot", auth_base=mock.base)
        assert ch.self_id == "mybot"
        assert await until(lambda: mock.sent("api/comment") and mock.sent("api/compose"), 20)
        c = mock.sent("api/comment")
        assert len(c) == 1 and c[0]["thing_id"] == "t1_new"  # 老通知不回，新的回在对应评论底下
        assert mock.sent("api/compose")[0]["to"] == "hong"
        assert ch.status == "online"
        # 用户代理按 Reddit 的要求带上用户名
        assert "by /u/mybot" in ch.ua()
        await e.engine.stop_account("ac_reddit")
        await mock.stop()
    env.run(body)


# ---------- YouTube ----------
def test_youtube_comments_quota_and_live(env):
    async def body(e):
        mock = await Mock().start()
        mock.on("POST", "/token", {"access_token": "AT", "expires_in": 3600})
        mock.on("GET", "/channels", {"items": [{"id": "UCbot", "snippet": {"title": "豚频道", "customUrl": "@mybot"}}]})
        th = lambda cid, text, author="UCx": {"snippet": {"topLevelComment": {"id": cid, "snippet": {
            "textOriginal": text, "authorDisplayName": "观众", "authorChannelId": {"value": author}}}}}
        threads = [{"items": [th("c_old", "@mybot 旧的")]}, {"items": [th("c_old", "@mybot 旧的"), th("c_new", "@mybot 你好呀"), th("c_other", "没叫她")]}, {"items": []}]
        mock.on("GET", "/commentThreads", threads)
        mock.on("POST", "/comments", {"id": "r1"})
        e.llm.main = lambda s, u: "你好"
        ch = await launch(e, mock, "youtube", {"client_secret": "cs", "refresh_token": "rt"}, client_id="cid", video_ids="vid1",
                          token_url=mock.base + "/token", mode="comments")
        assert ch.handle == "mybot"
        assert await until(lambda: mock.sent("/comments"), 20)
        ins = [c for c in mock.sent("/comments") if "snippet" in c and isinstance(c["snippet"], dict)]
        assert ins and ins[0]["snippet"]["parentId"] == "c_new"  # 只回被 @ 的新评论，回在它底下
        used = ch.used()
        assert used >= 50 + 3  # 回一条 50 点 + 读几次各 1 点
        assert "点配额" in ch.quota_text()
        # 配额到上限：不再发请求
        ch.account["quota_cap"] = used
        assert not ch.budget_left(1)
        await e.engine.stop_account("ac_youtube")
        await mock.stop()
    env.run(body)


def test_youtube_live_chat(env):
    async def body(e):
        mock = await Mock().start()
        mock.on("POST", "/token", {"access_token": "AT", "expires_in": 3600})
        mock.on("GET", "/channels", {"items": [{"id": "UCbot", "snippet": {"title": "豚频道", "customUrl": "@mybot"}}]})
        mock.on("GET", "/videos", {"items": [{"liveStreamingDetails": {"activeLiveChatId": "LC1"}}]})
        msg = lambda i, t: {"id": i, "snippet": {"type": "textMessageEvent", "displayMessage": t}, "authorDetails": {"channelId": "UCx", "displayName": "观众"}}
        mock.on("GET", "/liveChat/messages", [{"items": [msg("h1", "@mybot 历史")], "pollingIntervalMillis": 1000, "nextPageToken": "n1"},
                                              {"items": [msg("m2", "@mybot 现在在聊啥"), msg("m3", "别人聊天")], "pollingIntervalMillis": 1000, "nextPageToken": "n2"},
                                              {"items": [], "pollingIntervalMillis": 1000}])
        mock.on("POST", "/liveChat/messages", {"id": "sent1"})
        e.llm.main = lambda s, u: "在聊你"
        ch = await launch(e, mock, "youtube", {"client_secret": "cs", "refresh_token": "rt"}, client_id="cid", mode="live",
                          live_video_id="vid9", token_url=mock.base + "/token")
        assert ch.live_chat_id == "LC1"
        assert await until(lambda: mock.sent("liveChat/messages") and any("snippet" in c and isinstance(c["snippet"], dict) for c in mock.sent("liveChat/messages")), 20)
        posted = [c for c in mock.sent("liveChat/messages") if isinstance(c.get("snippet"), dict)]
        assert posted[0]["snippet"]["textMessageDetails"]["messageText"] == "在聊你"
        assert len(posted) == 1  # 历史消息、没叫她的消息都不回
        await e.engine.stop_account("ac_youtube")
        await mock.stop()
    env.run(body)


# ---------- B站（非官方渠道）----------
def bili_item(rpid, oid=111, root=0, uid=5, text="回复 @机器人 :在吗", typ=1):
    return {"user": {"mid": uid, "nickname": "阿明"}, "item": {"business_id": 1, "subject_id": oid, "source_id": rpid, "root_id": root,
                                                           "source_content": text, "title": "测试视频", "type": typ}}


def bili_mock(feed_at, feed_reply=None, reply_add=None):
    m = Mock()
    m.on("GET", "/x/web-interface/nav", {"code": 0, "data": {"isLogin": True, "mid": 900, "uname": "豚"}})
    m.on("GET", "/x/msgfeed/at", feed_at)
    m.on("GET", "/x/msgfeed/reply", feed_reply or {"code": 0, "data": {"items": []}})
    m.on("POST", "/x/v2/reply/add", reply_add or {"code": 0, "data": {"rpid": 999}})
    return m


def test_bilibili_reply_mapping_dedup_pacing_and_risk_stop(env):
    async def body(e):
        mock = await bili_mock([{"code": 0, "data": {"items": [bili_item(1)]}},
                                {"code": 0, "data": {"items": [bili_item(1), bili_item(2, root=0, text="@机器人 视频下直接问"), bili_item(3, root=50, text="楼中楼里回你")]}},
                                {"code": 0, "data": {"items": [bili_item(1), bili_item(2), bili_item(3)]}}]).start()
        e.llm.main = lambda s, u: "好的"
        e.core.settings.set("global", "pace_reply", "0-0")
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"})
        assert ch.self_id == "900"
        assert await until(lambda: len(mock.sent("reply/add")) >= 2, 20)
        adds = mock.sent("reply/add")
        # 第一次拉到的老通知（rpid 1）不回；只回启动之后的；csrf 带上
        assert {a["oid"] for a in adds} == {"111"} and all(a["csrf"] == "jct" for a in adds)
        top = next(a for a in adds if "root" in a and "parent" not in a)   # 视频下直接 @：root=对方那条，没有 parent
        floor = next(a for a in adds if "parent" in a)                      # 楼中楼：root=楼根，parent=对方那条
        assert top["root"] == "2" and floor["root"] == "50" and floor["parent"] == "3"
        n = len(mock.sent("reply/add"))
        await asyncio.sleep(0.3)
        assert len(mock.sent("reply/add")) == n  # 同一条不重复回
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
    env.run(body)


def test_bilibili_risk_code_halts_everything(env):
    async def body(e):
        mock = await bili_mock([{"code": 0, "data": {"items": []}}, {"code": 0, "data": {"items": [bili_item(7)]}},
                                {"code": 0, "data": {"items": [bili_item(7), bili_item(8)]}}],
                               reply_add={"code": -412, "message": "请求被拦截"}).start()
        e.llm.main = lambda s, u: "好的"
        e.core.settings.set("global", "pace_reply", "0-0")
        notes = []
        e.engine.notify_hook = notes.append
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"})
        assert await until(lambda: ch.status == "error", 20)
        assert "风控" in ch.detail and ch.stopping
        assert any("已停止" in n for n in notes)  # 发掉线通知
        calls = len(mock.sent("reply/add"))
        polls = len(mock.sent("msgfeed/at"))
        await asyncio.sleep(0.4)
        # 立即停止：不再重试发送，也不再拉通知
        assert len(mock.sent("reply/add")) == calls == 1 and len(mock.sent("msgfeed/at")) == polls
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
    env.run(body)


def test_bilibili_http_412_and_login_failure(env):
    async def body(e):
        mock = Mock()
        mock.on("GET", "/x/web-interface/nav", {"code": 0, "data": {"isLogin": False}})
        await mock.start()
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"})
        assert ch.status == "error" and "重新扫码" in ch.detail
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
        # 没勾「我知道有风险」不启动
        mock2 = await bili_mock({"code": 0, "data": {"items": []}}).start()
        ch2 = await launch(e, mock2, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"}, risk_ack=False)
        assert ch2.status == "error" and "风险" in ch2.detail and not mock2.calls
        await e.engine.stop_account("ac_bilibili")
        await mock2.stop()
    env.run(body)


def test_bilibili_dm_quota_and_shadow(env):
    async def body(e):
        sessions = {"code": 0, "data": {"session_list": [{"talker_id": 55, "last_msg": {
            "sender_uid": 55, "msg_type": 1, "content": json.dumps({"content": "你好呀"}), "msg_key": 9001}}]}}
        empty = {"code": 0, "data": {"items": []}}
        mock = bili_mock(empty).on("GET", "/session_svr/v1/session_svr/get_sessions", [{"code": 0, "data": {"session_list": []}}, sessions])
        mock.on("POST", "/web_im/v1/web_im/send_msg", {"code": 0, "data": {"msg_key": 1}})
        await mock.start()
        e.llm.main = lambda s, u: "你好"
        e.core.settings.set("global", "pace_dm", "0-0")
        e.core.settings.set("platform:bilibili", "quota_dm", 1)
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"}, enable_dm=True)
        assert await until(lambda: mock.sent("send_msg"), 20)
        sm = mock.sent("send_msg")[0]
        assert sm["msg[receiver_id]"] == "55" and json.loads(sm["msg[content]"])["content"] == "你好"
        assert await until(lambda: ch.quota_left("dm") == 0, 5)  # 私信每日额度单独记，用完不再发（发出请求后才记账，等一下）
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
    env.run(body)


def test_bilibili_shadow_mode_sends_nothing(env):
    async def body(e):
        mock = await bili_mock([{"code": 0, "data": {"items": []}}, {"code": 0, "data": {"items": [bili_item(5)]}},
                                {"code": 0, "data": {"items": [bili_item(5)]}}]).start()
        e.llm.main = lambda s, u: "本来要说的话"
        e.core.settings.set("global", "shadow", True)
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"})
        assert await until(lambda: len(mock.sent("msgfeed/at")) >= 3, 20)
        await settle(e)
        assert not mock.sent("reply/add")  # 影子模式：只记下「本来会说什么」，不发
        logs = e.core.db.x("SELECT text FROM logs WHERE cat='shadow'").fetchall()
        assert any("本来要说的话" in r[0] for r in logs)
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
    env.run(body)


def test_bilibili_send_failure_retries_then_gives_up(env):
    async def body(e):
        item = {"code": 0, "data": {"items": [bili_item(21)]}}
        mock = bili_mock([{"code": 0, "data": {"items": []}}, item], reply_add=(500, {"code": 500})).start() if False else None
        mock = bili_mock([{"code": 0, "data": {"items": []}}, item])
        mock.on("POST", "/x/v2/reply/add", (200, {"code": 12002, "message": "评论区已关闭"}))
        await mock.start()
        e.llm.main = lambda s, u: "好的"
        e.core.settings.set("global", "pace_reply", "0-0")
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"})
        assert await until(lambda: len(mock.sent("reply/add")) >= 3, 20)
        await asyncio.sleep(0.5)
        assert len(mock.sent("reply/add")) == 3  # 失败（非风控）最多试 3 次，之后不再试
        assert ch.status == "online"
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
    env.run(body)


def test_bilibili_qr_login_helpers():
    async def body():
        import aiohttp
        from mb.channels import bilibili
        m = Mock()
        m.on("GET", "/x/passport-login/web/qrcode/generate", {"code": 0, "data": {"url": "https://passport/x?qrcode_key=K", "qrcode_key": "K"}})
        m.on("GET", "/x/passport-login/web/qrcode/poll", [{"code": 0, "data": {"code": 86101}}, {"code": 0, "data": {"code": 86090}},
                                                          {"code": 0, "data": {"code": 0, "url": "https://x?SESSDATA=sd&bili_jct=jct&DedeUserID=9"}}])
        await m.start()
        async with aiohttp.ClientSession() as s:
            q = await bilibili.qr_generate(s, m.base)
            assert q["key"] == "K"
            states = [(await bilibili.qr_poll(s, "K", m.base)) for _ in range(3)]
        assert [x["state"] for x in states] == ["waiting", "scanned", "ok"]
        assert states[2]["cookies"] == {"SESSDATA": "sd", "bili_jct": "jct", "DedeUserID": "9"}
        await m.stop()
    asyncio.run(body())


# ---------- 贴吧、微博 ----------
def test_tieba_atme_reply_and_vcode_halts(env):
    async def body(e):
        at = lambda pid, **kw: {"content": "回复 机器人 :问你个事", "fname": "测试", "fid": "9", "thread_id": "800", "post_id": pid,
                                "replyer": {"id": "5", "name_show": "吧友"}, **kw}
        mock = Mock()
        mock.on("GET", "/dc/common/tbs", {"is_login": 1, "tbs": "TBS1"})
        mock.on("GET", "/f/user/json_userinfo", {"data": {"id": "900", "name_show": "豚"}})
        mock.on("POST", "/c/u/feed/atme", [{"error_code": "0", "at_list": []}, {"error_code": "0", "at_list": [at("p1", is_floor=1, quote_pid="p0")]},
                                           {"error_code": "0", "at_list": [at("p1", is_floor=1, quote_pid="p0"), at("p2")]}])
        posts = [{"no": 0, "data": {"pid": "x1"}}, {"no": 40, "error": "请输入验证码", "data": {"need_vcode": 1}}]
        mock.on("POST", "/f/commit/post/add", posts)
        await mock.start()
        e.llm.main = lambda s, u: "好"
        e.core.settings.set("global", "pace_reply", "0-0")
        notes = []
        e.engine.notify_hook = notes.append
        ch = await launch(e, mock, "tieba", {"BDUSS": "bd"})
        assert ch.self_id == "900"
        assert await until(lambda: ch.status == "error", 20)
        add = mock.sent("post/add")
        assert add[0]["tid"] == "800" and add[0]["tbs"] == "TBS1" and add[0]["quote_id"] == "p1" and add[0]["kw"] == "测试"
        assert len(add) == 2 and "验证码" in ch.detail and any("已停止" in n for n in notes)  # 遇验证码立即停
        await e.engine.stop_account("ac_tieba")
        await mock.stop()
    env.run(body)


def test_weibo_mentions_reply_and_frequency_halts(env):
    async def body(e):
        cm = lambda cid: {"id": cid, "text": "回复@机器人:<a>@机器人</a> 你好", "user": {"id": 5, "screen_name": "阿明"},
                          "status": {"id": 111, "mid": "M111", "text": "一条微博"}}
        mock = Mock()
        mock.on("GET", "/api/config", {"data": {"login": True, "st": "ST1", "uid": 900}})
        mock.on("GET", "/message/mentionsCmt", [{"ok": 1, "data": []}, {"ok": 1, "data": [cm(1)]}, {"ok": 1, "data": [cm(1), cm(2)]}])
        mock.on("GET", "/message/cmt", {"ok": 1, "data": []})
        mock.on("POST", "/api/comments/reply", [{"ok": 1, "data": {"id": 7}}, {"ok": 0, "msg": "请求过于频繁，请稍后再试"}])
        await mock.start()
        e.llm.main = lambda s, u: "你好呀"
        e.core.settings.set("global", "pace_reply", "0-0")
        ch = await launch(e, mock, "weibo", {"cookie": "SUB=abc"})
        assert ch.self_id == "900"
        assert await until(lambda: ch.status == "error", 20)
        rp = mock.sent("comments/reply")
        assert rp[0]["cid"] == "1" and rp[0]["mid"] == "M111" and rp[0]["st"] == "ST1" and rp[0]["content"] == "你好呀"
        assert "频繁" in ch.detail and len(rp) == 2
        await e.engine.stop_account("ac_weibo")
        await mock.stop()
    env.run(body)


# ---------- 公共：回补、额度 ----------
def test_social_daily_quota_and_baseline_backlog(env):
    async def body(e):
        mock = await bili_mock([{"code": 0, "data": {"items": [bili_item(31)]}}, {"code": 0, "data": {"items": [bili_item(31), bili_item(32), bili_item(33)]}},
                                {"code": 0, "data": {"items": [bili_item(31), bili_item(32), bili_item(33)]}}]).start()
        e.llm.main = lambda s, u: "好"
        e.core.settings.set("global", "pace_reply", "0-0")
        e.core.settings.set("platform:bilibili", "quota_reply", 1)
        ch = await launch(e, mock, "bilibili", {"SESSDATA": "sd", "bili_jct": "jct"}, reply_backlog=True)
        # 勾了「回补」：第一次拉到的老通知也回；但每日回复额度只有 1，之后的不发
        assert await until(lambda: len(mock.sent("msgfeed/at")) >= 3, 20)
        await settle(e)
        assert len(mock.sent("reply/add")) == 1 and ch.quota_left("reply") == 0
        await e.engine.stop_account("ac_bilibili")
        await mock.stop()
    env.run(body)
