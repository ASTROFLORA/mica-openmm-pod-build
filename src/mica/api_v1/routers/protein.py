"""
Protein Knowledge Resolver — App-wide entity resolution endpoint.

Accepts any protein-related query (UniProt accession, gene symbol, PDB id,
AlphaFold id, LMP filename, free-text) and returns a canonical entity with
typed available knowledge sources.

Endpoint:
    GET /api/v1/protein/resolve?q={query}
"""

from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/protein",
    tags=["protein-resolver"],
    dependencies=[Depends(user_dependency)],
)

_NS = "http://ai-university.edu/lmp/v4.0"
_UNIPROT_RE = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}$",
    re.I,
)
_PDB_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_AF_RE = re.compile(r"^AF[_-][A-Za-z0-9]+", re.I)


def _lmp_v4_dir() -> Path:
    override = os.getenv("MICA_LMP_V4_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).resolve().parents[4] / ".tmp_lmp_v4").resolve()


# ── Response models ────────────────────────────────────────────────────────

class SourceMatch(BaseModel):
    source: str  # "uniprot", "pdb", "alphafold", "lmp_xml", "library", "local"
    id: str
    label: str
    confidence: float = 1.0


class AvailableSource(BaseModel):
    source: str  # "session_assets", "lmp", "budo", "library", "full_text", "pdb", "alphafold", "uniprot"
    available: bool
    count: int = 0
    status: str = "ok"  # "ok", "blocked", "degraded", "local_only"
    detail: Optional[str] = None


class ProteinResolveResponse(BaseModel):
    query: str
    resolved: bool
    canonical_entity_id: Optional[str] = None
    accession: Optional[str] = None
    gene: Optional[str] = None
    organism: Optional[str] = None
    protein_name: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    source_matches: List[SourceMatch] = Field(default_factory=list)
    confidence: float = 0.0
    available_sources: List[AvailableSource] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)


# ── Query classification ───────────────────────────────────────────────────

def _classify_query(q: str) -> Tuple[str, str]:
    """Return (kind, normalized_id) for the query."""
    s = q.strip()
    if not s:
        return ("empty", "")

    # Exact UniProt accession pattern
    if _UNIPROT_RE.match(s):
        return ("uniprot", s.upper())

    # Exact PDB id pattern
    if _PDB_RE.match(s):
        return ("pdb", s.upper())

    # AlphaFold ID
    if _AF_RE.match(s):
        normalized = s.replace("AF_", "AF-").upper()
        return ("alphafold", normalized)

    # LMP XML filename (e.g., Q9H4A3_WNK1_full_Apo_Inactive.xml)
    if s.lower().endswith(".xml"):
        return ("lmp_xml_filename", s)

    # Looks like a gene symbol (short, uppercase alphanumeric)
    if re.match(r"^[A-Z][A-Z0-9]{1,7}$", s.upper()):
        return ("gene_symbol", s.upper())

    # Free-text
    return ("free_text", s)


# ── LMP XML scanning ───────────────────────────────────────────────────────

def _scan_lmp_for_accession(accession: str) -> List[Dict[str, Any]]:
    """Find LMP XML files matching a UniProt accession."""
    results: List[Dict[str, Any]] = []
    base = _lmp_v4_dir()
    if not base.exists() or not base.is_dir():
        return results

    accession_upper = accession.upper()
    for path in sorted(base.glob("*.xml")):
        try:
            root = ET.parse(path).getroot()
            ns = {"l": _NS}
            identity = root.find("l:Identity", ns)
            if identity is None:
                continue
            primary = identity.findtext("l:PrimaryAccession", default="", namespaces=ns) or ""
            if primary.upper() == accession_upper:
                results.append({
                    "filename": path.name,
                    "path": str(path),
                    "budo_id": identity.findtext("l:BudoID", default=None, namespaces=ns),
                    "preset": root.get("preset", "unknown"),
                    "size_bytes": path.stat().st_size,
                    "source": "local_filesystem",
                })
        except Exception:
            continue
    return results


