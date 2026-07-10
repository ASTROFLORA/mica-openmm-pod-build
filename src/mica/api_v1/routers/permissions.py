"""Product permission evaluation API (APV-02 / North Star §6.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency
from mica.identity.effective_context import EffectiveContext
from mica.tenancy.effective_permission_engine import get_permission_engine
from mica.tenancy.models import PermissionAction, PolicyDecision

router = APIRouter(tags=["permissions"])


class PermissionEvaluateTarget(BaseModel):
    target_ref: str
    action: PermissionAction = PermissionAction.READ


class PermissionEvaluateRequest(BaseModel):
    target_ref: str | None = None
    action: PermissionAction = PermissionAction.READ
    targets: list[PermissionEvaluateTarget] = Field(default_factory=list)


class PermissionEvaluateItem(BaseModel):
    target_ref: str
    action: PermissionAction
    final_decision: PolicyDecision
    allowed: bool
    layers: list[dict[str, Any]]
    policy_snapshot_id: str
    permission_fingerprint: str
    receipt_ref: str | None = None


class PermissionEvaluateResponse(BaseModel):
    effective_context: dict[str, Any]
    policy_snapshot_id: str
    results: list[PermissionEvaluateItem]
    batch: bool = False


@router.post("/api/v1/permissions/evaluate", response_model=PermissionEvaluateResponse)
async def evaluate_permissions(
    body: PermissionEvaluateRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
) -> PermissionEvaluateResponse:
    """Evaluate EffectivePermission for one or many targets under EffectiveContext."""
    engine = get_permission_engine()
    targets: list[dict[str, Any]] = []
    if body.targets:
        targets = [t.model_dump(mode="json") for t in body.targets]
    elif body.target_ref:
        targets = [{"target_ref": body.target_ref, "action": body.action}]
    else:
        raise HTTPException(status_code=400, detail="target_ref or targets required")

    try:
        decisions = engine.evaluate_batch(context=ctx, targets=targets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    results = [
        PermissionEvaluateItem(
            target_ref=d.target_ref,
            action=d.action,
            final_decision=d.final_decision,
            allowed=d.allowed,
            layers=d.layers,
            policy_snapshot_id=d.policy_snapshot_id,
            permission_fingerprint=d.permission_fingerprint,
            receipt_ref=d.receipt_ref,
        )
        for d in decisions
    ]
    snapshot = results[0].policy_snapshot_id if results else engine.policy_snapshot_id()
    bound_ctx = dict(decisions[0].effective_context) if decisions else ctx.model_dump(mode="json")
    return PermissionEvaluateResponse(
        effective_context=bound_ctx,
        policy_snapshot_id=snapshot,
        results=results,
        batch=len(results) > 1,
    )
