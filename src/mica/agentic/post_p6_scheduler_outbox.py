from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, Field

from mica.agentic.p6_scheduled_review_retry import (
    P6ScheduledReviewOutboxEntry,
    P6ScheduledReviewResult,
)
from mica.sandbox.backends import BackendSelector
from mica.sandbox.specialist_task import ModalSpecialistTask


POST_P6_DURABLE_OUTBOX_RECORD_SCHEMA_ID = "mica.project_tolomeo.post_p6.durable_scheduler_outbox_record.v1"
POST_P6_DURABLE_OUTBOX_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.post_p6.durable_scheduler_outbox_receipt.v1"
POST_P6_DURABLE_OUTBOX_RESULT_SCHEMA_ID = "mica.project_tolomeo.post_p6.durable_scheduler_outbox_result.v1"
POST_P6_WORKER_CLAIM_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_claim.v1"
POST_P6_WORKER_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_receipt.v1"
POST_P6_WORKER_RESULT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_result.v1"
POST_P6_WORKER_BUDGET_DECISION_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_budget_decision.v1"
POST_P6_WORKER_CIRCUIT_DECISION_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_circuit_decision.v1"
POST_P6_WORKER_MUDO_LINEAGE_EVENT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_mudo_lineage_event.v1"
POST_P6_WORKER_MUDO_LINEAGE_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_mudo_lineage_receipt.v1"
POST_P6_WORKER_MUDO_LINEAGE_RESULT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_mudo_lineage_result.v1"
POST_P6_WORKER_RETRY_TRANSITION_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_retry_transition_receipt.v1"
POST_P6_WORKER_RETRY_TRANSITION_RESULT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_retry_transition_result.v1"
POST_P6_HANDOFF_ACTIVATION_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_handoff_activation.v1"
POST_P6_HANDOFF_ACTIVATION_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_handoff_activation_receipt.v1"
POST_P6_HANDOFF_ACTIVATION_RESULT_SCHEMA_ID = "mica.project_tolomeo.post_p6.scheduler_worker_handoff_activation_result.v1"
POST_P6_DEFAULT_OUTBOX_PATH = ".artifacts/post_p6/scheduler_outbox/outbox.jsonl"
POST_P6_DEFAULT_WORKER_CLAIM_PATH = ".artifacts/post_p6/scheduler_outbox/worker_claims.jsonl"
POST_P6_DEFAULT_WORKER_MUDO_LINEAGE_PATH = ".artifacts/post_p6/scheduler_outbox/worker_mudo_lineage.jsonl"
POST_P6_DEFAULT_WORKER_RETRY_TRANSITION_PATH = ".artifacts/post_p6/scheduler_outbox/worker_retry_transitions.jsonl"
POST_P6_DEFAULT_HANDOFF_ACTIVATION_PATH = ".artifacts/post_p6/scheduler_outbox/handoff_activations.jsonl"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]


def _stable_ref(prefix: str, value: Any) -> str:
    return f"{prefix}{_digest(value)}"


def _resolve_store_path(path: str | Path | None) -> Path:
    value = Path(path or POST_P6_DEFAULT_OUTBOX_PATH)
    if not value.is_absolute():
        value = _repo_root() / value
    return value


def _resolve_claim_store_path(path: str | Path | None) -> Path:
    value = Path(path or POST_P6_DEFAULT_WORKER_CLAIM_PATH)
    if not value.is_absolute():
        value = _repo_root() / value
    return value


def _resolve_lineage_store_path(path: str | Path | None) -> Path:
    value = Path(path or POST_P6_DEFAULT_WORKER_MUDO_LINEAGE_PATH)
    if not value.is_absolute():
        value = _repo_root() / value
    return value


def _resolve_retry_transition_store_path(path: str | Path | None) -> Path:
    value = Path(path or POST_P6_DEFAULT_WORKER_RETRY_TRANSITION_PATH)
    if not value.is_absolute():
        value = _repo_root() / value
    return value


def _resolve_handoff_activation_store_path(path: str | Path | None) -> Path:
    value = Path(path or POST_P6_DEFAULT_HANDOFF_ACTIVATION_PATH)
    if not value.is_absolute():
        value = _repo_root() / value
    return value


def _valid_receipt_ref(value: str) -> bool:
    return value.startswith("receipt://")


def _valid_budget_ref(value: str) -> bool:
    return value.startswith(("budget://", "receipt://", "artifact://"))


class PostP6DurableSchedulerOutboxRecord(BaseModel):
    schema_id: str = POST_P6_DURABLE_OUTBOX_RECORD_SCHEMA_ID
    store_record_ref: str
    source_outbox_ref: str
    outbox_ref: str
    idempotency_key: str
    attempt_key: str
    review_kind: str
    mode: str
    attempt_count: int = Field(ge=0)
    signal_ref: str
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    proposal_ref: str
    proposal_receipt_ref: str
    quetzal_gate_ref: str
    budget_ref: str
    retry_policy: dict[str, Any]
    state: Literal["persisted_not_claimed"] = "persisted_not_claimed"
    retry_state: dict[str, Any]
    submission_authority: Literal["command_kernel_protocol_runtime"] = "command_kernel_protocol_runtime"
    scheduler_authority: Literal["retry_backfill_only"] = "retry_backfill_only"
    worker_authority: Literal["not_activated"] = "not_activated"
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    created_at: str
    creation_receipt_ref: str


class PostP6DurableSchedulerOutboxReceipt(BaseModel):
    schema_id: str = POST_P6_DURABLE_OUTBOX_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["PostP6DurableSchedulerOutboxReceipt"] = "PostP6DurableSchedulerOutboxReceipt"
    decision: Literal["persisted", "duplicate_suppressed", "blocked", "noop"]
    reason_codes: tuple[str, ...]
    outbox_ref: str | None = None
    store_record_ref: str | None = None
    idempotency_key: str | None = None
    source_receipt_refs: tuple[str, ...] = ()
    proposal_ref: str | None = None
    quetzal_gate_ref: str | None = None
    budget_ref: str | None = None
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=_now)


class PostP6DurableSchedulerOutboxResult(BaseModel):
    schema_id: str = POST_P6_DURABLE_OUTBOX_RESULT_SCHEMA_ID
    status: Literal["persisted", "noop", "blocked", "duplicate_suppressed", "partially_persisted"]
    store_backend: Literal["local_jsonl_contract"] = "local_jsonl_contract"
    store_path: str
    records: tuple[PostP6DurableSchedulerOutboxRecord, ...] = ()
    receipts: tuple[PostP6DurableSchedulerOutboxReceipt, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    persisted_count: int = 0
    duplicate_count: int = 0
    blocked_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    execution_started: bool = False


class PostP6SchedulerWorkerClaim(BaseModel):
    schema_id: str = POST_P6_WORKER_CLAIM_SCHEMA_ID
    claim_ref: str
    claim_token: str
    source_outbox_ref: str
    source_store_record_ref: str
    idempotency_key: str
    worker_id: str
    worker_authority: Literal["retry_backfill_only"] = "retry_backfill_only"
    state: Literal["claimed_not_executed"] = "claimed_not_executed"
    handoff_ref: str
    handoff_state: Literal["approved_not_submitted"] = "approved_not_submitted"
    quetzal_gate_ref: str
    budget_ref: str
    budget_decision_ref: str | None = None
    circuit_breaker_ref: str | None = None
    circuit_decision_ref: str | None = None
    source_receipt_refs: tuple[str, ...]
    proposal_ref: str
    proposal_receipt_ref: str
    retry_state: dict[str, Any]
    claimed_at: str
    worker_receipt_ref: str
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    canonical_mudo_mutation_performed: bool = False
    execution_started: bool = False


class PostP6SchedulerWorkerReceipt(BaseModel):
    schema_id: str = POST_P6_WORKER_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["PostP6SchedulerWorkerReceipt"] = "PostP6SchedulerWorkerReceipt"
    decision: Literal["claimed", "duplicate_claim_suppressed", "blocked", "noop"]
    reason_codes: tuple[str, ...]
    worker_id: str | None = None
    claim_ref: str | None = None
    handoff_ref: str | None = None
    source_outbox_ref: str | None = None
    source_store_record_ref: str | None = None
    idempotency_key: str | None = None
    quetzal_gate_ref: str | None = None
    budget_ref: str | None = None
    budget_decision_ref: str | None = None
    circuit_breaker_ref: str | None = None
    circuit_decision_ref: str | None = None
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    canonical_mudo_mutation_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=_now)


class PostP6WorkerBudgetDecision(BaseModel):
    schema_id: str = POST_P6_WORKER_BUDGET_DECISION_SCHEMA_ID
    decision_ref: str
    decision: Literal["approved", "blocked"]
    reason_codes: tuple[str, ...]
    budget_ref: str
    budget_status: Literal["approved", "missing", "unknown", "exceeded", "stale"]
    budget_limit: float | None = None
    budget_spent: float | None = None
    requested_cost: float | None = None
    remaining_budget: float | None = None
    evaluated_at: str = Field(default_factory=_now)


class PostP6WorkerCircuitDecision(BaseModel):
    schema_id: str = POST_P6_WORKER_CIRCUIT_DECISION_SCHEMA_ID
    decision_ref: str
    decision: Literal["approved", "blocked"]
    reason_codes: tuple[str, ...]
    circuit_breaker_ref: str
    circuit_breaker_status: Literal["closed", "missing", "unknown", "open", "stale"]
    evaluated_at: str = Field(default_factory=_now)


class PostP6SchedulerWorkerResult(BaseModel):
    schema_id: str = POST_P6_WORKER_RESULT_SCHEMA_ID
    status: Literal["claimed", "noop", "blocked", "duplicate_claim_suppressed"]
    store_backend: Literal["local_jsonl_contract"] = "local_jsonl_contract"
    outbox_store_path: str
    claim_store_path: str
    claims: tuple[PostP6SchedulerWorkerClaim, ...] = ()
    receipts: tuple[PostP6SchedulerWorkerReceipt, ...] = ()
    budget_decisions: tuple[PostP6WorkerBudgetDecision, ...] = ()
    circuit_decisions: tuple[PostP6WorkerCircuitDecision, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    claimed_count: int = 0
    duplicate_count: int = 0
    blocked_count: int = 0
    budget_blocked_count: int = 0
    circuit_blocked_count: int = 0
    handoff_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    canonical_mudo_mutations_performed: int = 0
    execution_started: bool = False


class PostP6WorkerMUDOLineageEvent(BaseModel):
    schema_id: str = POST_P6_WORKER_MUDO_LINEAGE_EVENT_SCHEMA_ID
    lineage_event_ref: str
    noncanonical_branch_ref: str
    branch_type: Literal["candidate", "failed", "rejected"] = "candidate"
    transition: Literal[
        "outbox_persisted_to_worker_claimed",
        "worker_blocked_projection",
        "worker_handoff_submitted_to_canonical_authority",
    ] = (
        "outbox_persisted_to_worker_claimed"
    )
    state: Literal["noncanonical_mudo_lineage_projected"] = "noncanonical_mudo_lineage_projected"
    source_outbox_ref: str
    source_store_record_ref: str
    worker_claim_ref: str
    worker_receipt_ref: str
    handoff_ref: str
    quetzal_gate_ref: str
    budget_ref: str
    budget_decision_ref: str | None = None
    circuit_breaker_ref: str | None = None
    circuit_decision_ref: str | None = None
    proposal_ref: str
    proposal_receipt_ref: str
    source_receipt_refs: tuple[str, ...]
    input_refs: tuple[str, ...]
    idempotency_key: str
    projected_at: str
    lineage_authority: Literal["post_p6_worker_refs_projection"] = "post_p6_worker_refs_projection"
    canonical_mudo_mutation_performed: bool = False
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False


class PostP6WorkerMUDOLineageReceipt(BaseModel):
    schema_id: str = POST_P6_WORKER_MUDO_LINEAGE_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["PostP6WorkerMUDOLineageReceipt"] = "PostP6WorkerMUDOLineageReceipt"
    decision: Literal["projected", "duplicate_projection_suppressed", "blocked", "noop"]
    reason_codes: tuple[str, ...]
    lineage_event_ref: str | None = None
    noncanonical_branch_ref: str | None = None
    source_outbox_ref: str | None = None
    source_store_record_ref: str | None = None
    worker_claim_ref: str | None = None
    worker_receipt_ref: str | None = None
    idempotency_key: str | None = None
    canonical_mudo_mutation_performed: bool = False
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=_now)


class PostP6WorkerMUDOLineageResult(BaseModel):
    schema_id: str = POST_P6_WORKER_MUDO_LINEAGE_RESULT_SCHEMA_ID
    status: Literal["projected", "noop", "blocked", "duplicate_projection_suppressed", "partially_projected"]
    store_backend: Literal["local_jsonl_contract"] = "local_jsonl_contract"
    outbox_store_path: str
    claim_store_path: str
    lineage_store_path: str
    events: tuple[PostP6WorkerMUDOLineageEvent, ...] = ()
    receipts: tuple[PostP6WorkerMUDOLineageReceipt, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    projected_count: int = 0
    duplicate_count: int = 0
    blocked_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    canonical_mudo_mutations_performed: int = 0
    execution_started: bool = False


class PostP6WorkerRetryTransitionReceipt(BaseModel):
    schema_id: str = POST_P6_WORKER_RETRY_TRANSITION_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["PostP6WorkerRetryTransitionReceipt"] = "PostP6WorkerRetryTransitionReceipt"
    decision: Literal["recorded", "duplicate_transition_suppressed", "blocked", "noop"]
    transition: Literal[
        "persisted_to_claimed",
        "persisted_to_blocked",
        "persisted_to_retryable_pending",
    ] | None = None
    reason_codes: tuple[str, ...]
    source_outbox_ref: str | None = None
    source_store_record_ref: str | None = None
    worker_claim_ref: str | None = None
    worker_receipt_ref: str | None = None
    handoff_ref: str | None = None
    idempotency_key: str | None = None
    transition_key: str | None = None
    attempt_index: int | None = None
    backoff_ms: int | None = None
    provider_from: str | None = None
    provider_to: str | None = None
    fallback_chain: tuple[str, ...] = ()
    selection_receipt_ref: str | None = None
    provider_binding_ref: str | None = None
    retry_state_before: dict[str, Any] = Field(default_factory=dict)
    retry_state_after: dict[str, Any] = Field(default_factory=dict)
    canonical_mudo_mutation_performed: bool = False
    provider_job_created: bool = False
    protocol_run_created: bool = False
    episode_run_created: bool = False
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=_now)


