from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from mica.kb.claim_atom import (
    BiologicalContext,
    ClaimAtom,
    ClaimKind,
    ClaimStatus,
    ClaimTier,
    EffectDirection,
    EntityBinding,
    EntityRef,
    PredicatePolarity,
    PredicateRef,
)
from mica.kb.consensus import ConsensusBuilder, ConsensusMetaClaim
from mica.kb.contradiction import ContradictionKind, ContradictionRecord, ContradictionStatus
from mica.kb.evidence_aggregator import EvidenceAggregator
from mica.kb.evidence_item import (
    EvidenceIndependenceKey,
    EvidenceItem,
    EvidenceKind,
    EvidenceStrength,
    SupportDirection,
)
from mica.kb.kb_store import KBStore

_DEBATE_FRONT_STATUSES = {
    "open_debate",
    "emerging_consensus",
    "stable_consensus",
    "resolved_contradiction",
    "revived_controversy",
}


def _stable_ref(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_refs(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


@dataclass(frozen=True)
class DebatePosition:
    position_ref: str
    claim_family_refs: tuple[str, ...]
    supporting_evidence_refs: tuple[str, ...]
    contradicting_evidence_refs: tuple[str, ...]
    institution_refs: tuple[str, ...]
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_ref": self.position_ref,
            "claim_family_refs": list(self.claim_family_refs),
            "supporting_evidence_refs": list(self.supporting_evidence_refs),
            "contradicting_evidence_refs": list(self.contradicting_evidence_refs),
            "institution_refs": list(self.institution_refs),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class DebateFront:
    debate_front_ref: str
    topic_ref: str
    status: str
    positions: tuple[DebatePosition, ...]
    open_contradiction_refs: tuple[str, ...]
    resolved_contradiction_refs: tuple[str, ...]
    consensus_history_refs: tuple[str, ...]
    last_reviewed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "debate_front_ref": self.debate_front_ref,
            "topic_ref": self.topic_ref,
            "status": self.status,
            "positions": [position.as_dict() for position in self.positions],
            "open_contradiction_refs": list(self.open_contradiction_refs),
            "resolved_contradiction_refs": list(self.resolved_contradiction_refs),
            "consensus_history_refs": list(self.consensus_history_refs),
            "last_reviewed_at": self.last_reviewed_at,
        }


@dataclass(frozen=True)
class MetaAnalysisNode:
    meta_analysis_ref: str
    topic_ref: str
    included_claim_family_refs: tuple[str, ...]
    excluded_claim_family_refs: tuple[str, ...]
    exclusion_reasons: dict[str, str]
    consensus_state: str
    consensus_meta_claim_ref: str | None
    evidence_summary_refs: tuple[str, ...]
    counts_as_independent_evidence: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta_analysis_ref": self.meta_analysis_ref,
            "topic_ref": self.topic_ref,
            "included_claim_family_refs": list(self.included_claim_family_refs),
            "excluded_claim_family_refs": list(self.excluded_claim_family_refs),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "consensus_state": self.consensus_state,
            "consensus_meta_claim_ref": self.consensus_meta_claim_ref,
            "evidence_summary_refs": list(self.evidence_summary_refs),
            "counts_as_independent_evidence": self.counts_as_independent_evidence,
        }


@dataclass(frozen=True)
class ConsensusReviewResult:
    front: DebateFront
    consensus_meta_claim: ConsensusMetaClaim | None
    meta_analysis_node: MetaAnalysisNode

    def as_dict(self) -> dict[str, Any]:
        return {
            "front": self.front.as_dict(),
            "consensus_meta_claim": asdict(self.consensus_meta_claim) if self.consensus_meta_claim else None,
            "meta_analysis_node": self.meta_analysis_node.as_dict(),
        }


