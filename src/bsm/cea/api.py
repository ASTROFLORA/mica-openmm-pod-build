"""FastAPI application for the Canonical Entity Atlas (CEA).

This module wires the ``CEAService`` with REST endpoints described in
Phase 1.002 of the unified roadmap. It provides:

* ``GET /resolve/{identifier}`` – resolve an entity from any known identifier,
  using Redis (when available) as a read-through cache.
* ``POST /entities`` – create new entities (admin scoped).
* Auto-generated OpenAPI documentation with descriptive tags.

The application can run in two modes:

* **Production** – when Neo4j and Redis are available the module instantiates
  the real integrations based on the BSM configuration.
* **Research/Test** – individual services (CEA service, Redis client, admin
  keys) can be injected via ``create_cea_app`` for deterministic tests.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable, Optional, Set

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from mica.infrastructure.redis_client import get_redis_if_configured, resolve_redis_url

try:  # pragma: no cover - optional dependency
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover - handled gracefully at runtime
    redis_async = None  # type: ignore

from ..config import get_bsm_config
from ..neo4j_integration import BSMNeo4jIntegration
from .cea_service import CEAService
from .exceptions import CEADuplicateError, CEAError, CEANotFoundError
from ..schemas.cea import CEAEntity

logger = logging.getLogger(__name__)


class CEACache:
    """Simple read-through cache with optional Redis backend."""

    def __init__(self, redis_client: Optional[redis_async.Redis] = None, *, ttl: int = 300) -> None:  # type: ignore[name-defined]
        self.redis = redis_client
        self.ttl = ttl
        self._local_store: dict[str, tuple[float, str]] = {}

    @staticmethod
    def _key(identifier: str) -> str:
        return f"cea:resolve:{identifier.lower()}"

    async def get(self, identifier: str) -> Optional[CEAEntity]:
        cache_key = self._key(identifier)
        payload: Optional[str] = None

        if self.redis is not None:
            raw = await self.redis.get(cache_key)
            if raw is not None:
                payload = raw if isinstance(raw, str) else raw.decode("utf-8")
        else:
            record = self._local_store.get(cache_key)
            if record and record[0] >= time.monotonic():
                payload = record[1]
            elif record:
                self._local_store.pop(cache_key, None)

        if not payload:
            return None

        data = json.loads(payload)
        return CEAEntity.model_validate(data)

    async def set(self, identifier: str, entity: CEAEntity) -> None:
        cache_key = self._key(identifier)
        payload = json.dumps(entity.model_dump(mode="json"))

        if self.redis is not None:
            await self.redis.set(cache_key, payload, ex=self.ttl)
        else:
            expiry = time.monotonic() + self.ttl
            self._local_store[cache_key] = (expiry, payload)

    async def invalidate(self, identifier: str) -> None:
        cache_key = self._key(identifier)
        if self.redis is not None:
            await self.redis.delete(cache_key)
        self._local_store.pop(cache_key, None)


def _parse_admin_keys(values: Optional[Iterable[str]]) -> Set[str]:
    return {value.strip() for value in values or [] if value and value.strip()}


def _default_admin_keys_from_env() -> Set[str]:
    env_value = os.getenv("CEA_ADMIN_API_KEYS", "")
    keys = [item for item in env_value.split(",") if item]
    return _parse_admin_keys(keys)


async def _require_service(request: Request) -> CEAService:
    service: Optional[CEAService] = getattr(request.app.state, "cea_service", None)
    if service is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="CEA service not configured")
    return service


def _admin_guard_factory(admin_keys: Set[str]):
    async def _guard(request: Request) -> None:
        if not admin_keys:
            return
        provided = request.headers.get("X-BSM-API-Key")
        if provided not in admin_keys:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid API key")

    return _guard


def create_cea_app(
    *,
    service: Optional[CEAService] = None,
    redis_client: Optional[redis_async.Redis] = None,  # type: ignore[name-defined]
    admin_api_keys: Optional[Iterable[str]] = None,
    cache_ttl_seconds: int = 300,
) -> FastAPI:
    """Instantiate a FastAPI app for the CEA domain."""

    admin_keys = _parse_admin_keys(admin_api_keys)
    cache = CEACache(redis_client, ttl=cache_ttl_seconds)

    app = FastAPI(
        title="BSM Canonical Entity Atlas API",
        version="0.1.0",
        description="Identity resolution and CRUD interface for the Canonical Entity Atlas (CEA).",
        openapi_tags=[
            {
                "name": "identity",
                "description": "Resolve canonical biological entities from any supported identifier.",
            },
            {
                "name": "administration",
                "description": "Administrative endpoints for managing canonical entities.",
            },
        ],
    )

    app.state.cea_service = service
    app.state.cea_cache = cache
    app.state.admin_api_keys = admin_keys

    admin_guard = _admin_guard_factory(admin_keys)

    @app.on_event("startup")
    async def configure_cache() -> None:  # pragma: no cover - trivial wiring
        if redis_client is not None:
            try:
                await redis_client.ping()
                logger.info("✅ Redis cache connected for CEA API")
            except Exception as exc:  # pragma: no cover - depends on environment
                logger.warning("⚠️ Unable to connect to Redis cache: %s", exc)

    @app.exception_handler(CEANotFoundError)
    async def not_found_handler(_: Request, exc: CEANotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(CEADuplicateError)
    async def duplicate_handler(_: Request, exc: CEADuplicateError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(CEAError)
    async def generic_handler(_: Request, exc: CEAError) -> JSONResponse:
        logger.error("CEA service error: %s", exc)
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})

    @app.get("/health", tags=["identity"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/resolve/{identifier}", response_model=CEAEntity, tags=["identity"])
    async def resolve_entity(
        identifier: str,
        request: Request,
        service: CEAService = Depends(_require_service),
    ) -> CEAEntity:
        cache_layer: CEACache = request.app.state.cea_cache  # type: ignore[assignment]
        cached = await cache_layer.get(identifier)
        if cached:
            return cached

        entity = await service.resolve(identifier)
        await cache_layer.set(identifier, entity)
        return entity

    @app.post(
        "/entities",
        response_model=CEAEntity,
        status_code=status.HTTP_201_CREATED,
        tags=["administration"],
        dependencies=[Depends(admin_guard)],
    )
    async def create_entity(
        entity: CEAEntity,
        request: Request,
        service: CEAService = Depends(_require_service),
    ) -> CEAEntity:
        created = await service.create_entity(entity)
        cache_layer: CEACache = request.app.state.cea_cache  # type: ignore[assignment]
        await cache_layer.invalidate(created.budo_id)
        for value in created.keyword_tokens():
            await cache_layer.invalidate(value)
        return created

    return app


def _initialise_service() -> Optional[CEAService]:
    try:
        integration = BSMNeo4jIntegration()
    except ImportError:
        logger.warning("Neo4j driver unavailable; CEA API running without service binding")
        return None
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.error("Failed to initialise BSM Neo4j integration: %s", exc)
        return None

    return CEAService(integration)


def _initialise_redis_client() -> Optional[redis_async.Redis]:  # type: ignore[name-defined]
    if redis_async is None:
        return None
    redis_url = (os.getenv("CEA_REDIS_URL") or "").strip() or resolve_redis_url()
    if not redis_url:
        return None
    try:
        return redis_async.from_url(redis_url, decode_responses=True)
    except Exception as exc:  # pragma: no cover - depends on runtime
        logger.warning("Unable to configure Redis client: %s", exc)
        return None


def get_default_app() -> FastAPI:
    config = get_bsm_config()
    admin_keys = config.api.allowed_api_keys or _default_admin_keys_from_env()
    service = _initialise_service()
    redis_client = _initialise_redis_client()
    ttl = int(os.getenv("CEA_CACHE_TTL", "300"))
    return create_cea_app(
        service=service,
        redis_client=redis_client,
        admin_api_keys=admin_keys,
        cache_ttl_seconds=ttl,
    )


app = get_default_app()

__all__ = [
    "app",
    "create_cea_app",
    "get_default_app",
    "CEACache",
]
