"""
FastAPI v1 surface with user-scoped storage, signed URLs, and per-user metrics.

Assumptions:
- Auth placeholder: header `X-User-Id` carries the canonical user id (Clerk JWT validation can wrap this later).
- GCS credentials are available via ADC (user already authenticated) or service account env vars.
- Bucket-per-user strategy using deterministic hash; no PII in bucket names.
"""

from __future__ import annotations

import os
import sys
import time
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import re

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import jwt
import asyncpg
import redis
from mica.infrastructure.redis_client import get_redis, close_redis
from mica.storage.gcs_user_storage import (
    GCSUserStorage,
    get_storage_manager as _shared_get_storage_manager,
    storage_status as _shared_storage_status,
)
from pydantic import BaseModel, Field, ValidationError

# Ensure `mica.*` absolute imports work when running from the repo root.
# `mica` is located at `src/mica`, so we need `src/` on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from mica.ws_agentic import handle_mica_agentic_websocket
from mica.config.dotenv_loader import seed_env_from_dotenv
from mica.infrastructure.unified_backend.database import close_database_pool, initialize_database
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
    choose_timescale_database_url,
    mask_dsn,
    ssl_context_for_database_url,
)
from mica.infrastructure.persistence.session_repository import inspect_neon_sessions_table_contract
from mica.protocol_drafts import compile_protocol_draft_to_prompt, protocol_jsonld_to_executor_request
from mica.drivers.execution.protocol_executor import build_protocol_dispatch_metadata, execute_protocol_executor_request
from mica_q.protocol_jsonld_validator import ProtocolJSONLDSemanticError
from mica.api_v1.cors_config import DEFAULT_DEV_CORS_ALLOW_ORIGINS, build_cors_allow_origins
from mica.api_v1.startup_guard import await_nonfatal_startup_step

logger = logging.getLogger(__name__)
_PROCESS_START_TIME = int(time.time())


# -----------------------------
# Configuration helpers
# -----------------------------

def _env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    return val if val not in (None, "") else default


_NONFATAL_STARTUP_TIMEOUT_SEC = float(_env("MICA_NONFATAL_STARTUP_TIMEOUT_SEC", "8") or "8")


# Load repo-level .env (best-effort) for local/prod parity.
# By default this does NOT override already-set environment variables.
seed_env_from_dotenv()


GCP_PROJECT = _env("GCP_PROJECT", _env("GOOGLE_CLOUD_PROJECT")) or ""
GCS_REGION = _env("GCS_REGION", "us-central1")
BUCKET_PREFIX = _env("GCS_BUCKET_PREFIX", "mica-user")

MAX_URL_TTL = int(_env("GCS_MAX_URL_TTL", "3600"))  # upper clamp for signed URLs
DEFAULT_URL_TTL = int(_env("GCS_DEFAULT_URL_TTL", "900"))
ALLOWED_CONTENT_TYPES = set(
    ct.strip()
    for ct in _env(
        "GCS_ALLOWED_CONTENT_TYPES",
        "application/octet-stream,text/plain,application/json,image/png,image/jpeg",
    ).split(",")
)
DEFAULT_CONTENT_TYPE = "application/octet-stream"
BUCKET_HASH_LEN = int(_env("GCS_BUCKET_HASH_LEN", "12"))
_DEFAULT_DEV_CORS_ALLOW_ORIGINS = ",".join(DEFAULT_DEV_CORS_ALLOW_ORIGINS)
CORS_ALLOW_ORIGINS = _env("CORS_ALLOW_ORIGINS", _DEFAULT_DEV_CORS_ALLOW_ORIGINS)
CORS_EXTRA_ALLOW_ORIGINS = _env("CORS_EXTRA_ALLOW_ORIGINS", "")
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = [
    "Authorization",
    "Content-Type",
    "X-User-Id",
    "X-Requested-With",
    "Accept",
    "Origin",
]

# Clerk / JWT settings
CLERK_JWKS_URL = _env("CLERK_JWKS_URL")
CLERK_ISSUER = _env("CLERK_ISSUER")
CLERK_AUDIENCE = _env("CLERK_AUDIENCE")
CLERK_REQUIRE_TOKEN = _env("CLERK_REQUIRE_TOKEN", "true").lower() == "true"
CLERK_WEBHOOK_SECRET = _env("CLERK_WEBHOOK_SECRET")

from mica.api_v1.auth import user_dependency as _auth_user_dependency


_MUDO_IMPORT_ERROR: str | None = None


# -----------------------------
# User identity dependency
# -----------------------------

def get_user_id(x_user_id: Optional[str]) -> str:
    if CLERK_JWKS_URL:
        raise HTTPException(status_code=401, detail="Authorization token required")
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id header")
    return x_user_id.strip()


def _user_id_from_header(x_user_id: Optional[str] = Header(default=None)) -> str:
    return get_user_id(x_user_id)


# -----------------------------
# Input validation helpers
# -----------------------------


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_prefix(prefix: str) -> str:
    if prefix is None:
        return ""
    prefix = prefix.strip().strip("/")
    if prefix == "":
        return ""
    segments = [seg for seg in prefix.split("/") if seg]
    for seg in segments:
        if seg in ("..", "."):
            raise HTTPException(status_code=400, detail="Invalid prefix segment")
        if not _SEGMENT_RE.match(seg):
            raise HTTPException(status_code=400, detail="Invalid prefix characters")
    return "/".join(segments)


def _validate_object_name(name: str) -> str:
    if not name:
        raise HTTPException(status_code=400, detail="object_name is required")
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="object_name must not contain path separators")
    if name in ("..", "."):
        raise HTTPException(status_code=400, detail="Invalid object_name")
    if not _SEGMENT_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid object_name characters")
    return name


def _normalize_object_path(prefix: str, object_name: str) -> str:
    safe_prefix = _validate_prefix(prefix)
    safe_object = _validate_object_name(object_name)
    return f"{safe_prefix}/{safe_object}" if safe_prefix else safe_object


def _clamp_ttl(seconds: int) -> int:
    return max(60, min(seconds, MAX_URL_TTL))


def _sanitize_content_type(content_type: Optional[str]) -> str:
    if not content_type:
        return DEFAULT_CONTENT_TYPE
    ct = content_type.strip()
    if ct in ALLOWED_CONTENT_TYPES or ct.startswith("text/"):
        return ct
    return DEFAULT_CONTENT_TYPE


@lru_cache(maxsize=1)
def _jwks_client():
    if not CLERK_JWKS_URL:
        return None
    try:
        return jwt.PyJWKClient(CLERK_JWKS_URL)
    except Exception as exc:  # pragma: no cover - network/init failure
        raise RuntimeError(f"Failed to initialize JWKS client: {exc}")


def _extract_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip() or None


def get_user_from_token(authorization: Optional[str] = Header(default=None)) -> str:
    token = _extract_bearer(authorization)
    if not token:
        if CLERK_REQUIRE_TOKEN:
            raise HTTPException(status_code=401, detail="Missing bearer token")
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    client = _jwks_client()
    if client is None:
        raise HTTPException(status_code=500, detail="Auth not configured")

    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=CLERK_AUDIENCE,
            issuer=CLERK_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub")
    return user_id


# -----------------------------
# Metrics (in-memory per-user counters)
# -----------------------------


class UserMetrics:
    def __init__(self) -> None:
        self._counters = defaultdict(lambda: {"upload_urls": 0, "download_urls": 0, "bytes_planned": 0})

    def inc_upload(self, user_id: str, bytes_planned: int | None = None) -> None:
        m = self._counters[user_id]
        m["upload_urls"] += 1
        if bytes_planned:
            m["bytes_planned"] += max(0, int(bytes_planned))

    def inc_download(self, user_id: str) -> None:
        self._counters[user_id]["download_urls"] += 1

    def snapshot(self, user_id: str) -> Dict[str, int]:
        return dict(self._counters[user_id])


metrics = UserMetrics()
_storage_manager: GCSUserStorage | None = None
_storage_init_error: str | None = None

# Public alias used by tests/introspection. Best-effort initialized.
storage_manager: GCSUserStorage | None = None

_agentic_driver_lock = asyncio.Lock()

# G6: LRU cache with TTL for AgenticDriver instances — prevents memory leak
_DRIVER_CACHE_MAX_SIZE = 50
_DRIVER_CACHE_TTL_SEC = 3600  # 1 hour
_agentic_driver_cache: dict[tuple[str, bool, bool], tuple[object, float]] = {}


def _evict_stale_drivers() -> None:
    """Remove expired entries from the driver cache."""
    now = time.monotonic()
    expired = [
        key for key, (_, ts) in _agentic_driver_cache.items()
        if (now - ts) > _DRIVER_CACHE_TTL_SEC
    ]
    for key in expired:
        del _agentic_driver_cache[key]
    # If still over capacity, remove oldest
    if len(_agentic_driver_cache) > _DRIVER_CACHE_MAX_SIZE:
        sorted_keys = sorted(
            _agentic_driver_cache.keys(),
            key=lambda k: _agentic_driver_cache[k][1],
        )
        for key in sorted_keys[: len(_agentic_driver_cache) - _DRIVER_CACHE_MAX_SIZE]:
            del _agentic_driver_cache[key]

# ---------------------------------------------------------------------------
# Agentic job store  (non-blocking endpoint support)
# ---------------------------------------------------------------------------

import uuid as _uuid_mod
from enum import Enum


class _JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class _JobRecord:
    __slots__ = ("job_id", "user_id", "status", "result", "error", "created_at", "finished_at", "request_metadata")

    def __init__(self, job_id: str, user_id: str, request_metadata: Optional[Dict[str, object]] = None) -> None:
        self.job_id = job_id
        self.user_id = user_id
        self.status = _JobStatus.QUEUED
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.request_metadata = dict(request_metadata or {})

    def to_dict(self) -> Dict:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "request_metadata": self.request_metadata,
        }


_agentic_jobs: Dict[str, _JobRecord] = {}
_AGENTIC_JOB_TTL = int(os.environ.get("MICA_JOB_TTL_SECONDS", "3600"))  # seconds to keep completed jobs in memory
_AGENTIC_JOBS_MAX = 500   # hard cap — evict oldest completed jobs when exceeded


def _evict_stale_jobs() -> None:
    """Remove expired completed jobs and enforce the hard cap."""
    now = time.time()
    # Phase 1: remove jobs past TTL
    stale = [
        jid for jid, j in _agentic_jobs.items()
        if j.finished_at and (now - j.finished_at) > _AGENTIC_JOB_TTL
    ]
    for jid in stale:
        _agentic_jobs.pop(jid, None)
    # Phase 2: if still over max, remove oldest completed
    if len(_agentic_jobs) > _AGENTIC_JOBS_MAX:
        completed = sorted(
            [(jid, j) for jid, j in _agentic_jobs.items() if j.finished_at],
            key=lambda x: x[1].finished_at or 0,
        )
        to_remove = len(_agentic_jobs) - _AGENTIC_JOBS_MAX
        for jid, _ in completed[:to_remove]:
            _agentic_jobs.pop(jid, None)


def _get_storage_manager() -> GCSUserStorage:
    global _storage_manager, _storage_init_error, storage_manager
    if _storage_manager is not None:
        return _storage_manager
    try:
        _storage_manager = _shared_get_storage_manager()
        storage_manager = _storage_manager
        return _storage_manager
    except Exception as exc:
        _storage_init_error = str(exc)
        raise HTTPException(status_code=503, detail="Storage not configured")


def _storage_status() -> dict:
    # Delegate to the shared helper (keeps behavior consistent across API surfaces).
    return _shared_storage_status()


def _semantic_cache_health() -> dict:
    """Best-effort RedisVL semantic cache status for /health.

    P0-SEC fix (2026-04-20): `semantic_cache_status()` returns the full
    Redis DSN in ``target`` (including the password). This endpoint is
    unauthenticated, so we must redact any credential-bearing field
    before returning. The full shape remains available to authenticated
    admins via internal logging.
    """
    try:
        from mica.infrastructure.redisvl_semantic_cache import semantic_cache_status
        raw = semantic_cache_status() or {}
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}

    # Redact credential-bearing fields
    redacted = {}
    for key, value in raw.items():
        if key == "target":
            # Never expose the DSN (contains redis:// user:password@host)
            redacted[key] = "redacted"
        else:
            redacted[key] = value
    return redacted


# NOTE: Do not eagerly initialize storage at import time.
# GCS credential discovery can block (e.g., metadata server probing) in offline
# environments, which would hang tests and smoke scripts. Storage remains lazily
# initialized via `_get_storage_manager()` when an endpoint actually needs it.

app = FastAPI(title="MICA API v1", version="1.0.0")

