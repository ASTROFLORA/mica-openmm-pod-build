#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TreeIndex utilities for embedding-derived trees.

- Build an in-memory index from NodeTable (produced by tree_builder.build_tree)
  with parent/children relationships, quick member lookup, and centroids.
- Provide a simple centroid-guided descent to find a subtree under a max size.
- Persist/Load artifacts: Newick + NodeTable (JSON) under artifacts dir.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .tree_builder import NodeRecord, NodeTable

logger = logging.getLogger(__name__)


class TreeNode:
    def __init__(self, record: NodeRecord) -> None:
        self.record = record
        self.children: List["TreeNode"] = []
        self.parent: Optional["TreeNode"] = None

    @property
    def id(self) -> str:
        return self.record.node_id

    @property
    def depth(self) -> int:
        return self.record.depth

    @property
    def size(self) -> int:
        return self.record.size

    @property
    def centroid(self) -> Optional[np.ndarray]:
        return self.record.centroid

    @property
    def members(self) -> List[int]:
        return self.record.members

    @property
    def branch_length(self) -> Optional[float]:
        return self.record.branch_length


class TreeIndex:
    def __init__(self, node_table: NodeTable) -> None:
        self.nodes: Dict[str, TreeNode] = {}
        for rec in node_table:
            self.nodes[rec.node_id] = TreeNode(rec)
        # link parents and children
        self.root: Optional[TreeNode] = None
        for rec in node_table:
            node = self.nodes[rec.node_id]
            if rec.parent_id is None:
                self.root = node
            else:
                parent = self.nodes.get(rec.parent_id)
                if parent is None:
                    logger.warning(f"Parent {rec.parent_id} not found for node {rec.node_id}")
                else:
                    node.parent = parent
                    parent.children.append(node)
        if self.root is None and node_table:
            # Fallback: pick the shallowest node as root
            self.root = min(self.nodes.values(), key=lambda n: n.depth)

    def centroid_descent(self, query_vec: np.ndarray, max_size: int) -> TreeNode:
        """Greedy descent picking the child whose centroid is most similar to query.

        Stops when node.size <= max_size or leaf reached.
        """
        if self.root is None:
            raise ValueError("TreeIndex has no root")
        q = query_vec.astype(np.float32, copy=False)
        q /= (np.linalg.norm(q) + 1e-8)
        node = self.root
        while node.children and node.size > max_size:
            # choose best child by cosine similarity to centroid (fallback to first if None)
            best: Optional[TreeNode] = None
            best_sim = -1e9
            for ch in node.children:
                c = ch.centroid
                if c is None:
                    cand = ch
                    sim = -1e9
                else:
                    c = c.astype(np.float32, copy=False)
                    c /= (np.linalg.norm(c) + 1e-8)
                    sim = float(np.dot(q, c))
                    cand = ch
                if sim > best_sim:
                    best_sim = sim
                    best = cand
            node = best or node.children[0]
        return node


def save_tree_artifacts(newick: str, node_table: NodeTable, out_dir: str, base_name: str) -> Tuple[str, str]:
    """Persist Newick and NodeTable JSON to artifacts directory.

    Returns: (newick_path, node_table_path)
    """
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    newick_path = d / f"{base_name}.nwk"
    node_path = d / f"{base_name}.nodes.json"
    newick_path.write_text(newick, encoding="utf-8")
    # serialize NodeTable with numpy arrays converted to lists
    serializable = []
    for rec in node_table:
        dct = asdict(rec)
        if dct.get("centroid", None) is not None:
            dct["centroid"] = np.asarray(dct["centroid"]).astype(float).tolist()
        serializable.append(dct)
    node_path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")
    return str(newick_path), str(node_path)


def load_node_table(json_path: str) -> NodeTable:
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    table: NodeTable = []
    for dct in data:
        centroid = dct.get("centroid")
        arr = np.array(centroid, dtype=np.float32) if centroid is not None else None
        table.append(
            NodeRecord(
                node_id=dct["node_id"],
                parent_id=dct.get("parent_id"),
                is_leaf=bool(dct["is_leaf"]),
                members=list(dct["members"]),
                size=int(dct["size"]),
                centroid=arr,
                depth=int(dct["depth"]),
                branch_length=dct.get("branch_length"),
                bootstrap_support=dct.get("bootstrap_support"),
                annotations=dct.get("annotations"),
            )
        )
    return table
