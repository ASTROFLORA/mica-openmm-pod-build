from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from mica.drivers.md_execution_contract import (
    enforce_no_silent_success,
    normalize_salad_execution_result,
)

from ...compute_image_contract import canonical_md_worker_image
from ..md_execution_protocol import RESULT_SCHEMA_VERSION
from ..md_provider_adapter import MDProviderAdapter
from ..salad_gcs_orchestrator import SaladGCSOrchestrator
from ..salad_submit_contract import SaladMDSubmitRequest, prepare_salad_md_submission


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src").exists() and (parent / "workers").exists():
            return parent
    return Path.cwd()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _artifact_uri(ref: Dict[str, Any]) -> str:
    return str((ref or {}).get("uri") or "").strip()


def _has_textual_martini_runtime(canonical_image_text: str, canonical_image_requirements: str) -> bool:
    return "martini_openmm" in canonical_image_text or "martini_openmm" in canonical_image_requirements


def _configured_digest_pinned_canonical_image_ref() -> str:
    image_ref = canonical_md_worker_image().strip()
    if image_ref.startswith("ghcr.io/") and "@sha256:" in image_ref:
        return image_ref
    return ""


@dataclass
class MartiniCGJobConfig:
    job_id: Optional[str] = None
    route_decision_id: Optional[str] = None
    preferred_provider: str = ""
    execution_class: str = "research"
    compute_mode: str = "coarse_grain"
    forcefield_family: str = "martini3"
    system_representation: str = "coarse_grained"
    source_target_id: str = ""
    input_structure_ref: str = ""
    output_gcs_prefix: str = ""
    gpu_type_str: str = "L40S"
    estimated_cost_usd: float = 0.0
    max_total_cost_usd: float = 0.0
    compiled_plan: Dict[str, Any] = field(default_factory=dict)
    compiled_context: Dict[str, Any] = field(default_factory=dict)
    runtime_bundle: Dict[str, Any] = field(default_factory=dict)
    coordinate_ref: Dict[str, Any] = field(default_factory=dict)
    topology_ref: Dict[str, Any] = field(default_factory=dict)
    preprocessed_topology_ref: Dict[str, Any] = field(default_factory=dict)
    artifact_refs: List[str] = field(default_factory=list)
    artifact_ref_records: List[Dict[str, Any]] = field(default_factory=list)


