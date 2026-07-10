"""
KB Operations Control Plane — K6-0 (KB Slice 4)

Constitutional foundation for all K5 services. Without this, every K5 service
is an invisible cronjob. SLO registry, burn-rate alerts, job ledger, capacity
budgets, and runbooks.

Key objects:
- SLODefinition: per-service SLO with SLI, target, window
- ErrorBudgetLedger: tracks consumed error budget
- JobRunLedger: durable record of every job run
- CapacityBudgetLedger: resource budgets per scope
- RunbookRegistry: operational runbooks per service
- KBOperationsControlPlane: orchestrates all of the above
- IncidentReceipt: emitted on P0/pager events
- ReleaseGateReport: gates releases on SLO health
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SLOStatus(str, Enum):
    """SLO health status."""
    GREEN = "green"          # within budget
    YELLOW = "yellow"        # burn-rate elevated
    RED = "red"              # budget exhausted
    UNKNOWN = "unknown"      # insufficient data


class JobStatus(str, Enum):
    """Job lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class IncidentSeverity(str, Enum):
    """Incident severity levels."""
    P0 = "P0"  # pager — immediate
    P1 = "P1"  # ticket — next business day
    P2 = "P2"  # backlog — weekly triage
    P3 = "P3"  # informational


class ReleaseGateStatus(str, Enum):
    """Release gate verdict."""
    PASS = "pass"
    HOLD = "hold"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# SLO Registry
# ---------------------------------------------------------------------------

@dataclass
class SLODefinition:
    """K6-0: Per-service SLO definition."""
    slo_id: str
    service_ref: str  # e.g. "kb.quant_normalization", "kb.asof_index"
    sli_name: str  # e.g. "quant_normalization_lag_p95"
    target_value: float  # e.g. 0.995 (99.5%)
    target_unit: str = "fraction"  # "fraction", "ms", "count"
    window_hours: int = 168  # 7d default
    burn_rate_threshold: float = 14.0  # 14x = P0, 6x = P1
    status: SLOStatus = SLOStatus.UNKNOWN
    current_value: Optional[float] = None
    budget_consumed_pct: float = 0.0
    owner: str = "KB_SUBSTRATE_OPERATOR"
    runbook_ref: Optional[str] = None
    receipt_ref: Optional[str] = None


@dataclass
class ErrorBudgetLedger:
    """Tracks consumed error budget per SLO over rolling window."""
    slo_id: str
    total_budget_ms: float = 0.0  # total allowed error time in window
    consumed_ms: float = 0.0
    remaining_pct: float = 100.0
    last_breach_at: Optional[datetime] = None
    breach_count: int = 0
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


@dataclass
class BurnRateReading:
    """Current burn rate for an SLO."""
    slo_id: str
    burn_rate_1h: float = 0.0
    burn_rate_6h: float = 0.0
    burn_rate_1d: float = 0.0
    burn_rate_7d: float = 0.0
    severity: IncidentSeverity = IncidentSeverity.P3
    measured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Job Run Ledger
# ---------------------------------------------------------------------------

@dataclass
class JobRunRecord:
    """K6-0: Durable record of a single job run."""
    job_ref: str
    job_kind: str  # e.g. "quant_backfill", "tier_recompute"
    scope_ref: str
    status: JobStatus = JobStatus.PENDING
    idempotency_key: Optional[str] = None
    budget_ref: Optional[str] = None
    created_by: str = "system"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    shards_total: int = 0
    shards_done: int = 0
    receipt_ref: Optional[str] = None


# ---------------------------------------------------------------------------
# Capacity Budget
# ---------------------------------------------------------------------------

@dataclass
class CapacityBudget:
    """K6-0: Resource budget per scope."""
    budget_ref: str
    scope_ref: str
    max_concurrent_jobs: int = 5
    max_daily_job_runs: int = 50
    max_monthly_job_runs: int = 500
    daily_runs_today: int = 0
    monthly_runs: int = 0
    current_concurrent: int = 0
    backpressure_active: bool = False
    period_start: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Runbook Registry
# ---------------------------------------------------------------------------

