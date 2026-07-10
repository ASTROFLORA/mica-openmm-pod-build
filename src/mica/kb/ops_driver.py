"""
KB Ops Driver — K6-13 (KB Slice 4)

Driver CLI for kb.job/kb.slo/kb.migration operations.
Dispatches to KBOperationsControlPlane and UnitRegistryManager.
Receipted operations with real backend surface.

Key objects:
- KBOpsDispatcher: dispatches CLI commands
- JobSubmitReceipt: receipt for job submission
- SLOReportReceipt: receipt for SLO report
- BackfillReceipt: receipt for backfill operation
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .operations_control_plane import (
    KBOperationsControlPlane,
    JobRunRecord,
    JobStatus,
    SLODefinition,
    SLOStatus,
)
from .unit_registry import UnitRegistryManager, QuantBackfillJob


class OpsCommand(str, Enum):
    JOB_SUBMIT = "kb.job.submit"
    JOB_STATUS = "kb.job.status"
    JOB_CANCEL = "kb.job.cancel"
    SLO_REPORT = "kb.slo.report"
    MIGRATION_BACKFILL = "kb.migration.backfill"


@dataclass
class JobSubmitReceipt:
    """Receipt for job submission via CLI."""
    receipt_ref: str
    job_ref: str
    status: str
    idempotency_key: str
    scope_ref: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SLOReportReceipt:
    """Receipt for SLO report."""
    receipt_ref: str
    scope: Optional[str]
    slo_count: int
    incidents_active: int
    report: Dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BackfillReceipt:
    """Receipt for backfill operation."""
    receipt_ref: str
    job_ref: str
    scope_ref: str
    target_version: str
    status: str
    idempotency_key: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class KBOpsDispatcher:
    """K6-13: Driver CLI for kb.job/kb.slo/kb.migration.

    Commands:
    - kb job submit --kind <kind> --scope <scope> --transport ws
    - kb job status <job_ref>
    - kb job cancel <job_ref>
    - kb slo --scope <scope>
    - kb migration backfill --scope <scope> --target-version <ver>

    Invoked via: python tools/mica_agent.py kb job submit ...
    """

    def __init__(
        self,
        control_plane: Optional[KBOperationsControlPlane] = None,
        unit_registry: Optional[UnitRegistryManager] = None,
    ) -> None:
        self._cp = control_plane or KBOperationsControlPlane()
        self._ur = unit_registry or UnitRegistryManager()

    def dispatch(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a CLI command. Returns receipt dict."""
        if command == OpsCommand.JOB_SUBMIT:
            return self._job_submit(args)
        elif command == OpsCommand.JOB_STATUS:
            return self._job_status(args)
        elif command == OpsCommand.JOB_CANCEL:
            return self._job_cancel(args)
        elif command == OpsCommand.SLO_REPORT:
            return self._slo_report(args)
        elif command == OpsCommand.MIGRATION_BACKFILL:
            return self._migration_backfill(args)
        else:
            return {"error": f"unknown command: {command}"}

    def _job_submit(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a KB job. Requires scope, idempotency_key."""
        scope_ref = args.get("scope_ref", "")
        job_kind = args.get("job_kind", "")
        idempotency_key = args.get("idempotency_key", "")
        budget_ref = args.get("budget_ref")

        if not scope_ref:
            return {"error": "scope_ref is required", "status": 400}
        if not idempotency_key:
            return {"error": "idempotency_key is required", "status": 400}
        if not job_kind:
            return {"error": "job_kind is required", "status": 400}

        record = self._cp.submit_job(
            job_kind=job_kind,
            scope_ref=scope_ref,
            idempotency_key=idempotency_key,
            budget_ref=budget_ref,
        )

        receipt = JobSubmitReceipt(
            receipt_ref=f"receipt://job_submitted/{datetime.now(timezone.utc).isoformat()}",
            job_ref=record.job_ref,
            status=record.status.value,
            idempotency_key=idempotency_key,
            scope_ref=scope_ref,
        )

        return {
            "job_ref": record.job_ref,
            "status": record.status.value,
            "idempotency_key": idempotency_key,
            "scope_ref": scope_ref,
            "budget_ref": budget_ref,
            "created_by_receipt_ref": receipt.receipt_ref,
        }

    def _job_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get job status."""
        job_ref = args.get("job_ref", "")
        if not job_ref:
            return {"error": "job_ref is required", "status": 400}

        record = self._cp.get_job(job_ref)
        if not record:
            return {"error": f"job not found: {job_ref}", "status": 404}

        return {
            "job_ref": record.job_ref,
            "status": record.status.value,
            "shards_total": record.shards_total,
            "shards_done": record.shards_done,
            "idempotency_key": record.idempotency_key or "",
            "budget_ref": record.budget_ref or "",
            "error_message": record.error_message or "",
        }

    def _job_cancel(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Cancel a job."""
        job_ref = args.get("job_ref", "")
        if not job_ref:
            return {"error": "job_ref is required", "status": 400}

        record = self._cp.cancel_job(job_ref)
        if not record:
            return {"error": f"job not found: {job_ref}", "status": 404}

        return {
            "job_ref": record.job_ref,
            "status": record.status.value,
            "cancelled": True,
        }

    def _slo_report(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Generate SLO report."""
        scope = args.get("scope")
        report = self._cp.slo_report(scope=scope)

        receipt = SLOReportReceipt(
            receipt_ref=f"receipt://slo_report/{datetime.now(timezone.utc).isoformat()}",
            scope=scope,
            slo_count=report["slo_count"],
            incidents_active=report["incidents_active"],
            report=report,
        )

        return {
            "slo_count": report["slo_count"],
            "incidents_active": report["incidents_active"],
            "slos": report["slos"],
            "receipt_ref": receipt.receipt_ref,
        }

    def _migration_backfill(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a unit registry backfill."""
        scope_ref = args.get("scope_ref", "")
        target_version = args.get("target_version", "")
        source_version = args.get("source_version", "")
        idempotency_key = args.get("idempotency_key")

        if not scope_ref:
            return {"error": "scope_ref is required", "status": 400}
        if not target_version:
            return {"error": "target_version is required", "status": 400}

        job = self._ur.submit_backfill(
            scope_ref=scope_ref,
            source_version=source_version,
            target_version=target_version,
            idempotency_key=idempotency_key,
        )

        receipt = BackfillReceipt(
            receipt_ref=f"receipt://backfill/{datetime.now(timezone.utc).isoformat()}",
            job_ref=job.job_ref,
            scope_ref=scope_ref,
            target_version=target_version,
            status=job.status,
            idempotency_key=job.idempotency_key,
        )

        return {
            "job_ref": job.job_ref,
            "scope_ref": scope_ref,
            "target_version": target_version,
            "idempotency_key": job.idempotency_key,
            "receipt_ref": receipt.receipt_ref,
        }

    def get_control_plane(self) -> KBOperationsControlPlane:
        return self._cp

    def get_unit_registry(self) -> UnitRegistryManager:
        return self._ur
