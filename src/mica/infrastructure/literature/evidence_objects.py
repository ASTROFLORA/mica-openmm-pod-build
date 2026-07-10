from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class EvidenceObject:
    evidence_id: str
    storage_uri: str
    content_type: str
    size_bytes: int
    sha256: str
    owner_id: str
    logical_alias: str
    producer: str
    producer_type: str
    session_id: str = ""
    run_id: str = ""
    parent_evidence_id: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceObjectManifest:
    manifest_id: str
    owner_id: str
    producer: str
    producer_type: str
    session_id: str = ""
    run_id: str = ""
    evidence_backend: str = ""
    evidence_objects: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["evidence_objects"] = list(payload.get("evidence_objects") or [])
        return payload


def build_evidence_object(
    *,
    storage_uri: str,
    content_type: str,
    payload: bytes,
    owner_id: str,
    logical_alias: str,
    producer: str,
    producer_type: str,
    session_id: str = "",
    run_id: str = "",
    parent_evidence_id: str = "",
) -> EvidenceObject:
    sha256 = hashlib.sha256(payload).hexdigest()
    evidence_id = sha256[:24]
    return EvidenceObject(
        evidence_id=evidence_id,
        storage_uri=str(storage_uri or ""),
        content_type=str(content_type or "application/octet-stream"),
        size_bytes=len(payload),
        sha256=sha256,
        owner_id=str(owner_id or ""),
        logical_alias=str(logical_alias or ""),
        producer=str(producer or "fulltext_router"),
        producer_type=str(producer_type or "literature_fulltext"),
        session_id=str(session_id or ""),
        run_id=str(run_id or ""),
        parent_evidence_id=str(parent_evidence_id or ""),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def build_evidence_manifest(
    *,
    owner_id: str,
    producer: str,
    producer_type: str,
    evidence_backend: str,
    evidence_objects: List[Dict[str, Any]],
    session_id: str = "",
    run_id: str = "",
) -> EvidenceObjectManifest:
    digest_input = "|".join(
        [
            str(owner_id or ""),
            str(producer or ""),
            str(producer_type or ""),
            str(session_id or ""),
            str(run_id or ""),
            *(str(item.get("evidence_id") or "") for item in evidence_objects or []),
        ]
    )
    manifest_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:24]
    return EvidenceObjectManifest(
        manifest_id=manifest_id,
        owner_id=str(owner_id or ""),
        producer=str(producer or "fulltext_router"),
        producer_type=str(producer_type or "literature_fulltext"),
        session_id=str(session_id or ""),
        run_id=str(run_id or ""),
        evidence_backend=str(evidence_backend or ""),
        evidence_objects=[dict(item) for item in list(evidence_objects or [])],
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def infer_owner_id(
    metadata: Optional[Dict[str, Any]],
    *,
    require_authenticated_user: bool = False,
) -> str:
    metadata = metadata or {}
    candidate_keys = ("user_id", "owner_id") if require_authenticated_user else ("owner_id", "user_id", "tenant_id")
    for key in candidate_keys:
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return ""