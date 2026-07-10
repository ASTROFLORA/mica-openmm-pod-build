"""Job Truth router — P0 product-layer API.

Wraps existing TimescaleJobStore + events hypertable infrastructure.
Does NOT create a new job store.

Endpoints:
  GET /api/v1/jobs/{id}/timeline   — Job timeline with events
  GET /api/v1/jobs/{id}/ui-state   — Lightweight pollable state
  GET /api/v1/jobs/{id}/receipt    — Job receipt (from job_receipts table)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import choose_neon_database_url, choose_timescale_database_url, asyncpg_connection_kwargs_for_database_url, mask_dsn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["job-truth"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class JobTimelineEvent(BaseModel):
    event_type: str
    timestamp: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    node_id: Optional[str] = None
    node_status: Optional[str] = None

class JobTimelineResponse(BaseModel):
    job_id: str
    user_id: str
    job_type: Optional[str] = "unknown"
    provider: Optional[str] = None
    status: str
    events: List[JobTimelineEvent] = Field(default_factory=list)
    cost_estimate_usd: Optional[float] = None
    cost_actual_usd: Optional[float] = None
    input_artifact_ids: List[str] = Field(default_factory=list)
    output_artifact_ids: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    protocol_run_id: Optional[str] = None
    protocol_id: Optional[str] = None
    study_id: Optional[str] = None

class JobUIStateResponse(BaseModel):
    job_id: str
    status: str
    progress_percent: Optional[int] = None
    current_step: Optional[str] = None
    log_tail: List[str] = Field(default_factory=list)
    cost_so_far_usd: Optional[float] = None
    estimated_completion: Optional[str] = None
    ws_channel: str = ""
    protocol_run_id: Optional[str] = None

class JobReceiptOutput(BaseModel):
    output_type: str
    artifact_id: Optional[str] = None
    gcs_key: str
    content_hash: str
    mime_type: str
    size_bytes: int

class JobReceiptResponse(BaseModel):
    receipt_id: str
    job_id: str
    user_id: str
    outputs: List[JobReceiptOutput] = Field(default_factory=list)
    cost_estimate_usd: Optional[float] = None
    cost_actual_usd: Optional[float] = None
    duration_seconds: Optional[int] = None
    provider: Optional[str] = None
    status: str = "completed"
    provenance_refs: List[str] = Field(default_factory=list)
    created_at: str = ""
    protocol_run_id: Optional[str] = None
    node_id: Optional[str] = None
    node_receipts: List[Any] = Field(default_factory=list, description="Node-level receipts or protocol output refs")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_NEON_POOL = None
_TS_POOL = None

async def _get_neon_pool():
    global _NEON_POOL
    if _NEON_POOL is not None:
        return _NEON_POOL
    import asyncpg
    dsn = choose_neon_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="Database not configured")
    _NEON_POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=3,
        **asyncpg_connection_kwargs_for_database_url(dsn))
    return _NEON_POOL

async def _get_ts_pool():
    global _TS_POOL
    if _TS_POOL is not None:
        return _TS_POOL
    import asyncpg
    dsn = choose_timescale_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="TimescaleDB not configured")
    _TS_POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=3,
        **asyncpg_connection_kwargs_for_database_url(dsn))
    return _TS_POOL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_jsonb(raw, default=None):
    """Parse JSONB from asyncpg (string or already-deserialized)."""
    import json as _json
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except (_json.JSONDecodeError, TypeError):
            return default
    return default

def _safe_isoformat(val):
    """Convert datetime or string to ISO format string."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    return str(val)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{job_id}/timeline", response_model=JobTimelineResponse)
