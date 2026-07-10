from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from mica.graphrag.golden_release_runtime import GoldenGraphRAGv3Runtime
from mica.graphrag.node2vec_runtime import (
    Node2VecABDecision,
    Node2VecABHarness,
    Node2VecABMetrics,
)

_ALLOWED_SIGNAL_KINDS = {
    "path_cited",
    "path_accepted_by_curator",
    "path_rejected",
    "inference_validated",
    "inference_refuted",
}

_MAY_AFFECT = (
    "candidate_expansion",
    "curation_priority",
    "ranking",
)

_MAY_NOT_AFFECT = (
    "claim_tier",
    "edge_activation",
    "evidence_strength",
)

_RETRAINING_STAGES = (
    "freeze_snapshot",
    "train_candidate",
    "evaluate_golden",
    "privacy_canary",
    "shadow_deploy",
    "ab_vs_traversal_only",
    "rollback_ready",
)


@dataclass(frozen=True)
class GraphUsageSignal:
    graph_usage_signal_ref: str
    signal_kind: str
    path_ref: str
    edge_refs: tuple[str, ...]
    actor_ref: str
    receipt_ref: str
    scope_ref: str
    may_affect: tuple[str, ...]
    may_not_affect: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphUsageSignalDecision:
    status: str
    signal: GraphUsageSignal
    allowed_effects: tuple[str, ...]
    blocked_effects: tuple[str, ...]
    blocked_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "signal": self.signal.as_dict(),
            "allowed_effects": list(self.allowed_effects),
            "blocked_effects": list(self.blocked_effects),
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class GovernedRetrainingPlan:
    retraining_plan_ref: str
    candidate_model_ref: str
    frozen_graph_snapshot_ref: str
    vector_snapshot_ref: str | None
    shadow_mode_only: bool
    stages: tuple[str, ...]
    allowed_effects: tuple[str, ...]
    forbidden_effects: tuple[str, ...]
    status: str
    blocked_reasons: tuple[str, ...]
    golden_release_status: str
    golden_failed_gates: tuple[str, ...]
    ab_decision: Node2VecABDecision
    rollback_ready: bool
    usage_signal_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "retraining_plan_ref": self.retraining_plan_ref,
            "candidate_model_ref": self.candidate_model_ref,
            "frozen_graph_snapshot_ref": self.frozen_graph_snapshot_ref,
            "vector_snapshot_ref": self.vector_snapshot_ref,
            "shadow_mode_only": self.shadow_mode_only,
            "stages": list(self.stages),
            "allowed_effects": list(self.allowed_effects),
            "forbidden_effects": list(self.forbidden_effects),
            "status": self.status,
            "blocked_reasons": list(self.blocked_reasons),
            "golden_release_status": self.golden_release_status,
            "golden_failed_gates": list(self.golden_failed_gates),
            "ab_decision": self.ab_decision.as_dict(),
            "rollback_ready": self.rollback_ready,
            "usage_signal_refs": list(self.usage_signal_refs),
        }


