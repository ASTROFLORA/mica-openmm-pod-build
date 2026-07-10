"""APV-05 EvidenceBinding HTTP surface."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency
from mica.artifacts.evidence_binding import (
    EvidenceBinding,
    EvidenceBindingError,
    Finding,
    create_evidence_binding,
    create_finding,
    get_evidence_binding_store,
    promote_finding,
)
from mica.artifacts.membership import get_membership_store
from mica.identity.effective_context import EffectiveContext
from mica.tenancy.models import PermissionAction
from mica.tenancy.pep import require_permission_http

router = APIRouter(tags=["evidence-bindings"])

SemanticRole = Literal["supports", "contradicts", "context", "method", "result"]


class CreateFindingRequest(BaseModel):
    statement: str = Field(..., min_length=1)
    home_scope_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateBindingRequest(BaseModel):
    finding_id: str
    artifact_membership_id: str
    semantic_role: SemanticRole
    evidence_path_id: Optional[str] = None
    excerpt_selector: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BindingListResponse(BaseModel):
    bindings: List[EvidenceBinding]
    total: int


class FindingListResponse(BaseModel):
    findings: List[Finding]
    total: int


@router.post("/api/v1/findings", response_model=Finding, status_code=201)
async def create_finding_route(
    body: CreateFindingRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return create_finding(
            ctx=ctx,
            statement=body.statement,
            home_scope_id=body.home_scope_id,
            metadata=body.metadata,
        )
    except EvidenceBindingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/findings/{finding_id}", response_model=Finding)
async def get_finding_route(
    finding_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    finding = get_evidence_binding_store().get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.post("/api/v1/findings/{finding_id}/promote", response_model=Finding)
async def promote_finding_route(
    finding_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return promote_finding(ctx=ctx, finding_id=finding_id)
    except EvidenceBindingError as exc:
        detail = str(exc)
        status = 409 if detail.startswith("no_path_no_promoted_finding") else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/api/v1/evidence-bindings", response_model=EvidenceBinding, status_code=201)
async def create_binding_route(
    body: CreateBindingRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    membership = get_membership_store().get(body.artifact_membership_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="ArtifactMembership not found")
    require_permission_http(
        ctx=ctx,
        resource_type="artifact",
        resource_id=membership.artifact_id,
        action=PermissionAction.READ,
    )
    try:
        return create_evidence_binding(
            ctx=ctx,
            finding_id=body.finding_id,
            artifact_membership_id=body.artifact_membership_id,
            semantic_role=body.semantic_role,
            evidence_path_id=body.evidence_path_id,
            excerpt_selector=body.excerpt_selector,
            metadata=body.metadata,
        )
    except EvidenceBindingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/findings/{finding_id}/bindings", response_model=BindingListResponse)
async def list_bindings_for_finding(
    finding_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    store = get_evidence_binding_store()
    if store.get_finding(finding_id) is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    rows = store.list_bindings_for_finding(finding_id)
    return BindingListResponse(bindings=rows, total=len(rows))
