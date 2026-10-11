"""触发规则：把配置里的关键词/正则编译成可复用的匹配器。

编译在配置变更时做一次，每条消息只做「包含」或「正则搜索」，不重复解析配置字符串。
写错的正则不会静默吞掉：会在日志里报 ERROR 并跳过该行，其余规则照常工作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Sequence, Tuple

import logging
import re

from .config import PersonaEntry, TriggerRule


@dataclass(frozen=True)
class CompiledTrigger:
    """一条编译后的触发规则。"""

    key: str
    """冷却键：``<人格 ID>#<规则序号>``。"""

    weight: float
    require_mention: bool
    cooldown_seconds: float
    keywords: Tuple[str, ...]
    regexes: Tuple["re.Pattern[str]", ...]
    note: str

    def matched(self, text: str, *, is_mention: bool) -> bool:
        """判断本条规则是否命中给定消息。"""

        if self.require_mention and not is_mention:
            return False
        for keyword in self.keywords:
            if keyword in text:
                return True
        for regex in self.regexes:
            if regex.search(text):
                return True
        return False


@dataclass(frozen=True)
class CompiledPersona:
    """一个编译后的人格，包含分数阈值、生效时长与全部触发规则。"""

    entry: PersonaEntry
    triggers: Tuple[CompiledTrigger, ...]

    @property
    def persona_id(self) -> str:
        """人格 ID。"""

        return self.entry.id.strip()

    @property
    def name(self) -> str:
        """人格显示名，缺省回落到人格 ID。"""

        return self.entry.name.strip() or self.persona_id

    @property
    def threshold(self) -> float:
        """切换阈值。"""

        return max(0.0, float(self.entry.threshold))

    @property
    def duration_minutes(self) -> float:
        """生效时长（分钟）。"""

        return max(0.1, float(self.entry.duration_minutes))

    @property
    def max_gain_per_message(self) -> float:
        """单条消息的加分上限，0 表示不限。"""

        return max(0.0, float(self.entry.max_gain_per_message))

    @property
    def cooldown_seconds(self) -> float:
        """人格级加分冷却。"""

        return max(0.0, float(self.entry.cooldown_seconds))

    @property
    def llm_criteria(self) -> str:
        """交给模型判断的软性条件。"""

        return self.entry.llm_criteria.strip()

    @property
    def llm_weight(self) -> float:
        """模型判定命中时的加分。"""

        return max(0.0, float(self.entry.llm_weight))

    @property
    def auto_switchable(self) -> bool:
        """该人格是否参与自动切换（阈值大于 0 才算）。"""

        return self.threshold > 0.0


@dataclass(frozen=True)
class TriggerHit:
    """一次规则命中。"""

    persona_id: str
    rule_key: str
    weight: float
    cooldown_seconds: float
    note: str


def split_patterns(patterns: str) -> list[str]:
    """按行拆出触发内容，忽略空行与行首行尾空白。"""

    return [line.strip() for line in patterns.splitlines() if line.strip()]


def compile_personas(
    personas: Sequence[PersonaEntry], logger: logging.Logger
) -> Dict[str, CompiledPersona]:
    """把配置中的人格列表编译成 ``人格 ID → CompiledPersona``。

    Args:
        personas: 配置里的人格条目。
        logger: 用于报告重复 ID、非法正则等配置问题的日志器。

    Returns:
        Dict[str, CompiledPersona]: 编译结果；ID 为空或重复的条目会被跳过。
    """

    compiled: Dict[str, CompiledPersona] = {}
    for persona in personas:
        persona_id = persona.id.strip()
        if not persona_id:
            logger.error("人格切换：配置里有条目没填「人格 ID」，该条目被跳过")
            continue
        if persona_id in compiled:
            logger.error("人格切换：人格 ID %s 重复，后出现的条目被跳过", persona_id)
            continue
        compiled[persona_id] = CompiledPersona(
            entry=persona,
            triggers=_compile_triggers(persona_id, persona.triggers, logger),
        )
    return compiled


def _compile_triggers(
    persona_id: str, triggers: Sequence[TriggerRule], logger: logging.Logger
) -> Tuple[CompiledTrigger, ...]:
    """编译单个人格的触发规则。"""

    compiled_triggers: list[CompiledTrigger] = []
    for index, trigger in enumerate(triggers):
        patterns = split_patterns(trigger.patterns)
        if not patterns:
            continue
        keywords: list[str] = []
        regexes: list["re.Pattern[str]"] = []
        if trigger.match_mode == "regex":
            for pattern in patterns:
                try:
                    regexes.append(re.compile(pattern, re.IGNORECASE))
                except re.error as exc:
                    logger.error(
                        "人格切换：人格 %s 第 %d 条规则的正文 %r 不是合法正则（%s），该行被跳过",
                        persona_id,
                        index + 1,
                        pattern,
                        exc,
                    )
        else:
            keywords = [pattern.casefold() for pattern in patterns]
        if not keywords and not regexes:
            continue
        compiled_triggers.append(
            CompiledTrigger(
                key=f"{persona_id}#{index}",
                weight=max(0.0, float(trigger.weight)),
                require_mention=bool(trigger.require_mention),
                cooldown_seconds=max(0.0, float(trigger.cooldown_seconds)),
                keywords=tuple(keywords),
                regexes=tuple(regexes),
                note=trigger.note.strip(),
            )
        )
    return tuple(compiled_triggers)


def match_message(
    personas: Mapping[str, CompiledPersona], text: str, *, is_mention: bool
) -> list[TriggerHit]:
    """返回这条消息命中的人格规则。

    冷却判定不在这里做，交给账本统一处理。

    Args:
        personas: 已编译的人格表。
        text: 消息正文。
        is_mention: 本条消息是否 @ 了麦麦。

    Returns:
        list[TriggerHit]: 命中的规则列表。
    """

    normalized_text = text.casefold()
    hits: list[TriggerHit] = []
    for persona in personas.values():
        for trigger in persona.triggers:
            if trigger.weight <= 0.0:
                continue
            if not trigger.matched(normalized_text, is_mention=is_mention):
                continue
            hits.append(
                TriggerHit(
                    persona_id=persona.persona_id,
                    rule_key=trigger.key,
                    weight=trigger.weight,
                    cooldown_seconds=trigger.cooldown_seconds,
                    note=trigger.note,
                )
            )
    return hits
