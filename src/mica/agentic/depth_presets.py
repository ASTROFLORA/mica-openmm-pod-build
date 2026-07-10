"""
DepthPresets — Configurable processing depth for MICA scientific workflows.
==========================================================================

Three presets control how deeply the system investigates a query:

- **Fast**: Minimal iteration, no cue enforcement, no peer review.
  Optimised for quick overviews and triage.
- **Standard**: Default depth with quality gating, audit-mode cues,
  partial MSRP phases, and single peer review pass.
- **Deep**: Full scientific rigour with blocking cues, complete MSRP
  5-phase reasoning, and iterative peer review.

Each preset is a frozen dataclass that the driver reads once at the start
of a session.  The driver then injects the values into MICAState fields
(``max_iterations``, ``quality_threshold``) and passes cue/MSRP flags to
the enforcement layer.

Usage::

    preset = resolve_depth_preset("standard")
    state["max_iterations"] = preset.max_iterations
    state["quality_threshold"] = preset.quality_threshold

Author: MICA Capability Authority Lab (L-10) — AGENT-A / NewDawn
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class DepthPreset:
    """Immutable processing depth configuration."""

    name: str
    max_iterations: int
    quality_threshold: float
    cue_mode: str           # "off" | "audit" | "blocking"
    msrp_phases: Tuple[int, ...]  # which MSRP phases to activate (1-indexed)
    peer_review_mode: str   # "skip" | "once" | "iterative"
    peer_review_max: int    # max iterative peer review rounds
    cost_multiplier: float  # approximate relative cost vs. Fast


# ── Canonical presets ─────────────────────────────────────────────────

FAST = DepthPreset(
    name="fast",
    max_iterations=5,
    quality_threshold=0.60,
    cue_mode="off",
    msrp_phases=(),
    peer_review_mode="skip",
    peer_review_max=0,
    cost_multiplier=1.0,
)

STANDARD = DepthPreset(
    name="standard",
    max_iterations=25,
    quality_threshold=0.85,
    cue_mode="audit",
    msrp_phases=(1, 3, 5),
    peer_review_mode="once",
    peer_review_max=1,
    cost_multiplier=3.0,
)

DEEP = DepthPreset(
    name="deep",
    max_iterations=50,
    quality_threshold=0.95,
    cue_mode="blocking",
    msrp_phases=(1, 2, 3, 4, 5),
    peer_review_mode="iterative",
    peer_review_max=3,
    cost_multiplier=8.0,
)

_REGISTRY = {
    "fast": FAST,
    "standard": STANDARD,
    "deep": DEEP,
}


def resolve_depth_preset(name: Optional[str] = None) -> DepthPreset:
    """Resolve a depth preset by name.

    Returns ``STANDARD`` when *name* is ``None`` or unrecognised.
    """
    if name is None:
        return STANDARD
    return _REGISTRY.get(name.strip().lower(), STANDARD)
