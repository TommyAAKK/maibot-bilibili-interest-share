# 麦麦刷B站插件（MaiBot）

定时「刷」B 站，根据白名单聊天流里大家的兴趣，挑一些**真的有趣**的视频分享到群里。

插件会读视频的 tags、CC 字幕、弹幕和热门评论，必要时**下载视频抽几帧看画面**，再让 LLM 判断「这视频到底讲了什么、值不值得分享」；分享文案是一段**符合人设口吻的自然语言介绍**，像在群里随口安利那样分条发出来。

> 当前版本 **v1.0.3**。每个版本的详细变更记录见 [CHANGELOG.md](CHANGELOG.md)。

---

## 功能一览

| 能力 | 说明 |
| --- | --- |
| 可调频率 | `[schedule].interval_minutes` 支持 **1 分钟 ~ 24 小时**，另有随机抖动、活跃时段 |
| 白名单 | 只有 `[[streams]]` 里列出的聊天流会收到分享；每个流可单独配兴趣、类别、数量 |
| 兴趣来源 | ① 配置里手写兴趣词 ② 自动从该流近期聊天里提取高频词 ③ 视频 tags/标题/简介/分区/UP 名 |
| 多来源抓取 | 关键词搜索（WBI 签名，无需登录）、热门列表、排行榜，全部可在配置里开关 |
| 内容类别 | 新鲜资讯 / 社区创作 / 玩家投稿 / 外网搬运，类别和关键词都能自己改（`[[categories]]`） |
| 深度理解 | CC 字幕 + 弹幕 + 热门评论 + **抽帧识图** + LLM 语义判断与自然语言介绍 |
| 噪声过滤 | 硬过滤（时长/播放/点赞/屏蔽词/官方内容）+ AI 鱼灾式模板弹幕评论过滤 |
| 去重 | 分享过的视频记入名单，默认 30 天内不再自动分享 |
| 主动搜索 | `/bili-search 关键词`，独立冷却（默认 5 分钟），不影响自动频率 |
| 自然语言重发 | 聊天里说「你之前发的那个 XX 再发一遍」，插件从历史记录里找回并**复用当时的介绍文案**重发 |
| 赤石视频 | `/赤石视频` 抓一个抽象整活类视频，**只发标题+链接，不带介绍** |
| 人设语气 | 推荐语和介绍文案按 MaiBot 的**麦麦人设**口吻生成（通过 `config.get` 能力读宿主配置），可覆盖、可关 |
| 可观测 | `/bili-status` 看状态、`/bili-test` 预览本轮打分（只预览，不分享视频） |

---

## 目录结构

```
github.TommyAAKK.bilibili_interest_share/
├── _manifest.json        # 插件清单（宿主识别用）
├── CHANGELOG.md          # 版本变更记录（每个版本改了什么）
├── config.example.toml   # 带中文注释的配置参考模板（可复制成 config.toml）
├── config.py             # 配置模型（WebUI 表单由它生成）
├── plugin.py             # 插件主体：调度、抓取、打分、分享、指令、Hook
├── bilibili.py           # B 站接口客户端（WBI 签名、搜索、详情、弹幕、评论、字幕）
├── interest.py           # 兴趣关键词提取与打分
├── textfilter.py         # AI 鱼灾弹幕/评论过滤
├── media.py              # 取流下载 + ffmpeg 抽帧
├── llm_judge.py          # LLM 语义判断、自然语言介绍、推荐语
├── persona.py            # 通过宿主 config.get 能力读人设，提供人设提示词
├── store.py              # 去重名单、分享历史、冷却状态（state.json）
├── tools/
│   ├── live_check.py     # 真实联网自检（可选抽帧/弹幕/LLM/赤石）
│   ├── probe_persona.py  # 人设自检：打印读到的人设 + 注入后的 prompt（不调用模型）
│   └── probe_vision.py   # 单独验证「下载 + 抽帧」链路
└── tests/                # unittest 单元测试（128 个用例）
```

---

## 安装

1. 把整个目录复制到 MaiBot 的 `plugins/` 下：

   ```
   <MaiBot 实例>/plugins/github.TommyAAKK.bilibili_interest_share/
   ```

   > 目录名要和插件 ID 一致（宿主按 ID 导入插件）。从插件市场安装时会自动放到这个位置。

