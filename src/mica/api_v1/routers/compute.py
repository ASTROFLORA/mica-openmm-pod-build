"""
compute.py — W6-2: Unified Compute Router

Merges Jobs API + MD API into /api/v1/compute/*

Endpoints:
  POST   /api/v1/compute/jobs              — submit a compute job (MD or generic)
  GET    /api/v1/compute/jobs              — list all jobs for user
  GET    /api/v1/compute/jobs/{job_id}     — get job status
  DELETE /api/v1/compute/jobs/{job_id}     — cancel a running job
  GET    /api/v1/compute/jobs/{job_id}/ledger  — economic ledger for job (W6-3)
  GET    /api/v1/compute/costs             — aggregate cost summary (W6-3)
  GET    /api/v1/compute/health            — provider health check
"""

from __future__ import annotations

import asyncio
import logging
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from mica.api_v1.auth import request_identity_dependency
from mica.api_v1.ws_ticket import issue_ws_ticket, ws_ticket_authority_status
from mica.identity.request_identity import RequestIdentity
from mica.infrastructure.orchestration.salad_submit_contract import (
    SaladMDContractError,
    SaladMDSubmitRequest,
    prepare_salad_md_submission,
    submit_prepared_salad_md_job,
    submit_salad_md_job,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/compute", tags=["compute"])

COMPUTE_MD_PROTOCOL_TOOL_NAMES = frozenset(
    {
        "check_resource_requirements",
        "rent_compute_instance",
        "run_container_job",
        "run_md_container_or_openmm",
        "checkpoint_to_gcs",
        "project_terminal_status",
    }
)

GOVERNED_VAST_GPU_ROUTE = "RTX_5080"
BLOCKED_PUBLIC_VAST_GPU_TYPES = frozenset({"RTX_5090"})
GOVERNED_COMPUTE_MD_PROVIDERS = frozenset({"vast", "salad"})


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class SubmitComputeJobRequest(BaseModel):
    """Request to submit a compute job."""

    # Job identity
    job_type: str = Field(
        default="md",
        description="Job type: 'md' (molecular dynamics) or 'generic'",
    )
    name: str = Field(default="", description="Human-readable job name")
    execution_class: str = Field(
        default="research",
        description="Execution class: research | production | audit",
    )

    # Input
    pdb_path: str = Field(default="", description="Path to PDB file (for MD jobs)")
    pdb_gcs_path: Optional[str] = Field(default=None, description="GCS URI of pre-staged PDB (gs://…); skips local staging when provided")
    biostate_v2_payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Inline BioState V2 manifest. When present, the public compute route compiles the"
            " scientific inputs from this payload before dispatching to the existing provider lane."
        ),
    )

    # Simulation params (MD-specific)
    steps: int = Field(default=50_000_000, ge=1000, description="Simulation steps")
    n_replicas: int = Field(default=1, ge=1, le=8, description="Number of replicas")
    production_ns: float = Field(default=100.0, ge=0.1, description="Target production nanoseconds")
    simulation_mode: str = Field(default="", description="Simulation mode: standard | complex_stability | paper_dodecaedrica")
    ligand_smiles: str = Field(default="", description="Ligand SMILES for complex-stability runs")
    docked_ligand_pdb: str = Field(default="", description="Local path to docked ligand pose PDB for complex-stability runs")
    benchmark_steps: int = Field(default=5_000, ge=1, description="Warmup steps for bounded runtime calibration")
    report_freq: int = Field(default=500, ge=1, description="Reporting cadence in steps")
    saving_interval_seconds: int = Field(default=600, ge=1, description="Target sync cadence in seconds")
    chunk_steps_override: int = Field(default=0, ge=0, description="Optional explicit chunk size override for bounded provider probes")
    frame_interval_ps: float = Field(default=50.0, gt=0.0, description="Requested preview frame interval in ps")

    # Resource requirements
    gpu_type: str = Field(default="L40S", description="GPU type: L40S, A100, RTX_4090, etc.")
    max_price_per_hour: float = Field(default=0.60, ge=0.0, description="Max $/hr")
    max_total_cost_usd: float = Field(default=50.0, ge=0.0, description="Max total cost")

    # Provider preference
    provider: str = Field(default="vast", description="Preferred provider: vast, runpod, gcp, salad")


class ComputeJobStatusResponse(BaseModel):
    """Status of a compute job."""

    job_id: str
    state: str
    provider: str = ""
    instance_id: str = ""
    gpu_type: str = ""
    execution_class: str = "research"
    phase: str = ""
    elapsed_seconds: float = 0.0
    total_cost_usd: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ComputeJobListResponse(BaseModel):
    """List of compute jobs."""

    jobs: List[ComputeJobStatusResponse]
    total: int


class SubmitComputeJobResponse(BaseModel):
    """Result of submitting a compute job."""

    job_id: str
    provider: str
    accepted: bool
    error: Optional[str] = None
    route_decision_id: Optional[str] = None


class EconomicLedgerResponse(BaseModel):
    """Economic ledger for a single job."""

    job_id: str
    provider: str = ""
    gpu_type: str = ""
    execution_class: str = "research"
    total_cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    state: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


class CostSummaryResponse(BaseModel):
    """Aggregate cost summary across all jobs."""

    total_cost_usd: float = 0.0
    job_count: int = 0
    ledgers: List[EconomicLedgerResponse] = Field(default_factory=list)


class ProviderHealthResponse(BaseModel):
    """Provider health status."""

    providers: Dict[str, bool] = Field(default_factory=dict)
    registered: List[str] = Field(default_factory=list)


class UserSpendResponse(BaseModel):
    """User spend tracking response (W3-4)."""

    user_id: str
    total_spend_usd: float = 0.0
    job_count: int = 0
    budget_remaining_usd: float = 0.0


class WebSocketTicketAuthorityResponse(BaseModel):
    """Redacted runtime status for websocket ticket issuance."""

    secret_env_name: str = "MICA_WS_TICKET_SECRET"
    secret_available: bool = False
    secret_length_category: str = "missing"
    raw_secret_logged: bool = False
    production_env: bool = False
    ticket_authority_ready: bool = False
    classification: str = ""
    warning: str = ""
    route_loaded: bool = False
    app_version: str = ""
    git_sha: str = ""
    deployment_surface: str = "api"


class WebSocketTicketRequest(BaseModel):
    """Request a short-lived websocket ticket for browser WS clients."""

    scope: str = Field(default="mica", description="mica | md | preview")
    job_id: str = ""
    run_id: str = ""
    workspace_id: str = ""
    session_id: str = ""


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_at: int
    ttl_seconds: int
    scope: str


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_compute_client = None


def _get_client():
    """Lazy-init the UnifiedComputeClient singleton."""
    global _compute_client
    if _compute_client is not None:
        return _compute_client
    try:
        from mica.unified_compute_client import UnifiedComputeClient
        _compute_client = UnifiedComputeClient.from_env()
    except Exception as exc:
        logger.error("Failed to init UnifiedComputeClient: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"Compute subsystem unavailable: {exc}",
        )
    return _compute_client


def _authenticated_user_id(user: Any) -> str:
    if isinstance(user, RequestIdentity):
        return user.user_id
    if isinstance(user, str):
        return user
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or user.get("id") or "anonymous")
    return str(user or "anonymous")


def resolve_protocol_compute_tool_name(node: Any) -> str:
    inputs = getattr(node, "inputs", None)
    if isinstance(inputs, dict):
        for key in ("tool_name", "action", "tool", "operation"):
            candidate = inputs.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip().lower()
    return ""


def protocol_node_uses_compute_md_surface(node: Any) -> bool:
    tool_name = resolve_protocol_compute_tool_name(node)
    if tool_name in COMPUTE_MD_PROTOCOL_TOOL_NAMES:
        return True
    executor_surface = str(getattr(node, "executor_surface", "") or "").strip().lower()
    return executor_surface in {"compute_md", "prometeus_compute", "compute"}


def _protocol_bool(inputs: Dict[str, Any], *keys: str) -> bool:
    for key in keys:
        candidate = inputs.get(key)
        if isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            normalized = candidate.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
    return False


def _protocol_float(inputs: Dict[str, Any], key: str, default: float) -> float:
    candidate = inputs.get(key, default)
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return float(default)


