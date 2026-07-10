"""DD-CS-016AC: Agent-feed native-execution branch.

The six agent-feed tools execute natively via mica.agentic.tools.agent_feed
(JSONL on disk, stdlib only). No MCP round-trip.

Extracted from _build_loop_executor to keep the executor closure lean.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Coroutine, Dict

FEED_TOOL_NAMES: frozenset = frozenset({
    "open_session_signature",
    "update_session_progress",
    "publish_cue",
    "scroll_agent_feed",
    "feed_stats",
    "feed_thread",
})


async def run_feed_tool_branch(
    *,
    name: str,
    args: Dict[str, Any],
    invoke_feed_tool_fn: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, Any]],
) -> str:
    """Execute a native agent-feed tool and return a JSON-encoded result string."""
    try:
        result = await invoke_feed_tool_fn(name, dict(args or {}))
        return json.dumps(
            {"status": "ok", "tool": name, "result": result},
            ensure_ascii=False,
            default=str,
        )
    except TypeError as te:
        return json.dumps(
            {
                "status": "error",
                "tool": name,
                "error": f"bad_args: {te}",
                "args_keys": list((args or {}).keys()),
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as feed_exc:
        return json.dumps(
            {"status": "error", "tool": name, "error": str(feed_exc)},
            ensure_ascii=False,
            default=str,
        )
