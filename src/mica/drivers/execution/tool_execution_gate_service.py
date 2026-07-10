"""Execution gate helpers extracted from AgenticDriver loop executor."""

import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Set


# Slice-2 §C5 — skip feed-native tools so observability cues do not recurse.
# Slice-3 §C5 — run_driver_experiment already emits its own cue pair.
DEFAULT_HEARTBEAT_SKIP_TOOLS = frozenset(
    {
        "publish_cue",
        "scroll_agent_feed",
        "feed_stats",
        "feed_thread",
        "open_session_signature",
        "update_session_progress",
        "run_driver_experiment",
    }
)


def heartbeat_skip_tools_policy(*, extra_skip_tools: Optional[Set[str]] = None) -> Set[str]:
    policy = set(DEFAULT_HEARTBEAT_SKIP_TOOLS)
    if extra_skip_tools:
        policy.update(str(item).strip() for item in extra_skip_tools if str(item).strip())
    return policy


async def run_gated_tool_call(
    name: str,
    call_id: str,
    args: Dict[str, Any],
    *,
    pre_dispatch_gate_fn: Callable[[str, Dict[str, Any]], Awaitable[Optional[str]]],
    dependency_state_for_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
    execute_fn: Callable[[str, str, Dict[str, Any]], Awaitable[str]],
    normalize_tool_payload_fn: Callable[..., str],
    heartbeat_skip_tools: Set[str],
    publish_heartbeat_fn: Optional[Callable[[str, str, Dict[str, Any], int, str], Awaitable[None]]] = None,
) -> str:
    gate_response = await pre_dispatch_gate_fn(name, args)
    if gate_response is not None:
        return gate_response

    dependency_state = await dependency_state_for_tool_fn(name, args)

    hb_t0 = time.monotonic()
    hb_status = "ok"
    try:
        raw_text = await execute_fn(name, call_id, args)
    except BaseException:
        hb_status = "error"
        raise
    finally:
        hb_elapsed_ms = int((time.monotonic() - hb_t0) * 1000)
        if publish_heartbeat_fn is not None and name not in heartbeat_skip_tools:
            try:
                await publish_heartbeat_fn(name, call_id, args, hb_elapsed_ms, hb_status)
            except Exception:
                # Heartbeats are best-effort. Never break tool execution on feed failure.
                pass

    return normalize_tool_payload_fn(name, raw_text, dependency_state=dependency_state)


def build_tool_heartbeat_body(
    *,
    call_id: str,
    args: Dict[str, Any],
    elapsed_ms: int,
    status: str,
) -> str:
    return json.dumps(
        {
            "call_id": call_id,
            "args_preview": json.dumps(args or {}, default=str)[:500],
            "elapsed_ms": elapsed_ms,
            "status": status,
        },
        default=str,
    )


async def publish_tool_heartbeat_cue(
    invoke_feed_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Any]],
    tool_name: str,
    call_id: str,
    args: Dict[str, Any],
    elapsed_ms: int,
    status: str,
) -> None:
    heartbeat_body = build_tool_heartbeat_body(
        call_id=call_id,
        args=args,
        elapsed_ms=elapsed_ms,
        status=status,
    )
    await invoke_feed_tool_fn(
        "publish_cue",
        {
            "post_type": "cue",
            "topic": "observability",
            "title": f"tool_call {tool_name}",
            "body": heartbeat_body,
        },
    )


def build_publish_heartbeat_adapter(
    invoke_feed_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Any]],
) -> Callable[[str, str, Dict[str, Any], int, str], Awaitable[None]]:
    async def _publish(
        tool_name: str,
        call_id: str,
        args: Dict[str, Any],
        elapsed_ms: int,
        status: str,
    ) -> None:
        await publish_tool_heartbeat_cue(
            invoke_feed_tool_fn,
            tool_name,
            call_id,
            args,
            elapsed_ms,
            status,
        )

    return _publish