2. 重启 MaiBot，或在 WebUI 的插件页启用 `github.TommyAAKK.bilibili_interest_share`。

3. 首次启动后，MaiBot 会在插件目录下自动生成 `config.toml`。打开它，**至少填一个 `[[streams]]`**（白名单），否则不会分享给任何会话：

   ```toml
   [[streams]]
   name = "原神群"
   enabled = true
   group_id = "123456789"          # 群号；也可直接填 stream_id
   interests = ["原神", "抽卡", "机械键盘"]
   auto_interest = true
   ```

   找不到群号？先在群里发一句 `/bili-status`，日志会打印解析到的会话信息；也可以把 `stream_id` 直接填上（优先级最高）。

   > 仓库里的 `config.example.toml` 是同一份配置的**带中文注释版模板**：想直接看注释版，把它复制成 `config.toml` 即可（值完全一致）。自动生成的 `config.toml` 不含注释，但 WebUI 表单里每一项都有中文说明。

插件**不依赖额外 pip 包**：HTTP 用宿主自带的 `httpx`。

**抽帧识图需要 ffmpeg**（可选功能）：优先用 `[advanced].ffmpeg_path`，其次找 `imageio-ffmpeg`（MaiBot 环境里通常已装），最后查系统 `PATH`。三者都没有时会自动跳过抽帧，其余流程正常。如果希望开箱即用，请确认运行 MaiBot 的环境里 `PATH` 有 `ffmpeg`，或装了 `imageio-ffmpeg`。

---

## 配置说明

配置按 section 分组，WebUI 会自动渲染成表单。下面**逐项说明全部配置项**（`config.example.toml` 里每个键上方也有一行中文注释，两处含义一致）。

> 改完 `config.toml` 保存即可，MaiBot 会自动重载插件配置，不用重启。

### `[plugin]` 插件开关

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用插件（`false` 时完全不工作） |
| `config_version` | `"1.0.3"` | 配置版本号，由插件维护，一般不要手动改 |

### `[schedule]` 频率

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `interval_minutes` | `60` | 每隔多少分钟刷一次，**1 ~ 1440**（1 分钟 ~ 24 小时） |
| `jitter_seconds` | `120` | 每轮叠加的随机抖动，避免固定时刻 |
| `run_on_start` | `true` | 加载后先刷一轮 |
| `active_hours` | `""` | 允许自动分享的时段，如 `08:00-23:30`，支持跨午夜；留空=全天 |
| `max_share_per_round` | `1` | 每个流每轮最多分享几个 |
| `search_cooldown_seconds` | `300` | **主动搜索**的独立冷却，与自动频率无关 |

### `[sources]` 来源

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `search_enabled` | `true` | 按关键词搜索（推荐，命中率最高） |
| `search_pages` | `1` | 每个关键词搜几页 |
| `search_keywords_per_round` | `3` | 每轮最多用几个兴趣词搜索 |
| `search_order` | `""` | `""` 综合 / `pubdate` 最新 / `click` 播放 / `dm` 弹幕 |
| `search_delay_seconds` | `1.2` | 关键词间隔，避免请求过密 |
| `popular_enabled` | `false` | 热门列表（以官方/大 UP 为主，默认关掉以避开主流官方内容） |
| `popular_page_size` | `20` | 抓热门时每页多少条（一般不用改） |
| `popular_pages` | `1` | 抓热门时抓几页（页数越多候选越多，也越慢） |
| `ranking_enabled` | `false` | 排行榜（风控严，未配 cookie 常失败） |
| `ranking_rids` | `[0]` | 排行榜分区号，`[0]` = 全站；其它分区号见 B 站排行榜接口 |

### `[[categories]]` 主动分享的内容类别

默认四类，可增删改关键词。`config.example.toml` 里自带的是下面这四类：

