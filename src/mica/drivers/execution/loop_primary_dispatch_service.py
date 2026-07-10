"""Primary dispatch helpers extracted from AgenticDriver loop executor."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from .atom_facts_service import run_atom_facts_branch
from .citation_verification_service import run_verify_citations_branch
from .dashboard_placeholder_service import run_dashboard_placeholder_branch
from .deep_research_service import run_deep_research_branch
from .driver_checkpoint_service import (
    run_driver_delegated_checkpoint_branch,
    run_driver_staging_deploy_checkpoint_branch,
)
from .driver_experiment_service import (
    get_experiment_quota_status_branch,
    replay_experiment_branch,
    run_driver_experiment_branch,
)
from .fallback_routing_service import run_backend_only_degraded_branch
from .hypothesis_service import (
    run_generate_hypotheses_branch,
    run_list_dlm_presets_branch,
)
from .literature_search_service import run_literature_search_branch
from .lmp_context_service import run_lmp_context_branch
from .peer_review_service import run_request_peer_review_branch
from .protein_lookup_service import run_protein_lookup_branch
from .report_orchestration_service import run_report_orchestration_branch
from .research_briefing_service import run_research_briefing_branch
from .sandbox_session_service import (
    run_execute_in_sandbox_branch,
    run_sandbox_session_status_branch,
    run_terminate_sandbox_session_branch,
)
from .scientific_role_dispatch_service import run_scientific_role_dispatch_branch
from .specialist_delegation_service import (
    build_specialist_response_adapter,
    run_consult_specialist_branch,
)
from .worker_delegation_service import run_execute_worker_branch


def _json_ready_serverless_result(payload: Any) -> Any:
    if payload is None:
        return None
    if hasattr(payload, "model_dump"):
        try:
            return payload.model_dump(mode="json")
        except TypeError:
            return payload.model_dump()
    if is_dataclass(payload):
        return asdict(payload)
    return payload


async def run_loop_primary_branch(
    *,
    name: str,
    args: Dict[str, Any],
    driver_obj: Any,
    executor_fn: Callable[[str, str, Dict[str, Any]], Awaitable[str]],
    pending: Any,
    session_id: Optional[str],
    user_id: str,
    workspace_id: str,
    parent_run_id: Optional[str],
    provider_id: str,
    model_id: Optional[str],
    abort: Any,
    active_session_id: str,
    agent_memory_obj: Any,
    summary_store_obj: Any,
    retrieval_planner_obj: Any,
    driver_literature_sources: list[str],
    last_bibliotecario_state: Dict[str, Any],
    run_bibliotecario_revision_cycle_fn: Callable[..., Awaitable[Optional[Dict[str, Any]]]],
    shorten_query_fn: Callable[..., str],
    search_literature_result_fn: Callable[..., Awaitable[Any]],
    search_literature_records_fn: Callable[..., Awaitable[Any]],
    coerce_seed_entities_fn: Callable[..., Any],
    degraded_tool_response_fn: Callable[..., str],
    transport_payload_or_degraded_fn: Callable[..., str],
    fallback_transport_execution_fn: Callable[..., Awaitable[Any]],
    build_runtime_consumption_context_fn: Callable[..., Dict[str, Any]],
    run_driver_owned_delegated_checkpoint_fn: Callable[..., Awaitable[Dict[str, Any]]],
    run_driver_owned_staging_deploy_checkpoint_fn: Callable[..., Awaitable[Dict[str, Any]]],
    is_backend_only_typed_tool_fn: Callable[..., bool],
    backend_only_typed_tools: Any,
    specialist_pool_obj: Any,
) -> Optional[str]:
    """Handle the main extracted branch ladder from ``_build_loop_executor``.

    Returns ``None`` when the tool name is not handled by this primary router so
    the caller can continue into the residual tail dispatcher.
    """

    if name in (
        "search_literature",
        "run_dlm_scan",
        "run_bibliotecario_scan",
        "analyse_knowledge_decay",
        "analyse_citation_impact",
        "track_entity_evolution",
        "query_co_occurrence",
    ):
        return await run_literature_search_branch(
            name=name,
            args=args,
            shorten_query_fn=shorten_query_fn,
            search_literature_result_fn=search_literature_result_fn,
            driver_literature_sources=driver_literature_sources,
            pending=pending,
        )

    if name in ("search_protein", "search_protein_metadata", "advanced_protein_search"):
        return await run_protein_lookup_branch(
            args=args,
            shorten_query_fn=lambda query: shorten_query_fn(query, max_words=5),
            uniprot_search_fn=driver_obj._run_uniprot_search,
        )

    if name == "list_dlm_presets":
        return await run_list_dlm_presets_branch(
            name=name,
            args=args,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "list_serverless_models":
        models = driver_obj.list_serverless_models()
        return json.dumps(
            {
                "status": "SUCCESS",
                "worker": name,
                "backend_type": "driver_native_gateway",
                "response": f"Listed {len(models)} serverless models.",
                "data": {
                    "models": models,
                    "count": len(models),
                },
            },
            ensure_ascii=False,
            default=str,
        )

    if name == "federated_retrieve":
        result = await driver_obj._invoke_feed_tool(
            "federated_retrieve",
            dict(args or {}),
        )
        return json.dumps(
            {
                "status": "SUCCESS",
                "worker": name,
                "backend_type": "offline_native",
                "response": "Executed federated retrieval across live feed and memory surfaces.",
                "data": result,
            },
            ensure_ascii=False,
            default=str,
        )

    if name == "run_serverless_model":
        model_id = str(args.get("model_id") or "").strip()
        inputs = args.get("inputs")
        if not model_id or not isinstance(inputs, dict):
            return degraded_tool_response_fn(
                name,
                "run_serverless_model requires model_id and inputs object.",
                args_payload=args,
            )
        result = await driver_obj.invoke_serverless_model(
            model_id=model_id,
            inputs=dict(inputs),
            metadata=dict(args.get("metadata") or {}),
            user_id=user_id,
            session_id=session_id,
            run_id=parent_run_id or active_session_id,
            requested_by="agentic_driver_loop",
            provider_override=str(args.get("provider_override") or "").strip() or None,
        )
        normalized = _json_ready_serverless_result(result)
        return json.dumps(
            {
                "status": "SUCCESS",
                "worker": name,
                "backend_type": "driver_native_gateway",
                "response": f"Invoked serverless model {model_id}.",
                "data": normalized,
            },
            ensure_ascii=False,
            default=str,
        )

    if is_backend_only_typed_tool_fn(name, backend_only_typed_tools):
        return run_backend_only_degraded_branch(
            name=name,
            args=args,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "generate_hypotheses":
        return await run_generate_hypotheses_branch(
            name=name,
            args=args,
            coerce_seed_entities_fn=coerce_seed_entities_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name in ("compile_research_briefing", "scan_drug_repurposing"):
        branch_args = dict(args or {})
        if name == "compile_research_briefing":
            branch_args["query"] = shorten_query_fn(
                str(branch_args.get("query") or branch_args.get("entity") or branch_args.get("protein") or ""),
                max_words=8,
            )
        else:
            branch_args["protein"] = shorten_query_fn(
                str(branch_args.get("protein") or branch_args.get("query") or ""),
                max_words=6,
            )

        return await run_research_briefing_branch(
            name=name,
            args=branch_args,
            shorten_query_fn=lambda query: str(query or "").strip(),
            degraded_tool_response_fn=degraded_tool_response_fn,
            search_literature_result_fn=search_literature_result_fn,
            driver_literature_sources=driver_literature_sources,
        )

    if name == "run_deep_research":
        return await run_deep_research_branch(
            name=name,
            args=args,
            shorten_query_fn=shorten_query_fn,
            search_literature_result_fn=search_literature_result_fn,
            driver_literature_sources=driver_literature_sources,
            pending=pending,
        )

    if name in ("generate_report", "run_cascade_pipeline"):
        return await run_report_orchestration_branch(
            name=name,
            args=args,
            user_id=user_id,
        )

    if name == "query_atom_facts":
        def _workspace_id_getter() -> Optional[str]:
            current_workspace_id = None
            if hasattr(getattr(driver_obj, "_workspace_id_var", None), "get"):
                try:
                    current_workspace_id = driver_obj._workspace_id_var.get()
                except Exception:
                    current_workspace_id = None
            return current_workspace_id

        return await run_atom_facts_branch(
            args=args,
            session_id=session_id,
            user_id=user_id,
            workspace_id_getter_fn=_workspace_id_getter,
            build_runtime_consumption_context_fn=build_runtime_consumption_context_fn,
        )

    if name in (
        "map_conformational_landscape",
        "scan_pharmacovigilance",
        "build_ortholog_dashboard",
    ):
        return await run_dashboard_placeholder_branch(
            name=name,
            args=args,
            shorten_query_fn=lambda query: shorten_query_fn(query, max_words=5),
            search_literature_records_fn=search_literature_records_fn,
            driver_literature_sources=driver_literature_sources,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name in (
        "generate_lmp",
        "load_knowledge_graph",
        "get_domain_coloring",
        "list_lmp_presets",
    ):
        return await run_lmp_context_branch(
            name=name,
            args=args,
            fallback_transport_execution_fn=fallback_transport_execution_fn,
            transport_payload_or_degraded_fn=transport_payload_or_degraded_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "execute_worker":
        return await run_execute_worker_branch(
            name=name,
            args=args,
            fallback_transport_execution_fn=fallback_transport_execution_fn,
            transport_payload_or_degraded_fn=transport_payload_or_degraded_fn,
        )

    if name == "verify_citations":
        return await run_verify_citations_branch(
            name=name,
            args=args,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "run_driver_delegated_checkpoint":
        return await run_driver_delegated_checkpoint_branch(
            name=name,
            args=args,
            run_driver_owned_delegated_checkpoint_fn=run_driver_owned_delegated_checkpoint_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "run_driver_staging_deploy_checkpoint":
        return await run_driver_staging_deploy_checkpoint_branch(
            name=name,
            args=args,
            run_driver_owned_staging_deploy_checkpoint_fn=run_driver_owned_staging_deploy_checkpoint_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "execute_in_sandbox":
        return await run_execute_in_sandbox_branch(
            name=name,
            args=args,
            executor_obj=executor_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "sandbox_session_status":
        return await run_sandbox_session_status_branch(
            executor_obj=executor_fn,
            specialist_pool=specialist_pool_obj,
        )

    if name == "terminate_sandbox_session":
        return await run_terminate_sandbox_session_branch(
            executor_obj=executor_fn,
            args=args,
        )

    if name == "run_driver_experiment":
        return await run_driver_experiment_branch(
            name=name,
            args=args,
            executor_obj=driver_obj,
            invoke_feed_tool_fn=driver_obj._invoke_feed_tool,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "replay_experiment":
        return await replay_experiment_branch(
            name=name,
            args=args,
            executor_obj=driver_obj,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "get_experiment_quota_status":
        return await get_experiment_quota_status_branch(
            name=name,
            args=args,
            executor_obj=driver_obj,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "consult_specialist":
        return await run_consult_specialist_branch(
            name=name,
            args=args,
            executor_obj=driver_obj,
            pending=pending,
            fallback_transport_execution_fn=fallback_transport_execution_fn,
            degraded_tool_response_fn=build_specialist_response_adapter(
                transport_payload_or_degraded_fn=transport_payload_or_degraded_fn,
                degraded_tool_response_fn=degraded_tool_response_fn,
            ),
        )

    if name in {"consult_bibliotecario", "request_peer_review"}:
        return await run_scientific_role_dispatch_branch(
            name=name,
            args=args,
            executor_obj=driver_obj,
            pending=pending,
            query_shortener_fn=shorten_query_fn,
            search_literature_result_fn=search_literature_result_fn,
            search_literature_records_fn=search_literature_records_fn,
            retrieval_planner_obj=retrieval_planner_obj,
            driver_literature_sources=driver_literature_sources,
            user_id=user_id,
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            provider_id=provider_id,
            model_id=model_id,
            abort=abort,
            active_session_id=active_session_id,
            agent_memory_obj=agent_memory_obj,
            summary_store_obj=summary_store_obj,
            last_bibliotecario_state=last_bibliotecario_state,
            run_bibliotecario_revision_cycle_fn=run_bibliotecario_revision_cycle_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    return None
