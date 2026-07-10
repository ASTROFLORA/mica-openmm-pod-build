from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from mica.graphrag.evidence_path_runtime import EvidencePathBundleComposer
from mica.infrastructure.persistence.timescale_graphrag_store import GraphEdgeRow


@dataclass(frozen=True)
class GraphInferenceProposal:
    graph_inference_ref: str
    source_path_refs: tuple[str, ...]
    source_receipt_refs: tuple[str, ...]
    inferred_relation: dict[str, str]
    inference_kind: str
    confidence: float
    max_status: str
    materialization_policy: str
    created_by_receipt_ref: str
    reasoning_trace: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GraphInferenceProposalEngine:
    """Derive bounded multi-hop hypotheses from already receipted edge paths."""

    def __init__(self, composer: EvidencePathBundleComposer | None = None) -> None:
        self._composer = composer or EvidencePathBundleComposer()

    @staticmethod
    def _edge_payload(edge: GraphEdgeRow) -> Mapping[str, Any]:
        return {
            "source_node": edge.source_node,
            "source_type": edge.source_type,
            "relationship": edge.relationship,
            "target_node": edge.target_node,
            "target_type": edge.target_type,
            "details": edge.details,
            "confidence": edge.confidence,
            "source_doi": edge.source_doi,
            "source_sentence": edge.source_sentence,
            "extraction_method": edge.extraction_method,
            "metadata": edge.metadata or {},
        }

    @staticmethod
    def _stable_ref(prefix: str, payload: Mapping[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}{digest}"

    def propose_from_edges(
        self,
        *,
        edges: Sequence[GraphEdgeRow],
        max_proposals: int = 10,
        inferred_predicate_ref: str = "biolink:related_to",
    ) -> list[GraphInferenceProposal]:
        if max_proposals <= 0:
            return []

        direct_pairs = {
            (
                str(edge.source_node).strip(),
                str(edge.target_node).strip(),
            )
            for edge in edges
        }

        proposals: list[GraphInferenceProposal] = []
        seen: set[tuple[str, str, str]] = set()
        hydrated: list[tuple[GraphEdgeRow, Any]] = []
        for edge in edges:
            bundle = self._composer.compose_for_edge(self._edge_payload(edge))
            if bundle is None:
                continue
            hydrated.append((edge, bundle))

        for left, left_bundle in hydrated:
            for right, right_bundle in hydrated:
                if left.target_node != right.source_node:
                    continue
                if left.source_node == right.target_node:
                    continue
                if (left.source_node, right.target_node) in direct_pairs:
                    continue

                proposal_key = (
                    str(left.source_node).strip(),
                    str(right.target_node).strip(),
                    inferred_predicate_ref,
                )
                if proposal_key in seen:
                    continue
                seen.add(proposal_key)

                confidence_floor = min(
                    float(left.confidence or 0.0),
                    float(right.confidence or 0.0),
                )
                confidence = round(max(0.0, min(1.0, confidence_floor * 0.5)), 4)
                payload = {
                    "source_path_refs": sorted({left_bundle.path_ref, right_bundle.path_ref}),
                    "inferred_relation": {
                        "source_ref": f"entity://{left.source_node}",
                        "target_ref": f"entity://{right.target_node}",
                        "predicate_ref": inferred_predicate_ref,
                    },
                    "inference_kind": "multi_hop_path",
                    "confidence": confidence,
                }
                graph_inference_ref = self._stable_ref("graph_inference://", payload)
                created_by_receipt_ref = self._stable_ref("receipt://graphrag/inference-proposal/", payload)
                proposals.append(
                    GraphInferenceProposal(
                        graph_inference_ref=graph_inference_ref,
                        source_path_refs=tuple(payload["source_path_refs"]),
                        source_receipt_refs=tuple(
                            sorted({*left_bundle.receipt_refs, *right_bundle.receipt_refs})
                        ),
                        inferred_relation=payload["inferred_relation"],
                        inference_kind="multi_hop_path",
                        confidence=confidence,
                        max_status="hypothesis",
                        materialization_policy="never_active_edge_without_receipt",
                        created_by_receipt_ref=created_by_receipt_ref,
                        reasoning_trace={
                            "intermediate_node_ref": f"entity://{left.target_node}",
                            "support_edge_refs": sorted({*left_bundle.edge_refs, *right_bundle.edge_refs}),
                            "support_path_refs": list(payload["source_path_refs"]),
                        },
                    )
                )
                if len(proposals) >= max_proposals:
                    return proposals

        return proposals


__all__ = [
    "GraphInferenceProposal",
    "GraphInferenceProposalEngine",
]
