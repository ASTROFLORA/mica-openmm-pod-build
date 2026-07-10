from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


_BUDGET_CLASSES = {"interactive", "background", "security", "exploratory"}
_POLICY_BY_BUDGET_CLASS = {
    "interactive": "interactive",
    "background": "background",
    "security": "impact_frontier",
    "exploratory": "interactive",
}
_LIMIT_CAP_BY_BUDGET_CLASS = {
    "interactive": 50,
    "background": 200,
    "security": 100,
    "exploratory": 12,
}


@dataclass(frozen=True)
class TenantBudgetDecision:
    budget_ref: str
    budget_class: str
    requested_policy: str
    effective_policy: str
    requested_limit: int
    effective_limit: int
    budget_status: str
    budget_reason: Optional[str]
    remaining_budget_ratio: Optional[float]


class TenantBudgetEnforcer:
    """Budget guardrail for GraphRAG traversal.

    The canonical traversal store remains the authority for cost emission.
    This runtime only decides how aggressively a tenant is allowed to traverse
    before the query reaches the store.
    """

    def __init__(
        self,
        *,
        budget_states: Optional[Mapping[str, Mapping[str, object]]] = None,
    ) -> None:
        self._budget_states = {
            str(key): dict(value)
            for key, value in (budget_states or {}).items()
        }

    def plan(
        self,
        *,
        tenant_id: Optional[str],
        policy: str,
        limit: int,
        budget_ref: Optional[str],
    ) -> TenantBudgetDecision:
        requested_policy = str(policy or "interactive").strip().lower() or "interactive"
        requested_limit = max(1, int(limit))
        normalized_budget_ref = str(budget_ref or f"budget://graphrag/{requested_policy}").strip()
        budget_class = self._infer_budget_class(
            budget_ref=normalized_budget_ref,
            requested_policy=requested_policy,
        )
        effective_policy = _POLICY_BY_BUDGET_CLASS[budget_class]
        default_cap = _LIMIT_CAP_BY_BUDGET_CLASS[budget_class]
        state = self._resolve_state(budget_ref=normalized_budget_ref, tenant_id=tenant_id)
        remaining_ratio = self._normalize_ratio(state.get("remaining_budget_ratio"))
        hard_cap = max(1, int(state.get("hard_cap", default_cap)))

        effective_limit = min(requested_limit, hard_cap)
        budget_status = "ok"
        budget_reason: Optional[str] = None

        if remaining_ratio is not None and remaining_ratio <= 0.0:
            effective_limit = min(effective_limit, _LIMIT_CAP_BY_BUDGET_CLASS["exploratory"])
            effective_policy = _POLICY_BY_BUDGET_CLASS["exploratory"]
            budget_class = "exploratory"
            budget_status = "degraded"
            budget_reason = "tenant_budget_exhausted"
        elif remaining_ratio is not None and remaining_ratio < 0.25:
            effective_limit = min(effective_limit, max(4, hard_cap // 4))
            effective_policy = _POLICY_BY_BUDGET_CLASS["exploratory"]
            budget_class = "exploratory"
            budget_status = "degraded"
            budget_reason = "tenant_budget_low"
        elif remaining_ratio is not None and remaining_ratio < 0.5:
            effective_limit = min(effective_limit, max(8, hard_cap // 2))
            budget_status = "degraded"
            budget_reason = "tenant_budget_guardrail"
        elif requested_limit > effective_limit:
            budget_status = "degraded"
            budget_reason = "budget_class_limit_cap"

        return TenantBudgetDecision(
            budget_ref=normalized_budget_ref,
            budget_class=budget_class,
            requested_policy=requested_policy,
            effective_policy=effective_policy,
            requested_limit=requested_limit,
            effective_limit=max(1, effective_limit),
            budget_status=budget_status,
            budget_reason=budget_reason,
            remaining_budget_ratio=remaining_ratio,
        )

    def _resolve_state(
        self,
        *,
        budget_ref: str,
        tenant_id: Optional[str],
    ) -> Mapping[str, object]:
        if budget_ref in self._budget_states:
            return self._budget_states[budget_ref]
        if tenant_id and tenant_id in self._budget_states:
            return self._budget_states[tenant_id]
        return {}

    @staticmethod
    def _normalize_ratio(raw_value: object) -> Optional[float]:
        if raw_value is None:
            return None
        try:
            ratio = float(raw_value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, ratio))

    @staticmethod
    def _infer_budget_class(*, budget_ref: str, requested_policy: str) -> str:
        lowered = budget_ref.lower()
        for budget_class in _BUDGET_CLASSES:
            if f"/{budget_class}" in lowered or lowered.endswith(f":{budget_class}"):
                return budget_class
        if requested_policy == "background":
            return "background"
        if requested_policy == "impact_frontier":
            return "security"
        return "interactive"


__all__ = [
    "TenantBudgetDecision",
    "TenantBudgetEnforcer",
]
