"""
Jobs API Router - Control-plane for cloud GPU job management

Provides HTTP endpoints for:
- POST /api/v1/jobs/submit - Submit new job
- GET /api/v1/jobs/{job_id} - Get job status
- GET /api/v1/jobs - List user jobs
- POST /api/v1/jobs/{job_id}/cancel - Cancel running job

Integrates with JobManager (Team 2 persistence) and Mock/Real providers.

Author: Team 1 (Core App)
Date: 2026-01-21
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
import inspect
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency

# Import from infrastructure (Team 2)
from mica.infrastructure.job_manager import (
    JobManager,
    JobSpec,
    Job,
    JobStatus,
    JobPriority,
)
from mica.infrastructure.cloud_orchestrator import CloudOrchestrator
from mica.infrastructure.providers.mock_provider import MockProvider
from mica.infrastructure.providers.vast_provider import VastProvider
from mica.infrastructure.providers.runpod_provider import RunPodProvider

from mica.security.governance import get_governance_settings_store


router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


# -----------------------------
# Request/Response Models
# -----------------------------


class SubmitJobRequest(BaseModel):
    """Request body for job submission."""
    
    # Identity
    name: str = Field(..., description="Human-readable job name")
    worker_type: str = Field(default="generic", description="Worker type: biodynamo, alchemist, smic, chronosfold")
    
    # Resource requirements
    gpu_type: str = Field(default="L40S", description="GPU type: L40S, A100, H100")
    gpu_count: int = Field(default=1, ge=1, le=8, description="Number of GPUs")
    min_ram_gb: float = Field(default=32.0, ge=8.0, description="Minimum RAM in GB")
    min_disk_gb: float = Field(default=100.0, ge=10.0, description="Minimum disk in GB")
    
    # Docker configuration
    docker_image: str = Field(default="nvcr.io/nvidia/pytorch:24.01-py3", description="Docker image")
    docker_command: Optional[str] = Field(default=None, description="Command to run in container")
    env_vars: Dict[str, str] = Field(default_factory=dict, description="Environment variables")
    
    # Cost constraints
    max_price_per_hour: Optional[float] = Field(default=None, ge=0.0, description="Max price per hour USD")
    max_total_cost_usd: Optional[float] = Field(default=None, ge=0.0, description="Max total cost USD")
    prefer_spot: bool = Field(default=True, description="Prefer spot/preemptible instances")
    
    # Time constraints
    max_duration_hours: Optional[float] = Field(default=None, ge=0.1, description="Max duration hours")
    
    # Priority
    priority: str = Field(default="NORMAL", description="Priority: LOW, NORMAL, HIGH, CRITICAL")
    
    # Checkpoint configuration
    checkpoint_interval_minutes: int = Field(default=30, ge=5, description="Checkpoint interval")
    checkpoint_gcs_path: Optional[str] = Field(default=None, description="GCS path for checkpoints")
    resume_from_checkpoint: Optional[str] = Field(default=None, description="Resume from checkpoint path")
    
    # Webhook
    webhook_url: Optional[str] = Field(default=None, description="Webhook URL for status updates")


class JobResponse(BaseModel):
    """Unified job response."""
    
    # Core fields
    job_id: str
    name: str
    user_id: str
    worker_type: str
    status: str
    provider: Optional[str]
    
    # Timestamps
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    
    # Resources
    gpu_type: str
    gpu_count: int
    instance_id: Optional[str]
    
    # Cost tracking
    cost_usd: Optional[float]
    estimated_cost_usd: Optional[float]
    
    # Execution details
    output_gcs_path: Optional[str]
    error_message: Optional[str]
    exit_code: Optional[int]
    
    # Metadata
    metadata: Dict[str, Any]


class JobListResponse(BaseModel):
    """Response for listing jobs."""
    
    jobs: List[JobResponse]
    total: int
    user_id: str


# -----------------------------
# JobManager Singleton
# -----------------------------


_job_manager_singleton: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Get or create JobManager singleton."""
    global _job_manager_singleton
    
    if _job_manager_singleton is not None:
        return _job_manager_singleton
    
    # Initialize orchestrator.
    # Prefer real providers when credentials are present; fall back to mock.
    orchestrator = CloudOrchestrator()

    registered_any = False
    try:
        if (os.getenv("VAST_API_KEY") or "").strip():
            orchestrator.register_provider(VastProvider())
            registered_any = True
    except Exception:
        # Provider is optional; keep server booting.
        pass

    try:
        if (os.getenv("RUNPOD_API_KEY") or "").strip():
            orchestrator.register_provider(RunPodProvider(api_key=os.getenv("RUNPOD_API_KEY")))
            registered_any = True
    except Exception:
        pass

    if not registered_any:
        orchestrator.register_provider(MockProvider())
    
    _job_manager_singleton = JobManager(orchestrator)
    return _job_manager_singleton


