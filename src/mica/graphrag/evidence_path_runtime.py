from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class EvidencePathBundle:
    path_ref: str
    bundle_ref: str
    edge_refs: list[str]
    receipt_refs: list[str]
    node_refs: list[str]
    path_kind: str = "graph_edge_direct_v1"


class EvidencePathBundleComposer:
    """Build a minimal evidence-path envelope for GraphRAG edge answers.

    Fail-closed rule:
    - A relation answer is authoritative only if the edge carries at least one
      durable anchor (`edge_ref` or `receipt_ref(s)`).
    - Raw textual fields alone are not enough to mint a relation path.
    """

    def compose_for_edge(self, edge_payload: Mapping[str, Any]) -> Optional[EvidencePathBundle]:
        metadata_raw = edge_payload.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, Mapping) else {}

        edge_refs: list[str] = []
        receipt_refs: list[str] = []

        edge_ref = metadata.get("edge_ref") or metadata.get("graph_edge_ref")
        if isinstance(edge_ref, str) and edge_ref.strip():
            edge_refs.append(edge_ref.strip())

        receipt_ref = metadata.get("receipt_ref")
        if isinstance(receipt_ref, str) and receipt_ref.strip():
            receipt_refs.append(receipt_ref.strip())

        receipt_refs_raw = metadata.get("receipt_refs")
        if isinstance(receipt_refs_raw, list):
            for ref in receipt_refs_raw:
                if isinstance(ref, str) and ref.strip():
                    receipt_refs.append(ref.strip())

        if not edge_refs and not receipt_refs:
            return None

        anchors = sorted(set(edge_refs + receipt_refs))
        node_refs = [
            f"node://graphrag/{str(edge_payload.get('source_node') or '').strip()}",
            f"node://graphrag/{str(edge_payload.get('target_node') or '').strip()}",
        ]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "anchors": anchors,
                    "relationship": edge_payload.get("relationship"),
                    "source_node": edge_payload.get("source_node"),
                    "target_node": edge_payload.get("target_node"),
                },
                sort_keys=True,
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        path_ref = f"path://graphrag/{digest}"
        bundle_ref = f"evidence_path_bundle://graphrag/{digest}"
        return EvidencePathBundle(
            path_ref=path_ref,
            bundle_ref=bundle_ref,
            edge_refs=sorted(set(edge_refs)),
            receipt_refs=sorted(set(receipt_refs)),
            node_refs=node_refs,
        )


class OutputValidator:
    """Enforce `no path, no claim` on GraphRAG query output."""

    def __init__(self, composer: EvidencePathBundleComposer | None = None) -> None:
        self._composer = composer or EvidencePathBundleComposer()

    def validate_item(self, item: Mapping[str, Any]) -> dict[str, Any]:
        validated = dict(item)

        edge_payload = item.get("edge")
        if isinstance(edge_payload, Mapping):
            bundle = self._composer.compose_for_edge(edge_payload=edge_payload)
            if bundle is None:
                validated["result_type"] = "edge_blocked"
                validated["claim_contract"] = {
                    "claim_kind": "relation",
                    "relation_claim_allowed": False,
                    "blocked_reason": "relation_requires_path_ref",
                    "no_path_disclaimer": (
                        "Graph relation claim blocked because no authoritative "
                        "path_ref was available."
                    ),
                    "rewritten": True,
                }
                return validated

            validated["evidence_path"] = asdict(bundle)
            validated["claim_contract"] = {
                "claim_kind": "relation",
                "relation_claim_allowed": True,
                "path_ref": bundle.path_ref,
                "bundle_ref": bundle.bundle_ref,
                "rewritten": False,
            }
            return validated

        fact_payload = item.get("fact")
        if isinstance(fact_payload, Mapping):
            validated["claim_contract"] = {
                "claim_kind": "fact_excerpt",
                "relation_claim_allowed": False,
                "no_path_disclaimer": (
                    "No graph path_ref was available; this result is returned "
                    "as a fact excerpt only, not as a relation claim."
                ),
                "rewritten": False,
            }
            return validated

        validated["claim_contract"] = {
            "claim_kind": "unknown",
            "relation_claim_allowed": False,
            "no_path_disclaimer": "Unsupported GraphRAG output shape.",
            "rewritten": True,
        }
        return validated
