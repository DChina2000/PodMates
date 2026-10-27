"""面板来源地址防护（开了局域网访问后只接受本机 / 局域网 / Tailscale，公网要另外打开）；发言占比：按条数或按比例，样本少时不拦可开关，旧数据迁移。"""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from mb import pacing
from mb.engine import Entry
from mb.server import Server, addr_kind, hash_pw


class Peer:
    def __init__(self, ip):
        self.ip = ip

    def get_extra_info(self, name, default=None):
        return (self.ip, 1234) if name == "peername" else default


async def ask(srv, ip, path="/api/state"):
    req = make_mocked_request("GET", path, transport=Peer(ip))

    async def ok(r):
        return web.json_response({"ok": True})
    r = await srv.auth(req, ok)
    return r.status


def test_address_kinds():
    local = ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"]
    private = ["10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.254", "192.168.0.1", "192.168.255.254", "fc00::1", "fdab:1::2", "fe80::1", "::ffff:192.168.1.5"]
    cgnat = ["100.64.0.1", "100.100.100.100", "100.127.255.254"]
    public = ["8.8.8.8", "172.32.0.1", "172.15.255.255", "100.63.255.255", "100.128.0.1", "11.0.0.1", "192.169.0.1", "1.1.1.1",
              "2001:db8::1", "2606:4700::1111", "::ffff:8.8.8.8", "", "not-an-ip"]
    assert {addr_kind(x) for x in local} == {"local"} and {addr_kind(x) for x in private} == {"private"}
    assert {addr_kind(x) for x in cgnat} == {"cgnat"} and {addr_kind(x) for x in public} == {"public"}
    assert addr_kind(None) == "public"


def test_panel_only_accepts_local_lan_and_tailscale_unless_public_is_allowed(env):
    async def body(e):
        srv = Server(e.core)
        s = e.core.settings
        e.core.db.set_secret("panel_password", hash_pw("pw"))
        # 没开局域网访问：只有本机
        assert await ask(srv, "127.0.0.1") == 200 and await ask(srv, "192.168.1.5") == 403 and await ask(srv, "100.64.0.1") == 403
        s.set("global", "panel_lan", True)
        for ip in ("127.0.0.1", "::1", "10.1.2.3", "172.16.0.9", "192.168.1.5", "fd12::1", "fe80::1", "100.64.0.1", "100.127.255.254"):
            assert await ask(srv, ip) == 200, ip
        for ip in ("8.8.8.8", "172.32.0.1", "100.128.0.1", "2001:db8::1", "203.0.113.9"):
            assert await ask(srv, ip) == 403, ip
        # 所有请求都查，不只是登录：页面、静态文件、别的接口
        for path in ("/", "/api/login", "/api/bootstrap", "/js/core.js", "/api/events"):
            assert await ask(srv, "8.8.8.8", path) == 403, path
        # 打开「也允许公网访问面板」：公网地址放行（仍然要密码）
        s.set("global", "panel_public", True)
        assert await ask(srv, "8.8.8.8") == 200
        assert await ask(srv, "8.8.8.8", "/api/bootstrap") == 401  # 没登录，要密码
        s.set("global", "panel_public", False)
        assert await ask(srv, "8.8.8.8") == 403
        # 开着局域网访问但没设密码：照旧只有本机
        e.core.db.set_secret("panel_password", "")
        assert await ask(srv, "192.168.1.5") == 403 and await ask(srv, "127.0.0.1") == 200
    env.run(body)


def test_lan_requests_still_need_the_password_and_denials_are_logged_once(env):
    async def body(e):
        srv = Server(e.core)
        e.core.db.set_secret("panel_password", hash_pw("pw"))
        e.core.settings.set("global", "panel_lan", True)
        assert await ask(srv, "192.168.1.5", "/api/bootstrap") == 401  # 局域网来的，没登录：要密码
        assert await ask(srv, "100.64.0.1", "/api/bootstrap") == 401
        for _ in range(5):
            assert await ask(srv, "8.8.8.8") == 403
        lines = [r["text"] for r in e.core.db.rows("SELECT text FROM logs") if "拒绝了来自 8.8.8.8" in r["text"]]
        assert len(lines) == 1 and "也允许公网访问面板" in lines[0]  # 同一个地址十分钟内只记一条
        assert await ask(srv, "1.1.1.1") == 403
        assert len([r for r in e.core.db.rows("SELECT text FROM logs") if "拒绝了来自" in r["text"]]) == 2
    env.run(body)


