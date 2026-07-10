"""Working Sets router — P0 product-layer API (canonical name per SURFACE_COLLISION_AUDIT C-01).

Endpoints:
  POST /api/v1/window-groups              Create window group
  GET  /api/v1/window-groups              List user's window groups
  GET  /api/v1/window-groups/{id}         Get group + items
  PUT  /api/v1/window-groups/{id}         Update group
  DELETE /api/v1/window-groups/{id}       Delete group
  POST /api/v1/window-groups/{id}/items   Add item to group
  DELETE /api/v1/window-groups/{id}/items/{iid}  Remove item
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.api_v1.services.product_link_service import (
    ProductLinkNotFoundError,
    ProductLinkServiceError,
    attach_artifact_to_working_set_for_user,
)
from mica.infrastructure.persistence.pg_async import choose_neon_database_url, asyncpg_connection_kwargs_for_database_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/working-sets", tags=["working-sets"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class WorkingSetItemModel(BaseModel):
    artifact_ref_type: str = Field(..., description="paper, structure, job, figure, asset")
    artifact_ref_id: str
    position: int = 0
    config: Dict[str, Any] = Field(default_factory=dict)

class CreateWorkingSetRequest(BaseModel):
    name: str = Field(default="", min_length=0, max_length=200)
    title: Optional[str] = Field(None, min_length=1, max_length=200, description="Alias for 'name'")
    description: Optional[str] = Field(None, max_length=2000)
    study_id: Optional[str] = None
    layout_data: Dict[str, Any] = Field(default_factory=dict)
    items: List[WorkingSetItemModel] = Field(default_factory=list)

class UpdateWorkingSetRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    layout_data: Optional[Dict[str, Any]] = None

class WorkingSetResponse(BaseModel):
    working_set_id: str
    study_id: Optional[str] = None
    user_id: str
    name: str
    description: Optional[str] = None
    layout_data: Dict[str, Any]
    items: List[WorkingSetItemModel] = Field(default_factory=list)
    created_at: str
    updated_at: str

class WorkingSetListResponse(BaseModel):
    groups: List[WorkingSetResponse]
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

async def __fetch_working_set(conn, working_set_id: str, user_id: str):
    row = await conn.fetchrow(
        "SELECT * FROM working_sets WHERE working_set_id = $1 AND user_id = $2",
        working_set_id, user_id,
    )
    if not row:
        return None
    item_rows = await conn.fetch(
        "SELECT * FROM working_set_items WHERE working_set_id = $1 ORDER BY position",
        working_set_id,
    )
    items = [WorkingSetItemModel(
        artifact_ref_type=r["artifact_ref_type"],
        artifact_ref_id=r["artifact_ref_id"],
        position=r["position"],
        config=_parse_jsonb(r.get("config"), {}),
    ) for r in item_rows]
    return _row_to_working_set(row, items)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=WorkingSetResponse, status_code=201)
async def create_working_set(
    body: CreateWorkingSetRequest,
    user_id: str = Depends(user_dependency),
):
    """Create a window group with optional items."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO working_sets (study_id, user_id, title, description, layout_data)
                   VALUES ($1, $2, $3, $4, $5::jsonb)
                   RETURNING working_set_id, study_id, user_id, title, description, layout_data, created_at, updated_at""",
                body.study_id, user_id, body.title or body.name, body.description,
                json.dumps(body.layout_data) if body.layout_data else '{}',
            )
            for item in body.items:
                await conn.execute(
                    """INSERT INTO working_set_items (working_set_id, artifact_ref_type, artifact_ref_id, position, config)
                       VALUES ($1, $2, $3, $4, $5::jsonb)""",
                    row["working_set_id"], item.artifact_ref_type, item.artifact_ref_id, item.position,
                    json.dumps(item.config) if item.config else '{}',
                )
    return _row_to_working_set(row, body.items)


@router.get("", response_model=WorkingSetListResponse)
async def list_working_sets(
    user_id: str = Depends(user_dependency),
    study_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List user's window groups."""
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
            f"SELECT * FROM working_sets WHERE {where} ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, limit, offset,
        )
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM working_sets WHERE {where}", *params,
        )
        groups = []
        for r in rows:
            item_rows = await conn.fetch(
                "SELECT * FROM working_set_items WHERE working_set_id = $1 ORDER BY position",
                r["working_set_id"],
            )
            items = [WorkingSetItemModel(
                artifact_ref_type=ir["artifact_ref_type"],
                artifact_ref_id=ir["artifact_ref_id"],
                position=ir["position"],
                config=_parse_jsonb(ir.get("config"), {}),
            ) for ir in item_rows]
            groups.append(_row_to_working_set(r, items))
    return WorkingSetListResponse(groups=groups, total=total_row["count"] if total_row else 0)


