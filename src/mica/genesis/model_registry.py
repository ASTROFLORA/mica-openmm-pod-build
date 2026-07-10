from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from mica.compute.provider_preflight import run_provider_preflight
from mica.infrastructure.runpod_client import RunPodClient
from mica.serverless_models.catalog import list_builtin_model_descriptors
from mica.serverless_models.contracts import ServerlessModelDescriptor
from mica.serverless_models.models.esm3_modal_app import (
    run_esm3_modal_preflight_smoke,
    run_esm3_modal_tiny_smoke,
)
from mica.serverless_models.registry import ServerlessModelRegistry

from .model_contracts import (
    GenesisModelCapability,
    GenesisModelExecutionReceipt,
    build_genesis_output_artifact_contract,
    utcnow_iso,
)


PROGRAM_ROOT = ".mica/programs/GENESIS_SUPERNOVA"
PACKET_FOUNDATION = (
    f"{PROGRAM_ROOT}/GENESIS_SUPERNOVA_MODEL_INFRA_FOUNDATION_V1_PACKET.md"
)
PACKET_ESM3 = (
    f"{PROGRAM_ROOT}/GENESIS_INFRA_02C_ESM3_MODAL_IMAGE_PATH_AND_TINY_SMOKE_PACKET.md"
)


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive
            error["value"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def _first_present_env(
    env: Mapping[str, str],
    keys: Iterable[str],
) -> tuple[str, str]:
    for key in keys:
        value = str(env.get(str(key), "") or "").strip()
        if value:
            return str(key), value
    return "", ""


def _descriptor_path_for(model_id: str) -> str:
    mapping = {
        "biolinkbert.embed.text.1024": "src/mica/serverless_models/models/biolinkbert.py",
        "esm3.predict.structure": "src/mica/serverless_models/models/esm3.py",
        "proteinmpnn.design.sequence": "src/mica/serverless_models/models/proteinmpnn.py",
        "openfold.predict.structure": "src/mica/serverless_models/models/openfold.py",
        "complexa.design.binder": "src/mica/serverless_models/models/complexa.py",
        "la_proteina.generate.atomistic": "src/mica/serverless_models/models/la_proteina.py",
    }
    return mapping.get(model_id, "")


def _supported_tasks_for(model_id: str, descriptor: ServerlessModelDescriptor) -> List[str]:
    explicit = {
        "biolinkbert.embed.text.1024": ["embedding", "dataset_filtering"],
        "esm3.predict.structure": ["structure_prediction", "conditioning"],
        "proteinmpnn.design.sequence": ["sequence_design", "inverse_folding"],
        "openfold.predict.structure": ["structure_prediction"],
        "complexa.design.binder": ["sequence_design", "conditioning", "complex_prediction"],
        "la_proteina.generate.atomistic": ["sequence_generation", "conditioning"],
    }
    tasks = explicit.get(model_id)
    if tasks:
        return tasks
    family_default = {
        "structure_prediction": ["structure_prediction"],
        "protein_design": ["sequence_design"],
        "protein_generation": ["sequence_generation"],
        "embeddings": ["embedding"],
    }
    return family_default.get(descriptor.family, ["conditioning"])


def _estimated_cost_class(model_id: str) -> str:
    if model_id == "biolinkbert.embed.text.1024":
        return "low_gpu"
    if model_id == "esm3.predict.structure":
        return "medium_gpu"
    if model_id == "proteinmpnn.design.sequence":
        return "medium_gpu"
    if model_id == "openfold.predict.structure":
        return "high_gpu"
    if model_id in {"complexa.design.binder", "la_proteina.generate.atomistic"}:
        return "high_gpu"
    return "unknown"


def _readiness_state_for(model_id: str, descriptor: ServerlessModelDescriptor) -> str:
    override = {
        "biolinkbert.embed.text.1024": "endpoint_probe_required",
        "esm3.predict.structure": "ready_live_tiny_smoke",
        "proteinmpnn.design.sequence": "ready_descriptor",
        "openfold.predict.structure": "image_ready",
        "complexa.design.binder": "external_repo_audited",
        "la_proteina.generate.atomistic": "external_repo_audited",
    }
    return override.get(model_id, str(descriptor.metadata.get("readiness_state") or "ready_descriptor"))


def _evidence_backing_for(model_id: str) -> str:
    if model_id == "biolinkbert.embed.text.1024":
        return "code-backed"
    if model_id == "esm3.predict.structure":
        return "runtime-backed"
    if model_id in {"proteinmpnn.design.sequence", "openfold.predict.structure"}:
        return "code-backed"
    return "doc-backed"


def _blocker_for(model_id: str) -> str:
    mapping = {
        "biolinkbert.embed.text.1024": "awaiting_runpod_endpoint_preflight",
        "esm3.predict.structure": "",
        "proteinmpnn.design.sequence": "awaiting_genesis_remote_preflight",
        "openfold.predict.structure": "endpoint_not_integrated",
        "complexa.design.binder": "blocked_missing_weights",
        "la_proteina.generate.atomistic": "blocked_missing_weights",
    }
    return mapping.get(model_id, "descriptor_not_present_in_repo")


def _product_exposure_for(model_id: str) -> Dict[str, Any]:
    return {
        "api_route": "/api/v1/serverless-models",
        "cli_surface": "tools/mica_agent.py",
        "ui_surface": "future",
        "current_lane": model_id,
    }


def _receipt_schema_for(model_id: str) -> Dict[str, Any]:
    mapping = {
        "biolinkbert.embed.text.1024": {
            "primary_receipt_key": "embedding_receipt",
            "preflight_receipt_key": "biolinkbert_runpod_endpoint_probe",
        },
        "esm3.predict.structure": {
            "primary_receipt_key": "esm3_inference_receipt",
            "preflight_receipt_key": "esm3_modal_remote_preflight_receipt",
        },
        "proteinmpnn.design.sequence": {
            "primary_receipt_key": "execution_record",
            "preflight_receipt_key": "proteinmpnn_runpod_preflight",
        },
        "openfold.predict.structure": {
            "primary_receipt_key": "structural_generation_receipt",
            "preflight_receipt_key": "openfold_image_preflight_receipt",
        },
        "complexa.design.binder": {
            "primary_receipt_key": "complexa_design_receipt",
            "preflight_receipt_key": "complexa_toolchain_receipt",
        },
        "la_proteina.generate.atomistic": {
            "primary_receipt_key": "la_proteina_generation_receipt",
            "preflight_receipt_key": "la_proteina_toolchain_receipt",
        },
    }
    return mapping.get(model_id, {"primary_receipt_key": "model_receipt"})


def _cost_runtime_policy_for(model_id: str) -> Dict[str, Any]:
    policy = {
        "biolinkbert.embed.text.1024": {
            "preflight": "runpod_endpoint_health_if_configured",
            "smoke": "cheap_short_text_embedding_allowed",
            "heavy_inference": "not_applicable",
        },
        "esm3.predict.structure": {
            "preflight": "remote_modal_preflight_allowed",
            "smoke": "tiny_live_smoke_allowed",
            "heavy_inference": "blocked",
        },
        "proteinmpnn.design.sequence": {
            "preflight": "remote_endpoint_health_if_configured",
            "smoke": "cost_guarded_until_explicit_opt_in",
            "heavy_inference": "blocked",
        },
        "openfold.predict.structure": {
            "preflight": "image_ref_and_contract_only",
            "smoke": "blocked_endpoint_not_integrated",
            "heavy_inference": "blocked",
        },
    }
    return policy.get(
        model_id,
        {
            "preflight": "descriptor_and_blocker_only",
            "smoke": "blocked",
            "heavy_inference": "blocked",
        },
    )


def _fallback_policy_for(model_id: str) -> Dict[str, Any]:
    policy = {
        "biolinkbert.embed.text.1024": {
            "degrade": "typed_semantic_lane_blocker_only",
            "fake_inference_forbidden": True,
        },
        "esm3.predict.structure": {
            "degrade": "fixture_or_not_configured_receipt_only",
            "fake_inference_forbidden": True,
        },
        "proteinmpnn.design.sequence": {
            "degrade": "typed_preflight_blocker_only",
            "fake_inference_forbidden": True,
        },
        "openfold.predict.structure": {
            "degrade": "image_ready_without_live_endpoint",
            "fake_inference_forbidden": True,
        },
    }
    return policy.get(
        model_id,
        {
            "degrade": "descriptor_only",
            "fake_inference_forbidden": True,
        },
    )


def _evidence_gate_for(model_id: str) -> Dict[str, Any]:
    gate = {
        "biolinkbert.embed.text.1024": {
            "requires_real_endpoint_health_or_typed_blocker": True,
            "requires_no_fake_embedding_output": True,
        },
        "esm3.predict.structure": {
            "requires_real_remote_receipt": True,
            "requires_secret_redaction": True,
            "requires_artifact_contract": True,
        },
        "proteinmpnn.design.sequence": {
            "requires_real_endpoint_health_or_typed_blocker": True,
            "requires_no_fake_sequence_output": True,
        },
        "openfold.predict.structure": {
            "requires_official_image_ref": True,
            "requires_digest_pin_for_production": True,
        },
    }
    return gate.get(model_id, {"requires_typed_blocker_if_not_live": True})


def _build_capability_from_descriptor(descriptor: ServerlessModelDescriptor) -> GenesisModelCapability:
    model_id = descriptor.model_id
    metadata = dict(descriptor.metadata or {})
    readiness_state = _readiness_state_for(model_id, descriptor)
    last_evidence_packet = PACKET_ESM3 if model_id == "esm3.predict.structure" else PACKET_FOUNDATION
    return GenesisModelCapability(
        model_id=model_id,
        display_name=descriptor.display_name,
        model_family=descriptor.family,
        provider=str(metadata.get("provider") or (descriptor.provider_preference[0] if descriptor.provider_preference else "unconfigured")),
        runtime_kind=str(metadata.get("backend") or ("serverless_" + descriptor.execution_mode)),
        supported_tasks=list(_supported_tasks_for(model_id, descriptor)),
        input_contract=dict(descriptor.input_schema),
        output_contract=dict(descriptor.output_schema),
        artifact_policy=dict(descriptor.artifact_policy),
        gcs_workspace_required=True,
        estimated_cost_class=_estimated_cost_class(model_id),
        secrets_required=list(metadata.get("secrets_required") or []),
        preflight_supported=True,
        smoke_supported=model_id == "esm3.predict.structure",
        production_ready=False,
        blocker=_blocker_for(model_id),
        readiness_state=readiness_state,
        evidence_backing=_evidence_backing_for(model_id),
        code_paths=[path for path in (_descriptor_path_for(model_id),) if path],
        image_ref=str(metadata.get("image_ref") or ""),
        dockerfile_ref=str(metadata.get("dockerfile_ref") or ""),
        ghcr_ref=str(metadata.get("ghcr_ref") or ""),
        last_evidence_packet=last_evidence_packet,
        receipt_schema=_receipt_schema_for(model_id),
        cost_runtime_policy=_cost_runtime_policy_for(model_id),
        fallback_policy=_fallback_policy_for(model_id),
        evidence_gate=_evidence_gate_for(model_id),
        product_exposure=_product_exposure_for(model_id),
        metadata=metadata,
    )


def _doc_only_capabilities() -> List[GenesisModelCapability]:
    common_artifact = build_genesis_output_artifact_contract()
    entries = [
        {
            "model_id": "proteina.generate.structure",
            "display_name": "Proteina Generic Lane",
            "blocker": "descriptor_not_present_in_repo",
            "readiness_state": "backlog",
            "supported_tasks": ["sequence_generation", "conditioning"],
        },
        {
            "model_id": "progen2.generate.sequence",
            "display_name": "ProGen2 Future Lane",
            "blocker": "descriptor_not_present_in_repo",
            "readiness_state": "backlog",
            "supported_tasks": ["sequence_generation", "conditioning"],
        },
        {
            "model_id": "protgpt2.generate.sequence",
            "display_name": "ProtGPT2 Future Lane",
            "blocker": "descriptor_not_present_in_repo",
            "readiness_state": "backlog",
            "supported_tasks": ["sequence_generation", "conditioning"],
        },
        {
            "model_id": "zymctrl.generate.sequence",
            "display_name": "ZymCTRL Future Lane",
            "blocker": "descriptor_not_present_in_repo",
            "readiness_state": "backlog",
            "supported_tasks": ["sequence_generation", "conditioning"],
        },
    ]
    capabilities: List[GenesisModelCapability] = []
    for entry in entries:
        capabilities.append(
            GenesisModelCapability(
                model_id=entry["model_id"],
                display_name=entry["display_name"],
                model_family="protein_generation",
                provider="unconfigured",
                runtime_kind="future_training_or_serverless_lane",
                supported_tasks=list(entry["supported_tasks"]),
                input_contract={"required": [], "properties": {}},
                output_contract={"properties": {}},
                artifact_policy={"contract_ref": common_artifact["schema_version"]},
                gcs_workspace_required=True,
                estimated_cost_class="unknown",
                secrets_required=[],
                preflight_supported=False,
                smoke_supported=False,
                production_ready=False,
                blocker=entry["blocker"],
                readiness_state=entry["readiness_state"],
                evidence_backing="future",
                last_evidence_packet=PACKET_FOUNDATION,
                receipt_schema={"primary_receipt_key": "model_receipt"},
                cost_runtime_policy={"preflight": "not_supported", "smoke": "not_supported"},
                fallback_policy={"degrade": "typed_backlog_only", "fake_inference_forbidden": True},
                evidence_gate={"requires_descriptor_before_runtime": True},
                product_exposure=_product_exposure_for(entry["model_id"]),
            )
        )
    return capabilities


@dataclass
class GenesisModelRegistry:
    capabilities: Dict[str, GenesisModelCapability]

    def get(self, model_id: str) -> Optional[GenesisModelCapability]:
        return self.capabilities.get(model_id)

    def require(self, model_id: str) -> GenesisModelCapability:
        capability = self.get(model_id)
        if capability is None:
            raise KeyError(f"Unknown Genesis model capability: {model_id}")
        return capability

    def list_capabilities(self) -> List[GenesisModelCapability]:
        return list(self.capabilities.values())


def build_genesis_model_registry(
    *,
    include_doc_only: bool = True,
) -> GenesisModelRegistry:
    serverless_registry = ServerlessModelRegistry.from_iterable(list_builtin_model_descriptors())
    capabilities: Dict[str, GenesisModelCapability] = {}
    for descriptor in serverless_registry.list_enabled():
        if descriptor.model_id not in {
            "biolinkbert.embed.text.1024",
            "esm3.predict.structure",
            "proteinmpnn.design.sequence",
            "openfold.predict.structure",
        }:
            continue
        capability = _build_capability_from_descriptor(descriptor)
        capabilities[capability.model_id] = capability

    for descriptor in list_builtin_model_descriptors():
        if descriptor.model_id not in {
            "complexa.design.binder",
            "la_proteina.generate.atomistic",
        }:
            continue
        capability = _build_capability_from_descriptor(descriptor)
        capabilities[capability.model_id] = capability

    if include_doc_only:
        for capability in _doc_only_capabilities():
            capabilities[capability.model_id] = capability
    return GenesisModelRegistry(capabilities=capabilities)


@dataclass
class GenesisModelPreflightRunner:
    registry: GenesisModelRegistry = field(default_factory=build_genesis_model_registry)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    allow_live_remote_preflight: bool = False
    allow_live_smoke: bool = False
    timeout_seconds: int = 120
    receipts: List[Dict[str, Any]] = field(default_factory=list)

    def list_capabilities(self) -> List[Dict[str, Any]]:
        return [capability.to_dict() for capability in self.registry.list_capabilities()]

    def preflight(self, model_id: str) -> Dict[str, Any]:
        capability = self.registry.require(model_id)
        if model_id == "esm3.predict.structure":
            receipt = self._preflight_esm3(capability)
        elif model_id == "proteinmpnn.design.sequence":
            receipt = self._preflight_proteinmpnn(capability)
        elif model_id == "openfold.predict.structure":
            receipt = self._preflight_openfold(capability)
        elif model_id == "complexa.design.binder":
            receipt = self._preflight_weight_blocked(capability)
        elif model_id == "la_proteina.generate.atomistic":
            receipt = self._preflight_weight_blocked(capability)
        else:
            receipt = self._preflight_doc_only(capability)
        self.receipts.append(receipt)
        return receipt

    def smoke(self, model_id: str, cheap_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        capability = self.registry.require(model_id)
        if model_id == "esm3.predict.structure":
            receipt = self._smoke_esm3(capability, cheap_input or {})
        elif model_id == "proteinmpnn.design.sequence":
            receipt = self._smoke_cost_guarded(capability)
        elif model_id == "openfold.predict.structure":
            receipt = self._blocked_smoke(capability, "blocked_endpoint_not_integrated")
        elif model_id in {"complexa.design.binder", "la_proteina.generate.atomistic"}:
            receipt = self._blocked_smoke(capability, "blocked_missing_weights")
        else:
            receipt = self._blocked_smoke(capability, capability.blocker or "not_supported")
        self.receipts.append(receipt)
        return receipt

    def emit_receipt(self) -> Dict[str, Any]:
        statuses: Dict[str, int] = {}
        for receipt in self.receipts:
            statuses[receipt["status"]] = statuses.get(receipt["status"], 0) + 1
        return {
            "schema_version": "genesis_model_registry_receipt_v1",
            "created_at": utcnow_iso(),
            "model_count": len(self.registry.capabilities),
            "receipt_count": len(self.receipts),
            "status_counts": statuses,
            "receipts": list(self.receipts),
        }

    def _make_receipt(
        self,
        capability: GenesisModelCapability,
        *,
        action: str,
        status: str,
        readiness_state: str,
        blockers: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        remote_probe: Optional[Dict[str, Any]] = None,
        artifact_manifest: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        receipt = GenesisModelExecutionReceipt(
            model_id=capability.model_id,
            action=action,
            status=status,
            readiness_state=readiness_state,
            provider=capability.provider,
            runtime_kind=capability.runtime_kind,
            production_ready=capability.production_ready,
            blockers=list(blockers or []),
            warnings=list(warnings or []),
            raw_secret_logged=False,
            remote_probe=dict(remote_probe or {}),
            artifact_manifest=dict(artifact_manifest or {}),
        )
        return receipt.to_dict()

    def _preflight_esm3(self, capability: GenesisModelCapability) -> Dict[str, Any]:
        payload = _run_async(
            run_esm3_modal_preflight_smoke(
                include_image_preflight=True,
                attempt_live_remote=self.allow_live_remote_preflight,
                timeout_seconds=self.timeout_seconds,
            )
        )
        remote_receipt = dict(payload.get("esm3_modal_remote_preflight_receipt") or {})
        warnings = list(remote_receipt.get("warnings") or [])
        blockers = list(remote_receipt.get("blockers") or [])
        failure_code = str(remote_receipt.get("failure_code") or "")
        if failure_code and failure_code not in blockers:
            blockers.append(failure_code)
        status = str(payload.get("status") or "partial_runtime_blocked")
        return self._make_receipt(
            capability,
            action="preflight",
            status=status,
            readiness_state=str(payload.get("readiness_state") or status),
            blockers=blockers,
            warnings=warnings,
            remote_probe=payload,
        )

    def _smoke_esm3(
        self,
        capability: GenesisModelCapability,
        cheap_input: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not self.allow_live_smoke:
            return self._make_receipt(
                capability,
                action="smoke",
                status="cost_guard_blocked",
                readiness_state=capability.readiness_state,
                blockers=["cost_guard_blocked"],
            )
        payload = _run_async(
            run_esm3_modal_tiny_smoke(
                dict(cheap_input) or None,
                timeout_seconds=max(self.timeout_seconds, 300),
            )
        )
        status = str(payload.get("status") or "failed_runtime")
        blockers = []
        failure_code = str(payload.get("failure_code") or "")
        if failure_code:
            blockers.append(failure_code)
        artifact_manifest = {
            "primary_structure_name": str((payload.get("artifact_summary") or {}).get("primary_structure_name") or ""),
            "archive_members": list((payload.get("artifact_summary") or {}).get("archive_members") or []),
        }
        return self._make_receipt(
            capability,
            action="smoke",
            status=status,
            readiness_state=capability.readiness_state if status == "completed" else "partial_runtime_blocked",
            blockers=blockers,
            remote_probe=payload,
            artifact_manifest=artifact_manifest,
        )

    def _preflight_proteinmpnn(self, capability: GenesisModelCapability) -> Dict[str, Any]:
        descriptor = next(
            descriptor
            for descriptor in list_builtin_model_descriptors()
            if descriptor.model_id == capability.model_id
        )
        env_preflight = run_provider_preflight("runpod", env=dict(self.env)).to_dict()
        endpoint_key, endpoint_id = _first_present_env(
            self.env,
            descriptor.metadata.get("endpoint_env_vars", []),
        )
        warnings: List[str] = []
        blockers: List[str] = []
        remote_probe: Dict[str, Any] = {
            "provider_env_preflight": env_preflight,
            "endpoint_env_var": endpoint_key,
            "endpoint_configured": bool(endpoint_id),
        }
        if not env_preflight["ok"]:
            blockers.append("blocked_missing_secret")
        if not endpoint_id:
            blockers.append("blocked_missing_endpoint")
        if blockers or not self.allow_live_remote_preflight:
            status = blockers[0] if len(blockers) == 1 else ("preflight_skipped" if not blockers else "partial_runtime_blocked")
            if not self.allow_live_remote_preflight:
                warnings.append("Live ProteinMPNN endpoint health probe skipped by configuration.")
            return self._make_receipt(
                capability,
                action="preflight",
                status=status,
                readiness_state=capability.readiness_state,
                blockers=blockers,
                warnings=warnings,
                remote_probe=remote_probe,
            )

        async def _probe_health() -> Dict[str, Any]:
            client = RunPodClient(
                api_key=str(self.env.get("RUNPOD_API_KEY") or ""),
                endpoint_id=endpoint_id,
                timeout=max(self.timeout_seconds, 30),
            )
            try:
                health = await client.get_endpoint_health(endpoint_id=endpoint_id)
                return {
                    "jobs_completed": health.jobs_completed,
                    "jobs_failed": health.jobs_failed,
                    "jobs_in_progress": health.jobs_in_progress,
                    "jobs_in_queue": health.jobs_in_queue,
                    "jobs_retried": health.jobs_retried,
                    "workers_idle": health.workers_idle,
                    "workers_running": health.workers_running,
                }
            finally:
                await client.close()

        try:
            remote_probe["endpoint_health"] = _run_async(_probe_health())
            return self._make_receipt(
                capability,
                action="preflight",
                status="ready_remote_preflight",
                readiness_state="ready_remote_preflight",
                remote_probe=remote_probe,
            )
        except Exception as exc:
            remote_probe["error"] = f"{exc.__class__.__name__}: {exc}"
            return self._make_receipt(
                capability,
                action="preflight",
                status="partial_runtime_blocked",
                readiness_state=capability.readiness_state,
                blockers=["blocked_endpoint_probe_failed"],
                remote_probe=remote_probe,
            )

    def _preflight_openfold(self, capability: GenesisModelCapability) -> Dict[str, Any]:
        image_ref = capability.image_ref or str(capability.metadata.get("image_ref") or "")
        blockers: List[str] = []
        warnings: List[str] = []
        if not image_ref:
            blockers.append("blocked_missing_image_ref")
        if image_ref and "@sha256:" not in image_ref:
            warnings.append("production_digest_unpinned")
        blockers.append("blocked_endpoint_not_integrated")
        return self._make_receipt(
            capability,
            action="preflight",
            status="partial_runtime_blocked",
            readiness_state=capability.readiness_state,
            blockers=blockers,
            warnings=warnings,
            remote_probe={
                "image_ref": image_ref,
                "digest_pinned": "@sha256:" in image_ref,
            },
        )

    def _preflight_weight_blocked(self, capability: GenesisModelCapability) -> Dict[str, Any]:
        return self._make_receipt(
            capability,
            action="preflight",
            status="blocked_missing_weights",
            readiness_state=capability.readiness_state,
            blockers=["blocked_missing_weights"],
            remote_probe={
                "weights_required": list(capability.metadata.get("weights_required") or []),
                "image_ref": capability.image_ref,
            },
        )

    def _preflight_doc_only(self, capability: GenesisModelCapability) -> Dict[str, Any]:
        blocker = capability.blocker or "not_supported"
        return self._make_receipt(
            capability,
            action="preflight",
            status=blocker,
            readiness_state=capability.readiness_state,
            blockers=[blocker],
            remote_probe={"descriptor_present": False},
        )

    def _smoke_cost_guarded(self, capability: GenesisModelCapability) -> Dict[str, Any]:
        return self._make_receipt(
            capability,
            action="smoke",
            status="cost_guard_blocked",
            readiness_state=capability.readiness_state,
            blockers=["cost_guard_blocked"],
        )

    def _blocked_smoke(self, capability: GenesisModelCapability, blocker: str) -> Dict[str, Any]:
        return self._make_receipt(
            capability,
            action="smoke",
            status=blocker,
            readiness_state=capability.readiness_state,
            blockers=[blocker],
        )
