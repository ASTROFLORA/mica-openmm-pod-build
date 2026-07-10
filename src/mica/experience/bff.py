"""APV-06 Experience BFF — scope-filtered product read models.

Authority: North Star V0.6 §5.7 / §6.2 / APV-06
Hard gate: scope-filtered, permission-safe responses.

Consumes: EffectiveContext, ArtifactMembership, EvidenceBinding/Finding stores.
Does not own: frontend geometry, renderer registry, event envelope (APV-07).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mica.artifacts.evidence_binding import FindingStatus, get_evidence_binding_store
from mica.artifacts.membership import get_membership_store
from mica.identity.effective_context import EffectiveContext, personal_home_scope_id

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


class ExperienceBffError(ValueError):
    """Fail-closed experience / workspace composition error."""


class WorkspaceRoot(BaseModel):
    workspace_id: str
    workspace_kind: WorkspaceKind
    home_scope_id: str
    root_study_id: str | None = None
    created_by: str
    version: int = 1
    name: str = "Workspace"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_kind(self) -> "WorkspaceRoot":
        if self.workspace_kind == "study":
            if not self.root_study_id:
                raise ValueError("workspace_kind=study requires exactly one root_study_id")
        elif self.workspace_kind == "ad_hoc":
            if self.root_study_id:
                raise ValueError("workspace_kind=ad_hoc prohibits root_study_id")
        return self


class WorkingSet(BaseModel):
    """Typed Working Set — stores references, never object copies."""

    working_set_id: str
    workspace_id: str
    home_scope_id: str
    name: str
    root_refs: list[str] = Field(default_factory=list)
    object_refs: list[str] = Field(default_factory=list)
    created_by: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class SurfaceBinding(BaseModel):
    surface_binding_id: str
    working_set_id: str
    object_ref: str
    surface_type: str
    semantic_role: str
    renderer_profile: str = "default"
    family: SurfaceFamily
    source_app: str = "astroflora"


class WorkspaceView(BaseModel):
    view_id: str
    working_set_id: str
    name: str
    layout_mode: LayoutMode = "semantic"
    surface_binding_ids: list[str] = Field(default_factory=list)
    filter_spec: dict[str, Any] = Field(default_factory=dict)
    grouping_spec: dict[str, Any] = Field(default_factory=dict)
    layout_metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkspaceManifest(BaseModel):
    """BFF projection for frontend Surface registry resolution."""

    workspace: WorkspaceRoot
    working_sets: list[WorkingSet] = Field(default_factory=list)
    views: list[WorkspaceView] = Field(default_factory=list)
    surface_bindings: list[SurfaceBinding] = Field(default_factory=list)
    permission_fingerprint: str
    active_scope_id: str
    receipt_id: str


class HomeExperienceModel(BaseModel):
    actor_user_id: str
    active_scope_id: str
    home_scope_id: str
    permission_fingerprint: str
    workspace_summaries: list[dict[str, Any]] = Field(default_factory=list)
    recent_working_set_ids: list[str] = Field(default_factory=list)
    open_finding_counts: dict[str, int] = Field(default_factory=dict)
    visible_artifact_count: int = 0


class ResearchExperienceModel(BaseModel):
    actor_user_id: str
    active_scope_id: str
    permission_fingerprint: str
    study_workspace_ids: list[str] = Field(default_factory=list)
    draft_finding_ids: list[str] = Field(default_factory=list)
    promoted_finding_ids: list[str] = Field(default_factory=list)
    membership_artifact_ids: list[str] = Field(default_factory=list)


class KnowledgeExperienceModel(BaseModel):
    actor_user_id: str
    active_scope_id: str
    permission_fingerprint: str
    promoted_finding_ids: list[str] = Field(default_factory=list)
    evidence_binding_ids: list[str] = Field(default_factory=list)
    path_backed_binding_ids: list[str] = Field(default_factory=list)


class ExperienceStore:
    def __init__(self) -> None:
        self.workspaces: dict[str, WorkspaceRoot] = {}
        self.working_sets: dict[str, WorkingSet] = {}
        self.views: dict[str, WorkspaceView] = {}
        self.bindings: dict[str, SurfaceBinding] = {}

    def clear(self) -> None:
        self.workspaces.clear()
        self.working_sets.clear()
        self.views.clear()
        self.bindings.clear()


_STORE: ExperienceStore | None = None


def get_experience_store() -> ExperienceStore:
    global _STORE
    if _STORE is None:
        _STORE = ExperienceStore()
    return _STORE


def reset_experience_store_for_tests() -> ExperienceStore:
    global _STORE
    _STORE = ExperienceStore()
    return _STORE


def _receipt(kind: str, *parts: str) -> str:
    material = ":".join([kind, *parts, uuid.uuid4().hex[:8]])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"receipt:experience_bff:{kind}:{digest}"


def _assert_workspace_access(ctx: EffectiveContext, workspace: WorkspaceRoot) -> None:
    if workspace.created_by != ctx.actor_user_id:
        raise ExperienceBffError("permission_denied: workspace not visible in active scope")


def create_workspace(
    *,
    ctx: EffectiveContext,
    workspace_kind: WorkspaceKind,
    name: str = "Workspace",
    root_study_id: str | None = None,
    home_scope_id: str | None = None,
) -> WorkspaceRoot:
    if workspace_kind == "study" and not root_study_id:
        raise ExperienceBffError("workspace_kind=study requires root_study_id")
    if workspace_kind == "ad_hoc" and root_study_id:
        raise ExperienceBffError("workspace_kind=ad_hoc prohibits root_study_id")
    ws = WorkspaceRoot(
        workspace_id=str(uuid.uuid4()),
        workspace_kind=workspace_kind,
        home_scope_id=home_scope_id or ctx.active_scope_id or personal_home_scope_id(ctx.actor_user_id),
        root_study_id=root_study_id,
        created_by=ctx.actor_user_id,
        name=name,
    )
    get_experience_store().workspaces[ws.workspace_id] = ws
    return ws


def create_working_set(
    *,
    ctx: EffectiveContext,
    workspace_id: str,
    name: str,
    root_refs: list[str] | None = None,
    object_refs: list[str] | None = None,
) -> WorkingSet:
    store = get_experience_store()
    workspace = store.workspaces.get(workspace_id)
    if workspace is None:
        raise ExperienceBffError(f"workspace not found: {workspace_id}")
    _assert_workspace_access(ctx, workspace)
    refs = list(object_refs or [])
    for ref in refs:
        if not isinstance(ref, str) or "://" not in ref:
            raise ExperienceBffError(
                f"object_refs must be canonical refs (type://id), got: {ref!r}"
            )
    ws = WorkingSet(
        working_set_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        home_scope_id=workspace.home_scope_id,
        name=name,
        root_refs=list(root_refs or []),
        object_refs=refs,
        created_by=ctx.actor_user_id,
    )
    store.working_sets[ws.working_set_id] = ws
    return ws


def add_object_ref_to_working_set(
    *,
    ctx: EffectiveContext,
    working_set_id: str,
    object_ref: str,
) -> WorkingSet:
    store = get_experience_store()
    working_set = store.working_sets.get(working_set_id)
    if working_set is None:
        raise ExperienceBffError(f"working_set not found: {working_set_id}")
    workspace = store.workspaces.get(working_set.workspace_id)
    if workspace is None:
        raise ExperienceBffError(f"workspace not found: {working_set.workspace_id}")
    _assert_workspace_access(ctx, workspace)
    if "://" not in object_ref:
        raise ExperienceBffError("object_ref must be canonical type://id")
    if object_ref not in working_set.object_refs:
        working_set.object_refs.append(object_ref)
        working_set.updated_at = datetime.now(timezone.utc)
        store.working_sets[working_set_id] = working_set
    return working_set


def create_workspace_view(
    *,
    ctx: EffectiveContext,
    working_set_id: str,
    name: str,
    layout_mode: LayoutMode = "semantic",
    surface_bindings: list[SurfaceBinding] | None = None,
    filter_spec: dict[str, Any] | None = None,
    grouping_spec: dict[str, Any] | None = None,
    layout_metadata: dict[str, Any] | None = None,
) -> WorkspaceView:
    store = get_experience_store()
    working_set = store.working_sets.get(working_set_id)
    if working_set is None:
        raise ExperienceBffError(f"working_set not found: {working_set_id}")
    workspace = store.workspaces.get(working_set.workspace_id)
    if workspace is None:
        raise ExperienceBffError(f"workspace not found: {working_set.workspace_id}")
    _assert_workspace_access(ctx, workspace)

    binding_ids: list[str] = []
    for binding in surface_bindings or []:
        if binding.object_ref not in working_set.object_refs and binding.object_ref not in working_set.root_refs:
            # Allow binding only to refs already in the working set (no silent object injection).
            raise ExperienceBffError(
                "surface binding object_ref must already exist in working set refs"
            )
        store.bindings[binding.surface_binding_id] = binding
        binding_ids.append(binding.surface_binding_id)

    view = WorkspaceView(
        view_id=str(uuid.uuid4()),
        working_set_id=working_set_id,
        name=name,
        layout_mode=layout_mode,
        surface_binding_ids=binding_ids,
        filter_spec=dict(filter_spec or {}),
        grouping_spec=dict(grouping_spec or {}),
        layout_metadata=dict(layout_metadata or {}),
        created_by=ctx.actor_user_id,
    )
    store.views[view.view_id] = view
    return view


def build_workspace_manifest(
    *,
    ctx: EffectiveContext,
    workspace_id: str,
) -> WorkspaceManifest:
    store = get_experience_store()
    workspace = store.workspaces.get(workspace_id)
    if workspace is None:
        raise ExperienceBffError(f"workspace not found: {workspace_id}")
    _assert_workspace_access(ctx, workspace)

    working_sets = [w for w in store.working_sets.values() if w.workspace_id == workspace_id]
    ws_ids = {w.working_set_id for w in working_sets}
    views = [v for v in store.views.values() if v.working_set_id in ws_ids]
    binding_ids = {bid for v in views for bid in v.surface_binding_ids}
    bindings = [store.bindings[b] for b in binding_ids if b in store.bindings]

    return WorkspaceManifest(
        workspace=workspace,
        working_sets=working_sets,
        views=views,
        surface_bindings=bindings,
        permission_fingerprint=ctx.permission_fingerprint,
        active_scope_id=ctx.active_scope_id,
        receipt_id=_receipt("manifest", workspace_id, ctx.actor_user_id),
    )


def assemble_home_experience(*, ctx: EffectiveContext) -> HomeExperienceModel:
    store = get_experience_store()
    actor = ctx.actor_user_id
    workspaces = [w for w in store.workspaces.values() if w.created_by == actor]
    working_sets = [
        ws
        for ws in store.working_sets.values()
        if store.workspaces.get(ws.workspace_id)
        and store.workspaces[ws.workspace_id].created_by == actor
    ]
    working_sets.sort(key=lambda w: w.updated_at, reverse=True)

    findings = [
        f
        for f in get_evidence_binding_store()._findings.values()
        if f.created_by == actor
    ]
    counts = {
        "draft": sum(1 for f in findings if f.status == FindingStatus.DRAFT),
        "promoted": sum(1 for f in findings if f.status == FindingStatus.PROMOTED),
        "retracted": sum(1 for f in findings if f.status == FindingStatus.RETRACTED),
    }
    return HomeExperienceModel(
        actor_user_id=actor,
        active_scope_id=ctx.active_scope_id,
        home_scope_id=ctx.home_scope_id,
        permission_fingerprint=ctx.permission_fingerprint,
        workspace_summaries=[
            {
                "workspace_id": w.workspace_id,
                "name": w.name,
                "workspace_kind": w.workspace_kind,
                "root_study_id": w.root_study_id,
                "home_scope_id": w.home_scope_id,
            }
            for w in workspaces
        ],
        recent_working_set_ids=[w.working_set_id for w in working_sets[:20]],
        open_finding_counts=counts,
        visible_artifact_count=len(get_membership_store().list_visible_artifact_ids(actor)),
    )


def assemble_research_experience(*, ctx: EffectiveContext) -> ResearchExperienceModel:
    store = get_experience_store()
    actor = ctx.actor_user_id
    study_ws = [
        w.workspace_id
        for w in store.workspaces.values()
        if w.created_by == actor and w.workspace_kind == "study"
    ]
    findings = [
        f
        for f in get_evidence_binding_store()._findings.values()
        if f.created_by == actor
    ]
    return ResearchExperienceModel(
        actor_user_id=actor,
        active_scope_id=ctx.active_scope_id,
        permission_fingerprint=ctx.permission_fingerprint,
        study_workspace_ids=study_ws,
        draft_finding_ids=[f.finding_id for f in findings if f.status == FindingStatus.DRAFT],
        promoted_finding_ids=[f.finding_id for f in findings if f.status == FindingStatus.PROMOTED],
        membership_artifact_ids=sorted(get_membership_store().list_visible_artifact_ids(actor)),
    )


def assemble_knowledge_experience(*, ctx: EffectiveContext) -> KnowledgeExperienceModel:
    actor = ctx.actor_user_id
    ebs = get_evidence_binding_store()
    findings = [f for f in ebs._findings.values() if f.created_by == actor]
    promoted_ids = [f.finding_id for f in findings if f.status == FindingStatus.PROMOTED]
    bindings = [
        b
        for b in ebs._bindings.values()
        if b.created_by == actor or b.finding_id in promoted_ids
    ]
    # Permission-safe: only bindings for actor-owned findings or created by actor.
    safe_bindings = [
        b
        for b in bindings
        if (ebs.get_finding(b.finding_id) and ebs.get_finding(b.finding_id).created_by == actor)
    ]
    return KnowledgeExperienceModel(
        actor_user_id=actor,
        active_scope_id=ctx.active_scope_id,
        permission_fingerprint=ctx.permission_fingerprint,
        promoted_finding_ids=promoted_ids,
        evidence_binding_ids=[b.binding_id for b in safe_bindings],
        path_backed_binding_ids=[
            b.binding_id for b in safe_bindings if b.evidence_path_id
        ],
    )


__all__ = [
    "ExperienceBffError",
    "HomeExperienceModel",
    "KnowledgeExperienceModel",
    "ResearchExperienceModel",
    "SurfaceBinding",
    "WorkingSet",
    "WorkspaceManifest",
    "WorkspaceRoot",
    "WorkspaceView",
    "add_object_ref_to_working_set",
    "assemble_home_experience",
    "assemble_knowledge_experience",
    "assemble_research_experience",
    "build_workspace_manifest",
    "create_workspace",
    "create_workspace_view",
    "create_working_set",
    "get_experience_store",
    "reset_experience_store_for_tests",
]
