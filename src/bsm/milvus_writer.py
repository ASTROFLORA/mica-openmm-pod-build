#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GAP-4: 4-Vector Protein-Only → Milvus Write Path (schema v3)
=============================================================

Writes ``MultiModalEmbedding`` + ``BudoV3`` metadata rows to the
``protein_multimodal_rag_v1`` Zilliz/Milvus collection.

Schema invariants (PINNED — schema_version = 3):
    prot_t5_vec  : FLOAT_VECTOR 1024D  ← embedding_sequence_space (ProtT5-XL-U50)
    esm2_vec     : FLOAT_VECTOR 1280D  ← embedding_esm2 / embedding_sequence_esmc (ESM-C)
    node2vec_vec : FLOAT_VECTOR  512D  ← embedding_network_space (STRING/KG node2vec)
    dct_vec      : FLOAT_VECTOR  480D  ← embedding_dct_domain (DCT int8→float32 avg-pool)
    schema_version: INT32 = 3          (af2_vec + bm25_sparse deferred; Zilliz Serverless = 4 vec cap)
    model_id     : VARCHAR

Usage::

    writer = ProteinMultimodalWriter(uri=ZILLIZ_URI, token=ZILLIZ_TOKEN)
    writer.connect()
    writer.ensure_collection()          # creates if absent, no-op if present
    writer.write(budo_obj, embedding)   # single row
    writer.write_batch(list_of_pairs)   # batched insert
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — PINNED
# ---------------------------------------------------------------------------

COLLECTION_NAME = "protein_multimodal_rag_v1"
SCHEMA_VERSION = 3
MODEL_ID = "bsm-4vec-protein-v3"

# Zero-vector sentinels (inserted when a model was not run)
_ZERO_1024 = [0.0] * 1024
_ZERO_1280 = [0.0] * 1280
_ZERO_512 = [0.0] * 512
# _ZERO_384 deferred (af2_vec removed — Zilliz Serverless 4-vec cap)
_ZERO_480 = [0.0] * 480

# ---------------------------------------------------------------------------
# BM25 tokeniser (simple TF-based, no corpus IDF needed for writes)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _bm25_tokenize(text: str) -> Dict[int, float]:
    """Return {token_hash: tf_score} sparse dict for a text string.

    Uses a 24-bit FNV-1a hash for term → uint32 mapping so the sparse
    vector stays within pymilvus SPARSE_FLOAT_VECTOR limits.  No IDF
    is applied at write time (IDF is a query-side concern for BM25).
    Tokens are lower-cased ASCII-alphanumeric splits.

    Args:
        text: Any text string (name, function annotation, organism …).

    Returns:
        Dict[int, float] suitable for pymilvus SPARSE_FLOAT_VECTOR insertion.
        Empty dict when text is empty — pymilvus accepts this.
    """
    if not text:
        return {}

    term_freq: Dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        term_freq[tok] = term_freq.get(tok, 0) + 1

    total = sum(term_freq.values()) or 1
    sparse: Dict[int, float] = {}
    for tok, freq in term_freq.items():
        # FNV-1a 32-bit hash, masked to 24 bits to stay well within uint32
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16) & 0xFFFFFF
        sparse[h] = freq / total  # normalised TF score in [0, 1]

    return sparse


# ---------------------------------------------------------------------------
# Row builder helpers
# ---------------------------------------------------------------------------

def _vec_to_list(arr: Optional[np.ndarray], fallback: List[float]) -> List[float]:
    """Convert numpy array to python list, returning fallback if None.

    Applies L2 normalization for COSINE metric correctness.
    """
    if arr is None:
        return fallback
    v = arr.astype(np.float32)
    _norm = np.linalg.norm(v)
    if _norm > 0:
        v = v / _norm
    return v.tolist()


def _extract_uniprot(budo) -> str:  # BudoV3
    """Extract first UniProt accession from BudoV3 cross_references."""
    xrefs = getattr(budo, "cross_references", None) or []
    for ref in xrefs:
        db = getattr(ref, "database", "") or ""
        if db.lower() in ("uniprot", "swissprot", "trembl"):
            ident = getattr(ref, "identifier", "") or ""
            if ident:
                return ident
    # Fall back to canonical_name (safe truncation to 32 chars)
    return (getattr(budo, "canonical_name", None) or "unknown")[:32]


