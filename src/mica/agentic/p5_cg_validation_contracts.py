from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Literal, Mapping

from pydantic import BaseModel, Field, model_validator


CG_PROPOSAL_SCHEMA_ID = "mica.project_tolomeo.p5.cg_validation_proposal.v1"
CG_JOB_KIND = "biodynamo_cg_validation"
CG_PENDING_ARTIFACT_PREFIX = "cg_validation_pending://"
REAL_PROVIDER_PROOF_KIND = "real_provider_protocol_native"


class P5CGBlocker(BaseModel):
    code: str
    message: str
    proof_id: str | None = None
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class P5CGCandidateRef(BaseModel):
    proof_id: str
    provider: str
    protocol_id: str
    artifact_ref: str
    artifact_sha256: str
    artifact_size_bytes: int = 0
    source_receipt_refs: tuple[str, ...]
    source_mudo_refs: tuple[str, ...]
    score: float
    rank: int
    selection_reason: str
    claim_tier_cap: str = "screening_signal"

    @model_validator(mode="after")
    def _refs_only(self) -> "P5CGCandidateRef":
        if not self.artifact_ref.startswith("gs://"):
            raise ValueError("P5 CG candidates must reference durable GCS artifacts only")
        if not self.source_receipt_refs:
            raise ValueError("P5 CG candidates require source receipt refs")
        if not self.source_mudo_refs:
            raise ValueError("P5 CG candidates require source MUDO refs")
        return self


class P5CGMUDODependencyEdge(BaseModel):
    relation: str = "candidate_for_cg_validation"
    source_artifact_ref: str
    target_artifact_ref: str
    source_mudo_refs: tuple[str, ...]
    pending_mudo_write: bool = True


class P5CGJobRequest(BaseModel):
    schema_id: str = "mica.biodynamo.cg_validation_job_request.v1"
    request_id: str
    job_kind: str = CG_JOB_KIND
    candidate: P5CGCandidateRef
    artifact_refs: tuple[str, ...]
    receipt_refs: tuple[str, ...]
    mudo_dependency_edges: tuple[P5CGMUDODependencyEdge, ...]
    provider_job_created: bool = False
    execution_status: Literal["proposal_only"] = "proposal_only"


