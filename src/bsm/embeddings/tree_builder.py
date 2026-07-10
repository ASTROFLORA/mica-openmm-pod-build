#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tree Builder for Embedding-Derived Phylogenies (NJ/BioNJ/FastME/RapidNJ)

Minimal, pluggable skeleton to construct a tree from embedding distances.
Implements: anisotropy correction (centering/ZCA optional), cosine distances,
backend switch, and artifact persistence contracts (Newick + node table).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Sequence, Literal

import numpy as np

logger = logging.getLogger(__name__)


Method = Literal["nj", "bionj", "fastme", "rapidnj"]


@dataclass
class TreeArtifacts:
    newick: str
    node_table: "NodeTable"


@dataclass
class NodeRecord:
    node_id: str
    parent_id: Optional[str]
    is_leaf: bool
    members: List[int]
    size: int
    centroid: Optional[np.ndarray]
    depth: int
    branch_length: Optional[float] = None
    bootstrap_support: Optional[float] = None
    annotations: Optional[Dict[str, str]] = None


NodeTable = List[NodeRecord]


def center_and_whiten(X: np.ndarray, whiten: bool = False) -> np.ndarray:
    """Center embeddings; optionally apply ZCA whitening (placeholder)."""
    Xc = X - X.mean(axis=0, keepdims=True)
    if not whiten:
        return Xc
    # Placeholder: fast ZCA; can be replaced with sklearn or custom SVD
    U, S, Vt = np.linalg.svd(np.cov(Xc, rowvar=False) + 1e-6 * np.eye(Xc.shape[1]))
    ZCA = U @ np.diag(1.0 / np.sqrt(S + 1e-6)) @ U.T
    return (Xc @ ZCA).astype(np.float32)


def cosine_distance_matrix(X: np.ndarray, batch: int = 8192) -> np.ndarray:
    """Compute condensed cosine distance matrix in batches.

    Returns a condensed vector (size n*(n-1)/2) to save memory.
    """
    X = X.astype(np.float32, copy=False)
    norms = np.linalg.norm(X, axis=1, keepdims=True) + 1e-8
    Xn = X / norms
    n = Xn.shape[0]
    # Pre-allocate condensed distances
    m = n * (n - 1) // 2
    D = np.empty(m, dtype=np.float32)
    idx = 0
    for i in range(n):
        # dot with block to keep cache-friendly
        sims = (Xn[i + 1 :] @ Xn[i].astype(np.float32))
        dists = (1.0 - sims).astype(np.float32)
        L = n - i - 1
        D[idx : idx + L] = dists
        idx += L
    return D