def _scan_lmp_for_gene(gene_symbol: str) -> List[Dict[str, Any]]:
    """Find LMP XML files matching a gene symbol."""
    results: List[Dict[str, Any]] = []
    base = _lmp_v4_dir()
    if not base.exists() or not base.is_dir():
        return results

    gene_upper = gene_symbol.upper()
    for path in sorted(base.glob("*.xml")):
        try:
            root = ET.parse(path).getroot()
            ns = {"l": _NS}
            identity = root.find("l:Identity", ns)
            semantics = root.find("l:Semantics", ns)
            if identity is None:
                continue
            # Check gene in semantics
            if semantics is not None:
                genes_el = semantics.find("l:Genes", ns)
                if genes_el is not None:
                    for gene_el in genes_el.findall("l:Value", ns):
                        if (gene_el.text or "").strip().upper() == gene_upper:
                            results.append({
                                "filename": path.name,
                                "path": str(path),
                                "accession": identity.findtext("l:PrimaryAccession", default="", namespaces=ns) or "",
                                "budo_id": identity.findtext("l:BudoID", default=None, namespaces=ns),
                                "preset": root.get("preset", "unknown"),
                                "size_bytes": path.stat().st_size,
                                "source": "local_filesystem",
                            })
                            break
        except Exception:
            continue
    return results


# ── Resolver ────────────────────────────────────────────────────────────────

