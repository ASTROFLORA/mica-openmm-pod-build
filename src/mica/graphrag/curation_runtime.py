from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from mica.kb.graph.economics import GraphKnowledgeEconomicsRuntime


_ALLOWED_CURATOR_ROLES = {"curator", "senior_curator"}
_ALLOWED_EDGE_STATUSES = {"review_required", "deprecated", "retracted", "superseded", "active"}
_CURATION_IMPACT_WEIGHTS = {
    "retracted": 8,
    "superseded": 6,
    "deprecated": 4,
    "review_required": 3,
    "active": 1,
    "contradiction": 5,
    "retraction": 4,
    "leakage": 7,
    "fugue": 7,
    "fuga": 7,
    "privacy": 6,
}


@dataclass(frozen=True)
class GraphCurationTask:
    source_node: str
    source_type: str
    target_node: str
    target_type: str
    relationship: str
    corrected_edge_status: str
    actor_ref: str
    actor_role: str
    source_receipt_ref: str
    reason_codes: tuple[str, ...] = ()
    details: Optional[str] = None
    confidence: float = 1.0
    source_doi: Optional[str] = None
    source_sentence: Optional[str] = None
    extraction_method: str = "graph_curation"
    policy_scope: str = "lab"
    semantic_context_ref: Optional[str] = None
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    contradiction_claim_ref: Optional[str] = None
    contradiction_kind: Optional[str] = None
    supporting_edge_refs: tuple[str, ...] = ()
    contradicting_edge_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CurationCredit:
    score: int
    impact_class: str
    weighted_reasons: dict[str, int]


@dataclass(frozen=True)
class EdgeCorrectionReceipt:
    receipt_ref: str
    task_ref: str
    edge_ref: str
    source_receipt_ref: str
    corrected_edge_status: str
    actor_ref: str
    actor_role: str
    reason_codes: tuple[str, ...]
    neo4j_projection_action: str
    contradiction_ref: Optional[str] = None


@dataclass(frozen=True)
class GraphCurationResult:
    receipt: EdgeCorrectionReceipt
    credit: CurationCredit
    edge_metadata: dict[str, Any]


