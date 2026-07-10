"""Scientific role dispatch helpers extracted from AgenticDriver."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .bibliotecario_consultation_service import (
    build_bibliotecario_role_spec,
    build_last_bibliotecario_state_setter,
    run_consult_bibliotecario_branch,
)
from .peer_review_service import (
    PeerReviewQualityAdapterService,
    build_reviewer_role_spec,
    run_request_peer_review_branch,
)


async def run_scientific_role_dispatch_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    pending: Any,
    query_shortener_fn: Callable[[str], str],
    search_literature_result_fn: Callable[..., Any],
    search_literature_records_fn: Callable[..., Any],
    retrieval_planner_obj: Any,
    driver_literature_sources: list[str],
    user_id: Optional[str],
    workspace_id: str,
    parent_run_id: Optional[str],
    provider_id: Optional[str],
    model_id: Optional[str],
    abort: Any,
    active_session_id: str,
    agent_memory_obj: Any,
    summary_store_obj: Any,
    last_bibliotecario_state: Dict[str, Any],
    run_bibliotecario_revision_cycle_fn: Callable[..., Any],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    if name == "consult_bibliotecario":
        return await run_consult_bibliotecario_branch(
            name=name,
            args=args,
            pending=pending,
            shorten_query_fn=query_shortener_fn,
            search_literature_result_fn=search_literature_result_fn,
            driver_literature_sources=driver_literature_sources,
            retrieval_planner_obj=retrieval_planner_obj,
            user_id=user_id,
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            provider_id=provider_id,
            model_id=model_id,
            abort=abort,
            role_spec=build_bibliotecario_role_spec(executor_obj._BIBLIOTECARIO_SYSTEM_PROMPT),
            embody_role_fn=executor_obj._embody_role,
            normalize_citations_fn=executor_obj._normalize_bibliotecario_citations,
            build_source_record_from_paper_fn=executor_obj._build_source_record_from_paper,
            format_bibliotecario_citation_entry_fn=executor_obj._format_bibliotecario_citation_entry,
            active_session_id=active_session_id,
            agent_memory_obj=agent_memory_obj,
            summary_store_obj=summary_store_obj,
            persist_agent_summary_fn=executor_obj._persist_agent_summary,
            record_claim_dicts_fn=executor_obj._record_claim_dicts_in_evidence_ledger,
            set_last_bibliotecario_state_fn=build_last_bibliotecario_state_setter(last_bibliotecario_state),
        )

    if name == "request_peer_review":
        return await run_request_peer_review_branch(
            name=name,
            args=args,
            pending=pending,
            search_literature_records_fn=search_literature_records_fn,
            retrieval_planner_obj=retrieval_planner_obj,
            driver_literature_sources=driver_literature_sources,
            user_id=user_id,
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            evidence_ledger_obj=getattr(executor_obj, "_evidence_ledger", None),
            role_spec=build_reviewer_role_spec(),
            embody_role_fn=executor_obj._embody_role,
            provider_id=provider_id,
            model_id=model_id,
            abort=abort,
            quality_adapter_service_obj=PeerReviewQualityAdapterService(executor_obj),
            last_bibliotecario_state=last_bibliotecario_state,
            serialize_legacy_model_fn=executor_obj._serialize_legacy_model,
            active_session_id=active_session_id,
            record_claim_dicts_fn=executor_obj._record_claim_dicts_in_evidence_ledger,
            agent_memory_obj=agent_memory_obj,
            summary_store_obj=summary_store_obj,
            persist_agent_summary_fn=executor_obj._persist_agent_summary,
            publish_communication_review_projection_fn=executor_obj._publish_communication_review_projection,
            run_bibliotecario_revision_cycle_fn=run_bibliotecario_revision_cycle_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    return degraded_tool_response_fn(
        name,
        "Scientific role dispatch received an unsupported tool.",
        args_payload=args,
        extra={"detail": f"unsupported scientific dispatch tool: {name}"},
    )