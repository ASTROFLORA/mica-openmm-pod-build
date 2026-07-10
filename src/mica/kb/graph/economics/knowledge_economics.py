from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Iterable


_STATUS_CURATION_MINUTES = {
    "active": 2,
    "review_required": 8,
    "deprecated": 12,
    "superseded": 18,
    "retracted": 28,
}

_PUBLIC_VISIBILITY_BY_SCOPE = {
    "global": 1.0,
    "org": 0.85,
    "lab": 0.55,
    "study": 0.35,
}


@dataclass(frozen=True)
class EdgeCostProfile:
    edge_cost_profile_ref: str
    edge_ref: str
    receipt_ref: str
    subsidy_ref: str
    maintenance_class: str
    total_cost: int
    cost_components: dict[str, int]


@dataclass(frozen=True)
class GraphCurationPriority:
    edge_ref: str
    priority_ref: str
    priority_score: float
    recommended_action: str
    scientific_impact: float
    downstream_usage: int
    contradiction_pressure: float
    public_visibility: float
    retraction_risk: float
    maintenance_cost: int


@dataclass(frozen=True)
class KnowledgeMaintenanceAllocation:
    edge_ref: str
    priority_score: float
    maintenance_cost: int
    allocated_units: int
    recommended_action: str


@dataclass(frozen=True)
class KnowledgeMaintenanceBudget:
    budget_ref: str
    subsidy_ref: str
    available_units: int
    allocated_units: int
    remaining_units: int
    allocations: tuple[KnowledgeMaintenanceAllocation, ...]
    uncovered_edge_refs: tuple[str, ...]