```toml
[[categories]]
name = "新鲜资讯"
enabled = true
keywords = ["科技资讯", "游戏资讯", "新游情报", "行业动态"]
search_order = "pubdate"        # 资讯类按最新排
max_share_per_round = 0
min_play = 1000
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `name` | — | 类别名，`[[streams]].categories` 引用这个名字 |
| `enabled` | `true` | 是否启用这个类别 |
| `keywords` | — | 这个类别用来搜索的关键词 |
| `search_order` | `""` | 该类别搜索排序；留空用 `[sources].search_order`，`pubdate` = 按最新发布 |
| `max_share_per_round` | `0` | 该类别每轮最多几个，`0` = 不单独限制 |
| `min_play` | `0` | 该类别的最低播放量，`0` = 沿用 `[filter].min_play` |

- 聊天流可用 `[[streams]].categories` 指定只启用其中几类

### `[interest]` 兴趣与语义判断

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `use_recent_chat` | `true` | 从近期聊天自动提取兴趣 |
| `lookback_hours` | `48` | 回看多少小时 |
| `max_messages_per_stream` | `200` | 每个流最多读多少条消息 |
| `top_keywords` / `keyword_min_count` | `12` / `2` | 取前 N 个、至少出现几次 |
| `explicit_weight` / `auto_weight` | `3.0` / `1.0` | 手写兴趣 vs 自动提取的权重 |
| `use_tags` / `tag_weight` | `true` / `0.7` | 视频 tags 参与打分 |
| `use_subtitles` | `true` | 读 CC 字幕（没有就跳过） |
| `use_llm_judge` | `true` | LLM 语义判断 + 生成自然语言介绍 |
| `llm_task_name` | `planner` | 文本 LLM 用的模型任务名 |
| `llm_max_candidates` | `5` | 每轮最多让 LLM 判断几个候选（控成本） |
| `llm_temperature` | `0.3` | LLM 温度，越低越稳定保守 |
| `extra_stopwords` | `[]` | 额外停用词，从聊天提兴趣时忽略的词，如 `["哈哈", "什么"]` |

> 中文兴趣提取用的是 2~4 字词组切分 + 停用词过滤 + 长词剪枝，不需要分词库。

### `[vision]` 抽帧识图

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否抽帧看画面（只对**最终入选**的视频做） |
| `task_name` | `vlm` | 识图模型任务名，**必须支持视觉**（planner 模型通常不支持图片） |
| `frame_count` | `4` | 抽几帧 |
| `max_width` | `640` | 帧宽（越小越省 token） |
| `max_download_mb` | `80` | 下载体积上限 |
| `timeout_seconds` | `90` | 下载+抽帧总超时 |

### `[danmaku]` 弹幕与热门评论

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `danmaku_enabled` | `true` | 是否抓弹幕 |
| `danmaku_fetch_limit` | `400` | 最多抓多少条原始弹幕 |
| `danmaku_limit` | `40` | 过滤后最多送多少条弹幕给 LLM |
| `comments_enabled` | `true` | 是否抓热门评论 |
| `comments_limit` | `10` | 最多送多少条热门评论给 LLM |
| `filter_ai_spam` | `true` | 过滤 AI 鱼灾类内容（模板式「感谢分享，学到了」、AI 生成声明、纯符号刷屏等） |

### `[filter]` 硬过滤

不符合的视频直接丢掉，不进打分。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `min_duration_seconds` | `60` | 视频最短时长（秒），太短的跳过 |
| `max_duration_seconds` | `1800` | 视频最长时长（秒），太长的跳过 |
| `min_play` | `5000` | 最低播放量 |
| `min_like` | `100` | 最低点赞数 |
| `max_age_days` | `30` | 只分享最近多少天内发布的，`0` = 不限 |
| `block_keywords` | 见配置 | 标题/简介命中即跳过（广告、带货、抽奖、互粉、三连、付费课程） |
| `block_up_names` | `[]` | 这些 UP 主的视频不分享 |
| `exclude_partitions` | `[]` | 这些分区的视频不分享，如 `["资讯", "影视"]` |
| `exclude_official` | `true` | 排除主流官方内容 |
| `official_keywords` | 见配置 | 命中标题/简介即排除：官方、预告、PV、正片、发布会、前瞻、速览、兑换码…… |

### `[scoring]` 打分

```
总分 = 兴趣分 × interest_weight + 热度分 × hot_weight + 新鲜度分 × freshness_weight
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `interest_weight` | `1.0` | 兴趣命中分的权重 |
| `title_match_boost` | `2.0` | 标题命中兴趣词时的额外加成倍数 |
| `hot_weight` | `1.0` | 播放/点赞热度的权重 |
| `freshness_weight` | `0.6` | 新鲜度的权重 |
| `freshness_days` | `7` | 新鲜度按多少天内线性衰减 |
| `min_score` | `4.0` | 入选最低分（纯规则分；开启 LLM 判断后还要通过 LLM） |
| `require_interest_match` | `true` | 必须命中至少一个兴趣词（关掉更容易分享但容易跑题） |

