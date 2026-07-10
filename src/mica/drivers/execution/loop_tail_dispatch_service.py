"""Tail dispatch helpers extracted from AgenticDriver loop executor."""

from typing import Any, Callable, Dict, Optional

from .expert_consultation_service import run_consult_expert_branch
from .fallback_routing_service import run_transport_fallback_branch
from .vertical_report_service import run_vertical_report


async def run_loop_tail_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    pending: Any,
    invoke_feed_tool_fn: Callable,
    search_literature_records_fn: Callable,
    retrieval_planner_obj: Any,
    driver_literature_sources: list,
    user_id: Optional[str],
    workspace_id: str,
    parent_run_id: Optional[str],
    agent_memory_obj: Any,
    summary_store_obj: Any,
    persist_summary_fn: Callable,
    filter_tools_for_lane_fn: Callable,
    degraded_tool_response_fn: Callable,
    provider_id: str,
    model_id: Optional[str],
    last_bibliotecario_state: Optional[Dict[str, Any]],
    fallback_transport_execution_fn: Callable,
) -> str:
    """Handle the residual tail branches of the loop executor.

    This keeps the remaining expert/vertical/fallback wiring out of the mega-method
    while preserving the exact branch behavior.
    """
    if name == "consult_expert":
        expert_args = dict(args or {})
        expert_args.setdefault("provider_id", provider_id)
        expert_args.setdefault("model_id", model_id)

        return await run_consult_expert_branch(
            name=name,
            args=expert_args,
            executor_obj=executor_obj,
            pending=pending,
            invoke_feed_tool_fn=invoke_feed_tool_fn,
            search_literature_records_fn=search_literature_records_fn,
            retrieval_planner_fn=retrieval_planner_obj,
            driver_literature_sources=driver_literature_sources,
            user_id=user_id,
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            agent_memory_fn=agent_memory_obj,
            summary_store_fn=summary_store_obj,
            persist_summary_fn=persist_summary_fn,
            filter_tools_for_lane_fn=filter_tools_for_lane_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    if name == "generate_vertical_report":
        return run_vertical_report(
            args=args,
            state=last_bibliotecario_state,
        )

    return await run_transport_fallback_branch(
        name=name,
        args=args,
        fallback_transport_execution_fn=fallback_transport_execution_fn,
    )