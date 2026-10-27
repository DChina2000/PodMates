"""QQ 小号收藏的表情：同步（新增描述一次、删掉的跟着删、重复的不再描述）、所有群都能用、不占每群张数也不被「用得最少的」删掉、内容检查开关、「现在同步收藏」按钮、发出去计入各项上限。全部用桩。"""
import json

from conftest import FakeChannel
from test_round5b import png


def setg(e, key, value, scope="global"):
    e.core.settings.set(scope, key, value)


class FavChannel(FakeChannel):
    """模拟 NapCat 的取收藏表情接口：fetch_custom_face(count) 返回图片地址列表。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.favs: list[str] = []
        self.calls: list[tuple] = []
        self.fetched: list[str] = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "fetch_custom_face":
            return list(self.favs)
        raise RuntimeError(f"没有桩 {action}")

    async def fetch_image(self, url):
        self.fetched.append(url)
        return self.imgs.get(url)


def fav_bot(e, uid="900"):
    acc = e.core.db.put("account", {"id": f"ac_{uid}", "platform": "qq", "role_id": "r_haitun", "qq": uid})
    ch = FavChannel(e.engine, acc, uid, f"bot{uid}")
    e.engine.add_bot(acc, ch)
    return ch


def rows(e, **where):
    sql = "SELECT * FROM stickers WHERE source='fav'" + "".join(f" AND {k}=?" for k in where)
    return e.core.db.rows(sql, tuple(where.values()))


def test_sync_describes_each_new_favourite_once_and_follows_the_account_when_one_is_removed(env):
    async def body(e):
        ch = fav_bot(e)
        e.llm.vis = lambda content: {"desc": "一只猫在翻白眼，表示无语"}
        for i in (1, 2, 3):
            ch.imgs[f"http://qq/fav{i}.png?rkey=a"] = png(i)
        ch.imgs["http://qq/fav1b.png"] = png(1, tweak=2)  # 和第 1 张近似：只算一张
        ch.favs = ["http://qq/fav1.png?rkey=a", "http://qq/fav2.png?rkey=a", "http://qq/fav3.png?rkey=a", "http://qq/fav1b.png"]
        st = e.engine.stickers
        b = e.engine.bots["ac_900"]
        out = await st.sync_favorites(b)
        assert (out["total"], out["added"], out["removed"], out["kept"], out["error"]) == (4, 3, 0, 3, "")
        assert ch.calls == [("fetch_custom_face", {"count": 500})]
        assert len(rows(e, status="'learned'".strip("'"))) == 3 and all(r["account"] == "ac_900" and r["file"] for r in rows(e))
        n_vis = [c["_kind"] for c in e.llm.calls].count("vis")
        assert n_vis == 3
        # 同样的收藏再同步一遍：不再描述，也不再下载（链接没变）
        before = len(ch.fetched)
        out = await st.sync_favorites(b)
        assert out["added"] == 0 and out["kept"] == 3 and [c["_kind"] for c in e.llm.calls].count("vis") == 3 and len(ch.fetched) == before
        # 链接变了（rkey 换了）但画面一样：按画面指纹认重复，不再描述
        ch.imgs["http://qq/fav2.png?rkey=zzz"] = png(2)
        ch.favs = ["http://qq/fav1.png?rkey=a", "http://qq/fav2.png?rkey=zzz", "http://qq/fav3.png?rkey=a"]
        out = await st.sync_favorites(b)
        assert out["added"] == 0 and out["removed"] == 0 and [c["_kind"] for c in e.llm.calls].count("vis") == 3
        # 从收藏里删掉一张：这里跟着删（文件、描述、向量都清掉）
        gone = [r for r in rows(e)][0]
        keep_urls = [u for u in ch.favs if "fav3" in u or "fav2" in u]
        ch.favs = keep_urls
        out = await st.sync_favorites(b)
        assert out["removed"] == 1 and out["kept"] == 2 and len(rows(e)) == 2
        assert not (st.folder() / gone["file"]).exists() and not e.core.db.rows("SELECT 1 FROM memories WHERE id=?", (gone["mem_id"],))
        # 以后又收藏回来：重新描述
        ch.favs = ["http://qq/fav1.png?rkey=a"] + keep_urls
        out = await st.sync_favorites(b)
        assert out["added"] == 1 and len(rows(e)) == 3
        assert e.core.db.kv("favsync:ac_900")["added"] == 1
    env.run(body)


def test_favourites_are_usable_in_every_group_without_group_stickers(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        e.llm.vis = lambda content: {"desc": "开心地鼓掌"}
        ch.imgs["http://qq/a.png"] = png(11)
        ch.favs = ["http://qq/a.png"]
        eng = e.engine
        await eng.stickers.sync_favorites(eng.bots["ac_900"])
        r1, r2 = eng.room("qq", "100", "群一"), eng.room("qq", "200", "群二")
        b = eng.bots["ac_900"]
        assert eng.stickers.count(eng.stickers.space(r1)) == 0 and eng.stickers.available(r1, b) and eng.stickers.available(r2, b)  # 本群什么都没学到，也能用
        for room in (r1, r2):
            row = (await eng.stickers.candidates(room, "鼓掌", b))[0]
            assert row and row["source"] == "fav" and row["descr"] == "开心地鼓掌"
        # 另一个账号看不到这个账号的收藏
        e.add_bot("901")
        assert not eng.stickers.available(r1, eng.bots["ac_901"]) and await eng.stickers.candidates(r1, "鼓掌", eng.bots["ac_901"]) == []
        # 关掉「用账号收藏的表情」：不给、不挑
        setg(e, "sticker_fav_use", False)
        assert not eng.stickers.available(r1, b) and await eng.stickers.candidates(r1, "鼓掌", b) == []
        setg(e, "sticker_fav_use", True)
        # 本群学来的和收藏的一起挑
        sp = eng.stickers.space(r1)
        mid = eng.memory.add(sp, "sticker", "1", "表情包", "群里学来的：无语", "inferred", None, r1.key, status="active")
        sid = e.core.db.x("INSERT INTO stickers (space, room, phash, seen, users, status, file, size, uses, descr, mem_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (sp, r1.key, "1", 3, "[]", "learned", "none.png", 10, 0, "群里学来的：无语", mid)).lastrowid
        picked = {r["id"] for r in await eng.stickers.candidates(r1, "鼓掌", b)}
        assert picked and all(p in {sid, rows(e)[0]["id"]} for p in picked)
    env.run(body)


def test_favourites_do_not_count_toward_the_per_group_limit_and_are_never_the_ones_evicted(env):
    async def body(e):
        ch = fav_bot(e)
        e.llm.vis = lambda content: {"desc": "收藏的"}
        for i in range(3):
            ch.imgs[f"http://qq/{i}.png"] = png(20 + i)
        ch.favs = [f"http://qq/{i}.png" for i in range(3)]
        eng = e.engine
        await eng.stickers.sync_favorites(eng.bots["ac_900"])
        room = eng.room("qq", "100", "群")
        sp = eng.stickers.space(room)
        for i in range(2):  # 本群学来的两张
            mid = eng.memory.add(sp, "sticker", str(i), "表情包", f"学来的{i}", "inferred", None, room.key, status="active")
            e.core.db.x("INSERT INTO stickers (space, room, phash, seen, users, status, file, size, uses, descr, mem_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sp, room.key, f"{i}", 3, "[]", "learned", f"n{i}.png", 900000, 0, f"学来的{i}", mid))
        setg(e, "sticker_max_count", 2)  # 每个群最多 2 张：本群学来的正好 2 张，收藏的 3 张不算在里面
        eng.stickers.trim(room, eng.bots["ac_900"])
        assert eng.stickers.count(sp) == 2 and len(rows(e)) == 3
        setg(e, "sticker_max_count", 1)
        eng.stickers.trim(room, eng.bots["ac_900"])
        assert eng.stickers.count(sp) == 1 and len(rows(e)) == 3
        # 总容量满了：只删学来的，收藏的一张不动
        e.core.settings.set("global", "sticker_cache_mb", 1)
        e.core.db.x("UPDATE stickers SET size=2000000")
        eng.stickers.trim(room, eng.bots["ac_900"])
        assert eng.stickers.count(sp) == 0 and len(rows(e)) == 3
        eng.stickers.trim_total()
        assert len(rows(e)) == 3  # 只剩收藏的：循环不死、也不删
    env.run(body)


def test_content_check_is_off_by_default_and_when_on_rejects_what_does_not_pass(env):
    async def body(e):
        ch = fav_bot(e)
        ch.imgs["http://qq/x.png"] = png(31)
        ch.favs = ["http://qq/x.png"]
        eng = e.engine
        b = eng.bots["ac_900"]
        assert e.core.settings.get("sticker_fav_check") is False
        e.llm.vis = lambda content: {"sticker": False, "ok": False, "desc": "不是表情包"}  # 检查关着：不管模型怎么判，都收
        out = await eng.stickers.sync_favorites(b)
        assert out["added"] == 1 and out["rejected"] == 0
        # 开着检查：不合格的不收，记下来不再重复问
        for r in rows(e):
            eng.stickers.purge(r["id"])
        e.core.db.del_kv("favmap:ac_900")
        setg(e, "sticker_fav_check", True)
        out = await eng.stickers.sync_favorites(b)
        assert out["added"] == 0 and out["rejected"] == 1
        n = [c["_kind"] for c in e.llm.calls].count("vis")
        out = await eng.stickers.sync_favorites(b)
        assert [c["_kind"] for c in e.llm.calls].count("vis") == n and out["rejected"] == 0
        # 通过检查的照收
        ch.imgs["http://qq/y.png"] = png(32)
        ch.favs = ["http://qq/x.png", "http://qq/y.png"]
        e.llm.vis = lambda content: {"sticker": True, "ok": True, "desc": "合格的"}
        out = await eng.stickers.sync_favorites(b)
        assert out["added"] == 1 and rows(e, status="'learned'".strip("'"))[0]["descr"] == "合格的"
        # 再把检查关掉：以前被拒的放行
        setg(e, "sticker_fav_check", False)
        e.llm.vis = lambda content: {"desc": "放行的"}
        out = await eng.stickers.sync_favorites(b)
        assert out["added"] == 1 and len([r for r in rows(e) if r["status"] == "learned"]) == 2
    env.run(body)


def test_sync_respects_the_switch_unless_forced_and_reports_problems_without_raising(env):
    async def body(e):
        ch = fav_bot(e)
        eng = e.engine
        b = eng.bots["ac_900"]
        setg(e, "sticker_fav_use", False)
        out = await eng.stickers.sync_favorites(b)
        assert out["error"] and not ch.calls  # 开关关着：不去问 NapCat
        out = await eng.stickers.sync_favorites(b, force=True)  # 面板上点「现在同步收藏」：不看开关
        assert out["error"] == "" and ch.calls
        # NapCat 这个接口出错：记下来，不抛
        async def boom(action, **kw):
            raise RuntimeError("NapCat 不认这个接口")
        ch.call = boom
        out = await eng.stickers.sync_favorites(b, force=True)
        assert "不认这个接口" in out["error"] and e.core.db.kv("favsync:ac_900")["error"]
        # 没在线的账号
        ch.status = "stopped"
        assert (await eng.stickers.sync_favorites(b, force=True))["error"] == "这个账号没在线"
        # 没配看图模型：新表情先放着，不报错
        ch.status = "online"
        setg(e, "vis_model", "")
        ch.call = FavChannel.call.__get__(ch)
        ch.imgs["http://qq/z.png"] = png(41)
        ch.favs = ["http://qq/z.png"]
        out = await eng.stickers.sync_favorites(b, force=True)
        assert out["added"] == 0 and out["pending"] == 1
    env.run(body)


def test_connect_and_periodic_triggers_and_the_panel_button(env):
    async def body(e):
        import time
        from mb.server import Server
        ch = fav_bot(e)
        e.llm.vis = lambda content: {"desc": "一句描述"}
        ch.imgs["http://qq/p.png"] = png(51)
        ch.favs = ["http://qq/p.png"]
        eng = e.engine
        # 定期：没到间隔不对，到了才对
        eng.fav_tick(time.time())
        await eng.idle()
        assert len(rows(e)) == 1 and e.core.db.kv("favsync:ac_900")["ts"] > 0
        n = len(ch.calls)
        eng.fav_tick(time.time())
        await eng.idle()
        assert len(ch.calls) == n  # 刚同步过：不重复
        st = e.core.db.kv("favsync:ac_900")
        e.core.db.set_kv("favsync:ac_900", {**st, "ts": time.time() - 25 * 3600})
        eng.fav_tick(time.time())
        await eng.idle()
        assert len(ch.calls) == n + 1
        setg(e, "sticker_fav_hours", 0)  # 0 = 不定期同步
        e.core.db.set_kv("favsync:ac_900", {**st, "ts": 0})
        eng.fav_tick(time.time())
        await eng.idle()
        assert len(ch.calls) == n + 1
        # 面板：现在同步收藏
        srv = Server(e.core)
        ch.favs = ["http://qq/p.png", "http://qq/q.png"]
        ch.imgs["http://qq/q.png"] = png(52)
        resp = await srv.api2.stickers_sync(None)
        data = json.loads(resp.text)
        assert data["results"]["ac_900"]["added"] == 1 and data["accounts"][0]["count"] == 2
        resp = await srv.api2.stickers_list(None)
        listing = json.loads(resp.text)
        assert len(listing) == 2 and all(r["source"] == "fav" for r in listing)
        info = json.loads((await srv.api2.tools_info(None)).text)
        assert info["stickers"]["fav"] == 2 and info["stickers"]["count"] == 2
    env.run(body)


def test_a_favourite_sticker_sent_counts_like_any_sticker(env):
    async def body(e):
        ch = fav_bot(e)
        setg(e, "proactive", False)
        setg(e, "sticker_chance", 100)
        setg(e, "reply_hourly", 5)
        e.llm.vis = lambda content: {"desc": "无语，翻白眼"}
        ch.imgs["http://qq/s.png"] = png(61)
        ch.favs = ["http://qq/s.png"]
        eng = e.engine
        await eng.stickers.sync_favorites(eng.bots["ac_900"])
        e.llm.tool_plan = lambda b: ("send_sticker", {"mood": "无语"}) if any(t["function"]["name"] == "send_sticker" for t in b["tools"]) else None
        e.llm.main = lambda s, u: "行吧\n【表情1】"
        await e.say("111", "你觉得呢", at=["900"])
        assert ch.sent[-1][2] == "行吧" and len(ch.images) == 1  # 本群什么都没学到，发的是账号收藏的
        room = eng.rooms["qq:100"]
        b = eng.duty(eng.bots["ac_900"], room)
        assert len(room.hourly["ac_900"]) == 1  # 计入每小时插话上限
        assert room.run["ac_900"] == 2  # 文字一条 + 表情包一条，都算连着发的条数
        assert 3.9 < eng.quota_tokens(b, room) < 4.1  # 一次回复用一个名额，后面跟的表情包不另算
        assert rows(e)[0]["uses"] == 1
    env.run(body)


def test_coming_online_syncs_once_and_a_quick_reconnect_does_not_ask_again(env):
    async def body(e):
        from mb.channels.onebot import QQChannel
        acc = e.core.db.put("account", {"id": "ac_q", "platform": "qq", "role_id": "r_haitun", "qq": "900", "manage_napcat": False})
        ch = QQChannel(e.engine, acc)
        asked = []

        async def call(action, **kw):
            asked.append(action)
            return {"get_login_info": {"user_id": 900, "nickname": "小号"}, "get_group_list": [], "fetch_custom_face": ["http://qq/o.png"]}.get(action)

        async def fetch_image(url):
            return png(71)
        ch.call, ch.fetch_image = call, fetch_image
        e.engine.add_bot(acc, ch)
        e.llm.vis = lambda content: {"desc": "上线时同步到的"}
        await ch._on_connect()
        await e.engine.idle()
        assert asked.count("fetch_custom_face") == 1 and len(rows(e)) == 1 and ch.status == "online"
        await ch._on_connect()  # 网络一抖动就重连：十分钟内刚同步过，不再去问 NapCat
        await e.engine.idle()
        assert asked.count("fetch_custom_face") == 1
        assert (await e.engine.stickers.sync_favorites(e.engine.bots["ac_q"], min_age=600))["error"] == "刚同步过"
        assert e.core.db.kv("favsync:ac_q")["added"] == 1  # 「刚同步过」没有盖掉上次的结果
    env.run(body)
