"""
vast_md_orchestrator.py — Autonomous Vast.ai MD Simulation Orchestrator

Fully automates the end-to-end lifecycle of molecular dynamics simulations on
Vast.ai GPU pods:

  FASE 0: Search & rent cheapest RTX 5080 → wait_ready
  FASE 1: SSH probe → detect conda path → GPU info
  FASE 2: Install OpenMM + CUDA toolkit (12.4 preferred, 11.8 fallback)
    FASE 3: Stage force-field assets (policy-driven; optional)
    FASE 4: Verify force-field loads (policy-driven)
  FASE 5: Create run dirs → SCP PDB + scripts
  FASE 6: Launch simulation(s) via SCP'd bash script
  FASE 7: Monitor progress (speed, step, ETA, GPU util)
  FASE 8: Download results → destroy instance

Rules enforced (from EXAMPLE_LOGS_MD.MD):
  R1  All work in /workspace, never /root
  R2  SSH key: ~/.ssh/vast_key (configurable)
  R3  One GPU per replica (CUDA_VISIBLE_DEVICES)
  R4  Default GPU: RTX 5080
  R5  HEREDOC via SCP'd .sh script — never inline
  R6  Detect conda path before anything
  R7  charmm36_2024 requires manual staging
  R8  Verify single PID after launch

Author: MICA BioDynamo Autonomous Deployment
Date: 2026-02-26
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional

import httpx

from ..compute_image_contract import canonical_md_worker_image
from ..providers.base_provider import CloudProvider, GPUType, InstanceStatus
from ..providers.vast_provider import VastProvider  # default provider
from ..ssh_resilience import (
    CommandProtocol,
    ResilientSSHExecutor,
    SSHResult,
)

logger = logging.getLogger(__name__)


def _utcnow_dt() -> datetime:
    return datetime.now(UTC)


def _utcnow_iso() -> str:
    return _utcnow_dt().isoformat().replace("+00:00", "Z")


def _compute_sha256_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_sha256_hexdigest(text: str) -> str:
    match = re.search(r"\b[a-fA-F0-9]{64}\b", text or "")
    return match.group(0).lower() if match else ""


def _rclone_hashsum_unsupported(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "hash unsupported" in lowered
        or "hash type not supported" in lowered
        or "hashes not supported" in lowered
    )


def _path_if_exists(path: str) -> str:
    """Return path if the file exists on disk, else empty string."""
    return path if (path and os.path.isfile(path)) else ""


# ────────────────────────────────────────────────────────────────
# Pod API Client — lightweight async HTTP adapter for the on-pod
# FastAPI service (mica-openmm-pod).  Used by the orchestrator to
# drive lifecycle, fetch metrics, and request teardown proofs once
# the pod reports its API as healthy.
# ────────────────────────────────────────────────────────────────

_POD_API_PORT = 8787


class _PodAPIClient:
    """Stateless async helper to talk to the Pod API running on a GPU pod."""

    def __init__(self, base_url: str, timeout: float = 15.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._pod_api_token = str(os.getenv("MICA_POD_API_TOKEN", "")).strip()
        self._pod_callback_token = str(
            os.getenv("MICA_POD_CALLBACK_TOKEN", self._pod_api_token)
        ).strip()

    def _build_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        upper_method = str(method or "").upper()
        is_mutation = upper_method in {"POST", "PUT", "PATCH", "DELETE"}
        if is_mutation and self._pod_api_token:
            headers["x-pod-api-token"] = self._pod_api_token
        if path.startswith("/pod/v1/callbacks/") and self._pod_callback_token:
            headers["x-pod-callback-token"] = self._pod_callback_token
        return headers

    # ── low-level ────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        headers = self._build_auth_headers(method, path)
        async with httpx.AsyncClient(timeout=timeout or self._timeout) as client:
            return await client.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                headers=headers or None,
            )

    # ── P0 endpoints ─────────────────────────────────────────────

    async def health(self) -> dict:
        r = await self._request("GET", "/pod/v1/health")
        r.raise_for_status()
        return r.json()

    async def create_run(self, payload: dict) -> dict:
        r = await self._request("POST", "/pod/v1/runs", json_body=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    async def get_run(self, run_id: str) -> dict:
        r = await self._request("GET", f"/pod/v1/runs/{run_id}")
        r.raise_for_status()
        return r.json()

    async def teardown(self, run_id: str) -> dict:
        r = await self._request("POST", f"/pod/v1/runs/{run_id}/teardown", timeout=60)
        r.raise_for_status()
        return r.json()

    async def cancel(self, run_id: str) -> dict:
        r = await self._request("POST", f"/pod/v1/runs/{run_id}/cancel")
        r.raise_for_status()
        return r.json()

    async def get_artifacts(self, run_id: str) -> list:
        r = await self._request("GET", f"/pod/v1/runs/{run_id}/artifacts")
        r.raise_for_status()
        return r.json()

    # ── P1 endpoints ─────────────────────────────────────────────

    async def get_metrics(self, run_id: str) -> Optional[dict]:
        """Fetch live PodMetricsSnapshot. Returns None on 404 (no metrics yet)."""
        r = await self._request("GET", f"/pod/v1/runs/{run_id}/metrics")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_latest_trajectory(self, run_id: str) -> Optional[dict]:
        r = await self._request("GET", f"/pod/v1/runs/{run_id}/trajectory/latest")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def register_stage(self, run_id: str, stage: str, detail: str = "") -> dict:
        r = await self._request(
            "POST",
            "/pod/v1/callbacks/stage",
            json_body={"run_id": run_id, "stage": stage, "detail": detail},
        )
        r.raise_for_status()
        return r.json()

    # ── PDBFixer (W3-4) ─────────────────────────────────────────

    async def pdbfix(self, payload: dict) -> dict:
        """Call PDBFixer auto-repair endpoint. Timeout 120s for large systems."""
        r = await self._request(
            "POST", "/pod/v1/pdbfix", json_body=payload, timeout=120
        )
        r.raise_for_status()
        return r.json()

    # ── Trajectory streaming (W4-3) ──────────────────────────────

    async def get_trajectory_frame_range(
        self, run_id: str, since_frame: int = 0
    ) -> Optional[dict]:
        """Fetch latest trajectory frame. Returns None if no new frames."""
        r = await self._request(
            "GET", f"/pod/v1/runs/{run_id}/trajectory/latest"
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("frame_index", 0) <= since_frame and not data.get("data"):
            return None
        return data

    # ── Live analysis RMSD / RMSF (W4-4) ─────────────────────────

    async def get_live_rmsd(self, run_id: str) -> Optional[dict]:
        """Fetch live RMSD from Pod API. Returns None on 404."""
        r = await self._request(
            "GET", f"/pod/v1/runs/{run_id}/analysis/rmsd"
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_live_rmsf(self, run_id: str) -> Optional[dict]:
        """Fetch live RMSF from Pod API. Returns None on 404."""
        r = await self._request(
            "GET", f"/pod/v1/runs/{run_id}/analysis/rmsf"
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


# ────────────────────────────────────────────────────────────────
# Module Dependency Manifest (P2: Remote Module Staging)
# ────────────────────────────────────────────────────────────────
# Maps each simulation mode to its required processor modules.
# Used to stage local modules to remote pods before launch, with
# SHA-256 verification to prevent import failures due to missing dependencies.

# Compute processor directory path: workers/dynamo/biodynamo/processors/
# from repository root
# src/mica/infrastructure/orchestration/vast_md_orchestrator.py -> 
#   ../../../../../../ (5 parent calls) -> repo root
_REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent
_BIODYNAMO_PROCESSOR_DIR = _REPO_ROOT / "workers" / "dynamo" / "biodynamo" / "processors"

_MODULE_DEPENDENCY_MANIFEST: dict[str, list[str]] = {
    "binding": [
        "run_binding_simulation_spontaneous.py",
    ],
    "complex": [
        "run_complex_stability.py",
        "publication_md_pipeline.py",  # May be imported by run_complex_stability
    ],
}


def _get_required_modules(simulation_mode: str) -> list[tuple[str, str, str]]:
    """Return list of (filename, local_path, sha256) for required modules.
    
    Args:
        simulation_mode: One of 'binding' or 'complex'
    
    Returns:
        List of (filename, full_local_path, sha256_hex) tuples
        
    Raises:
        FileNotFoundError: If any required module file is missing locally
    """
    if simulation_mode not in _MODULE_DEPENDENCY_MANIFEST:
        return []
    
    result = []
    for module_name in _MODULE_DEPENDENCY_MANIFEST[simulation_mode]:
        local_path = _BIODYNAMO_PROCESSOR_DIR / module_name
        if not local_path.exists():
            raise FileNotFoundError(
                f"Required module not found locally: {local_path} "
                f"(for {simulation_mode} mode)"
            )
        sha256_hash = _compute_sha256_file(str(local_path))
        result.append((module_name, str(local_path), sha256_hash))
    
    return result


# ────────────────────────────────────────────────────────────────
# Configuration & Data Types
# ────────────────────────────────────────────────────────────────

class OrchestratorPhase(str, Enum):
    """Phases of the autonomous deployment pipeline."""
    INIT = "init"
    PROVISION = "provision"
    PROBE = "probe"
    STALLED_BOOTSTRAP = "stalled_bootstrap"
    INSTALL = "install"
    STAGE_FF = "stage_forcefield"
    VERIFY_FF = "verify_forcefield"
    UPLOAD = "upload"
    LAUNCH = "launch"
    MONITOR = "monitor"
    DOWNLOAD = "download"
    DESTROY = "destroy"
    COMPLETE = "complete"
    FAILED = "failed"
    FAILED_RECOVERABLE = "failed_recoverable"


class SimStatus(str, Enum):
    """Status of a running simulation replica."""
    PENDING = "pending"
    SOLVATING = "solvating"       # addSolvent (CPU-bound, GPU at 0%)
    MINIMIZING = "minimizing"
    EQUILIBRATING = "equilibrating"
    PRODUCTION = "production"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


class SimulationMode(str, Enum):
    """Which simulation script to use.

    * ``BINDING``  – spontaneous binding with flat-bottom restraints
      (run_binding_simulation_spontaneous.py).  Typical for protein-ligand
      encounter simulations.  CLI key arg: ``--steps``.
    * ``COMPLEX``  – publication-grade equilibrium MD for protein-peptide
      or protein-protein complexes (runcomplex_paper_dodecaedrica.py).
      Full 4-phase protocol (min → NVT → NPT → production).
      CLI key arg: ``--ns``.
    """
    BINDING = "binding"
    COMPLEX = "complex"


class StorageBackendType(str, Enum):
    """Durability backend for artifacts."""
    NONE = "none"
    RCLONE = "rclone"


@dataclass
class ArtifactRecord:
    """One tracked artifact for resume, download, and durability flows."""
    category: str
    name: str
    remote_path: str
    local_path: str = ""
    replica_id: Optional[int] = None
    required: bool = True
    storage_path: str = ""
    size_bytes: int = 0
    sha256: str = ""
    synced: bool = False
    last_synced_at: Optional[str] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "remote_path": self.remote_path,
            "local_path": self.local_path,
            "replica_id": self.replica_id,
            "required": self.required,
            "storage_path": self.storage_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "synced": self.synced,
            "last_synced_at": self.last_synced_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArtifactRecord":
        return cls(**data)


@dataclass
class ResumeReplicaSpec:
    """Resume inputs for one replica."""
    replica_id: int
    checkpoint_path: str = ""
    checkpoint_storage_path: str = ""
    prepared_pdb_path: str = ""
    prepared_pdb_storage_path: str = ""
    pdb_path: str = ""
    pdb_storage_path: str = ""
    checkpoint_step: int = 0
    skip_equilibration: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "checkpoint_path": self.checkpoint_path,
            "checkpoint_storage_path": self.checkpoint_storage_path,
            "prepared_pdb_path": self.prepared_pdb_path,
            "prepared_pdb_storage_path": self.prepared_pdb_storage_path,
            "pdb_path": self.pdb_path,
            "pdb_storage_path": self.pdb_storage_path,
            "checkpoint_step": self.checkpoint_step,
            "skip_equilibration": self.skip_equilibration,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResumeReplicaSpec":
        return cls(**data)


@dataclass
class ResumeSpec:
    """Persisted resume contract for restarting a job or handing it to a new pod."""
    job_id: str
    simulation_mode: str
    run_dir: str = ""
    selected_forcefield: str = ""
    pdb_path: str = ""
    simulation_script: str = ""
    simulation_script_sha256: str = ""
    extractor_script: str = ""
    extractor_script_sha256: str = ""
    created_at: str = field(default_factory=_utcnow_iso)
    target_steps: int = 0
    target_production_ns: float = 0.0
    storage_backend: str = StorageBackendType.NONE.value
    storage_remote_root: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    replicas: List[ResumeReplicaSpec] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "simulation_mode": self.simulation_mode,
            "run_dir": self.run_dir,
            "selected_forcefield": self.selected_forcefield,
            "pdb_path": self.pdb_path,
            "simulation_script": self.simulation_script,
            "simulation_script_sha256": self.simulation_script_sha256,
            "extractor_script": self.extractor_script,
            "extractor_script_sha256": self.extractor_script_sha256,
            "created_at": self.created_at,
            "target_steps": self.target_steps,
            "target_production_ns": self.target_production_ns,
            "storage_backend": self.storage_backend,
            "storage_remote_root": self.storage_remote_root,
            "config": self.config,
            "replicas": [replica.to_dict() for replica in self.replicas],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResumeSpec":
        payload = dict(data)
        payload["replicas"] = [
            ResumeReplicaSpec.from_dict(item)
            for item in payload.get("replicas", [])
        ]
        return cls(**payload)


@dataclass
class MDArtifactManifest:
    """Job-level manifest of tracked artifacts and durability state."""
    job_id: str
    simulation_mode: str
    run_dir: str = ""
    selected_forcefield: str = ""
    local_output_dir: str = ""
    storage_backend: str = StorageBackendType.NONE.value
    storage_remote_root: str = ""
    execution_class: str = "research"  # W5-3
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    artifacts: List[ArtifactRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "simulation_mode": self.simulation_mode,
            "run_dir": self.run_dir,
            "selected_forcefield": self.selected_forcefield,
            "local_output_dir": self.local_output_dir,
            "storage_backend": self.storage_backend,
            "storage_remote_root": self.storage_remote_root,
            "execution_class": self.execution_class,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MDArtifactManifest":
        payload = dict(data)
        payload["artifacts"] = [
            ArtifactRecord.from_dict(item)
            for item in payload.get("artifacts", [])
        ]
        return cls(**payload)


@dataclass
class MDJobConfig:
    """Configuration for an autonomous MD simulation job."""
    # ── Input ──
    pdb_path: str                      # Local path to input PDB
    simulation_script: str = ""        # Local path to simulation script
    simulation_script_sha256: str = ""
    extractor_script: str = ""         # Local path to extract_latest_pdb_every_10min.py
    extractor_script_sha256: str = ""
    simulation_mode: SimulationMode = SimulationMode.BINDING
    ligand_smiles: str = ""
    docked_ligand_pdb: str = ""
    
    # ── Simulation params ──
    steps: int = 75_000_000            # 300 ns at 4 fs (HMR) — used by BINDING mode
    production_ns: float = 100.0       # Production time in ns — used by COMPLEX mode
    n_replicas: int = 1                # Number of replicas
    padding: float = 1.1               # Solvent padding (nm)
    prepare: bool = True               # Solvate + minimize + equilibrate
    extra_args: str = ""               # Additional CLI args
    
    # ── GPU ──
    gpu_type: GPUType = GPUType.RTX_5080
    gpu_fallback_cascade: List[GPUType] = field(default_factory=lambda: [
        GPUType.RTX_5080,
        GPUType.RTX_4090,
        GPUType.A100_80GB,
        GPUType.L40S,
    ])
    n_gpus: int = 1                    # GPUs to rent (>= n_replicas)
    max_price_per_hour: float = 0.50   # USD/hr ceiling
    min_reliability: float = 0.97      # Vast reliability floor
    required_disk_gb: float = 0.0      # Explicit disk request; 0 = auto-estimate
    install_footprint_gb: float = 35.0 # OpenMM/conda/charmm staging reserve
    disk_buffer_gb: float = 25.0       # Safety headroom for logs/checkpoints/results
    
    # ── SSH ──
    ssh_key_path: str = ""             # Defaults to ~/.ssh/vast_key
    local_output_dir: str = ""
    provision_timeout_sec: int = 900   # Vast pods can take ~8+ min to become ready
    provision_poll_interval_sec: int = 10
    ssh_probe_attempts: int = 24
    ssh_probe_sleep_sec: int = 10
    bootstrap_probe_attempts: int = 6
    bootstrap_probe_sleep_sec: float = 2.0
    
    # ── Force-field staging ──
    local_charmm36_xml: str = ""       # Local charmm36_2024.xml
    local_charmm36_dir: str = ""       # Local charmm36_2024/ dir
    local_charmm_ffxml_dir: str = ""   # Vendored openmmforcefields ffxml/charmm directory (bundle-capable)
    _ff_policy: object = field(default=None, repr=False)  # ForceFieldPolicy (P0-03)
    
    # ── Cost guard rails ──
    max_total_cost_usd: float = 10.0   # Auto-destroy if exceeded
    max_runtime_hours: float = 48.0    # Max wall-clock hours
    idle_timeout_sec: int = 300        # Auto-destroy after N seconds without progress
    
    # ── Monitoring ──
    monitor_interval_sec: int = 300    # 5 min between status checks
    preserve_instance_on_failure: bool = False
    preserve_instance_on_stop: bool = True
    destroy_on_readiness_non_recovery: bool = True
    readiness_retry_attempts: int = 1
    readiness_retry_backoff_sec: int = 15

    # ── Resume / durability ──
    resume_spec_path: str = ""
    resume_spec: Optional[ResumeSpec] = None
    storage_backend: StorageBackendType | str = StorageBackendType.NONE
    storage_remote: str = ""
    storage_remote_prefix: str = "md-jobs"
    storage_sync_interval_sec: int = 900
    storage_env: Dict[str, str] = field(default_factory=dict)
    install_rclone: bool = True
    
    # ── Docker ──
    # Custom mica-openmm-pod image: pre-baked OpenMM+CUDA+rclone+analysis tools.
    # Falls back to stock pytorch image for legacy/testing scenarios.
    MICA_OPENMM_POD_IMAGE: ClassVar[str] = canonical_md_worker_image()
    STOCK_PYTORCH_IMAGE: ClassVar[str] = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
    docker_image: str = field(default_factory=canonical_md_worker_image)
    
    # ── Job identity ──
    job_id: str = ""
    run_dir_name: str = ""             # e.g. "chimera_v3_run"

    # ── Execution class (W5-3) ──
    execution_class: str = "research"  # "research" | "production" | "audit"

    # ── Auto-SMIC post-MD (W1-2) ──
    auto_smic_analyses: List[str] = field(default_factory=list)  # SMIC analyses to auto-run after download
    auto_smic_stride: int = 10         # Stride for auto-SMIC analyses
    auto_smic_timeout: int = 1800      # Timeout per SMIC analysis

    _CANONICAL_SIMULATION_SCRIPTS: ClassVar[Dict[SimulationMode, str]] = {
        SimulationMode.BINDING: "run_binding_simulation_spontaneous.py",
        SimulationMode.COMPLEX: "runcomplex_paper_dodecaedrica.py",
    }
    _CANONICAL_EXTRACTOR_SCRIPT: ClassVar[str] = "extract_latest_pdb_every_10min.py"
    _MIN_PROVISION_TIMEOUT_SEC: ClassVar[int] = 900

    def _uses_protein_ligand_complex_contract(self) -> bool:
        return (
            self.simulation_mode == SimulationMode.COMPLEX
            and bool(self.ligand_smiles.strip())
            and bool(self.docked_ligand_pdb.strip())
        )

    def _expected_simulation_script_name_for_current_config(self) -> str:
        if self._uses_protein_ligand_complex_contract():
            return "run_complex_stability.py"
        return self._expected_simulation_script_name(self.simulation_mode)
    
    def __post_init__(self):
        if not self.job_id:
            self.job_id = f"md_{uuid.uuid4().hex[:8]}"
        if not self.ssh_key_path:
            self.ssh_key_path = os.path.expanduser("~/.ssh/vast_key")
        if not self.run_dir_name:
            stem = Path(self.pdb_path).stem if self.pdb_path else "md_run"
            self.run_dir_name = f"{stem}_run"
        if not self.local_output_dir:
            base_dir = os.path.dirname(self.pdb_path) if self.pdb_path else os.getcwd()
            self.local_output_dir = os.path.join(base_dir, f"vast_results_{self.job_id}")
        if self.provision_timeout_sec < self._MIN_PROVISION_TIMEOUT_SEC:
            logger.warning(
                "Clamping provision_timeout_sec from %s to %s to avoid destructive readiness underflow",
                self.provision_timeout_sec,
                self._MIN_PROVISION_TIMEOUT_SEC,
            )
            self.provision_timeout_sec = self._MIN_PROVISION_TIMEOUT_SEC
        if not isinstance(self.simulation_mode, SimulationMode):
            self.simulation_mode = SimulationMode(self.simulation_mode)
        if isinstance(self.storage_backend, str):
            self.storage_backend = StorageBackendType(self.storage_backend)
        if self.resume_spec_path and not self.resume_spec and os.path.isfile(self.resume_spec_path):
            with open(self.resume_spec_path, "r", encoding="utf-8") as f:
                self.resume_spec = ResumeSpec.from_dict(json.load(f))
        if self.n_gpus < self.n_replicas:
            self.n_gpus = self.n_replicas
        # Default scripts — look alongside this file or in processors/
        if not self.simulation_script:
            self.simulation_script = self._find_script(
                self._expected_simulation_script_name_for_current_config()
            )
        if not self.extractor_script:
            self.extractor_script = self._find_script(
                "extract_latest_pdb_every_10min.py"
            )
        expected_simulation_name = self._expected_simulation_script_name_for_current_config()
        self.simulation_script, simulation_sha256 = self._resolve_canonical_script(
            self.simulation_script,
            expected_name=expected_simulation_name,
            label="Simulation script",
        )
        self._enforce_expected_sha256(
            "Simulation script",
            simulation_sha256,
            self.simulation_script_sha256,
        )
        if self.resume_spec:
            self._enforce_expected_sha256(
                "Simulation script",
                simulation_sha256,
                self.resume_spec.simulation_script_sha256,
            )
            self.resume_spec.simulation_script = self.simulation_script
            self.resume_spec.simulation_script_sha256 = simulation_sha256
        self.simulation_script_sha256 = simulation_sha256

        self.extractor_script, extractor_sha256 = self._resolve_canonical_script(
            self.extractor_script,
            expected_name=self._CANONICAL_EXTRACTOR_SCRIPT,
            label="Extractor script",
        )
        self._enforce_expected_sha256(
            "Extractor script",
            extractor_sha256,
            self.extractor_script_sha256,
        )
        if self.resume_spec:
            self._enforce_expected_sha256(
                "Extractor script",
                extractor_sha256,
                self.resume_spec.extractor_script_sha256,
            )
            self.resume_spec.extractor_script = self.extractor_script
            self.resume_spec.extractor_script_sha256 = extractor_sha256
        self.extractor_script_sha256 = extractor_sha256
        # Default CHARMM36m paths — cross-platform discovery
        if not self.local_charmm36_xml:
            self.local_charmm36_xml = self._find_charmm36_xml()
        if not self.local_charmm36_dir:
            xml_path = self.local_charmm36_xml
            if xml_path:
                candidate = os.path.splitext(xml_path)[0]  # strip .xml
                if os.path.isdir(candidate):
                    self.local_charmm36_dir = candidate
        # Vendored CHARMM ffxml directory (openmmforcefields)
        if not self.local_charmm_ffxml_dir:
            self.local_charmm_ffxml_dir = self._find_vendored_charmm_ffxml_dir()
        # ── ForceFieldPolicy — policy-driven selection (P0-03) ──────────
        # Only set if not already provided (caller may inject a pre-built policy).
        if not self._ff_policy:
            try:
                from mica.infrastructure.orchestration.forcefield_policy import (
                    ForceFieldSelector,
                )
                self._ff_policy = ForceFieldSelector().select(
                    has_ligand=bool(self.ligand_smiles.strip() or self.docked_ligand_pdb.strip()),
                    gpu_available=True,  # assume GPU available on Vast.ai
                    charmm36_2024_local_xml=self.local_charmm36_xml or "",
                    charmm36_2024_local_dir=self.local_charmm36_dir or "",
                    # Back-compat: selector may use truthiness of this
                    charmm36m_xml_path=self.local_charmm_ffxml_dir or self.local_charmm36_xml or "",
                )
            except ImportError:
                self._ff_policy = None  # graceful: module not yet available

    @classmethod
    def _expected_simulation_script_name(cls, simulation_mode: SimulationMode | str) -> str:
        mode = (
            simulation_mode
            if isinstance(simulation_mode, SimulationMode)
            else SimulationMode(simulation_mode)
        )
        return cls._CANONICAL_SIMULATION_SCRIPTS[mode]

    @classmethod
    def _resolve_canonical_script(
        cls,
        configured_path: str,
        *,
        expected_name: str,
        label: str,
    ) -> tuple[str, str]:
        canonical_path = cls._find_script(expected_name)
        if not canonical_path:
            raise ValueError(f"{label} canonical script not found: {expected_name}")
        canonical_resolved = Path(canonical_path).expanduser().resolve()

        if configured_path:
            configured_candidate = Path(configured_path).expanduser()
            if not configured_candidate.is_file():
                raise ValueError(f"{label} path invalid: {configured_path}")
            configured_resolved = configured_candidate.resolve()
            if configured_resolved != canonical_resolved:
                raise ValueError(
                    f"{label} must reference canonical {expected_name}, got {configured_path}"
                )

        sha256 = _compute_sha256_file(str(canonical_resolved))
        if not sha256:
            raise ValueError(
                f"{label} SHA256 could not be computed: {canonical_resolved}"
            )
        return str(canonical_resolved), sha256

    @staticmethod
    def _enforce_expected_sha256(label: str, actual_sha256: str, expected_sha256: str) -> None:
        if expected_sha256 and expected_sha256.lower() != actual_sha256.lower():
            raise ValueError(
                f"{label} integrity mismatch: expected {expected_sha256} got {actual_sha256}"
            )

    @staticmethod
    def _find_vendored_charmm_ffxml_dir() -> str:
        """Discover vendored openmmforcefields CHARMM ffxml directory.

        Returns:
            Absolute path to `_third_party/openmmforcefields/openmmforcefields/ffxml/charmm`,
            or "" if not found.
        """
        here = Path(__file__).resolve()
        for parent in [here] + list(here.parents)[:10]:
            candidate = (
                parent
                / "_third_party"
                / "openmmforcefields"
                / "openmmforcefields"
                / "ffxml"
                / "charmm"
            )
            if candidate.exists() and candidate.is_dir():
                return str(candidate)
        return ""

    @staticmethod
    def _find_charmm36_xml() -> str:
        """Discover charmm36_2024.xml via openmm or common paths."""
        # 1. Try to locate via openmm package
        try:
            import openmm.app  # type: ignore[import-untyped]
            data_dir = os.path.dirname(openmm.app.__file__)
            candidate = os.path.join(data_dir, "data", "charmm36_2024.xml")
            if os.path.isfile(candidate):
                return candidate
        except ImportError:
            pass
        # 2. Fallback: search common locations (Windows + Linux)
        for pattern in [
            os.path.expanduser("~/.venv/Lib/site-packages/openmm/app/data/charmm36_2024.xml"),
            os.path.expanduser("~/Downloads/MICA/.venv/Lib/site-packages/openmm/app/data/charmm36_2024.xml"),
            "/opt/conda/lib/python3.*/site-packages/openmm/app/data/charmm36_2024.xml",
        ]:
            import glob as _glob
            for match in _glob.glob(pattern):
                if os.path.isfile(match):
                    return match
        return ""

    @staticmethod
    def _find_script(name: str) -> str:
        """Search common locations for a simulation script (cross-platform)."""
        _here = os.path.dirname(__file__)
        candidates = [
            # Relative to this module: ../../workers/dynamo/biodynamo/processors/
            os.path.join(_here, "..", "..", "..",
                         "workers", "dynamo", "biodynamo", "processors", name),
            # Project root processors/ (one more level up)
            os.path.join(_here, "..", "..", "..", "..",
                         "workers", "dynamo", "biodynamo", "processors", name),
            # Alongside the orchestrator itself
            os.path.join(_here, name),
            # Linux common deploy paths
            os.path.expanduser(f"~/mica/workers/dynamo/biodynamo/processors/{name}"),
            # Windows dev paths (legacy)
            os.path.expanduser(os.path.join("~", "Downloads", "MICA", name)),
            os.path.expanduser(os.path.join(
                "~", "Downloads", "MICA",
                "astroflora-core-feature-spectra-worker-integration-1",
                "workers", "dynamo", "biodynamo", "processors", name,
            )),
        ]
        for c in candidates:
            normed = os.path.normpath(c)
            if os.path.isfile(normed):
                return normed
        logger.warning("Simulation script '%s' not found in any known location", name)
        return ""


@dataclass
class ReplicaStatus:
    """Live status of one simulation replica."""
    replica_id: int
    gpu_id: int
    pid: Optional[int] = None
    status: SimStatus = SimStatus.PENDING
    speed_ns_day: float = 0.0
    current_step: int = 0
    current_ns: float = 0.0
    target_ns: float = 0.0
    eta_hours: float = 0.0
    gpu_utilization: float = 0.0
    gpu_memory_used_mb: float = 0.0
    last_log_line: str = ""
    last_check: Optional[datetime] = None


@dataclass 
class OrchestratorState:
    """Full state of the orchestrator — serialisable for persistence."""
    job_id: str
    phase: OrchestratorPhase = OrchestratorPhase.INIT
    instance_id: str = ""
    ssh_host: str = ""
    ssh_port: int = 22
    conda_python: str = ""          # e.g. /opt/miniforge3/bin/python
    run_dir: str = ""               # /workspace/<run_dir_name>
    replicas: Dict[int, ReplicaStatus] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: str = ""
    selected_forcefield: str = ""
    local_output_dir: str = ""
    latest_job_manifest_path: str = ""
    latest_resume_spec_path: str = ""
    safe_stop_completed: bool = False
    stop_reason: str = ""
    scientific_completion_achieved: bool = False
    destroy_attempted: bool = False
    destroy_succeeded: bool = False
    teardown_unconfirmed: bool = False
    teardown_failure_reason: str = ""
    pod_api_url: str = ""          # e.g. http://host:8787 — populated by _start_pod_api()
    pod_api_external_port: int = _POD_API_PORT  # Vast-mapped external port for pod_api
    pod_api_available: bool = False
    events: List[Dict[str, Any]] = field(default_factory=list)
    
    def log_event(self, phase: str, message: str, **extra):
        ts = _utcnow_iso()
        evt = {"ts": ts, "phase": phase, "msg": message, **extra}
        self.events.append(evt)
        logger.info(f"[{phase}] {message}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "phase": self.phase.value,
            "instance_id": self.instance_id,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "conda_python": self.conda_python,
            "run_dir": self.run_dir,
            "selected_forcefield": self.selected_forcefield,
            "total_cost_usd": self.total_cost_usd,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "local_output_dir": self.local_output_dir,
            "latest_job_manifest_path": self.latest_job_manifest_path,
            "latest_resume_spec_path": self.latest_resume_spec_path,
            "safe_stop_completed": self.safe_stop_completed,
            "stop_reason": self.stop_reason,
            "scientific_completion_achieved": self.scientific_completion_achieved,
            "destroy_attempted": self.destroy_attempted,
            "destroy_succeeded": self.destroy_succeeded,
            "teardown_unconfirmed": self.teardown_unconfirmed,
            "teardown_failure_reason": self.teardown_failure_reason,
            "pod_api_url": self.pod_api_url,
            "pod_api_available": self.pod_api_available,
            "replicas": {
                rid: {
                    "gpu_id": rs.gpu_id,
                    "pid": rs.pid,
                    "status": rs.status.value,
                    "speed_ns_day": rs.speed_ns_day,
                    "current_step": rs.current_step,
                    "current_ns": rs.current_ns,
                    "target_ns": rs.target_ns,
                    "eta_hours": rs.eta_hours,
                    "gpu_utilization": rs.gpu_utilization,
                    "gpu_memory_used_mb": rs.gpu_memory_used_mb,
                    "last_log_line": rs.last_log_line,
                    "last_check": rs.last_check.isoformat() if rs.last_check else None,
                }
                for rid, rs in self.replicas.items()
            },
            "events": self.events,
        }


# ────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────

class VastMDOrchestrator:
    """
    Autonomous end-to-end orchestrator for MD simulations on Vast.ai.
    
    Usage:
        config = MDJobConfig(
            pdb_path=r"C:\\Users\\busta\\Downloads\\MICA\\chimeras\\chimera_v3_4nm_az144_el0.pdb",
            steps=75_000_000,
            n_replicas=1,
        )
        orch = VastMDOrchestrator(config)
        state = await orch.run()
        # state.phase == OrchestratorPhase.COMPLETE  ← success
    """

    def __init__(
        self,
        config: MDJobConfig,
        provider: Optional[CloudProvider] = None,
        cloud_orchestrator: Optional["CloudOrchestrator"] = None,
        ssh: Optional[ResilientSSHExecutor] = None,
        on_event: Optional[Callable[[str, str], None]] = None,
    ):
        self.cfg = config
        self.provider: CloudProvider = provider or VastProvider(
            ssh_key_path=config.ssh_key_path
        )
        self.cloud_orchestrator = cloud_orchestrator
        self.ssh = ssh or ResilientSSHExecutor()
        self.state = OrchestratorState(
            job_id=config.job_id,
            local_output_dir=config.local_output_dir,
        )
        self._on_event = on_event  # callback(phase, message)
        self._stop_requested = False
        self._last_storage_sync_at: float = 0.0
        self._manifest_storage_lock = asyncio.Lock()
        self._tunnel_proc: Optional[asyncio.subprocess.Process] = None  # SSH tunnel for Pod API
        self._collected_teardown_proof: Optional[Dict[str, Any]] = None

    # ── helpers ──────────────────────────────────────────────────

    @property
    def _uses_custom_image(self) -> bool:
        """True when the pod runs the pre-baked mica-openmm-pod image.

        When True, OpenMM, rclone, and analysis tools are already installed
        in the image and runtime install phases can be skipped entirely.
        """
        return "mica-openmm-pod" in self.cfg.docker_image

    def _emit(self, phase: str, msg: str, **extra):
        self.state.log_event(phase, msg, **extra)
        if self._on_event:
            snapshot = self.state.to_dict() if hasattr(self.state, "to_dict") else {}
            try:
                self._on_event(phase, msg, snapshot)
            except TypeError:
                self._on_event(phase, msg)

    async def _ssh(
        self,
        cmd: str,
        protocol: CommandProtocol = CommandProtocol.RETRY_3X,
        timeout: int = 60,
    ) -> SSHResult:
        """SSH shortcut using current instance."""
        return await self.ssh.execute_with_protocol(
            host=self.state.ssh_host,
            port=self.state.ssh_port,
            command=cmd,
            protocol=protocol,
            timeout=timeout,
            key_path=self.cfg.ssh_key_path,
        )

    def _safe_remote_kill_command(self, pattern: str, signal: Optional[str] = None) -> str:
        """Return a shell snippet that kills matching remote processes without killing the SSH shell.

        ``pkill -f <pattern>`` can match the remote shell that is executing the cleanup
        command because the pattern appears in that shell's command line. Filter out the
        current shell and its parent so probe/launch cleanup stays non-destructive.
        """
        quoted_pattern = shlex.quote(pattern)
        kill_cmd = f"kill -{signal} \"$pid\"" if signal else 'kill "$pid"'
        return (
            "if command -v pgrep >/dev/null 2>&1; then "
            f"for pid in $(pgrep -f -- {quoted_pattern} || true); do "
            '[ "$pid" = "$$" ] && continue; '
            '[ "$pid" = "$PPID" ] && continue; '
            f"{kill_cmd} 2>/dev/null || true; "
            "done; "
            "fi; true"
        )

    async def _scp_up(
        self,
        local: str,
        remote: str,
        recursive: bool = False,
        timeout: int = 120,
    ) -> SSHResult:
        return await self.ssh.scp_upload(
            host=self.state.ssh_host,
            port=self.state.ssh_port,
            local_path=local,
            remote_path=remote,
            key_path=self.cfg.ssh_key_path,
            recursive=recursive,
            timeout=timeout,
        )

    async def _scp_down(
        self,
        remote: str,
        local: str,
        recursive: bool = False,
        timeout: int = 300,
    ) -> SSHResult:
        return await self.ssh.scp_download(
            host=self.state.ssh_host,
            port=self.state.ssh_port,
            remote_path=remote,
            local_path=local,
            key_path=self.cfg.ssh_key_path,
            recursive=recursive,
            timeout=timeout,
        )

    def _target_ns_for_job(self) -> float:
        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            return float(self.cfg.production_ns)
        return float(self.cfg.steps) * 4e-6

    def _artifact_root(self) -> str:
        return self.state.run_dir or f"/workspace/{self.cfg.run_dir_name}"

    def _count_pdb_atoms(self) -> int:
        pdb_path = self.cfg.pdb_path
        if not pdb_path or not os.path.isfile(pdb_path):
            return 0
        atom_count = 0
        try:
            with open(pdb_path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.startswith(("ATOM  ", "HETATM")):
                        atom_count += 1
        except OSError:
            return 0
        return atom_count

    def _estimated_dcd_frames_per_replica(self) -> int:
        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            total_ns = max(float(self.cfg.production_ns), 0.0)
        else:
            total_ns = max(float(self.cfg.steps) * 4e-6, 0.0)
        frame_interval_ns = 0.01  # 2500 steps @ 4 fs = 10 ps = 0.01 ns
        return max(1, int(math.ceil(total_ns / frame_interval_ns)))

    def _estimate_required_disk_gb(self) -> float:
        if self.cfg.required_disk_gb > 0:
            return float(self.cfg.required_disk_gb)

        atom_count = self._count_pdb_atoms()
        if atom_count <= 0:
            return float(self.cfg.install_footprint_gb + self.cfg.disk_buffer_gb)

        frames = self._estimated_dcd_frames_per_replica()
        bytes_per_frame = atom_count * 3 * 4  # float32 x/y/z
        dcd_bytes = bytes_per_frame * frames * max(1, self.cfg.n_replicas)
        dcd_bytes = int(dcd_bytes * 1.25)  # metadata + variability safety factor
        dcd_gb = dcd_bytes / (1024 ** 3)

        return float(
            math.ceil(
                dcd_gb
                + float(self.cfg.install_footprint_gb)
                + float(self.cfg.disk_buffer_gb)
            )
        )

    def _replica_dir(self, replica_id: int) -> str:
        return f"{self._artifact_root()}/runs/replica_{replica_id}"

    def _replica_run_name(self, replica_id: int) -> str:
        return f"replica_{replica_id}"

    def _replica_checkpoint_filename(self, replica_id: int) -> str:
        if self._uses_protein_ligand_complex_contract():
            return "final.chk"
        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            return f"{self._replica_run_name(replica_id)}.chk"
        return f"{self._replica_run_name(replica_id)}_chk.xml"

    def _replica_checkpoint_remote_path(self, replica_id: int) -> str:
        return f"{self._replica_dir(replica_id)}/{self._replica_checkpoint_filename(replica_id)}"

    def _replica_prepared_pdb_filename(self, replica_id: int) -> str:
        if self._uses_protein_ligand_complex_contract():
            return "equilibrated_complex.pdb"
        return f"{self._replica_run_name(replica_id)}_prepared.pdb"

    def _replica_prepared_pdb_remote_path(self, replica_id: int) -> str:
        return f"{self._replica_dir(replica_id)}/{self._replica_prepared_pdb_filename(replica_id)}"

    def _replica_log_remote_path(self, replica_id: int) -> str:
        return f"{self._artifact_root()}/logs/replica_{replica_id}.log"

    def _replica_latest_pdb_filename(self, replica_id: int) -> str:
        return f"{self._replica_run_name(replica_id)}_latest.pdb"

    def _replica_snapshots_pdb_filename(self, replica_id: int) -> str:
        return f"{self._replica_run_name(replica_id)}_snapshots.pdb"

    def _replica_metrics_filename(self, replica_id: int) -> str:
        if self._uses_protein_ligand_complex_contract():
            return "production_energy.csv"
        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            return f"{self._replica_run_name(replica_id)}_energy.csv"
        return f"{self._replica_run_name(replica_id)}.csv"

    def _replica_results_filename(self, replica_id: int) -> str:
        return f"{self._replica_run_name(replica_id)}_results.json"

    def _get_download_manifest(self, replica_id: int) -> List[str]:
        if self._uses_protein_ligand_complex_contract():
            return [
                "production.dcd",
                self._replica_metrics_filename(replica_id),
                self._replica_prepared_pdb_filename(replica_id),
                self._replica_checkpoint_filename(replica_id),
                "final_state.xml",
                self._replica_results_filename(replica_id),
            ]
        common = [
            f"{self._replica_run_name(replica_id)}.dcd",
            self._replica_metrics_filename(replica_id),
            self._replica_prepared_pdb_filename(replica_id),
        ]
        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            return common + [self._replica_checkpoint_filename(replica_id)]
        return common + [
            self._replica_checkpoint_filename(replica_id),
            self._replica_latest_pdb_filename(replica_id),
            self._replica_snapshots_pdb_filename(replica_id),
        ]

    def _resume_replica_specs(self) -> Dict[int, ResumeReplicaSpec]:
        if not self.cfg.resume_spec:
            return {}
        return {replica.replica_id: replica for replica in self.cfg.resume_spec.replicas}

    def _effective_local_pdb_source(self) -> str:
        for replica in self._resume_replica_specs().values():
            for candidate in [replica.prepared_pdb_path, replica.pdb_path]:
                if candidate and os.path.isfile(candidate):
                    return candidate
        return self.cfg.pdb_path if self.cfg.pdb_path and os.path.isfile(self.cfg.pdb_path) else ""

    def _effective_remote_pdb_restore_source(self) -> str:
        for replica in self._resume_replica_specs().values():
            for candidate in [replica.prepared_pdb_storage_path, replica.pdb_storage_path]:
                if candidate:
                    return candidate
        return ""

    def _effective_remote_pdb_name(self) -> str:
        local_source = self._effective_local_pdb_source()
        if local_source:
            return os.path.basename(local_source)
        restore_source = self._effective_remote_pdb_restore_source()
        if restore_source:
            return os.path.basename(restore_source.rstrip("/"))
        if self.cfg.pdb_path:
            return os.path.basename(self.cfg.pdb_path)
        return "input.pdb"

    def _uses_protein_ligand_complex_contract(self) -> bool:
        return self.cfg._uses_protein_ligand_complex_contract()

    def _effective_remote_docked_pose_name(self) -> str:
        if self.cfg.docked_ligand_pdb:
            return os.path.basename(self.cfg.docked_ligand_pdb)
        return "docked_ligand.pdb"

    def _remote_docked_pose_path(self, run_dir: str) -> str:
        return f"{run_dir}/inputs/{self._effective_remote_docked_pose_name()}"

    def _remote_simulation_script_path(self, run_dir: str, script_name: str) -> str:
        if self._uses_protein_ligand_complex_contract() and script_name == "run_complex_stability.py":
            return f"{run_dir}/workers/dynamo/biodynamo/processors/{script_name}"
        return f"{run_dir}/{script_name}"

    def _required_remote_support_files(self, run_dir: str) -> list[tuple[str, str, str, str]]:
        if self._uses_protein_ligand_complex_contract():
            repo_root = Path(__file__).resolve().parents[4]
            support_files = [
                (
                    "mica.__init__.py",
                    repo_root / "src" / "mica" / "__init__.py",
                    f"{run_dir}/src/mica/__init__.py",
                ),
                (
                    "mica.sim.__init__.py",
                    repo_root / "src" / "mica" / "sim" / "__init__.py",
                    f"{run_dir}/src/mica/sim/__init__.py",
                ),
                (
                    "run_complex_stability.py",
                    repo_root / "workers" / "dynamo" / "biodynamo" / "processors" / "run_complex_stability.py",
                    f"{run_dir}/workers/dynamo/biodynamo/processors/run_complex_stability.py",
                ),
                (
                    "publication_md_pipeline.py",
                    repo_root / "workers" / "dynamo" / "biodynamo" / "processors" / "publication_md_pipeline.py",
                    f"{run_dir}/workers/dynamo/biodynamo/processors/publication_md_pipeline.py",
                ),
                (
                    "mdops_loader.py",
                    repo_root / "workers" / "dynamo" / "biodynamo" / "core" / "mdops_loader.py",
                    f"{run_dir}/workers/dynamo/biodynamo/core/mdops_loader.py",
                ),
                (
                    "physiological_topology_kernel.py",
                    repo_root / "src" / "mica" / "sim" / "physiological_topology_kernel.py",
                    f"{run_dir}/src/mica/sim/physiological_topology_kernel.py",
                ),
                (
                    "scientific_task_graph.py",
                    repo_root / "src" / "mica" / "sim" / "scientific_task_graph.py",
                    f"{run_dir}/src/mica/sim/scientific_task_graph.py",
                ),
                (
                    "biostate.py",
                    repo_root / "src" / "mica" / "drivers" / "biostate.py",
                    f"{run_dir}/src/mica/drivers/biostate.py",
                ),
                (
                    "protonation_handler.py",
                    repo_root / "workers" / "dynamo" / "biodynamo" / "protonation" / "protonation_handler.py",
                    f"{run_dir}/workers/dynamo/biodynamo/protonation/protonation_handler.py",
                ),
            ]
            openmm_compiler_root = repo_root / "src" / "mica" / "sim" / "openmm_compiler"
            if not openmm_compiler_root.is_dir():
                raise FileNotFoundError(
                    f"Missing required complex support package: {openmm_compiler_root}"
                )
            for local_path in sorted(openmm_compiler_root.rglob("*.py")):
                relative_path = local_path.relative_to(repo_root / "src").as_posix()
                support_files.append(
                    (
                        f"openmm_compiler/{local_path.name}",
                        local_path,
                        f"{run_dir}/src/{relative_path}",
                    )
                )
            staged: list[tuple[str, str, str, str]] = []
            for name, local_path, remote_path in support_files:
                if not local_path.is_file():
                    raise FileNotFoundError(f"Missing required complex support file: {local_path}")
                staged.append((name, str(local_path), remote_path, _compute_sha256_file(str(local_path))))
            return staged

        return [
            (module_name, local_path, f"{run_dir}/{module_name}", local_sha256)
            for module_name, local_path, local_sha256 in _get_required_modules(self.cfg.simulation_mode.value)
        ]

    def _remote_python_prefix(self, run_dir: str, gpu_id: int) -> str:
        pythonpath_entries = [
            f"{run_dir}/src",
            run_dir,
        ]
        joined_pythonpath = ":".join(pythonpath_entries)
        return f"PYTHONPATH={shlex.quote(joined_pythonpath)} CUDA_VISIBLE_DEVICES={gpu_id}"

    def _job_manifest_local_path(self) -> str:
        return os.path.join(self.cfg.local_output_dir, "job_manifest.json")

    def _resume_spec_local_path(self) -> str:
        return os.path.join(self.cfg.local_output_dir, "resume_spec.json")

    def _job_manifest_remote_path(self) -> str:
        return f"{self._artifact_root()}/manifest/job_manifest.json"

    def _resume_spec_remote_path(self) -> str:
        return f"{self._artifact_root()}/manifest/resume_spec.json"

    def _storage_enabled(self) -> bool:
        return self.cfg.storage_backend == StorageBackendType.RCLONE and bool(self.cfg.storage_remote)

    def _storage_remote_root(self) -> str:
        root = self.cfg.storage_remote.rstrip("/")
        if self.cfg.storage_remote_prefix:
            root = f"{root}/{self.cfg.storage_remote_prefix.strip('/')}"
        return f"{root}/{self.cfg.job_id}"

    def _storage_destination_for_artifact(
        self,
        category: str,
        name: str,
        replica_id: Optional[int] = None,
    ) -> str:
        if not self._storage_enabled():
            return ""

        root = self._storage_remote_root()
        if category == "inputs":
            return f"{root}/inputs/{name}"
        if category == "logs":
            return f"{root}/telemetry/{name}"
        if category == "outputs":
            if replica_id is not None and name == self._replica_checkpoint_filename(replica_id):
                return f"{root}/checkpoints/replica_{replica_id}/{name}"
            if replica_id is not None:
                return f"{root}/outputs/replica_{replica_id}/{name}"
            return f"{root}/outputs/{name}"
        if category == "manifest":
            return f"{root}/manifest/{name}"
        return f"{root}/{category}/{name}"

    def _remote_env_exports(self) -> str:
        exports = []
        for key, value in sorted(self.cfg.storage_env.items()):
            exports.append(f"export {key}={shlex.quote(value)}")
        return " && ".join(exports)

    def _config_snapshot(self) -> Dict[str, Any]:
        return {
            "pdb_path": self.cfg.pdb_path,
            "simulation_script": self.cfg.simulation_script,
            "simulation_script_sha256": self.cfg.simulation_script_sha256,
            "extractor_script": self.cfg.extractor_script,
            "extractor_script_sha256": self.cfg.extractor_script_sha256,
            "simulation_mode": self.cfg.simulation_mode.value,
            "ligand_smiles": self.cfg.ligand_smiles,
            "docked_ligand_pdb": self.cfg.docked_ligand_pdb,
            "steps": self.cfg.steps,
            "production_ns": self.cfg.production_ns,
            "n_replicas": self.cfg.n_replicas,
            "padding": self.cfg.padding,
            "prepare": self.cfg.prepare,
            "extra_args": self.cfg.extra_args,
            "gpu_type": self.cfg.gpu_type.value,
            "n_gpus": self.cfg.n_gpus,
            "max_price_per_hour": self.cfg.max_price_per_hour,
            "max_total_cost_usd": self.cfg.max_total_cost_usd,
            "max_runtime_hours": self.cfg.max_runtime_hours,
            "monitor_interval_sec": self.cfg.monitor_interval_sec,
            "preserve_instance_on_failure": self.cfg.preserve_instance_on_failure,
            "preserve_instance_on_stop": self.cfg.preserve_instance_on_stop,
            "run_dir_name": self.cfg.run_dir_name,
            "local_output_dir": self.cfg.local_output_dir,
            "storage_backend": self.cfg.storage_backend.value,
            "storage_remote": self.cfg.storage_remote,
            "storage_remote_prefix": self.cfg.storage_remote_prefix,
            "storage_sync_interval_sec": self.cfg.storage_sync_interval_sec,
        }

    @staticmethod
    def _compute_sha256(path: str) -> str:
        return _compute_sha256_file(path)

    async def _binding_checkpoint_step(self, replica_id: int) -> int:
        if self.cfg.simulation_mode != SimulationMode.BINDING:
            return 0
        chk = self._replica_checkpoint_remote_path(replica_id)
        result = await self._ssh(
            f"grep -o 'stepCount=\"[0-9]*\"' {chk} 2>/dev/null | head -1 | grep -o '[0-9]*' || true",
            protocol=CommandProtocol.FAIL_FAST,
            timeout=10,
        )
        try:
            return int(result.stdout.strip()) if result.stdout.strip() else 0
        except ValueError:
            return 0

    async def _has_strict_completion_evidence(self, replica_id: int, log_path: str) -> bool:
        markers = ["Simulation Complete\\."] if self.cfg.simulation_mode == SimulationMode.BINDING else [
            "SIMULATION COMPLETE",
            "Production complete",
        ]
        marker_pattern = "|".join(markers)
        marker_cmd = f"grep -E {shlex.quote(marker_pattern)} {shlex.quote(log_path)} | tail -1 || true"
        marker_result = await self._ssh(marker_cmd, protocol=CommandProtocol.FAIL_FAST, timeout=10)
        required_files = self._get_download_manifest(replica_id)
        file_checks = " && ".join(
            [f"test -s {shlex.quote(f'{self._replica_dir(replica_id)}/{fname}')}" for fname in required_files]
        )
        artifact_result = await self._ssh(
            f"({file_checks}) && echo COMPLETE_ARTIFACTS || echo INCOMPLETE_ARTIFACTS",
            protocol=CommandProtocol.FAIL_FAST,
            timeout=15,
        )
        if self.cfg.simulation_mode == SimulationMode.BINDING:
            current_step = await self._binding_checkpoint_step(replica_id)
            return (
                "COMPLETE_ARTIFACTS" in artifact_result.stdout
                and bool(marker_result.stdout.strip())
                and current_step >= self.cfg.steps
            )
        return "COMPLETE_ARTIFACTS" in artifact_result.stdout and bool(marker_result.stdout.strip())

    def _build_artifact_manifest(self) -> MDArtifactManifest:
        local_root = self.state.local_output_dir or self.cfg.local_output_dir
        existing_manifest: Optional[MDArtifactManifest] = None
        existing_artifacts: Dict[tuple[str, str, Optional[int]], ArtifactRecord] = {}
        manifest_path = self._job_manifest_local_path()
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    existing_manifest = MDArtifactManifest.from_dict(json.load(f))
                existing_artifacts = {
                    (artifact.category, artifact.name, artifact.replica_id): artifact
                    for artifact in existing_manifest.artifacts
                }
            except Exception as exc:
                logger.warning("Failed to load existing artifact manifest for sync-state merge: %s", exc)

        manifest = MDArtifactManifest(
            job_id=self.cfg.job_id,
            simulation_mode=self.cfg.simulation_mode.value,
            run_dir=self.state.run_dir,
            selected_forcefield=self.state.selected_forcefield,
            local_output_dir=local_root,
            storage_backend=self.cfg.storage_backend.value,
            storage_remote_root=self._storage_remote_root() if self._storage_enabled() else "",
            execution_class=self.cfg.execution_class,
            created_at=existing_manifest.created_at if existing_manifest is not None else _utcnow_iso(),
        )

        def add_artifact(category: str, name: str, remote_path: str, local_path: str = "", replica_id: Optional[int] = None):
            artifact = ArtifactRecord(
                category=category,
                name=name,
                remote_path=remote_path,
                local_path=local_path,
                replica_id=replica_id,
                storage_path=self._storage_destination_for_artifact(category, name, replica_id),
            )
            if local_path and os.path.isfile(local_path):
                artifact.size_bytes = os.path.getsize(local_path)
                artifact.sha256 = self._compute_sha256(local_path)
            previous = existing_artifacts.get((category, name, replica_id))
            if previous is not None:
                artifact.synced = previous.synced
                artifact.last_synced_at = previous.last_synced_at
                artifact.error = previous.error
            manifest.artifacts.append(artifact)

        pdb_source = self._effective_local_pdb_source()
        add_artifact("inputs", os.path.basename(self._effective_remote_pdb_name()), f"{self._artifact_root()}/{self._effective_remote_pdb_name()}", pdb_source)
        if self.cfg.simulation_script:
            add_artifact("inputs", os.path.basename(self.cfg.simulation_script), f"{self._artifact_root()}/{os.path.basename(self.cfg.simulation_script)}", self.cfg.simulation_script)
        if self.cfg.extractor_script:
            add_artifact("inputs", os.path.basename(self.cfg.extractor_script), f"{self._artifact_root()}/{os.path.basename(self.cfg.extractor_script)}", self.cfg.extractor_script)

        for replica_id in range(1, self.cfg.n_replicas + 1):
            local_replica_dir = os.path.join(local_root, f"replica_{replica_id}")
            for fname in self._get_download_manifest(replica_id):
                add_artifact(
                    "outputs",
                    fname,
                    f"{self._replica_dir(replica_id)}/{fname}",
                    os.path.join(local_replica_dir, fname),
                    replica_id=replica_id,
                )
            add_artifact(
                "logs",
                f"replica_{replica_id}.log",
                self._replica_log_remote_path(replica_id),
                os.path.join(local_replica_dir, f"replica_{replica_id}.log"),
                replica_id=replica_id,
            )
        manifest.updated_at = _utcnow_iso()
        return manifest

    async def _persist_synced_artifact_manifest(self, synced_storage_paths: set[str], synced_at: str) -> bool:
        manifest_path = self._job_manifest_local_path()
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = MDArtifactManifest.from_dict(json.load(f))
        else:
            manifest = self._build_artifact_manifest()

        touched = False
        for artifact in manifest.artifacts:
            if artifact.storage_path and artifact.storage_path in synced_storage_paths:
                artifact.synced = True
                artifact.last_synced_at = synced_at
                artifact.error = ""
                touched = True

        if not touched:
            return False

        manifest.updated_at = synced_at
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)
        self.state.latest_job_manifest_path = manifest_path

        if self.state.run_dir:
            await self._scp_up(manifest_path, self._job_manifest_remote_path(), timeout=60)
        return True

    def _build_resume_spec(self) -> ResumeSpec:
        local_root = self.state.local_output_dir or self.cfg.local_output_dir
        replicas: List[ResumeReplicaSpec] = []
        resume_map = self._resume_replica_specs()
        for replica_id in range(1, self.cfg.n_replicas + 1):
            local_replica_dir = os.path.join(local_root, f"replica_{replica_id}")
            existing = resume_map.get(replica_id)
            checkpoint_local = os.path.join(local_replica_dir, self._replica_checkpoint_filename(replica_id))
            prepared_local = os.path.join(local_replica_dir, self._replica_prepared_pdb_filename(replica_id))
            replicas.append(
                ResumeReplicaSpec(
                    replica_id=replica_id,
                    checkpoint_path=checkpoint_local if os.path.isfile(checkpoint_local) else (existing.checkpoint_path if existing else ""),
                    checkpoint_storage_path=(
                        f"{self._storage_remote_root()}/checkpoints/replica_{replica_id}/{self._replica_checkpoint_filename(replica_id)}"
                        if self._storage_enabled() else (existing.checkpoint_storage_path if existing else "")
                    ),
                    prepared_pdb_path=prepared_local if os.path.isfile(prepared_local) else (existing.prepared_pdb_path if existing else ""),
                    prepared_pdb_storage_path=(
                        f"{self._storage_remote_root()}/outputs/replica_{replica_id}/{self._replica_prepared_pdb_filename(replica_id)}"
                        if self._storage_enabled() else (existing.prepared_pdb_storage_path if existing else "")
                    ),
                    pdb_path=self._effective_local_pdb_source() or (existing.pdb_path if existing else ""),
                    pdb_storage_path=(
                        f"{self._storage_remote_root()}/inputs/{self._effective_remote_pdb_name()}"
                        if self._storage_enabled() else (existing.pdb_storage_path if existing else "")
                    ),
                    checkpoint_step=self.state.replicas.get(replica_id, ReplicaStatus(replica_id, replica_id - 1)).current_step,
                    skip_equilibration=True,
                )
            )

        return ResumeSpec(
            job_id=self.cfg.job_id,
            simulation_mode=self.cfg.simulation_mode.value,
            run_dir=self.state.run_dir,
            selected_forcefield=self.state.selected_forcefield,
            pdb_path=self._effective_local_pdb_source() or self.cfg.pdb_path,
            simulation_script=self.cfg.simulation_script,
            simulation_script_sha256=self.cfg.simulation_script_sha256,
            extractor_script=self.cfg.extractor_script,
            extractor_script_sha256=self.cfg.extractor_script_sha256,
            target_steps=self.cfg.steps,
            target_production_ns=self.cfg.production_ns,
            storage_backend=self.cfg.storage_backend.value,
            storage_remote_root=self._storage_remote_root() if self._storage_enabled() else "",
            config=self._config_snapshot(),
            replicas=replicas,
        )

    async def _persist_runtime_manifests(self) -> None:
        async with self._manifest_storage_lock:
            await self._persist_runtime_manifests_unlocked()

    async def _persist_runtime_manifests_unlocked(self) -> None:
        os.makedirs(self.cfg.local_output_dir, exist_ok=True)
        manifest = self._build_artifact_manifest()
        resume_spec = self._build_resume_spec()

        with open(self._job_manifest_local_path(), "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)
        with open(self._resume_spec_local_path(), "w", encoding="utf-8") as f:
            json.dump(resume_spec.to_dict(), f, indent=2)

        self.state.latest_job_manifest_path = self._job_manifest_local_path()
        self.state.latest_resume_spec_path = self._resume_spec_local_path()

        if not self.state.run_dir:
            return

        await self._ssh(f"mkdir -p {self._artifact_root()}/manifest", timeout=15)
        await self._scp_up(self._job_manifest_local_path(), self._job_manifest_remote_path(), timeout=60)
        await self._scp_up(self._resume_spec_local_path(), self._resume_spec_remote_path(), timeout=60)

    async def _ensure_rclone_available(self) -> None:
        if not self._storage_enabled():
            return
        if self._uses_custom_image:
            self._emit("rclone", "Custom image — rclone pre-installed, skipping runtime install")
            return
        env_exports = self._remote_env_exports()
        prefix = f"{env_exports} && " if env_exports else ""
        command = (
            prefix +
            "(command -v rclone >/dev/null 2>&1 || "
            "(curl -fsSL https://rclone.org/install.sh | bash >/tmp/mica_rclone_install.log 2>&1))"
        )
        result = await self._ssh(command, protocol=CommandProtocol.RETRY_3X, timeout=300)
        if not result.success:
            raise RuntimeError(f"rclone setup failed: {result.stderr or result.stdout}")

    async def _rclone_copyto(self, source: str, dest: str) -> None:
        env_exports = self._remote_env_exports()
        prefix = f"{env_exports} && " if env_exports else ""
        command = prefix + f"rclone copyto {shlex.quote(source)} {shlex.quote(dest)} --immutable=false --checkers 4 --transfers 1"
        result = await self._ssh(command, protocol=CommandProtocol.RETRY_3X, timeout=600)
        if not result.success:
            raise RuntimeError(f"rclone copyto failed for {source}: {result.stderr or result.stdout}")

    async def _rclone_verify_sha256(self, remote_path: str, expected_sha256: str) -> bool:
        """Verify remote file SHA256 matches expected hash (W5-2).

        Returns True if hashes match, False on mismatch or error.
        """
        if not expected_sha256:
            return True  # no hash to verify
        env_exports = self._remote_env_exports()
        prefix = f"{env_exports} && " if env_exports else ""
        command = prefix + f"rclone hashsum SHA-256 {shlex.quote(remote_path)}"
        result = await self._ssh(command, protocol=CommandProtocol.FAIL_FAST, timeout=120)
        combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if not result.success:
            if _rclone_hashsum_unsupported(combined_output):
                fallback_hash = await self._rclone_verify_sha256_via_cat(remote_path)
                if not fallback_hash:
                    return False
                if fallback_hash.lower() != expected_sha256.lower():
                    logger.error(
                        "SHA256 MISMATCH for %s via streamed fallback: expected=%s got=%s",
                        remote_path,
                        expected_sha256,
                        fallback_hash,
                    )
                    return False
                logger.debug("SHA256 verified for %s via streamed fallback: %s", remote_path, fallback_hash)
                return True
            logger.warning("SHA256 verification failed for %s: %s", remote_path, combined_output)
            return False
        # rclone hashsum output format: "<hash>  <filename>"
        remote_hash = _extract_sha256_hexdigest(result.stdout)
        if not remote_hash:
            logger.warning("SHA256 verification produced no digest for %s: %s", remote_path, combined_output)
            return False
        if remote_hash.lower() != expected_sha256.lower():
            logger.error(
                "SHA256 MISMATCH for %s: expected=%s got=%s",
                remote_path, expected_sha256, remote_hash,
            )
            return False
        logger.debug("SHA256 verified for %s: %s", remote_path, remote_hash)
        return True

    async def _rclone_verify_sha256_via_cat(self, remote_path: str) -> str:
        env_exports = self._remote_env_exports()
        prefix = f"{env_exports} && " if env_exports else ""
        command = prefix + (
            'tmpfile=$(mktemp /tmp/mica_rclone_sha256.XXXXXX); '
            f'if rclone cat {shlex.quote(remote_path)} > "$tmpfile"; then '
            'sha256sum "$tmpfile" | awk \'{print $1}\'; '
            'status=$?; '
            'else status=$?; '
            'fi; '
            'rm -f "$tmpfile"; '
            'exit $status'
        )
        result = await self._ssh(command, protocol=CommandProtocol.FAIL_FAST, timeout=120)
        combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if not result.success:
            logger.warning(
                "SHA256 streamed fallback failed for %s: %s",
                remote_path,
                combined_output,
            )
            return ""
        remote_hash = _extract_sha256_hexdigest(result.stdout)
        if not remote_hash:
            logger.warning(
                "SHA256 streamed fallback produced no digest for %s: %s",
                remote_path,
                combined_output,
            )
            return ""
        return remote_hash

    async def _rclone_restore(self, source: str, dest: str) -> None:
        env_exports = self._remote_env_exports()
        prefix = f"{env_exports} && " if env_exports else ""
        command = prefix + f"rclone copyto {shlex.quote(source)} {shlex.quote(dest)} --checkers 4 --transfers 1"
        result = await self._ssh(command, protocol=CommandProtocol.RETRY_3X, timeout=600)
        if not result.success:
            raise RuntimeError(f"rclone restore failed for {source}: {result.stderr or result.stdout}")

    def _build_economic_ledger(self) -> Dict[str, Any]:
        """Build a structured economic record for GCS persistence (W0-3)."""
        elapsed_hours = 0.0
        if self.state.started_at:
            elapsed = _utcnow_dt() - self.state.started_at
            elapsed_hours = elapsed.total_seconds() / 3600
        return {
            "schema_version": "1.0",
            "job_id": self.state.job_id,
            "provider": self.provider.PROVIDER_NAME if hasattr(self.provider, "PROVIDER_NAME") else "vast",
            "instance_id": self.state.instance_id,
            "gpu_type": self.cfg.gpu_type.value,
            "gpu_count": self.cfg.n_gpus,
            "price_per_hour_usd": self.cfg.max_price_per_hour,
            "total_cost_usd": self.state.total_cost_usd,
            "elapsed_hours": round(elapsed_hours, 4),
            "max_budget_usd": self.cfg.max_total_cost_usd,
            "simulation_mode": self.cfg.simulation_mode.value,
            "phase": self.state.phase.value,
            "started_at": self.state.started_at.isoformat() if self.state.started_at else None,
            "snapshot_at": _utcnow_iso(),
            "docker_image": self.cfg.docker_image,
            "n_replicas": self.cfg.n_replicas,
            "execution_class": self.cfg.execution_class,
        }

    async def _persist_economic_ledger(self) -> None:
        """Write economic ledger JSON locally + to pod for GCS sync (W0-3)."""
        ledger = self._build_economic_ledger()
        os.makedirs(self.cfg.local_output_dir, exist_ok=True)
        local_path = os.path.join(self.cfg.local_output_dir, "economic_ledger.json")
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, default=str)
        # Upload to pod for rclone sync
        if self.state.run_dir:
            remote_path = f"{self._artifact_root()}/manifest/economic_ledger.json"
            await self._ssh(f"mkdir -p {self._artifact_root()}/manifest", timeout=15)
            await self._scp_up(local_path, remote_path, timeout=60)

    async def _sync_storage_artifacts(self, reason: str) -> None:
        if not self._storage_enabled() or not self.state.run_dir:
            return
        async with self._manifest_storage_lock:
            await self._ensure_rclone_available()
            await self._persist_runtime_manifests_unlocked()

            # W0-3: Persist economic ledger to GCS alongside artifacts
            await self._persist_economic_ledger()

            root = self._storage_remote_root()
            uploaded_storage_paths: set[str] = set()

            # W5-2: Track SHA256 of critical manifests for post-upload verification
            manifest_hashes: Dict[str, str] = {}
            job_manifest_local = self._job_manifest_remote_path()
            resume_spec_local = self._resume_spec_remote_path()

            # Compute local hashes before upload
            for name, path in [("job_manifest.json", job_manifest_local), ("resume_spec.json", resume_spec_local)]:
                exists_cmd = f"test -s {path} && echo YES || echo NO"
                exists_result = await self._ssh(exists_cmd, timeout=15)
                if "YES" in exists_result.stdout:
                    sha_cmd = f"sha256sum {path} | awk '{{print $1}}'"
                    sha_result = await self._ssh(sha_cmd, timeout=30)
                    if sha_result.success and sha_result.stdout.strip():
                        manifest_hashes[name] = sha_result.stdout.strip()

            await self._rclone_copyto(job_manifest_local, f"{root}/manifest/job_manifest.json")
            await self._rclone_copyto(resume_spec_local, f"{root}/manifest/resume_spec.json")

            # W5-2: Verify critical manifests after upload
            for name, expected_hash in manifest_hashes.items():
                remote_dest = f"{root}/manifest/{name}"
                verified = await self._rclone_verify_sha256(remote_dest, expected_hash)
                if not verified:
                    logger.error("POST-UPLOAD SHA256 MISMATCH for %s — retrying once", name)
                    # Retry upload + verification once
                    local_path = job_manifest_local if name == "job_manifest.json" else resume_spec_local
                    await self._rclone_copyto(local_path, remote_dest)
                    verified = await self._rclone_verify_sha256(remote_dest, expected_hash)
                    if not verified:
                        self._emit("storage_error", f"SHA256 verification failed after retry for {name}")

            # Store hashes for durability gate
            self._manifest_upload_hashes = manifest_hashes

            # W0-3: Sync economic ledger to GCS
            ledger_remote = f"{self._artifact_root()}/manifest/economic_ledger.json"
            exists = await self._ssh(f"test -s {ledger_remote} && echo YES || echo NO", timeout=15)
            if "YES" in exists.stdout:
                await self._rclone_copyto(ledger_remote, f"{root}/manifest/economic_ledger.json")
            pdb_storage_dest = self._storage_destination_for_artifact("inputs", self._effective_remote_pdb_name())
            await self._rclone_copyto(
                f"{self._artifact_root()}/{self._effective_remote_pdb_name()}",
                pdb_storage_dest,
            )
            uploaded_storage_paths.add(pdb_storage_dest)
            if self._uses_protein_ligand_complex_contract():
                remote_docked_pose = self._remote_docked_pose_path(self.state.run_dir)
                docked_name = self._effective_remote_docked_pose_name()
                exists = await self._ssh(f"test -s {remote_docked_pose} && echo YES || echo NO", timeout=15)
                if "YES" in exists.stdout:
                    docked_storage_dest = self._storage_destination_for_artifact("inputs", docked_name)
                    await self._rclone_copyto(
                        remote_docked_pose,
                        docked_storage_dest,
                    )
                    uploaded_storage_paths.add(docked_storage_dest)
            for replica_id in range(1, self.cfg.n_replicas + 1):
                storage_copy_candidates: list[tuple[str, str]] = [
                    (
                        self._replica_checkpoint_remote_path(replica_id),
                        self._storage_destination_for_artifact(
                            "outputs",
                            self._replica_checkpoint_filename(replica_id),
                            replica_id,
                        ),
                    ),
                    (
                        self._replica_prepared_pdb_remote_path(replica_id),
                        self._storage_destination_for_artifact(
                            "outputs",
                            self._replica_prepared_pdb_filename(replica_id),
                            replica_id,
                        ),
                    ),
                    (
                        self._replica_log_remote_path(replica_id),
                        self._storage_destination_for_artifact(
                            "logs",
                            f"replica_{replica_id}.log",
                            replica_id,
                        ),
                    ),
                ]
                seen_destinations = {dest for _, dest in storage_copy_candidates}
                for fname in self._get_download_manifest(replica_id):
                    dest = self._storage_destination_for_artifact("outputs", fname, replica_id)
                    if dest in seen_destinations:
                        continue
                    storage_copy_candidates.append((f"{self._replica_dir(replica_id)}/{fname}", dest))
                    seen_destinations.add(dest)

                for remote_path, dest in storage_copy_candidates:
                    exists = await self._ssh(f"test -e {remote_path} && echo YES || echo NO", timeout=15)
                    if "YES" in exists.stdout:
                        await self._rclone_copyto(remote_path, dest)
                        uploaded_storage_paths.add(dest)

            synced_at = _utcnow_iso()
            manifest_updated = await self._persist_synced_artifact_manifest(uploaded_storage_paths, synced_at)
            if manifest_updated:
                await self._rclone_copyto(job_manifest_local, f"{root}/manifest/job_manifest.json")
                updated_manifest_sha = self._compute_sha256(self._job_manifest_local_path())
                self._manifest_upload_hashes["job_manifest.json"] = updated_manifest_sha
                verified = await self._rclone_verify_sha256(f"{root}/manifest/job_manifest.json", updated_manifest_sha)
                if not verified:
                    logger.error("POST-UPLOAD SHA256 MISMATCH for synced job_manifest.json — retrying once")
                    await self._rclone_copyto(job_manifest_local, f"{root}/manifest/job_manifest.json")
                    verified = await self._rclone_verify_sha256(f"{root}/manifest/job_manifest.json", updated_manifest_sha)
                    if not verified:
                        self._emit("storage_error", "SHA256 verification failed after retry for synced job_manifest.json")

            self._last_storage_sync_at = time.monotonic()
            self._emit("storage", f"Synced critical artifacts to storage ({reason})")

    async def _verify_storage_durability(self) -> bool:
        """Pre-destroy durability gate (Invariant #1).

        Checks file existence AND SHA256 integrity for critical manifests (W5-2).
        """
        if not self._storage_enabled():
            return True
        async with self._manifest_storage_lock:
            root = self._storage_remote_root()
            env_exports = self._remote_env_exports()
            prefix = f"{env_exports} && " if env_exports else ""
            command = prefix + f"rclone lsf {shlex.quote(root + '/manifest')} | grep -E 'job_manifest\\.json|resume_spec\\.json' || true"
            result = await self._ssh(command, protocol=CommandProtocol.FAIL_FAST, timeout=60)
            if not ("job_manifest.json" in result.stdout and "resume_spec.json" in result.stdout):
                return False

            # W5-2: SHA256 integrity check for critical manifests
            manifest_hashes = getattr(self, "_manifest_upload_hashes", {})
            if manifest_hashes:
                for name, expected_sha256 in manifest_hashes.items():
                    remote_path = f"{root}/manifest/{name}"
                    if not await self._rclone_verify_sha256(remote_path, expected_sha256):
                        logger.error("Durability check FAILED: SHA256 mismatch for %s", name)
                        return False
                logger.info("Durability check PASSED with SHA256 verification for %d manifest(s)", len(manifest_hashes))
            return True

    async def _restore_resume_artifacts_from_storage(self) -> None:
        if not self.cfg.resume_spec or not self._storage_enabled():
            return

        main_restore = self._effective_remote_pdb_restore_source()
        if main_restore:
            await self._rclone_restore(main_restore, f"{self._artifact_root()}/{self._effective_remote_pdb_name()}")

        for replica in self.cfg.resume_spec.replicas:
            if replica.checkpoint_storage_path:
                await self._rclone_restore(
                    replica.checkpoint_storage_path,
                    self._replica_checkpoint_remote_path(replica.replica_id),
                )
            if replica.prepared_pdb_storage_path:
                await self._rclone_restore(
                    replica.prepared_pdb_storage_path,
                    self._replica_prepared_pdb_remote_path(replica.replica_id),
                )

    async def _perform_safe_stop(self) -> None:
        self._emit("stop", f"Safe stop requested ({self.state.stop_reason or 'user_request'})")
        for replica_id, replica in self.state.replicas.items():
            if replica.pid:
                await self._ssh(f"kill -TERM {replica.pid} 2>/dev/null || true", timeout=10)
        await self._ssh(self._safe_remote_kill_command(self._get_sim_grep_pattern(), signal="TERM"), timeout=10)
        await self._ssh(self._safe_remote_kill_command("extract_latest_pdb_every_10min", signal="TERM"), timeout=10)
        await asyncio.sleep(5)
        if self._storage_enabled():
            await self._sync_storage_artifacts("safe_stop")
        await self._persist_runtime_manifests()
        for replica in self.state.replicas.values():
            if replica.status not in (SimStatus.COMPLETE, SimStatus.FAILED):
                replica.status = SimStatus.STOPPED
                replica.eta_hours = 0.0
        self.state.safe_stop_completed = True

    # ── preflight ────────────────────────────────────────────────

    def _preflight_validate(self) -> None:
        """Validate all prerequisites before spending money on a pod.

        Raises ``RuntimeError`` with a clear message enumerating every
        missing prerequisite so the user can fix them all at once.
        """
        import shutil
        errors: list[str] = []

        # 1. VAST_API_KEY env var
        if not os.environ.get("VAST_API_KEY"):
            errors.append(
                "VAST_API_KEY environment variable is not set. "
                "Export it from https://cloud.vast.ai/account/"
            )

        # 2. vastai CLI on PATH
        if not shutil.which("vastai"):
            errors.append(
                "'vastai' CLI not found on PATH. Install with: pip install vastai"
            )
        else:
            try:
                auth_check = subprocess.run(
                    ["vastai", "show", "user", "--raw"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                    env=os.environ.copy(),
                )
                auth_output = "\n".join(
                    part for part in [auth_check.stdout, auth_check.stderr] if part
                ).lower()
                if auth_check.returncode != 0 or "invalid user key" in auth_output:
                    errors.append(
                        "Vast.ai CLI authentication failed. Export a valid `VAST_API_KEY` or run `vastai set api-key` before launching remote MD."
                    )
            except Exception as exc:
                errors.append(f"Unable to validate Vast.ai CLI authentication: {exc}")

        # 3. SSH key
        ssh_key = Path(self.cfg.ssh_key_path).expanduser()
        if not ssh_key.is_file():
            errors.append(
                f"SSH key not found at {ssh_key}. "
                f"Generate with: ssh-keygen -t ed25519 -f {ssh_key} -N ''"
            )

        # 4. PDB file (or resume-capable restore source)
        pdb_source = self._effective_local_pdb_source()
        if self.cfg.pdb_path and not pdb_source and not self._effective_remote_pdb_restore_source():
            errors.append(
                f"PDB file not found: {self.cfg.pdb_path}"
            )

        if self._uses_protein_ligand_complex_contract():
            if not self.cfg.ligand_smiles.strip():
                errors.append("Ligand-aware complex_stability requires ligand_smiles")
            docked_pose = Path(self.cfg.docked_ligand_pdb).expanduser() if self.cfg.docked_ligand_pdb else None
            if docked_pose is None or not docked_pose.is_file():
                errors.append(
                    "Ligand-aware complex_stability requires a local docked_ligand_pdb file: "
                    f"{self.cfg.docked_ligand_pdb or '<missing>'}"
                )

        # 5. Simulation script
        if not self.cfg.simulation_script:
            errors.append(
                f"No simulation script found ({self.cfg._expected_simulation_script_name_for_current_config()}). "
                "Check workers/dynamo/biodynamo/processors/ directory."
            )
        elif not Path(self.cfg.simulation_script).is_file():
            errors.append(
                f"Simulation script path invalid: {self.cfg.simulation_script}"
            )
        elif self.cfg.simulation_script_sha256:
            actual_simulation_sha256 = self._compute_sha256(self.cfg.simulation_script)
            if actual_simulation_sha256.lower() != self.cfg.simulation_script_sha256.lower():
                errors.append(
                    "Simulation script integrity mismatch: "
                    f"expected {self.cfg.simulation_script_sha256} got {actual_simulation_sha256}"
                )

        if not self.cfg.extractor_script:
            errors.append(
                "No extractor script found (extract_latest_pdb_every_10min.py). "
                "Check src/mica/infrastructure/orchestration/ directory."
            )
        elif not Path(self.cfg.extractor_script).is_file():
            errors.append(
                f"Extractor script path invalid: {self.cfg.extractor_script}"
            )
        elif self.cfg.extractor_script_sha256:
            actual_extractor_sha256 = self._compute_sha256(self.cfg.extractor_script)
            if actual_extractor_sha256.lower() != self.cfg.extractor_script_sha256.lower():
                errors.append(
                    "Extractor script integrity mismatch: "
                    f"expected {self.cfg.extractor_script_sha256} got {actual_extractor_sha256}"
                )

        if errors:
            banner = "Vast.ai MD Orchestrator — Preflight Failed:\n" + "\n".join(
                f"  [{i+1}] {e}" for i, e in enumerate(errors)
            )
            logger.error(banner)
            raise RuntimeError(banner)

        logger.info(
            "Preflight OK: API key ✓, CLI ✓, SSH key ✓, PDB ✓, scripts ✓"
        )

    # ── public API ──────────────────────────────────────────────

    async def run(self) -> OrchestratorState:
        """
        Execute the full autonomous pipeline.
        
        Returns OrchestratorState with final phase (COMPLETE or FAILED).
        """
        # ── Preflight checks ────────────────────────────────────
        self._preflight_validate()

        self.state.started_at = _utcnow_dt()
        self.state.local_output_dir = self.cfg.local_output_dir
        self._emit("init", f"Starting job {self.cfg.job_id}")

        try:
            await self._fase_0_provision(post_ready_callback=self._fase_1_probe)
            await self._fase_2_install_openmm()
            await self._fase_3_stage_forcefield()
            await self._fase_4_verify_forcefield()
            await self._fase_5_upload_files()
            await self._fase_6_launch()
            await self._fase_7_monitor()
            await self._fase_8_download()
            self.state.scientific_completion_achieved = True
            if self._storage_enabled():
                await self._sync_storage_artifacts("post_download")
            # W1-2: Auto-trigger SMIC analysis bundle after download
            if getattr(self.cfg, 'auto_smic_analyses', None):
                await self._fase_8b_auto_smic()
            if not (self._stop_requested and self.cfg.preserve_instance_on_stop):
                destroyed = await self._destroy()
                if not destroyed:
                    teardown_reason = (
                        self.state.teardown_failure_reason
                        or f"Failed to destroy {self.state.instance_id or 'unknown'} after bounded retry"
                    )
                    self.state.phase = OrchestratorPhase.FAILED_RECOVERABLE
                    self.state.error = (
                        f"Provider teardown was not confirmed for instance "
                        f"{self.state.instance_id or 'unknown'}: {teardown_reason}"
                    )
                    self.state.completed_at = _utcnow_dt()
                    await self._persist_runtime_manifests()
                    self._emit(
                        "error",
                        "Scientific execution completed, but provider teardown was not confirmed; marking failed_recoverable",
                    )
                    return self.state
            else:
                self._emit("stop", "Preserving instance after safe stop (preserve_instance_on_stop=True)")

            self.state.phase = OrchestratorPhase.COMPLETE
            self.state.completed_at = _utcnow_dt()
            await self._persist_runtime_manifests()
            elapsed_h = (
                self.state.completed_at - self.state.started_at
            ).total_seconds() / 3600
            self._emit(
                "complete",
                f"Job finished in {elapsed_h:.2f}h. Cost: ${self.state.total_cost_usd:.4f}",
            )

        except Exception as exc:
            if self.state.phase not in {OrchestratorPhase.STALLED_BOOTSTRAP, OrchestratorPhase.FAILED_RECOVERABLE}:
                self.state.phase = OrchestratorPhase.FAILED
            self.state.error = str(exc)
            self._emit("error", f"Pipeline failed: {exc}")
            logger.exception("Orchestrator pipeline failed")
            try:
                await self._persist_runtime_manifests()
            except Exception:
                logger.warning("Failed to persist manifests during failure handling")
            if self.cfg.preserve_instance_on_failure:
                self._emit(
                    "error",
                    "Preserving instance for recovery (preserve_instance_on_failure=True)",
                )
            else:
                try:
                    await self._destroy()
                except Exception:
                    logger.warning("Failed to destroy instance after error")

        return self.state

    def request_stop(self, reason: str = "user_request"):
        """Signal the orchestrator to enter safe-stop handling."""
        self._stop_requested = True
        self.state.stop_reason = reason

    # ── FASE 0: Provision ────────────────────────────────────────

    async def _fase_0_provision(self, post_ready_callback=None):
        self.state.phase = OrchestratorPhase.PROVISION
        self._emit("provision", f"Searching {self.cfg.gpu_type.value} offers ≤${self.cfg.max_price_per_hour}/hr")
        required_disk_gb = self._estimate_required_disk_gb()
        machine_attempts_total = max(1, int(self.cfg.readiness_retry_attempts) + 1)
        retry_backoff_sec = max(0, int(self.cfg.readiness_retry_backoff_sec))
        attempted_offer_keys: set[str] = set()
        self._emit(
            "provision",
            f"Remote disk requirement ≈ {required_disk_gb:.0f} GB with reliability floor {self.cfg.min_reliability:.2f}",
            machine_attempts_total=machine_attempts_total,
        )

        def _offer_retry_key(offer: GPUOffer) -> str:
            raw = offer.raw_data or {}
            host_id = raw.get("host_id") or raw.get("machine_id") or raw.get("machine") or ""
            if host_id:
                return f"host:{host_id}"
            return f"offer:{offer.offer_id}"

        def _format_price(price: float) -> str:
            normalized = f"{float(price):.2f}".rstrip("0").rstrip(".")
            if "." not in normalized:
                normalized += ".0"
            return normalized

        def _remaining_budget_usd() -> float:
            consumed = max(0.0, float(self.state.total_cost_usd or 0.0))
            return max(0.0, float(self.cfg.max_total_cost_usd) - consumed)

        def _max_price_for_machine_attempt(machine_attempt: int) -> float:
            base_max_price = max(0.0, float(self.cfg.max_price_per_hour))
            if machine_attempt <= 1:
                return base_max_price

            runtime_hours = float(self.cfg.max_runtime_hours or 0.0)
            if runtime_hours <= 0.0:
                return base_max_price

            budget_limited_max_price = _remaining_budget_usd() / runtime_hours
            return max(base_max_price, budget_limited_max_price)

        def _no_offers_message(max_price: float) -> str:
            return (
                f"No {self.cfg.gpu_type.value} offers found "
                f"under ${_format_price(max_price)}/hr with reliability >= {self.cfg.min_reliability:.2f} "
                f"and disk >= {required_disk_gb:.0f} GB"
            )

        def _reset_readiness_state() -> None:
            if self._tunnel_proc is not None:
                try:
                    self._tunnel_proc.kill()
                except Exception:
                    pass
                self._tunnel_proc = None
            self.state.ssh_host = ""
            self.state.ssh_port = 22
            self.state.conda_python = ""
            self.state.run_dir = ""
            self.state.pod_api_url = ""
            self.state.pod_api_available = False
            self.state.pod_api_external_port = _POD_API_PORT

        async def _run_post_ready_callback(*, machine_attempt: int) -> None:
            if post_ready_callback is None:
                return
            try:
                await post_ready_callback()
            except Exception as exc:
                self._emit(
                    "provision",
                    f"Readiness validation failed after machine attempt {machine_attempt}/{machine_attempts_total}: {exc}",
                    machine_attempt=machine_attempt,
                    instance_id=self.state.instance_id,
                    non_recovery_reason=str(exc),
                )
                raise

        async def _teardown_failed_attempt(instance_id: Optional[str], *, machine_attempt: int, reason: str):
            if not instance_id:
                return
            if self._tunnel_proc is not None:
                try:
                    self._tunnel_proc.kill()
                except Exception:
                    pass
                self._tunnel_proc = None
            if not self.cfg.destroy_on_readiness_non_recovery:
                self._emit(
                    "provision",
                    "Preserving failed instance after non-recovery because destroy_on_readiness_non_recovery=False",
                    machine_attempt=machine_attempt,
                    instance_id=instance_id,
                    non_recovery_reason=reason,
                )
                return
            self._emit(
                "provision",
                f"Destroying non-recovered instance {instance_id} after machine attempt {machine_attempt}/{machine_attempts_total}",
                machine_attempt=machine_attempt,
                instance_id=instance_id,
                non_recovery_reason=reason,
            )
            try:
                destroyed = await self._destroy_with_timeout_and_retry(
                    instance_id, attempt_num=machine_attempt, attempt_total=machine_attempts_total
                )
                self._emit(
                    "provision",
                    f"Teardown after non-recovery completed (destroyed={bool(destroyed)}) "
                    f"after machine attempt {machine_attempt}/{machine_attempts_total}",
                    machine_attempt=machine_attempt,
                    instance_id=instance_id,
                    destroyed=bool(destroyed),
                )
            except Exception as destroy_exc:
                self._emit(
                    "provision",
                    f"Teardown after non-recovery failed: {destroy_exc}",
                    machine_attempt=machine_attempt,
                    instance_id=instance_id,
                    destroy_error=str(destroy_exc),
                )

        # ── W1-3 + W4: CloudOrchestrator cascade path with GPU intelligence ──
        if self.cloud_orchestrator is not None and self.cfg.gpu_fallback_cascade:
            from ..providers.base_provider import ProvisionRequest
            from ..gpu_scorer import GPUScorer

            scorer = GPUScorer()
            cascade = scorer.get_cascade(self.cfg.gpu_type)
            estimated_atoms = getattr(self.cfg, "estimated_atom_count", 0) or 0
            self._emit(
                "provision",
                f"Using GPUScorer cascade for {self.cfg.gpu_type.value}: "
                f"{[g.value for g in cascade]} (atoms≈{estimated_atoms})",
            )

            # Inject scorer into CloudOrchestrator for DCEM-based offer ranking
            if estimated_atoms > 0:
                self.cloud_orchestrator.set_scorer(scorer.as_scorer(estimated_atoms))

            last_exc: Optional[Exception] = None
            for machine_attempt in range(1, machine_attempts_total + 1):
                self.state.phase = OrchestratorPhase.PROVISION
                self._emit(
                    "provision",
                    f"Machine attempt {machine_attempt}/{machine_attempts_total} (cascade)",
                    machine_attempt=machine_attempt,
                    machine_attempts_total=machine_attempts_total,
                )
                try:
                    bootstrap_command = None if self._uses_custom_image else "sleep infinity"
                    provision_req = ProvisionRequest(
                        gpu_type=self.cfg.gpu_type,          # overridden per step
                        gpu_count=self.cfg.n_gpus,
                        docker_image=self.cfg.docker_image,
                        docker_command=bootstrap_command,
                        max_price_per_hour=self.cfg.max_price_per_hour,
                        job_id=self.cfg.job_id,
                    )
                    result = await self.cloud_orchestrator.provision_with_cascade(
                        request=provision_req,
                        gpu_cascade=cascade,
                        scorer=scorer,
                        atom_count=estimated_atoms,
                    )
                    if not result.success:
                        raise RuntimeError(f"GPU cascade provisioning failed: {result.error_message}")

                    self.state.instance_id = result.instance.instance_id
                    winning_provider = self.cloud_orchestrator.providers.get(result.instance.provider)
                    if winning_provider is not None:
                        self.provider = winning_provider

                    self._emit(
                        "provision",
                        f"Instance {self.state.instance_id} created via cascade. Waiting for RUNNING...",
                        machine_attempt=machine_attempt,
                    )
                    instance = await self.provider.wait_for_ready(
                        self.state.instance_id,
                        timeout_seconds=self.cfg.provision_timeout_sec,
                        poll_interval=self.cfg.provision_poll_interval_sec,
                    )
                    self.state.ssh_host = instance.ssh_host
                    self.state.ssh_port = instance.ssh_port
                    self._emit(
                        "provision",
                        f"Pod RUNNING at {self.state.ssh_host}:{self.state.ssh_port}",
                        machine_attempt=machine_attempt,
                    )
                    await _run_post_ready_callback(machine_attempt=machine_attempt)
                    return
                except Exception as exc:
                    last_exc = exc
                    await _teardown_failed_attempt(
                        self.state.instance_id,
                        machine_attempt=machine_attempt,
                        reason=str(exc),
                    )
                    self.state.instance_id = None
                    _reset_readiness_state()
                    if machine_attempt < machine_attempts_total and retry_backoff_sec > 0:
                        self._emit(
                            "provision",
                            f"Provision retry backoff {retry_backoff_sec}s before next machine attempt",
                            machine_attempt=machine_attempt,
                        )
                        await asyncio.sleep(retry_backoff_sec)
            raise RuntimeError(
                f"GPU cascade provisioning failed after {machine_attempts_total} machine attempts: {last_exc}"
            )

        # ── Legacy direct-provider path ──────────────────────────
        last_exc: Optional[Exception] = None
        for machine_attempt in range(1, machine_attempts_total + 1):
            self.state.phase = OrchestratorPhase.PROVISION
            self._emit(
                "provision",
                f"Machine attempt {machine_attempt}/{machine_attempts_total}",
                machine_attempt=machine_attempt,
                machine_attempts_total=machine_attempts_total,
            )
            try:
                max_price_for_attempt = _max_price_for_machine_attempt(machine_attempt)
                if machine_attempt > 1 and max_price_for_attempt > float(self.cfg.max_price_per_hour):
                    self._emit(
                        "provision",
                        "Retry widening offer ceiling from "
                        f"${_format_price(self.cfg.max_price_per_hour)}/hr to "
                        f"${_format_price(max_price_for_attempt)}/hr within remaining budget "
                        f"${_format_price(_remaining_budget_usd())}",
                        machine_attempt=machine_attempt,
                        retry_price_ceiling=max_price_for_attempt,
                    )
                offers = await self.provider.search_offers(
                    gpu_type=self.cfg.gpu_type,
                    max_price=max_price_for_attempt,
                    min_gpu_count=self.cfg.n_gpus,
                    min_reliability=self.cfg.min_reliability,
                    min_disk_gb=required_disk_gb,
                )
                if not offers:
                    raise RuntimeError(_no_offers_message(max_price_for_attempt))

                cheapest = next(
                    (offer for offer in offers if _offer_retry_key(offer) not in attempted_offer_keys),
                    offers[0],
                )
                attempted_offer_keys.add(_offer_retry_key(cheapest))
                self._emit(
                    "provision",
                    f"Selected offer {cheapest.offer_id}: "
                    f"{cheapest.gpu_count}x GPU @ ${cheapest.price_per_hour:.4f}/hr "
                    f"({cheapest.region}) | disk={cheapest.disk_gb} GB",
                    machine_attempt=machine_attempt,
                )

                result = await self.provider.create_instance(
                    offer=cheapest,
                    docker_image=self.cfg.docker_image,
                    docker_command=None if self._uses_custom_image else "sleep infinity",
                    job_id=self.cfg.job_id,
                    disk_gb=required_disk_gb,
                )
                if not result.success:
                    raise RuntimeError(f"Failed to create instance: {result.error_message}")

                self.state.instance_id = result.instance.instance_id
                self._emit(
                    "provision",
                    f"Instance {self.state.instance_id} created. Waiting for RUNNING...",
                    machine_attempt=machine_attempt,
                )
                instance = await self.provider.wait_for_ready(
                    self.state.instance_id,
                    timeout_seconds=self.cfg.provision_timeout_sec,
                    poll_interval=self.cfg.provision_poll_interval_sec,
                )
                self.state.ssh_host = instance.ssh_host
                self.state.ssh_port = instance.ssh_port
                # Resolve Vast port mapping for Pod API (8787/tcp -> external port)
                _raw_ports = instance.raw_data.get("ports") or {}
                _port_key = f"{_POD_API_PORT}/tcp"
                _port_entries = _raw_ports.get(_port_key) or []
                if _port_entries:
                    _first = _port_entries[0]
                    _ext = (_first.get("HostPort") if isinstance(_first, dict) else None) or str(_first)
                    try:
                        self.state.pod_api_external_port = int(_ext)
                    except (ValueError, TypeError):
                        self.state.pod_api_external_port = _POD_API_PORT
                else:
                    self.state.pod_api_external_port = _POD_API_PORT
                self._emit(
                    "provision",
                    f"Pod RUNNING at {self.state.ssh_host}:{self.state.ssh_port} | Pod API external port: {self.state.pod_api_external_port}",
                    machine_attempt=machine_attempt,
                )
                await _run_post_ready_callback(machine_attempt=machine_attempt)
                return
            except Exception as exc:
                last_exc = exc
                await _teardown_failed_attempt(
                    self.state.instance_id,
                    machine_attempt=machine_attempt,
                    reason=str(exc),
                )
                self.state.instance_id = None
                _reset_readiness_state()
                if machine_attempt < machine_attempts_total and retry_backoff_sec > 0:
                    self._emit(
                        "provision",
                        f"Provision retry backoff {retry_backoff_sec}s before next machine attempt",
                        machine_attempt=machine_attempt,
                    )
                    await asyncio.sleep(retry_backoff_sec)

        raise RuntimeError(
            f"Provision failed after {machine_attempts_total} machine attempts: {last_exc}"
        )

    # ── FASE 1: Probe ────────────────────────────────────────────

    async def _fase_1_probe(self):
        self.state.phase = OrchestratorPhase.PROBE

        # SSH health check
        for attempt in range(self.cfg.ssh_probe_attempts):
            healthy = await self.ssh.health_check(
                self.state.ssh_host,
                self.state.ssh_port,
                timeout=12,
                key_path=self.cfg.ssh_key_path,
            )
            if healthy:
                break
            self._emit(
                "probe",
                f"SSH attempt {attempt+1}/{self.cfg.ssh_probe_attempts} — waiting {self.cfg.ssh_probe_sleep_sec} s",
            )
            await asyncio.sleep(self.cfg.ssh_probe_sleep_sec)
        else:
            raise RuntimeError(f"SSH connection failed after {self.cfg.ssh_probe_attempts} attempts")

        self._emit("probe", "SSH connected")

        # Kill old simulation processes (R8)
        await self._ssh(self._safe_remote_kill_command(self._get_sim_grep_pattern()))

        # GPU info
        gpu_result = await self._ssh(
            "nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader",
            timeout=15,
        )
        self._emit("probe", f"GPUs: {gpu_result.stdout.strip()}")

        # Detect conda path (R6)
        conda_detect = await self._ssh(
            'for P in /opt/miniforge3/bin/python /root/miniconda3/bin/python '
            '/opt/conda/bin/python /usr/bin/python3; do '
            '[ -f "$P" ] && echo "FOUND:$P" && exit 0; done; echo "NONE"',
            timeout=15,
        )
        output = conda_detect.stdout.strip()
        if "FOUND:" in output:
            self.state.conda_python = output.split("FOUND:")[1].strip().split("\n")[0]
            self._emit("probe", f"Python: {self.state.conda_python}")
        else:
            # Fallback: install miniconda
            self._emit("probe", "No Python found — installing miniconda")
            await self._install_miniconda()

        self.state.run_dir = f"/workspace/{self.cfg.run_dir_name}"
        if self._storage_enabled() and self.cfg.install_rclone:
            await self._ensure_rclone_available()

        # Custom image still runs under Vast SSH runtime, which does not guarantee
        # the image CMD is active. Start Pod API explicitly, then fail fast if
        # health probes never become ready.
        if self._uses_custom_image:
            await self._start_pod_api()
            await self._probe_pod_api(
                retries=self.cfg.bootstrap_probe_attempts,
                delay=self.cfg.bootstrap_probe_sleep_sec,
                fail_fast=True,
            )

    # ── Pod API lifecycle helpers (W3-3) ─────────────────────────

    async def _open_pod_api_tunnel(self) -> int:
        """Open a local SSH tunnel to the Pod API port on the remote pod.

        Returns the local port bound (always _POD_API_PORT for simplicity).
        Kills any previous tunnel process first.
        """
        local_port = _POD_API_PORT  # bind same port locally for simplicity
        if self._tunnel_proc is not None:
            try:
                self._tunnel_proc.kill()
            except Exception:
                pass
            self._tunnel_proc = None

        ssh_key = self.cfg.ssh_key_path or os.path.expanduser("~/.ssh/vast_key")
        cmd = [
            "ssh",
            "-N",  # no remote command — tunnel only
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-i", str(ssh_key),
            "-p", str(self.state.ssh_port),
            "-L", f"{local_port}:127.0.0.1:{_POD_API_PORT}",
            f"root@{self.state.ssh_host}",
        ]
        self._tunnel_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Give the tunnel a moment to bind
        await asyncio.sleep(3)
        self._emit("probe", f"SSH tunnel opened: localhost:{local_port} → remote:{_POD_API_PORT}")
        return local_port

    async def _start_pod_api(self):
        """Start the Pod API (uvicorn) on the remote pod in background."""
        ext_port = self.state.pod_api_external_port
        self._emit("probe", f"Starting Pod API on port {_POD_API_PORT} (external: {ext_port})...")
        # Check if already running first
        check = await self._ssh(
            f"curl -sf http://127.0.0.1:{_POD_API_PORT}/pod/v1/health 2>/dev/null && echo POD_API_OK || echo POD_API_DOWN",
            protocol=CommandProtocol.FAIL_FAST,
            timeout=10,
        )
        if "POD_API_OK" in check.stdout:
            self._emit("probe", "Pod API already running")
        else:
            # Launch uvicorn in background; logs go to /workspace/pod_api.log
            start_cmd = (
                f"cd /app && nohup {self.state.conda_python or 'python3'} -m uvicorn "
                f"pod_api.main:app --host 0.0.0.0 --port {_POD_API_PORT} "
                f"> /workspace/pod_api.log 2>&1 &"
            )
            await self._ssh(start_cmd, protocol=CommandProtocol.FAIL_FAST, timeout=15)
            # Give uvicorn a moment to bind
            await asyncio.sleep(2)

        # Open SSH tunnel so we can reach the Pod API via localhost regardless
        # of whether the Vast instance has routable direct ports.
        local_port = await self._open_pod_api_tunnel()
        self.state.pod_api_url = f"http://127.0.0.1:{local_port}"

    async def _probe_pod_api(
        self,
        retries: int = 5,
        delay: float = 2.0,
        fail_fast: bool = False,
    ):
        """Probe the Pod API health endpoint and set state flags."""
        # Use the tunnel URL set by _start_pod_api, falling back to external port.
        url = self.state.pod_api_url or f"http://127.0.0.1:{_POD_API_PORT}"
        client = _PodAPIClient(url)
        for attempt in range(1, retries + 1):
            try:
                health = await client.health()
                # Pod API returns status: "ok" (not "healthy") — accept both.
                if health.get("status") in ("ok", "healthy"):
                    self.state.pod_api_url = url
                    self.state.pod_api_available = True
                    platforms = health.get("platforms", [])
                    self._emit(
                        "probe",
                        f"Pod API healthy — OpenMM {health.get('openmm_version','?')}, "
                        f"platforms={platforms}, rclone={'yes' if health.get('rclone_available') else 'no'}, "
                        f"cuda={'yes' if health.get('cuda_available') else 'no'}",
                    )
                    return
            except (httpx.HTTPError, httpx.ConnectError, Exception) as e:
                self._emit("probe", f"Pod API probe {attempt}/{retries} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(delay)

        message = (
            f"Pod API unreachable after {retries} bootstrap probes "
            f"({delay:.1f}s spacing)"
        )
        self._emit("probe", message)
        self.state.pod_api_available = False
        if fail_fast:
            self.state.phase = OrchestratorPhase.STALLED_BOOTSTRAP
            self.state.error = message
            raise RuntimeError(message)

    async def _register_pod_run(self):
        """Register the current job with the Pod API as a tracked run.

        This enables structured metrics collection and teardown proof
        generation for the simulation lifecycle.
        """
        if not self.state.pod_api_url:
            return
        client = _PodAPIClient(self.state.pod_api_url, timeout=30)
        run_id = f"md-{self.state.job_id or uuid.uuid4().hex[:12]}"
        try:
            payload = {
                "run_id": run_id,
                "workflow_kind": self.cfg.simulation_mode.value
                if hasattr(self.cfg.simulation_mode, "value")
                else "md",
                "pdb_path": self.cfg.pdb_path,
                "steps": self.cfg.steps,
                "production_ns": self._target_ns_for_job(),
                "execution_class": self.cfg.execution_class,
            }
            result = await client.create_run(payload)
            self.state._pod_run_id = result.get("run_id", run_id)  # type: ignore[attr-defined]
            self._emit("launch", f"Pod API run registered: {self.state._pod_run_id}")
        except Exception as e:
            self._emit("launch", f"Pod API run registration failed (non-fatal): {e}")

    async def _install_miniconda(self):
        """Install miniconda on a bare pod."""
        install_cmds = (
            "cd /tmp && "
            "curl -sSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh "
            "-o miniconda.sh && "
            "bash miniconda.sh -b -p /root/miniconda3 && "
            "rm miniconda.sh && "
            "/root/miniconda3/bin/conda init bash"
        )
        r = await self._ssh(install_cmds, protocol=CommandProtocol.RETRY_5X, timeout=300)
        if not r.success:
            raise RuntimeError(f"Miniconda install failed: {r.stderr}")
        self.state.conda_python = "/root/miniconda3/bin/python"
        self._emit("probe", "Miniconda installed")

    def _build_openmm_install_script(
        self,
        python_bin: str,
        preferred_installer: str,
        setup_log: str,
    ) -> str:
        """Build a deterministic remote bash installer for OpenMM + pdbfixer with CUDA.

        Strategy:
        1) Short-circuit if OpenMM+CUDA already available
        2) Try preferred installer + common conda/mamba binaries
        3) Install openmm + pdbfixer together; use cuda-version=12.4 (modern)
           first, then cudatoolkit=12.4 fallback, then fall-all-the-way-back
           to cuda-version=11.8 for older hosts
        4) Verify CUDA platform exists in OpenMM before success

        PDBFixer is co-installed because raw RCSB PDB files frequently have
        missing atoms / residues that prevent forcefield template matching.
        """
        return f"""#!/bin/bash
