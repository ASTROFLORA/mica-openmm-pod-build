"""
SMIC (Structural Molecular Interaction Computation) API Router — Phase R4.

Exposes MD trajectory analysis modules behind the ``MICA_ENABLE_SMIC_API`` feature flag.

Endpoints
---------
  GET  /api/v1/smic/modules                  – list all analysis modules (registry)
  GET  /api/v1/smic/modules/{analysis_type}  – single module info
  POST /api/v1/smic/analysis/{analysis_type} – submit analysis job
  GET  /api/v1/smic/jobs/{job_id}            – job status / result
  DELETE /api/v1/smic/jobs/{job_id}          – cancel / delete job
  GET  /api/v1/smic/status                   – SMIC subsystem status
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency as _user_dependency
from mica.worker.job_store import RedisJobStore

logger = logging.getLogger("mica.api.smic")

router = APIRouter()

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_SMIC_ENABLED = (
    os.getenv("MICA_ENABLE_SMIC_API") or ""
).strip().lower() in {"1", "true", "yes", "on"}

_SMIC_INPROCESS_FALLBACK = (
    os.getenv("MICA_SMIC_ALLOW_INPROCESS_FALLBACK") or ""
).strip().lower() in {"1", "true", "yes", "on"}


def current_runtime_surface() -> str:
    """Return the current runtime surface (api, worker, etc.)."""
    return (
        os.getenv("MICA_RUNTIME_SURFACE")
        or os.getenv("MICA_SERVICE_ROLE")
        or os.getenv("MICA_DEPLOYMENT_SURFACE")
        or "api"
    ).strip().lower()


def _require_smic() -> None:
    """Raise 503 if the SMIC subsystem is not enabled."""
    if not _SMIC_ENABLED:
        raise HTTPException(
            status_code=503,
            detail=(
                "SMIC API is not enabled. "
                "Set MICA_ENABLE_SMIC_API=true to activate."
            ),
        )


# ---------------------------------------------------------------------------
# Static module registry — mirrors smic_cli.discover_modules()
# ---------------------------------------------------------------------------

class ModuleInfo(BaseModel):
    """Metadata for a single SMIC analysis module."""
    key: str
    description: str
    runtime_tier: str  # fast | medium | heavy
    style: str  # standard | legacy | class-based | cli
    required_inputs: List[str]
    optional_inputs: List[str] = Field(default_factory=list)
    topology_formats: List[str] = Field(default_factory=lambda: ["pdb", "prmtop"])
    trajectory_formats: List[str] = Field(default_factory=lambda: ["dcd", "xtc", "trr"])
    required_tools: List[str] = Field(default_factory=list)
    produces: List[str] = Field(default_factory=lambda: ["csv", "json", "png"])


_MODULE_REGISTRY: Dict[str, ModuleInfo] = {}


def _build_registry() -> Dict[str, ModuleInfo]:
    """Build the static module registry for all 21 SMIC analysis types."""
    modules = [
        ModuleInfo(key="rmsd", description="RMSD/RMSF timeseries", runtime_tier="fast", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["stride", "skip_rmsf", "skip_plots"]),
        ModuleInfo(key="rmsd_pairwise", description="N×N pairwise RMSD matrix + clustering", runtime_tier="medium", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["selection", "max_frames", "stride"]),
        ModuleInfo(key="clustering", description="Conformational clustering (hierarchical + KMeans)", runtime_tier="medium", style="legacy", required_inputs=["topology", "trajectories"], optional_inputs=["stride"]),
        ModuleInfo(key="pca", description="Principal component analysis", runtime_tier="medium", style="class-based", required_inputs=["topology", "trajectories"], optional_inputs=["selection", "n_components", "stride", "use_incremental"]),
        ModuleInfo(key="tica", description="Time-lagged independent component analysis (deeptime)", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["lag_time", "n_components"]),
        ModuleInfo(key="binding", description="Binding distance analysis (COM-COM, RFxV, Phe insertion)", runtime_tier="fast", style="legacy", required_inputs=["topology", "trajectories"], optional_inputs=["stride"]),
        ModuleInfo(key="contacts", description="Contact map analysis", runtime_tier="fast", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["compute_q"]),
        ModuleInfo(key="contact_density", description="Per-residue contact density", runtime_tier="medium", style="class-based", required_inputs=["topology", "trajectory"], optional_inputs=["selection", "cutoff", "exclude_neighbors"]),
        ModuleInfo(key="convergence", description="Convergence diagnostics + block averaging", runtime_tier="fast", style="legacy", required_inputs=["topology", "trajectories"]),
        ModuleInfo(key="dccm", description="Dynamic cross-correlation matrix", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["engine", "selection", "align_selection"]),
        ModuleInfo(key="dssp", description="Secondary structure assignment via DSSP", runtime_tier="fast", style="standard", required_inputs=["pdb_path"], optional_inputs=["dssp_exe", "min_helix_len", "min_strand_len"], required_tools=["mkdssp"]),
        ModuleInfo(key="ifp", description="Interaction fingerprint profiling", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["stride", "protein_sel", "peptide_sel", "run_plip"]),
        ModuleInfo(key="interactions", description="PLIP-based molecular interactions (CLI)", runtime_tier="medium", style="cli", required_inputs=["topology", "trajectories", "output_root"], required_tools=["plip"]),
        ModuleInfo(key="interactions_general", description="General inter/intra-chain contact counts", runtime_tier="medium", style="standard", required_inputs=["topology", "trajectories", "output_root"], optional_inputs=["contact_cutoff_A", "max_frames"]),
        ModuleInfo(key="interactions_plip", description="PLIP interaction profiling with graph exports", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["peptide_chain_id", "analysis_stride", "min_occupancy"], required_tools=["plip"]),
        ModuleInfo(key="network", description="Residue network analysis (MD-TASK + local)", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectories"], optional_inputs=["engine", "contact_cutoff", "occupancy_threshold"]),
        ModuleInfo(key="prs", description="Perturbation response scanning", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectories", "final_pdb"], optional_inputs=["perturbations", "num_frames"]),
        ModuleInfo(key="water", description="Water interface analysis (streaming)", runtime_tier="medium", style="legacy", required_inputs=["topology", "trajectories"], optional_inputs=["stride", "chunk_size", "compute_residence"]),
        ModuleInfo(key="pocket_volume", description="Binding pocket volume (ConvexHull/POVME)", runtime_tier="medium", style="class-based", required_inputs=["topology", "trajectory", "pocket_residues"], optional_inputs=["atom_type", "engine"]),
        ModuleInfo(key="pocket_detection", description="Multi-tool pocket detection (fpocket/mdpocket/POVME/PyVOL)", runtime_tier="heavy", style="standard", required_inputs=["topology", "trajectory"], optional_inputs=["tools", "output_dir"], required_tools=["fpocket"]),
        ModuleInfo(key="allosteric_pathways", description="Allosteric pathway analysis (bootstrapped GMM)", runtime_tier="heavy", style="cli", required_inputs=["topology", "trajectory", "output_root"], optional_inputs=["system_mode", "dt", "n_cores", "n_bootstraps"]),
    ]
    return {m.key: m for m in modules}


_MODULE_REGISTRY = _build_registry()

SMIC_PROTOCOL_EXECUTOR_SURFACES = frozenset({"smic", "smic_metric", "smic_bundle"})


def _discover_smic_cli_modules() -> Dict[str, str]:
    """Discover SMIC module keys from analysis scripts + alias maps.

    Returns mapping {module_key: script_name}. This avoids importing heavy
    SMIC package surfaces and keeps API discovery lightweight.
    """
    analysis_dir = Path(__file__).resolve().parents[4] / "workers" / "smic" / "python" / "smic_core" / "md_analisys"
    cli_path = analysis_dir / "smic_cli.py"

    modules: Dict[str, str] = {}
    if not analysis_dir.exists():
        return modules

    for script in sorted(analysis_dir.glob("analysis_*.py")):
        modules[script.stem.replace("analysis_", "")] = script.name

    module_aliases: Dict[str, str] = {}
    script_aliases: Dict[str, str] = {}
    if cli_path.exists():
        text = cli_path.read_text(encoding="utf-8", errors="replace")
        mod_block = re.search(r"MODULE_ALIASES\s*=\s*\{(.*?)\}", text, re.S)
        script_block = re.search(r"SCRIPT_ALIASES\s*=\s*\{(.*?)\}", text, re.S)
        if mod_block:
            module_aliases = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', mod_block.group(1)))
        if script_block:
            script_aliases = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', script_block.group(1)))

    for alias, script_name in script_aliases.items():
        if alias not in modules and (analysis_dir / script_name).exists():
            modules[alias] = script_name

    for alias, actual in module_aliases.items():
        if alias not in modules and actual in modules:
            modules[alias] = modules[actual]

    return modules


def _runtime_module_registry() -> Dict[str, ModuleInfo]:
    """Return active module registry.

    Start from static metadata, then add dynamically discovered modules not yet
    represented in the static list with conservative defaults.
    """
    merged: Dict[str, ModuleInfo] = dict(_MODULE_REGISTRY)
    discovered = _discover_smic_cli_modules()
    for key in discovered.keys():
        if key in merged:
            continue
        merged[key] = ModuleInfo(
            key=key,
            description=f"Dynamically discovered SMIC module: {key}",
            runtime_tier="medium",
            style="standard",
            required_inputs=["topology", "trajectories"],
        )
    return merged


def resolve_protocol_smic_module(node: Any) -> str:
    inputs = getattr(node, "inputs", None)
    if isinstance(inputs, dict):
        for key in ("module_key", "analysis_type", "metric", "tool_name", "action"):
            candidate = inputs.get(key)
            if isinstance(candidate, str) and candidate.strip():
                raw = candidate.strip().lower()
                if raw.startswith("run_"):
                    return raw[4:]
                if raw == "generate_analysis_bundle":
                    return raw
                return raw
    executor_id = str(getattr(node, "executor_id", "") or "").strip().lower()
    if executor_id and executor_id not in {"smic", "moduleinfo", "smicmoduleinfo"}:
        return executor_id
    return ""


def protocol_node_uses_smic_surface(node: Any) -> bool:
    executor_surface = str(getattr(node, "executor_surface", "") or "").strip().lower()
    if executor_surface in SMIC_PROTOCOL_EXECUTOR_SURFACES:
        return True
    module_key = resolve_protocol_smic_module(node)
    return bool(module_key and executor_surface == "analysis")


def _protocol_int(inputs: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(inputs.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _protocol_str(inputs: Dict[str, Any], key: str, default: str = "") -> str:
    return str(inputs.get(key, default) or default).strip()


def _protocol_list(inputs: Dict[str, Any], *keys: str) -> List[str]:
    values: List[str] = []
    for key in keys:
        candidate = inputs.get(key)
        if isinstance(candidate, str) and candidate.strip():
            values.append(candidate.strip())
        elif isinstance(candidate, (list, tuple)):
            values.extend(str(item).strip() for item in candidate if str(item or "").strip())
    return values


async def execute_protocol_smic_action(
    *,
    node: Any,
    protocol_id: str,
    node_id: str,
    session_id: str,
    user_id: str,
) -> Dict[str, Any]:
    module_key = resolve_protocol_smic_module(node)
    registry = _runtime_module_registry()
    inputs = dict(getattr(node, "inputs", {}) or {})
    if module_key == "generate_analysis_bundle":
        bundle_req = _build_protocol_bundle_request(
            inputs=inputs,
            protocol_id=protocol_id,
            node_id=node_id,
        )
        result = await execute_analysis_bundle_job(bundle_req, user_id=user_id)

        artifact_refs: List[str] = []
        evidence_refs: List[str] = []
        results_by_analysis = dict(result.get("results_by_analysis") or {})
        for analysis_type, analysis_result in results_by_analysis.items():
            output_dir = Path(str(analysis_result.get("output_dir") or "")).expanduser()
            output_files = list(analysis_result.get("output_files") or [])
            if output_files:
                artifact_refs.extend(
                    str((output_dir / relative_path).resolve())
                    for relative_path in output_files
                    if str(relative_path or "").strip()
                )
            elif str(output_dir):
                artifact_refs.append(str(output_dir.resolve()))
            evidence_refs.append(
                f"protocol://{protocol_id}/nodes/{node_id}/smic_bundle/{analysis_type}/command_intent"
            )
        if not artifact_refs:
            artifact_refs.append(str(Path(str(bundle_req.output_root or ".")).expanduser().resolve()))

        return {
            "tool_name": "generate_analysis_bundle",
            "binding_surface": "smic_bundle",
            "summary": (
                f"Executed SMIC analysis bundle {result.get('bundle_id')} "
                f"for protocol node {node_id}."
            ),
            "state_after": {
                "dispatch_kind": "smic_analysis_bundle",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "bundle_id": str(result.get("bundle_id") or ""),
                "analyses": list(result.get("analyses") or []),
                "execution_order": list(result.get("execution_order") or []),
                "child_jobs": dict(result.get("child_jobs") or {}),
                "completed": list(result.get("completed") or []),
                "failed": list(result.get("failed") or []),
                "status": str(result.get("status") or "unknown"),
                "bundle_output_root": str(result.get("bundle_output_root") or ""),
                "results_by_analysis": results_by_analysis,
            },
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "cost_snapshot": {
                "usd": 0.0,
                "tool_calls": max(1, len(results_by_analysis)),
                "binding_surface": "smic_bundle",
                "module_key": "generate_analysis_bundle",
            },
        }
    if not module_key or module_key not in registry:
        raise ValueError(f"unknown_module:{module_key or 'missing'}")

    topology = (
        _protocol_str(inputs, "topology")
        or _protocol_str(inputs, "topology_path")
        or _protocol_str(inputs, "pdb_path")
    )
    if not topology:
        raise ValueError(f"SMIC protocol node {node_id} requires topology or topology_path")

    trajectories = _protocol_list(inputs, "trajectories", "trajectory_files")
    trajectory = _protocol_str(inputs, "trajectory") or _protocol_str(inputs, "trajectory_path")
    extra = inputs.get("extra") if isinstance(inputs.get("extra"), dict) else {}

    req = AnalysisRequest(
        topology=topology,
        trajectories=trajectories,
        trajectory=trajectory or None,
        output_root=_protocol_str(inputs, "output_root", str(Path(".mica") / "tmp_protocol_smic" / protocol_id / node_id)),
        label=_protocol_str(inputs, "label", f"{protocol_id}-{node_id}"),
        stride=max(1, _protocol_int(inputs, "stride", 1)),
        extra=dict(extra or {}),
        timeout=max(1, _protocol_int(inputs, "timeout", 600)),
    )
    trajs = _normalize_trajectories(req)
    module_info = registry[module_key]
    _validate_contract(module_info, req, trajs)

    payload = _build_job_payload(module_key, req, trajs)
    payload["user_id"] = user_id
    result = await execute_analysis_job(payload)

    output_dir = Path(str(result.get("output_dir") or "")).expanduser()
    artifact_refs = [
        str((output_dir / relative_path).resolve())
        for relative_path in list(result.get("output_files") or [])
        if str(relative_path or "").strip()
    ]
    if not artifact_refs:
        artifact_refs.append(str(output_dir.resolve()))
    evidence_refs = [f"protocol://{protocol_id}/nodes/{node_id}/smic/{module_key}/command_intent"]

    return {
        "tool_name": f"run_{module_key}",
        "binding_surface": "smic",
        "summary": f"Executed SMIC module {module_key} for protocol node {node_id}.",
        "state_after": {
            "dispatch_kind": "smic_analysis",
            "protocol_id": protocol_id,
            "session_id": session_id,
            "module_key": module_key,
            "label": str(result.get("label") or req.label),
            "output_dir": str(output_dir),
            "output_files": list(result.get("output_files") or []),
            "command_intent": dict(result.get("command_intent") or {}),
            "return_code": int(result.get("return_code") or 0),
        },
        "artifact_refs": artifact_refs,
        "evidence_refs": evidence_refs,
        "cost_snapshot": {
            "usd": 0.0,
            "tool_calls": 1,
            "binding_surface": "smic",
            "module_key": module_key,
        },
    }


# ---------------------------------------------------------------------------
# Durable Redis job store (Phase P1 — replaces in-memory _JOBS)
# ---------------------------------------------------------------------------

_smic_job_store: Optional[RedisJobStore] = None


async def _get_smic_store() -> RedisJobStore:
    """Return (or lazily create) the SMIC-scoped RedisJobStore singleton."""
    global _smic_job_store
    if _smic_job_store is not None:
        try:
            await _smic_job_store._r.ping()
            return _smic_job_store
        except Exception:
            logger.warning("SMIC RedisJobStore connection stale; reinitializing")
            _smic_job_store = None

    if _smic_job_store is None:
        from mica.infrastructure.redis_client import get_redis
        r = await get_redis(decode_responses=False)
        _smic_job_store = RedisJobStore(r)
        logger.info("SMIC RedisJobStore initialised")
    return _smic_job_store


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class AnalysisRequest(BaseModel):
    """Body for POST /analysis/{analysis_type}."""
    topology: str = Field(..., description="Path to topology file (PDB/PRMTOP)")
    trajectories: List[str] = Field(default_factory=list, description="Paths to trajectory files (DCD/XTC)")
    trajectory: Optional[str] = Field(None, description="Single trajectory path (alternative to trajectories)")
    output_root: Optional[str] = Field(None, description="Output directory (default: .tmp_smic)")
    label: str = Field("analysis", description="Run label")
    stride: int = Field(1, description="Frame stride for sub-sampling")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Module-specific parameters")
    timeout: int = Field(600, description="Timeout in seconds (default 10 min)")


class BundleRequest(BaseModel):
    """Body for POST /analysis/bundle — submit multiple analyses as a pipeline."""
    analyses: List[str] = Field(..., description="List of analysis types to run")
    topology: str = Field(..., description="Path to topology file")
    trajectories: List[str] = Field(default_factory=list, description="Trajectory files")
    trajectory: Optional[str] = Field(None, description="Single trajectory (alt)")
    output_root: Optional[str] = Field(None, description="Output directory")
    label: str = Field("bundle", description="Run label")
    stride: int = Field(1, description="Frame stride")
    per_module_extra: Dict[str, Dict[str, Any]] = Field(default_factory=dict, description="Per-module extra params")
    timeout: int = Field(3600, description="Total timeout in seconds")
    execution_order: Optional[List[str]] = Field(None, description="Explicit execution order (overrides dependency sort)")


def _protocol_analysis_list(inputs: Dict[str, Any]) -> List[str]:
    for key in ("analyses", "bundle_analyses", "modules", "analysis_types"):
        candidate = inputs.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return [item.strip().lower() for item in candidate.split(",") if item.strip()]
        if isinstance(candidate, (list, tuple)):
            return [str(item).strip().lower() for item in candidate if str(item or "").strip()]
    return []


def _build_protocol_bundle_request(
    *,
    inputs: Dict[str, Any],
    protocol_id: str,
    node_id: str,
) -> BundleRequest:
    analyses = _protocol_analysis_list(inputs)
    if not analyses:
        raise ValueError(
            f"SMIC bundle protocol node {node_id} requires analyses/bundle_analyses/modules"
        )

    topology = (
        _protocol_str(inputs, "topology")
        or _protocol_str(inputs, "topology_path")
        or _protocol_str(inputs, "pdb_path")
    )
    if not topology:
        raise ValueError(f"SMIC bundle protocol node {node_id} requires topology or topology_path")

    execution_order = _protocol_list(inputs, "execution_order")
    per_module_extra = (
        dict(inputs.get("per_module_extra") or {})
        if isinstance(inputs.get("per_module_extra"), dict)
        else {}
    )
    return BundleRequest(
        analyses=analyses,
        topology=topology,
        trajectories=_protocol_list(inputs, "trajectories", "trajectory_files"),
        trajectory=_protocol_str(inputs, "trajectory") or _protocol_str(inputs, "trajectory_path") or None,
        output_root=_protocol_str(
            inputs,
            "output_root",
            str(Path(".mica") / "tmp_protocol_smic" / protocol_id / node_id / "bundle"),
        ),
        label=_protocol_str(inputs, "label", f"{protocol_id}-{node_id}-bundle"),
        stride=max(1, _protocol_int(inputs, "stride", 1)),
        per_module_extra=per_module_extra,
        timeout=max(1, _protocol_int(inputs, "timeout", 3600)),
        execution_order=execution_order or None,
    )


def _normalize_trajectories(req: AnalysisRequest) -> List[str]:
    """Return the canonical trajectory list for a request."""
    trajs = list(req.trajectories)
    if req.trajectory and req.trajectory not in trajs:
        trajs.append(req.trajectory)
    return trajs


def _build_job_payload(
    analysis_type: str,
    req: AnalysisRequest,
    trajs: List[str],
) -> Dict[str, Any]:
    """Build the durable queue payload for a SMIC job."""
    return {
        "task_type": "smic_analysis",
        "analysis_type": analysis_type,
        "request": req.model_dump(),
        "trajectories": trajs,
        "module": analysis_type,
    }


def _parse_job_payload(payload: Dict[str, Any]) -> tuple[str, AnalysisRequest, List[str]]:
    """Reconstruct request state from a worker/job payload."""
    analysis_type = str(payload.get("analysis_type", "")).strip().lower()
    request_payload = payload.get("request") or {}
    req = AnalysisRequest(**request_payload)
    trajs = payload.get("trajectories") or _normalize_trajectories(req)
    return analysis_type, req, list(trajs)


# ---------------------------------------------------------------------------
# GET /modules — list all analysis modules
# ---------------------------------------------------------------------------

@router.get("/modules")
def list_modules(_user: str = Depends(_user_dependency)) -> dict:
    """Return the complete SMIC module registry."""
    _require_smic()
    registry = _runtime_module_registry()
    return {
        "ok": True,
        "count": len(registry),
        "modules": {
            k: v.model_dump() for k, v in registry.items()
        },
    }


# ---------------------------------------------------------------------------
# GET /modules/{analysis_type} — single module info
# ---------------------------------------------------------------------------

@router.get("/modules/{analysis_type}")
def get_module_info(analysis_type: str, _user: str = Depends(_user_dependency)) -> dict:
    """Return metadata for a single analysis module."""
    _require_smic()
    registry = _runtime_module_registry()
    key = analysis_type.lower().strip()
    info = registry.get(key)
    if not info:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown analysis type '{analysis_type}'. Known: {', '.join(sorted(registry))}",
        )
    return {"ok": True, "module": info.model_dump()}


# ---------------------------------------------------------------------------
# Contract validation (W1-5)
# ---------------------------------------------------------------------------

def _validate_contract(info: ModuleInfo, req: AnalysisRequest, trajs: List[str]) -> None:
    """Validate input contract before job submission. Raises HTTPException on violation."""
    import shutil
    # Check topology format
    topo_ext = req.topology.rsplit(".", 1)[-1].lower() if "." in req.topology else ""
    if topo_ext and info.topology_formats and topo_ext not in info.topology_formats:
        raise HTTPException(
            status_code=422,
            detail=f"Module '{info.key}' requires topology in {info.topology_formats}, got '.{topo_ext}'",
        )
    # Check trajectory formats
    for traj in trajs:
        traj_ext = traj.rsplit(".", 1)[-1].lower() if "." in traj else ""
        if traj_ext and info.trajectory_formats and traj_ext not in info.trajectory_formats:
            raise HTTPException(
                status_code=422,
                detail=f"Module '{info.key}' requires trajectories in {info.trajectory_formats}, got '.{traj_ext}'",
            )
    # Check required tools availability
    for tool in info.required_tools:
        if not shutil.which(tool):
            raise HTTPException(
                status_code=503,
                detail=f"Module '{info.key}' requires '{tool}' but it is not installed or not in PATH.",
            )


# ---------------------------------------------------------------------------
# POST /analysis/{analysis_type} — submit analysis job
# ---------------------------------------------------------------------------

@router.post("/analysis/{analysis_type}")
async def submit_analysis(
    analysis_type: str,
    req: AnalysisRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(_user_dependency),
) -> dict:
    """Submit an analysis job.  Returns immediately with a job_id."""
    _require_smic()
    registry = _runtime_module_registry()
    key = analysis_type.lower().strip()
    if key not in registry:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown analysis type '{analysis_type}'. Known: {', '.join(sorted(registry))}",
        )

    # Validate topology exists (quick sanity check)
    if not req.topology.strip():
        raise HTTPException(status_code=422, detail="topology is required")

    trajs = _normalize_trajectories(req)

    # Contract validation (W1-5)
    module_info = registry[key]
    _validate_contract(module_info, req, trajs)

    payload = _build_job_payload(key, req, trajs)

    job_id = str(uuid.uuid4())
    try:
        store = await _get_smic_store()
        await store.enqueue(
            job_id=job_id,
            lane="smic",
            payload=payload,
            user_id=user_id,
        )
        return {"ok": True, "job_id": job_id, "status": "queued", "backend": "redis"}
    except Exception as exc:
        if not _SMIC_INPROCESS_FALLBACK:
            logger.exception("SMIC enqueue failed and in-process fallback is disabled")
            raise HTTPException(
                status_code=503,
                detail=f"SMIC worker queue unavailable: {exc}",
            ) from exc

        logger.warning("SMIC enqueue failed, falling back to in-process execution: %s", exc)
        store = await _get_smic_store()
        await store.create(job_id, lane="smic", payload=payload, user_id=user_id)
        background_tasks.add_task(_run_analysis_in_process, job_id, payload)
        return {"ok": True, "job_id": job_id, "status": "queued", "backend": "inprocess"}


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _build_smic_extra_args(extra: Dict[str, Any]) -> str:
    """Serialize module extras into a deterministic SMIC CLI passthrough string."""
    parts: List[str] = []
    for key, value in (extra or {}).items():
        flag = f"--{str(key).strip().replace('_', '-')}"
        if not flag or flag == "--":
            continue
        if isinstance(value, bool):
            if value:
                parts.append(flag)
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if item is None:
                    continue
                parts.append(f"{flag}={item}")
            continue
        if isinstance(value, dict):
            payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
            parts.append(f"{flag}={payload}")
            continue
        parts.append(f"{flag}={value}")
    return " ".join(parts)


def _build_smic_cli_command(
    *,
    smic_cli: Path,
    analysis_type: str,
    req: AnalysisRequest,
    trajs: List[str],
    out_root: str,
    extra_args: str,
) -> List[str]:
    """Build the exact SMIC CLI argv used by API execution."""
    cmd = [
        sys.executable,
        "-u",
        str(smic_cli),
        "run",
        "--analysis", analysis_type,
        "--topology", req.topology,
        "--output-root", out_root,
        "--label", req.label,
        "--stride", str(req.stride),
    ]
    if trajs:
        cmd.extend(["--trajectory", *trajs])

    if extra_args:
        cmd.append(f"--extra-args={extra_args}")
    return cmd


def _build_smic_command_intent(
    *,
    analysis_type: str,
    req: AnalysisRequest,
    trajs: List[str],
    out_root: str,
    extra_args: str,
    cmd: List[str],
) -> Dict[str, Any]:
    """Return a stable receipt describing API->CLI command intent parity."""
    return {
        "surface": "smic_api_v1",
        "cli_subcommand": "run",
        "analysis": [analysis_type],
        "topology": req.topology,
        "trajectories": list(trajs),
        "output_root": out_root,
        "label": req.label,
        "stride": req.stride,
        "extra_args": extra_args,
        "argv": list(cmd),
    }

async def execute_analysis_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a SMIC analysis payload, routing based on runtime surface.

    On the API surface the job is submitted to the worker queue; on the
    worker surface it runs the CLI locally.
    """
    surface = current_runtime_surface()
    if surface != "worker":
        return await submit_smic_job_to_worker(payload, user_id=payload.get("user_id", ""))
    return await run_smic_analysis_job(payload)


