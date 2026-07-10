"""APV-08 Astroflora context router HTTP surface."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency, user_dependency
from mica.experience.context_router import (
    ActiveContext,
    AppLocation,
    ContextRouterError,
    SemanticSidebarProjection,
    WorkspaceHandoffResult,
    encode_app_location,
    handoff_to_workspace,
    parse_app_location,
    project_semantic_sidebar,
    resolve_active_context,
)
from mica.identity.effective_context import EffectiveContext

router = APIRouter(prefix="/api/v1/location", tags=["context-router"])

AppId = Literal[
    "home",
    "research",
    "knowledge",
    "workspace",
    "drive",
    "pipelines",
    "agents",
    "labs",
]


class AppLocationIn(BaseModel):
    app: AppId
    scope_id: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    view: Optional[str] = None
    working_set_id: Optional[str] = None
    surface_id: Optional[str] = None
    source_app: Optional[AppId] = None


class ResolveLocationRequest(BaseModel):
    location: AppLocationIn
    session_id: Optional[str] = None


class EncodeLocationResponse(BaseModel):
    url: str
    location: AppLocation


class ParseLocationRequest(BaseModel):
    url: str


class WorkspaceHandoffRequest(BaseModel):
    source: AppLocationIn
    workspace_id: Optional[str] = None
    working_set_id: Optional[str] = None
    view: Optional[str] = None
    session_id: Optional[str] = None


def _http(exc: Exception) -> HTTPException:
    detail = str(exc)
    status = 403 if detail.startswith("permission_denied") else 400
    return HTTPException(status_code=status, detail=detail)


def _to_location(body: AppLocationIn) -> AppLocation:
    return AppLocation(**body.model_dump())


@router.post("/resolve", response_model=ActiveContext)
async def resolve_location(
    body: ResolveLocationRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Resolve AppLocation → ActiveContext. Fingerprint is server authority."""
    try:
        location = _to_location(body.location)
        return resolve_active_context(
            identity=user_id,
            location=location,
            session_id=body.session_id or ctx.session_id,
            policy_snapshot_id=ctx.policy_snapshot_id,
        )
    except (ContextRouterError, ValueError) as exc:
        raise _http(exc) from exc


@router.post("/encode", response_model=EncodeLocationResponse)
async def encode_location(
    body: AppLocationIn,
    _ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        location = _to_location(body)
        return EncodeLocationResponse(url=encode_app_location(location), location=location)
    except (ContextRouterError, ValueError) as exc:
        raise _http(exc) from exc


@router.post("/parse", response_model=AppLocation)
async def parse_location(
    body: ParseLocationRequest,
    _ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return parse_app_location(body.url)
    except (ContextRouterError, ValueError) as exc:
        raise _http(exc) from exc


@router.post("/workspace-handoff", response_model=WorkspaceHandoffResult)
async def workspace_handoff(
    body: WorkspaceHandoffRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Open in Workspace preserving source application and resource identity."""
    try:
        return handoff_to_workspace(
            identity=user_id,
            source=_to_location(body.source),
            workspace_id=body.workspace_id,
            working_set_id=body.working_set_id,
            view=body.view,
            session_id=body.session_id or ctx.session_id,
        )
    except (ContextRouterError, ValueError) as exc:
        raise _http(exc) from exc


@router.post("/sidebar", response_model=SemanticSidebarProjection)
async def sidebar_projection(
    body: ResolveLocationRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        active = resolve_active_context(
            identity=user_id,
            location=_to_location(body.location),
            session_id=body.session_id or ctx.session_id,
            policy_snapshot_id=ctx.policy_snapshot_id,
        )
        return project_semantic_sidebar(active=active)
    except (ContextRouterError, ValueError) as exc:
        raise _http(exc) from exc
