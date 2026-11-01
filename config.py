"""麦麦刷B站插件配置模型。

配置分为若干 section，MaiBot 的 WebUI 会按 section 渲染成表单。
每个字段都通过 ``json_schema_extra={"label": ...}`` 提供了中文名，
配置页上看到的都是中文（只有需要用户自己填的内容，比如关键词、Cookie，才是用户输入）。

- ``[plugin]``：插件总开关与配置版本（宿主保留字段，必须有）
- ``[schedule]``：刷 B 站的频率（1 分钟 ~ 24 小时可调）与主动搜索冷却
- ``[account]``：B 站登录（Cookie 可以手填，也可以用 ``/扫码登录`` 扫码自动保存）
- ``[sources]``：候选视频来源（关键词搜索 / 热门 / 排行榜）
- ``[categories]``：主动分享的内容类别（数组表 ``[[categories]]``，可自行增删）
- ``[interest]``：兴趣提取与语义判断（LLM / 弹幕 / 字幕 / tags）
- ``[vision]``：抽帧识图（用 vlm 任务模型）
- ``[danmaku]``：弹幕与热门评论读取、AI 鱼灾过滤
- ``[filter]``：视频硬过滤条件（时长、播放量、屏蔽词、官方内容排除等）
- ``[scoring]``：打分权重与入选阈值
- ``[persona]``：分享文案使用 MaiBot 配置的人设语气
- ``[share]``：分享格式（随口一提 / 分条发送）
- ``[reshare]``：自然语言「再发一遍」重发
- ``[shiwei]``：``/赤石视频`` 专用配置
- ``[advanced]``：数据目录、超时、去重等运行参数
- ``[[streams]]``：白名单聊天流（数组表），每个流可单独配置兴趣与分享上限
"""

from __future__ import annotations

from typing import List

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用麦麦刷B站",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="2.0.0",
        description="配置版本（由插件维护，请勿手动修改）",
        json_schema_extra={"label": "配置版本"},
    )


class ScheduleConfig(PluginConfigBase):
    """刷取频率与节奏。"""

    __ui_label__ = "刷取频率"
    __ui_icon__ = "clock"
    __ui_order__ = 1

    interval_minutes: int = Field(
        default=60,
        ge=1,
        le=1440,
        description="每隔多少分钟自动刷一次 B 站：1=每分钟，60=每小时，1440=每天（可调范围 1 分钟 ~ 24 小时）",
        json_schema_extra={"label": "刷取间隔（分钟）"},
    )
    jitter_seconds: int = Field(
        default=120,
        ge=0,
        le=3600,
        description="每轮刷取前叠加的随机抖动秒数，避免每天固定时刻刷",
        json_schema_extra={"label": "随机抖动（秒）"},
    )
    run_on_start: bool = Field(
        default=True,
        description="插件加载后是否立刻先刷一轮",
        json_schema_extra={"label": "启动时先刷一轮"},
    )
    active_hours: str = Field(
        default="",
        description="允许自动分享的时间段，如 08:00-23:30；留空表示全天允许",
        json_schema_extra={"label": "活跃时段", "placeholder": "08:00-23:30"},
    )
    max_share_per_round: int = Field(
        default=1,
        ge=1,
        le=10,
        description="每个聊天流每轮最多分享几个视频",
        json_schema_extra={"label": "每轮最多分享（个）"},
    )
    search_cooldown_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="用户主动要求搜索分享的冷却秒数（默认 300=5 分钟）；与自动分享频率互不影响",
        json_schema_extra={"label": "主动搜索冷却（秒）"},
    )


class AccountConfig(PluginConfigBase):
    """B 站登录：Cookie 可以手动填写，也可以在聊天里用 /扫码登录 扫码保存。"""

    __ui_label__ = "账号登录"
    __ui_icon__ = "key"
    __ui_order__ = 2

    cookie: str = Field(
        default="",
        description=(
            "B 站 Cookie（主要是 SESSDATA）。可以手动粘贴，也可以直接在聊天里发 /扫码登录 用手机扫码，"
            "扫码成功后会自动保存，不用手填。留空表示匿名访问（匿名读不到字幕、排行榜接口也容易被风控）"
        ),
        json_schema_extra={
            "label": "B 站 Cookie",
            "placeholder": "SESSDATA=...; bili_jct=...",
            "rows": 3,
        },
    )
    qrcode_timeout_seconds: int = Field(
        default=180,
        ge=30,
        le=600,
        description="/扫码登录 发出二维码后等待手机确认的秒数，超时就作废",
        json_schema_extra={"label": "扫码等待时长（秒）"},
    )
    check_on_start: bool = Field(
        default=True,
        description="插件启动时检查一次登录状态，Cookie 失效会在日志里提示",
        json_schema_extra={"label": "启动时检查登录状态"},
    )


