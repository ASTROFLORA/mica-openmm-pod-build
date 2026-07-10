from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


P6_MSRP_DESIGN_REQUEST_SCHEMA_ID = "mica.project_tolomeo.p6.msrp_self_improvement_design_request.v1"
P6_MSRP_METRIC_SCHEMA_ID = "mica.project_tolomeo.p6.msrp_quality_metric_observation.v1"
P6_MSRP_CALIBRATION_PROPOSAL_SCHEMA_ID = "mica.project_tolomeo.p6.msrp_calibration_proposal.v1"
P6_MSRP_DESIGN_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.p6.msrp_self_improvement_receipt.v1"
P6_MSRP_DESIGN_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.msrp_self_improvement_design_result.v1"


METRIC_DEFINITIONS: dict[str, dict[str, Any]] = {
    "quality.methods_reproducibility": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "quality.results_rigor": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "quality.discussion_depth": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "quality.data_availability": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "quality.overall_score": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "quality.nature_compliance_ratio": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "msrp.phase_completion_ratio": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "msrp.chain_validation_error_count": {"unit": "count", "minimum": 0.0},
    "msrp.evidence_source_count": {"unit": "count", "minimum": 0.0},
    "msrp.knowledge_gap_count": {"unit": "count", "minimum": 0.0},
    "msrp.mean_evidence_reliability": {"unit": "ratio", "minimum": 0.0, "maximum": 1.0},
    "msrp.uncertainty_source_count": {"unit": "count", "minimum": 0.0},
    "msrp.phase_dispatch_tokens": {"unit": "tokens", "minimum": 0.0},
}

_PARAMETER_REF_PREFIXES = ("config://", "model-config://", "policy://", "prompt://")


class P6MSRPDesignBlocker(BaseModel):
    code: str
    message: str
    field: str | None = None


class P6MSRPMetricDefinition(BaseModel):
    metric_id: str
    unit: Literal["ratio", "count", "tokens"]
    minimum: float
    maximum: float | None = None
    read_only: Literal[True] = True
    source_authority: Literal["existing_msrp_or_quality_runtime"] = "existing_msrp_or_quality_runtime"


class P6MSRPQualityMetricObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: str = P6_MSRP_METRIC_SCHEMA_ID
    observation_ref: str | None = None
    metric_id: str
    value: float
    unit: Literal["ratio", "count", "tokens"]
    source_artifact_ref: str
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    observed_at: str
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def _validate_metric_contract(self) -> "P6MSRPQualityMetricObservation":
        definition = METRIC_DEFINITIONS.get(self.metric_id)
        if definition is None:
            raise ValueError(f"unsupported metric_id: {self.metric_id}")
        if self.unit != definition["unit"]:
            raise ValueError(f"metric {self.metric_id} requires unit {definition['unit']}")
        if not math.isfinite(self.value):
            raise ValueError("metric value must be finite")
        if self.value < float(definition["minimum"]):
            raise ValueError(f"metric {self.metric_id} is below its minimum")
        maximum = definition.get("maximum")
        if maximum is not None and self.value > float(maximum):
            raise ValueError(f"metric {self.metric_id} exceeds its maximum")
        if self.unit in {"count", "tokens"} and not self.value.is_integer():
            raise ValueError(f"metric {self.metric_id} requires an integer-valued observation")
        if not self.source_artifact_ref.startswith("artifact://"):
            raise ValueError("metric observations require an artifact:// source")
        if not self.source_receipt_refs or any(
            not ref.startswith("receipt://") for ref in self.source_receipt_refs
        ):
            raise ValueError("metric observations require receipt:// source refs")
        if not self.target_refs or any("://" not in ref for ref in self.target_refs):
            raise ValueError("metric observations require durable target refs")
        if not self.observed_at.strip():
            raise ValueError("metric observations require observed_at")
        return self


class P6MSRPProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameter_ref: str
    change_kind: Literal[
        "threshold",
        "coefficient",
        "prompt_policy",
        "routing_policy",
        "review_policy",
    ]
    current_value: str | int | float | bool
    proposed_value: str | int | float | bool
    rationale: str

    @model_validator(mode="after")
    def _validate_change_is_descriptive(self) -> "P6MSRPProposedChange":
        if not self.parameter_ref.startswith(_PARAMETER_REF_PREFIXES):
            raise ValueError("proposed changes require a governed parameter ref")
        if not self.rationale.strip():
            raise ValueError("proposed changes require rationale")
        if isinstance(self.current_value, float) and not math.isfinite(self.current_value):
            raise ValueError("current_value must be finite")
        if isinstance(self.proposed_value, float) and not math.isfinite(self.proposed_value):
            raise ValueError("proposed_value must be finite")
        return self


