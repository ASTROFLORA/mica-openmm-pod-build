"""Studies router — P0 product-layer API.

Endpoints:
  POST   /api/v1/studies            Create study
  GET    /api/v1/studies            List user's studies
  GET    /api/v1/studies/{id}       Get study + artifacts
  PUT    /api/v1/studies/{id}       Update name/description/tags
  DELETE /api/v1/studies/{id}       Archive (soft-delete)
  POST   /api/v1/studies/{id}/artifacts  Add artifact to study
  DELETE /api/v1/studies/{id}/artifacts/{aid}  Remove artifact
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency, user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.api_v1.services.product_link_service import (
    ProductLinkNotFoundError,
    ProductLinkServiceError,
    attach_artifact_to_study_for_user,
)
from mica.artifacts.membership import attach_artifact_membership
from mica.identity.effective_context import EffectiveContext
from mica.infrastructure.persistence.pg_async import choose_neon_database_url
from mica.infrastructure.persistence.pg_async import asyncpg_connection_kwargs_for_database_url
from mica.tenancy.models import PermissionAction
from mica.tenancy.pep import (
    ensure_owner_grant_from_row,
    register_owner_grant,
    require_permission_http,
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/studies", tags=["studies"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CreateStudyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="Study name")
    description: Optional[str] = Field(None, max_length=2000)
    tags: List[str] = Field(default_factory=list)

class UpdateStudyRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    tags: Optional[List[str]] = None

class StudyResponse(BaseModel):
    study_id: str
    user_id: str
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    archived: bool = False
    artifact_count: int = 0

class AddArtifactRequest(BaseModel):
    artifact_id: str

class StudyListResponse(BaseModel):
    studies: List[StudyResponse]
    total: int

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_POOL = None

async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    import asyncpg
    dsn = choose_neon_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="Database not configured")
    _POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn))
    return _POOL

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=StudyResponse, status_code=201)
async def create_study(
    body: CreateStudyRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Create a new study."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO studies (user_id, name, description, tags)
               VALUES ($1, $2, $3, $4)
               RETURNING study_id, user_id, name, description, tags,
                         metadata, created_at, updated_at, archived""",
            user_id, body.name, body.description, body.tags,
        )
    study_id = str(row["study_id"])
    register_owner_grant(owner_user_id=user_id, resource_type="study", resource_id=study_id)
    require_permission_http(
        ctx=ctx,
        resource_type="study",
        resource_id=study_id,
        action=PermissionAction.CREATE,
    )
    return _row_to_study(row, 0)


@router.get("", response_model=StudyListResponse)
async def list_studies(
    user_id: str = Depends(user_dependency),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    archived: bool = Query(False),
):
    """List user's studies."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.*, COALESCE(sa.cnt, 0) AS artifact_count
               FROM studies s
               LEFT JOIN (SELECT study_id, COUNT(*) AS cnt FROM study_artifacts GROUP BY study_id) sa
                 ON s.study_id = sa.study_id
               WHERE s.user_id = $1 AND s.archived = $2
               ORDER BY s.updated_at DESC
               LIMIT $3 OFFSET $4""",
            user_id, archived, limit, offset,
        )
        total_row = await conn.fetchrow(
            "SELECT COUNT(*) FROM studies WHERE user_id = $1 AND archived = $2",
            user_id, archived,
        )
    studies = [_row_to_study(r, r.get("artifact_count", 0)) for r in rows]
    return StudyListResponse(studies=studies, total=total_row["count"] if total_row else 0)


