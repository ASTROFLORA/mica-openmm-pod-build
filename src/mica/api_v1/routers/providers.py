from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.serverless_models.provider_endpoint_control import (
    KillRequest,
    default_kill_ledger,
    default_registry,
    evaluate_zombies,
    execute_gated_kill,
    summarize_cost_bleed,
)
from mica.serverless_models.provider_dr_drill import (
    DEFAULT_PROVIDER_DR_GATE_REF,
    get_provider_dr_drill_runner,
)
from mica.serverless_models.provider_v1_closure import (
    get_provider_v1_closure_builder,
)

from .serverless_models import get_serverless_model_gateway


router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


class ProviderKillBody(BaseModel):
    scope: str = Field(default="endpoint")
    target_ref: str = ""
    endpoint_ref: str = ""
    provider: str = ""
    gate_ref: str = ""
    reason: str = "operator_requested"


class ProviderDRDrillBody(BaseModel):
    scenario: str = "provider_outage"
    provider: str = ""
    region: str = ""
    gate_ref: str = DEFAULT_PROVIDER_DR_GATE_REF


class ProviderV1ClosureBuildBody(BaseModel):
    passed: int = 0
    failed: int = 0
    cost_gate_green: bool = True
    security_gate_green: bool = True


def _provider_control_root_for_user(user_id: str):
    gateway = get_serverless_model_gateway(user_id)
    result_store = getattr(gateway, "_result_store", None)
    base_dir = getattr(result_store, "_base_dir", None)
    if base_dir is None:
        raise HTTPException(status_code=500, detail="Provider control storage is unavailable in this runtime.")
    return gateway, default_registry(base_dir.parent), default_kill_ledger(base_dir.parent)


@router.get("/endpoints")
async def list_provider_endpoints(
    provider: Optional[str] = None,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway, registry, _ = _provider_control_root_for_user(user_id)
    del gateway
    records = registry.list_records()
    provider_filter = str(provider or "").strip().lower()
    if provider_filter:
        records = [record for record in records if record.provider == provider_filter]
    return {
        "status": "ok",
        "endpoints": [record.to_dict() for record in records],
        "count": len(records),
        "cost_bleed": summarize_cost_bleed(records),
    }


@router.get("/endpoints/zombies")
async def list_provider_zombies(
    provider: Optional[str] = None,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway, registry, _ = _provider_control_root_for_user(user_id)
    del gateway
    records = registry.list_records()
    provider_filter = str(provider or "").strip().lower()
    if provider_filter:
        records = [record for record in records if record.provider == provider_filter]
    verdicts = evaluate_zombies(records)
    return {
        "status": "ok",
        "zombies": [verdict.to_dict() for verdict in verdicts],
        "count": len(verdicts),
        "cost_bleed": summarize_cost_bleed(records),
    }


@router.post("/endpoints/kill")
async def kill_provider_endpoints(
    body: ProviderKillBody,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway, registry, ledger = _provider_control_root_for_user(user_id)
    target_ref = body.target_ref or body.endpoint_ref or body.provider
    outcome = await execute_gated_kill(
        gateway=gateway,
        registry=registry,
        ledger=ledger,
        kill_request=KillRequest(
            scope=str(body.scope or "endpoint").strip().lower() or "endpoint",
            target_ref=str(target_ref or "").strip(),
            gate_ref=str(body.gate_ref or "").strip(),
            reason=str(body.reason or "").strip() or "operator_requested",
        ),
    )
    return {
        "status": outcome.get("status"),
        "reason": outcome.get("reason") or "",
        "results": list(outcome.get("results") or []),
        "audit_ref": str((outcome.get("audit_refs") or [""])[0] or "").strip(),
        "audit_refs": list(outcome.get("audit_refs") or []),
        "teardown_confirmed": bool(outcome.get("status") == "killed"),
    }


@router.post("/dr/outage-drill")
async def run_provider_dr_drill(
    body: ProviderDRDrillBody,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway, registry, ledger = _provider_control_root_for_user(user_id)
    runner = get_provider_dr_drill_runner()
    scenario = str(body.scenario or "provider_outage").strip().lower() or "provider_outage"
    if scenario == "provider_outage":
        receipt = await runner.run_outage_drill(
            gateway=gateway,
            registry=registry,
            ledger=ledger,
            provider=str(body.provider or "").strip(),
            gate_ref=str(body.gate_ref or DEFAULT_PROVIDER_DR_GATE_REF).strip() or DEFAULT_PROVIDER_DR_GATE_REF,
        )
        return receipt.model_dump()
    if scenario == "region_loss":
        receipt = runner.run_region_loss_rebuild(
            registry=registry,
            provider=str(body.provider or "").strip(),
            region=str(body.region or "").strip(),
        )
        return receipt.model_dump()
    raise HTTPException(
        status_code=400,
        detail={
            "code": "unsupported_dr_scenario",
            "scenario": scenario,
        },
    )


@router.get("/v1/closure")
async def get_provider_v1_closure(
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    del user_id
    builder = get_provider_v1_closure_builder()
    closure = builder.get_last_closure()
    if closure is None:
        closure = builder.build_closure()
    return closure.model_dump()


@router.post("/v1/closure/build")
async def build_provider_v1_closure(
    body: ProviderV1ClosureBuildBody,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    del user_id
    builder = get_provider_v1_closure_builder()
    closure = builder.build_closure(
        test_summary={"passed": int(body.passed or 0), "failed": int(body.failed or 0)},
        cost_gate_green=bool(body.cost_gate_green),
        security_gate_green=bool(body.security_gate_green),
    )
    return closure.model_dump()


@router.get("/v1/freeze")
async def get_provider_v1_freeze(
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    del user_id
    builder = get_provider_v1_closure_builder()
    closure = builder.get_last_closure()
    if closure is None:
        closure = builder.build_closure()
    return closure.freeze.model_dump()
