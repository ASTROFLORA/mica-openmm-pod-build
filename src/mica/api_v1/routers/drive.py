"""Biological Drive / File Records router — P0 product-layer API.

Endpoints:
  POST   /api/v1/drive/files/upload-url   Get signed upload URL
  POST   /api/v1/drive/files              Register uploaded file
  GET    /api/v1/drive/files              List files
  GET    /api/v1/drive/files/{id}         Get file + signed download URL
  DELETE /api/v1/drive/files/{id}         Soft-delete
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import choose_neon_database_url, asyncpg_connection_kwargs_for_database_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/drive/files", tags=["drive"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class UploadUrlRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=500)
    content_type: str = Field("application/octet-stream")
    size_bytes: Optional[int] = Field(None, description="Expected file size")
    file_type: str = Field("other", description="pdb, dcd, pdf, fasta, xml, cif, image, other")

class UploadUrlResponse(BaseModel):
    upload_url: str
    object_path: str
    bucket: str = ""
    expires_in: int = 3600
    content_type: str

class RegisterFileRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=500)
    display_name: Optional[str] = None
    gcs_key: str = Field(..., description="GCS object path from upload")
    file_type: str = Field("other")
    content_hash: str = Field(..., description="SHA-256 hex digest")
    mime_type: Optional[str] = None
    size_bytes: int = 0
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class FileRecordResponse(BaseModel):
    file_id: str
    user_id: str
    filename: str
    display_name: Optional[str] = None
    file_type: str
    gcs_key: str
    content_hash: str
    mime_type: Optional[str] = None
    size_bytes: int
    sync_status: str
    tags: List[str]
    metadata: Dict[str, Any]
    created_at: str
    updated_at: str
    download_url: Optional[str] = None

class FileListResponse(BaseModel):
    files: List[FileRecordResponse]
    total: int

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_POOL = None

async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    import asyncpg
    dsn = choose_neon_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="Database not configured")
    _POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn))
    return _POOL

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(
    body: UploadUrlRequest,
    user_id: str = Depends(user_dependency),
):
    """Get a signed upload URL for GCS."""
    try:
        from mica.storage.gcs_user_storage import GCSUserStorage, get_storage_manager
        storage = get_storage_manager()
        bucket = storage.ensure_bucket(user_id)
        object_name = f"biological-drive/{user_id}/{body.file_type}/{uuid.uuid4().hex[:12]}-{body.filename}"
        url = storage.signed_upload_url(
            user_id=user_id,
            object_name=object_name,
            content_type=body.content_type,
            expires_in=3600,
        )
        return UploadUrlResponse(
            upload_url=url,
            object_path=object_name,
            bucket=bucket.bucket_name,
            expires_in=3600,
            content_type=body.content_type,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Storage unavailable: {e}")


@router.post("", response_model=FileRecordResponse, status_code=201)
async def register_file(
    body: RegisterFileRequest,
    user_id: str = Depends(user_dependency),
):
    """Register a file record after upload completes."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO file_records (user_id, filename, display_name, file_type, gcs_key,
                       content_hash, mime_type, size_bytes, tags, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               ON CONFLICT (user_id, gcs_key) DO UPDATE
               SET filename = $2, display_name = $3, content_hash = $6,
                   mime_type = $7, size_bytes = $8, tags = $9, metadata = $10,
                   updated_at = now()
               RETURNING file_id, user_id, filename, display_name, file_type, gcs_key,
                         content_hash, mime_type, size_bytes, sync_status, tags, metadata,
                         created_at, updated_at""",
            user_id, body.filename, body.display_name or body.filename, body.file_type,
            body.gcs_key, body.content_hash, body.mime_type, body.size_bytes,
            body.tags, body.metadata,
        )
    return _row_to_file(row, include_download=False)


@router.get("", response_model=FileListResponse)
async def list_files(
    user_id: str = Depends(user_dependency),
    file_type: Optional[str] = Query(None),
    sync_status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List user's file records."""
    await ensure_product_schema()
    pool = await _get_pool()
    conditions = ["user_id = $1"]
    params: list = [user_id]
    idx = 2
    if file_type:
        conditions.append(f"file_type = ${idx}"); params.append(file_type); idx += 1
    if sync_status:
        conditions.append(f"sync_status = ${idx}"); params.append(sync_status); idx += 1
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM file_records WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, limit, offset,
        )
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM file_records WHERE {where}", *params,
        )
    return FileListResponse(
        files=[_row_to_file(r) for r in rows],
        total=total_row["count"] if total_row else 0,
    )


@router.get("/{file_id}", response_model=FileRecordResponse)
async def get_file(
    file_id: str,
    user_id: str = Depends(user_dependency),
    include_download: bool = Query(True),
):
    """Get file record, optionally with a signed download URL."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM file_records WHERE file_id = $1 AND user_id = $2",
            file_id, user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return _row_to_file(row, include_download=include_download)


@router.delete("/{file_id}", status_code=204)
async def soft_delete_file(
    file_id: str,
    user_id: str = Depends(user_dependency),
):
    """Soft-delete (set sync_status to 'deleted')."""
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE file_records SET sync_status = 'deleted', updated_at = now() WHERE file_id = $1 AND user_id = $2",
            file_id, user_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="File not found")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_file(row, include_download: bool = False) -> FileRecordResponse:
    download_url = None
    if include_download:
        try:
            from mica.storage.gcs_user_storage import get_storage_manager
            storage = get_storage_manager()
            download_url = storage.signed_download_url(
                user_id=row["user_id"],
                object_name=row["gcs_key"],
            )
        except Exception:
            pass

    return FileRecordResponse(
        file_id=str(row["file_id"]),
        user_id=row["user_id"],
        filename=row["filename"],
        display_name=row.get("display_name"),
        file_type=row["file_type"],
        gcs_key=row["gcs_key"],
        content_hash=row["content_hash"],
        mime_type=row.get("mime_type"),
        size_bytes=row["size_bytes"],
        sync_status=row["sync_status"],
        tags=row.get("tags") or [],
        metadata=row.get("metadata") or {},
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        download_url=download_url,
    )
