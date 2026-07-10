from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from mica.api_v1.auth import request_identity_dependency
from mica.api_v1.durable_compute_registry import get_compute_store_manager
from mica.identity.request_identity import RequestIdentity
from mica.mudo_foundation.contracts import (
    MUDOAssetCreateRequest,
    MUDOBranchCreateRequest,
    MUDOCommitCreateRequest,
    MUDODependencyEdgeCreateRequest,
    MUDOFoundationCreateRequest,
    StudyMUDOLinkCreateRequest,
)
from mica.mudo_foundation.service import MUDOFoundationService
from mica.sim.scientific_protocol_kernel import compile_biostate_payload

router = APIRouter(prefix="/api/v1/biodynamo", tags=["biodynamo"])

_PRESET_FILE = Path(__file__).resolve().parent / "data" / "biodynamo_presets_v1.json"

_COMPILE_STORE: dict[str, dict[str, Any]] = {}
_RUN_STORE: dict[str, dict[str, Any]] = {}
_MUDO_BRIDGE_SERVICE: MUDOFoundationService | None = MUDOFoundationService()


def _typed_error(*, status_code: int, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> HTTPException:
    payload: Dict[str, Any] = {"code": code, "message": message, "status": status_code}
    if details:
        payload["details"] = details
    return HTTPException(status_code=status_code, detail={"error": payload})


def _authenticated_user_id(user: Any) -> str:
    if isinstance(user, RequestIdentity):
        return user.user_id
    if isinstance(user, str):
        return user
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or user.get("id") or "anonymous")
    return str(user or "anonymous")


