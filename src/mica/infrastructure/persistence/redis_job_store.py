"""
redis_job_store.py — Redis-backed job persistence implementing JobStoreABC.

Stores Job records as JSON hashes in Redis with optional TTL.
Key schema:
    mica:jobs:{job_id}        — HASH with job fields
    mica:jobs:index:user:{uid} — SET of job_ids belonging to user
    mica:jobs:index:all        — SORTED SET (score = created_at epoch) of all job_ids

Phase R2 · 2026-03-15
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore

from .timescale_job_store import JobStoreABC

logger = logging.getLogger("mica.infrastructure.persistence.redis_job_store")

# FIX-06: Configurable TTL for completed/failed jobs (default 7 days).
_COMPLETED_TTL_SEC = int(os.getenv("MICA_JOB_TTL_HOURS", "168")) * 3600
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_KEY_PREFIX = "mica:jobs"
_ALL_INDEX = f"{_KEY_PREFIX}:index:all"


def _user_index(user_id: str) -> str:
    return f"{_KEY_PREFIX}:index:user:{user_id}"


def _job_key(job_id: str) -> str:
    return f"{_KEY_PREFIX}:{job_id}"


class RedisJobStore(JobStoreABC):
    """
    Redis-backed durable job store.

    Parameters
    ----------
    redis_client : redis.asyncio.Redis
        A pre-configured async Redis connection (from ``get_redis()``).
    ttl_completed : int
        Seconds after which terminal jobs expire (default 7 days).
    """

    def __init__(
        self,
        redis_client: "aioredis.Redis",  # type: ignore[name-defined]
        ttl_completed: int = _COMPLETED_TTL_SEC,
    ) -> None:
        if aioredis is None:
            raise RuntimeError("redis package not installed")
        self._r = redis_client
        self._ttl = ttl_completed

    # ------------------------------------------------------------------
    # JobStoreABC implementation
    # ------------------------------------------------------------------

    async def save_job(self, job: Any) -> None:
        """Persist or update a job (expects a Job dataclass with `.spec`)."""
        job_dict = job.to_dict() if hasattr(job, "to_dict") else asdict(job)  # type: ignore[arg-type]
        job_id: str = job_dict.get("job_id") or job.spec.job_id
        user_id: str = (
            job_dict.get("user_id")
            or (job.spec.user_id if hasattr(job, "spec") else "")
            or "system"
        )
        status: str = job_dict.get("status", "")
        created_epoch = time.time()
        if hasattr(job, "created_at") and job.created_at:
            created_epoch = job.created_at.timestamp()

        key = _job_key(job_id)
        payload = json.dumps(job_dict)

        pipe = self._r.pipeline(transaction=True)
        pipe.set(key, payload)
        # Indexes
        pipe.zadd(_ALL_INDEX, {job_id: created_epoch})
        if user_id:
            pipe.sadd(_user_index(user_id), job_id)
        # TTL for terminal jobs
        if status in _TERMINAL_STATUSES and self._ttl > 0:
            pipe.expire(key, self._ttl)
        await pipe.execute()
        logger.debug("Saved job %s (status=%s)", job_id, status)

    async def load_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Load a job by ID."""
        raw = await self._r.get(_job_key(job_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def load_all_jobs(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load all jobs, optionally filtered by user.  Most recent first."""
        if user_id:
            job_ids = list(await self._r.smembers(_user_index(user_id)))
        else:
            # Newest first — ZREVRANGE
            job_ids = await self._r.zrevrange(_ALL_INDEX, 0, 999)

        if not job_ids:
            return []

        # MGET all payloads
        keys = [_job_key(jid) for jid in job_ids]
        raws = await self._r.mget(*keys)
        results: list[Dict[str, Any]] = []
        for raw in raws:
            if raw is not None:
                results.append(json.loads(raw))
        return results

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job and remove from indexes."""
        key = _job_key(job_id)
        raw = await self._r.get(key)
        if raw is None:
            return False

        # Try to extract user_id for index cleanup
        try:
            data = json.loads(raw)
            uid = data.get("user_id", "")
        except Exception:
            uid = ""

        pipe = self._r.pipeline(transaction=True)
        pipe.delete(key)
        pipe.zrem(_ALL_INDEX, job_id)
        if uid:
            pipe.srem(_user_index(uid), job_id)
        await pipe.execute()
        logger.debug("Deleted job %s", job_id)
        return True

    # ------------------------------------------------------------------
    # Extra: load active jobs (for recovery on restart)
    # ------------------------------------------------------------------

    async def load_active_jobs(self) -> List[Dict[str, Any]]:
        """Load jobs not in terminal state (for startup recovery)."""
        all_jobs = await self.load_all_jobs()
        return [j for j in all_jobs if j.get("status") not in _TERMINAL_STATUSES]