class ConsensusMetaClaimBuilder:
    """G4.8 debate front and consensus authority over existing KB seams."""

    policy_ref = "graph_debate_consensus://g4p8/v1"

    def __init__(self, *, consensus_builder: ConsensusBuilder | None = None) -> None:
        self._consensus_builder = consensus_builder or ConsensusBuilder()

    def review_debate_front(
        self,
        *,
        topic_ref: str,
        positions: Sequence[DebatePosition],
        consensus_history_refs: Sequence[str] = (),
        open_contradiction_refs: Sequence[str] = (),
        resolved_contradiction_refs: Sequence[str] = (),
        last_reviewed_at: str | None = None,
        consensus_status: str | None = None,
    ) -> DebateFront:
        normalized_topic_ref = str(topic_ref or "").strip()
        if not normalized_topic_ref:
            raise ValueError("topic_ref_required")
        normalized_positions = tuple(positions)
        if not normalized_positions:
            raise ValueError("debate_front_requires_positions")

        open_refs = _normalize_refs(open_contradiction_refs)
        resolved_refs = _normalize_refs(resolved_contradiction_refs)
        history_refs = _normalize_refs(consensus_history_refs)
        normalized_last_reviewed_at = str(last_reviewed_at or _utc_now()).strip() or _utc_now()
        status = self._derive_front_status(
            position_count=len(normalized_positions),
            open_contradiction_refs=open_refs,
            resolved_contradiction_refs=resolved_refs,
            consensus_history_refs=history_refs,
            consensus_status=consensus_status,
        )
        front_payload = {
            "topic_ref": normalized_topic_ref,
            "status": status,
            "positions": [position.as_dict() for position in normalized_positions],
            "open_contradiction_refs": list(open_refs),
            "resolved_contradiction_refs": list(resolved_refs),
        }
        return DebateFront(
            debate_front_ref=_stable_ref("debate_front://graphrag/", front_payload),
            topic_ref=normalized_topic_ref,
            status=status,
            positions=normalized_positions,
            open_contradiction_refs=open_refs,
            resolved_contradiction_refs=resolved_refs,
            consensus_history_refs=history_refs,
            last_reviewed_at=normalized_last_reviewed_at,
        )

    def review_consensus(
        self,
        *,
        topic_ref: str,
        claims: Sequence[Mapping[str, Any]],
        evidence_items: Sequence[Mapping[str, Any]],
        contradictions: Sequence[Mapping[str, Any]] = (),
        positions: Sequence[DebatePosition] = (),
        consensus_history_refs: Sequence[str] = (),
        resolved_contradiction_refs: Sequence[str] = (),
        excluded_claim_family_refs: Sequence[str] = (),
        exclusion_reasons: Mapping[str, str] | None = None,
        last_reviewed_at: str | None = None,
    ) -> ConsensusReviewResult:
        kb = self._build_kb_store(
            claims=claims,
            evidence_items=evidence_items,
            contradictions=contradictions,
        )
        evidence_aggregator = EvidenceAggregator(kb._evidence)
        evidence_groups = {
            family_ref: evidence_aggregator.group_by_claim(family_ref)
            for family_ref in kb._families.keys()
        }
        consensus_meta_claim = self._consensus_builder.analyze(
            claim_families=kb._families,
            evidence_groups=evidence_groups,
            contradictions=kb._contradictions,
        )
        consensus_state = "insufficient_evidence"
        if consensus_meta_claim is not None:
            consensus_state = consensus_meta_claim.status
        history_refs = list(_normalize_refs(consensus_history_refs))
        history_event_ref = _stable_ref(
            "consensus_state://graphrag/",
            {
                "topic_ref": topic_ref,
                "consensus_state": consensus_state,
                "meta_claim_ref": consensus_meta_claim.meta_claim_ref if consensus_meta_claim else None,
                "blocked_by": list(consensus_meta_claim.blocked_by) if consensus_meta_claim else [],
            },
        )
        history_refs.append(history_event_ref)
        derived_positions = tuple(positions) if positions else self._derive_positions_from_claims(claims, evidence_items)
        front = self.review_debate_front(
            topic_ref=topic_ref,
            positions=derived_positions,
            consensus_history_refs=history_refs,
            open_contradiction_refs=[
                contradiction.contradiction_ref
                for contradiction in kb._contradictions.values()
                if contradiction.status == ContradictionStatus.OPEN
            ],
            resolved_contradiction_refs=resolved_contradiction_refs,
            last_reviewed_at=last_reviewed_at,
            consensus_status=consensus_state,
        )
        meta_analysis_node = self._build_meta_analysis_node(
            topic_ref=topic_ref,
            consensus_state=consensus_state,
            consensus_meta_claim=consensus_meta_claim,
            evidence_items=evidence_items,
            excluded_claim_family_refs=excluded_claim_family_refs,
            exclusion_reasons=exclusion_reasons or {},
        )
        return ConsensusReviewResult(
            front=front,
            consensus_meta_claim=consensus_meta_claim,
            meta_analysis_node=meta_analysis_node,
        )

    def _build_meta_analysis_node(
        self,
        *,
        topic_ref: str,
        consensus_state: str,
        consensus_meta_claim: ConsensusMetaClaim | None,
        evidence_items: Sequence[Mapping[str, Any]],
        excluded_claim_family_refs: Sequence[str],
        exclusion_reasons: Mapping[str, str],
    ) -> MetaAnalysisNode:
        included_claims = tuple(consensus_meta_claim.claim_family_refs) if consensus_meta_claim else ()
        excluded_claims = _normalize_refs(excluded_claim_family_refs)
        evidence_summary_refs = _normalize_refs(
            [
                str(item.get("evidence_ref") or "").strip()
                for item in evidence_items
            ]
        )
        payload = {
            "topic_ref": topic_ref,
            "included_claims": list(included_claims),
            "excluded_claims": list(excluded_claims),
            "consensus_state": consensus_state,
            "consensus_meta_claim_ref": consensus_meta_claim.meta_claim_ref if consensus_meta_claim else None,
        }
        return MetaAnalysisNode(
            meta_analysis_ref=_stable_ref("meta_analysis://graphrag/", payload),
            topic_ref=str(topic_ref or "").strip(),
            included_claim_family_refs=included_claims,
            excluded_claim_family_refs=excluded_claims,
            exclusion_reasons={str(key): str(value) for key, value in exclusion_reasons.items()},
            consensus_state=consensus_state,
            consensus_meta_claim_ref=consensus_meta_claim.meta_claim_ref if consensus_meta_claim else None,
            evidence_summary_refs=evidence_summary_refs,
            counts_as_independent_evidence=False,
        )

    def _derive_positions_from_claims(
        self,
        claims: Sequence[Mapping[str, Any]],
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> tuple[DebatePosition, ...]:
        evidence_by_claim: dict[str, list[str]] = {}
        contradicting_by_claim: dict[str, list[str]] = {}
        for item in evidence_items:
            claim_ref = str(item.get("claim_family_ref") or "").strip()
            evidence_ref = str(item.get("evidence_ref") or "").strip()
            direction = str(item.get("support_direction") or "supports").strip().lower()
            if not claim_ref or not evidence_ref:
                continue
            if direction == SupportDirection.CONTRADICTS.value:
                contradicting_by_claim.setdefault(claim_ref, []).append(evidence_ref)
            else:
                evidence_by_claim.setdefault(claim_ref, []).append(evidence_ref)
        positions: list[DebatePosition] = []
        for claim in claims:
            claim_family_ref = str(claim.get("claim_family_ref") or "").strip()
            if not claim_family_ref:
                continue
            positions.append(
                DebatePosition(
                    position_ref=_stable_ref(
                        "debate_position://graphrag/",
                        {
                            "claim_family_ref": claim_family_ref,
                            "institutions": list(claim.get("institution_refs") or []),
                        },
                    ),
                    claim_family_refs=(claim_family_ref,),
                    supporting_evidence_refs=_normalize_refs(evidence_by_claim.get(claim_family_ref, [])),
                    contradicting_evidence_refs=_normalize_refs(contradicting_by_claim.get(claim_family_ref, [])),
                    institution_refs=_normalize_refs(claim.get("institution_refs") or []),
                    confidence=max(0.0, min(1.0, float(claim.get("confidence") or 0.5))),
                )
            )
        return tuple(positions)

    def _build_kb_store(
        self,
        *,
        claims: Sequence[Mapping[str, Any]],
        evidence_items: Sequence[Mapping[str, Any]],
        contradictions: Sequence[Mapping[str, Any]],
    ) -> KBStore:
        kb = KBStore()
        for raw in claims:
            family_ref = str(raw.get("claim_family_ref") or "").strip()
            if not family_ref:
                raise ValueError("claim_family_ref_required")
            subject_ref = str(raw.get("subject_entity_ref") or "").strip()
            predicate_id = str(raw.get("predicate_id") or "").strip()
            object_ref = str(raw.get("object_entity_ref") or "").strip()
            object_literal = str(raw.get("object_literal") or "").strip() or None
            if not subject_ref:
                raise ValueError("subject_entity_ref_required")
            if not predicate_id:
                raise ValueError("predicate_id_required")
            if not object_ref and not object_literal:
                raise ValueError("object_entity_ref_or_object_literal_required")
            atom = ClaimAtom(
                claim_ref=family_ref,
                claim_kind=ClaimKind.RELATION,
                subject=self._build_entity_binding(
                    entity_ref=subject_ref,
                    entity_type=str(raw.get("subject_entity_type") or "entity"),
                ),
                predicate=PredicateRef(
                    predicate_id=predicate_id,
                    polarity=PredicatePolarity(str(raw.get("polarity") or PredicatePolarity.NEUTRAL.value)),
                    direction=EffectDirection(str(raw.get("direction") or EffectDirection.UNKNOWN.value)),
                ),
                object=self._build_entity_binding(
                    entity_ref=object_ref,
                    entity_type=str(raw.get("object_entity_type") or "entity"),
                ) if object_ref else None,
                object_literal=object_literal,
                biological_context=BiologicalContext(
                    organism=str(raw.get("organism") or "taxon://9606"),
                    cell_type=raw.get("cell_type"),
                    tissue=raw.get("tissue"),
                    condition=raw.get("condition"),
                ),
                status=ClaimStatus.ACTIVE,
                tier=ClaimTier.LITERATURE_SUPPORTED,
                created_by_receipt_ref=raw.get("created_by_receipt_ref"),
            )
            kb.add_claim(
                family_ref=family_ref,
                claim_atom=atom,
                receipt_ref=raw.get("created_by_receipt_ref"),
            )

        for raw in evidence_items:
            family_ref = str(raw.get("claim_family_ref") or "").strip()
            if not family_ref:
                raise ValueError("evidence_claim_family_ref_required")
            support_direction = SupportDirection(str(raw.get("support_direction") or SupportDirection.SUPPORTS.value))
            evidence_kind = EvidenceKind(str(raw.get("evidence_kind") or EvidenceKind.LITERATURE.value))
            strength = EvidenceStrength(str(raw.get("strength") or EvidenceStrength.MODERATE.value))
            source_work_ref = str(raw.get("source_work_ref") or raw.get("artifact_ref") or raw.get("evidence_ref") or "").strip()
            source_version_ref = str(raw.get("source_version_ref") or "v1").strip()
            method_family = str(raw.get("method_family") or evidence_kind.value).strip()
            experimental_system = str(raw.get("experimental_system") or "mixed").strip()
            evidence = EvidenceItem(
                evidence_ref=str(raw.get("evidence_ref") or "").strip() or _stable_ref("evidence://graphrag/", raw),
                claim_ref=family_ref,
                artifact_ref=raw.get("artifact_ref"),
                evidence_kind=evidence_kind,
                support_direction=support_direction,
                strength=strength,
                independence_key=EvidenceIndependenceKey(
                    source_work_ref=source_work_ref,
                    source_version_ref=source_version_ref,
                    method_family=method_family,
                    experimental_system=experimental_system,
                    lab_group=raw.get("lab_group"),
                    protocol_run_family=raw.get("protocol_run_family"),
                ),
                receipt_ref=raw.get("receipt_ref"),
                confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0.5))),
                source_doi=raw.get("source_doi"),
                source_pmid=raw.get("source_pmid"),
            )
            kb.add_evidence(evidence)

        for raw in contradictions:
            contradiction = ContradictionRecord(
                contradiction_ref=str(raw.get("contradiction_ref") or "").strip() or _stable_ref("contradiction://graphrag/", raw),
                claim_a_ref=str(raw.get("claim_a_ref") or "").strip(),
                claim_b_ref=str(raw.get("claim_b_ref") or "").strip(),
                contradiction_kind=ContradictionKind(str(raw.get("contradiction_kind") or ContradictionKind.OPPOSITE_DIRECTION.value)),
                status=ContradictionStatus(str(raw.get("status") or ContradictionStatus.OPEN.value)),
                explanation=raw.get("explanation"),
            )
            kb.add_contradiction(contradiction)

        return kb

    def _derive_front_status(
        self,
        *,
        position_count: int,
        open_contradiction_refs: Sequence[str],
        resolved_contradiction_refs: Sequence[str],
        consensus_history_refs: Sequence[str],
        consensus_status: str | None,
    ) -> str:
        normalized_consensus_status = str(consensus_status or "").strip().lower()
        if open_contradiction_refs:
            if consensus_history_refs:
                return "revived_controversy"
            return "open_debate"
        if resolved_contradiction_refs and normalized_consensus_status in {"consensus", "active"}:
            return "resolved_contradiction"
        if normalized_consensus_status in {"consensus", "active"}:
            return "stable_consensus" if position_count <= 1 else "emerging_consensus"
        if position_count > 1:
            return "open_debate"
        return "emerging_consensus"

    @staticmethod
    def _build_entity_binding(*, entity_ref: str, entity_type: str) -> EntityBinding:
        normalized_ref = str(entity_ref or "").strip()
        return EntityBinding(
            role="entity",
            entity_ref=EntityRef(
                entity_type=str(entity_type or "entity").strip() or "entity",
                entity_id=normalized_ref,
                canonical_label=normalized_ref.split("://")[-1],
            ),
            resolved_from=normalized_ref,
            resolver_snapshot_ref="resolver://graphrag/g4p8",
            confidence=1.0,
            receipt_ref="receipt://graphrag/g4p8/entity-binding",
        )
