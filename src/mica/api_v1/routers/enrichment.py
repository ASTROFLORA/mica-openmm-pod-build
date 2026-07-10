"""enrichment.py — Granular per-API enrichment endpoints.

L09B §18 — cable 6. Productises the HTTP clients already living inside
`bsm/lmp/generator_v4.py` (QuickGO, KEGG, Reactome, Ensembl, UniProt,
ProteinAtlas, AlphaFold, STRING, OpenTargets, ChEMBL, HPO, GTEx) by
re-exposing them as thin, read-only endpoints the frontend can call
directly without triggering a full LMP generation pipeline.

Design constraints (KB_SUBSTRATE_OPERATOR):
    * Soft-fail: every endpoint returns `{error: str, data: null}` on
      upstream failure, never a 5xx.
    * Never re-implement clients. Always delegate to the upstream REST
      API with a single `httpx.AsyncClient`.
    * Every endpoint requires an authenticated MICA user.
    * No caching here; the generator owns the cache (Phase 2).

Endpoints
---------
    GET  /api/v1/enrichment/go?uniprot=<acc>&tax=<id>
    GET  /api/v1/enrichment/kegg/find?db=<pathway|genes|compound>&q=<str>
    GET  /api/v1/enrichment/kegg/pathway/{pathway_id}
    GET  /api/v1/enrichment/kegg/gene/{org}/{gene}
    GET  /api/v1/enrichment/reactome/lookup?uniprot=<acc>
    GET  /api/v1/enrichment/ensembl/gene?symbol=<name>&species=<str>
    GET  /api/v1/enrichment/uniprot/{accession}
    GET  /api/v1/enrichment/alphafold/{accession}
    GET  /api/v1/enrichment/string/interactions?identifier=<str>&species=<id>
    GET  /api/v1/enrichment/opentargets/target?symbol=<name>
    GET  /api/v1/enrichment/chembl/target?uniprot=<acc>
    GET  /api/v1/enrichment/hpo?gene=<symbol>
    GET  /api/v1/enrichment/gtex/expression?gene=<symbol>
    GET  /api/v1/enrichment/protein-atlas?gene=<symbol>

Author: KB_SUBSTRATE_OPERATOR (L09B B-LIB-01 · 2026-04-20)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/enrichment", tags=["enrichment"])


# ── Config ──────────────────────────────────────────────────────────────────

_HTTP_TIMEOUT = float(os.getenv("MICA_ENRICH_HTTP_TIMEOUT", "8.0"))

UNIPROT_API = "https://rest.uniprot.org/uniprotkb"
PDB_API = "https://data.rcsb.org/rest/v1/core/entry"
KEGG_API_BASE = "https://rest.kegg.jp"
REACTOME_API_BASE = "https://reactome.org/ContentService"
ENSEMBL_API_BASE = "https://rest.ensembl.org"
QUICKGO_API = "https://www.ebi.ac.uk/QuickGO/services"
ALPHAFOLD_API = "https://alphafold.ebi.ac.uk/api/prediction"
STRING_API_BASE = "https://version-12-0.string-db.org/api"
OPENTARGETS_API_BASE = "https://api.platform.opentargets.org/api/v4/graphql"
CHEMBL_API_BASE = "https://www.ebi.ac.uk/chembl/api/data"
HPO_API_BASE = "https://ontology.jax.org/api/hp"
GTEX_API_BASE = "https://gtexportal.org/api/v2"
PROTEIN_ATLAS_API_BASE = "https://www.proteinatlas.org"


# ── Shared response envelope ────────────────────────────────────────────────

class EnrichmentResponse(BaseModel):
    source: str
    ok: bool
    data: Any | None = None
    error: Optional[str] = None


def _ok(source: str, data: Any) -> EnrichmentResponse:
    return EnrichmentResponse(source=source, ok=True, data=data)


def _err(source: str, msg: str) -> EnrichmentResponse:
    return EnrichmentResponse(source=source, ok=False, data=None, error=msg[:400])


async def _safe_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    r = await client.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


async def _safe_get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    r = await client.get(url, params=params, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


# ── Gene Ontology (QuickGO) ─────────────────────────────────────────────────

@router.get("/go", response_model=EnrichmentResponse, summary="QuickGO annotations for a UniProt accession")
async def get_go_annotations(
    uniprot: str = Query(..., description="UniProt accession (e.g. Q9H4B7)"),
    tax: str = Query("9606", description="NCBI taxon id (default: 9606 = human)"),
    limit: int = Query(100, ge=1, le=500),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{QUICKGO_API}/annotation/search"
    params = {"geneProductId": uniprot, "taxonId": tax, "limit": limit}
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params, headers=headers)
        return _ok("quickgo", data.get("results", []))
    except Exception as e:
        logger.warning("QuickGO fetch failed for %s: %s", uniprot, e)
        return _err("quickgo", str(e))


# ── KEGG ─────────────────────────────────────────────────────────────────────

@router.get("/kegg/find", response_model=EnrichmentResponse, summary="KEGG REST /find passthrough")
async def kegg_find(
    db: Literal["pathway", "genes", "compound", "drug", "disease", "enzyme"] = Query("pathway"),
    q: str = Query(..., min_length=1, max_length=120),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{KEGG_API_BASE}/find/{db}/{quote(q)}"
    try:
        async with httpx.AsyncClient() as client:
            text = await _safe_get_text(client, url)
        rows: List[Dict[str, str]] = []
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            pid, _, desc = line.partition("\t")
            rows.append({"id": pid, "description": desc})
        return _ok("kegg", rows)
    except Exception as e:
        logger.warning("KEGG find failed: %s", e)
        return _err("kegg", str(e))


@router.get("/kegg/pathway/{pathway_id}", response_model=EnrichmentResponse, summary="KEGG pathway details")
async def kegg_pathway(
    pathway_id: str,
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{KEGG_API_BASE}/get/{quote(pathway_id)}"
    try:
        async with httpx.AsyncClient() as client:
            text = await _safe_get_text(client, url)
        return _ok("kegg", {"raw": text, "pathway_id": pathway_id})
    except Exception as e:
        return _err("kegg", str(e))


@router.get("/kegg/gene/{organism}/{gene}", response_model=EnrichmentResponse, summary="KEGG gene entry")
async def kegg_gene(
    organism: str,
    gene: str,
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{KEGG_API_BASE}/get/{quote(organism)}:{quote(gene)}"
    try:
        async with httpx.AsyncClient() as client:
            text = await _safe_get_text(client, url)
        return _ok("kegg", {"raw": text, "organism": organism, "gene": gene})
    except Exception as e:
        return _err("kegg", str(e))


# ── Reactome ─────────────────────────────────────────────────────────────────

@router.get("/reactome/lookup", response_model=EnrichmentResponse, summary="Reactome pathway lookup by UniProt")
async def reactome_lookup(
    uniprot: str = Query(..., description="UniProt accession"),
    species: str = Query("9606"),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{REACTOME_API_BASE}/data/mapping/UniProt/{quote(uniprot)}/pathways"
    params = {"species": species}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params)
        return _ok("reactome", data)
    except Exception as e:
        return _err("reactome", str(e))


# ── Ensembl ──────────────────────────────────────────────────────────────────

@router.get("/ensembl/gene", response_model=EnrichmentResponse, summary="Ensembl gene lookup by symbol")
async def ensembl_gene(
    symbol: str = Query(..., min_length=1),
    species: str = Query("homo_sapiens"),
    expand: int = Query(1, ge=0, le=1),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{ENSEMBL_API_BASE}/lookup/symbol/{quote(species)}/{quote(symbol)}"
    headers = {"Content-Type": "application/json"}
    params = {"expand": expand}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params, headers=headers)
        return _ok("ensembl", data)
    except Exception as e:
        return _err("ensembl", str(e))


# ── UniProt ──────────────────────────────────────────────────────────────────

@router.get("/uniprot/{accession}", response_model=EnrichmentResponse, summary="UniProt entry JSON")
async def uniprot_entry(
    accession: str,
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{UNIPROT_API}/{quote(accession)}"
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, headers=headers)
        return _ok("uniprot", data)
    except Exception as e:
        return _err("uniprot", str(e))


# ── AlphaFold DB ─────────────────────────────────────────────────────────────

@router.get("/alphafold/{accession}", response_model=EnrichmentResponse, summary="AlphaFold DB prediction metadata")
async def alphafold_prediction(
    accession: str,
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{ALPHAFOLD_API}/{quote(accession)}"
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url)
        return _ok("alphafold", data)
    except Exception as e:
        return _err("alphafold", str(e))


# ── STRING-DB ────────────────────────────────────────────────────────────────

@router.get("/string/interactions", response_model=EnrichmentResponse, summary="STRING protein interaction partners")
async def string_interactions(
    identifier: str = Query(..., description="Protein symbol, UniProt or STRING id"),
    species: int = Query(9606),
    limit: int = Query(20, ge=1, le=500),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{STRING_API_BASE}/json/interaction_partners"
    params = {
        "identifiers": identifier,
        "species": species,
        "limit": limit,
        "caller_identity": "mica-enrichment",
    }
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params)
        return _ok("string", data)
    except Exception as e:
        return _err("string", str(e))


# ── OpenTargets ─────────────────────────────────────────────────────────────

_OT_QUERY = """
query targetBySymbol($sym: String!) {
  search(queryString: $sym, entityNames: ["target"]) {
    hits { id name entity object { __typename } }
  }
}
"""


@router.get("/opentargets/target", response_model=EnrichmentResponse, summary="OpenTargets target search by symbol")
async def opentargets_target(
    symbol: str = Query(..., min_length=1),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    payload = {"query": _OT_QUERY, "variables": {"sym": symbol}}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(OPENTARGETS_API_BASE, json=payload, timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        return _ok("opentargets", data.get("data", {}).get("search", {}).get("hits", []))
    except Exception as e:
        return _err("opentargets", str(e))


# ── ChEMBL ──────────────────────────────────────────────────────────────────

@router.get("/chembl/target", response_model=EnrichmentResponse, summary="ChEMBL target lookup by UniProt")
async def chembl_target(
    uniprot: str = Query(..., min_length=1),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{CHEMBL_API_BASE}/target.json"
    params = {"target_components__accession": uniprot}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params)
        return _ok("chembl", data.get("targets", []))
    except Exception as e:
        return _err("chembl", str(e))


# ── HPO (Jackson Lab) ───────────────────────────────────────────────────────

@router.get("/hpo", response_model=EnrichmentResponse, summary="HPO annotations for a gene symbol")
async def hpo_gene(
    gene: str = Query(..., min_length=1),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{HPO_API_BASE}/search/by-gene"
    params = {"q": gene, "max": 50}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params)
        return _ok("hpo", data)
    except Exception as e:
        return _err("hpo", str(e))


# ── GTEx ────────────────────────────────────────────────────────────────────

@router.get("/gtex/expression", response_model=EnrichmentResponse, summary="GTEx median tissue expression")
async def gtex_expression(
    gene: str = Query(..., min_length=1),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    url = f"{GTEX_API_BASE}/expression/medianGeneExpression"
    params = {"geneSymbol": gene, "format": "json"}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params)
        return _ok("gtex", data)
    except Exception as e:
        return _err("gtex", str(e))


# ── Human Protein Atlas ─────────────────────────────────────────────────────

@router.get("/protein-atlas", response_model=EnrichmentResponse, summary="HPA search by gene symbol")
async def protein_atlas(
    gene: str = Query(..., min_length=1),
    _user=Depends(user_dependency),
) -> EnrichmentResponse:
    # HPA has a simple /search?search=<gene>&format=json endpoint.
    url = f"{PROTEIN_ATLAS_API_BASE}/search/{quote(gene)}"
    params = {"format": "json", "columns": "g,gs,eg,rnatsm,t_RNA_tissue_enriched"}
    try:
        async with httpx.AsyncClient() as client:
            data = await _safe_get_json(client, url, params=params)
        return _ok("protein_atlas", data)
    except Exception as e:
        return _err("protein_atlas", str(e))


# ── Composite: enrichment bundle for a UniProt accession ────────────────────

@router.get("/bundle/{accession}", response_model=Dict[str, EnrichmentResponse], summary="Fan-out enrichment for one accession")
async def enrichment_bundle(
    accession: str,
    tax: str = Query("9606"),
    _user=Depends(user_dependency),
) -> Dict[str, EnrichmentResponse]:
    """Fetch GO, AlphaFold, Reactome, ChEMBL, STRING in parallel — soft-fail per source."""

    async def _safe(coro, source: str) -> EnrichmentResponse:
        try:
            return await coro
        except Exception as e:
            return _err(source, str(e))

    async with httpx.AsyncClient() as client:
        async def _go() -> EnrichmentResponse:
            url = f"{QUICKGO_API}/annotation/search"
            params = {"geneProductId": accession, "taxonId": tax, "limit": 100}
            data = await _safe_get_json(client, url, params=params, headers={"Accept": "application/json"})
            return _ok("quickgo", data.get("results", []))

        async def _af() -> EnrichmentResponse:
            data = await _safe_get_json(client, f"{ALPHAFOLD_API}/{quote(accession)}")
            return _ok("alphafold", data)

        async def _re() -> EnrichmentResponse:
            data = await _safe_get_json(
                client,
                f"{REACTOME_API_BASE}/data/mapping/UniProt/{quote(accession)}/pathways",
                params={"species": tax},
            )
            return _ok("reactome", data)

        async def _ch() -> EnrichmentResponse:
            data = await _safe_get_json(
                client,
                f"{CHEMBL_API_BASE}/target.json",
                params={"target_components__accession": accession},
            )
            return _ok("chembl", data.get("targets", []))

        async def _st() -> EnrichmentResponse:
            data = await _safe_get_json(
                client,
                f"{STRING_API_BASE}/json/interaction_partners",
                params={"identifiers": accession, "species": int(tax), "limit": 20,
                        "caller_identity": "mica-enrichment"},
            )
            return _ok("string", data)

        go_r, af_r, re_r, ch_r, st_r = await asyncio.gather(
            _safe(_go(), "quickgo"),
            _safe(_af(), "alphafold"),
            _safe(_re(), "reactome"),
            _safe(_ch(), "chembl"),
            _safe(_st(), "string"),
        )

    return {
        "go": go_r,
        "alphafold": af_r,
        "reactome": re_r,
        "chembl": ch_r,
        "string": st_r,
    }


@router.get("/health", summary="Enrichment router health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "sources": [
            "quickgo", "kegg", "reactome", "ensembl", "uniprot", "alphafold",
            "string", "opentargets", "chembl", "hpo", "gtex", "protein_atlas",
        ],
        "timeout_s": _HTTP_TIMEOUT,
    }
