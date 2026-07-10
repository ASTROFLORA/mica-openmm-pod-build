"""milvus_user_rag_store.py

Per-user Milvus/Zilliz derived index for user-scoped RAG chunks.

Design goals (see MICAV4 canonical docs):
- Tenant-scoped retrieval (always filter by user_id; optionally by session_id)
- Milvus is an index; Timescale remains the truth
- Optional dependency: runs without pymilvus installed (no hard import failures)

Env:
- ZILLIZ_URI / ZILLIZ_TOKEN (preferred)
- MILVUS_URI / MILVUS_TOKEN (fallback)
- MICA_USER_RAG_MILVUS_COLLECTION (default: mica_user_rag_chunks_v1)
- MICA_EMBED_DIM (required to create collection)
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)

try:
    from mica import embedding_service
except Exception:  # pragma: no cover
    embedding_service = None  # type: ignore


@dataclass(frozen=True)
class MilvusUserChunkHit:
    user_id: str
    collection: str
    doc_key: str
    chunk_index: int
    content: str
    score: float
    metadata: dict[str, Any]


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _resolve_uri_token() -> tuple[str, str]:
    uri = _env("ZILLIZ_URI") or _env("MILVUS_URI")
    token = _env("ZILLIZ_TOKEN") or _env("MILVUS_TOKEN")
    return uri, token


def _chunk_id(user_id: str, collection: str, doc_key: str, chunk_index: int) -> str:
    h = hashlib.sha256()
    h.update(user_id.encode("utf-8"))
    h.update(b"|")
    h.update(collection.encode("utf-8"))
    h.update(b"|")
    h.update(doc_key.encode("utf-8"))
    h.update(b"|")
    h.update(str(int(chunk_index)).encode("utf-8"))
    return h.hexdigest()  # 64 chars


class MilvusUserRAGStore:
    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        token: Optional[str] = None,
        collection_name: Optional[str] = None,
        embed_dim: Optional[int] = None,
        enable: Optional[bool] = None,
    ) -> None:
        self.uri = (uri or "").strip()
        self.token = (token or "").strip()
        if not self.uri or not self.token:
            u, t = _resolve_uri_token()
            self.uri = self.uri or u
            self.token = self.token or t

        self.collection_name = (collection_name or _env("MICA_USER_RAG_MILVUS_COLLECTION") or "mica_user_rag_chunks_v1")

        raw_dim = embed_dim if embed_dim is not None else int(_env("MICA_EMBED_DIM") or "0")
        self.embed_dim = int(raw_dim)

        if enable is None:
            enable = (_env("MICA_ENABLE_MILVUS_USER_RAG") or "1").lower() not in {"0", "false", "no"}
        self.enable = bool(enable)

        self._connected = False
        self._collection: Any = None

    def configured(self) -> bool:
        if not self.enable:
            return False
        if not self.uri or not self.token:
            return False
        try:
            importlib.import_module("pymilvus")
        except Exception:
            return False
        return True

    async def initialize(self) -> bool:
        if not self.configured():
            return False
        if self._connected and self._collection is not None:
            return True

        def _init_sync() -> bool:
            pymilvus = importlib.import_module("pymilvus")
            Collection = getattr(pymilvus, "Collection")
            CollectionSchema = getattr(pymilvus, "CollectionSchema")
            DataType = getattr(pymilvus, "DataType")
            FieldSchema = getattr(pymilvus, "FieldSchema")
            connections = getattr(pymilvus, "connections")
            utility = getattr(pymilvus, "utility")
            connections.connect(alias="default", uri=self.uri, token=self.token)

            if utility.has_collection(self.collection_name):
                self._collection = Collection(self.collection_name)
                self._collection.load()
                return True

            if self.embed_dim <= 0:
                # Contract: never silently create a collection with an implicit dim.
                raise RuntimeError("MICA_EMBED_DIM is required to create Milvus collection")

            schema = CollectionSchema(
                fields=[
                    FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
                    FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="collection", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="doc_key", dtype=DataType.VARCHAR, max_length=512),
                    FieldSchema(name="chunk_index", dtype=DataType.INT32),
                    FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=int(self.embed_dim)),
                    FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=4096),
                    FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=128),
                    FieldSchema(name="ts", dtype=DataType.INT64),
                    FieldSchema(name="metadata", dtype=DataType.JSON),
                ],
                description="User-scoped RAG chunk index (derived; truth in Timescale)",
                enable_dynamic_field=True,
            )

            self._collection = Collection(name=self.collection_name, schema=schema)
            try:
                self._collection.create_index(
                    "vector",
                    {
                        "index_type": "IVF_FLAT",
                        "metric_type": "COSINE",
                        "params": {"nlist": 1024},
                    },
                )
            except Exception:
                # Best-effort; collection still usable.
                pass

            self._collection.load()
            return True

        try:
            ok = await asyncio.to_thread(_init_sync)
        except Exception as exc:
            logger.warning("MilvusUserRAGStore init failed: %s", exc)
            return False

        self._connected = True
        return ok

    async def _embed(self, text: str) -> Optional[list[float]]:
        if not text:
            return None
        if embedding_service is None:
            return None
        try:
            vec = await embedding_service.embed(text)
        except Exception:
            return None
        if not isinstance(vec, list) or not vec:
            return None
        if self.embed_dim <= 0:
            return None
        if len(vec) != self.embed_dim:
            # Don't silently slice/pad; dims must be pinned.
            return None
        return vec

    async def insert_chunk(
        self,
        *,
        user_id: str,
        collection: str,
        doc_key: str,
        chunk_index: int,
        content: str,
        source: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        embedding: Optional[list[float]] = None,
    ) -> bool:
        if not user_id:
            raise ValueError("user_id required")
        if not doc_key:
            raise ValueError("doc_key required")
        if chunk_index < 0:
            raise ValueError("chunk_index must be >= 0")
        if not content:
            return False

        ok = await self.initialize()
        if not ok or self._collection is None:
            return False

        vec = embedding
        if vec is None:
            vec = await self._embed(content)
        if vec is None:
            return False

        cid = _chunk_id(user_id, collection, doc_key, chunk_index)
        ts = int(time.time() * 1000)
        src = (source or "").strip()[:64]
        sid = (session_id or "").strip()[:128]
        md = metadata or {}

        def _write_sync() -> bool:
            assert self._collection is not None
            try:
                self._collection.delete(expr=f'chunk_id == "{cid}"')
            except Exception:
                pass
            self._collection.insert(
                [
                    [cid],
                    [user_id],
                    [collection],
                    [doc_key],
                    [int(chunk_index)],
                    [vec],
                    [content[:4096]],
                    [src],
                    [sid],
                    [ts],
                    [md],
                ]
            )
            return True

        try:
            return await asyncio.to_thread(_write_sync)
        except Exception as exc:
            logger.warning("Milvus chunk insert failed (user=%s doc=%s): %s", user_id, doc_key, exc)
            return False

    async def search_chunks(
        self,
        *,
        user_id: str,
        query_text: str,
        collection: str,
        limit: int = 10,
        session_id: Optional[str] = None,
        query_embedding: Optional[list[float]] = None,
    ) -> list[MilvusUserChunkHit]:
        if not user_id:
            raise ValueError("user_id required")
        if limit <= 0 or limit > 100:
            raise ValueError("limit out of range")

        ok = await self.initialize()
        if not ok or self._collection is None:
            return []

        qv = query_embedding
        if qv is None:
            qv = await self._embed(query_text)
        if qv is None:
            return []

        expr_parts = [f'user_id == "{user_id}"', f'collection == "{collection}"']
        sid = (session_id or "").strip()
        if sid:
            expr_parts.append(f'session_id == "{sid}"')
        expr = " and ".join(expr_parts)

        def _search_sync() -> list[MilvusUserChunkHit]:
            assert self._collection is not None
            self._collection.load()
            res = self._collection.search(
                [qv],
                anns_field="vector",
                param={"nprobe": 16},
                limit=int(limit),
                expr=expr,
                output_fields=["doc_key", "chunk_index", "content", "metadata"],
            )
            out: list[MilvusUserChunkHit] = []
            for hits in res:
                for hit in hits:
                    ent = hit.entity
                    md = ent.get("metadata") or {}
                    out.append(
                        MilvusUserChunkHit(
                            user_id=user_id,
                            collection=collection,
                            doc_key=ent.get("doc_key") or "",
                            chunk_index=int(ent.get("chunk_index") or 0),
                            content=ent.get("content") or "",
                            score=float(1.0 - hit.distance),
                            metadata=md if isinstance(md, dict) else {},
                        )
                    )
            return out

        try:
            return await asyncio.to_thread(_search_sync)
        except Exception:
            return []


__all__ = ["MilvusUserRAGStore", "MilvusUserChunkHit"]
