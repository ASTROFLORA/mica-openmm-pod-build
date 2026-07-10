"""
ese.contracts — Typed contracts for ESE wrapper artifacts (P3).

Three levels (from roadmap J.12.2):
  A · ese_lite          ESMDance → per-residue flexibility + global embedding (ships first)
  B · ese_graph_inference  ESMDynamic → pairwise/contact map (blocked until weights exist)
  C · ese_graph_lite    Deterministic static contact graph fallback (always available)

`artifact://` is the canonical source of truth.  `ese://` is only a semantic alias.
No ESE storage parallel to ArtifactContract / MUDO.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── ESE-Lite (Level A) ─────────────────────────────────────────────────────

class EseLiteResidueFeature(BaseModel):
    """Per-residue features from ESMDance inference."""
    residue_index: int
    aa: str
    flexibility: Optional[float] = None         # 0–1 proxy from model output
    confidence: Optional[float] = None          # model per-residue confidence
    embedding: Optional[List[float]] = None     # optional per-residue embedding slice


class EseLitePayload(BaseModel):
    """
    Canonical ese_lite artifact payload.

    Invariants (J.13.4):
      - sha256 is stable; artifact_ref never changes once written.
      - model_ref identifies ESMDance revision used.
      - max_claim_tier is always 'screening_signal' for model-inferred only.
    """
    artifact_kind: str = "ese_lite"
    model_ref: str                                    # serverless_model://modal/esmdance@0.1.0
    model_revision_ref: str                           # serverless_model_revision://esmdance/rev_…
    sequence: str
    global_embedding: Optional[List[float]] = None   # global sequence-level embedding
    residue_features: List[EseLiteResidueFeature] = Field(default_factory=list)
    confidence: Optional[float] = None               # aggregate model confidence
    max_claim_tier: str = "screening_signal"          # J.12.4 — ESMDance alone never exceeds this
    source: str = "esmdance_serverless"
    raw_output_ref: Optional[str] = None             # artifact:// link to verbose raw output if kept


# ── ESE-Graph-Lite (Level C) — deterministic fallback ─────────────────────

class EseGraphLiteNode(BaseModel):
    """Node features for deterministic ESE-Graph-Lite."""
    residue_index: int
    aa: str
    aa_class: str                                    # hydrophobic / polar / charged / special
    ese_lite_flexibility_z: Optional[float] = None   # z-scored flexibility from ese_lite (if available)
    ese_lite_confidence: Optional[float] = None


class EseGraphLiteEdge(BaseModel):
    """Edge features for static contact graph."""
    source_residue: int
    target_residue: int
    edge_kind: str = "static_contact"               # always static_contact at Level C
    distance_bin: str                               # "short" / "medium" / "long"
    sequence_separation_bin: str                    # "local" / "medium_range" / "long_range"
    contact_source: str = "sequence_distance"       # sequence-derived heuristic
    confidence: float = 0.5                         # low baseline (heuristic)


class EseGraphLiteSummary(BaseModel):
    """Deterministic graph-level embedding summary."""
    contact_density: float          # edges / (N*(N-1)/2)
    mean_degree: float
    long_range_contact_fraction: float   # |i-j| >= 24 fraction
    hinge_candidate_count: int          # residues with high betweenness proxy
    flexibility_mean: Optional[float] = None
    flexibility_std: Optional[float] = None


class EseGraphLitePayload(BaseModel):
    """
    Canonical ese_graph_lite artifact payload (Level C — deterministic, no real dynamics).

    Enables: dynamic-neighbor search, family clustering, Atlas indexing, Genesis ranking.
    Does NOT enable: allosteric routes, contact lifetime, transient contacts.
    """
    artifact_kind: str = "ese_graph_lite"
    sequence: str
    nodes: List[EseGraphLiteNode] = Field(default_factory=list)
    edges: List[EseGraphLiteEdge] = Field(default_factory=list)
    summary: Optional[EseGraphLiteSummary] = None
    derived_from_ese_lite: bool = False             # True if ese_lite features were folded in
    max_claim_tier: str = "screening_signal"
    source: str = "deterministic_graph_lite"


# ── Unified result ─────────────────────────────────────────────────────────

class EseArtifact(BaseModel):
    """
    The durable output of normalize_and_register().

    artifact_ref and sha256 are the invariant cross-lane identity (J.13.4 §2).
    receipt_id links to the ReceiptCore for this run.
    """
    artifact_kind: str                              # ese_lite | ese_graph_lite
    artifact_ref: str                               # artifact://workspace/{ws}/ese/{kind}/{id}
    sha256: str                                     # canonical content hash
    model_ref: Optional[str] = None                 # None for deterministic graph_lite
    inference_receipt_id: Optional[str] = None      # ReceiptCore.receipt_id for serverless leg
    quetzal_receipt_id: Optional[str] = None        # ReceiptCore.receipt_id for gate leg
    mudo_commit_ref: Optional[str] = None           # future MUDO commit, None in PoC
    payload: Dict[str, Any] = Field(default_factory=dict)   # serialised EseLitePayload or EseGraphLitePayload


class EseNormalizeResult(BaseModel):
    """Top-level result from ese.normalize_and_register()."""
    status: str                                     # "completed" | "fallback_graph_lite" | "blocked"
    artifact: Optional[EseArtifact] = None
    reason: Optional[str] = None                    # why we fell back or blocked (if applicable)
    gate_verdict: Optional[str] = None              # quetzal gate decision for the artifact check
    inference_blocked: bool = False                 # True when serverless inference was gated/blocked