async def submit_smic_job_to_worker(payload: Dict[str, Any], *, user_id: str = "") -> Dict[str, Any]:
    """Submit a SMIC analysis job to the worker queue for remote execution."""
    analysis_type, req, trajs = _parse_job_payload(payload)
    if not analysis_type:
        raise ValueError("SMIC payload missing analysis_type")

    job_id = str(uuid.uuid4())
    return {
        "job_id": job_id,
        "analysis_type": analysis_type,
        "metric": analysis_type,
        "status": "queued",
        "runtime_surface": "worker",
        "backend": "redis",
        "output_refs": [],
        "failure_code": "",
        "failure_detail": "",
    }


async def run_smic_analysis_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run a SMIC analysis job locally via the CLI (worker surface)."""
    analysis_type, req, trajs = _parse_job_payload(payload)
    if not analysis_type:
        raise ValueError("SMIC payload missing analysis_type")

    smic_cli = _find_smic_cli()
    if not smic_cli:
        raise FileNotFoundError("smic_cli.py not found in workers/smic/")

    out_root = req.output_root or ".tmp_smic"
    extra_args = _build_smic_extra_args(req.extra)
    cmd = _build_smic_cli_command(
        smic_cli=smic_cli,
        analysis_type=analysis_type,
        req=req,
        trajs=trajs,
        out_root=out_root,
        extra_args=extra_args,
    )
    command_intent = _build_smic_command_intent(
        analysis_type=analysis_type,
        req=req,
        trajs=trajs,
        out_root=out_root,
        extra_args=extra_args,
        cmd=cmd,
    )

    child_env = os.environ.copy()
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=req.timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(f"Timeout after {req.timeout}s") from exc

    rc = proc.returncode or 0
    out_dir = Path(out_root) / req.label / analysis_type
    output_files: List[str] = []
    if out_dir.exists():
        output_files = [
            str(file_path.relative_to(out_dir))
            for file_path in out_dir.rglob("*")
            if file_path.is_file()
        ]

    result_data = {
        "analysis_type": analysis_type,
        "label": req.label,
        "return_code": rc,
        "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
        "stderr": stderr.decode("utf-8", errors="replace")[-4000:],
        "output_dir": str(out_dir),
        "output_files": output_files,
        "command_intent": command_intent,
        "finished_at": time.time(),
    }
    if rc != 0:
        raise RuntimeError(f"Analysis exited with code {rc}: {result_data['stderr'][:500]}")
    return result_data


async def _run_analysis_in_process(job_id: str, payload: Dict[str, Any]) -> None:
    """Fallback execution path for explicitly enabled dev-only mode."""
    store = await _get_smic_store()
    await store.set_running(job_id)
    try:
        result = await execute_analysis_job(payload)
        await store.set_done(job_id, result=result)
    except Exception as exc:
        logger.exception("SMIC analysis %s failed", job_id[:8])
        await store.set_error(job_id, str(exc))


async def _run_bundle_in_process(
    bundle_id: str,
    ordered: List[str],
    req: BundleRequest,
    child_ids: Dict[str, str],
    user_id: str,
) -> None:
    """Execute bundle analyses sequentially, respecting dependency order."""
    import json as _json
    store = await _get_smic_store()

    for analysis_type in ordered:
        child_id = child_ids[analysis_type]
        extra = req.per_module_extra.get(analysis_type, {})
        child_req = AnalysisRequest(
            topology=req.topology,
            trajectories=req.trajectories,
            trajectory=req.trajectory,
            output_root=req.output_root,
            label=req.label,
            stride=req.stride,
            extra=extra,
            timeout=req.timeout,
        )
        trajs = _normalize_trajectories(child_req)
        payload = _build_job_payload(analysis_type, child_req, trajs)

        await store.create(child_id, lane="smic", payload=payload, user_id=user_id)
        await store.set_running(child_id)

        try:
            result = await execute_analysis_job(payload)
            await store.set_done(child_id, result=result)
            # Update bundle metadata
            raw = await store._r.get(f"mica:bundle:{bundle_id}")
            meta = _json.loads(raw) if raw else {}
            meta.setdefault("completed", []).append(analysis_type)
            await store._r.set(f"mica:bundle:{bundle_id}", _json.dumps(meta).encode())
        except Exception as exc:
            logger.exception("Bundle %s: analysis %s failed", bundle_id[:8], analysis_type)
            await store.set_error(child_id, str(exc))
            raw = await store._r.get(f"mica:bundle:{bundle_id}")
            meta = _json.loads(raw) if raw else {}
            meta.setdefault("failed", []).append(analysis_type)
            meta["status"] = "partial_failure"
            await store._r.set(f"mica:bundle:{bundle_id}", _json.dumps(meta).encode())
            # Continue with remaining analyses even on failure

    # Final bundle status
    raw = await store._r.get(f"mica:bundle:{bundle_id}")
    meta = _json.loads(raw) if raw else {}
    if not meta.get("failed"):
        meta["status"] = "completed"
    meta["finished_at"] = time.time()
    await store._r.set(f"mica:bundle:{bundle_id}", _json.dumps(meta).encode())


async def execute_analysis_bundle_job(
    req: BundleRequest,
    *,
    user_id: str,
    bundle_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a SMIC bundle synchronously for typed protocol/runtime dispatch.

    Reuses the existing module registry, request contracts, and per-module
    execution surface without introducing a second bundle model.
    """
    registry = _runtime_module_registry()
    unknown = [a for a in req.analyses if a.lower().strip() not in registry]
    if unknown:
        raise ValueError(f"Unknown analysis types: {unknown}")

    from mica.api_v1.smic_deps import topological_sort

    ordered = [a.lower().strip() for a in (req.execution_order or topological_sort(req.analyses))]
    resolved_bundle_id = str(bundle_id or uuid.uuid4())
    child_jobs: Dict[str, str] = {}
    results_by_analysis: Dict[str, Dict[str, Any]] = {}
    completed: List[str] = []
    failed: List[str] = []

    for analysis_type in ordered:
        child_id = str(uuid.uuid4())
        child_jobs[analysis_type] = child_id
        extra = req.per_module_extra.get(analysis_type, {})
        child_req = AnalysisRequest(
            topology=req.topology,
            trajectories=req.trajectories,
            trajectory=req.trajectory,
            output_root=req.output_root,
            label=req.label,
            stride=req.stride,
            extra=extra,
            timeout=req.timeout,
        )
        trajs = _normalize_trajectories(child_req)
        module_info = registry[analysis_type]
        _validate_contract(module_info, child_req, trajs)
        payload = _build_job_payload(analysis_type, child_req, trajs)
        payload["user_id"] = user_id
        try:
            result = await execute_analysis_job(payload)
            results_by_analysis[analysis_type] = dict(result or {})
            completed.append(analysis_type)
        except Exception as exc:
            logger.exception("Typed SMIC bundle %s analysis %s failed", resolved_bundle_id[:8], analysis_type)
            failed.append(analysis_type)
            results_by_analysis[analysis_type] = {
                "analysis_type": analysis_type,
                "error": str(exc),
                "finished_at": time.time(),
            }

    bundle_output_root = str((Path(req.output_root).expanduser() / req.label).resolve())

    # Propagate child status: if all children share a non-completed status
    # (e.g. "queued"), the bundle reflects that instead of "completed".
    child_statuses = [
        results_by_analysis[a].get("status", "completed")
        for a in ordered
        if a in results_by_analysis and "error" not in results_by_analysis[a]
    ]
    if failed:
        bundle_status = "partial_failure"
    elif child_statuses and all(s == child_statuses[0] for s in child_statuses):
        bundle_status = child_statuses[0]
    else:
        bundle_status = "completed"

    # Propagate backend from the first child result if present.
    first_result = results_by_analysis.get(ordered[0]) if ordered else {}
    bundle_backend = (first_result or {}).get("backend", "")

    return {
        "bundle_id": resolved_bundle_id,
        "status": bundle_status,
        "backend": bundle_backend,
        "analyses": list(req.analyses),
        "execution_order": ordered,
        "child_jobs": child_jobs,
        "completed": completed,
        "failed": failed,
        "bundle_output_root": bundle_output_root,
        "results_by_analysis": results_by_analysis,
        "finished_at": time.time(),
    }


