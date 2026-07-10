from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)


_POOL = None
_LINE_STATUSES = ("proposed", "active", "paused", "archived")
_SPACE_RELATIONS = ("primary_domain", "related_domain", "supports", "depends_on")


class ResearchLineServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def derive_research_line_slug(display_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(display_name or "").strip().lower()).strip("-")
    return normalized or "research-line"


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL

    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise ResearchLineServiceError("database_not_configured", "ResearchLine service requires a configured Neon database URL.")

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


def _row_to_research_line(row: Any, *, study_count: int = 0, space_count: int = 0) -> Dict[str, Any]:
    return {
        "line_id": str(row["line_id"]),
        "lab_id": str(row["lab_id"]),
        "owner_user_id": row["owner_user_id"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "description": row["description"],
        "primary_question": row["primary_question"],
        "status": row["status"],
        "metadata": _json_dict(row["metadata"]),
        "archived": bool(row["archived"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "study_count": int(study_count),
        "space_count": int(space_count),
    }


def _row_to_space_link(row: Any) -> Dict[str, Any]:
    return {
        "link_id": str(row["link_id"]),
        "line_id": str(row["line_id"]),
        "space_id": str(row["space_id"]),
        "relation_type": row["relation_type"],
        "metadata": _json_dict(row["metadata"]),
        "created_by": row["created_by"],
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
        raise ResearchLineServiceError("laboratory_not_found", "Laboratory not found for this user.")
    return row


async def _assert_line_access(conn: Any, *, user_id: str, line_id: str) -> Any:
    row = await conn.fetchrow(
        """
        SELECT rl.*, l.owner_user_id AS laboratory_owner
        FROM research_lines rl
        JOIN laboratories l ON l.lab_id = rl.lab_id
        LEFT JOIN lab_memberships m
          ON m.lab_id = rl.lab_id
         AND m.principal_ref = $1
         AND m.status = 'active'
        WHERE rl.line_id = $2
          AND (l.owner_user_id = $1 OR m.principal_ref = $1)
        """,
        user_id,
        line_id,
    )
    if not row:
        raise ResearchLineServiceError("research_line_not_found", "ResearchLine not found for this user.")
    return row


async def _assert_line_owner(conn: Any, *, user_id: str, line_id: str) -> Any:
    row = await _assert_line_access(conn, user_id=user_id, line_id=line_id)
    if row["owner_user_id"] != user_id:
        raise ResearchLineServiceError("research_line_admin_required", "Only the ResearchLine owner can mutate this resource in this phase.")
    return row


async def create_research_line(
    *,
    actor_user_id: str,
    lab_id: str,
    display_name: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    primary_question: Optional[str] = None,
    status: str = "proposed",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if status not in _LINE_STATUSES:
        raise ResearchLineServiceError("research_line_status_invalid", "Unsupported ResearchLine status.")
    await ensure_product_schema()
    pool = await _get_pool()
    resolved_slug = derive_research_line_slug(slug or display_name)
    async with pool.acquire() as conn:
        access = await _assert_lab_access(conn, user_id=actor_user_id, lab_id=lab_id)
        if access["owner_user_id"] != actor_user_id:
            raise ResearchLineServiceError("laboratory_admin_required", "Only the laboratory owner can create ResearchLines in this phase.")
        row = await conn.fetchrow(
            """
            INSERT INTO research_lines (
                lab_id, owner_user_id, slug, display_name, description,
                primary_question, status, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            RETURNING *
            """,
            lab_id,
            actor_user_id,
            resolved_slug,
            display_name,
            description,
            primary_question,
            status,
            json.dumps(metadata or {}),
        )
    return _row_to_research_line(row)


async def list_research_lines_for_user(
    *,
    user_id: str,
    lab_id: Optional[str] = None,
    archived: bool = False,
) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    conditions = [
        "rl.archived = $2",
        "(l.owner_user_id = $1 OR m.principal_ref = $1)",
    ]
    params: list[Any] = [user_id, archived]
    if lab_id:
        conditions.append(f"rl.lab_id = ${len(params) + 1}")
        params.append(lab_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                rl.*,
                COALESCE(st.study_count, 0) AS study_count,
                COALESCE(sl.space_count, 0) AS space_count
            FROM research_lines rl
            JOIN laboratories l ON l.lab_id = rl.lab_id
            LEFT JOIN lab_memberships m
              ON m.lab_id = rl.lab_id
             AND m.principal_ref = $1
             AND m.status = 'active'
            LEFT JOIN (
                SELECT research_line_id, COUNT(*) AS study_count
                FROM studies
                WHERE research_line_id IS NOT NULL
                GROUP BY research_line_id
            ) st ON st.research_line_id = rl.line_id
            LEFT JOIN (
                SELECT line_id, COUNT(*) AS space_count
                FROM research_line_space_links
                GROUP BY line_id
            ) sl ON sl.line_id = rl.line_id
            WHERE {' AND '.join(conditions)}
            ORDER BY rl.updated_at DESC
            """,
            *params,
        )
    return [
        _row_to_research_line(
            row,
            study_count=row["study_count"],
            space_count=row["space_count"],
        )
        for row in rows
    ]


async def get_research_line_for_user(*, user_id: str, line_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await _assert_line_access(conn, user_id=user_id, line_id=line_id)
        study_count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS count FROM studies WHERE research_line_id = $1",
            line_id,
        )
        space_count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS count FROM research_line_space_links WHERE line_id = $1",
            line_id,
        )
    return _row_to_research_line(
        row,
        study_count=study_count_row["count"] if study_count_row else 0,
        space_count=space_count_row["count"] if space_count_row else 0,
    )


async def update_research_line_for_user(
    *,
    actor_user_id: str,
    line_id: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    primary_question: Optional[str] = None,
    status: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if status is not None and status not in _LINE_STATUSES:
        raise ResearchLineServiceError("research_line_status_invalid", "Unsupported ResearchLine status.")
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_line_owner(conn, user_id=actor_user_id, line_id=line_id)
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
        if primary_question is not None:
            updates.append(f"primary_question = ${idx}")
            params.append(primary_question)
            idx += 1
        if status is not None:
            updates.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if metadata is not None:
            updates.append(f"metadata = ${idx}::jsonb")
            params.append(json.dumps(metadata))
            idx += 1
        if not updates:
            raise ResearchLineServiceError("research_line_noop", "No ResearchLine fields to update.")
        updates.append("updated_at = now()")
        params.extend([line_id, actor_user_id])
        row = await conn.fetchrow(
            f"""
            UPDATE research_lines
            SET {', '.join(updates)}
            WHERE line_id = ${idx}
              AND owner_user_id = ${idx + 1}
            RETURNING *
            """,
            *params,
        )
        study_count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS count FROM studies WHERE research_line_id = $1",
            line_id,
        )
        space_count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS count FROM research_line_space_links WHERE line_id = $1",
            line_id,
        )
    return _row_to_research_line(
        row,
        study_count=study_count_row["count"] if study_count_row else 0,
        space_count=space_count_row["count"] if space_count_row else 0,
    )


async def archive_research_line_for_user(*, actor_user_id: str, line_id: str) -> None:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_line_owner(conn, user_id=actor_user_id, line_id=line_id)
        await conn.execute(
            """
            UPDATE research_lines
            SET archived = true, updated_at = now()
            WHERE line_id = $1
            """,
            line_id,
        )


async def create_research_line_space_link(
    *,
    actor_user_id: str,
    line_id: str,
    space_id: str,
    relation_type: str = "related_domain",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if relation_type not in _SPACE_RELATIONS:
        raise ResearchLineServiceError("research_line_space_relation_invalid", "Unsupported ResearchLine/KnowledgeSpace relation_type.")
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        line = await _assert_line_owner(conn, user_id=actor_user_id, line_id=line_id)
        space = await conn.fetchrow(
            """
            SELECT space_id, lab_id
            FROM knowledge_spaces
            WHERE space_id = $1
              AND archived = false
            """,
            space_id,
        )
        if not space:
            raise ResearchLineServiceError("knowledge_space_not_found", "KnowledgeSpace not found for this ResearchLine link.")
        if str(space["lab_id"]) != str(line["lab_id"]):
            raise ResearchLineServiceError("research_line_space_cross_lab_blocked", "ResearchLine can only link KnowledgeSpaces inside the same laboratory in this phase.")
        row = await conn.fetchrow(
            """
            INSERT INTO research_line_space_links (line_id, space_id, relation_type, metadata, created_by)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (line_id, space_id, relation_type)
            DO UPDATE SET metadata = EXCLUDED.metadata
            RETURNING *
            """,
            line_id,
            space_id,
            relation_type,
            json.dumps(metadata or {}),
            actor_user_id,
        )
    return _row_to_space_link(row)


async def list_research_line_space_links_for_user(*, user_id: str, line_id: str) -> List[Dict[str, Any]]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _assert_line_access(conn, user_id=user_id, line_id=line_id)
        rows = await conn.fetch(
            """
            SELECT *
            FROM research_line_space_links
            WHERE line_id = $1
            ORDER BY created_at ASC
            """,
            line_id,
        )
    return [_row_to_space_link(row) for row in rows]


async def link_study_to_research_line_for_user(*, actor_user_id: str, line_id: str, study_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        line = await _assert_line_owner(conn, user_id=actor_user_id, line_id=line_id)
        study = await conn.fetchrow(
            """
            SELECT study_id, user_id
            FROM studies
            WHERE study_id = $1
              AND user_id = $2
              AND archived = false
            """,
            study_id,
            actor_user_id,
        )
        if not study:
            raise ResearchLineServiceError("study_not_found", "Study not found for this ResearchLine link.")
        row = await conn.fetchrow(
            """
            UPDATE studies
            SET research_line_id = $1, lab_id = $2, updated_at = now()
            WHERE study_id = $3
            RETURNING study_id, lab_id, research_line_id, updated_at
            """,
            line_id,
            line["lab_id"],
            study_id,
        )
    return {
        "study_id": str(row["study_id"]),
        "lab_id": str(row["lab_id"]) if row["lab_id"] else None,
        "research_line_id": str(row["research_line_id"]) if row["research_line_id"] else None,
        "updated_at": row["updated_at"].isoformat(),
    }
