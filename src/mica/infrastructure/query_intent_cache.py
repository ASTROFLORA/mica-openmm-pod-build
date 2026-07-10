from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional, Sequence

from .redisvl_semantic_cache import SemanticCacheScope, get_semantic_cache
from ..routing.query_intent_router import (
    EpistemicArchetype,
    EpistemicQueryIntentRouter,
    QueryIntent,
)

logger = logging.getLogger(__name__)

_CACHE_EMPTY = "__none__"
_CACHE_DISABLED_VALUES = {"0", "false", "no", "off", ""}
_QUERY_INTENT_CACHE_ENABLED = str(
    os.getenv("MICA_QUERY_INTENT_SEMANTIC_CACHE_ENABLED", "true")
).strip().lower() not in _CACHE_DISABLED_VALUES
_QUERY_INTENT_CACHE_DOMAIN = os.getenv(
    "MICA_QUERY_INTENT_SEMANTIC_CACHE_DOMAIN",
    "query_intent_classification",
)
_QUERY_INTENT_CACHE_VERSION = os.getenv("MICA_QUERY_INTENT_SEMANTIC_CACHE_VERSION", "v1")
_SCOPE_USER_KEYS = ("user_id", "workspace_owner", "workspace_user_id", "owner_id")
_SCOPE_SESSION_KEYS = ("session_id", "workspace_id", "run_id")


def _scope_value(metadata: Dict[str, Any], keys: Sequence[str], *, default: Optional[str]) -> Optional[str]:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _router_signature(router: EpistemicQueryIntentRouter) -> str:
    payload = {
        archetype.value: list(patterns)
        for archetype, patterns in sorted(router.patterns.items(), key=lambda item: item[0].value)
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _serialize_intent(intent: QueryIntent) -> Dict[str, Any]:
    return {
        "archetype": intent.archetype.value,
        "confidence": float(intent.confidence),
        "keywords": list(intent.keywords),
        "suggested_workflow": str(intent.suggested_workflow),
        "metadata": dict(intent.metadata or {}),
    }


def _deserialize_intent(payload: Any) -> QueryIntent:
    if not isinstance(payload, dict):
        raise TypeError("Cached query intent payload must be a dict")
    return QueryIntent(
        archetype=EpistemicArchetype(str(payload.get("archetype") or EpistemicArchetype.EXPLORATORY.value)),
        confidence=float(payload.get("confidence") or 0.0),
        keywords=[str(item) for item in list(payload.get("keywords") or [])],
        suggested_workflow=str(payload.get("suggested_workflow") or "general_rag"),
        metadata=dict(payload.get("metadata") or {}),
    )


class CachedEpistemicQueryIntentRouter:
    """Async RedisVL-backed wrapper for the pure query intent router."""

    def __init__(
        self,
        router: EpistemicQueryIntentRouter | None = None,
        *,
        cache_domain: str = _QUERY_INTENT_CACHE_DOMAIN,
        model_id: str | None = None,
    ) -> None:
        self.router = router or EpistemicQueryIntentRouter()
        signature = _router_signature(self.router)
        self._cache_domain = cache_domain
        self._model_id = model_id or f"epistemic_query_intent_router:{_QUERY_INTENT_CACHE_VERSION}:{signature}"

    async def classify(self, query: str, *, metadata: Optional[Dict[str, Any]] = None) -> QueryIntent:
        normalized_query = str(query or "").strip()
        scope = self._semantic_scope(metadata)
        cache = self._cache_backend()
        if cache is not None and normalized_query:
            hit = await cache.lookup(prompt=normalized_query, scope=scope)
            if hit is not None:
                try:
                    intent = _deserialize_intent(hit.response)
                    logger.info(
                        "Query intent cache hit (%s) for '%s'",
                        "semantic" if hit.is_semantic else "exact",
                        normalized_query,
                    )
                    return intent
                except Exception as exc:
                    logger.warning("Query intent cache payload invalid; falling back to live classify: %s", exc)

        intent = self.router.classify(normalized_query)
        if cache is not None and normalized_query:
            await cache.store(prompt=normalized_query, response=_serialize_intent(intent), scope=scope)
        return intent

    async def explain_classification(self, query: str, *, metadata: Optional[Dict[str, Any]] = None) -> str:
        intent = await self.classify(query, metadata=metadata)
        explanation = f"""
Classification: {intent.archetype.value.upper()} ({intent.confidence:.2f} confidence)
Matched keywords: {intent.keywords}
Suggested workflow: {intent.suggested_workflow}

Reasoning:
- Mechanistic keywords: {', '.join(intent.keywords) if intent.keywords else 'None'}
- This query seeks to understand {self.router._archetype_explanation(intent.archetype)}

Recommended next steps:
{self.router._workflow_explanation(intent.suggested_workflow)}
        """.strip()
        return explanation

    async def route_query_to_workflow(self, query: str, *, metadata: Optional[Dict[str, Any]] = None) -> str:
        return (await self.classify(query, metadata=metadata)).suggested_workflow

    async def should_use_md_data(self, query: str, *, metadata: Optional[Dict[str, Any]] = None) -> bool:
        intent = await self.classify(query, metadata=metadata)
        return intent.archetype in {
            EpistemicArchetype.MECHANISTIC_EXPLORATION,
            EpistemicArchetype.COMPARATIVE_ANALYSIS,
        }

    async def should_use_experimental_data(self, query: str, *, metadata: Optional[Dict[str, Any]] = None) -> bool:
        intent = await self.classify(query, metadata=metadata)
        return intent.archetype in {EpistemicArchetype.VALIDATION_FOCUSED}

    def _cache_backend(self):
        if not _QUERY_INTENT_CACHE_ENABLED:
            return None
        return get_semantic_cache()

    def _semantic_scope(self, metadata: Optional[Dict[str, Any]]) -> SemanticCacheScope:
        metadata = metadata or {}
        return SemanticCacheScope(
            user_id=_scope_value(metadata, _SCOPE_USER_KEYS, default="anonymous") or _CACHE_EMPTY,
            model=self._model_id,
            cache_domain=self._cache_domain,
            session_id=_scope_value(metadata, _SCOPE_SESSION_KEYS, default=None),
        )


async def route_query_to_workflow_cached(
    query: str,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    return await CachedEpistemicQueryIntentRouter().route_query_to_workflow(query, metadata=metadata)


async def should_use_md_data_cached(
    query: str,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    return await CachedEpistemicQueryIntentRouter().should_use_md_data(query, metadata=metadata)


async def should_use_experimental_data_cached(
    query: str,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    return await CachedEpistemicQueryIntentRouter().should_use_experimental_data(query, metadata=metadata)