"""
mica.drivers.context_coherence
==============================
P2-03: ContextCoherenceScorer + re-plan trigger.

Evaluates workflow execution coherence across four dimensions and emits
ReplanTrigger events when coherence drops below threshold.

Stdlib-only — no third-party deps.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from mica.drivers.contracts import Phase, PhaseTransitionEvent
from mica.infrastructure.event_log import EventLogEntry

if TYPE_CHECKING:
    from mica.toolkg.router import RoutingPlan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_SIGNALS = 200


def _utcnow_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# CoherenceDimension
# ---------------------------------------------------------------------------

class CoherenceDimension(str, enum.Enum):
    """Four scored dimensions that compose overall workflow coherence."""

    ARTIFACT_COVERAGE = "artifact_coverage"
    """Do we have the artifacts each phase expects?"""

    PHASE_PROGRESSION = "phase_progression"
    """Are phases advancing or stalled?"""

    TOOL_ALIGNMENT = "tool_alignment"
    """Are tools producing the artifact types expected?"""

    QUALITY_SIGNAL = "quality_signal"
    """Are quality scores in routing plan converging?"""


# ---------------------------------------------------------------------------
# CoherenceScore
# ---------------------------------------------------------------------------

@dataclass
class CoherenceScore:
    """Snapshot of workflow coherence at a point in time."""

    workflow_id: str
    overall: float
    """0.0–1.0 weighted average of dimension scores."""
    dimensions: dict
    """CoherenceDimension.value → 0.0–1.0 score."""
    low_dimensions: list
    """Dimension ids whose score is below 0.4."""
    computed_at: str
    """ISO timestamp."""
    evidence: dict
    """Free-form dict for explainability."""

    def is_coherent(self, threshold: float = 0.6) -> bool:
        """Return True when overall score meets or exceeds *threshold*."""
        return self.overall >= threshold

    def worst_dimension(self) -> str | None:
        """Return the dimension id with the lowest score, or None if dimensions is empty."""
        if not self.dimensions:
            return None
        return min(self.dimensions, key=lambda k: self.dimensions[k])

    def to_dict(self) -> dict:
        """Serialise to a plain dict."""
        return {
            "workflow_id": self.workflow_id,
            "overall": self.overall,
            "dimensions": dict(self.dimensions),
            "low_dimensions": list(self.low_dimensions),
            "computed_at": self.computed_at,
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# CoherenceSignal
# ---------------------------------------------------------------------------

@dataclass
class CoherenceSignal:
    """Single data point fed into the scorer."""

    signal_type: str
    """'phase_event' | 'artifact' | 'tool_call' | 'routing_plan' | 'error'"""
    payload: dict
    quality_score: float
    """0.0–1.0; provided by the caller."""
    timestamp: str
    """ISO timestamp."""
    driver_id: str


# ---------------------------------------------------------------------------
# ReplanReason
# ---------------------------------------------------------------------------

class ReplanReason(str, enum.Enum):
    """Reason categories for triggering a re-plan."""

    LOW_COHERENCE = "low_coherence"
    STALLED_PHASE = "stalled_phase"
    ARTIFACT_GAP = "artifact_gap"
    REPEATED_FAILURES = "repeated_failures"


# ---------------------------------------------------------------------------
# ReplanTrigger
# ---------------------------------------------------------------------------

@dataclass
class ReplanTrigger:
    """Emitted when coherence drops below acceptable thresholds."""

    workflow_id: str
    reason: ReplanReason
    coherence_score: CoherenceScore
    suggested_replan_from_phase: str
    """Which Phase constant to restart from."""
    detail: str
    triggered_at: str
    """ISO timestamp."""

    def to_dict(self) -> dict:
        """Serialise to a plain dict."""
        return {
            "workflow_id": self.workflow_id,
            "reason": self.reason.value,
            "coherence_score": self.coherence_score.to_dict(),
            "suggested_replan_from_phase": self.suggested_replan_from_phase,
            "detail": self.detail,
            "triggered_at": self.triggered_at,
        }


# ---------------------------------------------------------------------------
# ContextCoherenceScorer
# ---------------------------------------------------------------------------

class ContextCoherenceScorer:
    """
    Accumulates CoherenceSignal instances and scores workflow coherence across
    four dimensions, triggering re-plan events when thresholds are breached.
    """

    def __init__(
        self,
        workflow_id: str,
        replan_threshold: float = 0.5,
        stall_limit: int = 3,
    ) -> None:
        """Initialise scorer for a single workflow run."""
        self._workflow_id = workflow_id
        self._replan_threshold = replan_threshold
        self._stall_limit = stall_limit
        self._signals: list[CoherenceSignal] = []

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def feed(self, signal: CoherenceSignal) -> None:
        """Append *signal* to the internal buffer; keeps at most 200 entries."""
        try:
            self._signals.append(signal)
            if len(self._signals) > _MAX_SIGNALS:
                self._signals = self._signals[-_MAX_SIGNALS:]
        except Exception:  # noqa: BLE001 — never raise from feed
            pass

    # ------------------------------------------------------------------
    # Score
    # ------------------------------------------------------------------

    def score(self, artifacts: list[str] | None = None) -> CoherenceScore:
        """
        Compute a CoherenceScore from all accumulated signals.

        Each dimension degrades gracefully to 0.5 on any internal error.
        """
        artifact_cov = self._score_artifact_coverage(artifacts)
        phase_prog = self._score_phase_progression()
        tool_align = self._score_tool_alignment()
        quality_sig = self._score_quality_signal()

        dimensions = {
            CoherenceDimension.ARTIFACT_COVERAGE.value: artifact_cov,
            CoherenceDimension.PHASE_PROGRESSION.value: phase_prog,
            CoherenceDimension.TOOL_ALIGNMENT.value: tool_align,
            CoherenceDimension.QUALITY_SIGNAL.value: quality_sig,
        }

        dim_values = list(dimensions.values())
        overall = _clamp(sum(dim_values) / len(dim_values) if dim_values else 0.5)

        low_dimensions = [
            dim for dim, s in dimensions.items() if s < 0.4
        ]

        evidence = {
            "signal_count": len(self._signals),
            "phase_event_count": sum(
                1 for s in self._signals if s.signal_type == "phase_event"
            ),
            "tool_call_count": sum(
                1 for s in self._signals if s.signal_type == "tool_call"
            ),
            "error_count": sum(
                1 for s in self._signals if s.signal_type == "error"
            ),
        }

        return CoherenceScore(
            workflow_id=self._workflow_id,
            overall=overall,
            dimensions=dimensions,
            low_dimensions=low_dimensions,
            computed_at=_utcnow_iso(),
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # Should Replan
    # ------------------------------------------------------------------

    def should_replan(
        self, score: CoherenceScore | None = None
    ) -> ReplanTrigger | None:
        """
        Evaluate whether a re-plan should be triggered.

        Returns a ReplanTrigger if any condition is met, else None.
        """
        if score is None:
            score = self.score()

        last_phase = self._last_phase_seen()
        suggested_phase = last_phase or Phase.STRUCTURE_PREPARED

        # (a) Low overall coherence
        if score.overall < self._replan_threshold:
            return ReplanTrigger(
                workflow_id=self._workflow_id,
                reason=ReplanReason.LOW_COHERENCE,
                coherence_score=score,
                suggested_replan_from_phase=suggested_phase,
                detail=(
                    f"Overall coherence {score.overall:.3f} below threshold "
                    f"{self._replan_threshold}"
                ),
                triggered_at=_utcnow_iso(),
            )

        # (b) Repeated failures (last N signals are all errors)
        if len(self._signals) >= self._stall_limit:
            tail = self._signals[-self._stall_limit :]
            if all(s.signal_type == "error" for s in tail):
                return ReplanTrigger(
                    workflow_id=self._workflow_id,
                    reason=ReplanReason.REPEATED_FAILURES,
                    coherence_score=score,
                    suggested_replan_from_phase=suggested_phase,
                    detail=(
                        f"Last {self._stall_limit} signals are all errors"
                    ),
                    triggered_at=_utcnow_iso(),
                )

        # (c) Stalled phase
        phase_val = score.dimensions.get(
            CoherenceDimension.PHASE_PROGRESSION.value, 0.5
        )
        if phase_val < 0.3:
            return ReplanTrigger(
                workflow_id=self._workflow_id,
                reason=ReplanReason.STALLED_PHASE,
                coherence_score=score,
                suggested_replan_from_phase=suggested_phase,
                detail=(
                    f"Phase progression score {phase_val:.3f} below 0.3 — "
                    "workflow may be stalled"
                ),
                triggered_at=_utcnow_iso(),
            )

        # (d) Artifact gap
        artifact_val = score.dimensions.get(
            CoherenceDimension.ARTIFACT_COVERAGE.value, 0.5
        )
        if artifact_val < 0.3:
            return ReplanTrigger(
                workflow_id=self._workflow_id,
                reason=ReplanReason.ARTIFACT_GAP,
                coherence_score=score,
                suggested_replan_from_phase=suggested_phase,
                detail=(
                    f"Artifact coverage score {artifact_val:.3f} below 0.3"
                ),
                triggered_at=_utcnow_iso(),
            )

        return None

    # ------------------------------------------------------------------
    # Feed helpers
    # ------------------------------------------------------------------

    def feed_from_event_log(self, entries: list[EventLogEntry]) -> int:
        """
        Convert EventLogEntry objects to CoherenceSignals and feed them.

        Returns the number of signals fed.
        """
        count = 0
        for entry in entries:
            try:
                etype = entry.event_type
                if etype == "phase_transition":
                    stype = "phase_event"
                    quality = entry.payload.get(
                        "quality_signals", {}
                    ).get("overall", 0.6)
                elif etype == "tool_call":
                    stype = "tool_call"
                    quality = entry.payload.get("quality_score", 0.7)
                elif etype == "error":
                    stype = "error"
                    quality = 0.0
                # Expert pool events (2026-03-05)
                elif etype in ("expert_consulted", "expert_gap_identified", "expert_finding_cited"):
                    stype = "domain_deep_dive"
                    quality = entry.payload.get("quality_score", 0.75)
                # Debate engine events (2026-03-05)
                elif etype in ("debate_started", "debate_turn", "debate_finished"):
                    stype = "hypothesis_validation"
                    quality = entry.payload.get("quality_score", 0.8)
                else:
                    stype = etype
                    quality = 0.5

                signal = CoherenceSignal(
                    signal_type=stype,
                    payload=dict(entry.payload),
                    quality_score=float(quality),
                    timestamp=entry.timestamp,
                    driver_id=entry.driver_id,
                )
                self.feed(signal)
                count += 1
            except Exception:  # noqa: BLE001
                pass
        return count

    def feed_phase_event(self, event: PhaseTransitionEvent) -> None:
        """Build a CoherenceSignal from a PhaseTransitionEvent and feed it."""
        try:
            quality = float(event.quality_signals.get("overall", 0.7))
            signal = CoherenceSignal(
                signal_type="phase_event",
                payload=event.to_dict(),
                quality_score=quality,
                timestamp=event.timestamp.isoformat(),
                driver_id=event.driver_id,
            )
            self.feed(signal)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all accumulated signals."""
        self._signals = []

    def signal_count(self) -> int:
        """Return the number of accumulated signals."""
        return len(self._signals)

    def to_dict(self) -> dict:
        """Serialise scorer state to a plain dict."""
        return {
            "workflow_id": self._workflow_id,
            "replan_threshold": self._replan_threshold,
            "stall_limit": self._stall_limit,
            "signal_count": len(self._signals),
        }

    # ------------------------------------------------------------------
    # Private scoring helpers
    # ------------------------------------------------------------------

    def _score_artifact_coverage(self, artifacts: list[str] | None) -> float:
        """Compute artifact_coverage dimension score."""
        try:
            artifact_signals = [
                s for s in self._signals if s.signal_type == "artifact"
            ]
            seen_types: set[str] = set()
            for s in self._signals:
                atype = s.payload.get("artifact_type") or s.payload.get("type")
                if atype:
                    seen_types.add(str(atype))

            if artifacts:
                provided = [a for a in artifacts if a]
                if not provided:
                    # fall through to baseline
                    pass
                else:
                    matched = sum(1 for a in provided if a in seen_types)
                    return _clamp(matched / len(provided))

            # Baseline 0.5 + 0.1 per unique artifact type seen; cap at 1.0
            score = 0.5 + 0.1 * len(seen_types)
            return _clamp(score)
        except Exception:  # noqa: BLE001
            return 0.5

    def _score_phase_progression(self) -> float:
        """Compute phase_progression dimension score."""
        try:
            phase_signals = [
                s for s in self._signals if s.signal_type == "phase_event"
            ]
            if not phase_signals:
                return 0.5

            unique_phases: set[str] = set()
            for s in phase_signals:
                phase_val = (
                    s.payload.get("phase")
                    or s.payload.get("phase_name")
                    or ""
                )
                if phase_val:
                    unique_phases.add(str(phase_val))

            base_score = min(1.0, len(unique_phases) / 4.0)

            # Stall penalty: last 3 signals all same phase
            if len(self._signals) >= 3:
                tail = self._signals[-3:]
                if all(s.signal_type == "phase_event" for s in tail):
                    tail_phases = set()
                    for s in tail:
                        p = (
                            s.payload.get("phase")
                            or s.payload.get("phase_name")
                            or ""
                        )
                        tail_phases.add(p)
                    if len(tail_phases) == 1:
                        base_score = max(0.1, base_score - 0.3)

            return _clamp(base_score)
        except Exception:  # noqa: BLE001
            return 0.5

    def _score_tool_alignment(self) -> float:
        """Compute tool_alignment dimension score."""
        try:
            tool_signals = [
                s for s in self._signals if s.signal_type == "tool_call"
            ]
            if not tool_signals:
                return 0.5
            good = sum(1 for s in tool_signals if s.quality_score >= 0.5)
            return _clamp(good / len(tool_signals))
        except Exception:  # noqa: BLE001
            return 0.5

    def _score_quality_signal(self) -> float:
        """Compute quality_signal dimension score as mean quality across all signals."""
        try:
            if not self._signals:
                return 0.5
            avg = sum(s.quality_score for s in self._signals) / len(self._signals)
            return _clamp(avg)
        except Exception:  # noqa: BLE001
            return 0.5

    def _last_phase_seen(self) -> str | None:
        """Return the phase value from the most recent phase_event signal, or None."""
        for s in reversed(self._signals):
            if s.signal_type == "phase_event":
                phase = (
                    s.payload.get("phase")
                    or s.payload.get("phase_name")
                    or ""
                )
                if phase:
                    return str(phase)
        return None
