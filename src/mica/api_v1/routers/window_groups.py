"""Window Groups router — P0 product-layer API.

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

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import choose_neon_database_url, asyncpg_connection_kwargs_for_database_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/window-groups", tags=["window-groups"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class WindowGroupItemModel(BaseModel):
    artifact_ref_type: str = Field(..., description="paper, structure, job, figure, asset")
    artifact_ref_id: str
    position: int = 0
    config: Dict[str, Any] = Field(default_factory=dict)

class CreateWindowGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    study_id: Optional[str] = None
    layout_data: Dict[str, Any] = Field(default_factory=dict)
    items: List[WindowGroupItemModel] = Field(default_factory=list)

class UpdateWindowGroupRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    layout_data: Optional[Dict[str, Any]] = None

class WindowGroupResponse(BaseModel):
    group_id: str
    study_id: Optional[str] = None
    user_id: str
    name: str
    description: Optional[str] = None
    layout_data: Dict[str, Any]
    items: List[WindowGroupItemModel] = Field(default_factory=list)
    created_at: str
    updated_at: str

class WindowGroupListResponse(BaseModel):
    groups: List[WindowGroupResponse]
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

async def _fetch_group(conn, group_id: str, user_id: str):
    row = await conn.fetchrow(
        "SELECT * FROM window_groups WHERE group_id = $1 AND user_id = $2",
        group_id, user_id,
    )
    if not row:
        return None
    item_rows = await conn.fetch(
        "SELECT * FROM window_group_items WHERE group_id = $1 ORDER BY position",
        group_id,
    )
    items = [WindowGroupItemModel(
        artifact_ref_type=r["artifact_ref_type"],
        artifact_ref_id=r["artifact_ref_id"],
        position=r["position"],
        config=r["config"] or {},
    ) for r in item_rows]
    return _row_to_group(row, items)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=WindowGroupResponse, status_code=201)
async def create_window_group(
    body: CreateWindowGroupRequest,
    user_id: str = Depends(user_dependency),
):
    """Create a window group with optional items."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO window_groups (study_id, user_id, name, description, layout_data)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING group_id, study_id, user_id, name, description, layout_data, created_at, updated_at""",
                body.study_id, user_id, body.name, body.description, body.layout_data,
            )
            for item in body.items:
                await conn.execute(
                    """INSERT INTO window_group_items (group_id, artifact_ref_type, artifact_ref_id, position, config)
                       VALUES ($1, $2, $3, $4, $5)""",
                    row["group_id"], item.artifact_ref_type, item.artifact_ref_id, item.position, item.config,
                )
    return _row_to_group(row, body.items)


@router.get("", response_model=WindowGroupListResponse)
async def list_window_groups(
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
            f"SELECT * FROM window_groups WHERE {where} ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, limit, offset,
        )
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM window_groups WHERE {where}", *params,
        )
        groups = []
        for r in rows:
            item_rows = await conn.fetch(
                "SELECT * FROM window_group_items WHERE group_id = $1 ORDER BY position",
                r["group_id"],
            )
            items = [WindowGroupItemModel(
                artifact_ref_type=ir["artifact_ref_type"],
                artifact_ref_id=ir["artifact_ref_id"],
                position=ir["position"],
                config=ir["config"] or {},
            ) for ir in item_rows]
            groups.append(_row_to_group(r, items))
    return WindowGroupListResponse(groups=groups, total=total_row["count"] if total_row else 0)


@router.get("/{group_id}", response_model=WindowGroupResponse)
async def get_window_group(
    group_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get window group with all items."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        group = await _fetch_group(conn, group_id, user_id)
    if not group:
        raise HTTPException(status_code=404, detail="Window group not found")
    return group


@router.put("/{group_id}", response_model=WindowGroupResponse)
async def update_window_group(
    group_id: str,
    body: UpdateWindowGroupRequest,
    user_id: str = Depends(user_dependency),
):
    """Update window group metadata."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT group_id FROM window_groups WHERE group_id = $1 AND user_id = $2",
            group_id, user_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Window group not found")

        updates = []
        params = []
        idx = 1
        if body.name is not None:
            updates.append(f"name = ${idx}"); params.append(body.name); idx += 1
        if body.description is not None:
            updates.append(f"description = ${idx}"); params.append(body.description); idx += 1
        if body.layout_data is not None:
            updates.append(f"layout_data = ${idx}"); params.append(body.layout_data); idx += 1
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updates.append("updated_at = now()")
        params.extend([group_id, user_id])
        await conn.execute(
            f"UPDATE window_groups SET {', '.join(updates)} WHERE group_id = ${idx} AND user_id = ${idx+1}",
            *params,
        )
        return await _fetch_group(conn, group_id, user_id)


@router.delete("/{group_id}", status_code=204)
async def delete_window_group(
    group_id: str,
    user_id: str = Depends(user_dependency),
):
    """Delete a window group and its items (CASCADE)."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM window_groups WHERE group_id = $1 AND user_id = $2",
            group_id, user_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Window group not found")


@router.post("/{group_id}/items", status_code=201)
async def add_item_to_group(
    group_id: str,
    body: WindowGroupItemModel,
    user_id: str = Depends(user_dependency),
):
    """Add an item to a window group."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT group_id FROM window_groups WHERE group_id = $1 AND user_id = $2",
            group_id, user_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Window group not found")

        await conn.execute(
            """INSERT INTO window_group_items (group_id, artifact_ref_type, artifact_ref_id, position, config)
               VALUES ($1, $2, $3, $4, $5)""",
            group_id, body.artifact_ref_type, body.artifact_ref_id, body.position, body.config,
        )
    return {"group_id": group_id, "status": "item_added"}


@router.delete("/{group_id}/items/{item_id}", status_code=204)
async def remove_item_from_group(
    group_id: str,
    item_id: str,
    user_id: str = Depends(user_dependency),
):
    """Remove an item from a window group."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM window_group_items WHERE item_id = $1 AND group_id = $2",
            item_id, group_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_group(row, items: List[WindowGroupItemModel]) -> WindowGroupResponse:
    return WindowGroupResponse(
        group_id=str(row["group_id"]),
        study_id=str(row["study_id"]) if row.get("study_id") else None,
        user_id=row["user_id"],
        name=row["name"],
        description=row.get("description"),
        layout_data=row["layout_data"] or {},
        items=items,
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
