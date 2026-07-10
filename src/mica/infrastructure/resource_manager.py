"""S2.6 — Resource Lifecycle Manager.

Tracks external jobs, MCP sessions, temporary artifacts, and other
resources created during a MICA run.  Provides:

- Registration with ``run_id`` scoping
- Status tracking (active / released / failed)
- Cleanup hooks for graceful teardown
- Introspection for audit / observability

This is a **synchronous, in-process** manager.  Distributed resource
tracking is left to future infrastructure (Redis / cloud orchestrator).

Usage::

    from mica.infrastructure.resource_manager import ResourceManager, ResourceEntry

    mgr = ResourceManager(run_id="run-abc")
    rid = mgr.register("mcp_session", "alphafold-123")
    mgr.release(rid)
    print(mgr.summary())
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence


# ── Types ──────────────────────────────────────────────────────────────
ResourceStatus = Literal["active", "released", "failed"]
ResourceKind = Literal[
    "mcp_session",
    "llm_call",
    "specialist_spawn",
    "external_job",
    "temp_artifact",
    "other",
]


# ── ResourceEntry ──────────────────────────────────────────────────────
@dataclass
class ResourceEntry:
    """Mutable record for a single tracked resource."""

    resource_id: str
    kind: str  # ResourceKind (kept as str for extensibility)
    handle: str  # opaque identifier (session id, file path, job id, …)
    run_id: str = ""
    status: ResourceStatus = "active"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    released_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "handle": self.handle,
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "released_at": self.released_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ResourceEntry":
        return cls(
            resource_id=d.get("resource_id", str(uuid.uuid4())),
            kind=d.get("kind", "other"),
            handle=d.get("handle", ""),
            run_id=d.get("run_id", ""),
            status=d.get("status", "active"),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            released_at=d.get("released_at"),
            metadata=d.get("metadata", {}),
        )


# ── ResourceManager ───────────────────────────────────────────────────
class ResourceManager:
    """In-process manager for tracking lifecycle of external resources.

    Thread-safe via a single lock.  All query methods return snapshots.
    """

    def __init__(self, run_id: str = "") -> None:
        self._run_id = run_id
        self._entries: Dict[str, ResourceEntry] = {}
        self._lock = threading.Lock()
        self._cleanup_hooks: Dict[str, Callable[[ResourceEntry], None]] = {}

    # ── registration ───────────────────────────────────────────────
    def register(
        self,
        kind: str,
        handle: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        cleanup_hook: Optional[Callable[["ResourceEntry"], None]] = None,
    ) -> str:
        """Register a new resource.  Returns its resource_id."""
        rid = str(uuid.uuid4())
        entry = ResourceEntry(
            resource_id=rid,
            kind=kind,
            handle=handle,
            run_id=self._run_id,
            metadata=metadata or {},
        )
        with self._lock:
            self._entries[rid] = entry
            if cleanup_hook is not None:
                self._cleanup_hooks[rid] = cleanup_hook
        return rid

    # ── status transitions ─────────────────────────────────────────
    def release(self, resource_id: str) -> bool:
        """Mark resource as released.  Returns False if not found."""
        with self._lock:
            entry = self._entries.get(resource_id)
            if entry is None:
                return False
            entry.status = "released"
            entry.released_at = datetime.now(timezone.utc).isoformat()
            return True

    def mark_failed(self, resource_id: str) -> bool:
        """Mark resource as failed.  Returns False if not found."""
        with self._lock:
            entry = self._entries.get(resource_id)
            if entry is None:
                return False
            entry.status = "failed"
            entry.released_at = datetime.now(timezone.utc).isoformat()
            return True

    # ── cleanup ────────────────────────────────────────────────────
    def cleanup_all(self) -> List[str]:
        """Run cleanup hooks for all active resources.

        Returns list of resource_ids that were cleaned up.
        Hooks that raise are caught and the resource is marked failed.
        """
        cleaned: List[str] = []
        with self._lock:
            active = [
                (rid, entry)
                for rid, entry in self._entries.items()
                if entry.status == "active"
            ]
        for rid, entry in active:
            hook = self._cleanup_hooks.get(rid)
            if hook is not None:
                try:
                    hook(entry)
                except Exception:
                    self.mark_failed(rid)
                    continue
            self.release(rid)
            cleaned.append(rid)
        return cleaned

    # ── queries ────────────────────────────────────────────────────
    def get(self, resource_id: str) -> Optional[ResourceEntry]:
        with self._lock:
            return self._entries.get(resource_id)

    def list_active(self) -> List[ResourceEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.status == "active"]

    def list_all(self) -> List[ResourceEntry]:
        with self._lock:
            return list(self._entries.values())

    def list_by_kind(self, kind: str) -> List[ResourceEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.kind == kind]

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.status == "active")

    @property
    def total_count(self) -> int:
        return len(self._entries)

    # ── serialisation ──────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "run_id": self._run_id,
                "entries": [e.to_dict() for e in self._entries.values()],
            }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ResourceManager":
        mgr = cls(run_id=d.get("run_id", ""))
        for ed in d.get("entries", []):
            entry = ResourceEntry.from_dict(ed)
            mgr._entries[entry.resource_id] = entry
        return mgr

    # ── human-readable ─────────────────────────────────────────────
    def summary(self) -> str:
        with self._lock:
            active = sum(1 for e in self._entries.values() if e.status == "active")
            released = sum(1 for e in self._entries.values() if e.status == "released")
            failed = sum(1 for e in self._entries.values() if e.status == "failed")
        return (
            f"ResourceManager[{self._run_id[:8] if self._run_id else 'no-run'}] "
            f"total={self.total_count} active={active} released={released} failed={failed}"
        )