class AntiConfirmationGuard:
    """Ensure usage cannot contaminate graph authority."""

    def review_effects(self, proposed_effects: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        normalized = tuple(
            sorted(
                {
                    str(effect or "").strip()
                    for effect in proposed_effects
                    if str(effect or "").strip()
                }
            )
        )
        allowed = tuple(effect for effect in normalized if effect in _MAY_AFFECT)
        blocked = tuple(effect for effect in normalized if effect in _MAY_NOT_AFFECT or effect not in _MAY_AFFECT)
        reasons: list[str] = []
        for effect in blocked:
            if effect in _MAY_NOT_AFFECT:
                reasons.append(f"{effect}_forbidden_for_usage_signal")
            else:
                reasons.append(f"{effect}_unsupported_for_usage_signal")
        return allowed, blocked, tuple(reasons)

    def review_retraining_inputs(
        self,
        *,
        frozen_graph_snapshot_ref: str,
        includes_unvalidated_inferred_edges: bool,
        uses_usage_popularity_as_evidence: bool,
        golden_release_status: str,
        golden_failed_gates: Sequence[str],
        ab_decision: Node2VecABDecision,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not str(frozen_graph_snapshot_ref or "").strip():
            reasons.append("frozen_graph_snapshot_required")
        if includes_unvalidated_inferred_edges:
            reasons.append("training_on_unvalidated_inferred_edges_forbidden")
        if uses_usage_popularity_as_evidence:
            reasons.append("usage_popularity_cannot_raise_evidence")
        if str(golden_release_status or "").strip().lower() != "pass":
            reasons.extend(
                f"golden_gate_blocked:{gate}"
                for gate in golden_failed_gates
            )
        if ab_decision.rollback_triggered:
            reasons.append(f"ab_guardrails_failed:{ab_decision.reason}")
        return tuple(reasons)


class GovernedLearningRuntime:
    """G4.6 governed learning over existing GraphRAG evaluation seams."""

    policy_ref = "graph_governed_learning://g4p6/v1"

    def __init__(
        self,
        *,
        golden_release_runtime: GoldenGraphRAGv3Runtime | None = None,
        ab_harness: Node2VecABHarness | None = None,
        anti_confirmation_guard: AntiConfirmationGuard | None = None,
    ) -> None:
        self._golden_release_runtime = golden_release_runtime or GoldenGraphRAGv3Runtime()
        self._ab_harness = ab_harness or Node2VecABHarness()
        self._anti_confirmation_guard = anti_confirmation_guard or AntiConfirmationGuard()

    def review_usage_signal(
        self,
        *,
        signal_kind: str,
        path_ref: str,
        edge_refs: Sequence[str],
        actor_ref: str,
        receipt_ref: str,
        scope_ref: str,
        proposed_effects: Sequence[str],
    ) -> GraphUsageSignalDecision:
        normalized_kind = str(signal_kind or "").strip()
        if normalized_kind not in _ALLOWED_SIGNAL_KINDS:
            raise ValueError("unsupported_usage_signal_kind")
        normalized_path_ref = str(path_ref or "").strip()
        normalized_receipt_ref = str(receipt_ref or "").strip()
        normalized_actor_ref = str(actor_ref or "").strip()
        if not normalized_path_ref:
            raise ValueError("path_ref_required")
        if not normalized_receipt_ref:
            raise ValueError("receipt_ref_required")
        if not normalized_actor_ref:
            raise ValueError("actor_ref_required")

        signal = GraphUsageSignal(
            graph_usage_signal_ref=self._stable_ref(
                "graph_usage_signal://graphrag/",
                {
                    "signal_kind": normalized_kind,
                    "path_ref": normalized_path_ref,
                    "edge_refs": list(edge_refs),
                    "actor_ref": normalized_actor_ref,
                    "receipt_ref": normalized_receipt_ref,
                    "scope_ref": scope_ref,
                },
            ),
            signal_kind=normalized_kind,
            path_ref=normalized_path_ref,
            edge_refs=tuple(str(ref).strip() for ref in edge_refs if str(ref).strip()),
            actor_ref=normalized_actor_ref,
            receipt_ref=normalized_receipt_ref,
            scope_ref=str(scope_ref or "scope://unscoped").strip() or "scope://unscoped",
            may_affect=_MAY_AFFECT,
            may_not_affect=_MAY_NOT_AFFECT,
        )
        allowed_effects, blocked_effects, blocked_reasons = self._anti_confirmation_guard.review_effects(proposed_effects)
        return GraphUsageSignalDecision(
            status="accepted" if not blocked_effects else "blocked",
            signal=signal,
            allowed_effects=allowed_effects,
            blocked_effects=blocked_effects,
            blocked_reasons=blocked_reasons,
        )

    def plan_node2vec_retraining(
        self,
        *,
        frozen_graph_snapshot_ref: str,
        candidate_model_ref: str,
        control_metrics: Node2VecABMetrics,
        treatment_metrics: Node2VecABMetrics,
        query_outputs: Sequence[Mapping[str, Any]] = (),
        leakage_canaries: Sequence[Mapping[str, Any]] = (),
        replay_results: Sequence[Mapping[str, Any]] = (),
        traversal_latencies_ms: Sequence[float] = (),
        hypothesis_labels: Sequence[Mapping[str, Any]] = (),
        fallback_metrics: Mapping[str, Any] | None = None,
        includes_unvalidated_inferred_edges: bool = False,
        uses_usage_popularity_as_evidence: bool = False,
        usage_signal_refs: Sequence[str] = (),
        vector_snapshot_ref: str | None = None,
    ) -> GovernedRetrainingPlan:
        metrics = self._golden_release_runtime.derive_metrics(
            query_outputs=query_outputs,
            leakage_canaries=leakage_canaries,
            replay_results=replay_results,
            traversal_latencies_ms=traversal_latencies_ms,
            hypothesis_labels=hypothesis_labels,
            fallback_metrics=fallback_metrics,
        )
        golden_verdict = self._golden_release_runtime.evaluate(metrics=metrics, evidence_refs=usage_signal_refs)
        ab_decision = self._ab_harness.evaluate(control=control_metrics, treatment=treatment_metrics)
        blocked_reasons = self._anti_confirmation_guard.review_retraining_inputs(
            frozen_graph_snapshot_ref=frozen_graph_snapshot_ref,
            includes_unvalidated_inferred_edges=includes_unvalidated_inferred_edges,
            uses_usage_popularity_as_evidence=uses_usage_popularity_as_evidence,
            golden_release_status=golden_verdict.release_status,
            golden_failed_gates=golden_verdict.failed_gates,
            ab_decision=ab_decision,
        )
        status = "shadow_ready" if not blocked_reasons else "blocked"
        payload = {
            "candidate_model_ref": candidate_model_ref,
            "frozen_graph_snapshot_ref": frozen_graph_snapshot_ref,
            "vector_snapshot_ref": vector_snapshot_ref,
            "usage_signal_refs": list(usage_signal_refs),
            "status": status,
            "blocked_reasons": list(blocked_reasons),
            "golden_release_status": golden_verdict.release_status,
            "golden_failed_gates": list(golden_verdict.failed_gates),
            "ab_reason": ab_decision.reason,
        }
        return GovernedRetrainingPlan(
            retraining_plan_ref=self._stable_ref("governed_retraining://graphrag/", payload),
            candidate_model_ref=str(candidate_model_ref or "mica_graph_node2vec_candidate").strip() or "mica_graph_node2vec_candidate",
            frozen_graph_snapshot_ref=str(frozen_graph_snapshot_ref or "").strip(),
            vector_snapshot_ref=str(vector_snapshot_ref).strip() if vector_snapshot_ref is not None else None,
            shadow_mode_only=True,
            stages=_RETRAINING_STAGES,
            allowed_effects=_MAY_AFFECT,
            forbidden_effects=_MAY_NOT_AFFECT,
            status=status,
            blocked_reasons=blocked_reasons,
            golden_release_status=golden_verdict.release_status,
            golden_failed_gates=golden_verdict.failed_gates,
            ab_decision=ab_decision,
            rollback_ready=True,
            usage_signal_refs=tuple(str(ref).strip() for ref in usage_signal_refs if str(ref).strip()),
        )

    @staticmethod
    def _stable_ref(prefix: str, payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}"


__all__ = [
    "AntiConfirmationGuard",
    "GovernedLearningRuntime",
    "GovernedRetrainingPlan",
    "GraphUsageSignal",
    "GraphUsageSignalDecision",
]
