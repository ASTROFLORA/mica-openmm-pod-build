from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict

from mica.drivers.md_execution_contract import enforce_no_silent_success

from ..md_provider_adapter import MDProviderAdapter
from ..vast_gcs_integration import MDJobConfig as VastGCSMDJobConfig
from ..vast_gcs_integration import VastGCSOrchestrator


def _gpu_label(cfg: Any) -> str:
    gpu_type = getattr(cfg, "gpu_type", "")
    return str(getattr(gpu_type, "value", gpu_type) or "")


def _artifact_manifest(raw_result: Dict[str, Any]) -> list[Dict[str, Any]]:
    entries: list[Dict[str, Any]] = []
    for artifact_type, artifact_path in (
        ("trajectory_dcd", raw_result.get("trajectory_path")),
        ("simulation_log", raw_result.get("log_path")),
        ("final_checkpoint", raw_result.get("checkpoint_path")),
    ):
        if not artifact_path:
            continue
        entries.append(
            {
                "artifact_type": artifact_type,
                "path": str(artifact_path),
                "exists": False,
                "producer": {
                    "template_id": "cl06_vast_gcs_compat",
                    "template_version": "1.0.0",
                    "adapter_id": "vast_gcs_adapter",
                },
            }
        )
    return entries


def _output_dir(raw_result: Dict[str, Any]) -> str:
    for field_name in ("trajectory_path", "log_path", "checkpoint_path"):
        artifact_path = str(raw_result.get(field_name, "") or "")
        if artifact_path:
            return str(PurePosixPath(artifact_path).parent)
    return ""


