from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.serverless_models import (
    ModelInvocationRequest,
    ModelInvocationResult,
    build_default_serverless_model_gateway,
)
from mica.serverless_models.contracts import build_execution_record, make_failed_result
from mica.serverless_models.frontend_support import (
    build_boltz2_frontend_inputs,
    resolve_ligands_with_cross_validation,
)
from mica.serverless_models.models.esm3 import ESM3_DESCRIPTOR, ESM3InferenceRequest
from mica.serverless_models.models.esm3_modal_app import (
    run_esm3_modal_preflight_smoke,
    run_esm3_modal_tiny_smoke,
)
from mica.serverless_models.result_materializer import ResultMaterializer


router = APIRouter(prefix="/api/v1/serverless-models", tags=["serverless-models"])
_gateway_cache: dict[str, object] = {}
_result_materializer = ResultMaterializer()


class ServerlessModelInvokeRequest(BaseModel):
    model_id: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    requested_by: str = "api"
    provider_override: Optional[str] = None


class Boltz2ParseInputRequest(BaseModel):
    protein_sequence: Optional[str] = None
    protein_fasta: Optional[str] = None
    protein_chain_id: str = "A"
    ligand_smiles: Optional[str] = None
    ligand_id: str = "B"
    predict_affinity: bool = False


class ESM3ServerlessInvokeRequest(BaseModel):
    sequence: str
    num_steps: int = Field(default=1, ge=1, le=32)
    output_formats: List[str] = Field(default_factory=lambda: ["mmcif"])
    source_context: Dict[str, Any] = Field(default_factory=dict)
    max_length: int = Field(default=2048, ge=1, le=2048)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    uniprot_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    requested_by: str = "api_esm3_endpoint"
    provider_override: Optional[str] = None
    timeout_seconds: int = Field(default=900, ge=60, le=1800)


class ESM3ServerlessPreflightRequest(BaseModel):
    attempt_live_remote: bool = True
    include_image_preflight: bool = True
    include_fixture_probe: bool = False
    timeout_seconds: int = Field(default=180, ge=30, le=1800)


def _safe_user_component(user_id: str | None) -> str:
    value = (user_id or "anonymous").strip() or "anonymous"
    return value.replace("/", "_").replace("\\", "_")


def get_serverless_model_gateway(user_id: str | None = None):
    cache_key = _safe_user_component(user_id)
    gateway = _gateway_cache.get(cache_key)
    if gateway is None:
        artifact_root = Path(
            os.getenv("MICA_SERVERLESS_MODEL_ARTIFACT_DIR") or ".artifacts/serverless_models/api_v1"
        ) / cache_key
        gateway = build_default_serverless_model_gateway(artifact_base_dir=str(artifact_root))
        _gateway_cache[cache_key] = gateway
    return gateway


async def close_serverless_model_gateways() -> None:
    gateways = list(_gateway_cache.values())
    _gateway_cache.clear()
    for gateway in gateways:
        close = getattr(gateway, "close", None)
        if close is None:
            continue
        await close()


def _result_to_dict(result) -> Dict[str, Any]:
    return asdict(result)


