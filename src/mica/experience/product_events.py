"""APV-07 ProductEventEnvelope — scoped, sequenced, resumable product events.

Authority: Frontend Runtime Contract V0.6 §4 / North Star APV-07
Hard gate: reconnect produces no loss/duplication.

Consumes: EffectiveContext (APV-01)
Does not own: agentic ws_bridge authority, storage outbox (Lane S), scheduler outbox.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from mica.identity.effective_context import EffectiveContext

PRODUCT_EVENT_SCHEMA = "urn:mica:event:product:v1"

ProductEventType = Literal[
    "agent.message.delta",
    "agent.plan.created",
    "agent.refusal",
    "execution.mode.selected",
    "protocol.submitted",
    "protocol.status.changed",
    "gog.child.status.changed",
    "run.node.started",
    "run.node.progress",
    "run.node.completed",
    "run.node.failed",
    "tool.call.started",
    "tool.call.completed",
    "policy.decision",
    "approval.required",
    "approval.resolved",
    "artifact.staged",
    "artifact.activated",
    "artifact.preview.ready",
    "study.result.attached",
    "study.result.interpreted",
    "workspace.working_set.changed",
    "workspace.view.changed",
    "workspace.surface.changed",
    "receipt.issued",
    "evidence.bound",
    "governance.case.changed",
    "study.closed",
    "study.derived",
    "ui.proposal.created",
]


class ProductEventError(ValueError):
    """Fail-closed product event / replay error."""


class SubjectRef(BaseModel):
    type: str
    id: str


class ProductEventEnvelope(BaseModel):
    """Canonical product WS/outbox event envelope."""

    schema_urn: Literal["urn:mica:event:product:v1"] = Field(
        default=PRODUCT_EVENT_SCHEMA,
        alias="schema",
    )
    event_id: str
    event_type: str
    sequence: int
    occurred_at: datetime
    session_id: str
    actor_user_id: str
    effective_scope_id: str
    subject_refs: list[SubjectRef] = Field(default_factory=list)
    correlation_id: str
    causation_id: str | None = None
    receipt_ref: str | None = None
    replay_cursor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    completeness: Literal["partial", "terminal"] = "terminal"

    model_config = {"populate_by_name": True}

    @field_validator("event_type")
    @classmethod
    def _non_empty_type(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("event_type is required")
        return normalized


class ReplayBatch(BaseModel):
    session_id: str
    from_cursor: str | None
    acknowledged_sequence: int
    events: list[ProductEventEnvelope]
    next_cursor: str | None
    truncated: bool = False


def make_replay_cursor(session_id: str, sequence: int) -> str:
    return f"cur:{session_id}:{sequence}"


def parse_replay_cursor(cursor: str | None) -> tuple[str | None, int]:
    """Return (session_id, sequence). sequence 0 means start-of-stream."""
    if cursor is None or not str(cursor).strip():
        return None, 0
    raw = str(cursor).strip()
    if not raw.startswith("cur:"):
        raise ProductEventError(f"invalid replay_cursor: {raw}")
    parts = raw.split(":")
    if len(parts) != 3:
        raise ProductEventError(f"invalid replay_cursor: {raw}")
    _, session_id, seq_s = parts
    try:
        seq = int(seq_s)
    except ValueError as exc:
        raise ProductEventError(f"invalid replay_cursor sequence: {raw}") from exc
    if seq < 0:
        raise ProductEventError("replay_cursor sequence must be >= 0")
    return session_id, seq


class _SessionStream:
    def __init__(self, session_id: str, owner_user_id: str, home_scope_id: str) -> None:
        self.session_id = session_id
        self.owner_user_id = owner_user_id
        self.home_scope_id = home_scope_id
        self._events: list[ProductEventEnvelope] = []
        self._by_id: dict[str, ProductEventEnvelope] = {}
        self._next_sequence = 1
        self.acked_sequence = 0

    def publish(
        self,
        *,
        ctx: EffectiveContext,
        event_type: str,
        payload: dict[str, Any],
        subject_refs: list[SubjectRef] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        receipt_ref: str | None = None,
        event_id: str | None = None,
        completeness: Literal["partial", "terminal"] = "terminal",
    ) -> ProductEventEnvelope:
        if ctx.actor_user_id != self.owner_user_id:
            raise ProductEventError("permission_denied: cannot publish to another actor stream")
        eid = event_id or str(uuid.uuid4())
        if eid in self._by_id:
            return self._by_id[eid]

        seq = self._next_sequence
        self._next_sequence += 1
        cursor = make_replay_cursor(self.session_id, seq)
        envelope = ProductEventEnvelope(
            schema=PRODUCT_EVENT_SCHEMA,
            event_id=eid,
            event_type=event_type,
            sequence=seq,
            occurred_at=datetime.now(timezone.utc),
            session_id=self.session_id,
            actor_user_id=ctx.actor_user_id,
            effective_scope_id=ctx.active_scope_id,
            subject_refs=list(subject_refs or []),
            correlation_id=correlation_id or f"corr:{uuid.uuid4().hex[:16]}",
            causation_id=causation_id,
            receipt_ref=receipt_ref,
            replay_cursor=cursor,
            payload=dict(payload or {}),
            completeness=completeness,
        )
        self._events.append(envelope)
        self._by_id[eid] = envelope
        return envelope

    def acknowledge(self, *, ctx: EffectiveContext, cursor: str) -> int:
        if ctx.actor_user_id != self.owner_user_id:
            raise ProductEventError("permission_denied: cannot ack another actor stream")
        session_id, seq = parse_replay_cursor(cursor)
        if session_id is not None and session_id != self.session_id:
            raise ProductEventError("replay_cursor session mismatch")
        if seq > len(self._events):
            raise ProductEventError("replay_cursor ahead of stream head")
        if seq < self.acked_sequence:
            # Idempotent ack of older cursor — no rewind of authority, keep max.
            return self.acked_sequence
        self.acked_sequence = seq
        return self.acked_sequence

    def replay(
        self,
        *,
        ctx: EffectiveContext,
        from_cursor: str | None,
        limit: int = 100,
    ) -> ReplayBatch:
        if ctx.actor_user_id != self.owner_user_id:
            raise ProductEventError("permission_denied: cannot replay unauthorized session stream")
        # Permission recheck: active scope must match event scope or home scope.
        session_id, after_seq = parse_replay_cursor(from_cursor)
        if session_id is not None and session_id != self.session_id:
            raise ProductEventError("replay_cursor session mismatch")

        selected: list[ProductEventEnvelope] = []
        for event in self._events:
            if event.sequence <= after_seq:
                continue
            if not self._event_visible(ctx, event):
                continue
            selected.append(event)
            if len(selected) >= limit:
                break

        truncated = False
        if selected:
            last_seq = selected[-1].sequence
            remaining = any(
                e.sequence > last_seq and self._event_visible(ctx, e) for e in self._events
            )
            truncated = remaining
            next_cursor = selected[-1].replay_cursor
        else:
            next_cursor = from_cursor or make_replay_cursor(self.session_id, after_seq)

        return ReplayBatch(
            session_id=self.session_id,
            from_cursor=from_cursor,
            acknowledged_sequence=self.acked_sequence,
            events=selected,
            next_cursor=next_cursor,
            truncated=truncated,
        )

    def _event_visible(self, ctx: EffectiveContext, event: ProductEventEnvelope) -> bool:
        if event.actor_user_id != ctx.actor_user_id:
            return False
        if event.effective_scope_id in (ctx.active_scope_id, ctx.home_scope_id):
            return True
        # Fail-closed for foreign scopes on reconnect.
        return False


class ProductEventOutbox:
    """Process-authoritative product event outbox (session streams)."""

    def __init__(self) -> None:
        self._streams: dict[str, _SessionStream] = {}
        self._lock = threading.RLock()

    def get_or_create_stream(
        self, *, session_id: str, owner_user_id: str, home_scope_id: str
    ) -> _SessionStream:
        with self._lock:
            stream = self._streams.get(session_id)
            if stream is None:
                stream = _SessionStream(session_id, owner_user_id, home_scope_id)
                self._streams[session_id] = stream
                return stream
            if stream.owner_user_id != owner_user_id:
                raise ProductEventError("permission_denied: session owned by another actor")
            return stream

    def clear(self) -> None:
        with self._lock:
            self._streams.clear()


_OUTBOX: ProductEventOutbox | None = None


def get_product_event_outbox() -> ProductEventOutbox:
    global _OUTBOX
    if _OUTBOX is None:
        _OUTBOX = ProductEventOutbox()
    return _OUTBOX


def reset_product_event_outbox_for_tests() -> ProductEventOutbox:
    global _OUTBOX
    _OUTBOX = ProductEventOutbox()
    return _OUTBOX


def publish_product_event(
    *,
    ctx: EffectiveContext,
    event_type: str,
    payload: dict[str, Any] | None = None,
    subject_refs: list[dict[str, str]] | list[SubjectRef] | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    receipt_ref: str | None = None,
    event_id: str | None = None,
    completeness: Literal["partial", "terminal"] = "terminal",
    session_id: str | None = None,
) -> ProductEventEnvelope:
    sid = session_id or ctx.session_id
    outbox = get_product_event_outbox()
    stream = outbox.get_or_create_stream(
        session_id=sid,
        owner_user_id=ctx.actor_user_id,
        home_scope_id=ctx.home_scope_id,
    )
    refs: list[SubjectRef] = []
    for item in subject_refs or []:
        if isinstance(item, SubjectRef):
            refs.append(item)
        else:
            refs.append(SubjectRef(type=str(item["type"]), id=str(item["id"])))
    return stream.publish(
        ctx=ctx,
        event_type=event_type,
        payload=dict(payload or {}),
        subject_refs=refs,
        correlation_id=correlation_id,
        causation_id=causation_id,
        receipt_ref=receipt_ref,
        event_id=event_id,
        completeness=completeness,
    )


def acknowledge_replay_cursor(*, ctx: EffectiveContext, cursor: str, session_id: str | None = None) -> int:
    sid = session_id or ctx.session_id
    cursor_session, _ = parse_replay_cursor(cursor)
    if cursor_session and cursor_session != sid:
        raise ProductEventError("replay_cursor session mismatch")
    stream = get_product_event_outbox().get_or_create_stream(
        session_id=sid,
        owner_user_id=ctx.actor_user_id,
        home_scope_id=ctx.home_scope_id,
    )
    return stream.acknowledge(ctx=ctx, cursor=cursor)


def resume_product_events(
    *,
    ctx: EffectiveContext,
    replay_cursor: str | None,
    session_id: str | None = None,
    limit: int = 100,
) -> ReplayBatch:
    sid = session_id or ctx.session_id
    if replay_cursor:
        cursor_session, _ = parse_replay_cursor(replay_cursor)
        if cursor_session and cursor_session != sid:
            raise ProductEventError("replay_cursor session mismatch")
    stream = get_product_event_outbox().get_or_create_stream(
        session_id=sid,
        owner_user_id=ctx.actor_user_id,
        home_scope_id=ctx.home_scope_id,
    )
    return stream.replay(ctx=ctx, from_cursor=replay_cursor, limit=limit)


def adapt_legacy_ws_message(
    *,
    ctx: EffectiveContext,
    legacy_type: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> ProductEventEnvelope:
    """Gateway adapter: map legacy STATE_UPDATE/TEXT_MESSAGE into ProductEventEnvelope.

    Does not extend legacy types as authorities — one-way adaptation only.
    """
    mapping = {
        "STATE_UPDATE": "run.node.progress",
        "TEXT_MESSAGE": "agent.message.delta",
        "STREAM_TOKEN": "agent.message.delta",
        "STREAM_CHUNK": "agent.message.delta",
        "ACTION_STEP": "tool.call.started",
    }
    event_type = mapping.get(legacy_type, "run.node.progress")
    return publish_product_event(
        ctx=ctx,
        event_type=event_type,
        payload={"legacy_type": legacy_type, **dict(payload or {})},
        correlation_id=correlation_id,
        completeness="partial",
    )


def stream_fingerprint(session_id: str) -> str:
    outbox = get_product_event_outbox()
    stream = outbox._streams.get(session_id)
    if stream is None:
        return hashlib.sha256(f"{session_id}:empty".encode()).hexdigest()
    material = "|".join(e.event_id for e in stream._events)
    return hashlib.sha256(f"{session_id}:{material}".encode()).hexdigest()


__all__ = [
    "PRODUCT_EVENT_SCHEMA",
    "ProductEventEnvelope",
    "ProductEventError",
    "ProductEventOutbox",
    "ReplayBatch",
    "SubjectRef",
    "acknowledge_replay_cursor",
    "adapt_legacy_ws_message",
    "get_product_event_outbox",
    "make_replay_cursor",
    "parse_replay_cursor",
    "publish_product_event",
    "reset_product_event_outbox_for_tests",
    "resume_product_events",
    "stream_fingerprint",
]
