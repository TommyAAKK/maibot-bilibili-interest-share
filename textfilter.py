"""弹幕 / 评论的清洗与「AI 鱼灾」过滤。

B 站上有一类明显的机器生成内容：模板化夸赞、无意义字符堆、成批重复、
带「AI 生成」自述的长评论等。这类内容会把 LLM 的判断带偏，因此在送进
模型之前先做确定性过滤。

所有过滤都是纯函数，便于单测：

- :func:`is_ai_spam`：单条文本是否像 AI 鱼灾
- :func:`filter_texts`：整批过滤 + 去重 + 截断
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

from .store import _char_grams, jaccard

# 明确自述由 AI 生成的标记
_AI_MARKERS = (
    "ai生成", "ai 生成", "由ai", "由 ai", "人工智能生成", "chatgpt", "gpt-4", "gpt4",
    "本评论由", "作为ai", "作为 ai", "语言模型", "自动生成", "机器生成",
)

# 典型模板化夸赞（整条完全一致时丢弃）
_TEMPLATE_PHRASES = {
    "感谢分享", "谢谢分享", "学到了", "很有帮助", "内容丰富", "期待更新", "支持一下",
    "路过", "打卡", "前排", "第一", "来了", "顶", "赞", "收藏了", "已三连",
    "感谢up", "感谢up主", "辛苦了", "太棒了", "不错不错", "很好看", "做得好",
    "受益匪浅", "干货满满", "值得一看", "推荐给大家", "希望能帮到你",
}

# 允许的常见符号（表情/标点），用于判断「纯符号无信息」
_MEANINGFUL_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2b00-\u2bff\ufe0f\u200d]"
)
_REPEAT_CHAR_RE = re.compile(r"(.)\1{5,}")


def normalize_text(text: str) -> str:
    """去掉首尾空白与零宽字符，压缩连续空白。"""

    if not isinstance(text, str):
        return ""
    cleaned = text.replace("\u200b", "").replace("\ufeff", "").strip()
    return re.sub(r"\s+", " ", cleaned)


def is_ai_spam(text: str, *, max_length: int = 200) -> bool:
    """判断一条弹幕/评论是否属于 AI 鱼灾类垃圾内容。"""

    normalized = normalize_text(text)
    if not normalized:
        return True
    lowered = normalized.lower()
    if any(marker in lowered for marker in _AI_MARKERS):
        return True
    if normalized in _TEMPLATE_PHRASES:
        return True
    # 「感谢分享，学到了」这类由多个模板短语拼起来的复读式夸赞
    segments = [
        segment.strip()
        for segment in re.split(r"[，,、。.！!？?~～\s]+", normalized)
        if segment.strip()
    ]
    if segments and all(segment in _TEMPLATE_PHRASES for segment in segments):
        return True
    if not _MEANINGFUL_RE.search(normalized):
        return True  # 纯符号/纯表情
    if len(normalized) > max_length:
        return True  # 异常长的一段话，弹幕/短评里几乎都是灌水
    # 单字/单符号重复（如「哈哈哈哈哈哈」之外的「--------」）
    core = _EMOJI_RE.sub("", normalized)
    if core and not _MEANINGFUL_RE.search(core):
        return True
    if _REPEAT_CHAR_RE.search(normalized) and len(set(normalized)) <= 3:
        return True
    return False


def filter_texts(
    items: Sequence[Tuple[str, float]],
    *,
    limit: int = 40,
    filter_ai: bool = True,
    max_repeat: int = 2,
    similarity_threshold: float = 0.9,
    max_length: int = 200,
) -> List[str]:
    """过滤一批 ``(文本, 权重)``，返回干净且不重复的文本列表。

    Args:
        items: ``(文本, 权重)``；权重用于排序（弹幕可用出现时间，评论可用点赞数）。
        limit: 最多返回多少条。
        filter_ai: 是否过滤 AI 鱼灾。
        max_repeat: 同一文本最多保留几次（弹幕复读很常见，保留少量即可）。
        similarity_threshold: 与已保留文本的字符二元组 Jaccard 超过该值即视为重复。
        max_length: 单条最大长度，超长直接丢弃。

    Returns:
        过滤后的文本列表（保持输入顺序）。
    """

    kept: List[str] = []
    kept_grams: List[set] = []
    repeat_counter: dict = {}

    for text, _weight in items or []:
        normalized = normalize_text(text)
        if not normalized or len(normalized) > max_length:
            continue
        if filter_ai and is_ai_spam(normalized, max_length=max_length):
            continue
        repeat_counter[normalized] = repeat_counter.get(normalized, 0) + 1
        if repeat_counter[normalized] > max(1, max_repeat):
            continue
        grams = _char_grams(normalized)
        if len(normalized) >= 8 and any(
            jaccard(grams, previous) >= similarity_threshold for previous in kept_grams
        ):
            continue
        kept.append(normalized)
        kept_grams.append(grams)
        if len(kept) >= max(1, int(limit)):
            break
    return kept


def filter_danmaku(
    danmaku: Iterable[object],
    *,
    limit: int = 40,
    filter_ai: bool = True,
) -> List[str]:
    """过滤 :class:`bilibili.Danmaku` 列表（按出现顺序）。"""

    items: List[Tuple[str, float]] = []
    for item in danmaku or []:
        text = getattr(item, "text", "")
        progress = float(getattr(item, "progress_ms", 0) or 0)
        items.append((text, progress))
    return filter_texts(items, limit=limit, filter_ai=filter_ai)


def filter_comments(
    comments: Iterable[object],
    *,
    limit: int = 10,
    filter_ai: bool = True,
) -> List[str]:
    """过滤 :class:`bilibili.Comment` 列表（按点赞数降序）。"""

    items: List[Tuple[str, float]] = []
    for item in comments or []:
        text = getattr(item, "message", "")
        like = float(getattr(item, "like", 0) or 0)
        items.append((text, -like))  # 权重取负，排序时点赞多的在前
    ordered = sorted(items, key=lambda pair: pair[1])
    return filter_texts(ordered, limit=limit, filter_ai=filter_ai)