def _condensed_to_square(D: np.ndarray, labels: Optional[Sequence[str]] = None) -> Tuple[np.ndarray, List[str]]:
    """Convert a condensed distance vector to a full square matrix.

    Args:
        D: Condensed distances of shape (n*(n-1)/2,)
        labels: Optional labels; if provided, must have length n

    Returns:
        (square distances (n,n), labels list)
    """
    # Infer n from m = n*(n-1)/2
    m = int(D.shape[0])
    # Solve n^2 - n - 2m = 0
    n = int((1 + np.sqrt(1 + 8 * m)) // 2)
    DM = np.zeros((n, n), dtype=np.float32)
    idx = 0
    for i in range(n - 1):
        L = n - i - 1
        DM[i, i + 1 :] = D[idx : idx + L]
        DM[i + 1 :, i] = D[idx : idx + L]
        idx += L
    lab = list(labels) if labels is not None else [str(i) for i in range(n)]
    return DM, lab


def build_tree(
    embeddings: np.ndarray,
    method: Method = "nj",
    anisotropy_correction: bool = True,
    zca_whiten: bool = False,
    precomputed_condensed: Optional[np.ndarray] = None,
    labels: Optional[Sequence[str]] = None,
) -> TreeArtifacts:
    """Build a tree from embeddings with a pluggable backend.

    Note: This is an initial skeleton. NJ/BioNJ/FastME/RapidNJ backends are
    represented as placeholders; integrate scikit-bio, FastME CLI, or RapidNJ
    binary as needed.
    """
    X = embeddings
    if anisotropy_correction:
        X = center_and_whiten(X, whiten=zca_whiten)

    D = precomputed_condensed if precomputed_condensed is not None else cosine_distance_matrix(X)

    # Dispatch to backend (minimal placeholder implementations)
    if method in ("nj", "bionj"):
        newick, node_table = _build_tree_nj_bionj(D, labels=labels, variant=method, embeddings_X=X)
    elif method == "fastme":
        newick, node_table = _build_tree_fastme(D, labels=labels)
    elif method == "rapidnj":
        newick, node_table = _build_tree_rapidnj(D, labels=labels)
    else:
        raise ValueError(f"Unsupported method: {method}")

    return TreeArtifacts(newick=newick, node_table=node_table)

class _NJNode:
    __slots__ = ("name", "left", "right", "len_left", "len_right", "members", "depth")

    def __init__(
        self,
        name: str,
        left: Optional["_NJNode"] = None,
        right: Optional["_NJNode"] = None,
        len_left: float = 0.0,
        len_right: float = 0.0,
        members: Optional[List[int]] = None,
        depth: int = 0,
    ) -> None:
        self.name = name
        self.left = left
        self.right = right
        self.len_left = float(len_left)
        self.len_right = float(len_right)
        self.members = members or []
        self.depth = depth

    def is_leaf(self) -> bool:
        return self.left is None and self.right is None

    def to_newick(self) -> str:
        if self.is_leaf():
            return self.name
        left_part = f"{self.left.to_newick()}:{max(self.len_left, 0.0):.6f}"
        right_part = f"{self.right.to_newick()}:{max(self.len_right, 0.0):.6f}"
        return f"({left_part},{right_part})"


def _build_tree_nj_bionj(
    D: np.ndarray,
    labels: Optional[Sequence[str]],
    variant: str,
    embeddings_X: Optional[np.ndarray] = None,
) -> Tuple[str, NodeTable]:
    """Neighbor Joining (NJ) backend. BioNJ falls back to NJ for now.

    Args:
        D: condensed distance vector of shape (n*(n-1)/2,)
        labels: optional sequence of labels length n
        variant: "nj" or "bionj" (treated the same here)
        embeddings_X: optional embeddings to compute centroids per node

    Returns:
        (Newick string with branch lengths, NodeTable with membership and centroids)
    """
    DM, lab = _condensed_to_square(D)
    n = DM.shape[0]
    if labels is not None:
        if len(labels) != n:
            raise ValueError("labels length must match number of items")
        lab = list(labels)

    # Initialize active nodes and bookkeeping
    nodes: List[_NJNode] = [_NJNode(name=lab[i], members=[i], depth=0) for i in range(n)]
    active_idx = list(range(n))  # indices into nodes list
    dist = DM.copy()
    np.fill_diagonal(dist, 0.0)

    # Main NJ loop
    next_internal_id = 0
    while len(active_idx) > 2:
        m = len(active_idx)
        # Row sums r_i
        r = dist[np.ix_(active_idx, active_idx)].sum(axis=1)
        # Build Q-matrix
        sub = dist[np.ix_(active_idx, active_idx)]
        Q = (m - 2) * sub - r[:, None] - r[None, :]
        np.fill_diagonal(Q, np.inf)
        # Find pair (i,j) with minimal Q
        min_pos = np.unravel_index(np.argmin(Q), Q.shape)
        ai, aj = active_idx[min_pos[0]], active_idx[min_pos[1]]

        # Compute branch lengths
        dij = dist[ai, aj]
        if m - 2 <= 0:
            delta = 0.0
        else:
            delta = (r[min_pos[0]] - r[min_pos[1]]) / (m - 2)
        li = 0.5 * (dij + delta)
        lj = max(dij - li, 0.0)

        # Create new internal node
        u_name = f"u{next_internal_id}"
        next_internal_id += 1
        depth = max(nodes[ai].depth, nodes[aj].depth) + 1
        u_node = _NJNode(name=u_name, left=nodes[ai], right=nodes[aj], len_left=li, len_right=lj,
                         members=sorted(set(nodes[ai].members + nodes[aj].members)), depth=depth)

        # Update distances: d(u,k) = 0.5*(d(i,k) + d(j,k) - d(i,j))
        for idx_k, ak in enumerate(active_idx):
            if ak in (ai, aj):
                continue
            dik = dist[ai, ak]
            djk = dist[aj, ak]
            duk = 0.5 * (dik + djk - dij)
            dist = _set_symmetric(dist, u_node_idx=len(nodes), k_idx=ak, value=duk)

        # Remove i, j from active set and add u
        active_idx = [x for x in active_idx if x not in (ai, aj)]
        nodes.append(u_node)
        active_idx.append(len(nodes) - 1)

    # Final join
    ai, aj = active_idx[0], active_idx[1]
    dij = dist[ai, aj]
    root_name = f"u{next_internal_id}"
    li = lj = max(dij / 2.0, 0.0)
    depth = max(nodes[ai].depth, nodes[aj].depth) + 1
    root = _NJNode(name=root_name, left=nodes[ai], right=nodes[aj], len_left=li, len_right=lj,
                   members=sorted(set(nodes[ai].members + nodes[aj].members)), depth=depth)

    newick = root.to_newick() + ";"

    # Build node table by traversal
    node_table: NodeTable = []
    def _traverse(node: _NJNode, parent_id: Optional[str], branch_length: Optional[float]) -> None:
        nonlocal node_table
        is_leaf = node.is_leaf()
        centroid = None
        if embeddings_X is not None and len(node.members) > 0:
            centroid = embeddings_X[node.members].mean(axis=0)
        node_table.append(
            NodeRecord(
                node_id=node.name,
                parent_id=parent_id,
                is_leaf=is_leaf,
                members=list(node.members),
                size=len(node.members),
                centroid=centroid,
                depth=node.depth,
                branch_length=branch_length,
                bootstrap_support=None,
                annotations=None,
            )
        )
        if node.left is not None:
            _traverse(node.left, parent_id=node.name, branch_length=node.len_left)
        if node.right is not None:
            _traverse(node.right, parent_id=node.name, branch_length=node.len_right)

    _traverse(root, parent_id=None, branch_length=None)
    return newick, node_table


def _set_symmetric(dist: np.ndarray, u_node_idx: int, k_idx: int, value: float) -> np.ndarray:
    # Ensure dist can grow to accommodate new node index
    n = dist.shape[0]
    if u_node_idx >= n:
        # Expand matrix by one row/col filled with zeros
        new = np.zeros((n + 1, n + 1), dtype=dist.dtype)
        new[:n, :n] = dist
        dist = new
    dist[u_node_idx, k_idx] = value
    dist[k_idx, u_node_idx] = value
    return dist


def _build_tree_fastme(D: np.ndarray, labels: Optional[Sequence[str]]) -> Tuple[str, NodeTable]:
    logger.warning("FastME backend is a placeholder; integrate FastME CLI.")
    newick = "(A:0.1,B:0.1,(C:0.1,D:0.1):0.05);"
    return newick, []


def _build_tree_rapidnj(D: np.ndarray, labels: Optional[Sequence[str]]) -> Tuple[str, NodeTable]:
    logger.warning("RapidNJ backend is a placeholder; integrate RapidNJ binary.")
    newick = "(A:0.1,B:0.1,(C:0.1,D:0.1):0.05);"
    return newick, []

