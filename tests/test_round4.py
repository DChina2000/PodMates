"""补充四：账号级插话上限、服务商目录与三种协议、角色模板、回复语言、读文件、用量新字段、DPAPI 加密、
新版本提醒、诊断包、体验模式、构型导入接口、平台引导。全部离线。"""
import asyncio
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mb import files as mfiles
from mb import personas, providers, secretbox, templates
from mb.llm import LLM, LLMError
from tests.test_platforms import Mock, until  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FORMAT = "不用Markdown、列表、标题、加粗"


# ---------- 一、账号级插话上限 ----------
def test_hourly_limit_is_per_account_not_per_chat(env):
    async def body(e):
        bot = e.add_bot("901")
        eng, s = e.engine, e.core.settings
        s.set("global", "hourly_limit", 3)
        s.set("global", "cooldown_seconds", 0)
        rooms = [eng.room("qq", c, f"群{c}") for c in ("100", "200", "300")]
        b = eng.bots["ac_901"]
        # 同一个账号挂 3 个群：合计 3 条之后，哪个群都不能再主动说
        for r in rooms:
            assert eng.proactive_ok(b, r, sample=False)
            eng.mark_proactive(b, r)
        assert not any(eng.proactive_ok(b, r, sample=False) for r in rooms)
        # 按群的上限现在默认开启（每群每小时 90 条），和账号级上限叠加，取最严
        assert s.g("hourly_limit_chat") == 90 and s.default("hourly_limit") == 360 and s.g("daily_limit") == 600 and s.g("cold_daily_max") == 12
        s.set("global", "hourly_limit", 90)
        for r in rooms:
            assert eng.proactive_ok(b, r, sample=False)
        s.set("global", "hourly_limit_chat", 1)
        assert not eng.proactive_ok(b, rooms[0], sample=False)  # 这个群已经说过 1 条了
        b2 = eng.room("qq", "400", "群400")
        assert eng.proactive_ok(b, b2, sample=False)
        # 一个小时过去了，旧记录失效
        eng.acct_hourly["ac_901"].clear()
        rooms[0].hourly["ac_901"].clear()
        assert eng.proactive_ok(b, rooms[0], sample=False)
    env.run(body)


# ---------- 二、服务商目录与三种协议 ----------
def test_catalog_contents():
    cat = providers.catalog()
    assert len(cat) >= 40
    ds = cat["deepseek"]
    assert ds["api"] == "https://api.deepseek.com" and ds["env"] == ["DEEPSEEK_API_KEY"] and ds["protocol"] == "openai"
    flash = providers.model_info("deepseek", "deepseek-flash")
    assert flash and flash["vision"] and flash["reasoning"] and "high" in flash["efforts"]
    assert not providers.model_info("deepseek", "deepseek-v4-pro")["vision"]
    assert cat["anthropic"]["protocol"] == "anthropic" and cat["google"]["protocol"] == "gemini"
    assert cat["minimax-cn"]["protocol"] == "anthropic"  # MiniMax 走 Anthropic 协议
    assert cat["ollama-local"]["local"] and cat["baidu-qianfan"]["manual"]
    assert all(m.get("status") != "deprecated" for p in cat.values() for m in p["models"])
    rows = providers.catalog_list()
    assert rows[0]["id"] == "deepseek" and all("models" not in r for r in rows)
    # 许可证声明随程序带
    assert "models.dev" in json.loads(providers.CATALOG_BUNDLED.read_text(encoding="utf-8"))["source"]


def test_catalog_refresh_online_update(env, tmp_path):
    async def body(e):
        raw = {"newco": {"id": "newco", "name": "NewCo", "api": "https://api.newco.test/v1", "env": ["NEWCO_API_KEY"],
                         "npm": "@ai-sdk/openai-compatible", "doc": "x",
                         "models": {"n1": {"id": "n1", "name": "N1", "attachment": True, "reasoning": True, "modalities": {"input": ["text", "image"]},
                                           "limit": {"context": 1000, "output": 100}, "reasoning_options": [{"type": "effort", "values": ["low", "high"]}]},
                                    "old": {"id": "old", "status": "deprecated", "modalities": {"input": ["text"]}, "limit": {}}}},
               "azure": {"id": "azure", "name": "Azure", "api": "https://x", "env": [], "npm": "@ai-sdk/azure", "models": {}}}
        mock = Mock().on("GET", "/api.json", raw)
        await mock.start()
        r = await providers.refresh(None, mock.base + "/api.json")
        assert r["updated"] and r["providers"] == 1
        c = providers.catalog()
        assert "newco" in c and c["newco"]["models"][0]["vision"] and len(c["newco"]["models"]) == 1  # 弃用的和企业云都不收
        assert "deepseek" in c  # 内置的还在（联网更新的只是覆盖/补充）
        await mock.stop()
        providers.user_catalog_path().unlink()
        providers.catalog(reload=True)
    env.run(body)


def ep(provider, **kw):
    p = providers.provider(provider) or {}
    return dict(id="t", provider=provider, protocol=p.get("protocol", "openai"), base_url=p.get("api", "https://x.test/v1"), **kw)


