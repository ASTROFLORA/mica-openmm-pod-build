"""reports.py — Literature Research Report (LRR) orchestration surface.

v4.3 blueprint for L09B gaps G3 (report enqueue) and G4 (report fetch).

Contract:
  POST /api/v1/reports/lrr         → enqueue LRR job (title, query, scope)
  GET  /api/v1/reports/lrr         → list current user's reports
  GET  /api/v1/reports/lrr/{id}    → fetch a single report manifest
  GET  /api/v1/reports/lrr/{id}/status → lightweight progress poll

Backed by Redis queue (lane="research") when available, with in-memory
fallback for dev. Reuses the same JobStore machinery as literature.py and
dlm.py for observability parity.

The actual report rendering worker lives in `mica.worker.lrr_worker`
(to be wired in a follow-up); this router is the HTTP contract only.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.infrastructure.redis_client import get_redis_if_configured
from mica.worker.job_store import RedisJobStore

logger = logging.getLogger(__name__)

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in (
    "prod",
    "production",
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

_job_store_instance: RedisJobStore | None = None


# ── In-memory fallback (dev only) ───────────────────────────────────────────

class _Job:
    __slots__ = ("job_id", "user_id", "kind", "status", "payload", "result",
                 "error", "created_at", "updated_at")

    def __init__(self, job_id: str, user_id: str, kind: str, payload: Dict[str, Any]):
        self.job_id = job_id
        self.user_id = user_id
        self.kind = kind
        self.status = "queued"
        self.payload = payload
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.updated_at = self.created_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "kind": self.kind,
            "status": self.status,
            "payload": self.payload,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_mem_jobs: Dict[str, _Job] = {}


async def _get_job_store() -> RedisJobStore | None:
    global _job_store_instance
    if _job_store_instance is not None:
        return _job_store_instance
    redis_client = await get_redis_if_configured(
        decode_responses=False, verify_connection=True
    )
    if redis_client is None:
        return None
    _job_store_instance = RedisJobStore(redis_client)
    return _job_store_instance


def _prod_queue_required() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="reports require Redis-backed worker execution in production.",
    )


def _assert_report_owner(record: Dict[str, Any], user_id: str) -> None:
    """Raise 403 when the stored record does not belong to the requesting user.

    Strict semantics aligned with research_pipeline and literature ownership gates:
    records with no stored user_id are also rejected (no legacy skip).
    Supports nested metadata dicts produced by some Redis job store writers.
    """
    stored = str(
        record.get("user_id")
        or record.get("metadata", {}).get("user_id")
        or ""
    )
    if stored != user_id:
        raise HTTPException(status_code=403, detail="Access denied")


# ── Request / Response schema ───────────────────────────────────────────────

class LRRRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=240)
    query: str = Field(..., min_length=3, max_length=2048,
                       description="Free-text scientific question or entity list")

    max_papers: int = Field(50, ge=5, le=500)
    year_from: Optional[int] = Field(None, ge=1900, le=2100)
    year_to: Optional[int] = Field(None, ge=1900, le=2100)
    fields_of_study: Optional[str] = Field(None, description="CSV of S2 fields-of-study")

    include_atom: bool = Field(True, description="Build ATOM fact graph from abstracts")
    include_dlm: bool = Field(True, description="Run DLM deep literature mining")
    include_kg: bool = Field(False, description="Persist extracted KG edges")

    session_id: Optional[str] = Field(None)
    tenant_id: Optional[str] = Field(None)
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0)


class LRRResponse(BaseModel):
    ok: bool
    report_id: str
    status: str
    backend: str
    created_at: float


# ── POST /api/v1/reports/lrr ────────────────────────────────────────────────

@router.post("/lrr", response_model=LRRResponse)
async def enqueue_lrr(
    payload: LRRRequest,
    user_id: str = Depends(user_dependency),
) -> LRRResponse:
    """Enqueue a Literature Research Report job.

    Returns immediately with a report_id. Poll /api/v1/reports/lrr/{id}
    for the manifest once status == 'done'.
    """
    report_id = f"lrr-{uuid.uuid4().hex[:12]}"

    job_payload: Dict[str, Any] = {
        "task_type": "literature_research_report",
        "request": payload.model_dump(),
        "user_id": user_id,
        "report_id": report_id,
    }

    store = await _get_job_store()
    if store is not None:
        await store.enqueue(
            job_id=report_id,
            lane="research",
            payload=job_payload,
            user_id=user_id,
        )
        return LRRResponse(
            ok=True,
            report_id=report_id,
            status="queued",
            backend="redis",
            created_at=time.time(),
        )

    if _PROD_ENV:
        raise _prod_queue_required()

    job = _Job(report_id, user_id, "literature_research_report", job_payload)
    _mem_jobs[report_id] = job
    return LRRResponse(
        ok=True,
        report_id=report_id,
        status="queued",
        backend="memory",
        created_at=job.created_at,
    )


# ── GET /api/v1/reports/lrr/{id} ────────────────────────────────────────────

@router.get("/lrr/{report_id}")
async def get_lrr(
    report_id: str,
    _user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Fetch a single LRR manifest (full result when done)."""
    store = await _get_job_store()
    if store is not None:
        record = await store.get(report_id)
        if record is not None:
            _assert_report_owner(record, _user_id)
            return record

    elif _PROD_ENV:
        raise _prod_queue_required()

    job = _mem_jobs.get(report_id)
    if job is None:
        raise HTTPException(status_code=404, detail="report not found")
    if job.user_id != _user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return job.to_dict()


