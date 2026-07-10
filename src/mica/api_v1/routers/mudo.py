from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from mica.api_v1.auth import request_identity_dependency
from mica.identity.request_identity import RequestIdentity
from mica.infrastructure.unified_backend.database import get_db
from mica.mudo_foundation.contracts import (
    MUDOAsset,
    MUDOAssetCreateRequest,
    MUDOBranch,
    MUDOBranchCreateRequest,
    MUDOCommit,
    MUDOCommitCreateRequest,
    MUDOCodexTreeResponse,
    MUDODependencyEdge,
    MUDODependencyEdgeCreateRequest,
    MUDOFoundationCreateRequest,
    MUDORecomputeProposal,
    MUDOStalePropagationRequest,
    StudyCodexResponse,
    StudyMUDOLink,
    StudyMUDOLinkCreateRequest,
    StudyMUDOLinkResponse,
)
from mica.mudo_foundation.service import MUDOFoundationService
from src.models.mudo import MUDOCreateRequest, MUDOCreateResponse, MUDOResponse, MUDOUpdateRequest
from src.services.mudo_service import MUDOService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mudos", tags=["mudo"])
legacy_router = APIRouter(prefix="/api/v1/mudo", tags=["mudo-legacy"])
study_router = APIRouter(prefix="/api/v1/studies", tags=["mudo-study"])
_mudo_service = MUDOService()


class MUDOListItemResponse(BaseModel):
    mudo_id: str
    entity_type: str
    name: str
    version: int
    created_at: str
    updated_at: str
    description: str | None = None
    created_by: str | None = None
    owner_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: str


def _authenticated_user_id(user: Any) -> str:
    if isinstance(user, RequestIdentity):
        return user.user_id
    if isinstance(user, str):
        return user
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or user.get("id") or "anonymous")
    return str(user or "anonymous")


def _actor_roles(user: Any) -> list[str]:
    roles: set[str] = set()
    if isinstance(user, RequestIdentity):
        if user.membership_role is not None:
            roles.add(user.membership_role.value)
        custom_roles = user.custom_claims.get("roles")
        if isinstance(custom_roles, list):
            for role in custom_roles:
                normalized = str(role or "").strip().lower()
                if normalized:
                    roles.add(normalized)
    elif isinstance(user, dict):
        for key in ("role", "org_role"):
            normalized = str(user.get(key) or "").strip().lower()
            if normalized:
                roles.add(normalized)
        raw_roles = user.get("roles")
        if isinstance(raw_roles, list):
            for role in raw_roles:
                normalized = str(role or "").strip().lower()
                if normalized:
                    roles.add(normalized)
    return sorted(roles)


def _require_db_connection(db_conn: asyncpg.Connection | None) -> asyncpg.Connection:
    if db_conn is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return db_conn


def _normalize_create_response(payload: dict[str, Any]) -> dict[str, Any]:
    response = dict(payload)
    if "id" in response and "mudo_id" not in response:
        response["mudo_id"] = str(response.pop("id"))
    return response


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.exception("MUDO router failure")
    raise HTTPException(status_code=500, detail="MUDO operation failed") from exc


def _foundation_service(db_conn: asyncpg.Connection | None) -> MUDOFoundationService:
    return MUDOFoundationService.for_db_connection(db_conn)


def _is_foundation_create_payload(payload: dict[str, Any]) -> bool:
    return "workspace_id" in payload and "raw_data" not in payload


def _is_foundation_mudo_id(mudo_id: str) -> bool:
    return str(mudo_id or "").startswith("muo_")


def _foundation_to_create_response(created: Any) -> MUDOCreateResponse:
    return MUDOCreateResponse(
        mudo_id=created.mudo_id,
        entity_type="mudo_object",
        name=created.name,
        version=1,
        created_at=created.created_at,
        status="foundation_created",
    )


def _foundation_to_mudo_response(created: Any) -> MUDOResponse:
    return MUDOResponse(
        mudo_id=created.mudo_id,
        entity_type="mudo_object",
        name=created.name,
        version=1,
        created_at=created.created_at,
        updated_at=created.updated_at,
        description=created.description,
        created_by=created.owner_user_id,
        owner_id=created.owner_user_id,
        tags=[],
        raw_data={
            "workspace_id": created.workspace_id,
            "canonical_branch_id": created.canonical_branch_id,
            "fixture_mode": created.fixture_mode,
            "metadata": created.metadata,
        },
        annotations={},
        history=[],
        status="foundation_v1",
        suggested_actions=[],
    )


