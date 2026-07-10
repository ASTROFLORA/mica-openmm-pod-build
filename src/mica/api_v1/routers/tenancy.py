"""src/mica/api_v1/routers/tenancy.py — Tenancy API router (T0.1 + T1).

Authority: Tenancy Lab Org — Capa T0.1 · Personalización tenant
  — T0.1.1 · Org/Lab/User hierarchy model
  — T0.1.3 · ScopeMemoryIndex
  — T0.1.4 · Sharing / federation primitives
  — T0.1.7 · Tenant onboarding/offboarding
  — T1.1   · RoleAssignment CRUD
  — T1.7   · ShareContract CRUD + revocation
  — T1.13  · Policy hierarchy

All state is in-memory (dict store). Production should replace with Postgres/TimescaleDB.

Doctrine:
  - No role assignment = no access (linea roja T1)
  - RBAC grants possibility; ABAC decides permission
  - Legal/compliance labels always veto (T0.1)
  - EffectivePermission by 8-plane intersection
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mica.api_v1.auth import user_dependency
from mica.tenancy.models import (
    CanonicalRole,
    EffectivePermission,
    PermissionAction,
    PolicyDecision,
    PolicyScope,
    RoleAssignment,
    RoleConstraints,
    ScopeMemoryIndex,
    ScopeType,
    TenantPolicy,
    build_scope_ref,
    parse_scope_ref,
)
from mica.tenancy.shares import (
    ShareContract,
    ShareGranteeType,
    ShareRevokeReceipt,
    ShareStatus,
    PropagationTarget,
    create_study_share,
    create_artifact_share,
    create_claim_share,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenancy", tags=["tenancy"])

# ═══════════════════════════════════════════════════════════════════════════
# In-memory store (temporal — reemplazar con Postgres en próxima iteración)
# ═══════════════════════════════════════════════════════════════════════════

_role_store: dict[str, RoleAssignment] = {}
_share_store: dict[str, ShareContract] = {}
_policy_store: dict[str, TenantPolicy] = {}
_scope_memory_store: dict[str, ScopeMemoryIndex] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════


class RoleAssignRequest(BaseModel):
    """POST body for role assignment."""
    principal_ref: str
    scope_type: ScopeType
    scope_ref: str
    role: CanonicalRole
    granted_by: str
    valid_to: Optional[datetime] = None
    constraints: Optional[RoleConstraints] = None


class RoleResponse(BaseModel):
    """Role assignment response."""
    assignment_id: str
    principal_ref: str
    scope_ref: str
    role: CanonicalRole
    active: bool
    valid_from: datetime
    valid_to: Optional[datetime] = None
    constraints: Optional[RoleConstraints] = None


class RoleListResponse(BaseModel):
    """List of role assignments."""
    roles: list[RoleResponse]
    total: int


class ShareCreateRequest(BaseModel):
    """POST body for share creation."""
    share_type: str  # 'study' | 'artifact' | 'claim'
    target_ref: str
    grantee_type: ShareGranteeType
    grantee_ref: str
    granted_by: str
    actions: list[PermissionAction] = [PermissionAction.READ, PermissionAction.CITE]
    expires_at: Optional[datetime] = None


class ShareResponse(BaseModel):
    """Share response."""
    share_ref: str
    share_type: str
    target_ref: str
    grantee_ref: str
    status: ShareStatus
    actions: list[PermissionAction]
    expires_at: Optional[datetime] = None


class ShareListResponse(BaseModel):
    """List of shares."""
    shares: list[ShareResponse]
    total: int


class EffectivePermissionRequest(BaseModel):
    """POST body for EffectivePermission calculation."""
    principal_ref: str
    scope_ref: str
    target_ref: str
    action: PermissionAction = PermissionAction.READ


class EffectivePermissionResponse(BaseModel):
    """EffectivePermission calculation result."""
    principal_ref: str
    scope_ref: str
    target_ref: str
    final_decision: PolicyDecision
    layer_count: int
    layers: list[dict[str, Any]]
    receipt_ref: Optional[str] = None
    action: Optional[PermissionAction] = None
    policy_snapshot_id: Optional[str] = None
    permission_fingerprint: Optional[str] = None
    allowed: Optional[bool] = None


class ScopeMemoryResponse(BaseModel):
    """ScopeMemoryIndex response."""
    index_id: str
    scope_ref: str
    collection_contract_ref: str
    partition_key: str
    isolated_collection: bool


class PolicyResponse(BaseModel):
    """TenantPolicy response."""
    policy_ref: str
    policy_scope: PolicyScope
    scope_ref: str
    version: int
    active: bool
    rule_count: int


# ═══════════════════════════════════════════════════════════════════════════
# Dependency — inject RequestIdentity for tenancy requests
# ═══════════════════════════════════════════════════════════════════════════


async def _identity_or_fallback(user_id: str = Depends(user_dependency)) -> dict:
    """Extract user identity for tenancy operations.

    P0: uses user_id from auth. T1.5+ will use full RequestIdentity.
    """
    return {"user_id": user_id}


# ═══════════════════════════════════════════════════════════════════════════
# RoleAssignment endpoints  (T1.1)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/roles", response_model=RoleResponse, status_code=201)
async def assign_role(body: RoleAssignRequest, identity: dict = Depends(_identity_or_fallback)):
    """Assign a role to a principal within a scope.

    Doc T1.1: ``RoleAssignment: principal_ref · scope_ref(lab|org|study) ·
    role · granted_by · valid_from · valid_to · constraints · receipt_ref``
    """
    assignment = RoleAssignment(
        assignment_id=f"ra_{len(_role_store) + 1}",
        principal_ref=body.principal_ref,
        scope_type=body.scope_type,
        scope_ref=body.scope_ref,
        role=body.role,
        granted_by=body.granted_by,
        valid_to=body.valid_to,
        constraints=body.constraints or RoleConstraints(),
    )
    _role_store[assignment.assignment_id] = assignment
    # APV-02: ingest into the single product PDP store (tenancy dict is not authority).
    from mica.tenancy.effective_permission_engine import get_permission_engine

    get_permission_engine().ingest_role_assignment(assignment)
    logger.info("Role assigned: %s -> %s on %s", assignment.principal_ref, assignment.role.value, assignment.scope_ref)
    return RoleResponse(
        assignment_id=assignment.assignment_id,
        principal_ref=assignment.principal_ref,
        scope_ref=assignment.scope_ref,
        role=assignment.role,
        active=assignment.is_valid(),
        valid_from=assignment.valid_from,
        valid_to=assignment.valid_to,
        constraints=assignment.constraints,
    )


@router.get("/roles", response_model=RoleListResponse)
async def list_roles(
    principal_ref: Optional[str] = Query(None, description="Filter by principal"),
    scope_ref: Optional[str] = Query(None, description="Filter by scope"),
    identity: dict = Depends(_identity_or_fallback),
):
    """List role assignments, optionally filtered by principal or scope."""
    roles = list(_role_store.values())
    if principal_ref:
        roles = [r for r in roles if r.principal_ref == principal_ref]
    if scope_ref:
        roles = [r for r in roles if r.scope_ref == scope_ref]
    return RoleListResponse(
        roles=[
            RoleResponse(
                assignment_id=r.assignment_id,
                principal_ref=r.principal_ref,
                scope_ref=r.scope_ref,
                role=r.role,
                active=r.is_valid(),
                valid_from=r.valid_from,
                valid_to=r.valid_to,
                constraints=r.constraints,
            )
            for r in roles
        ],
        total=len(roles),
    )


@router.get("/roles/{assignment_id}", response_model=RoleResponse)
async def get_role(assignment_id: str, identity: dict = Depends(_identity_or_fallback)):
    """Get a specific role assignment by ID."""
    role = _role_store.get(assignment_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"RoleAssignment {assignment_id} not found")
    return RoleResponse(
        assignment_id=role.assignment_id,
        principal_ref=role.principal_ref,
        scope_ref=role.scope_ref,
        role=role.role,
        active=role.is_valid(),
        valid_from=role.valid_from,
        valid_to=role.valid_to,
        constraints=role.constraints,
    )


@router.delete("/roles/{assignment_id}", status_code=204)
async def revoke_role(assignment_id: str, identity: dict = Depends(_identity_or_fallback)):
    """Revoke a role assignment (soft-delete: marks inactive).

    Doc T1.1: reassignment is explicit; revoke preserves receipt chain.
    """
    role = _role_store.get(assignment_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"RoleAssignment {assignment_id} not found")
    role.active = False
    logger.info("Role revoked: %s (%s)", assignment_id, role.principal_ref)


# ═══════════════════════════════════════════════════════════════════════════
# Share endpoints  (T0.1.4 / T1.7)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/shares", response_model=ShareResponse, status_code=201)
async def create_share(body: ShareCreateRequest, identity: dict = Depends(_identity_or_fallback)):
    """Create a share (StudyShare, ArtifactShare, or ClaimShare).

    Doc T1.7: ``Objetos: StudyShare · ArtifactShare · ClaimShare``
    Doc T0.1.4: ``compartir paquete o permiso explícito``
    """
    factory_map = {
        "study": create_study_share,
        "artifact": create_artifact_share,
        "claim": create_claim_share,
    }
    factory = factory_map.get(body.share_type)
    if not factory:
        raise HTTPException(status_code=400, detail=f"Invalid share_type: {body.share_type}. Use study/artifact/claim.")

    share = factory(
        target_ref=body.target_ref,
        grantee_type=body.grantee_type,
        grantee_ref=body.grantee_ref,
        granted_by=body.granted_by,
        actions=body.actions,
        expires_at=body.expires_at,
    )
    _share_store[share.share_ref] = share
    logger.info("Share created: %s (%s -> %s)", share.share_ref, share.share_type, share.grantee_ref)
    return ShareResponse(
        share_ref=share.share_ref,
        share_type=share.share_type,
        target_ref=share.target_ref,
        grantee_ref=share.grantee_ref,
        status=share.status,
        actions=share.actions,
        expires_at=share.expires_at,
    )


@router.get("/shares", response_model=ShareListResponse)
async def list_shares(
    grantee_ref: Optional[str] = Query(None, description="Filter by grantee"),
    share_type: Optional[str] = Query(None, description="Filter by type (study/artifact/claim)"),
    identity: dict = Depends(_identity_or_fallback),
):
    """List shares, optionally filtered."""
    shares = list(_share_store.values())
    if grantee_ref:
        shares = [s for s in shares if s.grantee_ref == grantee_ref]
    if share_type:
        shares = [s for s in shares if s.share_type == share_type]
    return ShareListResponse(
        shares=[
            ShareResponse(
                share_ref=s.share_ref,
                share_type=s.share_type,
                target_ref=s.target_ref,
                grantee_ref=s.grantee_ref,
                status=s.status,
                actions=s.actions,
                expires_at=s.expires_at,
            )
            for s in shares
        ],
        total=len(shares),
    )


@router.get("/shares/{share_id}", response_model=ShareResponse)
async def get_share(share_id: str, identity: dict = Depends(_identity_or_fallback)):
    """Get a specific share by ref."""
    share = _share_store.get(share_id)
    if not share:
        raise HTTPException(status_code=404, detail=f"Share {share_id} not found")
    return ShareResponse(
        share_ref=share.share_ref,
        share_type=share.share_type,
        target_ref=share.target_ref,
        grantee_ref=share.grantee_ref,
        status=share.status,
        actions=share.actions,
        expires_at=share.expires_at,
    )


@router.post("/shares/{share_id}/revoke", response_model=ShareRevokeReceipt)
async def revoke_share(share_id: str, identity: dict = Depends(_identity_or_fallback)):
    """Revoke a share and return revocation propagation receipt.

    Doc T1.7: ``Revocation is not complete until caches, search, graph and
    agent memory acknowledge.``
    """
    share = _share_store.get(share_id)
    if not share:
        raise HTTPException(status_code=404, detail=f"Share {share_id} not found")
    receipt = share.revoke()
    logger.info("Share revoked: %s -> %s", share_id, share.target_ref)
    return receipt


# ═══════════════════════════════════════════════════════════════════════════
# EffectivePermission endpoint  (T0.1 / T1.1)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/effective-permission", response_model=EffectivePermissionResponse)
async def compute_effective_permission(
    body: EffectivePermissionRequest,
    identity: dict = Depends(_identity_or_fallback),
):
    """Calculate EffectivePermission via the APV-02 product PDP engine.

    Legacy in-memory 8-plane stubs are no longer the authority. Role grants
    written through this router are ingested into EffectivePermissionEngine.
    """
    from mica.identity.effective_context import EffectiveContextHints, resolve_effective_context
    from mica.tenancy.effective_permission_engine import get_permission_engine, principal_ref_for_user

    engine = get_permission_engine()
    # Prefer principal_ref user id; fall back to authenticated caller.
    principal = body.principal_ref
    if principal.startswith("user://"):
        actor_user_id = principal.split("://", 1)[1]
    else:
        actor_user_id = identity.get("user_id") or principal

    # Ensure caller cannot evaluate as another principal without matching auth.
    caller = identity.get("user_id")
    if caller and principal_ref_for_user(caller) != principal_ref_for_user(actor_user_id):
        # Still allow evaluation of the requested principal when grants exist;
        # do not widen grants. Authz of "who may query whose permission" is APV-03.
        pass

    ctx = resolve_effective_context(
        identity=actor_user_id,
        hints=EffectiveContextHints(active_scope_id=body.scope_ref),
    )
    decision = engine.evaluate(
        context=ctx,
        target_ref=body.target_ref,
        action=body.action,
    )
    return EffectivePermissionResponse(
        principal_ref=decision.principal_ref,
        scope_ref=decision.scope_ref,
        target_ref=decision.target_ref,
        final_decision=decision.final_decision,
        layer_count=len(decision.layers),
        layers=decision.layers,
        receipt_ref=decision.receipt_ref,
        action=decision.action,
        policy_snapshot_id=decision.policy_snapshot_id,
        permission_fingerprint=decision.permission_fingerprint,
        allowed=decision.allowed,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ScopeMemoryIndex endpoints  (T0.1.3 / T1.2)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/scope-memory", response_model=list[ScopeMemoryResponse])
async def list_scope_memory(
    scope_ref: Optional[str] = Query(None, description="Filter by scope ref"),
    identity: dict = Depends(_identity_or_fallback),
):
    """List ScopeMemoryIndex entries.

    Doc T0.1.3: ``ScopeMemoryIndex: scope_type, scope_ref, collection_contract_ref``
    """
    indices = list(_scope_memory_store.values())
    if scope_ref:
        indices = [i for i in indices if i.scope_ref == scope_ref]
    return [
        ScopeMemoryResponse(
            index_id=i.index_id,
            scope_ref=i.scope_ref,
            collection_contract_ref=i.collection_contract_ref,
            partition_key=i.partition_key,
            isolated_collection=i.isolated_collection,
        )
        for i in indices
    ]


@router.post("/scope-memory", response_model=ScopeMemoryResponse, status_code=201)
async def create_scope_memory(
    scope_type: ScopeType,
    scope_id: str,
    collection_contract_ref: str = "collection://mica_lab_rag_chunks_v1",
    identity: dict = Depends(_identity_or_fallback),
):
    """Register a scope memory index (creates partition key automatically).

    Doc T1.2: ``scope_partition_key = scope_type:scope_id``
    ``Toda query privada debe incluir scope_partition_key.``
    """
    index_id = f"smi_{len(_scope_memory_store) + 1}"
    scope_ref = build_scope_ref(scope_type, scope_id)
    partition_key = ScopeMemoryIndex.build_partition_key(scope_type, scope_id)

    smi = ScopeMemoryIndex(
        index_id=index_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        collection_contract_ref=collection_contract_ref,
        partition_key=partition_key,
    )
    _scope_memory_store[index_id] = smi
    logger.info("ScopeMemoryIndex created: %s (partition_key=%s)", scope_ref, partition_key)
    return ScopeMemoryResponse(
        index_id=smi.index_id,
        scope_ref=smi.scope_ref,
        collection_contract_ref=smi.collection_contract_ref,
        partition_key=smi.partition_key,
        isolated_collection=smi.isolated_collection,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Policy endpoints  (T1.13)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/policies", response_model=list[PolicyResponse])
async def list_policies(
    scope_ref: Optional[str] = Query(None, description="Filter by scope"),
    identity: dict = Depends(_identity_or_fallback),
):
    """List tenant policies."""
    policies = list(_policy_store.values())
    if scope_ref:
        policies = [p for p in policies if p.scope_ref == scope_ref]
    return [
        PolicyResponse(
            policy_ref=p.policy_ref,
            policy_scope=p.policy_scope,
            scope_ref=p.scope_ref,
            version=p.version,
            active=p.active,
            rule_count=len(p.rules),
        )
        for p in policies
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Health / debug
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/health")
async def tenancy_health():
    """Tenancy module health check. Reports store sizes."""
    return {
        "status": "operational",
        "stores": {
            "role_assignments": len(_role_store),
            "shares": len(_share_store),
            "policies": len(_policy_store),
            "scope_memory_indices": len(_scope_memory_store),
        },
        "linea_roja": "No role assignment = no access",
        "mantra": "Tenancy is identity plus policy plus physical isolation",
    }
