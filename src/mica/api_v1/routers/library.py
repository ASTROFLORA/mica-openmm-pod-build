"""library.py — Alejandría Library and Workspace Explorer search router.

This router is the backend contract authority for scientific discovery in the
frontend. Both the Alejandría Library page and the shared Workspace Explorer
consume `GET /api/v1/library/search`, using explicit tabs instead of each UI
surface talking to third-party providers on its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.api_v1.routers.library_models import LibraryHit, LibrarySearchResponse
from mica.api_v1.services.library_search_facade import (
    DEGRADED_LEGACY_FALLBACK,
    LEGACY_CLASSIFICATION_MARKER,
    LibrarySearchFacade,
    get_library_search_facade,
)
from mica.storage import lmp_v4_public_scanner as _lmp_v4_scanner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/library", tags=["library"])


_HTTP_TIMEOUT = float(os.getenv("MICA_LIBRARY_HTTP_TIMEOUT", "8.0"))
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50
_NS = "http://ai-university.edu/lmp/v4.0"
_UNIPROT_ACCESSION_RE = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}$",
    re.I,
)


def _lmp_v4_dir() -> Path:
    override = os.getenv("MICA_LMP_V4_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).resolve().parents[4] / ".tmp_lmp_v4").resolve()


# LibraryHit and LibrarySearchResponse are now in library_models.py
# (extracted to break circular imports with LibrarySearchFacade)
# Re-exported here for backward compatibility:
__all__ = ["LibraryHit", "LibrarySearchResponse"]


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    number = _to_float(value)
    return int(number) if number is not None else None


# LEGACY_DIRECT_PROVIDER_FALLBACK: This function calls Semantic Scholar directly.
# It is preserved for degraded operation when Literature Consolidation
# infrastructure is unavailable. See library_provider_fallback_policy_v1.json.
async def _search_literature(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": q,
        "limit": str(min(limit, 50)),
        "fields": "title,abstract,authors.name,venue,year,externalIds",
    }
    response = await client.get(url, params=params, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json() or {}

    hits: List[LibraryHit] = []
    for paper in (data.get("data") or []):
        authors = ", ".join((author.get("name") or "") for author in (paper.get("authors") or [])[:4])
        external_ids = paper.get("externalIds") or {}
        hits.append(
            LibraryHit(
                source="literature",
                id=f"s2:{paper.get('paperId') or ''}",
                title=paper.get("title") or "(untitled)",
                subtitle=paper.get("venue"),
                authors=authors or None,
                journal=paper.get("venue"),
                year=paper.get("year"),
                abstract=paper.get("abstract"),
                doi=external_ids.get("DOI"),
            )
        )
    return hits


def _lmp_hit_from_file(path: Path, parsed: Optional[ET.Element] = None) -> LibraryHit:
    accession = path.stem.split("_")[0]
    title = accession
    organism: Optional[str] = None
    length: Optional[int] = None
    try:
        root_el = parsed or ET.parse(path).getroot()
        ns = {"l": _NS}
        sem = root_el.find("l:Semantics", ns)
        ident = root_el.find("l:Identity", ns)
        if sem is not None:
            protein_name = sem.findtext("l:ProteinName", default="", namespaces=ns)
            if protein_name:
                title = protein_name
        if ident is not None:
            organism_el = ident.find("l:Organism", ns)
            if organism_el is not None:
                organism = organism_el.get("name") or organism_el.get("scientific_name")
        geometry = root_el.find("l:Geometry", ns)
        if geometry is not None:
            sequence_el = geometry.find("l:Sequence", ns)
            if sequence_el is not None and sequence_el.text:
                length = len(sequence_el.text.strip())
    except Exception:  # noqa: BLE001
        pass
    return LibraryHit(
        source="alphafold",
        id=f"af:{accession}",
        title=title,
        subtitle=organism,
        accession=accession,
        organism=organism,
        length=length,
        raw={
            "entryId": accession,
            "gene": title,
            "uniprotAccession": accession,
            "uniprotId": accession,
            "uniprotDescription": title,
            "organismScientificName": organism or "",
        },
    )


def _search_lmp_local(q: str, limit: int) -> List[LibraryHit]:
    root = _lmp_v4_dir()
    if not root.exists():
        return []
    q_lc = q.lower()
    token = re.sub(r"[^a-zA-Z0-9]+", "", q).upper()
    xml_files = list(root.glob("*.xml"))
    if len(xml_files) > 500:
        xml_files = xml_files[:500]

    hits: List[LibraryHit] = []
    for path in xml_files:
        if token and token in path.stem.upper():
            hits.append(_lmp_hit_from_file(path))
            if len(hits) >= limit:
                return hits

    if len(hits) >= limit:
        return hits[:limit]

    for path in xml_files:
        if len(hits) >= limit:
            break
        try:
            root_el = ET.parse(path).getroot()
            ns = {"l": _NS}
            semantics = root_el.find("l:Semantics", ns)
            if semantics is None:
                continue
            protein_name = semantics.findtext("l:ProteinName", default="", namespaces=ns) or ""
            gene_el = semantics.find(".//l:Gene", ns)
            gene = gene_el.get("name", "") if gene_el is not None else ""
            blob = f"{protein_name} {gene}".lower()
            if q_lc in blob:
                hit = _lmp_hit_from_file(path, parsed=root_el)
                if not any(existing.id == hit.id for existing in hits):
                    hits.append(hit)
        except ET.ParseError:
            continue
        except Exception:  # noqa: BLE001
            continue
    return hits[:limit]


async def _search_pdb(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    body = {
        "query": {
            "type": "terminal",
            "service": "full_text",
            "parameters": {"value": q},
        },
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": min(limit, 25)}},
    }
    response = await client.post("https://search.rcsb.org/rcsbsearch/v2/query", json=body, timeout=_HTTP_TIMEOUT)
    if response.status_code == 204:
        return []
    response.raise_for_status()
    payload = response.json() or {}
    ids = [item.get("identifier") for item in (payload.get("result_set") or [])]
    ids = [pdb_id for pdb_id in ids if pdb_id]
    if not ids:
        return []

    async def _hydrate(pdb_id: str) -> Optional[LibraryHit]:
        try:
            summary = await client.get(f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id}", timeout=_HTTP_TIMEOUT)
            summary.raise_for_status()
            body = summary.json() or {}
            struct = body.get("struct") or {}
            exp = (body.get("exptl") or [{}])[0]
            refine = (body.get("refine") or [{}])[0]
            resolution = refine.get("ls_d_res_high")
            return LibraryHit(
                source="pdb",
                id=f"pdb:{pdb_id}",
                title=struct.get("title") or pdb_id,
                subtitle=exp.get("method"),
                pdb_id=pdb_id,
                method=exp.get("method"),
                resolution=float(resolution) if resolution is not None else None,
            )
        except Exception:  # noqa: BLE001
            return None

    hydrated = await asyncio.gather(*[_hydrate(pdb_id) for pdb_id in ids[:limit]])
    hits = [hit for hit in hydrated if hit is not None]
    if hits:
        return hits
    return [LibraryHit(source="pdb", id=f"pdb:{pdb_id}", title=pdb_id, pdb_id=pdb_id) for pdb_id in ids[:limit]]


async def _search_kegg(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    response = await client.get(f"https://rest.kegg.jp/find/pathway/{quote(q)}", timeout=_HTTP_TIMEOUT)
    if response.status_code != 200 or not response.text.strip():
        return []
    hits: List[LibraryHit] = []
    for line in response.text.splitlines()[:limit]:
        if "\t" not in line:
            continue
        path_id, title = line.split("\t", 1)
        pathway_id = path_id.replace("path:", "")
        hits.append(
            LibraryHit(
                source="kegg",
                id=f"kegg:{pathway_id}",
                title=title.strip(),
                subtitle=pathway_id,
                kegg_id=pathway_id,
                pathway_map=pathway_id,
            )
        )
    return hits


async def _search_uniprot(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    params = {
        "query": q,
        "format": "json",
        "size": str(min(limit, 25)),
        "fields": "accession,id,protein_name,organism_name,length,gene_names",
    }
    response = await client.get("https://rest.uniprot.org/uniprotkb/search", params=params, timeout=_HTTP_TIMEOUT)
    if response.status_code == 204:
        return []
    response.raise_for_status()
    payload = response.json() or {}
    hits: List[LibraryHit] = []
    for record in (payload.get("results") or [])[:limit]:
        protein_desc = record.get("proteinDescription") or {}
        recommended = (protein_desc.get("recommendedName") or {}).get("fullName") or {}
        submitted = ((protein_desc.get("submittedName") or [{}])[0].get("fullName") or {})
        gene = None
        genes = record.get("genes") or []
        if genes:
            gene = ((genes[0].get("geneName") or {}).get("value"))
        accession = record.get("primaryAccession")
        title = recommended.get("value") or submitted.get("value") or accession or "(unnamed protein)"
        hits.append(
            LibraryHit(
                source="uniprot",
                id=f"uniprot:{accession or title}",
                title=title,
                subtitle=gene or record.get("uniProtkbId"),
                accession=accession,
                organism=(record.get("organism") or {}).get("scientificName"),
                length=(record.get("sequence") or {}).get("length"),
                raw={
                    "gene": gene,
                    "uniprotId": record.get("uniProtkbId"),
                },
            )
        )
    return hits


async def _search_pubchem_molecules(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    sdq_query = {
        "select": "*",
        "collection": "compound",
        "where": {"ands": [{"*": q}]},
        "order": ["relevancescore,desc"],
        "start": 1,
        "limit": limit,
    }
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/sdq/sdqagent.cgi"
        f"?infmt=json&outfmt=json&query={quote(json.dumps(sdq_query, separators=(',', ':')))}"
    )
    response = await client.get(url, timeout=_HTTP_TIMEOUT)
    if response.status_code == 204:
        return []
    response.raise_for_status()
    payload = response.json() or {}
    containers = payload.get("SDQOutputSet") or payload.get("SDQOutputContainer") or []
    rows = (containers[0] or {}).get("rows") or []

    cids: List[int] = []
    names: Dict[int, str] = {}
    for row in rows[:limit]:
        cid = row.get("CID") or row.get("cid")
        if cid is None:
            continue
        cid_int = int(cid)
        cids.append(cid_int)
        names[cid_int] = row.get("cmpdname") or row.get("name") or row.get("IUPACName") or f"CID {cid_int}"
    if not cids:
        return []

    prop_url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
        f"{','.join(str(cid) for cid in cids)}/property/"
        "IsomericSMILES,CanonicalSMILES,MolecularFormula,IUPACName,MolecularWeight,XLogP,HBondAcceptorCount,HBondDonorCount,RotatableBondCount,TPSA/JSON"
    )
    prop_response = await client.get(prop_url, timeout=_HTTP_TIMEOUT)
    property_map: Dict[int, Dict[str, Any]] = {}
    if prop_response.is_success:
        prop_payload = prop_response.json() or {}
        for item in (prop_payload.get("PropertyTable") or {}).get("Properties") or []:
            cid = item.get("CID")
            if cid is not None:
                property_map[int(cid)] = item

    hits: List[LibraryHit] = []
    for idx, cid in enumerate(cids):
        props = property_map.get(cid, {})
        title = names.get(cid) or props.get("IUPACName") or f"CID {cid}"
        hits.append(
            LibraryHit(
                source="molecule",
                id=f"cid:{cid}",
                title=title,
                subtitle=props.get("MolecularFormula"),
                score=float(max(limit - idx, 1)),
                cid=cid,
                smiles=props.get("IsomericSMILES") or props.get("CanonicalSMILES"),
                molecular_formula=props.get("MolecularFormula"),
                molecular_weight=_to_float(props.get("MolecularWeight")),
                raw={
                    "source": "pubchem",
                    "xLogP": _to_float(props.get("XLogP")),
                    "hbondAcceptors": _to_int(props.get("HBondAcceptorCount")),
                    "hbondDonors": _to_int(props.get("HBondDonorCount")),
                    "rotatableBonds": _to_int(props.get("RotatableBondCount")),
                    "tpsa": _to_float(props.get("TPSA")),
                },
            )
        )
    return hits


async def _search_chembl_molecules(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    response = await client.get(
        f"https://www.ebi.ac.uk/chembl/api/data/molecule?q={quote(q)}&format=json&limit={limit}",
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code == 204:
        return []
    response.raise_for_status()
    payload = response.json() or {}

    hits: List[LibraryHit] = []
    for idx, molecule in enumerate((payload.get("molecules") or [])[:limit]):
        props = molecule.get("molecule_properties") or {}
        structures = molecule.get("molecule_structures") or {}
        chembl_id = molecule.get("molecule_chembl_id")
        title = molecule.get("pref_name") or chembl_id or "(unnamed molecule)"
        hits.append(
            LibraryHit(
                source="molecule",
                id=f"chembl:{chembl_id or idx}",
                title=title,
                subtitle=props.get("full_molformula") or props.get("molecular_formula"),
                score=float(max(limit - idx, 1)),
                chembl_id=chembl_id,
                smiles=structures.get("canonical_smiles"),
                molecular_formula=props.get("full_molformula") or props.get("molecular_formula"),
                molecular_weight=_to_float(props.get("full_mwt")),
                raw={
                    "source": "chembl",
                    "inchiKey": structures.get("standard_inchi_key"),
                    "moleculeType": molecule.get("molecule_type"),
                    "structureType": molecule.get("structure_type"),
                    "xLogP": _to_float(props.get("alogp")),
                    "hbondAcceptors": _to_int(props.get("hba")),
                    "hbondDonors": _to_int(props.get("hbd")),
                    "rotatableBonds": _to_int(props.get("rtb")),
                    "tpsa": _to_float(props.get("psa")),
                    "maxPhase": _to_int(molecule.get("max_phase")),
                    "firstApproval": _to_int(molecule.get("first_approval")),
                },
            )
        )
    return hits


async def _search_molecule(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    pubchem_hits, chembl_hits = await asyncio.gather(
        _search_pubchem_molecules(client, q, limit),
        _search_chembl_molecules(client, q, max(1, limit // 2)),
        return_exceptions=True,
    )
    merged: List[LibraryHit] = []
    seen: set[str] = set()
    for bucket in (pubchem_hits, chembl_hits):
        if isinstance(bucket, Exception):
            continue
        for hit in bucket:
            key = hit.chembl_id or (f"cid:{hit.cid}" if hit.cid is not None else re.sub(r"\W+", "", hit.title.lower()))
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= limit:
                return merged
    return merged


async def _fetch_alphafold_accession(client: httpx.AsyncClient, accession: str) -> List[LibraryHit]:
    response = await client.get(f"https://alphafold.ebi.ac.uk/api/prediction/{quote(accession)}", timeout=_HTTP_TIMEOUT)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json() or []
    rows = payload if isinstance(payload, list) else [payload]
    hits: List[LibraryHit] = []
    for row in rows[:1]:
        entry_id = row.get("entryId") or accession
        accession_id = row.get("uniprotAccession") or accession
        title = row.get("gene") or row.get("uniprotId") or accession_id
        organism = row.get("organismScientificName")
        hits.append(
            LibraryHit(
                source="alphafold",
                id=f"af:{entry_id}",
                title=title,
                subtitle=organism,
                accession=accession_id,
                organism=organism,
                length=_to_int(row.get("sequenceLength")),
                raw={
                    "entryId": entry_id,
                    "gene": row.get("gene") or "",
                    "uniprotAccession": accession_id,
                    "uniprotId": row.get("uniprotId") or accession_id,
                    "uniprotDescription": row.get("uniprotDescription") or title,
                    "taxId": row.get("taxId") or 0,
                    "organismScientificName": organism or "",
                    "modelCreatedDate": row.get("modelCreatedDate") or "",
                    "latestVersion": row.get("latestVersion") or 0,
                    "pdbUrl": row.get("pdbUrl") or "",
                    "cifUrl": row.get("cifUrl") or "",
                    "paeImageUrl": row.get("paeImageUrl") or "",
                    "globalMetricValue": row.get("globalMetricValue") or 0,
                },
            )
        )
    return hits


async def _search_alphafold_remote(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    trimmed = q.strip()
    accessions: List[str]
    if _UNIPROT_ACCESSION_RE.match(trimmed):
        accessions = [trimmed.upper()]
    else:
        uni_hits = await _search_uniprot(client, trimmed, min(limit, 8))
        accessions = [hit.accession for hit in uni_hits if hit.accession][: min(limit, 8)]
    if not accessions:
        return []

    gathered = await asyncio.gather(
        *[_fetch_alphafold_accession(client, accession) for accession in accessions],
        return_exceptions=True,
    )
    hits: List[LibraryHit] = []
    seen: set[str] = set()
    for bucket in gathered:
        if isinstance(bucket, Exception):
            continue
        for hit in bucket:
            key = hit.accession or hit.id
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


async def _search_alphafold(client: httpx.AsyncClient, q: str, limit: int) -> List[LibraryHit]:
    remote_hits = await _search_alphafold_remote(client, q, limit)
    if remote_hits:
        return remote_hits[:limit]
    return await asyncio.to_thread(_search_lmp_local, q, limit)


@router.get("/search", response_model=LibrarySearchResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=256, description="Free-text query"),
    tab: Literal["all", "literature", "alphafold", "pdb", "kegg", "uniprot", "molecule"] = Query("all"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    _user_id: str = Depends(user_dependency),
) -> LibrarySearchResponse:
    errors: Dict[str, str] = {}
    started = time.perf_counter()

    # ── Literature tab: try consolidated path first ──────────────────────
    # See library_to_literature_consolidation_adapter_plan_v1.md
    facade = get_library_search_facade()

    async with httpx.AsyncClient(headers={"User-Agent": "mica-library/1.0"}) as client:

        async def _lit() -> List[LibraryHit]:
            if tab not in ("all", "literature"):
                return []
            # Try consolidated path first (LiteratureSearchService → ProviderQuorumService)
            try:
                consolidated_hits, consolidated_errors = await facade.search_literature_consolidated(q, limit)
                # Merge any consolidation errors into the response errors map
                for key, msg in consolidated_errors.items():
                    errors[key] = msg
                if consolidated_hits:
                    return consolidated_hits
                # If consolidation returned empty but no blocker, fall through to legacy
                if consolidated_errors and any(
                    DEGRADED_LEGACY_FALLBACK in v or "unwired" in v
                    for v in consolidated_errors.values()
                ):
                    pass  # Will fall through to legacy below
                elif consolidated_hits:
                    return consolidated_hits
            except Exception as exc:  # noqa: BLE001
                errors["literature_consolidation"] = f"consolidation_error: {str(exc)[:200]}"

            # LEGACY_DIRECT_PROVIDER_FALLBACK: direct Semantic Scholar call
            # Used when Literature Consolidation infra is unavailable.
            try:
                legacy_hits = await _search_literature(client, q, limit)
                # Mark every legacy hit with classification
                for hit in legacy_hits:
                    if hit.raw is None:
                        hit.raw = {}
                    hit.raw["provider_status"] = DEGRADED_LEGACY_FALLBACK
                return legacy_hits
            except Exception as exc:  # noqa: BLE001
                errors["literature"] = str(exc)[:200]
                return []

        async def _af() -> List[LibraryHit]:
            if tab not in ("all", "alphafold"):
                return []
            try:
                return await _search_alphafold(client, q, limit)
            except Exception as exc:  # noqa: BLE001
                errors["alphafold"] = str(exc)[:200]
                return []

        async def _pdb_branch() -> List[LibraryHit]:
            if tab not in ("all", "pdb"):
                return []
            try:
                return await _search_pdb(client, q, limit)
            except Exception as exc:  # noqa: BLE001
                errors["pdb"] = str(exc)[:200]
                return []

        async def _kegg_branch() -> List[LibraryHit]:
            if tab not in ("all", "kegg"):
                return []
            try:
                return await _search_kegg(client, q, limit)
            except Exception as exc:  # noqa: BLE001
                errors["kegg"] = str(exc)[:200]
                return []

        async def _uniprot_branch() -> List[LibraryHit]:
            if tab != "uniprot":
                return []
            try:
                return await _search_uniprot(client, q, limit)
            except Exception as exc:  # noqa: BLE001
                errors["uniprot"] = str(exc)[:200]
                return []

        async def _molecule_branch() -> List[LibraryHit]:
            if tab != "molecule":
                return []
            try:
                return await _search_molecule(client, q, limit)
            except Exception as exc:  # noqa: BLE001
                errors["molecule"] = str(exc)[:200]
                return []

        lit_hits, alphafold_hits, pdb_hits, kegg_hits, uniprot_hits, molecule_hits = await asyncio.gather(
            _lit(),
            _af(),
            _pdb_branch(),
            _kegg_branch(),
            _uniprot_branch(),
            _molecule_branch(),
        )

    if tab == "all":
        hits: List[LibraryHit] = []
        buckets = [lit_hits, alphafold_hits, pdb_hits, kegg_hits]
        idx = 0
        while any(bucket for bucket in buckets):
            bucket = buckets[idx % len(buckets)]
            if bucket:
                hits.append(bucket.pop(0))
            idx += 1
            if len(hits) >= limit * 2:
                break
    else:
        hits = lit_hits + alphafold_hits + pdb_hits + kegg_hits + uniprot_hits + molecule_hits

    return LibrarySearchResponse(
        query=q,
        tab=tab,
        total=len(hits),
        hits=hits,
        errors=errors,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


@router.get("/protein/{accession}")
async def protein_shortcut(
    accession: str,
    _user_id: str = Depends(user_dependency),
) -> Dict[str, Any]:
    acc = (accession or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{5,15}$", acc):
        raise HTTPException(status_code=400, detail="invalid accession format")
    return {
        "accession": acc,
        "lmp_url": f"/api/v1/lmp/annotations/{acc}",
        "af_url": f"https://alphafold.ebi.ac.uk/entry/{acc}",
    }


# ── Entity Mentions ────────────────────────────────────────────────────────

class EntityMention(BaseModel):
    source_document_id: str
    title: str
    matched_text_span: Optional[str] = None
    entity_candidate: Optional[str] = None
    confidence: float = 0.5
    extraction_method: str = "lmp_xml_text_match"
    claim_candidate: Optional[str] = None
    citation_ref: Optional[str] = None
    # ── V2 persistence fields ──
    context_window: Optional[str] = None
    evidence_ref: Optional[str] = None
    provider: Optional[str] = None
    created_at: Optional[str] = None
    status: Optional[str] = None


class EntityMentionsResponse(BaseModel):
    entity_id: str
    total: int
    mentions: List[EntityMention] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    # ── V2 fields ──
    query: Optional[str] = None
    detection_mode: Optional[str] = None


# ── Entity Claims ───────────────────────────────────────────────────────────

class LibraryClaim(BaseModel):
    claim_id: str
    claim_text: str
    claim_type: str = "unknown"
    entity_id: Optional[str] = None
    source_document_id: Optional[str] = None
    evidence_refs: List[str] = Field(default_factory=list)
    confidence: float = 0.5
    extraction_method: str = "none"
    method_tags: List[str] = Field(default_factory=list)
    supporting_papers: List[str] = Field(default_factory=list)
    contradicting_papers: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    status: Optional[str] = None


class LibraryClaimsResponse(BaseModel):
    entity_id: str
    total: int
    claims: List[LibraryClaim] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    extraction_status: str = "unimplemented"


# ── Fulltext Detect ─────────────────────────────────────────────────────────

class FulltextDetectRequest(BaseModel):
    query: Optional[str] = None
    entity_id: Optional[str] = None
    document_refs: List[str] = Field(default_factory=list)
    max_docs: int = Field(default=10, ge=1, le=50)
    detection_mode: Literal["persisted_only", "bounded_live", "fixture"] = "persisted_only"


class FulltextDetectResult(BaseModel):
    source_document_id: str
    title: Optional[str] = None
    matched_text_span: Optional[str] = None
    entity_candidate: Optional[str] = None
    confidence: float = 0.5
    extraction_method: str = "fulltext_scan"
    claim_candidate: Optional[str] = None
    evidence_ref: Optional[str] = None
    provider: Optional[str] = None


class FulltextDetectResponse(BaseModel):
    query: Optional[str] = None
    entity_id: Optional[str] = None
    detection_mode: str
    total: int
    detections: List[FulltextDetectResult] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    status: str = "unwired"


@router.get("/entities/{entity_id}/mentions", response_model=EntityMentionsResponse)
async def entity_mentions(
    entity_id: str,
    query: Optional[str] = Query(default=None, description="Optional filter query"),
    max_results: int = Query(default=20, ge=1, le=100),
) -> EntityMentionsResponse:
    """Return literature mentions for a protein entity.

    Searches LMP XML files and DLM cache for documents mentioning the entity.
    """
    import json as _json
    import xml.etree.ElementTree as _ET

    mentions: List[EntityMention] = []
    warnings: List[str] = []
    entity_upper = (entity_id or "").upper()

    # Tier 1: LMP XML full-text mention scan
    lmp_dir = _lmp_v4_dir()
    if lmp_dir.exists():
        for xml_file in sorted(lmp_dir.glob("*.xml"))[:100]:
            try:
                text = xml_file.read_text(encoding="utf-8", errors="replace")
                if entity_upper.lower() in text.lower():
                    # Extract protein name and title from XML
                    try:
                        root = _ET.fromstring(text)
                        ns = {"l": _NS}
                        sem = root.find("l:Semantics", ns)
                        protein_name = sem.findtext("l:ProteinName", default="", namespaces=ns) if sem is not None else ""
                        ident = root.find("l:Identity", ns)
                        accession = ident.findtext("l:PrimaryAccession", default="", namespaces=ns) if ident is not None else ""
                        title = protein_name or accession or xml_file.stem
                    except Exception:
                        title = xml_file.stem
                    mentions.append(EntityMention(
                        source_document_id=xml_file.stem,
                        title=title,
                        entity_candidate=entity_id,
                        confidence=0.85,
                        extraction_method="lmp_xml_entity_match",
                    ))
                    if len(mentions) >= max_results:
                        break
            except Exception:
                continue

    # Tier 2: DLM API cache scan
    if len(mentions) < max_results:
        dlm_cache_dir = Path(__file__).resolve().parents[4] / "src" / "mica" / "memory" / "dlm" / "api_cache"
        if dlm_cache_dir.exists():
            for cache_file in sorted(dlm_cache_dir.glob("*.json"))[:200]:
                try:
                    data = _json.loads(cache_file.read_text(encoding="utf-8", errors="replace"))
                    text_blob = _json.dumps(data).lower()
                    if entity_upper.lower() in text_blob:
                        title = data.get("title", "") or data.get("query", "") or cache_file.stem
                        mentions.append(EntityMention(
                            source_document_id=cache_file.stem,
                            title=title,
                            entity_candidate=entity_id,
                            confidence=0.7,
                            extraction_method="dlm_api_cache_text_match",
                        ))
                        if len(mentions) >= max_results:
                            break
                except Exception:
                    continue

    if not mentions:
        warnings.append("no_literature_mentions_found: no LMP XML or DLM cache documents reference this entity")

    # ── Also try consolidated entity mentions if facade is available ────
    facade = get_library_search_facade()
    consolidated_mentions, consolidated_warnings = await facade.entity_mentions_consolidated(
        entity_id, query, max(1, max_results - len(mentions))
    )
    if consolidated_warnings:
        warnings.extend(consolidated_warnings)
    if consolidated_mentions:
        for cm in consolidated_mentions:
            mentions.append(EntityMention(
                source_document_id=cm.get("source_document_id", ""),
                title=cm.get("title", ""),
                entity_candidate=entity_id,
                confidence=float(cm.get("confidence", 0.5)),
                extraction_method=cm.get("extraction_method", "consolidated_search"),
                provider=cm.get("provider"),
                status=cm.get("status"),
            ))
            if len(mentions) >= max_results:
                break

    return EntityMentionsResponse(
        entity_id=entity_id,
        total=len(mentions),
        mentions=mentions[:max_results],
        warnings=warnings,
        query=query,
        detection_mode="lmp_xml_cache_scan",
    )


# ── Entity Claims ───────────────────────────────────────────────────────────

@router.get("/entities/{entity_id}/claims", response_model=LibraryClaimsResponse)
async def entity_claims(
    entity_id: str,
    max_results: int = Query(default=20, ge=1, le=50),
) -> LibraryClaimsResponse:
    """Return extracted claims for a protein entity.

    Claims are sourced from FrontierClaimExtractor via SOTAPipeline.
    If claim extraction is not implemented or unavailable, returns
    typed status rather than fabricating claims.

    Part of LIBRARY_DLM_CLAIMS_PERSISTENCE_V1.
    """
    warnings: List[str] = []
    claims: List[LibraryClaim] = []

    # Probe claim extraction availability
    facade = get_library_search_facade()
    facade._probe_imports()

    extraction_available = False
    extraction_status = "claims_extraction_unimplemented"

    # Try FrontierClaimExtractor import
    try:
        from mica.sota_reports.frontier_claim_extractor import FrontierClaimExtractor  # noqa: F401
        from mica.sota_reports.contracts import SOTAFrontierClaim
        extraction_available = True
    except ImportError:
        warnings.append(
            "claims_extraction_unimplemented: FrontierClaimExtractor not importable. "
            "Claims are sourced from SOTAPipeline/FrontierClaimExtractor which require "
            "the full DLM infrastructure. See LIBRARY_DLM_CLAIMS_PERSISTENCE_V1."
        )

    if not extraction_available:
        return LibraryClaimsResponse(
            entity_id=entity_id,
            total=0,
            claims=[],
            warnings=warnings,
            extraction_status="claims_extraction_unimplemented",
        )

    # Claim extraction is importable but we have no persistent claim store queried yet.
    # The SOTAPipeline extracts claims from paper batches — querying by entity_id
    # requires a claim→entity mapping index that does not yet exist in the Library.
    warnings.append(
        "claims_extraction_unimplemented: FrontierClaimExtractor is importable "
        "but no entity→claim index exists for per-entity claim lookup. "
        "Bulk claim extraction exists via SOTAPipeline for paper batches. "
        "Per-entity claim query requires a persistence layer not yet wired."
    )

    return LibraryClaimsResponse(
        entity_id=entity_id,
        total=0,
        claims=[],
        warnings=warnings,
        extraction_status="claims_extraction_unimplemented",
    )


# ── Fulltext Detect ─────────────────────────────────────────────────────────

@router.post("/fulltext/detect", response_model=FulltextDetectResponse)
async def fulltext_detect(
    body: FulltextDetectRequest,
) -> FulltextDetectResponse:
    """Detect full-text availability for documents/entities.

    Modes:
    - persisted_only: Check only previously persisted evidence (default, safe).
    - bounded_live: Run bounded live detection via FullTextRouter (explicit opt-in).
    - fixture: Return fixture data for testing.

    Part of LIBRARY_DLM_CLAIMS_PERSISTENCE_V1.
    """
    warnings: List[str] = []
    detections: List[FulltextDetectResult] = []

    facade = get_library_search_facade()
    facade._probe_imports()

    if not facade._fulltext_available:
        return FulltextDetectResponse(
            query=body.query,
            entity_id=body.entity_id,
            detection_mode=body.detection_mode,
            total=0,
            detections=[],
            warnings=["fulltext_detect_unwired: FullTextRouter not available"],
            status="unwired",
        )

    if body.detection_mode == "bounded_live":
        # bounded_live requires explicit FullTextRouter invocation.
        # The FullTextRouter is importable but the detect endpoint does not
        # yet call acquire_batch() — that requires paper IDs or DOIs as input.
        warnings.append(
            "fulltext_detect_bounded_live_not_wired: FullTextRouter is importable "
            "but bounded live detection requires paper IDs or DOIs to call "
            "acquire_batch(). Provide document_refs with valid paper IDs/DOIs."
        )
        return FulltextDetectResponse(
            query=body.query,
            entity_id=body.entity_id,
            detection_mode=body.detection_mode,
            total=0,
            detections=[],
            warnings=warnings,
            status="bounded_live_unwired",
        )

    if body.detection_mode == "fixture":
        # Return fixture data for testing
        detections.append(FulltextDetectResult(
            source_document_id="fixture:doc-001",
            title="Fixture Fulltext Detection",
            matched_text_span="WNK1 kinase domain",
            entity_candidate=body.entity_id or body.query or "WNK1",
            confidence=0.95,
            extraction_method="fulltext_fixture",
            provider="fixture",
        ))

    return FulltextDetectResponse(
        query=body.query,
        entity_id=body.entity_id,
        detection_mode=body.detection_mode,
        total=len(detections),
        detections=detections,
        warnings=warnings,
        status="available" if body.detection_mode == "fixture" else "persisted_only_no_data",
    )


@router.get("/health")
async def health() -> Dict[str, Any]:
    root = _lmp_v4_dir()
    local_count = len(list(root.glob("*.xml"))) if root.exists() else 0
    # Advertise the public GCS fallback so Alejandría can tell the user why
    # the corpus is visible even when the local cache is cold.
    corpus = _lmp_v4_scanner.describe_source(local_dir=root)
    total_count = local_count or int(corpus.get("count") or 0)

    # ── Literature Consolidation wiring status ──────────────────────────
    # Part of ALEJANDRIA_LIBRARY_LITERATURE_CONSOLIDATION_REWIRE_AUDIT_V1
    facade = get_library_search_facade()
    consolidation_health = facade.health()

    return {
        "ok": True,
        "lmp_v4_dir": str(root),
        "lmp_v4_exists": root.exists(),
        "lmp_v4_count": local_count,
        "lmp_v4_total": total_count,
        "corpus_source": corpus.get("source"),
        "corpus_bucket": corpus.get("bucket"),
        # ── Consolidation wiring status ──
        "literature_consolidation_wired": consolidation_health["literature_consolidation_wired"],
        "provider_quorum_available": consolidation_health["provider_quorum_available"],
        "bibliotecario_available": consolidation_health["bibliotecario_available"],
        "dlm_fulltext_available": consolidation_health["dlm_fulltext_available"],
        "active_providers": consolidation_health["active_providers"],
        "consolidation_init_errors": consolidation_health.get("init_errors"),
        # ── Mentions/Claims/Fulltext status (LIBRARY_DLM_CLAIMS_PERSISTENCE_V1) ──
        "mentions_endpoint_available": True,
        "mentions_detection_mode": "lmp_xml_cache_scan",
        "claims_endpoint_available": True,
        "claims_extraction_status": "claims_extraction_unimplemented",
        "fulltext_detect_status": "module_found_endpoint_unwired",
        "persistence_backend_status": "timescale_milvus_modules_found_not_queried",
        "default_mentions_mode": "lmp_xml_cache_scan",
        "default_claims_mode": "unimplemented",
    }