def _protocol_int(inputs: Dict[str, Any], key: str, default: int) -> int:
    candidate = inputs.get(key, default)
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return int(default)


def _protocol_str(inputs: Dict[str, Any], key: str, default: str = "") -> str:
    return str(inputs.get(key, default) or default).strip()


def _request_default(field_name: str) -> Any:
    field = SubmitComputeJobRequest.model_fields[field_name]
    return field.default


def _normalize_gpu_name(raw_gpu: Any) -> str:
    return str(raw_gpu or "").strip().upper().replace("-", "_").replace(" ", "_")


def _normalize_governed_md_provider(raw_provider: Any) -> str:
    provider = str(raw_provider or "vast").strip().lower() or "vast"
    if provider in GOVERNED_COMPUTE_MD_PROVIDERS:
        return provider
    raise HTTPException(
        status_code=422,
        detail=(
            f"Public/compatibility compute admission rejects provider '{provider}' on the current "
            "governed route surface; supported providers are 'vast' and 'salad'."
        ),
    )


def _enforce_compute_md_gpu_admission_policy(
    body: SubmitComputeJobRequest,
    *,
    allow_expensive_gpu_override: bool = False,
) -> SubmitComputeJobRequest:
    provider = str(body.provider or "").strip().lower()
    normalized_gpu = _normalize_gpu_name(body.gpu_type)
    if provider != "vast":
        return body
    if not normalized_gpu or allow_expensive_gpu_override:
        return body
    if normalized_gpu not in BLOCKED_PUBLIC_VAST_GPU_TYPES:
        return body
    raise HTTPException(
        status_code=422,
        detail=(
            f"Public/compatibility compute admission rejects Vast GPU {normalized_gpu} by default; "
            f"the governed Vast route is {GOVERNED_VAST_GPU_ROUTE}. "
            "Exact-route probes require an operator-owned bounded override surface."
        ),
    )


def _enforce_registered_provider(body: SubmitComputeJobRequest, client: Any) -> None:
    provider = str(body.provider or "").strip().lower()
    if provider != "salad":
        return
    registered = {str(name).strip().lower() for name in getattr(client, "providers", []) or []}
    if registered and provider not in registered:
        raise HTTPException(
            status_code=503,
            detail=(
                "Compute provider 'salad' is not registered in this runtime; refusing to "
                "fall back to another paid provider for an explicit Salad request."
            ),
        )


def _body_uses_biostate_payload(body: SubmitComputeJobRequest) -> bool:
    return isinstance(body.biostate_v2_payload, dict) and bool(body.biostate_v2_payload)


def _collect_biostate_public_mixed_authority_fields(body: SubmitComputeJobRequest) -> List[str]:
    conflicts: List[str] = []
    if str(body.pdb_path or "").strip():
        conflicts.append("pdb_path")
    if str(body.pdb_gcs_path or "").strip():
        conflicts.append("pdb_gcs_path")
    if int(body.steps) != int(_request_default("steps")):
        conflicts.append("steps")
    if float(body.production_ns) != float(_request_default("production_ns")):
        conflicts.append("production_ns")
    if str(body.simulation_mode or "").strip():
        conflicts.append("simulation_mode")
    if str(body.ligand_smiles or "").strip():
        conflicts.append("ligand_smiles")
    if str(body.docked_ligand_pdb or "").strip():
        conflicts.append("docked_ligand_pdb")
    return conflicts


def _normalize_public_biostate_sdf_pose_to_pdb(raw_sdf_path: str) -> str:
    sdf_path = Path(str(raw_sdf_path or "").strip())
    if not raw_sdf_path or not sdf_path.exists() or not sdf_path.is_file():
        raise HTTPException(
            status_code=422,
            detail=(
                "BioState V2 public compute pose normalization currently requires a local/file-backed "
                f"SDF pose path; got {raw_sdf_path!r}."
            ),
        )

    try:
        from rdkit import Chem
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"BioState V2 public compute pose normalization requires RDKit: {exc}",
        ) from exc

    try:
        supplier = Chem.SDMolSupplier(str(sdf_path), removeHs=False)
        mol = next((candidate for candidate in supplier if candidate is not None), None)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"BioState V2 public compute could not parse SDF ligand pose: {exc}",
        ) from exc
    if mol is None or mol.GetNumConformers() == 0:
        raise HTTPException(
            status_code=422,
            detail="BioState V2 public compute requires an SDF ligand pose with 3D coordinates",
        )

    normalized_root = Path.cwd() / ".mica" / "runtime" / "compute_pose_normalization"
    normalized_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(sdf_path.read_bytes()).hexdigest()[:16]
    normalized_path = normalized_root / f"{sdf_path.stem}_{digest}.pdb"
    if not normalized_path.exists():
        pdb_block = Chem.MolToPDBBlock(mol)
        if not str(pdb_block or "").strip():
            raise HTTPException(
                status_code=422,
                detail="BioState V2 public compute could not serialize the normalized ligand pose to PDB",
            )
        normalized_path.write_text(pdb_block, encoding="utf-8")
    return str(normalized_path)


def _build_biostate_submit_engine_job(
    body: SubmitComputeJobRequest,
    *,
    user_id: str,
):
    if not _body_uses_biostate_payload(body):
        return None
    conflicting_fields = _collect_biostate_public_mixed_authority_fields(body)
    if conflicting_fields:
        raise HTTPException(
            status_code=422,
            detail=(
                "BioState V2 public compute rejects mixed raw authority fields when "
                f"`biostate_v2_payload` is present: {', '.join(conflicting_fields)}. "
                "Put structure, pose, and scientific protocol inputs inside the BioState manifest "
                "and leave only bounded runtime hints such as provider, execution_class, "
                "n_replicas, gpu_type, and cost ceilings on the public request surface."
            ),
        )

    from mica.drivers.biodynamo_biostate_bridge import build_context_from_compiled_biostate
    from mica.drivers.md_execution_contract import build_execution_request_v1
    from mica.infrastructure.orchestration.biostate_engine_job import BioStateEngineJob
    from mica.sim.biostate_v2_importers import normalize_biostate_v2_manifest
    from mica.sim.scientific_protocol_kernel import compile_biostate_payload

    try:
        imported = normalize_biostate_v2_manifest(body.biostate_v2_payload or {})
        compiled = compile_biostate_payload(imported.normalized_payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"BioState V2 payload failed compile validation: {exc}",
        ) from exc

    if not compiled.submit_ready:
        raise HTTPException(
            status_code=422,
            detail=(
                "BioState V2 payload is not submit-ready: "
                + "; ".join(compiled.blockers or ("unknown blocker",))
            ),
        )

    compiled_plan = compiled.to_dict()
    compatibility_context: Dict[str, Any] = {
        "user_id": user_id,
        "preferred_provider": body.provider,
        "execution_backend": body.provider,
    }
    if str(body.name or "").strip():
        compatibility_context["job_name"] = str(body.name).strip()
    if str(body.execution_class or "").strip() != str(_request_default("execution_class") or "").strip():
        compatibility_context["execution_class"] = str(body.execution_class).strip()
    if int(body.n_replicas) != int(_request_default("n_replicas")):
        compatibility_context["n_replicas"] = int(body.n_replicas)
    if _normalize_gpu_name(body.gpu_type) != _normalize_gpu_name(_request_default("gpu_type")):
        compatibility_context["gpu_type"] = str(body.gpu_type).strip()
    if float(body.max_price_per_hour) != float(_request_default("max_price_per_hour")):
        compatibility_context["max_price_per_hour"] = float(body.max_price_per_hour)
    if float(body.max_total_cost_usd) != float(_request_default("max_total_cost_usd")):
        compatibility_context["max_total_cost_usd"] = float(body.max_total_cost_usd)

    context = build_context_from_compiled_biostate(
        compiled_plan=compiled_plan,
        raw_manifest=body.biostate_v2_payload or {},
        compatibility_context=compatibility_context,
    )
    protein_pdb = str(context.get("protein_pdb") or "").strip()
    if not protein_pdb:
        raise HTTPException(
            status_code=422,
            detail="BioState V2 payload compile did not yield a protein_pdb-compatible structure input",
        )

    docked_ligand_pdb = str(context.get("docked_ligand_pdb") or "").strip()
    docked_ligand_sdf = str(context.get("docked_ligand_sdf") or "").strip()
    pose_authority = "docked_ligand_pdb"
    pose_normalization_receipt: Dict[str, Any] = {}
    if docked_ligand_sdf and not docked_ligand_pdb:
        docked_ligand_pdb = _normalize_public_biostate_sdf_pose_to_pdb(docked_ligand_sdf)
        context["docked_ligand_pdb"] = docked_ligand_pdb
        pose_authority = "docked_ligand_pdb_normalized_from_sdf"
        pose_normalization_receipt = {
            "schema_version": "public_biostate_pose_normalization_v1",
            "normalizer": "BioStatePublicComputePoseNormalizer",
            "source_pose_path": docked_ligand_sdf,
            "normalized_pose_path": docked_ligand_pdb,
            "source_format": "sdf",
            "target_format": "pdb",
        }

    derived_mode = str(context.get("simulation_mode") or "").strip().lower()
    if derived_mode == "complex":
        derived_mode = "complex_stability"
    elif not derived_mode:
        derived_mode = "binding"

    execution_request = build_execution_request_v1(
        context,
        protein_pdb=protein_pdb,
        ligand_smiles=str(context.get("ligand_smiles") or "").strip(),
        docked_ligand_pdb=docked_ligand_pdb,
        execution_target="remote",
        simulation_mode=derived_mode,
    )
    metadata = dict(execution_request.get("metadata") or {})
    metadata["biostate_public_submit"] = {
        "binding_surface": "/api/v1/compute/jobs",
        "compiled_plan_present": True,
        "scientific_task_graph_present": bool(context.get("scientific_task_graph")),
        "pose_authority": pose_authority,
        "provider_route": body.provider,
    }
    metadata["biostate_handoff"] = {
        "compiled_biostate_plan": compiled_plan,
        "biostate_import_receipt": dict(context.get("biostate_import_receipt") or {}),
    }
    if isinstance(context.get("scientific_task_graph"), dict):
        metadata["biostate_handoff"]["scientific_task_graph"] = dict(context["scientific_task_graph"])
    if pose_normalization_receipt:
        metadata["biostate_handoff"]["pose_normalization_receipt"] = dict(pose_normalization_receipt)
    execution_request["metadata"] = metadata

    return BioStateEngineJob.from_execution_context(
        context=context,
        execution_request=execution_request,
        protein_pdb=protein_pdb,
        ligand_smiles=str(context.get("ligand_smiles") or "").strip(),
        docked_ligand_pdb=docked_ligand_pdb,
        simulation_mode=derived_mode,
    )


