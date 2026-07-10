"""Vector Store integration for Zilliz / Milvus with a built-in stub fallback.

Features:
- Zilliz/Milvus (preferred when configured): lazy client init, ensure collection,
    upsert plan embeddings, similarity search.
- Stub (in-memory) fallback: activates automatically when VECTOR_BACKEND=stub or
    when Zilliz/Milvus isn't available/configured. Uses the same public API so that
    the rest of the app doesn't need to change. Embeddings come from
    ``embedding_service`` if available, otherwise a deterministic hash.
"""
from __future__ import annotations
import os, time, hashlib
from typing import List, Dict, Any, Optional

try:
    from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility
except Exception:  # pragma: no cover
    connections = None  # type: ignore
    Collection = None  # type: ignore
    FieldSchema = None  # type: ignore
    CollectionSchema = None  # type: ignore
    DataType = None  # type: ignore
    utility = None  # type: ignore

try:
    from . import embedding_service
except Exception:  # pragma: no cover
    embedding_service = None  # type: ignore

BACKEND = (os.getenv("VECTOR_BACKEND", "stub") or "stub").lower()
ZILLIZ_URI = os.getenv("ZILLIZ_URI")
ZILLIZ_TOKEN = os.getenv("ZILLIZ_TOKEN")
VECTOR_COLLECTION = os.getenv("MICA_VECTOR_COLLECTION", "mica_plans")
EMBED_DIM = int(os.getenv("MICA_EMBED_DIM", "0"))  # dynamic if 0

_initialized = False
_collection: Any = None  # type: ignore
_stub_store: List[Dict[str, Any]] = []  # used when running in stub mode


def _connect():
    global _initialized
    if _initialized or connections is None:
        return
    if not ZILLIZ_URI or not ZILLIZ_TOKEN:
        return
    try:
        connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_TOKEN)
        _initialized = True
    except Exception:
        _initialized = False


def _ensure_collection():
    global _collection
    if Collection is None:
        return None
    _connect()
    if not _initialized:
        return None
    if utility.has_collection(VECTOR_COLLECTION):
        _collection = Collection(VECTOR_COLLECTION)
        return _collection
    fields = [
        FieldSchema(name="plan_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema(name="ts", dtype=DataType.INT64),
        FieldSchema(name="strategy", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=EMBED_DIM),
        # Campos adicionales para memoria semántica
        FieldSchema(name="context", dtype=DataType.VARCHAR, max_length=2048),
        FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="entities", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="topics", dtype=DataType.VARCHAR, max_length=512),
    ]
    schema = CollectionSchema(fields, description="MICA plans and conversation context store")
    _collection = Collection(name=VECTOR_COLLECTION, schema=schema)
    # create index
    try:
        _collection.create_index("vector", {"index_type": "IVF_FLAT", "metric_type": "L2", "params": {"nlist": 64}})
    except Exception:
        pass
    return _collection


def _stub_available() -> bool:
    # always available in stub mode
    return True


def available() -> bool:
    """Returns True when a vector backend is ready.

    - Zilliz/Milvus: collection ensured and client initialized
    - Stub: always True
    """
    if BACKEND == "stub" or connections is None or not (ZILLIZ_URI and ZILLIZ_TOKEN):
        return _stub_available()
    return bool(_ensure_collection())


async def ensure_dim():
    global EMBED_DIM
    if EMBED_DIM == 0 and embedding_service:
        try:
            EMBED_DIM = await embedding_service.dimension()
        except Exception:
            EMBED_DIM = 16
    if EMBED_DIM == 0:
        EMBED_DIM = 16
    return EMBED_DIM


async def _simple_embed(text: str) -> List[float]:
    if embedding_service:
        return await embedding_service.embed(text)
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [int.from_bytes(h[i:i+2], "big")/65535.0 for i in range(0, 32, 2)]


async def upsert_plan(plan_id: str, request: str, strategy: str) -> bool:
    """Upsert a plan embedding into the configured backend or stub store."""
    vec_full = await _simple_embed(request)
    dim = await ensure_dim()
    vec = vec_full[:dim]
    # Stub mode
    if BACKEND == "stub" or connections is None or not (ZILLIZ_URI and ZILLIZ_TOKEN):
        # Update if exists, else append
        ts_ms = int(time.time() * 1000)
        for rec in _stub_store:
            if rec.get("plan_id") == plan_id:
                rec.update({"ts": ts_ms, "strategy": strategy, "vector": vec})
                return True
        _stub_store.append({"plan_id": plan_id, "ts": ts_ms, "strategy": strategy, "vector": vec})
        return True
    # Zilliz / Milvus
    col = _ensure_collection()
    if not col:
        return False
    try:
        col.insert([[plan_id], [int(time.time()*1000)], [strategy], [vec]])
        return True
    except Exception:
        return False


