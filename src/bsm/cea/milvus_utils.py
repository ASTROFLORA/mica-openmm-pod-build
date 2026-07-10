"""Shared helpers for connecting to and interacting with Milvus/Zilliz collections.

These utilities encapsulate the environment-driven connection logic used across
multiple ingestion scripts so that higher level pipelines (e.g. STRING metadata
enrichment) can focus on business logic without duplicating Milvus boilerplate.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, MutableMapping, Optional, Sequence, Tuple

from pymilvus import Collection, connections, utility

from ..config import get_bsm_config

logger = logging.getLogger(__name__)


def connect_default(*, alias: str = "default"):
    """Connect to Milvus/Zilliz using the central BSM configuration."""

    config = get_bsm_config()
    milvus_cfg = config.milvus

    try:
        if milvus_cfg.uri:
            if milvus_cfg.token:
                connections.connect(alias=alias, uri=milvus_cfg.uri, token=milvus_cfg.token)
            else:
                connections.connect(alias=alias, uri=milvus_cfg.uri)
        else:
            connections.connect(alias=alias, host=milvus_cfg.host, port=str(milvus_cfg.port))
    except Exception as exc:  # pragma: no cover - surfaced to caller
        logger.error("No se pudo establecer conexión con Milvus/Zilliz: %s", exc)
        raise

    return config


def disconnect_default(*, alias: str = "default") -> None:
    """Gracefully close an existing Milvus/Zilliz connection."""

    try:
        connections.disconnect(alias)
    except Exception:  # pragma: no cover - best effort cleanup
        logger.debug("Fallo al cerrar la conexión Milvus alias=%s", alias, exc_info=True)


def has_collection(name: str, *, alias: str = "default") -> bool:
    """Return True when the collection exists."""

    try:
        return utility.has_collection(name, using=alias)
    except Exception:  # pragma: no cover - upstream should surface the failure
        logger.debug("utility.has_collection falló para %s", name, exc_info=True)
        return False


def get_collection(name: str, *, alias: str = "default", load: bool = True) -> Collection:
    """Fetch a collection by name, optionally loading it into memory."""

    if not has_collection(name, alias=alias):
        raise ValueError(f"Colección no encontrada en Milvus/Zilliz: {name}")

    collection = Collection(name, using=alias)

    if load:
        try:
            collection.load()
        except Exception:  # pragma: no cover - the caller can decide if this is fatal
            logger.debug("No se pudo cargar la colección %s en memoria", name, exc_info=True)

    return collection


def collection_field_names(collection: Collection) -> List[str]:
    """Return the ordered list of field names declared in the collection schema."""

    return [field.name for field in collection.schema.fields]


def supports_dynamic_field(collection: Collection) -> bool:
    """Check whether the collection schema has dynamic fields enabled."""

    return bool(getattr(collection.schema, "enable_dynamic_field", False))


def get_field_attr(collection: Collection, field_name: str, attr: str, default=None):
    """Helper to access a field attribute (e.g. max_length) when available."""

    for field in collection.schema.fields:
        if field.name == field_name:
            return getattr(field, attr, default)
    return default


def prune_to_schema(collection: Collection, entity: MutableMapping[str, object]) -> MutableMapping[str, object]:
    """Drop keys that are not part of the declared schema."""

    allowed = set(collection_field_names(collection))
    keys_to_remove = [key for key in entity.keys() if key not in allowed]
    for key in keys_to_remove:
        entity.pop(key, None)
    return entity


def safe_upsert(collection: Collection, rows: Sequence[MutableMapping[str, object]]) -> bool:
    """Insert or upsert rows, falling back to delete+insert when necessary."""

    if not rows:
        return True

    try:
        collection.upsert(rows)
        return True
    except AttributeError:
        # Older pymilvus without upsert support: emulate by delete + insert
        ids = [row.get("protein_id") for row in rows if row.get("protein_id")]
        if ids:
            expr = "protein_id in [" + ", ".join(f'"{pid}"' for pid in ids) + "]"
            try:
                collection.delete(expr)
            except Exception:  # pragma: no cover - non-fatal cleanup attempt
                logger.debug("Fallo al eliminar registros previos antes de insertar: %s", expr, exc_info=True)
        try:
            collection.insert(rows)
            return True
        except Exception:  # pragma: no cover
            logger.exception("Insert fallback falló en la colección %s", collection.name)
            return False
    except Exception:  # pragma: no cover - propagate failure upstream
        logger.exception("Upsert falló en la colección %s", collection.name)
        return False


__all__ = [
    "connect_default",
    "disconnect_default",
    "has_collection",
    "get_collection",
    "collection_field_names",
    "supports_dynamic_field",
    "get_field_attr",
    "prune_to_schema",
    "safe_upsert",
]
