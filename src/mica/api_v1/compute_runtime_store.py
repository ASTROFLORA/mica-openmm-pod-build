from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from mica.infrastructure.redis_client import get_redis


class ComputeRuntimeStore:
    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._idem_memory: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._registry_memory: Dict[str, Dict[str, Any]] = {}
        self._ttl_seconds = max(60, int(os.getenv("MICA_COMPUTE_IDEMPOTENCY_TTL_SECONDS", "86400")))

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    @property
    def production_safe(self) -> bool:
        return self.backend == "redis"

    @property
    def idempotency_ttl_seconds(self) -> int:
        return self._ttl_seconds

    @classmethod
    async def from_runtime(cls) -> "ComputeRuntimeStore":
        try:
            redis_client = await get_redis()
            await redis_client.ping()
            return cls(redis_client=redis_client)
        except Exception:
            return cls(redis_client=None)

    async def get_idempotency_record(self, *, user_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        if self._redis is not None:
            raw = await self._redis.get(self._idem_key(user_id, idempotency_key))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        record = self._idem_memory.get((user_id, idempotency_key))
        if not record:
            return None
        expires_at = float(record.get("expires_at", 0.0) or 0.0)
        if expires_at and time.time() >= expires_at:
            self._idem_memory.pop((user_id, idempotency_key), None)
            return None
        return dict(record)

    async def put_idempotency_record(self, *, user_id: str, idempotency_key: str, record: Dict[str, Any]) -> None:
        payload = dict(record)
        payload["store_backend"] = self.backend
        if self._redis is not None:
            ttl = max(60, int(float(payload.get("expires_at", time.time() + self._ttl_seconds)) - time.time()))
            await self._redis.set(self._idem_key(user_id, idempotency_key), json.dumps(payload), ex=ttl)
            return
        self._idem_memory[(user_id, idempotency_key)] = payload

    async def bump_idempotency_replay(self, *, user_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        record = await self.get_idempotency_record(user_id=user_id, idempotency_key=idempotency_key)
        if record is None:
            return None
        record["replay_count"] = int(record.get("replay_count", 0) or 0) + 1
        await self.put_idempotency_record(user_id=user_id, idempotency_key=idempotency_key, record=record)
        return record

    async def upsert_job_registry_record(self, *, record: Dict[str, Any]) -> None:
        payload = dict(record)
        payload["registry_backend"] = self.backend
        job_id = str(payload.get("job_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not job_id or not user_id:
            return

        if self._redis is not None:
            key = self._registry_key(job_id)
            await self._redis.set(key, json.dumps(payload))
            await self._redis.zadd(self._registry_user_index_key(user_id), {job_id: float(payload.get("updated_at", time.time()))})
            return

        self._registry_memory[job_id] = payload

    async def get_job_registry_record(self, *, job_id: str) -> Optional[Dict[str, Any]]:
        if self._redis is not None:
            raw = await self._redis.get(self._registry_key(job_id))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        record = self._registry_memory.get(job_id)
        if not record:
            return None
        return dict(record)

    async def list_job_registry_records(self, *, user_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        if self._redis is not None:
            ids = await self._redis.zrevrange(self._registry_user_index_key(user_id), 0, max(0, limit - 1))
            records: List[Dict[str, Any]] = []
            for job_id in ids:
                if isinstance(job_id, bytes):
                    job_id = job_id.decode("utf-8", errors="replace")
                record = await self.get_job_registry_record(job_id=str(job_id))
                if record is not None and str(record.get("user_id") or "") == user_id:
                    records.append(record)
            return records

        records = [
            dict(record)
            for record in self._registry_memory.values()
            if str(record.get("user_id") or "") == user_id
        ]
        records.sort(key=lambda item: float(item.get("updated_at", 0.0) or 0.0), reverse=True)
        return records[:limit]

    def _idem_key(self, user_id: str, idempotency_key: str) -> str:
        return f"mica:compute:idempotency:{user_id}:{idempotency_key}"

    def _registry_key(self, job_id: str) -> str:
        return f"mica:compute:registry:{job_id}"

    def _registry_user_index_key(self, user_id: str) -> str:
        return f"mica:compute:registry:user:{user_id}"