@router.get("/resolve", response_model=ProteinResolveResponse)
async def resolve_protein(
    q: str = Query(..., min_length=1, description="Protein query: accession, gene, PDB id, AlphaFold id, or free text"),
) -> ProteinResolveResponse:
    """Resolve a protein query to a canonical entity with available knowledge sources."""

    query_kind, normalized = _classify_query(q)
    warnings: List[str] = []
    blockers: List[str] = []
    source_matches: List[SourceMatch] = []
    available_sources: List[AvailableSource] = []

    canonical_id: Optional[str] = None
    accession: Optional[str] = None
    gene: Optional[str] = None
    organism: Optional[str] = None
    protein_name: Optional[str] = None
    aliases: List[str] = []
    confidence: float = 0.0

    # ── Resolve by kind ────────────────────────────────────────────────
    lmp_matches: List[Dict[str, Any]] = []

    if query_kind == "uniprot":
        accession = normalized
        canonical_id = f"uniprot:{accession}"
        confidence = 1.0
        source_matches.append(SourceMatch(source="uniprot", id=accession, label=f"UniProt {accession}"))
        lmp_matches = _scan_lmp_for_accession(accession)

    elif query_kind == "pdb":
        canonical_id = f"pdb:{normalized}"
        confidence = 0.95
        source_matches.append(SourceMatch(source="pdb", id=normalized, label=f"PDB {normalized}"))

    elif query_kind == "alphafold":
        canonical_id = f"alphafold:{normalized}"
        # Extract UniProt accession from AF-ACCESSION-F1
        af_match = re.match(r"^AF-([A-Z0-9]+)", normalized)
        if af_match:
            accession = af_match.group(1)
            if _UNIPROT_RE.match(accession):
                lmp_matches = _scan_lmp_for_accession(accession)
                source_matches.append(SourceMatch(source="uniprot", id=accession, label=f"UniProt {accession}"))
        confidence = 0.9
        source_matches.append(SourceMatch(source="alphafold", id=normalized, label=f"AlphaFold {normalized}"))

    elif query_kind == "gene_symbol":
        gene = normalized
        canonical_id = f"gene:{gene}"
        confidence = 0.7
        source_matches.append(SourceMatch(source="library", id=gene, label=f"Gene {gene}"))
        lmp_matches = _scan_lmp_for_gene(gene)
        if lmp_matches:
            first = lmp_matches[0]
            accession = first.get("accession", "")
            canonical_id = f"uniprot:{accession}" if accession else canonical_id

    elif query_kind == "lmp_xml_filename":
        canonical_id = f"lmp_xml:{normalized}"
        confidence = 0.8
        source_matches.append(SourceMatch(source="lmp_xml", id=normalized, label=f"LMP XML {normalized}"))

    else:  # free_text
        # Try to match against LMP by gene name
        lmp_matches = _scan_lmp_for_gene(normalized)
        if not lmp_matches:
            # Try as accession
            lmp_matches = _scan_lmp_for_accession(normalized)
        if lmp_matches:
            gene = normalized.upper()
            first = lmp_matches[0]
            accession = first.get("accession", "")
            canonical_id = f"uniprot:{accession}" if accession else f"gene:{gene}"
            confidence = 0.6
        else:
            canonical_id = f"free_text:{normalized}"
            confidence = 0.3

    # ── Populate from LMP matches ──────────────────────────────────────
    if lmp_matches:
        first = lmp_matches[0]
        canonical_id = f"uniprot:{first.get('accession', accession or '')}" if first.get("accession") else canonical_id
        try:
            root = ET.parse(first["path"]).getroot()
            ns = {"l": _NS}
            identity = root.find("l:Identity", ns)
            if identity is not None:
                accession = accession or identity.findtext("l:PrimaryAccession", default="", namespaces=ns) or ""
                organism_el = identity.find("l:Organism", ns)
                organism = organism_el.text if organism_el is not None and organism_el.text else organism
                if not organism:
                    org_name = (organism_el.get("name") if organism_el is not None else None) or ""
                    organism = org_name or organism
            semantics = root.find("l:Semantics", ns)
            if semantics is not None:
                protein_name = semantics.findtext("l:ProteinName", default="", namespaces=ns) or protein_name
                genes_el = semantics.find("l:Genes", ns)
                if genes_el is not None:
                    first_gene = genes_el.find("l:Value", ns)
                    gene = (first_gene.text or "").strip() if first_gene is not None else gene
        except Exception:
            pass

        for m in lmp_matches:
            source_matches.append(SourceMatch(
                source="lmp_xml",
                id=m["filename"],
                label=f"{m['preset']} — {m['filename']}",
                confidence=0.85,
            ))

    # ── Build available_sources ─────────────────────────────────────────
    available_sources = [
        AvailableSource(
            source="session_assets",
            available=True,  # Always available client-side
            count=0,
            status="ok",
            detail="Checked client-side via workspace session store",
        ),
        AvailableSource(
            source="lmp",
            available=len(lmp_matches) > 0,
            count=len(lmp_matches),
            status="ok" if lmp_matches else "local_only",
            detail=f"{len(lmp_matches)} LMP XML files found on local filesystem" if lmp_matches else "No LMP XML files found locally; GCS upload pending",
        ),
        AvailableSource(
            source="budo",
            available=True,
            count=len(lmp_matches),
            status="degraded",
            detail="BUDO query API available via GET /api/v1/budo/{accession} — uses LMP XML fallback when Neo4j unavailable",
        ),
        AvailableSource(
            source="library",
            available=True,
            count=0,
            status="ok",
            detail="Available via GET /api/v1/library/search",
        ),
        AvailableSource(
            source="full_text",
            available=True,
            count=0,
            status="degraded",
            detail="DLM full-text detection via POST /api/v1/dlm/fulltext/detect — searches DLM cache and LMP XML index",
        ),
        AvailableSource(
            source="pdb",
            available=True,
            count=0,
            status="ok",
            detail="Available via RCSB search API",
        ),
        AvailableSource(
            source="alphafold",
            available=True,
            count=0,
            status="ok",
            detail="Available via EBI AlphaFold API",
        ),
        AvailableSource(
            source="uniprot",
            available=accession is not None and len(accession) > 0,
            count=1 if accession else 0,
            status="ok" if accession else "degraded",
            detail=f"UniProt accession: {accession}" if accession else "No UniProt accession resolved",
        ),
    ]

    if not lmp_matches and query_kind in ("uniprot", "alphafold"):
        warnings.append("lmp_xml_local_only: LMP XML files exist locally but are not in GCS bucket mica-public-lmp-v4. Upload required for production annotation service.")

    if query_kind == "free_text" and confidence < 0.5:
        warnings.append("low_confidence_free_text: query did not match any known protein pattern. Try a UniProt accession or gene symbol.")

    return ProteinResolveResponse(
        query=q,
        resolved=confidence > 0.3,
        canonical_entity_id=canonical_id,
        accession=accession,
        gene=gene,
        organism=organism,
        protein_name=protein_name,
        aliases=aliases,
        source_matches=source_matches,
        confidence=confidence,
        available_sources=available_sources,
        warnings=warnings,
        blockers=blockers,
    )


