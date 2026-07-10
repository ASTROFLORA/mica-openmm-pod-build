from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping

from pydantic import BaseModel, Field, ValidationError, model_validator

from mica.agentic.p6_proactive_proposal import ProactiveProposal, _stable_ref
from mica.agentic.p6_proactive_proposal_gate import P6ProactiveProposalGateReceipt


P6_PROJECTED_REQUEST_SCHEMA_ID = "mica.project_tolomeo.p6.projected_request.v1"
P6_PROPOSAL_PROJECTION_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.p6.proposal_projection_receipt.v1"
P6_PROPOSAL_PROJECTION_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.proposal_projection_result.v1"

PROJECTABLE_ACTION_KINDS = ("protocol_request", "episode_request")


class P6ProposalProjectionBlocker(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class P6ProjectedRequest(BaseModel):
    schema_id: str = P6_PROJECTED_REQUEST_SCHEMA_ID
    request_ref: str
    request_artifact_ref: str
    request_kind: Literal["protocol_request", "episode_request"]
    p6_id: str
    workspace_id: str
    study_id: str
    proposal_ref: str
    proposal_artifact_ref: str
    proposal_receipt_ref: str
    quetzal_receipt_ref: str
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    requested_ref: str
    ingress_contract: Literal["ProtocolJSONLDDocument", "LiveDebateEpisodeRequest"]
    execution_authority: Literal["protocol_executor", "existing_episode_runtime"]
    projection_status: Literal["projected_not_submitted"] = "projected_not_submitted"
    idempotency_key: str
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def _validate_non_executing_projection(self) -> "P6ProjectedRequest":
        if self.projection_status != "projected_not_submitted":
            raise ValueError("P6 projected requests cannot be submitted during projection")
        if self.provider_jobs_created or self.protocol_runs_created or self.episode_runs_created:
            raise ValueError("P6 request projection cannot create runtime jobs")
        if self.outbox_dispatch_performed or self.execution_started:
            raise ValueError("P6 request projection cannot dispatch or execute")
        if self.claim_promotion_performed or self.graph_write_performed:
            raise ValueError("P6 request projection cannot mutate scientific knowledge")
        return self


class P6ProposalProjectionReceipt(BaseModel):
    schema_id: str = P6_PROPOSAL_PROJECTION_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["P6ProposalProjectionReceipt"] = "P6ProposalProjectionReceipt"
    p6_id: str
    proposal_ref: str
    proposal_receipt_ref: str | None = None
    quetzal_receipt_ref: str | None = None
    request_ref: str | None = None
    decision: Literal["projected", "blocked"]
    reason_codes: tuple[str, ...]
    request_kind: Literal["protocol_request", "episode_request", "none"]
    execution_authority: Literal["protocol_executor", "existing_episode_runtime", "none"]
    evaluated_policies: tuple[str, ...] = (
        "proposal_schema_valid",
        "proposal_receipt_required",
        "quetzal_approval_receipt_required",
        "proposal_and_gate_refs_must_match",
        "projectable_action_kind_required",
        "workspace_and_study_scope_required",
        "projection_only_no_dispatch",
    )
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6ProposalProjectionResult(BaseModel):
    schema_id: str = P6_PROPOSAL_PROJECTION_RESULT_SCHEMA_ID
    p6_id: str
    status: Literal["projected", "blocked"]
    request: P6ProjectedRequest | None = None
    receipt: P6ProposalProjectionReceipt
    blockers: tuple[P6ProposalProjectionBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _idempotency_key(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _block(code: str, message: str, **details: Any) -> P6ProposalProjectionBlocker:
    return P6ProposalProjectionBlocker(
        code=code,
        message=message,
        details={key: value for key, value in details.items() if value is not None},
    )


def _coerce_proposal(raw: Any) -> tuple[ProactiveProposal | None, tuple[P6ProposalProjectionBlocker, ...]]:
    if isinstance(raw, ProactiveProposal):
        return raw, ()
    if not isinstance(raw, Mapping):
        return None, (_block(
            "invalid_proposal_payload",
            "P6 request projection requires a ProactiveProposal object.",
            payload_type=type(raw).__name__,
        ),)
    try:
        return ProactiveProposal(**dict(raw)), ()
    except ValidationError as exc:
        return None, (_block(
            "invalid_proposal_payload",
            "ProactiveProposal failed schema validation before request projection.",
            errors=exc.errors(),
        ),)


def _coerce_gate_receipt(
    raw: Any,
) -> tuple[P6ProactiveProposalGateReceipt | None, tuple[P6ProposalProjectionBlocker, ...]]:
    if isinstance(raw, P6ProactiveProposalGateReceipt):
        return raw, ()
    if not isinstance(raw, Mapping):
        return None, (_block(
            "invalid_quetzal_receipt_payload",
            "P6 request projection requires a QuetzalProactiveProposalGateReceipt object.",
            payload_type=type(raw).__name__,
        ),)
    try:
        return P6ProactiveProposalGateReceipt(**dict(raw)), ()
    except ValidationError as exc:
        return None, (_block(
            "invalid_quetzal_receipt_payload",
            "Quetzal proposal gate receipt failed schema validation.",
            errors=exc.errors(),
        ),)


def _projection_blockers(
    *,
    p6_id: str,
    workspace_id: str,
    study_id: str,
    proposal: ProactiveProposal,
    proposal_receipt_ref: str | None,
    gate_receipt: P6ProactiveProposalGateReceipt,
) -> tuple[P6ProposalProjectionBlocker, ...]:
    blockers: list[P6ProposalProjectionBlocker] = []
    if not workspace_id:
        blockers.append(_block("missing_workspace_id", "P6 request projection requires workspace scope."))
    if not study_id:
        blockers.append(_block("missing_study_id", "P6 request projection requires study scope for lineage."))
    if proposal.p6_id != p6_id or gate_receipt.p6_id != p6_id:
        blockers.append(_block(
            "p6_id_mismatch",
            "Proposal and Quetzal receipt must belong to the active P6 packet.",
            packet_p6_id=p6_id,
            proposal_p6_id=proposal.p6_id,
            gate_p6_id=gate_receipt.p6_id,
        ))
    if not proposal_receipt_ref:
        blockers.append(_block(
            "missing_proposal_receipt_ref",
            "P6 request projection requires the parent proposal creation receipt ref.",
        ))
    elif not str(proposal_receipt_ref).startswith("receipt://p6/trigger-evaluation/"):
        blockers.append(_block(
            "invalid_proposal_receipt_ref",
            "Parent proposal receipt must use receipt://p6/trigger-evaluation/.",
        ))
    if gate_receipt.decision != "approved":
        blockers.append(_block(
            "quetzal_approval_required",
            "P6 request projection requires an approved Quetzal proposal gate receipt.",
            gate_decision=gate_receipt.decision,
        ))
    if not gate_receipt.receipt_ref.startswith("receipt://quetzal/p6-proactive-proposal-gate/"):
        blockers.append(_block(
            "invalid_quetzal_receipt_ref",
            "Quetzal receipt must use receipt://quetzal/p6-proactive-proposal-gate/.",
        ))
    if gate_receipt.proposal_ref != proposal.proposal_ref:
        blockers.append(_block(
            "proposal_ref_mismatch",
            "Quetzal receipt proposal_ref does not match the projected proposal.",
        ))
    if gate_receipt.proposal_receipt_ref != proposal_receipt_ref:
        blockers.append(_block(
            "proposal_receipt_ref_mismatch",
            "Quetzal receipt does not approve the supplied proposal creation receipt.",
        ))
    if proposal.proposed_action_kind not in PROJECTABLE_ACTION_KINDS:
        blockers.append(_block(
            "unsupported_projection_kind",
            "P6-4 projects protocol_request or episode_request only.",
            proposed_action_kind=proposal.proposed_action_kind,
        ))
    if gate_receipt.max_allowed_action != proposal.proposed_action_kind:
        blockers.append(_block(
            "max_allowed_action_mismatch",
            "Quetzal max_allowed_action does not match the proposal action kind.",
            max_allowed_action=gate_receipt.max_allowed_action,
            proposed_action_kind=proposal.proposed_action_kind,
        ))
    if proposal.proposed_action_kind == "protocol_request":
        if not proposal.proposed_protocol_ref:
            blockers.append(_block(
                "missing_proposed_protocol_ref",
                "Protocol request projection requires proposed_protocol_ref.",
            ))
        elif not proposal.proposed_protocol_ref.startswith("protocol://"):
            blockers.append(_block(
                "invalid_proposed_protocol_ref",
                "proposed_protocol_ref must use protocol://.",
            ))
    if proposal.proposed_action_kind == "episode_request":
        if not proposal.proposed_episode_ref:
            blockers.append(_block(
                "missing_proposed_episode_ref",
                "Episode request projection requires proposed_episode_ref.",
            ))
        elif not proposal.proposed_episode_ref.startswith("episode://"):
            blockers.append(_block(
                "invalid_proposed_episode_ref",
                "proposed_episode_ref must use episode://.",
            ))
    if (
        gate_receipt.provider_job_created
        or gate_receipt.protocol_run_created
        or gate_receipt.protocol_request_created
        or gate_receipt.episode_request_created
        or gate_receipt.claim_promotion_performed
        or gate_receipt.graph_write_performed
    ):
        blockers.append(_block(
            "gate_receipt_contains_side_effects",
            "P6 projection rejects gate receipts that report prior execution or mutation.",
        ))
    return tuple(blockers)


def build_p6_proposal_projection(
    packet: Mapping[str, Any],
    *,
    proposal_payload: Any,
    proposal_receipt_ref: str | None,
    quetzal_receipt_payload: Any,
    workspace_id: str,
    study_id: str,
) -> P6ProposalProjectionResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    workspace_id = str(workspace_id or "").strip()
    study_id = str(study_id or "").strip()
    proposal, proposal_blockers = _coerce_proposal(proposal_payload)
    gate_receipt, receipt_blockers = _coerce_gate_receipt(quetzal_receipt_payload)
    blockers = [*proposal_blockers, *receipt_blockers]

    if proposal is not None and gate_receipt is not None:
        blockers.extend(_projection_blockers(
            p6_id=p6_id,
            workspace_id=workspace_id,
            study_id=study_id,
            proposal=proposal,
            proposal_receipt_ref=proposal_receipt_ref,
            gate_receipt=gate_receipt,
        ))

    projected = proposal is not None and gate_receipt is not None and not blockers
    request: P6ProjectedRequest | None = None
    request_kind: Literal["protocol_request", "episode_request", "none"] = "none"
    execution_authority: Literal["protocol_executor", "existing_episode_runtime", "none"] = "none"
    request_ref: str | None = None

    if projected:
        request_kind = proposal.proposed_action_kind  # type: ignore[assignment]
        is_protocol = request_kind == "protocol_request"
        requested_ref = proposal.proposed_protocol_ref if is_protocol else proposal.proposed_episode_ref
        execution_authority = "protocol_executor" if is_protocol else "existing_episode_runtime"
        ingress_contract: Literal["ProtocolJSONLDDocument", "LiveDebateEpisodeRequest"]
        ingress_contract = "ProtocolJSONLDDocument" if is_protocol else "LiveDebateEpisodeRequest"
        idempotency_key = _idempotency_key({
            "p6_id": p6_id,
            "workspace_id": workspace_id,
            "study_id": study_id,
            "proposal_ref": proposal.proposal_ref,
            "proposal_receipt_ref": proposal_receipt_ref,
            "quetzal_receipt_ref": gate_receipt.receipt_ref,
            "request_kind": request_kind,
            "requested_ref": requested_ref,
        })
        request_ref = _stable_ref(
            f"request://p6/{'protocol' if is_protocol else 'episode'}/",
            {"idempotency_key": idempotency_key},
        )
        request_artifact_ref = _stable_ref(
            "artifact://p6/request-projection/",
            {"request_ref": request_ref, "proposal_ref": proposal.proposal_ref},
        )
        request = P6ProjectedRequest(
            request_ref=request_ref,
            request_artifact_ref=request_artifact_ref,
            request_kind=request_kind,
            p6_id=p6_id,
            workspace_id=workspace_id,
            study_id=study_id,
            proposal_ref=proposal.proposal_ref,
            proposal_artifact_ref=proposal.artifact_ref,
            proposal_receipt_ref=str(proposal_receipt_ref),
            quetzal_receipt_ref=gate_receipt.receipt_ref,
            source_event_refs=proposal.source_event_refs,
            source_receipt_refs=proposal.source_receipt_refs,
            target_refs=proposal.target_refs,
            requested_ref=str(requested_ref),
            ingress_contract=ingress_contract,
            execution_authority=execution_authority,
            idempotency_key=idempotency_key,
        )

    reason_codes = tuple(sorted({blocker.code for blocker in blockers})) or (
        "p6_proposal_projected_without_submission",
    )
    proposal_ref = proposal.proposal_ref if proposal else "proposal://p6/proactive/invalid"
    gate_receipt_ref = gate_receipt.receipt_ref if gate_receipt else None
    projection_receipt_ref = _stable_ref(
        "receipt://p6/proposal-projection/",
        {
            "p6_id": p6_id,
            "proposal_ref": proposal_ref,
            "proposal_receipt_ref": proposal_receipt_ref,
            "quetzal_receipt_ref": gate_receipt_ref,
            "request_ref": request_ref,
            "decision": "projected" if projected else "blocked",
            "reason_codes": reason_codes,
        },
    )
    receipt = P6ProposalProjectionReceipt(
        receipt_ref=projection_receipt_ref,
        p6_id=p6_id,
        proposal_ref=proposal_ref,
        proposal_receipt_ref=proposal_receipt_ref,
        quetzal_receipt_ref=gate_receipt_ref,
        request_ref=request_ref,
        decision="projected" if projected else "blocked",
        reason_codes=reason_codes,
        request_kind=request_kind,
        execution_authority=execution_authority,
    )
    artifact_refs = (
        (request.request_artifact_ref, proposal.artifact_ref)
        if request is not None and proposal is not None
        else ()
    )
    evidence_refs = tuple(sorted({
        *(proposal.source_event_refs if proposal else ()),
        *(proposal.source_receipt_refs if proposal else ()),
        *(ref for ref in (proposal_receipt_ref, gate_receipt_ref) if ref),
    }))
    receipt_refs = tuple(ref for ref in (proposal_receipt_ref, gate_receipt_ref, receipt.receipt_ref) if ref)
    return P6ProposalProjectionResult(
        p6_id=p6_id,
        status="projected" if projected else "blocked",
        request=request,
        receipt=receipt,
        blockers=tuple(blockers),
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        receipt_refs=receipt_refs,
    )
