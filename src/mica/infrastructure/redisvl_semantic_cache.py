from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Optional

from mica.infrastructure.redis_client import format_redis_target, get_redis_if_configured, resolve_redis_url

try:
    from redisvl.extensions.cache.llm import SemanticCache
    from redisvl.query.filter import Tag
    from redisvl.utils.vectorize import CustomVectorizer
except ImportError:
    SemanticCache = None  # type: ignore[assignment]
    Tag = None  # type: ignore[assignment]
    CustomVectorizer = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

_SCOPE_EMPTY = "__none__"
_DEFAULT_DOMAIN = "embedding_service"
_DEFAULT_VECTOR_DIMS = int(os.getenv("MICA_REDISVL_VECTOR_DIMS", "128"))
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _safe_tag(value: Optional[str]) -> str:
    cleaned = str(value or "").strip()
    return cleaned or _SCOPE_EMPTY


def _safe_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return slug or "default"


def _hash_prompt(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _serialize_response(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _deserialize_response(value: str) -> Any:
    return json.loads(value)


def _lexical_embedding(text: str, dims: int = _DEFAULT_VECTOR_DIMS) -> list[float]:
    vector = [0.0] * dims
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return vector

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return [value / norm for value in vector]


@dataclass(frozen=True)
class SemanticCacheScope:
    user_id: str = _SCOPE_EMPTY
    model: str = _SCOPE_EMPTY
    cache_domain: str = _DEFAULT_DOMAIN
    session_id: Optional[str] = None

    def tag_values(self) -> dict[str, str]:
        return {
            "user_id": _safe_tag(self.user_id),
            "model": _safe_tag(self.model),
            "cache_domain": _safe_tag(self.cache_domain),
            "session_id": _safe_tag(self.session_id),
        }

    def registry_key(self, namespace: str) -> str:
        parts = self.tag_values()
        return (
            f"{namespace}:registry:{parts['cache_domain']}:{parts['model']}:"
            f"{parts['user_id']}:{parts['session_id']}"
        )


@dataclass(frozen=True)
class SemanticCacheConfig:
    enabled: bool
    namespace: str
    distance_threshold: float
    ttl_seconds: int
    vector_dims: int
    exact_registry_ttl_bias: int
    redis_url: str


@dataclass(frozen=True)
class SemanticCacheHit:
    response: Any
    redis_key: str
    prompt: str
    vector_distance: Optional[float]
    metadata: dict[str, Any]
    is_semantic: bool


class RedisVLSemanticCache:
    def __init__(
        self,
        config: SemanticCacheConfig,
        *,
        semantic_cache_cls: Optional[type[Any]] = None,
        tag_cls: Optional[type[Any]] = None,
        vectorizer_factory: Optional[Callable[[int], Any]] = None,
        registry_client_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._config = config
        self._semantic_cache_cls = semantic_cache_cls if semantic_cache_cls is not None else SemanticCache
        self._tag_cls = tag_cls if tag_cls is not None else Tag
        self._vectorizer_factory = vectorizer_factory if vectorizer_factory is not None else _default_vectorizer_factory
        self._registry_client_getter = registry_client_getter
        self._cache_by_domain: dict[str, Any] = {}
        self._last_error: Optional[str] = None
        self._metrics = {
            "enabled": 1 if config.enabled else 0,
            "stores": 0,
            "hits": 0,
            "semantic_hits": 0,
            "misses": 0,
            "expired": 0,
            "invalidated": 0,
            "errors": 0,
        }

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def lookup(
        self,
        *,
        prompt: str,
        scope: SemanticCacheScope,
        prompt_vector: Optional[list[float]] = None,
    ) -> Optional[SemanticCacheHit]:
        prompt = (prompt or "").strip()
        if not prompt or not self._is_available():
            return None

        backend = self._backend_for_scope(scope)
        if backend is None:
            return None

        filter_expression = self._scope_filter(scope)
        try:
            hits = await backend.acheck(
                prompt=prompt,
                vector=prompt_vector or _lexical_embedding(prompt, self._config.vector_dims),
                num_results=1,
                filter_expression=filter_expression,
            )
        except Exception as exc:
            self._last_error = str(exc)
            self._metrics["errors"] += 1
            logger.warning("RedisVL semantic cache lookup degraded: %s", exc)
            return None

        if not hits:
            exact_mapping = await self._exact_mapping_key(scope, prompt)
            if exact_mapping is not None:
                self._metrics["expired"] += 1
            self._metrics["misses"] += 1
            return None

        hit = hits[0]
        response_text = hit.get("response")
        if response_text is None:
            self._metrics["misses"] += 1
            return None

        is_semantic = hit.get("prompt") != prompt
        self._metrics["semantic_hits" if is_semantic else "hits"] += 1
        metadata = hit.get("metadata") or {}
        return SemanticCacheHit(
            response=_deserialize_response(response_text),
            redis_key=str(hit.get("key") or hit.get("redis_key") or hit.get("entry_id") or ""),
            prompt=str(hit.get("prompt") or ""),
            vector_distance=_coerce_float(hit.get("vector_distance")),
            metadata=metadata if isinstance(metadata, dict) else {},
            is_semantic=is_semantic,
        )

    async def store(
        self,
        *,
        prompt: str,
        response: Any,
        scope: SemanticCacheScope,
        prompt_vector: Optional[list[float]] = None,
        metadata: Optional[dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[str]:
        prompt = (prompt or "").strip()
        if not prompt or not self._is_available():
            return None

        backend = self._backend_for_scope(scope)
        if backend is None:
            return None

        response_text = _serialize_response(response)
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("scope", scope.tag_values())
        try:
            redis_key = await backend.astore(
                prompt=prompt,
                response=response_text,
                vector=prompt_vector or _lexical_embedding(prompt, self._config.vector_dims),
                metadata=merged_metadata,
                filters=scope.tag_values(),
                ttl=ttl_seconds or self._config.ttl_seconds,
            )
        except Exception as exc:
            self._last_error = str(exc)
            self._metrics["errors"] += 1
            logger.warning("RedisVL semantic cache store degraded: %s", exc)
            return None

        await self._register_key(scope, prompt, redis_key, ttl_seconds or self._config.ttl_seconds)
        self._metrics["stores"] += 1
        return redis_key

    async def invalidate_scope(self, scope: SemanticCacheScope) -> int:
        if not self._config.redis_url:
            return 0
        redis_client = await self._get_registry_client()
        if redis_client is None:
            return 0

        registry_key = scope.registry_key(self._config.namespace)
        keys = await redis_client.smembers(registry_key)
        if not keys:
            await redis_client.delete(registry_key)
            return 0

        deleted = 0
        for key in keys:
            deleted += await redis_client.delete(key)

        await redis_client.delete(registry_key)
        self._metrics["invalidated"] += deleted
        return deleted

    def metrics_snapshot(self) -> dict[str, int]:
        return dict(self._metrics)

    def status_snapshot(self) -> dict[str, Any]:
        status = "disabled"
        if self._config.enabled:
            if not self._config.redis_url:
                status = "unconfigured"
            elif self._semantic_cache_cls is None:
                status = "missing_package"
            elif self._last_error:
                status = "degraded"
            else:
                status = "ready"
        return {
            "status": status,
            "enabled": self._config.enabled,
            "configured": bool(self._config.redis_url),
            "package": "ok" if SemanticCache is not None else "missing",
            "target": format_redis_target(self._config.redis_url),
            "namespace": self._config.namespace,
            "distance_threshold": self._config.distance_threshold,
            "ttl_seconds": self._config.ttl_seconds,
            "vector_dims": self._config.vector_dims,
            "last_error": self._last_error,
            "metrics": self.metrics_snapshot(),
        }

    def _is_available(self) -> bool:
        return self._config.enabled and bool(self._config.redis_url) and self._semantic_cache_cls is not None

    def _backend_for_scope(self, scope: SemanticCacheScope) -> Any:
        if not self._is_available():
            return None

        domain = _safe_name(scope.cache_domain)
        if domain in self._cache_by_domain:
            return self._cache_by_domain[domain]

        if self._semantic_cache_cls is None:
            return None

        try:
            backend = self._semantic_cache_cls(
                name=f"{self._config.namespace}_{domain}",
                ttl=self._config.ttl_seconds,
                redis_url=self._config.redis_url,
                distance_threshold=self._config.distance_threshold,
                vectorizer=self._vectorizer_factory(self._config.vector_dims),
                filterable_fields=[
                    {"name": "user_id", "type": "tag"},
                    {"name": "model", "type": "tag"},
                    {"name": "cache_domain", "type": "tag"},
                    {"name": "session_id", "type": "tag"},
                ],
            )
        except Exception as exc:
            self._last_error = str(exc)
            self._metrics["errors"] += 1
            logger.warning("RedisVL semantic cache initialization degraded for %s: %s", domain, exc)
            return None

        self._last_error = None
        self._cache_by_domain[domain] = backend
        return backend

    def _scope_filter(self, scope: SemanticCacheScope) -> Any:
        if self._tag_cls is None:
            return None
        tags = scope.tag_values()
        return (
            (self._tag_cls("user_id") == tags["user_id"])
            & (self._tag_cls("model") == tags["model"])
            & (self._tag_cls("cache_domain") == tags["cache_domain"])
            & (self._tag_cls("session_id") == tags["session_id"])
        )

    async def _register_key(self, scope: SemanticCacheScope, prompt: str, redis_key: str, ttl_seconds: int) -> None:
        redis_client = await self._get_registry_client()
        if redis_client is None:
            return

        registry_key = scope.registry_key(self._config.namespace)
        exact_key = self._exact_key(scope, prompt)
        ttl_bias = max(ttl_seconds + self._config.exact_registry_ttl_bias, ttl_seconds)
        await redis_client.sadd(registry_key, redis_key)
        await redis_client.expire(registry_key, ttl_bias)
        await redis_client.set(exact_key, redis_key, ex=ttl_bias)

    async def _exact_mapping_key(self, scope: SemanticCacheScope, prompt: str) -> Optional[str]:
        redis_client = await self._get_registry_client()
        if redis_client is None:
            return None
        return await redis_client.get(self._exact_key(scope, prompt))

    async def _get_registry_client(self):
        if self._registry_client_getter is not None:
            return await self._registry_client_getter()
        return await get_redis_if_configured(self._config.redis_url, decode_responses=True, verify_connection=True)

    def _exact_key(self, scope: SemanticCacheScope, prompt: str) -> str:
        return f"{scope.registry_key(self._config.namespace)}:exact:{_hash_prompt(prompt)}"


def _default_vectorizer_factory(dims: int):
    if CustomVectorizer is None:
        raise RuntimeError("redisvl package not installed")
    return CustomVectorizer(
        embed=lambda content, **_: _lexical_embedding(str(content or ""), dims),
        embed_many=lambda contents, **_: [_lexical_embedding(str(content or ""), dims) for content in (contents or [])],
        aembed=lambda content, **_: _lexical_embedding(str(content or ""), dims),
        aembed_many=lambda contents, **_: [_lexical_embedding(str(content or ""), dims) for content in (contents or [])],
    )


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def semantic_cache_config() -> SemanticCacheConfig:
    return SemanticCacheConfig(
        enabled=_env_flag("MICA_REDISVL_SEMANTIC_CACHE_ENABLED", True),
        namespace=_safe_name(os.getenv("MICA_REDISVL_NAMESPACE", "mica_semantic_cache")),
        distance_threshold=float(os.getenv("MICA_REDISVL_DISTANCE_THRESHOLD", "0.18")),
        ttl_seconds=int(os.getenv("MICA_REDISVL_TTL_SECONDS", os.getenv("MICA_EMBED_CACHE_TTL", "86400"))),
        vector_dims=int(os.getenv("MICA_REDISVL_VECTOR_DIMS", str(_DEFAULT_VECTOR_DIMS))),
        exact_registry_ttl_bias=int(os.getenv("MICA_REDISVL_EXACT_REGISTRY_TTL_BIAS", "60")),
        redis_url=resolve_redis_url(),
    )


@lru_cache(maxsize=1)
def get_semantic_cache() -> RedisVLSemanticCache:
    return RedisVLSemanticCache(semantic_cache_config())


def semantic_cache_metrics_snapshot() -> dict[str, int]:
    return get_semantic_cache().metrics_snapshot()


def semantic_cache_status() -> dict[str, Any]:
    return get_semantic_cache().status_snapshot()


__all__ = [
    "RedisVLSemanticCache",
    "SemanticCacheConfig",
    "SemanticCacheHit",
    "SemanticCacheScope",
    "get_semantic_cache",
    "semantic_cache_config",
    "semantic_cache_metrics_snapshot",
    "semantic_cache_status",
]