class P5CGQuetzalReceipt(BaseModel):
    receipt_ref: str
    gate_name: str = "quetzal.cg_validation_pre_submit"
    decision: Literal["approved_for_proposal_only", "rejected"]
    reason_codes: tuple[str, ...]
    evaluated_policies: tuple[str, ...] = (
        "refs_only_candidate_selection",
        "p4_receipt_required",
        "p4_mudo_lineage_required",
        "no_claim_promotion_before_validation_claim_gate",
    )
    max_allowed_tier: str = "screening_signal"
    claim_promotion_allowed: bool = False
    provider_job_created: bool = False
    approvers: tuple[str, ...] = ()
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P5CGProposalResult(BaseModel):
    schema_id: str = CG_PROPOSAL_SCHEMA_ID
    p5_id: str
    status: Literal["ready", "blocked"]
    top_k: int
    candidates: tuple[P5CGCandidateRef, ...]
    cg_job_requests: tuple[P5CGJobRequest, ...]
    quetzal_pre_submit_receipt: P5CGQuetzalReceipt
    blockers: tuple[P5CGBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    mudo_dependency_edges: tuple[P5CGMUDODependencyEdge, ...] = ()
    raw_provider_payload_embedded: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any, length: int = 20) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _packet_list(packet: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return [dict(item) for item in packet.get(key) or [] if isinstance(item, Mapping)]


def _group_refs_by_proof(packet: Mapping[str, Any], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in _packet_list(packet, key):
        proof_id = str(item.get("proof_id") or "").strip()
        if proof_id:
            grouped.setdefault(proof_id, []).append(item)
    return grouped


def _contains_raw_provider_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in {"raw_payload", "provider_payload", "raw_provider_payload", "raw_output"}:
                return True
            if _contains_raw_provider_payload(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_raw_provider_payload(item) for item in value)
    return False


def _real_provider_proofs(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    proofs = _packet_list(packet, "proof_refs")
    return [
        proof
        for proof in proofs
        if proof.get("proof_kind") == REAL_PROVIDER_PROOF_KIND
        and str(proof.get("status") or "").lower() == "passed"
    ]


def _validate_packet_refs(packet: Mapping[str, Any]) -> list[P5CGBlocker]:
    blockers: list[P5CGBlocker] = []
    if bool((packet.get("scope") or {}).get("raw_provider_payload_embedded")) or _contains_raw_provider_payload(packet):
        blockers.append(P5CGBlocker(
            code="raw_provider_payload_present",
            message="P5 CG proposal refuses packets that embed raw provider payloads.",
        ))

    artifacts_by_proof = _group_refs_by_proof(packet, "artifact_refs")
    receipts_by_proof = _group_refs_by_proof(packet, "receipt_refs")
    mudo_by_proof = _group_refs_by_proof(packet, "mudo_refs")
    for proof in _real_provider_proofs(packet):
        proof_id = str(proof.get("proof_id") or "").strip()
        if proof_id not in artifacts_by_proof:
            blockers.append(P5CGBlocker(
                code="missing_p4_artifact_ref",
                message="Real provider proof is missing a durable P4 artifact ref.",
                proof_id=proof_id,
            ))
        if proof_id not in receipts_by_proof:
            blockers.append(P5CGBlocker(
                code="missing_p4_receipt_ref",
                message="Real provider proof is missing a durable P4 receipt ref.",
                proof_id=proof_id,
            ))
        if proof_id not in mudo_by_proof:
            blockers.append(P5CGBlocker(
                code="missing_p4_mudo_ref",
                message="Real provider proof is missing imported P4 MUDO lineage refs.",
                proof_id=proof_id,
            ))
    return blockers


def _candidate_score(*, proof: Mapping[str, Any], artifact: Mapping[str, Any]) -> float:
    provider = str(proof.get("provider") or "").lower()
    provider_weight = {"modal": 100.0, "biolm": 80.0}.get(provider, 50.0)
    size_component = min(float(artifact.get("size_bytes") or 0) / 1000.0, 99.0)
    sha_component = int(hashlib.sha256(str(artifact.get("artifact_ref") or "").encode("utf-8")).hexdigest()[:4], 16) / 65535.0
    return round(provider_weight + size_component + sha_component, 6)


def select_p5_cg_top_k_candidates(packet: Mapping[str, Any], *, top_k: int = 3) -> tuple[tuple[P5CGCandidateRef, ...], tuple[P5CGBlocker, ...]]:
    requested_top_k = max(1, min(int(top_k or 1), 10))
    blockers = _validate_packet_refs(packet)
    if blockers:
        return (), tuple(blockers)

    proofs_by_id = {str(proof.get("proof_id") or "").strip(): proof for proof in _real_provider_proofs(packet)}
    artifacts_by_proof = _group_refs_by_proof(packet, "artifact_refs")
    receipts_by_proof = _group_refs_by_proof(packet, "receipt_refs")
    mudo_by_proof = _group_refs_by_proof(packet, "mudo_refs")
    claim_tier_cap = str((packet.get("claim_policy") or {}).get("max_allowed_tier") or "screening_signal")

    candidates: list[P5CGCandidateRef] = []
    for proof_id, proof in proofs_by_id.items():
        receipt_refs = tuple(sorted(str(item.get("receipt_ref") or "") for item in receipts_by_proof.get(proof_id, []) if item.get("receipt_ref")))
        mudo_refs = tuple(sorted(
            f"mudo://{item.get('mudo_id')}/commits/{item.get('commit_id')}"
            for item in mudo_by_proof.get(proof_id, [])
            if item.get("mudo_id") and item.get("commit_id")
        ))
        for artifact in artifacts_by_proof.get(proof_id, []):
            artifact_ref = str(artifact.get("artifact_ref") or "").strip()
            if not artifact_ref.startswith("gs://"):
                blockers.append(P5CGBlocker(
                    code="non_durable_artifact_ref",
                    message="CG candidates require gs:// durable artifact refs.",
                    proof_id=proof_id,
                    details={"artifact_ref": artifact_ref},
                ))
                continue
            candidates.append(P5CGCandidateRef(
                proof_id=proof_id,
                provider=str(proof.get("provider") or ""),
                protocol_id=str(proof.get("protocol_id") or ""),
                artifact_ref=artifact_ref,
                artifact_sha256=str(artifact.get("sha256") or ""),
                artifact_size_bytes=int(artifact.get("size_bytes") or 0),
                source_receipt_refs=receipt_refs,
                source_mudo_refs=mudo_refs,
                score=_candidate_score(proof=proof, artifact=artifact),
                rank=0,
                selection_reason="real_provider_p4_artifact_with_receipt_and_mudo_lineage",
                claim_tier_cap=claim_tier_cap,
            ))

    if blockers:
        return (), tuple(blockers)
    if not candidates:
        return (), (P5CGBlocker(
            code="no_cg_candidates",
            message="No real provider P4 artifacts are eligible for CG validation proposal.",
        ),)

    candidates.sort(key=lambda item: (-item.score, item.provider, item.proof_id, item.artifact_ref))
    ranked: list[P5CGCandidateRef] = []
    for index, candidate in enumerate(candidates[:requested_top_k], start=1):
        ranked.append(candidate.model_copy(update={"rank": index}))
    return tuple(ranked), ()


def build_p5_cg_validation_proposal(packet: Mapping[str, Any], *, top_k: int = 3) -> P5CGProposalResult:
    requested_top_k = max(1, min(int(top_k or 1), 10))
    p5_id = str(packet.get("p5_id") or "").strip() or "unknown-p5"
    candidates, blockers = select_p5_cg_top_k_candidates(packet, top_k=requested_top_k)
    status: Literal["ready", "blocked"] = "blocked" if blockers else "ready"
    reason_codes = tuple(sorted({blocker.code for blocker in blockers})) or ("cg_validation_proposal_ready",)
    decision: Literal["approved_for_proposal_only", "rejected"] = "rejected" if blockers else "approved_for_proposal_only"
    receipt_ref = f"receipt://quetzal/p5-cg-pre-submit/{_stable_id('qcg', {'p5_id': p5_id, 'reason_codes': reason_codes, 'top_k': requested_top_k})}"
    quetzal_receipt = P5CGQuetzalReceipt(
        receipt_ref=receipt_ref,
        decision=decision,
        reason_codes=reason_codes,
    )

    job_requests: list[P5CGJobRequest] = []
    dependency_edges: list[P5CGMUDODependencyEdge] = []
    for candidate in candidates:
        request_id = _stable_id("cg_req", candidate.model_dump(mode="json"))
        pending_artifact_ref = f"{CG_PENDING_ARTIFACT_PREFIX}{request_id}"
        edge = P5CGMUDODependencyEdge(
            source_artifact_ref=candidate.artifact_ref,
            target_artifact_ref=pending_artifact_ref,
            source_mudo_refs=candidate.source_mudo_refs,
        )
        dependency_edges.append(edge)
        job_requests.append(P5CGJobRequest(
            request_id=request_id,
            candidate=candidate,
            artifact_refs=(candidate.artifact_ref, pending_artifact_ref),
            receipt_refs=(*candidate.source_receipt_refs, quetzal_receipt.receipt_ref),
            mudo_dependency_edges=(edge,),
        ))

    artifact_refs = tuple(
        artifact_ref
        for job in job_requests
        for artifact_ref in job.artifact_refs
    )
    receipt_refs = tuple(sorted({quetzal_receipt.receipt_ref, *(ref for candidate in candidates for ref in candidate.source_receipt_refs)}))
    return P5CGProposalResult(
        p5_id=p5_id,
        status=status,
        top_k=requested_top_k,
        candidates=candidates,
        cg_job_requests=tuple(job_requests),
        quetzal_pre_submit_receipt=quetzal_receipt,
        blockers=tuple(blockers),
        artifact_refs=artifact_refs,
        receipt_refs=receipt_refs,
        mudo_dependency_edges=tuple(dependency_edges),
        raw_provider_payload_embedded=False,
    )