class SourcesConfig(PluginConfigBase):
    """候选视频来源。"""

    __ui_label__ = "视频来源"
    __ui_icon__ = "video"
    __ui_order__ = 3

    popular_enabled: bool = Field(
        default=False,
        description="是否抓取 B 站「热门」列表（热门榜以官方/大 UP 为主，默认关闭以避开主流官方内容）",
        json_schema_extra={"label": "抓取热门列表"},
    )
    popular_page_size: int = Field(
        default=20,
        ge=1,
        le=50,
        description="热门列表每页条数",
        json_schema_extra={"label": "热门每页条数"},
    )
    popular_pages: int = Field(
        default=1,
        ge=1,
        le=5,
        description="热门列表抓取页数",
        json_schema_extra={"label": "热门抓取页数"},
    )
    search_enabled: bool = Field(
        default=True,
        description="是否按兴趣关键词 / 内容类别搜索 B 站视频（推荐开启，命中率最高）",
        json_schema_extra={"label": "按兴趣搜索视频"},
    )
    search_pages: int = Field(
        default=1,
        ge=1,
        le=3,
        description="每个关键词搜索的页数",
        json_schema_extra={"label": "每个关键词搜索页数"},
    )
    search_keywords_per_round: int = Field(
        default=3,
        ge=1,
        le=10,
        description="每轮最多用几个兴趣关键词去搜索（按权重取前 N 个）",
        json_schema_extra={"label": "每轮搜索关键词数"},
    )
    search_order: str = Field(
        default="",
        description="搜索结果排序：留空=综合排序；可选 pubdate(最新)、click(播放多)、dm(弹幕多)",
        json_schema_extra={"label": "搜索排序方式"},
    )
    search_delay_seconds: float = Field(
        default=1.2,
        ge=0.0,
        le=10.0,
        description="关键词之间搜索的间隔秒数，避免请求过于密集",
        json_schema_extra={"label": "搜索间隔（秒）"},
    )
    ranking_enabled: bool = Field(
        default=False,
        description="是否抓取 B 站「排行榜」（该接口风控较严，未配置 Cookie 时经常失败）",
        json_schema_extra={"label": "抓取排行榜"},
    )
    ranking_rids: List[int] = Field(
        default_factory=lambda: [0],
        description="排行榜分区 rid 列表，0=全站；常见：1 动画、3 音乐、4 游戏、5 娱乐、36 科技、188 数码",
        json_schema_extra={"label": "排行榜分区"},
    )


class CategoryConfig(PluginConfigBase):
    """一个主动分享的内容类别（``[[categories]]``）。"""

    __ui_label__ = "内容类别"
    __ui_icon__ = "layers"
    __ui_order__ = 4

    name: str = Field(
        default="",
        description="类别名称，如「新鲜资讯」「社区创作」",
        json_schema_extra={"label": "类别名称"},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用这个类别",
        json_schema_extra={"label": "启用该类别"},
    )
    keywords: List[str] = Field(
        default_factory=list,
        description="该类别的搜索关键词，如 新游、更新、二创、手书、整活、搬运",
        json_schema_extra={"label": "搜索关键词"},
    )
    search_order: str = Field(
        default="",
        description="该类别搜索排序：留空沿用「视频来源」里的搜索排序；资讯类可用 pubdate",
        json_schema_extra={"label": "该类别搜索排序"},
    )
    max_share_per_round: int = Field(
        default=0,
        ge=0,
        le=10,
        description="该类别每轮最多分享几个；0 表示不单独限制",
        json_schema_extra={"label": "该类别每轮最多分享"},
    )
    min_play: int = Field(
        default=0,
        ge=0,
        description="该类别的最低播放量；0 表示沿用「过滤」里的最低播放量",
        json_schema_extra={"label": "该类别最低播放量"},
    )


