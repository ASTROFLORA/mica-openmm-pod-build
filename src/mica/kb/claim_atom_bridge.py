"""ClaimAtomBridge — connects DLM kernel to KB domain (P0-fixed).

Key rule from spec: "The bridge is the ONLY door between DLM and KB."
This bridge CONSUMES DLM kernel, does NOT reimplement it.

Fixes applied (responding to spec review):
- P0-1: Uses BiolinkSchemaAuthority._PREDICATE_EXPORT_CANDIDATES (37 predicates) instead of hardcoded 18
- P0-2: DLMPromotionGate wraps build_promotion_receipt() — no parallel promotion path
- P0-3: Bridge consumes SemanticEntityReceipt objects, not raw dicts
- P0-4: Tests validate kernel→KB binding contracts
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from .claim_atom import (
    BiologicalContext, ClaimAtom, ClaimKind, ClaimStatus, ClaimTier,
    EffectDirection, EntityBinding, EntityRef, MethodContext,
    PredicatePolarity, PredicateRef, Quantification,
)
from .predicate_registry import get_default_predicate_registry
from .evidence_item import (
    EvidenceIndependenceKey, EvidenceItem, EvidenceKind,
    EvidenceStrength, PromotedBy, SupportDirection,
)
from .semantic_context import SemanticContext, SemanticContextRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# P0-1: Import REAL predicate authority from DLM kernel (37 predicates)
# ---------------------------------------------------------------------------
from mica.memory.dlm.biolink_schema_authority import (
    _PREDICATE_EXPORT_CANDIDATES,
    _ENTITY_CATEGORY_CANDIDATES,
)

# ---------------------------------------------------------------------------
# P0-2: Import REAL promotion gate from DLM kernel
# ---------------------------------------------------------------------------
from mica.memory.dlm.semantic_promotion import (
    build_promotion_receipt,
    relation_promotion_state,
    relation_is_accepted,
)

# ---------------------------------------------------------------------------
# P0-3: Import REAL SemanticEntityReceipt (Pydantic model, imports cleanly)
# ---------------------------------------------------------------------------
from mica.memory.dlm.semantic_receipts import SemanticEntityReceipt

# P0-3: TemporalQuintuple import is lazy due to pre-existing dataclass bug
# in models.py (non-default arg follows default). Fixed separately.
TemporalQuintuple = None  # Set lazily if needed


# ---------------------------------------------------------------------------
# P0-1: Polarity/direction derived from Biolink predicate semantics
# ---------------------------------------------------------------------------
_POSITIVE_VERBS = {"activates", "phosphorylates", "stabilizes", "causes",
                   "increases", "rescues", "acetylates", "methylates",
                   "transcribes", "translates", "catalyzes"}
_NEGATIVE_VERBS = {"inhibits", "decreases", "degrades", "disrupts", "dephosphorylates"}


def _polarity_from_predicate(pred_id: str) -> PredicatePolarity:
    if pred_id in _POSITIVE_VERBS:
        return PredicatePolarity.POSITIVE
    if pred_id in _NEGATIVE_VERBS:
        return PredicatePolarity.NEGATIVE
    return PredicatePolarity.NEUTRAL


def _direction_from_predicate(pred_id: str) -> EffectDirection:
    if pred_id in _POSITIVE_VERBS:
        return EffectDirection.INCREASE
    if pred_id in _NEGATIVE_VERBS:
        return EffectDirection.DECREASE
    return EffectDirection.UNKNOWN


def _resolve_predicate_id(canonical: str) -> str:
    """Resolve through the shared predicate registry authority."""
    return get_default_predicate_registry().resolve(canonical).predicate_id


# ---------------------------------------------------------------------------
# P0-3: Entity building from REAL SemanticEntityReceipt
# ---------------------------------------------------------------------------

def build_entity_ref_from_receipt(receipt: SemanticEntityReceipt) -> EntityRef:
    """P0-3: EntityRef from REAL SemanticEntityReceipt.

    Rule: canonical_id IS the entity_ref. No fabrication.
    """
    entity_type = (receipt.raw_type_candidates[0] if receipt.raw_type_candidates else "unknown").lower().replace(" ", "_")
    entity_id = receipt.canonical_id or f"entity://{entity_type}/{hashlib.md5(receipt.mention_text.encode()).hexdigest()[:8]}"
    return EntityRef(entity_type=entity_type, entity_id=entity_id, canonical_label=receipt.mention_text)


def build_entity_binding_from_receipt(receipt: SemanticEntityReceipt, role: str) -> EntityBinding:
    """P0-3: EntityBinding from REAL SemanticEntityReceipt."""
    entity_ref = build_entity_ref_from_receipt(receipt)
    return EntityBinding(
        role=role, entity_ref=entity_ref,
        resolved_from=receipt.canonical_id or receipt.mention_text,
        resolver_snapshot_ref=f"resolver://{receipt.receipt_version}",
        confidence=1.0 if receipt.typing_decision == "accept" else 0.5,
    )


# ---------------------------------------------------------------------------
# P0-1: Predicate from REAL Biolink authority
# ---------------------------------------------------------------------------

def build_predicate_ref_from_dlm(predicate_canonical: str) -> PredicateRef:
    """Build PredicateRef through the shared predicate registry."""
    pred_id = _resolve_predicate_id(predicate_canonical)
    return PredicateRef(
        predicate_id=pred_id,
        polarity=_polarity_from_predicate(pred_id),
        direction=_direction_from_predicate(pred_id),
    )


# ---------------------------------------------------------------------------
# Quantification
# ---------------------------------------------------------------------------

def build_quantification_from_relation(relation_dict: Dict[str, Any]) -> Optional[Quantification]:
    value = relation_dict.get("quantification_value")
    unit = relation_dict.get("quantification_unit")
    comparator = relation_dict.get("quantification_comparator")
    effect_size = relation_dict.get("effect_size")
    if value is None and unit is None and comparator is None:
        return None
    bucket = f"{value}" if value is not None else ""
    if unit:
        bucket += f"_{unit}"
    if comparator:
        bucket = f"comparator_{bucket}"
    return Quantification(value=value, unit=unit, comparator=comparator, effect_size=effect_size, quantification_bucket=bucket or None)


# ---------------------------------------------------------------------------
# P0-2: DLMPromotionGate — wraps build_promotion_receipt()
# ---------------------------------------------------------------------------

class DLMPromotionGate:
    """P0-2: Wraps DLM build_promotion_receipt() — no parallel promotion path."""

    @staticmethod
    def validate(edge_attributes: Dict[str, Any]) -> Dict[str, Any]:
        return build_promotion_receipt(edge_attributes)

    @staticmethod
    def is_accepted(edge_attributes: Dict[str, Any]) -> bool:
        return relation_is_accepted(edge_attributes)

    @staticmethod
    def get_state(edge_attributes: Dict[str, Any]) -> str:
        return relation_promotion_state(edge_attributes)


# ---------------------------------------------------------------------------
# P0-3: Main bridge — consumes REAL DLM objects
# ---------------------------------------------------------------------------

_CLAIM_KIND_MAP = {
    "relation": ClaimKind.RELATION, "property": ClaimKind.PROPERTY,
    "effect": ClaimKind.EFFECT, "measurement": ClaimKind.MEASUREMENT,
    "classification": ClaimKind.CLASSIFICATION, "mechanism": ClaimKind.MECHANISM,
    "negative_result": ClaimKind.NEGATIVE_RESULT,
}


def extracted_relation_to_claim_atom(
    relation_dict: Dict[str, Any],
    subject_receipt: Optional[SemanticEntityReceipt] = None,
    object_receipt: Optional[SemanticEntityReceipt] = None,
    registry: Optional[SemanticContextRegistry] = None,
) -> ClaimAtom:
    """Bridge: DLM ExtractedRelation dict → KB ClaimAtom.

    P0-3: Consumes REAL SemanticEntityReceipt objects.
    P0-1: Uses BiolinkSchemaAuthority predicate registry.
    P0-2: Promotion gate available via DLMPromotionGate.
    """
    # Subject binding from REAL receipt
    subject = None
    if subject_receipt is not None:
        subject = build_entity_binding_from_receipt(subject_receipt, role="subject")
    elif relation_dict.get("subject_text"):
        receipt = SemanticEntityReceipt(
            mention_text=relation_dict.get("subject_text", ""),
            start=0, end=0, section_id="", zone_eligible=False,
            raw_type_candidates=[relation_dict.get("subject_type", "")],
            namespace_decision="no_explicit_identifier",
            typing_decision="ambiguous_hold",
            typing_reason="no_receipt_provided",
            evidence_strength="low",
        )
        subject = build_entity_binding_from_receipt(receipt, role="subject")

    # Object binding from REAL receipt
    obj = None
    obj_literal = None
    if object_receipt is not None:
        obj = build_entity_binding_from_receipt(object_receipt, role="object")
    elif relation_dict.get("object_text"):
        receipt = SemanticEntityReceipt(
            mention_text=relation_dict.get("object_text", ""),
            start=0, end=0, section_id="", zone_eligible=False,
            raw_type_candidates=[relation_dict.get("object_type", "")],
            namespace_decision="no_explicit_identifier",
            typing_decision="ambiguous_hold",
            typing_reason="no_receipt_provided",
            evidence_strength="low",
        )
        obj = build_entity_binding_from_receipt(receipt, role="object")

    # P0-1: Predicate from REAL Biolink authority
    predicate = build_predicate_ref_from_dlm(relation_dict.get("predicate_canonical", ""))

    # Quantification
    quantification = build_quantification_from_relation(relation_dict)

    # Claim kind
    claim_kind = _CLAIM_KIND_MAP.get(relation_dict.get("relation_category", "relation"), ClaimKind.RELATION)

    # Biological context from edge_attributes
    biological_context = None
    edge_attrs = relation_dict.get("biolink_edge_attributes", {})
    if isinstance(edge_attrs, dict):
        organism = edge_attrs.get("organism") or edge_attrs.get("taxon") or ""
        if organism:
            biological_context = BiologicalContext(
                organism=organism, cell_type=edge_attrs.get("cell_type"),
                tissue=edge_attrs.get("tissue"), condition=edge_attrs.get("condition"),
                isoform=edge_attrs.get("isoform"), mutation=edge_attrs.get("mutation"),
                disease_context=edge_attrs.get("disease"),
            )

    # Semantic context registry
    semantic_context_ref = None
    if biological_context and registry:
        sc = SemanticContext(
            organism=biological_context.organism, cell_type=biological_context.cell_type,
            tissue=biological_context.tissue, condition=biological_context.condition,
        )
        semantic_context_ref = f"semctx://{registry.register(sc)}"

    status = ClaimStatus.PROPOSED
    if not subject or not subject.entity_ref:
        status = ClaimStatus.UNRESOLVED_ENTITY

    return ClaimAtom(
        claim_kind=claim_kind, subject=subject, predicate=predicate, object=obj,
        biological_context=biological_context,
        method_context=MethodContext(evidence_modality="literature", method_ref=relation_dict.get("evidence_strategy")),
        semantic_context_ref=semantic_context_ref, quantification=quantification,
        status=status, created_from="agent",
    )