# Rate limiting (defense-in-depth behind NGINX)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.on_event("startup")
async def _startup() -> None:
    if os.getenv("MICA_DISABLE_STARTUP_LIFESPAN") == "true":
        logging.getLogger("mica.startup").warning("FastAPI startup lifespan disabled via MICA_DISABLE_STARTUP_LIFESPAN")
        return
    # ── PHY_ONLY: minimal startup for PhY lane (skip KB/Redis/GCS/Milvus warm-up) ─
    # The full startup hook calls into KBPostgresStore, Redis ping, DCT Milvus warmup,
    # GCS storage, TimescaleGraphRAG, and product_schema. None of those are needed for
    # the phy_router (POST /api/v1/phy/cmd, GET /api/v1/phy/state, /health, /telemetry).
    # When PHY_ONLY=true, we still allow agent_feed pin (cheap, file-based) and observability,
    # but skip everything else to keep the API boot fast and offline-friendly.
    _phy_only = os.getenv("PHY_ONLY", "").strip().lower() in ("1", "true", "yes")
    if _phy_only:
        logging.getLogger("mica.startup").warning("PHY_ONLY=true — minimal startup (skip KB/Redis/GCS/Milvus warm-up)")
        try:
            from mica.observability.bootstrap import init_observability
            init_observability(component="api")
        except Exception as _exc:
            logging.getLogger("mica.startup").warning("observability init skipped: %s", _exc)
        try:
            from mica.phy.dispatcher import Dispatcher as _PhyDispatcher
            app.state.phy_dispatcher = _PhyDispatcher()
            logger.info("PhY dispatcher initialized (PHY_ONLY=true)")
        except Exception as _phy_init_err:
            logger.warning("PhY dispatcher init failed (PHY_ONLY): %s", _phy_init_err)
        return
    # Slice-7 §3 — initialize OTel (idempotent, no-op without endpoint).
    try:
        from mica.observability.bootstrap import init_observability
        init_observability(component="api")
    except Exception as _exc:
        logging.getLogger("mica.startup").warning("observability init skipped: %s", _exc)
    # ── Slice-4 §5: pin canonical agent-feed root for this process ─
    try:
        from mica.agentic.tools.agent_feed import pin_feed_root
        from pathlib import Path as _Path
        # Canonical: env override wins; otherwise repo root walks up from this file.
        _override = os.environ.get("MICA_AGENT_FEED_ROOT")
        if _override:
            _root = _Path(_override).expanduser().resolve()
        else:
            _here = _Path(__file__).resolve()
            _candidate = None
            for parent in [_here.parent, *_here.parents]:
                if (parent / ".mica").is_dir():
                    _candidate = parent / ".mica" / "agent_feed"
                    break
            _root = _candidate or (_here.parents[3] / ".mica" / "agent_feed")
        _root.mkdir(parents=True, exist_ok=True)
        pin_feed_root(_root)
        logger.info("agent_feed root pinned: %s", _root)
    except Exception as _feed_err:  # noqa: BLE001
        logger.warning("agent_feed pin skipped (non-fatal): %s", _feed_err)

    # ── Slice-4 §7: tool registry drift guard ─────────────────────
    try:
        from mica.agentic.tool_registry_check import verify_no_drift
        _names = verify_no_drift()
        logger.info("tool registry parity OK (%d tools)", len(_names))
    except Exception as _drift_err:  # noqa: BLE001
        # In production this MUST be fatal — a missing _spec means tool
        # invocations from the LLM will KeyError later. In dev we log loud.
        _env_mode = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or ""
        if _env_mode.lower() in ("prod", "production"):
            raise RuntimeError(f"tool registry drift: {_drift_err}") from _drift_err
        logger.error("tool registry drift (non-prod, allowing): %s", _drift_err)

    # ── Production readiness checks ──────────────────────────────
    _env_mode = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or ""
    _is_prod = _env_mode.lower() in ("prod", "production")
    if _is_prod:
        # Fail fast if permissive flags are set in production
        if os.getenv("ALLOW_DB_STARTUP_FAILURE", "").strip() == "1":
            raise RuntimeError(
                "ALLOW_DB_STARTUP_FAILURE=1 is FORBIDDEN in production. "
                "Database must be reachable before serving traffic."
            )
        if os.getenv("ASTROFLORA_TEST_NO_DB", "").strip() == "1":
            raise RuntimeError(
                "ASTROFLORA_TEST_NO_DB=1 is FORBIDDEN in production. "
                "Remove test overrides from production environment."
            )

    # Ensure tables exist (including new ACL/collaboration tables) before serving requests.
    _db_ready, _ = await await_nonfatal_startup_step(
        "initialize_database",
        initialize_database(),
        timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
    )
    if _is_prod and not _db_ready:
        raise RuntimeError(
            "Database startup initialization timed out or failed in production. "
            "The API must not serve traffic without a confirmed database startup path."
        )

    # ── Durable KB store (P0-1) ──────────────────────────────────
    # Wire KBPostgresStore → KBService so KB state survives restarts.
    _kb_store = None
    try:
        from mica.infrastructure.persistence.kb_postgres_store import KBPostgresStore

        _kb_store = KBPostgresStore()
        _kb_ready, _ = await await_nonfatal_startup_step(
            "KBPostgresStore",
            _kb_store.initialize(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        if _kb_ready:
            logger.info("KBPostgresStore initialized — KB state is DURABLE")
        else:
            _kb_store = None
    except Exception as _kb_err:
        from mica.infrastructure.persistence.pg_async import mask_dsn
        _safe_err = mask_dsn(str(_kb_err))
        _kb_store = None
        # KBPostgresStore is non-fatal even in production — falls back to in-memory KB.
        # External DB connectivity issues (Railway networking) must NOT block deploy.
        logger.error("KBPostgresStore unavailable (FALLBACK to in-memory KB): %s", _safe_err)

    from mica.pipelines.knowledge_fabric.kb_service import KBService
    from mica.pipelines.knowledge_fabric.document_scan_service import DocumentScanService

    app.state.kb_store = _kb_store
    app.state.kb_service = KBService(store=_kb_store)
    app.state.document_scan_service = None
    try:
        app.state.document_scan_service = DocumentScanService(store=_kb_store)
    except Exception as _document_scan_err:
        # DocumentScanService is non-fatal — same reasoning as KBPostgresStore.
        logger.error("DocumentScanService unavailable (non-fatal): %s", _document_scan_err)

    # ── G7: KBAutoIngestListener (pipeline_completed → report-derived KB) ─
    try:
        from src.services.kb_auto_ingest_listener import KBAutoIngestListener
        app.state.kb_auto_ingest_listener = KBAutoIngestListener(
            kb_service=app.state.kb_service,
        )
    except Exception as _kb_ingest_err:
        logger.warning("KBAutoIngestListener unavailable: %s", _kb_ingest_err)

    logger.info(
        "KBService ready (durable=%s)",
        _kb_store is not None,
    )

    # ── Redis pool warm-up (FIX-04) ──────────────────────────────
    try:
        async def _warm_redis() -> None:
            _redis = await get_redis()
            await _redis.ping()

        _redis_ready, _ = await await_nonfatal_startup_step(
            "redis_ping",
            _warm_redis(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        if _redis_ready:
            logger.info("Redis pool warmed — connection verified at startup")
        elif _is_prod:
            raise RuntimeError("Redis REQUIRED in production but startup warm-up did not complete in time.")
    except Exception as _redis_err:
        if _is_prod:
            raise RuntimeError(
                f"Redis REQUIRED in production but failed: {_redis_err}"
            ) from _redis_err
        logger.warning("Redis unavailable at startup (dev/test OK): %s", _redis_err)

    # ── DCT Milvus warm-up (interactive DCT search should not pay first-hit load) ─
    try:
        from mica.services.dct_milvus_search_service import get_dct_milvus_search_service

        _dct_collection = os.getenv("MICA_DCT_COLLECTION_NAME", "dctdomain_embeddings")

        async def _warm_dct() -> dict[str, Any]:
            return await asyncio.to_thread(
                get_dct_milvus_search_service().warmup,
                _dct_collection,
            )

        _dct_ready, _dct_state = await await_nonfatal_startup_step(
            "dct_milvus_warmup",
            _warm_dct(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        if _dct_ready and isinstance(_dct_state, dict):
            logger.info(
                "DCT Milvus warmed at startup (collection=%s, entities=%s, cached=%s)",
                _dct_state.get("collection"),
                _dct_state.get("entity_count"),
                _dct_state.get("collection_cached"),
            )
    except Exception as _dct_err:
        logger.warning("DCT Milvus warm-up skipped (non-fatal): %s", _dct_err)

    # ── GCS storage manager warm-up (so /health reports ready=True) ─────
    # The lazy singleton in mica.storage.gcs_user_storage only materializes
    # the first time an endpoint calls it, which makes /health advertise
    # "storage manager not initialized yet" forever when the API never
    # receives a storage-dependent call before the probe. Force it now.
    try:
        async def _warm_storage() -> None:
            await asyncio.to_thread(_shared_get_storage_manager)

        _storage_ready, _ = await await_nonfatal_startup_step(
            "gcs_storage_manager",
            _warm_storage(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        if _storage_ready:
            logger.info("GCSUserStorage manager initialized at startup")
    except HTTPException as _stor_err:
        # Shared helper raises HTTPException(503) when credentials missing.
        logger.warning(
            "GCS storage manager not materialized at startup (status=%s): %s",
            _stor_err.status_code, _stor_err.detail,
        )
    except Exception as _stor_err:
        logger.warning("GCS storage manager init failed (non-fatal): %s", _stor_err)

    # ── TSG-001: ensure TimescaleDB GraphRAG partial indexes ─────
    # CREATE INDEX CONCURRENTLY IF NOT EXISTS on atom_graph_edges + atom_facts.
    # Idempotent — safe on every startup.  Table-not-found exceptions are
    # caught internally and logged as warnings (graceful on first deploy).
    try:
        from mica.infrastructure.persistence.graphrag_write_facade import GraphRAGWriteFacade
        from mica.infrastructure.persistence.timescale_graphrag_store import TimescaleGraphRAGStore

        _graphrag_store = TimescaleGraphRAGStore()
        _graphrag_ready, _ = await await_nonfatal_startup_step(
            "GraphRAG store init",
            _graphrag_store.initialize(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        _graphrag_indexes_ready = False
        if _graphrag_ready:
            _graphrag_indexes_ready, _ = await await_nonfatal_startup_step(
                "GraphRAG global indexes",
                _graphrag_store.ensure_global_indexes(),
                timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
            )
        if _graphrag_ready and _graphrag_indexes_ready:
            app.state.graphrag_store = _graphrag_store
            app.state.graphrag_write_facade = GraphRAGWriteFacade(_graphrag_store)
            logger.info("GraphRAG store initialized and global partial indexes ensured (TSG-001)")
        else:
            app.state.graphrag_store = None
            app.state.graphrag_write_facade = None
    except Exception as _idx_err:
        app.state.graphrag_store = None
        app.state.graphrag_write_facade = None
        logger.warning("ensure_global_indexes failed (non-fatal): %s", _idx_err)

    # Feed -> Timescale projection is best-effort and never becomes feed authority.
    try:
        from mica.agentic.feed_timescale_projector import get_feed_timescale_projector

        _feed_projector = get_feed_timescale_projector()
        _started_ok, _started = await await_nonfatal_startup_step(
            "feed_timescale_projector",
            _feed_projector.start(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        if not _started_ok:
            _started = False
        app.state.feed_timescale_projector = _feed_projector if _started else None
        if _started:
            logger.info("feed_timescale_projector started")
        else:
            logger.info("feed_timescale_projector skipped (Timescale unavailable)")
    except Exception as _feed_projector_err:
        logger.warning("feed_timescale_projector unavailable (non-fatal): %s", _feed_projector_err)

    # ── Product Layer schema (P0 tables: studies, app_memories, snapshots, etc.) ─
    try:
        from mica.api_v1.product_schema import ensure_product_schema

        _schema_ready, _schema_ok = await await_nonfatal_startup_step(
            "product_schema",
            ensure_product_schema(),
            timeout_sec=_NONFATAL_STARTUP_TIMEOUT_SEC,
        )
        if _schema_ready and _schema_ok:
            logger.info("Product schema ensured (studies, app_memories, snapshots, working_sets, artifacts, file_records, search_intents)")
        elif _schema_ready:
            logger.warning("Product schema skipped — Neon unavailable")
    except Exception as _prod_schema_err:
        logger.warning("Product schema init failed (non-fatal): %s", _prod_schema_err)


@app.on_event("shutdown")
async def _shutdown() -> None:
    feed_projector = getattr(getattr(app, "state", None), "feed_timescale_projector", None)
    if feed_projector is not None:
        try:
            await feed_projector.stop()
        except Exception:
            pass
    await close_database_pool()
    # Close KB store pool
    kb_store = getattr(getattr(app, "state", None), "kb_store", None)
    if kb_store is not None:
        try:
            await kb_store.close()
        except Exception:
            pass
    try:
        from mica.security.governance import close_governance_settings_store

        await close_governance_settings_store()
    except Exception:
        # Best-effort cleanup; never block shutdown.
        pass
    try:
        from mica.api_v1.routers.serverless_models import close_serverless_model_gateways

        await close_serverless_model_gateways()
    except Exception:
        pass
    # FIX-01: Close shared Redis connection pool
    try:
        await close_redis()
    except Exception:
        pass

# ── P2-3: Per-IP rate limiter (in-memory, no external deps) ─────
_RATE_LIMIT_WINDOW = int(_env("RATE_LIMIT_WINDOW_S", "60"))
_RATE_LIMIT_MAX = int(_env("RATE_LIMIT_MAX_REQUESTS", "120"))
_RATE_LIMIT_MAX_IPS = int(_env("RATE_LIMIT_MAX_IPS", "10000"))
_rate_limit_state: Dict[str, list] = {}

# ── P2-4: Max request body size ─────────────────────────────────
_MAX_BODY_BYTES = int(_env("MAX_REQUEST_BODY_BYTES", str(50 * 1024 * 1024)))  # 50 MB


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Inject standard security headers into every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def body_size_limit_middleware(request: Request, call_next):
    """Reject requests with Content-Length exceeding the configured maximum."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_BYTES:
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"detail": f"Request body too large (max {_MAX_BODY_BYTES} bytes)"},
            status_code=413,
        )
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Sliding-window per-IP rate limiter."""
    client_ip = (request.client.host if request.client else "unknown")
    now = time.monotonic()
    window = _rate_limit_state.setdefault(client_ip, [])
    # Prune entries older than the window
    cutoff = now - _RATE_LIMIT_WINDOW
    _rate_limit_state[client_ip] = window = [t for t in window if t > cutoff]
    # Evict empty buckets to prevent unbounded dict growth
    if not window:
        _rate_limit_state.pop(client_ip, None)
    # Cap tracked IPs to prevent unbounded memory growth from IP rotation
    if len(_rate_limit_state) > _RATE_LIMIT_MAX_IPS:
        oldest_ip = min(_rate_limit_state, key=lambda k: _rate_limit_state[k][0] if _rate_limit_state[k] else 0)
        _rate_limit_state.pop(oldest_ip, None)
    if len(window) >= _RATE_LIMIT_MAX:
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"detail": "Rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
        )
    window.append(now)
    return await call_next(request)


# ── P1-SEC fix (2026-04-20): security headers (HSTS, CSP) ────────
# Add defense-in-depth response headers. Complements X-Frame-Options
# and X-Content-Type-Options that Railway / Starlette already add.
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    # HSTS: 2y + subdomains; preload-ready
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=63072000; includeSubDomains; preload",
    )
    # CSP: strict default; allow inline/script for Swagger UI on /docs only
    if request.url.path in ("/docs", "/redoc"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self' https://cdn.jsdelivr.net https://fastapi.tiangolo.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "frame-ancestors 'none'",
        )
    else:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'",
        )
    return response


# ── P0-SEC fix (2026-04-20): CORS hardening ──────────────────────
# Never allow wildcard "*" together with allow_credentials=True. That combo
# causes Starlette/FastAPI to reflect the request Origin, enabling CSRF /
# authenticated-request forgery from any malicious site.
# If the env accidentally contains "*", strip it and fall back to a sane
# default list. The operational fix is to also remove "*" from
# CORS_ALLOW_ORIGINS in the Railway service variables.
_cors_origins_hardened = build_cors_allow_origins(CORS_ALLOW_ORIGINS, CORS_EXTRA_ALLOW_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_hardened,
    allow_credentials=True,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
)

# Optional: expose the core MUDO CRUD/versioning API on the v1 surface.
try:  # pragma: no cover
    from routes.mudo import router as mudo_router

    app.include_router(mudo_router, prefix="/api/v1/mudo", tags=["mudo"])
except Exception as _e:  # pragma: no cover
    _MUDO_IMPORT_ERROR = str(_e)

try:  # pragma: no cover
    from mica.api_v1.routers.mudo import router as mudo_foundation_router
    from mica.api_v1.routers.mudo import study_router as mudo_study_router

    app.include_router(mudo_foundation_router)
    app.include_router(mudo_study_router)
except Exception as _e:  # pragma: no cover
    _MUDO_FOUNDATION_IMPORT_ERROR = str(_e)

# Jobs API for GPU orchestration (Team 1 + Team 2)
_JOBS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.jobs import router as jobs_router

    app.include_router(jobs_router, tags=["jobs"])
except Exception as _e:  # pragma: no cover
    _JOBS_IMPORT_ERROR = str(_e)

# Unified Compute API (W6-2: merges Jobs + MD into /api/v1/compute/)
_COMPUTE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.compute import router as compute_router

    app.include_router(compute_router, tags=["compute"])
except Exception as _e:  # pragma: no cover
    _COMPUTE_IMPORT_ERROR = str(_e)


@app.get("/api/v1/runtime/fingerprint")
async def runtime_fingerprint():
    from mica.api_v1.runtime_metadata import runtime_fingerprint_payload

    return runtime_fingerprint_payload(
        app=app,
        compute_router_loaded=_COMPUTE_IMPORT_ERROR is None,
        process_start_time=_PROCESS_START_TIME,
    )


@app.get("/api/v1/runtime/routes")
def runtime_routes():
    from mica.api_v1.runtime_metadata import runtime_routes_payload

    return runtime_routes_payload(app)

# Serverless model product API
_SERVERLESS_MODELS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.serverless_models import router as serverless_models_router

    app.include_router(serverless_models_router)
except Exception as _e:  # pragma: no cover
    _SERVERLESS_MODELS_IMPORT_ERROR = str(_e)

# Vertex SVG icons + pathways API
_ICON_GENERATION_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.icon_generation import router as icon_generation_router

    app.include_router(icon_generation_router)
except Exception as _e:  # pragma: no cover
    _ICON_GENERATION_IMPORT_ERROR = str(_e)

# Presenta deck validation API
_PRESENTA_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.presenta import router as presenta_router

    app.include_router(presenta_router)
except Exception as _e:  # pragma: no cover
    _PRESENTA_IMPORT_ERROR = str(_e)

# Governance API (approval gating + per-user settings)
_GOVERNANCE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.governance import router as governance_router

    app.include_router(governance_router, tags=["governance"])
except Exception as _e:  # pragma: no cover
    _GOVERNANCE_IMPORT_ERROR = str(_e)

# Knowledge Fabric — KB CRUD + public listing
_KB_FABRIC_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.knowledge_fabric import router as kb_fabric_router

    app.include_router(kb_fabric_router)
except Exception as _e:  # pragma: no cover
    _KB_FABRIC_IMPORT_ERROR = str(_e)

# Literature ingestion (DLM → PDFs → ATOM → Timescale user RAG)
_LITERATURE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.literature import router as literature_router

    app.include_router(literature_router)
except Exception as _e:  # pragma: no cover
    _LITERATURE_IMPORT_ERROR = str(_e)

# Alejandria Search product surface
_ALEJANDRIA_SEARCH_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.alejandria_search import router as alejandria_search_router

    app.include_router(alejandria_search_router)
except Exception as _e:  # pragma: no cover
    _ALEJANDRIA_SEARCH_IMPORT_ERROR = str(_e)

# Alejandria Library multimodal search surface
_LIBRARY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.library import router as library_router

    app.include_router(library_router)
except Exception as _e:  # pragma: no cover
    _LIBRARY_IMPORT_ERROR = str(_e)

# LMP v4 XML -> KnowledgeGraph endpoints + viewer
_GRAPH_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.graph import router as graph_router

    app.include_router(graph_router)
except Exception as _e:  # pragma: no cover
    _GRAPH_IMPORT_ERROR = str(_e)

_GRAPHRAG_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.graphrag import router as graphrag_router

    app.include_router(graphrag_router)
except Exception as _e:  # pragma: no cover
    _GRAPHRAG_IMPORT_ERROR = str(_e)

# Lane PhY — MICA Real World (physical execution: mock-first)
_PHY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.phy_router import router as phy_router

    app.include_router(phy_router)
except Exception as _e:  # pragma: no cover
    _PHY_IMPORT_ERROR = str(_e)
    log.warning("phy_router not mounted: %s", _e)

# LMP v4 Presets + Generation API — exposes all 9 NeSyMol presets
_LMP_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.lmp import router as lmp_router

    app.include_router(lmp_router)
except Exception as _e:  # pragma: no cover
    _LMP_IMPORT_ERROR = str(_e)

# LMP v4 Annotations — parsed JSON sections for human proteome cache (R-MuDO-03 §4.1)
_LMP_ANNOTATIONS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.lmp_annotations import router as lmp_annotations_router

    app.include_router(lmp_annotations_router)
except Exception as _e:  # pragma: no cover
    _LMP_ANNOTATIONS_IMPORT_ERROR = str(_e)

# Shared LMP catalog + promotion surface
_LMP_CATALOG_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.lmp_catalog import router as lmp_catalog_router

    app.include_router(lmp_catalog_router)
except Exception as _e:  # pragma: no cover
    _LMP_CATALOG_IMPORT_ERROR = str(_e)

# Workspace session storage
_WORKSPACE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.workspace import router as workspace_router

    app.include_router(workspace_router)
except Exception as _e:  # pragma: no cover
    _WORKSPACE_IMPORT_ERROR = str(_e)

# User-bucket native API
_USER_BUCKET_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.user_bucket import router as user_bucket_router

    app.include_router(user_bucket_router)
except Exception as _e:  # pragma: no cover
    _USER_BUCKET_IMPORT_ERROR = str(_e)

# Operator Directive relay surface (ODRC-2026-04-20)
_OPERATOR_DIRECTIVE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.operator_directive import router as operator_directive_router

    app.include_router(operator_directive_router)
except Exception as _e:  # pragma: no cover
    _OPERATOR_DIRECTIVE_IMPORT_ERROR = str(_e)

# Driver-as-Tool invocation surface (R28 Slice D.2 — Level-5 Pillar 1, Rung 0)
_DRIVER_INVOKE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.driver_invoke import router as driver_invoke_router

    app.include_router(driver_invoke_router)
except Exception as _e:  # pragma: no cover
    _DRIVER_INVOKE_IMPORT_ERROR = str(_e)

# Structure visualizer surface (BinaryCIF proxy)
_STRUCTURE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.structure_visualizer import router as structure_router

    app.include_router(structure_router)
except Exception as _e:  # pragma: no cover
    _STRUCTURE_IMPORT_ERROR = str(_e)

# Query enqueue + SSE stream surface
_QUERY_STREAM_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.stream import router as query_stream_router

    app.include_router(query_stream_router)
except Exception as _e:  # pragma: no cover
    _QUERY_STREAM_IMPORT_ERROR = str(_e)

# DLM literature scan presets
_DLM_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.dlm import router as dlm_router

    app.include_router(dlm_router)
except Exception as _e:  # pragma: no cover
    _DLM_IMPORT_ERROR = str(_e)

# Deep Research pipeline (citation-graph exploration)
_DEEP_RESEARCH_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.deep_research import router as deep_research_router

    app.include_router(deep_research_router, prefix="/api/v1/research", tags=["deep-research"])
except Exception as _e:  # pragma: no cover
    _DEEP_RESEARCH_IMPORT_ERROR = str(_e)

# Full Research Pipeline (driver→DLM→ATOM→bibliotecarios→driver)
_RESEARCH_PIPELINE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.research_pipeline import router as research_pipeline_router

    app.include_router(research_pipeline_router, prefix="/api/v1/research", tags=["research-pipeline"])
except Exception as _e:  # pragma: no cover
    _RESEARCH_PIPELINE_IMPORT_ERROR = str(_e)

# Bibliotecario Agent Router (entity scan, co-occurrence, temporal evolution, ATOM query, PDF harvest)
_BIBLIOTECARIO_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.bibliotecario import router as bibliotecario_router

    app.include_router(bibliotecario_router, prefix="/api/v1/research", tags=["bibliotecario"])
except Exception as _e:  # pragma: no cover
    _BIBLIOTECARIO_IMPORT_ERROR = str(_e)

# Kernel commands (Lane I Ronda 3/4 — closures & manifest freeze)
_KERNEL_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.kernel_router import router as kernel_router
    app.include_router(kernel_router)
    from mica.api_v1.routers.kernel_gateway import router as kernel_gateway_router
    app.include_router(kernel_gateway_router)
    from mica.api_v1.routers.agent_tool_manifest import router as agent_tool_manifest_router
    app.include_router(agent_tool_manifest_router)
    from mica.api_v1.routers.protocol_authoring_service import router as protocol_authoring_router
    app.include_router(protocol_authoring_router)
except Exception as _e:  # pragma: no cover
    _KERNEL_IMPORT_ERROR = str(_e)

# ── Product Layer: Studies, AppMemory, Snapshots, WindowGroups, Artifacts, Drive, SearchIntents ─
# P0/P1 tables — durable user-facing state (not localStorage)

# Studies
_STUDIES_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.studies import router as studies_router
    app.include_router(studies_router)
except Exception as _e:  # pragma: no cover
    _STUDIES_IMPORT_ERROR = str(_e)

# ArtifactMembership (APV-04)
_ARTIFACT_MEMBERSHIPS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.artifact_memberships import router as artifact_memberships_router
    app.include_router(artifact_memberships_router)
except Exception as _e:  # pragma: no cover
    _ARTIFACT_MEMBERSHIPS_IMPORT_ERROR = str(_e)

# EvidenceBinding / Findings (APV-05)
_EVIDENCE_BINDINGS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.evidence_bindings import router as evidence_bindings_router
    app.include_router(evidence_bindings_router)
except Exception as _e:  # pragma: no cover
    _EVIDENCE_BINDINGS_IMPORT_ERROR = str(_e)

# Experience BFF (APV-06)
_EXPERIENCE_BFF_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.experience_bff import router as experience_bff_router
    app.include_router(experience_bff_router)
except Exception as _e:  # pragma: no cover
    _EXPERIENCE_BFF_IMPORT_ERROR = str(_e)

# ProductEventEnvelope (APV-07)
_PRODUCT_EVENTS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.product_events import router as product_events_router
    app.include_router(product_events_router)
except Exception as _e:  # pragma: no cover
    _PRODUCT_EVENTS_IMPORT_ERROR = str(_e)

# Astroflora context router (APV-08)
_CONTEXT_ROUTER_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.context_router import router as context_router
    app.include_router(context_router)
except Exception as _e:  # pragma: no cover
    _CONTEXT_ROUTER_IMPORT_ERROR = str(_e)

# Native product event WS (APV-09)
_PRODUCT_EVENT_WS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.product_event_ws import router as product_event_ws_router
    app.include_router(product_event_ws_router)
except Exception as _e:  # pragma: no cover
    _PRODUCT_EVENT_WS_IMPORT_ERROR = str(_e)

# Investigation Lines (P0 product projection over Studies)
_INVESTIGATION_LINES_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.investigation_lines import router as investigation_lines_router
    app.include_router(investigation_lines_router)
except Exception as _e:  # pragma: no cover
    _INVESTIGATION_LINES_IMPORT_ERROR = str(_e)

# Orchestration (P0 Slice C — lab→execute→surface + Poltergeist)
_ORCHESTRATION_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.orchestration_router import router as orchestration_router
    app.include_router(orchestration_router)
except Exception as _e:  # pragma: no cover
    _ORCHESTRATION_IMPORT_ERROR = str(_e)

# Tenancy (T0.1 + T1 — roles, shares, policies)
_TENANCY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.tenancy import router as tenancy_router
    app.include_router(tenancy_router)
except Exception as _e:  # pragma: no cover
    _TENANCY_IMPORT_ERROR = str(_e)

# Product EffectiveContext (APV-01)
_PRODUCT_CONTEXT_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.product_context import router as product_context_router
    app.include_router(product_context_router)
except Exception as _e:  # pragma: no cover
    _PRODUCT_CONTEXT_IMPORT_ERROR = str(_e)

# Product EffectivePermission evaluate (APV-02)
_PERMISSIONS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.permissions import router as permissions_router
    app.include_router(permissions_router)
except Exception as _e:  # pragma: no cover
    _PERMISSIONS_IMPORT_ERROR = str(_e)

# App Memory
_APP_MEMORY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.app_memory import router as app_memory_router
    app.include_router(app_memory_router)
except Exception as _e:  # pragma: no cover
    _APP_MEMORY_IMPORT_ERROR = str(_e)

# Workspace Snapshots
_WORKSPACE_SNAPSHOTS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.workspace_snapshots import router as workspace_snapshots_router
    app.include_router(workspace_snapshots_router)
except Exception as _e:  # pragma: no cover
    _WORKSPACE_SNAPSHOTS_IMPORT_ERROR = str(_e)

# Working Sets (WorkingSets) — P0 product-layer, canonical name per SURFACE_COLLISION_AUDIT C-01
_WORKING_SETS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.working_sets import router as working_sets_router
    app.include_router(working_sets_router)
except Exception as _e:  # pragma: no cover
    _WORKING_SETS_IMPORT_ERROR = str(_e)

# Artifacts (unified model)
_ARTIFACTS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.artifacts import router as artifacts_router
    app.include_router(artifacts_router)
except Exception as _e:  # pragma: no cover
    _ARTIFACTS_IMPORT_ERROR = str(_e)

# Laboratories / lab memberships
_LABS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.labs import router as labs_router
    app.include_router(labs_router)
except Exception as _e:  # pragma: no cover
    _LABS_IMPORT_ERROR = str(_e)

# Knowledge Spaces
_KNOWLEDGE_SPACES_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.knowledge_spaces import router as knowledge_spaces_router
    app.include_router(knowledge_spaces_router)
except Exception as _e:  # pragma: no cover
    _KNOWLEDGE_SPACES_IMPORT_ERROR = str(_e)

# Research Lines
_RESEARCH_LINES_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.research_lines import router as research_lines_router
    app.include_router(research_lines_router)
except Exception as _e:  # pragma: no cover
    _RESEARCH_LINES_IMPORT_ERROR = str(_e)

# Biological Drive / File Records
_DRIVE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.drive import router as drive_router
    app.include_router(drive_router)
except Exception as _e:  # pragma: no cover
    _DRIVE_IMPORT_ERROR = str(_e)

# Search Intents (P1)
_SEARCH_INTENT_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.search_intent import router as search_intent_router
    app.include_router(search_intent_router)
except Exception as _e:  # pragma: no cover
    _SEARCH_INTENT_IMPORT_ERROR = str(_e)

# Job Truth (P0 — wraps TimescaleJobStore + events hypertable)
_JOB_TRUTH_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.job_truth import router as job_truth_router
    app.include_router(job_truth_router)
except Exception as _e:  # pragma: no cover
    _JOB_TRUTH_IMPORT_ERROR = str(_e)

# SMIC structural/molecular analysis (feature-flagged)
_SMIC_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.smic import router as smic_router

    app.include_router(smic_router, prefix="/api/v1/smic", tags=["smic"])
except Exception as _e:  # pragma: no cover
    _SMIC_IMPORT_ERROR = str(_e)

# Structure Preparation API (W2: pLDDT trim, domain extract, chimera, clash)
_STRUCTURE_PREP_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.structure_prep import router as structure_prep_router

    app.include_router(structure_prep_router, prefix="/api/v1/structure-prep", tags=["structure-prep"])
except Exception as _e:  # pragma: no cover
    _STRUCTURE_PREP_IMPORT_ERROR = str(_e)

# Literature Research Reports (v4.3 — L09B G3/G4)
_REPORTS_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.reports import router as reports_router

    app.include_router(reports_router)
except Exception as _e:  # pragma: no cover
    _REPORTS_IMPORT_ERROR = str(_e)

# Feed Self-Review Trigger (Phase 1 — token gate + Redis enqueue)
_FEED_REVIEW_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.feed_review import router as feed_review_router

    app.include_router(feed_review_router, prefix="/api/v1/feed", tags=["feed-review"])
except Exception as _e:  # pragma: no cover
    _FEED_REVIEW_IMPORT_ERROR = str(_e)

# MCP Standard Router (Slice 2 — JSON-RPC /mcp endpoint per MCP_STANDARDIZATION.md)
_MCP_STANDARD_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.mcp_standard import make_default_server
    from mica.mcp_standard.http_app import make_router as make_mcp_router
    import os as _mcp_os

    _mcp_workspace = _mcp_os.environ.get("MICA_MCP_WORKSPACE_ROOT", ".")
    _mcp_server = make_default_server(_mcp_workspace)
    app.include_router(make_mcp_router(_mcp_server))
except Exception as _e:  # pragma: no cover
    _MCP_STANDARD_IMPORT_ERROR = str(_e)


# -----------------------------
# Request models
# -----------------------------


class SignedUrlRequest(BaseModel):
    object_name: str = Field(..., description="File name to store")
    prefix: str = Field("uploads", description="Optional folder prefix in bucket")
    expires_in: int = Field(3600, ge=60, le=86400)
    content_type: Optional[str] = Field(None, description="Content-Type for PUT uploads")
    bytes_planned: Optional[int] = Field(None, description="Planned size in bytes for metrics")


class ClerkWebhookPayload(BaseModel):
    type: str
    data: Dict[str, str]


class AgenticPromptRequest(BaseModel):
    prompt: str = Field(..., description="Natural language prompt")
    mode: str = Field("production", description="production|research")
    session_id: Optional[str] = Field(None, description="Optional session id")
    mcp_enabled: bool = Field(True, description="Enable MCP tool calling")
    resource_fabric_enabled: bool = Field(False, description="Enable MCP resource fabric injection")


class ProtocolDraftStepRequest(BaseModel):
    id: str
    toolName: str = Field(..., description="Frontend tool identifier")
    params: Dict[str, object] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    label: str = ""
    kind: str = "tool"
    description: str = ""
    status: str = "pending"


class ProtocolDraftExecuteRequest(BaseModel):
    protocolJsonld: Dict[str, object] = Field(..., description="Canonical protocol.jsonld artifact")
    nodeReceipts: list[Dict[str, object]] = Field(
        default_factory=list,
        description="Previously completed durable node receipts used to unlock downstream protocol nodes",
    )
    id: str = ""
    name: str = ""
    steps: list[ProtocolDraftStepRequest] = Field(default_factory=list)
    description: str = ""
    goal: str = ""
    source: str = "frontend"
    metadata: Dict[str, object] = Field(default_factory=dict)
    mode: str = Field("production", description="production|research")
    session_id: Optional[str] = Field(None, description="Optional session id")
    mcp_enabled: bool = Field(True, description="Enable MCP tool calling")
    resource_fabric_enabled: bool = Field(False, description="Enable MCP resource fabric injection")
    study_id: Optional[str] = Field(None, description="Optional study scope for this protocol run")
    protocol_run_id: Optional[str] = Field(None, description="Optional pre-generated protocol run id (auto-generated if not provided)")


def _safe_user_dirname(user_id: str) -> str:
    raw = (user_id or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    if safe and safe != "_":
        return safe[:80]
    # Fall back to deterministic hash for user ids with only unsafe chars.
    return f"user_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _artifact_paths_for_session(*, checkpoint_dir: str, session_id: str, user_id: str = "") -> dict:
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "unknown")
    base = Path(checkpoint_dir).resolve()
    conversation_log = base / "conversation_logs" / f"{safe_session}.json"
    saga_log = base / "saga_logs" / f"{safe_session}.jsonl"
    run_dir = base / "run_manifests" / safe_session
    run_manifest = run_dir / "run_manifest.json"
    report_card = run_dir / "report_card.json"
    result: dict = {
        "conversation_log": {"path": str(conversation_log)},
        "saga_log": {"path": str(saga_log)},
        "run_manifest": {"path": str(run_manifest)},
        "report_card": {"path": str(report_card)},
    }

    # Append GCS signed download URLs when available.
    uid = (user_id or "").strip()
    if uid:
        try:
            from mica.drivers.persistence import DriverArtifactSync

            sync = DriverArtifactSync(user_id=uid)
            if sync.is_cloud_ready:
                for key, fname in [
                    ("conversation_log", f"{safe_session}.json"),
                    ("run_manifest", "run_manifest.json"),
                    ("report_card", "report_card.json"),
                ]:
                    url = sync.signed_download_url(session_id=session_id, filename=fname)
                    if url:
                        result[key]["gcs_url"] = url
                runs = sync.list_session_runs(session_id=session_id)
                if runs:
                    result["gcs_runs"] = runs

                # presenta router now registered at top-level startup
        except Exception:
            pass
    return result


async def _get_agentic_driver_for_request(*, user_id: str, mcp_enabled: bool, resource_fabric_enabled: bool):
    """Return a cached AgenticDriver keyed per user + feature flags.

    This avoids artifact collisions between users by scoping checkpoint_dir.
    """

    key = (_safe_user_dirname(user_id), bool(mcp_enabled), bool(resource_fabric_enabled))
    async with _agentic_driver_lock:
        _evict_stale_drivers()
        cached = _agentic_driver_cache.get(key)
        if cached is not None:
            driver, _ts = cached
            # Refresh timestamp on access
            _agentic_driver_cache[key] = (driver, time.monotonic())
            return driver

        from mica.drivers.agentic_driver import AgenticDriver, AgenticDriverConfig

        cfg = AgenticDriverConfig.from_driver_config()
        cfg.checkpoint_dir = str(Path(cfg.checkpoint_dir) / "api_v1" / key[0])
        cfg.mcp_enabled = bool(mcp_enabled)
        cfg.mcp_resources_enabled = bool(resource_fabric_enabled)

        drv = AgenticDriver(config=cfg)
        try:
            await drv.initialize_async()
        except Exception:
            # Best-effort: allow API to respond even if MCP connectivity is down.
            pass

        _agentic_driver_cache[key] = (drv, time.monotonic())
        return drv


def _queue_agentic_prompt_job(
    *,
    prompt: str,
    mode: str,
    session_id: str | None,
    mcp_enabled: bool,
    resource_fabric_enabled: bool,
    user_id: str,
    background_tasks: BackgroundTasks,
    request_metadata: Dict[str, object] | None = None,
) -> Dict[str, object]:
    _evict_stale_jobs()

    job_id = _uuid_mod.uuid4().hex
    job = _JobRecord(job_id=job_id, user_id=user_id)
    _agentic_jobs[job_id] = job

    async def _run_job() -> None:
        job.status = _JobStatus.RUNNING
        try:
            drv = await _get_agentic_driver_for_request(
                user_id=user_id,
                mcp_enabled=mcp_enabled,
                resource_fabric_enabled=resource_fabric_enabled,
            )

            bucket_name: Optional[str] = None
            try:
                sm = _get_storage_manager()
                bucket_name = sm.ensure_bucket(user_id).bucket_name
            except Exception:
                bucket_name = None

            result = await drv.process_agentic_prompt(
                user_query=prompt,
                mode=mode or "production",
                session_id=session_id,
                user_id=user_id,
                bucket=bucket_name,
            )

            resolved_session_id = (result or {}).get("session_id") or session_id or "unknown"
            artifacts = _artifact_paths_for_session(
                checkpoint_dir=str(getattr(drv, "config").checkpoint_dir),
                session_id=resolved_session_id,
                user_id=user_id,
            )
            final_result = (result or {}).get("final_result") if isinstance(result, dict) else None
            materialization_policy = None
            if isinstance(final_result, dict):
                materialization_policy = final_result.get("materialization_policy")
            if materialization_policy is None and isinstance(result, dict):
                materialization_policy = result.get("materialization_policy")

            try:
                from mica.infrastructure.persistence.session_repository import NeonSessionRepository

                repo = NeonSessionRepository()
                await repo.save_session(
                    session_id=resolved_session_id,
                    user_id=user_id,
                    conversation_history=[],
                    mode=mode or "production",
                    metadata={"bucket": bucket_name, "source": "api_v1", **(request_metadata or {})},
                )
                await repo.append_message(
                    session_id=resolved_session_id,
                    role="user",
                    content=prompt,
                    metadata={"bucket": bucket_name, **(request_metadata or {})},
                )
                if final_result is not None:
                    await repo.append_message(
                        session_id=resolved_session_id,
                        role="assistant",
                        content=str(final_result),
                        metadata={"bucket": bucket_name, **(request_metadata or {})},
                    )
                await repo.close()
            except Exception:
                pass

            job.result = {
                "ok": True,
                "user_id": user_id,
                "session_id": resolved_session_id,
                "bucket": bucket_name,
                "answer": final_result,
                "materialization_policy": materialization_policy,
                "artifacts": artifacts,
                "raw": result,
            }
            if request_metadata:
                job.result["request_metadata"] = request_metadata
            job.status = _JobStatus.DONE

        except Exception as exc:
            job.error = str(exc)
            job.status = _JobStatus.ERROR
        finally:
            job.finished_at = time.time()

    background_tasks.add_task(_run_job)
    return {
        "ok": True,
        "job_id": job_id,
        "status": job.status,
        "message": "Job queued. Poll GET /api/v1/agentic/jobs/{job_id} for result.",
    }


def _protocol_executor_checkpoint_dir(*, user_id: str) -> str:
    from mica.drivers.agentic_driver import AgenticDriverConfig

    cfg = AgenticDriverConfig.from_driver_config()
    checkpoint_dir = Path(cfg.checkpoint_dir) / "api_v1" / _safe_user_dirname(user_id) / "protocol_executor"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return str(checkpoint_dir)


# ---------------------------------------------------------------------------
# Protocol Executor Persistence (TimescaleDB + Neon)
# ---------------------------------------------------------------------------

_PROTOCOL_TS_POOL = None
_PROTOCOL_NEON_POOL = None


async def _ensure_protocol_timescale_schema(conn) -> None:
    """Ensure protocol runtime persistence tables/columns exist on Timescale."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT,
            worker_type TEXT NOT NULL DEFAULT 'protocol_executor',
            status TEXT NOT NULL DEFAULT 'pending',
            gpu_type TEXT,
            gpu_count INTEGER DEFAULT 1,
            provider TEXT,
            instance_id TEXT,
            docker_image TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            total_cost_usd DOUBLE PRECISION DEFAULT 0.0,
            error_message TEXT,
            checkpoint_gcs_path TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            protocol_run_id TEXT,
            protocol_id TEXT,
            study_id TEXT,
            result JSONB
        )
        """,
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS worker_type TEXT NOT NULL DEFAULT 'protocol_executor'",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpu_type TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpu_count INTEGER DEFAULT 1",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provider TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS instance_id TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS docker_image TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS total_cost_usd DOUBLE PRECISION DEFAULT 0.0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS checkpoint_gcs_path TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS protocol_run_id TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS protocol_id TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS study_id TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result JSONB",
        "CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_protocol_run_id ON jobs(protocol_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_study_id ON jobs(study_id)",
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY DEFAULT md5(random()::text || clock_timestamp()::text),
            event_type TEXT NOT NULL,
            job_id TEXT,
            instance_id TEXT,
            provider TEXT,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
            sequence_id BIGINT,
            payload JSONB DEFAULT '{}'::jsonb,
            metadata JSONB DEFAULT '{}'::jsonb,
            node_id TEXT NOT NULL DEFAULT 'mica_api',
            protocol_run_id TEXT,
            node_status TEXT,
            user_id TEXT,
            session_id TEXT,
            data JSONB DEFAULT '{}'::jsonb
        )
        """,
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS job_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS instance_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS provider TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS sequence_id BIGINT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS node_id TEXT NOT NULL DEFAULT 'mica_api'",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS protocol_run_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS node_status TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS user_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS data JSONB DEFAULT '{}'::jsonb",
        "CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_protocol_run_id ON events(protocol_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)",
    ]
    for statement in statements:
        await conn.execute(statement)
    try:
        await conn.execute("SELECT create_hypertable('events', 'timestamp', if_not_exists => TRUE)")
    except Exception as exc:
        logger.debug("events hypertable ensure skipped: %s", exc)


async def _ensure_protocol_neon_schema(conn) -> None:
    """Ensure durable protocol receipt schema exists on Neon."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS job_receipts (
            receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            protocol_run_id TEXT,
            node_id TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            outputs JSONB DEFAULT '[]'::jsonb,
            cost_estimate_usd NUMERIC(10,6),
            cost_actual_usd NUMERIC(10,6),
            duration_seconds INTEGER,
            provider TEXT,
            provenance_refs TEXT[] DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS protocol_run_id TEXT",
        "ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS node_id TEXT",
        "ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'",
        "ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS outputs JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS cost_actual_usd NUMERIC(10,6)",
        "ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS duration_seconds INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_job_receipts_job ON job_receipts(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_job_receipts_user ON job_receipts(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_job_receipts_protocol_run_id ON job_receipts(protocol_run_id)",
    ]
    for statement in statements:
        await conn.execute(statement)

async def _get_protocol_ts_pool():
    """Get or create a TimescaleDB pool for protocol executor persistence."""
    global _PROTOCOL_TS_POOL
    if _PROTOCOL_TS_POOL is not None:
        return _PROTOCOL_TS_POOL
    import asyncpg as _asyncpg
    dsn = choose_timescale_database_url()
    if not dsn:
        return None
    _PROTOCOL_TS_POOL = await _asyncpg.create_pool(
        dsn, min_size=1, max_size=3,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    async with _PROTOCOL_TS_POOL.acquire() as conn:
        await _ensure_protocol_timescale_schema(conn)
    logger.info("Protocol executor TS pool created: %s", mask_dsn(dsn))
    return _PROTOCOL_TS_POOL

async def _get_protocol_neon_pool():
    """Get or create a Neon pool for protocol receipt persistence."""
    global _PROTOCOL_NEON_POOL
    if _PROTOCOL_NEON_POOL is not None:
        return _PROTOCOL_NEON_POOL
    import asyncpg as _asyncpg
    dsn = choose_neon_database_url()
    if not dsn:
        return None
    _PROTOCOL_NEON_POOL = await _asyncpg.create_pool(
        dsn, min_size=1, max_size=3,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    async with _PROTOCOL_NEON_POOL.acquire() as conn:
        await _ensure_protocol_neon_schema(conn)
    logger.info("Protocol executor Neon pool created: %s", mask_dsn(dsn))
    return _PROTOCOL_NEON_POOL

async def _persist_job_to_timescale(
    job_id: str,
    user_id: str,
    protocol_run_id: str,
    protocol_id: str = "",
    study_id: str | None = None,
    status: str = "queued",
    result: dict | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    """Persist protocol executor job metadata to TimescaleDB jobs table."""
    import json as _json
    try:
        pool = await _get_protocol_ts_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO jobs (job_id, user_id, worker_type, status, protocol_run_id, protocol_id, study_id,
                   result, error_message, created_at, started_at, completed_at)
                   VALUES ($1, $2, 'protocol_executor', $3, $4, $5, $6, $7, $8, $9, $10, $11)
                   ON CONFLICT (job_id) DO UPDATE SET
                   status = EXCLUDED.status,
                   result = EXCLUDED.result,
                   error_message = EXCLUDED.error_message,
                   started_at = COALESCE(EXCLUDED.started_at, jobs.started_at),
                   completed_at = COALESCE(EXCLUDED.completed_at, jobs.completed_at)""",
                job_id,
                user_id,
                status,
                protocol_run_id,
                protocol_id or "",
                study_id,
                _json.dumps(result) if result else None,
                error_message,
                created_at or datetime.now(timezone.utc),
                started_at,
                completed_at,
            )
    except Exception as exc:
        logger.warning("Failed to persist job %s to TimescaleDB: %s", job_id, exc)

async def _persist_event_to_timescale(
    job_id: str,
    event_type: str,
    payload: dict | None = None,
    *,
    protocol_run_id: str = "",
    node_id: str = "",
    node_status: str = "",
    user_id: str = "",
) -> None:
    """Persist a node-level event to TimescaleDB events hypertable."""
    import json as _json
    try:
        pool = await _get_protocol_ts_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO events (job_id, event_type, node_id, protocol_run_id, node_status, user_id, data, timestamp)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, now())""",
                job_id,
                event_type,
                node_id or "",
                protocol_run_id or "",
                node_status or "",
                user_id or "",
                _json.dumps(payload) if payload else "{}",
            )
    except Exception as exc:
        logger.warning("Failed to persist event %s for job %s: %s", event_type, job_id, exc)

async def _persist_receipt_to_neon(
    job_id: str,
    user_id: str,
    receipt_payload: dict | None = None,
    *,
    protocol_run_id: str = "",
    node_id: str = "",
    status: str = "completed",
    outputs: list | None = None,
    cost_actual_usd: float | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Persist a job/node receipt to Neon job_receipts table."""
    import json as _json
    try:
        pool = await _get_protocol_neon_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO job_receipts (job_id, user_id, protocol_run_id, node_id, status, outputs, cost_actual_usd, duration_seconds)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                   ON CONFLICT DO NOTHING""",
                job_id,
                user_id,
                protocol_run_id or "",
                node_id or "",
                status,
                _json.dumps(outputs) if outputs else "[]",
                cost_actual_usd,
                int(duration_seconds) if duration_seconds else None,
            )
    except Exception as exc:
        logger.warning("Failed to persist receipt for job %s: %s", job_id, exc)


def _queue_protocol_executor_job(
    *,
    executor_request: Any,
    fallback_prompt: str,
    mode: str,
    session_id: str | None,
    mcp_enabled: bool,
    resource_fabric_enabled: bool,
    user_id: str,
    background_tasks: BackgroundTasks,
    request_metadata: Dict[str, object] | None = None,
    study_id: str | None = None,
    protocol_run_id: str | None = None,
) -> Dict[str, object]:
    _evict_stale_jobs()

    merged_request_metadata = dict(request_metadata or {})
    merged_request_metadata.update(
        build_protocol_dispatch_metadata(
            executor_request,
            legacy_fallback_enabled=bool(fallback_prompt),
        )
    )
    protocol_id = str(merged_request_metadata.get("protocol_id") or executor_request.protocol_id or "").strip()
    idempotency_key = str(merged_request_metadata.get("idempotency_key") or "").strip()
    if idempotency_key:
        for existing in _agentic_jobs.values():
            existing_metadata = dict(getattr(existing, "request_metadata", {}) or {})
            if existing.user_id != user_id:
                continue
            if str(existing_metadata.get("idempotency_key") or "").strip() != idempotency_key:
                continue
            if str(existing_metadata.get("protocol_id") or "").strip() != protocol_id:
                continue
            if study_id and str(existing_metadata.get("study_id") or "").strip() != str(study_id).strip():
                continue
            return {
                "ok": True,
                "job_id": existing.job_id,
                "protocol_run_id": str(existing_metadata.get("protocol_run_id") or ""),
                "status": existing.status,
                "message": "Protocol executor job reused from idempotency key.",
                "idempotent_replay": True,
                "tracking": {
                    "job_timeline": f"/api/v1/jobs/{existing.job_id}/timeline",
                    "job_ui_state": f"/api/v1/jobs/{existing.job_id}/ui-state",
                    "agentic_job": f"/api/v1/agentic/jobs/{existing.job_id}",
                },
            }

    job_id = _uuid_mod.uuid4().hex
    if not protocol_run_id:
        protocol_run_id = _uuid_mod.uuid4().hex
    merged_request_metadata["protocol_run_id"] = protocol_run_id
    if study_id:
        merged_request_metadata["study_id"] = study_id
    merged_request_metadata["job_id"] = job_id

    job = _JobRecord(job_id=job_id, user_id=user_id, request_metadata=merged_request_metadata)
    _agentic_jobs[job_id] = job

    async def _run_job() -> None:
        job.status = _JobStatus.RUNNING
        started_dt = datetime.now(timezone.utc)
        # Persist job creation + start to TimescaleDB (source of truth)
        await _persist_job_to_timescale(
            job_id=job_id, user_id=user_id,
            protocol_run_id=protocol_run_id,
            protocol_id=merged_request_metadata.get("protocol_id", ""),
            study_id=study_id,
            status="running", started_at=started_dt,
        )
        await _persist_event_to_timescale(
            job_id=job_id, event_type="protocol.job.queued",
            protocol_run_id=protocol_run_id, user_id=user_id,
            payload={"protocol_id": merged_request_metadata.get("protocol_id", ""), "study_id": study_id},
        )
        await _persist_event_to_timescale(
            job_id=job_id, event_type="protocol.job.started",
            protocol_run_id=protocol_run_id, user_id=user_id,
        )
        try:
            checkpoint_dir = _protocol_executor_checkpoint_dir(user_id=user_id)
            outcome = await execute_protocol_executor_request(
                executor_request,
                checkpoint_dir=checkpoint_dir,
            )
            resolved_session_id = executor_request.session_id or session_id or "unknown"
            run_receipt_payload = outcome.run_receipt.model_dump(mode="json")
            node_receipts_payload = [receipt.model_dump(mode="json") for receipt in outcome.node_receipts]

            # Persist node-level events for each node receipt
            for receipt in outcome.node_receipts:
                rd = receipt.model_dump(mode="json") if hasattr(receipt, 'model_dump') else receipt
                node_id = rd.get("node_id", "") if isinstance(rd, dict) else getattr(receipt, "node_id", "")
                node_status = rd.get("status", "completed") if isinstance(rd, dict) else getattr(receipt, "status", "completed")
                event_type = f"protocol.node.{node_status}"
                await _persist_event_to_timescale(
                    job_id=job_id, event_type=event_type,
                    protocol_run_id=protocol_run_id,
                    node_id=str(node_id),
                    node_status=str(node_status),
                    user_id=user_id,
                    payload=rd if isinstance(rd, dict) else {},
                )
                # Persist individual node receipt to Neon
                await _persist_receipt_to_neon(
                    job_id=job_id, user_id=user_id,
                    protocol_run_id=protocol_run_id,
                    node_id=str(node_id),
                    status=str(node_status),
                    outputs=rd.get("artifact_refs", []) if isinstance(rd, dict) else [],
                )
            # Persist run-level receipt
            await _persist_receipt_to_neon(
                job_id=job_id, user_id=user_id,
                protocol_run_id=protocol_run_id,
                status=outcome.run_receipt.status,
                outputs=list(outcome.run_receipt.artifact_refs),
            )
            # Persist run completed event
            await _persist_event_to_timescale(
                job_id=job_id, event_type="protocol.run.completed",
                protocol_run_id=protocol_run_id, user_id=user_id,
                node_status=outcome.run_receipt.status,
                payload={"artifact_refs": list(outcome.run_receipt.artifact_refs)},
            )
            job.result = {
                "ok": outcome.failure_message is None and outcome.run_receipt.status == "completed",
                "user_id": user_id,
                "session_id": resolved_session_id,
                "protocol_id": executor_request.protocol_id,
                "protocol_run_id": protocol_run_id,
                "study_id": study_id,
                "answer": run_receipt_payload,
                "run_receipt": run_receipt_payload,
                "node_receipts": node_receipts_payload,
                "failure_message": outcome.failure_message,
                "legacy_fallback_prompt": fallback_prompt,
                "artifacts": {
                    "checkpoint_dir": checkpoint_dir,
                    "artifact_refs": list(outcome.run_receipt.artifact_refs),
                    "evidence_refs": list(outcome.run_receipt.evidence_refs),
                    "projection_message_ids": list(outcome.projection_message_ids),
                },
                "raw": {
                    "run_receipt": run_receipt_payload,
                    "node_receipts": node_receipts_payload,
                    "projection_message_ids": list(outcome.projection_message_ids),
                },
            }
            if merged_request_metadata:
                job.result["request_metadata"] = merged_request_metadata

            if outcome.failure_message or outcome.run_receipt.status == "failed":
                job.error = outcome.failure_message or "Protocol executor run failed"
                job.status = _JobStatus.ERROR
                # Persist job failure
                await _persist_job_to_timescale(
                    job_id=job_id, user_id=user_id,
                    protocol_run_id=protocol_run_id,
                    protocol_id=merged_request_metadata.get("protocol_id", ""),
                    study_id=study_id, status="failed",
                    error_message=job.error,
                    completed_at=datetime.now(timezone.utc),
                )
                await _persist_event_to_timescale(
                    job_id=job_id, event_type="protocol.job.failed",
                    protocol_run_id=protocol_run_id, user_id=user_id,
                    payload={"error": job.error},
                )
            else:
                job.status = _JobStatus.DONE
                # Persist job completion with result
                await _persist_job_to_timescale(
                    job_id=job_id, user_id=user_id,
                    protocol_run_id=protocol_run_id,
                    protocol_id=merged_request_metadata.get("protocol_id", ""),
                    study_id=study_id, status="completed",
                    result=job.result,
                    completed_at=datetime.now(timezone.utc),
                )
                await _persist_event_to_timescale(
                    job_id=job_id, event_type="protocol.job.completed",
                    protocol_run_id=protocol_run_id, user_id=user_id,
                )
        except Exception as exc:
            job.error = str(exc)
            job.status = _JobStatus.ERROR
            # Persist exception failure
            await _persist_job_to_timescale(
                job_id=job_id, user_id=user_id,
                protocol_run_id=protocol_run_id,
                protocol_id=merged_request_metadata.get("protocol_id", ""),
                study_id=study_id, status="failed",
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            await _persist_event_to_timescale(
                job_id=job_id, event_type="protocol.job.failed",
                protocol_run_id=protocol_run_id, user_id=user_id,
                payload={"error": str(exc)},
            )
        finally:
            job.finished_at = time.time()

    background_tasks.add_task(_run_job)
    return {
        "ok": True,
        "job_id": job_id,
        "protocol_run_id": protocol_run_id,
        "status": job.status,
        "message": "Protocol executor job queued. Poll GET /api/v1/agentic/jobs/{job_id} for result.",
        "tracking": {
            "job_timeline": f"/api/v1/jobs/{job_id}/timeline",
            "job_ui_state": f"/api/v1/jobs/{job_id}/ui-state",
            "agentic_job": f"/api/v1/agentic/jobs/{job_id}",
        },
    }


# -----------------------------
# Routes
# -----------------------------


async def _probe_database_dsn(label: str, dsn: str | None, timeout: int = 5) -> Dict[str, object]:
    if not dsn:
        return {"status": "not_configured"}

    conn = None
    try:
        connect_kwargs = asyncpg_connection_kwargs_for_database_url(dsn)
        connect_kwargs["timeout"] = timeout
        conn = await asyncpg.connect(**connect_kwargs)
        row = await conn.fetchrow("SELECT current_database() AS db, current_user AS usr")
        parsed = urlparse(dsn)
        return {
            "status": "ok",
            "dsn": mask_dsn(dsn),
            "host": parsed.hostname,
            "database": row["db"] if row else None,
            "user": row["usr"] if row else None,
        }
    except Exception as exc:
        parsed = urlparse(dsn)
        return {
            "status": "error",
            "dsn": mask_dsn(dsn),
            "host": parsed.hostname,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        if conn is not None:
            await conn.close()


async def _database_readiness_checks() -> Dict[str, object]:
    neon = choose_neon_database_url()
    timescale = choose_timescale_database_url()
    neon_check = await _probe_database_dsn("neon", neon)
    timescale_check = await _probe_database_dsn("timescale", timescale)
    sessions_schema_check = await inspect_neon_sessions_table_contract(neon) if neon else {"status": "not_configured"}

    overall = "ok"
    if (neon and neon_check.get("status") != "ok") or (timescale and timescale_check.get("status") != "ok"):
        overall = "degraded"
    elif sessions_schema_check.get("status") not in {"ok", "not_configured"}:
        overall = "degraded"

    return {
        "status": overall,
        "neon": neon_check,
        "neon_sessions_schema": sessions_schema_check,
        "timescale": timescale_check,
    }


@app.get("/health")
@app.get("/api/v1/health")
async def health():
    # Workspace backend status (gcs vs local)
    _ws_backend = {}
    try:
        from mica.api_v1.routers.workspace import workspace_backend_status
        _ws_backend = workspace_backend_status()
    except Exception:
        _ws_backend = {"backend": "unknown", "status": "import_error"}

    storage = _storage_status()
    router_states = {
        "graph": "ok" if not _GRAPH_IMPORT_ERROR else _GRAPH_IMPORT_ERROR,
        "lmp": "ok" if not _LMP_IMPORT_ERROR else _LMP_IMPORT_ERROR,
        "lmp_annotations": "ok" if not _LMP_ANNOTATIONS_IMPORT_ERROR else _LMP_ANNOTATIONS_IMPORT_ERROR,
        "dlm": "ok" if not _DLM_IMPORT_ERROR else _DLM_IMPORT_ERROR,
        "literature": "ok" if not _LITERATURE_IMPORT_ERROR else _LITERATURE_IMPORT_ERROR,
        "alejandria_search": "ok" if not _ALEJANDRIA_SEARCH_IMPORT_ERROR else _ALEJANDRIA_SEARCH_IMPORT_ERROR,
        "jobs": "ok" if not _JOBS_IMPORT_ERROR else _JOBS_IMPORT_ERROR,
        "compute": "ok" if not _COMPUTE_IMPORT_ERROR else _COMPUTE_IMPORT_ERROR,
        "query": "ok" if not _QUERY_STREAM_IMPORT_ERROR else _QUERY_STREAM_IMPORT_ERROR,
        "serverless_models": "ok" if not _SERVERLESS_MODELS_IMPORT_ERROR else _SERVERLESS_MODELS_IMPORT_ERROR,
        "icon_generation": "ok" if not _ICON_GENERATION_IMPORT_ERROR else _ICON_GENERATION_IMPORT_ERROR,
        "structure": "ok" if not _STRUCTURE_IMPORT_ERROR else _STRUCTURE_IMPORT_ERROR,
        "governance": "ok" if not _GOVERNANCE_IMPORT_ERROR else _GOVERNANCE_IMPORT_ERROR,
        "workspace": "ok" if not _WORKSPACE_IMPORT_ERROR else _WORKSPACE_IMPORT_ERROR,
        "deep_research": "ok" if not _DEEP_RESEARCH_IMPORT_ERROR else _DEEP_RESEARCH_IMPORT_ERROR,
        "research_pipeline": "ok" if not _RESEARCH_PIPELINE_IMPORT_ERROR else _RESEARCH_PIPELINE_IMPORT_ERROR,
        "smic": "ok" if not _SMIC_IMPORT_ERROR else _SMIC_IMPORT_ERROR,
        "structure_prep": "ok" if not _STRUCTURE_PREP_IMPORT_ERROR else _STRUCTURE_PREP_IMPORT_ERROR,
        "presenta": "ok" if not _PRESENTA_IMPORT_ERROR else _PRESENTA_IMPORT_ERROR,
        "reports": "ok" if not _REPORTS_IMPORT_ERROR else _REPORTS_IMPORT_ERROR,
        "feed_review": "ok" if not _FEED_REVIEW_IMPORT_ERROR else _FEED_REVIEW_IMPORT_ERROR,
        "driver_invoke": "ok" if not _DRIVER_INVOKE_IMPORT_ERROR else _DRIVER_INVOKE_IMPORT_ERROR,
        "mcp_standard": "ok" if not _MCP_STANDARD_IMPORT_ERROR else _MCP_STANDARD_IMPORT_ERROR,
    }
    overall_status = "ok"
    if not storage.get("ready", storage.get("configured", False)):
        overall_status = "degraded"
    elif any(value != "ok" for value in router_states.values()):
        overall_status = "degraded"

    # Slice-4 §1: live DB probes (Neon + Timescale). Never raises.
    try:
        from mica.api_v1.health_probes import probe_all as _probe_all_dbs
        from mica.api_v1.health_probes import summarize as _db_summary
        databases = await _probe_all_dbs(timeout=1.5)
        if _db_summary(databases) == "degraded":
            overall_status = "degraded"
    except Exception as _hp_exc:  # noqa: BLE001
        databases = {
            "neon": {"kind": "neon", "configured": False, "ok": False,
                     "latency_ms": 0, "status": "probe_error"},
            "timescale": {"kind": "timescale", "configured": False, "ok": False,
                          "latency_ms": 0, "status": "probe_error"},
        }

    # P0-SEC fix (2026-04-20): unauth callers must not see GCP project ID
    # (fingerprinting) or raw router import error strings (may contain paths).
    # Emit only `ok`/`degraded` per router, and drop `project`. Authenticated
    # admins can get the full shape via /api/v1/admin/health (future).
    safe_routers = {
        name: ("ok" if state == "ok" else "degraded")
        for name, state in router_states.items()
    }
    safe_storage = {
        "configured": bool(storage.get("configured", False)),
        "ready": bool(storage.get("ready", False)),
    }
    safe_workspace = {
        "backend": _ws_backend.get("backend", "unknown") if isinstance(_ws_backend, dict) else "unknown",
        "status": _ws_backend.get("status", "unknown") if isinstance(_ws_backend, dict) else "unknown",
    }

    sc_raw = _semantic_cache_health()
    safe_semantic_cache = {
        "status": sc_raw.get("status", "unknown"),
        "enabled": bool(sc_raw.get("enabled", False)),
        "configured": bool(sc_raw.get("configured", False)),
    }

    return {
        "status": overall_status,
        "timestamp": int(time.time()),
        "storage": safe_storage,
        "workspace": safe_workspace,
        "semantic_cache": safe_semantic_cache,
        "routers": safe_routers,
        "databases": databases,
    }


@app.get("/api/v1/ready")
@app.get("/api/v1/readiness")
async def readiness():
    checks: Dict[str, object] = {}
    ready = True

    checks["storage"] = _storage_status()
    if not checks["storage"].get("ready", checks["storage"].get("configured", False)):
        ready = False

    try:
        redis_url = _env("REDIS_URL", "")
        if not redis_url:
            checks["redis"] = {"status": "unconfigured"}
        else:
            # FIX-02: Use async singleton instead of blocking redis.from_url()
            _redis_client = await get_redis()
            await _redis_client.ping()
            checks["redis"] = "ok"
    except Exception as exc:
        ready = False
        checks["redis"] = {"status": "error", "error": str(exc)}

    routers_failed = {
        name: err
        for name, err in {
            "graph": _GRAPH_IMPORT_ERROR,
            "lmp": _LMP_IMPORT_ERROR,
            "dlm": _DLM_IMPORT_ERROR,
            "literature": _LITERATURE_IMPORT_ERROR,
            "alejandria_search": _ALEJANDRIA_SEARCH_IMPORT_ERROR,
            "jobs": _JOBS_IMPORT_ERROR,
            "compute": _COMPUTE_IMPORT_ERROR,
            "query": _QUERY_STREAM_IMPORT_ERROR,
            "serverless_models": _SERVERLESS_MODELS_IMPORT_ERROR,
            "icon_generation": _ICON_GENERATION_IMPORT_ERROR,
            "structure": _STRUCTURE_IMPORT_ERROR,
            "governance": _GOVERNANCE_IMPORT_ERROR,
            "workspace": _WORKSPACE_IMPORT_ERROR,
            "deep_research": _DEEP_RESEARCH_IMPORT_ERROR,
            "research_pipeline": _RESEARCH_PIPELINE_IMPORT_ERROR,
            "smic": _SMIC_IMPORT_ERROR,
            "structure_prep": _STRUCTURE_PREP_IMPORT_ERROR,
            "presenta": _PRESENTA_IMPORT_ERROR,
            "studies": _STUDIES_IMPORT_ERROR,
            "app_memory": _APP_MEMORY_IMPORT_ERROR,
            "workspace_snapshots": _WORKSPACE_SNAPSHOTS_IMPORT_ERROR,
            "working_sets": _WORKING_SETS_IMPORT_ERROR,
            "artifacts": _ARTIFACTS_IMPORT_ERROR,
            "drive": _DRIVE_IMPORT_ERROR,
            "search_intent": _SEARCH_INTENT_IMPORT_ERROR,
            "job_truth": _JOB_TRUTH_IMPORT_ERROR,
        }.items()
        if err
    }
    if routers_failed:
        ready = False
        checks["routers_failed"] = routers_failed
    else:
        checks["routers_failed"] = {}

    database = await _database_readiness_checks()
    checks["database"] = database
    if database.get("status") != "ok":
        ready = False

    # P1-SEC fix (2026-04-20): unauth /ready must not leak DB topology
    # (Neon/Timescale hostnames, users, schema columns). Keep only
    # status summaries; full shape is available via authenticated tooling.
    def _redact_db_check(block: object) -> object:
        if not isinstance(block, dict):
            return block
        allowed_top = {"status", "neon", "neon_sessions_schema", "timescale"}
        out: Dict[str, object] = {}
        for k, v in block.items():
            if k not in allowed_top:
                continue
            if k == "status":
                out[k] = v
            elif isinstance(v, dict):
                # Per-engine sub-block: emit only status + bool flags
                sub = {"status": v.get("status", "unknown")}
                for bk in ("table_exists", "session_id_unique", "database_url_configured"):
                    if bk in v:
                        sub[bk] = bool(v.get(bk))
                out[k] = sub
            else:
                out[k] = v
        return out

    checks["storage"] = {
        "configured": bool(checks.get("storage", {}).get("configured", False)),
        "ready": bool(checks.get("storage", {}).get("ready", False)),
    }
    if isinstance(checks.get("redis"), dict):
        checks["redis"] = {"status": checks["redis"].get("status", "unknown")}
    checks["database"] = _redact_db_check(database)

    payload = {
        "ready": ready,
        "timestamp": int(time.time()),
        "checks": checks,
    }
    if ready:
        return payload

    raise HTTPException(status_code=503, detail=payload)


# ── P3-3: Basic operational metrics endpoint ────────────────────
# In-memory counters (no external dependency required).
_metrics_counters: Dict[str, int] = defaultdict(int)
_metrics_start_time = time.monotonic()
_user_dependency = _auth_user_dependency


@app.middleware("http")
async def metrics_counter_middleware(request: Request, call_next):
    """Track total requests and per-status counts for /api/v1/metrics."""
    _metrics_counters["http_requests_total"] += 1
    response = await call_next(request)
    _metrics_counters[f"http_responses_{response.status_code}"] += 1
    return response


@app.get("/api/v1/metrics")
def metrics(user_id: str = Depends(_user_dependency)):
    """Operational metrics — request counters, uptime, rate limit state. Requires auth."""
    try:
        from mica.observability.slice_sweep_metrics import snapshot as _sweep_snap
        sweeps = _sweep_snap()
    except Exception:  # noqa: BLE001
        sweeps = {}
    return {
        "uptime_s": round(time.monotonic() - _metrics_start_time, 1),
        "counters": dict(_metrics_counters),
        "slice_sweeps": sweeps,
        "rate_limiter": {
            "tracked_ips": len(_rate_limit_state),
            "window_s": _RATE_LIMIT_WINDOW,
            "max_requests": _RATE_LIMIT_MAX,
        },
    }


@app.get("/api/v1/metrics/sweeps")
def metrics_sweeps():
    """Public read-only slice-sweep probe counters (Slice-6 §2).

    No auth required — contains only aggregate probe verdicts
    (slice_id, probe_id, verdict → count), no user data or secrets.
    Rate limited by the global middleware.
    """
    from datetime import datetime, timezone
    try:
        from mica.observability.slice_sweep_metrics import snapshot as _sweep_snap
        sweeps = _sweep_snap()
    except Exception as exc:  # noqa: BLE001
        return {"slice_sweeps": {}, "error": str(exc),
                "collected_at": datetime.now(timezone.utc).isoformat()}
    return {
        "slice_sweeps": sweeps,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/testimony/{post_id}")
def testimony(post_id: str, max_hops: int = 32):
    """Public read-only policy testimony chain (Slice-6 §4).

    Walks the parent_id chain from `post_id` through the agent feed,
    returning the ordered hops and a classification summary. No auth;
    feed contents are already public via scroll_agent_feed.
    """
    try:
        from mica.agentic.policy_testimony import testimony_as_dict
        return testimony_as_dict(post_id, max_hops=max_hops)
    except Exception as exc:  # noqa: BLE001
        return {"root_post_id": post_id, "error": str(exc), "hops": []}


@app.get("/api/v1/observability/grafana/config")
def grafana_config():
    """Slice-6 §7: redacted snapshot of configured Grafana Cloud endpoints.
    Token suffixes only (never the full credential)."""
    from mica.observability.grafana_push import grafana_config_snapshot
    return grafana_config_snapshot()


@app.post("/api/v1/observability/grafana/test_push")
def grafana_test_push(request: Request):
    """Slice-6 §7: emit one test log line to Grafana Cloud Loki and
    return the push result. Guarded by a shared probe key
    (``SLICE_PROBE_KEY`` env var); production fails closed if unset."""
    import os as _os
    from dataclasses import asdict
    from datetime import datetime, timezone
    from mica.observability.grafana_push import (
        push_loki_structured, grafana_config_snapshot,
    )
    expected = _os.getenv("SLICE_PROBE_KEY", "")
    is_production = (_os.getenv("MICA_ENV") or _os.getenv("ENVIRONMENT") or "").lower() in {"prod", "production"}
    if is_production and not expected:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Grafana probe key is required in production")
    if expected:
        provided = request.headers.get("X-Slice-Probe-Key", "")
        if provided != expected:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="invalid probe key")
    result = push_loki_structured(
        event={
            "event": "slice6_section7_test_push",
            "slice": "slice6",
            "section": "7",
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "source": "mica-api",
            "purpose": "verify Grafana Cloud Loki connectivity from Railway",
        },
        level="info",
        labels={"slice": "slice6", "probe": "section7_push_test"},
    )
    return {
        "push": asdict(result),
        "config": grafana_config_snapshot(),
    }


@app.get("/api/v1/audit/candidate/{post_id}")
async def audit_candidate(post_id: str):
    """Slice-7 §23 — reconstruct one candidate's full audit trail from
    feed testimony + (future) OTel trace + experiment_store ledger."""
    from mica.agentic.policy_testimony import testimony_as_dict
    try:
        testimony = await testimony_as_dict(post_id, max_hops=32)
    except Exception as exc:
        testimony = {"error": str(exc)[:200]}
    # Experiment-store lookup (best effort).
    exp_record: Dict[str, Any] = {}
    try:
        from mica.experiments.experiment_store import ExperimentStore  # type: ignore
        store = ExperimentStore()
        row = await store.get_by_post_id(post_id) if hasattr(store, "get_by_post_id") else None
        if row:
            exp_record = dict(row)
    except Exception as exc:
        exp_record = {"skipped": str(exc)[:120]}
    return {
        "post_id": post_id,
        "testimony": testimony,
        "experiment_record": exp_record,
        "trace_hint": "search Tempo with mica.session_id from testimony metadata",
    }


@app.get("/api/v1/observability/catalog")
def observability_catalog():
    """Slice-7 §15 — expose the metrics catalog for UI/dashboard discovery."""
    from mica.observability.metrics_catalog import catalog_snapshot, is_live
    return {"metrics": catalog_snapshot(), "otel_live": is_live()}


@app.get("/api/v1/observability/feed_signing/status")
def feed_signing_status():
    """Slice-7 §17 — crypto availability + session count."""
    from mica.agentic import feed_signing as _fs
    return {"crypto_available": _fs.is_crypto_available(),
            "sessions_with_keys": len(_fs._KEYSTORE)}




@app.websocket("/ws/mica")
async def mica_agentic_websocket_endpoint(websocket: WebSocket):
    await handle_mica_agentic_websocket(websocket)

@app.websocket("/ws/md/{job_id}")
async def md_websocket_endpoint(websocket: WebSocket, job_id: str):
    from mica.ws_md import handle_md_websocket
    await handle_md_websocket(websocket, job_id)

@app.websocket("/ws/preview/{run_id}")
async def preview_websocket_endpoint(websocket: WebSocket, run_id: str):
    from mica.md_preview.preview_ws_replayer import handle_preview_websocket
    await handle_preview_websocket(websocket, run_id)

@app.post("/api/v1/storage/upload-url")
def create_upload_url(payload: SignedUrlRequest, user_id: str = Depends(_user_dependency)):
    ttl = _clamp_ttl(payload.expires_in)
    object_path = _normalize_object_path(payload.prefix, payload.object_name)
    content_type = _sanitize_content_type(payload.content_type)
    try:
        storage_manager = _get_storage_manager()
        url = storage_manager.signed_url(
            user_id=user_id,
            object_path=object_path,
            method="PUT",
            expires_seconds=ttl,
            content_type=content_type,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to generate signed upload URL")
    metrics.inc_upload(user_id, payload.bytes_planned)
    return {
        "ok": True,
        "url": url,
        "bucket": storage_manager.ensure_bucket(user_id).bucket_name,
        "object_path": object_path,
        "expires_in": ttl,
    }


@app.post("/api/v1/storage/download-url")
def create_download_url(payload: SignedUrlRequest, user_id: str = Depends(_user_dependency)):
    ttl = _clamp_ttl(payload.expires_in)
    object_path = _normalize_object_path(payload.prefix, payload.object_name)
    try:
        storage_manager = _get_storage_manager()
        url = storage_manager.signed_url(
            user_id=user_id,
            object_path=object_path,
            method="GET",
            expires_seconds=ttl,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to generate signed download URL")
    metrics.inc_download(user_id)
    return {
        "ok": True,
        "url": url,
        "bucket": storage_manager.ensure_bucket(user_id).bucket_name,
        "object_path": object_path,
        "expires_in": ttl,
    }


@app.get("/api/v1/metrics/user")
def user_metrics(user_id: str = Depends(_user_dependency)):
    return {"user_id": user_id, "metrics": metrics.snapshot(user_id)}


@app.get("/api/v1/metrics/attribution")
async def usage_attribution(
    session_id: Optional[str] = None,
    user_id: str = Depends(_user_dependency),
):
    """Return per-instance runtime/cost attribution for the authenticated user."""
    try:
        from mica.infrastructure.persistence import TimescaleEventStore

        store = TimescaleEventStore()
        await store.initialize()
        summary = await store.summarize_instance_usage(user_id=user_id, session_id=session_id)
        await store.close()
        return {"ok": True, "attribution": summary}
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to compute attribution")


@app.post("/api/v1/webhooks/clerk")
async def clerk_webhook(request: Request):
    body_bytes = await request.body()
    if not CLERK_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Clerk webhook secret is required")

    svix_id = request.headers.get("svix-id")
    svix_ts = request.headers.get("svix-timestamp")
    svix_sig = request.headers.get("svix-signature")
    if not (svix_id and svix_ts and svix_sig):
        raise HTTPException(status_code=401, detail="Missing webhook signature headers")

    try:
        from svix.webhooks import Webhook  # type: ignore

        wh = Webhook(CLERK_WEBHOOK_SECRET)
        wh.verify(
            body_bytes.decode("utf-8"),
            {"svix-id": svix_id, "svix-timestamp": svix_ts, "svix-signature": svix_sig},
        )
    except ModuleNotFoundError:
        raise HTTPException(status_code=500, detail="Webhook verifier not installed")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload_dict = await request.json()
        payload = ClerkWebhookPayload(**payload_dict)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    if payload.type != "user.created":
        return {"ok": True, "ignored": True}

    clerk_user_id = payload.data.get("id") or payload.data.get("user_id")
    if not clerk_user_id:
        raise HTTPException(status_code=400, detail="Missing user id in webhook payload")
    storage_manager = _get_storage_manager()
    bucket = storage_manager.ensure_bucket(clerk_user_id)
    return {"ok": True, "bucket": bucket.bucket_name, "user_id": clerk_user_id}


@app.post("/api/v1/agentic/prompt")
async def agentic_prompt(
    payload: AgenticPromptRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(_user_dependency),
):
    """
    Non-blocking agentic prompt endpoint.

    Returns immediately with a ``job_id``. The caller can poll
    ``GET /api/v1/agentic/jobs/{job_id}`` for the result.

    For backward compat the legacy synchronous response shape is preserved
    when the job finishes (accessible via the polling endpoint).
    """
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    return _queue_agentic_prompt_job(
        prompt=prompt,
        mode=payload.mode or "production",
        session_id=payload.session_id,
        mcp_enabled=payload.mcp_enabled,
        resource_fabric_enabled=payload.resource_fabric_enabled,
        user_id=user_id,
        background_tasks=background_tasks,
    )



@app.post("/api/v1/protocol-drafts/execute")
async def execute_protocol_draft(
    payload: ProtocolDraftExecuteRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(_user_dependency),
):
    study_id = payload.study_id or None
    protocol_run_id = payload.protocol_run_id or None
    request_metadata = {
        "request_type": "protocol_jsonld",
        "protocol_id": (payload.id or "").strip(),
        "request_user_id": user_id,
        "mode": payload.mode or "production",
        "mcp_enabled": bool(payload.mcp_enabled),
        "resource_fabric_enabled": bool(payload.resource_fabric_enabled),
    }
    if study_id:
        request_metadata["study_id"] = study_id
    try:
        executor_request, draft, document, frontier = protocol_jsonld_to_executor_request(
            payload.protocolJsonld,
            fallback_name=payload.name,
            fallback_description=payload.description,
            fallback_goal=payload.goal,
            node_receipts=payload.nodeReceipts,
            session_id=payload.session_id or "",
            request_metadata=request_metadata,
        )
        if payload.id.strip() and payload.id.strip() != draft.id:
            raise ValueError("payload.id must match protocolJsonld protocol_id")
        request_metadata = {
            "request_type": "protocol_jsonld",
            "protocol_id": draft.id,
            "protocol_name": draft.name,
            "protocol_version": document.version,
            "step_count": len(draft.steps),
            "protocol_node_count": len(document.nodes),
            "completed_node_ids": list(frontier.completed_node_ids),
            "ready_node_ids": list(frontier.ready_node_ids),
            "blocked_node_ids": list(frontier.blocked_node_ids),
            "provided_receipt_count": frontier.receipt_count,
            "request_user_id": user_id,
            "mode": payload.mode or "production",
            "mcp_enabled": bool(payload.mcp_enabled),
            "resource_fabric_enabled": bool(payload.resource_fabric_enabled),
        }
        if study_id:
            request_metadata["study_id"] = study_id
        executor_request = executor_request.model_copy(update={"request_metadata": request_metadata})
        fallback_prompt = compile_protocol_draft_to_prompt(draft)
    except (ValueError, ValidationError, ProtocolJSONLDSemanticError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _queue_protocol_executor_job(
        executor_request=executor_request,
        fallback_prompt=fallback_prompt,
        mode=payload.mode or "production",
        session_id=payload.session_id,
        mcp_enabled=payload.mcp_enabled,
        resource_fabric_enabled=payload.resource_fabric_enabled,
        user_id=user_id,
        background_tasks=background_tasks,
        request_metadata=request_metadata,
        study_id=study_id,
        protocol_run_id=protocol_run_id,
    )


@app.get("/api/v1/agentic/jobs/{job_id}")
async def get_agentic_job(job_id: str, user_id: str = Depends(_user_dependency)):
    """Poll the status / result of a previously submitted agentic prompt job."""
    job = _agentic_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return job.to_dict()


@app.get("/api/v1/agentic/jobs")
async def list_agentic_jobs(user_id: str = Depends(_user_dependency)):
    """List all agentic jobs for the authenticated user (in-memory, current process only)."""
    _evict_stale_jobs()
    return [j.to_dict() for j in _agentic_jobs.values() if j.user_id == user_id]


@app.get("/api/v1/hn/runs/{run_ref:path}/retries")
async def hn_run_retry_status(
    run_ref: str,
    workspace_id: str = "",
    study_id: str = "",
    activation_store_path: str = "",
    retry_transition_store_path: str = "",
    claim_store_path: str = "",
    outbox_store_path: str = "",
    user_id: str = Depends(_user_dependency),
):
    from mica.agentic.command_kernel import UnifiedAgentCommandKernel
    from mica.sdk.command_contracts import BackendCommandEnvelope, BackendCommandPolicy

    kernel = UnifiedAgentCommandKernel(user_id=user_id)
    result = await kernel.execute(
        BackendCommandEnvelope(
            command_name="hn.run.retry.status",
            workspace_id=workspace_id,
            study_id=study_id,
            request_identity={"surface": "api_v1", "user_id": user_id},
            arguments={
                "run_ref": run_ref,
                "workspace_id": workspace_id,
                "study_id": study_id,
                "activation_store_path": activation_store_path or None,
                "retry_transition_store_path": retry_transition_store_path or None,
                "claim_store_path": claim_store_path or None,
                "outbox_store_path": outbox_store_path or None,
            },
            policy=BackendCommandPolicy(allow_side_effects=False),
        )
    )
    return result.model_dump(mode="json")


@app.get("/api/v1/hn/runs/{run_ref:path}/binding")
async def hn_run_binding(
    run_ref: str,
    workspace_id: str = "",
    study_id: str = "",
    activation_store_path: str = "",
    retry_transition_store_path: str = "",
    claim_store_path: str = "",
    outbox_store_path: str = "",
    user_id: str = Depends(_user_dependency),
):
    from mica.agentic.command_kernel import UnifiedAgentCommandKernel
    from mica.sdk.command_contracts import BackendCommandEnvelope, BackendCommandPolicy

    kernel = UnifiedAgentCommandKernel(user_id=user_id)
    result = await kernel.execute(
        BackendCommandEnvelope(
            command_name="hn.run.binding",
            workspace_id=workspace_id,
            study_id=study_id,
            request_identity={"surface": "api_v1", "user_id": user_id},
            arguments={
                "run_ref": run_ref,
                "workspace_id": workspace_id,
                "study_id": study_id,
                "activation_store_path": activation_store_path or None,
                "retry_transition_store_path": retry_transition_store_path or None,
                "claim_store_path": claim_store_path or None,
                "outbox_store_path": outbox_store_path or None,
            },
            policy=BackendCommandPolicy(allow_side_effects=False),
        )
    )
    return result.model_dump(mode="json")


@app.get("/api/v1/hn/runs/deadletter")
async def hn_run_deadletter(
    workspace_id: str = "",
    study_id: str = "",
    activation_store_path: str = "",
    retry_transition_store_path: str = "",
    claim_store_path: str = "",
    user_id: str = Depends(_user_dependency),
):
    from mica.agentic.command_kernel import UnifiedAgentCommandKernel
    from mica.sdk.command_contracts import BackendCommandEnvelope, BackendCommandPolicy

    kernel = UnifiedAgentCommandKernel(user_id=user_id)
    result = await kernel.execute(
        BackendCommandEnvelope(
            command_name="hn.run.deadletter",
            workspace_id=workspace_id,
            study_id=study_id,
            request_identity={"surface": "api_v1", "user_id": user_id},
            arguments={
                "workspace_id": workspace_id,
                "study_id": study_id,
                "activation_store_path": activation_store_path or None,
                "retry_transition_store_path": retry_transition_store_path or None,
                "claim_store_path": claim_store_path or None,
            },
            policy=BackendCommandPolicy(allow_side_effects=False),
        )
    )
    return result.model_dump(mode="json")



# Storage Outbox API (Lane S-R1 — single-writer outbox authority)
_STORAGE_OUTBOX_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.storage_outbox import router as storage_outbox_router

    app.include_router(storage_outbox_router, tags=["storage-outbox"])
except Exception as _e:  # pragma: no cover
    _STORAGE_OUTBOX_IMPORT_ERROR = str(_e)

# Storage Promotion API (Lane S-R2 — global-curated projection with gate)
_STORAGE_PROMOTION_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.storage_promotion import router as storage_promotion_router

    app.include_router(storage_promotion_router, tags=["storage-promotion"])
except Exception as _e:  # pragma: no cover
    _STORAGE_PROMOTION_IMPORT_ERROR = str(_e)

# Storage Reconcile API (Lane S-R3 — state machine + reconciler, no 2PC)
_STORAGE_RECONCILE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover
    from mica.api_v1.routers.storage_reconcile import router as storage_reconcile_router

    app.include_router(storage_reconcile_router, tags=["storage-reconcile"])
except Exception as _e:  # pragma: no cover
    _STORAGE_RECONCILE_IMPORT_ERROR = str(_e)

# -------------
# Dev entrypoint
# -------------

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("mica.api_v1.main:app", host="0.0.0.0", port=8080, reload=False)
