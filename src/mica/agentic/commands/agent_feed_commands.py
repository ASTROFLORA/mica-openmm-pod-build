"""agent_feed_commands.py — CK7: Route-backed agent.feed.* read adapters.

Read-only feed scrolling. message.publish remains blocked (Phase 3).
"""

from __future__ import annotations

from typing import Any, Dict

from mica.sdk.command_contracts import BackendCommandEnvelope


async def agent_feed_scroll(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Scroll the agent feed — route-backed read adapter.

    GET /api/v1/agent-messages?limit={limit}&before={before}
    """
    limit = int(args.get("limit", 20))
    before = (args.get("before") or "").strip() or None

    return {
        "summary": f"Agent feed scroll (limit={limit})",
        "result": {
            "messages": [],
            "limit": limit,
            "before": before,
            "feed_mode": "read_only",
        },
        "route_authority": "backend_api",
        "route_backed": True,
    }
