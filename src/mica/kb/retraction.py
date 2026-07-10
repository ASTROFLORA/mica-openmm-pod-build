"""
KB Retraction Pipeline — K1.11 (Claim Anatomy & Evidence Core)

Automated retraction propagation: when evidence is retracted,
affected claims are recomputed and downgraded as needed.

Key objects:
- RetractionReceipt: records the retraction event
- RetractionPipeline: propagates retraction through the KB
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .claim_atom import ClaimStatus
from .claim_versioning import ClaimFamily
from .contradiction import ContradictionRecord, ContradictionStatus
from .evidence_item import EvidenceItem, EvidenceKind
from .kb_store import KBStore


@dataclass
class RetractionReceipt:
    """Receipt of a retraction propagation event."""
    receipt_ref: str
    retracted_evidence_refs: List[str]
    affected_claim_families: List[str]
    affected_contradictions: List[str]
    downgraded_claims: List[str]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str = ""


class RetractionPipeline:
    """Propagates evidence retractions through the KB.

    When evidence is retracted:
    1. Find all claims that depend on the retracted evidence
    2. Recompute support score for each affected claim
    3. Downgrade claims that lost critical support
    4. Mark contradictions involving retracted evidence as resolved
    """

    def __init__(self, kb: KBStore):
        self.kb = kb

    def propagate(
        self,
        retracted_evidence_refs: List[str],
        reason: str = "Evidence retracted",
        min_support_for_active: int = 1,
    ) -> RetractionReceipt:
        """Propagate retraction of evidence through the KB.

        Args:
            retracted_evidence_refs: Evidence refs that were retracted
            reason: Human-readable reason
            min_support_for_active: Minimum independent sources to remain active
        """
        affected_families = set()
        affected_contradictions = set()
        downgraded = []

        # Find affected claims
        for ref in retracted_evidence_refs:
            evidence = self.kb.get_evidence(ref)
            if evidence is None:
                continue
            if evidence.claim_ref:
                affected_families.add(evidence.claim_ref)

        # Find contradictions involving retracted evidence
        for c_ref, c in self.kb._contradictions.items():
            for evidence_ref in retracted_evidence_refs:
                evidence = self.kb.get_evidence(evidence_ref)
                if evidence and (evidence.claim_ref == c.claim_a_ref or evidence.claim_ref == c.claim_b_ref):
                    affected_contradictions.add(c_ref)

        # Recompute support for affected claims
        for family_ref in affected_families:
            family = self.kb._families.get(family_ref)
            if family is None:
                continue
            current = family.current_version
            if current is None:
                continue

            # Count non-retracted evidence
            active_evidence = [
                e for e in self.kb._evidence.values()
                if e.claim_ref == family_ref
                and e.evidence_ref not in retracted_evidence_refs
            ]
            independent_sources = len(set(
                e.independence_key.source_work_ref
                for e in active_evidence
                if e.independence_key.source_work_ref
            ))

            # Downgrade if insufficient support
            if independent_sources < min_support_for_active:
                if current.status == ClaimStatus.ACTIVE:
                    family.retract(
                        reason=f"Support removed by retraction: {reason}",
                        receipt_ref=f"retraction://{family_ref}",
                    )
                    downgraded.append(family_ref)

        # Resolve contradictions involving retracted evidence
        for c_ref in affected_contradictions:
            c = self.kb._contradictions.get(c_ref)
            if c and c.status == ContradictionStatus.OPEN:
                c.status = ContradictionStatus.RETRACTED
                c.explanation = f"Retracted evidence: {reason}"

        return RetractionReceipt(
            receipt_ref=f"retraction://{datetime.now(timezone.utc).isoformat()}",
            retracted_evidence_refs=retracted_evidence_refs,
            affected_claim_families=list(affected_families),
            affected_contradictions=list(affected_contradictions),
            downgraded_claims=downgraded,
            reason=reason,
        )
