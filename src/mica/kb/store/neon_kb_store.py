"""KB-UNBLOCK-003: NeonKBStore — Tier-1 durable KB store.

Production target: Neon Postgres (serverless) with pgvector for embedding
search. Single authoritative backend for kb.ingest / kb.semantic_search in
P0/P1 product phases. No SQLite fallback, no degradation: if Neon is not
reachable, the caller surfaces degraded_reason explicitly.

Schema (v1):

- kb_documents:    UUID id PK, content TEXT, source_url, source_doi,
                   content_hash UNIQUE per (kb_id, mudo_id, branch_id),
                   kb_id, mudo_id, branch_id, created_at.
- kb_embeddings:   doc_id FK, model_id, embedding vector(1024) NOT NULL,
                   embed_receipt_urn, l2_norm, embedded_at.
                   ivfflat index over embedding.
- kb_provenance:   receipt_urn PK, doc_id FK, source_url, source_doi,
                   retrieval_method, retrieved_at.
- kb_edges:        from_doc_id FK, to_doc_id FK, predicate TEXT,
                   weight FLOAT, provenance_urn, created_at.
- kb_lineage:      parent_doc_id FK, child_doc_id FK, derivation_type,
                   created_at.
- kb_promotion_receipts: idempotency_key UNIQUE, from_tier, to_tier,
                   receipt_urn, promoted_count, skipped_duplicates, promoted_at.

Idempotency: (kb_id, mudo_id, branch_id, content_hash) is the
natural primary key for kb_documents. Re-ingest of the same
content_hash returns the existing doc_id.

Env vars (read at construction):
- MICA_KB_NEON_DIRECT_DSN (preferred for agent envs): DSN with port 5432.
- MICA_KB_NEON_DSN: explicit override.
- NEON_DATABASE_URL: legacy fallback (may point to pooler port 30571 which
  is NOT reachable from agent envs due to Neon IP allow-list defaults).
- MICA_KB_NEON_POOL_MIN (default 1), MICA_KB_NEON_POOL_MAX (default 5).
- MICA_KB_NEON_STATEMENT_TIMEOUT_MS (default 30000).

Why port 5432 vs 30571:
- 30571 = Neon pooler. Connection pooling at the Neon edge. In agent envs
  this port is BLOCKED by Neon IP allow-list defaults; only operator
  workstations where Neon is fully allow-listed can reach it.
- 5432 = direct Postgres endpoint. Reachable from any IP that Neon allows.
  Used for direct (non-pooled) connections.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)


# Single source of truth for embedding dim.
KB_EMBEDDING_DIM = 1024


SCHEMA_V1_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

-- KB-UNBLOCK program owns a separate schema namespace to avoid colliding with
-- the MUDO/KB Lane pre-existing tables (kb_documents, kb_chunks, etc.).
-- All tables are prefixed with `kbu_` (kb-unblock).
-- This is a clean architectural separation: the ingest pipeline is a
-- producer that writes here; promotion jobs propagate to GraphRAG/Timescale.

CREATE TABLE IF NOT EXISTS kbu_documents (
    id UUID PRIMARY KEY,
    kb_id UUID NOT NULL,
    mudo_id UUID NOT NULL,
    branch_id UUID NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_url TEXT,
    source_doi TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kb_id, mudo_id, branch_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_kbu_documents_kb_id ON kbu_documents (kb_id);
CREATE INDEX IF NOT EXISTS idx_kbu_documents_content_hash ON kbu_documents (content_hash);

CREATE TABLE IF NOT EXISTS kbu_embeddings (
    doc_id UUID PRIMARY KEY REFERENCES kbu_documents(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    embed_receipt_urn TEXT NOT NULL,
    l2_norm DOUBLE PRECISION NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_kbu_embeddings_ivfflat
    ON kbu_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS kbu_provenance (
    receipt_urn TEXT PRIMARY KEY,
    doc_id UUID NOT NULL REFERENCES kbu_documents(id) ON DELETE CASCADE,
    source_url TEXT,
    source_doi TEXT,
    retrieval_method TEXT NOT NULL DEFAULT 'seed_source',
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kbu_edges (
    from_doc_id UUID NOT NULL REFERENCES kbu_documents(id) ON DELETE CASCADE,
    to_doc_id UUID NOT NULL REFERENCES kbu_documents(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    provenance_urn TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_doc_id, to_doc_id, predicate)
);

CREATE INDEX IF NOT EXISTS idx_kbu_edges_from ON kbu_edges (from_doc_id);
CREATE INDEX IF NOT EXISTS idx_kbu_edges_to ON kbu_edges (to_doc_id);

CREATE TABLE IF NOT EXISTS kbu_lineage (
    parent_doc_id UUID NOT NULL REFERENCES kbu_documents(id) ON DELETE CASCADE,
    child_doc_id UUID NOT NULL REFERENCES kbu_documents(id) ON DELETE CASCADE,
    derivation_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_doc_id, child_doc_id, derivation_type)
);

CREATE TABLE IF NOT EXISTS kbu_promotion_receipts (
    receipt_urn TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    from_tier TEXT NOT NULL,
    to_tier TEXT NOT NULL,
    promoted_count INTEGER NOT NULL DEFAULT 0,
    skipped_duplicates INTEGER NOT NULL DEFAULT 0,
    promoted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _coerce_uuid(value: Any, *, label: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be a valid UUID string, got {value!r}") from exc
    raise TypeError(f"{label} must be UUID or UUID-string, got {type(value).__name__}")


def _resolve_dsn() -> Optional[str]:
    """Resolve the Neon DSN, preferring the direct endpoint (port 5432)
    over the pooler (port 30571).

    Precedence:
    1. MICA_KB_NEON_DIRECT_DSN (port 5432 direct endpoint, what the agent env actually reaches)
    2. MICA_KB_NEON_DSN (legacy explicit override)
    3. NEON_DATABASE_URL (legacy — note this may point to the pooler and fail in agent envs)

    The agent env (PowerShell from MICA repo) can NOT reach the pooler on 30571
    because of Neon IP allow-list defaults. The direct endpoint on 5432 IS
    reachable from this env. Operators running on the workstation where Neon
    is fully in the allow-list can keep using NEON_DATABASE_URL as-is.
    """
    for env_name in (
        "MICA_KB_NEON_DIRECT_DSN",
        "MICA_KB_NEON_DSN",
        "NEON_DATABASE_URL",
    ):
        dsn = (os.getenv(env_name) or "").strip()
        if not dsn:
            continue
        if dsn == "__DATA_API_KEY__":
            continue
        cleaned = dsn.strip("'\"")
        if env_name != "MICA_KB_NEON_DIRECT_DSN":
            return _derive_direct_neon_dsn(cleaned)
        return cleaned
    return None


def _derive_direct_neon_dsn(dsn: str) -> str:
    cleaned = str(dsn or "").strip().strip("'\"")
    if not cleaned:
        return cleaned
    try:
        parsed = urlsplit(cleaned)
    except Exception:
        return cleaned
    hostname = parsed.hostname or ""
    if "-pooler" not in hostname:
        return cleaned
    direct_host = hostname.replace("-pooler", "", 1)
    auth_bits = []
    if parsed.username is not None:
        auth_bits.append(parsed.username)
        if parsed.password is not None:
            auth_bits[-1] = f"{auth_bits[-1]}:{parsed.password}"
        auth_bits[-1] = f"{auth_bits[-1]}@"
    direct_netloc = f"{''.join(auth_bits)}{direct_host}:5432"
    return urlunsplit((parsed.scheme, direct_netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass
class KBDocumentRow:
    doc_id: uuid.UUID
    kb_id: uuid.UUID
    mudo_id: uuid.UUID
    branch_id: uuid.UUID
    content: str
    content_hash: str
    source_url: Optional[str]
    source_doi: Optional[str]
    created_at: str


@dataclass
class KBSearchHitRow:
    doc_id: uuid.UUID
    content: str
    source_url: Optional[str]
    source_doi: Optional[str]
    content_hash: str
    similarity: float
    provenance_receipt_urn: Optional[str]
    embed_receipt_urn: str


@dataclass
class KBProvenanceRow:
    receipt_urn: str
    doc_id: uuid.UUID
    source_url: Optional[str]
    source_doi: Optional[str]
    retrieval_method: str
    retrieved_at: str


@dataclass
class NeonKBStore:
    """Production Tier-1 KB store. Backed by Neon Postgres + pgvector.

    If Neon is unreachable from the calling environment, `migrate()` and
    every other method raise `NeonUnreachable` with the underlying error.
    No silent fallback: the caller (Command Kernel handler) translates
    that into degraded_reason and emits the corresponding receipt.
    """

    dsn: str = field(default_factory=_resolve_dsn)
    embedding_dim: int = KB_EMBEDDING_DIM
    pool_min: int = field(default_factory=lambda: int(os.getenv("MICA_KB_NEON_POOL_MIN", "1")))
    pool_max: int = field(default_factory=lambda: int(os.getenv("MICA_KB_NEON_POOL_MAX", "5")))
    statement_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("MICA_KB_NEON_STATEMENT_TIMEOUT_MS", "30000"))
    )

    def __post_init__(self) -> None:
        if not self.dsn:
            raise NeonUnreachable(
                "MICA_KB_NEON_DSN / NEON_DATABASE_URL not set or is the __DATA_API_KEY__ placeholder"
            )
        if self.embedding_dim != KB_EMBEDDING_DIM:
            raise ValueError(
                f"NeonKBStore only supports embedding_dim={KB_EMBEDDING_DIM}, got {self.embedding_dim}"
            )
        self._psycopg = None  # lazy import
        self._schema_ready = False

    def _import_psycopg(self):
        if self._psycopg is not None:
            return self._psycopg
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise NeonUnreachable(f"psycopg2 not installed: {exc}") from exc
        self._psycopg = psycopg2
        return psycopg2

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        psycopg2 = self._import_psycopg()
        try:
            conn = psycopg2.connect(self.dsn, connect_timeout=10)
        except Exception as exc:
            raise NeonUnreachable(
                f"Neon connection failed: {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
            conn.commit()
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _schema_tables_ready(self) -> bool:
        """Return True when the minimum durable KB schema is present."""
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            to_regclass('public.kbu_documents'),
                            to_regclass('public.kbu_embeddings')
                        """
                    )
                    row = cur.fetchone()
            except Exception as exc:
                raise NeonUnreachable(
                    f"Neon schema probe failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return bool(row and row[0] and row[1])

    def _ensure_schema_ready(self) -> None:
        """Idempotently bootstrap the durable KB schema on first real use."""
        if self._schema_ready:
            return
        if not self._schema_tables_ready():
            self.migrate()
        self._schema_ready = True

    # ── Schema ──────────────────────────────────────────────────────────────

    def migrate(self) -> dict:
        """Create schema if not exists. Returns receipt dict."""
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_V1_SQL)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise NeonUnreachable(
                    f"Neon migration failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return {
            "receipt_type": "neon_kb_schema_v1_migration",
            "status": "applied",
            "embedding_dim": self.embedding_dim,
            "tables": [
                "kbu_documents", "kbu_embeddings", "kbu_provenance",
                "kbu_edges", "kbu_lineage", "kbu_promotion_receipts",
            ],
        }

    # ── Document + Embedding persistence ───────────────────────────────────

    def upsert_document(
        self,
        *,
        kb_id: Any,
        mudo_id: Any,
        branch_id: Any,
        content: str,
        content_hash: str,
        embedding: Sequence[float],
        model_id: str,
        embed_receipt_urn: str,
        provenance_receipt_urn: str,
        source_url: Optional[str] = None,
        source_doi: Optional[str] = None,
        retrieval_method: str = "seed_source",
        embedding_l2_norm: Optional[float] = None,
    ) -> dict:
        """Upsert a document + its embedding + provenance atomically.

        Idempotency: (kb_id, mudo_id, branch_id, content_hash) is unique.
        Re-ingest of the same triple returns the existing doc_id.
        Returns receipt with doc_id and whether it was newly created.
        """
        kb_id_u = _coerce_uuid(kb_id, label="kb_id")
        mudo_id_u = _coerce_uuid(mudo_id, label="mudo_id")
        branch_id_u = _coerce_uuid(branch_id, label="branch_id")
        if not content or not content.strip():
            raise ValueError("content must be non-empty")
        if not content_hash:
            raise ValueError("content_hash must be non-empty")
        if len(embedding) != self.embedding_dim:
            raise ValueError(
                f"embedding must have {self.embedding_dim} dims, got {len(embedding)}"
            )
        if not embed_receipt_urn.startswith("urn:mica:embed:"):
            raise ValueError("embed_receipt_urn must start with urn:mica:embed:")
        if not provenance_receipt_urn.startswith("urn:mica:provenance:"):
            raise ValueError("provenance_receipt_urn must start with urn:mica:provenance:")
        if embedding_l2_norm is None:
            embedding_l2_norm = float(sum(float(x) ** 2 for x in embedding) ** 0.5)
        embedding_str = "[" + ",".join(f"{float(x):.8f}" for x in embedding) + "]"

        self._ensure_schema_ready()
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    # Upsert kbu_documents — RETURNING gives us either the new id or the existing one.
                    cur.execute(
                        """
                        INSERT INTO kbu_documents
                            (id, kb_id, mudo_id, branch_id, content, content_hash,
                             source_url, source_doi)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (kb_id, mudo_id, branch_id, content_hash) DO UPDATE
                            SET source_url = COALESCE(EXCLUDED.source_url, kbu_documents.source_url),
                                source_doi = COALESCE(EXCLUDED.source_doi, kbu_documents.source_doi)
                        RETURNING id, (xmax = 0) AS inserted
                        """,
                        (
                            str(uuid.uuid4()),
                            str(kb_id_u),
                            str(mudo_id_u),
                            str(branch_id_u),
                            content,
                            content_hash,
                            source_url,
                            source_doi,
                        ),
                    )
                    row = cur.fetchone()
                    doc_id = _coerce_uuid(row[0], label="doc_id")
                    inserted = bool(row[1])

                    # Upsert embedding only on insert or if embed_receipt changed.
                    cur.execute(
                        """
                        INSERT INTO kbu_embeddings
                            (doc_id, model_id, embedding, embed_receipt_urn, l2_norm)
                        VALUES (%s, %s, %s::vector, %s, %s)
                        ON CONFLICT (doc_id) DO UPDATE
                            SET model_id = EXCLUDED.model_id,
                                embedding = EXCLUDED.embedding,
                                embed_receipt_urn = EXCLUDED.embed_receipt_urn,
                                l2_norm = EXCLUDED.l2_norm,
                                embedded_at = now()
                        """,
                        (
                            str(doc_id),
                            model_id,
                            embedding_str,
                            embed_receipt_urn,
                            float(embedding_l2_norm),
                        ),
                    )

                    # Upsert provenance.
                    cur.execute(
                        """
                        INSERT INTO kbu_provenance
                            (receipt_urn, doc_id, source_url, source_doi, retrieval_method)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (receipt_urn) DO NOTHING
                        """,
                        (
                            provenance_receipt_urn,
                            str(doc_id),
                            source_url,
                            source_doi,
                            retrieval_method,
                        ),
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise NeonUnreachable(
                    f"Neon upsert failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return {
            "receipt_type": "neon_kb_document_upsert",
            "doc_id": str(doc_id),
            "kb_id": str(kb_id_u),
            "mudo_id": str(mudo_id_u),
            "branch_id": str(branch_id_u),
            "content_hash": content_hash,
            "embed_receipt_urn": embed_receipt_urn,
            "provenance_receipt_urn": provenance_receipt_urn,
            "inserted": inserted,
            "embedding_dim": self.embedding_dim,
            "embedding_l2_norm": float(embedding_l2_norm),
        }

    # ── Semantic search ─────────────────────────────────────────────────────

    def semantic_search(
        self,
        *,
        kb_id: Any,
        query_embedding: Sequence[float],
        top_k: int = 10,
        min_similarity: float = 0.0,
    ) -> list[KBSearchHitRow]:
        if len(query_embedding) != self.embedding_dim:
            raise ValueError(
                f"query_embedding must have {self.embedding_dim} dims, got {len(query_embedding)}"
            )
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be in [1, 100]")
        if not (-1.0 <= min_similarity <= 1.0):
            raise ValueError("min_similarity must be in [-1, 1]")
        kb_id_u = _coerce_uuid(kb_id, label="kb_id")
        embedding_str = "[" + ",".join(f"{float(x):.8f}" for x in query_embedding) + "]"

        self._ensure_schema_ready()
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    # Cosine distance (1 - cos_sim). We return 1 - distance = similarity.
                    cur.execute(
                        """
                        SELECT
                            d.id,
                            d.content,
                            d.source_url,
                            d.source_doi,
                            d.content_hash,
                            1.0 - (e.embedding <=> %s::vector) AS similarity,
                            e.embed_receipt_urn,
                            p.receipt_urn AS provenance_receipt_urn
                        FROM kbu_embeddings e
                        JOIN kbu_documents d ON d.id = e.doc_id
                        LEFT JOIN kbu_provenance p ON p.doc_id = d.id
                        WHERE d.kb_id = %s
                          AND 1.0 - (e.embedding <=> %s::vector) >= %s
                        ORDER BY e.embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (embedding_str, str(kb_id_u), embedding_str, min_similarity,
                         embedding_str, int(top_k)),
                    )
                    rows = cur.fetchall()
            except Exception as exc:
                raise NeonUnreachable(
                    f"Neon semantic_search failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return [
            KBSearchHitRow(
                doc_id=_coerce_uuid(r[0], label="doc_id"),
                content=r[1] or "",
                source_url=r[2],
                source_doi=r[3],
                content_hash=r[4] or "",
                similarity=float(r[5]),
                provenance_receipt_urn=r[6],
                embed_receipt_urn=r[7] or "",
            )
            for r in rows
        ]

    # ── Edges, lineage, promotion ───────────────────────────────────────────

    def attach_edge(
        self,
        *,
        from_doc_id: Any,
        to_doc_id: Any,
        predicate: str,
        weight: float = 1.0,
        provenance_urn: Optional[str] = None,
    ) -> dict:
        if not predicate or not predicate.strip():
            raise ValueError("predicate must be non-empty")
        from_u = _coerce_uuid(from_doc_id, label="from_doc_id")
        to_u = _coerce_uuid(to_doc_id, label="to_doc_id")
        self._ensure_schema_ready()
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO kbu_edges
                            (from_doc_id, to_doc_id, predicate, weight, provenance_urn)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (from_doc_id, to_doc_id, predicate) DO UPDATE
                            SET weight = EXCLUDED.weight,
                                provenance_urn = EXCLUDED.provenance_urn
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (str(from_u), str(to_u), predicate, float(weight), provenance_urn),
                    )
                    inserted = bool(cur.fetchone()[0])
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise NeonUnreachable(
                    f"Neon attach_edge failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return {
            "receipt_type": "neon_kb_edge_attach",
            "from_doc_id": str(from_u),
            "to_doc_id": str(to_u),
            "predicate": predicate,
            "weight": float(weight),
            "inserted": inserted,
        }

    def get_provenance(self, doc_id: Any) -> list[KBProvenanceRow]:
        doc_u = _coerce_uuid(doc_id, label="doc_id")
        self._ensure_schema_ready()
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT receipt_urn, doc_id, source_url, source_doi,
                               retrieval_method, retrieved_at
                        FROM kbu_provenance
                        WHERE doc_id = %s
                        ORDER BY retrieved_at DESC
                        """,
                        (str(doc_u),),
                    )
                    rows = cur.fetchall()
            except Exception as exc:
                raise NeonUnreachable(
                    f"Neon get_provenance failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return [
            KBProvenanceRow(
                receipt_urn=r[0],
                doc_id=_coerce_uuid(r[1], label="doc_id"),
                source_url=r[2],
                source_doi=r[3],
                retrieval_method=r[4],
                retrieved_at=str(r[5]),
            )
            for r in rows
        ]

    def record_promotion_receipt(
        self,
        *,
        receipt_urn: str,
        idempotency_key: str,
        from_tier: str,
        to_tier: str,
        promoted_count: int,
        skipped_duplicates: int = 0,
    ) -> dict:
        if not receipt_urn.startswith("urn:mica:promotion:"):
            raise ValueError("receipt_urn must start with urn:mica:promotion:")
        if not idempotency_key or ":" not in idempotency_key:
            raise ValueError("idempotency_key must be colon-separated triple")
        self._ensure_schema_ready()
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO kbu_promotion_receipts
                            (receipt_urn, idempotency_key, from_tier, to_tier,
                             promoted_count, skipped_duplicates)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (
                            receipt_urn,
                            idempotency_key,
                            from_tier,
                            to_tier,
                            int(promoted_count),
                            int(skipped_duplicates),
                        ),
                    )
                    row = cur.fetchone()
                    inserted = bool(row[0]) if row else False
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise NeonUnreachable(
                    f"Neon record_promotion_receipt failed: {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        return {
            "receipt_type": "neon_kb_promotion_receipt",
            "receipt_urn": receipt_urn,
            "idempotency_key": idempotency_key,
            "from_tier": from_tier,
            "to_tier": to_tier,
            "promoted_count": int(promoted_count),
            "skipped_duplicates": int(skipped_duplicates),
            "inserted": inserted,
        }


class NeonUnreachable(RuntimeError):
    """Raised when Neon Postgres cannot be reached from the calling env.

    The Command Kernel handler must catch this and surface it as
    `degraded_reason: "neon_unreachable"` in the response. No silent fallback.
    """


# Public symbol for the integration contract.
DEFAULT_NEON_KB_STORE_BACKEND = "neon"
