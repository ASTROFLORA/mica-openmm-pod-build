from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)
from mica.tenancy.models import CanonicalRole


class LaboratoryServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_POOL = None


def derive_lab_slug(display_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(display_name or "").strip().lower()).strip("-")
    return normalized or "lab"


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL

    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise LaboratoryServiceError("database_not_configured", "Laboratory service requires a configured Neon database URL.")

    _POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    return _POOL


def _row_to_lab(row: Any, membership_role: Optional[str] = None) -> Dict[str, Any]:
    return {
        "lab_id": str(row["lab_id"]),
        "owner_user_id": row["owner_user_id"],
        "org_ref": row["org_ref"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "description": row["description"],
        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}"),
        "archived": bool(row["archived"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "membership_role": membership_role,
    }


def _row_to_membership(row: Any) -> Dict[str, Any]:
    return {
        "membership_id": str(row["membership_id"]),
        "lab_id": str(row["lab_id"]),
        "principal_ref": row["principal_ref"],
        "role": row["role"],
        "status": row["status"],
        "invited_by": row["invited_by"],
        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}"),
        "joined_at": row["joined_at"].isoformat(),
    }


async def create_laboratory(
    *,
    owner_user_id: str,
    display_name: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    org_ref: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    resolved_slug = derive_lab_slug(slug or display_name)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO laboratories (owner_user_id, org_ref, slug, display_name, description, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING lab_id, owner_user_id, org_ref, slug, display_name, description, metadata, created_at, updated_at, archived
                """,
                owner_user_id,
                org_ref,
                resolved_slug,
                display_name,
                description,
                json.dumps(metadata or {}),
            )
            await conn.execute(
                """
                INSERT INTO lab_memberships (lab_id, principal_ref, role, status, invited_by, metadata)
                VALUES ($1, $2, $3, 'active', $2, '{}'::jsonb)
                ON CONFLICT (lab_id, principal_ref) DO NOTHING
                """,
                row["lab_id"],
                owner_user_id,
                CanonicalRole.LAB_ADMIN.value,
            )

    return _row_to_lab(row, CanonicalRole.LAB_ADMIN.value)


async def list_laboratories_for_user(*, user_id: str, archived: bool = False) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.*, m.role AS membership_role
            FROM laboratories l
            LEFT JOIN lab_memberships m
              ON m.lab_id = l.lab_id
             AND m.principal_ref = $1
             AND m.status = 'active'
            WHERE l.archived = $2
              AND (l.owner_user_id = $1 OR m.principal_ref = $1)
            ORDER BY l.updated_at DESC
            """,
            user_id,
            archived,
        )
    return [_row_to_lab(row, row["membership_role"]) for row in rows]


async def get_laboratory_for_user(*, user_id: str, lab_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT l.*, m.role AS membership_role
            FROM laboratories l
            LEFT JOIN lab_memberships m
              ON m.lab_id = l.lab_id
             AND m.principal_ref = $1
             AND m.status = 'active'
            WHERE l.lab_id = $2
              AND (l.owner_user_id = $1 OR m.principal_ref = $1)
            """,
            user_id,
            lab_id,
        )
    if not row:
        raise LaboratoryServiceError("laboratory_not_found", "Laboratory not found for this user.")
    return _row_to_lab(row, row["membership_role"])


async def create_lab_membership(
    *,
    actor_user_id: str,
    lab_id: str,
    principal_ref: str,
    role: CanonicalRole,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchrow(
            "SELECT lab_id FROM laboratories WHERE lab_id = $1 AND owner_user_id = $2 AND archived = false",
            lab_id,
            actor_user_id,
        )
        if not owner:
            raise LaboratoryServiceError("laboratory_admin_required", "Only the laboratory owner can add members in this phase.")

        row = await conn.fetchrow(
            """
            INSERT INTO lab_memberships (lab_id, principal_ref, role, status, invited_by, metadata)
            VALUES ($1, $2, $3, 'active', $4, $5::jsonb)
            ON CONFLICT (lab_id, principal_ref)
            DO UPDATE SET role = EXCLUDED.role, status = 'active', invited_by = EXCLUDED.invited_by, metadata = EXCLUDED.metadata
            RETURNING membership_id, lab_id, principal_ref, role, status, invited_by, metadata, joined_at
            """,
            lab_id,
            principal_ref,
            role.value,
            actor_user_id,
            json.dumps(metadata or {}),
        )
    return _row_to_membership(row)


async def list_lab_members_for_user(*, user_id: str, lab_id: str) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        access = await conn.fetchrow(
            """
            SELECT l.lab_id
            FROM laboratories l
            LEFT JOIN lab_memberships m
              ON m.lab_id = l.lab_id
             AND m.principal_ref = $1
             AND m.status = 'active'
            WHERE l.lab_id = $2
              AND (l.owner_user_id = $1 OR m.principal_ref = $1)
            """,
            user_id,
            lab_id,
        )
        if not access:
            raise LaboratoryServiceError("laboratory_not_found", "Laboratory not found for this user.")

        rows = await conn.fetch(
            """
            SELECT membership_id, lab_id, principal_ref, role, status, invited_by, metadata, joined_at
            FROM lab_memberships
            WHERE lab_id = $1
            ORDER BY joined_at ASC
            """,
            lab_id,
        )
    return [_row_to_membership(row) for row in rows]
