"""library_models.py — Shared Pydantic models for the Library router and facade.

Extracted from library.py to break circular imports between the router and
LibrarySearchFacade. Both modules import from here.

Part of ALEJANDRIA_LIBRARY_LITERATURE_CONSOLIDATION_REWIRE_AUDIT_V1.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class LibraryHit(BaseModel):
    source: Literal["literature", "alphafold", "pdb", "kegg", "uniprot", "molecule", "budo"]
    id: str
    title: str
    subtitle: Optional[str] = None
    score: Optional[float] = None

    authors: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[int] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None

    accession: Optional[str] = None
    organism: Optional[str] = None
    length: Optional[int] = None

    pdb_id: Optional[str] = None
    method: Optional[str] = None
    resolution: Optional[float] = None

    kegg_id: Optional[str] = None
    pathway_map: Optional[str] = None

    cid: Optional[int] = None
    chembl_id: Optional[str] = None
    smiles: Optional[str] = None
    molecular_formula: Optional[str] = None
    molecular_weight: Optional[float] = None

    raw: Optional[Dict[str, Any]] = None


class LibrarySearchResponse(BaseModel):
    query: str
    tab: str
    total: int
    hits: List[LibraryHit]
    errors: Dict[str, str] = Field(default_factory=dict)
    latency_ms: int = 0
