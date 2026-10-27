"""模拟环境：模拟的模型服务 + 3 个模拟的 QQ 账号（OneBot 正向 WebSocket，代替 NapCat）+ 持续的群消息。
不连任何真号、不调真模型。用于截图演示和性能实测。

  python tools/simulate.py setup <数据目录>      # 往数据目录里写好演示配置（面板密码 1234）
  python tools/simulate.py run [每秒消息数] [秒数]  # 起模拟服务并持续推群消息
"""
import asyncio
import json
import random
import sys
import time
from pathlib import Path

from aiohttp import web

LLM_PORT = 18901
BOTS = [("90001", "r_haitun", "海豚娘小号"), ("90002", "r_b", "乙角色小号"), ("90003", "r_owl", "猫头鹰小号")]
OB_PORTS = [18911, 18912, 18913]
GROUPS = {100: "模拟测试群", 200: "考研互助群"}
HUMANS = [("90009", "测试昵称"), ("10001", "阿明"), ("10002", "小红"), ("10003", "老王"),
          ("10004", "摸鱼大师"), ("10005", "今天也想下班")]
LINES = ["今天这破天气谁顶得住", "有人看昨晚的比赛吗", "拉康说欲望是他者的欲望，那我想下班是谁的欲望", "晚饭吃啥",
         "这题我真不会", "笑死", "＋1", "谁懂啊，又加班", "刚面试回来，感觉凉了", "推荐个好看的番",
         "有没有人一起考研", "猫又把杯子推下去了", "周末去爬山吗", "这个梗我没看懂", "早上好", "绷不住了"]
REPLIES = ["本豚表示不服", "这波我站你", "问就是摸鱼", "（尾巴甩了一下）这个我熟", "你先别急", "哈哈哈哈这个可以",
           "说人话就是：想下班", "我妈说得对", "建议直接躺平", "这不就是大他者在凝视你吗"]


# ---------- 模拟模型 ----------
async def llm(req):
    body = await req.json()
    last = body["messages"][-1]["content"]
    await asyncio.sleep(random.uniform(0.2, 0.8))
    if "只输出JSON：" in last:
        yes = random.random() < 0.25
        text = json.dumps({"brief": "有梗可接" if yes else "别人聊得正热", "reply": yes}, ensure_ascii=False)
    elif "请整理成以后聊天用得上的记忆" in last:
        text = json.dumps({"recap": "大家聊了天气、考研和晚饭，阿明说自己在考研，小红推荐了番。",
                           "people": [{"uid": "10001", "name": "阿明", "note": "在准备考研", "source": "self"},
                                      {"uid": "10002", "name": "小红", "note": "爱看番，推荐过好几部", "source": "inferred"}],
                           "memes": [{"keyword": "下班", "origin": "老王天天喊想下班，成了群梗"}]}, ensure_ascii=False)
    elif "合并成一段更概括的前情" in last:
        text = "这几天群里主要在聊考研和加班，阿明在备考，老王天天想下班。"
    else:
        text = random.choice(REPLIES)
    return web.json_response({"choices": [{"message": {"role": "assistant", "content": text}}],
                              "usage": {"prompt_tokens": random.randint(1500, 2600), "completion_tokens": random.randint(15, 60),
                                        "prompt_tokens_details": {"cached_tokens": random.randint(800, 1400)},
                                        "completion_tokens_details": {"reasoning_tokens": random.randint(0, 40)}}})


# ---------- 模拟 OneBot（每个账号一个）----------
class OneBot:
    def __init__(self, uid, name, hub):
        self.uid, self.name, self.hub = uid, name, hub
        self.clients: list[web.WebSocketResponse] = []
        self.n = 0

    async def handle(self, req):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)
        self.clients.append(ws)
        try:
            async for m in ws:
                a = json.loads(m.data)
                data, act, p = None, a["action"], a.get("params", {})
                if act == "get_login_info":
                    data = {"user_id": int(self.uid), "nickname": self.name}
                elif act == "get_group_list":
                    data = [{"group_id": g, "group_name": n} for g, n in GROUPS.items()]
                elif act in ("send_group_msg", "send_private_msg"):
                    self.n += 1
                    data = {"message_id": int(self.uid) * 1000 + self.n}
                    if act == "send_group_msg":
                        text = "".join(s["data"].get("text", "") for s in p["message"] if s["type"] == "text")
                        asyncio.create_task(self.hub.echo(self, p["group_id"], text))
                elif act == "get_msg":
                    data = {"sender": {"user_id": int(self.uid)}}
                await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": a.get("echo")}))
        finally:
            self.clients.remove(ws)
        return ws

    async def push(self, ev):
        for ws in list(self.clients):
            try:
                await ws.send_str(json.dumps(ev, ensure_ascii=False))
            except Exception:
                pass


