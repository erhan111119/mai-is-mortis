"""可选的模型判定：关键词覆盖不到的软性条件交给模型判断。

一次判定只调一次模型：把所有带 ``llm_criteria`` 的人格列成编号清单，让模型只回编号。
判定跑在后台任务里，不参与消息处理的等待链路，也不影响回复延迟。
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

import re

JUDGE_INSTRUCTION = "只输出满足条件的编号，用英文逗号分隔；一个都不满足就输出 none。不要输出任何解释。"

_NUMBER_PATTERN = re.compile(r"\d+")


def build_judge_prompt(
    *,
    bot_name: str,
    criteria_list: Sequence[str],
    history: Sequence[Tuple[str, str]],
    latest_text: str,
) -> str:
    """构造判定提示词。

    Args:
        bot_name: 机器人名字，帮助模型分清谁是麦麦。
        criteria_list: 各人格的判定条件，顺序即编号顺序。
        history: 最近的对话，元素为 ``(发言人, 内容)``。
        latest_text: 本次要判断的消息内容。

    Returns:
        str: 判定提示词。
    """

    lines: List[str] = [
        f"你是判读聊天语气的分类器。这里聊天的机器人叫{bot_name or '麦麦'}。",
        "下面是最近的对话记录，请判断最后一条消息满足了下面哪些条件：",
    ]
    for index, criteria in enumerate(criteria_list, start=1):
        lines.append(f"{index}. {criteria}")
    lines.append("最近对话：")
    for speaker, content in history:
        lines.append(f"- {speaker or '某人'}：{content}")
    lines.append(f"最后一条消息：{latest_text}")
    lines.append(JUDGE_INSTRUCTION)
    return "\n".join(lines)


def parse_verdict(response: str, total: int) -> List[int]:
    """从模型回复里解析命中的编号（从 0 开始）。

    Args:
        response: 模型回复文本。
        total: 条件总数，用于过滤越界编号。

    Returns:
        List[int]: 命中条件的下标列表；无法解析或回答 none 时为空列表。
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
    latest_text: str,
    model_task: str,
    temperature: float,
    max_tokens: int,
) -> List[int]:
    """把一次判定交给模型，返回命中条件的下标列表。

    Args:
        ctx: 插件运行时上下文。
        bot_name: 机器人名字。
        criteria_list: 各人格的判定条件。
        history: 最近的对话。
        latest_text: 本次要判断的消息内容。
        model_task: 使用的模型任务名。
        temperature: 采样温度。
        max_tokens: 最大输出 token 数。

    Returns:
        List[int]: 命中条件的下标列表。

    Raises:
        RuntimeError: 模型调用失败或返回结构异常时抛出。
    """

    prompt = build_judge_prompt(
        bot_name=bot_name,
        criteria_list=criteria_list,
        history=history,
        latest_text=latest_text,
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
    return parse_verdict(response_text, len(criteria_list))
