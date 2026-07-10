#!/usr/bin/env python3
"""Phase 2 dataset builder for NJ family expansion.

This utility consolidates staging metadata with the STRING sequence embeddings
to produce an NPZ package ready for NJ Phase 2 analyses. It mirrors the plan in
`PHASE_2_FAMILY_EXPANSION.md` and keeps the data pipeline reproducible both
locally and on remote GPU pods.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, List, Optional

import h5py
import numpy as np
import pandas as pd


DEFAULT_FAMILIES = ["WNK", "AGC", "CAMK", "STE", "TK", "TKL"]


def _decode(values: Iterable) -> List[str]:
    decoded: List[str] = []
    for value in values:
        if isinstance(value, (bytes, np.bytes_)):
            decoded.append(value.decode("utf-8"))
        else:
            decoded.append(str(value))
    return decoded


def build_dataset(
    protein_nodes: Path,
    embeddings_h5: Path,
    output_npz: Path,
    *,
    embedding_nodes: Optional[Path] = None,
    pdb_mapping: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    output_csv: Optional[Path] = None,
    families: Optional[List[str]] = None,
    max_per_family: Optional[int] = None,
    dataset_key: str = "embeddings",
    id_key: str = "protein_ids",
) -> None:
    """Create the consolidated NPZ + manifest for Phase 2."""

    families = families or DEFAULT_FAMILIES

    proteins = pd.read_csv(protein_nodes)
    proteins = proteins[proteins["family"].isin(families)].copy()

    if max_per_family is not None:
        proteins = (
            proteins.groupby("family", group_keys=False)
            .apply(lambda df: df.head(max_per_family))
            .reset_index(drop=True)
        )

    if embedding_nodes is not None and embedding_nodes.exists():
        embed_df = pd.read_csv(embedding_nodes)
        if "protein_id" in embed_df.columns and "embedding_id" in embed_df.columns:
            proteins = proteins.merge(embed_df, on="protein_id", how="left")

    if pdb_mapping is not None and pdb_mapping.exists():
        pdb_df = pd.read_csv(pdb_mapping)
        proteins = proteins.merge(pdb_df, on=["protein_id", "uniprot_ac"], how="left")

    proteins = proteins.drop_duplicates(subset=["protein_id"]).reset_index(drop=True)

    if proteins.empty:
        raise ValueError("No proteins available after filtering. Check families or staging data.")

    with h5py.File(embeddings_h5, "r") as h5:
        if dataset_key not in h5:
            raise KeyError(f"Dataset key '{dataset_key}' not present in {embeddings_h5}")
        if id_key not in h5:
            raise KeyError(f"ID key '{id_key}' not present in {embeddings_h5}")

        embeddings = np.asarray(h5[dataset_key])
        protein_ids = _decode(h5[id_key][:])

    id_to_index = {pid: idx for idx, pid in enumerate(protein_ids)}

    missing_ids = [pid for pid in proteins["protein_id"] if pid not in id_to_index]
    if missing_ids:
        raise KeyError(
            f"{len(missing_ids)} protein IDs missing in embeddings file (e.g. {missing_ids[:5]})"
        )

    indices = [id_to_index[pid] for pid in proteins["protein_id"]]
    subset_embeddings = embeddings[indices]

    metadata_arrays = {
        "protein_ids": proteins["protein_id"].astype(str).to_numpy(),
        "uniprot_acs": proteins["uniprot_ac"].astype(str).to_numpy(),
        "families": proteins["family"].astype(str).to_numpy(),
        "subfamilies": proteins.get("subfamily", "Unknown").fillna("Unknown").astype(str).to_numpy(),
    }

    if "string_cluster_id" in proteins.columns:
        metadata_arrays["string_clusters"] = (
            proteins["string_cluster_id"].fillna("None").astype(str).to_numpy()
        )

    if "pdb_id" in proteins.columns:
        metadata_arrays["pdb_ids"] = proteins["pdb_id"].fillna("None").astype(str).to_numpy()

    np.savez_compressed(output_npz, embeddings=subset_embeddings, **metadata_arrays)

    if output_csv is not None:
        proteins.to_csv(output_csv, index=False)

    if manifest_path is not None:
        manifest = {
            "total_proteins": int(len(proteins)),
            "family_counts": proteins["family"].value_counts().to_dict(),
            "embedding_dimensions": int(subset_embeddings.shape[1]),
            "has_pdb": int(proteins.get("pdb_id").notna().sum()) if "pdb_id" in proteins.columns else 0,
            "source_files": {
                "protein_nodes": str(protein_nodes),
                "embedding_nodes": str(embedding_nodes) if embedding_nodes else None,
                "pdb_mapping": str(pdb_mapping) if pdb_mapping else None,
                "embeddings_h5": str(embeddings_h5),
            },
            "families": families,
            "max_per_family": max_per_family,
        }

        manifest_path.write_text(json.dumps(manifest, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Phase 2 NPZ dataset for NJ pipeline")
    parser.add_argument("--protein-nodes", type=Path, required=True)
    parser.add_argument("--embeddings-h5", type=Path, required=True)
    parser.add_argument("--output-npz", type=Path, required=True)
    parser.add_argument("--embedding-nodes", type=Path)
    parser.add_argument("--pdb-mapping", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--families", type=str, nargs="*", default=DEFAULT_FAMILIES)
    parser.add_argument("--max-per-family", type=int)
    parser.add_argument("--dataset-key", type=str, default="embeddings")
    parser.add_argument("--id-key", type=str, default="protein_ids")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_dataset(
        protein_nodes=args.protein_nodes,
        embeddings_h5=args.embeddings_h5,
        output_npz=args.output_npz,
        embedding_nodes=args.embedding_nodes,
        pdb_mapping=args.pdb_mapping,
        manifest_path=args.manifest,
        output_csv=args.output_csv,
        families=args.families,
        max_per_family=args.max_per_family,
        dataset_key=args.dataset_key,
        id_key=args.id_key,
    )


if __name__ == "__main__":
    main()