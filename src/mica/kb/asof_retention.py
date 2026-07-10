"""
KB Asof Index Retention — K6-2 (KB Slice 4)

Hot/warm/cold retention for kb_asof_index.
Hot Postgres (90d) → warm read-mostly (12-24m) → cold object store (indefinite).
Append-only; REINDEX CONCURRENTLY if bloat > threshold.

Key objects:
- RetentionTier: hot/warm/cold classification
- AsofIndexPartition: partition metadata
- AsofRetentionPolicy: retention rules
- AsofRetentionRunner: manages partition lifecycle
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class RetentionTier(str, Enum):
    HOT = "hot"        # Postgres, <90d, full read/write
    WARM = "warm"      # read-mostly, 12-24m
    COLD = "cold"      # object store, indefinite


@dataclass
class AsofIndexPartition:
    """Metadata for a single asof index partition."""
    partition_id: str
    tier: RetentionTier = RetentionTier.HOT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    row_count: int = 0
    size_bytes: int = 0
    bloat_ratio: float = 0.0  # 0.0 = no bloat, 1.0 = 100% bloat
    last_reindex_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    storage_ref: Optional[str] = None  # GCS path for cold


@dataclass
class AsofRetentionPolicy:
    """K6-2: Retention rules for asof index."""
    hot_ttl_days: int = 90
    warm_ttl_days: int = 730  # 24 months
    bloat_reindex_threshold: float = 0.30  # 30% bloat triggers REINDEX
    max_hot_partitions: int = 12  # monthly
    partition_split_strategy: str = "monthly"
    policy_version_ref: str = "v1"


@dataclass
class PartitionAction:
    """Action taken on a partition."""
    partition_id: str
    action: str  # "reindex", "detach", "archive", "delete"
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


@dataclass
class RetentionReceipt:
    """Receipt for retention lifecycle actions."""
    receipt_ref: str
    actions_taken: List[PartitionAction] = field(default_factory=list)
    partitions_reindexed: int = 0
    partitions_detached: int = 0
    partitions_archived: int = 0
    partitions_deleted: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AsofRetentionManager:
    """K6-2: Manages asof index hot/warm/cold lifecycle.

    Append-only; REINDEX CONCURRENTLY if bloat > threshold.
    Detach old partitions to archive.
    Red-line: No bitemporal full scan in prod.
    """

    def __init__(self, policy: Optional[AsofRetentionPolicy] = None) -> None:
        self._policy = policy or AsofRetentionPolicy()
        self._partitions: Dict[str, AsofIndexPartition] = {}
        self._actions: List[PartitionAction] = []

    def register_partition(self, partition: AsofIndexPartition) -> AsofIndexPartition:
        self._partitions[partition.partition_id] = partition
        return partition

    def get_partition(self, partition_id: str) -> Optional[AsofIndexPartition]:
        return self._partitions.get(partition_id)

    def list_partitions(self, tier: Optional[RetentionTier] = None) -> List[AsofIndexPartition]:
        parts = list(self._partitions.values())
        if tier:
            parts = [p for p in parts if p.tier == tier]
        return sorted(parts, key=lambda p: p.created_at)

    def check_bloat(self, partition_id: str) -> Optional[PartitionAction]:
        """Check if partition needs REINDEX CONCURRENTLY."""
        part = self._partitions.get(partition_id)
        if not part:
            return None
        if part.bloat_ratio >= self._policy.bloat_reindex_threshold:
            action = PartitionAction(
                partition_id=partition_id,
                action="reindex",
                reason=f"bloat_ratio={part.bloat_ratio:.2f}>={self._policy.bloat_reindex_threshold}",
            )
            self._actions.append(action)
            part.last_reindex_at = datetime.now(timezone.utc)
            part.bloat_ratio = 0.0  # reset after reindex
            return action
        return None

    def evaluate_tier_transitions(self, now: Optional[datetime] = None) -> List[PartitionAction]:
        """Evaluate which partitions should move between tiers."""
        now = now or datetime.now(timezone.utc)
        actions = []
        for part in self._partitions.values():
            age_days = (now - part.created_at).days
            if part.tier == RetentionTier.HOT and age_days > self._policy.hot_ttl_days:
                action = PartitionAction(
                    partition_id=part.partition_id,
                    action="detach",
                    reason=f"hot_age={age_days}d>{self._policy.hot_ttl_days}d",
                )
                part.tier = RetentionTier.WARM
                actions.append(action)
                self._actions.append(action)
            elif part.tier == RetentionTier.WARM and age_days > self._policy.warm_ttl_days:
                action = PartitionAction(
                    partition_id=part.partition_id,
                    action="archive",
                    reason=f"warm_age={age_days}d>{self._policy.warm_ttl_days}d",
                )
                part.tier = RetentionTier.COLD
                part.archived_at = now
                actions.append(action)
                self._actions.append(action)
        return actions

    def generate_receipt(self) -> RetentionReceipt:
        """Generate retention lifecycle receipt."""
        return RetentionReceipt(
            receipt_ref=f"receipt://asof-retention/{datetime.now(timezone.utc).isoformat()}",
            actions_taken=list(self._actions),
            partitions_reindexed=sum(1 for a in self._actions if a.action == "reindex"),
            partitions_detached=sum(1 for a in self._actions if a.action == "detach"),
            partitions_archived=sum(1 for a in self._actions if a.action == "archive"),
            partitions_deleted=sum(1 for a in self._actions if a.action == "delete"),
        )

    def get_policy(self) -> AsofRetentionPolicy:
        return self._policy
