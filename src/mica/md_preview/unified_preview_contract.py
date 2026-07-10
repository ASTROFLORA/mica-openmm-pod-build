from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PreviewContractReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    representation: Literal["all_atom", "coarse_grained"]
    system_id: str
    source_job_id: str
    source_run_id: str
    bcif_ref: str
    bcif_size_bytes: int = Field(ge=1)
    bcif_sha256: str
    event_fixture_ref: str
    preview_manifest_ref: str
    ui_state_ref: str
    ui_preview_status: str
    frame_index: int | None = Field(default=None, ge=0)
    time_ps: float | None = Field(default=None, ge=0.0)
    canonical_artifact_refs: dict[str, str] = Field(default_factory=dict)
    smic_metrics_status: str = "not_executed"
    realtime_ws_claim: bool = False
    preview_not_canonical: bool = True
    status: Literal["completed", "blocked"]
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


def build_preview_contract_receipt(
    *,
    receipt_id: str,
    representation: Literal["all_atom", "coarse_grained"],
    system_id: str,
    source_job_id: str,
    source_run_id: str,
    bcif_ref: str,
    bcif_size_bytes: int,
    bcif_sha256: str,
    event_fixture_ref: str,
    preview_manifest_ref: str,
    ui_state_ref: str,
    ui_preview_status: str,
    frame_index: int | None,
    time_ps: float | None,
    canonical_artifact_refs: dict[str, str] | None = None,
    smic_metrics_status: str = "not_executed",
    warnings: list[str] | None = None,
    blockers: list[str] | None = None,
) -> PreviewContractReceipt:
    warnings = list(warnings or [])
    blockers = list(blockers or [])
    return PreviewContractReceipt(
        receipt_id=receipt_id,
        representation=representation,
        system_id=system_id,
        source_job_id=source_job_id,
        source_run_id=source_run_id,
        bcif_ref=bcif_ref,
        bcif_size_bytes=bcif_size_bytes,
        bcif_sha256=bcif_sha256,
        event_fixture_ref=event_fixture_ref,
        preview_manifest_ref=preview_manifest_ref,
        ui_state_ref=ui_state_ref,
        ui_preview_status=ui_preview_status,
        frame_index=frame_index,
        time_ps=time_ps,
        canonical_artifact_refs=dict(canonical_artifact_refs or {}),
        smic_metrics_status=smic_metrics_status,
        status="completed" if not blockers else "blocked",
        warnings=warnings,
        blockers=blockers,
    )


def build_preview_contract_parity_matrix(
    *,
    contract_path: str,
    cg_receipt: dict[str, Any],
    aa_receipt: dict[str, Any],
    cg_ui_state: dict[str, Any],
    aa_ui_state: dict[str, Any],
) -> dict[str, Any]:
    shared_ui_keys = sorted(set(cg_ui_state.keys()) & set(aa_ui_state.keys()))
    return {
        "matrix_id": "aa_cg_preview_contract_parity_matrix_v2",
        "contract_ref": contract_path,
        "representations": {
            "cg": cg_receipt.get("representation"),
            "aa": aa_receipt.get("representation"),
        },
        "proof_status": {
            "cg": cg_receipt.get("status"),
            "aa": aa_receipt.get("status"),
        },
        "event_schema": {
            "same_envelope": True,
            "cg_event_fixture_ref": cg_receipt.get("event_fixture_ref"),
            "aa_event_fixture_ref": aa_receipt.get("event_fixture_ref"),
        },
        "artifact_manifest_fields": {
            "cg_bcif_ref": bool(cg_receipt.get("bcif_ref")),
            "aa_bcif_ref": bool(aa_receipt.get("bcif_ref")),
            "cg_sha256": bool(cg_receipt.get("bcif_sha256")),
            "aa_sha256": bool(aa_receipt.get("bcif_sha256")),
            "cg_size_bytes": bool(cg_receipt.get("bcif_size_bytes")),
            "aa_size_bytes": bool(aa_receipt.get("bcif_size_bytes")),
        },
        "frame_metadata": {
            "cg_frame_index": cg_receipt.get("frame_index"),
            "aa_frame_index": aa_receipt.get("frame_index"),
            "cg_time_ps": cg_receipt.get("time_ps"),
            "aa_time_ps": aa_receipt.get("time_ps"),
        },
        "canonical_refs": {
            "cg": sorted((cg_receipt.get("canonical_artifact_refs") or {}).keys()),
            "aa": sorted((aa_receipt.get("canonical_artifact_refs") or {}).keys()),
        },
        "preview_refs": {
            "cg": cg_receipt.get("bcif_ref"),
            "aa": aa_receipt.get("bcif_ref"),
        },
        "errors": {
            "cg_error_count": len(list(cg_ui_state.get("errors") or [])),
            "aa_error_count": len(list(aa_ui_state.get("errors") or [])),
        },
        "ui_state_shape": {
            "shared_keys": shared_ui_keys,
            "same_shape": sorted(cg_ui_state.keys()) == sorted(aa_ui_state.keys()),
            "cg_preview_status": cg_ui_state.get("preview_status"),
            "aa_preview_status": aa_ui_state.get("preview_status"),
        },
        "smic_metrics_status": {
            "cg": cg_receipt.get("smic_metrics_status"),
            "aa": aa_receipt.get("smic_metrics_status"),
        },
        "claim_boundary": {
            "realtime_ws_claim": False,
            "production_claim": False,
            "biological_correctness_claim": False,
        },
    }