async def get_job_timeline(
    job_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get job timeline from TimescaleJobStore + events hypertable."""
    await ensure_product_schema()
    try:
        ts = await _get_ts_pool()
        async with ts.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT job_id, user_id, status, docker_image, created_at, started_at, completed_at, result, "
                "protocol_run_id, protocol_id, study_id "
                "FROM jobs WHERE job_id = $1 AND user_id = $2",
                job_id, user_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Job not found")

            # Aggregate events from events hypertable
            event_rows = await conn.fetch(
                "SELECT event_type, timestamp, data, node_id, node_status, protocol_run_id FROM events WHERE job_id = $1 ORDER BY timestamp",
                job_id,
            )

        events = [
            JobTimelineEvent(
                event_type=er["event_type"],
                timestamp=_safe_isoformat(er.get("timestamp")),
                detail=_parse_jsonb(er.get("data"), {}),
                node_id=er.get("node_id"),
                node_status=er.get("node_status"),
            ) for er in event_rows
        ]

        result = _parse_jsonb(row.get("result"), {})
        return JobTimelineResponse(
            job_id=job_id,
            user_id=row["user_id"] or "",
            job_type=row.get("docker_image", "unknown"),
            status=row["status"] or "unknown",
            events=events,
            cost_actual_usd=result.get("cost_actual_usd"),
            cost_estimate_usd=result.get("cost_estimate_usd"),
            created_at=_safe_isoformat(row.get("created_at")),
            started_at=_safe_isoformat(row.get("started_at")),
            finished_at=_safe_isoformat(row.get("completed_at")),
            protocol_run_id=row.get("protocol_run_id"),
            protocol_id=row.get("protocol_id"),
            study_id=row.get("study_id"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Job timeline unavailable for job %s: %s", job_id, e)
        raise HTTPException(status_code=503, detail=f"Job store unavailable: {e}")


@router.get("/{job_id}/ui-state", response_model=JobUIStateResponse)
async def get_job_ui_state(
    job_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get lightweight pollable job state."""
    await ensure_product_schema()
    try:
        ts = await _get_ts_pool()
        async with ts.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT job_id, status, result FROM jobs WHERE job_id = $1 AND user_id = $2",
                job_id, user_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Job not found")

        result = _parse_jsonb(row.get("result"), {})
        return JobUIStateResponse(
            job_id=job_id,
            status=row["status"] or "unknown",
            progress_percent=result.get("progress_percent"),
            current_step=result.get("current_step"),
            cost_so_far_usd=result.get("cost_actual_usd") or result.get("cost_estimate_usd"),
            estimated_completion=result.get("estimated_completion"),
            ws_channel=f"/ws/events/job/{job_id}",
            protocol_run_id=result.get("protocol_run_id"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Job store unavailable: {e}")


@router.get("/{job_id}/receipt", response_model=JobReceiptResponse)
async def get_job_receipt(
    job_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get durable job receipt from job_receipts table."""
    await ensure_product_schema()
    try:
        neon = await _get_neon_pool()
        async with neon.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM job_receipts WHERE job_id = $1 AND user_id = $2",
                job_id, user_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Receipt not found — job may still be running")

        outputs_raw = _parse_jsonb(row.get("outputs"), [])

        # Handle both structured output objects and simple string refs (protocol refs)
        outputs = []
        if isinstance(outputs_raw, list):
            for o in outputs_raw:
                if isinstance(o, dict):
                    outputs.append(JobReceiptOutput(
                        output_type=o.get("output_type", "unknown"),
                        artifact_id=o.get("artifact_id"),
                        gcs_key=o.get("gcs_key", ""),
                        content_hash=o.get("content_hash", ""),
                        mime_type=o.get("mime_type", "application/octet-stream"),
                        size_bytes=o.get("size_bytes", 0),
                    ))
                elif isinstance(o, str):
                    # Protocol executor outputs are simple string refs (URLs)
                    outputs.append(JobReceiptOutput(
                        output_type="protocol_ref",
                        gcs_key=o,
                        content_hash="",
                        mime_type="text/plain",
                        size_bytes=0,
                    ))

        return JobReceiptResponse(
            receipt_id=str(row["receipt_id"]),
            job_id=job_id,
            user_id=row["user_id"] or "",
            outputs=outputs,
            cost_estimate_usd=float(row["cost_estimate_usd"]) if row.get("cost_estimate_usd") else None,
            cost_actual_usd=float(row["cost_actual_usd"]) if row.get("cost_actual_usd") else None,
            duration_seconds=row.get("duration_seconds"),
            provider=row.get("provider"),
            status=row.get("status", "completed"),
            provenance_refs=list(row.get("provenance_refs") or []),
            created_at=_safe_isoformat(row.get("created_at")) or "",
            protocol_run_id=row.get("protocol_run_id"),
            node_id=row.get("node_id"),
            node_receipts=outputs_raw if isinstance(outputs_raw, list) else [],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Receipt store unavailable: {e}")
