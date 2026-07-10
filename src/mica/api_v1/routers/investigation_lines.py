# src/mica/api_v1/routers/investigation_lines.py
"""Investigation Lines router — P0 product-layer projection over Studies.

Doctrine:
  Investigation Line is a VIEW over Study. No parallel model.
  1 IL = 1 Study. Delete IL does NOT delete Study by default.
  Status stored in Study.metadata, not new table.

Endpoints:
  GET    /api/v1/investigation-lines         List ILs (projected from studies)
  POST   /api/v1/investigation-lines         Create IL (wraps Study create)
  GET    /api/v1/investigation-lines/{id}    Get IL detail
  PATCH  /api/v1/investigation-lines/{id}    Update IL (title, status, question)
  DELETE /api/v1/investigation-lines/{id}    Archive IL (soft-delete study)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/investigation-lines", tags=["investigation-lines"])

# ── Lifecycle ────────────────────────────────────────────────────────────────

ILStatus = Literal["proposed", "active", "paused", "archived"]

# ── Schemas ──────────────────────────────────────────────────────────────────

class InvestigationLineCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    research_question: Optional[str] = Field(None, max_length=2000)
    status: ILStatus = "proposed"
    tags: List[str] = Field(default_factory=list)

class InvestigationLineUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    research_question: Optional[str] = Field(None, max_length=2000)
    status: Optional[ILStatus] = None
    tags: Optional[List[str]] = None

class ProvenanceSummary(BaseModel):
    canonical: int = 0
    preview: int = 0
    stale: int = 0
    invalidated: int = 0
    other: int = 0

class InvestigationLine(BaseModel):
    il_ref: str
    study_ref: str
    title: str
    research_question: Optional[str] = None
    status: ILStatus
    tags: List[str] = Field(default_factory=list)
    working_set_refs: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    provenance_summary: ProvenanceSummary = Field(default_factory=ProvenanceSummary)
    created_by: str
    created_at: str
    updated_at: str

class InvestigationLineListResponse(BaseModel):
    lines: List[InvestigationLine]
    total: int

# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _study_to_il(study: Dict[str, Any], working_set_refs: List[str], artifact_refs: List[str]) -> InvestigationLine:
    """Project a Study row into an InvestigationLine."""
    meta = study.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    status = meta.get("il_status", "active")
    if status not in ("proposed", "active", "paused", "archived"):
        status = "active"

    # Derivar provenance_summary del estudio
    p = meta.get("provenance_summary", {})
    provenance_summary = ProvenanceSummary(
        canonical=p.get("canonical", 0),
        preview=p.get("preview", 0),
        stale=p.get("stale", 0),
        invalidated=p.get("invalidated", 0),
        other=p.get("other", 0),
    )

    return InvestigationLine(
        il_ref=f"il://studies/{study['study_id']}",
        study_ref=f"study://{study['study_id']}",
        title=meta.get("il_title") or study.get("name", "Untitled"),
        research_question=meta.get("research_question"),
        status=status,
        tags=study.get("tags") or [],
        working_set_refs=working_set_refs,
        artifact_refs=artifact_refs,
        provenance_summary=provenance_summary,
        created_by=study.get("user_id", ""),
        created_at=study.get("created_at", _now()),
        updated_at=study.get("updated_at", _now()),
    )


async def _get_pool():
    """Reuse studies router pool or create one."""
    from mica.api_v1.routers.studies import _get_pool as studies_pool
    return await studies_pool()


async def _fetch_working_set_refs(conn, study_id: str) -> List[str]:
    rows = await conn.fetch(
        "SELECT working_set_id FROM working_sets WHERE study_id = $1", study_id
    )
    return [f"ws://{r['working_set_id']}" for r in rows]


async def _fetch_artifact_refs(conn, study_id: str) -> List[str]:
    rows = await conn.fetch(
        """SELECT a.artifact_id FROM study_artifacts sa
           JOIN artifacts a ON sa.artifact_id = a.artifact_id
           WHERE sa.study_id = $1 LIMIT 100""",
        study_id,
    )
    return [f"artifact://{r['artifact_id']}" for r in rows]


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=InvestigationLineListResponse)
async def list_investigation_lines(
    user_id: str = Depends(user_dependency),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List Investigation Lines (projected from Studies)."""
    from mica.api_v1.product_schema import ensure_product_schema
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        where = "s.user_id = $1 AND s.archived = false"
        params = [user_id]
        idx = 2

        if status:
            # Filter by IL status stored in metadata->il_status
            where += f" AND (s.metadata->>'il_status')::text = ${idx}"
            params.append(status)
            idx += 1

        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM studies s WHERE {where}", *params
        )
        total = count_row["count"] if count_row else 0

        rows = await conn.fetch(
            f"""SELECT s.* FROM studies s
                WHERE {where}
                ORDER BY s.updated_at DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
            *params, limit, offset,
        )

    lines = []
    for r in rows:
        study = dict(r)
        ws_refs = []  # lazy: no fetch per-item in list view
        art_refs = []
        lines.append(_study_to_il(study, ws_refs, art_refs))

    return InvestigationLineListResponse(lines=lines, total=total)


@router.post("", response_model=InvestigationLine, status_code=201)
async def create_investigation_line(
    body: InvestigationLineCreate,
    user_id: str = Depends(user_dependency),
):
    """Create an Investigation Line (wraps Study.create)."""
    from mica.api_v1.routers.studies import create_study as backend_create_study
    from mica.api_v1.routers.studies import CreateStudyRequest

    # Delegate to Study.create with IL metadata
    study_resp = await backend_create_study(
        CreateStudyRequest(name=body.title, description=body.research_question, tags=body.tags),
        user_id,
    )
    # The response is a StudyResponse from the studies router
    study_dict = study_resp if isinstance(study_resp, dict) else study_resp.model_dump()

    # Re-open with metadata update for IL fields
    pool = await _get_pool()
    async with pool.acquire() as conn:
        meta = {
            "il_title": body.title,
            "il_status": body.status,
            "research_question": body.research_question,
        }
        await conn.execute(
            "UPDATE studies SET metadata = metadata || $1::jsonb, updated_at = now() WHERE study_id = $2",
            json.dumps(meta), study_dict["study_id"],
        )
        study_dict["metadata"] = {**study_dict.get("metadata", {}), **meta}

    return _study_to_il(study_dict, [], [])


@router.get("/{il_id}", response_model=InvestigationLine)
async def get_investigation_line(
    il_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get Investigation Line detail with working sets and artifacts."""
    from mica.api_v1.product_schema import ensure_product_schema
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM studies WHERE study_id = $1 AND user_id = $2 AND archived = false",
            il_id, user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Investigation Line not found")
        study = dict(row)
        ws_refs = await _fetch_working_set_refs(conn, il_id)
        art_refs = await _fetch_artifact_refs(conn, il_id)

    return _study_to_il(study, ws_refs, art_refs)


@router.patch("/{il_id}", response_model=InvestigationLine)
async def update_investigation_line(
    il_id: str,
    body: InvestigationLineUpdate,
    user_id: str = Depends(user_dependency),
):
    """Update Investigation Line metadata."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM studies WHERE study_id = $1 AND user_id = $2 AND archived = false",
            il_id, user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Investigation Line not found")
        study = dict(row)

        meta = study.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)

        if body.title is not None:
            meta["il_title"] = body.title
        if body.research_question is not None:
            meta["research_question"] = body.research_question
        if body.status is not None:
            meta["il_status"] = body.status
        if body.tags is not None:
            meta["tags"] = body.tags

        await conn.execute(
            "UPDATE studies SET metadata = $1::jsonb, updated_at = now() WHERE study_id = $2",
            json.dumps(meta), il_id,
        )
        study["metadata"] = meta

        ws_refs = await _fetch_working_set_refs(conn, il_id)
        art_refs = await _fetch_artifact_refs(conn, il_id)

    return _study_to_il(study, ws_refs, art_refs)


@router.delete("/{il_id}", status_code=204)
async def archive_investigation_line(
    il_id: str,
    user_id: str = Depends(user_dependency),
    delete_study: bool = Query(False, description="Also hard-delete the underlying Study"),
):
    """Archive (soft-delete) an Investigation Line.

    By default, the underlying Study is NOT deleted.
    Pass ?delete_study=true to also remove the Study.
    """
    from mica.api_v1.product_schema import ensure_product_schema
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Always mark IL as archived
        meta_update = json.dumps({"il_status": "archived"})
        if delete_study:
            await conn.execute(
                "UPDATE studies SET archived = true, metadata = metadata || $1::jsonb, updated_at = now() "
                "WHERE study_id = $2 AND user_id = $3",
                meta_update, il_id, user_id,
            )
        else:
            await conn.execute(
                "UPDATE studies SET metadata = metadata || $1::jsonb, updated_at = now() "
                "WHERE study_id = $2 AND user_id = $3",
                meta_update, il_id, user_id,
            )
