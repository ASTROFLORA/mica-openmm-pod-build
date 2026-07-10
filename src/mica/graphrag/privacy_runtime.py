from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class GraphAggregatePolicyDecision:
    metric: str
    scope_ref: str
    private_scope: bool
    blocked: bool
    exposure: str
    threshold: int
    exact_value_exposed: bool
    reason: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class LeakageCanaryResult:
    metric: str
    scope_ref: str
    private_scope: bool
    leakage_detected: bool
    exact_value_exposed: bool
    reason: str
    status: str


class GraphAggregatePolicy:
    """Fail-closed aggregate exposure policy for GraphRAG.

    Private scopes may inspect aggregate signals only through redacted
    representations. Global scopes may receive exact values.
    """

    def __init__(self, *, k_anon_threshold: int = 3) -> None:
        if k_anon_threshold <= 1:
            raise ValueError("k_anon_threshold must be > 1")
        self.k_anon_threshold = k_anon_threshold

    def decide(
        self,
        *,
        metric: str,
        scope_ref: str,
        global_only: bool,
        exact_value: Optional[int] = None,
        exact_payload: Optional[dict[str, Any]] = None,
    ) -> GraphAggregatePolicyDecision:
        private_scope = not global_only
        if metric == "centrality" and private_scope:
            return GraphAggregatePolicyDecision(
                metric=metric,
                scope_ref=scope_ref,
                private_scope=True,
                blocked=True,
                exposure="blocked",
                threshold=self.k_anon_threshold,
                exact_value_exposed=False,
                reason="centrality_private_scope_blocked",
                payload={},
            )

        if not private_scope:
            return GraphAggregatePolicyDecision(
                metric=metric,
                scope_ref=scope_ref,
                private_scope=False,
                blocked=False,
                exposure="public_exact",
                threshold=self.k_anon_threshold,
                exact_value_exposed=True,
                reason="global_scope_exact_allowed",
                payload=dict(exact_payload or ({metric: exact_value} if exact_value is not None else {})),
            )

        if metric in {"edge_count", "degree"}:
            normalized = max(0, int(exact_value or 0))
            if normalized < self.k_anon_threshold:
                bucket = f"lt_{self.k_anon_threshold}"
            else:
                bucket = f"ge_{self.k_anon_threshold}"
            return GraphAggregatePolicyDecision(
                metric=metric,
                scope_ref=scope_ref,
                private_scope=True,
                blocked=False,
                exposure="k_anon_bucket",
                threshold=self.k_anon_threshold,
                exact_value_exposed=False,
                reason="private_scope_bucketed",
                payload={"bucket": bucket},
            )

        return GraphAggregatePolicyDecision(
            metric=metric,
            scope_ref=scope_ref,
            private_scope=True,
            blocked=True,
            exposure="blocked",
            threshold=self.k_anon_threshold,
            exact_value_exposed=False,
            reason="unsupported_private_aggregate",
            payload={},
        )


class LeakageCanary:
    """Leakage detector for aggregate exposure policies."""

    def inspect_current(self, *, decision: GraphAggregatePolicyDecision) -> LeakageCanaryResult:
        leakage_detected = decision.private_scope and decision.exact_value_exposed
        return LeakageCanaryResult(
            metric=decision.metric,
            scope_ref=decision.scope_ref,
            private_scope=decision.private_scope,
            leakage_detected=leakage_detected,
            exact_value_exposed=decision.exact_value_exposed,
            reason=decision.reason,
            status="fail" if leakage_detected else "pass",
        )

    def compare_before_after(
        self,
        *,
        metric: str,
        scope_ref: str,
        private_scope: bool,
        before_exact: int,
        after_exact: int,
        before_payload: dict[str, Any],
        after_payload: dict[str, Any],
    ) -> LeakageCanaryResult:
        leakage_detected = bool(
            private_scope
            and before_exact != after_exact
            and before_payload != after_payload
        )
        return LeakageCanaryResult(
            metric=metric,
            scope_ref=scope_ref,
            private_scope=private_scope,
            leakage_detected=leakage_detected,
            exact_value_exposed=False,
            reason="hidden_edge_count_changed" if before_exact != after_exact else "no_hidden_change",
            status="fail" if leakage_detected else "pass",
        )


__all__ = [
    "GraphAggregatePolicy",
    "GraphAggregatePolicyDecision",
    "LeakageCanary",
    "LeakageCanaryResult",
]
