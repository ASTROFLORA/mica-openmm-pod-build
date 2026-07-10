"""R28 Slice D.2 — ``POST /api/v1/driver/invoke`` router.

Pillar 1 of the Level-5 blueprint: **driver-as-tool**. This router turns the
MICA runtime into a callable black box for any MCP-literate client (GHP
Copilot slash commands, other Railway services, local automation). At Rung 0
it dispatches a linear sequence of allowlisted tool calls — **no LLM loop**.
Rung 1 is a follow-up slice that spawns the full ``AgenticDriver``.

Safety rails baked in:
* ``tool_allowlist`` MUST be non-empty and every call must be in it.
* ``max_wall_seconds`` enforced per call via ``asyncio.wait_for``.
* Every invocation opens + closes a feed session; failures land a tombstone.
* Rung 0 allows only ``offline-native`` feed tools (stdlib safe, no cost).

See ``context/audits/MICA_CAPABILITIES_STATE_2026-04-21/05_LEVEL5_BLUEPRINT.md`` §B.2.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException

from mica.api_v1.auth_internal import verify_internal_token
from mica.sdk.contracts import (
    DriverInvokeRequest,
    DriverInvokeResponse,
    ToolCallRequest,
    ToolCallResponse,
)
from mica.sdk.errors import NotWiredYet, ToolNotAllowedError
from mica.sdk.transports.local import LocalTransport, _FEED_TOOLS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/driver", tags=["driver-invoke"])


# Rung 0 surface: feed tools only. Backend-native/network-native are
# gated until Rung 1 (AgenticDriver LLM loop). This is a deliberate
# constraint — see blueprint §G.
RUNG_0_SUPPORTED = frozenset(_FEED_TOOLS.keys())


def _safe_allowlist(request: DriverInvokeRequest) -> List[str]:
    rejected = [t for t in request.tool_allowlist if t not in RUNG_0_SUPPORTED]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "tool_allowlist_out_of_scope",
                "message": (
                    "Rung 0 of /driver/invoke only supports offline-native feed tools. "
                    "Rejected entries must wait for Rung 1."
                ),
                "rejected": rejected,
                "rung0_supported": sorted(RUNG_0_SUPPORTED),
            },
        )
    return list(request.tool_allowlist)


@router.post("/invoke", response_model=DriverInvokeResponse, dependencies=[Depends(verify_internal_token)])
async def invoke_driver(request: DriverInvokeRequest) -> DriverInvokeResponse:
    """Drive a bounded, allowlisted tool-call sequence. Rung 0 of Level-5."""

    if not request.tool_calls:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "empty_tool_calls",
                "message": (
                    "Rung 0 /driver/invoke expects a pre-planned tool_calls list. "
                    "Full-LLM planning arrives in Rung 1."
                ),
            },
        )

    allowlist = _safe_allowlist(request)
    session_id = f"drv-{uuid.uuid4().hex[:12]}"
    tool_results: List[ToolCallResponse] = []
    feed_post_ids: List[str] = []
    summary_fragments: List[str] = []

    # Lazy imports so the router is importable when feed storage doesn't yet exist.
    from mica.agentic.tools.agent_feed import publish_cue  # type: ignore

    t_start = time.perf_counter()
    try:
        session_open = await publish_cue(
            agent_id=request.caller_id,
            post_type="session_open",
            topic="governance",
            title=f"driver-as-tool rung-0: {request.objective[:80]}",
            body=(
                f"objective: {request.objective}\n"
                f"tool_calls: {len(request.tool_calls)}\n"
                f"allowlist: {allowlist}\n"
                f"session_id: {session_id}"
            ),
            session_id=session_id,
            parent_id=request.session_parent_id,
        )
        feed_post_ids.append(session_open["id"])

        transport = LocalTransport(allowlist=allowlist)
        deadline = t_start + request.max_wall_seconds

        for idx, call in enumerate(request.tool_calls, start=1):
            if time.perf_counter() > deadline:
                tombstone = await publish_cue(
                    agent_id=request.caller_id,
                    post_type="tombstone",
                    topic="governance",
                    title="driver-as-tool rung-0 timeout",
                    body=f"session {session_id} exceeded {request.max_wall_seconds}s budget at call {idx}",
                    session_id=session_id,
                    parent_id=session_open["id"],
                )
                feed_post_ids.append(tombstone["id"])
                return DriverInvokeResponse(
                    session_id=session_id,
                    status="timeout",
                    wall_seconds=round(time.perf_counter() - t_start, 4),
                    feed_post_ids=feed_post_ids,
                    tool_results=tool_results,
                    summary="timeout before all calls ran",
                    rung=0,
                )

            call.max_wall_seconds = min(
                call.max_wall_seconds, max(1.0, deadline - time.perf_counter())
            )

            try:
                tr = await transport.call(call)
                tool_results.append(tr)
                summary_fragments.append(f"#{idx} {call.tool_name}: ok={tr.ok} wall={tr.wall_seconds}s")
            except (ToolNotAllowedError, NotWiredYet) as exc:
                tr = ToolCallResponse(
                    ok=False,
                    tool_name=call.tool_name,
                    wall_seconds=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
                tool_results.append(tr)
                summary_fragments.append(f"#{idx} {call.tool_name}: blocked ({type(exc).__name__})")
            except asyncio.TimeoutError as exc:
                tr = ToolCallResponse(
                    ok=False,
                    tool_name=call.tool_name,
                    wall_seconds=float(call.max_wall_seconds),
                    error="per-call timeout",
                )
                tool_results.append(tr)
                summary_fragments.append(f"#{idx} {call.tool_name}: per-call timeout")

        status = "completed" if all(r.ok for r in tool_results) else "failed"
        wall = round(time.perf_counter() - t_start, 4)

        session_close = await publish_cue(
            agent_id=request.caller_id,
            post_type="session_close",
            topic="governance",
            title=f"driver-as-tool rung-0 {status}",
            body="\n".join(summary_fragments) or "no tool calls ran",
            session_id=session_id,
            parent_id=session_open["id"],
        )
        feed_post_ids.append(session_close["id"])

        return DriverInvokeResponse(
            session_id=session_id,
            status=status,
            wall_seconds=wall,
            feed_post_ids=feed_post_ids,
            tool_results=tool_results,
            summary=" | ".join(summary_fragments)[:4000],
            rung=0,
        )

    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception("driver-invoke failed for session %s", session_id)
        try:
            tombstone = await publish_cue(
                agent_id=request.caller_id,
                post_type="tombstone",
                topic="governance",
                title="driver-as-tool rung-0 unhandled exception",
                body=f"{type(exc).__name__}: {exc}",
                session_id=session_id,
            )
            feed_post_ids.append(tombstone["id"])
        except Exception:  # pragma: no cover
            pass
        raise HTTPException(status_code=500, detail=f"invoke failed: {exc}") from exc


@router.get("/rung0-capabilities")
def list_rung0_capabilities() -> dict[str, Any]:
    """Advertise which tool names are dispatchable via Rung 0 today."""

    return {
        "rung": 0,
        "supported_tools": sorted(RUNG_0_SUPPORTED),
        "planned_rung_1": ["full AgenticDriver LLM loop", "backend-native registry dispatch"],
        "blueprint_doc": "context/audits/MICA_CAPABILITIES_STATE_2026-04-21/05_LEVEL5_BLUEPRINT.md",
    }
