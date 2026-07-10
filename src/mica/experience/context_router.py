"""APV-08 Astroflora context router — URL AppLocation → ActiveContext.

Authority: Frontend Runtime Contract V0.6 §3 / North Star APV-08
Hard gate: deep-link, refresh and source-application continuity.

Consumes: EffectiveContext (APV-01), experience BFF workspaces (APV-06)
Does not own: frontend shell routes, Surface registry (APV-09), event client mount.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel, Field, field_validator

from mica.experience.bff import get_experience_store
from mica.identity.effective_context import (
    EffectiveContext,
    EffectiveContextHints,
    personal_home_scope_id,
    resolve_effective_context,
)

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

VALID_APPS: frozenset[str] = frozenset(
    {"home", "research", "knowledge", "workspace", "drive", "pipelines", "agents", "labs"}
)


class ContextRouterError(ValueError):
    """Fail-closed AppLocation / handoff error."""


class AppLocation(BaseModel):
    """URL-addressable product location. Client never fabricates fingerprints."""

    app: AppId
    scope_id: str
    resource_type: str | None = None
    resource_id: str | None = None
    view: str | None = None
    working_set_id: str | None = None
    surface_id: str | None = None
    source_app: AppId | None = None

    @field_validator("app", "source_app")
    @classmethod
    def _validate_app(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in VALID_APPS:
            raise ValueError(f"invalid app id: {value}")
        return normalized  # type: ignore[return-value]

    @field_validator("scope_id")
    @classmethod
    def _scope_required(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized or ":" not in normalized:
            raise ValueError("scope_id must be a scope_ref (type:id)")
        return normalized


class ActiveContext(BaseModel):
    """Backend-resolved active context. Fingerprint is server authority."""

    actor_user_id: str
    session_id: str
    active_scope_id: str
    home_scope_id: str
    lab_id: str | None = None
    study_id: str | None = None
    research_line_id: str | None = None
    workspace_id: str | None = None
    working_set_id: str | None = None
    workspace_view_id: str | None = None
    permission_fingerprint: str
    policy_snapshot_id: str
    app: AppId
    source_app: AppId | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    location: AppLocation


class SemanticSidebarNode(BaseModel):
    node_id: str
    kind: Literal["study", "working_set", "view", "surface", "layout"]
    label: str
    ref: str | None = None
    children: list["SemanticSidebarNode"] = Field(default_factory=list)
    focused: bool = False


class SemanticSidebarProjection(BaseModel):
    app: AppId
    active_scope_id: str
    nodes: list[SemanticSidebarNode] = Field(default_factory=list)
    focused_view_id: str | None = None
    focused_surface_id: str | None = None


class WorkspaceHandoffResult(BaseModel):
    source_location: AppLocation
    destination_location: AppLocation
    active_context: ActiveContext
    preserved_resource: bool
    preserved_source_app: bool


def encode_app_location(location: AppLocation) -> str:
    """Encode AppLocation as a deep-link path + query (refresh-stable)."""
    query: dict[str, str] = {"scope": location.scope_id}
    if location.resource_type:
        query["resourceType"] = location.resource_type
    if location.resource_id:
        query["resourceId"] = location.resource_id
    if location.view:
        query["view"] = location.view
    if location.working_set_id:
        query["workingSetId"] = location.working_set_id
    if location.surface_id:
        query["surfaceId"] = location.surface_id
    if location.source_app:
        query["sourceApp"] = location.source_app
    return f"/{location.app}?{urlencode(query)}"


def parse_app_location(url_or_path: str) -> AppLocation:
    """Parse a deep-link URL/path into AppLocation."""
    raw = str(url_or_path).strip()
    if not raw:
        raise ContextRouterError("location url is required")
    if "://" in raw:
        parsed = urlparse(raw)
        path = parsed.path
        query = parse_qs(parsed.query)
    else:
        if "?" in raw:
            path, qs = raw.split("?", 1)
            query = parse_qs(qs)
        else:
            path, query = raw, {}
    app = path.strip("/").split("/", 1)[0].lower()
    if app not in VALID_APPS:
        raise ContextRouterError(f"invalid app in location path: {app or '(empty)'}")

    def _one(key: str) -> str | None:
        values = query.get(key)
        if not values:
            return None
        return str(values[0]).strip() or None

    scope = _one("scope") or _one("scopeId")
    if not scope:
        raise ContextRouterError("deep-link requires scope query param")
    source_app = _one("sourceApp")
    return AppLocation(
        app=app,  # type: ignore[arg-type]
        scope_id=scope,
        resource_type=_one("resourceType"),
        resource_id=_one("resourceId"),
        view=_one("view"),
        working_set_id=_one("workingSetId"),
        surface_id=_one("surfaceId"),
        source_app=source_app,  # type: ignore[arg-type]
    )


def _hints_from_location(location: AppLocation) -> EffectiveContextHints:
    lab_id = None
    study_id = None
    workspace_id = None
    research_line_id = None
    if location.scope_id.startswith("lab:"):
        lab_id = location.scope_id.split(":", 1)[1]
    if location.scope_id.startswith("study:"):
        study_id = location.scope_id.split(":", 1)[1]
    if location.resource_type == "study" and location.resource_id:
        study_id = location.resource_id
    if location.resource_type == "lab" and location.resource_id:
        lab_id = location.resource_id
    if location.resource_type == "workspace" and location.resource_id:
        workspace_id = location.resource_id
    if location.resource_type == "research_line" and location.resource_id:
        research_line_id = location.resource_id
    if location.app == "workspace" and location.resource_id and location.resource_type in (None, "workspace"):
        workspace_id = location.resource_id
    return EffectiveContextHints(
        active_scope_id=location.scope_id,
        lab_id=lab_id,
        study_id=study_id,
        workspace_id=workspace_id,
        research_line_id=research_line_id,
    )


def resolve_active_context(
    *,
    identity: Any,
    location: AppLocation,
    session_id: str | None = None,
    policy_snapshot_id: str | None = None,
) -> ActiveContext:
    """Resolve ActiveContext from AppLocation. Fingerprint is server-derived."""
    hints = _hints_from_location(location)
    if session_id:
        hints.session_id = session_id
    if policy_snapshot_id:
        hints.policy_snapshot_id = policy_snapshot_id
    ctx = resolve_effective_context(identity=identity, hints=hints)

    workspace_id = ctx.workspace_id
    working_set_id = location.working_set_id
    view_id = location.view if location.view and location.view.startswith("view:") else location.view

    # Prefer explicit workspace resource when present in experience store.
    if location.resource_type == "workspace" and location.resource_id:
        workspace_id = location.resource_id
    if location.app == "workspace" and location.resource_id and not location.resource_type:
        workspace_id = location.resource_id

    return ActiveContext(
        actor_user_id=ctx.actor_user_id,
        session_id=ctx.session_id,
        active_scope_id=ctx.active_scope_id,
        home_scope_id=ctx.home_scope_id,
        lab_id=ctx.lab_id,
        study_id=ctx.study_id,
        research_line_id=ctx.research_line_id,
        workspace_id=workspace_id,
        working_set_id=working_set_id,
        workspace_view_id=view_id,
        permission_fingerprint=ctx.permission_fingerprint,
        policy_snapshot_id=ctx.policy_snapshot_id,
        app=location.app,
        source_app=location.source_app,
        resource_type=location.resource_type,
        resource_id=location.resource_id,
        location=location,
    )


def handoff_to_workspace(
    *,
    identity: Any,
    source: AppLocation,
    workspace_id: str | None = None,
    working_set_id: str | None = None,
    view: str | None = None,
    session_id: str | None = None,
) -> WorkspaceHandoffResult:
    """Open-in-Workspace without copying objects; preserves source app + resource identity."""
    if source.app == "workspace" and source.source_app is None:
        # Already in workspace — still preserve continuity fields.
        pass

    dest_workspace_id = workspace_id
    if dest_workspace_id is None and source.resource_type == "workspace":
        dest_workspace_id = source.resource_id
    if dest_workspace_id is None:
        # Create ephemeral ad_hoc destination id marker (client may POST /workspaces later).
        dest_workspace_id = f"pending:{source.scope_id}"

    # If a real workspace exists and belongs to actor, prefer it.
    store = get_experience_store()
    actor = str(getattr(identity, "user_id", identity) or "").strip() or str(identity)
    if workspace_id and workspace_id in store.workspaces:
        ws = store.workspaces[workspace_id]
        if ws.created_by != actor:
            raise ContextRouterError("permission_denied: workspace not visible for handoff")
        dest_workspace_id = workspace_id

    destination = AppLocation(
        app="workspace",
        scope_id=source.scope_id,
        resource_type="workspace" if not str(dest_workspace_id).startswith("pending:") else source.resource_type,
        resource_id=dest_workspace_id if not str(dest_workspace_id).startswith("pending:") else source.resource_id,
        view=view or source.view,
        working_set_id=working_set_id or source.working_set_id,
        surface_id=source.surface_id,
        source_app=source.source_app or source.app,
    )
    # Always keep original resource identity in destination when opening an object.
    if source.resource_type and source.resource_type != "workspace" and source.resource_id:
        destination = destination.model_copy(
            update={
                "resource_type": source.resource_type,
                "resource_id": source.resource_id,
            }
        )

    active = resolve_active_context(identity=identity, location=destination, session_id=session_id)
    return WorkspaceHandoffResult(
        source_location=source,
        destination_location=destination,
        active_context=active,
        preserved_resource=bool(
            source.resource_id
            and destination.resource_id == source.resource_id
            and destination.resource_type == source.resource_type
        )
        or (source.resource_id is None and destination.resource_id is None),
        preserved_source_app=destination.source_app == (source.source_app or source.app),
    )


def project_semantic_sidebar(
    *,
    active: ActiveContext,
) -> SemanticSidebarProjection:
    """Sidebar projection for Study / Working Sets / Views / Surfaces (refs only)."""
    nodes: list[SemanticSidebarNode] = []
    store = get_experience_store()
    workspace_id = active.workspace_id
    if workspace_id and workspace_id in store.workspaces:
        ws = store.workspaces[workspace_id]
        study_node = None
        if ws.root_study_id:
            study_node = SemanticSidebarNode(
                node_id=f"study:{ws.root_study_id}",
                kind="study",
                label=f"Study {ws.root_study_id}",
                ref=f"study://{ws.root_study_id}",
                focused=active.study_id == ws.root_study_id,
            )
            nodes.append(study_node)
        for working in [w for w in store.working_sets.values() if w.workspace_id == workspace_id]:
            view_children = []
            for view in [v for v in store.views.values() if v.working_set_id == working.working_set_id]:
                focused = active.workspace_view_id == view.view_id or active.location.view == view.view_id
                surfaces = [
                    SemanticSidebarNode(
                        node_id=bid,
                        kind="surface",
                        label=bid,
                        ref=store.bindings[bid].object_ref if bid in store.bindings else None,
                        focused=active.location.surface_id == bid,
                    )
                    for bid in view.surface_binding_ids
                ]
                view_children.append(
                    SemanticSidebarNode(
                        node_id=view.view_id,
                        kind="view",
                        label=view.name,
                        ref=f"view://{view.view_id}",
                        children=surfaces,
                        focused=focused,
                    )
                )
            nodes.append(
                SemanticSidebarNode(
                    node_id=working.working_set_id,
                    kind="working_set",
                    label=working.name,
                    ref=f"working_set://{working.working_set_id}",
                    children=view_children,
                    focused=active.working_set_id == working.working_set_id,
                )
            )
    else:
        # App-first fallback: single orientation node for current app/resource.
        nodes.append(
            SemanticSidebarNode(
                node_id=f"app:{active.app}",
                kind="layout",
                label=active.app,
                ref=f"app://{active.app}",
                focused=True,
                children=[
                    SemanticSidebarNode(
                        node_id=f"res:{active.resource_id or active.active_scope_id}",
                        kind="surface",
                        label=active.resource_id or active.active_scope_id,
                        ref=(
                            f"{active.resource_type}://{active.resource_id}"
                            if active.resource_type and active.resource_id
                            else active.active_scope_id
                        ),
                        focused=True,
                    )
                ]
                if active.resource_id or active.active_scope_id
                else [],
            )
        )

    return SemanticSidebarProjection(
        app=active.app,
        active_scope_id=active.active_scope_id,
        nodes=nodes,
        focused_view_id=active.workspace_view_id,
        focused_surface_id=active.location.surface_id,
    )


def default_location_for_actor(actor_user_id: str, *, app: AppId = "home") -> AppLocation:
    return AppLocation(app=app, scope_id=personal_home_scope_id(actor_user_id))


__all__ = [
    "ActiveContext",
    "AppLocation",
    "ContextRouterError",
    "SemanticSidebarNode",
    "SemanticSidebarProjection",
    "VALID_APPS",
    "WorkspaceHandoffResult",
    "default_location_for_actor",
    "encode_app_location",
    "handoff_to_workspace",
    "parse_app_location",
    "project_semantic_sidebar",
    "resolve_active_context",
]
