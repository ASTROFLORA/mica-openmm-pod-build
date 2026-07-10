"""FeedCore — unified canonical feed engine for MICA.

This is the single source of truth for all feed I/O. Both the MCP server
(`tools/mica_agent_feed_mcp_server.py`) and the legacy runtime
(`src/mica/agentic/tools/agent_feed.py`) delegate to this module.

Canonical post schema (superset of both legacy and new):
    {
        "id": "<uuid4>",
        "timestamp": "<ISO-8601 UTC>",          # primary time field
        "ts": "<ISO-8601 UTC>",                  # legacy alias (auto-derived)
        "agent_id": "<agent>",
        "post_type": "cue|decision|tombstone|hypothesis|comment|artifact|insight|session_open|session_progress|session_close",
        "topic": "<one of 14 topic categories>",
        "intent": "...",                         # primary — what you were trying to do
        "content": "...",                        # primary — the actual payload
        "title": "...",                          # legacy alias (auto-derived from intent)
        "body": "...",                           # legacy alias (auto-derived from content)
        "parent_id": "<uuid or null>",
        "artifacts": ["path1", ...],             # legacy
        "evidence": ["path1", ...],              # new
        "session_id": "<uuid or null>",
        "session_phase": "open|progress|close|null",
        "graph_updated": false,
        "memory_updated": false,
        "files_touched": ["path1", ...],
        "context_questions": ["q1", ...],
        "context_questions_answered": ["q1", ...],
        "current_situation": "...",
        "next_actions": ["a1", ...],
        "biological_context": "...",
        "target_agents": ["agent1", ...],
        "metadata": { ... },
        "progress_count": 0
    }

Feed root resolution follows the same priority as the legacy agent_feed:
    1. MICA_AGENT_FEED_ROOT env override
    2. .mica/.canonical sentinel (outermost wins)
    3. Any .mica directory walking up from CWD
    4. Fallback to <cwd>/.mica/agent_feed

Cross-platform advisory lock (msvcrt on Windows, fcntl elsewhere).
Append-only JSONL. Thread-safe writes.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

POST_TYPES = frozenset({
    "cue",
    "decision",
    "tombstone",
    "hypothesis",
    "comment",
    "artifact",
    "insight",
    "session_open",
    "session_progress",
    "session_close",
    # Slice-7 §8/§9 additions — driver self-observability
    "tool_invocation",
    "driver_thought",
    "driver_decision",
})

TOPIC_CATEGORIES = frozenset({
    "architecture",
    "biology",
    "errors",
    "literature",
    "orchestration",
    "infrastructure",
    "frontend",
    "memory",
    "graphrag",
    "bsm",
    "scientific_protocol",
    "deployment",
    "governance",
    "general",
})

SESSION_LIFECYCLE_PHASES = frozenset({"open", "progress", "close"})

# Best-effort projection hooks. FeedCore remains the only write authority.
_POST_APPEND_LISTENERS: List[Callable[[Dict[str, Any]], None]] = []
_APPEND_MUTEX = threading.Lock()


def register_post_append_listener(listener: Callable[[Dict[str, Any]], None]) -> None:
    if listener not in _POST_APPEND_LISTENERS:
        _POST_APPEND_LISTENERS.append(listener)


def unregister_post_append_listener(listener: Callable[[Dict[str, Any]], None]) -> None:
    try:
        _POST_APPEND_LISTENERS.remove(listener)
    except ValueError:
        pass


# --------------------------------------------------------------------------
# Feed root resolution (unified — replaces both legacy and MCP path logic)
# --------------------------------------------------------------------------

_CANONICAL_FEED_ROOT: Optional[Path] = None


class FeedRootMismatch(RuntimeError):
    """Feed root pinned at startup must not drift."""


class FeedWriteCancelled(RuntimeError):
    """Raised when a cooperative cancellation request reaches a write path."""


def _resolve_feed_root() -> Path:
    """Resolve `.mica/agent_feed/` anchored to the MICA repo root.

    Priority:
      1. ``MICA_AGENT_FEED_ROOT`` env override.
      2. Walk up from CWD looking for ``.mica/.canonical`` sentinel
         (outermost wins if multiple — resolves Slice-6 §1 drift).
      3. Walk up from CWD looking for any ``.mica`` directory.
      4. Fallback to ``<cwd>/.mica/agent_feed``.
    """
    global _CANONICAL_FEED_ROOT
    override = os.environ.get("MICA_AGENT_FEED_ROOT")
    if override:
        resolved = Path(override).expanduser().resolve()
        if _CANONICAL_FEED_ROOT is None:
            _CANONICAL_FEED_ROOT = resolved
        elif _CANONICAL_FEED_ROOT != resolved:
            raise FeedRootMismatch(
                f"feed root drift: pinned={_CANONICAL_FEED_ROOT} "
                f"but env now points to {resolved}"
            )
        return resolved

    cwd = Path.cwd().resolve()
    canonical_hit: Optional[Path] = None
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".mica" / ".canonical").is_file():
            canonical_hit = candidate  # keep walking; outermost wins
    if canonical_hit is not None:
        return canonical_hit / ".mica" / "agent_feed"
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".mica").is_dir():
            return candidate / ".mica" / "agent_feed"
    return cwd / ".mica" / "agent_feed"


def pin_feed_root(path: Path | str) -> Path:
    """Idempotently pin the canonical feed root.

    Used by API/driver/worker startup to lock the feed location for the
    process. Subsequent ``_resolve_feed_root()`` calls that disagree raise
    :class:`FeedRootMismatch`.
    """
    global _CANONICAL_FEED_ROOT
    resolved = Path(path).expanduser().resolve()
    if _CANONICAL_FEED_ROOT is not None and _CANONICAL_FEED_ROOT != resolved:
        raise FeedRootMismatch(
            f"feed root re-pin mismatch: existing={_CANONICAL_FEED_ROOT} new={resolved}"
        )
    _CANONICAL_FEED_ROOT = resolved
    os.environ["MICA_AGENT_FEED_ROOT"] = str(resolved)
    return resolved


def _feed_root() -> Path:
    root = _resolve_feed_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _feed_file() -> Path:
    return _feed_root() / "feed.jsonl"


def _index_file() -> Path:
    return _feed_root() / "feed_index.json"


def _outbox_file() -> Path:
    return _feed_root() / "mirror_outbox.jsonl"


# --------------------------------------------------------------------------
# Cross-platform advisory lock
# --------------------------------------------------------------------------

@contextmanager
def _locked_append(path: Path) -> Iterator[Any]:
    """Advisory append-lock around ``path`` (best-effort)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", encoding="utf-8")
    try:
        if sys.platform == "win32":
            try:
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                _locked = True
            except Exception:
                _locked = False
        else:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                _locked = True
            except Exception:
                _locked = False
        try:
            yield fh
        finally:
            if _locked:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Time utilities
