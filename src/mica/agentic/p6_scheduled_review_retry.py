from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Literal, Mapping

from pydantic import BaseModel, Field, model_validator

from mica.agentic.p6_proactive_proposal import P6ProposalBlocker, ProactiveProposal
from mica.agentic.p6_trigger_evaluator import P6TriggerEvaluationResult, evaluate_p6_triggers


P6_REVIEW_REQUEST_SCHEMA_ID = "mica.project_tolomeo.p6.scheduled_review_request.v1"
P6_REVIEW_OUTBOX_SCHEMA_ID = "mica.project_tolomeo.p6.scheduled_review_outbox.v1"
P6_REVIEW_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.p6.scheduled_review_receipt.v1"
P6_REVIEW_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.scheduled_review_result.v1"

REVIEW_TRIGGER_FAMILY = {
    "scheduled_peer_review": "evidence_gap_report",
    "stale_claim_review": "mudo_stale_asset",
}
REVIEW_PROTOCOL_REF = {
    "scheduled_peer_review": "protocol://p6/scheduled-peer-review",
    "stale_claim_review": "protocol://p6/stale-claim-review",
}
_DURABLE_SIGNAL_PREFIXES = ("artifact://", "event://", "mudo://", "receipt://", "claim://")


class P6ReviewBudget(BaseModel):
    currency: Literal["USD"] = "USD"
    status: Literal["known", "unknown"]
    max_total_cost_usd: Decimal | None = Field(default=None, ge=0)
    estimate_source: str | None = None

    @model_validator(mode="after")
    def _known_budget_is_explicit(self) -> "P6ReviewBudget":
        if self.status == "known":
            if self.max_total_cost_usd is None:
                raise ValueError("known review budget requires max_total_cost_usd")
            if not str(self.estimate_source or "").strip():
                raise ValueError("known review budget requires estimate_source")
        return self


class P6ReviewRetryPolicy(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=8)
    max_reviews_per_run: int = Field(default=25, ge=1, le=500)
    base_delay_seconds: int = Field(default=60, ge=1, le=86400)
    max_delay_seconds: int = Field(default=3600, ge=1, le=604800)

    @model_validator(mode="after")
    def _delay_is_bounded(self) -> "P6ReviewRetryPolicy":
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        return self


class P6ScheduledReviewRequest(BaseModel):
    schema_id: str = P6_REVIEW_REQUEST_SCHEMA_ID
    review_kind: Literal["scheduled_peer_review", "stale_claim_review"]
    signal_ref: str
    source_event_refs: tuple[str, ...] = ()
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    reason_codes: tuple[str, ...] = ()
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    scheduled_for: str | None = None
    mode: Literal["initial", "retry", "backfill"] = "initial"
    expensive_review: bool = True
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    proposed_protocol_ref: str | None = None
    metadata_refs: tuple[str, ...] = ()
    direct_execution_requested: bool = False
    provider_job_requested: bool = False
    protocol_run_requested: bool = False
    claim_promotion_requested: bool = False
    graph_write_requested: bool = False
    raw_payload_embedded: bool = False


class P6ScheduledReviewPriorReceipt(BaseModel):
    receipt_ref: str
    idempotency_key: str
    attempt_count: int = Field(ge=0)
    decision: Literal["proposal_projected", "blocked", "duplicate_suppressed"]
    retryable: bool = False

    @model_validator(mode="after")
    def _validate_prior_receipt_identity(self) -> "P6ScheduledReviewPriorReceipt":
        if not self.receipt_ref.startswith("receipt://"):
            raise ValueError("prior scheduled-review receipt requires a receipt:// ref")
        if not self.idempotency_key.strip():
            raise ValueError("prior scheduled-review receipt requires an idempotency key")
        return self


