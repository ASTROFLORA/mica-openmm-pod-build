from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Literal, Mapping

from pydantic import BaseModel, Field, model_validator


P6_PROACTIVE_PROPOSAL_SCHEMA_ID = "mica.project_tolomeo.p6.proactive_proposal.v1"
P6_TRIGGER_EVALUATION_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.p6.trigger_evaluation_receipt.v1"
P6_PROACTIVE_PROPOSAL_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.proactive_proposal_result.v1"

SUPPORTED_TRIGGER_FAMILIES = (
    "citation_cascade_update",
    "contradiction_surface",
    "evidence_gap_report",
    "genesis_candidate_uncertainty",
    "mudo_stale_asset",
    "quetzal_repeated_block",
)

RAW_PAYLOAD_KEYS = {
    "raw_payload",
    "raw_event",
    "raw_output",
    "provider_payload",
    "raw_provider_payload",
    "llm_transcript",
}


class P6ProposalBlocker(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class P6ProactiveProposalInput(BaseModel):
    trigger_family: str
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...] = ()
    target_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    proposed_protocol_ref: str | None = None
    proposed_episode_ref: str | None = None
    proposed_action_kind: str = "protocol_request"
    quetzal_policy_ref: str = "policy://quetzal/p6/proactive-proposal-gate"
    requested_execution_mode: str = "proposal_only"
    direct_execution_requested: bool = False
    provider_job_requested: bool = False
    protocol_run_requested: bool = False
    claim_promotion_requested: bool = False
    graph_write_requested: bool = False
    raw_payload_embedded: bool = False
    metadata_refs: tuple[str, ...] = ()


class ProactiveProposal(BaseModel):
    schema_id: str = P6_PROACTIVE_PROPOSAL_SCHEMA_ID
    proposal_ref: str
    artifact_ref: str
    p6_id: str
    trigger_family: str
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    proposed_protocol_ref: str | None = None
    proposed_episode_ref: str | None = None
    proposed_action_kind: str = "protocol_request"
    requires_approval: bool = True
    quetzal_policy_ref: str
    idempotency_key: str
    execution_status: Literal["proposal_only"] = "proposal_only"
    direct_execution_allowed: bool = False
    provider_job_created: bool = False
    protocol_run_created: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def _validate_refs_only_proposal(self) -> "ProactiveProposal":
        if not self.source_receipt_refs:
            raise ValueError("ProactiveProposal requires durable source receipt refs")
        if not self.target_refs:
            raise ValueError("ProactiveProposal requires target refs")
        if self.direct_execution_allowed:
            raise ValueError("ProactiveProposal cannot allow direct execution")
        if self.provider_job_created or self.protocol_run_created:
            raise ValueError("ProactiveProposal cannot create provider jobs or protocol runs")
        if self.claim_promotion_performed or self.graph_write_performed:
            raise ValueError("ProactiveProposal cannot promote claims or write graph state")
        return self


class P6TriggerEvaluationReceipt(BaseModel):
    schema_id: str = P6_TRIGGER_EVALUATION_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["P6TriggerEvaluationReceipt"] = "P6TriggerEvaluationReceipt"
    p6_id: str
    trigger_family: str
    decision: Literal["proposal_created", "blocked", "duplicate_suppressed"]
    reason_codes: tuple[str, ...]
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...] = ()
    proposal_ref: str | None = None
    idempotency_key: str
    evaluated_policies: tuple[str, ...] = (
        "durable_source_receipt_required",
        "target_refs_required",
        "proposal_only_no_direct_execution",
        "quetzal_approval_required_before_execution",
    )
    provider_job_created: bool = False
    protocol_run_created: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6ProactiveProposalResult(BaseModel):
    schema_id: str = P6_PROACTIVE_PROPOSAL_RESULT_SCHEMA_ID
    p6_id: str
    status: Literal["ready", "blocked", "duplicate_suppressed", "partially_blocked"]
    proposal_contract_status: Literal["ready", "blocked", "duplicate_suppressed", "partially_blocked"]
    proposals: tuple[ProactiveProposal, ...]
    receipts: tuple[P6TriggerEvaluationReceipt, ...]
    blockers: tuple[P6ProposalBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    idempotency_keys: tuple[str, ...] = ()
    proposal_count: int = 0
    blocked_count: int = 0
    duplicate_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    execution_started: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]