def _build_row(budo, embedding) -> Dict[str, Any]:  # BudoV3, MultiModalEmbedding
    """Map a (BudoV3, MultiModalEmbedding) pair → Milvus insert row dict.

    All 14 fields of ``protein_multimodal_rag_v1`` (schema v3) are populated:
      * Scalar metadata from BudoV3 (id, budo_id, uniprot_id, canonical_name,
        organism, function_text, ensp_id, schema_version, model_id)
      * 5 dense vectors from MultiModalEmbedding (zero-filled when absent)
      * BM25 sparse from provenance / function text
      * schema_version and model_id are PINNED constants

    Args:
        budo: A ``BudoV3`` instance.
        embedding: A ``MultiModalEmbedding`` instance from multi_model_router.

    Returns:
        Dict ready for ``Collection.insert()``.
    """
    budo_id: str = getattr(budo, "budoId", "") or ""
    canonical_name: str = getattr(budo, "canonical_name", "") or ""
    organism: str = getattr(budo, "organism", "") or ""

    # Build function_text for BM25 from first functional state description
    func_text = ""
    fs = getattr(budo, "functionalState", None)
    if fs is not None:
        func_text = (
            getattr(fs, "description", None)
            or getattr(fs, "state", None)
            or ""
        ) or ""
    if not func_text:
        # Fall back to metadata keywords
        meta = getattr(budo, "metadata", None) or {}
        func_text = meta.get("function", meta.get("description", ""))
    func_text = (func_text or "")[:4096]

    uniprot_id = _extract_uniprot(budo)

    # Stable row ID: sha256(budo_id + model_id) truncated to 32 chars
    row_id = hashlib.sha256(f"{budo_id}:{MODEL_ID}".encode()).hexdigest()[:32]

    ensp_id: str = (getattr(budo, "ensp_id", None) or "")[:64]

    return {
        "id": row_id,
        "budo_id": budo_id[:128],
        "uniprot_id": uniprot_id[:32],
        "canonical_name": canonical_name[:256],
        "organism": organism[:256],
        "function_text": func_text,
        "ensp_id": ensp_id,
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        # 4 dense protein-model vectors (schema v3; BioLinkBERT + SciBERT + af2 + bm25 deferred)
        "prot_t5_vec": _vec_to_list(
            getattr(embedding, "embedding_sequence_space", None), _ZERO_1024
        ),
        "esm2_vec": _vec_to_list(
            getattr(embedding, "embedding_esm2", None)
            or getattr(embedding, "embedding_sequence_esmc", None),
            _ZERO_1280,
        ),
        "node2vec_vec": _vec_to_list(
            getattr(embedding, "embedding_network_space", None), _ZERO_512
        ),
        "dct_vec": _vec_to_list(
            getattr(embedding, "embedding_dct_domain", None), _ZERO_480
        ),
    }


# ---------------------------------------------------------------------------
# Writer class
# ---------------------------------------------------------------------------

