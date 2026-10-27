"""定时查各家服务商的新模型：同系列出了新版本就提醒，用户在面板一键或私聊指令替换；永远不自动换。

状态存在 kv「model_watch」：
  last   上次检查的时间（没有 = 这次启动才开始计时，不一启动就查）
  seen   {服务商卡片id|live 或 cat: [模型id…]}  上次看到的模型（live = 问服务商，cat = 内置目录；两种分开记，免得来源一换就当成新模型）
  fresh  {卡片id: [{id, ts}]}  这家新出的、不是同系列新版本的模型（面板上列出来）
  props  {卡片id|旧|新: {eid, old, new, ts, notified, state, until}}  提议；state：""（待回复）/ snooze / never / done
  partial 最近一次因为看图没过而没换的替换 {eid, old, new}（「换能换的」用）
"""
import re
import time

from . import providers, visioncheck

PURPOSES = [("main", "主模型"), ("gk", "守门员"), ("mem", "记忆提取"), ("vis", "看图")]
FRESH_KEEP = 50
DONE_KEEP = 5
_DATE = re.compile(r"[-_]\d{6,8}(?=$|[-_])")
_PAIR = re.compile(r"(?<![\d.])(\d)-(\d)(?![\d.])")
_NUM = re.compile(r"\d+(?:\.\d+)*")


def series(mid: str) -> tuple[str, tuple] | None:
    """模型 id → (去掉版本数字后的样子, 版本号)。去掉日期后缀、把「4-5」当成 4.5；版本数字不是恰好一组的拿不准，返回 None。
    glm-5.3-flash → ("glm-.-flash", (5, 3))。"""
    t = _PAIR.sub(r"\1.\2", _DATE.sub("", mid.lower()))
    nums = _NUM.findall(t)
    if len(nums) != 1:
        return None
    return _NUM.sub(".", t), tuple(int(x) for x in nums[0].split("."))


def newer_in_series(old: str, new: str) -> bool:
    a, b = series(old), series(new)
    return bool(a and b and a[0] == b[0] and b[1] > a[1])


def _fmt_ctx(n: int) -> str:
    return f"{round(n / 1e6, 1):g}M" if n >= 1_000_000 else f"{round(n / 1000)}K"


def abilities(pid: str, old: str, new: str, vis_in_use: bool = False) -> str:
    """按目录比新旧两个模型能看图、能用工具、能读多长，有不同就写一句；目录里没有资料的项不写。"""
    a, b = providers.model_info(pid, old), providers.model_info(pid, new)
    if not a or not b:
        return ""
    out = ""
    if b.get("vision") and not a.get("vision"):
        out += "新模型还能看图。" if vis_in_use else "新模型还能看图（要拿它看图就回「看图也换」）。"
    elif a.get("vision") and not b.get("vision"):
        out += "注意：新模型不能看图。"
    if b.get("tools") and not a.get("tools"):
        out += "新模型还能用工具。"
    elif a.get("tools") and not b.get("tools"):
        out += "注意：新模型不能用工具。"
    if a.get("ctx") and b.get("ctx") and a["ctx"] != b["ctx"]:  # 目录里缺这一项就只跳过这一项
        out += f"新模型能读的长度从 {_fmt_ctx(a['ctx'])} 变成 {_fmt_ctx(b['ctx'])}。"
    return out


