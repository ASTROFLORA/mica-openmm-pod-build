"""LMP v4 Annotations Router

Serves parsed LMP v4 XML annotations as structured JSON, with per-section
lazy loading, Redis L1 cache, and GCS / filesystem backend.

Endpoints:
    GET /api/v1/lmp/annotations/manifest
    GET /api/v1/lmp/annotations/{accession}
    GET /api/v1/lmp/annotations/{accession}/section/{section}

Backing storage priority (first hit wins):
    1. Redis L1 cache   (key: lmp:ann:{preset}:{acc}:{iso}:{state}:{section})
    2. GCS bucket       ($MICA_LMP_V4_GCS_BUCKET, default "mica-public-lmp-v4")
    3. Local filesystem ($MICA_LMP_V4_DIR, reuse graph.py convention)

Author: DATABASE_INFRASTRUCTURE_OPERATOR (R-MuDO-03 §4.1)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/lmp/annotations",
    tags=["lmp-annotations"],
    dependencies=[Depends(user_dependency)],
)


# ---------------------------------------------------------------------------
# Config + resolvers
# ---------------------------------------------------------------------------

_DEFAULT_PRESET = "scflr_full_isoform"
_DEFAULT_BUCKET = "mica-public-lmp-v4"
_NS = "http://ai-university.edu/lmp/v4.0"
_VALID_SECTIONS = {
    "identity",
    "semantics",
    "geometry",
    "knowledge_graph",
    "provenance",
    "all",
}


def _lmp_v4_dir() -> Path:
    """Mirrors graph.py convention so both routers share the same filesystem backend."""
    override = os.getenv("MICA_LMP_V4_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).resolve().parents[4] / ".tmp_lmp_v4").resolve()


def _gcs_bucket() -> str:
    return os.getenv("MICA_LMP_V4_GCS_BUCKET", _DEFAULT_BUCKET)


def build_lmp_state_id(
    preset: str,
    accession: str,
    isoform: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    return ":".join(
        [
            "lmp",
            str(preset or _DEFAULT_PRESET).strip() or _DEFAULT_PRESET,
            str(accession or "").strip().upper(),
            str(isoform or "-").strip() or "-",
            str(state or "-").strip() or "-",
        ]
    )


def parse_lmp_state_id(state_id: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    parts = str(state_id or "").strip().split(":", 4)
    if len(parts) != 5 or parts[0] != "lmp":
        raise HTTPException(status_code=400, detail=f"Invalid LMP state_id: {state_id}")
    preset = parts[1] or _DEFAULT_PRESET
    accession = parts[2].strip().upper()
    if not accession:
        raise HTTPException(status_code=400, detail=f"Invalid LMP state_id: {state_id}")
    isoform = parts[3] if parts[3] and parts[3] != "-" else None
    state = parts[4] if parts[4] and parts[4] != "-" else None
    return preset, accession, isoform, state


def _object_key(preset: str, accession: str, isoform: Optional[str], state: Optional[str]) -> str:
    """Canonical object key in GCS + filesystem.

    Example: scflr_full_isoform/P00519_P00519-1_Phosphorylated_Active.xml
    """
    stem_parts = [accession]
    if isoform:
        stem_parts.append(isoform)
    if state:
        stem_parts.append(state)
    stem = "_".join(stem_parts)
    return f"{preset}/{stem}.xml"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ProvenanceBlock(BaseModel):
    generator: Optional[str] = None
    preset_name: str
    preset_hash: Optional[str] = None
    run_id: Optional[str] = None
    source: Dict[str, Any] = Field(default_factory=dict)
    integrity: Dict[str, Any] = Field(default_factory=dict)
    served_from: str = "gcs"


class IdentityBlock(BaseModel):
    budo_id: Optional[str] = None
    primary_accession: str
    isoform_accession: Optional[str] = None
    uniprot_kb_id: Optional[str] = None
    entry_type: Optional[str] = None
    organism: Dict[str, Any] = Field(default_factory=dict)
    lineages: List[str] = Field(default_factory=list)
    secondary_accessions: List[str] = Field(default_factory=list)


class SemanticsBlock(BaseModel):
    protein_name: Optional[str] = None
    genes: List[Dict[str, Any]] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    nesy_grammar: Optional[str] = None
    comments: List[Dict[str, Any]] = Field(default_factory=list)


class GeometryBlock(BaseModel):
    sequence: Optional[str] = None
    length: Optional[int] = None
    domains: List[Dict[str, Any]] = Field(default_factory=list)
    motifs: List[Dict[str, Any]] = Field(default_factory=list)
    ptms: List[Dict[str, Any]] = Field(default_factory=list)
    binding_sites: List[Dict[str, Any]] = Field(default_factory=list)
    alphafold: Dict[str, Any] = Field(default_factory=dict)  # avg_plddt + entry_id
    per_residue_plddt: Optional[List[Dict[str, Any]]] = None  # opt-in via ?include=
    visuals: List["VisualInfo"] = Field(default_factory=list)
    pocket_sites: List["PocketSiteInfo"] = Field(default_factory=list)
    structure_catalog: List["StructureCatalogEntryInfo"] = Field(default_factory=list)
    structure_set: Optional["StructureSetInfo"] = None
    residue_statistics: List["ResidueStatisticInfo"] = Field(default_factory=list)
    dynamics_statistics: Optional["DynamicsStatisticsInfo"] = None


class VisualInfo(BaseModel):
    kind: str
    source: str
    entry_id: Optional[str] = None
    pdb_id: Optional[str] = None
    url: Optional[str] = None
    preview_url: Optional[str] = None
    local_path: Optional[str] = None
    avg_plddt: Optional[float] = None


class PocketResidueInfo(BaseModel):
    residue_id: int
    chain: Optional[str] = None
    residue_name: Optional[str] = None


class PocketArtifactRefInfo(BaseModel):
    kind: Optional[str] = None
    path: Optional[str] = None
    format: Optional[str] = None


class PocketSiteInfo(BaseModel):
    id: str
    rank: Optional[int] = None
    engine: Optional[str] = None
    source: Optional[str] = None
    score: Optional[float] = None
    volume: Optional[float] = None
    center_x: Optional[float] = None
    center_y: Optional[float] = None
    center_z: Optional[float] = None
    point_count: Optional[int] = None
    residue_count: Optional[int] = None
    static: Optional[bool] = None
    residues: List[PocketResidueInfo] = Field(default_factory=list)
    artifact_refs: List[PocketArtifactRefInfo] = Field(default_factory=list)


class DynamicRunMetadataInfo(BaseModel):
    run_id: Optional[str] = None
    engine: Optional[str] = None
    topology_ref: Optional[str] = None
    trajectory_ref: Optional[str] = None
    replica_id: Optional[str] = None
    replica_count: Optional[int] = None
    ensemble_id: Optional[str] = None
    force_field: Optional[str] = None
    solvent_model: Optional[str] = None
    n_frames: Optional[int] = None
    stride: Optional[int] = None
    time_step_ps: Optional[float] = None
    duration_ns: Optional[float] = None
    temperature_k: Optional[float] = None


class DynamicDatasetReferenceInfo(BaseModel):
    dataset: str
    record_id: Optional[str] = None
    split: Optional[str] = None
    source_uri: Optional[str] = None


class ResidueDynamicStatInfo(BaseModel):
    position: int
    chain: Optional[str] = None
    rmsf: Optional[float] = None
    sasa_mean: Optional[float] = None
    sasa_std: Optional[float] = None
    secondary_structure: Optional[str] = None
    normal_mode_low: Optional[float] = None
    normal_mode_mid: Optional[float] = None
    normal_mode_high: Optional[float] = None


class PairDynamicStatInfo(BaseModel):
    position_i: int
    position_j: int
    chain_i: Optional[str] = None
    chain_j: Optional[str] = None
    vdw: Optional[float] = None
    hbbb: Optional[float] = None
    hbsb: Optional[float] = None
    hbss: Optional[float] = None
    hydrophobic: Optional[float] = None
    salt_bridge: Optional[float] = None
    pi_cation: Optional[float] = None
    pi_stacking: Optional[float] = None
    t_stacking: Optional[float] = None
    motion_correlation: Optional[float] = None
    normal_mode_low: Optional[float] = None
    normal_mode_mid: Optional[float] = None
    normal_mode_high: Optional[float] = None


class DynamicsStatisticsInfo(BaseModel):
    source_kind: Optional[str] = None
    run_metadata: List[DynamicRunMetadataInfo] = Field(default_factory=list)
    dataset_refs: List[DynamicDatasetReferenceInfo] = Field(default_factory=list)
    residue_stats: List[ResidueDynamicStatInfo] = Field(default_factory=list)
    pair_stats: List[PairDynamicStatInfo] = Field(default_factory=list)


class CoverageSegmentInfo(BaseModel):
    start: int
    end: int
    chain_id: Optional[str] = None
    auth_chain_id: Optional[str] = None
    label_chain_id: Optional[str] = None
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    structure_start: Optional[int] = None
    structure_end: Optional[int] = None
    auth_start: Optional[int] = None
    auth_end: Optional[int] = None
    identity: Optional[float] = None
    coverage: Optional[float] = None


class StructureCatalogEntryInfo(BaseModel):
    structure_ref: str
    source_kind: Optional[str] = None
    provider: Optional[str] = None
    provider_native_id: Optional[str] = None
    coordinate_accession_ref: Optional[str] = None
    artifact_uri: Optional[str] = None
    format: Optional[str] = None
    display_name: Optional[str] = None
    representative: Optional[bool] = None
    coverage_segments: List[CoverageSegmentInfo] = Field(default_factory=list)


class StructureMemberInfo(BaseModel):
    structure_ref: str
    role: Optional[str] = None


class StructureSetInfo(BaseModel):
    representative_structure_ref: Optional[str] = None
    coordinate_accession_ref: Optional[str] = None
    members: List[StructureMemberInfo] = Field(default_factory=list)


class ResidueStatisticInfo(BaseModel):
    position: int
    amino_acid: Optional[str] = None
    structure_ref: Optional[str] = None
    chain: Optional[str] = None
    secondary_structure: Optional[str] = None
    confidence: Optional[float] = None
    confidence_source: Optional[str] = None
    confidence_class: Optional[str] = None
    mean_pae: Optional[float] = None
    hub_score: Optional[float] = None
    contact_degree: Optional[float] = None


class StructuralReceiptBlock(BaseModel):
    source_kind: str = "lmp_geometry"
    structure_origin: str = "cached_annotation"
    alphafold: Dict[str, Any] = Field(default_factory=dict)
    visuals: List[VisualInfo] = Field(default_factory=list)
    pocket_sites: List[PocketSiteInfo] = Field(default_factory=list)
    structure_catalog: List[StructureCatalogEntryInfo] = Field(default_factory=list)
    structure_set: Optional[StructureSetInfo] = None
    residue_statistics: List[ResidueStatisticInfo] = Field(default_factory=list)
    structure_path: Optional[str] = None


class LMPStateReceiptResponse(BaseModel):
    state_id: str
    meta: Dict[str, Any]
    structural_receipt: StructuralReceiptBlock
    dynamics_statistics: Optional[DynamicsStatisticsInfo] = None


class LMPStateDynamicsResponse(BaseModel):
    state_id: str
    meta: Dict[str, Any]
    dynamics_statistics: Optional[DynamicsStatisticsInfo] = None


class ResidueDynamicQueryRequest(BaseModel):
    positions: List[int] = Field(default_factory=list)
    chain: Optional[str] = None
    max_results: int = Field(default=50, ge=1, le=200)


class PairDynamicQueryItem(BaseModel):
    position_i: int
    position_j: int
    chain_i: Optional[str] = None
    chain_j: Optional[str] = None


class PairDynamicQueryRequest(BaseModel):
    pairs: List[PairDynamicQueryItem] = Field(default_factory=list)
    chain_i: Optional[str] = None
    chain_j: Optional[str] = None
    max_results: int = Field(default=50, ge=1, le=200)


class ResidueDynamicQueryInfo(BaseModel):
    requested_positions: List[int] = Field(default_factory=list)
    chain: Optional[str] = None
    total_available: int = 0
    matched_count: int = 0
    returned_count: int = 0
    truncated: bool = False


class PairDynamicQueryInfo(BaseModel):
    requested_pairs: List[PairDynamicQueryItem] = Field(default_factory=list)
    chain_i: Optional[str] = None
    chain_j: Optional[str] = None
    total_available: int = 0
    matched_count: int = 0
    returned_count: int = 0
    truncated: bool = False


class LMPStateResidueDynamicsQueryResponse(BaseModel):
    state_id: str
    meta: Dict[str, Any]
    query: ResidueDynamicQueryInfo
    residue_stats: List[ResidueDynamicStatInfo] = Field(default_factory=list)


class LMPStatePairDynamicsQueryResponse(BaseModel):
    state_id: str
    meta: Dict[str, Any]
    query: PairDynamicQueryInfo
    pair_stats: List[PairDynamicStatInfo] = Field(default_factory=list)


class StructureCoverageSummaryInfo(BaseModel):
    structure: StructureCatalogEntryInfo
    coverage_segment_count: int = 0
    covered_residue_count: int = 0
    coverage_start: Optional[int] = None
    coverage_end: Optional[int] = None
    chain_ids: List[str] = Field(default_factory=list)


class AFDBPDBComparisonEntryInfo(BaseModel):
    predicted_structure_ref: str
    experimental_structure_ref: str
    coordinate_accession_ref: Optional[str] = None
    overlap_start: Optional[int] = None
    overlap_end: Optional[int] = None
    overlap_residue_count: int = 0
    predicted_covered_residue_count: int = 0
    experimental_covered_residue_count: int = 0
    overlap_fraction_predicted: Optional[float] = None
    overlap_fraction_experimental: Optional[float] = None
    representative_pair: bool = False
    status: str
    shared_chain_ids: List[str] = Field(default_factory=list)
    degraded: List[str] = Field(default_factory=list)


class AFDBPDBComparisonLedgerInfo(BaseModel):
    ledger_id: str
    structure_catalog_sha256: str
    predicted_structures: List[StructureCoverageSummaryInfo] = Field(default_factory=list)
    experimental_structures: List[StructureCoverageSummaryInfo] = Field(default_factory=list)
    comparisons: List[AFDBPDBComparisonEntryInfo] = Field(default_factory=list)
    degraded: List[str] = Field(default_factory=list)
    closure_state: str = "partial"


class LMPStateStructureComparisonLedgerResponse(BaseModel):
    state_id: str
    meta: Dict[str, Any]
    comparison_ledger: AFDBPDBComparisonLedgerInfo


class KnowledgeGraphBlock(BaseModel):
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    links: List[Dict[str, Any]] = Field(default_factory=list)
    counts: Dict[str, int] = Field(default_factory=dict)


class LMPAnnotationResponse(BaseModel):
    meta: Dict[str, Any]
    identity: Optional[IdentityBlock] = None
    semantics: Optional[SemanticsBlock] = None
    geometry: Optional[GeometryBlock] = None
    knowledge_graph: Optional[KnowledgeGraphBlock] = None
    provenance: Optional[ProvenanceBlock] = None


class ManifestEntry(BaseModel):
    accession: str
    isoform: Optional[str] = None
    state: Optional[str] = None
    preset: str
    state_id: Optional[str] = None
    gcs_uri: Optional[str] = None
    size_bytes: Optional[int] = None
    last_modified: Optional[str] = None
    plddt_avg: Optional[float] = None
    uniprot_version: Optional[str] = None
    preset_hash: Optional[str] = None
    run_id: Optional[str] = None


class ManifestResponse(BaseModel):
    preset: str
    total: int
    entries: List[ManifestEntry]


for _model in (
    GeometryBlock,
    StructuralReceiptBlock,
    LMPStateReceiptResponse,
    LMPStateDynamicsResponse,
    LMPStateResidueDynamicsQueryResponse,
    LMPStatePairDynamicsQueryResponse,
    LMPStateStructureComparisonLedgerResponse,
):
    if hasattr(_model, "model_rebuild"):
        _model.model_rebuild()
    else:
        _model.update_forward_refs()


# ---------------------------------------------------------------------------
# XML parsing (scoped to lmp v4; tolerant)
# ---------------------------------------------------------------------------


def _tag(local: str) -> str:
    return f"{{{_NS}}}{local}"


def _text(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    return (el.text or "").strip() or None


def _attr(el: Optional[ET.Element], key: str) -> Optional[str]:
    if el is None:
        return None
    v = el.attrib.get(key)
    return v.strip() if isinstance(v, str) else None


def _int_attr(el: Optional[ET.Element], key: str) -> Optional[int]:
    value = _attr(el, key)
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_attr(el: Optional[ET.Element], key: str) -> Optional[float]:
    value = _attr(el, key)
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool_attr(el: Optional[ET.Element], key: str) -> Optional[bool]:
    value = _attr(el, key)
    if value in {None, ""}:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _parse_identity(root: ET.Element) -> IdentityBlock:
    ident = root.find(_tag("Identity"))
    if ident is None:
        return IdentityBlock(primary_accession="")
    organism_el = ident.find(_tag("Organism"))
    organism: Dict[str, Any] = {}
    if organism_el is not None:
        organism = {
            "name": _text(organism_el.find(_tag("Name"))),
            "scientific_name": _text(organism_el.find(_tag("ScientificName"))),
            "tax_id": _attr(organism_el, "tax_id") or _text(organism_el.find(_tag("TaxId"))),
        }
    lineages = [
        (_text(t) or "")
        for t in ident.findall(f"{_tag('Lineages')}/{_tag('Taxon')}")
        if _text(t)
    ]
    secondary = [
        (_text(a) or "")
        for a in ident.findall(f"{_tag('SecondaryAccessions')}/{_tag('Accession')}")
        if _text(a)
    ]
    return IdentityBlock(
        budo_id=_text(ident.find(_tag("BudoID"))),
        primary_accession=_text(ident.find(_tag("PrimaryAccession"))) or "",
        isoform_accession=_text(ident.find(_tag("IsoformAccession"))),
        uniprot_kb_id=_text(ident.find(_tag("UniProtKBId"))),
        entry_type=_text(ident.find(_tag("EntryType"))),
        organism=organism,
        lineages=lineages,
        secondary_accessions=secondary,
    )


def _parse_semantics(root: ET.Element) -> SemanticsBlock:
    sem = root.find(_tag("Semantics"))
    if sem is None:
        return SemanticsBlock()
    genes = []
    for g in sem.findall(f"{_tag('Genes')}/{_tag('Gene')}"):
        genes.append(
            {
                "name": _attr(g, "name") or _text(g.find(_tag("Name"))),
                "synonyms": _attr(g, "synonyms"),
            }
        )
    keywords = [
        (_text(k) or "")
        for k in sem.findall(f"{_tag('Keywords')}/{_tag('Keyword')}")
        if _text(k)
    ]
    comments: List[Dict[str, Any]] = []
    for c in sem.findall(_tag("Comment")):
        comments.append({"type": _attr(c, "type"), "text": _text(c)})
    return SemanticsBlock(
        protein_name=_text(sem.find(_tag("ProteinName"))),
        genes=genes,
        keywords=keywords,
        nesy_grammar=_text(sem.find(_tag("NeSyGrammar"))),
        comments=comments,
    )


def _parse_geometry(root: ET.Element, *, per_residue: bool = False) -> GeometryBlock:
    geo = root.find(_tag("Geometry"))
    if geo is None:
        return GeometryBlock()
    seq_el = geo.find(_tag("Sequence"))
    seq = _text(seq_el)
    length = int(_attr(seq_el, "length") or 0) if seq_el is not None else (len(seq) if seq else None)

    domains: List[Dict[str, Any]] = []
    motifs: List[Dict[str, Any]] = []
    ptms: List[Dict[str, Any]] = []
    binding_sites: List[Dict[str, Any]] = []

    for chain in geo.findall(_tag("Chain")):
        for d in chain.findall(f"{_tag('Domains')}/{_tag('Domain')}"):
            domains.append(
                {
                    "id": _attr(d, "id"),
                    "name": _attr(d, "name"),
                    "start": int(_attr(d, "start") or 0) or None,
                    "end": int(_attr(d, "end") or 0) or None,
                    "db": _attr(d, "db"),
                }
            )
        for m in chain.findall(f"{_tag('Motifs')}/{_tag('Motif')}"):
            motifs.append(
                {
                    "id": _attr(m, "id"),
                    "name": _attr(m, "name"),
                    "start": int(_attr(m, "start") or 0) or None,
                    "end": int(_attr(m, "end") or 0) or None,
                }
            )
        for p in chain.findall(f"{_tag('PTMs')}/{_tag('PTM')}"):
            ptms.append(
                {
                    "type": _attr(p, "type"),
                    "residue": _attr(p, "residue"),
                    "position": int(_attr(p, "position") or 0) or None,
                }
            )
        for bs in chain.findall(f"{_tag('BindingSites')}/{_tag('BindingSite')}"):
            ligands = [
                {
                    "id": _attr(l, "id"),
                    "name": _attr(l, "name"),
                    "chebi": _attr(l, "ChEBI"),
                }
                for l in bs.findall(_tag("Ligand"))
            ]
            binding_sites.append(
                {
                    "id": _attr(bs, "id"),
                    "type": _attr(bs, "type"),
                    "ligands": ligands,
                }
            )

    af_el = geo.find(_tag("AlphaFoldModel"))
    af: Dict[str, Any] = {}
    per_res: Optional[List[Dict[str, Any]]] = None
    if af_el is not None:
        af = {
            "entry_id": _attr(af_el, "entry_id"),
            "avg_plddt": float(_attr(af_el, "avg_plddt") or 0) or None,
            "model_date": _attr(af_el, "model_date"),
        }
        if per_residue:
            per_res = [
                {
                    "position": int(_attr(c, "position") or 0),
                    "plddt": float(_attr(c, "plddt") or 0),
                    "confidence_class": _attr(c, "confidence_class"),
                }
                for c in af_el.findall(
                    f"{_tag('ConfidencePerResidue')}/{_tag('Confidence')}"
                )
            ]

    visuals: List[VisualInfo] = []
    visuals_el = geo.find(_tag("Visuals"))
    if visuals_el is not None:
        for visual_el in visuals_el.findall(_tag("Visual")):
            kind = _attr(visual_el, "kind")
            source = _attr(visual_el, "source")
            if not kind or not source:
                continue
            visuals.append(
                VisualInfo(
                    kind=kind,
                    source=source,
                    entry_id=_attr(visual_el, "entry_id"),
                    pdb_id=_attr(visual_el, "pdb_id"),
                    url=_attr(visual_el, "url"),
                    preview_url=_attr(visual_el, "preview_url"),
                    local_path=_attr(visual_el, "local_path"),
                    avg_plddt=_float_attr(visual_el, "avg_plddt"),
                )
            )

    pocket_sites: List[PocketSiteInfo] = []
    pockets_el = geo.find(_tag("PocketSites"))
    if pockets_el is not None:
        for pocket_el in pockets_el.findall(_tag("PocketSite")):
            pocket_id = _attr(pocket_el, "id")
            if not pocket_id:
                continue
            residues = []
            for residue_el in pocket_el.findall(_tag("Residue")):
                residue_id = _int_attr(residue_el, "residue_id")
                if residue_id is None:
                    continue
                residues.append(
                    PocketResidueInfo(
                        residue_id=residue_id,
                        chain=_attr(residue_el, "chain"),
                        residue_name=_attr(residue_el, "residue_name"),
                    )
                )
            artifact_refs = []
            for artifact_el in pocket_el.findall(_tag("ArtifactRef")):
                artifact_refs.append(
                    PocketArtifactRefInfo(
                        kind=_attr(artifact_el, "kind"),
                        path=_attr(artifact_el, "path"),
                        format=_attr(artifact_el, "format"),
                    )
                )
            static_attr = _attr(pocket_el, "static")
            pocket_sites.append(
                PocketSiteInfo(
                    id=pocket_id,
                    rank=_int_attr(pocket_el, "rank"),
                    engine=_attr(pocket_el, "engine"),
                    source=_attr(pocket_el, "source"),
                    score=_float_attr(pocket_el, "score"),
                    volume=_float_attr(pocket_el, "volume"),
                    center_x=_float_attr(pocket_el, "center_x"),
                    center_y=_float_attr(pocket_el, "center_y"),
                    center_z=_float_attr(pocket_el, "center_z"),
                    point_count=_int_attr(pocket_el, "point_count"),
                    residue_count=_int_attr(pocket_el, "residue_count"),
                    static=(static_attr.lower() == "true") if static_attr is not None else None,
                    residues=residues,
                    artifact_refs=artifact_refs,
                )
            )

    structure_catalog: List[StructureCatalogEntryInfo] = []
    catalog_el = geo.find(_tag("StructureCatalog"))
    if catalog_el is not None:
        for structure_el in catalog_el.findall(_tag("Structure")):
            structure_ref = _attr(structure_el, "structure_ref")
            if not structure_ref:
                continue
            coverage_segments = []
            for segment_el in structure_el.findall(_tag("CoverageSegment")):
                start = _int_attr(segment_el, "start")
                end = _int_attr(segment_el, "end")
                if start is None or end is None:
                    continue
                coverage_segments.append(
                    CoverageSegmentInfo(
                        start=start,
                        end=end,
                        chain_id=_attr(segment_el, "chain_id"),
                        auth_chain_id=_attr(segment_el, "auth_chain_id"),
                        label_chain_id=_attr(segment_el, "label_chain_id"),
                        entity_id=_attr(segment_el, "entity_id"),
                        entity_type=_attr(segment_el, "entity_type"),
                        structure_start=_int_attr(segment_el, "structure_start"),
                        structure_end=_int_attr(segment_el, "structure_end"),
                        auth_start=_int_attr(segment_el, "auth_start"),
                        auth_end=_int_attr(segment_el, "auth_end"),
                        identity=_float_attr(segment_el, "identity"),
                        coverage=_float_attr(segment_el, "coverage"),
                    )
                )
            structure_catalog.append(
                StructureCatalogEntryInfo(
                    structure_ref=structure_ref,
                    source_kind=_attr(structure_el, "source_kind"),
                    provider=_attr(structure_el, "provider"),
                    provider_native_id=_attr(structure_el, "provider_native_id"),
                    coordinate_accession_ref=_attr(structure_el, "coordinate_accession_ref"),
                    artifact_uri=_attr(structure_el, "artifact_uri"),
                    format=_attr(structure_el, "format"),
                    display_name=_attr(structure_el, "display_name"),
                    representative=_bool_attr(structure_el, "representative"),
                    coverage_segments=coverage_segments,
                )
            )

    structure_set: Optional[StructureSetInfo] = None
    structure_set_el = geo.find(_tag("StructureSet"))
    if structure_set_el is not None:
        members = []
        for member_el in structure_set_el.findall(_tag("StructureMember")):
            structure_ref = _attr(member_el, "structure_ref")
            if not structure_ref:
                continue
            members.append(
                StructureMemberInfo(
                    structure_ref=structure_ref,
                    role=_attr(member_el, "role"),
                )
            )
        structure_set = StructureSetInfo(
            representative_structure_ref=_attr(structure_set_el, "representative_structure_ref"),
            coordinate_accession_ref=_attr(structure_set_el, "coordinate_accession_ref"),
            members=members,
        )

    residue_statistics: List[ResidueStatisticInfo] = []
    residue_stats_el = geo.find(_tag("ResidueStatistics"))
    if residue_stats_el is not None:
        for residue_el in residue_stats_el.findall(_tag("ResidueStat")):
            position = _int_attr(residue_el, "position")
            if position is None:
                continue
            residue_statistics.append(
                ResidueStatisticInfo(
                    position=position,
                    amino_acid=_attr(residue_el, "amino_acid"),
                    structure_ref=_attr(residue_el, "structure_ref"),
                    chain=_attr(residue_el, "chain"),
                    secondary_structure=_attr(residue_el, "secondary_structure"),
                    confidence=_float_attr(residue_el, "confidence"),
                    confidence_source=_attr(residue_el, "confidence_source"),
                    confidence_class=_attr(residue_el, "confidence_class"),
                    mean_pae=_float_attr(residue_el, "mean_pae"),
                    hub_score=_float_attr(residue_el, "hub_score"),
                    contact_degree=_float_attr(residue_el, "contact_degree"),
                )
            )

    dynamics_statistics: Optional[DynamicsStatisticsInfo] = None
    dyn_el = geo.find(_tag("DynamicsStatistics"))
    if dyn_el is not None:
        run_metadata = []
        for run_el in dyn_el.findall(_tag("RunMetadata")):
            run_metadata.append(
                DynamicRunMetadataInfo(
                    run_id=_attr(run_el, "run_id"),
                    engine=_attr(run_el, "engine"),
                    topology_ref=_attr(run_el, "topology_ref"),
                    trajectory_ref=_attr(run_el, "trajectory_ref"),
                    replica_id=_attr(run_el, "replica_id"),
                    replica_count=_int_attr(run_el, "replica_count"),
                    ensemble_id=_attr(run_el, "ensemble_id"),
                    force_field=_attr(run_el, "force_field"),
                    solvent_model=_attr(run_el, "solvent_model"),
                    n_frames=_int_attr(run_el, "n_frames"),
                    stride=_int_attr(run_el, "stride"),
                    time_step_ps=_float_attr(run_el, "time_step_ps"),
                    duration_ns=_float_attr(run_el, "duration_ns"),
                    temperature_k=_float_attr(run_el, "temperature_k"),
                )
            )

        dataset_refs = []
        for dataset_el in dyn_el.findall(_tag("DatasetReference")):
            dataset = _attr(dataset_el, "dataset")
            if not dataset:
                continue
            dataset_refs.append(
                DynamicDatasetReferenceInfo(
                    dataset=dataset,
                    record_id=_attr(dataset_el, "record_id"),
                    split=_attr(dataset_el, "split"),
                    source_uri=_attr(dataset_el, "source_uri"),
                )
            )

        residue_stats = []
        for residue_el in dyn_el.findall(_tag("ResidueDynamicStat")):
            position = _int_attr(residue_el, "position")
            if position is None:
                continue
            residue_stats.append(
                ResidueDynamicStatInfo(
                    position=position,
                    chain=_attr(residue_el, "chain"),
                    rmsf=_float_attr(residue_el, "rmsf"),
                    sasa_mean=_float_attr(residue_el, "sasa_mean"),
                    sasa_std=_float_attr(residue_el, "sasa_std"),
                    secondary_structure=_attr(residue_el, "secondary_structure"),
                    normal_mode_low=_float_attr(residue_el, "normal_mode_low"),
                    normal_mode_mid=_float_attr(residue_el, "normal_mode_mid"),
                    normal_mode_high=_float_attr(residue_el, "normal_mode_high"),
                )
            )

        pair_stats = []
        for pair_el in dyn_el.findall(_tag("PairDynamicStat")):
            position_i = _int_attr(pair_el, "position_i")
            position_j = _int_attr(pair_el, "position_j")
            if position_i is None or position_j is None:
                continue
            pair_stats.append(
                PairDynamicStatInfo(
                    position_i=position_i,
                    position_j=position_j,
                    chain_i=_attr(pair_el, "chain_i"),
                    chain_j=_attr(pair_el, "chain_j"),
                    vdw=_float_attr(pair_el, "vdw"),
                    hbbb=_float_attr(pair_el, "hbbb"),
                    hbsb=_float_attr(pair_el, "hbsb"),
                    hbss=_float_attr(pair_el, "hbss"),
                    hydrophobic=_float_attr(pair_el, "hydrophobic"),
                    salt_bridge=_float_attr(pair_el, "salt_bridge"),
                    pi_cation=_float_attr(pair_el, "pi_cation"),
                    pi_stacking=_float_attr(pair_el, "pi_stacking"),
                    t_stacking=_float_attr(pair_el, "t_stacking"),
                    motion_correlation=_float_attr(pair_el, "motion_correlation"),
                    normal_mode_low=_float_attr(pair_el, "normal_mode_low"),
                    normal_mode_mid=_float_attr(pair_el, "normal_mode_mid"),
                    normal_mode_high=_float_attr(pair_el, "normal_mode_high"),
                )
            )

        dynamics_statistics = DynamicsStatisticsInfo(
            source_kind=_attr(dyn_el, "source_kind"),
            run_metadata=run_metadata,
            dataset_refs=dataset_refs,
            residue_stats=residue_stats,
            pair_stats=pair_stats,
        )

    return GeometryBlock(
        sequence=seq,
        length=length,
        domains=domains,
        motifs=motifs,
        ptms=ptms,
        binding_sites=binding_sites,
        alphafold=af,
        per_residue_plddt=per_res,
        visuals=visuals,
        pocket_sites=pocket_sites,
        structure_catalog=structure_catalog,
        structure_set=structure_set,
        residue_statistics=residue_statistics,
        dynamics_statistics=dynamics_statistics,
    )


def _parse_knowledge_graph(root: ET.Element) -> KnowledgeGraphBlock:
    """Delegates to the existing graph.py parser shape for consistency."""
    try:
        from mica.api_v1.routers.graph import _parse_lmp_v4_graph  # type: ignore
    except Exception:
        return KnowledgeGraphBlock()
    try:
        parsed = _parse_lmp_v4_graph(root)
        return KnowledgeGraphBlock(
            nodes=parsed.get("nodes", []),
            links=parsed.get("links", []),
            counts=parsed.get("counts", {}),
        )
    except Exception as exc:
        logger.warning("knowledge_graph parse failed: %s", exc)
        return KnowledgeGraphBlock()


def _parse_provenance(root: ET.Element, *, preset: str, served_from: str) -> ProvenanceBlock:
    prov_el = root.find(_tag("Provenance"))
    source: Dict[str, Any] = {}
    generator: Optional[str] = None
    run_id: Optional[str] = None
    preset_hash: Optional[str] = None
    integrity: Dict[str, Any] = {}
    if prov_el is not None:
        generator = _attr(prov_el, "generator") or _text(prov_el.find(_tag("Generator")))
        run_id = _attr(prov_el, "run_id") or _text(prov_el.find(_tag("RunId")))
        preset_hash = _attr(prov_el, "preset_hash")
        for src in prov_el.findall(_tag("Source")):
            key = _attr(src, "key") or _attr(src, "db")
            val = _attr(src, "version") or _text(src)
            if key:
                source[key] = val
        integ = prov_el.find(_tag("Integrity"))
        if integ is not None:
            integrity = {
                "sha256": _attr(integ, "sha256") or _text(integ.find(_tag("Sha256"))),
            }
    return ProvenanceBlock(
        generator=generator,
        preset_name=preset,
        preset_hash=preset_hash,
        run_id=run_id,
        source=source,
        integrity=integrity,
        served_from=served_from,
    )


# ---------------------------------------------------------------------------
# Storage backend (GCS + filesystem fallback)
# ---------------------------------------------------------------------------


def _fs_path(preset: str, accession: str, isoform: Optional[str], state: Optional[str]) -> Path:
    return _lmp_v4_dir() / _object_key(preset, accession, isoform, state)


def _read_from_fs(preset: str, accession: str, isoform: Optional[str], state: Optional[str]) -> Optional[bytes]:
    p = _fs_path(preset, accession, isoform, state)
    if p.exists() and p.is_file():
        return p.read_bytes()
    return None


async def _read_from_gcs(
    preset: str, accession: str, isoform: Optional[str], state: Optional[str]
) -> Optional[bytes]:
    try:
        from google.cloud import storage  # type: ignore
    except Exception:
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(_gcs_bucket())
        blob = bucket.blob(_object_key(preset, accession, isoform, state))
        if not blob.exists():
            return None
        return blob.download_as_bytes()
    except Exception as exc:
        logger.warning("GCS read failed (%s/%s): %s", _gcs_bucket(), accession, exc)
        return None


# ---------------------------------------------------------------------------
# Redis L1 cache (best-effort)
# ---------------------------------------------------------------------------


async def _cache_get(key: str) -> Optional[str]:
    try:
        from mica.infrastructure.redis_client import get_redis  # type: ignore

        r = await get_redis()
        return await r.get(key)
    except Exception:
        return None


async def _cache_set(key: str, value: str, ttl_seconds: int = 3600) -> None:
    try:
        from mica.infrastructure.redis_client import get_redis  # type: ignore

        r = await get_redis()
        await r.set(key, value, ex=ttl_seconds)
    except Exception:
        pass


def _cache_key(
    preset: str, accession: str, isoform: Optional[str], state: Optional[str], section: str
) -> str:
    return f"lmp:ann:{preset}:{accession}:{isoform or '-'}:{state or '-'}:{section}"


def _with_state_id(entry: ManifestEntry) -> ManifestEntry:
    if entry.state_id:
        return entry
    return entry.model_copy(
        update={
            "state_id": build_lmp_state_id(
                preset=entry.preset,
                accession=entry.accession,
                isoform=entry.isoform,
                state=entry.state,
            )
        }
    )


def _structural_receipt_from_geometry(geometry: Optional[GeometryBlock]) -> StructuralReceiptBlock:
    return StructuralReceiptBlock(
        source_kind="lmp_geometry",
        structure_origin="cached_annotation",
        alphafold=dict((geometry.alphafold if geometry is not None else {}) or {}),
        visuals=list((geometry.visuals if geometry is not None else []) or []),
        pocket_sites=list((geometry.pocket_sites if geometry is not None else []) or []),
        structure_catalog=list((geometry.structure_catalog if geometry is not None else []) or []),
        structure_set=(geometry.structure_set if geometry is not None else None),
        residue_statistics=list((geometry.residue_statistics if geometry is not None else []) or []),
    )


def _merge_structural_receipts(
    primary: StructuralReceiptBlock,
    fallback: Optional[StructuralReceiptBlock],
) -> StructuralReceiptBlock:
    if fallback is None:
        return primary

    merged = primary.model_copy(deep=True)
    used_fallback = False
    had_cached_payload = bool(
        primary.alphafold
        or primary.visuals
        or primary.pocket_sites
        or primary.structure_catalog
        or primary.residue_statistics
    )

    if not merged.alphafold and fallback.alphafold:
        merged.alphafold = dict(fallback.alphafold)
        used_fallback = True
    if not merged.visuals and fallback.visuals:
        merged.visuals = list(fallback.visuals)
        used_fallback = True
    if not merged.pocket_sites and fallback.pocket_sites:
        merged.pocket_sites = list(fallback.pocket_sites)
        used_fallback = True
    if fallback.structure_catalog:
        seen_structure_refs: Set[str] = {
            str(entry.structure_ref or "").strip()
            for entry in merged.structure_catalog or []
            if str(entry.structure_ref or "").strip()
        }
        appended_entries = [
            entry
            for entry in fallback.structure_catalog
            if str(entry.structure_ref or "").strip()
            and str(entry.structure_ref or "").strip() not in seen_structure_refs
        ]
        if appended_entries:
            merged.structure_catalog = list(merged.structure_catalog or []) + appended_entries
            used_fallback = True
    if merged.structure_set is None and fallback.structure_set is not None:
        merged.structure_set = fallback.structure_set
        used_fallback = True
    if not merged.residue_statistics and fallback.residue_statistics:
        merged.residue_statistics = list(fallback.residue_statistics)
        used_fallback = True
    if not merged.structure_path and fallback.structure_path:
        merged.structure_path = fallback.structure_path
        used_fallback = True

    if used_fallback:
        if had_cached_payload:
            merged.structure_origin = f"{primary.structure_origin}+{fallback.structure_origin}"
        else:
            merged.source_kind = fallback.source_kind
            merged.structure_origin = fallback.structure_origin

    return merged


def _normalize_chain_id(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    return text.upper() if text else None


def _normalize_dynamic_query_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 50
    return max(1, min(parsed, 200))


def _normalize_requested_positions(positions: List[int]) -> List[int]:
    normalized: Set[int] = set()
    for raw_value in positions or []:
        try:
            position = int(raw_value)
        except (TypeError, ValueError):
            continue
        if position > 0:
            normalized.add(position)
    return sorted(normalized)


def _normalize_requested_pairs(pairs: List[PairDynamicQueryItem]) -> List[PairDynamicQueryItem]:
    normalized: List[PairDynamicQueryItem] = []
    seen: Set[Tuple[int, int, Optional[str], Optional[str]]] = set()
    for pair in pairs or []:
        try:
            position_i = int(pair.position_i)
            position_j = int(pair.position_j)
        except (TypeError, ValueError):
            continue
        if position_i <= 0 or position_j <= 0:
            continue
        chain_i = _normalize_chain_id(pair.chain_i)
        chain_j = _normalize_chain_id(pair.chain_j)
        key = (position_i, position_j, chain_i, chain_j)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            PairDynamicQueryItem(
                position_i=position_i,
                position_j=position_j,
                chain_i=chain_i,
                chain_j=chain_j,
            )
        )
    return normalized


def _pair_stat_matches_requested_pair(
    pair_stat: PairDynamicStatInfo,
    requested_pair: PairDynamicQueryItem,
    *,
    default_chain_i: Optional[str],
    default_chain_j: Optional[str],
) -> bool:
    requested_chain_i = _normalize_chain_id(requested_pair.chain_i) or default_chain_i
    requested_chain_j = _normalize_chain_id(requested_pair.chain_j) or default_chain_j
    stat_chain_i = _normalize_chain_id(pair_stat.chain_i)
    stat_chain_j = _normalize_chain_id(pair_stat.chain_j)

    if pair_stat.position_i == requested_pair.position_i and pair_stat.position_j == requested_pair.position_j:
        return (requested_chain_i is None or stat_chain_i == requested_chain_i) and (
            requested_chain_j is None or stat_chain_j == requested_chain_j
        )

    if pair_stat.position_i == requested_pair.position_j and pair_stat.position_j == requested_pair.position_i:
        return (requested_chain_i is None or stat_chain_j == requested_chain_i) and (
            requested_chain_j is None or stat_chain_i == requested_chain_j
        )

    return False


def _pair_stat_matches_chain_filters(
    pair_stat: PairDynamicStatInfo,
    *,
    chain_i: Optional[str],
    chain_j: Optional[str],
) -> bool:
    stat_chain_i = _normalize_chain_id(pair_stat.chain_i)
    stat_chain_j = _normalize_chain_id(pair_stat.chain_j)
    if chain_i and chain_j:
        return (stat_chain_i == chain_i and stat_chain_j == chain_j) or (
            stat_chain_i == chain_j and stat_chain_j == chain_i
        )
    if chain_i:
        return stat_chain_i == chain_i or stat_chain_j == chain_i
    if chain_j:
        return stat_chain_i == chain_j or stat_chain_j == chain_j
    return True


def _coverage_positions(entry: StructureCatalogEntryInfo) -> Set[int]:
    positions: Set[int] = set()
    for segment in entry.coverage_segments or []:
        try:
            start = int(segment.start)
            end = int(segment.end)
        except (TypeError, ValueError):
            continue
        if start <= 0 or end <= 0 or end < start:
            continue
        positions.update(range(start, end + 1))
    return positions


def _coverage_chain_ids(entry: StructureCatalogEntryInfo) -> List[str]:
    chain_ids: List[str] = []
    seen: Set[str] = set()
    for segment in entry.coverage_segments or []:
        for candidate in (segment.chain_id, segment.auth_chain_id, segment.label_chain_id):
            normalized = _normalize_chain_id(candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                chain_ids.append(normalized)
    return chain_ids


def _summarize_structure_entry(entry: StructureCatalogEntryInfo) -> StructureCoverageSummaryInfo:
    positions = _coverage_positions(entry)
    return StructureCoverageSummaryInfo(
        structure=entry,
        coverage_segment_count=len(entry.coverage_segments or []),
        covered_residue_count=len(positions),
        coverage_start=min(positions) if positions else None,
        coverage_end=max(positions) if positions else None,
        chain_ids=_coverage_chain_ids(entry),
    )


def _is_afdb_structure_entry(entry: StructureCatalogEntryInfo) -> bool:
    provider = str(entry.provider or "").strip().lower()
    structure_ref = str(entry.structure_ref or "").strip().lower()
    return provider == "alphafold" or structure_ref.startswith("alphafold:")


def _is_experimental_pdb_entry(entry: StructureCatalogEntryInfo) -> bool:
    source_kind = str(entry.source_kind or "").strip().lower()
    provider = str(entry.provider or "").strip().lower()
    structure_ref = str(entry.structure_ref or "").strip().lower()
    return source_kind == "experimental_pdb" or provider == "pdbe" or structure_ref.startswith("pdb:")


def _build_afdb_pdb_comparison_entry(
    predicted: StructureCoverageSummaryInfo,
    experimental: StructureCoverageSummaryInfo,
) -> AFDBPDBComparisonEntryInfo:
    predicted_positions = _coverage_positions(predicted.structure)
    experimental_positions = _coverage_positions(experimental.structure)
    overlap_positions = predicted_positions & experimental_positions
    degraded: List[str] = []
    coordinate_accession_ref = (
        predicted.structure.coordinate_accession_ref
        or experimental.structure.coordinate_accession_ref
    )
    predicted_accession = str(predicted.structure.coordinate_accession_ref or "").strip().upper() or None
    experimental_accession = str(experimental.structure.coordinate_accession_ref or "").strip().upper() or None
    status = "coverage_overlap"
    if predicted_accession and experimental_accession and predicted_accession != experimental_accession:
        degraded.append("coordinate_accession_mismatch")
        status = "coordinate_accession_mismatch"
    elif not overlap_positions:
        degraded.append("no_residue_overlap")
        status = "no_residue_overlap"

    overlap_count = len(overlap_positions)
    predicted_count = predicted.covered_residue_count
    experimental_count = experimental.covered_residue_count
    return AFDBPDBComparisonEntryInfo(
        predicted_structure_ref=predicted.structure.structure_ref,
        experimental_structure_ref=experimental.structure.structure_ref,
        coordinate_accession_ref=coordinate_accession_ref,
        overlap_start=min(overlap_positions) if overlap_positions else None,
        overlap_end=max(overlap_positions) if overlap_positions else None,
        overlap_residue_count=overlap_count,
        predicted_covered_residue_count=predicted_count,
        experimental_covered_residue_count=experimental_count,
        overlap_fraction_predicted=round(overlap_count / predicted_count, 4) if predicted_count else None,
        overlap_fraction_experimental=round(overlap_count / experimental_count, 4) if experimental_count else None,
        representative_pair=bool(predicted.structure.representative and experimental.structure.representative),
        status=status,
        shared_chain_ids=sorted(set(predicted.chain_ids) & set(experimental.chain_ids)),
        degraded=degraded,
    )


def _build_afdb_pdb_comparison_ledger(
    structural_receipt: StructuralReceiptBlock,
    *,
    state_id: str,
) -> AFDBPDBComparisonLedgerInfo:
    catalog_payload = [
        (entry.model_dump() if hasattr(entry, "model_dump") else entry.dict())
        for entry in (structural_receipt.structure_catalog or [])
    ]
    structure_catalog_sha256 = hashlib.sha256(
        json.dumps(catalog_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    predicted_structures = [
        _summarize_structure_entry(entry)
        for entry in structural_receipt.structure_catalog or []
        if _is_afdb_structure_entry(entry)
    ]
    experimental_structures = [
        _summarize_structure_entry(entry)
        for entry in structural_receipt.structure_catalog or []
        if _is_experimental_pdb_entry(entry)
    ]

    degraded: List[str] = []
    if not predicted_structures:
        degraded.append("no_afdb_structure_available")
    if not experimental_structures:
        degraded.append("no_experimental_pdb_structure_available")

    comparisons = [
        _build_afdb_pdb_comparison_entry(predicted, experimental)
        for predicted in predicted_structures
        for experimental in experimental_structures
    ]
    if predicted_structures and experimental_structures and not any(
        comparison.status == "coverage_overlap" for comparison in comparisons
    ):
        degraded.append("no_afdb_pdb_overlap_materialized")

    ledger_payload = {
        "state_id": state_id,
        "structure_catalog_sha256": structure_catalog_sha256,
        "predicted_structures": [
            summary.model_dump() if hasattr(summary, "model_dump") else summary.dict()
            for summary in predicted_structures
        ],
        "experimental_structures": [
            summary.model_dump() if hasattr(summary, "model_dump") else summary.dict()
            for summary in experimental_structures
        ],
        "comparisons": [
            comparison.model_dump() if hasattr(comparison, "model_dump") else comparison.dict()
            for comparison in comparisons
        ],
    }
    ledger_digest = hashlib.sha256(
        json.dumps(ledger_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AFDBPDBComparisonLedgerInfo(
        ledger_id=f"lmp:structure-comparison:{ledger_digest[:16]}",
        structure_catalog_sha256=structure_catalog_sha256,
        predicted_structures=predicted_structures,
        experimental_structures=experimental_structures,
        comparisons=comparisons,
        degraded=degraded,
        closure_state="complete" if any(comp.status == "coverage_overlap" for comp in comparisons) else "partial",
    )


async def _resolve_afdb_structural_fallback(accession: str) -> Optional[StructuralReceiptBlock]:
    try:
        from bsm.lmp.afdb_state_adapter import compute_afdb_first_structural_receipt
    except Exception as exc:
        logger.debug("AFDB-first adapter unavailable: %s", exc)
        return None

    payload = await asyncio.to_thread(
        compute_afdb_first_structural_receipt,
        accession,
    )
    if not payload:
        return None

    pocket_sites = []
    for pocket_payload in payload.get("pocket_sites") or []:
        try:
            pocket_sites.append(PocketSiteInfo(**pocket_payload))
        except Exception as exc:
            logger.debug("Skipping malformed AFDB pocket payload for %s: %s", accession, exc)

    structure_catalog = []
    for structure_payload in payload.get("structure_catalog") or []:
        try:
            structure_catalog.append(StructureCatalogEntryInfo(**structure_payload))
        except Exception as exc:
            logger.debug("Skipping malformed AFDB structure catalog payload for %s: %s", accession, exc)

    structure_set = None
    if payload.get("structure_set"):
        try:
            structure_set = StructureSetInfo(**payload.get("structure_set"))
        except Exception as exc:
            logger.debug("Skipping malformed AFDB structure set payload for %s: %s", accession, exc)

    residue_statistics = []
    for residue_payload in payload.get("residue_statistics") or []:
        try:
            residue_statistics.append(ResidueStatisticInfo(**residue_payload))
        except Exception as exc:
            logger.debug("Skipping malformed AFDB residue-stat payload for %s: %s", accession, exc)

    return StructuralReceiptBlock(
        source_kind=str(payload.get("source_kind") or "alphafold_db"),
        structure_origin=str(payload.get("structure_origin") or "afdb_live"),
        alphafold=dict(payload.get("alphafold") or {}),
        pocket_sites=pocket_sites,
        structure_catalog=structure_catalog,
        structure_set=structure_set,
        residue_statistics=residue_statistics,
        structure_path=str(payload.get("structure_path") or "") or None,
    )


async def resolve_state_receipt_from_state_id(
    state_id: str,
    *,
    allow_afdb_fallback: bool = True,
) -> LMPStateReceiptResponse:
    preset, accession, isoform, state = parse_lmp_state_id(state_id)
    result = await _load_and_parse(accession, preset, isoform, state, ["geometry", "provenance"], False)
    geometry = result.geometry or GeometryBlock()
    structural_receipt = _structural_receipt_from_geometry(geometry)

    needs_afdb_fallback = not (
        structural_receipt.pocket_sites
        and structural_receipt.structure_catalog
        and structural_receipt.residue_statistics
    )

    if allow_afdb_fallback and needs_afdb_fallback:
        afdb_receipt = await _resolve_afdb_structural_fallback(accession)
        if afdb_receipt is not None:
            afdb_receipt.visuals = list(geometry.visuals or [])
            if not afdb_receipt.alphafold and geometry.alphafold:
                afdb_receipt.alphafold = dict(geometry.alphafold)
            structural_receipt = _merge_structural_receipts(structural_receipt, afdb_receipt)

    meta = dict(result.meta)
    meta["state_id"] = state_id
    return LMPStateReceiptResponse(
        state_id=state_id,
        meta=meta,
        structural_receipt=structural_receipt,
        dynamics_statistics=geometry.dynamics_statistics,
    )


async def resolve_state_dynamics_from_state_id(state_id: str) -> LMPStateDynamicsResponse:
    preset, accession, isoform, state = parse_lmp_state_id(state_id)
    result = await _load_and_parse(accession, preset, isoform, state, ["geometry", "provenance"], False)
    geometry = result.geometry or GeometryBlock()
    meta = dict(result.meta)
    meta["state_id"] = state_id
    return LMPStateDynamicsResponse(
        state_id=state_id,
        meta=meta,
        dynamics_statistics=geometry.dynamics_statistics,
    )


async def resolve_state_residue_dynamics_query_from_state_id(
    state_id: str,
    query: ResidueDynamicQueryRequest,
) -> LMPStateResidueDynamicsQueryResponse:
    requested_positions = _normalize_requested_positions(query.positions)
    normalized_chain = _normalize_chain_id(query.chain)
    if not requested_positions and normalized_chain is None:
        raise HTTPException(
            status_code=400,
            detail="Residue dynamics query must include positions or chain.",
        )

    response = await resolve_state_dynamics_from_state_id(state_id)
    dynamics = response.dynamics_statistics or DynamicsStatisticsInfo()
    matched = [
        stat
        for stat in dynamics.residue_stats or []
        if (not requested_positions or stat.position in requested_positions)
        and (normalized_chain is None or _normalize_chain_id(stat.chain) == normalized_chain)
    ]
    matched.sort(key=lambda stat: (stat.position, _normalize_chain_id(stat.chain) or ""))
    limit = _normalize_dynamic_query_limit(query.max_results)
    returned = matched[:limit]

    return LMPStateResidueDynamicsQueryResponse(
        state_id=state_id,
        meta=dict(response.meta),
        query=ResidueDynamicQueryInfo(
            requested_positions=requested_positions,
            chain=normalized_chain,
            total_available=len(dynamics.residue_stats or []),
            matched_count=len(matched),
            returned_count=len(returned),
            truncated=len(matched) > limit,
        ),
        residue_stats=returned,
    )


async def resolve_state_pair_dynamics_query_from_state_id(
    state_id: str,
    query: PairDynamicQueryRequest,
) -> LMPStatePairDynamicsQueryResponse:
    requested_pairs = _normalize_requested_pairs(query.pairs)
    normalized_chain_i = _normalize_chain_id(query.chain_i)
    normalized_chain_j = _normalize_chain_id(query.chain_j)
    if not requested_pairs and normalized_chain_i is None and normalized_chain_j is None:
        raise HTTPException(
            status_code=400,
            detail="Pair dynamics query must include pairs or chain filters.",
        )

    response = await resolve_state_dynamics_from_state_id(state_id)
    dynamics = response.dynamics_statistics or DynamicsStatisticsInfo()
    matched: List[PairDynamicStatInfo] = []
    for pair_stat in dynamics.pair_stats or []:
        if requested_pairs:
            if not any(
                _pair_stat_matches_requested_pair(
                    pair_stat,
                    requested_pair,
                    default_chain_i=normalized_chain_i,
                    default_chain_j=normalized_chain_j,
                )
                for requested_pair in requested_pairs
            ):
                continue
        elif not _pair_stat_matches_chain_filters(
            pair_stat,
            chain_i=normalized_chain_i,
            chain_j=normalized_chain_j,
        ):
            continue
        matched.append(pair_stat)

    matched.sort(
        key=lambda stat: (
            stat.position_i,
            stat.position_j,
            _normalize_chain_id(stat.chain_i) or "",
            _normalize_chain_id(stat.chain_j) or "",
        )
    )
    limit = _normalize_dynamic_query_limit(query.max_results)
    returned = matched[:limit]

    return LMPStatePairDynamicsQueryResponse(
        state_id=state_id,
        meta=dict(response.meta),
        query=PairDynamicQueryInfo(
            requested_pairs=requested_pairs,
            chain_i=normalized_chain_i,
            chain_j=normalized_chain_j,
            total_available=len(dynamics.pair_stats or []),
            matched_count=len(matched),
            returned_count=len(returned),
            truncated=len(matched) > limit,
        ),
        pair_stats=returned,
    )


async def resolve_state_structure_comparison_ledger_from_state_id(
    state_id: str,
    *,
    allow_afdb_fallback: bool = True,
) -> LMPStateStructureComparisonLedgerResponse:
    response = await resolve_state_receipt_from_state_id(
        state_id,
        allow_afdb_fallback=allow_afdb_fallback,
    )
    return LMPStateStructureComparisonLedgerResponse(
        state_id=state_id,
        meta=dict(response.meta),
        comparison_ledger=_build_afdb_pdb_comparison_ledger(
            response.structural_receipt,
            state_id=state_id,
        ),
    )


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------


async def _load_and_parse(
    accession: str,
    preset: str,
    isoform: Optional[str],
    state: Optional[str],
    sections: List[str],
    per_residue: bool,
) -> LMPAnnotationResponse:
    served_from = "gcs"
    xml_bytes = await _read_from_gcs(preset, accession, isoform, state)
    if xml_bytes is None:
        xml_bytes = _read_from_fs(preset, accession, isoform, state)
        served_from = "filesystem"
    if xml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=f"LMP annotation not found: {_object_key(preset, accession, isoform, state)}",
        )

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise HTTPException(status_code=500, detail=f"malformed LMP XML: {exc}") from exc

    want = set(sections) if "all" not in sections else {
        "identity",
        "semantics",
        "geometry",
        "knowledge_graph",
        "provenance",
    }

    identity = _parse_identity(root) if "identity" in want else None
    semantics = _parse_semantics(root) if "semantics" in want else None
    geometry = _parse_geometry(root, per_residue=per_residue) if "geometry" in want else None
    kg = _parse_knowledge_graph(root) if "knowledge_graph" in want else None
    provenance = _parse_provenance(root, preset=preset, served_from=served_from) if "provenance" in want else None

    meta = {
        "accession": accession,
        "isoform": isoform,
        "state": state,
        "preset": preset,
        "state_id": build_lmp_state_id(preset=preset, accession=accession, isoform=isoform, state=state),
        "served_from": served_from,
        "size_bytes": len(xml_bytes),
        "content_sha256": hashlib.sha256(xml_bytes).hexdigest(),
    }

    return LMPAnnotationResponse(
        meta=meta,
        identity=identity,
        semantics=semantics,
        geometry=geometry,
        knowledge_graph=kg,
        provenance=provenance,
    )


@router.get("/manifest", response_model=ManifestResponse)
async def get_manifest(
    preset: str = Query(_DEFAULT_PRESET),
) -> ManifestResponse:
    """Return manifest of cached LMP annotations.

    Reads `manifest.json` from GCS (generated by `scripts/lmp_corpus_upload.py`)
    or falls back to filesystem scan.
    """
    # GCS manifest first
    try:
        from google.cloud import storage  # type: ignore

        client = storage.Client()
        bucket = client.bucket(_gcs_bucket())
        blob = bucket.blob(f"{preset}/manifest.json")
        if blob.exists():
            data = json.loads(blob.download_as_bytes())
            return ManifestResponse(
                preset=preset,
                total=len(data.get("entries", [])),
                entries=[_with_state_id(ManifestEntry(**e)) for e in data.get("entries", [])],
            )
    except Exception as exc:
        logger.info("GCS manifest unavailable, falling back to FS: %s", exc)

    # Filesystem fallback
    base = _lmp_v4_dir() / preset
    entries: List[ManifestEntry] = []
    if base.exists() and base.is_dir():
        for xml_path in sorted(base.glob("*.xml")):
            stem = xml_path.stem  # P00519_P00519-1_Phosphorylated_Active
            parts = stem.split("_", 2)
            acc = parts[0] if parts else stem
            iso = parts[1] if len(parts) > 1 else None
            stt = parts[2] if len(parts) > 2 else None
            st = xml_path.stat()
            entries.append(
                _with_state_id(
                    ManifestEntry(
                        accession=acc,
                        isoform=iso,
                        state=stt,
                        preset=preset,
                        size_bytes=st.st_size,
                        last_modified=str(int(st.st_mtime)),
                    )
                )
            )
    return ManifestResponse(preset=preset, total=len(entries), entries=entries)


@router.get("/state/{state_id}/receipt", response_model=LMPStateReceiptResponse)
async def get_state_receipt(
    state_id: str,
    allow_afdb_fallback: bool = Query(
        True,
        description="When true, compute AFDB-derived PocketSites if the cached XML does not already contain them.",
    ),
) -> LMPStateReceiptResponse:
    return await resolve_state_receipt_from_state_id(
        state_id,
        allow_afdb_fallback=allow_afdb_fallback,
    )


@router.get("/state/{state_id}/dynamic-statistics", response_model=LMPStateDynamicsResponse)
async def get_state_dynamic_statistics(
    state_id: str,
) -> LMPStateDynamicsResponse:
    return await resolve_state_dynamics_from_state_id(state_id)


@router.post(
    "/state/{state_id}/dynamic-statistics/residue-query",
    response_model=LMPStateResidueDynamicsQueryResponse,
)
async def query_state_residue_dynamic_statistics(
    state_id: str,
    query: ResidueDynamicQueryRequest,
) -> LMPStateResidueDynamicsQueryResponse:
    return await resolve_state_residue_dynamics_query_from_state_id(state_id, query)


@router.post(
    "/state/{state_id}/dynamic-statistics/pair-query",
    response_model=LMPStatePairDynamicsQueryResponse,
)
async def query_state_pair_dynamic_statistics(
    state_id: str,
    query: PairDynamicQueryRequest,
) -> LMPStatePairDynamicsQueryResponse:
    return await resolve_state_pair_dynamics_query_from_state_id(state_id, query)


@router.get(
    "/state/{state_id}/structure-comparison-ledger",
    response_model=LMPStateStructureComparisonLedgerResponse,
)
async def get_state_structure_comparison_ledger(
    state_id: str,
    allow_afdb_fallback: bool = Query(
        True,
        description="When true, enrich the StructureCatalog with AFDB fallback before materializing the comparison ledger.",
    ),
) -> LMPStateStructureComparisonLedgerResponse:
    return await resolve_state_structure_comparison_ledger_from_state_id(
        state_id,
        allow_afdb_fallback=allow_afdb_fallback,
    )


@router.get("/{accession}", response_model=LMPAnnotationResponse)
async def get_annotation(
    accession: str,
    preset: str = Query(_DEFAULT_PRESET),
    isoform: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    include: Optional[str] = Query(
        None,
        description="Comma-separated sections: identity,semantics,geometry,knowledge_graph,provenance. Default=all. Append geometry.per_residue to include pLDDT.",
    ),
) -> LMPAnnotationResponse:
    sections: List[str] = []
    per_residue = False
    if include:
        for piece in (p.strip() for p in include.split(",") if p.strip()):
            if piece == "geometry.per_residue":
                per_residue = True
                sections.append("geometry")
            elif piece in _VALID_SECTIONS:
                sections.append(piece)
            else:
                raise HTTPException(status_code=400, detail=f"Unknown section: {piece}")
    if not sections:
        sections = ["all"]

    cache_key = _cache_key(preset, accession, isoform, state, ",".join(sorted(sections)) + (":pr" if per_residue else ""))
    cached = await _cache_get(cache_key)
    if cached:
        try:
            return LMPAnnotationResponse(**json.loads(cached))
        except Exception:
            pass

    result = await _load_and_parse(accession, preset, isoform, state, sections, per_residue)
    try:
        await _cache_set(cache_key, result.model_dump_json(), ttl_seconds=3600)
    except Exception:
        pass
    return result


@router.get("/{accession}/section/{section}")
async def get_annotation_section(
    accession: str,
    section: str,
    preset: str = Query(_DEFAULT_PRESET),
    isoform: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    per_residue: bool = Query(False),
) -> Dict[str, Any]:
    if section not in _VALID_SECTIONS or section == "all":
        raise HTTPException(status_code=400, detail=f"Invalid section: {section}")
    result = await _load_and_parse(
        accession, preset, isoform, state, [section], per_residue if section == "geometry" else False
    )
    block = getattr(result, section)
    return {
        "meta": result.meta,
        section: (block.model_dump() if hasattr(block, "model_dump") else block),
    }