# --------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Canonical post normalization
# --------------------------------------------------------------------------

def _normalize_post(post: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a post dict to the canonical schema.

    - Ensures `id` and `timestamp` are present.
    - Auto-derives legacy aliases (`ts`, `title`, `body`) from primary fields.
    - Auto-derives primary fields (`intent`, `content`) from legacy fields
      when the primary fields are missing.
    - Validates and coerces `post_type` and `topic`.
    """
    # ID
    post_id = post.get("id") or str(uuid.uuid4())
    post["id"] = post_id

    # Timestamp — primary field
    ts = post.get("timestamp") or post.get("ts") or _utcnow_iso()
    post["timestamp"] = ts
    post["ts"] = ts  # legacy alias

    # Intent/Content ↔ Title/Body bidirectional derivation
    intent = post.get("intent", "").strip()
    content = post.get("content", "").strip()
    title = post.get("title", "").strip()
    body = post.get("body", "").strip()

    # If primary fields missing but legacy fields present, derive primaries
    if not intent and title:
        intent = title
    if not content and body:
        content = body

    # If legacy fields missing but primary fields present, derive legacy
    if not title and intent:
        title = intent[:500]
    if not body and content:
        body = content[:20000]

    post["intent"] = intent
    post["content"] = content
    post["title"] = title
    post["body"] = body

    # Post type validation
    post_type = post.get("post_type", "cue")
    if post_type not in POST_TYPES:
        post_type = "cue"
    post["post_type"] = post_type

    # Topic normalization — known categories are preserved, unknown topics are
    # also preserved (slugged) so agents can publish new domains without being
    # collapsed into "general".
    topic_raw = post.get("topic", "general")
    if topic_raw is None:
        topic_raw = "general"
    topic = str(topic_raw).strip().lower()
    if not topic:
        topic = "general"
    else:
        topic = topic.replace(" ", "_")
        topic = "".join(ch for ch in topic if ch.isalnum() or ch in "_-.:/")
        if not topic:
            topic = "general"
        elif len(topic) > 80:
            topic = topic[:80]
    post["topic"] = topic

    # Session phase derivation
    if post_type in ("session_open", "session_progress", "session_close"):
        phase = post_type.replace("session_", "")
        post.setdefault("session_phase", phase)

    # Ensure list fields are lists
    for list_field in ("artifacts", "evidence", "files_touched",
                       "target_agents", "context_questions",
                       "context_questions_answered", "next_actions"):
        val = post.get(list_field)
        if val is None:
            post[list_field] = []
        elif isinstance(val, str):
            post[list_field] = [x.strip() for x in val.split(",") if x.strip()]
        elif not isinstance(val, list):
            post[list_field] = []

    # Ensure dict fields are dicts
    post.setdefault("metadata", {})
    if not isinstance(post["metadata"], dict):
        post["metadata"] = {}

    raw_idempotency_key = post.get("idempotency_key") or post["metadata"].get("idempotency_key")
    if raw_idempotency_key is not None:
        folded_key = str(raw_idempotency_key).strip()
        if folded_key:
            post["idempotency_key"] = folded_key[:160]
            post["metadata"].setdefault("idempotency_key", post["idempotency_key"])
        else:
            post.pop("idempotency_key", None)
            post["metadata"].pop("idempotency_key", None)

    # Boolean fields
    post["graph_updated"] = bool(post.get("graph_updated", False))
    post["memory_updated"] = bool(post.get("memory_updated", False))

    # Progress count (for session_progress posts)
    post.setdefault("progress_count", 0)

    return post


def compute_content_hash(post: Dict[str, Any]) -> str:
    """SHA-256 over a canonical projection of the feed post.

    Hash domain: sha256(agent_id | post_type | topic | intent | content | timestamp)
    """
    parts = [
        str(post.get("agent_id") or ""),
        str(post.get("post_type") or ""),
        str(post.get("topic") or ""),
        str(post.get("intent") or post.get("title") or ""),
        str(post.get("content") or post.get("body") or ""),
        str(post.get("timestamp") or post.get("ts") or ""),
    ]
    payload = "\u001f".join(parts).encode("utf-8")  # unit separator
    return hashlib.sha256(payload).hexdigest()


def compute_idempotency_key(post: Dict[str, Any]) -> str:
    """SHA-256 over the stable publish contract, excluding volatile fields.

    This is used to make publish retries idempotent when the client does not
    provide an explicit idempotency token and a previous attempt may have
    succeeded before the UI observed the result.
    """
    normalized = _normalize_post(dict(post))
    metadata = normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {}
    parts = [
        str(normalized.get("agent_id") or ""),
        str(normalized.get("post_type") or ""),
        str(normalized.get("topic") or ""),
        str(normalized.get("parent_id") or ""),
        str(normalized.get("session_id") or ""),
        str(normalized.get("intent") or normalized.get("title") or ""),
        str(normalized.get("content") or normalized.get("body") or ""),
        "\u001e".join(str(item) for item in normalized.get("artifacts") or []),
        "\u001e".join(str(item) for item in normalized.get("evidence") or []),
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
    ]
    payload = "\u001f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _cancel_requested(cancel_event: Any) -> bool:
    return bool(cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set())


def _ensure_not_cancelled(cancel_event: Any, stage: str) -> None:
    if _cancel_requested(cancel_event):
        raise FeedWriteCancelled(stage)


def get_post(post_id: str) -> Optional[Dict[str, Any]]:
    """Return a single post by id, or ``None`` if it is absent."""
    folded_post_id = str(post_id or "").strip()
    if not folded_post_id:
        return None
    ff = _feed_file()
    if not ff.exists():
        return None
    with open(ff, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                post = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if post.get("id") == folded_post_id:
                return post
    return None


def _find_post_by_idempotency_key(idempotency_key: Optional[str]) -> Optional[Dict[str, Any]]:
    folded_key = str(idempotency_key or "").strip()
    if not folded_key:
        return None
    ff = _feed_file()
    if not ff.exists():
        return None
    last_match: Optional[Dict[str, Any]] = None
    with open(ff, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                post = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(post.get("idempotency_key") or "").strip() == folded_key:
                last_match = post
    return last_match


# --------------------------------------------------------------------------
# Core I/O
# --------------------------------------------------------------------------

def append_post(post: Dict[str, Any], mirror: bool = True, cancel_event: Any = None) -> str:
    """Append a single normalized post to the JSONL feed. Returns the post id.

    When ``mirror=True`` (default) and the post type is eligible for
    mirroring, the post is also filed to MemPalace via
    :func:`mirror_to_mempalace`.  Mirror failures are enqueued in the
    retry outbox automatically.
    """
    with _APPEND_MUTEX:
        normalized = _normalize_post(post)
        post_id = normalized["id"]
        existing = _find_post_by_idempotency_key(normalized.get("idempotency_key"))
        if existing is not None and existing.get("id"):
            return str(existing["id"])

        _ensure_not_cancelled(cancel_event, "before_append")
        line = json.dumps(normalized, ensure_ascii=False, sort_keys=False)
        with _locked_append(_feed_file()) as fh:
            _ensure_not_cancelled(cancel_event, "before_write")
            fh.write(line + "\n")
            fh.flush()
        _update_index(normalized)

    # Memory mirror (best-effort, never blocks the append)
    if mirror and normalized.get("post_type") in MIRROR_ELIGIBLE_TYPES:
        try:
            mirror_to_mempalace(normalized)
        except Exception:
            pass  # mirror failure must never break the append

    for listener in list(_POST_APPEND_LISTENERS):
        try:
            listener(dict(normalized))
        except Exception:
            pass

    return post_id


def read_posts(
    limit: int = 50,
    topic: Optional[str] = None,
    agent_id: Optional[str] = None,
    post_type: Optional[str] = None,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read recent posts, newest-first, with optional filters."""
    ff = _feed_file()
    if not ff.exists():
        return []

    posts: List[Dict[str, Any]] = []
    with open(ff, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                post = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Apply filters
            if topic and post.get("topic") != topic:
                continue
            if agent_id and post.get("agent_id") != agent_id:
                continue
            if post_type and post.get("post_type") != post_type:
                continue
            if since and (post.get("timestamp") or post.get("ts", "")) < since:
                continue
            posts.append(post)

    # Newest first, limited
    posts.reverse()
    return posts[:max(1, min(limit, 500))]


def _update_index(post: Dict[str, Any]) -> None:
    """Increment topic/agent/type counters in the summary index."""
    path = _index_file()
    try:
        if path.exists():
            idx = json.loads(path.read_text(encoding="utf-8"))
        else:
            idx = {"total": 0, "per_topic": {}, "per_agent": {}, "per_type": {}}
    except Exception:
        idx = {"total": 0, "per_topic": {}, "per_agent": {}, "per_type": {}}

    idx["total"] = int(idx.get("total", 0)) + 1
    for key, bucket in (
        ("per_topic", post.get("topic") or "general"),
        ("per_agent", post.get("agent_id") or "unknown"),
        ("per_type", post.get("post_type") or "cue"),
    ):
        mapping = idx.setdefault(key, {})
        mapping[bucket] = int(mapping.get(bucket, 0)) + 1
    idx["last_updated"] = _utcnow_iso()

    try:
        path.write_text(
            json.dumps(idx, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_index() -> Dict[str, Any]:
    """Load the cached index. Reconstructs from JSONL if missing."""
    path = _index_file()
    idx: Dict[str, Any] = {"total": 0, "per_topic": {}, "per_agent": {}, "per_type": {}}
    if path.exists():
        try:
            idx = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            idx = {"total": 0, "per_topic": {}, "per_agent": {}, "per_type": {}}

    if not idx.get("total"):
        posts = read_posts(limit=None)
        idx = {"total": len(posts), "per_topic": {}, "per_agent": {}, "per_type": {}}
        for p in posts:
            for key, bucket in (
                ("per_topic", p.get("topic") or "general"),
                ("per_agent", p.get("agent_id") or "unknown"),
                ("per_type", p.get("post_type") or "cue"),
            ):
                mapping = idx[key]
                mapping[bucket] = int(mapping.get(bucket, 0)) + 1
    return idx


# --------------------------------------------------------------------------
# Session lifecycle API
# --------------------------------------------------------------------------

def open_session(
    agent_id: str,
    task_description: str,
    context_questions: str,
    files_under_review: Optional[str] = None,
    current_situation: Optional[str] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Open a coordination session. Returns session_id and post dict."""
    if not agent_id or not agent_id.strip():
        return {"error": "agent_id is required"}
    if not task_description or not task_description.strip():
        return {"error": "task_description is required"}
    if not context_questions or not context_questions.strip():
        return {"error": "context_questions is required"}

    session_id = str(uuid.uuid4())[:8]
    questions = [q.strip() for q in context_questions.replace("|", ",").split(",") if q.strip()]
    files = [f.strip() for f in files_under_review.split(",") if f.strip()] if files_under_review else []

    post = {
        "post_type": "session_open",
        "agent_id": agent_id.strip(),
        "topic": "governance",
        "session_id": session_id,
        "session_phase": "open",
        "intent": f"SESSION_OPEN: {task_description.strip()[:120]}",
        "content": task_description.strip(),
        "context_questions": questions,
        "files_under_review": files,
        "current_situation": (current_situation or "").strip(),
        "next_actions": [],
        "graph_updated": False,
        "memory_updated": False,
        "progress_count": 0,
    }
    post_id = append_post(post, cancel_event=cancel_event)
    return {
        "status": "session_opened",
        "session_id": session_id,
        "post_id": post_id,
        "questions_registered": questions,
        "law": "MINIMUM 3 calls to update_session_progress during this session. Close with publish_cue(post_type='session_close', session_id=...)",
    }


def update_progress(
    session_id: str,
    agent_id: str,
    progress_notes: str,
    current_situation: str,
    next_actions: Optional[str] = None,
    files_touched_so_far: Optional[str] = None,
    graph_updated: bool = False,
    memory_updated: bool = False,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Append a progress checkpoint to an open session."""
    if not session_id or not session_id.strip():
        return {"error": "session_id is required"}
    if not agent_id or not agent_id.strip():
        return {"error": "agent_id is required"}
    if not progress_notes or not progress_notes.strip():
        return {"error": "progress_notes is required"}
    if not current_situation or not current_situation.strip():
        return {"error": "current_situation is required"}

    next_list = [a.strip() for a in next_actions.replace("|", ",").split(",") if a.strip()] if next_actions else []
    files = [f.strip() for f in files_touched_so_far.split(",") if f.strip()] if files_touched_so_far else []

    post = {
        "post_type": "session_progress",
        "agent_id": agent_id.strip(),
        "topic": "governance",
        "session_id": session_id.strip(),
        "session_phase": "progress",
        "intent": f"SESSION_PROGRESS [{session_id.strip()}]: {progress_notes.strip()[:80]}",
        "content": progress_notes.strip(),
        "current_situation": current_situation.strip(),
        "next_actions": next_list,
        "files_touched": files,
        "graph_updated": bool(graph_updated),
        "memory_updated": bool(memory_updated),
    }
    post_id = append_post(post, cancel_event=cancel_event)
    return {
        "status": "progress_published",
        "session_id": session_id.strip(),
        "post_id": post_id,
        "reminder": "Call update_session_progress at least 3 times total. Close with publish_cue(post_type='session_close').",
    }


def publish_cue(
    intent: str,
    content: str,
    agent_id: str,
    topic: str = "general",
    post_type: str = "cue",
    biological_context: Optional[str] = None,
    target_agents: Optional[str] = None,
    evidence: Optional[str] = None,
    parent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    graph_updated: bool = False,
    memory_updated: bool = False,
    files_touched: Optional[str] = None,
    context_questions_answered: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Publish a cue, decision, tombstone, hypothesis, insight, or session_close."""
    if post_type not in POST_TYPES:
        return {"error": f"Invalid post_type '{post_type}'. Valid: {sorted(POST_TYPES)}"}
    if topic not in TOPIC_CATEGORIES:
        return {"error": f"Invalid topic '{topic}'. Valid: {sorted(TOPIC_CATEGORIES)}"}
    if not intent or not intent.strip():
        return {"error": "intent is required and cannot be empty"}
    if not content or not content.strip():
        return {"error": "content is required and cannot be empty"}
    if not agent_id or not agent_id.strip():
        return {"error": "agent_id is required"}

    post = {
        "post_type": post_type,
        "agent_id": agent_id.strip(),
        "topic": topic,
        "intent": intent.strip(),
        "content": content.strip(),
        "biological_context": biological_context,
        "target_agents": [a.strip() for a in target_agents.split(",")] if target_agents else [],
        "evidence": [e.strip() for e in evidence.split(",")] if evidence else [],
        "parent_id": parent_id,
        "session_id": session_id.strip() if session_id else None,
        "session_phase": "close" if post_type == "session_close" else None,
        "graph_updated": bool(graph_updated),
        "memory_updated": bool(memory_updated),
        "files_touched": [f.strip() for f in files_touched.split(",") if f.strip()] if files_touched else [],
        "context_questions_answered": [q.strip() for q in context_questions_answered.split(",") if q.strip()] if context_questions_answered else [],
    }
    post["idempotency_key"] = str(idempotency_key or compute_idempotency_key(post)).strip()
    post_id = append_post(post, cancel_event=cancel_event)

    # Mandatory daily report generation on session close
    if post_type == "session_close":
        try:
            generate_daily_report()
        except Exception:
            pass  # report generation failure must not break the publish

    return {
        "status": "published",
        "post_id": post_id,
        "agent_id": post["agent_id"],
        "topic": topic,
        "post_type": post_type,
        "visible_to": post["target_agents"] or ["all"],
    }


def comment_on_post(
    post_id: str,
    insight: str,
    agent_id: str,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Comment on another agent's post."""
    if not post_id or not post_id.strip():
        return {"error": "post_id is required"}
    if not insight or not insight.strip():
        return {"error": "insight is required"}
    if not agent_id or not agent_id.strip():
        return {"error": "agent_id is required"}

    post = {
        "post_type": "comment",
        "agent_id": agent_id.strip(),
        "topic": "general",
        "intent": f"Peer review of post {post_id}",
        "content": insight.strip(),
        "parent_id": post_id.strip(),
        "target_agents": [],
        "evidence": [],
    }
    cid = append_post(post, cancel_event=cancel_event)
    return {
        "status": "comment_published",
        "comment_id": cid,
        "parent_post_id": post_id,
        "agent_id": post["agent_id"],
    }


def get_thread(root_id: str) -> Dict[str, Any]:
    """Return a root post plus all its descendants (breadth-first).

    Supports two lookup modes:
    1. By post ``id`` — finds the post and all children via ``parent_id``.
    2. By ``session_id`` — finds all posts belonging to a session (session_open,
       session_progress, session_close) ordered chronologically.
    """
    if not root_id or not root_id.strip():
        return {"error": "post_id is required"}
    root_id = root_id.strip()

    all_posts = read_posts(limit=500)
    by_id = {p.get("id"): p for p in all_posts if p.get("id")}
    root = by_id.get(root_id)

    if root is None:
        # Fallback: treat root_id as a session_id and gather all session posts
        session_posts = [p for p in all_posts if p.get("session_id") == root_id]
        session_posts.sort(key=lambda p: p.get("timestamp") or p.get("ts", ""))
        if session_posts:
            return {
                "root": session_posts[0],
                "comments": session_posts[1:],
                "comment_count": len(session_posts) - 1,
                "session_id": root_id,
            }
        return {"error": f"Post {root_id} not found"}

    # Build parent→children index
    children: Dict[str, List[Dict[str, Any]]] = {}
    for p in all_posts:
        parent = p.get("parent_id")
        if parent:
            children.setdefault(parent, []).append(p)

    thread: List[Dict[str, Any]] = [root]
    queue: List[str] = [root_id]
    seen = {root_id}
    while queue:
        cur = queue.pop(0)
        for c in children.get(cur, []):
            cid = c.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                thread.append(c)
                queue.append(cid)

    return {
        "root": root,
        "comments": thread[1:],
        "comment_count": len(thread) - 1,
    }


def get_stats(topic: Optional[str] = None) -> Dict[str, Any]:
    """Summary counters over the feed."""
    idx = load_index()
    if topic:
        topic_total = int(idx.get("per_topic", {}).get(topic, 0))
        return {
            "topic": topic,
            "count": topic_total,
            "total": int(idx.get("total", 0)),
        }
    return idx


def search_posts(
    query: str,
    limit: int = 5,
    topic: Optional[str] = None,
    agent_id: Optional[str] = None,
    post_type: Optional[str] = None,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    """Search feed posts by keyword matching across intent, content, topic, evidence, files_touched."""
    import re
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_./:-]+", (query or "").lower()) if len(t) >= 3]
    tokens = list(dict.fromkeys(tokens))  # deduplicate while preserving order

    posts = read_posts(limit=200, topic=topic, agent_id=agent_id, post_type=post_type, since=since)
    if not tokens:
        return {"tokens": [], "matches": posts[:limit]}

    matches = []
    for post in posts:
        haystack = " ".join([
            str(post.get("intent") or ""),
            str(post.get("content") or ""),
            str(post.get("topic") or ""),
            " ".join(post.get("evidence") or []),
            " ".join(post.get("files_touched") or []),
            " ".join(post.get("context_questions_answered") or []),
        ]).lower()
        score = sum(1 for token in tokens if token in haystack)
        if score <= 0:
            continue
        matches.append({"score": score, "post": post})

    matches.sort(key=lambda item: (item["score"], item["post"].get("timestamp", "")), reverse=True)
    score_values = [int(item["score"]) for item in matches if isinstance(item.get("score"), (int, float))]
    top_match_score = max(score_values) if score_values else 0
    avg_match_score = (sum(score_values) / len(score_values)) if score_values else 0.0

    return {
        "tokens": tokens,
        "match_count": len(matches),
        "top_match_score": top_match_score,
        "avg_match_score": round(avg_match_score, 3),
        "matches": [item["post"] for item in matches[:limit]],
    }


# --------------------------------------------------------------------------
# Memory Mirror — Feed → MemPalace bridge with retry queue
# --------------------------------------------------------------------------

# Post types eligible for mirroring (durable knowledge, not transient)
MIRROR_ELIGIBLE_TYPES = frozenset({
    "cue",
    "decision",
    "tombstone",
    "hypothesis",
    "artifact",
    "insight",
    "session_close",
})

# Maximum retry attempts before giving up
MAX_MIRROR_RETRIES = 5

# Mapping: feed topic → MemPalace wing
TOPIC_TO_WING = {
    "architecture": "wing_code",
    "biology": "wing_code",
    "errors": "wing_code",
    "literature": "wing_code",
    "orchestration": "wing_code",
    "infrastructure": "wing_code",
    "frontend": "wing_code",
    "memory": "wing_code",
    "graphrag": "wing_code",
    "bsm": "wing_code",
    "scientific_protocol": "wing_code",
    "deployment": "wing_code",
    "governance": "wing_code",
    "general": "wing_code",
}

# Mapping: agent_id prefix → MemPalace wing override
AGENT_TO_WING = {
    "AGENTICDRIVER": "wing_code",
    "SYSTEM_SYNTHESIS": "wing_system_synthesis_auditor",
    "CAPABILITY_AUTHORITY": "wing_code",
    "PROMPT_RUNTIME": "wing_code",
    "CONTEXT_ECONOMY": "wing_code",
    "RAILWAY_DEPLOY": "wing_code",
    "EPISTEMIC": "wing_code",
    "MILVUS_RAG": "wing_code",
    "GRAPHRAG": "wing_code",
    "LITERATURE": "wing_code",
    "KB_SUBSTRATE": "wing_code",
    "BSM_NEWGEN": "wing_code",
    "SCIENTIFIC_PROTOCOL": "wing_code",
    "FRONTEND": "wing_code",
    "RUNTIME_GROUND": "wing_code",
    "FEEDCORE": "wing_code",
}


def _resolve_wing(post: Dict[str, Any]) -> str:
    """Resolve the MemPalace wing for a post.

    Priority: agent_id prefix match → topic mapping → default.
    """
    agent_id = post.get("agent_id", "")
    for prefix, wing in AGENT_TO_WING.items():
        if agent_id.upper().startswith(prefix.upper()):
            return wing
    topic = post.get("topic", "general")
    return TOPIC_TO_WING.get(topic, "wing_code")


def _resolve_room(post: Dict[str, Any]) -> str:
    """Resolve the MemPalace room for a post.

    Uses post_type as the primary room, with sub-distinction by topic
    for session posts.
    """
    post_type = post.get("post_type", "cue")
    if post_type.startswith("session_"):
        return "session_lifecycle"
    return post_type


def _build_mirror_content(post: Dict[str, Any]) -> str:
    """Build the verbatim content string for MemPalace filing.

    This MUST be the exact content, not a summary, per MemPalace contract.
    """
    parts = []
    agent = post.get("agent_id", "unknown")
    ptype = post.get("post_type", "cue")
    ts = post.get("timestamp", "")
    intent = post.get("intent", "")
    content = post.get("content", "")
    topic = post.get("topic", "")

    parts.append("[{ts}] {agent} | {ptype} | {topic}".format(
        ts=ts, agent=agent, ptype=ptype, topic=topic,
    ))
    parts.append("INTENT: " + intent)
    parts.append("CONTENT: " + content)

    # Session-specific fields
    session_id = post.get("session_id")
    if session_id:
        parts.append("SESSION: " + str(session_id))
    files = post.get("files_touched")
    if files:
        parts.append("FILES: " + ", ".join(files) if isinstance(files, list) else str(files))
    evidence = post.get("evidence")
    if evidence:
        parts.append("EVIDENCE: " + ", ".join(evidence) if isinstance(evidence, list) else str(evidence))
    questions = post.get("context_questions_answered")
    if questions:
        parts.append("QUESTIONS_ANSWERED: " + ", ".join(questions) if isinstance(questions, list) else str(questions))
    graph_upd = post.get("graph_updated")
    mem_upd = post.get("memory_updated")
    if graph_upd or mem_upd:
        parts.append("FLAGS: graph_updated={} memory_updated={}".format(graph_upd, mem_upd))

    return "\n".join(parts)


def _enqueue_mirror(post: Dict[str, Any], error: str) -> None:
    """Append a failed mirror attempt to the retry outbox.

    Outbox entry schema:
        {
            "post_id": "<id>",
            "post": { ... },
            "error": "<error message>",
            "attempts": 1,
            "enqueued_at": "<ISO timestamp>",
            "next_retry_at": "<ISO timestamp>"
        }
    """
    import time
    post_id = post.get("id", "unknown")
    # Read existing outbox to check current attempt count
    existing_attempts = 0
    ob = _outbox_file()
    if ob.exists():
        try:
            with open(ob, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("post_id") == post_id:
                            existing_attempts = entry.get("attempts", 0)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    attempts = existing_attempts + 1
    if attempts > MAX_MIRROR_RETRIES:
        return  # give up silently — the post is in the feed regardless

    # Exponential backoff: 2^attempts seconds
    backoff_seconds = min(2 ** attempts, 300)  # cap at 5 minutes
    now = datetime.now(timezone.utc)
    import datetime as _dt
    next_retry = (now + _dt.timedelta(seconds=backoff_seconds)).isoformat()

    entry = {
        "post_id": post_id,
        "post": post,
        "error": error,
        "attempts": attempts,
        "enqueued_at": _utcnow_iso(),
        "next_retry_at": next_retry,
    }
    with _locked_append(ob) as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        fh.flush()


def _get_mempalace_path() -> str:
    """Resolve the canonical MemPalace palace path.

    Priority:
      1. ``MICA_MEMPALACE_PALACE_PATH`` env override (explicit MICA config).
      2. ``MEMPALACE_PALACE_PATH`` env var (standard MemPalace config).
      3. Repository-local agents palace: ``<repo_root>/external/mempalace/palaces/agents``.
      4. Default MemPalace path (``~/.mempalace/palace``).

    This ensures FeedCore mirrors to the SAME palace that the MCP tools
    read from, avoiding split-brain.
    """
    mica_override = os.environ.get("MICA_MEMPALACE_PALACE_PATH")
    if mica_override:
        return mica_override
    standard_override = os.environ.get("MEMPALACE_PALACE_PATH")
    if standard_override:
        return standard_override
    # Repository-local agents palace (same one opencode MCP uses)
    repo_root = Path(__file__).resolve()
    for _ in range(8):  # walk up to repo root
        repo_root = repo_root.parent
        if (repo_root / "external" / "mempalace" / "palaces" / "agents").is_dir():
            return str(repo_root / "external" / "mempalace" / "palaces" / "agents")
    # Fallback to default
    return str(Path.home() / ".mempalace" / "palace")


def _ensure_mempalace_env() -> None:
    """Ensure MEMPALACE_PALACE_PATH is set before importing mempalace."""
    if not os.environ.get("MEMPALACE_PALACE_PATH"):
        os.environ["MEMPALACE_PALACE_PATH"] = _get_mempalace_path()


def _load_mempalace_mcp_symbols(*symbol_names: str) -> Tuple[Any, ...]:
    """Import MemPalace MCP helpers without leaving stdout/stderr redirected.

    ``mempalace.mcp_server`` protects its own stdio by redirecting stdout to
    stderr at import time and only restoring it in its process ``main()``.
    When FeedCore imports that module in-process for mirror writes, the feed
    MCP server can be left with stdout poisoned, causing subsequent JSON-RPC
    responses to show up as ``server stderr`` or break the client stream.

    We restore the original stdio immediately after import so FeedCore can use
    the helper functions without inheriting the MCP server's process-level I/O
    mutation.
    """
    module = importlib.import_module("mempalace.mcp_server")
    restore_stdout = getattr(module, "_restore_stdout", None)
    if callable(restore_stdout):
        try:
            restore_stdout()
        except Exception:
            pass
    return tuple(getattr(module, symbol_name) for symbol_name in symbol_names)


def mirror_to_mempalace(post: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror a feed post to MemPalace with dedup check.

    Returns:
        {"mirrored": True, "drawer_id": "..."} on success,
        {"mirrored": False, "reason": "duplicate"} if dedup catches it,
        {"mirrored": False, "reason": "error", "error": "..."} on failure
        (and enqueues to retry outbox).
    """
    post_id = post.get("id", "unknown")
    post_type = post.get("post_type", "")

    # Only mirror eligible types
    if post_type not in MIRROR_ELIGIBLE_TYPES:
        return {"mirrored": False, "reason": "not_eligible", "post_type": post_type}

    content = _build_mirror_content(post)
    wing = _resolve_wing(post)
    room = _resolve_room(post)
    agent = post.get("agent_id", "feed_mirror")
    source = "feed:" + post_id

    # Step 1: Dedup check
    _ensure_mempalace_env()
    try:
        (tool_check_duplicate,) = _load_mempalace_mcp_symbols("tool_check_duplicate")
        dup_result = tool_check_duplicate(content=content, threshold=0.9)
        if isinstance(dup_result, dict) and dup_result.get("is_duplicate"):
            return {"mirrored": False, "reason": "duplicate", "post_id": post_id}
    except Exception as exc:
        # If dedup fails, proceed anyway — better to have a potential dup
        # than to lose the mirror entirely
        pass

    # Step 2: File to MemPalace
    _ensure_mempalace_env()
    try:
        (tool_add_drawer,) = _load_mempalace_mcp_symbols("tool_add_drawer")
        result = tool_add_drawer(
            wing=wing,
            room=room,
            content=content,
            source_file=source,
            added_by=agent,
        )
        drawer_id = None
        if isinstance(result, dict):
            drawer_id = result.get("drawer_id") or result.get("id")
        return {"mirrored": True, "drawer_id": drawer_id, "wing": wing, "room": room, "post_id": post_id}
    except Exception as exc:
        # Enqueue for retry
        _enqueue_mirror(post, str(exc))
        return {"mirrored": False, "reason": "error", "error": str(exc), "post_id": post_id, "enqueued_for_retry": True}


def flush_mirror_outbox(max_items: int = 20) -> Dict[str, Any]:
    """Flush pending mirror entries from the retry outbox.

    Processes entries whose ``next_retry_at`` has passed, up to
    ``max_items``. Successfully mirrored entries are removed.

    Returns:
        Summary dict with flushed count, remaining count, and errors.
    """
    ob = _outbox_file()
    if not ob.exists():
        return {"flushed": 0, "remaining": 0, "errors": []}

    # Read all entries
    entries = []
    try:
        with open(ob, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        return {"flushed": 0, "remaining": 0, "errors": ["read_error"]}

    if not entries:
        return {"flushed": 0, "remaining": 0, "errors": []}

    now_str = _utcnow_iso()
    # Parse current time once for reliable comparison
    now_dt = datetime.now(timezone.utc)
    flushed = 0
    still_pending = []
    errors = []

    for entry in entries:
        post_id = entry.get("post_id", "")
        next_retry_str = entry.get("next_retry_at", "")
        attempts = entry.get("attempts", 0)

        # Parse next_retry_at robustly (handles Z and +00:00 suffixes)
        next_retry_dt = None
        if next_retry_str:
            try:
                # Normalize Z → +00:00 for fromisoformat compatibility
                normalized = next_retry_str.replace("Z", "+00:00")
                next_retry_dt = datetime.fromisoformat(normalized)
            except (ValueError, TypeError):
                pass

        # Skip if not yet time (compare as datetimes)
        if next_retry_dt and next_retry_dt > now_dt:
            still_pending.append(entry)
            continue

        if attempts > MAX_MIRROR_RETRIES:
            continue  # drop — exceeded max retries

        # Try mirroring
        post = entry.get("post", {})
        result = mirror_to_mempalace(post)

        if result.get("mirrored"):
            flushed += 1
        elif result.get("enqueued_for_retry"):
            # The enqueue inside mirror_to_mempalace will add a new entry,
            # but we should track the old one as consumed to avoid double-processing
            still_pending.append({
                "post_id": post_id,
                "post": post,
                "error": result.get("error", "unknown"),
                "attempts": attempts + 1,
                "enqueued_at": entry.get("enqueued_at", ""),
                "next_retry_at": entry.get("next_retry_at", ""),
            })
            errors.append({"post_id": post_id, "error": result.get("error", "unknown")})
        else:
            # Duplicate or not eligible — remove from outbox
            if result.get("reason") == "duplicate":
                flushed += 1  # count as resolved
            else:
                still_pending.append(entry)

    # Rewrite outbox with remaining entries
    if still_pending:
        with open(ob, "w", encoding="utf-8") as fh:
            for entry in still_pending:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    else:
        # Clean up empty outbox
        try:
            ob.unlink()
        except Exception:
            pass

    return {
        "flushed": flushed,
        "remaining": len(still_pending),
        "errors": errors,
    }


# --------------------------------------------------------------------------
# Daily Context Reports — mandatory per-day reconstruction documents
# --------------------------------------------------------------------------

def _reports_dir() -> Path:
    """Resolve the daily reports directory.

    Location: ``<feed_root>/../daily_reports/`` (sibling of agent_feed).
    Created automatically on first call.
    """
    reports = _feed_root().parent / "daily_reports"
    reports.mkdir(parents=True, exist_ok=True)
    return reports


def generate_daily_report(date_str: Optional[str] = None) -> Dict[str, Any]:
    """Generate a mandatory daily context report for a given date.

    Compiles all feed activity for the specified date into a structured
    JSON + human-readable Markdown document. The report is written to
    ``.mica/daily_reports/YYYY-MM-DD.{json,md}`` and also mirrored to
    MemPalace.

    This function is called automatically on ``session_close`` and can
    also be invoked manually.

    Args:
        date_str: ISO date string (``YYYY-MM-DD``). Defaults to today UTC.

    Returns:
        Dict with report metadata, file paths, and summary counts.
    """
    # Resolve date
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_str = date_str.strip()

    # Validate format
    try:
        _ = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return {"error": f"Invalid date format '{date_str}'. Expected YYYY-MM-DD."}

    # Time bounds for the date
    since = date_str + "T00:00:00Z"
    until = date_str + "T23:59:59Z"

    # Read all posts for this date
    all_posts = read_posts(limit=500, since=since)
    # Filter to only posts within the date
    day_posts = []
    for p in all_posts:
        ts = p.get("timestamp") or p.get("ts") or ""
        if ts.startswith(date_str):
            day_posts.append(p)

    # Sort chronologically
    day_posts.sort(key=lambda p: p.get("timestamp") or p.get("ts") or "")

    # Compile structured data
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    by_agent: Dict[str, int] = {}
    all_files_touched: List[str] = []
    all_sessions: Dict[str, Dict[str, Any]] = {}
    all_decisions: List[Dict[str, Any]] = []
    all_tombstones: List[Dict[str, Any]] = []
    all_hypotheses: List[Dict[str, Any]] = []
    all_insights: List[Dict[str, Any]] = []
    all_evidence: List[str] = []

    for p in day_posts:
        ptype = p.get("post_type", "unknown")
        agent = p.get("agent_id", "unknown")

        by_type.setdefault(ptype, []).append(p)
        by_agent[agent] = by_agent.get(agent, 0) + 1

        # Collect files touched
        files = p.get("files_touched") or []
        if isinstance(files, str):
            files = [f.strip() for f in files.split(",") if f.strip()]
        all_files_touched.extend(files)

        # Collect evidence
        evidence = p.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [e.strip() for e in evidence.split(",") if e.strip()]
        all_evidence.extend(evidence)

        # Track sessions
        sid = p.get("session_id")
        if sid:
            if sid not in all_sessions:
                all_sessions[sid] = {
                    "session_id": sid,
                    "agent_id": agent,
                    "opened_at": None,
                    "closed_at": None,
                    "progress_count": 0,
                    "files_touched": [],
                    "questions": [],
                    "questions_answered": [],
                }
            session = all_sessions[sid]
            if ptype == "session_open":
                session["opened_at"] = p.get("timestamp")
                session["questions"] = p.get("context_questions", [])
            elif ptype == "session_progress":
                session["progress_count"] = session.get("progress_count", 0) + 1
                session["files_touched"].extend(files)
            elif ptype == "session_close":
                session["closed_at"] = p.get("timestamp")
                session["questions_answered"] = p.get("context_questions_answered", [])
                session["files_touched"].extend(files)
                session["graph_updated"] = p.get("graph_updated", False)
                session["memory_updated"] = p.get("memory_updated", False)

        # Categorize
        if ptype == "decision":
            all_decisions.append(p)
        elif ptype == "tombstone":
            all_tombstones.append(p)
        elif ptype == "hypothesis":
            all_hypotheses.append(p)
        elif ptype == "insight":
            all_insights.append(p)

    # Deduplicate files
    unique_files = sorted(set(all_files_touched))
    unique_evidence = sorted(set(all_evidence))

    # Build structured JSON report
    report = {
        "report_type": "daily_context_report",
        "date": date_str,
        "generated_at": _utcnow_iso(),
        "summary": {
            "total_posts": len(day_posts),
            "by_type": {k: len(v) for k, v in sorted(by_type.items())},
            "by_agent": dict(sorted(by_agent.items(), key=lambda x: -x[1])),
            "sessions_opened": sum(1 for s in all_sessions.values() if s.get("opened_at")),
            "sessions_closed": sum(1 for s in all_sessions.values() if s.get("closed_at")),
            "decisions_count": len(all_decisions),
            "tombstones_count": len(all_tombstones),
            "hypotheses_count": len(all_hypotheses),
            "insights_count": len(all_insights),
            "unique_files_touched": len(unique_files),
            "unique_evidence_refs": len(unique_evidence),
        },
        "sessions": all_sessions,
        "decisions": [
            {
                "id": d.get("id"),
                "agent_id": d.get("agent_id"),
                "intent": d.get("intent"),
                "content": d.get("content"),
                "evidence": d.get("evidence", []),
                "timestamp": d.get("timestamp"),
            }
            for d in all_decisions
        ],
        "tombstones": [
            {
                "id": t.get("id"),
                "agent_id": t.get("agent_id"),
                "intent": t.get("intent"),
                "content": t.get("content"),
                "timestamp": t.get("timestamp"),
            }
            for t in all_tombstones
        ],
        "hypotheses": [
            {
                "id": h.get("id"),
                "agent_id": h.get("agent_id"),
                "intent": h.get("intent"),
                "content": h.get("content"),
                "timestamp": h.get("timestamp"),
            }
            for h in all_hypotheses
        ],
        "insights": [
            {
                "id": ins.get("id"),
                "agent_id": ins.get("agent_id"),
                "intent": ins.get("intent"),
                "content": ins.get("content"),
                "timestamp": ins.get("timestamp"),
            }
            for ins in all_insights
        ],
        "files_touched": unique_files,
        "evidence_refs": unique_evidence,
        "timeline": [
            {
                "timestamp": p.get("timestamp"),
                "agent_id": p.get("agent_id"),
                "post_type": p.get("post_type"),
                "intent": (p.get("intent") or "")[:120],
                "session_id": p.get("session_id"),
            }
            for p in day_posts
        ],
    }

    # Write JSON report
    rdir = _reports_dir()
    json_path = rdir / f"{date_str}.json"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # Write Markdown report
    md_path = rdir / f"{date_str}.md"
    md_lines = _build_markdown_report(report)
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # Mirror report to MemPalace
    try:
        mirror_to_mempalace({
            "id": f"daily-report-{date_str}",
            "post_type": "artifact",
            "agent_id": "FEEDCORE_DAILY_REPORT",
            "topic": "governance",
            "intent": f"Daily context report for {date_str}",
            "content": json.dumps(report.get("summary", {}), indent=2, default=str),
            "timestamp": _utcnow_iso(),
        })
    except Exception:
        pass  # mirror failure must not break report generation

    return {
        "status": "generated",
        "date": date_str,
        "json_path": str(json_path),
        "md_path": str(md_path),
        "summary": report["summary"],
    }


def _build_markdown_report(report: Dict[str, Any]) -> List[str]:
    """Build a human-readable Markdown daily context report."""
    date = report.get("date", "UNKNOWN")
    summary = report.get("summary", {})
    lines: List[str] = []

    lines.append(f"# Daily Context Report — {date}")
    lines.append("")
    lines.append(f"**Generated:** {report.get('generated_at', 'N/A')}")
    lines.append(f"**Total Posts:** {summary.get('total_posts', 0)}")
    lines.append(f"**Sessions:** {summary.get('sessions_opened', 0)} opened / {summary.get('sessions_closed', 0)} closed")
    lines.append(f"**Decisions:** {summary.get('decisions_count', 0)} | **Tombstones:** {summary.get('tombstones_count', 0)} | **Hypotheses:** {summary.get('hypotheses_count', 0)} | **Insights:** {summary.get('insights_count', 0)}")
    lines.append("")

    # Activity by agent
    by_agent = summary.get("by_agent", {})
    if by_agent:
        lines.append("## Agents Active")
        lines.append("")
        lines.append("| Agent | Posts |")
        lines.append("|-------|-------|")
        for agent, count in sorted(by_agent.items(), key=lambda x: -x[1]):
            lines.append(f"| `{agent}` | {count} |")
        lines.append("")

    # Activity by type
    by_type = summary.get("by_type", {})
    if by_type:
        lines.append("## Posts by Type")
        lines.append("")
        lines.append("| Type | Count |")
        lines.append("|------|-------|")
        for ptype, count in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"| `{ptype}` | {count} |")
        lines.append("")

    # Sessions
    sessions = report.get("sessions", {})
    if sessions:
        lines.append("## Sessions")
        lines.append("")
        for sid, session in sessions.items():
            agent = session.get("agent_id", "?")
            opened = session.get("opened_at", "N/A")
            closed = session.get("closed_at", "N/A")
            progress = session.get("progress_count", 0)
            status = "CLOSED" if closed else "OPEN"
            lines.append(f"### Session `{sid}` — {agent} [{status}]")
            lines.append(f"- Opened: {opened}")
            if closed:
                lines.append(f"- Closed: {closed}")
            lines.append(f"- Progress updates: {progress}")
            files = session.get("files_touched", [])
            if files:
                unique = sorted(set(files))
                lines.append(f"- Files touched: {len(unique)}")
                for f in unique:
                    lines.append(f"  - `{f}`")
            questions = session.get("questions", [])
            if questions:
                lines.append("- Context questions:")
                for q in questions:
                    lines.append(f"  - {q}")
            answers = session.get("questions_answered", [])
            if answers:
                lines.append("- Questions answered:")
                for a in answers:
                    lines.append(f"  - {a}")
            lines.append("")

    # Decisions
    decisions = report.get("decisions", [])
    if decisions:
        lines.append("## Decisions")
        lines.append("")
        for d in decisions:
            ts = d.get("timestamp") or ""
            agent = d.get("agent_id", "?")
            intent = d.get("intent", "N/A")
            content = d.get("content", "")
            evidence = d.get("evidence", [])
            ts_short = ts[:19] if len(ts) >= 19 else ts
            lines.append(f"### [{ts_short}] {agent}")
            lines.append(f"**{intent}**")
            if content:
                lines.append(f"> {content[:500]}")
            if evidence:
                lines.append(f"Evidence: {', '.join(f'`{e}`' for e in evidence[:5])}")
            lines.append("")

    # Tombstones
    tombstones = report.get("tombstones", [])
    if tombstones:
        lines.append("## Tombstones (Errors/Failures)")
        lines.append("")
        for t in tombstones:
            ts = t.get("timestamp") or ""
            agent = t.get("agent_id", "?")
            intent = t.get("intent", "N/A")
            content = t.get("content", "")
            ts_short = ts[:19] if len(ts) >= 19 else ts
            lines.append(f"### [{ts_short}] {agent}")
            lines.append(f"**{intent}**")
            if content:
                lines.append(f"> {content[:500]}")
            lines.append("")

    # Hypotheses
    hypotheses = report.get("hypotheses", [])
    if hypotheses:
        lines.append("## Hypotheses")
        lines.append("")
        for h in hypotheses:
            ts = h.get("timestamp") or ""
            agent = h.get("agent_id", "?")
            intent = h.get("intent", "N/A")
            ts_short = ts[:19] if len(ts) >= 19 else ts
            lines.append(f"- [{ts_short}] **{agent}**: {intent}")
        lines.append("")

    # Insights
    insights = report.get("insights", [])
    if insights:
        lines.append("## Insights")
        lines.append("")
        for ins in insights:
            ts = ins.get("timestamp") or ""
            agent = ins.get("agent_id", "?")
            intent = ins.get("intent", "N/A")
            ts_short = ts[:19] if len(ts) >= 19 else ts
            lines.append(f"- [{ts_short}] **{agent}**: {intent}")
        lines.append("")

    # Files touched
    files = report.get("files_touched", [])
    if files:
        lines.append("## Files Touched")
        lines.append("")
        for f in files:
            lines.append(f"- `{f}`")
        lines.append("")

    # Timeline
    timeline = report.get("timeline", [])
    if timeline:
        lines.append("## Timeline")
        lines.append("")
        lines.append("| Time | Agent | Type | Intent | Session |")
        lines.append("|------|-------|------|--------|---------|")
        for t in timeline:
            raw_ts = t.get("timestamp") or ""
            ts = raw_ts[11:19] if len(raw_ts) >= 19 else raw_ts  # just HH:MM:SS
            agent = t.get("agent_id", "?")
            ptype = t.get("post_type", "?")
            intent = (t.get("intent") or "")[:50]
            sid = t.get("session_id") or ""
            lines.append(f"| {ts} | `{agent}` | {ptype} | {intent} | {sid} |")
        lines.append("")

    return lines


def get_daily_report(date_str: Optional[str] = None) -> Dict[str, Any]:
    """Read an existing daily report, or generate one if missing.

    Args:
        date_str: ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        The report dict, or an error if generation fails.
    """
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_str = date_str.strip()

    rdir = _reports_dir()
    json_path = rdir / f"{date_str}.json"

    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # fall through to regenerate

    return generate_daily_report(date_str=date_str)


def list_daily_reports() -> Dict[str, Any]:
    """List all available daily reports.

    Returns:
        Dict with dates list, count, and latest report date.
    """
    rdir = _reports_dir()
    dates = sorted(
        p.stem
        for p in rdir.glob("*.json")
        if len(p.stem) == 10 and p.stem[4] == "-" and p.stem[7] == "-"
    )
    return {
        "count": len(dates),
        "dates": dates,
        "latest": dates[-1] if dates else None,
        "reports_dir": str(rdir),
    }
