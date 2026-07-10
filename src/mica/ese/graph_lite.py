"""
ese.graph_lite — Deterministic ESE-Graph-Lite (Level C) builder.

Builds a static contact graph from amino acid sequence only.
No model inference required — always available as a fallback.

Enables:
  - Dynamic-neighbor search (approximate)
  - Family clustering
  - Atlas indexing as a structural prior
  - Genesis candidate pre-ranking
  - CG/AA job selection

Does NOT enable (J.12.3):
  - Allosteric routes (causal)
  - Contact lifetime / transient contacts
  - Conformational barriers / kinetics
  - Open/closed state validation
  - CG/AA or experimental validation substitute
"""
from __future__ import annotations

import math
from typing import List, Optional

from .contracts import (
    EseGraphLiteEdge,
    EseGraphLiteNode,
    EseGraphLitePayload,
    EseGraphLiteSummary,
    EseLitePayload,
)


# ── Amino acid class mapping ───────────────────────────────────────────────

_AA_CLASS: dict[str, str] = {
    # Hydrophobic
    **{aa: "hydrophobic" for aa in "AILMFWYV"},
    # Polar uncharged
    **{aa: "polar" for aa in "STCNQ"},
    # Charged
    **{aa: "charged" for aa in "DEKRH"},
    # Special
    "G": "special",
    "P": "special",
}


def _aa_class(aa: str) -> str:
    return _AA_CLASS.get(aa.upper(), "unknown")


# ── Contact heuristics ────────────────────────────────────────────────────

_SHORT_RANGE = 6      # |i−j| < 6
_MEDIUM_RANGE = 12    # 6 ≤ |i−j| < 12 → local secondary structure
_LONG_RANGE = 24      # |i−j| ≥ 24 → domain contacts


def _distance_bin(sep: int) -> str:
    if sep < _MEDIUM_RANGE:
        return "short"
    if sep < _LONG_RANGE:
        return "medium"
    return "long"


def _seq_sep_bin(sep: int) -> str:
    if sep <= 5:
        return "local"
    if sep <= 23:
        return "medium_range"
    return "long_range"


def _contact_confidence(sep: int, aa_i: str, aa_j: str) -> float:
    """Heuristic confidence for a sequence-derived contact."""
    base = 0.5
    # Long-range hydrophobic contacts are more speculative
    if sep >= _LONG_RANGE and _aa_class(aa_i) == "hydrophobic" and _aa_class(aa_j) == "hydrophobic":
        return min(0.65, base + 0.15)
    if sep < _MEDIUM_RANGE:
        return min(0.7, base + 0.2)
    return base


# ── Betweenness proxy (degree centrality as cheap stand-in) ───────────────

def _hinge_candidates(degree: List[int], n: int, top_k_frac: float = 0.05) -> int:
    """Count residues whose degree is in the top fraction — hinge-like proxy."""
    if n == 0:
        return 0
    threshold = sorted(degree, reverse=True)[max(0, int(n * top_k_frac) - 1)]
    return sum(1 for d in degree if d >= threshold)


# ── Main builder ──────────────────────────────────────────────────────────

def build_ese_graph_lite(
    sequence: str,
    ese_lite: Optional[EseLitePayload] = None,
    *,
    contact_window: int = 3,            # connect all residues within ±window positions
    long_range_every_n: int = 8,        # sample long-range contacts every N residues
) -> EseGraphLitePayload:
    """
    Build a deterministic ESE-Graph-Lite from sequence (+ optional ese_lite features).

    Args:
        sequence: Single-letter amino acid sequence.
        ese_lite: Optional ESE-Lite payload to fold in per-residue flexibility/confidence.
        contact_window: All residues within ±window are connected (backbone contacts).
        long_range_every_n: Sample long-range contacts between every N-th residue pair.

    Returns:
        EseGraphLitePayload (artifact_kind="ese_graph_lite")
    """
    seq = sequence.upper().strip()
    n = len(seq)

    # --- Build per-residue flexibility index from ese_lite (if available)
    flex_map: dict[int, float] = {}
    conf_map: dict[int, float] = {}
    if ese_lite:
        for rf in ese_lite.residue_features:
            if rf.flexibility is not None:
                flex_map[rf.residue_index] = rf.flexibility
            if rf.confidence is not None:
                conf_map[rf.residue_index] = rf.confidence

    # --- Compute z-scored flexibility
    if flex_map:
        vals = list(flex_map.values())
        mean_f = sum(vals) / len(vals)
        std_f = math.sqrt(sum((v - mean_f) ** 2 for v in vals) / max(1, len(vals))) + 1e-8
        flex_z_map = {k: (v - mean_f) / std_f for k, v in flex_map.items()}
    else:
        flex_z_map = {}

    # --- Nodes
    nodes = [
        EseGraphLiteNode(
            residue_index=i,
            aa=seq[i],
            aa_class=_aa_class(seq[i]),
            ese_lite_flexibility_z=flex_z_map.get(i),
            ese_lite_confidence=conf_map.get(i),
        )
        for i in range(n)
    ]

    # --- Edges: backbone contacts (within window)
    edges: list[EseGraphLiteEdge] = []
    degree: list[int] = [0] * n
    edge_set: set[tuple[int, int]] = set()

    def _add_edge(i: int, j: int) -> None:
        key = (min(i, j), max(i, j))
        if key in edge_set:
            return
        edge_set.add(key)
        sep = abs(i - j)
        edges.append(EseGraphLiteEdge(
            source_residue=i,
            target_residue=j,
            distance_bin=_distance_bin(sep),
            sequence_separation_bin=_seq_sep_bin(sep),
            confidence=_contact_confidence(sep, seq[i], seq[j]),
        ))
        degree[i] += 1
        degree[j] += 1

    # Backbone contacts within window
    for i in range(n):
        for j in range(i + 1, min(n, i + contact_window + 1)):
            _add_edge(i, j)

    # Sampled long-range contacts
    if n > _LONG_RANGE:
        for i in range(0, n, long_range_every_n):
            for j in range(i + _LONG_RANGE, n, long_range_every_n):
                _add_edge(i, j)

    # --- Summary
    num_possible = n * (n - 1) // 2 if n > 1 else 1
    long_range_edges = sum(1 for e in edges if e.sequence_separation_bin == "long_range")
    hinge_count = _hinge_candidates(degree, n)
    mean_degree = sum(degree) / n if n else 0.0

    flex_vals = list(flex_map.values())
    flex_mean = sum(flex_vals) / len(flex_vals) if flex_vals else None
    flex_std = (
        math.sqrt(sum((v - flex_mean) ** 2 for v in flex_vals) / max(1, len(flex_vals)))
        if flex_vals and flex_mean is not None
        else None
    )

    summary = EseGraphLiteSummary(
        contact_density=len(edges) / num_possible,
        mean_degree=mean_degree,
        long_range_contact_fraction=long_range_edges / max(1, len(edges)),
        hinge_candidate_count=hinge_count,
        flexibility_mean=flex_mean,
        flexibility_std=flex_std,
    )

    return EseGraphLitePayload(
        sequence=seq,
        nodes=nodes,
        edges=edges,
        summary=summary,
        derived_from_ese_lite=bool(ese_lite),
    )
