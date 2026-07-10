from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from mica.infrastructure.persistence.pg_async import (
    choose_database_url,
    choose_neon_database_url,
    create_asyncpg_pool_for_database_url,
)
from mica.infrastructure.redis_client import get_redis_if_configured

logger = logging.getLogger(__name__)

_IDEMPOTENCY_TTL_SECONDS = max(60, int(os.getenv("MICA_COMPUTE_IDEMPOTENCY_TTL_SECONDS", "86400")))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_from_epoch(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(float(epoch_s), tz=timezone.utc)


def _epoch_from_dt(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return float(value.timestamp())


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}
    return {}


@dataclass(slots=True)
class StoreAudit:
    postgres_available: bool
    redis_available: bool
    no_shared_store: bool
    memory_only_current: bool
    selected_backend: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "postgres_available": self.postgres_available,
            "redis_available": self.redis_available,
            "no_shared_store": self.no_shared_store,
            "memory_only_current": self.memory_only_current,
            "selected_backend": self.selected_backend,
        }


@dataclass(slots=True)
class ComputeJobRegistryRecord:
    job_id: str
    user_id: str
    workspace_id: Optional[str]
    provider: str
    state: str
    created_at: float
    updated_at: float
    request_hash: str
    idempotency_key: Optional[str]
    artifact_prefix: Optional[str]
    registry_backend: str
    accepted: bool = True
    error: Optional[str] = None
    route_decision_id: Optional[str] = None
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "request_hash": self.request_hash,
            "idempotency_key": self.idempotency_key,
            "artifact_prefix": self.artifact_prefix,
            "registry_backend": self.registry_backend,
            "accepted": self.accepted,
            "error": self.error,
            "route_decision_id": self.route_decision_id,
            "metadata": dict(self.metadata or {}),
        }


def classify_store_backend() -> StoreAudit:
    postgres_url = choose_neon_database_url(allow_legacy_database_url=True) or choose_database_url(prefer_timescale=False)
    redis_url = os.getenv("REDIS_URL") or os.getenv("MICA_REDIS_URL")

    postgres_available = bool((postgres_url or "").strip())
    redis_available = bool((redis_url or "").strip())
    no_shared_store = not postgres_available and not redis_available
    selected_backend = "postgres" if postgres_available else ("redis" if redis_available else "memory")
    return StoreAudit(
        postgres_available=postgres_available,
        redis_available=redis_available,
        no_shared_store=no_shared_store,
        memory_only_current=no_shared_store,
        selected_backend=selected_backend,
    )


class MemoryIdempotencyStore:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, user_id: str, key: str) -> Optional[dict[str, Any]]:
        now = time.time()
        async with self._lock:
            stale: list[tuple[str, str]] = []
            for pair, row in self._entries.items():
                expires_at = float(row.get("expires_at", 0.0) or 0.0)
                if expires_at and expires_at < now:
                    stale.append(pair)
            for pair in stale:
                self._entries.pop(pair, None)
            row = self._entries.get((user_id, key))
            if row is None:
                return None
            return dict(row)

    async def put(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, Any],
        response_status: int,
        job_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        now = time.time()
        ttl = max(60, int(ttl_seconds or _IDEMPOTENCY_TTL_SECONDS))
        row = {
            "user_id": user_id,
            "key": key,
            "request_hash": request_hash,
            "response_payload": dict(response_payload),
            "response_status": int(response_status),
            "job_id": job_id,
            "created_at": now,
            "expires_at": now + ttl,
            "replay_count": 0,
            "backend": "memory",
        }
        async with self._lock:
            self._entries[(user_id, key)] = row
        return dict(row)

    async def increment_replay(self, *, user_id: str, key: str) -> None:
        async with self._lock:
            row = self._entries.get((user_id, key))
            if row is not None:
                row["replay_count"] = int(row.get("replay_count", 0) or 0) + 1

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


class MemoryJobRegistryStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, record: dict[str, Any]) -> None:
        user_id = str(record.get("user_id") or "").strip()
        job_id = str(record.get("job_id") or "").strip()
        if not user_id or not job_id:
            return
        now = time.time()
        payload = {
            "user_id": user_id,
            "job_id": job_id,
            "workspace_id": record.get("workspace_id"),
            "provider": str(record.get("provider") or ""),
            "state": str(record.get("state") or ""),
            "request_hash": str(record.get("request_hash") or ""),
            "idempotency_key": str(record.get("idempotency_key") or "") or None,
            "artifact_prefix": str(record.get("artifact_prefix") or "") or None,
            "accepted": bool(record.get("accepted", True)),
            "error": record.get("error"),
            "route_decision_id": record.get("route_decision_id"),
            "metadata": dict(record.get("metadata") or {}),
            "created_at": float(record.get("created_at") or now),
            "updated_at": now,
            "registry_backend": "memory",
            "backend": "memory",
        }
        async with self._lock:
            existing = self._records.get((user_id, job_id))
            if existing is not None:
                payload["created_at"] = float(existing.get("created_at", payload["created_at"]))
            self._records[(user_id, job_id)] = payload

    async def get(self, *, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            row = self._records.get((user_id, job_id))
            if row is None:
                return None
            return dict(row)

    async def list_for_user(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [dict(v) for (uid, _), v in self._records.items() if uid == user_id]
        rows.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0.0), reverse=True)
        return rows[: max(1, int(limit))]

    async def clear(self) -> None:
        async with self._lock:
            self._records.clear()


class RedisIdempotencyStore:
    def __init__(self, redis_client: Any) -> None:
        self._r = redis_client
        self._key_prefix = "mica:api:compute:idempotency:v2"

    def _key(self, user_id: str, key: str) -> str:
        digest = hashlib.sha256(f"{user_id}|{key}".encode("utf-8")).hexdigest()
        return f"{self._key_prefix}:{digest}"

    async def get(self, *, user_id: str, key: str) -> Optional[dict[str, Any]]:
        raw = await self._r.get(self._key(user_id, key))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        expires_at = float(payload.get("expires_at", 0.0) or 0.0)
        if expires_at and expires_at < time.time():
            return None
        return payload

    async def put(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, Any],
        response_status: int,
        job_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        now = time.time()
        ttl = max(60, int(ttl_seconds or _IDEMPOTENCY_TTL_SECONDS))
        row = {
            "user_id": user_id,
            "key": key,
            "request_hash": request_hash,
            "response_payload": dict(response_payload),
            "response_status": int(response_status),
            "job_id": job_id,
            "created_at": now,
            "expires_at": now + ttl,
            "replay_count": 0,
            "backend": "redis",
        }
        await self._r.set(self._key(user_id, key), _json_dumps(row), ex=ttl)
        return row

    async def increment_replay(self, *, user_id: str, key: str) -> None:
        row = await self.get(user_id=user_id, key=key)
        if row is None:
            return
        row["replay_count"] = int(row.get("replay_count", 0) or 0) + 1
        ttl = max(1, int(float(row.get("expires_at", time.time() + 1)) - time.time()))
        await self._r.set(self._key(user_id, key), _json_dumps(row), ex=ttl)


