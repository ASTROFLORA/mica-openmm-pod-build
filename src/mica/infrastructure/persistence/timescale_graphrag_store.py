"""timescale_graphrag_store.py

Minimal GraphRAG persistence/retrieval for Timescale/Postgres.

This is an MVP store:
- Upsert nodes
- Insert edges/facts
- Hybrid retrieval (built-in Postgres FTS + pgvector cosine distance)
- Hop-1 traversal (edges touching seed nodes)

Design constraints:
- Must work even if `pg_textsearch` (BM25) is not installed.
- Uses the same DSN selection conventions as TimescaleEventStore.

Env:
- TIMESCALE_SERVICE_URL (preferred) / TIMESCALE_DSN / TIMESCALE_URL
- PGPASSWORD (when DSN omits password)
- GRAPHRAG_SCHEMA (default: public)
- GRAPHRAG_EMBEDDING_DIM (default: 1536)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from mica.graphrag.edge_confidence_model import assess_edge_confidence
from mica.graphrag.node2vec_coverage_gate import GraphNode2VecCoverageSnapshot
from mica.kb.predicate_registry import get_default_predicate_registry

from .pg_async import AsyncPGStoreBase, validate_ident
from .pg_types import jsonb

_logger = logging.getLogger(__name__)

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in ("prod", "production")
_GRAPH_RECORD_STATUSES = {"proposed", "active", "deprecated", "retracted", "superseded", "review_required"}
_RECEIPT_REQUIRED_STATUSES = {"active", "deprecated", "retracted", "superseded"}
_POLICY_SCOPES = {"global", "org", "lab", "study"}
_CONTRADICTION_KINDS = {
    "opposite_direction",
    "quantitative_conflict",
    "failed_replication",
    "context_overlap_conflict",
}
_CONTRADICTION_STATUSES = {
    "contradiction_open",
    "resolved",
    "explained_by_context",
    "false_positive",
    "retracted",
}
_TRAVERSAL_POLICIES = {"interactive", "background", "impact_frontier"}
_TRAVERSAL_POLICY_EDGE_CAPS = {
    "interactive": 50,
    "background": 200,
    "impact_frontier": 100,
}
_TRAVERSAL_POLICY_LATENCY_BUDGET_MS = {
    "interactive": 750,
    "background": 5000,
    "impact_frontier": 1500,
}


_GRAPHRAG_SCHEMA_BOOTSTRAP_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.atom_graph_nodes (
    node_id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    node_type TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    description TEXT,
    embedding vector({embedding_dim}),
    external_ids JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    source_doi TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (canonical_name, node_type)
);

CREATE TABLE IF NOT EXISTS {schema}.atom_graph_edges (
    edge_id BIGSERIAL PRIMARY KEY,
    source_node TEXT NOT NULL,
    source_type TEXT NOT NULL,
    target_node TEXT NOT NULL,
    target_type TEXT NOT NULL,
    relationship TEXT NOT NULL,
    details TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    source_doi TEXT,
    source_sentence TEXT,
    extraction_method TEXT,
    embedding vector({embedding_dim}),
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    user_id TEXT,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {schema}.atom_facts (
    fact_id BIGSERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    topic TEXT,
    entities TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    source_doi TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    embedding vector({embedding_dim}),
    user_id TEXT,
    session_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_atom_graph_edges_scope ON {schema}.atom_graph_edges (user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_atom_graph_facts_scope ON {schema}.atom_facts (user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_atom_graph_nodes_lookup ON {schema}.atom_graph_nodes (canonical_name, node_type);
CREATE INDEX IF NOT EXISTS idx_atom_graph_edges_relationship ON {schema}.atom_graph_edges (relationship);
CREATE INDEX IF NOT EXISTS idx_atom_graph_facts_fact_type ON {schema}.atom_facts (fact_type);
"""


@dataclass(frozen=True)
class GraphEdgeRow:
    source_node: str
    relationship: str
    target_node: str
    details: Optional[str]
    hybrid_score: float
    source_type: Optional[str] = None
    target_type: Optional[str] = None
    confidence: Optional[float] = None
    source_doi: Optional[str] = None
    source_sentence: Optional[str] = None
    extraction_method: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    text_score: Optional[float] = None
    vector_score: Optional[float] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class GraphFactRow:
    content: str
    fact_type: str
    topic: Optional[str]
    entities: list[str]
    hybrid_score: float
    source_doi: Optional[str] = None
    confidence: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None
    text_score: Optional[float] = None
    vector_score: Optional[float] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class GraphSearchHit:
    result_type: str
    hybrid_score: float
    edge: Optional[GraphEdgeRow] = None
    fact: Optional[GraphFactRow] = None


@dataclass(frozen=True)
class GraphTraversalCostEvent:
    traversal_request_ref: str
    budget_ref: str
    policy: str
    visited_nodes: int
    visited_edges: int
    returned_paths: int
    cost_units: int
    status: str
    latency_budget_ms: int


@dataclass(frozen=True)
class BudgetedGraphTraversalResult:
    edges: list[GraphEdgeRow]
    traversal_request_ref: str
    budget_ref: str
    policy: str
    status: str
    cost_event: GraphTraversalCostEvent


@dataclass(frozen=True)
class FactTableCapabilities:
    has_session_id: bool = True
    has_metadata: bool = True

    @property
    def is_legacy_compatible(self) -> bool:
        return not (self.has_session_id and self.has_metadata)

    def missing_columns(self) -> list[str]:
        missing: list[str] = []
        if not self.has_session_id:
            missing.append("session_id")
        if not self.has_metadata:
            missing.append("metadata")
        return missing


