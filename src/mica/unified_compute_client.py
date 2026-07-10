"""
unified_compute_client.py — W6-1: Unified Compute Client Facade

Single async entry-point for all MICA compute operations:
  • Submit MD job (provisions pod, runs simulation, streams events)
  • Query job status (phase, cost, health, trajectory)
  • Cancel / stop / destroy jobs
  • List economic ledgers across providers
  • Cascade across providers (Vast → RunPod → GCP)

The client delegates to:
  - CloudOrchestrator   — multi-provider search & provision
  - VastMDOrchestrator  — Vast-specific 9-phase MD lifecycle
  - MDOrchestrator      — provider-agnostic alias
  - Economic ledger     — cost tracking

Usage:
    client = UnifiedComputeClient.from_env()
    result = await client.submit_md_job(MDJobConfig(pdb_path="1ubq.pdb"))
    status = await client.get_job_status(result.job_id)
    await client.cancel_job(result.job_id)
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .drivers.md_execution_contract import enforce_no_silent_success, normalize_remote_execution_result
from .env_aliases import bootstrap_runtime_env
from .infrastructure.cloud_orchestrator import CloudOrchestrator, SelectionStrategy
from .infrastructure.orchestration.biostate_engine_job import BioStateEngineJob
from .infrastructure.orchestration.md_adapter_registry import build_default_md_adapter_registry
from .infrastructure.orchestration.md_execution_protocol import RESULT_SCHEMA_VERSION
from .infrastructure.providers.base_provider import (
    CloudProvider,
    GPUType,
    InstanceStatus,
    ProvisionRequest,
)

logger = logging.getLogger(__name__)

_SALAD_PROGRESS_RE = re.compile(
    r"Progress:\s*(?P<percent>\d+(?:\.\d+)?)%\s*\((?P<ns>\d+(?:\.\d+)?)\s*ns\)\s*\|\s*Speed:\s*(?P<speed>\d+(?:\.\d+)?)\s*ns/day",
    re.IGNORECASE,
)
_DEFAULT_TERMINAL_SMIC_ANALYSES = ("rmsd", "contacts")
_DEFAULT_LIVE_SMIC_ANALYSES = ("rmsd", "contacts")
_SALAD_REMOTE_REFRESH_PENDING_SECONDS = max(5, int(os.getenv("MICA_SALAD_REMOTE_REFRESH_PENDING_SECONDS", "15")))
_SALAD_REMOTE_REFRESH_RUNNING_SECONDS = max(10, int(os.getenv("MICA_SALAD_REMOTE_REFRESH_RUNNING_SECONDS", "30")))
_SALAD_LIVE_SMIC_MIN_INTERVAL_SECONDS = max(30, int(os.getenv("MICA_SALAD_LIVE_SMIC_MIN_INTERVAL_SECONDS", "180")))
_SALAD_LIVE_SMIC_MIN_PROGRESS_NS = max(0.5, float(os.getenv("MICA_SALAD_LIVE_SMIC_MIN_PROGRESS_NS", "2.0")))


def _sanitize_remote_md_pdb_bytes(pdb_bytes: bytes) -> bytes:
    """Drop unsupported heterogens before staging a PDB for the remote MD worker."""
    text = pdb_bytes.decode("utf-8")
    kept_lines: List[str] = []
    removed_records = 0
    for line in text.splitlines():
        if line.startswith("HETATM"):
            residue_name = line[17:20].strip()
            if residue_name not in {"HOH", "WAT"}:
                removed_records += 1
                continue
        kept_lines.append(line)
    if removed_records:
        logger.info(
            "Remote MD staging stripped %d unsupported HETATM records before GCS upload",
            removed_records,
        )
    normalized = "\n".join(kept_lines)
    if kept_lines:
        normalized += "\n"
    return normalized.encode("utf-8")


def _parse_salad_durable_prefix(output_gcs_prefix: str, job_id: str) -> str:
    prefix_path = str(output_gcs_prefix or "").strip()
    if prefix_path.startswith("gs://"):
        parts = prefix_path[5:].split("/", 1)
        prefix_path = parts[1] if len(parts) > 1 else ""
    prefix_path = prefix_path.strip().rstrip("/")
    if prefix_path:
        return prefix_path
    return f"md-jobs/{job_id}"


def _salad_terminal_bundle_root(output_gcs_prefix: str, job_id: str) -> str:
    return f"{_parse_salad_durable_prefix(output_gcs_prefix, job_id)}/analysis/smic_bundle"


def _salad_terminal_bundle_packet_id(output_gcs_prefix: str, job_id: str) -> str:
    durable_prefix = _parse_salad_durable_prefix(output_gcs_prefix, job_id)
    packet_id = durable_prefix.rsplit("/", 1)[-1].strip()
    return packet_id or job_id


def _salad_runtime_receipts_root(output_gcs_prefix: str, job_id: str) -> str:
    return f"{_parse_salad_durable_prefix(output_gcs_prefix, job_id)}/analysis/runtime_receipts"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ComputeJobState(Enum):
    """High-level job lifecycle states."""
    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class ComputeJobSummary:
    """Lightweight status summary for any compute job."""
    job_id: str
    state: ComputeJobState
    provider: str = ""
    instance_id: str = ""
    gpu_type: str = ""
    execution_class: str = "research"
    phase: str = ""
    elapsed_seconds: float = 0.0
    total_cost_usd: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    user_id: str = ""
    awaiting_since: Optional[str] = None


@dataclass
class SubmitResult:
    """Result of submitting a compute job."""
    job_id: str
    provider: str
    instance_id: str = ""
    accepted: bool = True
    error: Optional[str] = None
    route_decision_id: str = ""


# ---------------------------------------------------------------------------
# Unified Compute Client
# ---------------------------------------------------------------------------

class UnifiedComputeClient:
    """Facade over MICA's compute substrate.

    All methods are async. The client tracks active jobs and can query
    their status from the underlying orchestrators.
    """

    def __init__(
        self,
        cloud_orchestrator: Optional[CloudOrchestrator] = None,
        default_provider: str = "vast",
        on_event: Optional[Callable[..., Any]] = None,
        max_concurrent_jobs: int = 10,
        cost_ceiling_usd: float = 500.0,
        md_adapter_registry: Any = None,
    ):
        self._cloud = cloud_orchestrator or CloudOrchestrator()
        self._default_provider = default_provider
        self._on_event = on_event
        self._cost_ceiling = cost_ceiling_usd
        self._md_adapters = md_adapter_registry or build_default_md_adapter_registry()

        # Concurrency control (W3-1)
        self._max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self._semaphore = asyncio.Semaphore(self._max_concurrent_jobs)
        self._job_queue: asyncio.Queue = asyncio.Queue()
        self._queue_drainer_tasks: List[asyncio.Task] = []

        # job_id → orchestrator instance (VastMDOrchestrator / MDOrchestrator)
        self._active_jobs: Dict[str, Any] = {}
        # job_id → ComputeJobSummary (lightweight cache)
        self._summaries: Dict[str, ComputeJobSummary] = {}

    def _provider_order_for_cfg(
        self,
        cfg: Any,
        preferred_provider: Optional[str] = None,
    ) -> List[tuple[str, CloudProvider]]:
        """Return provider candidates in the order this config should try them."""
        ordered_names: List[str] = []

        preferred_provider = str(
            preferred_provider or getattr(cfg, "preferred_provider", "") or ""
        ).strip().lower()
        if not preferred_provider:
            if hasattr(cfg, "pdb_gcs_path") and hasattr(cfg, "output_gcs_prefix"):
                preferred_provider = "salad"

        available_names = self._cloud.providers.keys()
        preferred_provider = self._md_adapters.resolve_provider_alias(
            preferred_provider,
            available_provider_names=available_names,
        )
        default_provider = self._md_adapters.resolve_provider_alias(
            self._default_provider,
            available_provider_names=available_names,
        )

        for candidate in (preferred_provider, default_provider):
            if candidate and candidate in self._cloud.providers and candidate not in ordered_names:
                ordered_names.append(candidate)

        for candidate in self._cloud.providers.keys():
            if candidate not in ordered_names:
                ordered_names.append(candidate)

        return [(name, self._cloud.providers[name]) for name in ordered_names]

    def _build_md_orchestrator(self, cfg: Any, provider: CloudProvider) -> Any:
        """Create the provider-appropriate orchestrator for an MD config."""
        return self._md_adapters.create_execution(cfg, provider, on_event=self._on_event)

    async def run_biostate_engine_job(
        self,
        job: BioStateEngineJob,
        *,
        on_event: Optional[Callable[..., Any]] = None,
    ) -> Dict[str, Any]:
        """Run one BioState engine remote job through the adapter registry."""
        route_decision_id = job.execution_request.get("runtime", {}).get("route_decision_id", "")
        if not route_decision_id:
            route_decision_id = self._route_decision_id_for_cfg(
                type("RouteCfg", (), {"route_decision_id": "", "job_id": job.job_id})(),
                job.job_id,
            )
            job = job.with_route_decision_id(route_decision_id)

        cfg = job.to_vast_md_config()
        provider_candidates = self._provider_order_for_cfg(
            cfg,
            preferred_provider=job.preferred_provider,
        )
        if not provider_candidates:
            return self._build_biostate_remote_error(
                job,
                route_decision_id=route_decision_id,
                provider_name="none",
                message="No compute providers registered",
            )

        last_error: Optional[str] = None
        for provider_name, provider in provider_candidates:
            try:
                execution = self._md_adapters.create_execution(
                    cfg,
                    provider,
                    on_event=on_event or self._on_event,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Skipping provider %s for BioState engine job %s during adapter construction: %s",
                    provider_name,
                    job.job_id,
                    exc,
                )
                continue

            canonical = await execution.run()
            provider_block = dict(canonical.get("provider") or {})
            status_block = dict(canonical.get("status") or {})
            backend_native = dict(canonical.get("backend_native") or {})
            return {
                "workflow": str(canonical.get("job", {}).get("workflow") or "protein_ligand_md"),
                "execution_mode": f"remote_{provider_name}",
                "adapter_id": str(provider_block.get("adapter_id", "") or ""),
                "status": "completed" if bool(status_block.get("success", False)) else "failed",
                "success": bool(status_block.get("success", False)),
                "job_id": str(canonical.get("job", {}).get("job_id", "") or job.job_id),
                "provider": provider_name,
                "provider_job_id": str(provider_block.get("instance_id", "") or backend_native.get("job_id", "")),
                "route_decision_id": route_decision_id,
                "results_json": dict(backend_native.get("results_json") or {}),
                "output_dir": str(canonical.get("artifacts", {}).get("output_dir", "") or backend_native.get("output_dir", "")),
                "execution_result_v1": canonical,
                "terminal_autopsy": dict(canonical.get("terminal_autopsy") or {}),
                "teardown_proof": dict(canonical.get("teardown_proof") or {}),
                "durability_evidence": dict(canonical.get("durability_evidence") or {}),
                "storage_object_paths": {
                    "manifest_path": str(canonical.get("artifacts", {}).get("manifest_path", "") or ""),
                    "resume_spec_path": str(canonical.get("artifacts", {}).get("resume_spec_path", "") or ""),
                    "output_gcs_prefix": str(canonical.get("artifacts", {}).get("output_gcs_prefix", "") or ""),
                },
                "cost_telemetry": {
                    "total_cost_usd": provider_block.get("total_cost_usd", backend_native.get("total_cost_usd", 0.0)),
                },
            }

        return self._build_biostate_remote_error(
            job,
            route_decision_id=route_decision_id,
            provider_name=job.preferred_provider,
            message=last_error or "No compatible compute provider available for this BioState engine job",
        )

    def _build_biostate_remote_error(
        self,
        job: BioStateEngineJob,
        *,
        route_decision_id: str,
        provider_name: str,
        message: str,
    ) -> Dict[str, Any]:
        request = job.with_route_decision_id(route_decision_id).execution_request
        request.setdefault("runtime", {})
        request["runtime"]["provider_preference"] = provider_name
        request["engine_handoff"] = dict(job.handoff)
        raw = {
            "workflow": "protein_ligand_md",
            "execution_mode": f"remote_{provider_name or 'unknown'}",
            "status": "error",
            "success": False,
            "results_json": {},
            "vast_phase_final": "failed",
            "output_dir": "",
            "adapter_id": "",
            "error": message,
        }
        canonical = enforce_no_silent_success(normalize_remote_execution_result(raw, request))
        canonical["status"]["state"] = "failed"
        canonical["status"]["terminal"] = True
        canonical["status"]["success"] = False
        canonical["status"]["reason_code"] = canonical["status"].get("reason_code") or "provider_unavailable"
        canonical["status"]["reason_message"] = message
        return {
            "workflow": "protein_ligand_md",
            "execution_mode": f"remote_{provider_name or 'unknown'}",
            "adapter_id": "",
            "status": "error",
            "success": False,
            "job_id": job.job_id,
            "provider": provider_name,
            "provider_job_id": "",
            "route_decision_id": route_decision_id,
            "results_json": {},
            "output_dir": "",
            "execution_result_v1": canonical,
            "terminal_autopsy": {},
            "teardown_proof": {},
            "durability_evidence": {},
            "storage_object_paths": {},
            "cost_telemetry": {},
            "error": message,
        }

    def _estimated_total_cost_usd(self, cfg: Any) -> float:
        for field_name in ("max_total_cost_usd", "estimated_cost_usd"):
            value = getattr(cfg, field_name, None)
            if value is not None:
                return float(value or 0.0)
        return 0.0

    def _looks_like_canonical_result(self, result: Any) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("schema_version") == RESULT_SCHEMA_VERSION
            and isinstance(result.get("status"), dict)
        )

    def _map_summary_state_from_canonical_result(self, result: Dict[str, Any]) -> ComputeJobState:
        status = dict(result.get("status") or {})
        state = str(status.get("state", "") or "").lower()
        success = bool(status.get("success", False))
        if state == "completed" and success:
            return ComputeJobState.COMPLETED
        if state == "running":
            return ComputeJobState.RUNNING
        if state == "queued":
            return ComputeJobState.QUEUED
        if state == "provisioning":
            return ComputeJobState.PROVISIONING
        if state == "cancelled":
            return ComputeJobState.CANCELLED
        return ComputeJobState.FAILED

    def _gpu_type_label(self, cfg: Any) -> str:
        """Render a stable GPU label across legacy and provider-specific MD configs."""
        gpu_type = getattr(cfg, "gpu_type", None)
        if gpu_type is not None:
            return gpu_type.value if hasattr(gpu_type, "value") else str(gpu_type)
        gpu_type_str = getattr(cfg, "gpu_type_str", "")
        return str(gpu_type_str or "")

    def _route_decision_id_for_cfg(self, cfg: Any, job_id: str) -> str:
        route_decision_id = str(getattr(cfg, "route_decision_id", "") or "").strip()
        if route_decision_id:
            return route_decision_id

        route_decision_id = f"route_{job_id}"
        try:
            setattr(cfg, "route_decision_id", route_decision_id)
        except Exception:
            logger.debug("MD config does not allow attaching route_decision_id for %s", job_id)
        return route_decision_id

    # ── Factory ──────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        on_event: Optional[Callable[..., Any]] = None,
    ) -> "UnifiedComputeClient":
        """Build client from environment variables.

        Registers providers that have API keys set:
          VAST_API_KEY  → VastProvider
          RUNPOD_API_KEY → RunPodProvider
        """
        bootstrap_runtime_env()
        cloud = CloudOrchestrator()

        # Vast.ai
        vast_key = os.environ.get("VAST_API_KEY", "")
        if vast_key:
            try:
                from .infrastructure.providers.vast_provider import VastProvider
                cloud.register_provider(VastProvider(api_key=vast_key))
            except Exception as e:
                logger.warning("VastProvider registration failed: %s", e)

        # RunPod
        runpod_key = os.environ.get("RUNPOD_API_KEY", "")
        if runpod_key:
            try:
                from .infrastructure.providers.runpod_pods_provider import RunPodPodsProvider
                cloud.register_provider(RunPodPodsProvider(api_key=runpod_key))
            except Exception as e:
                logger.warning("RunPodPodsProvider registration failed: %s", e)


        # SaladCloud — requires both API key and org name
        salad_key = os.environ.get("SALAD_CLOUD_API_KEY", "").strip().strip('"')
        salad_org = os.environ.get("SALAD_ORG_NAME", "")
        if salad_key and salad_org:
            try:
                from .infrastructure.providers.salad_provider import SaladProvider
                cloud.register_provider(SaladProvider(
                    api_key=salad_key,
                    org_name=salad_org,
                    project_name=os.environ.get("SALAD_PROJECT_NAME", "mica-compute"),
                ))
            except Exception as e:
                logger.warning("SaladProvider registration failed: %s", e)

        return cls(cloud_orchestrator=cloud, on_event=on_event)

    # ── Submit ───────────────────────────────────────────────────

    async def submit_md_job(
        self,
        cfg: Any,
        user_id: str = "",
        preferred_provider: Optional[str] = None,
    ) -> SubmitResult:
        """Submit an MD simulation job.

        Args:
            cfg: MDJobConfig (from vast_md_orchestrator). The
                 ``execution_class`` field is propagated to the economic
                 ledger and Pod API.
            user_id: Owner of the job (for spend tracking / HITL).

        Returns:
            SubmitResult with job_id and provider info.
        """
        job_id = getattr(cfg, "job_id", "") or f"md_{uuid.uuid4().hex[:8]}"
        if not getattr(cfg, "job_id", ""):
            try:
                setattr(cfg, "job_id", job_id)
            except Exception:
                logger.debug("MD config does not allow attaching job_id context for %s", job_id)
        route_decision_id = self._route_decision_id_for_cfg(cfg, job_id)
        if user_id and not getattr(cfg, "_mica_user_id", ""):
            try:
                setattr(cfg, "_mica_user_id", user_id)
            except Exception:
                logger.debug("MD config does not allow attaching user context for %s", job_id)
        if user_id:
            try:
                from mica.ws_md import register_md_job_owner

                register_md_job_owner(job_id, user_id)
            except Exception:
                logger.debug("MD WS owner registry unavailable for job %s", job_id)

        # ── W3-5: Cost ceiling enforcement ───────────────────────
        if user_id:
            spend_info = self.get_user_spend(user_id)
            estimated_cost = self._estimated_total_cost_usd(cfg)
            if spend_info["total_spend_usd"] + estimated_cost > self._cost_ceiling:
                return SubmitResult(
                    job_id="", provider="", instance_id="",
                    accepted=False, error="cost ceiling exceeded",
                )

        provider_candidates = self._provider_order_for_cfg(cfg, preferred_provider=preferred_provider)
        if not provider_candidates:
            return SubmitResult(
                job_id=job_id,
                provider="none",
                accepted=False,
                error="No compute providers registered",
                route_decision_id=route_decision_id,
            )

        provider_name = ""
        orchestrator = None
        last_error: Optional[str] = None
        for candidate_name, candidate_provider in provider_candidates:
            try:
                orchestrator = self._build_md_orchestrator(cfg, candidate_provider)
                provider_name = candidate_name
                break
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    "Skipping compute provider %s for job %s during orchestrator construction: %s",
                    candidate_name,
                    job_id,
                    e,
                )

        if orchestrator is None:
            return SubmitResult(
                job_id=job_id,
                provider="none",
                accepted=False,
                error=last_error or "No compatible compute provider available for this MD job",
                route_decision_id=route_decision_id,
            )

        try:
            estimated_cost = self._estimated_total_cost_usd(cfg)

            # ── W3-2: HITL gate — expensive jobs need approval ───
            if estimated_cost > 10:
                self._summaries[job_id] = ComputeJobSummary(
                    job_id=job_id,
                    state=ComputeJobState.AWAITING_APPROVAL,
                    provider=provider_name,
                    gpu_type=self._gpu_type_label(cfg),
                    execution_class=getattr(cfg, "execution_class", "research"),
                    metadata={"route_decision_id": route_decision_id},
                    user_id=user_id,
                    awaiting_since=datetime.utcnow().isoformat(),
                )
                self._active_jobs[job_id] = orchestrator
                return SubmitResult(
                    job_id=job_id,
                    provider=provider_name,
                    route_decision_id=route_decision_id,
                )

            # ── W3-1: Enqueue for throttled execution ────────────
            self._summaries[job_id] = ComputeJobSummary(
                job_id=job_id,
                state=ComputeJobState.QUEUED,
                provider=provider_name,
                gpu_type=self._gpu_type_label(cfg),
                execution_class=getattr(cfg, "execution_class", "research"),
                metadata={"route_decision_id": route_decision_id},
                user_id=user_id,
            )
            self._active_jobs[job_id] = orchestrator
            await self._job_queue.put((job_id, orchestrator))
            self._ensure_drainer()
            return SubmitResult(
                job_id=job_id,
                provider=provider_name,
                route_decision_id=route_decision_id,
            )

        except Exception as e:
            logger.error("Failed to submit MD job %s: %s", job_id, e)
            return SubmitResult(
                job_id=job_id,
                provider=provider_name,
                accepted=False,
                error=str(e),
                route_decision_id=route_decision_id,
            )

    # ── W3-1: Queue drainer ──────────────────────────────────────

    def _ensure_drainer(self) -> None:
        """Start queue workers up to max_concurrent_jobs."""
        self._queue_drainer_tasks = [task for task in self._queue_drainer_tasks if not task.done()]
        for _ in range(self._max_concurrent_jobs - len(self._queue_drainer_tasks)):
            self._queue_drainer_tasks.append(asyncio.create_task(self._drain_queue()))

    async def _drain_queue(self) -> None:
        """Run queued jobs under the configured fixed worker-pool limit."""
        while True:
            job_id, orchestrator = await self._job_queue.get()
            try:
                await self._run_orchestrator(job_id, orchestrator)
            finally:
                self._job_queue.task_done()

    # ── W3-2: HITL approve / reject ──────────────────────────────

    async def approve_job(self, job_id: str) -> bool:
        """Approve an AWAITING_APPROVAL job → QUEUED and enqueue it."""
        summary = self._summaries.get(job_id)
        if summary is None or summary.state != ComputeJobState.AWAITING_APPROVAL:
            return False
        summary.state = ComputeJobState.QUEUED
        summary.awaiting_since = None
        orchestrator = self._active_jobs.get(job_id)
        if orchestrator is not None:
            await self._job_queue.put((job_id, orchestrator))
            self._ensure_drainer()
        return True

    async def reject_job(self, job_id: str, reason: str = "") -> bool:
        """Reject an AWAITING_APPROVAL job."""
        summary = self._summaries.get(job_id)
        if summary is None or summary.state != ComputeJobState.AWAITING_APPROVAL:
            return False
        summary.state = ComputeJobState.REJECTED
        summary.error = reason or "Rejected by operator"
        summary.awaiting_since = None
        self._active_jobs.pop(job_id, None)
        return True

    async def _run_orchestrator(self, job_id: str, orchestrator: Any) -> None:
        """Background task: run the full orchestrator lifecycle."""
        summary = self._summaries.get(job_id)
        try:
            if summary:
                summary.state = ComputeJobState.PROVISIONING
            result = await orchestrator.run()
            if summary:
                state = getattr(orchestrator, "state", None)
                canonical_result = result if self._looks_like_canonical_result(result) else {}
                if canonical_result:
                    # P1 Fix: Enforce no-silent-success gate before accepting success
                    from mica.drivers.md_execution_contract import enforce_no_silent_success
                    canonical_result = enforce_no_silent_success(canonical_result)
                    
                    summary.metadata[RESULT_SCHEMA_VERSION] = canonical_result
                    summary.metadata["canonical_status"] = str(
                        canonical_result.get("status", {}).get("state", "") or ""
                    )
                    summary.phase = str(canonical_result.get("status", {}).get("phase", "") or "")
                    summary.state = self._map_summary_state_from_canonical_result(canonical_result)
                    summary.error = str(canonical_result.get("status", {}).get("reason_message", "") or "") or None
                else:
                    # No canonical result → mark failed (P1 no-silent-success enforcement)
                    summary.state = ComputeJobState.FAILED
                    summary.error = "No canonical execution result returned from orchestrator"
                summary.instance_id = getattr(state, "instance_id", "") or summary.instance_id
                summary.total_cost_usd = getattr(state, "total_cost_usd", 0.0) or summary.total_cost_usd
        except asyncio.CancelledError:
            if summary:
                summary.state = ComputeJobState.CANCELLED
        except Exception as e:
            logger.error("Orchestrator for %s failed: %s", job_id, e)
            if summary:
                summary.state = ComputeJobState.FAILED
                summary.error = str(e)
        finally:
            self._active_jobs.pop(job_id, None)

    # ── Query ────────────────────────────────────────────────────

    async def _load_salad_worker_result(
        self,
        *,
        user_id: str,
        output_gcs_prefix: str,
        job_id: str,
    ) -> Dict[str, Any]:
        """Load the worker-side result JSON for a Salad job if it exists."""
        try:
            from .storage.gcs_user_storage import get_storage_manager

            prefix_path = str(output_gcs_prefix or "")
            if prefix_path.startswith("gs://"):
                parts = prefix_path[5:].split("/", 1)
                prefix_path = parts[1] if len(parts) > 1 else ""
            prefix_path = prefix_path.rstrip("/")
            if not prefix_path:
                return {}

            payload = get_storage_manager().read_text_best_effort(
                user_id=user_id,
                object_path=f"{prefix_path}/output/{job_id}_results.json",
                max_chars=400_000,
            )
            text = str(payload.get("text") or "")
            if not text:
                return {}
            decoded = json.loads(text)
            return decoded if isinstance(decoded, dict) else {}
        except Exception as exc:
            logger.debug(
                "Salad worker result probe failed for %s (non-fatal): %s",
                job_id,
                exc,
            )
            return {}

    async def _rehydrate_salad_summary_from_remote_truth(
        self,
        *,
        job_id: str,
        user_id: str,
        existing_summary: Optional[ComputeJobSummary] = None,
    ) -> Optional[ComputeJobSummary]:
        """Reconstruct a Salad summary from deterministic GCS/provider truth."""
        if not user_id or not job_id.startswith("salad_"):
            return None

        from .storage.compute_durability import (
            canonical_compute_storage_prefix,
            compute_user_bucket_name,
        )
        from .infrastructure.providers.salad_provider import _make_cg_name

        bucket_name = compute_user_bucket_name(user_id)
        storage_prefix = canonical_compute_storage_prefix(lane="remote_md", job_id=job_id)
        canonical_output_gcs_prefix = f"gs://{bucket_name}/{storage_prefix}"
        rehydrate_started_at = time.time()
        summary = existing_summary or ComputeJobSummary(
            job_id=job_id,
            state=ComputeJobState.QUEUED,
            provider="salad",
            user_id=user_id,
        )
        summary.provider = summary.provider or "salad"
        summary.user_id = user_id
        summary.instance_id = summary.instance_id or _make_cg_name(job_id)

        provider = self._cloud.providers.get("salad")
        instance = None
        if provider is not None:
            try:
                instance = await provider.get_instance_status(summary.instance_id)
            except Exception as exc:
                logger.debug(
                    "Salad provider status probe failed for %s (non-fatal): %s",
                    job_id,
                    exc,
                )

        provider_output_gcs_prefix = ""
        if instance is not None:
            provider_output_gcs_prefix = str(getattr(instance, "raw_data", {}).get("output_gcs_prefix", "") or "")
        if provider_output_gcs_prefix and not provider_output_gcs_prefix.startswith("gs://"):
            provider_output_gcs_prefix = f"gs://{bucket_name}/{provider_output_gcs_prefix.lstrip('/')}"
        existing_output_gcs_prefix = str(summary.metadata.get("output_gcs_prefix", "") or "")
        output_gcs_prefix = provider_output_gcs_prefix or existing_output_gcs_prefix or canonical_output_gcs_prefix
        summary.metadata["output_gcs_prefix"] = output_gcs_prefix

        artifact_manifest = await self._probe_salad_artifact_manifest(
            user_id=user_id,
            output_gcs_prefix=output_gcs_prefix,
            job_id=job_id,
        )

        worker_result = await self._load_salad_worker_result(
            user_id=user_id,
            output_gcs_prefix=output_gcs_prefix,
            job_id=job_id,
        )
        canonical_result = dict(
            worker_result.get("execution_result_v1")
            or worker_result.get(RESULT_SCHEMA_VERSION)
            or {}
        )
        if canonical_result:
            summary.metadata[RESULT_SCHEMA_VERSION] = canonical_result
            summary.metadata["canonical_status"] = str(
                canonical_result.get("status", {}).get("state", "") or ""
            )
            summary.phase = str(canonical_result.get("status", {}).get("phase", "") or "")
            summary.state = self._map_summary_state_from_canonical_result(canonical_result)
            summary.error = (
                str(canonical_result.get("status", {}).get("reason_message", "") or "")
                or None
            )

        smic_post_analysis = await self._ensure_salad_terminal_smic_post_analysis(
            user_id=user_id,
            job_id=job_id,
            bucket_name=bucket_name,
            output_gcs_prefix=output_gcs_prefix,
            artifact_manifest=artifact_manifest,
            context={},
            execution_request=None,
            existing_summary=existing_summary,
            canonical_result=canonical_result,
        )
        if smic_post_analysis:
            artifact_manifest["smic_post_analysis"] = dict(smic_post_analysis)
            if canonical_result:
                canonical_result.setdefault("artifacts", {})
                canonical_result["artifacts"]["smic_post_analysis"] = dict(smic_post_analysis)
                summary.metadata[RESULT_SCHEMA_VERSION] = canonical_result

        if instance is not None:
            summary.instance_id = getattr(instance, "instance_id", "") or summary.instance_id
            summary.total_cost_usd = getattr(instance, "total_cost_usd", 0.0) or summary.total_cost_usd
            gpu_type = getattr(instance, "gpu_type", None)
            if gpu_type is not None:
                summary.gpu_type = gpu_type.value if hasattr(gpu_type, "value") else str(gpu_type)
            if not canonical_result:
                provider_state = getattr(getattr(instance, "status", None), "value", getattr(instance, "status", ""))
                provider_state = str(provider_state or "").lower()
                if provider_state in ("pending", "provisioning"):
                    summary.state = ComputeJobState.PROVISIONING
                elif provider_state in ("running",):
                    summary.state = ComputeJobState.RUNNING
                elif provider_state in ("stopping", "stopped", "terminated"):
                    if artifact_manifest.get("completed_marker_confirmed"):
                        summary.state = ComputeJobState.COMPLETED
                        summary.phase = summary.phase or "production"
                    elif artifact_manifest.get("failure_receipt_present"):
                        summary.state = ComputeJobState.FAILED

        if not canonical_result and artifact_manifest.get("completed_marker_confirmed"):
            summary.state = ComputeJobState.COMPLETED
            summary.phase = summary.phase or "production"

        if summary.state == ComputeJobState.RUNNING:
            live_runtime_receipts = await self._maybe_publish_live_salad_runtime_events(
                user_id=user_id,
                job_id=job_id,
                output_gcs_prefix=output_gcs_prefix,
                artifact_manifest=artifact_manifest,
                summary=summary,
            )
            if live_runtime_receipts:
                summary.metadata["live_runtime_receipts"] = dict(live_runtime_receipts)

        if instance is None and not canonical_result and not artifact_manifest.get("object_listing"):
            return None

        live_smic_metrics: Dict[str, Any] = {}
        if summary.state == ComputeJobState.RUNNING:
            live_smic_metrics = await self._maybe_publish_live_salad_smic_metrics(
                user_id=user_id,
                job_id=job_id,
                bucket_name=bucket_name,
                output_gcs_prefix=output_gcs_prefix,
                artifact_manifest=artifact_manifest,
                summary=summary,
            )
            if live_smic_metrics:
                summary.metadata["live_smic_metrics"] = dict(live_smic_metrics)

        smic_receipt_summary = dict(smic_post_analysis or {})
        if live_smic_metrics:
            smic_receipt_summary["live_metric_receipts"] = dict(live_smic_metrics)
            smic_receipt_summary.setdefault("status", "completed" if live_smic_metrics.get("last_metric_keys") else "degraded")

        runtime_receipts = await self._persist_salad_runtime_receipts(
            user_id=user_id,
            job_id=job_id,
            summary=summary,
            output_gcs_prefix=output_gcs_prefix,
            artifact_manifest=artifact_manifest,
            canonical_result=canonical_result,
            smic_summary=smic_receipt_summary,
        )
        if runtime_receipts:
            summary.metadata["runtime_receipts"] = dict(runtime_receipts)
        if artifact_manifest.get("object_listing") or artifact_manifest.get("smic_post_analysis"):
            summary.metadata["artifact_manifest"] = artifact_manifest
        summary.metadata["last_remote_refresh_ts"] = rehydrate_started_at

        return summary

    async def get_job_status(self, job_id: str, user_id: str = "") -> Optional[ComputeJobSummary]:
        """Return current job summary, refreshing from live or rehydratable truth."""
        summary = self._summaries.get(job_id)
        orch = self._active_jobs.get(job_id)

        if user_id and orch is None and job_id.startswith("salad_"):
            needs_remote_rehydrate = summary is None or RESULT_SCHEMA_VERSION not in summary.metadata
            if needs_remote_rehydrate:
                rehydrated = await self._rehydrate_salad_summary_from_remote_truth(
                    job_id=job_id,
                    user_id=user_id,
                    existing_summary=summary,
                )
                if rehydrated is not None:
                    summary = rehydrated
                    self._summaries[job_id] = summary

        if summary is None:
            return None

        if user_id and not summary.user_id:
            summary.user_id = user_id

        if orch is not None and hasattr(orch, "state"):
            state = orch.state
            if state is not None:
                summary.instance_id = getattr(state, "instance_id", "") or ""
                phase = getattr(state, "phase", None)
                summary.phase = phase.value if hasattr(phase, "value") else str(phase or "")
                summary.total_cost_usd = getattr(state, "total_cost_usd", 0.0)
                if hasattr(state, "started_at") and state.started_at:
                    started_at = state.started_at
                    # Normalize tz awareness to avoid naive/aware subtraction errors.
                    if getattr(started_at, "tzinfo", None) is None:
                        now = datetime.utcnow()
                    else:
                        now = datetime.now(timezone.utc)
                    summary.elapsed_seconds = (now - started_at).total_seconds()
                # Map orchestrator phase to high-level state
                phase_val = summary.phase.lower()
                if phase_val in ("provisioning", "installing"):
                    summary.state = ComputeJobState.PROVISIONING
                elif phase_val in ("running", "monitoring", "downloading"):
                    summary.state = ComputeJobState.RUNNING

        if user_id and job_id.startswith("salad_"):
            needs_live_salad_refresh = self._should_refresh_salad_remote_truth(summary)
            if needs_live_salad_refresh:
                rehydrated = await self._rehydrate_salad_summary_from_remote_truth(
                    job_id=job_id,
                    user_id=user_id,
                    existing_summary=summary,
                )
                if rehydrated is not None:
                    summary = rehydrated
                    self._summaries[job_id] = summary

        return summary

    def list_jobs(self) -> List[ComputeJobSummary]:
        """Return all tracked job summaries."""
        return list(self._summaries.values())

    # ── Control ──────────────────────────────────────────────────

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job. Returns True if cancellation was initiated."""
        orch = self._active_jobs.get(job_id)
        if orch is None:
            return False

        try:
            if hasattr(orch, "request_stop"):
                orch.request_stop(reason="unified_client_cancel")
            elif hasattr(orch, "_perform_safe_stop"):
                await orch._perform_safe_stop()
            summary = self._summaries.get(job_id)
            if summary:
                summary.state = ComputeJobState.CANCELLED
            return True
        except Exception as e:
            logger.error("Cancel failed for %s: %s", job_id, e)
            return False

    # ── Economic Ledger ──────────────────────────────────────────

    def get_economic_ledger(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return the economic ledger for a job if available."""
        orch = self._active_jobs.get(job_id)
        if orch is not None and hasattr(orch, "_build_economic_ledger"):
            return orch._build_economic_ledger()
        # Fall back to cached summary
        summary = self._summaries.get(job_id)
        if summary:
            return {
                "job_id": summary.job_id,
                "provider": summary.provider,
                "gpu_type": summary.gpu_type,
                "execution_class": summary.execution_class,
                "total_cost_usd": summary.total_cost_usd,
                "elapsed_seconds": summary.elapsed_seconds,
                "state": summary.state.value,
            }
        return None

    def get_all_economic_ledgers(self) -> List[Dict[str, Any]]:
        """Return ledgers for all tracked jobs."""
        return [
            ledger
            for jid in self._summaries
            if (ledger := self.get_economic_ledger(jid)) is not None
        ]

    # ── W3-4: User spend tracking ────────────────────────────────

    def get_user_spend(self, user_id: str) -> Dict[str, Any]:
        """Return aggregate spend for a specific user."""
        total = 0.0
        count = 0
        for s in self._summaries.values():
            if s.user_id == user_id:
                total += s.total_cost_usd
                count += 1
        return {"user_id": user_id, "total_spend_usd": total, "job_count": count}

    # ── Provider Health ──────────────────────────────────────────

    async def health_check(self) -> Dict[str, bool]:
        """Check health of all registered compute providers."""
        return await self._cloud.health_check_all()

    @property
    def providers(self) -> List[str]:
        """List registered provider names."""
        return list(self._cloud.providers.keys())

    # ── Inline MD execution (agent-driven) ───────────────────────

    async def run_md_inline(
        self,
        pdb_path: str,
        user_id: str,
        context: Optional[Dict[str, Any]] = None,
        execution_request: Optional[Dict[str, Any]] = None,
        on_event: Optional[Callable[..., Any]] = None,
    ) -> Dict[str, Any]:
        """Run MD inline and return a fully normalized result dict.

        This is the single-surface entrypoint for agent-driven MD execution.
        Provider selection, PDB staging, orchestration, artifact probing,
        normalization, and teardown proof are all handled internally.

        Args:
            pdb_path: Local PDB path or gs:// URI (staged to GCS if local + Salad).
            user_id: Owner for GCS bucket and cost tracking.
            context: Execution hints (execution_backend, steps, gpu_type, etc.).
            execution_request: Pre-built execution_request_v1 dict (optional).
            on_event: Optional callback for phase events.

        Returns:
            Dict with workflow, execution_mode, status, success, job_id,
            output_gcs_prefix, artifact_manifest, teardown_proof,
            execution_result_v1.  If provider handling is not available
            inline, returns {"_delegate_to_caller": True}.
        """
        ctx = dict(context or {})
        execution_backend = str(ctx.get("execution_backend") or "").lower().strip()

        # Auto-detect from registered providers if not explicit
        if not execution_backend and "salad" in self._cloud.providers:
            execution_backend = "salad"

        if execution_backend == "salad":
            if "salad" not in self._cloud.providers:
                return {
                    "workflow": "protein_ligand_md",
                    "execution_mode": "remote_salad",
                    "status": "error",
                    "success": False,
                    "error": "Salad provider not registered. Set SALAD_CLOUD_API_KEY + SALAD_ORG_NAME.",
                }
            return await self._run_md_inline_salad(
                pdb_path=pdb_path,
                user_id=user_id,
                context=ctx,
                execution_request=execution_request or {},
                on_event=on_event,
            )

        # No inline handler for this backend — delegate to caller's inline path
        return {
            "_delegate_to_caller": True,
            "execution_backend": execution_backend,
            "reason": f"No inline handler for provider '{execution_backend}' — use caller inline path",
        }

    async def _run_md_inline_salad(
        self,
        pdb_path: str,
        user_id: str,
        context: Dict[str, Any],
        execution_request: Dict[str, Any],
        on_event: Optional[Callable[..., Any]],
    ) -> Dict[str, Any]:
        """Full Salad SRCG inline execution: stage PDB, run orchestrator, normalize result."""
        import os as _os
        import base64 as _base64
        import time as _time

        scientific = dict(execution_request.get("scientific") or {})
        template_ref = dict(execution_request.get("job", {}).get("template_ref") or {})
        ligand_smiles = str(
            scientific.get("ligand_smiles") or context.get("ligand_smiles") or ""
        ).strip()
        docked_ligand_pdb = str(
            scientific.get("docked_ligand_pdb") or context.get("docked_ligand_pdb") or ""
        ).strip()
        simulation_mode = str(
            scientific.get("simulation_mode") or context.get("simulation_mode") or ""
        ).strip()
        template_id = str(template_ref.get("template_id") or "").strip()
        complex_stability_requested = bool(
            ligand_smiles
            or docked_ligand_pdb
            or simulation_mode == "complex_stability"
            or template_id == "complex_stability_v1"
        )
        if complex_stability_requested and (not ligand_smiles or not docked_ligand_pdb):
            return {
                "workflow": str(
                    execution_request.get("job", {}).get("workflow") or "protein_ligand_md"
                ),
                "execution_mode": "remote_salad",
                "adapter_id": "salad_gcs_adapter",
                "status": "error",
                "success": False,
                "error": (
                    "Salad inline complex_stability requires both ligand_smiles and "
                    "docked_ligand_pdb before staging the canonical worker inputs."
                ),
            }

        job_id = str(
            context.get("job_id")
            or execution_request.get("job", {}).get("job_id", "")
            or f"salad_{int(_time.time())}"
        )

        try:
            from .infrastructure.orchestration.salad_gcs_orchestrator import (
                SaladGCSOrchestrator,
                SaladMDJobConfig,
                _parse_gcs_uri,
            )
            from .infrastructure.orchestration.salad_bootstrap_policy import (
                validate_same_class_gpu_policy,
            )
        except ImportError as exc:
            logger.warning("SaladGCSOrchestrator unavailable: %s", exc)
            return {
                "_delegate_to_caller": True,
                "reason": f"SaladGCSOrchestrator import failed: {exc}",
            }

        from .storage.compute_durability import (
            compute_user_bucket_name,
            canonical_compute_storage_prefix,
            build_compute_teardown_proof,
        )
        from .drivers.md_execution_contract import (
            normalize_salad_execution_result,
            enforce_no_silent_success,
        )

        runtime_context = dict(execution_request.get("runtime") or {})
        metadata_context = dict(execution_request.get("metadata") or {})
        effective_salad_gpu = str(
            context.get("salad_gpu_type")
            or runtime_context.get("salad_gpu_type")
            or metadata_context.get("salad_gpu_type")
            or context.get("gpu_type")
            or runtime_context.get("gpu_type")
            or "RTX_5090"
        ).strip() or "RTX_5090"
        degraded_smoke_fallback = bool(
            context.get("degraded_smoke_fallback")
            or runtime_context.get("degraded_smoke_fallback")
            or metadata_context.get("degraded_smoke_fallback")
        )
        gpu_policy_decision = validate_same_class_gpu_policy(
            "RTX_5090",
            effective_salad_gpu,
            degraded_smoke_fallback=degraded_smoke_fallback,
        )
        if not gpu_policy_decision.get("allowed"):
            return {
                "workflow": "protein_ligand_md",
                "execution_mode": "remote_salad",
                "adapter_id": "salad_gcs_adapter",
                "status": "error",
                "success": False,
                "error": str(gpu_policy_decision.get("reason")),
                "salad_gpu_policy": gpu_policy_decision,
            }

        # 1. Stage PDB to GCS
        try:
            pdb_gcs_path = await self._stage_pdb_to_gcs(
                user_id=user_id, pdb_path=pdb_path, job_id=job_id
            )
            docked_gcs_path = ""
            if complex_stability_requested:
                docked_gcs_path = await self._stage_input_file_to_gcs(
                    user_id=user_id,
                    local_path=docked_ligand_pdb,
                    job_id=job_id,
                    object_name="docked_ligand.pdb",
                    content_type="chemical/x-pdb",
                    sanitize_pdb=False,
                )
        except Exception as exc:
            return {
                "workflow": "protein_ligand_md",
                "execution_mode": "remote_salad",
                "status": "error",
                "success": False,
                "error": f"PDB staging failed: {exc}",
            }

        # 2. Build output paths
        bucket_name = compute_user_bucket_name(user_id)
        storage_prefix = canonical_compute_storage_prefix(lane="remote_md", job_id=job_id)
        output_gcs_prefix = f"gs://{bucket_name}/{storage_prefix}"

        # 3. Resolve GCS credentials
        creds_b64 = _os.getenv("SALAD_GCS_CREDENTIALS_B64") or context.get("gcs_credentials_b64")
        if not creds_b64:
            creds_path = _os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
            if creds_path:
                try:
                    import pathlib as _pl
                    creds_b64 = _base64.b64encode(_pl.Path(creds_path).read_bytes()).decode("utf-8")
                except Exception:
                    pass

        # 4. Build SaladMDJobConfig
        cfg_kwargs: Dict[str, Any] = {
            "job_id": job_id,
            "pdb_gcs_path": pdb_gcs_path,
            "output_gcs_prefix": output_gcs_prefix,
            "max_steps": int(context.get("steps", 75_000_000)),
            "gpu_type_str": str(gpu_policy_decision.get("effective_gpu_type") or "RTX_5090"),
            "route_decision_id": str(
                context.get("route_decision_id")
                or runtime_context.get("route_decision_id")
                or metadata_context.get("route_decision_id")
                or execution_request.get("job", {}).get("route_decision_id")
                or ""
            ),
            "docker_image_size_gb": float(
                context.get("salad_docker_image_size_gb")
                or runtime_context.get("salad_docker_image_size_gb")
                or 8.0
            ),
            "degraded_smoke_fallback": degraded_smoke_fallback,
            "pre_destroy_inspection_enabled": True,
            "preserve_failed_cg_on_missing_bootstrap_evidence": True,
        }
        for key in (
            "benchmark_steps",
            "report_freq",
            "saving_interval_seconds",
            "max_no_response_time",
            "max_same_class_reallocation_attempts",
        ):
            if context.get(key) is not None:
                cfg_kwargs[key] = int(context[key])
        docker_image = str(
            context.get("salad_docker_image")
            or context.get("mica_md_docker_image")
            or _os.getenv("MICA_MD_DOCKER_IMAGE", "")
            or _os.getenv("SALAD_GCS_DOCKER_IMAGE", "")
        )
        docker_command = str(
            context.get("salad_docker_command")
            or _os.getenv("MICA_SALAD_WORKER_COMMAND", "")
        )
        if creds_b64:
            cfg_kwargs["gcs_credentials_b64"] = creds_b64
        if docker_image:
            cfg_kwargs["docker_image"] = docker_image
        if docker_command:
            cfg_kwargs["docker_command"] = docker_command
        env_extra = dict(context.get("salad_env_extra") or {})
        if complex_stability_requested:
            docked_bucket, docked_object = _parse_gcs_uri(docked_gcs_path)
            env_extra.update(
                {
                    "SIMULATION_MODE": "complex_stability",
                    "LIGAND_SMILES": ligand_smiles,
                    "DOCKED_LIGAND_GCS_BUCKET": docked_bucket,
                    "DOCKED_LIGAND_GCS_OBJECT": docked_object,
                    "PRODUCTION_NS": str(
                        float(
                            scientific.get("production_ns")
                            or context.get("production_ns")
                            or 100.0
                        )
                    ),
                }
            )
        if env_extra:
            cfg_kwargs["env_extra"] = env_extra

        job_cfg = SaladMDJobConfig(**cfg_kwargs)
        provider = self._cloud.providers["salad"]

        def _emit_salad_event(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
            if not callable(on_event):
                return
            payload_dict = dict(payload or {})
            patch: Dict[str, Any] = {
                "status": "running",
                "provider": "salad",
                "job_id": job_id,
                "output_gcs_prefix": output_gcs_prefix,
                "vast_phase": event_type,
                "last_orchestrator_state": event_type,
                "last_event_message": event_type,
            }
            cg_name = str(payload_dict.get("cg_name") or payload_dict.get("instance_id") or "").strip()
            if cg_name:
                patch["instance_id"] = cg_name
            provider_status = str(payload_dict.get("status") or "").strip()
            if provider_status:
                patch["last_provider_status"] = provider_status
                patch["last_event_message"] = f"{event_type}:{provider_status}"
            poll = payload_dict.get("poll")
            if poll is not None:
                patch["last_poll"] = poll
            if payload_dict:
                patch["salad_event_payload"] = payload_dict
            try:
                on_event(patch)
            except Exception:
                logger.debug("Salad on_event bridge failed", exc_info=True)

        orchestrator = SaladGCSOrchestrator(
            config=job_cfg,
            provider=provider,
            on_event=_emit_salad_event,
        )

        _emit_salad_event("submission_started")

        # 5. Run orchestrator
        salad_raw = await orchestrator.run()
        salad_raw.setdefault("job_id", job_id)
        salad_raw.setdefault("output_gcs_prefix", output_gcs_prefix)
        salad_raw.setdefault("salad_gpu_policy", gpu_policy_decision)

        # 6. Probe artifact manifest
        artifact_manifest = await self._probe_salad_artifact_manifest(
            user_id, output_gcs_prefix, job_id
        )
        artifact_manifest["smic_post_analysis"] = await self._ensure_salad_terminal_smic_post_analysis(
            user_id=user_id,
            job_id=job_id,
            bucket_name=bucket_name,
            output_gcs_prefix=output_gcs_prefix,
            artifact_manifest=artifact_manifest,
            context=context,
            execution_request=execution_request,
            existing_summary=None,
            canonical_result=None,
        )
        salad_raw["artifact_manifest"] = artifact_manifest

        # 7. Normalize + enforce no-silent-success
        execution_result_v1 = normalize_salad_execution_result(salad_raw, execution_request)
        execution_result_v1 = enforce_no_silent_success(execution_result_v1)
        execution_result_v1.setdefault("artifacts", {})
        execution_result_v1["artifacts"]["smic_post_analysis"] = dict(
            artifact_manifest.get("smic_post_analysis") or {}
        )

        # 8. Build teardown proof (serverless — container self-terminates)
        teardown_proof = build_compute_teardown_proof(
            execution_id=job_id,
            lane="serverless",
            user_id=user_id,
            job_id=job_id,
            provider="salad",
            provider_target=str(salad_raw.get("cg_name", "")),
            provider_job_id=str(salad_raw.get("job_id", job_id)),
            storage_bucket=bucket_name,
            storage_prefix=storage_prefix,
            destroy_attempted=False,
            destroy_succeeded=False,
            destroy_skipped_reason="salad_srcg_self_terminates",
        )

        return {
            "workflow": "protein_ligand_md",
            "execution_mode": "remote_salad",
            "adapter_id": "salad_gcs_adapter",
            "status": salad_raw.get("status", "unknown"),
            "success": salad_raw.get("status") == "completed",
            "job_id": job_id,
            "cg_name": salad_raw.get("cg_name", ""),
            "output_gcs_prefix": output_gcs_prefix,
            "elapsed_seconds": float(salad_raw.get("elapsed_seconds", 0.0)),
            "artifact_manifest": artifact_manifest,
            "smic_post_analysis": dict(artifact_manifest.get("smic_post_analysis") or {}),
            "salad_gpu_policy": gpu_policy_decision,
            "provider_bootstrap_receipts": dict(salad_raw.get("provider_bootstrap_receipts") or {}),
            "teardown_proof": teardown_proof,
            "execution_result_v1": execution_result_v1,
        }

    @staticmethod
    def _resolve_terminal_smic_bundle_analyses(
        context: Dict[str, Any],
        execution_request: Optional[Dict[str, Any]],
    ) -> List[str]:
        candidates = [
            context.get("smic_bundle_analyses"),
            context.get("post_analysis_bundle"),
            ((execution_request or {}).get("runtime") or {}).get("smic_bundle_analyses"),
            ((execution_request or {}).get("metadata") or {}).get("smic_bundle_analyses"),
            os.getenv("MICA_TERMINAL_SMIC_ANALYSES", ""),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return [part.strip().lower() for part in candidate.split(",") if part.strip()]
            if isinstance(candidate, (list, tuple)):
                values = [str(item).strip().lower() for item in candidate if str(item or "").strip()]
                if values:
                    return values
        return list(_DEFAULT_TERMINAL_SMIC_ANALYSES)

    @staticmethod
    def _filter_terminal_smic_analyses_for_topology(
        analyses: List[str],
        topology_path: Path,
        trajectory_path: Optional[Path] = None,
    ) -> tuple[List[str], Dict[str, str]]:
        resolved = [str(item or "").strip().lower() for item in analyses if str(item or "").strip()]
        skipped: Dict[str, str] = {}

        try:
            import mdtraj as md

            top = md.load_topology(str(topology_path))
            aa3 = {
                "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
                "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
                "ASX", "GLX", "SEC", "PYL",
            }
            solvent_like = {
                "HOH", "WAT", "TIP3", "SOL", "H2O",
                "NA", "NA+", "CL", "CL-", "K", "K+", "CA", "CA2+", "MG", "MG2+",
            }

            def _is_protein_residue(residue: Any) -> bool:
                name = (getattr(residue, "name", "") or "").strip().upper()
                if not name or name in solvent_like:
                    return False
                flag = getattr(residue, "is_protein", None)
                if flag is not None:
                    return bool(flag)
                return name in aa3

            chain_count = sum(
                1 for chain in top.chains
                if any(_is_protein_residue(residue) for residue in chain.residues)
            )
            topology_atoms = int(getattr(top, "n_atoms", 0) or 0)
        except Exception as exc:
            logger.debug("Terminal SMIC topology applicability probe failed for %s: %s", topology_path, exc)
            return resolved, skipped

        if trajectory_path is not None and any(name in {"rmsd", "rmsf"} for name in resolved):
            try:
                import mdtraj as md

                with md.open(str(trajectory_path)) as handle:
                    xyz, _cell_lengths, _cell_angles = handle.read(n_frames=1)
                    shape = getattr(xyz, "shape", ())
                    trajectory_atoms = int(shape[1]) if len(shape) >= 2 else 0
                if topology_atoms and trajectory_atoms and topology_atoms != trajectory_atoms:
                    blocker = f"smic_dynamic_metric_blocker:topology_atoms={topology_atoms}:trajectory_atoms={trajectory_atoms}"
                    resolved = [
                        name for name in resolved
                        if name not in {"rmsd", "rmsf"}
                    ]
                    if "rmsd" in analyses:
                        skipped["rmsd"] = blocker
                    if "rmsf" in analyses:
                        skipped["rmsf"] = blocker
            except Exception as exc:
                logger.debug(
                    "Terminal SMIC trajectory compatibility probe failed for %s / %s: %s",
                    topology_path,
                    trajectory_path,
                    exc,
                )

        if "contacts" in resolved and chain_count < 2:
            resolved = [name for name in resolved if name != "contacts"]
            skipped["contacts"] = "inapplicable_single_chain_topology"
        return resolved, skipped

    @staticmethod
    def _should_refresh_salad_remote_truth(summary: ComputeJobSummary) -> bool:
        metadata = dict(summary.metadata or {})
        last_refresh_ts = metadata.get("last_remote_refresh_ts")
        try:
            last_refresh = float(last_refresh_ts)
        except (TypeError, ValueError):
            return True

        now = time.time()
        state = summary.state
        if state in {ComputeJobState.QUEUED, ComputeJobState.PROVISIONING}:
            return (now - last_refresh) >= _SALAD_REMOTE_REFRESH_PENDING_SECONDS
        if state == ComputeJobState.RUNNING:
            return (now - last_refresh) >= _SALAD_REMOTE_REFRESH_RUNNING_SECONDS
        return False

    @staticmethod
    def _select_salad_terminal_smic_inputs(object_listing: List[Dict[str, Any]]) -> Dict[str, str]:
        names = [str(item.get("name") or "").strip() for item in object_listing if str(item.get("name") or "").strip()]
        topology_candidates = [
            name for name in names
            if name.endswith("prepared_topology.pdb")
            or name.endswith("_prepared.pdb")
            or name.endswith("_equilibrated.pdb")
            or name.endswith(".pdb")
        ]
        topology_candidates.sort(
            key=lambda name: (
                0 if name.endswith("prepared_topology.pdb") else
                1 if name.endswith("_prepared.pdb") else
                2 if name.endswith("_equilibrated.pdb") else
                3 if "/output/" in name else
                4,
                name,
            )
        )
        trajectory_candidates = sorted([name for name in names if name.endswith(".dcd")])
        topology_object = topology_candidates[0] if topology_candidates else ""
        trajectory_object = trajectory_candidates[0] if trajectory_candidates else ""
        return {
            "topology_object": topology_object,
            "trajectory_object": trajectory_object,
        }

    @staticmethod
    def _infer_route_decision_id(job_id: str, summary: Optional[ComputeJobSummary]) -> tuple[str, str]:
        metadata = dict((summary.metadata if summary is not None else {}) or {})
        route_decision_id = str(metadata.get("route_decision_id", "") or "").strip()
        if route_decision_id:
            return route_decision_id, "summary_metadata"
        return f"route_{job_id}", "inferred_conventional_route_id"
    @staticmethod
    def _salad_common_event_identity(
        *,
        summary: Optional[ComputeJobSummary],
        output_gcs_prefix: str,
        route_decision_id: str,
        live_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metadata = dict((summary.metadata if summary is not None else {}) or {})
        live = dict(live_status or {})
        salad_policy = dict(metadata.get("salad_gpu_policy") or {})

        def _first(*values: Any) -> str:
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        return {
            "route_decision_id": route_decision_id,
            "provider": "salad",
            "provider_instance_id": _first(live.get("provider_instance_id"), live.get("container_group_id"), getattr(summary, "instance_id", "")),
            "gcs_prefix": output_gcs_prefix,
            "requested_gpu_type": _first(live.get("requested_gpu_type"), metadata.get("requested_gpu_type"), salad_policy.get("requested_gpu_type")),
            "actual_gpu_type": _first(live.get("actual_gpu_type"), metadata.get("actual_gpu_type"), getattr(summary, "gpu_type", "")),
            "image_digest": _first(live.get("image_digest"), metadata.get("image_digest"), metadata.get("docker_image_digest")),
            "source_target_id": _first(live.get("source_target_id"), metadata.get("source_target_id"), metadata.get("target_id")),
            "allocation_attempt": int(live.get("allocation_attempt") or metadata.get("allocation_attempt") or 0),
            "protocol_node_id": _first(live.get("protocol_node_id"), metadata.get("protocol_node_id")),
            "session_id": _first(live.get("session_id"), metadata.get("session_id")),
        }

    @staticmethod
    def _artifact_durability_class(object_path: str) -> str:
        name = str(object_path or "").lower()
        if name.endswith("completed.marker"):
            return "terminal"
        if name.endswith(".cpt"):
            return "checkpoint"
        if name.endswith("multi_model_preview.pdb"):
            return "stream-preview"
        if "preview_frame_" in name and name.endswith(".pdb"):
            return "stream-preview"
        if "preview_frame_" in name and (name.endswith(".bcif") or name.endswith(".cif") or name.endswith(".mmcif")):
            return "stream-preview"
        if name.endswith("history.json") or name.endswith("worker_history.json") or name.endswith("latest_status.json"):
            return "runtime-receipt"
        if name.endswith(".dcd"):
            return "terminal-trajectory"
        if name.endswith(".txt") or name.endswith(".log"):
            return "runtime-log"
        if "/analysis/" in name:
            return "analysis"
        return "durable_object"

    @staticmethod
    def _object_uri_from_prefix(output_gcs_prefix: str, object_path: str) -> str:
        object_text = str(object_path or "").strip()
        if not object_text:
            return ""
        if object_text.startswith("gs://"):
            return object_text
        if output_gcs_prefix.startswith("gs://"):
            bucket = output_gcs_prefix[5:].split("/", 1)[0]
            return f"gs://{bucket}/{object_text}"
        return object_text

    @staticmethod
    def _content_type_for_object(object_path: str, fallback: str = "") -> str:
        name = str(object_path or "").lower()
        if name.endswith(".bcif") or name.endswith(".bcif.gz"):
            return "application/octet-stream"
        if name.endswith(".cif") or name.endswith(".mmcif"):
            return "chemical/x-cif"
        if name.endswith(".pdb"):
            return "chemical/x-pdb"
        return fallback or "application/octet-stream"

    @staticmethod
    def _preview_event_format(payload_ref: str, preview_payload_format: str, fallback_event_format: str = "artifact_ref") -> str:
        payload_lower = str(payload_ref or "").lower()
        format_lower = str(preview_payload_format or "").lower()
        if payload_lower.endswith(".bcif") or format_lower in {"bcif", "binarycif"}:
            return "bcif"
        if payload_lower.endswith((".cif", ".mmcif")) or format_lower in {"cif", "mmcif"}:
            return "cif"
        if payload_lower.endswith(".pdb") or format_lower in {"pdb", "pdb_preview"}:
            return "pdb_preview"
        return str(fallback_event_format or "artifact_ref")

    @staticmethod
    def _build_bcif_preview_receipts(telemetry_receipts: Dict[str, Any]) -> List[Dict[str, Any]]:
        receipts: List[Dict[str, Any]] = []
        for frame in list(telemetry_receipts.get("trajectory_frame_event_receipts") or []):
            if not isinstance(frame, dict):
                continue
            bcif_status = str(frame.get("bcif_preview_status") or "")
            readback_verified = bool(frame.get("readback_verified", False))
            failure_code = str(frame.get("failure_code") or frame.get("preview_encoder_error") or "")
            payload_ref = str(frame.get("payload_ref") or "")
            if bcif_status == "implemented" and readback_verified:
                classification = "bcif_preview_completed"
                receipt_type = "bcif_preview_receipt"
            elif bcif_status == "implemented":
                classification = "preview_artifact_readback_failed"
                receipt_type = "bcif_preview_receipt"
                failure_code = failure_code or "preview_artifact_readback_failed"
            elif bcif_status == "dropped":
                classification = "bcif_preview_failed"
                receipt_type = "dropped_preview_frame_receipt"
                failure_code = failure_code or "preview_payload_dropped"
            else:
                classification = "bcif_preview_degraded_fallback"
                receipt_type = "preview_encoder_degraded_receipt" if failure_code else "bcif_preview_receipt"
            receipts.append(
                {
                    "schema_id": "mica.md.preview.bcif_receipt.v1",
                    "receipt_type": receipt_type,
                    "classification": classification,
                    "trajectory_frame_classification": "trajectory_frame_stream_completed" if payload_ref and readback_verified else "trajectory_frame_stream_degraded",
                    "job_id": frame.get("job_id"),
                    "route_decision_id": frame.get("route_decision_id"),
                    "provider": frame.get("provider"),
                    "provider_instance_id": frame.get("provider_instance_id"),
                    "gcs_prefix": frame.get("gcs_prefix"),
                    "frame_index": frame.get("frame_index"),
                    "step": frame.get("step"),
                    "time_ps": frame.get("time_ps"),
                    "format": frame.get("format"),
                    "preview_payload_format": frame.get("preview_payload_format"),
                    "bcif_preview_status": bcif_status,
                    "payload_ref": payload_ref,
                    "payload_size_bytes": frame.get("payload_size_bytes") or frame.get("size_bytes"),
                    "sha256": frame.get("sha256"),
                    "content_type": frame.get("content_type"),
                    "readback_verified": readback_verified,
                    "preview_not_canonical": bool(frame.get("preview_not_canonical", False)),
                    "durability_class": frame.get("durability_class"),
                    "pdb_preview_ref": frame.get("pdb_preview_ref"),
                    "bcif_preview_ref": frame.get("bcif_preview_ref"),
                    "mmcif_preview_ref": frame.get("mmcif_preview_ref"),
                    "source_topology_ref": frame.get("source_topology_ref"),
                    "source_positions_ref": frame.get("source_positions_ref"),
                    "source_trajectory_ref": frame.get("source_trajectory_ref"),
                    "source_artifact_ref": frame.get("source_artifact_ref"),
                    "preview_encoder": frame.get("preview_encoder"),
                    "failure_code": failure_code,
                    "failure_detail": frame.get("failure_detail") or "",
                    "worker_produced_at": frame.get("worker_produced_at"),
                    "gcs_observed_at": frame.get("gcs_observed_at"),
                    "client_observed_at": frame.get("client_observed_at"),
                }
            )
        return receipts

    async def _build_salad_readback_manifest(
        self,
        *,
        user_id: str,
        object_listing: List[Dict[str, Any]],
        output_gcs_prefix: str,
        job_id: str,
        provider_instance_id: str,
        provider_name: str,
    ) -> Dict[str, Any]:
        manifest_entries: List[Dict[str, Any]] = []
        summary = {
            "verified_count": 0,
            "missing_count": 0,
            "sha256_computed_count": 0,
        }
        try:
            from .storage.gcs_user_storage import get_storage_manager

            storage = get_storage_manager()
            if not hasattr(storage, "get_object_info"):
                return {"artifacts": manifest_entries, "summary": summary}

            for item in list(object_listing or []):
                object_path = str(item.get("name") or "").strip()
                if not object_path:
                    continue
                try:
                    info = storage.get_object_info(user_id=user_id, object_path=object_path)
                except Exception:
                    summary["missing_count"] += 1
                    continue

                size_bytes = int(info.get("size", 0) or 0)
                sha256_value = ""
                if size_bytes <= 20 * 1024 * 1024 and hasattr(storage, "read_bytes"):
                    try:
                        payload = storage.read_bytes(
                            user_id=user_id,
                            object_path=object_path,
                            max_bytes=max(1, size_bytes) if size_bytes else 1,
                        )
                        sha256_value = hashlib.sha256(payload).hexdigest()
                        summary["sha256_computed_count"] += 1
                    except Exception:
                        sha256_value = ""

                manifest_entries.append(
                    {
                        "object_path": object_path,
                        "object_uri": f"gs://{info.get('bucket')}/{object_path}",
                        "size_bytes": size_bytes,
                        "sha256": sha256_value or None,
                        "content_type": str(info.get("content_type") or "application/octet-stream"),
                        "produced_at": info.get("updated"),
                        "source_job_id": job_id,
                        "source_provider": provider_name,
                        "source_container_group": provider_instance_id,
                        "prefix": output_gcs_prefix,
                        "durability_class": self._artifact_durability_class(object_path),
                        "readback_verified": True,
                    }
                )
                summary["verified_count"] += 1
        except Exception as exc:
            logger.debug("Salad readback manifest build failed for %s: %s", job_id, exc)
        return {"artifacts": manifest_entries, "summary": summary}

    async def _read_salad_live_status(
        self,
        *,
        user_id: str,
        job_id: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not artifact_manifest.get("latest_status_json_present"):
            return {}
        try:
            from .storage.gcs_user_storage import get_storage_manager

            storage = get_storage_manager()
            if not hasattr(storage, "read_text_best_effort"):
                return {}
            durable_prefix = _parse_salad_durable_prefix(output_gcs_prefix, job_id)
            payload = storage.read_text_best_effort(
                user_id=user_id,
                object_path=f"{durable_prefix}/output/latest_status.json",
                max_chars=200_000,
            )
            text = str(payload.get("text") or "")
            if not text:
                return {}
            decoded = json.loads(text)
            return decoded if isinstance(decoded, dict) else {}
        except Exception as exc:
            logger.debug("Salad live status read failed for %s: %s", job_id, exc)
            return {}

    async def _maybe_publish_live_salad_runtime_events(
        self,
        *,
        user_id: str,
        job_id: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        summary: ComputeJobSummary,
    ) -> Dict[str, Any]:
        live_status = await self._read_salad_live_status(
            user_id=user_id,
            job_id=job_id,
            output_gcs_prefix=output_gcs_prefix,
            artifact_manifest=artifact_manifest,
        )
        if not live_status:
            return {}

        try:
            from mica.ws_md import (
                publish_artifact_transmission_event,
                publish_checkpoint_written_event,
                publish_compute_status_event,
                publish_md_progress_event,
                publish_trajectory_frame,
                publish_worker_heartbeat_event,
            )
        except Exception as exc:
            logger.debug("Salad live WS publishers unavailable for %s: %s", job_id, exc)
            return {}

        route_decision_id, _ = self._infer_route_decision_id(job_id, summary)
        provider_instance_id = str(summary.instance_id or "").strip()
        cache = dict(summary.metadata.get("live_runtime_receipts") or {})
        published_chunk_ids = set(cache.get("published_chunk_ids") or [])
        published_frame_refs = set(cache.get("published_frame_refs") or [])
        telemetry_events = list(cache.get("telemetry_event_receipts") or [])
        frame_events = list(cache.get("trajectory_frame_event_receipts") or [])
        artifact_events = list(cache.get("artifact_transmission_receipts") or [])

        chunk_id = int(live_status.get("steps_done", 0) or 0)
        preview_object = str(live_status.get("preview_object") or "").strip()
        bcif_preview_object = str(live_status.get("bcif_preview_object") or "").strip()
        mmcif_preview_object = str(live_status.get("mmcif_preview_object") or "").strip()
        multi_model_preview_object = str(live_status.get("multi_model_preview_object") or "").strip()
        dcd_object = str(live_status.get("dcd_object") or "").strip()
        log_object = str(live_status.get("log_object") or "").strip()
        topology_object = str(live_status.get("topology_object") or "").strip()
        checkpoint_object = str(live_status.get("checkpoint_object") or "").strip()
        produced_at = str(live_status.get("produced_at") or datetime.now(timezone.utc).isoformat())
        common_identity = self._salad_common_event_identity(
            summary=summary,
            output_gcs_prefix=output_gcs_prefix,
            route_decision_id=route_decision_id,
            live_status=live_status,
        )
        provider_instance_id = str(common_identity.get("provider_instance_id") or provider_instance_id)

        readback_manifest = await self._build_salad_readback_manifest(
            user_id=user_id,
            object_listing=list(artifact_manifest.get("object_listing") or []),
            output_gcs_prefix=output_gcs_prefix,
            job_id=job_id,
            provider_instance_id=provider_instance_id,
            provider_name="salad",
        )
        artifacts_by_uri: Dict[str, Dict[str, Any]] = {}
        for item in list(readback_manifest.get("artifacts") or []):
            object_uri = str(item.get("object_uri") or "").strip()
            object_path = str(item.get("object_path") or "").strip()
            if object_uri:
                artifacts_by_uri[object_uri] = item
            if object_path:
                artifacts_by_uri[object_path] = item

        if chunk_id and chunk_id not in published_chunk_ids:
            sequence_id = len(telemetry_events) + len(frame_events) + len(artifact_events) + 1
            progress_event = {
                "job_id": job_id,
                **common_identity,
                "timestamp": produced_at,
                "event_type": "md_progress",
                "sequence_id": sequence_id,
                "cadence_policy": {
                    "requested_frame_interval_ps": live_status.get("frame_interval_ps_requested"),
                    "actual_frame_interval_ps": live_status.get("actual_frame_interval_ps"),
                    "reason_for_delta": live_status.get("frame_interval_reason") or "",
                },
                "telemetry_source": "native_live_stream",
                "native_live_stream": True,
                "progress_percent": live_status.get("progress_percent"),
                "simulated_ns": round(float(live_status.get("time_ps", 0.0) or 0.0) / 1000.0, 6),
                "step": chunk_id,
                "time_ps": live_status.get("time_ps"),
            }
            telemetry_events.append(progress_event)
            publish_md_progress_event(
                job_id,
                route_decision_id=route_decision_id,
                provider="salad",
                provider_instance_id=provider_instance_id,
                gcs_prefix=output_gcs_prefix,
                progress_percent=float(live_status.get("progress_percent", 0.0) or 0.0),
                simulated_ns=float(progress_event["simulated_ns"]),
                step=chunk_id,
                time_ps=float(live_status.get("time_ps", 0.0) or 0.0),
                cadence_policy=dict(progress_event["cadence_policy"]),
                metadata={
                    "telemetry_source": "native_live_stream",
                    "requested_frame_interval_ps": live_status.get("frame_interval_ps_requested"),
                    "actual_frame_interval_ps": live_status.get("actual_frame_interval_ps"),
                },
                sequence_id=sequence_id,
                requested_gpu_type=str(common_identity.get("requested_gpu_type") or ""),
                actual_gpu_type=str(common_identity.get("actual_gpu_type") or ""),
                image_digest=str(common_identity.get("image_digest") or ""),
                source_target_id=str(common_identity.get("source_target_id") or ""),
                allocation_attempt=int(common_identity.get("allocation_attempt") or 0),
                protocol_node_id=str(common_identity.get("protocol_node_id") or ""),
                session_id=str(common_identity.get("session_id") or ""),
            )
            publish_worker_heartbeat_event(
                job_id,
                **common_identity,
                status=str(live_status.get("status") or "running"),
                sequence_id=sequence_id + 1,
                metadata={
                    "steps_done": chunk_id,
                    "time_ps": live_status.get("time_ps"),
                    "telemetry_source": "native_live_stream",
                },
            )
            published_chunk_ids.add(chunk_id)

        for object_path in (dcd_object, log_object, topology_object, preview_object, bcif_preview_object, mmcif_preview_object, multi_model_preview_object, checkpoint_object):
            if not object_path:
                continue
            object_uri = f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{object_path}" if output_gcs_prefix.startswith("gs://") else object_path
            if object_uri in published_frame_refs:
                continue
            artifact_meta = artifacts_by_uri.get(object_uri, {})
            sequence_id = len(telemetry_events) + len(frame_events) + len(artifact_events) + 1
            artifact_event = {
                "job_id": job_id,
                **common_identity,
                "timestamp": produced_at,
                "event_type": "artifact_transmission",
                "sequence_id": sequence_id,
                "artifact_ref": object_uri,
                "object_uri": object_uri,
                "object_path": object_path,
                "size_bytes": artifact_meta.get("size_bytes"),
                "sha256": artifact_meta.get("sha256"),
                "content_type": artifact_meta.get("content_type"),
                "worker_produced_at": produced_at,
                "gcs_observed_at": artifact_meta.get("produced_at"),
                "client_observed_at": datetime.now(timezone.utc).isoformat(),
                "source_job_id": job_id,
                "source_provider": "salad",
                "source_container_group": provider_instance_id,
                "prefix": output_gcs_prefix,
                "durability_class": artifact_meta.get("durability_class") or self._artifact_durability_class(object_path),
                "readback_verified": bool(artifact_meta.get("readback_verified", False)),
            }
            artifact_events.append(artifact_event)
            publish_artifact_transmission_event(
                job_id,
                **common_identity,
                artifact_ref=object_uri,
                object_uri=object_uri,
                sha256=str(artifact_meta.get("sha256") or ""),
                size_bytes=artifact_meta.get("size_bytes"),
                content_type=str(artifact_meta.get("content_type") or ""),
                durability_class=str(artifact_event.get("durability_class") or ""),
                readback_verified=bool(artifact_event.get("readback_verified")),
                sequence_id=sequence_id,
                metadata={
                    "object_path": object_path,
                    "worker_produced_at": produced_at,
                    "gcs_observed_at": artifact_meta.get("produced_at"),
                    "client_observed_at": artifact_event["client_observed_at"],
                },
            )
            if str(artifact_event.get("durability_class") or "") == "checkpoint":
                publish_checkpoint_written_event(
                    job_id,
                    **common_identity,
                    checkpoint_ref=object_uri,
                    sequence_id=sequence_id + 1,
                    metadata={
                        "object_path": object_path,
                        "worker_produced_at": produced_at,
                        "gcs_observed_at": artifact_meta.get("produced_at"),
                        "readback_verified": artifact_event.get("readback_verified"),
                    },
                )
            publish_compute_status_event(
                job_id,
                route_decision_id=route_decision_id,
                provider="salad",
                provider_instance_id=provider_instance_id,
                gcs_prefix=output_gcs_prefix,
                status="artifact_transmitted",
                artifact_ref=object_uri,
                metadata={
                    "event_type": "artifact_transmission",
                    "object_path": object_path,
                    "size_bytes": artifact_meta.get("size_bytes"),
                    "sha256": artifact_meta.get("sha256"),
                    "worker_produced_at": produced_at,
                    "gcs_observed_at": artifact_meta.get("produced_at"),
                    "client_observed_at": artifact_event["client_observed_at"],
                    "durability_class": artifact_event.get("durability_class"),
                },
                sequence_id=sequence_id,
                requested_gpu_type=str(common_identity.get("requested_gpu_type") or ""),
                actual_gpu_type=str(common_identity.get("actual_gpu_type") or ""),
                image_digest=str(common_identity.get("image_digest") or ""),
                source_target_id=str(common_identity.get("source_target_id") or ""),
                allocation_attempt=int(common_identity.get("allocation_attempt") or 0),
                protocol_node_id=str(common_identity.get("protocol_node_id") or ""),
                session_id=str(common_identity.get("session_id") or ""),
            )
            published_frame_refs.add(object_uri)

        if preview_object:
            preview_uri = f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{preview_object}" if output_gcs_prefix.startswith("gs://") else preview_object
            preview_contract = live_status.get("preview_contract") if isinstance(live_status.get("preview_contract"), dict) else {}
            bcif_status = str(preview_contract.get("bcif_preview_status") or "implemented" if bcif_preview_object else "degraded_or_not_implemented")
            if bcif_status == "implemented" and not bcif_preview_object:
                bcif_status = "degraded_or_not_implemented"
            preview_payload_format = str(preview_contract.get("preview_payload_format") or ("bcif" if bcif_status == "implemented" else "pdb"))
            fallback_event_format = str(preview_contract.get("fallback_event_format") or "artifact_ref")
            bcif_preview_uri = f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{bcif_preview_object}" if bcif_preview_object and output_gcs_prefix.startswith("gs://") else bcif_preview_object
            mmcif_preview_uri = f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{mmcif_preview_object}" if mmcif_preview_object and output_gcs_prefix.startswith("gs://") else mmcif_preview_object
            payload_uri = bcif_preview_uri if bcif_status == "implemented" and bcif_preview_uri else preview_uri
            frame_index = int(live_status.get("frame_index", 0) or 0)
            if bcif_status == "implemented" and payload_uri != preview_uri:
                frame_events = [
                    item for item in frame_events
                    if not (
                        str(item.get("payload_ref") or "") == preview_uri
                        and int(item.get("frame_index") or -1) == frame_index
                    )
                ]
            if not any(str(item.get("payload_ref") or "") == payload_uri for item in frame_events):
                preview_meta = artifacts_by_uri.get(payload_uri) or artifacts_by_uri.get(preview_uri, {})
                topology_uri = self._object_uri_from_prefix(output_gcs_prefix, topology_object)
                dcd_uri = self._object_uri_from_prefix(output_gcs_prefix, dcd_object)
                source_artifact_ref = dcd_uri or preview_uri
                multi_model_preview_ref = self._object_uri_from_prefix(output_gcs_prefix, multi_model_preview_object)
                event_format = self._preview_event_format(payload_uri, preview_payload_format, fallback_event_format)
                content_type = str(preview_meta.get("content_type") or self._content_type_for_object(payload_uri))
                payload_path_for_match = payload_uri.replace(f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/", "") if output_gcs_prefix.startswith("gs://") and str(payload_uri).startswith("gs://") else payload_uri
                readback_verified = bool(preview_meta.get("readback_verified", False) or payload_uri in artifacts_by_uri or payload_path_for_match in artifacts_by_uri)
                sequence_id = len(telemetry_events) + len(frame_events) + len(artifact_events) + 1
                frame_event = {
                    "job_id": job_id,
                    **common_identity,
                    "timestamp": produced_at,
                    "event_type": "trajectory_frame",
                    "sequence_id": sequence_id,
                    "frame_index": frame_index,
                    "step": chunk_id,
                    "time_ps": float(live_status.get("time_ps", 0.0) or 0.0),
                    "format": event_format,
                    "payload_ref": payload_uri,
                    "payload_size_bytes": preview_meta.get("size_bytes"),
                    "content_type": content_type,
                    "readback_verified": readback_verified,
                    "source_artifact_ref": source_artifact_ref,
                    "source_topology_ref": topology_uri,
                    "source_positions_ref": preview_uri,
                    "source_trajectory_ref": dcd_uri,
                    "multi_model_preview_ref": multi_model_preview_ref,
                    "pdb_preview_ref": preview_uri,
                    "bcif_preview_ref": bcif_preview_uri,
                    "mmcif_preview_ref": mmcif_preview_uri,
                    "preview_lineage": {
                        "preview_frame_ref": preview_uri,
                        "bcif_preview_ref": bcif_preview_uri,
                        "mmcif_preview_ref": mmcif_preview_uri,
                        "multi_model_preview_ref": multi_model_preview_ref,
                        "source_artifact_ref": source_artifact_ref,
                    },
                    "preview_not_canonical": True,
                    "bcif_preview_status": bcif_status,
                    "fallback_event_format": fallback_event_format,
                    "preview_payload_format": preview_payload_format,
                    "preview_contract_version": str(preview_contract.get("contract_version") or "native_live_preview_v1"),
                    "preview_encoder": str(preview_contract.get("encoder") or ""),
                    "preview_encoder_error": str(preview_contract.get("encoder_error") or ""),
                    "requested_frame_interval_ps": live_status.get("frame_interval_ps_requested"),
                    "actual_frame_interval_ps": live_status.get("actual_frame_interval_ps"),
                    "reason_for_delta": live_status.get("frame_interval_reason") or "",
                    "durability_class": "stream-preview",
                    "size_bytes": preview_meta.get("size_bytes"),
                    "sha256": preview_meta.get("sha256"),
                    "worker_produced_at": produced_at,
                    "gcs_observed_at": preview_meta.get("produced_at"),
                    "client_observed_at": datetime.now(timezone.utc).isoformat(),
                }
                frame_events.append(frame_event)
                publish_trajectory_frame(
                    job_id,
                    frame_index=int(frame_event["frame_index"]),
                    step=chunk_id,
                    time_ps=float(frame_event["time_ps"]),
                    pdb_data="",
                    run_id=job_id,
                    route_decision_id=route_decision_id,
                    provider="salad",
                    provider_instance_id=provider_instance_id,
                    gcs_prefix=output_gcs_prefix,
                    payload_ref=payload_uri,
                    source_artifact_ref=str(frame_event["source_artifact_ref"]),
                    event_format=event_format,
                    bcif_preview_status=bcif_status,
                    fallback_event_format=fallback_event_format,
                    preview_not_canonical=True,
                    size_bytes=preview_meta.get("size_bytes"),
                    sha256=str(preview_meta.get("sha256") or ""),
                    content_type=content_type,
                    readback_verified=readback_verified,
                    source_topology_ref=topology_uri,
                    source_positions_ref=preview_uri,
                    source_trajectory_ref=dcd_uri,
                    worker_produced_at=produced_at,
                    gcs_observed_at=str(preview_meta.get("produced_at") or ""),
                    requested_frame_interval_ps=live_status.get("frame_interval_ps_requested"),
                    actual_frame_interval_ps=live_status.get("actual_frame_interval_ps"),
                    sequence_id=sequence_id,
                    requested_gpu_type=str(common_identity.get("requested_gpu_type") or ""),
                    actual_gpu_type=str(common_identity.get("actual_gpu_type") or ""),
                    image_digest=str(common_identity.get("image_digest") or ""),
                    source_target_id=str(common_identity.get("source_target_id") or ""),
                    allocation_attempt=int(common_identity.get("allocation_attempt") or 0),
                    protocol_node_id=str(common_identity.get("protocol_node_id") or ""),
                    session_id=str(common_identity.get("session_id") or ""),
                    durability_class="stream-preview",
                    preview_payload_format=preview_payload_format,
                    pdb_preview_ref=preview_uri,
                    bcif_preview_ref=bcif_preview_uri,
                    mmcif_preview_ref=mmcif_preview_uri,
                    preview_encoder=str(frame_event.get("preview_encoder") or ""),
                    preview_encoder_error=str(frame_event.get("preview_encoder_error") or ""),
                )

        last_value_snapshot = {
            "job_id": job_id,
            **common_identity,
            "timestamp": produced_at,
            "telemetry_source": "native_live_stream",
            "native_live_stream": True,
            "step": chunk_id,
            "time_ps": live_status.get("time_ps"),
            "progress_percent": live_status.get("progress_percent"),
            "frame_index": live_status.get("frame_index"),
        }
        cadence_policy = {
            "job_id": job_id,
            **common_identity,
            "telemetry_source": "native_live_stream",
            "native_live_stream": True,
            "requested_frame_interval_ps": live_status.get("frame_interval_ps_requested"),
            "actual_frame_interval_ps": live_status.get("actual_frame_interval_ps"),
            "reason_for_delta": live_status.get("frame_interval_reason") or "",
            "event_count": len(telemetry_events),
            "frame_event_count": len(frame_events),
            "artifact_event_count": len(artifact_events),
        }
        session_receipt = {
            "job_id": job_id,
            **common_identity,
            "telemetry_source": "native_live_stream",
            "native_live_stream": True,
            "status": str(live_status.get("status") or "running"),
            "event_count": len(telemetry_events),
            "frame_event_count": len(frame_events),
            "artifact_event_count": len(artifact_events),
        }

        cache.update(
            {
                "telemetry_source": "native_live_stream",
                "native_live_stream": True,
                "stream_session_receipt": session_receipt,
                "telemetry_event_receipts": telemetry_events,
                "trajectory_frame_event_receipts": frame_events,
                "artifact_transmission_receipts": artifact_events,
                "last_value_snapshot": last_value_snapshot,
                "cadence_policy": cadence_policy,
                "published_chunk_ids": sorted(published_chunk_ids),
                "published_frame_refs": sorted(published_frame_refs),
            }
        )
        summary.metadata["live_runtime_receipts"] = cache
        return cache
    async def _reconstruct_salad_telemetry_receipts(
        self,
        *,
        user_id: str,
        job_id: str,
        output_gcs_prefix: str,
        route_decision_id: str,
        provider_instance_id: str,
        artifact_manifest: Dict[str, Any],
        readback_manifest: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        receipts_root = _salad_runtime_receipts_root(output_gcs_prefix, job_id)
        result: Dict[str, Any] = {
            "telemetry_source": "reconstructed_from_worker_history",
            "native_live_stream": False,
            "stream_session_receipt": {},
            "telemetry_event_receipts": [],
            "trajectory_frame_event_receipts": [],
            "artifact_transmission_receipts": [],
            "last_value_snapshot": {},
            "cadence_policy": {},
            "receipt_refs": {
                "stream_session_receipt": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/stream_session_receipt.json" if output_gcs_prefix.startswith("gs://") else "",
                "telemetry_event_receipts": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/telemetry_event_receipts.json" if output_gcs_prefix.startswith("gs://") else "",
                "trajectory_frame_event_receipts": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/trajectory_frame_event_receipts.json" if output_gcs_prefix.startswith("gs://") else "",
                "bcif_preview_receipts": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/bcif_preview_receipts.json" if output_gcs_prefix.startswith("gs://") else "",
                "artifact_transmission_receipts": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/artifact_transmission_receipts.json" if output_gcs_prefix.startswith("gs://") else "",
                "last_value_snapshot": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/last_value_snapshot.json" if output_gcs_prefix.startswith("gs://") else "",
                "cadence_policy": f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{receipts_root}/cadence_policy.json" if output_gcs_prefix.startswith("gs://") else "",
            },
        }
        if not artifact_manifest.get("history_json_present") and not artifact_manifest.get("worker_history_json_present"):
            result["cadence_policy"] = {
                "telemetry_source": "reconstructed_from_worker_history",
                "native_live_stream": False,
                "status": "missing_worker_history",
            }
            return result

        try:
            from .storage.gcs_user_storage import get_storage_manager

            storage = get_storage_manager()
            if not hasattr(storage, "read_text_best_effort"):
                return result

            durable_prefix = _parse_salad_durable_prefix(output_gcs_prefix, job_id)
            output_prefix = f"{durable_prefix}/output"

            def _read_json(name: str) -> Dict[str, Any]:
                payload = storage.read_text_best_effort(
                    user_id=user_id,
                    object_path=f"{output_prefix}/{name}",
                    max_chars=400_000,
                )
                text = str(payload.get("text") or "")
                if not text:
                    return {}
                decoded = json.loads(text)
                return decoded if isinstance(decoded, dict) else {}

            history = _read_json("history.json") if artifact_manifest.get("history_json_present") else {}
            worker_history = _read_json("worker_history.json") if artifact_manifest.get("worker_history_json_present") else {}
            source_doc = worker_history or history
            events = list(source_doc.get("events") or [])
            started_at = source_doc.get("started_at")
            completed_at = source_doc.get("completed_at")
            mode = str(source_doc.get("mode") or source_doc.get("worker_mode") or "unknown")
            readback_doc = dict(readback_manifest or {})
            readback_by_uri = {
                str(item.get("object_uri") or "").strip(): item
                for item in list(readback_doc.get("artifacts") or [])
                if str(item.get("object_uri") or "").strip()
            }
            readback_by_path = {
                str(item.get("object_path") or "").strip(): item
                for item in list(readback_doc.get("artifacts") or [])
                if str(item.get("object_path") or "").strip()
            }
            listing_by_name = {
                str(item.get("name") or "").strip(): item
                for item in list(artifact_manifest.get("object_listing") or [])
                if str(item.get("name") or "").strip()
            }

            def _artifact_meta(object_path: str) -> Dict[str, Any]:
                uri = self._object_uri_from_prefix(output_gcs_prefix, object_path)
                if uri in readback_by_uri:
                    return dict(readback_by_uri[uri])
                if str(object_path or "") in readback_by_path:
                    return dict(readback_by_path[str(object_path or "")])
                listing = dict(listing_by_name.get(str(object_path or ""), {}))
                return {
                    "object_uri": uri,
                    "size_bytes": listing.get("size"),
                    "sha256": listing.get("sha256"),
                    "content_type": listing.get("content_type") or self._content_type_for_object(object_path),
                    "produced_at": listing.get("updated"),
                    "durability_class": self._artifact_durability_class(object_path),
                    "readback_verified": bool(uri in readback_by_uri),
                }

            telemetry_events: List[Dict[str, Any]] = []
            artifact_events: List[Dict[str, Any]] = []
            frame_events: List[Dict[str, Any]] = []
            seen_artifact_uris: set[str] = set()
            event_timestamps: List[str] = []
            for index, event in enumerate(events):
                timestamp = str(event.get("ts") or "").strip() or completed_at or started_at or ""
                if timestamp:
                    event_timestamps.append(timestamp)
                telemetry_events.append(
                    {
                        "job_id": job_id,
                        "route_decision_id": route_decision_id,
                        "provider": "salad",
                        "provider_instance_id": provider_instance_id,
                        "gcs_prefix": output_gcs_prefix,
                        "timestamp": timestamp,
                        "event_type": "md_progress",
                        "cadence_policy": "reconstructed_from_worker_history",
                        "telemetry_source": "reconstructed_from_worker_history",
                        "native_live_stream": False,
                        "event_index": index,
                        "steps_done": int(event.get("steps_done", 0) or 0),
                        "chunk_steps": int(event.get("chunk_steps", 0) or 0),
                        "chunk_id": int(event.get("chunk_id", 0) or 0),
                        "worker_mode": mode,
                    }
                )
                for object_path in (
                    str(event.get("dcd_object") or "").strip(),
                    str(event.get("log_object") or "").strip(),
                    str(event.get("topology_object") or "").strip(),
                    str(event.get("preview_object") or "").strip(),
                    str(event.get("bcif_preview_object") or "").strip(),
                    str(event.get("mmcif_preview_object") or "").strip(),
                    str(event.get("multi_model_preview_object") or "").strip(),
                    str(event.get("checkpoint_object") or "").strip(),
                ):
                    if not object_path:
                        continue
                    object_uri = self._object_uri_from_prefix(output_gcs_prefix, object_path)
                    if object_uri in seen_artifact_uris:
                        continue
                    meta = _artifact_meta(object_path)
                    artifact_events.append(
                        {
                            "job_id": job_id,
                            "route_decision_id": route_decision_id,
                            "provider": "salad",
                            "provider_instance_id": provider_instance_id,
                            "gcs_prefix": output_gcs_prefix,
                            "timestamp": timestamp,
                            "event_type": "artifact_transmission",
                            "sequence_id": len(telemetry_events) + len(artifact_events) + len(frame_events),
                            "artifact_ref": object_uri,
                            "object_uri": object_uri,
                            "object_path": object_path,
                            "size_bytes": meta.get("size_bytes"),
                            "sha256": meta.get("sha256"),
                            "content_type": meta.get("content_type") or self._content_type_for_object(object_path),
                            "worker_produced_at": timestamp,
                            "gcs_observed_at": meta.get("produced_at"),
                            "client_observed_at": datetime.now(timezone.utc).isoformat(),
                            "source_job_id": job_id,
                            "source_provider": "salad",
                            "source_container_group": provider_instance_id,
                            "prefix": output_gcs_prefix,
                            "durability_class": meta.get("durability_class") or self._artifact_durability_class(object_path),
                            "readback_verified": bool(meta.get("readback_verified", False)),
                        }
                    )
                    seen_artifact_uris.add(object_uri)

                preview_object = str(event.get("preview_object") or "").strip()
                if preview_object:
                    preview_contract = event.get("preview_contract") if isinstance(event.get("preview_contract"), dict) else {}
                    bcif_object = str(event.get("bcif_preview_object") or preview_contract.get("bcif_preview_ref") or "").strip()
                    mmcif_object = str(event.get("mmcif_preview_object") or preview_contract.get("mmcif_preview_ref") or "").strip()
                    bcif_status = str(preview_contract.get("bcif_preview_status") or ("implemented" if bcif_object else "degraded_or_not_implemented"))
                    if bcif_status == "implemented" and not bcif_object:
                        bcif_status = "degraded_or_not_implemented"
                    preview_payload_format = str(preview_contract.get("preview_payload_format") or ("bcif" if bcif_status == "implemented" else "pdb"))
                    preview_uri = self._object_uri_from_prefix(output_gcs_prefix, preview_object)
                    bcif_uri = self._object_uri_from_prefix(output_gcs_prefix, bcif_object)
                    mmcif_uri = self._object_uri_from_prefix(output_gcs_prefix, mmcif_object)
                    payload_uri = bcif_uri if bcif_status == "implemented" and bcif_uri else (mmcif_uri if preview_payload_format in {"cif", "mmcif"} and mmcif_uri else preview_uri)
                    payload_path = payload_uri.replace(f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/", "") if output_gcs_prefix.startswith("gs://") and payload_uri.startswith("gs://") else payload_uri
                    payload_meta = _artifact_meta(payload_path)
                    topology_object = str(event.get("topology_object") or preview_contract.get("source_topology_ref") or "").strip()
                    dcd_object = str(event.get("dcd_object") or preview_contract.get("source_trajectory_ref") or "").strip()
                    frame_events.append(
                        {
                            "job_id": job_id,
                            "route_decision_id": route_decision_id,
                            "provider": "salad",
                            "provider_instance_id": provider_instance_id,
                            "gcs_prefix": output_gcs_prefix,
                            "timestamp": timestamp,
                            "event_type": "trajectory_frame",
                            "sequence_id": len(telemetry_events) + len(artifact_events) + len(frame_events) + 1,
                            "frame_index": int(event.get("frame_index", index) or index),
                            "step": int(event.get("steps_done", 0) or 0),
                            "time_ps": float(event.get("time_ps", 0.0) or 0.0),
                            "format": self._preview_event_format(payload_uri, preview_payload_format, str(preview_contract.get("fallback_event_format") or "artifact_ref")),
                            "payload_ref": payload_uri,
                            "payload_size_bytes": payload_meta.get("size_bytes"),
                            "size_bytes": payload_meta.get("size_bytes"),
                            "sha256": payload_meta.get("sha256"),
                            "content_type": payload_meta.get("content_type") or self._content_type_for_object(payload_uri),
                            "readback_verified": bool(payload_meta.get("readback_verified", False)),
                            "source_artifact_ref": self._object_uri_from_prefix(output_gcs_prefix, dcd_object) or preview_uri,
                            "source_topology_ref": self._object_uri_from_prefix(output_gcs_prefix, topology_object),
                            "source_positions_ref": preview_uri,
                            "source_trajectory_ref": self._object_uri_from_prefix(output_gcs_prefix, dcd_object),
                            "pdb_preview_ref": preview_uri,
                            "bcif_preview_ref": bcif_uri,
                            "mmcif_preview_ref": mmcif_uri,
                            "preview_not_canonical": True,
                            "bcif_preview_status": bcif_status,
                            "preview_payload_format": preview_payload_format,
                            "fallback_event_format": str(preview_contract.get("fallback_event_format") or "artifact_ref"),
                            "preview_encoder": str(preview_contract.get("encoder") or ""),
                            "preview_encoder_error": str(preview_contract.get("encoder_error") or ""),
                            "failure_code": str(preview_contract.get("failure_code") or ""),
                            "failure_detail": str(preview_contract.get("failure_detail") or ""),
                            "requested_frame_interval_ps": event.get("requested_frame_interval_ps") or source_doc.get("frame_interval_ps_requested"),
                            "actual_frame_interval_ps": event.get("actual_frame_interval_ps") or source_doc.get("actual_frame_interval_ps"),
                            "reason_for_delta": event.get("frame_interval_reason") or source_doc.get("frame_interval_reason") or "",
                            "durability_class": "stream-preview",
                            "worker_produced_at": timestamp,
                            "gcs_observed_at": payload_meta.get("produced_at"),
                            "client_observed_at": datetime.now(timezone.utc).isoformat(),
                            "native_live_stream": False,
                            "telemetry_source": "reconstructed_from_worker_history",
                        }
                    )

            observed_cadence_seconds: Optional[float] = None
            if len(event_timestamps) >= 2:
                try:
                    start = datetime.fromisoformat(event_timestamps[0].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(event_timestamps[-1].replace("Z", "+00:00"))
                    observed_cadence_seconds = (end - start).total_seconds() / float(len(event_timestamps) - 1)
                except Exception:
                    observed_cadence_seconds = None

            last_event = telemetry_events[-1] if telemetry_events else {}
            result["telemetry_event_receipts"] = telemetry_events
            result["trajectory_frame_event_receipts"] = frame_events
            result["artifact_transmission_receipts"] = artifact_events
            result["last_value_snapshot"] = {
                "job_id": job_id,
                "route_decision_id": route_decision_id,
                "provider": "salad",
                "provider_instance_id": provider_instance_id,
                "gcs_prefix": output_gcs_prefix,
                "timestamp": str(last_event.get("timestamp") or completed_at or started_at or ""),
                "telemetry_source": "reconstructed_from_worker_history",
                "native_live_stream": False,
                "mode": mode,
                "steps_done": last_event.get("steps_done"),
                "chunk_steps": last_event.get("chunk_steps"),
                "chunk_id": last_event.get("chunk_id"),
            }
            result["cadence_policy"] = {
                "job_id": job_id,
                "route_decision_id": route_decision_id,
                "provider": "salad",
                "provider_instance_id": provider_instance_id,
                "gcs_prefix": output_gcs_prefix,
                "telemetry_source": "reconstructed_from_worker_history",
                "native_live_stream": False,
                "observed_event_count": len(telemetry_events),
                "observed_frame_event_count": len(frame_events),
                "observed_artifact_event_count": len(artifact_events),
                "observed_cadence_seconds": observed_cadence_seconds,
                "observed_cadence_reconstructible": observed_cadence_seconds is not None,
                "history_json_present": bool(history),
                "worker_history_json_present": bool(worker_history),
            }
            result["stream_session_receipt"] = {
                "job_id": job_id,
                "route_decision_id": route_decision_id,
                "provider": "salad",
                "provider_instance_id": provider_instance_id,
                "gcs_prefix": output_gcs_prefix,
                "telemetry_source": "reconstructed_from_worker_history",
                "native_live_stream": False,
                "event_count": len(telemetry_events),
                "frame_event_count": len(frame_events),
                "artifact_event_count": len(artifact_events),
                "session_started_at": started_at,
                "session_completed_at": completed_at,
                "worker_mode": mode,
                "status": "completed" if completed_at else "partial",
            }
        except Exception as exc:
            logger.debug("Salad telemetry reconstruction failed for %s: %s", job_id, exc)
        return result
    async def _guard_salad_smic_truth(
        self,
        *,
        user_id: str,
        job_id: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        smic_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        guarded = dict(smic_summary or {})
        try:
            selected = self._select_salad_terminal_smic_inputs(list(artifact_manifest.get("object_listing") or []))
            topology_object = selected.get("topology_object") or ""
            trajectory_object = selected.get("trajectory_object") or ""
            if not topology_object or not trajectory_object:
                return guarded

            from .storage.gcs_user_storage import get_storage_manager

            storage = get_storage_manager()
            runtime_root = Path.cwd() / ".mica" / "runtime" / "terminal_truth_guard" / job_id
            runtime_root.mkdir(parents=True, exist_ok=True)
            local_topology = runtime_root / Path(topology_object).name
            local_trajectory = runtime_root / Path(trajectory_object).name
            storage.download_file(user_id=user_id, object_path=topology_object, local_path=local_topology)
            storage.download_file(user_id=user_id, object_path=trajectory_object, local_path=local_trajectory)

            _, skipped_registry = self._filter_terminal_smic_analyses_for_registry(["rmsd", "rmsf", "contacts"])
            _, skipped_dynamic = self._filter_terminal_smic_analyses_for_topology(
                ["rmsd", "rmsf", "contacts"],
                local_topology,
                local_trajectory,
            )
            degraded_metrics = {}
            degraded_metrics.update(skipped_registry)
            degraded_metrics.update(skipped_dynamic)
            if not degraded_metrics:
                return guarded

            guarded.setdefault("guarded_truth", {})
            guarded["guarded_truth"] = {
                "status": "degraded" if any(key in degraded_metrics for key in ("rmsd", "rmsf")) else guarded.get("status", "completed"),
                "source": "terminal_truth_guard",
                "topology_object": topology_object,
                "trajectory_object": trajectory_object,
                "degraded_metrics": degraded_metrics,
            }
            if any(key in degraded_metrics for key in ("rmsd", "rmsf")):
                guarded["status"] = "degraded"
                guarded["degraded_metrics"] = degraded_metrics
                guarded["smic_dynamic_metric_blocker"] = {
                    key: value
                    for key, value in degraded_metrics.items()
                    if key in {"rmsd", "rmsf"}
                }
            elif degraded_metrics:
                guarded.setdefault("degraded_metrics", {})
                guarded["degraded_metrics"].update(degraded_metrics)
        except Exception as exc:
            logger.debug("Salad SMIC truth guard failed for %s: %s", job_id, exc)
        return guarded

    @staticmethod
    def _filter_terminal_smic_analyses_for_registry(analyses: List[str]) -> tuple[List[str], Dict[str, str]]:
        try:
            from mica.api_v1.routers.smic import _runtime_module_registry

            registry = _runtime_module_registry()
        except Exception as exc:
            logger.debug("Terminal SMIC registry probe failed: %s", exc)
            return analyses, {}

        resolved = [str(item or "").strip().lower() for item in analyses if str(item or "").strip()]
        skipped: Dict[str, str] = {}
        kept: List[str] = []
        for analysis in resolved:
            if analysis in registry:
                kept.append(analysis)
                continue
            skipped[analysis] = "rmsf_not_registered" if analysis == "rmsf" else "unknown_metric_not_registered"
        return kept, skipped

    def _build_terminal_projection_from_evidence(
        self,
        *,
        job_id: str,
        route_decision_id: str,
        provider_instance_id: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        readback_manifest: Dict[str, Any],
        telemetry_receipts: Dict[str, Any],
        smic_summary: Dict[str, Any],
        evidencegate_derivation: bool,
    ) -> Dict[str, Any]:
        completed_marker = bool(artifact_manifest.get("completed_marker_confirmed"))
        failure_receipt_present = bool(artifact_manifest.get("failure_receipt_present"))
        if failure_receipt_present:
            state = "failed"
            success = False
            reason_code = "active_failure_receipt_present"
            reason_message = "Active failure receipt is present under the governed packet prefix."
        elif completed_marker:
            state = "completed"
            success = True
            reason_code = "completed_marker_confirmed"
            reason_message = ""
        else:
            state = "stopped"
            success = False
            reason_code = "terminal_marker_missing"
            reason_message = "Terminal marker is absent; provider status alone cannot prove completion."

        smic_state = str(smic_summary.get("status") or "")
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "job": {
                "job_id": job_id,
                "workflow": "protein_ligand_md",
                "execution_target": "remote",
                "execution_class": "research",
                "route_decision_id": route_decision_id,
            },
            "status": {
                "state": state,
                "phase": "production" if completed_marker else "",
                "terminal": completed_marker or failure_receipt_present,
                "success": success,
                "reason_code": reason_code,
                "reason_message": reason_message,
            },
            "artifacts": {
                "output_gcs_prefix": output_gcs_prefix,
                "provider_instance_id": provider_instance_id,
                "completed_marker_confirmed": completed_marker,
                "failure_receipt_present": failure_receipt_present,
                "manifest_entries": list(artifact_manifest.get("object_listing") or []),
                "artifact_manifest_refs": [entry.get("object_uri") for entry in list(readback_manifest.get("artifacts") or [])],
                "readback_summary": dict(readback_manifest.get("summary") or {}),
                "telemetry_receipt_refs": dict(telemetry_receipts.get("receipt_refs") or {}),
            },
            "provider": {
                "name": "salad",
                "instance_id": provider_instance_id,
                "output_gcs_prefix": output_gcs_prefix,
            },
            "smic": {
                "status": smic_state or "degraded",
                "summary": dict(smic_summary or {}),
            },
            "validation": {
                "completed_marker_confirmed": completed_marker,
                "active_failure_receipt_present": failure_receipt_present,
                "readback_verified_count": int((readback_manifest.get("summary") or {}).get("verified_count", 0)),
                "telemetry_source": telemetry_receipts.get("telemetry_source"),
                "native_live_stream": bool(telemetry_receipts.get("native_live_stream", False)),
                "evidencegate_derivation": bool(evidencegate_derivation),
            },
            "projection": {
                "terminal_projection_from_evidencegate": bool(evidencegate_derivation),
                "evidencegate_derivation": bool(evidencegate_derivation),
                "produced_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    async def _persist_salad_runtime_receipts(
        self,
        *,
        user_id: str,
        job_id: str,
        summary: ComputeJobSummary,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        canonical_result: Dict[str, Any],
        smic_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        receipt_paths: Dict[str, Any] = {}
        try:
            from .storage.gcs_user_storage import get_storage_manager

            storage = get_storage_manager()
            if not hasattr(storage, "upload_text"):
                return receipt_paths

            route_decision_id, route_source = self._infer_route_decision_id(job_id, summary)
            provider_instance_id = str(summary.instance_id or "").strip()
            guarded_smic_summary = await self._guard_salad_smic_truth(
                user_id=user_id,
                job_id=job_id,
                output_gcs_prefix=output_gcs_prefix,
                artifact_manifest=artifact_manifest,
                smic_summary=smic_summary,
            )
            readback_manifest = await self._build_salad_readback_manifest(
                user_id=user_id,
                object_listing=list(artifact_manifest.get("object_listing") or []),
                output_gcs_prefix=output_gcs_prefix,
                job_id=job_id,
                provider_instance_id=provider_instance_id,
                provider_name="salad",
            )
            telemetry_receipts = dict(summary.metadata.get("live_runtime_receipts") or {})
            if not telemetry_receipts:
                telemetry_receipts = await self._reconstruct_salad_telemetry_receipts(
                    user_id=user_id,
                    job_id=job_id,
                    output_gcs_prefix=output_gcs_prefix,
                    route_decision_id=route_decision_id,
                    provider_instance_id=provider_instance_id,
                    artifact_manifest=artifact_manifest,
                    readback_manifest=readback_manifest,
                )

            bcif_preview_receipts = self._build_bcif_preview_receipts(telemetry_receipts)

            receipts_root = _salad_runtime_receipts_root(output_gcs_prefix, job_id)
            evidencegate_derivation = not bool(canonical_result)
            terminal_projection = dict(canonical_result or {})
            if not terminal_projection and artifact_manifest.get("completed_marker_confirmed"):
                terminal_projection = self._build_terminal_projection_from_evidence(
                    job_id=job_id,
                    route_decision_id=route_decision_id,
                    provider_instance_id=provider_instance_id,
                    output_gcs_prefix=output_gcs_prefix,
                    artifact_manifest=artifact_manifest,
                    readback_manifest=readback_manifest,
                    telemetry_receipts=telemetry_receipts,
                    smic_summary=guarded_smic_summary,
                    evidencegate_derivation=True,
                )
            if terminal_projection:
                terminal_projection.setdefault("projection", {})
                terminal_projection["projection"]["terminal_projection_from_evidencegate"] = bool(evidencegate_derivation)
                terminal_projection["projection"]["evidencegate_derivation"] = bool(evidencegate_derivation)
                terminal_projection["projection"]["produced_at"] = datetime.now(timezone.utc).isoformat()
                terminal_projection["projection"]["route_decision_id_source"] = route_source
                terminal_projection.setdefault("job", {})
                terminal_projection["job"]["route_decision_id"] = route_decision_id
                terminal_projection.setdefault("provider", {})
                terminal_projection["provider"]["name"] = "salad"
                terminal_projection["provider"]["instance_id"] = provider_instance_id
                terminal_projection["provider"]["output_gcs_prefix"] = output_gcs_prefix
                terminal_projection.setdefault("artifacts", {})
                terminal_projection["artifacts"]["output_gcs_prefix"] = output_gcs_prefix
                terminal_projection["artifacts"]["completed_marker_confirmed"] = bool(artifact_manifest.get("completed_marker_confirmed"))
                terminal_projection["artifacts"]["failure_receipt_present"] = bool(artifact_manifest.get("failure_receipt_present"))
                terminal_projection["artifacts"]["artifact_manifest_refs"] = [entry.get("object_uri") for entry in list(readback_manifest.get("artifacts") or [])]
                terminal_projection["artifacts"]["readback_summary"] = dict(readback_manifest.get("summary") or {})
                terminal_projection["artifacts"]["telemetry_receipt_refs"] = dict(telemetry_receipts.get("receipt_refs") or {})
                terminal_projection["artifacts"]["smic_summary"] = dict(guarded_smic_summary or {})
                terminal_projection.setdefault("smic", {})
                terminal_projection["smic"]["status"] = str(guarded_smic_summary.get("status") or terminal_projection["smic"].get("status") or "")
                terminal_projection["smic"]["summary"] = dict(guarded_smic_summary or {})

            live_metric_receipts = dict(guarded_smic_summary.get("live_metric_receipts") or {})
            metric_receipts_list = list(guarded_smic_summary.get("metric_receipts") or [])
            if not metric_receipts_list:
                metric_receipts_list = list(live_metric_receipts.get("metric_receipts") or [])
            smic_metric_receipts_payload = {
                "schema_id": "mica.smic.metric_receipts.v1",
                "job_id": job_id,
                "route_decision_id": route_decision_id,
                "provider": "salad",
                "provider_instance_id": provider_instance_id,
                "gcs_prefix": output_gcs_prefix,
                "status": str(guarded_smic_summary.get("status") or live_metric_receipts.get("status") or ""),
                "no_fake_metric": True,
                "metric_receipts": metric_receipts_list,
                "summary": dict(guarded_smic_summary or {}),
            }

            upload_specs = {
                "stream_session_receipt": telemetry_receipts.get("stream_session_receipt"),
                "telemetry_event_receipts": telemetry_receipts.get("telemetry_event_receipts"),
                "trajectory_frame_event_receipts": telemetry_receipts.get("trajectory_frame_event_receipts"),
                "bcif_preview_receipts": bcif_preview_receipts,
                "artifact_transmission_receipts": telemetry_receipts.get("artifact_transmission_receipts"),
                "last_value_snapshot": telemetry_receipts.get("last_value_snapshot"),
                "cadence_policy": telemetry_receipts.get("cadence_policy"),
                "readback_manifest": readback_manifest,
                "smic_metric_receipts": smic_metric_receipts_payload,
                "md_execution_result_v1": terminal_projection,
            }
            for key, payload in upload_specs.items():
                if not payload:
                    continue
                object_path = f"{receipts_root}/{key}.json"
                object_uri = storage.upload_text(
                    user_id=user_id,
                    object_path=object_path,
                    text=json.dumps(payload, indent=2, sort_keys=True),
                    content_type="application/json",
                    metadata={
                        "job_id": job_id,
                        "provider": "salad",
                        "route_decision_id": route_decision_id,
                        "terminal_projection_from_evidencegate": str(evidencegate_derivation).lower(),
                    },
                )
                receipt_paths[key] = object_uri

            receipt_paths["telemetry_source"] = telemetry_receipts.get("telemetry_source")
            receipt_paths["native_live_stream"] = bool(telemetry_receipts.get("native_live_stream", False))
            receipt_paths["route_decision_id"] = route_decision_id
            receipt_paths["route_decision_id_source"] = route_source
            receipt_paths["readback_manifest"] = readback_manifest
            receipt_paths["smic_summary"] = guarded_smic_summary
            if terminal_projection:
                summary.metadata[RESULT_SCHEMA_VERSION] = terminal_projection
                summary.metadata["canonical_status"] = str(
                    terminal_projection.get("status", {}).get("state", "") or summary.metadata.get("canonical_status", "")
                )
        except Exception as exc:
            logger.debug("Salad runtime receipt persistence failed for %s: %s", job_id, exc)
        return receipt_paths

    @staticmethod
    def _parse_live_metric_csv(
        csv_path: Path,
        *,
        metric_key: str,
        preferred_columns: List[str],
        aggregate: str = "last",
    ) -> Dict[str, Any]:
        if not csv_path.exists():
            return {}

        with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            headers = [str(name or "").strip() for name in (reader.fieldnames or [])]

        if not rows or not headers:
            return {}

        metric_column = ""
        normalized_headers = {name.lower(): name for name in headers}
        for candidate in preferred_columns:
            resolved = normalized_headers.get(candidate.lower())
            if resolved:
                metric_column = resolved
                break
        if not metric_column:
            metric_candidates = [
                name for name in headers
                if metric_key.lower() in name.lower()
            ]
            metric_column = metric_candidates[-1] if metric_candidates else headers[-1]
        metric_values: List[float] = []
        for row in rows:
            try:
                metric_values.append(float(str(row.get(metric_column, "")).strip()))
            except (TypeError, ValueError):
                continue
        if not metric_values:
            return {}

        aggregate_name = str(aggregate or "last").strip().lower()
        if aggregate_name == "mean":
            metric_value = sum(metric_values) / float(len(metric_values))
            last_row = rows[-1]
        elif aggregate_name == "max":
            metric_value = max(metric_values)
            last_row = rows[-1]
        else:
            last_row = rows[-1]
            try:
                metric_value = float(str(last_row.get(metric_column, "")).strip())
            except (TypeError, ValueError):
                return {}

        frame_index = None
        for key in ("frame", "frame_index", "index"):
            try:
                frame_index = int(float(str(last_row.get(key, "")).strip()))
                break
            except (TypeError, ValueError):
                continue

        time_ps = None
        if "time_ps" in last_row:
            try:
                time_ps = float(str(last_row.get("time_ps", "")).strip())
            except (TypeError, ValueError):
                time_ps = None
        if time_ps is None and "time_ns" in last_row:
            try:
                time_ps = float(str(last_row.get("time_ns", "")).strip()) * 1000.0
            except (TypeError, ValueError):
                time_ps = None
        if time_ps is None and "time" in last_row:
            try:
                time_ps = float(str(last_row.get("time", "")).strip()) * 1000.0
            except (TypeError, ValueError):
                time_ps = None

        return {
            "value": metric_value,
            "metric_key": metric_key,
            "metric_column": metric_column,
            "frame_index": frame_index,
            "time_ps": time_ps,
            "n_rows": len(rows),
            "aggregate": aggregate_name,
        }

    async def _maybe_publish_live_salad_smic_metrics(
        self,
        *,
        user_id: str,
        job_id: str,
        bucket_name: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        summary: ComputeJobSummary,
    ) -> Dict[str, Any]:
        progress = dict(artifact_manifest.get("simulation_progress") or {})
        try:
            progress_ns = float(progress.get("progress_ns"))
        except (TypeError, ValueError):
            return dict((summary.metadata or {}).get("live_smic_metrics") or {})

        metadata = dict(summary.metadata or {})
        live_cache = dict(metadata.get("live_smic_metrics") or {})
        now = time.time()
        try:
            last_progress_ns = float(live_cache.get("last_progress_ns"))
        except (TypeError, ValueError):
            last_progress_ns = -1.0
        try:
            last_published_at = float(live_cache.get("last_published_at"))
        except (TypeError, ValueError):
            last_published_at = 0.0

        if last_progress_ns >= 0 and (progress_ns - last_progress_ns) < _SALAD_LIVE_SMIC_MIN_PROGRESS_NS:
            return live_cache
        if last_published_at and (now - last_published_at) < _SALAD_LIVE_SMIC_MIN_INTERVAL_SECONDS:
            return live_cache

        selected = self._select_salad_terminal_smic_inputs(list(artifact_manifest.get("object_listing") or []))
        topology_object = selected.get("topology_object") or ""
        trajectory_object = selected.get("trajectory_object") or ""
        if not topology_object or not trajectory_object:
            return live_cache

        route_decision_id, _route_source = self._infer_route_decision_id(job_id, summary)
        common_identity = self._salad_common_event_identity(
            summary=summary,
            output_gcs_prefix=output_gcs_prefix,
            route_decision_id=route_decision_id,
        )

        def _object_uri(object_path: str) -> str:
            if output_gcs_prefix.startswith("gs://"):
                return f"gs://{output_gcs_prefix[5:].split('/', 1)[0]}/{object_path}"
            return object_path

        object_sizes = {
            str(item.get("name") or "").strip(): int(item.get("size", 0) or 0)
            for item in list(artifact_manifest.get("object_listing") or [])
        }
        trajectory_size = object_sizes.get(trajectory_object, 0)
        topology_size = object_sizes.get(topology_object, 0)

        try:
            from mica.api_v1.routers.smic import BundleRequest, execute_analysis_bundle_job
            from mica.storage.gcs_user_storage import get_storage_manager
            from mica.ws_md import publish_smic_metric_event

            storage = get_storage_manager()
            runtime_root = Path.cwd() / ".mica" / "runtime" / "live_smic_metrics" / job_id
            input_dir = runtime_root / "input"
            output_dir = runtime_root / "output"
            input_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)

            local_topology = input_dir / Path(topology_object).name
            local_trajectory = input_dir / Path(trajectory_object).name
            storage.download_file(user_id=user_id, object_path=topology_object, local_path=local_topology)
            storage.download_file(user_id=user_id, object_path=trajectory_object, local_path=local_trajectory)

            topology_atoms: Optional[int] = None
            trajectory_atoms: Optional[int] = None
            try:
                import mdtraj as md

                topology_atoms = int(getattr(md.load_topology(str(local_topology)), "n_atoms", 0) or 0) or None
                with md.open(str(local_trajectory)) as handle:
                    xyz, _cell_lengths, _cell_angles = handle.read(n_frames=1)
                    shape = getattr(xyz, "shape", ())
                    trajectory_atoms = int(shape[1]) if len(shape) >= 2 else None
            except Exception:
                topology_atoms = None
                trajectory_atoms = None

            requested_metrics = ["rmsd", "rmsf", "contacts"]
            _registered_metrics, skipped_registry = self._filter_terminal_smic_analyses_for_registry(requested_metrics)
            applicable_metrics, skipped_topology = self._filter_terminal_smic_analyses_for_topology(
                requested_metrics,
                local_topology,
                local_trajectory,
            )
            skipped_metrics: Dict[str, str] = {}
            skipped_metrics.update(skipped_registry)
            skipped_metrics.update(skipped_topology)
            metric_receipts = list(live_cache.get("metric_receipts") or [])

            for metric_key, failure_code in sorted(skipped_metrics.items()):
                metric_status = "inapplicable" if "inapplicable" in str(failure_code) else "degraded"
                receipt = {
                    "event_type": "smic_metric",
                    "job_id": job_id,
                    **common_identity,
                    "metric_name": metric_key,
                    "metric_key": metric_key,
                    "metric_status": metric_status,
                    "value": None,
                    "value_ref": "",
                    "window_start_ps": None,
                    "window_end_ps": progress_ns * 1000.0,
                    "source_topology_ref": _object_uri(topology_object),
                    "source_trajectory_ref": _object_uri(trajectory_object),
                    "topology_atoms": topology_atoms,
                    "trajectory_atoms": trajectory_atoms,
                    "output_artifact_refs": [],
                    "failure_code": failure_code,
                    "no_fake_metric": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                metric_receipts.append(receipt)
                publish_smic_metric_event(
                    job_id,
                    metric_key=metric_key,
                    value=None,
                    unit="",
                    metric_status=metric_status,
                    time_ps=progress_ns * 1000.0,
                    window_end_ps=progress_ns * 1000.0,
                    source_topology_ref=_object_uri(topology_object),
                    source_trajectory_ref=_object_uri(trajectory_object),
                    topology_atoms=topology_atoms,
                    trajectory_atoms=trajectory_atoms,
                    output_artifact_refs=[],
                    failure_code=failure_code,
                    run_id=job_id,
                    metadata={
                        "source_surface": "salad_live_smic_guard",
                        "bucket_name": bucket_name,
                        "topology_object": topology_object,
                        "trajectory_object": trajectory_object,
                    },
                    **common_identity,
                )
                live_cache[metric_key] = receipt

            analyses_to_run = [name for name in _DEFAULT_LIVE_SMIC_ANALYSES if name in set(applicable_metrics)]
            if not analyses_to_run:
                live_cache.update(
                    {
                        "metric_receipts": metric_receipts,
                        "last_metric_keys": [],
                        "last_progress_ns": progress_ns,
                        "last_published_at": now,
                        "topology_object": topology_object,
                        "trajectory_object": trajectory_object,
                        "topology_atoms": topology_atoms,
                        "trajectory_atoms": trajectory_atoms,
                        "status": "degraded",
                    }
                )
                return live_cache

            req = BundleRequest(
                analyses=analyses_to_run,
                topology=str(local_topology),
                trajectories=[str(local_trajectory)],
                trajectory=str(local_trajectory),
                output_root=str(output_dir),
                label="live_metrics",
                stride=1,
                timeout=1200,
                per_module_extra={"rmsd": {"skip_plots": True}},
            )
            bundle_result = await execute_analysis_bundle_job(
                req,
                user_id=user_id,
                bundle_id=f"{job_id}-live-smic",
            )

            metric_specs = {
                "rmsd": {
                    "analysis_key": "rmsd",
                    "unit": "angstrom",
                    "preferred_columns": ["rmsd"],
                    "source_surface": "salad_live_smic_rmsd",
                    "series_key": "rmsd_angstrom",
                    "aggregate": "last",
                },
                "contacts": {
                    "analysis_key": "contacts",
                    "unit": "count",
                    "preferred_columns": ["contacts", "total_contacts", "n_contacts"],
                    "source_surface": "salad_live_smic_contacts",
                    "series_key": "contacts_count",
                    "aggregate": "last",
                },
                "rmsf": {
                    "analysis_key": "rmsd",
                    "unit": "angstrom",
                    "preferred_columns": ["rmsf_A"],
                    "source_surface": "salad_live_smic_rmsf_mean",
                    "series_key": "rmsf_mean_angstrom",
                    "aggregate": "mean",
                },
            }

            published_metric_keys: List[str] = []
            results_by_analysis = dict(bundle_result.get("results_by_analysis") or {})
            for metric_key, spec in metric_specs.items():
                if metric_key in skipped_metrics:
                    continue
                analysis_result = dict(results_by_analysis.get(str(spec["analysis_key"])) or {})
                output_root = Path(str(analysis_result.get("output_dir") or "")).expanduser()
                csv_files = sorted(
                    file_path for file_path in output_root.rglob("*.csv")
                    if metric_key in file_path.name.lower()
                    or any(candidate in file_path.name.lower() for candidate in spec["preferred_columns"])
                )
                if not csv_files:
                    continue

                parsed = self._parse_live_metric_csv(
                    csv_files[0],
                    metric_key=metric_key,
                    preferred_columns=list(spec["preferred_columns"]),
                    aggregate=str(spec.get("aggregate") or "last"),
                )
                if not parsed:
                    continue

                time_ps = parsed.get("time_ps")
                if time_ps is None:
                    time_ps = progress_ns * 1000.0
                metric_value = float(parsed["value"])
                frame_index = parsed.get("frame_index")
                published_metric_keys.append(metric_key)

                publish_smic_metric_event(
                    job_id,
                    metric_key=metric_key,
                    value=metric_value,
                    unit=str(spec["unit"]),
                    metric_status="completed",
                    frame_index=frame_index if isinstance(frame_index, int) else None,
                    time_ps=float(time_ps),
                    window_end_ps=float(time_ps),
                    source_topology_ref=_object_uri(topology_object),
                    source_trajectory_ref=_object_uri(trajectory_object),
                    topology_atoms=topology_atoms,
                    trajectory_atoms=trajectory_atoms,
                    output_artifact_refs=[str(csv_files[0])],
                    run_id=job_id,
                    series_point={
                        "progress_ns": progress_ns,
                        str(spec["series_key"]): metric_value,
                        "speed_ns_per_day": progress.get("speed_ns_per_day"),
                    },
                    metadata={
                        "source_surface": str(spec["source_surface"]),
                        "provider": "salad",
                        "topology_object": topology_object,
                        "trajectory_object": trajectory_object,
                        "trajectory_size_bytes": trajectory_size,
                        "topology_size_bytes": topology_size,
                        "bucket_name": bucket_name,
                        "output_gcs_prefix": output_gcs_prefix,
                        "csv_path": str(csv_files[0]),
                        "metric_column": parsed.get("metric_column"),
                        "n_rows": parsed.get("n_rows"),
                        "aggregate": parsed.get("aggregate"),
                        "bundle_id": str(bundle_result.get("bundle_id") or ""),
                    },
                    **common_identity,
                )

                metric_receipt = {
                    "event_type": "smic_metric",
                    "job_id": job_id,
                    **common_identity,
                    "metric_name": metric_key,
                    "metric_key": metric_key,
                    "metric_status": "completed",
                    "value": metric_value,
                    "value_ref": str(csv_files[0]),
                    "unit": str(spec["unit"]),
                    "frame_index": frame_index,
                    "time_ps": float(time_ps),
                    "window_start_ps": None,
                    "window_end_ps": float(time_ps),
                    "source_topology_ref": _object_uri(topology_object),
                    "source_trajectory_ref": _object_uri(trajectory_object),
                    "topology_atoms": topology_atoms,
                    "trajectory_atoms": trajectory_atoms,
                    "output_artifact_refs": [str(csv_files[0])],
                    "failure_code": "",
                    "no_fake_metric": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "progress_ns": progress_ns,
                    "speed_ns_per_day": progress.get("speed_ns_per_day"),
                    "csv_path": str(csv_files[0]),
                    "aggregate": parsed.get("aggregate"),
                }
                metric_receipts.append(metric_receipt)
                live_cache[metric_key] = metric_receipt

            if not published_metric_keys:
                return live_cache

            live_cache.update(
                {
                    "last_metric_keys": published_metric_keys,
                    "last_progress_ns": progress_ns,
                    "last_published_at": now,
                    "topology_object": topology_object,
                    "trajectory_object": trajectory_object,
                    "topology_atoms": topology_atoms,
                    "trajectory_atoms": trajectory_atoms,
                    "trajectory_size_bytes": trajectory_size,
                    "metric_receipts": metric_receipts,
                    "status": "completed" if published_metric_keys else "degraded",
                    "bundle_id": str(bundle_result.get("bundle_id") or ""),
                }
            )
            return live_cache
        except Exception as exc:
            logger.debug("Live Salad SMIC metric publish failed for %s (non-fatal): %s", job_id, exc)
            live_cache["last_error"] = str(exc)
            return live_cache
    @staticmethod
    def _rehydrate_existing_salad_terminal_smic_post_analysis(
        *,
        bucket_name: str,
        job_id: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        object_listing = list(artifact_manifest.get("object_listing") or [])
        packet_id = _salad_terminal_bundle_packet_id(output_gcs_prefix, job_id)
        bundle_prefix = f"{_salad_terminal_bundle_root(output_gcs_prefix, job_id).rstrip('/')}/"
        bundle_entries = []
        analyses = set()
        object_uris: List[str] = []
        total_size_bytes = 0

        for item in object_listing:
            object_name = str(item.get("name") or "").strip()
            if not object_name.startswith(bundle_prefix):
                continue
            relative_path = object_name[len(bundle_prefix):]
            if not relative_path or relative_path.endswith("/"):
                continue
            parts = relative_path.split("/", 1)
            if len(parts) < 2:
                continue
            analysis_name = parts[0].strip().lower()
            if analysis_name:
                analyses.add(analysis_name)
            size_bytes = int(item.get("size", 0) or 0)
            total_size_bytes += size_bytes
            object_uri = f"gs://{bucket_name}/{object_name}"
            object_uris.append(object_uri)
            bundle_entries.append(
                {
                    "relative_path": relative_path,
                    "object_path": object_name,
                    "object_uri": object_uri,
                    "size_bytes": size_bytes,
                }
            )

        if not bundle_entries:
            return {}

        analyses_list = sorted(analyses)
        from mica.api_v1.routers.user_bucket import _durability_class_from_download_url_present

        return {
            "required": True,
            "status": "completed",
            "analyses": analyses_list,
            "bundle_id": f"{packet_id}-terminal-smic",
            "bundle_output_root": "",
            "topology_object": "",
            "trajectory_object": "",
            "completed": analyses_list,
            "failed": [],
            "promotion_receipt": {
                "schema_id": "mica.gcs_user_artifact_promotion_receipt.v1",
                "protocol_id": packet_id,
                "node_id": "terminal_smic_bundle",
                "source_session_id": packet_id,
                "source_kind": "smic",
                "source_node_id": "terminal_smic_bundle",
                "source_is_directory": True,
                "root_object_prefix": bundle_prefix.rstrip("/"),
                "object_uris": object_uris,
                "bundle_entries": bundle_entries,
                "size_bytes": total_size_bytes,
                "entry_count": len(bundle_entries),
                "durability_class": _durability_class_from_download_url_present(False),
            },
            "artifact_refs": object_uris,
            "evidence_refs": [f"compute://jobs/{packet_id}/terminal_smic_bundle_rehydrated"],
            "rehydrated_from_existing_bundle": True,
            "durable_packet_id": packet_id,
        }

    async def _ensure_salad_terminal_smic_post_analysis(
        self,
        *,
        user_id: str,
        job_id: str,
        bucket_name: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        context: Dict[str, Any],
        execution_request: Optional[Dict[str, Any]],
        existing_summary: Optional[ComputeJobSummary] = None,
        canonical_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not artifact_manifest.get("completed_marker_confirmed"):
            return {}
        if artifact_manifest.get("failure_receipt_present"):
            return {}

        existing_summary_metadata = dict((existing_summary.metadata if existing_summary is not None else {}) or {})
        existing_summary_manifest = dict(existing_summary_metadata.get("artifact_manifest") or {})
        existing_summary_result = dict(existing_summary_metadata.get(RESULT_SCHEMA_VERSION) or {})

        existing_candidates = [
            artifact_manifest.get("smic_post_analysis"),
            ((canonical_result or {}).get("artifacts") or {}).get("smic_post_analysis"),
            existing_summary_manifest.get("smic_post_analysis"),
            (existing_summary_result.get("artifacts") or {}).get("smic_post_analysis"),
        ]
        for candidate in existing_candidates:
            if isinstance(candidate, dict) and candidate:
                return dict(candidate)

        rehydrated = self._rehydrate_existing_salad_terminal_smic_post_analysis(
            bucket_name=bucket_name,
            job_id=job_id,
            output_gcs_prefix=output_gcs_prefix,
            artifact_manifest=artifact_manifest,
        )
        if rehydrated:
            return rehydrated

        return await self._run_salad_terminal_smic_post_analysis(
            user_id=user_id,
            job_id=job_id,
            output_gcs_prefix=output_gcs_prefix,
            artifact_manifest=artifact_manifest,
            context=context,
            execution_request=execution_request,
        )

    async def _run_salad_terminal_smic_post_analysis(
        self,
        *,
        user_id: str,
        job_id: str,
        output_gcs_prefix: str,
        artifact_manifest: Dict[str, Any],
        context: Dict[str, Any],
        execution_request: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        required = True
        if not artifact_manifest.get("completed_marker_confirmed"):
            return {"required": required, "status": "skipped_incomplete_md"}
        if artifact_manifest.get("failure_receipt_present"):
            return {"required": required, "status": "skipped_failed_md"}

        analyses = self._resolve_terminal_smic_bundle_analyses(context, execution_request)
        if not analyses:
            return {"required": required, "status": "disabled", "analyses": []}

        selected = self._select_salad_terminal_smic_inputs(list(artifact_manifest.get("object_listing") or []))
        topology_object = selected.get("topology_object") or ""
        trajectory_object = selected.get("trajectory_object") or ""
        if not topology_object or not trajectory_object:
            return {
                "required": required,
                "status": "failed",
                "analyses": analyses,
                "error": "salad terminal SMIC post-analysis could not resolve topology/trajectory objects",
            }

        try:
            from mica.api_v1.routers.smic import BundleRequest, execute_analysis_bundle_job
            from mica.api_v1.routers.user_bucket import promote_workspace_artifact_payload
            from mica.storage.gcs_user_storage import get_storage_manager

            storage = get_storage_manager()
            durable_packet_id = _salad_terminal_bundle_packet_id(output_gcs_prefix, job_id)
            durable_bundle_root = _salad_terminal_bundle_root(output_gcs_prefix, job_id)
            runtime_root = Path.cwd() / ".mica" / "runtime" / "terminal_smic_post" / job_id
            input_dir = runtime_root / "input"
            bundle_root = runtime_root / "bundle"
            input_dir.mkdir(parents=True, exist_ok=True)
            bundle_root.mkdir(parents=True, exist_ok=True)

            local_topology = input_dir / Path(topology_object).name
            local_trajectory = input_dir / Path(trajectory_object).name
            storage.download_file(user_id=user_id, object_path=topology_object, local_path=local_topology)
            storage.download_file(user_id=user_id, object_path=trajectory_object, local_path=local_trajectory)
            analyses, skipped_registry = self._filter_terminal_smic_analyses_for_registry(analyses)
            analyses, skipped_analyses = self._filter_terminal_smic_analyses_for_topology(
                analyses,
                local_topology,
                local_trajectory,
            )
            skipped_analyses.update(skipped_registry)
            if not analyses:
                return {
                    "required": required,
                    "status": "skipped_inapplicable",
                    "analyses": [],
                    "skipped_analyses": skipped_analyses,
                    "topology_object": topology_object,
                    "trajectory_object": trajectory_object,
                    "durable_packet_id": durable_packet_id,
                }

            bundle_req = BundleRequest(
                analyses=analyses,
                topology=str(local_topology),
                trajectories=[str(local_trajectory)],
                trajectory=str(local_trajectory),
                output_root=str(bundle_root),
                label=f"{durable_packet_id}_terminal_smic",
                stride=1,
                timeout=3600,
            )
            bundle_result = await execute_analysis_bundle_job(
                bundle_req,
                user_id=user_id,
                bundle_id=f"{durable_packet_id}-terminal-smic",
            )
            bundle_output_root = Path(str(bundle_result.get("bundle_output_root") or "")).expanduser()
            bucket_info = storage.ensure_bucket(user_id)
            promotion_payload = promote_workspace_artifact_payload(
                storage=storage,
                user_id=user_id,
                bucket_info=bucket_info,
                source_path=bundle_output_root,
                protocol_id=durable_packet_id,
                node_id="terminal_smic_bundle",
                session_id=durable_packet_id,
                source_kind="smic",
                workspace_prefix=durable_bundle_root,
                metadata_payload={
                    "job_id": job_id,
                    "durable_packet_id": durable_packet_id,
                    "output_gcs_prefix": output_gcs_prefix,
                    "provider": "salad",
                    "source_kind": "smic",
                    "post_step": "terminal_smic_bundle",
                },
                source_session_id=durable_packet_id,
                source_node_id="terminal_smic_bundle",
                object_path_hint=durable_bundle_root,
                binding_surface="compute_terminal_smic",
            )
            return {
                "required": required,
                "status": "completed" if not bundle_result.get("failed") else "partial_failure",
                "analyses": analyses,
                "bundle_id": str(bundle_result.get("bundle_id") or f"{durable_packet_id}-terminal-smic"),
                "bundle_output_root": str(bundle_output_root),
                "topology_object": topology_object,
                "trajectory_object": trajectory_object,
                "skipped_analyses": skipped_analyses,
                "completed": list(bundle_result.get("completed") or []),
                "failed": list(bundle_result.get("failed") or []),
                "promotion_receipt": dict(
                    (promotion_payload.get("state_after") or {}).get("promotion_receipt") or {}
                ),
                "artifact_refs": list(promotion_payload.get("artifact_refs") or []),
                "evidence_refs": list(promotion_payload.get("evidence_refs") or []),
                "durable_packet_id": durable_packet_id,
            }
        except Exception as exc:
            logger.exception("Salad terminal SMIC post-analysis failed for %s", job_id)
            return {
                "required": required,
                "status": "failed",
                "analyses": analyses,
                "topology_object": topology_object,
                "trajectory_object": trajectory_object,
                "error": str(exc),
            }

    async def _stage_pdb_to_gcs(self, user_id: str, pdb_path: str, job_id: str) -> str:
        """Upload a local PDB file to the user's GCS bucket. Returns gs:// URI."""
        return await self._stage_input_file_to_gcs(
            user_id=user_id,
            local_path=pdb_path,
            job_id=job_id,
            object_name="protein.pdb",
            content_type="chemical/x-pdb",
            sanitize_pdb=True,
        )

    async def _stage_input_file_to_gcs(
        self,
        *,
        user_id: str,
        local_path: str,
        job_id: str,
        object_name: str,
        content_type: str,
        sanitize_pdb: bool,
    ) -> str:
        """Upload a worker input file to the user's GCS bucket. Returns gs:// URI."""
        if str(local_path).startswith("gs://"):
            return str(local_path)

        input_file = Path(local_path)
        if not input_file.exists():
            raise RuntimeError(f"Worker input file not found for GCS staging: {local_path}")

        from .storage.gcs_user_storage import get_storage_manager

        storage = get_storage_manager()
        object_path = f"md-jobs/{job_id}/input/{object_name}"
        payload = input_file.read_bytes()
        if sanitize_pdb:
            payload = _sanitize_remote_md_pdb_bytes(payload)
        gcs_uri = storage.upload_bytes(
            user_id=user_id,
            object_path=object_path,
            data=payload,
            content_type=content_type,
        )
        logger.info("📤 Worker input staged to GCS: %s", gcs_uri)
        return gcs_uri

    async def _probe_salad_artifact_manifest(
        self, user_id: str, output_gcs_prefix: str, job_id: str
    ) -> Dict[str, Any]:
        """Probe GCS for Salad worker outputs. Returns artifact manifest dict."""
        manifest: Dict[str, Any] = {
            "completed_marker_confirmed": False,
            "dcd_chunk_count": 0,
            "history_json_present": False,
            "worker_history_json_present": False,
            "latest_status_json_present": False,
            "failure_receipt_present": False,
            "failure_traceback_present": False,
            "object_listing": [],
            "output_gcs_prefix": output_gcs_prefix,
            "job_id": job_id,
        }
        try:
            from .storage.gcs_user_storage import get_storage_manager

            prefix_path = output_gcs_prefix
            if prefix_path.startswith("gs://"):
                parts = prefix_path[5:].split("/", 1)
                prefix_path = parts[1] if len(parts) > 1 else ""

            storage = get_storage_manager()
            objects = storage.list_objects(
                user_id=user_id, prefix=prefix_path, max_results=1000
            )
            manifest["object_listing"] = [
                {"name": o["name"], "size": o.get("size", 0)} for o in objects
            ]
            normalized_prefix = str(prefix_path or "").strip().rstrip("/")
            root_prefix = f"{normalized_prefix}/" if normalized_prefix else ""
            output_prefix = f"{root_prefix}output/"
            names = {str(o["name"]) for o in objects}
            manifest["dcd_chunk_count"] = sum(
                1
                for n in names
                if n.startswith(output_prefix)
                and n.endswith(".dcd")
                and "/" not in n[len(output_prefix):]
            )
            manifest["completed_marker_confirmed"] = f"{root_prefix}completed.marker" in names
            manifest["history_json_present"] = f"{output_prefix}history.json" in names
            manifest["worker_history_json_present"] = f"{output_prefix}worker_history.json" in names
            manifest["latest_status_json_present"] = f"{output_prefix}latest_status.json" in names
            manifest["failure_receipt_present"] = f"{output_prefix}failure_receipt.json" in names
            manifest["failure_traceback_present"] = f"{output_prefix}failure_traceback.txt" in names

            simulation_logs = sorted(
                n
                for n in names
                if n.startswith(output_prefix)
                and n.endswith("_simulation.log")
                and "/" not in n[len(output_prefix):]
            )
            if simulation_logs:
                latest_simulation_log = simulation_logs[-1]
                manifest["latest_simulation_log_object"] = latest_simulation_log
                try:
                    log_payload = storage.read_text_best_effort(
                        user_id=user_id,
                        object_path=latest_simulation_log,
                        max_chars=120_000,
                    )
                    progress = self._extract_salad_simulation_progress(
                        str(log_payload.get("text") or "")
                    )
                    if progress:
                        manifest["simulation_progress"] = progress
                except Exception as exc:
                    logger.debug(
                        "Salad simulation progress probe failed for %s (non-fatal): %s",
                        latest_simulation_log,
                        exc,
                    )
        except Exception as exc:
            logger.warning("Salad artifact manifest probe failed (non-fatal): %s", exc)
        return manifest

    @staticmethod
    def _extract_salad_simulation_progress(log_text: str) -> Dict[str, Any]:
        matches = list(_SALAD_PROGRESS_RE.finditer(str(log_text or "")))
        if not matches:
            return {}
        latest = matches[-1]
        return {
            "progress_percent": float(latest.group("percent")),
            "progress_ns": float(latest.group("ns")),
            "speed_ns_per_day": float(latest.group("speed")),
        }