@router.get("/lrr/{report_id}/status")
async def get_lrr_status(
    report_id: str,
    _user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Lightweight status poll (no payload echo)."""
    record = await get_lrr(report_id, _user_id=_user_id)  # type: ignore[arg-type]
    return {
        "report_id": report_id,
        "status": record.get("status", "unknown"),
        "updated_at": record.get("updated_at"),
        "error": record.get("error"),
    }


# ── GET /api/v1/reports/lrr (list) ──────────────────────────────────────────

@router.get("/lrr")
async def list_lrr(
    user_id: str = Depends(user_dependency),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """List current user's LRR jobs. Returns most recent first."""
    store = await _get_job_store()
    if store is not None:
        try:
            records = await store.list_by_user(user_id=user_id, limit=limit)
        except AttributeError:
            records = []
        return {"ok": True, "backend": "redis", "reports": records}

    records_sorted = sorted(
        (j for j in _mem_jobs.values() if j.user_id == user_id),
        key=lambda j: j.created_at,
        reverse=True,
    )[:limit]
    return {
        "ok": True,
        "backend": "memory",
        "reports": [r.to_dict() for r in records_sorted],
    }


# ── GET /api/v1/reports/health ──────────────────────────────────────────────

@router.get("/health")
async def reports_health() -> Dict[str, Any]:
    """Lightweight health probe — confirms router is mounted and queue backend.

    Hardened: must never raise. If Redis probing fails we degrade to ``memory``
    backend and surface the error shape so the frontend can render a banner
    without the whole router looking dead.
    """
    backend = "memory"
    backend_error: str | None = None
    try:
        store = await _get_job_store()
        if store is not None:
            backend = "redis"
    except Exception as exc:  # defensive: Redis unreachable / auth fail
        backend_error = f"{type(exc).__name__}: {exc}"
        # P1-SEC fix (2026-04-20): log full error server-side but do not
        # expose type/message to unauth callers (reveals internal API shape,
        # e.g. "TypeError: get_redis_if_configured() got an unexpected
        # keyword argument 'verify_connection'" leaks function signatures).
        logger.warning("reports.health: queue backend probe failed: %s", backend_error)
    payload: Dict[str, Any] = {
        "ok": True,
        "router": "reports",
        "backend": backend,
        "prod_env": _PROD_ENV,
    }
    # Only expose error shape in non-prod / dev contexts
    if backend_error and not _PROD_ENV:
        payload["backend_error"] = backend_error
    return payload