def _stable_ref(prefix: str, value: Any) -> str:
    return f"{prefix}{_stable_digest(value)}"


def make_p6_proactive_proposal_idempotency_key(*, p6_id: str, payload: Mapping[str, Any]) -> str:
    canonical = {
        "p6_id": p6_id,
        "trigger_family": payload.get("trigger_family"),
        "source_event_refs": sorted(str(ref) for ref in payload.get("source_event_refs") or ()),
        "source_receipt_refs": sorted(str(ref) for ref in payload.get("source_receipt_refs") or ()),
        "target_refs": sorted(str(ref) for ref in payload.get("target_refs") or ()),
        "proposed_protocol_ref": payload.get("proposed_protocol_ref"),
        "proposed_episode_ref": payload.get("proposed_episode_ref"),
        "proposed_action_kind": payload.get("proposed_action_kind") or "protocol_request",
    }
    return hashlib.sha256(_stable_json(canonical).encode("utf-8")).hexdigest()


def _contains_raw_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in RAW_PAYLOAD_KEYS:
                return True
            if _contains_raw_payload(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_raw_payload(item) for item in value)
    return False


def _as_inputs(raw: Any) -> tuple[P6ProactiveProposalInput, ...]:
    values: Iterable[Any]
    if isinstance(raw, Mapping):
        values = (raw,)
    elif isinstance(raw, list):
        values = raw
    elif isinstance(raw, tuple):
        values = raw
    else:
        values = ()

    inputs: list[P6ProactiveProposalInput] = []
    for value in values:
        if isinstance(value, P6ProactiveProposalInput):
            inputs.append(value)
        elif isinstance(value, Mapping):
            inputs.append(P6ProactiveProposalInput(**dict(value)))
        else:
            raise ValueError("P6 proactive proposal payloads must be objects")
    return tuple(inputs)


def _input_blockers(item: P6ProactiveProposalInput) -> tuple[P6ProposalBlocker, ...]:
    blockers: list[P6ProposalBlocker] = []
    if item.trigger_family not in SUPPORTED_TRIGGER_FAMILIES:
        blockers.append(P6ProposalBlocker(
            code="unsupported_trigger_family",
            message="P6 trigger family is not registered for proactive proposal creation.",
            details={"trigger_family": item.trigger_family},
        ))
    if not item.source_receipt_refs:
        blockers.append(P6ProposalBlocker(
            code="missing_source_receipt_refs",
            message="P6 proactive proposals require at least one durable source receipt ref.",
        ))
    elif any(not str(ref).startswith("receipt://") for ref in item.source_receipt_refs):
        blockers.append(P6ProposalBlocker(
            code="invalid_source_receipt_ref",
            message="P6 proactive proposal source receipts must use receipt:// refs.",
            details={"source_receipt_refs": list(item.source_receipt_refs)},
        ))
    if not item.target_refs:
        blockers.append(P6ProposalBlocker(
            code="missing_target_refs",
            message="P6 proactive proposals require target refs.",
        ))
    if (
        item.direct_execution_requested
        or item.provider_job_requested
        or item.protocol_run_requested
        or item.claim_promotion_requested
        or item.graph_write_requested
        or item.requested_execution_mode != "proposal_only"
    ):
        blockers.append(P6ProposalBlocker(
            code="direct_execution_intent_blocked",
            message="P6-1 only permits proposal-only contracts; execution intent must be gated in later slices.",
            details={
                "requested_execution_mode": item.requested_execution_mode,
                "direct_execution_requested": item.direct_execution_requested,
                "provider_job_requested": item.provider_job_requested,
                "protocol_run_requested": item.protocol_run_requested,
                "claim_promotion_requested": item.claim_promotion_requested,
                "graph_write_requested": item.graph_write_requested,
            },
        ))
    if item.raw_payload_embedded or _contains_raw_payload(item.model_dump(mode="json")):
        blockers.append(P6ProposalBlocker(
            code="raw_payload_embedded",
            message="P6 proactive proposals are refs-only and cannot embed raw provider/event payloads.",
        ))
    return tuple(blockers)


