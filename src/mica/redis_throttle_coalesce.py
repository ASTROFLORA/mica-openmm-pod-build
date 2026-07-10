from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

from mica.infrastructure.redis_client import get_redis

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass
class _LocalBucketState:
    tokens: float
    last_refill: float


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class RedisThrottleCoalesce:
    """Global-ish provider throttling with local in-flight coalescing.

    Redis is used when available. The service degrades to local token buckets
    when Redis is unavailable so runtime paths keep working in offline tests.
    """

    _TOKEN_BUCKET_LUA = """
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at')
local tokens = tonumber(state[1])
local updated = tonumber(state[2])
local now = redis.call('TIME')
local now_sec = tonumber(now[1]) + tonumber(now[2]) / 1000000
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
if tokens == nil then
  tokens = capacity
  updated = now_sec
end
local delta = math.max(0, now_sec - updated)
tokens = math.min(capacity, tokens + (delta * rate))
local granted = 0
if tokens >= requested then
  tokens = tokens - requested
  granted = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_at', now_sec)
redis.call('EXPIRE', KEYS[1], 120)
return {granted, tokens}
"""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = str(
            redis_url
            or os.getenv("PROVIDER_THROTTLE_REDIS_URL")
            or os.getenv("MICA_REDIS_URL")
            or os.getenv("REDIS_URL")
            or ""
        ).strip() or None
        self.require_redis = _env_flag("PROVIDER_THROTTLE_REQUIRE_REDIS", False)
        self.allow_local_fallback = _env_flag("PROVIDER_THROTTLE_ALLOW_LOCAL_FALLBACK", True)
        self._redis = None
        self._script_sha: Optional[str] = None
        self._local_buckets: Dict[str, _LocalBucketState] = {}
        self._local_buckets_lock = asyncio.Lock()
        self._inflight: Dict[str, asyncio.Task[Any]] = {}
        self._inflight_lock = asyncio.Lock()
        self._mode = "uninitialized"
        self._init_attempted = False
        self._init_error = ""
        self._started_at = time.time()
        self._telemetry: Dict[str, Any] = {
            "provider_requests_total": 0,
            "provider_requests_by_mode": {"redis": 0, "local": 0},
            "provider_requests_by_name": {},
            "coalesce_calls": 0,
            "coalesce_hits": 0,
        }

    async def initialize(self) -> bool:
        self._init_attempted = True
        try:
            if not self.redis_url:
                raise RuntimeError("Redis URL not configured for provider throttle")
            self._redis = await get_redis(self.redis_url)
            await self._redis.ping()
            self._script_sha = await self._redis.script_load(self._TOKEN_BUCKET_LUA)
            self._mode = "redis"
            self._init_error = ""
            return True
        except Exception as exc:
            self._init_error = str(exc)
            self._redis = None
            self._script_sha = None
            if self.require_redis and not self.allow_local_fallback:
                self._mode = "error"
                raise RuntimeError(f"RedisThrottleCoalesce requires Redis but initialization failed: {exc}") from exc
            self._mode = "local_fallback"
            logger.warning("RedisThrottleCoalesce falling back to local mode: %s", exc)
            return False

    def telemetry_snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self._mode,
            "initialized": self._redis is not None and bool(self._script_sha),
            "init_attempted": self._init_attempted,
            "init_error": self._init_error or None,
            "redis_url_configured": bool(self.redis_url),
            "require_redis": self.require_redis,
            "allow_local_fallback": self.allow_local_fallback,
            "uptime_seconds": round(max(0.0, time.time() - self._started_at), 3),
            "provider_requests_total": int(self._telemetry["provider_requests_total"]),
            "provider_requests_by_mode": dict(self._telemetry["provider_requests_by_mode"]),
            "provider_requests_by_name": dict(self._telemetry["provider_requests_by_name"]),
            "coalesce_calls": int(self._telemetry["coalesce_calls"]),
            "coalesce_hits": int(self._telemetry["coalesce_hits"]),
        }

    def _record_provider_request(self, provider: str, mode: str) -> None:
        provider_key = str(provider or "unknown").strip().lower() or "unknown"
        mode_key = "redis" if mode == "redis" else "local"
        self._telemetry["provider_requests_total"] += 1
        self._telemetry["provider_requests_by_mode"][mode_key] = int(self._telemetry["provider_requests_by_mode"].get(mode_key, 0)) + 1
        provider_counts = self._telemetry["provider_requests_by_name"]
        provider_counts[provider_key] = int(provider_counts.get(provider_key, 0)) + 1

    async def acquire_provider_slot(
        self,
        provider: str,
        requests_per_minute: int,
        burst_limit: int = 1,
        *,
        tokens: float = 1.0,
    ) -> bool:
        provider_key = str(provider or "unknown").strip().lower() or "unknown"
        rpm = max(1, int(requests_per_minute or 1))
        capacity = max(float(burst_limit or 1), float(tokens or 1.0))
        rate_per_second = float(rpm) / 60.0
        if self._redis is not None and self._script_sha:
            redis_key = f"mica:literature:bucket:{provider_key}"
            while True:
                try:
                    result = await self._redis.evalsha(
                        self._script_sha,
                        1,
                        redis_key,
                        str(rate_per_second),
                        str(capacity),
                        str(tokens),
                    )
                    granted = bool(int((result or [0])[0]))
                    if granted:
                        self._record_provider_request(provider_key, "redis")
                        return True
                    await asyncio.sleep(max(0.05, float(tokens) / max(rate_per_second, 0.001)))
                    continue
                except Exception as exc:
                    logger.debug("Redis token bucket failed for %s, using local fallback: %s", provider_key, exc)
                    break
        await self._acquire_local(provider_key, rate_per_second=rate_per_second, capacity=capacity, tokens=tokens)
        self._record_provider_request(provider_key, "local")
        return True

    async def _acquire_local(
        self,
        provider: str,
        *,
        rate_per_second: float,
        capacity: float,
        tokens: float,
    ) -> None:
        while True:
            async with self._local_buckets_lock:
                now = time.monotonic()
                state = self._local_buckets.get(provider)
                if state is None:
                    state = _LocalBucketState(tokens=capacity, last_refill=now)
                    self._local_buckets[provider] = state
                elapsed = max(0.0, now - state.last_refill)
                state.tokens = min(capacity, state.tokens + (elapsed * rate_per_second))
                state.last_refill = now
                if state.tokens >= tokens:
                    state.tokens -= tokens
                    return
                wait_seconds = max(0.05, (tokens - state.tokens) / max(rate_per_second, 0.001))
            await asyncio.sleep(wait_seconds)

    async def coalesce(
        self,
        key: str,
        factory: Callable[[], Awaitable[_T]],
    ) -> _T:
        normalized_key = str(key or "").strip()
        self._telemetry["coalesce_calls"] += 1
        if not normalized_key:
            return await factory()
        async with self._inflight_lock:
            task = self._inflight.get(normalized_key)
            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[normalized_key] = task
            else:
                self._telemetry["coalesce_hits"] += 1
        try:
            return await task
        finally:
            async with self._inflight_lock:
                existing = self._inflight.get(normalized_key)
                if existing is task:
                    self._inflight.pop(normalized_key, None)