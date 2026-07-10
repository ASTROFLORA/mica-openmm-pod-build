"""Conversation persistence for agentic sessions.

Provides durable storage of conversation state (messages, tool results,
metadata) backed by GCS via the existing :class:`~mica.storage.gcs_user_storage.GCSUserStorage`
infrastructure.  An in-process LRU cache keeps hot sessions fast, and a
transparent local-filesystem fallback ensures the module works even when
GCS credentials are absent.

Public API::

    store = ConversationStore()
    store.save(user_id, session_id, messages, metadata)
    messages, metadata = store.load(user_id, session_id)
    sessions = store.list_sessions(user_id)

    resumer = SessionResumer(store, user_id="u1", session_id="s1")
    context = resumer.resume_context(last_n=10)
    resumer.append_and_save(new_messages, metadata)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _is_production_env() -> bool:
    env = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "development"
    return str(env).lower() in ("prod", "production")

# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

_CONVERSATION_FILENAME = "conversation.json"


def _blob_path(user_id: str, session_id: str) -> str:
    """Build the canonical GCS object path for a conversation."""
    return f"sessions/{user_id}/{session_id}/{_CONVERSATION_FILENAME}"


# ---------------------------------------------------------------------------
# Payload serialisation
# ---------------------------------------------------------------------------

def _serialise(
    messages: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]],
) -> bytes:
    payload: Dict[str, Any] = {
        "version": 1,
        "saved_at": time.time(),
        "messages": messages,
        "metadata": metadata or {},
    }
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _deserialise(raw: bytes) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    payload = json.loads(raw)
    return payload.get("messages", []), payload.get("metadata", {})


# ---------------------------------------------------------------------------
# Local-filesystem fallback
# ---------------------------------------------------------------------------

class _LocalFallbackStore:
    """Simple JSON-on-disk store used when GCS is unavailable."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path(tempfile.gettempdir()) / "mica_conversation_store"
        self._root.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "\u26a0\ufe0f LOCAL FALLBACK STORE ACTIVE at %s \u2014 "
            "NOT SAFE FOR MULTI-USER PRODUCTION",
            self._root,
        )

    def _path(self, user_id: str, session_id: str) -> Path:
        p = self._root / "sessions" / user_id / session_id
        if not p.resolve().is_relative_to(self._root.resolve()):
            raise ValueError("Invalid user_id/session_id \u2014 path escape detected")
        p.mkdir(parents=True, exist_ok=True)
        return p / _CONVERSATION_FILENAME

    def save(self, user_id: str, session_id: str, data: bytes) -> None:
        self._path(user_id, session_id).write_bytes(data)

    def load(self, user_id: str, session_id: str) -> bytes | None:
        p = self._path(user_id, session_id)
        return p.read_bytes() if p.exists() else None

    def list_sessions(self, user_id: str) -> List[str]:
        base = self._root / "sessions" / user_id
        if not base.is_dir():
            return []
        return [
            d.name
            for d in sorted(base.iterdir())
            if d.is_dir() and (d / _CONVERSATION_FILENAME).exists()
        ]