class P6ScheduledReviewOutboxEntry(BaseModel):
    schema_id: str = P6_REVIEW_OUTBOX_SCHEMA_ID
    outbox_ref: str
    idempotency_key: str
    attempt_key: str
    review_kind: Literal["scheduled_peer_review", "stale_claim_review"]
    mode: Literal["initial", "retry", "backfill"]
    attempt_count: int
    signal_ref: str
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    proposal_ref: str
    proposal_receipt_ref: str
    retry_policy: P6ReviewRetryPolicy
    state: Literal["projected_not_enqueued"] = "projected_not_enqueued"
    submission_authority: Literal["command_kernel_protocol_runtime"] = "command_kernel_protocol_runtime"
    scheduler_authority: Literal["retry_backfill_only"] = "retry_backfill_only"
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    dispatched: bool = False


class P6ScheduledReviewReceipt(BaseModel):
    schema_id: str = P6_REVIEW_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["P6ScheduledReviewReceipt"] = "P6ScheduledReviewReceipt"
    p6_id: str
    review_kind: str
    signal_ref: str
    decision: Literal["proposal_projected", "noop", "blocked", "duplicate_suppressed"]
    reason_codes: tuple[str, ...]
    idempotency_key: str
    attempt_key: str
    attempt_count: int
    proposal_ref: str | None = None
    proposal_receipt_ref: str | None = None
    outbox_ref: str | None = None
    retryable: bool = False
    evaluated_policies: tuple[str, ...] = (
        "receipted_inputs_only",
        "scheduler_retry_backfill_only",
        "proposal_only_no_dispatch",
        "expensive_review_budget_required",
        "bounded_attempts_and_run_size",
    )
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6ScheduledReviewResult(BaseModel):
    schema_id: str = P6_REVIEW_RESULT_SCHEMA_ID
    p6_id: str
    status: Literal["proposal_ready", "noop", "blocked", "partially_blocked", "duplicate_suppressed"]
    budget: P6ReviewBudget | None = None
    retry_policy: P6ReviewRetryPolicy
    requests: tuple[P6ScheduledReviewRequest, ...]
    outbox_entries: tuple[P6ScheduledReviewOutboxEntry, ...]
    receipts: tuple[P6ScheduledReviewReceipt, ...]
    trigger_results: tuple[P6TriggerEvaluationResult, ...] = ()
    proposals: tuple[ProactiveProposal, ...] = ()
    blockers: tuple[P6ProposalBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    proposal_refs: tuple[str, ...] = ()
    proposed_count: int = 0
    noop_count: int = 0
    blocked_count: int = 0
    duplicate_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    execution_started: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]


def _stable_ref(prefix: str, value: Any) -> str:
    return f"{prefix}{_stable_digest(value)}"


def _coerce_requests(raw: Any) -> tuple[P6ScheduledReviewRequest, ...]:
    values: Iterable[Any]
    if isinstance(raw, Mapping):
        values = (raw,)
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = ()
    requests: list[P6ScheduledReviewRequest] = []
    for value in values:
        if isinstance(value, P6ScheduledReviewRequest):
            requests.append(value)
        elif isinstance(value, Mapping):
            requests.append(P6ScheduledReviewRequest(**dict(value)))
        else:
            raise ValueError("scheduled review requests must be objects")
    return tuple(requests)


def _coerce_prior_receipts(raw: Any) -> tuple[P6ScheduledReviewPriorReceipt, ...]:
    values = raw if isinstance(raw, (list, tuple)) else ()
    receipts: list[P6ScheduledReviewPriorReceipt] = []
    for value in values:
        if isinstance(value, P6ScheduledReviewPriorReceipt):
            receipts.append(value)
        elif isinstance(value, Mapping):
            receipts.append(P6ScheduledReviewPriorReceipt(**dict(value)))
        else:
            raise ValueError("prior scheduled-review receipts must be objects")
    return tuple(receipts)


