from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from mica.graphrag.node2vec_coverage_gate import GraphNode2VecCoverageDecision


@dataclass(frozen=True)
class GraphNode2VecCandidate:
    node_ref: str
    similarity_score: float
    source_seed_refs: tuple[str, ...]
    receipted_path_refs: tuple[str, ...]
    eligible_for_answer: bool
    discovered_in_canonical_subgraph: bool
    blocked_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Node2VecABMetrics:
    grounding_rate: float
    p95_latency_ms: float
    permission_failure_rate: float


@dataclass(frozen=True)
class Node2VecABDecision:
    rollout_status: str
    rollback_triggered: bool
    reason: str
    control_metrics: Node2VecABMetrics
    treatment_metrics: Node2VecABMetrics

    def as_dict(self) -> dict[str, Any]:
        return {
            "rollout_status": self.rollout_status,
            "rollback_triggered": self.rollback_triggered,
            "reason": self.reason,
            "control_metrics": asdict(self.control_metrics),
            "treatment_metrics": asdict(self.treatment_metrics),
        }


@dataclass(frozen=True)
class Node2VecExpansionResult:
    model_ref: str
    status: str
    reason: str
    shadow_mode_only: bool
    coverage_gate_passed: bool
    rollback_triggered: bool
    candidates: tuple[GraphNode2VecCandidate, ...]
    ab_decision: Node2VecABDecision

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_ref": self.model_ref,
            "status": self.status,
            "reason": self.reason,
            "shadow_mode_only": self.shadow_mode_only,
            "coverage_gate_passed": self.coverage_gate_passed,
            "rollback_triggered": self.rollback_triggered,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "ab_decision": self.ab_decision.as_dict(),
        }


class Node2VecABHarness:
    def __init__(
        self,
        *,
        min_grounding_delta: float = -0.05,
        max_latency_regression_ratio: float = 1.25,
        max_permission_failure_increase: float = 0.0,
    ) -> None:
        self._min_grounding_delta = min_grounding_delta
        self._max_latency_regression_ratio = max_latency_regression_ratio
        self._max_permission_failure_increase = max_permission_failure_increase

    def evaluate(
        self,
        *,
        control: Node2VecABMetrics,
        treatment: Node2VecABMetrics,
    ) -> Node2VecABDecision:
        grounding_delta = treatment.grounding_rate - control.grounding_rate
        latency_ratio = (
            treatment.p95_latency_ms / control.p95_latency_ms
            if control.p95_latency_ms > 0
            else 1.0
        )
        permission_delta = treatment.permission_failure_rate - control.permission_failure_rate

        rollback_reasons: list[str] = []
        if grounding_delta < self._min_grounding_delta:
            rollback_reasons.append("grounding_regression")
        if latency_ratio > self._max_latency_regression_ratio:
            rollback_reasons.append("latency_regression")
        if permission_delta > self._max_permission_failure_increase:
            rollback_reasons.append("permission_regression")

        if rollback_reasons:
            return Node2VecABDecision(
                rollout_status="rolled_back",
                rollback_triggered=True,
                reason="|".join(rollback_reasons),
                control_metrics=control,
                treatment_metrics=treatment,
            )
        return Node2VecABDecision(
            rollout_status="experimental_shadow",
            rollback_triggered=False,
            reason="ab_within_guardrails",
            control_metrics=control,
            treatment_metrics=treatment,
        )


class Node2VecRuntime:
    """Candidate expansion only; never evidence authority."""

    def __init__(
        self,
        *,
        coverage_decision: GraphNode2VecCoverageDecision,
        ab_decision: Node2VecABDecision,
        candidate_provider: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]],
        model_ref: str = "mica_graph_node2vec_512_v1",
    ) -> None:
        self._coverage_decision = coverage_decision
        self._ab_decision = ab_decision
        self._candidate_provider = candidate_provider
        self._model_ref = model_ref

    def expand_candidates(
        self,
        *,
        seed_node_refs: Sequence[str],
        traversed_node_refs: Iterable[str] = (),
    ) -> Node2VecExpansionResult:
        traversed_set = set(traversed_node_refs)

        if not self._coverage_decision.coverage_gate_passed:
            return Node2VecExpansionResult(
                model_ref=self._model_ref,
                status="blocked",
                reason="coverage_gate_failed",
                shadow_mode_only=True,
                coverage_gate_passed=False,
                rollback_triggered=False,
                candidates=(),
                ab_decision=self._ab_decision,
            )
        if self._ab_decision.rollback_triggered:
            return Node2VecExpansionResult(
                model_ref=self._model_ref,
                status="rolled_back",
                reason=self._ab_decision.reason,
                shadow_mode_only=True,
                coverage_gate_passed=True,
                rollback_triggered=True,
                candidates=(),
                ab_decision=self._ab_decision,
            )

        candidates: list[GraphNode2VecCandidate] = []
        for raw_candidate in self._candidate_provider(seed_node_refs):
            node_ref = str(raw_candidate.get("node_ref") or "").strip()
            if not node_ref:
                continue
            receipted_path_refs = tuple(
                str(ref).strip()
                for ref in (raw_candidate.get("receipted_path_refs") or [])
                if str(ref).strip()
            )
            eligible_for_answer = bool(receipted_path_refs)
            blocked_reason = None if eligible_for_answer else "node2vec_candidate_requires_receipted_path"
            candidates.append(
                GraphNode2VecCandidate(
                    node_ref=node_ref,
                    similarity_score=float(raw_candidate.get("similarity_score") or 0.0),
                    source_seed_refs=tuple(
                        str(ref).strip()
                        for ref in (raw_candidate.get("source_seed_refs") or tuple(seed_node_refs))
                        if str(ref).strip()
                    ),
                    receipted_path_refs=receipted_path_refs,
                    eligible_for_answer=eligible_for_answer,
                    discovered_in_canonical_subgraph=node_ref in traversed_set,
                    blocked_reason=blocked_reason,
                )
            )

        return Node2VecExpansionResult(
            model_ref=self._model_ref,
            status="experimental_shadow",
            reason="candidate_expansion_only",
            shadow_mode_only=True,
            coverage_gate_passed=True,
            rollback_triggered=False,
            candidates=tuple(candidates),
            ab_decision=self._ab_decision,
        )


__all__ = [
    "GraphNode2VecCandidate",
    "Node2VecABDecision",
    "Node2VecABHarness",
    "Node2VecABMetrics",
    "Node2VecExpansionResult",
    "Node2VecRuntime",
]
