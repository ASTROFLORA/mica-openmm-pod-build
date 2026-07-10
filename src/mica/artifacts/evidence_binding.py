"""APV-05 EvidenceBinding — artifact-backed Finding evidence with receipts.

Authority: North Star V0.6 §5.3 / APV-05
Hard gate: no path, no promoted finding.

Consumes:
  - ArtifactMembership store (APV-04)
  - EvidencePathBundle.path_ref / bundle_ref (GraphRAG) as opaque path ids
  - PEP / EffectiveContext (APV-01..03)

Does not own: GraphRAG path composition, GovernanceCase, StudyClosure.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from mica.artifacts.membership import get_membership_store
from mica.identity.effective_context import EffectiveContext

SemanticRole = Literal["supports", "contradicts", "context", "method", "result"]


class EvidenceBindingError(ValueError):
    """Fail-closed evidence binding / finding promotion error."""


class FindingStatus(str, Enum):
    DRAFT = "draft"
    PROMOTED = "promoted"
    RETRACTED = "retracted"


class Finding(BaseModel):
    """Minimal product Finding. Promotion requires at least one pathed binding."""

    finding_id: str
    home_scope_id: str
    statement: str
    status: FindingStatus = FindingStatus.DRAFT
    created_by: str
    receipt_id: str
    promoted_at: datetime | None = None
    promotion_receipt_id: str | None = None
    binding_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceBinding(BaseModel):
    binding_id: str
    finding_id: str
    artifact_id: str
    artifact_membership_id: str
    evidence_path_id: str | None = None
    semantic_role: SemanticRole
    excerpt_selector: dict[str, Any] | None = None
    created_by: str
    receipt_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _receipt_id(kind: str, *parts: str) -> str:
    material = ":".join([kind, *parts, uuid.uuid4().hex[:8]])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"receipt:evidence_binding:{kind}:{digest}"


def _is_authoritative_path_id(path_id: str | None) -> bool:
    if not path_id or not str(path_id).strip():
        return False
    normalized = str(path_id).strip()
    return normalized.startswith(
        (
            "path://",
            "evidence_path://",
            "evidence_path_bundle://",
        )
    )


class EvidenceBindingStore:
    """Process-authoritative Finding + EvidenceBinding store."""

    def __init__(self) -> None:
        self._findings: dict[str, Finding] = {}
        self._bindings: dict[str, EvidenceBinding] = {}

    def upsert_finding(self, finding: Finding) -> Finding:
        self._findings[finding.finding_id] = finding
        return finding

    def get_finding(self, finding_id: str) -> Finding | None:
        return self._findings.get(finding_id)

    def upsert_binding(self, binding: EvidenceBinding) -> EvidenceBinding:
        self._bindings[binding.binding_id] = binding
        finding = self._findings.get(binding.finding_id)
        if finding is not None and binding.binding_id not in finding.binding_ids:
            finding.binding_ids.append(binding.binding_id)
            self._findings[finding.finding_id] = finding
        return binding

    def get_binding(self, binding_id: str) -> EvidenceBinding | None:
        return self._bindings.get(binding_id)

    def list_bindings_for_finding(self, finding_id: str) -> list[EvidenceBinding]:
        return [b for b in self._bindings.values() if b.finding_id == finding_id]

    def list_bindings_for_membership(self, membership_id: str) -> list[EvidenceBinding]:
        return [b for b in self._bindings.values() if b.artifact_membership_id == membership_id]

    def finding_has_authoritative_path(self, finding_id: str) -> bool:
        return any(
            _is_authoritative_path_id(b.evidence_path_id)
            for b in self.list_bindings_for_finding(finding_id)
        )

    def clear(self) -> None:
        self._findings.clear()
        self._bindings.clear()


_STORE: EvidenceBindingStore | None = None


def get_evidence_binding_store() -> EvidenceBindingStore:
    global _STORE
    if _STORE is None:
        _STORE = EvidenceBindingStore()
    return _STORE


def reset_evidence_binding_store_for_tests() -> EvidenceBindingStore:
    global _STORE
    _STORE = EvidenceBindingStore()
    return _STORE


def create_finding(
    *,
    ctx: EffectiveContext,
    statement: str,
    home_scope_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Finding:
    statement_norm = (statement or "").strip()
    if not statement_norm:
        raise EvidenceBindingError("finding statement is required")
    finding = Finding(
        finding_id=str(uuid.uuid4()),
        home_scope_id=home_scope_id or ctx.active_scope_id,
        statement=statement_norm,
        status=FindingStatus.DRAFT,
        created_by=ctx.actor_user_id,
        receipt_id=_receipt_id("finding_create", ctx.actor_user_id),
        metadata=dict(metadata or {}),
    )
    return get_evidence_binding_store().upsert_finding(finding)


def create_evidence_binding(
    *,
    ctx: EffectiveContext,
    finding_id: str,
    artifact_membership_id: str,
    semantic_role: SemanticRole,
    evidence_path_id: str | None = None,
    excerpt_selector: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> EvidenceBinding:
    """Bind an artifact membership to a finding.

    Membership must exist and match artifact_id. Path is optional for draft
    context/method attachments; promotion still requires an authoritative path.
    """
    store = get_evidence_binding_store()
    finding = store.get_finding(finding_id)
    if finding is None:
        raise EvidenceBindingError(f"finding not found: {finding_id}")
    if finding.status == FindingStatus.RETRACTED:
        raise EvidenceBindingError("cannot bind to retracted finding")

    membership = get_membership_store().get(artifact_membership_id)
    if membership is None or membership.archived:
        raise EvidenceBindingError(
            f"artifact_membership not found: {artifact_membership_id}"
        )

    if evidence_path_id is not None and str(evidence_path_id).strip():
        if not _is_authoritative_path_id(evidence_path_id):
            raise EvidenceBindingError(
                "evidence_path_id must be an authoritative path/bundle ref "
                "(path://, evidence_path://, or evidence_path_bundle://)"
            )

    binding = EvidenceBinding(
        binding_id=str(uuid.uuid4()),
        finding_id=finding_id,
        artifact_id=membership.artifact_id,
        artifact_membership_id=artifact_membership_id,
        evidence_path_id=(str(evidence_path_id).strip() if evidence_path_id else None),
        semantic_role=semantic_role,
        excerpt_selector=excerpt_selector,
        created_by=ctx.actor_user_id,
        receipt_id=_receipt_id("bind", finding_id, membership.artifact_id),
        metadata=dict(metadata or {}),
    )
    return store.upsert_binding(binding)


def promote_finding(
    *,
    ctx: EffectiveContext,
    finding_id: str,
    metadata: dict[str, Any] | None = None,
) -> Finding:
    """Promote a finding. Fail-closed without an authoritative evidence path."""
    store = get_evidence_binding_store()
    finding = store.get_finding(finding_id)
    if finding is None:
        raise EvidenceBindingError(f"finding not found: {finding_id}")
    if finding.status == FindingStatus.PROMOTED:
        return finding
    if finding.status == FindingStatus.RETRACTED:
        raise EvidenceBindingError("cannot promote retracted finding")

    bindings = store.list_bindings_for_finding(finding_id)
    if not bindings:
        raise EvidenceBindingError(
            "no_path_no_promoted_finding: finding has no EvidenceBinding"
        )
    if not store.finding_has_authoritative_path(finding_id):
        raise EvidenceBindingError(
            "no_path_no_promoted_finding: at least one EvidenceBinding must carry "
            "an authoritative evidence_path_id before promotion"
        )

    finding.status = FindingStatus.PROMOTED
    finding.promoted_at = datetime.now(timezone.utc)
    finding.promotion_receipt_id = _receipt_id("promote", finding_id, ctx.actor_user_id)
    if metadata:
        finding.metadata = {**finding.metadata, **metadata}
    return store.upsert_finding(finding)


def bind_path_from_bundle(
    *,
    ctx: EffectiveContext,
    finding_id: str,
    artifact_membership_id: str,
    path_ref: str,
    bundle_ref: str | None = None,
    semantic_role: SemanticRole = "supports",
    excerpt_selector: dict[str, Any] | None = None,
) -> EvidenceBinding:
    """Convenience: bind using GraphRAG EvidencePathBundle refs without recomposing."""
    meta: dict[str, Any] = {}
    if bundle_ref:
        meta["bundle_ref"] = bundle_ref
    return create_evidence_binding(
        ctx=ctx,
        finding_id=finding_id,
        artifact_membership_id=artifact_membership_id,
        semantic_role=semantic_role,
        evidence_path_id=path_ref,
        excerpt_selector=excerpt_selector,
        metadata=meta,
    )


__all__ = [
    "EvidenceBinding",
    "EvidenceBindingError",
    "EvidenceBindingStore",
    "Finding",
    "FindingStatus",
    "bind_path_from_bundle",
    "create_evidence_binding",
    "create_finding",
    "get_evidence_binding_store",
    "promote_finding",
    "reset_evidence_binding_store_for_tests",
]