def test_thinking_params_per_provider_dialect(env):
    """思考强度按服务商各自的写法对应，不把 GLM 的写法发给别家。"""
    async def body(e):
        llm = e.core.llm
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]

        def req(provider, effort, model="m", **kw):
            return llm.build(ep(provider, **kw), "KEY", model, msgs, 0.5, effort, 0)[2]
        glm = req("zhipuai", "high")
        assert glm["thinking"] == {"type": "enabled", "clear_thinking": True} and glm["reasoning_effort"] == "high"
        ds = req("deepseek", "low")
        assert ds["thinking"] == {"type": "enabled"} and ds["reasoning_effort"] == "low" and "clear_thinking" not in json.dumps(ds)
        assert req("deepseek", "")["thinking"] == {"type": "disabled"}  # 不思考：DeepSeek 默认开思考，要显式关
        oa = req("openai", "medium")
        assert oa["reasoning_effort"] == "medium" and "thinking" not in oa
        assert "thinking" not in req("moonshotai-cn", "high") and "reasoning_effort" not in req("moonshotai-cn", "high")  # 不认识的不发
        # 自选写法
        assert req("moonshotai-cn", "high", dialect="effort")["reasoning_effort"] == "high"
        # Anthropic
        url, h, a = llm.build(ep("anthropic"), "KEY", "claude-sonnet-5-5", msgs, 0.5, "high", 0)
        assert url == "https://api.anthropic.com/v1/messages" and h["x-api-key"] == "KEY" and h["anthropic-version"]
        assert a["system"] == "S" and a["messages"] == [{"role": "user", "content": "U"}] and a["max_tokens"] == 8192
        assert a["thinking"] == {"type": "adaptive"} and a["output_config"] == {"effort": "high"} and "temperature" not in a
        assert "thinking" not in llm.build(ep("anthropic"), "K", "m", msgs, 0.5, "", 0)[2]
        # MiniMax（Anthropic 协议，地址已带 /v1）
        assert llm.build(ep("minimax-cn"), "K", "m", msgs, 0.5, "", 0)[0] == "https://api.minimax.cn/anthropic/v1/messages"
        # Gemini
        url, h, g = llm.build(ep("google"), "KEY", "gemini-3-flash", msgs, 0.5, "low", 0)
        assert url.endswith("/models/gemini-3-flash:generateContent") and h["x-goog-api-key"] == "KEY"
        assert g["systemInstruction"]["parts"][0]["text"] == "S" and g["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
        assert llm.build(ep("google"), "K", "gemini-2.5-flash", msgs, 0.5, "high", 0)[2]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 24576}
        # 看图：OpenAI 风格的图片消息转成各协议的写法
        img = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}, {"type": "text", "text": "图里有啥"}]}]
        assert llm.build(ep("anthropic"), "K", "m", img, 0.2, "", 0)[2]["messages"][0]["content"][0] == {
            "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}
        assert llm.build(ep("google"), "K", "m", img, 0.2, "", 0)[2]["contents"][0]["parts"][0] == {"inline_data": {"mime_type": "image/png", "data": "AAA"}}
        # 额外请求参数
        assert req("openai", "", extra='{"top_p": 0.9}')["top_p"] == 0.9
        with pytest.raises(LLMError):
            req("openai", "", extra="{坏的")
    env.run(body)


def test_usage_parsing_three_protocols():
    p = LLM.parse
    t, u = p("openai", {"choices": [{"message": {"content": " 好 "}}], "usage": {
        "prompt_tokens": 1000, "completion_tokens": 300, "prompt_tokens_details": {"cached_tokens": 600},
        "completion_tokens_details": {"reasoning_tokens": 120}}})
    assert t == "好" and u == dict(prompt=1000, cached=600, cache_write=0, completion=300, reasoning=120)
    # DeepSeek 的缓存字段
    _, u = p("openai", {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 500, "completion_tokens": 5, "prompt_cache_hit_tokens": 400}})
    assert u["cached"] == 400 and u["reasoning"] == 0
    # Anthropic：输入 = 未命中 + 命中 + 写入
    t, u = p("anthropic", {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "嗯"}],
                           "usage": {"input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 50, "output_tokens": 40}})
    assert t == "嗯" and u == dict(prompt=950, cached=800, cache_write=50, completion=40, reasoning=0)
    # Gemini：思考 token 单独一列，算进输出
    t, u = p("gemini", {"candidates": [{"content": {"parts": [{"text": "想想", "thought": True}, {"text": "好的"}]}}],
                        "usageMetadata": {"promptTokenCount": 300, "cachedContentTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 60}})
    assert t == "好的" and u == dict(prompt=300, cached=100, cache_write=0, completion=80, reasoning=60)


def test_key_from_env_var_and_model_list(env, monkeypatch):
    async def body(e):
        mock = Mock()
        mock.on("GET", "/v1/models", {"data": [{"id": "b-model"}, {"id": "a-model"}]})
        mock.on("GET", "/anth/v1/models", {"data": [{"id": "claude-x"}]})
        mock.on("GET", "/gem/models", {"models": [{"name": "models/gemini-9", "supportedGenerationMethods": ["generateContent"]},
                                                  {"name": "models/embed-1", "supportedGenerationMethods": ["embedContent"]}]})
        await mock.start()
        db, llm = e.core.db, e.core.llm
        db.put("endpoint", {"id": "envep", "provider": "deepseek", "protocol": "openai", "base_url": mock.base + "/v1", "key_env": "MB_TEST_KEY"})
        with pytest.raises(LLMError):
            llm.endpoint("envep")  # 没填也没环境变量
        monkeypatch.setenv("MB_TEST_KEY", "from-env")
        st = llm.key_state(db.doc("endpoint", "envep"))
        assert st["has_key"] and st["source"] == "环境变量 MB_TEST_KEY" and st["env_set"]
        assert await llm.list_models("envep") == ["a-model", "b-model"]
        db.set_secret("endpoint:envep", "local-key")  # 本机填了的优先
        assert llm.key_state(db.doc("endpoint", "envep"))["source"] == "本机"
        db.put("endpoint", {"id": "anth", "provider": "anthropic", "protocol": "anthropic", "base_url": mock.base + "/anth", "key_env": ""})
        db.put("endpoint", {"id": "gem", "provider": "google", "protocol": "gemini", "base_url": mock.base + "/gem", "key_env": ""})
        db.set_secret("endpoint:anth", "k")
        db.set_secret("endpoint:gem", "k")
        assert await llm.list_models("anth") == ["claude-x"] and await llm.list_models("gem") == ["gemini-9"]
        assert mock.calls[-1][2] == {"pageSize": "1000"}
        await mock.stop()
    env.run(body)


def test_default_models_are_deepseek_flash(env):
    async def body(e):
        s = e.core.settings
        for p in ("main", "gk", "mem", "vis"):
            assert s.g(f"{p}_model") == "deepseek-flash"
        ep0 = e.core.db.doc("endpoint", "default")
        assert ep0["provider"] == "deepseek"
    env.run(body)


def test_import_old_keeps_glm_settings(env, tmp_path):
    async def body(e):
        d = tmp_path / "bot"
        d.mkdir()
        (d / "config.json").write_text(json.dumps({"glm_api_key": "OLD-KEY-123", "glm_base_url": "https://api.z.ai/api/coding/paas/v4/",
                                                   "glm_model": "glm-5.1", "gatekeeper_model": "glm-5.3-flash", "haitun_qq_ids": ["111", "222"]}),
                                       encoding="utf-8")
        (d / "persona_core.md").write_text((ROOT / "assets/seed/persona_core.md").read_text(encoding="utf-8").replace("{{平台}}", "QQ"), encoding="utf-8")
        (d / "persona_haitun.md").write_text("你是测试外壳。", encoding="utf-8")
        done = e.core.import_old(d)
        s = e.core.settings
        assert s.g("main_model") == "glm-5.1" and s.g("gk_model") == "glm-5.3-flash"
        assert all(s.g(f"{p}_endpoint") == "zai" for p in ("main", "gk", "mem", "vis"))  # 不受新的默认值影响
        assert e.core.db.secret("endpoint:zai") == "OLD-KEY-123" and e.core.db.doc("endpoint", "zai")["base_url"].endswith("/coding/paas/v4")
        assert e.core.db.doc("endpoint", "zai")["provider"] == "zai-coding-plan"
        card = e.engine.people.user_card()
        assert card["qq"] == ["111", "222"] and card["admin"]
        assert any("API Key" in x for x in done)
        # 导入后用的是 GLM 写法
        url, h, body_ = e.core.llm.build(e.core.llm.endpoint_doc("zai"), "k", "glm-5.1", [{"role": "user", "content": "x"}], 0.5, "high", 0)
        assert body_["thinking"]["clear_thinking"] is True and url.startswith("https://api.z.ai/api/coding/paas/v4/chat")
    env.run(body)


# ---------- 三、角色模板、回复语言 ----------
def test_templates_library(env):
    async def body(e):
        lst = templates.listing()
        assert [c["category"] for c in lst] == ["娱乐陪伴", "学术讲解", "生活健康", "实用工具", "游戏主持", "原创角色"]
        names = {t["name"] for c in lst for t in c["items"]}
        need = {"海豚娘", "温和版群友", "经济学教授", "商学院教授", "政治学教授", "精神分析理论家", "哲学教授", "历史学者", "社会学者", "心理学科普",
                "法学科普", "数理与编程老师", "文学评论家", "营养师", "健身教练", "作息顾问", "情绪陪伴", "翻译", "写作润色", "编程助手", "学习规划",
                "群聊总结与答疑", "跑团主持", "谜题主持", "狼人杀法官"}
        assert need <= names, need - names
        assert sum(len(c["items"]) for c in lst) == 28
        assert lst[0]["items"][0]["name"] == "温和版群友" and [x["name"] for x in lst[-1]["items"]] == ["海豚娘"]
        db = e.core.db
        for c in lst:
            for t in c["items"]:
                role = templates.instantiate(db, t["id"])
                e.engine.reload()
                assert role["summary"] and e.engine.personas[role["persona"]]
                for plat in ("qq", "discord"):
                    text = personas.assemble(e.engine.personas[role["persona"]], e.engine.modules, plat, {"people": "", "memory": ""})
                    assert "{{" not in text and len(text) > 150, t["name"]
                    assert (FORMAT in text) == (plat == "qq"), t["name"]  # 纯文本格式要求只挂在不渲染 Markdown 的平台
                full = templates.get(t["id"])
                text_all = full["shell"] + "".join(s["body"] for s in full["sections"])
                assert ("以科普和给方向为主，不下诊断，大事找执业人士" in text_all) == (t["id"] in templates.NEEDS_DISCLAIMER), t["id"]
                # 除原创角色外，模板默认不带构型，也不套用海豚娘的内核
                p = e.engine.personas[role["persona"]]
                mods = [e.engine.modules[i["m"]] for i in p["items"] if i["m"] in e.engine.modules]
                if not full.get("mods"):  # 原创角色（海豚娘）挂共用内核的模块（含构型171）
                    assert not any(m["kind"] == "archetype" for m in mods), t["name"]
                    assert "m_群友性格" not in [m["id"] for m in mods] and "贴吧" not in text_all, t["name"]
                    assert any(m["kind"] == "exclusive" and m["section"] == "说话" for m in mods)
        # 海豚娘自带构型171，且用的是共用的内核模块
        h = templates.instantiate(db, "haitun", name="我的海豚")
        e.engine.reload()
        mods = [e.engine.modules[i["m"]] for i in e.engine.personas[h["persona"]]["items"]]
        arch = next(m for m in mods if m["kind"] == "archetype")
        assert arch["archetype"] == "构型171"
        assert h["name"] == "我的海豚" and db.doc("role", h["id"])["template"] == "haitun"
    env.run(body)


def test_reply_language_layers(env):
    async def body(e):
        e.add_bot("901")
        s = e.core.settings
        s.set("global", "proactive", False)
        await e.say("111", "在吗", at=["901"])
        assert "【回复语言】" not in e.llm.calls[-1]["messages"][0]["content"]  # 默认中文：提示词不变
        s.set("role:r_haitun", "reply_lang", "en")
        await e.say("111", "在吗2", at=["901"])
        assert e.llm.calls[-1]["messages"][0]["content"].endswith("用English回复，群里别人用别的语言说话也照常用English。")
        s.set("platform:qq", "reply_lang", "ja")  # 角色层比平台层更具体：这个角色仍是 English
        await e.say("111", "在吗3", at=["901"])
        assert "用English回复" in e.llm.calls[-1]["messages"][0]["content"]
        s.reset("role:r_haitun", "reply_lang")  # 角色不设了，平台层生效
        await e.say("111", "在吗3b", at=["901"])
        assert "用日本語回复" in e.llm.calls[-1]["messages"][0]["content"]
        s.set("role:r_haitun|chat:qq:100", "reply_lang", "follow")  # 角色在这个群
        await e.say("111", "在吗4", at=["901"])
        assert "用对方说话所用的语言回复" in e.llm.calls[-1]["messages"][0]["content"]
        s.set("chat:qq:100", "reply_lang", "zh")
        s.reset("role:r_haitun|chat:qq:100", "reply_lang")
        await e.say("111", "在吗5", at=["901"])
        assert "【回复语言】" not in e.llm.calls[-1]["messages"][0]["content"]
        langs = {k for k, _ in __import__("mb.settings", fromlist=["LANGS"]).LANGS}
        assert {"zh", "follow", "en", "fr", "de", "it", "ru", "ja", "es", "ko", "ar", "fa", "pt"} <= langs
    env.run(body)


# ---------- 五、读文件 ----------
def make_docx(paras):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", xml)
    return b.getvalue()


def make_xlsx(rows):
    strings = []

    def si(t):
        strings.append(t)
        return len(strings) - 1
    rx = "".join(f'<row>{"".join(f"<c t=\"s\"><v>{si(c)}</v></c>" for c in r)}</row>' for r in rows)
    sheet = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rx}</sheetData></worksheet>'
    ss = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + "".join(f"<si><t>{t}</t></si>" for t in strings) + "</sst>"
    wb = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="预算" sheetId="1"/></sheets></workbook>'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/sharedStrings.xml", ss)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return b.getvalue()


def make_pptx(slides):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for i, t in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>{t}</a:t></p:sld>')
    return b.getvalue()


def make_pdf(text):
    """手写一个最小的 PDF（Helvetica，只含 ASCII），测抽文字。"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 14 Tf 20 100 Td ({text}) Tj ET"
    objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF"
    return out.encode("latin-1")


def test_file_extractors():
    assert "第一段" in mfiles.extract(make_docx(["第一段", "第二段"]), "a.docx") and mfiles.extract(make_docx(["甲", "乙"]), "a.docx").count("\n") == 1
    x = mfiles.extract(make_xlsx([["项目", "金额"], ["月球基地", "三百万"]]), "b.xlsx")
    assert "【工作表 预算】" in x and "月球基地\t三百万" in x
    assert "第 2 页" in mfiles.extract(make_pptx(["封面", "预算表"]), "c.pptx")
    assert "Hello PDF" in mfiles.extract(make_pdf("Hello PDF text"), "d.pdf")
    assert mfiles.extract("中文文本".encode("gb18030"), "e.txt") == "中文文本"
    with pytest.raises(mfiles.FileError):
        mfiles.extract(b"not a zip", "x.docx")
    with pytest.raises(mfiles.FileError):
        mfiles.extract(b"\x00\x01", "x.exe")
    # 只解析，不运行：带宏的 docm、压缩炸弹这类都读不进来 / 被限制
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<a>" + "x" * (90 << 20) + "</a>")
    with pytest.raises(mfiles.FileError, match="太大"):
        mfiles.extract(bomb.getvalue(), "bomb.docx")
    assert mfiles.est_tokens("你好" * 100) == 160 and mfiles.clip_tokens("你好" * 1000, 100)[1]
    ch = mfiles.chunks("段落。\n" * 1000)
    assert len(ch) > 3 and all(len(c) <= 700 for c in ch)


def file_setup(e, **s):
    e.add_bot("901")
    st = e.core.settings
    st.set("global", "proactive", False)
    st.set("global", "file_read", True)
    for k, v in s.items():
        st.set("global", k, v)
    return e.engine.bots["ac_901"].channel


def test_file_read_called_mode_and_cleanup(env):
    async def body(e):
        ch = file_setup(e)
        ch.files["f1"] = make_docx(["本次预算：月球基地三百万元，工期两年。"])
        f = {"name": "预算.docx", "id": "f1", "size": 500}
        # 默认「被叫到才读」：没人叫她，文件不读
        await e.say("111", "", files=[f])
        assert not any("预算" in json.dumps(c, ensure_ascii=False) for c in e.llm.calls if c.get("_kind") == "main")
        assert not e.core.db.x("SELECT 1 FROM memories WHERE kind='doc'").fetchone()
        # @她但没回复那条文件消息：取这个人最近发的文件
        await e.say("111", "总结一下", at=["901"])
        user = e.llm.calls[-1]["messages"][-1]["content"]
        assert "【文件《预算.docx》的内容】" in user and "月球基地三百万元" in user
        chunks = e.core.db.rows("SELECT * FROM memories WHERE kind='doc'")
        assert chunks and chunks[0]["subject_name"] == "预算.docx" and chunks[0]["status"] == "active" and chunks[0]["space"].startswith("chat:")
        # 原文件不留（没设保留天数）
        assert not list(mfiles.keep_dir().iterdir())
        # 再读同一份文件不重复存片段
        n = len(chunks)
        await e.say("111", "再看一遍", at=["901"], reply_to_seq=1)
        assert e.core.db.x("SELECT COUNT(*) FROM memories WHERE kind='doc'").fetchone()[0] == n
        # 回复那条文件消息也算叫到
        ch.files["f2"] = make_docx(["另一份：食堂菜单"])
        await e.say("112", "", files=[{"name": "菜单.docx", "id": "f2", "size": 300}])
        seq = e.seq
        await e.say("113", "这个讲了啥", at=["901"], reply_to_seq=seq)
        assert "食堂菜单" in e.llm.calls[-1]["messages"][-1]["content"]
    env.run(body)


def test_file_read_auto_limits_and_formats(env):
    async def body(e):
        ch = file_setup(e, file_mode="auto", file_max_mb=1, file_max_tokens=60)
        ch.files["big"] = make_docx(["字" * 5000])
        ch.files["t1"] = ("很长的文本。" * 400).encode()
        ch.files["x"] = b"MZ"
        ch.files["huge"] = b"0" * (2 << 20)
        # 格式没开放 / 太大 / 下载不了：读不成，如实告诉模型
        await e.say("111", "看看", at=["901"], files=[{"name": "a.exe", "id": "x", "size": 2}])
        assert "没读成：这种格式没有开放读取" in e.llm.calls[-1]["messages"][-1]["content"]
        await e.say("111", "看看", at=["901"], files=[{"name": "h.txt", "id": "huge", "size": 2 << 20}])
        assert "文件太大" in e.llm.calls[-1]["messages"][-1]["content"]
        await e.say("111", "看看", at=["901"], files=[{"name": "n.txt", "id": "nope", "size": 10}])
        assert "下载不了" in e.llm.calls[-1]["messages"][-1]["content"]
        # 自动读：没人叫她也读进记忆；单次读取有 token 上限，超出的不读
        await e.say("112", "", files=[{"name": "长文.txt", "id": "t1", "size": 2400}])
        rows = e.core.db.rows("SELECT content FROM memories WHERE kind='doc'")
        assert rows and sum(len(r["content"]) for r in rows) < 120
        # 单个群可以单独关
        e.core.settings.set("chat:qq:100", "file_read", False)
        ch.files["t2"] = "别的内容".encode()
        n = e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0]
        await e.say("113", "", files=[{"name": "t2.txt", "id": "t2", "size": 12}])
        assert e.core.db.x("SELECT COUNT(*) FROM memories").fetchone()[0] == n
        # 默认关闭
        assert e.core.settings.default("file_read") is False
    env.run(body)


def test_file_keep_days_and_capacity(env, monkeypatch):
    async def body(e):
        ch = file_setup(e, file_mode="auto", file_keep_days=3, file_cache_mb=1)
        for i in range(3):
            ch.files[f"k{i}"] = (f"文件{i}的内容。" * 200_000).encode("utf-8")[: 450_000]
            await e.say(f"2{i}", "", files=[{"name": f"k{i}.txt", "id": f"k{i}", "size": 450_000}])
            await asyncio.sleep(0.02)
        kept = sorted(mfiles.keep_dir().iterdir(), key=lambda f: f.stat().st_mtime)
        # 总容量上限 1 MB：满了从最旧的开始删，所以 3 个 45 万字节的文件最多留 2 个
        assert 1 <= len(kept) <= 2 and kept[-1].name.endswith("k2.txt")
        # 过期的删掉
        old = time.time() - 4 * 86400
        for f in kept:
            os.utime(f, (old, old))
        mfiles.enforce_cache(1, 3)
        assert not list(mfiles.keep_dir().iterdir())
    env.run(body)


def test_file_chunks_recalled_later_by_meaning(env):
    async def body(e):
        ch = file_setup(e, file_mode="auto")
        ch.files["m1"] = make_docx(["第三季度研究生入学考试报名人数同比增长，北京的报名点排队很长。"])
        await e.say("111", "", files=[{"name": "报名情况.docx", "id": "m1", "size": 600}])
        await e.engine.memory.flush_index()
        for i in range(5):  # 把文件消息顶出最近几句
            await e.say("111", f"闲聊{i}", chat="100")
        await e.say("222", "今年考研报名的人多不多", at=["901"])
        sysmsg = e.llm.calls[-1]["messages"][0]["content"]
        assert "【想起的旧事】" in sysmsg and "文件《报名情况.docx》里：" in sysmsg
    env.run(body)


# ---------- 六、用量 ----------
async def api_client(e):
    from mb.server import Server
    srv = Server(e.core)
    client = TestClient(TestServer(srv.app))
    await client.start_server()
    return srv, client


def test_usage_new_fields_summary_and_csv(env):
    async def body(e):
        db = e.core.db
        db.add_usage("r_haitun", "qq", "qq:100", "main", "deepseek-flash", 1000, 800, 200, 120, 0)
        db.add_usage("r_haitun", "qq", "qq:100", "main", "deepseek-flash", 2000, 0, 400, 0, 0)
        db.add_usage("r_haitun", "qq", "qq:100", "gk", "deepseek-flash", 500, 100, 20, 0, 30)
        srv, c = await api_client(e)
        rows = await (await c.get("/api/usage?by=purpose&days=7")).json()
        main = next(r for r in rows if r["k"] == "main")
        assert main["calls"] == 2 and main["cached"] == 800 and main["reasoning"] == 120 and main["cache_write"] == 0
        assert next(r for r in rows if r["k"] == "gk")["cache_write"] == 30
        j = await (await c.get("/api/usage/summary?days=7")).json()
        assert j["hit_rate"] == round(900 / 3500, 4)                                 # 缓存命中率
        assert j["avg_reply"] == {"calls": 2, "prompt": 1500, "completion": 300}      # 平均每条回复的 token（只算主模型）
        assert j["has_reasoning"] and j["has_cache_write"] and len(j["trend"]) == 1   # 趋势、思考 token、缓存写入
        csv_text = (await (await c.get("/api/usage/export.csv?by=day&days=7")).read()).decode("utf-8")
        assert csv_text.startswith("\ufeff日期,调用次数,输入tokens,缓存命中tokens,缓存命中率,缓存写入tokens,输出tokens,思考tokens")
        assert time.strftime("%Y-%m-%d") in csv_text and "25.7%" in csv_text and "$" not in csv_text and "¥" not in csv_text  # 不换算成钱
        # 接口没返回思考 token / 缓存写入时，这两列不显示
        db.x("DELETE FROM usage")
        db.add_usage("r_haitun", "qq", "qq:100", "main", "m", 100, 0, 10)
        j = await (await c.get("/api/usage/summary?days=7")).json()
        assert not j["has_reasoning"] and not j["has_cache_write"]
        await c.close()
    env.run(body)


# ---------- 八、DPAPI 加密 ----------
def test_secrets_encrypted_at_rest_and_unreadable_elsewhere(env, tmp_path):
    async def body(e):
        db = e.core.db
        db.set_secret("endpoint:x", "sk-SUPER-SECRET-123")
        raw = db.x("SELECT v FROM secrets WHERE k='endpoint:x'").fetchone()[0]
        assert raw.startswith("dpapi:") and "SUPER" not in raw and db.secret("endpoint:x") == "sk-SUPER-SECRET-123"
        assert db.secret_state("endpoint:x") == "ok" and db.secret_state("nope") == "missing"
        # 整个库文件里也找不到明文
        db.conn.execute("PRAGMA wal_checkpoint(FULL)")
        assert b"SUPER-SECRET" not in Path(db.path).read_bytes()
        # 旧版本留下的明文，打开时换成密文
        db.x("INSERT OR REPLACE INTO secrets VALUES ('legacy', 'plain-old-key')")
        assert db.encrypt_legacy_secrets() == 1
        assert db.secret("legacy") == "plain-old-key" and db.x("SELECT v FROM secrets WHERE k='legacy'").fetchone()[0].startswith("dpapi:")
        # 换电脑 / 换 Windows 用户：解不开 → 当没填，面板能看出是「需要重新填」
        db.x("INSERT OR REPLACE INTO secrets VALUES ('other-pc', 'dpapi:AQAAANCMnd8BFdERjHoAwE')")
        assert db.secret("other-pc") == "" and db.secret_state("other-pc") == "unreadable" and db.unreadable_secrets() == ["other-pc"]
        db.put("endpoint", {"id": "bad", "provider": "deepseek", "protocol": "openai", "base_url": "https://x"})
        db.x("INSERT OR REPLACE INTO secrets VALUES ('endpoint:bad', 'dpapi:AQAAANCMnd8BFdERjHoAwE')")
        assert e.core.llm.key_state(db.doc("endpoint", "bad"))["unreadable"]
        # 导出（带密钥）：说明换电脑后要重新填；导出的库里同样没有明文
        z = tmp_path / "k.zip"
        e.core.export(z, with_secrets=True)
        with zipfile.ZipFile(z) as zf:
            assert "重新填" in json.loads(zf.read("manifest.json"))["secrets_note"]
            assert all(b"SUPER-SECRET" not in zf.read(n) for n in zf.namelist())
        assert secretbox.decrypt(secretbox.encrypt("中文密钥")) == "中文密钥"
    env.run(body)


# ---------- 七、新版本提醒、诊断包 ----------
def test_update_check(env):
    async def body(e):
        from mb import update, VERSION
        assert update.newer("1.2.0", "1.1.0") and update.newer("v2.0", "1.9.9") and not update.newer("1.1.0", "1.1.0") and not update.newer("1.0.9", "1.1.0")
        mock = Mock().on("GET", "/repos/o/r/releases/latest", {"tag_name": "v9.9.9", "html_url": "https://github.com/o/r/releases/tag/v9.9.9", "body": "更新说明"})
        await mock.start()
        info = await update.check("o/r", None, mock.base + "/repos/{repo}/releases/latest")
        assert info["newer"] and info["latest"] == "9.9.9" and info["current"] == VERSION
        with pytest.raises(ValueError):
            await update.check("", None)
        # 每天查一次；只提醒，且同一个版本只提醒一次
        s = e.core.settings
        s.set("global", "update_repo", "o/r")
        notes = []
        e.engine.notify_hook = notes.append
        orig = update.API
        update.API = mock.base + "/repos/{repo}/releases/latest"
        try:
            monkey_check = update.check

            async def fake_check(repo, proxy=None, api=None):
                return await monkey_check(repo, proxy, update.API)
            update.check = fake_check
            await e.core.check_update()
            await e.core.check_update()
        finally:
            update.check = monkey_check
            update.API = orig
        assert len([n for n in notes if "9.9.9" in n]) == 1
        s.set("global", "update_check", False)
        # 面板接口
        srv, c = await api_client(e)
        j = await (await c.get("/api/update")).json()
        assert j["latest"] == "9.9.9" and j["newer"]
        await c.post("/api/update/dismiss")
        assert (await (await c.get("/api/update")).json())["dismissed"] is True
        await c.close()
        await mock.stop()
    env.run(body)


def test_diagnostics_zip_has_no_secrets(env):
    async def body(e):
        db = e.core.db
        db.set_secret("endpoint:default", "sk-VERYSECRETKEY9999")
        db.set_secret("acct:ac_x:token", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abc")
        db.put("account", {"id": "ac_x", "platform": "telegram", "role_id": "r_haitun", "enabled": True})
        e.core.settings.set("global", "proxy", "http://user:pass123@127.0.0.1:7890")
        e.core.settings.set("global", "api_secret_note", "x") if False else None
        e.engine.log("info", "reply", "用 sk-VERYSECRETKEY9999 调用；QQ 号 123456789012；Authorization: Bearer abcdefghijklmnop1234")
        e.engine.log("warn", "telegram", "token 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abc 连不上")
        srv, c = await api_client(e)
        r = await c.post("/api/diagnostics")
        assert r.status == 200 and r.headers["Content-Type"] == "application/zip"
        data = await r.read()
        z = zipfile.ZipFile(io.BytesIO(data))
        assert {"version.json", "settings.json", "accounts.json", "endpoints.json", "logs.txt", "说明.txt"} <= set(z.namelist())
        alltext = b"".join(z.read(n) for n in z.namelist()).decode("utf-8")
        for bad in ("VERYSECRETKEY", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "pass123", "123456789012", "abcdefghijklmnop1234"):
            assert bad not in alltext, bad
        assert json.loads(z.read("version.json"))["version"] and "…9012" in z.read("logs.txt").decode()  # 长数字只留末 4 位
        assert json.loads(z.read("endpoints.json"))[0]["has_key"] is True
        await c.close()
    env.run(body)


# ---------- 七、体验模式 ----------
def test_demo_mode_offline_and_with_key(env):
    async def body(e):
        demo = e.core.demo
        e.engine.pace = 1
        db = e.core.db
        # 还没填服务商密钥：用预设的演示回复，不调模型
        db.set_secret("endpoint:default", "")
        await demo.start("r_haitun", chatter=False)
        assert demo.state()["on"] and not demo.state()["has_key"] and "demo:demo-room" in e.engine.rooms
        e.engine.pace = 0
        await demo.say("你", "在吗", at=True)
        await e.engine.idle()
        msgs = db.rows("SELECT name, text, kind FROM messages WHERE room='demo:demo-room' ORDER BY id")
        assert any(m["kind"] == "bot" and "演示回复" in m["text"] for m in msgs)
        assert not [c for c in e.llm.calls if c.get("_kind") in ("main", "gk")]  # 没调模型
        # 填了密钥：真调模型，用量记在「试聊」下
        db.set_secret("endpoint:default", "test-key")
        e.llm.main = lambda s, u: "真模型回的"
        await demo.say("你", "再问一句", at=True)
        await e.engine.idle()
        assert any(m["text"] == "真模型回的" for m in db.rows("SELECT text FROM messages WHERE room='demo:demo-room'"))
        purposes = {r["purpose"] for r in db.rows("SELECT purpose FROM usage")}
        assert purposes == {"trial"}
        # 体验群不写记忆
        assert e.core.settings.get("memory_enabled", platform="demo", chat="demo:demo-room") is False
        # 剧本群友会自己说话
        e.llm.gate = lambda p: {"reply": False, "brief": "不接"}
        demo.chatter(True)
        assert await until(lambda: db.x("SELECT COUNT(*) FROM messages WHERE user_id LIKE 'demo-%阿明' OR name='阿明'").fetchone()[0] >= 1, 5)
        # 退出体验：账号、群、消息都清掉
        await demo.stop()
        assert not db.doc("account", "ac_demo") and "demo:demo-room" not in e.engine.rooms and not db.doc("chat", "demo:demo-room")
        assert db.x("SELECT COUNT(*) FROM messages WHERE room='demo:demo-room'").fetchone()[0] == 0
    env.run(body)


def test_demo_api(env):
    async def body(e):
        srv, c = await api_client(e)
        j = await (await c.post("/api/demo/start", json={"role_id": "r_haitun", "chatter": False})).json()
        assert j["on"] and j["role_id"] == "r_haitun"
        assert (await c.post("/api/demo/say", json={"text": "你好", "at": True})).status == 200
        await e.engine.idle()
        assert (await c.post("/api/demo/start", json={"role_id": "没有这个", "chatter": False})).status == 400
        assert not (await (await c.post("/api/demo/stop")).json())["on"]
        assert (await c.post("/api/demo/say", json={"text": "还在吗"})).status == 400
        await c.close()
    env.run(body)


# ---------- 接口：构型导入、平台引导、账号密钥状态 ----------
def test_archetype_import_endpoint_drops_extra_fields(env):
    async def body(e):
        srv, c = await api_client(e)
        data = {"entries": [{"id": "新构型", "coreProposition": "甲", "realityExpansion": "乙", "characterTendency": "丙",
                             "innerTension": "不该进来", "formalTitle": "t"}, {"id": "坏的"}]}
        from aiohttp import FormData
        fd = FormData()
        fd.add_field("file", json.dumps(data, ensure_ascii=False).encode(), filename="x.json", content_type="application/json")
        j = await (await c.post("/api/archetypes/import", data=fd)).json()
        assert j == {"imported": 1, "skipped": 1, "dropped_field_kinds": 2}
        boot = await (await c.get("/api/bootstrap")).json()
        ids = [a["id"] for a in boot["archetypes"]]
        assert "新构型" in ids and "构型171" in ids and len(ids) == 2
        assert "不该进来" not in json.dumps(await (await c.get("/api/docs/archetype")).json(), ensure_ascii=False)
        fd = FormData()
        fd.add_field("file", b"nope", filename="x.json")
        assert (await c.post("/api/archetypes/import", data=fd)).status == 400
        await c.close()
    env.run(body)


def test_platform_specs_and_account_secret_state(env):
    async def body(e):
        srv, c = await api_client(e)
        specs = {p["id"]: p for p in await (await c.get("/api/platform_specs")).json()}
        assert {"telegram", "line", "reddit", "youtube", "x", "bilibili", "tieba", "weibo", "whatsapp"} <= set(specs)
        for pid in ("bilibili", "tieba", "weibo"):  # 非官方渠道：标明风险，要勾「我知道有风险」
            assert specs[pid]["tier"] == "unofficial" and specs[pid]["risk"] and any(f[0] == "risk_ack" for f in specs[pid]["flags"])
        assert specs["whatsapp"]["tier"] == "info" and "不能加入已有" in "".join(specs["whatsapp"]["steps"])
        assert specs["reddit"]["experimental"] and "2027" in specs["reddit"]["summary"]
        assert all(len(p["steps"]) >= 3 for p in specs.values())
        e.core.db.put("account", {"id": "ac_t", "platform": "telegram", "role_id": "r_haitun"})
        e.core.db.set_secret("acct:ac_t:token", "123:abc")
        boot = await (await c.get("/api/bootstrap")).json()
        acc = next(a for a in boot["accounts"] if a["id"] == "ac_t")
        assert acc["secrets_set"] == {"token": "ok"} and "123:abc" not in json.dumps(boot)  # 密钥不下发到页面
        # 其他平台的密钥可以通过 /api/secret 存
        assert (await c.put("/api/secret", json={"k": "acct:ac_t:token", "v": "999:zzz"})).status == 200
        assert e.core.db.secret("acct:ac_t:token") == "999:zzz"
        await c.close()
    env.run(body)


def test_youtube_oauth_loopback_flow(env):
    async def body(e):
        import aiohttp
        from urllib.parse import parse_qs, urlparse
        tok = Mock().on("POST", "/token", {"access_token": "A", "refresh_token": "RT-NEW", "expires_in": 3600})
        await tok.start()
        e.core.db.put("account", {"id": "ac_yt", "platform": "youtube", "role_id": "r_haitun", "client_id": "cid", "token_url": tok.base + "/token"})
        e.core.db.set_secret("acct:ac_yt:client_secret", "cs")
        srv, c = await api_client(e)
        j = await (await c.post("/api/accounts/ac_yt/youtube_oauth")).json()
        q = parse_qs(urlparse(j["url"]).query)
        assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["https://www.googleapis.com/auth/youtube.force-ssl"] and q["access_type"] == ["offline"]
        async with aiohttp.ClientSession() as s:  # 模拟用户在浏览器里授权后被重定向回本机
            bad = await s.get(j["redirect"], params={"state": "wrong", "code": "X"})
            assert bad.status == 400
            ok = await s.get(j["redirect"], params={"state": q["state"][0], "code": "AUTHCODE"})
            assert ok.status == 200
        assert e.core.db.secret("acct:ac_yt:refresh_token") == "RT-NEW"
        sent = tok.sent("/token")[0]
        assert sent["code"] == "AUTHCODE" and sent["grant_type"] == "authorization_code" and sent["code_verifier"] and sent["redirect_uri"] == j["redirect"]
        await c.close()
        await tok.stop()
    env.run(body)


def test_bilibili_qr_login_endpoints(env):
    async def body(e):
        m = Mock()
        m.on("GET", "/x/passport-login/web/qrcode/generate", {"code": 0, "data": {"url": "https://x/qr", "qrcode_key": "K1"}})
        m.on("GET", "/x/passport-login/web/qrcode/poll", [{"code": 0, "data": {"code": 86101}},
                                                           {"code": 0, "data": {"code": 0, "url": "https://x?SESSDATA=sd1&bili_jct=jct1&DedeUserID=9"}}])
        await m.start()
        e.core.db.put("account", {"id": "ac_b", "platform": "bilibili", "role_id": "r_haitun", "passport_base": m.base})
        srv, c = await api_client(e)
        q = await (await c.post("/api/accounts/ac_b/bilibili_qr")).json()
        assert q["key"] == "K1"
        assert (await (await c.get("/api/accounts/ac_b/bilibili_qr?key=K1")).json())["state"] == "waiting"
        assert (await (await c.get("/api/accounts/ac_b/bilibili_qr?key=K1")).json())["state"] == "ok"
        assert e.core.db.secret("acct:ac_b:SESSDATA") == "sd1" and e.core.db.secret("acct:ac_b:bili_jct") == "jct1"
        await c.close()
        await m.stop()
    env.run(body)


# ---------- 公开版不含作者的私人信息（通用判据） ----------
def test_no_personal_data_in_published_files():
    """公开文件里不能出现作者的私人信息。判据：
    - `C:\\Users\\<用户名>`（或 `C:/Users/<用户名>`）里的用户名不是 `<…>`、`%…%`、`你的用户名`、`username`、
      `user`、`Public` 这类占位或系统公用名；
    - 盘符开头的绝对路径，第一层目录是 `Project` / `Projects` / `ZcodeProject` / `workspace` / `code` /
      `dev` / `repos` / `src` / `git` / `personal` / `home` 这类「某人的项目目录」（不分大小写）；
    - 邮箱地址，只放行 `noreply` / `no-reply` 一类，以及 `installer/ChineseSimplified.isl`
      文件头里 Inno Setup 简体中文翻译译者的署名邮箱（这份第三方语言文件原样保留）。
    """
    user_dir = re.compile(r"(?i)C:[\\/]+Users[\\/]+([^\\/\s\"'<>%]+)")
    user_ok = {"你的用户名", "用户名", "username", "user", "yourname", "your-name", "name", "public", "default"}
    proj_dir = re.compile(r"(?i)[A-Z]:[\\/]+(Project|Projects|ZcodeProject|Zcode|workspace|code|dev|repos|src|git|personal|home)(?![A-Za-z0-9_])")
    mail = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    skip_dirs = {".venv", "build", "dist", ".pytest_cache", "__pycache__", ".git", "截图"}
    skip_files = {"REPORT.md"}  # 给 Planner 的施工报告不进公开仓库
    hits = []
    for f in ROOT.rglob("*"):
        rel = f.relative_to(ROOT)
        if not f.is_file() or f.name in skip_files or rel.parts[:2] == ("tests", "local") or set(rel.parts) & skip_dirs or f.suffix in (".png", ".ico", ".onnx", ".exe", ".zip", ".db"):
            continue
        if f.name in ("tokenizer.json", "README.md") and "models" in f.parts:
            continue
        try:
            t = f.read_text(encoding="utf-8")
            rel = str(rel).replace("\\", "/")
        except (UnicodeDecodeError, OSError):
            continue
        for m in user_dir.finditer(t):
            if m.group(1).lower() not in user_ok:
                hits.append((rel, "C:/Users/" + m.group(1)))
        for m in proj_dir.finditer(t):
            hits.append((rel, m.group(0)))
        for m in mail.finditer(t):
            if rel == "installer/ChineseSimplified.isl" or "noreply" in m.group(0).lower().replace("-", ""):
                continue
            hits.append((rel, m.group(0)))
    assert not hits, hits
    # 默认数据里没有人物卡、账号、角色
    seed_files = (ROOT / "mb" / "core.py").read_text(encoding="utf-8")
    assert "HAITUN" not in seed_files and "pe_haitun" not in seed_files


def test_vision_check_tool_builds_image_request(env):
    """tools/vision_check.py：画一张数字小图，按服务商的协议拼成看图请求发出去；答对了才算「能看图」。"""
    async def body(e):
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import vision_check
        got = {}

        def chat(req, b):
            got.update(b)
            return {"choices": [{"message": {"content": "123"}}], "usage": {"prompt_tokens": 90, "completion_tokens": 2}}
        mock = Mock().on("POST", "/v1/chat/completions", chat)
        await mock.start()
        r = await vision_check.check("deepseek", "deepseek-flash", "k", base_url=mock.base + "/v1", digits="123")
        part = got["messages"][0]["content"][0]
        assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,") and got["model"] == "deepseek-flash"
        assert r["ok"] and r["answer"] == "123"
        wrong = await vision_check.check("deepseek", "deepseek-flash", "k", base_url=mock.base + "/v1", digits="999")
        assert not wrong["ok"]
        await mock.stop()
    env.run(body)


def test_vision_test_endpoint_and_recommended_embedding(env):
    async def body(e):
        from mb import embed
        assert embed.recommended_local(16) == embed.recommended_local(31.8) == "gte-multilingual-base"
        assert embed.recommended_local(15.9) == embed.recommended_local(8) == "bge-small-zh-v1.5"
        srv, c = await api_client(e)
        info = await (await c.get("/api/embed")).json()
        assert info["recommended"] == embed.recommended_local(info["ram_gb"]) and info["ram_gb"] > 0
        # 模拟的模型：把图里的数字「读」出来（模拟服务只回固定的 123，所以第一次必然没读对，第二次让它回对）
        reply = {"text": "不知道"}

        def chat(req, b):
            return {"choices": [{"message": {"content": reply["text"]}}], "usage": {"prompt_tokens": 120, "completion_tokens": 3}}
        mock = Mock().on("POST", "/v1/chat/completions", chat)
        await mock.start()
        e.core.db.put("endpoint", {"id": "vis", "provider": "deepseek", "protocol": "openai", "base_url": mock.base + "/v1"})
        e.core.db.set_secret("endpoint:vis", "k")
        j = await (await c.post("/api/endpoints/vis/vision_test", json={"model": "m1"})).json()
        assert j["ok"] is False and j["model"] == "m1"
        import mb.visioncheck as vc
        orig = vc.random.choice
        vc.random.choice = lambda s: "7"  # 固定图里的数字 777，好让模拟服务答对
        reply["text"] = "777"
        try:
            j = await (await c.post("/api/endpoints/vis/vision_test", json={"model": "m2"})).json()
        finally:
            vc.random.choice = orig
        assert j["ok"] is True and j["expected"] == "777"
        assert e.core.db.x("SELECT SUM(calls) FROM usage WHERE purpose='trial'").fetchone()[0] == 2  # 用量记在「试聊」下
        e.core.settings.set("global", "vis_model", "")
        assert (await c.post("/api/endpoints/vis/vision_test", json={"model": ""})).status == 400  # 没填模型名也没设看图模型
        await mock.stop()
        await c.close()
    env.run(body)