class GraphKnowledgeEconomicsRuntime:
    """G4.2 economics layer over canonical GraphRAG curation/traversal seams."""

    policy_ref = "graph_knowledge_economics://g4p1/v1"

    def build_edge_cost_profile(
        self,
        *,
        edge_ref: str,
        receipt_ref: str,
        corrected_edge_status: str,
        reason_codes: Iterable[str],
        policy_scope: str,
        contradiction_edge: bool,
        federation_depth: int = 0,
    ) -> EdgeCostProfile:
        normalized_scope = str(policy_scope or "lab").strip().lower() or "lab"
        normalized_status = str(corrected_edge_status or "active").strip().lower() or "active"
        normalized_reasons = [
            str(reason or "").strip().lower()
            for reason in reason_codes
            if str(reason or "").strip()
        ]

        extraction_cost = 1
        entity_resolution_cost = 1 + min(2, len(normalized_reasons) // 2)
        curation_minutes = _STATUS_CURATION_MINUTES.get(normalized_status, 6)
        projection_cost = 1
        reverification_cost = 1 + (
            2
            if contradiction_edge or normalized_status in {"superseded", "retracted"}
            else 0
        )
        privacy_review_cost = (
            2
            if normalized_scope in {"global", "org"}
            or any("privacy" in reason or "leak" in reason or "fuga" in reason for reason in normalized_reasons)
            else 0
        )
        federation_cost = max(0, int(federation_depth))
        cost_components = {
            "extraction_cost": extraction_cost,
            "entity_resolution_cost": entity_resolution_cost,
            "curation_minutes": curation_minutes,
            "projection_cost": projection_cost,
            "reverification_cost": reverification_cost,
            "privacy_review_cost": privacy_review_cost,
            "federation_cost": federation_cost,
        }
        total_cost = sum(cost_components.values())
        if total_cost >= 28:
            maintenance_class = "critical"
        elif total_cost >= 18:
            maintenance_class = "high"
        elif total_cost >= 10:
            maintenance_class = "medium"
        else:
            maintenance_class = "low"
        subsidy_ref = (
            "commons_budget://public"
            if normalized_scope in {"global", "org"}
            else "commons_budget://lab"
        )
        payload = {
            "edge_ref": edge_ref,
            "receipt_ref": receipt_ref,
            "subsidy_ref": subsidy_ref,
            "maintenance_class": maintenance_class,
            "cost_components": cost_components,
        }
        return EdgeCostProfile(
            edge_cost_profile_ref=self._stable_ref(
                "edge_cost_profile://graphrag/",
                payload,
            ),
            edge_ref=edge_ref,
            receipt_ref=receipt_ref,
            subsidy_ref=subsidy_ref,
            maintenance_class=maintenance_class,
            total_cost=total_cost,
            cost_components=cost_components,
        )

    def derive_curation_priority(
        self,
        *,
        edge_ref: str,
        maintenance_cost: int,
        scientific_impact: float,
        downstream_usage: int,
        contradiction_pressure: float,
        public_visibility: float,
        retraction_risk: float,
    ) -> GraphCurationPriority:
        bounded_impact = max(0.1, min(1.0, scientific_impact))
        bounded_contradiction = max(0.0, min(1.0, contradiction_pressure))
        bounded_visibility = max(0.0, min(1.0, public_visibility))
        bounded_retraction = max(0.0, min(1.0, retraction_risk))
        usage_multiplier = 1.0 + min(1.5, math.log10(max(1, downstream_usage) + 1))
        numerator = (
            (1.6 * bounded_impact)
            + (1.1 * bounded_contradiction)
            + (0.8 * bounded_visibility)
            + (1.2 * bounded_retraction)
        ) * usage_multiplier
        priority_score = round((numerator * 10.0) / max(1, maintenance_cost), 4)
        if priority_score >= 1.5:
            recommended_action = "curate_now"
        elif priority_score >= 0.8:
            recommended_action = "queue_next_cycle"
        else:
            recommended_action = "defer"
        payload = {
            "edge_ref": edge_ref,
            "priority_score": priority_score,
            "recommended_action": recommended_action,
            "maintenance_cost": maintenance_cost,
        }
        return GraphCurationPriority(
            edge_ref=edge_ref,
            priority_ref=self._stable_ref("curation_priority://graphrag/", payload),
            priority_score=priority_score,
            recommended_action=recommended_action,
            scientific_impact=bounded_impact,
            downstream_usage=max(0, int(downstream_usage)),
            contradiction_pressure=bounded_contradiction,
            public_visibility=bounded_visibility,
            retraction_risk=bounded_retraction,
            maintenance_cost=max(1, int(maintenance_cost)),
        )

    def derive_priority_from_cost_profile(
        self,
        *,
        edge_ref: str,
        policy_scope: str,
        cost_profile: EdgeCostProfile,
        curation_credit_score: int,
        contradiction_edge: bool,
        reason_codes: Iterable[str],
        downstream_usage: int = 0,
    ) -> GraphCurationPriority:
        normalized_scope = str(policy_scope or "lab").strip().lower() or "lab"
        normalized_reasons = [
            str(reason or "").strip().lower()
            for reason in reason_codes
            if str(reason or "").strip()
        ]
        public_visibility = _PUBLIC_VISIBILITY_BY_SCOPE.get(normalized_scope, 0.5)
        contradiction_pressure = 1.0 if contradiction_edge else min(1.0, len(normalized_reasons) / 4.0)
        retraction_risk = 1.0 if any(
            "retract" in reason or "supersed" in reason for reason in normalized_reasons
        ) else contradiction_pressure * 0.75
        scientific_impact = min(1.0, max(0.2, curation_credit_score / 20.0))
        return self.derive_curation_priority(
            edge_ref=edge_ref,
            maintenance_cost=cost_profile.total_cost,
            scientific_impact=scientific_impact,
            downstream_usage=downstream_usage,
            contradiction_pressure=contradiction_pressure,
            public_visibility=public_visibility,
            retraction_risk=retraction_risk,
        )

    def allocate_knowledge_maintenance_budget(
        self,
        *,
        budget_ref: str,
        subsidy_ref: str,
        available_units: int,
        priorities: Iterable[GraphCurationPriority],
    ) -> KnowledgeMaintenanceBudget:
        remaining = max(0, int(available_units))
        allocations: list[KnowledgeMaintenanceAllocation] = []
        uncovered_edge_refs: list[str] = []
        ordered = sorted(
            priorities,
            key=lambda item: (item.priority_score, item.scientific_impact, item.retraction_risk),
            reverse=True,
        )
        for priority in ordered:
            required = max(1, int(priority.maintenance_cost))
            if remaining >= required:
                allocated_units = required
                remaining -= required
            else:
                allocated_units = 0
                uncovered_edge_refs.append(priority.edge_ref)
            allocations.append(
                KnowledgeMaintenanceAllocation(
                    edge_ref=priority.edge_ref,
                    priority_score=priority.priority_score,
                    maintenance_cost=required,
                    allocated_units=allocated_units,
                    recommended_action=priority.recommended_action if allocated_units else "defer",
                )
            )
        allocated_total = sum(item.allocated_units for item in allocations)
        return KnowledgeMaintenanceBudget(
            budget_ref=budget_ref,
            subsidy_ref=subsidy_ref,
            available_units=max(0, int(available_units)),
            allocated_units=allocated_total,
            remaining_units=remaining,
            allocations=tuple(allocations),
            uncovered_edge_refs=tuple(uncovered_edge_refs),
        )

    @staticmethod
    def _stable_ref(prefix: str, payload: dict[str, object]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}"


__all__ = [
    "EdgeCostProfile",
    "GraphCurationPriority",
    "GraphKnowledgeEconomicsRuntime",
    "KnowledgeMaintenanceAllocation",
    "KnowledgeMaintenanceBudget",
]