class RedisJobRegistryStore:
    def __init__(self, redis_client: Any) -> None:
        self._r = redis_client
        self._key_prefix = "mica:api:compute:registry:v2"

    def _job_key(self, user_id: str, job_id: str) -> str:
        return f"{self._key_prefix}:user:{user_id}:job:{job_id}"

    def _user_index(self, user_id: str) -> str:
        return f"{self._key_prefix}:index:user:{user_id}"

    async def upsert(self, record: dict[str, Any]) -> None:
        user_id = str(record.get("user_id") or "").strip()
        job_id = str(record.get("job_id") or "").strip()
        if not user_id or not job_id:
            return
        now = time.time()
        payload = {
            "user_id": user_id,
            "job_id": job_id,
            "workspace_id": record.get("workspace_id"),
            "provider": str(record.get("provider") or ""),
            "state": str(record.get("state") or ""),
            "request_hash": str(record.get("request_hash") or ""),
            "idempotency_key": str(record.get("idempotency_key") or "") or None,
            "artifact_prefix": str(record.get("artifact_prefix") or "") or None,
            "accepted": bool(record.get("accepted", True)),
            "error": record.get("error"),
            "route_decision_id": record.get("route_decision_id"),
            "metadata": dict(record.get("metadata") or {}),
            "created_at": float(record.get("created_at") or now),
            "updated_at": now,
            "registry_backend": "redis",
            "backend": "redis",
        }
        existing = await self.get(user_id=user_id, job_id=job_id)
        if existing is not None:
            payload["created_at"] = float(existing.get("created_at", payload["created_at"]))

        pipe = self._r.pipeline(transaction=True)
        pipe.set(self._job_key(user_id, job_id), _json_dumps(payload))
        pipe.zadd(self._user_index(user_id), {job_id: float(payload["updated_at"])})
        await pipe.execute()

    async def get(self, *, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        raw = await self._r.get(self._job_key(user_id, job_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def list_for_user(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        job_ids = await self._r.zrevrange(self._user_index(user_id), 0, max(0, int(limit) - 1))
        if not job_ids:
            return []
        keys = [self._job_key(user_id, str(job_id)) for job_id in job_ids]
        raws = await self._r.mget(*keys)
        rows: list[dict[str, Any]] = []
        for raw in raws:
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except Exception:
                continue
        return rows


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Any = None
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._init_lock:
            if self._pool is not None:
                return
            pool = await create_asyncpg_pool_for_database_url(
                self._database_url,
                min_size=1,
                max_size=3,
                timeout=20,
                command_timeout=30,
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mica_api_compute_idempotency_v2 (
                        user_id TEXT NOT NULL,
                        idem_key TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        response_payload JSONB NOT NULL,
                        response_status INTEGER NOT NULL,
                        job_id TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL,
                        replay_count INTEGER NOT NULL DEFAULT 0,
                        backend TEXT NOT NULL DEFAULT 'postgres',
                        PRIMARY KEY (user_id, idem_key)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mica_api_compute_job_registry_v2 (
                        user_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        workspace_id TEXT,
                        provider TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL DEFAULT '',
                        request_hash TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT,
                        artifact_prefix TEXT,
                        accepted BOOLEAN NOT NULL DEFAULT TRUE,
                        error TEXT,
                        route_decision_id TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        registry_backend TEXT NOT NULL DEFAULT 'postgres',
                        PRIMARY KEY (user_id, job_id)
                    )
                    """
                )
                await conn.execute("ALTER TABLE mica_api_compute_job_registry_v2 ADD COLUMN IF NOT EXISTS workspace_id TEXT")
                await conn.execute("ALTER TABLE mica_api_compute_job_registry_v2 ADD COLUMN IF NOT EXISTS request_hash TEXT NOT NULL DEFAULT ''")
                await conn.execute("ALTER TABLE mica_api_compute_job_registry_v2 ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
                await conn.execute("ALTER TABLE mica_api_compute_job_registry_v2 ADD COLUMN IF NOT EXISTS artifact_prefix TEXT")
                await conn.execute("ALTER TABLE mica_api_compute_job_registry_v2 ADD COLUMN IF NOT EXISTS registry_backend TEXT NOT NULL DEFAULT 'postgres'")
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mica_api_compute_registry_user_updated_v2
                    ON mica_api_compute_job_registry_v2(user_id, updated_at DESC)
                    """
                )
            self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get_idempotency(self, *, user_id: str, key: str) -> Optional[dict[str, Any]]:
        await self.initialize()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, idem_key, request_hash, response_payload, response_status,
                       job_id, created_at, expires_at, replay_count, backend
                FROM mica_api_compute_idempotency_v2
                WHERE user_id = $1 AND idem_key = $2
                """,
                user_id,
                key,
            )
            if row is None:
                return None
            expires_at = row["expires_at"]
            if isinstance(expires_at, datetime) and expires_at < _utc_now():
                await conn.execute(
                    "DELETE FROM mica_api_compute_idempotency_v2 WHERE user_id = $1 AND idem_key = $2",
                    user_id,
                    key,
                )
                return None
            payload = {
                "user_id": row["user_id"],
                "key": row["idem_key"],
                "request_hash": row["request_hash"],
                "response_payload": _coerce_json_object(row["response_payload"]),
                "response_status": int(row["response_status"]),
                "job_id": row["job_id"],
                "created_at": _epoch_from_dt(row["created_at"]),
                "expires_at": _epoch_from_dt(row["expires_at"]),
                "replay_count": int(row["replay_count"]),
                "backend": str(row["backend"] or "postgres"),
            }
            return payload

    async def put_idempotency(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, Any],
        response_status: int,
        job_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        await self.initialize()
        assert self._pool is not None
        now = _utc_now()
        ttl = max(60, int(ttl_seconds or _IDEMPOTENCY_TTL_SECONDS))
        expires_at = _dt_from_epoch(time.time() + ttl)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mica_api_compute_idempotency_v2 (
                    user_id, idem_key, request_hash, response_payload, response_status,
                    job_id, created_at, expires_at, replay_count, backend
                ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, 0, 'postgres')
                ON CONFLICT (user_id, idem_key) DO UPDATE
                SET request_hash = EXCLUDED.request_hash,
                    response_payload = EXCLUDED.response_payload,
                    response_status = EXCLUDED.response_status,
                    job_id = EXCLUDED.job_id,
                    created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at,
                    backend = 'postgres'
                """,
                user_id,
                key,
                request_hash,
                json.dumps(response_payload),
                int(response_status),
                job_id,
                now,
                expires_at,
            )
        return {
            "user_id": user_id,
            "key": key,
            "request_hash": request_hash,
            "response_payload": dict(response_payload),
            "response_status": int(response_status),
            "job_id": job_id,
            "created_at": _epoch_from_dt(now),
            "expires_at": _epoch_from_dt(expires_at),
            "replay_count": 0,
            "backend": "postgres",
        }

    async def increment_idempotency_replay(self, *, user_id: str, key: str) -> None:
        await self.initialize()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE mica_api_compute_idempotency_v2
                SET replay_count = replay_count + 1
                WHERE user_id = $1 AND idem_key = $2
                """,
                user_id,
                key,
            )

    async def upsert_registry(self, record: dict[str, Any]) -> None:
        await self.initialize()
        assert self._pool is not None
        user_id = str(record.get("user_id") or "").strip()
        job_id = str(record.get("job_id") or "").strip()
        if not user_id or not job_id:
            return
        now = _utc_now()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mica_api_compute_job_registry_v2 (
                    user_id, job_id, workspace_id, provider, state,
                    request_hash, idempotency_key, artifact_prefix,
                    accepted, error, route_decision_id,
                    metadata, created_at, updated_at, registry_backend
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $13, 'postgres')
                ON CONFLICT (user_id, job_id) DO UPDATE
                SET workspace_id = EXCLUDED.workspace_id,
                    provider = EXCLUDED.provider,
                    state = EXCLUDED.state,
                    request_hash = EXCLUDED.request_hash,
                    idempotency_key = EXCLUDED.idempotency_key,
                    artifact_prefix = EXCLUDED.artifact_prefix,
                    accepted = EXCLUDED.accepted,
                    error = EXCLUDED.error,
                    route_decision_id = EXCLUDED.route_decision_id,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at,
                    registry_backend = 'postgres'
                """,
                user_id,
                job_id,
                record.get("workspace_id"),
                str(record.get("provider") or ""),
                str(record.get("state") or ""),
                str(record.get("request_hash") or ""),
                str(record.get("idempotency_key") or "") or None,
                str(record.get("artifact_prefix") or "") or None,
                bool(record.get("accepted", True)),
                record.get("error"),
                record.get("route_decision_id"),
                json.dumps(dict(record.get("metadata") or {})),
                now,
            )

    async def get_registry(self, *, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        await self.initialize()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, job_id, provider, state, accepted, error,
                      workspace_id, request_hash, idempotency_key, artifact_prefix,
                      route_decision_id, metadata, created_at, updated_at, registry_backend
                FROM mica_api_compute_job_registry_v2
                WHERE user_id = $1 AND job_id = $2
                """,
                user_id,
                job_id,
            )
            if row is None:
                return None
            return {
                "user_id": row["user_id"],
                "job_id": row["job_id"],
                "workspace_id": row["workspace_id"],
                "provider": str(row["provider"] or ""),
                "state": str(row["state"] or ""),
                "request_hash": str(row["request_hash"] or ""),
                "idempotency_key": row["idempotency_key"],
                "artifact_prefix": row["artifact_prefix"],
                "accepted": bool(row["accepted"]),
                "error": row["error"],
                "route_decision_id": row["route_decision_id"],
                "metadata": dict(row["metadata"] or {}),
                "created_at": _epoch_from_dt(row["created_at"]),
                "updated_at": _epoch_from_dt(row["updated_at"]),
                "registry_backend": str(row["registry_backend"] or "postgres"),
                "backend": str(row["registry_backend"] or "postgres"),
            }

    async def list_registry(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        await self.initialize()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, job_id, provider, state, accepted, error,
                      workspace_id, request_hash, idempotency_key, artifact_prefix,
                      route_decision_id, metadata, created_at, updated_at, registry_backend
                FROM mica_api_compute_job_registry_v2
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                user_id,
                max(1, int(limit)),
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "user_id": row["user_id"],
                    "job_id": row["job_id"],
                    "workspace_id": row["workspace_id"],
                    "provider": str(row["provider"] or ""),
                    "state": str(row["state"] or ""),
                    "request_hash": str(row["request_hash"] or ""),
                    "idempotency_key": row["idempotency_key"],
                    "artifact_prefix": row["artifact_prefix"],
                    "accepted": bool(row["accepted"]),
                    "error": row["error"],
                    "route_decision_id": row["route_decision_id"],
                    "metadata": dict(row["metadata"] or {}),
                    "created_at": _epoch_from_dt(row["created_at"]),
                    "updated_at": _epoch_from_dt(row["updated_at"]),
                    "registry_backend": str(row["registry_backend"] or "postgres"),
                    "backend": str(row["registry_backend"] or "postgres"),
                }
            )
        return out


class DurableComputeStoreManager:
    def __init__(self, *, idempotency_store: Any, registry_store: Any, idempotency_backend: str, registry_backend: str, audit: StoreAudit) -> None:
        self._idempotency_store = idempotency_store
        self._registry_store = registry_store
        self.idempotency_backend = idempotency_backend
        self.registry_backend = registry_backend
        self.audit = audit

    async def idempotency_get(self, *, user_id: str, key: str) -> Optional[dict[str, Any]]:
        return await self._idempotency_store.get(user_id=user_id, key=key)

    async def idempotency_put(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, Any],
        response_status: int,
        job_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        return await self._idempotency_store.put(
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_payload=response_payload,
            response_status=response_status,
            job_id=job_id,
            ttl_seconds=ttl_seconds,
        )

    async def idempotency_conflict(self, *, user_id: str, key: str, request_hash: str) -> bool:
        existing = await self.idempotency_get(user_id=user_id, key=key)
        if existing is None:
            return False
        return str(existing.get("request_hash") or "") != str(request_hash or "")

    async def idempotency_increment_replay(self, *, user_id: str, key: str) -> None:
        await self._idempotency_store.increment_replay(user_id=user_id, key=key)

    async def registry_upsert(self, record: dict[str, Any]) -> None:
        await self._registry_store.upsert(record)

    async def registry_get(self, *, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        return await self._registry_store.get(user_id=user_id, job_id=job_id)

    async def registry_list_for_user(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return await self._registry_store.list_for_user(user_id=user_id, limit=limit)

    async def clear_for_tests(self) -> None:
        clear_idem = getattr(self._idempotency_store, "clear", None)
        if callable(clear_idem):
            await clear_idem()
        clear_registry = getattr(self._registry_store, "clear", None)
        if callable(clear_registry):
            await clear_registry()


_store_manager: Optional[DurableComputeStoreManager] = None
_store_lock = asyncio.Lock()


async def _build_store_manager(audit: StoreAudit) -> DurableComputeStoreManager:
    postgres_url = choose_neon_database_url(allow_legacy_database_url=True) or choose_database_url(prefer_timescale=False)

    if audit.selected_backend == "postgres" and postgres_url:
        try:
            pg_store = PostgresStore(database_url=postgres_url)
            await pg_store.initialize()
            return DurableComputeStoreManager(
                idempotency_store=_PostgresIdempotencyAdapter(pg_store),
                registry_store=_PostgresRegistryAdapter(pg_store),
                idempotency_backend="postgres",
                registry_backend="postgres",
                audit=audit,
            )
        except Exception as exc:
            logger.warning("Postgres store unavailable, trying Redis fallback: %s", exc)

    if audit.redis_available:
        try:
            redis_client = await get_redis_if_configured()
            if redis_client is not None:
                return DurableComputeStoreManager(
                    idempotency_store=RedisIdempotencyStore(redis_client),
                    registry_store=RedisJobRegistryStore(redis_client),
                    idempotency_backend="redis",
                    registry_backend="redis",
                    audit=audit,
                )
        except Exception as exc:
            logger.warning("Redis store unavailable, using in-memory fallback: %s", exc)

    return DurableComputeStoreManager(
        idempotency_store=MemoryIdempotencyStore(),
        registry_store=MemoryJobRegistryStore(),
        idempotency_backend="memory",
        registry_backend="memory",
        audit=audit,
    )


class _PostgresIdempotencyAdapter:
    def __init__(self, pg: PostgresStore) -> None:
        self._pg = pg

    async def get(self, *, user_id: str, key: str) -> Optional[dict[str, Any]]:
        return await self._pg.get_idempotency(user_id=user_id, key=key)

    async def put(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, Any],
        response_status: int,
        job_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        return await self._pg.put_idempotency(
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_payload=response_payload,
            response_status=response_status,
            job_id=job_id,
            ttl_seconds=ttl_seconds,
        )

    async def increment_replay(self, *, user_id: str, key: str) -> None:
        await self._pg.increment_idempotency_replay(user_id=user_id, key=key)


class _PostgresRegistryAdapter:
    def __init__(self, pg: PostgresStore) -> None:
        self._pg = pg

    async def upsert(self, record: dict[str, Any]) -> None:
        await self._pg.upsert_registry(record)

    async def get(self, *, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        return await self._pg.get_registry(user_id=user_id, job_id=job_id)

    async def list_for_user(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return await self._pg.list_registry(user_id=user_id, limit=limit)


async def get_compute_store_manager() -> DurableComputeStoreManager:
    global _store_manager
    if _store_manager is not None:
        return _store_manager

    async with _store_lock:
        if _store_manager is not None:
            return _store_manager
        audit = classify_store_backend()
        _store_manager = await _build_store_manager(audit)
        return _store_manager


async def reset_compute_store_manager_for_tests() -> None:
    global _store_manager
    async with _store_lock:
        _store_manager = None
