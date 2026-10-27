"""定时查新模型、同系列新版本提醒、一键或私聊指令替换、暂不换和一直不换、「指令」总表。
服务商（模型列表、聊天、看图）和 models.dev 都用本机模拟服务；时钟用假的，不真等。"""
import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mb import commands, modelwatch, providers, update, visioncheck
from mb.server import Server


# ---------- 模拟服务 ----------
def raw_model(mid, vision=False, tools=True, ctx=128000):
    return {"id": mid, "name": mid, "modalities": {"input": ["text", "image"] if vision else ["text"]}, "tool_call": tools,
            "limit": {"context": ctx, "output": 8000}}


class Prov:
    """本机模拟的一家服务商：/models、/chat/completions（含看图），以及 models.dev 样子的 /api.json。"""

    def __init__(self):
        self.models = ["glm-5.3-flash", "glm-4.5-air"]
        self.list_fail = False
        self.chat_fail = set()
        self.vision = set()
        self.listed = 0
        self.used = []  # 被真调过的模型（按顺序）
        self.vis_used = []
        self.catalog_models = {}  # 联网更新目录时给出的清单

    async def list(self, req):
        self.listed += 1
        if self.list_fail:
            return web.Response(status=500, text="down")
        return web.json_response({"data": [{"id": m} for m in self.models]})

    async def chat(self, req):
        body = await req.json()
        m = body["model"]
        self.used.append(m)
        if m in self.chat_fail:
            return web.Response(status=400, text='{"error": "no such model"}')
        text = "好"
        if isinstance(body["messages"][-1]["content"], list):
            self.vis_used.append(m)
            text = "777" if m in self.vision else "看不清"
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    async def api(self, req):
        return web.json_response({"testprov": {"id": "testprov", "name": "测试商", "api": self.url, "env": [], "npm": "", "doc": "",
                                               "models": {k: raw_model(k, **v) for k, v in self.catalog_models.items()}}})