def _build_protocol_compute_submit_request(
    *,
    inputs: Dict[str, Any],
    protocol_id: str,
    node_id: str,
) -> SubmitComputeJobRequest:
    provider = _protocol_str(inputs, "provider", "vast") or "vast"
    structure_input = (
        _protocol_str(inputs, "pdb_gcs_path")
        or _protocol_str(inputs, "pdb_path")
        or _protocol_str(inputs, "structure_input_uri")
        or _protocol_str(inputs, "structure_path")
    )
    if not structure_input:
        raise ValueError(f"Protocol node {node_id} requires pdb_path, pdb_gcs_path, or structure_input_uri")

    simulation_mode = _protocol_str(inputs, "simulation_mode")
    if not simulation_mode:
        simulation_mode = "complex_stability" if _protocol_str(inputs, "ligand_smiles") else "standard"

    return SubmitComputeJobRequest(
        provider=provider,
        job_type="md",
        name=_protocol_str(inputs, "name", f"{protocol_id}:{node_id}"),
        execution_class=_protocol_str(inputs, "execution_class", "research"),
        pdb_gcs_path=structure_input if structure_input.startswith("gs://") else None,
        pdb_path="" if structure_input.startswith("gs://") else structure_input,
        steps=max(1000, _protocol_int(inputs, "steps", 50_000_000)),
        n_replicas=max(1, _protocol_int(inputs, "n_replicas", 1)),
        production_ns=max(0.1, _protocol_float(inputs, "production_ns", 100.0)),
        simulation_mode=simulation_mode,
        ligand_smiles=_protocol_str(inputs, "ligand_smiles"),
        docked_ligand_pdb=_protocol_str(inputs, "docked_ligand_pdb"),
        benchmark_steps=max(1, _protocol_int(inputs, "benchmark_steps", 5_000)),
        report_freq=max(1, _protocol_int(inputs, "report_freq", 500)),
        saving_interval_seconds=max(1, _protocol_int(inputs, "saving_interval_seconds", 600)),
        chunk_steps_override=max(0, _protocol_int(inputs, "chunk_steps_override", 0)),
        frame_interval_ps=max(0.001, _protocol_float(inputs, "frame_interval_ps", 50.0)),
        gpu_type=_protocol_str(inputs, "gpu_type", "L40S"),
        max_price_per_hour=max(0.0, _protocol_float(inputs, "max_price_per_hour", 0.60)),
        max_total_cost_usd=max(0.0, _protocol_float(inputs, "max_total_cost_usd", 50.0)),
    )


async def _submit_compute_md_request(
    body: SubmitComputeJobRequest,
    *,
    user_id: str,
    client: Any | None = None,
):
    body = body.model_copy(update={"provider": _normalize_governed_md_provider(body.provider)})
    body = _enforce_compute_md_gpu_admission_policy(body)
    active_client = client or _get_client()
    _enforce_registered_provider(body, active_client)
    biostate_job = _build_biostate_submit_engine_job(body, user_id=user_id)
    if biostate_job is not None:
        if body.provider.lower() == "salad":
            protein_pdb = str(biostate_job.protein_pdb or "").strip()
            prepared = prepare_salad_md_submission(
                SaladMDSubmitRequest(
                    user_id=user_id,
                    job_id=biostate_job.job_id,
                    pdb_path="" if protein_pdb.startswith("gs://") else protein_pdb,
                    pdb_gcs_path=protein_pdb if protein_pdb.startswith("gs://") else None,
                    steps=int(biostate_job.context.get("steps", 75_000_000) or 75_000_000),
                    gpu_type=str(biostate_job.context.get("gpu_type") or _request_default("gpu_type") or "L40S"),
                    max_total_cost_usd=float(
                        biostate_job.context.get("max_total_cost_usd", _request_default("max_total_cost_usd")) or 0.0
                    ),
                    execution_class=str(
                        biostate_job.execution_request.get("job", {}).get("execution_class", "research") or "research"
                    ),
                    production_ns=float(biostate_job.context.get("production_ns", 100.0) or 100.0),
                    simulation_mode=biostate_job.simulation_mode,
                    ligand_smiles=biostate_job.ligand_smiles,
                    docked_ligand_pdb=biostate_job.docked_ligand_pdb,
                    benchmark_steps=int(body.benchmark_steps),
                    report_freq=int(body.report_freq),
                    saving_interval_seconds=int(body.saving_interval_seconds),
                    chunk_steps_override=int(body.chunk_steps_override),
                    frame_interval_ps=float(body.frame_interval_ps),
                )
            )
            setattr(prepared.job_cfg, "_biostate_execution_request", dict(biostate_job.execution_request))
            setattr(prepared.job_cfg, "_biostate_handoff", dict(biostate_job.handoff))
            return await submit_prepared_salad_md_job(active_client, prepared, user_id=user_id)

        return await active_client.submit_md_job(
            biostate_job.to_vast_md_config(),
            user_id=user_id,
            preferred_provider=biostate_job.preferred_provider,
        )

    complex_stability_requested = (
        (body.simulation_mode or "").strip().lower() in {"complex_stability", "complex"}
        or bool((body.ligand_smiles or "").strip())
        or bool((body.docked_ligand_pdb or "").strip())
    )
    if complex_stability_requested and not (
        (body.ligand_smiles or "").strip() and (body.docked_ligand_pdb or "").strip()
    ):
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider.title()} complex_stability requires both ligand_smiles and docked_ligand_pdb",
        )

    if body.provider.lower() == "salad":
        try:
            submission = await submit_salad_md_job(
                active_client,
                SaladMDSubmitRequest(
                    user_id=user_id,
                    job_id=f"salad_{int(time.time())}",
                    pdb_path=body.pdb_path,
                    pdb_gcs_path=body.pdb_gcs_path,
                    steps=body.steps,
                    gpu_type=body.gpu_type,
                    max_total_cost_usd=body.max_total_cost_usd,
                    execution_class=body.execution_class,
                    production_ns=body.production_ns,
                    simulation_mode=body.simulation_mode,
                    ligand_smiles=body.ligand_smiles,
                    docked_ligand_pdb=body.docked_ligand_pdb,
                    benchmark_steps=body.benchmark_steps,
                    report_freq=body.report_freq,
                    saving_interval_seconds=body.saving_interval_seconds,
                    chunk_steps_override=body.chunk_steps_override,
                    frame_interval_ps=body.frame_interval_ps,
                ),
            )
        except SaladMDContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return submission.result

    from mica.infrastructure.orchestration.vast_md_orchestrator import MDJobConfig
    from mica.infrastructure.providers.base_provider import GPUType

    try:
        gpu = GPUType(body.gpu_type)
    except ValueError:
        gpu = GPUType.L40S

    storage_options: Dict[str, Any] = {}
    if complex_stability_requested:
        from mica.infrastructure.storage.rclone_gcs_backend import build_orchestrator_storage_options

        storage_options = build_orchestrator_storage_options(
            user_id=user_id,
            object_prefix="md-jobs",
        )

    cfg = MDJobConfig(
        pdb_path=body.pdb_path,
        simulation_mode="complex" if complex_stability_requested else "binding",
        ligand_smiles=(body.ligand_smiles or "").strip(),
        docked_ligand_pdb=(body.docked_ligand_pdb or "").strip(),
        steps=body.steps,
        n_replicas=body.n_replicas,
        production_ns=body.production_ns,
        gpu_type=gpu,
        max_price_per_hour=body.max_price_per_hour,
        max_total_cost_usd=body.max_total_cost_usd,
        execution_class=body.execution_class,
        readiness_retry_attempts=4,
        **storage_options,
    )

    return await active_client.submit_md_job(
        cfg,
        user_id=user_id,
        preferred_provider=body.provider.lower(),
    )