def _format_sse(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _build_request(
    payload: ServerlessModelInvokeRequest,
    *,
    user_id: str,
) -> ModelInvocationRequest:
    request_id = str(uuid.uuid4())
    session_id = payload.session_id or f"serverless-session-{user_id}"
    run_id = payload.run_id or request_id
    return ModelInvocationRequest(
        request_id=request_id,
        model_id=payload.model_id,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        inputs=dict(payload.inputs),
        metadata=dict(payload.metadata),
        requested_by=payload.requested_by,
        provider_override=payload.provider_override,
    )


def _build_esm3_request(
    payload: ESM3ServerlessInvokeRequest,
    *,
    user_id: str,
) -> ModelInvocationRequest:
    request_id = str(uuid.uuid4())
    session_id = payload.session_id or f"serverless-session-{user_id}"
    run_id = payload.run_id or request_id
    try:
        esm3_request = ESM3InferenceRequest(
            sequence=payload.sequence,
            num_steps=payload.num_steps,
            output_formats=list(payload.output_formats),
            source_context=dict(payload.source_context),
            max_length=payload.max_length,
            metadata=dict(payload.metadata),
            uniprot_id=str(payload.uniprot_id or "").strip(),
        )
        return esm3_request.to_invocation_request(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            requested_by=payload.requested_by,
            provider_override=payload.provider_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _invoke_esm3_runtime(
    request: ModelInvocationRequest,
    *,
    timeout_seconds: int,
) -> ModelInvocationResult:
    try:
        smoke_result = await run_esm3_modal_tiny_smoke(
            dict(request.inputs),
            timeout_seconds=timeout_seconds,
        )
        raw_output = dict(smoke_result.get("output") or {})
        if str(smoke_result.get("status") or "") != "completed" or not raw_output:
            failure_reason = str(smoke_result.get("failure_detail") or smoke_result.get("failure_code") or "ESM3 runtime failed")
            return make_failed_result(
                request=request,
                provider="modal",
                error=failure_reason,
                raw_output=smoke_result,
                execution_record=build_execution_record(
                    request,
                    provider="modal",
                    state="failed",
                    provider_target="modal_sdk_app_run",
                    failure_reason=failure_reason,
                    raw_status=smoke_result,
                ),
            )

        normalized_output = _result_materializer.materialize(
            descriptor=ESM3_DESCRIPTOR,
            request=request,
            raw_output=raw_output,
        )
        archive_members = list(normalized_output.get("archive_members") or [])
        return ModelInvocationResult(
            request_id=request.request_id,
            model_id=request.model_id,
            state="completed",
            provider="modal",
            normalized_output=normalized_output,
            artifact_ids=[],
            artifact_uris=[],
            ui_payload={
                "render_hint": ESM3_DESCRIPTOR.render_hint,
                "title": ESM3_DESCRIPTOR.display_name,
                "summary": ESM3_DESCRIPTOR.description,
                "artifacts": archive_members,
            },
            metrics={
                "artifact_count": len(archive_members),
                "endpoint_mode": "esm3_modal_runtime_helper",
            },
            raw_output=raw_output,
            execution_record=build_execution_record(
                request,
                provider="modal",
                state="completed",
                provider_target="modal_sdk_app_run",
                raw_status=raw_output,
            ),
        )
    except RuntimeError as exc:
        return make_failed_result(
            request=request,
            provider="modal",
            error=str(exc),
            execution_record=build_execution_record(
                request,
                provider="modal",
                state="failed",
                provider_target="modal_sdk_app_run",
                failure_reason=str(exc),
            ),
        )


@router.get("")
async def list_serverless_models(user_id: str = Depends(user_dependency)) -> Dict[str, List[Dict[str, Any]]]:
    gateway = get_serverless_model_gateway(user_id)
    return {"models": [asdict(descriptor) for descriptor in gateway.list_models()]}


@router.post("/boltz2/parse-input")
async def parse_boltz2_input(
    payload: Boltz2ParseInputRequest,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    try:
        return build_boltz2_frontend_inputs(
            protein_sequence=payload.protein_sequence,
            protein_fasta=payload.protein_fasta,
            protein_chain_id=payload.protein_chain_id,
            ligand_smiles=payload.ligand_smiles,
            ligand_id=payload.ligand_id,
            predict_affinity=payload.predict_affinity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/boltz2/ligands/search")
async def search_boltz2_ligands(
    query: str,
    limit: int = Query(default=5, ge=1, le=10),
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    try:
        return await resolve_ligands_with_cross_validation(query, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/invoke")
async def invoke_serverless_model(
    payload: ServerlessModelInvokeRequest,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway = get_serverless_model_gateway(user_id)
    request = _build_request(payload, user_id=user_id)
    try:
        result = await gateway.invoke(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _result_to_dict(result)


@router.post("/esm3/preflight")
async def preflight_esm3_serverless_endpoint(
    payload: ESM3ServerlessPreflightRequest,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    del user_id
    return await run_esm3_modal_preflight_smoke(
        include_image_preflight=payload.include_image_preflight,
        include_fixture_probe=payload.include_fixture_probe,
        attempt_live_remote=payload.attempt_live_remote,
        timeout_seconds=payload.timeout_seconds,
    )


@router.post("/esm3/invoke")
async def invoke_esm3_serverless_endpoint(
    payload: ESM3ServerlessInvokeRequest,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    request = _build_esm3_request(payload, user_id=user_id)
    result = await _invoke_esm3_runtime(request, timeout_seconds=payload.timeout_seconds)
    if result.state == "failed":
        raise HTTPException(status_code=503, detail=result.error or "ESM3 serverless inference failed")
    return _result_to_dict(result)


@router.post("/submit")
async def submit_serverless_model(
    payload: ServerlessModelInvokeRequest,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway = get_serverless_model_gateway(user_id)
    request = _build_request(payload, user_id=user_id)
    try:
        result = await gateway.submit(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _result_to_dict(result)


@router.get("/requests/{request_id}")
async def get_serverless_model_result(
    request_id: str,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway = get_serverless_model_gateway(user_id)
    try:
        result = await gateway.get_result(request_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _result_to_dict(result)


@router.get("/requests/{request_id}/stream")
async def stream_serverless_model_result(
    request_id: str,
    poll_interval_seconds: float = 1.0,
    heartbeat_interval_seconds: float = 15.0,
    user_id: str = Depends(user_dependency),
):
    gateway = get_serverless_model_gateway(user_id)
    try:
        initial_result = await gateway.get_result(request_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _gen():
        if initial_result.state in {"completed", "failed", "cancelled"}:
            yield _format_sse("serverless_terminal", _result_to_dict(initial_result))
            return

        yield _format_sse("serverless_result", _result_to_dict(initial_result))
        async for event_name, result in gateway.iter_result_updates(
            request_id,
            user_id=user_id,
            initial_result=None,
            poll_interval_seconds=poll_interval_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        ):
            payload = _result_to_dict(result)
            if event_name == "keepalive":
                payload = {
                    "request_id": result.request_id,
                    "state": result.state,
                    "provider_job_id": result.provider_job_id,
                    "ts": time.time(),
                }
            yield _format_sse(event_name, payload)

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


@router.post("/requests/{request_id}/cancel")
async def cancel_serverless_model(
    request_id: str,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    gateway = get_serverless_model_gateway(user_id)
    try:
        result = await gateway.cancel(request_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _result_to_dict(result)
