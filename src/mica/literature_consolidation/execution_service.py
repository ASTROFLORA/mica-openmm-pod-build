"""execution_service.py — Stable service-layer entrypoints for literature execution.

Workers import from HERE, never from ``mica.api_v1.routers.*``.
This module is the anti-corruption boundary between the worker plane and the
HTTP router plane. It:

  • Accepts plain ``dict`` payloads (no router model coupling for callers).
    • Uses consolidation service modules for execution (router-agnostic).
  • Converts HTTP-specific exceptions (``HTTPException``) to ``ValueError``
    so workers can handle errors without importing FastAPI.
  • Provides stable symbol names that will survive future router refactors.

Planned evolution (follow-on): move core execution logic from router modules
into this service module so routers become thin HTTP wrappers.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("mica.literature_consolidation.execution_service")


# ── Deep Research ─────────────────────────────────────────────────────────────

async def run_deep_research(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a deep-research scan.  Returns the result as a plain dict.

    Args:
        payload: Field-for-field equivalent of ``DeepResearchRequest``.

    Returns:
        ``DeepResearchResult.model_dump()``

    Raises:
        ValueError: On execution failure (converts FastAPI HTTPException).
    """
    from mica.literature_consolidation.services.deep_research_service import (  # noqa: PLC0415
        DeepResearchExecutionRequest,
        run_deep_research as _run_deep_research,
    )

    try:
        req = DeepResearchExecutionRequest(**payload)
        return await _run_deep_research(req)
    except Exception as exc:
        _maybe_convert_http_exc(exc, context="run_deep_research")
        raise


# ── Research Pipeline ─────────────────────────────────────────────────────────

async def run_research_pipeline(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the full research pipeline.  Returns a plain dict.

    Args:
        payload: Field-for-field equivalent of ``PipelineRequest``.

    Returns:
        Pipeline result dict.

    Raises:
        ValueError: On execution failure.
    """
    from mica.literature_consolidation.services.research_pipeline_service import (  # noqa: PLC0415
        ResearchPipelineExecutionRequest,
        run_research_pipeline as _run_pipeline,
    )

    try:
        req = ResearchPipelineExecutionRequest(**payload)
        return await _run_pipeline(req)
    except Exception as exc:
        _maybe_convert_http_exc(exc, context="run_research_pipeline")
        raise


# ── Literature Ingest ─────────────────────────────────────────────────────────

async def run_literature_ingest(
    payload: Dict[str, Any],
    user_id: str,
    prod_env: bool = False,
) -> Dict[str, Any]:
    """Execute a literature ingestion job.  Returns a plain dict.

    Args:
        payload: Field-for-field equivalent of ``LiteratureIngestRequest``.
        user_id: Authenticated user identifier.
        prod_env: When True, SQLite atom backend is forbidden (same as
            ``_PROD_ENV`` in the router).

    Returns:
        ``{"ok": True, "summary": ..., "papers_fetched": ...}``

    Raises:
        ValueError: When atom_backend='sqlite' is forbidden in production,
            or on other execution failures.
    """
    from mica.literature_consolidation.services.literature_ingest_service import (  # noqa: PLC0415
        LiteratureIngestExecutionRequest,
        run_literature_ingest as _run_ingest,
    )

    # Guard the prod SQLite restriction before calling into the router so the
    # router's HTTPException is never raised in a worker context.
    try:
        req = LiteratureIngestExecutionRequest(**payload)
        return await _run_ingest(req, user_id, prod_env=prod_env)
    except Exception as exc:
        _maybe_convert_http_exc(exc, context="run_literature_ingest")
        raise


# ── Bibliotecario Scan ────────────────────────────────────────────────────────

async def run_bibliotecario_scan(
    job_id: str,
    payload: Dict[str, Any],
) -> None:
    """Execute a bibliotecario scan via the decoupled service layer.

    Delegates to ``bibliotecario_service.run_bibliotecario_scan`` so that
    this module (the consolidation execution boundary) has NO direct dependency
    on ``mica.api_v1.routers.bibliotecario``.

    Args:
        job_id: Stable job identifier used as the scan_id.
        payload: Scan parameters dict (query, preset, entities, …).

    Raises:
        ValueError: On execution failure.
    """
    from mica.literature_consolidation.services.bibliotecario_service import (  # noqa: PLC0415
        BibliotecarioScanExecutionRequest,
        run_bibliotecario_scan as _run_bib_scan,
    )

    try:
        req = BibliotecarioScanExecutionRequest(
            query=payload.get("query", ""),
            preset=payload.get("preset", "deep-synthesis"),
            entities=payload.get("entities", []),
            pdb_ids=payload.get("pdb_ids", []),
            max_papers=int(payload.get("max_papers", 200)),
            sources=payload.get("sources", ["semantic_scholar", "pubmed", "openalex"]),
            session_id=payload.get("session_id") or payload.get("run_id") or job_id,
            run_id=payload.get("run_id") or job_id,
            user_id=payload.get("user_id", "anonymous"),
            acquisition_budget_usd=payload.get("acquisition_budget_usd"),
        )
        await _run_bib_scan(job_id, req)
    except Exception as exc:
        _maybe_convert_http_exc(exc, context=f"run_bibliotecario_scan[{job_id[:8]}]")
        raise


# ── Internal helpers ──────────────────────────────────────────────────────────

def _maybe_convert_http_exc(exc: BaseException, context: str) -> None:
    """If *exc* is a FastAPI ``HTTPException``, re-raise as ``ValueError``.

    This prevents FastAPI internals from leaking into the worker plane.
    Does nothing for all other exception types (they propagate as-is).
    """
    try:
        from fastapi import HTTPException  # noqa: PLC0415
    except ImportError:
        return
    if isinstance(exc, HTTPException):
        raise ValueError(
            f"[{context}] HTTP {exc.status_code}: {exc.detail}"
        ) from exc
