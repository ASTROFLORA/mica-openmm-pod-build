"""Versioned edge confidence model contracts for GraphRAG.

Doctrine anchor:
- confidence is a versioned admissibility model, not truth
- factors and decay policy are explicit per edge_kind
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class EdgeConfidenceProfile:
    model_ref: str
    edge_kind: str
    threshold: float
    factors: dict[str, float]
    decay_policy_ref: str

    @property
    def profile_ref(self) -> str:
        return f"{self.model_ref}/{self.edge_kind}"


@dataclass(frozen=True)
class EdgeConfidenceAssessment:
    profile: EdgeConfidenceProfile
    confidence_state: str
    freshness_factor: float


_EDGE_CONFIDENCE_PROFILES: dict[str, EdgeConfidenceProfile] = {
    "supports": EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind="supports",
        threshold=0.75,
        factors={
            "source_reliability": 0.20,
            "extraction_confidence": 0.15,
            "entity_resolution_confidence": 0.15,
            "evidence_strength": 0.25,
            "context_match": 0.15,
            "freshness": 0.05,
            "curation_level": 0.05,
        },
        decay_policy_ref="edge_decay://literature_v1",
    ),
    "contradicts": EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind="contradicts",
        threshold=0.75,
        factors={
            "source_reliability": 0.15,
            "contradiction_model_confidence": 0.25,
            "entity_resolution_confidence": 0.10,
            "evidence_strength": 0.20,
            "context_overlap": 0.15,
            "freshness": 0.10,
            "curation_level": 0.05,
        },
        decay_policy_ref="edge_decay://literature_v1",
    ),
    "cites": EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind="cites",
        threshold=0.95,
        factors={
            "parser_confidence": 0.35,
            "reference_match": 0.35,
            "source_version_integrity": 0.20,
            "freshness": 0.05,
            "curation_level": 0.05,
        },
        decay_policy_ref="edge_decay://citation_v1",
    ),
    "derived_from": EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind="derived_from",
        threshold=0.90,
        factors={
            "receipt_integrity": 0.45,
            "artifact_integrity": 0.25,
            "entity_resolution_confidence": 0.10,
            "freshness": 0.10,
            "curation_level": 0.10,
        },
        decay_policy_ref="edge_decay://artifact_v1",
    ),
    "same_as": EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind="same_as",
        threshold=0.90,
        factors={
            "resolver_confidence": 0.50,
            "namespace_authority": 0.20,
            "entity_resolution_confidence": 0.15,
            "freshness": 0.10,
            "curation_level": 0.05,
        },
        decay_policy_ref="edge_decay://resolver_v1",
    ),
    "mentions_entity": EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind="mentions_entity",
        threshold=0.70,
        factors={
            "ner_confidence": 0.35,
            "resolver_confidence": 0.30,
            "span_quality": 0.20,
            "freshness": 0.10,
            "curation_level": 0.05,
        },
        decay_policy_ref="edge_decay://resolver_v1",
    ),
}

_EDGE_FRESHNESS_DEFAULTS = {
    "active_high_confidence": 1.0,
    "active_low_confidence": 1.0,
    "stale_resolver": 0.50,
    "stale_literature_version": 0.60,
    "evidence_retracted": 0.00,
    "needs_revalidation": 0.70,
}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _clamp_unit_float(value: Any, *, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, numeric))


@lru_cache(maxsize=32)
def get_edge_confidence_profile(edge_kind: str) -> EdgeConfidenceProfile:
    normalized = str(edge_kind or "generic").strip().lower() or "generic"
    if normalized in _EDGE_CONFIDENCE_PROFILES:
        return _EDGE_CONFIDENCE_PROFILES[normalized]
    return EdgeConfidenceProfile(
        model_ref="edge_confidence://v1",
        edge_kind=normalized,
        threshold=0.80,
        factors={
            "source_reliability": 0.25,
            "extraction_confidence": 0.25,
            "entity_resolution_confidence": 0.20,
            "context_match": 0.15,
            "freshness": 0.10,
            "curation_level": 0.05,
        },
        decay_policy_ref="edge_decay://generic_v1",
    )


def assess_edge_confidence(*, edge_kind: str, confidence: float, metadata: dict[str, Any]) -> EdgeConfidenceAssessment:
    profile = get_edge_confidence_profile(edge_kind)
    confidence_value = _clamp_unit_float(confidence, default=0.0)

    if _coerce_bool(metadata.get("paper_retracted")) or _coerce_bool(metadata.get("evidence_retracted")):
        confidence_state = "evidence_retracted"
    elif _coerce_bool(metadata.get("resolver_snapshot_stale")) or _coerce_bool(metadata.get("resolver_drift")):
        confidence_state = "stale_resolver"
    elif _coerce_bool(metadata.get("literature_version_stale")) or _coerce_bool(metadata.get("stale_literature_version")):
        confidence_state = "stale_literature_version"
    elif (
        _coerce_bool(metadata.get("artifact_invalidated"))
        or _coerce_bool(metadata.get("method_obsolete"))
        or _coerce_bool(metadata.get("needs_revalidation"))
    ):
        confidence_state = "needs_revalidation"
    elif confidence_value >= profile.threshold:
        confidence_state = "active_high_confidence"
    else:
        confidence_state = "active_low_confidence"

    default_freshness = _EDGE_FRESHNESS_DEFAULTS[confidence_state]
    freshness_factor = _clamp_unit_float(metadata.get("freshness_factor"), default=default_freshness)
    if confidence_state not in {"active_high_confidence", "active_low_confidence"}:
        freshness_factor = min(freshness_factor, default_freshness)

    return EdgeConfidenceAssessment(
        profile=profile,
        confidence_state=confidence_state,
        freshness_factor=freshness_factor,
    )
