"""Agent feed — legacy async shim over FeedCore.

This module preserves the exact public API of the original R24
``agent_feed.py`` (async functions, ``title``/``body`` schema) but
delegates all I/O to the unified FeedCore engine.

Callers that import from this module see no behavioral change:
    - ``publish_cue()`` still accepts ``title``/``body``
    - ``scroll_agent_feed()`` still returns posts with ``ts``/``title``/``body``
    - ``open_session_signature()`` still accepts ``mission`` (mapped to intent)
    - ``update_session_progress()`` still accepts ``milestone`` (mapped to intent)
    - ``feed_stats()`` and ``feed_thread()`` work identically

FeedCore normalizes both schemas bidirectionally, so posts written via
this shim are readable by the MCP server and vice-versa.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

# ── Re-export FeedCore path utilities for callers that depend on them ────

from mica.agentic.feed_core import (
    FeedRootMismatch,
    pin_feed_root,
    compute_content_hash,
    POST_TYPES,
    TOPIC_CATEGORIES,
)

# ── FeedCore import ──────────────────────────────────────────────────────

_feed_core = None
_import_errors: List[str] = []

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GRAPH_JSON = _REPO_ROOT / "graphify-out" / "first_party_architecture" / "graph.json"
_AGENTS_PALACE = _REPO_ROOT / "external" / "mempalace" / "palaces" / "agents"
_GRAPH_CACHE: Optional[Dict[str, Any]] = None
_GRAPH_CACHE_MTIME_NS: Optional[int] = None
_COMMUNITY_LABELS = [
    "agentic_runtime",
    "scientific_protocol",
    "literature_intelligence",
    "memory_kb_graphrag",
    "api_and_services",
    "docs_context",
    "worker_execution",
    "bsm_core",
    "other_first_party",
]


def _node_source_file(node: Dict[str, Any]) -> str:
    return str(
        node.get("source_file")
        or node.get("path")
        or node.get("file")
        or node.get("id")
        or ""
    )


def _fc():
    """Lazily import FeedCore. Returns the module or None."""
    global _feed_core
    if _feed_core is not None:
        return _feed_core
    try:
        from mica.agentic import feed_core as fc
        _feed_core = fc
        return fc
    except Exception as exc:
        _import_errors.append(str(exc))
        _feed_core = False
        return None


def _tokenize_query(query: str) -> List[str]:
    seen: set[str] = set()
    tokens: List[str] = []
    for token in re.findall(r"[a-zA-Z0-9_./:-]+", (query or "").lower()):
        if len(token) < 3 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _load_graph_index() -> Optional[Dict[str, Any]]:
    global _GRAPH_CACHE, _GRAPH_CACHE_MTIME_NS
    if not _GRAPH_JSON.exists():
        return None
    stat = _GRAPH_JSON.stat()
    if _GRAPH_CACHE is not None and _GRAPH_CACHE_MTIME_NS == stat.st_mtime_ns:
        return _GRAPH_CACHE
    payload = json.loads(_GRAPH_JSON.read_text(encoding="utf-8"))
    nodes = payload.get("nodes", [])
    links = payload.get("links") or payload.get("edges") or []
    id_map = {node.get("id"): node for node in nodes if node.get("id")}
    adjacency: Dict[str, List[Dict[str, Any]]] = {}
    for link in links:
        src = link.get("source")
        dst = link.get("target")
        if not src or not dst:
            continue
        edge = {
            "target": dst,
            "weight": float(link.get("weight") or 1),
            "confidence_score": float(link.get("confidence_score") or 0),
            "edge_type": str(link.get("relation") or link.get("type") or "unknown"),
        }
        adjacency.setdefault(src, []).append(edge)
        adjacency.setdefault(dst, []).append(
            {
                "target": src,
                "weight": edge["weight"],
                "confidence_score": edge["confidence_score"],
                "edge_type": edge["edge_type"],
            }
        )
    _GRAPH_CACHE = {"nodes": nodes, "id_map": id_map, "adjacency": adjacency}
    _GRAPH_CACHE_MTIME_NS = stat.st_mtime_ns
    return _GRAPH_CACHE


def _community_label(node: Dict[str, Any]) -> str:
    raw = node.get("community")
    if isinstance(raw, int) and 0 <= raw < len(_COMMUNITY_LABELS):
        return _COMMUNITY_LABELS[raw]
    if raw is None:
        return "unknown"
    return str(raw or "unknown")


def _query_graph_seams(query: str, limit: int = 5) -> Dict[str, Any]:
    graph = _load_graph_index()
    if not graph:
        return {"available": False, "reason": "graph_unavailable", "matches": []}

    tokens = _tokenize_query(query)
    if not tokens:
        return {"available": True, "tokens": [], "matches": []}

    matches = []
    for node in graph["nodes"]:
        file_type = str(node.get("file_type") or "file").strip().lower()
        if file_type not in {"file", "directory", "root", ""}:
            continue
        text = " ".join(
            [
                str(node.get("label") or ""),
                _node_source_file(node),
                str(node.get("id") or ""),
                str(node.get("dir") or ""),
                str(node.get("file") or ""),
            ]
        ).lower()
        score = sum(
            3 if token in _node_source_file(node).lower() else 1
            for token in tokens
            if token in text
        )
        if score <= 0:
            continue
        neighbors = []
        for edge in sorted(
            graph["adjacency"].get(node.get("id"), []),
            key=lambda item: item["weight"],
            reverse=True,
        )[:3]:
            neighbor = graph["id_map"].get(edge["target"], {})
            neighbors.append(
                {
                    "source_file": _node_source_file(neighbor) or edge["target"],
                    "label": neighbor.get("label") or edge["target"],
                    "community": _community_label(neighbor),
                    "weight": edge["weight"],
                    "edge_type": edge.get("edge_type", "unknown"),
                }
            )
        matches.append(
            {
                "score": score,
                "node_id": node.get("id"),
                "source_file": _node_source_file(node) or node.get("id"),
                "label": node.get("label") or node.get("file") or node.get("id"),
                "community": _community_label(node),
                "neighbors": neighbors,
            }
        )

    matches.sort(key=lambda item: (item["score"], len(item.get("neighbors", []))), reverse=True)
    return {"available": True, "tokens": tokens, "matches": matches[:limit]}


def _resolve_graph_node(*, node_id: Optional[str] = None, source_file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    graph = _load_graph_index()
    if not graph:
        return None

    folded_node_id = str(node_id or "").strip()
    if folded_node_id:
        return graph["id_map"].get(folded_node_id)

    folded_source_file = str(source_file or "").strip().replace("\\", "/")
    if not folded_source_file:
        return None

    for node in graph["nodes"]:
        if _node_source_file(node).strip().replace("\\", "/") == folded_source_file:
            return node
    return None


def _canonical_agents_palace_path() -> Optional[Path]:
    candidates = [
        os.environ.get("MEMPALACE_AGENTS_PALACE_PATH"),
        str(_AGENTS_PALACE),
        os.environ.get("MEMPALACE_PALACE_PATH"),
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve()
        if candidate.exists():
            return candidate
    return None


def _normalize_paths(value: Optional[List[str] | str]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value
    normalized: List[str] = []
    seen: set[str] = set()
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


# --------------------------------------------------------------------------
# Legacy async API — thin wrappers over FeedCore sync functions
# --------------------------------------------------------------------------

async def publish_cue(
    agent_id: str,
    post_type: str = "cue",
    topic: str = "general",
    title: str = "",
    body: str = "",
    intent: Optional[str] = None,
    content: Optional[str] = None,
    parent_id: Optional[str] = None,
    artifacts: Optional[List[str]] = None,
    evidence: Optional[List[str] | str] = None,
    session_id: Optional[str] = None,
    biological_context: Optional[str] = None,
    target_agents: Optional[str] = None,
    graph_updated: bool = False,
    memory_updated: bool = False,
    files_touched: Optional[str] = None,
    context_questions_answered: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Publish a post to the agent feed. Returns the post dict.

    Legacy schema: ``title``/``body`` → FeedCore canonical: ``intent``/``content``.
    """
    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    # Map legacy schema → canonical
    intent = str(intent or title or "")
    content = str(content or body or "")

    normalized_artifacts = _normalize_paths(list(artifacts or []))
    normalized_artifacts.extend(
        [item for item in _normalize_paths(evidence) if item.casefold() not in {path.casefold() for path in normalized_artifacts}]
    )

    resolved_metadata = {
        "biological_context": biological_context,
        "target_agents": target_agents,
        "graph_updated": bool(graph_updated),
        "memory_updated": bool(memory_updated),
        "files_touched": files_touched,
        "context_questions_answered": context_questions_answered,
    }
    if metadata:
        resolved_metadata.update(metadata)

    post = {
        "post_type": post_type,
        "agent_id": str(agent_id or "unknown"),
        "topic": topic,
        "intent": intent[:500],
        "content": content[:20000],
        "title": intent[:500],
        "body": content[:20000],
        "parent_id": parent_id,
        "artifacts": normalized_artifacts,
        "evidence": normalized_artifacts,
        "session_id": session_id,
        "metadata": resolved_metadata,
    }

    resolved_idempotency_key = str(
        idempotency_key or resolved_metadata.get("idempotency_key") or fc.compute_idempotency_key(post)
    ).strip()
    post["idempotency_key"] = resolved_idempotency_key
    post["metadata"] = dict(post["metadata"])
    post["metadata"].setdefault("idempotency_key", resolved_idempotency_key)

    post_id = fc.append_post(post, cancel_event=cancel_event)
    stored_post = fc.get_post(post_id) or post

    # Return in legacy shape (with both old and new field names)
    return {
        "id": post_id,
        "ts": stored_post.get("timestamp") or stored_post.get("ts"),
        "timestamp": stored_post.get("timestamp") or stored_post.get("ts"),
        "agent_id": stored_post.get("agent_id"),
        "post_type": stored_post.get("post_type"),
        "topic": stored_post.get("topic"),
        "title": stored_post.get("title") or stored_post.get("intent"),
        "body": stored_post.get("body") or stored_post.get("content"),
        "intent": stored_post.get("intent"),
        "content": stored_post.get("content"),
        "parent_id": parent_id,
        "artifacts": stored_post.get("artifacts") or stored_post.get("evidence", []),
        "evidence": stored_post.get("evidence") or stored_post.get("artifacts", []),
        "session_id": stored_post.get("session_id"),
        "metadata": stored_post.get("metadata", {}),
        "idempotency_key": stored_post.get("idempotency_key") or resolved_idempotency_key,
    }