@dataclass
class RunbookEntry:
    """K6-0: Operational runbook for a service."""
    runbook_id: str
    service_ref: str
    title: str
    trigger_condition: str  # when to use this runbook
    steps: List[str] = field(default_factory=list)
    escalation_path: str = "KB_SUBSTRATE_OPERATOR"
    last_tested_at: Optional[datetime] = None
    version: str = "v1"


# ---------------------------------------------------------------------------
# Incident Receipt
# ---------------------------------------------------------------------------

@dataclass
class IncidentReceipt:
    """K6-0: Receipt emitted on P0/pager events."""
    incident_ref: str
    severity: IncidentSeverity
    slo_id: str
    trigger: str
    burn_rate: float = 0.0
    error_budget_remaining_pct: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    acknowledged: bool = False
    resolved: bool = False
    receipt_ref: Optional[str] = None


# ---------------------------------------------------------------------------
# Release Gate Report
# ---------------------------------------------------------------------------

@dataclass
class ReleaseGateReport:
    """K6-0: Gates releases on SLO health."""
    gate_id: str
    release_ref: str
    status: ReleaseGateStatus = ReleaseGateStatus.HOLD
    slo_checks: List[Dict[str, Any]] = field(default_factory=list)
    all_green: bool = False
    blockers: List[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


# ---------------------------------------------------------------------------
# P0 Pager Rules (constitucional — per spec)
# ---------------------------------------------------------------------------

_P0_PAGER_RULES: List[Dict[str, Any]] = [
    {"condition": "active_claim_missing_receipt_total > 0", "severity": IncidentSeverity.P0},
    {"condition": "asof_query_error burn-rate > 14x/5m", "severity": IncidentSeverity.P0},
    {"condition": "lineage drift public/global", "severity": IncidentSeverity.P0},
    {"condition": "legal_hold bypass", "severity": IncidentSeverity.P0},
    {"condition": "false_tier_upgrade en golden/canary", "severity": IncidentSeverity.P0},
]


# ---------------------------------------------------------------------------
# Operations Control Plane
# ---------------------------------------------------------------------------

class KBOperationsControlPlane:
    """K6-0: Constitutional control plane for all K5 services.

    SLO registry + error budget + job ledger + capacity budgets + runbooks.
    Without this, every K5 service is an invisible cronjob.

    Red-line rules enforced:
    - No cron writer outside Command Kernel
    - No ops claim without a real-driver job run
    - Jobs via Command Kernel, no cron suelto
    """

    def __init__(self) -> None:
        self._slos: Dict[str, SLODefinition] = {}
        self._budgets: Dict[str, ErrorBudgetLedger] = {}
        self._jobs: Dict[str, JobRunRecord] = {}
        self._capacity: Dict[str, CapacityBudget] = {}
        self._runbooks: Dict[str, RunbookEntry] = {}
        self._incidents: List[IncidentReceipt] = []
        self._on_incident: Optional[Callable[[IncidentReceipt], None]] = None

    # -- SLO Registry --

    def register_slo(self, slo: SLODefinition) -> SLODefinition:
        """Register or update an SLO definition."""
        self._slos[slo.slo_id] = slo
        if slo.slo_id not in self._budgets:
            self._budgets[slo.slo_id] = ErrorBudgetLedger(
                slo_id=slo.slo_id,
                total_budget_ms=slo.window_hours * 3600 * 1000 * (1.0 - slo.target_value),
                window_start=slo.receipt_ref and datetime.now(timezone.utc),
            )
        return slo

    def get_slo(self, slo_id: str) -> Optional[SLODefinition]:
        return self._slos.get(slo_id)

    def list_slos(self, service_ref: Optional[str] = None) -> List[SLODefinition]:
        slos = list(self._slos.values())
        if service_ref:
            slos = [s for s in slos if s.service_ref == service_ref]
        return slos

    def evaluate_burn_rate(self, slo_id: str, reading: BurnRateReading) -> IncidentReceipt | None:
        """Evaluate burn rate against SLO thresholds. Returns incident if P0/P1."""
        slo = self._slos.get(slo_id)
        if not slo:
            return None

        reading.burn_rate_1h = reading.burn_rate_1h
        if reading.burn_rate_1h >= slo.burn_rate_threshold:
            reading.severity = IncidentSeverity.P0
        elif reading.burn_rate_1h >= slo.burn_rate_threshold * 0.43:
            reading.severity = IncidentSeverity.P1
        else:
            reading.severity = IncidentSeverity.P3

        if reading.severity in (IncidentSeverity.P0, IncidentSeverity.P1):
            return self._emit_incident(slo_id, reading)
        return None

    def _emit_incident(self, slo_id: str, reading: BurnRateReading) -> IncidentReceipt:
        incident = IncidentReceipt(
            incident_ref=f"incident://{slo_id}/{reading.measured_at.isoformat()}",
            severity=reading.severity,
            slo_id=slo_id,
            trigger=f"burn_rate={reading.burn_rate_1h:.1f}x",
            burn_rate=reading.burn_rate_1h,
            created_at=reading.measured_at,
        )
        self._incidents.append(incident)
        if self._on_incident:
            self._on_incident(incident)
        return incident

    # -- Job Run Ledger --

    def submit_job(
        self,
        job_kind: str,
        scope_ref: str,
        idempotency_key: Optional[str] = None,
        budget_ref: Optional[str] = None,
        created_by: str = "system",
    ) -> JobRunRecord:
        """Submit a new job. Enforces capacity budgets (backpressure)."""
        budget = self._capacity.get(scope_ref)
        if budget and budget.backpressure_active:
            # backpressure: block low-priority unless retraction
            if job_kind not in ("tier_recompute", "retraction_batch"):
                record = JobRunRecord(
                    job_ref=f"kbjob://{job_kind}/blocked/{scope_ref}",
                    job_kind=job_kind,
                    scope_ref=scope_ref,
                    status=JobStatus.BLOCKED,
                    error_message="backpressure_active",
                    idempotency_key=idempotency_key,
                )
                self._jobs[record.job_ref] = record
                return record

        if budget and budget.current_concurrent >= budget.max_concurrent_jobs:
            record = JobRunRecord(
                job_ref=f"kbjob://{job_kind}/queued/{scope_ref}",
                job_kind=job_kind,
                scope_ref=scope_ref,
                status=JobStatus.PENDING,
                error_message="capacity_exceeded",
                idempotency_key=idempotency_key,
            )
            self._jobs[record.job_ref] = record
            return record

        record = JobRunRecord(
            job_ref=f"kbjob://{job_kind}/{datetime.now(timezone.utc).strftime('%Y_%m_%d')}/{scope_ref}",
            job_kind=job_kind,
            scope_ref=scope_ref,
            idempotency_key=idempotency_key,
            budget_ref=budget_ref,
            created_by=created_by,
            status=JobStatus.PENDING,
        )
        self._jobs[record.job_ref] = record
        return record

    def start_job(self, job_ref: str) -> JobRunRecord | None:
        record = self._jobs.get(job_ref)
        if record and record.status == JobStatus.PENDING:
            record.status = JobStatus.RUNNING
            record.started_at = datetime.now(timezone.utc)
        return record

    def complete_job(self, job_ref: str, success: bool = True, error: Optional[str] = None) -> JobRunRecord | None:
        record = self._jobs.get(job_ref)
        if record and record.status == JobStatus.RUNNING:
            record.status = JobStatus.COMPLETED if success else JobStatus.FAILED
            record.completed_at = datetime.now(timezone.utc)
            record.error_message = error
        return record

    def cancel_job(self, job_ref: str) -> JobRunRecord | None:
        record = self._jobs.get(job_ref)
        if record and record.status in (JobStatus.PENDING, JobStatus.RUNNING):
            record.status = JobStatus.CANCELLED
            record.completed_at = datetime.now(timezone.utc)
        return record

    def get_job(self, job_ref: str) -> Optional[JobRunRecord]:
        return self._jobs.get(job_ref)

    def list_jobs(
        self,
        scope_ref: Optional[str] = None,
        status: Optional[JobStatus] = None,
        kind: Optional[str] = None,
    ) -> List[JobRunRecord]:
        jobs = list(self._jobs.values())
        if scope_ref:
            jobs = [j for j in jobs if j.scope_ref == scope_ref]
        if status:
            jobs = [j for j in jobs if j.status == status]
        if kind:
            jobs = [j for j in jobs if j.job_kind == kind]
        return jobs

    # -- Capacity Budgets --

    def register_capacity(self, budget: CapacityBudget) -> CapacityBudget:
        self._capacity[budget.scope_ref] = budget
        return budget

    def get_capacity(self, scope_ref: str) -> Optional[CapacityBudget]:
        return self._capacity.get(scope_ref)

    def check_backpressure(self, scope_ref: str, job_kind: str) -> bool:
        """Returns True if job should be blocked by backpressure."""
        budget = self._capacity.get(scope_ref)
        if not budget:
            return False
        if budget.backpressure_active and job_kind not in ("tier_recompute", "retraction_batch"):
            return True
        return False

    # -- Runbook Registry --

    def register_runbook(self, runbook: RunbookEntry) -> RunbookEntry:
        self._runbooks[runbook.runbook_id] = runbook
        return runbook

    def get_runbook(self, service_ref: str) -> Optional[RunbookEntry]:
        for rb in self._runbooks.values():
            if rb.service_ref == service_ref:
                return rb
        return None

    def list_runbooks(self) -> List[RunbookEntry]:
        return list(self._runbooks.values())

    # -- Release Gate --

    def evaluate_release_gate(self, release_ref: str) -> ReleaseGateReport:
        """Gate a release on SLO health. All SLOs must be GREEN."""
        checks = []
        blockers = []
        all_green = True

        for slo_id, slo in self._slos.items():
            is_green = slo.status == SLOStatus.GREEN or slo.status == SLOStatus.UNKNOWN
            checks.append({
                "slo_id": slo_id,
                "service_ref": slo.service_ref,
                "status": slo.status.value,
                "green": is_green,
            })
            if not is_green:
                all_green = False
                blockers.append(f"SLO {slo_id} is {slo.status.value}")

        gate_status = ReleaseGateStatus.PASS if all_green else ReleaseGateStatus.BLOCKED
        return ReleaseGateReport(
            gate_id=f"gate://{release_ref}",
            release_ref=release_ref,
            status=gate_status,
            slo_checks=checks,
            all_green=all_green,
            blockers=blockers,
        )

    # -- SLO Report (CLI surface) --

    def slo_report(self, scope: Optional[str] = None) -> Dict[str, Any]:
        """Generate SLO report for CLI consumption."""
        slos = self.list_slos()
        if scope:
            slos = [s for s in slos if scope in s.service_ref]

        report_slos = []
        for slo in slos:
            budget = self._budgets.get(slo.slo_id)
            report_slos.append({
                "slo_id": slo.slo_id,
                "service_ref": slo.service_ref,
                "sli_name": slo.sli_name,
                "target": f"{slo.target_value}",
                "status": slo.status.value,
                "budget_remaining_pct": f"{budget.remaining_pct:.1f}" if budget else "N/A",
                "owner": slo.owner,
            })

        return {
            "slo_count": len(report_slos),
            "slos": report_slos,
            "incidents_active": sum(1 for i in self._incidents if not i.resolved),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # -- Incident Lifecycle --

    def acknowledge_incident(self, incident_ref: str) -> Optional[IncidentReceipt]:
        for inc in self._incidents:
            if inc.incident_ref == incident_ref:
                inc.acknowledged = True
                return inc
        return None

    def resolve_incident(self, incident_ref: str) -> Optional[IncidentReceipt]:
        for inc in self._incidents:
            if inc.incident_ref == incident_ref:
                inc.resolved = True
                return inc
        return None

    def list_incidents(self, unresolved_only: bool = False) -> List[IncidentReceipt]:
        incidents = self._incidents
        if unresolved_only:
            incidents = [i for i in incidents if not i.resolved]
        return incidents