@router.get("/{study_id}", response_model=StudyResponse)
async def get_study(
    study_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Get study details."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.*, COALESCE(sa.cnt, 0) AS artifact_count
               FROM studies s
               LEFT JOIN (SELECT study_id, COUNT(*) AS cnt FROM study_artifacts GROUP BY study_id) sa
                 ON s.study_id = sa.study_id
               WHERE s.study_id = $1""",
            study_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Study not found")
    ensure_owner_grant_from_row(
        actor_user_id=ctx.actor_user_id,
        owner_user_id=row["user_id"],
        resource_type="study",
        resource_id=study_id,
    )
    require_permission_http(
        ctx=ctx,
        resource_type="study",
        resource_id=study_id,
        action=PermissionAction.READ,
    )
    return _row_to_study(row, row.get("artifact_count", 0))


@router.put("/{study_id}", response_model=StudyResponse)
async def update_study(
    study_id: str,
    body: UpdateStudyRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Update study name, description, or tags."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT study_id, user_id FROM studies WHERE study_id = $1",
            study_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Study not found")
        ensure_owner_grant_from_row(
            actor_user_id=ctx.actor_user_id,
            owner_user_id=existing["user_id"],
            resource_type="study",
            resource_id=study_id,
        )
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=study_id,
            action=PermissionAction.UPDATE,
        )

        updates = []
        params = []
        idx = 1
        if body.name is not None:
            updates.append(f"name = ${idx}"); params.append(body.name); idx += 1
        if body.description is not None:
            updates.append(f"description = ${idx}"); params.append(body.description); idx += 1
        if body.tags is not None:
            updates.append(f"tags = ${idx}"); params.append(body.tags); idx += 1
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updates.append("updated_at = now()")
        params.append(study_id)

        row = await conn.fetchrow(
            f"UPDATE studies SET {', '.join(updates)} WHERE study_id = ${idx} "
            f"RETURNING study_id, user_id, name, description, tags, metadata, created_at, updated_at, archived",
            *params,
        )
    return _row_to_study(row, 0)


@router.delete("/{study_id}", status_code=204)
async def archive_study(
    study_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Soft-delete (archive) a study."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT study_id, user_id FROM studies WHERE study_id = $1",
            study_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Study not found")
        ensure_owner_grant_from_row(
            actor_user_id=ctx.actor_user_id,
            owner_user_id=existing["user_id"],
            resource_type="study",
            resource_id=study_id,
        )
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=study_id,
            action=PermissionAction.DELETE,
        )
        await conn.execute(
            "UPDATE studies SET archived = true, updated_at = now() WHERE study_id = $1",
            study_id,
        )


@router.post("/{study_id}/artifacts", status_code=201)
async def add_artifact_to_study(
    study_id: str,
    body: AddArtifactRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Link an artifact to a study."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT study_id, user_id FROM studies WHERE study_id = $1",
            study_id,
        )
    if not existing:
        raise HTTPException(status_code=404, detail="Study not found")
    ensure_owner_grant_from_row(
        actor_user_id=ctx.actor_user_id,
        owner_user_id=existing["user_id"],
        resource_type="study",
        resource_id=study_id,
    )
    require_permission_http(
        ctx=ctx,
        resource_type="study",
        resource_id=study_id,
        action=PermissionAction.UPDATE,
    )
    try:
        linked = await attach_artifact_to_study_for_user(
            user_id=user_id,
            study_id=study_id,
            artifact_id=body.artifact_id,
        )
    except ProductLinkNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except ProductLinkServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    # APV-04: typed membership + grant bridge (dual-write with study_artifacts).
    membership = attach_artifact_membership(
        ctx=ctx,
        artifact_id=body.artifact_id,
        container_type="study",
        container_id=study_id,
        semantic_role="attached",
        grantee_user_id=user_id,
        acl_role="editor",
    )
    return {
        **linked,
        "membership_id": membership.membership_id,
        "receipt_id": membership.receipt_id,
        "home_scope_id": membership.home_scope_id,
    }


@router.delete("/{study_id}/artifacts/{artifact_id}", status_code=204)
async def remove_artifact_from_study(
    study_id: str,
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Unlink an artifact from a study."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT study_id, user_id FROM studies WHERE study_id = $1",
            study_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Study not found")
        ensure_owner_grant_from_row(
            actor_user_id=ctx.actor_user_id,
            owner_user_id=existing["user_id"],
            resource_type="study",
            resource_id=study_id,
        )
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=study_id,
            action=PermissionAction.UPDATE,
        )
        await conn.execute(
            "DELETE FROM study_artifacts WHERE study_id = $1 AND artifact_id = $2",
            study_id, artifact_id,
        )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class StudySummaryResponse(BaseModel):
    study_id: str
    title: str
    paper_count: int = 0
    structure_count: int = 0
    job_count: int = 0
    figure_count: int = 0
    trajectory_count: int = 0
    notes_count: int = 0
    last_activity: Optional[str] = None
    active_working_sets: int = 0
    open_blockers: List[str] = Field(default_factory=list)

class WorkingSetRef(BaseModel):
    working_set_id: str
    title: str
    purpose: str
    artifact_count: int = 0
    created_at: str

class WorkingSetListResponse(BaseModel):
    working_sets: List[WorkingSetRef]
    total: int


@router.get("/{study_id}/summary", response_model=StudySummaryResponse)
async def get_study_summary(
    study_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Get cheap StudySummary — does NOT load all artifacts."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        study = await conn.fetchrow(
            "SELECT study_id, name, user_id FROM studies WHERE study_id = $1",
            study_id,
        )
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        ensure_owner_grant_from_row(
            actor_user_id=ctx.actor_user_id,
            owner_user_id=study["user_id"],
            resource_type="study",
            resource_id=study_id,
        )
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=study_id,
            action=PermissionAction.READ,
        )

        counts = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE a.artifact_type = 'paper') AS paper_count,
                COUNT(*) FILTER (WHERE a.artifact_type = 'structure') AS structure_count,
                COUNT(*) FILTER (WHERE a.artifact_type = 'figure') AS figure_count,
                COUNT(*) FILTER (WHERE a.artifact_type = 'trajectory') AS trajectory_count,
                COUNT(*) FILTER (WHERE a.artifact_type = 'note') AS notes_count,
                COUNT(*) AS total_count
            FROM study_artifacts sa
            JOIN artifacts a ON sa.artifact_id = a.artifact_id
            WHERE sa.study_id = $1
        """, study_id)

        ws_count = await conn.fetchval(
            "SELECT COUNT(*) FROM working_sets WHERE study_id = $1", study_id,
        )
        last_act = await conn.fetchval(
            "SELECT updated_at FROM studies WHERE study_id = $1", study_id,
        )

    return StudySummaryResponse(
        study_id=study_id,
        title=study["name"],
        paper_count=counts["paper_count"] or 0,
        structure_count=counts["structure_count"] or 0,
        job_count=0,  # jobs not linked to studies yet
        figure_count=counts["figure_count"] or 0,
        trajectory_count=counts["trajectory_count"] or 0,
        notes_count=counts["notes_count"] or 0,
        last_activity=last_act.isoformat() if last_act else None,
        active_working_sets=ws_count or 0,
        open_blockers=[],
    )


