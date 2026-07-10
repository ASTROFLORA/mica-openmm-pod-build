"""
mica.drivers.phase_tracker
==========================
S2.3 — Explicit agent-phase tracking.

Defines canonical phase constants for the agent lifecycle and provides
a lightweight ``PhaseTracker`` that:

1. Maintains the **current phase** (one of the four canonical values).
2. Records all transitions with timestamps.
3. Integrates with ``EventLog.log_phase_transition()`` when an event-log
   is attached.

The four canonical agent phases align with the ThunderAgent dual-state
model and map onto the ``ProgramEnvelope.phase`` field (S2.1):

    REASONING     — llm inference, planning, intent decomposition
    ACTING        — tool execution, MCP calls, specialist spawns
    WAITING       — blocked on external resource or human input
    HUMAN_REVIEW  — paused for operator approval

Phase constants are plain strings so they serialize naturally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase constants
# ---------------------------------------------------------------------------

AgentPhase = Literal["reasoning", "acting", "waiting", "human_review"]

REASONING: AgentPhase = "reasoning"
ACTING: AgentPhase = "acting"
WAITING: AgentPhase = "waiting"
HUMAN_REVIEW: AgentPhase = "human_review"

ALL_PHASES: frozenset[AgentPhase] = frozenset({REASONING, ACTING, WAITING, HUMAN_REVIEW})

# ---------------------------------------------------------------------------
# Transition record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseTransition:
    """Immutable record of a single phase change."""

    from_phase: Optional[AgentPhase]
    to_phase: AgentPhase
    timestamp: str
    driver_id: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "from_phase": self.from_phase,
            "to_phase": self.to_phase,
            "timestamp": self.timestamp,
            "driver_id": self.driver_id,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Phase hooks
# ---------------------------------------------------------------------------

PhaseHook = Callable[[PhaseTransition], None]


# ---------------------------------------------------------------------------
# PhaseTracker
# ---------------------------------------------------------------------------

class PhaseTracker:
    """Track the current phase of an agent run and record transitions.

    Parameters
    ----------
    initial_phase : AgentPhase, optional
        Phase to start in.  Defaults to ``"reasoning"``.
    driver_id : str
        Identifier of the owning driver / run (logged with transitions).
    event_log : object, optional
        An ``EventLog`` instance.  If provided, every transition calls
        ``event_log.log_phase_transition()`` automatically.  Accepts
        ``None`` so the tracker can be used stand-alone for tests.
    """

    def __init__(
        self,
        initial_phase: AgentPhase = REASONING,
        *,
        driver_id: str = "",
        event_log: Any = None,
    ) -> None:
        if initial_phase not in ALL_PHASES:
            raise ValueError(f"Unknown phase: {initial_phase!r}")
        self._current: AgentPhase = initial_phase
        self._driver_id: str = driver_id
        self._event_log: Any = event_log
        self._history: list[PhaseTransition] = []
        self._hooks: list[PhaseHook] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current(self) -> AgentPhase:
        return self._current

    @property
    def history(self) -> list[PhaseTransition]:
        return list(self._history)

    @property
    def transition_count(self) -> int:
        return len(self._history)

    # ------------------------------------------------------------------
    # Transition
    # ------------------------------------------------------------------

    def transition_to(
        self,
        phase: AgentPhase,
        *,
        driver_id: str = "",
        metadata: Optional[dict] = None,
    ) -> PhaseTransition:
        """Move to *phase*.  No-op if already in that phase.

        Returns the ``PhaseTransition`` record (or the last one if no-op).
        Raises ``ValueError`` for unknown phases.
        """
        if phase not in ALL_PHASES:
            raise ValueError(f"Unknown phase: {phase!r}")

        if phase == self._current and self._history:
            return self._history[-1]

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        did = driver_id or self._driver_id
        trans = PhaseTransition(
            from_phase=self._current,
            to_phase=phase,
            timestamp=ts,
            driver_id=did,
            metadata=metadata or {},
        )

        self._current = phase
        self._history.append(trans)

        # Fire hooks
        for hook in self._hooks:
            try:
                hook(trans)
            except Exception:  # noqa: BLE001 — hooks should not crash the tracker
                logger.warning("PhaseTracker hook error", exc_info=True)

        # Emit to EventLog
        if self._event_log is not None:
            try:
                self._event_log.log_phase_transition(
                    phase=phase,
                    driver_id=did,
                    artifacts=[],
                    quality_signals=metadata or {},
                )
            except Exception:  # noqa: BLE001
                logger.warning("PhaseTracker event_log error", exc_info=True)

        return trans

    # Convenience shortcuts
    def reasoning(self, **kw: Any) -> PhaseTransition:
        return self.transition_to(REASONING, **kw)

    def acting(self, **kw: Any) -> PhaseTransition:
        return self.transition_to(ACTING, **kw)

    def waiting(self, **kw: Any) -> PhaseTransition:
        return self.transition_to(WAITING, **kw)

    def human_review(self, **kw: Any) -> PhaseTransition:
        return self.transition_to(HUMAN_REVIEW, **kw)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def add_hook(self, hook: PhaseHook) -> None:
        """Register a callable invoked on every transition."""
        self._hooks.append(hook)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "current_phase": self._current,
            "driver_id": self._driver_id,
            "transition_count": len(self._history),
            "transitions": [t.to_dict() for t in self._history],
        }

    def summary(self) -> str:
        return (
            f"PhaseTracker(current={self._current}, "
            f"transitions={len(self._history)}, "
            f"driver={self._driver_id!r})"
        )