# ── Knowledge Tree ─────────────────────────────────────────────────────────

class KnowledgeTreeNode(BaseModel):
    id: str
    label: str
    type: str  # entity, structure, lmp_xml, domain, pathway, go_term, paper, session_asset, analysis, action
    source: str = "resolver"
    confidence: float = 1.0
    asset_ref: Optional[str] = None
    entity_ref: Optional[str] = None
    evidence_refs: List[str] = Field(default_factory=list)
    children: List["KnowledgeTreeNode"] = Field(default_factory=list)
    actions_available: List[str] = Field(default_factory=list)
    status: str = "ok"
    warnings: List[str] = Field(default_factory=list)


class KnowledgeTreeResponse(BaseModel):
    entity_id: str
    tree: KnowledgeTreeNode


def _build_knowledge_tree(
    accession: str,
    gene: Optional[str],
    organism: Optional[str],
    protein_name: Optional[str],
    lmp_matches: List[Dict[str, Any]],
) -> KnowledgeTreeNode:
    """Build a typed knowledge tree for a resolved protein entity."""

    entity_label = protein_name or gene or accession or "Unknown Protein"
    entity_children: List[KnowledgeTreeNode] = []

    # ── 2. Structures branch ────────────────────────────────────────────
    structure_children: List[KnowledgeTreeNode] = []

    # AlphaFold
    if accession:
        af_id = f"AF-{accession}-F1"
        structure_children.append(KnowledgeTreeNode(
            id=f"alphafold:{af_id}",
            label=f"AlphaFold {af_id}",
            type="structure",
            source="alphafold",
            confidence=0.85,
            actions_available=["open_structure", "annotate_with_lmp", "compare_states"],
            status="ok",
        ))

    # PDB structures from LMP graph
    pdb_ids_found: set = set()
    if lmp_matches:
        for m in lmp_matches:
            try:
                root = ET.parse(m["path"]).getroot()
                ns = {"l": _NS}
                kg = root.find("l:KnowledgeGraph", ns)
                if kg is not None:
                    for edge in kg.findall("l:Edge", ns):
                        if edge.get("type") == "HAS_STRUCTURE" and edge.get("db") == "PDB":
                            pdb_id = edge.get("id", "")
                            if pdb_id and pdb_id not in pdb_ids_found:
                                pdb_ids_found.add(pdb_id)
                                # Also look for resolution in CrossReferences
                                resolution = None
                                method = None
                                chains = None
                                for xref in kg.findall("l:CrossReference", ns):
                                    if xref.get("db") == "PDB" and xref.get("id") == pdb_id:
                                        for prop in xref.findall("l:Property", ns):
                                            pname = prop.get("name", "")
                                            if pname == "Resolution":
                                                resolution = (prop.text or "").strip()
                                            elif pname == "Method":
                                                method = (prop.text or "").strip()
                                            elif pname == "Chains":
                                                chains = (prop.text or "").strip()
                                detail = f"PDB {pdb_id}"
                                if resolution:
                                    detail += f" · {resolution}"
                                if method:
                                    detail += f" · {method}"
                                structure_children.append(KnowledgeTreeNode(
                                    id=f"pdb:{pdb_id}",
                                    label=detail,
                                    type="structure",
                                    source="pdb",
                                    asset_ref=f"pdb:{pdb_id}",
                                    confidence=0.95,
                                    actions_available=["open_structure", "view_budo_graph", "compare_states"],
                                    status="ok",
                                ))
            except Exception:
                continue

    if structure_children:
        entity_children.append(KnowledgeTreeNode(
            id=f"structures_{accession or 'unknown'}",
            label="Structures",
            type="structure",
            source="lmp+alphafold+pdb",
            children=structure_children,
            status="ok" if structure_children else "empty",
        ))

    # ── 3. LMP XMLs branch ──────────────────────────────────────────────
    lmp_xml_children: List[KnowledgeTreeNode] = []
    for m in lmp_matches:
        status = "ok"
        warnings_list: List[str] = []
        source_label = m.get("source", "local_filesystem")
        if source_label == "local_filesystem":
            warnings_list.append("lmp_xml_local_only: file not in GCS bucket")
            status = "local_only"
        lmp_xml_children.append(KnowledgeTreeNode(
            id=f"lmp_xml:{m['filename']}",
            label=f"{m.get('preset', 'unknown')} — {m['filename']}",
            type="lmp_xml",
            source="lmp",
            asset_ref=m.get("path", ""),
            entity_ref=m.get("budo_id", ""),
            actions_available=["view_lmp_xml", "view_budo_graph", "compare_states"],
            status=status,
            warnings=warnings_list,
        ))
    if lmp_xml_children:
        entity_children.append(KnowledgeTreeNode(
            id=f"lmp_xmls_{accession or 'unknown'}",
            label="LMP XMLs",
            type="lmp_xml",
            source="lmp",
            children=lmp_xml_children,
            status="ok" if all(c.status == "ok" for c in lmp_xml_children) else "local_only",
        ))
    else:
        entity_children.append(KnowledgeTreeNode(
            id=f"lmp_xmls_{accession or 'unknown'}",
            label="LMP XMLs",
            type="lmp_xml",
            source="lmp",
            status="empty",
            warnings=["lmp_xml_bucket_missing: No LMP XML files found; upload to GCS mica-public-lmp-v4 required"],
        ))

    # ── 4. BUDO Knowledge branch ────────────────────────────────────────
    budo_children: List[KnowledgeTreeNode] = []
    # Extract domains from LMP
    if lmp_matches:
        for m in lmp_matches:
            try:
                root = ET.parse(m["path"]).getroot()
                ns = {"l": _NS}
                geometry = root.find("l:Geometry", ns)
                if geometry is not None:
                    chain_el = geometry.find("l:Chain", ns)
                    if chain_el is not None:
                        for domain_el in chain_el.findall("l:Domain", ns):
                            budo_children.append(KnowledgeTreeNode(
                                id=f"domain:{domain_el.get('name', '')}",
                                label=f"{domain_el.get('name', '')} ({domain_el.get('start', '')}-{domain_el.get('end', '')}) [{domain_el.get('type', '')}]",
                                type="domain",
                                source="lmp",
                                confidence=0.85,
                            ))
            except Exception:
                continue

    if budo_children:
        entity_children.append(KnowledgeTreeNode(
            id=f"budo_{accession or 'unknown'}",
            label="BUDO Knowledge",
            type="domain",
            source="budo",
            children=budo_children,
            status="ok",
        ))
    else:
        entity_children.append(KnowledgeTreeNode(
            id=f"budo_{accession or 'unknown'}",
            label="BUDO Knowledge",
            type="domain",
            source="budo",
            status="degraded",
            actions_available=["view_budo_graph", "query_budo_api"],
            warnings=["budo_neo4j_query_available: GET /api/v1/budo/{accession} provides LMP XML fallback data"],
        ))

    # ── 5. Literature / Library branch ──────────────────────────────────
    lit_children: List[KnowledgeTreeNode] = [
        KnowledgeTreeNode(
            id=f"library_search_{accession or 'unknown'}",
            label=f"Search Library for {gene or accession or 'protein'}",
            type="paper",
            source="library",
            actions_available=["search_literature"],
            status="ok",
        ),
        KnowledgeTreeNode(
            id=f"library_mentions_{accession or 'unknown'}",
            label=f"Entity Mentions for {accession or gene or 'protein'}",
            type="paper",
            source="library",
            actions_available=["view_mentions"],
            entity_ref=f"GET /api/v1/library/entities/{accession or gene or 'unknown'}/mentions",
            status="ok",
        ),
        KnowledgeTreeNode(
            id=f"dlm_fulltext_{accession or 'unknown'}",
            label=f"DLM Full-Text Detection: {gene or accession or 'protein'}",
            type="paper",
            source="full_text",
            actions_available=["detect_fulltext"],
            entity_ref="POST /api/v1/dlm/fulltext/detect",
            status="ok",
        ),
    ]
    entity_children.append(KnowledgeTreeNode(
        id=f"literature_{accession or 'unknown'}",
        label="Literature & Library",
        type="paper",
        source="library",
        status="ok",
        actions_available=["search_literature", "attach_literature", "view_citations"],
        children=lit_children,
    ))

    # ── 6. Session Assets branch ────────────────────────────────────────
    entity_children.append(KnowledgeTreeNode(
        id=f"session_{accession or 'unknown'}",
        label="Session Assets",
        type="session_asset",
        source="session",
        status="ok",
        actions_available=["open_in_workspace", "upload_asset"],
        warnings=["session_assets_checked_client_side"],
    ))

    # ── 7. Analyses branch ──────────────────────────────────────────────
    entity_children.append(KnowledgeTreeNode(
        id=f"analyses_{accession or 'unknown'}",
        label="Analyses",
        type="analysis",
        source="analytics",
        status="ok",
        actions_available=["run_biodynamo", "compare_states", "export_packet"],
        children=[
            KnowledgeTreeNode(
                id=f"analysis_smic_{accession or 'unknown'}",
                label="SMIC Metrics",
                type="analysis",
                source="smic",
                actions_available=["run_smic"],
                status="ok",
            ),
            KnowledgeTreeNode(
                id=f"analysis_biodynamo_{accession or 'unknown'}",
                label="BioDynamo Simulations",
                type="analysis",
                source="biodynamo",
                actions_available=["run_biodynamo"],
                status="ok",
            ),
        ],
    ))

    # ── 8. Actions branch ───────────────────────────────────────────────
    entity_children.append(KnowledgeTreeNode(
        id=f"actions_{accession or 'unknown'}",
        label="Actions",
        type="action",
        source="resolver",
        status="ok",
        children=[
            KnowledgeTreeNode(
                id=f"action_open_{accession or 'unknown'}",
                label="Open Structure",
                type="action",
                actions_available=["open_structure"],
                status="ok",
            ),
            KnowledgeTreeNode(
                id=f"action_annotate_{accession or 'unknown'}",
                label="Annotate with LMP",
                type="action",
                actions_available=["annotate_with_lmp"],
                status="ok" if lmp_matches else "blocked",
            ),
            KnowledgeTreeNode(
                id=f"action_budo_{accession or 'unknown'}",
                label="View BUDO Graph",
                type="action",
                actions_available=["view_budo_graph"],
                status="ok" if lmp_matches else "blocked",
            ),
            KnowledgeTreeNode(
                id=f"action_msa_{accession or 'unknown'}",
                label="Send to MSA",
                type="action",
                actions_available=["send_to_msa"],
                status="ok",
            ),
            KnowledgeTreeNode(
                id=f"action_export_{accession or 'unknown'}",
                label="Export Packet",
                type="action",
                actions_available=["export_packet"],
                status="ok",
            ),
        ],
    ))

    return KnowledgeTreeNode(
        id=f"entity:{accession or gene or 'unknown'}",
        label=entity_label,
        type="entity",
        source="resolver",
        children=entity_children,
        status="ok",
    )


