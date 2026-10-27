# 平台 → (模块, 类名)。只在用到时才导入对应的库（不开 Discord 就不加载 discord.py，省内存）
CHANNELS = {
    "qq": ("onebot", "QQChannel"),
    "discord": ("discord_ch", "DiscordChannel"),
    "wechat": ("wechat", "WeChatChannel"),
    "telegram": ("telegram", "TelegramChannel"),
    "line": ("line", "LineChannel"),
    "reddit": ("reddit", "RedditChannel"),
    "youtube": ("youtube", "YouTubeChannel"),
    "x": ("x_ch", "XChannel"),
    "bilibili": ("bilibili", "BilibiliChannel"),
    "tieba": ("tieba", "TiebaChannel"),
    "weibo": ("weibo", "WeiboChannel"),
    "demo": ("demo", "DemoChannel"),
}


def make_channel(engine, account: dict):
    p = account["platform"]
    if p not in CHANNELS:
        raise ValueError(f"未知平台 {p}")
    import importlib
    mod, cls = CHANNELS[p]
    return getattr(importlib.import_module(f".{mod}", __package__), cls)(engine, account)
