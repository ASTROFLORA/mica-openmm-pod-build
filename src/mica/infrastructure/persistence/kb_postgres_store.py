"""kb_postgres_store.py — Durable PostgreSQL store for Knowledge Fabric.

Implements the async store interface expected by
:class:`~mica.pipelines.knowledge_fabric.kb_service.KBService`:

    save_kb / load_kb / list_kbs
    save_run / load_run / list_runs

Uses :class:`AsyncPGStoreBase` for pool lifecycle and DSN selection,
following the same pattern as :class:`TimescaleJobStore`.

Tables ``knowledge_bases`` and ``knowledge_runs`` are created automatically
on :meth:`initialize` (idempotent ``CREATE TABLE IF NOT EXISTS``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .pg_async import AsyncPGStoreBase, PoolConfig

logger = logging.getLogger(__name__)

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in ("prod", "production")

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_KNOWLEDGE_BASES = """\
CREATE TABLE IF NOT EXISTS knowledge_bases (
    kb_id           TEXT PRIMARY KEY,
    owner_scope     TEXT        NOT NULL DEFAULT 'user',
    owner_id        TEXT        NOT NULL DEFAULT '',
    workspace_id    TEXT        NOT NULL DEFAULT '',
    name            TEXT        NOT NULL DEFAULT '',
    kb_type         TEXT        NOT NULL DEFAULT 'query',
    canonical_query TEXT        NOT NULL DEFAULT '',
    target_entities JSONB       NOT NULL DEFAULT '[]',
    target_topics   JSONB       NOT NULL DEFAULT '[]',
    status          TEXT        NOT NULL DEFAULT 'building',
    storage_manifest_uri TEXT   NOT NULL DEFAULT '',
    graph_namespace TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    policies        JSONB       NOT NULL DEFAULT '{}'
);
"""

_DDL_KNOWLEDGE_RUNS = """\
CREATE TABLE IF NOT EXISTS knowledge_runs (
    run_id              TEXT PRIMARY KEY,
    kb_id               TEXT        NOT NULL,
    run_type            TEXT        NOT NULL DEFAULT 'kb.build.query',
    query               TEXT        NOT NULL DEFAULT '',
    entity_focus        JSONB       NOT NULL DEFAULT '[]',
    topic_focus         JSONB       NOT NULL DEFAULT '[]',
    status              TEXT        NOT NULL DEFAULT 'pending',
    job_manifest_uri    TEXT        NOT NULL DEFAULT '',
    artifact_manifest_uri TEXT     NOT NULL DEFAULT '',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    degradation_flags   JSONB       NOT NULL DEFAULT '[]',
    metadata            JSONB       NOT NULL DEFAULT '{}'
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_kb_owner ON knowledge_bases (owner_id);",
    "CREATE INDEX IF NOT EXISTS idx_kb_workspace ON knowledge_bases (workspace_id);",
    "CREATE INDEX IF NOT EXISTS idx_kb_status ON knowledge_bases (status);",
    # Global/public KB queries use owner_scope = 'global' — index prevents full table scan.
    "CREATE INDEX IF NOT EXISTS idx_kb_scope ON knowledge_bases (owner_scope);",
    "CREATE INDEX IF NOT EXISTS idx_runs_kb ON knowledge_runs (kb_id);",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON knowledge_runs (status);",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_or_none(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(val))


def _ensure_list(val: Any) -> list:
    if isinstance(val, str):
        return json.loads(val)
    if isinstance(val, list):
        return val
    return []


def _ensure_dict(val: Any) -> dict:
    if isinstance(val, str):
        return json.loads(val)
    if isinstance(val, dict):
        return val
    return {}


def _coerce_enum_value(raw: Any) -> str:
    return str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def _coerce_owner_scope(raw: Any) -> str:
    value = _coerce_enum_value(raw)
    aliases = {
        "lab": "team",
        "public": "global",
    }
    return aliases.get(value, value or "user")


def _coerce_kb_type(raw: Any) -> str:
    value = _coerce_enum_value(raw)
    aliases = {
        "report": "report_derived",
    }
    return aliases.get(value, value or "query")


def _coerce_kb_status(raw: Any) -> str:
    value = _coerce_enum_value(raw)
    aliases = {
        "ready": "active",
        "completed": "active",
        "complete": "active",
    }
    return aliases.get(value, value or "building")


# ---------------------------------------------------------------------------
# Input size limits (DoS mitigation)
# ---------------------------------------------------------------------------

_MAX_LIST_ITEMS = 1_000
_MAX_TEXT_LEN = 100_000
_MAX_JSON_BYTES = 1_000_000  # 1 MB


def _validate_save_inputs(d: Dict[str, Any]) -> None:
    """Reject obviously oversized payloads before hitting the database."""
    for key in ("target_entities", "target_topics", "entity_focus", "topic_focus", "degradation_flags"):
        val = d.get(key)
        if isinstance(val, list) and len(val) > _MAX_LIST_ITEMS:
            raise ValueError(f"{key} exceeds max {_MAX_LIST_ITEMS} items (got {len(val)})")
    for key in ("canonical_query", "name", "query"):
        val = d.get(key, "")
        if isinstance(val, str) and len(val) > _MAX_TEXT_LEN:
            raise ValueError(f"{key} exceeds max {_MAX_TEXT_LEN} chars (got {len(val)})")
    for key in ("policies", "metadata"):
        val = d.get(key)
        if val is not None:
            serialized = json.dumps(val) if not isinstance(val, str) else val
            if len(serialized) > _MAX_JSON_BYTES:
                raise ValueError(f"{key} exceeds max {_MAX_JSON_BYTES} bytes")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class KBPostgresStore(AsyncPGStoreBase):
    """Durable PostgreSQL-backed store for KnowledgeBase and KnowledgeRun.

    Usage::

        store = KBPostgresStore()          # picks DSN from env
        await store.initialize()           # creates pool + tables
        kb_svc = KBService(store=store)    # wire into service
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        *,
        pool_config: Optional[PoolConfig] = None,
    ) -> None:
        super().__init__(
            database_url,
            prefer_timescale=False,  # KB tables can live on Neon or Timescale
            role="kb",
            pool_config=pool_config or PoolConfig(min_size=1, max_size=5, command_timeout=30),
        )

    async def initialize(self) -> None:
        """Create connection pool and ensure KB tables exist."""
        await super().initialize()
        async with self.pool.acquire() as conn:
            await conn.execute(_DDL_KNOWLEDGE_BASES)
            await conn.execute(_DDL_KNOWLEDGE_RUNS)
            for idx_ddl in _DDL_INDEXES:
                await conn.execute(idx_ddl)
        logger.info("KBPostgresStore initialized — tables ensured")

    # ------------------------------------------------------------------
    # KnowledgeBase CRUD
    # ------------------------------------------------------------------

    async def save_kb(self, kb: Any) -> None:
        """Upsert a KnowledgeBase (dataclass with .to_dict())."""
        d = kb.to_dict() if hasattr(kb, "to_dict") else kb
        _validate_save_inputs(d)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO knowledge_bases (
                    kb_id, owner_scope, owner_id, workspace_id, name,
                    kb_type, canonical_query, target_entities, target_topics,
                    status, storage_manifest_uri, graph_namespace,
                    created_at, updated_at, policies
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8::jsonb, $9::jsonb,
                    $10, $11, $12,
                    $13, $14, $15::jsonb
                )
                ON CONFLICT (kb_id) DO UPDATE SET
                    owner_scope          = EXCLUDED.owner_scope,
                    owner_id             = EXCLUDED.owner_id,
                    workspace_id         = EXCLUDED.workspace_id,
                    name                 = EXCLUDED.name,
                    kb_type              = EXCLUDED.kb_type,
                    canonical_query      = EXCLUDED.canonical_query,
                    target_entities      = EXCLUDED.target_entities,
                    target_topics        = EXCLUDED.target_topics,
                    status               = EXCLUDED.status,
                    storage_manifest_uri = EXCLUDED.storage_manifest_uri,
                    graph_namespace      = EXCLUDED.graph_namespace,
                    updated_at           = EXCLUDED.updated_at,
                    policies             = EXCLUDED.policies
                """,
                d["kb_id"],
                d.get("owner_scope", "user"),
                d.get("owner_id", ""),
                d.get("workspace_id", ""),
                d.get("name", ""),
                d.get("kb_type", "query"),
                d.get("canonical_query", ""),
                json.dumps(d.get("target_entities", [])),
                json.dumps(d.get("target_topics", [])),
                d.get("status", "building"),
                d.get("storage_manifest_uri", ""),
                d.get("graph_namespace", ""),
                _parse_dt(d.get("created_at")) or datetime.now(timezone.utc),
                _parse_dt(d.get("updated_at")) or datetime.now(timezone.utc),
                json.dumps(d.get("policies", {})),
            )

    async def load_kb(self, kb_id: str, *, owner_id: str = "") -> Optional[Any]:
        """Load a KnowledgeBase by ID.

        When *owner_id* is provided the query matches on ``owner_id`` OR
        grants access when the KB has ``owner_scope = 'global'`` (public KBs
        are readable by any authenticated caller).

        In production, ``owner_id`` is **required** for non-global KBs.
        Global KBs are returned regardless of ``owner_id``.
        """
        if not owner_id and _PROD_ENV:
            raise ValueError(
                f"load_kb requires owner_id in production (kb_id={kb_id})"
            )
        async with self.pool.acquire() as conn:
            if owner_id:
                # Returns the KB if owned by this user OR if it is global/public.
                row = await conn.fetchrow(
                    "SELECT * FROM knowledge_bases WHERE kb_id = $1 AND (owner_id = $2 OR owner_scope = 'global')",
                    kb_id, owner_id,
                )
            else:
                logger.warning(
                    "load_kb called WITHOUT owner_id — "
                    "no tenant isolation for kb_id=%s", kb_id,
                )
                row = await conn.fetchrow(
                    "SELECT * FROM knowledge_bases WHERE kb_id = $1",
                    kb_id,
                )
        if row is None:
            logger.info("kb.load miss kb_id=%s owner=%s", kb_id, owner_id or "<none>")
            return None
        logger.info(
            "kb.load hit kb_id=%s owner=%s scope=%s",
            kb_id, owner_id or "<none>", dict(row).get("owner_scope", "?")
        )
        try:
            return self._row_to_kb(dict(row))
        except Exception as exc:
            logger.warning(
                "kb.load malformed kb row skipped kb_id=%s owner=%s error=%s",
                kb_id,
                owner_id or "<none>",
                exc,
            )
            return None

    async def list_kbs(
        self,
        owner_id: str = "",
        workspace_id: str = "",
        include_global: bool = False,
    ) -> List[Any]:
        """List KBs with optional filters.

        In production, at least one of *owner_id*, *workspace_id*, or
        *include_global* must be supplied to prevent unrestricted full-table
        scans.

        When *include_global* is ``True``, global/public KBs
        (``owner_scope = 'global'``) are included in the results alongside
        any user- or workspace-scoped KBs that match the other filters.
        Passing ``include_global=True`` alone (no owner/workspace) is valid
        and returns all public KBs without requiring an ``owner_id``.
        """
        if not owner_id and not workspace_id and not include_global and _PROD_ENV:
            raise ValueError(
                "list_kbs requires owner_id, workspace_id, or include_global=True in production"
            )
        tenant_clauses = []
        params: list[Any] = []
        idx = 1
        if owner_id:
            tenant_clauses.append(f"owner_id = ${idx}")
            params.append(owner_id)
            idx += 1
        if workspace_id:
            tenant_clauses.append(f"workspace_id = ${idx}")
            params.append(workspace_id)
            idx += 1

        if include_global:
            global_clause = "owner_scope = 'global'"
            if tenant_clauses:
                # User/workspace KBs OR global KBs.
                where = " WHERE (" + " AND ".join(tenant_clauses) + f") OR {global_clause}"
            else:
                where = f" WHERE {global_clause}"
        else:
            where = (" WHERE " + " AND ".join(tenant_clauses)) if tenant_clauses else ""

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM knowledge_bases{where} ORDER BY created_at DESC",
                *params,
            )
        logger.info(
            "kb.list count=%d owner=%s ws=%s include_global=%s",
            len(rows), owner_id or "<none>", workspace_id or "<none>", include_global,
        )
        decoded: List[Any] = []
        skipped = 0
        for row in rows:
            try:
                decoded.append(self._row_to_kb(dict(row)))
            except Exception as exc:
                skipped += 1
                row_dict = dict(row)
                logger.warning(
                    "kb.list skipped malformed kb row kb_id=%s scope=%s type=%s status=%s error=%s",
                    row_dict.get("kb_id", "<unknown>"),
                    row_dict.get("owner_scope", "<unknown>"),
                    row_dict.get("kb_type", "<unknown>"),
                    row_dict.get("status", "<unknown>"),
                    exc,
                )
        if skipped:
            logger.warning(
                "kb.list returned %d rows after skipping %d malformed rows owner=%s ws=%s include_global=%s",
                len(decoded),
                skipped,
                owner_id or "<none>",
                workspace_id or "<none>",
                include_global,
            )
        return decoded

    # ------------------------------------------------------------------
    # KnowledgeRun CRUD
    # ------------------------------------------------------------------

    async def save_run(self, run: Any) -> None:
        """Upsert a KnowledgeRun.

        I09 gap closure: ``checkpoint_ids`` and ``step_cursors`` live as
        top-level fields on the dataclass but are not separate DB columns.
        We pack them into the ``metadata`` JSONB blob under reserved keys
        ``_checkpoint_ids`` and ``_step_cursors`` so they survive a
        round-trip through the durable store without a schema migration.
        """
        d = run.to_dict() if hasattr(run, "to_dict") else run
        # Pack I09 checkpoint fields into metadata
        meta = dict(d.get("metadata") or {})
        meta["_checkpoint_ids"] = list(d.get("checkpoint_ids") or [])
        meta["_step_cursors"] = dict(d.get("step_cursors") or {})
        d = dict(d)
        d["metadata"] = meta
        _validate_save_inputs(d)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO knowledge_runs (
                    run_id, kb_id, run_type, query,
                    entity_focus, topic_focus, status,
                    job_manifest_uri, artifact_manifest_uri,
                    started_at, completed_at,
                    degradation_flags, metadata
                ) VALUES (
                    $1, $2, $3, $4,
                    $5::jsonb, $6::jsonb, $7,
                    $8, $9,
                    $10, $11,
                    $12::jsonb, $13::jsonb
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    kb_id                 = EXCLUDED.kb_id,
                    run_type              = EXCLUDED.run_type,
                    query                 = EXCLUDED.query,
                    entity_focus          = EXCLUDED.entity_focus,
                    topic_focus           = EXCLUDED.topic_focus,
                    status                = EXCLUDED.status,
                    job_manifest_uri      = EXCLUDED.job_manifest_uri,
                    artifact_manifest_uri = EXCLUDED.artifact_manifest_uri,
                    started_at            = EXCLUDED.started_at,
                    completed_at          = EXCLUDED.completed_at,
                    degradation_flags     = EXCLUDED.degradation_flags,
                    metadata              = EXCLUDED.metadata
                """,
                d["run_id"],
                d.get("kb_id", ""),
                d.get("run_type", "kb.build.query"),
                d.get("query", ""),
                json.dumps(d.get("entity_focus", [])),
                json.dumps(d.get("topic_focus", [])),
                d.get("status", "pending"),
                d.get("job_manifest_uri", ""),
                d.get("artifact_manifest_uri", ""),
                _parse_dt(d.get("started_at")),
                _parse_dt(d.get("completed_at")),
                json.dumps(d.get("degradation_flags", [])),
                json.dumps(d.get("metadata", {})),
            )

    async def load_run(self, run_id: str, *, owner_id: str = "") -> Optional[Any]:
        """Load a KnowledgeRun by ID.

        When *owner_id* is provided, the run is looked up via a JOIN to
        ``knowledge_bases`` to verify that the KB owner matches.  Runs
        belonging to global KBs (``owner_scope = 'global'``) are visible
        to any caller regardless of ``owner_id``.

        In production, ``owner_id`` is **required**.
        """
        if not owner_id and _PROD_ENV:
            raise ValueError(
                f"load_run requires owner_id in production (run_id={run_id})"
            )
        async with self.pool.acquire() as conn:
            if owner_id:
                # Allow access if the run belongs to this user's KB OR a global KB.
                row = await conn.fetchrow(
                    "SELECT r.* FROM knowledge_runs r "
                    "JOIN knowledge_bases kb ON r.kb_id = kb.kb_id "
                    "WHERE r.run_id = $1 AND (kb.owner_id = $2 OR kb.owner_scope = 'global')",
                    run_id, owner_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM knowledge_runs WHERE run_id = $1",
                    run_id,
                )
        if row is None:
            return None
        return self._row_to_run(dict(row))

    async def list_runs(self, kb_id: str, *, owner_id: str = "") -> List[Any]:
        """List all runs for a given KB, optionally verifying ownership.

        Runs on global KBs are visible to any caller regardless of
        ``owner_id``.  In production, ``owner_id`` is still required so the
        caller is identified, but the ownership check is bypassed for global
        KBs.
        """
        if not owner_id and _PROD_ENV:
            raise ValueError(
                f"list_runs requires owner_id in production (kb_id={kb_id})"
            )
        async with self.pool.acquire() as conn:
            if owner_id:
                rows = await conn.fetch(
                    "SELECT r.* FROM knowledge_runs r "
                    "JOIN knowledge_bases kb ON r.kb_id = kb.kb_id "
                    "WHERE r.kb_id = $1 AND (kb.owner_id = $2 OR kb.owner_scope = 'global') "
                    "ORDER BY r.started_at DESC NULLS LAST",
                    kb_id, owner_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM knowledge_runs WHERE kb_id = $1 ORDER BY started_at DESC NULLS LAST",
                    kb_id,
                )
        return [self._row_to_run(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Row → domain object mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_kb(row: Dict[str, Any]) -> Any:
        """Reconstruct a KnowledgeBase dataclass from a DB row."""
        from mica.pipelines.knowledge_fabric.contracts import (
            KBStatus,
            KBType,
            KnowledgeBase,
            OwnerScope,
        )

        return KnowledgeBase(
            kb_id=row["kb_id"],
            owner_scope=OwnerScope(_coerce_owner_scope(row.get("owner_scope", "user"))),
            owner_id=row.get("owner_id", ""),
            workspace_id=row.get("workspace_id", ""),
            name=row.get("name", ""),
            kb_type=KBType(_coerce_kb_type(row.get("kb_type", "query"))),
            canonical_query=row.get("canonical_query", ""),
            target_entities=_ensure_list(row.get("target_entities", [])),
            target_topics=_ensure_list(row.get("target_topics", [])),
            status=KBStatus(_coerce_kb_status(row.get("status", "building"))),
            storage_manifest_uri=row.get("storage_manifest_uri", ""),
            graph_namespace=row.get("graph_namespace", ""),
            created_at=_parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
            updated_at=_parse_dt(row.get("updated_at")) or datetime.now(timezone.utc),
            policies=_ensure_dict(row.get("policies", {})),
        )

    @staticmethod
    def _row_to_run(row: Dict[str, Any]) -> Any:
        """Reconstruct a KnowledgeRun dataclass from a DB row."""
        from mica.pipelines.knowledge_fabric.contracts import (
            JobKind,
            KnowledgeRun,
            RunStatus,
        )

        # I09 gap: unpack checkpoint_ids/step_cursors from metadata reservoir
        meta = _ensure_dict(row.get("metadata", {}))
        checkpoint_ids = _ensure_list(meta.pop("_checkpoint_ids", []))
        step_cursors = _ensure_dict(meta.pop("_step_cursors", {}))
        return KnowledgeRun(
            run_id=row["run_id"],
            kb_id=row.get("kb_id", ""),
            run_type=JobKind(row.get("run_type", "kb.build.query")),
            query=row.get("query", ""),
            entity_focus=_ensure_list(row.get("entity_focus", [])),
            topic_focus=_ensure_list(row.get("topic_focus", [])),
            status=RunStatus(row.get("status", "pending")),
            job_manifest_uri=row.get("job_manifest_uri", ""),
            artifact_manifest_uri=row.get("artifact_manifest_uri", ""),
            started_at=_parse_dt(row.get("started_at")),
            completed_at=_parse_dt(row.get("completed_at")),
            degradation_flags=_ensure_list(row.get("degradation_flags", [])),
            checkpoint_ids=checkpoint_ids,
            step_cursors=step_cursors,
            metadata=meta,
        )