class ModelWatch:
    def __init__(self, eng):
        self.eng = eng
        self.db, self.settings, self.llm = eng.db, eng.settings, eng.llm
        self.refreshed = False

    # ---------- 状态 ----------
    def st(self) -> dict:
        s = self.db.kv("model_watch") or {}
        for k, v in (("last", 0), ("seen", {}), ("fresh", {}), ("props", {}), ("partial", None)):
            s.setdefault(k, v)
        return s

    def save(self, s: dict):
        self.db.set_kv("model_watch", s)

    # ---------- 哪些地方在用哪个模型 ----------
    def eid_of(self, v) -> str:
        """设置里的服务商 id → 真的服务商卡片 id（空的、已删的按默认那张）；没有卡片返回空。"""
        if v and self.db.doc("endpoint", v):
            return v
        return "default" if self.db.doc("endpoint", "default") else ""

    def uses(self) -> list[dict]:
        """所有作用域里被主模型、守门员、记忆提取、看图、备用用到的 (服务商卡片, 模型)：每一处一条。"""
        st = self.settings
        out = []
        for sc in ["global"] + [s for s in st.data if s != "global"]:
            own = st.data.get(sc, {})
            for p, label in PURPOSES:
                # 这一层自己写了模型，或者只盖了服务商、模型沿用上层：都算这一层在用，替换时在这一层写上新模型
                if sc != "global" and f"{p}_model" not in own and f"{p}_endpoint" not in own:
                    continue
                model = st.source(f"{p}_model", sc)[0]
                eid = self.eid_of(st.source(f"{p}_endpoint", sc)[0])
                if model and eid:
                    out.append(dict(scope=sc, key=f"{p}_model", purpose=label, model=model, eid=eid))
        g = st.data.get("global", {})
        if g.get("backup_model") and g.get("backup_endpoint"):
            eid = self.eid_of(g["backup_endpoint"])
            if eid:
                out.append(dict(scope="global", key="backup_model", purpose="备用", model=g["backup_model"], eid=eid))
        return out

    def ep_name(self, eid: str) -> str:
        ep = self.db.doc("endpoint", eid) or {}
        return ep.get("name") or (providers.provider(ep.get("provider", "")) or {}).get("name") or eid

    def provider_of(self, eid: str) -> str:
        return (self.db.doc("endpoint", eid) or {}).get("provider", "")

    # ---------- 检查 ----------
    async def step(self):
        """periodic() 里每轮调：到点才查；提醒到期的提议（暂不换到期等）。出错只记日志，不拖累别的定时任务。"""
        try:
            await self.check()
            self.remind()
        except Exception as ex:
            self.eng.log("error", "model", f"查新模型出错 {type(ex).__name__}: {ex}")

    async def _ids(self, eid: str) -> tuple[list[str], str]:
        try:
            ids = await self.llm.list_models(eid)
            if ids:
                return list(ids), "live"
        except Exception:
            pass
        if not self.refreshed:  # 拉不到列表：先把目录联网更新一次（一轮只更新一次），再用目录里的清单
            self.refreshed = True
            try:
                await providers.refresh(self.settings.g("proxy") or None)
            except Exception as ex:
                self.eng.log("debug", "model", f"更新服务商目录没成功：{ex}")
        return [m["id"] for m in providers.models_of(self.provider_of(eid))], "cat"

    async def check(self, force: bool = False) -> dict | None:
        """到点（或 force）才查。返回这次的报告；没到点返回 None。第一次看到某家只记不提醒。"""
        now = time.time()
        hours = int(self.settings.g("model_check_hours") or 0)
        s = self.st()
        if not force:
            if hours <= 0:
                return None
            if not s["last"]:
                s["last"] = now
                self.save(s)
                return None
            if now - s["last"] < hours * 3600:
                return None
        s["last"] = now
        self.refreshed = False
        uses = self.uses()
        report = {"eps": [], "errors": [], "props": []}
        for eid in dict.fromkeys(u["eid"] for u in uses):
            ids, src = await self._ids(eid)
            name = self.ep_name(eid)
            if not ids:
                report["errors"].append(name)
                continue
            key = f"{eid}|{src}"
            seen = s["seen"].get(key)
            s["seen"][key] = sorted(set(seen or []) | set(ids))
            if seen is None:
                report["eps"].append(dict(eid=eid, name=name, n=len(ids), baseline=True, new=[]))
                continue
            new_ids = [i for i in ids if i not in set(seen)]
            report["eps"].append(dict(eid=eid, name=name, n=len(ids), baseline=False, new=new_ids))
            used = {u["model"] for u in uses if u["eid"] == eid}
            made = set()
            for old in sorted(used):
                cands = [i for i in new_ids if newer_in_series(old, i)]
                if not cands:
                    continue
                best = max(cands, key=lambda i: series(i)[1])
                made.add(best)
                pk = f"{eid}|{old}|{best}"
                if pk in s["props"]:
                    continue
                for k, p in list(s["props"].items()):  # 同一个旧模型已有更低版本的待回复提议：换成这个更新的
                    if p["eid"] == eid and p["old"] == old and p["state"] == "":
                        del s["props"][k]
                prop = dict(eid=eid, old=old, new=best, ts=now, notified=0, state="", until=0)
                same = next((p for p in s["props"].values() if p["eid"] == eid and p["new"] == best and p["state"] in ("snooze", "never")), None)
                if same:  # 这个新模型已经被用户回过「不换」：不再当成新提议
                    prop.update(state=same["state"], until=same["until"], notified=same["notified"])
                s["props"][pk] = prop
                report["props"].append(pk)
            fresh = [dict(id=i, ts=now) for i in new_ids if i not in made]
            if fresh:
                s["fresh"][eid] = (fresh + s["fresh"].get(eid, []))[:FRESH_KEEP]
        in_use = {(u["eid"], u["model"]) for u in uses}
        for k, p in list(s["props"].items()):  # 旧模型已经没人用了：待回复和暂不换的作废（一直不换的留着，面板上能撤销）
            if p["state"] in ("", "snooze") and (p["eid"], p["old"]) not in in_use:
                del s["props"][k]
        done = [k for k, p in s["props"].items() if p["state"] == "done"]
        for k in sorted(done, key=lambda k: s["props"][k]["ts"])[:-DONE_KEEP]:
            del s["props"][k]
        self.save(s)
        return report

    # ---------- 提醒 ----------
    def text_of(self, p: dict) -> str:
        uses = self.uses()
        purposes = list(dict.fromkeys(u["purpose"] for u in uses if u["eid"] == p["eid"] and u["model"] == p["old"])) or ["主模型"]
        diff = abilities(self.provider_of(p["eid"]), p["old"], p["new"], "看图" in purposes)
        days = int(self.settings.g("model_snooze_days") or 0)
        return (f"「{self.ep_name(p['eid'])}」出了新模型 {p['new']}，你现在用的是 {p['old']}（{'、'.join(purposes)}）。{diff}"
                f"私聊回我「换」就一起换掉；回「不换」过 {days} 天再提醒，回「一直不换」就不再提。也可以在面板 设置 → 模型与接口 里换。")

    def remind(self):
        """还没提醒过的提议提醒一次；暂不换到期的，再提醒一次。不联网。"""
        now = time.time()
        s = self.st()
        due = []
        for p in s["props"].values():
            if p["state"] == "snooze" and p["until"] <= now:
                p.update(state="", notified=0, until=0)
            if p["state"] == "" and not p["notified"]:
                due.append(p)
        for p in due:
            p["notified"] = now
        self.save(s)
        for p in due:
            self.eng.notify(self.text_of(p))

    def pending(self) -> dict | None:
        """最近一条待回复的提议（已经提醒过、没回「不换」的）。"""
        ps = [p for p in self.st()["props"].values() if p["state"] == "" and p["notified"]]
        return max(ps, key=lambda p: p["notified"]) if ps else None

    def latest(self) -> dict | None:
        """最近一条提议，换完的也算（「看图也换」用）；暂不换、一直不换的不算。"""
        ps = [p for p in self.st()["props"].values() if p["state"] in ("", "done") and (p["notified"] or p["state"] == "done")]
        return max(ps, key=lambda p: p["notified"] or p["ts"]) if ps else None

    def mark(self, p: dict, state: str, days: float = 0):
        s = self.st()
        k = f"{p['eid']}|{p['old']}|{p['new']}"
        if k in s["props"]:
            s["props"][k].update(state=state, until=time.time() + days * 86400 if days else 0)
            if state == "":  # 撤销暂不换 / 一直不换：下一轮重新提醒
                s["props"][k]["notified"] = 0
            self.save(s)

    def snooze(self, p: dict, days: int | None = None) -> str:
        days = int(days) if days is not None else int(self.settings.g("model_snooze_days") or 0)
        self.mark(p, "snooze", max(days, 0))
        return f"好，先不换，{days} 天后再提醒 {p['new']}。"

    def never(self, p: dict) -> str:
        self.mark(p, "never")
        return f"好，以后不再提 {p['new']}。面板里可以撤销。"

    def revoke(self, key: str) -> bool:
        p = self.st()["props"].get(key)
        if not p or p["state"] not in ("snooze", "never"):
            return False
        self.mark(p, "")
        self.remind()
        return True

    # ---------- 替换 ----------
    def vis_global(self) -> tuple[str, str]:
        g = self.settings
        return self.eid_of(g.g("vis_endpoint")), g.g("vis_model")

    async def _probe(self, eid: str, model: str, vision: bool) -> str:
        """真调一次新模型（vision：再让它读一张小图）。返回空 = 通过，否则是原因。"""
        ep, key = self.llm.endpoint(eid)
        try:
            r = await self.llm._call_full(eid, model, [{"role": "user", "content": "只回答一个字：好"}], 0.0, "", 64)
        except Exception as ex:
            return f"用 {model} 真调了一次没成功（{str(ex)[:120]}）"
        u = r["usage"]
        self.db.add_usage(None, "", "", "trial", model, u["prompt"], u["cached"], u["completion"], u["reasoning"], u["cache_write"])
        if vision:
            try:
                v = await visioncheck.check(self.llm, ep, key, model)
            except Exception as ex:
                return f"{model} 没通过看图测试（{str(ex)[:120]}）"
            u = v["usage"]
            self.db.add_usage(None, "", "", "trial", model, u["prompt"], u["cached"], u["completion"], u["reasoning"], u["cache_write"])
            if not v["ok"]:
                return f"{model} 没通过看图测试（读不出测试图）"
        return ""

    async def replace(self, eid: str, old: str, new: str, only_able: bool = False) -> dict:
        """把所有作用域里「用这家服务商的 {旧}」换成新。先真调新模型一次，有看图的地方再测看图；不通过一处都不换。
        返回 {ok, text, vision_failed}；text 就是回给用户的话。"""
        places = [u for u in self.uses() if u["eid"] == eid and u["model"] == old]
        if not places:
            return dict(ok=False, vision_failed=False, text=f"没换：现在没有地方在用 {old}。")
        vis = [u for u in places if u["key"] == "vis_model"]
        if only_able:
            places = [u for u in places if u not in vis]
        try:
            why = await self._probe(eid, new, bool(vis) and not only_able)
        except Exception as ex:
            why = str(ex)
        if why:
            s = self.st()
            vision_failed = "看图测试" in why
            if vision_failed:
                s["partial"] = dict(eid=eid, old=old, new=new)
                self.save(s)
                why += f"。要只换看图以外的几处，就回「换能换的」（看图仍用 {old}）"
            return dict(ok=False, vision_failed=vision_failed, text=f"没换：{why}。")
        for u in places:
            self.settings.set(u["scope"], u["key"], new)
        s = self.st()
        s["partial"] = None
        for p in s["props"].values():
            if p["eid"] == eid and p["old"] == old and p["new"] == new and p["state"] in ("", "snooze"):
                p["state"] = "done"
        self.save(s)
        names = "、".join(dict.fromkeys(u["purpose"] for u in places))
        self.eng.log("info", "model", f"替换模型：「{self.ep_name(eid)}」上的 {old} → {new}，换了 {len(places)} 处："
                     + "；".join(f"{u['scope']} {u['purpose']}" for u in places))
        text = f"已把 {old} 换成 {new}（{names}，共 {len(places)} 处）"
        if only_able and vis:
            return dict(ok=True, vision_failed=False, text=text + f"；看图仍用 {old}，因为新模型看不了图。")
        return dict(ok=True, vision_failed=False, text=text + f"。想换回去就私聊我「换模型 {new} {old}」。")

    def vision_ok_for(self, p: dict) -> bool:
        """「看图也换」适用吗：新模型目录里写着能看图，看图那一项既没在用旧模型、也已经不是新模型。"""
        info = providers.model_info(self.provider_of(p["eid"]), p["new"])
        if not info or not info.get("vision"):
            return False
        if any(u["key"] == "vis_model" and u["eid"] == p["eid"] and u["model"] == p["old"] for u in self.uses()):
            return False
        return self.vis_global() != (p["eid"], p["new"])

    async def replace_vision(self, p: dict) -> dict:
        eid, new = p["eid"], p["new"]
        why = await self._probe(eid, new, True)
        if why:
            return dict(ok=False, text=f"没换：{why}。")
        self.settings.set("global", "vis_endpoint", eid)
        self.settings.set("global", "vis_model", new)
        self.eng.log("info", "model", f"看图换成了「{self.ep_name(eid)}」的 {new}")
        text = f"已把看图换成 {new}。"
        if not self.settings.g("vision_enabled"):
            text += "看图现在是关着的，要用就在面板打开。"
        return dict(ok=True, text=text)

    async def model_ids(self, eid: str) -> list[str]:
        return (await self._ids(eid))[0]

    # ---------- 面板和私聊共用的动作 ----------
    async def act(self, action: str, key: str) -> dict:
        """面板按钮：replace（一键替换）/ replace_able（只换能换的）/ replace_vision（看图也换）/ snooze / never / revoke。"""
        p = self.st()["props"].get(key)
        if not p:
            return dict(ok=False, text="这条提议已经不在了。")
        if action == "revoke":
            return dict(ok=self.revoke(key), text="已撤销，会重新提醒。")
        if action == "snooze":
            return dict(ok=True, text=self.snooze(p))
        if action == "never":
            return dict(ok=True, text=self.never(p))
        if action == "replace_vision":
            return await self.replace_vision(p)
        if action in ("replace", "replace_able"):
            return await self.replace(p["eid"], p["old"], p["new"], only_able=action == "replace_able")
        return dict(ok=False, text="不认识这个操作。")

    def view(self) -> dict:
        """面板用：按服务商卡片分组的提议、新出的模型、暂不换和一直不换的。"""
        s, now = self.st(), time.time()
        eps = {}
        for k, p in s["props"].items():
            e = eps.setdefault(p["eid"], dict(pending=[], snoozed=[], never=[], fresh=[]))
            row = dict(key=k, old=p["old"], new=p["new"], until=p["until"], diff=abilities(self.provider_of(p["eid"]), p["old"], p["new"]))
            if p["state"] == "":
                e["pending"].append(dict(row, vision=self.vision_ok_for(p),
                                         partial=bool(s["partial"]) and (s["partial"]["eid"], s["partial"]["old"], s["partial"]["new"]) == (p["eid"], p["old"], p["new"])))
            elif p["state"] == "snooze":
                e["snoozed"].append(row)
            elif p["state"] == "never":
                e["never"].append(row)
        for eid, rows in s["fresh"].items():
            eps.setdefault(eid, dict(pending=[], snoozed=[], never=[], fresh=[]))["fresh"] = rows
        return dict(hours=int(self.settings.g("model_check_hours") or 0), last=s["last"], now=now, eps=eps)

    def summary(self, report: dict) -> str:
        """一次检查的结果，几行字。"""
        lines = []
        for e in report["eps"]:
            if e["baseline"]:
                lines.append(f"「{e['name']}」第一次查，先记下现在的 {e['n']} 个模型，以后有新的再告诉你。")
            elif e["new"]:
                lines.append(f"「{e['name']}」新出了 {len(e['new'])} 个模型：{'、'.join(e['new'][:5])}{'……' if len(e['new']) > 5 else ''}。")
        lines += [f"「{n}」没拿到模型列表（连目录里也没有）。" for n in report["errors"]]
        return "\n".join(lines) or "这次没发现新模型。"
