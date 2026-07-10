"""
KB Evidence Aggregator — A3/A5 (KB Slice 2)

Corpus-level dedup by independence_key + aggregate weight for tier scoring.
Replaces raw evidence counting with proper independent source grouping.

Key objects:
- EvidenceAggregator: groups evidence by independence_key at corpus scale
- AggregatedClaimSupport: weighted support from independent groups
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .claim_atom import ClaimTier
from .evidence_item import EvidenceIndependenceKey, EvidenceItem, EvidenceKind, SupportDirection


# Tier thresholds (ordinal)
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


@dataclass
class EvidenceGroup:
    """A group of evidence items from the same independent source."""
    key_fingerprint: str
    items: List[EvidenceItem]
    best_weight: float = 0.0
    source_work_ref: str = ""
    source_count: int = 0

    def __post_init__(self):
        if self.items:
            self.best_weight = max(item.weight_for_tier() for item in self.items)
            refs = set()
            for item in self.items:
                if item.independence_key:
                    refs.add(item.independence_key.source_work_ref)
            self.source_work_ref = refs.pop() if refs else ""
            self.source_count = len(refs)


@dataclass
class AggregatedClaimSupport:
    """Aggregated support for a claim from independent evidence groups."""
    claim_ref: str
    total_weight: float
    independent_group_count: int
    groups: List[EvidenceGroup]
    contradiction_penalty: float
    staleness_penalty: float
    final_score: float
    tier: ClaimTier
    supports_count: int = 0
    contradicts_count: int = 0
    contextualizes_count: int = 0


class EvidenceAggregator:
    """A3: Corpus-level evidence aggregation with independence dedup.

    5 chunks from same paper = 1 group with best weight.
    Different papers = different groups, each contributing independently.
    """

    def __init__(self, evidence: Dict[str, EvidenceItem]):
        self._evidence = evidence

    def group_by_claim(self, claim_ref: str) -> Dict[str, EvidenceGroup]:
        """Group evidence for a claim by independence key fingerprint."""
        claim_items = [e for e in self._evidence.values() if e.claim_ref == claim_ref]
        raw_groups: Dict[str, List[EvidenceItem]] = defaultdict(list)

        for item in claim_items:
            if item.independence_key:
                key = item.independence_key.to_fingerprint()
            else:
                key = item.source_doi or item.source_pmid or item.evidence_ref
            raw_groups[key].append(item)

        result = {}
        for fingerprint, items in raw_groups.items():
            result[fingerprint] = EvidenceGroup(
                key_fingerprint=fingerprint,
                items=items,
            )
        return result

    def aggregate_for_claim(
        self,
        claim_ref: str,
        contradiction_count: int = 0,
        age_days: float = 0.0,
        max_staleness_days: float = 365.0,
    ) -> AggregatedClaimSupport:
        """A3+A5: Aggregate evidence for a claim into weighted support.

        Uses independent groups, not raw counts.
        """
        groups = self.group_by_claim(claim_ref)
        group_list = list(groups.values())

        # Sum best weight per independent group
        total_weight = sum(g.best_weight for g in group_list)

        # Direction counts
        supports = sum(
            1 for g in group_list
            for item in g.items
            if item.support_direction == SupportDirection.SUPPORTS
        )
        contradicts = sum(
            1 for g in group_list
            for item in g.items
            if item.support_direction == SupportDirection.CONTRADICTS
        )
        contextualizes = sum(
            1 for g in group_list
            for item in g.items
            if item.support_direction == SupportDirection.CONTEXTUALIZES
        )

        # Penalties
        contradiction_penalty = contradiction_count * 0.4
        staleness_penalty = min(age_days / max_staleness_days, 1.0) * 0.5

        # Final score
        score = total_weight - contradiction_penalty - staleness_penalty
        score = max(score, 0.0)

        # Tier from score
        tier = ClaimTier.HYPOTHESIS
        for t in TIER_ORDER:
            if score >= TIER_THRESHOLDS[t]:
                tier = t

        return AggregatedClaimSupport(
            claim_ref=claim_ref,
            total_weight=total_weight,
            independent_group_count=len(group_list),
            groups=group_list,
            contradiction_penalty=contradiction_penalty,
            staleness_penalty=staleness_penalty,
            final_score=score,
            tier=tier,
            supports_count=supports,
            contradicts_count=contradicts,
            contextualizes_count=contextualizes,
        )

    def aggregate_all(
        self,
        contradiction_counts: Optional[Dict[str, int]] = None,
        claim_ages: Optional[Dict[str, float]] = None,
    ) -> List[AggregatedClaimSupport]:
        """Aggregate support for all claims."""
        cc = contradiction_counts or {}
        ages = claim_ages or {}

        # Find all unique claim_refs
        claim_refs = set(e.claim_ref for e in self._evidence.values())
        results = []
        for ref in claim_refs:
            results.append(self.aggregate_for_claim(
                claim_ref=ref,
                contradiction_count=cc.get(ref, 0),
                age_days=ages.get(ref, 0.0),
            ))
        return sorted(results, key=lambda a: a.final_score, reverse=True)
