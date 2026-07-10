"""APV-07 ProductEventEnvelope HTTP surface."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency
from mica.experience.product_events import (
    ProductEventEnvelope,
    ProductEventError,
    ReplayBatch,
    acknowledge_replay_cursor,
    adapt_legacy_ws_message,
    publish_product_event,
    resume_product_events,
)
from mica.identity.effective_context import EffectiveContext

router = APIRouter(prefix="/api/v1/product-events", tags=["product-events"])


class SubjectRefIn(BaseModel):
    type: str
    id: str


class PublishEventRequest(BaseModel):
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    subject_refs: List[SubjectRefIn] = Field(default_factory=list)
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    receipt_ref: Optional[str] = None
    event_id: Optional[str] = None
    completeness: Literal["partial", "terminal"] = "terminal"
    session_id: Optional[str] = None


class AckCursorRequest(BaseModel):
    replay_cursor: str
    session_id: Optional[str] = None


class AdaptLegacyRequest(BaseModel):
    legacy_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: Optional[str] = None


def _http(exc: ProductEventError) -> HTTPException:
    detail = str(exc)
    status = 403 if detail.startswith("permission_denied") else 400
    return HTTPException(status_code=status, detail=detail)


@router.post("/publish", response_model=ProductEventEnvelope, status_code=201)
async def publish_event(
    body: PublishEventRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return publish_product_event(
            ctx=ctx,
            event_type=body.event_type,
            payload=body.payload,
            subject_refs=[r.model_dump() for r in body.subject_refs],
            correlation_id=body.correlation_id,
            causation_id=body.causation_id,
            receipt_ref=body.receipt_ref,
            event_id=body.event_id,
            completeness=body.completeness,
            session_id=body.session_id,
        )
    except ProductEventError as exc:
        raise _http(exc) from exc


@router.post("/ack", response_model=dict)
async def ack_cursor(
    body: AckCursorRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        seq = acknowledge_replay_cursor(
            ctx=ctx,
            cursor=body.replay_cursor,
            session_id=body.session_id,
        )
        return {"acknowledged_sequence": seq, "replay_cursor": body.replay_cursor}
    except ProductEventError as exc:
        raise _http(exc) from exc


@router.get("/resume", response_model=ReplayBatch)
async def resume_events(
    replay_cursor: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return resume_product_events(
            ctx=ctx,
            replay_cursor=replay_cursor,
            session_id=session_id,
            limit=limit,
        )
    except ProductEventError as exc:
        raise _http(exc) from exc


@router.post("/adapt-legacy", response_model=ProductEventEnvelope, status_code=201)
async def adapt_legacy(
    body: AdaptLegacyRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return adapt_legacy_ws_message(
            ctx=ctx,
            legacy_type=body.legacy_type,
            payload=body.payload,
            correlation_id=body.correlation_id,
        )
    except ProductEventError as exc:
        raise _http(exc) from exc
