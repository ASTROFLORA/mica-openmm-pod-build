"""APV-10 ProductEventBridge — agentic/protocol frames → ProductEventEnvelope.

Authority: North Star APV-10 / Frontend Runtime Contract §8
Hard gate support: live agent run visible on /ws/product without a second bus.

Consumes: publish_product_event / adapt_legacy_ws_message (APV-07)
Does not own: /ws/mica chat authority, protocol executor, storage outbox.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mica.experience.product_events import (
    ProductEventEnvelope,
    ProductEventError,
    adapt_legacy_ws_message,
    publish_product_event,
)
from mica.identity.effective_context import EffectiveContext, personal_home_scope_id

logger = logging.getLogger(__name__)

_BRIDGE_ENABLED_ENV = "MICA_PRODUCT_EVENT_BRIDGE"


def product_event_bridge_enabled() -> bool:
    raw = (os.getenv(_BRIDGE_ENABLED_ENV) or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def context_from_actor(
    *,
    actor_user_id: str,
    session_id: str,
    active_scope_id: str | None = None,
    permission_fingerprint: str | None = None,
) -> EffectiveContext:
    home = personal_home_scope_id(actor_user_id)
    scope = active_scope_id or home
    return EffectiveContext(
        actor_user_id=actor_user_id,
        session_id=session_id,
        active_scope_id=scope,
        home_scope_id=home,
        destination_scope_id=scope,
        permission_fingerprint=permission_fingerprint or f"fp:bridge:{actor_user_id}",
        policy_snapshot_id="policy_snapshot:bridge",
    )


_LEGACY_TO_PRODUCT: dict[str, str] = {
    "STATE_UPDATE": "run.node.progress",
    "TEXT_MESSAGE": "agent.message.delta",
    "STREAM_TOKEN": "agent.message.delta",
    "STREAM_CHUNK": "agent.message.delta",
    "STREAM_START": "agent.message.delta",
    "STREAM_END": "agent.message.delta",
    "ACTION_STEP": "tool.call.started",
    "TOOL_CALL": "tool.call.started",
    "TOOL_RESULT": "tool.call.completed",
    "APPROVAL_REQUIRED": "approval.required",
    "APPROVAL_RESOLVED": "approval.resolved",
}


def map_legacy_frame_type(frame_type: str) -> str:
    return _LEGACY_TO_PRODUCT.get(str(frame_type).strip().upper(), "run.node.progress")


def bridge_legacy_ws_frame(
    *,
    ctx: EffectiveContext,
    frame_type: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    event_id: str | None = None,
) -> ProductEventEnvelope:
    """One-way adapt of a legacy /ws/mica frame into the product outbox."""
    normalized = str(frame_type).strip().upper()
    if normalized in {"STATE_UPDATE", "TEXT_MESSAGE", "STREAM_TOKEN", "STREAM_CHUNK", "ACTION_STEP"}:
        return adapt_legacy_ws_message(
            ctx=ctx,
            legacy_type=normalized,
            payload=payload,
            correlation_id=correlation_id,
        )
    return publish_product_event(
        ctx=ctx,
        event_type=map_legacy_frame_type(normalized),
        payload={"legacy_type": normalized, **dict(payload or {})},
        correlation_id=correlation_id,
        event_id=event_id,
        session_id=session_id or ctx.session_id,
        completeness="partial",
    )


def try_bridge_agentic_frame(
    *,
    actor_user_id: str,
    session_id: str,
    frame_type: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    active_scope_id: str | None = None,
) -> ProductEventEnvelope | None:
    """Fail-soft bridge for /ws/mica send path. Never raises into chat."""
    if not product_event_bridge_enabled():
        return None
    if not actor_user_id or not session_id:
        return None
    try:
        ctx = context_from_actor(
            actor_user_id=str(actor_user_id),
            session_id=str(session_id),
            active_scope_id=active_scope_id,
        )
        return bridge_legacy_ws_frame(
            ctx=ctx,
            frame_type=frame_type,
            payload=payload,
            correlation_id=correlation_id or f"corr:ws:{session_id}",
            session_id=session_id,
        )
    except (ProductEventError, ValueError, TypeError) as exc:
        logger.debug("product event bridge skipped: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover - never break agentic WS
        logger.warning("product event bridge failed soft: %s", exc)
        return None


def project_visual_agent_run(
    *,
    ctx: EffectiveContext,
    correlation_id: str,
    user_message: str,
    assistant_delta: str,
    plan_summary: str,
    execution_mode: str = "protocol",
    dag_nodes: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    approvals: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    receipts: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
) -> list[ProductEventEnvelope]:
    """Emit a complete harness-visible run onto the product outbox.

    Used by tests and BFF bootstrap when a live agentic loop is not attached.
    """
    sid = session_id or ctx.session_id
    corr = correlation_id
    emitted: list[ProductEventEnvelope] = []

    def _pub(
        event_type: str,
        payload: dict[str, Any],
        *,
        subjects: list[dict[str, str]] | None = None,
        completeness: str = "terminal",
        receipt_ref: str | None = None,
    ) -> ProductEventEnvelope:
        env = publish_product_event(
            ctx=ctx,
            event_type=event_type,
            payload=payload,
            subject_refs=subjects,
            correlation_id=corr,
            receipt_ref=receipt_ref,
            session_id=sid,
            completeness=completeness,  # type: ignore[arg-type]
        )
        emitted.append(env)
        return env

    _pub(
        "agent.message.delta",
        {"role": "user", "text": user_message},
        completeness="partial",
    )
    _pub(
        "execution.mode.selected",
        {"mode": execution_mode, "rationale": "harness_projection"},
    )
    _pub(
        "agent.plan.created",
        {"summary": plan_summary, "mode": execution_mode},
    )
    _pub(
        "agent.message.delta",
        {"role": "assistant", "text": assistant_delta},
        completeness="partial",
    )

    for node in dag_nodes or []:
        node_id = str(node.get("node_id") or node.get("id") or "node")
        status = str(node.get("status") or "completed").lower()
        subjects = [{"type": "protocol_node", "id": node_id}]
        if status in {"started", "running", "queued"}:
            _pub("run.node.started", {"node": node}, subjects=subjects, completeness="partial")
        elif status in {"failed", "error"}:
            _pub("run.node.failed", {"node": node}, subjects=subjects)
        else:
            _pub(
                "run.node.progress",
                {"node": node, "progress": node.get("progress", 1.0)},
                subjects=subjects,
                completeness="partial",
            )
            _pub("run.node.completed", {"node": node}, subjects=subjects)

    for tool in tool_calls or []:
        name = str(tool.get("name") or tool.get("tool") or "tool")
        subjects = [{"type": "tool", "id": name}]
        _pub(
            "tool.call.started",
            {"tool": tool},
            subjects=subjects,
            completeness="partial",
        )
        _pub(
            "tool.call.completed",
            {"tool": tool, "ok": bool(tool.get("ok", True))},
            subjects=subjects,
        )

    for approval in approvals or []:
        case_id = str(approval.get("case_id") or approval.get("id") or "approval")
        subjects = [{"type": "approval", "id": case_id}]
        _pub("approval.required", {"approval": approval}, subjects=subjects, completeness="partial")
        if approval.get("resolved"):
            _pub(
                "approval.resolved",
                {
                    "approval": approval,
                    "decision": approval.get("decision", "approved"),
                },
                subjects=subjects,
            )

    for artifact in artifacts or []:
        art_id = str(artifact.get("artifact_id") or artifact.get("id") or "artifact")
        subjects = [{"type": "artifact", "id": art_id}]
        _pub("artifact.staged", {"artifact": artifact}, subjects=subjects, completeness="partial")
        if artifact.get("active", True):
            _pub("artifact.activated", {"artifact": artifact}, subjects=subjects)

    for receipt in receipts or []:
        ref = str(receipt.get("receipt_ref") or receipt.get("id") or f"receipt:{corr}")
        _pub(
            "receipt.issued",
            {"receipt": receipt},
            subjects=[{"type": "receipt", "id": ref}],
            receipt_ref=ref,
        )

    _pub(
        "protocol.status.changed",
        {"status": "completed", "correlation_id": corr},
    )
    return emitted


__all__ = [
    "bridge_legacy_ws_frame",
    "context_from_actor",
    "map_legacy_frame_type",
    "product_event_bridge_enabled",
    "project_visual_agent_run",
    "try_bridge_agentic_frame",
]
