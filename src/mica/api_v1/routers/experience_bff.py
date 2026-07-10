"""APV-06 Experience BFF HTTP surface — North Star §6.2."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency
from mica.experience.agent_harness import AgentHarnessViewModel, assemble_agent_harness
from mica.experience.bff import (
    ExperienceBffError,
    HomeExperienceModel,
    KnowledgeExperienceModel,
    ResearchExperienceModel,
    SurfaceBinding,
    WorkingSet,
    WorkspaceManifest,
    WorkspaceRoot,
    WorkspaceView,
    add_object_ref_to_working_set,
    assemble_home_experience,
    assemble_knowledge_experience,
    assemble_research_experience,
    build_workspace_manifest,
    create_workspace,
    create_workspace_view,
    create_working_set,
    get_experience_store,
)
from mica.experience.product_events import ProductEventError
from mica.identity.effective_context import EffectiveContext

router = APIRouter(tags=["experience-bff"])

WorkspaceKind = Literal["study", "ad_hoc"]
LayoutMode = Literal["autogrid", "preset", "semantic", "freeform"]
SurfaceFamily = Literal[
    "research",
    "knowledge",
    "evidence",
    "literature",
    "structure",
    "sequence",
    "analysis",
    "compute",
    "protocol",
    "communication",
    "governance",
]


class CreateWorkspaceRequest(BaseModel):
    workspace_kind: WorkspaceKind
    name: str = "Workspace"
    root_study_id: Optional[str] = None
    home_scope_id: Optional[str] = None


class CreateWorkingSetRequest(BaseModel):
    workspace_id: str
    name: str
    root_refs: List[str] = Field(default_factory=list)
    object_refs: List[str] = Field(default_factory=list)


class AddObjectRefRequest(BaseModel):
    object_ref: str


class SurfaceBindingInput(BaseModel):
    object_ref: str
    surface_type: str
    semantic_role: str
    family: SurfaceFamily
    renderer_profile: str = "default"
    source_app: str = "astroflora"


class CreateViewRequest(BaseModel):
    working_set_id: str
    name: str
    layout_mode: LayoutMode = "semantic"
    surface_bindings: List[SurfaceBindingInput] = Field(default_factory=list)
    filter_spec: dict[str, Any] = Field(default_factory=dict)
    grouping_spec: dict[str, Any] = Field(default_factory=dict)
    layout_metadata: dict[str, Any] = Field(default_factory=dict)


def _http_error(exc: ExperienceBffError) -> HTTPException:
    detail = str(exc)
    status = 403 if detail.startswith("permission_denied") else 400
    if "not found" in detail:
        status = 404
    return HTTPException(status_code=status, detail=detail)


@router.get("/api/v1/experience/home", response_model=HomeExperienceModel)
async def experience_home(ctx: EffectiveContext = Depends(effective_context_dependency)):
    return assemble_home_experience(ctx=ctx)


@router.get("/api/v1/experience/research", response_model=ResearchExperienceModel)
async def experience_research(ctx: EffectiveContext = Depends(effective_context_dependency)):
    return assemble_research_experience(ctx=ctx)


@router.get("/api/v1/experience/knowledge", response_model=KnowledgeExperienceModel)
async def experience_knowledge(ctx: EffectiveContext = Depends(effective_context_dependency)):
    return assemble_knowledge_experience(ctx=ctx)


@router.get("/api/v1/experience/agent-harness", response_model=AgentHarnessViewModel)
async def experience_agent_harness(
    session_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    protocol_run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    replay_cursor: Optional[str] = None,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """APV-10 Visual agent harness — chat + DAG + timeline + approvals + artifacts."""
    try:
        return assemble_agent_harness(
            ctx=ctx,
            session_id=session_id or ctx.session_id,
            correlation_id=correlation_id,
            protocol_run_id=protocol_run_id,
            job_id=job_id,
            replay_cursor=replay_cursor,
        )
    except ProductEventError as exc:
        detail = str(exc)
        status = 403 if detail.startswith("permission_denied") else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.get("/api/v1/experience/workspace/{workspace_id}", response_model=WorkspaceManifest)
async def experience_workspace(
    workspace_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return build_workspace_manifest(ctx=ctx, workspace_id=workspace_id)
    except ExperienceBffError as exc:
        raise _http_error(exc) from exc


@router.get("/api/v1/workspaces/{workspace_id}/manifest", response_model=WorkspaceManifest)
async def workspace_manifest(
    workspace_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return build_workspace_manifest(ctx=ctx, workspace_id=workspace_id)
    except ExperienceBffError as exc:
        raise _http_error(exc) from exc


@router.post("/api/v1/workspaces", response_model=WorkspaceRoot, status_code=201)
async def create_workspace_route(
    body: CreateWorkspaceRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return create_workspace(
            ctx=ctx,
            workspace_kind=body.workspace_kind,
            name=body.name,
            root_study_id=body.root_study_id,
            home_scope_id=body.home_scope_id,
        )
    except (ExperienceBffError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/experience/working-sets", response_model=WorkingSet, status_code=201)
async def create_typed_working_set(
    body: CreateWorkingSetRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Typed Working Set (APV-06). Legacy PG `/api/v1/working-sets` remains for P0 rows."""
    try:
        return create_working_set(
            ctx=ctx,
            workspace_id=body.workspace_id,
            name=body.name,
            root_refs=body.root_refs,
            object_refs=body.object_refs,
        )
    except ExperienceBffError as exc:
        raise _http_error(exc) from exc


@router.post("/api/v1/experience/working-sets/{working_set_id}/objects", response_model=WorkingSet)
async def add_typed_working_set_object(
    working_set_id: str,
    body: AddObjectRefRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    try:
        return add_object_ref_to_working_set(
            ctx=ctx,
            working_set_id=working_set_id,
            object_ref=body.object_ref,
        )
    except ExperienceBffError as exc:
        raise _http_error(exc) from exc


@router.post("/api/v1/workspace-views", response_model=WorkspaceView, status_code=201)
async def create_view_route(
    body: CreateViewRequest,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    import uuid

    bindings = [
        SurfaceBinding(
            surface_binding_id=str(uuid.uuid4()),
            working_set_id=body.working_set_id,
            object_ref=b.object_ref,
            surface_type=b.surface_type,
            semantic_role=b.semantic_role,
            renderer_profile=b.renderer_profile,
            family=b.family,
            source_app=b.source_app,
        )
        for b in body.surface_bindings
    ]
    try:
        return create_workspace_view(
            ctx=ctx,
            working_set_id=body.working_set_id,
            name=body.name,
            layout_mode=body.layout_mode,
            surface_bindings=bindings,
            filter_spec=body.filter_spec,
            grouping_spec=body.grouping_spec,
            layout_metadata=body.layout_metadata,
        )
    except ExperienceBffError as exc:
        raise _http_error(exc) from exc


@router.post("/api/v1/workspace-views/{view_id}/materialize", response_model=WorkspaceManifest)
async def materialize_view(
    view_id: str,
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    store = get_experience_store()
    view = store.views.get(view_id)
    if view is None:
        raise HTTPException(status_code=404, detail="View not found")
    working_set = store.working_sets.get(view.working_set_id)
    if working_set is None:
        raise HTTPException(status_code=404, detail="Working set not found")
    try:
        return build_workspace_manifest(ctx=ctx, workspace_id=working_set.workspace_id)
    except ExperienceBffError as exc:
        raise _http_error(exc) from exc
