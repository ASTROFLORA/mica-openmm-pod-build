"""
KB Inference Layer — A7 (KB Slice 2)

Derives inferred claims with provenance from existing claims.
Inferred claims carry derivation_provenance and is_inferred=True.
Never reaches 'established' tier without independent empirical evidence.

Key objects:
- InferredClaim: derived claim with derivation provenance
- InferenceEngine: produces inferred claims from existing claims + rules
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .claim_atom import ClaimAtom, ClaimKind, ClaimStatus, ClaimTier


@dataclass
class DerivationProvenance:
    """Provenance for an inferred claim."""
    source_claim_refs: List[str]  # claims used to derive this
    rule_name: str  # which inference rule was applied
    rule_version: str = "v1"
    confidence: float = 0.5
    is_inferred: bool = True


@dataclass
class InferredClaim:
    """A7: Derived claim with provenance. Always is_inferred=True."""
    claim_ref: str
    atom: ClaimAtom
    derivation: DerivationProvenance
    max_tier: ClaimTier = ClaimTier.COMPUTATIONAL_SUPPORTED  # ceiling without empirical evidence
    status: ClaimStatus = ClaimStatus.PROPOSED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Inference rules

def rule_transitive_relation(
    claims: List[ClaimAtom],
    max_chain: int = 2,
) -> List[Tuple[ClaimAtom, ClaimAtom, DerivationProvenance]]:
    """If A inhibits B and B inhibits C, infer A inhibits C (chain length ≤ max_chain).

    Returns list of (derived_atom, source_claims, provenance) tuples.
    """
    results = []
    by_subject = {}
    for claim in claims:
        if claim.subject and claim.subject.entity_ref:
            key = claim.subject.entity_ref.entity_id
            if key not in by_subject:
                by_subject[key] = []
            by_subject[key].append(claim)

    for claim_b in claims:
        if not claim_b.subject or not claim_b.object:
            continue
        obj_id = claim_b.object.entity_ref.entity_id if claim_b.object and claim_b.object.entity_ref else ""
        if obj_id not in by_subject:
            continue
        for claim_c in by_subject[obj_id]:
            if claim_c.predicate and claim_b.predicate:
                if claim_c.predicate.predicate_id == claim_b.predicate.predicate_id:
                    # A inhibits B, B inhibits C -> A inhibits C
                    if claim_b.subject and claim_b.subject.entity_ref:
                        derived = ClaimAtom(
                            claim_kind=ClaimKind.RELATION,
                            subject=claim_b.subject,
                            predicate=claim_b.predicate,
                            object=claim_c.object,
                            object_literal=claim_c.object_literal,
                            status=ClaimStatus.PROPOSED,
                            created_from="inference",
                        )
                        provenance = DerivationProvenance(
                            source_claim_refs=[
                                claim_b.subject.entity_ref.entity_id if claim_b.subject else "",
                                claim_c.subject.entity_ref.entity_id if claim_c.subject else "",
                            ],
                            rule_name="transitive_relation",
                            confidence=0.4,
                        )
                        results.append((derived, [claim_b, claim_c], provenance))
    return results


class InferenceEngine:
    """A7: Produces inferred claims from existing claims + rules."""

    def __init__(self):
        self._rules = [rule_transitive_relation]

    def infer(
        self,
        claims: List[ClaimAtom],
        max_chain: int = 2,
    ) -> List[InferredClaim]:
        """Run all inference rules on the claim set."""
        all_results = []
        for rule in self._rules:
            for derived, sources, provenance in rule(claims, max_chain=max_chain):
                all_results.append(InferredClaim(
                    claim_ref=derived.claim_ref,
                    atom=derived,
                    derivation=provenance,
                    max_tier=ClaimTier.COMPUTATIONAL_SUPPORTED,
                    status=ClaimStatus.PROPOSED,
                ))
        return all_results
