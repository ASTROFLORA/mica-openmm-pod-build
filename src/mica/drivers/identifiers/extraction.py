"""Regex-based identifier extraction from free text.

Phase 3 extraction from agentic_driver.py.
All functions are pure — no driver state required.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# ── Constants ──────────────────────────────────────────────────────────
PDB_FALSE_POSITIVES: set = {
    "1way", "2way", "3way", "4way", "1sec", "2min", "1mol", "2mol",
}

# ── UniProt / PDB regex patterns ──────────────────────────────────────
_UNIPROT_RE = re.compile(
    r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5})\b"
)
_PDB_RE = re.compile(r"\b([0-9][A-Za-z][A-Za-z0-9]{2})\b")
_UNIPROT_FULL_RE = re.compile(
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5}"
)
_PDB_FULL_RE = re.compile(r"[0-9][A-Z0-9]{3}")
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{2,15}\b")

_GENE_STOP_WORDS = frozenset({
    "ALPHAFOLD", "UNIPROT", "PDB", "PROTEIN", "STRUCTURE",
    "DOWNLOAD", "HUMAN",
})


def extract_identifiers(
    text: str,
    *,
    pdb_false_positives: set | None = None,
) -> Dict[str, List[str]]:
    """Extract UniProt accessions and PDB IDs from free text (best effort).

    Args:
        text: Raw text potentially containing identifiers.
        pdb_false_positives: Optional override set of tokens to reject as
            PDB IDs.  Defaults to :data:`PDB_FALSE_POSITIVES`.

    Returns:
        ``{"uniprot": [...], "pdb": [...]}`` with sorted, deduplicated values.
    """
    fp = pdb_false_positives if pdb_false_positives is not None else PDB_FALSE_POSITIVES
    text = text or ""
    uniprot = _UNIPROT_RE.findall(text)
    pdb = [
        m for m in _PDB_RE.findall(text)
        if m.strip().lower() not in fp
    ]
    return {
        "uniprot": sorted({u.strip().upper() for u in uniprot if u.strip()}),
        "pdb": sorted({p.strip().lower() for p in pdb if p.strip()}),
    }


def merge_identifiers(
    a: Dict[str, List[str]],
    b: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Merge two identifier dicts, deduplicating and sorting."""
    return {
        "uniprot": sorted(set((a.get("uniprot") or []) + (b.get("uniprot") or []))),
        "pdb": sorted(set((a.get("pdb") or []) + (b.get("pdb") or []))),
    }


def best_protein_hint(identifiers: Dict[str, List[str]]) -> Optional[str]:
    """Return the first available UniProt or PDB identifier, or *None*."""
    if identifiers.get("uniprot"):
        return identifiers["uniprot"][0]
    if identifiers.get("pdb"):
        return identifiers["pdb"][0]
    return None


def extract_candidate_gene_symbols(
    query: str,
    identifiers: Dict[str, List[str]],
) -> List[str]:
    """Heuristically extract candidate gene symbol tokens from text.

    Filters out known UniProt/PDB patterns and common stop-words so only
    plausible gene names remain.
    """
    seed_values: List[str] = []
    if identifiers.get("uniprot"):
        seed_values.extend(identifiers["uniprot"])
    if identifiers.get("pdb"):
        seed_values.extend(identifiers["pdb"])
    if query:
        seed_values.append(query)

    candidates: List[str] = []
    for value in seed_values:
        if not value:
            continue
        for token in _TOKEN_RE.findall(str(value)):
            token_upper = token.strip().upper()
            if not token_upper:
                continue
            # Skip known identifier patterns
            if _UNIPROT_FULL_RE.fullmatch(token_upper):
                continue
            if _PDB_FULL_RE.fullmatch(token_upper):
                continue
            if token_upper in _GENE_STOP_WORDS:
                continue
            if token_upper not in candidates:
                candidates.append(token_upper)
    return candidates
