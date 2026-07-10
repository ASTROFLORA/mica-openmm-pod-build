from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mica.graphrag.cost_runtime import TenantBudgetDecision, TenantBudgetEnforcer
from mica.graphrag.projection_runtime import GraphProjectionGuardDecision


@dataclass(frozen=True)
class TraversalDegradePolicy:
    policy: str
    policy_ref: str
    latency_budget_ms: int


@dataclass(frozen=True)
class TraversalDegradationEnvelope:
    policy_ref: str
    authoritative_source: str
    projection_state: str
    partial_response: bool
    partial_reason: Optional[str]
    fallback_used: bool
    fallback_reason: Optional[str]
    budget_class: str
    budget_status: str
    budget_reason: Optional[str]
    requested_limit: int
    effective_limit: int
    remaining_budget_ratio: Optional[float]


@dataclass(frozen=True)
class TraversalServeDecision:
    traversal: Any
    projection_guard: GraphProjectionGuardDecision
    degradation: TraversalDegradationEnvelope


def _policy_ref(policy: str) -> str:
    normalized = str(policy or "interactive").strip().lower() or "interactive"
    return f"degrade://{normalized}_graph_v1"


def _partial_reason(status: str) -> Optional[str]:
    if status == "partial_budget_exhausted":
        return "budget_exhausted_or_policy_capped"
    return None


def build_traversal_degrade_policy(*, policy: str, latency_budget_ms: int) -> TraversalDegradePolicy:
    return TraversalDegradePolicy(
        policy=policy,
        policy_ref=_policy_ref(policy),
        latency_budget_ms=int(latency_budget_ms),
    )


class BudgetedTraversalEngine:
    """Serve-time GraphRAG traversal selector over projection + canonical store.

    Doctrine:
    - Neo4j accelerates but is never authoritative.
    - Canonical Postgres traversal is the fail-closed fallback.
    - Partial results must be explicit and reconstructible.
    """

    def __init__(
        self,
        *,
        store: Any,
        projection_runtime: Any = None,
        budget_enforcer: Optional[TenantBudgetEnforcer] = None,
    ) -> None:
        self._store = store
        self._projection_runtime = projection_runtime
        self._budget_enforcer = budget_enforcer or TenantBudgetEnforcer()

    async def hop1(
        self,
        *,
        seed_nodes: list[str],
        limit: int,
        user_id: str,
        workspace_id: Optional[str],
        global_only: bool,
        policy: str,
        budget_ref: Optional[str],
        scope_ref: str,
    ) -> TraversalServeDecision:
        budget_decision = self._budget_enforcer.plan(
            tenant_id=user_id,
            policy=policy,
            limit=limit,
            budget_ref=budget_ref,
        )
        canonical_kwargs = {
            "seed_nodes": seed_nodes,
            "limit": budget_decision.effective_limit,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "global_only": global_only,
            "policy": budget_decision.effective_policy,
            "budget_ref": budget_ref,
        }
        default_guard = GraphProjectionGuardDecision(
            scope_ref=scope_ref,
            projection_ref=None,
            target=None,
            state="unavailable",
            traversal_source="canonical_postgres",
            projected_traversal_allowed=False,
            fallback_used=True,
            reason="projection_runtime_missing",
        )

        if self._projection_runtime is None:
            traversal = await self._store.budgeted_hop1_edges(**canonical_kwargs)
            return self._decision_from(
                traversal=traversal,
                projection_guard=default_guard,
                authoritative_source="canonical_postgres",
                fallback_reason=default_guard.reason,
                budget_decision=budget_decision,
            )

        inspect_scope = getattr(self._projection_runtime, "inspect_scope", None)
        if not callable(inspect_scope):
            traversal = await self._store.budgeted_hop1_edges(**canonical_kwargs)
            guard = GraphProjectionGuardDecision(
                **{
                    **default_guard.__dict__,
                    "reason": "projection_runtime_inspect_scope_missing",
                }
            )
            return self._decision_from(
                traversal=traversal,
                projection_guard=guard,
                authoritative_source="canonical_postgres",
                fallback_reason=guard.reason,
                budget_decision=budget_decision,
            )

        projection_guard = inspect_scope(scope_ref)
        if hasattr(projection_guard, "__await__"):
            projection_guard = await projection_guard
        if not isinstance(projection_guard, GraphProjectionGuardDecision):
            raise ValueError("projection runtime returned invalid guard decision")

        if projection_guard.projected_traversal_allowed:
            projected_budgeted_hop1_edges = getattr(self._projection_runtime, "budgeted_hop1_edges", None)
            if callable(projected_budgeted_hop1_edges):
                try:
                    traversal = await projected_budgeted_hop1_edges(
                        **{
                            **canonical_kwargs,
                            "scope_ref": scope_ref,
                        }
                    )
                    return self._decision_from(
                        traversal=traversal,
                        projection_guard=projection_guard,
                        authoritative_source=projection_guard.traversal_source,
                        fallback_reason=None,
                        budget_decision=budget_decision,
                    )
                except Exception as exc:
                    traversal = await self._store.budgeted_hop1_edges(**canonical_kwargs)
                    downgraded_guard = GraphProjectionGuardDecision(
                        **{
                            **projection_guard.__dict__,
                            "traversal_source": "canonical_postgres",
                            "fallback_used": True,
                            "reason": f"projection_runtime_error:{exc.__class__.__name__}",
                        }
                    )
                    return self._decision_from(
                        traversal=traversal,
                        projection_guard=downgraded_guard,
                        authoritative_source="canonical_postgres",
                        fallback_reason=downgraded_guard.reason,
                        budget_decision=budget_decision,
                    )

        traversal = await self._store.budgeted_hop1_edges(**canonical_kwargs)
        return self._decision_from(
            traversal=traversal,
            projection_guard=projection_guard,
            authoritative_source="canonical_postgres",
            fallback_reason=projection_guard.reason if projection_guard.fallback_used else None,
            budget_decision=budget_decision,
        )

    @staticmethod
    def _decision_from(
        *,
        traversal: Any,
        projection_guard: GraphProjectionGuardDecision,
        authoritative_source: str,
        fallback_reason: Optional[str],
        budget_decision: TenantBudgetDecision,
    ) -> TraversalServeDecision:
        degrade_policy = build_traversal_degrade_policy(
            policy=str(getattr(traversal, "policy", "interactive")),
            latency_budget_ms=int(getattr(getattr(traversal, "cost_event", None), "latency_budget_ms", 0) or 0),
        )
        status = str(getattr(traversal, "status", "complete"))
        degradation = TraversalDegradationEnvelope(
            policy_ref=degrade_policy.policy_ref,
            authoritative_source=authoritative_source,
            projection_state=projection_guard.state,
            partial_response=status != "complete",
            partial_reason=_partial_reason(status),
            fallback_used=projection_guard.fallback_used,
            fallback_reason=fallback_reason,
            budget_class=budget_decision.budget_class,
            budget_status=budget_decision.budget_status,
            budget_reason=budget_decision.budget_reason,
            requested_limit=budget_decision.requested_limit,
            effective_limit=budget_decision.effective_limit,
            remaining_budget_ratio=budget_decision.remaining_budget_ratio,
        )
        return TraversalServeDecision(
            traversal=traversal,
            projection_guard=projection_guard,
            degradation=degradation,
        )


__all__ = [
    "BudgetedTraversalEngine",
    "TraversalDegradationEnvelope",
    "TraversalDegradePolicy",
    "TraversalServeDecision",
    "build_traversal_degrade_policy",
]