# -----------------------------
# Helper Functions
# -----------------------------


def _job_to_response(job: Job, user_id: str) -> JobResponse:
    """Convert Job to JobResponse."""
    # Tests in `tests/api_v1/test_jobs_api.py` use very lightweight mocks
    # that don't always have the full JobSpec shape. Be tolerant.
    spec = getattr(job, "spec", None)
    spec_name = getattr(spec, "name", None) if spec is not None else None
    spec_worker_type = getattr(spec, "worker_type", None) if spec is not None else None
    spec_user_id = getattr(spec, "user_id", None) if spec is not None else None
    spec_gpu_type = getattr(spec, "gpu_type", None) if spec is not None else None
    spec_gpu_count = getattr(spec, "gpu_count", None) if spec is not None else None

    return JobResponse(
        job_id=job.job_id,
        name=spec_name or getattr(job, "name", ""),
        user_id=user_id,
        worker_type=spec_worker_type or getattr(job, "worker_type", "generic"),
        status=job.status.value,
        provider=job.provider,
        created_at=job.created_at.isoformat() if job.created_at else "",
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        gpu_type=spec_gpu_type or getattr(job, "gpu_type", ""),
        gpu_count=int(spec_gpu_count or getattr(job, "gpu_count", 1) or 1),
        instance_id=job.instance_id,
        cost_usd=getattr(job, "total_cost_usd", None),
        estimated_cost_usd=None,
        output_gcs_path=getattr(spec, "output_data_path", None) if spec is not None else getattr(job, "output_gcs_path", None),
        error_message=job.error_message,
        exit_code=job.exit_code,
        metadata=(getattr(spec, "metadata", None) if spec is not None else getattr(job, "metadata", None)) or {},
    )


async def _maybe_await(value: Any) -> Any:
    """Allow compatibility with both sync and async manager implementations/mocks."""
    return await value if inspect.isawaitable(value) else value


def _parse_priority(priority_str: str) -> JobPriority:
    """Parse priority string to JobPriority enum."""
    try:
        return JobPriority[priority_str.upper()]
    except KeyError:
        return JobPriority.NORMAL


# -----------------------------
# Router Endpoints
# -----------------------------


