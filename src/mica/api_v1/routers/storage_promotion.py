"""S-R2 · Storage Promotion Router — global-curated promotion with gate.

Exposes:
  POST /api/v1/storage/promotions              → request_promotion (F0+F1)
  GET  /api/v1/storage/promotions/{ref}        → status
  POST /api/v1/storage/promotions/{ref}/retract → retract (F4)
  GET  /api/v1/storage/curated                 → list active curated records
  GET  /api/v1/storage/curated/{ref}           → get specific curated record
  GET  /api/v1/storage/promotions/metrics      → metrics
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mica.storage.promotion import (
    CuratedRecord,
    PromotionReceipt,
    PromotionRequest,
    PromotionRetraction,
)
from mica.storage.promotion_store import (
    PromotionError,
    PromotionStore,
    get_promotion_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/storage/promotions",
    tags=["storage-promotion"],
)


def _get_store() -> PromotionStore:
    return get_promotion_store()


# ── F0 · Request schema ─────────────────────────────────────────────────────

class PromotionRequestBody(BaseModel):
    source_ref: str
    idempotency_key: str
    gate_ref: str = ""
    provenance_refs: List[str] = []
    requester_ref: str = ""
    notes: str = ""
    license: str = ""
    source_artifact_kind: str = ""
    checksum_ref: str = ""


class PromotionResponse(BaseModel):
    promotion_ref: str = ""
    source_ref: str = ""
    state: str = ""
    gate_ref: str = ""
    target_record_ref: str = ""
    provenance_branch_ref: str = ""
    receipt_ref: str = ""
    decision: str = ""
    reason_codes: List[str] = []
    blocked_reason: str = ""


class RetractionResponse(BaseModel):
    promotion_ref: str = ""
    state: str = ""
    retraction_receipt_ref: str = ""
    retraction_reason: str = ""
    retracted_at: str = ""
    lineage_preserved: bool = True


# ── F0+F1+F2 · request_promotion ────────────────────────────────────────────

@router.post("", response_model=PromotionResponse)
async def request_promotion(
    body: PromotionRequestBody,
    store: PromotionStore = Depends(_get_store),
):
    """F0+F1+F2: Crear una PromotionRequest.

    Sin gate_ref => state=blocked (fail-closed, nunca automatico).
    Con gate_ref aprobado => state=promoted + projection a global-curated.
    """
    req, receipt = store.request_promotion(
        source_ref=body.source_ref,
        idempotency_key=body.idempotency_key,
        gate_ref=body.gate_ref,
        provenance_refs=tuple(body.provenance_refs),
        requester_ref=body.requester_ref,
        notes=body.notes,
        license=body.license,
        source_artifact_kind=body.source_artifact_kind,
        checksum_ref=body.checksum_ref,
    )
    return PromotionResponse(
        promotion_ref=req.promotion_ref,
        source_ref=req.source_ref,
        state=req.state,
        gate_ref=req.gate_ref,
        target_record_ref=req.target_record_ref,
        provenance_branch_ref=req.provenance_branch_ref,
        receipt_ref=receipt.receipt_ref,
        decision=receipt.decision,
        reason_codes=list(receipt.reason_codes),
        blocked_reason=req.blocked_reason,
    )


# ── F0 · status ─────────────────────────────────────────────────────────────

@router.get("/{promotion_ref}")
async def get_promotion_status(
    promotion_ref: str,
    store: PromotionStore = Depends(_get_store),
):
    """Get PromotionRequest status."""
    req = store.get_request(promotion_ref)
    if req is None:
        raise HTTPException(
            status_code=404,
            detail=f"PromotionRequest not found: {promotion_ref}",
        )
    return {
        "promotion_ref": req.promotion_ref,
        "source_ref": req.source_ref,
        "state": req.state,
        "gate_ref": req.gate_ref,
        "target_record_ref": req.target_record_ref,
        "provenance_branch_ref": req.provenance_branch_ref,
        "provenance_refs": list(req.provenance_refs),
        "blocked_reason": req.blocked_reason,
        "retracted_at": req.retracted_at,
        "retraction_reason": req.retraction_reason,
        "retraction_receipt_ref": req.retraction_receipt_ref,
        "promoted_at": req.promoted_at,
        "requested_at": req.requested_at,
        "requester_ref": req.requester_ref,
        "notes": req.notes,
    }


# ── F4 · retract ───────────────────────────────────────────────────────────

class RetractionBody(BaseModel):
    retraction_reason: str = ""
    retracted_by: str = ""
    retractor_role: str = ""


@router.post("/{promotion_ref}/retract", response_model=RetractionResponse)
async def retract_promotion(
    promotion_ref: str,
    body: RetractionBody = RetractionBody(),
    store: PromotionStore = Depends(_get_store),
):
    """F4: Retracta una promotion. Durable, auditable, no borra linaje."""
    try:
        req, retraction = store.retract(
            promotion_ref=promotion_ref,
            retraction_reason=body.retraction_reason,
            retracted_by=body.retracted_by,
            retractor_role=body.retractor_role,
        )
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if req is None:
        raise HTTPException(
            status_code=404,
            detail=f"PromotionRequest not found: {promotion_ref}",
        )

    return RetractionResponse(
        promotion_ref=promotion_ref,
        state=req.state,
        retraction_receipt_ref=retraction.retraction_receipt_ref,
        retraction_reason=retraction.retraction_reason,
        retracted_at=retraction.timestamp,
        lineage_preserved=retraction.lineage_preserved,
    )


# ── Curated records ────────────────────────────────────────────────────────

@router.get("/curated/list")
async def list_curated(
    store: PromotionStore = Depends(_get_store),
):
    """List active global-curated records."""
    records = store.list_curated_active()
    return {
        "curated_count": len(records),
        "records": [r.model_dump() for r in records],
    }


@router.get("/curated/{curated_ref}")
async def get_curated(
    curated_ref: str,
    store: PromotionStore = Depends(_get_store),
):
    """Get specific curated record."""
    curated = store.get_curated(curated_ref)
    if curated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Curated record not found: {curated_ref}",
        )
    return curated.model_dump()


# ── Listing ───────────────────────────────────────────────────────────────

@router.get("/list")
async def list_promotions(
    state: Optional[str] = Query(default=None),
    source_ref: Optional[str] = Query(default=None),
    store: PromotionStore = Depends(_get_store),
):
    """List promotion requests, optionally filtered."""
    result = store.list_promotions(state=state, source_ref=source_ref)
    return {
        "count": len(result),
        "promotions": [r.model_dump() for r in result],
    }


# ── Metrics ────────────────────────────────────────────────────────────────

@router.get("/metrics")
async def promotion_metrics(
    store: PromotionStore = Depends(_get_store),
):
    """Get promotion store metrics."""
    return store.metrics()
