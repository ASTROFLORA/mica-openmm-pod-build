from __future__ import annotations

import os
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.services.vertex_svg_icon_service import DEFAULT_SIGNED_URL_TTL, VertexSvgIconService
from mica.storage.gcs_user_storage import get_storage_manager, storage_status

router = APIRouter(prefix="/api/v1/icons", tags=["icon-generation"])


class SvgGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=4, max_length=4000)
    generation_mode: Literal["icon", "diagram", "pathway"] = "icon"
    title: str = Field(default="", max_length=200)
    category_id: str = Field(default="custom", max_length=100)
    semantic_tags: List[str] = Field(default_factory=list)
    persist: bool = True
    signed_url_ttl: int = Field(default=DEFAULT_SIGNED_URL_TTL, ge=60, le=3600)
    model_override: Optional[str] = Field(default=None, max_length=120)


class SvgSweepRequest(BaseModel):
    category_id: str = Field(..., max_length=100)
    item_ids: List[str] = Field(default_factory=list)
    limit: int = Field(default=4, ge=1, le=8)
    persist: bool = True
    signed_url_ttl: int = Field(default=DEFAULT_SIGNED_URL_TTL, ge=60, le=3600)
    model_override: Optional[str] = Field(default=None, max_length=120)


def get_icon_service() -> VertexSvgIconService:
    return VertexSvgIconService()


@router.get("/catalog")
async def get_icon_catalog(service: VertexSvgIconService = Depends(get_icon_service)) -> Dict[str, Any]:
    return service.catalog()


@router.get("/runs")
async def list_recent_svg_runs(
    limit: int = Query(default=12, ge=1, le=50),
    user_id: str = Depends(user_dependency),
    service: VertexSvgIconService = Depends(get_icon_service),
) -> Dict[str, Any]:
    return {"ok": True, **(await service.list_recent_runs(user_id=user_id, limit=limit))}


@router.post("/generate")
async def generate_svg_asset(
    payload: SvgGenerateRequest,
    user_id: str = Depends(user_dependency),
    service: VertexSvgIconService = Depends(get_icon_service),
) -> Dict[str, Any]:
    try:
        return await service.generate(
            user_id=user_id,
            prompt=payload.prompt,
            generation_mode=payload.generation_mode,
            title=payload.title,
            category_id=payload.category_id,
            semantic_tags=payload.semantic_tags,
            persist=payload.persist,
            signed_url_ttl=payload.signed_url_ttl,
            model_override=payload.model_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sweep")
async def sweep_default_library(
    payload: SvgSweepRequest,
    user_id: str = Depends(user_dependency),
    service: VertexSvgIconService = Depends(get_icon_service),
) -> Dict[str, Any]:
    try:
        return await service.sweep(
            user_id=user_id,
            category_id=payload.category_id,
            item_ids=payload.item_ids,
            limit=payload.limit,
            persist=payload.persist,
            signed_url_ttl=payload.signed_url_ttl,
            model_override=payload.model_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
async def get_svg_run_manifest(
    run_id: str,
    user_id: str = Depends(user_dependency),
    service: VertexSvgIconService = Depends(get_icon_service),
) -> Dict[str, Any]:
    try:
        manifest = await service.get_run_manifest(user_id=user_id, run_id=run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id, "manifest": manifest}


@router.get("/health")
async def icon_generation_health(
    probe_storage: bool = Query(default=False),
) -> Dict[str, Any]:
    storage = {"checked": False}
    if probe_storage:
        storage = storage_status()
        if storage.get("configured") and not storage.get("ready"):
            try:
                get_storage_manager()
            except HTTPException:
                pass
            storage = storage_status()

    status = {
        "status": "ok",
        "vertex": {
            "project_configured": bool(
                os.getenv("MICA_VERTEX_PROJECT_ID")
                or os.getenv("VERTEX_PROJECT_ID")
                or os.getenv("GCP_PROJECT_ID")
                or os.getenv("GCP_PROJECT")
                or os.getenv("GOOGLE_CLOUD_PROJECT")
            ),
            "location": os.getenv("MICA_VERTEX_LOCATION")
            or os.getenv("VERTEX_LOCATION")
            or os.getenv("GOOGLE_CLOUD_LOCATION")
            or "auto",
        },
        "storage": storage,
    }
    if probe_storage and not status["storage"].get("configured"):
        status["status"] = "degraded"
    return status