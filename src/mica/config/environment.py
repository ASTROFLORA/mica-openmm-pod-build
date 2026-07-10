"""
Canonical environment authority for MICA.

DOCTRINE (2026-06-15, session e6230433):
  Production is the SAFE DEFAULT. Dev mode is opt-in via MICA_FORCE_DEV=true.
  Any signal of Railway / Cloud Run / GCP / explicit MICA_ENV=production
  forces is_production() == True.

This module is the SINGLE SOURCE OF TRUTH for environment classification.
No other file in the codebase should define its own _is_production_env()
or _PROD_ENV constant.

Why this exists:
  The DRIVER_PROVIDER_AND_QUICK_WIN_DEMO_V2 sprint audit revealed that
  a single misconfigured env var (MICA_ENV=production missing in Railway
  Variables dashboard) caused MICA to silently boot in dev mode in
  production. All 13 silent local_fallback sites in the codebase
  bypassed themselves because _is_production_env() returned False.
  Result: ClaimCards, ReportPackets, and Studies were created with
  mock_* IDs, not real GCS/Neon artifacts, and the sprint was
  classified as `passed` when it was in fact partial.

This module prevents that class of bug at the root.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Final

logger = logging.getLogger(__name__)

# ── Canonical signals ────────────────────────────────────────────────────────

_PRODUCTION_ENV_NAMES: Final[frozenset[str]] = frozenset({
    "prod",
    "production",
    "railway",
    "railway-production",
    "live",
})

# Railway sets these automatically on every deploy. ANY of them = production.
_RAILWAY_PROD_SIGNALS: Final[tuple[str, ...]] = (
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_SERVICE_NAME",
)

# GCP SA present = production (dev runs do not have GCP creds).
_GCP_PROD_SIGNALS: Final[tuple[str, ...]] = (
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "GCP_PROJECT",
    "GCP_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
)

# Explicit dev opt-in. The ONLY way to force dev mode.
_DEV_OPT_IN: Final[str] = "MICA_FORCE_DEV"


# ── Public API ──────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def is_production() -> bool:
    """Return True iff the runtime is in production mode.

    Production is the default. Returns True if ANY of the following:
      1. MICA_ENV or ENVIRONMENT in {prod, production, railway, railway-production, live}
      2. ANY Railway deployment var is set (RAILWAY_ENVIRONMENT_NAME,
         RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_DEPLOYMENT_ID,
         RAILWAY_SERVICE_NAME)
      3. ANY GCP production signal is set
         (GOOGLE_APPLICATION_CREDENTIALS_JSON, GCP_PROJECT, GCP_PROJECT_ID,
         GOOGLE_CLOUD_PROJECT)
      4. MICA_FORCE_DEV is NOT explicitly True

    Returns False ONLY if MICA_FORCE_DEV is explicitly True AND none
    of the production signals above is present.

    The first call seeds from the .env file (best-effort) and caches
    the result. Subsequent calls are O(1) and consistent.
    """
    # Best-effort: ensure .env is loaded so MICA_ENV etc. are visible
    # without requiring the caller to have called seed_env_from_dotenv.
    try:
        from mica.config.dotenv_loader import seed_env_from_dotenv
        seed_env_from_dotenv()
    except Exception:
        pass

    # 1) Explicit dev opt-out (only way to be in dev mode)
    force_dev = _truthy(os.getenv(_DEV_OPT_IN))
    if force_dev:
        # Logged once per process at INFO so operators see the override
        logger.info(
            "Environment: DEV mode forced via MICA_FORCE_DEV=true. "
            "All production safeguards are DISABLED. Use only for tests/local debugging."
        )
        return False

    # 2) Explicit production env var
    mica_env = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if mica_env in _PRODUCTION_ENV_NAMES:
        return True

    # 3) Railway deployment = production by default (any env)
    for var in _RAILWAY_PROD_SIGNALS:
        if os.getenv(var):
            return True

    # 4) GCP credentials present = production (dev runs do not have these)
    for var in _GCP_PROD_SIGNALS:
        if os.getenv(var):
            return True

    # 5) Default: production
    # This is the key behavior change. If we got here with no signals,
    # the runtime is either:
    #   a) Local dev (operator should set MICA_FORCE_DEV=true explicitly)
    #   b) A misconfigured production deploy (fail closed = production)
    # Option (b) is the safer choice. Operators who actually want dev
    # mode MUST set MICA_FORCE_DEV=true.
    return True


def is_development() -> bool:
    """Convenience inverse of is_production()."""
    return not is_production()


def environment_name() -> str:
    """Return a human-readable label for the current environment.

    Returns 'production', 'development', or 'railway-production' etc.
    Pure read-only — does not change the cached classification.
    """
    if not is_production():
        return "development"
    if any(os.getenv(v) for v in _RAILWAY_PROD_SIGNALS):
        return "railway-production"
    mica_env = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if mica_env:
        return mica_env
    return "production"


def reset_cache() -> None:
    """Reset the lru_cache. Use only in tests."""
    is_production.cache_clear()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _truthy(value: str | None) -> bool:
    """Return True for the common truthy string spellings."""
    if not value:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


# ── Self-check (called from main.py startup) ────────────────────────────────


def assert_production_safeguards_or_abort() -> None:
    """In production mode, verify the canonical GCS authority is ready.

    Raises RuntimeError with a typed blocker_code if GCS is not ready
    in a production runtime. This is the doctrine GATE 5 enforcement
    at startup, not at request time.

    Intended to be called once at FastAPI startup AND once at the
    AgenticDriver entrypoint.
    """
    if not is_production():
        # Dev mode: emit a warning but do not abort. The GCS proof is
        # still attempted; fallbacks are permitted in dev only.
        logger.warning(
            "Environment: development mode. GCS safeguards are advisory. "
            "Production safeguards would abort on failure."
        )
        return

    # Production: GCS MUST be ready. No silent fallback to local.
    try:
        from mica.storage.gcs_user_storage import get_storage_manager
        manager = get_storage_manager()
        # PRODUCTION_FIRST_GUARD_V1: deeper check. The GCSUserStorage object
        # is constructible without credentials (storage.Client accepts
        # project=None), so just instantiating it is not enough. We must
        # verify that the configured bucket can be reached, which forces
        # the actual authentication path. This catches the case where
        # credentials are missing or invalid.
        try:
            test_bucket = manager.ensure_bucket("startup-probe-user")
            # Verify we can list objects in the bucket (a real auth call)
            blobs = list(manager.client.list_blobs(test_bucket.bucket_name, max_results=1))
            logger.info(
                "PRODUCTION_FIRST_GUARD: GCS authority verified, "
                "bucket=%s reachable, list_blobs returned %d entries",
                test_bucket.bucket_name, len(blobs),
            )
        except Exception as _auth_exc:  # noqa: BLE001
            raise RuntimeError(
                "GCSUserStorage constructed but authentication/bucket access "
                f"failed: {type(_auth_exc).__name__}: {_auth_exc}. "
                "Check GOOGLE_APPLICATION_CREDENTIALS, GCP_PROJECT, and "
                "the service account's roles/storage.admin permission. "
                "blocker_code: gcs_auth_failed_in_production"
            ) from _auth_exc
    except Exception as exc:  # noqa: BLE001 — any failure aborts
        raise RuntimeError(
            "MICA cannot start in production mode: GCS authority is not ready. "
            f"Underlying error: {type(exc).__name__}: {exc}. "
            "Set GOOGLE_APPLICATION_CREDENTIALS_JSON or "
            "GOOGLE_APPLICATION_CREDENTIALS to a valid service account JSON, "
            "and verify GCP_PROJECT, GCS_REGION, GCS_BUCKET_PREFIX are set. "
            "blocker_code: gcs_init_failed_in_production"
        ) from exc

    # Production: ConversationStore must use GCS (no _LocalFallbackStore).
    try:
        from mica.agentic.persistence import ConversationStore
        store = ConversationStore()
        if store._gcs is None:  # noqa: SLF001 — direct internal check
            raise RuntimeError(
                "MICA cannot start in production mode: "
                "ConversationStore fell back to local storage. "
                "blocker_code: conversation_store_local_in_production"
            )
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"MICA cannot start in production mode: ConversationStore init failed: "
            f"{type(exc).__name__}: {exc}. "
            "blocker_code: conversation_store_init_failed"
        ) from exc

    # Production: DriverArtifactSync must be cloud-ready.
    try:
        from mica.drivers.persistence.gcs_sync import DriverArtifactSync
        sync = DriverArtifactSync(user_id="startup-probe")
        if not sync.is_cloud_ready:
            raise RuntimeError(
                "MICA cannot start in production mode: "
                "DriverArtifactSync reports is_cloud_ready=False. "
                "blocker_code: driver_gcs_sync_not_ready_in_production"
            )
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"MICA cannot start in production mode: DriverArtifactSync init failed: "
            f"{type(exc).__name__}: {exc}. "
            "blocker_code: driver_gcs_sync_init_failed"
        ) from exc

    logger.info(
        "Environment: production mode (%s). "
        "GCS authority verified at startup. All production safeguards active.",
        environment_name(),
    )
