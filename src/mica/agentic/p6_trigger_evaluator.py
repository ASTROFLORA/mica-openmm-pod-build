from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Literal, Mapping

from pydantic import BaseModel, Field

from mica.agentic.p6_proactive_proposal import (
    P6ProposalBlocker,
    P6TriggerEvaluationReceipt,
    ProactiveProposal,
    SUPPORTED_TRIGGER_FAMILIES,
    _stable_ref,
    build_p6_proactive_proposals,
)


P6_TRIGGER_INPUT_SCHEMA_ID = "mica.project_tolomeo.p6.trigger_input.v1"
P6_TRIGGER_EVALUATOR_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.p6.trigger_evaluator_receipt.v1"
P6_TRIGGER_EVALUATION_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.trigger_evaluation_result.v1"

DURABLE_SIGNAL_PREFIXES = (
    "event://",
    "receipt://",
    "mudo://",
    "artifact://",
    "evidence://",
    "packet://",
)


class P6TriggerInput(BaseModel):
    schema_id: str = P6_TRIGGER_INPUT_SCHEMA_ID
    trigger_family: str
    signal_ref: str
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...] = ()
    target_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_confidence_for_proposal: float = Field(default=0.5, ge=0.0, le=1.0)
    proposed_protocol_ref: str | None = None
    proposed_episode_ref: str | None = None
    proposed_action_kind: str = "protocol_request"
    quetzal_policy_ref: str = "policy://quetzal/p6/proactive-proposal-gate"
    direct_execution_requested: bool = False
    provider_job_requested: bool = False
    protocol_run_requested: bool = False
    claim_promotion_requested: bool = False
    graph_write_requested: bool = False
    raw_payload_embedded: bool = False
    metadata_refs: tuple[str, ...] = ()


class P6TriggerEvaluatorReceipt(BaseModel):
    schema_id: str = P6_TRIGGER_EVALUATOR_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["P6TriggerEvaluatorReceipt"] = "P6TriggerEvaluatorReceipt"
    p6_id: str
    trigger_family: str
    decision: Literal["proposal_created", "noop", "blocked", "duplicate_suppressed"]
    reason_codes: tuple[str, ...]
    signal_ref: str
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...] = ()
    proposal_ref: str | None = None
    proposal_receipt_ref: str | None = None
    evaluated_policies: tuple[str, ...] = (
        "durable_signal_ref_required",
        "durable_source_receipt_required",
        "known_trigger_family_required",
        "proposal_only_no_direct_execution",
        "weak_signals_noop_until_stronger_evidence",
    )
    provider_job_created: bool = False
    protocol_run_created: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6TriggerEvaluationResult(BaseModel):
    schema_id: str = P6_TRIGGER_EVALUATION_RESULT_SCHEMA_ID
    p6_id: str
    status: Literal["proposal_ready", "noop", "blocked", "partially_blocked", "duplicate_suppressed"]
    trigger_evaluator_status: Literal["ready", "noop", "blocked", "partially_blocked", "duplicate_suppressed"]
    evaluator_receipts: tuple[P6TriggerEvaluatorReceipt, ...]
    proposal_receipts: tuple[P6TriggerEvaluationReceipt, ...] = ()
    proposals: tuple[ProactiveProposal, ...] = ()
    blockers: tuple[P6ProposalBlocker, ...] = ()
    proposal_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    proposal_count: int = 0
    noop_count: int = 0
    blocked_count: int = 0
    duplicate_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    execution_started: bool = False


def _as_trigger_inputs(raw: Any) -> tuple[P6TriggerInput, ...]:
    values: Iterable[Any]
    if isinstance(raw, Mapping):
        values = (raw,)
    elif isinstance(raw, list):
        values = raw
    elif isinstance(raw, tuple):
        values = raw
    else:
        values = ()

    inputs: list[P6TriggerInput] = []
    for value in values:
        if isinstance(value, P6TriggerInput):
            inputs.append(value)
        elif isinstance(value, Mapping):
            inputs.append(P6TriggerInput(**dict(value)))
        else:
            raise ValueError("P6 trigger inputs must be objects")
    return tuple(inputs)


