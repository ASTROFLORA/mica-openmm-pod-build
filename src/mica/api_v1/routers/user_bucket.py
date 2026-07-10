"""User-bucket native API router.

Exposes the user's GCS bucket as a first-class data plane — independent of
the workspace abstraction.  The workspace can consume objects from this plane,
but the driver can also browse, inspect, and read objects directly.

Security model:
- Every endpoint is scoped to the authenticated ``user_id`` (via ``user_dependency``).
- Object paths are validated/sanitized through ``_validate_prefix`` / ``_validate_object_name``
  from ``gcs_user_storage`` before reaching GCS.
- No cross-user access is possible — the bucket name is deterministic on ``user_id``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import request_identity_dependency
from mica.identity.request_identity import RequestIdentity
from mica.storage.gcs_user_storage import get_storage_manager, normalize_object_path, sanitize_object_prefix, storage_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/user-bucket", tags=["user-bucket"])
logger = logging.getLogger(__name__)

_CRITICAL_STATUS_REASON_CODES = {
    "missing_project",
    "credentials_unavailable",
    "init_failed",
}
WORKSPACE_BINDING_RECEIPT_SCHEMA = "mica.gcs_user_workspace_binding_receipt.v1"
ARTIFACT_PROMOTION_RECEIPT_SCHEMA = "mica.gcs_user_artifact_promotion_receipt.v1"
WORKSPACE_PROTOCOL_EXECUTOR_SURFACES = frozenset(
    {"mica_user_workspace", "gcs_user_workspace", "user_bucket", "workspace_storage"}
)
DEMO_WORKSPACE_ENTITLEMENT_CODE = "demo-full-workspace-access"
WORKSPACE_BINDING_MARKER_PREFIX = "_mica/workspace_bindings"


def _authenticated_user_id(user: Any) -> str:
    if isinstance(user, RequestIdentity):
        return user.user_id
    if isinstance(user, str):
        return user
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or user.get("id") or "anonymous")
    return str(user or "anonymous")


def protocol_node_uses_workspace_surface(node: Any) -> bool:
    executor_surface = str(getattr(node, "executor_surface", "") or "").strip().lower()
    if executor_surface in WORKSPACE_PROTOCOL_EXECUTOR_SURFACES:
        return True
    inputs = getattr(node, "inputs", None)
    if isinstance(inputs, dict):
        tool_name = str(inputs.get("tool_name") or "").strip().lower()
        return tool_name in {"bind_workspace_identity", "promote_artifact"}
    return False


def _protocol_str(inputs: Dict[str, Any], key: str, default: str = "") -> str:
    return str(inputs.get(key, default) or default).strip()


def _validate_entitlement_code(candidate: str) -> str:
    value = str(candidate or "").strip()
    if value != DEMO_WORKSPACE_ENTITLEMENT_CODE:
        raise ValueError("workspace promotion requires a valid demo entitlement code")
    return value


def _resolve_workspace_subject(
    *,
    request_metadata: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, str]:
    request_identity = request_metadata.get("request_identity")
    request_identity_payload = request_identity if isinstance(request_identity, dict) else {}
    custom_claims = request_identity_payload.get("custom_claims")
    custom_claims_payload = custom_claims if isinstance(custom_claims, dict) else {}

    clerk_user_id = (
        _protocol_str(inputs, "clerk_user_id")
        or str(request_metadata.get("clerk_user_id") or "").strip()
        or str(request_identity_payload.get("user_id") or "").strip()
    )
    clerk_email = (
        _protocol_str(inputs, "clerk_email")
        or str(request_metadata.get("clerk_email") or "").strip()
        or str(custom_claims_payload.get("email") or "").strip()
    )
    entitlement_code = _validate_entitlement_code(
        _protocol_str(inputs, "entitlement_code")
        or str(request_metadata.get("entitlement_code") or "").strip()
    )
    if not clerk_user_id or not clerk_email:
        raise ValueError("workspace promotion requires Clerk-bound clerk_user_id and clerk_email")
    return {
        "clerk_user_id": clerk_user_id,
        "clerk_email": clerk_email,
        "entitlement_code": entitlement_code,
    }


def _workspace_prefix(inputs: Dict[str, Any], protocol_id: str) -> str:
    prefix = _protocol_str(inputs, "workspace_prefix", f"protocol/{protocol_id}")
    return sanitize_object_prefix(prefix)


def _normalize_workspace_object_path(inputs: Dict[str, Any], protocol_id: str, source_path: Path) -> str:
    object_path = _protocol_str(inputs, "object_path")
    if object_path:
        cleaned = object_path.strip().strip("/")
        if "/" in cleaned:
            prefix, object_name = cleaned.rsplit("/", 1)
            return normalize_object_path(prefix, object_name)
        return normalize_object_path("", cleaned)
    prefix = _workspace_prefix(inputs, protocol_id)
    return normalize_object_path(prefix, source_path.name)


def _content_type_for_path(inputs: Dict[str, Any], source_path: Path) -> str:
    explicit = _protocol_str(inputs, "content_type")
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(str(source_path))
    return guessed or "application/octet-stream"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding_marker_object_path(*, clerk_user_id: str, workspace_prefix: str) -> str:
    digest = hashlib.sha256(f"{clerk_user_id}:{workspace_prefix}".encode("utf-8")).hexdigest()
    return normalize_object_path(WORKSPACE_BINDING_MARKER_PREFIX, f"{digest}.json")


def _load_existing_binding_receipt(
    *,
    storage: Any,
    user_id: str,
    clerk_user_id: str,
    workspace_prefix: str,
) -> Dict[str, Any] | None:
    read_text_best_effort = getattr(storage, "read_text_best_effort", None)
    if not callable(read_text_best_effort):
        return None

    marker_path = _binding_marker_object_path(
        clerk_user_id=clerk_user_id,
        workspace_prefix=workspace_prefix,
    )
    try:
        payload = read_text_best_effort(
            user_id=user_id,
            object_path=marker_path,
            max_chars=32_000,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise

    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return None
    try:
        receipt = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Workspace binding marker is not valid JSON: %s", marker_path)
        return None
    if not isinstance(receipt, dict):
        return None
    if str(receipt.get("schema_id") or "") != WORKSPACE_BINDING_RECEIPT_SCHEMA:
        logger.warning("Workspace binding marker schema drift at %s", marker_path)
        return None
    return receipt


def _persist_binding_receipt_marker(
    *,
    storage: Any,
    user_id: str,
    clerk_user_id: str,
    workspace_prefix: str,
    binding_receipt: Dict[str, Any],
) -> None:
    upload_text = getattr(storage, "upload_text", None)
    if not callable(upload_text):
        return

    marker_path = _binding_marker_object_path(
        clerk_user_id=clerk_user_id,
        workspace_prefix=workspace_prefix,
    )
    upload_text(
        user_id=user_id,
        object_path=marker_path,
        text=json.dumps(binding_receipt, ensure_ascii=True, sort_keys=True),
        content_type="application/json",
        metadata={
            "schema_id": WORKSPACE_BINDING_RECEIPT_SCHEMA,
            "clerk_user_id": clerk_user_id,
            "workspace_prefix": workspace_prefix,
        },
    )


def _promotion_receipt_subject_fields(metadata_payload: Dict[str, str]) -> Dict[str, str]:
    subject_fields: Dict[str, str] = {}
    clerk_user_id = str(metadata_payload.get("clerk_user_id") or "").strip()
    clerk_email = str(metadata_payload.get("clerk_email") or "").strip()
    if clerk_user_id:
        subject_fields["clerk_user_id"] = clerk_user_id
    if clerk_email:
        subject_fields["clerk_email"] = clerk_email
    return subject_fields


def _durability_class_from_download_url_present(download_url_present: bool) -> str:
    return "workspace_durable_with_signed_url" if download_url_present else "workspace_durable"


def promote_workspace_artifact_payload(
    *,
    storage: Any,
    user_id: str,
    bucket_info: Any,
    source_path: Path,
    protocol_id: str,
    node_id: str,
    session_id: str,
    source_kind: str,
    workspace_prefix: str,
    metadata_payload: Dict[str, str],
    source_session_id: Optional[str] = None,
    source_node_id: Optional[str] = None,
    object_path_hint: str = "",
    binding_surface: str = "mica_user_workspace",
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    source_session_value = str(source_session_id or session_id).strip() or session_id
    source_node_value = str(source_node_id or node_id).strip() or node_id
    subject_fields = _promotion_receipt_subject_fields(metadata_payload)

    if source_path.is_dir():
        root_object_prefix = str(object_path_hint or "").strip()
        if root_object_prefix:
            object_prefix = sanitize_object_prefix(root_object_prefix)
        else:
            object_prefix = sanitize_object_prefix(f"{workspace_prefix}/{source_path.name}")

        uploaded_objects: List[Dict[str, Any]] = []
        artifact_refs: List[str] = []
        evidence_refs: List[str] = [f"protocol://{protocol_id}/nodes/{node_id}/artifact_promotion"]
        total_size_bytes = 0
        digest_lines: List[str] = []

        for file_path in sorted(path for path in source_path.rglob("*") if path.is_file()):
            relative_path = file_path.relative_to(source_path).as_posix()
            relative_name = relative_path.split("/")[-1]
            relative_prefix = "/".join(relative_path.split("/")[:-1])
            destination_prefix = "/".join(part for part in (object_prefix, relative_prefix) if part)
            destination_object_path = normalize_object_path(destination_prefix, relative_name)
            content_type = _content_type_for_path({}, file_path)
            file_size = int(file_path.stat().st_size)
            file_sha256 = _sha256_file(file_path)
            total_size_bytes += file_size
            digest_lines.append(f"{relative_path}:{file_sha256}:{file_size}")
            object_uri = storage.upload_file(
                user_id=user_id,
                object_path=destination_object_path,
                local_path=file_path,
                content_type=content_type,
                metadata={**metadata_payload, "bundle_relative_path": relative_path},
            )
            info = storage.get_object_info(user_id=user_id, object_path=destination_object_path)
            uploaded_objects.append(
                {
                    "relative_path": relative_path,
                    "object_path": destination_object_path,
                    "object_uri": object_uri,
                    "sha256": file_sha256,
                    "size_bytes": file_size,
                    "content_type": content_type,
                    "evidence_object": dict(info.get("evidence_object") or {}),
                }
            )
            artifact_refs.extend(
                [
                    object_uri,
                    str(info.get("evidence_object", {}).get("storage_uri") or "").strip(),
                ]
            )
            evidence_refs.append(str(info.get("evidence_object", {}).get("evidence_id") or "").strip())

        bundle_sha256 = hashlib.sha256("\n".join(digest_lines).encode("utf-8")).hexdigest()
        durability_class = _durability_class_from_download_url_present(
            any(str((entry.get("evidence_object") or {}).get("download_url") or "").strip() for entry in uploaded_objects)
        )
        promotion_receipt = {
            "schema_id": ARTIFACT_PROMOTION_RECEIPT_SCHEMA,
            "protocol_id": protocol_id,
            "node_id": node_id,
            "source_session_id": source_session_value,
            "source_kind": source_kind,
            "source_node_id": source_node_value,
            **subject_fields,
            "source_is_directory": True,
            "root_object_prefix": object_prefix,
            "object_uris": [entry["object_uri"] for entry in uploaded_objects],
            "bundle_entries": uploaded_objects,
            "sha256": bundle_sha256,
            "size_bytes": total_size_bytes,
            "entry_count": len(uploaded_objects),
            "content_type": "application/vnd.mica.directory-manifest+json",
            "produced_at": now.isoformat(),
            "durability_class": durability_class,
        }
        return {
            "tool_name": "promote_artifact",
            "binding_surface": binding_surface,
            "summary": (
                f"Promoted artifact bundle {source_path.name} into workspace custody "
                f"for protocol node {node_id}."
            ),
            "state_after": {
                "dispatch_kind": "workspace_artifact_promotion",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "receipt_family": ARTIFACT_PROMOTION_RECEIPT_SCHEMA,
                "promotion_receipt": promotion_receipt,
                "bucket_name": bucket_info.bucket_name,
                "workspace_prefix": workspace_prefix,
                "object_path": object_prefix,
            },
            "artifact_refs": [ref for ref in artifact_refs if ref],
            "evidence_refs": [ref for ref in evidence_refs if ref],
            "cost_snapshot": {"usd": 0.0, "tool_calls": len(uploaded_objects), "binding_surface": binding_surface},
        }

    if not source_path.is_file():
        raise ValueError(f"workspace promotion source must be a file or directory: {source_path}")

    object_path = _normalize_workspace_object_path({"object_path": object_path_hint}, protocol_id, source_path)
    content_type = _content_type_for_path({}, source_path)
    size_bytes = int(source_path.stat().st_size)
    sha256 = _sha256_file(source_path)
    object_uri = storage.upload_file(
        user_id=user_id,
        object_path=object_path,
        local_path=source_path,
        content_type=content_type,
        metadata=metadata_payload,
    )
    info = storage.get_object_info(user_id=user_id, object_path=object_path)
    durability_class = _durability_class_from_download_url_present(
        bool(info.get("evidence_object", {}).get("download_url"))
    )
    promotion_receipt = {
        "schema_id": ARTIFACT_PROMOTION_RECEIPT_SCHEMA,
        "protocol_id": protocol_id,
        "node_id": node_id,
        "source_session_id": source_session_value,
        "source_kind": source_kind,
        "source_node_id": source_node_value,
        **subject_fields,
        "object_uri": object_uri,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "produced_at": now.isoformat(),
        "durability_class": durability_class,
    }
    return {
        "tool_name": "promote_artifact",
        "binding_surface": binding_surface,
        "summary": f"Promoted artifact {source_path.name} into workspace custody for protocol node {node_id}.",
        "state_after": {
            "dispatch_kind": "workspace_artifact_promotion",
            "protocol_id": protocol_id,
            "session_id": session_id,
            "receipt_family": ARTIFACT_PROMOTION_RECEIPT_SCHEMA,
            "promotion_receipt": promotion_receipt,
            "bucket_name": bucket_info.bucket_name,
            "workspace_prefix": workspace_prefix,
            "object_path": object_path,
        },
        "artifact_refs": [
            object_uri,
            str(info.get("evidence_object", {}).get("storage_uri") or "").strip(),
        ],
        "evidence_refs": [
            f"protocol://{protocol_id}/nodes/{node_id}/artifact_promotion",
            str(info.get("evidence_object", {}).get("evidence_id") or "").strip(),
        ],
        "cost_snapshot": {"usd": 0.0, "tool_calls": 1, "binding_surface": binding_surface},
    }


async def execute_protocol_workspace_action(
    *,
    node: Any,
    protocol_id: str,
    node_id: str,
    session_id: str,
    user_id: str,
    request_metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    metadata = dict(request_metadata or {})
    inputs = dict(getattr(node, "inputs", {}) or {})
    tool_name = _protocol_str(inputs, "tool_name").lower()
    if tool_name not in {"bind_workspace_identity", "promote_artifact"}:
        raise ValueError(f"Unsupported workspace protocol tool '{tool_name or 'missing'}'")

    subject = _resolve_workspace_subject(request_metadata=metadata, inputs=inputs)
    storage = get_storage_manager()
    bucket_info = storage.ensure_bucket(user_id)
    workspace_prefix = _workspace_prefix(inputs, protocol_id)
    now = datetime.now(timezone.utc)

    if tool_name == "bind_workspace_identity":
        binding_receipt = _load_existing_binding_receipt(
            storage=storage,
            user_id=user_id,
            clerk_user_id=subject["clerk_user_id"],
            workspace_prefix=workspace_prefix,
        )
        binding_status = "existing"
        if binding_receipt is None:
            expires_at = now + timedelta(hours=24)
            binding_receipt = {
                "schema_id": WORKSPACE_BINDING_RECEIPT_SCHEMA,
                "protocol_id": protocol_id,
                "node_id": node_id,
                "source_session_id": session_id,
                "clerk_user_id": subject["clerk_user_id"],
                "clerk_email": subject["clerk_email"],
                "entitlement_code": subject["entitlement_code"],
                "bucket_name": bucket_info.bucket_name,
                "workspace_prefix": workspace_prefix,
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "decision_reason": "demo_entitlement_workspace_binding",
            }
            _persist_binding_receipt_marker(
                storage=storage,
                user_id=user_id,
                clerk_user_id=subject["clerk_user_id"],
                workspace_prefix=workspace_prefix,
                binding_receipt=binding_receipt,
            )
            binding_status = "created"
        return {
            "tool_name": tool_name,
            "binding_surface": "mica_user_workspace",
            "summary": (
                f"{('Bound' if binding_status == 'created' else 'Reused')} Clerk workspace identity "
                f"for protocol node {node_id}."
            ),
            "state_after": {
                "dispatch_kind": "workspace_identity_binding",
                "protocol_id": protocol_id,
                "session_id": session_id,
                "binding_status": binding_status,
                "receipt_family": WORKSPACE_BINDING_RECEIPT_SCHEMA,
                "binding_receipt": binding_receipt,
                "bucket_name": bucket_info.bucket_name,
                "workspace_prefix": workspace_prefix,
            },
            "artifact_refs": [f"gs://{bucket_info.bucket_name}/{workspace_prefix}"],
            "evidence_refs": [f"protocol://{protocol_id}/nodes/{node_id}/workspace_binding"],
            "cost_snapshot": {"usd": 0.0, "tool_calls": 1, "binding_surface": "mica_user_workspace"},
        }

    source_path = Path(_protocol_str(inputs, "source_path")).expanduser()
    if not source_path.exists():
        raise ValueError(f"workspace promotion source file not found: {source_path}")
    source_kind = _protocol_str(inputs, "source_kind", "smic")
    if source_kind not in {"sandbox", "smic", "archivist_staged", "serverless_generated"}:
        raise ValueError(f"Unsupported workspace promotion source_kind '{source_kind}'")

    metadata_payload = {
        "protocol_id": protocol_id,
        "node_id": node_id,
        "session_id": session_id,
        "source_kind": source_kind,
        "clerk_user_id": subject["clerk_user_id"],
        "clerk_email": subject["clerk_email"],
    }

    return promote_workspace_artifact_payload(
        storage=storage,
        user_id=user_id,
        bucket_info=bucket_info,
        source_path=source_path,
        protocol_id=protocol_id,
        node_id=node_id,
        session_id=session_id,
        source_kind=source_kind,
        workspace_prefix=workspace_prefix,
        metadata_payload=metadata_payload,
        source_session_id=_protocol_str(inputs, "source_session_id", session_id),
        source_node_id=_protocol_str(inputs, "source_node_id", node_id),
        object_path_hint=_protocol_str(inputs, "object_path"),
    )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CopyToWorkspaceRequest(BaseModel):
    object_path: str = Field(..., min_length=1, max_length=1024)
    workspace_session_id: str = Field(..., min_length=1, max_length=128)
    asset_type: str = Field(default="other", max_length=32)
    name: Optional[str] = Field(None, max_length=255)


class CopyObjectRequest(BaseModel):
    source_path: str = Field(..., min_length=1, max_length=1024)
    dest_path: str = Field(..., min_length=1, max_length=1024)


class UploadTextRequest(BaseModel):
    object_path: str = Field(..., min_length=1, max_length=1024)
    content: str = Field(..., min_length=1, max_length=10_000_000)
    content_type: Optional[str] = Field(None, max_length=128)


class SearchContentRequest(BaseModel):
    terms: List[str] = Field(..., min_length=1, max_length=100)
    prefix: str = Field(default="", max_length=512)
    max_results: int = Field(default=50, ge=1, le=500)
    max_chars_per_object: int = Field(default=50_000, ge=100, le=500_000)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/objects")
def list_objects(
    prefix: str = Query(default="", max_length=512),
    max_results: int = Query(default=200, ge=1, le=5000),
    include_metadata: bool = Query(default=False),
    user: Any = Depends(request_identity_dependency),
):
    """List objects in the user's GCS bucket under a given prefix."""
    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    objects = storage.list_objects(
        user_id=user_id,
        prefix=prefix,
        max_results=max_results,
        include_metadata=include_metadata,
    )
    return {
        "ok": True,
        "scope": "user_bucket",
        "prefix": prefix,
        "count": len(objects),
        "objects": objects,
    }


