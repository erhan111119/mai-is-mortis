"""mai is mortis —— 麦麦是多首的怪物。

每来一批消息，插件把最近的对话交给模型判定一次：模型按每个人格的「判定条件」回编号，
命中就给对应人格加分（有人骂麦麦 → 嘴臭人格加分，有人夸麦麦 → 撒娇人格加分）。
某个人格分数顶到阈值就切过去、其他人格清零，维持一段时间后自动切回原人格；分数随时间衰减。

与宿主交互的三处：

1. ``chat.receive.after_process``：记录消息，按「抖动等待 + 判定冷却」节流地发起判定；
2. ``maisaka.replyer.before_model_request``：临时人格生效时，把系统提示里的人设、
   说话风格段落替换成该人格的版本（规划器走 ``maisaka.planner.before_request``）；
3. ``maisaka.replyer.before_request``：把人格的「额外要求」并进本次回复要求。

原人格始终取自宿主配置（``personality.personality`` / ``reply_style`` / ``behavior_style``），
插件只改写「这一次发给模型的请求」，不写宿主配置：临时人格到期即自动恢复，
宿主配置热重载也会立刻反映到下一次回复。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

import asyncio
import time

from .mortis.config import MortisConfig
from .mortis.judge import PersonaProfile, build_profiles, judge_message, judged_profiles
from .mortis.prompt_patch import BaseTextCache, PersonaTexts, patch_system_items
from .mortis.state import ScoreBoard

STATE_FILE_NAME = "persona_scores.json"


class MaiIsMortisPlugin(MaiBotPlugin):
    """插件主类：计分、切换与提示词改写。"""

    config_model = MortisConfig
    config_reload_subscriptions = ("bot",)

    def __init__(self) -> None:
        """初始化运行时容器。"""

        super().__init__()
        self._board: Optional[ScoreBoard] = None
        self._profiles: Dict[str, PersonaProfile] = {}
        self._persona_order: List[str] = []
        self._base_cache = BaseTextCache()
        self._judge_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._judge_pending: set[str] = set()
        self._last_mention: Dict[str, bool] = {}
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
        await asyncio.to_thread(board.load, self.ctx.logger)
        self.ctx.logger.info(
            "mai is mortis 已加载：人格=%s；判定模型任务=%s；衰减 %.1f 分/分钟（宽限 %.1f 分钟）",
            "、".join(profile.name for profile in self._profiles.values()) or "未配置",
            self.config.evaluation.model_task or "默认",
            self.config.scoring.decay_per_minute,
            self.config.scoring.decay_grace_minutes,
        )

    async def on_unload(self) -> None:
        """取消后台判定并落盘分数状态。"""

        for task in list(self._judge_tasks.values()):
            task.cancel()
        self._judge_tasks.clear()
        self._judge_pending.clear()
        await self._save_state(time.time(), force=True)
        self.ctx.logger.info("mai is mortis 已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """处理配置热重载：``self`` 重编译人格，``bot`` 刷新宿主原人格文本。"""

        if scope == "self":
            self._apply_config()
            self.ctx.logger.info(
                "mai is mortis 配置已更新：人格=%s（版本 %s）",
                "、".join(profile.name for profile in self._profiles.values()) or "未配置",
                version,
            )
            return
        if scope == "bot":
            if not self._base_cache.update_from_bot_config(config_data):
                self._base_cache.invalidate()

    def _apply_config(self) -> None:
        """把最新配置编译成运行时可用的结构。"""

        config = self.config
        self._profiles = build_profiles(config.personas.personas, self.ctx.logger)
        self._persona_order = list(self._profiles.keys())
        if self._board is not None:
            self._board.configure(
                decay_per_minute=config.scoring.decay_per_minute,
                grace_minutes=config.scoring.decay_grace_minutes,
                score_cap=config.scoring.score_cap,
                max_sessions=config.scoring.max_sessions,
            )

    # ──── 入站消息与判定 ──────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="mortis_incoming",
        description="记录消息并在合适的时机发起模型判定，命中就给人格加分。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_incoming(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """处理入站消息：记账、必要时发起判定。"""

        config = self.config
        board = self._board
        if not config.plugin.enabled or board is None or not isinstance(message, Mapping):
            return {"action": "continue"}

        text = str(message.get("processed_plain_text") or "").strip()
        session_id = str(message.get("session_id") or "")
        if not session_id:
            return {"action": "continue"}
        is_mention = self._is_mention(message)
        if config.plugin.verbose_log:
            self.ctx.logger.debug(
                "mai is mortis：收到消息 session=%s 长度=%d 是否@=%s 文本=%.40s",
                session_id[:8],
                len(text),
                is_mention,
                text,
            )
        if not text or bool(message.get("is_command")):
            return {"action": "continue"}

        now = time.time()
        board.push_recent(session_id, self._speaker_name(message), text, config.evaluation.recent_message_count)
        self._last_mention[session_id] = is_mention
        if len(text) >= max(0, config.evaluation.min_text_length):
            self._schedule_judgement(session_id)
        await self._save_state(now)
        return {"action": "continue"}

    def _schedule_judgement(self, session_id: str) -> None:
        """安排一次判定；同一会话同时只跑一个，新消息只标记「有更新」，判完补一轮。"""

        config = self.config
        board = self._board
        if board is None or not config.evaluation.enabled:
            return
        if not judged_profiles(self._profiles):
            return
        now = time.time()
        if self._scoring_paused(session_id, now):
            return
        running_task = self._judge_tasks.get(session_id)
        if running_task is not None and not running_task.done():
            self._judge_pending.add(session_id)
            return
        if not board.judgement_ready(session_id, now, config.evaluation.cooldown_seconds):
            self._judge_pending.add(session_id)
            return
        task = asyncio.create_task(self._run_judgement(session_id))
        self._judge_tasks[session_id] = task
        task.add_done_callback(lambda finished, sid=session_id: self._on_judgement_done(sid, finished))

    def _on_judgement_done(self, session_id: str, task: asyncio.Task[Any]) -> None:
        """判定结束后清理，并补跑冷却期内积压的那一轮。"""

        if self._judge_tasks.get(session_id) is task:
            self._judge_tasks.pop(session_id, None)
        if task.cancelled():
            return
        if session_id in self._judge_pending:
            self._judge_pending.discard(session_id)
            self._schedule_judgement(session_id)

    async def _run_judgement(self, session_id: str) -> None:
        """抖动等待后判定一次，命中就加分并重新做切换判定。"""

        evaluation = self.config.evaluation
        await asyncio.sleep(max(0.0, evaluation.debounce_seconds))

        board = self._board
        if board is None:
            return
        now = time.time()
        if self._scoring_paused(session_id, now):
            return
        targets = judged_profiles(self._profiles)
        history = board.recent(session_id)
        if not targets or not history:
            return

        board.mark_judged(session_id, now)
        base = await self._base_cache.get(self.ctx)
        try:
            outcome = await judge_message(
                self.ctx,
                bot_name=base.bot_name,
                criteria_list=[profile.criteria for profile in targets],
                history=history,
                latest_is_mention=self._last_mention.get(session_id, False),
                model_task=evaluation.model_task,
                temperature=evaluation.temperature,
                max_tokens=evaluation.max_response_tokens,
            )
        except Exception as exc:
            self.ctx.logger.warning(f"mai is mortis：模型判定失败，本轮不加分: {exc}")
            return
        if self.config.plugin.verbose_log:
            self.ctx.logger.debug("mai is mortis：判定原始输出=%s", outcome.raw_response[:120])
        if not outcome.matched_indexes:
            return

        now = time.time()
        if self._scoring_paused(session_id, now):
            return
        for index in outcome.matched_indexes:
            profile = targets[index]
            score = board.add_gain(session_id, profile.persona_id, profile.weight, now)
            if self.config.plugin.verbose_log:
                self.ctx.logger.info(
                    "mai is mortis：%s +%.1f → %.1f/%.1f",
                    profile.name,
                    profile.weight,
                    score,
                    profile.threshold,
                )
        self._maybe_switch(session_id, now)
        await self._save_state(now)

    def _scoring_paused(self, session_id: str, now: float) -> bool:
        """临时人格生效中且配置为不累计时暂停计分（省下模型调用）。"""

        board = self._board
        if board is None:
            return True
        active_persona_id, _ = board.active_persona(session_id, now)
        return bool(active_persona_id) and not self.config.switching.accumulate_while_active

    def _maybe_switch(self, session_id: str, now: float) -> Optional[PersonaProfile]:
        """分数顶到阈值就切换人格，返回本次激活的人格。"""

        board = self._board
        if board is None:
            return None
        candidates: List[Tuple[float, float, PersonaProfile]] = []
        for profile in self._profiles.values():
            if not profile.auto_switchable:
                continue
            score = board.effective_score(session_id, profile.persona_id, now)
            if score >= profile.threshold:
                candidates.append((score / profile.threshold, score, profile))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, score, chosen = candidates[0]
        previous_persona_id, _ = board.active_persona(session_id, now)
        board.activate(session_id, chosen.persona_id, now, chosen.duration_minutes)
        if self.config.switching.reset_scores_on_switch:
            board.clear_scores(session_id)
        self.ctx.logger.info(
            "mai is mortis 人格切换：%s → %s（分数 %.1f/%.1f，维持 %.1f 分钟）",
            previous_persona_id or "原人格",
            chosen.name,
            score,
            chosen.threshold,
            chosen.duration_minutes,
        )
        return chosen

    def _active_persona(self, session_id: str) -> Optional[PersonaProfile]:
        """返回该会话当前生效的临时人格；到期时顺手记一条日志。"""

        board = self._board
        if board is None or not session_id or not self.config.plugin.enabled:
            return None
        active_persona_id, expired_persona_id = board.active_persona(session_id, time.time())
        if expired_persona_id:
            expired = self._profiles.get(expired_persona_id)
            self.ctx.logger.info(
                "mai is mortis：临时人格「%s」已到期，恢复原人格",
                expired.name if expired is not None else expired_persona_id,
            )
        if not active_persona_id:
            return None
        return self._profiles.get(active_persona_id)

    # ──── 提示词改写 ──────────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="mortis_replyer_prompt",
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
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": patched_items}}

    @HookHandler(
        "maisaka.planner.before_request",
        name="mortis_planner_prompt",
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
            items, base, self._persona_texts(persona), behavior_style_only=True
        )
        if not replaced:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": patched_items}}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="mortis_replyer_requirement",
        description="临时人格生效时，把该人格的额外要求并进本次回复要求。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def append_reply_requirement(self, **kwargs: Any) -> Dict[str, Any]:
        """把人格的额外要求追加到 ``extra_prompt``。"""

        persona = self._active_persona(str(kwargs.get("session_id") or ""))
        if persona is None or not persona.extra_prompt:
            return {"action": "continue"}
        existing = str(kwargs.get("extra_prompt") or "").strip()
        merged = f"{existing}\n{persona.extra_prompt}" if existing else persona.extra_prompt
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": merged}}

    @staticmethod
    def _persona_texts(persona: PersonaProfile) -> PersonaTexts:
        """取出临时人格要写入提示词的文本。"""

        return PersonaTexts(
            name=persona.name,
            personality=persona.personality,
            reply_style=persona.reply_style,
            behavior_style=persona.behavior_style,
        )

    # ──── 指令 ────────────────────────────────────────────────

    @Command(
        "mortis_persona",
        description="查看当前会话的人格分数与临时人格；支持 /人格 切换 <人格ID> 与 /人格 重置",
        pattern=r"^/(?:人格|persona)(?:\s+(?P<action>切换|重置|switch|reset))?(?:\s+(?P<argument>\S+))?$",
    )
    async def handle_persona_command(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """处理 /人格 指令：查询状态、手动切换、重置分数。"""

        if not self.config.plugin.enabled or self._board is None:
            return True, "mai is mortis 当前已停用。", 2

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

        persona = self._profiles.get(persona_id)
        if persona is None:
            available = "、".join(self._persona_order) or "（没有配置任何人格）"
            return f"没有找到人格「{persona_id or '空'}」。可用的人格 ID：{available}"
        board = self._board
        if board is None:
            return "mai is mortis 当前不可用。"
        board.activate(session_id, persona.persona_id, now, persona.duration_minutes)
        return f"已手动切换到「{persona.name}」，{persona.duration_minutes:g} 分钟后自动切回原人格。"

    def _format_status(self, session_id: str, now: float) -> str:
        """拼出指令返回的人格状态文本。"""

        board = self._board
        if board is None:
            return "mai is mortis 当前不可用。"
        active_persona_id, _ = board.active_persona(session_id, now)
        lines: List[str] = ["【人格状态】"]
        if active_persona_id:
            persona = self._profiles.get(active_persona_id)
            remaining = board.remaining_seconds(session_id, now)
            lines.append(
                f"当前：{persona.name if persona is not None else active_persona_id}"
                f"（剩余 {self._format_duration(remaining)}）"
            )
        else:
            lines.append("当前：原人格")
        if not self._persona_order:
            lines.append("没有配置任何人格。")
            return "\n".join(lines)
        lines.append("分数：")
        for persona_id, score in board.snapshot(session_id, now, self._persona_order):
            persona = self._profiles[persona_id]
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
            self.ctx.logger.error(f"mai is mortis：分数状态落盘失败: {exc}")


def create_plugin() -> MaiIsMortisPlugin:
    """返回插件实例。"""

    return MaiIsMortisPlugin()
