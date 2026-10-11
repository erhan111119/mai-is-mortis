"""临时人格的分数账本。

每个会话（聊天流）各有一份账本：

* 每个人格一行分数，加分只写 ``PersonaScore``，实时值在读取时按「最后一次加分 +
  宽限期」往后推算衰减，因此不需要后台定时器，重启后也不会漂移。
* 分数顶到阈值时激活该人格并记下到期时间，到期后自动失效、回到原人格。
* 账本按最久未用顺序淘汰会话，并落盘到插件数据目录，重启后分数与剩余时间都还在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import json
import os
import time

STATE_FILE_VERSION = 2
"""状态文件格式版本，字段结构变化时递增，老文件直接丢弃。"""

STALE_SESSION_SECONDS = 3600.0
"""分数全为 0 且没有临时人格的会话，闲置超过这个时间就从账本里清掉。"""


@dataclass
class PersonaScore:
    """单个人格在单个会话里的分数。"""

    score: float = 0.0
    """最后一次加分后的分数（尚未扣掉之后的衰减）。"""

    last_gain_at: float = 0.0
    """最后一次加分的时刻，宽限期从这里开始算。"""

    def value_at(self, now: float, *, decay_per_minute: float, grace_minutes: float) -> float:
        """返回当前时刻的实时分数（已扣掉衰减）。

        Args:
            now: 当前时间戳。
            decay_per_minute: 每分钟衰减多少分。
            grace_minutes: 最后一次加分后的免衰减时间。

        Returns:
            float: 衰减后的分数，最低为 0。
        """

        if self.score <= 0:
            return 0.0
        decay_start = self.last_gain_at + max(0.0, grace_minutes) * 60.0
        if now <= decay_start:
            return self.score
        decayed = self.score - max(0.0, decay_per_minute) * (now - decay_start) / 60.0
        return decayed if decayed > 0.0 else 0.0

    def gain(
        self,
        amount: float,
        now: float,
        *,
        decay_per_minute: float,
        grace_minutes: float,
        score_cap: float,
    ) -> float:
        """先结算衰减再加分，返回加分后的分数。"""

        current = self.value_at(now, decay_per_minute=decay_per_minute, grace_minutes=grace_minutes) + amount
        if score_cap > 0.0:
            current = min(current, score_cap)
        self.score = current
        self.last_gain_at = now
        return current

    def clear(self) -> None:
        """把分数清零。"""

        self.score = 0.0
        self.last_gain_at = 0.0


@dataclass
class SessionState:
    """单个会话的人格状态。"""

    scores: Dict[str, PersonaScore] = field(default_factory=dict)
    """人格 ID → 分数。"""

    active_persona_id: str = ""
    """当前生效的临时人格 ID，空字符串表示正在用原人格。"""

    active_until: float = 0.0
    """临时人格的到期时间戳。"""

    recent: List[Tuple[str, str]] = field(default_factory=list)
    """最近的对话，元素为 ``(发言人, 内容)``，用于模型判定，不落盘。"""

    last_judged_at: float = 0.0
    """上一次交给模型判定的时刻。"""

    touched_at: float = 0.0
    """最后一次读写时刻，用于淘汰最久未用的会话。"""


class ScoreBoard:
    """按会话维护人格分数的账本。"""

    def __init__(self, path: Path, *, max_sessions: int = 200) -> None:
        """初始化账本。

        Args:
            path: 状态文件路径。
            max_sessions: 最多同时保留多少个会话的状态。
        """

        self._path = path
        self._max_sessions = max(1, int(max_sessions))
        self._sessions: Dict[str, SessionState] = {}
        self._decay_per_minute = 1.5
        self._grace_minutes = 1.5
        self._score_cap = 60.0

    def configure(
        self,
        *,
        decay_per_minute: float,
        grace_minutes: float,
        score_cap: float,
        max_sessions: int,
    ) -> None:
        """按配置更新衰减参数。"""

        self._decay_per_minute = max(0.0, float(decay_per_minute))
        self._grace_minutes = max(0.0, float(grace_minutes))
        self._score_cap = max(0.0, float(score_cap))
        self._max_sessions = max(1, int(max_sessions))

    # ──── 分数读写 ────────────────────────────────────────────

    def effective_score(self, session_id: str, persona_id: str, now: float) -> float:
        """返回某人格在该会话里的实时分数。"""

        state = self._sessions.get(session_id)
        if state is None:
            return 0.0
        score = state.scores.get(persona_id)
        if score is None:
            return 0.0
        return score.value_at(now, decay_per_minute=self._decay_per_minute, grace_minutes=self._grace_minutes)

    def add_gain(self, session_id: str, persona_id: str, amount: float, now: float) -> float:
        """给某人格加分，返回加分后的分数。"""

        state = self._state(session_id, now)
        score = state.scores.get(persona_id)
        if score is None:
            score = PersonaScore()
            state.scores[persona_id] = score
        return score.gain(
            amount,
            now,
            decay_per_minute=self._decay_per_minute,
            grace_minutes=self._grace_minutes,
            score_cap=self._score_cap,
        )

    def clear_scores(self, session_id: str, *, keep_persona_id: str = "") -> None:
        """把会话里除 ``keep_persona_id`` 之外的人格分数清零。"""

        state = self._sessions.get(session_id)
        if state is None:
            return
        for persona_id, score in state.scores.items():
            if persona_id != keep_persona_id:
                score.clear()

    # ──── 临时人格 ────────────────────────────────────────────

    def activate(self, session_id: str, persona_id: str, now: float, duration_minutes: float) -> float:
        """激活临时人格，返回到期时间戳。"""

        state = self._state(session_id, now)
        state.active_persona_id = persona_id
        state.active_until = now + max(0.1, float(duration_minutes)) * 60.0
        return state.active_until

    def active_persona(self, session_id: str, now: float) -> Tuple[str, str]:
        """返回 ``(当前生效的人格 ID, 刚刚到期的人格 ID)``。

        到期的临时人格会在这里被清掉，第二个返回值用于让调用方记一条「恢复原人格」的日志。
        """

        state = self._sessions.get(session_id)
        if state is None or not state.active_persona_id:
            return "", ""
        if now < state.active_until:
            return state.active_persona_id, ""
        expired_persona_id = state.active_persona_id
        state.active_persona_id = ""
        state.active_until = 0.0
        return "", expired_persona_id

    def remaining_seconds(self, session_id: str, now: float) -> float:
        """返回临时人格的剩余秒数，没有生效人格时返回 0。"""

        active_persona_id, _ = self.active_persona(session_id, now)
        if not active_persona_id:
            return 0.0
        return max(0.0, self._sessions[session_id].active_until - now)

    def reset(self, session_id: str) -> None:
        """清空会话的全部分数与临时人格。"""

        state = self._sessions.get(session_id)
        if state is None:
            return
        for score in state.scores.values():
            score.clear()
        state.active_persona_id = ""
        state.active_until = 0.0

    # ──── 判定冷却 ────────────────────────────────────────────

    def judgement_ready(self, session_id: str, now: float, cooldown_seconds: float) -> bool:
        """判断该会话现在是否可以再调一次模型判定。"""

        state = self._sessions.get(session_id)
        if state is None:
            return True
        return now - state.last_judged_at >= max(0.0, cooldown_seconds)

    def mark_judged(self, session_id: str, now: float) -> None:
        """记录该会话刚刚做过模型判定。"""

        self._state(session_id, now).last_judged_at = now

    # ──── 背景对话 ────────────────────────────────────────────

    def push_recent(self, session_id: str, speaker: str, text: str, limit: int) -> None:
        """记录一条背景对话，供模型判定使用。"""

        if limit <= 0:
            return
        state = self._state(session_id, time.time())
        state.recent.append((speaker, text))
        overflow = len(state.recent) - max(1, int(limit))
        if overflow > 0:
            del state.recent[:overflow]

    def recent(self, session_id: str) -> List[Tuple[str, str]]:
        """返回该会话最近的背景对话。"""

        state = self._sessions.get(session_id)
        if state is None:
            return []
        return list(state.recent)

    # ──── 展示 ────────────────────────────────────────────────

    def snapshot(self, session_id: str, now: float, persona_ids: List[str]) -> List[Tuple[str, float]]:
        """返回按配置顺序排列的 ``(人格 ID, 实时分数)``，供指令输出。"""

        return [(persona_id, self.effective_score(session_id, persona_id, now)) for persona_id in persona_ids]

    # ──── 落盘 ────────────────────────────────────────────────

    def load(self) -> None:
        """从磁盘读取状态；文件不存在时按空账本处理。"""

        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"人格分数状态文件无法读取: {self._path}") from exc
        if not isinstance(payload, Mapping) or int(payload.get("version", 0)) != STATE_FILE_VERSION:
            return
        raw_sessions = payload.get("sessions")
        if not isinstance(raw_sessions, Mapping):
            return
        for session_id, raw_state in raw_sessions.items():
            if isinstance(raw_state, Mapping):
                self._sessions[str(session_id)] = self._deserialize_session(raw_state)

    def save(self, now: float) -> None:
        """清理过期会话后把状态写回磁盘（先写临时文件再替换，避免写坏）。"""

        self._prune_stale(now)
        payload = {
            "version": STATE_FILE_VERSION,
            "sessions": {session_id: self._serialize_session(state) for session_id, state in self._sessions.items()},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary_path, self._path)

    # ──── 内部工具 ────────────────────────────────────────────

    def _state(self, session_id: str, now: float) -> SessionState:
        """取出会话状态，顺带刷新使用时间并淘汰超出上限的旧会话。"""

        state = self._sessions.get(session_id)
        if state is None:
            state = SessionState()
            self._sessions[session_id] = state
        state.touched_at = now
        if len(self._sessions) > self._max_sessions:
            self._evict()
        return state

    def _evict(self) -> None:
        """淘汰最久未用、且当前没有临时人格生效的会话。"""

        while len(self._sessions) > self._max_sessions:
            candidates = sorted(
                (state.touched_at, session_id)
                for session_id, state in self._sessions.items()
                if not state.active_persona_id
            )
            if not candidates:
                # 所有会话都还在临时人格生效期，先不淘汰，等它们到期后再说
                return
            del self._sessions[candidates[0][1]]

    def _prune_stale(self, now: float) -> None:
        """清掉完全归零、且闲置太久的会话。"""

        for session_id in [
            session_id
            for session_id, state in self._sessions.items()
            if not state.active_persona_id
            and now - state.touched_at > STALE_SESSION_SECONDS
            and not any(score.score > 0 for score in state.scores.values())
        ]:
            del self._sessions[session_id]

    @staticmethod
    def _serialize_session(state: SessionState) -> Dict[str, Any]:
        """把会话状态转成可写入 JSON 的结构。"""

        return {
            "scores": {
                persona_id: {"score": round(score.score, 3), "last_gain_at": round(score.last_gain_at, 3)}
                for persona_id, score in state.scores.items()
                if score.score > 0
            },
            "active_persona_id": state.active_persona_id,
            "active_until": round(state.active_until, 3),
        }

    @staticmethod
    def _deserialize_session(raw_state: Mapping[str, Any]) -> SessionState:
        """从 JSON 结构恢复会话状态。"""

        state = SessionState()
        raw_scores = raw_state.get("scores")
        if isinstance(raw_scores, Mapping):
            for persona_id, raw_score in raw_scores.items():
                if not isinstance(raw_score, Mapping):
                    continue
                state.scores[str(persona_id)] = PersonaScore(
                    score=float(raw_score.get("score", 0.0) or 0.0),
                    last_gain_at=float(raw_score.get("last_gain_at", 0.0) or 0.0),
                )
        state.active_persona_id = str(raw_state.get("active_persona_id", "") or "")
        state.active_until = float(raw_state.get("active_until", 0.0) or 0.0)
        state.touched_at = time.time()
        return state
