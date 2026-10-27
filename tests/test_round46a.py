"""给表情包写描述被服务商的内容审核拦下：带搜索原样重试、再去掉搜索重试，都不过就停手（不收 / 保留原来的描述），以后不再试。"""
from test_round35 import logs
from test_round36 import fav_bot, rows, setg
from test_round43a import add_group_sticker, row_of
from test_round43b import img_calls, mock_search, prompt_of, sync_one, urls

from mb.settings import BY_KEY
from mb.stickers import QUERY_PROMPT


def test_the_two_retry_settings_are_global_advanced_tools_with_the_agreed_defaults():
    for key, default, label in (("sticker_block_retry", 2, "内容审核拦下时重试几次（带搜索）"),
                                ("sticker_block_retry_plain", 3, "内容审核拦下后不带搜索再试几次")):
        s = BY_KEY[key]
        assert (s["default"], s["type"], s["cat"], s["adv"], s["scopes"], s["label"]) == (default, "int", "工具", True, ["G"], label)


def test_a_favourite_blocked_without_search_is_tried_once_plus_the_plain_retries_then_not_taken_and_never_again(env):
    async def body(e):
        ch = fav_bot(e)
        e.llm.block_vis = lambda content: True
        await sync_one(e, ch)
        assert len(img_calls(e)) == 1 + 3  # 本来就不搜：第一次 + 不带搜索再试 3 次
        assert [r["status"] for r in rows(e)] == ["blocked"]
        assert logs(e, "被服务商的内容审核拦下") and logs(e, "1 张被服务商的内容审核拦下（不收）")
        n = len(e.llm.calls)
        await e.engine.stickers.sync_favorites(e.engine.bots["ac_900"])  # 下一次同步：不再试这张
        assert len(e.llm.calls) == n and [r["status"] for r in rows(e)] == ["blocked"]
    env.run(body)


def test_blocked_only_with_search_results_falls_back_to_describing_without_search(env):
    async def body(e):
        with urls():
            ch = fav_bot(e)
            mock_search(e)
            e.llm.block_vis = lambda content: "下面是联网搜索的结果" in content[-1]["text"]
            e.llm.vis = lambda content: "词" if content[-1]["text"] == QUERY_PROMPT else {"desc": "不带搜索写的"}
            await sync_one(e, ch)
            calls = img_calls(e)
            with_results = [c for c in calls if "下面是联网搜索的结果" in prompt_of(c)]
            assert len(with_results) == 1 + 2  # 带搜索：第一次 + 原样再试 2 次
            assert "下面是联网搜索的结果" not in prompt_of(calls[-1]) and prompt_of(calls[-1]) != QUERY_PROMPT
            assert [(r["status"], r["descr"]) for r in rows(e)] == [("learned", "不带搜索写的")]
    env.run(body)


def test_retry_counts_follow_the_settings(env):
    async def body(e):
        setg(e, "sticker_block_retry_plain", 1)
        ch = fav_bot(e)
        e.llm.block_vis = lambda content: True
        await sync_one(e, ch)
        assert len(img_calls(e)) == 1 + 1
    env.run(body)


def test_redescribing_a_blocked_sticker_keeps_the_old_description_and_does_not_come_back(env):
    async def body(e):
        fav_bot(e)  # 要有一个 QQ 账号，重新描述才知道用谁的看图模型
        st = e.engine.stickers
        sid = add_group_sticker(e, descr="旧描述")
        e.llm.block_vis = lambda content: True
        res = await st.redescribe()
        r = row_of(e, sid)
        assert (r["descr"], r["desc_max"], r["status"]) == ("旧描述", 250, "learned")
        assert res["done"] == 0 and res["skipped"] == 0 and res["kept"] == 1
        assert logs(e, "保留原来的描述，以后不再重描") and logs(e, "1 张被内容审核拦下、保留原来的描述")
        assert st.redesc_todo() == []  # 不会每 10 分钟又来一轮
    env.run(body)


def test_other_errors_are_not_treated_as_a_content_block(env):
    async def body(e):
        fav_bot(e)  # 要有一个 QQ 账号，重新描述才知道用谁的看图模型
        st = e.engine.stickers
        sid = add_group_sticker(e, descr="旧描述")
        e.llm.vis = lambda content: (_ for _ in ()).throw(RuntimeError("看图接口出错"))  # 别的错误：照旧留到下一轮
        res = await st.redescribe()
        assert res["skipped"] == 1 and row_of(e, sid)["desc_max"] is None
    env.run(body)
