from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from mica.infrastructure.orchestration.vast_md_orchestrator import MDArtifactManifest, ResumeSpec


TERMINAL_RECOVERY_STATUSES = {"completed", "failed", "failed_recoverable", "error", "interrupted", "lost", "cancelled"}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_resume_spec(path: str) -> Optional[ResumeSpec]:
    candidate = str(path or "").strip()
    if not candidate or not Path(candidate).is_file():
        return None
    return ResumeSpec.from_dict(json.loads(Path(candidate).read_text(encoding="utf-8")))


def _load_manifest(path: str) -> Optional[MDArtifactManifest]:
    candidate = str(path or "").strip()
    if not candidate or not Path(candidate).is_file():
        return None
    return MDArtifactManifest.from_dict(json.loads(Path(candidate).read_text(encoding="utf-8")))


def build_remote_md_result(session: Dict[str, Any], *, result_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    existing = dict(result_override or session.get("result") or {})
    if existing:
        if not existing.get("status"):
            existing["status"] = str(session.get("status") or "failed")
        if session.get("instance_id") and not existing.get("instance_id"):
            existing["instance_id"] = session.get("instance_id")
        if session.get("ssh_host") and not existing.get("ssh_host"):
            existing["ssh_host"] = session.get("ssh_host")
        if session.get("ssh_port") and not existing.get("ssh_port"):
            existing["ssh_port"] = session.get("ssh_port")
        if session.get("results_json") and not existing.get("results_json"):
            existing["results_json"] = session.get("results_json")
        return existing

    manifest = _load_manifest(str(session.get("artifact_manifest_path") or ""))
    resume_spec = _load_resume_spec(str(session.get("resume_spec_path") or ""))
    status = str(session.get("status") or "failed")
    result_status = "completed" if status == "completed" else "failed"
    result: Dict[str, Any] = {
        "workflow": "protein_ligand_md",
        "execution_mode": "remote_vast",
        "status": result_status,
        "success": result_status == "completed",
        "vast_phase_final": str(session.get("vast_phase") or session.get("phase") or "unknown"),
        "instance_id": session.get("instance_id"),
        "ssh_host": session.get("ssh_host"),
        "ssh_port": session.get("ssh_port"),
        "output_dir": session.get("output_dir") or session.get("local_output_dir"),
        "resume_spec_path": session.get("resume_spec_path"),
        "artifact_manifest_path": session.get("artifact_manifest_path"),
        "results_json": session.get("results_json") or session.get("last_orchestrator_state"),
        "recovery_classification": status,
    }
    if status != "completed":
        result["error"] = str(session.get("error") or status)
    if resume_spec is not None:
        result["simulation_mode"] = resume_spec.simulation_mode
        result["job_id"] = resume_spec.job_id
        result["run_dir"] = resume_spec.run_dir
        result["selected_forcefield"] = resume_spec.selected_forcefield
        result["target_steps"] = resume_spec.target_steps
        result["target_production_ns"] = resume_spec.target_production_ns
        result["storage_backend"] = resume_spec.storage_backend
        result["storage_remote_root"] = resume_spec.storage_remote_root
    if manifest is not None:
        result.setdefault("simulation_mode", manifest.simulation_mode)
        result.setdefault("job_id", manifest.job_id)
        result.setdefault("selected_forcefield", manifest.selected_forcefield)
        result.setdefault("output_dir", manifest.local_output_dir or result.get("output_dir"))
        result["tracked_artifacts"] = [artifact.to_dict() for artifact in manifest.artifacts]
    return result


def build_remote_md_output_payload(
    session: Dict[str, Any],
    *,
    output_json: str,
    result_override: Optional[Dict[str, Any]] = None,
    mode: str = "biodynamo_md_reconciled",
) -> Dict[str, Any]:
    context = dict(session.get("context") or {})
    result = build_remote_md_result(session, result_override=result_override)
    finished_at = str(session.get("finished_at") or _utcnow())
    return {
        "entrypoint": "tools/mica_agent.py",
        "mode": mode,
        "provider_id": session.get("provider_id"),
        "model_id": session.get("model_id"),
        "user_id": session.get("user_id"),
        "started_at": str(session.get("started_at") or finished_at),
        "finished_at": finished_at,
        "query": str(session.get("query") or "Remote MD reconciliation"),
        "context": context,
        "result": result,
        "output_json": output_json,
    }


def materialize_remote_md_output_json(
    session: Dict[str, Any],
    *,
    output_json: str = "",
    result_override: Optional[Dict[str, Any]] = None,
    mode: str = "biodynamo_md_reconciled",
) -> str:
    candidate = str(output_json or session.get("output_json") or "").strip()
    if not candidate:
        session_id = str(session.get("session_id") or "remote_md")
        candidate = str((Path.cwd() / f"mica_biodynamo_md_reconciled_{session_id}.json").resolve())
    target = Path(candidate).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_remote_md_output_payload(
        session,
        output_json=str(target),
        result_override=result_override,
        mode=mode,
    )
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(target)