@router.post("/submit", response_model=JobResponse)
async def submit_job(
    request: SubmitJobRequest,
    user_id: str = Depends(user_dependency),
) -> JobResponse:
    """
    Submit a new cloud GPU job.
    
    **Example**:
    ```json
    {
      "name": "ChronosFold Training Run",
      "worker_type": "chronosfold",
      "gpu_type": "A100",
      "gpu_count": 1,
      "docker_image": "ghcr.io/chronosfold/trainer:latest",
      "docker_command": "python train.py --epochs 100",
      "max_price_per_hour": 2.5,
      "checkpoint_gcs_path": "gs://my-bucket/checkpoints/"
    }
    ```
    
    **Returns**: Job details with job_id for tracking
    """
    manager = get_job_manager()

    # Governance: treat jobs as "economic" when any real paid provider is available.
    # Default: require human approval unless the user opted into autonomous execution.
    orchestrator = getattr(manager, "orchestrator", None)
    provider_names = set(getattr(orchestrator, "providers", {}).keys()) if orchestrator is not None else set()
    economic_environment = any(p != "mock" for p in provider_names)
    governance_store = get_governance_settings_store()
    settings = await governance_store.get_settings(user_id)
    
    # Convert request to JobSpec
    spec = JobSpec(
        name=request.name,
        worker_type=request.worker_type,
        user_id=user_id,
        gpu_type=request.gpu_type,
        gpu_count=request.gpu_count,
        min_ram_gb=request.min_ram_gb,
        min_disk_gb=request.min_disk_gb,
        docker_image=request.docker_image,
        docker_command=request.docker_command,
        env_vars=request.env_vars,
        max_price_per_hour=request.max_price_per_hour,
        max_total_cost_usd=request.max_total_cost_usd,
        prefer_spot=request.prefer_spot,
        max_duration_hours=request.max_duration_hours,
        priority=_parse_priority(request.priority),
        checkpoint_interval_minutes=request.checkpoint_interval_minutes,
        checkpoint_gcs_path=request.checkpoint_gcs_path,
        resume_from_checkpoint=request.resume_from_checkpoint,
        webhook_url=request.webhook_url,
    )
    
    try:
        if economic_environment and not settings.allow_autonomous_economic_execution:
            # Create the job but do not enqueue/provision until explicitly approved.
            if not isinstance(spec.metadata, dict):
                spec.metadata = {}
            spec.metadata["requires_human_approval"] = True
            spec.metadata["governance"] = {
                "reason": "Economic execution requires human approval",
                "economic_environment": True,
            }
            job = await manager.create_job(spec=spec, status=JobStatus.AWAITING_APPROVAL, enqueue=False)
        else:
            job = await manager.submit(spec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to submit job")
    
    return _job_to_response(job, user_id)


@router.get("/health")
async def jobs_health() -> Dict[str, Any]:
    """Health check for jobs subsystem."""
    manager = get_job_manager()
    
    # Count jobs by status.
    # Keep this static route ahead of /{job_id} so FastAPI never treats
    # "health" as a dynamic job identifier.
    all_jobs = await _maybe_await(manager.list_jobs())
    status_counts = {}
    for status in JobStatus:
        status_counts[status.value] = sum(1 for j in all_jobs if j.status == status)
    
    return {
        "ok": True,
        "service": "jobs_api",
        "total_jobs": len(all_jobs),
        "status_breakdown": status_counts,
    }


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    user_id: str = Depends(user_dependency),
) -> JobResponse:
    """
    Get status and details of a specific job.
    
    **Returns**: Current job state including status, cost, and output paths
    """
    manager = get_job_manager()
    
    job = await manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Authz: avoid IDOR and job enumeration.
    if (job.spec.user_id or "") != user_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    return _job_to_response(job, user_id)


@router.get("", response_model=JobListResponse)
async def list_jobs(
    user_id: str = Depends(user_dependency),
    status: Optional[str] = Query(None, description="Filter by status: queued, running, completed, failed, cancelled"),
    worker_type: Optional[str] = Query(None, description="Filter by worker type"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
) -> JobListResponse:
    """
    List jobs for the authenticated user with optional filters.
    
    **Query Parameters**:
    - `status`: Filter by job status
    - `worker_type`: Filter by worker type (biodynamo, chronosfold, etc.)
    - `limit`: Max number of results (default: 100)
    
    **Returns**: List of jobs with total count
    """
    manager = get_job_manager()
    
    # Parse status filter
    status_filter = None
    if status:
        try:
            status_filter = JobStatus(status.lower())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    
    # List jobs scoped to the authenticated user.
    jobs = await _maybe_await(manager.list_jobs(user_id=user_id, status=status_filter))
    
    # Apply additional filters
    if worker_type:
        jobs = [j for j in jobs if (j.spec.worker_type or "") == worker_type]
    
    # Apply limit
    jobs = jobs[:limit]
    
    return JobListResponse(
        jobs=[_job_to_response(j, user_id) for j in jobs],
        total=len(jobs),
        user_id=user_id,
    )


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """
    Cancel a running or queued job.
    
    **Returns**: Success status
    """
    manager = get_job_manager()
    
    # Verify job exists
    job = await _maybe_await(manager.get_job(job_id))
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Authz
    if (job.spec.user_id or "") != user_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    # Check if cancellable
    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel job in status {job.status.value}"
        )
    
    try:
        success = await manager.cancel(job_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to cancel job")
    
    if not success:
        raise HTTPException(status_code=500, detail="Cancellation failed")
    
    return {
        "ok": True,
        "job_id": job_id,
        "message": "Job cancellation initiated",
    }

