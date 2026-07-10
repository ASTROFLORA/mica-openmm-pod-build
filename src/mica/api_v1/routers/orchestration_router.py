"""orchestration_router.py — REST+WS endpoint for P0 Slice C orchestration.

POST /api/v1/orchestrate/run     → Execute orchestration loop, emit mutations
GET  /api/v1/orchestrate/health   → Health check

Mutations emitted via WS after orchestration for Poltergeist effect.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orchestrate", tags=["orchestrate"])


class MutationPayload(BaseModel):
    mutation_type: str
    payload: Dict[str, Any] = {}
    actor: str = "orchestrator"


class OrchestrateRunRequest(BaseModel):
    study_id: str
    workspace_id: str
    action: str = "custom"
    payload: Dict[str, Any] = {}
    target_window: str | None = None
    mutations: List[MutationPayload] = []


class OrchestrateRunResponse(BaseModel):
    orchestration_id: str
    status: str
    artifact_refs: List[str] = []
    receipt_refs: List[str] = []
    mutations: List[MutationPayload] = []
    summary: str = ""


@router.post("/run", response_model=OrchestrateRunResponse)
async def orchestrate_run(
    body: OrchestrateRunRequest,
    user_id: str = Depends(user_dependency),
):
    """Execute an orchestration loop.

    Wraps the OrchestrationController and returns typed Poltergeist mutations.
    The caller (ChatPanel or agent) applies mutations to the OS store.
    """
    from mica.orchestration.orchestration import (
        OrchestrationController,
        OrchestrationRequest,
        AgentUiMutationBM,
    )

    controller = OrchestrationController()
    converted = []
    for _m in body.mutations:
        converted.append(AgentUiMutationBM(actor=_m.actor, mutation_type=_m.mutation_type, payload=_m.payload))
    req = OrchestrationRequest(
        study_id=body.study_id,
        workspace_id=body.workspace_id,
        action=body.action,
        payload=body.payload,
        target_window=body.target_window,
        mutations=converted,
    )
    result = await controller.run(req, user_id=user_id)

    return OrchestrateRunResponse(
        orchestration_id=result.orchestration_id,
        status=result.status,
        artifact_refs=result.artifact_refs,
        receipt_refs=result.receipt_refs,
        mutations=[MutationPayload(mutation_type=m.mutation_type, payload=m.payload, actor=m.actor) for m in result.mutations],
        summary=result.summary,
    )


@router.get("/health")
async def orchestrate_health():
    """Health check for orchestration subsystem."""
    return {"status": "ok", "service": "orchestration", "slice": "P0-C"}
