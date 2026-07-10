"""
Evidence — Citation & Source Extraction
========================================

Pure functions for resolving scientific identifiers to official URLs,
building structured source records from Semantic Scholar paper dicts,
extracting DOI / PMID / PDB / UniProt identifiers from free text,
and deriving claim-level provenance.

All functions are stateless and take only explicit parameters —
no AgenticDriver instance required.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _url_quote

# Phase 1 extraction — redaction / truncation helpers
from ..utils import _truncate_text

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n+")


# ────────────────────────────────────────────────────────────────────
# Identifier → URL resolution
# ────────────────────────────────────────────────────────────────────

def official_link_from_identifiers(
    *,
    doi: Optional[str] = None,
    pmid: Optional[str] = None,
    pmcid: Optional[str] = None,
    uniprot_id: Optional[str] = None,
    pdb_id: Optional[str] = None,
    chembl_id: Optional[str] = None,
    open_access_url: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the first available identifier to an ``(official_url, display_citation)`` pair."""
    doi = str(doi or "").strip()
    pmid = str(pmid or "").strip()
    pmcid = str(pmcid or "").strip()
    uniprot_id = str(uniprot_id or "").strip()
    pdb_id = str(pdb_id or "").strip()
    chembl_id = str(chembl_id or "").strip()
    open_access_url = str(open_access_url or "").strip()

    if doi:
        return f"https://doi.org/{_url_quote(doi, safe='/')}", f"DOI:{doi}"
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{_url_quote(pmid, safe='')}/", f"PMID:{pmid}"
    if pmcid:
        pmcid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{_url_quote(pmcid, safe='')}/", f"PMCID:{pmcid}"
    if uniprot_id:
        return f"https://www.uniprot.org/uniprotkb/{_url_quote(uniprot_id, safe='')}/entry", f"UniProt:{uniprot_id}"
    if pdb_id:
        return f"https://www.rcsb.org/structure/{_url_quote(pdb_id, safe='')}", f"PDB:{pdb_id}"
    if chembl_id:
        return f"https://www.ebi.ac.uk/chembl/compound_report_card/{_url_quote(chembl_id, safe='')}/", f"ChEMBL:{chembl_id}"
    if open_access_url:
        # Only allow http(s) URLs – reject javascript:, data:, file:, etc.
        if re.match(r"^https?://", open_access_url):
            return open_access_url, "OpenAccessPDF"
    return None, None


# ────────────────────────────────────────────────────────────────────
# Paper → Source record
# ────────────────────────────────────────────────────────────────────

def build_source_record_from_paper(paper: Dict[str, Any]) -> Dict[str, Any]:
    """Build a normalised source record from a Semantic Scholar paper dict."""
    external_ids = paper.get("externalIds") or {}
    doi = external_ids.get("DOI") or paper.get("doi")
    pmid = external_ids.get("PubMed") or paper.get("pmid")
    pmcid = external_ids.get("PubMedCentral") or paper.get("pmcid")
    open_access_url = (
        (paper.get("openAccessPdf") or {}).get("url")
        if isinstance(paper.get("openAccessPdf"), dict)
        else None
    )
    official_url, display_citation = official_link_from_identifiers(
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        open_access_url=open_access_url,
    )
    source_id = str(
        paper.get("paperId")
        or doi
        or (f"PMID:{pmid}" if pmid else "")
        or (f"PMCID:{pmcid}" if pmcid else "")
        or paper.get("title")
        or "unknown_source"
    )
    return {
        "source_id": source_id,
        "source_type": "paper",
        "title": paper.get("title") or source_id,
        "official_url": official_url,
        "display_citation": display_citation or source_id,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "paper_id": paper.get("paperId"),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "evidence_snippet": _truncate_text(str(paper.get("abstract") or ""), max_len=300),
        "metadata": {
            "year": paper.get("year"),
            "citation_count": paper.get("citationCount"),
            "claim_atom_count": len(list(paper.get("claim_atoms") or [])),
            "authors": (
                [a.get("name", "") for a in (paper.get("authors") or [])[:6]]
                if isinstance(paper.get("authors"), list)
                else []
            ),
            "open_access_url": open_access_url,
        },
    }


