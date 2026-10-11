"""把系统提示里的人设段落换成当前生效的临时人格。

插件不改写宿主配置，只在「这一次请求」里做字符串替换：原人格文本从宿主配置读出来
（``personality.personality`` / ``personality.reply_style`` / ``personality.behavior_style``），
在系统提示里定位到同样的一段文字，替换成临时人格的同名字段。

这样临时人格到期后不需要「恢复」动作——下一次请求读到的仍然是宿主配置的原文；
宿主配置热重载也会立刻反映到下一次回复。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import time

BASE_TEXT_TTL_SECONDS = 30.0
"""原人格文本的缓存时间：避免每条消息都往宿主发一次配置读取 RPC。"""

OVERRIDE_TAG = "【临时人格】"
"""找不到原人格段落时，覆盖块用的标题前缀。"""


@dataclass
class BasePersonaTexts:
    """宿主配置里的原人格文本，用来在系统提示里定位要替换的段落。"""

    personality: str = ""
    reply_style: str = ""
    behavior_style: str = ""
    bot_name: str = ""
    fetched_at: float = 0.0

    def expired(self, now: float, ttl: float = BASE_TEXT_TTL_SECONDS) -> bool:
        """判断缓存是否已经过期。"""

        return (now - self.fetched_at) >= ttl


class BaseTextCache:
    """宿主原人格文本的缓存，配置热重载或 TTL 到期后重新读取。"""

    def __init__(self) -> None:
        """初始化空缓存。"""

        self._texts = BasePersonaTexts()

    @property
    def texts(self) -> BasePersonaTexts:
        """返回当前缓存内容。"""

        return self._texts

    async def get(self, ctx: Any, *, now: Optional[float] = None, force: bool = False) -> BasePersonaTexts:
        """读取原人格文本，必要时重新向宿主查询。

        Args:
            ctx: 插件运行时上下文。
            now: 当前时间戳，默认取系统时间。
            force: 是否强制重新读取。

        Returns:
            BasePersonaTexts: 原人格文本。
        """

        current_time = time.time() if now is None else now
        if not force and not self._texts.expired(current_time):
            return self._texts
        fetched = BasePersonaTexts(fetched_at=current_time)
        try:
            fetched.personality = _as_text(await ctx.config.get("personality.personality", ""))
            fetched.reply_style = _as_text(await ctx.config.get("personality.reply_style", ""))
            fetched.behavior_style = _as_text(await ctx.config.get("personality.behavior_style", ""))
            fetched.bot_name = _as_text(await ctx.config.get("bot.nickname", ""))
        except Exception as exc:
            ctx.logger.warning(f"读取宿主人设配置失败，本次沿用上一次的结果: {exc}")
            self._texts.fetched_at = current_time
            return self._texts
        self._texts = fetched
        return fetched

    def update_from_bot_config(self, config_data: Mapping[str, Any]) -> bool:
        """从宿主广播的 bot 配置里直接刷新缓存。

        Args:
            config_data: 宿主广播的 bot 配置字典。

        Returns:
            bool: 是否取到了人设字段。
        """

        personality_section = config_data.get("personality")
        bot_section = config_data.get("bot")
        if not isinstance(personality_section, Mapping) or not isinstance(bot_section, Mapping):
            return False
        self._texts = BasePersonaTexts(
            personality=_as_text(personality_section.get("personality")),
            reply_style=_as_text(personality_section.get("reply_style")),
            behavior_style=_as_text(personality_section.get("behavior_style")),
            bot_name=_as_text(bot_section.get("nickname")),
            fetched_at=time.time(),
        )
        return True

    def invalidate(self) -> None:
        """让缓存立即失效。"""

        self._texts.fetched_at = 0.0


def _as_text(value: Any) -> str:
    """把配置值规范成去空白的字符串。"""

    return str(value or "").strip()


def patch_personality_text(text: str, base: BasePersonaTexts, persona_texts: "PersonaTexts") -> Tuple[str, bool]:
    """把系统提示里的人格与说话风格段落替换成临时人格的版本。

    Args:
        text: 系统提示原文。
        base: 宿主原人格文本。
        persona_texts: 临时人格要写入的文本。

    Returns:
        Tuple[str, bool]: 替换后的文本，以及本次是否发生了替换。
    """

    if not persona_texts.personality and not persona_texts.reply_style:
        return text, False
    if persona_texts.personality and persona_texts.personality in text:
        # 已经替换过（同一份 items 被重复触发），保持幂等
        return text, False

    patched = text
    replaced = False
    if persona_texts.personality:
        if base.personality and base.personality in patched:
            patched = patched.replace(base.personality, persona_texts.personality)
        else:
            patched = f"{persona_texts.override_header}\n{persona_texts.personality}\n（以上设定覆盖后文所有关于你的身份、性格与说话方式的描述）\n\n{patched}"
        replaced = True
    if persona_texts.reply_style:
        if base.reply_style and base.reply_style in patched:
            patched = patched.replace(base.reply_style, persona_texts.reply_style)
        else:
            patched = f"{patched}\n{persona_texts.override_header}说话风格：\n{persona_texts.reply_style}"
        replaced = True
    return patched, replaced


def patch_behavior_style_text(text: str, base: BasePersonaTexts, persona_texts: "PersonaTexts") -> Tuple[str, bool]:
    """把规划器系统提示里的行为风格段落替换成临时人格的版本。"""

    if not persona_texts.behavior_style:
        return text, False
    if persona_texts.behavior_style in text:
        return text, False
    if base.behavior_style and base.behavior_style in text:
        return text.replace(base.behavior_style, persona_texts.behavior_style), True
    return f"{text}\n{persona_texts.override_header}行为风格：\n{persona_texts.behavior_style}", True


@dataclass(frozen=True)
class PersonaTexts:
    """临时人格要写进提示词的文本。"""

    name: str
    personality: str
    reply_style: str
    behavior_style: str

    @property
    def override_header(self) -> str:
        """覆盖块标题。"""

        return f"{OVERRIDE_TAG}{self.name}" if self.name else OVERRIDE_TAG


def find_system_item(items: Sequence[Any]) -> Tuple[int, str]:
    """找出请求里的系统消息 Item 与其文本。

    Args:
        items: Hook 载荷里的 Context Item 列表。

    Returns:
        Tuple[int, str]: Item 下标与文本；找不到时返回 ``(-1, "")``。
    """

    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or item.get("item_type") != "SystemMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        first_part = parts[0]
        if not isinstance(first_part, Mapping) or first_part.get("type") != "text":
            continue
        return index, str(first_part.get("text") or "")
    return -1, ""


def replace_system_item_text(item: Mapping[str, Any], text: str) -> Dict[str, Any]:
    """复制 Item 并替换系统消息文本，其余字段（item_id 等）原样保留。"""

    patched_item: Dict[str, Any] = dict(item)
    parts = item.get("parts")
    if not isinstance(parts, list):
        raise ValueError("系统消息 Item 缺少 parts")
    patched_parts: List[Any] = [dict(part) if isinstance(part, Mapping) else part for part in parts]
    first_part = dict(patched_parts[0]) if isinstance(patched_parts[0], Mapping) else {}
    first_part["text"] = text
    patched_parts[0] = first_part
    patched_item["parts"] = patched_parts
    return patched_item


def patch_system_items(
    items: Sequence[Any], base: BasePersonaTexts, persona_texts: PersonaTexts, *, behavior_style_only: bool = False
) -> Tuple[List[Any], bool]:
    """把请求 Items 里的系统提示替换成临时人格版本。

    Args:
        items: Hook 载荷里的 Context Item 列表。
        base: 宿主原人格文本。
        persona_texts: 临时人格要写入的文本。
        behavior_style_only: 只改写行为风格（规划器请求用），不动人格与说话风格。

    Returns:
        Tuple[List[Any], bool]: 新的 Items 列表，以及本次是否发生了改写。
    """

    system_index, system_text = find_system_item(items)
    if system_index < 0 or not system_text:
        return list(items), False
    if behavior_style_only:
        patched_text, replaced = patch_behavior_style_text(system_text, base, persona_texts)
    else:
        patched_text, replaced = patch_personality_text(system_text, base, persona_texts)
    if not replaced:
        return list(items), False
    patched_items = list(items)
    patched_items[system_index] = replace_system_item_text(items[system_index], patched_text)
    return patched_items, True
