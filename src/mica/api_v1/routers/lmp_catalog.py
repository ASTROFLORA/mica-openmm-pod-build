from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.pipelines.lmp_publication.promoter import LMPCorpusPromoter
from mica.storage.lmp_corpus_storage import LMPCorpusResolver

router = APIRouter(prefix="/api/v1/lmp_catalog", tags=["lmp-catalog"])


class WorkspacePromotionRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    asset_id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    preset: str = Field(default="full", min_length=1)
    reviewer: str = Field(default="system", min_length=1)
    dry_run: bool = True
    operator_approved: bool = False


class UserBucketPromotionRequest(BaseModel):
    object_path: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    preset: str = Field(default="full", min_length=1)
    reviewer: str = Field(default="system", min_length=1)
    dry_run: bool = True
    operator_approved: bool = False


def get_lmp_corpus_resolver() -> LMPCorpusResolver:
    resolver = LMPCorpusResolver.from_env_with_public_fallback()
    if resolver is None:
        raise HTTPException(status_code=503, detail="LMP shared corpus catalog is not configured")
    return resolver


def get_writable_lmp_corpus_resolver(
    resolver: LMPCorpusResolver = Depends(get_lmp_corpus_resolver),
) -> LMPCorpusResolver:
    """Dependency for endpoints that mutate the catalog.

    Refuses when the resolver is in public-fallback mode (read-only), so
    promotion endpoints degrade cleanly with a 409 instead of corrupting a
    read-only bucket.
    """
    if getattr(resolver, "is_public_fallback", False):
        raise HTTPException(
            status_code=409,
            detail=(
                "LMP promotions require a writable catalog "
                "(set GCS_LMP_CATALOG_MANIFEST + GCS_LMP_SHARED_BUCKET). "
                "Public read-only fallback is active."
            ),
        )
    return resolver


def get_lmp_corpus_promoter(
    resolver: LMPCorpusResolver = Depends(get_writable_lmp_corpus_resolver),
) -> LMPCorpusPromoter:
    return LMPCorpusPromoter(resolver=resolver)


@router.get("/versions")
async def list_lmp_catalog_versions(
    resolver: LMPCorpusResolver = Depends(get_lmp_corpus_resolver),
) -> Dict[str, Any]:
    return {
        "ok": True,
        "default_version": resolver.choose_version(None),
        "versions": resolver.list_versions(),
        "is_public_fallback": getattr(resolver, "is_public_fallback", False),
        "writable": not getattr(resolver, "is_public_fallback", False),
    }


@router.get("/{version}/entries")
async def list_lmp_catalog_entries(
    version: str,
    preset: Optional[str] = Query(default=None),
    resolver: LMPCorpusResolver = Depends(get_lmp_corpus_resolver),
) -> Dict[str, Any]:
    chosen_preset = resolver.choose_preset(preset)
    entries = [resolver.preview_payload(entry) for entry in resolver.list_entries(version=version, preset=chosen_preset)]
    return {"ok": True, "version": version, "preset": chosen_preset, "entries": entries}


@router.get("/{version}/{preset}/{entry_id}/preview")
async def get_lmp_catalog_preview(
    version: str,
    preset: str,
    entry_id: str,
    resolver: LMPCorpusResolver = Depends(get_lmp_corpus_resolver),
) -> Dict[str, Any]:
    entry = resolver.get_entry(version=version, preset=preset, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="LMP catalog entry not found")
    return {"ok": True, **resolver.preview_payload(entry)}


@router.get("/{version}/{preset}/{entry_id}/render")
async def get_lmp_catalog_render(
    version: str,
    preset: str,
    entry_id: str,
    resolver: LMPCorpusResolver = Depends(get_lmp_corpus_resolver),
) -> Dict[str, Any]:
    entry = resolver.get_entry(version=version, preset=preset, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="LMP catalog entry not found")
    return {
        "ok": True,
        "entry_id": entry.entry_id,
        "version": entry.version,
        "preset": entry.preset,
        "render": dict(entry.render),
        "preview": dict(entry.preview),
    }


@router.post("/promotions/workspace")
async def promote_workspace_lmp_asset(
    payload: WorkspacePromotionRequest,
    user_id: str = Depends(user_dependency),
    promoter: LMPCorpusPromoter = Depends(get_lmp_corpus_promoter),
) -> Dict[str, Any]:
    try:
        return promoter.promote_workspace_asset(
            user_id=user_id,
            session_id=payload.session_id,
            asset_id=payload.asset_id,
            version=payload.version,
            preset=payload.preset,
            reviewer=payload.reviewer,
            dry_run=payload.dry_run,
            operator_approved=payload.operator_approved,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/promotions/user-bucket")
async def promote_user_bucket_lmp_object(
    payload: UserBucketPromotionRequest,
    user_id: str = Depends(user_dependency),
    promoter: LMPCorpusPromoter = Depends(get_lmp_corpus_promoter),
) -> Dict[str, Any]:
    try:
        return promoter.promote_user_bucket_object(
            user_id=user_id,
            object_path=payload.object_path,
            version=payload.version,
            preset=payload.preset,
            reviewer=payload.reviewer,
            dry_run=payload.dry_run,
            operator_approved=payload.operator_approved,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc