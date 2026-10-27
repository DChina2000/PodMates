"""设置项定义与分层继承。

层级（越往前越具体，先找到的生效）：
  账号@群 → 角色@群 → 群 → 角色@平台 → 角色 → 平台 → 全局默认 → 内置默认
作用域写法：global / platform:qq / role:<id> / role:<id>|platform:qq / chat:qq:<群号> / role:<id>|chat:qq:<群号> / acct:<账号id>|chat:qq:<群号>
「账号@群」只有群里的管理指令（本群插话开关、插话力度）会写，面板群页也能看到、能清除；它比所有面板上的设置都具体，所以指令设的值优先。
"""
import json

G, P, R, RP, C, RC, AC = "G", "P", "R", "RP", "C", "RC", "AC"
ALL = (G, P, R, RP, C, RC)
SCOPE_LABEL = {G: "全局默认", P: "平台", R: "角色", RP: "角色在平台", C: "群", RC: "角色在群", AC: "账号在群"}

FILE_FORMAT_OPTIONS = [["pdf", "PDF"], ["word", "Word（docx）"], ["excel", "Excel（xlsx）"], ["ppt", "PowerPoint（pptx）"], ["text", "文本与代码"], ["image", "图片"]]
EFFORTS = [["", "不思考/默认"], ["low", "低"], ["medium", "中"], ["high", "高"], ["max", "最高"]]
EAGER = [[1, "很少"], [2, "偏少"], [3, "正常"], [4, "偏多"], [5, "话痨"]]
COMMANDS = {
    "mute": "闭嘴N分钟", "unmute": "恢复", "persona": "换人设/换角色（临时，可带时长）", "proactive": "本群插话开关",
    "eagerness": "插话力度", "status": "状态", "recall": "撤回上一条", "forget": "忘掉某人或某事",
}

SCHEMA: list[dict] = []


def S(key, label, type, default, cat, scopes=ALL, adv=False, help="", options=None, pdef=None, platforms=None, when=None):
    """when：{别的设置项: 值}，面板上只在这些条件都满足时才显示这一项（比如选了「按比例」才显示比例）。"""
    SCHEMA.append(dict(key=key, label=label, type=type, default=default, cat=cat, scopes=list(scopes), adv=adv,
                       help=help, options=options, pdef=pdef or {}, platforms=platforms, when=when))


# ---- 回复 ----
S("persona", "人设", "persona", "", "回复", (RP, RC), help="角色在某平台、某群可以换一套人设（「换人设」指令改的就是这一项）")
S("enabled", "在这里说话", "bool", True, "回复", (P, RP, C, RC), help="关掉后只读不发（也不进守门员）")
S("private_scope", "私聊", "select", "all", "回复", (G, P, R, RP),
  options=[["all", "所有人"], ["list", "只回人物表里的人"], ["off", "关闭"]])
S("wake_words", "唤醒词", "list", [], "回复", help="消息里出现这些词就当作叫她，必回。比如她的名字")
LANGS = [["zh", "中文（默认）"], ["follow", "跟随对方的语言"], ["en", "English"], ["fr", "Français"], ["de", "Deutsch"],
         ["it", "Italiano"], ["ru", "Русский"], ["ja", "日本語"], ["es", "Español"], ["ko", "한국어"], ["ar", "العربية"],
         ["fa", "فارسی"], ["pt", "Português"], ["zh-TW", "繁體中文"], ["nl", "Nederlands"], ["tr", "Türkçe"], ["pl", "Polski"],
         ["id", "Bahasa Indonesia"], ["th", "ไทย"], ["vi", "Tiếng Việt"], ["hi", "हिन्दी"]]
LANG_NAME = {k: v for k, v in LANGS if k not in ("zh", "follow")}
S("reply_lang", "回复语言", "select", "zh", "回复", options=LANGS,
  help="角色、平台、群三层都能设；默认中文。「跟随对方的语言」= 对方用什么语言就用什么语言回")
S("admit_ai", "承认是AI", "bool", True, "回复",
  help="开：被问是不是AI就认。关：提示词里不写「是AI」这类句子（人设里的「AI身份」模块和各模板里写着承认是AI的句子都归这一项管）。"
       "总开关在面板「总览」页最上面；角色页可以给单个角色设成开/关（默认跟随总开关），某个平台、某个群里也能单独设")
S("strip_actions", "去掉括号里的动作描写", "bool", True, "回复",
  help="发送前把回复里括号里的动作描写去掉：整行只有一段「（尾巴晃了晃）」这样的，删掉这一行；行首、行尾单独一段的，去掉这一段。"
       "夹在正文中间的括号、「（含税）」「（2019 年）」这类补充说明不动；判断很保守，宁可漏删，不会误删。人设里不用写限制句")
S("strip_fake_images", "删掉回复里编出来的图片", "bool", True, "回复",
  help="发送前把回复里整行只是一个媒体标记的行删掉：「[图片：……]」「[表情包：……]」「[表情]」「[图片]」这类（那是程序给上下文写的格式，她有时会照着编出来冒充图片，其实什么都没发）。"
       "夹在句子里的不动；判断很保守，宁可漏删，不会误删。和「去掉括号里的动作描写」各管各的。删完整条回复空了就不发文字（她调了表情包工具的话，表情照发）")
S("error_notice", "模型出错时在群里发一句", "bool", False, "回复", adv=True,
  help="模型接口出错（已经自动重试过一次）时，被@的那次回复发一句「断线了……」。默认关，只在日志和面板总览里提示；打开后同一个群十分钟内最多发一次")
S("quote_reply", "回@时引用对方那条消息", "bool", True, "回复")
S("pro_quote", "主动插话时，被隔开了就引用她要回的那条", "bool", True, "回复", adv=True,
  help="主动插话发出去的时候，她要回的那条后面已经有这么多条别人的消息（下一项设几条），就引用那条；那条就是群里最新的就不引用。微信、QQ 都支持")
S("pro_quote_min", "她要回的那条后面至少有几条别人的消息才引用", "int", 1, "回复", adv=True,
  help="默认 1：后面有 1 条别人的消息就引用，那条就是最新的则不引用；设 0 = 主动插话总是引用她要回的那条")
S("at_convert", "把她写的 @名字 发成真正的 @", "bool", True, "回复", adv=True, platforms=["qq", "wechat"],
  help="她回复里写的「@名字」，名字和这个群里能对上的成员（最近上下文、人物表里的昵称 / 群备注）精确对上时，发成真正的 @（QQ 用 @ 段，可以在句中任何位置，原来那段文字换掉；微信的 @ 只能在消息开头，所以只转换写在一行开头的「@名字」，句子中间的保持原样文字）；对不上的保持原样文字")