@router.post("", response_model=MUDOCreateResponse)
@legacy_router.post("", response_model=MUDOCreateResponse)
async def create_mudo(
    payload: dict[str, Any],
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOCreateResponse:
    user_id = _authenticated_user_id(user)
    actor_roles = _actor_roles(user)
    try:
        if _is_foundation_create_payload(payload):
            foundation_service = _foundation_service(db_conn)
            request = MUDOFoundationCreateRequest.model_validate(payload)
            payload_owner = str(request.owner_user_id or user_id).strip()
            if payload_owner != user_id and "admin" not in actor_roles:
                raise PermissionError("owner_user_id must match authenticated user")
            created = await foundation_service.create_mudo(request, owner_user_id=payload_owner)
            return _foundation_to_create_response(created)

        conn = _require_db_connection(db_conn)
        legacy_request = MUDOCreateRequest.model_validate(payload)
        created = await _mudo_service.create_mudo(conn, legacy_request.model_dump(), user_id=user_id)
        return MUDOCreateResponse.model_validate(_normalize_create_response(created))
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.get("", response_model=list[MUDOListItemResponse])
@legacy_router.get("", response_model=list[MUDOListItemResponse])
async def list_mudos(
    entity_type: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    tags: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> list[MUDOListItemResponse]:
    conn = _require_db_connection(db_conn)
    user_id = _authenticated_user_id(user)
    actor_roles = _actor_roles(user)
    parsed_tags = [tag.strip() for tag in (tags or "").split(",") if tag.strip()] or None
    try:
        records = await _mudo_service.list_mudos(
            conn,
            entity_type=entity_type,
            status=status_filter,
            tags=parsed_tags,
            limit=limit,
            offset=offset,
            owner_id=user_id,
            actor_roles=actor_roles,
        )
        return [MUDOListItemResponse.model_validate(record) for record in records]
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.get("/{mudo_id}", response_model=MUDOResponse)
@legacy_router.get("/{mudo_id}", response_model=MUDOResponse)
async def get_mudo(
    mudo_id: str,
    version: int | None = Query(default=None, ge=1),
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOResponse:
    user_id = _authenticated_user_id(user)
    actor_roles = _actor_roles(user)
    try:
        record = None
        foundation_service = _foundation_service(db_conn)
        if version is None and _is_foundation_mudo_id(mudo_id):
            foundation = await foundation_service.get_mudo(mudo_id, owner_user_id=user_id)
            if foundation is not None:
                return _foundation_to_mudo_response(foundation)
        if db_conn is not None:
            record = await _mudo_service.get_mudo(
                db_conn,
                mudo_id,
                version=version,
                owner_id=user_id,
                actor_roles=actor_roles,
            )
        if record is None and version is None:
            foundation = await foundation_service.get_mudo(mudo_id, owner_user_id=user_id)
            if foundation is not None:
                return _foundation_to_mudo_response(foundation)
        if record is None:
            raise HTTPException(status_code=404, detail=f"MUDO {mudo_id} not found")
        return MUDOResponse.model_validate(record)
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.put("/{mudo_id}", response_model=MUDOResponse)
@legacy_router.put("/{mudo_id}", response_model=MUDOResponse)
async def update_mudo(
    mudo_id: str,
    payload: MUDOUpdateRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOResponse:
    conn = _require_db_connection(db_conn)
    user_id = _authenticated_user_id(user)
    actor_roles = _actor_roles(user)
    try:
        updated = await _mudo_service.update_mudo(
            conn,
            mudo_id,
            payload.model_dump(exclude_none=True),
            user_id=user_id,
            actor_roles=actor_roles,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"MUDO {mudo_id} not found")
        return MUDOResponse.model_validate(updated)
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.delete("/{mudo_id}", status_code=status.HTTP_204_NO_CONTENT)
@legacy_router.delete("/{mudo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mudo(
    mudo_id: str,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> Response:
    conn = _require_db_connection(db_conn)
    user_id = _authenticated_user_id(user)
    actor_roles = _actor_roles(user)
    try:
        deleted = await _mudo_service.delete_mudo(
            conn,
            mudo_id,
            user_id=user_id,
            actor_roles=actor_roles,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail=f"MUDO {mudo_id} not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.get("/{mudo_id}/codex", response_model=MUDOCodexTreeResponse)
async def get_mudo_codex(
    mudo_id: str,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOCodexTreeResponse:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).list_codex_tree(mudo_id, owner_user_id=user_id)
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@study_router.post("/{study_id}/mudos", response_model=StudyMUDOLinkResponse)
async def link_mudo_to_study(
    study_id: str,
    payload: StudyMUDOLinkCreateRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> StudyMUDOLinkResponse:
    user_id = _authenticated_user_id(user)
    try:
        link = await _foundation_service(db_conn).link_mudo_to_study(
            study_id,
            payload,
            owner_user_id=user_id,
            created_by=user_id,
        )
        return StudyMUDOLinkResponse(link=link)
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@study_router.get("/{study_id}/mudos", response_model=list[StudyMUDOLink])
async def list_study_mudos(
    study_id: str,
    workspace_id: str = Query(..., min_length=1),
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> list[StudyMUDOLink]:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).list_study_mudos(
            study_id,
            workspace_id=workspace_id,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@study_router.get("/{study_id}/codex", response_model=StudyCodexResponse)
async def get_study_codex(
    study_id: str,
    workspace_id: str = Query(..., min_length=1),
    include_nodes: bool = Query(default=True),
    limit_per_mudo: int = Query(default=50, ge=1, le=200),
    depth: int = Query(default=1, ge=0, le=3),
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> StudyCodexResponse:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).get_study_codex(
            study_id,
            workspace_id=workspace_id,
            owner_user_id=user_id,
            include_nodes=include_nodes,
            limit_per_mudo=limit_per_mudo,
            depth=depth,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.post("/{mudo_id}/branches", response_model=MUDOBranch)
async def create_mudo_branch(
    mudo_id: str,
    payload: MUDOBranchCreateRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOBranch:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).create_branch(
            mudo_id,
            payload,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.post("/{mudo_id}/commits", response_model=MUDOCommit)
async def create_mudo_commit(
    mudo_id: str,
    payload: MUDOCommitCreateRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOCommit:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).create_commit(
            mudo_id,
            payload,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.post("/{mudo_id}/assets", response_model=MUDOAsset)
async def attach_mudo_asset(
    mudo_id: str,
    payload: MUDOAssetCreateRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDOAsset:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).attach_asset(
            mudo_id,
            payload,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.post("/{mudo_id}/dependencies", response_model=MUDODependencyEdge)
async def add_mudo_dependency(
    mudo_id: str,
    payload: MUDODependencyEdgeCreateRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDODependencyEdge:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).add_dependency_edge(
            mudo_id,
            payload,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.post("/{mudo_id}/stale-propagation")
async def propagate_mudo_stale(
    mudo_id: str,
    payload: MUDOStalePropagationRequest,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> dict[str, Any]:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).propagate_stale(
            mudo_id,
            payload.from_asset_id,
            owner_user_id=user_id,
            policy=payload.policy,
            reason=payload.reason,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.post("/{mudo_id}/recompute-proposals", response_model=MUDORecomputeProposal | None)
async def generate_recompute_proposal(
    mudo_id: str,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> MUDORecomputeProposal | None:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).generate_recompute_proposal(
            mudo_id,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)


@router.get("/{mudo_id}/recompute-proposals", response_model=list[MUDORecomputeProposal])
async def list_recompute_proposals(
    mudo_id: str,
    user: Any = Depends(request_identity_dependency),
    db_conn: asyncpg.Connection | None = Depends(get_db),
) -> list[MUDORecomputeProposal]:
    user_id = _authenticated_user_id(user)
    try:
        return await _foundation_service(db_conn).list_recompute_proposals(
            mudo_id,
            owner_user_id=user_id,
        )
    except Exception as exc:  # pragma: no cover
        _raise_http_error(exc)