async def serve(prov):
    app = web.Application()
    app.router.add_get("/v1/models", prov.list)
    app.router.add_post("/v1/chat/completions", prov.chat)
    app.router.add_get("/api.json", prov.api)
    prov.runner = web.AppRunner(app)
    await prov.runner.setup()
    site = web.TCPSite(prov.runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    prov.url, prov.api_url = f"http://127.0.0.1:{port}/v1", f"http://127.0.0.1:{port}/api.json"


def seed_catalog(models: dict):
    """把目录直接写进数据目录里（不联网）：{模型id: {vision, tools, ctx}}。"""
    raw = {"testprov": {"id": "testprov", "name": "测试商", "api": "http://x", "env": [], "npm": "", "doc": "",
                        "models": {k: raw_model(k, **v) for k, v in models.items()}}}
    providers.user_catalog_path().write_text(json.dumps(providers.slim(raw)), encoding="utf-8")
    providers.catalog(reload=True)


class Clock:
    t = 1_000_000.0

    def time(self):
        return self.t


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


def setup(e, prov, models=("glm-5.3-flash",) * 4):
    """智谱这张卡片指向模拟服务商；主模型、守门员、记忆提取、看图都用它（模型按 models 顺序）。"""
    e.core.db.put("endpoint", {"id": "glm", "name": "智谱", "provider": "testprov", "protocol": "openai", "base_url": prov.url})
    e.core.db.set_secret("endpoint:glm", "k")
    for p, m in zip(("main", "gk", "mem", "vis"), models):
        setg(e, f"{p}_endpoint", "glm")
        setg(e, f"{p}_model", m)


def put_user(e, **kw):
    card = e.core.db.doc("person", "pe_user")
    e.core.db.put("person", {**card, "name": "用户", "self": True, **kw})
    e.engine.reload()


def notices(e):
    return [n["text"] for n in (e.core.db.kv("notices") or [])]


async def propose(e, prov, new="glm-5.5-flash"):
    """先记下现在的清单（第一次不提醒），服务商出了新模型，再查一次并提醒。"""
    w = e.engine.watch
    await w.check(force=True)
    prov.models.append(new)
    await w.check(force=True)
    w.remind()


def fixtures(monkeypatch, prov=None):
    clock = Clock()
    monkeypatch.setattr(modelwatch, "time", clock)
    monkeypatch.setattr(providers, "_cache", None)  # 目录缓存各测试各用各的
    monkeypatch.setattr(visioncheck, "random", SimpleNamespace(choice=lambda s: "7"))  # 看图测试的数字固定成 777，模拟服务商好回答
    return clock


# ---------- 同系列 ----------
def test_same_series_rules():
    n = modelwatch.newer_in_series
    assert n("glm-5.3-flash", "glm-5.5-flash")
    assert modelwatch.series("glm-5.3-flash")[0] == "glm-.-flash"
    assert not n("glm-5.5-flash", "glm-5.3-flash")  # 版本号更小
    assert not n("glm-5.3-flash", "glm-5.3-flash")  # 一样
    assert not n("glm-5.3-flash", "glm-5.5-air")  # 不是同系列
    assert n("glm-5", "glm-5.1") and n("gemini-2.5-flash", "gemini-3-flash")
    assert n("claude-opus-4-5", "claude-opus-4-6") and n("claude-opus-4-5-20250929", "claude-opus-4-6")  # 「4-5」当 4.5、日期后缀不看
    assert not n("qwen3-235b", "qwen3-480b")  # 版本数字不止一组：拿不准，不算
    assert not n("deepseek-flash", "deepseek-pro")  # 没有版本数字


def test_the_new_settings_are_global_system_with_the_agreed_texts():
    from mb.settings import BY_KEY
    a, b = BY_KEY["model_check_hours"], BY_KEY["model_snooze_days"]
    assert (a["default"], a["type"], a["cat"], a["scopes"]) == (24, "int", "系统", ["G"])
    assert (b["default"], b["type"], b["cat"], b["scopes"]) == (7, "int", "系统", ["G"])
    assert a["label"] == "多久查一次新模型（小时，0 = 不查）" and b["label"] == "回「不换」后多久再提醒（天）"
    assert a["help"] == "到点问一遍你配过的各家服务商现在有哪些模型（不经过搜索服务、不花 token），和上次比；你在用的模型出了同系列的新版本时提醒你，不会自动换"
    assert b["help"] == "私聊回「不换」或在面板点「暂不换」后，过这么多天再提醒同一个新模型；回「不换 30 天」这样带天数的按你说的算；回「一直不换」就不再提"


# ---------- 定时查、只记不提醒、同系列、只提醒一次 ----------
def test_check_waits_for_its_time_first_run_only_records_and_a_new_model_is_reminded_once(env, monkeypatch):
    async def body(e):
        clock = fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            setup(e, prov)
            seed_catalog({"glm-5.3-flash": dict(vision=False, ctx=128000), "glm-5.5-flash": dict(vision=True, ctx=1048576), "glm-4.5-air": {}})
            w = e.engine.watch
            await w.step()  # 启动后第一次：只开始计时，不查
            assert prov.listed == 0 and w.st()["last"] == clock.t
            clock.t += 3600
            await w.step()
            assert prov.listed == 0  # 没到点
            clock.t += 24 * 3600
            await w.step()
            assert prov.listed == 1 and not notices(e)  # 到点才查；第一次只记、不提醒
            clock.t += 24 * 3600 + 1
            await w.step()
            assert prov.listed == 2 and not notices(e)  # 没有新模型
            prov.models.append("glm-5.5-flash")
            clock.t += 24 * 3600 + 1
            await w.step()
            assert notices(e) == ["「智谱」出了新模型 glm-5.5-flash，你现在用的是 glm-5.3-flash（主模型、守门员、记忆提取、看图）。"
                                  "新模型还能看图。新模型能读的长度从 128K 变成 1M。"
                                  "私聊回我「换」就一起换掉；回「不换」过 7 天再提醒，回「一直不换」就不再提。也可以在面板 设置 → 模型与接口 里换。"]
            clock.t += 24 * 3600 + 1
            await w.step()
            await w.check(force=True)
            w.remind()
            assert len(notices(e)) == 1  # 同一个新模型只提醒一次
            setg(e, "model_check_hours", 0)
            clock.t += 99 * 24 * 3600
            n = prov.listed
            await w.step()
            assert prov.listed == n  # 0 = 不查
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_other_series_is_only_listed_and_an_older_version_is_not_reminded(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            setup(e, prov, ("glm-5.3-flash",) * 4)
            w = e.engine.watch
            await w.check(force=True)
            prov.models += ["glm-5.5-air", "glm-5.1-flash", "glm-vision-x"]
            await w.check(force=True)
            w.remind()
            assert not notices(e) and not w.st()["props"]  # 不同系列、版本号更小：都不提醒
            fresh = [f["id"] for f in w.view()["eps"]["glm"]["fresh"]]
            assert sorted(fresh) == ["glm-5.1-flash", "glm-5.5-air", "glm-vision-x"]  # 面板上列出这家新出的模型
            r = w.view()["eps"]["glm"]
            assert r["pending"] == [] and all(f["ts"] for f in r["fresh"])
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_when_the_list_cannot_be_fetched_the_catalog_is_refreshed_and_used(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            setup(e, prov)
            prov.list_fail = True
            orig = providers.refresh

            async def local(proxy=None):
                return await orig(proxy, prov.api_url)  # models.dev 换成本机模拟服务
            monkeypatch.setattr(providers, "refresh", local)
            prov.catalog_models = {"glm-5.3-flash": {}, "glm-4.5-air": {}}
            w = e.engine.watch
            await w.check(force=True)
            assert any(k.endswith("|cat") for k in w.st()["seen"]) and not notices(e)  # 目录里的清单记下来，不提醒
            prov.catalog_models["glm-5.5-flash"] = {}
            await w.check(force=True)
            w.remind()
            assert len(notices(e)) == 1 and "出了新模型 glm-5.5-flash" in notices(e)[0]
            assert prov.listed >= 2  # 每次都先问服务商
        finally:
            await prov.runner.cleanup()
    env.run(body)


# ---------- 能力差别 ----------
def test_ability_differences_follow_the_catalog(monkeypatch):
    monkeypatch.setattr(providers, "_cache", None)
    from pathlib import Path
    import tempfile, os
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        monkeypatch.setenv("PODMATES_DATA", d)
        seed_catalog({"a": dict(vision=False, tools=True, ctx=128000), "b": dict(vision=True, tools=True, ctx=128000),
                      "c": dict(vision=False, tools=False, ctx=1048576), "nodata": dict(ctx=0)})
        ab = modelwatch.abilities
        assert ab("testprov", "a", "b") == "新模型还能看图（要拿它看图就回「看图也换」）。"  # 多了看图
        assert ab("testprov", "b", "a") == "注意：新模型不能看图。"  # 少了看图
        assert ab("testprov", "a", "c") == "注意：新模型不能用工具。新模型能读的长度从 128K 变成 1M。"  # 长度变了
        assert ab("testprov", "a", "nodata") == "" and ab("testprov", "a", "unknown") == ""  # 目录里没有资料的不写
        assert ab("testprov", "a", "a") == ""


# ---------- 不换、一直不换 ----------
def test_not_now_snooze_default_days_n_days_never_and_panel_undo(env, monkeypatch):
    async def body(e):
        clock = fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            setup(e, prov)
            bot = e.add_bot("900")
            put_user(e, qq=["12345"])
            w = e.engine.watch
            await propose(e, prov)
            assert len(notices(e)) == 1
            await e.say("12345", "不换", private=True)
            assert bot.sent[-1][2] == "好，先不换，7 天后再提醒 glm-5.5-flash。"
            clock.t += 6 * 86400
            w.remind()
            assert len(notices(e)) == 1  # 没到 7 天
            clock.t += 86400 + 1
            w.remind()
            assert len(notices(e)) == 2 and "出了新模型 glm-5.5-flash" in notices(e)[0]  # 7 天后再提醒一次
            w.remind()
            assert len(notices(e)) == 2
            await e.say("12345", "不换 30 天", private=True)
            assert bot.sent[-1][2] == "好，先不换，30 天后再提醒 glm-5.5-flash。"
            clock.t += 29 * 86400
            w.remind()
            assert len(notices(e)) == 2
            clock.t += 86400 + 1
            w.remind()
            assert len(notices(e)) == 3
            await e.say("12345", "不换30天", private=True)
            assert bot.sent[-1][2] == "好，先不换，30 天后再提醒 glm-5.5-flash。"
            c = TestClient(TestServer(Server(e.core).app))
            await c.start_server()
            v = await (await c.get("/api/models/watch")).json()
            sn = v["eps"]["glm"]["snoozed"][0]
            assert sn["new"] == "glm-5.5-flash" and sn["until"] == clock.t + 30 * 86400
            # 面板撤销：马上恢复提醒
            r = await (await c.post("/api/models/watch/revoke", json={"key": sn["key"]})).json()
            assert r["ok"] and len(notices(e)) == 4
            # 一直不换（「别再提」同样）：之后不再提醒
            await e.say("12345", "一直不换", private=True)
            assert bot.sent[-1][2] == "好，以后不再提 glm-5.5-flash。面板里可以撤销。"
            clock.t += 400 * 86400
            w.remind()
            assert len(notices(e)) == 4
            v = await (await c.get("/api/models/watch")).json()
            nv = v["eps"]["glm"]["never"][0]
            r = await (await c.post("/api/models/watch/revoke", json={"key": nv["key"]})).json()
            assert r["ok"] and len(notices(e)) == 5  # 撤销后恢复提醒
            await e.say("12345", "别再提", private=True)
            assert bot.sent[-1][2] == "好，以后不再提 glm-5.5-flash。面板里可以撤销。"
            # 一直不换的新模型，再查一次也不会重新当成新提议
            await w.check(force=True)
            w.remind()
            assert len(notices(e)) == 5
            await c.close()
        finally:
            await prov.runner.cleanup()
    env.run(body)


# ---------- 替换 ----------
def scoped_setup(e, prov):
    setup(e, prov)
    setg(e, "main_model", "glm-5.3-flash", "role:r_haitun")  # 角色：沿用全局的服务商，要换
    setg(e, "main_endpoint", "default", "platform:qq")
    setg(e, "main_model", "glm-5.3-flash", "platform:qq")  # 平台：生效的服务商是别家，不换
    setg(e, "main_endpoint", "glm", "chat:qq:100")
    setg(e, "main_model", "glm-5.3-flash", "chat:qq:100")  # 群：自己指明是这家，要换
    setg(e, "gk_model", "other-model", "role:r_haitun|chat:qq:200")  # 别的模型，不动
    setg(e, "backup_endpoint", "glm")
    setg(e, "backup_model", "glm-5.3-flash")  # 备用：要换


def test_replace_tests_first_then_changes_every_scope_whose_effective_provider_is_this_one(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            scoped_setup(e, prov)
            prov.vision.add("glm-5.5-flash")
            w = e.engine.watch
            r = await w.replace("glm", "glm-5.3-flash", "glm-5.5-flash")
            assert r["ok"] and r["text"] == ("已把 glm-5.3-flash 换成 glm-5.5-flash（主模型、守门员、记忆提取、看图、备用，共 7 处）。"
                                             "想换回去就私聊我「换模型 glm-5.5-flash glm-5.3-flash」。")
            assert prov.used == ["glm-5.5-flash", "glm-5.5-flash"] and prov.vis_used == ["glm-5.5-flash"]  # 先真调一次，看图那处再测看图
            s = e.core.settings
            for sc, k in (("global", "main_model"), ("global", "gk_model"), ("global", "mem_model"), ("global", "vis_model"),
                          ("role:r_haitun", "main_model"), ("chat:qq:100", "main_model"), ("global", "backup_model")):
                assert s.data[sc][k] == "glm-5.5-flash", (sc, k)
            assert s.data["platform:qq"]["main_model"] == "glm-5.3-flash"  # 生效的服务商是别家：不动
            assert s.data["role:r_haitun|chat:qq:200"]["gk_model"] == "other-model"
            assert any("替换模型" in x["text"] for x in e.core.db.rows("SELECT text FROM logs"))
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_a_failed_call_changes_nothing_and_says_why(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            scoped_setup(e, prov)
            prov.chat_fail.add("glm-5.5-flash")
            before = json.dumps(e.core.settings.data, sort_keys=True)
            r = await e.engine.watch.replace("glm", "glm-5.3-flash", "glm-5.5-flash")
            assert not r["ok"] and not r["vision_failed"] and r["text"].startswith("没换：用 glm-5.5-flash 真调了一次没成功（HTTP 400")
            assert json.dumps(e.core.settings.data, sort_keys=True) == before
            assert prov.vis_used == []  # 基本调用没过，不再往下测
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_vision_failure_changes_nothing_then_replace_what_can_be_replaced_keeps_vision(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            bot = e.add_bot("900")
            put_user(e, qq=["12345"])
            setup(e, prov)
            await propose(e, prov)
            await e.say("12345", "换能换的", private=True)  # 还没有因为看图没过而没换：当普通聊天
            assert bot.sent[-1][2] == "好"
            before = json.dumps(e.core.settings.data, sort_keys=True)
            await e.say("12345", "换", private=True)  # 新模型看不了图：看图那处没过，一处都不换
            assert bot.sent[-1][2] == ("没换：glm-5.5-flash 没通过看图测试（读不出测试图）。要只换看图以外的几处，就回「换能换的」（看图仍用 glm-5.3-flash）。")
            assert json.dumps(e.core.settings.data, sort_keys=True) == before
            view = e.engine.watch.view()["eps"]["glm"]["pending"][0]
            assert view["partial"] is True  # 面板上出现「只换能换的」
            await e.say("12345", "换能换的", private=True)
            assert bot.sent[-1][2] == "已把 glm-5.3-flash 换成 glm-5.5-flash（主模型、守门员、记忆提取，共 3 处）；看图仍用 glm-5.3-flash，因为新模型看不了图。"
            s = e.core.settings
            assert [s.g(f"{p}_model") for p in ("main", "gk", "mem", "vis")] == ["glm-5.5-flash"] * 3 + ["glm-5.3-flash"]
            await e.say("12345", "换能换的", private=True)  # 已经换完：不再有效，当普通聊天
            assert bot.sent[-1][2] == "好"
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_replace_vision_too_tests_first_and_never_turns_vision_on(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            bot = e.add_bot("900")
            put_user(e, qq=["12345"])
            setup(e, prov, ("glm-5.3-flash", "glm-5.3-flash", "glm-5.3-flash", "glm-4.5-air"))  # 看图用的是别的模型
            seed_catalog({"glm-5.3-flash": dict(vision=False), "glm-5.5-flash": dict(vision=True), "glm-4.5-air": {}})
            await propose(e, prov)
            await e.say("12345", "看图也换", private=True)  # 看图没过：不换
            assert bot.sent[-1][2].startswith("没换：glm-5.5-flash 没通过看图测试")
            assert e.core.settings.g("vis_model") == "glm-4.5-air"
            prov.vision.add("glm-5.5-flash")
            await e.say("12345", "看图也换", private=True)
            assert bot.sent[-1][2] == "已把看图换成 glm-5.5-flash。看图现在是关着的，要用就在面板打开。"
            assert e.core.settings.g("vis_model") == "glm-5.5-flash" and e.core.settings.g("vision_enabled") is False  # 不替用户打开
            assert e.core.settings.g("main_model") == "glm-5.3-flash"  # 只换看图
            await e.say("12345", "看图也换", private=True)  # 看图已经是新模型：当普通聊天
            assert bot.sent[-1][2] == "好"
            setg(e, "vision_enabled", True)
            e.core.settings.set("global", "vis_model", "glm-4.5-air")
            await e.say("12345", "看图也换", private=True)
            assert bot.sent[-1][2] == "已把看图换成 glm-5.5-flash。"  # 看图本来开着：不再提醒
            assert prov.vis_used.count("glm-5.5-flash") == 3
        finally:
            await prov.runner.cleanup()
    env.run(body)


# ---------- 私聊指令 ----------
def test_private_commands_only_for_the_user_card_and_replies_are_verbatim(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            bot = e.add_bot("900")
            put_user(e, qq=["12345"])
            setup(e, prov)
            prov.vision.add("glm-5.5-flash")
            await e.say("12345", "换", private=True)  # 没有待回复的提议：当普通聊天
            assert bot.sent[-1][2] == "好"
            for t in ("不换", "一直不换", "别再提", "看图也换", "换能换的"):
                await e.say("12345", t, private=True)
                assert bot.sent[-1][2] == "好", t
            await propose(e, prov)
            before = json.dumps(e.core.settings.data, sort_keys=True)
            for t in ("换", "不换", "一直不换", "换模型 glm-5.3-flash glm-5.5-flash", "查更新"):  # 别人说同样的话：照常聊天
                await e.say("55555", t, private=True)
                assert bot.sent[-1][:2] == ("private", "55555") and bot.sent[-1][2] == "好", t
            assert json.dumps(e.core.settings.data, sort_keys=True) == before and e.engine.watch.pending()["state"] == ""
            await e.say("12345", " 换 ", private=True)  # 前后空白不算
            assert bot.sent[-1][2] == ("已把 glm-5.3-flash 换成 glm-5.5-flash（主模型、守门员、记忆提取、看图，共 4 处）。"
                                       "想换回去就私聊我「换模型 glm-5.5-flash glm-5.3-flash」。")
            await e.say("12345", "换模型 glm-5.5-flash glm-5.3-flash", private=True)  # 换回去
            assert bot.sent[-1][2].startswith("没换：glm-5.3-flash 没通过看图测试") or bot.sent[-1][2].startswith("已把 glm-5.5-flash 换成 glm-5.3-flash")
            await e.say("12345", "换模型 glm-5.5-flash glm-9", private=True)
            assert bot.sent[-1][2] == "没换：glm-9 不在「智谱」的模型列表里。"
            await e.say("12345", "换模型 没人用的 glm-9", private=True)
            assert bot.sent[-1][2] == "没换：现在没有地方在用 没人用的。"
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_swap_command_replaces_on_the_provider_that_lists_the_new_model(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            bot = e.add_bot("900")
            put_user(e, qq=["12345"])
            setup(e, prov)
            prov.models.append("glm-5.5-flash")
            prov.vision.add("glm-5.5-flash")
            await e.say("12345", "换模型 glm-5.3-flash glm-5.5-flash", private=True)
            assert bot.sent[-1][2] == ("已把 glm-5.3-flash 换成 glm-5.5-flash（主模型、守门员、记忆提取、看图，共 4 处）。"
                                       "想换回去就私聊我「换模型 glm-5.5-flash glm-5.3-flash」。")
            assert e.core.settings.g("vis_model") == "glm-5.5-flash" and prov.used[0] == "glm-5.5-flash"
        finally:
            await prov.runner.cleanup()
    env.run(body)


def test_check_updates_command_reports_models_and_version_without_installing(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            bot = e.add_bot("900")
            put_user(e, qq=["12345"])
            setup(e, prov)
            seed_catalog({"glm-5.3-flash": dict(vision=False), "glm-5.5-flash": dict(vision=False)})
            info = {"latest": "1.4.3", "url": "http://x/r", "notes": "", "newer": False, "checked": 0, "current": "1.4.3"}

            async def fake(repo, proxy=None, api=None):
                return dict(info)
            monkeypatch.setattr(update, "check", fake)
            setg(e, "update_check", False)  # 设置里关了每天的检查，指令照样查
            await e.say("12345", "查更新", private=True)
            assert bot.sent[-1][2] == "「智谱」第一次查，先记下现在的 2 个模型，以后有新的再告诉你。\n新版本：已是最新（1.4.3）。"
            prov.models.append("glm-5.5-flash")
            info.update(latest="9.9.9", newer=True)
            await e.say("12345", "查更新", private=True)
            lines = [x for x in bot.sent if not x[2].startswith("【机器人通知】")][-1][2].split("\n")  # 新版本的通知（照旧只提醒一次）不算回复
            assert notices(e) == ["有新版本 9.9.9（当前 1.4.3）：http://x/r"]
            assert lines[0] == "「智谱」新出了 1 个模型：glm-5.5-flash。"
            assert lines[1].startswith("「智谱」出了新模型 glm-5.5-flash，你现在用的是 glm-5.3-flash（主模型、守门员、记忆提取、看图）。")
            assert lines[-1] == "新版本：有新版本 9.9.9（当前 1.4.3）：http://x/r（只提醒，不会自动下载或安装）"
            assert [n for n in notices(e) if "出了新模型" in n] == []  # 提议写在回复里，不再另发一条通知
            e.engine.watch.remind()
            assert [n for n in notices(e) if "出了新模型" in n] == []
            assert e.engine.watch.pending()["new"] == "glm-5.5-flash"  # 但算已经提醒过：「换」「不换」对它有效
        finally:
            await prov.runner.cleanup()
    env.run(body)


# ---------- 「指令」「帮助」总表 ----------
def test_help_lists_only_what_this_person_can_use_in_a_group(env):
    async def body(e):
        bot = e.add_bot("900")
        e.core.settings.set("global", "cmd_perms", {"mute": {"mode": "all"}, "status": {"mode": "admins"}})
        await e.say("70001", "指令", at=["900"])  # 普通群友：「闭嘴」开给了所有人，再加上人人能用的「忘掉我」
        assert bot.sent[-1][2] == "你能用的指令：\n闭嘴 N 分钟：让她安静 N 分钟（不写就是 30 分钟）\n忘掉我：删掉关于你自己的记忆和你挂的提醒"
        await e.say("70002", "帮助", at=["900"], role="admin")  # 群管理员：再加上「mute」（所有人）和「status」（群主和管理员）
        assert bot.sent[-1][2] == ("你能用的指令：\n闭嘴 N 分钟：让她安静 N 分钟（不写就是 30 分钟）\n状态：看她现在的状态\n"
                                   "忘掉我：删掉关于你自己的记忆和你挂的提醒")
        await e.say("90001", "指令", at=["900"])  # 人物表里的管理员：全部，只是「忘掉我」不适用
        lines = bot.sent[-1][2].split("\n")
        assert lines[0] == "你能用的指令：" and len(lines) == 1 + 9 and not any(x.startswith("忘掉我") for x in lines)
        assert "指令" not in bot.sent[-1][2].replace("你能用的指令：", "").replace("指令清单", "")
        n = len(bot.sent)
        await e.say("70001", "指令")  # 没 @ 她：不是指令
        assert len(bot.sent) == n
    env.run(body)


def test_help_in_private_differs_for_the_user_card_and_others(env):
    async def body(e):
        bot = e.add_bot("900")
        put_user(e, qq=["12345"])
        await e.say("12345", "指令", private=True)
        lines = bot.sent[-1][2].split("\n")
        assert lines[0] == "你能用的指令：" and len(lines) == 1 + len(commands.PRIVATE_HELP)
        assert "查更新：现在查一次新模型和新版本" in lines and not any(x.startswith("闭嘴") for x in lines)
        await e.say("55555", "帮助", private=True)  # 别人私聊：没有可用的指令
        assert bot.sent[-1][:3] == ("private", "55555", "你在这里没有可用的指令")
        await e.say("55555", "指令", private=True)
        assert bot.sent[-1][2] == "你在这里没有可用的指令"
    env.run(body)


def test_command_table_covers_every_command_with_where_and_who():
    rows = commands.all_rows()
    says = [r["say"] for r in rows]
    for need in ("闭嘴 N 分钟", "忘掉我", "指令 / 帮助", "换", "换能换的", "看图也换", "不换 / 不换 N 天", "一直不换 / 别再提", "换模型 旧 新", "查更新"):
        assert need in says, need
    assert all(r["where"] and r["who"] and r["use"] for r in rows)
    assert {r["where"] for r in rows if r["say"] == "换"} == {"私聊她"} and {r["who"] for r in rows if r["say"] == "换"} == {"只有用户卡本人"}


# ---------- 面板接口 ----------
def test_panel_check_now_and_one_click_replace(env, monkeypatch):
    async def body(e):
        fixtures(monkeypatch)
        prov = Prov()
        await serve(prov)
        try:
            setup(e, prov)
            prov.vision.add("glm-5.5-flash")
            c = TestClient(TestServer(Server(e.core).app))
            await c.start_server()
            r = await (await c.post("/api/models/watch/check")).json()
            assert r["text"] == "「智谱」第一次查，先记下现在的 2 个模型，以后有新的再告诉你。"
            prov.models.append("glm-5.5-flash")
            r = await (await c.post("/api/models/watch/check")).json()
            assert r["text"] == "「智谱」新出了 1 个模型：glm-5.5-flash。" and len(notices(e)) == 1
            v = await (await c.get("/api/models/watch")).json()
            p = v["eps"]["glm"]["pending"][0]
            assert p["old"] == "glm-5.3-flash" and p["new"] == "glm-5.5-flash"
            r = await (await c.post("/api/models/watch/replace", json={"key": p["key"]})).json()
            assert r["ok"] and r["text"].startswith("已把 glm-5.3-flash 换成 glm-5.5-flash（主模型、守门员、记忆提取、看图，共 4 处）")
            assert e.core.settings.g("main_model") == "glm-5.5-flash"
            v = await (await c.get("/api/models/watch")).json()
            assert v["eps"]["glm"]["pending"] == []
            r = await (await c.post("/api/models/watch/replace", json={"key": p["key"]})).json()
            assert not r["ok"]  # 已经换完，提议不在了
            tbl = (await (await c.get("/api/commands")).json())["table"]
            assert tbl == commands.all_rows()
            boot = await (await c.get("/api/bootstrap")).json()
            assert boot["command_table"] == commands.all_rows()
            await c.close()
        finally:
            await prov.runner.cleanup()
    env.run(body)