@router.get("/objects/info")
def get_object_info(
    path: str = Query(..., min_length=1, max_length=1024),
    user: Any = Depends(request_identity_dependency),
):
    """Get metadata for a single object in the user bucket."""
    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    info = storage.get_object_info(user_id=user_id, object_path=path)
    return {"ok": True, **info}


@router.get("/objects/read")
def read_object_text(
    path: str = Query(..., min_length=1, max_length=1024),
    max_chars: int = Query(default=80_000, ge=100, le=500_000),
    user: Any = Depends(request_identity_dependency),
):
    """Read an object as text (best-effort extraction for PDF/XML/JSON/PDB/etc)."""
    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    result = storage.read_text_best_effort(
        user_id=user_id,
        object_path=path,
        max_chars=max_chars,
    )
    result["ok"] = True
    result["path"] = path
    try:
        result["evidence_object"] = storage.get_object_info(user_id=user_id, object_path=path).get("evidence_object")
    except Exception:
        pass
    return result


@router.post("/copy")
def copy_object(
    payload: CopyObjectRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Copy an object within the user's bucket."""
    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    info = storage.copy_object(
        user_id=user_id,
        source_path=payload.source_path,
        dest_path=payload.dest_path,
    )
    return {"ok": True, "copied": info}


@router.post("/copy-to-workspace")
def copy_to_workspace(
    payload: CopyToWorkspaceRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Copy a bucket object into a workspace session as an asset.

    Reads object bytes from the user bucket and creates a workspace asset.
    This is the canonical bucket -> workspace bridge operation.
    """
    from mica.api_v1.routers.workspace import _get_backend, _sanitize_filename

    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()

    # Read source bytes
    data = storage.read_bytes(user_id=user_id, object_path=payload.object_path)

    # Determine asset name
    name = payload.name or payload.object_path.rsplit("/", 1)[-1]
    safe_name = _sanitize_filename(name)

    # Add to workspace
    backend = _get_backend()
    asset = backend.add_asset(
        user_id=user_id,
        session_id=payload.workspace_session_id,
        asset_type=payload.asset_type,
        name=safe_name,
        data=data,
    )
    asset["source_evidence_object"] = storage.get_object_info(user_id=user_id, object_path=payload.object_path).get("evidence_object")
    return {"ok": True, "asset": asset}


@router.post("/upload")
def upload_text(
    payload: UploadTextRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Upload text content to the user's GCS bucket.

    Server-side upload for internal modules (LMP generation, DLM pipeline,
    convergence). Content is stored as UTF-8 text.
    """
    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    storage.upload_text(
        user_id=user_id,
        object_path=payload.object_path,
        text=payload.content,
        content_type=payload.content_type,
    )
    evidence_object = storage.get_object_info(user_id=user_id, object_path=payload.object_path).get("evidence_object")
    return {"ok": True, "object_path": payload.object_path, "evidence_object": evidence_object}


@router.post("/search-content")
def search_content(
    payload: SearchContentRequest,
    user: Any = Depends(request_identity_dependency),
):
    """Search text content of bucket objects using Aho-Corasick multi-pattern matching.

    Scans objects under *prefix* for occurrences of all *terms* simultaneously.
    Returns matching objects with hit counts per term.
    """
    from mica.storage.bucket_search import AhoCorasickBucketScanner

    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    try:
        scanner = AhoCorasickBucketScanner(storage)
        results = scanner.scan(
            user_id=user_id,
            terms=payload.terms,
            prefix=payload.prefix,
            max_results=payload.max_results,
            max_chars_per_object=payload.max_chars_per_object,
        )
    except Exception as exc:
        logger.exception("Bucket content search failed for user %s", user_id)
        raise HTTPException(status_code=502, detail=f"Bucket content search failed: {exc}") from exc
    return {"ok": True, "results": results, "terms": payload.terms}


@router.delete("/objects")
def delete_object(
    path: str = Query(..., min_length=1, max_length=1024),
    user: Any = Depends(request_identity_dependency),
):
    """Delete an object from the user's GCS bucket."""
    user_id = _authenticated_user_id(user)
    storage = get_storage_manager()
    storage.delete_object(user_id=user_id, object_path=path)
    return {"ok": True, "deleted": path}


@router.get("/status")
def bucket_status(user: Any = Depends(request_identity_dependency)):
    """Return bucket info and basic stats for the authenticated user."""
    user_id = _authenticated_user_id(user)
    status = storage_status()
    reason_code = str(status.get("reason_code") or "unknown")
    if reason_code in _CRITICAL_STATUS_REASON_CODES:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "critical_storage_setup_failure",
                "reason_code": reason_code,
                "storage_status": status,
            },
        )

    if not status.get("configured") or not status.get("ready"):
        return {
            "ok": True,
            "scope": "user_bucket",
            "bucket_ready": False,
            "storage_status": status,
            "bucket_name": None,
            "project": status.get("project"),
            "top_prefixes": [],
            "root_objects": 0,
        }

    storage = get_storage_manager()
    bucket_info = storage.ensure_bucket(user_id)
    # Quick count of top-level prefixes
    bucket = storage.client.bucket(bucket_info.bucket_name)
    blobs = bucket.list_blobs(delimiter="/", max_results=1)
    # Consume to populate prefixes
    total = 0
    for _ in blobs:
        total += 1
    top_prefixes = list(blobs.prefixes)
    return {
        "ok": True,
        "bucket_ready": True,
        "scope": "user_bucket",
        "storage_status": status,
        "bucket_name": bucket_info.bucket_name,
        "project": bucket_info.project,
        "top_prefixes": top_prefixes,
        "root_objects": total,
    }