def _signal_blockers(item: P6TriggerInput) -> tuple[P6ProposalBlocker, ...]:
    blockers: list[P6ProposalBlocker] = []
    if item.trigger_family not in SUPPORTED_TRIGGER_FAMILIES:
        blockers.append(P6ProposalBlocker(
            code="unsupported_trigger_family",
            message="P6 trigger evaluator only accepts registered trigger families.",
            details={"trigger_family": item.trigger_family},
        ))
    if not item.signal_ref or not item.signal_ref.startswith(DURABLE_SIGNAL_PREFIXES):
        blockers.append(P6ProposalBlocker(
            code="invalid_durable_signal_ref",
            message="P6 trigger evaluator requires a durable signal ref.",
            details={"signal_ref": item.signal_ref},
        ))
    if not item.source_receipt_refs:
        blockers.append(P6ProposalBlocker(
            code="unreceipted_trigger_input",
            message="P6 trigger evaluator rejects signals without source receipt refs.",
        ))
    elif any(not str(ref).startswith("receipt://") for ref in item.source_receipt_refs):
        blockers.append(P6ProposalBlocker(
            code="invalid_source_receipt_ref",
            message="P6 trigger evaluator source receipts must use receipt:// refs.",
            details={"source_receipt_refs": list(item.source_receipt_refs)},
        ))
    if not item.target_refs:
        blockers.append(P6ProposalBlocker(
            code="missing_target_refs",
            message="P6 trigger evaluator requires target refs before proposing work.",
        ))
    if (
        item.direct_execution_requested
        or item.provider_job_requested
        or item.protocol_run_requested
        or item.claim_promotion_requested
        or item.graph_write_requested
    ):
        blockers.append(P6ProposalBlocker(
            code="direct_execution_intent_blocked",
            message="P6 trigger evaluation cannot execute, dispatch providers, promote claims, or write graph state.",
            details={
                "direct_execution_requested": item.direct_execution_requested,
                "provider_job_requested": item.provider_job_requested,
                "protocol_run_requested": item.protocol_run_requested,
                "claim_promotion_requested": item.claim_promotion_requested,
                "graph_write_requested": item.graph_write_requested,
            },
        ))
    if item.raw_payload_embedded:
        blockers.append(P6ProposalBlocker(
            code="raw_payload_embedded",
            message="P6 trigger inputs must be refs-only and cannot embed raw payloads.",
        ))
    return tuple(blockers)


def _evaluator_receipt(
    *,
    p6_id: str,
    item: P6TriggerInput,
    decision: Literal["proposal_created", "noop", "blocked", "duplicate_suppressed"],
    reason_codes: tuple[str, ...],
    proposal_ref: str | None = None,
    proposal_receipt_ref: str | None = None,
) -> P6TriggerEvaluatorReceipt:
    receipt_ref = _stable_ref(
        "receipt://p6/trigger-evaluator/",
        {
            "p6_id": p6_id,
            "trigger_family": item.trigger_family,
            "signal_ref": item.signal_ref,
            "source_receipt_refs": sorted(item.source_receipt_refs),
            "decision": decision,
            "reason_codes": reason_codes,
            "proposal_ref": proposal_ref,
        },
    )
    return P6TriggerEvaluatorReceipt(
        receipt_ref=receipt_ref,
        p6_id=p6_id,
        trigger_family=item.trigger_family,
        decision=decision,
        reason_codes=reason_codes,
        signal_ref=item.signal_ref,
        source_event_refs=item.source_event_refs,
        source_receipt_refs=item.source_receipt_refs,
        proposal_ref=proposal_ref,
        proposal_receipt_ref=proposal_receipt_ref,
    )


def _proposal_payload_from_trigger(item: P6TriggerInput) -> Dict[str, Any]:
    return {
        "trigger_family": item.trigger_family,
        "source_event_refs": item.source_event_refs,
        "source_receipt_refs": item.source_receipt_refs,
        "target_refs": item.target_refs,
        "reason_codes": item.reason_codes or ("p6_trigger_signal_requires_review",),
        "priority": item.priority,
        "proposed_protocol_ref": item.proposed_protocol_ref,
        "proposed_episode_ref": item.proposed_episode_ref,
        "proposed_action_kind": item.proposed_action_kind,
        "quetzal_policy_ref": item.quetzal_policy_ref,
        "direct_execution_requested": False,
        "provider_job_requested": False,
        "protocol_run_requested": False,
        "claim_promotion_requested": False,
        "graph_write_requested": False,
        "raw_payload_embedded": False,
        "metadata_refs": item.metadata_refs,
    }


