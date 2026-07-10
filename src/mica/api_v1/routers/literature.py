from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from mica.api_v1.auth import user_dependency
from mica.infrastructure.redis_client import get_redis_if_configured
from mica.literature_consolidation.contracts.query_facade import (
    LiteratureQueryFacadeRequest,
    LiteratureQueryFacadeResult,
)
from mica.literature_consolidation.contracts.poll_envelope import normalize_poll_envelope
from mica.literature_consolidation.services.literature_ingest_service import (
    LiteratureIngestExecutionRequest,
    run_literature_ingest as _run_literature_ingest_service,
)
from mica.model_runtime.backends import DEFAULT_GEMINI_FLASH_MODEL
from mica.storage.gcs_user_storage import sanitize_object_prefix
from mica.worker.job_store import RedisJobStore

logger = logging.getLogger(__name__)

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in (
    "prod",
    "production",
)

router = APIRouter(prefix="/api/v1/literature", tags=["literature"])

_job_store_instance: RedisJobStore | None = None


class _JobStatus:
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
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
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


async def _get_job_store() -> RedisJobStore | None:
    global _job_store_instance
    if _job_store_instance is not None:
        return _job_store_instance
    redis_client = await get_redis_if_configured(decode_responses=False, verify_connection=True)
    if redis_client is None:
        return None
    _job_store_instance = RedisJobStore(redis_client)
    return _job_store_instance


def _production_queue_required(route_name: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"{route_name} requires Redis-backed worker execution in production; in-process fallback is disabled.",
    )


def _assert_job_owner(record: Dict[str, Any], user_id: str) -> None:
    if str(record.get("user_id") or "") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")


async def _run_ingest(payload: LiteratureIngestRequest, user_id: str) -> Dict[str, Any]:
    try:
        return await _run_literature_ingest_service(
            LiteratureIngestExecutionRequest(**payload.model_dump()),
            user_id,
            prod_env=_PROD_ENV,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class LiteratureIngestRequest(BaseModel):
    query: str = Field(..., description="Literature search query (source-neutral)")
    max_papers: int = Field(50, ge=1, le=2000)

    download_pdfs: bool = Field(True, description="Attempt to download PDFs when available")
    extract_full_text: bool = Field(True, description="Extract full text (fulltext-first default; set False to degrade to abstract-only)")

    gcs_object_prefix: Optional[str] = Field(
        None,
        description="Optional object prefix inside the per-user bucket (e.g. 'literature/papers' or 'myapp/lit').",
    )

    session_id: Optional[str] = Field(None, description="Optional session scope for user memory")
    run_id: Optional[str] = Field(None, description="Optional run scope for evidence lineage")
    tenant_id: Optional[str] = Field(None, description="Optional tenant/account scope for acquisition budgeting")
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0, description="Optional max spend for paid acquisition fallbacks")
    allow_paid_fulltext: bool = Field(False, description="Allow paid OpenAlex PDF fallback after OA sources are exhausted")

    enable_atom: bool = Field(True, description="Create ATOM snapshots")
    atom_backend: str = Field("timescale", description="sqlite|timescale")
    atom_timescale_dsn: Optional[str] = Field(None, description="Override DSN (else env TIMESCALE_DSN/ATOM_TIMESCALE_DSN)")

    atom_enable_llm: bool = Field(False, description="Use LLM-backed ATOM extraction")
    atom_llm_provider: str = Field("vertex")
    atom_llm_model_facts: str = Field(DEFAULT_GEMINI_FLASH_MODEL)

    # Canonical source-neutral filter field; s2_filters is a compatibility alias.
    filters: Optional[Dict[str, Any]] = Field(None, description="Provider-agnostic search filters")
    s2_filters: Optional[Dict[str, Any]] = Field(None, description="Deprecated: use 'filters'. Semantic Scholar filter alias kept for backwards compatibility.")

    @model_validator(mode="after")
    def _merge_s2_filters_compat(self) -> "LiteratureIngestRequest":
        """If 'filters' is absent but the legacy 's2_filters' was supplied, promote it."""
        if self.filters is None and self.s2_filters is not None:
            self.filters = self.s2_filters
        return self


