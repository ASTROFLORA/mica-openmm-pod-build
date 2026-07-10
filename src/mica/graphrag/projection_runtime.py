from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


class GraphProjectionState(str, Enum):
    GREEN = "green"
    LAGGING = "lagging"
    DRIFT_DETECTED = "drift_detected"
    QUARANTINED = "quarantined"
    REBUILDING = "rebuilding"
    RESTORED = "restored"
    UNAVAILABLE = "unavailable"


@dataclass
class GraphProjectionScopeStatus:
    scope_ref: str
    projection_ref: str
    target: str = "neo4j"
    state: GraphProjectionState = GraphProjectionState.GREEN
    projected_traversal_enabled: bool = True
    last_drift_signal_ref: Optional[str] = None
    last_checkpoint_ref: Optional[str] = None
    replay_backlog: int = 0
    lag_seconds: Optional[float] = None
    last_sync_at: Optional[datetime] = None
    last_action_ref: Optional[str] = None
    reason: str = ""


@dataclass
class GraphProjectionDriftSignal:
    signal_ref: str
    scope_ref: str
    projection_ref: str
    target: str = "neo4j"
    reason: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GraphProjectionLagSignal:
    signal_ref: str
    scope_ref: str
    projection_ref: str
    lag_seconds: float
    replay_backlog: int
    checkpoint_ref: Optional[str] = None
    target: str = "neo4j"
    max_lag_seconds: float = 60.0
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GraphProjectionReconcileAction:
    action_ref: str
    scope_ref: str
    projection_ref: str
    action: str
    reason: str
    checkpoint_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GraphProjectionGuardDecision:
    scope_ref: str
    projection_ref: Optional[str]
    target: Optional[str]
    state: str
    traversal_source: str
    projected_traversal_allowed: bool
    fallback_used: bool
    drift_signal_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    replay_backlog: int = 0
    lag_seconds: Optional[float] = None
    reconcile_action_ref: Optional[str] = None
    reason: str = ""


