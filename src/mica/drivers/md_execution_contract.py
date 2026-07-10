from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import sys
from datetime import UTC, datetime
from typing import Any, Dict

REQUEST_SCHEMA_VERSION = "md_execution_request_v1"
RESULT_SCHEMA_VERSION = "md_execution_result_v1"

ADVANCED_TECHNIQUE_PATH = (
    "workers/dynamo/biodynamo/processors/run_binding_simulation_spontaneous.py"
)
QUALITY_STANDARD_PATH = (
    "workers/dynamo/biodynamo/processors/runcomplex_paper_dodecaedrica.py"
)

DEFAULT_TEMPLATE_ID = "complex_stability_v1"
DEFAULT_TEMPLATE_VERSION = "1.0.0"


def _load_scientific_protocol_module():
    module_name = "_biodynamo_scientific_protocol_contract_helper"
    module = sys.modules.get(module_name)
    if module is not None:
        return module

    module_path = Path(__file__).with_name("biodynamo_scientific_protocol.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load scientific protocol helper from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_scientific_protocol_metadata(
    context: Dict[str, Any],
    *,
    simulation_mode: str,
) -> Dict[str, Any]:
    module = _load_scientific_protocol_module()
    return module.build_scientific_protocol_metadata(
        context,
        simulation_mode=simulation_mode,
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_status_state(raw_status: str, success: bool) -> str:
    status = (raw_status or "").strip().lower()
    if status == "failed_recoverable":
        return "failed_recoverable"
    if status in {"error", "failed", "failure"}:
        return "failed"
    if status in {"completed", "complete", "success"} and success:
        return "completed"
    if status in {"running", "in_progress"}:
        return "running"
    return "unknown"


def _backend_native_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = dict(payload or {})
    snapshot.pop("execution_result_v1", None)
    snapshot.pop("backend_native", None)
    return deepcopy(snapshot)


def _extract_task_graph_receipt(md_result: Dict[str, Any]) -> Dict[str, Any]:
    task_graph = md_result.get("task_graph")
    if isinstance(task_graph, dict):
        return deepcopy(task_graph)
    results_json = md_result.get("results_json") or {}
    task_graph = results_json.get("task_graph")
    if isinstance(task_graph, dict):
        return deepcopy(task_graph)
    return {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_producer_metadata(
    request: Dict[str, Any],
    md_result: Dict[str, Any],
) -> Dict[str, str]:
    template_ref = request.get("job", {}).get("template_ref", {})
    return {
        "template_id": str(template_ref.get("template_id", DEFAULT_TEMPLATE_ID) or DEFAULT_TEMPLATE_ID),
        "template_version": str(
            template_ref.get("template_version", DEFAULT_TEMPLATE_VERSION) or DEFAULT_TEMPLATE_VERSION
        ),
        "adapter_id": str(md_result.get("adapter_id", "") or "unknown_adapter"),
        "execution_mode": str(md_result.get("execution_mode", "") or "unknown"),
    }


def _build_manifest_entry(
    *,
    artifact_type: str,
    artifact_path: str,
    producer: Dict[str, str],
) -> Dict[str, Any]:
    path_str = str(artifact_path or "")
    path = Path(path_str)
    exists = path.is_file()

    checksum_sha256 = _sha256_file(path) if exists else ""
    size_bytes = path.stat().st_size if exists else 0

    return {
        "artifact_type": artifact_type,
        "path": path_str,
        "exists": exists,
        "size_bytes": int(size_bytes),
        "checksum_sha256": checksum_sha256,
        "producer": dict(producer),
    }


def _collect_local_manifest_entries(
    md_result: Dict[str, Any],
    request: Dict[str, Any],
) -> list[Dict[str, Any]]:
    results_json = md_result.get("results_json") or {}
    phases = results_json.get("phases") or {}
    production = phases.get("production") or {}
    producer = _build_producer_metadata(request, md_result)
    progress_csv = production.get("energy_csv") or production.get("state_csv")

    candidates = [
        ("trajectory_dcd", production.get("dcd")),
        ("energy_csv", progress_csv),
        ("final_checkpoint", production.get("final_checkpoint") or production.get("checkpoint")),
        ("final_state_xml", production.get("state_xml") or production.get("final_state")),
    ]

    entries: list[Dict[str, Any]] = []
    for artifact_type, artifact_path in candidates:
        if artifact_path:
            entries.append(
                _build_manifest_entry(
                    artifact_type=artifact_type,
                    artifact_path=str(artifact_path),
                    producer=producer,
                )
            )
    return entries


def _collect_remote_manifest_entries(
    md_result: Dict[str, Any],
    request: Dict[str, Any],
) -> list[Dict[str, Any]]:
    state_json = md_result.get("results_json") or {}
    producer = _build_producer_metadata(request, md_result)

    candidates = [
        ("resume_spec", state_json.get("latest_resume_spec_path")),
        ("job_manifest", state_json.get("latest_job_manifest_path")),
    ]

    entries: list[Dict[str, Any]] = []
    for artifact_type, artifact_path in candidates:
        if artifact_path:
            entries.append(
                _build_manifest_entry(
                    artifact_type=artifact_type,
                    artifact_path=str(artifact_path),
                    producer=producer,
                )
            )
    return entries


def _looks_like_remote_scientific_artifact(path_str: str) -> bool:
    path = Path(str(path_str or ""))
    name = path.name.lower()
    suffix = path.suffix.lower()

    if suffix in {".dcd", ".xtc", ".trr", ".chk", ".cpt"}:
        return True
    if suffix == ".csv":
        return any(marker in name for marker in ("state", "energy", "progress"))
    if suffix == ".xml":
        return any(marker in name for marker in ("state", "checkpoint"))
    return False


def _collect_remote_scientific_manifest_entries(
    md_result: Dict[str, Any],
    request: Dict[str, Any],
) -> list[Dict[str, Any]]:
    state_json = md_result.get("results_json") or {}
    producer = _build_producer_metadata(request, md_result)
    entries: list[Dict[str, Any]] = []
    seen_paths: set[str] = set()

    def _maybe_add(path_value: Any) -> None:
        path_str = str(path_value or "")
        if not path_str or path_str in seen_paths or not _looks_like_remote_scientific_artifact(path_str):
            return
        seen_paths.add(path_str)
        entries.append(
            _build_manifest_entry(
                artifact_type="scientific_output",
                artifact_path=path_str,
                producer=producer,
            )
        )

    direct_candidates = [
        state_json.get("trajectory_path"),
        state_json.get("trajectory_dcd"),
        state_json.get("dcd"),
        state_json.get("checkpoint_path"),
        state_json.get("final_checkpoint"),
        state_json.get("state_csv"),
        state_json.get("energy_csv"),
        state_json.get("state_xml"),
        state_json.get("final_state_xml"),
    ]
    artifacts = state_json.get("artifacts") or {}
    if isinstance(artifacts, dict):
        direct_candidates.extend(
            [
                artifacts.get("trajectory_dcd"),
                artifacts.get("state_csv"),
                artifacts.get("energy_csv"),
                artifacts.get("final_checkpoint"),
                artifacts.get("state_xml"),
            ]
        )

    for candidate in direct_candidates:
        _maybe_add(candidate)

    output_dir_str = str(md_result.get("output_dir") or "").strip()
    output_dir = Path(output_dir_str) if output_dir_str else None
    if output_dir is not None and output_dir.is_dir():
        for pattern in (
            "*.dcd",
            "*.xtc",
            "*.trr",
            "*.chk",
            "*.cpt",
            "*state*.csv",
            "*energy*.csv",
            "*progress*.csv",
            "*state*.xml",
            "*checkpoint*.xml",
        ):
            for artifact_path in output_dir.rglob(pattern):
                _maybe_add(str(artifact_path))

    return entries


def _extract_local_segment_evidence(results_json: Dict[str, Any]) -> bool:
    phases = results_json.get("phases") or {}
    production = phases.get("production") or {}

    for key in ("segment_count", "production_segments", "checkpoint_segments", "batch_segments"):
        value = production.get(key)
        if isinstance(value, int) and value > 0:
            return True

    checkpoints = production.get("checkpoints")
    if isinstance(checkpoints, list) and len(checkpoints) > 0:
        return True

    checkpoint_events = production.get("checkpoint_events")
    if isinstance(checkpoint_events, list) and len(checkpoint_events) > 0:
        return True

    return False


def _extract_execution_loop_receipt(results_json: Dict[str, Any]) -> Dict[str, Any]:
    payload = results_json.get("execution_loop")
    if isinstance(payload, dict):
        return deepcopy(payload)
    return {}


def _evaluate_local_phase_markers(results_json: Dict[str, Any]) -> Dict[str, Any]:
    execution_loop = _extract_execution_loop_receipt(results_json)
    phase_markers = execution_loop.get("phase_markers")
    if not isinstance(phase_markers, list) or not phase_markers:
        phases = results_json.get("phases") or {}
        return {
            "phase_markers_verified": bool(phases),
            "phase_marker_authority": "legacy_phases_fallback",
            "phase_marker_summary": {},
        }

    statuses: Dict[str, str] = {}
    for marker in phase_markers:
        if not isinstance(marker, dict):
            continue
        phase = str(marker.get("phase") or "").strip().lower()
        status = str(marker.get("status") or "").strip().lower()
        if phase:
            statuses[phase] = status

    required_phases = ("minimization", "equilibration", "production")
    verified = all(
        statuses.get(phase) in {"completed", "skipped"} for phase in required_phases
    ) and statuses.get("production") == "completed"

    return {
        "phase_markers_verified": verified,
        "phase_marker_authority": "execution_loop_v1",
        "phase_marker_summary": dict(statuses),
    }


def _build_local_segment_evidence(results_json: Dict[str, Any]) -> list[Dict[str, Any]]:
    phases = results_json.get("phases") or {}
    production = phases.get("production") or {}
    segment_evidence: list[Dict[str, Any]] = []

    checkpoint_events = production.get("checkpoint_events")
    if isinstance(checkpoint_events, list):
        for index, event in enumerate(checkpoint_events, start=1):
            if not isinstance(event, dict):
                continue
            artifact_path = str(
                event.get("path")
                or production.get("checkpoint")
                or production.get("final_checkpoint")
                or ""
            )
            artifact_uris = [artifact_path] if artifact_path else []
            artifact_sha256 = ""
            if artifact_path:
                artifact_file = Path(artifact_path)
                if artifact_file.is_file():
                    artifact_sha256 = _sha256_file(artifact_file)
            segment_evidence.append(
                {
                    "segment_id": str(event.get("segment_id") or f"checkpoint-{index}"),
                    "start_step": event.get("start_step"),
                    "end_step": event.get("end_step", event.get("interval_steps")),
                    "artifact_uris": artifact_uris,
                    "artifact_sha256": artifact_sha256,
                    "terminal_state": str(event.get("terminal_state") or event.get("kind") or "checkpointed"),
                }
            )

    if segment_evidence:
        return segment_evidence

    segment_count = production.get("segment_count")
    if isinstance(segment_count, int) and segment_count > 0:
        artifact_path = str(production.get("checkpoint") or production.get("final_checkpoint") or "")
        artifact_uris = [artifact_path] if artifact_path else []
        artifact_sha256 = ""
        if artifact_path:
            artifact_file = Path(artifact_path)
            if artifact_file.is_file():
                artifact_sha256 = _sha256_file(artifact_file)
        return [
            {
                "segment_id": "aggregate",
                "start_step": 0,
                "end_step": None,
                "artifact_uris": artifact_uris,
                "artifact_sha256": artifact_sha256,
                "terminal_state": "aggregated",
                "segment_count": segment_count,
            }
        ]

    return []


def _aggregate_segment_evidence_hash(segment_evidence: list[Dict[str, Any]]) -> str:
    if not segment_evidence:
        return ""
    encoded = json.dumps(segment_evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evaluate_local_durability(
    request: Dict[str, Any],
    results_json: Dict[str, Any],
    manifest_entries: list[Dict[str, Any]],
    required_artifacts_present: bool,
) -> Dict[str, Any]:
    policy = str(request.get("durability", {}).get("checkpoint_policy", "strict") or "strict").lower()
    require_segment_evidence = bool(
        request.get("durability", {}).get("require_segment_evidence", False)
    )
    has_checkpoint = any(
        entry.get("artifact_type") == "final_checkpoint" and bool(entry.get("exists"))
        for entry in manifest_entries
    )
    has_segment_evidence = _extract_local_segment_evidence(results_json)

    if policy == "disabled":
        satisfied = True
    elif policy == "best_effort":
        satisfied = bool(required_artifacts_present or has_checkpoint)
    else:  # strict
        satisfied = bool(required_artifacts_present and has_checkpoint)
        if require_segment_evidence:
            satisfied = bool(satisfied and has_segment_evidence)

    return {
        "checkpoint_policy": policy,
        "require_segment_evidence": require_segment_evidence,
        "checkpoint_evidence_present": has_checkpoint,
        "segment_evidence_present": has_segment_evidence,
        "durability_policy_satisfied": satisfied,
    }


def _evaluate_remote_durability(
    request: Dict[str, Any],
    *,
    has_resume: bool,
    has_manifest: bool,
) -> Dict[str, Any]:
    policy = str(request.get("durability", {}).get("checkpoint_policy", "strict") or "strict").lower()
    if policy == "disabled":
        satisfied = True
    elif policy == "best_effort":
        satisfied = bool(has_resume or has_manifest)
    else:  # strict
        satisfied = bool(has_resume and has_manifest)

    return {
        "checkpoint_policy": policy,
        "checkpoint_evidence_present": bool(has_resume),
        "recovery_evidence_present": bool(has_resume),
        "manifest_evidence_present": bool(has_manifest),
        "durability_policy_satisfied": satisfied,
    }


def _local_durability_class(
    *,
    required_artifacts_present: bool,
    checkpoint_evidence_present: bool,
    manifest_complete: bool,
    durability_policy_satisfied: bool,
) -> str:
    if not checkpoint_evidence_present and not required_artifacts_present:
        return "none"
    if checkpoint_evidence_present and manifest_complete and durability_policy_satisfied:
        return "resumable_verified"
    if checkpoint_evidence_present:
        return "resumable"
    return "restartable"


def _remote_durability_class(
    *,
    has_resume: bool,
    has_manifest: bool,
    manifest_complete: bool,
    durability_policy_satisfied: bool,
    required_artifacts_present: bool,
) -> str:
    if has_resume and has_manifest and manifest_complete and durability_policy_satisfied:
        return "resumable_verified"
    if has_resume:
        return "resumable"
    if required_artifacts_present:
        return "restartable"
    return "none"


def build_execution_request_v1(
    context: Dict[str, Any],
    *,
    protein_pdb: str,
    ligand_smiles: str,
    docked_ligand_pdb: str,
    execution_target: str,
    simulation_mode: str,
) -> Dict[str, Any]:
    template_id = str(context.get("md_template_id", DEFAULT_TEMPLATE_ID) or DEFAULT_TEMPLATE_ID)
    template_version = str(
        context.get("md_template_version", DEFAULT_TEMPLATE_VERSION) or DEFAULT_TEMPLATE_VERSION
    )
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "job": {
            "job_id": context.get("job_id") or f"md_{_utc_now_iso()}",
            "workflow": "protein_ligand_md",
            "execution_target": execution_target,
            "execution_class": context.get("execution_class", "production"),
            "template_ref": {
                "template_id": template_id,
                "template_version": template_version,
            },
            "request_ts": _utc_now_iso(),
        },
        "scientific": {
            "simulation_mode": simulation_mode,
            "protein_pdb": protein_pdb,
            "ligand_smiles": ligand_smiles,
            "docked_ligand_pdb": docked_ligand_pdb,
            "production_ns": float(context.get("production_ns", 50.0)),
            "steps": int(context.get("steps", 75_000_000)),
            "n_replicas": int(context.get("n_replicas", 1)),
            "forcefield": str(context.get("forcefield", "amber14sb")),
            "ligand_ff": str(context.get("ligand_ff", "gaff-2.11")),
        },
        "runtime": {
            "gpu_default": "RTX_5080",
            "max_price_per_hour": float(context.get("max_price_per_hour", 0.50)),
            "max_total_cost_usd": float(context.get("max_total_cost_usd", 10.0)),
            "max_runtime_hours": float(context.get("max_runtime_hours", 48.0)),
            "monitor_interval_sec": int(context.get("monitor_interval_sec", 300)),
        },
        "durability": {
            "checkpoint_policy": str(context.get("checkpoint_policy", "strict")),
            "require_segment_evidence": bool(context.get("require_segment_evidence", False)),
            "preserve_on_failure": bool(context.get("preserve_instance_on_failure", False)),
            "resume_spec_path": str(context.get("resume_spec_path", "") or ""),
            "storage_backend": str(context.get("storage_backend", "none") or "none"),
        },
        "quality_profile": {
            "advanced_technique": ADVANCED_TECHNIQUE_PATH,
            "engine_quality_standard": QUALITY_STANDARD_PATH,
        },
        "metadata": {
            "scientific_protocol": _build_scientific_protocol_metadata(
                context,
                simulation_mode=simulation_mode,
            ),
            "biodynamo": {
                "template_id": template_id,
                "template_version": template_version,
            },
        },
    }


def _build_publication_results_json(publication_result: Dict[str, Any]) -> Dict[str, Any]:
    phases = deepcopy(publication_result.get("phases") or {})
    production = phases.setdefault("production", {})
    files = deepcopy(publication_result.get("files") or {})

    file_to_phase_key = {
        "trajectory_dcd": "dcd",
        "energy_csv": "energy_csv",
        "final_checkpoint": "checkpoint",
        "final_state_xml": "state_xml",
    }
    for file_key, phase_key in file_to_phase_key.items():
        if not production.get(phase_key) and files.get(file_key):
            production[phase_key] = files[file_key]

    return {
        "schema_version": "publication_md_raw_result_v1",
        "run_name": publication_result.get("run_name", ""),
        "phases": phases,
        "files": files,
        "state_bundle": deepcopy(publication_result.get("state_bundle") or {}),
        "execution_contract": deepcopy(publication_result.get("execution_contract") or {}),
    }


def adapt_publication_result_to_execution_contract(
    publication_result: Dict[str, Any],
    request: Dict[str, Any],
) -> Dict[str, Any]:
    raw_status = str(
        publication_result.get("status")
        or ("completed" if publication_result.get("success") else "failed")
    )
    local_result = {
        "workflow": "protein_ligand_md",
        "execution_mode": "local_publication_recipe",
        "adapter_id": "publication_md_engine_adapter",
        "status": raw_status,
        "success": bool(publication_result.get("success", False)),
        "output_dir": publication_result.get("output_dir", ""),
        "results_json": _build_publication_results_json(publication_result),
    }
    normalized = normalize_local_execution_result(local_result, request)
    return enforce_no_silent_success(normalized)


def normalize_local_execution_result(
    md_result: Dict[str, Any],
    request: Dict[str, Any],
) -> Dict[str, Any]:
    raw_status = str(md_result.get("status", "unknown"))
    success = bool(md_result.get("success", False))
    state = _normalize_status_state(raw_status, success)

    results_json = md_result.get("results_json") or {}
    phases = results_json.get("phases") or {}
    production = phases.get("production") or {}
    manifest_entries = _collect_local_manifest_entries(md_result, request)
    required_artifact_types = {"trajectory_dcd", "energy_csv"}
    required_entries = [
        entry
        for entry in manifest_entries
        if str(entry.get("artifact_type") or "") in required_artifact_types
    ]
    required_artifacts_present = bool(required_entries) and all(
        bool(entry.get("exists")) and int(entry.get("size_bytes", 0) or 0) > 0
        for entry in required_entries
    )

    completion_evidence_level = (
        "strict" if required_artifacts_present else "weak"
    )
    manifest_complete = bool(manifest_entries) and all(
        bool(entry.get("exists"))
        and int(entry.get("size_bytes", 0) or 0) > 0
        and bool(entry.get("checksum_sha256"))
        for entry in manifest_entries
    )
    segment_evidence = _build_local_segment_evidence(results_json)
    durability = _evaluate_local_durability(
        request,
        results_json,
        manifest_entries,
        required_artifacts_present,
    )
    durability_class = _local_durability_class(
        required_artifacts_present=required_artifacts_present,
        checkpoint_evidence_present=bool(durability["checkpoint_evidence_present"]),
        manifest_complete=manifest_complete,
        durability_policy_satisfied=bool(durability["durability_policy_satisfied"]),
    )
    task_graph = _extract_task_graph_receipt(md_result)
    phase_marker_eval = _evaluate_local_phase_markers(results_json)

    normalized = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job": {
            "job_id": request.get("job", {}).get("job_id", ""),
            "workflow": "protein_ligand_md",
            "execution_target": "local",
            "execution_class": request.get("job", {}).get("execution_class", "production"),
        },
        "status": {
            "state": state,
            "phase": "production" if state == "completed" else "failed",
            "terminal": state in {"completed", "failed"},
            "success": success,
            "reason_code": "",
            "reason_message": "",
        },
        "progress": {
            "current_ns": float(production.get("duration_ns", request.get("scientific", {}).get("production_ns", 0.0))),
            "target_ns": float(request.get("scientific", {}).get("production_ns", 0.0)),
            "speed_ns_day": float(production.get("ns_per_day", 0.0) or 0.0),
        },
        "artifacts": {
            "output_dir": md_result.get("output_dir", ""),
            "result_json": results_json,
            "checkpoints": [],
            "logs": [],
            "manifest_entries": manifest_entries,
        },
        "validation": {
            "completion_evidence_level": completion_evidence_level,
            "required_artifacts_present": required_artifacts_present,
            "phase_markers_verified": phase_marker_eval["phase_markers_verified"],
            "phase_marker_authority": phase_marker_eval["phase_marker_authority"],
            "phase_marker_summary": phase_marker_eval["phase_marker_summary"],
            "checkpoint_consistent": bool(durability["checkpoint_evidence_present"]),
            "artifact_manifest_complete": manifest_complete,
            "checkpoint_policy": durability["checkpoint_policy"],
            "require_segment_evidence": durability["require_segment_evidence"],
            "segment_evidence_present": durability["segment_evidence_present"],
            "segment_evidence": segment_evidence,
            "segment_evidence_aggregate_sha256": _aggregate_segment_evidence_hash(segment_evidence),
            "durability_policy_satisfied": durability["durability_policy_satisfied"],
            "durability_class": durability_class,
        },
        "effective_config": request,
        "backend_native": _backend_native_snapshot(md_result),
    }
    if task_graph:
        normalized["task_graph"] = task_graph
    return normalized


