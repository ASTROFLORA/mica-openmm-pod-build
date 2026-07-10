"""timescale_user_rag_store.py

Multi-tenant (per-user) RAG persistence for personalized retrieval.

This is designed to support:
- Massive ingestion (e.g., UniProt, KEGG, PubMed, internal notes)
- Per-user isolation via `user_id` (+ optional `collection`)
- Storing rich metadata/external IDs for traceability
- Hybrid retrieval (built-in FTS + pgvector)

Tables are created by scripts/migrate_user_rag_timescale.py.

Env:
- TIMESCALE_SERVICE_URL (preferred) / TIMESCALE_DSN / TIMESCALE_URL
- PGPASSWORD
- USER_RAG_SCHEMA (default: user_rag)
- USER_RAG_EMBEDDING_DIM (default: 1536)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from .pg_async import AsyncPGStoreBase, validate_ident
from .pg_types import jsonb


@dataclass(frozen=True)
class UserChunkRow:
    user_id: str
    collection: str
    doc_key: str
    chunk_index: int
    content: str
    hybrid_score: float
    metadata: dict[str, Any]


class TimescaleUserRAGStore:
    def __init__(
        self,
        database_url: Optional[str] = None,
        *,
        schema: Optional[str] = None,
        embedding_dim: Optional[int] = None,
        write_history: Optional[bool] = None,
    ) -> None:
        self._base = AsyncPGStoreBase(database_url, role="timescale")

        self.schema = validate_ident(schema or os.getenv("USER_RAG_SCHEMA") or "user_rag")

        raw_dim = embedding_dim or int(os.getenv("USER_RAG_EMBEDDING_DIM") or "1536")
        if raw_dim <= 0 or raw_dim > 8192:
            raise ValueError(f"invalid embedding_dim={raw_dim}")
        self.embedding_dim = raw_dim

        if write_history is None:
            write_history = (os.getenv("USER_RAG_WRITE_HISTORY") or "1").strip().lower() not in {"0", "false", "no"}
        self.write_history = bool(write_history)

    @property
    def pool(self):
        return self._base.pool

    async def initialize(self) -> None:
        await self._base.initialize()

    async def close(self) -> None:
        await self._base.close()

    async def upsert_document(
        self,
        *,
        user_id: str,
        doc_key: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        source: Optional[str] = None,
        collection: str = "default",
        external_ids: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if not user_id:
            raise ValueError("user_id required")
        if not doc_key:
            raise ValueError("doc_key required")

        await self.initialize()

        external_ids_json = jsonb(external_ids)
        metadata_json = jsonb(metadata)

        async with self.pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.schema}.user_documents(
                    user_id, collection, doc_key, source, title, content, external_ids, metadata, updated_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,NOW())
                ON CONFLICT (user_id, collection, doc_key) DO UPDATE SET
                    source = EXCLUDED.source,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    external_ids = EXCLUDED.external_ids,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW();
                """,
                user_id,
                collection,
                doc_key,
                source,
                title,
                content,
                external_ids_json,
                metadata_json,
            )

    async def insert_chunk(
        self,
        *,
        user_id: str,
        doc_key: str,
        chunk_index: int,
        content: str,
        embedding: Optional[str],
        collection: str = "default",
        source: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if not user_id:
            raise ValueError("user_id required")
        if not doc_key:
            raise ValueError("doc_key required")
        if chunk_index < 0:
            raise ValueError("chunk_index must be >= 0")
        if not content:
            raise ValueError("content required")

        await self.initialize()

        metadata_json = jsonb(metadata)

        async with self.pool.acquire() as conn:
            lock_key = f"{user_id}|{collection}|{doc_key}|{chunk_index}"
            async with conn.transaction():
                # Serialize writes per logical chunk to prevent duplicates under concurrency.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1));", lock_key)

                if self.write_history:
                    await conn.execute(
                        f"""
                        INSERT INTO {self.schema}.user_doc_chunks_history(
                            user_id, collection, doc_key, chunk_index, content, embedding, source, metadata
                        )
                        VALUES ($1,$2,$3,$4,$5,$6::vector,$7,$8::jsonb)
                        """,
                        user_id,
                        collection,
                        doc_key,
                        chunk_index,
                        content,
                        embedding,
                        source,
                        metadata_json,
                    )

                await conn.execute(
                    f"""
                    DELETE FROM {self.schema}.user_doc_chunks
                    WHERE user_id = $1 AND collection = $2 AND doc_key = $3 AND chunk_index = $4;
                    """,
                    user_id,
                    collection,
                    doc_key,
                    chunk_index,
                )

                await conn.execute(
                    f"""
                    INSERT INTO {self.schema}.user_doc_chunks(
                        user_id, collection, doc_key, chunk_index, content, embedding, source, metadata
                    )
                    VALUES ($1,$2,$3,$4,$5,$6::vector,$7,$8::jsonb)
                    """,
                    user_id,
                    collection,
                    doc_key,
                    chunk_index,
                    content,
                    embedding,
                    source,
                    metadata_json,
                )

    async def search_chunks_hybrid(
        self,
        *,
        user_id: str,
        query_text: str,
        query_embedding: Optional[str],
        collection: str = "default",
        session_id: Optional[str] = None,
        limit: int = 10,
    ) -> list[UserChunkRow]:
        if not user_id:
            raise ValueError("user_id required")
        if limit <= 0 or limit > 100:
            raise ValueError("limit out of range")

        await self.initialize()

        where_clauses = ["c.user_id = $1", "c.collection = $2"]
        params: list[Any] = [user_id, collection, query_text, query_embedding]
        if session_id:
            where_clauses.append(f"COALESCE(c.metadata->>'session_id', '') = ${len(params) + 1}")
            params.append(session_id)

        where_sql = " AND ".join(where_clauses)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH q AS (
                    SELECT websearch_to_tsquery('english', $3) AS tsq, $4::vector AS qv
                )
                SELECT
                    c.user_id,
                    c.collection,
                    c.doc_key,
                    c.chunk_index,
                    c.content,
                    (to_tsvector('english', c.content) @@ q.tsq) AS text_match,
                    ts_rank_cd(to_tsvector('english', c.content), q.tsq) AS text_score,
                    CASE WHEN q.qv IS NULL THEN NULL ELSE (-(c.embedding <=> q.qv)) END AS vec_score,
                    (0.6 * ts_rank_cd(to_tsvector('english', c.content), q.tsq)
                     + 0.4 * COALESCE((-(c.embedding <=> q.qv)), 0.0)) AS hybrid_score,
                    c.metadata
                FROM {self.schema}.user_doc_chunks c, q
                WHERE {where_sql}
                ORDER BY text_match DESC, hybrid_score DESC, c.timestamp DESC
                LIMIT {int(limit)};
                """,
                *params,
            )

        out: list[UserChunkRow] = []
        for r in rows:
            md = r["metadata"]
            if isinstance(md, str):
                # Defensive: some schemas may return JSONB as string.
                import json

                md = json.loads(md)
            out.append(
                UserChunkRow(
                    user_id=r["user_id"],
                    collection=r["collection"],
                    doc_key=r["doc_key"],
                    chunk_index=int(r["chunk_index"]),
                    content=r["content"],
                    hybrid_score=float(r["hybrid_score"]),
                    metadata=md or {},
                )
            )
        return out