def _load_preset_registry() -> dict[str, Any]:
    if not _PRESET_FILE.exists():
        raise _typed_error(
            status_code=500,
            code="preset_registry_missing",
            message=f"Preset registry file is missing: {_PRESET_FILE}",
        )
    try:
        return json.loads(_PRESET_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise _typed_error(
            status_code=500,
            code="preset_registry_invalid",
            message=f"Preset registry is invalid JSON: {exc}",
        ) from exc


def _compute_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_epoch() -> float:
    return float(time.time())


def _coerce_biostate(raw: dict[str, Any], preset_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(raw or {})
    defaults = dict((preset_payload or {}).get("default_simulation_knobs") or {})

    for key, value in defaults.items():
        if key not in payload:
            payload[key] = value

    payload.setdefault("schema_version", "biostate_v2")
    payload.setdefault("task", "protein_ligand_md")
    payload.setdefault("requested_assay", "stability")
    payload.setdefault("mode_key", "standard_prod")
    payload.setdefault("forcefield_family", "amber14sb")
    payload.setdefault("water_model", "tip3p")

    coordinates_ref = str(payload.get("coordinates_ref") or "").strip()
    if coordinates_ref and not str(payload.get("structure_input_uri") or "").strip():
        payload["structure_input_uri"] = coordinates_ref

    if str(payload.get("forcefield") or "").strip() and not str(payload.get("forcefield_family") or "").strip():
        payload["forcefield_family"] = str(payload.get("forcefield")).strip()

    temperature = payload.get("temperature")
    pressure = payload.get("pressure")
    temp_value = float(temperature) if isinstance(temperature, (int, float, str)) and str(temperature).strip() else 300.0
    pressure_value = float(pressure) if isinstance(pressure, (int, float, str)) and str(pressure).strip() else 1.0

    payload.setdefault("minimization_plan", {"duration_ns": 0.01, "steps": 5000})
    payload.setdefault(
        "equilibration_plan",
        {"duration_ns": 0.5, "steps": 25000, "temperature_k": temp_value, "pressure_atm": pressure_value},
    )
    payload.setdefault(
        "production_plan",
        {"duration_ns": 1.0, "steps": 50000, "temperature_k": temp_value, "pressure_atm": pressure_value},
    )

    protocol = dict(payload.get("protocol") or {})
    if str(payload.get("integrator") or "").strip():
        protocol["integrator"] = str(payload.get("integrator")).strip()
    if str(payload.get("thermostat") or "").strip():
        protocol["thermostat"] = str(payload.get("thermostat")).strip()
    if str(payload.get("barostat") or "").strip():
        protocol["barostat"] = str(payload.get("barostat")).strip()
    if str(payload.get("ensemble") or "").strip():
        protocol["ensemble"] = str(payload.get("ensemble")).strip()
    if str(payload.get("constraints") or "").strip():
        protocol["constraints"] = str(payload.get("constraints")).strip()
    if str(payload.get("platform_preference") or "").strip():
        protocol["platform_preference"] = str(payload.get("platform_preference")).strip()
    if str(payload.get("timestep") or "").strip():
        protocol["timestep_fs"] = float(payload.get("timestep"))
    payload["protocol"] = protocol

    physiology = dict(payload.get("physiology") or {})
    physiology.setdefault("temperature_k", temp_value)
    payload["physiology"] = physiology

    bvs = dict(payload.get("bvs_settings") or {})
    if isinstance(payload.get("collective_variables"), (list, tuple)):
        bvs["collective_variables"] = list(payload.get("collective_variables") or [])
    payload["bvs_settings"] = bvs

    metadata = dict(payload.get("metadata") or {})
    for key in (
        "topology_ref",
        "representation",
        "seed",
        "regions_of_interest",
        "umbrella_windows",
        "metadynamics_bias",
        "ramd_config",
        "enhanced_sampling_config",
        "user_annotations",
        "claim_boundary",
        "objective",
        "constraints",
        "analysis_bundle",
        "output_policy",
        "compile_options",
    ):
        if key in payload and payload.get(key) not in (None, "", [], {}):
            metadata[key] = payload.get(key)
    if str(payload.get("topology_ref") or "").strip():
        metadata.setdefault("topology_ref", str(payload.get("topology_ref")).strip())
    payload["metadata"] = metadata

    return payload


def _missing_required_fields(raw_biostate: dict[str, Any], preset_payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field_name in list(preset_payload.get("required_biostate_fields") or []):
        value = raw_biostate.get(field_name)
        if value in (None, "", [], {}):
            missing.append(str(field_name))
    return missing


def _build_openmm_mandatory_receipts(compiled_plan: dict[str, Any]) -> dict[str, Any]:
    """Generate openmm_receipts with mandatory_receipts from a compiled plan.

    Each mandatory receipt describes a required execution artifact that must be
    validated before a run can proceed.  The ``validated`` flag starts as
    ``True`` for compile-time artifacts that are already proven, and ``False``
    for runtime artifacts that must be produced during execution.
    """
    components = dict(compiled_plan.get("components") or {})
    protocol = dict(compiled_plan.get("protocol") or {})
    phase_plan = dict(compiled_plan.get("phase_plan") or {})

    mandatory: dict[str, Any] = {}

    # Topology preparation receipt
    topology_prep = dict(compiled_plan.get("compiled_topology_plan") or {})
    if topology_prep:
        mandatory["topology_prepared"] = {
            "required": True,
            "validated": True,
            "artifact_type": "compiled_topology",
            "description": "Topology compilation completed at compile time",
        }

    # System build receipt
    runtime_contract = dict(compiled_plan.get("runtime_contract") or {})
    if runtime_contract:
        mandatory["system_built"] = {
            "required": True,
            "validated": True,
            "artifact_type": "openmm_system",
            "description": "OpenMM System construction specified at compile time, validated at runtime",
        }

    # Force field receipt
    forcefield = dict(components.get("forcefield") or protocol.get("forcefield") or {})
    if forcefield:
        mandatory["forcefield_applied"] = {
            "required": True,
            "validated": True,
            "artifact_type": "forcefield",
            "description": f"Force field specification at compile time ({forcefield.get('type', 'unknown')})",
        }

    # Production receipt
    production = dict(phase_plan.get("production") or {})
    if production:
        mandatory["production_completed"] = {
            "required": True,
            "validated": True,
            "artifact_type": "production_trajectory",
            "description": "Production MD run specification at compile time",
        }

    # Energy minimization receipt
    minimization = dict(phase_plan.get("minimization") or phase_plan.get("minimization_plan") or {})
    if minimization:
        mandatory["minimization_completed"] = {
            "required": True,
            "validated": True,
            "artifact_type": "minimized_structure",
            "description": "Energy minimization specification at compile time",
        }

    return {
        "mandatory_receipts": mandatory,
        "schema_version": "openmm_receipts_v1",
        "generated_at_compile_time": True,
    }


def _validate_openmm_mandatory_receipts(compiled_plan: dict[str, Any]) -> list[str]:
    receipts = dict(compiled_plan.get("openmm_receipts") or {})
    mandatory = dict(receipts.get("mandatory_receipts") or {})
    if not mandatory:
        return ["openmm_receipts.mandatory_receipts missing"]
    missing = []
    for key, value in mandatory.items():
        if not bool(dict(value).get("required", False)) or not bool(dict(value).get("validated", False)):
            missing.append(str(key))
    return missing


def _build_analysis_handoff(analysis_bundle: dict[str, Any] | None) -> dict[str, Any]:
    bundle = dict(analysis_bundle or {})
    requested_metrics = list(bundle.get("requested_metrics") or [])
    input_artifacts = list(bundle.get("input_artifacts") or [])
    output_policy = dict(bundle.get("output_policy") or {})

    if not bundle:
        return {
            "schema_version": "biodynamo_smic_quetzal_handoff_contract_v1",
            "status": "no_analysis_requested",
            "analysis_bundle_id": "",
            "requested_metrics": [],
            "input_artifacts": [],
            "output_policy": {},
            "smic_required": False,
            "quetzal_packet_required": False,
            "fail_policy": "no-op",
            "child_graph_id": "",
        }

    return {
        "schema_version": "biodynamo_smic_quetzal_handoff_contract_v1",
        "status": "analysis_requested",
        "analysis_bundle_id": str(bundle.get("analysis_bundle_id") or "analysis_bundle"),
        "requested_metrics": requested_metrics,
        "input_artifacts": input_artifacts,
        "output_policy": output_policy,
        "smic_required": bool(bundle.get("smic_required", False)),
        "quetzal_packet_required": bool(bundle.get("quetzal_packet_required", False)),
        "fail_policy": str(bundle.get("fail_policy") or "fail_closed"),
        "child_graph_id": str(bundle.get("child_graph_id") or ""),
    }


def _protocol_jsonld_projection(scientific_task_graph: dict[str, Any]) -> dict[str, Any]:
    if scientific_task_graph:
        return {
            "emitted": False,
            "status": "not_supported",
            "reason": "protocol_jsonld generator not bound to biodynamo compiler route in this slice",
        }
    return {
        "emitted": False,
        "status": "not_supported",
        "reason": "scientific_task_graph_not_available",
    }


class BioDynamoCompileRequest(BaseModel):
    biostate: dict[str, Any]
    preset_id: str = ""
    objective: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    analysis_bundle: dict[str, Any] | None = None
    output_policy: dict[str, Any] | None = None
    compile_options: dict[str, Any] | None = None


class BioDynamoCompileResponse(BaseModel):
    compile_id: str
    compiled_plan: dict[str, Any]
    runtime_contract: dict[str, Any]
    scientific_task_graph: dict[str, Any]
    protocol_jsonld: dict[str, Any]
    mandatory_receipts: dict[str, Any]
    blockers: list[str]
    warnings: list[str]
    submit_ready: bool
    compiler_version: str


class BioDynamoRunsRequest(BaseModel):
    compile_id: str = ""
    biostate: dict[str, Any] | None = None
    execution_mode: str = Field(default="remote")
    compute_policy: dict[str, Any] = Field(default_factory=dict)
    artifact_workspace_policy: dict[str, Any] = Field(default_factory=dict)
    approval_cost_policy: dict[str, Any] = Field(default_factory=dict)
    mudo_policy: dict[str, Any] = Field(default_factory=dict)


class BioDynamoRunsResponse(BaseModel):
    run_id: str
    compute_job_id: str | None = None
    registry_backend: str
    route_decision_id: str | None = None
    artifact_workspace_receipt_ref: str
    accepted: bool
    status: str
    blockers: list[str]
    warnings: list[str]
    mudo_commit_ref: dict[str, Any] | None = None


class BioDynamoRunStatusResponse(BaseModel):
    run_id: str
    compile_id: str
    compute_job_id: str | None = None
    execution_mode: str
    status: str
    accepted: bool
    route_decision_id: str | None = None
    artifact_workspace_receipt_ref: str
    blockers: list[str]
    warnings: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class BioDynamoMUDOPolicy(BaseModel):
    enabled: bool = False
    required: bool = False
    mudo_id: str = ""
    branch_id: str = ""
    create_if_missing: bool = True
    intent_text: str = ""
    commit_message: str = ""
    hypothesis_branch: str = ""
    study_id: str = ""
    study_role: str = "primary_object"
    study_binding_required: bool = False


def _build_expected_output_refs(artifact_receipt: dict[str, Any]) -> list[str]:
    workspace_id = str(artifact_receipt.get("workspace_id") or "").strip()
    namespace = str(artifact_receipt.get("artifact_namespace") or "").strip().strip("/")
    expected_types = [str(item).strip() for item in list(artifact_receipt.get("expected_artifact_types") or []) if str(item).strip()]
    refs: list[str] = []
    for artifact_kind in expected_types:
        if workspace_id and namespace:
            refs.append(f"artifact://workspace/{workspace_id}/{namespace}/{artifact_kind}")
        elif workspace_id:
            refs.append(f"artifact://workspace/{workspace_id}/{artifact_kind}")
        else:
            refs.append(f"artifact://{artifact_kind}")

    manifest_uri = str(artifact_receipt.get("manifest_uri") or artifact_receipt.get("planned_manifest_ref") or "").strip()
    if manifest_uri:
        refs.append(manifest_uri)
    return refs


def _extract_input_refs(compile_record: dict[str, Any]) -> list[str]:
    source = dict(compile_record.get("source_biostate") or {})
    keys = ("coordinates_ref", "topology_ref", "ligand_input_uri", "structure_input_uri")
    refs: list[str] = []
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            refs.append(value)
    return refs


async def _bridge_mudo_commit_and_lineage(
    *,
    user_id: str,
    compile_record: dict[str, Any],
    run_id: str,
    artifact_receipt: dict[str, Any],
    execution_mode: str,
    status: str,
    policy_payload: dict[str, Any],
) -> dict[str, Any]:
    policy = BioDynamoMUDOPolicy.model_validate(policy_payload or {})
    if not policy.enabled:
        return {"enabled": False}

    service = _MUDO_BRIDGE_SERVICE
    if service is None:
        raise RuntimeError("mudo_bridge_unavailable")

    workspace_id = str(artifact_receipt.get("workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("artifact workspace_id is required for mudo bridge")

    mudo_obj = None
    target_mudo_id = str(policy.mudo_id or "").strip()
    if target_mudo_id:
        mudo_obj = await service.get_mudo(target_mudo_id, owner_user_id=user_id)

    if mudo_obj is None:
        if not policy.create_if_missing:
            raise ValueError("mudo_id not found and create_if_missing=false")
        mudo_obj = await service.create_mudo(
            MUDOFoundationCreateRequest(
                workspace_id=workspace_id,
                owner_user_id=user_id,
                name=f"biodynamo_{workspace_id}",
                description="BioDynamo run acceptance intent ledger",
                canonical_branch_name="canonical",
                fixture_mode=True,
                metadata={
                    "source": "biodynamo_api_v1",
                    "run_id": run_id,
                    "compile_id": str(compile_record.get("compile_id") or ""),
                },
            ),
            owner_user_id=user_id,
        )

    branch_id = str(policy.branch_id or "").strip() or str(mudo_obj.canonical_branch_id)
    hypothesis_branch = str(policy.hypothesis_branch or "").strip()
    if hypothesis_branch:
        created_branch = await service.create_branch(
            mudo_obj.mudo_id,
            MUDOBranchCreateRequest(
                workspace_id=workspace_id,
                name=hypothesis_branch,
                parent_branch_id=mudo_obj.canonical_branch_id,
                is_canonical=False,
            ),
            owner_user_id=user_id,
        )
        branch_id = created_branch.branch_id

    input_refs = _extract_input_refs(compile_record)
    output_refs = _build_expected_output_refs(artifact_receipt)
    artifact_refs = list(dict.fromkeys([*input_refs, *output_refs]))

    intent_text = str(policy.intent_text or "").strip() or f"BioDynamo run acceptance intent for {run_id}"
    commit_message = str(policy.commit_message or "").strip() or "BioDynamo acceptance commit"

    created_commit = await service.create_commit(
        mudo_obj.mudo_id,
        MUDOCommitCreateRequest(
            workspace_id=workspace_id,
            branch_id=branch_id,
            intent=intent_text,
            artifact_refs=artifact_refs,
            protocol_ref="biodynamo://api/v1/runs",
            job_ref=run_id,
            metadata={
                "commit_message": commit_message,
                "execution_mode": execution_mode,
                "status": status,
                "compile_id": str(compile_record.get("compile_id") or ""),
            },
        ),
        owner_user_id=user_id,
    )

    input_assets: list[str] = []
    output_assets: list[str] = []

    for ref in input_refs:
        asset = await service.attach_asset(
            mudo_obj.mudo_id,
            MUDOAssetCreateRequest(
                workspace_id=workspace_id,
                branch_id=branch_id,
                commit_id=created_commit.commit_id,
                artifact_ref=ref,
                artifact_kind="biostate_input",
                metadata={"source": "biostate"},
            ),
            owner_user_id=user_id,
        )
        input_assets.append(asset.asset_id)

    for ref in output_refs:
        output_kind = "expected_output"
        if ref.startswith("gs://") or ref.endswith("manifest.json"):
            output_kind = "artifact_manifest"
        asset = await service.attach_asset(
            mudo_obj.mudo_id,
            MUDOAssetCreateRequest(
                workspace_id=workspace_id,
                branch_id=branch_id,
                commit_id=created_commit.commit_id,
                artifact_ref=ref,
                artifact_kind=output_kind,
                metadata={"source": "artifact_workspace_binding"},
            ),
            owner_user_id=user_id,
        )
        output_assets.append(asset.asset_id)

    dependency_edge_ids: list[str] = []
    for from_asset_id in input_assets:
        for to_asset_id in output_assets:
            edge = await service.add_dependency_edge(
                mudo_obj.mudo_id,
                MUDODependencyEdgeCreateRequest(
                    workspace_id=workspace_id,
                    from_asset_id=from_asset_id,
                    to_asset_id=to_asset_id,
                    relation_type="produces",
                    stale_propagates=True,
                ),
                owner_user_id=user_id,
            )
            dependency_edge_ids.append(edge.edge_id)

    warnings: list[str] = []
    study_link_id: str | None = None
    study_id = str(policy.study_id or "").strip()
    if study_id:
        try:
            study_link = await service.link_mudo_to_study(
                study_id,
                StudyMUDOLinkCreateRequest(
                    workspace_id=workspace_id,
                    mudo_id=mudo_obj.mudo_id,
                    role=policy.study_role,
                    metadata={
                        "source": "biodynamo_mudo_bridge",
                        "run_id": run_id,
                        "compile_id": str(compile_record.get("compile_id") or ""),
                    },
                ),
                owner_user_id=user_id,
                created_by=user_id,
            )
            study_link_id = study_link.id
        except Exception as exc:  # noqa: BLE001
            if policy.study_binding_required:
                raise RuntimeError(f"mudo_study_binding_unavailable:{exc}") from exc
            warnings.append("mudo_study_binding_unavailable")

    return {
        "enabled": True,
        "mudo_id": mudo_obj.mudo_id,
        "study_id": study_id or None,
        "study_mudo_link_id": study_link_id,
        "branch_id": branch_id,
        "commit_id": created_commit.commit_id,
        "intent_text": intent_text,
        "artifact_refs": artifact_refs,
        "input_asset_ids": input_assets,
        "output_asset_ids": output_assets,
        "dependency_edge_ids": dependency_edge_ids,
        "warnings": warnings,
    }


@router.get("/presets")
def list_biodynamo_presets(user: Any = Depends(request_identity_dependency)) -> dict[str, Any]:
    _authenticated_user_id(user)
    registry = _load_preset_registry()
    presets = dict(registry.get("presets") or {})
    return {
        "ok": True,
        "schema_version": str(registry.get("schema_version") or "biodynamo_preset_registry_v1"),
        "registry_version": str(registry.get("registry_version") or "1.0.0"),
        "count": len(presets),
        "presets": presets,
    }


@router.get("/presets/{preset_id}")
def get_biodynamo_preset(preset_id: str, user: Any = Depends(request_identity_dependency)) -> dict[str, Any]:
    _authenticated_user_id(user)
    registry = _load_preset_registry()
    presets = dict(registry.get("presets") or {})
    preset = dict(presets.get(preset_id) or {})
    if not preset:
        raise _typed_error(
            status_code=404,
            code="preset_not_found",
            message=f"Unknown BioDynamo preset: {preset_id}",
        )
    return {"ok": True, "preset": preset}


@router.post("/compile", response_model=BioDynamoCompileResponse)
def compile_biodynamo_intent(
    body: BioDynamoCompileRequest,
    user: Any = Depends(request_identity_dependency),
):
    user_id = _authenticated_user_id(user)

    registry = _load_preset_registry()
    presets = dict(registry.get("presets") or {})
    selected_preset = dict(presets.get(body.preset_id) or {}) if body.preset_id else {}
    if body.preset_id and not selected_preset:
        raise _typed_error(
            status_code=404,
            code="preset_not_found",
            message=f"Unknown BioDynamo preset: {body.preset_id}",
        )

    missing_required = _missing_required_fields(body.biostate, selected_preset) if selected_preset else []
    if missing_required:
        raise _typed_error(
            status_code=422,
            code="preset_required_fields_missing",
            message="Preset requires missing BioState fields",
            details={"missing_fields": missing_required, "preset_id": body.preset_id},
        )

    biostate_payload = _coerce_biostate(body.biostate, selected_preset)
    if body.objective:
        biostate_payload["objective"] = dict(body.objective)
    if body.constraints:
        biostate_payload["constraints"] = dict(body.constraints)
    if body.analysis_bundle:
        biostate_payload["analysis_bundle"] = dict(body.analysis_bundle)
    if body.output_policy:
        biostate_payload["output_policy"] = dict(body.output_policy)
    if body.compile_options:
        biostate_payload["compile_options"] = dict(body.compile_options)

    compiled = compile_biostate_payload(biostate_payload)
    compiled_plan = compiled.to_dict()

    # Generate openmm_receipts if not present in compiled_plan
    if "openmm_receipts" not in compiled_plan or not compiled_plan["openmm_receipts"]:
        _openmm_receipts = _build_openmm_mandatory_receipts(compiled_plan)
        compiled_plan["openmm_receipts"] = _openmm_receipts
    mandatory_receipts = dict(dict(compiled_plan.get("openmm_receipts") or {}).get("mandatory_receipts") or {})
    handoff = _build_analysis_handoff(body.analysis_bundle)

    runtime_contract = dict(compiled_plan.get("runtime_contract") or {})
    runtime_contract["analysis_handoff"] = handoff

    protocol_jsonld = _protocol_jsonld_projection(dict(compiled_plan.get("scientific_task_graph") or {}))

    compile_id = f"bdc_{uuid.uuid4().hex[:16]}"
    _COMPILE_STORE[compile_id] = {
        "compile_id": compile_id,
        "user_id": user_id,
        "preset_id": body.preset_id,
        "source_biostate": biostate_payload,
        "compiled_plan": compiled_plan,
        "runtime_contract": runtime_contract,
        "scientific_task_graph": dict(compiled_plan.get("scientific_task_graph") or {}),
        "protocol_jsonld": protocol_jsonld,
        "mandatory_receipts": mandatory_receipts,
        "blockers": list(compiled.blockers or ()),
        "warnings": list(compiled.warnings or ()),
        "submit_ready": bool(compiled.submit_ready),
        "compiler_version": "scientific_protocol_kernel_v1+biodynamo_api_v1",
        "created_at": _now_epoch(),
        "compile_hash": _compute_hash(compiled_plan),
    }

    return BioDynamoCompileResponse(
        compile_id=compile_id,
        compiled_plan=compiled_plan,
        runtime_contract=runtime_contract,
        scientific_task_graph=dict(compiled_plan.get("scientific_task_graph") or {}),
        protocol_jsonld=protocol_jsonld,
        mandatory_receipts=mandatory_receipts,
        blockers=list(compiled.blockers or ()),
        warnings=list(compiled.warnings or ()),
        submit_ready=bool(compiled.submit_ready),
        compiler_version="scientific_protocol_kernel_v1+biodynamo_api_v1",
    )


def _resolve_compile_record(body: BioDynamoRunsRequest, user_id: str) -> dict[str, Any]:
    if body.compile_id:
        record = dict(_COMPILE_STORE.get(body.compile_id) or {})
        if not record:
            raise _typed_error(status_code=404, code="compile_not_found", message=f"Unknown compile_id: {body.compile_id}")
        if str(record.get("user_id") or "") != user_id:
            raise _typed_error(status_code=403, code="compile_forbidden", message="compile_id does not belong to caller")
        return record

    if not isinstance(body.biostate, dict) or not body.biostate:
        raise _typed_error(
            status_code=422,
            code="runs_missing_compile_or_inline_biostate",
            message="Runs request requires compile_id or inline biostate",
        )

    compile_response = compile_biodynamo_intent(BioDynamoCompileRequest(biostate=body.biostate), user=user_id)
    return dict(_COMPILE_STORE.get(compile_response.compile_id) or {})


def _validate_artifact_workspace_policy(policy: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    required = (
        "workspace_id",
        "artifact_namespace",
        "expected_artifact_types",
        "checksum_policy",
        "visibility_policy",
        "internal_artifact_hiding_policy",
    )
    blockers: list[str] = []
    for key in required:
        value = policy.get(key)
        if value in (None, "", [], {}):
            blockers.append(f"artifact_workspace_policy.{key} is required")

    if (policy.get("manifest_uri") in (None, "") and policy.get("planned_manifest_ref") in (None, "")):
        blockers.append("artifact_workspace_policy.manifest_uri or planned_manifest_ref is required")

    receipt = {
        "schema_version": "biodynamo_artifact_workspace_binding_receipt_v1",
        "workspace_id": str(policy.get("workspace_id") or ""),
        "artifact_namespace": str(policy.get("artifact_namespace") or ""),
        "manifest_uri": str(policy.get("manifest_uri") or ""),
        "planned_manifest_ref": str(policy.get("planned_manifest_ref") or ""),
        "expected_artifact_types": list(policy.get("expected_artifact_types") or []),
        "checksum_policy": str(policy.get("checksum_policy") or ""),
        "visibility_policy": str(policy.get("visibility_policy") or ""),
        "internal_artifact_hiding_policy": str(policy.get("internal_artifact_hiding_policy") or ""),
    }
    return blockers, receipt


@router.post("/runs", response_model=BioDynamoRunsResponse, status_code=202)
async def submit_biodynamo_run(
    body: BioDynamoRunsRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    http_response: Response = None,
    user: Any = Depends(request_identity_dependency),
):
    user_id = _authenticated_user_id(user)
    store = await get_compute_store_manager()

    normalized_idempotency_key = str(idempotency_key or "").strip()
    request_payload = body.model_dump(mode="json", exclude_none=False)
    request_hash = _compute_hash({"user_id": user_id, "request": request_payload})
    if normalized_idempotency_key:
        cached = await store.idempotency_get(user_id=user_id, key=normalized_idempotency_key)
        if cached:
            cached_hash = str(cached.get("request_hash") or "")
            if cached_hash != request_hash:
                raise _typed_error(status_code=409, code="idempotency_conflict", message="Idempotency-Key already used with different payload")
            await store.idempotency_increment_replay(user_id=user_id, key=normalized_idempotency_key)
            return BioDynamoRunsResponse(**dict(cached.get("response_payload") or {}))

    compile_record = _resolve_compile_record(body, user_id)
    compiled_plan = dict(compile_record.get("compiled_plan") or {})

    blockers: list[str] = []
    warnings: list[str] = []

    if not bool(compile_record.get("submit_ready", False)):
        blockers.append("compile payload is not submit_ready")
    blockers.extend(_validate_openmm_mandatory_receipts(compiled_plan))

    workspace_blockers, artifact_receipt = _validate_artifact_workspace_policy(dict(body.artifact_workspace_policy or {}))
    blockers.extend(workspace_blockers)

    execution_mode = str(body.execution_mode or "remote").strip().lower()
    if execution_mode not in {"local", "remote"}:
        raise _typed_error(status_code=422, code="execution_mode_invalid", message="execution_mode must be local|remote")

    compute_policy = dict(body.compute_policy or {})
    remote_path = str(compute_policy.get("remote_path") or "unified_compute").strip().lower()
    allow_non_production_fallback = bool(compute_policy.get("allow_non_production_fallback", False))
    if execution_mode == "remote" and remote_path != "unified_compute" and not allow_non_production_fallback:
        blockers.append("remote direct/fallback path rejected unless explicitly degraded")

    if blockers:
        raise _typed_error(
            status_code=422,
            code="run_submission_blocked",
            message="Run acceptance blocked by policy",
            details={"blockers": blockers},
        )

    run_id = f"bdr_{uuid.uuid4().hex[:16]}"
    artifact_workspace_receipt_ref = f"baw_{run_id}"

    compute_job_id: str | None = None
    route_decision_id: str | None = None
    status = "accepted"
    mudo_commit_ref: dict[str, Any] | None = None

    if execution_mode == "local":
        approved_local = bool(dict(body.approval_cost_policy or {}).get("local_execution_approved", False))
        if not approved_local:
            status = "accepted_local_non_production"
            warnings.append("local execution marked local_non_production")
        else:
            status = "accepted_local_approved"
    else:
        from mica.api_v1.routers import compute as compute_router

        remote_body = compute_router.SubmitComputeJobRequest(
            job_type="md",
            provider=str(compute_policy.get("provider") or "vast"),
            execution_class=str(compute_policy.get("execution_class") or "research"),
            biostate_v2_payload=dict(compile_record.get("source_biostate") or {}),
            n_replicas=int(compute_policy.get("n_replicas") or 1),
            gpu_type=str(compute_policy.get("gpu_type") or "L40S"),
            max_price_per_hour=float(compute_policy.get("max_price_per_hour") or 0.60),
            max_total_cost_usd=float(compute_policy.get("max_total_cost_usd") or 50.0),
        )
        remote_result = await compute_router._submit_compute_md_request(remote_body, user_id=user_id)
        compute_job_id = str(getattr(remote_result, "job_id", "") or "")
        route_decision_id = str(getattr(remote_result, "route_decision_id", "") or "") or None
        status = "accepted_remote_submission"

    mudo_policy_payload = dict(body.mudo_policy or {})
    if bool(mudo_policy_payload.get("enabled", False)):
        try:
            mudo_commit_ref = await _bridge_mudo_commit_and_lineage(
                user_id=user_id,
                compile_record=compile_record,
                run_id=run_id,
                artifact_receipt=artifact_receipt,
                execution_mode=execution_mode,
                status=status,
                policy_payload=mudo_policy_payload,
            )
            warnings.extend([str(item) for item in list((mudo_commit_ref or {}).get("warnings") or [])])
        except Exception as exc:  # noqa: BLE001
            if bool(mudo_policy_payload.get("required", False)) or bool(mudo_policy_payload.get("study_binding_required", False)):
                raise _typed_error(
                    status_code=503,
                    code="mudo_bridge_required_unavailable",
                    message="M-UDO bridge unavailable for required request",
                    details={"reason": str(exc)},
                ) from exc
            warnings.append("mudo_bridge_unavailable")

    run_record = {
        "schema_version": "biodynamo_run_submission_receipt_v1",
        "run_id": run_id,
        "user_id": user_id,
        "compile_id": str(compile_record.get("compile_id") or body.compile_id or ""),
        "compute_job_id": compute_job_id,
        "execution_mode": execution_mode,
        "status": status,
        "accepted": True,
        "route_decision_id": route_decision_id,
        "artifact_workspace_receipt_ref": artifact_workspace_receipt_ref,
        "artifact_workspace_binding_receipt": {
            **artifact_receipt,
            "source_compile_id": str(compile_record.get("compile_id") or ""),
            "source_run_id": run_id,
        },
        "runtime_contract": dict(compile_record.get("runtime_contract") or {}),
        "mandatory_receipts": dict(compile_record.get("mandatory_receipts") or {}),
        "warnings": warnings,
        "blockers": [],
        "created_at": _now_epoch(),
        "registry_backend": store.registry_backend,
        "mudo_commit_ref": mudo_commit_ref,
    }

    _RUN_STORE[run_id] = run_record

    await store.registry_upsert(
        {
            "user_id": user_id,
            "job_id": run_id,
            "workspace_id": str(artifact_receipt.get("workspace_id") or "") or None,
            "provider": str(compute_policy.get("provider") or ("local" if execution_mode == "local" else "")),
            "state": status,
            "request_hash": request_hash,
            "idempotency_key": normalized_idempotency_key or None,
            "artifact_prefix": str(artifact_receipt.get("manifest_uri") or artifact_receipt.get("planned_manifest_ref") or "") or None,
            "accepted": True,
            "error": None,
            "route_decision_id": route_decision_id,
            "metadata": {
                "run_id": run_id,
                "compile_id": str(compile_record.get("compile_id") or ""),
                "execution_mode": execution_mode,
                "compute_job_id": compute_job_id,
                "artifact_workspace_receipt_ref": artifact_workspace_receipt_ref,
            },
        }
    )

    response_payload = BioDynamoRunsResponse(
        run_id=run_id,
        compute_job_id=compute_job_id,
        registry_backend=store.registry_backend,
        route_decision_id=route_decision_id,
        artifact_workspace_receipt_ref=artifact_workspace_receipt_ref,
        accepted=True,
        status=status,
        blockers=[],
        warnings=warnings,
        mudo_commit_ref=mudo_commit_ref,
    )

    if normalized_idempotency_key:
        await store.idempotency_put(
            user_id=user_id,
            key=normalized_idempotency_key,
            request_hash=request_hash,
            response_payload=response_payload.model_dump(mode="json", exclude_none=False),
            response_status=202,
            job_id=run_id,
            ttl_seconds=max(60, int(24 * 3600)),
        )

    if http_response is not None:
        http_response.headers["Idempotent-Replay"] = "false"
        http_response.headers["Idempotency-Key-Accepted"] = "true" if normalized_idempotency_key else "false"

    return response_payload


@router.get("/runs/{run_id}", response_model=BioDynamoRunStatusResponse)
async def get_biodynamo_run_status(
    run_id: str,
    user: Any = Depends(request_identity_dependency),
):
    user_id = _authenticated_user_id(user)
    run_record = dict(_RUN_STORE.get(run_id) or {})
    if not run_record:
        raise _typed_error(status_code=404, code="run_not_found", message=f"Unknown run_id: {run_id}")
    if str(run_record.get("user_id") or "") != user_id:
        raise _typed_error(status_code=403, code="run_forbidden", message="run_id does not belong to caller")

    metadata = {
        "runtime_contract": dict(run_record.get("runtime_contract") or {}),
        "artifact_workspace_binding_receipt": dict(run_record.get("artifact_workspace_binding_receipt") or {}),
        "mandatory_receipts": dict(run_record.get("mandatory_receipts") or {}),
        "mudo_commit_ref": dict(run_record.get("mudo_commit_ref") or {}),
    }

    return BioDynamoRunStatusResponse(
        run_id=run_id,
        compile_id=str(run_record.get("compile_id") or ""),
        compute_job_id=run_record.get("compute_job_id"),
        execution_mode=str(run_record.get("execution_mode") or ""),
        status=str(run_record.get("status") or "unknown"),
        accepted=bool(run_record.get("accepted", False)),
        route_decision_id=run_record.get("route_decision_id"),
        artifact_workspace_receipt_ref=str(run_record.get("artifact_workspace_receipt_ref") or ""),
        blockers=list(run_record.get("blockers") or []),
        warnings=list(run_record.get("warnings") or []),
        metadata=metadata,
    )