S("poke_reply", "戳一戳有反应", "bool", True, "回复", platforms=["qq"])
S("qq_face", "把 [表情名] 转成 QQ 表情", "bool", True, "回复", platforms=["qq"], adv=True,
  help="她写出 [斜眼笑] 这类表情名时发成 QQ 表情。想让她常用，在人设里打开「QQ表情」平台模块")
S("output_format", "输出格式", "select", "plain", "回复", (G, P, RP), pdef={"discord": "markdown"},
  options=[["plain", "纯文本（去掉加粗、标题）"], ["markdown", "Markdown"]])
S("reply_max_chars", "单次回复最多字数（兜底，私聊也适用）", "int", 2000, "回复", adv=True)
S("group_reply_max", "群聊单次回复字数上限（私聊不限）", "int", 300, "回复",
  help="超出时让模型自己压缩到上限以内，不会在句子中间截断；拆条发送按压缩后的文字来拆。0 = 不设")
S("repeat_hint_n", "重复梗提示：统计她最近几条发言", "int", 12, "回复", adv=True,
  help="从她最近这几条发言里找出反复出现的梗、术语和比喻，下一次回复时提醒她换别的或不用（不额外调用模型）。0 = 不提醒")
S("stale_on", "发送前发现回复已过时就重新判断", "bool", True, "回复", adv=True,
  help="生成回复期间群里又来了新消息，发送前用最新的上下文重新判断：照发、改写，还是不发了（多花一次模型调用，只在过时时才有）")
S("stale_msgs", "新消息来了几条就算过时", "int", 3, "回复", adv=True)
S("stale_secs", "生成花了多久（秒）之后，只要有新消息就算过时", "int", 20, "回复", adv=True)
S("merge_at", "同一个群里排队的多次 @ 合并成一次回复", "bool", True, "回复", adv=True,
  help="消息积压时，同一个机器人被@了好几次，合成一条回复，分别照顾到每个提问的人"),
S("only_listed_chats", "只在群列表里登记过的群说话", "bool", False, "回复", (G, P, RP), adv=True,
  help="打开后，新加入的群默认不说话，要到「群」页面里打开")
S("private_owner_note", "私聊对象在人物表里时，提醒她这是本人", "bool", True, "回复", (G, R), adv=True,
  help="私聊对象在人物表里时，在提示词末尾加一句「这次私聊的是××本人。」")

# ---- 插话 ----
S("proactive", "主动插话", "bool", True, "插话", ALL + (AC,), help="没人@她时交给守门员判断要不要接话")
S("eagerness", "插话力度", "select", 3, "插话", ALL + (AC,), options=EAGER,
  help="很少/偏少会少问守门员、冷却更长；偏多/话痨冷却更短。被@、私聊不受影响")
S("cooldown_seconds", "插话冷却（秒）", "int", 60, "插话")
S("pro_wait", "主动插话前等对方停几秒", "int", 15, "插话",
  help="最新那条消息的发言人停下来够这么多秒，才去问守门员要不要接；这期间他又发了，就重新计时。守门员和她看到的是这一串完整的话。"
       "默认 15 秒的来历：同一个人接着发的下一条，69% 在 15 秒内到；一串连发，90% 在 29 秒内发完。"
       "只管主动插话：被 @、被回复、被叫名字、戳一戳照旧马上处理，私聊默认先等对方说完 5 秒（见「私聊里等对方停几秒再回」）；等的这段时间不卡群里其他消息，也不额外调用模型（到点才问一次守门员，冷却、名额、连发条数、闭嘴等条件那时再照常判断）。"
       "这期间别人发了消息，这一条就不再插，留给最新那条。0 = 不等")
S("pro_wait_max", "最多等几秒", "int", 30, "插话", adv=True,
  help="从这一串连发的第一条算起，等到这么多秒就不再等，拿当时最新的那条去问守门员（一串连发 90% 在 29 秒内发完）")
S("hourly_limit", "每小时插话上限（整个账号合计）", "int", 360, "插话",
  help="按机器人账号算，这个号在所有群里加起来每小时最多主动说多少条（风控按账号算）")
S("hourly_limit_chat", "每小时插话上限（每个群）", "int", 90, "插话",
  help="单个群里每小时最多主动说多少条（发表情包也算一条）；0 = 不设")
S("daily_limit", "每天插话上限（每群）", "int", 600, "插话")
S("quiet_hours", "静默时段", "str", "", "插话", help="如 01:00-08:00，留空为不设")
S("quiet_mode", "静默时段里", "select", "no_proactive", "插话",
  options=[["no_proactive", "不主动插话，@照回"], ["silent", "完全不回"]])
S("repeat_follow", "跟复读", "bool", False, "插话", help="两个以上的人发了同一句，她也跟一句")
S("cold_start", "冷场时开话题", "bool", False, "插话", (G, R, C, RC))
S("cold_minutes", "冷场多久算冷（分钟）", "int", 120, "插话", (G, R, C, RC), adv=True)
S("cold_hours", "只在这个时段开话题", "str", "10:00-23:00", "插话", (G, R, C, RC), adv=True)
S("cold_daily_max", "每群每天最多开几次话题", "int", 12, "插话", (G, R, C, RC), adv=True)
S("share_limit_on", "发言占比上限", "bool", True, "插话",
  help="在「统计最近几条」里，我们所有机器人在这个群的发言合计到了上限，就暂停主动插话。被@照回。上限可以按条数设，也可以按比例设")
S("share_window", "统计最近几条", "int", 15, "插话", adv=True,
  help="只数条数、不花 token；和给模型看的上下文条数是两回事。按条数、按比例共用。她一次回复拆成几行，按行算条数")
S("share_mode", "统计方式", "select", "count", "插话", adv=True, options=[["count", "按条数"], ["percent", "按比例"]],
  help="按条数：最近这些条里她们的发言到了「最多几条」就不再插话。按比例：占比到了「最多占比」就不再插话（四舍五入到整数百分比再比）")
S("share_max_count", "最多几条", "int", 7, "插话", adv=True, when={"share_mode": "count"},
  help="最近这些条消息里，我们所有机器人的发言到了这么多条，就暂停主动插话（默认 15 条里最多 7 条）")
S("share_max", "最多占比（%）", "int", 47, "插话", adv=True, when={"share_mode": "percent"},
  help="默认 47，和「15 条里 7 条」差不多")
S("share_min_on", "样本少时不拦", "bool", False, "插话", adv=True,
  help="最近的消息还不满下面这个条数时，不拦主动插话。主要配合按比例使用（消息很少时一两条发言就是很高的比例），按比例时建议打开")