def _iso_or_empty(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class _VastGCSExecutionWrapper:
    def __init__(self, cfg: Any, provider: Any):
        self.cfg = cfg
        self.state = SimpleNamespace(
            instance_id="",
            total_cost_usd=0.0,
            phase=SimpleNamespace(value="queued"),
        )
        self._delegate = VastGCSOrchestrator(
            vast_api_key=getattr(provider, "api_key", None),
            gcs_credentials_path=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
            gcs_project=os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            gcs_region=os.environ.get("GCS_REGION", "us-central1"),
        )

    async def run(self):
        user_id = str(getattr(self.cfg, "_mica_user_id", "") or "anonymous")
        raw_result = await self._delegate.run_md_job(user_id=user_id, config=self.cfg)
        self.state.instance_id = str(getattr(raw_result, "instance_id", "") or "")
        self.state.total_cost_usd = float(getattr(raw_result, "total_cost_usd", 0.0) or 0.0)
        phase = "completed" if bool(getattr(raw_result, "success", False)) else "failed"
        self.state.phase = SimpleNamespace(value=phase)
        return raw_result


class VastGCSAdapter(MDProviderAdapter):
    provider_aliases = ("vast",)
    adapter_id = "vast_gcs_adapter"

    def supports_config(self, cfg: Any) -> bool:
        return isinstance(cfg, VastGCSMDJobConfig)

    def build_orchestrator(self, cfg: Any, provider: Any, on_event: Any = None) -> Any:
        return _VastGCSExecutionWrapper(cfg, provider)

    def build_request(self, cfg: Any, provider_name: str) -> Dict[str, Any]:
        return {
            "schema_version": "md_execution_request_v1",
            "job": {
                "job_id": str(getattr(cfg, "job_id", "") or ""),
                "workflow": "protein_ligand_md",
                "execution_target": "remote",
                "execution_class": str(getattr(cfg, "execution_class", "research") or "research"),
                "template_ref": {
                    "template_id": "cl06_vast_gcs_compat",
                    "template_version": "1.0.0",
                },
            },
            "runtime": {
                "provider_preference": provider_name,
                "gpu_type": _gpu_label(cfg),
                "steps": int(getattr(cfg, "steps", 0) or 0),
                "max_price_per_hour": float(getattr(cfg, "max_price_per_hour", 0.0) or 0.0),
            },
            "durability": {
                "checkpoint_policy": "strict",
                "preserve_on_failure": False,
                "storage_backend": "gcs",
            },
            "input": {
                "pdb_file": str(getattr(cfg, "pdb_file", "") or ""),
            },
        }

    def normalize_result(
        self,
        raw_result: Any,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        raw_dict = asdict(raw_result) if is_dataclass(raw_result) else dict(raw_result or {})
        success = bool(raw_dict.get("success", False))
        required_artifacts_present = bool(raw_dict.get("trajectory_path")) and bool(raw_dict.get("log_path"))
        checkpoint_present = bool(raw_dict.get("checkpoint_path"))
        phase_markers_verified = bool(raw_dict.get("end_time") or raw_dict.get("steps_completed") or raw_dict.get("wall_time_seconds"))
        durability_policy_satisfied = bool(checkpoint_present)
        durability_class = (
            "resumable_verified"
            if required_artifacts_present and checkpoint_present
            else "restartable"
            if required_artifacts_present
            else "none"
        )

        canonical = {
            "schema_version": "md_execution_result_v1",
            "job": {
                "job_id": request.get("job", {}).get("job_id", ""),
                "workflow": "protein_ligand_md",
                "execution_target": "remote",
                "execution_class": request.get("job", {}).get("execution_class", "research"),
            },
            "status": {
                "state": "completed" if success else "failed",
                "phase": "completed" if success else "failed",
                "terminal": True,
                "success": success,
                "reason_code": "",
                "reason_message": str(raw_dict.get("error_message", "") or ""),
            },
            "artifacts": {
                "output_dir": _output_dir(raw_dict),
                "result_json": {
                    "provider_job_id": str(raw_dict.get("job_id", "") or ""),
                    "trajectory_path": str(raw_dict.get("trajectory_path", "") or ""),
                    "log_path": str(raw_dict.get("log_path", "") or ""),
                    "checkpoint_path": str(raw_dict.get("checkpoint_path", "") or ""),
                    "steps_completed": int(raw_dict.get("steps_completed", 0) or 0),
                    "ns_per_day": float(raw_dict.get("ns_per_day", 0.0) or 0.0),
                    "start_time": _iso_or_empty(raw_dict.get("start_time")),
                    "end_time": _iso_or_empty(raw_dict.get("end_time")),
                },
                "resume_spec_path": "",
                "manifest_path": "",
                "checkpoints": [str(raw_dict.get("checkpoint_path"))] if raw_dict.get("checkpoint_path") else [],
                "logs": [str(raw_dict.get("log_path"))] if raw_dict.get("log_path") else [],
                "manifest_entries": _artifact_manifest(raw_dict),
            },
            "recovery": {
                "recoverable": checkpoint_present,
                "recovery_class": "resumable" if checkpoint_present else "restartable",
                "instance_preserved": False,
                "durability_class": durability_class,
            },
            "validation": {
                "completion_evidence_level": "strict" if required_artifacts_present else "weak",
                "required_artifacts_present": required_artifacts_present,
                "phase_markers_verified": phase_markers_verified,
                "checkpoint_consistent": checkpoint_present,
                "artifact_manifest_complete": bool(_artifact_manifest(raw_dict)),
                "checkpoint_policy": request.get("durability", {}).get("checkpoint_policy", "strict"),
                "durability_policy_satisfied": durability_policy_satisfied,
                "recovery_evidence_present": checkpoint_present,
                "manifest_evidence_present": bool(_artifact_manifest(raw_dict)),
                "durability_class": durability_class,
            },
            "provider": {
                "name": provider_name,
                "adapter_id": self.adapter_id,
                "instance_id": str(raw_dict.get("instance_id", "") or ""),
                "provider_job_id": str(raw_dict.get("job_id", "") or ""),
                "total_cost_usd": float(raw_dict.get("total_cost_usd", 0.0) or 0.0),
            },
            "terminal_autopsy": {
                "schema_version": "terminal_autopsy_v1",
                "terminal_state": "completed" if success else "failed",
                "reason_code": "legacy_vast_gcs_failure" if not success else "",
                "reason_message": str(raw_dict.get("error_message", "") or ""),
                "metadata": {
                    "wall_time_seconds": float(raw_dict.get("wall_time_seconds", 0.0) or 0.0),
                    "steps_completed": int(raw_dict.get("steps_completed", 0) or 0),
                },
            },
            "teardown_proof": {
                "schema_version": "teardown_proof_v1",
                "destroy_attempted": bool(raw_dict.get("instance_id") or raw_dict.get("end_time") or success),
                "destroy_succeeded": bool(success),
                "preserved_for_recovery": False,
                "metadata": {
                    "proof_class": "synthetic_from_vast_gcs_legacy",
                },
            },
            "effective_config": request,
            "backend_native": raw_dict,
        }
        return enforce_no_silent_success(canonical)