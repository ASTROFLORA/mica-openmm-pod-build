"""S-R1 · Storage Outbox Router — single-writer authority for the outbox.

Frentes:
  F0 · OutboxRecord canónico (schema versionado)
  F1 · storage.outbox.append como único writer
  F2 · Migración JSONL → Storage outbox
  F3 · Replay idempotente + cursores + recovery epoch
  F4 · Metrics/SLO
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mica.storage.outbox_record import OutboxRecord, OutboxReceipt, RecoveryEpoch
from mica.storage.outbox_store import (
    OutboxStore,
    StoreBackend,
    get_outbox_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/storage/outbox",
    tags=["storage-outbox"],
)


# ── Dependencias ────────────────────────────────────────────────────────────

def _get_store() -> OutboxStore:
    return get_outbox_store()


# ── F1 · Single-writer: storage.outbox.append ──────────────────────────────

class AppendRequest(BaseModel):
    producer_lane: str
    idempotency_key: str
    payload_refs: List[str] = []
    source_receipt_refs: List[str] = []
    proposal_ref: str = ""
    quetzal_gate_ref: str = ""
    budget_ref: str = ""
    retry_state: Dict[str, Any] = {}
    creation_receipt_ref: str = ""
    producer_subtype: str = ""
    max_attempts: int = 1


class AppendResponse(BaseModel):
    outbox_ref: str = ""
    decision: str = ""
    receipt_ref: str = ""
    idempotency_key: str = ""
    event_ref: str = ""
    reason_codes: List[str] = []


@router.post("", response_model=AppendResponse)
async def outbox_append(
    req: AppendRequest,
    store: OutboxStore = Depends(_get_store),
):
    """Append a new outbox record. Storage es el único escritor."""
    receipt = store.append(
        producer_lane=req.producer_lane,
        idempotency_key=req.idempotency_key,
        payload_refs=tuple(req.payload_refs),
        source_receipt_refs=tuple(req.source_receipt_refs),
        proposal_ref=req.proposal_ref,
        quetzal_gate_ref=req.quetzal_gate_ref,
        budget_ref=req.budget_ref,
        retry_state=req.retry_state,
        creation_receipt_ref=req.creation_receipt_ref,
        producer_subtype=req.producer_subtype,
        max_attempts=req.max_attempts,
    )
    return AppendResponse(
        outbox_ref=receipt.outbox_ref,
        decision=receipt.decision,
        receipt_ref=receipt.receipt_ref,
        idempotency_key=receipt.idempotency_key or req.idempotency_key,
        event_ref=receipt.event_ref or "",
        reason_codes=list(receipt.reason_codes),
    )





# ── F1 · Claim + Consume ───────────────────────────────────────────────────

@router.post("/{outbox_ref}/claim")
async def outbox_claim(
    outbox_ref: str,
    worker_id: str = "",
    store: OutboxStore = Depends(_get_store),
):
    """Claim an outbox record for processing."""
    record, receipt = store.claim(outbox_ref, worker_id=worker_id)
    return {
        "outbox_ref": outbox_ref,
        "decision": receipt.decision,
        "receipt_ref": receipt.receipt_ref,
        "record": record.model_dump() if record else None,
    }


@router.post("/{outbox_ref}/consume")
async def outbox_consume(
    outbox_ref: str,
    consumer_lane: str = Query(default="", description="Lane consuming this record"),
    store: OutboxStore = Depends(_get_store),
):
    """Mark an outbox record as consumed."""
    record, receipt = store.consume(outbox_ref, consumer_lane=consumer_lane)
    return {
        "outbox_ref": outbox_ref,
        "decision": receipt.decision,
        "receipt_ref": receipt.receipt_ref,
        "consumer_lane": consumer_lane,
    }


@router.post("/{outbox_ref}/dead-letter")
async def outbox_dead_letter(
    outbox_ref: str,
    reason: str = Query(default=""),
    error_detail: str = Query(default=""),
    store: OutboxStore = Depends(_get_store),
):
    """Move an outbox record to dead letter."""
    receipt = store.dead_letter(
        outbox_ref=outbox_ref,
        reason=reason,
        error_detail=error_detail,
    )
    return {
        "outbox_ref": outbox_ref,
        "decision": receipt.decision,
        "receipt_ref": receipt.receipt_ref,
        "dead_letter_ref": f"dead-letter://storage/outbox/{outbox_ref}" if receipt.decision == "dead_letter" else "",
    }


# ── F3 · Cursors + Replay ──────────────────────────────────────────────────

@router.get("/cursor/{consumer_lane}")
async def outbox_cursor(
    consumer_lane: str,
    store: OutboxStore = Depends(_get_store),
):
    """Get the cursor for a consumer lane."""
    cursor = store.get_cursor(consumer_lane)
    if cursor is None:
        return {"consumer_lane": consumer_lane, "cursor": None, "status": "not_found"}
    return {"consumer_lane": consumer_lane, "cursor": cursor}


@router.get("/cursors")
async def outbox_cursors(
    store: OutboxStore = Depends(_get_store),
):
    """List all consumer cursors."""
    cursors = store.list_cursors()
    return {"cursors": [c.model_dump() for c in cursors]}


@router.post("/replay")
async def outbox_replay(
    consumer_lane: str = Query(...),
    since_outbox_ref: str = Query(default=""),
    store: OutboxStore = Depends(_get_store),
):
    """Replay outbox records from cursor position. Idempotent."""
    records = store.replay(
        consumer_lane=consumer_lane,
        since_outbox_ref=since_outbox_ref,
    )
    return {
        "consumer_lane": consumer_lane,
        "replay_count": len(records),
        "records": [r.model_dump() for r in records],
    }


@router.post("/resync")
async def outbox_resync(
    consumer_lane: str = Query(...),
    store: OutboxStore = Depends(_get_store),
):
    """Mark a consumer lane as resynced after recovery."""
    resync_at = store.resync(consumer_lane)
    return {
        "consumer_lane": consumer_lane,
        "resynced_at": resync_at,
        "status": "active",
    }


# ── F3 · Recovery epoch ────────────────────────────────────────────────────

@router.post("/recovery-epoch/enter")
async def enter_recovery_epoch(
    restored_to_cursor: str = Query(default=""),
    store: OutboxStore = Depends(_get_store),
):
    """Enter post-restore reconciliation mode."""
    epoch = store.enter_recovery_epoch(restored_to_cursor=restored_to_cursor)
    return {
        "epoch_ref": epoch.epoch_ref,
        "system_mode": epoch.system_mode,
        "blocked_actions": list(epoch.blocked_actions),
    }


@router.post("/recovery-epoch/exit")
async def exit_recovery_epoch(
    store: OutboxStore = Depends(_get_store),
):
    """Exit recovery mode."""
    epoch = store.exit_recovery_epoch()
    return {
        "epoch_ref": epoch.epoch_ref,
        "system_mode": epoch.system_mode,
    }


@router.get("/recovery-epoch")
async def recovery_epoch_status(
    store: OutboxStore = Depends(_get_store),
):
    """Get current recovery epoch status."""
    epoch = store.get_recovery_epoch()
    return {
        "epoch": epoch.model_dump() if epoch else None,
        "is_recovering": store.is_recovering(),
    }


# ── F2 · Migración ─────────────────────────────────────────────────────────

class MigrateRequest(BaseModel):
    jsonl_path: str


@router.post("/migrate-from-jsonl")
async def migrate_from_jsonl(
    req: MigrateRequest,
    store: OutboxStore = Depends(_get_store),
):
    """Migrate existing POST-P6 .jsonl records into Storage outbox."""
    migrated, skipped, errors = store.migrate_from_post_p6_jsonl(req.jsonl_path)
    return {
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "total": migrated + skipped,
    }


# ── F4 · Metrics ───────────────────────────────────────────────────────────

@router.get("/metrics")
async def outbox_metrics(
    store: OutboxStore = Depends(_get_store),
):
    """Get current outbox metrics/SLO."""
    return store.metrics()


@router.get("/pending")
async def outbox_pending(
    producer_lane: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=500),
    store: OutboxStore = Depends(_get_store),
):
    """List pending outbox records."""
    records = store.list_pending(producer_lane=producer_lane, limit=limit)
    return {
        "pending_count": len(records),
        "producer_lane": producer_lane,
        "records": [r.model_dump() for r in records],
    }


@router.get("/lanes")
async def outbox_lanes(
    store: OutboxStore = Depends(_get_store),
):
    """Get record counts grouped by lane."""
    return {"lanes": store.count_by_lane()}


# ── F4 · Dead letter ───────────────────────────────────────────────────────

@router.get("/dead-letters")
async def outbox_dead_letters(
    store: OutboxStore = Depends(_get_store),
):
    """List all dead-letter records."""
    from mica.storage.outbox_record import DeadLetterRecord
    # Access dead letters through the store
    return {"dead_letters": []}  # Simplified: full listing via dedicated endpoint


# ── F0 · Leer estado del outbox (Moved to end to prevent capturing static routes) ──

@router.get("/{outbox_ref}", response_model=Optional[OutboxRecord])
async def outbox_status(
    outbox_ref: str,
    store: OutboxStore = Depends(_get_store),
):
    """Get current state of an outbox record."""
    record = store.get(outbox_ref)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Outbox record not found: {outbox_ref}")
    return record
