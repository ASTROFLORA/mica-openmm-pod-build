"""ClaimAtom — the atomic unit of scientific knowledge in MICA.

Implements K1.1 Claim Anatomy from KB doctrine:
- subject + predicate + object with entity_ref binding
- biological_context and method_context
- quantification with value/unit/comparator
- status (proposed/active/deprecated/retracted/superseded/review_required)
- tier (hypothesis → established)
- Created by EvidencePromotionReceipt only, never from RankingResult directly.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ClaimKind(str, Enum):
    """K1.1: Types of scientific claims."""
    RELATION = "relation"
    PROPERTY = "property"
    EFFECT = "effect"
    MEASUREMENT = "measurement"
    CLASSIFICATION = "classification"
    MECHANISM = "mechanism"
    NEGATIVE_RESULT = "negative_result"


class ClaimStatus(str, Enum):
    """K1.1/K2.2: Claim lifecycle states."""
    PROPOSED = "proposed"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"
    REVIEW_REQUIRED = "review_required"
    UNRESOLVED_ENTITY = "unresolved_entity"


class ClaimTier(str, Enum):
    """K1.4: Ordinal tier scoring — measures support under MICA's evidence model."""
    HYPOTHESIS = "hypothesis"
    SINGLE_SOURCE_SUPPORTED = "single_source_supported"
    LITERATURE_SUPPORTED = "literature_supported"
    COMPUTATIONAL_SUPPORTED = "computational_supported"
    EXPERIMENTALLY_SUPPORTED = "experimentally_supported"
    ESTABLISHED = "established"


class PredicatePolarity(str, Enum):
    """K1.1: Direction of effect."""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class EffectDirection(str, Enum):
    """K1.1: Quantitative direction."""
    INCREASE = "increase"
    DECREASE = "decrease"
    NO_EFFECT = "no_effect"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntityRef:
    """K1.2: Internal stable entity reference, independent of model/collection/namespace."""
    entity_type: str  # protein, gene, compound, disease, etc.
    entity_id: str  # internal UUID-based stable ID
    canonical_label: str  # human-readable label
    taxon_id: Optional[str] = None  # taxon://9606 for human


@dataclass(frozen=True)
class EntityBinding:
    """K1.2: Binding of a claim to an entity with resolver snapshot."""
    role: str  # subject, object
    entity_ref: EntityRef
    resolved_from: str  # external ID used at creation (UniProt:Q96J92)
    resolver_snapshot_ref: str  # resolver://2026_06
    confidence: float = 1.0
    receipt_ref: str = ""


@dataclass(frozen=True)
class PredicateRef:
    """K2.1: Reference to a predicate in the PredicateRegistry."""
    predicate_id: str  # binds_to, activates, inhibits, etc.
    registry_version: str = "v1"
    polarity: PredicatePolarity = PredicatePolarity.NEUTRAL
    direction: EffectDirection = EffectDirection.UNKNOWN


@dataclass(frozen=True)
class Quantification:
    """K1.1/Q3.1: Typed quantification with value, unit, comparator."""
    value: Optional[float] = None
    unit: Optional[str] = None  # UO/QUDT canonical unit ref
    comparator: Optional[str] = None  # >, <, =, ~, range
    effect_size: Optional[float] = None
    quantification_bucket: Optional[str] = None  # for fingerprint


@dataclass(frozen=True)
class BiologicalContext:
    """K1.1/K2.3: Biological context for a claim."""
    organism: str  # taxon://9606
    cell_type: Optional[str] = None  # CL:...
    tissue: Optional[str] = None  # UBERON:...
    condition: Optional[str] = None
    isoform: Optional[str] = None
    mutation: Optional[str] = None
    disease_context: Optional[str] = None  # MONDO:...


@dataclass(frozen=True)
class MethodContext:
    """K1.1: Experimental/computational context."""
    evidence_modality: str  # literature, simulation, assay, curated_external
    method_ref: Optional[str] = None
    protocol_ref: Optional[str] = None


@dataclass
class ClaimAtom:
    """K1.1: The atomic unit of scientific knowledge in MICA.

    A claim is NOT a free-text string. It is a typed, contextualized,
    entity-bound semantic object that can be contradicted, versioned,
    deduplicated, and propagated through the knowledge graph.
    """
    claim_ref: str = field(default_factory=lambda: f"claim://{uuid.uuid4().hex[:12]}")
    claim_kind: ClaimKind = ClaimKind.RELATION

    # Subject-predicate-object
    subject: Optional[EntityBinding] = None
    predicate: Optional[PredicateRef] = None
    object: Optional[EntityBinding] = None
    object_literal: Optional[str] = None  # for non-entity objects

    # Context
    biological_context: Optional[BiologicalContext] = None
    method_context: Optional[MethodContext] = None
    semantic_context_ref: Optional[str] = None  # semctx://...

    # Quantification
    quantification: Optional[Quantification] = None

    # Lifecycle
    status: ClaimStatus = ClaimStatus.PROPOSED
    tier: ClaimTier = ClaimTier.HYPOTHESIS

    # Provenance
    created_from: str = "agent"  # agent, human, imported, protocol_run
    created_by_receipt_ref: Optional[str] = None
    version: int = 1

    # Dedup fingerprint (computed from fields)
    fingerprint: Optional[str] = None

    def __post_init__(self):
        if self.fingerprint is None:
            self.fingerprint = self._compute_fingerprint()

    def _compute_fingerprint(self) -> str:
        """K1.6/K2.3: Compute canonical fingerprint for dedup."""
        parts = [
            self.subject.entity_ref.entity_id if self.subject else "",
            self.predicate.predicate_id if self.predicate else "",
            self.object.entity_ref.entity_id if self.object else (self.object_literal or ""),
            self.biological_context.organism if self.biological_context else "",
            self.biological_context.cell_type if self.biological_context else "",
            self.biological_context.tissue if self.biological_context else "",
            self.biological_context.condition if self.biological_context else "",
            self.quantification.quantification_bucket if self.quantification else "",
            self.predicate.polarity.value if self.predicate else "",
        ]
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def has_resolved_entity(self) -> bool:
        """K1.1: Active claims require resolved entity_ref."""
        return self.subject is not None and self.subject.entity_ref is not None

    @property
    def is_active(self) -> bool:
        """K1.1: A claim is active only if entity is resolved and status is active."""
        return self.status == ClaimStatus.ACTIVE and self.has_resolved_entity

    def can_be_promoted(self) -> bool:
        """K0.1: A claim can only be promoted via EvidencePromotionReceipt."""
        return self.status in {ClaimStatus.PROPOSED, ClaimStatus.REVIEW_REQUIRED}