class InterestConfig(PluginConfigBase):
    """兴趣提取与语义判断策略。"""

    __ui_label__ = "兴趣提取"
    __ui_icon__ = "heart"
    __ui_order__ = 5

    use_recent_chat: bool = Field(
        default=True,
        description="是否从白名单聊天流的近期消息里自动提取兴趣关键词",
        json_schema_extra={"label": "从聊天记录提取兴趣"},
    )
    lookback_hours: float = Field(
        default=48.0,
        ge=1.0,
        le=720.0,
        description="自动提取兴趣时回看最近多少小时的聊天记录",
        json_schema_extra={"label": "回看最近多少小时"},
    )
    max_messages_per_stream: int = Field(
        default=200,
        ge=10,
        le=2000,
        description="每个聊天流最多读取多少条近期消息用于提取兴趣",
        json_schema_extra={"label": "每个会话读取消息上限"},
    )
    top_keywords: int = Field(
        default=12,
        ge=1,
        le=50,
        description="自动提取的兴趣关键词数量上限",
        json_schema_extra={"label": "自动兴趣词数量上限"},
    )
    keyword_min_count: int = Field(
        default=2,
        ge=1,
        le=20,
        description="关键词至少出现多少次才会被采用",
        json_schema_extra={"label": "关键词最少出现次数"},
    )
    explicit_weight: float = Field(
        default=3.0,
        ge=0.0,
        le=20.0,
        description="聊天流配置里手写兴趣词的权重",
        json_schema_extra={"label": "手写兴趣权重"},
    )
    auto_weight: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description="自动提取兴趣词的权重",
        json_schema_extra={"label": "自动兴趣权重"},
    )
    persist_auto_interests: bool = Field(
        default=True,
        description=(
            "是否把自动提取的兴趣写回对应聊天流的「自动提取的兴趣」配置项，"
            "这样在设置页就能看到并修改；关掉则只在运行时使用，设置页不会更新"
        ),
        json_schema_extra={"label": "自动兴趣写回配置"},
    )
    persona_seed_enabled: bool = Field(
        default=True,
        description=(
            "还没有任何兴趣（既没手写、聊天记录里也提不出）时，让 LLM 按 MaiBot 配置的人设"
            "自动生成一份初始兴趣，避免新群第一次刷 B 站时完全没有方向"
        ),
        json_schema_extra={"label": "用人设生成初始兴趣"},
    )
    persona_seed_count: int = Field(
        default=8,
        ge=3,
        le=20,
        description="用人设生成初始兴趣时生成几个词",
        json_schema_extra={"label": "初始兴趣数量"},
    )
    persona_seed_interests: List[str] = Field(
        default_factory=list,
        description=(
            "按人设生成的初始兴趣，**通用配置，所有聊天流共用**。"
            "留空时插件会按人设让 LLM **自动生成一次**并填到这里；"
            "某个聊天流既没手写兴趣、聊天记录里也提不出词时也用它兜底，你也可以直接改"
        ),
        json_schema_extra={"label": "人设初始兴趣"},
    )
    exclude_user_ids: List[str] = Field(
        default_factory=list,
        description=(
            "不参与兴趣提取的账号（填 user_id / QQ 号）。"
            "插件自己的账号会自动跳过；"
            "其他 bot 没法自动认出来，要挡哪个就把它填到这里"
        ),
        json_schema_extra={"label": "不参与提取的账号"},
    )
    use_tags: bool = Field(
        default=True,
        description="是否让视频 tags（搜索接口返回）参与兴趣打分",
        json_schema_extra={"label": "使用视频标签"},
    )
    tag_weight: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="tags 命中时的字段权重",
        json_schema_extra={"label": "标签命中权重"},
    )
    use_subtitles: bool = Field(
        default=True,
        description="是否尝试读取视频 CC / AI 字幕辅助判断（匿名访问读不到字幕，需要登录 Cookie）",
        json_schema_extra={"label": "读取字幕"},
    )
    use_llm_judge: bool = Field(
        default=True,
        description="是否用 LLM 做语义判断（判断视频是否真的贴合兴趣，并生成推荐语与介绍）",
        json_schema_extra={"label": "LLM 语义判断"},
    )
    llm_task_name: str = Field(
        default="planner",
        description="文本类 LLM 调用使用的模型任务名（默认 planner，可在 model_config.toml 里改）",
        json_schema_extra={"label": "LLM 任务名"},
    )
    llm_max_candidates: int = Field(
        default=5,
        ge=1,
        le=20,
        description="每轮最多让 LLM 判断几个候选视频（控制成本；判断通过的第一个会被分享）",
        json_schema_extra={"label": "每轮 LLM 判断候选数"},
    )
    llm_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="LLM 温度，越高越发散",
        json_schema_extra={"label": "LLM 温度"},
    )
    extra_stopwords: List[str] = Field(
        default_factory=list,
        description="额外的停用词，避免把口头禅当成兴趣",
        json_schema_extra={"label": "额外停用词"},
    )


