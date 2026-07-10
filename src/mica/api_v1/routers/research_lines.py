from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.services.research_line_service import (
    ResearchLineServiceError,
    archive_research_line_for_user,
    create_research_line,
    create_research_line_space_link,
    get_research_line_for_user,
    link_study_to_research_line_for_user,
    list_research_line_space_links_for_user,
    list_research_lines_for_user,
    update_research_line_for_user,
)

router = APIRouter(prefix="/api/v1/research-lines", tags=["research-lines"])

ResearchLineStatus = Literal["proposed", "active", "paused", "archived"]
ResearchLineSpaceRelation = Literal["primary_domain", "related_domain", "supports", "depends_on"]


class CreateResearchLineRequest(BaseModel):
    lab_id: str
    display_name: str = Field(..., min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=4000)
    primary_question: Optional[str] = Field(None, max_length=4000)
    status: ResearchLineStatus = "proposed"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class UpdateResearchLineRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=4000)
    primary_question: Optional[str] = Field(None, max_length=4000)
    status: Optional[ResearchLineStatus] = None
    metadata: Optional[Dict[str, Any]] = None


class ResearchLineResponse(BaseModel):
    line_id: str
    lab_id: str
    owner_user_id: str
    slug: str
    display_name: str
    description: Optional[str] = None
    primary_question: Optional[str] = None
    status: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
    created_at: str
    updated_at: str
    study_count: int = 0
    space_count: int = 0


class ResearchLineListResponse(BaseModel):
    lines: List[ResearchLineResponse]
    total: int


class CreateResearchLineSpaceLinkRequest(BaseModel):
    space_id: str
    relation_type: ResearchLineSpaceRelation = "related_domain"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ResearchLineSpaceLinkResponse(BaseModel):
    link_id: str
    line_id: str
    space_id: str
    relation_type: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: str


class ResearchLineSpaceLinkListResponse(BaseModel):
    links: List[ResearchLineSpaceLinkResponse]
    total: int


class ResearchLineStudyLinkResponse(BaseModel):
    study_id: str
    lab_id: Optional[str] = None
    research_line_id: Optional[str] = None
    updated_at: str


def _raise_http_error(exc: ResearchLineServiceError) -> None:
    status_map = {
        "laboratory_not_found": 404,
        "research_line_not_found": 404,
        "knowledge_space_not_found": 404,
        "study_not_found": 404,
        "laboratory_admin_required": 403,
        "research_line_admin_required": 403,
    }
    raise HTTPException(status_code=status_map.get(exc.code, 400), detail=exc.message) from exc


@router.post("", response_model=ResearchLineResponse, status_code=201)
async def create_line(body: CreateResearchLineRequest, user_id: str = Depends(user_dependency)):
    try:
        payload = await create_research_line(
            actor_user_id=user_id,
            lab_id=body.lab_id,
            display_name=body.display_name,
            slug=body.slug,
            description=body.description,
            primary_question=body.primary_question,
            status=body.status,
            metadata=body.metadata,
        )
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)
    return ResearchLineResponse(**payload)


@router.get("", response_model=ResearchLineListResponse)
async def list_lines(
    lab_id: Optional[str] = Query(None),
    archived: bool = Query(False),
    user_id: str = Depends(user_dependency),
):
    payload = await list_research_lines_for_user(user_id=user_id, lab_id=lab_id, archived=archived)
    return ResearchLineListResponse(
        lines=[ResearchLineResponse(**item) for item in payload],
        total=len(payload),
    )


@router.get("/{line_id}", response_model=ResearchLineResponse)
async def get_line(line_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await get_research_line_for_user(user_id=user_id, line_id=line_id)
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)
    return ResearchLineResponse(**payload)


@router.put("/{line_id}", response_model=ResearchLineResponse)
async def update_line(
    line_id: str,
    body: UpdateResearchLineRequest,
    user_id: str = Depends(user_dependency),
):
    try:
        payload = await update_research_line_for_user(
            actor_user_id=user_id,
            line_id=line_id,
            display_name=body.display_name,
            description=body.description,
            primary_question=body.primary_question,
            status=body.status,
            metadata=body.metadata,
        )
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)
    return ResearchLineResponse(**payload)


@router.delete("/{line_id}", status_code=204)
async def archive_line(line_id: str, user_id: str = Depends(user_dependency)):
    try:
        await archive_research_line_for_user(actor_user_id=user_id, line_id=line_id)
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)


@router.post("/{line_id}/spaces", response_model=ResearchLineSpaceLinkResponse, status_code=201)
async def create_line_space_link(
    line_id: str,
    body: CreateResearchLineSpaceLinkRequest,
    user_id: str = Depends(user_dependency),
):
    try:
        payload = await create_research_line_space_link(
            actor_user_id=user_id,
            line_id=line_id,
            space_id=body.space_id,
            relation_type=body.relation_type,
            metadata=body.metadata,
        )
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)
    return ResearchLineSpaceLinkResponse(**payload)


@router.get("/{line_id}/spaces", response_model=ResearchLineSpaceLinkListResponse)
async def list_line_space_links(line_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await list_research_line_space_links_for_user(user_id=user_id, line_id=line_id)
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)
    return ResearchLineSpaceLinkListResponse(
        links=[ResearchLineSpaceLinkResponse(**item) for item in payload],
        total=len(payload),
    )


@router.post("/{line_id}/studies/{study_id}", response_model=ResearchLineStudyLinkResponse, status_code=201)
async def link_study_to_line(line_id: str, study_id: str, user_id: str = Depends(user_dependency)):
    try:
        payload = await link_study_to_research_line_for_user(
            actor_user_id=user_id,
            line_id=line_id,
            study_id=study_id,
        )
    except ResearchLineServiceError as exc:
        _raise_http_error(exc)
    return ResearchLineStudyLinkResponse(**payload)
