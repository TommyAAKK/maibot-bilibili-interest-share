# 更新日志

「麦麦刷B站」插件的全部版本变更记录。

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
`_manifest.json` 的 `version` 与 `config.py` 里的 `config_version` 始终一致。

## [1.0.3] - 2026-10-25

对齐文档与实现的两处小出入（插件市场审核指出）。

### 修复

- `/bili-share` 补齐 README 中已声明的别名 `/b站分享`（`/bili-search` 的 `/b站搜索` 一直有效）。

### 文档

- `/bili-test` 的描述改为「只预览，不分享视频」：它会把预览结果发回当前会话，但不会真的分享视频；原文「不发送」容易被理解成什么都不回。

### 测试

- 新增 `tests/test_docs.py::CommandDocTests`：README 指令表里出现的每条指令和别名，代码里都必须真的声明过，防止文档与实现再次漂移。

## [1.0.2] - 2026-10-25

补齐三个「文档里写了、代码却没读」的配置项，并修一个分类归属 bug。

### 修复

- `[sources].search_pages` 真正按页数翻页搜索（之前永远只取第 1 页）。
- `[[categories]].min_play` 真正生效（之前只有 `[filter].min_play` 起作用，类别级门槛被忽略）。
- `[reshare].reply_enabled` 真正生效（重发成功后回一句「好嘞，再发一遍～」，可关掉）。
- 修了「普通关键词搜索先把 bvid 记成无分类、导致后面按分类搜到的类别补不上」的问题，`[[categories]]` 的类别过滤与类别门槛才能正常匹配。

### 变更

- `HookMode` 改为从文档推荐的 `maibot_sdk.types` 导入，保留对旧版 SDK 的兼容回退。

## [1.0.1] - 2026-10-25

按插件市场审核意见整改（首次提交后的第一轮整改）。

### 变更

- **配置文件不再随仓库发布**：运行时的 `config.toml` 由 MaiBot 按插件 schema 自动生成，仓库改为提供带中文注释的参考模板 `config.example.toml`（值与默认值逐项一致）。
- **人设改走能力边界**：不再反推实例目录、直接读 `config/bot_config.toml`，改为通过宿主 `config.get` 能力读 `bot.nickname` / `personality.*`，并声明 `config.get` 能力。
- **下载地址做安全校验**：B 站返回的播放地址与字幕地址只放行 `http`/`https`，拒绝本机与内网地址，重定向每一跳都重新校验（防 SSRF）。
- **`bvid` 做路径校验**：只有匹配 `^BV[0-9A-Za-z]+$` 的才会被当作目录名，避免上游数据里出现 `../` 之类越界路径。
- **重发也受白名单约束**：`/再发一遍` 这类重发只对白名单会话生效。
- `_manifest.json` 补上 `plugin_type: "integration"`，`host_application.max_version` 收窄为 `1.3.99`。

### 移除

- 声明但未使用的 `chat.get_all_streams` 能力。
- 仓库里直接提交的 `config.toml`（改为 `.gitignore` + `config.example.toml`）。

### 测试

- 新增 `tests/test_safety.py`（下载地址安全、重定向逐跳校验、bvid 路径校验）。
- 重写 `tests/test_persona.py`（人设改走 `config.get`）与 `tests/test_docs.py`（改为校验 `config.example.toml` 与默认值一致）。

## [1.0.0] - 2026-10-25

首个公开发布版。

### 变更

- 插件正式定名「麦麦刷B站」。
- 插件 ID 改为市场规范格式 `github.TommyAAKK.bilibili_interest_share`（目录名与 ID 一致）。
- `_manifest.json` 的 `version` 与 `config.py` 的 `config_version` 统一为 `1.0.0`。
- 发布副本的配置全部还原为初始默认值（示例白名单默认关闭）。
- 发布到麦麦插件市场（Mai-with-u/plugin-repo）。

### 新增

- `LICENSE`（MIT）。
- `config.toml` 每个配置项的中文注释，以及 README 的逐项配置说明。

## 0.x 开发期（未公开发布）

### 0.4.2

- 每个配置项都补上中文注释：`config.toml` 里每个键上方一行紧邻的中文说明，README 逐项列出全部配置项（默认值 + 含义）。
- 新增文档一致性测试，防止以后加配置忘了写注释或忘了更新 README。

### 0.4.1

- 修 `message.get_recent` 的调用签名：真实 SDK 不接受 `hours` 参数，之前会让「近期聊天兴趣」静默为空；改为只传 `chat_id`/`limit`，再按消息 `timestamp` 本地过滤回看窗口。
- 修插件数据目录多套一层：`state.json` 曾落到 `data/plugins/<插件ID>/<插件ID>/` 下。

### 0.4.0

- 分享文案不再用「内容：」「看点：」这类正式格式，改为一段**符合人设口吻的自然语言介绍**（`narrative`），像在群里随口安利那样分条发出。
- 新增 `[share].narrative_enabled` / `narrative_max_chars`；旧版 `state.json` 里的 `summary` / `points` 仍兼容读取。

### 0.3.0

- 分享语气按 MaiBot 配置好的人设来：读取宿主的名字、别名、性格、说话风格、行为准则，生成推荐语（`intro`）与介绍文案。
- 新增 `[persona]` 配置节（可覆盖、可关闭、可追加风格提示）与 `tools/probe_persona.py` 人设自检工具。

### 0.2.0

- 不再只看标题和播放量：读视频 tags、CC 字幕、弹幕、热门评论，必要时下载视频抽几帧看画面，再让 LLM 判断「讲了什么、值不值得分享」。
- 新增 AI 鱼灾式弹幕/评论过滤、内容类别（`[[categories]]`）、多来源抓取（关键词搜索/热门/排行）、主动搜索指令 `/bili-search`、赤石视频指令 `/赤石视频`、自然语言重发（「你之前发的那个再发一遍」）。

### 0.1.0

- 初版：可调频率的定时刷 B 站、白名单聊天流、兴趣关键词（手写 + 自动从近期聊天提取）、关键词搜索、噪音过滤、打分挑选、去重名单、指令（`/bili-share`、`/bili-search`、`/bili-status`、`/bili-test`）。
