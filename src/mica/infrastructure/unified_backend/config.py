from __future__ import annotations

import os
from dataclasses import dataclass

from mica.config.dotenv_loader import normalize_env_value


def _resolve_database_url() -> str | None:
    for key in ("DATABASE_URL", "NEON_DATABASE_URL"):
        value = normalize_env_value(os.getenv(key))
        if value:
            return value

    try:
        from config.settings import settings as astro_settings  # type: ignore

        candidate = getattr(astro_settings, "DATABASE_URL", None) or getattr(
            astro_settings, "neon_database_url", None
        )
        return normalize_env_value(candidate)
    except Exception:
        return None


@dataclass(frozen=True)
class UnifiedBackendSettings:
    DATABASE_URL: str | None


settings = UnifiedBackendSettings(DATABASE_URL=_resolve_database_url())