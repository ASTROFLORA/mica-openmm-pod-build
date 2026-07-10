"""
S1.1 — DelegationSession contract.

Frozen dataclass capturing a single delegation lifecycle.  Produced by
``_spawn_agent()`` (S1.2) and consumed by the ``EventLog`` session tracer
(S1.9) and ``coordination_mode`` dispatcher (S1.3).

Design lineage:
    - CrewAI ``TaskHandoff`` pattern
    - ThunderAgent ``DelegateAction`` envelope
    - Agent-Diff state-diff checkpoint contract
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional


# ============================================================================
# Delegation Status Enum
# ============================================================================

class DelegationStatus(str, Enum):
    """Lifecycle states for a delegation session."""

    PENDING     = "pending"       # created, not yet started
    RUNNING     = "running"       # sub-agent is executing
    COMPLETED   = "completed"     # finished successfully
    FAILED      = "failed"        # terminated with error
    TIMED_OUT   = "timed_out"     # exceeded timeout
    CANCELLED   = "cancelled"     # explicitly cancelled
    RESUMING    = "resuming"      # restored from checkpoint

    @property
    def is_terminal(self) -> bool:
        return self in (
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.TIMED_OUT,
            DelegationStatus.CANCELLED,
        )


# ============================================================================
# Coordination Mode Type
# ============================================================================

CoordinationMode = Literal["sequential", "parallel", "consensus"]


# ============================================================================
# DelegationSession — frozen contract
# ============================================================================

@dataclass(frozen=True)
class DelegationSession:
    """Immutable snapshot of a single delegation lifecycle.

    Creating a new session::

        session = DelegationSession(
            parent_run_id="abc-123",
            delegated_agent="structural_biology",
            coordination_mode="sequential",
        )

    Transitioning status (returns a new instance since frozen)::

        running = session.with_status(DelegationStatus.RUNNING)
        done    = running.with_status(DelegationStatus.COMPLETED, result={"text": "..."})
    """

    # ── Identity ───────────────────────────────────────────────────────
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id: str = ""

    # ── Delegation target ──────────────────────────────────────────────
    delegated_agent: str = ""
    agent_capabilities: tuple = ()     # capability tags from ExpertRegistry

    # ── Coordination ───────────────────────────────────────────────────
    coordination_mode: CoordinationMode = "sequential"
    timeout_s: float = 300.0           # max seconds before TIMED_OUT

    # ── Lifecycle ──────────────────────────────────────────────────────
    status: DelegationStatus = DelegationStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None

    # ── Resume / checkpoint ────────────────────────────────────────────
    resume_state: Optional[Dict[str, Any]] = field(default=None, hash=False)
    error_message: Optional[str] = None

    # ── Result ─────────────────────────────────────────────────────────
    result_text: Optional[str] = None
    result_artifact_path: Optional[str] = None  # .md report path

    # ── Provenance ─────────────────────────────────────────────────────
    tool_calls_count: int = 0
    iterations_count: int = 0

    # ─────────────────────────────────────────────────────────────────
    # Transition helpers (frozen → returns new instance)
    # ─────────────────────────────────────────────────────────────────

    def with_status(
        self,
        new_status: DelegationStatus,
        *,
        result_text: Optional[str] = None,
        result_artifact_path: Optional[str] = None,
        error_message: Optional[str] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        tool_calls_count: Optional[int] = None,
        iterations_count: Optional[int] = None,
    ) -> "DelegationSession":
        """Return a new session with updated status + optional fields."""
        overrides: Dict[str, Any] = {"status": new_status}
        if new_status.is_terminal or new_status == DelegationStatus.TIMED_OUT:
            overrides["finished_at"] = datetime.now(timezone.utc).isoformat()
        if result_text is not None:
            overrides["result_text"] = result_text
        if result_artifact_path is not None:
            overrides["result_artifact_path"] = result_artifact_path
        if error_message is not None:
            overrides["error_message"] = error_message
        if resume_state is not None:
            overrides["resume_state"] = resume_state
        if tool_calls_count is not None:
            overrides["tool_calls_count"] = tool_calls_count
        if iterations_count is not None:
            overrides["iterations_count"] = iterations_count
        return self._replace(**overrides)

    def _replace(self, **changes: Any) -> "DelegationSession":
        """Functional update for a frozen dataclass."""
        import dataclasses
        return dataclasses.replace(self, **changes)

    # ─────────────────────────────────────────────────────────────────
    # Serialization
    # ─────────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (JSON-safe)."""
        return {
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "delegated_agent": self.delegated_agent,
            "agent_capabilities": list(self.agent_capabilities),
            "coordination_mode": self.coordination_mode,
            "timeout_s": self.timeout_s,
            "status": self.status.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "resume_state": self.resume_state,
            "error_message": self.error_message,
            "result_text": self.result_text,
            "result_artifact_path": self.result_artifact_path,
            "tool_calls_count": self.tool_calls_count,
            "iterations_count": self.iterations_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DelegationSession":
        """Restore from dict (e.g. JSON checkpoint)."""
        return cls(
            session_id=d.get("session_id", str(uuid.uuid4())),
            parent_run_id=d.get("parent_run_id", ""),
            delegated_agent=d.get("delegated_agent", ""),
            agent_capabilities=tuple(d.get("agent_capabilities", ())),
            coordination_mode=d.get("coordination_mode", "sequential"),
            timeout_s=d.get("timeout_s", 300.0),
            status=DelegationStatus(d.get("status", "pending")),
            created_at=d.get("created_at", ""),
            finished_at=d.get("finished_at"),
            resume_state=d.get("resume_state"),
            error_message=d.get("error_message"),
            result_text=d.get("result_text"),
            result_artifact_path=d.get("result_artifact_path"),
            tool_calls_count=d.get("tool_calls_count", 0),
            iterations_count=d.get("iterations_count", 0),
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        import json
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, s: str) -> "DelegationSession":
        """Restore from JSON string."""
        import json
        return cls.from_dict(json.loads(s))