def build_p6_proactive_proposals(
    packet: Mapping[str, Any],
    *,
    proposal_payloads: Any,
) -> P6ProactiveProposalResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    inputs = _as_inputs(proposal_payloads)
    result_blockers: list[P6ProposalBlocker] = []
    proposals: list[ProactiveProposal] = []
    receipts: list[P6TriggerEvaluationReceipt] = []
    seen_keys: set[str] = set()

    if not inputs:
        result_blockers.append(P6ProposalBlocker(
            code="missing_proposal_payloads",
            message="P6-1 requires at least one proactive proposal payload.",
        ))

    for item in inputs:
        payload = item.model_dump(mode="json")
        idempotency_key = make_p6_proactive_proposal_idempotency_key(p6_id=p6_id, payload=payload)
        blockers = list(_input_blockers(item))
        duplicate = idempotency_key in seen_keys
        seen_keys.add(idempotency_key)
        reason_codes: tuple[str, ...]
        proposal_ref: str | None = None

        if duplicate:
            decision: Literal["proposal_created", "blocked", "duplicate_suppressed"] = "duplicate_suppressed"
            reason_codes = ("duplicate_proposal_suppressed",)
        elif blockers:
            decision = "blocked"
            reason_codes = tuple(sorted({blocker.code for blocker in blockers}))
            result_blockers.extend(blockers)
        else:
            decision = "proposal_created"
            reason_codes = item.reason_codes or ("p6_proactive_proposal_ready",)
            proposal_ref = _stable_ref(
                "proposal://p6/proactive/",
                {"p6_id": p6_id, "idempotency_key": idempotency_key},
            )
            artifact_ref = _stable_ref(
                "artifact://p6/proactive-proposal/",
                {"proposal_ref": proposal_ref, "target_refs": item.target_refs},
            )
            receipt_ref_for_proposal = _stable_ref(
                "receipt://p6/trigger-evaluation/",
                {"proposal_ref": proposal_ref, "idempotency_key": idempotency_key, "decision": decision},
            )
            proposals.append(ProactiveProposal(
                proposal_ref=proposal_ref,
                artifact_ref=artifact_ref,
                p6_id=p6_id,
                trigger_family=item.trigger_family,
                source_event_refs=item.source_event_refs,
                source_receipt_refs=item.source_receipt_refs,
                target_refs=item.target_refs,
                reason_codes=reason_codes,
                priority=item.priority,
                proposed_protocol_ref=item.proposed_protocol_ref,
                proposed_episode_ref=item.proposed_episode_ref,
                proposed_action_kind=item.proposed_action_kind,
                quetzal_policy_ref=item.quetzal_policy_ref,
                idempotency_key=idempotency_key,
            ))

        receipt_ref = _stable_ref(
            "receipt://p6/trigger-evaluation/",
            {
                "p6_id": p6_id,
                "trigger_family": item.trigger_family,
                "idempotency_key": idempotency_key,
                "decision": decision,
                "reason_codes": reason_codes,
            },
        )
        receipts.append(P6TriggerEvaluationReceipt(
            receipt_ref=receipt_ref,
            p6_id=p6_id,
            trigger_family=item.trigger_family,
            decision=decision,
            reason_codes=reason_codes,
            source_event_refs=item.source_event_refs,
            source_receipt_refs=item.source_receipt_refs,
            proposal_ref=proposal_ref,
            idempotency_key=idempotency_key,
        ))

    proposal_count = len(proposals)
    blocked_count = sum(receipt.decision == "blocked" for receipt in receipts)
    duplicate_count = sum(receipt.decision == "duplicate_suppressed" for receipt in receipts)
    if proposal_count and not blocked_count and not duplicate_count:
        status: Literal["ready", "blocked", "duplicate_suppressed", "partially_blocked"] = "ready"
    elif proposal_count:
        status = "partially_blocked"
    elif duplicate_count and not blocked_count:
        status = "duplicate_suppressed"
    else:
        status = "blocked"

    return P6ProactiveProposalResult(
        p6_id=p6_id,
        status=status,
        proposal_contract_status=status,
        proposals=tuple(proposals),
        receipts=tuple(receipts),
        blockers=tuple(result_blockers),
        artifact_refs=tuple(proposal.artifact_ref for proposal in proposals),
        receipt_refs=tuple(receipt.receipt_ref for receipt in receipts),
        idempotency_keys=tuple(receipt.idempotency_key for receipt in receipts),
        proposal_count=proposal_count,
        blocked_count=blocked_count,
        duplicate_count=duplicate_count,
    )
