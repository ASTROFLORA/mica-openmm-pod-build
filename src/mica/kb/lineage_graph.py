"""
KB LineageGraph Projector — K5-7 (KB Slice 3)

Projects MUDO graph into a lineage-restricted subgraph (PROV-O + local terms).
Detects drift between projection and source via source_hash.
Neo4j optional; starts with dict projection. Projections are disposable.

Key objects:
- LineageEdge: single edge in lineage subgraph
- LineageGraph: projected subgraph with drift detection
- LineageProjector: projects source graph → lineage subgraph
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class LineageRelation(str, Enum):
    """PROV-O + local lineage relations."""
    WAS_DERIVED_FROM = "wasDerivedFrom"
    WAS_GENERATED_BY = "wasGeneratedBy"
    WAS_ASSOCIATED_WITH = "wasAssociatedWith"
    USED = "used"
    WAS_INFORMED_BY = "wasInformedBy"
    HAD_MEMBER = "hadMember"
    HAS_INPUT = "hasInput"
    HAS_OUTPUT = "hasOutput"
    WAS_CONTRIBUTED_BY = "wasContributedBy"  # local
    HAS_LICENSE = "hasLicense"  # local
    HAS_INTEGRITY_DIGEST = "hasIntegrityDigest"  # local


@dataclass(frozen=True)
class LineageEdge:
    """A single edge in the lineage subgraph."""
    source_ref: str
    target_ref: str
    relation: LineageRelation
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source_ref,
            "target": self.target_ref,
            "relation": self.relation.value,
            "metadata": self.metadata,
        }


@dataclass
class LineageGraph:
    """Projected lineage subgraph with drift detection."""
    graph_ref: str
    source_hash: str
    edges: List[LineageEdge] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    drift_detected: bool = False

    def add_edge(self, edge: LineageEdge) -> None:
        self.edges.append(edge)

    def nodes(self) -> Set[str]:
        nodes: Set[str] = set()
        for e in self.edges:
            nodes.add(e.source_ref)
            nodes.add(e.target_ref)
        return nodes

    def compute_hash(self) -> str:
        """Compute hash of all edges for drift detection."""
        edge_str = "|".join(
            f"{e.source_ref}:{e.relation.value}:{e.target_ref}"
            for e in sorted(self.edges, key=lambda x: (x.source_ref, x.relation.value))
        )
        return hashlib.sha256(edge_str.encode()).hexdigest()[:16]

    def detect_drift(self) -> bool:
        """Compare current hash with source hash."""
        current = self.compute_hash()
        self.drift_detected = current != self.source_hash
        return self.drift_detected


class LineageProjector:
    """K5-7: Projects source graph → lineage-restricted subgraph."""

    # Relations we project into lineage
    _PROJECTABLE = {
        "wasDerivedFrom", "wasGeneratedBy", "wasAssociatedWith", "used",
        "wasInformedBy", "hadMember", "hasInput", "hasOutput",
        "wasContributedBy", "hasLicense", "hasIntegrityDigest",
        "derived_from", "generated_by", "associated_with", "used",
        "informed_by", "contributed_by", "has_license",
    }

    def __init__(self, graph_ref: str = "lineage://default"):
        self._graph_ref = graph_ref

    def project(
        self,
        edges: List[Dict[str, Any]],
        source_hash: Optional[str] = None,
    ) -> LineageGraph:
        """Project source edges into lineage subgraph."""
        projected = LineageGraph(
            graph_ref=self._graph_ref,
            source_hash=source_hash or "",
        )

        for raw in edges:
            relation_name = raw.get("relation", raw.get("predicate", ""))
            if relation_name not in self._PROJECTABLE:
                continue

            # Map to LineageRelation
            try:
                rel = LineageRelation(relation_name)
            except ValueError:
                # Try mapping from biolink to PROV-O
                mapped = self._map_to_prov_o(relation_name)
                if mapped is None:
                    continue
                rel = mapped

            projected.add_edge(LineageEdge(
                source_ref=raw.get("source", ""),
                target_ref=raw.get("target", ""),
                relation=rel,
                metadata={k: v for k, v in raw.items() if k not in ("source", "target", "relation", "predicate")},
            ))

        projected.source_hash = projected.compute_hash()
        return projected

    def _map_to_prov_o(self, biolink_rel: str) -> Optional[LineageRelation]:
        """Map biolink predicates to PROV-O + local lineage relations."""
        _MAP = {
            "derived_from": LineageRelation.WAS_DERIVED_FROM,
            "generated_by": LineageRelation.WAS_GENERATED_BY,
            "associated_with": LineageRelation.WAS_ASSOCIATED_WITH,
            "informed_by": LineageRelation.WAS_INFORMED_BY,
            "has_license": LineageRelation.HAS_LICENSE,
            "contributed_by": LineageRelation.WAS_CONTRIBUTED_BY,
        }
        return _MAP.get(biolink_rel)
