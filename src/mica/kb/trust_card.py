"""
KB Trust Card — A9 (KB Slice 2)

Composes tier + support + contradictions + lineage into a human-readable trust card.
Shows DLM/ATOM lineage without reinterpreting it.

Key objects:
- TrustCard: composed view of claim trustworthiness
- TrustCardBuilder: builds trust cards from KB components
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .claim_atom import ClaimAtom, ClaimTier, ClaimStatus
from .contradiction import ContradictionStatus
from .evidence_item import EvidenceItem, SupportDirection


@dataclass
class TrustCard:
    """A9: Human-readable trust card for a claim."""
    claim_family_ref: str
    tier: str  # ClaimTier value
    independent_source_count: int
    total_evidence_count: int
    support_summary: Dict[str, int]  # supports/contradicts/contextualizes counts
    open_contradictions: int
    consensus: Optional[str]  # meta_claim://... or None
    lineage: List[str]  # chain of provenance references
    as_of: str  # ISO timestamp of snapshot
    caveats: List[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class LicenseGate:
    """K1.7: License gate for a source."""
    source_ref: str
    can_store_metadata: bool
    can_store_fulltext: bool
    can_embed_text: bool
    can_serve_snippet: bool
    can_export_text: bool
    requires_attribution: bool
    license_ref: str  # cc-by-4.0, cc-by-nc, closed, unknown


class TrustCardBuilder:
    """A9: Builds trust cards from KB components."""

    def build(
        self,
        claim_family_ref: str,
        atom: ClaimAtom,
        tier: ClaimTier,
        status: ClaimStatus,
        evidence: List[EvidenceItem],
        contradiction_count: int = 0,
        consensus_ref: Optional[str] = None,
        lineage: Optional[List[str]] = None,
        version_refs: Optional[List[str]] = None,
        license_gates: Optional[Dict[str, LicenseGate]] = None,
        as_of: Optional[datetime] = None,
    ) -> TrustCard:
        """Build a trust card from claim components."""
        # Direction counts
        supports = sum(1 for e in evidence if e.support_direction == SupportDirection.SUPPORTS)
        contradicts = sum(1 for e in evidence if e.support_direction == SupportDirection.CONTRADICTS)
        contextualizes = sum(1 for e in evidence if e.support_direction == SupportDirection.CONTEXTUALIZES)

        # Build lineage from atom provenance
        lineage_list = list(lineage or [])
        if atom.created_by_receipt_ref:
            lineage_list.append(atom.created_by_receipt_ref)
        if atom.semantic_context_ref:
            lineage_list.append(atom.semantic_context_ref)
        for vr in (version_refs or []):
            lineage_list.append(vr)

        # Check license gates for caveats
        caveats = []
        if license_gates:
            for ref, gate in license_gates.items():
                if not gate.can_embed_text:
                    caveats.append(f"Source {ref} cannot be embedded (license: {gate.license_ref})")
                if not gate.can_serve_snippet:
                    caveats.append(f"Source {ref} snippets blocked (license: {gate.license_ref})")

        return TrustCard(
            claim_family_ref=claim_family_ref,
            tier=tier.value,
            independent_source_count=len(set(
                e.independence_key.source_work_ref
                for e in evidence if e.independence_key
            )),
            total_evidence_count=len(evidence),
            support_summary={
                "supports": supports,
                "contradicts": contradicts,
                "contextualizes": contextualizes,
            },
            open_contradictions=contradiction_count,
            consensus=consensus_ref,
            lineage=lineage_list,
            as_of=(as_of or datetime.now(timezone.utc)).isoformat(),
            caveats=caveats,
            status=status.value,
        )
