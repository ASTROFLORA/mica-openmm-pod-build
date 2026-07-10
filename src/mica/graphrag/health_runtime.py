from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class GraphHealthState(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class GraphHealthScopeStatus:
    scope_ref: str
    state: GraphHealthState
    reason: str
    blocker_ref: Optional[str] = None
    warning_ref: Optional[str] = None
    source: str = "graph_health_runtime"


@dataclass(frozen=True)
class GraphHealthGateDecision:
    scope_ref: str
    state: str
    allow_answer: bool
    blocker_ref: Optional[str]
    warning_ref: Optional[str]
    reason: str
    source: str


class GraphHealthRuntime:
    """Serve-time graph answer health gate.

    This runtime is advisory for `green/yellow` and fail-closed for `red`.
    It does not replace Postgres/MUDO authority; it only decides whether a
    user-facing answer may be served right now for a given scope.
    """

    def __init__(self) -> None:
        self._scopes: Dict[str, GraphHealthScopeStatus] = {}

    def register_scope(self, status: GraphHealthScopeStatus) -> GraphHealthScopeStatus:
        self._scopes[status.scope_ref] = status
        return status

    def inspect_scope(self, scope_ref: str) -> GraphHealthGateDecision:
        status = self._scopes.get(scope_ref)
        if status is None:
            return GraphHealthGateDecision(
                scope_ref=scope_ref,
                state=GraphHealthState.GREEN.value,
                allow_answer=True,
                blocker_ref=None,
                warning_ref=None,
                reason="graph_health_runtime_unregistered",
                source="graph_health_runtime",
            )

        return GraphHealthGateDecision(
            scope_ref=scope_ref,
            state=status.state.value,
            allow_answer=status.state is not GraphHealthState.RED,
            blocker_ref=status.blocker_ref,
            warning_ref=status.warning_ref,
            reason=status.reason,
            source=status.source,
        )


__all__ = [
    "GraphHealthGateDecision",
    "GraphHealthRuntime",
    "GraphHealthScopeStatus",
    "GraphHealthState",
]
