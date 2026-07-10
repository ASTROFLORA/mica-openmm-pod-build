from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Literal, Mapping

from pydantic import BaseModel, Field, model_validator


P5_SCALE_READINESS_SCHEMA_ID = "mica.project_tolomeo.p5.proteome_scale_readiness.v1"
P5_SCALE_OUTBOX_SCHEMA_ID = "mica.project_tolomeo.p5.proteome_scale_outbox.v1"


class P5BudgetEnvelope(BaseModel):
    currency: Literal["USD"] = "USD"
    status: Literal["known", "unknown"]
    max_total_cost_usd: Decimal | None = Field(default=None, ge=0)
    estimated_total_cost_usd: Decimal | None = Field(default=None, ge=0)
    is_estimated: bool
    estimate_source: str | None = None

    @model_validator(mode="after")
    def _validate_known_budget(self) -> "P5BudgetEnvelope":
        if self.status == "known":
            if self.max_total_cost_usd is None or self.estimated_total_cost_usd is None:
                raise ValueError("known budget requires max and estimated total cost")
            if not str(self.estimate_source or "").strip():
                raise ValueError("known budget requires estimate_source")
        return self


class P5ScaleCandidate(BaseModel):
    candidate_ref: str
    protocol_ref: str
    model_ref: str
    provider: str
    provider_status: Literal["available", "unavailable", "unknown"] = "unknown"
    expensive_path: bool = True
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    raw_payload_embedded: bool = False

    @model_validator(mode="after")
    def _validate_refs_only(self) -> "P5ScaleCandidate":
        for field_name in ("candidate_ref", "protocol_ref", "model_ref", "provider"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"scale candidate requires {field_name}")
        if self.raw_payload_embedded:
            raise ValueError("scale candidate must remain refs-only")
        return self


class P5ScaleRetryPolicy(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=8)
    base_delay_seconds: int = Field(default=30, ge=1, le=3600)
    max_delay_seconds: int = Field(default=300, ge=1, le=86400)
    retryable_reason_codes: tuple[str, ...] = ("provider_unavailable",)

    @model_validator(mode="after")
    def _validate_delay_cap(self) -> "P5ScaleRetryPolicy":
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        return self


class P5ScaleOutboxEntry(BaseModel):
    schema_id: str = P5_SCALE_OUTBOX_SCHEMA_ID
    outbox_ref: str
    idempotency_key: str
    candidate_ref: str
    protocol_ref: str
    model_ref: str
    provider: str
    state: Literal["proposed", "blocked", "provider_unavailable", "duplicate_suppressed"]
    attempt_count: int = 0
    retry_policy: P5ScaleRetryPolicy
    payload_refs: tuple[str, ...] = ()
    submission_authority: Literal["command_kernel_protocol_runtime"] = "command_kernel_protocol_runtime"
    provider_job_created: bool = False
    dispatched: bool = False


class P5ScaleReadinessReceipt(BaseModel):
    receipt_ref: str
    receipt_type: Literal["P5ProteomeScaleReadinessReceipt"] = "P5ProteomeScaleReadinessReceipt"
    candidate_ref: str
    decision: Literal["proposed", "blocked", "provider_unavailable", "duplicate_suppressed"]
    reason_codes: tuple[str, ...]
    retryable: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    provider_job_created: bool = False
    outbox_dispatch_performed: bool = False


class P5ProteomeScaleReadinessResult(BaseModel):
    schema_id: str = P5_SCALE_READINESS_SCHEMA_ID
    p5_id: str
    status: Literal["ready_for_dry_run", "partially_blocked", "blocked"]
    execution_mode: Literal["dry_run"] = "dry_run"
    budget: P5BudgetEnvelope | None = None
    retry_policy: P5ScaleRetryPolicy
    candidates: tuple[P5ScaleCandidate, ...]
    outbox_entries: tuple[P5ScaleOutboxEntry, ...]
    receipts: tuple[P5ScaleReadinessReceipt, ...]
    blockers: tuple[Dict[str, Any], ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    idempotency_keys: tuple[str, ...] = ()
    proposed_count: int = 0
    blocked_count: int = 0
    duplicate_count: int = 0
    provider_unavailable_count: int = 0
    provider_jobs_created: int = 0
    outbox_dispatch_performed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_ref(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}{digest}"


def _block(code: str, message: str, *, candidate_ref: str | None = None) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": code == "provider_unavailable",
        "details": {"candidate_ref": candidate_ref} if candidate_ref else {},
    }


def _coerce_candidates(raw: Any) -> tuple[P5ScaleCandidate, ...]:
    values = raw if isinstance(raw, list) else []
    candidates: list[P5ScaleCandidate] = []
    for value in values:
        if isinstance(value, P5ScaleCandidate):
            candidates.append(value)
        elif isinstance(value, Mapping):
            candidates.append(P5ScaleCandidate(**dict(value)))
        else:
            raise ValueError("scale candidates must be objects")
    return tuple(candidates)


