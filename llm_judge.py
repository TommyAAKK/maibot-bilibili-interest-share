"""LLM 语义判断：视频是否真的贴合兴趣，并生成一段人设口吻的自然语言介绍。

两类调用：

- **文本判断**（默认 ``task_name="planner"``）：把标题/简介/标签/弹幕/评论/字幕/
  画面描述一起喂给模型，让它判断相关性并输出结构化 JSON。
- **识图**（默认 ``task_name="vlm"``）：把抽出来的帧作为图片发给支持视觉的模型，
  让它描述画面内容。planner 这类纯文本模型通常不支持图片，所以要分开配置。

模型都通过 ``ctx.llm.generate`` 调用，模型名由 ``model_config.toml`` 里的任务
配置决定，插件只指定任务名。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("plugin.bilibili_interest_share.llm")

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型回复里抠出第一个 JSON 对象。"""

    if not isinstance(text, str) or not text.strip():
        return None
    candidates: List[str] = []
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.append(match.group(1))
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


_JSON_ARRAY_RE = re.compile(r"\[[^\[\]]*\]", re.S)


def _parse_term_list(text: str, limit: int) -> List[str]:
    """从模型回复里抠出一个字符串数组（兴趣词）。

    容忍 ```json 代码块、前后废话、以及模型把数组包在对象里的情况。
    """

    if not isinstance(text, str) or not text.strip():
        return []
    candidates: List[str] = []
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.append(match.group(1))
    candidates.append(text)
    array_match = _JSON_ARRAY_RE.search(text)
    if array_match:
        candidates.append(array_match.group(0))

    for candidate in candidates:
        raw = candidate.strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            for key in ("interests", "terms", "keywords", "list", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    data = value
                    break
            else:
                continue
        if not isinstance(data, list):
            continue
        terms = _clean_terms(data, limit)
        if terms:
            return terms
    # 模型偶尔不按 JSON 输出（直接写「1. 游戏」或把词用引号括起来），
    # 严格的 JSON 解析会全部失败——这里再兜一层，尽量捞回能用的词。
    return _scavenge_terms(text, limit)


_QUOTED_TERM_RE = re.compile(r"[“\"']([^“”\"'\n]{1,20})[”\"']")
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•·]|\(?\d{1,2}[.)、]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")
_TERM_NOISE_RE = re.compile(r"[，。！？；：、（）()\[\]{}<>《》/\\|=+*#$%^&@~`]")
_NOT_A_TERM = {
    "json",
    "json数组",
    "数组",
    "兴趣",
    "兴趣方向",
    "推荐",
    "解释",
    "说明",
    "输出",
    "例如",
    "比如",
}


def _clean_terms(items: Any, limit: int) -> List[str]:
    """把候选列表清洗成兴趣词（去重、限长、丢弃明显不是词的项）。"""

    if not isinstance(items, (list, tuple)):
        return []
    terms: List[str] = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("term") or item.get("name") or item.get("interest") or ""
        term = str(item or "").strip().strip("\"'，,、 ")
        if not term or len(term) > 20 or term in seen:
            continue
        if term.lower() in _NOT_A_TERM:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= max(1, int(limit)):
            break
    return terms


def _scavenge_terms(text: str, limit: int) -> List[str]:
    """JSON 解析失败时，从自由文本里尽量捞出兴趣词。"""

    if not isinstance(text, str) or not text.strip():
        return []
    # 有左方括号说明模型确实在输出数组，只是可能被截断（缺右括号），
    # 这种就按引号把已经写出来的词捞回来。
    if "[" in text:
        found = [match.group(1) for match in _QUOTED_TERM_RE.finditer(text)]
        if not found:
            found = re.findall(r'"([^"\n]{1,20})"', text)
        return _usable_terms(found, limit)
    # 只有花括号说明是对象（比如把判断结果当成兴趣列表返回了），别把字段名当兴趣词。
    if "{" in text:
        return []
    found = [match.group(1) for match in _QUOTED_TERM_RE.finditer(text)]
    if not found:
        # 没有引号就按列表项切，常见形态是「1. 游戏」「- 原神」「游戏、原神」；
        # 整句散文（带句号/问号/感叹号）直接跳过，免得捞出半句话。
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _BULLET_PREFIX_RE.match(stripped):
                rest = _BULLET_PREFIX_RE.sub("", stripped)
                found.extend(re.split(r"[、,，;；]", rest))
                continue
            if any(mark in stripped for mark in "。！？"):
                continue
            found.extend(re.split(r"[、,，;；]", stripped))
    return _usable_terms(found, limit)