class PostP6WorkerRetryTransitionResult(BaseModel):
    schema_id: str = POST_P6_WORKER_RETRY_TRANSITION_RESULT_SCHEMA_ID
    status: Literal["recorded", "noop", "blocked", "duplicate_transition_suppressed"]
    store_backend: Literal["local_jsonl_contract"] = "local_jsonl_contract"
    outbox_store_path: str
    claim_store_path: str
    retry_transition_store_path: str
    receipts: tuple[PostP6WorkerRetryTransitionReceipt, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    recorded_count: int = 0
    duplicate_count: int = 0
    blocked_count: int = 0
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    episode_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    canonical_mudo_mutations_performed: int = 0
    execution_started: bool = False


class PostP6HandoffActivation(BaseModel):
    schema_id: str = POST_P6_HANDOFF_ACTIVATION_SCHEMA_ID
    activation_ref: str
    source_handoff_ref: str
    source_outbox_ref: str
    source_claim_ref: str
    source_store_record_ref: str
    idempotency_key: str
    state: Literal["activation_requested", "blocked", "submitted_to_canonical_authority"]
    source_authority: Literal["command_kernel", "protocol_executor"]
    worker_authority: Literal["retry_backfill_only"] = "retry_backfill_only"
    quetzal_regate_ref: str | None = None
    budget_decision_ref: str | None = None
    circuit_decision_ref: str | None = None
    harness_run_request_ref: str | None = None
    run_ref: str | None = None
    run_receipt_ref: str | None = None
    run_receipt_bundle_ref: str | None = None
    budget_ref: str | None = None
    provider_binding_ref: str | None = None
    selected_provider: str | None = None
    selection_receipt_ref: str | None = None
    fallback_chain: tuple[str, ...] = ()
    failover_chain: tuple[dict[str, Any], ...] = ()
    noncanonical_branch_ref: str | None = None
    blocker_code: str | None = None
    blocker_detail: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    creation_receipt_ref: str
    created_at: str


class PostP6HandoffActivationReceipt(BaseModel):
    schema_id: str = POST_P6_HANDOFF_ACTIVATION_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["PostP6HandoffActivationReceipt"] = "PostP6HandoffActivationReceipt"
    decision: Literal["submitted", "blocked", "duplicate_activation_suppressed", "noop"]
    reason_codes: tuple[str, ...]
    activation_ref: str | None = None
    source_handoff_ref: str | None = None
    source_outbox_ref: str | None = None
    source_claim_ref: str | None = None
    harness_run_request_ref: str | None = None
    run_ref: str | None = None
    run_receipt_ref: str | None = None
    run_receipt_bundle_ref: str | None = None
    provider_binding_ref: str | None = None
    selected_provider: str | None = None
    selection_receipt_ref: str | None = None
    noncanonical_branch_ref: str | None = None
    blocker_code: str | None = None
    source_authority: str | None = None
    timestamp: str = Field(default_factory=_now)


class PostP6HandoffActivationResult(BaseModel):
    schema_id: str = POST_P6_HANDOFF_ACTIVATION_RESULT_SCHEMA_ID
    status: Literal["submitted", "blocked", "duplicate_activation_suppressed", "noop", "retrying", "dead_letter"]
    store_backend: Literal["local_jsonl_contract"] = "local_jsonl_contract"
    outbox_store_path: str
    claim_store_path: str
    activation_store_path: str
    lineage_store_path: str
    activations: tuple[PostP6HandoffActivation, ...] = ()
    receipts: tuple[PostP6HandoffActivationReceipt, ...] = ()
    lineage_events: tuple[PostP6WorkerMUDOLineageEvent, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    retry_transition_receipt_refs: tuple[str, ...] = ()
    submitted_count: int = 0
    blocked_count: int = 0
    duplicate_count: int = 0
    retry_count: int = 0
    dead_letter_count: int = 0
    protocol_runs_created: int = 0
    outbox_dispatch_performed: bool = False
    canonical_mudo_mutations_performed: int = 0
    execution_started: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)


class PostP6JsonlSchedulerOutboxStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_store_path(path)

    def read_records(self) -> tuple[PostP6DurableSchedulerOutboxRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[PostP6DurableSchedulerOutboxRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(PostP6DurableSchedulerOutboxRecord(**json.loads(line)))
        return tuple(records)

    def append_records(self, records: Iterable[PostP6DurableSchedulerOutboxRecord]) -> None:
        rows = list(records)
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in rows:
                handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
                handle.write("\n")


class PostP6JsonlSchedulerWorkerClaimStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_claim_store_path(path)

    def read_claims(self) -> tuple[PostP6SchedulerWorkerClaim, ...]:
        if not self.path.exists():
            return ()
        claims: list[PostP6SchedulerWorkerClaim] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            claims.append(PostP6SchedulerWorkerClaim(**json.loads(line)))
        return tuple(claims)

    def append_claims(self, claims: Iterable[PostP6SchedulerWorkerClaim]) -> None:
        rows = list(claims)
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for claim in rows:
                handle.write(json.dumps(claim.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
                handle.write("\n")


class PostP6JsonlWorkerMUDOLineageStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_lineage_store_path(path)

    def read_events(self) -> tuple[PostP6WorkerMUDOLineageEvent, ...]:
        if not self.path.exists():
            return ()
        events: list[PostP6WorkerMUDOLineageEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(PostP6WorkerMUDOLineageEvent(**json.loads(line)))
        return tuple(events)

    def append_events(self, events: Iterable[PostP6WorkerMUDOLineageEvent]) -> None:
        rows = list(events)
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for event in rows:
                handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
                handle.write("\n")


class PostP6JsonlWorkerRetryTransitionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_retry_transition_store_path(path)

    def read_receipts(self) -> tuple[PostP6WorkerRetryTransitionReceipt, ...]:
        if not self.path.exists():
            return ()
        receipts: list[PostP6WorkerRetryTransitionReceipt] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            receipts.append(PostP6WorkerRetryTransitionReceipt(**json.loads(line)))
        return tuple(receipts)

    def append_receipts(self, receipts: Iterable[PostP6WorkerRetryTransitionReceipt]) -> None:
        rows = list(receipts)
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for receipt in rows:
                handle.write(json.dumps(receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
                handle.write("\n")


class PostP6JsonlHandoffActivationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_handoff_activation_store_path(path)

    def read_activations(self) -> tuple[PostP6HandoffActivation, ...]:
        if not self.path.exists():
            return ()
        activations: list[PostP6HandoffActivation] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            activations.append(PostP6HandoffActivation(**json.loads(line)))
        return tuple(activations)

    def append_activations(self, activations: Iterable[PostP6HandoffActivation]) -> None:
        rows = list(activations)
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for activation in rows:
                handle.write(json.dumps(activation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
                handle.write("\n")


def _block_receipt(
    *,
    reason_codes: Iterable[str],
    entry: P6ScheduledReviewOutboxEntry | None = None,
    quetzal_gate_ref: str | None = None,
    budget_ref: str | None = None,
) -> PostP6DurableSchedulerOutboxReceipt:
    reason_tuple = tuple(sorted(set(reason_codes)))
    payload = {
        "decision": "blocked",
        "reason_codes": reason_tuple,
        "outbox_ref": entry.outbox_ref if entry else None,
        "idempotency_key": entry.idempotency_key if entry else None,
        "quetzal_gate_ref": quetzal_gate_ref,
        "budget_ref": budget_ref,
    }
    return PostP6DurableSchedulerOutboxReceipt(
        receipt_ref=_stable_ref("receipt://post-p6/scheduler-outbox/", payload),
        decision="blocked",
        reason_codes=reason_tuple,
        outbox_ref=entry.outbox_ref if entry else None,
        idempotency_key=entry.idempotency_key if entry else None,
        source_receipt_refs=entry.source_receipt_refs if entry else (),
        proposal_ref=entry.proposal_ref if entry else None,
        quetzal_gate_ref=quetzal_gate_ref,
        budget_ref=budget_ref,
    )


def _entry_blockers(
    entry: P6ScheduledReviewOutboxEntry,
    *,
    quetzal_gate_ref: str,
    budget_ref: str,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not entry.source_receipt_refs or any(not _valid_receipt_ref(ref) for ref in entry.source_receipt_refs):
        blockers.append("missing_source_receipt_refs")
    if not _valid_receipt_ref(quetzal_gate_ref):
        blockers.append("missing_quetzal_approval")
    if not _valid_budget_ref(budget_ref):
        blockers.append("missing_budget_ref")
    if (
        entry.provider_job_created
        or entry.protocol_run_created
        or entry.episode_run_created
        or entry.dispatched
    ):
        blockers.append("execution_intent_blocked")
    return tuple(blockers)


def _worker_receipt(
    *,
    decision: Literal["claimed", "duplicate_claim_suppressed", "blocked", "noop"],
    reason_codes: Iterable[str],
    worker_id: str | None = None,
    record: PostP6DurableSchedulerOutboxRecord | None = None,
    claim_ref: str | None = None,
    handoff_ref: str | None = None,
    budget_decision_ref: str | None = None,
    circuit_breaker_ref: str | None = None,
    circuit_decision_ref: str | None = None,
) -> PostP6SchedulerWorkerReceipt:
    reason_tuple = tuple(sorted(set(reason_codes)))
    payload = {
        "decision": decision,
        "reason_codes": reason_tuple,
        "worker_id": worker_id,
        "source_outbox_ref": record.outbox_ref if record else None,
        "idempotency_key": record.idempotency_key if record else None,
        "claim_ref": claim_ref,
        "handoff_ref": handoff_ref,
        "budget_decision_ref": budget_decision_ref,
        "circuit_breaker_ref": circuit_breaker_ref,
        "circuit_decision_ref": circuit_decision_ref,
    }
    return PostP6SchedulerWorkerReceipt(
        receipt_ref=_stable_ref("receipt://post-p6/scheduler-worker/", payload),
        decision=decision,
        reason_codes=reason_tuple,
        worker_id=worker_id,
        claim_ref=claim_ref,
        handoff_ref=handoff_ref,
        source_outbox_ref=record.outbox_ref if record else None,
        source_store_record_ref=record.store_record_ref if record else None,
        idempotency_key=record.idempotency_key if record else None,
        quetzal_gate_ref=record.quetzal_gate_ref if record else None,
        budget_ref=record.budget_ref if record else None,
        budget_decision_ref=budget_decision_ref,
        circuit_breaker_ref=circuit_breaker_ref,
        circuit_decision_ref=circuit_decision_ref,
    )


def _record_is_worker_claimable(record: PostP6DurableSchedulerOutboxRecord) -> tuple[str, ...]:
    blockers: list[str] = []
    if record.state != "persisted_not_claimed":
        blockers.append("outbox_record_not_persisted_not_claimed")
    if record.worker_authority != "not_activated":
        blockers.append("outbox_record_already_has_worker_authority")
    if not record.source_receipt_refs or any(not _valid_receipt_ref(ref) for ref in record.source_receipt_refs):
        blockers.append("missing_source_receipt_refs")
    if not _valid_receipt_ref(record.quetzal_gate_ref):
        blockers.append("missing_quetzal_approval")
    if not _valid_budget_ref(record.budget_ref):
        blockers.append("missing_budget_ref")
    if (
        record.provider_job_created
        or record.protocol_run_created
        or record.episode_run_created
        or record.outbox_dispatch_performed
        or record.claim_promotion_performed
        or record.graph_write_performed
        or record.execution_started
    ):
        blockers.append("source_outbox_execution_intent_blocked")
    return tuple(blockers)


def _claim_from_record(
    record: PostP6DurableSchedulerOutboxRecord,
    *,
    worker_id: str,
    budget_decision: PostP6WorkerBudgetDecision | None = None,
    circuit_decision: PostP6WorkerCircuitDecision | None = None,
) -> PostP6SchedulerWorkerClaim:
    claimed_at = _now()
    claim_ref = _stable_ref("claim://post-p6/scheduler-worker/", {
        "source_outbox_ref": record.outbox_ref,
        "idempotency_key": record.idempotency_key,
        "worker_id": worker_id,
    })
    handoff_ref = _stable_ref("handoff://post-p6/scheduler-worker/", {
        "source_outbox_ref": record.outbox_ref,
        "quetzal_gate_ref": record.quetzal_gate_ref,
        "budget_ref": record.budget_ref,
        "budget_decision_ref": budget_decision.decision_ref if budget_decision else None,
        "circuit_decision_ref": circuit_decision.decision_ref if circuit_decision else None,
    })
    worker_receipt_ref = _stable_ref("receipt://post-p6/scheduler-worker/", {
        "decision": "claimed",
        "claim_ref": claim_ref,
        "handoff_ref": handoff_ref,
    })
    retry_state = dict(record.retry_state)
    retry_state.update({
        "claimed": True,
        "claim_ref": claim_ref,
        "claimed_at": claimed_at,
        "handoff_state": "approved_not_submitted",
    })
    return PostP6SchedulerWorkerClaim(
        claim_ref=claim_ref,
        claim_token=_stable_ref("token://post-p6/scheduler-worker/", {
            "claim_ref": claim_ref,
            "worker_id": worker_id,
        }),
        source_outbox_ref=record.outbox_ref,
        source_store_record_ref=record.store_record_ref,
        idempotency_key=record.idempotency_key,
        worker_id=worker_id,
        handoff_ref=handoff_ref,
        quetzal_gate_ref=record.quetzal_gate_ref,
        budget_ref=record.budget_ref,
        budget_decision_ref=budget_decision.decision_ref if budget_decision else None,
        circuit_breaker_ref=circuit_decision.circuit_breaker_ref if circuit_decision else None,
        circuit_decision_ref=circuit_decision.decision_ref if circuit_decision else None,
        source_receipt_refs=record.source_receipt_refs,
        proposal_ref=record.proposal_ref,
        proposal_receipt_ref=record.proposal_receipt_ref,
        retry_state=retry_state,
        claimed_at=claimed_at,
        worker_receipt_ref=worker_receipt_ref,
    )


def _execution_intent_blockers(
    *,
    provider_job_requested: bool,
    protocol_run_requested: bool,
    episode_run_requested: bool,
    claim_promotion_requested: bool,
    graph_write_requested: bool,
    canonical_mudo_mutation_requested: bool,
    execute_now: bool,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if provider_job_requested:
        blockers.append("provider_job_request_blocked")
    if protocol_run_requested:
        blockers.append("protocol_run_request_blocked")
    if episode_run_requested:
        blockers.append("episode_run_request_blocked")
    if claim_promotion_requested:
        blockers.append("claim_promotion_request_blocked")
    if graph_write_requested:
        blockers.append("graph_write_request_blocked")
    if canonical_mudo_mutation_requested:
        blockers.append("canonical_mudo_mutation_request_blocked")
    if execute_now:
        blockers.append("worker_direct_execution_request_blocked")
    return tuple(blockers)


def _handoff_activation_receipt(
    *,
    decision: Literal["submitted", "blocked", "duplicate_activation_suppressed", "noop"],
    reason_codes: Iterable[str],
    activation: PostP6HandoffActivation | None = None,
    claim: PostP6SchedulerWorkerClaim | None = None,
    blocker_code: str | None = None,
    harness_run_request_ref: str | None = None,
    run_ref: str | None = None,
    run_receipt_ref: str | None = None,
    run_receipt_bundle_ref: str | None = None,
    noncanonical_branch_ref: str | None = None,
    source_authority: str | None = None,
) -> PostP6HandoffActivationReceipt:
    reason_tuple = tuple(sorted(set(reason_codes)))
    payload = {
        "decision": decision,
        "reason_codes": reason_tuple,
        "source_handoff_ref": activation.source_handoff_ref if activation else claim.handoff_ref if claim else None,
        "source_claim_ref": activation.source_claim_ref if activation else claim.claim_ref if claim else None,
        "harness_run_request_ref": harness_run_request_ref or (activation.harness_run_request_ref if activation else None),
        "run_ref": run_ref or (activation.run_ref if activation else None),
        "blocker_code": blocker_code or (activation.blocker_code if activation else None),
        "provider_binding_ref": activation.provider_binding_ref if activation else None,
        "selected_provider": activation.selected_provider if activation else None,
        "selection_receipt_ref": activation.selection_receipt_ref if activation else None,
    }
    return PostP6HandoffActivationReceipt(
        receipt_ref=_stable_ref("receipt://post-p6/handoff-activation/", payload),
        decision=decision,
        reason_codes=reason_tuple,
        activation_ref=activation.activation_ref if activation else None,
        source_handoff_ref=activation.source_handoff_ref if activation else claim.handoff_ref if claim else None,
        source_outbox_ref=activation.source_outbox_ref if activation else claim.source_outbox_ref if claim else None,
        source_claim_ref=activation.source_claim_ref if activation else claim.claim_ref if claim else None,
        harness_run_request_ref=harness_run_request_ref or (activation.harness_run_request_ref if activation else None),
        run_ref=run_ref or (activation.run_ref if activation else None),
        run_receipt_ref=run_receipt_ref or (activation.run_receipt_ref if activation else None),
        run_receipt_bundle_ref=run_receipt_bundle_ref or (activation.run_receipt_bundle_ref if activation else None),
        provider_binding_ref=activation.provider_binding_ref if activation else None,
        selected_provider=activation.selected_provider if activation else None,
        selection_receipt_ref=activation.selection_receipt_ref if activation else None,
        noncanonical_branch_ref=noncanonical_branch_ref or (activation.noncanonical_branch_ref if activation else None),
        blocker_code=blocker_code or (activation.blocker_code if activation else None),
        source_authority=source_authority or (activation.source_authority if activation else None),
    )


def _activation_event_from_claim(
    claim: PostP6SchedulerWorkerClaim,
    record: PostP6DurableSchedulerOutboxRecord,
    *,
    run_receipt_ref: str,
    run_receipt_bundle_ref: str,
) -> PostP6WorkerMUDOLineageEvent:
    projected_at = _now()
    seed = {
        "source_outbox_ref": claim.source_outbox_ref,
        "worker_claim_ref": claim.claim_ref,
        "worker_receipt_ref": claim.worker_receipt_ref,
        "transition": "worker_handoff_submitted_to_canonical_authority",
        "run_receipt_ref": run_receipt_ref,
    }
    input_refs = tuple(
        ref
        for ref in (
            record.store_record_ref,
            record.creation_receipt_ref,
            claim.worker_receipt_ref,
            claim.claim_ref,
            claim.handoff_ref,
            claim.budget_decision_ref,
            claim.circuit_decision_ref,
            run_receipt_ref,
            run_receipt_bundle_ref,
            *claim.source_receipt_refs,
        )
        if ref
    )
    return PostP6WorkerMUDOLineageEvent(
        lineage_event_ref=_stable_ref("event://post-p6/scheduler-worker-mudo-lineage/", seed),
        noncanonical_branch_ref=_stable_ref("mudo-branch://post-p6/scheduler-worker/", seed),
        transition="worker_handoff_submitted_to_canonical_authority",
        source_outbox_ref=claim.source_outbox_ref,
        source_store_record_ref=claim.source_store_record_ref,
        worker_claim_ref=claim.claim_ref,
        worker_receipt_ref=claim.worker_receipt_ref,
        handoff_ref=claim.handoff_ref,
        quetzal_gate_ref=claim.quetzal_gate_ref,
        budget_ref=claim.budget_ref,
        budget_decision_ref=claim.budget_decision_ref,
        circuit_breaker_ref=claim.circuit_breaker_ref,
        circuit_decision_ref=claim.circuit_decision_ref,
        proposal_ref=claim.proposal_ref,
        proposal_receipt_ref=claim.proposal_receipt_ref,
        source_receipt_refs=claim.source_receipt_refs,
        input_refs=input_refs,
        idempotency_key=f"post-p6:handoff-activation:{claim.source_outbox_ref}:{run_receipt_ref}",
        projected_at=projected_at,
        execution_started=True,
    )


def _lineage_receipt(
    *,
    decision: Literal["projected", "duplicate_projection_suppressed", "blocked", "noop"],
    reason_codes: Iterable[str],
    event: PostP6WorkerMUDOLineageEvent | None = None,
    claim: PostP6SchedulerWorkerClaim | None = None,
    record: PostP6DurableSchedulerOutboxRecord | None = None,
) -> PostP6WorkerMUDOLineageReceipt:
    reason_tuple = tuple(sorted(set(reason_codes)))
    payload = {
        "decision": decision,
        "reason_codes": reason_tuple,
        "lineage_event_ref": event.lineage_event_ref if event else None,
        "source_outbox_ref": (event.source_outbox_ref if event else record.outbox_ref if record else None),
        "worker_claim_ref": (event.worker_claim_ref if event else claim.claim_ref if claim else None),
        "worker_receipt_ref": (event.worker_receipt_ref if event else claim.worker_receipt_ref if claim else None),
    }
    return PostP6WorkerMUDOLineageReceipt(
        receipt_ref=_stable_ref("receipt://post-p6/scheduler-worker-mudo-lineage/", payload),
        decision=decision,
        reason_codes=reason_tuple,
        lineage_event_ref=event.lineage_event_ref if event else None,
        noncanonical_branch_ref=event.noncanonical_branch_ref if event else None,
        source_outbox_ref=event.source_outbox_ref if event else record.outbox_ref if record else None,
        source_store_record_ref=event.source_store_record_ref if event else record.store_record_ref if record else None,
        worker_claim_ref=event.worker_claim_ref if event else claim.claim_ref if claim else None,
        worker_receipt_ref=event.worker_receipt_ref if event else claim.worker_receipt_ref if claim else None,
        idempotency_key=event.idempotency_key if event else claim.idempotency_key if claim else None,
    )


def _lineage_event_from_claim(
    claim: PostP6SchedulerWorkerClaim,
    record: PostP6DurableSchedulerOutboxRecord,
) -> PostP6WorkerMUDOLineageEvent:
    projected_at = _now()
    input_refs = tuple(
        ref
        for ref in (
            record.store_record_ref,
            record.creation_receipt_ref,
            claim.worker_receipt_ref,
            claim.claim_ref,
            claim.handoff_ref,
            claim.budget_decision_ref,
            claim.circuit_decision_ref,
            *claim.source_receipt_refs,
        )
        if ref
    )
    seed = {
        "source_outbox_ref": claim.source_outbox_ref,
        "worker_claim_ref": claim.claim_ref,
        "worker_receipt_ref": claim.worker_receipt_ref,
        "transition": "outbox_persisted_to_worker_claimed",
    }
    return PostP6WorkerMUDOLineageEvent(
        lineage_event_ref=_stable_ref("event://post-p6/scheduler-worker-mudo-lineage/", seed),
        noncanonical_branch_ref=_stable_ref("mudo-branch://post-p6/scheduler-worker/", seed),
        source_outbox_ref=claim.source_outbox_ref,
        source_store_record_ref=claim.source_store_record_ref,
        worker_claim_ref=claim.claim_ref,
        worker_receipt_ref=claim.worker_receipt_ref,
        handoff_ref=claim.handoff_ref,
        quetzal_gate_ref=claim.quetzal_gate_ref,
        budget_ref=claim.budget_ref,
        budget_decision_ref=claim.budget_decision_ref,
        circuit_breaker_ref=claim.circuit_breaker_ref,
        circuit_decision_ref=claim.circuit_decision_ref,
        proposal_ref=claim.proposal_ref,
        proposal_receipt_ref=claim.proposal_receipt_ref,
        source_receipt_refs=claim.source_receipt_refs,
        input_refs=input_refs,
        idempotency_key=f"post-p6:mudo-lineage:{claim.source_outbox_ref}:{claim.worker_receipt_ref}",
        projected_at=projected_at,
    )


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _handoff_retry_receipts(
    transition_store: "PostP6JsonlWorkerRetryTransitionStore",
    *,
    handoff_ref: str,
) -> tuple[PostP6WorkerRetryTransitionReceipt, ...]:
    return tuple(receipt for receipt in transition_store.read_receipts() if receipt.handoff_ref == handoff_ref)


def inspect_post_p6_handoff_retry_status(
    *,
    run_ref: str | None = None,
    handoff_ref: str | None = None,
    activation_store_path: str | Path | None = None,
    retry_transition_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    outbox_store_path: str | Path | None = None,
) -> dict[str, Any]:
    activation_store = PostP6JsonlHandoffActivationStore(activation_store_path)
    transition_store = PostP6JsonlWorkerRetryTransitionStore(retry_transition_store_path)
    claim_store = PostP6JsonlSchedulerWorkerClaimStore(claim_store_path)
    outbox_store = PostP6JsonlSchedulerOutboxStore(outbox_store_path)

    normalized_run_ref = str(run_ref or "").strip()
    normalized_handoff_ref = str(handoff_ref or "").strip()
    activations = activation_store.read_activations()
    activation = None
    if normalized_run_ref:
        activation = next((item for item in activations if item.run_ref == normalized_run_ref), None)
        if activation is not None and not normalized_handoff_ref:
            normalized_handoff_ref = activation.source_handoff_ref
        elif activation is None and normalized_run_ref.startswith("handoff://") and not normalized_handoff_ref:
            normalized_handoff_ref = normalized_run_ref
    elif normalized_handoff_ref:
        activation = next((item for item in activations if item.source_handoff_ref == normalized_handoff_ref), None)

    if not normalized_handoff_ref:
        return {
            "status": "not_found",
            "run_ref": normalized_run_ref or None,
            "handoff_ref": None,
            "retry_receipts": [],
            "retry_attempt_count": 0,
            "dead_lettered": False,
            "activation_found": False,
        }

    retry_receipts = list(_handoff_retry_receipts(transition_store, handoff_ref=normalized_handoff_ref))
    claim = next((item for item in claim_store.read_claims() if item.handoff_ref == normalized_handoff_ref), None)
    outbox_record = None
    if claim is not None:
        outbox_record = next((item for item in outbox_store.read_records() if item.outbox_ref == claim.source_outbox_ref), None)

    retry_attempt_count = sum(
        1
        for receipt in retry_receipts
        if receipt.transition == "persisted_to_retryable_pending"
    )
    dead_letter_receipt = next(
        (receipt for receipt in reversed(retry_receipts) if receipt.transition == "persisted_to_blocked"),
        None,
    )
    latest_receipt = retry_receipts[-1] if retry_receipts else None
    dead_lettered = dead_letter_receipt is not None
    if dead_lettered:
        status = "dead_letter"
    elif retry_receipts:
        status = "retrying"
    else:
        status = "idle"

    return {
        "status": status,
        "run_ref": normalized_run_ref or (activation.run_ref if activation else None),
        "handoff_ref": normalized_handoff_ref,
        "activation_found": activation is not None,
        "activation_ref": activation.activation_ref if activation else None,
        "activation_state": activation.state if activation else None,
        "claim_ref": claim.claim_ref if claim else None,
        "claim_state": claim.handoff_state if claim else None,
        "source_outbox_ref": claim.source_outbox_ref if claim else (activation.source_outbox_ref if activation else None),
        "retry_attempt_count": retry_attempt_count,
        "retry_receipt_count": len(retry_receipts),
        "dead_lettered": dead_lettered,
        "dead_letter_attempt_index": dead_letter_receipt.attempt_index if dead_letter_receipt else None,
        "latest_retry_transition": latest_receipt.transition if latest_receipt else None,
        "latest_retry_timestamp": latest_receipt.timestamp if latest_receipt else None,
        "retry_state": dict(outbox_record.retry_state) if outbox_record is not None else {},
        "retry_receipts": [receipt.model_dump(mode="json") for receipt in retry_receipts],
    }


def inspect_post_p6_handoff_binding(
    *,
    run_ref: str | None = None,
    handoff_ref: str | None = None,
    activation_store_path: str | Path | None = None,
    retry_transition_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    outbox_store_path: str | Path | None = None,
) -> dict[str, Any]:
    retry_status = inspect_post_p6_handoff_retry_status(
        run_ref=run_ref,
        handoff_ref=handoff_ref,
        activation_store_path=activation_store_path,
        retry_transition_store_path=retry_transition_store_path,
        claim_store_path=claim_store_path,
        outbox_store_path=outbox_store_path,
    )
    activation_store = PostP6JsonlHandoffActivationStore(activation_store_path)
    normalized_run_ref = str(run_ref or "").strip()
    normalized_handoff_ref = str(retry_status.get("handoff_ref") or handoff_ref or "").strip()
    activation = None
    if normalized_run_ref:
        activation = next((item for item in activation_store.read_activations() if item.run_ref == normalized_run_ref), None)
    if activation is None and normalized_handoff_ref:
        activation = next((item for item in activation_store.read_activations() if item.source_handoff_ref == normalized_handoff_ref), None)

    retry_receipts = retry_status.get("retry_receipts") or []
    failovers = [
        {
            "receipt_ref": item.get("receipt_ref"),
            "attempt_index": item.get("attempt_index"),
            "from_provider": item.get("provider_from"),
            "to_provider": item.get("provider_to"),
            "reason_codes": list(item.get("reason_codes") or []),
            "selection_receipt_ref": item.get("selection_receipt_ref"),
            "provider_binding_ref": item.get("provider_binding_ref"),
            "timestamp": item.get("timestamp"),
        }
        for item in retry_receipts
        if item.get("transition") == "persisted_to_retryable_pending"
    ]
    fallback_chain = list(activation.fallback_chain) if activation is not None else []
    if not fallback_chain:
        for item in retry_receipts:
            fallback_chain = list(item.get("fallback_chain") or [])
            if fallback_chain:
                break
    selected_provider = activation.selected_provider if activation is not None else None
    if selected_provider is None and failovers:
        selected_provider = failovers[-1].get("to_provider")
    binding_receipt_ref = activation.selection_receipt_ref if activation is not None else None
    if binding_receipt_ref is None:
        for item in reversed(retry_receipts):
            binding_receipt_ref = item.get("selection_receipt_ref")
            if binding_receipt_ref:
                break
    binding_ref = activation.provider_binding_ref if activation is not None else None
    if binding_ref is None:
        for item in reversed(retry_receipts):
            binding_ref = item.get("provider_binding_ref")
            if binding_ref:
                break
    distinct_run_refs = sorted(
        {
            value
            for value in [retry_status.get("run_ref"), activation.run_ref if activation is not None else None]
            if value
        }
    )
    if bool(retry_status.get("dead_lettered")):
        status = "dead_letter"
    elif activation is not None and activation.state == "submitted_to_canonical_authority":
        status = "submitted"
    else:
        status = retry_status.get("status") or ("submitted" if activation is not None else "not_found")
    return {
        "status": status,
        "run_ref": retry_status.get("run_ref") or (activation.run_ref if activation is not None else None),
        "handoff_ref": normalized_handoff_ref or None,
        "activation_ref": activation.activation_ref if activation is not None else None,
        "provider_binding_ref": binding_ref,
        "selected_provider": selected_provider,
        "selection_receipt_ref": binding_receipt_ref,
        "fallback_chain": fallback_chain,
        "failovers": failovers if activation is None else list(activation.failover_chain or failovers),
        "distinct_run_refs": len(distinct_run_refs),
        "retry_attempt_count": retry_status.get("retry_attempt_count", 0),
        "dead_lettered": bool(retry_status.get("dead_lettered")),
    }


def inspect_post_p6_handoff_status(
    *,
    run_ref: str | None = None,
    handoff_ref: str | None = None,
    activation_store_path: str | Path | None = None,
    retry_transition_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    outbox_store_path: str | Path | None = None,
) -> dict[str, Any]:
    retry_status = inspect_post_p6_handoff_retry_status(
        run_ref=run_ref,
        handoff_ref=handoff_ref,
        activation_store_path=activation_store_path,
        retry_transition_store_path=retry_transition_store_path,
        claim_store_path=claim_store_path,
        outbox_store_path=outbox_store_path,
    )
    binding_status = inspect_post_p6_handoff_binding(
        run_ref=run_ref,
        handoff_ref=handoff_ref,
        activation_store_path=activation_store_path,
        retry_transition_store_path=retry_transition_store_path,
        claim_store_path=claim_store_path,
        outbox_store_path=outbox_store_path,
    )
    deadletter_entry = None
    if bool(retry_status.get("dead_lettered")):
        deadletters = list_post_p6_handoff_deadletters(
            retry_transition_store_path=retry_transition_store_path,
            activation_store_path=activation_store_path,
            claim_store_path=claim_store_path,
        )
        normalized_handoff_ref = str(retry_status.get("handoff_ref") or handoff_ref or "").strip()
        deadletter_entry = next(
            (
                item
                for item in deadletters.get("dead_letters") or []
                if str(item.get("handoff_ref") or "").strip() == normalized_handoff_ref
            ),
            None,
        )

    return {
        "status": binding_status.get("status") or retry_status.get("status") or "not_found",
        "run_ref": binding_status.get("run_ref") or retry_status.get("run_ref"),
        "handoff_ref": binding_status.get("handoff_ref") or retry_status.get("handoff_ref"),
        "activation_found": bool(retry_status.get("activation_found")),
        "activation_ref": binding_status.get("activation_ref") or retry_status.get("activation_ref"),
        "activation_state": retry_status.get("activation_state"),
        "claim_ref": retry_status.get("claim_ref"),
        "claim_state": retry_status.get("claim_state"),
        "source_outbox_ref": retry_status.get("source_outbox_ref"),
        "provider_binding_ref": binding_status.get("provider_binding_ref"),
        "selected_provider": binding_status.get("selected_provider"),
        "selection_receipt_ref": binding_status.get("selection_receipt_ref"),
        "fallback_chain": list(binding_status.get("fallback_chain") or []),
        "failovers": list(binding_status.get("failovers") or []),
        "retry_attempt_count": int(retry_status.get("retry_attempt_count") or 0),
        "retry_receipt_count": int(retry_status.get("retry_receipt_count") or 0),
        "latest_retry_transition": retry_status.get("latest_retry_transition"),
        "latest_retry_timestamp": retry_status.get("latest_retry_timestamp"),
        "retry_state": dict(retry_status.get("retry_state") or {}),
        "dead_lettered": bool(retry_status.get("dead_lettered")),
        "dead_letter_attempt_index": retry_status.get("dead_letter_attempt_index"),
        "dead_letter_entry": dict(deadletter_entry or {}),
    }


def list_post_p6_handoff_deadletters(
    *,
    retry_transition_store_path: str | Path | None = None,
    activation_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
) -> dict[str, Any]:
    transition_store = PostP6JsonlWorkerRetryTransitionStore(retry_transition_store_path)
    activation_store = PostP6JsonlHandoffActivationStore(activation_store_path)
    claim_store = PostP6JsonlSchedulerWorkerClaimStore(claim_store_path)

    activations_by_handoff = {
        activation.source_handoff_ref: activation
        for activation in activation_store.read_activations()
    }
    claims_by_handoff = {
        claim.handoff_ref: claim
        for claim in claim_store.read_claims()
    }

    deadletter_entries: list[dict[str, Any]] = []
    seen_handoffs: set[str] = set()
    for receipt in reversed(transition_store.read_receipts()):
        handoff = str(receipt.handoff_ref or "").strip()
        if not handoff or handoff in seen_handoffs:
            continue
        if receipt.transition != "persisted_to_blocked":
            continue
        seen_handoffs.add(handoff)
        activation = activations_by_handoff.get(handoff)
        claim = claims_by_handoff.get(handoff)
        deadletter_entries.append(
            {
                "handoff_ref": handoff,
                "run_ref": activation.run_ref if activation else None,
                "activation_ref": activation.activation_ref if activation else None,
                "claim_ref": claim.claim_ref if claim else None,
                "source_outbox_ref": receipt.source_outbox_ref or (claim.source_outbox_ref if claim else None),
                "worker_receipt_ref": receipt.worker_receipt_ref,
                "attempt_index": receipt.attempt_index,
                "reason_codes": list(receipt.reason_codes),
                "receipt_ref": receipt.receipt_ref,
                "timestamp": receipt.timestamp,
            }
        )

    deadletter_entries.reverse()
    return {
        "status": "ok",
        "dead_letter_count": len(deadletter_entries),
        "dead_letters": deadletter_entries,
    }


def _resolve_retry_policy(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {
            "enabled": False,
            "base_ms": 250,
            "factor": 2.0,
            "max_attempts": 3,
            "jitter": 0.0,
            "transient_set": (
                "provider_unavailable",
                "protocol_executor_unavailable",
            ),
        }
    data = dict(payload or {})
    transient_values = data.get("transient_set") or (
        "provider_unavailable",
        "protocol_executor_unavailable",
    )
    transient_set = tuple(str(value).strip() for value in transient_values if str(value).strip())
    return {
        "enabled": True,
        "base_ms": max(1, int(data.get("base_ms", 250))),
        "factor": max(1.0, float(data.get("factor", 2.0))),
        "max_attempts": max(1, int(data.get("max_attempts", 3))),
        "jitter": max(0.0, float(data.get("jitter", 0.0))),
        "transient_set": transient_set,
    }


def _resolve_backpressure_policy(payload: Mapping[str, Any] | None) -> dict[str, int]:
    data = dict(payload or {})
    return {
        "max_concurrent_submits": max(0, int(data.get("max_concurrent_submits", 0))),
        "max_queue_depth": max(0, int(data.get("max_queue_depth", 0))),
    }


def _compute_backoff_ms(*, handoff_ref: str, attempt_index: int, base_ms: int, factor: float, jitter: float) -> int:
    base_value = int(round(base_ms * (factor ** max(0, attempt_index - 1))))
    if jitter <= 0:
        return base_value
    jitter_window = max(1, int(round(base_value * jitter)))
    jitter_seed = int(_digest({"handoff_ref": handoff_ref, "attempt_index": attempt_index}, length=8), 16)
    return base_value + (jitter_seed % jitter_window)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = value.strip()
        return (normalized,) if normalized else ()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return tuple(dict.fromkeys(items))
    return ()


def _selector_scalar(selection_requirements: Mapping[str, Any] | None, request_metadata: Mapping[str, Any] | None, *keys: str) -> Any:
    for source in (selection_requirements or {}, request_metadata or {}):
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, "", (), [], {}):
                return value
    return None


def _selector_provider_candidates(selection_requirements: Mapping[str, Any] | None, request_metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    capability_requirements = _selector_scalar(selection_requirements, request_metadata, "capability_requirements")
    nested = capability_requirements if isinstance(capability_requirements, Mapping) else {}
    return (
        _string_tuple(_selector_scalar(selection_requirements, request_metadata, "allowed_providers", "provider_candidates"))
        or _string_tuple(nested.get("providers"))
        or _string_tuple(nested.get("allowed_providers"))
    )


def _build_selector_task(
    *,
    handoff_ref: str,
    budget_ref: str,
    protocol_payload: Mapping[str, Any] | None,
    request_metadata: Mapping[str, Any] | None,
    selection_requirements: Mapping[str, Any] | None,
) -> ModalSpecialistTask:
    protocol_budgets = (
        dict(protocol_payload.get("budgets") or {})
        if isinstance(protocol_payload, Mapping)
        else {}
    )
    gpu_value = _selector_scalar(selection_requirements, request_metadata, "gpu", "gpu_type")
    timeout_value = _selector_scalar(selection_requirements, request_metadata, "timeout_s", "timeout")
    if timeout_value is None:
        timeout_value = protocol_budgets.get("max_wall_clock_s")
    timeout_s = max(10, int(timeout_value or 300))
    max_cost_value = _selector_scalar(selection_requirements, request_metadata, "max_cost_usd")
    if max_cost_value is None:
        max_cost_value = protocol_budgets.get("max_usd")
    max_cost_usd = _float_or_none(max_cost_value) or 5.0
    region = _selector_scalar(selection_requirements, request_metadata, "region")
    features = _selector_scalar(selection_requirements, request_metadata, "features")
    backend = _selector_scalar(selection_requirements, request_metadata, "backend")
    capability_requirements = _selector_scalar(selection_requirements, request_metadata, "capability_requirements")
    provider_candidates = _selector_provider_candidates(selection_requirements, request_metadata)
    remote_only = bool(gpu_value) or bool(_selector_scalar(selection_requirements, request_metadata, "remote_only"))
    parameters: dict[str, Any] = {
        "handoff_ref": handoff_ref,
        "budget_ref": budget_ref,
        "region": region,
        "features": list(_string_tuple(features)),
        "provider_candidates": list(provider_candidates),
        "remote_only": remote_only,
    }
    if backend not in (None, ""):
        parameters["backend"] = str(backend).strip().lower()
    if isinstance(capability_requirements, Mapping):
        parameters["capability_requirements"] = dict(capability_requirements)
    return ModalSpecialistTask(
        specialist="driver_self_test",
        operation="repo_sandbox_smoke_matrix",
        query="Select canonical provider binding for HN worker handoff submit.",
        task_id=f"hn-binding-{_digest({'handoff_ref': handoff_ref, 'budget_ref': budget_ref}, length=12)}",
        parameters=parameters,
        gpu=str(gpu_value).strip() if gpu_value not in (None, "") else None,
        timeout=timeout_s,
        max_cost_usd=max_cost_usd,
    )


def _provider_available(
    selector: BackendSelector,
    provider_name: str,
    provider_available_override: bool | Mapping[str, bool] | None,
) -> bool:
    if isinstance(provider_available_override, Mapping):
        if provider_name in provider_available_override:
            return bool(provider_available_override[provider_name])
    elif isinstance(provider_available_override, bool):
        return provider_available_override
    return bool(selector._get(provider_name).is_available())


def _provider_failover_history(
    retry_receipts: Iterable[PostP6WorkerRetryTransitionReceipt],
) -> tuple[dict[str, Any], ...]:
    history: list[dict[str, Any]] = []
    for receipt in retry_receipts:
        if receipt.transition != "persisted_to_retryable_pending":
            continue
        if not (receipt.provider_from or receipt.provider_to):
            continue
        history.append(
            {
                "receipt_ref": receipt.receipt_ref,
                "attempt_index": receipt.attempt_index,
                "from_provider": receipt.provider_from,
                "to_provider": receipt.provider_to,
                "reason_codes": list(receipt.reason_codes),
                "selection_receipt_ref": receipt.selection_receipt_ref,
                "provider_binding_ref": receipt.provider_binding_ref,
                "timestamp": receipt.timestamp,
            }
        )
    return tuple(history)


def _select_provider_binding(
    *,
    handoff_ref: str,
    budget_ref: str,
    protocol_payload: Mapping[str, Any] | None,
    request_metadata: Mapping[str, Any] | None,
    selection_requirements: Mapping[str, Any] | None,
    retry_receipts_before: Iterable[PostP6WorkerRetryTransitionReceipt],
) -> dict[str, Any]:
    selector = BackendSelector()
    selector_task = _build_selector_task(
        handoff_ref=handoff_ref,
        budget_ref=budget_ref,
        protocol_payload=protocol_payload,
        request_metadata=request_metadata,
        selection_requirements=selection_requirements,
    )
    remote_only = bool(selector_task.parameters.get("remote_only"))
    ordered_chain = selector.candidate_chain(selector_task, only_available=False, include_local=not remote_only)
    provider_candidates = _selector_provider_candidates(selection_requirements, request_metadata)
    if provider_candidates:
        ordered_chain = tuple(name for name in ordered_chain if name in set(provider_candidates))
        if not ordered_chain:
            return {
                "status": "provider_selection_failed",
                "reason_codes": ("provider_selection_failed",),
                "selected_provider": None,
                "fallback_chain": (),
                "failover_chain": _provider_failover_history(retry_receipts_before),
                "selector_task": selector_task,
                "provider_candidates": provider_candidates,
            }
    if not ordered_chain:
        return {
            "status": "all_providers_unavailable",
            "reason_codes": ("all_providers_unavailable",),
            "selected_provider": None,
            "fallback_chain": (),
            "failover_chain": _provider_failover_history(retry_receipts_before),
            "selector_task": selector_task,
            "provider_candidates": provider_candidates,
        }
    consumed_failovers = len(
        [receipt for receipt in retry_receipts_before if receipt.transition == "persisted_to_retryable_pending"]
    )
    if consumed_failovers >= len(ordered_chain):
        return {
            "status": "all_providers_unavailable",
            "reason_codes": ("all_providers_unavailable",),
            "selected_provider": None,
            "fallback_chain": ordered_chain,
            "failover_chain": _provider_failover_history(retry_receipts_before),
            "selector_task": selector_task,
            "provider_candidates": provider_candidates,
        }
    selected_provider = ordered_chain[consumed_failovers]
    selection_receipt_ref = _stable_ref(
        "receipt://post-p6/handoff-provider-selection/",
        {
            "handoff_ref": handoff_ref,
            "budget_ref": budget_ref,
            "selected_provider": selected_provider,
            "fallback_chain": ordered_chain,
            "attempt_index": consumed_failovers + 1,
        },
    )
    provider_binding_ref = _stable_ref(
        "binding://post-p6/handoff-provider/",
        {
            "handoff_ref": handoff_ref,
            "selected_provider": selected_provider,
            "attempt_index": consumed_failovers + 1,
        },
    )
    return {
        "status": "selected",
        "reason_codes": ("provider_selected",),
        "selected_provider": selected_provider,
        "selection_receipt_ref": selection_receipt_ref,
        "provider_binding_ref": provider_binding_ref,
        "fallback_chain": ordered_chain,
        "failover_chain": _provider_failover_history(retry_receipts_before),
        "selector_task": selector_task,
        "provider_candidates": provider_candidates,
    }


def _build_hn_submit_metrics(
    *,
    inflight_count: int = 0,
    queue_depth: int = 0,
    retry_attempt_count: int = 0,
    backoff_ms: int = 0,
    dead_letter_count: int = 0,
) -> dict[str, Any]:
    return {
        "hn_submit_inflight": inflight_count,
        "hn_submit_queue_depth": queue_depth,
        "hn_submit_retry_attempts_total": retry_attempt_count,
        "hn_submit_backoff_seconds": round(backoff_ms / 1000.0, 6),
        "hn_submit_dead_letter_total": dead_letter_count,
    }


def _budget_decision_from_payload(
    record: PostP6DurableSchedulerOutboxRecord,
    payload: Mapping[str, Any] | None,
) -> PostP6WorkerBudgetDecision:
    if not isinstance(payload, Mapping):
        reason_codes = ("worker_budget_missing",)
        budget_ref = record.budget_ref
        decision_payload = {
            "source_outbox_ref": record.outbox_ref,
            "budget_ref": budget_ref,
            "reason_codes": reason_codes,
        }
        return PostP6WorkerBudgetDecision(
            decision_ref=_stable_ref("decision://post-p6/scheduler-worker/budget/", decision_payload),
            decision="blocked",
            reason_codes=reason_codes,
            budget_ref=budget_ref,
            budget_status="missing",
        )

    budget_ref = str(payload.get("budget_ref") or record.budget_ref or "").strip()
    status = str(
        payload.get("budget_status")
        or payload.get("status")
        or payload.get("state")
        or ""
    ).strip().lower()
    limit = _float_or_none(_first_present(payload, "budget_limit", "limit", "max_cost_units", "max_units"))
    spent = _float_or_none(_first_present(payload, "budget_spent", "spent", "used_cost_units", "used_units"))
    requested = _float_or_none(_first_present(payload, "requested_cost", "requested", "requested_cost_units", "cost_units"))
    stale = bool(payload.get("stale") or payload.get("is_stale") or status == "stale")

    reason_codes: tuple[str, ...]
    budget_status: Literal["approved", "missing", "unknown", "exceeded", "stale"]
    decision: Literal["approved", "blocked"]
    remaining: float | None = None

    if stale:
        decision = "blocked"
        budget_status = "stale"
        reason_codes = ("worker_budget_stale",)
    elif not budget_ref:
        decision = "blocked"
        budget_status = "missing"
        reason_codes = ("worker_budget_missing",)
    elif status in {"missing", "absent"}:
        decision = "blocked"
        budget_status = "missing"
        reason_codes = ("worker_budget_missing",)
    elif status in {"unknown", "unavailable", "pending", ""}:
        decision = "blocked"
        budget_status = "unknown"
        reason_codes = ("worker_budget_unknown",)
    elif limit is None or spent is None or requested is None:
        decision = "blocked"
        budget_status = "unknown"
        reason_codes = ("worker_budget_unknown",)
    elif spent + requested > limit:
        decision = "blocked"
        budget_status = "exceeded"
        reason_codes = ("worker_budget_exceeded",)
        remaining = limit - spent
    elif status not in {"approved", "ok", "available", "closed"}:
        decision = "blocked"
        budget_status = "unknown"
        reason_codes = ("worker_budget_unknown",)
        remaining = limit - spent
    else:
        decision = "approved"
        budget_status = "approved"
        reason_codes = ("worker_budget_approved",)
        remaining = limit - spent - requested

    decision_payload = {
        "source_outbox_ref": record.outbox_ref,
        "budget_ref": budget_ref,
        "budget_status": budget_status,
        "budget_limit": limit,
        "budget_spent": spent,
        "requested_cost": requested,
        "reason_codes": reason_codes,
    }
    return PostP6WorkerBudgetDecision(
        decision_ref=_stable_ref("decision://post-p6/scheduler-worker/budget/", decision_payload),
        decision=decision,
        reason_codes=reason_codes,
        budget_ref=budget_ref,
        budget_status=budget_status,
        budget_limit=limit,
        budget_spent=spent,
        requested_cost=requested,
        remaining_budget=remaining,
    )


def _circuit_decision_from_payload(
    record: PostP6DurableSchedulerOutboxRecord,
    payload: Mapping[str, Any] | None,
) -> PostP6WorkerCircuitDecision:
    if not isinstance(payload, Mapping):
        reason_codes = ("worker_circuit_breaker_missing",)
        circuit_ref = _stable_ref("circuit://post-p6/scheduler-worker/", record.outbox_ref)
        decision_payload = {
            "source_outbox_ref": record.outbox_ref,
            "circuit_breaker_ref": circuit_ref,
            "reason_codes": reason_codes,
        }
        return PostP6WorkerCircuitDecision(
            decision_ref=_stable_ref("decision://post-p6/scheduler-worker/circuit/", decision_payload),
            decision="blocked",
            reason_codes=reason_codes,
            circuit_breaker_ref=circuit_ref,
            circuit_breaker_status="missing",
        )

    circuit_ref = str(
        payload.get("circuit_breaker_ref")
        or payload.get("circuit_ref")
        or payload.get("ref")
        or ""
    ).strip()
    status = str(
        payload.get("circuit_breaker_status")
        or payload.get("status")
        or payload.get("state")
        or ""
    ).strip().lower()
    stale = bool(payload.get("stale") or payload.get("is_stale") or status == "stale")

    if stale:
        decision = "blocked"
        circuit_status = "stale"
        reason_codes = ("worker_circuit_breaker_stale",)
    elif not circuit_ref:
        decision = "blocked"
        circuit_status = "missing"
        reason_codes = ("worker_circuit_breaker_missing",)
    elif status in {"open", "tripped", "blocked"}:
        decision = "blocked"
        circuit_status = "open"
        reason_codes = ("worker_circuit_breaker_open",)
    elif status in {"closed", "ok", "healthy"}:
        decision = "approved"
        circuit_status = "closed"
        reason_codes = ("worker_circuit_breaker_closed",)
    else:
        decision = "blocked"
        circuit_status = "unknown"
        reason_codes = ("worker_circuit_breaker_unknown",)

    decision_payload = {
        "source_outbox_ref": record.outbox_ref,
        "circuit_breaker_ref": circuit_ref,
        "circuit_breaker_status": circuit_status,
        "reason_codes": reason_codes,
    }
    return PostP6WorkerCircuitDecision(
        decision_ref=_stable_ref("decision://post-p6/scheduler-worker/circuit/", decision_payload),
        decision=decision,
        reason_codes=reason_codes,
        circuit_breaker_ref=circuit_ref,
        circuit_breaker_status=circuit_status,
    )


def claim_post_p6_scheduler_outbox_entry(
    *,
    outbox_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    worker_id: str | None,
    max_claims: int = 1,
    provider_job_requested: bool = False,
    protocol_run_requested: bool = False,
    episode_run_requested: bool = False,
    claim_promotion_requested: bool = False,
    graph_write_requested: bool = False,
    canonical_mudo_mutation_requested: bool = False,
    execute_now: bool = False,
    enforce_budget: bool = False,
    budget_payload: Mapping[str, Any] | None = None,
    enforce_circuit_breaker: bool = False,
    circuit_breaker_payload: Mapping[str, Any] | None = None,
) -> PostP6SchedulerWorkerResult:
    outbox_store = PostP6JsonlSchedulerOutboxStore(outbox_store_path)
    claim_store = PostP6JsonlSchedulerWorkerClaimStore(claim_store_path)
    worker_value = str(worker_id or "").strip()
    blockers: list[dict[str, Any]] = []

    request_blockers = list(_execution_intent_blockers(
        provider_job_requested=provider_job_requested,
        protocol_run_requested=protocol_run_requested,
        episode_run_requested=episode_run_requested,
        claim_promotion_requested=claim_promotion_requested,
        graph_write_requested=graph_write_requested,
        canonical_mudo_mutation_requested=canonical_mudo_mutation_requested,
        execute_now=execute_now,
    ))
    if not worker_value:
        request_blockers.append("missing_worker_id")
    if max_claims != 1:
        request_blockers.append("worker_claim_batching_not_enabled")
    if request_blockers:
        blockers.append({"reason_codes": request_blockers})
        receipt = _worker_receipt(
            decision="blocked",
            reason_codes=request_blockers,
            worker_id=worker_value or None,
        )
        return PostP6SchedulerWorkerResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            receipts=(receipt,),
            blockers=tuple(blockers),
            blocked_count=1,
        )

    records = outbox_store.read_records()
    if not records:
        receipt = _worker_receipt(
            decision="noop",
            reason_codes=("no_persisted_outbox_records",),
            worker_id=worker_value,
        )
        return PostP6SchedulerWorkerResult(
            status="noop",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            receipts=(receipt,),
        )

    existing_claims = claim_store.read_claims()
    claimed_keys = {claim.idempotency_key for claim in existing_claims}
    for record in records:
        if record.idempotency_key in claimed_keys:
            receipt = _worker_receipt(
                decision="duplicate_claim_suppressed",
                reason_codes=("duplicate_worker_claim_suppressed",),
                worker_id=worker_value,
                record=record,
            )
            return PostP6SchedulerWorkerResult(
                status="duplicate_claim_suppressed",
                outbox_store_path=str(outbox_store.path),
                claim_store_path=str(claim_store.path),
                receipts=(receipt,),
                duplicate_count=1,
            )

        record_blockers = _record_is_worker_claimable(record)
        if record_blockers:
            blockers.append({
                "outbox_ref": record.outbox_ref,
                "idempotency_key": record.idempotency_key,
                "reason_codes": list(record_blockers),
            })
            receipt = _worker_receipt(
                decision="blocked",
                reason_codes=record_blockers,
                worker_id=worker_value,
                record=record,
            )
            return PostP6SchedulerWorkerResult(
                status="blocked",
                outbox_store_path=str(outbox_store.path),
                claim_store_path=str(claim_store.path),
                receipts=(receipt,),
                blockers=tuple(blockers),
                blocked_count=1,
            )

        budget_decision = _budget_decision_from_payload(record, budget_payload) if enforce_budget else None
        circuit_decision = (
            _circuit_decision_from_payload(record, circuit_breaker_payload)
            if enforce_circuit_breaker
            else None
        )
        gate_reason_codes: list[str] = []
        if budget_decision is not None:
            gate_reason_codes.extend(budget_decision.reason_codes)
        if circuit_decision is not None:
            gate_reason_codes.extend(circuit_decision.reason_codes)
        if (
            (budget_decision is not None and budget_decision.decision == "blocked")
            or (circuit_decision is not None and circuit_decision.decision == "blocked")
        ):
            blockers.append({
                "outbox_ref": record.outbox_ref,
                "idempotency_key": record.idempotency_key,
                "reason_codes": gate_reason_codes,
                "budget_decision_ref": budget_decision.decision_ref if budget_decision else None,
                "circuit_decision_ref": circuit_decision.decision_ref if circuit_decision else None,
            })
            receipt = _worker_receipt(
                decision="blocked",
                reason_codes=gate_reason_codes,
                worker_id=worker_value,
                record=record,
                budget_decision_ref=budget_decision.decision_ref if budget_decision else None,
                circuit_breaker_ref=circuit_decision.circuit_breaker_ref if circuit_decision else None,
                circuit_decision_ref=circuit_decision.decision_ref if circuit_decision else None,
            )
            return PostP6SchedulerWorkerResult(
                status="blocked",
                outbox_store_path=str(outbox_store.path),
                claim_store_path=str(claim_store.path),
                receipts=(receipt,),
                budget_decisions=(budget_decision,) if budget_decision else (),
                circuit_decisions=(circuit_decision,) if circuit_decision else (),
                blockers=tuple(blockers),
                blocked_count=1,
                budget_blocked_count=1 if budget_decision and budget_decision.decision == "blocked" else 0,
                circuit_blocked_count=1 if circuit_decision and circuit_decision.decision == "blocked" else 0,
            )

        claim = _claim_from_record(
            record,
            worker_id=worker_value,
            budget_decision=budget_decision,
            circuit_decision=circuit_decision,
        )
        claim_store.append_claims((claim,))
        receipt = _worker_receipt(
            decision="claimed",
            reason_codes=(
                "worker_claim_persisted",
                "quetzal_approved_handoff_created_not_submitted",
                *gate_reason_codes,
            ),
            worker_id=worker_value,
            record=record,
            claim_ref=claim.claim_ref,
            handoff_ref=claim.handoff_ref,
            budget_decision_ref=budget_decision.decision_ref if budget_decision else None,
            circuit_breaker_ref=circuit_decision.circuit_breaker_ref if circuit_decision else None,
            circuit_decision_ref=circuit_decision.decision_ref if circuit_decision else None,
        )
        return PostP6SchedulerWorkerResult(
            status="claimed",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            claims=(claim,),
            receipts=(receipt,),
            budget_decisions=(budget_decision,) if budget_decision else (),
            circuit_decisions=(circuit_decision,) if circuit_decision else (),
            claimed_count=1,
            handoff_count=1,
        )

    receipt = _worker_receipt(
        decision="noop",
        reason_codes=("no_claimable_outbox_records",),
        worker_id=worker_value,
    )
    return PostP6SchedulerWorkerResult(
        status="noop",
        outbox_store_path=str(outbox_store.path),
        claim_store_path=str(claim_store.path),
        receipts=(receipt,),
    )


def project_post_p6_worker_mudo_lineage(
    *,
    outbox_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    lineage_store_path: str | Path | None = None,
    max_events: int = 1,
    provider_job_requested: bool = False,
    protocol_run_requested: bool = False,
    episode_run_requested: bool = False,
    claim_promotion_requested: bool = False,
    graph_write_requested: bool = False,
    canonical_mudo_mutation_requested: bool = False,
    execute_now: bool = False,
) -> PostP6WorkerMUDOLineageResult:
    outbox_store = PostP6JsonlSchedulerOutboxStore(outbox_store_path)
    claim_store = PostP6JsonlSchedulerWorkerClaimStore(claim_store_path)
    lineage_store = PostP6JsonlWorkerMUDOLineageStore(lineage_store_path)
    request_blockers = _execution_intent_blockers(
        provider_job_requested=provider_job_requested,
        protocol_run_requested=protocol_run_requested,
        episode_run_requested=episode_run_requested,
        claim_promotion_requested=claim_promotion_requested,
        graph_write_requested=graph_write_requested,
        canonical_mudo_mutation_requested=canonical_mudo_mutation_requested,
        execute_now=execute_now,
    )
    if max_events != 1:
        request_blockers = (*request_blockers, "lineage_batching_not_enabled")
    if request_blockers:
        receipt = _lineage_receipt(decision="blocked", reason_codes=request_blockers)
        return PostP6WorkerMUDOLineageResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(receipt,),
            blockers=({"reason_codes": list(request_blockers)},),
            blocked_count=1,
        )

    records = outbox_store.read_records()
    claims = claim_store.read_claims()
    if not claims:
        receipt = _lineage_receipt(
            decision="noop",
            reason_codes=("no_worker_claim_records",),
        )
        return PostP6WorkerMUDOLineageResult(
            status="noop",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(receipt,),
        )

    records_by_outbox = {record.outbox_ref: record for record in records}
    existing_events = lineage_store.read_events()
    existing_keys = {event.idempotency_key for event in existing_events}
    blockers: list[dict[str, Any]] = []
    receipts: list[PostP6WorkerMUDOLineageReceipt] = []
    events: list[PostP6WorkerMUDOLineageEvent] = []

    for claim in claims:
        record = records_by_outbox.get(claim.source_outbox_ref)
        if record is None:
            blockers.append({
                "worker_claim_ref": claim.claim_ref,
                "source_outbox_ref": claim.source_outbox_ref,
                "reason_codes": ["missing_source_outbox_record"],
            })
            receipts.append(_lineage_receipt(
                decision="blocked",
                reason_codes=("missing_source_outbox_record",),
                claim=claim,
            ))
            break

        event = _lineage_event_from_claim(claim, record)
        if event.idempotency_key in existing_keys:
            receipts.append(_lineage_receipt(
                decision="duplicate_projection_suppressed",
                reason_codes=("duplicate_mudo_lineage_projection_suppressed",),
                event=event,
                claim=claim,
                record=record,
            ))
            break

        events.append(event)
        receipts.append(_lineage_receipt(
            decision="projected",
            reason_codes=("worker_refs_projected_to_noncanonical_mudo_lineage",),
            event=event,
            claim=claim,
            record=record,
        ))
        existing_keys.add(event.idempotency_key)
        break

    lineage_store.append_events(events)
    projected_count = len(events)
    duplicate_count = sum(receipt.decision == "duplicate_projection_suppressed" for receipt in receipts)
    blocked_count = sum(receipt.decision == "blocked" for receipt in receipts)
    if projected_count and not (duplicate_count or blocked_count):
        status: Literal["projected", "noop", "blocked", "duplicate_projection_suppressed", "partially_projected"] = "projected"
    elif projected_count:
        status = "partially_projected"
    elif blocked_count:
        status = "blocked"
    elif duplicate_count:
        status = "duplicate_projection_suppressed"
    else:
        status = "noop"
    return PostP6WorkerMUDOLineageResult(
        status=status,
        outbox_store_path=str(outbox_store.path),
        claim_store_path=str(claim_store.path),
        lineage_store_path=str(lineage_store.path),
        events=tuple(events),
        receipts=tuple(receipts),
        blockers=tuple(blockers),
        projected_count=projected_count,
        duplicate_count=duplicate_count,
        blocked_count=blocked_count,
    )


def _retry_transition_receipt(
    *,
    decision: Literal["recorded", "duplicate_transition_suppressed", "blocked", "noop"],
    reason_codes: tuple[str, ...],
    transition: Literal["persisted_to_claimed", "persisted_to_blocked", "persisted_to_retryable_pending"] | None = None,
    record: PostP6DurableSchedulerOutboxRecord | None = None,
    claim: PostP6SchedulerWorkerClaim | None = None,
    worker_receipt_payload: Mapping[str, Any] | None = None,
    attempt_index: int | None = None,
    backoff_ms: int | None = None,
    provider_from: str | None = None,
    provider_to: str | None = None,
    fallback_chain: tuple[str, ...] = (),
    selection_receipt_ref: str | None = None,
    provider_binding_ref: str | None = None,
) -> PostP6WorkerRetryTransitionReceipt:
    transition_key = None
    worker_receipt_ref = claim.worker_receipt_ref if claim else None
    worker_claim_ref = claim.claim_ref if claim else None
    handoff_ref = claim.handoff_ref if claim else None
    if worker_receipt_payload is not None:
        worker_receipt_ref = str(worker_receipt_payload.get("receipt_ref") or worker_receipt_ref or "").strip() or None
        worker_claim_ref = str(worker_receipt_payload.get("claim_ref") or worker_claim_ref or "").strip() or None
        handoff_ref = str(worker_receipt_payload.get("handoff_ref") or handoff_ref or "").strip() or None
        provider_from = str(worker_receipt_payload.get("provider_from") or provider_from or "").strip() or None
        provider_to = str(worker_receipt_payload.get("provider_to") or provider_to or "").strip() or None
        selection_receipt_ref = str(worker_receipt_payload.get("selection_receipt_ref") or selection_receipt_ref or "").strip() or None
        provider_binding_ref = str(worker_receipt_payload.get("provider_binding_ref") or provider_binding_ref or "").strip() or None
        if not fallback_chain:
            fallback_chain = _string_tuple(worker_receipt_payload.get("fallback_chain"))
    if record is not None:
        transition_key = (
            f"post-p6:retry-transition:{record.outbox_ref}:{transition}:"
            f"{attempt_index or 'no-attempt'}:{worker_receipt_ref or 'no-worker-receipt'}:{provider_from or 'no-provider-from'}:{provider_to or 'no-provider-to'}"
        )
    payload = {
        "decision": decision,
        "transition": transition,
        "source_outbox_ref": record.outbox_ref if record else None,
        "worker_claim_ref": worker_claim_ref,
        "worker_receipt_ref": worker_receipt_ref,
        "transition_key": transition_key,
        "attempt_index": attempt_index,
        "backoff_ms": backoff_ms,
        "provider_from": provider_from,
        "provider_to": provider_to,
        "fallback_chain": fallback_chain,
        "selection_receipt_ref": selection_receipt_ref,
        "provider_binding_ref": provider_binding_ref,
        "reason_codes": reason_codes,
    }
    retry_state_before = dict(record.retry_state) if record else {}
    retry_state_after = dict(retry_state_before)
    if transition == "persisted_to_claimed":
        retry_state_after.update({
            "claimed": True,
            "claim_ref": worker_claim_ref,
            "worker_receipt_ref": worker_receipt_ref,
        })
    elif transition == "persisted_to_blocked":
        retry_state_after.update({
            "blocked": True,
            "worker_receipt_ref": worker_receipt_ref,
            "reason_codes": list(reason_codes),
        })
    elif transition == "persisted_to_retryable_pending":
        retry_state_after.update({
            "retryable": True,
            "reason_codes": list(reason_codes),
            "provider_from": provider_from,
            "provider_to": provider_to,
            "fallback_chain": list(fallback_chain),
            "selection_receipt_ref": selection_receipt_ref,
            "provider_binding_ref": provider_binding_ref,
        })
    return PostP6WorkerRetryTransitionReceipt(
        receipt_ref=_stable_ref("receipt://post-p6/scheduler-worker/retry-transition/", payload),
        decision=decision,
        transition=transition,
        reason_codes=reason_codes,
        source_outbox_ref=record.outbox_ref if record else None,
        source_store_record_ref=record.store_record_ref if record else None,
        worker_claim_ref=worker_claim_ref,
        worker_receipt_ref=worker_receipt_ref,
        handoff_ref=handoff_ref,
        idempotency_key=record.idempotency_key if record else None,
        transition_key=transition_key,
        attempt_index=attempt_index,
        backoff_ms=backoff_ms,
        provider_from=provider_from,
        provider_to=provider_to,
        fallback_chain=fallback_chain,
        selection_receipt_ref=selection_receipt_ref,
        provider_binding_ref=provider_binding_ref,
        retry_state_before=retry_state_before,
        retry_state_after=retry_state_after,
    )


def record_post_p6_worker_retry_transition(
    *,
    outbox_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    retry_transition_store_path: str | Path | None = None,
    source_outbox_ref: str | None = None,
    worker_receipt_payload: Mapping[str, Any] | None = None,
    max_transitions: int = 1,
    provider_job_requested: bool = False,
    protocol_run_requested: bool = False,
    episode_run_requested: bool = False,
    claim_promotion_requested: bool = False,
    graph_write_requested: bool = False,
    canonical_mudo_mutation_requested: bool = False,
    execute_now: bool = False,
    attempt_index: int | None = None,
    backoff_ms: int | None = None,
    provider_from: str | None = None,
    provider_to: str | None = None,
    fallback_chain: tuple[str, ...] = (),
    selection_receipt_ref: str | None = None,
    provider_binding_ref: str | None = None,
) -> PostP6WorkerRetryTransitionResult:
    outbox_store = PostP6JsonlSchedulerOutboxStore(outbox_store_path)
    claim_store = PostP6JsonlSchedulerWorkerClaimStore(claim_store_path)
    transition_store = PostP6JsonlWorkerRetryTransitionStore(retry_transition_store_path)
    request_blockers = _execution_intent_blockers(
        provider_job_requested=provider_job_requested,
        protocol_run_requested=protocol_run_requested,
        episode_run_requested=episode_run_requested,
        claim_promotion_requested=claim_promotion_requested,
        graph_write_requested=graph_write_requested,
        canonical_mudo_mutation_requested=canonical_mudo_mutation_requested,
        execute_now=execute_now,
    )
    if max_transitions != 1:
        request_blockers = (*request_blockers, "retry_transition_batching_not_enabled")
    if request_blockers:
        receipt = _retry_transition_receipt(decision="blocked", reason_codes=request_blockers)
        return PostP6WorkerRetryTransitionResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            retry_transition_store_path=str(transition_store.path),
            receipts=(receipt,),
            blockers=({"reason_codes": list(request_blockers)},),
            blocked_count=1,
        )

    records = outbox_store.read_records()
    if not records:
        receipt = _retry_transition_receipt(decision="noop", reason_codes=("no_persisted_outbox_records",))
        return PostP6WorkerRetryTransitionResult(
            status="noop",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            retry_transition_store_path=str(transition_store.path),
            receipts=(receipt,),
        )

    requested_outbox = str(source_outbox_ref or "").strip()
    selected_record = None
    for record in records:
        if not requested_outbox or requested_outbox in {record.outbox_ref, record.source_outbox_ref}:
            selected_record = record
            break
    if selected_record is None:
        receipt = _retry_transition_receipt(decision="blocked", reason_codes=("missing_source_outbox_record",))
        return PostP6WorkerRetryTransitionResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            retry_transition_store_path=str(transition_store.path),
            receipts=(receipt,),
            blockers=({"source_outbox_ref": requested_outbox, "reason_codes": ["missing_source_outbox_record"]},),
            blocked_count=1,
        )

    claims = claim_store.read_claims()
    claim_by_outbox = {claim.source_outbox_ref: claim for claim in claims}
    selected_claim = claim_by_outbox.get(selected_record.outbox_ref)
    transition: Literal["persisted_to_claimed", "persisted_to_blocked", "persisted_to_retryable_pending"]
    reason_codes: tuple[str, ...]
    if worker_receipt_payload is not None:
        decision_value = str(worker_receipt_payload.get("decision") or "").strip()
        if decision_value == "blocked":
            transition = "persisted_to_blocked"
            raw_reasons = worker_receipt_payload.get("reason_codes") or ("worker_blocked",)
            reason_codes = tuple(str(reason) for reason in raw_reasons) or ("worker_blocked",)
        else:
            transition = "persisted_to_retryable_pending"
            reason_codes = ("retryable_pending_without_claim",)
    elif selected_claim is not None:
        transition = "persisted_to_claimed"
        reason_codes = ("retry_transition_recorded_from_worker_claim",)
    else:
        transition = "persisted_to_retryable_pending"
        reason_codes = ("retryable_pending_without_claim",)

    candidate = _retry_transition_receipt(
        decision="recorded",
        reason_codes=reason_codes,
        transition=transition,
        record=selected_record,
        claim=selected_claim,
        worker_receipt_payload=worker_receipt_payload,
        attempt_index=attempt_index,
        backoff_ms=backoff_ms,
        provider_from=provider_from,
        provider_to=provider_to,
        fallback_chain=fallback_chain,
        selection_receipt_ref=selection_receipt_ref,
        provider_binding_ref=provider_binding_ref,
    )
    existing_keys = {receipt.transition_key for receipt in transition_store.read_receipts()}
    if candidate.transition_key in existing_keys:
        duplicate = _retry_transition_receipt(
            decision="duplicate_transition_suppressed",
            reason_codes=("duplicate_retry_transition_suppressed",),
            transition=transition,
            record=selected_record,
            claim=selected_claim,
            worker_receipt_payload=worker_receipt_payload,
            provider_from=provider_from,
            provider_to=provider_to,
            fallback_chain=fallback_chain,
            selection_receipt_ref=selection_receipt_ref,
            provider_binding_ref=provider_binding_ref,
        )
        return PostP6WorkerRetryTransitionResult(
            status="duplicate_transition_suppressed",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            retry_transition_store_path=str(transition_store.path),
            receipts=(duplicate,),
            duplicate_count=1,
        )

    transition_store.append_receipts((candidate,))
    return PostP6WorkerRetryTransitionResult(
        status="recorded",
        outbox_store_path=str(outbox_store.path),
        claim_store_path=str(claim_store.path),
        retry_transition_store_path=str(transition_store.path),
        receipts=(candidate,),
        recorded_count=1,
    )


async def activate_post_p6_worker_handoff(
    *,
    handoff_ref: str,
    protocol_payload: Mapping[str, Any] | None = None,
    proposal_payload: Mapping[str, Any] | None = None,
    selection_requirements: Mapping[str, Any] | None = None,
    outbox_store_path: str | Path | None = None,
    claim_store_path: str | Path | None = None,
    activation_store_path: str | Path | None = None,
    lineage_store_path: str | Path | None = None,
    quetzal_verdict_payload: Mapping[str, Any] | None = None,
    backpressure_policy: Mapping[str, Any] | None = None,
    retry_policy: Mapping[str, Any] | None = None,
    provider_available: bool | None = None,
    source_authority: str = "command_kernel",
    checkpoint_dir: str | Path | None = None,
    dispatch_node: Any = None,
    request_metadata: Mapping[str, Any] | None = None,
    retry_transition_store_path: str | Path | None = None,
) -> PostP6HandoffActivationResult:
    outbox_store = PostP6JsonlSchedulerOutboxStore(outbox_store_path)
    claim_store = PostP6JsonlSchedulerWorkerClaimStore(claim_store_path)
    activation_store = PostP6JsonlHandoffActivationStore(activation_store_path)
    lineage_store = PostP6JsonlWorkerMUDOLineageStore(lineage_store_path)
    transition_store = PostP6JsonlWorkerRetryTransitionStore(retry_transition_store_path)

    existing_activations = activation_store.read_activations()
    if any(activation.source_handoff_ref == handoff_ref for activation in existing_activations):
        duplicate = _handoff_activation_receipt(
            decision="duplicate_activation_suppressed",
            reason_codes=("duplicate_handoff_activation_suppressed",),
        )
        return PostP6HandoffActivationResult(
            status="duplicate_activation_suppressed",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(duplicate,),
            duplicate_count=1,
        )

    selected_claim = next(
        (claim for claim in claim_store.read_claims() if claim.handoff_ref == handoff_ref),
        None,
    )
    if selected_claim is None:
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("missing_worker_claim_for_handoff",),
            blocker_code="missing_worker_claim_for_handoff",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["missing_worker_claim_for_handoff"],
                },
            ),
            blocked_count=1,
        )

    if source_authority != "command_kernel":
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("worker_direct_submit_blocked",),
            claim=selected_claim,
            blocker_code="worker_direct_submit_blocked",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["worker_direct_submit_blocked"],
                    "source_authority": source_authority,
                },
            ),
            blocked_count=1,
        )

    selected_record = next(
        (record for record in outbox_store.read_records() if record.outbox_ref == selected_claim.source_outbox_ref),
        None,
    )
    if selected_record is None:
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("missing_source_outbox_record",),
            claim=selected_claim,
            blocker_code="missing_source_outbox_record",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["missing_source_outbox_record"],
                },
            ),
            blocked_count=1,
        )

    if selected_claim.handoff_state != "approved_not_submitted":
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("handoff_state_not_submittable",),
            claim=selected_claim,
            blocker_code="handoff_state_not_submittable",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["handoff_state_not_submittable"],
                },
            ),
            blocked_count=1,
        )

    backpressure = _resolve_backpressure_policy(backpressure_policy)
    inflight_handoffs = {
        activation.source_handoff_ref
        for activation in existing_activations
        if activation.state == "submitted_to_canonical_authority"
    }
    pending_queue_handoffs = {
        claim.handoff_ref
        for claim in claim_store.read_claims()
        if claim.handoff_state == "approved_not_submitted"
        and claim.handoff_ref not in inflight_handoffs
        and claim.handoff_ref != handoff_ref
    }
    inflight_count = len(inflight_handoffs)
    queue_depth = len(pending_queue_handoffs)
    if backpressure["max_concurrent_submits"] and inflight_count >= backpressure["max_concurrent_submits"]:
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("submit_inflight_saturated",),
            claim=selected_claim,
            blocker_code="submit_inflight_saturated",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["submit_inflight_saturated"],
                    "inflight_count": inflight_count,
                    "queue_depth": queue_depth,
                },
            ),
            blocked_count=1,
            metrics=_build_hn_submit_metrics(inflight_count=inflight_count, queue_depth=queue_depth),
        )
    if backpressure["max_queue_depth"] and queue_depth >= backpressure["max_queue_depth"]:
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("submit_queue_full",),
            claim=selected_claim,
            blocker_code="submit_queue_full",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["submit_queue_full"],
                    "inflight_count": inflight_count,
                    "queue_depth": queue_depth,
                },
            ),
            blocked_count=1,
            metrics=_build_hn_submit_metrics(inflight_count=inflight_count, queue_depth=queue_depth),
        )

    verdict = dict(quetzal_verdict_payload or {})
    verdict_decision = str(verdict.get("decision") or verdict.get("status") or "").strip().lower()
    quetzal_approved = bool(verdict.get("approved")) or verdict_decision in {"approved", "allow", "allowed", "passed", "pass"}
    if not quetzal_approved:
        blocker_code = "quetzal_submit_blocked" if verdict else "quetzal_submit_regate_missing"
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=(blocker_code,),
            claim=selected_claim,
            blocker_code=blocker_code,
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": [blocker_code],
                    "quetzal_verdict_payload": verdict,
                },
            ),
            blocked_count=1,
        )

    retry_config = _resolve_retry_policy(retry_policy)
    retry_receipts_before = _handoff_retry_receipts(transition_store, handoff_ref=handoff_ref)
    provider_binding = _select_provider_binding(
        handoff_ref=handoff_ref,
        budget_ref=selected_claim.budget_ref,
        protocol_payload=protocol_payload,
        request_metadata=request_metadata,
        selection_requirements=selection_requirements,
        retry_receipts_before=retry_receipts_before,
    )

    def _transient_retry_result(blocker_code: str) -> PostP6HandoffActivationResult | None:
        if not bool(retry_config.get("enabled")):
            return None
        if blocker_code not in set(retry_config["transient_set"]):
            return None
        attempt_index = len(retry_receipts_before) + 1
        if attempt_index > int(retry_config["max_attempts"]):
            transition_result = record_post_p6_worker_retry_transition(
                outbox_store_path=outbox_store.path,
                claim_store_path=claim_store.path,
                retry_transition_store_path=transition_store.path,
                source_outbox_ref=selected_record.outbox_ref,
                worker_receipt_payload={
                    "decision": "blocked",
                    "receipt_ref": f"receipt://hn/r2/dead-letter/{_digest({'handoff_ref': handoff_ref, 'blocker_code': blocker_code, 'attempt_index': attempt_index})}",
                    "handoff_ref": handoff_ref,
                    "reason_codes": [blocker_code],
                },
                attempt_index=attempt_index,
                backoff_ms=0,
            )
            blocked = _handoff_activation_receipt(
                decision="blocked",
                reason_codes=(blocker_code, "submit_dead_lettered"),
                claim=selected_claim,
                blocker_code=blocker_code,
                source_authority=source_authority,
            )
            return PostP6HandoffActivationResult(
                status="dead_letter",
                outbox_store_path=str(outbox_store.path),
                claim_store_path=str(claim_store.path),
                activation_store_path=str(activation_store.path),
                lineage_store_path=str(lineage_store.path),
                receipts=(blocked,),
                blockers=(
                    {
                        "handoff_ref": handoff_ref,
                        "reason_codes": [blocker_code, "submit_dead_lettered"],
                        "attempt_index": attempt_index,
                        "dead_lettered": True,
                    },
                ),
                retry_transition_receipt_refs=tuple(receipt.receipt_ref for receipt in transition_result.receipts),
                blocked_count=1,
                dead_letter_count=1,
                metrics=_build_hn_submit_metrics(
                    inflight_count=inflight_count,
                    queue_depth=queue_depth,
                    retry_attempt_count=attempt_index,
                    dead_letter_count=1,
                ),
            )
        backoff_ms = _compute_backoff_ms(
            handoff_ref=handoff_ref,
            attempt_index=attempt_index,
            base_ms=int(retry_config["base_ms"]),
            factor=float(retry_config["factor"]),
            jitter=float(retry_config["jitter"]),
        )
        transition_result = record_post_p6_worker_retry_transition(
            outbox_store_path=outbox_store.path,
            claim_store_path=claim_store.path,
            retry_transition_store_path=transition_store.path,
            source_outbox_ref=selected_record.outbox_ref,
            worker_receipt_payload={
                "decision": "retrying",
                "receipt_ref": f"receipt://hn/r2/retry/{_digest({'handoff_ref': handoff_ref, 'blocker_code': blocker_code, 'attempt_index': attempt_index})}",
                "handoff_ref": handoff_ref,
                "reason_codes": [blocker_code],
            },
            attempt_index=attempt_index,
            backoff_ms=backoff_ms,
        )
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=(blocker_code, "retry_scheduled"),
            claim=selected_claim,
            blocker_code=blocker_code,
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="retrying",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": [blocker_code, "retry_scheduled"],
                    "attempt_index": attempt_index,
                    "backoff_ms": backoff_ms,
                    "dead_lettered": False,
                },
            ),
            retry_transition_receipt_refs=tuple(receipt.receipt_ref for receipt in transition_result.receipts),
            blocked_count=1,
            retry_count=1,
            metrics=_build_hn_submit_metrics(
                inflight_count=inflight_count,
                queue_depth=queue_depth,
                retry_attempt_count=attempt_index,
                backoff_ms=backoff_ms,
            ),
        )

    binding_status = str(provider_binding.get("status") or "").strip()
    if binding_status == "provider_selection_failed":
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("provider_selection_failed",),
            claim=selected_claim,
            blocker_code="provider_selection_failed",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["provider_selection_failed"],
                    "provider_candidates": list(provider_binding.get("provider_candidates") or ()),
                },
            ),
            blocked_count=1,
            metrics=_build_hn_submit_metrics(inflight_count=inflight_count, queue_depth=queue_depth),
        )
    if binding_status == "all_providers_unavailable":
        transition_attempt = len(retry_receipts_before) + 1
        transition_result = record_post_p6_worker_retry_transition(
            outbox_store_path=outbox_store.path,
            claim_store_path=claim_store.path,
            retry_transition_store_path=transition_store.path,
            source_outbox_ref=selected_record.outbox_ref,
            worker_receipt_payload={
                "decision": "blocked",
                "receipt_ref": f"receipt://hn/r3/all-providers-unavailable/{_digest({'handoff_ref': handoff_ref, 'attempt_index': transition_attempt})}",
                "handoff_ref": handoff_ref,
                "reason_codes": ["all_providers_unavailable"],
                "fallback_chain": list(provider_binding.get("fallback_chain") or ()),
            },
            attempt_index=transition_attempt,
            backoff_ms=0,
            fallback_chain=tuple(provider_binding.get("fallback_chain") or ()),
        )
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("all_providers_unavailable", "submit_dead_lettered"),
            claim=selected_claim,
            blocker_code="all_providers_unavailable",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="dead_letter",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["all_providers_unavailable", "submit_dead_lettered"],
                    "fallback_chain": list(provider_binding.get("fallback_chain") or ()),
                },
            ),
            retry_transition_receipt_refs=tuple(receipt.receipt_ref for receipt in transition_result.receipts),
            blocked_count=1,
            dead_letter_count=1,
            metrics=_build_hn_submit_metrics(
                inflight_count=inflight_count,
                queue_depth=queue_depth,
                retry_attempt_count=transition_attempt,
                dead_letter_count=1,
            ),
        )

    selected_provider = str(provider_binding.get("selected_provider") or "").strip()
    fallback_chain = tuple(str(item).strip() for item in provider_binding.get("fallback_chain") or () if str(item).strip())
    selection_receipt_ref = str(provider_binding.get("selection_receipt_ref") or "").strip() or None
    provider_binding_ref = str(provider_binding.get("provider_binding_ref") or "").strip() or None
    failover_chain = tuple(provider_binding.get("failover_chain") or ())
    provider_is_available = _provider_available(BackendSelector(), selected_provider, provider_available)
    if not provider_is_available:
        next_provider = None
        if selected_provider in fallback_chain:
            selected_index = fallback_chain.index(selected_provider)
            if selected_index + 1 < len(fallback_chain):
                next_provider = fallback_chain[selected_index + 1]
        if next_provider is None:
            legacy_retry_result = None
            if not isinstance(provider_available, Mapping):
                legacy_retry_result = _transient_retry_result("provider_unavailable")
            if legacy_retry_result is not None:
                return legacy_retry_result
            transition_attempt = len(retry_receipts_before) + 1
            transition_result = record_post_p6_worker_retry_transition(
                outbox_store_path=outbox_store.path,
                claim_store_path=claim_store.path,
                retry_transition_store_path=transition_store.path,
                source_outbox_ref=selected_record.outbox_ref,
                worker_receipt_payload={
                    "decision": "blocked",
                    "receipt_ref": f"receipt://hn/r3/provider-pool-exhausted/{_digest({'handoff_ref': handoff_ref, 'attempt_index': transition_attempt, 'provider_from': selected_provider})}",
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["all_providers_unavailable"],
                    "provider_from": selected_provider,
                    "selection_receipt_ref": selection_receipt_ref,
                    "provider_binding_ref": provider_binding_ref,
                    "fallback_chain": list(fallback_chain),
                },
                attempt_index=transition_attempt,
                backoff_ms=0,
                provider_from=selected_provider,
                fallback_chain=fallback_chain,
                selection_receipt_ref=selection_receipt_ref,
                provider_binding_ref=provider_binding_ref,
            )
            blocked = _handoff_activation_receipt(
                decision="blocked",
                reason_codes=("all_providers_unavailable", "submit_dead_lettered"),
                claim=selected_claim,
                blocker_code="all_providers_unavailable",
                source_authority=source_authority,
            )
            return PostP6HandoffActivationResult(
                status="dead_letter",
                outbox_store_path=str(outbox_store.path),
                claim_store_path=str(claim_store.path),
                activation_store_path=str(activation_store.path),
                lineage_store_path=str(lineage_store.path),
                receipts=(blocked,),
                blockers=(
                    {
                        "handoff_ref": handoff_ref,
                        "reason_codes": ["all_providers_unavailable", "submit_dead_lettered"],
                        "selected_provider": selected_provider,
                        "fallback_chain": list(fallback_chain),
                    },
                ),
                retry_transition_receipt_refs=tuple(receipt.receipt_ref for receipt in transition_result.receipts),
                blocked_count=1,
                dead_letter_count=1,
                metrics=_build_hn_submit_metrics(
                    inflight_count=inflight_count,
                    queue_depth=queue_depth,
                    retry_attempt_count=transition_attempt,
                    dead_letter_count=1,
                ),
            )
        transition_attempt = len(retry_receipts_before) + 1
        backoff_ms = _compute_backoff_ms(
            handoff_ref=handoff_ref,
            attempt_index=transition_attempt,
            base_ms=int(retry_config["base_ms"]),
            factor=float(retry_config["factor"]),
            jitter=float(retry_config["jitter"]),
        )
        transition_result = record_post_p6_worker_retry_transition(
            outbox_store_path=outbox_store.path,
            claim_store_path=claim_store.path,
            retry_transition_store_path=transition_store.path,
            source_outbox_ref=selected_record.outbox_ref,
            worker_receipt_payload={
                "decision": "retrying",
                "receipt_ref": f"receipt://hn/r3/provider-failover/{_digest({'handoff_ref': handoff_ref, 'attempt_index': transition_attempt, 'provider_from': selected_provider, 'provider_to': next_provider})}",
                "handoff_ref": handoff_ref,
                "reason_codes": ["provider_unavailable"],
                "provider_from": selected_provider,
                "provider_to": next_provider,
                "selection_receipt_ref": selection_receipt_ref,
                "provider_binding_ref": provider_binding_ref,
                "fallback_chain": list(fallback_chain),
            },
            attempt_index=transition_attempt,
            backoff_ms=backoff_ms,
            provider_from=selected_provider,
            provider_to=next_provider,
            fallback_chain=fallback_chain,
            selection_receipt_ref=selection_receipt_ref,
            provider_binding_ref=provider_binding_ref,
        )
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("provider_unavailable", "retry_scheduled"),
            claim=selected_claim,
            blocker_code="provider_unavailable",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="retrying",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["provider_unavailable", "retry_scheduled"],
                    "selected_provider": selected_provider,
                    "next_provider": next_provider,
                    "fallback_chain": list(fallback_chain),
                    "backoff_ms": backoff_ms,
                },
            ),
            retry_transition_receipt_refs=tuple(receipt.receipt_ref for receipt in transition_result.receipts),
            blocked_count=1,
            retry_count=1,
            metrics=_build_hn_submit_metrics(
                inflight_count=inflight_count,
                queue_depth=queue_depth,
                retry_attempt_count=transition_attempt,
                backoff_ms=backoff_ms,
            ),
        )

    from mica.drivers.execution.protocol_executor import execute_protocol_executor_request
    from mica.protocol_proposal_compiler import ProposalPromotionError, promote_proposal_to_protocol
    from mica.protocol_drafts import build_protocol_executor_request
    from mica_q.protocol_jsonld_validator import derive_protocol_execution_frontier, validate_protocol_jsonld

    protocol_document = None
    proposal_promotion_receipt_id: str | None = None
    if isinstance(protocol_payload, Mapping):
        protocol_document = validate_protocol_jsonld(dict(protocol_payload))
    elif isinstance(proposal_payload, Mapping):
        try:
            protocol_document, promotion_receipt = promote_proposal_to_protocol(
                dict(proposal_payload),
                approver="post_p6_handoff_submit",
            )
            proposal_promotion_receipt_id = promotion_receipt.receipt_id
        except ProposalPromotionError as exc:
            blocked = _handoff_activation_receipt(
                decision="blocked",
                reason_codes=("proposal_compilation_rejected",),
                claim=selected_claim,
                blocker_code="proposal_compilation_rejected",
                source_authority=source_authority,
            )
            return PostP6HandoffActivationResult(
                status="blocked",
                outbox_store_path=str(outbox_store.path),
                claim_store_path=str(claim_store.path),
                activation_store_path=str(activation_store.path),
                lineage_store_path=str(lineage_store.path),
                receipts=(blocked,),
                blockers=(
                    {
                        "handoff_ref": handoff_ref,
                        "reason_codes": ["proposal_compilation_rejected"],
                        "validation_errors": list(exc.receipt.validation_debug.get("errors") or ()),
                        "promotion_receipt_id": exc.receipt.receipt_id,
                    },
                ),
                blocked_count=1,
            )
    else:
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("handoff_contract_drift",),
            claim=selected_claim,
            blocker_code="handoff_contract_drift",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["handoff_contract_drift"],
                },
            ),
            blocked_count=1,
        )

    assert protocol_document is not None
    frontier = derive_protocol_execution_frontier(protocol_document, ())
    normalized_request_metadata = dict(protocol_document.metadata or {})
    for key, value in dict(request_metadata or {}).items():
        if value not in (None, ""):
            normalized_request_metadata.setdefault(key, value)
    normalized_request_metadata.setdefault(
        "owner_user_id",
        str(normalized_request_metadata.get("owner_user_id") or normalized_request_metadata.get("user_id") or "post-p6-worker"),
    )
    normalized_request_metadata.setdefault(
        "user_id",
        str(normalized_request_metadata.get("user_id") or normalized_request_metadata.get("owner_user_id") or "post-p6-worker"),
    )
    normalized_request_metadata.setdefault("workspace_id", str(normalized_request_metadata.get("workspace_id") or "workspace-post-p6"))
    normalized_request_metadata.setdefault("study_id", str(normalized_request_metadata.get("study_id") or "study-post-p6"))
    executor_request = build_protocol_executor_request(
        protocol_document,
        frontier,
        request_metadata=normalized_request_metadata,
    )

    try:
        outcome = await execute_protocol_executor_request(
            executor_request,
            checkpoint_dir=checkpoint_dir,
            dispatch_node=dispatch_node,
        )
    except Exception as exc:
        retry_result = _transient_retry_result("protocol_executor_unavailable")
        if retry_result is not None:
            return retry_result
        blocked = _handoff_activation_receipt(
            decision="blocked",
            reason_codes=("protocol_executor_unavailable",),
            claim=selected_claim,
            blocker_code="protocol_executor_unavailable",
            source_authority=source_authority,
        )
        return PostP6HandoffActivationResult(
            status="blocked",
            outbox_store_path=str(outbox_store.path),
            claim_store_path=str(claim_store.path),
            activation_store_path=str(activation_store.path),
            lineage_store_path=str(lineage_store.path),
            receipts=(blocked,),
            blockers=(
                {
                    "handoff_ref": handoff_ref,
                    "reason_codes": ["protocol_executor_unavailable"],
                    "error": str(exc),
                },
            ),
            blocked_count=1,
            metrics=_build_hn_submit_metrics(inflight_count=inflight_count, queue_depth=queue_depth),
        )

    run_receipt = outcome.run_receipt
    run_ref = f"hn-run://{run_receipt.run_id}"
    run_receipt_ref = _stable_ref(
        "receipt://post-p6/handoff-protocol-run/",
        {"run_id": run_receipt.run_id, "protocol_id": run_receipt.protocol_id},
    )
    run_receipt_bundle_ref = _stable_ref(
        "receipt-bundle://post-p6/handoff-protocol-run/",
        {
            "run_id": run_receipt.run_id,
            "protocol_id": run_receipt.protocol_id,
            "node_receipt_ids": tuple(run_receipt.emitted_node_receipt_ids),
        },
    )
    quetzal_regate_ref = str(
        verdict.get("receipt_ref")
        or verdict.get("quetzal_gate_ref")
        or _stable_ref(
            "receipt://post-p6/handoff-submit-quetzal/",
            {"handoff_ref": handoff_ref, "decision": verdict_decision or "approved"},
        )
    )
    creation_receipt_ref = _stable_ref(
        "receipt://post-p6/handoff-activation/",
        {"handoff_ref": handoff_ref, "run_id": run_receipt.run_id, "decision": "submitted"},
    )
    activation = PostP6HandoffActivation(
        activation_ref=_stable_ref(
            "activation://post-p6/handoff/",
            {"handoff_ref": handoff_ref, "run_id": run_receipt.run_id},
        ),
        source_handoff_ref=selected_claim.handoff_ref,
        source_outbox_ref=selected_claim.source_outbox_ref,
        source_claim_ref=selected_claim.claim_ref,
        source_store_record_ref=selected_claim.source_store_record_ref,
        idempotency_key=selected_claim.idempotency_key,
        state="submitted_to_canonical_authority",
        source_authority=source_authority,
        quetzal_regate_ref=quetzal_regate_ref,
        budget_decision_ref=selected_claim.budget_decision_ref,
        circuit_decision_ref=selected_claim.circuit_decision_ref,
        harness_run_request_ref=_stable_ref(
            "run-request://post-p6/handoff/",
            {"handoff_ref": handoff_ref, "protocol_id": executor_request.protocol_id},
        ),
        run_ref=run_ref,
        run_receipt_ref=run_receipt_ref,
        run_receipt_bundle_ref=run_receipt_bundle_ref,
        budget_ref=selected_claim.budget_ref,
        provider_binding_ref=provider_binding_ref,
        selected_provider=selected_provider,
        selection_receipt_ref=selection_receipt_ref,
        fallback_chain=fallback_chain,
        failover_chain=failover_chain,
        noncanonical_branch_ref=_stable_ref(
            "mudo-branch://post-p6/scheduler-worker/",
            {"handoff_ref": handoff_ref, "run_id": run_receipt.run_id},
        ),
        evidence_refs=tuple(
            [
                selected_claim.worker_receipt_ref,
                quetzal_regate_ref,
                *run_receipt.evidence_refs,
                *([selection_receipt_ref] if selection_receipt_ref else []),
                *[item.receipt_ref for item in retry_receipts_before if item.receipt_ref],
                *([proposal_promotion_receipt_id] if proposal_promotion_receipt_id else []),
            ]
        ),
        artifact_refs=tuple(run_receipt.artifact_refs),
        creation_receipt_ref=creation_receipt_ref,
        created_at=_now(),
    )
    activation_store.append_activations((activation,))

    lineage_event = _activation_event_from_claim(
        selected_claim,
        selected_record,
        run_receipt_ref=run_receipt_ref,
        run_receipt_bundle_ref=run_receipt_bundle_ref,
    )
    lineage_store.append_events((lineage_event,))

    receipt = _handoff_activation_receipt(
        decision="submitted",
        reason_codes=("submitted_via_protocol_executor",),
        activation=activation,
        claim=selected_claim,
        harness_run_request_ref=activation.harness_run_request_ref,
        run_ref=run_ref,
        run_receipt_ref=run_receipt_ref,
        run_receipt_bundle_ref=run_receipt_bundle_ref,
        noncanonical_branch_ref=lineage_event.noncanonical_branch_ref,
        source_authority=source_authority,
    )
    return PostP6HandoffActivationResult(
        status="submitted",
        outbox_store_path=str(outbox_store.path),
        claim_store_path=str(claim_store.path),
        activation_store_path=str(activation_store.path),
        lineage_store_path=str(lineage_store.path),
        activations=(activation,),
        receipts=(receipt,),
        lineage_events=(lineage_event,),
        submitted_count=1,
        protocol_runs_created=1,
        execution_started=True,
        metrics=_build_hn_submit_metrics(inflight_count=inflight_count, queue_depth=queue_depth),
    )


