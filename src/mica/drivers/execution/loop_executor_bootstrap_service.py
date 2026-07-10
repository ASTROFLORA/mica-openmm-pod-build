"""Bootstrap wiring for AgenticDriver loop execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from .helpers import resolve_depth_preset
from .loop_dependency_gate_service import LoopDependencyGates, build_loop_dependency_gates
from .loop_execution_context import LoopExecutionContext
from .loop_literature_helper_service import LoopLiteratureHelpers, build_loop_literature_helpers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopExecutorBootstrap:
    context: LoopExecutionContext
    backend_native_tool_names: frozenset[str]
    get_backend_native_executor_fn: Callable[[], Awaitable[Callable[[str, str, Dict[str, Any]], Awaitable[str]]]]
    literature_helpers: LoopLiteratureHelpers
    dependency_gates: LoopDependencyGates
    public_tool_names: List[str]
    spawn_tool_names: List[str]
    missing_registry_entries: List[str]
    run_bibliotecario_revision_cycle_fn: Callable[..., Awaitable[Optional[Dict[str, Any]]]]


def _ensure_driver_loop_state(driver_obj: Any, *, driver_config: Any) -> None:
    if getattr(driver_obj, "_depth_preset", None) is None:
        driver_obj._depth_preset = resolve_depth_preset(getattr(driver_config, "depth_preset", None))
    if getattr(driver_obj, "_delegation_sessions", None) is None:
        driver_obj._delegation_sessions = {}
    if getattr(driver_obj, "_role_contexts", None) is None:
        driver_obj._role_contexts = {}
    if getattr(driver_obj, "_program_envelopes", None) is None:
        driver_obj._program_envelopes = {}
    if getattr(driver_obj, "_run_event_logs", None) is None:
        driver_obj._run_event_logs = {}
    if getattr(driver_obj, "_evidence_ledgers", None) is None:
        driver_obj._evidence_ledgers = {}


def _derive_backend_native_tool_names(
    *,
    public_tool_names: Sequence[str],
    get_tool_capability_fn: Callable[[str], Any],
    registry_items_fn: Optional[Callable[[], Any]] = None,
) -> frozenset[str]:
    """Derive backend-native routing from the canonical capability registry.

    The direct loop must route public backend-native tools through the backend
    executor instead of relying on a tiny manual allowlist that drifts behind
    the real runtime surface.
    """

    candidate_names: list[str] = [str(raw_name or "").strip() for raw_name in public_tool_names]
    if callable(registry_items_fn):
        try:
            registry_items = registry_items_fn() or {}
            candidate_names.extend(str(name or "").strip() for name in registry_items.keys())
        except Exception:
            pass

    selected: list[str] = []
    for raw_name in candidate_names:
        tool_name = str(raw_name or "").strip()
        if not tool_name:
            continue
        try:
            spec = get_tool_capability_fn(tool_name)
        except Exception:
            continue
        if str(getattr(spec, "surface", "") or "").strip() != "public":
            continue
        if str(getattr(spec, "capability_mode", "") or "").strip() != "backend-native":
            continue
        selected.append(tool_name)
    return frozenset(dict.fromkeys(selected))


def build_loop_executor_bootstrap(
    *,
    driver_obj: Any,
    driver_config: Any,
    user_id: str,
    session_id: Optional[str],
    provider_id: str,
    model_id: Optional[str],
    abort: Any,
    reinjection_packet: Optional[Dict[str, Any]],
    mcp_available: bool,
    get_tool_capability_fn: Callable[[str], Any],
    validate_tool_registry_coverage_fn: Callable[[Sequence[str]], Sequence[str]],
    registry_items_fn: Optional[Callable[[], Any]],
    prepare_tool_surface_fn: Callable[..., tuple[List[str], List[str], List[str]]],
    effective_mica_tools: Sequence[Dict[str, Any]],
    spawn_tools: Sequence[Dict[str, Any]],
    provider_dependency_state_service_fn: Callable[..., Dict[str, Any]],
    backend_dependency_state_service_fn: Callable[..., Dict[str, Any]],
    network_dependency_state_service_fn: Callable[..., Awaitable[Dict[str, Any]]],
    sandbox_dependency_state_service_fn: Callable[..., Dict[str, Any]],
    dependency_state_for_tool_service_fn: Callable[..., Awaitable[Dict[str, Any]]],
    pre_dispatch_gate_service_fn: Callable[..., Awaitable[Optional[str]]],
    unavailable_tool_response_fn: Callable[..., str],
    degraded_tool_response_fn: Callable[..., str],
    run_bibliotecario_revision_cycle_service_fn: Callable[..., Awaitable[Optional[Dict[str, Any]]]],
) -> LoopExecutorBootstrap:
    _ensure_driver_loop_state(driver_obj, driver_config=driver_config)

    context = LoopExecutionContext.from_driver(
        driver_obj,
        user_id=user_id,
        session_id=session_id,
        provider_id=provider_id,
        model_id=model_id,
        abort=abort,
        reinjection_packet=reinjection_packet,
    )
    driver_obj._evidence_ledger = getattr(
        driver_obj,
        "_evidence_ledger",
        driver_obj._get_or_create_evidence_ledger(context.active_session_id, context.parent_run_id or context.active_session_id),
    )
    driver_obj._evidence_ledgers.setdefault(context.active_session_id, driver_obj._evidence_ledger)

    async def _get_backend_native_executor() -> Callable[[str, str, Dict[str, Any]], Awaitable[str]]:
        return await context.get_backend_native_executor()

    literature_helpers = build_loop_literature_helpers(
        resolve_literature_retrieval_policy_fn=driver_obj._resolve_literature_retrieval_policy,
        atom_memory=getattr(driver_obj, "atom_memory", None),
        negative_memory_context=context.negative_memory_context,
        active_session_id=context.active_session_id,
        latest_pipeline_outputs=context.latest_pipeline_outputs,
        driver_literature_sources=context.driver_literature_sources,
        degraded_tool_response_fn=degraded_tool_response_fn,
    )

    dependency_gates = build_loop_dependency_gates(
        configured_provider_ids_fn=driver_obj._configured_provider_ids,
        get_tool_capability_fn=get_tool_capability_fn,
        provider_dependency_state_service_fn=provider_dependency_state_service_fn,
        backend_dependency_state_service_fn=backend_dependency_state_service_fn,
        network_dependency_state_service_fn=network_dependency_state_service_fn,
        sandbox_dependency_state_service_fn=sandbox_dependency_state_service_fn,
        dependency_state_for_tool_service_fn=dependency_state_for_tool_service_fn,
        pre_dispatch_gate_service_fn=pre_dispatch_gate_service_fn,
        mcp_enabled=bool(getattr(driver_config, "mcp_enabled", True)),
        mcp_available=mcp_available,
        specialist_drivers=getattr(driver_obj, "specialist_drivers", None),
        specialist_pool_available=getattr(driver_obj, "_specialist_pool", None) is not None,
        last_bibliotecario_state=context.last_bibliotecario_state,
        unavailable_tool_response_fn=unavailable_tool_response_fn,
        degraded_tool_response_fn=degraded_tool_response_fn,
    )

    public_tool_names, spawn_tool_names, missing_registry_entries = prepare_tool_surface_fn(
        effective_mica_tools=effective_mica_tools,
        spawn_tools=spawn_tools,
        validate_registry_coverage_fn=validate_tool_registry_coverage_fn,
    )
    backend_native_tool_names = _derive_backend_native_tool_names(
        public_tool_names=public_tool_names,
        get_tool_capability_fn=get_tool_capability_fn,
        registry_items_fn=registry_items_fn,
    )
    try:
        context.backend_native_tool_names = set(backend_native_tool_names)
    except Exception:
        pass
    if missing_registry_entries:
        logger.warning(
            "Tool capability registry is missing entries for: %s",
            ", ".join(missing_registry_entries),
        )

    async def _run_bibliotecario_revision_cycle(
        *,
        verdict: Dict[str, Any],
        reviewer_focus: str,
        reviewer_critique: str,
        live_pending: list,
    ) -> Optional[Dict[str, Any]]:
        return await run_bibliotecario_revision_cycle_service_fn(
            verdict=verdict,
            reviewer_focus=reviewer_focus,
            reviewer_critique=reviewer_critique,
            live_pending=live_pending,
            last_bibliotecario_state=context.last_bibliotecario_state,
            shorten_query_fn=lambda text: literature_helpers.shorten_query(text, max_words=8),
            search_literature_records_fn=literature_helpers.search_literature_records,
            driver_literature_sources=context.driver_literature_sources,
            build_source_record_from_paper_fn=driver_obj._build_source_record_from_paper,
            retrieval_planner=context.retrieval_planner,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            parent_run_id=context.parent_run_id,
            embody_role_fn=driver_obj._embody_role,
            provider_id=context.provider_id,
            model_id=context.model_id,
            abort=context.abort,
            bibliotecario_system_prompt=driver_obj._BIBLIOTECARIO_SYSTEM_PROMPT,
            bibliotecario_tools=driver_obj._BIBLIOTECARIO_TOOLS,
            depth_preset_name=driver_obj._depth_preset.name,
            normalize_bibliotecario_citations_fn=driver_obj._normalize_bibliotecario_citations,
            agent_memory=context.agent_memory,
            persist_agent_summary_fn=driver_obj._persist_agent_summary,
            summary_store=context.summary_store,
            active_session_id=context.active_session_id,
            record_claim_dicts_fn=driver_obj._record_claim_dicts_in_evidence_ledger,
        )

    return LoopExecutorBootstrap(
        context=context,
        backend_native_tool_names=backend_native_tool_names,
        get_backend_native_executor_fn=_get_backend_native_executor,
        literature_helpers=literature_helpers,
        dependency_gates=dependency_gates,
        public_tool_names=public_tool_names,
        spawn_tool_names=spawn_tool_names,
        missing_registry_entries=list(missing_registry_entries),
        run_bibliotecario_revision_cycle_fn=_run_bibliotecario_revision_cycle,
    )
