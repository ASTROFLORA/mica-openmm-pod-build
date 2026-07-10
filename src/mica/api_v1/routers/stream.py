"""
SSE streaming endpoint for MICA task results.

Subscribes to Redis Pub/Sub channel "mica:events:{task_id}"
and re-streams events as Server-Sent Events to the client.

Routes:
    POST /api/v1/query                    → enqueue task, return task_id
    GET  /api/v1/query/{task_id}/stream   → SSE stream
    GET  /api/v1/query/{task_id}/status   → current status

See: MICAV4DOCS/PRODUCTION_MICROSERVICES_2026.md §2.3
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from mica.api_v1.auth import user_dependency as _user_dependency
from mica.infrastructure.redis_client import get_redis

logger = logging.getLogger("mica.api.stream")

TASK_QUEUE_KEY = "mica:tasks"

router = APIRouter(prefix="/api/v1", tags=["query"])


# ──────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────


class QueryRequest(BaseModel):
    query: str
    provider_id: str = "openai"
    session_id: str | None = None


class QueryResponse(BaseModel):
    task_id: str
    status: str = "queued"
    stream_url: str


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────


@router.post("/query", response_model=QueryResponse)
async def enqueue_query(
    body: QueryRequest,
    user_id: str = Depends(_user_dependency),
) -> QueryResponse:
    """Enqueue a driver task and return a task_id for SSE streaming."""
    task_id = str(uuid.uuid4())
    # FIX-03: Use shared Redis singleton instead of per-request connection
    redis_client = await get_redis()

    task_payload = {
        "task_id": task_id,
        "query": body.query,
        "provider_id": body.provider_id,
        "session_id": body.session_id or "",
        # user_id is required for per-user GCS bucket + checkpoint dir scoping.
        "user_id": user_id,
    }
    await redis_client.lpush(TASK_QUEUE_KEY, json.dumps(task_payload))
    await redis_client.set(f"mica:status:{task_id}", "queued", ex=3600)

    return QueryResponse(
        task_id=task_id,
        status="queued",
        stream_url=f"/api/v1/query/{task_id}/stream",
    )


@router.get("/query/{task_id}/stream")
async def stream_query(task_id: str, request: Request, _user: str = Depends(_user_dependency)) -> EventSourceResponse:
    """
    SSE endpoint.

    Subscribes to Redis "mica:events:{task_id}" and forwards
    each published JSON event as an SSE message.

    The stream ends when:
      - mica:status:{task_id} == "done" or "error"
      - Client disconnects
      - LoopFinish event is received
    """

    async def _generate() -> AsyncGenerator[str, None]:
        # FIX-03: Use shared Redis singleton
        redis_client = await get_redis()
        pubsub = redis_client.pubsub()
        channel = f"mica:events:{task_id}"

        # Verify task exists
        status = await redis_client.get(f"mica:status:{task_id}")
        if status is None:
            yield json.dumps({"type": "Error", "message": f"Task {task_id} not found"})
            await redis_client.aclose()
            return

        await pubsub.subscribe(channel)
        logger.info(f"SSE stream opened for task {task_id[:8]}")

        try:
            async for message in pubsub.listen():
                if await request.is_disconnected():
                    logger.info(f"Client disconnected from task {task_id[:8]}")
                    break

                if message["type"] != "message":
                    continue

                data: str = message["data"]
                yield data

                # Check if terminal event
                try:
                    event = json.loads(data)
                    if event.get("type") in ("LoopFinish", "Error"):
                        break
                except json.JSONDecodeError:
                    pass

                # Also check via status key (belt-and-suspenders)
                current_status = await redis_client.get(f"mica:status:{task_id}")
                if current_status in ("done", "error"):
                    # Emit a final LoopFinish if not already done
                    if not (event.get("type") in ("LoopFinish", "Error")):
                        yield json.dumps({"type": "LoopFinish", "via": "status_key"})
                    break

        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            # Don't close shared singleton; just clean up pubsub
            logger.info(f"SSE stream closed for task {task_id[:8]}")

    return EventSourceResponse(_generate())


@router.get("/query/{task_id}/status")
async def get_task_status(task_id: str, _user: str = Depends(_user_dependency)) -> JSONResponse:
    """Return current task status (queued|running|done|error)."""
    # FIX-03: Use shared Redis singleton
    redis_client = await get_redis()
    status = await redis_client.get(f"mica:status:{task_id}")
    if status is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return JSONResponse({"task_id": task_id, "status": status})
