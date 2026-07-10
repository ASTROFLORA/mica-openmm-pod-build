"""App Memory router — P0 product-layer API.

Endpoints:
  GET  /api/v1/app-memory/{app_name}       Get app memory
  PUT  /api/v1/app-memory/{app_name}       Save app memory (auto-version)
  GET  /api/v1/app-memory                   List all app memories
  GET  /api/v1/studies/{id}/app-memories    List app memories for a study
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

router = APIRouter(prefix="/api/v1/app-memory", tags=["app-memory"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SaveAppMemoryRequest(BaseModel):
    memory_data: Dict[str, Any] = Field(..., description="Arbitrary app-specific state blob")
    study_id: Optional[str] = Field(None, description="Optional study scope")

class AppMemoryResponse(BaseModel):
    memory_id: str
    user_id: str
    app_name: str
    study_id: Optional[str] = None
    memory_data: Dict[str, Any]
    version: int
    created_at: str
    updated_at: str

class AppMemoryListResponse(BaseModel):
    memories: List[AppMemoryResponse]

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

@router.get("/{app_name}", response_model=AppMemoryResponse)
async def get_app_memory(
    app_name: str,
    user_id: str = Depends(user_dependency),
    study_id: Optional[str] = Query(None, description="Optional study scope"),
):
    """Get the latest app memory for an app."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT memory_id, user_id, study_id, app_name, memory_data, version,
                      created_at, updated_at
               FROM app_memories
               WHERE user_id = $1 AND app_name = $2 AND (study_id = $3 OR ($3 IS NULL AND study_id IS NULL))
               ORDER BY version DESC LIMIT 1""",
            user_id, app_name, study_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="No memory found for this app")
    return _row_to_memory(row)


@router.put("/{app_name}", response_model=AppMemoryResponse)
async def save_app_memory(
    app_name: str,
    body: SaveAppMemoryRequest,
    user_id: str = Depends(user_dependency),
):
    """Save (or update) app memory. Auto-increments version."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Get current version
        current = await conn.fetchrow(
            """SELECT memory_id, version FROM app_memories
               WHERE user_id = $1 AND app_name = $2 AND (study_id = $3 OR ($3 IS NULL AND study_id IS NULL))
               ORDER BY version DESC LIMIT 1""",
            user_id, app_name, body.study_id,
        )

        if current:
            new_version = current["version"] + 1
            row = await conn.fetchrow(
                """INSERT INTO app_memories (user_id, study_id, app_name, memory_data, version)
                   VALUES ($1, $2, $3, $4::jsonb, $5)
                   ON CONFLICT (user_id, study_id, app_name)
                   DO UPDATE SET memory_data = $4::jsonb, version = $5, updated_at = now()
                   RETURNING memory_id, user_id, study_id, app_name, memory_data, version,
                             created_at, updated_at""",
                user_id, body.study_id, app_name,
                json.dumps(body.memory_data) if body.memory_data else '{}',
                new_version,
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO app_memories (user_id, study_id, app_name, memory_data, version)
                   VALUES ($1, $2, $3, $4::jsonb, 1)
                   RETURNING memory_id, user_id, study_id, app_name, memory_data, version,
                             created_at, updated_at""",
                user_id, body.study_id, app_name,
                json.dumps(body.memory_data) if body.memory_data else '{}',
            )
    return _row_to_memory(row)


@router.get("", response_model=AppMemoryListResponse)
async def list_app_memories(
    user_id: str = Depends(user_dependency),
    study_id: Optional[str] = Query(None),
):
    """List all app memories for user, optionally scoped to a study."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if study_id:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (app_name) memory_id, user_id, study_id, app_name, memory_data, version,
                          created_at, updated_at
                   FROM app_memories
                   WHERE user_id = $1 AND study_id = $2
                   ORDER BY app_name, version DESC""",
                user_id, study_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (app_name) memory_id, user_id, study_id, app_name, memory_data, version,
                          created_at, updated_at
                   FROM app_memories
                   WHERE user_id = $1
                   ORDER BY app_name, version DESC""",
                user_id,
            )
    return AppMemoryListResponse(memories=[_row_to_memory(r) for r in rows])


@router.get("/study/{study_id}", response_model=AppMemoryListResponse)
async def list_study_app_memories(
    study_id: str,
    user_id: str = Depends(user_dependency),
):
    """Alias for GET /app-memory?study_id=..."""
    return await list_app_memories(user_id=user_id, study_id=study_id)


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

def _row_to_memory(row) -> AppMemoryResponse:
    study_id = row.get("study_id")
    return AppMemoryResponse(
        memory_id=str(row["memory_id"]),
        user_id=row["user_id"],
        app_name=row["app_name"],
        study_id=str(study_id) if study_id is not None else None,
        memory_data=_parse_jsonb(row.get("memory_data"), {}),
        version=row["version"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
