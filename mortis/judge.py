"""人格档案与模型判定。

* :class:`PersonaProfile` 把配置条目包一层，统一取值（去空白、缺省名、能否自动切换）；
* 判定是插件唯一的加分来源：一次调用把所有参与判定的人格列成编号清单，
  模型只回命中的编号（``1`` / ``1,2`` / ``none``），命中就按该人格的命中加分加分。

判定跑在后台任务里，不阻塞消息处理，也不影响回复延迟。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

import logging
import re

from .config import PersonaEntry

JUDGE_INSTRUCTION = "只输出满足条件的编号，用英文逗号分隔；一个都不满足就输出 none。不要输出任何解释。"

_NUMBER_PATTERN = re.compile(r"\d+")


@dataclass(frozen=True)
class PersonaProfile:
    """运行时使用的人格。"""

    entry: PersonaEntry

    @property
    def persona_id(self) -> str:
        return self.entry.id.strip()

    @property
    def name(self) -> str:
        """显示名，缺省回落到人格 ID。"""

        return self.entry.name.strip() or self.persona_id

    @property
    def criteria(self) -> str:
        return self.entry.criteria.strip()

    @property
    def weight(self) -> float:
        return max(0.0, float(self.entry.weight))

    @property
    def threshold(self) -> float:
        return max(0.0, float(self.entry.threshold))

    @property
    def duration_minutes(self) -> float:
        return max(0.1, float(self.entry.duration_minutes))

    @property
    def personality(self) -> str:
        return self.entry.personality.strip()

    @property
    def reply_style(self) -> str:
        return self.entry.reply_style.strip()

    @property
    def behavior_style(self) -> str:
        return self.entry.behavior_style.strip()

    @property
    def extra_prompt(self) -> str:
        return self.entry.extra_prompt.strip()

    @property
    def judged(self) -> bool:
        """是否参与模型判定（有判定条件且加分非 0）。"""

        return bool(self.criteria) and self.weight > 0.0

    @property
    def auto_switchable(self) -> bool:
        """是否参与自动切换（参与判定且阈值大于 0）。"""

        return self.judged and self.threshold > 0.0


@dataclass(frozen=True)
class JudgeOutcome:
    """一次判定的结果。"""

    matched_indexes: List[int]
    """命中的条件下标（从 0 开始）。"""

    raw_response: str
    """模型原始回复，供日志与排查使用。"""


def build_profiles(personas: Sequence[PersonaEntry], logger: logging.Logger) -> Dict[str, PersonaProfile]:
    """编译人格列表；ID 为空或重复的条目跳过，缺判定条件的条目只报警告。"""

    profiles: Dict[str, PersonaProfile] = {}
    for persona in personas:
        profile = PersonaProfile(entry=persona)
        if not profile.persona_id:
            logger.error("mai is mortis：配置里有条目没填「人格 ID」，该条目被跳过")
            continue
        if profile.persona_id in profiles:
            logger.error("mai is mortis：人格 ID %s 重复，后出现的条目被跳过", profile.persona_id)
            continue
        if not profile.judged:
            logger.warning(
                "mai is mortis：人格 %s 没有填「判定条件」或「命中加分」为 0，不会被自动切换（仍可 /人格 切换 %s）",
                profile.name,
                profile.persona_id,
            )
        profiles[profile.persona_id] = profile
    return profiles


def judged_profiles(profiles: Dict[str, PersonaProfile]) -> List[PersonaProfile]:
    """返回参与模型判定的人格，顺序与配置一致。"""

    return [profile for profile in profiles.values() if profile.judged]


def build_judge_prompt(
    *,
    bot_name: str,
    criteria_list: Sequence[str],
    history: Sequence[Tuple[str, str]],
    latest_is_mention: bool,
) -> str:
    """构造判定提示词：最近对话 + 编号条件清单，最后一条是最新消息。"""

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
    """从模型回复里解析命中的编号（从 0 开始）；回答 none / 解析不出时为空列表。"""

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
    """把一次判定交给模型。

    Raises:
        RuntimeError: 模型调用失败或返回空内容。
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
