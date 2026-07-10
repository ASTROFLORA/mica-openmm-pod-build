"""Expert consultation helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Callable, Dict, Optional


async def run_consult_expert_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    pending: Any,
    invoke_feed_tool_fn: Callable,
    search_literature_records_fn: Callable,
    retrieval_planner_fn: Any,
    driver_literature_sources: list,
    user_id: Optional[str],
    workspace_id: str,
    parent_run_id: Optional[str],
    agent_memory_fn: Any,
    summary_store_fn: Any,
    persist_summary_fn: Callable,
    filter_tools_for_lane_fn: Callable,
    degraded_tool_response_fn: Callable,
) -> str:
    """Consult domain expert with literature verification and gap identification.
    
    Routes query through expert role with access to citation, gap identification,
    and literature search tools for independent verification.
    """
    _live_pending = pending if pending is not None else []
    expert_name = args.get("expert", "")
    question = args.get("question", "")
    context = args.get("context", "")
    
    # Lookup expert configuration
    from mica.drivers.role_context import RoleSpec, EXPERT_INVARIANTS
    _EXPERT_POOL = getattr(executor_obj, '_EXPERT_POOL', {})
    expert_cfg = _EXPERT_POOL.get(expert_name)
    if not expert_cfg:
        return json.dumps({"error": f"Unknown expert '{expert_name}'. Available: {list(_EXPERT_POOL)}"})
    
    from mica.agentic.events import AgentTurn
    _live_pending.append(AgentTurn(
        agent=expert_name, role="thinking",
        text=f"[{expert_cfg['description']}]", session_id="",
    ))
    
    user_msg = (
        f"DRIVER QUESTION:\n{question}\n\n"
        + (f"CURRENT ANALYSIS CONTEXT:\n{context}" if context else "")
    )
    
    # Expert executor: cite_finding + identify_gap are recorded; search_literature routed through literature service
    async def _expert_exec(n: str, cid: str, a: dict) -> str:
        if n in ("cite_finding", "identify_gap"):
            return json.dumps({"recorded": True, "tool": n})
        elif n == "search_literature":
            try:
                papers = await search_literature_records_fn(
                    query=a.get("query", ""),
                    max_papers=int(a.get("max_papers", 12)),
                    sources=driver_literature_sources,
                )
                summaries = [
                    f"[{p.get('paperId','?')}] {p.get('title','')} ({p.get('year','?')}): "
                    f"{(p.get('abstract') or '')[:200]}"
                    for p in (papers or [])[:12]
                ]
                return json.dumps({"count": len(summaries), "results": summaries})
            except Exception as _se:
                return json.dumps({"error": str(_se)})
        return json.dumps({"ok": True})
    
    try:
        # ── Inject prior expert context (Q6/Q7) ───────────────
        expert_messages = [{"role": "user", "content": user_msg}]
        prior_expert_ctx = await retrieval_planner_fn.build_mode_context(
            agent_name=expert_name,
            query_text=question,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
        )
        if prior_expert_ctx:
            expert_messages.insert(0, {"role": "user", "content": prior_expert_ctx})
        
        # ── UCS embodiment (Phase 4: unconditional) ─────────
        _expert_role_spec = RoleSpec(
            role_id=expert_name,
            system_prompt=expert_cfg["system"],
            max_iterations=4,
            temperature=0.3,
            output_invariants=EXPERT_INVARIANTS,
        )
        
        # Get provider/model from executor
        provider_id = args.get("provider_id", "anthropic")
        model_id = args.get("model_id")
        abort = getattr(executor_obj, '_abort_event', None)
        
        expert_answer, report_path, _role_ctx = await executor_obj._embody_role(
            role_spec=_expert_role_spec,
            task_messages=expert_messages,
            provider_id=provider_id,
            model_id=model_id,
            pending_events=_live_pending,
            abort=abort,
            parent_executor=_expert_exec,
            available_tools=filter_tools_for_lane_fn(
                getattr(executor_obj, '_EXPERT_BASE_TOOLS', []),
                lane="scientific_audit",
                depth_preset_name=getattr(getattr(executor_obj, '_depth_preset', None), 'name', 'standard'),
            ),
        )
        
        # ── Store expert output for persistence ───────────────
        _agent_memory = agent_memory_fn
        expert_entry = _agent_memory.store(
            agent_name=expert_name,
            query=question,
            synthesis=expert_answer or "",
        )
        persist_summary_fn(
            summary_store=summary_store_fn,
            entry=expert_entry,
            agent_name=expert_name,
            query=question,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
            run_id=parent_run_id,
            artifact_path=report_path,
        )
        
        return expert_answer or f"Expert '{expert_name}' could not produce a response."
        
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Expert consultation degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc), "expert": expert_name},
        )