class VisionConfig(PluginConfigBase):
    """抽帧识图。"""

    __ui_label__ = "抽帧识图"
    __ui_icon__ = "image"
    __ui_order__ = 6

    enabled: bool = Field(
        default=True,
        description="是否抽帧看画面（需要 ffmpeg；只对最终入选的视频做）",
        json_schema_extra={"label": "启用抽帧识图"},
    )
    task_name: str = Field(
        default="vlm",
        description="识图用的模型任务名（必须支持视觉；默认 vlm，planner 模型通常不支持图片）",
        json_schema_extra={"label": "视觉模型任务名"},
    )
    frame_count: int = Field(
        default=4,
        ge=1,
        le=12,
        description="抽取几帧",
        json_schema_extra={"label": "抽取帧数"},
    )
    max_width: int = Field(
        default=640,
        ge=160,
        le=1280,
        description="帧图片最大宽度（越小越省 token）",
        json_schema_extra={"label": "帧图片最大宽度"},
    )
    max_download_mb: int = Field(
        default=80,
        ge=5,
        le=500,
        description="下载视频用于抽帧的最大体积（MB）",
        json_schema_extra={"label": "下载体积上限（MB）"},
    )
    timeout_seconds: float = Field(
        default=90.0,
        ge=10.0,
        le=600.0,
        description="下载 + 抽帧的总超时",
        json_schema_extra={"label": "抽帧总超时（秒）"},
    )


class DanmakuConfig(PluginConfigBase):
    """弹幕与热门评论。"""

    __ui_label__ = "弹幕评论"
    __ui_icon__ = "message-square"
    __ui_order__ = 7

    danmaku_enabled: bool = Field(
        default=True,
        description="是否读取弹幕",
        json_schema_extra={"label": "读取弹幕"},
    )
    danmaku_fetch_limit: int = Field(
        default=400,
        ge=20,
        le=3000,
        description="最多抓取多少条弹幕",
        json_schema_extra={"label": "弹幕抓取上限"},
    )
    danmaku_limit: int = Field(
        default=40,
        ge=5,
        le=200,
        description="过滤后最多送多少条弹幕给 LLM",
        json_schema_extra={"label": "送 LLM 的弹幕条数"},
    )
    comments_enabled: bool = Field(
        default=True,
        description="是否读取热门评论",
        json_schema_extra={"label": "读取热门评论"},
    )
    comments_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="最多读取多少条热门评论",
        json_schema_extra={"label": "热门评论条数"},
    )
    filter_ai_spam: bool = Field(
        default=True,
        description="过滤 AI 鱼灾式的模板弹幕/评论（重复、无意义、AI 生成痕迹等）",
        json_schema_extra={"label": "过滤 AI 鱼灾弹幕"},
    )