class Hub:
    def __init__(self):
        self.bots = [OneBot(uid, name, self) for uid, _, name in BOTS]
        self.seq = 0

    def event(self, gid, uid, name, segs):
        self.seq += 1
        return {"post_type": "message", "message_type": "group", "group_id": gid, "group_name": GROUPS[gid],
                "user_id": int(uid), "message_id": self.seq, "message_seq": self.seq, "real_seq": self.seq, "time": int(time.time()),
                "message": segs, "sender": {"nickname": name, "card": "", "role": "member"}}

    async def broadcast(self, ev, skip=None):
        for b in self.bots:
            if b is not skip:
                ev = dict(ev, self_id=int(b.uid))
                await b.push(ev)

    async def echo(self, sender, gid, text):
        """机器人发的消息，群里其他账号也会收到（和真 QQ 一样）。"""
        await self.broadcast(self.event(gid, sender.uid, sender.name, [{"type": "text", "data": {"text": text}}]), skip=sender)

    async def human(self):
        gid = random.choice(list(GROUPS))
        uid, name = random.choice(HUMANS)
        segs = []
        if random.random() < 0.12:
            segs.append({"type": "at", "data": {"qq": random.choice(BOTS)[0]}})
        segs.append({"type": "text", "data": {"text": random.choice(LINES)}})
        await self.broadcast(self.event(gid, uid, name, segs))


async def run(rate: float, seconds: float):
    app = web.Application()
    app.router.add_post("/v4/chat/completions", llm)
    app.router.add_get("/v4/models", lambda r: web.json_response({"data": [{"id": "mock-model"}]}))
    runners = []
    r = web.AppRunner(app)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", LLM_PORT).start()
    runners.append(r)
    hub = Hub()
    for b, port in zip(hub.bots, OB_PORTS):
        a = web.Application()
        a.router.add_get("/", b.handle)
        rr = web.AppRunner(a)
        await rr.setup()
        await web.TCPSite(rr, "127.0.0.1", port).start()
        runners.append(rr)
    print(f"模拟服务已启动：模型 {LLM_PORT}，OneBot {OB_PORTS}；每秒 {rate} 条，持续 {seconds} 秒", flush=True)
    end = time.time() + seconds
    n = 0
    while time.time() < end:
        if any(b.clients for b in hub.bots) and rate > 0:
            await hub.human()
            n += 1
        await asyncio.sleep(1 / rate if rate > 0 else 1)
    print(f"共推送 {n} 条群消息", flush=True)