def build_p5_proteome_scale_readiness(
    packet: Mapping[str, Any],
    *,
    candidates_payload: Any,
    budget_payload: Mapping[str, Any] | None = None,
    retry_policy_payload: Mapping[str, Any] | None = None,
) -> P5ProteomeScaleReadinessResult:
    p5_id = str(packet.get("p5_id") or "unknown-p5").strip()
    candidates = _coerce_candidates(candidates_payload)
    budget = P5BudgetEnvelope(**dict(budget_payload)) if budget_payload else None
    retry_policy = P5ScaleRetryPolicy(**dict(retry_policy_payload or {}))
    blockers: list[Dict[str, Any]] = []
    entries: list[P5ScaleOutboxEntry] = []
    receipts: list[P5ScaleReadinessReceipt] = []
    seen_keys: set[str] = set()

    if not candidates:
        blockers.append(_block("missing_batch_candidates", "Proteome-scale dry-run requires at least one candidate."))

    for candidate in candidates:
        idempotency_key = hashlib.sha256(
            _stable_json({
                "p5_id": p5_id,
                "candidate_ref": candidate.candidate_ref,
                "protocol_ref": candidate.protocol_ref,
                "model_ref": candidate.model_ref,
                "provider": candidate.provider,
            }).encode("utf-8")
        ).hexdigest()
        reason_codes: list[str] = []
        retryable = False

        if idempotency_key in seen_keys:
            state = "duplicate_suppressed"
            reason_codes.append("duplicate_submission_suppressed")
        elif candidate.provider_status == "unavailable":
            state = "provider_unavailable"
            reason_codes.append("provider_unavailable")
            retryable = True
            blockers.append(_block(
                "provider_unavailable",
                "Provider preflight reported unavailable; no job was created.",
                candidate_ref=candidate.candidate_ref,
            ))
        elif candidate.provider_status == "unknown":
            state = "blocked"
            reason_codes.append("provider_status_unknown")
            blockers.append(_block(
                "provider_status_unknown",
                "Provider status must be known before a submission proposal can be emitted.",
                candidate_ref=candidate.candidate_ref,
            ))
        elif candidate.expensive_path and (budget is None or budget.status == "unknown"):
            state = "blocked"
            reason_codes.append("expensive_path_budget_unknown")
            blockers.append(_block(
                "expensive_path_budget_unknown",
                "Expensive paths fail closed when the batch budget is missing or unknown.",
                candidate_ref=candidate.candidate_ref,
            ))
        elif (
            candidate.expensive_path
            and budget is not None
            and budget.max_total_cost_usd is not None
            and budget.estimated_total_cost_usd is not None
            and budget.estimated_total_cost_usd > budget.max_total_cost_usd
        ):
            state = "blocked"
            reason_codes.append("estimated_cost_exceeds_budget")
            blockers.append(_block(
                "estimated_cost_exceeds_budget",
                "Estimated batch cost exceeds the approved maximum.",
                candidate_ref=candidate.candidate_ref,
            ))
        else:
            state = "proposed"
            reason_codes.append("dry_run_submission_proposal_ready")

        seen_keys.add(idempotency_key)
        outbox_ref = _stable_ref(
            "outbox://p5/proteome-scale/",
            {"idempotency_key": idempotency_key, "state": state},
        )
        receipt_ref = _stable_ref(
            "receipt://p5/proteome-scale-readiness/",
            {"outbox_ref": outbox_ref, "state": state, "reason_codes": reason_codes},
        )
        entries.append(P5ScaleOutboxEntry(
            outbox_ref=outbox_ref,
            idempotency_key=idempotency_key,
            candidate_ref=candidate.candidate_ref,
            protocol_ref=candidate.protocol_ref,
            model_ref=candidate.model_ref,
            provider=candidate.provider,
            state=state,
            retry_policy=retry_policy,
            payload_refs=tuple(sorted({candidate.candidate_ref, *candidate.artifact_refs, *candidate.evidence_refs})),
        ))
        receipts.append(P5ScaleReadinessReceipt(
            receipt_ref=receipt_ref,
            candidate_ref=candidate.candidate_ref,
            decision=state,
            reason_codes=tuple(reason_codes),
            retryable=retryable,
        ))

    proposed_count = sum(entry.state == "proposed" for entry in entries)
    blocked_count = sum(entry.state == "blocked" for entry in entries)
    duplicate_count = sum(entry.state == "duplicate_suppressed" for entry in entries)
    unavailable_count = sum(entry.state == "provider_unavailable" for entry in entries)
    if not entries or proposed_count == 0:
        status = "blocked"
    elif blocked_count or unavailable_count:
        status = "partially_blocked"
    else:
        status = "ready_for_dry_run"

    artifact_ref = _stable_ref(
        "artifact://p5/proteome-scale-readiness/",
        {
            "p5_id": p5_id,
            "budget": budget.model_dump(mode="json") if budget else None,
            "outbox": [entry.model_dump(mode="json") for entry in entries],
        },
    )
    return P5ProteomeScaleReadinessResult(
        p5_id=p5_id,
        status=status,
        budget=budget,
        retry_policy=retry_policy,
        candidates=candidates,
        outbox_entries=tuple(entries),
        receipts=tuple(receipts),
        blockers=tuple(blockers),
        artifact_refs=(artifact_ref,),
        receipt_refs=tuple(receipt.receipt_ref for receipt in receipts),
        idempotency_keys=tuple(entry.idempotency_key for entry in entries),
        proposed_count=proposed_count,
        blocked_count=blocked_count,
        duplicate_count=duplicate_count,
        provider_unavailable_count=unavailable_count,
    )
