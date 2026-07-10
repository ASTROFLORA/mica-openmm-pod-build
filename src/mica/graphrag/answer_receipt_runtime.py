from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


def _stable_json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GraphAnswerReceipt:
    answer_receipt_ref: str
    query_text: str
    scope_ref: str
    graph_snapshot_manifest_ref: str
    vector_index_manifest_ref: str
    answer_hash: str
    total_hits: int
    replay_mode: str
    replayable: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphAnswerReplayResult:
    replay_status: str
    matched: bool
    reason: str
    answer_hash: str
    expected_answer_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GraphAnswerReceiptRuntime:
    """As-of answer receipts over manifest-bound GraphRAG answers."""

    def build_receipt(
        self,
        *,
        query_text: str,
        scope_ref: str,
        retrieval_contract: Mapping[str, Any],
        hits: list[Mapping[str, Any]],
    ) -> GraphAnswerReceipt:
        graph_snapshot_manifest_ref = str(retrieval_contract["graph_snapshot_manifest_ref"])
        vector_index_manifest_ref = str(retrieval_contract["vector_index_manifest_ref"])
        answer_hash = _stable_json_hash(
            {
                "query_text": query_text,
                "scope_ref": scope_ref,
                "graph_snapshot_manifest_ref": graph_snapshot_manifest_ref,
                "vector_index_manifest_ref": vector_index_manifest_ref,
                "hits": hits,
            }
        )
        answer_receipt_ref = f"receipt://graphrag/answer/{answer_hash[:24]}"
        return GraphAnswerReceipt(
            answer_receipt_ref=answer_receipt_ref,
            query_text=query_text,
            scope_ref=scope_ref,
            graph_snapshot_manifest_ref=graph_snapshot_manifest_ref,
            vector_index_manifest_ref=vector_index_manifest_ref,
            answer_hash=answer_hash,
            total_hits=len(hits),
            replay_mode="manifest_bound_hash_compare",
            replayable=True,
        )

    def replay_as_of(
        self,
        *,
        receipt: GraphAnswerReceipt,
        hits: list[Mapping[str, Any]],
        graph_snapshot_manifest_ref: str,
        vector_index_manifest_ref: str,
    ) -> GraphAnswerReplayResult:
        if graph_snapshot_manifest_ref != receipt.graph_snapshot_manifest_ref:
            return GraphAnswerReplayResult(
                replay_status="manifest_mismatch",
                matched=False,
                reason="graph_snapshot_manifest_mismatch",
                answer_hash="",
                expected_answer_hash=receipt.answer_hash,
            )
        if vector_index_manifest_ref != receipt.vector_index_manifest_ref:
            return GraphAnswerReplayResult(
                replay_status="manifest_mismatch",
                matched=False,
                reason="vector_index_manifest_mismatch",
                answer_hash="",
                expected_answer_hash=receipt.answer_hash,
            )
        answer_hash = _stable_json_hash(
            {
                "query_text": receipt.query_text,
                "scope_ref": receipt.scope_ref,
                "graph_snapshot_manifest_ref": graph_snapshot_manifest_ref,
                "vector_index_manifest_ref": vector_index_manifest_ref,
                "hits": hits,
            }
        )
        matched = answer_hash == receipt.answer_hash
        return GraphAnswerReplayResult(
            replay_status="match" if matched else "answer_mismatch",
            matched=matched,
            reason="replay_match" if matched else "answer_hash_mismatch",
            answer_hash=answer_hash,
            expected_answer_hash=receipt.answer_hash,
        )


__all__ = [
    "GraphAnswerReceipt",
    "GraphAnswerReceiptRuntime",
    "GraphAnswerReplayResult",
]