set +e

PY='{python_bin}'
SETUP_LOG='{setup_log}'
PREFERRED='{preferred_installer}'

mkdir -p "$(dirname "$SETUP_LOG")"
echo "[openmm-install] start $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$SETUP_LOG"

# 1) Already installed with CUDA?
$PY - <<'PY'
import sys
try:
    import openmm
    names = [openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())]
    if "CUDA" in names:
        print("OPENMM_CUDA_OK", openmm.__version__, names)
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
if [ $? -eq 0 ]; then
  echo "OPENMM_CUDA_OK_ALREADY" >> "$SETUP_LOG"
  exit 0
fi

INSTALLERS=(
  "$PREFERRED"
  "$(dirname "$PY")/mamba"
  "$(dirname "$PY")/conda"
  "/opt/miniforge3/bin/mamba"
  "/opt/miniforge3/bin/conda"
  "/root/miniconda3/bin/mamba"
  "/root/miniconda3/bin/conda"
  "/opt/conda/bin/mamba"
  "/opt/conda/bin/conda"
)

SUCCESS=0
for I in "${{INSTALLERS[@]}}"; do
  [ -x "$I" ] || continue
  echo "TRY_INSTALLER:$I" | tee -a "$SETUP_LOG"

  "$I" install -n base -c conda-forge openmm pdbfixer cuda-version=12.4 -y >> "$SETUP_LOG" 2>&1
  if [ $? -eq 0 ]; then
    SUCCESS=1
    echo "INSTALL_OK:cuda-version=12.4 via $I" | tee -a "$SETUP_LOG"
    break
  fi

  "$I" install -n base -c conda-forge openmm pdbfixer cudatoolkit=12.4 -y >> "$SETUP_LOG" 2>&1
  if [ $? -eq 0 ]; then
    SUCCESS=1
    echo "INSTALL_OK:cudatoolkit=12.4 via $I" | tee -a "$SETUP_LOG"
    break
  fi

  # Last resort: try 11.8 in case the host is older (RTX 3090/4090/A100)
  "$I" install -n base -c conda-forge openmm pdbfixer cudatoolkit=11.8 -y >> "$SETUP_LOG" 2>&1
  if [ $? -eq 0 ]; then
    SUCCESS=1
    echo "INSTALL_OK:cudatoolkit=11.8 via $I" | tee -a "$SETUP_LOG"
    break
  fi

  "$I" install -n base -c conda-forge openmm pdbfixer cuda-version=11.8 -y >> "$SETUP_LOG" 2>&1
  if [ $? -eq 0 ]; then
    SUCCESS=1
    echo "INSTALL_OK:cuda-version=11.8 via $I" | tee -a "$SETUP_LOG"
    break
  fi