S("share_min_n", "样本不满几条时不拦", "int", 10, "插话", adv=True, when={"share_min_on": True},
  help="实际的门槛是这个数和「统计最近几条」里小的那个")
S("eager_auto", "插话力度自动调节", "bool", False, "插话", (G, P, R, RP, C, RC),
  help="守门员顺带判断她上一次主动插话有没有人接：接得好就回升，被无视或被嫌烦就调低；每个群、每个机器人分开算")
S("eager_auto_down", "最多比设定的力度低几档", "int", 2, "插话", (G, P, R, RP, C, RC), adv=True)
S("eager_auto_up", "最多比设定的力度高几档", "int", 1, "插话", (G, P, R, RP, C, RC), adv=True)
S("stop_on", "有人说「别说了」就暂停主动插话", "bool", False, "插话",
  help="由守门员判断（同一次调用，不额外花钱），不靠关键词。该群所有机器人暂停主动插话；被@照样回，有人@她就提前恢复")
S("stop_minutes", "「别说了」后暂停多久（分钟）", "int", 30, "插话", adv=True,
  help="短时间内反复被叫停，时长翻倍递增，到下面的上限为止")
S("stop_max_minutes", "递增后最长暂停（分钟）", "int", 480, "插话", adv=True)
S("prefilter_on", "先用代码筛一遍，再问守门员", "bool", True, "插话", (G, P, C),
  help="只用于主动插话：纯表情、纯链接、签到、以 / 或 # 开头的指令，直接跳过，不调守门员。被@照旧必回。规则在 设置 → 全局默认 → 预筛规则 里改")
S("bot_skip_gate", "别的机器人的消息跳过守门员", "bool", True, "插话", adv=True,
  help="人物表里被标成「这是别的机器人」的人发的消息，不去问守门员（省调用，也免得两个 AI 没完没了互相接话）")
S("bot_at_mode", "别的机器人 @ 她或回复她时", "select", "limit", "插话", adv=True, options=[["ignore", "不理它"], ["limit", "最多接 N 轮"], ["normal", "照常回复"]],
  help="「最多接 N 轮」：和同一个别的机器人连续互接到 N 轮就停，等真人在这个群开口后重新计数；和我们自己几个机器人之间的互接上限分开算")
S("bot_at_rounds", "「最多接 N 轮」里的 N", "int", 1, "插话", adv=True)
S("shadow", "影子模式", "bool", False, "插话", (R, RP, C, RC),
  help="只读不发：面板「消息」页显示她本来会说什么，用来调新角色")

# ---- 多机器人 ----
S("max_bots_per_message", "一条消息最多几个机器人回", "int", 2, "多机器人", (G, P, C),
  help="只限制主动插话的机器人：被 @（或被回复、被叫名字）的机器人都会回，先占名额，剩下的名额才给主动插话的")
S("bot_chain_limit", "机器人之间连续接话最多几轮", "int", 3, "多机器人", (G, P, C),
  help="到了就停，等真人开口")
S("shift_greeting", "换班时新角色在群里打个招呼", "bool", False, "多机器人", (G, P, C),
  help="排班规则或临时换角色后，新上岗的角色说一两句")
S("shift_card", "QQ 群名片随角色自动改", "bool", False, "多机器人", (G, C), platforms=["qq"],
  help="换班时把机器人在这个群的群名片改成角色的名片（角色页可设，没设就用角色名）。头像、昵称是整个账号共用的，不随群或时段变")
S("sibling_relation", "同群其他机器人对她来说是", "str", "姐妹", "多机器人", (G, R), adv=True,
  help="默认关系；在角色页可对单个机器人另设")

# ---- 上下文 ----
S("context_window", "群上下文条数", "int", 30, "上下文")
S("context_max_chars", "每条最多字数", "int", 3000, "上下文", help="给模型看的上下文里，每条消息最多留这么多字；大多数消息用不到，长消息也不会被截断")
S("max_history", "私聊记住几轮", "int", 10, "上下文", adv=True)

# ---- 模型 ----
for p, name, model, eff, temp, scopes in [
    ("main", "主模型", "deepseek-flash", "high", 0.8, ALL),
    ("gk", "守门员", "deepseek-flash", "high", 0.2, ALL),
    ("mem", "记忆提取", "deepseek-flash", "high", 0.3, (G, R, C)),
    ("vis", "看图", "deepseek-flash", "high", 0.2, (G, P, C)),
]:
    S(f"{p}_endpoint", f"{name}服务商", "endpoint", "default", "模型", scopes, adv=p != "main")
    S(f"{p}_model", f"{name}模型", "model", model, "模型", scopes, adv=p != "main",
      help="deepseek-flash 是 DeepSeek 官方的别名，永远指向最新的 Flash" if p == "main" else "")
    S(f"{p}_effort", f"{name}思考强度", "select", eff, "模型", scopes, adv=True, options=EFFORTS,
      help="选项按这个模型真正支持的档位列出（服务商目录里有资料的才限制），不同服务商的写法程序会自动对应；不认识的服务商不发思考参数。"
           "GLM-5.3 这类模型不能关思考，只有 低 / 高 / 最高，不选就等于最高；以前存的「中」这类模型不支持的值，发请求时自动换成最接近的（中 → 高），别的值不动。"
           "默认都是「高」，模型不支持「高」时按它支持的档位取最接近的" if p == "main" else "")
    S(f"{p}_temperature", f"{name}温度", "float", temp, "模型", scopes, adv=True)
S("vision_enabled", "看图", "bool", False, "模型", (G, P, C),
  help="群里有人发图时，先让看图模型说一句图里是什么再交给她。每张图多一次调用；要选支持看图的模型")
S("vision_stickers", "群友发的表情也看", "bool", True, "模型", (G, P, C), when={"vision_enabled": True},
  help="微信、QQ 里群友发的表情（自定义表情、动图）也交给看图模型，在上下文里写成「[表情：……]」。表情每张多一次看图调用，关掉能省用量，关掉后表情只写「[表情]」，普通照片照常看。微信的表情是截聊天里那一块、裁到表情本身给看图模型看（截之前会先关掉盖在那块上的微信自己的小菜单，只对那个菜单发 Esc 或关闭，不碰主窗口；关不掉或者被别的窗口盖住就不截；微信读完消息留在头像旁边的「@某某／拍一拍」小菜单，每次读完消息都会对它发一次 Esc，不碰主窗口，关不掉就留着、记一条日志），动图连截三帧一起看，同一张图只看一次，结果会记下来")
S("vis_search", "群里看图时联网搜索", "bool", False, "模型", (G, P, C),
  help="看图模型说一句图里是什么时，需要的话可以上网查一下。会让这次回复慢几秒，算进「每天最多搜索几次」")
