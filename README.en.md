# Group Chat Bot Panel · PodMates

Author: [本昵称不代表本人](https://space.bilibili.com/10228715) (Bilibili)　·　Repository: <https://github.com/DChina2000/PodMates>

English · [中文](README.md)

A Windows program that manages all your group chat bots: **QQ, WeChat alt accounts, Discord, Telegram, LINE, YouTube, X** (via official APIs or common practices), plus **unofficial channels** (Bilibili, Baidu Tieba, Weibo), which are disabled by default.
Any number of bot roles, each with its own persona, memory, and attitude, managed from one control panel: a window on your computer, and the same panel can be opened in a browser on your phone.

- **Beginner-friendly**: installer + desktop icon + setup wizard. Pick a provider, enter your API key, choose a role template, and you're ready to go. There are many options, but each one has a default value. Common options are up front, and less-used ones are tucked into "Advanced settings".
- **Roles**: dozens of role templates (entertainment and companionship, academic explanation, lifestyle and health, practical tools, game hosting, original characters). Personas are assembled from modules that you can drag to reorder, toggle on or off, and review version history for. The same account can automatically switch roles by chat, time slot, or date (duty schedule).
- **Long-term memory**: notes about people, chat recaps, group memes, plus "semantic recall" using a local embedding model. Everything lives in a single SQLite file, with no database server to run. Can read PDF, Word, Excel and other files and store them in memory.
- **Models**: a built-in catalog of dozens of providers, supporting three protocols: OpenAI-compatible, Anthropic, and Gemini. Defaults to DeepSeek's `deepseek-flash`. API keys are stored encrypted with your Windows account, or you can use environment variables instead.
- **Chat etiquette**: learns each chat's speaking style, web search, “remember…” and reminders, sticker learning (all optional); share-of-conversation cap, auto-adjusted chime-in level, “stop talking” pause, and a hard daily usage cap.
- **Peace of mind**: shadow mode (only records what it "would have said" without actually sending), hourly/daily quotas, immediate stop when a platform's risk control kicks in, no automatic updates, and API keys never written to logs.
- **Lightweight**: about 45 MB of memory and near-zero CPU when idle (about 96 MB with the local embedding model and 3 roles chatting continuously).
- **Demo mode**: connects to no accounts; try it out first with simulated chats.
- The interface can be switched between Chinese and English.

**Opening the panel**: after installation, double-click the “Group Chat Bot Panel” desktop icon (or open it from the Start menu); on a phone, open the panel address in a browser — see “Using the panel on a phone” in the [User guide](docs/User-Guide.en.md).

## Installation

Download `PodMates-Setup-x.y.z.exe` from [Releases](../../releases) and double-click to install. (There is no code-signing certificate, so Windows will show "Windows protected your PC" → More info → Run anyway.)
For detailed steps and how to set up each platform, see the [User guide](docs/User-Guide.en.md).

## Important Notes

- **NapCat (QQ) is not bundled**: its license does not clearly permit redistribution and prohibits commercial use, so it is downloaded from the official GitHub on first use. **wxauto4 (WeChat) is not bundled**: the upstream project does not declare a license, so it is downloaded from PyPI at runtime. See [THIRD_PARTY.md](THIRD_PARTY.md) for details.
- **Unofficial channels carry risk**: Bilibili, Baidu Tieba, and Weibo do not offer open APIs for these features. They rely on web login sessions, which may violate the platforms' user agreements, and accounts may be restricted or banned. They are off by default. Please use a secondary account, and run in shadow mode as a dry run first.
- **WhatsApp is not supported**: the official Cloud API cannot join existing WhatsApp groups. The Reddit adapter is experimental: Reddit has announced it will shut down its remaining public API in March 2027.
- Bots are AI. Please follow each platform's rules and local laws, and do not use them to harass anyone.

## Building from Source

See [BUILDING.en.md](BUILDING.en.md). For development and testing:

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests -q
```

## License

[MIT](LICENSE). For third-party components, see [THIRD_PARTY.md](THIRD_PARTY.md).