def _logical_idempotency_key(p6_id: str, request: P6ScheduledReviewRequest) -> str:
    return hashlib.sha256(_stable_json({
        "p6_id": p6_id,
        "review_kind": request.review_kind,
        "signal_ref": request.signal_ref,
        "target_refs": sorted(request.target_refs),
        "proposed_protocol_ref": request.proposed_protocol_ref or REVIEW_PROTOCOL_REF[request.review_kind],
    }).encode("utf-8")).hexdigest()


def _request_blockers(request: P6ScheduledReviewRequest) -> list[P6ProposalBlocker]:
    blockers: list[P6ProposalBlocker] = []
    if not request.signal_ref.startswith(_DURABLE_SIGNAL_PREFIXES):
        blockers.append(P6ProposalBlocker(
            code="invalid_durable_signal_ref",
            message="Scheduled review requires a durable signal ref.",
        ))
    if not request.source_receipt_refs or any(not ref.startswith("receipt://") for ref in request.source_receipt_refs):
        blockers.append(P6ProposalBlocker(
            code="unreceipted_scheduled_review",
            message="Scheduled review requires durable receipt:// source refs.",
        ))
    if not request.target_refs:
        blockers.append(P6ProposalBlocker(
            code="missing_review_target_refs",
            message="Scheduled review requires at least one target ref.",
        ))
    if (
        request.direct_execution_requested
        or request.provider_job_requested
        or request.protocol_run_requested
        or request.claim_promotion_requested
        or request.graph_write_requested
    ):
        blockers.append(P6ProposalBlocker(
            code="scheduler_execution_intent_blocked",
            message="The scheduler may only project proposals; execution and mutation intent is forbidden.",
        ))
    if request.raw_payload_embedded:
        blockers.append(P6ProposalBlocker(
            code="raw_payload_embedded",
            message="Scheduled review requests must remain refs-only.",
        ))
    return blockers


