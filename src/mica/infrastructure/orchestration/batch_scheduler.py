"""
batch_scheduler.py — Industrial MD Batch Scheduler (SP-21)

Priority-aware job queue with budget enforcement, retry logic, and
backpressure for large-scale provider-backed OpenMM batch runs.

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │                      BatchScheduler                           │
    ├──────────────────────────────────────────────────────────────┤
    │  PriorityQueue (min-heap by priority + submit_time)          │
    │       ↓                                                        │
    │  BudgetGate  → reject if total/hourly budget exceeded        │
    │       ↓                                                        │
    │  BackpressureGate → pause if active_jobs >= max_concurrent   │
    │       ↓                                                        │
    │  Dispatch → orchestrator_factory(job) → run                  │
    │       ↓                                                        │
    │  RetryPolicy → requeue on transient failure (exp backoff)    │
    └──────────────────────────────────────────────────────────────┘

Priority levels (lower number = higher priority):
    0 = CRITICAL   (hot path, user-interactive)
    1 = HIGH       (production batch)
    2 = NORMAL     (standard research)
    3 = LOW        (background / speculative)
    4 = BULK       (lowest, fill spare capacity)

Usage:
    scheduler = BatchScheduler(
        budget_policy=BudgetPolicy(hourly_cap_usd=5.0, total_cap_usd=50.0),
        backpressure_policy=BackpressurePolicy(max_concurrent=4, max_queue_depth=100),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=30.0),
    )
    for cfg in my_configs:
        scheduler.submit(BatchJob(config=cfg, priority=JobPriority.NORMAL))

    async def make_orchestrator(job):
        return await provider_specific_orchestrator(job.config).run()

    summary = await scheduler.run(make_orchestrator)
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

class JobPriority(IntEnum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3
    BULK     = 4


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

@dataclass
class BudgetPolicy:
    """
    Cost controls applied before each job dispatch.

    hourly_cap_usd: maximum $/hr across all concurrently running jobs.
    total_cap_usd: hard cap on total spend across the entire batch run (0 = unlimited).
    """
    hourly_cap_usd: float = 10.0       # max parallel hourly spend
    total_cap_usd: float = 0.0         # 0 = unlimited


@dataclass
class BackpressurePolicy:
    """
    Concurrency and queue-depth controls.

    max_concurrent: maximum simultaneously dispatched jobs.
    max_queue_depth: refuse submit() beyond this queue size (0 = unlimited).
    """
    max_concurrent: int = 4
    max_queue_depth: int = 0           # 0 = unlimited


@dataclass
class RetryPolicy:
    """
    Retry semantics for transient failures.

    max_attempts: total attempts per job (1 = no retry).
    base_delay_seconds: initial retry delay; doubles each attempt (capped at max_delay_seconds).
    retryable_statuses: set of job result status strings that trigger retry.
    """
    max_attempts: int = 3
    base_delay_seconds: float = 30.0
    max_delay_seconds: float = 300.0
    retryable_statuses: frozenset = frozenset({"failed", "timeout", "stopped"})

    def delay_for_attempt(self, attempt: int) -> float:
        """Exponential backoff: base * 2^(attempt-1), capped at max."""
        return min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)


# ---------------------------------------------------------------------------
# BatchJob
# ---------------------------------------------------------------------------

@dataclass
class BatchJob:
    """
    A single schedulable MD job.

    config: provider-specific MD config or any dict/object passed through to orchestrator_factory.
    priority: dispatch priority (lower = higher priority).
    estimated_cost_per_hour: hint used for budget gating (0 = unknown, bypass gate).
    tags: arbitrary key→value metadata (protein, run_id, team, etc.).
    job_id: auto-generated if not provided.
    """
    config: Any
    priority: JobPriority = JobPriority.NORMAL
    estimated_cost_per_hour: float = 0.0
    tags: Dict[str, str] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    # Internal scheduler state — do not set manually
    _attempt: int = field(default=0, init=False)
    _submitted_at: float = field(default_factory=time.monotonic, init=False)
    _last_error: Optional[str] = field(default=None, init=False)

    def __lt__(self, other: "BatchJob") -> bool:
        # Heap ordering: priority first, then FIFO by submit time
        if self.priority != other.priority:
            return self.priority < other.priority
        return self._submitted_at < other._submitted_at


# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------

@dataclass
class SchedulerStats:
    """Snapshot of scheduler state at a point in time."""
    queued: int = 0
    active: int = 0
    completed: int = 0
    failed: int = 0
    retried: int = 0
    rejected_budget: int = 0
    rejected_backpressure: int = 0
    total_spend_usd: float = 0.0
    peak_concurrent: int = 0


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------

@dataclass
class JobOutcome:
    """Final outcome record for a single BatchJob."""
    job_id: str
    status: str          # "completed" | "failed" | "budget_rejected" | "backpressure_rejected"
    attempts: int
    elapsed_seconds: float
    error: Optional[str]
    result: Optional[Dict[str, Any]]   # raw orchestrator result dict


# ---------------------------------------------------------------------------
# BatchScheduler
# ---------------------------------------------------------------------------

class BatchScheduler:
    """
    Priority-aware batch scheduler for MICA MD jobs.

    Thread-safety: designed for use within a single asyncio event loop.
    All public methods are coroutines or synchronous (submit).
    """

    def __init__(
        self,
        budget_policy: Optional[BudgetPolicy] = None,
        backpressure_policy: Optional[BackpressurePolicy] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        self._budget = budget_policy or BudgetPolicy()
        self._bp = backpressure_policy or BackpressurePolicy()
        self._retry = retry_policy or RetryPolicy()

        self._queue: List[BatchJob] = []   # min-heap
        self._active: Dict[str, BatchJob] = {}
        self._outcomes: List[JobOutcome] = []
        self._stats = SchedulerStats()
        self._running_hourly_cost: float = 0.0
        self._total_spend: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public: submit
    # ------------------------------------------------------------------

    def submit(self, job: BatchJob) -> bool:
        """
        Enqueue a job for dispatch.

        Returns True if accepted, False if rejected by backpressure policy.
        Budget rejection is deferred to dispatch time (hourly rate unknown until dispatch).
        """
        if self._bp.max_queue_depth > 0 and len(self._queue) >= self._bp.max_queue_depth:
            logger.warning("BatchScheduler: queue full (%d), rejecting job %s",
                           len(self._queue), job.job_id)
            self._stats.rejected_backpressure += 1
            self._outcomes.append(JobOutcome(
                job_id=job.job_id,
                status="backpressure_rejected",
                attempts=0,
                elapsed_seconds=0.0,
                error="Queue depth limit reached",
                result=None,
            ))
            return False

        heapq.heappush(self._queue, job)
        self._stats.queued += 1
        logger.debug("BatchScheduler: submitted job %s (priority=%s, queue_depth=%d)",
                     job.job_id, job.priority.name, len(self._queue))
        return True

    # ------------------------------------------------------------------
    # Public: run
    # ------------------------------------------------------------------

    async def run(
        self,
        orchestrator_factory: Callable[[BatchJob], Awaitable[Dict[str, Any]]],
        *,
        poll_interval_seconds: float = 2.0,
    ) -> List[JobOutcome]:
        """
        Drain the queue, dispatching jobs to orchestrator_factory.

        orchestrator_factory(job) must be an async callable that returns a
        result dict with at least {"status": ..., "elapsed_seconds": ...}.

        Runs until all submitted jobs are settled (completed, failed, or rejected).
        Returns list of JobOutcome for all jobs.
        """
        logger.info("BatchScheduler: run() started — %d jobs queued", len(self._queue))
        semaphore = asyncio.Semaphore(self._bp.max_concurrent)
        tasks: List[asyncio.Task] = []

        while self._queue or tasks:
            # Drain any done tasks first
            done = [t for t in tasks if t.done()]
            for t in done:
                tasks.remove(t)
                try:
                    t.result()  # surface exceptions to log
                except Exception as exc:
                    logger.error("BatchScheduler: dispatch task error: %s", exc)

            # Dispatch as many jobs as concurrency allows
            while self._queue:
                async with semaphore:
                    if self._stats.active >= self._bp.max_concurrent:
                        break

                    job = heapq.heappop(self._queue)
                    self._stats.queued -= 1

                    # Budget gate
                    if not self._check_budget(job):
                        self._stats.rejected_budget += 1
                        self._outcomes.append(JobOutcome(
                            job_id=job.job_id,
                            status="budget_rejected",
                            attempts=job._attempt,
                            elapsed_seconds=0.0,
                            error="Hourly or total budget cap exceeded",
                            result=None,
                        ))
                        logger.warning("BatchScheduler: job %s rejected by budget gate", job.job_id)
                        continue

                    task = asyncio.create_task(
                        self._dispatch(job, orchestrator_factory, semaphore)
                    )
                    tasks.append(task)
                    break  # re-check semaphore each iteration

            await asyncio.sleep(poll_interval_seconds)

        # Wait for any remaining tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "BatchScheduler: run() complete — completed=%d failed=%d retried=%d "
            "budget_rejected=%d backpressure_rejected=%d total_spend=$%.2f",
            self._stats.completed, self._stats.failed, self._stats.retried,
            self._stats.rejected_budget, self._stats.rejected_backpressure,
            self._total_spend,
        )
        return list(self._outcomes)

    # ------------------------------------------------------------------
    # Internal: dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        job: BatchJob,
        factory: Callable[[BatchJob], Awaitable[Dict[str, Any]]],
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Acquire semaphore, run orchestrator, handle retry."""
        async with semaphore:
            job._attempt += 1
            self._active[job.job_id] = job
            self._stats.active += 1
            self._running_hourly_cost += job.estimated_cost_per_hour
            if self._stats.active > self._stats.peak_concurrent:
                self._stats.peak_concurrent = self._stats.active

            start = time.monotonic()
            result: Optional[Dict[str, Any]] = None
            error: Optional[str] = None
            status = "failed"

            try:
                logger.info("BatchScheduler: dispatching job %s (attempt %d/%d, priority=%s)",
                            job.job_id, job._attempt, self._retry.max_attempts, job.priority.name)
                result = await factory(job)
                status = result.get("status", "failed")
                error = result.get("error")
            except Exception as exc:
                error = str(exc)
                status = "failed"
                logger.error("BatchScheduler: job %s raised exception: %s", job.job_id, exc)
            finally:
                elapsed = time.monotonic() - start
                self._active.pop(job.job_id, None)
                self._stats.active -= 1
                self._running_hourly_cost = max(0.0, self._running_hourly_cost - job.estimated_cost_per_hour)
                # Accumulate spend (hourly_rate × hours)
                if job.estimated_cost_per_hour > 0:
                    self._total_spend += job.estimated_cost_per_hour * (elapsed / 3600.0)
                self._stats.total_spend_usd = self._total_spend

            # Retry logic
            if (
                status in self._retry.retryable_statuses
                and job._attempt < self._retry.max_attempts
            ):
                delay = self._retry.delay_for_attempt(job._attempt)
                logger.info("BatchScheduler: job %s → %s, scheduling retry in %.0fs (attempt %d/%d)",
                            job.job_id, status, delay, job._attempt, self._retry.max_attempts)
                self._stats.retried += 1
                await asyncio.sleep(delay)
                heapq.heappush(self._queue, job)
                self._stats.queued += 1
                return

            # Final settlement
            final_status = "completed" if status == "completed" else "failed"
            if final_status == "completed":
                self._stats.completed += 1
            else:
                self._stats.failed += 1

            self._outcomes.append(JobOutcome(
                job_id=job.job_id,
                status=final_status,
                attempts=job._attempt,
                elapsed_seconds=round(elapsed, 1),
                error=error,
                result=result,
            ))
            logger.info("BatchScheduler: job %s settled as %s after %d attempt(s) in %.1fs",
                        job.job_id, final_status, job._attempt, elapsed)

    # ------------------------------------------------------------------
    # Internal: budget gate
    # ------------------------------------------------------------------

    def _check_budget(self, job: BatchJob) -> bool:
        """
        Return True if the job may proceed within budget constraints.

        Checks:
          1. If total_cap_usd > 0 and already spent >= cap → reject.
          2. If hourly_cap_usd > 0 and running_hourly_cost + job_rate > cap → reject.
        """
        if self._budget.total_cap_usd > 0:
            if self._total_spend >= self._budget.total_cap_usd:
                return False
        if self._budget.hourly_cap_usd > 0 and job.estimated_cost_per_hour > 0:
            projected = self._running_hourly_cost + job.estimated_cost_per_hour
            if projected > self._budget.hourly_cap_usd:
                return False
        return True

    # ------------------------------------------------------------------
    # Public: introspection
    # ------------------------------------------------------------------

    @property
    def stats(self) -> SchedulerStats:
        """Current scheduler statistics snapshot."""
        return SchedulerStats(
            queued=len(self._queue),
            active=self._stats.active,
            completed=self._stats.completed,
            failed=self._stats.failed,
            retried=self._stats.retried,
            rejected_budget=self._stats.rejected_budget,
            rejected_backpressure=self._stats.rejected_backpressure,
            total_spend_usd=round(self._total_spend, 4),
            peak_concurrent=self._stats.peak_concurrent,
        )

    def pending_jobs(self) -> List[BatchJob]:
        """Return a copy of the current queue (unsorted list of jobs)."""
        return list(self._queue)

    def active_jobs(self) -> Dict[str, BatchJob]:
        """Return currently running jobs keyed by job_id."""
        return dict(self._active)

    def outcomes(self) -> List[JobOutcome]:
        """Return all settled outcomes so far."""
        return list(self._outcomes)
