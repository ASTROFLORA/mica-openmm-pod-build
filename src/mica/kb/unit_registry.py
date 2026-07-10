"""
KB Unit Registry — K6-1 (KB Slice 4)

Versioned unit registry with shadow dual-write, idempotent backfill,
and cutover gate. Never destructive migration.

Key objects:
- UnitRegistryVersion: versioned registry record
- QuantBackfillJob: idempotent backfill by scope
- BucketDriftReport: drift detection before cutover
- QuetzalGate: blocks cutover if drift is critical
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class RegistryStatus(str, Enum):
    DRAFT = "draft"
    SHADOW = "shadow"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


@dataclass
class UnitRegistryVersion:
    """K6-1: Versioned unit registry record."""
    version_id: str
    status: RegistryStatus = RegistryStatus.DRAFT
    unit_mappings: Dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    activated_at: Optional[datetime] = None
    deprecated_at: Optional[datetime] = None
    receipt_ref: Optional[str] = None


@dataclass
class QuantBackfillJob:
    """K6-1: Idempotent backfill job by scope."""
    job_ref: str
    scope_ref: str
    source_version: str
    target_version: str
    idempotency_key: str  # sha256(quant_ref + target_version + raw_hash + bucket_policy)
    total_claims: int = 0
    processed: int = 0
    bucket_drift_count: int = 0
    status: str = "pending"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    receipt_ref: Optional[str] = None


@dataclass
class BucketDriftReport:
    """K6-1: Bucket drift detection before cutover."""
    report_ref: str
    scope_ref: str
    total_claims: int = 0
    drift_count: int = 0
    drift_pct: float = 0.0
    critical_drift: bool = False  # True if >5% drift
    drift_details: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CutoverReceipt:
    """K6-1: Receipt for alias cutover."""
    receipt_ref: str
    source_version: str
    target_version: str
    scope_ref: str
    drift_report_ref: str
    approved: bool = False
    blocked: bool = False
    block_reason: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ponytail: 5% is the critical drift threshold per K6-1 spec
_CRITICAL_DRIFT_PCT = 5.0


class UnitRegistryManager:
    """K6-1: Versioned unit registry with shadow dual-write + cutover gate.

    Flow: import draft → validate golden → shadow dual-write (serves old) →
    backfill incremental → bucket drift report → Quetzal gate blocks cutover
    if critical drift → switch alias.

    Red-line: No migration without versioned registry.
    """

    def __init__(self) -> None:
        self._versions: Dict[str, UnitRegistryVersion] = {}
        self._active_version: Optional[str] = None
        self._shadow_version: Optional[str] = None
        self._backfill_jobs: Dict[str, QuantBackfillJob] = {}
        self._on_version_change: Optional[Callable[[str, str], None]] = None

    def create_version(self, version_id: str, unit_mappings: Dict[str, str]) -> UnitRegistryVersion:
        version = UnitRegistryVersion(
            version_id=version_id,
            status=RegistryStatus.DRAFT,
            unit_mappings=unit_mappings,
        )
        self._versions[version_id] = version
        return version

    def promote_to_shadow(self, version_id: str) -> UnitRegistryVersion | None:
        version = self._versions.get(version_id)
        if not version or version.status != RegistryStatus.DRAFT:
            return None
        # demote previous shadow
        if self._shadow_version:
            prev = self._versions.get(self._shadow_version)
            if prev and prev.status == RegistryStatus.SHADOW:
                prev.status = RegistryStatus.DRAFT
        version.status = RegistryStatus.SHADOW
        self._shadow_version = version_id
        return version

    def promote_to_active(self, version_id: str) -> UnitRegistryVersion | None:
        version = self._versions.get(version_id)
        if not version or version.status not in (RegistryStatus.DRAFT, RegistryStatus.SHADOW):
            return None
        # demote previous active
        if self._active_version:
            prev = self._versions.get(self._active_version)
            if prev and prev.status == RegistryStatus.ACTIVE:
                prev.status = RegistryStatus.DEPRECATED
                prev.deprecated_at = datetime.now(timezone.utc)
        version.status = RegistryStatus.ACTIVE
        version.activated_at = datetime.now(timezone.utc)
        self._active_version = version_id
        return version

    def get_version(self, version_id: str) -> Optional[UnitRegistryVersion]:
        return self._versions.get(version_id)

    def get_active(self) -> Optional[UnitRegistryVersion]:
        if self._active_version:
            return self._versions.get(self._active_version)
        return None

    def get_shadow(self) -> Optional[UnitRegistryVersion]:
        if self._shadow_version:
            return self._versions.get(self._shadow_version)
        return None

    def dual_read(self, raw_unit: str) -> Tuple[Optional[str], Optional[str]]:
        """Read from both active and shadow for shadow dual-write serving."""
        active = self.get_active()
        shadow = self.get_shadow()
        active_result = active.unit_mappings.get(raw_unit) if active else None
        shadow_result = shadow.unit_mappings.get(raw_unit) if shadow else None
        return active_result, shadow_result

    def submit_backfill(
        self,
        scope_ref: str,
        source_version: str,
        target_version: str,
        idempotency_key: Optional[str] = None,
    ) -> QuantBackfillJob:
        """Submit an idempotent backfill job."""
        if not idempotency_key:
            idempotency_key = hashlib.sha256(
                f"{scope_ref}:{source_version}:{target_version}".encode()
            ).hexdigest()
        # idempotent: if same key exists, return existing
        for job in self._backfill_jobs.values():
            if job.idempotency_key == idempotency_key:
                return job
        job = QuantBackfillJob(
            job_ref=f"backfill://{scope_ref}/{target_version}",
            scope_ref=scope_ref,
            source_version=source_version,
            target_version=target_version,
            idempotency_key=f"sha256:{idempotency_key}",
        )
        self._backfill_jobs[job.job_ref] = job
        return job

    def complete_backfill(self, job_ref: str, processed: int, drift_count: int) -> QuantBackfillJob | None:
        job = self._backfill_jobs.get(job_ref)
        if job:
            job.processed = processed
            job.bucket_drift_count = drift_count
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
        return job

    def generate_drift_report(
        self, scope_ref: str, total_claims: int, drift_count: int,
    ) -> BucketDriftReport:
        """Generate bucket drift report. Critical if >5% drift."""
        drift_pct = (drift_count / total_claims * 100) if total_claims > 0 else 0.0
        return BucketDriftReport(
            report_ref=f"drift://{scope_ref}/{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            scope_ref=scope_ref,
            total_claims=total_claims,
            drift_count=drift_count,
            drift_pct=drift_pct,
            critical_drift=drift_pct > _CRITICAL_DRIFT_PCT,
        )

    def cutover_gate(
        self,
        source_version: str,
        target_version: str,
        scope_ref: str,
        drift_report: BucketDriftReport,
    ) -> CutoverReceipt:
        """Quetzal gate: blocks cutover if critical drift."""
        receipt_ref = f"receipt://cutover/{scope_ref}/{datetime.now(timezone.utc).isoformat()}"
        if drift_report.critical_drift:
            return CutoverReceipt(
                receipt_ref=receipt_ref,
                source_version=source_version,
                target_version=target_version,
                scope_ref=scope_ref,
                drift_report_ref=drift_report.report_ref,
                approved=False,
                blocked=True,
                block_reason=f"critical_drift={drift_report.drift_pct:.1f}%>{_CRITICAL_DRIFT_PCT}%",
            )
        return CutoverReceipt(
            receipt_ref=receipt_ref,
            source_version=source_version,
            target_version=target_version,
            scope_ref=scope_ref,
            drift_report_ref=drift_report.report_ref,
            approved=True,
        )

    def list_backfill_jobs(self, scope_ref: Optional[str] = None) -> List[QuantBackfillJob]:
        jobs = list(self._backfill_jobs.values())
        if scope_ref:
            jobs = [j for j in jobs if j.scope_ref == scope_ref]
        return jobs
