"""兴趣提取与视频打分。

整体思路是「确定性关键词匹配」，不依赖 LLM：

1. 从白名单聊天流的近期消息里统计高频中文 n-gram（2~4 字）与英文单词，
   过滤停用词后得到自动兴趣词；
2. 与配置里手写的兴趣词合并成兴趣画像 ``InterestProfile``；
3. 对每个候选视频，在标题/分区/简介/UP 名里做子串匹配得到兴趣分，
   再叠加热度分与新鲜度分；
4. 先按硬条件过滤（时长、播放、点赞、屏蔽词），再按总分排序。
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .bilibili import Video

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]{1,24}")

# 常见口头禅/虚词：整词命中直接丢弃
STOPWORDS = {
    "就是", "这个", "那个", "什么", "怎么", "可以", "没有", "还是", "但是", "因为",
    "所以", "然后", "现在", "今天", "感觉", "真的", "一个", "不是", "我们", "你们",
    "他们", "自己", "时候", "东西", "知道", "觉得", "已经", "有点", "比较", "可能",
    "应该", "其实", "这样", "那样", "这么", "那么", "如果", "而且", "或者", "不过",
    "还有", "一些", "这种", "那种", "问题", "哈哈", "哈哈哈", "哈哈哈哈", "emmm",
    "就是", "确实", "主要", "直接", "一直", "一起", "一下", "一点", "怎么", "为啥",
    "为什么", "怎么样", "是不是", "有没有", "能不能", "要不要", "好不好", "对不对",
    "完了", "好的", "行吧", "算了", "没事", "有点", "好像", "估计", "大概", "反正",
    "以及", "关于", "对于", "通过", "根据", "比如", "例如", "另外", "此外", "同时",
    "the", "and", "for", "you", "are", "this", "that", "with", "have", "not",
    # 聊天里高频但没有兴趣含义的词（真实群聊里提取出来会污染兴趣画像）
    "用户", "主人", "消息", "系统", "机器人", "图片", "表情", "链接", "文件",
    "语音", "视频", "直播", "群友", "群主", "管理", "管理员", "记得", "刚才",
    "等下", "晚点", "明天", "昨天", "早上", "中午", "晚上", "先", "再看",
    "https", "http", "www", "com", "cn", "html", "jpeg", "jpg", "png", "gif",
    "qq", "ww", "hhh", "hh", "233", "2333", "awa", "qwq", "orz", "lol",
    "哔哩", "bilibili", "b站", "阿b", "up", "up主",
}

# 单字停用字：一个 n-gram 如果全部由这些字组成，也丢弃
_SINGLE_STOP_CHARS = set(
    "的了吗呢吧啊哦嗯是在有我你他她它们这那个就都也还和与及或但不没很太又要会能可对说道着过被把让给从向以于为之其而且所如果因为所以然后什么怎么可以没有我们你们他们自己时候东西知道觉得已经有点比较可能应该其实这样那样这么那么一些这种那种问题哈"
)

# 虚词：出现在 n-gram 的开头或结尾，说明它只是从一句话里截下来的碎片。
# 只放几乎不会作为名词首尾的字（「能」「会」「对」「为」这类会误杀「智能」「机会」「绝对」「行为」）。
_EDGE_STOP_CHARS = set("的了吗呢吧啊哦嗯")


def normalize_term(term: str) -> str:
    """归一化兴趣词：去空白、英文转小写。"""

    if not isinstance(term, str):
        return ""
    normalized = term.strip()
    if not normalized:
        return ""
    if all(ord(ch) < 128 for ch in normalized):
        normalized = normalized.lower()
    return normalized


def is_meaningful_term(term: str, extra_stopwords: Optional[Iterable[str]] = None) -> bool:
    """判断一个词是否值得作为兴趣词。"""

    normalized = normalize_term(term)
    if len(normalized) < 2:
        return False
    if normalized.isdigit():
        return False
    # URL / 域名 / 路径碎片（www.bilibili.com、video/BV...）不是兴趣
    if any(ch in normalized for ch in "./:@#?=&%+"):
        return False
    if normalized in STOPWORDS:
        return False
    if extra_stopwords and normalized in {normalize_term(w) for w in extra_stopwords if w}:
        return False
    if all(ch in _SINGLE_STOP_CHARS for ch in normalized):
        return False
    # 以虚词开头/结尾的多半是「回复了」「的消息」这类从句子里截出来的碎片
    if normalized[0] in _EDGE_STOP_CHARS or normalized[-1] in _EDGE_STOP_CHARS:
        return False
    return True


def iter_terms(text: str) -> Iterable[str]:
    """把一段文本拆成候选兴趣词（优先用 jieba 分词，退化为中文 n-gram + 英文单词）。"""

    if not text:
        return
    lowered = text.lower()
    if _jieba_available():
        for word in _segment_cjk(lowered):
            yield word
        for word in _LATIN_RE.findall(lowered):
            if word.isdigit():
                continue
            yield word
        return
    for run in _CJK_RUN_RE.findall(lowered):
        length = len(run)
        for size in (2, 3, 4):
            if length < size:
                continue
            for index in range(length - size + 1):
                gram = run[index : index + size]
                if all(ch in _SINGLE_STOP_CHARS for ch in gram):
                    continue
                yield gram
    for word in _LATIN_RE.findall(lowered):
        if word.isdigit():
            continue
        yield word


# 能当兴趣词保留的中文词性：名词类 + 英文。其余（动词/副词/虚词/数词）多半是
# 「回复」「觉得」「真的」这种口语碎片。
_KEEP_POS_PREFIXES = ("n", "eng", "vn", "an", "j", "l", "i", "nz", "nt", "nr")

_JIEBA_PSEG: Any = None
_JIEBA_FAILED = False


def _jieba_available() -> bool:
    """宿主环境装没装 jieba（装了就用分词，比 n-gram 干净得多）。"""

    global _JIEBA_PSEG, _JIEBA_FAILED
    if _JIEBA_PSEG is not None:
        return True
    if _JIEBA_FAILED:
        return False
    try:
        import jieba.posseg as pseg  # type: ignore[import-not-found]

        _JIEBA_PSEG = pseg
        return True
    except Exception:  # noqa: BLE001 - 没有 jieba 就退回 n-gram
        _JIEBA_FAILED = True
        return False


def _segment_cjk(text: str) -> List[str]:
    """用 jieba 分词，只保留名词类词（不做相邻词拼接）。

    拼复合词看着更「像兴趣」（「机械键盘」），但会让基础词被包含规则吃掉：
    「原神」被「原神版本」压掉之后，标题叫「原神攻略」的视频就匹配不上了，
    召回反而变差。单词匹配是按子串找的，「机械」照样能命中「机械键盘评测」。
    """

    if _JIEBA_PSEG is None:
        return []
    words: List[str] = []
    try:
        for raw_word, flag in _JIEBA_PSEG.cut(text):
            word = raw_word.strip()
            if len(word) < 2:
                continue
            if _CJK_RUN_RE.fullmatch(word) is None:
                continue
            if not str(flag).startswith(_KEEP_POS_PREFIXES):
                continue
            words.append(word)
    except Exception:  # noqa: BLE001 - 分词出错就当没分出来
        return []
    return words


def extract_interest_keywords(
    messages: Sequence[Dict[str, Any]],
    *,
    top_n: int = 12,
    min_count: int = 2,
    extra_stopwords: Optional[Iterable[str]] = None,
    max_message_chars: int = 200,
) -> List[Tuple[str, int]]:
    """从消息列表里统计高频兴趣词。

    Args:
        messages: ``message.get_recent`` 返回的消息字典列表。
        top_n: 最多返回多少个词。
        min_count: 词频低于该值不采用。
        extra_stopwords: 额外停用词。
        max_message_chars: 单条消息只取前多少个字符，避免长文刷屏。

    Returns:
        ``[(词, 次数), ...]``，按次数降序。
    """

    counts: Counter[str] = Counter()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if message.get("is_command"):
            continue
        text = message.get("processed_plain_text") or ""
        if not isinstance(text, str) or not text.strip():
            text = _text_from_raw_message(message.get("raw_message"))
        if not text:
            continue
        for term in iter_terms(text[:max_message_chars]):
            counts[term] += 1

    filtered = [
        (term, count)
        for term, count in counts.items()
        if count >= max(1, min_count) and is_meaningful_term(term, extra_stopwords)
    ]
    filtered.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))

    # 先去掉「每次出现都被某个更长候选词完整包住」的碎片：
    # 「器人程」的每一次出现都落在「机器人程」里面，词频必然相等，
    # 留着只是把同一个词切成了几截。
    surviving = [
        (term, count)
        for term, count in filtered
        if not any(
            len(other) > len(term) and term in other and other_count >= count
            for other, other_count in filtered
        )
    ]

    # 再去掉和已保留词大面积重叠的相邻切片（「工智能聊」vs「智能聊天」这类
    # 互相都不是子串的窗口碎片）。
    kept: List[Tuple[str, int]] = []
    for term, count in surviving:
        if any(_overlaps_heavily(term, other) for other, _c in kept):
            continue
        kept.append((term, count))
        if len(kept) >= top_n:
            break
    return kept


def _overlaps_heavily(left: str, right: str) -> bool:
    """两个同长度词是否大面积重叠。

    滑动窗口切词会把同一句里的「智能聊天」切成「工智能聊」「智能聊天」
    「聊天机器」……它们互相都不是子串，光靠包含判断去不掉，同时保留只会
    让兴趣列表看起来像乱码。这里只处理**长度相同**的情况：一方有超过一半
    的连续片段出现在另一方里就算重复。跨长度不处理——「游戏本」和「单机游戏」
    这种共享两个字的词是真实存在的不同兴趣，不该被误杀。
    """

    if left == right:
        return True
    if len(left) != len(right) or len(left) < 3:
        return False
    size = len(left)
    for start in range(size):
        for end in range(start + 2, size + 1):
            piece = left[start:end]
            if len(piece) * 2 >= size and piece in right:
                return True
    return False


def _text_from_raw_message(raw_message: Any) -> str:
    """从 ``raw_message`` 组件列表里拼出纯文本（兜底）。"""

    if not isinstance(raw_message, list):
        return ""
    parts: List[str] = []
    for component in raw_message:
        if not isinstance(component, dict):
            continue
        if component.get("type") != "text":
            continue
        data = component.get("data")
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            parts.append(data["text"])
        elif isinstance(data, str):
            parts.append(data)
    return "".join(parts)


@dataclass
class InterestProfile:
    """兴趣画像：兴趣词 -> 权重。"""

    weights: Dict[str, float] = field(default_factory=dict)

    def add(self, term: str, weight: float) -> None:
        normalized = normalize_term(term)
        if not is_meaningful_term(normalized):
            return
        if weight <= 0:
            return
        current = self.weights.get(normalized, 0.0)
        if weight > current:
            self.weights[normalized] = float(weight)

    def extend(self, terms: Iterable[str], weight: float) -> None:
        for term in terms or []:
            self.add(term, weight)

    def __len__(self) -> int:
        return len(self.weights)

    def __bool__(self) -> bool:
        return bool(self.weights)

    def has(self, term: str) -> bool:
        """这个兴趣词是否已经在画像里（按归一化后的形式比较）。"""

        return normalize_term(term) in self.weights

    def sorted_terms(self) -> List[Tuple[str, float]]:
        return sorted(self.weights.items(), key=lambda item: (-item[1], item[0]))


def hot_score(video: Video) -> float:
    """热度分：播放/点赞/投币收藏/互动加权取对数，约 0~5。"""

    play = math.log10(1 + max(0, video.play))
    like = math.log10(1 + max(0, video.like))
    invest = math.log10(1 + max(0, video.coin) + max(0, video.favorite))
    interact = math.log10(1 + max(0, video.share) + max(0, video.reply) + max(0, video.danmaku))
    return 0.35 * play + 0.25 * like + 0.2 * invest + 0.1 * interact


def freshness_score(video: Video, *, now: Optional[float] = None, days: float = 7.0) -> float:
    """新鲜度分：发布 0 天得 1，超过 ``days`` 天得 0。"""

    now = time.time() if now is None else now
    if not video.pubdate:
        return 0.0
    age_days = max(0.0, (now - float(video.pubdate)) / 86400.0)
    if days <= 0:
        return 0.0
    return max(0.0, 1.0 - age_days / days)


def score_video(
    video: Video,
    profile: InterestProfile,
    *,
    now: Optional[float] = None,
    interest_weight: float = 1.0,
    title_match_boost: float = 2.0,
    hot_weight: float = 1.0,
    freshness_weight: float = 0.6,
    freshness_days: float = 7.0,
    use_tags: bool = True,
    tag_weight: float = 0.7,
) -> "ScoreResult":
    """给单个视频打分。

    同一个兴趣词只计一次，取命中位置里最高的字段权重：
    标题（``title_match_boost``）> 简介（0.8）> 标签（``tag_weight``）> 分区（0.6）> UP 名（0.4）。
    """

    title = (video.title or "").lower()
    description = (video.description or "").lower()
    partition = (video.partition or "").lower()
    owner = (video.owner or "").lower()
    tags = (getattr(video, "tags", "") or "").lower()

    interest = 0.0
    matched: List[str] = []
    for term, weight in profile.weights.items():
        field_weight = 0.0
        if term in title:
            field_weight = max(1.0, title_match_boost)
        elif term in description:
            field_weight = 0.8
        elif use_tags and tags and term in tags:
            field_weight = max(0.1, min(1.0, tag_weight))
        elif term in partition:
            field_weight = 0.6
        elif term in owner:
            field_weight = 0.4
        if field_weight <= 0:
            continue
        interest += weight * field_weight
        matched.append(term)

    interest_score = interest * max(0.0, interest_weight)
    hot = hot_score(video) * max(0.0, hot_weight)
    fresh = freshness_score(video, now=now, days=freshness_days) * max(0.0, freshness_weight)
    return ScoreResult(
        video=video,
        score=interest_score + hot + fresh,
        interest_score=interest_score,
        hot_score=hot,
        freshness_score=fresh,
        matched=sorted(matched),
    )


@dataclass
class ScoreResult:
    video: Video
    score: float
    interest_score: float = 0.0
    hot_score: float = 0.0
    freshness_score: float = 0.0
    matched: List[str] = field(default_factory=list)
    category: str = ""


def passes_filter(
    video: Video,
    *,
    min_duration: int = 0,
    max_duration: int = 10**9,
    min_play: int = 0,
    min_like: int = 0,
    block_keywords: Optional[Iterable[str]] = None,
    block_up_names: Optional[Iterable[str]] = None,
    exclude_partitions: Optional[Iterable[str]] = None,
) -> Tuple[bool, str]:
    """硬过滤；返回 ``(是否通过, 原因)``。"""

    if video.duration < max(0, min_duration):
        return False, f"时长过短({video.duration}s)"
    if video.duration > max_duration:
        return False, f"时长过长({video.duration}s)"
    if video.play < max(0, min_play):
        return False, f"播放量不足({video.play})"
    if video.like < max(0, min_like):
        return False, f"点赞不足({video.like})"

    haystack = " ".join((video.title, video.description, video.owner)).lower()
    for keyword in block_keywords or []:
        normalized = normalize_term(keyword)
        if normalized and normalized in haystack:
            return False, f"命中屏蔽词({keyword})"
    for name in block_up_names or []:
        normalized = normalize_term(name)
        if normalized and normalized in (video.owner or "").lower():
            return False, f"屏蔽UP({name})"
    partitions = {normalize_term(item) for item in (exclude_partitions or []) if normalize_term(item)}
    if partitions and normalize_term(video.partition) in partitions:
        return False, f"屏蔽分区({video.partition})"
    return True, ""


def rank_videos(
    videos: Sequence[Video],
    profile: InterestProfile,
    *,
    now: Optional[float] = None,
    min_score: float = 4.0,
    require_interest_match: bool = True,
    interest_weight: float = 1.0,
    title_match_boost: float = 2.0,
    hot_weight: float = 1.0,
    freshness_weight: float = 0.6,
    freshness_days: float = 7.0,
    min_duration: int = 0,
    max_duration: int = 10**9,
    min_play: int = 0,
    min_like: int = 0,
    block_keywords: Optional[Iterable[str]] = None,
    block_up_names: Optional[Iterable[str]] = None,
    exclude_partitions: Optional[Iterable[str]] = None,
    use_tags: bool = True,
    tag_weight: float = 0.7,
) -> List[ScoreResult]:
    """过滤 + 打分 + 排序，返回得分从高到低的候选。"""

    results: List[ScoreResult] = []
    for video in videos:
        passed, _reason = passes_filter(
            video,
            min_duration=min_duration,
            max_duration=max_duration,
            min_play=min_play,
            min_like=min_like,
            block_keywords=block_keywords,
            block_up_names=block_up_names,
            exclude_partitions=exclude_partitions,
        )
        if not passed:
            continue
        result = score_video(
            video,
            profile,
            now=now,
            interest_weight=interest_weight,
            title_match_boost=title_match_boost,
            hot_weight=hot_weight,
            freshness_weight=freshness_weight,
            freshness_days=freshness_days,
            use_tags=use_tags,
            tag_weight=tag_weight,
        )
        if require_interest_match and not result.matched:
            continue
        if result.score < min_score:
            continue
        results.append(result)

    results.sort(key=lambda item: (-item.score, item.video.bvid))
    return results