class P6MSRPCalibrationProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_parameter_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    evidence_packet_ref: str
    reason_codes: tuple[str, ...]
    proposed_changes: tuple[P6MSRPProposedChange, ...]

    @model_validator(mode="after")
    def _validate_proposal_evidence(self) -> "P6MSRPCalibrationProposalInput":
        if not self.target_parameter_refs:
            raise ValueError("calibration proposals require target_parameter_refs")
        if any(not ref.startswith(_PARAMETER_REF_PREFIXES) for ref in self.target_parameter_refs):
            raise ValueError("calibration target refs must be governed parameter refs")
        if not self.evidence_refs or any("://" not in ref for ref in self.evidence_refs):
            raise ValueError("calibration proposals require durable evidence refs")
        if not self.evidence_packet_ref.startswith("artifact://"):
            raise ValueError("calibration proposals require an artifact:// evidence packet")
        if not self.reason_codes or any(not code.strip() for code in self.reason_codes):
            raise ValueError("calibration proposals require reason_codes")
        if not self.proposed_changes:
            raise ValueError("calibration proposals require proposed_changes")
        declared = set(self.target_parameter_refs)
        if any(change.parameter_ref not in declared for change in self.proposed_changes):
            raise ValueError("every proposed change must target a declared parameter ref")
        return self


class P6MSRPSelfImprovementDesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: str = P6_MSRP_DESIGN_REQUEST_SCHEMA_ID
    metric_observations: tuple[Mapping[str, Any], ...] = ()
    calibration_proposal: Mapping[str, Any] | None = None
    automatic_apply_requested: bool = False
    runtime_retuning_requested: bool = False
    weight_update_requested: bool = False
    training_run_requested: bool = False
    provider_job_requested: bool = False
    protocol_run_requested: bool = False
    claim_promotion_requested: bool = False
    graph_write_requested: bool = False
    canonical_mudo_write_requested: bool = False
    raw_payload_embedded: bool = False


class P6MSRPCalibrationProposal(BaseModel):
    schema_id: str = P6_MSRP_CALIBRATION_PROPOSAL_SCHEMA_ID
    proposal_ref: str
    artifact_ref: str
    receipt_ref: str
    p6_id: str
    target_parameter_refs: tuple[str, ...]
    metric_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    evidence_packet_ref: str
    reason_codes: tuple[str, ...]
    proposed_changes: tuple[P6MSRPProposedChange, ...]
    status: Literal["proposal_only"] = "proposal_only"
    quetzal_decision: Literal["pending"] = "pending"
    requires_quetzal_approval: Literal[True] = True
    requires_evidence_packet: Literal[True] = True
    application_performed: Literal[False] = False


class P6MSRPSelfImprovementReceipt(BaseModel):
    schema_id: str = P6_MSRP_DESIGN_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["P6MSRPSelfImprovementReceipt"] = "P6MSRPSelfImprovementReceipt"
    p6_id: str
    decision: Literal["design_recorded", "proposal_created", "blocked"]
    reason_codes: tuple[str, ...]
    metric_refs: tuple[str, ...] = ()
    proposal_ref: str | None = None
    artifact_ref: str | None = None
    idempotency_key: str
    evaluated_policies: tuple[str, ...] = (
        "metrics_are_read_only",
        "evidence_packet_required",
        "quetzal_approval_required_before_future_application",
        "automatic_retuning_forbidden_in_p6_7",
        "no_training_or_weight_mutation",
        "no_provider_protocol_claim_graph_or_mudo_side_effects",
    )
    provider_job_created: bool = False
    protocol_run_created: bool = False
    training_run_created: bool = False
    model_weights_changed: bool = False
    evaluator_configuration_changed: bool = False
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    canonical_mudo_write_performed: bool = False
    execution_started: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6MSRPSelfImprovementDesignResult(BaseModel):
    schema_id: str = P6_MSRP_DESIGN_RESULT_SCHEMA_ID
    p6_id: str
    current_status: Literal["design_only"] = "design_only"
    status: Literal["design_only", "blocked"]
    metric_inventory: tuple[P6MSRPMetricDefinition, ...]
    observations: tuple[P6MSRPQualityMetricObservation, ...]
    calibration_proposal: P6MSRPCalibrationProposal | None = None
    receipt: P6MSRPSelfImprovementReceipt
    blockers: tuple[P6MSRPDesignBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...]
    provider_jobs_created: int = 0
    protocol_runs_created: int = 0
    training_runs_created: int = 0
    model_weights_changed: bool = False
    evaluator_configuration_changed: bool = False
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    canonical_mudo_writes_performed: int = 0
    execution_started: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]


