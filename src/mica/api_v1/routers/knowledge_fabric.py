"""Knowledge Fabric HTTP endpoints — KB CRUD + public listing.

Prefix: /api/v1/kbs
Tags:   knowledge-fabric

Endpoints
---------
GET  /api/v1/kbs                     list authenticated caller's KBs
GET  /api/v1/kbs/public              list all global/public KBs (any authed user)
POST /api/v1/kbs                     create a KB (global owner_scope requires admin)
GET  /api/v1/kbs/{kb_id}             fetch single KB by ID
PATCH /api/v1/kbs/{kb_id}/status     update KB status
DELETE /api/v1/kbs/{kb_id}           archive KB (sets status=ARCHIVED)
GET  /api/v1/kbs/{kb_id}/runs        list runs for a KB
POST /api/v1/kbs/{kb_id}/runs        create a new run on a KB

Admin gating
------------
Creating a KB with ``owner_scope=global`` or mutating a global KB requires the
caller's user_id to be listed in the ``MICA_ADMIN_USER_IDS`` env var.

Access pattern for KBService
-----------------------------
The KBService is wired onto ``app.state.kb_service`` by the ``_startup`` handler
in ``api_v1/main.py``.  Routers access it via ``request.app.state``.

    async def my_endpoint(request: Request, user_id: str = Depends(user_dependency)):
        svc = request.app.state.kb_service
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict, FrozenSet, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.pipelines.knowledge_fabric.contracts import JobKind, KBStatus, KBType, OwnerScope
from mica.pipelines.knowledge_fabric.document_envelope import DocumentKind, DocumentScanMode
from mica.pipelines.knowledge_fabric.document_scan_service import DocumentScanService

router = APIRouter(prefix="/api/v1/kbs", tags=["knowledge-fabric"])


# ---------------------------------------------------------------------------
# Inline admin gate (avoids circular imports with admin_auth.py)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _admin_ids() -> FrozenSet[str]:
    raw = os.getenv("MICA_ADMIN_USER_IDS", "")
    return frozenset(uid.strip() for uid in raw.split(",") if uid.strip())


def _assert_admin(user_id: str) -> None:
    """Raise HTTP 403/503 unless the caller is a configured admin."""
    ids = _admin_ids()
    if not ids:
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin access is not configured. "
                "Set MICA_ADMIN_USER_IDS to enable this operation."
            ),
        )
    if user_id not in ids:
        raise HTTPException(status_code=403, detail="Admin access required")


def _is_admin(user_id: str) -> bool:
    ids = _admin_ids()
    return bool(ids) and user_id in ids


def _policy_user_set(policies: Dict[str, Any], key: str) -> Set[str]:
    raw = policies.get(key, []) if isinstance(policies, dict) else []
    if not isinstance(raw, list):
        return set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _can_read_kb(user_id: str, kb: Any) -> bool:
    if kb.owner_scope == OwnerScope.GLOBAL:
        return True
    if kb.owner_scope == OwnerScope.USER:
        return kb.owner_id == user_id
    policies = kb.policies or {}
    if kb.owner_scope == OwnerScope.TEAM:
        team_members = _policy_user_set(policies, "team_member_user_ids")
        team_admins = _policy_user_set(policies, "team_admin_user_ids")
        return user_id == kb.owner_id or user_id in team_members or user_id in team_admins or _is_admin(user_id)
    if kb.owner_scope == OwnerScope.WORKSPACE:
        workspace_members = _policy_user_set(policies, "workspace_member_user_ids")
        workspace_admins = _policy_user_set(policies, "workspace_admin_user_ids")
        return user_id == kb.owner_id or user_id in workspace_members or user_id in workspace_admins or _is_admin(user_id)
    return False


def _can_mutate_kb(user_id: str, kb: Any) -> bool:
    if kb.owner_scope == OwnerScope.GLOBAL:
        return _is_admin(user_id)
    if kb.owner_scope == OwnerScope.USER:
        return kb.owner_id == user_id
    policies = kb.policies or {}
    if kb.owner_scope == OwnerScope.TEAM:
        team_admins = _policy_user_set(policies, "team_admin_user_ids")
        return user_id == kb.owner_id or user_id in team_admins or _is_admin(user_id)
    if kb.owner_scope == OwnerScope.WORKSPACE:
        workspace_admins = _policy_user_set(policies, "workspace_admin_user_ids")
        return user_id == kb.owner_id or user_id in workspace_admins or _is_admin(user_id)
    return False


def _assert_can_mutate(user_id: str, kb: Any) -> None:
    """Raise HTTP 403/404 if the caller cannot mutate the given KB.

    - Global KBs: admin-only
    - User/workspace KBs: owner must match
    """
    if _can_mutate_kb(user_id, kb):
        return
    if kb.owner_scope == OwnerScope.USER:
        raise HTTPException(status_code=404, detail="KB not found")
    raise HTTPException(status_code=403, detail="Insufficient scope permissions")


def _assert_can_read(user_id: str, kb: Any) -> None:
    if _can_read_kb(user_id, kb):
        return
    raise HTTPException(status_code=404, detail="KB not found")


def _authoritative_owner_id(user_id: str, kb: Any) -> str:
    if kb.owner_scope == OwnerScope.GLOBAL:
        return user_id
    if kb.owner_scope == OwnerScope.TEAM:
        return str(kb.owner_id or "").strip()
    return str(kb.owner_id or user_id).strip()


async def _load_authorized_kb(request: Request, *, kb_id: str, user_id: str) -> Any:
    svc = _kb_service(request)
    kb = await svc.get_kb(kb_id, owner_id=user_id)
    if kb is None:
        # Team-scoped policy membership is currently evaluated at the KB layer,
        # so non-owner members still need one unscoped fetch before the router can
        # derive the authoritative owner id for subsequent operations.
        kb = await svc.get_kb(kb_id, owner_id="")
        if kb is None:
            raise HTTPException(status_code=404, detail="KB not found")
    _assert_can_read(user_id, kb)
    return kb


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------

class CreateKBRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kb_type: KBType = Field(KBType.QUERY)
    owner_scope: OwnerScope = Field(OwnerScope.USER)
    owner_id: str = Field("", description="Required when owner_scope=team")
    workspace_id: str = Field("", description="Required when owner_scope=workspace")
    canonical_query: str = Field("")
    target_entities: List[str] = Field(default_factory=list)
    target_topics: List[str] = Field(default_factory=list)
    policies: Dict[str, Any] = Field(default_factory=dict)


class UpdateKBStatusRequest(BaseModel):
    status: KBStatus


class CreateRunRequest(BaseModel):
    run_type: JobKind = Field(JobKind.KB_BUILD_QUERY)
    query: str = Field("")
    entity_focus: List[str] = Field(default_factory=list)
    topic_focus: List[str] = Field(default_factory=list)


class InlineScanDocument(BaseModel):
    name: str = Field(..., min_length=1, max_length=400)
    text: str = Field("")
    acquisition_type: str = Field("")
    source_format: str = Field("")
    document_modality: str = Field("")
    modality_metadata: Dict[str, Any] = Field(default_factory=dict)


class ScanKnowledgeBaseRequest(BaseModel):
    mode: DocumentScanMode = Field(DocumentScanMode.DLM_SECTIONS)
    documents: List[InlineScanDocument] = Field(default_factory=list)
    session_id: str = Field("")
    asset_ids: List[str] = Field(default_factory=list)


class PromoteKnowledgeBaseScanRequest(BaseModel):
    scan_id: str = Field(..., min_length=1)
    minimum_evidentiality_score: float = Field(0.5, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _kb_service(request: Request):
    """Return the KBService wired onto app.state, or raise 503."""
    svc = getattr(request.app.state, "kb_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="KB service is not available (startup may have failed)",
        )
    return svc


def _document_scan_service(request: Request) -> DocumentScanService:
    svc = getattr(request.app.state, "document_scan_service", None)
    if svc is None:
        svc = DocumentScanService(store=getattr(request.app.state, "kb_store", None))
        request.app.state.document_scan_service = svc
    return svc


def _lineage_payload(
    kb: Any,
    *,
    run_id: str = "",
    scan_id: str = "",
    promotion_id: str = "",
) -> Dict[str, Any]:
    return {
        "kb_id": kb.kb_id,
        "run_id": run_id,
        "scan_id": scan_id,
        "promotion_id": promotion_id,
        "owner_scope": kb.owner_scope.value if hasattr(kb.owner_scope, "value") else str(kb.owner_scope),
        "owner_id": kb.owner_id,
        "workspace_id": kb.workspace_id,
        "graph_namespace": kb.graph_namespace,
        "storage_manifest_uri": kb.storage_manifest_uri,
    }


def _guess_asset_acquisition(asset_name: str, asset_type: str) -> tuple[str, str, str]:
    lower_name = str(asset_name or "").lower()
    lower_type = str(asset_type or "").lower()
    combined = f"{lower_type} {lower_name}"

    if any(token in combined for token in (".pdb", ".cif", "mmcif", " pdb")):
        return "structure_pdb", "pdb", "pdb_structure"
    if any(token in combined for token in (".xml", "nesymol", "generator_v4", "lmp")):
        return "nesymol_xml", "xml", "nesymol_xml"
    if any(token in combined for token in (".xtc", ".dcd", "trajectory", "dynamic", "md")):
        return "dynamic_trajectory", "trajectory", "dynamic_trajectory"
    if any(token in combined for token in (".pdf", ".txt", ".md", "literature", "paper", "document")):
        return "literature_document", "text", "literature"
    return "kb_source_document", "text", "generic_text"


def _serialize_kb(kb: Any) -> Dict[str, Any]:
    payload = kb.to_dict()
    payload["lineage"] = _lineage_payload(kb)
    return payload


def _serialize_run(run: Any, kb: Any) -> Dict[str, Any]:
    payload = run.to_dict()
    payload["lineage"] = _lineage_payload(kb, run_id=run.run_id)
    return payload


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/public",
    summary="List all global / public knowledge bases",
    response_description="Flat list of global KBs serialized as dicts",
)
async def list_public_kbs(
    request: Request,
    _uid: str = Depends(user_dependency),  # auth still required, just no scope filter
) -> List[Dict[str, Any]]:
    """Return all KBs with ``owner_scope=global``.

    Any authenticated user may call this endpoint — no admin check.
    Results are ordered by creation date (descending) by the store.
    """
    svc = _kb_service(request)
    kbs = await svc.list_global_kbs()
    return [_serialize_kb(kb) for kb in kbs]


@router.get(
    "",
    summary="List caller's knowledge bases",
    response_description="Flat list of KBs owned by the caller",
)
async def list_kbs(
    request: Request,
    include_global: bool = Query(False, description="Also return global/public KBs"),
    team_id: Optional[str] = Query(None, description="Include KBs owned by team/lab id"),
    workspace_id: Optional[str] = Query(None, description="Filter by workspace"),
    user_id: str = Depends(user_dependency),
) -> List[Dict[str, Any]]:
    """Return KBs owned by the authenticated user.

    Pass ``include_global=true`` to also include all global KBs in one call.
    """
    svc = _kb_service(request)
    dedup: Dict[str, Any] = {}

    owned = await svc.list_kbs(owner_id=user_id, workspace_id=workspace_id or "", include_global=False)
    for kb in owned:
        dedup[kb.kb_id] = kb

    if team_id:
        team_kbs = await svc.list_kbs(owner_id=team_id, workspace_id=workspace_id or "", include_global=False)
        for kb in team_kbs:
            dedup[kb.kb_id] = kb

    if workspace_id:
        workspace_kbs = await svc.list_kbs(owner_id="", workspace_id=workspace_id, include_global=False)
        for kb in workspace_kbs:
            dedup[kb.kb_id] = kb

    if include_global:
        for kb in await svc.list_global_kbs():
            dedup[kb.kb_id] = kb

    visible = [kb for kb in dedup.values() if _can_read_kb(user_id, kb)]
    return [_serialize_kb(kb) for kb in visible]


@router.post(
    "",
    status_code=201,
    summary="Create a knowledge base",
    response_description="Created KB serialized as dict",
)
async def create_kb(
    body: CreateKBRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Create a new KB.

    If ``owner_scope`` is ``global`` the caller must be listed in
    ``MICA_ADMIN_USER_IDS`` — otherwise HTTP 403.
    """
    requested_owner_id = (body.owner_id or "").strip()
    if body.owner_scope == OwnerScope.GLOBAL:
        _assert_admin(user_id)
        resolved_owner_id = ""
    elif body.owner_scope == OwnerScope.TEAM:
        if not requested_owner_id:
            raise HTTPException(status_code=422, detail="owner_id is required when owner_scope=team")
        if user_id != requested_owner_id and user_id not in _policy_user_set(body.policies, "team_member_user_ids") and user_id not in _policy_user_set(body.policies, "team_admin_user_ids") and not _is_admin(user_id):
            raise HTTPException(status_code=403, detail="Caller is not authorized for the requested team scope")
        resolved_owner_id = requested_owner_id
    else:
        resolved_owner_id = user_id

    if body.owner_scope == OwnerScope.WORKSPACE and not (body.workspace_id or "").strip():
        raise HTTPException(status_code=422, detail="workspace_id is required when owner_scope=workspace")

    svc = _kb_service(request)
    try:
        kb = await svc.create_kb(
            name=body.name,
            kb_type=body.kb_type,
            owner_scope=body.owner_scope,
            owner_id=resolved_owner_id,
            workspace_id=body.workspace_id,
            canonical_query=body.canonical_query,
            target_entities=body.target_entities,
            target_topics=body.target_topics,
            policies=body.policies,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize_kb(kb)


@router.get(
    "/{kb_id}",
    summary="Get a single knowledge base",
    response_description="KB serialized as dict",
)
async def get_kb(
    kb_id: str,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Fetch a KB by ID.

    Returns the KB if:

    * The caller is the owner, OR
    * The KB is global (visible to any authenticated user).

    Global KBs are visible to all users; no admin check required for reads.
    """
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    return _serialize_kb(kb)


@router.patch(
    "/{kb_id}/status",
    summary="Update KB status",
    response_description="Updated KB serialized as dict",
)
async def update_kb_status(
    kb_id: str,
    body: UpdateKBStatusRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Update the lifecycle status of a KB.

    * Caller must be the KB owner, OR
    * KB is global — requires admin.
    """
    svc = _kb_service(request)
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    _assert_can_mutate(user_id, kb)

    owner_id = _authoritative_owner_id(user_id, kb)
    await svc.update_kb_status(kb_id, body.status, owner_id=owner_id)
    updated = await svc.get_kb(kb_id, owner_id=owner_id)
    if updated:
        return _serialize_kb(updated)
    return {
        "kb_id": kb_id,
        "status": body.status.value,
        "lineage": _lineage_payload(kb),
    }


@router.delete(
    "/{kb_id}",
    status_code=200,
    summary="Archive a knowledge base",
    response_description="Archived KB serialized as dict",
)
async def archive_kb(
    kb_id: str,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Soft-delete a KB by setting its status to ``ARCHIVED``.

    * Caller must be the KB owner, OR
    * KB is global — requires admin.
    """
    svc = _kb_service(request)
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    _assert_can_mutate(user_id, kb)

    owner_id = _authoritative_owner_id(user_id, kb)
    await svc.update_kb_status(kb_id, KBStatus.ARCHIVED, owner_id=owner_id)
    updated = await svc.get_kb(kb_id, owner_id=owner_id)
    if updated:
        return _serialize_kb(updated)
    return {
        "kb_id": kb_id,
        "status": "archived",
        "lineage": _lineage_payload(kb),
    }


@router.get(
    "/{kb_id}/runs",
    summary="List runs for a knowledge base",
    response_description="Flat list of runs serialized as dicts",
)
async def list_runs(
    kb_id: str,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> List[Dict[str, Any]]:
    """Return all runs for a KB.

    The KB must be visible to the caller (owned or global).
    """
    svc = _kb_service(request)
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    runs = await svc.list_runs(kb_id=kb_id, owner_id=_authoritative_owner_id(user_id, kb))
    return [_serialize_run(r, kb) for r in runs]


@router.post(
    "/{kb_id}/runs",
    status_code=201,
    summary="Create a run on a knowledge base",
    response_description="Created run serialized as dict",
)
async def create_run(
    kb_id: str,
    body: CreateRunRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    """Create a new ingestion/analysis run against an existing KB.

    Global KBs: caller must be admin to create a run (ingestion is privileged).
    User KBs: caller must be the owner.
    """
    svc = _kb_service(request)
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    _assert_can_mutate(user_id, kb)

    run = await svc.create_run(
        kb_id=kb_id,
        run_type=body.run_type,
        query=body.query,
        entity_focus=body.entity_focus,
        topic_focus=body.topic_focus,
        owner_id=_authoritative_owner_id(user_id, kb),
    )
    return _serialize_run(run, kb)


@router.post(
    "/{kb_id}/scan",
    summary="Scan KB source documents into sections/entities/claims",
)
async def scan_knowledge_base(
    kb_id: str,
    body: ScanKnowledgeBaseRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    _assert_can_mutate(user_id, kb)

    documents: List[InlineScanDocument] = list(body.documents)
    if body.session_id and body.asset_ids:
        from mica.api_v1.routers.workspace import _extract_text_from_bytes, _get_backend

        backend = _get_backend()
        for asset_id in body.asset_ids:
            data, asset_record = backend.read_asset_content(user_id, body.session_id, asset_id)
            extracted = _extract_text_from_bytes(data, asset_record["name"], asset_record.get("asset_type", "other"))
            acq_type, source_format, modality = _guess_asset_acquisition(
                str(asset_record.get("name") or asset_id),
                str(asset_record.get("asset_type") or "other"),
            )
            documents.append(
                InlineScanDocument(
                    name=str(asset_record.get("name") or asset_id),
                    text=str(extracted.get("text") or ""),
                    acquisition_type=acq_type,
                    source_format=source_format,
                    document_modality=modality,
                    modality_metadata={
                        "asset_type": str(asset_record.get("asset_type") or "other"),
                        "source_session_id": body.session_id,
                        "source_format": str(extracted.get("format") or source_format),
                    },
                )
            )

    if not documents:
        raise HTTPException(status_code=422, detail="Provide at least one document or workspace asset")

    scan_service = _document_scan_service(request)
    scans_payload: List[Dict[str, Any]] = []
    last_status = "queued"
    for doc in documents:
        scan_result = await scan_service.create_scan(
            title=doc.name,
            text=doc.text,
            mode=body.mode,
            document_kind=DocumentKind.KB_SOURCE,
            owner_id=kb.owner_id or user_id,
            workspace_id=kb.workspace_id,
            kb_id=kb.kb_id,
            provider="kb_inline" if doc in body.documents else "workspace_asset",
            acquisition_type=doc.acquisition_type or "kb_source_document",
            metadata={
                "owner_scope": kb.owner_scope.value,
                "lineage": _lineage_payload(kb),
                "source_session_id": body.session_id,
                "document_modality": doc.document_modality,
                "source_format": doc.source_format,
                **dict(doc.modality_metadata or {}),
            },
        )
        payload = scan_result.model_dump()
        payload["lineage"] = _lineage_payload(kb, scan_id=scan_result.scan_id)
        scans_payload.append(payload)
        last_status = payload.get("status") or last_status

    return {
        "ok": True,
        "kb_id": kb_id,
        "scan_status": last_status,
        "scan_count": len(scans_payload),
        "scans": scans_payload,
        "lineage": _lineage_payload(kb, scan_id=scans_payload[-1]["scan_id"] if scans_payload else ""),
    }


@router.get(
    "/{kb_id}/scan-status",
    summary="Get scan status for a KB",
)
async def get_knowledge_base_scan_status(
    kb_id: str,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)

    scan_service = _document_scan_service(request)
    scans = await scan_service.list_scans(kb_id=kb_id)
    scans_payload: List[Dict[str, Any]] = []
    for scan in scans:
        if scan.document.kb_id != kb_id:
            continue
        payload = scan.model_dump()
        payload["lineage"] = _lineage_payload(kb, scan_id=scan.scan_id)
        scans_payload.append(payload)

    last_scan_id = scans_payload[-1]["scan_id"] if scans_payload else ""
    status = scans_payload[-1].get("status", "not_found") if scans_payload else "not_found"
    return {
        "ok": True,
        "kb_id": kb_id,
        "scan_status": status,
        "last_scan_id": last_scan_id,
        "scans": scans_payload,
        "lineage": _lineage_payload(kb, scan_id=last_scan_id),
    }


@router.post(
    "/{kb_id}/promote",
    summary="Promote a KB scan into ATOM",
)
async def promote_knowledge_base_scan(
    kb_id: str,
    body: PromoteKnowledgeBaseScanRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)
    _assert_can_mutate(user_id, kb)

    scan_service = _document_scan_service(request)
    try:
        promoted = await scan_service.promote_kb_scan(
            kb_id=kb_id,
            scan_id=body.scan_id,
            minimum_evidentiality_score=body.minimum_evidentiality_score,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Scan not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if getattr(promoted, "record", None) is not None:
        payload = promoted.record.model_dump()
        payload["passed"] = promoted.passed
        payload["blocked_reason"] = promoted.blocked_reason.to_dict() if promoted.blocked_reason else None
        payload["reason_code"] = payload.get("reason_code") or promoted.reason_code.value
        lineage_scan_id = promoted.record.scan_id
        lineage_promotion_id = promoted.record.promotion_id
    else:
        payload = promoted.to_dict() if hasattr(promoted, "to_dict") else promoted.model_dump()
        lineage_scan_id = body.scan_id
        lineage_promotion_id = payload.get("promotion_id") or ""
    payload["ok"] = True
    payload["lineage"] = _lineage_payload(
        kb,
        scan_id=lineage_scan_id,
        promotion_id=lineage_promotion_id,
    )
    return payload


@router.get(
    "/{kb_id}/atoms",
    summary="List promoted KB atoms",
)
async def list_knowledge_base_atoms(
    kb_id: str,
    request: Request,
    user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    kb = await _load_authorized_kb(request, kb_id=kb_id, user_id=user_id)

    scan_service = _document_scan_service(request)
    atoms = await scan_service.list_kb_atoms(kb_id)
    atom_payload: List[Dict[str, Any]] = []
    for atom in atoms:
        payload = atom.model_dump()
        payload["lineage"] = _lineage_payload(
            kb,
            scan_id=atom.scan_id,
            promotion_id=atom.promotion_id,
        )
        atom_payload.append(payload)

    return {
        "ok": True,
        "kb_id": kb_id,
        "atoms": atom_payload,
        "lineage": _lineage_payload(kb),
    }
