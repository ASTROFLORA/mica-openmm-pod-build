from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException

from mica.infrastructure.literature.control_plane import (
    build_literature_identity_keys,
    build_preferred_literature_id,
    default_tenant_id_for_user,
)
from mica.infrastructure.literature.scope_authority import normalize_cache_scope

logger = logging.getLogger(__name__)

_CACHE_VERSION = 1
_CACHE_ROOT = "institutional-fulltext-cache"
_CACHE_INDEX_ROOT = f"{_CACHE_ROOT}/index"
_CACHE_ENTRY_ROOT = f"{_CACHE_ROOT}/entries"
_GLOBAL_OWNER_KEY = "global"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_slug(value: str, *, limit: int = 48) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "").strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:limit] or "entry"


def _sha16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _paid_content_from_manifest(manifest: Dict[str, Any]) -> bool:
    if bool(manifest.get("paid_content")):
        return True
    for audit in list(manifest.get("acquisition_audit") or []):
        if not isinstance(audit, dict):
            continue
        if _safe_text(audit.get("access_tier")).lower() == "paid_content":
            return True
    return False


def _cache_entry_key(*, canonical_paper_id: str, content_checksum: str, title: str) -> str:
    base = canonical_paper_id or content_checksum or title or "entry"
    return f"{_safe_slug(base)}-{_sha16('|'.join([canonical_paper_id, content_checksum, title]))}"


def _index_object_path(identity_key: str) -> str:
    return f"{_CACHE_INDEX_ROOT}/{_sha16(identity_key)}.json"


@dataclass(frozen=True)
class CacheScopeDescriptor:
    scope: str
    owner_key: str
    storage_owner_id: str