def _stable_ref(prefix: str, value: Any) -> str:
    return f"{prefix}{_digest(value)}"


def _metric_inventory() -> tuple[P6MSRPMetricDefinition, ...]:
    return tuple(
        P6MSRPMetricDefinition(metric_id=metric_id, **definition)
        for metric_id, definition in sorted(METRIC_DEFINITIONS.items())
    )


def _policy_blockers(request: P6MSRPSelfImprovementDesignRequest) -> list[P6MSRPDesignBlocker]:
    intent_fields = {
        "automatic_apply_requested": request.automatic_apply_requested,
        "runtime_retuning_requested": request.runtime_retuning_requested,
        "weight_update_requested": request.weight_update_requested,
        "training_run_requested": request.training_run_requested,
        "provider_job_requested": request.provider_job_requested,
        "protocol_run_requested": request.protocol_run_requested,
        "claim_promotion_requested": request.claim_promotion_requested,
        "graph_write_requested": request.graph_write_requested,
        "canonical_mudo_write_requested": request.canonical_mudo_write_requested,
    }
    blockers = [
        P6MSRPDesignBlocker(
            code="runtime_mutation_intent_blocked",
            message="P6-7 is a design and measurement gate; runtime application is out of scope.",
            field=field,
        )
        for field, requested in intent_fields.items()
        if requested
    ]
    if request.raw_payload_embedded:
        blockers.append(P6MSRPDesignBlocker(
            code="raw_payload_embedded",
            message="P6-7 accepts durable refs and scalar proposal descriptions only.",
            field="raw_payload_embedded",
        ))
    return blockers


def _coerce_observations(
    p6_id: str,
    raw_observations: tuple[Mapping[str, Any], ...],
) -> tuple[tuple[P6MSRPQualityMetricObservation, ...], tuple[P6MSRPDesignBlocker, ...]]:
    observations: list[P6MSRPQualityMetricObservation] = []
    blockers: list[P6MSRPDesignBlocker] = []
    for index, raw in enumerate(raw_observations):
        payload = dict(raw)
        identity_payload = {"p6_id": p6_id, **payload}
        payload.setdefault("observation_ref", _stable_ref("metric://p6/msrp-quality/", identity_payload))
        try:
            observations.append(P6MSRPQualityMetricObservation(**payload))
        except ValidationError as exc:
            blockers.append(P6MSRPDesignBlocker(
                code="invalid_metric_observation",
                message=str(exc),
                field=f"metric_observations[{index}]",
            ))
    return tuple(observations), tuple(blockers)


def _blocked_result(
    *,
    p6_id: str,
    request_payload: Mapping[str, Any],
    observations: tuple[P6MSRPQualityMetricObservation, ...],
    blockers: tuple[P6MSRPDesignBlocker, ...],
) -> P6MSRPSelfImprovementDesignResult:
    reason_codes = tuple(dict.fromkeys(blocker.code for blocker in blockers))
    idempotency_key = hashlib.sha256(_stable_json({
        "p6_id": p6_id,
        "request": request_payload,
        "reason_codes": reason_codes,
    }).encode("utf-8")).hexdigest()
    receipt_ref = _stable_ref("receipt://p6/msrp-self-improvement/", {
        "idempotency_key": idempotency_key,
        "decision": "blocked",
    })
    receipt = P6MSRPSelfImprovementReceipt(
        receipt_ref=receipt_ref,
        p6_id=p6_id,
        decision="blocked",
        reason_codes=reason_codes,
        metric_refs=tuple(item.observation_ref or "" for item in observations),
        idempotency_key=idempotency_key,
    )
    return P6MSRPSelfImprovementDesignResult(
        p6_id=p6_id,
        status="blocked",
        metric_inventory=_metric_inventory(),
        observations=observations,
        receipt=receipt,
        blockers=blockers,
        receipt_refs=(receipt_ref,),
    )


