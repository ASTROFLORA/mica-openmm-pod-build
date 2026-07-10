"""Search Intent router — P1 product-layer API.

Endpoints:
  POST /api/v1/library/search/intent         Create structured search intent
  GET  /api/v1/library/search/intent/{id}    Get intent status + results
  GET  /api/v1/library/search/intents        List user's search intents
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

router = APIRouter(prefix="/api/v1/library/search", tags=["search-intent"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SearchFilters(BaseModel):
    sources: List[str] = Field(default_factory=list)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    organisms: List[str] = Field(default_factory=list)
    evidence_types: List[str] = Field(default_factory=list)
    domains: List[str] = Field(default_factory=list)
    structures: bool = False
    claims: bool = False

class CreateSearchIntentRequest(BaseModel):
    query: str = Field(default="", min_length=0, max_length=2000)
    query_text: Optional[str] = Field(None, min_length=1, max_length=2000, description="Alias for 'query'")
    intent_type: str = Field("free_text", description="literature, structure, entity, multimodal, free_text")
    filters: SearchFilters = Field(default_factory=SearchFilters)
    desired_output: Optional[str] = Field(None, description="paper_list, structure_viewer, claim_graph, entity_network, report")
    save: bool = Field(True)

class SearchIntentResponse(BaseModel):
    intent_id: str
    user_id: str
    query_text: str
    intent_type: str
    filters: Dict[str, Any]
    desired_output: Optional[str] = None
    status: str
    result_count: Optional[int] = None
    result_snapshot: Optional[Dict[str, Any]] = None
    created_at: str
    completed_at: Optional[str] = None

class SearchIntentListResponse(BaseModel):
    intents: List[SearchIntentResponse]
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

@router.post("/intent", response_model=SearchIntentResponse, status_code=202)
async def create_search_intent(
    body: CreateSearchIntentRequest,
    user_id: str = Depends(user_dependency),
):
    """Create a structured search intent. Runs async; poll for results."""
    await ensure_product_schema()
    # Accept 'query_text' as alias for 'query' (canonical contract alignment)
    query = body.query or body.query_text or ""
    if not query:
        raise HTTPException(status_code=422, detail="query or query_text is required")
    pool = await _get_pool()
    async with pool.acquire() as conn:
        filters_dict = body.filters.model_dump() if hasattr(body.filters, 'model_dump') else body.filters.dict()
        row = await conn.fetchrow(
            """INSERT INTO search_intents (user_id, query_text, intent_type, filters, desired_output, status)
               VALUES ($1, $2, $3, $4::jsonb, $5, 'pending')
               RETURNING intent_id, user_id, query_text, intent_type, filters, desired_output, status, created_at, completed_at, result_count, result_snapshot""",
            user_id, query, body.intent_type,
            json.dumps(filters_dict) if filters_dict else '{}',
            body.desired_output,
        )
    # In a full implementation, enqueue to Redis/Bibliotecario for async processing.
    # For now, mark as completed with placeholder.
    # TODO: Integrate with Bibliotecario entity scan or LibrarySearchFacade for async dispatch.
    return _row_to_intent(row)


@router.get("/intents", response_model=SearchIntentListResponse)
async def list_search_intents(
    user_id: str = Depends(user_dependency),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List user's search intents."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM search_intents WHERE user_id = $1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            user_id, limit, offset,
        )
        total_row = await conn.fetchrow(
            "SELECT COUNT(*) FROM search_intents WHERE user_id = $1", user_id,
        )
    return SearchIntentListResponse(
        intents=[_row_to_intent(r) for r in rows],
        total=total_row["count"] if total_row else 0,
    )


@router.get("/intent/{intent_id}", response_model=SearchIntentResponse)
async def get_search_intent(
    intent_id: str,
    user_id: str = Depends(user_dependency),
):
    """Get search intent status and results."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM search_intents WHERE intent_id = $1 AND user_id = $2",
            intent_id, user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Search intent not found")
    return _row_to_intent(row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class SearchResultItem(BaseModel):
    result_id: str
    source_lane: Optional[str] = None
    rank: Optional[int] = None
    score: Optional[float] = None
    artifact_id: Optional[str] = None
    provider: Optional[str] = None
    result_payload: Dict[str, Any] = Field(default_factory=dict)
    can_ingest: bool = False
    indexed_in_kb: bool = False
    created_at: str

class SearchResultsResponse(BaseModel):
    intent_id: str
    status: str
    results: List[SearchResultItem] = Field(default_factory=list)
    total: int = 0


@router.get("/intent/{intent_id}/results", response_model=SearchResultsResponse)
async def get_search_intent_results(
    intent_id: str,
    user_id: str = Depends(user_dependency),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get search results. If dispatch not ready, returns typed pending state."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        intent = await conn.fetchrow(
            "SELECT intent_id, status FROM search_intents WHERE intent_id = $1 AND user_id = $2",
            intent_id, user_id,
        )
        if not intent:
            raise HTTPException(status_code=404, detail="Search intent not found")

        # If still pending and no results, return typed pending
        if intent["status"] == "pending":
            return SearchResultsResponse(intent_id=intent_id, status="pending", results=[], total=0)

        rows = await conn.fetch(
            "SELECT * FROM search_intent_results WHERE intent_id = $1 ORDER BY rank ASC LIMIT $2 OFFSET $3",
            intent_id, limit, offset,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM search_intent_results WHERE intent_id = $1", intent_id,
        )
    return SearchResultsResponse(
        intent_id=intent_id,
        status=intent["status"],
        results=[SearchResultItem(
            result_id=str(r["result_id"]), source_lane=r.get("source_lane"), rank=r.get("rank"),
            score=float(r["score"]) if r.get("score") else None,
            artifact_id=str(r["artifact_id"]) if r.get("artifact_id") else None,
            provider=r.get("provider"), result_payload=r.get("result_payload") or {},
            can_ingest=bool(r.get("can_ingest", False)), indexed_in_kb=bool(r.get("indexed_in_kb", False)),
            created_at=r["created_at"].isoformat() if r.get("created_at") else "",
        ) for r in rows],
        total=total or 0,
    )


def _parse_jsonb(raw, default=None):
    if raw is None: return default
    if isinstance(raw, (dict, list)): return raw
    if isinstance(raw, str):
        try: return json.loads(raw)
        except (json.JSONDecodeError, TypeError): return default
    return default

def _row_to_intent(row) -> SearchIntentResponse:
    return SearchIntentResponse(
        intent_id=str(row["intent_id"]),
        user_id=row["user_id"],
        query_text=row["query_text"],
        intent_type=row["intent_type"],
        filters=_parse_jsonb(row.get("filters"), {}),
        desired_output=row.get("desired_output"),
        status=row["status"],
        result_count=row.get("result_count"),
        result_snapshot=_parse_jsonb(row.get("result_snapshot")),
        created_at=row["created_at"].isoformat(),
        completed_at=row["completed_at"].isoformat() if row.get("completed_at") else None,
    )
