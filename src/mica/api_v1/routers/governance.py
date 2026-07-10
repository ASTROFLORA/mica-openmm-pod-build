"""Governance API router.

Exposes per-user governance settings (UI-consumable) and approval actions for
cost-bearing jobs.

Rule:
- Economic executions (paid cloud providers) require human approval by default.
- Users can opt in to autonomous economic execution via settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.routers.jobs import _job_to_response, get_job_manager  # reuse canonical singleton
from mica.infrastructure.job_manager import JobStatus
from mica.security.governance import GovernanceSettings, get_governance_settings_store


router = APIRouter(prefix="/api/v1/governance", tags=["governance"])
logger = logging.getLogger(__name__)


def _settings_read_timeout_seconds() -> float:
    try:
        return max(0.5, float((os.getenv("MICA_GOVERNANCE_READ_TIMEOUT_SECONDS") or "5").strip()))
    except ValueError:
        return 5.0


def _default_settings_response(user_id: str) -> GovernanceSettingsResponse:
    return GovernanceSettingsResponse(
        user_id=user_id,
        allow_autonomous_economic_execution=False,
        updated_at=None,
    )


class GovernanceSettingsResponse(BaseModel):
    user_id: str
    allow_autonomous_economic_execution: bool
    updated_at: Optional[str] = None


class UpdateGovernanceSettingsRequest(BaseModel):
    allow_autonomous_economic_execution: bool = Field(
        ..., description="If true, allow autonomous cost-bearing executions"
    )


@router.get("/settings", response_model=GovernanceSettingsResponse)
async def get_settings(user_id: str = Depends(user_dependency)) -> GovernanceSettingsResponse:
    store = get_governance_settings_store()
    try:
        settings = await asyncio.wait_for(store.get_settings(user_id), timeout=_settings_read_timeout_seconds())
    except Exception as exc:
        logger.warning("Governance settings read degraded for %s: %s", user_id, exc)
        return _default_settings_response(user_id)
    if settings is None:
        logger.warning("Governance settings store returned no row object for %s; using safe default", user_id)
        return _default_settings_response(user_id)
    return GovernanceSettingsResponse(
        user_id=settings.user_id,
        allow_autonomous_economic_execution=settings.allow_autonomous_economic_execution,
        updated_at=settings.updated_at.isoformat() if settings.updated_at else None,
    )


@router.put("/settings", response_model=GovernanceSettingsResponse)
async def update_settings(
    body: UpdateGovernanceSettingsRequest,
    user_id: str = Depends(user_dependency),
) -> GovernanceSettingsResponse:
    store = get_governance_settings_store()
    updated = await store.set_settings(
        GovernanceSettings(
            user_id=user_id,
            allow_autonomous_economic_execution=bool(body.allow_autonomous_economic_execution),
        )
    )
    return GovernanceSettingsResponse(
        user_id=updated.user_id,
        allow_autonomous_economic_execution=updated.allow_autonomous_economic_execution,
        updated_at=updated.updated_at.isoformat() if updated.updated_at else None,
    )


@router.get("/pending-jobs")
async def list_pending_jobs(user_id: str = Depends(user_dependency)) -> Dict[str, Any]:
    manager = get_job_manager()
    jobs = manager.list_jobs(user_id=user_id, status=JobStatus.AWAITING_APPROVAL)
    return {
        "user_id": user_id,
        "total": len(jobs),
        "jobs": [_job_to_response(j, user_id).model_dump() for j in jobs],
    }


@router.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str, user_id: str = Depends(user_dependency)) -> Dict[str, Any]:
    manager = get_job_manager()

    job = await manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if (job.spec.user_id or "") != user_id:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.AWAITING_APPROVAL:
        return {"ok": True, "job": _job_to_response(job, user_id).model_dump()}

    approved = await manager.approve_job(job_id, approved_by=user_id)
    if not approved:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job": _job_to_response(approved, user_id).model_dump()}


@router.post("/jobs/{job_id}/deny")
async def deny_job(
    job_id: str,
    reason: str = "Denied by human",
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    manager = get_job_manager()

    job = await manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if (job.spec.user_id or "") != user_id:
        raise HTTPException(status_code=404, detail="Job not found")

    denied = await manager.deny_job(job_id, denied_by=user_id, reason=reason)
    if not denied:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job": _job_to_response(denied, user_id).model_dump()}
