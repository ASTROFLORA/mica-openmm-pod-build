"""Artifacts router — P0 product-layer API with APV-03 PEP.

Endpoints:
  POST  /api/v1/artifacts                 Register artifact
  GET   /api/v1/artifacts                 List user's artifacts
  GET   /api/v1/artifacts/{id}            Get artifact
  GET   /api/v1/artifacts/{id}/download   Get signed download URL
  POST  /api/v1/artifacts/{id}/link       Link to another artifact
  DELETE /api/v1/artifacts/{id}           Archive artifact
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency, user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.identity.effective_context import EffectiveContext
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)
from mica.artifacts.membership import get_membership_store
from mica.tenancy.models import PermissionAction
from mica.tenancy.pep import (
    ensure_owner_grant_from_row,
    register_owner_grant,
    require_permission_http,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


def _signed_download_url_for(storage, *, user_id: str, object_path: str, expires_seconds: int = 900) -> str:
    return storage.signed_url(
        user_id=user_id,
        object_path=object_path,
        method="GET",
        expires_seconds=expires_seconds,
    )


class CreateArtifactRequest(BaseModel):
    artifact_type: str = Field(default="", description="structure, paper, figure, report, job_output, lmp_xml, sequence, molecule, trajectory, presenta_deck, note, plot")
    kind: Optional[str] = Field(None, description="Alias for 'artifact_type'")
    display_name: str = Field(default="", min_length=0, max_length=500)
    title: Optional[str] = Field(None, min_length=1, max_length=500, description="Alias for 'display_name'")
    source: Optional[str] = Field(None, description="upload, generated, imported, extracted")
    gcs_key: Optional[str] = None
    content_hash: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    ref_url: Optional[str] = Field(None, description="External reference URL (for non-blob artifacts)")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArtifactResponse(BaseModel):
    artifact_id: str
    user_id: str
    artifact_type: str
    display_name: str
    source: Optional[str] = None
    gcs_key: Optional[str] = None
    content_hash: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    ref_url: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    links: List[Dict[str, Any]] = Field(default_factory=list)


class ArtifactLinkRequest(BaseModel):
    target_artifact_id: str
    link_type: str = Field(..., description="derived_from, cites, contains, annotates")


class ArtifactListResponse(BaseModel):
    artifacts: List[ArtifactResponse]
    total: int


class LineageNodeResponse(BaseModel):
    lineage_id: str
    source_artifact_id: Optional[str] = None
    source_job_id: Optional[str] = None
    source_protocol_run_id: Optional[str] = None
    source_receipt_ref: Optional[str] = None
    lineage_type: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class LineageResponse(BaseModel):
    artifact_id: str
    lineage_chain: List[LineageNodeResponse] = Field(default_factory=list)


_POOL = None


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="Database not configured")
    _POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    return _POOL


def _pep_artifact(ctx: EffectiveContext, row, *, action: PermissionAction) -> None:
    artifact_id = str(row["artifact_id"])
    ensure_owner_grant_from_row(
        actor_user_id=ctx.actor_user_id,
        owner_user_id=row["user_id"],
        resource_type="artifact",
        resource_id=artifact_id,
    )
    require_permission_http(
        ctx=ctx,
        resource_type="artifact",
        resource_id=artifact_id,
        action=action,
    )


@router.post("", response_model=ArtifactResponse, status_code=201)
async def create_artifact(
    body: CreateArtifactRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Register a new artifact (any type)."""
    await ensure_product_schema()
    artifact_type = body.kind or body.artifact_type
    display_name = body.title or body.display_name
    if not artifact_type:
        raise HTTPException(status_code=422, detail="artifact_type or kind is required")
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name or title is required")
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO artifacts (user_id, artifact_type, display_name, source, gcs_key,
                       content_hash, mime_type, size_bytes, ref_url, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
               RETURNING artifact_id, user_id, artifact_type, display_name, source, gcs_key,
                         content_hash, mime_type, size_bytes, ref_url, metadata, created_at, updated_at""",
            user_id,
            artifact_type,
            display_name,
            body.source,
            body.gcs_key,
            body.content_hash,
            body.mime_type,
            body.size_bytes,
            body.ref_url,
            json.dumps(body.metadata) if body.metadata else "{}",
        )
    artifact_id = str(row["artifact_id"])
    register_owner_grant(owner_user_id=user_id, resource_type="artifact", resource_id=artifact_id)
    require_permission_http(
        ctx=ctx,
        resource_type="artifact",
        resource_id=artifact_id,
        action=PermissionAction.CREATE,
    )
    return _row_to_artifact(row, [])


@router.get("", response_model=ArtifactListResponse)
async def list_artifacts(
    user_id: str = Depends(user_dependency),
    artifact_type: Optional[str] = Query(None, description="Filter by type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List artifacts owned by user plus those visible via ArtifactMembership (APV-04)."""
    await ensure_product_schema()
    pool = await _get_pool()
    membership_ids = get_membership_store().list_visible_artifact_ids(user_id)

    conditions = ["user_id = $1"]
    params: list = [user_id]
    idx = 2
    if artifact_type:
        conditions.append(f"artifact_type = ${idx}")
        params.append(artifact_type)
        idx += 1
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM artifacts WHERE {where} ORDER BY created_at DESC",
            *params,
        )
        owned_ids = {str(r["artifact_id"]) for r in rows}
        extra_ids = [aid for aid in membership_ids if aid not in owned_ids]
        shared_rows = []
        if extra_ids:
            shared_q = "SELECT * FROM artifacts WHERE artifact_id = ANY($1::uuid[])"
            shared_params: list = [extra_ids]
            if artifact_type:
                shared_q += " AND artifact_type = $2"
                shared_params.append(artifact_type)
            shared_rows = await conn.fetch(shared_q, *shared_params)

    merged = list(rows) + list(shared_rows)
    merged.sort(key=lambda r: r["created_at"], reverse=True)
    total = len(merged)
    page = merged[offset : offset + limit]
    return ArtifactListResponse(
        artifacts=[_row_to_artifact(r, []) for r in page],
        total=total,
    )


