"""S2.1 — ProgramEnvelope + BudgetState contracts.

A *program* is the first-class identity for a long-running,
schedulable, pausable, resumable workflow.  Every spawned agent
or DAG task can belong to exactly one program.

Inspired by ThunderAgent's ``program_id`` pattern (dual lifecycle /
functional state) and adapted to MICA's agentic architecture.

Usage::

    from mica.drivers.program_envelope import ProgramEnvelope, BudgetState

    budget = BudgetState(budget_tokens=500_000, budget_usd=2.0)
    env = ProgramEnvelope(
        run_id="run-abc",
        budget_state=budget,
    )
    env = env.with_phase("acting")
    env = env.with_lifecycle("paused", pause_reason="human review needed")
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional


# ── Canonical type aliases ─────────────────────────────────────────────
ProgramPhase = Literal["reasoning", "acting", "waiting", "human_review"]
LifecycleState = Literal["active", "paused", "completed", "failed"]
ResourceClass = Literal["llm", "mcp", "specialist", "external_job"]


# ── BudgetState ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BudgetState:
    """Immutable snapshot of a program's budget consumption.

    All values start at zero.  ``budget_*`` fields are hard caps;
    ``consumed_*`` fields track real usage.  ``ttl_seconds`` is an
    optional wall-clock deadline for the whole program.
    """

    budget_tokens: int = 0
    budget_usd: float = 0.0
    consumed_tokens: int = 0
    consumed_usd: float = 0.0
    ttl_seconds: Optional[int] = None

    # ── queries ────────────────────────────────────────────────────
    @property
    def tokens_remaining(self) -> int:
        if self.budget_tokens <= 0:
            return 0  # uncapped
        return max(0, self.budget_tokens - self.consumed_tokens)

    @property
    def usd_remaining(self) -> float:
        if self.budget_usd <= 0.0:
            return 0.0  # uncapped
        return max(0.0, self.budget_usd - self.consumed_usd)

    @property
    def is_budget_exceeded(self) -> bool:
        """True when any capped budget dimension has been exhausted."""
        if self.budget_tokens > 0 and self.consumed_tokens >= self.budget_tokens:
            return True
        if self.budget_usd > 0.0 and self.consumed_usd >= self.budget_usd:
            return True
        return False

    # ── functional updates ─────────────────────────────────────────
    def consume(self, tokens: int = 0, usd: float = 0.0) -> "BudgetState":
        """Return a new BudgetState with consumption incremented."""
        return replace(
            self,
            consumed_tokens=self.consumed_tokens + tokens,
            consumed_usd=round(self.consumed_usd + usd, 6),
        )

    # ── serialization ──────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "budget_usd": self.budget_usd,
            "consumed_tokens": self.consumed_tokens,
            "consumed_usd": self.consumed_usd,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BudgetState":
        return cls(
            budget_tokens=d.get("budget_tokens", 0),
            budget_usd=d.get("budget_usd", 0.0),
            consumed_tokens=d.get("consumed_tokens", 0),
            consumed_usd=d.get("consumed_usd", 0.0),
            ttl_seconds=d.get("ttl_seconds"),
        )


# ── ProgramEnvelope ────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProgramEnvelope:
    """First-class identity for a schedulable, pausable workflow.

    Implements the dual-state model:
    - ``lifecycle_state``: active / paused / completed / failed
    - ``phase``: reasoning / acting / waiting / human_review

    All mutations return **new** instances (frozen dataclass).
    """

    # identity
    program_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""

    # dual state model
    phase: ProgramPhase = "reasoning"
    lifecycle_state: LifecycleState = "active"

    # resource tracking
    resource_class: ResourceClass = "llm"
    resource_handles: tuple = ()  # immutable list of handle strings

    # pause / resume
    pause_reason: Optional[str] = None
    resume_token: Optional[str] = None

    # budget
    budget_state: BudgetState = field(default_factory=BudgetState)

    # timestamps
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # optional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── lifecycle transitions ──────────────────────────────────────
    def with_lifecycle(
        self,
        state: LifecycleState,
        *,
        pause_reason: Optional[str] = None,
        resume_token: Optional[str] = None,
    ) -> "ProgramEnvelope":
        """Transition lifecycle state (returns new instance)."""
        return replace(
            self,
            lifecycle_state=state,
            pause_reason=pause_reason if state == "paused" else None,
            resume_token=resume_token if state == "paused" else None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def with_phase(self, phase: ProgramPhase) -> "ProgramEnvelope":
        """Transition functional phase (returns new instance)."""
        return replace(
            self,
            phase=phase,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def with_budget(self, budget_state: BudgetState) -> "ProgramEnvelope":
        """Replace budget snapshot (returns new instance)."""
        return replace(
            self,
            budget_state=budget_state,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def add_resource(self, handle: str) -> "ProgramEnvelope":
        """Append a resource handle (returns new instance)."""
        return replace(
            self,
            resource_handles=self.resource_handles + (handle,),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── queries ────────────────────────────────────────────────────
    @property
    def is_active(self) -> bool:
        return self.lifecycle_state == "active"

    @property
    def is_paused(self) -> bool:
        return self.lifecycle_state == "paused"

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle_state in ("completed", "failed")

    @property
    def is_budget_exceeded(self) -> bool:
        return self.budget_state.is_budget_exceeded

    # ── serialisation ──────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "program_id": self.program_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "lifecycle_state": self.lifecycle_state,
            "resource_class": self.resource_class,
            "resource_handles": list(self.resource_handles),
            "pause_reason": self.pause_reason,
            "resume_token": self.resume_token,
            "budget_state": self.budget_state.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata) if self.metadata else {},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProgramEnvelope":
        return cls(
            program_id=d.get("program_id", str(uuid.uuid4())),
            run_id=d.get("run_id", ""),
            phase=d.get("phase", "reasoning"),
            lifecycle_state=d.get("lifecycle_state", "active"),
            resource_class=d.get("resource_class", "llm"),
            resource_handles=tuple(d.get("resource_handles", ())),
            pause_reason=d.get("pause_reason"),
            resume_token=d.get("resume_token"),
            budget_state=BudgetState.from_dict(d.get("budget_state", {})),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=d.get("updated_at", datetime.now(timezone.utc).isoformat()),
            metadata=d.get("metadata", {}),
        )

    # ── JSON helpers ───────────────────────────────────────────────
    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "ProgramEnvelope":
        import json
        return cls.from_dict(json.loads(raw))

    # ── human-readable ─────────────────────────────────────────────
    def summary(self) -> str:
        budget = self.budget_state
        budget_str = (
            f"tok={budget.consumed_tokens}/{budget.budget_tokens} "
            f"usd={budget.consumed_usd:.4f}/{budget.budget_usd:.2f}"
        )
        return (
            f"Program[{self.program_id[:8]}] "
            f"life={self.lifecycle_state} phase={self.phase} "
            f"resources={len(self.resource_handles)} budget=({budget_str})"
        )
