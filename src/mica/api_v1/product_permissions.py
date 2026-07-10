"""Permission enforcement helper for product-layer routers.

APV-02: prefer EffectivePermissionEngine for typed decisions. DB ACL helpers
remain for durable policy/entry rows and are bridged into the engine when used.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from mica.identity.request_identity import RequestIdentity
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)
from mica.tenancy.effective_permission_engine import (
    get_permission_engine,
    grant_from_acl_role,
    grant_owner,
)
from mica.tenancy.models import PermissionAction

logger = logging.getLogger(__name__)

_POOL = None


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise HTTPException(status_code=503, detail="Database not configured")
    _POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    return _POOL


def _user_id(identity: RequestIdentity | str) -> str:
    if isinstance(identity, RequestIdentity):
        return identity.user_id
    return str(identity)


def _target_ref(resource_type: str, resource_id: str) -> str:
    return f"{resource_type}://{resource_id}"


def _scope_ref_for_user(user_id: str) -> str:
    return f"user:{user_id}"


async def _get_policy(resource_type: str, resource_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT policy_id, owner_user_id, access_level FROM permission_policies "
            "WHERE resource_type = $1 AND resource_id = $2",
            resource_type,
            resource_id,
        )


async def _get_entry(policy_id: str, user_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT role FROM permission_entries WHERE policy_id = $1 AND user_id = $2",
            policy_id,
            user_id,
        )


async def _ensure_policy(resource_type: str, resource_id: str, owner_user_id: str):
    """Create a policy if none exists (for new resources)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO permission_policies (resource_type, resource_id, owner_user_id, access_level) "
            "VALUES ($1, $2, $3, 'owned') ON CONFLICT (resource_type, resource_id) DO NOTHING",
            resource_type,
            resource_id,
            owner_user_id,
        )
    get_permission_engine().upsert_grant(
        grant_owner(
            owner_user_id=owner_user_id,
            scope_ref=_scope_ref_for_user(owner_user_id),
            target_ref=_target_ref(resource_type, resource_id),
        )
    )


def _bridge_acl_into_engine(
    *,
    owner_user_id: str,
    resource_type: str,
    resource_id: str,
    entry_user_id: str | None = None,
    entry_role: str | None = None,
) -> None:
    engine = get_permission_engine()
    target = _target_ref(resource_type, resource_id)
    engine.upsert_grant(
        grant_owner(
            owner_user_id=owner_user_id,
            scope_ref=_scope_ref_for_user(owner_user_id),
            target_ref=target,
        )
    )
    if entry_user_id and entry_role:
        engine.upsert_grant(
            grant_from_acl_role(
                principal_user_id=entry_user_id,
                scope_ref=_scope_ref_for_user(owner_user_id),
                target_ref=target,
                role=entry_role,
                granted_by=owner_user_id,
            )
        )


async def require_owner(identity: RequestIdentity | str, resource_type: str, resource_id: str):
    """Raise 403 unless the user is the owner of the resource."""
    uid = _user_id(identity)
    policy = await _get_policy(resource_type, resource_id)
    if not policy:
        raise HTTPException(status_code=403, detail="Resource not found or not accessible")
    if policy["owner_user_id"] != uid:
        raise HTTPException(status_code=403, detail="Only the owner can perform this action")
    _bridge_acl_into_engine(
        owner_user_id=policy["owner_user_id"],
        resource_type=resource_type,
        resource_id=resource_id,
    )