@router.get("/{study_id}/working-sets", response_model=WorkingSetListResponse)
async def list_study_working_sets(
    study_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """List working sets for a study."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        study = await conn.fetchrow(
            "SELECT study_id, user_id FROM studies WHERE study_id = $1",
            study_id,
        )
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        ensure_owner_grant_from_row(
            actor_user_id=ctx.actor_user_id,
            owner_user_id=study["user_id"],
            resource_type="study",
            resource_id=study_id,
        )
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=study_id,
            action=PermissionAction.READ,
        )

        rows = await conn.fetch(
            """SELECT ws.working_set_id, ws.title, ws.purpose, ws.created_at,
                      COUNT(wsi.item_id) AS artifact_count
               FROM working_sets ws
               LEFT JOIN working_set_items wsi ON ws.working_set_id = wsi.working_set_id
               WHERE ws.study_id = $1
               GROUP BY ws.working_set_id, ws.title, ws.purpose, ws.created_at
               ORDER BY ws.created_at DESC""",
            study_id,
        )
    working_sets = [
        WorkingSetRef(
            working_set_id=str(r["working_set_id"]),
            title=r["title"],
            purpose=r["purpose"] or "custom",
            artifact_count=r["artifact_count"] or 0,
            created_at=r["created_at"].isoformat(),
        ) for r in rows
    ]
    return WorkingSetListResponse(working_sets=working_sets, total=len(working_sets))


def _row_to_study(row, artifact_count: int) -> StudyResponse:
    import json
    metadata_raw = row.get("metadata")
    if isinstance(metadata_raw, str):
        try:
            metadata = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = metadata_raw
    else:
        metadata = {}
    tags_raw = row.get("tags")
    if isinstance(tags_raw, str):
        try:
            tags = json.loads(tags_raw)
        except (json.JSONDecodeError, TypeError):
            tags = []
    elif isinstance(tags_raw, list):
        tags = tags_raw
    else:
        tags = []
    return StudyResponse(
        study_id=str(row["study_id"]),
        user_id=row["user_id"],
        name=row["name"],
        description=row.get("description"),
        tags=tags,
        metadata=metadata,
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        archived=bool(row.get("archived", False)),
        artifact_count=artifact_count,
    )
