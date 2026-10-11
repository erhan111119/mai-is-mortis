"""人格档案：把配置里的人格条目整理成运行时使用的只读结构。

配置里的字段是给 WebUI 编辑用的（可读、可写、带说明），运行时用的这一份只做取值、
补默认名、判断「这个人格能不能被自动切到」这类判断，避免各处重复写字段名。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

import logging

from .config import PersonaEntry


@dataclass(frozen=True)
class PersonaProfile:
    """一个可切换的人格。"""

    entry: PersonaEntry

    @property
    def persona_id(self) -> str:
        """人格 ID。"""

        return self.entry.id.strip()

    @property
    def name(self) -> str:
        """人格显示名，缺省回落到人格 ID。"""

        return self.entry.name.strip() or self.persona_id

    @property
    def criteria(self) -> str:
        """交给模型判断的判定条件。"""

        return self.entry.criteria.strip()

    @property
    def weight(self) -> float:
        """模型判定命中一次的加分。"""

        return max(0.0, float(self.entry.weight))

    @property
    def threshold(self) -> float:
        """切换阈值。"""

        return max(0.0, float(self.entry.threshold))

    @property
    def duration_minutes(self) -> float:
        """生效时长（分钟）。"""

        return max(0.1, float(self.entry.duration_minutes))

    @property
    def personality(self) -> str:
        """替换掉的人格设定文本。"""

        return self.entry.personality.strip()

    @property
    def reply_style(self) -> str:
        """替换掉的说话风格文本。"""

        return self.entry.reply_style.strip()

    @property
    def behavior_style(self) -> str:
        """替换掉的规划行为风格文本。"""

        return self.entry.behavior_style.strip()

    @property
    def extra_prompt(self) -> str:
        """追加到本次回复要求里的额外约束。"""

        return self.entry.extra_prompt.strip()

    @property
    def judged(self) -> bool:
        """该人格是否参与模型判定（有判定条件且加分非 0）。"""

        return bool(self.criteria) and self.weight > 0.0

    @property
    def auto_switchable(self) -> bool:
        """该人格是否参与自动切换（参与判定且阈值大于 0）。"""

        return self.judged and self.threshold > 0.0


def build_profiles(personas: Sequence[PersonaEntry], logger: logging.Logger) -> Dict[str, PersonaProfile]:
    """把配置中的人格列表编译成 ``人格 ID → PersonaProfile``。

    Args:
        personas: 配置里的人格条目。
        logger: 用于报告配置问题的日志器。

    Returns:
        Dict[str, PersonaProfile]: 编译结果；ID 为空或重复的条目会被跳过。
    """

    profiles: Dict[str, PersonaProfile] = {}
    for persona in personas:
        profile = PersonaProfile(entry=persona)
        if not profile.persona_id:
            logger.error("人格切换：配置里有条目没填「人格 ID」，该条目被跳过")
            continue
        if profile.persona_id in profiles:
            logger.error("人格切换：人格 ID %s 重复，后出现的条目被跳过", profile.persona_id)
            continue
        if not profile.judged:
            logger.warning(
                "人格切换：人格 %s 没有填「判定条件」或「命中加分」为 0，不会被自动切换（仍可用 /人格 切换 %s 手动切）",
                profile.name,
                profile.persona_id,
            )
        profiles[profile.persona_id] = profile
    return profiles


def judged_profiles(profiles: Dict[str, PersonaProfile]) -> List[PersonaProfile]:
    """返回参与模型判定的人格，顺序与配置一致。"""

    return [profile for profile in profiles.values() if profile.judged]