S("max_tokens", "单次回复最多 tokens（0 为不限）", "int", 0, "模型", (G,), adv=True,
  help="Anthropic 协议必须有上限，0 时用 8192")
S("backup_enabled", "主服务商出错时切到备用", "bool", False, "模型", (G,))
S("backup_endpoint", "备用服务商", "endpoint", "", "模型", (G,))
S("backup_model", "备用服务商用的模型（留空沿用原模型）", "str", "", "模型", (G,), adv=True)
S("proxy", "代理地址", "str", "", "模型", (G,),
  help="访问海外服务商和海外平台（Telegram、LINE、Reddit、YouTube、Discord……）用，如 http://127.0.0.1:7890；各服务商、各账号再单独选用不用")
# ---- 学习与记住 ----
S("style_learn", "学群里的说话习惯", "bool", True, "学习", (G, C),
  help="每个群攒一份风格画像（句子长短、标点、口头禅、表情习惯），在整理记忆那次调用里顺带更新，不额外花钱")
S("style_follow", "说话时贴近这个群的习惯", "bool", True, "学习",
  help="只影响说话的表面形式；人设、身份与态度、回复语言、平台格式要求永远优先。学术讲解、生活健康类模板默认关")
S("style_min_msgs", "攒够多少条群友消息才生成画像", "int", 30, "学习", (G,), adv=True)
S("remember_on", "「记住……」", "bool", True, "学习", (G, P, R, C),
  help="有人对她说「记住……」就写进他的人物备忘（来源标为「本人要求记住」，自动整理记忆时不会覆盖）")
S("remind_on", "提醒", "bool", True, "学习", (G, P, R, C),
  help="有人说「N 分钟后提醒我……」「明天 8 点提醒我……」，到点由当时在这个群值班的角色 @ 他提醒")
S("remind_max", "每人同时最多挂几条提醒", "int", 5, "学习", (G,), adv=True)

# ---- 工具：联网搜索、表情包 ----
S("search_on", "联网搜索", "bool", True, "工具", (G, P, R, C),
  help="主模型需要最新信息时，通过工具调用上网搜一下。搜索结果只用于当次回复，不写进记忆。要在 设置 → 模型与接口 里选搜索服务；当前模型不支持工具调用时这一项不生效")
S("search_daily", "每天最多搜索几次", "int", 100, "工具", (G,), adv=True)
S("search_rounds", "一次回复最多搜几轮", "int", 2, "工具", (G,), adv=True)
S("sticker_learn", "学表情包", "bool", True, "工具", (G, P, C),
  help="只学群里「常用」的：同一张图被多人或多次发过才学。要选好看图模型（设置 → 模型与接口）；按描述建向量，按群存")
S("sticker_use", "发表情包", "bool", True, "工具", (G, P, R, C),
  help="发的时机和发哪张都由主模型在回复时决定（工具调用：按心情找出几张候选，她看描述自己挑，也可以都不选）。发一张算一条发言，计入插话上限。不能发图的平台自动跳过。私聊里也能发，但只从这个账号自己的收藏里挑（QQ 的收藏表情、微信的自定义表情），私聊没有学来的表情包，发一张算私聊里连着发的一条")
S("sticker_call", "被叫到时总给她表情包工具", "bool", True, "工具", (G, P, R, C),
  help="开着时，被@、被回复、被叫名字的这次回复，只要这个平台能发、这个账号有可用的表情，就一定给她 send_sticker 工具，不看「多大概率给她这个工具」。"
       "给工具不等于每次都发：发不发由她看气氛决定。关掉时被叫到也按概率给")
S("sticker_chance", "多大概率给她这个工具（%）", "int", 100, "工具", (G, P, R, C), adv=True,
  help="默认 100：每次回复都给她这个工具（有可用的表情、平台能发的话）。给工具不等于每次都发：发不发由她看气氛决定，嫌她发得多就调低这个概率。"
       "只管主动插话、冷场开话题这两类（「被叫到时总给她表情包工具」开着时）；关掉那一项时，被叫到也按这个概率给。私聊里每次回复都是在回对方，按「被叫到」算。"
       "这次回复没给她工具（概率调低了、平台不能发、模型不支持工具）、而「发表情包」开着且本该有表情可用时，提示词末尾会加一句「这次回复发不了表情包，只能发文字。」，免得她说要发却什么也没发")
S("sticker_candidates", "发表情包时给她看几张候选", "int", 10, "工具", (G,), adv=True,
  help="她用发表情包的工具时，按心情找出这么多张最接近的，把描述给她挑；她也可以都不选")
S("sticker_desc_max", "表情包描述最多多少字", "int", 250, "工具", (G,), adv=True,
  help="看图模型给每张表情包写描述时的字数上限。描述写清画面、图上的字、情绪和适合的场合，挑表情包时会给她看。改了以后，已有的表情包按「改了字数后自动重新描述已有的」处理")
S("sticker_desc_auto", "改了字数后自动重新描述已有的", "bool", True, "工具", (G,), adv=True,
  help="开着时，已有的表情包里按别的字数上限描述的，会在后台用看图模型重新描述一遍（每张一次调用，算在看图的用量里），不挡回复；关着就只影响以后新收的。面板上的「重新描述全部表情包」随时可以手动全部重来")
S("sticker_desc_search", "给表情包写描述时联网搜索", "bool", True, "工具", (G,), adv=True,
  help="看图写描述前先上网搜一下这张图的出处或含义（用 设置 → 模型与接口 里选的搜索服务），搜到的只作参考。不算进「每天最多搜索几次」，用量记在「表情包搜索」下。没有可用的搜索服务、或看图模型不支持工具时，照常只看图")
S("sticker_desc_search_max", "每张表情包最多搜几次", "int", 5, "工具", (G,), adv=True)
S("sticker_desc_search_auto", "搜不搜由看图模型自己判断", "bool", False, "工具", (G,), adv=True,
  help="关着：每张都至少搜一次。开着：看图模型认不出出处或含义时才搜")
S("sticker_block_retry", "内容审核拦下时重试几次（带搜索）", "int", 2, "工具", (G,), adv=True,
  help="给表情包写描述时被服务商的内容审核拦下，原样再试这么多次；还不行就去掉联网搜索再试（下一项）。都过不了就停手：重新描述时保留原来的描述，新收的表情不收，都记日志，以后不再试")
S("sticker_block_retry_plain", "内容审核拦下后不带搜索再试几次", "int", 3, "工具", (G,), adv=True,
  help="带搜索还过不了（或者本来就没开搜索）时，只看图再试这么多次")
