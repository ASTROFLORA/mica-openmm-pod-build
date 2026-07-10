"""Thermodynamic routing helpers extracted from AgenticDriver."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from mica.scientific_workflow.mudo_envelope import CognitiveAttractorState


def _compute_thermodynamic_signals(query: str) -> Dict[str, Any]:
    lowered = str(query or "").lower()
    contradiction_terms = sum(
        1 for token in ("contradiction", "competing", "rival", "tension", "dispute") if token in lowered
    )
    exploration_terms = sum(
        1 for token in ("explore", "investigate", "map", "analyze", "survey") if token in lowered
    )
    complexity_penalty = min(
        1.0,
        0.18 * contradiction_terms + 0.15 * exploration_terms + min(len(lowered.split()) / 40.0, 0.35),
    )
    semantic_consistency = max(0.2, 0.75 - (0.12 * contradiction_terms) - (0.08 * exploration_terms))
    quality_baseline = max(0.25, 0.55 - (0.10 * contradiction_terms))
    return {
        "complexity_penalty": float(complexity_penalty),
        "semantic_consistency": float(semantic_consistency),
        "quality_baseline": float(quality_baseline),
        "contradiction_terms": int(contradiction_terms),
        "exploration_terms": int(exploration_terms),
    }


def _regulate_query_state(*, query: str, workflow_id: str, biorouter_obj: Any) -> tuple[Any, Dict[str, Any]]:
    signals = _compute_thermodynamic_signals(query)
    soul = CognitiveAttractorState(workflow_id=workflow_id)
    u_energy = biorouter_obj.calculate_hamiltonian_from_scores(
        quality=signals["quality_baseline"],
        semantic_consistency=signals["semantic_consistency"],
        complexity_penalty=signals["complexity_penalty"],
    )
    regulated = biorouter_obj.regulate_temperature(soul, u_energy, stagnation=0.0)
    return regulated, signals


def build_thermodynamic_snapshot(
    *,
    query: str,
    thermodynamic_cognition_enabled: bool,
    biorouter_obj: Any,
) -> Optional[Dict[str, Any]]:
    if not thermodynamic_cognition_enabled or biorouter_obj is None:
        return None
    try:
        regulated, _signals = _regulate_query_state(
            query=query,
            workflow_id="specialist_dispatch",
            biorouter_obj=biorouter_obj,
        )
        return {
            "temperature": round(float(regulated.temperature), 4),
            "energy": round(float(regulated.energy), 4),
            "phase": regulated.phase.value,
            "exploration_budget": 2 if regulated.temperature >= 0.72 else 1,
        }
    except Exception:
        return None


def build_thermodynamic_route_plan(
    *,
    query: str,
    session_id: str,
    requested_execution_path: str,
    thermodynamic_cognition_enabled: bool,
    biorouter_obj: Any,
    compiled_graph_available: bool,
) -> Dict[str, Any]:
    if not thermodynamic_cognition_enabled or biorouter_obj is None:
        return {
            "enabled": False,
            "requested_execution_path": requested_execution_path,
            "preferred_execution_path": requested_execution_path,
            "temperature": None,
            "energy": None,
            "phase": None,
            "exploration_budget": 1,
            "critique_escalation_threshold": 0.7,
            "note": "Thermodynamic cognition disabled for this run.",
        }

    regulated, signals = _regulate_query_state(
        query=query,
        workflow_id=session_id,
        biorouter_obj=biorouter_obj,
    )

    preferred_path = requested_execution_path
    if requested_execution_path == "auto":
        if compiled_graph_available and regulated.temperature >= 0.56:
            preferred_path = "langgraph"
        else:
            preferred_path = "agentic_loop"

    return {
        "enabled": True,
        "requested_execution_path": requested_execution_path,
        "preferred_execution_path": preferred_path,
        "temperature": round(float(regulated.temperature), 4),
        "energy": round(float(regulated.energy), 4),
        "phase": regulated.phase.value,
        "exploration_budget": 2 if regulated.temperature >= 0.72 else 1,
        "critique_escalation_threshold": 0.68,
        "note": "BioRouter selected the preferred execution path and exploration budget for the current query.",
        "signals": {
            "complexity_penalty": round(float(signals["complexity_penalty"]), 4),
            "semantic_consistency": round(float(signals["semantic_consistency"]), 4),
            "quality_baseline": round(float(signals["quality_baseline"]), 4),
            "contradiction_terms": signals["contradiction_terms"],
            "exploration_terms": signals["exploration_terms"],
        },
        "soul": regulated.to_dict(),
    }


async def emit_thermodynamic_routing_telemetry(
    *,
    session_id: str,
    run_id: str,
    mode: str,
    route_plan: Dict[str, Any],
    emit_runtime_status_telemetry_fn: Callable[..., Awaitable[None]],
) -> None:
    if not route_plan.get("enabled"):
        return
    await emit_runtime_status_telemetry_fn(
        session_id=session_id,
        run_id=run_id,
        phase="thermodynamic_routing",
        status=str(route_plan.get("preferred_execution_path") or "auto"),
        details=str(route_plan.get("note") or "BioRouter routing decision recorded."),
        mode=mode,
        metrics={
            "temperature": float(route_plan.get("temperature") or 0.0),
            "energy": float(route_plan.get("energy") or 0.0),
            "exploration_budget": float(route_plan.get("exploration_budget") or 1.0),
        },
    )