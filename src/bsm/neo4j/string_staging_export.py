"""Utilities to export STRING metadata into Neo4j staging CSVs."""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROTEIN_COLUMNS = (
    "protein_id",
    "uniprot_id",
    "name",
    "gene_name",
    "organism",
    "organism_id",
    "sequence_length",
    "molecular_weight",
    "isoelectric_point",
    "protein_family",
    "functional_class",
    "subcellular_location",
    "paper_count",
    "first_characterized",
    "clinical_relevance",
    "druggability_score",
    "conservation_score",
    "pathway_centrality",
    "description",
)


PATHWAY_COLUMNS = (
    "pathway_id",
    "kegg_id",
    "reactome_id",
    "pathway_length",
    "disease_associations",
    "drug_targets",
    "clinical_trials",
    "confidence_level",
    "curation_source",
    "last_updated",
    "description",
)


PROTEIN_PATHWAY_COLUMNS = (
    "protein_id",
    "pathway_id",
    "role",
    "pathway_position",
    "regulatory_effect",
    "tissue_expression",
    "disease_relevance",
    "confidence",
    "curation_source",
)


PROTEIN_EDGE_COLUMNS = (
    "source_protein_id",
    "target_protein_id",
    "confidence",
    "evidence_count",
    "supporting_papers",
    "experimental_methods",
    "physiological_relevance",
    "tissue_specificity",
    "phosphorylation_site",
    "km_value",
    "regulatory_effect",
    "first_reported",
    "last_validated",
    "validation_count",
)


EMBEDDING_COLUMNS = (
    "embedding_id",
    "model",
    "model_version",
    "dimensions",
    "embedding_norm",
    "milvus_collection",
    "embedding_quality_score",
    "semantic_coherence",
    "created_at",
    "model_config",
)