def test_public_switch_is_a_global_setting_off_by_default():
    from mb.settings import BY_KEY
    s = BY_KEY["panel_public"]
    assert s["default"] is False and s["scopes"] == ["G"] and s["when"] == {"panel_lan": True}
    assert "明文" in s["help"] and "Tailscale" in s["help"] and "整个互联网" in s["help"]


# ---------- 发言占比 ----------
def ctx(n_bot, n_other):
    """先 n_other 条群友的话、再 n_bot 条机器人的话（机器人的是最近的）。"""
    return [Entry("111", "群友", f"话{i}") for i in range(n_other)] + [Entry("900", "bot", f"回{i}", bot="ac_900") for i in range(n_bot)]


def test_by_count_blocks_at_the_seventh_not_the_sixth():
    c = lambda n_bot: ctx(n_bot, 15 - n_bot)
    assert not pacing.share_blocked(c(6), 15, "count", 7) and pacing.share_blocked(c(7), 15, "count", 7)
    assert not pacing.share_blocked(c(0), 15, "count", 7) and pacing.share_blocked(c(12), 15, "count", 7)
    # 只数最近 window 条：更早的不算
    old_bots = [Entry("900", "bot", "旧", bot="ac_900") for _ in range(10)] + [Entry("111", "群友", f"新{i}") for i in range(15)]
    assert not pacing.share_blocked(old_bots, 15, "count", 7)
    # 一次回复拆成几行，按行算条数
    lines = [Entry("111", "群友", "问")] * 8 + [Entry("900", "bot", f"行{i}", bot="ac_900") for i in range(7)]
    assert pacing.share_blocked(lines, 15, "count", 7)
    # 最多几条可调
    assert pacing.share_blocked(c(3), 15, "count", 3) and not pacing.share_blocked(c(2), 15, "count", 3)


def test_by_percent_uses_the_rounded_percentage():
    c = lambda n_bot, n_other: ctx(n_bot, n_other)
    # 47% 和 15 条里 7 条差不多：7/15 = 46.7% 四舍五入是 47
    assert pacing.share_blocked(c(7, 8), 15, "percent", 7, 47) and not pacing.share_blocked(c(6, 9), 15, "percent", 7, 47)
    assert pacing.share_blocked(c(6, 9), 15, "percent", 7, 40) and not pacing.share_blocked(c(5, 10), 15, "percent", 7, 40)  # 40%：6/15 到顶
    assert pacing.share_blocked(c(12, 28), 40, "percent", 7, 30) and not pacing.share_blocked(c(11, 29), 40, "percent", 7, 30)
    # 按比例时不看「最多几条」
    assert not pacing.share_blocked(c(10, 90), 100, "percent", 7, 47)
    assert pacing.share_blocked(c(0, 0), 15, "percent", 7, 0) is False  # 没有消息：不拦


def test_few_messages_switch():
    five_bots = ctx(5, 0)
    # 关（默认）：按比例时 5 条全是机器人 = 100%，拦
    assert pacing.share_blocked(five_bots, 15, "percent", 7, 47, min_on=False)
    # 开：样本不满 min(15, 10) 条不拦；N 可调
    assert not pacing.share_blocked(five_bots, 15, "percent", 7, 47, min_on=True, min_n=10)
    assert pacing.share_blocked(five_bots, 15, "percent", 7, 47, min_on=True, min_n=5)
    assert pacing.share_blocked(ctx(10, 0), 15, "percent", 7, 47, min_on=True, min_n=10)
    # 窗口比 N 还小：门槛是窗口（min(窗口, N)），不会永远拦不住
    assert pacing.share_blocked(ctx(8, 0), 8, "percent", 7, 47, min_on=True, min_n=10)
    # 按条数也适用
    assert not pacing.share_blocked(ctx(7, 1), 15, "count", 7, min_on=True, min_n=10) and pacing.share_blocked(ctx(7, 1), 15, "count", 7, min_on=False)


