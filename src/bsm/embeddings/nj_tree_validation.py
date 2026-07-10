#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neighbor-Joining validation pipeline for SPACE embeddings.

This module builds a cosine-distance NJ tree over SPACE protein embeddings,
compares it against a Ward+Euclidean baseline, and persists the resulting
artifacts (Newick + node table + metrics). The goal is to operationalize the
Yeung et al. methodology for BSM-BUDO-CEA Phase 3.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import h5py
import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage, cophenet
from scipy.spatial.distance import pdist, squareform
from scipy.stats import spearmanr
from sklearn.metrics import davies_bouldin_score, silhouette_score

from bsm.embeddings.tree_builder import (
    TreeArtifacts,
    build_tree,
    center_and_whiten,
    cosine_distance_matrix,
)
from bsm.embeddings.tree_index import save_tree_artifacts


DEFAULT_NETWORK_H5 = Path("D:/STRING-DATABASE/9606.protein.network.embeddings.v12.0.h5")
DEFAULT_OUTPUT_DIR = Path("C:/Users/busta/Downloads/MICA/space_embeddings_output/nj_trees")


def _decode_bytes(values: Iterable) -> List[str]:
    out: List[str] = []
    for value in values:
        if isinstance(value, (bytes, np.bytes_)):
            out.append(value.decode("utf-8"))
        else:
            out.append(str(value))
    return out


def load_embeddings(
    path: Path,
    dataset_key: str,
    id_key: Optional[str],
    sample_size: Optional[int],
    seed: int,
) -> Tuple[List[str], np.ndarray]:
    """Load embeddings + identifiers from NPZ or H5."""
    if not path.exists():
        raise FileNotFoundError(f"Embedding file not found: {path}")

    ids: List[str]
    embeddings: np.ndarray

    if path.suffix.lower() == ".npz":
        data = np.load(path)
        if dataset_key not in data:
            raise KeyError(f"Dataset key '{dataset_key}' not present in NPZ")
        embeddings = data[dataset_key]
        ids_key = "protein_ids" if "protein_ids" in data else id_key
        if ids_key is None or ids_key not in data:
            raise KeyError("NPZ must contain 'protein_ids' or specify --id-key")
        ids = _decode_bytes(data[ids_key])
    elif path.suffix.lower() in {".h5", ".hdf5"}:
        with h5py.File(path, "r") as h5:
            if dataset_key not in h5:
                raise KeyError(f"Dataset key '{dataset_key}' not present in H5")
            embeddings = h5[dataset_key][:]
            if id_key and id_key in h5:
                ids = _decode_bytes(h5[id_key][:])
            else:
                # fallback to sequential IDs
                ids = [f"item_{i}" for i in range(len(embeddings))]
    else:
        raise ValueError(f"Unsupported embedding format: {path.suffix}")

    embeddings = np.asarray(embeddings, dtype=np.float32)

    if sample_size is not None and sample_size < len(ids):
        rng = np.random.default_rng(seed)
        sample_idx = np.sort(rng.choice(len(ids), size=sample_size, replace=False))
        ids = [ids[i] for i in sample_idx]
        embeddings = embeddings[sample_idx]

    return ids, embeddings


def _dijkstra(adjacency: Dict[str, List[Tuple[str, float]]], start: str) -> Dict[str, float]:
    import heapq

    distances: Dict[str, float] = {start: 0.0}
    heap: List[Tuple[float, str]] = [(0.0, start)]

    while heap:
        dist, node = heapq.heappop(heap)
        if dist > distances[node] + 1e-12:
            continue
        for neighbor, weight in adjacency.get(node, []):
            cand = dist + weight
            if neighbor not in distances or cand < distances[neighbor] - 1e-12:
                distances[neighbor] = cand
                heapq.heappush(heap, (cand, neighbor))
    return distances