def _usable_terms(items: Any, limit: int) -> List[str]:
    """清洗兜底捞出来的词：去掉标点/数字/太短的碎片。"""

    if not isinstance(items, (list, tuple)):
        items = list(items or [])
    usable = [
        item
        for item in _clean_terms(items, len(items))
        if not _TERM_NOISE_RE.search(item) and not item.isdigit() and len(item) >= 2
    ]
    return usable[: max(1, int(limit))]


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def build_judge_prompt(
    video: Any,
    *,
    interests: Sequence[str],
    danmaku: Sequence[str],
    comments: Sequence[str],
    subtitle: str = "",
    frames_desc: str = "",
    narrative_max_chars: int = 160,
    persona_block: str = "",
    intro_enabled: bool = False,
    intro_max_chars: int = 40,
    casual_mode: bool = False,
    casual_max_chars: int = 60,
) -> str:
    """拼出文本判断的 prompt。

    ``casual_mode`` 打开时，让模型输出的是一句「刚好刷到、随口提一句」的话（仍放在
    ``narrative`` 字段里），不要推荐口吻、不要宣布分享意图；关闭时输出一段自然语言介绍。
    """

    lines: List[str] = []
    if persona_block:
        lines.append(persona_block)
        lines.append("")
        lines.append(
            "下面请你完全以这个角色的身份和口吻说话：用第一人称、语气符合上面的性格设定，"
            "像平时在群里聊天那样自然，不要写成新闻播报或产品文案。"
        )
        lines.append("")
    lines.append("你是 B 站内容筛选助手。下面是一个候选视频的资料，以及某个聊天群里用户们的兴趣。")
    if casual_mode:
        lines.append("请判断这个视频值不值得发到这个群里（要求：内容新鲜有趣、贴合兴趣，不是主流官方宣传物料），")
        lines.append(
            "如果值得，请像平时在群里随口提一句那样，说说它哪里有意思——"
            "不是「我要给大家推荐」，而是刚好刷到、顺嘴聊两句。"
        )
    else:
        lines.append("请判断这个视频是否值得分享给这个群（要求：内容新鲜有趣、贴合兴趣，不是主流官方宣传物料），")
        lines.append("如果值得，请用你自己的口吻自然地讲讲它讲了什么、哪里有意思。")
    lines.append("")
    lines.append("【视频资料】")
    tags = getattr(video, "tags", "") or ""
    if tags:
        lines.append(f"标签：{_clip(tags, 120)}")
    description = getattr(video, "description", "") or ""
    if description:
        lines.append(f"简介：{_clip(description, 220)}")
    if frames_desc:
        lines.append(f"画面（抽帧识别）：{_clip(frames_desc, 300)}")
    if subtitle:
        lines.append(f"字幕节选：{_clip(subtitle, 400)}")
    if danmaku:
        lines.append("弹幕节选：" + " / ".join(_clip(item, 40) for item in danmaku[:20]))
    if comments:
        lines.append("热门评论：" + " / ".join(_clip(item, 60) for item in comments[:8]))
    lines.append("")
    lines.append("【群里用户的兴趣】" + ("、".join(interests) if interests else "（未提供）"))
    lines.append("")
    lines.append("只输出一个 JSON 对象，不要输出其它文字，格式：")
    json_fields = [
        '"relevant": true 或 false',
        '"score": 0 到 10 的数字',
        '"category": "新鲜资讯" 或 "社区创作" 或 "玩家投稿" 或 "外网搬运" 或 "其他"',
    ]
    if casual_mode:
        json_fields.append(
            f'"narrative": "像在群里随口提一句那样说这视频哪里有意思，一到两句话，'
            f"不超过 {casual_max_chars} 字；可以带点吐槽、情绪或疑问，像刚刷到顺手说一句；"
            "不要用「推荐」「分享」「安利」「值得一看」「大家快看」这类词，"
            '不要说「给大家分享一个」「推荐给你们」，不要分点、不要编号、不要加前缀"'
        )
    else:
        json_fields.append(
            f'"narrative": "用你的口吻自然地说说这个视频讲了什么、有什么好看的，一到三句话，'
            f"像在群里跟朋友随口安利；不要分点、不要编号、不要加「内容：」「看点：」这类前缀，"
            f'不超过 {narrative_max_chars} 字"'
        )
    if intro_enabled and not casual_mode:
        json_fields.insert(
            0,
            f'"intro": "一句不超过 {intro_max_chars} 字的推荐语，用你的口吻招呼群友来看，'
            '可以说说为什么觉得有意思"',
        )
    json_fields.append('"reason": "一句话说明判断理由"')
    lines.append("{" + ", ".join(json_fields) + "}")
    lines.append(
        "如果视频主要是官方预告/PV/发布会/带货，或与兴趣明显无关，请把 relevant 设为 false。"
    )
    if persona_block:
        if casual_mode:
            lines.append(
                "记住：narrative 要用上面人设的口吻来写，像平时在群里随口说话那样自然"
                "（不要出现「作为AI」之类的自我说明）。"
            )
        else:
            lines.append(
                "记住：narrative 和 intro 都要用上面人设的口吻来写，像平时聊天那样自然"
                "（不要出现「作为AI」之类的自我说明）。"
            )
    return "\n".join(lines)


