from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from mica.api_v1.auth import request_identity_dependency, user_dependency
from mica.api_v1.services.alejandria_search_service import (
    AlejandriaSearchFilters,
    AlejandriaSearchRequest,
    AttachToStudyRequest,
    BSMContextRequest,
    EntityDetectRequest,
    LMPExpandRequest,
    PromoteToKBRequest,
    PromoteToWorkingSetRequest,
    get_alejandria_search_service,
)

router = APIRouter(
    prefix="/api/v1/alejandria-search",
    tags=["alejandria-search"],
    dependencies=[Depends(user_dependency)],
)


@router.post("/runs")
async def create_search_run(
    body: AlejandriaSearchRequest,
    user_id: str = Depends(user_dependency),
    request_identity=Depends(request_identity_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.execute_search(
            request=body,
            user_id=user_id,
            request_identity=request_identity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
async def get_search_run(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_run(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/hits")
async def get_search_hits(
    run_id: str,
    user_id: str = Depends(user_dependency),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("final_score"),
    descending: bool = Query(True),
    year_from: int | None = Query(None),
    year_to: int | None = Query(None),
    open_access_only: bool = Query(False),
    has_fulltext: bool = Query(False),
    has_lmp_context: bool = Query(False),
    has_bsm_context: bool = Query(False),
    providers: list[str] | None = Query(None),
    journals: list[str] | None = Query(None),
    authors: list[str] | None = Query(None),
    organisms: list[str] | None = Query(None),
    proteins: list[str] | None = Query(None),
    genes: list[str] | None = Query(None),
):
    service = get_alejandria_search_service()
    filters = AlejandriaSearchFilters(
        year_from=year_from,
        year_to=year_to,
        open_access_only=open_access_only,
        has_fulltext=has_fulltext,
        has_lmp_context=has_lmp_context,
        has_bsm_context=has_bsm_context,
        providers=list(providers or []),
        journals=list(journals or []),
        authors=list(authors or []),
        organisms=list(organisms or []),
        proteins=list(proteins or []),
        genes=list(genes or []),
    )
    try:
        return await service.list_hits(
            run_id=run_id,
            user_id=user_id,
            filters=filters,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/analytics")
async def get_search_analytics(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_analytics(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/trace")
async def get_search_trace(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_trace(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/manifest")
async def get_search_manifest(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_manifest(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/abstracts")
async def get_search_abstracts(
    run_id: str,
    user_id: str = Depends(user_dependency),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_abstracts(run_id=run_id, user_id=user_id, limit=limit, offset=offset)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/resources")
async def get_search_resources(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_resource_manifest(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/fulltext")
async def get_search_fulltext_availability(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_fulltext_availability(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/sections")
async def get_search_sections(
    run_id: str,
    user_id: str = Depends(user_dependency),
    hit_id: str = Query(""),
    limit: int = Query(20, ge=1, le=200),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_sections(run_id=run_id, user_id=user_id, hit_id=hit_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/figures")
async def get_search_figures(
    run_id: str,
    user_id: str = Depends(user_dependency),
    hit_id: str = Query(""),
    limit: int = Query(20, ge=1, le=200),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_figures(run_id=run_id, user_id=user_id, hit_id=hit_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/dlm")
async def get_search_dlm(
    run_id: str,
    user_id: str = Depends(user_dependency),
    query: str = Query(""),
    entity_type: str = Query(""),
    limit: int = Query(20, ge=1, le=200),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_dlm_matches(
            run_id=run_id,
            user_id=user_id,
            query=query,
            entity_type=entity_type,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/pdf-audit")
async def get_search_pdf_audit(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_pdf_audit(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/ranking-trace")
async def get_search_ranking_trace(
    run_id: str,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_ranking_trace(run_id=run_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.get("/runs/{run_id}/artifacts/{artifact_name:path}")
async def get_search_artifact(
    run_id: str,
    artifact_name: str,
    user_id: str = Depends(user_dependency),
    include_body: bool = Query(False),
):
    service = get_alejandria_search_service()
    try:
        return await service.get_artifact(
            run_id=run_id,
            user_id=user_id,
            artifact_name=artifact_name,
            include_body=include_body,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc


@router.post("/entity-detect")
async def entity_detect(
    body: EntityDetectRequest,
    user_id: str = Depends(user_dependency),
):
    del user_id
    service = get_alejandria_search_service()
    return service.detect_entities(body)


@router.post("/lmp-expand")
async def lmp_expand(
    body: LMPExpandRequest,
    user_id: str = Depends(user_dependency),
):
    del user_id
    service = get_alejandria_search_service()
    return service.expand_lmp_context(body)


@router.post("/bsm-context")
async def bsm_context(
    body: BSMContextRequest,
    user_id: str = Depends(user_dependency),
):
    del user_id
    service = get_alejandria_search_service()
    return service.build_bsm_context(body)


@router.post("/runs/{run_id}/promote-to-kb")
async def promote_to_kb(
    run_id: str,
    body: PromoteToKBRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    dss = getattr(request.app.state, "document_scan_service", None)
    try:
        return await service.promote_to_kb(
            run_id=run_id,
            user_id=user_id,
            request=body,
            document_scan_service=dss,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/promote-to-working-set")
async def promote_to_working_set(
    run_id: str,
    body: PromoteToWorkingSetRequest,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.promote_to_working_set(
            run_id=run_id,
            user_id=user_id,
            request=body,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/attach-to-study")
async def attach_to_study(
    run_id: str,
    body: AttachToStudyRequest,
    user_id: str = Depends(user_dependency),
):
    service = get_alejandria_search_service()
    try:
        return await service.attach_to_study(
            run_id=run_id,
            user_id=user_id,
            request=body,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Search run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
