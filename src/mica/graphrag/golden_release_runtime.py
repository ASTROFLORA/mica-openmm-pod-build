from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class GoldenGraphRAGv3Thresholds:
    path_grounding_rate_min: float = 1.0
    unsupported_relation_output_max: int = 0
    private_edge_leakage_max: int = 0
    aggregate_leakage_canary_max: int = 0
    asof_replay_success_min: float = 0.99
    p95_interactive_traversal_ms_max: float = 750.0
    multi_hop_hypothesis_label_accuracy_min: float = 0.95
    no_path_honesty_rate_min: float = 1.0


@dataclass(frozen=True)
class GoldenGraphRAGv3Metrics:
    path_grounding_rate: float
    unsupported_relation_output: int
    private_edge_leakage: int
    aggregate_leakage_canary: int
    asof_replay_success: float
    p95_interactive_traversal_ms: float
    multi_hop_hypothesis_label_accuracy: float
    no_path_honesty_rate: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoldenGraphRAGv3Verdict:
    release_gate_ref: str
    release_status: str
    failed_gates: tuple[str, ...]
    metrics: GoldenGraphRAGv3Metrics
    thresholds: GoldenGraphRAGv3Thresholds

    def as_dict(self) -> dict[str, Any]:
        return {
            "release_gate_ref": self.release_gate_ref,
            "release_status": self.release_status,
            "failed_gates": list(self.failed_gates),
            "metrics": self.metrics.as_dict(),
            "thresholds": asdict(self.thresholds),
        }


