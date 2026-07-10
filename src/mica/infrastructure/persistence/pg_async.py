"""pg_async.py

Small, reusable asyncpg utilities used across Timescale/Neon stores.

Goals:
- Centralize DSN selection & password handling (PGPASSWORD when DSN omits password)
- Provide a minimal base class for pool lifecycle and common query helpers
- Provide safe identifier validation for schema/table names (no quoting gymnastics)

This module is intentionally lightweight so other scripts/stores can reuse it.
"""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional
from urllib.parse import parse_qs, urlparse

try:
    import asyncpg as _asyncpg
except ImportError:  # pragma: no cover
    _asyncpg = None  # type: ignore

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    from asyncpg.pool import Pool
else:  # pragma: no cover
    Pool = Any  # type: ignore


def mask_dsn(dsn: str) -> str:
    return re.sub(r"(postgres(?:ql)?://[^:]+:)([^@]+)(@)", r"\1***\3", dsn)


def clean_env_value(v: Optional[str]) -> Optional[str]:
    if not v:
        return v
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    return v


def looks_like_neon(v: str) -> bool:
    return "neon.tech" in v


def looks_like_timescale(v: str) -> bool:
    return "timescale.com" in v or "tsdb.cloud.timescale.com" in v


DatabaseRole = Literal["generic", "timescale", "neon", "kb"]