def _iter_metadata(path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            yield json.loads(line)


def _safe(value, fallback="NA"):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    return text if text else fallback


def _write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def build_protein_rows(metadata_path):
    rows = []
    for record in _iter_metadata(metadata_path):
        pfam = record.get("pfam_domains") or []
        smart = record.get("smart_domains") or []
        functional_tags = []
        if pfam:
            functional_tags.extend(item.get("description", "") for item in pfam if isinstance(item, dict))
        if smart:
            functional_tags.extend(item.get("description", "") for item in smart if isinstance(item, dict))

        functional_class = "|".join(tag for tag in functional_tags if tag) or "NA"
        protein_family = record.get("cluster_id") or "NA"
        clinical_info = record.get("clinical_relevance") or {}
        literature_refs = clinical_info.get("literature_references") or []
        paper_count = str(len(literature_refs)) if literature_refs else "0"
        first_characterized = literature_refs[0] if literature_refs else "NA"
        clinical_payload = clinical_info or {"summary": "No clinical signals detected"}
        pathway_centrality = record.get("pathway_centrality")

        rows.append(
            [
                _safe(record.get("protein_id")),
                _safe(record.get("primary_uniprot")),
                _safe(record.get("preferred_name")),
                _safe(record.get("gene_symbol")),
                _safe(record.get("organism")),
                _safe(record.get("species_id")),
                _safe(record.get("sequence_length")),
                "NA",
                "NA",
                protein_family,
                functional_class,
                "NA",
                paper_count,
                _safe(first_characterized),
                _safe(clinical_payload),
                "NA",
                "NA",
                _safe(pathway_centrality),
                _safe(record.get("functional_annotation")),
            ]
        )
    return rows


def build_pathway_assets(metadata_path):
    pathway_rows = {}
    relationships = []

    now = datetime.now(timezone.utc).isoformat()

    for record in _iter_metadata(metadata_path):
        reactome_entries = record.get("reactome_pathways") or []
        for entry in reactome_entries:
            reactome_id = _safe(entry.get("id"))
            if reactome_id == "NA":
                continue
            pathway_id = "PW_STRING_{}".format(reactome_id.replace(":", "_"))
            name = _safe(entry.get("description"))
            if pathway_id not in pathway_rows:
                pathway_rows[pathway_id] = [
                    pathway_id,
                    "NA",
                    reactome_id,
                    name,
                    "reactome_pathway",
                    name,
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "inferred",
                    "STRING_REACTOME",
                    now,
                    name,
                ]

            relationships.append(
                [
                    _safe(record.get("protein_id")),
                    pathway_id,
                    "associated",
                    "unknown",
                    "NA",
                    "NA",
                    "NA",
                    "0.8",
                    "STRING_REACTOME",
                ]
            )

    return list(pathway_rows.values()), relationships


def build_protein_edges(metadata_path, neighbor_limit):
    rows = []

    for record in _iter_metadata(metadata_path):
        protein_id = record.get("protein_id")
        if not protein_id:
            continue

        neighbors = record.get("interaction_neighbors") or []
        dedup = {}
        for neighbor in neighbors:
            target = neighbor.get("protein_id")
            if not target or target == protein_id:
                continue
            stored = dedup.get(target)
            if stored is None or float(neighbor.get("score", 0.0) or 0.0) > float(stored.get("score", 0.0) or 0.0):
                dedup[target] = neighbor

        # Sort by score descending and trim
        sorted_neighbors = sorted(dedup.values(), key=lambda item: float(item.get("score", 0.0) or 0.0), reverse=True)
        for neighbor in sorted_neighbors[:neighbor_limit]:
            score = float(neighbor.get("score", 0.0) or 0.0)
            confidence = "{:.3f}".format(min(score / 1000.0, 1.0))
            evidence = neighbor.get("evidence") or {}
            evidence_count = sum(1 for value in evidence.values() if (value or 0) > 0)
            dominant = neighbor.get("dominant_evidence") or "unknown"

            rows.append(
                [
                    _safe(protein_id),
                    _safe(neighbor.get("protein_id")),
                    confidence,
                    str(evidence_count),
                    "NA",
                    dominant,
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                ]
            )

    return rows


def build_embedding_rows(sequence_dim, network_dim):
    now = datetime.now(timezone.utc).isoformat()
    base_config = json.dumps({"max_length": 0, "pooling": "unknown", "normalize": True})
    return [
        [
            "EMB_STRING_SEQ",
            "SPACE_SEQUENCE",
            "v12.0",
            str(sequence_dim),
            "NA",
            "protein_sequences_embeddings",
            "0.85",
            "0.80",
            now,
            base_config,
        ],
        [
            "EMB_STRING_NET",
            "SPACE_NETWORK",
            "v12.0",
            str(network_dim),
            "NA",
            "protein_networks_embeddings",
            "0.83",
            "0.78",
            now,
            base_config,
        ],
    ]


def export_staging(metadata_path, output_dir, neighbor_limit):
    stats = defaultdict(int)

    protein_rows = build_protein_rows(metadata_path)
    _write_csv(output_dir / "protein_nodes.csv", PROTEIN_COLUMNS, protein_rows)
    stats["protein_nodes"] = len(protein_rows)

    pathway_rows, pathway_relationships = build_pathway_assets(metadata_path)
    _write_csv(output_dir / "pathway_nodes.csv", PATHWAY_COLUMNS, pathway_rows)
    stats["pathway_nodes"] = len(pathway_rows)

    _write_csv(
        output_dir / "protein_pathway_relationships.csv",
        PROTEIN_PATHWAY_COLUMNS,
        pathway_relationships,
    )
    stats["protein_pathway_relationships"] = len(pathway_relationships)

    edge_rows = build_protein_edges(metadata_path, neighbor_limit)
    _write_csv(output_dir / "protein_protein_relationships.csv", PROTEIN_EDGE_COLUMNS, edge_rows)
    stats["protein_protein_relationships"] = len(edge_rows)

    embedding_rows = build_embedding_rows(sequence_dim=1024, network_dim=512)
    _write_csv(output_dir / "embedding_nodes_enhanced.csv", EMBEDDING_COLUMNS, embedding_rows)
    stats["embedding_nodes"] = len(embedding_rows)

    summary_path = output_dir / "string_staging_summary.json"
    summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

    return stats


def parse_args():
    parser = argparse.ArgumentParser(description="Export STRING metadata to Neo4j staging CSVs")
    parser.add_argument("--metadata", type=Path, default=Path("processed_string/space_metadata.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("staging/string_v12"))
    parser.add_argument("--neighbor-limit", type=int, default=20, help="Maximum neighbors per protein")
    return parser.parse_args()


def main():
    args = parse_args()
    export_staging(args.metadata, args.output, args.neighbor_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