# ---------------------------------------------------------------------------
# LRU cache keyed on (user_id, session_id)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=50)
def _cached_load(
    store: ConversationStore,
    user_id: str,
    session_id: str,
    _cache_buster: float,  # noqa: ARG001 – forces refresh when value changes
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Cache wrapper – the *_cache_buster* parameter lets callers
    invalidate a specific entry by changing its value."""
    return store._load_from_backend(user_id, session_id)


# ---------------------------------------------------------------------------
# ConversationStore
# ---------------------------------------------------------------------------

class ConversationStore:
    """Persist and retrieve conversation state.

    Storage priority:
    1. GCS via :func:`~mica.storage.gcs_user_storage.get_storage_manager`
    2. Local-filesystem fallback (auto-selected when GCS is not configured)

    An in-process :func:`functools.lru_cache` (maxsize=50) ensures repeated
    reads of the same session avoid redundant I/O.
    """

    def __init__(self, *, local_root: Path | None = None) -> None:
        self._gcs = self._try_init_gcs()
        if self._gcs is None and _is_production_env():
            raise RuntimeError(
                "GCS is REQUIRED in production mode but initialisation failed. "
                "Set GOOGLE_APPLICATION_CREDENTIALS or disable production mode "
                "(MICA_ENV != 'production')."
            )
        self._local = _LocalFallbackStore(local_root) if self._gcs is None else None
        # Monotonically increasing counter per (user, session) to bust the
        # LRU cache after writes.
        self._versions: Dict[Tuple[str, str], float] = {}

    # -- GCS bootstrap ------------------------------------------------------

    @staticmethod
    def _try_init_gcs() -> Any | None:
        """Return the singleton *GCSUserStorage* or ``None``."""
        try:
            from mica.storage.gcs_user_storage import get_storage_manager  # type: ignore[import-untyped]
            return get_storage_manager()
        except Exception:
            logger.warning(
                "GCSUserStorage unavailable – falling back to local storage",
                exc_info=True,
            )
            return None

    # -- Low-level I/O (bypasses cache) -------------------------------------

    def _save_to_backend(
        self,
        user_id: str,
        session_id: str,
        data: bytes,
    ) -> None:
        object_path = _blob_path(user_id, session_id)
        if self._gcs is not None:
            with tempfile.NamedTemporaryFile(
                suffix=".json", delete=False,
            ) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            try:
                self._gcs.upload_file(
                    user_id=user_id,
                    object_path=object_path,
                    local_path=tmp_path,
                    content_type="application/json",
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            assert self._local is not None
            self._local.save(user_id, session_id, data)

    def _load_from_backend(
        self,
        user_id: str,
        session_id: str,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        object_path = _blob_path(user_id, session_id)
        if self._gcs is not None:
            with tempfile.NamedTemporaryFile(
                suffix=".json", delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
            try:
                self._gcs.download_file(
                    user_id=user_id,
                    object_path=object_path,
                    local_path=tmp_path,
                )
                return _deserialise(tmp_path.read_bytes())
            except Exception:
                logger.debug(
                    "Failed to load %s from GCS – returning empty",
                    object_path,
                    exc_info=True,
                )
                return [], {}
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            assert self._local is not None
            raw = self._local.load(user_id, session_id)
            if raw is None:
                return [], {}
            return _deserialise(raw)

    # -- Public API ---------------------------------------------------------

    def save(
        self,
        user_id: str,
        session_id: str,
        messages: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Serialise and persist conversation state.

        Parameters
        ----------
        user_id:
            Unique user identifier (scopes the GCS bucket).
        session_id:
            Unique session identifier within the user scope.
        messages:
            Ordered list of message dicts (role, content, tool_call_id, …).
        metadata:
            Arbitrary session-level metadata (model, token counts, …).
        """
        data = _serialise(messages, metadata)
        try:
            self._save_to_backend(user_id, session_id, data)
        except Exception:
            logger.error(
                "Failed to save session %s/%s", user_id, session_id,
                exc_info=True,
            )
            raise
        # Bump version so the next cached read fetches fresh data.
        self._versions[(user_id, session_id)] = time.monotonic()

    def load(
        self,
        user_id: str,
        session_id: str,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Load conversation state, returning ``(messages, metadata)``.

        Returns ``([], {})`` when the session does not exist or the backend
        is unreachable.
        """
        version = self._versions.get((user_id, session_id), 0.0)
        return _cached_load(self, user_id, session_id, version)

    def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Return metadata summaries for all sessions belonging to *user_id*.

        Each entry contains at least ``{"session_id": ...}``.  If the
        backend supports it, ``saved_at`` and ``message_count`` are included.
        """
        if self._gcs is not None:
            return self._list_sessions_gcs(user_id)

        assert self._local is not None
        results: List[Dict[str, Any]] = []
        for sid in self._local.list_sessions(user_id):
            msgs, meta = self.load(user_id, sid)
            results.append({
                "session_id": sid,
                "message_count": len(msgs),
                **meta,
            })
        return results

    def _list_sessions_gcs(self, user_id: str) -> List[Dict[str, Any]]:
        """List sessions for a user by querying GCS blobs with a prefix."""
        prefix = f"sessions/{user_id}/"
        try:
            bucket_info = self._gcs.ensure_bucket(user_id)
            bucket = self._gcs.client.bucket(bucket_info.bucket_name)
            blobs = bucket.list_blobs(prefix=prefix)

            results: List[Dict[str, Any]] = []
            for blob in blobs:
                if not blob.name.endswith(f"/{_CONVERSATION_FILENAME}"):
                    continue
                # Extract session_id from "sessions/{uid}/{sid}/conversation.json"
                parts = blob.name.split("/")
                if len(parts) >= 4:
                    sid = parts[2]
                    entry: Dict[str, Any] = {"session_id": sid}
                    if blob.updated:
                        entry["updated_at"] = blob.updated.isoformat()
                    if blob.size is not None:
                        entry["size_bytes"] = blob.size
                    results.append(entry)
            return results
        except Exception:
            logger.warning(
                "Failed to list sessions for %s from GCS", user_id,
                exc_info=True,
            )
            return []

    # -- Cache management ---------------------------------------------------

    @staticmethod
    def clear_cache() -> None:
        """Evict all entries from the in-process LRU cache."""
        _cached_load.cache_clear()

    @staticmethod
    def cache_info() -> Any:
        """Return :class:`functools._CacheInfo` for the LRU cache."""
        return _cached_load.cache_info()


# ---------------------------------------------------------------------------
# SessionResumer
# ---------------------------------------------------------------------------

class SessionResumer:
    """Convenience wrapper for resuming and extending a single session.

    Example::

        resumer = SessionResumer(store, user_id="u1", session_id="abc123")
        history = resumer.resume_context(last_n=10)
        # … run agent loop …
        resumer.append_and_save(new_messages, {"model": "gemini-pro"})
    """

    def __init__(
        self,
        store: ConversationStore,
        user_id: str,
        session_id: str,
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._session_id = session_id
        self._messages: List[Dict[str, Any]] = []
        self._metadata: Dict[str, Any] = {}
        self._loaded = False

    # -- Internal helpers ---------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._messages, self._metadata = self._store.load(
                self._user_id, self._session_id,
            )
            self._loaded = True

    # -- Public API ---------------------------------------------------------

    def resume_context(self, last_n: int = 10) -> List[Dict[str, Any]]:
        """Return the last *last_n* messages from the persisted session.

        If the session does not exist yet an empty list is returned.
        """
        self._ensure_loaded()
        if last_n <= 0:
            return []
        return self._messages[-last_n:]

    def append_and_save(
        self,
        messages: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append *messages* to the session history and persist.

        *metadata* is merged (shallow) into existing metadata; pass an
        explicit key with ``None`` value to delete it.
        """
        self._ensure_loaded()
        self._messages.extend(messages)
        if metadata:
            self._metadata.update(metadata)
        # Strip None-valued keys produced by explicit deletions.
        self._metadata = {k: v for k, v in self._metadata.items() if v is not None}
        self._store.save(
            self._user_id,
            self._session_id,
            self._messages,
            self._metadata,
        )

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """Full message history (loads lazily on first access)."""
        self._ensure_loaded()
        return list(self._messages)

    @property
    def metadata(self) -> Dict[str, Any]:
        """Session metadata (loads lazily on first access)."""
        self._ensure_loaded()
        return dict(self._metadata)