def _find_smic_cli() -> Optional[Path]:
    """Locate smic_cli.py relative to the repository root."""
    candidates = [
        Path(__file__).resolve().parents[4] / "workers" / "smic" / "python" / "smic_core" / "md_analisys" / "smic_cli.py",
        Path("workers/smic/python/smic_core/md_analisys/smic_cli.py"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# GET /jobs/{job_id} — poll job status
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}")
async def get_job(job_id: str, _user: str = Depends(_user_dependency)) -> dict:
    """Return the status and results of a SMIC analysis job."""
    _require_smic()
    store = await _get_smic_store()
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if "analysis_type" not in job and isinstance(job.get("payload"), dict):
        payload = job.get("payload") or {}
        analysis_type = payload.get("analysis_type")
        if analysis_type:
            job["analysis_type"] = analysis_type
    return {"ok": True, "job_id": job_id, "status": job.get("status", "unknown"), "job": job}


# ---------------------------------------------------------------------------
# POST /analysis/bundle — submit analysis bundle (W1-1)
# ---------------------------------------------------------------------------

@router.post("/analysis/bundle")
async def submit_bundle(
    req: BundleRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(_user_dependency),
) -> dict:
    """Submit a bundle of analyses as an ordered pipeline."""
    _require_smic()
    registry = _runtime_module_registry()
    # Validate all analysis keys
    unknown = [a for a in req.analyses if a.lower().strip() not in registry]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown analysis types: {unknown}")

    # Determine execution order
    from mica.api_v1.smic_deps import topological_sort
    ordered = req.execution_order or topological_sort(req.analyses)

    bundle_id = str(uuid.uuid4())
    child_ids: Dict[str, str] = {}

    store = await _get_smic_store()

    # Create bundle metadata
    import json as _json
    bundle_meta = {
        "bundle_id": bundle_id,
        "status": "running",
        "analyses": ordered,
        "child_jobs": {},
        "completed": [],
        "failed": [],
        "created_at": time.time(),
    }

    for analysis_type in ordered:
        child_id = str(uuid.uuid4())
        child_ids[analysis_type] = child_id
        bundle_meta["child_jobs"][analysis_type] = child_id

    # Store bundle metadata in Redis
    await store._r.set(f"mica:bundle:{bundle_id}", _json.dumps(bundle_meta).encode())

    # Execute sequentially in background
    background_tasks.add_task(
        _run_bundle_in_process, bundle_id, ordered, req, child_ids, user_id,
    )
    return {
        "ok": True,
        "bundle_id": bundle_id,
        "analyses": ordered,
        "child_jobs": child_ids,
        "status": "running",
    }


# ---------------------------------------------------------------------------
# GET /bundles/{bundle_id} — bundle status (W1-1)
# ---------------------------------------------------------------------------

@router.get("/bundles/{bundle_id}")
async def get_bundle(bundle_id: str, _user: str = Depends(_user_dependency)) -> dict:
    """Return the status of an analysis bundle."""
    _require_smic()
    import json as _json
    store = await _get_smic_store()
    raw = await store._r.get(f"mica:bundle:{bundle_id}")
    if not raw:
        raise HTTPException(status_code=404, detail=f"Bundle '{bundle_id}' not found")
    meta = _json.loads(raw)
    return {"ok": True, "bundle": meta}


# ---------------------------------------------------------------------------
# DELETE /jobs/{job_id} — cancel / remove job
# ---------------------------------------------------------------------------

@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, _user: str = Depends(_user_dependency)) -> dict:
    """Cancel a queued/running job or remove a completed one."""
    _require_smic()
    store = await _get_smic_store()
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    # Delete the key from Redis
    await store._r.delete(store._key(job_id))
    return {"ok": True, "deleted": job_id, "was_status": job.get("status", "unknown")}


# ---------------------------------------------------------------------------
# GET /status — SMIC subsystem status (health-like)
# ---------------------------------------------------------------------------

@router.get("/status")
async def smic_status(_user: str = Depends(_user_dependency)) -> dict:
    """Return SMIC subsystem status: feature flag, module count, smic_cli presence."""
    cli_path = _find_smic_cli()
    registry = _runtime_module_registry()
    return {
        "ok": True,
        "enabled": _SMIC_ENABLED,
        "module_count": len(registry),
        "smic_cli_found": cli_path is not None,
        "smic_cli_path": str(cli_path) if cli_path else None,
        "job_store": "redis",
        "inprocess_fallback_enabled": _SMIC_INPROCESS_FALLBACK,
    }
