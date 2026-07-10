"""
KB Consensus — A4 (KB Slice 2)

ConsensusMetaClaim: derived claim when N independent sources agree.
Blocked by open contradictions with overlapping context.

Key objects:
- ConsensusMetaClaim: meta-claim with lineage and consensus status
- ConsensusBuilder: builds consensus from aggregated evidence
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .claim_atom import ClaimAtom, ClaimKind, ClaimStatus, ClaimTier
from .contradiction import ContradictionStatus


@dataclass
class ConsensusMetaClaim:
    """A4: Meta-claim derived when N independent sources agree.

    Consensus is NOT a primary evidence source — it summarizes
    the evidence state. It cannot replace independent evidence.
    """
    meta_claim_ref: str
    claim_family_refs: List[str]  # source claim families that agree
    consensus_predicate: str  # common predicate
    consensus_subject: str  # common subject entity
    consensus_object: str  # common object entity or literal
    consensus_context: str  # shared biological context
    independent_source_count: int
    tier: ClaimTier
    status: str = "active"  # active | blocked_by_contradiction
    blocked_by: List[str] = field(default_factory=list)  # contradiction_refs
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_inferred: bool = True  # meta-claims are always inferred

    def __post_init__(self):
        if not self.meta_claim_ref:
            raw = f"meta:{self.consensus_subject}:{self.consensus_predicate}:{self.consensus_object}"
            self.meta_claim_ref = f"meta_claim://{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


@dataclass
class ConsensusResult:
    """Result of consensus analysis over a set of claims."""
    subject: str
    predicate: str
    object: str
    consensus_met: bool  # True if enough independent agreement
    claim_family_refs: List[str]
    independent_source_count: int
    open_contradictions: List[str]
    consensus_tier: ClaimTier
    status: str  # consensus | blocked_by_contradiction | insufficient_evidence


class ConsensusBuilder:
    """Builds consensus claims from aggregated evidence groups.

    Rules:
    - Minimum independent sources for consensus (default: 2)
    - Open contradictions with overlapping context block consensus
    - Consensus is always is_inferred=True (never primary evidence)
    """

    def __init__(self, min_independent_sources: int = 2):
        self.min_independent_sources = min_independent_sources

    def analyze(
        self,
        claim_families: Dict[str, Any],  # ref -> ClaimFamily
        evidence_groups: Dict[str, Dict[str, Any]],  # claim_ref -> {fingerprint -> group}
        contradictions: Dict[str, Any],  # ref -> ContradictionRecord
    ) -> Optional[ConsensusMetaClaim]:
        """Analyze whether consensus exists across claim families.

        Returns ConsensusMetaClaim if consensus exists, None otherwise.
        """
        if len(claim_families) < self.min_independent_sources:
            return None

        # Find shared predicate, subject, object across families
        predicates = set()
        subjects = set()
        objects = set()

        for ref, family in claim_families.items():
            current = family.current_version
            if current is None:
                continue
            if current.status.value != "active":
                continue
            predicates.add(current.atom.predicate.predicate_id if current.atom.predicate else "")
            subjects.add(current.atom.subject.entity_ref.entity_id if current.atom and current.atom.subject and current.atom.subject.entity_ref else "")
            objects.add(current.atom.object.entity_ref.entity_id if current.atom and current.atom.object and current.atom.object.entity_ref else (current.atom.object_literal if current.atom and current.atom.object_literal else ""))

        if len(predicates) != 1 or len(subjects) != 1 or len(objects) != 1:
            return None  # No consensus — claims diverge

        predicate = predicates.pop()
        subject = subjects.pop()
        obj = objects.pop()

        # Count independent sources
        independent_count = 0
        for ref, groups in evidence_groups.items():
            independent_count += len(groups)

        if independent_count < self.min_independent_sources:
            return None  # Insufficient independent evidence

        # Check open contradictions block consensus
        blocked_by = []
        for c_ref, contradiction in contradictions.items():
            if contradiction.status != ContradictionStatus.OPEN:
                continue
            # Check if contradiction involves any of our claim families
            family_refs = set(claim_families.keys())
            if contradiction.claim_a_ref in family_refs or contradiction.claim_b_ref in family_refs:
                blocked_by.append(c_ref)

        status = "blocked_by_contradiction" if blocked_by else "consensus"

        # Compute tier from independent count
        if independent_count >= 6:
            tier = ClaimTier.ESTABLISHED
        elif independent_count >= 4:
            tier = ClaimTier.EXPERIMENTALLY_SUPPORTED
        elif independent_count >= 2:
            tier = ClaimTier.LITERATURE_SUPPORTED
        else:
            tier = ClaimTier.SINGLE_SOURCE_SUPPORTED

        # Determine shared context
        context = "mixed"
        for ref, family in claim_families.items():
            current = family.current_version
            if current and current.atom.biological_context:
                context = current.atom.biological_context.organism
                break

        return ConsensusMetaClaim(
            meta_claim_ref="",  # auto-computed in __post_init__
            claim_family_refs=list(claim_families.keys()),
            consensus_predicate=predicate,
            consensus_subject=subject,
            consensus_object=obj,
            consensus_context=context,
            independent_source_count=independent_count,
            tier=tier,
            status=status,
            blocked_by=blocked_by,
        )
