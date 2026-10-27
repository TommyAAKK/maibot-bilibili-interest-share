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
