"""资源监控：每小时一行日志；内存（commit 连续 5 分钟 ≥ 90%）和硬盘（< 5GB）告急才提醒，一天一次，恢复后再出问题重新提醒，总开关关了照常记录。读数全用桩。"""
import time

from mb import monitor
from mb.monitor import GB

T0 = 1_800_000_000.0  # 钟（秒）；日期部分用 time.localtime 算，所以按天测试时加 86400


class Rig:
    def __init__(self, e, monkeypatch):
        self.e, self.mp = e, monkeypatch
        self.commit = 50.0  # 百分比
        self.free_gb = 30.0
        self.mp.setattr(monitor, "read_memory", lambda: {"phys_avail": 3 * GB, "phys_total": 4 * GB, "commit_limit": 8 * GB, "commit_used": int(8 * GB * self.commit / 100)})
        self.mp.setattr(monitor, "read_disk", lambda path: {"free": int(self.free_gb * GB), "total": 50 * GB, "drive": "C:"})
        self.mp.setattr(monitor, "top_processes", lambda n=3: [("big.exe", 11, 2 * GB), ("QQ.exe", 12, GB), ("python.exe", 13, GB // 2)])
        self.mp.setattr(monitor, "count_processes", lambda: {"napcat": 1, "qq": 2, "wechat": 0})
        self.mp.setattr(monitor, "dir_size", lambda p: 3 * GB)

    @property
    def mon(self):
        return self.e.core.monitor

    def notices(self):
        return [n["text"] for n in (self.e.core.db.kv("notices") or [])]

    def logs(self, cat):
        return [r["text"] for r in self.e.core.db.rows("SELECT text FROM logs WHERE cat=? ORDER BY id", (cat,))]


async def minutes(rig, start, n, commit=None):
    """每分钟走一步，共 n 分钟（从 start 起）；返回结束时刻。"""
    t = start
    for _ in range(n):
        if commit is not None:
            rig.commit = commit
        await rig.mon.step(t)
        t += 60
    return t


def test_one_line_per_hour_with_everything_in_it(env, monkeypatch):
    async def body(e):
        e.add_bot("901")
        e.core.db.put("account", {"id": "ac_dead", "platform": "wechat", "name": "微信小号", "role_id": "r_haitun", "enabled": False})
        rig = Rig(e, monkeypatch)
        await rig.mon.step(T0)  # 第一步就记一行
        await rig.mon.step(T0 + 1800)
        await rig.mon.step(T0 + 3599)
        assert len(rig.logs("resource")) == 1
        await rig.mon.step(T0 + 3600)  # 过了一小时再记一行
        lines = rig.logs("resource")
        assert len(lines) == 2
        line = lines[0]
        for need in ("剩余物理内存 3.0 GB", "已用虚拟内存 50%（4.0 GB / 8.0 GB）", "系统盘 C: 剩余 30.0 GB", "901（qq）在线", "微信小号（wechat）没在线", "NapCat 启动器 1 个、QQ 2 个、微信 0 个"):
            assert need in line, need
        # 面板日志页能按「资源」类别筛出来
        assert e.core.db.x("SELECT COUNT(*) FROM logs WHERE cat='resource'").fetchone()[0] == 2
        assert "resource: '资源'" in open("web/js/pages-main.js", encoding="utf-8").read()
    env.run(body)


def test_memory_alert_needs_five_minutes_over_the_line_and_a_dip_restarts_the_timer(env, monkeypatch):
    async def body(e):
        rig = Rig(e, monkeypatch)
        t = await minutes(rig, T0, 3, commit=95)  # 3 分钟（T0、+1、+2 分钟）在线以上
        assert rig.notices() == []
        t = await minutes(rig, t, 1, commit=60)  # 跌回去：重新计时
        t = await minutes(rig, t, 5, commit=95)  # 又超线，但 5 个读数只覆盖 4 分钟
        assert rig.notices() == []
        await rig.mon.step(t)  # 第 5 分钟：连续 5 分钟了
        n = rig.notices()
        assert len(n) == 1 and "内存告急" in n[0] and "95%" in n[0] and "连续 5 分钟" in n[0] and "big.exe" in n[0] and "QQ.exe" in n[0]  # 当前数值和占用最多的进程
        assert "剩余物理内存 3.0 GB" in n[0]
        # 低于线（比如 89%）不提醒
        rig2_commit = 89
        e.core.monitor.over_since = None
        e.core.monitor.alerted["mem"] = 0
        await minutes(rig, t + 3600, 8, commit=rig2_commit)
        assert len(rig.notices()) == 1
    env.run(body)


def test_disk_alert_below_five_gb(env, monkeypatch):
    async def body(e):
        rig = Rig(e, monkeypatch)
        rig.free_gb = 5.0  # 刚好 5GB 不算
        await rig.mon.step(T0)
        assert rig.notices() == []
        rig.free_gb = 4.9
        await rig.mon.step(T0 + 3600)
        n = rig.notices()
        assert len(n) == 1 and "硬盘告急" in n[0] and "C:" in n[0] and "4.9 GB" in n[0] and "3.0 GB" in n[0] and "big.exe" in n[0]
    env.run(body)


def test_same_alert_once_a_day_and_again_after_recovery(env, monkeypatch):
    async def body(e):
        rig = Rig(e, monkeypatch)
        rig.free_gb = 1.0
        for h in range(5):  # 硬盘一直不够：同一天里每小时都查，只提醒一次
            await rig.mon.step(T0 + h * 3600)
        assert len(rig.notices()) == 1
        await rig.mon.step(T0 + 86400 + 7200)  # 隔天还没好：再提醒一次
        assert len(rig.notices()) == 2
        # 恢复正常后再出问题：算新的一次（哪怕还是同一天）
        rig.free_gb = 40.0
        await rig.mon.step(T0 + 86400 + 7200 + 3600)
        rig.free_gb = 2.0
        await rig.mon.step(T0 + 86400 + 7200 + 7200)
        assert len(rig.notices()) == 3
        # 内存同理
        t = await minutes(rig, T0 + 10 * 86400, 6, commit=95)
        assert len([x for x in rig.notices() if "内存告急" in x]) == 1
        t = await minutes(rig, t, 10, commit=95)  # 一直超线：同一天不重复
        assert len([x for x in rig.notices() if "内存告急" in x]) == 1
        t = await minutes(rig, t, 1, commit=50)  # 恢复
        t = await minutes(rig, t, 6, commit=96)  # 再出问题：新的一次
        assert len([x for x in rig.notices() if "内存告急" in x]) == 2
        # 重启后不会因为同一件事重发
        mon2 = monitor.Monitor(e.core)
        assert mon2.alerted["mem"] and not mon2.new_today("mem", t)
    env.run(body)


def test_switch_off_means_no_alert_but_the_hourly_line_is_still_written(env, monkeypatch):
    async def body(e):
        rig = Rig(e, monkeypatch)
        e.core.settings.set("global", "resource_alert", False)
        rig.free_gb = 1.0
        t = await minutes(rig, T0, 8, commit=99)
        await rig.mon.step(T0 + 3600)
        assert rig.notices() == [] and len(rig.logs("resource")) >= 1
        e.core.settings.set("global", "resource_alert", True)  # 打开后问题还在：照常提醒
        rig.free_gb = 1.0
        await rig.mon.step(T0 + 7200)
        assert any("硬盘告急" in x for x in rig.notices())
    env.run(body)


def test_alert_goes_through_the_user_card_notify_path(env, monkeypatch):
    async def body(e):
        from conftest import FakeChannel
        qq = e.add_bot("900")
        acc = e.core.db.put("account", {"id": "ac_wx", "platform": "wechat", "role_id": "r_haitun"})
        wx = FakeChannel(e.engine, acc, "wxid", "微信小号")
        e.engine.add_bot(acc, wx)
        card = e.core.db.doc("person", "pe_user")
        e.core.db.put("person", {**card, "name": "用户", "self": True, "wechat_remark": ["小海"]})
        e.engine.reload()
        rig = Rig(e, monkeypatch)
        rig.free_gb = 1.0
        await rig.mon.step(T0)
        await e.engine.idle()
        assert wx.sent and wx.sent[-1][:2] == ("private", "小海") and "硬盘告急" in wx.sent[-1][2]  # 现有的通知：发给用户卡本人
        assert any("硬盘告急" in x for x in rig.notices())  # 同时进面板未读通知
    env.run(body)


def test_settings_have_defaults_and_chinese_and_english_descriptions():
    import json
    from pathlib import Path
    from mb.settings import BY_KEY
    d = {"resource_alert": True, "resource_mem_pct": 90, "resource_mem_minutes": 5, "resource_disk_gb": 5.0}
    en = json.loads((Path(__file__).resolve().parent.parent / "web/i18n/en.json").read_text(encoding="utf-8"))
    for k, v in d.items():
        s = BY_KEY[k]
        assert s["default"] == v and s["adv"] and s["scopes"] == ["G"] and s["help"] and s["label"] in en and s["help"] in en, k


def test_real_readings_work_on_this_machine():
    m = monitor.read_memory()
    assert m["commit_limit"] > 0 and 0 <= m["commit_used"] <= m["commit_limit"] * 1.01 and m["phys_total"] > 0
    d = monitor.read_disk(".")
    assert d["total"] > d["free"] > 0
    assert monitor.top_processes(2) and set(monitor.count_processes()) == {"napcat", "qq", "wechat"}
