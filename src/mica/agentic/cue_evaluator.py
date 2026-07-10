"""
CueEvaluator — Active protocol cue enforcement for MICA.
=========================================================

Converts inert protocol cue dicts into runtime enforcement triggers.
Each cue evaluation produces a CueResult with pass/fail, evidence,
recommended action, and whether a sub-agent spawn is warranted.

Integration points:
    - Pre/post tool calls in the execute node
    - Quality gate iterations
    - Promotion gates before GraphRAG commits

Design constraints:
    - max_cue_depth prevents infinite recursion when a cue spawns
      a sub-agent that may itself trigger cues.
    - Cost-aware: each evaluation annotates estimated token cost.

Author: MICA Capability Authority Lab (L-10)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass
class CueResult:
    """Outcome of evaluating a single protocol cue against runtime state."""

    cue_id: str
    passed: bool
    evidence: str
    recommended_action: str  # "continue" | "pause" | "revise_plan" | "contradiction_search" | "warn"
    spawn_agent: bool = False
    phase_triggered: Optional[str] = None  # MSRP phase to activate on failure
    estimated_tokens: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


MICAState = Dict[str, Any]


class CueEvaluator:
    """Evaluate protocol cues against live MICA workflow state.

    Parameters
    ----------
    max_cue_depth : int
        Hard cap on recursive cue evaluation depth.  Prevents infinite
        spawn chains when a sub-agent fails its own cues.
    atom_memory : optional
        ``ATOMMemorySystem`` instance for contradiction queries.
    graph_store : optional
        GraphRAG store for evidence retrieval queries.
    """

    def __init__(
        self,
        *,
        max_cue_depth: int = 2,
        atom_memory: Any = None,
        graph_store: Any = None,
    ) -> None:
        if max_cue_depth < 1:
            raise ValueError("max_cue_depth must be >= 1")
        self.max_cue_depth = max_cue_depth
        self.atom_memory = atom_memory
        self.graph_store = graph_store
        # R25.5: governance-side subscriber surface. Populated by bind_event_bus().
        # Stores the last observed snapshot-confidence distribution so evaluators
        # can react to memory state WITHOUT the driver having to pass it through.
        self._last_snapshot_confidence: Optional[Dict[str, Any]] = None
        self._snapshot_events_seen: int = 0

    # ------------------------------------------------------------------
    # R25.5: non-driver subscriber to SnapshotPersisted
    # ------------------------------------------------------------------

    def bind_event_bus(self, bus: Any) -> None:
        """Subscribe this evaluator to ``SnapshotPersisted`` events.

        This is the governance-side activation of the w=988 driver↔events
        edge as a true cross-community bus: the evaluator now receives
        memory-community signals without the driver as intermediary.
        Graph witness: adds a new inbound edge to ``cue_evaluator`` (deg 0→1+).
        """
        try:
            from .events import SnapshotPersisted
        except ImportError:
            logger.debug("[CUE_EVAL] events.SnapshotPersisted unavailable; subscription skipped")
            return
        bus.subscribe(SnapshotPersisted, self._on_snapshot_persisted)

    def _on_snapshot_persisted(self, event: Any) -> None:
        """Record the confidence distribution of the most recent snapshot."""
        self._snapshot_events_seen += 1
        self._last_snapshot_confidence = {
            "snapshot_id": getattr(event, "snapshot_id", ""),
            "user_id": getattr(event, "user_id", "default"),
            "mu": float(getattr(event, "mu", 0.0)),
            "sigma": float(getattr(event, "sigma", 0.0)),
            "contributing": int(getattr(event, "contributing_quintuples", 0)),
            "empty_fallback": bool(getattr(event, "empty_fallback", False)),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.debug(
            "[CUE_EVAL] snapshot %s mu=%.3f sigma=%.3f contrib=%d",
            self._last_snapshot_confidence["snapshot_id"],
            self._last_snapshot_confidence["mu"],
            self._last_snapshot_confidence["sigma"],
            self._last_snapshot_confidence["contributing"],
        )

    @property
    def last_snapshot_confidence(self) -> Optional[Dict[str, Any]]:
        """Last observed ``SnapshotPersisted`` payload (or ``None``)."""
        return self._last_snapshot_confidence

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_cue(
        self,
        cue: Dict[str, Any],
        state: MICAState,
        *,
        current_depth: int = 0,
    ) -> CueResult:
        """Evaluate a single cue against runtime state.

        Args:
            cue: Protocol cue dict (matches ProtocolCue fields).
            state: Current MICAState snapshot.
            current_depth: Recursion depth counter (caller sets this).

        Returns:
            CueResult with pass/fail, evidence trail, and action.
        """
        cue_id = str(cue.get("cue_id", "unknown"))
        fail_action = str(cue.get("fail_action", "warn"))
        phase = str(cue.get("phase", ""))
        question = str(cue.get("question", ""))
        mode = str(cue.get("mode", "advisory"))

        # Depth guard — prevents infinite cue→spawn→cue loops
        if current_depth >= self.max_cue_depth:
            logger.warning(
                "[CUE_EVAL] Depth limit (%d) reached for cue %s — auto-pass",
                self.max_cue_depth,
                cue_id,
            )
            return CueResult(
                cue_id=cue_id,
                passed=True,
                evidence=f"Auto-passed: max_cue_depth={self.max_cue_depth} reached",
                recommended_action="continue",
                spawn_agent=False,
                estimated_tokens=0,
            )

        # Dispatch to phase-specific evaluator
        if fail_action == "contradiction_search_required":
            return self._evaluate_contradiction_cue(cue_id, question, state, current_depth)

        if phase == "intake":
            return self._evaluate_intake_cue(cue_id, question, state)

        if phase == "planning":
            return self._evaluate_planning_cue(cue_id, question, state)

        if phase in ("pre_tool", "post_tool"):
            return self._evaluate_tool_cue(cue_id, question, phase, state)

        if phase == "promotion":
            return self._evaluate_promotion_cue(cue_id, question, state, current_depth)

        # Fallback: advisory pass with warning
        return CueResult(
            cue_id=cue_id,
            passed=True,
            evidence=f"No evaluator for phase={phase}; advisory pass",
            recommended_action="continue",
            estimated_tokens=0,
        )

    def evaluate_batch(
        self,
        cues: Sequence[Dict[str, Any]],
        state: MICAState,
        *,
        phase_filter: Optional[str] = None,
        current_depth: int = 0,
    ) -> List[CueResult]:
        """Evaluate multiple cues, optionally filtered by phase.

        Returns list of CueResults.  Blocking failures appear first.
        """
        filtered = cues
        if phase_filter:
            filtered = [c for c in cues if c.get("phase") == phase_filter]

        results: List[CueResult] = []
        for cue in filtered:
            results.append(self.evaluate_cue(cue, state, current_depth=current_depth))

        # Sort: failures first, then by priority ordering
        priority_rank = {"critical": 0, "high": 1, "normal": 2}
        results.sort(
            key=lambda r: (
                0 if not r.passed else 1,
                priority_rank.get(str(r.cue_id), 2),
            )
        )
        return results

    def has_blocking_failure(self, results: Sequence[CueResult]) -> bool:
        """True if any result is a blocking failure."""
        return any(not r.passed and r.recommended_action != "warn" for r in results)

    # ------------------------------------------------------------------
    # Phase-specific evaluators
    # ------------------------------------------------------------------

    def _evaluate_intake_cue(
        self, cue_id: str, question: str, state: MICAState
    ) -> CueResult:
        """Intake: is the query scientifically scoped?"""
        query = str(state.get("user_query", "") or state.get("research_question", ""))
        # Heuristic: query must be >15 chars and contain at least one domain keyword
        domain_keywords = {
            "protein", "kinase", "gene", "pathway", "mutation", "structure",
            "binding", "expression", "allosteric", "receptor", "enzyme",
            "inhibitor", "signaling", "phosphorylation", "domain", "residue",
            "molecular", "cellular", "therapeutic", "clinical", "mechanism",
        }
        query_lower = query.lower()
        has_domain = any(kw in query_lower for kw in domain_keywords)
        passed = len(query) > 15 and has_domain

        return CueResult(
            cue_id=cue_id,
            passed=passed,
            evidence=f"query_len={len(query)}, domain_match={has_domain}",
            recommended_action="continue" if passed else "pause",
            spawn_agent=False,
            phase_triggered="problem_decomposition" if not passed else None,
            estimated_tokens=0,
        )

    def _evaluate_planning_cue(
        self, cue_id: str, question: str, state: MICAState
    ) -> CueResult:
        """Planning: has the plan preserved a competing explanation?"""
        subtasks = state.get("subtasks") or []
        hypotheses = state.get("hypotheses") or []
        plan_text = str(state.get("plan_summary", ""))

        competing_keywords = {
            "alternative", "competing", "rival", "counter", "falsif",
            "hypothesis", "null", "versus",
        }
        plan_lower = plan_text.lower()
        has_competing = any(kw in plan_lower for kw in competing_keywords)
        has_multiple_hypotheses = len(hypotheses) >= 2

        passed = has_competing or has_multiple_hypotheses or len(subtasks) >= 3

        return CueResult(
            cue_id=cue_id,
            passed=passed,
            evidence=(
                f"competing_keyword={has_competing}, "
                f"hypotheses={len(hypotheses)}, "
                f"subtasks={len(subtasks)}"
            ),
            recommended_action="continue" if passed else "revise_plan",
            spawn_agent=not passed,
            phase_triggered="hypothesis_generation" if not passed else None,
            estimated_tokens=200 if not passed else 0,
        )

    def _evaluate_tool_cue(
        self, cue_id: str, question: str, phase: str, state: MICAState
    ) -> CueResult:
        """Pre/post tool: did the tool produce an actionable artifact?"""
        lab_reports = state.get("lab_reports") or []
        if not lab_reports:
            return CueResult(
                cue_id=cue_id,
                passed=phase == "pre_tool",  # pre_tool passes with no reports yet
                evidence="no_lab_reports" if phase == "post_tool" else "pre_tool_check",
                recommended_action="warn" if phase == "post_tool" else "continue",
                estimated_tokens=0,
            )

        last_report = lab_reports[-1] if lab_reports else {}
        findings = str(last_report.get("findings", ""))
        confidence = float(last_report.get("confidence", 0.0))

        passed = len(findings) > 50 and confidence > 0.3

        return CueResult(
            cue_id=cue_id,
            passed=passed,
            evidence=f"findings_len={len(findings)}, confidence={confidence:.2f}",
            recommended_action="continue" if passed else "warn",
            estimated_tokens=0,
        )

    def _evaluate_contradiction_cue(
        self,
        cue_id: str,
        question: str,
        state: MICAState,
        current_depth: int,
    ) -> CueResult:
        """Promotion: contradiction search via ATOM memory."""
        claims = state.get("claims") or state.get("conclusions") or []
        lab_reports = state.get("lab_reports") or []

        # Check if claims expose contradictions, missing controls, next steps
        has_contradiction_check = False
        has_next_step = False
        for claim in (claims if isinstance(claims, list) else []):
            claim_text = str(claim.get("text", "") if isinstance(claim, dict) else claim).lower()
            if "contradict" in claim_text or "inconsisten" in claim_text:
                has_contradiction_check = True
            if "next" in claim_text or "future" in claim_text or "further" in claim_text:
                has_next_step = True

        # If ATOM memory available, query for contradictions
        atom_contradictions: List[str] = []
        if self.atom_memory is not None and lab_reports:
            try:
                graph = getattr(self.atom_memory, "current_graph", None)
                if graph is not None:
                    quintuples = getattr(graph, "quintuples", [])
                    # Simple contradiction scan: look for opposing relations
                    seen: Dict[str, str] = {}
                    for q in quintuples:
                        key = f"{getattr(q, 'subject', '')}-{getattr(q, 'object', '')}"
                        rel = str(getattr(q, "relation", ""))
                        if key in seen and seen[key] != rel:
                            atom_contradictions.append(
                                f"Conflicting relations for {key}: {seen[key]} vs {rel}"
                            )
                        seen[key] = rel
            except Exception as exc:
                logger.warning("[CUE_EVAL] ATOM contradiction scan failed: %s", exc)

        passed = has_contradiction_check and has_next_step and not atom_contradictions

        return CueResult(
            cue_id=cue_id,
            passed=passed,
            evidence=(
                f"contradiction_check={has_contradiction_check}, "
                f"next_step={has_next_step}, "
                f"atom_contradictions={len(atom_contradictions)}"
            ),
            recommended_action="continue" if passed else "contradiction_search",
            spawn_agent=not passed and current_depth < self.max_cue_depth,
            phase_triggered="alternative_consideration" if not passed else None,
            estimated_tokens=500 if not passed else 0,
        )

    def _evaluate_promotion_cue(
        self,
        cue_id: str,
        question: str,
        state: MICAState,
        current_depth: int,
    ) -> CueResult:
        """Promotion gate: block unsupported conclusions before GraphRAG commit."""
        quality_score = float(state.get("quality_score", 0.0))
        claims = state.get("claims") or []
        lab_reports = state.get("lab_reports") or []

        has_evidence = len(lab_reports) > 0
        quality_sufficient = quality_score >= 0.7
        has_claims = len(claims) > 0 if isinstance(claims, list) else bool(claims)

        passed = has_evidence and quality_sufficient and has_claims

        return CueResult(
            cue_id=cue_id,
            passed=passed,
            evidence=(
                f"quality={quality_score:.2f}, "
                f"evidence_count={len(lab_reports)}, "
                f"claims={has_claims}"
            ),
            recommended_action="continue" if passed else "contradiction_search",
            spawn_agent=not passed and current_depth < self.max_cue_depth,
            phase_triggered="uncertainty_quantification" if not passed else None,
            estimated_tokens=300 if not passed else 0,
        )
