"""APV-04 ArtifactMembership — scoped artifact attachment and cross-scope ops.

Authority: North Star V0.6 §5.2–5.3 / APV-04
Consumes: EffectivePermissionEngine (APV-02), PEP helpers (APV-03), scope refs (APV-01)
Does not own: PDP evaluation, EvidenceBinding (APV-05), GovernanceCase (APV-18)
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from mica.identity.effective_context import EffectiveContext, personal_home_scope_id
from mica.tenancy.effective_permission_engine import (
    get_permission_engine,
    grant_from_acl_role,
)
from mica.tenancy.models import ScopeType, build_scope_ref
from mica.tenancy.pep import build_target_ref

ContainerType = Literal["knowledge_space", "study", "workspace", "research_line"]
Visibility = Literal["private", "scope", "shared", "public"]
CrossScopeOpName = Literal["link", "share", "copy", "fork", "propose_promotion", "transfer"]


class ArtifactMembershipError(ValueError):
    """Fail-closed membership / cross-scope error."""


class CrossScopeOperationName(str, Enum):
    LINK = "link"
    SHARE = "share"
    COPY = "copy"
    FORK = "fork"
    PROPOSE_PROMOTION = "propose_promotion"
    TRANSFER = "transfer"


class ScopedResourceRef(BaseModel):
    resource_type: str
    resource_id: str
    home_scope_id: str
    origin_scope_id: str | None = None
    created_by: str
    visibility: Visibility = "private"


class ArtifactMembership(BaseModel):
    membership_id: str
    artifact_id: str
    container_type: ContainerType
    container_id: str
    home_scope_id: str
    semantic_role: str = "attached"
    origin_membership_id: str | None = None
    attached_by: str
    receipt_id: str
    grantee_principal_ref: str | None = None
    acl_role: str = "viewer"
    archived: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CrossScopeOperation(BaseModel):
    operation: CrossScopeOpName
    source: ScopedResourceRef
    destination_scope_id: str
    expected_permission_fingerprint: str
    idempotency_key: str
    grantee_user_id: str | None = None
    semantic_role: str = "attached"
    acl_role: str = "viewer"
    container_type: ContainerType | None = None
    container_id: str | None = None


class CrossScopeResult(BaseModel):
    operation: CrossScopeOpName
    receipt_id: str
    membership: ArtifactMembership | None = None
    derived_artifact_id: str | None = None
    status: str
    notes: str | None = None


def study_scope_id(study_id: str) -> str:
    return build_scope_ref(ScopeType.STUDY, study_id)


def container_scope_id(container_type: str, container_id: str) -> str:
    if container_type == "study":
        return study_scope_id(container_id)
    if container_type == "knowledge_space":
        return f"lab:{container_id}"  # provisional until KS scope type lands
    if container_type == "research_line":
        return f"study:{container_id}"
    if container_type == "workspace":
        return f"user:{container_id}"
    raise ArtifactMembershipError(f"unsupported container_type: {container_type}")


def _receipt_id(kind: str, *parts: str) -> str:
    material = ":".join([kind, *parts, uuid.uuid4().hex[:8]])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"receipt:artifact_membership:{kind}:{digest}"


def _principal_ref(user_id: str) -> str:
    return f"user://{user_id}"


class ArtifactMembershipStore:
    """Process-authoritative membership store (APV-02 grant-store pattern)."""

    def __init__(self) -> None:
        self._by_id: dict[str, ArtifactMembership] = {}
        self._idempotency: dict[str, CrossScopeResult] = {}

    def upsert(self, membership: ArtifactMembership) -> ArtifactMembership:
        self._by_id[membership.membership_id] = membership
        return membership

    def get(self, membership_id: str) -> ArtifactMembership | None:
        return self._by_id.get(membership_id)

    def list_for_artifact(self, artifact_id: str) -> list[ArtifactMembership]:
        return [m for m in self._by_id.values() if m.artifact_id == artifact_id and not m.archived]

    def list_for_container(self, container_type: str, container_id: str) -> list[ArtifactMembership]:
        return [
            m
            for m in self._by_id.values()
            if m.container_type == container_type
            and m.container_id == container_id
            and not m.archived
        ]

    def list_visible_artifact_ids(self, actor_user_id: str) -> set[str]:
        principal = _principal_ref(actor_user_id)
        out: set[str] = set()
        for m in self._by_id.values():
            if m.archived:
                continue
            if m.attached_by == actor_user_id:
                out.add(m.artifact_id)
            elif m.grantee_principal_ref == principal:
                out.add(m.artifact_id)
        return out

    def find_by_unique(
        self, *, artifact_id: str, container_type: str, container_id: str
    ) -> ArtifactMembership | None:
        for m in self._by_id.values():
            if (
                m.artifact_id == artifact_id
                and m.container_type == container_type
                and m.container_id == container_id
                and not m.archived
            ):
                return m
        return None

    def get_idempotent(self, key: str) -> CrossScopeResult | None:
        return self._idempotency.get(key)

    def put_idempotent(self, key: str, result: CrossScopeResult) -> None:
        self._idempotency[key] = result

    def clear(self) -> None:
        self._by_id.clear()
        self._idempotency.clear()


_STORE: ArtifactMembershipStore | None = None


def get_membership_store() -> ArtifactMembershipStore:
    global _STORE
    if _STORE is None:
        _STORE = ArtifactMembershipStore()
    return _STORE


def reset_membership_store_for_tests() -> ArtifactMembershipStore:
    global _STORE
    _STORE = ArtifactMembershipStore()
    return _STORE


def _register_membership_grant(
    *,
    grantee_user_id: str,
    artifact_id: str,
    role: str,
    granted_by: str,
) -> None:
    """Register ACL-shaped grant on the grantee's home scope so PEP matches request ctx."""
    engine = get_permission_engine()
    engine.upsert_grant(
        grant_from_acl_role(
            principal_user_id=grantee_user_id,
            scope_ref=personal_home_scope_id(grantee_user_id),
            target_ref=build_target_ref("artifact", artifact_id),
            role=role,
            granted_by=granted_by,
        )
    )