S("sticker_min_times", "同一张图出现几次才学", "int", 3, "工具", (G, C), adv=True)
S("sticker_min_users", "或被几个人发过才学", "int", 2, "工具", (G, C), adv=True)
S("sticker_daily_learn", "每个群每天最多学几张", "int", 10, "工具", (G, C), adv=True)
S("sticker_max_count", "每个群最多存几张", "int", 200, "工具", (G, C), adv=True, help="满了先删用得最少的")
S("sticker_cache_mb", "表情包总容量上限（MB）", "int", 100, "工具", (G,), adv=True, help="满了先删用得最少的")
S("sticker_fav_use", "用账号收藏的表情", "bool", True, "工具", (G, P, R, C), platforms=["qq", "wechat"],
  help="账号收藏的表情也能被她用（QQ 是表情面板里的「收藏」，微信是表情面板里的「自定义表情」）：定期同步下来，用看图模型各写一句描述，和本群学来的表情包一起挑。这个账号所在的所有群都能用，不占「每个群最多存几张」，也不会被「满了先删用得最少的」删掉")
S("sticker_fav_hours", "多久对一次收藏（小时）", "int", 24, "工具", (G,), adv=True, platforms=["qq", "wechat"],
  help="程序启动、账号上线时也对一次（QQ 是收藏表情，微信是「自定义表情」；微信要打开表情面板一格一格翻着看，会占用微信窗口一会儿）。新出现的图下载下来描述一次，从收藏里删掉的，这里跟着删；在表情包页面也能点「现在同步收藏」")
S("sticker_fav_check", "收藏的表情也做内容检查", "bool", False, "工具", (G,), adv=True, platforms=["qq", "wechat"],
  help="默认关：收藏的（QQ 的收藏表情、微信的「自定义表情」）是你自己挑的，不按「是不是表情包、合不合适」丢弃。打开后和群里学来的一样检查，不合格的不收")

# ---- 读文件 ----
S("file_read", "读文件", "bool", False, "文件", (G, P, C),
  help="默认关闭。打开后她能读群里发的文件（PDF、Word、Excel、文本……），读到的内容切成片段存进记忆，以后能被想起")
S("file_mode", "什么时候读", "select", "called", "文件", (G, P, C),
  options=[["called", "被叫到才读（@她，或回复那条文件消息）"], ["auto", "收到文件就读"]])
S("file_formats", "允许的格式", "multi", ["pdf", "word", "excel", "ppt", "text"], "文件", (G, P, C),
  options=FILE_FORMAT_OPTIONS, help="只解析，不运行；图片交给看图模型")
S("file_max_mb", "单个文件大小上限（MB）", "int", 10, "文件", (G,))
S("file_max_tokens", "单次读取的 token 上限", "int", 6000, "文件", (G,),
  help="一份文件最多读这么多（约数），超出的部分不读")
S("file_keep_days", "保留原文件（天，0 = 读完就删）", "int", 0, "文件", (G,), adv=True)
S("file_cache_mb", "保留的原文件总容量上限（MB）", "int", 200, "文件", (G,), adv=True,
  help="满了自动删最旧的")
S("daily_token_limit", "每日用量上限（万 tokens，0 为不限）", "int", 0, "模型", (G, R),
  help="到了就停止主动插话；被@和私聊照常回，除非打开下面的「用量硬上限」")
S("usage_hard_cap", "用量硬上限", "bool", False, "模型", (G, R),
  help="当天用量达到上面的上限后，被@和私聊也不再调用模型，只回一句固定的话；记忆整理、看图、搜索、学表情包、接口向量也一律暂停（本机向量不受影响）。第二天自动恢复")
S("hard_cap_message", "用量用完时回的那句话", "text", "今天的额度用完了，明天再来", "模型", (G, R),
  help="按角色可设，用这个角色自己的口吻写")
S("gatekeeper_prompt", "守门员提示词模板", "text", "", "模型", (G,), adv=True,
  help="留空使用内置模板。{summary} {marks} {context} {speaker} {text} 会被替换；{user_line} 会换成按用户卡生成的那句（「她妈说了她有得接的话；」，没有用户卡或没填称呼就是空）")

# ---- 记忆 ----
S("memory_enabled", "长期记忆", "bool", True, "记忆", (G, C))
S("memory_auto_apply", "新记忆自动生效", "bool", True, "记忆", (G, R, C), help="关掉后新记忆要在「记忆」页审核")
S("memory_segment", "每多少条消息整理一次记忆", "int", 50, "记忆", (G, C))
S("memory_merge", "几段前情合并成上一层", "int", 5, "记忆", (G, C), adv=True)
S("memory_retention_days", "记忆保留天数（0 为永久）", "int", 0, "记忆", (G, C), adv=True)
S("memory_expire_person", "保留天数也让人物备忘过期", "bool", True, "记忆", (G, C), adv=True,
  help="默认开：人物备忘也按上面的「记忆保留天数」过期，过期后用这个人剩下的备忘重写他在这个记忆空间里的印象（和面板删除备忘后一样），一条都不剩就清空印象。"
       "关着：人物备忘不过期，其余照旧。「本人要求记住」的、风格画像、表情包和印象始终不按天数清")
S("semantic_enabled", "语义回忆", "bool", True, "记忆", (G,),
  help="按眼下聊的话题，找意思相近的旧记忆（没提到当事人、没碰上关键词也能想起来）。每个记忆空间还能在「记忆 → 记忆空间」里单独关")
S("semantic_top_k", "语义回忆最多带几条", "int", 3, "记忆", (G,), adv=True)
S("semantic_min_score", "语义相似度门槛（0 = 用模型自带的；0～1，越高越严）", "float", 0.0, "记忆", (G,), adv=True,
  help="不同模型分数高低不一样，默认用每个模型标定好的门槛。在「语义回忆 → 试一试」里看看分数再调")
S("memory_person_limit", "每人最多带几条最新备忘进提示词", "int", 100, "记忆", (G, R, C), adv=True,
  help="每个在场的人带进提示词的是：印象、最新这么多条备忘（带日期）、他要求记住的。群聊里一条备忘很短，所以默认 100。备忘永久留着；被挤出这个数的备忘并进印象，再往前的按人取回")
S("memory_ratio", "往上并时，每层的字数上限 = 所并各段实际字数之和 × 倍数", "float", 0.7, "记忆", (G,), options=[[0.5, "0.5"], [0.6, "0.6"], [0.7, "0.7"], [0.75, "0.75"], [0.8, "0.8"]],
  help="可选 0.5、0.6、0.7、0.75、0.8，也可以自己填。第一层不限字数；每 5 段并成上一层时，这一层最多写「所并各段实际字数之和 × 倍数」字。已经写成的单位不会重写，改了倍数只影响以后新并的")
