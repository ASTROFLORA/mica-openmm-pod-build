"""S-R3 · Storage Reconcile Router — state machine + reconciler.

Exposes:
  GET  /api/v1/storage/promotions/{ref}/state   → current state from history
  POST /api/v1/storage/reconcile                → run reconcile batch
  GET  /api/v1/storage/reconcile/stuck          → list stuck promotions
  POST /api/v1/storage/reconcile/replay         → replay post-restore
  GET  /api/v1/storage/reconcile/metrics        → reconciler metrics
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mica.storage.promotion_state_machine import (
    PromotionHistory,
    PromotionTransition,
)
from mica.storage.promotion_reconciler import (
    ReconcileResult,
    PromotionReconciler,
    get_promotion_reconciler,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/storage",
    tags=["storage-reconcile"],
)


def _get_reconciler() -> PromotionReconciler:
    return get_promotion_reconciler()


# ── F1+F2 · state machine state ────────────────────────────────────────────

class StateResponse(BaseModel):
    promotion_ref: str
    current_state: str
    history: List[Dict[str, Any]] = []
    applied_event_count: int = 0


@router.get("/promotions/{promotion_ref}/state", response_model=StateResponse)
async def get_promotion_state(
    promotion_ref: str,
    reconciler: PromotionReconciler = Depends(_get_reconciler),
):
    """Retorna el estado actual de la promoción (derivado de la state machine)."""
    current = reconciler.get_state(promotion_ref)
    hist = reconciler.get_history(promotion_ref)
    transitions = [tr.model_dump() for tr in (hist.transitions if hist else ())]
    return StateResponse(
        promotion_ref=promotion_ref,
        current_state=current,
        history=transitions,
        applied_event_count=len(transitions),
    )


# ── F1+F2+F4 · reconcile batch ─────────────────────────────────────────────

class ReconcileRequest(BaseModel):
    since_event_ref: str = ""
    promotion_refs: List[str] = []


@router.post("/reconcile", response_model=Dict[str, Any])
async def run_reconcile(
    body: ReconcileRequest = ReconcileRequest(),
    reconciler: PromotionReconciler = Depends(_get_reconciler),
):
    """Pasa de reconciliación idempotente.

    S-R3-F1: outbox-driven, sin 2PC.
    S-R3-F2: dedupe por event_ref.
    S-R3-F4: stuck detection -> dead-letter + senal.
    """
    result = reconciler.reconcile(
        since_event_ref=body.since_event_ref,
        promotion_refs=body.promotion_refs or None,
    )
    return {
        "applied": result.applied,
        "noop_duplicates": result.noop_duplicates,
        "illegal_transitions": result.illegal_transitions,
        "gate_guard_failures": result.gate_guard_failures,
        "stuck_detected": result.stuck_detected,
        "promoted": result.promoted,
        "retracted": result.retracted,
        "blocked": result.blocked,
        "transitions": [tr.model_dump() for tr in result.transitions],
        "errors": list(result.errors),
        "duration_ms": result.duration_ms,
        "recovery_epoch_active": result.recovery_epoch_active,
    }


# ── F4 · stuck detection ──────────────────────────────────────────────────

@router.get("/reconcile/stuck")
async def list_stuck(
    reconciler: PromotionReconciler = Depends(_get_reconciler),
):
    """Lista las promociones atascadas (sin transición por más de stuck_threshold)."""
    stuck = reconciler.get_stuck_promotions()
    return {
        "stuck_count": len(stuck),
        "stuck_promotions": stuck,
    }


# ── F3 · replay post-restore ──────────────────────────────────────────────

class ReplayRequest(BaseModel):
    consumer_lane: str = "storage-reconciler"


@router.post("/reconcile/replay", response_model=Dict[str, Any])
async def run_replay(
    body: ReplayRequest = ReplayRequest(),
    reconciler: PromotionReconciler = Depends(_get_reconciler),
):
    """S-R3-F3: replay determinista post-restore.

    Idempotente: los eventos ya aplicados son noop.
    """
    result = reconciler.replay(consumer_lane=body.consumer_lane)
    return {
        "applied": result.applied,
        "noop_duplicates": result.noop_duplicates,
        "recovery_epoch_active": result.recovery_epoch_active,
        "transitions": [tr.model_dump() for tr in result.transitions],
        "duration_ms": result.duration_ms,
    }


# ── Metrics ───────────────────────────────────────────────────────────────

@router.get("/reconcile/metrics")
async def reconcile_metrics(
    reconciler: PromotionReconciler = Depends(_get_reconciler),
):
    """Métricas del reconciler."""
    return reconciler.metrics()
