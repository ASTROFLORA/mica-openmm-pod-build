"""Bibliotecario Router — exposes memory module capabilities as API endpoints.

Surfaces underutilized capabilities from:
- memory/dlm/batch_mapper.py (scan_protein_literature, entity co-occurrence, temporal evolution)
- memory/dlm/entity_mapper.py (KB entity resolution)
- memory/dlm/presets.py (literature scan presets)
- memory/dlm/gcs_pdf_bridge.py (PDF → workspace download)
- memory/atom/system.py (temporal query, cognitive phase, thermodynamic summary)
- memory/dlm_lmp/metadata_service.py (protein metadata search)
- memory/dlm_lmp/convergence.py (LMP+DLM XML convergence)

POST /api/v1/research/bibliotecario/scan        — launch preset-driven research scan
GET  /api/v1/research/bibliotecario/presets      — list available presets
POST /api/v1/research/mica-q/query               — query the public MICA-Q multisurface console
POST /api/v1/research/entity/resolve             — resolve entity to KB IDs
GET  /api/v1/research/entity/co-occurrence       — entity co-occurrence from ATOM
GET  /api/v1/research/entity/evolution            — temporal entity evolution
POST /api/v1/research/atom/query                  — query ATOM TKG by phase/temperature
GET  /api/v1/research/atom/summary                — ATOM thermodynamic summary
POST /api/v1/research/pdf/download-to-workspace   — download PDF and store in GCS
GET  /api/v1/research/metadata/search             — search protein metadata cache
POST /api/v1/research/dlm-lmp/converge            — run DLM-LMP XML convergence
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.literature_consolidation.contracts.poll_envelope import normalize_poll_envelope
from mica.literature_consolidation.lmp_bibliotecario_handoff import compile_lmp_bibliotecario_handoff
from mica.literature_consolidation.pipeline import (
    best_available_literature_text,
    build_canonical_literature_bundle,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(user_dependency)])


def _serialize_router_payload(item: Any) -> Dict[str, Any]:
    payload: Any = item
    if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")):
        payload = item.to_dict()
    elif hasattr(item, "model_dump") and callable(getattr(item, "model_dump")):
        payload = item.model_dump(mode="json")
    elif hasattr(item, "__dataclass_fields__"):
        payload = asdict(item)
    elif isinstance(item, dict):
        payload = dict(item)
    elif hasattr(item, "__dict__"):
        payload = {key: value for key, value in vars(item).items() if not key.startswith("_")}
    else:
        payload = {"value": item}
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _json_safe_payload(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_payload(item) for item in value]
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        try:
            return _json_safe_payload(value.model_dump(mode="json"))
        except Exception:
            try:
                return _json_safe_payload(value.model_dump())
            except Exception:
                pass
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return _json_safe_payload(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dataclass_fields__"):
        try:
            return _json_safe_payload(asdict(value))
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _json_safe_payload(
            {key: item for key, item in vars(value).items() if not str(key).startswith("_")}
        )
    return str(value)


def _serialize_atom_fact_payload(item: Any) -> Dict[str, Any]:
    payload = _serialize_router_payload(item)
    if payload.get("content"):
        return payload
    triplet = {
        "subject": payload.get("subject"),
        "predicate": payload.get("predicate"),
        "object": payload.get("object"),
        "validity_start": payload.get("validity_start"),
        "validity_end": payload.get("validity_end"),
        "observation_time": payload.get("observation_time"),
        "confidence": payload.get("cognitive_confidence") if payload.get("cognitive_confidence") is not None else payload.get("confidence"),
    }
    if any(value is not None for value in triplet.values()):
        return triplet
    return payload

# ---------------------------------------------------------------------------
# RedisJobStore lazy singleton (same pattern as smic.py P1 migration)
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mica.worker.job_store import RedisJobStore as _RSType

_bib_job_store: Optional["_RSType"] = None


async def _get_bib_job_store() -> "_RSType":
    """Return (or create) a lazily-initialised RedisJobStore."""
    global _bib_job_store
    if _bib_job_store is None:
        import redis.asyncio as aioredis
        from mica.worker.job_store import RedisJobStore
        url = os.getenv("REDIS_URL") or os.getenv("MICA_REDIS_URL", "")
        if not url:
            raise HTTPException(503, "Redis not configured — cannot enqueue jobs")
        client = aioredis.from_url(url, decode_responses=False)
        _bib_job_store = RedisJobStore(client)
    return _bib_job_store


_MICA_Q_PROTOCOL_TOOL_DEFINE_RESEARCH_SCOPE = "define_research_scope"
_MICA_Q_PROTOCOL_TOOL_SEED_LITERATURE_SEARCH = "seed_literature_search"
_MICA_Q_PROTOCOL_TOOL_CITATION_CHAIN_EXPAND = "citation_chain_expand"
_MICA_Q_PROTOCOL_TOOL_INDEPENDENT_QUERY_EXPAND = "independent_query_expand"
_MICA_Q_PROTOCOL_TOOL_RANK_AND_SELECT_PAPERS = "rank_and_select_papers"
_MICA_Q_PROTOCOL_TOOL_RESOLVE_OPEN_ACCESS_FULLTEXT = "resolve_open_access_fulltext"
_MICA_Q_PROTOCOL_TOOL_DOWNLOAD_AND_STORE_PDFS = "download_and_store_pdfs"
_MICA_Q_PROTOCOL_TOOL_CREATE_KB_RECORD = "create_kb_record"
_MICA_Q_PROTOCOL_TOOL_HYDRATE_DOCUMENTS = "hydrate_documents"
_MICA_Q_PROTOCOL_TOOL_SCAN_DOCUMENTS = "scan_documents"
_MICA_Q_PROTOCOL_TOOL_PROMOTE_EVIDENCE = "promote_evidence"
_MICA_Q_PROTOCOL_TOOL_BUILD_PAPER_READMES = "build_paper_readmes"
_MICA_Q_PROTOCOL_TOOL_BUILD_KB_GRAPH = "build_kb_graph"
_MICA_Q_PROTOCOL_TOOL_SUMMARIZE_KB = "summarize_kb"
_MICA_Q_PROTOCOL_TOOL_FINALIZE_KB_RECEIPT = "finalize_kb_receipt"
_MICA_Q_PROTOCOL_TOOL_SEQUENCE = [
    _MICA_Q_PROTOCOL_TOOL_DEFINE_RESEARCH_SCOPE,
    _MICA_Q_PROTOCOL_TOOL_SEED_LITERATURE_SEARCH,
    _MICA_Q_PROTOCOL_TOOL_CITATION_CHAIN_EXPAND,
    _MICA_Q_PROTOCOL_TOOL_INDEPENDENT_QUERY_EXPAND,
    _MICA_Q_PROTOCOL_TOOL_RANK_AND_SELECT_PAPERS,
    _MICA_Q_PROTOCOL_TOOL_RESOLVE_OPEN_ACCESS_FULLTEXT,
    _MICA_Q_PROTOCOL_TOOL_DOWNLOAD_AND_STORE_PDFS,
    _MICA_Q_PROTOCOL_TOOL_CREATE_KB_RECORD,
    _MICA_Q_PROTOCOL_TOOL_HYDRATE_DOCUMENTS,
    _MICA_Q_PROTOCOL_TOOL_SCAN_DOCUMENTS,
    _MICA_Q_PROTOCOL_TOOL_PROMOTE_EVIDENCE,
    _MICA_Q_PROTOCOL_TOOL_BUILD_PAPER_READMES,
    _MICA_Q_PROTOCOL_TOOL_BUILD_KB_GRAPH,
    _MICA_Q_PROTOCOL_TOOL_SUMMARIZE_KB,
    _MICA_Q_PROTOCOL_TOOL_FINALIZE_KB_RECEIPT,
]
_MICA_Q_PROTOCOL_TOOL_SET = set(_MICA_Q_PROTOCOL_TOOL_SEQUENCE)
_MICA_Q_PROTOCOL_DEFAULT_PAPER_COUNT = 25
_MICA_Q_PROTOCOL_MAX_PAPER_COUNT = 200

_KB_PROVIDER_CAPABILITY_MATRIX: Dict[str, Dict[str, Any]] = {
    "pubmed_pmc": {
        "provider_label": "PubMed/PMC",
        "metadata_search": True,
        "references_support": "partial",
        "cited_by_support": "partial",
        "oa_fulltext_support": "partial",
        "rate_limits_auth_needs": "NCBI E-utilities quotas; optional API key for higher throughput",
        "implementation_status": "metadata_and_candidate_url_mapping",
    },
    "europepmc": {
        "provider_label": "EuropePMC",
        "metadata_search": True,
        "references_support": "partial",
        "cited_by_support": "partial",
        "oa_fulltext_support": "partial",
        "rate_limits_auth_needs": "Public API quotas; no mandatory auth for baseline usage",
        "implementation_status": "metadata_url_mapping",
    },
    "semantic_scholar_oa": {
        "provider_label": "Semantic Scholar OA",
        "metadata_search": True,
        "references_support": "partial",
        "cited_by_support": "partial",
        "oa_fulltext_support": "partial",
        "rate_limits_auth_needs": "API key often required for stable throughput",
        "implementation_status": "oa_url_mapping_plus_deep_research_seed",
    },
    "openalex": {
        "provider_label": "OpenAlex",
        "metadata_search": True,
        "references_support": "partial",
        "cited_by_support": "partial",
        "oa_fulltext_support": "partial",
        "rate_limits_auth_needs": "Public API quotas; polite pool mailto recommended",
        "implementation_status": "oa_url_mapping",
    },
    "unpaywall": {
        "provider_label": "Unpaywall",
        "metadata_search": "doi_only",
        "references_support": False,
        "cited_by_support": False,
        "oa_fulltext_support": "partial",
        "rate_limits_auth_needs": "Email parameter required for production-safe use",
        "implementation_status": "conditional_metadata_url_mapping",
    },
    "local_cache": {
        "provider_label": "Local Cache",
        "metadata_search": True,
        "references_support": "cache_dependent",
        "cited_by_support": "cache_dependent",
        "oa_fulltext_support": "cache_dependent",
        "rate_limits_auth_needs": "none",
        "implementation_status": "fixture_backed_and_workspace_dependent",
    },
}


def _provider_matrix_key(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"pmc", "pubmed", "pubmed_pmc"}:
        return "pubmed_pmc"
    if normalized in {"semantic_scholar", "semantic_scholar_oa", "semanticscholar"}:
        return "semantic_scholar_oa"
    if normalized in _KB_PROVIDER_CAPABILITY_MATRIX:
        return normalized
    return normalized


def _provider_matrix_entry(provider: str) -> Dict[str, Any]:
    key = _provider_matrix_key(provider)
    entry = dict(_KB_PROVIDER_CAPABILITY_MATRIX.get(key) or {})
    if not entry:
        entry = {
            "provider_label": str(provider or "unknown_provider"),
            "metadata_search": "unknown",
            "references_support": "unknown",
            "cited_by_support": "unknown",
            "oa_fulltext_support": "unknown",
            "rate_limits_auth_needs": "unknown",
            "implementation_status": "unknown",
        }
    entry["provider"] = key
    return entry


def _build_provider_capability_receipts(
    *,
    stage: str,
    providers: List[str],
    fixture_backed: bool,
    contacted_by_provider: Optional[Dict[str, bool]] = None,
    observations_by_provider: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    receipts: List[Dict[str, Any]] = []
    contacted_by_provider = dict(contacted_by_provider or {})
    observations_by_provider = dict(observations_by_provider or {})
    for provider in _dedupe_string_sequence(list(providers or [])):
        entry = _provider_matrix_entry(provider)
        provider_key = str(entry.get("provider") or provider)
        receipt = {
            "stage": stage,
            "provider": provider_key,
            "provider_label": str(entry.get("provider_label") or provider_key),
            "fixture_backed": bool(fixture_backed),
            "metadata_search": entry.get("metadata_search"),
            "references_support": entry.get("references_support"),
            "cited_by_support": entry.get("cited_by_support"),
            "oa_fulltext_support": entry.get("oa_fulltext_support"),
            "rate_limits_auth_needs": str(entry.get("rate_limits_auth_needs") or ""),
            "implementation_status": str(entry.get("implementation_status") or "unknown"),
            "contacted": bool(contacted_by_provider.get(provider_key, False)),
            "observation": dict(observations_by_provider.get(provider_key) or {}),
        }
        receipts.append(receipt)
    return receipts
_MICA_Q_EXPLICIT_VERB_PREFIXES = (
    "lit:",
    "dlm:",
    "atom:",
    "workspace:",
    "protocol:",
    "ledger:",
    "node:",
)
_MICA_Q_AUTOPROTOCOL_ACTION_MARKERS = (
    "build",
    "create",
    "make",
    "generate",
    "compile",
    "collect",
    "gather",
    "hydrate",
    "hydration",
    "deep scan",
    "deep-scan",
    "scan",
    "download",
    "crear",
    "crea",
    "hacer",
    "haz",
    "genera",
    "generar",
    "hidratar",
    "hidrata",
    "descargar",
)
_MICA_Q_AUTOPROTOCOL_OUTPUT_MARKERS = (
    "knowledge base",
    "summary",
    "report",
    "synthesis",
    "resumen",
    "reporte",
    "sintesis",
)
_MICA_Q_AUTOPROTOCOL_DOWNLOAD_MARKERS = ("download", "descargar")
_MICA_Q_AUTOPROTOCOL_STRICTNESS_MARKERS = (
    "strict",
    "rigorous",
    "high confidence",
    "alta confianza",
    "riguroso",
)
_MICA_Q_AUTOPROTOCOL_CITATION_CHAIN_MARKERS = (
    "citation chain",
    "chain of citations",
    "citation lineage",
    "cadena de citas",
)
_MICA_Q_GENERIC_QUERY_TOKENS = {
    "a",
    "about",
    "an",
    "and",
    "axis",
    "base",
    "build",
    "compile",
    "construct",
    "create",
    "deep",
    "download",
    "for",
    "generate",
    "hydrate",
    "i",
    "kb",
    "knowledge",
    "make",
    "me",
    "of",
    "on",
    "paper",
    "papers",
    "report",
    "scan",
    "summary",
    "the",
    "with",
}

_protocol_kb_services_lock = asyncio.Lock()
_protocol_kb_store: Any = None
_protocol_kb_service: Any = None
_protocol_document_scan_service: Any = None


def _mica_q_protocol_receipt_fields() -> List[str]:
    return [
        "protocol_id",
        "node_id",
        "event_type",
        "actor_surface",
        "actor_id",
        "state_before",
        "state_after",
        "artifact_refs",
        "evidence_refs",
        "cost_snapshot",
        "approval_refs",
        "timestamp",
    ]


def _mica_q_normalize_query_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _mica_q_kb_requested(query_text: str) -> bool:
    normalized = _mica_q_normalize_query_text(query_text)
    return bool(re.search(r"\bkb\b", normalized)) or "knowledge base" in normalized


def _extract_requested_paper_count(query_text: str, *, fallback: int) -> int:
    normalized = _mica_q_normalize_query_text(query_text)
    match = re.search(
        r"\b(\d{1,4})\s+(?:papers?|articles?|publications?|documents?|pdfs?|articulos?|publicaciones?)\b",
        normalized,
    )
    if match:
        return max(1, min(int(match.group(1)), _MICA_Q_PROTOCOL_MAX_PAPER_COUNT))
    return max(1, min(int(fallback), _MICA_Q_PROTOCOL_MAX_PAPER_COUNT))


def _compact_mica_q_query_label(query_text: str, *, max_chars: int = 96) -> str:
    compact = " ".join(str(query_text or "").strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _extract_mica_q_research_focus(query_text: str) -> str:
    compact = " ".join(str(query_text or "").strip().split())
    if not compact:
        return compact
    match = re.search(
        r"\b(?:on|about|regarding|for|sobre|acerca de)\s+(.+?)(?:\s+(?:with|using|including|con)\s+\d{1,4}\s+(?:papers?|articles?|publications?|documents?|pdfs?|articulos?|publicaciones?)\b|$)",
        compact,
        flags=re.IGNORECASE,
    )
    focus = match.group(1) if match else compact
    focus = re.sub(r"^(?:the|a|an|el|la|los|las)\s+", "", focus, flags=re.IGNORECASE)
    return focus.strip(" .") or compact


def _extract_mica_q_focus_terms(*, focus_query: str, payload_terms: List[str]) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9-]+", focus_query)
    filtered: List[str] = []
    seen: set[str] = set()
    for token in tokens + list(payload_terms or []):
        cleaned = str(token or "").strip()
        if not cleaned:
            continue
        lower = cleaned.lower()
        if lower in _MICA_Q_GENERIC_QUERY_TOKENS:
            continue
        if len(cleaned) < 3:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        filtered.append(cleaned)
    return filtered[:12]


def _safe_protocol_user_dirname(user_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(user_id or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or "anonymous"


def _mica_q_protocol_checkpoint_dir(*, user_id: str) -> str:
    from mica.drivers.agentic_driver import AgenticDriverConfig

    cfg = AgenticDriverConfig.from_driver_config()
    checkpoint_dir = (
        Path(cfg.checkpoint_dir)
        / "api_v1"
        / _safe_protocol_user_dirname(user_id)
        / "mica_q_protocol_executor"
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return str(checkpoint_dir)


def _canonical_paper_identifier(paper: Dict[str, Any]) -> str:
    for key in ("canonical_id", "paperId", "paper_id", "doi", "pmid", "externalIds"):
        value = paper.get(key)
        if isinstance(value, dict):
            for nested_key in ("DOI", "PubMed", "CorpusId", "ArXiv"):
                nested = str(value.get(nested_key) or "").strip()
                if nested:
                    return nested
        cleaned = str(value or "").strip()
        if cleaned:
            return cleaned
    return ""


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _paper_scan_metadata(
    paper: Dict[str, Any],
    *,
    query_text: str,
    session_id: str,
    workspace_id: str,
) -> Dict[str, Any]:
    external_ids = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    return {
        "query_text": query_text,
        "source_format": "literature_document",
        "source_session_id": session_id,
        "workspace_id": workspace_id,
        "doi": str(paper.get("doi") or external_ids.get("DOI") or ""),
        "pmid": str(paper.get("pmid") or external_ids.get("PubMed") or ""),
        "year": paper.get("year"),
        "venue": str(paper.get("venue") or paper.get("journal") or ""),
        "authors": list(paper.get("authors") or []),
        "citation_count": _safe_int(paper.get("citationCount") or paper.get("citation_count")),
        "paper_url": str(paper.get("url") or ""),
    }


def _mica_q_top_papers(papers: List[Dict[str, Any]], *, limit: int = 5) -> List[Dict[str, Any]]:
    ranked = sorted(
        [dict(item or {}) for item in papers],
        key=lambda item: (
            _safe_int(item.get("citationCount") or item.get("citation_count")),
            _safe_int(item.get("year")),
        ),
        reverse=True,
    )
    top: List[Dict[str, Any]] = []
    for paper in ranked[:limit]:
        top.append(
            {
                "canonical_id": _canonical_paper_identifier(paper),
                "title": str(paper.get("title") or ""),
                "year": paper.get("year"),
                "citation_count": _safe_int(paper.get("citationCount") or paper.get("citation_count")),
                "doi": str(paper.get("doi") or ""),
            }
        )
    return top


def _mica_q_top_claims(scans: List[Any], *, limit: int = 8) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    seen_text: set[str] = set()
    for scan in scans:
        for claim in list(getattr(scan, "candidate_claims", []) or []):
            text = " ".join(str(getattr(claim, "text", "") or "").split())
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            ranked.append(
                {
                    "claim_id": str(getattr(claim, "claim_id", "") or ""),
                    "text": text,
                    "support_score": _safe_float(getattr(claim, "support_score", 0.0)),
                    "section_type": str(getattr(claim, "section_type", "") or ""),
                    "scan_id": str(getattr(scan, "scan_id", "") or ""),
                    "title": str(getattr(scan, "title", "") or ""),
                    "canonical_paper_id": str(getattr(getattr(scan, "document", None), "canonical_paper_id", "") or ""),
                }
            )
    ranked.sort(key=lambda item: (item["support_score"], item["title"]), reverse=True)
    return ranked[:limit]


def _build_mica_q_summary_text(
    *,
    query_text: str,
    kb_id: str,
    retrieved_papers: int,
    hydrated_papers: int,
    scan_count: int,
    promotion_count: int,
    top_claims: List[Dict[str, Any]],
) -> str:
    lines = [
        f"Deterministic MICA_Q protocol executed for '{query_text}'.",
        f"Deep research retrieved {retrieved_papers} papers and KB {kb_id} hydrated {hydrated_papers} selected papers into {scan_count} scans with {promotion_count} promoted evidence packets.",
    ]
    if top_claims:
        claim_lines = []
        for index, claim in enumerate(top_claims[:3], start=1):
            claim_lines.append(
                f"{index}. {claim['text']} (support={claim['support_score']:.2f}, paper={claim['canonical_paper_id'] or claim['title'] or 'unknown'})"
            )
        lines.append("High-signal claims: " + " ".join(claim_lines))
    return "\n".join(lines)


def _dedupe_string_sequence(values: List[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def _paper_reference_ids(paper: Dict[str, Any]) -> List[str]:
    references: List[str] = []
    raw_refs = paper.get("references") if isinstance(paper.get("references"), list) else []
    for ref in raw_refs:
        if isinstance(ref, dict):
            paper_id = str(ref.get("paperId") or ref.get("paper_id") or ref.get("id") or "").strip()
            if paper_id:
                references.append(paper_id)
        else:
            reference = str(ref or "").strip()
            if reference:
                references.append(reference)
    raw_ids = paper.get("reference_ids") if isinstance(paper.get("reference_ids"), list) else []
    for ref in raw_ids:
        reference = str(ref or "").strip()
        if reference:
            references.append(reference)
    return _dedupe_string_sequence(references)


def _paper_cited_by_ids(paper: Dict[str, Any]) -> List[str]:
    cited_by: List[str] = []
    for key in ("cited_by", "citedBy", "citations", "citing_papers"):
        raw_values = paper.get(key)
        if not isinstance(raw_values, list):
            continue
        for item in raw_values:
            if isinstance(item, dict):
                paper_id = str(item.get("paperId") or item.get("paper_id") or item.get("id") or "").strip()
                if paper_id:
                    cited_by.append(paper_id)
            else:
                value = str(item or "").strip()
                if value:
                    cited_by.append(value)
    return _dedupe_string_sequence(cited_by)


def _citation_candidate_from_edge(*, paper_id: str, relation: str, seed_id: str) -> Dict[str, Any]:
    return {
        "paperId": paper_id,
        "canonical_id": paper_id,
        "title": f"Citation candidate {paper_id}",
        "source_relation": relation,
        "seed_paper_id": seed_id,
        "citation_chain_role": relation,
    }


def _paper_provider_candidates(paper: Dict[str, Any], *, provider_order: List[str]) -> List[Dict[str, Any]]:
    metadata = dict(paper.get("metadata") or {})
    candidates: Dict[str, Dict[str, Any]] = {}

    pmcid = str(paper.get("pmcid") or metadata.get("pmcid") or "").strip()
    if pmcid:
        candidates["pmc"] = {
            "provider": "pmc",
            "source_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
            "kind": "html",
        }

    europepmc_url = str(metadata.get("europepmc_url") or "").strip()
    if europepmc_url:
        candidates["europepmc"] = {
            "provider": "europepmc",
            "source_url": europepmc_url,
            "kind": "html",
        }

    open_access_pdf = paper.get("openAccessPdf") if isinstance(paper.get("openAccessPdf"), dict) else {}
    semantic_scholar_pdf = str(open_access_pdf.get("url") or "").strip()
    if semantic_scholar_pdf:
        candidates["semantic_scholar"] = {
            "provider": "semantic_scholar",
            "source_url": semantic_scholar_pdf,
            "kind": "pdf",
        }

    openalex_url = str(metadata.get("openalex_best_oa_url") or metadata.get("openalex_url") or "").strip()
    if openalex_url:
        candidates["openalex"] = {
            "provider": "openalex",
            "source_url": openalex_url,
            "kind": "pdf" if openalex_url.lower().endswith(".pdf") else "html",
        }

    unpaywall_url = str(metadata.get("unpaywall_oa_url") or metadata.get("unpaywall_url") or "").strip()
    if unpaywall_url:
        candidates["unpaywall"] = {
            "provider": "unpaywall",
            "source_url": unpaywall_url,
            "kind": "pdf" if unpaywall_url.lower().endswith(".pdf") else "html",
        }

    ordered: List[Dict[str, Any]] = []
    for provider in provider_order:
        if provider in candidates:
            ordered.append(candidates[provider])
    for provider, candidate in candidates.items():
        if provider not in provider_order:
            ordered.append(candidate)
    return ordered


def _fulltext_status_for_paper(
    paper: Dict[str, Any],
    *,
    provider_order: List[str],
    always_try_pdf: bool,
    user_id: str,
    workspace_id: str,
    session_id: str,
) -> Dict[str, Any]:
    paper_id = _canonical_paper_identifier(paper)
    metadata = dict(paper.get("metadata") or {})
    abstract_text = str(paper.get("abstract") or "").strip()
    full_text = str(best_available_literature_text(paper) or "").strip()

    downloaded_uri = str(
        metadata.get("downloaded_pdf_uri")
        or metadata.get("pdf_download_uri")
        or paper.get("downloaded_pdf_uri")
        or ""
    ).strip()
    downloaded_checksum = str(
        metadata.get("pdf_checksum")
        or metadata.get("sha256")
        or paper.get("pdf_checksum")
        or ""
    ).strip()
    downloaded_size = _safe_int(metadata.get("pdf_size") or metadata.get("pdf_size_bytes") or paper.get("pdf_size"))

    provider_candidates = _paper_provider_candidates(paper, provider_order=provider_order)
    chosen_provider = provider_candidates[0] if provider_candidates else None

    if downloaded_uri:
        status = "pdf_downloaded"
        source_provider = str((chosen_provider or {}).get("provider") or "workspace_pdf")
        source_url = downloaded_uri
    elif chosen_provider and (full_text or always_try_pdf):
        status = "fulltext_html_available"
        source_provider = str(chosen_provider.get("provider") or "")
        source_url = str(chosen_provider.get("source_url") or "")
    elif abstract_text:
        status = "abstract_only"
        source_provider = str((chosen_provider or {}).get("provider") or "abstract")
        source_url = str((chosen_provider or {}).get("source_url") or "")
    else:
        status = "unavailable"
        source_provider = str((chosen_provider or {}).get("provider") or "unknown")
        source_url = str((chosen_provider or {}).get("source_url") or "")

    checksum_seed = "|".join([paper_id, source_provider, source_url])
    checksum = downloaded_checksum or hashlib.sha256(checksum_seed.encode("utf-8")).hexdigest()

    return {
        "paper_id": paper_id,
        "status": status,
        "provider": source_provider,
        "source_url": source_url,
        "checksum": checksum,
        "size_bytes": downloaded_size,
        "always_try_pdf": bool(always_try_pdf),
        "provider_candidates": provider_candidates,
        "user_scope": {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "session_id": session_id,
        },
    }


def _paper_entities(paper: Dict[str, Any], *, terms: List[str]) -> List[str]:
    entities = paper.get("entities") if isinstance(paper.get("entities"), list) else []
    named = [str(item or "").strip() for item in entities if str(item or "").strip()]
    if named:
        return _dedupe_string_sequence(named)[:8]
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    pool = [item for item in list(terms or []) if item and item.lower() in f"{title} {abstract}".lower()]
    return _dedupe_string_sequence([str(item) for item in pool])[:8]


def _paper_methods(paper: Dict[str, Any]) -> List[str]:
    methods = paper.get("methods") if isinstance(paper.get("methods"), list) else []
    if methods:
        return _dedupe_string_sequence([str(item or "").strip() for item in methods if str(item or "").strip()])[:6]
    abstract = str(paper.get("abstract") or "").lower()
    inferred: List[str] = []
    for keyword, label in (
        ("cryo", "cryo-EM"),
        ("molecular dynamics", "molecular_dynamics"),
        ("western blot", "western_blot"),
        ("mass spectrometry", "mass_spectrometry"),
        ("rna-seq", "rna_seq"),
    ):
        if keyword in abstract:
            inferred.append(label)
    return _dedupe_string_sequence(inferred)


def _paper_claims_and_limitations(paper: Dict[str, Any]) -> Dict[str, List[str]]:
    abstract = " ".join(str(paper.get("abstract") or "").split())
    if not abstract:
        return {"claims": [], "limitations": []}
    sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", abstract) if segment.strip()]
    claims = sentences[:2]
    limitations = [segment for segment in sentences if re.search(r"\b(limit|however|uncertain|bias|small)\b", segment, re.IGNORECASE)]
    return {
        "claims": claims[:3],
        "limitations": limitations[:2],
    }


def _build_paper_cards(
    *,
    papers: List[Dict[str, Any]],
    citation_roles: Dict[str, str],
    fulltext_map: Dict[str, Dict[str, Any]],
    terms: List[str],
) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for paper in papers:
        paper_id = _canonical_paper_identifier(paper)
        role = str(citation_roles.get(paper_id) or "seed_query")
        fulltext = dict(fulltext_map.get(paper_id) or {})
        entities = _paper_entities(paper, terms=terms)
        methods = _paper_methods(paper)
        claim_payload = _paper_claims_and_limitations(paper)
        citation_count = _safe_int(paper.get("citationCount") or paper.get("citation_count"))
        why_included = f"Selected as {role} candidate"
        if citation_count > 0:
            why_included += f" with citation_count={citation_count}."
        else:
            why_included += "."
        cards.append(
            {
                "paper_id": paper_id,
                "title": str(paper.get("title") or ""),
                "identifiers": {
                    "paperId": str(paper.get("paperId") or ""),
                    "doi": str(paper.get("doi") or (paper.get("externalIds") or {}).get("DOI") or ""),
                    "pmid": str(paper.get("pmid") or (paper.get("externalIds") or {}).get("PubMed") or ""),
                },
                "why_included": why_included,
                "citation_chain_role": role,
                "fulltext_status": str(fulltext.get("status") or "unavailable"),
                "key_entities": entities,
                "methods": methods,
                "evidence_types": ["fulltext" if str(fulltext.get("status") or "") in {"pdf_downloaded", "fulltext_html_available"} else "abstract"],
                "key_claims": list(claim_payload.get("claims") or []),
                "limitations": list(claim_payload.get("limitations") or []),
                "artifact_refs": _dedupe_string_sequence(
                    [
                        f"paper://{paper_id}",
                        str(fulltext.get("source_url") or ""),
                    ]
                ),
            }
        )
    return cards


def _build_kb_evidence_graph(
    *,
    kb_id: str,
    papers: List[Dict[str, Any]],
    paper_cards: List[Dict[str, Any]],
    citation_graph: Dict[str, Dict[str, List[str]]],
) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    node_seen: set[str] = set()

    def _add_node(node_id: str, node_type: str, payload: Dict[str, Any]) -> None:
        if node_id in node_seen:
            return
        node_seen.add(node_id)
        nodes.append({"id": node_id, "type": node_type, **payload})

    card_by_paper = {str(card.get("paper_id") or ""): dict(card) for card in paper_cards}
    for paper in papers:
        paper_id = _canonical_paper_identifier(paper)
        card = card_by_paper.get(paper_id, {})
        paper_node_id = f"paper://{paper_id}"
        _add_node(
            paper_node_id,
            "paper",
            {
                "title": str(paper.get("title") or ""),
                "year": paper.get("year"),
                "citation_chain_role": str(card.get("citation_chain_role") or "seed_query"),
            },
        )

        for entity in list(card.get("key_entities") or []):
            entity_node_id = f"entity://{entity}"
            _add_node(entity_node_id, "entity", {"label": entity})
            edges.append({"source": paper_node_id, "target": entity_node_id, "type": "mentions_entity"})

        for method in list(card.get("methods") or []):
            method_node_id = f"method://{method}"
            _add_node(method_node_id, "method", {"label": method})
            edges.append({"source": paper_node_id, "target": method_node_id, "type": "uses_method"})

        for index, claim in enumerate(list(card.get("key_claims") or []), start=1):
            claim_node_id = f"claim://{paper_id}:{index}"
            _add_node(claim_node_id, "claim", {"text": claim})
            edges.append({"source": paper_node_id, "target": claim_node_id, "type": "supports_claim"})
            evidence_node_id = f"evidence://{paper_id}:{index}"
            _add_node(evidence_node_id, "evidence_span", {"span_text": claim})
            edges.append({"source": claim_node_id, "target": evidence_node_id, "type": "has_evidence_span"})

    for source_paper_id, linkage in dict(citation_graph or {}).items():
        source_node = f"paper://{source_paper_id}"
        for target_id in list((linkage or {}).get("references") or []):
            edges.append({"source": source_node, "target": f"paper://{target_id}", "type": "cites"})
        for target_id in list((linkage or {}).get("cited_by") or []):
            edges.append({"source": f"paper://{target_id}", "target": source_node, "type": "cites"})

    serialized = {
        "kb_id": kb_id,
        "nodes": nodes,
        "edges": edges,
    }
    graph_digest = hashlib.sha256(json.dumps(serialized, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:16]
    artifact_uri = f"artifact://kb_graph/{kb_id or 'transient'}-{graph_digest}.json"
    return {
        "artifact_uri": artifact_uri,
        "json": serialized,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def _infer_mica_q_autonomous_candidate(
    *,
    query_text: str,
    workspace_id: str,
    session_id: str,
    limit: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    normalized = _mica_q_normalize_query_text(query_text)
    if not normalized:
        return None
    if any(normalized.startswith(prefix) for prefix in _MICA_Q_EXPLICIT_VERB_PREFIXES):
        return None
    if list(payload.get("compiled_tool_calls") or []):
        return None

    has_action = any(marker in normalized for marker in _MICA_Q_AUTOPROTOCOL_ACTION_MARKERS)
    has_output = _mica_q_kb_requested(normalized) or any(
        marker in normalized for marker in _MICA_Q_AUTOPROTOCOL_OUTPUT_MARKERS
    )
    has_download = any(marker in normalized for marker in _MICA_Q_AUTOPROTOCOL_DOWNLOAD_MARKERS)
    mentions_papers = bool(
        re.search(
            r"\b(?:papers?|articles?|publications?|documents?|pdfs?|articulos?|publicaciones?)\b",
            normalized,
        )
    )
    if not ((has_action and has_output) or (has_download and mentions_papers)):
        return None

    requested_papers = _extract_requested_paper_count(
        query_text,
        fallback=max(limit, _MICA_Q_PROTOCOL_DEFAULT_PAPER_COUNT),
    )
    strict_mode = any(marker in normalized for marker in _MICA_Q_AUTOPROTOCOL_STRICTNESS_MARKERS)
    citation_depth = 2 if any(marker in normalized for marker in _MICA_Q_AUTOPROTOCOL_CITATION_CHAIN_MARKERS) else 1
    research_query = _extract_mica_q_research_focus(query_text)
    payload_terms = [str(item or "").strip() for item in list(payload.get("terms") or []) if str(item or "").strip()]
    terms = _extract_mica_q_focus_terms(focus_query=research_query, payload_terms=payload_terms)
    kb_label = _compact_mica_q_query_label(query_text)

    return {
        "tool_name": _MICA_Q_PROTOCOL_TOOL_DEFINE_RESEARCH_SCOPE,
        "objective": "Compile and execute a deterministic multi-node KB protocol graph.",
        "query": query_text,
        "research_query": research_query,
        "requested_papers": requested_papers,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "terms": terms,
        "citation_depth": citation_depth,
        "download_pdfs": bool(has_download and session_id),
        "download_requested": bool(has_download),
        "kb_requested": _mica_q_kb_requested(normalized),
        "report_requested": True,
        "minimum_evidentiality_score": 0.4 if strict_mode else 0.1,
        "kb_name": f"MICA_Q :: {kb_label}",
        "strict_mode": strict_mode,
    }


def _build_mica_q_protocol_document(*, candidate: Dict[str, Any]) -> Dict[str, Any]:
    def _node(node_id: str, tool_name: str, objective: str, dependencies: List[str], artifacts: List[str]) -> Dict[str, Any]:
        return {
            "node_id": node_id,
            "node_kind": "tool",
            "executor_surface": "mica_q_multisurface",
            "executor_id": "MICAQMultisurfaceService",
            "objective": objective,
            "dependencies": list(dependencies),
            "inputs": {
                **dict(candidate),
                "tool_name": tool_name,
                "node_id": node_id,
            },
            "expected_outputs": {
                "artifacts": list(artifacts),
            },
            "evidence_requirements": ["node_receipt"],
            "policies": {},
            "failure_policy": "halt",
            "receipt_schema": {
                "schema_id": "mica.receipts.node.v1",
                "required_fields": _mica_q_protocol_receipt_fields(),
            },
        }

    protocol_id = f"protocol-mica-q-{uuid.uuid4().hex[:12]}"
    session_id = str(candidate.get("session_id") or f"mica-q-{uuid.uuid4().hex[:12]}")
    nodes = [
        _node(
            "define-research-scope",
            _MICA_Q_PROTOCOL_TOOL_DEFINE_RESEARCH_SCOPE,
            "Define research scope and deterministic protocol parameters.",
            [],
            ["scope_profile"],
        ),
        _node(
            "seed-literature-search",
            _MICA_Q_PROTOCOL_TOOL_SEED_LITERATURE_SEARCH,
            "Run seeded literature retrieval for the scoped query.",
            ["define-research-scope"],
            ["literature_candidates"],
        ),
        _node(
            "citation-chain-expand",
            _MICA_Q_PROTOCOL_TOOL_CITATION_CHAIN_EXPAND,
            "Apply deterministic citation chain expansion.",
            ["seed-literature-search"],
            ["citation_expansion"],
        ),
        _node(
            "independent-query-expand",
            _MICA_Q_PROTOCOL_TOOL_INDEPENDENT_QUERY_EXPAND,
            "Apply independent query expansion for recall hardening.",
            ["citation-chain-expand"],
            ["query_expansion"],
        ),
        _node(
            "rank-and-select-papers",
            _MICA_Q_PROTOCOL_TOOL_RANK_AND_SELECT_PAPERS,
            "Rank and select papers for hydration.",
            ["independent-query-expand"],
            ["selected_papers"],
        ),
        _node(
            "resolve-open-access-fulltext",
            _MICA_Q_PROTOCOL_TOOL_RESOLVE_OPEN_ACCESS_FULLTEXT,
            "Resolve fulltext availability and OA signals.",
            ["rank-and-select-papers"],
            ["fulltext_resolution"],
        ),
        _node(
            "download-and-store-pdfs",
            _MICA_Q_PROTOCOL_TOOL_DOWNLOAD_AND_STORE_PDFS,
            "Download/store PDFs when requested.",
            ["resolve-open-access-fulltext"],
            ["pdf_download_report"],
        ),
        _node(
            "create-kb-record",
            _MICA_Q_PROTOCOL_TOOL_CREATE_KB_RECORD,
            "Create the durable KB record with authoritative owner scope.",
            ["download-and-store-pdfs"],
            ["knowledge_base"],
        ),
        _node(
            "hydrate-documents",
            _MICA_Q_PROTOCOL_TOOL_HYDRATE_DOCUMENTS,
            "Hydrate selected literature documents into KB scans.",
            ["create-kb-record"],
            ["scan_inputs"],
        ),
        _node(
            "scan-documents",
            _MICA_Q_PROTOCOL_TOOL_SCAN_DOCUMENTS,
            "Materialize scan inventory for promotion.",
            ["hydrate-documents"],
            ["scan_inventory"],
        ),
        _node(
            "promote-evidence",
            _MICA_Q_PROTOCOL_TOOL_PROMOTE_EVIDENCE,
            "Promote evidential atoms using deterministic thresholds.",
            ["scan-documents"],
            ["promoted_evidence"],
        ),
        _node(
            "build-paper-readmes",
            _MICA_Q_PROTOCOL_TOOL_BUILD_PAPER_READMES,
            "Build per-paper summary/readme payloads.",
            ["promote-evidence"],
            ["paper_readmes"],
        ),
        _node(
            "build-kb-graph",
            _MICA_Q_PROTOCOL_TOOL_BUILD_KB_GRAPH,
            "Build KB graph projection from promoted atoms.",
            ["build-paper-readmes"],
            ["kb_graph"],
        ),
        _node(
            "summarize-kb",
            _MICA_Q_PROTOCOL_TOOL_SUMMARIZE_KB,
            "Compose deterministic KB summary with lineage stats.",
            ["build-kb-graph"],
            ["deterministic_summary"],
        ),
        _node(
            "finalize-kb-receipt",
            _MICA_Q_PROTOCOL_TOOL_FINALIZE_KB_RECEIPT,
            "Emit final KB receipt payload for API surface projection.",
            ["summarize-kb"],
            ["knowledge_base", "literature_bundle", "deterministic_summary", "final_receipt"],
        ),
    ]
    edges: List[Dict[str, Any]] = []
    for node in nodes:
        for dependency in list(node.get("dependencies") or []):
            edges.append(
                {
                    "source_node_id": dependency,
                    "target_node_id": str(node.get("node_id") or ""),
                    "edge_type": "control_dependency",
                }
            )

    return {
        "@context": "https://mica.astroflora.org/schema/protocol/v1",
        "@type": "MICAProtocol",
        "protocol_id": protocol_id,
        "version": "1.0.0",
        "session_id": session_id,
        "owner_lab": "Literature And Evidence Acquisition Lab",
        "execution_mode": "development",
        "risk_profile": "medium",
        "budgets": {
            "max_steps": len(nodes),
            "max_usd": 25.0,
            "max_wall_clock_s": 5400,
        },
        "approval_policy": {
            "mode": "auto",
            "required_approvers": [],
            "protected_surfaces": [],
        },
        "ledger_policy": {
            "mode": "protocol_and_node_receipts",
            "receipt_schema": "mica.receipts.node.v1",
            "emit_events": True,
            "require_node_receipts": True,
        },
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "name": "MICA_Q autonomous deep research multi-node workflow",
            "query_surface": "public_mica_q_query",
            "requested_papers": int(candidate.get("requested_papers") or 0),
            "protocol_editor": {
                "editable": True,
                "graph_mode": "multi_node",
                "template": "mica_q_create_kb_v1",
            },
        },
    }


async def _get_protocol_kb_services() -> tuple[Any, Any]:
    global _protocol_kb_store, _protocol_kb_service, _protocol_document_scan_service
    if _protocol_kb_service is not None and _protocol_document_scan_service is not None:
        return _protocol_kb_service, _protocol_document_scan_service

    async with _protocol_kb_services_lock:
        if _protocol_kb_service is not None and _protocol_document_scan_service is not None:
            return _protocol_kb_service, _protocol_document_scan_service

        from mica.infrastructure.persistence.kb_postgres_store import KBPostgresStore
        from mica.pipelines.knowledge_fabric.document_scan_service import DocumentScanService
        from mica.pipelines.knowledge_fabric.kb_service import KBService

        store = KBPostgresStore()
        await store.initialize()
        _protocol_kb_store = store
        _protocol_kb_service = KBService(store=store)
        _protocol_document_scan_service = DocumentScanService(store=store)
        return _protocol_kb_service, _protocol_document_scan_service


async def _run_mica_q_research_kb_summary_workflow(
    *,
    query_text: str,
    research_query: str,
    user_id: str,
    session_id: str,
    workspace_id: str,
    requested_papers: int,
    terms: List[str],
    citation_depth: int,
    download_pdfs: bool,
    download_requested: bool,
    kb_name: str,
    minimum_evidentiality_score: float,
) -> Dict[str, Any]:
    from mica.literature_consolidation.services.deep_research_service import (
        DeepResearchExecutionRequest,
        run_deep_research,
    )
    from mica.pipelines.knowledge_fabric.contracts import KBStatus, KBType, OwnerScope
    from mica.pipelines.knowledge_fabric.document_envelope import DocumentKind, DocumentScanMode

    kb_service, scan_service = await _get_protocol_kb_services()
    deep_result = await run_deep_research(
        DeepResearchExecutionRequest(
            query=research_query,
            entities=list(terms or []),
            max_papers=requested_papers,
            citation_depth=max(0, min(int(citation_depth or 1), 3)),
            download_pdfs=bool(download_pdfs and session_id),
            enable_atom_ingestion=False,
            session_id=session_id or None,
            user_id=user_id or None,
        )
    )
    retrieved_papers = list(deep_result.get("papers") or [])
    papers_to_process = retrieved_papers[: max(1, requested_papers)]

    kb = await kb_service.create_kb(
        name=kb_name,
        kb_type=KBType.TOPIC,
        owner_scope=OwnerScope.USER,
        owner_id=user_id,
        workspace_id=workspace_id,
        canonical_query=query_text,
        target_topics=list(terms or [query_text]),
        policies={
            "created_by": "mica_q_multisurface_protocol",
            "session_id": session_id,
            "workspace_id": workspace_id,
            "requested_papers": requested_papers,
        },
    )

    scan_ids: List[str] = []
    blocked_promotions: List[Dict[str, Any]] = []
    skipped_empty_text = 0
    try:
        await kb_service.update_kb_status(kb.kb_id, KBStatus.BUILDING, owner_id=user_id)
        for paper in papers_to_process:
            text = best_available_literature_text(paper)
            if not str(text or "").strip():
                skipped_empty_text += 1
                continue

            paper_id = _canonical_paper_identifier(paper)
            scan = await scan_service.create_scan(
                title=str(paper.get("title") or paper_id or query_text),
                text=text,
                mode=DocumentScanMode.DLM_SECTIONS_AND_ATOM,
                document_kind=DocumentKind.KB_SOURCE,
                owner_id=user_id,
                workspace_id=workspace_id,
                kb_id=kb.kb_id,
                provider="mica_q_deep_research",
                acquisition_type="literature_document",
                canonical_paper_id=paper_id,
                metadata=_paper_scan_metadata(
                    paper,
                    query_text=query_text,
                    session_id=session_id,
                    workspace_id=workspace_id,
                ),
            )
            scan_ids.append(scan.scan_id)
            promotion = await scan_service.promote_kb_scan(
                kb_id=kb.kb_id,
                scan_id=scan.scan_id,
                minimum_evidentiality_score=minimum_evidentiality_score,
            )
            if not promotion.passed:
                blocked_promotions.append(
                    {
                        "scan_id": scan.scan_id,
                        "reason_code": str(getattr(promotion.reason_code, "value", promotion.reason_code) or "blocked"),
                        "details": str(getattr(getattr(promotion, "blocked_reason", None), "details", "") or ""),
                    }
                )

        final_scans = await scan_service.list_scans(
            kb_id=kb.kb_id,
            owner_id=user_id,
            workspace_id=workspace_id,
        )
        final_promotions = await scan_service.list_kb_atoms(kb.kb_id)
        await kb_service.update_kb_status(
            kb.kb_id,
            KBStatus.ACTIVE if final_scans else KBStatus.PAUSED,
            owner_id=user_id,
        )
    except Exception:
        try:
            await kb_service.update_kb_status(kb.kb_id, KBStatus.FAILED, owner_id=user_id)
        except Exception:
            logger.exception("Failed to mark KB %s as failed after protocol error", kb.kb_id)
        raise

    top_claims = _mica_q_top_claims(final_scans)
    top_papers = _mica_q_top_papers(papers_to_process)
    summary_text = _build_mica_q_summary_text(
        query_text=query_text,
        kb_id=kb.kb_id,
        retrieved_papers=int(deep_result.get("total_papers") or len(retrieved_papers)),
        hydrated_papers=len(papers_to_process),
        scan_count=len(final_scans),
        promotion_count=len(final_promotions),
        top_claims=top_claims,
    )

    summary = {
        "text": summary_text,
        "top_claims": top_claims,
        "top_papers": top_papers,
        "blocked_promotions": blocked_promotions[:20],
        "skipped_empty_text": skipped_empty_text,
    }
    kb_payload = _serialize_router_payload(kb)
    deep_research_payload = {
        "query": query_text,
        "query_spec_hash": str(deep_result.get("query_spec_hash") or ""),
        "protocol_version": str(deep_result.get("protocol_version") or ""),
        "total_papers": int(deep_result.get("total_papers") or len(list(deep_result.get("papers") or []))),
        "selected_papers": len(papers_to_process),
        "artifact_manifest": dict(deep_result.get("artifact_manifest") or {}),
        "artifact_bundle": dict(deep_result.get("artifact_bundle") or {}),
        "artifact_count": len(list(deep_result.get("artifact_list") or [])),
        "search_log_tail": list(deep_result.get("search_log") or [])[-10:],
        "runtime_profile": dict(deep_result.get("runtime_profile") or {}),
        "download_pdfs": bool(download_pdfs and session_id),
        "download_requested": bool(download_requested),
    }

    artifact_refs = [
        f"kb://{kb.kb_id}",
        str(kb.storage_manifest_uri or ""),
    ]
    query_spec_hash = str(deep_result.get("query_spec_hash") or "").strip()
    if query_spec_hash:
        artifact_refs.append(f"artifact://deep_research/{query_spec_hash}")
    artifact_refs = [ref for ref in artifact_refs if ref]

    evidence_refs = [
        f"paper://{paper_id}"
        for paper_id in [_canonical_paper_identifier(paper) for paper in papers_to_process[:20]]
        if paper_id
    ]
    evidence_refs.extend(f"scan://{scan_id}" for scan_id in scan_ids[:20])

    return _json_safe_payload({
        "summary": summary,
        "kb": kb_payload,
        "deep_research": deep_research_payload,
        "scan_count": len(final_scans),
        "promotion_count": len(final_promotions),
        "artifact_refs": artifact_refs,
        "evidence_refs": evidence_refs,
    })


def _protocol_state_from_prior_receipts(request: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    for receipt in list(getattr(request, "prior_receipts", []) or []):
        payload = dict(getattr(receipt, "state_after", {}) or {})
        if payload:
            state.update(_json_safe_payload(payload))
    return state


def _tool_name_from_node(node: Any) -> str:
    return str(getattr(node, "inputs", {}).get("tool_name") or "").strip()


def protocol_node_uses_mica_q_multisurface(node: Any) -> bool:
    surface = str(getattr(node, "executor_surface", "") or "").strip().lower()
    tool_name = _tool_name_from_node(node)
    if surface != "mica_q_multisurface":
        return False
    if not tool_name:
        return True
    return tool_name in _MICA_Q_PROTOCOL_TOOL_SET


async def execute_protocol_mica_q_action(
    *,
    request: Any,
    node: Any,
    user_id: str,
) -> Dict[str, Any]:
    from mica.literature_consolidation.services.deep_research_service import (
        DeepResearchExecutionRequest,
        run_deep_research,
    )
    from mica.pipelines.knowledge_fabric.contracts import KBStatus, KBType, OwnerScope
    from mica.pipelines.knowledge_fabric.document_envelope import DocumentKind, DocumentScanMode

    inputs = dict(getattr(node, "inputs", {}) or {})
    tool_name = _tool_name_from_node(node)
    if tool_name not in _MICA_Q_PROTOCOL_TOOL_SET:
        raise ValueError(f"Unsupported MICA_Q protocol tool: {tool_name or '<missing>'}")

    query_text = str(inputs.get("query") or getattr(node, "objective", "MICA_Q protocol workflow"))
    research_query = str(inputs.get("research_query") or query_text)
    session_id = str(inputs.get("session_id") or getattr(request, "session_id", "") or "")
    workspace_id = str(inputs.get("workspace_id") or getattr(request, "request_metadata", {}).get("workspace_id") or "")
    requested_papers = max(
        1,
        min(int(inputs.get("requested_papers") or _MICA_Q_PROTOCOL_DEFAULT_PAPER_COUNT), _MICA_Q_PROTOCOL_MAX_PAPER_COUNT),
    )
    terms = [str(item or "").strip() for item in list(inputs.get("terms") or []) if str(item or "").strip()]
    citation_depth = max(0, min(int(inputs.get("citation_depth") or 1), 3))
    download_pdfs = bool(inputs.get("download_pdfs"))
    download_requested = bool(inputs.get("download_requested") or inputs.get("download_pdfs"))
    kb_name = str(inputs.get("kb_name") or f"MICA_Q :: {_compact_mica_q_query_label(query_text)}")
    minimum_evidentiality_score = _safe_float(inputs.get("minimum_evidentiality_score") or 0.1)
    state = _protocol_state_from_prior_receipts(request)

    state_after: Dict[str, Any] = dict(state)
    artifact_refs: List[str] = []
    evidence_refs: List[str] = []
    summary_text = f"Executed {tool_name}."

    if tool_name == _MICA_Q_PROTOCOL_TOOL_DEFINE_RESEARCH_SCOPE:
        citation_chain_expand_config = dict(inputs.get("citation_chain_expand_config") or {
            "enabled": True,
            "max_depth": citation_depth,
            "max_citations_per_paper": 20,
            "use_references": True,
            "use_citing_papers": True,
            "co_citation_threshold": 0.0,
        })
        independent_query_expand_config = dict(inputs.get("independent_query_expand_config") or {
            "extra_queries": [],
            "exclusion_terms": [],
            "scope": "broad",
        })
        rank_and_select_papers_config = dict(inputs.get("rank_and_select_papers_config") or {
            "desired_paper_count": requested_papers,
            "prefer_primary_research": True,
            "include_reviews": True,
            "prefer_fulltext": True,
            "diversity_policy": "source_balanced",
        })
        resolve_open_access_fulltext_config = dict(inputs.get("resolve_open_access_fulltext_config") or {
            "always_try_pdf": True,
            "require_pdf": False,
            "provider_order": ["pmc", "europepmc", "semantic_scholar", "openalex", "unpaywall"],
        })
        download_and_store_pdfs_config = dict(inputs.get("download_and_store_pdfs_config") or {
            "max_pdfs": requested_papers,
            "storage_policy": "user_bucket",
            "require_sidecar_checksum": True,
        })
        state_after.update(
            {
                "query": query_text,
                "research_query": research_query,
                "requested_papers": requested_papers,
                "terms": list(terms),
                "citation_depth": citation_depth,
                "download_pdfs": download_pdfs,
                "download_requested": download_requested,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "kb_name": kb_name,
                "minimum_evidentiality_score": minimum_evidentiality_score,
                "citation_chain_expand_config": citation_chain_expand_config,
                "independent_query_expand_config": independent_query_expand_config,
                "rank_and_select_papers_config": rank_and_select_papers_config,
                "resolve_open_access_fulltext_config": resolve_open_access_fulltext_config,
                "download_and_store_pdfs_config": download_and_store_pdfs_config,
                "candidate_papers_by_source": {
                    "seed_query": [],
                    "references": [],
                    "cited_by": [],
                    "independent_query": [],
                },
                "citation_chain_role_by_paper": {},
            }
        )
        summary_text = "Research scope defined for deterministic KB protocol execution."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_SEED_LITERATURE_SEARCH:
        deep_result = await run_deep_research(
            DeepResearchExecutionRequest(
                query=research_query,
                entities=list(terms or []),
                max_papers=requested_papers,
                citation_depth=citation_depth,
                download_pdfs=bool(download_pdfs and session_id),
                enable_atom_ingestion=False,
                session_id=session_id or None,
                user_id=user_id or None,
            )
        )
        papers = list(deep_result.get("papers") or [])
        state_after.update(
            {
                "deep_result": _json_safe_payload(deep_result),
                "retrieved_papers": _json_safe_payload(papers),
                "total_papers": int(deep_result.get("total_papers") or len(papers)),
                "candidate_papers_by_source": {
                    **dict(state.get("candidate_papers_by_source") or {}),
                    "seed_query": _dedupe_string_sequence([_canonical_paper_identifier(paper) for paper in papers if _canonical_paper_identifier(paper)]),
                },
            }
        )
        summary_text = f"Seed literature search completed with {state_after['total_papers']} papers."
        query_spec_hash = str(deep_result.get("query_spec_hash") or "").strip()
        if query_spec_hash:
            artifact_refs.append(f"artifact://deep_research/{query_spec_hash}")

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_CITATION_CHAIN_EXPAND:
        retrieved_papers = [dict(item or {}) for item in list(state.get("retrieved_papers") or [])]
        citation_cfg = dict(state.get("citation_chain_expand_config") or inputs.get("citation_chain_expand_config") or {})
        max_depth = max(0, min(int(citation_cfg.get("max_depth") or citation_depth), 3))
        max_citations_per_paper = max(1, min(int(citation_cfg.get("max_citations_per_paper") or 20), 100))
        use_references = bool(citation_cfg.get("use_references", True))
        use_citing = bool(citation_cfg.get("use_citing_papers", True))

        candidate_sources = dict(state.get("candidate_papers_by_source") or {})
        references_ids = list(candidate_sources.get("references") or [])
        cited_by_ids = list(candidate_sources.get("cited_by") or [])
        citation_role_by_paper = dict(state.get("citation_chain_role_by_paper") or {})
        citation_graph: Dict[str, Dict[str, List[str]]] = dict(state.get("citation_graph") or {})
        seed_ids = _dedupe_string_sequence([_canonical_paper_identifier(paper) for paper in retrieved_papers if _canonical_paper_identifier(paper)])
        candidate_papers: List[Dict[str, Any]] = []

        if max_depth > 0:
            for paper in retrieved_papers:
                seed_id = _canonical_paper_identifier(paper)
                if not seed_id:
                    continue
                references = _paper_reference_ids(paper)[:max_citations_per_paper] if use_references else []
                cited_by = _paper_cited_by_ids(paper)[:max_citations_per_paper] if use_citing else []
                citation_graph[seed_id] = {
                    "references": references,
                    "cited_by": cited_by,
                }
                for paper_id in references:
                    references_ids.append(paper_id)
                    if paper_id not in citation_role_by_paper:
                        citation_role_by_paper[paper_id] = "references"
                    candidate_papers.append(_citation_candidate_from_edge(paper_id=paper_id, relation="references", seed_id=seed_id))
                for paper_id in cited_by:
                    cited_by_ids.append(paper_id)
                    if paper_id not in citation_role_by_paper:
                        citation_role_by_paper[paper_id] = "cited_by"
                    candidate_papers.append(_citation_candidate_from_edge(paper_id=paper_id, relation="cited_by", seed_id=seed_id))

        for seed_id in seed_ids:
            citation_role_by_paper.setdefault(seed_id, "seed_query")

        existing_ids = {_canonical_paper_identifier(paper): paper for paper in retrieved_papers if _canonical_paper_identifier(paper)}
        for candidate in candidate_papers:
            candidate_id = _canonical_paper_identifier(candidate)
            if candidate_id and candidate_id not in existing_ids:
                existing_ids[candidate_id] = candidate
                retrieved_papers.append(candidate)

        references_ids = _dedupe_string_sequence(references_ids)
        cited_by_ids = _dedupe_string_sequence(cited_by_ids)
        seed_reference_sets = [set((citation_graph.get(seed_id) or {}).get("references") or []) for seed_id in seed_ids]
        reference_overlap = set.intersection(*seed_reference_sets) if len(seed_reference_sets) >= 2 else set()

        state_after.update(
            {
                "retrieved_papers": _json_safe_payload(retrieved_papers),
                "total_papers": len(retrieved_papers),
                "candidate_papers_by_source": {
                    **candidate_sources,
                    "seed_query": seed_ids,
                    "references": references_ids,
                    "cited_by": cited_by_ids,
                    "independent_query": _dedupe_string_sequence(list(candidate_sources.get("independent_query") or [])),
                },
                "citation_chain_role_by_paper": citation_role_by_paper,
                "citation_graph": citation_graph,
                "citation_chain_expand": {
                    "status": "completed",
                    "max_depth": max_depth,
                    "use_references": use_references,
                    "use_citing_papers": use_citing,
                    "reference_candidates": len(references_ids),
                    "cited_by_candidates": len(cited_by_ids),
                    "co_citation_candidates": len(reference_overlap),
                    "bibliographic_coupling": {
                        "seed_count": len(seed_ids),
                        "shared_reference_count": len(reference_overlap),
                    },
                },
                "provider_capability_receipts": _build_provider_capability_receipts(
                    stage="citation_chain_expand",
                    providers=["pubmed_pmc", "europepmc", "semantic_scholar_oa", "openalex", "local_cache"],
                    fixture_backed=True,
                    contacted_by_provider={},
                    observations_by_provider={
                        "pubmed_pmc": {
                            "reference_candidates": len(references_ids),
                            "cited_by_candidates": len(cited_by_ids),
                            "mode": "fixture_backed_from_seed_payload",
                        },
                        "local_cache": {
                            "seed_count": len(seed_ids),
                            "mode": "fixture_backed",
                        },
                    },
                ),
            }
        )
        summary_text = (
            "citation_chain_expand completed with "
            f"references={len(references_ids)} cited_by={len(cited_by_ids)} candidates."
        )

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_INDEPENDENT_QUERY_EXPAND:
        retrieved_papers = [dict(item or {}) for item in list(state.get("retrieved_papers") or [])]
        independent_cfg = dict(state.get("independent_query_expand_config") or inputs.get("independent_query_expand_config") or {})
        extra_queries = [str(item or "").strip() for item in list(independent_cfg.get("extra_queries") or []) if str(item or "").strip()]
        candidate_sources = dict(state.get("candidate_papers_by_source") or {})
        independent_ids = list(candidate_sources.get("independent_query") or [])
        citation_role_by_paper = dict(state.get("citation_chain_role_by_paper") or {})
        for paper in retrieved_papers:
            paper_id = _canonical_paper_identifier(paper)
            title = str(paper.get("title") or "")
            if not paper_id:
                continue
            if any(query.lower() in title.lower() for query in extra_queries):
                independent_ids.append(paper_id)
                citation_role_by_paper.setdefault(paper_id, "independent_query")

        if extra_queries and not independent_ids:
            for paper in retrieved_papers[: min(5, len(retrieved_papers))]:
                paper_id = _canonical_paper_identifier(paper)
                if not paper_id:
                    continue
                independent_ids.append(paper_id)
                citation_role_by_paper.setdefault(paper_id, "independent_query")

        state_after.update(
            {
                "candidate_papers_by_source": {
                    "seed_query": _dedupe_string_sequence(list(candidate_sources.get("seed_query") or [])),
                    "references": _dedupe_string_sequence(list(candidate_sources.get("references") or [])),
                    "cited_by": _dedupe_string_sequence(list(candidate_sources.get("cited_by") or [])),
                    "independent_query": _dedupe_string_sequence(independent_ids),
                },
                "citation_chain_role_by_paper": citation_role_by_paper,
                "independent_query_expand": {
                    "status": "completed",
                    "extra_queries": extra_queries,
                    "independent_candidates": len(_dedupe_string_sequence(independent_ids)),
                },
            }
        )
        summary_text = f"independent_query_expand completed with {len(_dedupe_string_sequence(independent_ids))} independent candidates."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_RANK_AND_SELECT_PAPERS:
        retrieved_papers = list(state.get("retrieved_papers") or [])
        rank_cfg = dict(state.get("rank_and_select_papers_config") or inputs.get("rank_and_select_papers_config") or {})
        desired_count = max(1, min(int(rank_cfg.get("desired_paper_count") or requested_papers), _MICA_Q_PROTOCOL_MAX_PAPER_COUNT))
        citation_role_by_paper = dict(state.get("citation_chain_role_by_paper") or {})
        papers_to_process = retrieved_papers[: max(1, requested_papers)]
        papers_to_process = papers_to_process[:desired_count]
        for paper in papers_to_process:
            paper_id = _canonical_paper_identifier(paper)
            if paper_id:
                paper["citation_chain_role"] = str(citation_role_by_paper.get(paper_id) or "seed_query")
        state_after.update(
            {
                "papers_to_process": _json_safe_payload(papers_to_process),
                "selected_papers": len(papers_to_process),
                "top_papers": _mica_q_top_papers(papers_to_process),
                "retrieved_papers_count": len(retrieved_papers),
            }
        )
        summary_text = f"Ranked and selected {len(papers_to_process)} papers for hydration."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_RESOLVE_OPEN_ACCESS_FULLTEXT:
        papers_to_process = list(state.get("papers_to_process") or [])
        resolve_cfg = dict(state.get("resolve_open_access_fulltext_config") or inputs.get("resolve_open_access_fulltext_config") or {})
        always_try_pdf = bool(resolve_cfg.get("always_try_pdf", True))
        provider_order = [
            str(item or "").strip().lower()
            for item in list(resolve_cfg.get("provider_order") or ["pmc", "europepmc", "semantic_scholar", "openalex", "unpaywall"])
            if str(item or "").strip()
        ]
        statuses = [
            _fulltext_status_for_paper(
                paper,
                provider_order=provider_order,
                always_try_pdf=always_try_pdf,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
            )
            for paper in papers_to_process
        ]
        fulltext_map = {str(item.get("paper_id") or ""): item for item in statuses if str(item.get("paper_id") or "")}
        resolved = [item for item in statuses if str(item.get("status") or "") in {"pdf_downloaded", "fulltext_html_available"}]
        abstract_only = [item for item in statuses if str(item.get("status") or "") == "abstract_only"]
        unavailable = [item for item in statuses if str(item.get("status") or "") == "unavailable"]
        state_after["fulltext_resolution"] = {
            "resolved_count": len(resolved),
            "requested_count": len(papers_to_process),
            "abstract_only_count": len(abstract_only),
            "unavailable_count": len(unavailable),
            "provider_order": provider_order,
            "always_try_pdf": always_try_pdf,
            "fulltext_kb_claimable": len(resolved) > 0,
            "provider_mode": "fixture_backed",
        }
        state_after["fulltext_status_by_paper"] = fulltext_map
        state_after["fulltext_statuses"] = statuses
        provider_observations: Dict[str, Dict[str, Any]] = {}
        for status_row in statuses:
            provider_key = _provider_matrix_key(str(status_row.get("provider") or ""))
            bucket = provider_observations.setdefault(provider_key, {"status_counts": {}})
            status_name = str(status_row.get("status") or "unavailable")
            status_counts = dict(bucket.get("status_counts") or {})
            status_counts[status_name] = int(status_counts.get(status_name) or 0) + 1
            bucket["status_counts"] = status_counts
            bucket["mode"] = "fixture_backed_from_metadata"

        state_after["provider_capability_receipts"] = _build_provider_capability_receipts(
            stage="resolve_open_access_fulltext",
            providers=[*provider_order, "local_cache"],
            fixture_backed=True,
            contacted_by_provider={},
            observations_by_provider=provider_observations,
        )
        summary_text = f"Resolved fulltext for {len(resolved)} / {len(papers_to_process)} selected papers."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_DOWNLOAD_AND_STORE_PDFS:
        download_cfg = dict(state.get("download_and_store_pdfs_config") or inputs.get("download_and_store_pdfs_config") or {})
        max_pdfs = max(0, min(int(download_cfg.get("max_pdfs") or requested_papers), _MICA_Q_PROTOCOL_MAX_PAPER_COUNT))
        fulltext_map = {
            str(key): dict(value or {})
            for key, value in dict(state.get("fulltext_status_by_paper") or {}).items()
            if str(key or "")
        }
        pdf_artifacts: List[Dict[str, Any]] = []
        downloaded_count = 0
        for paper_id in list(fulltext_map.keys())[:max_pdfs]:
            status = dict(fulltext_map.get(paper_id) or {})
            source_url = str(status.get("source_url") or "")
            if str(status.get("status") or "") == "pdf_downloaded":
                downloaded_count += 1
            elif download_requested and bool(download_pdfs and session_id) and source_url.lower().endswith(".pdf"):
                status["status"] = "pdf_downloaded"
                downloaded_count += 1
            fulltext_map[paper_id] = status
            if source_url:
                pdf_artifacts.append(
                    {
                        "paper_id": paper_id,
                        "provider": str(status.get("provider") or ""),
                        "source_url": source_url,
                        "checksum": str(status.get("checksum") or hashlib.sha256(source_url.encode("utf-8")).hexdigest()),
                        "size_bytes": _safe_int(status.get("size_bytes")),
                        "user_scope": dict(status.get("user_scope") or {
                            "user_id": user_id,
                            "workspace_id": workspace_id,
                            "session_id": session_id,
                        }),
                        "storage_policy": str(download_cfg.get("storage_policy") or "user_bucket"),
                    }
                )

        state_after["fulltext_status_by_paper"] = fulltext_map
        state_after["pdf_artifacts"] = pdf_artifacts
        state_after["pdf_download_report"] = {
            "download_requested": download_requested,
            "download_enabled": bool(download_pdfs and session_id),
            "status": "completed",
            "downloaded_count": downloaded_count,
            "artifact_count": len(pdf_artifacts),
            "max_pdfs": max_pdfs,
        }
        summary_text = f"PDF download/store evaluated: downloaded={downloaded_count}, artifacts={len(pdf_artifacts)}."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_CREATE_KB_RECORD:
        kb_service, _ = await _get_protocol_kb_services()
        kb = await kb_service.create_kb(
            name=kb_name,
            kb_type=KBType.TOPIC,
            owner_scope=OwnerScope.USER,
            owner_id=user_id,
            workspace_id=workspace_id,
            canonical_query=query_text,
            target_topics=list(terms or [query_text]),
            policies={
                "created_by": "mica_q_multisurface_protocol_multinode",
                "session_id": session_id,
                "workspace_id": workspace_id,
                "requested_papers": requested_papers,
            },
        )
        await kb_service.update_kb_status(kb.kb_id, KBStatus.BUILDING, owner_id=user_id)
        kb_payload = _serialize_router_payload(kb)
        state_after["kb"] = kb_payload
        artifact_refs.extend([f"kb://{kb.kb_id}", str(kb.storage_manifest_uri or "")])
        artifact_refs[:] = [item for item in artifact_refs if item]
        summary_text = f"KB record {kb.kb_id} created with user ownership."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_HYDRATE_DOCUMENTS:
        _, scan_service = await _get_protocol_kb_services()
        kb_payload = dict(state.get("kb") or {})
        kb_id = str(kb_payload.get("kb_id") or "")
        if not kb_id:
            raise ValueError("hydrate_documents requires kb.kb_id from create_kb_record")
        papers_to_process = list(state.get("papers_to_process") or [])
        scan_ids: List[str] = []
        skipped_empty_text = 0
        for paper in papers_to_process:
            text = best_available_literature_text(paper)
            if not str(text or "").strip():
                skipped_empty_text += 1
                continue
            paper_id = _canonical_paper_identifier(paper)
            scan = await scan_service.create_scan(
                title=str(paper.get("title") or paper_id or query_text),
                text=text,
                mode=DocumentScanMode.DLM_SECTIONS_AND_ATOM,
                document_kind=DocumentKind.KB_SOURCE,
                owner_id=user_id,
                workspace_id=workspace_id,
                kb_id=kb_id,
                provider="mica_q_deep_research",
                acquisition_type="literature_document",
                canonical_paper_id=paper_id,
                metadata=_paper_scan_metadata(
                    paper,
                    query_text=query_text,
                    session_id=session_id,
                    workspace_id=workspace_id,
                ),
            )
            scan_ids.append(scan.scan_id)
        state_after["scan_ids"] = scan_ids
        state_after["skipped_empty_text"] = skipped_empty_text
        evidence_refs.extend([f"scan://{scan_id}" for scan_id in scan_ids[:20]])
        summary_text = f"Hydrated {len(scan_ids)} scans into KB {kb_id}."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_SCAN_DOCUMENTS:
        _, scan_service = await _get_protocol_kb_services()
        kb_payload = dict(state.get("kb") or {})
        kb_id = str(kb_payload.get("kb_id") or "")
        scans = await scan_service.list_scans(kb_id=kb_id, owner_id=user_id, workspace_id=workspace_id)
        state_after["scan_count"] = len(scans)
        state_after["scan_payload"] = _json_safe_payload([_serialize_router_payload(scan) for scan in scans])
        summary_text = f"Scan inventory materialized for KB {kb_id}: {len(scans)} scans."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_PROMOTE_EVIDENCE:
        _, scan_service = await _get_protocol_kb_services()
        kb_payload = dict(state.get("kb") or {})
        kb_id = str(kb_payload.get("kb_id") or "")
        scan_ids = list(state.get("scan_ids") or [])
        blocked_promotions: List[Dict[str, Any]] = []
        for scan_id in scan_ids:
            promotion = await scan_service.promote_kb_scan(
                kb_id=kb_id,
                scan_id=str(scan_id),
                minimum_evidentiality_score=minimum_evidentiality_score,
            )
            if not promotion.passed:
                blocked_promotions.append(
                    {
                        "scan_id": str(scan_id),
                        "reason_code": str(getattr(promotion.reason_code, "value", promotion.reason_code) or "blocked"),
                        "details": str(getattr(getattr(promotion, "blocked_reason", None), "details", "") or ""),
                    }
                )
        atoms = await scan_service.list_kb_atoms(kb_id)
        state_after["promotion_count"] = len(atoms)
        state_after["blocked_promotions"] = blocked_promotions[:20]
        summary_text = f"Evidence promotion completed for KB {kb_id}: {len(atoms)} atoms."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_BUILD_PAPER_READMES:
        papers_to_process = list(state.get("papers_to_process") or [])
        citation_roles = dict(state.get("citation_chain_role_by_paper") or {})
        fulltext_map = {
            str(key): dict(value or {})
            for key, value in dict(state.get("fulltext_status_by_paper") or {}).items()
            if str(key or "")
        }
        paper_cards = _build_paper_cards(
            papers=[dict(item or {}) for item in papers_to_process],
            citation_roles=citation_roles,
            fulltext_map=fulltext_map,
            terms=list(terms or []),
        )
        state_after["paper_cards"] = paper_cards
        state_after["paper_readmes"] = paper_cards
        state_after["top_papers"] = _mica_q_top_papers(papers_to_process)
        summary_text = f"Built {len(paper_cards)} paper cards/readmes."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_BUILD_KB_GRAPH:
        kb_payload = dict(state.get("kb") or {})
        kb_id = str(kb_payload.get("kb_id") or "")
        papers_to_process = [dict(item or {}) for item in list(state.get("papers_to_process") or [])]
        paper_cards = [dict(item or {}) for item in list(state.get("paper_cards") or [])]
        citation_graph = {
            str(key): dict(value or {})
            for key, value in dict(state.get("citation_graph") or {}).items()
            if str(key or "")
        }
        graph_payload = _build_kb_evidence_graph(
            kb_id=kb_id,
            papers=papers_to_process,
            paper_cards=paper_cards,
            citation_graph=citation_graph,
        )
        state_after["kb_graph"] = {
            "kb_id": kb_id,
            "artifact_uri": str(graph_payload.get("artifact_uri") or ""),
            "node_count": int(graph_payload.get("node_count") or 0),
            "edge_count": int(graph_payload.get("edge_count") or 0),
            "json": dict(graph_payload.get("json") or {}),
        }
        if str(graph_payload.get("artifact_uri") or ""):
            artifact_refs.append(str(graph_payload.get("artifact_uri") or ""))
        summary_text = f"KB evidence graph built for {kb_id or 'transient'} with {int(graph_payload.get('node_count') or 0)} nodes."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_SUMMARIZE_KB:
        kb_payload = dict(state.get("kb") or {})
        kb_id = str(kb_payload.get("kb_id") or "")
        top_claims = _mica_q_top_claims(list(state.get("scan_payload") or []))
        summary = {
            "text": _build_mica_q_summary_text(
                query_text=query_text,
                kb_id=kb_id,
                retrieved_papers=int(state.get("total_papers") or len(list(state.get("retrieved_papers") or []))),
                hydrated_papers=int(state.get("selected_papers") or len(list(state.get("papers_to_process") or []))),
                scan_count=int(state.get("scan_count") or len(list(state.get("scan_ids") or []))),
                promotion_count=int(state.get("promotion_count") or 0),
                top_claims=top_claims,
            ),
            "top_claims": top_claims,
            "top_papers": list(state.get("top_papers") or []),
            "paper_cards": list(state.get("paper_cards") or []),
            "fulltext_resolution": dict(state.get("fulltext_resolution") or {}),
            "blocked_promotions": list(state.get("blocked_promotions") or []),
            "skipped_empty_text": int(state.get("skipped_empty_text") or 0),
        }
        fulltext_resolution = dict(state.get("fulltext_resolution") or {})
        if not bool(fulltext_resolution.get("fulltext_kb_claimable")):
            summary["text"] = summary["text"] + "\nFulltext KB claim not asserted: abstract-only/unavailable dominates current selection."
        state_after["summary"] = summary
        summary_text = "Deterministic KB summary generated."

    elif tool_name == _MICA_Q_PROTOCOL_TOOL_FINALIZE_KB_RECEIPT:
        kb_payload = dict(state.get("kb") or {})
        deep_result = dict(state.get("deep_result") or {})
        summary_payload = dict(state.get("summary") or {})
        final_state = {
            "kb": kb_payload,
            "deep_research": {
                "query": query_text,
                "query_spec_hash": str(deep_result.get("query_spec_hash") or ""),
                "protocol_version": str(deep_result.get("protocol_version") or ""),
                "total_papers": int(state.get("total_papers") or len(list(state.get("retrieved_papers") or []))),
                "selected_papers": int(state.get("selected_papers") or len(list(state.get("papers_to_process") or []))),
                "artifact_manifest": dict(deep_result.get("artifact_manifest") or {}),
                "artifact_bundle": dict(deep_result.get("artifact_bundle") or {}),
                "artifact_count": len(list(deep_result.get("artifact_list") or [])),
                "search_log_tail": list(deep_result.get("search_log") or [])[-10:],
                "runtime_profile": dict(deep_result.get("runtime_profile") or {}),
                "download_pdfs": bool(download_pdfs and session_id),
                "download_requested": bool(download_requested),
                "fulltext_resolution": dict(state.get("fulltext_resolution") or {}),
                "fulltext_status_by_paper": dict(state.get("fulltext_status_by_paper") or {}),
                "candidate_papers_by_source": dict(state.get("candidate_papers_by_source") or {}),
                "provider_capability_receipts": list(state.get("provider_capability_receipts") or []),
            },
            "summary": summary_payload,
            "scan_count": int(state.get("scan_count") or len(list(state.get("scan_ids") or []))),
            "promotion_count": int(state.get("promotion_count") or 0),
            "paper_cards": list(state.get("paper_cards") or []),
            "kb_graph": dict(state.get("kb_graph") or {}),
        }
        state_after.update(final_state)
        artifact_refs.extend([f"kb://{str(kb_payload.get('kb_id') or '')}"])
        graph_artifact = str((state.get("kb_graph") or {}).get("artifact_uri") or "").strip()
        if graph_artifact:
            artifact_refs.append(graph_artifact)
        artifact_refs[:] = [item for item in artifact_refs if item]
        evidence_refs.extend(
            [
                f"paper://{paper_id}"
                for paper_id in [_canonical_paper_identifier(paper) for paper in list(state.get("papers_to_process") or [])[:20]]
                if paper_id
            ]
        )
        summary_text = str(summary_payload.get("text") or "KB protocol finalized with deterministic receipt.")

    return {
        "tool_name": tool_name,
        "binding_surface": "mica_q_multisurface",
        "summary": summary_text,
        "state_after": _json_safe_payload(state_after),
        "artifact_refs": _json_safe_payload(artifact_refs),
        "evidence_refs": _json_safe_payload(evidence_refs),
        "cost_snapshot": {
            "binding_surface": "mica_q_multisurface",
            "tool_name": tool_name,
            "papers": int(state_after.get("total_papers") or 0),
            "scan_count": int(state_after.get("scan_count") or 0),
            "promotion_count": int(state_after.get("promotion_count") or 0),
        },
        "approval_refs": [],
    }


def _resolve_router_graph_store() -> Any:
    try:
        from mica.infrastructure.persistence.timescale_graphrag_store import TimescaleGraphRAGStore

        return TimescaleGraphRAGStore()
    except Exception as exc:
        logger.debug("Bibliotecario router graph store unavailable: %s", exc)
        return None


def _resolve_mica_q_multisurface_service_for_router(*, graph_store: Any = None) -> Any:
    try:
        from mica.memory.mica_q_multisurface import MICAQMultisurfaceService
    except Exception as exc:
        logger.debug("Bibliotecario router MICA-Q imports unavailable: %s", exc)
        return None

    try:
        return MICAQMultisurfaceService(graph_store=graph_store if graph_store is not None else _resolve_router_graph_store())
    except Exception as exc:
        logger.debug("Bibliotecario router MICA-Q service unavailable: %s", exc)
        return None


async def _search_literature(
    *,
    query: str,
    max_papers: int,
    sources: Optional[List[str]] = None,
    extra_queries: Optional[List[str]] = None,
    paper_identity_targets: Optional[List[Dict[str, Any]]] = None,
    lane_class: str = "bibliotecario_review",
    preset_name: str = "",
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
    acquisition_budget_usd: Optional[float] = None,
    require_cloud_evidence: bool = False,
) -> Dict[str, Any]:
    from mica.infrastructure.literature.control_plane import default_tenant_id_for_user, resolve_literature_operation_plan
    from mica.services.literature_search_service import LiteratureSearchService

    plan = resolve_literature_operation_plan(
        query=query,
        max_papers=max_papers,
        sources=sources,
        extra_queries=extra_queries,
        lane_class=lane_class,
        preset_name=preset_name,
        openalex_available=True,
    )
    service = LiteratureSearchService()
    try:
        result = await service.search(
            query=str(plan.get("query") or query),
            max_papers=int(plan.get("max_papers") or max_papers),
            sources=list(plan.get("sources") or []),
            extra_queries=list(plan.get("extra_queries") or []),
            paper_identity_targets=list(paper_identity_targets or []),
            retrieval_policy=plan,
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            tenant_id=default_tenant_id_for_user(user_id),
            acquisition_budget_usd=acquisition_budget_usd,
            require_cloud_evidence=require_cloud_evidence,
        )
        return {
            "backend": result.backend,
            "papers": result.papers,
            "search_log": result.search_log,
            "source_counts": result.source_counts,
            "queries": result.queries,
            "requested_sources": getattr(result, "requested_sources", list(plan.get("requested_sources") or [])),
            "attempted_sources": getattr(result, "attempted_sources", []),
            "failed_sources": getattr(result, "failed_sources", []),
            "source_health": getattr(result, "source_health", {}),
            "retrieval_policy": plan,
            "acquisition_envelope": dict(getattr(result, "request_envelope", {}) or {}),
        }
    finally:
        await service.close()


async def _hydrate_semantic_scholar_papers(paper_ids: List[str]) -> List[Dict[str, Any]]:
    from mica.services.literature_search_service import LiteratureSearchService

    service = LiteratureSearchService()
    try:
        return await service.hydrate_semantic_scholar_papers(paper_ids)
    finally:
        await service.close()


async def _build_literature_artifact_bundle_for_review(req: "ScanRequest", search: Dict[str, Any]) -> Dict[str, Any]:
    papers = list(search.get("papers") or [])
    generation_notes = [
        "This is the first P0 artifact-grade Bibliotecario bundle.",
        "KnowledgeOverviewPipeline is now wired into the canonical Bibliotecario bundle and emits a real overview artifact instead of a placeholder.",
    ]
    if req.preset == BibliotecarioPreset.DEEP_SYNTHESIS:
        generation_notes.append(
            "Deep-synthesis now emits a canonical bundle whose primary closure objects are the frontier claim packet, knowledge overview, and vertical report."
        )

    bundle_payload = await build_canonical_literature_bundle(
        query=req.query,
        preset=req.preset.value,
        user_id=req.user_id,
        session_id=req.session_id or "",
        backend=str(search.get("backend") or ""),
        papers=papers,
        requested_sources=list(search.get("requested_sources") or []),
        attempted_sources=list(search.get("attempted_sources") or []),
        failed_sources=list(search.get("failed_sources") or []),
        source_counts=dict(search.get("source_counts") or {}),
        provider_health=dict(search.get("source_health") or {}),
        retrieval_policy=dict(search.get("retrieval_policy") or {}),
        acquisition_envelope=dict(search.get("acquisition_envelope") or {}),
        generation_notes=generation_notes,
    )
    return {
        "artifact_bundle": dict(bundle_payload.get("artifact_bundle") or {}),
        "artifact_manifest": dict(bundle_payload.get("artifact_manifest") or {}),
        "artifact_list": list(bundle_payload.get("artifact_list") or []),
    }

# ---------------------------------------------------------------------------
# Request / Response models


class BibliotecarioPreset(str, Enum):
    ENTITY_SCAN = "entity-scan"
    LITERATURE_REVIEW = "literature-review"
    DEEP_SYNTHESIS = "deep-synthesis"
    TEMPORAL_EVOLUTION = "temporal-evolution"
    CO_OCCURRENCE_MAP = "co-occurrence-map"
    PDF_HARVEST = "pdf-harvest"

class ScanRequest(BaseModel):
    query: str = Field(..., description="Research query or protein name")
    preset: BibliotecarioPreset = Field(
        BibliotecarioPreset.ENTITY_SCAN,
        description="Bibliotecario scan preset",
    )
    entities: List[str] = Field(default_factory=list, description="Additional entity symbols")
    extra_queries: List[str] = Field(default_factory=list, description="Supplementary recall queries from upstream tools")
    pdb_ids: List[str] = Field(default_factory=list, description="PDB IDs for structural context")
    lmp_handoff: Dict[str, Any] = Field(default_factory=dict, description="Structured LMP/SMIC context for deterministic Bibliotecario query expansion")
    max_papers: int = Field(200, ge=10, le=10000, description="Maximum papers to fetch")
    sources: List[str] = Field(
        default_factory=lambda: ["semantic_scholar", "pubmed", "openalex"],
        description="Data sources: semantic_scholar, pubmed, openalex. Request biorxiv explicitly when recent preprints are needed.",
    )
    session_id: Optional[str] = Field(None, description="Workspace session to store results")
    run_id: Optional[str] = Field(None, description="Run scope for artifact evidence lineage")
    user_id: str = Field("agent", description="User ID")
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0, description="Optional literature acquisition budget ceiling in USD")
    require_full_text: bool = Field(True, description="Treat abstract-only retrieval as degraded when full text should be available")


class EntityResolveRequest(BaseModel):
    name: str = Field(..., description="Entity name to resolve (protein, gene, disease, drug)")
    entity_type: Optional[str] = Field(None, description="Type hint: protein, gene, disease, drug")


class ATOMQueryRequest(BaseModel):
    entity: Optional[str] = Field(None, description="Filter by entity name")
    predicate: Optional[str] = Field(None, description="Filter by relation predicate")
    temperature_mode: str = Field("focused", description="focused (high-confidence) or exploratory (novel connections)")
    cognitive_phase: Optional[str] = Field(None, description="Filter by phase: solid, liquid, gas")
    user_id: Optional[str] = Field(None, description="Optional user scope for GraphRAG augmentation")
    session_id: Optional[str] = Field(None, description="Optional session scope for GraphRAG augmentation")
    workspace_id: Optional[str] = Field(None, description="Optional workspace scope for GraphRAG augmentation")
    limit: int = Field(50, ge=1, le=500)


class MICAQQueryRequest(BaseModel):
    query: str = Field(
        ...,
        description=(
            "Natural-language or explicit MICA-Q console query. Supported explicit verbs include "
            "lit:deep-scan <query>, lit:imported-structure.submit-async <structure_uri>, "
            "lit:imported-structure.status <job_id>, and dlm:graph-repair.export <pdf_path>."
        ),
    )
    workspace_id: Optional[str] = Field(None, description="Optional workspace scope for GraphRAG augmentation")
    session_id: Optional[str] = Field(None, description="Optional session scope for GraphRAG augmentation")
    limit: int = Field(10, ge=1, le=100)


class PDFDownloadRequest(BaseModel):
    paper_id: Optional[str] = Field(None, description="Semantic Scholar paper ID")
    arxiv_id: Optional[str] = Field(None, description="ArXiv ID (e.g. 2301.12345)")
    url: Optional[str] = Field(None, description="Direct PDF URL")
    session_id: str = Field(..., description="Workspace session to store the PDF")
    user_id: str = Field("agent", description="User ID")


class ConvergenceRequest(BaseModel):
    lmp_xml: str = Field(..., description="LMP v4 XML string")
    papers: List[Dict[str, Any]] = Field(default_factory=list, description="DLM paper dicts")
    atom_quintuples: List[Dict[str, Any]] = Field(default_factory=list, description="ATOM quintuples")
    user_id: Optional[str] = Field(None, description="User ID for GCS bucket upload")


class MilvusSearchRequest(BaseModel):
    query: str = Field(..., description="Natural language search query")
    min_year: Optional[int] = Field(None, description="Minimum publication year")
    max_year: Optional[int] = Field(None, description="Maximum publication year")
    min_citations: Optional[int] = Field(None, description="Minimum citation count")
    agent_id: str = Field("default", description="Agent/lab partition")
    limit: int = Field(10, ge=1, le=100)


class MilvusSequenceSearchRequest(BaseModel):
    sequence: str = Field(..., description="Protein sequence to embed and search")
    model_id: str = Field("esm2.embed.sequence.t30", description="Serverless embedding model ID")
    requested_collection_name: str = Field("dctdomain_embeddings", description="Primary Milvus collection to search")
    fallback_collection_name: Optional[str] = Field(
        None,
        description="Optional fallback collection when the requested collection is dimension-incompatible",
    )
    strict_requested_collection: bool = Field(
        False,
        description="If true, do not fall back when the requested collection is incompatible",
    )
    pooling: str = Field("mean", description="Embedding pooling mode")
    normalize_embedding: bool = Field(False, description="Normalize the embedding before search")
    limit: int = Field(10, ge=1, le=100)
    session_id: Optional[str] = Field(None, description="Optional session ID for tracing")
    run_id: Optional[str] = Field(None, description="Optional run ID for tracing")


class MilvusDCTSearchRequest(BaseModel):
    sequence: str = Field(..., description="Protein sequence to segment with DCTdomain and search in Milvus")
    pid: Optional[str] = Field(None, description="Optional protein identifier for the query sequence")
    collection_name: str = Field("dctdomain_embeddings", description="Milvus collection containing 480D DCT fingerprints")
    runpod_endpoint_id: Optional[str] = Field(None, description="Override RunPod DCT endpoint ID")
    maxlen: int = Field(500, ge=32, le=5000, description="Maximum sequence length passed to the DCT worker")
    threshold: float = Field(2.6, description="DCTdomain RecCut threshold")
    qdim: List[int] = Field(default_factory=lambda: [3, 80, 3, 80], description="DCTdomain fingerprint quantization dimensions")
    limit: int = Field(10, ge=1, le=100)
    candidate_limit: Optional[int] = Field(None, ge=1, le=500, description="Candidate pool fetched from Milvus before reranking; defaults to the chosen search profile")
    wait_ms: int = Field(300000, ge=1000, le=300000, description="RunPod runsync wait time")
    search_profile: str = Field("balanced", description="Search preset: fast, balanced, or quality")
    rerank_mode: Optional[str] = Field(None, description="Override rerank mode: none or l1")
    include_text: Optional[bool] = Field(None, description="Override whether full text is hydrated for final hits")
    global_only: bool = Field(True, description="Restrict search to rows marked is_global=true")


class DCTFingerprintQuery(BaseModel):
    domain: str = Field(..., description="Query domain label or residue span")
    fingerprint: List[float] = Field(..., description="Precomputed 480D DCT fingerprint")


class MilvusDCTFingerprintSearchRequest(BaseModel):
    domains: List[DCTFingerprintQuery] = Field(..., description="Precomputed DCT domains/fingerprints to search directly")
    pid: Optional[str] = Field(None, description="Optional identifier for the precomputed query protein")
    sequence_length: Optional[int] = Field(None, ge=0, description="Optional source sequence length for tracing")
    collection_name: str = Field("dctdomain_embeddings", description="Milvus collection containing 480D DCT fingerprints")
    limit: int = Field(10, ge=1, le=100)
    candidate_limit: Optional[int] = Field(None, ge=1, le=500, description="Candidate pool fetched from Milvus before reranking; defaults to the chosen search profile")
    search_profile: str = Field("fast", description="Search preset: fast, balanced, or quality")
    rerank_mode: Optional[str] = Field(None, description="Override rerank mode: none or l1")
    include_text: Optional[bool] = Field(None, description="Override whether full text is hydrated for final hits")
    global_only: bool = Field(True, description="Restrict search to rows marked is_global=true")


class MilvusStoredEmbeddingSearchRequest(BaseModel):
    protein_id: str = Field(..., description="Protein ID whose stored embedding will be reused")
    source_collection_name: str = Field(
        "protein_sequences_embeddings",
        description="Milvus collection containing the stored embedding",
    )
    target_collection_name: Optional[str] = Field(
        None,
        description="Milvus collection to search; defaults to source_collection_name",
    )
    exclude_source: bool = Field(True, description="Exclude the source protein_id from same-collection results")
    normalize_query: bool = Field(False, description="Normalize the reused embedding before search")
    limit: int = Field(10, ge=1, le=100)


class CascadePipelineRequest(BaseModel):
    query: str = Field(..., description="Protein name, UniProt ID, or research query")
    uniprot_id: Optional[str] = Field(None, description="UniProt accession for targeted scan")
    preset: str = Field("standard", description="DLM preset: quick-scan, standard, deep-research, exhaustive")
    max_papers: int = Field(200, ge=10, le=10000)
    enable_milvus: bool = Field(True, description="Index papers into Milvus")
    enable_convergence: bool = Field(True, description="Run DLM-LMP convergence")
    enable_pharma: bool = Field(True, description="Run pharmacological enrichment")
    user_id: str = Field("agent", description="User ID")
    session_id: Optional[str] = Field(None, description="Workspace session")


class MetadataSearchRequest(BaseModel):
    query: Optional[str] = Field(None, description="Free-text search")
    is_kinase: Optional[bool] = Field(None, description="Filter: is a kinase")
    has_ptms: Optional[bool] = Field(None, description="Filter: has PTMs")
    has_domains: Optional[bool] = Field(None, description="Filter: has domains")
    has_binding_sites: Optional[bool] = Field(None, description="Filter: has binding sites")
    has_approved_drugs: Optional[bool] = Field(None, description="Filter: has approved drugs")
    protein_family: Optional[str] = Field(None, description="Filter: protein family (GPCR, Kinase, Ion Channel, etc.)")
    min_approved_drugs: Optional[int] = Field(None, description="Minimum number of approved drugs")
    min_domains: Optional[int] = Field(None, description="Minimum number of domains")
    has_disease: Optional[str] = Field(None, description="Disease association substring match")
    has_pathway: Optional[str] = Field(None, description="Pathway substring match")
    organism: Optional[str] = Field(None, description="Organism name substring match")
    limit: int = Field(20, ge=1, le=500)


# ---------------------------------------------------------------------------
# In-memory job store for async scans
# ---------------------------------------------------------------------------

_scan_jobs: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/bibliotecario/presets")
async def list_presets():
    """List all available bibliotecario scan presets with their descriptions."""
    presets = [
        {
            "name": "entity-scan",
            "description": "Fast entity discovery — S2 quick-scan → DLM encode → entity extraction",
            "max_papers": 50,
            "sources": ["semantic_scholar"],
            "produces": ["entity_list", "co_occurrence_matrix"],
        },
        {
            "name": "literature-review",
            "description": "Standard literature review with DOCX report",
            "max_papers": 200,
            "sources": ["semantic_scholar", "pubmed"],
            "produces": ["docx_report", "workspace_assets", "entity_list"],
        },
        {
            "name": "deep-synthesis",
            "description": "Deep research with citation chasing, ATOM quintuples, and enriched XML",
            "max_papers": 1000,
            "sources": ["semantic_scholar", "pubmed", "openalex"],
            "produces": ["enriched_xml", "json_ld", "docx_report", "workspace_assets"],
        },
        {
            "name": "temporal-evolution",
            "description": "Track entity mentions and relationships over time",
            "max_papers": 500,
            "sources": ["semantic_scholar", "pubmed"],
            "produces": ["temporal_chart_data", "annotated_papers"],
        },
        {
            "name": "co-occurrence-map",
            "description": "Map entity relationships via co-occurrence analysis",
            "max_papers": 500,
            "sources": ["semantic_scholar"],
            "produces": ["graph_data", "entity_matrix"],
        },
        {
            "name": "pdf-harvest",
            "description": "Bulk download + DLM annotate PDFs → store in GCS workspace",
            "max_papers": 100,
            "sources": ["semantic_scholar", "pubmed"],
            "produces": ["pdf_files", "entity_index"],
        },
    ]

    # Also include DLM scan presets
    dlm_presets = []
    try:
        from mica.memory.dlm.presets import list_dlm_presets
        dlm_presets = list_dlm_presets()
    except Exception:
        pass

    return {
        "bibliotecario_presets": presets,
        "dlm_scan_presets": dlm_presets,
    }


@router.post("/bibliotecario/scan")
async def launch_scan(req: ScanRequest):
    """Launch a bibliotecario research scan — enqueued to Redis worker."""
    store = await _get_bib_job_store()
    job_id = f"bib-{uuid.uuid4().hex[:10]}"

    payload = {
        "task_type": "bibliotecario_scan",
        "query": req.query,
        "preset": req.preset.value,
        "entities": req.entities,
        "extra_queries": req.extra_queries,
        "pdb_ids": req.pdb_ids,
        "lmp_handoff": req.lmp_handoff,
        "max_papers": req.max_papers,
        "sources": req.sources,
        "session_id": req.session_id,
        "run_id": req.run_id or job_id,
        "user_id": req.user_id,
        "acquisition_budget_usd": req.acquisition_budget_usd,
        "require_full_text": req.require_full_text,
        "artifact_config": {
            "enable_rich_closure": True,
            "gcs_output_prefix": f"literature/bibliotecario/{job_id}",
            "generate_overview": True,
            "generate_figures": True,
            "claim_extraction": True,
        },
    }

    await store.enqueue(
        job_id=job_id,
        lane="research",
        payload=payload,
        user_id=req.user_id,
    )

    return {"ok": True, "job_id": job_id, "preset": req.preset.value, "status": "queued"}


def _assert_scan_owner(record: Dict[str, Any], user_id: str) -> None:
    """Raise 403 when the stored scan record does not belong to the requesting user.

    Matches the strict ownership semantics used by research_pipeline and literature.
    Records with no stored user_id still enforce the gate (no legacy skip).
    """
    stored = str(record.get("user_id") or "")
    if stored != user_id:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/bibliotecario/scan/{job_id}")
async def poll_scan(job_id: str, _user_id: str = Depends(user_dependency)):
    """Poll the status of a bibliotecario scan from Redis."""
    store = await _get_bib_job_store()
    record = await store.get(job_id)
    if not record:
        # Fallback: check in-memory dict for legacy/in-progress scans
        job = _scan_jobs.get(job_id)
        if not job:
            try:
                from mica.literature_consolidation.services.bibliotecario_service import (  # noqa: PLC0415
                    get_bibliotecario_scan_state,
                )
            except Exception:
                job = None
            else:
                job = get_bibliotecario_scan_state(job_id)
        if not job:
            raise HTTPException(404, f"Scan job {job_id} not found")
        _assert_scan_owner(job, _user_id)
        return normalize_poll_envelope(job)
    _assert_scan_owner(record, _user_id)
    return normalize_poll_envelope(record)


@router.get("/bibliotecario/scan/{job_id}/status")
async def poll_scan_status(job_id: str, _user_id: str = Depends(user_dependency)):
    """Lightweight status poll for a bibliotecario scan — no payload echo."""
    record = await poll_scan(job_id, _user_id=_user_id)
    return {
        "job_id": job_id,
        "status": record.get("status", "unknown"),
        "updated_at": record.get("updated_at"),
        "error": record.get("error"),
    }


@router.post("/mica-q/query")
async def query_mica_q(req: MICAQQueryRequest, _user_id: str = Depends(user_dependency)):
    """Query the public MICA-Q multisurface console and expose live literature/DLM verbs."""
    graph_store = _resolve_router_graph_store()
    try:
        from mica.infrastructure.persistence.retrieval_planner import RetrievalPlanner, RetrievalRequest
        from mica.memory.contracts import RetrievalMode
    except ImportError as exc:
        raise HTTPException(501, f"MICA-Q retrieval planner not available: {exc}")

    try:
        mica_q_service = _resolve_mica_q_multisurface_service_for_router(graph_store=graph_store)
        if mica_q_service is None:
            raise HTTPException(503, "MICA-Q multisurface service unavailable")

        planner = RetrievalPlanner(graph_store=graph_store, mica_q_service=mica_q_service)
        response = await planner.retrieve(
            RetrievalRequest(
                mode=RetrievalMode.MICA_Q_MULTISURFACE,
                query_text=req.query,
                user_id=_user_id or None,
                workspace_id=req.workspace_id or None,
                session_id=req.session_id or None,
                limit=req.limit,
            )
        )
        payload = dict(response.payload or {})
        candidate = _infer_mica_q_autonomous_candidate(
            query_text=req.query,
            workspace_id=req.workspace_id or "",
            session_id=req.session_id or "",
            limit=req.limit,
            payload=payload,
        )
        if candidate is not None:
            payload["auto_protocol_candidate"] = dict(candidate)
            try:
                from mica.drivers.execution.protocol_executor import execute_protocol_executor_request
                from mica.protocol_drafts import build_protocol_executor_request
                from mica_q.protocol_jsonld_validator import (
                    derive_protocol_execution_frontier,
                    validate_protocol_jsonld,
                )

                protocol_document = validate_protocol_jsonld(
                    _build_mica_q_protocol_document(candidate=candidate)
                )
                frontier = derive_protocol_execution_frontier(protocol_document, node_receipts=None)
                executor_request = build_protocol_executor_request(
                    protocol_document,
                    frontier,
                    request_metadata={
                        "workspace_id": req.workspace_id or "",
                        "session_id": req.session_id or protocol_document.session_id,
                        "query_surface": "public_mica_q_query",
                    },
                )
                outcome = await execute_protocol_executor_request(
                    executor_request,
                    checkpoint_dir=_mica_q_protocol_checkpoint_dir(user_id=_user_id or "anonymous"),
                )
                node_receipts = [_serialize_router_payload(item) for item in list(outcome.node_receipts or [])]
                payload["protocol_execution"] = {
                    "ok": outcome.failure_message is None and str(outcome.run_receipt.status or "") == "completed",
                    "status": str(outcome.run_receipt.status or "unknown"),
                    "failure_message": outcome.failure_message,
                    "transport": "protocol_executor",
                    "protocol_document": _serialize_router_payload(protocol_document),
                    "executor_request": _serialize_router_payload(executor_request),
                    "run_receipt": _serialize_router_payload(outcome.run_receipt),
                    "node_receipts": node_receipts,
                    "projection_message_ids": list(outcome.projection_message_ids or []),
                    "result": dict(node_receipts[0].get("state_after") or {}) if node_receipts else {},
                }
            except Exception as exc:
                degraded = list(payload.get("degraded") or [])
                degraded.append("mica_q_autonomous_protocol_failed")
                payload["degraded"] = degraded
                payload["protocol_execution"] = {
                    "ok": False,
                    "status": "failed",
                    "failure_message": str(exc),
                    "transport": "protocol_executor",
                    "candidate": dict(candidate),
                }
        payload["ok"] = True
        payload["mode"] = response.mode.value
        payload["query_surface"] = "public_mica_q_query"
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"MICA-Q query failed: {exc}")
    finally:
        if graph_store is not None:
            close = getattr(graph_store, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result


async def execute_scan_request(job_id: str, req: ScanRequest, *, state: Dict[str, Any]) -> None:
    """Execute one bibliotecario scan against an injected mutable state dict.

    This is the public execution seam shared by the router and the
    literature service, so the service no longer needs router internals or
    router-owned job-state storage.
    """
    state["status"] = "running"
    try:
        # ---- Apply DLM preset to request ----
        from mica.memory.dlm.presets import get_dlm_preset

        dlm_preset_map = {
            BibliotecarioPreset.ENTITY_SCAN: "quick-scan",
            BibliotecarioPreset.LITERATURE_REVIEW: "standard",
            BibliotecarioPreset.CO_OCCURRENCE_MAP: "standard",
            BibliotecarioPreset.TEMPORAL_EVOLUTION: "standard",
            BibliotecarioPreset.PDF_HARVEST: "deep-research",
            BibliotecarioPreset.DEEP_SYNTHESIS: "deep-research",
        }
        dlm_name = dlm_preset_map.get(req.preset, "standard")
        try:
            dlm_cfg = get_dlm_preset(dlm_name)
        except KeyError:
            dlm_cfg = get_dlm_preset("standard")

        # Merge DLM preset into request defaults
        if not req.sources or req.sources == ["semantic_scholar"]:
            req.sources = dlm_cfg.get("sources", req.sources)
        req.max_papers = min(req.max_papers, dlm_cfg.get("max_papers", req.max_papers))
        state["dlm_preset_applied"] = dlm_name

        query_strategy = _compile_scan_query_strategy(req)
        state["lmp_bibliotecario_handoff"] = query_strategy
        result: Dict[str, Any] = {}

        if req.preset == BibliotecarioPreset.ENTITY_SCAN:
            result = await _entity_scan(req)
        elif req.preset == BibliotecarioPreset.LITERATURE_REVIEW:
            result = await _literature_review(req, dlm_cfg=dlm_cfg)
        elif req.preset == BibliotecarioPreset.CO_OCCURRENCE_MAP:
            result = await _co_occurrence_scan(req)
        elif req.preset == BibliotecarioPreset.TEMPORAL_EVOLUTION:
            result = await _temporal_evolution_scan(req)
        elif req.preset == BibliotecarioPreset.PDF_HARVEST:
            result = await _pdf_harvest(req)
        elif req.preset == BibliotecarioPreset.DEEP_SYNTHESIS:
            result = await _literature_review(req, dlm_cfg=dlm_cfg)
        else:
            result = {"warning": f"Preset {req.preset.value} not fully implemented"}

        result.setdefault("lmp_bibliotecario_handoff", query_strategy)

        state["status"] = "done"
        state["result"] = result
    except Exception as exc:
        logger.exception("Bibliotecario scan %s failed", job_id)
        state["status"] = "error"
        state["error"] = str(exc)


async def _run_scan(job_id: str, req: ScanRequest) -> None:
    """Execute the bibliotecario scan in the router-owned in-memory state."""
    state = _scan_jobs.setdefault(job_id, {"status": "queued", "result": None})
    await execute_scan_request(job_id, req, state=state)


def _compile_scan_query_strategy(req: ScanRequest) -> Dict[str, Any]:
    return compile_lmp_bibliotecario_handoff(
        query=req.query,
        entities=req.entities,
        pdb_ids=req.pdb_ids,
        extra_queries=req.extra_queries,
        lmp_handoff=req.lmp_handoff,
        require_full_text=req.require_full_text,
    )


def _scan_extra_queries(req: ScanRequest, query_strategy: Optional[Dict[str, Any]] = None) -> List[str]:
    strategy = query_strategy if query_strategy is not None else _compile_scan_query_strategy(req)
    return list(strategy.get("extra_queries") or [])


async def _entity_scan(req: ScanRequest) -> Dict[str, Any]:
    """Fast entity scan: S2 search → DLM encode → entity extraction."""
    from mica.memory.dlm.encoder import DLMEncoder

    search = await _search_literature(
        query=req.query,
        max_papers=min(req.max_papers, 50),
        sources=["semantic_scholar"],
        extra_queries=_scan_extra_queries(req),
        lane_class="entity_scan",
        preset_name=req.preset.value,
        session_id=req.session_id,
        user_id=req.user_id,
        acquisition_budget_usd=req.acquisition_budget_usd,
    )
    papers = search["papers"]

    encoder = DLMEncoder()
    all_entities: Dict[str, Dict[str, Any]] = {}
    encoded_count = 0

    for paper in papers:
        text = f"{paper.get('title', '')}. {paper.get('abstract', '')}"
        if not text.strip(". "):
            continue
        doc = encoder.encode(text)
        encoded_count += 1
        for ent in doc.entities:
            key = ent["text"].lower()
            if key not in all_entities:
                all_entities[key] = {
                    "text": ent["text"],
                    "type": ent["type"],
                    "count": 0,
                    "papers": [],
                }
            all_entities[key]["count"] += 1
            pid = paper.get("paperId", paper.get("title", "")[:30])
            if pid not in all_entities[key]["papers"]:
                all_entities[key]["papers"].append(pid)

    # Sort by frequency
    sorted_entities = sorted(all_entities.values(), key=lambda e: -e["count"])

    return {
        "total_papers": len(papers),
        "encoded_papers": encoded_count,
        "total_entities": len(sorted_entities),
        "entities": sorted_entities[:100],  # Top 100
        "query": req.query,
    }


async def _literature_review(
    req: ScanRequest,
    *,
    dlm_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Standard literature review: fetch → encode → optional ATOM → DOCX.

    When ``dlm_cfg`` is provided (via DLM preset), respects its flags:
    ``extract_entities``, ``run_atom``, ``download_pdfs``, ``sources``.
    """
    if dlm_cfg is None:
        dlm_cfg = {}

    active_sources = list(dlm_cfg.get("sources", req.sources) or req.sources)
    effective_run_id = str(req.run_id or "").strip() or f"bib-{uuid.uuid4().hex[:12]}"
    effective_session_id = str(req.session_id or "").strip() or effective_run_id
    req = req.model_copy(update={"session_id": effective_session_id, "run_id": effective_run_id})
    query_strategy = _compile_scan_query_strategy(req)
    search = await _search_literature(
        query=req.query,
        max_papers=req.max_papers,
        sources=active_sources,
        extra_queries=_scan_extra_queries(req, query_strategy),
        paper_identity_targets=list(query_strategy.get("paper_identity_targets") or []),
        lane_class="bibliotecario_review",
        preset_name=dlm_cfg.get("name", req.preset.value if hasattr(req, "preset") else "bibliotecario_review"),
        session_id=req.session_id,
        run_id=req.run_id,
        user_id=req.user_id,
        acquisition_budget_usd=req.acquisition_budget_usd,
        require_cloud_evidence=True,
    )
    papers = list(search["papers"])

    # Entity extraction if DLM preset enables it
    entity_summary = None
    if dlm_cfg.get("extract_entities", False):
        try:
            from mica.memory.dlm.encoder import DLMEncoder
            encoder = DLMEncoder()
            all_entities: Dict[str, int] = {}
            for paper in papers[:200]:
                text = best_available_literature_text(paper)
                if not text.strip(". "):
                    continue
                doc = encoder.encode(text)
                for ent in doc.entities:
                    key = ent["text"].lower()
                    all_entities[key] = all_entities.get(key, 0) + 1
            sorted_ents = sorted(all_entities.items(), key=lambda x: -x[1])
            entity_summary = {
                "total_unique": len(sorted_ents),
                "top_entities": [{"text": t, "count": c} for t, c in sorted_ents[:50]],
            }
        except Exception as exc:
            logger.warning("Entity extraction in literature review failed: %s", exc)

    # Milvus indexing if available
    milvus_indexed = 0
    try:
        from mica.memory.milvus_integration import get_milvus_service
        svc = get_milvus_service()
        if svc.is_connected:
            milvus_indexed = svc.index_papers_batch(papers)
    except Exception:
        pass

    result: Dict[str, Any] = {
        "total_papers": len(papers),
        "papers": papers[:50],  # Return top 50 for UI
        "all_paper_count": len(papers),
        "sources_used": active_sources,
        "dlm_preset": dlm_cfg.get("name", "custom"),
        "query": req.query,
        "session_id": req.session_id,
        "run_id": req.run_id,
        "milvus_indexed": milvus_indexed,
        "literature_backend": search["backend"],
        "source_counts": search["source_counts"],
        "retrieval_policy": search.get("retrieval_policy", {}),
        "acquisition_envelope": search.get("acquisition_envelope", {}),
        "lmp_bibliotecario_handoff": query_strategy,
        "full_text_policy": query_strategy.get("full_text_policy", {}),
    }
    if req.preset in (BibliotecarioPreset.LITERATURE_REVIEW, BibliotecarioPreset.DEEP_SYNTHESIS):
        result.update(await _build_literature_artifact_bundle_for_review(req, search))
    if entity_summary:
        result["entity_summary"] = entity_summary
    return result