class FilterConfig(PluginConfigBase):
    """视频硬过滤条件。"""

    __ui_label__ = "过滤"
    __ui_icon__ = "filter"
    __ui_order__ = 8

    min_duration_seconds: int = Field(
        default=60,
        ge=0,
        description="视频最短时长（秒）",
        json_schema_extra={"label": "最短时长（秒）"},
    )
    max_duration_seconds: int = Field(
        default=1800,
        ge=60,
        description="视频最长时长（秒）",
        json_schema_extra={"label": "最长时长（秒）"},
    )
    min_play: int = Field(
        default=5000,
        ge=0,
        description="最低播放量",
        json_schema_extra={"label": "最低播放量"},
    )
    min_like: int = Field(
        default=100,
        ge=0,
        description="最低点赞数",
        json_schema_extra={"label": "最低点赞数"},
    )
    max_age_days: float = Field(
        default=30.0,
        ge=0.0,
        le=3650.0,
        description="只分享最近多少天内发布的视频；0 表示不限制",
        json_schema_extra={"label": "最大发布天数"},
    )
    block_keywords: List[str] = Field(
        default_factory=lambda: ["广告", "带货", "抽奖", "互粉", "三连", "付费课程"],
        description="标题/简介/UP 名命中这些词直接排除",
        json_schema_extra={"label": "屏蔽关键词"},
    )
    block_up_names: List[str] = Field(
        default_factory=list,
        description="屏蔽的 UP 主名称",
        json_schema_extra={"label": "屏蔽 UP 主"},
    )
    exclude_partitions: List[str] = Field(
        default_factory=list,
        description="屏蔽的分区名（tname）",
        json_schema_extra={"label": "屏蔽分区"},
    )
    exclude_official: bool = Field(
        default=True,
        description="是否排除主流官方内容（标题/简介命中「官方内容关键词」的视频）",
        json_schema_extra={"label": "排除官方内容"},
    )
    official_keywords: List[str] = Field(
        default_factory=lambda: [
            "官方", "预告", "PV", "正片", "发布会", "宣传片", "定档", "首曝",
            "概念片", "实机演示", "OP", "ED", "主题曲",
            "前瞻", "速览", "兑换码", "直播回放", "特别节目", "版本更新说明",
        ],
        description="判定为「主流官方内容」的关键词（命中标题/简介即排除）",
        json_schema_extra={"label": "官方内容关键词"},
    )


class ScoringConfig(PluginConfigBase):
    """打分权重与阈值。"""

    __ui_label__ = "打分"
    __ui_icon__ = "sliders"
    __ui_order__ = 9

    interest_weight: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description="兴趣命中的权重",
        json_schema_extra={"label": "兴趣权重"},
    )
    title_match_boost: float = Field(
        default=2.0,
        ge=1.0,
        le=10.0,
        description="标题命中的额外倍数",
        json_schema_extra={"label": "标题命中倍数"},
    )
    hot_weight: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="热度（播放/点赞/投币等）权重",
        json_schema_extra={"label": "热度权重"},
    )
    freshness_weight: float = Field(
        default=0.6,
        ge=0.0,
        le=5.0,
        description="新鲜度权重",
        json_schema_extra={"label": "新鲜度权重"},
    )
    freshness_days: float = Field(
        default=7.0,
        ge=0.5,
        le=60.0,
        description="新鲜度衰减天数",
        json_schema_extra={"label": "新鲜度衰减天数"},
    )
    min_score: float = Field(
        default=4.0,
        ge=0.0,
        le=100.0,
        description="入选最低分",
        json_schema_extra={"label": "入选最低分"},
    )
    require_interest_match: bool = Field(
        default=True,
        description="是否必须至少命中一个兴趣词（关闭后纯靠热度也可能入选）",
        json_schema_extra={"label": "必须命中兴趣词"},
    )


class PersonaConfig(PluginConfigBase):
    """分享语气：使用 MaiBot 配置的人设。"""

    __ui_label__ = "人设语气"
    __ui_icon__ = "user"
    __ui_order__ = 10

    enabled: bool = Field(
        default=True,
        description="分享文案是否使用 MaiBot 配置的人设语气（通过 config.get 能力读取宿主配置）",
        json_schema_extra={"label": "启用人设语气"},
    )
    name: str = Field(
        default="",
        description="覆盖人设名字；留空自动读取宿主的昵称",
        json_schema_extra={"label": "覆盖人设名字"},
    )
    personality: str = Field(
        default="",
        description="覆盖人设描述；留空自动读取宿主的人设配置",
        json_schema_extra={"label": "覆盖人设描述"},
    )
    reply_style: str = Field(
        default="",
        description="覆盖说话风格；留空自动读取宿主的说话风格配置",
        json_schema_extra={"label": "覆盖说话风格"},
    )
    style_hint: str = Field(
        default="",
        description="额外的风格要求，会追加到人设后面，如「分享时不要太啰嗦，最多两句话」",
        json_schema_extra={"label": "额外风格要求"},
    )
    cache_seconds: int = Field(
        default=120,
        ge=0,
        le=3600,
        description="人设缓存秒数；宿主改人设后最多这么久生效",
        json_schema_extra={"label": "人设缓存（秒）"},
    )