class TimescaleGraphRAGStore:
    def __init__(
        self,
        database_url: Optional[str] = None,
        *,
        schema: Optional[str] = None,
        embedding_dim: Optional[int] = None,
        pool: Any = None,
    ) -> None:
        self._base = AsyncPGStoreBase(database_url, role="timescale")
        self._fact_table_capabilities: Optional[FactTableCapabilities] = None

        # Default to a dedicated schema to avoid collisions with earlier PoCs in `public`.
        self.schema = validate_ident(schema or os.getenv("GRAPHRAG_SCHEMA") or "graphrag")

        raw_dim = embedding_dim or int(os.getenv("GRAPHRAG_EMBEDDING_DIM") or "1536")
        if raw_dim <= 0 or raw_dim > 8192:
            raise ValueError(f"invalid embedding_dim={raw_dim}")
        self.embedding_dim = raw_dim
        if pool is not None:
            self._base._pool = pool

    @property
    def _pool(self):
        return self._base._pool

    async def initialize(self) -> None:
        await self._base.initialize()
        async with self._pool.acquire() as conn:
            await conn.execute(
                _GRAPHRAG_SCHEMA_BOOTSTRAP_SQL.format(
                    schema=self.schema,
                    embedding_dim=self.embedding_dim,
                )
            )

    # ------------------------------------------------------------------
    # Index maintenance
    # ------------------------------------------------------------------

    async def ensure_global_indexes(self) -> None:
        """Create partial indexes for global-scope queries if not present.

        These indexes accelerate ``_global_expr``-based WHERE clauses used
        in ``search_edges_hybrid``, ``hop1_edges``, and ``_update_matching_edge``.
        Both statements use ``IF NOT EXISTS`` and are therefore idempotent.

        .. warning::
            ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction block.
            This method opens a dedicated connection and executes each statement
            outside of any transaction.  Call from startup or a maintenance
            endpoint — NOT inside an ongoing transaction context.

        Tables targeted:
        - ``{schema}.atom_graph_edges``  → ``idx_age_global_scope``
        - ``{schema}.atom_facts``        → ``idx_af_global_scope``
        """
        stmts = [
            (
                "idx_age_global_scope",
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_age_global_scope"
                f" ON {self.schema}.atom_graph_edges"
                f" ((metadata->'scope'->>'global'))"
                f" WHERE (metadata->'scope'->>'global') = 'true'",
            ),
            (
                "idx_af_global_scope",
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_af_global_scope"
                f" ON {self.schema}.atom_facts"
                f" ((metadata->'scope'->>'global'))"
                f" WHERE (metadata->'scope'->>'global') = 'true'",
            ),
        ]
        if not self._pool:
            await self.initialize()

        # Acquire a raw connection and execute outside any transaction block.
        async with self._pool.acquire() as conn:
            for idx_name, sql in stmts:
                try:
                    await conn.execute(sql)
                    _logger.info("GraphRAG index ensured: %s", idx_name)
                except Exception as exc:  # pragma: no cover
                    # Log and continue — the table may not exist yet (first deploy).
                    _logger.warning(
                        "ensure_global_indexes: %s skipped — %s", idx_name, exc
                    )

    async def close(self) -> None:
        await self._base.close()

    @asynccontextmanager
    async def _acquire_conn(self, conn: Any = None):
        if conn is not None:
            yield conn
            return
        if not self._pool:
            await self.initialize()
        async with self._pool.acquire() as pooled_conn:
            yield pooled_conn

    @staticmethod
    def _global_expr(alias: str) -> str:
        return (
            "CASE WHEN lower(coalesce("
            f"{alias}.metadata->'scope'->>'global', {alias}.metadata->>'global', 'false'"
            ")) IN ('true','t','1','yes') THEN TRUE ELSE FALSE END"
        )

    @staticmethod
    def _workspace_expr(alias: str) -> str:
        return f"coalesce({alias}.metadata->'scope'->>'workspace_id', {alias}.metadata->>'workspace_id')"

    @staticmethod
    def _policy_scope_expr(alias: str) -> str:
        normalized_scope = f"lower(coalesce({alias}.metadata->>'policy_scope', ''))"
        return (
            "CASE "
            f"WHEN {normalized_scope} IN ('global','org','lab','study') THEN {normalized_scope} "
            f"WHEN {TimescaleGraphRAGStore._global_expr(alias)} THEN 'global' "
            f"WHEN {TimescaleGraphRAGStore._workspace_expr(alias)} IS NOT NULL THEN 'study' "
            "ELSE 'lab' END"
        )

    @staticmethod
    def _allowed_policy_scopes(
        *,
        workspace_id: Optional[str],
        global_only: bool,
    ) -> list[str]:
        if global_only:
            return ["global"]
        if workspace_id is not None:
            return ["global"]
        return ["lab", "org", "global"]

    @staticmethod
    def _normalize_metadata(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        return dict(metadata)

    @staticmethod
    def _claim_key(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _fact_claim_payload(
        *,
        content: str,
        fact_type: str,
        topic: Optional[str],
        entities: list[str],
        source_doi: Optional[str],
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "content": content,
            "fact_type": fact_type,
            "topic": topic,
            "entities": list(entities),
            "source_doi": source_doi,
            "scope": dict(scope),
        }

    @staticmethod
    def _edge_claim_payload(
        *,
        source_node: str,
        source_type: str,
        target_node: str,
        target_type: str,
        relationship: str,
        source_doi: Optional[str],
        details: Optional[str],
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "source_node": source_node,
            "source_type": source_type,
            "target_node": target_node,
            "target_type": target_type,
            "relationship": relationship,
            "source_doi": source_doi,
            "scope": dict(scope),
        }

    @staticmethod
    def _graph_ref(prefix: str, payload: dict[str, Any]) -> str:
        return f"{prefix}://{TimescaleGraphRAGStore._claim_key(payload)[:24]}"

    @staticmethod
    def _normalize_traversal_policy(policy: Optional[str]) -> str:
        normalized = str(policy or "interactive").strip().lower() or "interactive"
        if normalized not in _TRAVERSAL_POLICIES:
            raise ValueError(f"invalid traversal policy={normalized}")
        return normalized

    @staticmethod
    def _normalize_traversal_budget(
        *,
        seed_nodes: list[str],
        limit: int,
        policy: Optional[str],
        budget_ref: Optional[str],
    ) -> dict[str, Any]:
        normalized_policy = TimescaleGraphRAGStore._normalize_traversal_policy(policy)
        requested_limit = max(1, int(limit))
        policy_edge_cap = _TRAVERSAL_POLICY_EDGE_CAPS[normalized_policy]
        effective_limit = min(requested_limit, policy_edge_cap)
        normalized_budget_ref = str(budget_ref or f"budget://graphrag/{normalized_policy}").strip()
        traversal_payload = {
            "seed_nodes": list(seed_nodes),
            "requested_limit": requested_limit,
            "effective_limit": effective_limit,
            "policy": normalized_policy,
            "budget_ref": normalized_budget_ref,
        }
        return {
            "policy": normalized_policy,
            "budget_ref": normalized_budget_ref,
            "requested_limit": requested_limit,
            "effective_limit": effective_limit,
            "latency_budget_ms": _TRAVERSAL_POLICY_LATENCY_BUDGET_MS[normalized_policy],
            "traversal_request_ref": TimescaleGraphRAGStore._graph_ref("traversal_req", traversal_payload),
        }

    def _scope_metadata(
        self,
        *,
        metadata: Optional[dict[str, Any]],
        user_id: Optional[str],
        session_id: Optional[str],
        workspace_id: Optional[str] = None,
        is_global: Optional[bool] = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_metadata(metadata)
        scope = normalized.get("scope") if isinstance(normalized.get("scope"), dict) else {}
        if user_id is not None:
            scope["user_id"] = user_id
        if session_id is not None:
            scope["session_id"] = session_id
        if workspace_id is not None:
            scope["workspace_id"] = workspace_id
        if is_global is not None:
            scope["global"] = bool(is_global)
        elif "global" not in scope:
            scope["global"] = False
        normalized["scope"] = scope
        return normalized

    @staticmethod
    def _derive_policy_scope(metadata: dict[str, Any]) -> str:
        policy_scope = str(metadata.get("policy_scope") or "").strip().lower()
        if policy_scope in _POLICY_SCOPES:
            return policy_scope
        scope = metadata.get("scope") if isinstance(metadata.get("scope"), dict) else {}
        if bool(scope.get("global")):
            return "global"
        if scope.get("workspace_id"):
            return "study"
        return "lab"

    @staticmethod
    def _normalize_record_status(
        *,
        metadata: dict[str, Any],
        status_key: str,
        operation: str,
    ) -> dict[str, Any]:
        normalized = dict(metadata)
        receipt_ref = str(normalized.get("created_by_receipt_ref") or "").strip()
        status = str(normalized.get(status_key) or "").strip().lower()
        if not status:
            status = "active" if receipt_ref else "proposed"
        if status not in _GRAPH_RECORD_STATUSES:
            raise ValueError(f"{operation} invalid {status_key}={status}")
        if status in _RECEIPT_REQUIRED_STATUSES and not receipt_ref:
            raise ValueError(f"{operation} requires created_by_receipt_ref for {status_key}={status}")
        normalized[status_key] = status
        normalized["policy_scope"] = TimescaleGraphRAGStore._derive_policy_scope(normalized)
        if not receipt_ref and status == "proposed":
            normalized.setdefault("review_required", True)
        return normalized

    @staticmethod
    def _normalize_edge_confidence_metadata(
        *,
        metadata: dict[str, Any],
        relationship: str,
        confidence: float,
    ) -> dict[str, Any]:
        normalized = dict(metadata)
        registry = get_default_predicate_registry()
        registry_entry = registry.resolve(normalized.get("edge_kind") or relationship or "generic")
        edge_kind = registry_entry.edge_kind
        assessment = assess_edge_confidence(
            edge_kind=edge_kind,
            confidence=confidence,
            metadata=normalized,
        )

        normalized["edge_kind"] = edge_kind
        normalized["predicate_id"] = registry_entry.predicate_id
        normalized["predicate_registry_version"] = registry_entry.registry_version
        normalized["predicate_registry_status"] = registry_entry.registry_status
        normalized["biolink_predicate_curie"] = registry_entry.biolink_predicate_curie
        normalized["confidence_model_ref"] = assessment.profile.profile_ref
        normalized["confidence_model_version_ref"] = assessment.profile.model_ref
        normalized["confidence_threshold"] = assessment.profile.threshold
        normalized["confidence_factor_weights"] = dict(assessment.profile.factors)
        normalized["decay_policy_ref"] = assessment.profile.decay_policy_ref
        normalized["confidence_state"] = assessment.confidence_state
        normalized["freshness_factor"] = assessment.freshness_factor
        return normalized

    @staticmethod
    def _normalize_contradiction_metadata(
        *,
        metadata: dict[str, Any],
        claim_ref: str,
        supporting_edge_refs: list[str],
        contradicting_edge_refs: list[str],
        contradiction_kind: str,
        status: str,
    ) -> dict[str, Any]:
        normalized = dict(metadata)
        normalized_kind = str(contradiction_kind or "").strip().lower()
        normalized_status = str(status or "").strip().lower()
        if normalized_kind not in _CONTRADICTION_KINDS:
            raise ValueError(f"upsert_contradiction_record invalid contradiction_kind={normalized_kind}")
        if normalized_status not in _CONTRADICTION_STATUSES:
            raise ValueError(f"upsert_contradiction_record invalid status={normalized_status}")
        if not claim_ref:
            raise ValueError("upsert_contradiction_record requires claim_ref")
        if not supporting_edge_refs:
            raise ValueError("upsert_contradiction_record requires supporting_edge_refs")
        if not contradicting_edge_refs:
            raise ValueError("upsert_contradiction_record requires contradicting_edge_refs")

        normalized["claim_ref"] = claim_ref
        normalized["supporting_edge_refs"] = list(supporting_edge_refs)
        normalized["contradicting_edge_refs"] = list(contradicting_edge_refs)
        normalized["contradiction_kind"] = normalized_kind
        normalized["status"] = normalized_status
        contradiction_payload = {
            "claim_ref": claim_ref,
            "supporting_edge_refs": list(supporting_edge_refs),
            "contradicting_edge_refs": list(contradicting_edge_refs),
            "contradiction_kind": normalized_kind,
            "scope": normalized.get("scope") or {},
        }
        normalized["contradiction_ref"] = TimescaleGraphRAGStore._graph_ref("contradiction", contradiction_payload)
        return normalized

    @staticmethod
    def _active_status_expr(alias: str, status_key: str) -> str:
        return f"coalesce({alias}.metadata->>'{status_key}', 'active') = 'active'"

    @staticmethod
    def _column_name_from_row(row: Any) -> Optional[str]:
        if isinstance(row, dict):
            value = row.get("column_name")
            return str(value) if value is not None else None
        try:
            value = row["column_name"]
            return str(value) if value is not None else None
        except Exception:
            pass
        try:
            value = row[0]
            return str(value) if value is not None else None
        except Exception:
            return None

    async def get_fact_table_capabilities(self, *, conn: Any = None) -> FactTableCapabilities:
        if self._fact_table_capabilities is not None:
            return self._fact_table_capabilities

        async with self._acquire_conn(conn) as active_conn:
            override = getattr(active_conn, "fact_table_capabilities", None)
            if isinstance(override, FactTableCapabilities):
                self._fact_table_capabilities = override
                return override
            if isinstance(override, dict):
                detected = FactTableCapabilities(
                    has_session_id=bool(override.get("has_session_id", True)),
                    has_metadata=bool(override.get("has_metadata", True)),
                )
                self._fact_table_capabilities = detected
                return detected

            fetch = getattr(active_conn, "fetch", None)
            if not callable(fetch):
                detected = FactTableCapabilities()
                self._fact_table_capabilities = detected
                return detected

            try:
                rows = await active_conn.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = $1 AND table_name = 'atom_facts'
                    ORDER BY ordinal_position
                    """,
                    self.schema,
                )
            except Exception:
                detected = FactTableCapabilities()
                self._fact_table_capabilities = detected
                return detected

        columns = {
            column_name
            for column_name in (self._column_name_from_row(row) for row in rows)
            if column_name is not None
        }
        if not columns:
            detected = FactTableCapabilities()
        else:
            detected = FactTableCapabilities(
                has_session_id="session_id" in columns,
                has_metadata="metadata" in columns,
            )
        self._fact_table_capabilities = detected
        return detected

    @staticmethod
    def _compatibility_metadata(
        metadata: Optional[dict[str, Any]],
        *,
        capabilities: FactTableCapabilities,
        ignored_scope_filters: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        normalized = TimescaleGraphRAGStore._normalize_metadata(metadata)
        if capabilities.is_legacy_compatible:
            compatibility = normalized.get("compatibility") if isinstance(normalized.get("compatibility"), dict) else {}
            compatibility["atom_facts_schema"] = "legacy"
            compatibility["missing_columns"] = capabilities.missing_columns()
            if ignored_scope_filters:
                compatibility["ignored_scope_filters"] = list(ignored_scope_filters)
            normalized["compatibility"] = compatibility
        elif ignored_scope_filters:
            compatibility = normalized.get("compatibility") if isinstance(normalized.get("compatibility"), dict) else {}
            compatibility["ignored_scope_filters"] = list(ignored_scope_filters)
            normalized["compatibility"] = compatibility
        return normalized or None

    async def upsert_node(
        self,
        *,
        canonical_name: str,
        node_type: str,
        aliases: Optional[list[str]] = None,
        description: Optional[str] = None,
        embedding: Optional[str] = None,
        external_ids: Optional[dict[str, Any]] = None,
        properties: Optional[dict[str, Any]] = None,
        source_doi: Optional[list[str]] = None,
        conn: Any = None,
    ) -> None:
        aliases = aliases or []
        external_ids_json = jsonb(external_ids)
        properties_json = jsonb(properties)
        source_doi = source_doi or []

        async with self._acquire_conn(conn) as active_conn:
            await active_conn.execute(
                f"""
                INSERT INTO {self.schema}.atom_graph_nodes(
                    canonical_name, node_type, aliases, description, embedding,
                    external_ids, properties, source_doi, updated_at
                )
                VALUES ($1,$2,$3,$4,$5::vector,$6::jsonb,$7::jsonb,$8,NOW())
                ON CONFLICT (canonical_name, node_type)
                DO UPDATE SET
                    aliases = EXCLUDED.aliases,
                    description = EXCLUDED.description,
                    embedding = EXCLUDED.embedding,
                    external_ids = EXCLUDED.external_ids,
                    properties = EXCLUDED.properties,
                    source_doi = EXCLUDED.source_doi,
                    updated_at = NOW();
                """,
                canonical_name,
                node_type,
                aliases,
                description,
                embedding,
                external_ids_json,
                properties_json,
                source_doi,
            )

    async def get_node_id(self, *, canonical_name: str, node_type: str, conn: Any = None) -> Optional[Any]:
        async with self._acquire_conn(conn) as active_conn:
            row = await active_conn.fetchrow(
                f"""
                SELECT node_id
                FROM {self.schema}.atom_graph_nodes
                WHERE canonical_name = $1 AND node_type = $2
                LIMIT 1
                """,
                canonical_name,
                node_type,
            )
        if not row:
            return None
        return row["node_id"]

    async def insert_edge(
        self,
        *,
        source_node: str,
        source_type: str,
        target_node: str,
        target_type: str,
        relationship: str,
        details: Optional[str] = None,
        confidence: float = 1.0,
        source_doi: Optional[str] = None,
        source_sentence: Optional[str] = None,
        extraction_method: Optional[str] = None,
        embedding: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        metadata = self._scope_metadata(metadata=metadata, user_id=user_id, session_id=session_id)
        metadata = self._normalize_record_status(
            metadata=metadata,
            status_key="edge_status",
            operation="insert_edge",
        )
        metadata = self._normalize_edge_confidence_metadata(
            metadata=metadata,
            relationship=relationship,
            confidence=confidence,
        )
        edge_payload = self._edge_claim_payload(
            source_node=source_node,
            source_type=source_type,
            target_node=target_node,
            target_type=target_type,
            relationship=relationship,
            source_doi=source_doi,
            details=details,
            scope=metadata.get("scope") or {},
        )
        metadata["claim_key"] = self._claim_key(edge_payload)
        metadata["edge_ref"] = self._graph_ref("edge", edge_payload)
        metadata_json = jsonb(metadata)

        async with self._acquire_conn(conn) as active_conn:
            await active_conn.execute(
                f"""
                INSERT INTO {self.schema}.atom_graph_edges(
                    source_node, source_type, target_node, target_type, relationship,
                    details, confidence, source_doi, source_sentence, extraction_method,
                    embedding, metadata, user_id, session_id
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::vector,$12::jsonb,$13,$14)
                """,
                source_node,
                source_type,
                target_node,
                target_type,
                relationship,
                details,
                confidence,
                source_doi,
                source_sentence,
                extraction_method,
                embedding,
                metadata_json,
                user_id,
                session_id,
            )

    async def upsert_edge(
        self,
        payload: Optional[dict[str, Any]] = None,
        *,
        source_node: Optional[str] = None,
        source_type: Optional[str] = None,
        target_node: Optional[str] = None,
        target_type: Optional[str] = None,
        relationship: Optional[str] = None,
        details: Optional[str] = None,
        confidence: float = 1.0,
        source_doi: Optional[str] = None,
        source_sentence: Optional[str] = None,
        extraction_method: Optional[str] = None,
        embedding: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        if payload is not None:
            source_node = str(payload.get("source_node") or payload.get("query_summary") or payload.get("source") or "")
            source_type = str(payload.get("source_type") or "summary_topic")
            target_node = str(payload.get("target_node") or payload.get("summary_id") or payload.get("target") or "")
            target_type = str(payload.get("target_type") or "summary_claim")
            relationship = str(payload.get("relationship") or "summarized_as")
            details = payload.get("details") or payload.get("synthesis") or payload.get("content")
            confidence = float(payload.get("confidence", confidence))
            source_doi = payload.get("source_doi") or source_doi
            source_sentence = payload.get("source_sentence") or source_sentence
            extraction_method = payload.get("extraction_method") or "summary_promotion"
            embedding = payload.get("embedding") or embedding
            metadata = self._normalize_metadata(payload.get("metadata") or metadata)
            scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
            provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
            if scope:
                metadata["scope"] = dict(scope)
            if provenance:
                metadata["provenance"] = dict(provenance)
            user_id = scope.get("user_id", user_id)
            session_id = scope.get("session_id", provenance.get("session_id", session_id))
            workspace_id = scope.get("workspace_id", workspace_id)

        if not source_node or not target_node or not relationship or not source_type or not target_type:
            raise ValueError("source_node/source_type/target_node/target_type/relationship required")

        metadata = self._scope_metadata(
            metadata=metadata,
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            is_global=bool((metadata or {}).get("scope", {}).get("global")) if metadata else None,
        )
        metadata = self._normalize_record_status(
            metadata=metadata,
            status_key="edge_status",
            operation="upsert_edge",
        )
        metadata = self._normalize_edge_confidence_metadata(
            metadata=metadata,
            relationship=relationship,
            confidence=confidence,
        )
        claim_payload = self._edge_claim_payload(
            source_node=source_node,
            source_type=source_type,
            target_node=target_node,
            target_type=target_type,
            relationship=relationship,
            source_doi=source_doi,
            details=details,
            scope=metadata.get("scope") or {},
        )
        metadata["claim_key"] = self._claim_key(claim_payload)
        metadata["edge_ref"] = self._graph_ref("edge", claim_payload)

        async with self._acquire_conn(conn) as active_conn:
            transaction = getattr(active_conn, "transaction", None)
            if callable(transaction):
                async with active_conn.transaction():
                    updated = await self._update_matching_edge(
                        active_conn,
                        source_node=source_node,
                        source_type=source_type,
                        target_node=target_node,
                        target_type=target_type,
                        relationship=relationship,
                        details=details,
                        confidence=confidence,
                        source_doi=source_doi,
                        source_sentence=source_sentence,
                        extraction_method=extraction_method,
                        embedding=embedding,
                        metadata=metadata,
                        user_id=user_id,
                        session_id=session_id,
                        workspace_id=workspace_id,
                        is_global=bool((metadata.get("scope") or {}).get("global")),
                    )
                    if updated == 0:
                        await self.insert_edge(
                            source_node=source_node,
                            source_type=source_type,
                            target_node=target_node,
                            target_type=target_type,
                            relationship=relationship,
                            details=details,
                            confidence=confidence,
                            source_doi=source_doi,
                            source_sentence=source_sentence,
                            extraction_method=extraction_method,
                            embedding=embedding,
                            metadata=metadata,
                            user_id=user_id,
                            session_id=session_id,
                            conn=active_conn,
                        )
            else:
                updated = await self._update_matching_edge(
                    active_conn,
                    source_node=source_node,
                    source_type=source_type,
                    target_node=target_node,
                    target_type=target_type,
                    relationship=relationship,
                    details=details,
                    confidence=confidence,
                    source_doi=source_doi,
                    source_sentence=source_sentence,
                    extraction_method=extraction_method,
                    embedding=embedding,
                    metadata=metadata,
                    user_id=user_id,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    is_global=bool((metadata.get("scope") or {}).get("global")),
                )
                if updated == 0:
                    await self.insert_edge(
                        source_node=source_node,
                        source_type=source_type,
                        target_node=target_node,
                        target_type=target_type,
                        relationship=relationship,
                        details=details,
                        confidence=confidence,
                        source_doi=source_doi,
                        source_sentence=source_sentence,
                        extraction_method=extraction_method,
                        embedding=embedding,
                        metadata=metadata,
                        user_id=user_id,
                        session_id=session_id,
                        conn=active_conn,
                    )

    async def _update_matching_edge(
        self,
        conn: Any,
        *,
        source_node: str,
        source_type: str,
        target_node: str,
        target_type: str,
        relationship: str,
        details: Optional[str],
        confidence: float,
        source_doi: Optional[str],
        source_sentence: Optional[str],
        extraction_method: Optional[str],
        embedding: Optional[str],
        metadata: dict[str, Any],
        user_id: Optional[str],
        session_id: Optional[str],
        workspace_id: Optional[str],
        is_global: bool,
    ) -> int:
        result = await conn.execute(
            f"""
            UPDATE {self.schema}.atom_graph_edges e
               SET details = $6,
                   confidence = $7,
                   source_doi = $8,
                   source_sentence = $9,
                   extraction_method = $10,
                   embedding = $11::vector,
                   metadata = $12::jsonb,
                   user_id = $13,
                   session_id = $14
            WHERE e.source_node = $1
              AND e.source_type = $2
              AND e.target_node = $3
              AND e.target_type = $4
              AND e.relationship = $5
              AND e.user_id IS NOT DISTINCT FROM $13
              AND e.session_id IS NOT DISTINCT FROM $14
              AND {self._workspace_expr('e')} IS NOT DISTINCT FROM $15
              AND {self._global_expr('e')} = $16
            """,
            source_node,
            source_type,
            target_node,
            target_type,
            relationship,
            details,
            confidence,
            source_doi,
            source_sentence,
            extraction_method,
            embedding,
            jsonb(metadata),
            user_id,
            session_id,
            workspace_id,
            is_global,
        )
        return self._rowcount_from_command(result)

    async def upsert_contradiction_record(
        self,
        *,
        claim_ref: str,
        supporting_edge_refs: list[str],
        contradicting_edge_refs: list[str],
        contradiction_kind: str,
        context_overlap: Optional[dict[str, Any]] = None,
        status: str = "contradiction_open",
        summary: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        conn: Any = None,
    ) -> str:
        contradiction_metadata = self._scope_metadata(
            metadata=metadata,
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            is_global=bool((metadata or {}).get("scope", {}).get("global")) if metadata else None,
        )
        contradiction_metadata = self._normalize_record_status(
            metadata=contradiction_metadata,
            status_key="fact_status",
            operation="upsert_contradiction_record",
        )
        contradiction_metadata = self._normalize_contradiction_metadata(
            metadata=contradiction_metadata,
            claim_ref=claim_ref,
            supporting_edge_refs=supporting_edge_refs,
            contradicting_edge_refs=contradicting_edge_refs,
            contradiction_kind=contradiction_kind,
            status=status,
        )
        if context_overlap is not None:
            contradiction_metadata["context_overlap"] = dict(context_overlap)

        contradiction_ref = str(contradiction_metadata["contradiction_ref"])
        content = summary or f"Contradiction record for {claim_ref}"
        await self.upsert_fact(
            content=content,
            fact_type="contradiction_record",
            topic=claim_ref,
            entities=[claim_ref],
            source_doi=None,
            confidence=1.0,
            embedding=None,
            user_id=user_id,
            session_id=session_id,
            metadata=contradiction_metadata,
            workspace_id=workspace_id,
            conn=conn,
        )
        return contradiction_ref

    async def insert_fact(
        self,
        *,
        content: str,
        fact_type: str = "observation",
        topic: Optional[str] = None,
        entities: Optional[list[str]] = None,
        source_doi: Optional[str] = None,
        confidence: float = 1.0,
        embedding: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        conn: Any = None,
    ) -> None:
        entities = entities or []
        metadata = self._scope_metadata(metadata=metadata, user_id=user_id, session_id=session_id)
        metadata = self._normalize_record_status(
            metadata=metadata,
            status_key="fact_status",
            operation="insert_fact",
        )

        # FIX-07: Compute claim_key and skip insert if duplicate exists
        claim_key = self._claim_key(
            self._fact_claim_payload(
                content=content,
                fact_type=fact_type,
                topic=topic,
                entities=entities,
                source_doi=source_doi,
                scope=metadata.get("scope") or {},
            )
        )
        metadata["claim_key"] = claim_key

        async with self._acquire_conn(conn) as active_conn:
            # Dedup check: skip if a fact with the same claim_key already exists
            # Scope dedup by user_id to prevent cross-tenant collision
            capabilities = await self.get_fact_table_capabilities(conn=active_conn)
            if capabilities.has_metadata:
                if user_id:
                    existing = await active_conn.fetchval(
                        f"SELECT 1 FROM {self.schema}.atom_facts WHERE metadata->>'claim_key' = $1 AND user_id = $2 LIMIT 1",
                        claim_key,
                        user_id,
                    )
                else:
                    existing = await active_conn.fetchval(
                        f"SELECT 1 FROM {self.schema}.atom_facts WHERE metadata->>'claim_key' = $1 AND user_id IS NULL LIMIT 1",
                        claim_key,
                    )
                if existing:
                    logger.debug("insert_fact skipped — duplicate claim_key %s", claim_key[:12])
                    return

            if capabilities.has_session_id and capabilities.has_metadata:
                await active_conn.execute(
                    f"""
                    INSERT INTO {self.schema}.atom_facts(
                        content, fact_type, topic, entities, source_doi, confidence, embedding, user_id, session_id, metadata
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8,$9,$10::jsonb)
                    """,
                    content,
                    fact_type,
                    topic,
                    entities,
                    source_doi,
                    confidence,
                    embedding,
                    user_id,
                    session_id,
                    jsonb(metadata),
                )
                return

            columns = [
                "content",
                "fact_type",
                "topic",
                "entities",
                "source_doi",
                "confidence",
                "embedding",
                "user_id",
            ]
            values = ["$1", "$2", "$3", "$4", "$5", "$6", "$7::vector", "$8"]
            args: list[Any] = [
                content,
                fact_type,
                topic,
                entities,
                source_doi,
                confidence,
                embedding,
                user_id,
            ]
            next_index = 9
            if capabilities.has_session_id:
                columns.append("session_id")
                values.append(f"${next_index}")
                args.append(session_id)
                next_index += 1
            if capabilities.has_metadata:
                columns.append("metadata")
                values.append(f"${next_index}::jsonb")
                args.append(jsonb(metadata))

            await active_conn.execute(
                f"""
                INSERT INTO {self.schema}.atom_facts(
                    {", ".join(columns)}
                )
                VALUES ({", ".join(values)})
                """,
                *args,
            )

    async def upsert_fact(
        self,
        *,
        content: str,
        fact_type: str = "observation",
        topic: Optional[str] = None,
        entities: Optional[list[str]] = None,
        source_doi: Optional[str] = None,
        confidence: float = 1.0,
        embedding: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        metadata = self._scope_metadata(
            metadata=metadata,
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            is_global=bool((metadata or {}).get("scope", {}).get("global")) if metadata else None,
        )
        metadata = self._normalize_record_status(
            metadata=metadata,
            status_key="fact_status",
            operation="upsert_fact",
        )
        entities = entities or []
        claim_key = self._claim_key(
            self._fact_claim_payload(
                content=content,
                fact_type=fact_type,
                topic=topic,
                entities=entities,
                source_doi=source_doi,
                scope=metadata.get("scope") or {},
            )
        )
        metadata["claim_key"] = claim_key
        async with self._acquire_conn(conn) as active_conn:
            capabilities = await self.get_fact_table_capabilities(conn=active_conn)
            transaction = getattr(active_conn, "transaction", None)
            if callable(transaction):
                async with active_conn.transaction():
                    updated = await self._update_matching_fact(
                        active_conn,
                        content=content,
                        fact_type=fact_type,
                        topic=topic,
                        entities=entities,
                        source_doi=source_doi,
                        confidence=confidence,
                        embedding=embedding,
                        metadata=metadata,
                        user_id=user_id,
                        session_id=session_id,
                        workspace_id=workspace_id,
                        is_global=bool((metadata.get("scope") or {}).get("global")),
                        claim_key=claim_key,
                        capabilities=capabilities,
                    )
                    if updated == 0:
                        await self.insert_fact(
                            content=content,
                            fact_type=fact_type,
                            topic=topic,
                            entities=entities,
                            source_doi=source_doi,
                            confidence=confidence,
                            embedding=embedding,
                            user_id=user_id,
                            session_id=session_id,
                            metadata=metadata,
                            conn=active_conn,
                        )
            else:
                updated = await self._update_matching_fact(
                    active_conn,
                    content=content,
                    fact_type=fact_type,
                    topic=topic,
                    entities=entities,
                    source_doi=source_doi,
                    confidence=confidence,
                    embedding=embedding,
                    metadata=metadata,
                    user_id=user_id,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    is_global=bool((metadata.get("scope") or {}).get("global")),
                    claim_key=claim_key,
                    capabilities=capabilities,
                )
                if updated == 0:
                    await self.insert_fact(
                        content=content,
                        fact_type=fact_type,
                        topic=topic,
                        entities=entities,
                        source_doi=source_doi,
                        confidence=confidence,
                        embedding=embedding,
                        user_id=user_id,
                        session_id=session_id,
                        metadata=metadata,
                        conn=active_conn,
                    )

    async def _update_matching_fact(
        self,
        conn: Any,
        *,
        content: str,
        fact_type: str,
        topic: Optional[str],
        entities: list[str],
        source_doi: Optional[str],
        confidence: float,
        embedding: Optional[str],
        metadata: dict[str, Any],
        user_id: Optional[str],
        session_id: Optional[str],
        workspace_id: Optional[str],
        is_global: bool,
        capabilities: FactTableCapabilities,
        claim_key: str,
    ) -> int:
        if capabilities.has_session_id and capabilities.has_metadata:
            result = await conn.execute(
                f"""
                UPDATE {self.schema}.atom_facts f
                   SET topic = $3,
                       entities = $4,
                       source_doi = $5,
                       confidence = $6,
                       embedding = $7::vector,
                       user_id = $8,
                       session_id = $9,
                       metadata = $10::jsonb
                WHERE f.content = $1
                  AND f.fact_type = $2
                  AND f.user_id IS NOT DISTINCT FROM $8
                  AND f.session_id IS NOT DISTINCT FROM $9
                  AND {self._workspace_expr('f')} IS NOT DISTINCT FROM $11
                  AND {self._global_expr('f')} = $12
                  AND (
                        coalesce(f.metadata->>'claim_key', '') = $13
                     OR coalesce(f.metadata->>'claim_key', '') = ''
                  )
                """,
                content,
                fact_type,
                topic,
                entities,
                source_doi,
                confidence,
                embedding,
                user_id,
                session_id,
                jsonb(metadata),
                workspace_id,
                is_global,
                claim_key,
            )
            return self._rowcount_from_command(result)

        set_clauses = [
            "topic = $3",
            "entities = $4",
            "source_doi = $5",
            "confidence = $6",
            "embedding = $7::vector",
            "user_id = $8",
        ]
        where_clauses = [
            "f.content = $1",
            "f.fact_type = $2",
            "f.user_id IS NOT DISTINCT FROM $8",
            "f.topic IS NOT DISTINCT FROM $3",
            "f.entities = $4",
            "f.source_doi IS NOT DISTINCT FROM $5",
        ]
        args: list[Any] = [content, fact_type, topic, entities, source_doi, confidence, embedding, user_id]
        next_index = 9
        if capabilities.has_session_id:
            set_clauses.append(f"session_id = ${next_index}")
            where_clauses.append(f"f.session_id IS NOT DISTINCT FROM ${next_index}")
            args.append(session_id)
            next_index += 1
        if capabilities.has_metadata:
            set_clauses.append(f"metadata = ${next_index}::jsonb")
            args.append(jsonb(metadata))
            next_index += 1
            where_clauses.append(f"{self._workspace_expr('f')} IS NOT DISTINCT FROM ${next_index}")
            args.append(workspace_id)
            next_index += 1
            where_clauses.append(f"{self._global_expr('f')} = ${next_index}")
            args.append(is_global)
            next_index += 1
            where_clauses.append(
                f"(coalesce(f.metadata->>'claim_key', '') = ${next_index} OR coalesce(f.metadata->>'claim_key', '') = '')"
            )
            args.append(claim_key)
        result = await conn.execute(
            f"""
            UPDATE {self.schema}.atom_facts f
               SET {', '.join(set_clauses)}
            WHERE {' AND '.join(where_clauses)}
            """,
            *args,
        )
        return self._rowcount_from_command(result)

    @staticmethod
    def _rowcount_from_command(result: Any) -> int:
        if isinstance(result, str):
            parts = result.split()
            if parts:
                try:
                    return int(parts[-1])
                except ValueError:
                    return 0
        return 0

    async def upsert_promoted_summary_claim(self, payload: dict[str, Any], *, conn: Any = None) -> None:
        summary_id = str(payload.get("summary_id") or "")
        query_summary = str(payload.get("query_summary") or "")
        synthesis = str(payload.get("synthesis") or "")
        if not summary_id or not query_summary or not synthesis:
            raise ValueError("summary_id/query_summary/synthesis required for promoted summary claim")

        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        citations = payload.get("citations") or []
        summary_node = f"summary:{summary_id}"
        metadata = {
            "summary_id": summary_id,
            "query_summary": query_summary,
            "citations": citations,
            "scope": dict(scope),
            "provenance": dict(provenance),
        }
        metadata["policy_scope"] = self._derive_policy_scope(metadata)
        metadata["fact_status"] = "proposed"
        metadata["edge_status"] = "proposed"
        metadata["review_required"] = True

        async with self._acquire_conn(conn) as active_conn:
            transaction = getattr(active_conn, "transaction", None)
            if callable(transaction):
                async with active_conn.transaction():
                    await self.upsert_node(
                        canonical_name=query_summary,
                        node_type="summary_topic",
                        aliases=[],
                        description=None,
                        embedding=None,
                        external_ids={"summary_id": summary_id},
                        properties={"promoted_from": "agent_summary"},
                        source_doi=[],
                        conn=active_conn,
                    )
                    await self.upsert_node(
                        canonical_name=summary_node,
                        node_type="summary_claim",
                        aliases=[],
                        description=synthesis,
                        embedding=None,
                        external_ids={"summary_id": summary_id},
                        properties={"query_summary": query_summary},
                        source_doi=[],
                        conn=active_conn,
                    )
                    await self.upsert_edge(
                        source_node=query_summary,
                        source_type="summary_topic",
                        target_node=summary_node,
                        target_type="summary_claim",
                        relationship="summarized_as",
                        details=synthesis,
                        confidence=1.0,
                        source_doi=None,
                        source_sentence=synthesis,
                        extraction_method="summary_promotion",
                        embedding=None,
                        metadata=metadata,
                        user_id=scope.get("user_id"),
                        session_id=scope.get("session_id") or provenance.get("session_id"),
                        workspace_id=scope.get("workspace_id"),
                        conn=active_conn,
                    )
                    await self.upsert_fact(
                        content=synthesis,
                        fact_type="promoted_summary_claim",
                        topic=query_summary,
                        entities=[query_summary, summary_node],
                        source_doi=None,
                        confidence=1.0,
                        embedding=None,
                        user_id=scope.get("user_id"),
                        session_id=scope.get("session_id") or provenance.get("session_id"),
                        metadata=metadata,
                        workspace_id=scope.get("workspace_id"),
                        conn=active_conn,
                    )
            else:
                await self.upsert_node(
                    canonical_name=query_summary,
                    node_type="summary_topic",
                    aliases=[],
                    description=None,
                    embedding=None,
                    external_ids={"summary_id": summary_id},
                    properties={"promoted_from": "agent_summary"},
                    source_doi=[],
                    conn=active_conn,
                )
                await self.upsert_node(
                    canonical_name=summary_node,
                    node_type="summary_claim",
                    aliases=[],
                    description=synthesis,
                    embedding=None,
                    external_ids={"summary_id": summary_id},
                    properties={"query_summary": query_summary},
                    source_doi=[],
                    conn=active_conn,
                )
                await self.upsert_edge(
                    source_node=query_summary,
                    source_type="summary_topic",
                    target_node=summary_node,
                    target_type="summary_claim",
                    relationship="summarized_as",
                    details=synthesis,
                    confidence=1.0,
                    source_doi=None,
                    source_sentence=synthesis,
                    extraction_method="summary_promotion",
                    embedding=None,
                    metadata=metadata,
                    user_id=scope.get("user_id"),
                    session_id=scope.get("session_id") or provenance.get("session_id"),
                    workspace_id=scope.get("workspace_id"),
                    conn=active_conn,
                )
                await self.upsert_fact(
                    content=synthesis,
                    fact_type="promoted_summary_claim",
                    topic=query_summary,
                    entities=[query_summary, summary_node],
                    source_doi=None,
                    confidence=1.0,
                    embedding=None,
                    user_id=scope.get("user_id"),
                    session_id=scope.get("session_id") or provenance.get("session_id"),
                    metadata=metadata,
                    workspace_id=scope.get("workspace_id"),
                    conn=active_conn,
                )

    async def search_edges_hybrid(
        self,
        *,
        query_text: str,
        query_embedding: Optional[str],
        limit: int = 10,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
    ) -> list[GraphEdgeRow]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit out of range")
        if user_id is None and not global_only:
            if _PROD_ENV:
                raise ValueError(
                    "search_edges_hybrid requires user_id in production "
                    "(or set global_only=True for cross-tenant queries)"
                )
            _logger.warning(
                "search_edges_hybrid called WITHOUT user_id — "
                "results may leak cross-tenant data"
            )

        where_clauses: list[str] = []
        params: list[Any] = [query_text, query_embedding]
        policy_scope_expr = self._policy_scope_expr("e")
        details_vector_expr = "to_tsvector('english', coalesce(e.details,''))"
        sentence_vector_expr = "to_tsvector('english', coalesce(e.source_sentence,''))"
        text_match_expr = f"({details_vector_expr} @@ q.tsq OR {sentence_vector_expr} @@ q.tsq)"
        text_rank_expr = (
            "GREATEST("
            f"ts_rank_cd({details_vector_expr}, q.tsq), "
            f"ts_rank_cd({sentence_vector_expr}, q.tsq)"
            ")"
        )

        # $1=query_text, $2=query_embedding
        param_index = 3
        if user_id is not None:
            where_clauses.append(
                f"({policy_scope_expr} = 'global' OR e.user_id = ${param_index})"
            )
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            where_clauses.append(
                f"({policy_scope_expr} = 'global' OR e.session_id = ${param_index})"
            )
            params.append(session_id)
            param_index += 1
        if global_only:
            where_clauses.append(
                f"/*policy_scope_global_only*/ {policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
            param_index += 1
        elif workspace_id is not None:
            where_clauses.append(
                "/*policy_scope_workspace*/ "
                f"(({policy_scope_expr} = 'study' AND {self._workspace_expr('e')} = ${param_index}) "
                f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
            )
            params.append(workspace_id)
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
            param_index += 2
        else:
            where_clauses.append(
                f"/*policy_scope_user_scoped*/ {policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        where_clauses.append(text_match_expr)
        if not include_inactive:
            where_clauses.append(self._active_status_expr("e", "edge_status"))

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Works with or without query_embedding (if None, vec_score becomes NULL and hybrid reduces to text_score).
        sql = f"""
        WITH q AS (
            SELECT websearch_to_tsquery('english', $1) AS tsq, $2::vector AS qv
        )
        SELECT
            e.source_node,
            e.source_type,
            e.relationship,
            e.target_node,
            e.target_type,
            e.details,
            e.confidence,
            e.source_doi,
            e.source_sentence,
            e.extraction_method,
            e.metadata,
            e.user_id,
            e.session_id,
            {text_rank_expr} AS text_score,
            CASE WHEN q.qv IS NULL THEN NULL ELSE (-(e.embedding <=> q.qv)) END AS vec_score,
            (0.6 * {text_rank_expr}
             + 0.4 * COALESCE((-(e.embedding <=> q.qv)), 0.0)) AS hybrid_score
        FROM {self.schema}.atom_graph_edges e, q
        {where_sql}
        ORDER BY hybrid_score DESC
        LIMIT {int(limit)};
        """

        async with self._acquire_conn() as active_conn:
            rows = await active_conn.fetch(sql, *params)

        return [
            GraphEdgeRow(
                source_node=r["source_node"],
                relationship=r["relationship"],
                target_node=r["target_node"],
                details=r["details"],
                hybrid_score=float(r["hybrid_score"]),
                source_type=r.get("source_type"),
                target_type=r.get("target_type"),
                confidence=float(r["confidence"]) if r.get("confidence") is not None else None,
                source_doi=r.get("source_doi"),
                source_sentence=r.get("source_sentence"),
                extraction_method=r.get("extraction_method"),
                metadata=r.get("metadata"),
                text_score=float(r["text_score"]) if r.get("text_score") is not None else None,
                vector_score=float(r["vec_score"]) if r.get("vec_score") is not None else None,
                user_id=r.get("user_id"),
                session_id=r.get("session_id"),
            )
            for r in rows
        ]

    async def search_facts_hybrid(
        self,
        *,
        query_text: str,
        query_embedding: Optional[str],
        limit: int = 10,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
    ) -> list[GraphFactRow]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit out of range")
        if user_id is None and not global_only:
            if _PROD_ENV:
                raise ValueError(
                    "search_facts_hybrid requires user_id in production "
                    "(or set global_only=True for cross-tenant queries)"
                )
            _logger.warning(
                "search_facts_hybrid called WITHOUT user_id — "
                "results may leak cross-tenant data"
            )

        capabilities = await self.get_fact_table_capabilities()
        if global_only and not capabilities.has_metadata:
            return []

        where_clauses: list[str] = []
        ignored_scope_filters: list[str] = []
        params: list[Any] = [query_text, query_embedding]
        param_index = 3
        policy_scope_expr = self._policy_scope_expr("f")
        if user_id is not None:
            if capabilities.has_metadata:
                where_clauses.append(
                    f"({policy_scope_expr} = 'global' OR f.user_id = ${param_index})"
                )
            else:
                where_clauses.append(f"f.user_id = ${param_index}")
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            if capabilities.has_session_id:
                if capabilities.has_metadata:
                    where_clauses.append(
                        f"({policy_scope_expr} = 'global' OR f.session_id = ${param_index})"
                    )
                else:
                    where_clauses.append(f"f.session_id = ${param_index}")
                params.append(session_id)
                param_index += 1
            else:
                ignored_scope_filters.append("session_id")
        if global_only:
            if capabilities.has_metadata:
                where_clauses.append(
                    f"/*policy_scope_global_only*/ {policy_scope_expr} = ANY(${param_index}::text[])"
                )
                params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
                param_index += 1
            else:
                ignored_scope_filters.append("global_only")
        elif workspace_id is not None:
            if capabilities.has_metadata:
                where_clauses.append(
                    "/*policy_scope_workspace*/ "
                    f"(({policy_scope_expr} = 'study' AND {self._workspace_expr('f')} = ${param_index}) "
                    f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
                )
                params.append(workspace_id)
                params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
                param_index += 2
            else:
                ignored_scope_filters.append("workspace_id")
        elif capabilities.has_metadata:
            where_clauses.append(
                f"/*policy_scope_user_scoped*/ {policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        if not include_inactive:
            where_clauses.append(self._active_status_expr("f", "fact_status"))

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        metadata_select = "f.metadata" if capabilities.has_metadata else "NULL::jsonb AS metadata"
        session_select = "f.session_id" if capabilities.has_session_id else "NULL::text AS session_id"
        sql = f"""
        WITH q AS (
            SELECT websearch_to_tsquery('english', $1) AS tsq, $2::vector AS qv
        )
        SELECT
            f.content,
            f.fact_type,
            f.topic,
            f.entities,
            f.source_doi,
            f.confidence,
            {metadata_select},
            f.user_id,
            {session_select},
            ts_rank_cd(to_tsvector('english', coalesce(f.content,'')), q.tsq) AS text_score,
            CASE WHEN q.qv IS NULL THEN NULL ELSE (-(f.embedding <=> q.qv)) END AS vec_score,
            (0.6 * ts_rank_cd(to_tsvector('english', coalesce(f.content,'')), q.tsq)
             + 0.4 * COALESCE((-(f.embedding <=> q.qv)), 0.0)) AS hybrid_score
        FROM {self.schema}.atom_facts f, q
        {where_sql}
        ORDER BY (to_tsvector('english', coalesce(f.content,'')) @@ q.tsq) DESC, hybrid_score DESC
        LIMIT {int(limit)};
        """

        async with self._acquire_conn() as active_conn:
            rows = await active_conn.fetch(sql, *params)

        return [
            GraphFactRow(
                content=r["content"],
                fact_type=r["fact_type"],
                topic=r["topic"],
                entities=list(r["entities"] or []),
                source_doi=r.get("source_doi"),
                confidence=float(r["confidence"]) if r.get("confidence") is not None else None,
                metadata=self._compatibility_metadata(
                    r.get("metadata"),
                    capabilities=capabilities,
                    ignored_scope_filters=ignored_scope_filters,
                ),
                hybrid_score=float(r["hybrid_score"]),
                text_score=float(r["text_score"]) if r.get("text_score") is not None else None,
                vector_score=float(r["vec_score"]) if r.get("vec_score") is not None else None,
                user_id=r.get("user_id"),
                session_id=r.get("session_id"),
            )
            for r in rows
        ]

    async def search_graph_hybrid(
        self,
        *,
        query_text: str,
        query_embedding: Optional[str],
        limit: int = 10,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_edges: bool = True,
        include_facts: bool = True,
        include_inactive: bool = False,
    ) -> list[GraphSearchHit]:
        edge_hits: list[GraphSearchHit] = []
        fact_hits: list[GraphSearchHit] = []
        if include_edges:
            for edge in await self.search_edges_hybrid(
                query_text=query_text,
                query_embedding=query_embedding,
                limit=limit,
                user_id=user_id,
                session_id=session_id,
                workspace_id=workspace_id,
                global_only=global_only,
                include_inactive=include_inactive,
            ):
                edge_hits.append(GraphSearchHit(result_type="edge", hybrid_score=edge.hybrid_score, edge=edge))
        if include_facts:
            for fact in await self.search_facts_hybrid(
                query_text=query_text,
                query_embedding=query_embedding,
                limit=limit,
                user_id=user_id,
                session_id=session_id,
                workspace_id=workspace_id,
                global_only=global_only,
                include_inactive=include_inactive,
            ):
                fact_hits.append(GraphSearchHit(result_type="fact", hybrid_score=fact.hybrid_score, fact=fact))
        hits = edge_hits + fact_hits
        hits.sort(key=lambda item: item.hybrid_score, reverse=True)
        return hits[:limit]

    async def export_active_edge_manifest(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """Export a deterministic manifest of active canonical edges.

        This does not introduce a second authority. It projects the already
        receipted canonical edge surface into a stable manifest that can be used
        to verify whether a downstream graph projection was rebuilt faithfully.
        """
        if limit <= 0:
            raise ValueError("export_active_edge_manifest limit must be positive")
        if user_id is None and not global_only:
            if _PROD_ENV:
                raise ValueError(
                    "export_active_edge_manifest requires user_id in production "
                    "(or set global_only=True for cross-tenant exports)"
                )
            _logger.warning(
                "export_active_edge_manifest called WITHOUT user_id — "
                "results may leak cross-tenant data"
            )

        where_clauses: list[str] = []
        params: list[Any] = []
        policy_scope_expr = self._policy_scope_expr("e")
        param_index = 1
        if user_id is not None:
            where_clauses.append(
                f"({policy_scope_expr} = 'global' OR e.user_id = ${param_index})"
            )
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            where_clauses.append(
                f"({policy_scope_expr} = 'global' OR e.session_id = ${param_index})"
            )
            params.append(session_id)
            param_index += 1
        if global_only:
            where_clauses.append(
                f"/*policy_scope_global_only*/ {policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
            param_index += 1
        elif workspace_id is not None:
            where_clauses.append(
                "/*policy_scope_workspace*/ "
                f"(({policy_scope_expr} = 'study' AND {self._workspace_expr('e')} = ${param_index}) "
                f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
            )
            params.append(workspace_id)
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
            param_index += 2
        else:
            where_clauses.append(
                f"/*policy_scope_user_scoped*/ {policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        where_clauses.append(self._active_status_expr("e", "edge_status"))
        where_sql = "WHERE " + " AND ".join(where_clauses)
        sql = f"""
        SELECT
            e.source_node,
            e.source_type,
            e.relationship,
            e.target_node,
            e.target_type,
            e.details,
            e.confidence,
            e.source_doi,
            e.source_sentence,
            e.extraction_method,
            e.metadata,
            e.user_id,
            e.session_id
        FROM {self.schema}.atom_graph_edges e
        /*edge_manifest_export*/
        {where_sql}
        ORDER BY e.timestamp DESC
        LIMIT {int(limit)};
        """
        async with self._acquire_conn() as active_conn:
            rows = await active_conn.fetch(sql, *params)

        edges = [
            GraphEdgeRow(
                source_node=r["source_node"],
                relationship=r["relationship"],
                target_node=r["target_node"],
                details=r["details"],
                hybrid_score=0.0,
                source_type=r.get("source_type"),
                target_type=r.get("target_type"),
                confidence=float(r["confidence"]) if r.get("confidence") is not None else None,
                source_doi=r.get("source_doi"),
                source_sentence=r.get("source_sentence"),
                extraction_method=r.get("extraction_method"),
                metadata=r.get("metadata"),
                user_id=r.get("user_id"),
                session_id=r.get("session_id"),
            )
            for r in rows
        ]
        manifest: list[dict[str, Any]] = []
        for edge in edges:
            metadata = self._normalize_metadata(edge.metadata)
            edge_ref = str(metadata.get("edge_ref") or "").strip()
            receipt_ref = str(metadata.get("created_by_receipt_ref") or "").strip()
            if not edge_ref:
                raise ValueError("export_active_edge_manifest requires edge_ref on active edges")
            if not receipt_ref:
                raise ValueError("export_active_edge_manifest requires created_by_receipt_ref on active edges")
            manifest.append(
                {
                    "edge_ref": edge_ref,
                    "source_node": edge.source_node,
                    "source_type": edge.source_type,
                    "relationship": edge.relationship,
                    "target_node": edge.target_node,
                    "target_type": edge.target_type,
                    "details": edge.details,
                    "confidence": edge.confidence,
                    "source_doi": edge.source_doi,
                    "source_sentence": edge.source_sentence,
                    "extraction_method": edge.extraction_method,
                    "edge_status": str(metadata.get("edge_status") or "active"),
                    "edge_kind": metadata.get("edge_kind"),
                    "policy_scope": metadata.get("policy_scope"),
                    "confidence_model_ref": metadata.get("confidence_model_ref"),
                    "created_by_receipt_ref": receipt_ref,
                }
            )
        manifest.sort(
            key=lambda item: (
                str(item["edge_ref"]),
                str(item["source_node"]),
                str(item["relationship"]),
                str(item["target_node"]),
            )
        )
        return manifest

    async def build_node2vec_coverage_snapshot(
        self,
        *,
        active_node_count: Optional[int] = None,
        active_node_resolvable_count: Optional[int] = None,
        projection_drift_green_days: int = 0,
        edge_kind_registry_frozen: bool = False,
        golden_v2_available: bool = False,
        p0_permission_traversal_failures: int = 0,
        domain_density_threshold_passed: bool = False,
        changed_edges_since_training: int = 0,
        training_edges_total: int = 0,
        conn: Any = None,
    ) -> GraphNode2VecCoverageSnapshot:
        """Build the G2.4 node2vec coverage snapshot from canonical edge authority.

        This method only computes what the canonical GraphRAG schema can know
        today without inventing a second authority. Receipt coverage comes from
        canonical edges; node resolvability and operational health signals are
        injected by the caller until those surfaces become first-class runtime
        registries.
        """
        async with self._acquire_conn(conn) as active_conn:
            row = await active_conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE {self._active_status_expr('atom_graph_edges', 'edge_status')}
                    ) AS active_edge_count,
                    COUNT(*) FILTER (
                        WHERE {self._active_status_expr('atom_graph_edges', 'edge_status')}
                          AND nullif(trim(coalesce(atom_graph_edges.metadata->>'created_by_receipt_ref', '')), '') IS NOT NULL
                    ) AS active_edges_with_receipt_count
                FROM {self.schema}.atom_graph_edges AS atom_graph_edges
                """
            )

        def _row_metric(name: str) -> int:
            if row is None:
                return 0
            if isinstance(row, dict):
                return int(row.get(name) or 0)
            try:
                return int(row[name] or 0)
            except Exception:
                return 0

        active_edge_count = _row_metric("active_edge_count")
        active_edges_with_receipt_count = _row_metric("active_edges_with_receipt_count")
        active_edge_receipt_coverage_ratio = (
            active_edges_with_receipt_count / active_edge_count if active_edge_count > 0 else 0.0
        )

        active_node_resolvable_ratio: Optional[float] = None
        if active_node_count is not None and active_node_resolvable_count is not None:
            normalized_node_count = max(0, int(active_node_count))
            normalized_resolvable_count = max(0, int(active_node_resolvable_count))
            if normalized_node_count > 0:
                active_node_resolvable_ratio = normalized_resolvable_count / normalized_node_count
            else:
                active_node_resolvable_ratio = 0.0
            active_node_count = normalized_node_count
            active_node_resolvable_count = normalized_resolvable_count

        return GraphNode2VecCoverageSnapshot(
            active_edge_count=active_edge_count,
            active_edges_with_receipt_count=active_edges_with_receipt_count,
            active_edge_receipt_coverage_ratio=active_edge_receipt_coverage_ratio,
            active_node_count=active_node_count,
            active_node_resolvable_count=active_node_resolvable_count,
            active_node_resolvable_ratio=active_node_resolvable_ratio,
            projection_drift_green_days=max(0, int(projection_drift_green_days)),
            edge_kind_registry_frozen=bool(edge_kind_registry_frozen),
            golden_v2_available=bool(golden_v2_available),
            p0_permission_traversal_failures=max(0, int(p0_permission_traversal_failures)),
            domain_density_threshold_passed=bool(domain_density_threshold_passed),
            changed_edges_since_training=max(0, int(changed_edges_since_training)),
            training_edges_total=max(0, int(training_edges_total)),
            notes={
                "canonical_authority": "atom_graph_edges",
                "node_resolvability_source": "caller_injected_until_global_ref_index_is_runtime_authority",
                "runtime_enablement": "g2_4_never_auto_promotes_runtime",
            },
        )

    async def aggregate_edge_count(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
    ) -> int:
        where_clauses: list[str] = []
        params: list[Any] = []
        param_index = 1
        policy_scope_expr = self._policy_scope_expr("e")

        if user_id is not None:
            where_clauses.append(f"({policy_scope_expr} = 'global' OR e.user_id = ${param_index})")
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            where_clauses.append(f"({policy_scope_expr} = 'global' OR e.session_id = ${param_index})")
            params.append(session_id)
            param_index += 1
        if global_only:
            where_clauses.append(f"{policy_scope_expr} = ANY(${param_index}::text[])")
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
            param_index += 1
        elif workspace_id is not None:
            where_clauses.append(
                f"(({policy_scope_expr} = 'study' AND {self._workspace_expr('e')} = ${param_index}) "
                f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
            )
            params.append(workspace_id)
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
            param_index += 2
        else:
            where_clauses.append(f"{policy_scope_expr} = ANY(${param_index}::text[])")
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        if not include_inactive:
            where_clauses.append(self._active_status_expr("e", "edge_status"))

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"SELECT COUNT(*) AS count FROM {self.schema}.atom_graph_edges e {where_sql}"
        async with self._acquire_conn() as active_conn:
            row = await active_conn.fetchrow(sql, *params)
        return int((row["count"] if row else 0) or 0)

    async def aggregate_node_degree(
        self,
        *,
        node_name: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
    ) -> int:
        where_clauses: list[str] = ["(e.source_node = $1 OR e.target_node = $1)"]
        params: list[Any] = [node_name]
        param_index = 2
        policy_scope_expr = self._policy_scope_expr("e")

        if user_id is not None:
            where_clauses.append(f"({policy_scope_expr} = 'global' OR e.user_id = ${param_index})")
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            where_clauses.append(f"({policy_scope_expr} = 'global' OR e.session_id = ${param_index})")
            params.append(session_id)
            param_index += 1
        if global_only:
            where_clauses.append(f"{policy_scope_expr} = ANY(${param_index}::text[])")
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
            param_index += 1
        elif workspace_id is not None:
            where_clauses.append(
                f"(({policy_scope_expr} = 'study' AND {self._workspace_expr('e')} = ${param_index}) "
                f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
            )
            params.append(workspace_id)
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
            param_index += 2
        else:
            where_clauses.append(f"{policy_scope_expr} = ANY(${param_index}::text[])")
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        if not include_inactive:
            where_clauses.append(self._active_status_expr("e", "edge_status"))

        where_sql = "WHERE " + " AND ".join(where_clauses)
        sql = f"SELECT COUNT(*) AS count FROM {self.schema}.atom_graph_edges e {where_sql}"
        async with self._acquire_conn() as active_conn:
            row = await active_conn.fetchrow(sql, *params)
        return int((row["count"] if row else 0) or 0)

    async def rank_nodes_by_degree(
        self,
        *,
        top_k: int = 10,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or top_k > 100:
            raise ValueError("top_k out of range")

        where_clauses: list[str] = []
        params: list[Any] = []
        param_index = 1
        policy_scope_expr = self._policy_scope_expr("e")

        if user_id is not None:
            where_clauses.append(f"({policy_scope_expr} = 'global' OR e.user_id = ${param_index})")
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            where_clauses.append(f"({policy_scope_expr} = 'global' OR e.session_id = ${param_index})")
            params.append(session_id)
            param_index += 1
        if global_only:
            where_clauses.append(f"{policy_scope_expr} = ANY(${param_index}::text[])")
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
            param_index += 1
        elif workspace_id is not None:
            where_clauses.append(
                f"(({policy_scope_expr} = 'study' AND {self._workspace_expr('e')} = ${param_index}) "
                f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
            )
            params.append(workspace_id)
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
            param_index += 2
        else:
            where_clauses.append(f"{policy_scope_expr} = ANY(${param_index}::text[])")
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        if not include_inactive:
            where_clauses.append(self._active_status_expr("e", "edge_status"))

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"""
        WITH visible_edges AS (
            SELECT e.source_node, e.target_node
            FROM {self.schema}.atom_graph_edges e
            {where_sql}
        ),
        degrees AS (
            SELECT source_node AS node_name FROM visible_edges
            UNION ALL
            SELECT target_node AS node_name FROM visible_edges
        )
        SELECT node_name, COUNT(*) AS degree
        FROM degrees
        GROUP BY node_name
        ORDER BY degree DESC, node_name ASC
        LIMIT {int(top_k)}
        """
        async with self._acquire_conn() as active_conn:
            rows = await active_conn.fetch(sql, *params)
        return [
            {"node_name": str(row["node_name"]), "degree": int(row["degree"] or 0)}
            for row in rows
        ]

    async def budgeted_hop1_edges(
        self,
        *,
        seed_nodes: Iterable[str],
        limit: int = 50,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
        policy: Optional[str] = "interactive",
        budget_ref: Optional[str] = None,
    ) -> BudgetedGraphTraversalResult:
        normalized_seed_nodes = list(seed_nodes)
        if not normalized_seed_nodes:
            budget = self._normalize_traversal_budget(
                seed_nodes=[],
                limit=limit,
                policy=policy,
                budget_ref=budget_ref,
            )
            cost_event = GraphTraversalCostEvent(
                traversal_request_ref=budget["traversal_request_ref"],
                budget_ref=budget["budget_ref"],
                policy=budget["policy"],
                visited_nodes=0,
                visited_edges=0,
                returned_paths=0,
                cost_units=0,
                status="complete",
                latency_budget_ms=budget["latency_budget_ms"],
            )
            return BudgetedGraphTraversalResult(
                edges=[],
                traversal_request_ref=budget["traversal_request_ref"],
                budget_ref=budget["budget_ref"],
                policy=budget["policy"],
                status="complete",
                cost_event=cost_event,
            )

        if user_id is None and not global_only:
            if _PROD_ENV:
                raise ValueError(
                    "budgeted_hop1_edges requires user_id in production "
                    "(or set global_only=True for cross-tenant queries)"
                )
            _logger.warning(
                "budgeted_hop1_edges called WITHOUT user_id — "
                "results may leak cross-tenant data"
            )

        budget = self._normalize_traversal_budget(
            seed_nodes=normalized_seed_nodes,
            limit=limit,
            policy=policy,
            budget_ref=budget_ref,
        )

        where = "(source_node = ANY($1::text[]) OR target_node = ANY($1::text[]))"
        params: list[Any] = [normalized_seed_nodes]
        param_index = 2
        policy_scope_expr = self._policy_scope_expr("atom_graph_edges")

        if user_id is not None:
            where += f" AND ({policy_scope_expr} = 'global' OR user_id = ${param_index})"
            params.append(user_id)
            param_index += 1
        if session_id is not None:
            where += f" AND ({policy_scope_expr} = 'global' OR session_id = ${param_index})"
            params.append(session_id)
            param_index += 1
        if global_only:
            where += (
                f" AND /*policy_scope_global_only*/ "
                f"{policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=True))
            param_index += 1
        elif workspace_id is not None:
            where += (
                " AND /*policy_scope_workspace*/ "
                f"(({policy_scope_expr} = 'study' "
                f"AND {self._workspace_expr('atom_graph_edges')} = ${param_index}) "
                f"OR {policy_scope_expr} = ANY(${param_index + 1}::text[]))"
            )
            params.append(workspace_id)
            params.append(self._allowed_policy_scopes(workspace_id=workspace_id, global_only=False))
            param_index += 2
        else:
            where += (
                f" AND /*policy_scope_user_scoped*/ "
                f"{policy_scope_expr} = ANY(${param_index}::text[])"
            )
            params.append(self._allowed_policy_scopes(workspace_id=None, global_only=False))
            param_index += 1
        if not include_inactive:
            where += " AND " + self._active_status_expr("atom_graph_edges", "edge_status")

        fetch_limit = int(budget["effective_limit"]) + 1
        sql = f"""
        SELECT source_node, source_type, relationship, target_node, target_type, details, confidence, source_doi,
               source_sentence, extraction_method, metadata, user_id, session_id, 0.0 AS hybrid_score
        FROM {self.schema}.atom_graph_edges
        WHERE {where}
        ORDER BY timestamp DESC
        LIMIT {fetch_limit};
        """

        async with self._acquire_conn() as active_conn:
            rows = await active_conn.fetch(sql, *params)

        has_more_than_effective_limit = len(rows) > int(budget["effective_limit"])
        status = (
            "partial_budget_exhausted"
            if has_more_than_effective_limit or int(budget["requested_limit"]) > int(budget["effective_limit"])
            else "complete"
        )
        effective_rows = rows[: int(budget["effective_limit"])]
        edges = [
            GraphEdgeRow(
                source_node=r["source_node"],
                relationship=r["relationship"],
                target_node=r["target_node"],
                details=r["details"],
                hybrid_score=float(r["hybrid_score"]),
                source_type=r.get("source_type"),
                target_type=r.get("target_type"),
                confidence=float(r["confidence"]) if r.get("confidence") is not None else None,
                source_doi=r.get("source_doi"),
                source_sentence=r.get("source_sentence"),
                extraction_method=r.get("extraction_method"),
                metadata=r.get("metadata"),
                user_id=r.get("user_id"),
                session_id=r.get("session_id"),
            )
            for r in effective_rows
        ]
        visited_edges = len(effective_rows) + (1 if has_more_than_effective_limit else 0)
        cost_event = GraphTraversalCostEvent(
            traversal_request_ref=budget["traversal_request_ref"],
            budget_ref=budget["budget_ref"],
            policy=budget["policy"],
            visited_nodes=len(normalized_seed_nodes),
            visited_edges=visited_edges,
            returned_paths=len(edges),
            cost_units=len(normalized_seed_nodes) + visited_edges,
            status=status,
            latency_budget_ms=budget["latency_budget_ms"],
        )
        return BudgetedGraphTraversalResult(
            edges=edges,
            traversal_request_ref=budget["traversal_request_ref"],
            budget_ref=budget["budget_ref"],
            policy=budget["policy"],
            status=status,
            cost_event=cost_event,
        )

    async def hop1_edges(
        self,
        *,
        seed_nodes: Iterable[str],
        limit: int = 50,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        include_inactive: bool = False,
    ) -> list[GraphEdgeRow]:
        result = await self.budgeted_hop1_edges(
            seed_nodes=seed_nodes,
            limit=limit,
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            global_only=global_only,
            include_inactive=include_inactive,
            policy="interactive",
            budget_ref=None,
        )
        return result.edges


__all__ = [
    "BudgetedGraphTraversalResult",
    "FactTableCapabilities",
    "GraphEdgeRow",
    "GraphFactRow",
    "GraphSearchHit",
    "GraphTraversalCostEvent",
    "TimescaleGraphRAGStore",
]