@router.get("/{working_set_id}", response_model=WorkingSetResponse)
async def get_working_set(
    working_set_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get window group with all items."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        group = await __fetch_working_set(conn, working_set_id, user_id)
    if not group:
        raise HTTPException(status_code=404, detail="Window group not found")
    return group


@router.put("/{working_set_id}", response_model=WorkingSetResponse)
async def update_working_set(
    working_set_id: str,
    body: UpdateWorkingSetRequest,
    user_id: str = Depends(user_dependency),
):
    """Update window group metadata."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT working_set_id FROM working_sets WHERE working_set_id = $1 AND user_id = $2",
            working_set_id, user_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Window group not found")

        updates = []
        params = []
        idx = 1
        display_name = body.name or body.title
        if display_name is not None:
            updates.append(f"title = ${idx}"); params.append(display_name); idx += 1
        if body.description is not None:
            updates.append(f"description = ${idx}"); params.append(body.description); idx += 1
        if body.layout_data is not None:
            updates.append(f"layout_data = ${idx}::jsonb"); params.append(json.dumps(body.layout_data) if body.layout_data else '{}'); idx += 1
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updates.append("updated_at = now()")
        params.extend([working_set_id, user_id])
        await conn.execute(
            f"UPDATE working_sets SET {', '.join(updates)} WHERE working_set_id = ${idx} AND user_id = ${idx+1}",
            *params,
        )
        return await __fetch_working_set(conn, working_set_id, user_id)


@router.delete("/{working_set_id}", status_code=204)
async def delete_working_set(
    working_set_id: str,
    user_id: str = Depends(user_dependency),
):
    """Delete a window group and its items (CASCADE)."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM working_sets WHERE working_set_id = $1 AND user_id = $2",
            working_set_id, user_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Window group not found")


@router.post("/{working_set_id}/items", status_code=201)
async def add_item_to_working_set(
    working_set_id: str,
    body: WorkingSetItemModel,
    user_id: str = Depends(user_dependency),
):
    """Add an item to a window group."""
    try:
        return await attach_artifact_to_working_set_for_user(
            user_id=user_id,
            working_set_id=working_set_id,
            artifact_id=body.artifact_ref_id,
            position=body.position,
            config=body.config,
            artifact_ref_type=body.artifact_ref_type,
        )
    except ProductLinkNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except ProductLinkServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.delete("/{working_set_id}/items/{item_id}", status_code=204)
async def remove_item_from_working_set(
    working_set_id: str,
    item_id: str,
    user_id: str = Depends(user_dependency),
):
    """Remove an item from a window group."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM working_set_items WHERE item_id = $1 AND working_set_id = $2",
            item_id, working_set_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_jsonb(raw, default=None):
    """Parse asyncpg JSONB string into Python dict/list, with fallback."""
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


def _row_to_working_set(row, items: List[WorkingSetItemModel]) -> WorkingSetResponse:
    return WorkingSetResponse(
        working_set_id=str(row["working_set_id"]),
        study_id=str(row["study_id"]) if row.get("study_id") else None,
        user_id=row["user_id"],
        name=row.get("title") or row.get("name", ""),
        description=row.get("description"),
        layout_data=_parse_jsonb(row.get("layout_data"), {}),
        items=items,
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
