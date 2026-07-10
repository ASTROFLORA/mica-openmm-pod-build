"""KB-UNBLOCK-001 + 004: BiolinkBERT Modal embedding client.

Thin wrapper that hits the deployed Modal BioLinkBERT class method and
returns L2-normalised 1024-dim vectors. Used by kb.ingest and
kb.semantic_search handlers. Falls back from embed_json to embed
when the wrapper method has not been re-deployed yet.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


KB_EMBEDDING_DIM = 1024
_METHOD_CACHE_LOCK = threading.Lock()
_METHOD_FALLBACK_CACHE: dict[tuple[str, str], str] = {}


class BiolinkBertUnavailable(RuntimeError):
    """Raised when BiolinkBERT Modal cannot be reached or returns invalid output."""


def _l2_normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) ** 2 for x in vec))
    if norm == 0.0:
        raise BiolinkBertUnavailable("cannot L2-normalise a zero vector")
    return [float(x) / norm for x in vec]


def _resolve_modal_credentials() -> tuple[str, str]:
    token_id = (os.getenv("MODAL_TOKEN_ID") or "").strip()
    token_secret = (os.getenv("MODAL_TOKEN_SECRET") or "").strip()
    if not token_id or not token_secret:
        raise BiolinkBertUnavailable(
            "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET not set in the active environment"
        )
    return token_id, token_secret


def _resolve_modal_target() -> tuple[str, str, list[str]]:
    app = (os.getenv("MODAL_BIOLINKBERT_APP_NAME") or "mica-biolinkbert-large").strip()
    cls_name = (os.getenv("MODAL_BIOLINKBERT_CLASS_NAME") or "BioLinkBERTA10G").strip()
    method_priority_env = os.getenv("MODAL_BIOLINKBERT_METHOD_PRIORITY")
    if method_priority_env:
        priority = [m.strip() for m in method_priority_env.split(",") if m.strip()]
    else:
        priority = ["embed_json", "embed"]
    return app, cls_name, priority


def _candidate_method_order(app: str, cls_name: str, configured_priority: Sequence[str]) -> list[str]:
    with _METHOD_CACHE_LOCK:
        cached = _METHOD_FALLBACK_CACHE.get((app, cls_name))
    if not cached or cached not in configured_priority:
        return list(configured_priority)
    return [cached, *[m for m in configured_priority if m != cached]]


def _remember_working_method(app: str, cls_name: str, method_name: str) -> None:
    with _METHOD_CACHE_LOCK:
        _METHOD_FALLBACK_CACHE[(app, cls_name)] = method_name


def _looks_like_missing_modal_method(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return "has no method" in message or "attributes can't be accessed" in message


def embed_texts_modal(texts: Sequence[str]) -> tuple[list[list[float]], dict]:
    """Embed a list of texts via BiolinkBERT Modal A10G.

    Returns (vectors, metadata). Each vector is 1024-dim L2-normalised.
    Metadata includes modal_app, modal_class, modal_function, latency_ms,
    n_texts, embedding_dim.

    Raises BiolinkBertUnavailable if the call fails. The handler must
    catch this and surface degraded_reason="biolinkbert_unavailable".
    """
    if not texts:
        return [], {
            "embedding_dim": KB_EMBEDDING_DIM,
            "n_texts": 0,
            "modal_app": "",
            "modal_class": "",
            "modal_function": "",
            "latency_ms": 0.0,
        }

    _resolve_modal_credentials()  # raises if missing

    try:
        import modal  # type: ignore
    except ModuleNotFoundError as exc:
        raise BiolinkBertUnavailable(f"modal SDK not installed: {exc}") from exc

    app, cls_name, configured_priority = _resolve_modal_target()
    text_list = [str(t) for t in texts]

    t0 = time.perf_counter()
    try:
        cls = modal.Cls.from_name(app, cls_name)
        instance = cls()
    except Exception as exc:
        raise BiolinkBertUnavailable(
            f"Modal lookup failed for {app}/{cls_name}: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc

    raw: list[list[float]] = []
    method_used = ""
    for candidate in _candidate_method_order(app, cls_name, configured_priority):
        try:
            method = getattr(instance, candidate)
        except Exception as exc:
            if candidate == "embed_json" and _looks_like_missing_modal_method(exc):
                _remember_working_method(app, cls_name, "embed")
                logger.info(
                    "BiolinkBERT Modal class %s/%s does not expose %s; caching embed fallback",
                    app,
                    cls_name,
                    candidate,
                )
                continue
            logger.warning(
                "Failed to resolve Modal method %s on %s/%s: %s",
                candidate,
                app,
                cls_name,
                str(exc)[:120],
            )
            continue
        if candidate == "embed_json":
            try:
                payload = {"embeddings": method.remote(text_list)}
            except Exception as exc:
                if _looks_like_missing_modal_method(exc):
                    _remember_working_method(app, cls_name, "embed")
                    logger.info(
                        "BiolinkBERT Modal remote %s missing on %s/%s; caching embed fallback",
                        candidate,
                        app,
                        cls_name,
                    )
                    continue
                logger.warning("embed_json failed (%s); falling back to embed", str(exc)[:80])
                continue
            result = payload.get("embeddings", [])
            if not isinstance(result, list) or not result:
                continue
            raw = result
            method_used = candidate
            _remember_working_method(app, cls_name, candidate)
            break
        else:
            try:
                raw = method.remote(text_list)
            except Exception as exc:
                raise BiolinkBertUnavailable(
                    f"Modal {candidate}.remote failed: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
            method_used = candidate
            _remember_working_method(app, cls_name, candidate)
            break
    else:
        raise BiolinkBertUnavailable("no usable BiolinkBERT Modal method found")

    if not isinstance(raw, list):
        raise BiolinkBertUnavailable(
            f"unexpected Modal response type: {type(raw).__name__} (expected list)"
        )
    if len(raw) != len(text_list):
        raise BiolinkBertUnavailable(
            f"Modal returned {len(raw)} embeddings for {len(text_list)} inputs"
        )

    normalised: list[list[float]] = []
    for vec in raw:
        if not isinstance(vec, list) or len(vec) != KB_EMBEDDING_DIM:
            raise BiolinkBertUnavailable(
                f"Modal returned a vector of dim {len(vec) if isinstance(vec, list) else 'N/A'}, "
                f"expected {KB_EMBEDDING_DIM}"
            )
        normalised.append(_l2_normalise(vec))

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return normalised, {
        "embedding_dim": KB_EMBEDDING_DIM,
        "n_texts": len(text_list),
        "modal_app": app,
        "modal_class": cls_name,
        "modal_function": method_used,
        "latency_ms": latency_ms,
        "model_id": "biolinkbert-large",
    }
