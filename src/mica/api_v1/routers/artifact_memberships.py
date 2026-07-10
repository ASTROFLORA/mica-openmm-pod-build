"""APV-04 ArtifactMembership HTTP surface."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency
from mica.artifacts.membership import (
    ArtifactMembership,
    ArtifactMembershipError,
    CrossScopeOperation,
    CrossScopeResult,
    ScopedResourceRef,
    attach_artifact_membership,
    execute_cross_scope_operation,
    get_membership_store,
    scoped_resource_from_artifact,
)
from mica.identity.effective_context import EffectiveContext
from mica.tenancy.models import PermissionAction
from mica.tenancy.pep import require_permission_http

router = APIRouter(prefix="/api/v1/artifact-memberships", tags=["artifact-memberships"])

ContainerType = Literal["knowledge_space", "study", "workspace", "research_line"]


class AttachMembershipRequest(BaseModel):
    artifact_id: str
    container_type: ContainerType
    container_id: str
    semantic_role: str = "attached"
    grantee_user_id: Optional[str] = None
    acl_role: str = "viewer"


class CrossScopeRequest(BaseModel):
    operation: Literal["link", "share", "copy", "fork", "propose_promotion", "transfer"]
    source_artifact_id: str
    source_home_scope_id: str
    destination_scope_id: str
    expected_permission_fingerprint: str
    idempotency_key: str
    grantee_user_id: Optional[str] = None
    semantic_role: str = "attached"
    acl_role: str = "viewer"
    container_type: Optional[ContainerType] = "study"
    container_id: Optional[str] = None
    visibility: Literal["private", "scope", "shared", "public"] = "private"


class MembershipListResponse(BaseModel):
    memberships: List[ArtifactMembership]
    total: int


@router.post("", response_model=ArtifactMembership, status_code=201)
async def create_membership(
    body: AttachMembershipRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Attach artifact to a container and register membership grant."""
    # Container UPDATE authority for study; other containers accept actor attach for now.
    if body.container_type == "study":
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=body.container_id,
            action=PermissionAction.UPDATE,
        )
    try:
        return attach_artifact_membership(
            ctx=ctx,
            artifact_id=body.artifact_id,
            container_type=body.container_type,
            container_id=body.container_id,
            semantic_role=body.semantic_role,
            grantee_user_id=body.grantee_user_id,
            acl_role=body.acl_role,
        )
    except ArtifactMembershipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/by-container/{container_type}/{container_id}", response_model=MembershipListResponse)
async def list_memberships_for_container(
    container_type: ContainerType,
    container_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    if container_type == "study":
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=container_id,
            action=PermissionAction.READ,
        )
    rows = get_membership_store().list_for_container(container_type, container_id)
    return MembershipListResponse(memberships=rows, total=len(rows))


@router.get("/by-artifact/{artifact_id}", response_model=MembershipListResponse)
async def list_memberships_for_artifact(
    artifact_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    require_permission_http(
        ctx=ctx,
        resource_type="artifact",
        resource_id=artifact_id,
        action=PermissionAction.READ,
    )
    rows = get_membership_store().list_for_artifact(artifact_id)
    return MembershipListResponse(memberships=rows, total=len(rows))


@router.post("/cross-scope", response_model=CrossScopeResult)
async def cross_scope(
    body: CrossScopeRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Execute link/share/copy/fork/propose_promotion (transfer stubbed)."""
    require_permission_http(
        ctx=ctx,
        resource_type="artifact",
        resource_id=body.source_artifact_id,
        action=PermissionAction.READ,
    )
    if body.operation in ("share", "copy", "fork", "link") and body.container_type == "study" and body.container_id:
        require_permission_http(
            ctx=ctx,
            resource_type="study",
            resource_id=body.container_id,
            action=PermissionAction.UPDATE,
        )
    source = scoped_resource_from_artifact(
        artifact_id=body.source_artifact_id,
        home_scope_id=body.source_home_scope_id,
        created_by=ctx.actor_user_id,
        visibility=body.visibility,
    )
    op = CrossScopeOperation(
        operation=body.operation,
        source=source,
        destination_scope_id=body.destination_scope_id,
        expected_permission_fingerprint=body.expected_permission_fingerprint,
        idempotency_key=body.idempotency_key,
        grantee_user_id=body.grantee_user_id,
        semantic_role=body.semantic_role,
        acl_role=body.acl_role,
        container_type=body.container_type,
        container_id=body.container_id,
    )
    try:
        return execute_cross_scope_operation(ctx=ctx, operation=op)
    except ArtifactMembershipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/visible-artifacts")
async def list_visible_artifact_ids(
    ctx: EffectiveContext = Depends(effective_context_dependency),
) -> dict[str, Any]:
    """Artifact IDs visible via membership (complement to owner-SQL list)."""
    ids = sorted(get_membership_store().list_visible_artifact_ids(ctx.actor_user_id))
    return {"artifact_ids": ids, "total": len(ids)}