class GoldenGraphRAGv3Runtime:
    """Hard release gate for GraphRAG production readiness."""

    def __init__(self, *, thresholds: GoldenGraphRAGv3Thresholds | None = None) -> None:
        self._thresholds = thresholds or GoldenGraphRAGv3Thresholds()

    def evaluate(
        self,
        *,
        metrics: GoldenGraphRAGv3Metrics,
        evidence_refs: Sequence[str] = (),
    ) -> GoldenGraphRAGv3Verdict:
        failed: list[str] = []
        t = self._thresholds
        if metrics.path_grounding_rate < t.path_grounding_rate_min:
            failed.append("path_grounding_rate")
        if metrics.unsupported_relation_output > t.unsupported_relation_output_max:
            failed.append("unsupported_relation_output")
        if metrics.private_edge_leakage > t.private_edge_leakage_max:
            failed.append("private_edge_leakage")
        if metrics.aggregate_leakage_canary > t.aggregate_leakage_canary_max:
            failed.append("aggregate_leakage_canary")
        if metrics.asof_replay_success < t.asof_replay_success_min:
            failed.append("asof_replay_success")
        if metrics.p95_interactive_traversal_ms >= t.p95_interactive_traversal_ms_max:
            failed.append("p95_interactive_traversal_ms")
        if metrics.multi_hop_hypothesis_label_accuracy < t.multi_hop_hypothesis_label_accuracy_min:
            failed.append("multi_hop_hypothesis_label_accuracy")
        if metrics.no_path_honesty_rate < t.no_path_honesty_rate_min:
            failed.append("no_path_honesty_rate")

        release_gate_ref = self._stable_ref(
            {
                "metrics": metrics.as_dict(),
                "thresholds": asdict(t),
                "evidence_refs": list(evidence_refs),
            }
        )
        return GoldenGraphRAGv3Verdict(
            release_gate_ref=release_gate_ref,
            release_status="pass" if not failed else "blocked",
            failed_gates=tuple(failed),
            metrics=metrics,
            thresholds=t,
        )

    def derive_metrics(
        self,
        *,
        query_outputs: Sequence[Mapping[str, Any]] = (),
        leakage_canaries: Sequence[Mapping[str, Any]] = (),
        replay_results: Sequence[Mapping[str, Any]] = (),
        traversal_latencies_ms: Sequence[float] = (),
        hypothesis_labels: Sequence[Mapping[str, Any]] = (),
        fallback_metrics: Optional[Mapping[str, Any]] = None,
    ) -> GoldenGraphRAGv3Metrics:
        fallback = dict(fallback_metrics or {})
        path_grounding_rate = float(
            fallback.get(
                "path_grounding_rate",
                self._derive_path_grounding_rate(query_outputs),
            )
        )
        unsupported_relation_output = int(
            fallback.get(
                "unsupported_relation_output",
                self._derive_unsupported_relation_output(query_outputs),
            )
        )
        private_edge_leakage = int(
            fallback.get(
                "private_edge_leakage",
                self._derive_private_edge_leakage(query_outputs),
            )
        )
        aggregate_leakage_canary = int(
            fallback.get(
                "aggregate_leakage_canary",
                self._count_failed_canaries(leakage_canaries),
            )
        )
        asof_replay_success = float(
            fallback.get(
                "asof_replay_success",
                self._derive_replay_success(replay_results),
            )
        )
        p95_interactive_traversal_ms = float(
            fallback.get(
                "p95_interactive_traversal_ms",
                self._derive_p95(traversal_latencies_ms),
            )
        )
        multi_hop_hypothesis_label_accuracy = float(
            fallback.get(
                "multi_hop_hypothesis_label_accuracy",
                self._derive_hypothesis_accuracy(hypothesis_labels),
            )
        )
        no_path_honesty_rate = float(
            fallback.get(
                "no_path_honesty_rate",
                self._derive_no_path_honesty_rate(query_outputs),
            )
        )
        return GoldenGraphRAGv3Metrics(
            path_grounding_rate=path_grounding_rate,
            unsupported_relation_output=unsupported_relation_output,
            private_edge_leakage=private_edge_leakage,
            aggregate_leakage_canary=aggregate_leakage_canary,
            asof_replay_success=asof_replay_success,
            p95_interactive_traversal_ms=p95_interactive_traversal_ms,
            multi_hop_hypothesis_label_accuracy=multi_hop_hypothesis_label_accuracy,
            no_path_honesty_rate=no_path_honesty_rate,
        )

    @staticmethod
    def _stable_ref(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"release_gate://graphrag/golden-v3/{digest}"

    @staticmethod
    def _derive_path_grounding_rate(query_outputs: Sequence[Mapping[str, Any]]) -> float:
        relation_items = 0
        grounded_items = 0
        for item in query_outputs:
            claim_contract = item.get("claim_contract")
            if not isinstance(claim_contract, Mapping):
                continue
            if claim_contract.get("claim_kind") != "relation":
                continue
            relation_items += 1
            if bool(claim_contract.get("relation_claim_allowed")) and str(claim_contract.get("path_ref") or "").strip():
                grounded_items += 1
        if relation_items == 0:
            return 1.0
        return grounded_items / relation_items

    @staticmethod
    def _derive_unsupported_relation_output(query_outputs: Sequence[Mapping[str, Any]]) -> int:
        count = 0
        for item in query_outputs:
            claim_contract = item.get("claim_contract")
            if not isinstance(claim_contract, Mapping):
                continue
            if claim_contract.get("claim_kind") != "relation":
                continue
            if not bool(claim_contract.get("relation_claim_allowed")) and item.get("result_type") != "edge_blocked":
                count += 1
        return count

    @staticmethod
    def _derive_private_edge_leakage(query_outputs: Sequence[Mapping[str, Any]]) -> int:
        leaks = 0
        for item in query_outputs:
            if not bool(item.get("private_scope")):
                continue
            claim_contract = item.get("claim_contract")
            if not isinstance(claim_contract, Mapping):
                continue
            if claim_contract.get("claim_kind") == "relation" and bool(claim_contract.get("relation_claim_allowed")):
                leaks += 1
        return leaks

    @staticmethod
    def _count_failed_canaries(leakage_canaries: Sequence[Mapping[str, Any]]) -> int:
        return sum(1 for entry in leakage_canaries if str(entry.get("status") or "").strip().lower() == "fail")

    @staticmethod
    def _derive_replay_success(replay_results: Sequence[Mapping[str, Any]]) -> float:
        if not replay_results:
            return 1.0
        matched = sum(1 for entry in replay_results if bool(entry.get("matched")))
        return matched / len(replay_results)

    @staticmethod
    def _derive_p95(samples: Sequence[float]) -> float:
        if not samples:
            return 0.0
        ordered = sorted(float(value) for value in samples)
        index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
        return ordered[index]

    @staticmethod
    def _derive_hypothesis_accuracy(hypothesis_labels: Sequence[Mapping[str, Any]]) -> float:
        if not hypothesis_labels:
            return 1.0
        correct = 0
        total = 0
        for entry in hypothesis_labels:
            predicted = str(entry.get("predicted_label") or "").strip()
            expected = str(entry.get("expected_label") or "").strip()
            if not predicted or not expected:
                continue
            total += 1
            if predicted == expected:
                correct += 1
        if total == 0:
            return 1.0
        return correct / total

    @staticmethod
    def _derive_no_path_honesty_rate(query_outputs: Sequence[Mapping[str, Any]]) -> float:
        blocked_without_path = 0
        honest_blocked = 0
        for item in query_outputs:
            claim_contract = item.get("claim_contract")
            if not isinstance(claim_contract, Mapping):
                continue
            if claim_contract.get("claim_kind") != "relation":
                continue
            if bool(claim_contract.get("relation_claim_allowed")):
                continue
            blocked_without_path += 1
            if str(claim_contract.get("blocked_reason") or "") == "relation_requires_path_ref" and str(
                claim_contract.get("no_path_disclaimer") or ""
            ).strip():
                honest_blocked += 1
        if blocked_without_path == 0:
            return 1.0
        return honest_blocked / blocked_without_path


__all__ = [
    "GoldenGraphRAGv3Metrics",
    "GoldenGraphRAGv3Runtime",
    "GoldenGraphRAGv3Thresholds",
    "GoldenGraphRAGv3Verdict",
]