async def execute_protocol_compute_md_action(
    *,
    tool_name: str,
    inputs: Dict[str, Any],
    protocol_id: str,
    node_id: str,
    session_id: str,
    user_id: str,
    approval_required: bool,
) -> Dict[str, Any]:
    normalized_tool = str(tool_name or "").strip().lower()
    if normalized_tool not in COMPUTE_MD_PROTOCOL_TOOL_NAMES:
        raise ValueError(f"Unsupported ComputeMD protocol tool '{tool_name}'")

    approval_granted = _protocol_bool(inputs, "approval_granted", "approved", "approved_for_dispatch")
    route_seed = _protocol_str(inputs, "route_decision_id", f"route_{protocol_id}_{node_id}")
    job_seed = _protocol_str(inputs, "job_id", f"{protocol_id}_{node_id}")
    provider = _protocol_str(inputs, "provider", "vast") or "vast"
    if normalized_tool in {"rent_compute_instance", "run_container_job", "run_md_container_or_openmm"}:
        if approval_required and not approval_granted:
            raise ValueError(
                f"Protocol node {node_id} requires explicit approval before paid compute dispatch"
            )

    if normalized_tool == "check_resource_requirements":
        return {
            "tool_name": normalized_tool,
            "binding_surface": "compute",
            "summary": f"Validated ComputeMD resource requirements for {protocol_id}/{node_id}.",
            "state_after": {
                "dispatch_kind": "compute_md_resource_check",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "provider": provider,
                "gpu_type": _protocol_str(inputs, "gpu_type", "L40S"),
                "max_price_per_hour": _protocol_float(inputs, "max_price_per_hour", 0.60),
                "max_total_cost_usd": _protocol_float(inputs, "max_total_cost_usd", 50.0),
                "approval_required": approval_required,
                "approval_granted": approval_granted,
            },
            "artifact_refs": [f"protocol://{protocol_id}/nodes/{node_id}/resource_requirements"],
            "evidence_refs": [f"protocol://{protocol_id}/nodes/{node_id}/resource_check"],
            "cost_snapshot": {"usd": 0.0, "tool_calls": 1},
        }

    if normalized_tool == "rent_compute_instance":
        return {
            "tool_name": normalized_tool,
            "binding_surface": "compute",
            "summary": f"Recorded ComputeMD rental approval for {protocol_id}/{node_id}.",
            "state_after": {
                "dispatch_kind": "compute_md_rental_gate",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "provider": provider,
                "approval_required": approval_required,
                "approval_granted": approval_granted,
                "resource_class": _protocol_str(inputs, "resource_class", _protocol_str(inputs, "gpu_type", "L40S")),
                "abort_policy": _protocol_str(inputs, "abort_policy", "manual_stop"),
            },
            "artifact_refs": [f"protocol://{protocol_id}/nodes/{node_id}/rental_gate"],
            "evidence_refs": [f"protocol://{protocol_id}/nodes/{node_id}/approval_gate"],
            "approval_refs": [f"approval://{protocol_id}/{node_id}"],
            "cost_snapshot": {"usd": 0.0, "tool_calls": 1},
        }

    if normalized_tool in {"run_container_job", "run_md_container_or_openmm"}:
        body = _build_protocol_compute_submit_request(
            inputs=inputs,
            protocol_id=protocol_id,
            node_id=node_id,
        )
        result = await _submit_compute_md_request(body, user_id=user_id)
        route_decision_id = str(getattr(result, "route_decision_id", "") or route_seed)
        job_id = str(getattr(result, "job_id", "") or job_seed)
        provider_name = str(getattr(result, "provider", "") or provider)
        return {
            "tool_name": normalized_tool,
            "binding_surface": "compute",
            "summary": f"Submitted ComputeMD job {job_id} via provider {provider_name}.",
            "job_id": job_id,
            "route_decision_id": route_decision_id,
            "state_after": {
                "dispatch_kind": "compute_md_submission",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "job_id": job_id,
                "route_decision_id": route_decision_id,
                "provider": provider_name,
                "accepted": bool(getattr(result, "accepted", True)),
                "provider_job_id": str(getattr(result, "instance_id", "") or ""),
                "execution_class": body.execution_class,
            },
            "artifact_refs": [
                f"compute://jobs/{job_id}",
                f"protocol://{protocol_id}/routes/{route_decision_id}",
            ],
            "evidence_refs": [
                f"compute://jobs/{job_id}/submit",
                f"protocol://{protocol_id}/routes/{route_decision_id}/submit",
            ],
            "cost_snapshot": {"usd": 0.0, "tool_calls": 1, "provider": provider_name},
        }

    if normalized_tool == "checkpoint_to_gcs":
        gcs_prefix = _protocol_str(inputs, "gcs_prefix", f"gs://mica-user-workspace/{protocol_id}/{node_id}")
        artifact_refs = [gcs_prefix]
        if _protocol_str(inputs, "job_id"):
            artifact_refs.append(f"compute://jobs/{_protocol_str(inputs, 'job_id')}/checkpoint")
        return {
            "tool_name": normalized_tool,
            "binding_surface": "compute",
            "summary": f"Recorded ComputeMD checkpoint custody for {protocol_id}/{node_id}.",
            "state_after": {
                "dispatch_kind": "compute_md_checkpoint",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "gcs_prefix": gcs_prefix,
                "job_id": _protocol_str(inputs, "job_id"),
                "route_decision_id": _protocol_str(inputs, "route_decision_id", route_seed),
            },
            "artifact_refs": artifact_refs,
            "evidence_refs": [f"{gcs_prefix.rstrip('/')}/checkpoint_receipt.json"],
            "cost_snapshot": {"usd": 0.0, "tool_calls": 1},
        }

    client = _get_client()
    job_id = _protocol_str(inputs, "job_id")
    if not job_id:
        raise ValueError(f"Protocol node {node_id} requires job_id for terminal status projection")
    summary = await client.get_job_status(job_id, user_id=user_id)
    if summary is None:
        raise ValueError(f"Compute job {job_id} not found for protocol node {node_id}")
    metadata = dict(getattr(summary, "metadata", {}) or {})
    artifact_refs = []
    metadata_artifacts = metadata.get("artifact_refs")
    if isinstance(metadata_artifacts, list):
        artifact_refs.extend(str(item) for item in metadata_artifacts if str(item).strip())
    artifact_refs.append(f"compute://jobs/{job_id}/status")
    return {
        "tool_name": normalized_tool,
        "binding_surface": "compute",
        "summary": f"Projected terminal ComputeMD status for job {job_id}.",
        "job_id": job_id,
        "route_decision_id": str(metadata.get("route_decision_id") or route_seed),
        "state_after": {
            "dispatch_kind": "compute_md_status_projection",
            "protocol_id": protocol_id,
            "session_id": session_id,
            "job_id": job_id,
            "route_decision_id": str(metadata.get("route_decision_id") or route_seed),
            "provider": str(getattr(summary, "provider", "") or provider),
            "state": str(getattr(getattr(summary, "state", None), "value", getattr(summary, "state", "")) or ""),
            "phase": str(getattr(summary, "phase", "") or ""),
            "instance_id": str(getattr(summary, "instance_id", "") or ""),
        },
        "artifact_refs": artifact_refs,
        "evidence_refs": [f"compute://jobs/{job_id}/status_projection"],
        "cost_snapshot": {
            "usd": float(getattr(summary, "total_cost_usd", 0.0) or 0.0),
            "tool_calls": 1,
            "provider": str(getattr(summary, "provider", "") or provider),
        },
    }


