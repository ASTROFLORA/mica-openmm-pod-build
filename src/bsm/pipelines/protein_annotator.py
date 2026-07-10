"""
Protein Annotator Pipeline (GAP-3c)
=====================================

Enriches a ``BudoV3`` object with domain-level structural and functional
annotations sourced from the CATH enriched CSV inventory.

Pipeline:
    1. Load enriched CSV via ``load_enriched_csv()``
    2. For each domain in ``budo.domains``:
       a. Lookup ``domain_id`` → ``CrossRefDict`` via ``lmp_crossref_resolver``
       b. Populate all GAP-1 CATH/EC/GO fields on the ``BudoDomain``
       c. Compute ``semantic_richness`` score
    3. Return the annotated ``BudoV3`` (mutated in-place or as a copy)

Contract: §2.3.3 of BSM_INTERAGENT_GAP_CONTRACTS_2026-04-04.md
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from ..schemas.budo_v3 import BudoV3, BudoDomain
from ..lmp.lmp_crossref_resolver import (
    CrossRefDict,
    compute_semantic_richness,
    load_enriched_csv,
    resolve_from_csv_row,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default CSV path — override via env or constructor arg
# ---------------------------------------------------------------------------

_DEFAULT_ENRICHED_CSV = (
    Path(__file__).parent.parent.parent.parent
    / "EMBEDDINGORDERINGPLAN"
    / "TEAM_AI_COLLABORATION"
    / "AIUNIVERSITY"
    / "RESEARCH_LABS"
    / "DR_YUANG_CHENG_EMBEDDING"
    / "INVESTIGATION_LINES"
    / "CATHMAPING"
    / "cath_mapping_outputs"
    / "mdcath_inventory_FULLY_ENRICHED_20251022_015759.csv"
)


# ---------------------------------------------------------------------------
# Annotator class
# ---------------------------------------------------------------------------

class ProteinAnnotator:
    """Enriches BudoV3 objects with CATH/EC/GO cross-references.

    Args:
        enriched_csv_path: Path to the fully enriched CATH CSV.  Defaults to
            the canonical inventory file in the EMBEDDINGORDERINGPLAN tree.
        lazy: If True (default), the CSV is loaded on first use rather than at
            construction time.

    Example::

        annotator = ProteinAnnotator()
        annotated_budo = annotator.annotate(budo_object)
        print(annotated_budo.domains[0].semantic_richness)
    """

    def __init__(
        self,
        enriched_csv_path: Optional[Path] = None,
        lazy: bool = True,
    ) -> None:
        self._csv_path: Path = Path(enriched_csv_path or _DEFAULT_ENRICHED_CSV)
        self._lookup: Optional[Dict[str, CrossRefDict]] = None
        self._raw_rows: Optional[Dict[str, Dict]] = None

        if not lazy:
            self._ensure_loaded()

    # -----------------------------------------------------------------------
    # Public
    # -----------------------------------------------------------------------

    def annotate(self, budo: BudoV3) -> BudoV3:
        """Enrich all domains of *budo* with CATH/EC/GO cross-references.

        The ``BudoV3`` object is mutated **in-place** and also returned for
        convenience (to allow chaining).

        Args:
            budo: A ``BudoV3`` instance whose ``domains`` list will be enriched.

        Returns:
            The same ``BudoV3`` instance with enriched ``domains``.
        """
        self._ensure_loaded()

        enriched_count = 0
        for domain in budo.domains:
            domain_id = domain.domain_id
            if domain_id and domain_id in self._lookup:
                ref = self._lookup[domain_id]
                raw_row = self._raw_rows.get(domain_id, {})
                self._apply_crossref(domain, ref, raw_row)
                enriched_count += 1
            else:
                logger.debug(
                    "ProteinAnnotator: domain_id %r not found in enriched CSV",
                    domain_id,
                )

        logger.info(
            "ProteinAnnotator.annotate: budo=%s  domains=%d  enriched=%d",
            budo.budoId,
            len(budo.domains),
            enriched_count,
        )
        return budo

    def annotate_batch(self, budos: List[BudoV3]) -> List[BudoV3]:
        """Enrich a list of BudoV3 objects.

        Args:
            budos: List of BudoV3 instances.

        Returns:
            Same list, each element mutated in-place.
        """
        return [self.annotate(b) for b in budos]

    def annotate_domain(
        self,
        domain: BudoDomain,
        domain_id: Optional[str] = None,
    ) -> BudoDomain:
        """Enriches a single ``BudoDomain`` standalone (useful for testing).

        Args:
            domain: The BudoDomain to enrich.
            domain_id: Override key to look up in the CSV (defaults to
                ``domain.domain_id``).

        Returns:
            The mutated BudoDomain.
        """
        self._ensure_loaded()
        key = domain_id or domain.domain_id
        if key and key in self._lookup:
            ref = self._lookup[key]
            raw_row = self._raw_rows.get(key, {})
            self._apply_crossref(domain, ref, raw_row)
        else:
            logger.warning(
                "ProteinAnnotator.annotate_domain: domain_id %r not in CSV", key
            )
        return domain

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._lookup is not None:
            return

        if not self._csv_path.exists():
            logger.warning(
                "ProteinAnnotator: enriched CSV not found at %s — "
                "annotation will be no-op",
                self._csv_path,
            )
            self._lookup = {}
            self._raw_rows = {}
            return

        logger.info("ProteinAnnotator: loading enriched CSV from %s", self._csv_path)
        self._lookup = load_enriched_csv(str(self._csv_path))

        # Also keep raw rows for semantic_richness scoring
        self._raw_rows = {}
        try:
            import csv as _csv

            with open(self._csv_path, newline="", encoding="utf-8") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    row_id = row.get("domain_id", "").strip()
                    if row_id:
                        self._raw_rows[row_id] = dict(row)
        except Exception as exc:
            logger.warning(
                "ProteinAnnotator: failed to load raw rows for richness scoring: %s", exc
            )

        logger.info(
            "ProteinAnnotator: loaded %d domain cross-reference records",
            len(self._lookup),
        )

    def _apply_crossref(
        self,
        domain: BudoDomain,
        ref: CrossRefDict,
        raw_row: Dict,
    ) -> None:
        """Write cross-reference fields onto the domain (in-place)."""
        # CATH hierarchy
        if ref.get("cath_id") and not domain.cath_id:
            object.__setattr__(domain, "cath_id", ref["cath_id"])
        if ref.get("cath_code") and not domain.cath_code:
            object.__setattr__(domain, "cath_code", ref["cath_code"])
        if ref.get("superfamily_id") and not domain.superfamily_id:
            object.__setattr__(domain, "superfamily_id", ref["superfamily_id"])
        if ref.get("funfam_number") is not None and domain.funfam_number is None:
            object.__setattr__(domain, "funfam_number", ref["funfam_number"])

        # Catalytic residues from EC numbers (presence signals catalytic function)
        ec_numbers: List[str] = ref.get("ec_numbers") or []
        if ec_numbers and not domain.catalytic_residues:
            # Store EC numbers as catalytic metadata tokens
            object.__setattr__(domain, "catalytic_residues", ec_numbers)

        # Semantic richness score
        richness = compute_semantic_richness(raw_row)
        object.__setattr__(domain, "semantic_richness", richness)

        # Log GO term presence at DEBUG
        go_terms = ref.get("go_terms") or []
        if go_terms:
            logger.debug(
                "domain=%s  ec=%s  go_terms=%d  richness=%.1f",
                domain.domain_id,
                ec_numbers[:2],
                len(go_terms),
                richness,
            )


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def annotate_protein(
    budo: BudoV3,
    enriched_csv_path: Optional[Path] = None,
) -> BudoV3:
    """One-shot annotation — creates a disposable ProteinAnnotator.

    Prefer instantiating ``ProteinAnnotator`` directly when processing many
    proteins to avoid reloading the CSV on every call.

    Args:
        budo: BudoV3 to annotate.
        enriched_csv_path: Optional override for CSV path.

    Returns:
        Annotated BudoV3 (mutated in-place).
    """
    return ProteinAnnotator(enriched_csv_path=enriched_csv_path).annotate(budo)