def normalize_remote_execution_result(
    md_result: Dict[str, Any],
    request: Dict[str, Any],
) -> Dict[str, Any]:
    raw_status = str(md_result.get("status", "unknown"))
    success = bool(md_result.get("success", False))
    state = _normalize_status_state(raw_status, success)

    state_json = md_result.get("results_json") or {}
    has_resume = bool(state_json.get("latest_resume_spec_path"))
    has_manifest = bool(state_json.get("latest_job_manifest_path"))
    scientific_manifest_entries = _collect_remote_scientific_manifest_entries(md_result, request)
    required_artifacts_present = any(bool(entry.get("exists")) for entry in scientific_manifest_entries)

    completion_evidence_level = "strict" if required_artifacts_present else "weak"
    manifest_entries = _collect_remote_manifest_entries(md_result, request)
    manifest_complete = bool(manifest_entries) and all(
        bool(entry.get("path")) and bool(entry.get("producer", {}).get("template_id"))
        for entry in manifest_entries
    )
    durability = _evaluate_remote_durability(request, has_resume=has_resume, has_manifest=has_manifest)
    durability_class = _remote_durability_class(
        has_resume=has_resume,
        has_manifest=has_manifest,
        manifest_complete=manifest_complete,
        durability_policy_satisfied=bool(durability["durability_policy_satisfied"]),
        required_artifacts_present=required_artifacts_present,
    )
    task_graph = _extract_task_graph_receipt(md_result)

    normalized = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job": {
            "job_id": request.get("job", {}).get("job_id", ""),
            "workflow": "protein_ligand_md",
            "execution_target": "remote",
            "execution_class": request.get("job", {}).get("execution_class", "production"),
        },
        "status": {
            "state": state,
            "phase": str(md_result.get("vast_phase_final", "unknown")).lower(),
            "terminal": state in {"completed", "failed", "failed_recoverable"},
            "success": success,
            "reason_code": "",
            "reason_message": "",
        },
        "artifacts": {
            "output_dir": md_result.get("output_dir", ""),
            "result_json": state_json,
            "resume_spec_path": state_json.get("latest_resume_spec_path", ""),
            "manifest_path": state_json.get("latest_job_manifest_path", ""),
            "checkpoints": [],
            "logs": [],
            "manifest_entries": manifest_entries,
        },
        "recovery": {
            "recoverable": bool(has_resume),
            "recovery_class": "resumable" if has_resume else "restartable",
            "instance_preserved": bool(request.get("durability", {}).get("preserve_on_failure", True)),
            "durability_class": durability_class,
        },
        "validation": {
            "completion_evidence_level": completion_evidence_level,
            "required_artifacts_present": required_artifacts_present,
            "phase_markers_verified": bool(md_result.get("vast_phase_final")),
            "checkpoint_consistent": bool(has_resume),
            "artifact_manifest_complete": manifest_complete,
            "checkpoint_policy": durability["checkpoint_policy"],
            "durability_policy_satisfied": durability["durability_policy_satisfied"],
            "recovery_evidence_present": durability["recovery_evidence_present"],
            "manifest_evidence_present": durability["manifest_evidence_present"],
            "durability_class": durability_class,
        },
        "effective_config": request,
        "backend_native": _backend_native_snapshot(md_result),
    }
    if task_graph:
        normalized["task_graph"] = task_graph
    return normalized


