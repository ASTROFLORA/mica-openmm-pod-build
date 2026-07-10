"""CORS origin normalization for the MICA API v1 surface."""

from __future__ import annotations

from collections.abc import Iterable


DEFAULT_DEV_CORS_ALLOW_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

FIRST_PARTY_CORS_ALLOW_ORIGINS = [
    *DEFAULT_DEV_CORS_ALLOW_ORIGINS,
    "https://alejandria-frontend-production.up.railway.app",
    "https://alejandria-ultimate-main-production.up.railway.app",
]


def split_cors_origins(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = value

    origins: list[str] = []
    for candidate in candidates:
        origin = str(candidate).strip().rstrip("/")
        if origin:
            origins.append(origin)
    return origins


def build_cors_allow_origins(
    configured_origins: str | Iterable[str] | None,
    extra_origins: str | Iterable[str] | None = None,
    *,
    first_party_origins: Iterable[str] = FIRST_PARTY_CORS_ALLOW_ORIGINS,
) -> list[str]:
    """Merge configured origins with first-party Alejandria origins.

    Wildcard origins are intentionally ignored because the API allows
    credentials. Starlette would otherwise reflect arbitrary origins.
    """
    merged: list[str] = []
    seen: set[str] = set()

    for origin in [
        *split_cors_origins(configured_origins),
        *split_cors_origins(extra_origins),
        *split_cors_origins(first_party_origins),
    ]:
        if origin == "*" or origin in seen:
            continue
        seen.add(origin)
        merged.append(origin)

    if merged:
        return merged
    return list(DEFAULT_DEV_CORS_ALLOW_ORIGINS)