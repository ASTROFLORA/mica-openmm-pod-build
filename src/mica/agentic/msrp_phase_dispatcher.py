"""
MSRPPhaseDispatcher — Map cue fail_actions to MSRP thinking phases.
====================================================================

When a cue fails, the dispatcher decides which MSRP phase to activate
and what kind of corrective micro-reasoning should be injected.  This
bridges the gap between passive cues and active MSRP enforcement.

Mapping:
    "pause"                          → Phase 4 (AlternativeConsideration) micro-call
    "revise_plan"                    → Phase 1 (ProblemDecomposition) with updated evidence
    "contradiction_search_required"  → ATOM query + Phase 4
    "request_review"                 → Phase 5 (UncertaintyQuantification) peer feedback
    "warn"                           → Structured log only (no spawn, no phase)

Author: MICA Capability Authority Lab (L-10)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Canonical fail_action → MSRP phase mapping
_FAIL_ACTION_TO_PHASE = {
    "pause": "alternative_consideration",
    "revise_plan": "problem_decomposition",
    "contradiction_search_required": "alternative_consideration",
    "request_review": "uncertainty_quantification",
    "warn": None,  # no MSRP phase — log-only
}

# Approximate token costs for each corrective micro-reasoning
_PHASE_TOKEN_COST = {
    "problem_decomposition": 250,
    "hypothesis_generation": 300,
    "evidence_evaluation": 350,
    "alternative_consideration": 500,
    "uncertainty_quantification": 200,
}


@dataclass
class DispatchResult:
    """Result of dispatching a failed cue to an MSRP phase."""

    cue_id: str
    fail_action: str
    msrp_phase: Optional[str]
    spawn_specialist: bool
    specialist_prompt: Optional[str]
    estimated_tokens: int
    rationale: str


class MSRPPhaseDispatcher:
    """Dispatch failed cues to the appropriate MSRP corrective phase.

    Parameters
    ----------
    max_spawn_tokens : int
        Global token budget cap for spawned micro-reasoning.
        If cumulative cost exceeds this, no more spawns are allowed.
    """

    def __init__(self, *, max_spawn_tokens: int = 3000) -> None:
        self.max_spawn_tokens = max_spawn_tokens
        self._cumulative_tokens: int = 0

    def dispatch(
        self,
        cue_id: str,
        fail_action: str,
        state: Dict[str, Any],
    ) -> DispatchResult:
        """Map a failed cue to an MSRP corrective action.

        Args:
            cue_id: Identifier of the failing cue.
            fail_action: The cue's declared fail_action value.
            state: Current runtime state for context.

        Returns:
            DispatchResult with phase, spawn decision, and prompt.
        """
        msrp_phase = _FAIL_ACTION_TO_PHASE.get(fail_action)
        estimated_tokens = _PHASE_TOKEN_COST.get(msrp_phase or "", 0)

        # Budget guard — prevent runaway token spending
        budget_exceeded = (self._cumulative_tokens + estimated_tokens) > self.max_spawn_tokens
        if budget_exceeded and msrp_phase:
            logger.warning(
                "[MSRP_DISPATCH] Token budget exceeded (%d + %d > %d) for cue %s — downgrade to warn",
                self._cumulative_tokens,
                estimated_tokens,
                self.max_spawn_tokens,
                cue_id,
            )
            return DispatchResult(
                cue_id=cue_id,
                fail_action=fail_action,
                msrp_phase=None,
                spawn_specialist=False,
                specialist_prompt=None,
                estimated_tokens=0,
                rationale=f"Budget exceeded ({self._cumulative_tokens}/{self.max_spawn_tokens}); downgrade to warn",
            )

        # warn → no MSRP activation
        if fail_action == "warn" or msrp_phase is None:
            return DispatchResult(
                cue_id=cue_id,
                fail_action=fail_action,
                msrp_phase=None,
                spawn_specialist=False,
                specialist_prompt=None,
                estimated_tokens=0,
                rationale="Advisory-only: structured log entry",
            )

        # Build specialist prompt for the corrective phase
        prompt = self._build_phase_prompt(msrp_phase, cue_id, fail_action, state)
        self._cumulative_tokens += estimated_tokens

        return DispatchResult(
            cue_id=cue_id,
            fail_action=fail_action,
            msrp_phase=msrp_phase,
            spawn_specialist=True,
            specialist_prompt=prompt,
            estimated_tokens=estimated_tokens,
            rationale=f"Activated {msrp_phase} phase for {fail_action}",
        )

    def cumulative_tokens(self) -> int:
        """Return total tokens consumed by dispatched phases so far."""
        return self._cumulative_tokens

    def reset_budget(self) -> None:
        """Reset cumulative token counter (e.g. at new iteration start)."""
        self._cumulative_tokens = 0

    # ------------------------------------------------------------------
    # Phase prompt builders
    # ------------------------------------------------------------------

    def _build_phase_prompt(
        self,
        phase: str,
        cue_id: str,
        fail_action: str,
        state: Dict[str, Any],
    ) -> str:
        query = str(state.get("user_query", "") or state.get("research_question", ""))
        evidence_summary = str(state.get("evidence_summary", ""))[:500]

        if phase == "problem_decomposition":
            return (
                f"[MSRP Phase-1 Correction] Cue '{cue_id}' failed with action '{fail_action}'.\n"
                f"Original query: {query}\n"
                f"Evidence so far: {evidence_summary}\n"
                "Re-decompose the problem: identify missing sub-questions, "
                "validate assumptions, and list potential confounds that the "
                "current plan overlooked."
            )

        if phase == "alternative_consideration":
            return (
                f"[MSRP Phase-4 Correction] Cue '{cue_id}' triggered '{fail_action}'.\n"
                f"Query: {query}\n"
                f"Evidence: {evidence_summary}\n"
                "Generate at least two competing explanations for the current "
                "findings. For each, state supporting evidence and the single "
                "strongest piece of counter-evidence. Explicitly flag any "
                "contradiction detected."
            )

        if phase == "uncertainty_quantification":
            return (
                f"[MSRP Phase-5 Review] Cue '{cue_id}' requested peer review.\n"
                f"Query: {query}\n"
                f"Evidence: {evidence_summary}\n"
                "Quantify confidence levels for each claim. List residual "
                "unknowns, data limitations, and suggest what additional "
                "evidence would change the conclusion."
            )

        # Generic fallback
        return (
            f"[MSRP Correction] Phase={phase}, cue={cue_id}, action={fail_action}.\n"
            f"Query: {query}\nEvidence: {evidence_summary}\n"
            "Perform the required reasoning correction."
        )
