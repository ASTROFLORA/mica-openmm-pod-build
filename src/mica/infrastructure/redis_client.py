"""
redis_client.py — Centralized async Redis client for MICA.

Provides a shared singleton so that every module (worker, stream, jobs, ready)
uses the same connection pool instead of creating its own.

Usage:
    from mica.infrastructure.redis_client import get_redis, close_redis

    client = await get_redis()            # lazily creates the pool
    await client.ping()
    ...
    await close_redis()                   # at shutdown

Phase R2 · 2026-03-15
"""

from __future__ import annotations

import logging
import os
from typing import Optional

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore

logger = logging.getLogger("mica.infrastructure.redis_client")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _current_redis_url() -> str:
    return os.getenv("REDIS_URL") or os.getenv("MICA_REDIS_URL", "")

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_client: Optional["aioredis.Redis"] = None  # type: ignore[name-defined]


async def get_redis(
    url: Optional[str] = None,
    *,
    decode_responses: bool = True,
) -> "aioredis.Redis":  # type: ignore[name-defined]
    """Return the shared async Redis client, creating it on first call.

    Parameters
    ----------
    url : str | None
        Override URL (defaults to ``REDIS_URL`` env var).
    decode_responses : bool
        Whether to auto-decode bytes → str.
    """
    global _client
    if aioredis is None:
        raise RuntimeError("redis package not installed — run: pip install redis>=5.0.0")
    if _client is None:
        resolved_url = resolve_redis_url(url)
        if not resolved_url:
            raise RuntimeError("REDIS_URL not configured")
        logger.info("Creating shared Redis client → %s", resolved_url.split("@")[-1])
        _client = aioredis.from_url(
            resolved_url,
            decode_responses=decode_responses,
            socket_connect_timeout=5,
            socket_timeout=10,
            retry_on_timeout=True,
        )
    return _client


async def close_redis() -> None:
    """Gracefully close the shared Redis connection (call at shutdown)."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None
        logger.info("Shared Redis client closed")


def reset_redis() -> None:
    """Reset the singleton (for tests). Does NOT close the connection."""
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Utility helpers (used by job_manager, redisvl_semantic_cache, etc.)
# ---------------------------------------------------------------------------


def resolve_redis_url(url: Optional[str] = None) -> str:
    """Return a usable Redis URL, falling back to env vars.

    Priority: explicit *url* → ``REDIS_URL`` → ``MICA_REDIS_URL`` → empty string.
    """
    if url:
        return url
    resolved = _current_redis_url()
    if not resolved:
        logger.warning("REDIS_URL not set — Redis features will be unavailable")
    return resolved


def format_redis_target(url: Optional[str] = None) -> str:
    """Return a human-safe representation of a Redis URL (masks password)."""
    resolved = resolve_redis_url(url) if url is None else url
    if not resolved:
        return "<no-redis>"
    # Mask password: redis://:PASSWORD@host:port → redis://***@host:port
    import re
    return re.sub(r"://:[^@]+@", "://***@", resolved)


async def get_redis_if_configured(
    url: Optional[str] = None,
    *,
    decode_responses: bool = True,
) -> Optional["aioredis.Redis"]:  # type: ignore[name-defined]
    """Like :func:`get_redis` but returns ``None`` when Redis is unavailable
    instead of raising.
    """
    resolved = resolve_redis_url(url)
    if not resolved:
        return None
    if aioredis is None:
        return None
    try:
        return await get_redis(resolved, decode_responses=decode_responses)
    except Exception:
        logger.warning("Redis unavailable at %s — falling back to no-cache", format_redis_target(resolved))
        return None