async def require_view(identity: RequestIdentity | str, resource_type: str, resource_id: str):
    """Raise 403 unless the user can view the resource (owner or ACL entry)."""
    uid = _user_id(identity)
    policy = await _get_policy(resource_type, resource_id)
    if not policy:
        raise HTTPException(status_code=403, detail="Resource not found or not accessible")
    if policy["owner_user_id"] == uid:
        _bridge_acl_into_engine(
            owner_user_id=policy["owner_user_id"],
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return
    entry = await _get_entry(policy["policy_id"], uid)
    if not entry:
        raise HTTPException(status_code=403, detail="You do not have access to this resource")
    _bridge_acl_into_engine(
        owner_user_id=policy["owner_user_id"],
        resource_type=resource_type,
        resource_id=resource_id,
        entry_user_id=uid,
        entry_role=entry["role"],
    )
    decision = get_permission_engine().evaluate(
        context=uid,
        target_ref=_target_ref(resource_type, resource_id),
        action=PermissionAction.READ,
        hints={"active_scope_id": _scope_ref_for_user(policy["owner_user_id"])},
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="You do not have access to this resource")


async def require_edit(identity: RequestIdentity | str, resource_type: str, resource_id: str):
    """Raise 403 unless the user can edit the resource (owner or editor/admin ACL)."""
    uid = _user_id(identity)
    policy = await _get_policy(resource_type, resource_id)
    if not policy:
        raise HTTPException(status_code=403, detail="Resource not found or not accessible")
    if policy["owner_user_id"] == uid:
        _bridge_acl_into_engine(
            owner_user_id=policy["owner_user_id"],
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return
    entry = await _get_entry(policy["policy_id"], uid)
    if not entry or entry["role"] not in ("editor", "admin"):
        raise HTTPException(status_code=403, detail="You do not have edit access to this resource")
    _bridge_acl_into_engine(
        owner_user_id=policy["owner_user_id"],
        resource_type=resource_type,
        resource_id=resource_id,
        entry_user_id=uid,
        entry_role=entry["role"],
    )
    decision = get_permission_engine().evaluate(
        context=uid,
        target_ref=_target_ref(resource_type, resource_id),
        action=PermissionAction.UPDATE,
        hints={"active_scope_id": _scope_ref_for_user(policy["owner_user_id"])},
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="You do not have edit access to this resource")


async def can_view(identity: RequestIdentity | str, resource_type: str, resource_id: str) -> bool:
    try:
        await require_view(identity, resource_type, resource_id)
        return True
    except HTTPException:
        return False


async def can_edit(identity: RequestIdentity | str, resource_type: str, resource_id: str) -> bool:
    try:
        await require_edit(identity, resource_type, resource_id)
        return True
    except HTTPException:
        return False


async def share_resource(
    owner_identity: RequestIdentity | str,
    resource_type: str,
    resource_id: str,
    target_user_id: str,
    role: str = "viewer",
):
    """Share a resource with another user. Must be owner."""
    uid = _user_id(owner_identity)
    policy = await _get_policy(resource_type, resource_id)
    if not policy:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            p = await conn.fetchrow(
                "INSERT INTO permission_policies (resource_type, resource_id, owner_user_id, access_level) "
                "VALUES ($1, $2, $3, 'owned') "
                "ON CONFLICT (resource_type, resource_id) DO UPDATE SET access_level='owned' "
                "RETURNING policy_id",
                resource_type,
                resource_id,
                uid,
            )
            await conn.execute(
                "INSERT INTO permission_entries (policy_id, user_id, role, granted_by) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (policy_id, user_id) DO UPDATE SET role=$3",
                p["policy_id"],
                target_user_id,
                role,
                uid,
            )
        _bridge_acl_into_engine(
            owner_user_id=uid,
            resource_type=resource_type,
            resource_id=resource_id,
            entry_user_id=target_user_id,
            entry_role=role,
        )
        return {
            "shared": True,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "role": role,
        }

    if policy["owner_user_id"] != uid:
        raise HTTPException(status_code=403, detail="Only the owner can share this resource")

    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO permission_entries (policy_id, user_id, role, granted_by) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (policy_id, user_id) DO UPDATE SET role=$3",
            policy["policy_id"],
            target_user_id,
            role,
            uid,
        )
    _bridge_acl_into_engine(
        owner_user_id=uid,
        resource_type=resource_type,
        resource_id=resource_id,
        entry_user_id=target_user_id,
        entry_role=role,
    )
    return {
        "shared": True,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "role": role,
    }


async def revoke_access(
    owner_identity: RequestIdentity | str,
    resource_type: str,
    resource_id: str,
    target_user_id: str,
):
    """Revoke a user's access. Must be owner."""
    uid = _user_id(owner_identity)
    policy = await _get_policy(resource_type, resource_id)
    if not policy or policy["owner_user_id"] != uid:
        raise HTTPException(status_code=403, detail="Only the owner can revoke access")
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM permission_entries WHERE policy_id = $1 AND user_id = $2",
            policy["policy_id"],
            target_user_id,
        )
    engine = get_permission_engine()
    target = _target_ref(resource_type, resource_id)
    for grant in engine.store.list_for_principal(f"user://{target_user_id}"):
        if grant.target_ref == target and grant.source == "acl_entry":
            engine.store.revoke(grant.grant_id)
    return {"revoked": True}