def build_p6_scheduled_reviews(
    packet: Mapping[str, Any],
    *,
    review_requests: Any,
    budget_payload: Mapping[str, Any] | None = None,
    retry_policy_payload: Mapping[str, Any] | None = None,
    prior_receipts_payload: Any = None,
) -> P6ScheduledReviewResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    requests = _coerce_requests(review_requests)
    budget = P6ReviewBudget(**dict(budget_payload)) if budget_payload else None
    retry_policy = P6ReviewRetryPolicy(**dict(retry_policy_payload or {}))
    prior_receipts = _coerce_prior_receipts(prior_receipts_payload)
    prior_by_key: dict[str, list[P6ScheduledReviewPriorReceipt]] = {}
    for receipt in prior_receipts:
        prior_by_key.setdefault(receipt.idempotency_key, []).append(receipt)

    entries: list[P6ScheduledReviewOutboxEntry] = []
    receipts: list[P6ScheduledReviewReceipt] = []
    trigger_results: list[P6TriggerEvaluationResult] = []
    proposals: list[ProactiveProposal] = []
    blockers: list[P6ProposalBlocker] = []
    seen_attempt_keys: set[str] = set()
    projected_cost = Decimal("0")

    if not requests:
        idempotency_key = hashlib.sha256(f"{p6_id}:scheduled-review-noop".encode("utf-8")).hexdigest()
        attempt_key = hashlib.sha256(f"{idempotency_key}:0".encode("utf-8")).hexdigest()
        receipt_ref = _stable_ref("receipt://p6/scheduled-review/", {
            "p6_id": p6_id,
            "decision": "noop",
            "reason_codes": ("no_eligible_review_events",),
        })
        receipts.append(P6ScheduledReviewReceipt(
            receipt_ref=receipt_ref,
            p6_id=p6_id,
            review_kind="none",
            signal_ref="event://p6/scheduled-review/no-eligible-events",
            decision="noop",
            reason_codes=("no_eligible_review_events",),
            idempotency_key=idempotency_key,
            attempt_key=attempt_key,
            attempt_count=0,
        ))

    for index, request in enumerate(requests):
        idempotency_key = _logical_idempotency_key(p6_id, request)
        history = sorted(prior_by_key.get(idempotency_key, ()), key=lambda item: item.attempt_count)
        attempt_count = 0 if request.mode == "initial" else (history[-1].attempt_count + 1 if history else 0)
        attempt_key = hashlib.sha256(f"{idempotency_key}:{attempt_count}:{request.mode}".encode("utf-8")).hexdigest()
        item_blockers = _request_blockers(request)

        if index >= retry_policy.max_reviews_per_run:
            item_blockers.append(P6ProposalBlocker(
                code="bounded_review_run_limit_reached",
                message="The configured maximum reviews per scheduler run was reached.",
            ))
        if request.mode in {"retry", "backfill"} and not history:
            item_blockers.append(P6ProposalBlocker(
                code="retry_requires_prior_receipt",
                message="Retry and backfill require prior scheduled-review receipts.",
            ))
        if request.mode == "retry" and history and not history[-1].retryable:
            item_blockers.append(P6ProposalBlocker(
                code="prior_receipt_not_retryable",
                message="The latest scheduled-review receipt does not permit retry.",
            ))
        if attempt_count >= retry_policy.max_attempts:
            item_blockers.append(P6ProposalBlocker(
                code="retry_attempts_exhausted",
                message="The scheduled review reached its bounded retry limit.",
            ))
        if request.expensive_review and (budget is None or budget.status == "unknown"):
            item_blockers.append(P6ProposalBlocker(
                code="expensive_review_budget_unknown",
                message="Expensive scheduled reviews fail closed without a known budget.",
            ))
        estimated_cost = request.estimated_cost_usd or Decimal("0")
        if (
            request.expensive_review
            and budget is not None
            and budget.status == "known"
            and budget.max_total_cost_usd is not None
            and projected_cost + estimated_cost > budget.max_total_cost_usd
        ):
            item_blockers.append(P6ProposalBlocker(
                code="review_budget_exceeded",
                message="Projected review cost exceeds the bounded run budget.",
            ))
        duplicate = attempt_key in seen_attempt_keys
        seen_attempt_keys.add(attempt_key)

        if duplicate:
            decision: Literal["proposal_projected", "blocked", "duplicate_suppressed"] = "duplicate_suppressed"
            reason_codes = ("duplicate_review_attempt_suppressed",)
            proposal_ref = None
            proposal_receipt_ref = None
            outbox_ref = None
        elif item_blockers:
            decision = "blocked"
            reason_codes = tuple(sorted({blocker.code for blocker in item_blockers}))
            blockers.extend(item_blockers)
            proposal_ref = None
            proposal_receipt_ref = None
            outbox_ref = None
        else:
            trigger_result = evaluate_p6_triggers(packet, trigger_payloads={
                "trigger_family": REVIEW_TRIGGER_FAMILY[request.review_kind],
                "signal_ref": request.signal_ref,
                "source_event_refs": request.source_event_refs,
                "source_receipt_refs": request.source_receipt_refs,
                "target_refs": request.target_refs,
                "reason_codes": request.reason_codes or (f"{request.review_kind}_{request.mode}_requested",),
                "priority": request.priority,
                "proposed_protocol_ref": request.proposed_protocol_ref or REVIEW_PROTOCOL_REF[request.review_kind],
                "proposed_action_kind": "protocol_request",
                "metadata_refs": request.metadata_refs,
            })
            trigger_results.append(trigger_result)
            blockers.extend(trigger_result.blockers)
            if trigger_result.status != "proposal_ready" or not trigger_result.proposals:
                decision = "blocked"
                reason_codes = tuple(sorted({blocker.code for blocker in trigger_result.blockers})) or (
                    "p6_trigger_evaluator_did_not_create_proposal",
                )
                proposal_ref = None
                proposal_receipt_ref = None
                outbox_ref = None
            else:
                decision = "proposal_projected"
                reason_codes = (f"{request.review_kind}_{request.mode}_proposal_projected",)
                proposal = trigger_result.proposals[0]
                proposals.append(proposal)
                proposal_ref = proposal.proposal_ref
                proposal_receipt_ref = trigger_result.proposal_receipts[0].receipt_ref
                outbox_ref = _stable_ref("outbox://p6/scheduled-review/", {
                    "attempt_key": attempt_key,
                    "proposal_ref": proposal_ref,
                })
                entries.append(P6ScheduledReviewOutboxEntry(
                    outbox_ref=outbox_ref,
                    idempotency_key=idempotency_key,
                    attempt_key=attempt_key,
                    review_kind=request.review_kind,
                    mode=request.mode,
                    attempt_count=attempt_count,
                    signal_ref=request.signal_ref,
                    source_receipt_refs=request.source_receipt_refs,
                    target_refs=request.target_refs,
                    proposal_ref=proposal_ref,
                    proposal_receipt_ref=proposal_receipt_ref,
                    retry_policy=retry_policy,
                ))
                projected_cost += estimated_cost

        receipt_ref = _stable_ref("receipt://p6/scheduled-review/", {
            "p6_id": p6_id,
            "attempt_key": attempt_key,
            "decision": decision,
            "reason_codes": reason_codes,
            "proposal_ref": proposal_ref,
        })
        receipts.append(P6ScheduledReviewReceipt(
            receipt_ref=receipt_ref,
            p6_id=p6_id,
            review_kind=request.review_kind,
            signal_ref=request.signal_ref,
            decision=decision,
            reason_codes=reason_codes,
            idempotency_key=idempotency_key,
            attempt_key=attempt_key,
            attempt_count=attempt_count,
            proposal_ref=proposal_ref,
            proposal_receipt_ref=proposal_receipt_ref,
            outbox_ref=outbox_ref,
            retryable=(
                decision == "blocked"
                and bool({"expensive_review_budget_unknown", "review_budget_exceeded"}.intersection(reason_codes))
            ),
        ))

    proposed_count = sum(receipt.decision == "proposal_projected" for receipt in receipts)
    noop_count = sum(receipt.decision == "noop" for receipt in receipts)
    blocked_count = sum(receipt.decision == "blocked" for receipt in receipts)
    duplicate_count = sum(receipt.decision == "duplicate_suppressed" for receipt in receipts)
    if proposed_count and not (blocked_count or duplicate_count):
        status: Literal["proposal_ready", "noop", "blocked", "partially_blocked", "duplicate_suppressed"] = "proposal_ready"
    elif proposed_count:
        status = "partially_blocked"
    elif noop_count and not blocked_count:
        status = "noop"
    elif duplicate_count and not blocked_count:
        status = "duplicate_suppressed"
    else:
        status = "blocked"

    artifact_ref = _stable_ref("artifact://p6/scheduled-review-harness/", {
        "p6_id": p6_id,
        "outbox_refs": [entry.outbox_ref for entry in entries],
        "receipt_refs": [receipt.receipt_ref for receipt in receipts],
    })
    nested_artifact_refs = tuple(
        dict.fromkeys(ref for result in trigger_results for ref in result.artifact_refs)
    )
    nested_receipt_refs = tuple(
        dict.fromkeys(ref for result in trigger_results for ref in result.receipt_refs)
    )
    return P6ScheduledReviewResult(
        p6_id=p6_id,
        status=status,
        budget=budget,
        retry_policy=retry_policy,
        requests=requests,
        outbox_entries=tuple(entries),
        receipts=tuple(receipts),
        trigger_results=tuple(trigger_results),
        proposals=tuple(proposals),
        blockers=tuple(blockers),
        artifact_refs=(artifact_ref, *nested_artifact_refs),
        receipt_refs=tuple(receipt.receipt_ref for receipt in receipts) + nested_receipt_refs,
        proposal_refs=tuple(proposal.proposal_ref for proposal in proposals),
        proposed_count=proposed_count,
        noop_count=noop_count,
        blocked_count=blocked_count,
        duplicate_count=duplicate_count,
    )
