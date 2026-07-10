"""kernel_router.py — REST endpoint for Command Kernel v1 closures and manifest freeze."""

from __future__ import annotations

from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.agentic.closure_store import (
    default_regression_suite_for_closure,
    get_closure_store,
    run_regression_suite,
)
from mica.agentic.manifest_freeze import get_freeze_manager

router = APIRouter(prefix="/api/v1/kernel", tags=["kernel"])


class EmitClosureRequest(BaseModel):
    lane: str
    round: int | str = Field(alias="round_num", default="round-1")
    evidence_packet_refs: List[str] = Field(default_factory=list, alias="evidence_refs")
    test_summary: Dict[str, int] = Field(default_factory=dict)
    require_green: bool = False
    regression_suite: List[str] = Field(default_factory=list)
    cost_gate_green: bool = True
    security_gate_green: bool = True

    class Config:
        populate_by_name = True


class FreezeManifestRequest(BaseModel):
    version: str
    require_green: bool = False


@router.post("/closures")
async def emit_closure(
    body: EmitClosureRequest,
    user_id: str = Depends(user_dependency),
):
    """Emit a new closure receipt for a lane/round."""
    store = get_closure_store()
    try:
        test_summary = dict(body.test_summary or {})
        if body.require_green:
            regression_suite = list(body.regression_suite or default_regression_suite_for_closure(body.lane, body.round))
            test_summary = run_regression_suite(regression_suite)
            if int(test_summary.get("failed", 0) or 0) > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "regression_suite_red",
                        "lane": body.lane,
                        "round": str(body.round),
                        "regression_suite": regression_suite,
                        "test_summary": test_summary,
                    },
                )
        if not bool(body.cost_gate_green):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "cost_gate_red",
                    "lane": body.lane,
                    "round": str(body.round),
                },
            )
        if not bool(body.security_gate_green):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "security_gate_red",
                    "lane": body.lane,
                    "round": str(body.round),
                },
            )
        receipt = store.emit_closure(
            lane=body.lane,
            round_num=body.round,
            evidence_packet_refs=body.evidence_packet_refs,
            test_summary=test_summary,
            require_green=body.require_green,
        )
        await store.drain_provenance_tasks()
        return receipt.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/closures/{closure_ref:path}")
async def get_closure_status(
    closure_ref: str,
    user_id: str = Depends(user_dependency),
):
    """Retrieve closure status by URN ref."""
    store = get_closure_store()
    closure = store.get_closure(closure_ref)
    if closure is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Closure '{closure_ref}' not found.",
        )
    return closure.model_dump(mode="json")


@router.post("/closures/{closure_ref:path}/retract")
async def retract_closure(
    closure_ref: str,
    user_id: str = Depends(user_dependency),
):
    """Retract an existing closure receipt."""
    store = get_closure_store()
    try:
        retraction = store.retract_closure(closure_ref)
        await store.drain_provenance_tasks()
        return retraction.model_dump(mode="json")
    except KeyError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/manifest/freeze")
async def freeze_manifest(
    body: FreezeManifestRequest,
    user_id: str = Depends(user_dependency),
):
    """Freeze manifest under a version name."""
    manager = get_freeze_manager()
    res = manager.freeze_manifest(body.version, require_green=body.require_green)
    return res


@router.get("/manifest/{manifest_version}")
async def verify_manifest(
    manifest_version: str,
    user_id: str = Depends(user_dependency),
):
    """Verify manifest against a frozen snapshot."""
    manager = get_freeze_manager()
    res = manager.verify_manifest(manifest_version)
    if res.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=res.get("message"),
        )
    return res
