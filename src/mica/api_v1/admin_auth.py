"""Admin authorization dependency for MICA API v1.

Usage::

    from mica.api_v1.admin_auth import admin_dependency

    @router.post("/some-admin-endpoint")
    async def do_thing(admin_id: str = Depends(admin_dependency)):
        ...

Configuration
-------------
Set the ``MICA_ADMIN_USER_IDS`` environment variable to a comma-separated
list of user IDs that are allowed to call admin-gated endpoints::

    MICA_ADMIN_USER_IDS=user_abc123,user_def456

When the variable is unset or empty, all admin-gated endpoints respond 403
(fail-closed — no user can elevate to admin without explicit configuration).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import FrozenSet

from fastapi import Depends, HTTPException

from mica.api_v1.auth import user_dependency


@lru_cache(maxsize=1)
def _admin_ids() -> FrozenSet[str]:
    """Parse and cache MICA_ADMIN_USER_IDS at first call."""
    raw = os.getenv("MICA_ADMIN_USER_IDS", "")
    return frozenset(uid.strip() for uid in raw.split(",") if uid.strip())


def admin_dependency(user_id: str = Depends(user_dependency)) -> str:
    """FastAPI dependency that requires the caller to be an admin.

    Returns the user_id when the check passes so callers can use it
    directly without a redundant ``Depends(user_dependency)`` alongside.

    Raises
    ------
    HTTP 403 — caller is not in MICA_ADMIN_USER_IDS
    HTTP 503 — MICA_ADMIN_USER_IDS is unset/empty (fail-closed)
    """
    ids = _admin_ids()
    if not ids:
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin access is not configured. "
                "Set MICA_ADMIN_USER_IDS to enable admin endpoints."
            ),
        )
    if user_id not in ids:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id
