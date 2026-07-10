"""Phase 1 dataset assembly pulling embeddings directly from Milvus.

This module reproduces the Phase 1 WNK control cohort packaging workflow but
reads vectors and metadata from the `protein_sequences_embeddings_v2`
collection instead of the legacy STRING HDF5 store.  The business rules are
kept aligned with the NJ3PHASE specification:

- Restrict to human SPACE sequence embeddings (species_id == 9606)
- Keep kinase families WNK, RAF, CAMK with an optional padding family list
- Cap each family at a configurable number of entries (default 150)
- Export an NPZ bundle plus manifest/metadata artefacts

Usage example::

    python -m bsm.cea.phase1_milvus_data_assembly \
        --output-dir "d:/STRING-DATABASE/NJ3PHASE/phase1" \
        --max-per-family 150

The script assumes Milvus/Zilliz credentials are available through the
standard BSM configuration (.env + config manager).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, MutableMapping, Optional

import numpy as np
import pandas as pd
from pymilvus import Collection

from .milvus_utils import connect_default, disconnect_default, get_collection

LOGGER = logging.getLogger(__name__)


COLLECTION_NAME = "protein_sequences_embeddings_v2"
HUMAN_SPECIES_ID = "9606"
DEFAULT_BATCH_SIZE = 400

FAMILY_QUERY_OVERRIDES = {
    "WNK": "preferred_name like 'WNK%'",
    "RAF": "preferred_name in ['ARAF', 'BRAF', 'RAF1', 'CRAF']",
    "CAMK": "preferred_name like 'CAMK%'",
}


@dataclass
class FamilyRule:
    """Pattern-based assignment from gene symbols to kinase family labels."""

    family: str
    regexes: List[re.Pattern]

    def matches(self, gene_symbol: str) -> bool:
        normalized = (gene_symbol or "").upper()
        return any(pattern.match(normalized) for pattern in self.regexes)


DEFAULT_RULES: List[FamilyRule] = [
    FamilyRule("WNK", [re.compile(r"^WNK\d"), re.compile(r"^WNK\d+[A-Z]?$")]),
    FamilyRule("RAF", [re.compile(r"^(ARAF|BRAF|RAF1|CRAF)$")]),
    FamilyRule(
        "CAMK",
        [
            re.compile(r"^CAMK"),
            re.compile(r"^CAMKK"),
            re.compile(r"^CAMK\d"),
            re.compile(r"^CAMK2[ABDG]"),
        ],
    ),
]


def _query_expr_all(
    collection: Collection,
    expr: str,
    fields: List[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[MutableMapping[str, object]]:
    """Fetch records for a specific expression using paginated queries."""

    LOGGER.debug("Milvus query expr=%s batch_size=%s", expr, batch_size)
    results: List[MutableMapping[str, object]] = []
    offset = 0
    while True:
        limit = min(batch_size, 16384 - offset) if offset + batch_size > 16384 else batch_size
        batch = collection.query(
            expr=expr,
            output_fields=fields,
            limit=limit,
            offset=offset,
        )
        if not batch:
            break
        results.extend(batch)
        offset += len(batch)
        if len(batch) < batch_size or offset >= 16384:
            break
    return results


def _query_human_sequences(
    collection: Collection,
    fields: List[str],
    *,
    family_rules: Iterable[FamilyRule],
) -> List[MutableMapping[str, object]]:
    """Fetch all human entries relevant to the configured family rules."""

    combined: Dict[str, MutableMapping[str, object]] = {}
    base = f"species_id == '{HUMAN_SPECIES_ID}'"

    for rule in family_rules:
        family_expr = FAMILY_QUERY_OVERRIDES.get(rule.family)
        expr = base if family_expr is None else f"({base}) and ({family_expr})"
        LOGGER.info("Querying Milvus for family %s", rule.family)
        for record in _query_expr_all(collection, expr, fields):
            protein_id = record.get("protein_id")
            if protein_id in combined:
                continue
            combined[protein_id] = record

    LOGGER.info("Retrieved %s candidate records from Milvus", len(combined))
    return list(combined.values())


def _assign_families(df: pd.DataFrame, rules: Iterable[FamilyRule]) -> pd.DataFrame:
    family_labels: List[Optional[str]] = []
    for preferred_name in df["preferred_name"].fillna(""):
        assigned: Optional[str] = None
        for rule in rules:
            if rule.matches(preferred_name):
                assigned = rule.family
                break
        family_labels.append(assigned)

    df = df.assign(family=family_labels)
    unmatched = df["family"].isna().sum()
    if unmatched:
        LOGGER.info("Family rules left %s proteins unclassified", unmatched)
    return df.dropna(subset=["family"]).copy()


def _limit_per_family(df: pd.DataFrame, max_per_family: int) -> pd.DataFrame:
    if max_per_family <= 0:
        return df
    limited = (
        df.sort_values("preferred_name")
        .groupby("family", group_keys=False)
        .head(max_per_family)
    )
    LOGGER.info(
        "Selected %s proteins after applying max_per_family=%s", len(limited), max_per_family
    )
    return limited


def _extract_numpy_payload(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    embeddings = np.stack(df["embedding"].apply(np.asarray).to_numpy()).astype(np.float32)
    protein_ids = df["protein_id"].astype(str).to_numpy()
    families = df["family"].astype(str).to_numpy()
    string_clusters = df["cluster_id"].fillna("UNKNOWN").astype(str).to_numpy()
    gene_symbols = df["preferred_name"].fillna("").astype(str).to_numpy()
    if "uniprot_accession" in df.columns:
        uniprot_series = df["uniprot_accession"].fillna("")
    else:
        uniprot_series = pd.Series(["" for _ in range(len(df))], index=df.index)
    uniprot_acs = uniprot_series.astype(str).to_numpy()

    return {
        "embeddings": embeddings,
        "protein_ids": protein_ids,
        "families": families,
        "string_clusters": string_clusters,
        "gene_symbols": gene_symbols,
        "uniprot_acs": uniprot_acs,
    }


def assemble_phase1_dataset(
    output_dir: Path,
    *,
    max_per_family: int = 150,
    additional_fields: Optional[List[str]] = None,
    family_rules: Optional[Iterable[FamilyRule]] = None,
) -> Dict[str, object]:
    """Main orchestration routine used by CLI entrypoint."""

    family_rules = list(family_rules or DEFAULT_RULES)
    fields = [
        "protein_id",
        "embedding",
        "preferred_name",
        "cluster_id",
        "reactome_pathway_names",
        "reactome_pathway_ids",
        "kegg_pathway_names",
        "kegg_pathway_ids",
        "network_degree",
        "network_avg_score",
        "network_max_score",
    ]
    if additional_fields:
        fields.extend(additional_fields)

    config = connect_default()
    try:
        collection = get_collection(COLLECTION_NAME)
        records = _query_human_sequences(collection, fields, family_rules=family_rules)
    finally:
        disconnect_default()

    if not records:
        raise RuntimeError("No records retrieved from Milvus collection")

    df = pd.DataFrame(records)
    df = _assign_families(df, family_rules)

    target_families = {rule.family for rule in family_rules}
    df = df[df["family"].isin(target_families)].copy()

    df = _limit_per_family(df, max_per_family)

    df = df[df["embedding"].notna()].copy()
    if df.empty:
        raise RuntimeError("No embeddings available after filtering")

    payload = _extract_numpy_payload(df)

    output_dir.mkdir(parents=True, exist_ok=True)
    embeddings_path = output_dir / "phase1_wnk_control_embeddings.npz"
    manifest_path = output_dir / "phase1_wnk_control_manifest.json"
    metadata_path = output_dir / "phase1_wnk_control_metadata.csv"

    np.savez_compressed(
        embeddings_path,
        embeddings=payload["embeddings"],
        protein_ids=payload["protein_ids"],
        families=payload["families"],
        string_clusters=payload["string_clusters"],
        uniprot_acs=payload["uniprot_acs"],
        gene_symbols=payload["gene_symbols"],
    )

    df.to_csv(metadata_path, index=False)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "collection": COLLECTION_NAME,
        "species_id": HUMAN_SPECIES_ID,
        "total_proteins": int(len(df)),
        "families": (
            df.groupby("family").agg(count=("protein_id", "nunique"))
        )["count"].to_dict(),
        "embedding_dimension": int(payload["embeddings"].shape[1]),
        "max_per_family": max_per_family,
        "source_uri": getattr(config.milvus, "uri", ""),
        "string_clusters_missing": int(df["cluster_id"].isna().sum()),
        "notes": "Generated via Milvus Phase 1 assembly pipeline",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "embeddings_path": embeddings_path,
        "manifest_path": manifest_path,
        "metadata_path": metadata_path,
        "manifest": manifest,
        "record_count": len(df),
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble Phase 1 dataset from Milvus")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-per-family", type=int, default=150)
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))
    result = assemble_phase1_dataset(
        output_dir=args.output_dir,
        max_per_family=args.max_per_family,
    )
    LOGGER.info(
        "Phase 1 assembly complete: %s proteins saved to %s",
        result["record_count"],
        result["embeddings_path"],
    )


if __name__ == "__main__":
    main()