async def scroll_agent_feed(
    limit: int = 50,
    topic: Optional[str] = None,
    agent_id: Optional[str] = None,
    post_type: Optional[str] = None,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read recent posts with optional filters. Most recent last.

    Returns posts in legacy shape (``ts``/``title``/``body``) with canonical
    fields (``timestamp``/``intent``/``content``) also present.
    """
    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    posts = fc.read_posts(
        limit=limit,
        topic=topic,
        agent_id=agent_id,
        post_type=post_type,
        since=since,
    )

    # Map to legacy shape
    result = []
    for p in posts:
        result.append({
            "id": p.get("id"),
            "ts": p.get("timestamp") or p.get("ts"),
            "timestamp": p.get("timestamp") or p.get("ts"),
            "agent_id": p.get("agent_id"),
            "post_type": p.get("post_type"),
            "topic": p.get("topic"),
            "title": p.get("title") or p.get("intent", ""),
            "body": p.get("body") or p.get("content", ""),
            "intent": p.get("intent") or p.get("title", ""),
            "content": p.get("content") or p.get("body", ""),
            "parent_id": p.get("parent_id"),
            "artifacts": p.get("artifacts") or p.get("evidence", []),
            "evidence": p.get("evidence") or p.get("artifacts", []),
            "session_id": p.get("session_id"),
            "metadata": p.get("metadata", {}),
            "idempotency_key": p.get("idempotency_key") or p.get("metadata", {}).get("idempotency_key"),
            # Session lifecycle fields (pass-through)
            "session_phase": p.get("session_phase"),
            "graph_updated": p.get("graph_updated", False),
            "memory_updated": p.get("memory_updated", False),
            "files_touched": p.get("files_touched", []),
            "context_questions": p.get("context_questions", []),
            "context_questions_answered": p.get("context_questions_answered", []),
            "current_situation": p.get("current_situation"),
            "next_actions": p.get("next_actions", []),
            "progress_count": p.get("progress_count", 0),
        })

    return result


async def open_session_signature(
    agent_id: str,
    mission: str = "",
    task_description: Optional[str] = None,
    context_questions: Optional[str] = None,
    files_under_review: Optional[str] = None,
    current_situation: Optional[str] = None,
    topic: str = "orchestration",
    metadata: Optional[Dict[str, Any]] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Open a coordination session. Emits a ``session_open`` post.

    Legacy: ``mission`` → FeedCore: ``task_description``.
    """
    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    result = fc.open_session(
        agent_id=agent_id,
        task_description=(task_description or mission),
        context_questions=(context_questions or task_description or mission),
        files_under_review=files_under_review,
        current_situation=current_situation,
        cancel_event=cancel_event,
    )

    # Map back to legacy shape
    if "error" in result:
        return result

    return {
        "session_id": result["session_id"],
        "post": {
            "id": result["post_id"],
            "ts": result.get("timestamp"),
            "agent_id": agent_id,
            "post_type": "session_open",
            "topic": topic,
            "title": f"session_open :: {mission}"[:500],
            "body": mission,
            "metadata": metadata or {},
        },
    }


async def update_session_progress(
    session_id: str,
    milestone: str = "",
    progress_notes: Optional[str] = None,
    current_situation: Optional[str] = None,
    next_actions: Optional[str] = None,
    files_touched_so_far: Optional[str] = None,
    evidence: Optional[List[str]] = None,
    agent_id: str = "unknown",
    topic: str = "orchestration",
    metadata: Optional[Dict[str, Any]] = None,
    graph_updated: bool = False,
    memory_updated: bool = False,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Append a progress checkpoint to an open session.

    Legacy: ``milestone`` → FeedCore: ``progress_notes``.
    """
    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    result = fc.update_progress(
        session_id=session_id,
        agent_id=agent_id,
        progress_notes=(progress_notes or milestone),
        current_situation=(current_situation or progress_notes or milestone),
        next_actions=next_actions,
        files_touched_so_far=(files_touched_so_far or ",".join(evidence) if evidence else None),
        graph_updated=graph_updated,
        memory_updated=memory_updated,
        cancel_event=cancel_event,
    )

    if "error" in result:
        return result

    return {
        "id": result["post_id"],
        "ts": result.get("timestamp"),
        "agent_id": agent_id,
        "post_type": "session_progress",
        "topic": topic,
        "title": f"progress :: {(progress_notes or milestone)}"[:500],
        "body": (progress_notes or milestone),
        "parent_id": session_id,
        "artifacts": list(evidence or []),
        "session_id": session_id,
        "metadata": metadata or {},
    }


async def feed_stats(topic: Optional[str] = None) -> Dict[str, Any]:
    """Summary counters over the feed. Uses the cached index when present."""
    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    result = fc.get_stats(topic=topic)

    # Map FeedCore keys (per_topic, per_agent, per_type) to legacy keys
    if topic:
        return {
            "topic": topic,
            "count": result.get("count", 0),
            "total": result.get("total", 0),
        }

    return {
        "total": result.get("total", 0),
        "per_topic": result.get("per_topic", {}),
        "per_agent": result.get("per_agent", {}),
        "per_type": result.get("per_type", {}),
        "last_updated": result.get("last_updated"),
    }


async def feed_thread(root_id: str = "", post_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return a root post plus all its descendants (breadth-first)."""
    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    resolved_root_id = str(post_id or root_id or "").strip()
    result = fc.get_thread(root_id=resolved_root_id)

    if "error" in result:
        return []

    thread = [result["root"]] + result.get("comments", [])

    # Map to legacy shape
    legacy_thread = []
    for p in thread:
        legacy_thread.append({
            "id": p.get("id"),
            "ts": p.get("timestamp") or p.get("ts"),
            "agent_id": p.get("agent_id"),
            "post_type": p.get("post_type"),
            "topic": p.get("topic"),
            "title": p.get("title") or p.get("intent", ""),
            "body": p.get("body") or p.get("content", ""),
            "parent_id": p.get("parent_id"),
            "artifacts": p.get("artifacts") or p.get("evidence", []),
            "session_id": p.get("session_id"),
            "metadata": p.get("metadata", {}),
            "idempotency_key": p.get("idempotency_key") or p.get("metadata", {}).get("idempotency_key"),
        })

    return legacy_thread


async def federated_retrieve(
    query: str,
    limit: int = 5,
    topic: Optional[str] = None,
    agent_id: Optional[str] = None,
    post_type: Optional[str] = None,
    since: Optional[str] = None,
    wing: Optional[str] = None,
    room: Optional[str] = None,
    entity: Optional[str] = None,
    direction: str = "both",
) -> Dict[str, Any]:
    """Federated retrieval across live feed, durable memory, KG, and graph seams.

    This is the canonical shared implementation for both the dedicated
    `mica-agent-feed` server and the remote `/mcp` surface.
    """
    folded_query = str(query or "").strip()
    if not folded_query:
        return {"error": "query is required"}

    fc = _fc()
    if fc is None:
        raise RuntimeError(f"FeedCore unavailable: {'; '.join(_import_errors)}")

    limit = max(1, min(int(limit or 5), 50))
    feed_result = fc.search_posts(
        query=folded_query,
        limit=limit,
        topic=topic,
        agent_id=agent_id,
        post_type=post_type,
        since=since,
    )
    graph_result = _query_graph_seams(folded_query, limit=limit)
    palace_path = _canonical_agents_palace_path()

    result: Dict[str, Any] = {
        "query": folded_query,
        "retrieval_goal": "live coordination + durable semantic memory + curated KG + structural graph seams",
        "sources_used": {
            "feed": True,
            "memory": False,
            "knowledge_graph": False,
            "graph": bool(graph_result.get("available")),
        },
        "feed": feed_result,
        "memory": {
            "available": False,
            "reason": "agents_palace_unavailable" if palace_path is None else "search_not_run",
        },
        "knowledge_graph": {
            "available": False,
            "reason": "entity_not_provided",
        },
        "graph": graph_result,
        "freshness": {
            "feed_total": fc.load_index().get("total", 0),
            "durable_palace_path": str(palace_path) if palace_path is not None else None,
        },
        "contradictions": [],
    }

    if palace_path is not None:
        try:
            from mempalace.searcher import search_memories

            memory_result = search_memories(
                folded_query,
                palace_path=str(palace_path),
                wing=wing,
                room=room,
                n_results=limit,
            )
            memory_available = "error" not in memory_result
            result["memory"] = {
                "available": memory_available,
                "palace_path": str(palace_path),
                "result": memory_result,
            }
            result["sources_used"]["memory"] = memory_available
        except Exception as exc:
            result["memory"] = {
                "available": False,
                "palace_path": str(palace_path),
                "error": str(exc),
            }

        kg_entity = str(entity or "").strip()
        if not kg_entity and len(folded_query) <= 80 and len(folded_query.split()) <= 6:
            kg_entity = folded_query
        if kg_entity:
            try:
                from mempalace.knowledge_graph import KnowledgeGraph

                kg = KnowledgeGraph(db_path=str(palace_path / "knowledge_graph.sqlite3"))
                facts = kg.query_entity(kg_entity, direction=direction)
                result["knowledge_graph"] = {
                    "available": True,
                    "entity": kg_entity,
                    "db_path": str(palace_path / "knowledge_graph.sqlite3"),
                    "result": {"entity": kg_entity, "facts": facts, "count": len(facts)},
                }
                result["sources_used"]["knowledge_graph"] = True
            except Exception as exc:
                result["knowledge_graph"] = {
                    "available": False,
                    "entity": kg_entity,
                    "error": str(exc),
                }

    if result["sources_used"]["graph"] and not result["graph"].get("matches"):
        result["contradictions"].append("graph_available_but_no_structural_match")
    if palace_path is None:
        result["contradictions"].append("durable_memory_authority_unavailable")

    return result


async def search_architecture_graph(query: str, limit: int = 5) -> Dict[str, Any]:
    folded_query = str(query or "").strip()
    if not folded_query:
        return {"error": "query is required"}
    return _query_graph_seams(folded_query, limit=max(1, min(int(limit or 5), 50)))


async def inspect_architecture_graph_node(
    node_id: Optional[str] = None,
    source_file: Optional[str] = None,
    neighbor_limit: int = 10,
) -> Dict[str, Any]:
    graph = _load_graph_index()
    if not graph:
        return {"available": False, "reason": "graph_unavailable"}

    node = _resolve_graph_node(node_id=node_id, source_file=source_file)
    if node is None:
        return {
            "available": True,
            "error": "graph node not found",
            "requested": {
                "node_id": str(node_id or "").strip() or None,
                "source_file": str(source_file or "").strip() or None,
            },
        }

    limit = max(1, min(int(neighbor_limit or 10), 50))
    neighbors = []
    for edge in sorted(graph["adjacency"].get(node.get("id"), []), key=lambda item: item["weight"], reverse=True)[:limit]:
        neighbor = graph["id_map"].get(edge["target"], {})
        neighbors.append(
            {
                "node_id": neighbor.get("id") or edge["target"],
                "source_file": _node_source_file(neighbor) or edge["target"],
                "label": neighbor.get("label") or neighbor.get("file") or edge["target"],
                "community": _community_label(neighbor),
                "weight": edge["weight"],
                "confidence_score": edge.get("confidence_score", 0.0),
                "edge_type": edge.get("edge_type", "unknown"),
            }
        )

    return {
        "available": True,
        "node": {
            "node_id": node.get("id"),
            "source_file": _node_source_file(node),
            "label": node.get("label") or node.get("file"),
            "file_type": node.get("file_type") or "file",
            "community": _community_label(node),
        },
        "neighbors": neighbors,
    }


# --------------------------------------------------------------------------
# Convenience sync smoke (used by tests)
# --------------------------------------------------------------------------

def _smoke_roundtrip() -> Dict[str, Any]:
    """Sync helper for 6-gate G3 smoke. Returns a tiny status dict."""
    async def _run() -> Dict[str, Any]:
        session = await open_session_signature(
            agent_id="smoke",
            mission="agent_feed smoke test",
            topic="orchestration",
        )
        sid = session["session_id"]
        await update_session_progress(
            session_id=sid,
            milestone="halfway",
            evidence=["step1"],
            agent_id="smoke",
        )
        await publish_cue(
            agent_id="smoke",
            post_type="decision",
            topic="orchestration",
            title="smoke decision",
            body="ok",
            parent_id=sid,
            session_id=sid,
        )
        thread = await feed_thread(sid)
        stats = await feed_stats()
        return {
            "session_id": sid,
            "thread_size": len(thread),
            "stats_total": int(stats.get("total", 0)),
        }

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover — manual smoke
    import json
    print(json.dumps(_smoke_roundtrip(), indent=2))