def _legacy_database_url_allowed() -> bool:
    raw = (os.getenv("MICA_ALLOW_LEGACY_DATABASE_URL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _pick_first(candidates: list[Optional[str]]) -> Optional[str]:
    for candidate in candidates:
        candidate = clean_env_value(candidate)
        if candidate:
            return candidate
    return None


def choose_timescale_database_url(
    explicit: Optional[str] = None,
    *,
    allow_legacy_database_url: Optional[bool] = None,
) -> Optional[str]:
    """Pick a Timescale DSN without silently falling back to Neon."""

    if allow_legacy_database_url is None:
        allow_legacy_database_url = _legacy_database_url_allowed()

    candidates = [
        explicit,
        os.getenv("TIMESCALE_SERVICE_URL"),
        os.getenv("TIMESCALE_DSN"),
        os.getenv("TIMESCALE_URL"),
    ]
    if allow_legacy_database_url:
        candidates.append(os.getenv("DATABASE_URL"))

    chosen = _pick_first(candidates)
    if not chosen:
        return None
    if looks_like_neon(chosen) and not looks_like_timescale(chosen):
        return None
    return chosen


def choose_neon_database_url(
    explicit: Optional[str] = None,
    *,
    allow_legacy_database_url: Optional[bool] = None,
) -> Optional[str]:
    """Pick a Neon DSN without silently falling back to Timescale."""

    if allow_legacy_database_url is None:
        allow_legacy_database_url = _legacy_database_url_allowed()

    candidates = [
        explicit,
        os.getenv("NEON_DATABASE_URL"),
    ]
    if allow_legacy_database_url:
        candidates.append(os.getenv("DATABASE_URL"))

    chosen = _pick_first(candidates)
    if not chosen:
        return None
    if looks_like_timescale(chosen) and not looks_like_neon(chosen):
        return None
    return chosen


def choose_kb_database_url(
    explicit: Optional[str] = None,
    *,
    allow_legacy_database_url: Optional[bool] = None,
) -> Optional[str]:
    """Pick a KB DSN using KB-specific env vars before broader fallbacks."""

    if allow_legacy_database_url is None:
        allow_legacy_database_url = _legacy_database_url_allowed()

    candidates = [
        explicit,
        os.getenv("KB_DATABASE_URL"),
        os.getenv("KNOWLEDGE_BASE_DATABASE_URL"),
        os.getenv("NEON_DATABASE_URL"),
        os.getenv("TIMESCALE_SERVICE_URL"),
        os.getenv("TIMESCALE_DSN"),
        os.getenv("TIMESCALE_URL"),
    ]
    if allow_legacy_database_url:
        candidates.append(os.getenv("DATABASE_URL"))
    return _pick_first(candidates)


def validate_ident(ident: str) -> str:
    ident = ident.strip()
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", ident):
        raise ValueError(f"invalid identifier: {ident!r}")
    return ident


def choose_database_url(
    explicit: Optional[str] = None,
    *,
    prefer_timescale: bool = True,
) -> Optional[str]:
    """Pick a DSN using common env vars.

    If prefer_timescale=True, filters out Neon DSNs when they don't look like Timescale.
    """

    candidates = [
        explicit,
        os.getenv("TIMESCALE_SERVICE_URL"),
        os.getenv("DATABASE_URL"),
        os.getenv("TIMESCALE_URL"),
        os.getenv("NEON_DATABASE_URL"),
    ]

    chosen: Optional[str] = None
    for c in candidates:
        c = clean_env_value(c)
        if not c:
            continue
        if prefer_timescale and looks_like_neon(c) and not looks_like_timescale(c):
            continue
        chosen = c
        break

    if chosen:
        return chosen

    # Fallback: first non-empty without filtering
    for c in candidates:
        c = clean_env_value(c)
        if c:
            return c

    return None


# ---------------------------------------------------------------------------
# DSN Canonical Validation Gate (added 2026-03-17)
# ---------------------------------------------------------------------------
_CANONICAL_NEON_HOST_PATTERN = "us-east-1.aws.neon.tech"
_CANONICAL_TIMESCALE_HOST_PATTERN = "tsdb.cloud.timescale.com"
_DSN_VALIDATED = False

_pg_logger = __import__("logging").getLogger(__name__)


def validate_dsn_canonical(*, once: bool = True) -> None:
    """Emit warnings if runtime DSNs don't match canonical host patterns.

    Called lazily on first store init.  Never crashes — only warns.
    """
    global _DSN_VALIDATED
    if once and _DSN_VALIDATED:
        return
    _DSN_VALIDATED = True

    neon = choose_neon_database_url()
    if neon:
        parsed_host = urlparse(neon).hostname or ""
        if _CANONICAL_NEON_HOST_PATTERN not in parsed_host:
            _pg_logger.warning(
                "DSN-DRIFT Neon DSN host %r does not match canonical pattern %r — "
                "check .env loading order / stale nested .env",
                parsed_host,
                _CANONICAL_NEON_HOST_PATTERN,
            )

    ts = choose_timescale_database_url()
    if ts:
        parsed_host = urlparse(ts).hostname or ""
        if _CANONICAL_TIMESCALE_HOST_PATTERN not in parsed_host:
            _pg_logger.warning(
                "DSN-DRIFT Timescale DSN host %r does not match canonical pattern %r",
                parsed_host,
                _CANONICAL_TIMESCALE_HOST_PATTERN,
            )


def ssl_context_for_database_url(database_url: str) -> ssl.SSLContext | None:
    try:
        parsed = urlparse(database_url)
        sslmode = (parse_qs(parsed.query).get("sslmode", [""])[0] or "").strip().lower()
    except Exception:
        sslmode = ""

    if not sslmode:
        sslmode = (os.getenv("PGSSLMODE") or "").strip().lower()

    _skip_verify = os.getenv("MICA_SSL_VERIFY_SKIP", "").strip().lower() in ("1", "true", "yes")

    if sslmode == "require":
        ctx = ssl.create_default_context()
        # Match libpq semantics: sslmode=require enforces encryption but does not
        # require certificate or hostname verification.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    if sslmode in {"verify-ca", "verify-full"}:
        ctx = ssl.create_default_context()
        if sslmode == "verify-ca":
            ctx.check_hostname = False
        if _skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    return None


def asyncpg_connection_kwargs_for_database_url(
    database_url: str,
    *,
    password_env_var: str = "PGPASSWORD",
) -> dict[str, Any]:
    """Build explicit asyncpg connection kwargs from a database URL.

    Neon connections are reliable in this runtime when asyncpg receives
    host/user/password/database separately instead of the full DSN string.
    For URLs we cannot safely decompose, fall back to `dsn=...`.
    """

    parsed = urlparse(database_url)
    database = parsed.path.lstrip("/")
    if not parsed.hostname or not parsed.username or not database:
        kwargs: dict[str, Any] = {"dsn": database_url}
    else:
        kwargs = {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "user": parsed.username,
            "database": database,
        }
        password = parsed.password or os.getenv(password_env_var)
        if password:
            kwargs["password"] = password

    ssl_ctx = ssl_context_for_database_url(database_url)
    if ssl_ctx is not None:
        kwargs["ssl"] = ssl_ctx

    # PgBouncer transaction mode (Neon pooler) does not support server-side
    # prepared statements across pool connections.  Disable the asyncpg
    # statement cache to prevent "prepared statement ... does not exist" errors.
    hostname = parsed.hostname or ""
    if "-pooler." in hostname or hostname.startswith("pooler."):
        kwargs["statement_cache_size"] = 0

    return kwargs


async def create_asyncpg_pool_for_database_url(
    database_url: str,
    **pool_kwargs: Any,
):
    if _asyncpg is None:
        raise RuntimeError("asyncpg not installed")

    kwargs = dict(pool_kwargs)
    kwargs.update(asyncpg_connection_kwargs_for_database_url(database_url))
    return await _asyncpg.create_pool(**kwargs)


async def connect_asyncpg_for_database_url(
    database_url: str,
    **connect_kwargs: Any,
):
    if _asyncpg is None:
        raise RuntimeError("asyncpg not installed")

    kwargs = dict(connect_kwargs)
    kwargs.update(asyncpg_connection_kwargs_for_database_url(database_url))
    return await _asyncpg.connect(**kwargs)


@dataclass(frozen=True)
class PoolConfig:
    min_size: int = 1
    max_size: int = 5
    command_timeout: int = 30
    connect_timeout: int = 20
    max_inactive_connection_lifetime: float = 300.0
    max_queries: int = 50000


class AsyncPGStoreBase:
    """A tiny reusable base for asyncpg-backed stores."""

    def __init__(
        self,
        database_url: Optional[str] = None,
        *,
        prefer_timescale: bool = True,
        role: DatabaseRole = "generic",
        pool_config: Optional[PoolConfig] = None,
    ) -> None:
        if _asyncpg is None:
            raise RuntimeError("asyncpg not installed")

        if role == "timescale":
            self.database_url = choose_timescale_database_url(database_url)
        elif role == "neon":
            self.database_url = choose_neon_database_url(database_url)
        elif role == "kb":
            self.database_url = choose_kb_database_url(database_url)
        else:
            self.database_url = choose_database_url(database_url, prefer_timescale=prefer_timescale)
        if not self.database_url:
            raise RuntimeError("No database URL configured")

        self.pool_config = pool_config or PoolConfig()
        self._pool: Optional[Pool] = None

    @property
    def pool(self) -> Pool:
        if self._pool is None:
            raise RuntimeError("Store not initialized; call initialize()")
        return self._pool

    async def initialize(self) -> None:
        if self._pool is not None:
            return

        validate_dsn_canonical()

        pool_kwargs: dict[str, Any] = {
            "min_size": self.pool_config.min_size,
            "max_size": self.pool_config.max_size,
            "command_timeout": self.pool_config.command_timeout,
            "timeout": self.pool_config.connect_timeout,
            "max_inactive_connection_lifetime": self.pool_config.max_inactive_connection_lifetime,
            "max_queries": self.pool_config.max_queries,
        }

        self._pool = await create_asyncpg_pool_for_database_url(self.database_url, **pool_kwargs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
