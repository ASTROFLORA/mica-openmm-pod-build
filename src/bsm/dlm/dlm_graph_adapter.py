"""
DLM Graph Adapter (GAP-3b)
============================

Converts DLM temporal knowledge quintuples into ``atom_graph_edges`` rows
suitable for insertion into the TimescaleDB GraphRAG schema.

A DLM quintuple has the form:
    (subject, predicate, object, confidence, pmid)

This adapter:
1. Resolves both ``subject`` and ``object`` to ``budo_id`` via CEA identity
2. Maps the ``predicate`` to a canonical edge type
3. Returns ``atom_graph_edges``-compatible dicts (or skips with a warning if
   identity resolution fails — NO silent drops)

Contract: §2.3.2 of BSM_INTERAGENT_GAP_CONTRACTS_2026-04-04.md

TimescaleDB target schema (graphrag.atom_graph_edges):
    source_id TEXT, target_id TEXT, edge_type TEXT,
    weight FLOAT, properties JSONB, created_at TIMESTAMPTZ
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Quintuple = Tuple[str, str, str, float, Optional[str]]
"""(subject, predicate, object, confidence, pmid)"""

GraphEdge = Dict[str, Any]
"""Matches atom_graph_edges schema columns."""


# ---------------------------------------------------------------------------
# Predicate → edge_type mapping
# ---------------------------------------------------------------------------

PREDICATE_TO_EDGE_TYPE: Dict[str, str] = {
    # Canonical DLM predicates
    "binds": "BINDS",
    "inhibits": "INHIBITS",
    "activates": "ACTIVATES",
    "phosphorylates": "PHOSPHORYLATES",
    "ubiquitinates": "UBIQUITINATES",
    "acetylates": "ACETYLATES",
    "methylates": "METHYLATES",
    "cleaves": "CLEAVES",
    "regulates": "REGULATES",
    "interacts_with": "INTERACTS_WITH",
    "is_part_of": "MEMBER_OF",
    "instance_of": "INSTANCE_OF",
    "subfamily_of": "SUBFAMILY_OF",
    # Synonyms / LMP variants
    "interact": "INTERACTS_WITH",
    "interaction": "INTERACTS_WITH",
    "bind": "BINDS",
    "activate": "ACTIVATES",
    "inhibit": "INHIBITS",
    "phosphorylate": "PHOSPHORYLATES",
    "regulate": "REGULATES",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DLMGraphAdapter:
    """Converts DLM quintuples to atom_graph_edges rows.

    Args:
        budo_id_lookup: Callable ``(entity_name: str) -> Optional[str]`` that
            resolves an entity name to its canonical ``budo_id``.  Typically
            wraps the CEA resolver.  If ``None``, entity names are used as-is
            with a ``raw:`` prefix.

    Example::

        adapter = DLMGraphAdapter(budo_id_lookup=my_cea_resolver)
        edges = adapter.convert_quintuples(quintuples)
        # Insert `edges` into graphrag.atom_graph_edges
    """

    def __init__(
        self,
        budo_id_lookup: Optional[Any] = None,
    ) -> None:
        self._lookup = budo_id_lookup
        self._unresolved_log: List[str] = []

    def convert_quintuples(
        self,
        quintuples: List[Quintuple],
        skip_unresolvable: bool = True,
    ) -> List[GraphEdge]:
        """Convert a list of quintuples to graph edge dicts.

        Args:
            quintuples: List of ``(subject, predicate, object, confidence, pmid)``
            skip_unresolvable: If True, unresolvable entities are logged but
                skipped (not silently dropped — logged at WARNING level).
                If False, raw entity names are used with ``raw:`` prefix.

        Returns:
            List of dicts compatible with ``atom_graph_edges`` INSERT.
        """
        edges: List[GraphEdge] = []

        for quintuple in quintuples:
            edge = self._convert_one(quintuple, skip_unresolvable)
            if edge is not None:
                edges.append(edge)

        if self._unresolved_log:
            logger.warning(
                "DLMGraphAdapter: %d entities could not be resolved to budo_id:\n  %s",
                len(self._unresolved_log),
                "\n  ".join(self._unresolved_log[:20]),
            )

        return edges

    def get_unresolved_entities(self) -> List[str]:
        """Return list of entity names that failed budo_id resolution."""
        return list(self._unresolved_log)

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _convert_one(
        self,
        quintuple: Quintuple,
        skip_unresolvable: bool,
    ) -> Optional[GraphEdge]:
        subject, predicate, obj, confidence, pmid = quintuple

        # Resolve subject
        source_id = self._resolve_entity(subject)
        if source_id is None:
            msg = f"subject={subject!r}"
            self._unresolved_log.append(msg)
            if skip_unresolvable:
                logger.warning("Skipping quintuple — unresolvable %s", msg)
                return None
            source_id = f"raw:{subject}"

        # Resolve object
        target_id = self._resolve_entity(obj)
        if target_id is None:
            msg = f"object={obj!r}"
            self._unresolved_log.append(msg)
            if skip_unresolvable:
                logger.warning("Skipping quintuple — unresolvable %s", msg)
                return None
            target_id = f"raw:{obj}"

        edge_type = PREDICATE_TO_EDGE_TYPE.get(
            predicate.lower().strip(), predicate.upper().replace(" ", "_")
        )

        properties: Dict[str, Any] = {
            "confidence": confidence,
            "predicate_raw": predicate,
        }
        if pmid:
            properties["pmid"] = pmid

        return {
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "weight": float(confidence),
            "properties": properties,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _resolve_entity(self, entity_name: str) -> Optional[str]:
        """Resolve entity name to budo_id via the provided lookup callable."""
        if not entity_name or not entity_name.strip():
            return None

        # Already a budo_id
        if entity_name.startswith("budo:"):
            return entity_name

        # Delegate to lookup
        if self._lookup is not None:
            try:
                result = self._lookup(entity_name)
                return result  # may be None
            except Exception as exc:
                logger.warning(
                    "budo_id_lookup raised for %r: %s", entity_name, exc
                )
                return None

        # No lookup provided: use raw prefix
        return f"raw:{entity_name}"


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def convert_quintuples_to_edges(
    quintuples: List[Quintuple],
    budo_id_lookup: Optional[Any] = None,
    skip_unresolvable: bool = True,
) -> List[GraphEdge]:
    """Convenience wrapper — creates a one-shot adapter and converts.

    Args:
        quintuples: DLM quintuple list.
        budo_id_lookup: Optional entity → budo_id callable.
        skip_unresolvable: Whether to skip edges with unresolvable entities.

    Returns:
        List of ``atom_graph_edges``-compatible dicts.
    """
    adapter = DLMGraphAdapter(budo_id_lookup=budo_id_lookup)
    return adapter.convert_quintuples(quintuples, skip_unresolvable=skip_unresolvable)