@router.post("/bibliotecario/deep-synthesis/sync")
async def deep_synthesis_sync(req: ScanRequest, user_id: str = Depends(user_dependency)):
    """Direct deep-synthesis helper route that returns the canonical artifact bundle."""
    req = req.model_copy(update={"preset": BibliotecarioPreset.DEEP_SYNTHESIS, "user_id": user_id})

    from mica.memory.dlm.presets import get_dlm_preset

    try:
        dlm_cfg = get_dlm_preset("deep-research")
    except KeyError:
        dlm_cfg = {"name": "deep-research", "sources": req.sources, "max_papers": req.max_papers}

    result = await _literature_review(req, dlm_cfg=dlm_cfg)
    return {
        "ok": True,
        "preset": BibliotecarioPreset.DEEP_SYNTHESIS.value,
        "query": req.query,
        "session_id": result.get("session_id") or req.session_id,
        "run_id": result.get("run_id") or req.run_id,
        "artifact_bundle": result.get("artifact_bundle") or {},
        "artifact_manifest": result.get("artifact_manifest") or {},
        "artifact_list": result.get("artifact_list") or [],
        "papers_preview": result.get("papers") or [],
        "total_papers": int(result.get("total_papers") or 0),
    }


async def _co_occurrence_scan(req: ScanRequest) -> Dict[str, Any]:
    """Entity co-occurrence mapping via ATOM quintuples."""
    from mica.memory.dlm.encoder import DLMEncoder

    search = await _search_literature(
        query=req.query,
        max_papers=min(req.max_papers, 200),
        sources=["semantic_scholar"],
        extra_queries=_scan_extra_queries(req),
        lane_class="entity_scan",
        preset_name=req.preset.value,
        session_id=req.session_id,
        user_id=req.user_id,
        acquisition_budget_usd=req.acquisition_budget_usd,
    )
    papers = search["papers"]

    encoder = DLMEncoder()
    co_occurrence: Dict[str, Dict[str, int]] = {}
    entity_counts: Dict[str, int] = {}

    for paper in papers:
        text = f"{paper.get('title', '')}. {paper.get('abstract', '')}"
        if not text.strip(". "):
            continue
        doc = encoder.encode(text)
        paper_entities = list({e["text"].lower() for e in doc.entities})

        for ent in paper_entities:
            entity_counts[ent] = entity_counts.get(ent, 0) + 1

        # Build co-occurrence pairs
        for i, e1 in enumerate(paper_entities):
            for e2 in paper_entities[i + 1:]:
                pair_key = tuple(sorted([e1, e2]))
                if pair_key[0] not in co_occurrence:
                    co_occurrence[pair_key[0]] = {}
                co_occurrence[pair_key[0]][pair_key[1]] = co_occurrence[pair_key[0]].get(pair_key[1], 0) + 1

    # Convert to graph data for KnowledgeGraphPanel
    nodes = []
    links = []
    top_entities = sorted(entity_counts.items(), key=lambda x: -x[1])[:30]
    top_set = {e[0] for e in top_entities}

    for ent, count in top_entities:
        nodes.append({
            "id": ent,
            "label": ent,
            "type": "knowledge",
            "metadata": {"count": str(count)},
        })

    for e1, targets in co_occurrence.items():
        if e1 not in top_set:
            continue
        for e2, weight in targets.items():
            if e2 not in top_set or weight < 2:
                continue
            links.append({
                "source": e1,
                "target": e2,
                "type": "CO_OCCURRENCE",
                "label": str(weight),
            })

    return {
        "total_papers": len(papers),
        "total_entities": len(entity_counts),
        "graph_data": {"nodes": nodes, "links": links},
        "entity_counts": dict(top_entities),
        "query": req.query,
    }


