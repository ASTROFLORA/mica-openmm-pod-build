"""Specialist delegation helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Callable, Dict, Optional


def build_specialist_response_adapter(
    *,
    transport_payload_or_degraded_fn: Callable[..., str],
    degraded_tool_response_fn: Callable[..., str],
) -> Callable[..., str]:
    def _specialist_response_adapter(
        tool_name: str,
        message_or_result: Any,
        *,
        args_payload: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        if isinstance(message_or_result, dict):
            return transport_payload_or_degraded_fn(
                tool_name,
                message_or_result,
                args_payload=args_payload,
            )
        return degraded_tool_response_fn(
            tool_name,
            str(message_or_result),
            args_payload=args_payload,
            extra=extra,
        )

    return _specialist_response_adapter


async def run_consult_specialist_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    pending: Any,
    fallback_transport_execution_fn,
    degraded_tool_response_fn,
) -> str:
    """Delegate to specialist via Modal pool or host driver.
    
    Tier 2: Modal specialist path with lazy pool initialization.
    Tier 0/1: Host specialist or fallback transport paths.
    """
    import uuid as _uuid_spec
    from mica.agentic.events import AgentTurn
    
    _live_pending = pending if pending is not None else []
    _spec_id = str(_uuid_spec.uuid4())[:8]
    target = args.get("specialist", "biodynamo")
    task = args.get("task", "")
    context = args.get("context", {})
    force_modal = bool(args.get("force_modal", False))
    
    _live_pending.append(AgentTurn(
        agent=target, role="thinking",
        text="[delegated by driver]", session_id=_spec_id,
    ))
    
    try:
        # ── Tier 2: Modal Specialist path (V2 architecture) ──────
        from mica.sandbox.specialist_task import (
            should_use_modal_specialist, detect_specialist_operation,
            ModalSpecialistTask,
        )
        _pool = getattr(executor_obj, '_specialist_pool', None)
        host_deps = bool(getattr(executor_obj, 'specialist_drivers', None)
                         and target in executor_obj.specialist_drivers)
        if _pool is not None and should_use_modal_specialist(
            target, task, context,
            force_modal=force_modal,
            host_deps_available=host_deps,
        ):
            _live_pending.append(AgentTurn(
                agent=target, role="thinking",
                text="[routing to Modal GPU specialist]",
                session_id=_spec_id,
            ))
            # Build ModalSpecialistTask
            operation = detect_specialist_operation(target, task, context)
            modal_task = ModalSpecialistTask(
                specialist=target,
                operation=operation,
                query=task,
                parameters=context if isinstance(context, dict) else {},
                input_files=args.get("input_files", {}),
                input_context=str(context)[:4000],
                gpu=args.get("gpu"),
                timeout=int(args.get("timeout", 600)),
                expected_outputs=args.get("expected_outputs", []),
            )
            modal_result = await _pool.spawn(modal_task)
            answer = modal_result.summary_for_context(max_len=2000)
            _live_pending.append(AgentTurn(
                agent=target, role="speaking",
                text=answer[:2000], session_id=_spec_id,
            ))
            _live_pending.append(AgentTurn(
                agent=target, role="done", text="",
                session_id=_spec_id,
            ))
            return answer

        # ── Tier 0/1: Host specialist path (existing V1 logic) ───
        if getattr(executor_obj, "specialist_drivers", None):
            from mica.drivers.agent_hub import SimpleAgentHub
            hub = SimpleAgentHub(drivers=executor_obj.specialist_drivers)
            result = await hub.route(target, query=task, context=context)
        else:
            result = await fallback_transport_execution_fn(target, task)
        
        if isinstance(result, dict) and result.get("status") == "FAILED":
            return degraded_tool_response_fn(name, result, args_payload=args)
        
        answer = str(result.get("answer", result.get("response", json.dumps(result, default=str))))
        _live_pending.append(AgentTurn(
            agent=target, role="speaking", text=answer[:2000], session_id=_spec_id,
        ))
        _live_pending.append(AgentTurn(agent=target, role="done", text="", session_id=_spec_id))
        return answer
        
    except Exception as exc:
        _live_pending.append(AgentTurn(agent=target, role="done", text=f"Error: {exc}", session_id=_spec_id))
        return degraded_tool_response_fn(
            name,
            "Specialist delegation degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc), "specialist": target},
        )