class ShareConfig(PluginConfigBase):
    """分享格式。"""

    __ui_label__ = "分享格式"
    __ui_icon__ = "send"
    __ui_order__ = 11

    casual_mode: bool = Field(
        default=True,
        description=(
            "「随口一提」模式：把一句话和【标题】链接合成一条消息，语气像群里随口说话，"
            "不宣布「我要分享/推荐」这种意图；关闭则回到「推荐语 / 标题链接 / 介绍」分条格式"
        ),
        json_schema_extra={"label": "随口一提模式"},
    )
    casual_max_chars: int = Field(
        default=60,
        ge=20,
        le=160,
        description="随口一句的最大字数（仅「随口一提」模式生效）",
        json_schema_extra={"label": "随口一句字数上限"},
    )
    intro_enabled: bool = Field(
        default=True,
        description="是否先用一句人设口吻的推荐语开场（仅关闭「随口一提」模式时生效）",
        json_schema_extra={"label": "发送推荐语"},
    )
    intro_max_chars: int = Field(
        default=40,
        ge=10,
        le=120,
        description="推荐语的最大字数",
        json_schema_extra={"label": "推荐语字数上限"},
    )
    send_as_parts: bool = Field(
        default=True,
        description="是否分条发送：①推荐语 ②【标题】链接 ③一段自然语言介绍；关闭则合成一条消息（「随口一提」模式下始终只发一条）",
        json_schema_extra={"label": "分条发送"},
    )
    part_delay_seconds: float = Field(
        default=0.8,
        ge=0.0,
        le=10.0,
        description="分条发送之间的间隔秒数",
        json_schema_extra={"label": "分条间隔（秒）"},
    )
    narrative_enabled: bool = Field(
        default=True,
        description=(
            "是否发一段人设口吻的自然语言介绍（把「讲了什么」和「有什么好看」揉在一起，"
            "不用「内容：」「看点：」这类格式；仅关闭「随口一提」模式时生效）"
        ),
        json_schema_extra={"label": "发送自然语言介绍"},
    )
    narrative_max_chars: int = Field(
        default=160,
        ge=40,
        le=500,
        description="自然语言介绍的最大字数",
        json_schema_extra={"label": "介绍字数上限"},
    )
    include_stats_line: bool = Field(
        default=False,
        description="是否在链接消息里附带 UP / 时长 / 播放量等信息",
        json_schema_extra={"label": "附带视频信息"},
    )


class ReshareConfig(PluginConfigBase):
    """自然语言「再发一遍」。"""

    __ui_label__ = "重发"
    __ui_icon__ = "repeat"
    __ui_order__ = 12

    enabled: bool = Field(
        default=True,
        description="是否允许在聊天里用自然语言要求重发之前分享过的视频（如「你之前发的那个XX再发一遍」）",
        json_schema_extra={"label": "启用自然语言重发"},
    )
    trigger_keywords: List[str] = Field(
        default_factory=lambda: [
            "再发一遍", "再发一次", "再分享", "重新发", "再来一遍", "再放一次",
            "再发下", "重发", "发一遍", "发一次",
        ],
        description="触发重发的关键词",
        json_schema_extra={"label": "触发关键词"},
    )
    history_keywords: List[str] = Field(
        default_factory=lambda: ["之前", "上次", "那个", "刚才", "你发过", "你分享过", "前面"],
        description="配合触发词使用，表示「指代之前分享过的视频」",
        json_schema_extra={"label": "指代关键词"},
    )
    match_window_days: float = Field(
        default=60.0,
        ge=1.0,
        le=365.0,
        description="在多长时间内的分享记录里查找",
        json_schema_extra={"label": "查找记录天数"},
    )
    min_match_score: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="标题模糊匹配的最低相似度",
        json_schema_extra={"label": "最低匹配相似度"},
    )
    reply_enabled: bool = Field(
        default=True,
        description="重发时是否附带一句说明",
        json_schema_extra={"label": "重发时附带说明"},
    )


