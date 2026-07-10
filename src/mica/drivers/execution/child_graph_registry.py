"""
ChildGraphRegistry
==================

Phase 1: Local child graph registry for GoG native execution.

Resolves ``child_graph_id`` strings to validated ``ProtocolJSONLDDocument``
objects. Accepts either:
  - a directory of ``*.json`` protocol files (keyed by ``protocol_id`` field)
  - a pre-loaded ``dict[str, ProtocolJSONLDDocument | dict]``

Typed blockers
--------------
ChildGraphNotFound     — child_graph_id has no matching entry
ChildGraphInvalid      — entry fails validate_protocol_jsonld
ChildGraphExecFailed   — bridge-level execution error (re-exported for bridge)

Usage::

    from mica.drivers.execution.child_graph_registry import (
        ChildGraphRegistry, ChildGraphNotFound, ChildGraphInvalid
    )

    # From a directory of .json files:
    registry = ChildGraphRegistry.from_dir(Path("tests/fixtures"))

    # From a pre-loaded map:
    registry = ChildGraphRegistry.from_map({
        "gog-v3-child-a": document_a,
        "gog-v3-child-b": document_b,
    })

    doc = registry.get("gog-v3-child-a")  # ProtocolJSONLDDocument
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from mica_q.protocol_jsonld_contract import ProtocolJSONLDDocument
from mica_q.protocol_jsonld_validator import validate_protocol_jsonld


# ---------------------------------------------------------------------------
# Typed blocker exceptions
# ---------------------------------------------------------------------------

class ChildGraphNotFound(KeyError):
    """Raised when child_graph_id is not registered in the registry."""

    def __init__(self, child_graph_id: str) -> None:
        self.child_graph_id = child_graph_id
        super().__init__(
            f"Child graph not found in registry: {child_graph_id!r}. "
            "Register it via ChildGraphRegistry.from_dir() or from_map()."
        )


class ChildGraphInvalid(ValueError):
    """Raised when a child graph document fails protocol validation."""

    def __init__(self, child_graph_id: str, cause: BaseException) -> None:
        self.child_graph_id = child_graph_id
        self.cause = cause
        super().__init__(
            f"Child graph {child_graph_id!r} failed protocol validation: {cause}"
        )


class ChildGraphExecFailed(RuntimeError):
    """Raised when a child graph execution terminates with status != completed."""

    def __init__(self, child_graph_id: str, child_status: str, message: str) -> None:
        self.child_graph_id = child_graph_id
        self.child_status = child_status
        super().__init__(
            f"Child graph {child_graph_id!r} execution terminated with "
            f"status={child_status!r}: {message}"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ChildGraphRegistry:
    """Registry that maps child_graph_id → validated ProtocolJSONLDDocument.

    Thread-safety: read-only after construction — safe for concurrent async use.
    """

    def __init__(self, _store: Dict[str, ProtocolJSONLDDocument]) -> None:
        self._store = _store

    # ── Construction ─────────────────────────────────────────────────────

    @classmethod
    def from_dir(
        cls,
        directory: Union[str, Path],
        *,
        validate: bool = True,
        glob: str = "*.json",
    ) -> "ChildGraphRegistry":
        """Load all matching JSON files in *directory* as child graph documents.

        Each file is parsed and, if validate=True, run through
        ``validate_protocol_jsonld``.  The registry key is the ``protocol_id``
        field from the document.  Files that lack ``protocol_id`` or fail
        JSON parsing are skipped with a warning printed to stderr.

        Args:
            directory: Directory path containing protocol JSON-LD files.
            validate: Whether to validate each loaded document.
            glob: Glob pattern for matching files (default ``*.json``).
        """
        import sys
        store: Dict[str, ProtocolJSONLDDocument] = {}
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"ChildGraphRegistry.from_dir: not a directory: {root}")

        for path in sorted(root.glob(glob)):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[ChildGraphRegistry] skip {path.name}: JSON parse error: {exc}", file=sys.stderr)
                continue

            protocol_id = raw.get("protocol_id") or raw.get("@id") or ""
            if not protocol_id:
                # Not a protocol document — skip silently
                continue

            if validate:
                try:
                    doc = validate_protocol_jsonld(raw)
                except Exception as exc:
                    print(
                        f"[ChildGraphRegistry] skip {path.name} (id={protocol_id!r}): "
                        f"validation error: {exc}",
                        file=sys.stderr,
                    )
                    continue
            else:
                doc = ProtocolJSONLDDocument.model_validate(raw)

            store[protocol_id] = doc

        return cls(store)

    @classmethod
    def from_map(
        cls,
        mapping: Dict[str, Union[ProtocolJSONLDDocument, Dict[str, Any]]],
        *,
        validate: bool = True,
    ) -> "ChildGraphRegistry":
        """Build a registry from a pre-loaded mapping.

        Args:
            mapping: Dict of child_graph_id → document (Pydantic model or raw dict).
            validate: Whether to validate raw-dict entries.
        """
        store: Dict[str, ProtocolJSONLDDocument] = {}
        for key, value in mapping.items():
            if isinstance(value, ProtocolJSONLDDocument):
                store[key] = value
            elif isinstance(value, dict):
                if validate:
                    try:
                        store[key] = validate_protocol_jsonld(value)
                    except Exception as exc:
                        raise ChildGraphInvalid(key, exc) from exc
                else:
                    store[key] = ProtocolJSONLDDocument.model_validate(value)
            else:
                raise TypeError(
                    f"ChildGraphRegistry.from_map: value for {key!r} must be "
                    f"ProtocolJSONLDDocument or dict, got {type(value).__name__}"
                )
        return cls(store)

    @classmethod
    def empty(cls) -> "ChildGraphRegistry":
        """Return an empty registry (useful as a null-object default)."""
        return cls({})

    # ── Access ────────────────────────────────────────────────────────────

    def get(self, child_graph_id: str) -> ProtocolJSONLDDocument:
        """Return the document for *child_graph_id* or raise ChildGraphNotFound."""
        doc = self._store.get(child_graph_id)
        if doc is None:
            raise ChildGraphNotFound(child_graph_id)
        return doc

    def contains(self, child_graph_id: str) -> bool:
        """Return True if *child_graph_id* is registered."""
        return child_graph_id in self._store

    def ids(self) -> list[str]:
        """Return all registered child_graph_id values."""
        return list(self._store.keys())

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"ChildGraphRegistry({len(self)} entries: {self.ids()!r})"


__all__ = [
    "ChildGraphRegistry",
    "ChildGraphNotFound",
    "ChildGraphInvalid",
    "ChildGraphExecFailed",
]