class GraphProjectionRuntime:
    """Optional GraphRAG projection guard over a non-authoritative graph projection.

    This runtime does not make the projection authoritative. It only tracks
    whether a scope is healthy enough to use projected traversal. Canonical
    Postgres traversal remains the fallback and source of truth.
    """

    def __init__(self) -> None:
        self._scopes: Dict[str, GraphProjectionScopeStatus] = {}
        self._actions: List[GraphProjectionReconcileAction] = []

    def register_scope(self, status: GraphProjectionScopeStatus) -> GraphProjectionScopeStatus:
        self._scopes[status.scope_ref] = status
        return status

    @staticmethod
    def _action_ref(action: str, scope_ref: str) -> str:
        slug = scope_ref.replace("scope://", "").replace("/", "_")
        return f"graphproj://{action}/{slug}/{datetime.now(timezone.utc).isoformat()}"

    def _record_action(
        self,
        *,
        action: str,
        scope_ref: str,
        projection_ref: str,
        reason: str,
        checkpoint_ref: Optional[str] = None,
    ) -> GraphProjectionReconcileAction:
        recorded = GraphProjectionReconcileAction(
            action_ref=self._action_ref(action, scope_ref),
            scope_ref=scope_ref,
            projection_ref=projection_ref,
            action=action,
            reason=reason,
            checkpoint_ref=checkpoint_ref,
        )
        self._actions.append(recorded)
        status = self._scopes.get(scope_ref)
        if status is not None:
            status.last_action_ref = recorded.action_ref
        return recorded

    def report_drift(self, signal: GraphProjectionDriftSignal) -> GraphProjectionScopeStatus:
        status = self._scopes.get(signal.scope_ref)
        if status is None:
            status = GraphProjectionScopeStatus(
                scope_ref=signal.scope_ref,
                projection_ref=signal.projection_ref,
                target=signal.target,
            )
            self._scopes[signal.scope_ref] = status
        status.state = GraphProjectionState.QUARANTINED
        status.projected_traversal_enabled = False
        status.last_drift_signal_ref = signal.signal_ref
        status.reason = signal.reason or "projection_drift_detected"
        self._record_action(
            action="quarantine",
            scope_ref=signal.scope_ref,
            projection_ref=signal.projection_ref,
            reason=status.reason,
        )
        return status

    def report_lag(self, signal: GraphProjectionLagSignal) -> GraphProjectionScopeStatus:
        status = self._scopes.get(signal.scope_ref)
        if status is None:
            status = GraphProjectionScopeStatus(
                scope_ref=signal.scope_ref,
                projection_ref=signal.projection_ref,
                target=signal.target,
            )
            self._scopes[signal.scope_ref] = status
        status.lag_seconds = max(0.0, float(signal.lag_seconds))
        status.replay_backlog = max(0, int(signal.replay_backlog))
        status.last_checkpoint_ref = signal.checkpoint_ref
        status.reason = "projection_replay_lagging"
        status.state = GraphProjectionState.LAGGING
        status.projected_traversal_enabled = False
        self._record_action(
            action="fallback",
            scope_ref=signal.scope_ref,
            projection_ref=signal.projection_ref,
            reason=(
                f"lag_seconds={status.lag_seconds:.3f}>"
                f"{float(signal.max_lag_seconds):.3f} or replay_backlog={status.replay_backlog}"
            ),
            checkpoint_ref=signal.checkpoint_ref,
        )
        return status

    def complete_sync(
        self,
        *,
        scope_ref: str,
        projection_ref: str,
        checkpoint_ref: Optional[str],
        lag_seconds: float = 0.0,
        replay_backlog: int = 0,
        target: str = "neo4j",
    ) -> GraphProjectionScopeStatus:
        status = self._scopes.get(scope_ref)
        if status is None:
            status = GraphProjectionScopeStatus(
                scope_ref=scope_ref,
                projection_ref=projection_ref,
                target=target,
            )
            self._scopes[scope_ref] = status
        status.state = GraphProjectionState.RESTORED if status.last_action_ref else GraphProjectionState.GREEN
        status.projected_traversal_enabled = True
        status.lag_seconds = max(0.0, float(lag_seconds))
        status.replay_backlog = max(0, int(replay_backlog))
        status.last_checkpoint_ref = checkpoint_ref
        status.last_sync_at = datetime.now(timezone.utc)
        status.reason = "projection_healthy"
        self._record_action(
            action="restore" if status.state == GraphProjectionState.RESTORED else "sync",
            scope_ref=scope_ref,
            projection_ref=projection_ref,
            reason="projection_backlog_cleared",
            checkpoint_ref=checkpoint_ref,
        )
        return status

    def start_rebuild(self, *, scope_ref: str, checkpoint_ref: str) -> GraphProjectionScopeStatus:
        status = self._scopes.get(scope_ref)
        if status is None:
            raise ValueError(f"unknown projection scope: {scope_ref}")
        status.state = GraphProjectionState.REBUILDING
        status.projected_traversal_enabled = False
        status.last_checkpoint_ref = checkpoint_ref
        status.reason = "projection_rebuild_started"
        self._record_action(
            action="rebuild",
            scope_ref=scope_ref,
            projection_ref=status.projection_ref,
            reason=status.reason,
            checkpoint_ref=checkpoint_ref,
        )
        return status

    def list_actions(self, scope_ref: Optional[str] = None) -> List[GraphProjectionReconcileAction]:
        if scope_ref is None:
            return list(self._actions)
        return [action for action in self._actions if action.scope_ref == scope_ref]

    def inspect_scope(self, scope_ref: str) -> GraphProjectionGuardDecision:
        status = self._scopes.get(scope_ref)
        if status is None:
            return GraphProjectionGuardDecision(
                scope_ref=scope_ref,
                projection_ref=None,
                target=None,
                state=GraphProjectionState.UNAVAILABLE.value,
                traversal_source="canonical_postgres",
                projected_traversal_allowed=False,
                fallback_used=True,
                drift_signal_ref=None,
                checkpoint_ref=None,
                replay_backlog=0,
                lag_seconds=None,
                reconcile_action_ref=None,
                reason="projection_runtime_scope_unregistered",
            )

        projected_traversal_allowed = (
            status.state in {GraphProjectionState.GREEN, GraphProjectionState.RESTORED}
            and status.projected_traversal_enabled
        )
        return GraphProjectionGuardDecision(
            scope_ref=scope_ref,
            projection_ref=status.projection_ref,
            target=status.target,
            state=status.state.value,
            traversal_source="projected_graphdb" if projected_traversal_allowed else "canonical_postgres",
            projected_traversal_allowed=projected_traversal_allowed,
            fallback_used=not projected_traversal_allowed,
            drift_signal_ref=status.last_drift_signal_ref,
            checkpoint_ref=status.last_checkpoint_ref,
            replay_backlog=status.replay_backlog,
            lag_seconds=status.lag_seconds,
            reconcile_action_ref=status.last_action_ref,
            reason=status.reason,
        )


__all__ = [
    "GraphProjectionDriftSignal",
    "GraphProjectionGuardDecision",
    "GraphProjectionLagSignal",
    "GraphProjectionReconcileAction",
    "GraphProjectionRuntime",
    "GraphProjectionScopeStatus",
    "GraphProjectionState",
]
