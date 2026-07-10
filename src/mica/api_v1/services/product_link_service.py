from __future__ import annotations

import json
from typing import Any, Dict, Optional

from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)


class ProductLinkServiceError(Exception):
    def __init__(self, code: str, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class ProductLinkNotFoundError(ProductLinkServiceError):
    pass


_POOL = None


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL

    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise ProductLinkServiceError(
            "database_not_configured",
            "Product link service requires a configured Neon database URL.",
        )

    _POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    return _POOL


async def attach_artifact_to_study_for_user(*, user_id: str, study_id: str, artifact_id: str) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()

    async with pool.acquire() as conn:
        study = await conn.fetchrow(
            "SELECT study_id FROM studies WHERE study_id = $1 AND user_id = $2",
            study_id,
            user_id,
        )
        if not study:
            raise ProductLinkNotFoundError(
                "study_not_found",
                "Study not found for artifact attachment.",
                details={"study_id": study_id},
            )

        artifact = await conn.fetchrow(
            "SELECT artifact_id FROM artifacts WHERE artifact_id = $1 AND user_id = $2",
            artifact_id,
            user_id,
        )
        if not artifact:
            raise ProductLinkNotFoundError(
                "artifact_not_found",
                "Artifact not found for study attachment.",
                details={"artifact_id": artifact_id},
            )

        await conn.execute(
            "INSERT INTO study_artifacts (study_id, artifact_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            study_id,
            artifact_id,
        )

    return {
        "study_id": study_id,
        "artifact_id": artifact_id,
        "status": "linked",
    }


async def attach_artifact_to_working_set_for_user(
    *,
    user_id: str,
    working_set_id: str,
    artifact_id: str,
    position: int = 0,
    config: Optional[Dict[str, Any]] = None,
    artifact_ref_type: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_product_schema()
    pool = await _get_pool()

    async with pool.acquire() as conn:
        working_set = await conn.fetchrow(
            "SELECT working_set_id FROM working_sets WHERE working_set_id = $1 AND user_id = $2",
            working_set_id,
            user_id,
        )
        if not working_set:
            raise ProductLinkNotFoundError(
                "working_set_not_found",
                "Working set not found for artifact attachment.",
                details={"working_set_id": working_set_id},
            )

        artifact = await conn.fetchrow(
            "SELECT artifact_id, artifact_type FROM artifacts WHERE artifact_id = $1 AND user_id = $2",
            artifact_id,
            user_id,
        )
        if not artifact:
            raise ProductLinkNotFoundError(
                "artifact_not_found",
                "Artifact not found for working set attachment.",
                details={"artifact_id": artifact_id},
            )

        resolved_ref_type = str(artifact_ref_type or artifact["artifact_type"] or "artifact").strip()
        if not resolved_ref_type:
            raise ProductLinkServiceError(
                "artifact_ref_type_missing",
                "Artifact reference type could not be resolved for working set attachment.",
                details={"artifact_id": artifact_id},
            )

        await conn.execute(
            """
            INSERT INTO working_set_items (
                working_set_id,
                artifact_ref_type,
                artifact_ref_id,
                position,
                config
            )
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            working_set_id,
            resolved_ref_type,
            artifact_id,
            int(position),
            json.dumps(config or {}),
        )

    return {
        "working_set_id": working_set_id,
        "artifact_id": artifact_id,
        "artifact_ref_type": resolved_ref_type,
        "position": int(position),
        "status": "item_added",
    }
