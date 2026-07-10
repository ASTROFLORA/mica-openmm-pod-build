from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping

from pydantic import BaseModel, Field, ValidationError

from mica.agentic.p6_proactive_proposal import ProactiveProposal, _stable_ref


P6_PROACTIVE_PROPOSAL_GATE_SCHEMA_ID = "mica.project_tolomeo.p6.proactive_proposal_gate.v1"
P6_PROACTIVE_PROPOSAL_GATE_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.proactive_proposal_gate_result.v1"
P6_PROACTIVE_PROPOSAL_GATE_NAME = "quetzal.proactive_proposal_gate"

SUPPORTED_ACTION_KINDS = ("protocol_request", "episode_request", "review_request")


class P6ProactiveProposalGateBlocker(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class P6ProactiveProposalGateReceipt(BaseModel):
    schema_id: str = P6_PROACTIVE_PROPOSAL_GATE_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["QuetzalProactiveProposalGateReceipt"] = "QuetzalProactiveProposalGateReceipt"
    gate_name: Literal["quetzal.proactive_proposal_gate"] = P6_PROACTIVE_PROPOSAL_GATE_NAME
    p6_id: str
    proposal_ref: str
    decision: Literal["approved", "rejected"]
    reason_codes: tuple[str, ...]
    max_allowed_action: Literal["none", "protocol_request", "episode_request", "review_request"]
    approval_required: bool = True
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...] = ()
    proposal_receipt_ref: str | None = None
    evaluated_policies: tuple[str, ...] = (
        "proposal_ref_required",
        "proposal_receipt_required",
        "durable_source_receipts_required",
        "target_refs_required",
        "requires_approval_true",
        "proposal_only_no_direct_execution",
        "no_protocol_request_creation_in_gate",
    )
    provider_job_created: bool = False
    protocol_run_created: bool = False
    protocol_request_created: bool = False
    episode_request_created: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6ProactiveProposalGateResult(BaseModel):
    schema_id: str = P6_PROACTIVE_PROPOSAL_GATE_RESULT_SCHEMA_ID
    p6_id: str
    status: Literal["approved", "rejected"]
    proposal: ProactiveProposal | None = None
    receipt: P6ProactiveProposalGateReceipt
    blockers: tuple[P6ProactiveProposalGateBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    provider_job_created: bool = False
    protocol_run_created: bool = False
    protocol_request_created: bool = False
    episode_request_created: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False


def _block(code: str, message: str, **details: Any) -> P6ProactiveProposalGateBlocker:
    return P6ProactiveProposalGateBlocker(
        code=code,
        message=message,
        details={key: value for key, value in details.items() if value is not None},
    )


def _coerce_proposal(raw: Any) -> tuple[ProactiveProposal | None, tuple[P6ProactiveProposalGateBlocker, ...]]:
    if isinstance(raw, ProactiveProposal):
        return raw, ()
    if not isinstance(raw, Mapping):
        return None, (_block(
            "invalid_proposal_payload",
            "quetzal.proactive_proposal_gate requires a ProactiveProposal object.",
            payload_type=type(raw).__name__,
        ),)
    try:
        return ProactiveProposal(**dict(raw)), ()
    except ValidationError as exc:
        return None, (_block(
            "invalid_proposal_payload",
            "ProactiveProposal failed schema validation before Quetzal gate evaluation.",
            errors=exc.errors(),
        ),)


def _proposal_blockers(
    proposal: ProactiveProposal,
    *,
    proposal_receipt_ref: str | None,
) -> tuple[P6ProactiveProposalGateBlocker, ...]:
    blockers: list[P6ProactiveProposalGateBlocker] = []
    if not proposal.proposal_ref.startswith("proposal://p6/proactive/"):
        blockers.append(_block("invalid_proposal_ref", "P6 proposal_ref must use proposal://p6/proactive/."))
    if not proposal.artifact_ref.startswith("artifact://p6/proactive-proposal/"):
        blockers.append(_block("invalid_proposal_artifact_ref", "P6 proposal artifact_ref must use artifact://p6/proactive-proposal/."))
    if not proposal.source_receipt_refs:
        blockers.append(_block("missing_source_receipt_refs", "P6 proposal gate requires durable source receipt refs."))
    elif any(not str(ref).startswith("receipt://") for ref in proposal.source_receipt_refs):
        blockers.append(_block("invalid_source_receipt_ref", "P6 proposal source receipts must use receipt:// refs."))
    if not proposal.target_refs:
        blockers.append(_block("missing_target_refs", "P6 proposal gate requires target refs."))
    if not proposal.requires_approval:
        blockers.append(_block("approval_not_required_on_proposal", "P6 proposals must require Quetzal approval before projection."))
    if proposal.execution_status != "proposal_only" or proposal.direct_execution_allowed:
        blockers.append(_block("direct_execution_intent_blocked", "P6 proposal gate only accepts proposal_only objects."))
    if proposal.provider_job_created or proposal.protocol_run_created:
        blockers.append(_block("execution_already_started", "P6 proposal gate rejects proposals that already created provider jobs or protocol runs."))
    if proposal.claim_promotion_performed or proposal.graph_write_performed:
        blockers.append(_block("knowledge_side_effect_already_performed", "P6 proposal gate rejects proposals that promoted claims or wrote graph state."))
    if proposal.proposed_action_kind not in SUPPORTED_ACTION_KINDS:
        blockers.append(_block(
            "unsupported_proposed_action_kind",
            "P6 proposal action kind is not supported by this Quetzal gate.",
            proposed_action_kind=proposal.proposed_action_kind,
            supported_action_kinds=list(SUPPORTED_ACTION_KINDS),
        ))
    if not proposal_receipt_ref:
        blockers.append(_block("missing_proposal_receipt_ref", "P6 proposal gate requires the proposal creation receipt ref."))
    elif not str(proposal_receipt_ref).startswith("receipt://p6/trigger-evaluation/"):
        blockers.append(_block("invalid_proposal_receipt_ref", "P6 proposal receipt must use receipt://p6/trigger-evaluation/."))
    return tuple(blockers)


def build_p6_proactive_proposal_gate_result(
    packet: Mapping[str, Any],
    *,
    proposal_payload: Any,
    proposal_receipt_ref: str | None = None,
) -> P6ProactiveProposalGateResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    proposal, coercion_blockers = _coerce_proposal(proposal_payload)
    blockers = list(coercion_blockers)
    if proposal is not None:
        blockers.extend(_proposal_blockers(proposal, proposal_receipt_ref=proposal_receipt_ref))

    approved = proposal is not None and not blockers
    reason_codes = tuple(sorted({blocker.code for blocker in blockers})) or ("p6_proactive_proposal_approved",)
    proposal_ref = proposal.proposal_ref if proposal else "proposal://p6/proactive/invalid"
    receipt_ref = _stable_ref(
        "receipt://quetzal/p6-proactive-proposal-gate/",
        {
            "p6_id": p6_id,
            "proposal_ref": proposal_ref,
            "proposal_receipt_ref": proposal_receipt_ref,
            "decision": "approved" if approved else "rejected",
            "reason_codes": reason_codes,
        },
    )
    max_allowed_action: Literal["none", "protocol_request", "episode_request", "review_request"]
    max_allowed_action = proposal.proposed_action_kind if approved else "none"  # type: ignore[assignment]
    receipt = P6ProactiveProposalGateReceipt(
        receipt_ref=receipt_ref,
        p6_id=p6_id,
        proposal_ref=proposal_ref,
        decision="approved" if approved else "rejected",
        reason_codes=reason_codes,
        max_allowed_action=max_allowed_action,
        source_event_refs=proposal.source_event_refs if proposal else (),
        source_receipt_refs=proposal.source_receipt_refs if proposal else (),
        proposal_receipt_ref=proposal_receipt_ref,
    )
    return P6ProactiveProposalGateResult(
        p6_id=p6_id,
        status="approved" if approved else "rejected",
        proposal=proposal,
        receipt=receipt,
        blockers=tuple(blockers),
        artifact_refs=(proposal.artifact_ref,) if proposal and approved else (),
        evidence_refs=tuple(sorted({*(proposal.source_receipt_refs if proposal else ()), *(proposal.source_event_refs if proposal else ())})),
        receipt_refs=tuple(ref for ref in (receipt.receipt_ref, proposal_receipt_ref) if ref),
    )