def _record_from_entry(
    entry: P6ScheduledReviewOutboxEntry,
    *,
    quetzal_gate_ref: str,
    budget_ref: str,
) -> PostP6DurableSchedulerOutboxRecord:
    created_at = _now()
    outbox_ref = _stable_ref("outbox://post-p6/scheduler-review/", {
        "source_outbox_ref": entry.outbox_ref,
        "idempotency_key": entry.idempotency_key,
        "quetzal_gate_ref": quetzal_gate_ref,
        "budget_ref": budget_ref,
    })
    creation_receipt_ref = _stable_ref("receipt://post-p6/scheduler-outbox/", {
        "decision": "persisted",
        "outbox_ref": outbox_ref,
        "idempotency_key": entry.idempotency_key,
        "created_at": created_at,
    })
    return PostP6DurableSchedulerOutboxRecord(
        store_record_ref=_stable_ref("event://post-p6/scheduler-outbox/", {
            "outbox_ref": outbox_ref,
            "idempotency_key": entry.idempotency_key,
        }),
        source_outbox_ref=entry.outbox_ref,
        outbox_ref=outbox_ref,
        idempotency_key=entry.idempotency_key,
        attempt_key=entry.attempt_key,
        review_kind=entry.review_kind,
        mode=entry.mode,
        attempt_count=entry.attempt_count,
        signal_ref=entry.signal_ref,
        source_receipt_refs=entry.source_receipt_refs,
        target_refs=entry.target_refs,
        proposal_ref=entry.proposal_ref,
        proposal_receipt_ref=entry.proposal_receipt_ref,
        quetzal_gate_ref=quetzal_gate_ref,
        budget_ref=budget_ref,
        retry_policy=entry.retry_policy.model_dump(mode="json"),
        retry_state={
            "attempt_count": entry.attempt_count,
            "max_attempts": entry.retry_policy.max_attempts,
            "base_delay_seconds": entry.retry_policy.base_delay_seconds,
            "max_delay_seconds": entry.retry_policy.max_delay_seconds,
            "claimed": False,
        },
        created_at=created_at,
        creation_receipt_ref=creation_receipt_ref,
    )