def test_defaults_and_engine_wiring(env):
    from mb.settings import BY_KEY

    async def body(e):
        s = e.core.settings
        g = s.g
        assert (g("share_limit_on"), g("share_window"), g("share_mode"), g("share_max_count"), g("share_max"), g("share_min_on"), g("share_min_n")) == (True, 15, "count", 7, 47, False, 10)
        assert BY_KEY["share_max_count"]["when"] == {"share_mode": "count"} and BY_KEY["share_max"]["when"] == {"share_mode": "percent"} and BY_KEY["share_min_n"]["when"] == {"share_min_on": True}
        b = e.add_bot("900")
        eng = e.engine
        room = eng.room("qq", "100", "群100")
        room.accounts |= {"ac_900"}
        bot = eng.bots["ac_900"]
        s.set("global", "cooldown_seconds", 0)
        s.set("global", "reply_min_gap", 0)
        for i in range(9):
            room.ctx.append(Entry("111", "群友", f"话{i}"))
        for i in range(6):
            room.ctx.append(Entry("900", "bot", f"回{i}", bot="ac_900"))
        assert eng.proactive_ok(bot, room, sample=False)  # 最近 15 条里 6 条：不拦
        room.ctx.append(Entry("900", "bot", "回6", bot="ac_900"))
        assert not eng.proactive_ok(bot, room, sample=False)  # 第 7 条到顶：拦（最近 15 条里 7 条）
        s.set("global", "share_max_count", 8)
        assert eng.proactive_ok(bot, room, sample=False)
        s.set("global", "share_mode", "percent")  # 按比例：8 条里 ... 现在是 7/15 = 47%
        s.set("global", "share_max", 47)
        assert not eng.proactive_ok(bot, room, sample=False)
        s.set("global", "share_max", 48)
        assert eng.proactive_ok(bot, room, sample=False)
        # 某个群单独改成按条数、最多 3 条
        s.set("chat:qq:100", "share_mode", "count")
        s.set("chat:qq:100", "share_max_count", 3)
        assert not eng.proactive_ok(bot, room, sample=False)
    env.run(body)


def test_migration_keeps_the_old_percent_behaviour(env):
    async def body(e):
        core, s = e.core, e.core.settings
        # 升级前的数据：全局设过 25%、窗口 40；一个角色设过 60%；一个群只调过窗口
        core.db.del_kv("share_mode_v1")
        for sc in list(s.data):
            for k in ("share_mode", "share_min_on", "share_max", "share_window", "share_max_count"):
                s.data[sc].pop(k, None)
        core.db.x("DELETE FROM settings WHERE key LIKE 'share_%'")
        s.set("global", "share_max", 25)
        s.set("global", "share_window", 40)
        s.set("role:r_haitun", "share_max", 60)
        s.set("chat:qq:200", "share_window", 20)
        core.migrate_share_mode()
        assert s.get("share_mode") == "percent" and s.get("share_min_on") is True and s.get("share_max") == 25 and s.get("share_window") == 40
        assert s.get("share_mode", "r_haitun") == "percent" and s.get("share_max", "r_haitun") == 60
        # 只调过窗口的那一层：沿用原来的默认 40%
        assert s.get("share_mode", platform="qq", chat="qq:200") == "percent" and s.get("share_max", platform="qq", chat="qq:200") == 40
        # 效果和以前一样：旧规则（n_bot*100 >= 百分比*n，样本不满 min(窗口,10) 不拦）
        for n_bot, n in ((12, 40), (11, 40), (10, 40), (3, 5), (3, 10)):
            entries = ctx(n_bot, n - n_bot)
            old = n >= min(40, 10) and n_bot * 100 >= 25 * n
            new = pacing.share_blocked(entries, 40, s.get("share_mode"), s.get("share_max_count"), s.get("share_max"), s.get("share_min_on"), s.get("share_min_n"))
            assert new == old, (n_bot, n)
        assert core.db.kv("share_mode_v1") is not None
        s.set("global", "share_mode", "count")  # 之后用户自己改的不会被覆盖：只跑一次
        core.migrate_share_mode()
        assert s.get("share_mode") == "count"
    env.run(body)


def test_fresh_install_has_nothing_to_migrate_and_uses_new_defaults(env, tmp_path):
    from mb.core import Core
    core = Core(tmp_path)
    try:
        assert core.db.kv("share_mode_v1")["scopes"] == 0
        assert core.settings.g("share_mode") == "count" and core.settings.g("share_max_count") == 7
    finally:
        core.engine.close()
        core.db.close()