def setup(data: str):
    import os
    os.environ["PODMATES_DATA"] = data
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mb.core import Core
    from mb.server import hash_pw
    core = Core(Path(data))
    db = core.db
    db.set_secret("panel_password", hash_pw("1234"))
    db.set_kv("setup_done", True)
    db.set_kv("platforms", {"qq": True, "wechat": False, "discord": True, "telegram": True})
    from mb import templates
    ep = db.doc("endpoint", "default")
    ep["base_url"] = f"http://127.0.0.1:{LLM_PORT}/v4"
    db.put("endpoint", ep)
    db.set_secret("endpoint:default", "mock-key")
    # 演示用的第二、三个服务商卡片（不连真地址，只为截图）
    db.put("endpoint", dict(id="ep_zhipu", name="智谱 AI（开放平台）", provider="zhipuai", protocol="openai", base_url="https://open.bigmodel.cn/api/paas/v4",
                            key_env="ZHIPU_API_KEY", use_proxy=False))
    db.put("endpoint", dict(id="ep_claude", name="Anthropic", provider="anthropic", protocol="anthropic", base_url="https://api.anthropic.com",
                            key_env="ANTHROPIC_API_KEY", use_proxy=True))
    db.set_kv("ep_status", {"default": {"ok": True, "ts": time.time(), "n": 2}, "ep_claude": {"ok": False, "ts": time.time(), "msg": "超时（国内需要代理）"}})
    templates.instantiate(db, "haitun", name="海豚娘", role_id="r_haitun", persona_id="p_海豚娘")
    templates.instantiate(db, "gentle-friend", name="乙角色", role_id="r_b", persona_id="p_乙角色")
    db.put("role", dict(id="r_b", name="乙角色", persona="p_乙角色", summary="温和的群友。", attitudes={}, relations={"r_haitun": "朋友"}))
    templates.instantiate(db, "deadpan", name="猫头鹰", role_id="r_owl", persona_id="p_猫头鹰")
    db.put("person", dict(id="pe_owner", name="测试昵称", identity="管理员", call="", qq=["90009"], discord=[], wechat_remark=[], nicks={"qq": "测试昵称"},
                          note="", knows="", attitude={"presets": ["亲近贴心"], "custom": ""}, admin=True, groups=[]))
    core.engine.reload()
    for (uid, rid, name), port in zip(BOTS, OB_PORTS):
        db.put("account", {"id": f"ac_{uid}", "platform": "qq", "qq": uid, "role_id": rid, "name": name, "enabled": True,
                           "manage_napcat": False, "ws_port": port})
        db.set_secret(f"onebot:ac_{uid}", "")
    db.put("account", {"id": "ac_discord", "platform": "discord", "role_id": "r_haitun", "name": "海豚娘（Discord）", "enabled": False})
    db.put("account", {"id": "ac_tg", "platform": "telegram", "role_id": "r_b", "name": "乙角色（Telegram）", "enabled": False})
    db.put("account", {"id": "ac_bili", "platform": "bilibili", "role_id": "r_owl", "name": "猫头鹰（B站小号）", "enabled": False})
    for i, (uid, name) in enumerate(HUMANS[1:4]):
        db.put("person", {"id": f"pe_{uid}", "name": name, "identity": "", "call": "", "qq": [uid], "discord": [], "wechat_id": "",
                          "wechat_remark": [], "note": "", "knows": "", "attitude": {"presets": [["互怼损友", "尊敬客气", ""][i]] if i < 2 else [], "custom": ""},
                          "admin": False, "groups": ["g_friends"] if i < 2 else []})
    db.put("pgroup", {"id": "g_friends", "name": "老朋友", "attitude": {"presets": ["互怼损友"], "custom": ""}})
    core.settings.set("global", "memory_segment", 30)
    # 排班演示：群分组 + 三条规则
    db.put("cgroup", {"id": "g_fun", "name": "闲聊群"})
    db.put("chat", {"id": "qq:100", "platform": "qq", "chat_id": "100", "name": GROUPS[100], "desc": "", "attitude": None,
                    "schedules": [], "cgroups": ["g_fun"]})
    db.put("chat", {"id": "qq:200", "platform": "qq", "chat_id": "200", "name": GROUPS[200], "desc": "考研互助，正经为主",
                    "attitude": None, "schedules": []})
    db.put("rule", {"id": "rl_fest", "name": "万圣节：全员猫头鹰", "on": True, "date_from": "2026-10-31", "date_to": "2026-11-01",
                    "mode": "fixed", "role": "r_owl"})
    db.put("rule", {"id": "rl_work", "name": "工作日白天：考研群由乙角色值班", "on": True, "accounts": ["ac_90001"],
                    "chats": ["qq:200"], "weekdays": [0, 1, 2, 3, 4], "time_from": "09:00", "time_to": "18:00",
                    "mode": "fixed", "role": "r_b"})
    db.put("rule", {"id": "rl_night", "name": "周末夜里闲聊群轮换", "on": True, "chat_groups": ["g_fun"], "weekdays": [4, 5],
                    "time_from": "22:00", "time_to": "02:00", "mode": "rotate", "roles": ["r_haitun", "r_b"],
                    "order": "random", "every": "day"})
    core.settings.set("chat:qq:200", "eagerness", 2)
    print("演示配置已写入", data)


if __name__ == "__main__":
    if sys.argv[1] == "setup":
        setup(sys.argv[2])
    else:
        asyncio.run(run(float(sys.argv[2]) if len(sys.argv) > 2 else 1.0, float(sys.argv[3]) if len(sys.argv) > 3 else 3600))
