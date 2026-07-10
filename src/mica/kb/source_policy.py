"""
KB Source Policy Registry — K5-8 (KB Slice 3)

Versioned source signals: Crossref, DOAJ, OpenAlex, RetractionWatch.
Source weight affects tier scoring. Retracted source weight → 0.
Override with expiry + receipt.

Key objects:
- SourcePolicy: per-source policy with weights and status
- SourcePolicyRegistry: manages source policies and triggers tier recompute
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class PeerReviewStatus(str, Enum):
    PEER_REVIEWED = "peer_reviewed"
    NOT_PEER_REVIEWED = "not_peer_reviewed"
    UNKNOWN = "unknown"
    RETRACTED = "retracted"


class OAStatus(str, Enum):
    OPEN_ACCESS = "open_access"
    CLOSED = "closed"
    EMBARGOED = "embargoed"
    UNKNOWN = "unknown"


@dataclass
class SourcePolicy:
    """K5-8: Per-source policy with weights and signals."""
    source_ref: str
    policy_version_ref: str
    peer_review_status: PeerReviewStatus = PeerReviewStatus.UNKNOWN
    oa_status: OAStatus = OAStatus.UNKNOWN
    retraction_status: str = "not_retracted"
    editorial_risk_flags: List[str] = field(default_factory=list)
    source_weight: float = 1.0  # 0.0 for retracted, 1.0 for normal
    evidence_serving_policy: str = "standard"  # standard, restricted, blocked
    receipt_ref: Optional[str] = None


@dataclass
class SourcePolicyOverride:
    """Manual override with expiry."""
    source_ref: str
    override_weight: float
    reason: str
    expires_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    receipt_ref: Optional[str] = None


class SourcePolicyRegistry:
    """K5-8: Manages source policies and triggers tier recompute on changes."""

    def __init__(self):
        self._policies: Dict[str, SourcePolicy] = {}
        self._overrides: Dict[str, SourcePolicyOverride] = {}
        self._on_change_callbacks: List[Callable[[str], None]] = []

    def register(self, policy: SourcePolicy) -> None:
        self._policies[policy.source_ref] = policy

    def get_weight(self, source_ref: str) -> float:
        """Get effective weight for a source, considering overrides."""
        override = self._overrides.get(source_ref)
        if override:
            if override.expires_at and override.expires_at < datetime.now(timezone.utc):
                del self._overrides[source_ref]
            else:
                return override.override_weight
        policy = self._policies.get(source_ref)
        return policy.source_weight if policy else 1.0

    def set_retracted(self, source_ref: str, receipt_ref: str = "") -> None:
        """Mark a source as retracted — weight → 0."""
        policy = self._policies.get(source_ref)
        if policy:
            policy.retraction_status = "retracted"
            policy.source_weight = 0.0
            self._notify_change(source_ref)

    def add_override(self, override: SourcePolicyOverride) -> None:
        self._overrides[override.source_ref] = override
        self._notify_change(override.source_ref)

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._on_change_callbacks.append(callback)

    def _notify_change(self, source_ref: str) -> None:
        for cb in self._on_change_callbacks:
            cb(source_ref)

    def get_policy(self, source_ref: str) -> Optional[SourcePolicy]:
        return self._policies.get(source_ref)
