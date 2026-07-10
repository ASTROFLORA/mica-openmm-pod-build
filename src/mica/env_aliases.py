"""Utility helpers to normalize provider API key aliases.

The `.env` file (see pages 90-101 of the ops handbook) defines both canonical
keys such as ``OPENAI_API_KEY`` and a set of provider-specific aliases like
``OPENAI_GPT4O_API_KEY`` or ``CLAUDE_API_KEY``.  Runtime components only read the
canonical names, so this module maps any populated alias back into the
corresponding canonical key at import time without mutating the alias itself.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterable

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency guard
    load_dotenv = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Canonical env var -> ordered list of alias names that may hold the same key.
_CANONICAL_ALIAS_TABLE: Dict[str, Iterable[str]] = {
    # OpenAI operator variants (4o, mini, etc.)
    "OPENAI_API_KEY": (
        "OPENAI_GPT4O_API_KEY",
        "OPENAI_GPT4O_MINI_API_KEY",
        "OPENAI_GPT4_API_KEY",
        "OPENAI_GPT4_TURBO_API_KEY",
    ),
    # Claude / Anthropic
    "ANTHROPIC_API_KEY": (
        "CLAUDE_API_KEY",
    ),
    # Gemini / Google GenAI
    "GOOGLE_API_KEY": (
        "GEMINI_1_5_PRO_API_KEY",
        "GEMINI_1_5_FLASH_API_KEY",
    ),
    # MICA-prefixed canonical keys hydrate from plain provider env names.
    "MICA_OPENAI_API_KEY": (
        "OPENAI_API_KEY",
        "OPENAI_GPT4O_API_KEY",
        "OPENAI_GPT4O_MINI_API_KEY",
        "OPENAI_GPT4_API_KEY",
        "OPENAI_GPT4_TURBO_API_KEY",
    ),
    "MICA_ANTHROPIC_API_KEY": (
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
    ),
    "MICA_GOOGLE_API_KEY": (
        "GOOGLE_API_KEY",
        "GEMINI_1_5_PRO_API_KEY",
        "GEMINI_1_5_FLASH_API_KEY",
    ),
    "MICA_FIREWORKS_API_KEY": (
        "FIREWORKS_API_KEY",
    ),
    "MICA_DEEPINFRA_API_KEY": (
        "DEEPINFRA_API_KEY",
        "DEEPINFRA_API_KEY_DEV",
    ),
}


def _default_runtime_env_paths() -> tuple[Path, ...]:
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / ".env", repo_root / ".env.local")


def _should_autoload_dotenv(*, force: bool = False) -> bool:
    if force:
        return True
    if str(os.getenv("MICA_DISABLE_DOTENV_AUTOLOAD", "") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return True


def bootstrap_runtime_env(*, overwrite: bool = False, force: bool = False) -> Dict[str, object]:
    """Load repo dotenv files and normalize aliases for runtime consumers."""

    loaded_files: list[str] = []
    if load_dotenv is not None and _should_autoload_dotenv(force=force):
        for env_path in _default_runtime_env_paths():
            if not env_path.is_file():
                continue
            load_dotenv(dotenv_path=env_path, override=overwrite)
            loaded_files.append(str(env_path))

    aliases = apply_env_aliases(overwrite=overwrite)
    return {
        "loaded_files": loaded_files,
        "aliases": aliases,
    }


def apply_env_aliases(*, overwrite: bool = False) -> Dict[str, str]:
    """Ensure canonical env vars are populated when any alias is set.

    Args:
        overwrite: When ``True`` the canonical value is always replaced with the
            first populated alias.  Default keeps an existing canonical value.

    Returns:
        Mapping of canonical variable -> alias that provided its value (or the
        literal name if it was already populated).
    """

    applied: Dict[str, str] = {}

    for canonical, aliases in _CANONICAL_ALIAS_TABLE.items():
        existing = os.environ.get(canonical)
        if existing and not overwrite:
            applied[canonical] = canonical
            continue

        for alias in aliases:
            alias_value = os.environ.get(alias)
            if not alias_value:
                continue

            if existing and overwrite:
                logger.debug("Overwriting %s with alias %s", canonical, alias)
            elif not existing:
                logger.debug("Mapping alias %s to %s", alias, canonical)

            os.environ[canonical] = alias_value
            applied[canonical] = alias
            break
        else:
            if existing:
                applied[canonical] = canonical
            else:
                logger.debug("No alias populated for %s", canonical)

    return applied


__all__ = ["apply_env_aliases", "bootstrap_runtime_env"]
