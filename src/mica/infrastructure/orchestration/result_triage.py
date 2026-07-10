"""
result_triage.py — Batch MD Output Classifier (SP-21)

Classifies large-batch GCS outputs from Salad SRCG OpenMM runs into
discrete outcome categories for automated post-batch review.

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │                      ResultTriager                            │
    ├──────────────────────────────────────────────────────────────┤
    │  triage_job(job_id, output_gcs_prefix)                       │
    │    → inspect GCS for: completed.marker, DCD files, log files │
    │    → classify: COMPLETED | INCOMPLETE | FAILED | STALE | EMPTY│
    │    → return TriageResult with metrics and evidence            │
    │                                                               │
    │  triage_batch(jobs)                                           │
    │    → parallel triage across all jobs                         │
    │    → return BatchTriageReport with summary + per-job results  │
    └──────────────────────────────────────────────────────────────┘

Classification logic:
    COMPLETED   — completed.marker present + at least one DCD file
    INCOMPLETE  — DCD files present but no completed.marker (in-progress or interrupted)
    FAILED      — no DCD files, no completed.marker, no checkpoint
    STALE       — checkpoint present but mtime older than stale_threshold; no DCD
    EMPTY       — no GCS objects at all under output_gcs_prefix

GCS access strategy:
    - Uses google-cloud-storage Python client if GOOGLE_APPLICATION_CREDENTIALS
      or GCS_CREDENTIALS_JSON_B64 env var is available.
    - Falls back to a mock/stub in environments without GCS credentials
      (for unit testing and dry-run batch audits).

Usage:
    triager = ResultTriager(gcs_client=gcs_client)
    result = await triager.triage_job("job-abc", "gs://mica-compute/salad-jobs/job-abc/")
    report = await triager.triage_batch(job_map)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TriageStatus(Enum):
    COMPLETED  = "completed"    # completed.marker + ≥1 DCD
    INCOMPLETE = "incomplete"   # ≥1 DCD but no completed.marker
    FAILED     = "failed"       # no DCD, no marker, no checkpoint
    STALE      = "stale"        # checkpoint found but old, no DCD
    EMPTY      = "empty"        # no objects found under prefix


# ---------------------------------------------------------------------------
# Per-job result
# ---------------------------------------------------------------------------

@dataclass
class TriageResult:
    """
    Classification result for a single MD job output.
    """
    job_id: str
    output_gcs_prefix: str
    status: TriageStatus
    # Evidence metrics
    dcd_count: int = 0
    log_count: int = 0
    has_checkpoint: bool = False
    has_completed_marker: bool = False
    last_checkpoint_age_seconds: Optional[float] = None   # None if no checkpoint
    estimated_frames: int = 0                              # rough: dcd_count * frames_per_dcd
    total_objects_found: int = 0
    # Diagnostics
    error_hint: Optional[str] = None
    evidence: List[str] = field(default_factory=list)     # list of found GCS object keys


# ---------------------------------------------------------------------------
# Batch report
# ---------------------------------------------------------------------------

@dataclass
class BatchTriageReport:
    """
    Summary of a full batch triage run.
    """
    triaged_at: str            # ISO-8601 UTC
    total_jobs: int
    completed_count: int
    incomplete_count: int
    failed_count: int
    stale_count: int
    empty_count: int
    success_rate_pct: float    # completed / total * 100
    results: List[TriageResult]
    residual_risks: List[str]


# ---------------------------------------------------------------------------
# GCS inspector
# ---------------------------------------------------------------------------

class GCSInspector:
    """
    Thin wrapper around google-cloud-storage for listing objects under a prefix.

    In environments without GCS credentials, returns empty results (graceful degradation).
    Supports injection of a mock client for unit tests.
    """

    def __init__(self, gcs_client: Optional[Any] = None):
        """
        Parameters
        ----------
        gcs_client : google.cloud.storage.Client or compatible mock
            If None, attempts to auto-initialize from env vars:
              - GOOGLE_APPLICATION_CREDENTIALS (path to SA JSON)
              - GCS_CREDENTIALS_JSON_B64 (base64-encoded SA JSON)
        """
        self._client = gcs_client or self._auto_init()

    def _auto_init(self) -> Optional[Any]:
        """Try to initialize a real GCS client from environment."""
        try:
            from google.cloud import storage as gcs_storage
            # Try GCS_CREDENTIALS_JSON_B64 first
            creds_b64 = os.environ.get("GCS_CREDENTIALS_JSON_B64", "")
            if creds_b64:
                sa_json = base64.b64decode(creds_b64).decode("utf-8")
                sa_info = json.loads(sa_json)
                from google.oauth2 import service_account
                creds = service_account.Credentials.from_service_account_info(sa_info)
                return gcs_storage.Client(credentials=creds, project=sa_info.get("project_id"))
            # Try GOOGLE_APPLICATION_CREDENTIALS (file path)
            if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                return gcs_storage.Client()
            return None
        except Exception as exc:
            logger.debug("GCSInspector: could not init GCS client: %s", exc)
            return None

    def is_available(self) -> bool:
        return self._client is not None

    async def list_prefix(self, bucket: str, prefix: str) -> List[Dict[str, Any]]:
        """
        List all objects under gs://bucket/prefix.

        Returns list of dicts: {"name": str, "size": int, "updated": datetime or None}
        Returns empty list if client unavailable or on error.
        """
        if self._client is None:
            return []
        try:
            # google-cloud-storage is sync; run in executor
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._sync_list, bucket, prefix)
        except Exception as exc:
            logger.warning("GCSInspector.list_prefix(%s/%s): %s", bucket, prefix, exc)
            return []

    def _sync_list(self, bucket: str, prefix: str) -> List[Dict[str, Any]]:
        blobs = self._client.list_blobs(bucket, prefix=prefix)
        result = []
        for blob in blobs:
            result.append({
                "name": blob.name,
                "size": blob.size or 0,
                "updated": blob.updated,   # datetime or None
            })
        return result


# ---------------------------------------------------------------------------
# ResultTriager
# ---------------------------------------------------------------------------

class ResultTriager:
    """
    Classify batch GCS outputs for post-run analysis.

    Parameters
    ----------
    gcs_inspector : GCSInspector or None
        If None, auto-initializes from environment.
    stale_threshold_seconds : float
        A checkpoint older than this (with no DCD) is classified STALE.
        Default: 7200 (2 hours).
    frames_per_dcd : int
        Estimated simulation frames per DCD file (for reporting only).
        Default: 500 (report_freq default).
    """

    DEFAULT_STALE_THRESHOLD = 7200.0
    DEFAULT_FRAMES_PER_DCD  = 500

    def __init__(
        self,
        gcs_inspector: Optional[GCSInspector] = None,
        stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD,
        frames_per_dcd: int = DEFAULT_FRAMES_PER_DCD,
    ):
        self._inspector = gcs_inspector or GCSInspector()
        self._stale_threshold = stale_threshold_seconds
        self._frames_per_dcd = frames_per_dcd

    # ------------------------------------------------------------------
    # Public: triage_job
    # ------------------------------------------------------------------

    async def triage_job(self, job_id: str, output_gcs_prefix: str) -> TriageResult:
        """
        Inspect GCS output prefix and classify the job outcome.

        Parameters
        ----------
        job_id : str
            Identifier for this job (used in TriageResult and logging).
        output_gcs_prefix : str
            GCS URI prefix for this job's outputs, e.g.
            "gs://mica-compute/salad-jobs/job-abc/"

        Returns
        -------
        TriageResult with status, evidence metrics, and error_hint.
        """
        bucket, prefix = _parse_gcs_uri(output_gcs_prefix)
        prefix = prefix.rstrip("/") + "/"

        logger.debug("ResultTriager: triaging job %s at gs://%s/%s", job_id, bucket, prefix)

        objects = await self._inspector.list_prefix(bucket, prefix)

        if not objects:
            return TriageResult(
                job_id=job_id,
                output_gcs_prefix=output_gcs_prefix,
                status=TriageStatus.EMPTY,
                error_hint="No objects found at GCS prefix — job may not have started or GCS unavailable",
            )

        # Classify each object
        dcd_objects = [o for o in objects if _is_dcd(o["name"])]
        log_objects = [o for o in objects if _is_log(o["name"])]
        checkpoint_objects = [o for o in objects if _is_checkpoint(o["name"])]
        marker_objects = [o for o in objects if _is_marker(o["name"])]

        has_marker = len(marker_objects) > 0
        has_dcd = len(dcd_objects) > 0
        has_checkpoint = len(checkpoint_objects) > 0

        # Checkpoint age
        checkpoint_age: Optional[float] = None
        if has_checkpoint:
            now = datetime.now(timezone.utc)
            cpt = checkpoint_objects[0]
            updated = cpt.get("updated")
            if updated is not None:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                checkpoint_age = (now - updated).total_seconds()

        # Classify
        status, error_hint = self._classify(
            has_marker=has_marker,
            has_dcd=has_dcd,
            has_checkpoint=has_checkpoint,
            checkpoint_age=checkpoint_age,
        )

        evidence = [o["name"] for o in objects[:20]]  # cap evidence list

        return TriageResult(
            job_id=job_id,
            output_gcs_prefix=output_gcs_prefix,
            status=status,
            dcd_count=len(dcd_objects),
            log_count=len(log_objects),
            has_checkpoint=has_checkpoint,
            has_completed_marker=has_marker,
            last_checkpoint_age_seconds=checkpoint_age,
            estimated_frames=len(dcd_objects) * self._frames_per_dcd,
            total_objects_found=len(objects),
            error_hint=error_hint,
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # Public: triage_batch
    # ------------------------------------------------------------------

    async def triage_batch(
        self,
        jobs: Dict[str, str],
        *,
        concurrency: int = 8,
    ) -> BatchTriageReport:
        """
        Triage a batch of jobs concurrently.

        Parameters
        ----------
        jobs : dict[job_id, output_gcs_prefix]
        concurrency : int
            Max parallel GCS list calls (default: 8).

        Returns
        -------
        BatchTriageReport with summary counts and per-job TriageResult list.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        semaphore = asyncio.Semaphore(concurrency)

        async def _triage_one(job_id: str, prefix: str) -> TriageResult:
            async with semaphore:
                return await self.triage_job(job_id, prefix)

        tasks = [_triage_one(jid, pfx) for jid, pfx in jobs.items()]
        results: List[TriageResult] = await asyncio.gather(*tasks)

        counts = {s: 0 for s in TriageStatus}
        for r in results:
            counts[r.status] += 1

        total = len(results)
        completed_n = counts[TriageStatus.COMPLETED]
        success_rate = (completed_n / total * 100.0) if total > 0 else 0.0

        risks = self._batch_risks(counts, total, results)

        logger.info(
            "ResultTriager: batch triage complete — total=%d completed=%d incomplete=%d "
            "failed=%d stale=%d empty=%d success_rate=%.1f%%",
            total, completed_n, counts[TriageStatus.INCOMPLETE],
            counts[TriageStatus.FAILED], counts[TriageStatus.STALE],
            counts[TriageStatus.EMPTY], success_rate,
        )

        return BatchTriageReport(
            triaged_at=now_str,
            total_jobs=total,
            completed_count=completed_n,
            incomplete_count=counts[TriageStatus.INCOMPLETE],
            failed_count=counts[TriageStatus.FAILED],
            stale_count=counts[TriageStatus.STALE],
            empty_count=counts[TriageStatus.EMPTY],
            success_rate_pct=round(success_rate, 2),
            results=results,
            residual_risks=risks,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _classify(
        self,
        *,
        has_marker: bool,
        has_dcd: bool,
        has_checkpoint: bool,
        checkpoint_age: Optional[float],
    ) -> Tuple[TriageStatus, Optional[str]]:
        if has_marker and has_dcd:
            return TriageStatus.COMPLETED, None
        if has_marker and not has_dcd:
            return TriageStatus.INCOMPLETE, "Completion marker present but no DCD files — possible output upload failure"
        if has_dcd and not has_marker:
            return TriageStatus.INCOMPLETE, "DCD files found but no completion marker — job may still be running or was interrupted"
        if has_checkpoint:
            if checkpoint_age is not None and checkpoint_age > self._stale_threshold:
                return TriageStatus.STALE, (
                    f"Checkpoint found but {checkpoint_age/3600:.1f}h old with no DCD — "
                    "job likely crashed or GCS upload failed"
                )
            return TriageStatus.STALE, "Checkpoint found but no DCD or completion marker"
        return TriageStatus.FAILED, "No DCD, checkpoint, or completion marker found — job likely failed before first output"

    def _batch_risks(
        self,
        counts: Dict[TriageStatus, int],
        total: int,
        results: List[TriageResult],
    ) -> List[str]:
        risks: List[str] = []
        if total == 0:
            risks.append("Empty batch — no jobs to triage.")
            return risks
        failed_n = counts[TriageStatus.FAILED]
        empty_n = counts[TriageStatus.EMPTY]
        stale_n = counts[TriageStatus.STALE]
        if failed_n > 0:
            risks.append(f"{failed_n} job(s) FAILED with no output — check Salad CG logs for container errors.")
        if empty_n > 0:
            risks.append(f"{empty_n} job(s) EMPTY — GCS inspector may be unavailable or jobs never launched.")
        if stale_n > 0:
            risks.append(f"{stale_n} stale job(s) — checkpoints found but no completion. Consider re-queuing from checkpoint.")
        incomplete = counts[TriageStatus.INCOMPLETE]
        if incomplete > 0:
            risks.append(f"{incomplete} incomplete job(s) — may still be running or were interrupted. Re-check later.")
        if not risks:
            risks.append("All triaged jobs completed successfully.")
        return risks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_gcs_uri(uri: str) -> Tuple[str, str]:
    """Parse gs://bucket/object/path → (bucket, object_path)."""
    if not uri.startswith("gs://"):
        raise ValueError(f"Expected gs:// URI, got: {uri!r}")
    stripped = uri[5:]
    if "/" in stripped:
        bucket, obj = stripped.split("/", 1)
    else:
        bucket, obj = stripped, ""
    return bucket, obj


def _is_dcd(name: str) -> bool:
    n = name.lower()
    return n.endswith(".dcd") or "/dcd_" in n


def _is_log(name: str) -> bool:
    n = name.lower()
    return n.endswith(".txt") or n.endswith(".log") or "/log_" in n


def _is_checkpoint(name: str) -> bool:
    n = name.lower()
    return "checkpoint.cpt" in n or n.endswith(".cpt")


def _is_marker(name: str) -> bool:
    n = name.lower()
    return "completed.marker" in n or n.endswith(".marker")
