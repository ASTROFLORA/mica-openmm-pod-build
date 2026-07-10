"""Prepare a live Milvus inventory and a balanced SCFLR pilot manifest.

This utility is the operational bridge for the corpus staging work described in
the SCFLR protocol notes. It does three things in one pass:

1. Inspect the live Milvus deployment and serialize a collection inventory.
2. Fetch or reuse the full human kinase catalog from UniProt and cache it as a
   raw acquisition artifact.
3. Build a balanced 1000-protein pilot manifest with a kinome core plus a
   diverse human background panel sampled from live Milvus collections.

Default outputs live under the configured corpus root so the raw/curated split
stays explicit and reproducible.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Sequence, Tuple

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pymilvus import Collection, utility

from bsm.cea.milvus_utils import collection_field_names, connect_default, disconnect_default, has_collection
from bsm.config import get_bsm_config
from bsm.hpc.kinase_catalog import KinaseCatalogGenerator


LOGGER = logging.getLogger(__name__)


def _resolve_default_corpus_root() -> Path:
    override = os.environ.get("LMP_CORPUS_ROOT")
    if override:
        return Path(override).expanduser()

    preferred = Path(r"D:\LMP_CORPUS")
    if preferred.parent.exists():
        return preferred

    return Path(__file__).resolve().parents[3] / "LMP_CORPUS"


DEFAULT_CORPUS_ROOT = _resolve_default_corpus_root()
DEFAULT_RAW_DIR = DEFAULT_CORPUS_ROOT / "_raw"
DEFAULT_DOWNLOADS_DIR = DEFAULT_RAW_DIR / "downloads"
DEFAULT_MILVUS_SNAPSHOTS_DIR = DEFAULT_RAW_DIR / "milvus_snapshots"
DEFAULT_INVENTORY_DIR = DEFAULT_CORPUS_ROOT / "_inventory"
DEFAULT_MANIFEST_DIR = DEFAULT_CORPUS_ROOT / "_manifests"
DEFAULT_REPORT_DIR = DEFAULT_CORPUS_ROOT / "_reports"

DEFAULT_INVENTORY_PATH = DEFAULT_INVENTORY_DIR / "milvus_inventory.json"
DEFAULT_INVENTORY_REPORT_PATH = DEFAULT_REPORT_DIR / "milvus_inventory.md"
DEFAULT_KINASE_CATALOG_PATH = DEFAULT_DOWNLOADS_DIR / "kinase_catalog_human.json"
DEFAULT_MANIFEST_PATH = DEFAULT_MANIFEST_DIR / "scflr_pilot_manifest.jsonl"
DEFAULT_SUMMARY_PATH = DEFAULT_REPORT_DIR / "scflr_pilot_summary.json"
DEFAULT_REPORT_PATH = DEFAULT_REPORT_DIR / "scflr_pilot_report.md"

DEFAULT_SEQUENCE_SOURCE_CANDIDATES = (
    "protein_sequences_embeddings",
    "protein_sequences_embeddings_v2",
    "swissprot_esmc_v2",
    "protein_multimodal_rag_v1",
)

KINOME_TOPUP_SOURCE_CANDIDATES = (
    "swissprot_esmc_v2",
    "protein_sequences_embeddings",
    "protein_sequences_embeddings_v2",
    "protein_multimodal_rag_v1",
)

DEFAULT_REQUESTED_LANES = (
    "sequence:esm2_650m_cls",
    "sequence:prott5",
    "network:string",
    "domain:dct",
    "structure:af2",
    "multimodal:fused",
)

HUMAN_SPECIES_ID = "9606"
KINOME_TARGET = 518
BACKGROUND_TARGET = 482
PILOT_TARGET = 1000

FIELD_PREFERENCES = (
    "protein_id",
    "uniprot_id",
    "uniprot_accession",
    "preferred_name",
    "gene_symbol",
    "cluster_id",
    "species_id",
    "taxonomy_id",
    "organism",
    "name",
    "protein_name",
)

KINASE_FAMILY_RULES: Tuple[Tuple[str, Tuple[re.Pattern[str], ...]], ...] = (
    ("WNK", (re.compile(r"^WNK\d+[A-Z]?$", re.IGNORECASE),)),
    ("RAF", (re.compile(r"^(ARAF|BRAF|RAF1|CRAF)$", re.IGNORECASE),)),
    (
        "CAMK",
        (
            re.compile(r"^(CAMK|CAMKK|CAMK2|CAMK4|CAMK5|MARK|BRSK|SIK|NUAK|PNCK|MOK)", re.IGNORECASE),
        ),
    ),
    (
        "TK_RECEPTOR",
        (
            re.compile(
                r"^(EGFR|ERBB\d*|FGFR\d*|VEGFR\d*|PDGFR[AB]?|KIT|FLT\d*|RET|MET|ALK|AXL|MERTK|TYRO3|TEK|TIE1|EPHA\d*|EPHB\d*|NTRK\d*|INSR|IGF1R|DDR[12]|MUSK|ROR1|ROR2|ROS1|LTK)$",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "TK_NON_RECEPTOR",
        (
            re.compile(r"^(ABL\d?|SRC|JAK\d*|SYK|BTK|TEC|CSK|FES|FER|FYN|HCK|LYN|LCK|YES1?|ZAP70|TNK2|FRK|BLK|ITK|TXK|BMX)$", re.IGNORECASE),
        ),
    ),
    (
        "AGC",
        (
            re.compile(r"^(PRK[A-Z0-9]+|AKT\d*|PKA|PKC|SGK\d*|RPS6KA\d*|RPS6KB\d*|RPS6KC\d*|ROCK\d*|PKN\d*|GRK\d*|PRKG\d*|PDPK1|PDK1|S6K1|S6K2)$", re.IGNORECASE),
        ),
    ),
    (
        "CMGC",
        (
            re.compile(r"^(CDK\d*|CDKL\d*|MAPK\d*|GSK3[AB]?|CLK\d*|DYRK\d*|SRPK\d*|HIPK\d*|TTK|LATS\d*|PHKG[12]?|MAST\d*|NLK|CCNK)$", re.IGNORECASE),
        ),
    ),
    (
        "STE",
        (
            re.compile(r"^(MAP3K\d*|MAP4K\d*|PAK\d*|RIPK\d*|TAOK\d*|IRAK\d*|MST\d*|LIMK\d*|BMPR\d*|ACVR\d*|TGFBR\d*|MINK1|MAP2K\d*)$", re.IGNORECASE),
        ),
    ),
    (
        "CK1",
        (
            re.compile(r"^(CSNK1[A-Z]?\d*|CSNK2[A-Z]?\d*|CK1[A-Z]?)$", re.IGNORECASE),
        ),
    ),
    (
        "ATYPICAL",
        (
            re.compile(r"^(PIK3[A-Z0-9]*|PI4K\d*|MTOR|ATM|ATR|RIOK\d*|PINK1|ULK\d*|PIP5K\d*|WEE1|BUB1B?|PRKAA\d*|TBK1|IKBKE|PASK|STK11|STK24|STK25|STK26|STK33|STK38|STK39)$", re.IGNORECASE),
        ),
    ),
)

KINASE_HINT_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    pattern for _, family_patterns in KINASE_FAMILY_RULES for pattern in family_patterns
)


@dataclass(frozen=True)
class ManifestRecord:
    """Normalized manifest row."""

    manifest_id: str
    protein_id: str
    cohort: str
    source: str
    family: str
    gene_symbol: str
    protein_name: str
    uniprot_id: str
    species_id: str
    source_collection: str
    requested_lanes: Tuple[str, ...]
    priority: int
    notes: str
    cluster_id: str = ""
    entity_type: str = "protein"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "protein_id": self.protein_id,
            "cohort": self.cohort,
            "source": self.source,
            "family": self.family,
            "gene_symbol": self.gene_symbol,
            "protein_name": self.protein_name,
            "uniprot_id": self.uniprot_id,
            "species_id": self.species_id,
            "source_collection": self.source_collection,
            "requested_lanes": list(self.requested_lanes),
            "priority": self.priority,
            "notes": self.notes,
            "cluster_id": self.cluster_id,
            "entity_type": self.entity_type,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(*values: object) -> str:
    chunks = [str(value).strip() for value in values if value not in (None, "")]
    return " ".join(chunks).upper()


def _first_non_empty(record: Dict[str, Any], keys: Sequence[str], default: str = "") -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _identifier(record: Dict[str, Any]) -> str:
    return _first_non_empty(record, ("uniprot_id", "uniprot_accession", "protein_id", "id", "accession"), default="UNKNOWN")


def _protein_name(record: Dict[str, Any]) -> str:
    return _first_non_empty(record, ("preferred_name", "protein_name", "name", "full_name", "gene_symbol"), default="")


def _gene_symbol(record: Dict[str, Any]) -> str:
    return _first_non_empty(record, ("gene_symbol", "preferred_name", "protein_name", "name"), default="")


def _species_id(record: Dict[str, Any]) -> str:
    return _first_non_empty(record, ("species_id", "taxonomy_id", "taxon_id"), default="")


def _primary_collection_field(collection: Collection, preferred: Sequence[str]) -> str:
    available = set(collection_field_names(collection))
    for field_name in preferred:
        if field_name in available:
            return field_name
    for fallback in available:
        if fallback.endswith("id") or fallback.endswith("_id"):
            return fallback
    raise RuntimeError(f"No usable identifier field found in {collection.name}")


def _human_expr(field_names: Sequence[str]) -> str:
    available = set(field_names)
    if "species_id" in available:
        return f"species_id == '{HUMAN_SPECIES_ID}'"
    if "taxonomy_id" in available:
        return f"taxonomy_id == {HUMAN_SPECIES_ID}"
    if "taxon_id" in available:
        return f"taxon_id == {HUMAN_SPECIES_ID}"
    if "organism" in available:
        return "organism like 'Homo sapiens%'"
    if "name" in available:
        return "name != ''"
    if "protein_id" in available:
        return "protein_id != ''"
    return "id != ''"


def _collection_indexes(collection: Collection) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    try:
        indexes = getattr(collection, "indexes", []) or []
    except Exception:  # pragma: no cover - defensive fallback
        indexes = []

    for index in indexes:
        if isinstance(index, dict):
            snapshots.append(index)
            continue
        snapshots.append(
            {
                "field_name": getattr(index, "field_name", ""),
                "index_name": getattr(index, "index_name", ""),
                "index_type": getattr(index, "index_type", ""),
                "params": getattr(index, "params", {}),
            }
        )
    return snapshots


def _collection_schema_snapshot(collection: Collection) -> Dict[str, Any]:
    fields = []
    for field in collection.schema.fields:
        fields.append(
            {
                "name": field.name,
                "dtype": getattr(field.dtype, "name", str(field.dtype)),
                "is_primary": bool(getattr(field, "is_primary", False)),
                "auto_id": bool(getattr(field, "auto_id", False)),
                "description": getattr(field, "description", ""),
                "max_length": getattr(field, "max_length", None),
                "dim": getattr(field, "dim", None),
            }
        )
    return {
        "description": getattr(collection.schema, "description", ""),
        "enable_dynamic_field": bool(getattr(collection.schema, "enable_dynamic_field", False)),
        "fields": fields,
        "primary_field": next((field["name"] for field in fields if field["is_primary"]), ""),
        "vector_fields": [field["name"] for field in fields if field.get("dim")],
    }


def _collection_snapshot(collection: Collection) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "name": collection.name,
        "field_names": collection_field_names(collection),
        "schema": _collection_schema_snapshot(collection),
        "indexes": _collection_indexes(collection),
    }
    try:
        snapshot["row_count"] = int(collection.num_entities)
    except Exception as exc:  # pragma: no cover - depends on remote state
        snapshot["row_count_error"] = str(exc)
        snapshot["row_count"] = None
    try:
        snapshot["has_index"] = bool(collection.has_index())
    except Exception as exc:  # pragma: no cover - depends on client version
        snapshot["has_index_error"] = str(exc)
    try:
        snapshot["describe_collection"] = utility.describe_collection(collection.name)
    except Exception as exc:  # pragma: no cover - some pymilvus builds omit it
        snapshot["describe_collection_error"] = str(exc)
    return snapshot


def _list_live_collections(alias: str = "default") -> List[str]:
    try:
        return list(utility.list_collections(using=alias))
    except TypeError:
        return list(utility.list_collections())


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[ManifestRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False))
            handle.write("\n")


def _family_for_kinase(record: Dict[str, Any]) -> str:
    text = _normalize_text(_gene_symbol(record), _protein_name(record), record.get("entry_name", ""))
    for family, patterns in KINASE_FAMILY_RULES:
        for pattern in patterns:
            if pattern.search(text):
                return family
    return "OTHER_KINASE"


def _looks_like_kinase(record: Dict[str, Any], kinase_accessions: Optional[set[str]] = None) -> bool:
    identifier = _identifier(record)
    if kinase_accessions and identifier in kinase_accessions:
        return True
    text = _normalize_text(_gene_symbol(record), _protein_name(record), record.get("entry_name", ""))
    if "KINASE" in text:
        return True
    for pattern in KINASE_HINT_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _round_robin_sample(records: Sequence[Dict[str, Any]], target_count: int, bucket_keys: Sequence[str]) -> List[Dict[str, Any]]:
    if target_count <= 0:
        return []

    grouped: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        bucket = "UNKNOWN"
        for key in bucket_keys:
            value = record.get(key)
            if value not in (None, ""):
                bucket = str(value)
                break
        grouped[bucket].append(record)

    ordered_buckets = sorted(grouped.keys())
    for bucket in ordered_buckets:
        grouped[bucket].sort(key=lambda row: (_identifier(row), _gene_symbol(row), _protein_name(row)))

    selected: List[Dict[str, Any]] = []
    while len(selected) < target_count and ordered_buckets:
        next_round: List[str] = []
        for bucket in ordered_buckets:
            bucket_records = grouped[bucket]
            if bucket_records:
                selected.append(bucket_records.pop(0))
                if len(selected) >= target_count:
                    break
            if bucket_records:
                next_round.append(bucket)
        ordered_buckets = next_round

    return selected[:target_count]


def _kinase_manifest_records(kinases: Sequence[Dict[str, Any]], catalog_path: Path) -> List[ManifestRecord]:
    records: List[ManifestRecord] = []
    for kinase in kinases:
        protein_id = _identifier(kinase)
        family = _family_for_kinase(kinase)
        gene_symbol = _gene_symbol(kinase)
        protein_name = _protein_name(kinase)
        records.append(
            ManifestRecord(
                manifest_id=f"kinome:{protein_id}",
                protein_id=protein_id,
                cohort="kinome_core",
                source="uniprot",
                family=family,
                gene_symbol=gene_symbol,
                protein_name=protein_name,
                uniprot_id=protein_id,
                species_id=str(kinase.get("taxonomy_id", HUMAN_SPECIES_ID)),
                source_collection="uniprot:reviewed_human_kinase_catalog",
                requested_lanes=DEFAULT_REQUESTED_LANES,
                priority=1,
                notes=f"Kinome core entry cached from {catalog_path.name}",
                cluster_id=kinase.get("entry_name", ""),
            )
        )
    return records


def _kinase_topup_manifest_records(records: Sequence[Dict[str, Any]], source_collection: str) -> List[ManifestRecord]:
    manifest: List[ManifestRecord] = []
    for record in records:
        protein_id = _identifier(record)
        manifest.append(
            ManifestRecord(
                manifest_id=f"kinome_topup:{protein_id}",
                protein_id=protein_id,
                cohort="kinome_core",
                source="milvus_live",
                family=_family_for_kinase(record),
                gene_symbol=_gene_symbol(record),
                protein_name=_protein_name(record),
                uniprot_id=_first_non_empty(record, ("uniprot_id", "uniprot_accession", "protein_id"), default=protein_id),
                species_id=_species_id(record) or HUMAN_SPECIES_ID,
                source_collection=source_collection,
                requested_lanes=DEFAULT_REQUESTED_LANES,
                priority=1,
                notes="Kinome top-up selected from live Milvus to complete the 518-protein core",
                cluster_id=_first_non_empty(record, ("cluster_id", "preferred_name", "gene_symbol"), default=""),
            )
        )
    return manifest


def _collect_kinome_topup_candidates(
    source_candidates: Sequence[str],
    *,
    kinase_accessions: set[str],
    alias: str,
    target_count: int,
) -> Tuple[str, List[Dict[str, Any]]]:
    collected: List[Dict[str, Any]] = []
    selected_source = ""

    for name in source_candidates:
        if len(collected) >= target_count:
            break
        if not has_collection(name, alias=alias):
            continue

        collection = Collection(name, using=alias)
        source_fields = set(collection_field_names(collection))
        query_fields = [field for field in FIELD_PREFERENCES if field in source_fields]
        if not query_fields:
            continue

        expr = _human_expr(query_fields)
        raw_candidates = _query_collection_rows(
            collection,
            expr=expr,
            output_fields=query_fields,
            limit=max(1000, target_count * 20),
        )
        kinase_like = [
            record
            for record in raw_candidates
            if _looks_like_kinase(record, kinase_accessions) and _identifier(record) not in kinase_accessions
        ]
        if kinase_like and not selected_source:
            selected_source = name
        collected.extend(kinase_like)

    if len(collected) < target_count:
        raise RuntimeError(f"Kinome top-up candidate pool too small: {len(collected)} rows for target {target_count}")

    return selected_source, collected[:target_count]


def _background_manifest_records(records: Sequence[Dict[str, Any]], source_collection: str) -> List[ManifestRecord]:
    manifest: List[ManifestRecord] = []
    for record in records:
        protein_id = _identifier(record)
        manifest.append(
            ManifestRecord(
                manifest_id=f"background:{protein_id}",
                protein_id=protein_id,
                cohort="background_panel",
                source="milvus_live",
                family="BACKGROUND",
                gene_symbol=_gene_symbol(record),
                protein_name=_protein_name(record),
                uniprot_id=_first_non_empty(record, ("uniprot_id", "uniprot_accession", "protein_id"), default=protein_id),
                species_id=_species_id(record) or HUMAN_SPECIES_ID,
                source_collection=source_collection,
                requested_lanes=DEFAULT_REQUESTED_LANES,
                priority=2,
                notes="Non-kinase human background selected from live Milvus",
                cluster_id=_first_non_empty(record, ("cluster_id", "family", "preferred_name"), default=""),
            )
        )
    return manifest


def _ensure_kinase_catalog(catalog_path: Path, *, batch_size: int, rate_limit: float, refresh: bool) -> Dict[str, Any]:
    if catalog_path.exists() and not refresh:
        cached_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        cached_kinases = cached_catalog.get("kinases") if isinstance(cached_catalog, dict) else None
        if isinstance(cached_kinases, list) and cached_kinases:
            return cached_catalog
        LOGGER.warning("Cached kinase catalog at %s is empty; refreshing from UniProt", catalog_path)

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    generator = KinaseCatalogGenerator(rate_limit_delay=rate_limit)
    kinases = generator.fetch_kinases(batch_size=batch_size)
    generator.save_catalog(kinases, catalog_path)
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def _select_background_source_collection(preferred: Sequence[str], alias: str = "default") -> Tuple[str, Collection]:
    for name in preferred:
        if not has_collection(name, alias=alias):
            continue
        collection = Collection(name, using=alias)
        field_names = set(collection_field_names(collection))
        if any(field in field_names for field in ("protein_id", "uniprot_id", "protein_name", "preferred_name")):
            return name, collection
    raise RuntimeError(f"No suitable live Milvus source collection found among: {', '.join(preferred)}")


def _query_collection_rows(
    collection: Collection,
    *,
    expr: str,
    output_fields: Sequence[str],
    limit: int,
    batch_size: int = 400,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if hasattr(collection, "query_iterator"):
        iterator = collection.query_iterator(batch_size=batch_size, output_fields=list(output_fields), expr=expr)
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                results.extend(batch)
                if len(results) >= limit:
                    return results[:limit]
        finally:
            iterator.close()
        return results[:limit]

    offset = 0
    while len(results) < limit:
        current_limit = min(batch_size, limit - len(results))
        batch = collection.query(expr=expr, output_fields=list(output_fields), limit=current_limit, offset=offset)
        if not batch:
            break
        results.extend(batch)
        offset += len(batch)
        if len(batch) < current_limit:
            break
    return results[:limit]


def _build_inventory(corpus_root: Path, *, alias: str, collection_names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    inventory: Dict[str, Any] = {
        "generated_at": _utc_now(),
        "corpus_root": str(corpus_root),
        "milvus_alias": alias,
        "collections": [],
    }

    available = _list_live_collections(alias=alias)
    targets = list(collection_names) if collection_names else available
    inventory["available_collections"] = available
    inventory["requested_collections"] = targets

    for name in targets:
        if not has_collection(name, alias=alias):
            inventory["collections"].append({"name": name, "exists": False})
            continue

        collection = Collection(name, using=alias)
        snapshot = _collection_snapshot(collection)
        snapshot["exists"] = True
        inventory["collections"].append(snapshot)

    return inventory


def _render_inventory_markdown(inventory: Dict[str, Any]) -> str:
    lines = [
        "# Milvus Inventory",
        "",
        f"Generated at: {inventory.get('generated_at', '')}",
        f"Corpus root: {inventory.get('corpus_root', '')}",
        f"Raw snapshot: {inventory.get('raw_snapshot_path', '')}",
        "",
        "| Collection | Exists | Rows | Fields | Vector Fields |",
        "| --- | --- | ---: | --- | --- |",
    ]

    for item in inventory.get("collections", []):
        name = item.get("name", "")
        exists = "yes" if item.get("exists") else "no"
        rows = item.get("row_count")
        rows_text = str(rows) if rows is not None else "n/a"
        fields = ", ".join(item.get("field_names", []))
        vector_fields = ", ".join(item.get("schema", {}).get("vector_fields", []))
        lines.append(f"| {name} | {exists} | {rows_text} | {fields} | {vector_fields} |")

    return "\n".join(lines) + "\n"


def _summarize_manifest(records: Sequence[ManifestRecord], kinome_count: int, background_count: int) -> Dict[str, Any]:
    families = Counter(record.family for record in records if record.cohort == "kinome_core")
    cohorts = Counter(record.cohort for record in records)
    sources = Counter(record.source_collection for record in records)
    return {
        "generated_at": _utc_now(),
        "total": len(records),
        "target_total": PILOT_TARGET,
        "kinome_target": KINOME_TARGET,
        "background_target": BACKGROUND_TARGET,
        "kinome_count": kinome_count,
        "background_count": background_count,
        "cohort_counts": dict(sorted(cohorts.items())),
        "family_counts": dict(sorted(families.items())),
        "source_collection_counts": dict(sorted(sources.items())),
        "requested_lanes": list(DEFAULT_REQUESTED_LANES),
    }


def _render_report(inventory: Dict[str, Any], summary: Dict[str, Any], source_collection: str) -> str:
    lines = [
        "# SCFLR Pilot Manifest Report",
        "",
        f"Generated at: {summary['generated_at']}",
        f"Background source collection: {source_collection}",
        f"Raw Milvus snapshot: {summary.get('milvus_snapshot_path', '')}",
        "",
        "## Pilot Counts",
        "",
        f"- Total: {summary['total']}",
        f"- Kinome core: {summary['kinome_count']}",
        f"- Background panel: {summary['background_count']}",
        "",
        "## Cohorts",
        "",
    ]

    for cohort, count in summary.get("cohort_counts", {}).items():
        lines.append(f"- {cohort}: {count}")

    lines.extend([
        "",
        "## Kinase Families",
        "",
    ])
    for family, count in summary.get("family_counts", {}).items():
        lines.append(f"- {family}: {count}")

    lines.extend([
        "",
        "## Live Milvus Collections",
        "",
    ])
    for item in inventory.get("collections", []):
        rows = item.get("row_count")
        rows_text = str(rows) if rows is not None else "n/a"
        lines.append(f"- {item.get('name', '')}: {rows_text} rows")

    return "\n".join(lines) + "\n"


def build_pilot_manifest(
    *,
    corpus_root: Path,
    inventory_output: Path,
    inventory_report_output: Path,
    kinase_catalog_output: Path,
    manifest_output: Path,
    summary_output: Path,
    report_output: Path,
    alias: str,
    refresh_kinase_catalog: bool,
    kinase_batch_size: int,
    kinase_rate_limit: float,
    background_source_candidates: Sequence[str],
) -> Dict[str, Any]:
    corpus_root.mkdir(parents=True, exist_ok=True)
    DEFAULT_RAW_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_MILVUS_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    for path in (inventory_output, inventory_report_output, kinase_catalog_output, manifest_output, summary_output, report_output):
        path.parent.mkdir(parents=True, exist_ok=True)

    connect_default(alias=alias)
    try:
        inventory = _build_inventory(corpus_root, alias=alias)
        snapshot_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = DEFAULT_MILVUS_SNAPSHOTS_DIR / f"milvus_inventory_{snapshot_stamp}.json"
        inventory["raw_snapshot_path"] = str(snapshot_path)
        _write_json(inventory_output, inventory)
        _write_json(snapshot_path, inventory)
        inventory_report_output.write_text(_render_inventory_markdown(inventory), encoding="utf-8")

        kinase_catalog = _ensure_kinase_catalog(
            kinase_catalog_output,
            batch_size=kinase_batch_size,
            rate_limit=kinase_rate_limit,
            refresh=refresh_kinase_catalog,
        )
        kinases = list(kinase_catalog.get("kinases", []))
        if not kinases:
            raise RuntimeError("Kinase catalog is empty after acquisition")

        kinase_accessions = {_identifier(kinase) for kinase in kinases}
        kinase_records = _kinase_manifest_records(kinases, kinase_catalog_output)

        if len(kinase_records) < KINOME_TARGET:
            topup_needed = KINOME_TARGET - len(kinase_records)
            topup_source, topup_candidates = _collect_kinome_topup_candidates(
                KINOME_TOPUP_SOURCE_CANDIDATES,
                kinase_accessions=kinase_accessions,
                alias=alias,
                target_count=topup_needed,
            )
            topup_records = _round_robin_sample(
                topup_candidates,
                topup_needed,
                bucket_keys=("cluster_id", "preferred_name", "gene_symbol", "protein_id", "uniprot_id"),
            )
            if len(topup_records) < topup_needed:
                raise RuntimeError(f"Could not assemble full kinome core: {len(kinase_records) + len(topup_records)} / {KINOME_TARGET}")

            kinase_records.extend(_kinase_topup_manifest_records(topup_records, topup_source))
            kinase_accessions.update(_identifier(record) for record in topup_records)

        source_name, source_collection = _select_background_source_collection(background_source_candidates, alias=alias)
        source_fields = set(collection_field_names(source_collection))
        query_fields = [field for field in FIELD_PREFERENCES if field in source_fields]
        expr = _human_expr(query_fields)
        raw_candidates = _query_collection_rows(
            source_collection,
            expr=expr,
            output_fields=query_fields,
            limit=max(2500, BACKGROUND_TARGET * 8),
        )

        background_candidates = [
            record
            for record in raw_candidates
            if not _looks_like_kinase(record, kinase_accessions)
            and _identifier(record) not in kinase_accessions
        ]

        if len(background_candidates) < BACKGROUND_TARGET:
            raise RuntimeError(
                f"Background candidate pool too small: {len(background_candidates)} rows for target {BACKGROUND_TARGET}"
            )

        background_records = _round_robin_sample(
            background_candidates,
            BACKGROUND_TARGET,
            bucket_keys=("cluster_id", "preferred_name", "gene_symbol", "protein_id", "uniprot_id"),
        )

        if len(background_records) < BACKGROUND_TARGET:
            raise RuntimeError(
                f"Could not assemble full background panel: {len(background_records)} / {BACKGROUND_TARGET}"
            )

        background_manifest = _background_manifest_records(background_records, source_name)
        manifest_records = kinase_records + background_manifest

        if len(manifest_records) != PILOT_TARGET:
            raise RuntimeError(
                f"Pilot manifest size mismatch: {len(manifest_records)} != {PILOT_TARGET}"
            )

        _write_jsonl(manifest_output, manifest_records)

        summary = _summarize_manifest(manifest_records, len(kinase_records), len(background_manifest))
        summary["kinase_catalog_path"] = str(kinase_catalog_output)
        summary["inventory_path"] = str(inventory_output)
        summary["inventory_report_path"] = str(inventory_report_output)
        summary["milvus_snapshot_path"] = str(snapshot_path)
        summary["manifest_path"] = str(manifest_output)
        summary["report_path"] = str(report_output)
        summary["background_source_collection"] = source_name
        summary["background_candidate_count"] = len(background_candidates)
        summary["background_requested_target"] = BACKGROUND_TARGET
        summary["kinome_requested_target"] = KINOME_TARGET

        _write_json(summary_output, summary)
        report_output.write_text(_render_report(inventory, summary, source_name), encoding="utf-8")

        return summary
    finally:
        disconnect_default(alias=alias)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare SCFLR pilot manifest and Milvus inventory")
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT, help="Corpus root directory")
    parser.add_argument("--inventory-output", type=Path, default=DEFAULT_INVENTORY_PATH, help="Milvus inventory JSON path")
    parser.add_argument("--inventory-report-output", type=Path, default=DEFAULT_INVENTORY_REPORT_PATH, help="Milvus inventory markdown path")
    parser.add_argument("--kinase-catalog-output", type=Path, default=DEFAULT_KINASE_CATALOG_PATH, help="Cached human kinase catalog JSON path")
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST_PATH, help="Pilot manifest JSONL path")
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_PATH, help="Pilot summary JSON path")
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT_PATH, help="Pilot report markdown path")
    parser.add_argument(
        "--background-source",
        action="append",
        dest="background_sources",
        help="Preferred live Milvus source collection for the background panel (repeatable)",
    )
    parser.add_argument("--refresh-kinase-catalog", action="store_true", help="Re-fetch the kinase catalog from UniProt")
    parser.add_argument("--kinase-batch-size", type=int, default=500, help="UniProt page size for kinase catalog fetch")
    parser.add_argument("--kinase-rate-limit", type=float, default=0.5, help="Delay between UniProt requests in seconds")
    parser.add_argument("--milvus-alias", type=str, default="default", help="Milvus connection alias")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging verbosity")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = get_bsm_config()
    LOGGER.info("Using Milvus URI=%s collection=%s", getattr(config.milvus, "uri", ""), getattr(config.milvus, "collection_name", ""))

    background_sources = tuple(args.background_sources) if args.background_sources else DEFAULT_SEQUENCE_SOURCE_CANDIDATES

    summary = build_pilot_manifest(
        corpus_root=args.corpus_root,
        inventory_output=args.inventory_output,
        inventory_report_output=args.inventory_report_output,
        kinase_catalog_output=args.kinase_catalog_output,
        manifest_output=args.manifest_output,
        summary_output=args.summary_output,
        report_output=args.report_output,
        alias=args.milvus_alias,
        refresh_kinase_catalog=args.refresh_kinase_catalog,
        kinase_batch_size=args.kinase_batch_size,
        kinase_rate_limit=args.kinase_rate_limit,
        background_source_candidates=background_sources,
    )

    LOGGER.info(
        "Prepared pilot manifest: total=%s kinome=%s background=%s source=%s",
        summary["total"],
        summary["kinome_count"],
        summary["background_count"],
        summary["background_source_collection"],
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())