# SCP-CN Feed for AstrBot

> ⚠️ **本插件由 AI 生成，使用前请自行审查代码与运行效果，重要场景请先小范围测试。**

把 SCP 基金会中文分部首页的精品和竞赛活动整理成日报，首页一有新内容就推到订阅的群聊 / 私聊里。

![日报预览](docs/images/daily-report-preview.png)

- 首页 5 个区块随便勾：精品原创 SCP、精品原创故事、精品原创图书馆、主题精品、竞赛与活动
- 三种形式：SCP 档案风图片卡片、QQ 官方 Markdown + 按钮、纯文字，日报和推送分开选
- QQ 官方机器人直接走开放平台接口推送，AstrBot 重启后也照样能推

## 安装

在 AstrBot WebUI 的插件市场安装，或者把整个文件夹放进 `AstrBot/data/plugins/astrbot_plugin_scp_cn_feed` 后重载插件。

图片卡片要用浏览器渲染，第一次用记得装一下 Chromium：

```bash
playwright install chromium
```

装不了的话在配置里填个 Edge / Chrome 的路径也行；实在渲染不出来会自动改发文字。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/scp` 或 `/scp 日报` | 看首页当前的精品和竞赛 |
| `/scp 订阅` | 本会话接收新内容推送 |
| `/scp 退订` | 取消推送 |
| `/scp 状态` | 查看订阅状态和会话 ID |
| `/scp 帮助` | 显示上面这张表 |

日报和推送各自展示哪些区块、用什么形式，全在配置里改，不用记参数。

## 配置

| 配置项 | 说明 |
| --- | --- |
| 日报形式 `report_mode` | `image` 图片卡片 / `markdown` QQ 官方 Markdown + 按钮 / `text` 纯文字；选 Markdown 时指令回复也带按钮 |
| 日报展示区块 `report_sections` | 日报里展示哪些区块，全部不勾等于全选 |
| 推送形式 `push_mode` | 有新内容时推送用的形式，选项同上 |
| 推送展示区块 `push_sections` | 推送里展示哪些区块，全部不勾等于全选 |
| 订阅会话 `subscribed_sessions` | `/scp 订阅` 会自动写进来，也可以手动填会话 ID |
| Markdown 附带链接 `markdown_links` | 标题做成原文超链接，并加一个跳中文站首页的「SCP基金会」按钮，默认开 |
| 检查间隔 `poll_interval_days` | 多久抓一次首页，最少 1 天 |
| 浏览器路径 `playwright_browser_path` | 可选，渲染图片卡片用的浏览器 |

## QQ 官方机器人 Markdown 模式

把「日报形式」或「推送形式」选成 `QQ 官方 Markdown + 按钮` 后，qq_official / qq_official_webhook 的群聊和单聊会收到这样的消息（推送时标题换成「🚨 SCP-CN 新内容通报」）：

```markdown
# 🗂️ SCP-CN 日报
> 2026-09-30 ｜ 安保 · 收容 · 保护

***

## ☣️ 精品原创 SCP
**[SCP-CN-4003：命名混沌](https://scp-wiki-cn.wikidot.com/scp-cn-4003)**
> *作者* *H-Storm Z*

*“置身妖野，我沐浴丁香。”*

## 🏆 竞赛与活动
**[2026夏季征文](https://scp-wiki-cn.wikidot.com/summer-contest-2026)**

*幕布缓缓滑落，幕前的光怪陆离和幕后的幽暗深邃都暂告一段落……*

***
📡 数据来源：SCP 基金会中文分部首页
```

排版来自 `templates/report.md`（日报）和 `templates/push.md`（推送），直接改文件就行，文件顶部写了能用的占位符，插件每次发送都会重新读，不用重载。

点标题就能跳原文。消息下面挂两行按钮：

```text
[               SCP基金会               ]  ← 跳转中文站首页
[🗞️ 日报] [🔔 订阅] [🔕 退订] [📡 状态]   ← 指令按钮
```

- 指令按钮在单聊里点一下直接发送；群聊里会把 `@机器人 /scp 日报` 填进输入框，再点发送就行。
- `/scp 订阅`、`/scp 状态` 这些回复在 Markdown 模式下也会带上这两行按钮。
- 其他平台选了 Markdown 会自动改发图片卡片。
- 2026-04-23 起 QQ 群聊、单聊的自定义 Markdown 已经对所有机器人开放，不用再申请模板。
- 如果日志里出现「Markdown 推送失败」并提到链接 / URL，把「Markdown 附带链接」关掉，就只保留指令按钮。

### 以前官 bot 为啥不推送

AstrBot 的 qq_official 适配器只在内存里记住「这个会话是群」和「最近一条消息的 msg_id」。AstrBot 一重启，或者群里很久没人 @ 机器人，定时推送就会被静默跳过（日志里是 `No cached msg_id for session ... skip send_by_session`），旧版插件还以为发出去了，直接把这条更新标成已读。

现在插件遇到 QQ 官方会话会直接调用开放平台的发消息接口，不依赖那份缓存；发送失败也不会标记已读，下次检查会重试。QQ 群主动消息每个群每天上限 1000 条，用户在客户端关了「允许主动消息」的话会发不出去。

## 推送规则

- 每次检查只请求一次中文站首页；`/scp 日报` 会复用 10 分钟内的结果，定时检查一定会重新抓。
- 订阅时会把当前内容记为已读。首页这几个区块一般一起更新，所以勾选的推送区块里只要有一个换了内容就推送一次，消息里列出全部勾选区块，换了的标 NEW / 🆕。
- 没勾选的区块换了内容不会触发推送，但也会记下来，之后再勾上不会被当成新内容。
- 同一天同样内容的图片卡片只渲染一次，多个群推送时直接复用。
- 已读记录保存在 `data/plugin_data/astrbot_plugin_scp_cn_feed/state.json`。

## 从 0.4.x 升级

- 旧的订阅会话会在第一次启动时自动搬进「订阅会话」，已读记录继续沿用，不会刷屏。
- `/scp 取消订阅` 改成 `/scp 退订`；`检查`、`截图`、`基线`、`来源` 指令删掉了。
- 「新增模块截图」推送形式、黑白名单、渲染超时、图片保留时间这些配置删掉了。需要限制某些会话用插件的话，用 AstrBot 自带的会话管理关掉本插件就行。

更新日志见 [CHANGELOG.md](CHANGELOG.md)。

插件 logo 是 SCP 基金会中文分部徽标，图片卡片里的是 SCP 基金会徽标，都按 [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) 使用。
