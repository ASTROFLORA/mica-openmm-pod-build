from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _first_non_empty(*keys: str) -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and str(value).strip() != "":
            return str(value)
    return ""


@dataclass(frozen=True)
class AstrofloraSettings:
    enable_session_persistence: bool
    DATABASE_URL: str
    neon_database_url: str


settings = AstrofloraSettings(
    enable_session_persistence=_as_bool("ENABLE_SESSION_PERSISTENCE", True),
    DATABASE_URL=_first_non_empty("DATABASE_URL", "NEON_DATABASE_URL"),
    neon_database_url=_first_non_empty("NEON_DATABASE_URL", "DATABASE_URL"),
)