兴趣命中按字段给不同权重：标题 `title_match_boost`（默认 2.0）、简介 0.8、分区 0.6、UP 名 0.4、tags `tag_weight`。

### `[share]` 分享格式

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `send_as_parts` | `true` | 分条发送：⓪推荐语 ①【标题】链接 ②自然语言介绍 |
| `part_delay_seconds` | `0.8` | 分条间隔 |
| `intro_enabled` | `true` | 是否先用**人设口吻**发一句推荐语（见 `[persona]`） |
| `intro_max_chars` | `40` | 推荐语字数上限 |
| `narrative_enabled` | `true` | 是否发一段人设口吻的**自然语言介绍**（内容 + 看点揉在一起，不用「内容：」「看点：」格式） |
| `narrative_max_chars` | `160` | 介绍文案字数上限 |
| `include_stats_line` | `false` | 链接消息里是否附 UP/时长/播放 |

实际发送效果（`intro_enabled = true` 时）：

```
第 1 条：呜哇，这个必须拉你们一起看！   ← 人设口吻的推荐语（LLM 生成）
第 2 条：【标题】 https://www.bilibili.com/video/BVxxxx
第 3 条：这期把新版本的角色和玩法都聊透了，几个新配队思路挺有参考价值，
        看到一半我就想去试试了 (｡･ω･｡)   ← 人设口吻的自然语言介绍（LLM 生成）
```

介绍文案不是固定模板：模型会按人设口吻用一两三句话讲清「讲了什么 + 哪里有意思」，像在群里跟朋友随口安利，不分点、不加编号、不带「内容：」「看点：」这类前缀。没有 LLM 可用时退回视频简介原文。

### `[persona]` 人设语气

分享文案（推荐语 / 自然语言介绍）会按 MaiBot 主程序里配置的**麦麦人设**来写，而不是固定模板语气。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用人设语气 |
| `name` / `personality` / `reply_style` | `""` | 手动覆盖人设字段；留空则用宿主的，非空则覆盖对应字段 |
| `style_hint` | `""` | 追加给 LLM 的额外要求，如「多提一句视频看点」 |
| `cache_seconds` | `120` | 人设缓存时间（秒）；过期后下次分享前自动重新读一次 |

人设通过宿主提供的 `config.get` 能力读取，字段是：`bot.nickname`、`bot.alias_names`、`personality.personality`、`personality.reply_style`、`personality.behavior_style`（也就是实例 `config/bot_config.toml` 里 `[bot]` 和 `[personality]` 两节的内容）。读不到人设时自动退回中性语气，不影响分享。

> 人设只影响「怎么说」，不影响「分享什么」：筛选与打分仍由 `[interest]` / `[scoring]` 决定。

### `[reshare]` 自然语言重发

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否允许在聊天里用自然语言让机器人重发分享过的视频 |
| `trigger_keywords` | 见配置 | 触发词，消息里要出现其中之一（再发一遍/重发/再分享…） |
| `history_keywords` | 见配置 | 指代词，消息里还要出现其中之一（之前/上次/那个/你发过…） |
| `match_window_days` | `60` | 在多长时间内的分享记录里查找（天） |
| `min_match_score` | `0.25` | 标题模糊匹配的最低相似度（0~1，越低越宽松） |
| `reply_enabled` | `true` | 重发后是否回一句确认（如「好嘞，再发一遍～」） |

同时命中「触发词 + 指代词」才会触发，避免误判；重发时**直接复用当时存下的介绍文案和推荐语**，不会重新生成。

