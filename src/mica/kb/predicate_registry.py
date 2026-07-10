"""Shared predicate and edge-kind registry for KB and GraphRAG.

Doctrine anchor:
- KB and GraphRAG share one predicate/edge-kind authority.
- External mapping reuses BiolinkSchemaAuthority; no parallel ontology.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class PredicateRegistryEntry:
    input_key: str
    edge_kind: str
    predicate_id: str
    registry_version: str
    biolink_predicate_curie: str | None
    registry_status: str

    @property
    def confidence_model_ref(self) -> str:
        return f"edge_confidence://{self.registry_version}/{self.edge_kind}"


class PredicateRegistry:
    """Single shared registry surface for KB and GraphRAG."""

    def __init__(self, *, registry_version: str = "v1") -> None:
        from mica.memory.dlm.biolink_schema_authority import (
            get_default_biolink_schema_authority,
        )

        self._registry_version = registry_version
        self._authority = get_default_biolink_schema_authority()

    @property
    def registry_version(self) -> str:
        return self._registry_version

    def resolve(self, predicate_or_edge_kind: str | None) -> PredicateRegistryEntry:
        normalized_input = str(predicate_or_edge_kind or "related_to").strip().lower() or "related_to"
        edge_kind = self._resolve_edge_kind(normalized_input)
        authority_receipt = self._authority.resolve_predicate(normalized_input)
        predicate_id = str(authority_receipt.get("matched_key") or edge_kind).strip().lower() or edge_kind
        predicate_curie = authority_receipt.get("predicate_curie")
        registry_status = "registered" if predicate_curie else "fallback"
        if predicate_curie is None and predicate_id == "related_to":
            predicate_curie = "biolink:related_to"
        return PredicateRegistryEntry(
            input_key=normalized_input,
            edge_kind=edge_kind,
            predicate_id=predicate_id,
            registry_version=self._registry_version,
            biolink_predicate_curie=predicate_curie,
            registry_status=registry_status,
        )

    @staticmethod
    def _resolve_edge_kind(normalized_input: str) -> str:
        from mica.memory.dlm.biolink_schema_authority import (
            _PREDICATE_EXPORT_CANDIDATES,
        )

        if normalized_input in _PREDICATE_EXPORT_CANDIDATES:
            return normalized_input
        for edge_kind, export_candidates in _PREDICATE_EXPORT_CANDIDATES.items():
            if normalized_input in export_candidates:
                return edge_kind
        return normalized_input


@lru_cache(maxsize=1)
def get_default_predicate_registry() -> PredicateRegistry:
    return PredicateRegistry()