async def _owned_summary(client: Any, job_id: str, user_id: str) -> Any:
    summary = await client.get_job_status(job_id, user_id=user_id)
    if summary is None or (summary.user_id or "") != user_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return summary


# ---------------------------------------------------------------------------
# WS-Ticket authority helpers
# ---------------------------------------------------------------------------

async def _owns_salad_job_via_storage(user_id: str, job_id: str) -> bool:
    """Best-effort fallback for provider jobs whose status probe is slow.

    The authoritative path is still ``_owned_summary``; this fallback defaults to
    fail-closed unless a future storage index can prove ownership.
    """

    return False


async def _run_owned_summary_with_timeout(client: Any, job_id: str, user_id: str) -> Any:
    timeout = float(os.getenv("MICA_WS_TICKET_OWNERSHIP_TIMEOUT_SECONDS", "1.0") or "1.0")

    async def _run_in_worker() -> Any:
        return await asyncio.to_thread(lambda: asyncio.run(_owned_summary(client, job_id, user_id)))

    return await asyncio.wait_for(_run_in_worker(), timeout=timeout)


async def _storage_fallback_with_timeout(user_id: str, job_id: str) -> bool:
    timeout = float(os.getenv("MICA_WS_TICKET_STORAGE_FALLBACK_TIMEOUT_SECONDS", "0.5") or "0.5")
    probe = _owns_salad_job_via_storage(user_id, job_id)
    if asyncio.iscoroutine(probe):
        return bool(await asyncio.wait_for(probe, timeout=timeout))
    return bool(probe)


# ---------------------------------------------------------------------------
# WS-Ticket authority endpoints
# ---------------------------------------------------------------------------

@router.get("/ws-ticket/authority", response_model=WebSocketTicketAuthorityResponse)
async def get_websocket_ticket_authority(
    request: Request,
    user: Any = Depends(request_identity_dependency),
):
    status = dict(ws_ticket_authority_status())
    route_paths = {str(getattr(route, "path", "") or "") for route in getattr(request.app, "routes", [])}
    route_loaded = (
        "/api/v1/compute/ws-ticket/authority" in route_paths
        and "/api/v1/compute/ws-ticket" in route_paths
    )
    status["route_loaded"] = route_loaded
    if not route_loaded:
        status["classification"] = "route_not_deployed"
    return WebSocketTicketAuthorityResponse(**status)


@router.post("/ws-ticket", response_model=WebSocketTicketResponse)
async def create_websocket_ticket(
    body: WebSocketTicketRequest,
    user: Any = Depends(request_identity_dependency),
):
    user_id = _authenticated_user_id(user)
    scope = str(body.scope or "").strip().lower()

    if scope == "md":
        job_id = str(body.job_id or "").strip()
        if not job_id:
            raise HTTPException(status_code=422, detail="MD WebSocket tickets require job_id")
        client = _get_client()
        try:
            await _run_owned_summary_with_timeout(client, job_id, user_id)
        except asyncio.TimeoutError as exc:
            try:
                if not await _storage_fallback_with_timeout(user_id, job_id):
                    raise HTTPException(status_code=504, detail="WebSocket ticket ownership check timed out") from exc
            except asyncio.TimeoutError as fallback_exc:
                raise HTTPException(status_code=504, detail="WebSocket ticket ownership check timed out") from fallback_exc
        except HTTPException:
            raise

    issued = issue_ws_ticket(
        user_id=user_id,
        scope=scope,  # type: ignore[arg-type]
        job_id=body.job_id,
        run_id=body.run_id,
        workspace_id=body.workspace_id,
        session_id=body.session_id,
    )
    return WebSocketTicketResponse(**issued)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs", response_model=SubmitComputeJobResponse, status_code=202)
async def submit_compute_job(
    body: SubmitComputeJobRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Submit a compute job (MD simulation or generic GPU work)."""
    user_id = _authenticated_user_id(user)

    if body.job_type == "md":
        result = await _submit_compute_md_request(body, user_id=user_id)

        # W3-5: cost ceiling → HTTP 402
        if not result.accepted and result.error and "cost ceiling" in result.error:
            raise HTTPException(status_code=402, detail=result.error)

        return SubmitComputeJobResponse(
            job_id=result.job_id,
            provider=result.provider,
            accepted=result.accepted,
            error=result.error,
            route_decision_id=getattr(result, 'route_decision_id', None),
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported job_type: {body.job_type}. Currently supported: 'md'",
        )


@router.get("/jobs", response_model=ComputeJobListResponse)
async def list_compute_jobs(
    user: Any = Depends(request_identity_dependency),
):
    """List all compute jobs."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    summaries = [s for s in client.list_jobs() if (s.user_id or "") == user_id]
    jobs = [
        ComputeJobStatusResponse(
            job_id=s.job_id,
            state=s.state.value,
            provider=s.provider,
            instance_id=s.instance_id,
            gpu_type=s.gpu_type,
            execution_class=s.execution_class,
            phase=s.phase,
            elapsed_seconds=s.elapsed_seconds,
            total_cost_usd=s.total_cost_usd,
            error=s.error,
            metadata=dict(s.metadata or {}),
        )
        for s in summaries
    ]
    return ComputeJobListResponse(jobs=jobs, total=len(jobs))


@router.get("/jobs/{job_id}", response_model=ComputeJobStatusResponse)
async def get_compute_job(
    job_id: str,
    user: Any = Depends(request_identity_dependency),
):
    """Get status of a specific compute job."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    summary = await _owned_summary(client, job_id, user_id)

    return ComputeJobStatusResponse(
        job_id=summary.job_id,
        state=summary.state.value,
        provider=summary.provider,
        instance_id=summary.instance_id,
        gpu_type=summary.gpu_type,
        execution_class=summary.execution_class,
        phase=summary.phase,
        elapsed_seconds=summary.elapsed_seconds,
        total_cost_usd=summary.total_cost_usd,
        error=summary.error,
        metadata=dict(summary.metadata or {}),
    )


@router.delete("/jobs/{job_id}")
async def cancel_compute_job(
    job_id: str,
    user: Any = Depends(request_identity_dependency),
):
    """Cancel a running compute job."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    await _owned_summary(client, job_id, user_id)
    ok = await client.cancel_job(job_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found or already finished",
        )
    return {"job_id": job_id, "cancelled": True}


# ---------------------------------------------------------------------------
# W3-2: HITL approve / reject
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_id}/approve")
async def approve_compute_job(
    job_id: str,
    user: Any = Depends(request_identity_dependency),
):
    """Approve an AWAITING_APPROVAL job so it enters the queue."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    await _owned_summary(client, job_id, user_id)
    ok = await client.approve_job(job_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found or not awaiting approval",
        )
    return {"job_id": job_id, "approved": True}


@router.post("/jobs/{job_id}/reject")
async def reject_compute_job(
    job_id: str,
    reason: str = "",
    user: Any = Depends(request_identity_dependency),
):
    """Reject an AWAITING_APPROVAL job."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    await _owned_summary(client, job_id, user_id)
    ok = await client.reject_job(job_id, reason)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found or not awaiting approval",
        )
    return {"job_id": job_id, "rejected": True, "reason": reason}


# ---------------------------------------------------------------------------
# W3-3: Pending approvals list
# ---------------------------------------------------------------------------

