"""Build the final gated executor wrapper for AgenticDriver loop execution."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

from mica.agentic.tool_capability_registry import build_tool_capability_matrix

from .resource_cleanup_service import cleanup_execution_resources
from .tool_execution_gate_service import (
    build_publish_heartbeat_adapter,
    heartbeat_skip_tools_policy,
    run_gated_tool_call,
)


def build_loop_gated_executor(
    *,
    execute_fn: Callable[[str, str, Dict[str, Any]], Awaitable[str]],
    invoke_feed_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Any]],
    pre_dispatch_gate_fn: Callable[[str, Dict[str, Any]], Awaitable[Optional[str]]],
    dependency_state_for_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
    normalize_tool_payload_fn: Callable[..., str],
    public_tool_names: Sequence[str],
    spawn_tool_names: Sequence[str],
    cleanup_literature_service_fn: Optional[Callable[[], Awaitable[None]]] = None,
) -> Callable[[str, str, Dict[str, Any]], Awaitable[str]]:
    """Create the gated tool executor with cleanup and capability metadata."""

    heartbeat_skip_tools = heartbeat_skip_tools_policy()
    publish_heartbeat_fn = build_publish_heartbeat_adapter(invoke_feed_tool_fn)

    async def _cleanup_fetcher() -> None:
        if cleanup_literature_service_fn is not None:
            await cleanup_literature_service_fn()
        await cleanup_execution_resources(
            literature_service=None,
            sandbox_manager=getattr(execute_fn, "_sandbox_mgr", None),
        )

    async def gated_executor(name: str, call_id: str, args: Dict[str, Any]) -> str:
        return await run_gated_tool_call(
            name,
            call_id,
            args,
            pre_dispatch_gate_fn=pre_dispatch_gate_fn,
            dependency_state_for_tool_fn=dependency_state_for_tool_fn,
            execute_fn=execute_fn,
            normalize_tool_payload_fn=normalize_tool_payload_fn,
            heartbeat_skip_tools=heartbeat_skip_tools,
            publish_heartbeat_fn=publish_heartbeat_fn,
        )

    gated_executor._cleanup = _cleanup_fetcher  # type: ignore[attr-defined]
    gated_executor._tool_capability_matrix = build_tool_capability_matrix(  # type: ignore[attr-defined]
        list(public_tool_names) + list(spawn_tool_names)
    )
    return gated_executor
