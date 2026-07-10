from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.services.knowledge_space_service import (
    KnowledgeSpaceServiceError,
    archive_knowledge_space_for_user,
    capture_membership_snapshot,
    create_knowledge_membership,
    create_knowledge_space,
    get_knowledge_space_for_user,
    get_membership_snapshot_for_user,
    list_knowledge_memberships_for_user,
    list_knowledge_spaces_for_user,
    list_membership_snapshots_for_user,
    update_knowledge_space_for_user,
)

router = APIRouter(prefix="/api/v1/knowledge-spaces", tags=["knowledge-spaces"])

KnowledgeMembershipRelation = Literal["contains", "specializes", "subset_of", "related_domain", "contributes_to"]
KnowledgeExpansionPolicy = Literal["no_expand", "expand_forward", "expand_reverse", "expand_bidirectional"]


class CreateKnowledgeSpaceRequest(BaseModel):
    lab_id: str
    display_name: str = Field(..., min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=4000)
    primary_parent_space_id: Optional[str] = None
    review_cadence: Optional[str] = Field(None, max_length=200)
    health_status: str = Field(default="active", max_length=100)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class UpdateKnowledgeSpaceRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=4000)
    primary_parent_space_id: Optional[str] = None
    review_cadence: Optional[str] = Field(None, max_length=200)
    health_status: Optional[str] = Field(None, max_length=100)
    metadata: Optional[Dict[str, Any]] = None


class KnowledgeSpaceResponse(BaseModel):
    space_id: str
    lab_id: str
    owner_user_id: str
    slug: str
    display_name: str
    description: Optional[str] = None
    primary_parent_space_id: Optional[str] = None
    review_cadence: Optional[str] = None
    health_status: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
    created_at: str
    updated_at: str


class KnowledgeSpaceListResponse(BaseModel):
    spaces: List[KnowledgeSpaceResponse]
    total: int


class CreateKnowledgeMembershipRequest(BaseModel):
    child_space_id: Optional[str] = None
    member_kb_ref: Optional[str] = None
    relation_type: KnowledgeMembershipRelation = "contains"
    expansion_policy: KnowledgeExpansionPolicy = "no_expand"
    primary_parent: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeMembershipResponse(BaseModel):
    membership_id: str
    parent_space_id: str
    child_space_id: Optional[str] = None
    member_kb_ref: Optional[str] = None
    relation_type: str
    expansion_policy: str
    primary_parent: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: str
    archived: bool = False


class KnowledgeMembershipListResponse(BaseModel):
    memberships: List[KnowledgeMembershipResponse]
    total: int


class MembershipSnapshotResponse(BaseModel):
    snapshot_id: str
    space_id: str
    captured_by: str
    snapshot_data: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MembershipSnapshotListResponse(BaseModel):
    snapshots: List[MembershipSnapshotResponse]
    total: int


def _raise_http_error(exc: KnowledgeSpaceServiceError) -> None:
    status_map = {
        "laboratory_not_found": 404,
        "knowledge_space_not_found": 404,
        "knowledge_membership_child_not_found": 404,
        "membership_snapshot_not_found": 404,
        "laboratory_admin_required": 403,
        "knowledge_space_admin_required": 403,
    }
    raise HTTPException(status_code=status_map.get(exc.code, 400), detail=exc.message) from exc


@router.post("", response_model=KnowledgeSpaceResponse, status_code=201)
async def create_space(body: CreateKnowledgeSpaceRequest, user_id: str = Depends(user_dependency)):
    try:
        payload = await create_knowledge_space(
            actor_user_id=user_id,
            lab_id=body.lab_id,
            display_name=body.display_name,
            slug=body.slug,
            description=body.description,
            primary_parent_space_id=body.primary_parent_space_id,
            review_cadence=body.review_cadence,
            health_status=body.health_status,
            metadata=body.metadata,
        )
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return KnowledgeSpaceResponse(**payload)


@router.get("", response_model=KnowledgeSpaceListResponse)
async def list_spaces(
    lab_id: Optional[str] = Query(None),
    archived: bool = Query(False),
    user_id: str = Depends(user_dependency),
):
    payload = await list_knowledge_spaces_for_user(user_id=user_id, lab_id=lab_id, archived=archived)
    return KnowledgeSpaceListResponse(
        spaces=[KnowledgeSpaceResponse(**item) for item in payload],
        total=len(payload),
    )


@router.get("/{space_id}", response_model=KnowledgeSpaceResponse)
async def get_space(space_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await get_knowledge_space_for_user(user_id=user_id, space_id=space_id)
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return KnowledgeSpaceResponse(**payload)


@router.put("/{space_id}", response_model=KnowledgeSpaceResponse)
async def update_space(
    space_id: str,
    body: UpdateKnowledgeSpaceRequest,
    user_id: str = Depends(user_dependency),
):
    try:
        payload = await update_knowledge_space_for_user(
            actor_user_id=user_id,
            space_id=space_id,
            display_name=body.display_name,
            description=body.description,
            primary_parent_space_id=body.primary_parent_space_id,
            review_cadence=body.review_cadence,
            health_status=body.health_status,
            metadata=body.metadata,
        )
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return KnowledgeSpaceResponse(**payload)


@router.delete("/{space_id}", status_code=204)
async def archive_space(space_id: str, user_id: str = Depends(user_dependency)):
    try:
        await archive_knowledge_space_for_user(actor_user_id=user_id, space_id=space_id)
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)


@router.post("/{space_id}/memberships", response_model=KnowledgeMembershipResponse, status_code=201)
async def create_space_membership(
    space_id: str,
    body: CreateKnowledgeMembershipRequest,
    user_id: str = Depends(user_dependency),
):
    try:
        payload = await create_knowledge_membership(
            actor_user_id=user_id,
            parent_space_id=space_id,
            child_space_id=body.child_space_id,
            member_kb_ref=body.member_kb_ref,
            relation_type=body.relation_type,
            expansion_policy=body.expansion_policy,
            primary_parent=body.primary_parent,
            metadata=body.metadata,
        )
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return KnowledgeMembershipResponse(**payload)


@router.get("/{space_id}/memberships", response_model=KnowledgeMembershipListResponse)
async def list_space_memberships(space_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await list_knowledge_memberships_for_user(user_id=user_id, space_id=space_id)
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return KnowledgeMembershipListResponse(
        memberships=[KnowledgeMembershipResponse(**item) for item in payload],
        total=len(payload),
    )


@router.post("/{space_id}/snapshots", response_model=MembershipSnapshotResponse, status_code=201)
async def create_space_snapshot(space_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await capture_membership_snapshot(actor_user_id=user_id, space_id=space_id)
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return MembershipSnapshotResponse(**payload)


@router.get("/{space_id}/snapshots", response_model=MembershipSnapshotListResponse)
async def list_space_snapshots(space_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await list_membership_snapshots_for_user(user_id=user_id, space_id=space_id)
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return MembershipSnapshotListResponse(
        snapshots=[MembershipSnapshotResponse(**item) for item in payload],
        total=len(payload),
    )


@router.get("/{space_id}/snapshots/{snapshot_id}", response_model=MembershipSnapshotResponse)
async def get_space_snapshot(space_id: str, snapshot_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await get_membership_snapshot_for_user(user_id=user_id, space_id=space_id, snapshot_id=snapshot_id)
    except KnowledgeSpaceServiceError as exc:
        _raise_http_error(exc)
    return MembershipSnapshotResponse(**payload)