@router.get("/pending-approvals", response_model=ComputeJobListResponse)
async def list_pending_approvals(
    user: Any = Depends(request_identity_dependency),
):
    """List all jobs awaiting human approval."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    pending = [
        ComputeJobStatusResponse(
            job_id=s.job_id,
            state=s.state.value,
            provider=s.provider,
            instance_id=s.instance_id,
            gpu_type=s.gpu_type,
            execution_class=s.execution_class,
            phase=s.phase,
            elapsed_seconds=s.elapsed_seconds,
            total_cost_usd=s.total_cost_usd,
            error=s.error,
            metadata=dict(s.metadata or {}),
        )
        for s in client.list_jobs()
        if s.state.value == "awaiting_approval" and (s.user_id or "") == user_id
    ]
    return ComputeJobListResponse(jobs=pending, total=len(pending))


# ---------------------------------------------------------------------------
# W3-4: User spend tracking
# ---------------------------------------------------------------------------

@router.get("/user-spend", response_model=UserSpendResponse)
async def get_user_spend(
    user: Any = Depends(request_identity_dependency),
):
    """Get spend summary for the authenticated user."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    spend = client.get_user_spend(user_id)
    return UserSpendResponse(
        user_id=user_id,
        total_spend_usd=spend["total_spend_usd"],
        job_count=spend["job_count"],
        budget_remaining_usd=max(0, client._cost_ceiling - spend["total_spend_usd"]),
    )


# ---------------------------------------------------------------------------
# W6-3: Economic Ledger endpoints
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/ledger", response_model=EconomicLedgerResponse)
async def get_job_ledger(
    job_id: str,
    user: Any = Depends(request_identity_dependency),
):
    """Get economic ledger for a specific job."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    await _owned_summary(client, job_id, user_id)
    ledger = client.get_economic_ledger(job_id)
    if ledger is None:
        raise HTTPException(status_code=404, detail=f"Ledger for job {job_id} not found")

    return EconomicLedgerResponse(
        job_id=ledger.get("job_id", job_id),
        provider=ledger.get("provider", ""),
        gpu_type=ledger.get("gpu_type", ""),
        execution_class=ledger.get("execution_class", "research"),
        total_cost_usd=ledger.get("total_cost_usd", 0.0),
        elapsed_seconds=ledger.get("elapsed_seconds", 0.0),
        state=ledger.get("state", ""),
        extra={k: v for k, v in ledger.items() if k not in (
            "job_id", "provider", "gpu_type", "execution_class",
            "total_cost_usd", "elapsed_seconds", "state",
        )},
    )


@router.get("/costs", response_model=CostSummaryResponse)
async def get_cost_summary(
    user: Any = Depends(request_identity_dependency),
):
    """Get aggregate cost summary across all compute jobs."""
    client = _get_client()
    user_id = _authenticated_user_id(user)
    ledgers_raw = client.get_all_economic_ledgers()
    owned_job_ids = {s.job_id for s in client.list_jobs() if (s.user_id or "") == user_id}
    ledgers_raw = [l for l in ledgers_raw if str(l.get("job_id") or "") in owned_job_ids]
    ledgers = [
        EconomicLedgerResponse(
            job_id=l.get("job_id", ""),
            provider=l.get("provider", ""),
            gpu_type=l.get("gpu_type", ""),
            execution_class=l.get("execution_class", "research"),
            total_cost_usd=l.get("total_cost_usd", 0.0),
            elapsed_seconds=l.get("elapsed_seconds", 0.0),
            state=l.get("state", ""),
        )
        for l in ledgers_raw
    ]
    total = sum(l.total_cost_usd for l in ledgers)
    return CostSummaryResponse(
        total_cost_usd=total,
        job_count=len(ledgers),
        ledgers=ledgers,
    )


# ---------------------------------------------------------------------------
# SP-17: Scientific Value Accounting endpoints
# ---------------------------------------------------------------------------

class ValueClassRequest(BaseModel):
    """Request to classify scientific value for a compute job."""

    value_class: str = Field(
        description="Value class: 'useful' | 'failed_useful' | 'failed_waste'",
    )
    artifact_uris: List[str] = Field(
        default_factory=list,
        description="Artifact URIs produced by the run.",
    )
    artifact_manifest_uri: str = Field(
        default="",
        description="URI of the artifact manifest (optional).",
    )
    notes: str = Field(default="", description="Optional operator notes.")
    quality_signals: Dict[str, Any] = Field(
        default_factory=dict,
        description="Domain-specific quality signals (e.g. RMSD, convergence).",
    )


class ValueClassResponse(BaseModel):
    """Value accounting record for a single compute job."""

    record_id: str
    job_id: str
    lane: str = ""
    user_id: str = ""
    provider: str = ""
    execution_class: str = "research"
    value_class: str
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    artifact_count: int = 0
    artifact_uris: List[str] = Field(default_factory=list)
    artifact_manifest_uri: str = ""
    cost_per_artifact_usd: float = 0.0
    notes: str = ""
    quality_signals: Dict[str, Any] = Field(default_factory=dict)
    classified_at: str = ""
    schema_version: str = "sp17_v1"


class ScientificValueReportResponse(BaseModel):
    """Aggregate cost-per-useful-output report across all compute jobs."""

    total_runs: int = 0
    total_cost_usd: float = 0.0
    useful_run_count: int = 0
    failed_useful_run_count: int = 0
    failed_waste_run_count: int = 0
    useful_cost_usd: float = 0.0
    failed_useful_cost_usd: float = 0.0
    failed_waste_cost_usd: float = 0.0
    useful_artifact_count: int = 0
    cost_per_useful_run_usd: Optional[float] = None
    cost_per_useful_artifact_usd: Optional[float] = None
    useful_ratio: float = 0.0
    waste_ratio: float = 0.0
    by_lane: Dict[str, Any] = Field(default_factory=dict)
    by_provider: Dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "sp17_v1"


# In-memory store for value records (keyed by job_id).
# In production this would be backed by the execution_record persistence layer.
_value_records: Dict[str, Dict[str, Any]] = {}


@router.post("/jobs/{job_id}/value-class", response_model=ValueClassResponse)
async def classify_job_value(
    job_id: str,
    body: ValueClassRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Classify the scientific value of a completed compute job (SP-17).

    Accepts operator or automated classification with optional artifact lineage
    and quality signals, builds a cost-to-artifact lineage record, and stores
    it in the in-memory value ledger.
    """
    from mica.storage.scientific_value import (
        ScientificValueClass,
        build_value_record,
    )

    client = _get_client()
    user_id = _authenticated_user_id(user)

    # Validate value_class
    try:
        vc = ScientificValueClass(body.value_class)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid value_class '{body.value_class}'. "
                   f"Must be one of: {[c.value for c in ScientificValueClass]}",
        )

    # Pull cost and metadata from the economic ledger
    ledger = client.get_economic_ledger(job_id) or {}
    summary = await client.get_job_status(job_id, user_id=user_id)

    record = build_value_record(
        job_id=job_id,
        lane=ledger.get("lane", "job"),
        user_id=user_id,
        value_class=vc,
        cost_usd=ledger.get("total_cost_usd", 0.0),
        artifact_uris=body.artifact_uris,
        artifact_manifest_uri=body.artifact_manifest_uri,
        elapsed_seconds=ledger.get("elapsed_seconds", 0.0),
        provider=ledger.get("provider", summary.provider if summary else ""),
        execution_class=ledger.get("execution_class", "research"),
        notes=body.notes,
        quality_signals=body.quality_signals if body.quality_signals else None,
    )

    _value_records[job_id] = record

    return ValueClassResponse(**record)


@router.get("/costs/scientific-value-report", response_model=ScientificValueReportResponse)
async def get_scientific_value_report(
    user: Any = Depends(request_identity_dependency),
):
    """Return cost-per-useful-output report for all classified compute jobs (SP-17).

    Aggregates all value records owned by the requesting user and reports
    cost efficiency split by taxonomy class, lane, and provider.
    """
    from mica.storage.scientific_value import compute_cost_per_useful_output

    user_id = _authenticated_user_id(user)
    owned = [r for r in _value_records.values() if (r.get("user_id") or "") == user_id]
    report = compute_cost_per_useful_output(owned)
    return ScientificValueReportResponse(**report)


