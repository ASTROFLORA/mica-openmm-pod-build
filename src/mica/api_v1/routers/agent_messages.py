"""Agent Messages Router — Inter-agent collaboration surface.

POST   /api/v1/agent-messages         Publish typed agent message
GET    /api/v1/agent-messages         Scroll/poll messages by session/protocol_run
GET    /api/v1/agent-messages/{mid}   Get single message
POST   /api/v1/agent-messages/stream  Subscribe to message stream (SSE)

Service-token auth (X-Internal-Token + X-User-Id) is supported alongside Clerk.
Messages are persisted in TimescaleDB agent_messages hypertable.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-messages", tags=["agent-messages"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AgentMessagePublishRequest(BaseModel):
    session_id: str = Field("")
    protocol_run_id: Optional[str] = None
    study_id: Optional[str] = None
    working_set_id: Optional[str] = None
    from_agent: str = Field(..., min_length=1, max_length=50)
    to_agent: str = Field("broadcast", max_length=50)
    message_type: str = Field(..., min_length=1, max_length=50)
    summary: str = Field("", max_length=500)
    resource_refs: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    manifest_uri: Optional[str] = None
    snippet_uri: Optional[str] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    trace_id: str = Field("")
    parent_message_id: Optional[str] = None

class AgentMessageResponse(BaseModel):
    message_id: str
    session_id: str
    from_agent: str
    to_agent: str
    message_type: str
    summary: str
    resource_refs: List[str]
    artifact_refs: List[str]
    manifest_uri: Optional[str] = None
    snippet_uri: Optional[str] = None
    confidence: float
    parent_message_id: Optional[str] = None
    trace_id: str
    created_at: str

class AgentMessageListResponse(BaseModel):
    messages: List[AgentMessageResponse]
    total: int

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _resolve_identity(
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Resolve caller identity: internal token or Clerk JWT."""
    # Internal token path
    if x_internal_token:
        expected = os.getenv("MICA_INTERNAL_TOKEN", "")
        if expected:
            import hmac
            if not hmac.compare_digest(x_internal_token.strip(), expected.strip()):
                raise HTTPException(status_code=401, detail="Invalid internal token")
        return (x_user_id or "agent_service").strip()

    # Clerk JWT path — reuse existing auth module
    if authorization:
        try:
            from mica.api_v1.auth import resolve_user_id
            return resolve_user_id(x_user_id=x_user_id, authorization=authorization, transport="http")
        except Exception:
            pass

    raise HTTPException(status_code=401, detail="Authentication required")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_POOL = None

async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    import asyncpg
    from mica.infrastructure.persistence.pg_async import choose_neon_database_url, asyncpg_connection_kwargs_for_database_url
    dsn = choose_neon_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="Database not configured")
    _POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn))
    return _POOL

async def _ensure_table():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_messages (
                message_id      TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL DEFAULT '',
                protocol_run_id TEXT,
                study_id        TEXT,
                working_set_id  TEXT,
                from_agent      TEXT NOT NULL,
                to_agent        TEXT NOT NULL DEFAULT 'broadcast',
                message_type    TEXT NOT NULL,
                summary         TEXT NOT NULL DEFAULT '',
                resource_refs   JSONB NOT NULL DEFAULT '[]',
                artifact_refs   JSONB NOT NULL DEFAULT '[]',
                manifest_uri    TEXT,
                snippet_uri     TEXT,
                confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                parent_message_id TEXT,
                trace_id        TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                metadata        JSONB NOT NULL DEFAULT '{}'
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_messages_session
            ON agent_messages (session_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_messages_to_agent
            ON agent_messages (to_agent, created_at DESC)
        """)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=AgentMessageResponse, status_code=201)
async def publish_agent_message(
    body: AgentMessagePublishRequest,
    user_id: str = Depends(_resolve_identity),
):
    """Publish a typed agent message to the inter-agent bus."""
    try:
        await _ensure_table()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB schema unavailable: {e}")
    
    mid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_messages (
                    message_id, session_id, protocol_run_id, study_id, working_set_id,
                    from_agent, to_agent, message_type, summary,
                    resource_refs, artifact_refs, manifest_uri, snippet_uri,
                    confidence, parent_message_id, trace_id, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
                mid, body.session_id, body.protocol_run_id, body.study_id, body.working_set_id,
                body.from_agent, body.to_agent, body.message_type, body.summary[:500],
                json.dumps(body.resource_refs), json.dumps(body.artifact_refs),
                body.manifest_uri, body.snippet_uri,
                body.confidence, body.parent_message_id, body.trace_id or mid, now,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB write failed: {str(e)[:200]}")


    return AgentMessageResponse(
        message_id=mid, session_id=body.session_id,
        from_agent=body.from_agent, to_agent=body.to_agent,
        message_type=body.message_type, summary=body.summary[:500],
        resource_refs=body.resource_refs, artifact_refs=body.artifact_refs,
        manifest_uri=body.manifest_uri, snippet_uri=body.snippet_uri,
        confidence=body.confidence, parent_message_id=body.parent_message_id,
        trace_id=body.trace_id or mid, created_at=now_iso,
    )


@router.get("", response_model=AgentMessageListResponse)
async def scroll_agent_messages(
    user_id: str = Depends(_resolve_identity),
    session_id: str = Query(""),
    protocol_run_id: Optional[str] = Query(None),
    to_agent: Optional[str] = Query(None),
    message_type: Optional[str] = Query(None),
    topic: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Scroll/poll agent messages. Filter by session, target agent, message type."""
    await _ensure_table()
    pool = await _get_pool()

    conditions = ["1=1"]
    params: list = []
    idx = 1

    if session_id:
        conditions.append(f"session_id = ${idx}")
        params.append(session_id); idx += 1
    if protocol_run_id:
        conditions.append(f"protocol_run_id = ${idx}")
        params.append(protocol_run_id); idx += 1
    if to_agent:
        conditions.append(f"to_agent IN ('broadcast', ${idx})")
        params.append(to_agent); idx += 1
    if message_type:
        conditions.append(f"message_type = ${idx}")
        params.append(message_type); idx += 1

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM agent_messages WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, limit, offset,
        )
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM agent_messages WHERE {where}", *params,
        )

    messages = [_row_to_message(r) for r in rows]
    return AgentMessageListResponse(messages=messages, total=total_row["count"] if total_row else 0)


@router.get("/{message_id}", response_model=AgentMessageResponse)
async def get_agent_message(
    message_id: str,
    user_id: str = Depends(_resolve_identity),
):
    """Get a single agent message by ID."""
    await _ensure_table()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_messages WHERE message_id = $1", message_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    return _row_to_message(row)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _row_to_message(row) -> AgentMessageResponse:
    return AgentMessageResponse(
        message_id=row["message_id"],
        session_id=row["session_id"] or "",
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        message_type=row["message_type"],
        summary=row["summary"] or "",
        resource_refs=json.loads(row["resource_refs"]) if isinstance(row["resource_refs"], str) else (row["resource_refs"] or []),
        artifact_refs=json.loads(row["artifact_refs"]) if isinstance(row["artifact_refs"], str) else (row["artifact_refs"] or []),
        manifest_uri=row["manifest_uri"],
        snippet_uri=row["snippet_uri"],
        confidence=float(row["confidence"] or 0.0),
        parent_message_id=row["parent_message_id"],
        trace_id=row["trace_id"] or "",
        created_at=row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
    )
