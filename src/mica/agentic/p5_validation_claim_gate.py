from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, model_validator


VALIDATION_CLAIM_GATE_SCHEMA_ID = "mica.project_tolomeo.p5.validation_claim_gate.v1"
VALIDATION_CLAIM_GATE_RESULT_SCHEMA_ID = "mica.project_tolomeo.p5.validation_claim_gate_result.v1"
VALIDATION_CLAIM_GATE_NAME = "quetzal.validation_claim_gate"

SCREENING_TIER = "screening_signal"
CG_SUPPORTED_TIER = "cg_supported"
AA_SUPPORTED_TIER = "aa_supported"
VALIDATED_TIERS = ("validated_computational", "validated_experimental")
SUPPORTED_REQUESTED_TIERS = (
    SCREENING_TIER,
    "hypothesis",
    "literature_supported",
    CG_SUPPORTED_TIER,
    AA_SUPPORTED_TIER,
    *VALIDATED_TIERS,
)
CG_EVIDENCE_MARKERS = (
    "ese_cg",
    "cg_validation",
    "cg_supported",
    "mdanalysis",
    "trajectory_to_ese_cg_signature",
    "receipt://quetzal/p5-ese-cg-contract/",
    "receipt://quetzal/p5-cg-pre-submit/",
)
AA_EVIDENCE_MARKERS = (
    "ese_aa",
    "aa_supported",
    "atomistic",
    "all_atom",
)
PROVIDER_MARKERS = (
    "provider",
    "biolm",
    "modal",
    "runpod",
    "serverless_model",
    "model_inferred",
    "screening",
)


class P5ClaimEvidenceRef(BaseModel):
    evidence_ref: str
    evidence_kind: str = "unknown"
    artifact_ref: str | None = None
    receipt_ref: str | None = None
    tier_support: str | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _has_some_ref(self) -> "P5ClaimEvidenceRef":
        if not (self.evidence_ref or self.artifact_ref or self.receipt_ref):
            raise ValueError("evidence ref requires evidence_ref, artifact_ref, or receipt_ref")
        return self


