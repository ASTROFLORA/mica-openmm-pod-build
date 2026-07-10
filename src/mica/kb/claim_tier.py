"""ClaimTier — ordinal scoring for scientific claims.

Implements K1.4 ClaimSupportScore:
- Σ independent_evidence_weight
- - contradiction_penalty
- - staleness_penalty
- - license/availability_penalty
- - identity_uncertainty_penalty

Anti-dedup: 5 chunks from same paper = 1 source with 5 spans, NOT 5 evidences.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .claim_atom import ClaimAtom, ClaimTier
from .evidence_item import (
    EvidenceIndependenceKey,
    EvidenceItem,
    EvidenceKind,
    SupportDirection,
)


# Tier thresholds (ordinal, not absolute)
TIER_THRESHOLDS = {
    ClaimTier.HYPOTHESIS: 0.0,
    ClaimTier.SINGLE_SOURCE_SUPPORTED: 0.3,
    ClaimTier.LITERATURE_SUPPORTED: 0.8,
    ClaimTier.COMPUTATIONAL_SUPPORTED: 1.2,
    ClaimTier.EXPERIMENTALLY_SUPPORTED: 1.8,
    ClaimTier.ESTABLISHED: 2.5,
}

TIER_ORDER = [
    ClaimTier.HYPOTHESIS,
    ClaimTier.SINGLE_SOURCE_SUPPORTED,
    ClaimTier.LITERATURE_SUPPORTED,
    ClaimTier.COMPUTATIONAL_SUPPORTED,
    ClaimTier.EXPERIMENTALLY_SUPPORTED,
    ClaimTier.ESTABLISHED,
]


def _tier_from_score(score: float) -> ClaimTier:
    """Convert a numeric support score to ordinal tier."""
    result = ClaimTier.HYPOTHESIS
    for tier in TIER_ORDER:
        if score >= TIER_THRESHOLDS[tier]:
            result = tier
    return result


@dataclass
class TierScoringResult:
    """Result of tier scoring computation."""
    claim_ref: str
    score: float
    tier: ClaimTier
    independent_source_count: int
    evidence_count_before_dedup: int
    contradiction_penalty: float
    staleness_penalty: float
    details: Dict[str, float] = field(default_factory=dict)


def compute_independent_evidence_groups(
    evidence_items: List[EvidenceItem],
) -> Dict[str, List[EvidenceItem]]:
    """K1.4/K1.6: Group evidence by independence key.

    Anti-dedup: 5 chunks from the same paper = 1 source with 5 spans,
    NOT 5 independent evidences.
    """
    groups: Dict[str, List[EvidenceItem]] = defaultdict(list)
    for item in evidence_items:
        if item.independence_key:
            key = item.independence_key.to_fingerprint()
        else:
            # Fallback: use source DOI/PMID as key
            key = item.source_doi or item.source_pmid or item.evidence_ref
        groups[key].append(item)
    return dict(groups)


def score_claim_tier(
    claim: ClaimAtom,
    evidence_items: List[EvidenceItem],
    contradiction_count: int = 0,
    age_days: float = 0.0,
    max_staleness_days: float = 365.0,
) -> TierScoringResult:
    """K1.4: Compute ClaimSupportScore and resulting tier.

    Uses independent evidence groups to prevent double-counting.
    Applies contradiction and staleness penalties.
    """
    # Group by independence key
    groups = compute_independent_evidence_groups(evidence_items)
    independent_count = len(groups)

    # Sum weights from independent groups (using best evidence per group)
    total_weight = 0.0
    for key, items in groups.items():
        # Use the highest-weight evidence from each group
        best_weight = max(item.weight_for_tier() for item in items)
        total_weight += best_weight

    # Contradiction penalty (proportional to count)
    contradiction_penalty = contradiction_count * 0.4

    # Staleness penalty (grows with age)
    staleness_penalty = min(age_days / max_staleness_days, 1.0) * 0.5

    # Final score
    score = total_weight - contradiction_penalty - staleness_penalty
    score = max(score, 0.0)  # Floor at 0

    tier = _tier_from_score(score)

    return TierScoringResult(
        claim_ref=claim.claim_ref,
        score=score,
        tier=tier,
        independent_source_count=independent_count,
        evidence_count_before_dedup=len(evidence_items),
        contradiction_penalty=contradiction_penalty,
        staleness_penalty=staleness_penalty,
        details={
            "total_weight": total_weight,
            "independent_groups": independent_count,
            "raw_evidence_count": len(evidence_items),
        },
    )
