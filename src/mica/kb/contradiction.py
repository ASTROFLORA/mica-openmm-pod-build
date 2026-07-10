"""
KB Contradiction Detection — K1.5 (Claim Anatomy & Evidence Core)

Formal contradiction records with overlap_context detection.
Two claims contradict when they are incompatible under overlapping context.

Key objects:
- ContradictionRecord: formal contradiction with kind, context overlap, status
- ContradictionDetector: detects contradictions between ClaimAtom pairs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from .claim_atom import ClaimAtom, BiologicalContext


class ContradictionKind(str, Enum):
    OPPOSITE_DIRECTION = "opposite_direction"
    MUTUALLY_EXCLUSIVE = "mutually_exclusive"
    FAILED_REPLICATION = "failed_replication"
    QUANTITATIVE_CONFLICT = "quantitative_conflict"
    CONTEXT_MISMATCH = "context_mismatch"


class ContradictionStatus(str, Enum):
    OPEN = "contradiction_open"
    RESOLVED = "resolved"
    EXPLAINED_BY_CONTEXT = "explained_by_context"
    FALSE_POSITIVE = "false_positive"
    RETRACTED = "retracted"


def _context_compatible(a: BiologicalContext, b: BiologicalContext) -> bool:
    """Check if two biological contexts overlap enough for contradiction.

    P2-3: Two unspecified contexts (empty organism) do NOT overlap.
    An unspecified context is "broad/weak, not universal".
    """
    # Unspecified contexts never overlap with anything
    if not a.organism or not b.organism:
        return False
    if a.organism != b.organism:
        return False
    if a.cell_type and b.cell_type and a.cell_type != b.cell_type:
        return False
    if a.tissue and b.tissue and a.tissue != b.tissue:
        return False
    if a.condition and b.condition and a.condition != b.condition:
        return False
    return True


@dataclass
class ContradictionRecord:
    """Formal contradiction between two claims under overlapping context."""
    contradiction_ref: str
    claim_a_ref: str
    claim_b_ref: str
    contradiction_kind: ContradictionKind
    overlap_context: Optional[BiologicalContext] = None
    status: ContradictionStatus = ContradictionStatus.OPEN
    explanation: Optional[str] = None
    created_by_receipt_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def resolve(
        self,
        explanation: str,
        status: ContradictionStatus = ContradictionStatus.RESOLVED,
        receipt_ref: Optional[str] = None,
    ):
        """Mark contradiction as resolved."""
        self.status = status
        self.explanation = explanation
        self.created_by_receipt_ref = receipt_ref or self.created_by_receipt_ref


class ContradictionDetector:
    """Detects contradictions between ClaimAtom pairs."""

    def detect(
        self,
        a: ClaimAtom,
        b: ClaimAtom,
        claim_a_ref: str,
        claim_b_ref: str,
        overlap_context: Optional[BiologicalContext] = None,
    ) -> Optional[ContradictionRecord]:
        """Check if two claims contradict each other.

        Returns ContradictionRecord if contradiction found, None if compatible.
        """
        # Must share predicate
        if a.predicate.predicate_id != b.predicate.predicate_id:
            return None

        # Must share subject/object pattern
        if a.subject != b.subject or a.object != b.object:
            return None

        # Check context overlap
        if overlap_context is None:
            overlap_context = BiologicalContext(
                organism=a.biological_context.organism,
                cell_type=a.biological_context.cell_type or b.biological_context.cell_type,
                tissue=a.biological_context.tissue or b.biological_context.tissue,
                condition=a.biological_context.condition or b.biological_context.condition,
            )

        if not _context_compatible(a.biological_context, b.biological_context):
            return None  # Different contexts = not a contradiction

        # Check direction contradiction
        if str(a.predicate.direction) != "unknown" and str(b.predicate.direction) != "unknown" and a.predicate.direction != b.predicate.direction:
            return ContradictionRecord(
                contradiction_ref=f"contradiction://{claim_a_ref}->{claim_b_ref}",
                claim_a_ref=claim_a_ref,
                claim_b_ref=claim_b_ref,
                contradiction_kind=ContradictionKind.OPPOSITE_DIRECTION,
                overlap_context=overlap_context,
            )

        # Check polarity contradiction
        if a.predicate.polarity != "neutral" and b.predicate.polarity != "neutral" and a.predicate.polarity != b.predicate.polarity:
            return ContradictionRecord(
                contradiction_ref=f"contradiction://{claim_a_ref}->{claim_b_ref}",
                claim_a_ref=claim_a_ref,
                claim_b_ref=claim_b_ref,
                contradiction_kind=ContradictionKind.OPPOSITE_DIRECTION,
                overlap_context=overlap_context,
            )

        # Check quantitative conflict
        # P2-1: Without UO/QUDT normalization, comparing 5 µM vs 5000 nM gives
        # false results. Mark as needs_normalization instead of detecting conflict.
        if (a.quantification.value is not None and b.quantification.value is not None
                and a.quantification.unit == b.quantification.unit):
            if str(a.predicate.direction) == "increase" and str(b.predicate.direction) == "decrease":
                return ContradictionRecord(
                    contradiction_ref=f"contradiction://{claim_a_ref}->{claim_b_ref}",
                    claim_a_ref=claim_a_ref,
                    claim_b_ref=claim_b_ref,
                    contradiction_kind=ContradictionKind.QUANTITATIVE_CONFLICT,
                    overlap_context=overlap_context,
                    explanation="quantitative_values_conflict_different_units_need_normalization",
                )

        return None

    def detect_batch(
        self,
        claims: List[tuple[str, ClaimAtom]],
    ) -> List[ContradictionRecord]:
        """Detect all pairwise contradictions in a set of claims."""
        contradictions = []
        for i, (ref_a, atom_a) in enumerate(claims):
            for ref_b, atom_b in claims[i + 1:]:
                record = self.detect(atom_a, atom_b, ref_a, ref_b)
                if record is not None:
                    contradictions.append(record)
        return contradictions
