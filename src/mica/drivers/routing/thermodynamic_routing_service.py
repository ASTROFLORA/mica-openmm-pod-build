"""
Thermodynamic Routing Service — extraction target for DD-CS-008 (Domain G)

Owns temperature calculation, rupture budget bonus, retry plan temperature,
and BioRouter snapshot attachment without changing scientific outputs.
"""

from typing import Any, Dict, Optional


class ThermodynamicRoutingService:
    """Encapsulates thermodynamic / temperature-aware routing policy."""

    def calculate_effective_temperature(
        self,
        *,
        role_spec: Any,
        role_ctx: Any,
    ) -> float:
        """Compute final temperature considering override + rupture budget bonus."""
        _effective_temperature: float = (
            role_spec.temperature_override
            if getattr(role_spec, "temperature_override", None) is not None
            else getattr(role_spec, "temperature", 0.4)
        )
        bonus = 0.0
        if role_ctx is not None and getattr(role_ctx, "applied_rupture_budget", None):
            bonus = float(
                getattr(role_ctx.applied_rupture_budget, "temperature_bonus", 0.0) or 0.0
            )
        return min(_effective_temperature + bonus, 2.0)

    def attach_thermodynamic_route(
        self,
        *,
        result: Dict[str, Any],
        route_state: Dict[str, Any],
    ) -> None:
        """Attach thermodynamic routing metadata to result and runtime/final_result."""
        result["thermodynamic_routing"] = route_state
        runtime_state = result.get("runtime")
        if isinstance(runtime_state, dict):
            runtime_state["thermodynamic_routing"] = route_state
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["thermodynamic_routing"] = route_state

    def build_retry_plan_temperature(
        self,
        *,
        retry_plan: Dict[str, Any],
        current_execution_path: str,
    ) -> Dict[str, Any]:
        """Enrich retry plan with temperature and execution path (parity helper)."""
        return {
            "target_execution_path": str(
                retry_plan.get("retry_execution_path") or current_execution_path
            ),
            "temperature": float(retry_plan.get("temperature") or 0.0),
        }

    def get_thermodynamic_snapshot(self, query: str, *, biorouter: Any, config: Any) -> Optional[Dict[str, Any]]:
        """Return lightweight thermodynamic snapshot using BioRouter (if enabled)."""
        if not getattr(config, "enable_thermodynamic_cognition", False) or biorouter is None:
            return None
        try:
            from mica.cognition.biorouter import CognitiveAttractorState
            lowered = (query or "").lower()
            contradiction_terms = sum(1 for t in ("contradiction", "competing", "rival", "tension", "dispute") if t in lowered)
            exploration_terms = sum(1 for t in ("explore", "investigate", "map", "analyze", "survey") if t in lowered)
            complexity_penalty = min(1.0, 0.18 * contradiction_terms + 0.15 * exploration_terms + min(len(lowered.split()) / 40.0, 0.35))
            semantic_consistency = max(0.2, 0.75 - 0.12 * contradiction_terms - 0.08 * exploration_terms)
            quality_baseline = max(0.25, 0.55 - 0.10 * contradiction_terms)

            soul = CognitiveAttractorState(workflow_id="specialist_dispatch")
            u_energy = biorouter.calculate_hamiltonian_from_scores(
                quality=quality_baseline,
                semantic_consistency=semantic_consistency,
                complexity_penalty=complexity_penalty,
            )
            regulated = biorouter.regulate_temperature(soul, u_energy, stagnation=0.0)
            return {
                "temperature": round(float(regulated.temperature), 4),
            }
        except Exception:
            return None