class LlmJudge:
    """封装 ``ctx.llm``，提供判断与识图。"""

    def __init__(
        self,
        llm: Any,
        *,
        task_name: str = "planner",
        vision_task_name: str = "vlm",
        temperature: float = 0.3,
        persona_provider: Any = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._llm = llm
        self.task_name = (task_name or "planner").strip() or "planner"
        self.vision_task_name = (vision_task_name or "vlm").strip() or "vlm"
        self.temperature = float(temperature)
        self._persona_provider = persona_provider
        self._logger = logger_ or logger

    @property
    def available(self) -> bool:
        return self._llm is not None

    def persona_block(self) -> str:
        """取当前人设段落（没人设或关闭时返回空串）。"""

        provider = self._persona_provider
        if provider is None:
            return ""
        try:
            return str(provider.prompt_block() or "")
        except Exception as exc:  # noqa: BLE001 - 人设读取失败不应影响分享
            self._logger.debug("读取人设失败：%s", exc)
            return ""

    async def _generate(
        self,
        prompt: Any,
        *,
        task_name: str,
        max_tokens: int = 800,
    ) -> str:
        """调用宿主 LLM，返回文本；失败返回空字符串。"""

        if self._llm is None:
            return ""
        try:
            result = await self._llm.generate(
                prompt=prompt,
                task_name=task_name,
                temperature=self.temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - LLM 失败不应中断分享流程
            self._logger.warning("调用 LLM（%s）失败：%s", task_name, exc)
            return ""
        if isinstance(result, dict):
            if result.get("success") is False:
                self._logger.warning("LLM（%s）返回失败：%s", task_name, result.get("error") or result)
                return ""
            text = result.get("response") or result.get("text") or ""
            return text if isinstance(text, str) else str(text)
        return str(result or "")

    async def seed_interests(
        self,
        *,
        persona_block: str = "",
        count: int = 8,
    ) -> List[str]:
        """还没有任何兴趣时，按人设生成一份初始兴趣词。

        只在「聊天流既没有手写兴趣、近期聊天也提不出关键词」时使用，
        目的是让新群第一次刷 B 站时有点方向，而不是完全随机。

        返回归一化后的兴趣词列表；模型不可用或解析失败时返回空列表。
        """

        if self._llm is None:
            return []
        count = max(3, min(20, int(count)))
        block = (persona_block or "").strip()
        lines = [
            f"根据下面的机器人人设，给出 {count} 个它适合去 B 站刷、去分享的兴趣方向。",
            "",
        ]
        if block:
            lines.extend([block, ""])
        lines.extend(
            [
                "要求：",
                f"- 只输出一个 JSON 数组，共 {count} 个字符串；",
                "- 不要开场白、不要解释、不要编号、不要代码块标记；",
                "- 每个词 2~6 个字，是 B 站上真实存在的内容方向"
                "（如 游戏、原神、编程、机械键盘、猫咪、搞笑整活）；",
                "- 不要写太宽泛的词（如 视频、内容、娱乐），也不要写人名。",
                "",
                "直接输出数组，例如：[\"游戏\", \"原神\"]",
            ]
        )
        prompt = [{"role": "user", "content": "\n".join(lines)}]
        # 给足 token：模型有时会先写一句解释再输出数组，300 容易被截断
        # （线上实测提示词 867 tok、输出 412 tok 就把数组吃掉了）。
        text = await self._generate(prompt, task_name=self.task_name, max_tokens=800)
        terms = _parse_term_list(text, count)
        if not terms and text.strip():
            self._logger.info(
                "人设初始兴趣解析失败，模型原始输出：%s", _clip(text, 200)
            )
        return terms

    async def describe_frames(self, video: Any, frames: Sequence[str]) -> str:
        """让视觉模型描述抽到的帧。"""

        if not frames:
            return ""
        content: List[Dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "这是一个 B 站视频的抽帧画面。请用 60~120 字客观描述：画面里在做什么、"
                    "是什么类型的视频（游戏/生活/搞笑/资讯/剪辑等）、画面风格，以及画面是否与标题相符。"
                    f"\n标题：{getattr(video, 'title', '')}"
                    f"\n分区：{getattr(video, 'partition', '')}"
                    "\n只输出描述本身，不要客套话。"
                ),
            }
        ]
        for data_url in frames:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        text = await self._generate(
            [{"role": "user", "content": content}],
            task_name=self.vision_task_name,
            max_tokens=400,
        )
        return _clip(text, 300)

    async def judge(
        self,
        video: Any,
        *,
        interests: Sequence[str],
        danmaku: Sequence[str] = (),
        comments: Sequence[str] = (),
        subtitle: str = "",
        frames_desc: str = "",
        narrative_max_chars: int = 160,
        intro_enabled: bool = False,
        intro_max_chars: int = 40,
        casual_mode: bool = False,
        casual_max_chars: int = 60,
    ) -> Optional[Dict[str, Any]]:
        """判断视频是否值得分享，并生成分享文案。

        Returns:
            ``{"relevant", "score", "category", "narrative", "intro", "reason"}``；
            「随口一提」模式下 ``narrative`` 就是那句随口说的话，``intro`` 恒为空。
            模型不可用或解析失败时返回 ``None``（调用方应回退到纯规则打分）。
        """

        persona_block = self.persona_block()
        prompt = build_judge_prompt(
            video,
            interests=interests,
            danmaku=danmaku,
            comments=comments,
            subtitle=subtitle,
            frames_desc=frames_desc,
            narrative_max_chars=narrative_max_chars,
            persona_block=persona_block,
            intro_enabled=intro_enabled,
            intro_max_chars=intro_max_chars,
            casual_mode=casual_mode,
            casual_max_chars=casual_max_chars,
        )
        text = await self._generate(prompt, task_name=self.task_name, max_tokens=900)
        data = _extract_json(text)
        if not isinstance(data, dict):
            if text:
                self._logger.debug("LLM 判断结果无法解析为 JSON：%s", _clip(text, 200))
            return None

        relevant = bool(data.get("relevant"))
        try:
            score = float(data.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        narrative_limit = casual_max_chars if casual_mode else narrative_max_chars
        narrative = _clip(str(data.get("narrative") or ""), narrative_limit)
        if not narrative:
            # 兼容旧格式回复：只有 summary/points 时，用 summary 兜底成一段叙述
            legacy_summary = str(data.get("summary") or "").strip()
            if legacy_summary:
                narrative = _clip(legacy_summary, narrative_limit)
        intro = (
            _clip(str(data.get("intro") or ""), intro_max_chars)
            if intro_enabled and not casual_mode
            else ""
        )
        return {
            "relevant": relevant,
            "score": score,
            "category": _clip(str(data.get("category") or ""), 20),
            "narrative": narrative,
            "intro": intro,
            "reason": _clip(str(data.get("reason") or ""), 120),
        }
