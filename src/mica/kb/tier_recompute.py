"""
KB Tier Recompute — K6-8 (KB Slice 4)

Retraction-triggered tier recompute with ImpactFrontier.
Batched (500 families/shard), no recursive invalidation.
Coalesce in 15-min window. Public/high-tier first.

Key objects:
- RetractionBatch: batch of retracted claims
- ImpactFrontier: affected families from retraction
- TierRecomputeJob: batched recompute job
- KnowledgeHealthIncident: coalesced incident receipt
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class RecomputeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class RetractionBatch:
    """K6-8: Batch of retracted claims."""
    batch_ref: str
    claim_refs: List[str]
    scope_ref: str
    reason: str = "source_retracted"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


@dataclass
class ImpactFrontier:
    """K6-8: Affected families from retraction — not recursive, one level only."""
    frontier_ref: str
    retracted_claims: List[str]
    affected_families: List[str]  # claim families that reference retracted claims
    affected_count: int = 0
    high_tier_first: bool = True  # public/high-tier processed first
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TierRecomputeJob:
    """K6-8: Batched recompute job."""
    job_ref: str
    scope_ref: str
    frontier_ref: str
    batch_size: int = 500  # families per shard
    shards_total: int = 0
    shards_done: int = 0
    families_recomputed: int = 0
    status: RecomputeStatus = RecomputeStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    receipt_ref: Optional[str] = None


@dataclass
class KnowledgeHealthIncident:
    """Coalesced incident receipt for retraction-triggered recompute."""
    incident_ref: str
    batch_ref: str
    frontier_ref: str
    families_affected: int = 0
    tiers_changed: int = 0
    coalesced_count: int = 0  # how many retraction batches were coalesced
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    receipt_ref: Optional[str] = None


# ponytail: 15-min coalesce window per K6-8 spec
_COALESCE_WINDOW_MINUTES = 15
_MAX_BATCH_SIZE = 500


class TierRecomputeManager:
    """K6-8: Retraction-triggered tier recompute with ImpactFrontier.

    Batched (500 families/shard), no recursive invalidation.
    Coalesce in 15-min window. Public/high-tier first.

    Red-line: No mass retraction as sync recursive storm.
    """

    def __init__(self) -> None:
        self._batches: Dict[str, RetractionBatch] = {}
        self._frontiers: Dict[str, ImpactFrontier] = {}
        self._jobs: Dict[str, TierRecomputeJob] = {}
        self._incidents: List[KnowledgeHealthIncident] = []
        self._pending_batches: List[RetractionBatch] = []
        self._last_coalesce_at: Optional[datetime] = None

    def submit_retraction_batch(self, batch: RetractionBatch) -> RetractionBatch:
        """Submit a retraction batch. Coalesced within 15-min window."""
        self._batches[batch.batch_ref] = batch
        self._pending_batches.append(batch)
        return batch

    def compute_impact_frontier(
        self,
        batch_ref: str,
        family_lookup: Callable[[str], List[str]],
    ) -> ImpactFrontier:
        """Compute affected families. One level only — no recursive invalidation."""
        batch = self._batches.get(batch_ref)
        if not batch:
            raise ValueError(f"unknown batch: {batch_ref}")

        affected_families: Set[str] = set()
        for claim_ref in batch.claim_refs:
            families = family_lookup(claim_ref)
            affected_families.update(families)

        frontier = ImpactFrontier(
            frontier_ref=f"frontier://{batch.scope_ref}/{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            retracted_claims=batch.claim_refs,
            affected_families=sorted(affected_families),
            affected_count=len(affected_families),
        )
        self._frontiers[frontier.frontier_ref] = frontier
        return frontier

    def start_recompute(
        self,
        scope_ref: str,
        frontier_ref: str,
        batch_size: int = _MAX_BATCH_SIZE,
    ) -> TierRecomputeJob:
        """Start batched recompute job."""
        frontier = self._frontiers.get(frontier_ref)
        if not frontier:
            raise ValueError(f"unknown frontier: {frontier_ref}")

        shards_total = max(1, (frontier.affected_count + batch_size - 1) // batch_size)
        job = TierRecomputeJob(
            job_ref=f"recompute://{scope_ref}/{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            scope_ref=scope_ref,
            frontier_ref=frontier_ref,
            batch_size=batch_size,
            shards_total=shards_total,
            status=RecomputeStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._jobs[job.job_ref] = job
        return job

    def complete_shard(self, job_ref: str, families_done: int) -> TierRecomputeJob | None:
        job = self._jobs.get(job_ref)
        if job:
            job.shards_done += 1
            job.families_recomputed += families_done
            if job.shards_done >= job.shards_total:
                job.status = RecomputeStatus.COMPLETED
                job.completed_at = datetime.now(timezone.utc)
        return job

    def coalesce_batches(self, now: Optional[datetime] = None) -> KnowledgeHealthIncident | None:
        """Coalesce pending batches within 15-min window."""
        now = now or datetime.now(timezone.utc)
        if not self._pending_batches:
            return None

        if self._last_coalesce_at:
            elapsed = (now - self._last_coalesce_at).total_seconds() / 60
            if elapsed < _COALESCE_WINDOW_MINUTES:
                return None

        batches = list(self._pending_batches)
        self._pending_batches.clear()
        self._last_coalesce_at = now

        all_claims = []
        all_families = set()
        for batch in batches:
            all_claims.extend(batch.claim_refs)
            frontier = None
            for f in self._frontiers.values():
                if f.retracted_claims == batch.claim_refs:
                    frontier = f
                    break
            if frontier:
                all_families.update(frontier.affected_families)

        incident = KnowledgeHealthIncident(
            incident_ref=f"incident://retraction/{now.isoformat()}",
            batch_ref=batches[0].batch_ref,
            frontier_ref=f"coalesced://{len(batches)}_batches",
            families_affected=len(all_families),
            coalesced_count=len(batches),
        )
        self._incidents.append(incident)
        return incident

    def get_job(self, job_ref: str) -> Optional[TierRecomputeJob]:
        return self._jobs.get(job_ref)

    def list_jobs(self, scope_ref: Optional[str] = None) -> List[TierRecomputeJob]:
        jobs = list(self._jobs.values())
        if scope_ref:
            jobs = [j for j in jobs if j.scope_ref == scope_ref]
        return jobs

    def list_incidents(self, unresolved_only: bool = False) -> List[KnowledgeHealthIncident]:
        incidents = self._incidents
        if unresolved_only:
            incidents = [i for i in incidents if not i.resolved]
        return incidents