def compute_tree_condensed_distances(node_table) -> np.ndarray:
    """Compute condensed matrix of path lengths between leaves."""
    adjacency: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    leaves: List = []
    for rec in node_table:
        adjacency.setdefault(rec.node_id, [])
        if rec.is_leaf:
            leaves.append(rec)
        if rec.parent_id is not None and rec.branch_length is not None:
            w = float(rec.branch_length)
            adjacency[rec.node_id].append((rec.parent_id, w))
            adjacency[rec.parent_id].append((rec.node_id, w))

    # sort leaves by original index to match embedding order
    leaves_sorted = sorted(leaves, key=lambda r: r.members[0])
    m = len(leaves_sorted)
    condensed = np.zeros(m * (m - 1) // 2, dtype=np.float32)
    idx = 0
    for i, leaf in enumerate(leaves_sorted):
        distances = _dijkstra(adjacency, leaf.node_id)
        for j in range(i + 1, m):
            condensed[idx] = float(distances[leaves_sorted[j].node_id])
            idx += 1
    return condensed, leaves_sorted


def assign_clusters_from_tree(node_table, target_clusters: int, n_samples: int) -> np.ndarray:
    nodes = {rec.node_id: rec for rec in node_table}
    children: Dict[str, List[str]] = defaultdict(list)
    root_id: Optional[str] = None
    for rec in node_table:
        if rec.parent_id is None:
            root_id = rec.node_id
        else:
            children[rec.parent_id].append(rec.node_id)
    if root_id is None:
        raise ValueError("Tree has no root node")

    # Start from the root and recursively split the largest clusters until target reached
    active: List = [nodes[root_id]]
    while len(active) < target_clusters:
        candidates = [c for c in active if not c.is_leaf and len(children[c.node_id]) >= 2]
        if not candidates:
            break
        # pick largest cluster to split
        largest = max(candidates, key=lambda c: c.size)
        active.remove(largest)
        for child_id in children[largest.node_id]:
            active.append(nodes[child_id])

    assignments = np.zeros(n_samples, dtype=np.int32)
    for cluster_idx, cluster in enumerate(active):
        for member_idx in cluster.members:
            assignments[member_idx] = cluster_idx
    return assignments


def compute_nj_metrics(
    artifacts: TreeArtifacts,
    cosine_condensed: np.ndarray,
    embeddings_whitened: np.ndarray,
    target_clusters: int,
) -> Dict[str, float]:
    tree_condensed, leaves_sorted = compute_tree_condensed_distances(artifacts.node_table)
    order = [rec.members[0] for rec in leaves_sorted]

    cosine_square = squareform(cosine_condensed)
    cosine_square = cosine_square[np.ix_(order, order)]
    cosine_reordered = squareform(cosine_square)

    spearman_corr, _ = spearmanr(cosine_reordered, tree_condensed)

    assignments = assign_clusters_from_tree(artifacts.node_table, target_clusters, embeddings_whitened.shape[0])
    assignments_ordered = assignments[order]
    unique_clusters = np.unique(assignments_ordered)

    silhouette = float("nan")
    davies = float("nan")
    if unique_clusters.size >= 2:
        emb_reordered = embeddings_whitened[order]
        silhouette = float(silhouette_score(emb_reordered, assignments_ordered, metric="euclidean"))
        davies = float(davies_bouldin_score(emb_reordered, assignments_ordered))

    return {
        "spearman_correlation": float(spearman_corr),
        "num_clusters": int(unique_clusters.size),
        "silhouette_score": silhouette,
        "davies_bouldin_index": davies,
    }


def compute_ward_metrics(embeddings_whitened: np.ndarray, target_clusters: int) -> Dict[str, float]:
    distances = pdist(embeddings_whitened, metric="euclidean")
    linkage_matrix = linkage(embeddings_whitened, method="ward")
    cophenetic_corr = cophenet(linkage_matrix, distances)[0]
    clusters = fcluster(linkage_matrix, t=target_clusters, criterion="maxclust")
    unique_clusters = np.unique(clusters)

    silhouette = float("nan")
    davies = float("nan")
    if unique_clusters.size >= 2:
        silhouette = float(silhouette_score(embeddings_whitened, clusters, metric="euclidean"))
        davies = float(davies_bouldin_score(embeddings_whitened, clusters))

    return {
        "cophenetic_correlation": float(cophenetic_corr),
        "num_clusters": int(unique_clusters.size),
        "silhouette_score": silhouette,
        "davies_bouldin_index": davies,
    }


def run_validation(args: argparse.Namespace) -> Dict:
    ids, embeddings = load_embeddings(
        path=args.input_path,
        dataset_key=args.dataset_key,
        id_key=args.id_key,
        sample_size=args.sample_size,
        seed=args.seed,
    )

    start = time.time()
    artifacts = build_tree(
        embeddings,
        method="nj",
        anisotropy_correction=True,
        zca_whiten=args.zca_whiten,
        labels=ids,
    )
    nj_runtime = time.time() - start

    output_dir = args.output_dir
    artifact_name = args.artifact_name or f"nj_tree_{len(ids)}"
    newick_path, node_table_path = save_tree_artifacts(
        artifacts.newick,
        artifacts.node_table,
        out_dir=str(output_dir),
        base_name=artifact_name,
    )

    embeddings_whitened = center_and_whiten(embeddings, whiten=args.zca_whiten)
    cosine_condensed = cosine_distance_matrix(embeddings_whitened)

    nj_metrics = compute_nj_metrics(
        artifacts=artifacts,
        cosine_condensed=cosine_condensed,
        embeddings_whitened=embeddings_whitened,
        target_clusters=args.target_clusters,
    )

    ward_metrics = compute_ward_metrics(
        embeddings_whitened=embeddings_whitened,
        target_clusters=args.target_clusters,
    )

    config = {
        "input_path": str(args.input_path),
        "sample_size": len(ids),
        "target_clusters": args.target_clusters,
        "zca_whiten": args.zca_whiten,
        "seed": args.seed,
    }

    return {
        "config": config,
        "artifacts": {
            "newick_path": newick_path,
            "node_table_path": node_table_path,
            "nj_runtime_sec": nj_runtime,
        },
        "nj_metrics": nj_metrics,
        "ward_baseline": ward_metrics,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build NJ tree and validate against Ward baseline")
    parser.add_argument("--input-path", type=Path, default=DEFAULT_NETWORK_H5, help="Path to embeddings H5/NPZ")
    parser.add_argument("--dataset-key", type=str, default="embeddings", help="Dataset key in H5/NPZ")
    parser.add_argument("--id-key", type=str, default="proteins", help="Identifier key in H5 (ignored for NPZ)")
    parser.add_argument("--sample-size", type=int, default=1500, help="Optional subsample for metrics")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for subsampling")
    parser.add_argument("--target-clusters", type=int, default=50, help="Number of clusters for evaluation")
    parser.add_argument("--zca-whiten", action="store_true", help="Apply ZCA whitening before NJ")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to persist artifacts")
    parser.add_argument("--artifact-name", type=str, default=None, help="Base name for saved tree artifacts")
    parser.add_argument(
        "--metrics-path",
        type=Path,
        default=None,
        help="Optional JSON path to store validation metrics",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics = run_validation(args)

    metrics_path = args.metrics_path or (args.output_dir / "nj_validation_metrics.json")
    with open(metrics_path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)

    print("NJ tree validation complete. Metrics saved to", metrics_path)
    print(json.dumps(metrics, indent=2)[:2000])


if __name__ == "__main__":
    main()
