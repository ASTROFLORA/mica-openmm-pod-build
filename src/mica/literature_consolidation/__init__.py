"""Unified literature consolidation surfaces.

This package centralizes shared literature orchestration helpers used by
API routers and workers so artifact contracts flow through one lane.
"""

from .pipeline import build_canonical_literature_bundle, best_available_literature_text

__all__ = [
    "best_available_literature_text",
    "build_canonical_literature_bundle",
]