class GraphCurationRuntime:
    """Canonical GraphRAG curation over the Postgres authority.

    GP9 explicitly forbids direct Neo4j mutation. This runtime writes the
    correction onto the canonical edge record and leaves projection rebuild to
    the projector lane.
    """

    def plan_credit(
        self,
        *,
        corrected_edge_status: str,
        reason_codes: Iterable[str],
        contradiction_edge: bool,
    ) -> CurationCredit:
        normalized_reasons = [str(reason or "").strip().lower() for reason in reason_codes if str(reason or "").strip()]
        weighted_reasons: dict[str, int] = {}
        score = _CURATION_IMPACT_WEIGHTS.get(corrected_edge_status, 0)
        for reason in normalized_reasons:
            for key, weight in _CURATION_IMPACT_WEIGHTS.items():
                if key in reason:
                    weighted_reasons[reason] = max(weighted_reasons.get(reason, 0), weight)
            score += weighted_reasons.get(reason, 0)
        if contradiction_edge:
            score += _CURATION_IMPACT_WEIGHTS["contradiction"]
        if score >= 15:
            impact_class = "critical"
        elif score >= 9:
            impact_class = "high"
        elif score >= 4:
            impact_class = "medium"
        else:
            impact_class = "low"
        return CurationCredit(score=score, impact_class=impact_class, weighted_reasons=weighted_reasons)

    async def curate_edge(
        self,
        *,
        store: Any,
        task: GraphCurationTask,
        user_id: Optional[str],
        session_id: Optional[str] = None,
    ) -> GraphCurationResult:
        actor_role = task.actor_role.strip().lower()
        if actor_role not in _ALLOWED_CURATOR_ROLES:
            raise ValueError("invalid_actor_role")
        if task.corrected_edge_status not in _ALLOWED_EDGE_STATUSES:
            raise ValueError("invalid_corrected_edge_status")
        if not task.source_receipt_ref.startswith("receipt://"):
            raise ValueError("invalid_source_receipt_ref")

        contradiction_edge = bool(task.contradiction_claim_ref and task.contradiction_kind)
        credit = self.plan_credit(
            corrected_edge_status=task.corrected_edge_status,
            reason_codes=task.reason_codes,
            contradiction_edge=contradiction_edge,
        )
        economics = GraphKnowledgeEconomicsRuntime()

        identity_payload = {
            "source_node": task.source_node,
            "source_type": task.source_type,
            "target_node": task.target_node,
            "target_type": task.target_type,
            "relationship": task.relationship,
            "policy_scope": task.policy_scope,
            "study_id": task.study_id,
            "kb_id": task.kb_id,
            "working_set_id": task.working_set_id,
        }
        edge_ref = self._stable_ref("edge://graphrag/curation-target/", identity_payload)
        task_ref = self._stable_ref("task://graphrag/curation/", {"identity": identity_payload, "actor_ref": task.actor_ref})
        receipt_ref = self._stable_ref(
            "receipt://graphrag/curation/",
            {
                "identity": identity_payload,
                "task_ref": task_ref,
                "source_receipt_ref": task.source_receipt_ref,
                "corrected_edge_status": task.corrected_edge_status,
                "reason_codes": list(task.reason_codes),
                "actor_ref": task.actor_ref,
                "actor_role": actor_role,
                "credit": asdict(credit),
            },
        )
        cost_profile = economics.build_edge_cost_profile(
            edge_ref=edge_ref,
            receipt_ref=receipt_ref,
            corrected_edge_status=task.corrected_edge_status,
            reason_codes=task.reason_codes,
            policy_scope=task.policy_scope,
            contradiction_edge=contradiction_edge,
            federation_depth=1 if (task.semantic_context_ref or "").startswith("external_asserted://") else 0,
        )
        maintenance_priority = economics.derive_priority_from_cost_profile(
            edge_ref=edge_ref,
            policy_scope=task.policy_scope,
            cost_profile=cost_profile,
            curation_credit_score=credit.score,
            contradiction_edge=contradiction_edge,
            reason_codes=task.reason_codes,
            downstream_usage=0,
        )

        edge_metadata: dict[str, Any] = {
            "created_by_receipt_ref": receipt_ref,
            "source_receipt_ref": task.source_receipt_ref,
            "semantic_context_ref": task.semantic_context_ref,
            "policy_scope": task.policy_scope,
            "study_id": task.study_id,
            "kb_id": task.kb_id,
            "working_set_id": task.working_set_id,
            "graph_write_mode": "canonical_receipt_only",
            "neo4j_projection_action": "rebuild_required",
            "edge_status": task.corrected_edge_status,
            "curation_task_ref": task_ref,
            "curation_actor_ref": task.actor_ref,
            "curation_actor_role": actor_role,
            "curation_reason_codes": list(task.reason_codes),
            "curation_credit": asdict(credit),
            "curation_edge_ref": edge_ref,
            "edge_cost_profile": asdict(cost_profile),
            "knowledge_maintenance_priority": asdict(maintenance_priority),
        }
        edge_metadata = {k: v for k, v in edge_metadata.items() if v is not None}

        await store.upsert_edge(
            source_node=task.source_node,
            source_type=task.source_type,
            target_node=task.target_node,
            target_type=task.target_type,
            relationship=task.relationship,
            details=task.details,
            confidence=task.confidence,
            source_doi=task.source_doi,
            source_sentence=task.source_sentence,
            extraction_method=task.extraction_method,
            metadata=edge_metadata,
            user_id=user_id,
            session_id=session_id,
        )

        contradiction_ref: Optional[str] = None
        if contradiction_edge:
            contradiction_metadata = {
                "created_by_receipt_ref": receipt_ref,
                "source_receipt_ref": task.source_receipt_ref,
                "policy_scope": task.policy_scope,
                "graph_write_mode": "canonical_receipt_only",
                "curation_task_ref": task_ref,
                "curation_actor_ref": task.actor_ref,
                "curation_actor_role": actor_role,
                "curation_credit": asdict(credit),
            }
            contradiction_ref = await store.upsert_contradiction_record(
                claim_ref=task.contradiction_claim_ref or "",
                supporting_edge_refs=list(task.supporting_edge_refs),
                contradicting_edge_refs=list(task.contradicting_edge_refs),
                contradiction_kind=task.contradiction_kind or "",
                summary=task.details or f"Curated contradiction for {task.relationship}",
                metadata=contradiction_metadata,
                user_id=user_id,
                session_id=session_id,
            )

        receipt = EdgeCorrectionReceipt(
            receipt_ref=receipt_ref,
            task_ref=task_ref,
            edge_ref=edge_ref,
            source_receipt_ref=task.source_receipt_ref,
            corrected_edge_status=task.corrected_edge_status,
            actor_ref=task.actor_ref,
            actor_role=actor_role,
            reason_codes=tuple(task.reason_codes),
            neo4j_projection_action="rebuild_required",
            contradiction_ref=contradiction_ref,
        )
        return GraphCurationResult(receipt=receipt, credit=credit, edge_metadata=edge_metadata)

    @staticmethod
    def _stable_ref(prefix: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}"
