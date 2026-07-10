"""Internal token gate — constant-time HMAC validation.

Protects agent-callable Railway endpoints from external callers without
introducing JWT / JWKS / OAuth attack surface.

Token rotation = change ``MICA_INTERNAL_TOKEN`` env var in Railway → redeploy.
Dev mode = leave env var empty → bypass (logs warning).
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_TOKEN_ENV = "MICA_INTERNAL_TOKEN"


def _is_production_env() -> bool:
    env = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "development"
    return str(env).lower() in ("prod", "production")


def _configured_token() -> str:
    return os.getenv(_TOKEN_ENV, "").strip()


def verify_internal_token(x_internal_token: str = Header(default="")) -> None:
    """FastAPI dependency — validates ``X-Internal-Token`` header.

    Uses :func:`hmac.compare_digest` so verification runs in constant time
    regardless of how many prefix characters match. This denies timing-based
    token-guessing attacks from external callers measuring response latency.

    If the env var ``MICA_INTERNAL_TOKEN`` is empty or unset, authentication
    is bypassed (dev/local mode). In production on Railway, the var MUST be
    set or agent-callable endpoints become unreachable by design.
    """
    expected = _configured_token()
    if not expected:
        if _is_production_env():
            logger.error("MICA_INTERNAL_TOKEN is required in production")
            raise HTTPException(status_code=503, detail="Internal auth misconfigured")
        logger.warning(
            "MICA_INTERNAL_TOKEN not set — internal endpoint is unauthenticated. "
            "Set the env var in Railway to enable the gate."
        )
        return

    provided = (x_internal_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing internal token")
