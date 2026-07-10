"""
salad_gcs_orchestrator.py — SaladCloud + GCS Checkpoint Orchestrator

Manages the lifecycle of an OpenMM MD simulation running on a SaladCloud
Single-Replica Container Group (SRCG) with GCS checkpoint streaming.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                   SaladGCSOrchestrator                       │
    ├─────────────────────────────────────────────────────────────┤
    │  SaladProvider           GCS bucket (mica-compute)           │
    │       │                        │                             │
    │  Container Group (SRCG)        │                             │
    │  ┌─────────────────────┐       │                             │
    │  │  OpenMM worker      │       │                             │
    │  │  ├── download PDB ──┼───────┘  gs://{bucket}/{prefix}/   │
    │  │  ├── download CPT   │              input/{pdb_file}        │
    │  │  ├── run chunk      │              checkpoint.cpt         │
    │  │  ├── upload DCD ────┼──────────►  output/dcd_{step}.dcd  │
    │  │  ├── upload log ────┼──────────►  output/log_{step}.txt  │
    │  │  └── upload CPT ────┼──────────►  checkpoint.cpt         │
    │  └─────────────────────┘                                     │
    └─────────────────────────────────────────────────────────────┘

Checkpoint loop (per-chunk):
  1. Container downloads input PDB + latest checkpoint.cpt from GCS
  2. Runs N steps (N calibrated via dynamic benchmark for ~30-min chunks)
  3. Saves checkpoint.cpt + DCD + log → uploads all 3 to GCS
  4. Loops until max_steps reached; then writes completed.marker → exits 0
  5. Salad CG transitions RUNNING → STOPPED/SUCCEEDED
  6. MICA detects STOPPED and marks job COMPLETED

Monitoring strategy (MICA side):
  - Poll Salad CG status every poll_interval_seconds
  - Poll GCS for completed.marker (completion sentinel) every poll_interval_seconds
  - Also track latest checkpoint.cpt mtime for progress telemetry
  - On FAILED or ERROR: destroy CG; surface error

GCS credentials in the container:
  - Pass GOOGLE_APPLICATION_CREDENTIALS_JSON_B64 env var (base64-encoded SA JSON)
  - OR use GCS S3 interoperability endpoint with HMAC keys (boto3-compatible)
  - OR use ADC if Salad ever supports Workload Identity (future)
  - MICA injects GCS_CREDENTIALS_JSON_B64 via env_vars at CG creation time

Reference:
  .mica/external_docs/salad/container-engine/how-to-guides/molecular-dynamics-simulation/openmm-srcg.mdx
  .mica/external_docs/salad/api-specs/salad-cloud.yaml
  https://docs.salad.com/reference/saladcloud-api/container-groups
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..compute_image_contract import (
    canonical_md_worker_image,
    default_salad_worker_command,
)
from .salad_bootstrap_policy import (
    SaladBootstrapObservation,
    SaladBootstrapPolicy,
    build_policy_receipts,
    build_provider_stall_receipt,
    build_same_class_reallocation_receipt,
    classify_salad_bootstrap,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "SaladGCSOrchestrator: invalid integer for %s=%r; using default %d",
            name,
            raw,
            default,
        )
        return default

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class SaladMDJobConfig:
    """
    Configuration for a SaladCloud OpenMM MD job.

    All GCS paths use gs://bucket/prefix/... notation.
    env_extra can inject any additional env vars into the container.
    """
    # Input PDB already uploaded to GCS before job submission
    pdb_gcs_path: str                       # e.g. "gs://mica-compute/inputs/1ubq.pdb"
    # Output GCS prefix for DCD, log, and checkpoint files
    output_gcs_prefix: str                  # e.g. "gs://mica-compute/salad-jobs/job-abc/"
    # Docker image to run on Salad (must include OpenMM + google-cloud-storage)
    docker_image: str = field(default_factory=canonical_md_worker_image)
    docker_command: str = field(default_factory=default_salad_worker_command)
    # Simulation steps
    max_steps: int = 50_000_000
    benchmark_steps: int = 5_000
    report_freq: int = 500
    # Chunk duration target (container uses this via dynamic benchmark)
    saving_interval_seconds: int = 600      # 10 min per chunk
    # Unresponsive watchdog: container reallocates if main thread stalls
    max_no_response_time: int = 3600        # 1 hour
    # GPU type preference
    gpu_type_str: str = "RTX_5090"
    # Route identity and bootstrap policy knobs
    route_decision_id: Optional[str] = None
    docker_image_size_gb: float = 8.0
    max_same_class_reallocation_attempts: int = 2
    degraded_smoke_fallback: bool = False
    pre_destroy_inspection_enabled: bool = True
    preserve_failed_cg_on_missing_bootstrap_evidence: bool = False
    # ponytail: per-job deploy timeout, overrides env var. 0 = use env default.
    deploy_timeout_seconds: int = 0
    # Job ID (auto-generated if not provided)
    job_id: Optional[str] = None
    # GCS credentials: base64-encoded Google service account JSON
    # Set SALAD_GCS_CREDENTIALS_B64 env var or pass here
    gcs_credentials_b64: Optional[str] = None
    # Additional env vars to inject
    env_extra: Dict[str, str] = field(default_factory=dict)
    # HITL cost control
    estimated_cost_usd: float = 0.0
    execution_class: str = "research"
    # Salad self-shutdown: container calls Salad API when done
    # These are auto-populated by the orchestrator from SaladProvider config
    salad_org: Optional[str] = None
    salad_project: Optional[str] = None
    salad_cg_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SaladGCSOrchestrator:
    """
    Manages a SaladCloud SRCG running OpenMM with GCS checkpoint streaming.

    Usage:
        cfg = SaladMDJobConfig(pdb_gcs_path="gs://...", output_gcs_prefix="gs://...")
        orch = SaladGCSOrchestrator(config=cfg, provider=salad_provider)
        result = await orch.run()
    """

    POLL_INTERVAL_SECONDS = 30
    MAX_POLL_RETRIES = 360
    DOWNLOAD_STALL_REALLOCATE_SECONDS = 600
    MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS = 1200

    def __init__(self, config, provider, on_event=None):
        self._cfg = config
        self._provider = provider
        self._on_event = on_event
        self._job_id = config.job_id or f"salad-{uuid.uuid4().hex[:12]}"
        self._cg_name = None
        self._started_at = None
        self._status_history = []
        self.DOWNLOAD_STALL_REALLOCATE_SECONDS = _env_int(
            "SALAD_DOWNLOAD_STALL_REALLOCATE_SECONDS", self.DOWNLOAD_STALL_REALLOCATE_SECONDS)
        self.MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS = (
            int(config.deploy_timeout_seconds) if config.deploy_timeout_seconds > 0
            else _env_int("SALAD_MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS", self.MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS))
        self._download_recovery_attempts = set()
        self._allocation_attempt = 1
        self._last_pulling_progress_by_instance = {}
        self._last_progress_at_by_instance = {}
        self._deploying_started_at = None
        self._bootstrap_policy = SaladBootstrapPolicy(
            image_size_gb=max(float(config.docker_image_size_gb or 8.0), 0.1),
            download_no_progress_seconds=max(int(self.DOWNLOAD_STALL_REALLOCATE_SECONDS), 1),
            max_same_class_reallocation_attempts=max(
                int(config.max_same_class_reallocation_attempts or 1), 1
            ),
            allow_degraded_smoke_fallback=bool(config.degraded_smoke_fallback),
        )
        self._bootstrap_receipts: Dict[str, Any] = {"history": []}
        self._worker_failure_receipt: Dict[str, Any] = {}
        self._last_runtime_signal: Dict[str, Any] = {}

    def _estimated_production_ns(self) -> float:
        raw_ns = str(self._cfg.env_extra.get("PRODUCTION_NS", "") or "").strip()
        if raw_ns:
            try:
                return max(float(raw_ns), 0.0)
            except ValueError:
                pass

        raw_timestep_fs = str(self._cfg.env_extra.get("OPENMM_TIMESTEP_FS", "") or "").strip()
        try:
            timestep_fs = max(float(raw_timestep_fs), 0.0) if raw_timestep_fs else 2.0
        except ValueError:
            timestep_fs = 2.0
        return max(float(self._cfg.max_steps) * timestep_fs / 1_000_000.0, 0.0)

    def _max_poll_retries(self) -> int:
        explicit_retries = _env_int("SALAD_MAX_POLL_RETRIES", 0)
        if explicit_retries > 0:
            return explicit_retries

        poll_interval = max(int(self.POLL_INTERVAL_SECONDS), 1)
        explicit_monitor_seconds = _env_int("SALAD_MAX_MONITOR_SECONDS", 0)
        if explicit_monitor_seconds > 0:
            return max(1, math.ceil(explicit_monitor_seconds / poll_interval))

        base_retries = max(int(self.MAX_POLL_RETRIES), 1)
        production_ns = self._estimated_production_ns()
        if production_ns <= 0:
            return base_retries

        try:
            from mica.infrastructure.gpu_scorer import GPU_NS_PER_DAY
            from mica.infrastructure.providers.base_provider import GPUType

            gpu_type = GPUType[str(self._cfg.gpu_type_str).strip()]
            ns_day = float(GPU_NS_PER_DAY.get(gpu_type, 0.0) or 0.0)
        except Exception:
            ns_day = 0.0

        if ns_day <= 0:
            return base_retries

        estimated_runtime_hours = production_ns / (ns_day / 24.0)
        monitor_seconds = max(
            float(base_retries * poll_interval),
            (estimated_runtime_hours * 2.0 + 1.5) * 3600.0,
        )
        return max(base_retries, math.ceil(monitor_seconds / poll_interval))

    @property
    def job_id(self) -> str:
        return self._job_id

    def build_env_vars(self, cg_name: str) -> Dict[str, str]:
        """
        Build environment variable dict for the Salad SRCG container.

        Maps SaladMDJobConfig fields to the env vars expected by the
        OpenMM worker script (main_salad.py pattern from official Salad docs).
        GCS_BUCKET and GCS_PREFIX are derived from output_gcs_prefix.
        """
        # Parse gs://bucket/prefix/... → bucket, prefix
        bucket, prefix = _parse_gcs_uri(self._cfg.output_gcs_prefix)
        pdb_bucket, pdb_object = _parse_gcs_uri(self._cfg.pdb_gcs_path)

        env: Dict[str, str] = {
            # GCS storage
            "GCS_BUCKET": bucket,
            "GCS_PREFIX": prefix.rstrip("/"),
            "PDB_GCS_BUCKET": pdb_bucket,
            "PDB_GCS_OBJECT": pdb_object,
            "CHECKPOINT_OBJECT": f"{prefix.rstrip('/')}/checkpoint.cpt",
            "OUTPUT_GCS_PREFIX": f"{prefix.rstrip('/')}/output/",
            "COMPLETED_MARKER_OBJECT": f"{prefix.rstrip('/')}/completed.marker",
            # OpenMM simulation params
            "MAX_STEPS": str(self._cfg.max_steps),
            "BENCHMARK_STEPS": str(self._cfg.benchmark_steps),
            "REPORT_FREQ": str(self._cfg.report_freq),
            "SAVING_INTERVAL_SECONDS": str(self._cfg.saving_interval_seconds),
            "MAX_NO_RESPONSE_TIME": str(self._cfg.max_no_response_time),
            # Salad self-shutdown credentials (container stops itself when done)
            "SALAD_API_KEY": self._provider._api_key,
            "ORGANIZATION_NAME": self._provider._org_name,
            "PROJECT_NAME": self._provider._project_name,
            "CONTAINER_GROUP_NAME": cg_name,
            # Job tracking
            "MICA_JOB_ID": self._job_id,
            "MICA_ROUTE_DECISION_ID": self._cfg.route_decision_id or "",
            "MICA_REQUESTED_GPU_TYPE": str(self._cfg.gpu_type_str or "RTX_5090"),
            "MICA_ALLOCATION_ATTEMPT": str(self._allocation_attempt),
            "MICA_PROVIDER": "salad",
            "MICA_CONTAINER_GROUP_ID": cg_name,
            "MICA_OUTPUT_GCS_PREFIX": self._cfg.output_gcs_prefix.rstrip("/"),
            "MICA_IMAGE_REF": str(self._cfg.docker_image or ""),
            "MICA_IMAGE_DIGEST": str(self._cfg.docker_image or "").split("@", 1)[1]
            if "@sha256:" in str(self._cfg.docker_image or "")
            else "",
            "TASK_CREATION_TIME": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

        # GCS credentials: inject SA JSON if provided
        gcs_creds = self._cfg.gcs_credentials_b64 or os.environ.get("SALAD_GCS_CREDENTIALS_B64", "")
        if gcs_creds:
            env["GCS_CREDENTIALS_JSON_B64"] = gcs_creds
            # Compatibility alias used by some existing toolchains.
            env["GOOGLE_APPLICATION_CREDENTIALS_JSON_B64"] = gcs_creds

        # Merge any extra env vars
        env.update(self._cfg.env_extra)
        return env

    async def run(self) -> Dict[str, Any]:
        """
        Execute the full SRCG lifecycle:
          1. Resolve GPU class ID for the requested GPU type
          2. Create + start Container Group
          3. Monitor until STOPPED/SUCCEEDED/FAILED
          4. Destroy CG on completion or error
          5. Return result dict with final status and GCS output paths

        Returns:
            {job_id, status, cg_name, output_gcs_prefix, elapsed_seconds, error, terminal_autopsy, teardown_proof}
        """
        self._started_at = datetime.now(timezone.utc)

        # Step 1: Multi-GPU flex — 4090/5080/5090 pool
        from ..providers.base_provider import GPUOffer
        from ..providers.salad_provider import GPUType as _GPUType
        all_classes = await self._provider.list_gpu_classes()
        gpu_class_ids = [cls["id"] for cls in all_classes if any(kw in cls.get("name","").lower() for kw in ["4090","5080","5090"])]
        if not gpu_class_ids:
            return self._error_result("No RTX 4090/5080/5090 GPU classes on Salad")
        offers = []
        for gt in [_GPUType.RTX_4090, _GPUType.RTX_5090]:
            try:
                offers.extend(await self._provider.search_offers(gpu_type=gt, max_price=None))
            except Exception:
                pass
        offer = offers[0] if offers else GPUOffer(
            provider="salad", offer_id=gpu_class_ids[0], gpu_type="RTX_4090",
            gpu_count=1, gpu_memory_gb=24.0, cpu_cores=4, ram_gb=8.0, disk_gb=30.0,
            disk_type="nvme", price_per_hour=0.16, is_spot=True, raw_data={"name": "RTX flex pool"})

        # Step 2: env vars
        from ..providers.salad_provider import _make_cg_name
        cg_name = _make_cg_name(self._job_id)
        self._cg_name = cg_name
        env_vars = self.build_env_vars(cg_name)
        env_vars["MICA_ACCEPTED_GPU_CLASSES"] = ",".join(gpu_class_ids)

        # Step 3: Create CG with multi-GPU
        result = await self._provider.create_instance(
            offer=offer, docker_image=self._cfg.docker_image,
            docker_command=self._cfg.docker_command, env_vars=env_vars,
            job_id=self._job_id, gpu_class_ids=gpu_class_ids,
        )

        if not result.success:
            return self._error_result(f"Container Group creation failed: {result.error_message}")

        actual_cg_name = result.instance.instance_id
        self._cg_name = actual_cg_name
        logger.info("SaladGCSOrchestrator: CG %s created — monitoring", actual_cg_name)
        self._emit_event("cg_created", {"cg_name": actual_cg_name})

        # Step 4: Monitor loop
        final_status = None
        for monitor_attempt in range(2):
            try:
                final_status = await self._monitor_until_done(actual_cg_name)
                break
            except asyncio.CancelledError:
                self._log_cancellation_context(
                    where="run.monitor",
                    cg_name=actual_cg_name,
                    poll=monitor_attempt + 1,
                )
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                self._emit_event(
                    "monitor_cancelled",
                    {
                        "cg_name": actual_cg_name,
                        "attempt": monitor_attempt + 1,
                    },
                )
                if monitor_attempt == 0:
                    logger.warning(
                        "SaladGCSOrchestrator: recovered from outer monitor cancellation for %s; retrying",
                        actual_cg_name,
                    )
                    continue
                logger.warning("SaladGCSOrchestrator: monitoring cancelled for %s", actual_cg_name)
                teardown_proof = await self._safe_destroy(actual_cg_name, terminal_status="cancelled")
                return self._error_result("Monitoring cancelled")
            except Exception as exc:
                logger.error("SaladGCSOrchestrator: monitoring error: %s", exc)
                teardown_proof = await self._safe_destroy(actual_cg_name, terminal_status="failed")
                return self._error_result(str(exc))

        if final_status is None:
            await self._safe_destroy(actual_cg_name, terminal_status="unknown")
            return self._error_result("Monitoring ended without terminal status")

        # Step 5: Destroy CG (Salad auto-stopped it; delete to free quota)
        teardown_proof = await self._safe_destroy(actual_cg_name, terminal_status=final_status)
        autopsy = self._build_terminal_autopsy(final_status, actual_cg_name, teardown_proof)
        durability_evidence = await self._collect_durability_evidence()

        elapsed = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return {
            "job_id": self._job_id,
            "status": final_status,
            "cg_name": actual_cg_name,
            "output_gcs_prefix": self._cfg.output_gcs_prefix,
            "elapsed_seconds": round(elapsed, 1),
            "error": self._terminal_error_message(final_status),
            "terminal_autopsy": autopsy,
            "teardown_proof": teardown_proof,
            "artifact_state": durability_evidence.get("artifact_state", "none"),
            "durability_evidence": durability_evidence,
            "worker_failure_receipt": dict(self._worker_failure_receipt or {}),
            "provider_bootstrap_receipts": dict(self._bootstrap_receipts or {}),
        }

    async def _monitor_until_done(self, cg_name: str) -> str:
        """
        Poll Salad CG status every POLL_INTERVAL_SECONDS.

        Terminal states: STOPPED (success) or FAILED/ERROR.
        Returns final status string: "completed" | "failed" | "stopped"
        """
        from ..providers.base_provider import InstanceStatus
        max_poll_retries = self._max_poll_retries()
        for attempt in range(max_poll_retries):
            try:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                self._log_cancellation_context(
                    where="monitor.sleep",
                    cg_name=cg_name,
                    poll=attempt + 1,
                )
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                logger.warning(
                    "SaladGCSOrchestrator: transient cancellation during poll sleep for %s (poll %d)",
                    cg_name,
                    attempt + 1,
                )
                self._emit_event(
                    "status_poll_sleep_cancelled",
                    {"cg_name": cg_name, "poll": attempt + 1},
                )
                continue
            try:
                instance = await self._provider.get_instance_status(cg_name)
            except asyncio.CancelledError:
                # Some SDK/network paths can surface cancellation-like errors on poll.
                # Keep the monitor alive and retry instead of failing the whole job early.
                self._log_cancellation_context(
                    where="monitor.status_poll",
                    cg_name=cg_name,
                    poll=attempt + 1,
                )
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                logger.warning(
                    "SaladGCSOrchestrator: transient cancellation during status poll for %s (poll %d)",
                    cg_name,
                    attempt + 1,
                )
                self._emit_event(
                    "status_poll_cancelled",
                    {"cg_name": cg_name, "poll": attempt + 1},
                )
                continue
            status_str = instance.raw_data.get("status_str", "pending")
            self._status_history.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "poll": attempt + 1,
                    "status": status_str,
                }
            )
            logger.debug("SaladGCSOrchestrator: %s status=%s (poll %d)", cg_name, status_str, attempt + 1)
            self._emit_event("status_poll", {"cg_name": cg_name, "status": status_str, "poll": attempt + 1})

            runtime_signal = await self._probe_output_runtime_signal()
            self._last_runtime_signal = runtime_signal
            observation, decision = self._record_bootstrap_decision(
                instance,
                cg_name,
                attempt + 1,
                runtime_signal,
            )
            await self._maybe_recover_download_stall(
                instance, cg_name, attempt + 1,
                observation=observation, decision=decision,
            )
            if (status_str in ("pending", "deploying") and self._deploying_started_at is not None
                and (datetime.now(timezone.utc) - self._deploying_started_at).total_seconds()
                >= self.MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS
                and len(self._download_recovery_attempts) >= self._bootstrap_policy.max_same_class_reallocation_attempts):
                logger.warning("SaladGCSOrchestrator: CG %s %s exhausted. Failing early.", cg_name, status_str)
                return "failed"
            if runtime_signal.get("failure_receipt_present"):
                failure_receipt = dict(runtime_signal.get("failure_receipt") or {})
                if not failure_receipt:
                    failure_receipt = {
                        "error_type": "RuntimeError",
                        "error_message": "Salad worker failure receipt present in GCS but could not be parsed",
                        "probe_reason": str(runtime_signal.get("probe_reason") or ""),
                    }
                self._worker_failure_receipt = failure_receipt
                self._emit_event(
                    "worker_failure_detected",
                    {
                        "cg_name": cg_name,
                        "poll": attempt + 1,
                        "error_type": str(failure_receipt.get("error_type") or "RuntimeError"),
                        "error_message": str(failure_receipt.get("error_message") or ""),
                    },
                )
                return "failed"

            if runtime_signal.get("completed_marker_present") and int(runtime_signal.get("dcd_chunk_count") or 0) > 0:
                self._emit_event(
                    "worker_completion_detected",
                    {
                        "cg_name": cg_name,
                        "poll": attempt + 1,
                        "completed_marker_present": True,
                        "dcd_chunk_count": int(runtime_signal.get("dcd_chunk_count") or 0),
                    },
                )
                return "completed"

            if instance.status == InstanceStatus.STOPPED:
                # STOPPED = succeeded (exit 0) or manually stopped
                if status_str in ("succeeded", "stopped"):
                    return "completed"
                return "stopped"
            if instance.status == InstanceStatus.ERROR:
                return "failed"

        logger.warning("SaladGCSOrchestrator: poll limit reached for %s", cg_name)
        return "timeout"

    def _record_bootstrap_decision(
        self,
        instance: Any,
        cg_name: str,
        poll: int,
        runtime_signal: Dict[str, Any],
    ) -> tuple[SaladBootstrapObservation, Any]:
        raw = dict(getattr(instance, "raw_data", {}) or {})
        now = datetime.now(timezone.utc)
        instance_id = str(
            raw.get("container_group_instance_id")
            or raw.get("latest_container_group_instance_id")
            or getattr(instance, "instance_id", "")
            or cg_name
        ).strip()
        pulling_progress = raw.get("pulling_progress")
        try:
            pulling_progress_value = float(pulling_progress) if pulling_progress is not None else None
        except (TypeError, ValueError):
            pulling_progress_value = None
        previous_progress = self._last_pulling_progress_by_instance.get(instance_id)
        if pulling_progress_value is not None:
            if previous_progress is None or pulling_progress_value > previous_progress:
                self._last_progress_at_by_instance[instance_id] = now
            self._last_pulling_progress_by_instance[instance_id] = pulling_progress_value
        last_progress_at = self._last_progress_at_by_instance.get(instance_id)
        seconds_since_progress = None
        if last_progress_at is not None:
            seconds_since_progress = (now - last_progress_at).total_seconds()
        elif raw.get("latest_system_event_time"):
            try:
                event_time = datetime.fromisoformat(
                    str(raw.get("latest_system_event_time") or "").replace("Z", "+00:00")
                )
                seconds_since_progress = (now - event_time).total_seconds()
            except ValueError:
                seconds_since_progress = None

        elapsed = 0.0
        if self._started_at is not None:
            elapsed = (now - self._started_at).total_seconds()

        recent_events = list(raw.get("recent_system_events") or [])
        latest_event = str(raw.get("latest_system_event") or "").strip()
        latest_event_time = str(raw.get("latest_system_event_time") or "").strip()
        worker_heartbeat_seen = bool(
            runtime_signal.get("bootstrap_heartbeat_present")
            or runtime_signal.get("latest_status_json_present")
        )
        gcs_bootstrap_seen = self._has_worker_bootstrap_evidence(runtime_signal)
        worker_execution_started = bool(
            int(runtime_signal.get("dcd_chunk_count") or 0) > 0
            or runtime_signal.get("history_json_present")
            or runtime_signal.get("worker_history_json_present")
            or runtime_signal.get("latest_status_json_present")
        )
        observation = SaladBootstrapObservation(
            job_id=self._job_id,
            cg_name=cg_name,
            route_decision_id=str(self._cfg.route_decision_id or ""),
            output_gcs_prefix=self._cfg.output_gcs_prefix,
            requested_gpu_type=str(self._cfg.gpu_type_str or "RTX_5090"),
            actual_gpu_type=str(raw.get("gpu_type") or raw.get("gpu_class") or self._cfg.gpu_type_str or ""),
            allocation_attempt=self._allocation_attempt,
            poll=poll,
            status_str=str(raw.get("status_str") or "pending"),
            instance_state=str(raw.get("instance_state") or ""),
            instance_id=instance_id,
            machine_id=str(raw.get("machine_id") or ""),
            latest_system_event=latest_event,
            latest_system_event_time=latest_event_time,
            recent_events=recent_events,
            pulling_progress=pulling_progress_value,
            previous_pulling_progress=previous_progress,
            elapsed_seconds=elapsed,
            seconds_since_progress=seconds_since_progress,
            image_size_gb=float(self._cfg.docker_image_size_gb or 8.0),
            logs_seen=bool(latest_event or recent_events),
            worker_heartbeat_seen=worker_heartbeat_seen,
            gcs_bootstrap_seen=gcs_bootstrap_seen,
            worker_execution_started=worker_execution_started,
            completed_marker_present=bool(runtime_signal.get("completed_marker_present")),
            failure_receipt_present=bool(runtime_signal.get("failure_receipt_present")),
        )
        decision = classify_salad_bootstrap(observation, self._bootstrap_policy)
        receipts = build_policy_receipts(observation, decision, self._bootstrap_policy)
        self._bootstrap_receipts.update(receipts)
        history = list(self._bootstrap_receipts.get("history") or [])
        history.append(
            {
                "poll": poll,
                "status_str": observation.status_str,
                "state": decision.state,
                "user_visible_status": decision.user_visible_status,
                "reason_code": decision.reason_code,
                "reallocation_required": decision.reallocation_required,
                "produced_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._bootstrap_receipts["history"] = history[-30:]
        self._bootstrap_receipts["latest_decision"] = decision.as_dict()
        self._emit_event(
            "provider_bootstrap_status",
            {
                "cg_name": cg_name,
                "poll": poll,
                "status": observation.status_str,
                "bootstrap_state": decision.state,
                "user_visible_status": decision.user_visible_status,
                "reason_code": decision.reason_code,
                "reallocation_required": decision.reallocation_required,
            },
        )
        return observation, decision

    async def _maybe_recover_download_stall(
        self, instance, cg_name, poll, *, observation=None, decision=None,
    ) -> None:
        """Smart reallocation: pending/deploying stall OR download stall."""
        raw = dict(getattr(instance, "raw_data", {}) or {})
        status_str = str(raw.get("status_str") or "").strip().lower()
        elapsed = 0.0
        if self._started_at is not None:
            elapsed = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        is_provisioning = status_str in ("pending", "deploying")
        if is_provisioning and self._deploying_started_at is None:
            self._deploying_started_at = datetime.now(timezone.utc)
        deploying_seconds = 0.0
        if self._deploying_started_at is not None:
            deploying_seconds = (datetime.now(timezone.utc) - self._deploying_started_at).total_seconds()
        if not is_provisioning:
            self._deploying_started_at = None
        latest_instance_id = str(
            raw.get("container_group_instance_id")
            or raw.get("latest_container_group_instance_id")
            or ""
        ).strip()
        if decision and decision.reallocation_required:
            await self._execute_reallocation(cg_name=cg_name, poll=poll,
                instance_id=str(raw.get("container_group_instance_id") or "").strip(),
                stall_seconds=deploying_seconds, observation=observation, decision=decision,
                reason="bootstrap_decision_reallocation_required")
            return
        if is_provisioning and not latest_instance_id:
            if deploying_seconds >= self.MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS:
                logger.warning("SaladGCSOrchestrator: CG %s stuck '%s' %.0fs no instance — destroying.", cg_name, status_str, deploying_seconds)
                destroy = getattr(self._provider, "destroy_instance", None)
                if callable(destroy):
                    try: await destroy(cg_name)
                    except Exception as exc: logger.warning("destroy_instance(%s) failed: %s", cg_name, exc)
                self._emit_event("no_instance_reallocation_impossible", {"cg_name": cg_name, "poll": poll, "status": status_str, "deploying_seconds": round(deploying_seconds, 1)})
            return
        if is_provisioning and latest_instance_id:
            if deploying_seconds >= self.MAX_DEPLOYING_WITHOUT_INSTANCE_SECONDS:
                await self._execute_reallocation(cg_name=cg_name, poll=poll, instance_id=latest_instance_id,
                    stall_seconds=deploying_seconds, observation=observation, decision=decision,
                    reason=f"stuck_{status_str}_with_instance")
            return
        if status_str != "deploying" and latest_event != "Instance Downloading":
            return
        if not latest_event_time or not latest_instance_id:
            return
        if latest_instance_id in self._download_recovery_attempts:
            return
        stall_seconds = 0.0
        if latest_event_time:
            try:
                observed_at = datetime.fromisoformat(latest_event_time.replace("Z", "+00:00"))
                stall_seconds = (datetime.now(timezone.utc) - observed_at).total_seconds()
            except ValueError:
                stall_seconds = 0.0
        if stall_seconds < self.DOWNLOAD_STALL_REALLOCATE_SECONDS:
            return
        await self._execute_reallocation(cg_name=cg_name, poll=poll, instance_id=latest_instance_id,
            stall_seconds=stall_seconds, observation=observation, decision=decision, reason="download_stall")

    async def _execute_reallocation(self, *, cg_name, poll, instance_id, stall_seconds, observation, decision, reason):
        """Centralised reallocation with max attempts."""
        if len(self._download_recovery_attempts) >= self._bootstrap_policy.max_same_class_reallocation_attempts:
            if observation and decision:
                exhausted = build_same_class_reallocation_receipt(observation, decision,
                    target_gpu_type=str(self._cfg.gpu_type_str or "RTX_5090"),
                    reallocation_started=False, reallocation_succeeded=False, reallocation_exhausted=True)
                self._bootstrap_receipts["same_class_reallocation_receipt"] = exhausted
                self._bootstrap_receipts["provider_stall_receipt"] = build_provider_stall_receipt(observation, decision, self._bootstrap_policy)
            self._emit_event("same_class_reallocation_exhausted", {"cg_name": cg_name, "poll": poll,
                "instance_id": instance_id, "max_attempts": self._bootstrap_policy.max_same_class_reallocation_attempts, "reason": reason})
            logger.warning("SaladGCSOrchestrator: reallocation exhausted for %s after %d attempts (reason=%s)",
                cg_name, len(self._download_recovery_attempts), reason)
            return
        reallocate = getattr(self._provider, "reallocate_container_group_instance", None)
        if not callable(reallocate):
            logger.warning("SaladGCSOrchestrator: reallocate not available for %s", cg_name)
            return
        if observation and decision:
            self._bootstrap_receipts["same_class_reallocation_receipt"] = build_same_class_reallocation_receipt(
                observation, decision, target_gpu_type=str(self._cfg.gpu_type_str or "RTX_5090"), reallocation_started=True)
            self._bootstrap_receipts["provider_stall_receipt"] = build_provider_stall_receipt(observation, decision, self._bootstrap_policy)
        if instance_id and await reallocate(cg_name, instance_id):
            self._download_recovery_attempts.add(instance_id)
            self._allocation_attempt += 1
            self._deploying_started_at = None
            if observation and decision:
                self._bootstrap_receipts["same_class_reallocation_receipt"] = build_same_class_reallocation_receipt(
                    observation, decision, target_gpu_type=str(self._cfg.gpu_type_str or "RTX_5090"),
                    reallocation_started=True, reallocation_succeeded=True)
            self._emit_event("same_class_reallocation_started", {"cg_name": cg_name, "poll": poll,
                "instance_id": instance_id, "stall_seconds": round(stall_seconds, 1),
                "requested_gpu_type": str(self._cfg.gpu_type_str or "RTX_5090"),
                "allocation_attempt": self._allocation_attempt, "reason": reason})

    def _has_worker_bootstrap_evidence(self, runtime_signal=None) -> bool:
        signal = dict(runtime_signal or self._last_runtime_signal or {})
        return bool(
            signal.get("worker_entrypoint_started_present")
            or signal.get("bootstrap_heartbeat_present")
            or signal.get("gcs_write_probe_present")
            or signal.get("latest_status_json_present")
            or signal.get("worker_history_json_present")
            or signal.get("history_json_present")
            or signal.get("failure_receipt_present")
            or signal.get("completed_marker_present")
            or int(signal.get("bootstrap_object_count") or 0) > 0
            or int(signal.get("worker_produced_object_count") or 0) > 0
        )

    async def _inspect_before_destroy(self, cg_name: str) -> Dict[str, Any]:
        inspection: Dict[str, Any] = {
            "inspection_attempted": False,
            "inspection_available": False,
            "inspection_error": None,
            "container_group_inspection": {},
        }
        if not self._cfg.pre_destroy_inspection_enabled:
            inspection["inspection_error"] = "pre_destroy_inspection_disabled"
            return inspection
        inspect_fn = getattr(self._provider, "inspect_container_group", None)
        if not callable(inspect_fn):
            inspection["inspection_error"] = "provider_inspection_unavailable"
            return inspection
        inspection["inspection_attempted"] = True
        try:
            inspection["container_group_inspection"] = await inspect_fn(cg_name)
            inspection["inspection_available"] = True
        except Exception as exc:
            inspection["inspection_error"] = str(exc)
        return inspection

    async def _safe_destroy(self, cg_name: Optional[str], *, terminal_status: Optional[str] = None) -> Dict[str, Any]:
        """Destroy CG and return teardown proof summary."""
        if not cg_name:
            return {
                "destroy_attempted": False,
                "destroy_succeeded": False,
                "destroy_skipped_reason": "cg_name_missing",
                "destroy_error": None,
                "terminal_status": terminal_status or "",
                "worker_bootstrap_evidence_seen": False,
                "inspection_attempted": False,
                "inspection_available": False,
                "container_group_inspection": {},
            }
        inspection = await self._inspect_before_destroy(cg_name)
        bootstrap_evidence_seen = self._has_worker_bootstrap_evidence()
        preserve_failed = bool(
            self._cfg.preserve_failed_cg_on_missing_bootstrap_evidence
            and terminal_status not in {None, "", "completed"}
            and not bootstrap_evidence_seen
        )
        if preserve_failed:
            return {
                "destroy_attempted": False,
                "destroy_succeeded": False,
                "destroy_skipped_reason": "preserved_for_bootstrap_autopsy",
                "destroy_error": None,
                "terminal_status": terminal_status or "",
                "worker_bootstrap_evidence_seen": bootstrap_evidence_seen,
                **inspection,
            }
        try:
            await self._provider.destroy_instance(cg_name)
            return {
                "destroy_attempted": True,
                "destroy_succeeded": True,
                "destroy_skipped_reason": None,
                "destroy_error": None,
                "terminal_status": terminal_status or "",
                "worker_bootstrap_evidence_seen": bootstrap_evidence_seen,
                **inspection,
            }
        except Exception as exc:
            logger.warning("SaladGCSOrchestrator: destroy failed for %s: %s", cg_name, exc)
            return {
                "destroy_attempted": True,
                "destroy_succeeded": False,
                "destroy_skipped_reason": None,
                "destroy_error": str(exc),
                "terminal_status": terminal_status or "",
                "worker_bootstrap_evidence_seen": bootstrap_evidence_seen,
                **inspection,
            }

    def _error_result(self, msg: str) -> Dict[str, Any]:
        elapsed = (
            (datetime.now(timezone.utc) - self._started_at).total_seconds()
            if self._started_at else 0.0
        )
        teardown_proof = {
            "destroy_attempted": False,
            "destroy_succeeded": False,
            "destroy_skipped_reason": "error_result_no_destroy_context",
            "destroy_error": None,
        }
        autopsy = self._build_terminal_autopsy("failed", self._cg_name or "", teardown_proof)
        durability_evidence = {
            "schema_version": "durability_evidence_v1",
            "output_gcs_prefix": self._cfg.output_gcs_prefix,
            "artifact_state": "none",
            "probe_status": "not_checked",
            "probe_reason": "error_result_before_postrun_probe",
            "object_count": 0,
            "object_listing": [],
            "evidence_hash": hashlib.sha256(b"none").hexdigest(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        return {
            "job_id": self._job_id,
            "status": "failed",
            "cg_name": self._cg_name,
            "output_gcs_prefix": self._cfg.output_gcs_prefix,
            "elapsed_seconds": round(elapsed, 1),
            "error": self._terminal_error_message("failed", default=msg),
            "terminal_autopsy": autopsy,
            "teardown_proof": teardown_proof,
            "artifact_state": "none",
            "durability_evidence": durability_evidence,
            "worker_failure_receipt": dict(self._worker_failure_receipt or {}),
            "provider_bootstrap_receipts": dict(self._bootstrap_receipts or {}),
        }

    def _terminal_error_message(self, final_status: str, default: Optional[str] = None) -> Optional[str]:
        if final_status == "completed":
            return None
        if self._worker_failure_receipt:
            error_type = str(self._worker_failure_receipt.get("error_type") or "RuntimeError")
            error_message = str(self._worker_failure_receipt.get("error_message") or "").strip()
            if error_message:
                return f"{error_type}: {error_message}"
            return error_type
        return default or f"Terminal state: {final_status}"

    def _timeout_cause_hint(self) -> str:
        if not self._status_history:
            return "poll_window_exhausted"
        statuses = {str(s.get("status", "")).lower() for s in self._status_history}
        startup_states = {"pending", "deploying", "creating", "allocating"}
        if statuses and statuses.issubset(startup_states):
            return "startup_stall"
        if "running" in statuses:
            return "job_not_progressing"
        return "poll_window_exhausted"

    def _build_terminal_autopsy(
        self,
        final_status: str,
        cg_name: str,
        teardown_proof: Dict[str, Any],
    ) -> Dict[str, Any]:
        elapsed = (
            (datetime.now(timezone.utc) - self._started_at).total_seconds()
            if self._started_at else 0.0
        )
        autopsy: Dict[str, Any] = {
            "schema_version": "terminal_autopsy_v1",
            "job_id": self._job_id,
            "cg_name": cg_name,
            "provider": "salad",
            "final_status": final_status,
            "poll_interval_seconds": self.POLL_INTERVAL_SECONDS,
            "max_poll_retries": self._max_poll_retries(),
            "polls_observed": len(self._status_history),
            "elapsed_seconds": round(elapsed, 1),
            "status_snapshots_tail": self._status_history[-20:],
            "timeout_cause_hint": None,
            "teardown_proof": teardown_proof,
            "worker_failure_receipt": dict(self._worker_failure_receipt or {}),
            "runtime_signal": dict(self._last_runtime_signal or {}),
            "provider_bootstrap_receipts": dict(self._bootstrap_receipts or {}),
        }
        if final_status == "timeout":
            autopsy["timeout_cause_hint"] = self._timeout_cause_hint()
        return autopsy

    def _build_gcs_client(self):
        from google.cloud import storage

        gcs_b64 = self._cfg.gcs_credentials_b64 or os.environ.get("SALAD_GCS_CREDENTIALS_B64", "")
        if gcs_b64:
            info = json.loads(base64.b64decode(gcs_b64).decode("utf-8"))
            return storage.Client.from_service_account_info(info)
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""):
            return storage.Client.from_service_account_json(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        return storage.Client()

    def _list_output_objects(self, *, max_results: int) -> tuple[Any, str, str, List[Dict[str, Any]]]:
        bucket, prefix = _parse_gcs_uri(self._cfg.output_gcs_prefix)
        prefix = prefix.rstrip("/") + "/"
        client = self._build_gcs_client()
        blobs = list(client.list_blobs(bucket, prefix=prefix, max_results=max_results))
        listing = [
            {"name": blob.name, "size": int(getattr(blob, "size", 0) or 0)}
            for blob in blobs
        ]
        return client, bucket, prefix, listing

    @staticmethod
    def _summarize_output_listing(
        listing: List[Dict[str, Any]],
        *,
        prefix: str | None = None,
    ) -> Dict[str, Any]:
        names = [str(obj.get("name") or "") for obj in listing]
        normalized_prefix = str(prefix or "").strip().rstrip("/")
        if not normalized_prefix:
            output_name = next((name for name in names if "/output/" in name), "")
            if output_name:
                normalized_prefix = output_name.split("/output/", 1)[0]
            else:
                marker_name = next((name for name in names if name.endswith("completed.marker")), "")
                if marker_name:
                    normalized_prefix = marker_name[: -len("completed.marker")].rstrip("/")

        root_prefix = f"{normalized_prefix}/" if normalized_prefix else ""
        output_prefix = f"{root_prefix}output/"
        bootstrap_prefix = f"{root_prefix}bootstrap/"

        failure_receipt_name = next(
            (name for name in names if name == f"{output_prefix}failure_receipt.json"),
            "",
        )
        bootstrap_object_names = [
            name
            for name in names
            if name.startswith(bootstrap_prefix) and "/" not in name[len(bootstrap_prefix):]
        ]
        output_object_names = [
            name
            for name in names
            if name.startswith(output_prefix) and "/" not in name[len(output_prefix):]
        ]
        return {
            "object_count": len(listing),
            "bootstrap_object_count": len(bootstrap_object_names),
            "worker_produced_object_count": len(bootstrap_object_names) + len(output_object_names),
            "dcd_chunk_count": sum(
                1
                for name in names
                if name.startswith(output_prefix)
                and name.endswith(".dcd")
                and "/" not in name[len(output_prefix):]
            ),
            "completed_marker_present": f"{root_prefix}completed.marker" in names if root_prefix else any(
                name.endswith("completed.marker") for name in names
            ),
            "worker_entrypoint_started_present": f"{bootstrap_prefix}worker_entrypoint_started.json" in names,
            "bootstrap_heartbeat_present": f"{bootstrap_prefix}bootstrap_heartbeat.json" in names,
            "gcs_write_probe_present": f"{bootstrap_prefix}gcs_write_probe.json" in names,
            "history_json_present": f"{output_prefix}history.json" in names,
            "latest_status_json_present": f"{output_prefix}latest_status.json" in names,
            "worker_history_json_present": f"{output_prefix}worker_history.json" in names,
            "failure_receipt_present": bool(failure_receipt_name),
            "failure_receipt_name": failure_receipt_name,
            "failure_traceback_present": f"{output_prefix}failure_traceback.txt" in names,
        }

    async def _probe_output_runtime_signal(self) -> Dict[str, Any]:
        signal: Dict[str, Any] = {
            "probe_status": "ok",
            "probe_reason": None,
            "failure_receipt_present": False,
            "failure_receipt": {},
            "failure_traceback_present": False,
            "completed_marker_present": False,
        }
        try:
            client, bucket, _prefix, listing = self._list_output_objects(max_results=200)
            summary = self._summarize_output_listing(listing, prefix=_prefix)
            signal.update(summary)
            failure_receipt_name = str(summary.get("failure_receipt_name") or "")
            if failure_receipt_name:
                receipt_text = client.bucket(bucket).blob(failure_receipt_name).download_as_text()
                signal["failure_receipt"] = json.loads(receipt_text)
        except Exception as exc:
            signal["probe_status"] = "probe_failed"
            signal["probe_reason"] = str(exc)
        return signal

    async def _collect_durability_evidence(self) -> Dict[str, Any]:
        """Probe output GCS prefix and classify artifact_state as none|partial|complete."""
        checked_at = datetime.now(timezone.utc).isoformat()
        _bucket, prefix = _parse_gcs_uri(self._cfg.output_gcs_prefix)
        prefix = prefix.rstrip("/") + "/"

        evidence: Dict[str, Any] = {
            "schema_version": "durability_evidence_v1",
            "output_gcs_prefix": self._cfg.output_gcs_prefix,
            "bucket": _bucket,
            "prefix": prefix,
            "artifact_state": "none",
            "probe_status": "ok",
            "probe_reason": None,
            "object_count": 0,
            "bootstrap_object_count": 0,
            "worker_produced_object_count": 0,
            "dcd_chunk_count": 0,
            "completed_marker_present": False,
            "worker_entrypoint_started_present": False,
            "bootstrap_heartbeat_present": False,
            "gcs_write_probe_present": False,
            "history_json_present": False,
            "worker_history_json_present": False,
            "failure_receipt_present": False,
            "failure_traceback_present": False,
            "object_listing": [],
            "evidence_hash": "",
            "checked_at": checked_at,
        }

        try:
            _client, _bucket, _prefix, listing = self._list_output_objects(max_results=500)
            summary = self._summarize_output_listing(listing, prefix=_prefix)

            if summary["completed_marker_present"] and summary["dcd_chunk_count"] > 0:
                artifact_state = "complete"
            elif int(summary.get("worker_produced_object_count") or 0) > 0:
                artifact_state = "partial"
            else:
                artifact_state = "none"

            evidence.update(
                {
                    "artifact_state": artifact_state,
                    "bucket": _bucket,
                    "object_count": summary["object_count"],
                    "bootstrap_object_count": summary["bootstrap_object_count"],
                    "worker_produced_object_count": summary["worker_produced_object_count"],
                    "dcd_chunk_count": summary["dcd_chunk_count"],
                    "completed_marker_present": summary["completed_marker_present"],
                    "worker_entrypoint_started_present": summary["worker_entrypoint_started_present"],
                    "bootstrap_heartbeat_present": summary["bootstrap_heartbeat_present"],
                    "gcs_write_probe_present": summary["gcs_write_probe_present"],
                    "history_json_present": summary["history_json_present"],
                    "worker_history_json_present": summary["worker_history_json_present"],
                    "failure_receipt_present": summary["failure_receipt_present"],
                    "failure_traceback_present": summary["failure_traceback_present"],
                    "object_listing": listing,
                }
            )
        except Exception as exc:
            evidence.update(
                {
                    "artifact_state": "none",
                    "probe_status": "probe_failed",
                    "probe_reason": str(exc),
                    "object_count": 0,
                    "object_listing": [],
                }
            )

        hash_input = json.dumps(
            {
                "output_gcs_prefix": evidence["output_gcs_prefix"],
                "artifact_state": evidence["artifact_state"],
                "object_listing": evidence["object_listing"],
                "checked_at": evidence["checked_at"],
            },
            sort_keys=True,
        ).encode("utf-8")
        evidence["evidence_hash"] = hashlib.sha256(hash_input).hexdigest()
        return evidence

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(event_type, payload)
            except Exception:
                pass

    def _log_cancellation_context(self, where: str, cg_name: str, poll: int) -> None:
        task = asyncio.current_task()
        cancelling = task.cancelling() if task is not None and hasattr(task, "cancelling") else None
        stack_preview: list[str] = []
        if task is not None:
            try:
                stack_preview = traceback.format_list(traceback.extract_stack(limit=12))[-6:]
            except Exception:
                stack_preview = []

        logger.warning(
            "SaladGCSOrchestrator: cancellation context where=%s cg=%s poll=%s cancelling=%s stack=%s",
            where,
            cg_name,
            poll,
            cancelling,
            " | ".join(s.strip().replace("\n", " ") for s in stack_preview),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """
    Parse gs://bucket/object/path → (bucket, object_path).

    >>> _parse_gcs_uri("gs://mica-compute/salad/job-abc/")
    ('mica-compute', 'salad/job-abc/')
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"Expected gs:// URI, got: {uri!r}")
    stripped = uri[5:]
    if "/" in stripped:
        bucket, obj = stripped.split("/", 1)
    else:
        bucket, obj = stripped, ""
    return bucket, obj


def build_gcs_env_for_container(
    gcs_credentials_b64: str,
    bucket: str,
    prefix: str,
) -> Dict[str, str]:
    """
    Build the GCS credential env vars for a container that needs GCS access.

    The container entrypoint is expected to decode GCS_CREDENTIALS_JSON_B64,
    write it to a temp file, and set GOOGLE_APPLICATION_CREDENTIALS.

    Example container bootstrap snippet:
        import base64, json, tempfile, os
        creds_b64 = os.environ.get("GCS_CREDENTIALS_JSON_B64", "")
        if creds_b64:
            creds_json = base64.b64decode(creds_b64).decode()
            tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
            tmp.write(creds_json); tmp.flush()
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
    """
    return {
        "GCS_CREDENTIALS_JSON_B64": gcs_credentials_b64,
        "GCS_BUCKET": bucket,
        "GCS_PREFIX": prefix.rstrip("/"),
    }
