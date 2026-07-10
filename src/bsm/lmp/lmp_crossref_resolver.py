"""
LMP Cross-Reference Resolver (GAP-3a)
======================================

Converts enriched mdCATH CSV rows into ``ExternalReferences`` dicts suitable
for ``BudoDomain`` and ``BudoV3`` construction.

Enriched CSV columns consumed:
    domain_id, cath_id, cath_code, superfamily_id, funfam_number,
    pdb_id, chain_id, sequence, resolution, r_factor, r_free,
    ec_numbers (JSON array with uniprot_acc), go_terms (JSON array),
    s35_cluster … s100_cluster

Contract: §2.3.1 of BSM_INTERAGENT_GAP_CONTRACTS_2026-04-04.md
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

CrossRefDict = Dict[str, Any]
"""Resolved external references keyed by database name."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_from_csv_row(row: Dict[str, Any]) -> CrossRefDict:
    """Convert a single enriched mdCATH CSV row into a cross-reference dict.

    Args:
        row: Dict representing one CSV row (column name → raw string/value).

    Returns:
        Dict with keys: ``cath_id``, ``cath_code``, ``superfamily_id``,
        ``funfam_number``, ``pdb_id``, ``chain_id``, ``uniprot_acc``,
        ``ec_numbers``, ``go_terms``, ``resolution``, ``r_factor``,
        ``r_free``, ``clusters``.
    """
    refs: CrossRefDict = {}

    # ── CATH hierarchy ──────────────────────────────────────────────────────
    refs["cath_id"] = _str_or_none(row.get("cath_id"))
    refs["cath_code"] = _str_or_none(row.get("cath_code"))
    refs["superfamily_id"] = _str_or_none(row.get("superfamily_id"))
    refs["funfam_number"] = _str_or_none(row.get("funfam_number"))
    refs["domain_id"] = _str_or_none(row.get("domain_id"))

    # ── Structure ───────────────────────────────────────────────────────────
    refs["pdb_id"] = _str_or_none(row.get("pdb_id"))
    refs["chain_id"] = _str_or_none(row.get("chain_id"))
    refs["resolution"] = _float_or_none(row.get("resolution"))
    refs["r_factor"] = _float_or_none(row.get("r_factor"))
    refs["r_free"] = _float_or_none(row.get("r_free"))

    # ── Sequence clusters ───────────────────────────────────────────────────
    refs["clusters"] = {
        f"s{pct}": _str_or_none(row.get(f"s{pct}_cluster"))
        for pct in (35, 60, 95, 100)
    }

    # ── EC + UniProt (extracted from ec_numbers JSON array) ─────────────────
    ec_raw = row.get("ec_numbers", "[]")
    ec_list = _parse_json_list(ec_raw)
    refs["ec_numbers"] = [
        (entry.get("ec_number") or entry) if isinstance(entry, dict) else entry
        for entry in ec_list
        if isinstance(entry, (dict, str))
    ]
    # UniProt acc may be nested inside ec_numbers entries
    uniprot_accs: List[str] = []
    for entry in ec_list:
        if isinstance(entry, dict):
            acc = entry.get("uniprot_acc") or entry.get("uniprot")
            if acc:
                uniprot_accs.append(str(acc))
    refs["uniprot_acc"] = uniprot_accs[0] if uniprot_accs else None

    # ── GO terms ────────────────────────────────────────────────────────────
    go_raw = row.get("go_terms", "[]")
    go_list = _parse_json_list(go_raw)
    refs["go_terms"] = go_list  # preserve full objects with evidence codes

    return refs


def load_enriched_csv(csv_path: str | Path) -> Dict[str, CrossRefDict]:
    """Load the full enriched mdCATH CSV into a dict keyed by ``domain_id``.

    Args:
        csv_path: Path to ``mdcath_inventory_FULLY_ENRICHED_*.csv``.

    Returns:
        ``{domain_id: CrossRefDict}`` for all rows in the CSV.

    Note:
        Uses pandas for efficient CSV loading. Falls back to csv.DictReader
        if pandas is not available.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Enriched CSV not found: {csv_path}")

    try:
        import pandas as pd  # type: ignore
        df = pd.read_csv(str(path), low_memory=False)
        rows = df.to_dict(orient="records")
    except ImportError:
        import csv
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

    lookup: Dict[str, CrossRefDict] = {}
    for row in rows:
        did = str(row.get("domain_id", "")).strip()
        if did:
            lookup[did] = resolve_from_csv_row(row)  # type: ignore[arg-type]

    logger.info(
        "Loaded enriched CSV: %d domain entries from %s", len(lookup), path.name
    )
    return lookup


def compute_semantic_richness(csv_row: Dict[str, Any]) -> float:
    """Compute the [0.0, 1.0] semantic richness score for a single CSV row.

    Formula (5 components × 0.2 each):
        - cath_id present and non-empty
        - ec_numbers present and non-empty JSON array
        - go_terms present and non-empty JSON array
        - resolution < 3.0 Å (structure quality gate)
        - sequence present and non-empty

    Expected distribution: ~40% of mdCATH rows score 1.0 (all 5 components).
    """
    score = 0.0

    if _str_or_none(csv_row.get("cath_id")):
        score += 0.2
    if _str_or_none(csv_row.get("ec_numbers")) not in (None, "[]", ""):
        score += 0.2
    if _str_or_none(csv_row.get("go_terms")) not in (None, "[]", ""):
        score += 0.2
    if _float_or_none(csv_row.get("resolution")) is not None:
        try:
            if float(csv_row["resolution"]) < 3.0:  # type: ignore[arg-type]
                score += 0.2
        except (ValueError, TypeError):
            pass
    if _str_or_none(csv_row.get("sequence")):
        score += 0.2

    return round(score, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s and s.lower() not in ("nan", "none", "") else None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_json_list(raw: Any) -> List[Any]:
    """Safely parse a JSON list-string. Returns [] on any error."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw  # already parsed (pandas may do this)
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, ValueError):
        return []
