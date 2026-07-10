"""EvidenceItem — typed evidence object for scientific claims.

Implements K0.1/K1.4 Evidence contract:
- evidence_ref, claim_ref, artifact_ref, chunk_refs, quoted_span_refs
- evidence_kind discriminator (literature, simulation, assay, curated_external)
- support_direction (supports, contradicts, contextualizes)
- strength (weak, moderate, strong)
- promoted_by (user, agent_proposal)
- receipt_ref (EvidencePromotionReceipt required)

Key rule: RankingResult NEVER becomes EvidenceItem directly.
Evidence only enters via EvidencePromotionReceipt.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EvidenceKind(str, Enum):
    """K0.1/K1.10: Type of evidence source."""
    LITERATURE = "literature"
    SIMULATION = "simulation"
    ASSAY = "assay"
    CURATED_EXTERNAL = "curated_external"
    PROTOCOL_RUN = "protocol_run"
    INSTRUMENT_RUN = "instrument_run"
    DATASET = "dataset"
    MANUAL_CURATED = "manual_curated_observation"


class SupportDirection(str, Enum):
    """K1.4: How evidence relates to the claim."""
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXTUALIZES = "contextualizes"


class EvidenceStrength(str, Enum):
    """K1.4: Strength of evidence."""
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class PromotedBy(str, Enum):
    """K0.1: How evidence entered the KB."""
    USER = "user"
    AGENT_PROPOSAL = "agent_proposal"


@dataclass
class EvidenceIndependenceKey:
    """K1.4/K1.6: Key for counting independent evidence sources.

    Anti-dedup: 5 chunks from the same paper = 1 source with 5 spans,
    NOT 5 independent evidences.
    """
    source_work_ref: str  # DOI/PMID/protocol_run_id
    source_version_ref: str  # LiteratureVersion or protocol version
    method_family: str  # experimental_method_class
    experimental_system: str  # cell_line, organism, in_silico
    lab_group: Optional[str] = None
    protocol_run_family: Optional[str] = None

    def to_fingerprint(self) -> str:
        """Unique key for independent evidence counting."""
        parts = [
            self.source_work_ref,
            self.source_version_ref,
            self.method_family,
            self.experimental_system,
            self.lab_group or "",
            self.protocol_run_family or "",
        ]
        return "|".join(parts)


@dataclass
class EvidenceItem:
    """K0.1: Typed evidence object. Never created from RankingResult directly.

    The only path to create an EvidenceItem is via EvidencePromotionReceipt
    after human/agent proposal and validation.
    """
    evidence_ref: str = field(default_factory=lambda: f"evidence://{uuid.uuid4().hex[:12]}")
    claim_ref: str = ""
    artifact_ref: Optional[str] = None  # paper DOI/PMID, protocol_run, etc.
    chunk_refs: List[str] = field(default_factory=list)
    quoted_span_refs: List[str] = field(default_factory=list)

    # Type and direction
    evidence_kind: EvidenceKind = EvidenceKind.LITERATURE
    support_direction: SupportDirection = SupportDirection.SUPPORTS
    strength: EvidenceStrength = EvidenceStrength.MODERATE

    # Independence tracking (anti-dedup)
    independence_key: Optional[EvidenceIndependenceKey] = None

    # Provenance
    promoted_by: PromotedBy = PromotedBy.USER
    receipt_ref: Optional[str] = None  # EvidencePromotionReceipt ref
    confidence: float = 0.5

    # Source metadata
    source_doi: Optional[str] = None
    source_pmid: Optional[str] = None
    source_license: Optional[str] = None  # cc-by-4.0, cc-by-nc, closed, etc.
    section_type: Optional[str] = None  # results, methods, discussion, etc.

    def is_independent_from(self, other: "EvidenceItem") -> bool:
        """K1.6: Two evidence items are independent iff their independence keys differ."""
        if self.independence_key is None or other.independence_key is None:
            return True  # Can't determine, assume independent
        return self.independence_key.to_fingerprint() != other.independence_key.to_fingerprint()

    def requires_promotion_receipt(self) -> bool:
        """K0.1: Evidence can only enter KB via EvidencePromotionReceipt."""
        return self.receipt_ref is None or self.receipt_ref == ""

    def weight_for_tier(self) -> float:
        """K1.4: Compute evidence weight for tier scoring.

        NOTE: The contradicts direction_mult (-0.3) is an evidence-level penalty
        for evidence that contradicts THIS claim. It is NOT the same as the
        cross-claim contradiction_penalty (0.4×count) in score_claim_tier().
        They operate at different levels: this is per-evidence, that is per-claim-pair.
        """
        base = {
            EvidenceKind.LITERATURE: 0.5,
            EvidenceKind.SIMULATION: 0.4,
            EvidenceKind.ASSAY: 0.7,
            EvidenceKind.CURATED_EXTERNAL: 0.8,
            EvidenceKind.PROTOCOL_RUN: 0.6,
            EvidenceKind.MANUAL_CURATED: 0.9,
        }.get(self.evidence_kind, 0.3)

        strength_mult = {
            EvidenceStrength.WEAK: 0.5,
            EvidenceStrength.MODERATE: 1.0,
            EvidenceStrength.STRONG: 1.5,
        }.get(self.strength, 1.0)

        direction_mult = {
            SupportDirection.SUPPORTS: 1.0,
            SupportDirection.CONTRADICTS: -0.3,  # penalty, not removal
            SupportDirection.CONTEXTUALIZES: 0.7,
        }.get(self.support_direction, 0.5)

        return base * strength_mult * direction_mult