@router.post("/ingest")
async def ingest_literature(
    payload: LiteratureIngestRequest,
    user_id: str = Depends(user_dependency),
):
    """Ingest papers for the authenticated user.

    This is a first-class infrastructure entrypoint (no scripts):
    - PDFs go into the user GCS bucket
    - ATOM snapshots are created and persisted (sqlite or timescale)
    - Text chunks are persisted into TimescaleUserRAGStore per user
    """

    try:
        if payload.gcs_object_prefix is not None:
            sanitize_object_prefix(payload.gcs_object_prefix)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        job_id = f"lit-{uuid.uuid4().hex[:12]}"
        store = await _get_job_store()
        if store is not None:
            await store.enqueue(
                job_id=job_id,
                lane="research",
                payload={"task_type": "literature_ingest", "request": payload.model_dump(), "user_id": user_id},
                user_id=user_id,
            )
            return {"ok": True, "job_id": job_id, "status": "queued", "backend": "redis"}
        if _PROD_ENV:
            raise _production_queue_required("literature ingest")

        job = _JobRecord(job_id, user_id)
        _jobs[job_id] = job

        async def _run_local() -> None:
            job.status = _JobStatus.RUNNING
            try:
                job.result = await _run_ingest(payload, user_id)
                job.status = _JobStatus.DONE
            except Exception as exc:
                logger.exception("Literature ingest job %s failed", job_id)
                job.error = str(exc)
                job.status = _JobStatus.ERROR
            finally:
                job.finished_at = time.time()

        import asyncio
        asyncio.create_task(_run_local())
        return {"ok": True, "job_id": job_id, "status": "queued", "backend": "memory"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ingestion failed: {exc}")


@router.get("/ingest/jobs/{job_id}")
async def get_ingest_job(job_id: str, _user_id: str = Depends(user_dependency)):
    store = await _get_job_store()
    if store is not None:
        record = await store.get(job_id)
        if record is not None:
            _assert_job_owner(record, _user_id)
            return record
    elif _PROD_ENV:
        raise _production_queue_required("literature ingest polling")

    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != _user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return job.to_dict()


@router.post("/ingest/sync")
async def ingest_literature_sync(
    payload: LiteratureIngestRequest,
    user_id: str = Depends(user_dependency),
):
    try:
        return await _run_ingest(payload, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ingestion failed: {exc}")


# ── Lightweight Scholar-style search (v4.3) ─────────────────────────────────
# Mirrors the same primary source MICA uses internally (Semantic Scholar
# Graph API). Returns paper metadata only — no PDFs, no ingestion. Designed
# as the blueprint contract for the Alejandria/Presenta paper-viewer UI.

class LiteratureSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=512)
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0, le=9_900)
    fields: Optional[str] = Field(
        None,
        description=(
            "Comma-separated Semantic Scholar fields. Default: "
            "'title,abstract,year,authors,venue,openAccessPdf,externalIds,citationCount,url'"
        ),
    )
    year_from: Optional[int] = Field(None, ge=1900, le=2100)
    year_to: Optional[int] = Field(None, ge=1900, le=2100)
    fields_of_study: Optional[str] = Field(None, description="CSV of S2 fields-of-study")


_DEFAULT_S2_FIELDS = (
    "title,abstract,year,authors.name,authors.authorId,venue,"
    "openAccessPdf,externalIds,citationCount,url,publicationDate"
)


@router.post("/search")
async def literature_search(
    payload: LiteratureSearchRequest,
    _user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Scholar-style paper search.

    Contract used by:
      * Alejandria dual-pane viewer (paper list → right-pane PDF)
      * Presenta's Gen AI figure search (finds source papers for entities)
      * MICA search_literature primary lane (same upstream host)
    """
    import httpx

    fields = payload.fields or _DEFAULT_S2_FIELDS
    params: Dict[str, Any] = {
        "query": payload.query,
        "limit": payload.limit,
        "offset": payload.offset,
        "fields": fields,
    }
    if payload.year_from and payload.year_to:
        params["year"] = f"{payload.year_from}-{payload.year_to}"
    elif payload.year_from:
        params["year"] = f"{payload.year_from}-"
    elif payload.year_to:
        params["year"] = f"-{payload.year_to}"
    if payload.fields_of_study:
        params["fieldsOfStudy"] = payload.fields_of_study

    headers: Dict[str, str] = {"Accept": "application/json"}
    s2_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    if s2_key:
        headers["x-api-key"] = s2_key

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            r = await client.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params=params,
                headers=headers,
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Semantic Scholar timeout")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream error: {exc}")

    if r.status_code >= 500:
        raise HTTPException(status_code=502, detail=f"Semantic Scholar upstream {r.status_code}")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Semantic Scholar rate limit")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])

    data = r.json() or {}
    return {
        "ok": True,
        "query": payload.query,
        "total": int(data.get("total") or 0),
        "offset": int(data.get("offset") or payload.offset),
        "next": data.get("next"),
        "results": data.get("data") or [],
        "source": "semantic-scholar",
        "primary_host": "api.semanticscholar.org",
    }


@router.get("/search")
async def literature_search_get(
    query: str,
    limit: int = 20,
    offset: int = 0,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    _user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """GET shortcut — identical contract to POST /search."""
    return await literature_search(
        LiteratureSearchRequest(
            query=query,
            limit=limit,
            offset=offset,
            year_from=year_from,
            year_to=year_to,
        ),
        _user_id=_user_id,
    )


# ── Unified Query Facade — Iteration 08 ────────────────────────────────────


@router.post("/query", response_model=None)
async def unified_literature_query(
    payload: LiteratureQueryFacadeRequest,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Unified literature query entrypoint.

    Dispatches to one of three execution lanes based on the ``lane`` field:
      - ``"ingest"``        — per-user PDF + ATOM + RAG ingestion
      - ``"deep_research"`` — broad multi-source deep search + DLM enrichment
      - ``"bibliotecario"`` — targeted entity/protein scan

    Returns a unified envelope with ``query_spec_hash``, ``protocol_version``,
    ``lane_used``, and ``papers_fetched`` always present, plus the full
    lane-specific result in ``payload``.

    This endpoint is synchronous (runs inline). For long-running scans use the
    lane-specific async endpoints (``/ingest``, ``/bibliotecario/scan``, etc.)
    which enqueue via Redis.
    """
    from mica.literature_consolidation.services.query_facade_service import (  # noqa: PLC0415
        LiteratureQueryFacadeService,
    )

    try:
        svc = LiteratureQueryFacadeService()
        result: LiteratureQueryFacadeResult = await svc.dispatch(payload, user_id=user_id)
        raw = normalize_poll_envelope(result.model_dump())
        return raw
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("unified_literature_query failed for lane=%s", payload.lane)
        raise HTTPException(status_code=502, detail=f"Query failed: {exc}") from exc