async def query_similar(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Return nearest plans to the query using the configured backend.

    For stub mode, we compute cosine similarity and convert to a distance-like score
    as ``1 - cosine`` to keep monotonicity with the Zilliz L2-style distance.
    """
    vec_full = await _simple_embed(query)
    dim = await ensure_dim()
    q = vec_full[:dim]
    # Stub mode
    if BACKEND == "stub" or connections is None or not (ZILLIZ_URI and ZILLIZ_TOKEN):
        def _norm(v: List[float]) -> float:
            return sum(x*x for x in v) ** 0.5 or 1.0
        nq = _norm(q)
        scored: List[Dict[str, Any]] = []
        for rec in _stub_store:
            v = rec.get("vector") or []
            if not v:
                continue
            nv = _norm(v)
            cos = sum(a*b for a,b in zip(q, v)) / (nq*nv or 1.0)
            distance = 1.0 - cos  # smaller is closer
            scored.append({
                "plan_id": rec["plan_id"],
                "distance": float(distance),
                "strategy": rec.get("strategy"),
                "ts": rec.get("ts"),
            })
        scored.sort(key=lambda r: r["distance"])  # asc distance
        return scored[:top_k]
    # Zilliz / Milvus
    col = _ensure_collection()
    if not col:
        return []
    try:
        col.load()
        res = col.search([q], "vector", param={"nprobe": 16}, limit=top_k, output_fields=["strategy", "ts"])
        out: List[Dict[str, Any]] = []
        for hits in res:
            for hit in hits:
                out.append({
                    "plan_id": hit.id,
                    "distance": hit.distance,
                    "strategy": hit.entity.get("strategy"),
                    "ts": hit.entity.get("ts"),
                })
        return out
    except Exception:
        return []

# === FUNCIONES PARA MEMORIA SEMÁNTICA ===

async def upsert_plan_embedding(data: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert embedding de contexto de conversación para memoria semántica."""
    try:
        plan_id = data.get("plan_id")
        context = data.get("context", "")
        strategy = data.get("strategy", "conversation")
        ts = data.get("ts", int(time.time()))
        
        # Crear embedding del contexto
        vec_full = await _simple_embed(context)
        dim = await ensure_dim()
        vec = vec_full[:dim]
        
        # Stub mode
        if BACKEND == "stub" or connections is None or not (ZILLIZ_URI and ZILLIZ_TOKEN):
            # Actualizar si existe, sino agregar
            for rec in _stub_store:
                if rec.get("plan_id") == plan_id:
                    rec.update({
                        "ts": ts, 
                        "strategy": strategy, 
                        "vector": vec,
                        "context": context,
                        "session_id": data.get("session_id"),
                        "entities": data.get("entities", []),
                        "topics": data.get("topics", [])
                    })
                    return {"ok": True, "plan_id": plan_id}
            
            _stub_store.append({
                "plan_id": plan_id, 
                "ts": ts, 
                "strategy": strategy, 
                "vector": vec,
                "context": context,
                "session_id": data.get("session_id"),
                "entities": data.get("entities", []),
                "topics": data.get("topics", [])
            })
            return {"ok": True, "plan_id": plan_id}
        
        # Zilliz / Milvus
        col = _ensure_collection()
        if not col:
            return {"ok": False, "error": "Collection not available"}
        
        # Insertar en Zilliz
        col.insert([[
            plan_id, 
            ts, 
            strategy, 
            vec,
            context,
            data.get("session_id", ""),
            str(data.get("entities", [])),
            str(data.get("topics", []))
        ]])
        
        return {"ok": True, "plan_id": plan_id}
        
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def similarity_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Búsqueda semántica de contexto de conversaciones."""
    try:
        vec_full = await _simple_embed(query)
        dim = await ensure_dim()
        q = vec_full[:dim]
        
        # Stub mode
        if BACKEND == "stub" or connections is None or not (ZILLIZ_URI and ZILLIZ_TOKEN):
            def _norm(v: List[float]) -> float:
                return sum(x*x for x in v) ** 0.5 or 1.0
            
            nq = _norm(q)
            scored: List[Dict[str, Any]] = []
            
            for rec in _stub_store:
                v = rec.get("vector") or []
                if not v or "context" not in rec:
                    continue
                
                nv = _norm(v)
                cos = sum(a*b for a,b in zip(q, v)) / (nq*nv or 1.0)
                similarity = cos  # higher is more similar
                
                scored.append({
                    "plan_id": rec["plan_id"],
                    "score": float(similarity),
                    "context": rec.get("context", ""),
                    "session_id": rec.get("session_id", ""),
                    "entities": rec.get("entities", []),
                    "topics": rec.get("topics", []),
                    "timestamp": rec.get("ts")
                })
            
            scored.sort(key=lambda r: r["score"], reverse=True)  # desc similarity
            return scored[:limit]
        
        # Zilliz / Milvus
        col = _ensure_collection()
        if not col:
            return []
        
        col.load()
        res = col.search([q], "vector", param={"nprobe": 16}, limit=limit, 
                        output_fields=["strategy", "ts", "context", "session_id", "entities", "topics"])
        
        out: List[Dict[str, Any]] = []
        for hits in res:
            for hit in hits:
                out.append({
                    "plan_id": hit.id,
                    "score": 1.0 - hit.distance,  # Convertir distancia a similitud
                    "context": hit.entity.get("context", ""),
                    "session_id": hit.entity.get("session_id", ""),
                    "entities": eval(hit.entity.get("entities", "[]")),
                    "topics": eval(hit.entity.get("topics", "[]")),
                    "timestamp": hit.entity.get("ts")
                })
        
        return out
        
    except Exception as e:
        print(f"❌ Error en búsqueda semántica: {e}")
        return []

__all__ = ["available", "upsert_plan", "query_similar", "ensure_dim", "upsert_plan_embedding", "similarity_search"]