@router.get("/{entity_id}/knowledge-tree", response_model=KnowledgeTreeResponse)
async def protein_knowledge_tree(
    entity_id: str,
    q: Optional[str] = Query(default=None, description="Optional fallback query for resolution"),
) -> KnowledgeTreeResponse:
    """Return the full knowledge tree for a protein entity.

    The tree has branches: Entity, Structures, LMP XMLs, BUDO Knowledge,
    Literature, Session Assets, Analyses, Actions.
    """
    # Clean entity_id (strip prefix like "uniprot:", "pdb:", etc.)
    clean_id = entity_id
    for prefix in ("uniprot:", "pdb:", "alphafold:", "gene:", "lmp_xml:"):
        if entity_id.startswith(prefix):
            clean_id = entity_id[len(prefix):]
            break

    # Resolve to accession
    query_kind, normalized = _classify_query(clean_id)
    accession: Optional[str] = None
    gene: Optional[str] = None
    organism: Optional[str] = None
    protein_name: Optional[str] = None
    lmp_matches: List[Dict[str, Any]] = []

    if query_kind == "uniprot":
        accession = normalized
        lmp_matches = _scan_lmp_for_accession(accession)
    elif query_kind == "gene_symbol":
        gene = normalized
        lmp_matches = _scan_lmp_for_gene(gene)
        if lmp_matches:
            accession = lmp_matches[0].get("accession", "")
    elif query_kind == "alphafold":
        af_match = re.match(r"^AF-([A-Z0-9]+)", normalized)
        if af_match:
            accession = af_match.group(1)
            lmp_matches = _scan_lmp_for_accession(accession)
    elif query_kind == "pdb":
        # PDB-only: no LMP, but still build tree
        pass
    elif q:
        # Fallback via resolve
        _, resolved_normalized = _classify_query(q)
        if _UNIPROT_RE.match(resolved_normalized):
            accession = resolved_normalized.upper()
            lmp_matches = _scan_lmp_for_accession(accession)
        else:
            lmp_matches = _scan_lmp_for_gene(resolved_normalized)
            if lmp_matches:
                accession = lmp_matches[0].get("accession", "")
                gene = resolved_normalized.upper()

    # Populate metadata from LMP
    if lmp_matches:
        try:
            root = ET.parse(lmp_matches[0]["path"]).getroot()
            ns = {"l": _NS}
            identity = root.find("l:Identity", ns)
            if identity is not None:
                accession = accession or identity.findtext("l:PrimaryAccession", default="", namespaces=ns) or ""
                organism_el = identity.find("l:Organism", ns)
                organism = organism_el.text if organism_el is not None and organism_el.text else organism
            semantics = root.find("l:Semantics", ns)
            if semantics is not None:
                protein_name = semantics.findtext("l:ProteinName", default="", namespaces=ns) or protein_name
                genes_el = semantics.find("l:Genes", ns)
                if genes_el is not None:
                    first_gene = genes_el.find("l:Value", ns)
                    gene = (first_gene.text or "").strip() if first_gene is not None else gene
        except Exception:
            pass

    tree = _build_knowledge_tree(
        accession=accession or clean_id,
        gene=gene,
        organism=organism,
        protein_name=protein_name,
        lmp_matches=lmp_matches,
    )

    return KnowledgeTreeResponse(entity_id=entity_id, tree=tree)
