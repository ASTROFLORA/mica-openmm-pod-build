"""Pure / near-pure LangGraph node and router functions.

Phase 3 extraction from agentic_driver.py.
Phase 4a additions: node_initialize, node_thermostat, node_assign.
Phase 4b additions: node_execute, node_quality_gate.

Each function operates on a ``MICAState`` (typed dict) and returns an
updated copy — no ``self`` required.  Side-effect callbacks (e.g.
``emit_event_fn``) are injected explicitly.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# Phase 4b — reuse already-extracted helpers (no new coupling)
from ..utils import _truncate_text, _redact_text, _redact_obj
from ..evidence.contract import build_minimal_lab_report
from ..types import MICAState  # Sprint 0 S0.1 — canonical import

logger = logging.getLogger(__name__)


def _normalize_tombstone_class(tombstone: Dict[str, Any]) -> str:
    raw = str(tombstone.get("tombstone_class") or tombstone.get("class") or "operational").strip().lower()
    if raw in {"operational", "archaeological", "heretical"}:
        return raw
    return "operational"


def _tombstone_prunes_in_mode(tombstone: Dict[str, Any], mode: str) -> bool:
    normalized_mode = str(mode or "full").strip().lower()
    if normalized_mode != "full":
        return False
    if _normalize_tombstone_class(tombstone) != "operational":
        return False
    return str(tombstone.get("action") or "prune_context").strip().lower() == "prune_context"


def _fallback_message_matches_tombstones(
    message: Dict[str, Any],
    tombstones: List[Dict[str, Any]],
) -> bool:
    role = str(message.get("role") or "").strip().lower()
    if role not in {"assistant", "tool"}:
        return False

    content = message.get("content")
    if isinstance(content, str):
        haystack = content
    else:
        haystack = json.dumps(content, ensure_ascii=False, default=str)
    haystack_folded = haystack.casefold()

    raw_needles: List[str] = []
    for tombstone in tombstones:
        if not _tombstone_prunes_in_mode(tombstone, "full"):
            continue
        for key in (
            "target_id",
            "claim_id",
            "hypothesis_id",
            "origin_claim_id",
            "origin_hypothesis_id",
        ):
            value = str(tombstone.get(key) or "").strip()
            if value:
                raw_needles.append(value)
        for key in ("match_strings", "text_markers"):
            for value in list(tombstone.get(key) or []):
                text = str(value or "").strip()
                if text:
                    raw_needles.append(text)

    seen: set[str] = set()
    for needle in raw_needles:
        normalized = re.sub(r"\s+", " ", needle).strip()
        if len(normalized) < 6:
            continue
        folded = normalized.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        if folded in haystack_folded:
            return True
    return False


def _message_matches_tombstones(
    message: Dict[str, Any],
    tombstones: List[Dict[str, Any]],
) -> bool:
    try:
        from mica.agentic.core import message_matches_tombstones as _shared_message_matches_tombstones

        return _shared_message_matches_tombstones(message, tombstones)
    except ImportError:
        return _fallback_message_matches_tombstones(message, tombstones)


def _query_for_runtime(state: MICAState) -> str:
    return str(state.get("original_user_query") or state.get("user_query") or "")


def _should_prune_payload(payload: Any, tombstones: List[Dict[str, Any]]) -> bool:
    return _message_matches_tombstones(
        {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        tombstones,
    )


def _prune_state_for_tombstones(state: MICAState) -> MICAState:
    negative_memory_mode = str(state.get("negative_memory_mode") or "full").strip().lower()
    tombstones = [
        tombstone
        for tombstone in list(state.get("branch_tombstones") or [])
        if isinstance(tombstone, dict) and _tombstone_prunes_in_mode(tombstone, negative_memory_mode)
    ]
    if not tombstones:
        return state

    pruned_lab_reports = [
        report for report in list(state.get("lab_reports") or [])
        if not _should_prune_payload(report, tombstones)
    ]
    pruned_peer_feedback = [
        feedback for feedback in list(state.get("peer_feedback") or [])
        if not _should_prune_payload(feedback, tombstones)
    ]
    pruned_tool_results = [
        result for result in list(state.get("mcp_tool_results") or [])
        if not _should_prune_payload(result, tombstones)
    ]
    pruned_specialist_outputs = {
        key: value
        for key, value in dict(state.get("specialist_outputs") or {}).items()
        if not _should_prune_payload({"key": key, "value": value}, tombstones)
    }

    pruned_count = (
        len(list(state.get("lab_reports") or [])) - len(pruned_lab_reports)
        + len(list(state.get("peer_feedback") or [])) - len(pruned_peer_feedback)
        + len(list(state.get("mcp_tool_results") or [])) - len(pruned_tool_results)
        + len(dict(state.get("specialist_outputs") or {})) - len(pruned_specialist_outputs)
    )
    if pruned_count <= 0:
        return state

    return {
        **state,
        "lab_reports": pruned_lab_reports,
        "peer_feedback": pruned_peer_feedback,
        "mcp_tool_results": pruned_tool_results,
        "specialist_outputs": pruned_specialist_outputs,
        "logs": list(state.get("logs") or []) + [{
            "step": "langgraph_tombstone_prune",
            "pruned_records": pruned_count,
            "tombstone_count": len(tombstones),
        }],
    }


# ── Pure nodes ────────────────────────────────────────────────────────

def node_route(state: MICAState) -> MICAState:
    """STATELESS NODE: Route to specialists (no-op passthrough)."""
    logger.info("[ROUTE] Intent keys: %s", list(state["intent"].keys()))
    return {**state, "logs": state.get("logs", []) + [{"step": "route"}]}


def node_decompose(state: MICAState) -> MICAState:
    """STATELESS NODE: Decompose intent into subtasks."""
    logger.info("[DECOMPOSE] Creating subtasks...")

    subtasks: List[Dict[str, str]] = []
    intent = state.get("intent") or {}

    if intent.get("requires_literature"):
        subtasks.append({"subtask_id": "literature", "description": "Search literature", "worker_type": "literature"})
    if intent.get("requires_protein_analysis"):
        subtasks.append({"subtask_id": "protein_analysis", "description": "Analyze protein", "worker_type": "uniprot"})
    if intent.get("requires_md_simulation"):
        subtasks.append({"subtask_id": "md_simulation", "description": "MD simulation", "worker_type": "biodynamo"})
    if intent.get("requires_drug_discovery"):
        subtasks.append({"subtask_id": "drug_discovery", "description": "Drug discovery", "worker_type": "alchemist"})
    if intent.get("requires_graph_analysis"):
        subtasks.append({"subtask_id": "graph_analysis", "description": "Graph analysis", "worker_type": "smic"})

    if not subtasks:
        subtasks.append({
            "subtask_id": "orchestrator",
            "description": "General reasoning and orchestration",
            "worker_type": "router_native",
        })

    logger.info("[DECOMPOSE] Created %d subtasks", len(subtasks))
    return {
        **state,
        "subtasks": subtasks,
        "logs": state.get("logs", []) + [{"step": "decompose", "subtask_count": len(subtasks)}],
    }


# ── Near-pure nodes (with optional callbacks) ────────────────────────

def node_analyze(
    state: MICAState,
    *,
    emit_event_fn: Optional[Callable[..., None]] = None,
) -> MICAState:
    """STATELESS NODE: Analyse intent from *user_query*.

    Args:
        state: Current workflow state.
        emit_event_fn: Optional callback ``(event_type, node_id, workflow_id, data)``
            for telemetry events.
    """
    if emit_event_fn:
        emit_event_fn(event_type="NodeExecutionStarted", node_id="analyze",
                       workflow_id=state.get("workflow_id"), data={})

    query_text = _query_for_runtime(state)
    logger.info("[ANALYZE] Query: %s...", query_text[:100])

    normalized = (
        unicodedata.normalize("NFKD", query_text)
        .encode("ascii", "ignore")
        .decode("utf-8")
        .lower()
    )

    intent = {
        "requires_md_simulation": any(kw in normalized for kw in ["simulate", "dynamics", "md", "trajectory"]),
        "requires_protein_analysis": any(kw in normalized for kw in ["protein", "p53", "egfr", "uniprot"]),
        "requires_drug_discovery": any(kw in normalized for kw in ["drug", "ligand", "docking", "adme"]),
        "requires_graph_analysis": any(kw in normalized for kw in ["cavity", "graph", "topology"]),
        "requires_literature": any(kw in normalized for kw in ["papers", "research", "pubmed", "arxiv"]),
    }

    logger.info("[ANALYZE] Intent: %s", intent)
    out = {**state, "intent": intent, "logs": state.get("logs", []) + [{"step": "analyze", "intent": intent}]}

    if emit_event_fn:
        emit_event_fn(event_type="IntentAnalysis", node_id="analyze",
                       workflow_id=state.get("workflow_id"), data=intent)
        emit_event_fn(event_type="NodeExecutionCompleted", node_id="analyze",
                       workflow_id=state.get("workflow_id"), data={})
    return out


def node_synthesize(
    state: MICAState,
    *,
    derive_claims_fn: Callable[[str, list], Tuple[list, list]],
) -> MICAState:
    """STATELESS NODE: Synthesize final result from lab reports.

    Args:
        state: Current workflow state.
        derive_claims_fn: ``(summary, findings) -> (claims, sources)``
    """
    state = _prune_state_for_tombstones(state)
    findings = state.get("lab_reports") or []
    query_text = _query_for_runtime(state)
    logger.info("[SYNTHESIZE] Synthesizing %d reports...", len(findings))

    summary = (
        f"MICA synthesized {len(findings)} report(s) for: {query_text}"
        if findings
        else f"MICA processed the request: {query_text}"
    )
    appeal_regime_state = dict(state.get("appeal_regime_state") or {})
    soft_repulsion_warnings = [
        warning for warning in list(state.get("soft_repulsion_warnings") or [])
        if isinstance(warning, dict)
    ]
    negative_memory_guidance: Dict[str, Any] = {
        "appeal_regime_active": bool(appeal_regime_state.get("appeal_regime_active")),
        "soft_repulsion_warning_count": len(soft_repulsion_warnings),
        "appeal_candidates": list(appeal_regime_state.get("appeal_candidates") or []),
        "guidance": [],
    }
    if soft_repulsion_warnings:
        negative_memory_guidance["guidance"].append(
            f"{len(soft_repulsion_warnings)} soft-repulsion zone(s) remain open and require materially new evidence before revival."
        )
    if appeal_regime_state.get("appeal_regime_active"):
        negative_memory_guidance["guidance"].append(
            "Appeal regime is active; anomaly candidates remain under investigative review."
        )
    if negative_memory_guidance["guidance"]:
        summary = f"{summary} {' '.join(negative_memory_guidance['guidance'])}".strip()
    claims, sources = derive_claims_fn(summary=summary, findings=findings)

    final_result = {
        "query": query_text,
        "summary": summary,
        "findings": findings,
        "lab_reports": findings,
        "quality_score": state.get("quality_score", 0.0),
        "converged": state.get("converged", False),
        "iterations": state.get("iteration_count", 0),
        "claims": claims,
        "sources": sources,
        "artifacts": [],
        "appeal_regime_state": appeal_regime_state,
        "soft_repulsion_warnings": soft_repulsion_warnings,
        "negative_memory_guidance": negative_memory_guidance,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    logger.info("[SYNTHESIZE] Final result generated (converged=%s)", state.get("converged", False))
    return {**state, "final_result": final_result, "logs": state.get("logs", []) + [{"step": "synthesize"}]}


def node_proactive_monitor(
    state: MICAState,
    *,
    proactive_system: Any,
    get_spawn_count: Callable[[], int],
    set_spawn_count: Callable[[int], None],
) -> MICAState:
    """STATELESS NODE: Proactive gap detection.

    Args:
        state: Current workflow state.
        proactive_system: Object with ``.scan(state)`` method.
        get_spawn_count: Returns current spawn count.
        set_spawn_count: Sets new spawn count (for instance tracking).
    """
    logger.info("[PROACTIVE_MONITOR] Scanning for gaps...")

    proactive_triggers = proactive_system.scan(state)

    new_count = get_spawn_count() + 1
    set_spawn_count(new_count)

    logger.info("[PROACTIVE_MONITOR] Detected %d gaps (spawn #%d)", len(proactive_triggers), new_count)
    return {
        **state,
        "proactive_triggers": proactive_triggers,
        "_proactive_spawn_count": new_count,
        "logs": state.get("logs", []) + [{"step": "proactive_monitor", "gap_count": len(proactive_triggers)}],
    }


# ── Pure routers ──────────────────────────────────────────────────────

def router_quality_gate(state: MICAState) -> str:
    """CONTROL FLOW ROUTER: Quality gate decision.

    Returns:
        ``"continue"`` | ``"iterate"`` | ``"escalate"``
    """
    quality_score = state.get("quality_score", 0.0)
    iteration_count = state.get("iteration_count", 0)
    max_iterations = state.get("max_iterations", 3)
    quality_threshold = state.get("quality_threshold", 0.85)

    if quality_score >= quality_threshold:
        logger.info("[ROUTER] Quality gate PASSED (%.2f%% >= %.2f%%) -> continue",
                     quality_score * 100, quality_threshold * 100)
        return "continue"

    if iteration_count >= max_iterations:
        logger.warning("[ROUTER] Max iterations (%d) reached -> escalate", max_iterations)
        return "escalate"

    logger.info("[ROUTER] Quality insufficient (%.2f%%) -> iterate", quality_score * 100)
    return "iterate"


def router_proactive_monitor(
    state: MICAState,
    *,
    spawn_count: int = 0,
    max_spawns: int = 2,
) -> str:
    """CONTROL FLOW ROUTER: Proactive monitoring decision.

    Args:
        state: Current workflow state.
        spawn_count: Current spawn count (from instance tracker).
        max_spawns: Hard cap on re-spawns.

    Returns:
        ``"spawn_research"`` | ``"end"``
    """
    proactive_triggers = state.get("proactive_triggers", [])

    if proactive_triggers and spawn_count < max_spawns:
        logger.info("[ROUTER] Proactive triggers detected (%d, spawn %d/%d) -> spawn_research",
                     len(proactive_triggers), spawn_count, max_spawns)
        return "spawn_research"

    if proactive_triggers and spawn_count >= max_spawns:
        logger.warning("[ROUTER] Proactive triggers detected but spawn cap reached (%d/%d) -> end",
                       spawn_count, max_spawns)
    else:
        logger.info("[ROUTER] No proactive triggers -> end")
    return "end"


# ── Phase 4a: Additional node extractions ─────────────────────────────

def node_initialize(
    state: MICAState,
    *,
    enable_thermodynamic_cognition: bool = False,
    attractor_state_cls: Any = None,
    emit_event_fn: Optional[Callable[..., None]] = None,
) -> MICAState:
    """STATELESS NODE: Initialize workflow.

    Args:
        state: Current workflow state.
        enable_thermodynamic_cognition: Whether to create a default soul.
        attractor_state_cls: ``CognitiveAttractorState`` class (injected to
            avoid coupling to *mudo_envelope*).
        emit_event_fn: Optional telemetry callback.
    """
    if emit_event_fn:
        emit_event_fn(
            event_type="NodeExecutionStarted", node_id="initialize",
            workflow_id=state.get("workflow_id"), data={},
        )

    # Reset iteration_count on (re-)initialization
    if state.get("iteration_count", 0) > 0:
        logger.info("[INITIALIZE] Resetting iteration_count from %s to 0", state.get("iteration_count"))
        state = {**state, "iteration_count": 0}

    logger.info("[INITIALIZE] Workflow: %s", state.get("workflow_id"))

    updates: Dict[str, Any] = {}
    if enable_thermodynamic_cognition and "soul" not in state and attractor_state_cls is not None:
        default_soul = attractor_state_cls(
            workflow_id=state["workflow_id"],
            temperature=0.5,
            energy=0.8,
            coherence=0.9,
            entropy=0.1,
            iteration=0,
        )
        updates["soul"] = default_soul.to_dict()
        logger.info("[INITIALIZE] Soul created: T=%.2f, U=%.2f", default_soul.temperature, default_soul.energy)

    out = {
        **state,
        **updates,
        "logs": state.get("logs", []) + [
            {"step": "initialize", "timestamp": datetime.now(timezone.utc).isoformat()}
        ],
    }

    if emit_event_fn:
        emit_event_fn(
            event_type="NodeExecutionCompleted", node_id="initialize",
            workflow_id=state.get("workflow_id"), data={},
        )
    return out


def node_thermostat(
    state: MICAState,
    *,
    biorouter: Any,
    attractor_state_cls: Any,
) -> MICAState:
    """STATELESS NODE: Thermodynamic regulation (BioRouter).

    Calculates Hamiltonian (U) and regulates Temperature (T).
    High T → Exploration (Chaos), Low T → Exploitation (Order).

    Args:
        state: Current workflow state (must contain ``"soul"`` key).
        biorouter: ``BioRouter`` instance with ``.regulate_temperature`` method.
        attractor_state_cls: ``CognitiveAttractorState`` class for rehydration.
    """
    if biorouter is None or "soul" not in state:
        return state

    logger.info("[THERMOSTAT] Regulating thermodynamic state...")

    soul = attractor_state_cls.from_dict(state["soul"])
    current_quality = state.get("quality_score", 0.0)
    soul.iteration = state.get("iteration_count", 0)

    # U = 1 - Quality (lower energy = better state)
    current_energy = 1.0 - current_quality
    biorouter.regulate_temperature(soul, current_energy)

    logger.info(
        "[THERMOSTAT] T=%.3f, U=%.3f, Coherence=%.3f",
        soul.temperature, soul.energy, soul.coherence,
    )

    return {
        **state,
        "soul": soul.to_dict(),
        "logs": state.get("logs", []) + [{
            "step": "thermostat",
            "temperature": soul.temperature,
            "energy": soul.energy,
            "coherence": soul.coherence,
        }],
    }


def node_assign(
    state: MICAState,
    *,
    registry: Optional[Mapping[str, Any]] = None,
    specialist_drivers: Optional[Mapping[str, Any]] = None,
) -> MICAState:
    """STATELESS NODE: Assign subtasks to workers.

    Args:
        state: Current workflow state (must contain ``"subtasks"`` list).
        registry: Worker registry (maps worker_type → impl). May be ``None``.
        specialist_drivers: Specialist driver map (e.g. biodynamo, alchemist).
    """
    subtasks = state.get("subtasks") or []
    logger.info("[ASSIGN] Assigning %d subtasks...", len(subtasks))

    specialist_set = set(specialist_drivers or {})
    assigned: Dict[str, str] = {}

    for subtask in subtasks:
        worker_type = subtask["worker_type"]
        if registry is not None and registry.get(worker_type) is not None:
            assigned[subtask["subtask_id"]] = worker_type
            continue
        if worker_type in {"biodynamo", "alchemist", "smic"} and worker_type in specialist_set:
            assigned[subtask["subtask_id"]] = worker_type
        else:
            assigned[subtask["subtask_id"]] = f"mcp_{worker_type}"

    logger.info("[ASSIGN] Assignments: %s", assigned)
    return {
        **state,
        "assigned_workers": assigned,
        "logs": state.get("logs", []) + [{"step": "assign", "assignments": assigned}],
    }


# ── Phase 4b: Heavy node extractions ──────────────────────────────────

async def node_execute(
    state: MICAState,
    *,
    emit_event_fn: Optional[Callable[..., None]] = None,
    specialist_drivers: Optional[Mapping[str, Any]] = None,
    execute_worker_fn: Optional[Callable] = None,
    execute_with_mcp_fn: Optional[Callable] = None,
    session_cls: Any = None,
) -> MICAState:
    """STATELESS NODE: Execute assigned tasks.

    Three worker branches:
      1. **Specialist** — ``specialist_drivers[worker_name].execute(...)``
      2. **MCP** — ``execute_with_mcp_fn(worker_name, session)``
      3. **Transport** — ``execute_worker_fn(worker, prompt, session_id)``

    Args:
        state: Current workflow state (must contain ``assigned_workers``, ``subtasks``).
        emit_event_fn: Telemetry callback ``(event_type=, node_id=, workflow_id=, data=)``.
        specialist_drivers: Map of specialist driver name → driver instance.
        execute_worker_fn: Async fn for transport-backed workers.
        execute_with_mcp_fn: Async fn for MCP tool execution.
        session_cls: ``AgenticSession`` class (or compat) for MCP dummy sessions.
    """
    _emit = emit_event_fn or (lambda **kw: None)
    _specialists = dict(specialist_drivers or {})
    _wid = state.get("workflow_id")
    thermodynamic_context = state.get("soul")

    _emit(
        event_type="NodeExecutionStarted", node_id="execute",
        workflow_id=_wid,
        data={"task_count": len(state.get("assigned_workers") or {})},
    )
    logger.info("[EXECUTE] Executing %d tasks...", len(state.get("assigned_workers") or {}))

    lab_reports: List[Dict[str, Any]] = []
    lab_report_models: Dict[str, Any] = {}

    for subtask_id, worker_name in (state.get("assigned_workers") or {}).items():
        subtask_desc = next(
            (s["description"] for s in state.get("subtasks", []) if s["subtask_id"] == subtask_id),
            _query_for_runtime(state),
        )
        try:
            _emit(
                event_type="ToolCallStarted", node_id="execute",
                workflow_id=_wid,
                data={"subtask_id": subtask_id, "worker": worker_name},
            )

            if worker_name in _specialists:
                # ── Branch 1: Specialist driver ──
                driver = _specialists[worker_name]
                exec_ctx: Dict[str, Any] = {
                    "subtask_id": subtask_id,
                    "full_query": _query_for_runtime(state),
                }
                if worker_name == "biodynamo" and state.get("resolved_pdb_path"):
                    exec_ctx["protein_pdb"] = state["resolved_pdb_path"]
                    exec_ctx["use_remote_vast"] = True

                result = await driver.execute(
                    query=subtask_desc,
                    context=exec_ctx,
                    thermodynamic_context=thermodynamic_context,
                )

                if worker_name == "alchemist":
                    _rpdb = result.get("resolved_pdb_path") or ""
                    if not _rpdb:
                        for hit in result.get("prioritized_hits") or []:
                            _rpdb = hit.get("protein_pdb") or hit.get("target_pdb") or ""
                            if _rpdb:
                                break
                    if _rpdb:
                        state = {**state, "resolved_pdb_path": str(_rpdb)}

                findings_text = str(result.get("answer", "No answer"))
                lab_report_models[subtask_id] = build_minimal_lab_report(
                    worker_name=worker_name, query=subtask_desc,
                    findings_text=findings_text,
                    quantitative_metrics={}, raw_attachments=[],
                )
                lab_reports.append({
                    "subtask_id": subtask_id,
                    "worker": worker_name,
                    "findings": _truncate_text(_redact_text(findings_text), max_len=4000),
                    "confidence": result.get("confidence", 0.8),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": _redact_obj(result),
                })

            elif worker_name.startswith("mcp_"):
                # ── Branch 2: MCP tools ──
                if execute_with_mcp_fn is None:
                    raise RuntimeError(f"execute_with_mcp_fn required for {worker_name}")
                if session_cls is not None:
                    dummy_session = session_cls(
                        user_query=_query_for_runtime(state),
                        soul=thermodynamic_context,
                    )
                else:
                    dummy_session = None
                mcp_result = await execute_with_mcp_fn(worker_name, dummy_session)

                findings_text = str(mcp_result.get("message") or str(mcp_result))
                lab_report_models[subtask_id] = build_minimal_lab_report(
                    worker_name=worker_name, query=subtask_desc,
                    findings_text=findings_text,
                    quantitative_metrics={}, raw_attachments=[],
                )
                safe_mcp_meta = {k: v for k, v in (mcp_result or {}).items() if k not in {"args"}}
                lab_reports.append({
                    "subtask_id": subtask_id,
                    "worker": worker_name,
                    "findings": _truncate_text(_redact_text(findings_text), max_len=4000),
                    "confidence": 0.8 if mcp_result.get("status") == "success" else 0.0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": _redact_obj(safe_mcp_meta),
                })

            else:
                # ── Branch 3: Transport-backed worker ──
                if execute_worker_fn is None:
                    raise RuntimeError(f"execute_worker_fn required for {worker_name}")
                transport_result = await execute_worker_fn(
                    worker=worker_name,
                    prompt=subtask_desc,
                    session_id=str(
                        state.get("session_id") or state.get("workflow_id") or "default"
                    ),
                )

                data_obj = transport_result.get("data") if isinstance(transport_result, dict) else {}
                if not isinstance(data_obj, dict):
                    data_obj = {}

                findings_text = str(
                    transport_result.get("response")
                    or data_obj.get("response")
                    or data_obj.get("result")
                    or transport_result
                )
                lab_report_models[subtask_id] = build_minimal_lab_report(
                    worker_name=worker_name, query=subtask_desc,
                    findings_text=findings_text,
                    quantitative_metrics={
                        "success": 1.0 if str(transport_result.get("status", "")).upper() == "SUCCESS" else 0.0,
                    },
                    raw_attachments=[],
                )
                safe_meta = {
                    "backend_type": transport_result.get("backend_type"),
                    "status": transport_result.get("status"),
                    "tool_calls": transport_result.get("tool_calls"),
                    "tool_runs": transport_result.get("tool_runs"),
                    "errors": transport_result.get("errors"),
                }
                lab_reports.append({
                    "subtask_id": subtask_id,
                    "worker": worker_name,
                    "findings": _truncate_text(_redact_text(findings_text), max_len=4000),
                    "confidence": 0.8 if str(transport_result.get("status", "")).upper() == "SUCCESS" else 0.0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": _redact_obj(safe_meta),
                })

            _emit(
                event_type="ToolCallCompleted", node_id="execute",
                workflow_id=_wid,
                data={"subtask_id": subtask_id, "worker": worker_name, "ok": True},
            )

        except Exception as e:
            logger.error("Task %s failed: %s", subtask_id, e)
            _emit(
                event_type="ToolCallCompleted", node_id="execute",
                workflow_id=_wid,
                data={"subtask_id": subtask_id, "worker": worker_name, "ok": False, "error": str(e)},
            )
            lab_reports.append({
                "subtask_id": subtask_id,
                "worker": worker_name,
                "findings": f"Error: {e}",
                "confidence": 0.0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    logger.info("[EXECUTE] Completed %d tasks", len(lab_reports))
    out = {
        **state,
        "lab_reports": lab_reports,
        "lab_report_models": lab_report_models,
        "logs": state.get("logs", []) + [{"step": "execute", "report_count": len(lab_reports)}],
    }
    _emit(
        event_type="NodeExecutionCompleted", node_id="execute",
        workflow_id=_wid,
        data={"report_count": len(lab_reports)},
    )
    return out


async def node_quality_gate(
    state: MICAState,
    *,
    emit_event_fn: Optional[Callable[..., None]] = None,
    quality_evaluator: Any = None,
    config: Any = None,
    extract_identifiers_fn: Optional[Callable[[str], Dict[str, List[str]]]] = None,
    merge_identifiers_fn: Optional[Callable] = None,
    ensure_structures_fn: Optional[Callable] = None,
    best_protein_hint_fn: Optional[Callable] = None,
) -> MICAState:
    """STATELESS NODE: Evaluate quality of lab reports.

    Runs canonical Nature-standards quality scoring.  Optionally blends
    MQA (Multi-level Quality Assessment) when ``config.enable_mqa`` is set.

    Args:
        state: Current workflow state (must contain ``lab_reports``).
        emit_event_fn: Telemetry callback.
        quality_evaluator: Object with ``evaluate_lab_report`` / ``evaluate_quality``
            and ``aggregate_overall_score`` methods.
        config: Driver config (for MQA flags).
        extract_identifiers_fn: ``(text) -> {type: [ids]}``.
        merge_identifiers_fn: ``(a, b) -> merged``.
        ensure_structures_fn: Async ``(ids, query, use_pdb, use_alphafold) -> structures``.
        best_protein_hint_fn: ``(identifiers) -> protein_id | None``.
    """
    _emit = emit_event_fn or (lambda **kw: None)
    _wid = state.get("workflow_id")

    _emit(
        event_type="NodeExecutionStarted", node_id="quality_gate",
        workflow_id=_wid,
        data={"report_count": len(state.get("lab_reports") or [])},
    )
    logger.info("[QUALITY_GATE] Evaluating %d reports...", len(state.get("lab_reports") or []))

    # ── Canonical quality scoring ──
    quality_scores: Dict[str, Any] = {}
    report_models = state.get("lab_report_models") or {}
    if isinstance(report_models, dict) and report_models and quality_evaluator is not None:
        evaluate_fn = getattr(quality_evaluator, "evaluate_lab_report", None)
        for subtask_id, report in report_models.items():
            try:
                if callable(evaluate_fn):
                    quality_scores[subtask_id] = evaluate_fn(report)
                else:
                    quality_scores[subtask_id] = quality_evaluator.evaluate_quality(report)
            except Exception as exc:
                logger.warning("[QUALITY_GATE] eval failed for %s: %s", subtask_id, exc)

    avg_quality = 0.0
    if quality_scores and quality_evaluator is not None:
        try:
            avg_quality = float(
                quality_evaluator.aggregate_overall_score(list(quality_scores.values()))
            )
        except Exception:
            avg_quality = 0.0
    else:
        avg_quality = 1.0  # pass-through when no evaluator

    # ── Optional MQA hook ──
    mqa_payload = None
    _cfg = config
    if _cfg is not None and getattr(_cfg, "enable_mqa", False) and getattr(_cfg, "mqa_fetch_structures", False):
        try:
            identifiers = (extract_identifiers_fn or (lambda t: {}))(_query_for_runtime(state))
            for r in state.get("lab_reports") or []:
                if merge_identifiers_fn and extract_identifiers_fn:
                    identifiers = merge_identifiers_fn(
                        identifiers, extract_identifiers_fn(str(r)),
                    )

            structures = None
            if ensure_structures_fn is not None:
                structures = await ensure_structures_fn(
                    identifiers,
                    query=_query_for_runtime(state),
                    use_pdb=getattr(_cfg, "mqa_use_pdb", True),
                    use_alphafold=getattr(_cfg, "mqa_use_alphafold", True),
                )

            if structures:
                from mqa import MultiLevelMQA
                from ...scientific_workflow.mqa_bridge import (
                    energy_from_signal,
                    signal_from_mqa_result,
                )

                protein_id = (
                    (best_protein_hint_fn(identifiers) if best_protein_hint_fn else None)
                    or state.get("workflow_id")
                )
                mqa = MultiLevelMQA()
                mqa_result = await mqa.evaluate_ensemble_quality(
                    ensemble_structures=structures,
                    protein_id=protein_id,
                    experimental_data=None,
                    environmental_context=None,
                )
                signal = signal_from_mqa_result(mqa_result)
                energy = float(energy_from_signal(signal))
                mqa_quality = max(0.0, min(1.0, 1.0 - energy))

                mqa_weight = float(getattr(_cfg, "mqa_weight", 0.4) or 0.4)
                if quality_scores and quality_evaluator is not None:
                    avg_quality = float(
                        quality_evaluator.aggregate_overall_score(
                            list(quality_scores.values()),
                            mqa_quality=mqa_quality,
                            mqa_weight=mqa_weight,
                        )
                    )
                else:
                    avg_quality = max(avg_quality, mqa_quality)

                mqa_payload = {
                    "protein_id": protein_id,
                    "structures": structures,
                    "energy": energy,
                    "signal": {"quality": signal.quality, "consistency": signal.consistency},
                }
        except Exception as exc:
            logger.warning("[QUALITY_GATE] MQA hook failed (non-fatal): %s", exc)

    converged = avg_quality >= state.get("quality_threshold", 0.85)
    iteration_count = state.get("iteration_count", 0) + 1

    logger.info(
        "[QUALITY_GATE] Quality: %.2f%%, Threshold: %.2f%%, Iteration: %d",
        avg_quality * 100,
        state.get("quality_threshold", 0.85) * 100,
        iteration_count,
    )

    _emit(
        event_type="QualityAssessment", node_id="quality_gate",
        workflow_id=_wid,
        data={
            "score": float(avg_quality),
            "threshold": float(state.get("quality_threshold", 0.0) or 0.0),
            "converged": bool(converged),
            "iteration": int(iteration_count),
        },
    )

    out = {
        **state,
        "quality_score": avg_quality,
        "quality_scores": quality_scores,
        "converged": converged,
        "iteration_count": iteration_count,
        "quality_metrics": {
            **(state.get("quality_metrics") or {}),
            **({"mqa": mqa_payload} if mqa_payload else {}),
        },
        "logs": state.get("logs", []) + [
            {"step": "quality_gate", "quality": avg_quality, "converged": converged}
        ],
    }

    _emit(
        event_type="NodeExecutionCompleted", node_id="quality_gate",
        workflow_id=_wid,
        data={"converged": bool(converged)},
    )
    return out
