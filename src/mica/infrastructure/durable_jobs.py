"""durable_jobs.py — Slice-4 §3. Redis-backed durable job store.

Replaces in-memory dicts behind ``BackgroundTasks.add_task`` so submitted
jobs survive API restarts and can be observed across replicas.

Storage layout (per ``namespace``):
  mica:jobs:<ns>:<job_id>            HASH   {status, payload, detail, ts_*}
  mica:jobs:<ns>:index               ZSET   created_at → job_id
  mica:jobs:queue:<ns>               LIST   FIFO of job_ids waiting to be picked
                                            up by ``durable_worker``.

All values are JSON-encoded strings.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ── module-level constants ────────────────────────────────────────────
DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 days
KNOWN_STATUSES = frozenset({
    "queued", "running", "succeeded", "failed", "cancelled", "timed_out",
})


@dataclass
class JobRecord:
    job_id: str
    namespace: str
    status: str
    payload: Dict[str, Any]
    detail: Optional[str]
    ts_created: float
    ts_updated: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "namespace": self.namespace,
            "status": self.status,
            "payload": self.payload,
            "detail": self.detail,
            "ts_created": self.ts_created,
            "ts_updated": self.ts_updated,
        }


class DurableJobStore:
    """Async wrapper over redis.asyncio for durable job tracking."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        *,
        namespace: str = "default",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.namespace = namespace
        self._ttl = int(ttl_seconds)
        self._redis: Any = None

    # ── lifecycle ──────────────────────────────────────────────────────
    async def connect(self) -> None:
        if self._redis is not None:
            return
        try:
            import redis.asyncio as redis_async  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("redis package required: pip install redis") from exc
        self._redis = redis_async.from_url(
            self._redis_url, encoding="utf-8", decode_responses=True,
        )
        await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    # ── keys ───────────────────────────────────────────────────────────
    def _key(self, job_id: str) -> str:
        return f"mica:jobs:{self.namespace}:{job_id}"

    def _index_key(self) -> str:
        return f"mica:jobs:{self.namespace}:index"

    def _queue_key(self) -> str:
        return f"mica:jobs:queue:{self.namespace}"

    # ── operations ─────────────────────────────────────────────────────
    async def create(
        self,
        job_id: str,
        payload: Dict[str, Any],
        *,
        enqueue: bool = True,
        initial_status: str = "queued",
    ) -> JobRecord:
        if initial_status not in KNOWN_STATUSES:
            raise ValueError(f"unknown status: {initial_status}")
        await self.connect()
        now = time.time()
        rec = JobRecord(
            job_id=job_id,
            namespace=self.namespace,
            status=initial_status,
            payload=payload,
            detail=None,
            ts_created=now,
            ts_updated=now,
        )
        pipe = self._redis.pipeline()
        pipe.hset(self._key(job_id), mapping={
            "status": rec.status,
            "payload": json.dumps(payload, default=str),
            "detail": "",
            "ts_created": str(now),
            "ts_updated": str(now),
        })
        pipe.expire(self._key(job_id), self._ttl)
        pipe.zadd(self._index_key(), {job_id: now})
        if enqueue:
            pipe.rpush(self._queue_key(), job_id)
        await pipe.execute()
        return rec

    async def update_status(
        self,
        job_id: str,
        status: str,
        detail: Optional[str] = None,
    ) -> bool:
        if status not in KNOWN_STATUSES:
            raise ValueError(f"unknown status: {status}")
        await self.connect()
        if not await self._redis.exists(self._key(job_id)):
            return False
        pipe = self._redis.pipeline()
        pipe.hset(self._key(job_id), mapping={
            "status": status,
            "detail": detail or "",
            "ts_updated": str(time.time()),
        })
        pipe.expire(self._key(job_id), self._ttl)
        await pipe.execute()
        return True

    async def get(self, job_id: str) -> Optional[JobRecord]:
        await self.connect()
        h = await self._redis.hgetall(self._key(job_id))
        if not h:
            return None
        try:
            payload = json.loads(h.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {"_raw": h.get("payload")}
        return JobRecord(
            job_id=job_id,
            namespace=self.namespace,
            status=h.get("status", "unknown"),
            payload=payload,
            detail=h.get("detail") or None,
            ts_created=float(h.get("ts_created", 0) or 0),
            ts_updated=float(h.get("ts_updated", 0) or 0),
        )

    async def list_recent(self, limit: int = 50) -> List[JobRecord]:
        await self.connect()
        ids = await self._redis.zrevrange(self._index_key(), 0, max(0, limit - 1))
        out: List[JobRecord] = []
        for jid in ids:
            r = await self.get(jid)
            if r is not None:
                out.append(r)
        return out

    async def claim_next(self, timeout_s: int = 5) -> Optional[str]:
        """BLPOP next job id from the queue. Returns None on timeout."""
        await self.connect()
        res = await self._redis.blpop(self._queue_key(), timeout=timeout_s)
        if res is None:
            return None
        # res = (key, value)
        return res[1]

    async def mark_stale(self, job_id: str, seconds_ago: int = 3600) -> bool:
        """Force ts_updated backwards. Helper for tests / operational reset."""
        await self.connect()
        if not await self._redis.exists(self._key(job_id)):
            return False
        backdated = time.time() - max(0, int(seconds_ago))
        await self._redis.hset(self._key(job_id), "ts_updated", str(backdated))
        return True

    async def rehydrate_stale_running(
        self, max_age_seconds: int = 600,
    ) -> List[str]:
        """Mark long-running jobs as failed(api_restart). Returns ids changed."""
        await self.connect()
        cutoff = time.time() - max_age_seconds
        # Scan all index entries; for small N this is fine.
        ids = await self._redis.zrange(self._index_key(), 0, -1)
        changed: List[str] = []
        for jid in ids:
            rec = await self.get(jid)
            if rec is None:
                continue
            if rec.status == "running" and rec.ts_updated < cutoff:
                await self.update_status(jid, "failed", detail="api_restart")
                changed.append(jid)
        return changed