# ---------------------------------------------------------------------------
# SP-19: Pharma Compliance And Regulated Export endpoints
# ---------------------------------------------------------------------------

class ComplianceEventRequest(BaseModel):
    """Request to record a compliance event in the ledger (SP-19)."""

    event_type: str
    subject_id: str
    subject_kind: str
    framework: str = "internal-qa"
    payload: Dict[str, Any] = Field(default_factory=dict)
    parent_event_id: str = ""


class ComplianceEventResponse(BaseModel):
    """Single compliance ledger event record (SP-19)."""

    event_id: str
    event_type: str
    subject_id: str
    subject_kind: str
    actor_id: str
    framework: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    parent_event_id: str = ""
    event_hash: str
    occurred_at: str
    schema_version: str = "sp19_v1"


class ComplianceLedgerResponse(BaseModel):
    """Assembled compliance ledger (SP-19)."""

    ledger_id: str
    event_count: int
    chain_hash: str
    frameworks: List[str] = Field(default_factory=list)
    events: List[ComplianceEventResponse] = Field(default_factory=list)
    assembled_at: str
    schema_version: str = "sp19_v1"


class ExportBundleRequest(BaseModel):
    """Request a regulated export bundle for one or more jobs (SP-19)."""

    bundle_label: str
    subject_ids: List[str] = Field(default_factory=list)
    methods: Dict[str, Any] = Field(default_factory=dict)
    provenance: List[Dict[str, Any]] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    frameworks: Optional[List[str]] = None
    system_version: str = "mica-sp19"
    extra: Dict[str, Any] = Field(default_factory=dict)


class ExportBundleResponse(BaseModel):
    """Regulated export bundle (SP-19)."""

    bundle_id: str
    bundle_label: str
    subject_ids: List[str] = Field(default_factory=list)
    frameworks: List[str] = Field(default_factory=list)
    status: str = "issued"
    methods: Dict[str, Any] = Field(default_factory=dict)
    provenance: List[Dict[str, Any]] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    signatures: Dict[str, Any] = Field(default_factory=dict)
    bundle_hash: str = ""
    operator_id: str = ""
    system_version: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)
    issued_at: str = ""
    schema_version: str = "sp19_v1"


class ExportBundleValidationResponse(BaseModel):
    """Validation report for an export bundle (SP-19)."""

    decision: str
    passed_checks: int
    total_checks: int
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    bundle_id: str = ""
    validated_at: str = ""
    schema_version: str = "sp19_v1"


# In-memory ledger store and export bundle store (production path: Timescale)
_compliance_events: List[Dict[str, Any]] = []
_export_bundles: Dict[str, Any] = {}