class ShiweiConfig(PluginConfigBase):
    """``/赤石视频``：石味（抽象整活）视频。"""

    __ui_label__ = "赤石视频"
    __ui_icon__ = "flame"
    __ui_order__ = 13

    enabled: bool = Field(
        default=True,
        description="是否启用 /赤石视频 指令",
        json_schema_extra={"label": "启用赤石视频"},
    )
    cooldown_seconds: int = Field(
        default=600,
        ge=0,
        le=86400,
        description="该指令的冷却秒数（默认 10 分钟）",
        json_schema_extra={"label": "冷却（秒）"},
    )
    max_share: int = Field(
        default=1,
        ge=1,
        le=5,
        description="一次最多分享几个石味视频",
        json_schema_extra={"label": "一次最多分享"},
    )
    keywords: List[str] = Field(
        default_factory=lambda: [
            "抽象", "整活", "鬼畜", "离谱", "翻车", "整蛊", "土味", "迷惑行为",
            "逆天", "难绷", "典中典", "社死", "搞笑",
        ],
        description="石味视频的搜索关键词",
        json_schema_extra={"label": "搜索关键词"},
    )
    search_keywords_per_round: int = Field(
        default=2,
        ge=1,
        le=6,
        description="每次用几个关键词搜索",
        json_schema_extra={"label": "每次搜索关键词数"},
    )
    min_play: int = Field(
        default=20000,
        ge=0,
        description="石味视频的最低播放量",
        json_schema_extra={"label": "最低播放量"},
    )
    min_duration_seconds: int = Field(
        default=10,
        ge=0,
        description="最短时长（秒）",
        json_schema_extra={"label": "最短时长（秒）"},
    )
    max_duration_seconds: int = Field(
        default=900,
        ge=60,
        description="最长时长（秒）",
        json_schema_extra={"label": "最长时长（秒）"},
    )
    block_keywords: List[str] = Field(
        default_factory=lambda: ["官方", "预告", "发布会", "宣传片"],
        description="石味视频额外排除的关键词",
        json_schema_extra={"label": "额外排除关键词"},
    )


class AdvancedConfig(PluginConfigBase):
    """运行参数。"""

    __ui_label__ = "高级"
    __ui_icon__ = "settings"
    __ui_order__ = 14

    data_dir: str = Field(
        default="",
        description="状态文件目录；留空使用插件默认数据目录",
        json_schema_extra={"label": "数据目录"},
    )
    request_timeout_seconds: float = Field(
        default=12.0,
        ge=1.0,
        le=60.0,
        description="B 站接口请求超时",
        json_schema_extra={"label": "接口请求超时（秒）"},
    )
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        description="请求 B 站时使用的 User-Agent",
        json_schema_extra={"label": "浏览器标识（User-Agent）"},
    )
    cookie: str = Field(
        default="",
        description="旧版字段，已迁移到「账号登录」里的 Cookie；这里留空即可",
        json_schema_extra={"label": "（旧版）Cookie", "hidden": True},
    )
    ffmpeg_path: str = Field(
        default="",
        description="ffmpeg 可执行文件路径；留空则自动从 imageio-ffmpeg 或 PATH 查找",
        json_schema_extra={"label": "ffmpeg 路径"},
    )
    dedupe_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="同一个视频多少天内不重复分享",
        json_schema_extra={"label": "去重天数"},
    )
    max_seen_records: int = Field(
        default=3000,
        ge=100,
        le=20000,
        description="本地去重记录上限",
        json_schema_extra={"label": "去重记录上限"},
    )
    debug: bool = Field(
        default=False,
        description="输出调试日志",
        json_schema_extra={"label": "调试日志"},
    )


class StreamTargetConfig(PluginConfigBase):
    """白名单聊天流配置（数组表 ``[[streams]]``）。"""

    __ui_label__ = "聊天流"
    __ui_icon__ = "message-circle"
    __ui_order__ = 15

    name: str = Field(
        default="",
        description="给这个聊天流起的备注名（便于识别）",
        json_schema_extra={"label": "备注名"},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用这个聊天流",
        json_schema_extra={"label": "启用该会话"},
    )
    stream_id: str = Field(
        default="",
        description="聊天流 ID（MaiBot 的 session_id）；填写后优先级最高",
        json_schema_extra={"label": "会话 ID"},
    )
    group_id: str = Field(
        default="",
        description="群号：未填会话 ID 时按群号自动查找聊天流",
        json_schema_extra={"label": "群号"},
    )
    user_id: str = Field(
        default="",
        description="私聊用户 ID：未填会话 ID / 群号时按用户查找",
        json_schema_extra={"label": "私聊用户 ID"},
    )
    interests: List[str] = Field(
        default_factory=list,
        description="这个聊天流的手写兴趣词，如 原神、编程、机械键盘、猫咪（权重最高，不会被自动覆盖）",
        json_schema_extra={"label": "手写兴趣"},
    )
    auto_interests: List[str] = Field(
        default_factory=list,
        description=(
            "插件从聊天记录里自动提取的兴趣词，会写回这里，方便在设置页直接看到；"
            "手动改过之后插件就不再自动覆盖（相当于锁定），想恢复自动维护把它清空即可"
        ),
        json_schema_extra={"label": "自动提取的兴趣"},
    )
    categories: List[str] = Field(
        default_factory=list,
        description="这个聊天流启用的内容类别名（对应内容类别里的名称）；留空表示使用全部启用的类别",
        json_schema_extra={"label": "启用的内容类别"},
    )
    auto_interest: bool = Field(
        default=True,
        description="是否额外从该流的近期聊天里自动提取兴趣（需开启「兴趣提取」里的从聊天记录提取兴趣）",
        json_schema_extra={"label": "自动提取兴趣"},
    )
    max_share_per_round: int = Field(
        default=0,
        ge=0,
        le=10,
        description="该流每轮最多分享几个视频；0 表示使用「刷取频率」里的每轮最多分享",
        json_schema_extra={"label": "该流每轮最多分享"},
    )