class P5ValidationClaimDraft(BaseModel):
    claim_ref: str
    subject_ref: str
    predicate: str
    object_ref: str | None = None
    claim_text: str
    current_tier: str = SCREENING_TIER
    requested_tier: str = CG_SUPPORTED_TIER
    proposer_agent: str | None = None
    source_surface: str | None = None
    evidence_refs: tuple[P5ClaimEvidenceRef, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _required_claim_fields(self) -> "P5ValidationClaimDraft":
        missing = [
            name
            for name, value in (
                ("claim_ref", self.claim_ref),
                ("subject_ref", self.subject_ref),
                ("predicate", self.predicate),
                ("claim_text", self.claim_text),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise ValueError(f"claim draft missing required fields: {', '.join(missing)}")
        return self


class P5ValidationClaimGateReceipt(BaseModel):
    receipt_ref: str
    receipt_type: str = "QuetzalValidationClaimGateReceipt"
    gate_name: str = VALIDATION_CLAIM_GATE_NAME
    decision: Literal["approved_for_claim_transition_proposal", "rejected"]
    reason_codes: tuple[str, ...]
    evaluated_policies: tuple[str, ...] = (
        "claim_transition_requires_validation_evidence",
        "cg_supported_requires_cg_evidence",
        "aa_supported_requires_aa_evidence",
        "provider_inference_cannot_directly_validate",
        "chronoracle_observe_propose_only_cannot_bypass_gate",
        "no_graph_promotion_side_effects",
    )
    max_allowed_tier: str = SCREENING_TIER
    requested_tier: str
    approved_tier: str | None = None
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P5ValidationClaimGateResult(BaseModel):
    schema_id: str = VALIDATION_CLAIM_GATE_RESULT_SCHEMA_ID
    p5_id: str
    claim: P5ValidationClaimDraft
    status: Literal["approved_for_proposal", "blocked"]
    receipt: P5ValidationClaimGateReceipt
    blockers: tuple[Dict[str, Any], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    mudo_lineage_required: bool = True
    graph_write_performed: bool = False
    claim_promotion_performed: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any, length: int = 20) -> str:
    return f"{prefix}_{_sha256_text(_stable_json(value))[:length]}"


def _field_text(evidence: P5ClaimEvidenceRef) -> str:
    return " ".join(
        str(value or "")
        for value in (
            evidence.evidence_ref,
            evidence.evidence_kind,
            evidence.artifact_ref,
            evidence.receipt_ref,
            evidence.tier_support,
            evidence.source,
        )
    ).lower()


def _contains_marker(text: str, markers: Sequence[str]) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _has_cg_evidence(claim: P5ValidationClaimDraft) -> bool:
    for evidence in claim.evidence_refs:
        if _contains_marker(_field_text(evidence), CG_EVIDENCE_MARKERS):
            return True
    return any(_contains_marker(ref, CG_EVIDENCE_MARKERS) for ref in (*claim.artifact_refs, *claim.receipt_refs))


def _has_aa_evidence(claim: P5ValidationClaimDraft) -> bool:
    for evidence in claim.evidence_refs:
        if _contains_marker(_field_text(evidence), AA_EVIDENCE_MARKERS):
            return True
    return any(_contains_marker(ref, AA_EVIDENCE_MARKERS) for ref in (*claim.artifact_refs, *claim.receipt_refs))


def _has_only_provider_inference(claim: P5ValidationClaimDraft) -> bool:
    all_refs = [
        *claim.artifact_refs,
        *claim.receipt_refs,
        *(evidence.evidence_ref for evidence in claim.evidence_refs),
        *(evidence.evidence_kind for evidence in claim.evidence_refs),
        *(evidence.source or "" for evidence in claim.evidence_refs),
    ]
    if not all_refs:
        return False
    has_provider_marker = any(_contains_marker(ref, PROVIDER_MARKERS) for ref in all_refs)
    has_validation_marker = _has_cg_evidence(claim) or _has_aa_evidence(claim)
    return has_provider_marker and not has_validation_marker


def _is_chronoracle(claim: P5ValidationClaimDraft) -> bool:
    text = f"{claim.proposer_agent or ''} {claim.source_surface or ''}".lower()
    return "chronoracle" in text


def _block(code: str, message: str, **details: Any) -> Dict[str, Any]:
    return {"code": code, "message": message, "retryable": False, "details": {k: v for k, v in details.items() if v is not None}}


def _coerce_evidence_refs(raw: Any) -> tuple[P5ClaimEvidenceRef, ...]:
    if raw is None:
        return ()
    values = raw if isinstance(raw, list) else [raw]
    refs: list[P5ClaimEvidenceRef] = []
    for value in values:
        if isinstance(value, P5ClaimEvidenceRef):
            refs.append(value)
        elif isinstance(value, Mapping):
            refs.append(P5ClaimEvidenceRef(**dict(value)))
        else:
            refs.append(P5ClaimEvidenceRef(evidence_ref=str(value)))
    return tuple(refs)


def build_p5_validation_claim_gate_result(
    packet: Mapping[str, Any],
    *,
    claim_payload: Mapping[str, Any],
) -> P5ValidationClaimGateResult:
    p5_id = str(packet.get("p5_id") or claim_payload.get("p5_id") or "unknown-p5").strip()
    claim_input = dict(claim_payload)
    claim_input["evidence_refs"] = _coerce_evidence_refs(claim_input.get("evidence_refs"))
    claim = P5ValidationClaimDraft(**claim_input)
    requested_tier = str(claim.requested_tier or "").strip()
    blockers: list[Dict[str, Any]] = []

    if requested_tier not in SUPPORTED_REQUESTED_TIERS:
        blockers.append(_block(
            "unsupported_requested_tier",
            "Requested claim tier is not supported by the P5 validation claim gate.",
            requested_tier=requested_tier,
            supported_tiers=list(SUPPORTED_REQUESTED_TIERS),
        ))
    if claim.current_tier != SCREENING_TIER and requested_tier != claim.current_tier:
        blockers.append(_block(
            "non_screening_transition_requires_future_gate",
            "P5 only governs transitions from screening_signal into validation-supported proposal tiers.",
            current_tier=claim.current_tier,
            requested_tier=requested_tier,
        ))
    if requested_tier == CG_SUPPORTED_TIER and not _has_cg_evidence(claim):
        blockers.append(_block(
            "missing_cg_evidence",
            "cg_supported claim proposal requires CG/ESE-CG evidence refs.",
            requested_tier=requested_tier,
        ))
    if requested_tier == AA_SUPPORTED_TIER and not _has_aa_evidence(claim):
        blockers.append(_block(
            "missing_aa_evidence",
            "aa_supported claim proposal requires AA evidence refs.",
            requested_tier=requested_tier,
        ))
    if requested_tier in VALIDATED_TIERS:
        blockers.append(_block(
            "validated_tier_requires_later_aa_gate",
            "P5 validation_claim_gate cannot promote claims directly into validated tiers.",
            requested_tier=requested_tier,
        ))
    if _has_only_provider_inference(claim) and requested_tier != SCREENING_TIER:
        blockers.append(_block(
            "provider_inference_cannot_directly_promote",
            "Provider inference evidence alone cannot move a claim beyond screening_signal.",
            requested_tier=requested_tier,
        ))
    if _is_chronoracle(claim) and requested_tier != SCREENING_TIER and not (_has_cg_evidence(claim) or _has_aa_evidence(claim)):
        blockers.append(_block(
            "chronoracle_cannot_bypass_validation_claim_gate",
            "ChronOracle is observe/propose-only and must provide validation evidence refs to pass this gate.",
            requested_tier=requested_tier,
        ))

    approved = not blockers
    max_allowed_tier = requested_tier if approved else SCREENING_TIER
    receipt_ref = "receipt://quetzal/p5-validation-claim-gate/" + _stable_id(
        "qclaim",
        {
            "p5_id": p5_id,
            "claim_ref": claim.claim_ref,
            "requested_tier": requested_tier,
            "blockers": [blocker["code"] for blocker in blockers],
        },
    )
    evidence_refs = tuple(
        sorted({
            *(evidence.evidence_ref for evidence in claim.evidence_refs if evidence.evidence_ref),
            *(evidence.artifact_ref for evidence in claim.evidence_refs if evidence.artifact_ref),
            *(evidence.receipt_ref for evidence in claim.evidence_refs if evidence.receipt_ref),
            *claim.artifact_refs,
            *claim.receipt_refs,
        })
    )
    receipt = P5ValidationClaimGateReceipt(
        receipt_ref=receipt_ref,
        decision="approved_for_claim_transition_proposal" if approved else "rejected",
        reason_codes=tuple(sorted({blocker["code"] for blocker in blockers})) or ("claim_transition_evidence_sufficient",),
        max_allowed_tier=max_allowed_tier,
        requested_tier=requested_tier,
        approved_tier=requested_tier if approved else None,
    )
    return P5ValidationClaimGateResult(
        p5_id=p5_id,
        claim=claim,
        status="approved_for_proposal" if approved else "blocked",
        receipt=receipt,
        blockers=tuple(blockers),
        evidence_refs=evidence_refs,
        artifact_refs=tuple(claim.artifact_refs),
        receipt_refs=(receipt_ref, *claim.receipt_refs),
    )