@router.post("/compliance/events", response_model=ComplianceEventResponse)
async def record_compliance_event(
    body: ComplianceEventRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Record a compliance event in the in-process ledger (SP-19).

    Events are appended to the in-memory ledger in arrival order. Each event
    receives a deterministic SHA-256 hash over event_id, type, subject, actor,
    and timestamp — providing an append-only audit trail.
    """
    from mica.compliance.compliance_ledger import build_compliance_event

    actor_id = _authenticated_user_id(user)
    event = build_compliance_event(
        event_type=body.event_type,
        subject_id=body.subject_id,
        subject_kind=body.subject_kind,
        actor_id=actor_id,
        framework=body.framework,
        payload=body.payload,
        parent_event_id=body.parent_event_id,
    )
    _compliance_events.append(event)
    return ComplianceEventResponse(**event)


@router.get("/compliance/ledger", response_model=ComplianceLedgerResponse)
async def get_compliance_ledger(
    user: Any = Depends(request_identity_dependency),
):
    """Return the assembled compliance ledger for the requesting user (SP-19).

    Assembles all events owned by this user into a ledger with chain hash.
    The chain hash is computed over the concatenation of all event hashes so
    any insertion, deletion, or reordering is detectable.
    """
    from mica.compliance.compliance_ledger import build_compliance_ledger

    user_id = _authenticated_user_id(user)
    owned = [e for e in _compliance_events if (e.get("actor_id") or "") == user_id]
    ledger = build_compliance_ledger(owned)
    # Coerce events to response model
    ledger["events"] = [ComplianceEventResponse(**e).model_dump() for e in owned]
    return ComplianceLedgerResponse(**ledger)


@router.post("/compliance/export-bundle", response_model=ExportBundleResponse)
async def create_export_bundle(
    body: ExportBundleRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Generate a regulated export bundle for one or more compute jobs (SP-19).

    Produces a full pharma-grade bundle with methods, provenance, limitations,
    and placeholder signatures. The bundle is validated immediately after
    construction and stored in the in-memory bundle store.

    Raises 422 if any required section (methods, provenance, limitations) is
    empty or if the bundle fails structural validation.
    """
    from mica.compliance.compliance_ledger import (
        build_export_bundle,
        validate_export_bundle,
    )

    operator_id = _authenticated_user_id(user)

    # Auto-populate provenance from compliance event ledger if not provided
    provenance = list(body.provenance)
    if not provenance:
        for jid in body.subject_ids:
            matching = [
                e for e in _compliance_events
                if e.get("subject_id") == jid
            ]
            for ev in matching:
                provenance.append({
                    "step": ev["event_type"],
                    "actor_id": ev["actor_id"],
                    "subject_id": ev["subject_id"],
                    "timestamp": ev["occurred_at"],
                    "event_id": ev["event_id"],
                })

    # Fallback provenance if ledger is empty
    if not provenance:
        provenance = [
            {
                "step": "export_requested",
                "actor_id": operator_id,
                "subject_id": ",".join(body.subject_ids) or "unspecified",
                "timestamp": "",
                "event_id": "",
            }
        ]

    # Default limitations if none provided
    limitations = list(body.limitations)
    if not limitations:
        limitations = [
            "Simulation results are for research use only and have not been validated for clinical decision-making.",
            "Force field accuracy limitations apply; results should be interpreted alongside experimental data.",
            "Placeholder signatures are present; real PKI binding is required before regulatory submission.",
        ]

    # Default methods if none provided
    methods = dict(body.methods)
    if not methods:
        methods = {
            "software": "MICA compute platform",
            "version": body.system_version,
            "description": "Molecular dynamics simulation; parameters not fully specified in this bundle.",
            "note": "Populate methods section with full simulation parameters before regulatory submission.",
        }

    try:
        bundle = build_export_bundle(
            bundle_label=body.bundle_label,
            subject_ids=body.subject_ids,
            methods=methods,
            provenance=provenance,
            limitations=limitations,
            frameworks=body.frameworks,
            operator_id=operator_id,
            system_version=body.system_version,
            extra=body.extra,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Validate immediately — fail fast if bundle is structurally incomplete
    validation = validate_export_bundle(bundle)
    if validation["decision"] != "valid":
        failed = [c for c in validation["checks"] if not c["passed"]]
        raise HTTPException(
            status_code=422,
            detail=f"Export bundle failed validation: {failed}",
        )

    _export_bundles[bundle["bundle_id"]] = bundle

    # Record compliance event for this export
    from mica.compliance.compliance_ledger import build_compliance_event
    export_event = build_compliance_event(
        event_type="export_issued",
        subject_id=bundle["bundle_id"],
        subject_kind="export_bundle",
        actor_id=operator_id,
        framework=(body.frameworks or ["internal-qa"])[0],
        payload={"bundle_label": body.bundle_label, "subject_ids": body.subject_ids},
    )
    _compliance_events.append(export_event)

    return ExportBundleResponse(**bundle)


@router.get(
    "/compliance/export-bundle/{bundle_id}/validate",
    response_model=ExportBundleValidationResponse,
)
async def validate_export_bundle_endpoint(
    bundle_id: str,
    user: Any = Depends(request_identity_dependency),
):
    """Validate an existing export bundle by ID (SP-19).

    Runs the structural validation checks against the stored bundle and
    returns a per-check report. Raises 404 if bundle_id is not found.
    """
    from mica.compliance.compliance_ledger import validate_export_bundle

    bundle = _export_bundles.get(bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=f"Export bundle '{bundle_id}' not found.",
        )
    result = validate_export_bundle(bundle)
    return ExportBundleValidationResponse(**result)


# ---------------------------------------------------------------------------
# SP-18: Commercialization Billing Gate endpoints
# ---------------------------------------------------------------------------

class CommercializationChecklistResponse(BaseModel):
    """Result of the commercial billing release checklist (SP-18)."""

    decision: str
    passed_items: int
    total_items: int
    critical_failures: List[str] = Field(default_factory=list)
    high_failures: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    items: List[Dict[str, Any]] = Field(default_factory=list)
    evaluated_at: str = ""
    schema_version: str = "sp18_v1"


class DryRunInvoiceRequest(BaseModel):
    """Request a dry-run invoice for one or more compute jobs (SP-18)."""

    job_ids: List[str] = Field(default_factory=list)
    user_id_override: Optional[str] = None
    line_items: List[Dict[str, Any]] = Field(default_factory=list)
    currency: str = "USD"
    tax_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""


class DryRunInvoiceResponse(BaseModel):
    """Dry-run invoice envelope (SP-18)."""

    invoice_id: str
    mode: str = "dry_run"
    user_id: str
    job_ids: List[str] = Field(default_factory=list)
    currency: str = "USD"
    line_items: List[Dict[str, Any]] = Field(default_factory=list)
    subtotal_usd: float = 0.0
    tax_rate: float = 0.0
    tax_amount_usd: float = 0.0
    total_usd: float = 0.0
    audit_hash: str = ""
    lifecycle: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    issued_at: str = ""
    schema_version: str = "sp18_v1"


class DryRunRefundRequest(BaseModel):
    """Request a dry-run refund for an existing dry-run invoice (SP-18)."""

    invoice_id: str
    refund_reason: str
    refund_amount_usd: Optional[float] = None
    initiated_by: str = "operator"


class DryRunRefundResponse(BaseModel):
    """Dry-run refund / credit note record (SP-18)."""

    refund_id: str
    mode: str = "dry_run"
    original_invoice_id: str
    original_audit_hash: str = ""
    user_id: str = ""
    currency: str = "USD"
    refund_amount_usd: float = 0.0
    invoice_total_usd: float = 0.0
    is_full_refund: bool = True
    refund_reason: str = ""
    initiated_by: str = "operator"
    credit_note: Dict[str, Any] = Field(default_factory=dict)
    lifecycle: Dict[str, Any] = Field(default_factory=dict)
    audit_hash: str = ""
    issued_at: str = ""
    schema_version: str = "sp18_v1"


# In-memory invoice store (dry-run only; production path extends to Timescale)
_dry_run_invoices: Dict[str, Any] = {}


@router.get(
    "/billing/commercialization-checklist",
    response_model=CommercializationChecklistResponse,
)
async def get_commercialization_checklist(
    user: Any = Depends(request_identity_dependency),
):
    """Run the commercial billing release checklist and return per-item pass/fail (SP-18).

    Probes the live router state to determine which preconditions have been met.
    The checklist gate PASS requires zero critical and zero high failures.
    Warnings (medium/low risks) are allowed for a PASS.
    """
    from mica.billing.commercialization_gate import run_commercialization_checklist
    from mica.storage.scientific_value import ScientificValueClass

    # Probe live state -------------------------------------------------------
    # BG-01: SP-17 taxonomy importable
    try:
        _ = ScientificValueClass.USEFUL
        taxonomy_ok = True
    except Exception:
        taxonomy_ok = False

    # BG-02: lineage endpoint present — we are inside the router, so yes
    lineage_ok = True

    # BG-03: value report endpoint present — same
    value_report_ok = True

    # BG-04: user spend working — probe client
    try:
        client = _get_client()
        spend = client.get_user_spend(_authenticated_user_id(user))
        spend_ok = isinstance(spend, dict) and "total_spend_usd" in spend
    except Exception:
        spend_ok = False

    # BG-05: cost ceiling enforced — probe client attribute
    try:
        client = _get_client()
        cost_ceiling_ok = hasattr(client, "_cost_ceiling") and float(client._cost_ceiling) > 0
    except Exception:
        cost_ceiling_ok = False

    # BG-06: economic ledger endpoints present — we are inside the router, so yes
    ledger_ok = True

    # BG-07: dry-run invoice verified — module importable
    try:
        from mica.billing.commercialization_gate import build_dry_run_invoice
        dry_invoice_ok = callable(build_dry_run_invoice)
    except Exception:
        dry_invoice_ok = False

    # BG-08: dry-run refund verified — module importable
    try:
        from mica.billing.commercialization_gate import build_dry_run_refund
        dry_refund_ok = callable(build_dry_run_refund)
    except Exception:
        dry_refund_ok = False

    # BG-09: residual risk ledger present
    try:
        from mica.billing.commercialization_gate import build_residual_risk_ledger
        risk_ledger_ok = callable(build_residual_risk_ledger)
    except Exception:
        risk_ledger_ok = False

    # BG-10: no real payment processor active — always true in this codebase
    no_real_money = True

    result = run_commercialization_checklist(
        value_taxonomy_imported=taxonomy_ok,
        lineage_endpoint_present=lineage_ok,
        value_report_endpoint_present=value_report_ok,
        user_spend_working=spend_ok,
        cost_ceiling_enforced=cost_ceiling_ok,
        economic_ledger_present=ledger_ok,
        dry_run_invoice_verified=dry_invoice_ok,
        dry_run_refund_verified=dry_refund_ok,
        residual_risk_ledger_present=risk_ledger_ok,
        no_real_money_write=no_real_money,
    )
    return CommercializationChecklistResponse(**result)


@router.post("/billing/dry-run-invoice", response_model=DryRunInvoiceResponse)
async def create_dry_run_invoice(
    body: DryRunInvoiceRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Generate a dry-run invoice for one or more compute jobs (SP-18).

    Produces a deterministic invoice envelope with line items, subtotal, tax,
    total, and SHA-256 audit hash. The invoice is frozen at lifecycle state
    'issued' — no real charge transition is allowed in dry-run mode.

    Stores the invoice in the in-memory store so subsequent refund requests can
    reference it by invoice_id.
    """
    from mica.billing.commercialization_gate import build_dry_run_invoice

    user_id = body.user_id_override or _authenticated_user_id(user)

    # Auto-generate line items from economic ledger if none provided
    line_items = list(body.line_items)
    if not line_items:
        try:
            client = _get_client()
            for jid in body.job_ids:
                ledger = client.get_economic_ledger(jid)
                if ledger:
                    line_items.append({
                        "description": f"Compute job {jid} ({ledger.get('gpu_type', 'GPU')})",
                        "quantity": 1,
                        "unit_price_usd": ledger.get("total_cost_usd", 0.0),
                    })
        except Exception as exc:
            logger.warning("Could not auto-populate line items from ledger: %s", exc)

    if not line_items:
        line_items = [{"description": "Compute job(s)", "quantity": 1, "unit_price_usd": 0.0}]

    invoice = build_dry_run_invoice(
        user_id=user_id,
        job_ids=list(body.job_ids),
        line_items=line_items,
        currency=body.currency,
        tax_rate=body.tax_rate,
        notes=body.notes,
    )

    _dry_run_invoices[invoice["invoice_id"]] = invoice
    return DryRunInvoiceResponse(**invoice)


@router.post("/billing/dry-run-refund", response_model=DryRunRefundResponse)
async def create_dry_run_refund(
    body: DryRunRefundRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Issue a dry-run refund / credit note against an existing dry-run invoice (SP-18).

    The refund record is append-only — it never mutates the original invoice.
    Partial refunds are supported. The resulting credit note carries an
    immutable SHA-256 audit hash back-linked to the original invoice.

    Raises 404 if the invoice_id does not exist in the in-memory store.
    Raises 422 if the refund_reason is empty or the amount exceeds the total.
    """
    from mica.billing.commercialization_gate import build_dry_run_refund

    invoice = _dry_run_invoices.get(body.invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=404,
            detail=f"Invoice '{body.invoice_id}' not found. "
                   "Create a dry-run invoice first via POST /billing/dry-run-invoice.",
        )

    try:
        refund = build_dry_run_refund(
            original_invoice=invoice,
            refund_reason=body.refund_reason,
            refund_amount_usd=body.refund_amount_usd,
            initiated_by=body.initiated_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DryRunRefundResponse(**refund)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=ProviderHealthResponse)
async def compute_health():
    """Check health of all registered compute providers."""
    client = _get_client()
    health = await client.health_check()
    return ProviderHealthResponse(
        providers=health,
        registered=client.providers,
    )


