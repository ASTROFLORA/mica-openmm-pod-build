"""Product context endpoints (APV-01).

North Star §6.1:
  GET  /api/v1/me/context
  POST /api/v1/context/switch
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency, user_dependency
from mica.identity.effective_context import (
    EffectiveContext,
    EffectiveContextError,
    EffectiveContextHints,
    resolve_effective_context,
)

router = APIRouter(tags=["product-context"])


class ContextSwitchRequest(BaseModel):
    active_scope_id: str | None = None
    destination_scope_id: str | None = None
    lab_id: str | None = None
    study_id: str | None = None
    research_line_id: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None
    policy_snapshot_id: str | None = None


class ContextSwitchResponse(BaseModel):
    previous: EffectiveContext
    current: EffectiveContext
    switched: bool = Field(default=True)


@router.get("/api/v1/me/context", response_model=EffectiveContext)
async def get_me_context(
    ctx: EffectiveContext = Depends(effective_context_dependency),
) -> EffectiveContext:
    """Return the resolved EffectiveContext for the authenticated actor."""
    return ctx


@router.post("/api/v1/context/switch", response_model=ContextSwitchResponse)
async def switch_context(
    body: ContextSwitchRequest,
    user_id: str = Depends(user_dependency),
    current: EffectiveContext = Depends(effective_context_dependency),
) -> ContextSwitchResponse:
    """Resolve a new EffectiveContext from explicit switch hints.

    Does not mutate durable membership. Permission fingerprint changes with
    active scope so caches invalidate (APV-01 gate).
    """
    hints = EffectiveContextHints(
        session_id=body.session_id or current.session_id,
        active_scope_id=body.active_scope_id,
        destination_scope_id=body.destination_scope_id,
        lab_id=body.lab_id if body.lab_id is not None else current.lab_id,
        study_id=body.study_id if body.study_id is not None else current.study_id,
        research_line_id=(
            body.research_line_id if body.research_line_id is not None else current.research_line_id
        ),
        workspace_id=body.workspace_id if body.workspace_id is not None else current.workspace_id,
        policy_snapshot_id=body.policy_snapshot_id or current.policy_snapshot_id,
    )
    try:
        nxt = resolve_effective_context(identity=user_id, hints=hints)
    except EffectiveContextError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_effective_context: {exc}") from exc

    switched = (
        nxt.active_scope_id != current.active_scope_id
        or nxt.permission_fingerprint != current.permission_fingerprint
        or nxt.destination_scope_id != current.destination_scope_id
    )
    return ContextSwitchResponse(previous=current, current=nxt, switched=switched)
