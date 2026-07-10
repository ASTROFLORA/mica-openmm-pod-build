from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping

from pydantic import BaseModel, Field, model_validator

from mica.agentic.p5_validation_claim_gate import P5ValidationClaimDraft


CITATION_CASCADE_SCHEMA_ID = "mica.project_tolomeo.p5.citation_cascade.v1"
CITATION_CONSOLIDATION_SCHEMA_ID = "mica.project_tolomeo.p5.citation_consolidation.v1"
ALEJANDRIA_PROJECTION_MODE = "read_propose_only"
QUETZAL_RECEIPT_PREFIX = "receipt://quetzal/p5-validation-claim-gate/"


class P5EvidenceLine(BaseModel):
    evidence_ref: str
    citation_ref: str
    claim_ref: str
    direction: Literal["supports", "contradicts", "contextualizes", "unknown"] = "unknown"
    source_authority: str = "external_literature"
    provider: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    raw_payload_embedded: bool = False

    @model_validator(mode="after")
    def _validate_refs_only(self) -> "P5EvidenceLine":
        for field_name in ("evidence_ref", "citation_ref", "claim_ref"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"EvidenceLine requires {field_name}")
        if self.raw_payload_embedded:
            raise ValueError("EvidenceLine must remain refs-only")
        return self


class P5QuetzalClaimGateReceiptRef(BaseModel):
    receipt_ref: str
    decision: Literal["approved_for_claim_transition_proposal", "rejected"]
    max_allowed_tier: str

    @model_validator(mode="after")
    def _validate_authority(self) -> "P5QuetzalClaimGateReceiptRef":
        if not self.receipt_ref.startswith(QUETZAL_RECEIPT_PREFIX):
            raise ValueError("Quetzal receipt ref is not a P5 validation_claim_gate receipt")
        return self


class P5CitationCascadePacket(BaseModel):
    schema_id: str = CITATION_CASCADE_SCHEMA_ID
    packet_ref: str
    claim_ref: str
    evidence_lines: tuple[P5EvidenceLine, ...]
    evidence_refs: tuple[str, ...]
    citation_refs: tuple[str, ...]
    raw_payload_embedded: bool = False


class P5CitationConsolidationReceipt(BaseModel):
    receipt_ref: str
    receipt_type: str = "P5CitationConsolidationProposalReceipt"
    decision: Literal["ready_for_consolidation_proposal", "blocked"]
    reason_codes: tuple[str, ...]
    evaluated_policies: tuple[str, ...] = (
        "citation_evidence_required",
        "quetzal_receipt_required_beyond_screening",
        "evidence_line_direction_preserved",
        "alejandria_read_propose_only",
        "no_claim_promotion_side_effect",
    )
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claim_promotion_performed: bool = False
    alejandria_write_performed: bool = False
    graph_write_performed: bool = False


class P5CitationConsolidationResult(BaseModel):
    schema_id: str = CITATION_CONSOLIDATION_SCHEMA_ID
    p5_id: str
    status: Literal["ready_for_proposal", "blocked"]
    claim: P5ValidationClaimDraft
    claim_state_after: Literal["draft_unpromoted"] = "draft_unpromoted"
    citation_cascade: P5CitationCascadePacket
    quetzal_receipt: P5QuetzalClaimGateReceiptRef | None = None
    receipt: P5CitationConsolidationReceipt
    blockers: tuple[Dict[str, Any], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    alejandria_projection_mode: Literal["read_propose_only"] = ALEJANDRIA_PROJECTION_MODE
    claim_promotion_performed: bool = False
    alejandria_write_performed: bool = False
    graph_write_performed: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_ref(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}{digest}"


def _block(code: str, message: str, **details: Any) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": False,
        "details": {key: value for key, value in details.items() if value is not None},
    }


def _coerce_evidence_lines(raw: Any, *, claim_ref: str) -> tuple[P5EvidenceLine, ...]:
    values = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
    lines: list[P5EvidenceLine] = []
    for value in values:
        if isinstance(value, P5EvidenceLine):
            lines.append(value)
            continue
        if not isinstance(value, Mapping):
            raise ValueError("citation evidence lines must be objects")
        payload = dict(value)
        payload.setdefault("claim_ref", claim_ref)
        lines.append(P5EvidenceLine(**payload))
    return tuple(lines)


