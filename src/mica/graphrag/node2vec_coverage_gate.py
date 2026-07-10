"""Coverage gate and staleness contract for GraphRAG node2vec readiness.

Doctrine anchor:
- `mica_graph_node2vec_512_v1` is not authoritative knowledge
- graph embeddings remain recommendation surfaces
- receipted traversal remains proof
- passing G2.4 does not auto-enable runtime; it only allows shadow promotion
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class GraphNode2VecCoverageThresholds:
    min_active_edge_receipt_coverage_ratio: float = 0.95
    min_active_node_resolvable_ratio: float = 0.98
    min_projection_drift_green_days: int = 14
    max_graph_embedding_staleness_ratio: float = 0.10


@dataclass(frozen=True)
class GraphNode2VecCoverageSnapshot:
    model_ref: str = "mica_graph_node2vec_512_v1"
    current_contract_status: str = "draft_not_runtime"
    active_edge_count: int = 0
    active_edges_with_receipt_count: int = 0
    active_edge_receipt_coverage_ratio: float = 0.0
    active_node_count: Optional[int] = None
    active_node_resolvable_count: Optional[int] = None
    active_node_resolvable_ratio: Optional[float] = None
    projection_drift_green_days: int = 0
    edge_kind_registry_frozen: bool = False
    golden_v2_available: bool = False
    p0_permission_traversal_failures: int = 0
    domain_density_threshold_passed: bool = False
    changed_edges_since_training: int = 0
    training_edges_total: int = 0
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def graph_embedding_staleness_ratio(self) -> float:
        return compute_graph_embedding_staleness(
            changed_edges_since_training=self.changed_edges_since_training,
            training_edges_total=self.training_edges_total,
        )


@dataclass(frozen=True)
class GraphNode2VecCoverageDecision:
    model_ref: str
    current_contract_status: str
    coverage_gate_passed: bool
    runtime_allowed: bool
    graph_embedding_staleness_ratio: float
    blocking_conditions: list[str]
    recommended_next_status: Optional[str]
    recommendation_surface: str = "graph_embedding_suggestion_not_evidence"
    proof_surface: str = "receipted_traversal"


def compute_graph_embedding_staleness(
    *,
    changed_edges_since_training: int,
    training_edges_total: int,
) -> float:
    changed = max(0, int(changed_edges_since_training))
    total = max(0, int(training_edges_total))
    if total == 0:
        return 0.0
    return changed / total


def assess_node2vec_coverage_gate(
    snapshot: GraphNode2VecCoverageSnapshot,
    *,
    thresholds: GraphNode2VecCoverageThresholds = GraphNode2VecCoverageThresholds(),
) -> GraphNode2VecCoverageDecision:
    blocking_conditions: list[str] = []

    if snapshot.active_edge_receipt_coverage_ratio < thresholds.min_active_edge_receipt_coverage_ratio:
        blocking_conditions.append("active_edge_receipt_coverage_ratio_below_threshold")

    if snapshot.active_node_resolvable_ratio is None:
        blocking_conditions.append("active_node_resolvable_ratio_missing")
    elif snapshot.active_node_resolvable_ratio < thresholds.min_active_node_resolvable_ratio:
        blocking_conditions.append("active_node_resolvable_ratio_below_threshold")

    if snapshot.projection_drift_green_days < thresholds.min_projection_drift_green_days:
        blocking_conditions.append("projection_drift_green_window_too_short")

    if not snapshot.edge_kind_registry_frozen:
        blocking_conditions.append("edge_kind_registry_not_frozen_for_model_version")

    if not snapshot.golden_v2_available:
        blocking_conditions.append("graphrag_golden_v2_missing")

    if snapshot.p0_permission_traversal_failures > 0:
        blocking_conditions.append("p0_permission_traversal_failures_present")

    if not snapshot.domain_density_threshold_passed:
        blocking_conditions.append("domain_density_threshold_not_met")

    staleness_ratio = snapshot.graph_embedding_staleness_ratio
    if staleness_ratio > thresholds.max_graph_embedding_staleness_ratio:
        blocking_conditions.append("graph_embedding_staleness_above_threshold")

    coverage_gate_passed = not blocking_conditions
    recommended_next_status = "experimental_shadow" if coverage_gate_passed else None

    return GraphNode2VecCoverageDecision(
        model_ref=snapshot.model_ref,
        current_contract_status=snapshot.current_contract_status,
        coverage_gate_passed=coverage_gate_passed,
        runtime_allowed=False,
        graph_embedding_staleness_ratio=staleness_ratio,
        blocking_conditions=blocking_conditions,
        recommended_next_status=recommended_next_status,
    )


__all__ = [
    "GraphNode2VecCoverageDecision",
    "GraphNode2VecCoverageSnapshot",
    "GraphNode2VecCoverageThresholds",
    "assess_node2vec_coverage_gate",
    "compute_graph_embedding_staleness",
]
