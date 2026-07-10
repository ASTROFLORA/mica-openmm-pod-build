"""
KB GC + Retention — K5-11 (KB Slice 3)

GC purges projections/caches; MUDO/ATOM lineage is preserved.
Legal hold blocks all destructive operations.
Projections are disposable; lineage is not.

Key objects:
- RetentionPolicy: defines retention rules per object kind
- LegalHoldGuard: blocks GC when legal hold is active
- ProjectionGCRunner: purges stale projections
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ObjectKind(str, Enum):
    PROJECTION = "projection"
    CACHE = "cache"
    SNAPSHOT = "snapshot"
    LINEAGE = "lineage"
    EVIDENCE = "evidence"
    CLAIM = "claim"


class LineagePolicy(str, Enum):
    PRESERVE = "preserve"  # never delete lineage
    ARCHIVE = "archive"  # move to cold storage
    DELETE = "delete"  # only with legal approval


@dataclass
class RetentionPolicy:
    """Retention rules for an object kind."""
    object_kind: ObjectKind
    hot_ttl_days: int = 90
    warm_ttl_days: int = 365
    cold_ttl_days: int = -1  # -1 = indefinite
    lineage_policy: LineagePolicy = LineagePolicy.PRESERVE
    legal_hold_behavior: str = "block_all_destructive"
    policy_version_ref: str = "v1"
    receipt_ref: Optional[str] = None


@dataclass
class LegalHoldGuard:
    """Blocks GC when legal hold is active."""
    hold_ref: str
    scope_ref: str
    reason: str
    placed_by: str
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    receipt_ref: Optional[str] = None

    def is_active(self) -> bool:
        if self.expires_at and self.expires_at < datetime.now(timezone.utc):
            return False
        return True


@dataclass
class ProjectionPurgeReceipt:
    """Receipt of projection purge with lineage preserved."""
    receipt_ref: str
    purged_count: int
    lineage_preserved_count: int
    scope_ref: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ProjectionGCRunner:
    """K5-11: Purges stale projections; never touches lineage."""

    def __init__(self):
        self._policies: Dict[str, RetentionPolicy] = {}
        self._legal_holds: Dict[str, LegalHoldGuard] = {}

    def register_policy(self, policy: RetentionPolicy) -> None:
        self._policies[policy.object_kind.value] = policy

    def place_legal_hold(self, hold: LegalHoldGuard) -> None:
        self._legal_holds[hold.hold_ref] = hold

    def has_active_hold(self, scope_ref: str) -> bool:
        for hold in self._legal_holds.values():
            if hold.scope_ref == scope_ref and hold.is_active():
                return True
        return False

    def purge_projections(
        self,
        objects: List[Dict[str, Any]],
        scope_ref: str = "global",
    ) -> ProjectionPurgeReceipt:
        """Purge stale projections. Never touches lineage."""
        if self.has_active_hold(scope_ref):
            return ProjectionPurgeReceipt(
                receipt_ref=f"gc_blocked/{scope_ref}",
                purged_count=0,
                lineage_preserved_count=0,
                scope_ref=scope_ref,
            )

        purged = 0
        preserved = 0
        for obj in objects:
            kind = obj.get("kind", "projection")
            lineage = obj.get("lineage", False)
            if lineage:
                preserved += 1
                continue
            if kind in ("projection", "cache"):
                purged += 1

        return ProjectionPurgeReceipt(
            receipt_ref=f"gc_purge/{scope_ref}/{purged}",
            purged_count=purged,
            lineage_preserved_count=preserved,
            scope_ref=scope_ref,
        )