async def _temporal_evolution_scan(req: ScanRequest) -> Dict[str, Any]:
    """Track entity evolution over time across papers."""
    from mica.memory.dlm.encoder import DLMEncoder

    search = await _search_literature(
        query=req.query,
        max_papers=min(req.max_papers, 500),
        sources=["semantic_scholar"],
        extra_queries=_scan_extra_queries(req),
        session_id=req.session_id,
        user_id=req.user_id,
        acquisition_budget_usd=req.acquisition_budget_usd,
    )
    papers = search["papers"]

    encoder = DLMEncoder()
    # Year → entity → count
    timeline: Dict[int, Dict[str, int]] = {}

    for paper in papers:
        year = paper.get("year")
        if not year or not isinstance(year, int):
            continue
        text = f"{paper.get('title', '')}. {paper.get('abstract', '')}"
        if not text.strip(". "):
            continue
        doc = encoder.encode(text)

        if year not in timeline:
            timeline[year] = {}
        for ent in doc.entities:
            key = ent["text"].lower()
            timeline[year][key] = timeline[year].get(key, 0) + 1

    # Build chart data: {entity: [{year, count}, ...]}
    all_entities: Dict[str, int] = {}
    for year_data in timeline.values():
        for ent, count in year_data.items():
            all_entities[ent] = all_entities.get(ent, 0) + count

    top_entities = sorted(all_entities.items(), key=lambda x: -x[1])[:15]
    top_set = {e[0] for e in top_entities}

    chart_data = {}
    for ent_name, _ in top_entities:
        series = []
        for year in sorted(timeline.keys()):
            count = timeline[year].get(ent_name, 0)
            if count > 0:
                series.append({"year": year, "count": count})
        chart_data[ent_name] = series

    return {
        "total_papers": len(papers),
        "year_range": [min(timeline.keys()), max(timeline.keys())] if timeline else [],
        "top_entities": [{"name": n, "total": c} for n, c in top_entities],
        "chart_data": chart_data,
        "query": req.query,
    }