def _default_categories() -> List[CategoryConfig]:
    """默认的四类主动分享内容。"""

    return [
        CategoryConfig(
            name="新鲜资讯",
            enabled=True,
            keywords=["新游", "版本更新", "资讯", "首曝", "开箱"],
            search_order="pubdate",
        ),
        CategoryConfig(
            name="社区创作",
            enabled=True,
            keywords=["二创", "手书", "MAD", "同人", "混剪"],
        ),
        CategoryConfig(
            name="玩家投稿",
            enabled=True,
            keywords=["整活", "实测", "挑战", "攻略", "搞笑"],
        ),
        CategoryConfig(
            name="外网搬运",
            enabled=True,
            keywords=["搬运", "中字", "外网", "转载"],
        ),
    ]


class BilibiliInterestShareConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(
        default_factory=PluginSectionConfig,
        description="插件基础开关（是否启用、配置版本号）",
    )
    schedule: ScheduleConfig = Field(
        default_factory=ScheduleConfig,
        description="自动刷 B 站的频率（间隔、抖动、活跃时段、每轮分享数）",
    )
    account: AccountConfig = Field(
        default_factory=AccountConfig,
        description="B 站登录（Cookie，或直接用 /扫码登录 扫码）",
    )
    sources: SourcesConfig = Field(
        default_factory=SourcesConfig,
        description="候选来源（关键词搜索、热门列表、排行榜）",
    )
    categories: List[CategoryConfig] = Field(
        default_factory=_default_categories,
        description="主动分享的内容类别；每个类别有自己的搜索关键词，可自行增删",
        json_schema_extra={"label": "内容类别"},
    )
    interest: InterestConfig = Field(
        default_factory=InterestConfig,
        description="兴趣画像与语义判断（怎么判断聊天流对什么感兴趣）",
    )
    vision: VisionConfig = Field(
        default_factory=VisionConfig,
        description="抽帧看画面（只对最终入选的视频做，需要 ffmpeg）",
    )
    danmaku: DanmakuConfig = Field(
        default_factory=DanmakuConfig,
        description="弹幕与热门评论（帮助判断视频实际讲了什么）",
    )
    filter: FilterConfig = Field(
        default_factory=FilterConfig,
        description="硬过滤（时长、播放、点赞、屏蔽词、官方物料等）",
    )
    scoring: ScoringConfig = Field(
        default_factory=ScoringConfig,
        description="打分规则（兴趣命中、热度、新鲜度）",
    )
    persona: PersonaConfig = Field(
        default_factory=PersonaConfig,
        description="人设语气（分享文案按 MaiBot 配置的人设来说）",
    )
    share: ShareConfig = Field(
        default_factory=ShareConfig,
        description="分享格式（随口一提、分条发送、自然语言介绍）",
    )
    reshare: ReshareConfig = Field(
        default_factory=ReshareConfig,
        description="自然语言重发（聊天里说「把之前那个再发一遍」）",
    )
    shiwei: ShiweiConfig = Field(
        default_factory=ShiweiConfig,
        description="/赤石视频 指令（抽象整活类视频分享）",
    )
    advanced: AdvancedConfig = Field(
        default_factory=AdvancedConfig,
        description="高级运行参数（数据目录、超时、去重、调试）",
    )
    streams: List[StreamTargetConfig] = Field(
        default_factory=list,
        description="白名单聊天流列表；只有这里列出的聊天流才会收到视频分享",
        json_schema_extra={"label": "白名单聊天流"},
    )
