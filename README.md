# 群聊机器人面板 · PodMates

作者：[本昵称不代表本人](https://space.bilibili.com/10228715)　·　仓库：<https://github.com/DChina2000/PodMates>

[English](README.en.md) · 中文

一个 Windows 程序，管所有群聊机器人：**QQ、微信小号、Discord、Telegram、LINE、YouTube、X**（官方接口或常见做法），以及默认关闭的**非官方渠道**（B站、贴吧、微博）。
任意多个机器人角色，各有人设、记忆、态度；一个控制面板——电脑上是窗口，手机用浏览器打开同一个。

- **傻瓜式**：安装包 + 桌面图标 + 设置向导；选一家服务商、填密钥、选个角色模板就能用。选项很多，但每项都有默认值，常用的放外面，少用的收进「高级设置」。
- **角色**：几十个角色模板（娱乐陪伴、学术讲解、生活健康、实用工具、游戏主持、原创角色）；人设由模块拼成，可拖动排序、开关、看历史版本；同一个号可按群、时段、日期自动换角色（排班）。
- **长期记忆**：人物备忘、群前情、群梗，加本机向量模型的「语义回忆」（同一个 SQLite 文件里，不跑数据库服务）；可读 PDF/Word/Excel 等文件存进记忆。
- **模型**：内置几十家服务商目录，支持 OpenAI 兼容 / Anthropic / Gemini 三种协议，默认 DeepSeek 的 `deepseek-flash`；密钥用 Windows 账户加密保存，也可以改用环境变量。
- **群里的分寸**：学群里的说话习惯、联网搜索、「记住……」和提醒、学表情包（都是可选项）；发言占比上限、插话力度自动调节、「别说了」暂停、每日用量硬上限。
- **安全感**：影子模式（只记「本来会说什么」不真发）、每小时/每日额度、遇平台风控立即停止、不自动更新、密钥不进日志。
- **不吃资源**：空闲时内存约 45 MB、CPU 接近 0（含本机向量模型 + 3 个角色持续聊天约 96 MB）。
- **体验模式**：不连任何账号，用模拟群先试试。
- 界面中文/英文可切换。

**打开界面**：安装后双击桌面的「群聊机器人面板」图标（或从开始菜单打开）；手机用浏览器打开面板地址，见[使用说明](docs/使用说明.md)的「手机上用」。

## 安装

到 [Releases](../../releases) 下载 `PodMates-Setup-x.y.z.exe`，双击安装（没有代码签名证书，Windows 会提示「已保护你的电脑」→ 更多信息 → 仍要运行）。
详细步骤和各平台怎么开通：[docs/使用说明.md](docs/使用说明.md)（English: [docs/User-Guide.en.md](docs/User-Guide.en.md)）。

## 重要说明

- **NapCat（QQ）不随包分发**：它的许可证不明确允许、且禁止商用，首次使用时从官方 GitHub 下载。**wxauto4（微信）不随包分发**：上游没有声明许可证，运行时才从 PyPI 下载。详见 [THIRD_PARTY.md](THIRD_PARTY.md)。
- **非官方渠道有风险**：B站、贴吧、微博没有开放这些接口，用的是网页登录态，可能违反平台用户协议，账号可能被限制或封禁。默认关闭，请用小号、先开影子模式演练。
- **WhatsApp 没有做**：官方 Cloud API 不能加入已有的 WhatsApp 群。Reddit 适配器是实验性的：Reddit 已宣布 2027 年 3 月关闭剩余公共 API。
- 机器人是 AI，请遵守各平台规则和当地法律；不要拿它骚扰别人。

## 从源码构建

见 [BUILDING.md](BUILDING.md)。开发测试：

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests -q
```

## 许可证

[MIT](LICENSE)，Copyright (c) 2026 本昵称不代表本人（[B 站主页](https://space.bilibili.com/10228715)）。第三方组件见 [THIRD_PARTY.md](THIRD_PARTY.md)。
