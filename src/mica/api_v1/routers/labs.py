from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.services.laboratory_service import (
    LaboratoryServiceError,
    create_lab_membership,
    create_laboratory,
    get_laboratory_for_user,
    list_lab_members_for_user,
    list_laboratories_for_user,
)
from mica.tenancy.models import CanonicalRole

router = APIRouter(prefix="/api/v1/labs", tags=["labs"])


class CreateLaboratoryRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    org_ref: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LaboratoryResponse(BaseModel):
    lab_id: str
    owner_user_id: str
    org_ref: Optional[str] = None
    slug: str
    display_name: str
    description: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
    created_at: str
    updated_at: str
    membership_role: Optional[str] = None


class LaboratoryListResponse(BaseModel):
    laboratories: List[LaboratoryResponse]
    total: int


class CreateLabMembershipRequest(BaseModel):
    principal_ref: str = Field(..., min_length=1, max_length=200)
    role: CanonicalRole
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LabMembershipResponse(BaseModel):
    membership_id: str
    lab_id: str
    principal_ref: str
    role: str
    status: str
    invited_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    joined_at: str


class LabMembershipListResponse(BaseModel):
    memberships: List[LabMembershipResponse]
    total: int


@router.post("", response_model=LaboratoryResponse, status_code=201)
async def create_lab(body: CreateLaboratoryRequest, user_id: str = Depends(user_dependency)):
    try:
        payload = await create_laboratory(
            owner_user_id=user_id,
            display_name=body.display_name,
            slug=body.slug,
            description=body.description,
            org_ref=body.org_ref,
            metadata=body.metadata,
        )
    except LaboratoryServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return LaboratoryResponse(**payload)


@router.get("", response_model=LaboratoryListResponse)
async def list_labs(
    archived: bool = Query(False),
    user_id: str = Depends(user_dependency),
):
    payload = await list_laboratories_for_user(user_id=user_id, archived=archived)
    return LaboratoryListResponse(
        laboratories=[LaboratoryResponse(**item) for item in payload],
        total=len(payload),
    )


@router.get("/{lab_id}", response_model=LaboratoryResponse)
async def get_lab(lab_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await get_laboratory_for_user(user_id=user_id, lab_id=lab_id)
    except LaboratoryServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return LaboratoryResponse(**payload)


@router.post("/{lab_id}/members", response_model=LabMembershipResponse, status_code=201)
async def add_lab_member(
    lab_id: str,
    body: CreateLabMembershipRequest,
    user_id: str = Depends(user_dependency),
):
    try:
        payload = await create_lab_membership(
            actor_user_id=user_id,
            lab_id=lab_id,
            principal_ref=body.principal_ref,
            role=body.role,
            metadata=body.metadata,
        )
    except LaboratoryServiceError as exc:
        status_code = 403 if exc.code == "laboratory_admin_required" else 404
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
    return LabMembershipResponse(**payload)


@router.get("/{lab_id}/members", response_model=LabMembershipListResponse)
async def list_lab_members(lab_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await list_lab_members_for_user(user_id=user_id, lab_id=lab_id)
    except LaboratoryServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return LabMembershipListResponse(
        memberships=[LabMembershipResponse(**item) for item in payload],
        total=len(payload),
    )