@router.get("/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Get artifact details."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM artifacts WHERE artifact_id = $1", artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        _pep_artifact(ctx, row, action=PermissionAction.READ)
        link_rows = await conn.fetch(
            "SELECT * FROM artifact_links WHERE source_artifact_id = $1",
            artifact_id,
        )
    return _row_to_artifact(row, link_rows)


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Get signed download URL for artifact (if GCS-backed)."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT artifact_id, user_id, gcs_key, mime_type FROM artifacts WHERE artifact_id = $1",
            artifact_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    _pep_artifact(ctx, row, action=PermissionAction.READ)
    if not row["gcs_key"]:
        raise HTTPException(status_code=400, detail="Artifact has no GCS key (may be reference-only)")

    try:
        from mica.storage.gcs_user_storage import get_storage_manager

        storage = get_storage_manager()
        url = _signed_download_url_for(storage, user_id=row["user_id"], object_path=row["gcs_key"])
        return {
            "download_url": url,
            "artifact_id": artifact_id,
            "content_type": row.get("mime_type"),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Storage unavailable: {e}")


@router.post("/{artifact_id}/link", status_code=201)
async def link_artifact(
    artifact_id: str,
    body: ArtifactLinkRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Link this artifact to another (provenance edge)."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        for aid in [artifact_id, body.target_artifact_id]:
            row = await conn.fetchrow(
                "SELECT artifact_id, user_id FROM artifacts WHERE artifact_id = $1",
                aid,
            )
            if not row:
                raise HTTPException(status_code=404, detail=f"Artifact {aid} not found")
            _pep_artifact(ctx, row, action=PermissionAction.UPDATE)

        await conn.execute(
            """INSERT INTO artifact_links (source_artifact_id, target_artifact_id, link_type)
               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
            artifact_id,
            body.target_artifact_id,
            body.link_type,
        )
    return {
        "source_artifact_id": artifact_id,
        "target_artifact_id": body.target_artifact_id,
        "link_type": body.link_type,
        "status": "linked",
    }


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Delete an artifact (hard delete — removes from DB, not GCS)."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT artifact_id, user_id FROM artifacts WHERE artifact_id = $1",
            artifact_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        _pep_artifact(ctx, row, action=PermissionAction.DELETE)
        await conn.execute("DELETE FROM artifacts WHERE artifact_id = $1", artifact_id)


@router.get("/{artifact_id}/lineage", response_model=LineageResponse)
async def get_artifact_lineage(
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Get full causal lineage chain for an artifact."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        art = await conn.fetchrow(
            "SELECT artifact_id, user_id FROM artifacts WHERE artifact_id = $1",
            artifact_id,
        )
        if not art:
            raise HTTPException(status_code=404, detail="Artifact not found")
        _pep_artifact(ctx, art, action=PermissionAction.READ)
        rows = await conn.fetch(
            "SELECT * FROM artifact_lineage WHERE artifact_id = $1 ORDER BY created_at",
            artifact_id,
        )
    return LineageResponse(
        artifact_id=artifact_id,
        lineage_chain=[
            LineageNodeResponse(
                lineage_id=str(r["lineage_id"]),
                source_artifact_id=str(r["source_artifact_id"]) if r.get("source_artifact_id") else None,
                source_job_id=r.get("source_job_id"),
                source_protocol_run_id=r.get("source_protocol_run_id"),
                source_receipt_ref=r.get("source_receipt_ref"),
                lineage_type=r["lineage_type"],
                metadata=r.get("metadata") or {},
                created_at=r["created_at"].isoformat(),
            )
            for r in rows
        ],
    )


@router.get("/{artifact_id}/preview")
async def get_artifact_preview(
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Get signed preview URL for an artifact."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        art = await conn.fetchrow(
            "SELECT artifact_id, user_id, gcs_key, mime_type FROM artifacts WHERE artifact_id = $1",
            artifact_id,
        )
        if not art:
            raise HTTPException(status_code=404, detail="Artifact not found")
        _pep_artifact(ctx, art, action=PermissionAction.READ)
        preview = await conn.fetchrow(
            "SELECT gcs_key, mime_type FROM artifact_previews WHERE artifact_id = $1 LIMIT 1",
            artifact_id,
        )
        gcs_key = preview["gcs_key"] if preview else art.get("gcs_key")
        if not gcs_key:
            raise HTTPException(status_code=400, detail="No preview available for this artifact")
    try:
        from mica.storage.gcs_user_storage import get_storage_manager

        storage = get_storage_manager()
        url = _signed_download_url_for(storage, user_id=art["user_id"], object_path=gcs_key)
        return {
            "preview_url": url,
            "artifact_id": artifact_id,
            "content_type": preview["mime_type"] if preview else art.get("mime_type"),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Storage unavailable: {e}")


def _parse_jsonb(raw, default=None):
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default
    return default


def _row_to_artifact(row, link_rows) -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id=str(row["artifact_id"]),
        user_id=row["user_id"],
        artifact_type=row["artifact_type"],
        display_name=row["display_name"],
        source=row.get("source"),
        gcs_key=row.get("gcs_key"),
        content_hash=row.get("content_hash"),
        mime_type=row.get("mime_type"),
        size_bytes=row.get("size_bytes"),
        ref_url=row.get("ref_url"),
        metadata=_parse_jsonb(row.get("metadata"), {}),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        links=[
            {"target_artifact_id": str(lr["target_artifact_id"]), "link_type": lr["link_type"]}
            for lr in link_rows
        ],
    )