def evaluate_p6_triggers(
    packet: Mapping[str, Any],
    *,
    trigger_payloads: Any,
) -> P6TriggerEvaluationResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    inputs = _as_trigger_inputs(trigger_payloads)
    blockers: list[P6ProposalBlocker] = []
    evaluator_receipts: list[P6TriggerEvaluatorReceipt] = []
    proposal_receipts: list[P6TriggerEvaluationReceipt] = []
    proposals: list[ProactiveProposal] = []

    if not inputs:
        blocker = P6ProposalBlocker(
            code="missing_trigger_payloads",
            message="P6-2 requires at least one trigger input.",
        )
        blockers.append(blocker)

    for item in inputs:
        item_blockers = tuple(_signal_blockers(item))
        if item_blockers:
            reason_codes = tuple(sorted({blocker.code for blocker in item_blockers}))
            blockers.extend(item_blockers)
            evaluator_receipts.append(_evaluator_receipt(
                p6_id=p6_id,
                item=item,
                decision="blocked",
                reason_codes=reason_codes,
            ))
            continue

        if item.confidence < item.minimum_confidence_for_proposal:
            evaluator_receipts.append(_evaluator_receipt(
                p6_id=p6_id,
                item=item,
                decision="noop",
                reason_codes=("signal_below_proposal_threshold",),
            ))
            continue

        proposal_result = build_p6_proactive_proposals(
            packet,
            proposal_payloads=_proposal_payload_from_trigger(item),
        )
        proposal_receipts.extend(proposal_result.receipts)
        proposals.extend(proposal_result.proposals)
        blockers.extend(proposal_result.blockers)
        proposal_receipt = proposal_result.receipts[0] if proposal_result.receipts else None
        proposal = proposal_result.proposals[0] if proposal_result.proposals else None
        if proposal_result.status == "ready":
            evaluator_receipts.append(_evaluator_receipt(
                p6_id=p6_id,
                item=item,
                decision="proposal_created",
                reason_codes=proposal_receipt.reason_codes if proposal_receipt else ("p6_proactive_proposal_ready",),
                proposal_ref=proposal.proposal_ref if proposal else None,
                proposal_receipt_ref=proposal_receipt.receipt_ref if proposal_receipt else None,
            ))
        elif proposal_result.status == "duplicate_suppressed":
            evaluator_receipts.append(_evaluator_receipt(
                p6_id=p6_id,
                item=item,
                decision="duplicate_suppressed",
                reason_codes=("duplicate_proposal_suppressed",),
                proposal_receipt_ref=proposal_receipt.receipt_ref if proposal_receipt else None,
            ))
        else:
            evaluator_receipts.append(_evaluator_receipt(
                p6_id=p6_id,
                item=item,
                decision="blocked",
                reason_codes=tuple(sorted({blocker.code for blocker in proposal_result.blockers})) or ("proposal_contract_blocked",),
                proposal_receipt_ref=proposal_receipt.receipt_ref if proposal_receipt else None,
            ))

    proposal_count = len(proposals)
    blocked_count = sum(receipt.decision == "blocked" for receipt in evaluator_receipts)
    noop_count = sum(receipt.decision == "noop" for receipt in evaluator_receipts)
    duplicate_count = sum(receipt.decision == "duplicate_suppressed" for receipt in evaluator_receipts)

    if proposal_count and not blocked_count and not noop_count and not duplicate_count:
        status: Literal["proposal_ready", "noop", "blocked", "partially_blocked", "duplicate_suppressed"] = "proposal_ready"
    elif proposal_count:
        status = "partially_blocked"
    elif noop_count and not blocked_count and not duplicate_count:
        status = "noop"
    elif duplicate_count and not blocked_count:
        status = "duplicate_suppressed"
    else:
        status = "blocked"

    return P6TriggerEvaluationResult(
        p6_id=p6_id,
        status=status,
        trigger_evaluator_status={
            "proposal_ready": "ready",
            "noop": "noop",
            "blocked": "blocked",
            "partially_blocked": "partially_blocked",
            "duplicate_suppressed": "duplicate_suppressed",
        }[status],
        evaluator_receipts=tuple(evaluator_receipts),
        proposal_receipts=tuple(proposal_receipts),
        proposals=tuple(proposals),
        blockers=tuple(blockers),
        proposal_refs=tuple(proposal.proposal_ref for proposal in proposals),
        artifact_refs=tuple(proposal.artifact_ref for proposal in proposals),
        receipt_refs=tuple(receipt.receipt_ref for receipt in evaluator_receipts)
        + tuple(receipt.receipt_ref for receipt in proposal_receipts),
        proposal_count=proposal_count,
        noop_count=noop_count,
        blocked_count=blocked_count,
        duplicate_count=duplicate_count,
    )
