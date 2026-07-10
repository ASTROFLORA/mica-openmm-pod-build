"""
job_manager.py - Cloud Job Queue and Lifecycle Manager

Manages the lifecycle of cloud GPU jobs including:
- Job queue with priority
- Status webhooks
- Checkpoint/resume on preemption
- Cost limits and auto-termination
- MUDOEnvelope integration for worker handoffs
- **Timescale persistence for durability** (Team 2 fix)

Integration with CloudOrchestrator:
    orchestrator = CloudOrchestrator()
    job_manager = JobManager(orchestrator)
    
    job = await job_manager.submit(JobSpec(...))
    await job_manager.wait_for_completion(job.job_id)

Author: MICA Team
Date: December 2024
Updated: January 2025 (Team 2 - added Timescale persistence)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING
import uuid
import json
import os
from pathlib import Path

# Import persistence layer (Team 2)
from .persistence.timescale_job_store import JobStoreABC, TimescaleJobStore, InMemoryJobStore
from .redis_client import format_redis_target, resolve_redis_url

if TYPE_CHECKING:
    from .cloud_orchestrator import CloudOrchestrator


class JobStatus(Enum):
    """Job lifecycle status."""
    QUEUED = "queued"           # Waiting for resources
    AWAITING_APPROVAL = "awaiting_approval"  # Requires human approval before enqueue
    PROVISIONING = "provisioning"  # Instance being created
    RUNNING = "running"          # Job executing
    CHECKPOINTING = "checkpointing"  # Saving state before preemption
    PAUSED = "paused"           # Temporarily stopped
    COMPLETED = "completed"      # Successfully finished
    FAILED = "failed"           # Error during execution
    CANCELLED = "cancelled"      # User cancelled
    PREEMPTED = "preempted"     # Spot instance reclaimed


class JobPriority(Enum):
    """Job priority levels."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class JobSpec:
    """
    Specification for a cloud GPU job.
    """
    # Identity
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    worker_type: str = "generic"  # "dynamo", "chronos", "spectra"
    user_id: str = ""  # owner (used for authz / filtering)
    
    # Resource requirements (passed to ProvisionRequest)
    gpu_type: str = "L40S"
    gpu_count: int = 1
    min_ram_gb: float = 32.0
    min_disk_gb: float = 100.0
    
    # Docker configuration
    docker_image: str = "nvcr.io/nvidia/pytorch:24.01-py3"
    docker_command: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    
    # Cost constraints
    max_price_per_hour: Optional[float] = None
    max_total_cost_usd: Optional[float] = None
    prefer_spot: bool = True
    
    # Time constraints
    max_duration_hours: Optional[float] = None
    
    # Priority and scheduling
    priority: JobPriority = JobPriority.NORMAL
    
    # Checkpoint configuration
    checkpoint_interval_minutes: int = 30
    checkpoint_gcs_path: Optional[str] = None  # gs://bucket/path/
    resume_from_checkpoint: Optional[str] = None
    
    # Callback URLs for webhooks
    webhook_url: Optional[str] = None
    
    # Input/Output
    input_data_path: Optional[str] = None   # GCS path to input data
    output_data_path: Optional[str] = None  # GCS path for outputs
    
    # Metadata for MUDOEnvelope
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    """
    Runtime state of a job.
    """
    spec: JobSpec
    status: JobStatus = JobStatus.QUEUED
    
    # Instance info
    instance_id: Optional[str] = None
    provider: Optional[str] = None
    
    # Timing
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Cost tracking
    total_cost_usd: float = 0.0
    
    # Execution
    exit_code: Optional[int] = None
    error_message: Optional[str] = None
    
    # Checkpointing
    last_checkpoint: Optional[datetime] = None
    checkpoint_count: int = 0
    
    # Progress (0-100)
    progress_percent: float = 0.0
    
    # Logs
    log_tail: List[str] = field(default_factory=list)
    
    @property
    def job_id(self) -> str:
        return self.spec.job_id
    
    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.completed_at or datetime.utcnow()
        return (end - self.started_at).total_seconds()
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON/webhook."""
        return {
            "job_id": self.job_id,
            "name": self.spec.name,
            "status": self.status.value,
            "worker_type": self.spec.worker_type,
            "instance_id": self.instance_id,
            "provider": self.provider,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "total_cost_usd": self.total_cost_usd,
            "progress_percent": self.progress_percent,
            "exit_code": self.exit_code,
            "error_message": self.error_message,
        }


class JobManager:
    """
    Manages cloud GPU job lifecycle.
    
    Features:
    - Priority queue
    - Automatic retry on preemption
    - Cost limits
    - Webhook notifications
    - Checkpoint coordination
    - **Timescale persistence** (jobs survive restarts)
    - **Redis persistence** via MICA_ENABLE_REDIS_JOBS (Phase R2)
    """
    
    def __init__(
        self,
        orchestrator: "CloudOrchestrator",
        max_concurrent_jobs: int = 10,
        status_poll_interval: float = 30.0,
        job_store: Optional[JobStoreABC] = None,
        use_timescale: bool = True,
    ):
        """
        Initialize job manager.
        
        Args:
            orchestrator: CloudOrchestrator for provisioning
            max_concurrent_jobs: Maximum simultaneous running jobs
            status_poll_interval: Seconds between status checks
            job_store: Optional custom job store (defaults to auto-detect)
            use_timescale: If True and no job_store provided, use TimescaleJobStore
        
        Store selection priority (when ``job_store`` is None):
            1. ``MICA_ENABLE_REDIS_JOBS=true`` + ``REDIS_URL`` set → RedisJobStore
            2. ``use_timescale=True`` + ``TIMESCALE_URL``/``DATABASE_URL`` set → TimescaleJobStore
            3. Otherwise → InMemoryJobStore (no durability)
        """
        self.orchestrator = orchestrator
        self.max_concurrent_jobs = max_concurrent_jobs
        self.status_poll_interval = status_poll_interval
        
        # Job storage - now with persistence! (Team 2)
        self._jobs: Dict[str, Job] = {}
        self._queue: List[str] = []  # Job IDs in priority order
        
        # Persistence layer selection (Phase R2: add Redis option)
        if job_store is not None:
            self._job_store: JobStoreABC = job_store
        else:
            self._job_store = self._auto_select_store(use_timescale=use_timescale)
        
        # Callbacks
        self._status_callbacks: List[Callable[[Job], None]] = []
        
        # Background task
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._initialized = False

    # ------------------------------------------------------------------
    # Store auto-selection  (Phase R2)
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_select_store(*, use_timescale: bool = True) -> JobStoreABC:
        """Pick the best available persistence backend.

        Priority:
            1. ``MICA_ENABLE_REDIS_JOBS=true`` + ``REDIS_URL`` → RedisJobStore
            2. ``use_timescale`` + ``TIMESCALE_URL`` / ``DATABASE_URL`` → TimescaleJobStore
            3. InMemoryJobStore
        """
        import logging as _log
        _logger = _log.getLogger("mica.infrastructure.job_manager")

        enable_redis = (os.getenv("MICA_ENABLE_REDIS_JOBS") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        redis_url = resolve_redis_url()
        disable_db = (os.getenv("MICA_DISABLE_DATABASE") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }

        if enable_redis and redis_url:
            try:
                import redis.asyncio as _aioredis
                from .persistence.redis_job_store import RedisJobStore

                client = _aioredis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    retry_on_timeout=True,
                )
                _logger.info("Job store → RedisJobStore (%s)", format_redis_target(redis_url))
                return RedisJobStore(redis_client=client)
            except Exception as exc:
                _logger.warning("RedisJobStore init failed (%s) — falling back", exc)
        elif enable_redis:
            _logger.warning(
                "MICA_ENABLE_REDIS_JOBS is enabled but Redis is unconfigured (set REDIS_URL or MICA_REDIS_URL)"
            )

        if use_timescale:
            timescale_url = (
                os.getenv("TIMESCALE_URL") or os.getenv("DATABASE_URL") or ""
            ).strip()
            if not disable_db and timescale_url:
                _logger.info("Job store → TimescaleJobStore")
                return TimescaleJobStore(database_url=timescale_url)

        _logger.info("Job store → InMemoryJobStore (no durability)")
        return InMemoryJobStore()
    
    async def submit(self, spec: JobSpec) -> Job:
        """
        Submit a job to the queue.
        
        Args:
            spec: Job specification
            
        Returns:
            Job object for tracking
        """
        return await self.create_job(spec=spec, status=JobStatus.QUEUED, enqueue=True)

    async def create_job(
        self,
        *,
        spec: JobSpec,
        status: JobStatus = JobStatus.QUEUED,
        enqueue: bool = True,
    ) -> Job:
        """Create a job record, optionally enqueueing it.

        This is used by governance to create jobs in `AWAITING_APPROVAL`
        without starting them.
        """
        job = Job(spec=spec)
        job.status = status
        self._jobs[job.job_id] = job

        if enqueue:
            self._insert_by_priority(job.job_id, spec.priority)

        await self._persist_job(job)
        await self._notify_status_change(job)

        if enqueue and not self._running:
            self.start()

        return job

    async def approve_job(self, job_id: str, *, approved_by: str) -> Optional[Job]:
        """Approve a job that is awaiting human approval and enqueue it."""
        job = await self.get_job(job_id)
        if job is None:
            return None

        if job.status != JobStatus.AWAITING_APPROVAL:
            return job

        if not isinstance(job.spec.metadata, dict):
            job.spec.metadata = {}

        job.spec.metadata["approved_by"] = (approved_by or "").strip()
        job.spec.metadata["approved_at"] = datetime.utcnow().isoformat()
        job.spec.metadata["requires_human_approval"] = False

        job.status = JobStatus.QUEUED
        self._insert_by_priority(job.job_id, job.spec.priority)
        await self._notify_status_change(job)

        if not self._running:
            self.start()

        return job

    async def deny_job(self, job_id: str, *, denied_by: str, reason: str = "Denied by human") -> Optional[Job]:
        """Deny a job awaiting approval, moving it to CANCELLED."""
        job = await self.get_job(job_id)
        if job is None:
            return None

        if job_id in self._queue:
            self._queue.remove(job_id)

        if not isinstance(job.spec.metadata, dict):
            job.spec.metadata = {}

        job.spec.metadata["denied_by"] = (denied_by or "").strip()
        job.spec.metadata["denied_at"] = datetime.utcnow().isoformat()
        job.spec.metadata["denial_reason"] = reason

        job.status = JobStatus.CANCELLED
        job.error_message = reason
        job.completed_at = datetime.utcnow()
        await self._notify_status_change(job)
        return job
    
    async def _persist_job(self, job: Job) -> None:
        """Persist job to Timescale (Team 2 fix)."""
        try:
            await self._job_store.save_job(job)
        except Exception as e:
            # Log but don't fail - job is still in memory
            print(f"Warning: Failed to persist job {job.job_id}: {e}")
    
    async def recover_jobs(self) -> int:
        """
        Recover active jobs from Timescale after restart (Team 2).
        
        Returns:
            Number of jobs recovered
        """
        try:
            active_jobs = await self._job_store.load_active_jobs()
            recovered = 0
            
            for job_data in active_jobs:
                job_id = job_data.get("job_id")
                if job_id and job_id not in self._jobs:
                    # Reconstruct JobSpec from stored data
                    spec = JobSpec(
                        job_id=job_id,
                        name=job_data.get("name", ""),
                        worker_type=job_data.get("worker_type", "generic"),
                        gpu_type=job_data.get("gpu_type", "L40S"),
                        gpu_count=job_data.get("gpu_count", 1),
                        docker_image=job_data.get("docker_image", ""),
                    )
                    
                    job = Job(spec=spec)
                    job.status = JobStatus(job_data.get("status", "queued"))
                    job.instance_id = job_data.get("instance_id")
                    job.provider = job_data.get("provider")
                    job.total_cost_usd = job_data.get("total_cost_usd", 0.0)
                    
                    self._jobs[job_id] = job
                    
                    # Re-queue if needed
                    if job.status == JobStatus.QUEUED:
                        self._queue.append(job_id)
                    
                    recovered += 1
            
            return recovered
        except Exception as e:
            print(f"Warning: Failed to recover jobs: {e}")
            return 0
    
    def _insert_by_priority(self, job_id: str, priority: JobPriority) -> None:
        """Insert job into queue maintaining priority order."""
        # Find insertion point
        insert_at = len(self._queue)
        for i, existing_id in enumerate(self._queue):
            existing_job = self._jobs.get(existing_id)
            if existing_job and existing_job.spec.priority.value < priority.value:
                insert_at = i
                break
        
        self._queue.insert(insert_at, job_id)
    
    async def cancel(self, job_id: str) -> bool:
        """
        Cancel a job.
        
        Args:
            job_id: Job to cancel
            
        Returns:
            True if cancelled successfully
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        
        # Remove from queue
        if job_id in self._queue:
            self._queue.remove(job_id)
        
        # Destroy instance if running
        if job.instance_id:
            await self.orchestrator.destroy(job.instance_id)
        
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.utcnow()
        
        await self._notify_status_change(job)
        
        return True
    
    async def get_job(self, job_id: str) -> Optional[Job]:
        """Get job by ID.

        Prefers in-memory state, but falls back to the persistent job store
        (Timescale) so API reads continue working after process restarts.
        """
        job = self._jobs.get(job_id)
        if job is not None:
            return job

        # Best-effort DB fallback (do not fail hard if DB is unavailable).
        try:
            job_data = await self._job_store.load_job(job_id)
        except Exception:
            return None

        if not job_data:
            return None

        def _parse_dt(v: Any) -> Optional[datetime]:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(str(v))
            except Exception:
                return None

        # Reconstruct minimal JobSpec/Job.
        spec = JobSpec(
            job_id=str(job_data.get("job_id") or job_id),
            name=str(job_data.get("name") or ""),
            worker_type=str(job_data.get("worker_type") or "generic"),
            user_id=str(job_data.get("user_id") or ""),
            gpu_type=str(job_data.get("gpu_type") or "L40S"),
            gpu_count=int(job_data.get("gpu_count") or 1),
            docker_image=str(job_data.get("docker_image") or ""),
            metadata=(job_data.get("metadata") or {}) if isinstance(job_data.get("metadata"), dict) else {},
        )
        reconstructed = Job(spec=spec)

        try:
            reconstructed.status = JobStatus(str(job_data.get("status") or "queued"))
        except Exception:
            reconstructed.status = JobStatus.QUEUED

        reconstructed.instance_id = job_data.get("instance_id")
        reconstructed.provider = job_data.get("provider")
        reconstructed.created_at = _parse_dt(job_data.get("created_at")) or reconstructed.created_at
        reconstructed.started_at = _parse_dt(job_data.get("started_at"))
        reconstructed.completed_at = _parse_dt(job_data.get("completed_at"))
        reconstructed.total_cost_usd = float(job_data.get("total_cost_usd") or 0.0)
        reconstructed.error_message = job_data.get("error_message")

        # Cache it for subsequent operations.
        self._jobs[reconstructed.job_id] = reconstructed
        return reconstructed
    
    def list_jobs(
        self,
        user_id: Optional[str] = None,
        status: Optional[JobStatus] = None,
        worker_type: Optional[str] = None,
    ) -> List[Job]:
        """
        List jobs with optional filtering.
        
        Args:
            status: Filter by status
            worker_type: Filter by worker type
            
        Returns:
            List of matching jobs
        """
        jobs = list(self._jobs.values())

        if user_id is not None:
            jobs = [j for j in jobs if (j.spec.user_id or "") == user_id]
        
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        
        if worker_type is not None:
            jobs = [j for j in jobs if j.spec.worker_type == worker_type]
        
        return jobs
    
    def start(self) -> None:
        """Start the background monitor."""
        if self._running:
            return
        
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def stop(self) -> None:
        """Stop the background monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
    
    async def _monitor_loop(self) -> None:
        """Background loop for job management."""
        while self._running:
            try:
                # Process queue
                await self._process_queue()
                
                # Check running jobs
                await self._check_running_jobs()
                
            except Exception as e:
                print(f"JobManager monitor error: {e}")
            
            await asyncio.sleep(self.status_poll_interval)
    
    async def _process_queue(self) -> None:
        """Try to start queued jobs."""
        running_count = len([
            j for j in self._jobs.values()
            if j.status in (JobStatus.RUNNING, JobStatus.PROVISIONING)
        ])
        
        while self._queue and running_count < self.max_concurrent_jobs:
            job_id = self._queue.pop(0)
            job = self._jobs.get(job_id)
            
            if job is None or job.status != JobStatus.QUEUED:
                continue
            
            # Try to provision
            await self._start_job(job)
            running_count += 1
    
    async def _start_job(self, job: Job) -> None:
        """Provision and start a job."""
        from .providers.base_provider import ProvisionRequest, GPUType
        
        job.status = JobStatus.PROVISIONING
        await self._notify_status_change(job)
        
        # Build provision request
        try:
            gpu_type = GPUType[job.spec.gpu_type]
        except KeyError:
            gpu_type = GPUType.L40S
        
        # Add job-specific env vars
        env_vars = dict(job.spec.env_vars)
        env_vars["MICA_JOB_ID"] = job.job_id
        env_vars["MICA_WORKER_TYPE"] = job.spec.worker_type
        
        if job.spec.checkpoint_gcs_path:
            env_vars["MICA_CHECKPOINT_PATH"] = job.spec.checkpoint_gcs_path
        
        if job.spec.resume_from_checkpoint:
            env_vars["MICA_RESUME_CHECKPOINT"] = job.spec.resume_from_checkpoint
        
        request = ProvisionRequest(
            gpu_type=gpu_type,
            gpu_count=job.spec.gpu_count,
            min_ram_gb=job.spec.min_ram_gb,
            min_disk_gb=job.spec.min_disk_gb,
            docker_image=job.spec.docker_image,
            docker_command=job.spec.docker_command,
            env_vars=env_vars,
            max_price_per_hour=job.spec.max_price_per_hour,
            prefer_spot=job.spec.prefer_spot,
            job_id=job.job_id,
            worker_type=job.spec.worker_type,
        )
        
        result = await self.orchestrator.provision(request)
        
        if result.success and result.instance:
            job.instance_id = result.instance.instance_id
            job.provider = result.instance.provider
            job.started_at = datetime.utcnow()
            job.status = JobStatus.RUNNING
        else:
            job.status = JobStatus.FAILED
            job.error_message = result.error_message
            job.completed_at = datetime.utcnow()
        
        await self._notify_status_change(job)
    
    async def _check_running_jobs(self) -> None:
        """Check status of running jobs."""
        from .providers.base_provider import InstanceStatus
        
        for job in self._jobs.values():
            if job.status not in (JobStatus.RUNNING, JobStatus.PROVISIONING):
                continue
            
            if job.instance_id is None:
                continue
            
            # Get instance status
            instance = await self.orchestrator.get_status(job.instance_id)
            if instance is None:
                continue
            
            # Update cost
            job.total_cost_usd = instance.compute_current_cost()
            
            # Check cost limit
            if job.spec.max_total_cost_usd:
                if job.total_cost_usd >= job.spec.max_total_cost_usd:
                    await self._terminate_job(job, "Cost limit exceeded")
                    continue
            
            # Check time limit
            if job.spec.max_duration_hours:
                max_duration = timedelta(hours=job.spec.max_duration_hours)
                if job.started_at and (datetime.utcnow() - job.started_at) > max_duration:
                    await self._terminate_job(job, "Time limit exceeded")
                    continue
            
            # Check instance status
            if instance.status == InstanceStatus.TERMINATED:
                # Check if this was a preemption
                if "preempted" in (instance.error_message or "").lower():
                    await self._handle_preemption(job)
                else:
                    job.status = JobStatus.COMPLETED
                    job.completed_at = datetime.utcnow()
                    await self._notify_status_change(job)
            
            elif instance.status == InstanceStatus.ERROR:
                job.status = JobStatus.FAILED
                job.error_message = instance.error_message
                job.completed_at = datetime.utcnow()
                await self._notify_status_change(job)
    
    async def _terminate_job(self, job: Job, reason: str) -> None:
        """Terminate a job with reason."""
        if job.instance_id:
            await self.orchestrator.destroy(job.instance_id)
        
        job.status = JobStatus.FAILED
        job.error_message = reason
        job.completed_at = datetime.utcnow()
        
        await self._notify_status_change(job)
    
    async def _handle_preemption(self, job: Job) -> None:
        """Handle spot instance preemption."""
        job.status = JobStatus.PREEMPTED
        await self._notify_status_change(job)
        
        # If we have a checkpoint, try to resume
        if job.last_checkpoint and job.spec.checkpoint_gcs_path:
            # Re-queue with resume checkpoint
            job.spec.resume_from_checkpoint = job.spec.checkpoint_gcs_path
            job.status = JobStatus.QUEUED
            job.instance_id = None
            self._insert_by_priority(job.job_id, JobPriority.HIGH)
            await self._notify_status_change(job)
    
    async def _notify_status_change(self, job: Job) -> None:
        """Notify callbacks of status change."""
        # Persist to Timescale on every status change (Team 2)
        await self._persist_job(job)
        
        # Call registered callbacks
        for callback in self._status_callbacks:
            try:
                callback(job)
            except Exception as e:
                print(f"Callback error: {e}")
        
        # Send webhook if configured
        if job.spec.webhook_url:
            await self._send_webhook(job)
    
    async def _send_webhook(self, job: Job) -> None:
        """Send status webhook."""
        try:
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                await session.post(
                    job.spec.webhook_url,
                    json=job.to_dict(),
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except ImportError:
            print("aiohttp not installed for webhooks")
        except Exception as e:
            print(f"Webhook failed: {e}")
    
    def on_status_change(self, callback: Callable[[Job], None]) -> None:
        """Register a callback for job status changes."""
        self._status_callbacks.append(callback)
    
    async def wait_for_completion(
        self,
        job_id: str,
        timeout_seconds: Optional[float] = None,
    ) -> Job:
        """
        Wait for a job to complete.
        
        Args:
            job_id: Job to wait for
            timeout_seconds: Maximum wait time (None = forever)
            
        Returns:
            Completed job
            
        Raises:
            TimeoutError: If timeout exceeded
            ValueError: If job not found
        """
        start_time = asyncio.get_event_loop().time()
        
        while True:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError(f"Job not found: {job_id}")
            
            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return job
            
            if timeout_seconds is not None:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout_seconds:
                    raise TimeoutError(f"Job {job_id} did not complete in {timeout_seconds}s")
            
            await asyncio.sleep(5.0)
    
    def get_queue_position(self, job_id: str) -> Optional[int]:
        """Get job's position in queue (0-indexed)."""
        try:
            return self._queue.index(job_id)
        except ValueError:
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get job manager statistics."""
        by_status = {}
        for job in self._jobs.values():
            status = job.status.value
            by_status[status] = by_status.get(status, 0) + 1
        
        total_cost = sum(j.total_cost_usd for j in self._jobs.values())
        
        return {
            "total_jobs": len(self._jobs),
            "queued": len(self._queue),
            "jobs_by_status": by_status,
            "total_cost_usd": total_cost,
        }
