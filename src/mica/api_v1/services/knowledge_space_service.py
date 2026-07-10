from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)


_POOL = None
_CONTAINMENT_RELATIONS = ("contains", "specializes", "subset_of")
_ALL_RELATIONS = _CONTAINMENT_RELATIONS + ("related_domain", "contributes_to")
_EXPANSION_POLICIES = ("no_expand", "expand_forward", "expand_reverse", "expand_bidirectional")


class KnowledgeSpaceServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def derive_space_slug(display_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(display_name or "").strip().lower()).strip("-")
    return normalized or "knowledge-space"


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL

    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise KnowledgeSpaceServiceError("database_not_configured", "KnowledgeSpace service requires a configured Neon database URL.")

    _POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    return _POOL


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return json.loads(value or "{}")


def _row_to_space(row: Any) -> Dict[str, Any]:
    return {
        "space_id": str(row["space_id"]),
        "lab_id": str(row["lab_id"]),
        "owner_user_id": row["owner_user_id"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "description": row["description"],
        "primary_parent_space_id": str(row["primary_parent_space_id"]) if row["primary_parent_space_id"] else None,
        "review_cadence": row["review_cadence"],
        "health_status": row["health_status"],
        "metadata": _json_dict(row["metadata"]),
        "archived": bool(row["archived"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _row_to_membership(row: Any) -> Dict[str, Any]:
    return {
        "membership_id": str(row["membership_id"]),
        "parent_space_id": str(row["parent_space_id"]),
        "child_space_id": str(row["child_space_id"]) if row["child_space_id"] else None,
        "member_kb_ref": row["member_kb_ref"],
        "relation_type": row["relation_type"],
        "expansion_policy": row["expansion_policy"],
        "primary_parent": bool(row["primary_parent"]),
        "metadata": _json_dict(row["metadata"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(),
        "archived": bool(row["archived"]),
    }


def _row_to_snapshot(row: Any) -> Dict[str, Any]:
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "space_id": str(row["space_id"]),
        "captured_by": row["captured_by"],
        "snapshot_data": _json_dict(row["snapshot_data"]),
        "created_at": row["created_at"].isoformat(),
    }


async def _assert_lab_access(conn: Any, *, user_id: str, lab_id: str) -> Any:
    row = await conn.fetchrow(
        """
        SELECT l.lab_id, l.owner_user_id, m.role AS membership_role
        FROM laboratories l
        LEFT JOIN lab_memberships m
          ON m.lab_id = l.lab_id
         AND m.principal_ref = $1
         AND m.status = 'active'
        WHERE l.lab_id = $2
          AND l.archived = false
          AND (l.owner_user_id = $1 OR m.principal_ref = $1)
        """,
        user_id,
        lab_id,
    )
    if not row:
        raise KnowledgeSpaceServiceError("laboratory_not_found", "Laboratory not found for this user.")
    return row


async def _assert_space_access(conn: Any, *, user_id: str, space_id: str) -> Any:
    row = await conn.fetchrow(
        """
        SELECT ks.*, l.owner_user_id AS laboratory_owner
        FROM knowledge_spaces ks
        JOIN laboratories l ON l.lab_id = ks.lab_id
        LEFT JOIN lab_memberships m
          ON m.lab_id = ks.lab_id
         AND m.principal_ref = $1
         AND m.status = 'active'
        WHERE ks.space_id = $2
          AND (l.owner_user_id = $1 OR m.principal_ref = $1)
        """,
        user_id,
        space_id,
    )
    if not row:
        raise KnowledgeSpaceServiceError("knowledge_space_not_found", "KnowledgeSpace not found for this user.")
    return row


async def _assert_space_owner(conn: Any, *, user_id: str, space_id: str) -> Any:
    row = await _assert_space_access(conn, user_id=user_id, space_id=space_id)
    if row["owner_user_id"] != user_id:
        raise KnowledgeSpaceServiceError("knowledge_space_admin_required", "Only the KnowledgeSpace owner can mutate this resource in this phase.")
    return row


async def _validate_primary_parent(
    conn: Any,
    *,
    lab_id: str,
    primary_parent_space_id: Optional[str],
) -> None:
    if not primary_parent_space_id:
        return
    parent = await conn.fetchrow(
        """
        SELECT space_id
        FROM knowledge_spaces
        WHERE space_id = $1
          AND lab_id = $2
          AND archived = false
        """,
        primary_parent_space_id,
        lab_id,
    )
    if not parent:
        raise KnowledgeSpaceServiceError("primary_parent_not_found", "Primary parent KnowledgeSpace not found inside the same laboratory.")


async def _ensure_no_cycle(
    conn: Any,
    *,
    parent_space_id: str,
    child_space_id: str,
    relation_type: str,
) -> None:
    if relation_type not in _CONTAINMENT_RELATIONS:
        return
    if parent_space_id == child_space_id:
        raise KnowledgeSpaceServiceError("knowledge_membership_cycle", "A KnowledgeSpace cannot contain itself.")
    row = await conn.fetchrow(
        """
        WITH RECURSIVE descendants(space_id) AS (
            SELECT child_space_id
            FROM knowledge_memberships
            WHERE parent_space_id = $1
              AND child_space_id IS NOT NULL
              AND archived = false
              AND relation_type = ANY($2::text[])
            UNION
            SELECT km.child_space_id
            FROM knowledge_memberships km
            JOIN descendants d ON km.parent_space_id = d.space_id
            WHERE km.child_space_id IS NOT NULL
              AND km.archived = false
              AND km.relation_type = ANY($2::text[])
        )
        SELECT 1
        FROM descendants
        WHERE space_id = $3
        LIMIT 1
        """,
        child_space_id,
        list(_CONTAINMENT_RELATIONS),
        parent_space_id,
    )
    if row:
        raise KnowledgeSpaceServiceError("knowledge_membership_cycle", "This membership would create a containment cycle.")


async def create_knowledge_space(
    *,
    actor_user_id: str,
    lab_id: str,
    display_name: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    primary_parent_space_id: Optional[str] = None,
    review_cadence: Optional[str] = None,
    health_status: str = "active",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    resolved_slug = derive_space_slug(slug or display_name)
    async with pool.acquire() as conn:
        access = await _assert_lab_access(conn, user_id=actor_user_id, lab_id=lab_id)
        if access["owner_user_id"] != actor_user_id:
            raise KnowledgeSpaceServiceError("laboratory_admin_required", "Only the laboratory owner can create KnowledgeSpaces in this phase.")
        await _validate_primary_parent(conn, lab_id=lab_id, primary_parent_space_id=primary_parent_space_id)
        row = await conn.fetchrow(
            """
            INSERT INTO knowledge_spaces (
                lab_id, owner_user_id, slug, display_name, description,
                primary_parent_space_id, review_cadence, health_status, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            RETURNING *
            """,
            lab_id,
            actor_user_id,
            resolved_slug,
            display_name,
            description,
            primary_parent_space_id,
            review_cadence,
            health_status,
            json.dumps(metadata or {}),
        )
    return _row_to_space(row)


async def list_knowledge_spaces_for_user(
    *,
    user_id: str,
    lab_id: Optional[str] = None,
    archived: bool = False,
) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    conditions = [
        "ks.archived = $2",
        "(l.owner_user_id = $1 OR m.principal_ref = $1)",
    ]
    params: list[Any] = [user_id, archived]
    if lab_id:
        conditions.append(f"ks.lab_id = ${len(params) + 1}")
        params.append(lab_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT ks.*
            FROM knowledge_spaces ks
            JOIN laboratories l ON l.lab_id = ks.lab_id
            LEFT JOIN lab_memberships m
              ON m.lab_id = ks.lab_id
             AND m.principal_ref = $1
             AND m.status = 'active'
            WHERE {' AND '.join(conditions)}
            ORDER BY ks.updated_at DESC
            """,
            *params,
        )
    return [_row_to_space(row) for row in rows]


async def get_knowledge_space_for_user(*, user_id: str, space_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await _assert_space_access(conn, user_id=user_id, space_id=space_id)
    return _row_to_space(row)


async def update_knowledge_space_for_user(
    *,
    actor_user_id: str,
    space_id: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    primary_parent_space_id: Optional[str] = None,
    review_cadence: Optional[str] = None,
    health_status: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await _assert_space_owner(conn, user_id=actor_user_id, space_id=space_id)
        await _validate_primary_parent(
            conn,
            lab_id=str(row["lab_id"]),
            primary_parent_space_id=primary_parent_space_id,
        )
        updates = []
        params: list[Any] = []
        idx = 1
        if display_name is not None:
            updates.append(f"display_name = ${idx}")
            params.append(display_name)
            idx += 1
        if description is not None:
            updates.append(f"description = ${idx}")
            params.append(description)
            idx += 1
        if primary_parent_space_id is not None:
            updates.append(f"primary_parent_space_id = ${idx}")
            params.append(primary_parent_space_id)
            idx += 1
        if review_cadence is not None:
            updates.append(f"review_cadence = ${idx}")
            params.append(review_cadence)
            idx += 1
        if health_status is not None:
            updates.append(f"health_status = ${idx}")
            params.append(health_status)
            idx += 1
        if metadata is not None:
            updates.append(f"metadata = ${idx}::jsonb")
            params.append(json.dumps(metadata))
            idx += 1
        if not updates:
            raise KnowledgeSpaceServiceError("knowledge_space_noop", "No KnowledgeSpace fields to update.")
        updates.append("updated_at = now()")
        params.extend([space_id, actor_user_id])
        updated = await conn.fetchrow(
            f"""
            UPDATE knowledge_spaces
            SET {', '.join(updates)}
            WHERE space_id = ${idx}
              AND owner_user_id = ${idx + 1}
            RETURNING *
            """,
            *params,
        )
    return _row_to_space(updated)


async def archive_knowledge_space_for_user(*, actor_user_id: str, space_id: str) -> None:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_space_owner(conn, user_id=actor_user_id, space_id=space_id)
        await conn.execute(
            """
            UPDATE knowledge_spaces
            SET archived = true, updated_at = now()
            WHERE space_id = $1
            """,
            space_id,
        )


async def create_knowledge_membership(
    *,
    actor_user_id: str,
    parent_space_id: str,
    child_space_id: Optional[str] = None,
    member_kb_ref: Optional[str] = None,
    relation_type: str = "contains",
    expansion_policy: str = "no_expand",
    primary_parent: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if relation_type not in _ALL_RELATIONS:
        raise KnowledgeSpaceServiceError("knowledge_membership_relation_invalid", "Unsupported KnowledgeMembership relation_type.")
    if expansion_policy not in _EXPANSION_POLICIES:
        raise KnowledgeSpaceServiceError("knowledge_membership_expansion_invalid", "Unsupported KnowledgeMembership expansion_policy.")
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        parent = await _assert_space_owner(conn, user_id=actor_user_id, space_id=parent_space_id)
        if child_space_id:
            child = await conn.fetchrow(
                """
                SELECT space_id, lab_id
                FROM knowledge_spaces
                WHERE space_id = $1
                  AND archived = false
                """,
                child_space_id,
            )
            if not child:
                raise KnowledgeSpaceServiceError("knowledge_membership_child_not_found", "Child KnowledgeSpace not found.")
            if str(child["lab_id"]) != str(parent["lab_id"]):
                raise KnowledgeSpaceServiceError("knowledge_membership_cross_lab_blocked", "KnowledgeSpace containment across laboratories is blocked in this phase.")
            await _ensure_no_cycle(
                conn,
                parent_space_id=parent_space_id,
                child_space_id=child_space_id,
                relation_type=relation_type,
            )
        row = await conn.fetchrow(
            """
            INSERT INTO knowledge_memberships (
                parent_space_id, child_space_id, member_kb_ref, relation_type,
                expansion_policy, primary_parent, metadata, created_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
            RETURNING *
            """,
            parent_space_id,
            child_space_id,
            member_kb_ref,
            relation_type,
            expansion_policy,
            primary_parent,
            json.dumps(metadata or {}),
            actor_user_id,
        )
    return _row_to_membership(row)


async def list_knowledge_memberships_for_user(*, user_id: str, space_id: str) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_space_access(conn, user_id=user_id, space_id=space_id)
        rows = await conn.fetch(
            """
            SELECT *
            FROM knowledge_memberships
            WHERE parent_space_id = $1
              AND archived = false
            ORDER BY created_at ASC
            """,
            space_id,
        )
    return [_row_to_membership(row) for row in rows]


async def capture_membership_snapshot(*, actor_user_id: str, space_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_space_owner(conn, user_id=actor_user_id, space_id=space_id)
        memberships = await conn.fetch(
            """
            SELECT *
            FROM knowledge_memberships
            WHERE parent_space_id = $1
              AND archived = false
            ORDER BY created_at ASC
            """,
            space_id,
        )
        payload = {
            "space_id": space_id,
            "memberships": [_row_to_membership(row) for row in memberships],
            "relation_types": list(_ALL_RELATIONS),
        }
        row = await conn.fetchrow(
            """
            INSERT INTO membership_snapshots (space_id, captured_by, snapshot_data)
            VALUES ($1, $2, $3::jsonb)
            RETURNING *
            """,
            space_id,
            actor_user_id,
            json.dumps(payload),
        )
    return _row_to_snapshot(row)


async def list_membership_snapshots_for_user(*, user_id: str, space_id: str) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_space_access(conn, user_id=user_id, space_id=space_id)
        rows = await conn.fetch(
            """
            SELECT *
            FROM membership_snapshots
            WHERE space_id = $1
            ORDER BY created_at DESC
            """,
            space_id,
        )
    return [_row_to_snapshot(row) for row in rows]


async def get_membership_snapshot_for_user(*, user_id: str, space_id: str, snapshot_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_space_access(conn, user_id=user_id, space_id=space_id)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM membership_snapshots
            WHERE snapshot_id = $1
              AND space_id = $2
            """,
            snapshot_id,
            space_id,
        )
    if not row:
        raise KnowledgeSpaceServiceError("membership_snapshot_not_found", "MembershipSnapshot not found for this KnowledgeSpace.")
    return _row_to_snapshot(row)
