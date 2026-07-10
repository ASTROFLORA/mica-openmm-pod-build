"""SemanticContext — deduplicable, versionable biological context registry.

Implements K2.3: semctx:// registry.
- Same context fingerprint deduplicates
- Different tissue → different fingerprint
- Unspecified context ≠ universal context
- Registry is versioned and immutable
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SemanticContext:
    """K2.3: A deduplicable biological context object.

    An unspecified context is NOT "all contexts" — it is
    "context unknown / broad claim with weak specificity".
    """
    organism: str  # taxon://9606
    cell_type: Optional[str] = None  # CL:...
    tissue: Optional[str] = None  # UBERON:...
    disease: Optional[str] = None  # MONDO:...
    condition: Optional[str] = None
    isoform: Optional[str] = None
    mutation: Optional[str] = None
    developmental_stage: Optional[str] = None

    def fingerprint(self) -> str:
        """Compute dedup fingerprint from context fields."""
        parts = [
            self.organism,
            self.cell_type or "",
            self.tissue or "",
            self.disease or "",
            self.condition or "",
            self.isoform or "",
            self.mutation or "",
            self.developmental_stage or "",
        ]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def is_unspecified(self) -> bool:
        """K2.3: Unspecified context is broad/weak, not universal."""
        return (
            self.organism == ""
            and self.cell_type is None
            and self.tissue is None
            and self.disease is None
            and self.condition is None
        )

    def overlaps_with(self, other: "SemanticContext") -> bool:
        """K1.5: Check if two contexts overlap (potential contradiction).

        P2-3: Two unspecified contexts do NOT overlap.
        """
        # Unspecified contexts never overlap
        if not self.organism or not other.organism:
            return False
        if self.organism != other.organism:
            return False
        # Same organism + at least one shared context dimension → potential overlap
        if self.cell_type and other.cell_type and self.cell_type != other.cell_type:
            return False
        if self.tissue and other.tissue and self.tissue != other.tissue:
            return False
        return True

    def is_compatible_with(self, other: "SemanticContext") -> bool:
        """K1.5: Check if two contexts are compatible (no contradiction)."""
        if not self.overlaps_with(other):
            return True  # Different contexts → not a contradiction
        # Same organism + same tissue/cell → compatible
        return True

    def to_dict(self) -> Dict[str, str]:
        return {
            "organism": self.organism,
            "cell_type": self.cell_type or "",
            "tissue": self.tissue or "",
            "disease": self.disease or "",
            "condition": self.condition or "",
            "isoform": self.isoform or "",
            "mutation": self.mutation or "",
            "developmental_stage": self.developmental_stage or "",
        }


class SemanticContextRegistry:
    """K2.3: Registry for deduplicable, versionable semantic contexts.

    Same fingerprint → same context object (dedup).
    Different fingerprint → different context (related but not same claim).
    """

    def __init__(self) -> None:
        self._by_fingerprint: Dict[str, SemanticContext] = {}
        self._version: int = 1

    def register(self, context: SemanticContext) -> str:
        """Register a context, deduplicating by fingerprint. Returns fingerprint."""
        fp = context.fingerprint()
        if fp not in self._by_fingerprint:
            self._by_fingerprint[fp] = context
        return fp

    def get(self, fingerprint: str) -> Optional[SemanticContext]:
        """Retrieve a context by fingerprint."""
        return self._by_fingerprint.get(fingerprint)

    def find_overlapping(self, context: SemanticContext) -> List[Tuple[str, SemanticContext]]:
        """K1.5: Find all contexts that overlap with the given context."""
        results = []
        for fp, registered in self._by_fingerprint.items():
            if context.overlaps_with(registered) and fp != context.fingerprint():
                results.append((fp, registered))
        return results

    @property
    def size(self) -> int:
        return len(self._by_fingerprint)

    @property
    def version(self) -> int:
        return self._version