class InstitutionalFullTextCache:
    """Shared literature-body cache over the existing GCS user-bucket authority.

    The owner scope is encoded through synthetic storage owners so the same
    canonical paper body can be reused across:
    - user
    - team/org (mapped from tenant_id)
    - workspace/lab
    - global/public
    """

    def __init__(self, *, storage: Any) -> None:
        self._storage = storage

    def _storage_owner_id(self, *, scope: str, owner_key: str, request_user_id: str) -> str:
        if scope == "user":
            return request_user_id or owner_key or "anonymous-user"
        normalized_owner = owner_key or _GLOBAL_OWNER_KEY
        return f"literature-cache:{scope}:{normalized_owner}"

    def _derive_scope_context(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        request_user_id = _safe_text(metadata.get("user_id") or metadata.get("owner_id"))
        raw_tenant_id = _safe_text(metadata.get("tenant_id"))
        explicit_scope = normalize_cache_scope(
            metadata.get("cache_write_scope")
            or metadata.get("kb_owner_scope")
            or metadata.get("cache_scope")
        )
        default_user_tenant = default_tenant_id_for_user(request_user_id)
        normalized_tenant_id = raw_tenant_id

        if explicit_scope == "global" or normalized_tenant_id.lower() in {"global", "public"}:
            write_scope = CacheScopeDescriptor(
                scope="global",
                owner_key=_GLOBAL_OWNER_KEY,
                storage_owner_id=self._storage_owner_id(
                    scope="global",
                    owner_key=_GLOBAL_OWNER_KEY,
                    request_user_id=request_user_id,
                ),
            )
        elif explicit_scope == "workspace":
            owner_key = normalized_tenant_id or request_user_id or default_user_tenant
            write_scope = CacheScopeDescriptor(
                scope="workspace",
                owner_key=owner_key,
                storage_owner_id=self._storage_owner_id(
                    scope="workspace",
                    owner_key=owner_key,
                    request_user_id=request_user_id,
                ),
            )
        elif explicit_scope == "team":
            owner_key = normalized_tenant_id or request_user_id or default_user_tenant
            write_scope = CacheScopeDescriptor(
                scope="team",
                owner_key=owner_key,
                storage_owner_id=self._storage_owner_id(
                    scope="team",
                    owner_key=owner_key,
                    request_user_id=request_user_id,
                ),
            )
        elif explicit_scope == "user":
            write_scope = CacheScopeDescriptor(
                scope="user",
                owner_key=request_user_id or default_user_tenant,
                storage_owner_id=self._storage_owner_id(
                    scope="user",
                    owner_key=request_user_id or default_user_tenant,
                    request_user_id=request_user_id,
                ),
            )
        elif normalized_tenant_id and normalized_tenant_id not in {"default", default_user_tenant}:
            tenant_lower = normalized_tenant_id.lower()
            inferred_scope = "workspace" if tenant_lower.startswith(("workspace:", "lab:")) else "team"
            owner_key = normalized_tenant_id
            write_scope = CacheScopeDescriptor(
                scope=inferred_scope,
                owner_key=owner_key,
                storage_owner_id=self._storage_owner_id(
                    scope=inferred_scope,
                    owner_key=owner_key,
                    request_user_id=request_user_id,
                ),
            )
        else:
            write_scope = CacheScopeDescriptor(
                scope="user",
                owner_key=request_user_id or default_user_tenant,
                storage_owner_id=self._storage_owner_id(
                    scope="user",
                    owner_key=request_user_id or default_user_tenant,
                    request_user_id=request_user_id,
                ),
            )

        lookup_scopes: List[CacheScopeDescriptor] = []
        user_scope = CacheScopeDescriptor(
            scope="user",
            owner_key=request_user_id or default_user_tenant,
            storage_owner_id=self._storage_owner_id(
                scope="user",
                owner_key=request_user_id or default_user_tenant,
                request_user_id=request_user_id,
            ),
        )
        lookup_scopes.append(user_scope)
        if write_scope not in lookup_scopes:
            lookup_scopes.append(write_scope)
        if write_scope.scope != "global" and normalized_tenant_id.lower() in {"global", "public"}:
            global_scope = CacheScopeDescriptor(
                scope="global",
                owner_key=_GLOBAL_OWNER_KEY,
                storage_owner_id=self._storage_owner_id(
                    scope="global",
                    owner_key=_GLOBAL_OWNER_KEY,
                    request_user_id=request_user_id,
                ),
            )
            if global_scope not in lookup_scopes:
                lookup_scopes.append(global_scope)

        return {
            "request_user_id": request_user_id,
            "tenant_id": normalized_tenant_id or default_user_tenant,
            "write_scope": write_scope,
            "lookup_scopes": lookup_scopes,
        }

    def _identity_keys_from_seed(
        self,
        *,
        paper_id: str,
        doi: str,
        pmid: str,
        pmcid: str,
        arxiv_id: str,
        title: str,
        provider: str,
        metadata: Dict[str, Any],
    ) -> List[str]:
        openalex_payload = dict(metadata.get("openalex") or {})
        openalex_id = _safe_text(
            metadata.get("openalex_id")
            or openalex_payload.get("id")
        )
        provider_paper_id = paper_id or openalex_id
        provider_name = provider or _safe_text(metadata.get("source") or metadata.get("provider") or "literature")
        canonical_id = build_preferred_literature_id(
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            arxiv_id=arxiv_id,
            title=title,
            platform=provider_name,
            paper_id=provider_paper_id,
        )
        return build_literature_identity_keys(
            canonical_id=canonical_id,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            arxiv_id=arxiv_id,
            title=title,
            platform=provider_name,
            paper_id=provider_paper_id,
        )

    def _identity_keys_from_document(self, doc: Any) -> List[str]:
        metadata = dict(getattr(doc, "metadata", {}) or {})
        openalex_payload = dict(metadata.get("openalex") or {})
        openalex_id = _safe_text(metadata.get("openalex_id") or openalex_payload.get("id"))
        provider = _safe_text(getattr(doc, "provider", "") or metadata.get("source") or metadata.get("provider"))
        paper_id = _safe_text(getattr(doc, "paper_id", "") or openalex_id)
        canonical_id = build_preferred_literature_id(
            doi=_safe_text(getattr(doc, "doi", "")),
            pmid=_safe_text(getattr(doc, "pmid", "")),
            pmcid=_safe_text(getattr(doc, "pmcid", "")),
            arxiv_id=_safe_text(getattr(doc, "arxiv_id", "")),
            title=_safe_text(getattr(doc, "title", "")),
            platform=provider,
            paper_id=paper_id,
        )
        return build_literature_identity_keys(
            canonical_id=canonical_id,
            doi=_safe_text(getattr(doc, "doi", "")),
            pmid=_safe_text(getattr(doc, "pmid", "")),
            pmcid=_safe_text(getattr(doc, "pmcid", "")),
            arxiv_id=_safe_text(getattr(doc, "arxiv_id", "")),
            title=_safe_text(getattr(doc, "title", "")),
            platform=provider,
            paper_id=paper_id,
        )

    def _read_json_if_exists(self, *, user_id: str, object_path: str) -> Optional[Dict[str, Any]]:
        try:
            payload = self._storage.read_bytes(user_id=user_id, object_path=object_path)
        except HTTPException:
            return None
        except Exception as exc:
            logger.debug("Institutional cache read miss for %s/%s: %s", user_id, object_path, exc)
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception as exc:
            logger.warning("Institutional cache payload decode failed for %s/%s: %s", user_id, object_path, exc)
            return None

    def _read_text_if_exists(self, *, user_id: str, object_path: str) -> str:
        try:
            payload = self._storage.read_bytes(user_id=user_id, object_path=object_path)
        except HTTPException:
            return ""
        except Exception as exc:
            logger.debug("Institutional cache text read miss for %s/%s: %s", user_id, object_path, exc)
            return ""
        return payload.decode("utf-8", errors="ignore")

    def _manifest_to_document(self, manifest: Dict[str, Any]) -> Any:
        from mica.infrastructure.literature.fulltext_router import NormalizedDocument

        refs = dict(manifest.get("refs") or {})
        storage_owner_id = _safe_text(manifest.get("storage_owner_id"))
        raw_text = self._read_text_if_exists(
            user_id=storage_owner_id,
            object_path=_safe_text(refs.get("normalized_text_object_path")),
        )
        sections = self._read_json_if_exists(
            user_id=storage_owner_id,
            object_path=_safe_text(refs.get("sections_object_path")),
        ) or {}
        citations = self._read_json_if_exists(
            user_id=storage_owner_id,
            object_path=_safe_text(refs.get("citations_object_path")),
        ) or {}
        paper = dict(manifest.get("paper") or {})
        metadata = dict(manifest.get("metadata") or {})

        doc = NormalizedDocument(
            paper_id=_safe_text(paper.get("paper_id")),
            doi=_safe_text(paper.get("doi")),
            pmid=_safe_text(paper.get("pmid")),
            pmcid=_safe_text(paper.get("pmcid")),
            arxiv_id=_safe_text(paper.get("arxiv_id")),
            title=_safe_text(paper.get("title")),
            abstract=_safe_text(paper.get("abstract")),
            full_text=raw_text,
            sections=list(sections if isinstance(sections, list) else []),
            citations=list(citations if isinstance(citations, list) else []),
            acquisition_kind=_safe_text(manifest.get("acquisition_kind")) or "abstract_only",
            provider=_safe_text(manifest.get("provider")),
            license_status=_safe_text(manifest.get("license_status")) or "unknown",
            content_uri=_safe_text(refs.get("content_uri")),
            normalized_text_uri=_safe_text(refs.get("normalized_text_uri")),
            section_json_uri=_safe_text(refs.get("section_json_uri")),
            citations_json_uri=_safe_text(refs.get("citations_json_uri")),
            checksum=_safe_text(manifest.get("content_checksum")),
            degraded=bool(manifest.get("degraded")),
            graph_worthiness_score=float(manifest.get("graph_worthiness_score") or 0.0),
            persistence_eligible=bool(manifest.get("persistence_eligible")),
            persistence_reason=_safe_text(manifest.get("persistence_reason")),
            metadata=metadata,
            year=(int(paper["year"]) if str(paper.get("year") or "").isdigit() else None),
            authors=[str(author) for author in list(paper.get("authors") or []) if str(author)],
            journal=_safe_text(paper.get("journal")),
            acquisition_audit=[dict(item) for item in list(manifest.get("acquisition_audit") or []) if isinstance(item, dict)],
            acquisition_cost_usd=float(manifest.get("acquisition_cost_usd") or 0.0),
        )
        doc.metadata["provider_fetch_receipts"] = list(manifest.get("provider_fetch_receipts") or [])
        doc.metadata["provider_lineage"] = dict(manifest.get("provider_lineage") or {})
        return doc

    def lookup(
        self,
        *,
        paper_id: str = "",
        doi: str = "",
        pmid: str = "",
        pmcid: str = "",
        arxiv_id: str = "",
        title: str = "",
        provider: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        seed_metadata = dict(metadata or {})
        identity_keys = self._identity_keys_from_seed(
            paper_id=paper_id,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            arxiv_id=arxiv_id,
            title=title,
            provider=provider,
            metadata=seed_metadata,
        )
        if not identity_keys:
            return None
        context = self._derive_scope_context(seed_metadata)
        allow_paid_reuse = bool(
            seed_metadata.get("allow_paid_openalex")
            or seed_metadata.get("allow_paid_fulltext")
            or seed_metadata.get("allow_paid_cache_reuse")
        )
        for scope in list(context.get("lookup_scopes") or []):
            for identity_key in identity_keys:
                index_payload = self._read_json_if_exists(
                    user_id=scope.storage_owner_id,
                    object_path=_index_object_path(identity_key),
                )
                if not isinstance(index_payload, dict):
                    continue
                manifest_path = _safe_text(index_payload.get("manifest_object_path"))
                if not manifest_path:
                    continue
                manifest = self._read_json_if_exists(
                    user_id=scope.storage_owner_id,
                    object_path=manifest_path,
                )
                if not isinstance(manifest, dict):
                    continue
                if _paid_content_from_manifest(manifest) and not allow_paid_reuse:
                    continue
                doc = self._manifest_to_document(manifest)
                hit_count = int(manifest.get("hit_count") or 0) + 1
                manifest["hit_count"] = hit_count
                manifest["last_hit_at"] = _now_iso()
                self._storage.upload_text(
                    user_id=scope.storage_owner_id,
                    object_path=manifest_path,
                    text=json.dumps(manifest, ensure_ascii=False, indent=2),
                    content_type="application/json",
                    metadata={
                        "cache_scope": scope.scope,
                        "cache_owner_key": scope.owner_key,
                        "entry_key": _safe_text(manifest.get("entry_key")),
                    },
                )
                doc.metadata["institutional_cache"] = {
                    "status": "hit",
                    "cache_version": int(manifest.get("cache_version") or _CACHE_VERSION),
                    "entry_key": _safe_text(manifest.get("entry_key")),
                    "scope": scope.scope,
                    "owner_key": scope.owner_key,
                    "storage_owner_id": scope.storage_owner_id,
                    "tenant_id": context.get("tenant_id"),
                    "hit_count": hit_count,
                    "paid_content": _paid_content_from_manifest(manifest),
                    "manifest_uri": _safe_text((manifest.get("refs") or {}).get("manifest_uri")),
                    "identity_keys": list(manifest.get("identity_keys") or []),
                }
                return doc
        return None

    def persist(
        self,
        *,
        doc: Any,
        raw_content: str,
    ) -> Dict[str, Any]:
        doc_metadata = dict(getattr(doc, "metadata", {}) or {})
        request_user_id = _safe_text(doc_metadata.get("user_id") or doc_metadata.get("owner_id"))
        if not request_user_id:
            return {"status": "skipped", "reason": "missing_user_context"}
        identity_keys = self._identity_keys_from_document(doc)
        if not identity_keys:
            return {"status": "skipped", "reason": "missing_identity_keys"}

        context = self._derive_scope_context(doc_metadata)
        scope = context["write_scope"]
        existing_manifest = None
        for identity_key in identity_keys:
            index_payload = self._read_json_if_exists(
                user_id=scope.storage_owner_id,
                object_path=_index_object_path(identity_key),
            )
            if not isinstance(index_payload, dict):
                continue
            manifest_path = _safe_text(index_payload.get("manifest_object_path"))
            if not manifest_path:
                continue
            existing_manifest = self._read_json_if_exists(
                user_id=scope.storage_owner_id,
                object_path=manifest_path,
            )
            if isinstance(existing_manifest, dict):
                break

        if (
            isinstance(existing_manifest, dict)
            and not bool(existing_manifest.get("degraded"))
            and bool(getattr(doc, "degraded", True))
        ):
            return {
                "status": "retained_existing",
                "scope": scope.scope,
                "owner_key": scope.owner_key,
                "storage_owner_id": scope.storage_owner_id,
                "entry_key": _safe_text(existing_manifest.get("entry_key")),
                "paid_content": _paid_content_from_manifest(existing_manifest),
                "reason": "existing_fulltext_preferred_over_degraded_update",
                "manifest_uri": _safe_text((existing_manifest.get("refs") or {}).get("manifest_uri")),
                "identity_keys": list(existing_manifest.get("identity_keys") or identity_keys),
            }

        canonical_paper_id = build_preferred_literature_id(
            doi=_safe_text(getattr(doc, "doi", "")),
            pmid=_safe_text(getattr(doc, "pmid", "")),
            pmcid=_safe_text(getattr(doc, "pmcid", "")),
            arxiv_id=_safe_text(getattr(doc, "arxiv_id", "")),
            title=_safe_text(getattr(doc, "title", "")),
            platform=_safe_text(getattr(doc, "provider", "")),
            paper_id=_safe_text(getattr(doc, "paper_id", "")),
        )
        entry_key = _cache_entry_key(
            canonical_paper_id=canonical_paper_id,
            content_checksum=_safe_text(getattr(doc, "checksum", "")),
            title=_safe_text(getattr(doc, "title", "")),
        )
        entry_prefix = f"{_CACHE_ENTRY_ROOT}/{entry_key}"
        content_object_path = f"{entry_prefix}/content.txt"
        normalized_object_path = f"{entry_prefix}/normalized_text.txt"
        sections_object_path = f"{entry_prefix}/sections.json"
        citations_object_path = f"{entry_prefix}/citations.json"
        manifest_object_path = f"{entry_prefix}/manifest.json"

        normalized_text = _safe_text(getattr(doc, "full_text", "") or getattr(doc, "abstract", ""))
        raw_payload = raw_content or normalized_text
        refs = {
            "content_uri": self._storage.upload_text(
                user_id=scope.storage_owner_id,
                object_path=content_object_path,
                text=raw_payload,
                content_type="text/plain; charset=utf-8",
                metadata={
                    "cache_scope": scope.scope,
                    "cache_owner_key": scope.owner_key,
                    "entry_key": entry_key,
                    "paper_id": _safe_text(getattr(doc, "paper_id", "")),
                    "content_checksum": _safe_text(getattr(doc, "checksum", "")),
                },
            ),
            "normalized_text_uri": self._storage.upload_text(
                user_id=scope.storage_owner_id,
                object_path=normalized_object_path,
                text=normalized_text,
                content_type="text/plain; charset=utf-8",
                metadata={
                    "cache_scope": scope.scope,
                    "cache_owner_key": scope.owner_key,
                    "entry_key": entry_key,
                    "paper_id": _safe_text(getattr(doc, "paper_id", "")),
                    "content_checksum": _safe_text(getattr(doc, "checksum", "")),
                },
            ),
            "section_json_uri": self._storage.upload_text(
                user_id=scope.storage_owner_id,
                object_path=sections_object_path,
                text=json.dumps(list(getattr(doc, "sections", []) or []), ensure_ascii=False, indent=2),
                content_type="application/json",
                metadata={
                    "cache_scope": scope.scope,
                    "cache_owner_key": scope.owner_key,
                    "entry_key": entry_key,
                },
            ),
            "citations_json_uri": self._storage.upload_text(
                user_id=scope.storage_owner_id,
                object_path=citations_object_path,
                text=json.dumps(list(getattr(doc, "citations", []) or []), ensure_ascii=False, indent=2),
                content_type="application/json",
                metadata={
                    "cache_scope": scope.scope,
                    "cache_owner_key": scope.owner_key,
                    "entry_key": entry_key,
                },
            ),
            "content_object_path": content_object_path,
            "normalized_text_object_path": normalized_object_path,
            "sections_object_path": sections_object_path,
            "citations_object_path": citations_object_path,
            "manifest_object_path": manifest_object_path,
        }
        metadata_for_manifest = dict(doc_metadata)
        metadata_for_manifest.pop("institutional_cache", None)
        manifest = {
            "cache_version": _CACHE_VERSION,
            "entry_key": entry_key,
            "canonical_paper_id": canonical_paper_id,
            "identity_keys": identity_keys,
            "scope": scope.scope,
            "owner_key": scope.owner_key,
            "storage_owner_id": scope.storage_owner_id,
            "paid_content": any(
                _safe_text(item.get("access_tier")).lower() == "paid_content"
                for item in list(getattr(doc, "acquisition_audit", []) or [])
                if isinstance(item, dict)
            ),
            "provider": _safe_text(getattr(doc, "provider", "")),
            "acquisition_kind": _safe_text(getattr(doc, "acquisition_kind", "")),
            "degraded": bool(getattr(doc, "degraded", True)),
            "license_status": _safe_text(getattr(doc, "license_status", "")),
            "content_checksum": _safe_text(getattr(doc, "checksum", "")),
            "paper": {
                "paper_id": _safe_text(getattr(doc, "paper_id", "")),
                "doi": _safe_text(getattr(doc, "doi", "")),
                "pmid": _safe_text(getattr(doc, "pmid", "")),
                "pmcid": _safe_text(getattr(doc, "pmcid", "")),
                "arxiv_id": _safe_text(getattr(doc, "arxiv_id", "")),
                "title": _safe_text(getattr(doc, "title", "")),
                "abstract": _safe_text(getattr(doc, "abstract", "")),
                "year": getattr(doc, "year", None),
                "authors": list(getattr(doc, "authors", []) or []),
                "journal": _safe_text(getattr(doc, "journal", "")),
            },
            "refs": refs,
            "sections_count": len(list(getattr(doc, "sections", []) or [])),
            "citation_count": len(list(getattr(doc, "citations", []) or [])),
            "graph_worthiness_score": float(getattr(doc, "graph_worthiness_score", 0.0) or 0.0),
            "persistence_eligible": bool(getattr(doc, "persistence_eligible", False)),
            "persistence_reason": _safe_text(getattr(doc, "persistence_reason", "")),
            "metadata": metadata_for_manifest,
            "acquisition_audit": list(getattr(doc, "acquisition_audit", []) or []),
            "acquisition_cost_usd": float(getattr(doc, "acquisition_cost_usd", 0.0) or 0.0),
            "provider_fetch_receipts": list(doc_metadata.get("provider_fetch_receipts") or []),
            "provider_lineage": dict(doc_metadata.get("provider_lineage") or {}),
            "hit_count": int((existing_manifest or {}).get("hit_count") or 0),
            "created_at": _safe_text((existing_manifest or {}).get("created_at")) or _now_iso(),
            "updated_at": _now_iso(),
        }
        manifest_uri = self._storage.upload_text(
            user_id=scope.storage_owner_id,
            object_path=manifest_object_path,
            text=json.dumps(manifest, ensure_ascii=False, indent=2),
            content_type="application/json",
            metadata={
                "cache_scope": scope.scope,
                "cache_owner_key": scope.owner_key,
                "entry_key": entry_key,
                "canonical_paper_id": canonical_paper_id,
                "content_checksum": _safe_text(getattr(doc, "checksum", "")),
            },
        )
        refs["manifest_uri"] = manifest_uri
        manifest["refs"]["manifest_uri"] = manifest_uri
        self._storage.upload_text(
            user_id=scope.storage_owner_id,
            object_path=manifest_object_path,
            text=json.dumps(manifest, ensure_ascii=False, indent=2),
            content_type="application/json",
            metadata={
                "cache_scope": scope.scope,
                "cache_owner_key": scope.owner_key,
                "entry_key": entry_key,
                "canonical_paper_id": canonical_paper_id,
                "content_checksum": _safe_text(getattr(doc, "checksum", "")),
            },
        )

        index_payload = {
            "cache_version": _CACHE_VERSION,
            "entry_key": entry_key,
            "canonical_paper_id": canonical_paper_id,
            "scope": scope.scope,
            "owner_key": scope.owner_key,
            "storage_owner_id": scope.storage_owner_id,
            "manifest_object_path": manifest_object_path,
            "manifest_uri": manifest_uri,
            "content_checksum": _safe_text(getattr(doc, "checksum", "")),
            "updated_at": manifest["updated_at"],
        }
        for identity_key in identity_keys:
            self._storage.upload_text(
                user_id=scope.storage_owner_id,
                object_path=_index_object_path(identity_key),
                text=json.dumps(index_payload, ensure_ascii=False, indent=2),
                content_type="application/json",
                metadata={
                    "cache_scope": scope.scope,
                    "cache_owner_key": scope.owner_key,
                    "entry_key": entry_key,
                    "canonical_paper_id": canonical_paper_id,
                },
            )

        return {
            "status": "updated" if isinstance(existing_manifest, dict) else "stored",
            "cache_version": _CACHE_VERSION,
            "entry_key": entry_key,
            "scope": scope.scope,
            "owner_key": scope.owner_key,
            "storage_owner_id": scope.storage_owner_id,
            "tenant_id": context.get("tenant_id"),
            "paid_content": bool(manifest.get("paid_content")),
            "manifest_uri": manifest_uri,
            "identity_keys": identity_keys,
            "content_checksum": _safe_text(getattr(doc, "checksum", "")),
        }
