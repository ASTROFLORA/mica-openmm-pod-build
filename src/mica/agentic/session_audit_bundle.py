"""
SessionAuditBundle — Export complete forensics for a MICA session.
=================================================================

Gathers decision ledger, cue compliance, quality trajectory, evidence
chain, gap inventory, and cost report into a single JSON-serialisable
bundle.  Written once at session end; consumed by dashboards and post-hoc
audits.

Usage::

    builder = SessionAuditBundleBuilder(session_id="...", run_id="...")
    builder.set_decision_ledger(ledger)
    builder.set_cue_results(results)
    builder.set_quality_trajectory(scores)
    bundle = builder.build()
    json_str = bundle.to_json()

Author: MICA Capability Authority Lab (L-10) — AGENT-A / NewDawn
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class QualityTrajectoryPoint:
    """Single quality measurement in the iteration trajectory."""
    iteration: int
    quality_score: float
    converged: bool
    feedback_summary: str = ""


@dataclass
class SessionAuditBundle:
    """Complete forensic snapshot of a MICA session."""

    session_id: str
    run_id: str
    depth_preset: str
    started_at: str
    completed_at: str

    # Decision ledger (from DecisionLedger.to_audit_bundle())
    decision_ledger: Dict[str, Any] = field(default_factory=dict)

    # Cue compliance
    cue_evaluations: List[Dict[str, Any]] = field(default_factory=list)
    cue_pass_count: int = 0
    cue_fail_count: int = 0
    blocking_failures: List[str] = field(default_factory=list)

    # Quality trajectory across iterations
    quality_trajectory: List[Dict[str, Any]] = field(default_factory=list)
    final_quality_score: float = 0.0

    # Evidence chain (cite_finding calls with provenance)
    evidence_chain: List[Dict[str, Any]] = field(default_factory=list)

    # Gap inventory (identify_gap calls)
    gap_inventory: List[Dict[str, Any]] = field(default_factory=list)

    # Cost report
    total_tokens_spent: int = 0
    total_llm_calls: int = 0
    total_tool_calls: int = 0
    cost_multiplier_used: float = 1.0

    # MSRP phases activated
    msrp_phases_activated: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialise to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)

    def summary_line(self) -> str:
        """One-line summary for logging."""
        return (
            f"[SessionAudit] session={self.session_id} "
            f"depth={self.depth_preset} "
            f"quality={self.final_quality_score:.2f} "
            f"cues={self.cue_pass_count}✓/{self.cue_fail_count}✗ "
            f"tokens={self.total_tokens_spent} "
            f"msrp_phases={self.msrp_phases_activated}"
        )


class SessionAuditBundleBuilder:
    """Incrementally build a SessionAuditBundle during a MICA run."""

    def __init__(self, *, session_id: str, run_id: str, depth_preset: str = "standard") -> None:
        self._session_id = session_id
        self._run_id = run_id
        self._depth_preset = depth_preset
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._decision_ledger: Dict[str, Any] = {}
        self._cue_results: List[Dict[str, Any]] = []
        self._quality_trajectory: List[QualityTrajectoryPoint] = []
        self._evidence_chain: List[Dict[str, Any]] = []
        self._gap_inventory: List[Dict[str, Any]] = []
        self._total_tokens: int = 0
        self._total_llm_calls: int = 0
        self._total_tool_calls: int = 0
        self._cost_multiplier: float = 1.0
        self._msrp_phases: List[str] = []

    def set_decision_ledger(self, ledger_bundle: Dict[str, Any]) -> None:
        self._decision_ledger = ledger_bundle

    def add_cue_result(self, cue_result_dict: Dict[str, Any]) -> None:
        self._cue_results.append(cue_result_dict)

    def add_quality_point(
        self, iteration: int, score: float, converged: bool, feedback: str = ""
    ) -> None:
        self._quality_trajectory.append(
            QualityTrajectoryPoint(iteration, score, converged, feedback)
        )

    def add_evidence(self, evidence: Dict[str, Any]) -> None:
        self._evidence_chain.append(evidence)

    def add_gap(self, gap: Dict[str, Any]) -> None:
        self._gap_inventory.append(gap)

    def add_tokens(self, count: int) -> None:
        self._total_tokens += count

    def increment_llm_calls(self) -> None:
        self._total_llm_calls += 1

    def increment_tool_calls(self) -> None:
        self._total_tool_calls += 1

    def set_cost_multiplier(self, multiplier: float) -> None:
        self._cost_multiplier = multiplier

    def add_msrp_phase(self, phase: str) -> None:
        if phase not in self._msrp_phases:
            self._msrp_phases.append(phase)

    def build(self) -> SessionAuditBundle:
        """Finalise and return the audit bundle."""
        cue_pass = sum(1 for c in self._cue_results if c.get("passed", False))
        cue_fail = len(self._cue_results) - cue_pass
        blocking = [
            c.get("cue_id", "?")
            for c in self._cue_results
            if not c.get("passed", False) and c.get("recommended_action") not in ("warn", "continue")
        ]

        return SessionAuditBundle(
            session_id=self._session_id,
            run_id=self._run_id,
            depth_preset=self._depth_preset,
            started_at=self._started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            decision_ledger=self._decision_ledger,
            cue_evaluations=self._cue_results,
            cue_pass_count=cue_pass,
            cue_fail_count=cue_fail,
            blocking_failures=blocking,
            quality_trajectory=[asdict(p) for p in self._quality_trajectory],
            final_quality_score=(
                self._quality_trajectory[-1].quality_score
                if self._quality_trajectory else 0.0
            ),
            evidence_chain=self._evidence_chain,
            gap_inventory=self._gap_inventory,
            total_tokens_spent=self._total_tokens,
            total_llm_calls=self._total_llm_calls,
            total_tool_calls=self._total_tool_calls,
            cost_multiplier_used=self._cost_multiplier,
            msrp_phases_activated=list(self._msrp_phases),
        )