def persist_post_p6_scheduled_review_outbox(
    schedule: P6ScheduledReviewResult,
    *,
    quetzal_gate_ref: str | None,
    budget_ref: str | None,
    store_path: str | Path | None = None,
) -> PostP6DurableSchedulerOutboxResult:
    store = PostP6JsonlSchedulerOutboxStore(store_path)
    existing = store.read_records()
    existing_keys = {record.idempotency_key for record in existing}
    records: list[PostP6DurableSchedulerOutboxRecord] = []
    receipts: list[PostP6DurableSchedulerOutboxReceipt] = []
    blockers: list[dict[str, Any]] = []

    if not schedule.outbox_entries:
        receipt = PostP6DurableSchedulerOutboxReceipt(
            receipt_ref=_stable_ref("receipt://post-p6/scheduler-outbox/", {
                "decision": "noop",
                "reason_codes": ("no_projected_outbox_entries",),
                "schedule_status": schedule.status,
            }),
            decision="noop",
            reason_codes=("no_projected_outbox_entries",),
            quetzal_gate_ref=quetzal_gate_ref,
            budget_ref=budget_ref,
        )
        return PostP6DurableSchedulerOutboxResult(
            status="noop",
            store_path=str(store.path),
            receipts=(receipt,),
        )

    quetzal_ref = str(quetzal_gate_ref or "").strip()
    budget_value = str(budget_ref or "").strip()
    for entry in schedule.outbox_entries:
        entry_blockers = _entry_blockers(entry, quetzal_gate_ref=quetzal_ref, budget_ref=budget_value)
        if entry_blockers:
            blockers.append({
                "outbox_ref": entry.outbox_ref,
                "idempotency_key": entry.idempotency_key,
                "reason_codes": list(entry_blockers),
            })
            receipts.append(_block_receipt(
                reason_codes=entry_blockers,
                entry=entry,
                quetzal_gate_ref=quetzal_ref or None,
                budget_ref=budget_value or None,
            ))
            continue
        if entry.idempotency_key in existing_keys:
            receipt = PostP6DurableSchedulerOutboxReceipt(
                receipt_ref=_stable_ref("receipt://post-p6/scheduler-outbox/", {
                    "decision": "duplicate_suppressed",
                    "idempotency_key": entry.idempotency_key,
                    "outbox_ref": entry.outbox_ref,
                }),
                decision="duplicate_suppressed",
                reason_codes=("duplicate_idempotency_key_suppressed",),
                outbox_ref=entry.outbox_ref,
                idempotency_key=entry.idempotency_key,
                source_receipt_refs=entry.source_receipt_refs,
                proposal_ref=entry.proposal_ref,
                quetzal_gate_ref=quetzal_ref,
                budget_ref=budget_value,
            )
            receipts.append(receipt)
            continue

        record = _record_from_entry(entry, quetzal_gate_ref=quetzal_ref, budget_ref=budget_value)
        records.append(record)
        existing_keys.add(entry.idempotency_key)
        receipts.append(PostP6DurableSchedulerOutboxReceipt(
            receipt_ref=record.creation_receipt_ref,
            decision="persisted",
            reason_codes=("durable_scheduler_outbox_record_persisted",),
            outbox_ref=record.outbox_ref,
            store_record_ref=record.store_record_ref,
            idempotency_key=record.idempotency_key,
            source_receipt_refs=record.source_receipt_refs,
            proposal_ref=record.proposal_ref,
            quetzal_gate_ref=record.quetzal_gate_ref,
            budget_ref=record.budget_ref,
        ))

    store.append_records(records)
    persisted_count = len(records)
    duplicate_count = sum(receipt.decision == "duplicate_suppressed" for receipt in receipts)
    blocked_count = sum(receipt.decision == "blocked" for receipt in receipts)
    if persisted_count and not (duplicate_count or blocked_count):
        status: Literal["persisted", "noop", "blocked", "duplicate_suppressed", "partially_persisted"] = "persisted"
    elif persisted_count:
        status = "partially_persisted"
    elif blocked_count:
        status = "blocked"
    elif duplicate_count:
        status = "duplicate_suppressed"
    else:
        status = "noop"
    return PostP6DurableSchedulerOutboxResult(
        status=status,
        store_path=str(store.path),
        records=tuple(records),
        receipts=tuple(receipts),
        blockers=tuple(blockers),
        persisted_count=persisted_count,
        duplicate_count=duplicate_count,
        blocked_count=blocked_count,
    )
