"""
DecisionLedger — Thread-safe structured decision log for MICA runs.
===================================================================

Records every non-trivial decision point — cue evaluations, MSRP phase
activations, quality gate iterations, and promotion gates — so that the
full reasoning trace is reconstructible from the ledger alone.

Usage:
    ledger = DecisionLedger()
    ledger.record(LedgerEntry(
        node="quality_gate",
        decision="iterate",
        cue_triggered="planning_counter_hypothesis_check",
        ...
    ))
    bundle = ledger.to_audit_bundle()

Author: MICA Capability Authority Lab (L-10)
"""

from __future__ import annotations

import json
import threading
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LedgerEntry:
    """Single entry in the decision ledger."""

    node: str  # e.g. "execute", "quality_gate", "promotion", "intake"
    decision: str  # e.g. "continue", "iterate", "pause", "escalate"
    alternatives_considered: List[str] = field(default_factory=list)
    rejection_reasons: List[str] = field(default_factory=list)
    evidence: str = ""
    cue_triggered: Optional[str] = None
    cue_passed: Optional[bool] = None
    msrp_phase_activated: Optional[str] = None
    tokens_spent: int = 0
    quality_score: Optional[float] = None
    iteration: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DecisionLedger:
    """Append-only, thread-safe ledger of runtime decisions.

    Parameters
    ----------
    max_entries : int
        Hard limit on entries to prevent unbounded memory growth.
    """

    def __init__(self, *, max_entries: int = 500) -> None:
        self._entries: List[LedgerEntry] = []
        self._lock = threading.Lock()
        self._max_entries = max_entries

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def record(self, entry: LedgerEntry) -> None:
        """Append an entry to the ledger (thread-safe)."""
        with self._lock:
            if len(self._entries) >= self._max_entries:
                logger.warning(
                    "[LEDGER] Max entries (%d) reached — dropping oldest entry",
                    self._max_entries,
                )
                self._entries.pop(0)
            self._entries.append(entry)

    def record_cue_result(
        self,
        node: str,
        cue_id: str,
        passed: bool,
        evidence: str,
        action: str,
        msrp_phase: Optional[str] = None,
        tokens: int = 0,
        iteration: int = 0,
    ) -> None:
        """Convenience: record a cue evaluation result."""
        self.record(LedgerEntry(
            node=node,
            decision=action,
            evidence=evidence,
            cue_triggered=cue_id,
            cue_passed=passed,
            msrp_phase_activated=msrp_phase,
            tokens_spent=tokens,
            iteration=iteration,
        ))

    def record_quality_gate(
        self,
        decision: str,
        quality_score: float,
        iteration: int,
        *,
        alternatives: Optional[List[str]] = None,
        rejections: Optional[List[str]] = None,
    ) -> None:
        """Convenience: record a quality gate routing decision."""
        self.record(LedgerEntry(
            node="quality_gate",
            decision=decision,
            quality_score=quality_score,
            iteration=iteration,
            alternatives_considered=alternatives or [],
            rejection_reasons=rejections or [],
        ))

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    @property
    def entries(self) -> List[LedgerEntry]:
        """Return snapshot of all entries (read-only list copy)."""
        with self._lock:
            return list(self._entries)

    def count(self) -> int:
        """Total entries recorded."""
        with self._lock:
            return len(self._entries)

    def failures(self) -> List[LedgerEntry]:
        """Return entries where a cue failed."""
        with self._lock:
            return [e for e in self._entries if e.cue_passed is False]

    def total_tokens_spent(self) -> int:
        """Sum of tokens_spent across all entries."""
        with self._lock:
            return sum(e.tokens_spent for e in self._entries)

    def phases_activated(self) -> List[str]:
        """Distinct MSRP phases that were activated."""
        with self._lock:
            return list({
                e.msrp_phase_activated
                for e in self._entries
                if e.msrp_phase_activated
            })

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_audit_bundle(self) -> Dict[str, Any]:
        """Export the full ledger as an audit-ready dict."""
        with self._lock:
            entries_data = [asdict(e) for e in self._entries]
        return {
            "type": "decision_ledger",
            "version": "1.0.0",
            "total_entries": len(entries_data),
            "total_tokens": sum(e.get("tokens_spent", 0) for e in entries_data),
            "failures": sum(1 for e in entries_data if e.get("cue_passed") is False),
            "phases_activated": list({
                e.get("msrp_phase_activated")
                for e in entries_data
                if e.get("msrp_phase_activated")
            }),
            "entries": entries_data,
        }

    def summary(self) -> str:
        """Human-readable summary for logging or dashboard display."""
        bundle = self.to_audit_bundle()
        return (
            f"[DecisionLedger] entries={bundle['total_entries']}, "
            f"failures={bundle['failures']}, "
            f"tokens_spent={bundle['total_tokens']}, "
            f"phases={bundle['phases_activated']}"
        )

    def to_json(self) -> str:
        """Export as JSON string."""
        return json.dumps(self.to_audit_bundle(), indent=2, default=str)

    def clear(self) -> None:
        """Reset the ledger (for testing or new session)."""
        with self._lock:
            self._entries.clear()
