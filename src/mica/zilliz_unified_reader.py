"""
Zilliz/Milvus unified reader utilities for Equipo 3 (validators).

Abstracts fetching vectors from collections to compare embedding modalities:
- Unified:    unified_protein_embeddings  (vector field: "vector",    id: "mudo_id")
- Spectral:   spectral_protein_embeddings (vector field: "spectral_embedding", id configurable)
- ESM3-only:  esm3_protein_embeddings     (vector field: "esm3_embedding",      id configurable)

If Zilliz isn't configured, functions return None and callers should handle fallbacks.
"""
from __future__ import annotations

import os
from typing import Optional, Dict, Any, List

try:
    from pymilvus import connections, Collection
except Exception:  # pragma: no cover
    connections = None  # type: ignore
    Collection = None  # type: ignore


ZILLIZ_URI = os.getenv("ZILLIZ_URI")
ZILLIZ_TOKEN = os.getenv("ZILLIZ_TOKEN")


def _connect() -> bool:
    if connections is None or not ZILLIZ_URI or not ZILLIZ_TOKEN:
        return False
    try:
        # Safe to call multiple times; Milvus SDK deduplicates alias
        connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_TOKEN)
        return True
    except Exception:
        return False


def fetch_vector(
    collection_name: str,
    id_field: str,
    id_value: str,
    vector_field: str,
    output_fields: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fetch one vector by ID from a Zilliz collection.

    Returns: { 'vector': List[float], 'meta': Dict[str, Any] } or None
    """
    if not _connect() or Collection is None:
        return None
    try:
        col = Collection(collection_name)
        expr = f'{id_field} == "{id_value}"'
        fields = [vector_field]
        if output_fields:
            for f in output_fields:
                if f not in fields:
                    fields.append(f)
        res = col.query(expr, output_fields=fields)
        if not res:
            return None
        row = res[0]
        vec = row.get(vector_field)
        if not isinstance(vec, list):
            return None
        meta = {k: v for k, v in row.items() if k != vector_field}
        return {"vector": vec, "meta": meta}
    except Exception:
        return None


def fetch_modality_vectors(
    mudo_id: str,
    collections: Dict[str, Dict[str, str]],
) -> Dict[str, Optional[List[float]]]:
    """
    Fetch available vectors for modalities for the given mudo_id.

    collections example:
    {
      'unified':  {'collection': 'unified_protein_embeddings', 'id_field': 'mudo_id', 'vector_field': 'vector'},
      'spectral': {'collection': 'spectral_protein_embeddings', 'id_field': 'mudo_id', 'vector_field': 'spectral_embedding'},
      'esm3':     {'collection': 'esm3_protein_embeddings',     'id_field': 'mudo_id', 'vector_field': 'esm3_embedding'}
    }
    """
    out: Dict[str, Optional[List[float]]] = {k: None for k in collections.keys()}
    for name, cfg in collections.items():
        rec = fetch_vector(
            cfg["collection"], cfg.get("id_field", "mudo_id"), mudo_id, cfg.get("vector_field", "vector")
        )
        out[name] = rec.get("vector") if rec else None
    return out


__all__ = ["fetch_vector", "fetch_modality_vectors"]


