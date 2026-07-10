"""
mica.drivers.research_phase_context
=====================================
Phase-aware capability gating for the MICA research pipeline (P1-08).

Tracks the current research phase, gates which capabilities the
QueryIntentRouter may use, and advances phase based on PhaseTransitionEvent
arrivals.

Anti-rigidity contract:
- Phase gating DEGRADES gracefully (warn + allow) rather than hard-blocking
  when admissibility data is absent (no CapabilityNode or empty admissibility).
- Terminal events (FAILED / DEGRADED) always lock the context to COMPLETE.

Standard-library only — no third-party deps.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .contracts import Phase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ResearchPhase enum
# ---------------------------------------------------------------------------

class ResearchPhase(str, Enum):
    """High-level research lifecycle phases used for capability gating."""
    EXPLORATION          = "exploration"           # initial query, literature, protein lookup
    STRUCTURE_ACQUISITION = "structure_acquisition" # PDB / AlphaFold fetching
    VIRTUAL_SCREENING    = "virtual_screening"     # SMILES, docking, QSAR
    LEAD_OPTIMIZATION    = "lead_optimization"     # refine hits, binding affinity
    MD_VALIDATION        = "md_validation"         # MD simulation running / monitoring
    ANALYSIS             = "analysis"              # trajectory analysis, result synthesis
    COMPLETE             = "complete"              # final result ready (terminal)


# ---------------------------------------------------------------------------
# PhaseTransition dataclass
# ---------------------------------------------------------------------------

@dataclass
class PhaseTransition:
    """Record of a single phase advancement."""
    from_phase: ResearchPhase
    to_phase: ResearchPhase
    trigger_event_type: str     # e.g. "phase_transition"
    trigger_phase: str          # the Phase.* constant value that fired the transition
    timestamp: str              # ISO UTC datetime string
    workflow_id: str = ""


# ---------------------------------------------------------------------------
# PhaseGateResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class PhaseGateResult:
    """Result of a single capability gate check."""
    capability_id: str
    allowed: bool
    current_phase: ResearchPhase
    reason: str     # one of the four reason constants below
    degraded: bool = False

    # Reason constants for callers to branch on without string literals
    R_ADMISSIBLE_ALL    = "admissible_all_phases"
    R_ADMISSIBLE_PHASE  = "admissible_in_phase"
    R_BLOCKED           = "blocked_wrong_phase"
    R_DEGRADED          = "degraded_no_admissibility_data"


# ---------------------------------------------------------------------------
# ResearchPhaseContext
# ---------------------------------------------------------------------------

class ResearchPhaseContext:
    """
    Phase-aware capability gating context.

    Tracks the current research phase and gates RoutingPlan capabilities
    through phase admissibility metadata on CapabilityNode.

    Thread-safety: not thread-safe; callers should serialise access.
    """

    # ------------------------------------------------------------------
    # Phase advancement map
    # (current_phase, trigger_phase_value) → next ResearchPhase
    # ------------------------------------------------------------------
    _PHASE_ADVANCEMENT_MAP: dict[tuple[ResearchPhase, str], ResearchPhase] = {
        (ResearchPhase.EXPLORATION,           Phase.STRUCTURE_PREPARED):    ResearchPhase.STRUCTURE_ACQUISITION,
        (ResearchPhase.EXPLORATION,           Phase.ARTIFACTS_INVENTORIED): ResearchPhase.STRUCTURE_ACQUISITION,
        (ResearchPhase.STRUCTURE_ACQUISITION, Phase.LIGAND_RESOLVED):       ResearchPhase.VIRTUAL_SCREENING,
        (ResearchPhase.VIRTUAL_SCREENING,     Phase.DOCKING_COMPLETE):      ResearchPhase.LEAD_OPTIMIZATION,
        (ResearchPhase.LEAD_OPTIMIZATION,     Phase.SIMULATION_QUEUED):     ResearchPhase.MD_VALIDATION,
        (ResearchPhase.MD_VALIDATION,         Phase.SIMULATION_COMPLETE):   ResearchPhase.ANALYSIS,
        (ResearchPhase.ANALYSIS,              Phase.ANALYSIS_COMPLETE):     ResearchPhase.COMPLETE,
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        workflow_id: str,
        initial_phase: ResearchPhase = ResearchPhase.EXPLORATION,
    ) -> None:
        self.workflow_id: str = workflow_id
        self.current_phase: ResearchPhase = initial_phase
        self._phase_history: list[PhaseTransition] = []
        self._event_count: int = 0

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def current_phase_value(self) -> str:
        """Return the string value of the current phase."""
        return self.current_phase.value

    def is_complete(self) -> bool:
        """Return True when at the terminal COMPLETE phase."""
        return self.current_phase == ResearchPhase.COMPLETE

    def phase_history(self) -> list[PhaseTransition]:
        """Return a shallow copy of the phase transition history."""
        return list(self._phase_history)

    def to_dict(self) -> dict:
        """Serialise current state to a plain dict."""
        return {
            "workflow_id": self.workflow_id,
            "current_phase": self.current_phase.value,
            "event_count": self._event_count,
            "history": [
                {
                    "from_phase": t.from_phase.value,
                    "to_phase": t.to_phase.value,
                    "trigger_event_type": t.trigger_event_type,
                    "trigger_phase": t.trigger_phase,
                    "timestamp": t.timestamp,
                    "workflow_id": t.workflow_id,
                }
                for t in self._phase_history
            ],
        }

    # ------------------------------------------------------------------
    # Phase advancement
    # ------------------------------------------------------------------

    def advance(self, event_type: str, phase_value: str) -> PhaseTransition | None:
        """
        Attempt to advance the current phase based on an incoming event.

        Always increments the internal event counter.

        Returns a PhaseTransition if a transition occurred, else None.
        Terminal overrides: FAILED or DEGRADED always transitions to COMPLETE.
        """
        self._event_count += 1

        # Terminal override — any failed / degraded signal locks to COMPLETE.
        if phase_value in (Phase.FAILED, Phase.DEGRADED):
            if self.current_phase != ResearchPhase.COMPLETE:
                return self._make_transition(
                    event_type=event_type,
                    phase_value=phase_value,
                    to_phase=ResearchPhase.COMPLETE,
                )
            return None

        # Normal map lookup
        key = (self.current_phase, phase_value)
        next_phase = self._PHASE_ADVANCEMENT_MAP.get(key)
        if next_phase is None:
            logger.debug(
                "ResearchPhaseContext[%s]: no transition for (%s, %s)",
                self.workflow_id, self.current_phase.value, phase_value,
            )
            return None

        return self._make_transition(
            event_type=event_type,
            phase_value=phase_value,
            to_phase=next_phase,
        )

    def _make_transition(
        self,
        event_type: str,
        phase_value: str,
        to_phase: ResearchPhase,
    ) -> PhaseTransition:
        """Internal helper: build, record, and apply a transition."""
        transition = PhaseTransition(
            from_phase=self.current_phase,
            to_phase=to_phase,
            trigger_event_type=event_type,
            trigger_phase=phase_value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            workflow_id=self.workflow_id,
        )
        self._phase_history.append(transition)
        self.current_phase = to_phase
        logger.info(
            "ResearchPhaseContext[%s]: %s → %s (trigger=%s)",
            self.workflow_id, transition.from_phase.value,
            transition.to_phase.value, phase_value,
        )
        return transition

    def advance_from_phase_event(self, phase_event: Any) -> PhaseTransition | None:
        """
        Convenience method: extract `phase` from a PhaseTransitionEvent-like
        object and call advance().

        Accepts any object that has a `.phase` attribute (duck-typing).
        """
        phase_value: str = getattr(phase_event, "phase", "")
        if not phase_value:
            logger.warning(
                "ResearchPhaseContext[%s]: advance_from_phase_event received "
                "an object with no `.phase` attribute: %r",
                self.workflow_id, phase_event,
            )
            return None
        return self.advance(event_type="phase_transition", phase_value=phase_value)

    # ------------------------------------------------------------------
    # Capability gating
    # ------------------------------------------------------------------

    def gate_capability(
        self,
        capability_id: str,
        capability_node: Any | None,
    ) -> PhaseGateResult:
        """
        Gate a single capability against the current phase.

        Degrades gracefully:
        - None node                    → allowed=True, degraded=True
        - missing phase_admissibility  → allowed=True (admissible in all phases)
        - admissibility data present   → strict check
        """
        # Degrade gracefully: no node means we cannot gate → allow with warning.
        if capability_node is None:
            warnings.warn(
                f"ResearchPhaseContext[{self.workflow_id}]: capability "
                f"'{capability_id}' has no CapabilityNode — gating degraded "
                f"(allowing by default).",
                stacklevel=2,
            )
            return PhaseGateResult(
                capability_id=capability_id,
                allowed=True,
                current_phase=self.current_phase,
                reason=PhaseGateResult.R_DEGRADED,
                degraded=True,
            )

        phase_admissibility: list[str] = getattr(
            capability_node, "phase_admissibility", []
        )

        # Empty list → admissible in all phases.
        if not phase_admissibility:
            return PhaseGateResult(
                capability_id=capability_id,
                allowed=True,
                current_phase=self.current_phase,
                reason=PhaseGateResult.R_ADMISSIBLE_ALL,
                degraded=False,
            )

        # Check whether current phase is in the admissibility list.
        if self.current_phase.value in phase_admissibility:
            return PhaseGateResult(
                capability_id=capability_id,
                allowed=True,
                current_phase=self.current_phase,
                reason=PhaseGateResult.R_ADMISSIBLE_PHASE,
                degraded=False,
            )

        return PhaseGateResult(
            capability_id=capability_id,
            allowed=False,
            current_phase=self.current_phase,
            reason=PhaseGateResult.R_BLOCKED,
            degraded=False,
        )

    def gate_routing_plan(
        self,
        plan: Any,
        registry: Any,
    ) -> tuple[Any, list[PhaseGateResult]]:
        """
        Filter a RoutingPlan's planned_tools through phase gating.

        For each PlannedToolCall in plan.planned_tools:
        - Look up the CapabilityNode in registry.capabilities.
        - Gate it via gate_capability().
        - Allowed tools remain; blocked tools are moved to skipped_capabilities
          and the plan is flagged degraded=True.

        Returns a new RoutingPlan (all other fields copied) and the full list
        of PhaseGateResult objects.

        Avoids importing RoutingPlan at module level to prevent circular deps.
        """
        from ..toolkg.router import RoutingPlan  # local import — avoids circular

        gate_results: list[PhaseGateResult] = []
        allowed_tools = []
        extra_skipped: list[str] = []
        plan_degraded: bool = plan.degraded

        for ptc in plan.planned_tools:
            cap_id: str = ptc.capability_id
            cap_node = registry.capabilities.get(cap_id)  # may be None
            result = self.gate_capability(cap_id, cap_node)
            gate_results.append(result)

            if result.allowed:
                allowed_tools.append(ptc)
            else:
                extra_skipped.append(cap_id)
                plan_degraded = True
                logger.info(
                    "ResearchPhaseContext[%s]: blocked capability '%s' "
                    "(phase=%s, reason=%s)",
                    self.workflow_id, cap_id,
                    self.current_phase.value, result.reason,
                )

        new_skipped = list(plan.skipped_capabilities) + extra_skipped

        filtered_plan = RoutingPlan(
            query=plan.query,
            planned_tools=allowed_tools,
            skipped_capabilities=new_skipped,
            degraded=plan_degraded,
            available_artifacts=list(plan.available_artifacts),
            intent_tags=list(plan.intent_tags),
        )
        return filtered_plan, gate_results
