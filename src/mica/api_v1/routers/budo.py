"""
BUDO Query HTTP API
===================

Exposes BUDO (Biological Unified Data Object) data via HTTP for the Alejandria frontend.
Wraps BudoNeo4jService for entity queries, domain/feature/variant retrieval.

Endpoints:
    GET /api/v1/budo/{accession}      — Full BUDO entity by UniProt accession
    GET /api/v1/budo/{accession}/domains  — Domains only
    GET /api/v1/budo/{accession}/variants — Variants only
    GET /api/v1/budo/health              — Health + connection status
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/budo",
    tags=["budo"],
    dependencies=[Depends(user_dependency)],
)

# ── Lazy Neo4j service ─────────────────────────────────────────────────────
_neo4j_service = None
_neo4j_init_error: Optional[str] = None
_neo4j_available = False


def _get_neo4j_service():
    """Lazy-init Neo4j service with environment-based connection."""
    global _neo4j_service, _neo4j_init_error, _neo4j_available

    if _neo4j_service is not None:
        return _neo4j_service

    if _neo4j_init_error is not None:
        return None

    uri = os.getenv("NEO4J_URI", os.getenv("BUDO_NEO4J_URI", ""))
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", os.getenv("BUDO_NEO4J_PASSWORD", ""))

    if not uri:
        _neo4j_init_error = "NEO4J_URI not configured — BUDO persistence is available but not connected"
        _neo4j_available = False
        return None

    try:
        from src.bsm.budo.neo4j_service import BudoNeo4jService
        _neo4j_service = BudoNeo4jService(uri=uri, user=user, password=password)
        _neo4j_available = True
        return _neo4j_service
    except Exception as exc:
        _neo4j_init_error = f"Neo4j initialization failed: {exc}"
        _neo4j_available = False
        return None


# ── Response models ─────────────────────────────────────────────────────────

class BudoDomain(BaseModel):
    name: str
    pfam_id: Optional[str] = None
    interpro_id: Optional[str] = None
    start_position: int
    end_position: int
    confidence_score: float = 1.0


class BudoVariant(BaseModel):
    position: int
    wild_type: str
    mutant_type: str
    variant_type: Optional[str] = None
    clinical_significance: Optional[str] = None
    dbsnp_id: Optional[str] = None


class BudoCrossReference(BaseModel):
    database: str
    identifier: str
    confidence_score: float = 1.0


class BudoEntityResponse(BaseModel):
    accession: str
    budo_id: Optional[str] = None
    name: Optional[str] = None
    gene_symbol: Optional[str] = None
    organism: Optional[str] = None
    functional_state: Optional[str] = None
    domains: List[BudoDomain] = Field(default_factory=list)
    variants: List[BudoVariant] = Field(default_factory=list)
    cross_references: List[BudoCrossReference] = Field(default_factory=list)
    available: bool = False
    blockers: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class BudoHealthResponse(BaseModel):
    ok: bool
    neo4j_available: bool
    neo4j_error: Optional[str] = None
    lmp_v4_xml_count: int = 0


# ── LMP XML fallback (when Neo4j is unavailable) ────────────────────────────

def _find_lmp_xml_for_accession(accession: str) -> Optional[Path]:
    """Find an LMP XML file for a given UniProt accession."""
    lmp_dir = Path(__file__).resolve().parents[4] / ".tmp_lmp_v4"
    if not lmp_dir.exists():
        return None
    acc_upper = accession.upper()
    import xml.etree.ElementTree as ET
    _NS_LOCAL = "http://ai-university.edu/lmp/v4.0"
    for path in sorted(lmp_dir.glob("*.xml")):
        try:
            root = ET.parse(path).getroot()
            ns = {"l": _NS_LOCAL}
            identity = root.find("l:Identity", ns)
            if identity is None:
                continue
            primary = identity.findtext("l:PrimaryAccession", default="", namespaces=ns) or ""
            if primary.upper() == acc_upper:
                return path
        except Exception:
            continue
    return None


def _extract_budo_from_lmp(path: Path) -> Dict[str, Any]:
    """Extract BUDO-like data from LMP XML as fallback when Neo4j is unavailable."""
    import xml.etree.ElementTree as ET
    _NS_LOCAL = "http://ai-university.edu/lmp/v4.0"
    ns = {"l": _NS_LOCAL}

    result: Dict[str, Any] = {
        "domains": [],
        "variants": [],
        "cross_references": [],
    }

    try:
        root = ET.parse(path).getroot()
        identity = root.find("l:Identity", ns)
        semantics = root.find("l:Semantics", ns)
        geometry = root.find("l:Geometry", ns)
        kg = root.find("l:KnowledgeGraph", ns)

        if identity is not None:
            result["budo_id"] = identity.findtext("l:BudoID", default="", namespaces=ns) or ""
            result["accession"] = identity.findtext("l:PrimaryAccession", default="", namespaces=ns) or ""
            org_el = identity.find("l:Organism", ns)
            result["organism"] = (org_el.text or "").strip() if org_el is not None and org_el.text else (
                org_el.get("name", "") if org_el is not None else ""
            )

        if semantics is not None:
            result["name"] = semantics.findtext("l:ProteinName", default="", namespaces=ns) or ""
            genes_el = semantics.find("l:Genes", ns)
            if genes_el is not None:
                first_gene = genes_el.find("l:Value", ns)
                result["gene_symbol"] = (first_gene.text or "").strip() if first_gene is not None else ""

        # Extract domains from Geometry
        if geometry is not None:
            for dom in geometry.findall(".//l:Domain", ns):
                name = dom.get("name", "")
                start_val = int(dom.get("start", 0))
                end_val = int(dom.get("end", 0))
                interpro_id = dom.get("interpro_id")
                if name:
                    result["domains"].append({
                        "name": name,
                        "start_position": start_val,
                        "end_position": end_val,
                        "interpro_id": interpro_id,
                        "confidence_score": 0.9,
                    })

        # Extract variants from annotations
        # (LMP XML embeds variant info in Provenance/GroundTruthEntry)

        # Extract cross-references from KnowledgeGraph
        if kg is not None:
            for xref in kg.findall("l:CrossReference", ns):
                db = xref.get("db", "")
                xid = xref.get("id", "")
                if db and xid:
                    result["cross_references"].append({
                        "database": db,
                        "identifier": f"{db}:{xid}",
                        "confidence_score": 0.85,
                    })

    except Exception:
        pass

    return result


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/{accession}", response_model=BudoEntityResponse)
async def get_budo_entity(
    accession: str,
) -> BudoEntityResponse:
    """Query BUDO entity by UniProt accession.

    Falls back to LMP XML extraction if Neo4j is unavailable.
    """
    blockers: List[str] = []
    warnings: List[str] = []

    acc = accession.strip().upper()

    # Try Neo4j first
    service = _get_neo4j_service()
    if service is not None:
        try:
            budo_id = f"budo:{acc}_HUMAN_v2"
            entity = service.get_budo_by_id(budo_id)
            if entity:
                budo_data = entity.get("budo", {})
                return BudoEntityResponse(
                    accession=acc,
                    budo_id=budo_id,
                    name=budo_data.get("name"),
                    gene_symbol=budo_data.get("gene_symbol"),
                    organism=budo_data.get("organism"),
                    functional_state=budo_data.get("functional_state"),
                    domains=[
                        BudoDomain(
                            name=d.get("name", ""),
                            pfam_id=d.get("pfam_id"),
                            interpro_id=d.get("interpro_id"),
                            start_position=d.get("start_position", 0),
                            end_position=d.get("end_position", 0),
                            confidence_score=d.get("confidence_score", 1.0),
                        )
                        for d in (entity.get("domains") or [])
                    ],
                    variants=[
                        BudoVariant(
                            position=v.get("position", 0),
                            wild_type=v.get("wild_type", ""),
                            mutant_type=v.get("mutant_type", ""),
                            variant_type=v.get("variant_type"),
                            clinical_significance=v.get("clinical_significance"),
                            dbsnp_id=v.get("dbsnp_id"),
                        )
                        for v in (entity.get("variants") or [])
                    ],
                    cross_references=[
                        BudoCrossReference(
                            database=x.get("database", ""),
                            identifier=x.get("identifier", ""),
                            confidence_score=x.get("confidence_score", 1.0),
                        )
                        for x in entity.get("cross_references", [])
                    ],
                    available=True,
                )
        except Exception as exc:
            warnings.append(f"Neo4j query failed: {exc}. Falling back to LMP XML extraction.")

    # Fallback: LMP XML extraction
    blockers.append(
        "budo_neo4j_unavailable: NEO4J_URI not configured or connection failed. "
        "BUDO data extracted from LMP XML as degraded fallback."
    )
    warnings.append(_neo4j_init_error or "Neo4j service not initialized")

    lmp_path = _find_lmp_xml_for_accession(acc)
    if not lmp_path:
        blockers.append("budo_lmp_xml_missing: no LMP XML file found for this accession. BUDO data unavailable.")
        return BudoEntityResponse(
            accession=acc,
            available=False,
            blockers=blockers,
            warnings=warnings,
        )

    data = _extract_budo_from_lmp(lmp_path)
    if not data.get("accession"):
        return BudoEntityResponse(
            accession=acc,
            available=False,
            blockers=blockers + ["budo_lmp_parse_failed: could not extract BUDO data from LMP XML"],
            warnings=warnings,
        )

    return BudoEntityResponse(
        accession=acc,
        budo_id=data.get("budo_id"),
        name=data.get("name"),
        gene_symbol=data.get("gene_symbol"),
        organism=data.get("organism"),
        domains=[
            BudoDomain(
                name=d.get("name", ""),
                interpro_id=d.get("interpro_id"),
                start_position=d.get("start_position", 0),
                end_position=d.get("end_position", 0),
                confidence_score=d.get("confidence_score", 0.9),
            )
            for d in data.get("domains", [])
        ],
        cross_references=[
            BudoCrossReference(
                database=x.get("database", ""),
                identifier=x.get("identifier", ""),
                confidence_score=x.get("confidence_score", 0.85),
            )
            for x in data.get("cross_references", [])
        ],
        available=True,
        blockers=blockers,
        warnings=warnings,
    )


@router.get("/health", response_model=BudoHealthResponse)
async def budo_health() -> BudoHealthResponse:
    """Health check for BUDO service."""
    lmp_dir = Path(__file__).resolve().parents[4] / ".tmp_lmp_v4"
    lmp_count = len(list(lmp_dir.glob("*.xml"))) if lmp_dir.exists() else 0

    service = _get_neo4j_service()
    neo4j_ok = service is not None

    return BudoHealthResponse(
        ok=True,
        neo4j_available=neo4j_ok,
        neo4j_error=_neo4j_init_error,
        lmp_v4_xml_count=lmp_count,
    )