async def _pdf_harvest(req: ScanRequest) -> Dict[str, Any]:
    """Bulk download PDFs and store in GCS workspace."""
    search = await _search_literature(
        query=req.query,
        max_papers=min(req.max_papers, 100),
        sources=req.sources,
        extra_queries=_scan_extra_queries(req),
        session_id=req.session_id,
        user_id=req.user_id,
        acquisition_budget_usd=req.acquisition_budget_usd,
    )
    papers = search["papers"]
    target_session_id = req.session_id or "default"

    downloaded = 0
    failed = 0
    results: List[Dict[str, Any]] = []
    verification_artifact: Optional[Dict[str, Any]] = None

    # Try to download open-access PDFs
    try:
        from mica.memory.dlm.gcs_pdf_bridge import download_pdf_to_workspace

        for paper in papers[:30]:  # Cap at 30 PDFs per harvest
            oa = paper.get("openAccessPdf") or {}
            url = oa.get("url")
            if not url:
                continue
            paper_id = paper.get("paperId") or paper.get("externalIds", {}).get("DOI") or f"pdf_{downloaded}"
            title = paper.get("title", "unknown")
            safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in title[:80])
            try:
                download = await download_pdf_to_workspace(
                    session_id=target_session_id,
                    user_id=req.user_id,
                    paper_id=paper_id,
                    source_url=url,
                    filename=f"{safe_name}.pdf",
                )
                if download.get("ok"):
                    downloaded += 1
                    results.append(
                        {
                            "paper_id": paper_id,
                            "title": paper.get("title"),
                            "status": "downloaded",
                            "download": download,
                        }
                    )
                else:
                    failed += 1
                    results.append(
                        {
                            "paper_id": paper_id,
                            "title": paper.get("title"),
                            "status": "failed",
                            "error": download.get("error") or "download failed",
                            "download": download,
                        }
                    )
            except Exception as exc:
                failed += 1
                results.append(
                    {
                        "paper_id": paper_id,
                        "title": paper.get("title"),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
    except ImportError:
        logger.warning("gcs_pdf_bridge not available, skipping PDF downloads")

    manifest: Dict[str, Any] = {
        "artifact_type": "pdf_harvest_manifest",
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "query": req.query,
        "preset": req.preset.value,
        "session_id": target_session_id,
        "user_id": req.user_id,
        "literature_backend": search.get("backend"),
        "source_counts": search.get("source_counts") or {},
        "queries": search.get("queries") or [req.query],
        "total_papers": len(papers),
        "open_access_found": sum(1 for p in papers if p.get("openAccessPdf")),
        "downloaded": downloaded,
        "failed": failed,
        "results": results,
    }

    try:
        from mica.api_v1.routers.workspace import _get_backend

        backend = _get_backend()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=True).encode("utf-8")
        asset = backend.add_asset(
            user_id=req.user_id,
            session_id=target_session_id,
            asset_type="other",
            name=f"pdf_harvest_manifest_{timestamp}.json",
            data=manifest_bytes,
        )
        verification_artifact = dict(asset)
        verification_artifact["backend"] = backend.get_asset_download_info(
            req.user_id,
            target_session_id,
            asset["asset_id"],
        ).get("backend")
        manifest["manifest_asset"] = verification_artifact
    except Exception as exc:
        verification_artifact = {
            "status": "not_persisted",
            "error": str(exc),
            "session_id": target_session_id,
        }
        manifest["manifest_asset"] = verification_artifact
        logger.warning("Failed to persist pdf-harvest manifest for session %s: %s", target_session_id, exc)

    return {
        "total_papers": len(papers),
        "open_access_found": sum(1 for p in papers if p.get("openAccessPdf")),
        "downloaded": downloaded,
        "failed": failed,
        "results": results[:20],
        "verification_artifact": verification_artifact,
        "harvest_manifest": manifest,
        "query": req.query,
    }


# ---------------------------------------------------------------------------
# Entity Resolution
# ---------------------------------------------------------------------------


@router.post("/entity/resolve")
async def resolve_entity(req: EntityResolveRequest):
    """Resolve an entity name to knowledge base IDs (UniProt, HGNC, MONDO, DrugBank)."""
    try:
        from mica.memory.dlm.entity_mapper import EntityMapper
        mapper = EntityMapper()
        result = await asyncio.to_thread(mapper.map_entity, req.name, req.entity_type)
        if result and result.is_mapped():
            return {
                "resolved": True,
                "name": req.name,
                "kb_id": result.kb_id,
                "kb_source": result.kb_source,
                "confidence": result.confidence,
                "synonyms": list(getattr(result, "synonyms", []) or []),
                "entity_type": result.entity_type,
            }
        return {"resolved": False, "name": req.name, "message": "Entity not found in any KB"}
    except ImportError as exc:
        raise HTTPException(501, f"Entity mapper not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Entity resolution failed: {exc}")


@router.get("/entity/co-occurrence")
async def entity_co_occurrence(
    entities: str = Query(..., description="Comma-separated entity names"),
    min_papers: int = Query(1, ge=1, description="Minimum shared papers for a pair"),
):
    """Query entity co-occurrence from the ATOM knowledge graph.

    Scans all ATOM quintuples to find papers where multiple entities
    co-occur, and builds a pairwise co-occurrence matrix.
    """
    entity_list = [e.strip() for e in entities.split(",") if e.strip()]
    if len(entity_list) < 2:
        raise HTTPException(400, "Provide at least 2 comma-separated entity names")

    try:
        from mica.memory.dlm.batch_mapper import DLMBatchMapper

        mapper = DLMBatchMapper(enable_atom=True)
        # Load ATOM state
        try:
            await mapper.atom_system.load_persistent_state()
        except Exception:
            pass

        result = await mapper.query_entity_co_occurrence(
            entities=entity_list, min_papers=min_papers
        )
        return result
    except ImportError as exc:
        raise HTTPException(501, f"Batch mapper not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Co-occurrence query failed: {exc}")


@router.get("/entity/evolution")
async def entity_evolution(
    entity: str = Query(..., description="Entity name to track"),
    entity_type: str = Query("protein", description="Entity type label"),
    start_year: int = Query(2015, description="Start year"),
    end_year: int = Query(2025, description="End year"),
):
    """Track temporal evolution of an entity's mentions across publications.

    Returns yearly mention counts, co-occurring entities, and publication trends
    from the ATOM temporal knowledge graph.
    """
    try:
        from mica.memory.dlm.batch_mapper import DLMBatchMapper

        mapper = DLMBatchMapper(enable_atom=True)
        try:
            await mapper.atom_system.load_persistent_state()
        except Exception:
            pass

        result = await mapper.track_entity_evolution(
            entity=entity,
            entity_type=entity_type,
            year_range=(start_year, end_year),
        )
        return result
    except ImportError as exc:
        raise HTTPException(501, f"Batch mapper not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Temporal evolution query failed: {exc}")


@router.post("/pharma/enrich")
async def enrich_pharma(
    uniprot_id: str = Query(..., description="UniProt accession ID"),
):
    """Enrich a protein with pharmacological data from DrugBank/ChEMBL/OpenTargets.

    Returns approved drugs, clinical trials, mechanisms of action, ChEMBL IDs.
    """
    try:
        from mica.memory.dlm.pharma_enrichment import enrich_single_protein
        result = await asyncio.to_thread(enrich_single_protein, uniprot_id)
        return result
    except ImportError as exc:
        raise HTTPException(501, f"Pharma enrichment module not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Pharma enrichment failed: {exc}")


# ---------------------------------------------------------------------------
# ATOM Temporal Queries
# ---------------------------------------------------------------------------


@router.post("/atom/query")
async def query_atom(req: ATOMQueryRequest):
    """Query ATOM temporal knowledge graph by entity, predicate, phase, or temperature mode."""
    try:
        from mica.memory.atom.system import ATOMMemorySystem, ATOMMemoryConfig
        from mica.infrastructure.persistence.retrieval_planner import RetrievalPlanner, RetrievalRequest
        from mica.memory.contracts import RetrievalMode
        config = ATOMMemoryConfig()
        system = ATOMMemorySystem(config)
        graph_store = None

        # Load persisted state
        try:
            await system.load_persistent_state()
        except Exception:
            pass

        if req.cognitive_phase:
            facts = system.query_by_cognitive_phase(req.cognitive_phase, limit=req.limit)
            graph_facts: List[Any] = []
            degraded: List[str] = []
        else:
            query_text = str(req.entity or req.predicate or "").strip()
            if query_text:
                graph_store = _resolve_router_graph_store()
                planner = RetrievalPlanner(
                    graph_store=graph_store,
                    atom_memory=system,
                    mica_q_service=_resolve_mica_q_multisurface_service_for_router(graph_store=graph_store),
                )
                response = await planner.retrieve(
                    RetrievalRequest(
                        mode=RetrievalMode.TEMPORAL_FACTS,
                        query_text=query_text,
                        user_id=req.user_id,
                        workspace_id=req.workspace_id,
                        session_id=req.session_id,
                        limit=req.limit,
                    )
                )
                payload = dict(response.payload or {})
                facts = list(payload.get("facts") or [])
                graph_facts = list(payload.get("graph_facts") or [])
                degraded = list(payload.get("degraded") or [])
            else:
                facts = []
                graph_facts = []
                degraded = []

        return {
            "facts": [_serialize_atom_fact_payload(fact) for fact in facts],
            "graph_facts": [_serialize_atom_fact_payload(fact) for fact in graph_facts],
            "total": len(facts) + len(graph_facts),
            "atom_total": len(facts),
            "graph_total": len(graph_facts),
            "temperature_mode": req.temperature_mode,
            "provenance": {
                "query_text": str(req.entity or req.predicate or "").strip(),
                "degraded": degraded,
            },
        }
    except ImportError as exc:
        raise HTTPException(501, f"ATOM system not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"ATOM query failed: {exc}")
    finally:
        if 'graph_store' in locals() and graph_store is not None:
            close = getattr(graph_store, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result


@router.get("/atom/summary")
async def atom_summary():
    """Get ATOM thermodynamic summary — temperature, phase, entity/relation counts."""
    try:
        from mica.memory.atom.system import ATOMMemorySystem, ATOMMemoryConfig
        config = ATOMMemoryConfig()
        system = ATOMMemorySystem(config)

        try:
            await system.load_persistent_state()
        except Exception:
            pass

        summary = await system.summarize()
        return summary
    except ImportError as exc:
        raise HTTPException(501, f"ATOM system not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"ATOM summary failed: {exc}")


# ---------------------------------------------------------------------------
# PDF Download to Workspace
# ---------------------------------------------------------------------------


@router.post("/pdf/download-to-workspace")
async def download_pdf(req: PDFDownloadRequest):
    """Download a PDF and store it in the user's GCS workspace."""
    try:
        from mica.memory.dlm.gcs_pdf_bridge import download_pdf_to_workspace

        url = req.url
        if not url and req.arxiv_id:
            url = f"https://export.arxiv.org/pdf/{req.arxiv_id}"
        if not url and req.paper_id:
            # Try to get URL from Semantic Scholar
            papers = await _hydrate_semantic_scholar_papers([req.paper_id])
            if papers:
                oa = papers[0].get("openAccessPdf") or {}
                url = oa.get("url")

        if not url:
            raise HTTPException(400, "No PDF URL available. Provide url, arxiv_id, or an open-access paper_id.")

        _paper_id = req.paper_id or (req.arxiv_id and f"arxiv_{req.arxiv_id}") or "pdf_download"
        result = await download_pdf_to_workspace(
            session_id=req.session_id,
            user_id=req.user_id,
            paper_id=_paper_id,
            source_url=url,
        )
        return {"ok": True, "result": result}
    except ImportError as exc:
        raise HTTPException(501, f"PDF bridge not available: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"PDF download failed: {exc}")


# ---------------------------------------------------------------------------
# Protein Metadata Search
# ---------------------------------------------------------------------------


@router.post("/metadata/search")
async def search_metadata(req: MetadataSearchRequest):
    """Search the protein metadata cache by flags, text query, pharmacological filters, or any combination."""
    try:
        from pathlib import Path
        from mica.memory.dlm_lmp.metadata_service import LMPMetadataService

        # Discover cache directory from environment or common locations
        import os
        cache_dir = os.environ.get("LMP_CACHE_DIR", "")
        if not cache_dir:
            # Try common locations
            for candidate in [
                Path("lmp_cache"),
                Path.home() / ".mica" / "lmp_cache",
                Path(__file__).resolve().parents[3] / "lmp_cache",
            ]:
                if candidate.exists():
                    cache_dir = str(candidate)
                    break

        if not cache_dir:
            return {"results": [], "total": 0, "error": "LMP cache directory not configured"}

        service = LMPMetadataService(cache_dir=Path(cache_dir))

        criteria: Dict[str, Any] = {}
        if req.query:
            criteria["query"] = req.query
        if req.is_kinase is not None:
            criteria["is_kinase"] = req.is_kinase
        if req.has_ptms is not None:
            criteria["has_ptms"] = req.has_ptms
        if req.has_domains is not None:
            criteria["has_domains"] = req.has_domains
        if req.has_binding_sites is not None:
            criteria["has_binding_sites"] = req.has_binding_sites
        if req.has_approved_drugs is not None:
            criteria["has_approved_drugs"] = req.has_approved_drugs
        if req.protein_family is not None:
            criteria["protein_family"] = req.protein_family
        if req.min_approved_drugs is not None:
            criteria["min_approved_drugs"] = req.min_approved_drugs
        if req.min_domains is not None:
            criteria["min_domains"] = req.min_domains
        if req.has_disease is not None:
            criteria["has_disease"] = req.has_disease
        if req.has_pathway is not None:
            criteria["has_pathway"] = req.has_pathway
        if req.organism is not None:
            criteria["organism"] = req.organism

        if not criteria:
            return {"results": [], "total": 0}

        results = service.search(**criteria)

        # Serialize using to_dict() which includes all new fields
        items = []
        for meta in results[:req.limit]:
            d = meta.to_dict()
            items.append({
                "uniprot_id": d["uniprot_id"],
                "gene_name": d["gene_name"],
                "organism": d["organism"],
                "description": d.get("description"),
                "protein_family": d.get("protein_family"),
                "function": d.get("function", []),
                "pathways": d.get("pathways", []),
                "disease_associations": d.get("disease_associations", []),
                "drug_targets": d.get("drug_targets", []),
                "approved_drugs_count": d.get("approved_drugs_count", 0),
                "chembl_ids": d.get("chembl_ids", []),
                "mechanism_of_action": d.get("mechanism_of_action", []),
                "tissue_expression": d.get("tissue_expression", []),
                "flags": d.get("flags", {}),
                "counts": d.get("counts", {}),
            })

        return {"results": items, "total": len(items)}
    except ImportError as exc:
        raise HTTPException(501, f"Metadata service not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Metadata search failed: {exc}")


# ---------------------------------------------------------------------------
# DLM-LMP Convergence
# ---------------------------------------------------------------------------


@router.post("/dlm-lmp/converge")
async def converge_dlm_lmp(req: ConvergenceRequest):
    """Run DLM-LMP XML convergence — merge literature evidence into LMP v4 XML."""
    try:
        from mica.memory.dlm_lmp.convergence import LMPDLMConvergence
        convergence = LMPDLMConvergence()
        result_xml = await asyncio.to_thread(
            convergence.merge,
            lmp_xml_string=req.lmp_xml,
            dlm_papers=req.papers,
            atom_quintuples=req.atom_quintuples,
            user_id=req.user_id,
        )
        return {"ok": True, "enriched_xml": result_xml, "papers_merged": len(req.papers)}
    except ImportError as exc:
        raise HTTPException(501, f"Convergence module not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Convergence failed: {exc}")


# ---------------------------------------------------------------------------
# Milvus Hybrid Search
# ---------------------------------------------------------------------------


@router.post("/milvus/search")
async def milvus_hybrid_search(req: MilvusSearchRequest):
    """Run hybrid vector + scalar search on the Milvus papers collection.

    Combines semantic similarity (BioBERT/PubMedBERT embeddings) with
    scalar filters (year, citations) for precision retrieval.
    """
    try:
        from mica.memory.milvus_integration import get_milvus_service

        svc = get_milvus_service(agent_id=req.agent_id)
        if not svc.is_connected:
            return {
                "results": [],
                "total": 0,
                "warning": "Milvus not connected — search unavailable",
                "milvus_stats": svc.get_stats(),
            }

        filters: Dict[str, Any] = {}
        if req.min_year is not None:
            filters["publication_year"] = {"$gte": req.min_year}
        if req.max_year is not None:
            filters.setdefault("publication_year", {})
            if isinstance(filters["publication_year"], dict):
                filters["publication_year"]["$lte"] = req.max_year
        if req.min_citations is not None:
            filters["citation_count"] = {"$gte": req.min_citations}

        results = await asyncio.to_thread(
            svc.hybrid_search,
            query=req.query,
            filters=filters if filters else None,
            limit=req.limit,
        )

        return {
            "results": results,
            "total": len(results),
            "query": req.query,
            "filters_applied": filters,
            "milvus_stats": svc.get_stats(),
        }
    except ImportError as exc:
        raise HTTPException(501, f"Milvus integration not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Milvus search failed: {exc}")


@router.post("/milvus/sequence-search")
async def milvus_sequence_search(
    req: MilvusSequenceSearchRequest,
    user_id: str = Depends(user_dependency),
):
    """Embed a protein sequence with serverless ESM2 and search a Milvus collection."""
    try:
        from mica.api_v1.routers.serverless_models import get_serverless_model_gateway
        from mica.serverless_models import ModelInvocationRequest
        from mica.services.sequence_milvus_search_service import SequenceMilvusSearchService

        gateway = get_serverless_model_gateway(user_id)
        request_id = str(uuid.uuid4())
        invoke_request = ModelInvocationRequest(
            request_id=request_id,
            model_id=req.model_id,
            user_id=user_id,
            session_id=req.session_id or f"milvus-sequence-search-{user_id}",
            run_id=req.run_id or request_id,
            inputs={
                "sequence": req.sequence,
                "pooling": req.pooling,
                "normalize": req.normalize_embedding,
            },
            requested_by="api.research.milvus.sequence_search",
        )
        invocation_result = await gateway.invoke(invoke_request)
        normalized_output = dict(invocation_result.normalized_output or {})
        embedding = normalized_output.get("embedding")
        if not embedding:
            raise HTTPException(502, "Serverless model did not return an embedding")

        service = SequenceMilvusSearchService()
        try:
            requested_search = await service.search_embedding(
                embedding=embedding,
                collection_name=req.requested_collection_name,
                limit=req.limit,
                normalize_query=req.normalize_embedding,
            )
        except Exception as exc:
            requested_search = {
                "collection": {
                    "name": req.requested_collection_name,
                    "compatible_with_query": False,
                    "error": str(exc),
                },
                "results": [],
                "milvus_stats": {},
            }

        fallback_used = False
        effective_search = requested_search
        requested_collection = requested_search["collection"]
        requested_compatible = bool(requested_collection.get("compatible_with_query"))
        if not requested_compatible and req.fallback_collection_name and req.fallback_collection_name != req.requested_collection_name:
            if not req.strict_requested_collection:
                fallback_used = True
                effective_search = await service.search_embedding(
                    embedding=embedding,
                    collection_name=req.fallback_collection_name,
                    limit=req.limit,
                    normalize_query=req.normalize_embedding,
                )

        return {
            "model_invocation": {
                "request_id": invocation_result.request_id,
                "model_id": invocation_result.model_id,
                "provider": invocation_result.provider,
                "state": invocation_result.state,
                "provider_job_id": invocation_result.provider_job_id,
            },
            "embedding_summary": {
                "vector_dim": normalized_output.get("vector_dim"),
                "token_count": normalized_output.get("token_count"),
                "sequence_length": normalized_output.get("sequence_length", len(req.sequence)),
                "hf_model_id": normalized_output.get("hf_model_id"),
                "summary_text": normalized_output.get("summary_text"),
                "dctdomain_semantic_parity": normalized_output.get("dctdomain_semantic_parity"),
                "dctdomain_parity_note": normalized_output.get("dctdomain_parity_note"),
            },
            "requested_collection": requested_collection,
            "searched_collection": effective_search["collection"],
            "fallback_used": fallback_used,
            "results": effective_search["results"],
            "total": len(effective_search["results"]),
            "milvus_stats": effective_search["milvus_stats"],
        }
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(404, f"Serverless model not found: {exc}")
    except ValueError as exc:
        raise HTTPException(400, f"Sequence Milvus search request invalid: {exc}")
    except RuntimeError as exc:
        raise HTTPException(503, f"Sequence Milvus search unavailable: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Sequence Milvus search failed: {exc}")


@router.post("/milvus/stored-embedding-search")
async def milvus_stored_embedding_search(req: MilvusStoredEmbeddingSearchRequest):
    """Reuse an embedding already stored in Milvus to query the same or another collection."""
    try:
        from mica.services.sequence_milvus_search_service import SequenceMilvusSearchService

        service = SequenceMilvusSearchService()
        result = await service.search_with_stored_embedding(
            protein_id=req.protein_id,
            source_collection_name=req.source_collection_name,
            target_collection_name=req.target_collection_name,
            limit=req.limit,
            exclude_source=req.exclude_source,
            normalize_query=req.normalize_query,
        )
        return {
            **result,
            "total": len(result["results"]),
        }
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, f"Stored embedding search request invalid: {exc}")
    except RuntimeError as exc:
        raise HTTPException(503, f"Stored embedding search unavailable: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Stored embedding search failed: {exc}")


@router.post("/milvus/dct-search")
async def milvus_dct_search(
    req: MilvusDCTSearchRequest,
    user_id: str = Depends(user_dependency),
):
    """Generate DCTdomain fingerprints through RunPod and search the Milvus DCT collection."""
    del user_id
    try:
        from mica.services.dct_milvus_search_service import get_dct_milvus_search_service

        service = get_dct_milvus_search_service()
        result = await service.search_remote_sequence(
            sequence=req.sequence,
            pid=req.pid or f"dct-query-{uuid.uuid4().hex[:8]}",
            collection_name=req.collection_name,
            limit=req.limit,
            candidate_limit=req.candidate_limit,
            maxlen=req.maxlen,
            threshold=req.threshold,
            qdim=req.qdim,
            wait_ms=req.wait_ms,
            runpod_endpoint_id=req.runpod_endpoint_id,
            search_profile=req.search_profile,
            rerank_mode=req.rerank_mode,
            include_text=req.include_text,
            global_only=req.global_only,
        )
        return {
            **result,
            "total": len(result["top_hits"]),
        }
    except ValueError as exc:
        raise HTTPException(400, f"DCT Milvus search request invalid: {exc}")
    except (ImportError, LookupError, RuntimeError) as exc:
        raise HTTPException(503, f"DCT Milvus search unavailable: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"DCT Milvus search failed: {exc}")


@router.post("/milvus/dct-search/fingerprint")
async def milvus_dct_fingerprint_search(
    req: MilvusDCTFingerprintSearchRequest,
    user_id: str = Depends(user_dependency),
):
    """Search Milvus directly from precomputed DCT fingerprints without invoking RunPod."""
    del user_id
    try:
        from mica.services.dct_milvus_search_service import get_dct_milvus_search_service

        service = get_dct_milvus_search_service()
        result = await service.search_remote_fingerprint_payload(
            remote_payload={
                "provider": "precomputed",
                "pid": req.pid or f"dct-fingerprint-{uuid.uuid4().hex[:8]}",
                "sequence_length": req.sequence_length,
                "domain_count": len(req.domains),
                "domains": [domain.model_dump() for domain in req.domains],
            },
            collection_name=req.collection_name,
            limit=req.limit,
            candidate_limit=req.candidate_limit,
            search_profile=req.search_profile,
            rerank_mode=req.rerank_mode,
            include_text=req.include_text,
            global_only=req.global_only,
        )
        return {
            **result,
            "total": len(result["top_hits"]),
        }
    except ValueError as exc:
        raise HTTPException(400, f"DCT fingerprint search request invalid: {exc}")
    except (ImportError, LookupError, RuntimeError) as exc:
        raise HTTPException(503, f"DCT fingerprint search unavailable: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"DCT fingerprint search failed: {exc}")


@router.get("/milvus/stats")
async def milvus_stats():
    """Get Milvus indexing and search statistics."""
    try:
        from mica.memory.milvus_integration import get_milvus_service
        svc = get_milvus_service()
        return svc.get_stats()
    except ImportError as exc:
        raise HTTPException(501, f"Milvus integration not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Milvus stats failed: {exc}")


# ---------------------------------------------------------------------------
# Cascade Super Pipeline
# ---------------------------------------------------------------------------

_cascade_jobs: Dict[str, Dict[str, Any]] = {}


@router.post("/cascade/run")
async def run_cascade_pipeline(req: CascadePipelineRequest, background_tasks: BackgroundTasks):
    """Launch the full cascade pipeline: scan → encode → enrich → converge → index.

    Orchestrates ALL underutilized modules in sequence:
    1. Literature scan (DLM batch_mapper or S2 search)
    2. Entity extraction + KB resolution (EntityMapper)
    3. ATOM quintuple storage (temporal knowledge)
    4. Pharmacological enrichment (MyChemInfo/DrugBank)
    5. Milvus hybrid indexing (vector + scalar)
    6. DLM-LMP convergence (inject literature into LMP XML)
    """
    job_id = uuid.uuid4().hex[:12]
    _cascade_jobs[job_id] = {
        "status": "queued",
        "query": req.query,
        "preset": req.preset,
        "stages": {},
        "result": None,
        "error": None,
    }

    background_tasks.add_task(_run_cascade, job_id, req)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.get("/cascade/{job_id}")
async def poll_cascade(job_id: str):
    """Poll the status of a cascade pipeline run."""
    job = _cascade_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Cascade job {job_id} not found")
    return job


async def _run_cascade(job_id: str, req: CascadePipelineRequest) -> None:
    """Execute the full cascade pipeline."""
    _cascade_jobs[job_id]["status"] = "running"
    stages = _cascade_jobs[job_id]["stages"]

    try:
        import time as _time

        # --- Stage 1: Literature Scan ---
        stages["literature_scan"] = {"status": "running"}
        t0 = _time.perf_counter()

        from mica.memory.dlm.encoder import DLMEncoder
        from mica.memory.dlm.presets import get_dlm_preset

        # Apply DLM preset configuration
        try:
            preset_cfg = get_dlm_preset(req.preset)
        except KeyError:
            preset_cfg = get_dlm_preset("standard")

        max_papers = min(req.max_papers, preset_cfg["max_papers"])
        search = await _search_literature(
            query=req.query,
            max_papers=max_papers,
            sources=list(preset_cfg.get("sources", ["semantic_scholar"])),
            session_id=req.session_id,
            user_id=req.user_id,
            acquisition_budget_usd=req.acquisition_budget_usd,
        )
        papers = list(search["papers"])

        stages["literature_scan"] = {
            "status": "done",
            "papers_found": len(papers),
            "duration_s": round(_time.perf_counter() - t0, 2),
        }

        # --- Stage 2: Entity Extraction ---
        stages["entity_extraction"] = {"status": "running"}
        t1 = _time.perf_counter()

        encoder = DLMEncoder()
        all_entities: Dict[str, Dict[str, Any]] = {}
        paper_entities_map: Dict[str, List[str]] = {}  # paper_id → [entity_names]

        if preset_cfg.get("extract_entities", True):
            for paper in papers:
                text = f"{paper.get('title', '')}. {paper.get('abstract', '')}"
                if not text.strip(". "):
                    continue
                doc = encoder.encode(text)
                pid = paper.get("paperId", "")
                paper_entities_map[pid] = []
                for ent in doc.entities:
                    key = ent["text"].lower()
                    paper_entities_map[pid].append(key)
                    if key not in all_entities:
                        all_entities[key] = {"text": ent["text"], "type": ent["type"], "count": 0, "papers": []}
                    all_entities[key]["count"] += 1
                    if pid not in all_entities[key]["papers"]:
                        all_entities[key]["papers"].append(pid)

        stages["entity_extraction"] = {
            "status": "done",
            "entities_found": len(all_entities),
            "duration_s": round(_time.perf_counter() - t1, 2),
        }

        # --- Stage 3: KB Resolution (EntityMapper) ---
        stages["kb_resolution"] = {"status": "running"}
        t2 = _time.perf_counter()

        resolved_entities: Dict[str, Any] = {}
        try:
            from mica.memory.dlm.entity_mapper import EntityMapper
            mapper = EntityMapper()
            # Resolve top entities
            top_ents = sorted(all_entities.values(), key=lambda e: -e["count"])[:50]
            for ent_info in top_ents:
                try:
                    result = mapper.map_entity(ent_info["text"], ent_info.get("type"))
                    if result and result.kb_id:
                        resolved_entities[ent_info["text"]] = {
                            "kb_id": result.kb_id,
                            "kb_source": result.kb_source,
                            "confidence": result.confidence,
                        }
                except Exception:
                    pass
        except ImportError:
            logger.warning("EntityMapper not available in cascade")

        stages["kb_resolution"] = {
            "status": "done",
            "resolved": len(resolved_entities),
            "duration_s": round(_time.perf_counter() - t2, 2),
        }

        # --- Stage 4: ATOM Quintuples (if preset enables) ---
        atom_quintuples: List[Dict[str, Any]] = []
        if preset_cfg.get("run_atom", False):
            stages["atom_storage"] = {"status": "running"}
            t3 = _time.perf_counter()
            try:
                from mica.memory.atom.system import ATOMMemorySystem, ATOMMemoryConfig
                config = ATOMMemoryConfig()
                atom = ATOMMemorySystem(config)
                for paper in papers[:100]:  # Cap ATOM processing
                    text = f"{paper.get('title', '')}. {paper.get('abstract', '')}"
                    if not text.strip(". "):
                        continue
                    snapshot = await atom.store_experience(
                        experience=text,
                        observation_time=datetime.now(timezone.utc).replace(tzinfo=None),
                        metadata={"paper_id": paper.get("paperId"), "cascade": True},
                        user_id=req.user_id,
                    )
                    for q in snapshot.quintuples:
                        atom_quintuples.append({
                            "subject": q.subject,
                            "predicate": q.predicate,
                            "object": q.obj,
                            "observation_time": str(q.observation_time),
                        })
            except Exception as exc:
                logger.warning("ATOM processing failed in cascade: %s", exc)

            stages["atom_storage"] = {
                "status": "done",
                "quintuples": len(atom_quintuples),
                "duration_s": round(_time.perf_counter() - t3, 2),
            }

        # --- Stage 5: Milvus Indexing ---
        milvus_indexed = 0
        if req.enable_milvus:
            stages["milvus_indexing"] = {"status": "running"}
            t4 = _time.perf_counter()
            try:
                from mica.memory.milvus_integration import get_milvus_service
                svc = get_milvus_service(agent_id=req.user_id)
                if svc.is_connected:
                    milvus_indexed = svc.index_papers_batch(papers)
                    svc.flush()
            except Exception as exc:
                logger.warning("Milvus indexing failed in cascade: %s", exc)

            stages["milvus_indexing"] = {
                "status": "done",
                "indexed": milvus_indexed,
                "duration_s": round(_time.perf_counter() - t4, 2),
            }

        # --- Stage 6: Pharmacological Enrichment ---
        pharma_results: Dict[str, Any] = {}
        if req.enable_pharma and resolved_entities:
            stages["pharma_enrichment"] = {"status": "running"}
            t5 = _time.perf_counter()
            try:
                from mica.memory.dlm.pharma_enrichment import enrich_proteins_pharmacology
                protein_ids = [
                    v["kb_id"] for v in resolved_entities.values()
                    if v.get("kb_source") == "uniprot"
                ]
                if protein_ids:
                    pharma_results = await asyncio.to_thread(
                        enrich_proteins_pharmacology, protein_ids[:20]
                    )
            except Exception as exc:
                logger.warning("Pharma enrichment failed in cascade: %s", exc)

            stages["pharma_enrichment"] = {
                "status": "done",
                "proteins_enriched": len(pharma_results),
                "duration_s": round(_time.perf_counter() - t5, 2),
            }

        # --- Stage 7: DLM-LMP Convergence ---
        enriched_xml = None
        if req.enable_convergence and req.uniprot_id:
            stages["convergence"] = {"status": "running"}
            t6 = _time.perf_counter()
            try:
                from mica.memory.dlm_lmp.convergence import LMPDLMConvergence
                from pathlib import Path
                import os

                # Try to load existing LMP XML for this protein
                cache_dir = os.environ.get("LMP_CACHE_DIR", "lmp_cache")
                xml_path = Path(cache_dir) / "xml" / f"{req.uniprot_id}_v4.xml"
                if xml_path.exists():
                    lmp_xml = xml_path.read_text(encoding="utf-8")
                    conv = LMPDLMConvergence()
                    enriched_xml = conv.merge(
                        lmp_xml_string=lmp_xml,
                        dlm_papers=papers[:50],
                        atom_quintuples=atom_quintuples[:100],
                        user_id=req.user_id,
                    )
            except Exception as exc:
                logger.warning("DLM-LMP convergence failed in cascade: %s", exc)

            stages["convergence"] = {
                "status": "done",
                "xml_generated": enriched_xml is not None,
                "duration_s": round(_time.perf_counter() - t6, 2),
            }

        # --- Final Result ---
        sorted_entities = sorted(all_entities.values(), key=lambda e: -e["count"])

        _cascade_jobs[job_id]["status"] = "done"
        _cascade_jobs[job_id]["result"] = {
            "total_papers": len(papers),
            "total_entities": len(all_entities),
            "top_entities": sorted_entities[:50],
            "resolved_entities": resolved_entities,
            "atom_quintuples_count": len(atom_quintuples),
            "milvus_indexed": milvus_indexed,
            "pharma_enrichment": pharma_results,
            "convergence_xml_generated": enriched_xml is not None,
            "stages": stages,
        }

    except Exception as exc:
        logger.exception("Cascade pipeline %s failed", job_id)
        _cascade_jobs[job_id]["status"] = "error"
        _cascade_jobs[job_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# DLM-LMP Pipeline (3-Layer) endpoint
# ---------------------------------------------------------------------------


@router.post("/pipeline/process")
async def run_dlm_lmp_pipeline(
    text: str = Query(..., description="Document text to process through 3-layer pipeline"),
    enable_layer2: bool = Query(True, description="Enable metadata enrichment"),
    enable_layer3: bool = Query(False, description="Enable full NeSy generation"),
):
    """Run the 3-layer DLM-LMP pipeline: DLM-Lite → Metadata → Full NeSy.

    Exposes the previously unrouted DLM_LMP_Pipeline.process() method.
    """
    try:
        from pathlib import Path
        import os
        from mica.memory.dlm_lmp.pipeline import DLM_LMP_Pipeline

        cache_dir = os.environ.get("LMP_CACHE_DIR", "lmp_cache")
        pipeline = DLM_LMP_Pipeline(
            cache_dir=Path(cache_dir),
            enable_layer2=enable_layer2,
            enable_layer3=enable_layer3,
        )
        result = await asyncio.to_thread(pipeline.process, text)
        return result.to_dict()
    except ImportError as exc:
        raise HTTPException(501, f"DLM-LMP pipeline not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Pipeline processing failed: {exc}")


# ===================================================================
# P0–P3 Novel Feature Endpoints
# ===================================================================

# ── P0: Hypothesis Generator ──────────────────────────────────────

class HypothesisRequest(BaseModel):
    entities: List[str] = Field(..., description="Seed entities for hypothesis generation")
    max_hypotheses: int = Field(10, description="Max hypotheses to return")
    preset: Optional[str] = Field(None, description="DLM preset name (quick-scan, standard, deep-research, exhaustive, llm-context)")

@router.post("/hypothesis/generate")
async def generate_hypotheses(req: HypothesisRequest):
    """P0 — Generate research hypotheses from knowledge-graph gap analysis."""
    try:
        from mica.memory.dlm.hypothesis_generator import HypothesisGenerator
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        gen = HypothesisGenerator(pipeline_output=po)
        hypotheses = gen.generate(req.entities, max_results=req.max_hypotheses, pipeline_output=po)
        return {"count": len(hypotheses), "hypotheses": [h.to_dict() for h in hypotheses]}
    except ImportError as exc:
        raise HTTPException(501, f"HypothesisGenerator not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Hypothesis generation failed: {exc}")


# ── P0: Research Briefing ─────────────────────────────────────────

class BriefingRequest(BaseModel):
    query: str = Field(..., description="Protein name or research topic")
    scan_result: Optional[Dict[str, Any]] = Field(None, description="Pre-existing scan result dict")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/briefing/compile")
async def compile_briefing(req: BriefingRequest):
    """P0 — Compile a structured research briefing from scan results."""
    try:
        from mica.memory.dlm.research_briefing import ResearchBriefingGenerator
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        gen = ResearchBriefingGenerator(pipeline_output=po)
        scan = req.scan_result or {"query": req.query, "papers": [], "entities": {}}
        briefing = await gen.compile(req.query, scan_result=scan, pipeline_output=po)
        return {"briefing": briefing.to_dict(), "markdown": briefing.to_markdown()}
    except ImportError as exc:
        raise HTTPException(501, f"ResearchBriefingGenerator not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Briefing compilation failed: {exc}")


# ── P1: Drug Repurposing Alerts ───────────────────────────────────

class RepurposingRequest(BaseModel):
    protein: str = Field(..., description="Protein name (e.g. ABL1)")
    max_alerts: int = Field(20, description="Max alerts to return")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/repurposing/scan")
async def scan_repurposing(req: RepurposingRequest):
    """P1 — Scan for drug repurposing opportunities via 3 detection strategies."""
    try:
        from mica.memory.dlm.drug_repurposing import DrugRepurposingEngine
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        engine = DrugRepurposingEngine(pipeline_output=po)
        result = await engine.scan([req.protein], max_alerts=req.max_alerts, pipeline_output=po)
        return result
    except ImportError as exc:
        raise HTTPException(501, f"DrugRepurposingEngine not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Repurposing scan failed: {exc}")


# ── P1: Citation Impact ──────────────────────────────────────────

class CitationImpactRequest(BaseModel):
    entity: str = Field(..., description="Entity name for citation impact analysis")
    entity_type: str = Field("protein", description="Entity type (protein, gene, drug, disease)")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/citation-impact/analyse")
async def analyse_citation_impact(req: CitationImpactRequest):
    """P1 — Analyse citation velocity, burst detection, sleeping-beauty papers."""
    try:
        from mica.memory.dlm.citation_impact import CitationImpactTracker
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        tracker = CitationImpactTracker(pipeline_output=po)
        report = await tracker.analyse(req.entity, entity_type=req.entity_type, pipeline_output=po)
        return report.to_dict()
    except ImportError as exc:
        raise HTTPException(501, f"CitationImpactTracker not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Citation impact analysis failed: {exc}")


# ── P2: Knowledge Decay ──────────────────────────────────────────

class KnowledgeDecayRequest(BaseModel):
    entity: str = Field(..., description="Entity to analyse for knowledge decay")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/knowledge-decay/analyse")
async def analyse_knowledge_decay(req: KnowledgeDecayRequest):
    """P2 — Analyse confidence erosion of knowledge-graph facts over time."""
    try:
        from mica.memory.knowledge_decay import KnowledgeDecayAnalyser
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        analyser = KnowledgeDecayAnalyser(pipeline_output=po)
        report = await analyser.analyse_entity(req.entity, pipeline_output=po)
        return report.to_dict()
    except ImportError as exc:
        raise HTTPException(501, f"KnowledgeDecayAnalyser not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Knowledge-decay analysis failed: {exc}")


# ── P2: Conformational Landscape ─────────────────────────────────

class ConformationalRequest(BaseModel):
    uniprot_id: Optional[str] = Field(None, description="UniProt accession (e.g. P00519)")
    gene_name: Optional[str] = Field(None, description="Gene symbol (e.g. ABL1)")
    max_structures: int = Field(50, description="Max PDB structures to fetch")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/conformational/map")
async def map_conformational_landscape(req: ConformationalRequest):
    """P2 — Map the conformational landscape of a protein across PDB structures."""
    try:
        from mica.memory.conformational_landscape import ConformationalMapper
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        mapper = ConformationalMapper(pipeline_output=po)
        if req.uniprot_id:
            landscape = await mapper.map_protein(req.uniprot_id, max_structures=req.max_structures)
        elif req.gene_name:
            landscape = await mapper.map_protein_by_gene(req.gene_name, max_structures=req.max_structures)
        else:
            raise HTTPException(400, "Provide uniprot_id or gene_name")
        return landscape.to_dict()
    except HTTPException:
        raise
    except ImportError as exc:
        raise HTTPException(501, f"ConformationalMapper not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Conformational mapping failed: {exc}")


# ── P3: Pharmacovigilance Signals ────────────────────────────────

class PharmacovigilanceRequest(BaseModel):
    entity: str = Field(..., description="Protein or drug name")
    entity_type: str = Field("protein", description="protein or drug")
    max_drugs: int = Field(10, description="Max drugs to scan (protein mode)")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/pharmacovigilance/scan")
async def scan_pharmacovigilance(req: PharmacovigilanceRequest):
    """P3 — Scan for post-market safety signals via openFDA FAERS."""
    try:
        from mica.memory.dlm.pharmacovigilance import PharmacovigilanceEngine
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        engine = PharmacovigilanceEngine(pipeline_output=po)
        if req.entity_type == "drug":
            report = engine.scan_drug(req.entity)
        else:
            report = engine.scan_protein(req.entity, max_drugs=req.max_drugs, pipeline_output=po)
        return report.to_dict()
    except ImportError as exc:
        raise HTTPException(501, f"PharmacovigilanceEngine not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Pharmacovigilance scan failed: {exc}")


# ── P3: Ortholog Dashboard ───────────────────────────────────────

class OrthologRequest(BaseModel):
    gene_name: str = Field(..., description="Gene symbol (e.g. ABL1)")
    species: str = Field("homo_sapiens", description="Source species")
    preset: Optional[str] = Field(None, description="DLM preset name")

@router.post("/ortholog/dashboard")
async def build_ortholog_dashboard(req: OrthologRequest):
    """P3 — Cross-species ortholog dashboard with conservation & structure data."""
    try:
        from mica.memory.dlm.ortholog_dashboard import OrthologDashboard
        from mica.memory.pipeline_output import PipelineOutput
        po = PipelineOutput.build(preset_name=req.preset) if req.preset else None
        dash = OrthologDashboard(pipeline_output=po)
        result = dash.build(req.gene_name, species=req.species)
        return result.to_dict()
    except ImportError as exc:
        raise HTTPException(501, f"OrthologDashboard not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Ortholog dashboard failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════
# UNDERUTILIZED_DLM wiring — Internal Semantic Quality Layer
# ══════════════════════════════════════════════════════════════════════════


# ── JSON-LD Export (atom/jsonld.py) ──────────────────────────────────────

class JSONLDExportRequest(BaseModel):
    text: str = Field(..., description="Scientific text to decompose and serialise as JSON-LD")
    user_id: str = Field("agent", description="User ID")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Optional document metadata")


@router.post("/atom/jsonld")
async def export_atom_jsonld(req: JSONLDExportRequest):
    """Export an ATOM experience trace as a JSON-LD document.

    Runs the full ATOM pipeline (decompose → extract → merge) and then
    serialises the resulting trace with ``experience_trace_to_jsonld()``.
    """
    try:
        from mica.memory.atom.system import ATOMMemorySystem, ATOMMemoryConfig
        from mica.memory.atom.jsonld import experience_trace_to_jsonld

        config = ATOMMemoryConfig(enable_llm=False)
        system = ATOMMemorySystem(config=config)
        _snapshot, trace = await system.store_experience_with_trace(
            experience=req.text,
            metadata=req.metadata,
            user_id=req.user_id,
        )
        jsonld = experience_trace_to_jsonld(trace)
        return {"ok": True, "jsonld": jsonld}
    except ImportError as exc:
        raise HTTPException(501, f"ATOM JSON-LD not available: {exc}")
    except Exception as exc:
        logger.exception("JSON-LD export failed")
        raise HTTPException(500, f"JSON-LD export failed: {exc}")


# ── Event Replay / Time-Travel (event_store.py) ─────────────────────────

class EventReplayRequest(BaseModel):
    node_id: str = Field(..., description="Node ID to replay events for")
    event_type: Optional[str] = Field(None, description="Optional event type filter (e.g. 'citation', 'validation')")
    limit: int = Field(200, ge=1, le=5000, description="Max events to return")


@router.post("/events/replay")
async def replay_events(req: EventReplayRequest):
    """Replay all events for a node and reconstruct its current state.

    Wraps ``EventStore.get_events_for_node()`` + ``reconstruct_state_for_node()``
    to expose event-sourcing time-travel as an API.
    """
    try:
        from mica.memory.event_store import EventStore
        from mica.memory.events import EventType as ET_Enum
        import os

        conn_str = os.getenv("MICA_EVENT_STORE_DSN", "")
        if not conn_str:
            return {
                "ok": False,
                "error": "MICA_EVENT_STORE_DSN not configured — event store requires PostgreSQL",
                "state": {},
                "events": [],
            }

        store = EventStore(connection_string=conn_str)
        et_filter = None
        if req.event_type:
            try:
                et_filter = ET_Enum(req.event_type)
            except ValueError:
                pass

        events = store.get_events_for_node(
            req.node_id, event_type=et_filter, limit=req.limit,
        )
        state = store.reconstruct_state_for_node(req.node_id)
        return {
            "ok": True,
            "node_id": req.node_id,
            "state": state,
            "events": [e.to_dict() for e in events],
            "event_count": len(events),
        }
    except ImportError as exc:
        raise HTTPException(501, f"EventStore not available (psycopg2?): {exc}")
    except Exception as exc:
        logger.exception("Event replay failed")
        raise HTTPException(500, f"Event replay failed: {exc}")


@router.get("/events/count")
async def get_event_count(node_id: Optional[str] = Query(None)):
    """Return the total event count, optionally filtered by node."""
    try:
        from mica.memory.event_store import EventStore
        import os

        conn_str = os.getenv("MICA_EVENT_STORE_DSN", "")
        if not conn_str:
            return {"ok": False, "count": 0, "error": "MICA_EVENT_STORE_DSN not configured"}

        store = EventStore(connection_string=conn_str)
        count = store.get_event_count(node_id=node_id)
        return {"ok": True, "count": count, "node_id": node_id}
    except ImportError as exc:
        raise HTTPException(501, f"EventStore not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Event count failed: {exc}")


# ── DLM XML Export (dlm/xml_exporter.py) ─────────────────────────────────

class XMLExportRequest(BaseModel):
    text: str = Field(..., description="Scientific text (raw paper or abstract)")
    paper_id: str = Field("unknown", description="Paper identifier (PMC, DOI, arXiv)")
    with_kb_mapping: bool = Field(False, description="Enable KB entity mapping (requires EntityMapper)")


@router.post("/dlm/export-xml")
async def export_dlm_xml(req: XMLExportRequest):
    """Run DLMEncoder on input text and export structured XML files.

    Returns XML content as strings (paper_metadata, proteins, structures,
    compounds, entities_summary) so the caller doesn't need filesystem access.
    """
    try:
        import tempfile
        from mica.memory.dlm import DLMEncoder
        from mica.memory.dlm.xml_exporter import DLMXMLExporter, XMLExportResult

        encoder = DLMEncoder()
        encoded = encoder.encode(req.text)

        with tempfile.TemporaryDirectory(prefix="dlm_xml_") as tmpdir:
            exporter = DLMXMLExporter()
            result: XMLExportResult = exporter.export_all(
                encoded,
                output_dir=tmpdir,
                paper_id=req.paper_id,
                with_kb_mapping=req.with_kb_mapping,
            )

            # Read generated XMLs into response dict
            xml_contents: Dict[str, str] = {}
            for path in result.get_all_paths():
                xml_contents[path.stem] = path.read_text(encoding="utf-8")

        return {
            "ok": True,
            "paper_id": req.paper_id,
            "protein_count": result.protein_count,
            "structure_count": result.structure_count,
            "compound_count": result.compound_count,
            "total_entities": result.total_entities,
            "generation_time_ms": round(result.generation_time_ms, 1),
            "xml_files": xml_contents,
        }
    except ImportError as exc:
        raise HTTPException(501, f"DLM XML exporter not available: {exc}")
    except Exception as exc:
        logger.exception("DLM XML export failed")
        raise HTTPException(500, f"DLM XML export failed: {exc}")


# ── Section Classifier (dlm/classifier.py) ───────────────────────────────

class SectionClassifyRequest(BaseModel):
    headers: List[str] = Field(..., description="Section header texts to classify")


@router.post("/section/classify")
async def classify_section_headers(req: SectionClassifyRequest):
    """Classify scientific paper section headers using ML + heuristic fallback.

    Exposes ``SectionClassifier.classify_batch()`` as an API.
    """
    try:
        from mica.memory.dlm.classifier import SectionClassifier

        classifier = SectionClassifier()
        predictions = classifier.classify_batch(req.headers)
        return {
            "ok": True,
            "predictions": [
                {
                    "header": header,
                    "section": pred.section,
                    "confidence": round(pred.confidence, 4),
                    "method": pred.method,
                }
                for header, pred in zip(req.headers, predictions)
            ],
            "classifier_stats": classifier.get_stats(),
        }
    except ImportError as exc:
        raise HTTPException(501, f"SectionClassifier not available: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Section classification failed: {exc}")
