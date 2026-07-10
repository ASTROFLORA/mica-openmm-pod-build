"""Feed self-review trigger — Phase 1 (token gate + Redis enqueue).

POST /api/v1/feed/review-trigger

Receives a webhook from the MICA agent feed MCP every time an agent calls
``publish_cue``. The endpoint is thin by design:

1. Validate ``X-Internal-Token`` (constant-time HMAC).
2. Normalise the payload into a ``feed_review`` job envelope.
3. Enqueue on ``mica:queue:research`` via ``RedisJobStore``.
4. Return job_id immediately — Driver review runs in the worker.

Phase 2 will add the "sleep" consolidation endpoint here too.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.auth_internal import verify_internal_token
from mica.infrastructure.redis_client import get_redis
from mica.worker.job_store import RedisJobStore

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_feed_review_payload(payload: FeedReviewTriggerRequest, *, requested_by: str | None = None) -> Dict[str, Any]:
    envelope_payload: Dict[str, Any] = {
        "task_type": "feed_review",
        "post_id": payload.post_id,
        "post_type": payload.post_type,
        "agent_id": payload.agent_id,
        "content": payload.content,
        "topic": payload.topic,
        "session_id": payload.session_id,
        "metadata": dict(payload.metadata),
    }
    if requested_by:
        envelope_payload["metadata"] = {
            **envelope_payload["metadata"],
            "requested_by_user_id": requested_by,
        }
    return envelope_payload


def _build_feed_sleep_payload(payload: SleepConsolidationRequest, *, requested_by: str | None = None) -> Dict[str, Any]:
    envelope_payload: Dict[str, Any] = {
        "task_type": "feed_sleep",
        "posts": [p.model_dump() for p in payload.posts],
        "since": payload.since,
        "session_id": payload.session_id,
        "post_count": len(payload.posts),
    }
    if requested_by:
        envelope_payload["requested_by_user_id"] = requested_by
    return envelope_payload


async def _get_feed_store() -> RedisJobStore:
    try:
        redis_client = await get_redis()
    except Exception as exc:
        logger.exception("Redis unavailable for feed router")
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc}") from exc
    return RedisJobStore(redis_client)


async def _enqueue_feed_job(
    *,
    payload: Dict[str, Any],
    user_id: str,
) -> tuple[str, str]:
    job_id = str(uuid.uuid4())
    store = await _get_feed_store()
    queue = "mica:queue:research"
    try:
        await store.enqueue(
            job_id=job_id,
            lane="research",
            payload=payload,
            user_id=user_id,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue %s job %s", payload.get("task_type"), job_id)
        await store.set_error(job_id, f"enqueue failed: {exc}")
        raise HTTPException(status_code=503, detail=f"Enqueue failed: {exc}") from exc
    return job_id, queue


def _build_feed_job_status_response(job_id: str, record: Dict[str, Any]) -> "FeedJobStatusResponse":
    result_raw = record.get("result")
    result_val: Optional[Dict[str, Any]] = result_raw if isinstance(result_raw, dict) else None
    error_val = record.get("error") or None
    return FeedJobStatusResponse(
        job_id=job_id,
        status=record.get("status", "unknown"),
        task_type=record.get("task_type") or record.get("payload", {}).get("task_type"),
        result=result_val,
        error=error_val,
    )


class FeedReviewTriggerRequest(BaseModel):
    post_id: str = Field(..., description="Original feed post id from publish_cue")
    post_type: str = Field(..., description="cue | decision | hypothesis | insight | ...")
    agent_id: str = Field(..., description="Agent that published the post")
    content: str = Field(..., description="Post content (truncatable upstream)")
    topic: Optional[str] = Field(None, description="Optional feed topic tag")
    session_id: Optional[str] = Field(None, description="Agent session id if available")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Free-form passthrough")


class FeedReviewTriggerResponse(BaseModel):
    status: str
    job_id: str
    queue: str
    task_type: str


@router.post(
    "/review-trigger",
    response_model=FeedReviewTriggerResponse,
    dependencies=[Depends(verify_internal_token)],
    summary="Enqueue a feed-post self-review job for the AgenticDriver",
)
async def trigger_feed_review(payload: FeedReviewTriggerRequest) -> FeedReviewTriggerResponse:
    """Validate token + enqueue feed_review job on mica:queue:research."""
    envelope_payload = _build_feed_review_payload(payload)
    job_id, queue = await _enqueue_feed_job(
        payload=envelope_payload,
        user_id=payload.agent_id or "mica-feed",
    )

    logger.info(
        "feed_review enqueued job_id=%s agent=%s post_type=%s post_id=%s",
        job_id,
        payload.agent_id,
        payload.post_type,
        payload.post_id,
    )

    return FeedReviewTriggerResponse(
        status="queued",
        job_id=job_id,
        queue=queue,
        task_type="feed_review",
    )


@router.post(
    "/user/review-request",
    response_model=FeedReviewTriggerResponse,
    summary="Enqueue a user-authenticated feed review job for the AgenticDriver",
)
async def trigger_feed_review_for_user(
    payload: FeedReviewTriggerRequest,
    user_id: str = Depends(user_dependency),
) -> FeedReviewTriggerResponse:
    """Bearer-authenticated facade for feed_review jobs used by frontends like Alejandria."""
    envelope_payload = _build_feed_review_payload(payload, requested_by=user_id)
    job_id, queue = await _enqueue_feed_job(payload=envelope_payload, user_id=user_id)

    logger.info(
        "feed_review user facade enqueued job_id=%s requester=%s agent=%s post_type=%s post_id=%s",
        job_id,
        user_id,
        payload.agent_id,
        payload.post_type,
        payload.post_id,
    )

    return FeedReviewTriggerResponse(
        status="queued",
        job_id=job_id,
        queue=queue,
        task_type="feed_review",
    )


# ---------------------------------------------------------------------------
# Phase 2: Sleep-consolidation endpoint
# Caller (VS Code agent) scrolls the local feed JSONL via MCP tools and
# forwards the posts here.  Worker synthesises them with AgenticDriver.
# ---------------------------------------------------------------------------


class FeedPost(BaseModel):
    post_id: str = Field(..., description="Feed post unique id")
    post_type: str = Field(..., description="cue | decision | insight | hypothesis | ...")
    agent_id: str = Field(..., description="Author agent id")
    content: str = Field(..., description="Post body text")
    topic: Optional[str] = Field(None)
    timestamp: Optional[str] = Field(None, description="ISO 8601 timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SleepConsolidationRequest(BaseModel):
    posts: List[FeedPost] = Field(
        ...,
        description=(
            "Feed posts to synthesise (caller scrolls .mica/agent_feed/feed.jsonl "
            "via MCP tools and passes posts here — worker cannot access the local file)"
        ),
        min_length=1,
    )
    since: Optional[str] = Field(None, description="ISO timestamp lower bound (informational)")
    session_id: Optional[str] = Field(None, description="Caller session id")


class SleepConsolidationResponse(BaseModel):
    status: str
    job_id: str
    queue: str
    task_type: str
    post_count: int


@router.post(
    "/sleep-consolidation",
    response_model=SleepConsolidationResponse,
    dependencies=[Depends(verify_internal_token)],
    summary="Synthesise a batch of feed posts into a memory cue via AgenticDriver",
)
async def trigger_sleep_consolidation(
    payload: SleepConsolidationRequest,
) -> SleepConsolidationResponse:
    """Validate token + enqueue feed_sleep job on mica:queue:research."""
    envelope_payload = _build_feed_sleep_payload(payload)
    job_id, queue = await _enqueue_feed_job(
        payload=envelope_payload,
        user_id="mica-feed-sleep",
    )

    logger.info(
        "feed_sleep enqueued job_id=%s post_count=%d since=%s",
        job_id,
        len(payload.posts),
        payload.since,
    )

    return SleepConsolidationResponse(
        status="queued",
        job_id=job_id,
        queue=queue,
        task_type="feed_sleep",
        post_count=len(payload.posts),
    )


@router.post(
    "/user/sleep-consolidation",
    response_model=SleepConsolidationResponse,
    summary="Enqueue a user-authenticated feed sleep consolidation job",
)
async def trigger_sleep_consolidation_for_user(
    payload: SleepConsolidationRequest,
    user_id: str = Depends(user_dependency),
) -> SleepConsolidationResponse:
    """Bearer-authenticated facade for feed_sleep jobs used by frontends like Alejandria."""
    envelope_payload = _build_feed_sleep_payload(payload, requested_by=user_id)
    job_id, queue = await _enqueue_feed_job(payload=envelope_payload, user_id=user_id)

    logger.info(
        "feed_sleep user facade enqueued job_id=%s requester=%s post_count=%d since=%s",
        job_id,
        user_id,
        len(payload.posts),
        payload.since,
    )

    return SleepConsolidationResponse(
        status="queued",
        job_id=job_id,
        queue=queue,
        task_type="feed_sleep",
        post_count=len(payload.posts),
    )


# ---------------------------------------------------------------------------
# Job poll endpoint (internal-token protected, RedisJobStore backed)
# GET /api/v1/feed/jobs/{job_id}
# Used by feed_caller.py to poll job completion without Clerk auth.
# ---------------------------------------------------------------------------


class FeedJobStatusResponse(BaseModel):
    job_id: str
    status: str  # queued | running | done | error
    task_type: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@router.get(
    "/jobs/{job_id}",
    response_model=FeedJobStatusResponse,
    dependencies=[Depends(verify_internal_token)],
    summary="Poll a feed job by id (internal-token protected, Redis-backed)",
)
async def get_feed_job(job_id: str) -> FeedJobStatusResponse:
    """Return current status and result of a feed_review or feed_sleep job."""
    store = await _get_feed_store()
    record = await store.get(job_id)

    if record is None:
        raise HTTPException(status_code=404, detail=f"Feed job {job_id} not found")

    return _build_feed_job_status_response(job_id, record)



@router.get(
    "/user/jobs/{job_id}",
    response_model=FeedJobStatusResponse,
    summary="Poll a user-authenticated feed job by id",
)
async def get_feed_job_for_user(
    job_id: str,
    user_id: str = Depends(user_dependency),
) -> FeedJobStatusResponse:
    """Return current status/result for a feed job owned by the authenticated user."""
    store = await _get_feed_store()
    record = await store.get(job_id)

    if record is None:
        raise HTTPException(status_code=404, detail=f"Feed job {job_id} not found")
    if str(record.get("user_id") or "") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return _build_feed_job_status_response(job_id, record)
