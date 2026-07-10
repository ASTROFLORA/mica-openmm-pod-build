"""Agent memory persistence for MICA sub-agents.

Addresses Q6/Q7 from the 42-question audit: sub-agent context dies forever
after fire-and-forget execution. This module stores sub-agent synthesis
outputs so they can be recalled on subsequent calls within the same session
or across sessions.

Architecture:
- Per-session JSON file: {session_id}/{agent_name}.jsonl
- Each entry: {timestamp, query_fingerprint, query_summary, synthesis, citations, gaps}
- On subsequent calls: inject relevant prior context from same agent
- LRU eviction: keep last 20 entries per agent per session
- Cross-session: optional global agent memory for persistent experts

Usage in driver:
    memory = AgentMemory(session_id="abc123")
    prior = memory.recall("bibliotecario", query="p53 IDR dynamics")
    # ... spawn agent with prior context injected ...
    memory.store("bibliotecario", query="p53 IDR dynamics", synthesis="...", citations=[...])
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_ENTRIES_PER_AGENT = 20
MAX_SYNTHESIS_CHARS = 3000
MEMORY_BASE_DIR = Path.home() / ".mica" / "agent_memory"


@dataclass
class AgentMemoryEntry:
    """Single stored sub-agent output."""
    timestamp: float
    agent_name: str
    query_fingerprint: str
    query_summary: str
    synthesis: str
    citations: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[Dict[str, Any]] = field(default_factory=list)
    review_issues: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "agent_name": self.agent_name,
            "query_fingerprint": self.query_fingerprint,
            "query_summary": self.query_summary,
            "synthesis": self.synthesis[:MAX_SYNTHESIS_CHARS],
            "citations": self.citations[:10],
            "gaps": self.gaps[:5],
            "review_issues": self.review_issues[:10],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentMemoryEntry":
        return cls(
            timestamp=d.get("timestamp", 0.0),
            agent_name=d.get("agent_name", "unknown"),
            query_fingerprint=d.get("query_fingerprint", ""),
            query_summary=d.get("query_summary", ""),
            synthesis=d.get("synthesis", ""),
            citations=d.get("citations", []),
            gaps=d.get("gaps", []),
            review_issues=d.get("review_issues", []),
            metadata=d.get("metadata", {}),
        )

    def relevance_snippet(self, max_chars: int = 500) -> str:
        """Generate a compact snippet for injection into a new agent run."""
        parts = [f"[Prior analysis — {self.query_summary}]"]
        if self.synthesis:
            parts.append(f"Synthesis: {self.synthesis[:max_chars]}")
        if self.citations:
            cited = ", ".join(c.get("finding", "")[:60] for c in self.citations[:3])
            parts.append(f"Key citations: {cited}")
        if self.gaps:
            gap_list = ", ".join(g.get("description", "")[:60] for g in self.gaps[:2])
            parts.append(f"Known gaps: {gap_list}")
        return "\n".join(parts)


class AgentMemory:
    """Persistent memory store for MICA sub-agents.

    Provides session-scoped and cross-session recall for bibliotecario,
    expert, and reviewer agents.
    """

    def __init__(
        self,
        session_id: str = "default",
        base_dir: Optional[Path] = None,
    ):
        self._session_id = session_id
        self._base_dir = base_dir or MEMORY_BASE_DIR
        self._session_dir = self._base_dir / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, List[AgentMemoryEntry]] = {}

    @staticmethod
    def _fingerprint(query: str) -> str:
        """SHA256 fingerprint of normalized query for dedup/matching."""
        normalized = " ".join(query.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    @staticmethod
    def _query_overlap(a: str, b: str) -> float:
        """Word-overlap similarity between two queries (0-1)."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / max(len(wa), len(wb))

    def _agent_file(self, agent_name: str) -> Path:
        return self._session_dir / f"{agent_name}.jsonl"

    def _load_entries(self, agent_name: str) -> List[AgentMemoryEntry]:
        """Load all stored entries for an agent (cached in memory)."""
        if agent_name in self._cache:
            return self._cache[agent_name]
        path = self._agent_file(agent_name)
        entries = []
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").strip().split("\n"):
                    if line.strip():
                        entries.append(AgentMemoryEntry.from_dict(json.loads(line)))
            except Exception as e:
                logger.warning("Failed to load agent memory for %s: %s", agent_name, e)
        self._cache[agent_name] = entries
        return entries

    def _rewrite_agent_file(self, agent_name: str, entries: List[AgentMemoryEntry]) -> None:
        path = self._agent_file(agent_name)
        with path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False, default=str) + "\n")

    def store(
        self,
        agent_name: str,
        query: str,
        synthesis: str,
        citations: Optional[List[Dict]] = None,
        gaps: Optional[List[Dict]] = None,
        review_issues: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
    ) -> AgentMemoryEntry:
        """Store a sub-agent's output for future recall.

        Appends to JSONL file and in-memory cache. Evicts oldest entries
        if exceeding MAX_ENTRIES_PER_AGENT.
        """
        entry = AgentMemoryEntry(
            timestamp=time.time(),
            agent_name=agent_name,
            query_fingerprint=self._fingerprint(query),
            query_summary=query[:200],
            synthesis=synthesis,
            citations=citations or [],
            gaps=gaps or [],
            review_issues=review_issues or [],
            metadata=metadata or {},
        )
        entries = self._load_entries(agent_name)

        # Dedup: skip if identical fingerprint already stored
        if any(e.query_fingerprint == entry.query_fingerprint for e in entries):
            logger.debug("Skipping duplicate agent memory entry for %s: %s", agent_name, query[:50])
            for existing in entries:
                if existing.query_fingerprint == entry.query_fingerprint:
                    return existing
            return entry

        entries.append(entry)

        # Evict oldest if over limit
        if len(entries) > MAX_ENTRIES_PER_AGENT:
            entries = entries[-MAX_ENTRIES_PER_AGENT:]
        self._cache[agent_name] = entries

        # Write to disk
        try:
            if len(entries) == 1 or entries[-1].query_fingerprint == entry.query_fingerprint:
                self._rewrite_agent_file(agent_name, entries)
            else:
                self._rewrite_agent_file(agent_name, entries)
        except Exception as e:
            logger.warning("Failed to persist agent memory for %s: %s", agent_name, e)

        return entry

    def recall(
        self,
        agent_name: str,
        query: str,
        top_k: int = 3,
        min_similarity: float = 0.25,
    ) -> List[AgentMemoryEntry]:
        """Recall prior agent outputs relevant to a new query.

        Returns up to top_k entries sorted by query similarity (descending).
        Only entries above min_similarity threshold are returned.
        """
        entries = self._load_entries(agent_name)
        if not entries:
            return []

        scored = [
            (e, self._query_overlap(query, e.query_summary))
            for e in entries
        ]
        # Sort by similarity descending, then by recency (timestamp) descending
        scored.sort(key=lambda x: (x[1], x[0].timestamp), reverse=True)

        return [e for e, sim in scored[:top_k] if sim >= min_similarity]

    def recall_context_injection(
        self,
        agent_name: str,
        query: str,
        max_chars: int = 1500,
    ) -> Optional[str]:
        """Generate a context injection string from prior agent memory.

        Returns None if no relevant prior context exists.
        Returns a formatted string suitable for prepending to agent messages.
        """
        relevant = self.recall(agent_name, query)
        if not relevant:
            return None

        parts = ["[PRIOR CONTEXT FROM PREVIOUS ANALYSES]"]
        chars_used = len(parts[0])
        for entry in relevant:
            snippet = entry.relevance_snippet(max_chars=400)
            if chars_used + len(snippet) > max_chars:
                break
            parts.append(snippet)
            chars_used += len(snippet)
        if len(parts) <= 1:
            return None

        parts.append("[END PRIOR CONTEXT — build upon this, do not repeat it]")
        return "\n\n".join(parts)

    def list_agents(self) -> List[str]:
        """List all agents with stored memory in this session."""
        return [
            p.stem for p in self._session_dir.glob("*.jsonl")
        ]

    def get_session_summary(self) -> Dict[str, Any]:
        """Summary of all agent memory in this session."""
        return {
            "session_id": self._session_id,
            "agents": {
                name: len(self._load_entries(name))
                for name in self.list_agents()
            },
        }