def enforce_no_silent_success(result: Dict[str, Any]) -> Dict[str, Any]:
    status = result.get("status") or {}
    validation = result.get("validation") or {}

    success = bool(status.get("success", False))
    artifacts_ok = bool(validation.get("required_artifacts_present", False))
    markers_ok = bool(validation.get("phase_markers_verified", False))
    durability_ok = bool(validation.get("durability_policy_satisfied", True))

    if success and (not artifacts_ok or not markers_ok):
        status["state"] = "failed"
        status["success"] = False
        status["terminal"] = True
        status["reason_code"] = "evidence_missing"
        status["reason_message"] = (
            "Terminal success blocked by no-silent-success policy: missing required evidence artifacts or phase markers."
        )
        result["status"] = status
    elif success and not durability_ok:
        status["state"] = "failed"
        status["success"] = False
        status["terminal"] = True
        status["reason_code"] = "durability_evidence_missing"
        status["reason_message"] = (
            "Terminal success blocked by durability policy: checkpoint/recovery evidence does not satisfy requested policy."
        )
        result["status"] = status

    return result


# ---------------------------------------------------------------------------
# Salad SRCG execution normalizer (SP-22)
# ---------------------------------------------------------------------------

def normalize_salad_execution_result(
    salad_result: Dict[str, Any],
    request: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize a SaladGCSOrchestrator result into the canonical md_execution_result_v1 envelope.

    *salad_result* must have at minimum:
      - ``status``: "completed" | "failed" | "stopped" | "timeout"
      - ``job_id``: str
      - ``cg_name``: str
      - ``output_gcs_prefix``: str  (gs:// URI)
      - ``elapsed_seconds``: float
      - ``error``: str | None
      - ``artifact_manifest``: dict (built by _build_salad_artifact_manifest in biodynamo_driver)
    """
    status_str = str(salad_result.get("status", "unknown"))
    success = status_str == "completed"
    state = _normalize_status_state(status_str, success)

    artifacts = dict(salad_result.get("artifact_manifest") or {})
    worker_failure = dict(salad_result.get("worker_failure_receipt") or {})
    completed_marker = bool(artifacts.get("completed_marker_confirmed"))
    dcd_chunks = int(artifacts.get("dcd_chunk_count", 0))
    history_present = bool(artifacts.get("history_json_present"))
    worker_history_present = bool(artifacts.get("worker_history_json_present"))
    failure_receipt_present = bool(artifacts.get("failure_receipt_present")) or bool(worker_failure)
    failure_traceback_present = bool(artifacts.get("failure_traceback_present"))

    required_artifacts_present = completed_marker and dcd_chunks > 0
    phase_markers_verified = completed_marker
    durability_policy_satisfied = completed_marker and history_present
    reason_code = ""
    reason_message = str(salad_result.get("error") or "")

    if failure_receipt_present:
        state = "failed"
        success = False
        reason_code = "worker_runtime_error"
        reason_message = str(
            worker_failure.get("error_message")
            or reason_message
            or "Salad worker emitted a runtime failure receipt"
        )

    durability_class = (
        "resumable_verified" if durability_policy_satisfied
        else "restartable" if required_artifacts_present
        else "none"
    )

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job": {
            "job_id": str(salad_result.get("job_id", "")),
            "workflow": "protein_ligand_md",
            "execution_target": "remote",
            "execution_class": request.get("job", {}).get("execution_class", "production"),
        },
        "status": {
            "state": state,
            "phase": status_str,
            "terminal": state in {"completed", "failed"},
            "success": success,
            "reason_code": reason_code,
            "reason_message": reason_message,
        },
        "artifacts": {
            "output_gcs_prefix": str(salad_result.get("output_gcs_prefix", "")),
            "cg_name": str(salad_result.get("cg_name", "")),
            "dcd_chunk_count": dcd_chunks,
            "completed_marker_confirmed": completed_marker,
            "history_json_present": history_present,
            "worker_history_json_present": worker_history_present,
            "failure_receipt_present": failure_receipt_present,
            "failure_traceback_present": failure_traceback_present,
            "failure_error_message": str(worker_failure.get("error_message") or ""),
            "manifest_entries": list(artifacts.get("object_listing", [])),
        },
        "recovery": {
            "recoverable": False,  # Salad SRCG is serverless — no resume spec
            "recovery_class": "restartable",
            "instance_preserved": False,
            "durability_class": durability_class,
        },
        "validation": {
            "completion_evidence_level": "strict" if required_artifacts_present else "weak",
            "required_artifacts_present": required_artifacts_present,
            "phase_markers_verified": phase_markers_verified,
            "checkpoint_consistent": False,  # serverless lane — no checkpoints
            "artifact_manifest_complete": completed_marker,
            "checkpoint_policy": "none",
            "durability_policy_satisfied": durability_policy_satisfied,
            "recovery_evidence_present": False,
            "manifest_evidence_present": completed_marker,
            "durability_class": durability_class,
        },
        "provider": {
            "name": "salad",
            "cg_name": str(salad_result.get("cg_name", "")),
            "output_gcs_prefix": str(salad_result.get("output_gcs_prefix", "")),
            "elapsed_seconds": float(salad_result.get("elapsed_seconds", 0.0)),
        },
        "effective_config": request,
        "backend_native": _backend_native_snapshot(salad_result),
    }