def _sentence_candidates(text: str, *, max_sentences: int = 3) -> List[str]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []

    candidates: List[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(normalized):
        sentence = raw.strip(" -•\t")
        if len(sentence) < 24 or len(sentence.split()) < 4:
            continue
        if not re.search(r"[A-Za-z]", sentence):
            continue
        candidates.append(sentence)
        if len(candidates) >= max_sentences:
            break
    return candidates


def _fallback_claim_atoms_for_paper(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    atoms: List[Dict[str, Any]] = []
    title = str(paper.get("title") or "").strip()
    if title and len(title.split()) >= 4:
        atoms.append(
            {
                "atom_id": f"{paper.get('source_id') or paper.get('canonical_id') or paper.get('paperId') or 'paper'}:title",
                "section": "title",
                "text": title,
                "kind": "paper_title",
                "confidence": 0.62,
            }
        )

    for index, sentence in enumerate(_sentence_candidates(str(paper.get("abstract") or ""), max_sentences=2), start=1):
        atoms.append(
            {
                "atom_id": f"{paper.get('source_id') or paper.get('canonical_id') or paper.get('paperId') or 'paper'}:abstract:{index}",
                "section": "abstract",
                "text": sentence,
                "kind": "paper_sentence",
                "confidence": 0.58,
            }
        )
    return atoms


def _claim_from_paper_atom(paper: Dict[str, Any], atom: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    atom_id = str(atom.get("atom_id") or atom.get("claim_id") or f"{source_id}:atom").strip()
    text = _truncate_text(str(atom.get("text") or "").strip(), max_len=800)
    return {
        "claim_id": atom_id,
        "section": str(atom.get("section") or "literature"),
        "text": text,
        "strength": "supported",
        "confidence": float(atom.get("confidence", 0.62) or 0.62),
        "source_ids": [source_id],
        "counterevidence_ids": list(atom.get("counterevidence_ids") or []),
        "claim_origin": "literature_atom",
        "atom_kind": str(atom.get("kind") or "paper_sentence"),
        "source_role_types": {source_id: "primary_literature"},
        "paper_id": paper.get("paperId"),
        "canonical_paper_id": paper.get("canonical_id"),
    }


def extract_atomic_claims_and_sources_from_papers(
    papers: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    claims: List[Dict[str, Any]] = []
    sources: Dict[str, Dict[str, Any]] = {}
    seen_claim_ids: set[str] = set()

    for paper in papers or []:
        if not isinstance(paper, dict):
            continue
        source = build_source_record_from_paper(paper)
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            continue
        sources[source_id] = source
        claim_atoms = [atom for atom in list(paper.get("claim_atoms") or []) if isinstance(atom, dict)]
        if not claim_atoms:
            claim_atoms = _fallback_claim_atoms_for_paper(paper)
        for atom in claim_atoms:
            claim = _claim_from_paper_atom(paper, atom, source_id)
            claim_id = str(claim.get("claim_id") or "").strip()
            if not claim_id or claim_id in seen_claim_ids or not str(claim.get("text") or "").strip():
                continue
            seen_claim_ids.add(claim_id)
            claims.append(claim)

    return claims, list(sources.values())


def _collect_literature_papers(payload: Any) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            paper_id = str(node.get("paperId") or node.get("canonical_id") or "").strip()
            if paper_id and (node.get("abstract") or node.get("title") or node.get("claim_atoms")):
                collected.append(node)
            papers = node.get("papers")
            if isinstance(papers, list):
                for paper in papers:
                    _walk(paper)
            for nested_key in ("literature_context", "dlm_literature_context", "payload"):
                nested = node.get(nested_key)
                if isinstance(nested, (dict, list)):
                    _walk(nested)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for paper in collected:
        key = str(paper.get("canonical_id") or paper.get("paperId") or paper.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(paper)
    return deduped


# ────────────────────────────────────────────────────────────────────
# Bibliotecario citation formatting
# ────────────────────────────────────────────────────────────────────

def format_bibliotecario_citation_entry(
    citation: Dict[str, Any],
    paper_by_id: Dict[str, Dict[str, Any]],
) -> str:
    """Render a single citation as a Markdown bullet suitable for Bibliotecario output."""
    paper_id = str(citation.get("paper_id") or "?").strip()
    paper = paper_by_id.get(paper_id, {})
    source = build_source_record_from_paper(paper) if paper else {}
    link = str(source.get("official_url") or "").strip()
    label = str(source.get("display_citation") or paper_id or "source")
    title = str((paper or {}).get("title") or "").strip()
    finding = str(citation.get("finding") or "").strip()
    confidence = citation.get("confidence", "?")

    citation_ref = f"[{label}]({link})" if link else f"[{paper_id}]"
    title_part = f" — {title}" if title else ""
    return f"  • {citation_ref}{title_part}: {finding} (conf={confidence})"


# ────────────────────────────────────────────────────────────────────
# Free-text → source extraction (DOI, PMID, PDB, UniProt, ChEMBL)
# ────────────────────────────────────────────────────────────────────

def extract_sources_from_text(text: str) -> List[Dict[str, Any]]:
    """Extract scientific identifiers from *text* and return structured source records."""
    if not isinstance(text, str) or not text.strip():
        return []

    found: Dict[str, Dict[str, Any]] = {}

    for doi in sorted(set(re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", text, flags=re.IGNORECASE))):
        url, display = official_link_from_identifiers(doi=doi)
        found[f"DOI:{doi.lower()}"] = {
            "source_id": f"DOI:{doi}",
            "source_type": "paper",
            "title": f"DOI source {doi}",
            "official_url": url,
            "display_citation": display,
            "doi": doi,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "evidence_snippet": _truncate_text(text, max_len=240),
            "metadata": {},
        }

    for pmid in sorted(set(re.findall(r"\bPMID\s*:?\s*(\d{5,10})\b", text, flags=re.IGNORECASE))):
        url, display = official_link_from_identifiers(pmid=pmid)
        found[f"PMID:{pmid}"] = {
            "source_id": f"PMID:{pmid}",
            "source_type": "paper",
            "title": f"PubMed article {pmid}",
            "official_url": url,
            "display_citation": display,
            "pmid": pmid,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "evidence_snippet": _truncate_text(text, max_len=240),
            "metadata": {},
        }

    for pmcid in sorted(set(re.findall(r"\bPMC\s*:?\s*(\d{4,12})\b", text, flags=re.IGNORECASE))):
        pmcid_norm = f"PMC{pmcid}"
        url, display = official_link_from_identifiers(pmcid=pmcid_norm)
        found[pmcid_norm] = {
            "source_id": pmcid_norm,
            "source_type": "paper",
            "title": f"PMC article {pmcid_norm}",
            "official_url": url,
            "display_citation": display,
            "pmcid": pmcid_norm,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "evidence_snippet": _truncate_text(text, max_len=240),
            "metadata": {},
        }

    for accession in sorted(set(re.findall(r"\bUniProt\s*:?\s*([A-Z0-9]{6,10})\b", text, flags=re.IGNORECASE))):
        url, display = official_link_from_identifiers(uniprot_id=accession)
        found[f"UniProt:{accession}"] = {
            "source_id": f"UniProt:{accession}",
            "source_type": "protein",
            "title": f"UniProt entry {accession}",
            "official_url": url,
            "display_citation": display,
            "uniprot_id": accession,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "evidence_snippet": _truncate_text(text, max_len=240),
            "metadata": {},
        }

    for pdb_id in sorted(set(re.findall(r"\bPDB\s*:?\s*([0-9][A-Za-z0-9]{3})\b", text, flags=re.IGNORECASE))):
        url, display = official_link_from_identifiers(pdb_id=pdb_id)
        found[f"PDB:{pdb_id.upper()}"] = {
            "source_id": f"PDB:{pdb_id.upper()}",
            "source_type": "structure",
            "title": f"PDB structure {pdb_id.upper()}",
            "official_url": url,
            "display_citation": display,
            "pdb_id": pdb_id.upper(),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "evidence_snippet": _truncate_text(text, max_len=240),
            "metadata": {},
        }

    return list(found.values())


# ────────────────────────────────────────────────────────────────────
# Claim / source derivation
# ────────────────────────────────────────────────────────────────────

def derive_claims_and_sources(
    *,
    summary: str,
    findings: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Derive claim-level provenance + source records from *summary* and *findings*."""
    sources: Dict[str, Dict[str, Any]] = {}
    claims: List[Dict[str, Any]] = []

    def _register_sources(items: List[Dict[str, Any]]) -> List[str]:
        ids: List[str] = []
        for item in items:
            source_id = str(item.get("source_id") or "").strip()
            if not source_id:
                continue
            sources[source_id] = item
            ids.append(source_id)
        return ids

    literature_papers = _collect_literature_papers(findings)
    literature_claims, literature_sources = extract_atomic_claims_and_sources_from_papers(literature_papers)
    _register_sources(literature_sources)
    claims.extend(literature_claims)

    summary_source_ids = _register_sources(extract_sources_from_text(summary))
    if summary.strip():
        claims.append(
            {
                "claim_id": "claim-summary",
                "section": "abstract",
                "text": summary.strip(),
                "strength": "supported" if summary_source_ids else "suggestive",
                "confidence": 0.8 if summary_source_ids else 0.45,
                "source_ids": summary_source_ids,
                "counterevidence_ids": [],
            }
        )

    if isinstance(findings, list):
        for idx, finding in enumerate(findings, start=1):
            if isinstance(finding, dict):
                raw_text = finding.get("findings")
                if isinstance(raw_text, (list, tuple)):
                    text = "; ".join(str(x) for x in raw_text[:4])
                else:
                    text = str(raw_text or finding.get("summary") or "").strip()
                section = str(finding.get("subtask") or f"finding_{idx}")
            else:
                text = str(finding or "").strip()
                section = f"finding_{idx}"
            if not text:
                continue
            source_ids = _register_sources(extract_sources_from_text(text))
            claims.append(
                {
                    "claim_id": f"claim-{idx}",
                    "section": section,
                    "text": _truncate_text(text, max_len=800),
                    "strength": "supported" if source_ids else "suggestive",
                    "confidence": 0.78 if source_ids else 0.4,
                    "source_ids": source_ids,
                    "counterevidence_ids": [],
                }
            )

    return claims, list(sources.values())


# ────────────────────────────────────────────────────────────────────
# Native evidence extraction (agent side-data)
# ────────────────────────────────────────────────────────────────────

def extract_native_evidence_from_side_data(
    *,
    agent: str,
    channel: str,
    payload: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract claims + sources from specialist-agent side data."""
    sources: Dict[str, Dict[str, Any]] = {}
    claims: List[Dict[str, Any]] = []

    raw_sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    for source in raw_sources:
        if isinstance(source, dict):
            sid = str(source.get("source_id") or "").strip()
            if sid:
                sources[sid] = source

    papers = payload.get("papers") if isinstance(payload.get("papers"), list) else []
    paper_claims, paper_sources = extract_atomic_claims_and_sources_from_papers([
        paper for paper in papers if isinstance(paper, dict)
    ])
    claims.extend(paper_claims)
    for source in paper_sources:
        sid = str(source.get("source_id") or "").strip()
        if sid:
            sources[sid] = source

    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    for idx, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        paper_id = str(citation.get("paper_id") or "").strip()
        linked_source_ids = []
        for sid, source in sources.items():
            if sid == paper_id or str(source.get("paper_id") or "").strip() == paper_id:
                linked_source_ids.append(sid)
        claims.append(
            {
                "claim_id": f"{agent}-citation-{idx}",
                "section": f"{agent}:{channel}",
                "text": str(citation.get("finding") or "").strip(),
                "strength": "supported" if linked_source_ids else "suggestive",
                "confidence": float(citation.get("confidence", 0.6) or 0.6),
                "source_ids": linked_source_ids,
                "counterevidence_ids": [],
            }
        )

    return claims, list(sources.values())