def build_p5_citation_consolidation(
    packet: Mapping[str, Any],
    *,
    claim_payload: Mapping[str, Any],
    citation_evidence: Any,
    quetzal_receipt_payload: Mapping[str, Any] | None = None,
) -> P5CitationConsolidationResult:
    p5_id = str(packet.get("p5_id") or claim_payload.get("p5_id") or "unknown-p5").strip()
    claim = P5ValidationClaimDraft(**dict(claim_payload))
    evidence_lines = _coerce_evidence_lines(citation_evidence, claim_ref=claim.claim_ref)
    blockers: list[Dict[str, Any]] = []

    if not evidence_lines:
        blockers.append(_block(
            "missing_citation_evidence",
            "Citation consolidation requires one or more refs-only EvidenceLine entries.",
        ))

    quetzal_receipt: P5QuetzalClaimGateReceiptRef | None = None
    if quetzal_receipt_payload:
        try:
            quetzal_receipt = P5QuetzalClaimGateReceiptRef(**dict(quetzal_receipt_payload))
        except ValueError as exc:
            blockers.append(_block(
                "invalid_quetzal_validation_receipt",
                "Citation consolidation received an invalid validation_claim_gate receipt.",
                error=str(exc),
            ))

    if claim.requested_tier != "screening_signal":
        if quetzal_receipt is None:
            blockers.append(_block(
                "missing_quetzal_validation_receipt",
                "Claim consolidation beyond screening_signal requires a durable Quetzal validation_claim_gate receipt.",
                requested_tier=claim.requested_tier,
            ))
        elif quetzal_receipt.decision != "approved_for_claim_transition_proposal":
            blockers.append(_block(
                "quetzal_validation_rejected",
                "Quetzal rejected the requested claim transition.",
                requested_tier=claim.requested_tier,
            ))
        elif quetzal_receipt.max_allowed_tier != claim.requested_tier:
            blockers.append(_block(
                "quetzal_tier_mismatch",
                "Quetzal max_allowed_tier does not match the requested claim tier.",
                requested_tier=claim.requested_tier,
                max_allowed_tier=quetzal_receipt.max_allowed_tier,
            ))

    evidence_refs = tuple(sorted({
        *(line.evidence_ref for line in evidence_lines),
        *(evidence.evidence_ref for evidence in claim.evidence_refs),
        *claim.artifact_refs,
        *claim.receipt_refs,
    }))
    citation_refs = tuple(sorted({line.citation_ref for line in evidence_lines}))
    cascade_ref = _stable_ref(
        "artifact://p5/citation-cascade/",
        {
            "p5_id": p5_id,
            "claim_ref": claim.claim_ref,
            "evidence_lines": [line.model_dump(mode="json") for line in evidence_lines],
        },
    )
    cascade = P5CitationCascadePacket(
        packet_ref=cascade_ref,
        claim_ref=claim.claim_ref,
        evidence_lines=evidence_lines,
        evidence_refs=evidence_refs,
        citation_refs=citation_refs,
    )
    receipt_ref = _stable_ref(
        "receipt://p5/citation-consolidation/",
        {
            "p5_id": p5_id,
            "claim_ref": claim.claim_ref,
            "cascade_ref": cascade_ref,
            "blockers": [blocker["code"] for blocker in blockers],
            "quetzal_receipt_ref": quetzal_receipt.receipt_ref if quetzal_receipt else None,
        },
    )
    ready = not blockers
    receipt = P5CitationConsolidationReceipt(
        receipt_ref=receipt_ref,
        decision="ready_for_consolidation_proposal" if ready else "blocked",
        reason_codes=tuple(sorted({blocker["code"] for blocker in blockers}))
        or ("citation_evidence_and_gate_sufficient_for_proposal",),
    )
    return P5CitationConsolidationResult(
        p5_id=p5_id,
        status="ready_for_proposal" if ready else "blocked",
        claim=claim,
        citation_cascade=cascade,
        quetzal_receipt=quetzal_receipt,
        receipt=receipt,
        blockers=tuple(blockers),
        evidence_refs=evidence_refs,
        artifact_refs=(cascade_ref,),
        receipt_refs=tuple(ref for ref in (
            receipt_ref,
            quetzal_receipt.receipt_ref if quetzal_receipt else None,
        ) if ref),
    )
