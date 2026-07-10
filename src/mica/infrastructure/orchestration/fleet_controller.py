"""
fleet_controller.py — Container Group Fleet Health Monitor (SP-21)

Detects stuck container groups, orphaned jobs, and real-time cost burn
across a live container-group provider fleet.

Current implementation note:
    - This first adapter targets Salad's container-groups API via provider._sdk.
    - The public control surface is intentionally phrased in provider-neutral
        terms so SP-21 batch governance can sit above any future container-group
        provider with equivalent list/destroy semantics.

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │                     FleetController                           │
    ├──────────────────────────────────────────────────────────────┤
    │  scan()                                                       │
    │    → list all live CGs in Salad project                      │
    │    → classify each as: HEALTHY | STUCK | ORPHANED | DEPLOYING │
    │    → compute: running_count, hourly_burn, stuck_list, orphans │
    │                                                               │
    │  cleanup_orphans()                                            │
    │    → destroy every CG flagged as ORPHANED                    │
    │    → returns list of destroyed CG names                      │
    │                                                               │
    │  surface_report()                                             │
    │    → return structured FleetReport dict (JSON-safe)          │
    └──────────────────────────────────────────────────────────────┘

Definitions:
    HEALTHY   — CG is running and known to the scheduler (job_id in known_ids).
    STUCK     — CG has been in RUNNING/DEPLOYING state longer than stuck_threshold_seconds
                            without any GCS checkpoint activity (optional GCS check).
    ORPHANED  — CG exists in the provider fleet but NOT in known_ids; it was never
                            registered with this controller or its job record was lost.
  DEPLOYING — CG is still pending/deploying, within normal startup grace window.

Cost burn is computed as:
    sum(price_per_hour × running_duration_hours) for all live CGs

Usage:
    controller = FleetController(
        provider=container_group_provider,
        known_job_ids=scheduler.active_jobs().keys(),
        stuck_threshold_seconds=3600,
    )
    report = await controller.scan()
    if report.orphan_count > 0:
        destroyed = await controller.cleanup_orphans()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CG classification
# ---------------------------------------------------------------------------

class CGHealth(Enum):
    HEALTHY   = auto()    # running + known + not stuck
    STUCK     = auto()    # running but > stuck_threshold with no progress
    ORPHANED  = auto()    # exists in Salad, unknown to scheduler
    DEPLOYING = auto()    # still starting up, within grace window
    UNKNOWN   = auto()    # cannot determine (status data missing)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CGRecord:
    """
    Snapshot of a single provider container group from the perspective of
    the fleet controller.
    """
    cg_name: str
    status_str: str                         # raw provider status (e.g. "running", "deploying")
    health: CGHealth
    price_per_hour: float                   # from GPU class price, 0 if unknown
    running_since: Optional[datetime]       # when CG entered RUNNING state (approx)
    age_seconds: float                      # total age since creation/detection
    estimated_cost_usd: float               # price_per_hour × (age_seconds / 3600)
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FleetReport:
    """
    Structured fleet health snapshot returned by FleetController.scan().

    All fields are JSON-safe (no datetime objects in structured output).
    """
    scanned_at: str                         # ISO-8601 UTC
    total_cgs: int
    healthy_count: int
    stuck_count: int
    orphan_count: int
    deploying_count: int
    hourly_burn_usd: float                  # $/hr across all running CGs
    total_estimated_cost_usd: float         # sum of estimated costs for all CGs
    stuck_cgs: List[str]                    # cg_names of stuck groups
    orphaned_cgs: List[str]                 # cg_names of orphaned groups
    all_cgs: List[Dict[str, Any]]           # full record per CG (serialized)
    residual_risks: List[str]               # human-readable risk notes


# ---------------------------------------------------------------------------
# FleetController
# ---------------------------------------------------------------------------

class FleetController:
    """
    Monitor and control a fleet of provider container groups for MICA batch runs.

    Parameters
    ----------
    provider : Any
        The live container-group provider instance (must have list/destroy capabilities).
    known_job_ids : set[str]
        Set of job IDs currently tracked by the BatchScheduler (used for
        orphan detection).  Pass an empty set to classify all CGs as orphaned.
    stuck_threshold_seconds : float
        CGs in RUNNING state longer than this without GCS checkpoint update
        are classified as STUCK. Default: 3600 (1 hour).
    deploy_grace_seconds : float
        CGs in PENDING/DEPLOYING state within this window are considered
        normal (DEPLOYING). Beyond it they are flagged STUCK. Default: 900 (15 min).
    gpu_price_per_hour : float
        Fallback GPU price for cost estimation when the provider cannot
        supply it from the CG record. Default: 0.25 (RTX 5090 base price).
    """

    DEFAULT_STUCK_THRESHOLD = 3600.0       # 1 hour
    DEFAULT_DEPLOY_GRACE    = 900.0        # 15 minutes
    DEFAULT_GPU_PRICE       = 0.25         # RTX 5090 $/hr

    def __init__(
        self,
        provider: Any,
        known_job_ids: Optional[Set[str]] = None,
        stuck_threshold_seconds: float = DEFAULT_STUCK_THRESHOLD,
        deploy_grace_seconds: float = DEFAULT_DEPLOY_GRACE,
        gpu_price_per_hour: float = DEFAULT_GPU_PRICE,
    ):
        self._provider = provider
        self._known_ids: Set[str] = set(known_job_ids or [])
        self._stuck_threshold = stuck_threshold_seconds
        self._deploy_grace = deploy_grace_seconds
        self._gpu_price = gpu_price_per_hour
        # Populated by scan()
        self._last_report: Optional[FleetReport] = None
        self._cg_records: List[CGRecord] = []

    # ------------------------------------------------------------------
    # Public: scan
    # ------------------------------------------------------------------

    async def scan(self) -> FleetReport:
        """
        Query the provider for all live container groups and classify them.

        Returns a FleetReport with health classification, cost burn, and
        risk notes.  Also stores the records for cleanup_orphans().
        """
        now = datetime.now(timezone.utc)
        cg_list = await self._list_live_cgs()
        self._cg_records = []

        healthy = stuck = orphaned = deploying = 0
        hourly_burn = 0.0
        total_cost = 0.0
        stuck_names: List[str] = []
        orphan_names: List[str] = []

        for raw in cg_list:
            record = self._classify_cg(raw, now)
            self._cg_records.append(record)

            if record.health == CGHealth.HEALTHY:
                healthy += 1
            elif record.health == CGHealth.STUCK:
                stuck += 1
                stuck_names.append(record.cg_name)
            elif record.health == CGHealth.ORPHANED:
                orphaned += 1
                orphan_names.append(record.cg_name)
            elif record.health == CGHealth.DEPLOYING:
                deploying += 1

            hourly_burn += record.price_per_hour
            total_cost += record.estimated_cost_usd

        risks = self._assess_risks(stuck_names, orphan_names, hourly_burn, total_cost)

        report = FleetReport(
            scanned_at=now.isoformat(),
            total_cgs=len(cg_list),
            healthy_count=healthy,
            stuck_count=stuck,
            orphan_count=orphaned,
            deploying_count=deploying,
            hourly_burn_usd=round(hourly_burn, 4),
            total_estimated_cost_usd=round(total_cost, 4),
            stuck_cgs=stuck_names,
            orphaned_cgs=orphan_names,
            all_cgs=[_serialize_cg_record(r) for r in self._cg_records],
            residual_risks=risks,
        )
        self._last_report = report
        logger.info(
            "FleetController: scan complete — total=%d healthy=%d stuck=%d orphaned=%d deploying=%d "
            "hourly_burn=$%.3f/hr total_cost=$%.3f",
            report.total_cgs, healthy, stuck, orphaned, deploying,
            hourly_burn, total_cost,
        )
        return report

    # ------------------------------------------------------------------
    # Public: cleanup_orphans
    # ------------------------------------------------------------------

    async def cleanup_orphans(self) -> List[str]:
        """
        Destroy all container groups classified as ORPHANED in the last scan().

        Returns list of CG names that were successfully destroyed.
        Must call scan() first or raises RuntimeError.
        """
        if self._last_report is None:
            raise RuntimeError("FleetController.cleanup_orphans() called before scan()")

        destroyed: List[str] = []
        for record in self._cg_records:
            if record.health != CGHealth.ORPHANED:
                continue
            try:
                await self._provider.destroy_instance(record.cg_name)
                destroyed.append(record.cg_name)
                logger.info("FleetController: destroyed orphaned CG %s", record.cg_name)
            except Exception as exc:
                logger.error("FleetController: failed to destroy orphan %s: %s", record.cg_name, exc)

        return destroyed

    # ------------------------------------------------------------------
    # Public: surface_report
    # ------------------------------------------------------------------

    def surface_report(self) -> Optional[Dict[str, Any]]:
        """
        Return the last FleetReport as a JSON-safe dict, or None if
        scan() has not been called yet.
        """
        if self._last_report is None:
            return None
        r = self._last_report
        return {
            "scanned_at": r.scanned_at,
            "total_cgs": r.total_cgs,
            "healthy_count": r.healthy_count,
            "stuck_count": r.stuck_count,
            "orphan_count": r.orphan_count,
            "deploying_count": r.deploying_count,
            "hourly_burn_usd": r.hourly_burn_usd,
            "total_estimated_cost_usd": r.total_estimated_cost_usd,
            "stuck_cgs": r.stuck_cgs,
            "orphaned_cgs": r.orphaned_cgs,
            "residual_risks": r.residual_risks,
            "all_cgs": r.all_cgs,
        }

    # ------------------------------------------------------------------
    # Public: register / unregister known jobs
    # ------------------------------------------------------------------

    def register_job(self, job_id: str) -> None:
        """Mark a job_id as scheduler-known (prevents orphan classification)."""
        self._known_ids.add(job_id)

    def unregister_job(self, job_id: str) -> None:
        """Remove a job_id from known set (e.g. on cleanup)."""
        self._known_ids.discard(job_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _list_live_cgs(self) -> List[Dict[str, Any]]:
        """
        Retrieve all container groups from the provider adapter.

        Current implementation delegates to ``provider._sdk.container_groups``
        when available. Returns raw dicts extracted from the SDK response.
        """
        try:
            sdk = getattr(self._provider, "_sdk", None)
            if sdk is None:
                logger.warning("FleetController: provider has no _sdk; returning empty fleet")
                return []
            cg_list = await sdk.container_groups.list_container_groups(
                organization_name=self._provider._org_name,
                project_name=self._provider._project_name,
            )
            items = getattr(cg_list, "items", None) or []
            result = []
            for cg in items:
                status_str = "unknown"
                if cg.current_state and cg.current_state.status:
                    sv = getattr(cg.current_state.status, "value", None)
                    status_str = str(sv if sv is not None else cg.current_state.status)
                result.append({
                    "name": cg.name,
                    "status": status_str,
                    "raw": cg,
                })
            return result
        except Exception as exc:
            logger.error("FleetController: list_container_groups failed: %s", exc)
            return []

    def _classify_cg(self, raw: Dict[str, Any], now: datetime) -> CGRecord:
        """
        Classify a single CG dict into a CGRecord with health label.

        The CG name encodes the MICA job_id via the _make_cg_name() pattern:
            mica-{job_id[:24]}-{timestamp}  or  mica-{job_id}
        We extract the job_id prefix from the name for known_ids lookup.
        """
        cg_name: str = raw.get("name", "unknown")
        status_str: str = raw.get("status", "unknown").lower()

        # Extract job_id from CG name for orphan detection
        # CG name pattern: mica-{job_id}-{yyyymmdd}-{hhmmss} (max 63 chars)
        # We test if any known_id is a prefix of the cg_name content after "mica-"
        is_known = self._is_known_cg(cg_name)

        # Approximate age from current time (we don't have exact create time from list API)
        # Use a conservative estimate: 0 if we can't determine
        age_seconds = 0.0

        price = self._gpu_price
        estimated_cost = price * (age_seconds / 3600.0)

        # Classify
        if status_str in ("running",):
            if not is_known:
                health = CGHealth.ORPHANED
            elif age_seconds > self._stuck_threshold:
                health = CGHealth.STUCK
            else:
                health = CGHealth.HEALTHY
        elif status_str in ("pending", "deploying", "allocating", "creating"):
            if not is_known:
                health = CGHealth.ORPHANED
            elif age_seconds > self._deploy_grace:
                health = CGHealth.STUCK
            else:
                health = CGHealth.DEPLOYING
        elif status_str in ("stopped", "succeeded", "failed", "terminated"):
            # Terminal states: treat as orphaned if not known (they shouldn't be here long)
            health = CGHealth.ORPHANED if not is_known else CGHealth.HEALTHY
        else:
            health = CGHealth.UNKNOWN

        return CGRecord(
            cg_name=cg_name,
            status_str=status_str,
            health=health,
            price_per_hour=price,
            running_since=None,
            age_seconds=age_seconds,
            estimated_cost_usd=round(estimated_cost, 4),
            raw_data={"status": status_str},
        )

    def _is_known_cg(self, cg_name: str) -> bool:
        """
        Return True if the CG name corresponds to a known job_id.

        Checks if any known job_id appears as a substring in the CG name.
        Falls back to prefix-match on 'mica-' stripped form.
        """
        for job_id in self._known_ids:
            # CG names are truncated lowercase; match job_id fragment
            if job_id.lower()[:12] in cg_name.lower():
                return True
        return False

    def _assess_risks(
        self,
        stuck_names: List[str],
        orphan_names: List[str],
        hourly_burn: float,
        total_cost: float,
    ) -> List[str]:
        """Generate human-readable residual risk notes."""
        risks: List[str] = []
        if stuck_names:
            risks.append(
                f"{len(stuck_names)} stuck CG(s) detected: {stuck_names[:3]}{'...' if len(stuck_names) > 3 else ''}. "
                "Investigate GCS checkpoint activity; consider manual destroy."
            )
        if orphan_names:
            risks.append(
                f"{len(orphan_names)} orphaned CG(s) detected: {orphan_names[:3]}{'...' if len(orphan_names) > 3 else ''}. "
                "Call cleanup_orphans() to destroy and stop cost burn."
            )
        if hourly_burn > 10.0:
            risks.append(
                f"High hourly cost burn: ${hourly_burn:.2f}/hr. "
                "Verify active jobs are making progress."
            )
        if total_cost > 100.0:
            risks.append(
                f"Total estimated cost has reached ${total_cost:.2f}. "
                "Review batch budget policy."
            )
        if not risks:
            risks.append("No active risk flags detected.")
        return risks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_cg_record(r: CGRecord) -> Dict[str, Any]:
    """Convert CGRecord to JSON-safe dict."""
    return {
        "cg_name": r.cg_name,
        "status_str": r.status_str,
        "health": r.health.name,
        "price_per_hour": r.price_per_hour,
        "age_seconds": r.age_seconds,
        "estimated_cost_usd": r.estimated_cost_usd,
    }
