"""ClaimDomainClassifier — P1/INV-7 (KB Slice 5 · P1+P3 gaps).

Gate constitucional: corre ANTES del ClaimAtomBridge (INV-7).
Clasifica si una afirmación es científicamente adjudicable, normativa,
jurídica, estética, ética, metafísica, experiencial, tácita o comunitaria.

La brecha is/ought: de hechos no se deriva automáticamente un deber.

Red-line: No scientification of everything.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class DomainKind(str, Enum):
    SCIENTIFIC = "scientific"
    NORMATIVE = "normative"
    ETHICAL = "ethical"
    LEGAL = "legal"
    AESTHETIC = "aesthetic"
    METAPHYSICAL = "metaphysical"
    TACIT = "tacit"
    COMMUNITY_KNOWLEDGE = "community_knowledge"
    MIXED = "mixed"


class AllowedMicaAction(str, Enum):
    EXTRACT = "extract"                        # → ClaimAtomBridge
    CONTEXTUALIZE = "contextualize"            # → context, not claim
    FLAG_BOUNDARY = "flag_boundary"            # → note boundary, no action
    ROUTE_TO_GOVERNANCE = "route_to_governance" # → NormativePosition
    DO_NOT_ADJUDICATE = "do_not_adjudicate"    # → blocked


class PredicateCategory(str, Enum):
    """How a predicate maps to scientific adjudication."""
    SCIENTIFIC = "scientific"           # Biolink-predicate-backed, adjudicable
    NORMATIVE = "normative"             # requires value premise
    MIXED = "mixed"                     # factual premise + value inference
    NON_ADJUDICABLE = "non_adjudicable" # no scientific grounding


# K12.1 routing rules (from spec):
# scientific → ClaimAtomBridge
# normative/ethical → NormativePosition, NOT ClaimVersion
# legal → LegalInterpretationRecord
# aesthetic/metaphysical → context, no adjudication
# tacit/community → governance of carriers, no unilateral extraction

_DOMAIN_ROUTING: Dict[DomainKind, AllowedMicaAction] = {
    DomainKind.SCIENTIFIC: AllowedMicaAction.EXTRACT,
    DomainKind.NORMATIVE: AllowedMicaAction.ROUTE_TO_GOVERNANCE,
    DomainKind.ETHICAL: AllowedMicaAction.ROUTE_TO_GOVERNANCE,
    DomainKind.LEGAL: AllowedMicaAction.CONTEXTUALIZE,
    DomainKind.AESTHETIC: AllowedMicaAction.CONTEXTUALIZE,
    DomainKind.METAPHYSICAL: AllowedMicaAction.CONTEXTUALIZE,
    DomainKind.TACIT: AllowedMicaAction.FLAG_BOUNDARY,
    DomainKind.COMMUNITY_KNOWLEDGE: AllowedMicaAction.ROUTE_TO_GOVERNANCE,
    DomainKind.MIXED: AllowedMicaAction.FLAG_BOUNDARY,
}


# Heuristic markers for domain detection
_SCIENTIFIC_MARKERS = {
    "phosphorylates", "activates", "inhibits", "binds", "expresses",
    "regulates", "phosphorylation", "kinase", "receptor", "ligand",
    "affinity", "ic50", "ec50", "kd", "km", "vmax", "mutation",
    "deletion", "knockdown", "overexpression", "assay", "p-value",
    "fold_change", "upregulated", "downregulated",
}

_NORMATIVE_MARKERS = {
    "should", "must", "ought", "require", "recommend", "policy",
    "guideline", "standard", "regulation", "compliance", "approve",
    "prohibit", "mandate", "forbidden", "obligatory",
}

_ETHICAL_MARKERS = {
    "consent", "autonomy", "beneficence", "non_maleficence",
    "justice", "ethics", "irb", "irb_approval", "deidentification",
    "fairness", "equity", "harm", "dignity", "rights",
}

_LEGAL_MARKERS = {
    "patent", "copyright", "license", "jurisdiction", "statute",
    "regulation", "compliance", "liability", "indemnity",
    "contract", "agreement", "nda", "confidential",
}

_COMMUNITY_MARKERS = {
    "indigenous", "traditional", "community", "oral", "local_knowledge",
    "craft", "artisan", "folk", "custom", "practitioner",
}


@dataclass
class DomainClassification:
    """Result of domain classification for a statement."""
    classification_ref: str
    statement_ref: str
    domain_kind: DomainKind
    scientifically_adjudicable: bool
    requires_value_premise: bool
    requires_community_authority: bool
    allowed_mica_action: AllowedMicaAction
    predicate_category: PredicateCategory = PredicateCategory.SCIENTIFIC
    confidence: float = 0.0  # 0.0-1.0
    reasoning: str = ""
    created_by_receipt_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _classify_domain_heuristic(
    text: str,
    predicate_id: Optional[str] = None,
) -> Tuple[DomainKind, float, str]:
    """Heuristic domain classification from text + predicate."""
    text_lower = text.lower()

    sci_score = sum(1 for m in _SCIENTIFIC_MARKERS if m in text_lower)
    norm_score = sum(1 for m in _NORMATIVE_MARKERS if m in text_lower)
    eth_score = sum(1 for m in _ETHICAL_MARKERS if m in text_lower)
    legal_score = sum(1 for m in _LEGAL_MARKERS if m in text_lower)
    comm_score = sum(1 for m in _COMMUNITY_MARKERS if m in text_lower)

    total = sci_score + norm_score + eth_score + legal_score + comm_score
    if total == 0:
        return DomainKind.MIXED, 0.0, "no_markers_detected"

    scores = {
        DomainKind.SCIENTIFIC: sci_score,
        DomainKind.NORMATIVE: norm_score,
        DomainKind.ETHICAL: eth_score,
        DomainKind.LEGAL: legal_score,
        DomainKind.COMMUNITY_KNOWLEDGE: comm_score,
    }
    best = max(scores, key=scores.get)
    confidence = scores[best] / total

    return best, confidence, f"score_{scores[best]}/{total}"


class ClaimDomainClassifier:
    """K12.1: Pre-bridge domain classifier (INV-7).

    Runs BEFORE ClaimAtomBridge. Non-scientific statements are routed
    away before they can enter as scientific claims.

    INV-7: "ClaimDomainClassifier corre ANTES del bridge.
    No-científico no entra como claim científico."
    """

    def __init__(self) -> None:
        self._classifications: Dict[str, DomainClassification] = {}
        self._on_route: Optional[Callable[[str, DomainKind, AllowedMicaAction], None]] = None

    def classify(
        self,
        statement_ref: str,
        text: str,
        predicate_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> DomainClassification:
        """Classify a statement's domain before bridge entry."""
        domain, confidence, reasoning = _classify_domain_heuristic(text, predicate_id)

        # Override: if predicate is in Biolink catalog, it's scientific
        if predicate_id and predicate_id in _SCIENTIFIC_MARKERS:
            domain = DomainKind.SCIENTIFIC
            confidence = 0.95
            reasoning = "predicate_biolink_backed"

        action = _DOMAIN_ROUTING.get(domain, AllowedMicaAction.DO_NOT_ADJUDICATE)
        scientifically_adjudicable = (domain == DomainKind.SCIENTIFIC)
        requires_value_premise = domain in (DomainKind.NORMATIVE, DomainKind.ETHICAL)
        requires_community_authority = domain == DomainKind.COMMUNITY_KNOWLEDGE

        classification = DomainClassification(
            classification_ref=f"domain://{statement_ref}/{datetime.now(timezone.utc).isoformat()}",
            statement_ref=statement_ref,
            domain_kind=domain,
            scientifically_adjudicable=scientifically_adjudicable,
            requires_value_premise=requires_value_premise,
            requires_community_authority=requires_community_authority,
            allowed_mica_action=action,
            confidence=confidence,
            reasoning=reasoning,
        )
        self._classifications[classification.classification_ref] = classification
        return classification

    def get_classification(self, classification_ref: str) -> Optional[DomainClassification]:
        return self._classifications.get(classification_ref)

    def can_enter_bridge(self, classification_or_ref) -> bool:
        """Check if a classification allows bridge entry. Accepts DomainClassification or statement_ref str."""
        if hasattr(classification_or_ref, 'allowed_mica_action'):
            return classification_or_ref.allowed_mica_action == AllowedMicaAction.EXTRACT
        for c in self._classifications.values():
            if c.statement_ref == classification_or_ref:
                return c.allowed_mica_action == AllowedMicaAction.EXTRACT
        return False

    def list_classifications(self, domain: Optional[DomainKind] = None) -> List[DomainClassification]:
        classes = list(self._classifications.values())
        if domain:
            classes = [c for c in classes if c.domain_kind == domain]
        return classes

    def classification_summary(self) -> Dict[str, int]:
        """Count by domain kind."""
        counts: Dict[str, int] = {}
        for c in self._classifications.values():
            counts[c.domain_kind.value] = counts.get(c.domain_kind.value, 0) + 1
        return counts


# Type imports for heuristic