S("memory_retrieve_share", "取回旧记录最多占记忆一节的百分之几", "int", 75, "记忆", (G, C), adv=True,
  help="按人取回的旧记录（已经并进上层、眼下没原样摆出来的前情，和在场的人没摆出来的旧备忘）最多占记忆一节空间的这么多；默认 75。空间紧时，取回不会挤掉最粗一层前情。可以按群单独设")
S("memory_retrieve_max", "取回旧记录一次至多几条", "int", 1000, "记忆", (G, C), adv=True,
  help="按人取回的旧记录一次至多带这么多条；默认 1000（群聊里一条很短，要多取几条才装得下同样多的事）。可以按群单独设")
S("memory_max", "记忆最多带多少字（0 = 不限）", "int", 0, "记忆", (G,), adv=True,
  help="默认不限：记忆一节能用多少，由模型能读多长减去回复长度和人设、人物表、原始消息等其余部分决定。想省用量就填一个数；群梗和语义回忆想起的旧事也算在里面。超出时按记忆一节的先后少放：先让这次没被提到的人的备忘（同一相关分里从最早的让），再让最早的前情单位；印象和「要求记住的」必放")

# ---- 节奏 ----
S("split_enabled", "短句分条发（回复里的每一行单独发一条）", "bool", True, "节奏",
  help="回复里每一行（句子）单独发成一条，像真人一句一句打字；一行特别长的再按句子拆。超过「中间没人说话时最多连着发几条」的，把最短的相邻几行并在一起。评论区类平台（X、Reddit、YouTube、B站、贴吧、微博）和 LINE（推送有额度）默认不拆，可以在平台设置里打开",
  pdef={p: False for p in ("x", "line", "reddit", "youtube", "bilibili", "tieba", "weibo")})
S("split_threshold", "一行超过多少字就按句子再拆", "int", 160, "节奏", adv=True)
S("split_interval", "每条之间至少隔（秒）", "float", 1.2, "节奏", adv=True)
S("typing_speed", "打字速度（字/秒，越小越慢）", "float", 40.0, "节奏", adv=True)
S("split_max_parts", "中间没人说话时最多连着发几条", "int", 4, "节奏", adv=True,
  help="她在一个群里（或一个私聊里）发出的每一个气泡都算一条：拆出来的每一行、单发的表情包、紧接着的下一次回复。只要有别人说话就从 0 重新数——被@、被回复也是别人说话，我们的另一个账号开口也算。"
       "这次回复剩下的条数不够时，把最短的相邻几行并起来，并到剩下的条数为止；剩下 0 条时主动插话、冷场开话题不发。"
       "私聊里按「哪个账号 × 哪个人」各数各的，对方说话就从 0 重新数；私聊里每次回复都是在回对方的话，按群里被@的规则，条数用完了也至少发一条。面板让她回、提醒、定时消息、指令回执不受这个上限拦，但发出的气泡照样计入。同一个群里我们的几个账号各数各的。不拆条的平台（评论区类、LINE）一次回复算一条。0 = 不限")
S("poke_counts", "有人戳她也算有人说话", "bool", True, "节奏", adv=True, platforms=["qq"],
  help="戳一戳不会记成聊天消息，这一项决定它算不算打断「连着发」的计数：开 = 有人戳她就从 0 重新数")
S("reply_min_gap", "两次回复最小间隔（秒）", "int", 10, "节奏",
  help="她在一个群里发完一次回复后，同一个群里不到这么多秒，不发下一次回复；每个群单独计时。被 @ 或被回复的消息不丢：排队，时间到了再回（这期间被叫到好几次的合成一条回复）；"
       "主动插话直接不插（它和「插话冷却」各算各的，取更严的）。同一次回复拆出来的几行不受影响。0 = 不限",
  pdef={p: 0 for p in ("demo", "x", "reddit", "youtube", "bilibili", "tieba", "weibo")})
S("reply_hourly", "每小时最多回几次（每个群，含被@）", "int", 0, "节奏",
  help="每个机器人账号在每个群里有一份回复名额，最多攒到「名额最多攒几次」那么多个；每回一次用掉一个，用掉的按「60 分钟 ÷ 每小时次数」的速度一个一个补回来，重启后还在。"
       "算一次回复的：被@、被回复、被叫名字，戳一戳，主动插话，冷场开话题，单独发的表情包（一次回复不管拆成几条都算一次，回复后面跟的表情包不另算）；"
       "面板让她回、提醒、定时消息、指令和指令回执、用量用完时回的那句话不算。"
       "名额用完时：主动插话和冷场开话题直接不发；被@、被回复、被叫名字、戳一戳的不丢，排队到下一个名额补回来再回（期间被叫到好几次的合成一条回复）。"
       "例：每小时 20 次、最多攒 5 次——一口气最多回 5 次，之后每 3 分钟补回一个。和插话的各项上限、冷却、发言占比、「两次回复最小间隔」叠加，取最严。0 = 不设")
S("reply_hourly_burst", "名额最多攒几次", "int", 0, "节奏", adv=True,
  help="名额最多能攒到多少个，也是刚开始时手里有几个。0 = 和「每小时最多回几次」一样")
S("reply_user_free", "用户本人叫她不算进上限", "bool", True, "节奏", adv=True,
  help="「用户本人」是人物表里固定的「用户」卡里的各平台账号。他@她、回复她、叫她名字、戳她的那次回复不用名额，也不排队；他的发言引出的主动插话照常计入")
S("private_wait", "私聊里等对方停几秒再回", "int", 5, "节奏", (G, P, R, RP),
  help="对方停下来够这么多秒才开始回；这期间他又发了，就重新计时，从这一串第一条算起到了「最多等几秒」（插话页）那么久就不再等。开始回时，对方这一串没回过的几条一起当作这次要回的话，合成一次回；"
       "她写回复期间对方又发了新的，发送前照群里「回复写好时已过时」的办法重新判断（照发 / 改写 / 不发了），不为新来的每一条再单独回一次。"
       "默认 5 秒的来历：同一个人接着发的下一条，三成多在 5 秒内到（10 秒内 59%，15 秒内 69%），她写回复本身也要十来秒，这期间补的话也会并进来。"
       "面板上「让她回这条」照旧马上回，不等；和下面的「私聊里两次回复最小间隔」各算各的，取更严的。0 = 不等，来一条回一条")
S("private_reply_gap", "私聊里两次回复最小间隔（秒）", "int", 0, "节奏", (G, P, R, RP),
  help="和上面那项一样，但管私聊（按「哪个账号 × 哪个人」各自计时）。默认 0 = 私聊不限")

