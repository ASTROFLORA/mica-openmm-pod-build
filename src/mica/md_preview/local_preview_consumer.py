from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .bcif_runtime import validate_bcif


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_non_empty(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


class LocalBCIFPreviewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    format: Literal["bcif"] = "bcif"
    path: str
    size_bytes: int = Field(ge=1)
    sha256: str
    source_job: str
    source_run: str = ""
    source_run_id: str = ""
    representation: Literal["all_atom", "coarse_grained"] = "coarse_grained"
    system_id: str = "clcn7"
    frame_index: int = Field(ge=0, default=0)
    time_ps: float | None = Field(default=None, ge=0.0)
    canonical_or_preview: str
    preview_not_canonical: bool = True
    validation_status: str = "passed"
    header_hex: str = ""
    header_ascii: str = ""

    @field_validator(
        "artifact_id",
        "path",
        "sha256",
        "source_job",
        "representation",
        "system_id",
        "canonical_or_preview",
        "validation_status",
    )
    @classmethod
    def validate_non_empty_fields(cls, value: str, info) -> str:
        return _validate_non_empty(value, field_name=info.field_name)


class LocalBCIFPreviewConsumerReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    status: Literal["completed", "blocked"]
    source_target_id: str = "clcn7"
    consumed_preview_count: int = Field(ge=0)
    previews: list[LocalBCIFPreviewArtifact]
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    preview_transport: Literal["local_filesystem"] = "local_filesystem"
    realtime_ws_claim: bool = False
    production_claim: bool = False
    smic_metrics_status: Literal["not_executed"] = "not_executed"


class PreviewEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal[
        "trajectory.preview.available",
        "trajectory.frame.preview",
        "artifact.synced",
        "cg.batch.experiment.completed",
        "cg.batch.experiment.blocked",
        "smic.metric",
        "error.typed",
    ]
    source: str
    subject: str
    time: str
    datacontenttype: Literal["application/json"] = "application/json"
    data: dict[str, Any]

    @field_validator("id", "source", "subject", "time")
    @classmethod
    def validate_event_text_fields(cls, value: str, info) -> str:
        return _validate_non_empty(value, field_name=info.field_name)


def consume_bcif_preview_artifact(
    *,
    artifact_id: str,
    bcif_path: str,
    source_job: str,
    source_run: str = "",
    source_run_id: str = "",
    representation: Literal["all_atom", "coarse_grained"] = "coarse_grained",
    system_id: str = "clcn7",
    frame_index: int = 0,
    time_ps: float | None = None,
    canonical_or_preview: str = "preview",
    preview_not_canonical: bool = True,
) -> LocalBCIFPreviewArtifact:
    validation = validate_bcif(bcif_path)
    if validation.validation_status != "passed":
        raise ValueError(f"BCIF validation failed: {validation.blocker or validation.detail}")
    return LocalBCIFPreviewArtifact(
        artifact_id=artifact_id,
        path=validation.path,
        size_bytes=validation.size_bytes,
        sha256=validation.sha256,
        source_job=source_job,
        source_run=source_run,
        source_run_id=source_run_id or source_run,
        representation=representation,
        system_id=system_id,
        frame_index=frame_index,
        time_ps=time_ps,
        canonical_or_preview=canonical_or_preview,
        preview_not_canonical=preview_not_canonical,
        validation_status=validation.validation_status,
        header_hex=validation.header_hex,
        header_ascii=validation.header_ascii,
    )


def build_local_preview_manifest(
    *,
    manifest_id: str,
    previews: list[LocalBCIFPreviewArtifact],
) -> dict[str, Any]:
    return {
        "manifest_id": manifest_id,
        "preview_transport": "local_filesystem",
        "previews": [preview.model_dump(mode="json") for preview in previews],
        "realtime_ws_claim": False,
        "production_claim": False,
        "smic_metrics_status": "not_executed",
    }


def build_local_preview_consumer_receipt(
    *,
    receipt_id: str,
    previews: list[LocalBCIFPreviewArtifact],
    warnings: list[str] | None = None,
    blockers: list[str] | None = None,
) -> LocalBCIFPreviewConsumerReceipt:
    warnings = list(warnings or [])
    blockers = list(blockers or [])
    return LocalBCIFPreviewConsumerReceipt(
        receipt_id=receipt_id,
        status="completed" if not blockers else "blocked",
        consumed_preview_count=len(previews),
        previews=previews,
        warnings=warnings,
        blockers=blockers,
        realtime_ws_claim=False,
        production_claim=False,
        smic_metrics_status="not_executed",
    )


def build_preview_event_contract_v1() -> dict[str, Any]:
    return {
        "contract_id": "clcn7_preview_event_contract_v1",
        "envelope": {
            "style": "cloudevents_like",
            "required_fields": ["id", "type", "source", "subject", "time", "datacontenttype", "data"],
            "datacontenttype": "application/json",
        },
        "event_types": {
            "trajectory.preview.available": {
                "required_data_fields": [
                    "artifact_id",
                    "format",
                    "path",
                    "size_bytes",
                    "sha256",
                    "preview_not_canonical",
                ],
                "lane_metadata_fields": ["representation", "system_id", "source_run_id"],
            },
            "trajectory.frame.preview": {
                "required_data_fields": [
                    "artifact_id",
                    "frame_index",
                    "time_ps",
                    "format",
                    "path",
                    "preview_not_canonical",
                ],
                "lane_metadata_fields": ["representation", "system_id", "source_run_id"],
            },
            "artifact.synced": {
                "required_data_fields": [
                    "artifact_id",
                    "path",
                    "sha256",
                    "size_bytes",
                ],
                "lane_metadata_fields": ["representation", "system_id", "source_run_id"],
            },
            "cg.batch.experiment.completed": {
                "required_data_fields": [
                    "batch_id",
                    "experiment_id",
                    "runtime_status",
                    "preview_status",
                ],
                "lane_metadata_fields": ["representation", "system_id", "source_run_id"],
            },
            "cg.batch.experiment.blocked": {
                "required_data_fields": [
                    "batch_id",
                    "experiment_id",
                    "failure_code",
                    "blockers",
                ],
                "lane_metadata_fields": ["representation", "system_id", "source_run_id"],
            },
            "smic.metric": {
                "required_data_fields": [
                    "metric_name",
                    "metric_status",
                    "no_fake_metric",
                ],
                "lane_metadata_fields": ["representation", "system_id", "source_run_id"],
            },
            "error.typed": {
                "required_data_fields": [
                    "failure_code",
                    "failure_detail",
                ]
            },
        },
        "claim_boundary": {
            "realtime_ws_claim": False,
            "production_claim": False,
            "biological_correctness_claim": False,
            "smic_metrics_status": "not_executed",
        },
    }


def validate_preview_event(event: dict[str, Any]) -> PreviewEventEnvelope:
    parsed = PreviewEventEnvelope.model_validate(event)
    contract = build_preview_event_contract_v1()
    required = contract["event_types"][parsed.type]["required_data_fields"]
    missing = [field for field in required if field not in parsed.data]
    if missing:
        raise ValueError(f"event {parsed.type} missing required data fields: {missing}")
    return parsed


def _event(
    *,
    event_type: str,
    source: str,
    subject: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    payload = PreviewEventEnvelope(
        id=str(uuid4()),
        type=event_type,
        source=source,
        subject=subject,
        time=_utcnow(),
        data=data,
    ).model_dump(mode="json")
    validate_preview_event(payload)
    return payload


def build_preview_available_event(
    *,
    preview: LocalBCIFPreviewArtifact,
    source: str,
    subject: str,
) -> dict[str, Any]:
    return _event(
        event_type="trajectory.preview.available",
        source=source,
        subject=subject,
        data=preview.model_dump(mode="json"),
    )


def build_frame_preview_event(
    *,
    preview: LocalBCIFPreviewArtifact,
    source: str,
    subject: str,
) -> dict[str, Any]:
    return _event(
        event_type="trajectory.frame.preview",
        source=source,
        subject=subject,
        data=preview.model_dump(mode="json"),
    )


def build_artifact_synced_event(
    *,
    preview: LocalBCIFPreviewArtifact,
    source: str,
    subject: str,
) -> dict[str, Any]:
    return _event(
        event_type="artifact.synced",
        source=source,
        subject=subject,
        data={
            "artifact_id": preview.artifact_id,
            "path": preview.path,
            "sha256": preview.sha256,
            "size_bytes": preview.size_bytes,
            "representation": preview.representation,
            "system_id": preview.system_id,
            "source_run_id": preview.source_run_id or preview.source_run,
        },
    )


def build_batch_completed_event(
    *,
    batch_id: str,
    experiment: dict[str, Any],
    source: str,
    subject: str,
    representation: Literal["all_atom", "coarse_grained"] = "coarse_grained",
    system_id: str = "clcn7",
    source_run_id: str = "",
) -> dict[str, Any]:
    return _event(
        event_type="cg.batch.experiment.completed",
        source=source,
        subject=subject,
        data={
            "batch_id": batch_id,
            "experiment_id": str(experiment.get("experiment_id") or ""),
            "runtime_status": str((experiment.get("runtime_receipt") or {}).get("status") or ""),
            "preview_status": str((experiment.get("preview_receipt") or {}).get("bcif_preview_status") or ""),
            "dynamics_steps_run": int((experiment.get("runtime_receipt") or {}).get("dynamics_steps_run") or 0),
            "final_energy": (experiment.get("runtime_receipt") or {}).get("final_energy"),
            "representation": representation,
            "system_id": system_id,
            "source_run_id": source_run_id or str(experiment.get("experiment_id") or ""),
        },
    )


def build_batch_blocked_event(
    *,
    batch_id: str,
    experiment: dict[str, Any],
    source: str,
    subject: str,
    representation: Literal["all_atom", "coarse_grained"] = "coarse_grained",
    system_id: str = "clcn7",
    source_run_id: str = "",
) -> dict[str, Any]:
    return _event(
        event_type="cg.batch.experiment.blocked",
        source=source,
        subject=subject,
        data={
            "batch_id": batch_id,
            "experiment_id": str(experiment.get("experiment_id") or ""),
            "failure_code": str(experiment.get("failure_code") or ""),
            "blockers": [str(item) for item in list(experiment.get("blockers") or []) if str(item)],
            "warnings": [str(item) for item in list(experiment.get("warnings") or []) if str(item)],
            "representation": representation,
            "system_id": system_id,
            "source_run_id": source_run_id or str(experiment.get("experiment_id") or ""),
        },
    )


def build_smic_metric_event(
    *,
    metric_name: str,
    metric_status: str,
    no_fake_metric: bool,
    source: str,
    subject: str,
    representation: Literal["all_atom", "coarse_grained"],
    system_id: str,
    source_run_id: str,
    value: float | None = None,
    units: str = "",
    series_ref: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "metric_name": metric_name,
        "metric_status": metric_status,
        "no_fake_metric": no_fake_metric,
        "representation": representation,
        "system_id": system_id,
        "source_run_id": source_run_id,
    }
    if value is not None:
        data["value"] = value
    if units:
        data["units"] = units
    if series_ref:
        data["series_ref"] = series_ref
    return _event(
        event_type="smic.metric",
        source=source,
        subject=subject,
        data=data,
    )


def write_event_fixtures_jsonl(*, path: str, events: list[dict[str, Any]]) -> str:
    from pathlib import Path
    import json

    output_path = Path(path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            validate_preview_event(event)
            handle.write(json.dumps(event) + "\n")
    return str(output_path)