def build_p6_msrp_self_improvement_design(
    packet: Mapping[str, Any],
    *,
    design_request_payload: Mapping[str, Any] | None = None,
) -> P6MSRPSelfImprovementDesignResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    raw_request = dict(design_request_payload or {})
    try:
        request = P6MSRPSelfImprovementDesignRequest(**raw_request)
    except ValidationError as exc:
        blocker = P6MSRPDesignBlocker(
            code="invalid_design_request",
            message=str(exc),
            field="design_request",
        )
        return _blocked_result(
            p6_id=p6_id,
            request_payload=raw_request,
            observations=(),
            blockers=(blocker,),
        )

    observations, observation_blockers = _coerce_observations(p6_id, request.metric_observations)
    blockers = [*_policy_blockers(request), *observation_blockers]
    proposal_input: P6MSRPCalibrationProposalInput | None = None
    if request.calibration_proposal is not None:
        try:
            proposal_input = P6MSRPCalibrationProposalInput(**dict(request.calibration_proposal))
        except ValidationError as exc:
            blockers.append(P6MSRPDesignBlocker(
                code="invalid_calibration_proposal",
                message=str(exc),
                field="calibration_proposal",
            ))
        if not observations:
            blockers.append(P6MSRPDesignBlocker(
                code="calibration_requires_metric_observations",
                message="Calibration proposals require at least one valid read-only metric observation.",
                field="metric_observations",
            ))

    if blockers:
        return _blocked_result(
            p6_id=p6_id,
            request_payload=raw_request,
            observations=observations,
            blockers=tuple(blockers),
        )

    metric_refs = tuple(item.observation_ref or "" for item in observations)
    design_identity = {
        "p6_id": p6_id,
        "metric_refs": metric_refs,
        "calibration_proposal": proposal_input.model_dump(mode="json") if proposal_input else None,
    }
    idempotency_key = hashlib.sha256(_stable_json(design_identity).encode("utf-8")).hexdigest()
    artifact_ref = _stable_ref("artifact://p6/msrp-self-improvement-design/", design_identity)
    proposal: P6MSRPCalibrationProposal | None = None
    decision: Literal["design_recorded", "proposal_created"] = "design_recorded"
    reason_codes = ("read_only_metric_inventory_recorded",)
    if proposal_input is not None:
        proposal_ref = _stable_ref("proposal://p6/msrp-calibration/", design_identity)
        proposal_artifact_ref = _stable_ref("artifact://p6/msrp-calibration-proposal/", design_identity)
        proposal_receipt_ref = _stable_ref("receipt://p6/msrp-calibration-proposal/", design_identity)
        proposal = P6MSRPCalibrationProposal(
            proposal_ref=proposal_ref,
            artifact_ref=proposal_artifact_ref,
            receipt_ref=proposal_receipt_ref,
            p6_id=p6_id,
            target_parameter_refs=proposal_input.target_parameter_refs,
            metric_refs=metric_refs,
            evidence_refs=proposal_input.evidence_refs,
            evidence_packet_ref=proposal_input.evidence_packet_ref,
            reason_codes=proposal_input.reason_codes,
            proposed_changes=proposal_input.proposed_changes,
        )
        decision = "proposal_created"
        reason_codes = ("evidence_backed_calibration_proposal_created", "quetzal_review_pending")

    receipt_ref = _stable_ref("receipt://p6/msrp-self-improvement/", {
        "idempotency_key": idempotency_key,
        "decision": decision,
    })
    receipt = P6MSRPSelfImprovementReceipt(
        receipt_ref=receipt_ref,
        p6_id=p6_id,
        decision=decision,
        reason_codes=reason_codes,
        metric_refs=metric_refs,
        proposal_ref=proposal.proposal_ref if proposal else None,
        artifact_ref=artifact_ref,
        idempotency_key=idempotency_key,
    )
    artifact_refs = [artifact_ref]
    receipt_refs = [receipt_ref]
    if proposal is not None:
        artifact_refs.append(proposal.artifact_ref)
        receipt_refs.append(proposal.receipt_ref)
    return P6MSRPSelfImprovementDesignResult(
        p6_id=p6_id,
        status="design_only",
        metric_inventory=_metric_inventory(),
        observations=observations,
        calibration_proposal=proposal,
        receipt=receipt,
        artifact_refs=tuple(artifact_refs),
        receipt_refs=tuple(receipt_refs),
    )