# ---- 过滤 ----
S("blocked_words", "屏蔽词", "list", [], "过滤", (G, P, R, C), help="她不说这些词；含这些词的消息她不理")
S("blacklist", "黑名单（账号ID）", "list", [], "过滤", (G, P), help="QQ号、Discord ID 或微信备注，她完全不理")
S("friend_request", "好友申请", "select", "list", "过滤", (G, P),
  options=[["accept", "自动同意"], ["reject", "自动拒绝"], ["list", "仅人物表里的人同意"]], platforms=["qq"])
S("group_invite", "入群邀请", "select", "list", "过滤", (G, P),
  options=[["accept", "自动同意"], ["reject", "自动拒绝"], ["list", "仅人物表里的人邀请才同意"]], platforms=["qq"])

# ---- 权限 ----
S("cmd_perms", "管理指令权限", "perms", {}, "权限", (G, R),
  help="@她 + 固定说法。没权限的人发指令，她用自己的方式拒绝")

# ---- 平台 ----
S("napcat_clean_days", "每隔几天清理 NapCat/QQ 缓存（0 为不清理）", "int", 0, "平台", (P,), platforms=["qq"], adv=True)
S("wechat_poll", "微信检查新消息间隔（秒）", "float", 1.0, "平台", (P,), platforms=["wechat"], adv=True)
S("wechat_recv_mode", "微信收消息方式", "select", "auto", "平台", (P,), platforms=["wechat"],
  options=[["auto", "自动（微信版本在支持范围内用独立窗口，超出范围用主窗口轮询）"], ["window", "独立窗口（把每个监听的聊天拆成单独的窗口）"],
           ["poll", "主窗口轮询（不拆窗口：看会话列表有没有新消息，有就切过去读，读完切回原来的会话）"]],
  help="独立窗口是原来的方式；微信版本太新（比如 4.1.15）时拆窗口会报错，这时用主窗口轮询。选「自动」的话，独立窗口失败了也会自动改用轮询并写日志。"
       "主窗口轮询时，发消息也在主窗口里完成（切到那个聊天再发）。检查间隔见「微信检查新消息间隔」")
S("wechat_lib_check", "每天检查 wxauto4 有没有新版本", "bool", True, "平台", (P,), platforms=["wechat"], adv=True,
  help="每天查一次 PyPI。有新版就在面板总览和账号页提示，再私聊通知你一次，写明新版号和它声明适用到哪个微信版本；只提醒，不自动升级，"
       "要升级在「账号 → 微信」点「升级 wxauto4」（升级失败会自动回退到原来的版本）")
S("wechat_offscreen", "把微信窗口移到屏幕外", "bool", False, "平台", (P,), platforms=["wechat"],
  help="机器人运行期间，微信主窗口放到所有显示器之外（不是最小化，最小化后读不到界面）。要切换会话、发送时短暂移回来，做完再移出去；停止或退出时放回原位。"
       "注意：运行中别用小号手动聊天；切换会话时窗口会闪一下；仍然不能最小化。窗口留在屏幕外了（比如程序被强行结束）就点账号页微信卡片上的「找回微信窗口」")

