"""structural_fusion.py — Residue-to-Domain Coordinate Validator

Implements the 'SQL CONSTRAINT for protein geometry' concept:
before any residue-level interpretation is generated (e.g.,
"target_residues_in_disordered_region"), the claimed feature MUST
be consistent with:

  1. Actual domain membership — from Domain annotations in the LMP v4 XML
     (Geometry/Chain/Domain elements, which encode UniProt Region/Domain
     features and PDBe-SIFTS InterPro/Pfam/CATH boundaries).
  2. Per-residue pLDDT confidence — from AlphaFoldModel/ConfidencePerResidue
     (aligned to UniProt sequence coordinates).

Key constraint rules
--------------------
DISORDERED interpretations (e.g., "disordered_region", "idr",
"target_residues_in_disordered_region"):
  - The residue MUST fall within a Domain element whose name contains
    "Disordered" (case-insensitive) or whose type is compositional_bias.
  - The residue pLDDT MUST be < 70 (i.e., class "low" or "very_low").
  - Violation of EITHER rule → REJECTED with reason string.

STRUCTURED interpretations (e.g., "activation_loop", "catalytic_residue",
"atp_binding", "alpha_helix"):
  - The residue MUST NOT be inside a Disordered-annotated feature.
  - The residue pLDDT MUST be >= 50 (otherwise "very_low" = almost certainly
    unstructured).
  - Violation of EITHER rule → REJECTED.

UNKNOWN class: no hard constraint; ValidationResult.valid=True with a warning.

Motivation
----------
The factual error that prompted this module: in the proteome_kg_annotator
architecture document the YopJ/MAP2K2 example stated

    "S222_domain": "activation_loop",
    "interpretation": "target_residues_in_disordered_region"

S222 (position 222, MAP2K2/P36507) has pLDDT=79.4 (class: confident) and is
within the Protein kinase domain (72–369), NOT within the Disordered feature
(286–310).  The interpretation was internally contradictory.  This module
enforces the constraint programmatically so such contradictions cannot be
silently committed to XML.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional

from bsm.schemas.domain_ontology import DomainClass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LMP_NS: str = "http://ai-university.edu/lmp/v4.0"

# Tokens whose presence in an interpretation string implies disorder.
_DISORDER_TOKENS: FrozenSet[str] = frozenset(
    {
        "disordered",
        "disordered_region",
        "target_residues_in_disordered_region",
        "intrinsically_disordered",
        "idr",
        "flexible_loop",
        "unstructured",
        "low_complexity",
        "compositional_bias",
    }
)

# Tokens whose presence implies an ordered structural feature.
_STRUCTURED_TOKENS: FrozenSet[str] = frozenset(
    {
        "activation_loop",
        "catalytic_residue",
        "atp_binding",
        "hydrophobic_core",
        "alpha_helix",
        "beta_strand",
        "active_site",
        "binding_site",
        "nucleotide_binding",
        "metal_binding",
        "p_loop",
        "glycine_rich_loop",
        "hinge_region",
        "dfg_motif",
    }
)

# pLDDT threshold below which a residue may legitimately be called disordered.
_DISORDER_PLDDT_THRESHOLD: float = 70.0

# pLDDT threshold below which a "structured" interpretation is suspicious.
_STRUCTURED_PLDDT_MIN: float = 50.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class InterpretationClass(str, Enum):
    """Semantic class of a residue-level interpretation string."""

    DISORDERED = "disordered"
    STRUCTURED = "structured"
    UNKNOWN = "unknown"


@dataclass
class DomainInterval:
    """A named feature with sequence boundaries from a LMP XML Domain element."""

    name: str
    feature_type: str  # e.g. "domain", "region", "interpro", "pfam"
    start: int
    end: int
    is_disordered: bool = False

    def contains(self, residue_id: int) -> bool:
        return self.start <= residue_id <= self.end

    def span_label(self) -> str:
        return f"{self.name} ({self.start}–{self.end})"


@dataclass
class ResidueRecord:
    """Per-residue data parsed from AlphaFoldModel/ConfidencePerResidue."""

    residue_id: int
    residue_name: str
    plddt: Optional[float]
    confidence_class: Optional[str]  # very_high / confident / low / very_low


@dataclass
class ValidationResult:
    """Outcome of a single residue-interpretation constraint check."""

    valid: bool
    residue_id: int
    claimed_interpretation: str
    interpretation_class: InterpretationClass
    reason: str
    actual_domains: List[str] = field(default_factory=list)
    actual_plddt: Optional[float] = None
    actual_confidence_class: Optional[str] = None

    def summary(self) -> str:
        status = "OK      " if self.valid else "REJECTED"
        plddt_str = f"{self.actual_plddt:.1f}" if self.actual_plddt is not None else "N/A"
        conf = self.actual_confidence_class or "N/A"
        return (
            f"[{status}] residue={self.residue_id}"
            f" pLDDT={plddt_str} ({conf})"
            f" interpretation={self.claimed_interpretation!r}"
            f" | {self.reason}"
        )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ResidueCoordinateValidator:
    """Validates residue-level interpretations against domain and pLDDT data
    from a parsed LMP v4 XML tree.

    Usage::

        validator = ResidueCoordinateValidator.from_file("P36507_Phosphorylated_Active.xml")

        r = validator.validate_interpretation(222, "target_residues_in_disordered_region")
        print(r.summary())
        # [REJECTED] residue=222 pLDDT=79.4 (confident) interpretation='target_residues_in_disordered_region'
        # | residue 222 not in any Disordered annotation (within: ['Protein kinase']); pLDDT=79.4 (confident) >= 70 — residue is ordered

        r2 = validator.validate_interpretation(222, "activation_loop")
        print(r2.summary())
        # [OK      ] residue=222 pLDDT=79.4 (confident) interpretation='activation_loop'
        # | pLDDT=79.4 (confident); in domains: ['Protein kinase']; not disordered
    """

    def __init__(self, xml_root: ET.Element) -> None:
        self._domains: List[DomainInterval] = _parse_domain_map(xml_root)
        self._plddt_map: Dict[int, ResidueRecord] = _parse_plddt_map(xml_root)

    @classmethod
    def from_file(cls, xml_path: str) -> "ResidueCoordinateValidator":
        """Construct from a LMP v4 XML file path."""
        tree = ET.parse(xml_path)
        return cls(tree.getroot())

    @classmethod
    def from_string(cls, xml_content: str) -> "ResidueCoordinateValidator":
        """Construct from XML string content (useful for testing)."""
        root = ET.fromstring(xml_content)
        return cls(root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_interpretation(
        self,
        residue_id: int,
        claimed_interpretation: str,
    ) -> ValidationResult:
        """Check whether *claimed_interpretation* is consistent with the
        actual domain membership and pLDDT for *residue_id*.

        Returns a :class:`ValidationResult` with ``valid=True`` if all
        constraints pass, otherwise ``valid=False`` with an explanatory
        ``reason`` string.
        """
        iclass = _classify_interpretation(claimed_interpretation)
        record = self._plddt_map.get(residue_id)
        domains_hit = [d for d in self._domains if d.contains(residue_id)]
        domain_names = [d.name for d in domains_hit]
        disordered_hit = [d for d in domains_hit if d.is_disordered]

        plddt = record.plddt if record else None
        conf_class = record.confidence_class if record else None

        if iclass == InterpretationClass.DISORDERED:
            return _check_disordered(
                residue_id, claimed_interpretation, plddt, conf_class,
                domain_names, disordered_hit,
            )
        if iclass == InterpretationClass.STRUCTURED:
            return _check_structured(
                residue_id, claimed_interpretation, plddt, conf_class,
                domain_names, disordered_hit,
            )
        # Unknown interpretation — soft pass, no constraint
        return ValidationResult(
            valid=True,
            residue_id=residue_id,
            claimed_interpretation=claimed_interpretation,
            interpretation_class=InterpretationClass.UNKNOWN,
            reason="Interpretation class unknown; no geometric constraint applied",
            actual_domains=domain_names,
            actual_plddt=plddt,
            actual_confidence_class=conf_class,
        )

    def validate_batch(
        self,
        pairs: List[tuple[int, str]],
    ) -> List[ValidationResult]:
        """Validate multiple (residue_id, interpretation) pairs.

        Returns one :class:`ValidationResult` per input pair, in order.
        """
        return [
            self.validate_interpretation(rid, interp)
            for rid, interp in pairs
        ]

    def find_residue_domains(self, residue_id: int) -> List[DomainInterval]:
        """Return all DomainInterval objects that contain *residue_id*."""
        return [d for d in self._domains if d.contains(residue_id)]

    def is_in_disordered_region(self, residue_id: int) -> bool:
        """True if any overlapping domain annotation is flagged as disordered."""
        return any(d.is_disordered for d in self._domains if d.contains(residue_id))

    def get_plddt(self, residue_id: int) -> Optional[float]:
        """Return the pLDDT for *residue_id*, or None if not available."""
        rec = self._plddt_map.get(residue_id)
        return rec.plddt if rec else None

    def get_record(self, residue_id: int) -> Optional[ResidueRecord]:
        """Return the full :class:`ResidueRecord` for *residue_id*."""
        return self._plddt_map.get(residue_id)

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def domain_count(self) -> int:
        return len(self._domains)

    @property
    def residue_count(self) -> int:
        return len(self._plddt_map)

    def disordered_ranges(self) -> List[DomainInterval]:
        """Return all domain intervals marked as disordered."""
        return [d for d in self._domains if d.is_disordered]


# ---------------------------------------------------------------------------
# Module-level parser helpers (reusable without instantiating the validator)
# ---------------------------------------------------------------------------


def _lmp_tag(local: str) -> str:
    return f"{{{LMP_NS}}}{local}"


def _parse_domain_map(root: ET.Element) -> List[DomainInterval]:
    """Extract all Domain elements from the LMP XML into DomainInterval objects.

    Disordered annotation detection:
    - name contains "disordered" (case-insensitive)
    - type is "compositional_bias" or "compositional bias"
    - name contains "low complexity"
    """
    intervals: List[DomainInterval] = []
    for dom_elem in root.iter(_lmp_tag("Domain")):
        name = dom_elem.get("name", "")
        feature_type = dom_elem.get("type", "domain").lower()
        try:
            start = int(dom_elem.get("start", 0))
            end = int(dom_elem.get("end", 0))
        except (ValueError, TypeError):
            continue
        if start <= 0 or end <= 0 or end < start:
            continue

        # Prefer the pre-computed class= attribute (set by generator_v4 via domain_ontology).
        # Fall back to name/type string-matching for XMLs generated before domain_ontology.
        dc_str = dom_elem.get("class", "")
        if dc_str:
            is_disordered = dc_str == DomainClass.DISORDERED.value
        else:
            name_lc = name.lower()
            is_disordered = (
                "disordered" in name_lc
                or "low complexity" in name_lc
                or feature_type in ("compositional_bias", "compositional bias")
            )
        intervals.append(
            DomainInterval(
                name=name,
                feature_type=feature_type,
                start=start,
                end=end,
                is_disordered=is_disordered,
            )
        )
    return intervals


def _parse_plddt_map(root: ET.Element) -> Dict[int, ResidueRecord]:
    """Extract per-residue pLDDT from AlphaFoldModel/ConfidencePerResidue.

    Uses `id` attribute (1-based UniProt position) and `pLDDT` attribute.
    Interface/Contact Residue elements (which use `resnum`, not `id`) are
    naturally excluded because their `id` attribute is absent → rid=0 → skipped.
    """
    records: Dict[int, ResidueRecord] = {}
    for res_elem in root.iter(_lmp_tag("Residue")):
        # Only process ConfidencePerResidue entries (they carry pLDDT)
        plddt_str = res_elem.get("pLDDT", "")
        if not plddt_str:
            continue
        try:
            rid = int(res_elem.get("id", 0))
        except (ValueError, TypeError):
            continue
        if rid <= 0:
            continue
        try:
            plddt = float(plddt_str)
        except ValueError:
            plddt = None
        records[rid] = ResidueRecord(
            residue_id=rid,
            residue_name=res_elem.get("name", ""),
            plddt=plddt,
            confidence_class=res_elem.get("confidence_class") or None,
        )
    return records


def _classify_interpretation(interpretation: str) -> InterpretationClass:
    norm = interpretation.lower().replace(" ", "_").replace("-", "_")
    if any(tok in norm for tok in _DISORDER_TOKENS):
        return InterpretationClass.DISORDERED
    if any(tok in norm for tok in _STRUCTURED_TOKENS):
        return InterpretationClass.STRUCTURED
    return InterpretationClass.UNKNOWN


def _check_disordered(
    residue_id: int,
    interp: str,
    plddt: Optional[float],
    conf_class: Optional[str],
    domain_names: List[str],
    disordered_hit: List[DomainInterval],
) -> ValidationResult:
    reasons: List[str] = []

    # Rule 1: must be inside a Disordered-annotated feature.
    if not disordered_hit:
        domain_str = ", ".join(domain_names) if domain_names else "no domain"
        reasons.append(
            f"residue {residue_id} not in any Disordered annotation"
            f" (within: [{domain_str}])"
        )

    # Rule 2: pLDDT must be < 70 (otherwise the residue is ordered).
    if plddt is not None and plddt >= _DISORDER_PLDDT_THRESHOLD:
        reasons.append(
            f"pLDDT={plddt:.1f} ({conf_class}) >= {_DISORDER_PLDDT_THRESHOLD:.0f}"
            f" — residue is ordered"
        )

    if reasons:
        return ValidationResult(
            valid=False,
            residue_id=residue_id,
            claimed_interpretation=interp,
            interpretation_class=InterpretationClass.DISORDERED,
            reason="; ".join(reasons),
            actual_domains=domain_names,
            actual_plddt=plddt,
            actual_confidence_class=conf_class,
        )

    span = f"{disordered_hit[0].start}–{disordered_hit[0].end}"
    return ValidationResult(
        valid=True,
        residue_id=residue_id,
        claimed_interpretation=interp,
        interpretation_class=InterpretationClass.DISORDERED,
        reason=f"residue in Disordered region {span}; pLDDT={plddt:.1f} ({conf_class})",
        actual_domains=domain_names,
        actual_plddt=plddt,
        actual_confidence_class=conf_class,
    )


def _check_structured(
    residue_id: int,
    interp: str,
    plddt: Optional[float],
    conf_class: Optional[str],
    domain_names: List[str],
    disordered_hit: List[DomainInterval],
) -> ValidationResult:
    reasons: List[str] = []

    # Rule 1: must NOT be inside a Disordered feature.
    if disordered_hit:
        d = disordered_hit[0]
        reasons.append(
            f"residue {residue_id} is within Disordered annotation"
            f" ({d.name} {d.start}–{d.end})"
        )

    # Rule 2: pLDDT must be >= 50.
    if plddt is not None and plddt < _STRUCTURED_PLDDT_MIN:
        reasons.append(
            f"pLDDT={plddt:.1f} ({conf_class}) < {_STRUCTURED_PLDDT_MIN:.0f}"
            f" — residue is likely unstructured"
        )

    if reasons:
        return ValidationResult(
            valid=False,
            residue_id=residue_id,
            claimed_interpretation=interp,
            interpretation_class=InterpretationClass.STRUCTURED,
            reason="; ".join(reasons),
            actual_domains=domain_names,
            actual_plddt=plddt,
            actual_confidence_class=conf_class,
        )

    dom_str = ", ".join(domain_names) if domain_names else "none"
    return ValidationResult(
        valid=True,
        residue_id=residue_id,
        claimed_interpretation=interp,
        interpretation_class=InterpretationClass.STRUCTURED,
        reason=f"pLDDT={plddt:.1f} ({conf_class}); in domains: [{dom_str}]; not disordered",
        actual_domains=domain_names,
        actual_plddt=plddt,
        actual_confidence_class=conf_class,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "LMP_NS",
    "InterpretationClass",
    "DomainInterval",
    "ResidueRecord",
    "ValidationResult",
    "ResidueCoordinateValidator",
]


# ---------------------------------------------------------------------------
# Standalone demo / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    xml_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else (
            r"C:\Users\busta\Downloads\MICA"
            r"\astroflora-core-feature-spectra-worker-integration-1"
            r"\output\kinome_pilot\P36507_Phosphorylated_Active.xml"
        )
    )

    print(f"Loading: {xml_path}\n")
    v = ResidueCoordinateValidator.from_file(xml_path)
    print(f"Domains loaded  : {v.domain_count}")
    print(f"Residues loaded : {v.residue_count}")
    print(f"Disordered ranges: {[d.span_label() for d in v.disordered_ranges()]}")
    print()

    cases = [
        # The exact claim from the architecture document (must REJECT both)
        (222, "target_residues_in_disordered_region"),
        (226, "target_residues_in_disordered_region"),
        # Correct interpretation for these residues (must PASS)
        (222, "activation_loop"),
        (226, "activation_loop"),
        # A residue actually in the disordered region (290, must PASS as disordered)
        (290, "disordered_region"),
        # A very-low pLDDT residue with structured claim (should REJECT if pLDDT<50)
        (227, "activation_loop"),
    ]

    print("=== Constraint check results ===\n")
    for rid, interp in cases:
        r = v.validate_interpretation(rid, interp)
        print(r.summary())
