"""
KB Projection Reconciler — K6-7 (KB Slice 4)

Drift detection for PROV-O/Neo4j projectors.
On drift: quarantine + fallback to MUDO/Postgres canonical query + rebuild from checkpoint.

Key objects:
- ProjectionState: green/lagging/drift_detected/quarantined/rebuilding/restored
- DriftSignal: evidence of projection divergence
- ReconcileAction: quarantine/fallback/rebuild receipt
- ProjectionReconciler: orchestrates lifecycle
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ProjectionState(str, Enum):
    GREEN = "green"
    LAGGING = "lagging"
    DRIFT_DETECTED = "drift_detected"
    QUARANTINED = "quarantined"
    REBUILDING = "rebuilding"
    RESTORED = "restored"


@dataclass
class ProjectionMetadata:
    """Metadata for a projection target."""
    projection_ref: str
    target: str  # "neo4j", "provo", "milvus"
    scope_ref: str
    state: ProjectionState = ProjectionState.GREEN
    last_sync_at: Optional[datetime] = None
    last_drift_check_at: Optional[datetime] = None
    checkpoint_ref: Optional[str] = None
    source_hash: Optional[str] = None
    projection_hash: Optional[str] = None


@dataclass
class DriftSignal:
    """Evidence of projection divergence."""
    signal_ref: str
    projection_ref: str
    source_hash: str
    projection_hash: str
    drift_type: str  # "hash_mismatch", "count_mismatch", "staleness"
    details: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ReconcileAction:
    """Action taken during reconciliation."""
    action_ref: str
    projection_ref: str
    action: str  # "quarantine", "fallback", "rebuild", "restore"
    reason: str
    fallback_query: Optional[str] = None  # canonical MUDO/Postgres query
    checkpoint_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


class ProjectionReconciler:
    """K6-7: Drift detection + quarantine + fallback-to-MUDO + rebuild-from-checkpoint.

    States: green → lagging → drift_detected → quarantined → rebuilding → restored.

    Red-line: No projection drift served as truth. MUDO is authority.
    """

    def __init__(self) -> None:
        self._projections: Dict[str, ProjectionMetadata] = {}
        self._drift_signals: List[DriftSignal] = []
        self._actions: List[ReconcileAction] = []
        self._on_quarantine: Optional[Callable[[str, str], None]] = None

    def register_projection(self, projection: ProjectionMetadata) -> ProjectionMetadata:
        self._projections[projection.projection_ref] = projection
        return projection

    def get_projection(self, projection_ref: str) -> Optional[ProjectionMetadata]:
        return self._projections.get(projection_ref)

    def report_drift(self, signal: DriftSignal) -> ReconcileAction:
        """Report drift → auto-quarantine + fallback."""
        proj = self._projections.get(signal.projection_ref)
        if not proj:
            raise ValueError(f"unknown projection: {signal.projection_ref}")

        self._drift_signals.append(signal)

        # quarantine
        proj.state = ProjectionState.QUARANTINED
        quarantine_action = ReconcileAction(
            action_ref=f"reconcile://quarantine/{signal.projection_ref}/{datetime.now(timezone.utc).isoformat()}",
            projection_ref=signal.projection_ref,
            action="quarantine",
            reason=f"drift_type={signal.drift_type}, source_hash={signal.source_hash[:8]}",
        )
        self._actions.append(quarantine_action)

        if self._on_quarantine:
            self._on_quarantine(signal.projection_ref, quarantine_action.action_ref)

        return quarantine_action

    def fallback_to_canonical(self, projection_ref: str) -> ReconcileAction:
        """Switch to canonical MUDO/Postgres query."""
        proj = self._projections.get(projection_ref)
        if proj:
            proj.state = ProjectionState.QUARANTINED  # stays quarantined during fallback

        action = ReconcileAction(
            action_ref=f"reconcile://fallback/{projection_ref}/{datetime.now(timezone.utc).isoformat()}",
            projection_ref=projection_ref,
            action="fallback",
            reason="canonical_query_mudo_postgres",
            fallback_query=f"SELECT * FROM kb_claims WHERE scope_ref = '{proj.scope_ref}'" if proj else "",
        )
        self._actions.append(action)
        return action

    def start_rebuild(self, projection_ref: str, checkpoint_ref: str) -> ReconcileAction:
        """Start rebuild from checkpoint."""
        proj = self._projections.get(projection_ref)
        if proj:
            proj.state = ProjectionState.REBUILDING
            proj.checkpoint_ref = checkpoint_ref

        action = ReconcileAction(
            action_ref=f"reconcile://rebuild/{projection_ref}/{datetime.now(timezone.utc).isoformat()}",
            projection_ref=projection_ref,
            action="rebuild",
            reason=f"checkpoint={checkpoint_ref}",
            checkpoint_ref=checkpoint_ref,
        )
        self._actions.append(action)
        return action

    def complete_rebuild(self, projection_ref: str, new_hash: str) -> ReconcileAction:
        """Complete rebuild → restored → green."""
        proj = self._projections.get(projection_ref)
        if proj:
            proj.state = ProjectionState.GREEN
            proj.projection_hash = new_hash
            proj.last_sync_at = datetime.now(timezone.utc)

        action = ReconcileAction(
            action_ref=f"reconcile://restore/{projection_ref}/{datetime.now(timezone.utc).isoformat()}",
            projection_ref=projection_ref,
            action="restore",
            reason=f"new_hash={new_hash[:8]}",
        )
        self._actions.append(action)
        return action

    def get_quarantined(self) -> List[ProjectionMetadata]:
        return [p for p in self._projections.values() if p.state == ProjectionState.QUARANTINED]

    def list_projections(self, state: Optional[ProjectionState] = None) -> List[ProjectionMetadata]:
        projs = list(self._projections.values())
        if state:
            projs = [p for p in projs if p.state == state]
        return projs

    def list_actions(self, projection_ref: Optional[str] = None) -> List[ReconcileAction]:
        actions = self._actions
        if projection_ref:
            actions = [a for a in actions if a.projection_ref == projection_ref]
        return actions