# ---- 系统（只在全局）----
S("theme", "界面主题", "select", "system", "系统", (G,), options=[["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]])
S("autostart", "开机自启", "bool", False, "系统", (G,))
S("hide_console", "后台运行不弹黑窗口", "bool", True, "系统", (G,), help="NapCat 的命令行窗口藏起来，输出进日志")
S("notify_dm_owner", "出事时发消息给我（用户卡上的账号）", "bool", True, "系统", (G,),
  help="掉线、风控、微信版本不对这类事，发给「用户」卡本人：在所有在线账号里挑一个能联系到你的发（出事的那个账号除外）。"
       "QQ 按用户卡上的 QQ 号，微信按秘密备注，Discord、Telegram 按 ID。通知只私聊你本人：按卡上你在这个平台的账号挨个试，"
       "微信上要切过去后确认打开的正好是你那个私聊才发，对不上就换下一个；都发不出去就只留在面板的「未读通知」里，绝不会发到群里。"
       "不管开不开，面板总览里都会留一条「未读通知」")
S("notify_windows", "出事时弹 Windows 通知", "bool", True, "系统", (G,))
S("resource_alert", "资源告急时提醒我", "bool", True, "系统", (G,), adv=True,
  help="服务器的内存或硬盘快不够时，用「出事时发消息给我」的同一套办法通知用户，面板总览也留一条未读通知；写明当前数值和占用最多的进程。同一种提醒每天最多发一次，恢复正常后再出问题算新的一次。"
       "关掉后不再提醒，但日志页「资源」类别里每小时一行的记录照常写")
S("resource_mem_pct", "内存提醒线（已用虚拟内存占上限的百分之几）", "int", 90, "系统", (G,), adv=True,
  help="看的是已用虚拟内存（commit）占上限的百分比，不看剩余物理内存——Windows 会拿空闲内存做缓存，剩余物理内存平时就偏低，容易误报。默认 90")
S("resource_mem_minutes", "内存要连续超线多少分钟才提醒", "int", 5, "系统", (G,), adv=True,
  help="内存每分钟查一次；连续这么多分钟都在提醒线以上才提醒，中途跌回去就重新计时。默认 5")
S("resource_disk_gb", "硬盘提醒线（系统盘剩余多少 GB 以下）", "float", 5.0, "系统", (G,), adv=True,
  help="PodMates 数据所在的盘，剩余空间低于这个数就提醒；硬盘每小时查一次。默认 5")
# ---- 评论区类平台（B站、贴吧、微博、Reddit、YouTube、X）：节奏与额度，评论、回复、私信分开 ----
SOCIAL = ["bilibili", "tieba", "weibo", "reddit", "youtube", "x"]
S("poll_seconds", "多久查一次新通知（秒）", "int", 60, "平台", (G, P), platforms=SOCIAL,
  pdef={"tieba": 90, "weibo": 90, "youtube": 120, "x": 90}, help="没有公网地址，靠定时去拉；太勤容易被限速")
for _k, _label, _wait, _quota, _q2 in [("comment", "评论", "15-45", 20, 20), ("reply", "回复", "15-45", 30, 30), ("dm", "私信", "20-60", 10, 10)]:
    S(f"pace_{_k}", f"{_label}：两次之间等多久（秒，最短-最长）", "str", _wait, "平台", (G, P), platforms=SOCIAL, adv=True,
      help="每次发之前随机等一个时间，看起来不像机器")
    S(f"quota_{_k}", f"{_label}：每天最多发几条", "int", _quota, "平台", (G, P), platforms=SOCIAL,
      pdef={"youtube": 40} if _k != "dm" else {})
S("ui_lang", "界面语言 / Language", "select", "zh", "系统", (G,), options=[["zh", "中文"], ["en", "English"]],
  help="面板的文字。选 English 后刷新页面生效")
S("update_check", "有新版本时提醒我", "bool", True, "系统", (G,), help="每天查一次 GitHub Releases；只提醒，不自动更新")
S("update_repo", "版本发布的仓库（owner/repo）", "str", "", "系统", (G,), adv=True, help="留空就用程序自带的默认仓库")
S("model_check_hours", "多久查一次新模型（小时，0 = 不查）", "int", 24, "系统", (G,),
  help="到点问一遍你配过的各家服务商现在有哪些模型（不经过搜索服务、不花 token），和上次比；你在用的模型出了同系列的新版本时提醒你，不会自动换")
S("model_snooze_days", "回「不换」后多久再提醒（天）", "int", 7, "系统", (G,),
  help="私聊回「不换」或在面板点「暂不换」后，过这么多天再提醒同一个新模型；回「不换 30 天」这样带天数的按你说的算；回「一直不换」就不再提")
S("backup_keep_days", "自动备份保留天数", "int", 14, "系统", (G,))
S("log_keep_days", "日志保留天数", "int", 7, "系统", (G,))
S("message_keep_days", "聊天记录保留天数（0 为永久）", "int", 30, "系统", (G,), adv=True)
S("panel_lan", "允许局域网访问面板", "bool", False, "系统", (G,), help="手机和电脑在同一个 Wi-Fi 时用；改后重启程序生效")
S("panel_port", "面板端口", "int", 18765, "系统", (G,), adv=True, help="改后重启程序生效")
S("panel_public", "也允许公网访问面板", "bool", False, "系统", (G,), when={"panel_lan": True},
  help="默认关。打开「允许局域网访问」后，面板只接受本机、局域网（10.x、172.16～31.x、192.168.x）和 Tailscale（100.64～127.x）这类地址来的访问，别的一律拒绝。"
       "打开这一项就是对整个互联网开放面板：走的是明文 http，密码和聊天内容都可能被人看到，扫描的人也会来试密码，有风险，不建议。"
       "要在外面用手机，建议改用 Tailscale 这类组网工具。改完立即生效"),

BY_KEY = {s["key"]: s for s in SCHEMA}


def scope_type(scope: str) -> str:
    if scope == "global":
        return G
    if scope.startswith("acct:"):
        return AC
    if "|" in scope:
        return RC if "|chat:" in scope else RP
    return {"platform": P, "role": R, "chat": C}[scope.split(":")[0]]


def chain(role: str | None = None, platform: str | None = None, chat: str | None = None, acct: str | None = None) -> list[str]:
    """chat 写成 '<平台>:<群号>'。返回从最具体到最一般的作用域列表。"""
    out = []
    if acct and chat:
        out.append(f"acct:{acct}|chat:{chat}")
    if role and chat:
        out.append(f"role:{role}|chat:{chat}")
    if chat:
        out.append(f"chat:{chat}")
    if role and platform:
        out.append(f"role:{role}|platform:{platform}")
    if role:
        out.append(f"role:{role}")
    if platform:
        out.append(f"platform:{platform}")
    out.append("global")
    return out


class Settings:
    def __init__(self, db):
        self.db = db
        self.data: dict[str, dict] = {}
        for r in db.rows("SELECT scope, key, value FROM settings"):
            self.data.setdefault(r["scope"], {})[r["key"]] = json.loads(r["value"])

    def default(self, key: str, platform: str | None = None):
        s = BY_KEY[key]
        return s["pdef"].get(platform, s["default"])

    def get(self, key: str, role=None, platform=None, chat=None, acct=None):
        for sc in chain(role, platform, chat, acct):
            d = self.data.get(sc)
            if d and key in d:
                return d[key]
        return self.default(key, platform)

    def g(self, key: str):
        return self.get(key)

    def source(self, key: str, scope: str) -> tuple[object, str | None]:
        """从 scope 往上找，返回（生效值，来自哪个作用域；None 表示内置默认）。"""
        role, platform, chat = parse_scope(scope)
        for sc in chain(role, platform, chat, acct_of(scope)):
            d = self.data.get(sc)
            if d and key in d:
                return d[key], sc
        return self.default(key, platform), None

    def set(self, scope: str, key: str, value):
        if key not in BY_KEY:
            raise KeyError(key)
        self.data.setdefault(scope, {})[key] = value
        self.db.x("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (scope, key, json.dumps(value, ensure_ascii=False)))

    def reset(self, scope: str, key: str):
        self.data.get(scope, {}).pop(key, None)
        self.db.x("DELETE FROM settings WHERE scope=? AND key=?", (scope, key))

    def drop_scope(self, prefix: str):
        for sc in [s for s in self.data if s == prefix or s.startswith(prefix + "|")]:
            del self.data[sc]
        self.db.x("DELETE FROM settings WHERE scope=? OR scope LIKE ?", (prefix, prefix + "|%"))

    def view(self, scope: str) -> list[dict]:
        st = scope_type(scope)
        _, platform, _ = parse_scope(scope)
        out = []
        for s in SCHEMA:
            if st not in s["scopes"]:
                continue
            if s["platforms"] and platform and platform not in s["platforms"]:
                continue
            value, src = self.source(s["key"], scope)
            item = {"key": s["key"], "value": value, "own": src == scope, "from": src}
            if s["key"].endswith("_effort") and s["type"] == "select":  # 思考强度：只列这个模型支持的档位，已存的不支持的值显示成换算后的
                from . import providers
                pre = s["key"][:-7]
                ep = self.db.doc("endpoint", self.source(f"{pre}_endpoint", scope)[0]) or self.db.doc("endpoint", "default") or {}
                model = self.source(f"{pre}_model", scope)[0]
                fitted = providers.fit_effort(ep, model, value)
                opts = providers.effort_options(ep, model, fitted)
                if opts:
                    item.update(value=fitted, options=opts)
            out.append(item)
        return out


def acct_of(scope: str) -> str | None:
    """'acct:<账号id>|chat:...' 里的账号 id；不是这种作用域返回 None。"""
    return next((p[5:] for p in scope.split("|") if p.startswith("acct:")), None)


def parse_scope(scope: str):
    role = platform = chat = None
    for part in scope.split("|"):
        if part.startswith("role:"):
            role = part[5:]
        elif part.startswith("platform:"):
            platform = part[9:]
        elif part.startswith("chat:"):
            chat = part[5:]
            platform = chat.split(":")[0]
    return role, platform, chat
