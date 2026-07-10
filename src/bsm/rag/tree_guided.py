#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tree-guided retriever: navigate embedding-derived tree to select a small subtree,
run ANN within that subtree (via Milvus/Zilliz), and re-rank candidates using CSLS.

This is a minimal skeleton aligning with bsm.rag package. Wire Milvus client and
tree index structure in follow-ups.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

import numpy as np

from ..embeddings.csls import csls_rerank
from ..embeddings.tree_index import TreeIndex, TreeNode


@dataclass
class RetrievalResult:
    doc_id: str
    score: float
    metadata: Dict[str, Any]


class TreeGuidedRetriever:
    def __init__(
        self,
        tree_index: Any,
        node_table: Any,
        milvus_client: Any = None,
        collection_name: Optional[str] = None,
        depth_penalty: float = 0.0,
        csls_k: int = 10,
        subtree_max_size: int = 2000,
        resolve_ids: Optional[Callable[[List[int]], List[str]]] = None,
    ) -> None:
        self.tree_index: TreeIndex = tree_index
        self.node_table = node_table
        self.milvus = milvus_client
        self.collection_name = collection_name
        self.depth_penalty = float(depth_penalty)
        self.csls_k = int(csls_k)
        self.subtree_max_size = int(subtree_max_size)
        self.resolve_ids = resolve_ids

    def _traverse_to_subtree(self, query_vec: np.ndarray, max_size: Optional[int] = None) -> TreeNode:
        """Centroid-guided descent to a subtree with bounded size."""
        limit = max_size if max_size is not None else self.subtree_max_size
        return self.tree_index.centroid_descent(query_vec, max_size=limit)

    def _ann_in_subtree(self, subtree: Any, query_vec: np.ndarray, k: int) -> Tuple[List[str], np.ndarray]:
        """Retrieve candidates within subtree.

        If Milvus client and id resolver are provided, perform constrained search
        within subtree protein IDs and return top-k IDs (vectors not used in this path).
        Otherwise, fall back to in-memory vectors on the subtree node.
        """
        # Milvus path (no CSLS; use backend scores later)
        if self.milvus is not None and self.resolve_ids is not None and hasattr(subtree, "record"):
            try:
                member_idxs = list(getattr(subtree.record, "members", []))
                candidate_ids = self.resolve_ids(member_idxs)
                # Oversample before final selection
                kk = max(k * 5, k)
                # Async interface expected; if using sync wrapper, adapt accordingly
                import asyncio

                async def _search():
                    return await self.milvus.search_within_ids(query_vec, candidate_ids, k=kk)

                results = asyncio.get_event_loop().run_until_complete(_search())
                ids = [r.protein_id for r in results]
                # Vectors not available in this path
                return ids, np.zeros((0, query_vec.shape[-1]), dtype=np.float32)
            except Exception:
                # Fallback to in-memory if Milvus path fails
                pass

        # In-memory fallback
        ids = getattr(subtree, "ids", [])
        vecs = getattr(subtree, "vectors", np.zeros((0, query_vec.shape[-1]), dtype=np.float32))
        return ids[: max(k * 5, k)], vecs[: max(k * 5, k)]  # oversample before CSLS

    def _apply_depth_penalty(self, scores: np.ndarray, depth: int) -> np.ndarray:
        if self.depth_penalty <= 0 or depth <= 0:
            return scores
        return scores - self.depth_penalty * float(depth)

    def retrieve(self, query_vec: np.ndarray, k: int = 10) -> List[RetrievalResult]:
        subtree = self._traverse_to_subtree(query_vec)
        ids, vecs = self._ann_in_subtree(subtree, query_vec, k=k)
        if len(ids) == 0:
            return []
        results: List[RetrievalResult] = []
        depth = getattr(subtree, "depth", 0)
        # If vectors available, use CSLS re-ranking
        if vecs is not None and vecs.shape[0] > 0:
            idx, scores = csls_rerank(query_vec, vecs, topk=k, k_avg=self.csls_k)
            scores = self._apply_depth_penalty(scores, depth)
            for i, s in zip(idx, scores):
                if i < len(ids):
                    results.append(
                        RetrievalResult(doc_id=str(ids[i]), score=float(s), metadata={"subtree_depth": depth})
                    )
            return results
        # Else, assume Milvus scores were already top-k; return with placeholder scores
        # Depth penalty cannot be applied without scores; keep metadata
        for pid in ids[:k]:
            results.append(RetrievalResult(doc_id=str(pid), score=0.0, metadata={"subtree_depth": depth}))
        return results