def attach_artifact_membership(
    *,
    ctx: EffectiveContext,
    artifact_id: str,
    container_type: ContainerType,
    container_id: str,
    semantic_role: str = "attached",
    grantee_user_id: str | None = None,
    acl_role: str = "viewer",
    origin_membership_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ArtifactMembership:
    """Create (or return existing) ArtifactMembership and bridge READ grant for grantee."""
    store = get_membership_store()
    existing = store.find_by_unique(
        artifact_id=artifact_id,
        container_type=container_type,
        container_id=container_id,
    )
    if existing is not None:
        return existing

    home_scope_id = container_scope_id(container_type, container_id)
    grantee = grantee_user_id or ctx.actor_user_id
    membership = ArtifactMembership(
        membership_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        container_type=container_type,
        container_id=container_id,
        home_scope_id=home_scope_id,
        semantic_role=semantic_role,
        origin_membership_id=origin_membership_id,
        attached_by=ctx.actor_user_id,
        receipt_id=_receipt_id("attach", artifact_id, container_type, container_id),
        grantee_principal_ref=_principal_ref(grantee),
        acl_role=acl_role,
        metadata=dict(metadata or {}),
    )
    store.upsert(membership)
    _register_membership_grant(
        grantee_user_id=grantee,
        artifact_id=artifact_id,
        role=acl_role,
        granted_by=ctx.actor_user_id,
    )
    return membership


def execute_cross_scope_operation(
    *,
    ctx: EffectiveContext,
    operation: CrossScopeOperation,
) -> CrossScopeResult:
    """Execute link/share/copy/fork/propose_promotion. transfer is stubbed."""
    if operation.expected_permission_fingerprint != ctx.permission_fingerprint:
        raise ArtifactMembershipError(
            "permission_fingerprint_mismatch: "
            f"expected={operation.expected_permission_fingerprint} "
            f"actual={ctx.permission_fingerprint}"
        )

    store = get_membership_store()
    cached = store.get_idempotent(operation.idempotency_key)
    if cached is not None:
        return cached

    op = operation.operation
    source = operation.source

    if op == "transfer":
        result = CrossScopeResult(
            operation=op,
            receipt_id=_receipt_id("transfer_stub", source.resource_id),
            status="not_implemented",
            notes="transfer deferred; use propose_promotion + GovernanceCase (APV-18)",
        )
        store.put_idempotent(operation.idempotency_key, result)
        return result

    if op == "propose_promotion":
        result = CrossScopeResult(
            operation=op,
            receipt_id=_receipt_id("propose_promotion", source.resource_id, operation.destination_scope_id),
            status="proposed",
            notes="GovernanceCase hook reserved for APV-18; no canonical mutation",
        )
        store.put_idempotent(operation.idempotency_key, result)
        return result

    container_type: ContainerType = operation.container_type or "study"
    container_id = operation.container_id or operation.destination_scope_id.split(":", 1)[-1]

    if op == "link":
        origin = None
        if source.resource_type == "membership":
            origin = source.resource_id
        elif source.resource_type == "artifact":
            # Link creates destination membership rooted at source artifact.
            origin = None
        membership = attach_artifact_membership(
            ctx=ctx,
            artifact_id=source.resource_id if source.resource_type == "artifact" else _artifact_from_membership(source.resource_id),
            container_type=container_type,
            container_id=container_id,
            semantic_role=operation.semantic_role or "linked",
            grantee_user_id=operation.grantee_user_id or ctx.actor_user_id,
            acl_role=operation.acl_role,
            origin_membership_id=origin,
            metadata={"cross_scope": "link", "destination_scope_id": operation.destination_scope_id},
        )
        result = CrossScopeResult(
            operation=op,
            receipt_id=membership.receipt_id,
            membership=membership,
            status="linked",
        )
        store.put_idempotent(operation.idempotency_key, result)
        return result

    if op == "share":
        if not operation.grantee_user_id:
            raise ArtifactMembershipError("share requires grantee_user_id")
        if source.resource_type != "artifact":
            raise ArtifactMembershipError("share source must be artifact")
        membership = attach_artifact_membership(
            ctx=ctx,
            artifact_id=source.resource_id,
            container_type=container_type,
            container_id=container_id,
            semantic_role=operation.semantic_role or "shared",
            grantee_user_id=operation.grantee_user_id,
            acl_role=operation.acl_role,
            metadata={
                "cross_scope": "share",
                "destination_scope_id": operation.destination_scope_id,
                "visibility": "shared",
            },
        )
        result = CrossScopeResult(
            operation=op,
            receipt_id=membership.receipt_id,
            membership=membership,
            status="shared",
        )
        store.put_idempotent(operation.idempotency_key, result)
        return result

    if op in ("copy", "fork"):
        if source.resource_type != "artifact":
            raise ArtifactMembershipError(f"{op} source must be artifact")
        derived_id = str(uuid.uuid4())
        membership = attach_artifact_membership(
            ctx=ctx,
            artifact_id=derived_id,
            container_type=container_type,
            container_id=container_id,
            semantic_role=operation.semantic_role or op,
            grantee_user_id=operation.grantee_user_id or ctx.actor_user_id,
            acl_role=operation.acl_role or "editor",
            metadata={
                "cross_scope": op,
                "source_artifact_id": source.resource_id,
                "destination_scope_id": operation.destination_scope_id,
                "derived": True,
            },
        )
        # Also grant READ on source lineage pointer for the actor (cite/context).
        _register_membership_grant(
            grantee_user_id=ctx.actor_user_id,
            artifact_id=source.resource_id,
            role="viewer",
            granted_by=ctx.actor_user_id,
        )
        result = CrossScopeResult(
            operation=op,
            receipt_id=membership.receipt_id,
            membership=membership,
            derived_artifact_id=derived_id,
            status="copied" if op == "copy" else "forked",
            notes=f"{op} created derived artifact identity; durable blob copy is Storage lane",
        )
        store.put_idempotent(operation.idempotency_key, result)
        return result

    raise ArtifactMembershipError(f"unsupported operation: {op}")


def _artifact_from_membership(membership_id: str) -> str:
    m = get_membership_store().get(membership_id)
    if m is None:
        raise ArtifactMembershipError(f"membership not found: {membership_id}")
    return m.artifact_id


def scoped_resource_from_artifact(
    *,
    artifact_id: str,
    home_scope_id: str,
    created_by: str,
    visibility: Visibility = "private",
    origin_scope_id: str | None = None,
) -> ScopedResourceRef:
    return ScopedResourceRef(
        resource_type="artifact",
        resource_id=artifact_id,
        home_scope_id=home_scope_id,
        origin_scope_id=origin_scope_id,
        created_by=created_by,
        visibility=visibility,
    )


__all__ = [
    "ArtifactMembership",
    "ArtifactMembershipError",
    "ArtifactMembershipStore",
    "CrossScopeOperation",
    "CrossScopeOperationName",
    "CrossScopeResult",
    "ScopedResourceRef",
    "attach_artifact_membership",
    "container_scope_id",
    "execute_cross_scope_operation",
    "get_membership_store",
    "reset_membership_store_for_tests",
    "scoped_resource_from_artifact",
    "study_scope_id",
]