class ProteinMultimodalWriter:
    """Synchronous write client for ``protein_multimodal_rag_v1``.

    Manages connection lifecycle, collection creation/discovery, and
    batch-optimised row insertion.

    Example::

        writer = ProteinMultimodalWriter(uri=ZILLIZ_URI, token=ZILLIZ_TOKEN)
        writer.connect()
        writer.ensure_collection()
        ids = writer.write_batch([(budo1, emb1), (budo2, emb2)])
    """

    def __init__(
        self,
        uri: str,
        token: str,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        self._uri = uri
        self._token = token
        self.collection_name = collection_name
        self._collection = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish connection to Zilliz / Milvus."""
        try:
            from pymilvus import connections

            connections.connect(
                alias="default",
                uri=self._uri,
                token=self._token,
            )
            self._connected = True
            logger.info("ProteinMultimodalWriter connected to %s", self._uri)
        except Exception as exc:
            logger.error("Connection failed: %s", exc)
            raise

    def disconnect(self) -> None:
        """Disconnect from Milvus."""
        if self._connected:
            from pymilvus import connections

            connections.disconnect("default")
            self._connected = False

    # ------------------------------------------------------------------
    # Collection lifecycle
    # ------------------------------------------------------------------

    def ensure_collection(self) -> None:
        """Create ``protein_multimodal_rag_v1`` if absent; load if present.

        Idempotent — safe to call every startup.
        Raises RuntimeError when called before ``connect()``.
        """
        if not self._connected:
            raise RuntimeError("Call connect() before ensure_collection()")

        from pymilvus import Collection, utility
        from bsm.milvus_integration import BSMMilvusSchema

        if utility.has_collection(self.collection_name):
            self._collection = Collection(self.collection_name)
            self._collection.load()
            logger.info(
                "Collection '%s' already exists — attached and loaded.",
                self.collection_name,
            )
            return

        schema = BSMMilvusSchema.get_protein_multimodal_schema()
        self._collection = Collection(name=self.collection_name, schema=schema)
        logger.info("Created collection '%s'.", self.collection_name)

        # Create indexes on all searchable fields
        index_specs = BSMMilvusSchema.get_multimodal_index_params()
        for field_name, idx_params in index_specs:
            self._collection.create_index(field_name=field_name, index_params=idx_params)
            logger.debug("Index created on '%s'.", field_name)

        self._collection.load()
        logger.info("Collection '%s' indexed and loaded.", self.collection_name)

    def _require_collection(self) -> None:
        if self._collection is None:
            raise RuntimeError("Call ensure_collection() first.")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def write(self, budo, embedding) -> str:
        """Insert a single (BudoV3, MultiModalEmbedding) pair.

        Args:
            budo: ``BudoV3`` instance.
            embedding: ``MultiModalEmbedding`` instance.

        Returns:
            Stable row id string (sha256 of budo_id + model_id).

        Raises:
            RuntimeError: when collection is not ready.
        """
        self._require_collection()
        row = _build_row(budo, embedding)
        # pymilvus insert expects a list of rows OR columnar dicts
        _insert_rows(self._collection, [row])
        logger.debug("Inserted row id=%s budo_id=%s", row["id"], row["budo_id"])
        return row["id"]

    def write_batch(
        self,
        pairs: List[Tuple[Any, Any]],
        batch_size: int = 100,
    ) -> List[str]:
        """Insert a list of (BudoV3, MultiModalEmbedding) pairs in batches.

        Args:
            pairs: List of (budo, embedding) tuples.
            batch_size: Rows per Milvus insert call (default 100).

        Returns:
            List of stable row id strings, same order as input.
        """
        self._require_collection()
        rows = [_build_row(b, e) for b, e in pairs]
        ids: List[str] = [r["id"] for r in rows]

        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            _insert_rows(self._collection, chunk)
            logger.info(
                "Inserted batch %d-%d / %d",
                start,
                min(start + batch_size, len(rows)) - 1,
                len(rows),
            )

        return ids


# ---------------------------------------------------------------------------
# Low-level insert helper (columnar format for pymilvus efficiency)
# ---------------------------------------------------------------------------

def _insert_rows(collection, rows: List[Dict[str, Any]]) -> None:
    """Convert list-of-dicts to columnar format and call collection.insert().

    pymilvus performs significantly better with columnar inserts than
    row-by-row dicts when batches > 10 rows.

    Args:
        collection: pymilvus Collection object.
        rows: List of row dicts (all must share the same field keys).
    """
    if not rows:
        return

    keys = list(rows[0].keys())
    columnar: Dict[str, List[Any]] = {k: [r[k] for r in rows] for k in keys}
    collection.insert(columnar)
    collection.flush()


# ---------------------------------------------------------------------------
# Public convenience function
# ---------------------------------------------------------------------------

def write_protein_to_milvus(
    budo,
    embedding,
    uri: str,
    token: str,
    collection_name: str = COLLECTION_NAME,
) -> str:
    """One-shot connect + ensure_collection + write + disconnect.

    Convenience wrapper for single-protein use cases.  Prefer
    ``ProteinMultimodalWriter`` for batch workloads.

    Args:
        budo: ``BudoV3`` instance.
        embedding: ``MultiModalEmbedding`` instance.
        uri: Zilliz/Milvus endpoint URI.
        token: Authentication token.
        collection_name: Target collection (default: ``protein_multimodal_rag_v1``).

    Returns:
        Stable row id string.
    """
    writer = ProteinMultimodalWriter(uri=uri, token=token, collection_name=collection_name)
    try:
        writer.connect()
        writer.ensure_collection()
        return writer.write(budo, embedding)
    finally:
        writer.disconnect()


__all__ = [
    "COLLECTION_NAME",
    "SCHEMA_VERSION",
    "MODEL_ID",
    "ProteinMultimodalWriter",
    "write_protein_to_milvus",
    "_build_row",
    "_bm25_tokenize",
]
