"""domain_ontology.py — Unified Domain Classification for BSM / LMP / BudoV3

Single source of truth for domain semantic typing across the whole system.

Previously, domain classification was scattered across three independent
string-matching sites:

  - generator_v4._infer_states()         → "kinase" in name.lower()
  - structural_fusion._parse_domain_map() → "disordered" in name_lc
  - (planned) budo_parser.py              → would have added a third copy

This module replaces all of them with one function: classify_domain().

The LMP XML generator emits ``class=`` on every <lmp:Domain> element.
Consumers (structural_fusion, budo_parser, embeddings pipeline) read the
``class=`` attribute directly — no text inference required.

Usage
-----
    from bsm.schemas.domain_ontology import DomainClass, classify_domain

    dc = classify_domain("Compositional bias", "Poly-Ser region")
    assert dc == DomainClass.DISORDERED

    dc = classify_domain("Domain", "Protein kinase", interpro_type="domain")
    assert dc == DomainClass.STRUCTURAL
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class DomainClass(str, Enum):
    """Semantic class of a protein domain / annotated region.

    Stored as the ``class=`` XML attribute on every <lmp:Domain> element
    and as the ``domain_class`` field on BudoDomain.
    """

    STRUCTURAL    = "structural"     # Pfam fold, CATH superfamily, InterPro structural
    FUNCTIONAL    = "functional"     # active site, catalytic site, binding pocket
    DISORDERED    = "disordered"     # IDR, compositional bias, low-complexity
    REPEAT        = "repeat"         # ARM, WD40, TPR, HEAT, ankyrin, tandem repeat
    COILED_COIL   = "coiled_coil"    # coiled-coil segment
    ZINC_FINGER   = "zinc_finger"    # zinc-finger motif
    TRANSMEMBRANE = "transmembrane"  # TM helix, intramembrane
    SIGNAL        = "signal"         # signal peptide, transit peptide, propeptide
    GLOBAL        = "global"         # synthetic whole-protein container (GlobalFeatures)
    UNKNOWN       = "unknown"        # fallback — no confident assignment


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

# UniProt feature types that map unambiguously (no name inspection needed)
_FT_MAP: dict[str, DomainClass] = {
    "compositional bias":   DomainClass.DISORDERED,
    "compositional_bias":   DomainClass.DISORDERED,
    "coiled coil":          DomainClass.COILED_COIL,
    "coiled_coil":          DomainClass.COILED_COIL,
    "zinc finger":          DomainClass.ZINC_FINGER,
    "zinc_finger":          DomainClass.ZINC_FINGER,
    "repeat":               DomainClass.REPEAT,
    "tandem repeat":        DomainClass.REPEAT,
    "signal peptide":       DomainClass.SIGNAL,
    "transit peptide":      DomainClass.SIGNAL,
    "propeptide":           DomainClass.SIGNAL,
    "transmembrane":        DomainClass.TRANSMEMBRANE,
    "intramembrane":        DomainClass.TRANSMEMBRANE,
    "active site":          DomainClass.FUNCTIONAL,
    "binding site":         DomainClass.FUNCTIONAL,
    "metal binding":        DomainClass.FUNCTIONAL,
    "nucleotide binding":   DomainClass.FUNCTIONAL,
    "dna binding":          DomainClass.FUNCTIONAL,
    "global":               DomainClass.GLOBAL,
    # InterPro/PDB source labels (from PDBe-SIFTS integration)
    "interpro":             DomainClass.STRUCTURAL,
    "pfam":                 DomainClass.STRUCTURAL,
    "cath":                 DomainClass.STRUCTURAL,
    "scop":                 DomainClass.STRUCTURAL,
    "smart":                DomainClass.STRUCTURAL,
    "prosite":              DomainClass.STRUCTURAL,
    "superfamily":          DomainClass.STRUCTURAL,
    "homologous_superfamily": DomainClass.STRUCTURAL,
    "domain":               DomainClass.STRUCTURAL,
    "family":               DomainClass.STRUCTURAL,
}

# Name-substring tokens for secondary classification (only when FT lookup fails)
_DISORDERED_TOKENS = frozenset([
    "disordered", "intrinsically disordered", "idr",
    "low-complexity", "low complexity", "compositionally biased",
])
_FUNCTIONAL_TOKENS = frozenset([
    "active site", "catalytic", "binding site", "binding pocket",
    "nucleotide-bind", "atp-bind", "gtp-bind", "metal-bind",
])
_SIGNAL_TOKENS = frozenset(["signal peptide", "signal anchor"])
_COIL_TOKENS   = frozenset(["coiled coil", "coiled-coil"])
_TM_TOKENS     = frozenset(["transmembrane", "membrane span"])
_REPEAT_TOKENS = frozenset([
    "wd repeat", "arm repeat", "tpr repeat", "heat repeat",
    "ankyrin repeat", "lrr repeat", "leucine-rich repeat",
])

# InterPro type strings → DomainClass
_IPR_TYPE_MAP: dict[str, DomainClass] = {
    "site":                DomainClass.FUNCTIONAL,
    "active_site":         DomainClass.FUNCTIONAL,
    "binding_site":        DomainClass.FUNCTIONAL,
    "conserved_site":      DomainClass.FUNCTIONAL,
    "ptm":                 DomainClass.FUNCTIONAL,
    "repeat":              DomainClass.REPEAT,
    "domain":              DomainClass.STRUCTURAL,
    "family":              DomainClass.STRUCTURAL,
    "homologous_superfamily": DomainClass.STRUCTURAL,
}


def classify_domain(
    uniprot_feature_type: str,
    domain_name: str = "",
    interpro_type: Optional[str] = None,
) -> DomainClass:
    """Return the DomainClass for a domain / annotated region.

    Parameters
    ----------
    uniprot_feature_type:
        The UniProt feature ``type`` string, or a PDBe-SIFTS source label
        ("InterPro", "Pfam", "CATH"), or a synthetic label ("global").
        Case-insensitive.
    domain_name:
        Free-text domain name.  Used as secondary signal only when the
        feature type alone is ambiguous.  Case-insensitive.
    interpro_type:
        InterPro entry type string from the InterPro API response metadata
        ("domain", "family", "site", "repeat", etc.).  Optional override.

    Returns
    -------
    DomainClass
        The semantic class.  Never raises; returns UNKNOWN on ambiguity.
    """
    ft = uniprot_feature_type.strip().lower()
    name_lc = domain_name.strip().lower()

    # 1. Unambiguous feature type → direct return
    if ft in _FT_MAP:
        return _FT_MAP[ft]

    # 2. Name-based tokens (secondary, only when FT is generic like "region")
    for tok in _DISORDERED_TOKENS:
        if tok in name_lc:
            return DomainClass.DISORDERED
    for tok in _FUNCTIONAL_TOKENS:
        if tok in name_lc:
            return DomainClass.FUNCTIONAL
    for tok in _SIGNAL_TOKENS:
        if tok in name_lc:
            return DomainClass.SIGNAL
    for tok in _COIL_TOKENS:
        if tok in name_lc:
            return DomainClass.COILED_COIL
    for tok in _TM_TOKENS:
        if tok in name_lc:
            return DomainClass.TRANSMEMBRANE
    for tok in _REPEAT_TOKENS:
        if tok in name_lc:
            return DomainClass.REPEAT

    # 3. InterPro entry type (authoritative when available)
    if interpro_type:
        ipr_t = interpro_type.strip().lower()
        if ipr_t in _IPR_TYPE_MAP:
            return _IPR_TYPE_MAP[ipr_t]

    # 4. Generic structural FT that didn't match any special token
    if ft in ("region", "motif"):
        # "Region" in UniProt can be structural or disordered; name heuristic
        # already ran above and found nothing → treat as structural
        return DomainClass.STRUCTURAL

    return DomainClass.UNKNOWN
