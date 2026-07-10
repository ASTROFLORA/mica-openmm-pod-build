from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict

from mica.drivers.md_execution_contract import REQUEST_SCHEMA_VERSION


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_payload(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _as_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return deepcopy(payload)
    return {}


def _resolve_gpu_type(gpu_name: str):
    from mica.infrastructure.providers.base_provider import GPUType

    normalized = str(gpu_name or "").strip().upper().replace(" ", "_")
    if not normalized:
        return GPUType.RTX_5080
    return getattr(GPUType, normalized, GPUType.RTX_5080)


def merge_biostate_execution_request(
    cfg: Any,
    provider_name: str,
    *,
    default_template_id: str,
    default_checkpoint_policy: str,
    default_storage_backend: str,
) -> Dict[str, Any]:
    request = _as_dict(getattr(cfg, "_biostate_execution_request", {}) or {})
    request["schema_version"] = REQUEST_SCHEMA_VERSION

    job = _as_dict(request.get("job") or {})
    template_ref = _as_dict(job.get("template_ref") or {})
    if not template_ref.get("template_id"):
        template_ref = {
            "template_id": default_template_id,
            "template_version": "1.0.0",
        }
    job.update(
        {
            "job_id": str(getattr(cfg, "job_id", "") or job.get("job_id", "")),
            "workflow": str(job.get("workflow") or "protein_ligand_md"),
            "execution_target": "remote",
            "execution_class": str(
                getattr(cfg, "execution_class", job.get("execution_class", "research"))
                or "research"
            ),
            "template_ref": template_ref,
        }
    )
    request["job"] = job

    runtime = _as_dict(request.get("runtime") or {})
    runtime["provider_preference"] = str(provider_name or runtime.get("provider_preference", ""))
    if getattr(cfg, "gpu_type", None) is not None:
        runtime["gpu_type"] = str(
            getattr(getattr(cfg, "gpu_type", None), "value", getattr(cfg, "gpu_type", ""))
            or runtime.get("gpu_type", "")
        )
    elif getattr(cfg, "gpu_type_str", None) is not None:
        runtime["gpu_type"] = str(getattr(cfg, "gpu_type_str", "") or runtime.get("gpu_type", ""))
    if getattr(cfg, "max_total_cost_usd", None) is not None:
        runtime["max_total_cost_usd"] = float(getattr(cfg, "max_total_cost_usd", 0.0) or 0.0)
    if getattr(cfg, "max_price_per_hour", None) is not None:
        runtime["max_price_per_hour"] = float(getattr(cfg, "max_price_per_hour", 0.0) or 0.0)
    if getattr(cfg, "estimated_cost_usd", None) is not None:
        runtime["estimated_cost_usd"] = float(getattr(cfg, "estimated_cost_usd", 0.0) or 0.0)
    request["runtime"] = runtime

    durability = _as_dict(request.get("durability") or {})
    durability.setdefault("checkpoint_policy", default_checkpoint_policy)
    durability["preserve_on_failure"] = bool(
        getattr(cfg, "preserve_instance_on_failure", durability.get("preserve_on_failure", True))
    )
    durability["storage_backend"] = str(
        getattr(cfg, "storage_backend", durability.get("storage_backend", default_storage_backend))
        or default_storage_backend
    )
    request["durability"] = durability

    handoff = _as_dict(getattr(cfg, "_biostate_handoff", {}) or {})
    if handoff:
        request["engine_handoff"] = handoff

    return request


@dataclass
class BioStateEngineJob:
    protein_pdb: str
    ligand_smiles: str
    docked_ligand_pdb: str
    simulation_mode: str
    execution_request: Dict[str, Any]
    user_id: str = ""
    preferred_provider: str = "vast"
    execution_backend: str = "vast"
    context: Dict[str, Any] = field(default_factory=dict)
    handoff: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_execution_context(
        cls,
        *,
        context: Dict[str, Any],
        execution_request: Dict[str, Any],
        protein_pdb: str,
        ligand_smiles: str,
        docked_ligand_pdb: str,
        simulation_mode: str,
    ) -> "BioStateEngineJob":
        if not protein_pdb:
            raise ValueError("BioState engine remote handoff requires protein_pdb")

        requested_mode = str(simulation_mode or context.get("simulation_mode") or "binding").strip().lower()
        if requested_mode == "complex_stability" and (not ligand_smiles or not docked_ligand_pdb):
            raise ValueError(
                "BioState engine remote complex_stability handoff requires ligand_smiles and docked_ligand_pdb"
            )

        preferred_provider = str(
            context.get("execution_backend")
            or context.get("preferred_provider")
            or ("salad" if str(context.get("execution_backend") or "").strip().lower() == "salad" else "vast")
        ).strip().lower() or "vast"

        scientific = _as_dict(execution_request.get("scientific") or {})
        compiled_plan = _as_dict(context.get("compiled_biostate_plan") or {})
        compile_receipts = sorted(str(key) for key, value in compiled_plan.items() if isinstance(value, dict))
        manifest_source = {
            "execution_request": execution_request,
            "compiled_biostate_plan": compiled_plan,
            "protein_pdb": protein_pdb,
            "ligand_smiles": ligand_smiles,
            "docked_ligand_pdb": docked_ligand_pdb,
            "simulation_mode": requested_mode,
        }
        handoff = {
            "schema_version": "biostate_engine_handoff_v1",
            "manifest_hash": _sha256_payload(manifest_source),
            "execution_request_hash": _sha256_payload(execution_request),
            "compile_packet": {
                "compiled_plan_present": bool(compiled_plan),
                "receipt_keys": compile_receipts,
            },
            "artifact_policy": {
                "execution_target": "remote",
                "simulation_mode": requested_mode,
                "required_artifact_policy": str(
                    scientific.get("artifact_policy")
                    or context.get("artifact_policy")
                    or "provider_canonical"
                ),
            },
            "restart_policy": {
                "resume_spec_path": str(context.get("resume_spec_path", "") or ""),
                "preserve_instance_on_failure": bool(context.get("preserve_instance_on_failure", True)),
                "storage_backend": str(context.get("storage_backend", "none") or "none"),
                "storage_remote": str(context.get("storage_remote", "") or ""),
                "storage_remote_prefix": str(context.get("storage_remote_prefix", "md-jobs") or "md-jobs"),
                "storage_sync_interval_sec": int(context.get("storage_sync_interval_sec", 900) or 900),
            },
            "provider_constraints": {
                "preferred_provider": preferred_provider,
                "execution_backend": str(context.get("execution_backend", "") or "").strip().lower(),
                "gpu_type": str(context.get("gpu_type") or context.get("salad_gpu_type") or ""),
            },
            "budget_envelope": {
                "max_total_cost_usd": float(context.get("max_total_cost_usd", 10.0) or 0.0),
                "max_price_per_hour": float(context.get("max_price_per_hour", 0.5) or 0.0),
                "max_runtime_hours": float(context.get("max_runtime_hours", 48.0) or 0.0),
            },
        }

        return cls(
            protein_pdb=str(protein_pdb),
            ligand_smiles=str(ligand_smiles),
            docked_ligand_pdb=str(docked_ligand_pdb),
            simulation_mode=requested_mode,
            execution_request=_as_dict(execution_request),
            user_id=str(context.get("user_id") or ""),
            preferred_provider=preferred_provider,
            execution_backend=str(context.get("execution_backend") or "").strip().lower() or preferred_provider,
            context=dict(context or {}),
            handoff=handoff,
        )

    @property
    def job_id(self) -> str:
        return str(self.execution_request.get("job", {}).get("job_id", "") or "")

    def with_route_decision_id(self, route_decision_id: str) -> "BioStateEngineJob":
        cloned = BioStateEngineJob(
            protein_pdb=self.protein_pdb,
            ligand_smiles=self.ligand_smiles,
            docked_ligand_pdb=self.docked_ligand_pdb,
            simulation_mode=self.simulation_mode,
            execution_request=_as_dict(self.execution_request),
            user_id=self.user_id,
            preferred_provider=self.preferred_provider,
            execution_backend=self.execution_backend,
            context=dict(self.context),
            handoff=_as_dict(self.handoff),
        )
        cloned.execution_request.setdefault("runtime", {})
        cloned.execution_request["runtime"]["route_decision_id"] = route_decision_id
        cloned.handoff["route_decision_id"] = route_decision_id
        return cloned

    def to_vast_md_config(self):
        from mica.infrastructure.orchestration.vast_md_orchestrator import MDJobConfig

        requested_mode = "complex" if self.simulation_mode == "complex_stability" else self.simulation_mode
        storage_backend = str(self.context.get("storage_backend", "none") or "none").lower()
        cfg = MDJobConfig(
            pdb_path=self.protein_pdb,
            simulation_mode=requested_mode,
            ligand_smiles=self.ligand_smiles,
            docked_ligand_pdb=self.docked_ligand_pdb,
            steps=int(self.context.get("steps", 75_000_000) or 75_000_000),
            production_ns=float(self.context.get("production_ns", 100.0) or 100.0),
            n_replicas=int(self.context.get("n_replicas", 1) or 1),
            gpu_type=_resolve_gpu_type(str(self.context.get("gpu_type") or "")),
            max_price_per_hour=float(self.context.get("max_price_per_hour", 0.50) or 0.50),
            max_total_cost_usd=float(self.context.get("max_total_cost_usd", 10.0) or 10.0),
            max_runtime_hours=float(self.context.get("max_runtime_hours", 48.0) or 48.0),
            monitor_interval_sec=int(self.context.get("monitor_interval_sec", 300) or 300),
            preserve_instance_on_failure=bool(self.context.get("preserve_instance_on_failure", True)),
            resume_spec_path=str(self.context.get("resume_spec_path", "") or ""),
            storage_backend=storage_backend,
            storage_remote=str(self.context.get("storage_remote", "") or ""),
            storage_remote_prefix=str(self.context.get("storage_remote_prefix", "md-jobs") or "md-jobs"),
            storage_sync_interval_sec=int(self.context.get("storage_sync_interval_sec", 900) or 900),
            storage_env=dict(self.context.get("storage_env", {}) or {}),
            min_reliability=float(self.context.get("min_reliability", 0.97) or 0.97),
            required_disk_gb=float(self.context.get("required_disk_gb", 0.0) or 0.0),
            provision_timeout_sec=int(self.context.get("provision_timeout_sec", 900) or 900),
            ssh_probe_attempts=int(self.context.get("ssh_probe_attempts", 24) or 24),
            ssh_probe_sleep_sec=int(self.context.get("ssh_probe_sleep_sec", 10) or 10),
            execution_class=str(
                self.execution_request.get("job", {}).get("execution_class", "research") or "research"
            ),
            job_id=self.job_id,
        )
        setattr(cfg, "_biostate_execution_request", _as_dict(self.execution_request))
        setattr(cfg, "_biostate_handoff", _as_dict(self.handoff))
        return cfg