done

if [ $SUCCESS -ne 1 ]; then
  echo "INSTALL_FAIL: all installer candidates failed" | tee -a "$SETUP_LOG"
  exit 2
fi

# 4) Verify OpenMM + CUDA platform
$PY - <<'PY'
import sys
import openmm
names = [openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())]
print("OPENMM_VERSION", openmm.__version__)
print("OPENMM_PLATFORMS", names)
if "CUDA" not in names:
    sys.exit(3)
PY
EC=$?
if [ $EC -ne 0 ]; then
  echo "VERIFY_FAIL: OpenMM installed but CUDA platform missing" | tee -a "$SETUP_LOG"
  exit $EC
fi

echo "OPENMM_INSTALL_COMPLETE" | tee -a "$SETUP_LOG"
exit 0
"""

    async def _run_remote_script_via_scp(
        self,
        script_content: str,
        remote_path: str,
        timeout: int,
    ) -> SSHResult:
        """Upload a local temp bash script and execute it remotely via bash."""
        local_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".sh",
                delete=False,
                encoding="utf-8",
                newline="\n",
            ) as tf:
                tf.write(script_content)
                local_path = tf.name

            up = await self._scp_up(local_path, remote_path, timeout=90)
            if not up.success:
                raise RuntimeError(f"SCP script upload failed: {up.stderr}")

            await self._ssh(f"chmod +x {remote_path}", timeout=15)
            return await self._ssh(
                f"bash {remote_path}",
                protocol=CommandProtocol.RETRY_3X,
                timeout=timeout,
            )
        finally:
            if local_path:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass

    # ── FASE 2: Install OpenMM ───────────────────────────────────

    async def _fase_2_install_openmm(self):
        self.state.phase = OrchestratorPhase.INSTALL

        # ── Custom image: OpenMM + CUDA already baked in ──
        if self._uses_custom_image:
            self._emit("install", "Custom image — OpenMM pre-installed, verifying...")
            python = self.state.conda_python
            verify = await self._ssh(
                f'{python} -c "'
                'import openmm; '
                'print(openmm.__version__); '
                'names=[openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())]; '
                'print(names)"',
                timeout=30,
            )
            if verify.success and "CUDA" in verify.stdout:
                self._emit("install", f"Pre-baked OpenMM verified: {verify.stdout.strip()}")
                return
            self._emit("install", "Pre-baked OpenMM CUDA check failed — falling through to runtime install")

        python = self.state.conda_python
        # Determine conda/mamba binary
        conda_dir = os.path.dirname(python)  # e.g. /opt/miniforge3/bin
        # Check if mamba is available
        mamba_check = await self._ssh(f"test -f {conda_dir}/mamba && echo YES || echo NO", timeout=10)
        use_mamba = "YES" in mamba_check.stdout

        installer = f"{conda_dir}/mamba" if use_mamba else f"{conda_dir}/conda"
        self._emit("install", f"Using {'mamba' if use_mamba else 'conda'}")

        # Check if OpenMM already installed
        check = await self._ssh(
            f'{python} -c "import openmm; print(openmm.__version__)" 2>/dev/null',
            protocol=CommandProtocol.FAIL_FAST,
            timeout=15,
        )
        if check.success and check.stdout.strip():
            self._emit("install", f"OpenMM {check.stdout.strip()} already installed")
            # Still verify CUDA platform
            plat_check = await self._ssh(
                f'{python} -c "'
                'import openmm;'
                'names=[openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())];'
                'print(names)"',
                timeout=15,
            )
            if "CUDA" in plat_check.stdout:
                self._emit("install", f"Platforms: {plat_check.stdout.strip()}")
                return
            else:
                self._emit("install", "CUDA platform not found — reinstalling with cudatoolkit")

        setup_log = f"{self.state.run_dir}/logs/setup_openmm.log"
        script = self._build_openmm_install_script(
            python_bin=python,
            preferred_installer=installer,
            setup_log=setup_log,
        )
        self._emit("install", "Installing OpenMM CUDA 11.8 via deterministic SCP+bash installer")
        await self._ssh(f"mkdir -p {self.state.run_dir}/logs", timeout=15)
        r = await self._run_remote_script_via_scp(
            script_content=script,
            remote_path=f"/tmp/mica_install_openmm_{self.cfg.job_id}.sh",
            timeout=1200,
        )
        if not r.success:
            tail = await self._ssh(f"tail -n 60 {setup_log} 2>/dev/null || true", timeout=20)
            raise RuntimeError(
                f"OpenMM install failed: {r.stderr or r.stdout}\n"
                f"setup_log_tail:\n{tail.stdout}"
            )

        # Verify
        verify = await self._ssh(
            f'{python} -c "'
            'import openmm; '
            'print(openmm.__version__); '
            'names=[openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())]; '
            'print(names)"',
            timeout=30,
        )
        if not verify.success or "CUDA" not in verify.stdout:
            raise RuntimeError(
                f"OpenMM verification failed. stdout: {verify.stdout}, stderr: {verify.stderr}"
            )
        self._emit("install", f"OpenMM verified: {verify.stdout.strip()}")

    # ── FASE 3: Stage force-field assets (policy-driven) ──────────

    async def _fase_3_stage_forcefield(self):
        self.state.phase = OrchestratorPhase.STAGE_FF

        python = self.state.conda_python

        # Policy-driven: only stage CHARMM36_2024 when policy requests it.
        policy = getattr(self.cfg, "_ff_policy", None)
        stage_2024 = bool(getattr(policy, "stage_charmm36_2024", False))
        if not stage_2024:
            self._emit("stage_ff", "No force-field staging requested by policy")
            return

        # Check if FF is already installed
        ff_check = await self._ssh(
            f'{python} -c "from openmm.app import ForceField; '
            'ForceField(\\"charmm36_2024.xml\\",\\"charmm36_2024/water.xml\\"); '
            'print(\\"FF_LOAD_OK\\")" 2>/dev/null',
            protocol=CommandProtocol.FAIL_FAST,
            timeout=30,
        )
        if ff_check.success and "FF_LOAD_OK" in ff_check.stdout:
            self.state.selected_forcefield = "charmm36_2024"
            self._emit("stage_ff", "CHARMM36m force-field already present")
            return

        self._emit("stage_ff", "Staging CHARMM36_2024 force-field from local machine")

        # Degrade gracefully (R-08): if local assets aren't present, skip staging and
        # let verification fall back to any built-in FF.
        if not self.cfg.local_charmm36_xml or not os.path.isfile(self.cfg.local_charmm36_xml):
            self._emit(
                "stage_ff",
                "CHARMM36_2024 staging requested but local charmm36_2024.xml not found; continuing without staging",
                local_charmm36_xml=self.cfg.local_charmm36_xml,
            )
            return
        if not self.cfg.local_charmm36_dir or not os.path.isdir(self.cfg.local_charmm36_dir):
            self._emit(
                "stage_ff",
                "CHARMM36_2024 staging requested but local charmm36_2024/ dir not found; continuing without staging",
                local_charmm36_dir=self.cfg.local_charmm36_dir,
            )
            return

        staging = f"{self.state.run_dir}/charmm36_staging"
        await self._ssh(f"mkdir -p {staging}/charmm36_2024")

        # Upload XML
        r = await self._scp_up(self.cfg.local_charmm36_xml, f"{staging}/charmm36_2024.xml", timeout=60)
        if not r.success:
            self._emit("stage_ff", f"Warning: SCP charmm36_2024.xml failed ({r.stderr}); continuing")
            return

        # Upload dir (recursive)
        r = await self._scp_up(self.cfg.local_charmm36_dir, f"{staging}/charmm36_2024", recursive=True, timeout=120)
        if not r.success:
            self._emit("stage_ff", f"Warning: SCP charmm36_2024/ dir failed ({r.stderr}); continuing")
            return

        # Find OpenMM data dir and copy
        copy_cmd = (
            f'OMDIR=$({python} -c "import openmm.app, os; '
            f'print(os.path.join(os.path.dirname(openmm.app.__file__),\\"data\\"))") && '
            f'cp {staging}/charmm36_2024.xml $OMDIR/ && '
            f'mkdir -p $OMDIR/charmm36_2024 && '
            f'cp -r {staging}/charmm36_2024/* $OMDIR/charmm36_2024/ && '
            f'echo "FF_STAGED_OK"'
        )
        r = await self._ssh(copy_cmd, timeout=60)
        if not r.success or "FF_STAGED_OK" not in r.stdout:
            self._emit(
                "stage_ff",
                "Warning: force-field staging failed; continuing without staged CHARMM36_2024",
                stderr=r.stderr,
            )
            return

        self._emit("stage_ff", "CHARMM36m files staged successfully")

    # ── FASE 4: Verify force-field ───────────────────────────────

    async def _fase_4_verify_forcefield(self):
        self.state.phase = OrchestratorPhase.VERIFY_FF
        python = self.state.conda_python

        policy = getattr(self.cfg, "_ff_policy", None)
        requested_mode = (getattr(policy, "name", "auto") or "auto")

        base_candidates = [
            ("charmm36_2024", "charmm36_2024.xml", "charmm36_2024/water.xml"),
            ("charmm36", "charmm36.xml", "charmm36/water.xml"),
            ("amber14", "amber14-all.xml", "amber14/tip3pfb.xml"),
        ]

        if requested_mode in {"charmm36_2024", "charmm36", "amber14"}:
            # Try requested first, then degrade to remaining candidates.
            candidates = [c for c in base_candidates if c[0] == requested_mode]
            candidates += [c for c in base_candidates if c[0] != requested_mode]
        else:
            candidates = list(base_candidates)
        errors: list[str] = []
        for ff_name, main_xml, water_xml in candidates:
            r = await self._ssh(
                f'{python} -c "from openmm.app import ForceField; '
                f'ForceField(\\"{main_xml}\\",\\"{water_xml}\\"); '
                'print(\\"FF_LOAD_OK\\")"',
                timeout=30,
            )
            if r.success and "FF_LOAD_OK" in r.stdout:
                self.state.selected_forcefield = ff_name
                self._emit("verify_ff", f"Force-field verified: {ff_name}")
                return
            errors.append(f"{ff_name}: {r.stdout} | {r.stderr}")

        raise RuntimeError(
            "Force-field verification failed for all candidates: " + " || ".join(errors)
        )

    # ── FASE 5: Upload simulation files ──────────────────────────

    async def _fase_5_upload_files(self):
        self.state.phase = OrchestratorPhase.UPLOAD

        run_dir = self.state.run_dir

        # Create directories for all replicas  (R1: /workspace)
        dir_cmds = [f"mkdir -p {run_dir}/logs"]
        for i in range(1, self.cfg.n_replicas + 1):
            dir_cmds.append(f"mkdir -p {run_dir}/runs/replica_{i}")
        dir_cmds.append(f"mkdir -p {run_dir}/manifest")
        if self._uses_protein_ligand_complex_contract():
            dir_cmds.extend(
                [
                    f"mkdir -p {run_dir}/inputs",
                    f"mkdir -p {run_dir}/workers/dynamo/biodynamo/core",
                    f"mkdir -p {run_dir}/workers/dynamo/biodynamo/processors",
                    f"mkdir -p {run_dir}/workers/dynamo/biodynamo/protonation",
                    f"mkdir -p {run_dir}/src/mica",
                    f"mkdir -p {run_dir}/src/mica/sim",
                    f"mkdir -p {run_dir}/src/mica/sim/openmm_compiler",
                    f"mkdir -p {run_dir}/src/mica/drivers",
                ]
            )
        await self._ssh(" && ".join(dir_cmds), timeout=15)
        self._emit("upload", f"Created {run_dir}/ directory tree")

        # Upload PDB
        pdb_source = self._effective_local_pdb_source()
        pdb_name = self._effective_remote_pdb_name()
        if pdb_source:
            r = await self._scp_up(pdb_source, f"{run_dir}/{pdb_name}")
            if not r.success:
                raise RuntimeError(f"SCP PDB failed: {r.stderr}")
            self._emit("upload", f"Uploaded {pdb_name}")
        elif self._effective_remote_pdb_restore_source() and self._storage_enabled():
            await self._restore_resume_artifacts_from_storage()
            self._emit("upload", f"Restored {pdb_name} from durable storage")
        else:
            raise RuntimeError("No local or storage-backed PDB source available")

        if self._uses_protein_ligand_complex_contract():
            docked_pose = Path(self.cfg.docked_ligand_pdb).expanduser()
            remote_docked_pose = self._remote_docked_pose_path(run_dir)
            r = await self._scp_up(str(docked_pose), remote_docked_pose, timeout=180)
            if not r.success:
                raise RuntimeError(f"SCP docked ligand pose failed: {r.stderr}")
            self._emit("upload", f"Uploaded {remote_docked_pose}")

        # Upload simulation script
        if self.cfg.simulation_script and os.path.isfile(self.cfg.simulation_script):
            script_name = os.path.basename(self.cfg.simulation_script)
            r = await self._scp_up(
                self.cfg.simulation_script, f"{run_dir}/{script_name}"
            )
            if not r.success:
                raise RuntimeError(f"SCP script failed: {r.stderr}")
            self._emit("upload", f"Uploaded {script_name}")

        # Upload extractor script
        if self.cfg.extractor_script and os.path.isfile(self.cfg.extractor_script):
            ext_name = os.path.basename(self.cfg.extractor_script)
            r = await self._scp_up(
                self.cfg.extractor_script, f"{run_dir}/{ext_name}"
            )
            if not r.success:
                self._emit("upload", f"Warning: extractor upload failed ({r.stderr})")
            else:
                self._emit("upload", f"Uploaded {ext_name}")

        # Upload or restore resume artifacts
        for replica in self._resume_replica_specs().values():
            if replica.checkpoint_path and os.path.isfile(replica.checkpoint_path):
                r = await self._scp_up(
                    replica.checkpoint_path,
                    self._replica_checkpoint_remote_path(replica.replica_id),
                    timeout=180,
                )
                if not r.success:
                    raise RuntimeError(f"SCP resume checkpoint failed: {r.stderr}")
            elif replica.checkpoint_storage_path and self._storage_enabled():
                await self._restore_resume_artifacts_from_storage()

            if replica.prepared_pdb_path and os.path.isfile(replica.prepared_pdb_path):
                r = await self._scp_up(
                    replica.prepared_pdb_path,
                    self._replica_prepared_pdb_remote_path(replica.replica_id),
                    timeout=180,
                )
                if not r.success:
                    raise RuntimeError(f"SCP prepared PDB failed: {r.stderr}")

        # ── P2: Stage required processor modules ──────────────────
        # Gather and upload all modules required by the simulation mode.
        # This ensures that import statements in the launched script won't fail.
        try:
            required_modules = self._required_remote_support_files(run_dir)
            if required_modules:
                for module_name, local_path, remote_module_path, local_sha256 in required_modules:
                    r = await self._scp_up(local_path, remote_module_path, timeout=60)
                    if not r.success:
                        raise RuntimeError(
                            f"SCP module {module_name} failed: {r.stderr}"
                        )
                    
                    # Verify remote file hash to ensure upload integrity
                    remote_sha_cmd = (
                        f"python3 -c \"import hashlib; "
                        f"print(hashlib.sha256(open('{remote_module_path}', 'rb').read()).hexdigest())\""
                    )
                    hash_result = await self._ssh(remote_sha_cmd, timeout=30)
                    if not hash_result.success:
                        raise RuntimeError(
                            f"Failed to verify module {module_name} hash: {hash_result.stderr}"
                        )
                    remote_sha256 = hash_result.stdout.strip()
                    if remote_sha256 != local_sha256:
                        raise RuntimeError(
                            f"Module {module_name} hash mismatch: "
                            f"local={local_sha256}, remote={remote_sha256}"
                        )
                    self._emit("upload", f"Verified {module_name} (SHA256: {local_sha256[:16]}...)")
        except FileNotFoundError as e:
            raise RuntimeError(f"Module dependency resolution failed: {e}")

        await self._persist_runtime_manifests()
        if self._storage_enabled():
            await self._sync_storage_artifacts("upload")

    # ── FASE 6: Launch simulations ───────────────────────────────

    def _build_replica_args(self, replica_id: int, gpu_id: int,
                            run_dir: str, pdb_name: str,
                            script_name: str) -> list[str]:
        """Build per-replica CLI arguments depending on simulation_mode."""
        python = self.state.conda_python
        lines: list[str] = []
        resume_spec = self._resume_replica_specs().get(replica_id)
        pdb_arg_path = f"{run_dir}/{pdb_name}"
        if resume_spec and (resume_spec.prepared_pdb_path or resume_spec.prepared_pdb_storage_path):
            pdb_arg_path = self._replica_prepared_pdb_remote_path(replica_id)
        resume_arg = self._replica_checkpoint_remote_path(replica_id) if resume_spec else ""
        script_path = self._remote_simulation_script_path(run_dir, script_name)

        lines.append(f"# ── Replica {replica_id} on GPU {gpu_id} ──")

        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            if self._uses_protein_ligand_complex_contract() and script_name == "run_complex_stability.py":
                if resume_arg:
                    raise RuntimeError("Protein-ligand complex_stability remote lane does not support resume checkpoints yet")
                lines.append(
                    f"{self._remote_python_prefix(run_dir, gpu_id)} nohup {python} -u {script_path} \\"
                )
                lines.append(f"    --protein-pdb {shlex.quote(pdb_arg_path)} \\")
                lines.append(f"    --ligand-smiles {shlex.quote(self.cfg.ligand_smiles)} \\")
                lines.append(f"    --docked-ligand-pdb {shlex.quote(self._remote_docked_pose_path(run_dir))} \\")
                lines.append(f"    --output-dir {shlex.quote(f'{run_dir}/runs/replica_{replica_id}')} \\")
                lines.append(f"    --job-name {shlex.quote(self._replica_run_name(replica_id))} \\")
                lines.append(f"    --production-ns {self.cfg.production_ns} \\")
                lines.append("    --platform CUDA \\")
                lines.append(f"    --gpu-id {gpu_id} \\")
                if self.cfg.extra_args:
                    lines.append(f"    {self.cfg.extra_args} \\")
                lines.append(
                    f"    > {run_dir}/logs/replica_{replica_id}.log 2>&1 &"
                )
            else:
                lines.append(
                    f"{self._remote_python_prefix(run_dir, gpu_id)} nohup {python} -u {script_path} \\"
                )
                lines.append(f"    --pdb {pdb_arg_path} \\")
                lines.append(f"    --output_dir {run_dir}/runs/replica_{replica_id} \\")
                lines.append(f"    --replica {replica_id} \\")
                lines.append(f"    --ns {self.cfg.production_ns} \\")
                lines.append(f"    --platform CUDA \\")
                lines.append(f"    --gpu_id {gpu_id} \\")
                if resume_arg:
                    lines.append(f"    --resume {resume_arg} \\")
                if self.cfg.prepare and not resume_arg:
                    lines.append("    --prepare \\")
                lines.append(f"    --padding {self.cfg.padding} \\")
                if self.cfg.extra_args:
                    lines.append(f"    {self.cfg.extra_args} \\")
                lines.append(
                    f"    > {run_dir}/logs/replica_{replica_id}.log 2>&1 &"
                )
        else:
            # ── BINDING mode: run_binding_simulation_spontaneous.py ──
            lines.append(
                f"{self._remote_python_prefix(run_dir, gpu_id)} nohup {python} -u {script_path} \\"
            )
            lines.append(f"    --pdb {pdb_arg_path} \\")
            lines.append(f"    --output_dir {run_dir}/runs/replica_{replica_id} \\")
            lines.append(f"    --steps {self.cfg.steps} \\")
            if resume_arg:
                lines.append(f"    --resume {resume_arg} \\")
                if resume_spec is None or resume_spec.skip_equilibration:
                    lines.append("    --skip_equilibration \\")
            if self.cfg.prepare and not resume_arg:
                lines.append("    --prepare \\")
            lines.append(f"    --padding {self.cfg.padding} \\")
            if self.state.selected_forcefield:
                lines.append(f"    --forcefield-mode {self.state.selected_forcefield} \\")
            if self.cfg.extra_args:
                lines.append(f"    {self.cfg.extra_args} \\")
            lines.append(
                f"    > {run_dir}/logs/replica_{replica_id}.log 2>&1 &"
            )

        lines.append(f"echo \"SIM_PID_R{replica_id}:$!\"")
        lines.append("")
        return lines

    async def _fase_6_launch(self):
        self.state.phase = OrchestratorPhase.LAUNCH

        run_dir = self.state.run_dir
        pdb_name = self._effective_remote_pdb_name()
        python = self.state.conda_python

        if self.cfg.simulation_script:
            script_name = os.path.basename(self.cfg.simulation_script)
        else:
            script_name = self.cfg._expected_simulation_script_name_for_current_config()

        # Build launch.sh (R5: always use SCP'd script, never HEREDOC)
        launch_lines = ["#!/bin/bash", "set -e", ""]
        launch_lines.append("# Kill any previous simulation processes (R8)")
        launch_lines.append(self._safe_remote_kill_command(script_name))
        launch_lines.append("sleep 2")
        launch_lines.append(f"cd {run_dir}")
        launch_lines.append("")

        for replica_id in range(1, self.cfg.n_replicas + 1):
            gpu_id = replica_id - 1  # R3: one GPU per replica
            launch_lines.extend(
                self._build_replica_args(
                    replica_id, gpu_id, run_dir, pdb_name, script_name,
                )
            )

        # Extractor daemon (if script present)
        if self.cfg.extractor_script and os.path.isfile(self.cfg.extractor_script):
            ext_name = os.path.basename(self.cfg.extractor_script)
            for replica_id in range(1, self.cfg.n_replicas + 1):
                launch_lines.append(f"# Extractor for replica {replica_id}")
                launch_lines.append(
                    f"SNAPSHOTS_PDB={run_dir}/runs/replica_{replica_id}/replica_{replica_id}_snapshots.pdb \\"
                )
                launch_lines.append(
                    f"LATEST_PDB={run_dir}/runs/replica_{replica_id}/replica_{replica_id}_latest.pdb \\"
                )
                launch_lines.append("INTERVAL_SEC=600 \\")
                launch_lines.append(
                    f"nohup {python} {run_dir}/{ext_name} "
                    f"> {run_dir}/logs/extractor_r{replica_id}.log 2>&1 &"
                )
                launch_lines.append(f"echo \"EXT_PID_R{replica_id}:$!\"")
                launch_lines.append("")

        launch_lines.append("echo LAUNCH_COMPLETE")

        # ── P2: Pre-launch module verification ────────────────────
        # Verify that all required modules exist in the run directory before launch.
        # This catches missing modules early, with a clear error message, without
        # triggering expensive imports that might fail due to missing dependencies.
        try:
            required_modules = self._required_remote_support_files(run_dir)
            for module_name, _, remote_module_path, _ in required_modules:
                # Check if module file exists (lightweight check)
                test_cmd = f"test -f {remote_module_path} && echo 'EXISTS' || echo 'MISSING'"
                result = await self._ssh(test_cmd, timeout=10)
                if "EXISTS" not in result.stdout:
                    raise RuntimeError(
                        f"Required module not found on remote: {remote_module_path} "
                        f"(for {self.cfg.simulation_mode.value} mode)"
                    )
                self._emit("launch", f"Module present: {module_name}")
        except FileNotFoundError as e:
            raise RuntimeError(f"Module dependency resolution failed before launch: {e}")

        # Write launch.sh to temp, SCP it, execute it
        launch_content = "\n".join(launch_lines)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_launch.sh", delete=False, newline="\n", encoding="utf-8"
        ) as f:
            f.write(launch_content)
            local_launch = f.name

        try:
            r = await self._scp_up(local_launch, "/tmp/launch.sh")
            if not r.success:
                raise RuntimeError(f"SCP launch.sh failed: {r.stderr}")
        finally:
            os.unlink(local_launch)

        # Make executable and run
        r = await self._ssh(
            "chmod +x /tmp/launch.sh && bash /tmp/launch.sh",
            timeout=30,
        )
        if not r.success or "LAUNCH_COMPLETE" not in r.stdout:
            raise RuntimeError(f"Launch failed: {r.stdout}\n{r.stderr}")

        # Parse PIDs
        for line in r.stdout.split("\n"):
            for replica_id in range(1, self.cfg.n_replicas + 1):
                tag = f"SIM_PID_R{replica_id}:"
                if tag in line:
                    try:
                        pid = int(line.split(tag)[1].strip())
                        self.state.replicas[replica_id] = ReplicaStatus(
                            replica_id=replica_id,
                            gpu_id=replica_id - 1,
                            pid=pid,
                            status=SimStatus.SOLVATING,
                            target_ns=self._target_ns_for_job(),
                        )
                        self._emit("launch", f"Replica {replica_id} PID={pid} on GPU {replica_id-1}")
                    except ValueError:
                        self._emit("launch", f"Warning: could not parse PID from '{line}'")

        # Wait 10s then verify PIDs are alive (R8)
        await asyncio.sleep(10)
        await self._verify_launch()
        await self._persist_runtime_manifests()
        if self._storage_enabled():
            await self._sync_storage_artifacts("launch")

        # W3-3: Register run with Pod API for structured metrics/teardown
        if self.state.pod_api_available:
            await self._register_pod_run()

    def _get_sim_grep_pattern(self) -> str:
        """Return the grep pattern that matches the active simulation script process."""
        if self.cfg.simulation_mode == SimulationMode.COMPLEX:
            if self._uses_protein_ligand_complex_contract():
                return "run_complex_stability"
            return "runcomplex_paper"
        return "run_binding"

    async def _verify_launch(self):
        """Verify simulation processes are running after launch."""
        pattern = self._get_sim_grep_pattern()
        r = await self._ssh(
            f"ps aux | grep {pattern} | grep -v grep",
            timeout=15,
        )
        running_pids = len([l for l in r.stdout.strip().split("\n") if l.strip()])
        expected = self.cfg.n_replicas
        if running_pids != expected:
            self._emit(
                "launch",
                f"WARNING: Expected {expected} processes, found {running_pids}. "
                "Checking logs for errors...",
            )
            # Check first replica log for errors
            log_check = await self._ssh(
                f"tail -20 {self.state.run_dir}/logs/replica_1.log 2>/dev/null || echo NO_LOG",
                timeout=15,
            )
            self._emit("launch", f"Log tail: {log_check.stdout[-300:]}")
            if running_pids == 0:
                raise RuntimeError(
                    f"No simulation processes found after launch. "
                    f"Log: {log_check.stdout[-500:]}"
                )
        else:
            self._emit("launch", f"Verified: {running_pids}/{expected} processes running")

    # ── FASE 7: Monitor ──────────────────────────────────────────

    async def _fase_7_monitor(self):
        self.state.phase = OrchestratorPhase.MONITOR
        self._emit("monitor", f"Entering monitoring loop (interval={self.cfg.monitor_interval_sec}s)")

        deadline = _utcnow_dt() + timedelta(hours=self.cfg.max_runtime_hours)
        _last_progress_ns: dict[int, float] = {}  # replica_id → last observed ns
        _last_progress_log: dict[int, str] = {}   # replica_id → last observed log line
        _last_status: dict[int, SimStatus] = {}   # replica_id → last observed state
        _idle_since: float = time.monotonic()

        while not self._stop_requested:
            await asyncio.sleep(self.cfg.monitor_interval_sec)

            # Cost check
            if self.state.instance_id:
                try:
                    inst = await self.provider.get_instance_status(self.state.instance_id)
                    self.state.total_cost_usd = inst.compute_current_cost()
                except Exception:
                    pass

            if self.state.total_cost_usd >= self.cfg.max_total_cost_usd:
                self._emit(
                    "monitor",
                    f"COST LIMIT REACHED: ${self.state.total_cost_usd:.2f} ≥ ${self.cfg.max_total_cost_usd:.2f}",
                )
                break

            # Time check
            if _utcnow_dt() > deadline:
                self._emit("monitor", f"RUNTIME LIMIT REACHED: {self.cfg.max_runtime_hours}h")
                break

            # Per-replica status + idle detection
            all_done = True
            any_progress = False
            for replica_id in range(1, self.cfg.n_replicas + 1):
                status = await self._check_replica(replica_id)
                if status.status not in (SimStatus.COMPLETE, SimStatus.FAILED):
                    all_done = False
                # Check for progress (ns advancement)
                prev_ns = _last_progress_ns.get(replica_id, 0.0)
                current_ns = getattr(status, "current_ns", 0.0) or 0.0
                current_log = (getattr(status, "last_log_line", "") or "").strip()
                prev_log = _last_progress_log.get(replica_id, "")
                prev_status = _last_status.get(replica_id)
                if current_ns > prev_ns:
                    any_progress = True
                    _last_progress_ns[replica_id] = current_ns
                elif current_log and current_log != prev_log:
                    any_progress = True
                elif prev_status is not None and status.status != prev_status:
                    any_progress = True

                if current_log:
                    _last_progress_log[replica_id] = current_log
                _last_status[replica_id] = status.status

            # W3-3: Overlay Pod API metrics when available
            if self.state.pod_api_available:
                await self._overlay_pod_api_metrics()

            # W4-3: Stream trajectory frames to WS channel
            if self.state.pod_api_available:
                await self._stream_trajectory_to_ws()

            # W4-4: Push live RMSD/RMSF analysis to WS channel (every 5th cycle)
            if self.state.pod_api_available:
                await self._push_live_analysis_to_ws()

            # Idle timeout: auto-destroy if no replica advanced in N seconds
            if any_progress:
                _idle_since = time.monotonic()
            elif (time.monotonic() - _idle_since) >= self.cfg.idle_timeout_sec:
                self._emit(
                    "monitor",
                    f"IDLE TIMEOUT: no progress for {self.cfg.idle_timeout_sec}s — "
                    f"auto-destroying instance to prevent cost leak",
                )
                break

            self._emit(
                "monitor",
                f"Cost: ${self.state.total_cost_usd:.4f} | "
                + " | ".join(
                    f"R{rid}: {rs.speed_ns_day:.0f} ns/day, "
                    f"{rs.current_ns:.1f}/{rs.target_ns:.1f} ns, "
                    f"ETA {rs.eta_hours:.1f}h"
                    for rid, rs in self.state.replicas.items()
                ),
            )

            if all_done:
                self._emit("monitor", "All replicas complete")
                break

            await self._persist_runtime_manifests()
            if self._storage_enabled() and (
                self._last_storage_sync_at == 0.0 or
                (time.monotonic() - self._last_storage_sync_at) >= self.cfg.storage_sync_interval_sec
            ):
                await self._sync_storage_artifacts("monitor_interval")

        if self._stop_requested:
            await self._perform_safe_stop()

    # ── Pod API metrics overlay (W3-3) ───────────────────────────

    async def _overlay_pod_api_metrics(self):
        """Fetch live metrics from the Pod API and overlay onto replica state.

        This enriches the SSH-parsed monitoring data with structured metrics
        (ns/day, GPU utilization, energy) directly from the OpenMM reporter
        running inside the pod.  Non-destructive: SSH data is kept as fallback.
        """
        if not self.state.pod_api_url:
            return
        client = _PodAPIClient(self.state.pod_api_url)
        # The Pod API currently tracks a single run; use the first run_id we know
        run_id = getattr(self.state, "_pod_run_id", None)
        if not run_id:
            return
        try:
            metrics = await client.get_metrics(run_id)
            if not metrics:
                return
            # Apply to replica 1 (single-run pod model)
            rs = self.state.replicas.get(1)
            if rs is None:
                return
            ns_per_day = metrics.get("ns_per_day")
            if ns_per_day and ns_per_day > 0:
                rs.speed_ns_day = ns_per_day
            ns_completed = metrics.get("ns_completed")
            if ns_completed is not None:
                rs.current_ns = ns_completed
            gpu_util = metrics.get("gpu_utilization")
            if gpu_util is not None:
                rs.gpu_utilization = gpu_util
            gpu_mem = metrics.get("gpu_memory_mb")
            if gpu_mem is not None:
                rs.gpu_memory_used_mb = gpu_mem
        except Exception as e:
            logger.debug("Pod API metrics overlay failed (non-fatal): %s", e)

    # ── Trajectory frame streaming to WS (W4-3) ─────────────────

    async def _stream_trajectory_to_ws(self):
        """Fetch latest trajectory frame from Pod API and push to WS channel.

        Tracks the last-seen frame index so only new frames are pushed.
        Non-fatal: failures are logged but never interrupt monitoring.
        """
        if not self.state.pod_api_url:
            return
        run_id = getattr(self.state, "_pod_run_id", None)
        if not run_id:
            return

        last_frame = getattr(self, "_last_ws_traj_frame", -1)
        client = _PodAPIClient(self.state.pod_api_url)

        try:
            data = await client.get_trajectory_frame_range(run_id, since_frame=last_frame)
            if not data:
                return

            frame_idx = data.get("frame_index", 0)
            pdb_data = data.get("data", "")
            if frame_idx <= last_frame or not pdb_data:
                return

            self._last_ws_traj_frame = frame_idx

            # Push to WS channel — import lazily to avoid circular deps
            try:
                from mica.ws_md import publish_trajectory_frame
                job_id = getattr(self, "_ws_job_id", None) or str(self.cfg.job_id)
                publish_trajectory_frame(
                    job_id=job_id,
                    frame_index=frame_idx,
                    step=data.get("step", 0),
                    time_ps=data.get("time_ps", 0.0),
                    pdb_data=pdb_data,
                    run_id=run_id,
                )
                logger.debug(
                    "Trajectory frame %d pushed to WS for job %s",
                    frame_idx, job_id,
                )
            except Exception as ws_err:
                logger.debug("WS trajectory push failed (non-fatal): %s", ws_err)

        except Exception as e:
            logger.debug("Trajectory stream fetch failed (non-fatal): %s", e)

    async def _push_live_analysis_to_ws(self):
        """Fetch live RMSD/RMSF from Pod API and push to WS channel.

        Rate-limited: only fires every 5th monitoring cycle (~50 s) to
        avoid recomputing analysis every tick.  Non-fatal.
        """
        self._analysis_ws_counter = getattr(self, "_analysis_ws_counter", 0) + 1
        if self._analysis_ws_counter % 5 != 0:
            return  # skip this cycle

        if not self.state.pod_api_url:
            return
        run_id = getattr(self.state, "_pod_run_id", None)
        if not run_id:
            return

        client = _PodAPIClient(self.state.pod_api_url)
        job_id = getattr(self, "_ws_job_id", None) or str(self.cfg.job_id)

        try:
            from mica.ws_md import publish_md_event
        except Exception:
            return  # WS module not available

        # RMSD
        try:
            rmsd = await client.get_live_rmsd(run_id)
            if rmsd and not rmsd.get("error"):
                publish_md_event(
                    job_id=job_id,
                    phase="analysis",
                    message=f"RMSD update: {rmsd.get('n_frames',0)} frames, "
                            f"mean={rmsd.get('mean_angstrom',0):.2f} Å",
                    snapshot={"type": "live_rmsd", **rmsd},
                )
        except Exception as e:
            logger.debug("Live RMSD WS push failed (non-fatal): %s", e)

        # RMSF
        try:
            rmsf = await client.get_live_rmsf(run_id)
            if rmsf and not rmsf.get("error"):
                publish_md_event(
                    job_id=job_id,
                    phase="analysis",
                    message=f"RMSF update: {rmsf.get('n_frames',0)} frames, "
                            f"max={rmsf.get('max_angstrom',0):.2f} Å",
                    snapshot={"type": "live_rmsf", **rmsf},
                )
        except Exception as e:
            logger.debug("Live RMSF WS push failed (non-fatal): %s", e)

    async def _check_replica(self, replica_id: int) -> ReplicaStatus:
        """Check the status of one simulation replica."""
        rs = self.state.replicas.get(replica_id)
        if rs is None:
            rs = ReplicaStatus(replica_id=replica_id, gpu_id=replica_id - 1)
            self.state.replicas[replica_id] = rs

        run_dir = self.state.run_dir
        log_path = self._replica_log_remote_path(replica_id)

        # Check if process is still alive
        fallback_pid_cmd = f"ps aux | grep {self._get_sim_grep_pattern()} | grep -v grep | head -1 | awk '{{print $2}}'"
        pid_check = await self._ssh(
            f"ps -p {rs.pid} -o pid= 2>/dev/null" if rs.pid else fallback_pid_cmd,
            protocol=CommandProtocol.FAIL_FAST,
            timeout=10,
        )
        active_pid = pid_check.stdout.strip()
        process_alive = bool(active_pid)

        if not process_alive and rs.pid:
            fallback_pid = await self._ssh(
                fallback_pid_cmd,
                protocol=CommandProtocol.FAIL_FAST,
                timeout=10,
            )
            active_pid = fallback_pid.stdout.strip()
            process_alive = bool(active_pid)
            if process_alive:
                try:
                    rs.pid = int(active_pid.splitlines()[0].strip())
                except ValueError:
                    pass

        # Get speed from log
        speed_result = await self._ssh(
            f'grep -i speed {log_path} | tail -1',
            protocol=CommandProtocol.FAIL_FAST,
            timeout=10,
        )
        if speed_result.success and speed_result.stdout.strip():
            rs.last_log_line = speed_result.stdout.strip()
            # Parse speed: "#"Speed" 0.XXX ns/day, XX.X ns/day" or "Speed: XXX ns/day"
            speed_match = re.search(r'([\d.]+)\s*ns/day', speed_result.stdout)
            if speed_match:
                # Get the LAST ns/day value in the line (the higher one)
                all_speeds = re.findall(r'([\d.]+)\s*ns/day', speed_result.stdout)
                if all_speeds:
                    rs.speed_ns_day = float(all_speeds[-1])

        # Get last log line for step/time
        tail_result = await self._ssh(
            f'tail -3 {log_path}',
            protocol=CommandProtocol.FAIL_FAST,
            timeout=10,
        )
        if tail_result.success and tail_result.stdout:
            log_lines = [line.strip() for line in tail_result.stdout.splitlines() if line.strip()]
            if log_lines:
                rs.last_log_line = log_lines[-1]
            progress_match = re.search(r'Progress:\s*([\d.]+)%\s*\(([\d.]+)\s*ns\)', tail_result.stdout)
            if progress_match:
                rs.current_ns = float(progress_match.group(2))

        if self.cfg.simulation_mode == SimulationMode.BINDING:
            rs.current_step = await self._binding_checkpoint_step(replica_id)
            if rs.current_step > 0:
                rs.current_ns = min(rs.current_step * 4e-6, rs.target_ns or self._target_ns_for_job())

        # GPU utilization
        gpu_result = await self._ssh(
            "nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader",
            protocol=CommandProtocol.FAIL_FAST,
            timeout=10,
        )
        if gpu_result.success:
            lines = gpu_result.stdout.strip().split("\n")
            if rs.gpu_id < len(lines):
                parts = lines[rs.gpu_id].split(",")
                if len(parts) >= 2:
                    try:
                        rs.gpu_utilization = float(parts[0].strip().replace("%", "").strip())
                        rs.gpu_memory_used_mb = float(parts[1].strip().split()[0])
                    except ValueError:
                        pass

        # Calculate ETA
        if rs.target_ns <= 0:
            rs.target_ns = self._target_ns_for_job()
        if rs.speed_ns_day > 0 and rs.target_ns > 0 and rs.current_ns >= 0:
            remaining_ns = max(0.0, rs.target_ns - rs.current_ns)
            rs.eta_hours = (remaining_ns / rs.speed_ns_day) * 24 if rs.speed_ns_day > 0 else 0.0

        # Determine simulation phase
        if not process_alive and rs.status != SimStatus.COMPLETE:
            # Process ended — check if it's done or crashed
            if await self._has_strict_completion_evidence(replica_id, log_path):
                rs.status = SimStatus.COMPLETE
                rs.current_ns = rs.target_ns
                rs.eta_hours = 0
            elif self._stop_requested or self.state.safe_stop_completed:
                rs.status = SimStatus.STOPPED
            else:
                rs.status = SimStatus.FAILED
        elif rs.gpu_utilization < 5 and rs.speed_ns_day == 0:
            rs.status = SimStatus.SOLVATING
        elif rs.speed_ns_day > 0:
            rs.status = SimStatus.PRODUCTION

        rs.last_check = _utcnow_dt()
        return rs

    # ── FASE 8: Download results ─────────────────────────────────

    async def _fase_8_download(self):
        self.state.phase = OrchestratorPhase.DOWNLOAD

        local_results = self.cfg.local_output_dir
        os.makedirs(local_results, exist_ok=True)
        self.state.local_output_dir = local_results

        for replica_id in range(1, self.cfg.n_replicas + 1):
            replica_dir = f"{self.state.run_dir}/runs/replica_{replica_id}"
            local_replica_dir = os.path.join(local_results, f"replica_{replica_id}")
            os.makedirs(local_replica_dir, exist_ok=True)

            # Download key files
            for fname in self._get_download_manifest(replica_id):
                remote = f"{replica_dir}/{fname}"
                local = os.path.join(local_replica_dir, fname)
                r = await self._scp_down(remote, local, timeout=600)
                if r.success:
                    self._emit("download", f"Downloaded {fname}")
                else:
                    self._emit("download", f"Warning: failed to download {fname} ({r.stderr[:100]})")

            # Download log
            log_remote = f"{self.state.run_dir}/logs/replica_{replica_id}.log"
            log_local = os.path.join(local_replica_dir, f"replica_{replica_id}.log")
            await self._scp_down(log_remote, log_local, timeout=120)

            self._hydrate_terminal_replica_from_downloaded_results(replica_id, local_replica_dir)

        self._emit("download", f"Results saved to {local_results}")
        await self._persist_runtime_manifests()

    def _hydrate_terminal_replica_from_downloaded_results(self, replica_id: int, local_replica_dir: str) -> None:
        result_path = Path(local_replica_dir) / f"{self._replica_run_name(replica_id)}_results.json"
        if not result_path.is_file():
            return

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Failed to parse downloaded replica result %s: %s", result_path, exc)
            return

        rs = self.state.replicas.get(replica_id)
        if rs is None:
            rs = ReplicaStatus(replica_id=replica_id, gpu_id=replica_id - 1)
            self.state.replicas[replica_id] = rs

        config = payload.get("config") or {}
        production = (payload.get("phases") or {}).get("production") or {}
        terminal_status = str(payload.get("status") or "").strip().lower()

        target_ns = config.get("production_ns")
        if target_ns is not None:
            try:
                rs.target_ns = float(target_ns)
            except (TypeError, ValueError):
                pass
        elif rs.target_ns <= 0:
            rs.target_ns = self._target_ns_for_job()

        duration_ns = production.get("duration_ns")
        if duration_ns is not None:
            try:
                rs.current_ns = float(duration_ns)
            except (TypeError, ValueError):
                pass

        ns_per_day = production.get("ns_per_day")
        if ns_per_day is not None:
            try:
                parsed_speed = float(ns_per_day)
            except (TypeError, ValueError):
                parsed_speed = 0.0
            if parsed_speed > 0:
                rs.speed_ns_day = parsed_speed

        if terminal_status == "completed":
            rs.status = SimStatus.COMPLETE
            rs.eta_hours = 0.0
            if rs.current_ns <= 0 and rs.target_ns > 0:
                rs.current_ns = rs.target_ns
            if rs.speed_ns_day > 0 and rs.current_ns > 0:
                rs.last_log_line = (
                    f"Production complete: {rs.current_ns:.6g} ns "
                    f"({rs.speed_ns_day:.1f} ns/day)"
                )
            elif rs.current_ns > 0:
                rs.last_log_line = f"Production complete: {rs.current_ns:.6g} ns"
            else:
                rs.last_log_line = "Production complete"

        rs.last_check = _utcnow_dt()

    # ── FASE 8b: Auto-SMIC (W1-2) ────────────────────────────────

    def _find_smic_cli_path(self) -> Optional[Path]:
        """Locate smic_cli.py relative to the repository root."""
        candidates = [
            Path(__file__).resolve().parents[3] / "workers" / "smic" / "python" / "smic_core" / "md_analisys" / "smic_cli.py",
            Path("workers/smic/python/smic_core/md_analisys/smic_cli.py"),
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    async def _fase_8b_auto_smic(self) -> None:
        """Run SMIC analysis bundle on all replicas after download (W1-2)."""
        analyses = list(self.cfg.auto_smic_analyses)
        if not analyses:
            return
        self._emit("smic", f"Starting auto-SMIC analyses: {analyses}")
        smic_cli = self._find_smic_cli_path()
        if not smic_cli:
            self._emit("smic", "WARN: smic_cli.py not found, skipping auto-SMIC")
            return

        stride = getattr(self.cfg, 'auto_smic_stride', 10)
        timeout = getattr(self.cfg, 'auto_smic_timeout', 1800)

        for replica_id in range(1, self.cfg.n_replicas + 1):
            local_dir = os.path.join(self.state.local_output_dir, f"replica_{replica_id}")
            topology = os.path.join(local_dir, self._replica_prepared_pdb_filename(replica_id))
            trajectory = os.path.join(local_dir, f"{self._replica_run_name(replica_id)}.dcd")
            output = os.path.join(local_dir, "smic")

            if not os.path.isfile(topology) or not os.path.isfile(trajectory):
                self._emit("smic", f"WARN: replica {replica_id} missing topology or trajectory, skipping")
                continue

            cmd = [
                sys.executable, "-u", str(smic_cli),
                "run", "--analysis",
            ] + analyses + [
                "--topology", topology,
                "--trajectory", trajectory,
                "--output-root", output,
                "--label", f"auto_r{replica_id}",
                "--stride", str(stride),
            ]
            self._emit("smic", f"Running auto-SMIC for replica {replica_id}: {' '.join(cmd[-6:])}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                rc = proc.returncode or 0
                if rc != 0:
                    self._emit("smic", f"WARN: auto-SMIC replica {replica_id} exited with code {rc}")
                    logger.warning("Auto-SMIC replica %d failed (rc=%d): %s", replica_id, rc, stderr.decode()[:500])
                else:
                    self._emit("smic", f"Auto-SMIC replica {replica_id} completed successfully")
                    # W1-3: Archive SMIC results to GCS
                    if self._storage_enabled():
                        smic_remote = f"{self._storage_remote_root()}/smic/replica_{replica_id}"
                        try:
                            # Use rclone copy from local to remote (local rclone)
                            env_vars = dict(os.environ)
                            env_vars.update(self.cfg.storage_env)
                            rclone_cmd = [
                                "rclone", "copy", output, smic_remote,
                                "--checkers", "4", "--transfers", "2",
                            ]
                            rclone_proc = await asyncio.create_subprocess_exec(
                                *rclone_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                env=env_vars,
                            )
                            r_out, r_err = await asyncio.wait_for(rclone_proc.communicate(), timeout=300)
                            if rclone_proc.returncode == 0:
                                self._emit("smic", f"Archived SMIC results for replica {replica_id} to GCS")
                            else:
                                self._emit("smic", f"WARN: GCS archival failed for replica {replica_id}: {r_err.decode()[:300]}")
                        except Exception as gcs_exc:
                            self._emit("smic", f"WARN: GCS archival failed for replica {replica_id}: {gcs_exc}")
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                self._emit("smic", f"WARN: auto-SMIC replica {replica_id} timed out after {timeout}s")

    # ── FASE 9: Destroy ──────────────────────────────────────────

    async def _cleanup_credentials(self):
        """Wipe rclone/GCS credentials and shell history before destroy."""
        cleanup_cmds = [
            # Unset all RCLONE_CONFIG_* env vars from the shell
            "unset $(env | grep -oP '^RCLONE_CONFIG_[^=]+') 2>/dev/null || true",
            # Remove any rclone config files that may have leaked
            "rm -f ~/.config/rclone/rclone.conf /tmp/rclone.conf 2>/dev/null || true",
            # Unset GCS service account vars
            "unset GOOGLE_APPLICATION_CREDENTIALS 2>/dev/null || true",
            # Clear shell history
            "history -c 2>/dev/null; rm -f ~/.bash_history ~/.ash_history 2>/dev/null || true",
            # Overwrite /proc/self/environ is not possible, but clear env files
            "rm -f /tmp/.env /workspace/.env 2>/dev/null || true",
        ]
        combined = " && ".join(cleanup_cmds)
        try:
            await self._ssh(combined, protocol=CommandProtocol.FAIL_FAST, timeout=30)
            self._emit("security", "Credentials cleaned before destroy")
        except Exception as e:
            self._emit("security", f"WARNING: credential cleanup failed: {e}")

    async def _collect_teardown_proof(self):
        """W3-3: Request a structured teardown proof from the Pod API.

        Collects PodTeardownProof (Invariant 4 from MICA_COMPUTE_POD_API_SPEC)
        before the instance is destroyed.  Non-fatal: if the Pod API is
        unreachable the destroy proceeds with SSH-only cleanup.
        """
        if not self.state.pod_api_available or not self.state.pod_api_url:
            return
        run_id = getattr(self.state, "_pod_run_id", None)
        if not run_id:
            return
        client = _PodAPIClient(self.state.pod_api_url, timeout=30)
        try:
            proof = await client.teardown(run_id)
            self._emit(
                "destroy",
                f"Pod API teardown proof collected: "
                f"run={proof.get('run_id')}, "
                f"terminated_at={proof.get('terminated_at')}, "
                f"residual_artifacts={len(proof.get('residual_artifacts', []))}",
            )
            # Persist the proof alongside economic ledger
            proof_path = os.path.join(self.cfg.local_output_dir, "teardown_proof.json")
            os.makedirs(os.path.dirname(proof_path), exist_ok=True)
            with open(proof_path, "w", encoding="utf-8") as f:
                json.dump(proof, f, indent=2, default=str)
            self._emit("destroy", f"Teardown proof saved to {proof_path}")
            self._collected_teardown_proof = proof
        except Exception as e:
            self._emit("destroy", f"Pod API teardown proof failed (non-fatal): {e}")

    async def _destroy_with_timeout_and_retry(
        self, instance_id: str, attempt_num: int = 0, attempt_total: int = 0
    ) -> bool:
        """
        Destroy instance with 120-second timeout and bounded retry.
        Returns True if destroy succeeded, False if timeout/permanent failure.
        Emits destroy_ack receipt with timestamp and outcome.
        """
        destroy_timeout_sec = 120
        max_destroy_attempts = 2
        retry_backoff_sec = 5

        for destroy_attempt in range(1, max_destroy_attempts + 1):
            try:
                self._emit(
                    "destroy",
                    f"Starting destroy attempt {destroy_attempt}/{max_destroy_attempts} "
                    f"for instance {instance_id} [timeout: {destroy_timeout_sec}s]",
                    machine_attempt=attempt_num,
                    instance_id=instance_id,
                    destroy_attempt=destroy_attempt,
                )
                async with asyncio.timeout(destroy_timeout_sec):
                    success = await self.provider.destroy_instance(instance_id)
                    if success:
                        self._emit(
                            "destroy",
                            f"Destroy succeeded on attempt {destroy_attempt}/{max_destroy_attempts}",
                            instance_id=instance_id,
                            machine_attempt=attempt_num,
                            destroy_attempt=destroy_attempt,
                            destroy_ack=True,
                            timestamp_iso=_utcnow_dt().isoformat(),
                        )
                        return True
                    else:
                        self._emit(
                            "destroy",
                            f"Destroy returned False on attempt {destroy_attempt}/{max_destroy_attempts}",
                            instance_id=instance_id,
                            machine_attempt=attempt_num,
                            destroy_attempt=destroy_attempt,
                        )
            except asyncio.TimeoutError:
                self._emit(
                    "destroy",
                    f"Destroy timeout ({destroy_timeout_sec}s) on attempt {destroy_attempt}/{max_destroy_attempts}",
                    instance_id=instance_id,
                    machine_attempt=attempt_num,
                    destroy_attempt=destroy_attempt,
                    destroy_timeout_ack=True,
                    timestamp_iso=_utcnow_dt().isoformat(),
                )
                if destroy_attempt < max_destroy_attempts:
                    self._emit(
                        "destroy",
                        f"Waiting {retry_backoff_sec}s before retry...",
                        instance_id=instance_id,
                    )
                    await asyncio.sleep(retry_backoff_sec)
            except Exception as destroy_exc:
                error_type = type(destroy_exc).__name__
                self._emit(
                    "destroy",
                    f"Destroy exception on attempt {destroy_attempt}/{max_destroy_attempts}: {error_type}: {destroy_exc}",
                    instance_id=instance_id,
                    machine_attempt=attempt_num,
                    destroy_attempt=destroy_attempt,
                    destroy_error_type=error_type,
                )
                if destroy_attempt < max_destroy_attempts:
                    self._emit(
                        "destroy",
                        f"Waiting {retry_backoff_sec}s before retry...",
                        instance_id=instance_id,
                    )
                    await asyncio.sleep(retry_backoff_sec)

        self._emit(
            "destroy",
            f"Destroy failed after {max_destroy_attempts} attempts for instance {instance_id}",
            instance_id=instance_id,
            machine_attempt=attempt_num,
            destroy_final_ack=False,
            timestamp_iso=_utcnow_dt().isoformat(),
        )
        return False

    async def _destroy(self) -> bool:
        self.state.phase = OrchestratorPhase.DESTROY
        self.state.destroy_attempted = bool(self.state.instance_id)
        self.state.destroy_succeeded = False
        self.state.teardown_unconfirmed = False
        self.state.teardown_failure_reason = ""
        # Kill SSH tunnel before destroying the instance
        if self._tunnel_proc is not None:
            try:
                self._tunnel_proc.kill()
                self._emit("destroy", "SSH tunnel closed")
            except Exception:
                pass
            self._tunnel_proc = None
        if self.state.instance_id:
            if self._storage_enabled():
                await self._sync_storage_artifacts("pre_destroy")
                if not await self._verify_storage_durability():
                    raise RuntimeError("Refusing to destroy instance before storage durability confirmation")
            # W3-3: Collect teardown proof from Pod API before destroying
            await self._collect_teardown_proof()
            # W0-1: Wipe credentials before pod destruction
            await self._cleanup_credentials()
            success = await self._destroy_with_timeout_and_retry(self.state.instance_id)
            if success:
                self.state.destroy_succeeded = True
                self._emit("destroy", f"Instance {self.state.instance_id} destroyed successfully")
            else:
                self.state.teardown_unconfirmed = True
                self.state.teardown_failure_reason = (
                    f"Failed to destroy {self.state.instance_id} after bounded retry"
                )
                self._emit("destroy", f"WARNING: {self.state.teardown_failure_reason}")
            return success
        return True

    # ── Serialization ────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state for persistence / reporting."""
        payload = self.state.to_dict()
        payload.update({
            "ssh": f"{self.state.ssh_host}:{self.state.ssh_port}",
            "preserve_instance_on_failure": self.cfg.preserve_instance_on_failure,
            "preserve_instance_on_stop": self.cfg.preserve_instance_on_stop,
            "storage_backend": self.cfg.storage_backend.value,
            "storage_remote_root": self._storage_remote_root() if self._storage_enabled() else "",
            "recovery": {
                "instance_preserved": bool(
                    (self.cfg.preserve_instance_on_failure and self.state.phase == OrchestratorPhase.FAILED) or
                    (getattr(self, "_stop_requested", False) and self.cfg.preserve_instance_on_stop)
                ) and bool(self.state.instance_id),
                "instance_id": self.state.instance_id,
                "ssh_host": self.state.ssh_host,
                "ssh_port": self.state.ssh_port,
                "ssh_key_path": self.cfg.ssh_key_path,
                "run_dir": self.state.run_dir,
            },
            "artifact_manifest_path": self.state.latest_job_manifest_path,
            "resume_spec_path": self.state.latest_resume_spec_path,
            "teardown_proof": getattr(self, "_collected_teardown_proof", []),
            "teardown_proof_path": (
                _path_if_exists(os.path.join(self.cfg.local_output_dir, "teardown_proof.json"))
            ),
            "economic_ledger_path": (
                _path_if_exists(os.path.join(self.cfg.local_output_dir, "economic_ledger.json"))
            ),
        })
        return payload

    def save_report(self, path: str):
        """Save a JSON report of the orchestration."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        self._emit("report", f"Report saved to {path}")


# ────────────────────────────────────────────────────────────────
# Convenience launcher
# ────────────────────────────────────────────────────────────────

async def run_autonomous_md(
    pdb_path: str,
    steps: int = 75_000_000,
    n_replicas: int = 1,
    max_price: float = 0.50,
    max_cost: float = 10.0,
    gpu_type: GPUType = GPUType.RTX_5080,
    ssh_key: str = "",
    simulation_mode: str | SimulationMode = SimulationMode.BINDING,
    production_ns: float = 100.0,
    on_event: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
    cloud_orchestrator: Optional[Any] = None,
    **kwargs,
) -> OrchestratorState:
    """
    One-liner to launch an autonomous MD simulation.
    
    Args:
        simulation_mode: "binding" or "complex" (or SimulationMode enum).
            * binding — spontaneous binding with flat-bottom restraints
            * complex — publication-grade equilibrium MD (min→NVT→NPT→production)
        production_ns: Production time in ns (only used in complex mode).
        cloud_orchestrator: Optional CloudOrchestrator for multi-provider
            GPU cascade provisioning.
    
    Example:
        # Binding mode (default)
        state = await run_autonomous_md(pdb_path="complex.pdb", steps=75_000_000)

        # Complex mode
        state = await run_autonomous_md(
            pdb_path="complex.pdb",
            simulation_mode="complex",
            production_ns=200,
        )
    """
    if isinstance(simulation_mode, str):
        simulation_mode = SimulationMode(simulation_mode)

    config = MDJobConfig(
        pdb_path=pdb_path,
        steps=steps,
        production_ns=production_ns,
        n_replicas=n_replicas,
        max_price_per_hour=max_price,
        max_total_cost_usd=max_cost,
        gpu_type=gpu_type,
        ssh_key_path=ssh_key or os.path.expanduser("~/.ssh/vast_key"),
        simulation_mode=simulation_mode,
        **kwargs,
    )
    orch = VastMDOrchestrator(
        config,
        on_event=on_event,
        cloud_orchestrator=cloud_orchestrator,
    )
    return await orch.run()


async def resume_autonomous_md(
    resume_spec_path: str,
    ssh_key: str = "",
    on_event: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
    **overrides,
) -> OrchestratorState:
    """Resume an MD job from a saved `resume_spec.json` contract."""
    _cloud_orchestrator = overrides.pop("cloud_orchestrator", None)

    with open(resume_spec_path, "r", encoding="utf-8") as f:
        spec = ResumeSpec.from_dict(json.load(f))

    config_data = dict(spec.config)
    config_data.update(overrides)
    config_data.setdefault("pdb_path", spec.pdb_path)
    config_data.setdefault("simulation_script", spec.simulation_script)
    config_data.setdefault("simulation_script_sha256", spec.simulation_script_sha256)
    config_data.setdefault("extractor_script", spec.extractor_script)
    config_data.setdefault("extractor_script_sha256", spec.extractor_script_sha256)
    config_data.setdefault("simulation_mode", spec.simulation_mode)
    config_data.setdefault("steps", spec.target_steps)
    config_data.setdefault("production_ns", spec.target_production_ns)
    config_data.setdefault("storage_backend", spec.storage_backend)
    config_data.setdefault("resume_spec", spec)
    config_data.setdefault("resume_spec_path", resume_spec_path)
    config_data.setdefault("ssh_key_path", ssh_key or os.path.expanduser("~/.ssh/vast_key"))

    config = MDJobConfig(**config_data)
    orch = VastMDOrchestrator(
        config,
        on_event=on_event,
        cloud_orchestrator=_cloud_orchestrator,
    )
    orch.state.stop_reason = "resume"
    return await orch.run()


# ────────────────────────────────────────────────────────────────
# Provider-neutral alias (W1-2)
# ────────────────────────────────────────────────────────────────
MDOrchestrator = VastMDOrchestrator
"""Backward-compatible alias.  New call-sites should use MDOrchestrator
to decouple from the Vast.ai naming; VastMDOrchestrator remains valid."""
