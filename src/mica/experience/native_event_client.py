"""APV-09 Native event client — mount ProductEventEnvelope on product WS.

Authority: Frontend Runtime Contract V0.6 §4–5 / North Star APV-09
Hard gate: browser sees real backend events without duplicate surfaces.

Consumes: ProductEventEnvelope outbox (APV-07), AppLocation/ActiveContext (APV-08),
          WorkspaceManifest (APV-06)
Does not own: legacy /ws/mica agentic authority (adapted, not replaced).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from mica.experience.bff import build_workspace_manifest, get_experience_store
from mica.experience.context_router import AppLocation, resolve_active_context
from mica.experience.product_events import (
    ProductEventEnvelope,
    ProductEventError,
    acknowledge_replay_cursor,
    adapt_legacy_ws_message,
    publish_product_event,
    resume_product_events,
)
from mica.identity.effective_context import EffectiveContext, personal_home_scope_id

CLIENT_WS_SCHEMA = "urn:mica:ws:client:v1"
SurfaceLifecycle = Literal["preview", "hydrating", "hydrated", "hibernated", "fullscreen"]


class NativeEventError(ValueError):
    """Fail-closed native event client error."""


class ClientHello(BaseModel):
    type: Literal["client.hello"] = "client.hello"
    schema_urn: Literal["urn:mica:ws:client:v1"] = Field(
        default=CLIENT_WS_SCHEMA,
        alias="schema",
    )
    session_id: str
    effective_scope_id: str
    replay_cursor: str | None = None
    client_capabilities: list[str] = Field(default_factory=list)
    actor_user_id: str | None = None
    location: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


class MountedSurface(BaseModel):
    surface_id: str
    object_ref: str
    surface_type: str
    family: str = "research"
    lifecycle: SurfaceLifecycle = "preview"
    source_app: str | None = None
    view_purpose: str = "default"
    singleton_per_object: bool = True


class NativeEventSession(BaseModel):
    """Server-side mount state for one product WS connection."""

    session_id: str
    actor_user_id: str
    effective_scope_id: str
    hello_capabilities: list[str] = Field(default_factory=list)
    surfaces: dict[str, MountedSurface] = Field(default_factory=dict)
    # object_ref + view_purpose -> surface_id (dedupe key)
    surface_index: dict[str, str] = Field(default_factory=dict)
    last_replay_cursor: str | None = None
    workspace_id: str | None = None
    mounted: bool = False

    model_config = {"arbitrary_types_allowed": True}


def _surface_key(object_ref: str, view_purpose: str) -> str:
    return f"{object_ref}::{view_purpose}"


def _ctx_for_session(session: NativeEventSession) -> EffectiveContext:
    home = personal_home_scope_id(session.actor_user_id)
    return EffectiveContext(
        actor_user_id=session.actor_user_id,
        session_id=session.session_id,
        active_scope_id=session.effective_scope_id,
        home_scope_id=home,
        destination_scope_id=session.effective_scope_id,
        workspace_id=session.workspace_id,
        permission_fingerprint=f"fp:{session.actor_user_id}:{session.effective_scope_id}",
        policy_snapshot_id="policy_snapshot:unresolved",
    )


class NativeEventClientRuntime:
    """In-process runtime that mounts envelopes for product WS sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, NativeEventSession] = {}

    def clear(self) -> None:
        self._sessions.clear()

    def get_session(self, session_id: str) -> NativeEventSession | None:
        return self._sessions.get(session_id)

    def handle_hello(
        self,
        *,
        hello: ClientHello,
        actor_user_id: str,
    ) -> tuple[NativeEventSession, list[ProductEventEnvelope]]:
        if not hello.session_id.strip():
            raise NativeEventError("client.hello requires session_id")
        if not hello.effective_scope_id.strip() or ":" not in hello.effective_scope_id:
            raise NativeEventError("client.hello requires effective_scope_id scope_ref")

        actor = (hello.actor_user_id or actor_user_id).strip()
        if not actor:
            raise NativeEventError("actor_user_id required")

        workspace_id = None
        if hello.location:
            loc = AppLocation(
                app=str(hello.location.get("app") or "workspace"),
                scope_id=hello.effective_scope_id,
                resource_type=hello.location.get("resource_type"),
                resource_id=hello.location.get("resource_id"),
                view=hello.location.get("view"),
                working_set_id=hello.location.get("working_set_id") or hello.location.get("workingSetId"),
                surface_id=hello.location.get("surface_id") or hello.location.get("surfaceId"),
                source_app=hello.location.get("source_app") or hello.location.get("sourceApp"),
            )
            active = resolve_active_context(identity=actor, location=loc, session_id=hello.session_id)
            workspace_id = active.workspace_id
            # Prefer resolved scope from location when present.
            scope_id = active.active_scope_id
        else:
            scope_id = hello.effective_scope_id

        session = NativeEventSession(
            session_id=hello.session_id,
            actor_user_id=actor,
            effective_scope_id=scope_id,
            hello_capabilities=list(hello.client_capabilities),
            last_replay_cursor=hello.replay_cursor,
            workspace_id=workspace_id,
            mounted=True,
        )
        self._sessions[session.session_id] = session

        ctx = _ctx_for_session(session)
        # Resume missing events exactly once from cursor.
        batch = resume_product_events(
            ctx=ctx,
            replay_cursor=hello.replay_cursor,
            session_id=session.session_id,
        )
        events = list(batch.events)

        ready = publish_product_event(
            ctx=ctx,
            event_type="workspace.view.changed",
            payload={
                "kind": "session.ready",
                "capabilities": session.hello_capabilities,
                "workspace_id": session.workspace_id,
                "resumed_count": len(events),
            },
            subject_refs=[{"type": "session", "id": session.session_id}],
            correlation_id=f"hello:{session.session_id}",
            completeness="terminal",
            session_id=session.session_id,
        )
        events.append(ready)
        session.last_replay_cursor = ready.replay_cursor
        return session, events

    def mount_surface(
        self,
        *,
        session_id: str,
        object_ref: str,
        surface_type: str,
        family: str = "research",
        source_app: str | None = None,
        view_purpose: str = "default",
        singleton_per_object: bool = True,
        lifecycle: SurfaceLifecycle = "preview",
    ) -> tuple[MountedSurface, ProductEventEnvelope, bool]:
        """Mount a surface. Returns (surface, event, created_new).

        If singleton_per_object and the same object_ref+purpose exists, focus existing
        instead of creating a duplicate surface (APV-09 hard gate).
        """
        session = self._require_session(session_id)
        if "://" not in object_ref:
            raise NativeEventError("object_ref must be canonical type://id")
        key = _surface_key(object_ref, view_purpose)
        ctx = _ctx_for_session(session)

        if singleton_per_object and key in session.surface_index:
            existing_id = session.surface_index[key]
            surface = session.surfaces[existing_id]
            event = publish_product_event(
                ctx=ctx,
                event_type="workspace.surface.changed",
                payload={
                    "action": "focus_existing",
                    "surface_id": surface.surface_id,
                    "object_ref": object_ref,
                    "surface_type": surface.surface_type,
                    "lifecycle": surface.lifecycle,
                    "duplicate_prevented": True,
                },
                subject_refs=[
                    {"type": "surface", "id": surface.surface_id},
                    {"type": "object", "id": object_ref},
                ],
                session_id=session.session_id,
            )
            session.last_replay_cursor = event.replay_cursor
            return surface, event, False

        surface = MountedSurface(
            surface_id=str(uuid.uuid4()),
            object_ref=object_ref,
            surface_type=surface_type,
            family=family,
            lifecycle=lifecycle,
            source_app=source_app,
            view_purpose=view_purpose,
            singleton_per_object=singleton_per_object,
        )
        session.surfaces[surface.surface_id] = surface
        session.surface_index[key] = surface.surface_id
        event = publish_product_event(
            ctx=ctx,
            event_type="workspace.surface.changed",
            payload={
                "action": "mounted",
                "surface_id": surface.surface_id,
                "object_ref": object_ref,
                "surface_type": surface_type,
                "lifecycle": lifecycle,
                "duplicate_prevented": False,
            },
            subject_refs=[
                {"type": "surface", "id": surface.surface_id},
                {"type": "object", "id": object_ref},
            ],
            session_id=session.session_id,
        )
        session.last_replay_cursor = event.replay_cursor
        return surface, event, True

    def set_surface_lifecycle(
        self,
        *,
        session_id: str,
        surface_id: str,
        lifecycle: SurfaceLifecycle,
    ) -> ProductEventEnvelope:
        session = self._require_session(session_id)
        surface = session.surfaces.get(surface_id)
        if surface is None:
            raise NativeEventError(f"surface not mounted: {surface_id}")
        surface.lifecycle = lifecycle
        session.surfaces[surface_id] = surface
        ctx = _ctx_for_session(session)
        event = publish_product_event(
            ctx=ctx,
            event_type="workspace.surface.changed",
            payload={
                "action": "lifecycle",
                "surface_id": surface_id,
                "object_ref": surface.object_ref,
                "lifecycle": lifecycle,
            },
            subject_refs=[{"type": "surface", "id": surface_id}],
            session_id=session.session_id,
            completeness="partial" if lifecycle in ("preview", "hydrating") else "terminal",
        )
        session.last_replay_cursor = event.replay_cursor
        return event

    def publish_workspace_manifest_event(
        self,
        *,
        session_id: str,
        workspace_id: str,
    ) -> ProductEventEnvelope:
        session = self._require_session(session_id)
        ctx = _ctx_for_session(session)
        store = get_experience_store()
        workspace = store.workspaces.get(workspace_id)
        if workspace is None:
            raise NativeEventError(f"workspace not found: {workspace_id}")
        if workspace.created_by != session.actor_user_id:
            raise NativeEventError("permission_denied: workspace not visible")
        # Build manifest under a context that matches workspace owner scope.
        manifest_ctx = EffectiveContext(
            actor_user_id=session.actor_user_id,
            session_id=session.session_id,
            active_scope_id=workspace.home_scope_id,
            home_scope_id=personal_home_scope_id(session.actor_user_id),
            workspace_id=workspace_id,
            permission_fingerprint=ctx.permission_fingerprint,
            policy_snapshot_id=ctx.policy_snapshot_id,
        )
        manifest = build_workspace_manifest(ctx=manifest_ctx, workspace_id=workspace_id)
        session.workspace_id = workspace_id
        event = publish_product_event(
            ctx=ctx,
            event_type="workspace.working_set.changed",
            payload={
                "action": "manifest_mounted",
                "workspace_id": workspace_id,
                "working_set_ids": [w.working_set_id for w in manifest.working_sets],
                "view_ids": [v.view_id for v in manifest.views],
                "surface_binding_ids": [b.surface_binding_id for b in manifest.surface_bindings],
                "receipt_id": manifest.receipt_id,
            },
            subject_refs=[{"type": "workspace", "id": workspace_id}],
            session_id=session.session_id,
        )
        session.last_replay_cursor = event.replay_cursor
        return event

    def acknowledge(self, *, session_id: str, replay_cursor: str) -> int:
        session = self._require_session(session_id)
        ctx = _ctx_for_session(session)
        seq = acknowledge_replay_cursor(ctx=ctx, cursor=replay_cursor, session_id=session_id)
        session.last_replay_cursor = replay_cursor
        return seq

    def adapt_legacy(
        self,
        *,
        session_id: str,
        legacy_type: str,
        payload: dict[str, Any] | None = None,
    ) -> ProductEventEnvelope:
        session = self._require_session(session_id)
        ctx = _ctx_for_session(session)
        event = adapt_legacy_ws_message(
            ctx=ctx,
            legacy_type=legacy_type,
            payload=payload,
            correlation_id=f"legacy:{session_id}",
        )
        session.last_replay_cursor = event.replay_cursor
        return event

    def _require_session(self, session_id: str) -> NativeEventSession:
        session = self._sessions.get(session_id)
        if session is None or not session.mounted:
            raise NativeEventError(f"session not mounted: {session_id}")
        return session


_RUNTIME: NativeEventClientRuntime | None = None


def get_native_event_runtime() -> NativeEventClientRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = NativeEventClientRuntime()
    return _RUNTIME


def reset_native_event_runtime_for_tests() -> NativeEventClientRuntime:
    global _RUNTIME
    from mica.experience.product_events import reset_product_event_outbox_for_tests

    reset_product_event_outbox_for_tests()
    _RUNTIME = NativeEventClientRuntime()
    return _RUNTIME


def envelope_to_ws_message(event: ProductEventEnvelope) -> dict[str, Any]:
    """Serialize ProductEventEnvelope as the WS server frame."""
    return {
        "type": "product.event",
        "schema": "urn:mica:ws:server:v1",
        "event": event.model_dump(mode="json", by_alias=True),
    }


__all__ = [
    "CLIENT_WS_SCHEMA",
    "ClientHello",
    "MountedSurface",
    "NativeEventClientRuntime",
    "NativeEventError",
    "NativeEventSession",
    "envelope_to_ws_message",
    "get_native_event_runtime",
    "reset_native_event_runtime_for_tests",
]
