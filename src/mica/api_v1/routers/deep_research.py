"""Deep Research Pipeline — citation-graph exploration via Semantic Scholar.

Provides ``POST /api/v1/research/deep-scan`` which:
1. Runs bulk S2 searches across multiple query variants.
2. Deduplicates papers by S2 paperId.
3. Chases citations/references up to *citation_depth* levels.
4. Optionally downloads open-access PDFs via gcs_pdf_bridge.
5. Returns a structured report with citation graph + gap analysis.

Heavy lifting runs on the Redis worker so the endpoint returns
immediately with a ``job_id`` and never executes deep work in the web tier
for production traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.literature_consolidation.contracts.poll_envelope import normalize_poll_envelope
from mica.literature_consolidation.services.deep_research_service import (
    DeepResearchExecutionRequest,
    run_deep_research as _run_deep_research_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in {"prod", "production"}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class DeepResearchRequest(BaseModel):
    query: str = Field(..., description="Primary search query, e.g. 'OSR1 kinase SPAK kinase'")
    entities: List[str] = Field(default_factory=list, description="Entity symbols to expand queries")
    max_papers: int = Field(500, ge=1, le=10000, description="Per-query paper limit")
    citation_depth: int = Field(1, ge=0, le=3, description="Levels of citation chasing")
    sources: List[str] = Field(default_factory=list, description="Optional literature sources: semantic_scholar, pubmed, openalex, biorxiv")
    download_pdfs: bool = Field(False, description="Download open-access PDFs to workspace")
    enable_atom_ingestion: Optional[bool] = Field(
        None,
        description=(
            "Whether to run the ATOM ingestion tail after deep search. "
            "If omitted, async worker-oriented routes keep the stock behavior, "
            "while the sync helper route disables it for local validation."
        ),
    )
    session_id: Optional[str] = Field(None, description="Workspace session for storage")
    user_id: Optional[str] = Field(None, description="User scope (overridden by auth)")
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0, description="Optional literature acquisition budget ceiling in USD")


class DeepResearchResult(BaseModel):
    query: str
    query_spec_hash: str = Field(default="")
    protocol_version: str = Field(default="")
    total_papers: int
    papers: List[Dict[str, Any]]
    citation_graph: Dict[str, List[str]]
    gaps: Dict[str, Any]
    search_log: List[str]
    acquisition_envelope: Dict[str, Any] = Field(default_factory=dict)
    artifact_bundle: Dict[str, Any] = Field(default_factory=dict)
    artifact_manifest: Dict[str, Any] = Field(default_factory=dict)
    artifact_list: List[Dict[str, Any]] = Field(default_factory=list)
    runtime_profile: Dict[str, Any] = Field(default_factory=dict)


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


async def _get_job_store():
    """Return a shared RedisJobStore, or None if Redis is unreachable."""
    global _job_store_instance
    if _job_store_instance is not None:
        return _job_store_instance
    try:
        from mica.infrastructure.redis_client import get_redis
        from mica.worker.job_store import RedisJobStore

        redis_client = await get_redis()
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


def _persist_result_to_gcs(result: Dict[str, Any], user_id: str, job_id: str) -> Dict[str, str]:
    """Upload deep-research result JSON to the user's GCS bucket."""
    try:
        from mica.storage.gcs_user_storage import get_storage_manager

        storage = get_storage_manager()
        object_path = f"jobs/deep_research/{job_id}/result.json"
        data = json.dumps(result, default=str).encode("utf-8")
        gcs_uri = storage.upload_bytes(
            user_id=user_id,
            object_path=object_path,
            data=data,
            content_type="application/json",
        )
        signed_url = storage.signed_url(user_id, object_path, "GET", 86400)
        return {"gcs_uri": gcs_uri, "signed_url": signed_url}
    except Exception as exc:
        logger.warning("Deep research GCS persist failed for job %s: %s", job_id, exc)
        return {}

# ---------------------------------------------------------------------------
# Core pipeline logic (sync-safe, runs in background task)
# ---------------------------------------------------------------------------


async def _run_deep_research(payload: DeepResearchRequest) -> DeepResearchResult:
    """Execute the full deep-research pipeline via service layer."""
    execution_payload = payload.model_dump(exclude_none=True)
    result = await _run_deep_research_service(DeepResearchExecutionRequest(**execution_payload))
    return DeepResearchResult(**result)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@router.post("/deep-scan")
async def deep_scan(
    payload: DeepResearchRequest,
    user_id: str = Depends(user_dependency),
):
    """Launch an async deep-research scan.

    Returns immediately with a ``job_id``.  Poll
    ``GET /api/v1/research/deep-scan/jobs/{job_id}`` for the result.

    Attempts to enqueue via Redis for durable worker execution.
    Production traffic must never fall back to in-process execution.
    """
    job_id = uuid.uuid4().hex
    payload.user_id = user_id

    # ── Try durable Redis path ──────────────────────────
    store = await _get_job_store()
    if store is not None:
        try:
            await store.enqueue(
                job_id=job_id,
                lane="research",
                payload={**payload.model_dump(), "task_type": "deep_research"},
                user_id=user_id,
            )
            logger.info("deep_research job %s enqueued to Redis", job_id[:8])
            return {"ok": True, "job_id": job_id, "status": "queued", "backend": "redis"}
        except Exception as exc:
            logger.warning("Redis enqueue failed for deep_research: %s", exc)
            if _PROD_ENV:
                raise _production_queue_required("deep research")

    if _PROD_ENV:
        raise _production_queue_required("deep research")

    job = _JobRecord(job_id, user_id)
    _jobs[job_id] = job

    async def _bg():
        job.status = _JobStatus.RUNNING
        try:
            result = await _run_deep_research(payload)
            result_dict = result.model_dump()
            gcs_meta = _persist_result_to_gcs(
                result_dict,
                user_id,
                job_id,
            )
            if gcs_meta:
                result_dict.update(gcs_meta)
            job.result = result_dict
            job.status = _JobStatus.DONE
        except Exception as exc:
            logger.exception("Deep research job %s failed", job_id)
            job.error = str(exc)
            job.status = _JobStatus.ERROR
        finally:
            job.finished_at = time.time()

    asyncio.create_task(_bg())
    return {"ok": True, "job_id": job_id, "status": job.status, "backend": "memory"}


@router.get("/deep-scan/jobs/{job_id}")
async def get_deep_scan_job(job_id: str, user_id: str = Depends(user_dependency)):
    """Poll the result of a deep-scan job (checks Redis first, then in-memory)."""
    # Try Redis store
    store = await _get_job_store()
    if store is not None:
        try:
            record = await store.get(job_id)
            if record is not None:
                _assert_job_owner(record, user_id)
                return normalize_poll_envelope(record)
        except HTTPException:
            raise
        except Exception:
            if _PROD_ENV:
                raise _production_queue_required("deep research polling")
            pass  # fall through to in-memory

    if _PROD_ENV:
        raise _production_queue_required("deep research polling")

    # Fallback to in-memory dict
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return normalize_poll_envelope(job.to_dict())


@router.post("/deep-scan/sync")
async def deep_scan_sync(payload: DeepResearchRequest, user_id: str = Depends(user_dependency)):
    """Synchronous variant (blocks until done). Useful for scripts/tests."""
    if _PROD_ENV:
        raise _production_queue_required("deep research sync")
    payload = payload.model_copy(
        update={
            "user_id": user_id,
            "enable_atom_ingestion": False if payload.enable_atom_ingestion is None else payload.enable_atom_ingestion,
        }
    )
    result = await _run_deep_research(payload)
    return result.model_dump()
