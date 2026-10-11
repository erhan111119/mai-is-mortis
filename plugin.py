"""人格切换插件：用一个评分机制让麦麦临时换人格。

玩法（默认配置）：

* 有人骂麦麦 → 「嘴臭人格」加分；有人夸麦麦 → 「撒娇人格」加分；
* 某个人格分数顶到阈值 → 立刻切到该人格，其他人格分数清零；
* 该人格维持若干分钟后自动切回原人格；
* 分数随时间衰减：最后一次加分后的宽限期内不掉，之后每分钟按配置速度掉。

实现上有三处与宿主交互：

1. ``chat.receive.after_process``：每条入站消息按触发规则加分，并做切换判定；
2. ``maisaka.replyer.before_model_request``：临时人格生效时，把系统提示里的人设、
   说话风格段落替换成该人格的版本（规划器请求走 ``maisaka.planner.before_request``）；
3. ``maisaka.replyer.before_request``：把人格的「额外要求」并进本次回复要求。

原人格始终取自宿主配置（``personality.personality`` / ``reply_style`` / ``behavior_style``），
插件只改写「这一次发给模型的请求」，不写宿主配置，所以临时人格到期即自动恢复，
宿主配置热重载也会立刻反映到下一次回复。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

import asyncio
import time

from .switcher.config import PersonaSwitcherConfig
from .switcher.evaluator import judge_message
from .switcher.prompt_patch import BaseTextCache, PersonaTexts, patch_system_items
from .switcher.state import ScoreBoard
from .switcher.triggers import CompiledPersona, compile_personas, match_message

STATE_FILE_NAME = "persona_scores.json"
"""分数状态在插件数据目录里的文件名。"""

PERSONA_COOLDOWN_SUFFIX = "#persona"
"""人格级冷却在账本里的键前缀，避免和 ``<人格ID>#<规则序号>`` 撞车。"""


