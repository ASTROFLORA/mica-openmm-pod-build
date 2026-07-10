"""Real embedding service with provider selection and Redis caching.
Providers supported: nvidia (integrate), openai. Fallback to hash stub.
"""
from __future__ import annotations
import os, hashlib, json, logging, asyncio
from typing import List, Optional

import httpx

try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore

log = logging.getLogger(__name__)

# Configuration
PROVIDER = os.getenv("MICA_EMBED_PROVIDER", "nvidia").lower()

# NVIDIA embedding models configuration
NVIDIA_E5_MODEL = "nv-embedqa-e5-v5"  # 1024 dimensions
NVIDIA_MISTRAL_MODEL = "nv-embedqa-mistral-7b-v2"  # 4096 dimensions

# Select model based on preference (E5 for 1024D, Mistral for 4096D)
NVIDIA_MODEL = os.getenv("MICA_EMBED_MODEL", NVIDIA_E5_MODEL)  # Default to E5 1024D
OPENAI_MODEL = os.getenv("MICA_OPENAI_EMBED_MODEL", "text-embedding-3-large")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Use the correct NVIDIA embedding API keys and base URLs
if NVIDIA_MODEL == NVIDIA_E5_MODEL:
    NVIDIA_API_KEY = os.getenv("NVIDIA_NV_EMBEDQA_E5_V5_API_KEY")
    NVIDIA_BASE_URL = os.getenv("NVIDIA_NV_EMBEDQA_E5_V5_BASE_URL", "https://nim.nvidia.example/v1")
else:
    NVIDIA_API_KEY = os.getenv("NVIDIA_NV_EMBEDQA_MISTRAL_API_KEY")
    NVIDIA_BASE_URL = os.getenv("NVIDIA_NV_EMBEDQA_MISTRAL_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Fallback to other NVIDIA keys if specific ones not found
NVIDIA_API_KEY = NVIDIA_API_KEY or os.getenv("BIONEMO_API_KEY") or os.getenv("NEMOTRON_API_KEY") or os.getenv("NVIDIA_API_KEY")
REDIS_URL = os.getenv("REDIS_URL", "")
CACHE_TTL = int(os.getenv("MICA_EMBED_CACHE_TTL", "86400"))

_redis = None
_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()

async def _redis_client():
    global _redis
    if redis is None:
        return None
    if _redis is None:
        try:
            _redis = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
            await _redis.ping()
        except Exception:
            _redis = None
    return _redis

async def _http_client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30)
    return _client

def _hash_key(model: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(text.encode())
    return h.hexdigest()[:32]

async def _cache_get(model: str, text: str):
    r = await _redis_client()
    if not r:
        return None
    return await r.get(f"mica:emb:{_hash_key(model, text)}")

async def _cache_set(model: str, text: str, vec: List[float]):
    r = await _redis_client()
    if not r:
        return
    try:
        await r.set(f"mica:emb:{_hash_key(model, text)}", json.dumps(vec), ex=CACHE_TTL)
    except Exception:
        pass

async def _nvidia_embed(text: str) -> List[float]:
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY missing")
    client = await _http_client()
    
    # Use the correct base URL for the selected model
    endpoint_url = f"{NVIDIA_BASE_URL}/embeddings"
    
    resp = await client.post(
        endpoint_url,
        headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
        json={"input": text, "model": NVIDIA_MODEL},
    )
    resp.raise_for_status()
    data = resp.json()
    vec = data.get("data", [{}])[0].get("embedding") or []
    return vec

async def _openai_embed(text: str) -> List[float]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")
    client = await _http_client()
    resp = await client.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={"input": text, "model": OPENAI_MODEL},
    )
    resp.raise_for_status()
    data = resp.json()
    vec = data.get("data", [{}])[0].get("embedding") or []
    return vec

def _fallback_hash(text: str) -> List[float]:
    raw = hashlib.sha256(text.encode()).digest()
    return [int.from_bytes(raw[i:i+2], "big")/65535.0 for i in range(0, 64, 2)]

async def embed(text: str) -> List[float]:
    text = text.strip()
    if not text:
        return []
    model = NVIDIA_MODEL if PROVIDER == "nvidia" else OPENAI_MODEL
    cached = await _cache_get(model, text)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    try:
        if PROVIDER == "nvidia":
            vec = await _nvidia_embed(text)
        elif PROVIDER == "openai":
            vec = await _openai_embed(text)
        else:
            vec = _fallback_hash(text)
    except Exception as e:
        log.warning(f"[embedding_service] provider failed ({PROVIDER}): {e}; fallback hash")
        vec = _fallback_hash(text)
    # cache
    await _cache_set(model, text, vec)
    return vec

async def dimension() -> int:
    # naive dimension discovery: embed short token once
    vec = await embed("dim_probe")
    return len(vec)

__all__ = ["embed", "dimension"]
