"""
Protein Identity Resolver (Stage 1 of Unified Pipeline)
=======================================================

Resolves any protein identifier format (UniProt accession, STRING ENSP ID,
gene name) to a canonical identity dict with all known aliases. This bridges
the fragmentation gap where different Zilliz collections use different ID
schemes.

Collection→ID mapping:
    - protein_sequences_embeddings: protein_id = "9606.ENSP00000261250", gene, uniprot
    - protein_networks_embeddings:  protein_id = "9606.ENSP00000261250", gene, uniprot
    - swissprot_esmc_v2:            uniprot_accession = "Q9NQ89", gene = "FERRY3"
    - protein_af2_rag_v1:           uniprot_id = "Q9NQ89", gene = "C12orf4"
    - protein_multimodal_rag_v1:    uniprot_id + canonical_name (ENSP)
    - dctdomain_embeddings:         protein_id = "9606.ENSP00000261250", gene, uniprot

Contract: §2 Stage 1 of PIPELINE_UNIFICATION_STRATEGY_2026-04-08.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Regex patterns for identifier format detection
_UNIPROT_RE = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$"
)
_ENSP_RE = re.compile(r"^(9606\.)?ENSP\d{11}$", re.IGNORECASE)
_STRING_FULL_RE = re.compile(r"^\d+\.ENSP\d{11}$")


@dataclass
class ProteinIdentity:
    """Canonical protein identity with all known aliases."""

    uniprot: Optional[str] = None
    ensp: Optional[str] = None  # Full STRING ID: "9606.ENSP00000261250"
    gene: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    resolved_from: str = ""  # Original input that was resolved

    @property
    def is_resolved(self) -> bool:
        """True if at least two identifier spaces are populated."""
        filled = sum(1 for v in [self.uniprot, self.ensp, self.gene] if v)
        return filled >= 2

    def all_query_ids(self) -> List[str]:
        """Return all identifier strings to use when querying collections."""
        ids: List[str] = []
        if self.uniprot:
            ids.append(self.uniprot)
        if self.ensp:
            ids.append(self.ensp)
        if self.gene:
            ids.append(self.gene)
        ids.extend(self.aliases)
        return ids

    def to_dict(self) -> Dict[str, object]:
        return {
            "uniprot": self.uniprot,
            "ensp": self.ensp,
            "gene": self.gene,
            "aliases": self.aliases,
            "resolved_from": self.resolved_from,
            "is_resolved": self.is_resolved,
        }


def detect_id_type(identifier: str) -> str:
    """Detect the format of a protein identifier.

    Returns one of: 'uniprot', 'ensp', 'gene', 'unknown'.
    """
    identifier = identifier.strip()
    if _STRING_FULL_RE.match(identifier):
        return "ensp"
    if _ENSP_RE.match(identifier):
        return "ensp"
    if _UNIPROT_RE.match(identifier):
        return "uniprot"
    # If alphanumeric and short, likely a gene name
    if re.match(r"^[A-Za-z][A-Za-z0-9_-]{1,20}$", identifier):
        return "gene"
    return "unknown"


def resolve_identity_from_hit(
    hit: Dict[str, object],
    original_input: str,
) -> ProteinIdentity:
    """Extract a ProteinIdentity from a Milvus collection hit dict.

    Different collections store identifiers under different field names.
    This function normalises them into a single ProteinIdentity.
    """
    identity = ProteinIdentity(resolved_from=original_input)

    # UniProt accession
    for key in ("uniprot_accession", "uniprot_id", "uniprot", "UniProt"):
        val = hit.get(key)
        if val and str(val).strip() and str(val).strip().lower() not in ("nan", "none", ""):
            identity.uniprot = str(val).strip()
            break

    # STRING / ENSP ID
    for key in ("protein_id", "canonical_name", "string_id", "ensp"):
        val = hit.get(key)
        if val and str(val).strip():
            s = str(val).strip()
            if _ENSP_RE.match(s) or _STRING_FULL_RE.match(s):
                identity.ensp = s
                break

    # Gene name
    for key in ("gene", "gene_name", "preferred_name"):
        val = hit.get(key)
        if val and str(val).strip() and str(val).strip().lower() not in ("nan", "none", ""):
            identity.gene = str(val).strip()
            break

    # Collect aliases
    seen = {identity.uniprot, identity.ensp, identity.gene}
    for key in ("protein_id", "canonical_name", "uniprot_id", "uniprot_accession",
                "gene", "gene_name", "preferred_name", "string_id"):
        val = hit.get(key)
        if val and str(val).strip() and str(val).strip() not in seen:
            s = str(val).strip()
            if s.lower() not in ("nan", "none", ""):
                identity.aliases.append(s)
                seen.add(s)

    return identity


def merge_identities(a: ProteinIdentity, b: ProteinIdentity) -> ProteinIdentity:
    """Merge two ProteinIdentity objects, preferring non-None values."""
    merged = ProteinIdentity(resolved_from=a.resolved_from or b.resolved_from)
    merged.uniprot = a.uniprot or b.uniprot
    merged.ensp = a.ensp or b.ensp
    merged.gene = a.gene or b.gene
    seen = {merged.uniprot, merged.ensp, merged.gene}
    for alias in a.aliases + b.aliases:
        if alias and alias not in seen:
            merged.aliases.append(alias)
            seen.add(alias)
    return merged
