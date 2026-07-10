#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSLS re-ranking utilities to mitigate hubness in high-dimensional embedding retrieval.

Reference: Conneau et al., 2018 (MUSE / CSLS) – s_csls(q, x) = 2*cos(q,x) - r_C(q) - r_C(x)
"""
from __future__ import annotations

import numpy as np
from typing import Tuple


def normalize_rows(X: np.ndarray, eps: float = 1e-8) -> np.ndarray:
    norms = np.linalg.norm(X, axis=1, keepdims=True) + eps
    return X / norms


def csls_scores(query: np.ndarray, candidates: np.ndarray, k_avg: int = 10) -> np.ndarray:
    """
    Compute CSLS-adjusted similarity scores for a single query against candidates.

    Args:
        query: shape (d,)
        candidates: shape (m, d)
        k_avg: neighborhood size for r_C(.) averaging

    Returns:
        np.ndarray shape (m,) with CSLS scores
    """
    q = query.astype(np.float32, copy=False)
    C = candidates.astype(np.float32, copy=False)
    q = q / (np.linalg.norm(q) + 1e-8)
    C = normalize_rows(C)

    # Cosine similarities
    sims = C @ q  # (m,)

    # r_C(q): average of top-k_avg sims among candidates
    if k_avg > 0 and k_avg <= C.shape[0]:
        rq = float(np.partition(sims, -k_avg)[-k_avg:].mean())
    else:
        rq = float(sims.mean())

    # r_C(x): for each candidate, average similarity to its k_avg nearest in C
    # For efficiency, approximate by using dot-products within C (can be optimized further)
    # Compute top-k_avg mean for each row efficiently with partial sorting
    CC = C @ C.T  # (m, m)
    np.fill_diagonal(CC, -np.inf)
    if k_avg > 0 and k_avg <= C.shape[0] - 1:
        # take k_avg largest per row
        idx = np.argpartition(CC, -k_avg, axis=1)[:, -k_avg:]
        row_means = CC[np.arange(C.shape[0])[:, None], idx].mean(axis=1)
    else:
        row_means = CC.mean(axis=1)

    # s_csls = 2*sims - rq - rC(x)
    return 2.0 * sims - rq - row_means.astype(np.float32)


def csls_rerank(query: np.ndarray, candidates: np.ndarray, topk: int = 10, k_avg: int = 10) -> Tuple[np.ndarray, np.ndarray]:
    scores = csls_scores(query, candidates, k_avg=k_avg)
    topk = min(topk, scores.shape[0])
    idx = np.argpartition(scores, -topk)[-topk:]
    # sort descending among topk
    order = idx[np.argsort(scores[idx])[::-1]]
    return order, scores[order]
