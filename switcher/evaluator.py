"""模型判定：由模型决定「刚才这几句话里有没有发生够分的事」。

插件不用关键词/正则猜意图，而是把最近的对话交给模型：把所有参与判定的人格列成编号
清单，模型只回命中的编号（一次调用判完所有条件）。判定跑在后台任务里，不阻塞消息处理，
也不影响回复延迟。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

import re

JUDGE_INSTRUCTION = "只输出满足条件的编号，用英文逗号分隔；一个都不满足就输出 none。不要输出任何解释。"

_NUMBER_PATTERN = re.compile(r"\d+")


@dataclass(frozen=True)
class JudgeOutcome:
    """一次判定的结果。"""

    matched_indexes: List[int]
    """命中的条件下标（从 0 开始）。"""

    raw_response: str
    """模型原始回复，供日志与排查使用。"""


def build_judge_prompt(
    *,
    bot_name: str,
    criteria_list: Sequence[str],
    history: Sequence[Tuple[str, str]],
    latest_is_mention: bool,
) -> str:
    """构造判定提示词。

    Args:
        bot_name: 机器人名字，帮助模型分清谁是麦麦。
        criteria_list: 各人格的判定条件，顺序即编号顺序。
        history: 最近的对话，元素为 ``(发言人, 内容)``，最后一条是最新消息。
        latest_is_mention: 最新消息是否 @ 了麦麦。

    Returns:
        str: 判定提示词。
    """

    name = bot_name or "麦麦"
    lines: List[str] = [
        f"你是判读聊天语气的分类器。这里聊天的机器人叫{name}。",
        "下面是最近的聊天记录（越靠后越新）：",
    ]
    for speaker, content in history:
        lines.append(f"- {speaker or '某人'}：{content}")
    lines.append(f"（最后一条消息{'@ 了' if latest_is_mention else '没有 @'}{name}）")
    lines.append("请判断最后一条消息满足了下面哪些条件：")
    for index, criteria in enumerate(criteria_list, start=1):
        lines.append(f"{index}. {criteria}")
    lines.append(JUDGE_INSTRUCTION)
    return "\n".join(lines)


def parse_verdict(response: str, total: int) -> List[int]:
    """从模型回复里解析命中的编号（从 0 开始）。

    Args:
        response: 模型回复文本。
        total: 条件总数，用于过滤越界编号。

    Returns:
        List[int]: 命中条件的下标列表；回答 none / 无法解析时为空列表。
    """

    normalized = (response or "").strip().lower()
    if not normalized or "none" in normalized or "没有" in normalized or "不满足" in normalized:
        return []
    matched: List[int] = []
    for match in _NUMBER_PATTERN.findall(normalized):
        index = int(match) - 1
        if 0 <= index < total and index not in matched:
            matched.append(index)
    return matched


async def judge_message(
    ctx: Any,
    *,
    bot_name: str,
    criteria_list: Sequence[str],
    history: Sequence[Tuple[str, str]],
    latest_is_mention: bool,
    model_task: str,
    temperature: float,
    max_tokens: int,
) -> JudgeOutcome:
    """把一次判定交给模型，返回命中的条件下标。

    Args:
        ctx: 插件运行时上下文。
        bot_name: 机器人名字。
        criteria_list: 各人格的判定条件。
        history: 最近的对话，最后一条是最新消息。
        latest_is_mention: 最新消息是否 @ 了麦麦。
        model_task: 使用的模型任务名。
        temperature: 采样温度。
        max_tokens: 最大输出 token 数。

    Returns:
        JudgeOutcome: 命中下标与模型原始回复。

    Raises:
        RuntimeError: 模型调用失败或返回空内容时抛出。
    """

    prompt = build_judge_prompt(
        bot_name=bot_name,
        criteria_list=criteria_list,
        history=history,
        latest_is_mention=latest_is_mention,
    )
    result = await ctx.llm.generate(
        prompt,
        model="",
        task_name=model_task.strip() or None,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not isinstance(result, dict) or not result.get("success"):
        error = result.get("error") if isinstance(result, dict) else result
        raise RuntimeError(f"模型判定调用失败: {error}")
    response_text = str(result.get("response") or "").strip()
    if not response_text:
        raise RuntimeError("模型判定返回了空内容")
    return JudgeOutcome(
        matched_indexes=parse_verdict(response_text, len(criteria_list)),
        raw_response=response_text,
    )