> 重发**只对白名单会话（`[[streams]]`）生效**：其它群/私聊里说「再发一遍」不会触发，避免插件变成任意群都能指挥的转发器。

### `[shiwei]` 赤石视频

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用 `/赤石视频` 指令 |
| `cooldown_seconds` | `600` | 该指令的冷却（秒） |
| `max_share` | `1` | 一次最多分享几个 |
| `keywords` | 见配置 | 用来搜索的关键词（抽象/整活/鬼畜/离谱/翻车/整蛊/土味/迷惑行为…） |
| `search_keywords_per_round` | `2` | 每次用几个关键词去搜 |
| `min_play` | `20000` | 最低播放量 |
| `min_duration_seconds` | `10` | 视频最短时长（秒） |
| `max_duration_seconds` | `900` | 视频最长时长（秒） |
| `block_keywords` | 见配置 | 命中即跳过（官方/预告/发布会/宣传片） |

`/赤石视频` 只发「【标题】链接」，**不带介绍文案**，也不影响普通分享的去重与冷却。

### `[advanced]` 运行参数

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `data_dir` | `""` | 状态文件目录；留空用插件默认数据目录 `data/plugins/<插件ID>` |
| `request_timeout_seconds` | `12.0` | 单次 B 站请求超时（秒） |
| `user_agent` | 见配置 | 请求用的 User-Agent |
| `cookie` | `""` | 可选，填入 B 站 cookie 可提高搜索成功率（**不要提交到公开仓库**） |
| `ffmpeg_path` | `""` | 可选，自定义 ffmpeg 路径；留空自动查找（imageio-ffmpeg / PATH） |
| `dedupe_days` | `30` | 同一视频多少天内不重复分享 |
| `max_seen_records` | `3000` | 最多保留多少条已分享记录（超过淘汰最旧的） |
| `debug` | `false` | 是否输出调试日志（排查问题时打开） |

### `[[streams]]` 白名单聊天流

| 键 | 说明 |
| --- | --- |
| `name` | 备注名 |
| `enabled` | 是否启用 |
| `stream_id` | 聊天流 ID（session_id），填了优先级最高 |
| `group_id` | 群号，未填 `stream_id` 时按群号反查 |
| `user_id` | 私聊用户 ID，前两者都空时按用户反查 |
| `interests` | 该流的手写兴趣词 |
| `categories` | 该流启用的类别名，留空=全部启用的类别 |
| `auto_interest` | 是否从该流近期聊天自动提取兴趣 |
| `max_share_per_round` | 该流每轮最多几个，`0`=用 `[schedule]` 的值 |

---

## 指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/bili-share` | `/b站分享` | 立即跑一轮（无视自动频率，仍受活跃时段限制） |
| `/bili-status` | — | 查看启用状态、频率、白名单数量、下一轮时间、记录统计 |
| `/bili-test` | — | 预览本轮候选与前 5 名打分，只把预览结果发回当前会话，**不分享视频** |
| `/bili-search 关键词` | `/b站搜索` | 主动搜索并分享，受 `search_cooldown_seconds` 冷却 |
| `/赤石视频` | — | 抓一个抽象整活视频，只发标题+链接 |

---

## 处理流程

```
定时触发（interval + 抖动）
  ├─ 检查：插件启用 / 在活跃时段 / 有白名单流
  ├─ 对每个白名单流：解析会话 → 构建兴趣画像（手写 + 聊天提取）
  ├─ 收集搜索关键词（兴趣词 + 启用的内容类别关键词）
  ├─ 抓候选：关键词搜索 / 热门 / 排行榜 → 合并去重
  ├─ 硬过滤：时长、播放、点赞、发布天数、屏蔽词、官方内容
  ├─ 去重：踢掉已分享过的 bvid
  ├─ 打分排序 → 取前 N（min_score / require_interest_match）
  ├─ 对候选做深度理解：
  │    ├─ 读 tags / CC 字幕
  │    ├─ 读弹幕（过滤 AI 鱼灾）+ 热门评论
  │    ├─ 下载视频抽 4 帧（vlm 任务识图）
  │    └─ 读人设 → LLM 判断是否贴合兴趣 → 用人设口吻生成推荐语 + 自然语言介绍
  ├─ 入选者按分条格式发送（推荐语 / 标题+链接 / 自然语言介绍）
  └─ 写入 state.json（去重名单 + 分享记录 + 冷却时间）
```

