"""Research Pipeline Router — full orchestrated research workflow.

POST /api/v1/research/pipeline       — launch async pipeline
GET  /api/v1/research/pipeline/{id}  — poll pipeline status
POST /api/v1/research/pipeline/sync  — synchronous variant (for testing)
"""
from __future__ import annotations

import logging
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency as _user_dependency
from mica.literature_consolidation.contracts.poll_envelope import normalize_poll_envelope
from mica.literature_consolidation.services.research_pipeline_service import (
    ResearchPipelineExecutionRequest,
    run_research_pipeline as _run_research_pipeline_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in {"prod", "production"}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class PipelineRequest(BaseModel):
    query: str = Field(..., description="Primary research query, e.g. 'OSR1 kinase SPAK signaling'")
    entities: List[str] = Field(default_factory=list, description="Entity symbols to expand queries")
    pdb_ids: List[str] = Field(default_factory=list, description="PDB IDs for structural context")
    dlm_preset: str = Field("standard", description="DLM scan depth preset")
    lmp_preset: str = Field("structural", description="LMP generation preset")
    generate_report: bool = Field(True, description="Produce DOCX report")
    session_id: Optional[str] = Field(None, description="Workspace session ID (auto-create if null)")
    user_id: str = Field("agent", description="User ID")


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

class _JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class _JobRecord:
    __slots__ = ("job_id", "user_id", "status", "result", "error", "created_at", "finished_at")

    def __init__(self, job_id: str, user_id: str):
        self.job_id = job_id
        self.user_id = user_id
        self.status = _JobStatus.QUEUED
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


_jobs: Dict[str, _JobRecord] = {}

# ---------------------------------------------------------------------------
# RedisJobStore singleton (lazy — graceful fallback if Redis unavailable)
# ---------------------------------------------------------------------------

_job_store_instance = None


def _get_job_store():
    """Return a shared RedisJobStore, or None if Redis is unreachable."""
    global _job_store_instance
    if _job_store_instance is not None:
        return _job_store_instance
    try:
        from mica.infrastructure.redis_client import get_redis
        from mica.worker.job_store import RedisJobStore

        redis_client = get_redis()
        _job_store_instance = RedisJobStore(redis_client)
        return _job_store_instance
    except Exception as exc:
        logger.warning("RedisJobStore unavailable: %s", exc)
        return None


def _production_queue_required(route_name: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"{route_name} requires Redis-backed worker execution in production; in-process fallback is disabled.",
    )


def _assert_job_owner(record: Dict[str, Any], user_id: str) -> None:
    if str(record.get("user_id") or "") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")


# ---------------------------------------------------------------------------
# Core pipeline execution
# ---------------------------------------------------------------------------

async def _run_pipeline(payload: PipelineRequest) -> Dict[str, Any]:
    """Execute the full research pipeline via service layer."""
    return await _run_research_pipeline_service(
        ResearchPipelineExecutionRequest(**payload.model_dump())
    )


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@router.post("/pipeline")
async def launch_pipeline(
    payload: PipelineRequest,
    user_id: str = Depends(_user_dependency),
):
    """Launch an async research pipeline.

    Returns immediately with a ``job_id``. Poll
    ``GET /api/v1/research/pipeline/{job_id}`` for the result.

    Attempts to enqueue via Redis for durable worker execution.
    Production traffic must never fall back to in-process execution.
    """
    job_id = uuid.uuid4().hex
    payload.user_id = user_id

    # ── Try durable Redis path ──────────────────────────
    store = _get_job_store()
    if store is not None:
        try:
            await store.enqueue(
                job_id=job_id,
                lane="research",
                payload={**payload.model_dump(), "task_type": "research_pipeline"},
                user_id=user_id,
            )
            logger.info("research_pipeline job %s enqueued to Redis", job_id[:8])
            return {"ok": True, "job_id": job_id, "status": "queued", "backend": "redis"}
        except Exception as exc:
            logger.warning("Redis enqueue failed for research_pipeline: %s", exc)
            if _PROD_ENV:
                raise _production_queue_required("research pipeline")

    if _PROD_ENV:
        raise _production_queue_required("research pipeline")

    job = _JobRecord(job_id, user_id)
    _jobs[job_id] = job

    async def _bg():
        job.status = _JobStatus.RUNNING
        try:
            result = await _run_pipeline(payload)
            job.result = result
            job.status = _JobStatus.DONE
        except Exception as exc:
            logger.exception("Research pipeline job %s failed", job_id)
            job.error = str(exc)
            job.status = _JobStatus.ERROR
        finally:
            job.finished_at = time.time()

    import asyncio
    asyncio.create_task(_bg())
    return {"ok": True, "job_id": job_id, "status": job.status, "backend": "memory"}


@router.get("/pipeline/{job_id}")
async def get_pipeline_job(job_id: str, _user: str = Depends(_user_dependency)):
    """Poll the result of a research pipeline job (checks Redis first, then in-memory)."""
    # Try Redis store
    store = _get_job_store()
    if store is not None:
        try:
            record = await store.get(job_id)
            if record is not None:
                _assert_job_owner(record, _user)
                return normalize_poll_envelope(record)
        except HTTPException:
            raise
        except Exception:
            if _PROD_ENV:
                raise _production_queue_required("research pipeline polling")
            pass  # fall through to in-memory

    if _PROD_ENV:
        raise _production_queue_required("research pipeline polling")

    # Fallback to in-memory dict
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != _user:
        raise HTTPException(status_code=403, detail="Access denied")
    return normalize_poll_envelope(job.to_dict())


@router.get("/pipeline/{job_id}/status")
async def get_pipeline_job_status(job_id: str, _user: str = Depends(_user_dependency)):
    """Lightweight status poll — no payload echo. Matches /api/v1/reports/lrr/{id}/status shape."""
    record = await get_pipeline_job(job_id, _user=_user)
    return {
        "job_id": job_id,
        "status": record.get("status", "unknown"),
        "updated_at": record.get("updated_at") or record.get("finished_at"),
        "error": record.get("error"),
    }


@router.post("/pipeline/sync")
async def run_pipeline_sync(payload: PipelineRequest, user_id: str = Depends(_user_dependency)):
    """Synchronous variant — blocks until pipeline completes. For scripts/tests."""
    payload.user_id = user_id
    result = await _run_pipeline(payload)
    return result