class MartiniCGAdapter(MDProviderAdapter):
    provider_aliases = ("salad", "vast")
    adapter_id = "martini_cg_provider_adapter_v1"

    def supports_config(self, cfg: Any) -> bool:
        return isinstance(cfg, MartiniCGJobConfig)

    def build_orchestrator(self, cfg: Any, provider: Any, on_event: Any = None) -> Any:
        provider_name = str(getattr(provider, "PROVIDER_NAME", "") or "").strip().lower()
        if provider_name != "salad":
            raise RuntimeError(f"Martini CG provider execution currently supports salad only, got {provider_name or 'unknown'}")
        return _MartiniCGSaladExecution(
            cfg=cfg,
            provider=provider,
            submit_request=self._build_submit_request(cfg),
            on_event=on_event,
        )

    def build_request(self, cfg: MartiniCGJobConfig, provider_name: str) -> Dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "job": {
                "job_id": str(cfg.job_id or ""),
                "workflow": "cg_martini_smoke",
                "execution_target": "remote",
                "execution_class": str(cfg.execution_class or "research"),
            },
            "scientific": {
                "compute_mode": str(cfg.compute_mode or "coarse_grain"),
                "forcefield_family": str(cfg.forcefield_family or "martini3"),
                "system_representation": str(cfg.system_representation or "coarse_grained"),
                "source_target_id": str(cfg.source_target_id or ""),
                "input_structure_ref": str(cfg.input_structure_ref or ""),
            },
            "runtime": {
                "provider_preference": provider_name,
                "route_decision_id": str(cfg.route_decision_id or ""),
                "runtime_bundle": dict(cfg.runtime_bundle or {}),
                "topology_ref": dict(cfg.topology_ref or {}),
                "coordinate_ref": dict(cfg.coordinate_ref or {}),
                "preprocessed_topology_ref": dict(cfg.preprocessed_topology_ref or {}),
            },
            "artifacts": {
                "artifact_refs": list(cfg.artifact_refs or []),
                "artifact_ref_records": list(cfg.artifact_ref_records or []),
                "output_gcs_prefix": str(cfg.output_gcs_prefix or ""),
            },
        }

    def build_submission_blocker(
        self,
        cfg: MartiniCGJobConfig,
        provider_name: str,
        request: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        bundle = dict(cfg.runtime_bundle or {})
        blockers: List[Dict[str, Any]] = []

        if not _artifact_uri(cfg.coordinate_ref):
            blockers.append(
                {
                    "code": "cg_coordinate_ref_missing",
                    "message": "Canonical CG runtime submit requires a coordinate_ref pointing to a real Martini-ready .gro coordinate artifact.",
                }
            )
        if not _artifact_uri(cfg.topology_ref):
            blockers.append(
                {
                    "code": "cg_topology_ref_missing",
                    "message": "Canonical CG runtime submit requires a topology_ref pointing to a real Martini topology artifact.",
                }
            )
        if not _artifact_uri(cfg.preprocessed_topology_ref):
            blockers.append(
                {
                    "code": "cg_preprocessed_topology_missing",
                    "message": "Canonical CG runtime submit requires a preprocessed_topology_ref for the martini_openmm-compatible topology path.",
                }
            )

        repo_root = _repo_root()
        worker_dockerfile = repo_root / "workers" / "salad" / "gcs_openmm_srcg" / "Dockerfile"
        worker_entrypoint = repo_root / "workers" / "salad" / "gcs_openmm_srcg" / "main_gcs.py"
        salad_submit_contract = repo_root / "src" / "mica" / "infrastructure" / "orchestration" / "salad_submit_contract.py"

        dockerfile_text = _read_text(worker_dockerfile)
        canonical_image_text = _read_text(repo_root / "mica-openmm-pod" / "Dockerfile")
        canonical_image_requirements = _read_text(repo_root / "mica-openmm-pod" / "requirements-pip.txt")
        entrypoint_text = _read_text(worker_entrypoint)
        submit_contract_text = _read_text(salad_submit_contract)
        digest_pinned_image_ref = _configured_digest_pinned_canonical_image_ref()
        has_local_image_runtime_proof = _has_textual_martini_runtime(
            canonical_image_text,
            canonical_image_requirements,
        )
        has_external_image_runtime_proof = bool(digest_pinned_image_ref)
        if not has_local_image_runtime_proof and not has_external_image_runtime_proof:
            blockers.append(
                {
                    "code": "cg_provider_image_missing_martini_runtime",
                    "message": "The canonical provider worker image does not install martini_openmm, so a provider-backed Martini/OpenMM runtime cannot boot yet.",
                    "source_file": str(repo_root / "mica-openmm-pod" / "requirements-pip.txt"),
                }
            )
        entrypoint_has_cg_mode = '"cg_martini"' in entrypoint_text
        if not entrypoint_has_cg_mode and not has_external_image_runtime_proof:
            blockers.append(
                {
                    "code": "cg_worker_mode_missing",
                    "message": "The canonical provider worker entrypoint has no cg_martini worker mode for Martini/OpenMM runtime bundles.",
                    "source_file": str(worker_entrypoint),
                }
            )
        if "cg_martini" not in submit_contract_text or "preprocessed_topology_path" not in submit_contract_text:
            blockers.append(
                {
                    "code": "cg_submit_contract_missing",
                    "message": "The current provider submit contract does not stage or bind Martini topology/coordinate inputs for a CG runtime lane.",
                    "source_file": str(salad_submit_contract),
                }
            )

        if not blockers:
            return None

        primary = blockers[0]
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "job": {
                "job_id": request.get("job", {}).get("job_id", ""),
                "workflow": request.get("job", {}).get("workflow", "cg_martini_smoke"),
                "execution_target": "remote",
                "execution_class": request.get("job", {}).get("execution_class", "research"),
            },
            "status": {
                "state": "failed",
                "phase": "submit_blocked",
                "terminal": True,
                "success": False,
                "reason_code": str(primary.get("code") or "cg_provider_blocked"),
                "reason_message": str(primary.get("message") or "provider-backed CG runtime blocked"),
            },
            "provider": {
                "name": provider_name,
                "adapter_id": self.adapter_id,
            },
            "effective_config": request,
            "backend_native": {
                "blocker_code": str(primary.get("code") or "cg_provider_blocked"),
                "blockers": blockers,
                "bundle_present": {
                    "coordinate_ref": bool(_artifact_uri(cfg.coordinate_ref)),
                    "topology_ref": bool(_artifact_uri(cfg.topology_ref)),
                    "preprocessed_topology_ref": bool(_artifact_uri(cfg.preprocessed_topology_ref)),
                },
                "worker_contract": {
                    "worker_dockerfile": str(worker_dockerfile),
                    "canonical_image_requirements": str(repo_root / "mica-openmm-pod" / "requirements-pip.txt"),
                    "canonical_image_ref": digest_pinned_image_ref or canonical_md_worker_image(),
                    "worker_entrypoint": str(worker_entrypoint),
                    "submit_contract_path": str(salad_submit_contract),
                    "dockerfile_has_martini_openmm": has_local_image_runtime_proof,
                    "digest_pinned_image_ref_present": bool(digest_pinned_image_ref),
                    "entrypoint_has_cg_mode": entrypoint_has_cg_mode,
                },
            },
            "artifacts": {
                "artifact_refs": list(cfg.artifact_refs or []),
                "artifact_ref_records": list(cfg.artifact_ref_records or []),
                "coordinate_ref": dict(cfg.coordinate_ref or {}),
                "topology_ref": dict(cfg.topology_ref or {}),
                "preprocessed_topology_ref": dict(cfg.preprocessed_topology_ref or {}),
                "output_gcs_prefix": str(cfg.output_gcs_prefix or ""),
            },
            "metadata": {
                "forcefield_family": str(cfg.forcefield_family or "martini3"),
                "system_representation": str(cfg.system_representation or "coarse_grained"),
                "source_target_id": str(cfg.source_target_id or ""),
                "route_decision_id": str(cfg.route_decision_id or ""),
                "bundle_id": str(bundle.get("bundle_id") or bundle.get("system_bundle_ref") or ""),
            },
        }

    def _build_submit_request(self, cfg: MartiniCGJobConfig) -> SaladMDSubmitRequest:
        return SaladMDSubmitRequest(
            user_id=str(getattr(cfg, "_mica_user_id", "") or "anonymous"),
            job_id=str(cfg.job_id or ""),
            coordinate_path=_artifact_uri(cfg.coordinate_ref),
            topology_path=_artifact_uri(cfg.topology_ref),
            preprocessed_topology_path=_artifact_uri(cfg.preprocessed_topology_ref),
            source_target_id=str(cfg.source_target_id or ""),
            runtime_bundle_id=str(
                cfg.runtime_bundle.get("system_bundle_ref")
                or cfg.runtime_bundle.get("bundle_id")
                or cfg.runtime_bundle.get("cg_system_bundle_ref")
                or ""
            ),
            steps=500,
            gpu_type=str(cfg.gpu_type_str or "L40S"),
            max_total_cost_usd=float(cfg.max_total_cost_usd or 0.0),
            execution_class=str(cfg.execution_class or "research"),
            simulation_mode="cg_martini",
            worker_mode="cg_martini",
            benchmark_steps=50,
            report_freq=25,
            saving_interval_seconds=180,
            chunk_steps_override=100,
            frame_interval_ps=50.0,
        )

    def normalize_result(
        self,
        raw_result: Any,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        if isinstance(raw_result, dict) and raw_result.get("schema_version") == RESULT_SCHEMA_VERSION:
            return raw_result
        payload = dict(raw_result or {})
        payload["artifact_manifest"] = _artifact_manifest_from_payload(payload)
        canonical = normalize_salad_execution_result(payload, request)
        status_str = str(payload.get("status", "unknown") or "unknown").lower()
        if status_str in {"failed", "error", "stopped"}:
            canonical["status"]["state"] = "failed"
            canonical["status"]["phase"] = "runtime_failed"
            canonical["status"]["terminal"] = True
            canonical["status"]["success"] = False
            if not canonical["status"].get("reason_code"):
                canonical["status"]["reason_code"] = "cg_provider_runtime_failed"
        canonical["provider"] = {
            **dict(canonical.get("provider") or {}),
            "name": provider_name,
            "adapter_id": self.adapter_id,
        }
        canonical["terminal_autopsy"] = dict(payload.get("terminal_autopsy") or {})
        canonical["teardown_proof"] = dict(payload.get("teardown_proof") or {})
        canonical["artifact_state"] = str(payload.get("artifact_state", "") or "")
        canonical["durability_evidence"] = dict(payload.get("durability_evidence") or {})
        return enforce_no_silent_success(canonical)


def _artifact_manifest_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    artifact_manifest = dict(payload.get("artifact_manifest") or {})
    if artifact_manifest:
        return artifact_manifest
    durability_evidence = dict(payload.get("durability_evidence") or {})
    runtime_signal = dict((payload.get("terminal_autopsy") or {}).get("runtime_signal") or {})
    object_listing = list(durability_evidence.get("object_listing") or runtime_signal.get("object_listing") or [])
    return {
        "completed_marker_confirmed": bool(
            durability_evidence.get("completed_marker_present") or runtime_signal.get("completed_marker_present")
        ),
        "object_listing": object_listing,
        "worker_history_json_present": bool(
            durability_evidence.get("worker_history_json_present") or runtime_signal.get("worker_history_json_present")
        ),
        "history_json_present": bool(
            durability_evidence.get("history_json_present") or runtime_signal.get("history_json_present")
        ),
        "failure_receipt_present": bool(
            durability_evidence.get("failure_receipt_present") or runtime_signal.get("failure_receipt_present")
        ),
    }


class _MartiniCGSaladExecution:
    def __init__(
        self,
        *,
        cfg: MartiniCGJobConfig,
        provider: Any,
        submit_request: SaladMDSubmitRequest,
        on_event: Any = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.submit_request = submit_request
        self.prepared = None
        self.on_event = on_event
        self.state = SimpleNamespace(instance_id="", total_cost_usd=0.0, phase=SimpleNamespace(value="queued"))

    async def run(self) -> Dict[str, Any]:
        self.state.phase = SimpleNamespace(value="provisioning")
        prepared = prepare_salad_md_submission(self.submit_request)
        self.prepared = prepared
        delegate = SaladGCSOrchestrator(config=self.prepared.job_cfg, provider=self.provider, on_event=self.on_event)
        result = await delegate.run()
        self.state.instance_id = str(result.get("cg_name", "") or "")
        self.state.total_cost_usd = float(
            dict(result.get("terminal_autopsy") or {}).get("metadata", {}).get("total_cost_usd", 0.0) or 0.0
        )
        self.state.phase = SimpleNamespace(value=str(result.get("status", "completed") or "completed"))
        return result