class PersonaSwitcherPlugin(MaiBotPlugin):
    """人格切换插件主类。"""

    config_model = PersonaSwitcherConfig
    config_reload_subscriptions = ("bot",)

    def __init__(self) -> None:
        """初始化运行时容器。"""

        super().__init__()
        self._board: Optional[ScoreBoard] = None
        self._personas: Dict[str, CompiledPersona] = {}
        self._persona_order: List[str] = []
        self._base_cache = BaseTextCache()
        self._judge_tasks: set[asyncio.Task[Any]] = set()
        self._last_save_at = 0.0

    # ──── 生命周期 ────────────────────────────────────────────

    async def on_load(self) -> None:
        """加载配置并读取上次的分数状态。"""

        board = ScoreBoard(
            self.ctx.paths.data_dir / STATE_FILE_NAME,
            max_sessions=self.config.scoring.max_sessions,
        )
        self._board = board
        self._apply_config()
        await asyncio.to_thread(board.load)
        persona_names = "、".join(persona.name for persona in self._personas.values()) or "未配置"
        self.ctx.logger.info(
            "人格切换插件已加载：人格=%s；衰减 %.1f 分/分钟（宽限 %.1f 分钟）",
            persona_names,
            self.config.scoring.decay_per_minute,
            self.config.scoring.decay_grace_minutes,
        )

    async def on_unload(self) -> None:
        """取消后台判定并落盘分数状态。"""

        for task in list(self._judge_tasks):
            task.cancel()
        self._judge_tasks.clear()
        await self._save_state(time.time(), force=True)
        self.ctx.logger.info("人格切换插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """处理配置热重载。

        Args:
            scope: 配置范围：``self`` 为插件自身配置，``bot`` 为宿主 bot 配置。
            config_data: 该范围下的最新配置。
            version: 配置版本号。
        """

        if scope == "self":
            self._apply_config()
            persona_names = "、".join(persona.name for persona in self._personas.values()) or "未配置"
            self.ctx.logger.info("人格切换插件配置已更新：人格=%s（版本 %s）", persona_names, version)
            return
        if scope == "bot":
            if self._base_cache.update_from_bot_config(config_data):
                self.ctx.logger.debug("人格切换插件已同步宿主 bot 配置里的人设文本")
            else:
                self._base_cache.invalidate()

    def _apply_config(self) -> None:
        """把最新配置编译成运行时可用的结构。"""

        config = self.config
        self._personas = compile_personas(config.personas.personas, self.ctx.logger)
        self._persona_order = list(self._personas.keys())
        if self._board is not None:
            self._board.configure(
                decay_per_minute=config.scoring.decay_per_minute,
                grace_minutes=config.scoring.decay_grace_minutes,
                score_cap=config.scoring.score_cap,
                max_sessions=config.scoring.max_sessions,
            )

    # ──── 加分与切换 ──────────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="persona_switcher_incoming",
        description="按触发规则给人格加分，分数顶到阈值就临时切换人格。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_incoming(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """处理入站消息：加分、判定切换、必要时交给模型判定。"""

        config = self.config
        board = self._board
        if not config.plugin.enabled or board is None or not isinstance(message, Mapping):
            return {"action": "continue"}

        text = str(message.get("processed_plain_text") or "").strip()
        session_id = str(message.get("session_id") or "")
        if not text or not session_id or bool(message.get("is_command")):
            return {"action": "continue"}

        now = time.time()
        board.push_recent(session_id, self._speaker_name(message), text, config.evaluation.recent_message_count)
        if not self._scoring_paused(session_id, now):
            hits = match_message(self._personas, text, is_mention=self._is_mention(message))
            if hits:
                self._apply_hits(session_id, hits, now, text)
            self._maybe_switch(session_id, now)
        self._schedule_judgement(session_id, text, now)
        await self._save_state(now)
        return {"action": "continue"}

    def _scoring_paused(self, session_id: str, now: float) -> bool:
        """判断当前是否暂停计分（临时人格生效中且配置为不累计）。"""

        board = self._board
        if board is None:
            return True
        active_persona_id, _ = board.active_persona(session_id, now)
        if not active_persona_id:
            return False
        return not self.config.switching.accumulate_while_active

    def _apply_hits(
        self, session_id: str, hits: Sequence[Any], now: float, text: str
    ) -> Dict[str, float]:
        """按冷却规则把命中换算成加分，返回 ``人格 ID → 加分后分数``。"""

        board = self._board
        if board is None:
            return {}
        # 先按规则级冷却筛出本条消息的有效命中：同一句话可以同时命中多条规则
        matched: Dict[str, float] = {}
        for hit in hits:
            persona = self._personas.get(hit.persona_id)
            if persona is None:
                continue
            if hit.cooldown_seconds > 0 and board.is_cooling_down(session_id, hit.rule_key, now):
                continue
            matched[hit.persona_id] = matched.get(hit.persona_id, 0.0) + hit.weight
            if hit.cooldown_seconds > 0:
                board.mark_cooldown(session_id, hit.rule_key, now, hit.cooldown_seconds)

        # 再按人格级冷却决定该人格本条消息算不算分：人格冷却只拦「下一条消息」，
        # 不会把同一句话里的多条规则拆开
        updated: Dict[str, float] = {}
        for persona_id, gain in matched.items():
            persona = self._personas[persona_id]
            persona_cooldown_key = f"{persona_id}{PERSONA_COOLDOWN_SUFFIX}"
            if persona.cooldown_seconds > 0 and board.is_cooling_down(session_id, persona_cooldown_key, now):
                continue
            capped_gain = min(gain, persona.max_gain_per_message) if persona.max_gain_per_message > 0 else gain
            score = board.add_gain(session_id, persona_id, capped_gain, now)
            updated[persona_id] = score
            if persona.cooldown_seconds > 0:
                board.mark_cooldown(session_id, persona_cooldown_key, now, persona.cooldown_seconds)
            if self.config.switching.log_gain:
                self.ctx.logger.info(
                    "人格加分：%s +%.1f → %.1f/%.1f（触发内容：%s）",
                    persona.name,
                    capped_gain,
                    score,
                    persona.threshold,
                    text[:40],
                )
        return updated

    def _maybe_switch(self, session_id: str, now: float) -> Optional[CompiledPersona]:
        """分数顶到阈值就切换人格，返回本次激活的人格。"""

        board = self._board
        if board is None:
            return None
        candidates: List[Tuple[float, float, CompiledPersona]] = []
        for persona in self._personas.values():
            if not persona.auto_switchable:
                continue
            score = board.effective_score(session_id, persona.persona_id, now)
            if score >= persona.threshold:
                candidates.append((score / persona.threshold, score, persona))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, score, chosen = candidates[0]
        previous_persona_id, _ = board.active_persona(session_id, now)
        board.activate(session_id, chosen.persona_id, now, chosen.duration_minutes)
        if self.config.switching.reset_scores_on_switch:
            board.clear_scores(session_id)
        self.ctx.logger.info(
            "人格切换：%s → %s（分数 %.1f/%.1f，维持 %.1f 分钟）",
            previous_persona_id or "原人格",
            chosen.name,
            score,
            chosen.threshold,
            chosen.duration_minutes,
        )
        return chosen

    def _active_persona(self, session_id: str) -> Optional[CompiledPersona]:
        """返回该会话当前生效的临时人格；到期时顺手记一条日志。"""

        board = self._board
        if board is None or not session_id or not self.config.plugin.enabled:
            return None
        active_persona_id, expired_persona_id = board.active_persona(session_id, time.time())
        if expired_persona_id:
            expired = self._personas.get(expired_persona_id)
            self.ctx.logger.info(
                "临时人格「%s」已到期，恢复原人格",
                expired.name if expired is not None else expired_persona_id,
            )
        if not active_persona_id:
            return None
        return self._personas.get(active_persona_id)

    # ──── 提示词改写 ──────────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="persona_switcher_replyer_prompt",
        description="临时人格生效时，把回复系统提示里的人设与说话风格换成该人格的版本。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def patch_replyer_request(self, items: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """把临时人格写进回复请求的系统提示。"""

        persona = self._active_persona(str(kwargs.get("session_id") or ""))
        if persona is None or not isinstance(items, list) or not items:
            return {"action": "continue"}
        base = await self._base_cache.get(self.ctx)
        patched_items, replaced = patch_system_items(items, base, self._persona_texts(persona))
        if not replaced:
            return {"action": "continue"}
        self.ctx.logger.debug("临时人格「%s」已写入本次回复的系统提示", persona.name)
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": patched_items}}

    @HookHandler(
        "maisaka.planner.before_request",
        name="persona_switcher_planner_prompt",
        description="临时人格配置了行为风格时，同步改写规划器系统提示。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def patch_planner_request(self, items: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """把临时人格的行为风格写进规划请求的系统提示。"""

        if not self.config.switching.inject_planner:
            return {"action": "continue"}
        persona = self._active_persona(str(kwargs.get("session_id") or ""))
        if persona is None or not isinstance(items, list) or not items:
            return {"action": "continue"}
        base = await self._base_cache.get(self.ctx)
        patched_items, replaced = patch_system_items(
            items,
            base,
            self._persona_texts(persona),
            behavior_style_only=True,
        )
        if not replaced:
            return {"action": "continue"}
        self.ctx.logger.debug("临时人格「%s」的行为风格已写入本次规划提示", persona.name)
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": patched_items}}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="persona_switcher_replyer_requirement",
        description="临时人格生效时，把该人格的额外要求并进本次回复要求。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def append_reply_requirement(self, **kwargs: Any) -> Dict[str, Any]:
        """把人格的额外要求追加到 ``extra_prompt``。"""

        persona = self._active_persona(str(kwargs.get("session_id") or ""))
        if persona is None:
            return {"action": "continue"}
        requirement = persona.entry.extra_prompt.strip()
        if not requirement:
            return {"action": "continue"}
        existing = str(kwargs.get("extra_prompt") or "").strip()
        merged = f"{existing}\n{requirement}" if existing else requirement
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": merged}}

    @staticmethod
    def _persona_texts(persona: CompiledPersona) -> PersonaTexts:
        """取出临时人格要写入提示词的文本。"""

        return PersonaTexts(
            name=persona.name,
            personality=persona.entry.personality.strip(),
            reply_style=persona.entry.reply_style.strip(),
            behavior_style=persona.entry.behavior_style.strip(),
        )

    # ──── 模型判定 ────────────────────────────────────────────

    def _schedule_judgement(self, session_id: str, text: str, now: float) -> None:
        """需要时把最近的对话交给模型判定一次。"""

        config = self.config
        evaluation = config.evaluation
        board = self._board
        if board is None or not evaluation.enabled:
            return
        targets = [
            persona
            for persona in self._personas.values()
            if persona.llm_criteria and persona.llm_weight > 0 and persona.auto_switchable
        ]
        if not targets:
            return
        if len(text) < max(0, evaluation.min_text_length):
            return
        if self._scoring_paused(session_id, now):
            return
        if not board.judgement_ready(session_id, now, evaluation.cooldown_seconds):
            return
        board.mark_judged(session_id, now)
        task = asyncio.create_task(self._run_judgement(session_id, targets, board.recent(session_id), text))
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)

    async def _run_judgement(
        self,
        session_id: str,
        targets: Sequence[CompiledPersona],
        history: Sequence[Tuple[str, str]],
        text: str,
    ) -> None:
        """后台执行模型判定，命中就加分并重新做切换判定。"""

        evaluation = self.config.evaluation
        base = await self._base_cache.get(self.ctx)
        try:
            matched_indexes = await judge_message(
                self.ctx,
                bot_name=base.bot_name,
                criteria_list=[persona.llm_criteria for persona in targets],
                history=history,
                latest_text=text,
                model_task=evaluation.model_task,
                temperature=evaluation.temperature,
                max_tokens=evaluation.max_response_tokens,
            )
        except Exception as exc:
            self.ctx.logger.warning(f"人格模型判定失败，本轮跳过: {exc}")
            return
        if not matched_indexes:
            return
        board = self._board
        if board is None:
            return
        now = time.time()
        if self._scoring_paused(session_id, now):
            return
        for index in matched_indexes:
            persona = targets[index]
            score = board.add_gain(session_id, persona.persona_id, persona.llm_weight, now)
            self.ctx.logger.info(
                "人格模型判定命中：%s +%.1f → %.1f/%.1f",
                persona.name,
                persona.llm_weight,
                score,
                persona.threshold,
            )
        self._maybe_switch(session_id, now)
        await self._save_state(now)

    # ──── 指令 ────────────────────────────────────────────────

    @Command(
        "persona_status",
        description="查看当前会话的人格分数与临时人格；支持 /人格 切换 <人格ID> 与 /人格 重置",
        pattern=r"^/(?:人格|persona)(?:\s+(?P<action>切换|重置|switch|reset))?(?:\s+(?P<argument>\S+))?$",
    )
    async def handle_persona_command(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """处理 /人格 指令：查询状态、手动切换、重置分数。"""

        if not self.config.plugin.enabled or self._board is None:
            return True, "人格切换插件当前已停用。", 2

        session_id = str(kwargs.get("stream_id") or "")
        matched_groups = kwargs.get("matched_groups")
        action = ""
        argument = ""
        if isinstance(matched_groups, Mapping):
            action = str(matched_groups.get("action") or "").strip()
            argument = str(matched_groups.get("argument") or "").strip()
        now = time.time()

        if action in ("切换", "switch"):
            if not self._is_operator(kwargs):
                return True, "只有管理员可以手动切换人格。", 2
            return True, self._manual_switch(session_id, argument, now), 2
        if action in ("重置", "reset"):
            if not self._is_operator(kwargs):
                return True, "只有管理员可以重置人格状态。", 2
            self._board.reset(session_id)
            return True, "已清零本会话的人格分数，并恢复原人格。", 2
        return True, self._format_status(session_id, now), 2

    def _manual_switch(self, session_id: str, persona_id: str, now: float) -> str:
        """按指令手动激活人格。"""

        persona = self._personas.get(persona_id)
        if persona is None:
            available = "、".join(self._persona_order) or "（没有配置任何人格）"
            return f"没有找到人格「{persona_id or '空'}」。可用的人格 ID：{available}"
        board = self._board
        if board is None:
            return "人格切换插件当前不可用。"
        board.activate(session_id, persona.persona_id, now, persona.duration_minutes)
        return f"已手动切换到「{persona.name}」，{persona.duration_minutes:g} 分钟后自动切回原人格。"

    def _format_status(self, session_id: str, now: float) -> str:
        """拼出指令返回的人格状态文本。"""

        board = self._board
        if board is None:
            return "人格切换插件当前不可用。"
        active_persona_id, _ = board.active_persona(session_id, now)
        lines: List[str] = ["【人格状态】"]
        if active_persona_id:
            persona = self._personas.get(active_persona_id)
            remaining = board.remaining_seconds(session_id, now)
            lines.append(
                f"当前：{persona.name if persona is not None else active_persona_id}（剩余 {self._format_duration(remaining)}）"
            )
        else:
            lines.append("当前：原人格")
        if not self._persona_order:
            lines.append("没有配置任何人格。")
            return "\n".join(lines)
        lines.append("分数：")
        for persona_id, score in board.snapshot(session_id, now, self._persona_order):
            persona = self._personas[persona_id]
            if not persona.auto_switchable:
                lines.append(f"- {persona.name}：{score:.1f}（不参与自动切换）")
                continue
            lines.append(f"- {persona.name}：{score:.1f}/{persona.threshold:g}")
        lines.append(f"（每分钟衰减 {self.config.scoring.decay_per_minute:g} 分；/人格 重置 可清零）")
        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """把秒数写成「x 分 y 秒」。"""

        total_seconds = max(0, int(seconds))
        minutes, remaining_seconds = divmod(total_seconds, 60)
        if minutes <= 0:
            return f"{remaining_seconds} 秒"
        return f"{minutes} 分 {remaining_seconds} 秒"

    def _is_operator(self, kwargs: Mapping[str, Any]) -> bool:
        """判断指令发起者是否有权手动切换或重置。"""

        if bool(kwargs.get("is_local_operator")):
            return True
        user_id = str(kwargs.get("user_id") or "")
        if not user_id:
            return False
        return user_id in {item.strip() for item in self.config.command.operator_user_ids if item.strip()}

    # ──── 消息解析与落盘 ──────────────────────────────────────

    @staticmethod
    def _speaker_name(message: Mapping[str, Any]) -> str:
        """取出发言人显示名，供模型判定区分说话人。"""

        message_info = message.get("message_info")
        if isinstance(message_info, Mapping):
            user_info = message_info.get("user_info")
            if isinstance(user_info, Mapping):
                name = user_info.get("user_cardname") or user_info.get("user_nickname") or user_info.get("user_id")
                return str(name or "")
        return ""

    @staticmethod
    def _is_mention(message: Mapping[str, Any]) -> bool:
        """判断消息是否 @ 了麦麦。"""

        return bool(message.get("is_at") or message.get("is_mentioned"))

    async def _save_state(self, now: float, *, force: bool = False) -> None:
        """按配置的间隔把分数状态落盘。"""

        board = self._board
        if board is None:
            return
        interval = max(0.0, self.config.switching.save_interval_seconds)
        if not force and interval > 0 and (now - self._last_save_at) < interval:
            return
        self._last_save_at = now
        try:
            await asyncio.to_thread(board.save, now)
        except OSError as exc:
            self.ctx.logger.error(f"人格分数状态落盘失败: {exc}")


def create_plugin() -> PersonaSwitcherPlugin:
    """返回插件实例。"""

    return PersonaSwitcherPlugin()