## 数据文件

状态存在 `<data_dir>/github.TommyAAKK.bilibili_interest_share/state.json`（`data_dir` 默认取宿主给插件的数据目录）：

```json
{
  "version": 2,
  "seen": { "BV1xx": { "title": "...", "shared_at": 1760000000, "kind": "auto" } },
  "runs": [ { "at": 1760000000, "fetched": 120, "shared": 1 } ],
  "cooldowns": { "search": 1760000000, "shiwei": 1760000000 }
}
```

- 名单里的视频**不会再被自动抓取/分享**，但仍可通过自然语言要求重发
- 每条分享记录都保存了当时的 `narrative`（介绍文案）和 `intro`（推荐语），重发时直接复用；旧版本留下的 `summary` / `points` 仍兼容读取
- 删除 `state.json` 等于「忘掉所有历史」，下次会重新分享

---

## 测试与自检

单元测试（128 个用例，不需要联网）：

```powershell
# 用 MaiBot 所在环境的 python
& "<MaiBot>\.onekey\environments\<env>\python.exe" -X utf8 -m unittest discover -s tests
```

覆盖：兴趣提取与打分、B 站客户端（httpx.MockTransport）、弹幕/评论解析与鱼灾过滤、抽帧帧选择、LLM 判断降级、**人设读取与提示词注入**、插件完整轮次、去重、冷却、重发（含白名单约束）、赤石、指令、**下载地址与 bvid 安全校验**、以及**用宿主真实 ManifestValidator 与 PluginLoader 校验打包**。

真实联网自检：

```powershell
# 基本一轮：搜索「机械键盘」「原神」，分享 2 个
python tools/live_check.py 机械键盘 原神 --limit 2

# 带弹幕/评论 + 分条格式
python tools/live_check.py 原神 --limit 2 --danmaku --parts

# 走完整 LLM 判断（需要宿主环境能调 planner）
python tools/live_check.py 原神 --limit 1 --llm

# 验证 /赤石视频
python tools/live_check.py --shiwei --limit 2 --parts

# 单独验证下载 + 抽帧（不调 LLM）
python tools/probe_vision.py --save ..\bili-frames

# 人设自检：打印读到的人设和注入后的 prompt（不调模型）
python tools/probe_persona.py --data-dir "<MaiBot>\data\plugins\github.TommyAAKK.bilibili_interest_share"
```

> `probe_persona.py` 是开发自检脚本，直接解析实例里的 `config/bot_config.toml` 来模拟宿主的 `config.get` 返回值；插件运行时并不直接读文件。

---

## 已知限制

- **只用公开接口**，不需要登录；但 B 站有风控，请求过密可能返回 `-352`，插件会自动重试一次（重新取 WBI 密钥）后放弃本轮。
- **排行榜接口**风控较严，未配 cookie 时基本拿不到数据，所以默认关闭。
- **抽帧依赖 ffmpeg**：优先用 `[advanced].ffmpeg_path`，其次 `imageio-ffmpeg`，最后查 `PATH`；都没有时自动跳过识图，其余流程正常。
- **识图需要支持视觉的模型**：默认 `vlm` 任务；如果你的 `model_config.toml` 里没有这个任务，请把 `[vision].task_name` 改成任意支持图片的模型任务名，或关掉 `[vision].enabled`。
- **LLM 判断有成本**：`llm_max_candidates` 控制每轮最多判断几个，默认 5。
- **字幕**不是每个视频都有，没有就只用标题/简介/弹幕/评论/画面。
- **人设依赖宿主配置**：通过宿主 `config.get` 能力读取 `bot.nickname` 等字段；读不到时退回中性语气，不影响分享。
- 分享的是**文本卡片 + 链接**，不会把视频文件发到群里。
- 请遵守 B 站服务条款，不要把频率调得过低（`interval_minutes` 建议 ≥ 10）。
