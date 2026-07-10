"""Workspace Snapshots router — P0 product-layer API.

Endpoints:
  POST /api/v1/workspace-snapshots               Create snapshot
  GET  /api/v1/workspace-snapshots               List snapshots
  GET  /api/v1/workspace-snapshots/{id}          Get snapshot
  POST /api/v1/workspace-snapshots/{id}/restore  Restore snapshot
  DELETE /api/v1/workspace-snapshots/{id}        Delete snapshot
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import choose_neon_database_url, asyncpg_connection_kwargs_for_database_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workspace-snapshots", tags=["workspace-snapshots"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CreateSnapshotRequest(BaseModel):
    study_id: Optional[str] = Field(None, description="Optional study scope")
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    snapshot_data: Dict[str, Any] = Field(default_factory=dict, description="Full workspace state")
    state: Optional[Dict[str, Any]] = Field(None, description="Alias for snapshot_data")

class SnapshotResponse(BaseModel):
    snapshot_id: str
    study_id: Optional[str] = None
    user_id: str
    name: Optional[str] = None
    snapshot_data: Dict[str, Any]
    created_at: str
    restored_at: Optional[str] = None

class SnapshotListResponse(BaseModel):
    snapshots: List[SnapshotResponse]
    total: int

class RestoreResponse(BaseModel):
    snapshot_id: str
    restored: bool
    restored_at: str

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

@router.post("", response_model=SnapshotResponse, status_code=201)
async def create_snapshot(
    body: CreateSnapshotRequest,
    user_id: str = Depends(user_dependency),
):
    """Save a workspace snapshot."""
    await ensure_product_schema()
    # Accept 'state' as alias for 'snapshot_data' (canonical contract alignment)
    data = body.snapshot_data or body.state or {}
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO workspace_snapshots (study_id, user_id, name, snapshot_data)
               VALUES ($1, $2, $3, $4::jsonb)
               RETURNING snapshot_id, study_id, user_id, name, snapshot_data, created_at, restored_at""",
            body.study_id, user_id, body.name, json.dumps(data) if data else '{}',
        )
    return _row_to_snapshot(row)


@router.get("", response_model=SnapshotListResponse)
async def list_snapshots(
    user_id: str = Depends(user_dependency),
    study_id: Optional[str] = Query(None, description="Filter by study"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List user's workspace snapshots."""
    await ensure_product_schema()
    pool = await _get_pool()

    conditions = ["user_id = $1"]
    params: list = [user_id]
    idx = 2
    if study_id:
        conditions.append(f"study_id = ${idx}")
        params.append(study_id)
        idx += 1

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM workspace_snapshots WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, limit, offset,
        )
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM workspace_snapshots WHERE {where}", *params,
        )
    return SnapshotListResponse(
        snapshots=[_row_to_snapshot(r) for r in rows],
        total=total_row["count"] if total_row else 0,
    )


@router.get("/{snapshot_id}", response_model=SnapshotResponse)
async def get_snapshot(
    snapshot_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get a snapshot by ID."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM workspace_snapshots WHERE snapshot_id = $1 AND user_id = $2",
            snapshot_id, user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return _row_to_snapshot(row)


@router.post("/{snapshot_id}/restore", response_model=RestoreResponse)
async def restore_snapshot(
    snapshot_id: str,
    user_id: str = Depends(user_dependency),
):
    """Mark a snapshot as restored (returns snapshot data for frontend to apply)."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "UPDATE workspace_snapshots SET restored_at = now() WHERE snapshot_id = $1 AND user_id = $2 "
            "RETURNING restored_at",
            snapshot_id, user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return RestoreResponse(
        snapshot_id=snapshot_id,
        restored=True,
        restored_at=row.isoformat(),
    )


@router.delete("/{snapshot_id}", status_code=204)
async def delete_snapshot(
    snapshot_id: str,
    user_id: str = Depends(user_dependency),
):
    """Delete a snapshot."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM workspace_snapshots WHERE snapshot_id = $1 AND user_id = $2",
            snapshot_id, user_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Snapshot not found")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_jsonb(raw, default=None):
    if raw is None: return default
    if isinstance(raw, (dict, list)): return raw
    if isinstance(raw, str):
        try: return json.loads(raw)
        except (json.JSONDecodeError, TypeError): return default
    return default

def _row_to_snapshot(row) -> SnapshotResponse:
    return SnapshotResponse(
        snapshot_id=str(row["snapshot_id"]),
        study_id=str(row["study_id"]) if row.get("study_id") else None,
        user_id=row["user_id"],
        name=row.get("name"),
        snapshot_data=_parse_jsonb(row.get("snapshot_data"), {}),
        created_at=row["created_at"].isoformat(),
        restored_at=row["restored_at"].isoformat() if row.get("restored_at") else None,
    )
