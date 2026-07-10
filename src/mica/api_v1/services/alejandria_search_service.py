from __future__ import annotations

import json
import logging
import math
import os
import re
import hashlib
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, Field

from mica.api_v1.product_schema import ensure_product_schema
from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
)
from mica.identity.request_identity import RequestIdentity
from mica.infrastructure.literature.scope_authority import resolve_literature_scope_authority
from mica.literature_consolidation.contracts.query_protocol import LiteratureQuerySpec
from mica.literature_consolidation.lmp_bibliotecario_handoff import (
    compile_lmp_bibliotecario_handoff,
)
from mica.literature_consolidation.provider_compiler import LiteratureProviderCompiler
from mica.memory.dlm.encoder import DLMEncoder
from mica.storage.gcs_user_storage import get_storage_manager, sanitize_object_prefix, storage_status

logger = logging.getLogger(__name__)

_POOL = None
_NS = {"l": "http://ai-university.edu/lmp/v4.0"}
_UNIPROT_RE = re.compile(
    r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b",
    re.I,
)
_PDB_RE = re.compile(r"\b[1-9][A-Za-z0-9]{3}\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TOKEN_RE = re.compile(r"[A-Za-z0-9\-_/]+")
_CURRENT_YEAR = datetime.now(timezone.utc).year
_SOURCE_CONFIDENCE = {
    "semantic_scholar": 0.92,
    "pubmed": 0.9,
    "openalex": 0.82,
    "biorxiv": 0.7,
}
_LIBRARY_KNOWN_ARTIFACTS = {
    "library_manifest.json",
    "dlm_resource_manifest.json",
    "request.json",
    "provider_receipts.jsonl",
    "raw_hits.jsonl",
    "normalized_hits.jsonl",
    "deduped_hits.jsonl",
    "abstracts.jsonl",
    "fulltext_availability.json",
    "free_pdf_audit.json",
    "section_figure_index.json",
    "dlm_receipt.json",
    "dlm_entities.json",
    "dlm_sections.json",
    "ranking_trace.json",
    "run_summary.json",
}


class AlejandriaSearchFilters(BaseModel):
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    publication_types: List[str] = Field(default_factory=list)
    journals: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)
    authors: List[str] = Field(default_factory=list)
    organisms: List[str] = Field(default_factory=list)
    proteins: List[str] = Field(default_factory=list)
    genes: List[str] = Field(default_factory=list)
    methods: List[str] = Field(default_factory=list)
    providers: List[str] = Field(default_factory=list)
    open_access_only: bool = False
    has_fulltext: bool = False
    has_lmp_context: bool = False
    has_bsm_context: bool = False
    citation_count_min: int = 0


class AlejandriaProviderPolicy(BaseModel):
    max_hits: int = Field(20, ge=1, le=100)
    max_provider_pages: int = Field(1, ge=1, le=20)
    providers_requested: List[str] = Field(default_factory=list)
    allow_degraded: bool = True
    provider_timeout_s: float = Field(15.0, ge=1.0, le=120.0)


class AlejandriaExpansionPolicy(BaseModel):
    use_dlm: bool = True
    use_lmp: bool = True
    use_bsm: bool = True
    use_kb_context: bool = False
    use_citation_expansion: bool = False


class AlejandriaRankingPolicy(BaseModel):
    relevance_weight: float = 0.3
    semantic_weight: float = 0.15
    recency_weight: float = 0.1
    citation_weight: float = 0.15
    lmp_crossref_weight: float = 0.15
    bsm_context_weight: float = 0.05
    exact_entity_weight: float = 0.1


class AlejandriaPaperSeed(BaseModel):
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    paper_id: str = ""
    openalex_id: str = ""
    title: str = ""


class AlejandriaSearchRequest(BaseModel):
    query_text: str = Field(..., min_length=1)
    search_mode: str = Field("auto")
    workspace_id: str = ""
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    query_groups: List[str] = Field(default_factory=list)
    paper_seed: Optional[AlejandriaPaperSeed] = None
    citation_depth: int = Field(0, ge=0, le=2)
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0)
    allow_paid_fulltext: bool = False
    cache_write_scope: str = ""
    enable_gcs_artifacts: bool = True
    artifact_prefix: str = ""
    filters: AlejandriaSearchFilters = Field(default_factory=AlejandriaSearchFilters)
    provider_policy: AlejandriaProviderPolicy = Field(default_factory=AlejandriaProviderPolicy)
    expansion_policy: AlejandriaExpansionPolicy = Field(default_factory=AlejandriaExpansionPolicy)
    ranking_policy: AlejandriaRankingPolicy = Field(default_factory=AlejandriaRankingPolicy)


class EntityDetectRequest(BaseModel):
    query_text: str = Field(..., min_length=1)
    snippet: str = ""


class LMPExpandRequest(BaseModel):
    query_text: str = Field(..., min_length=1)
    workspace_id: str = ""
    kb_id: str = ""


class BSMContextRequest(BaseModel):
    query_text: str = Field(..., min_length=1)
    proteins: List[str] = Field(default_factory=list)
    genes: List[str] = Field(default_factory=list)


class PromoteToKBRequest(BaseModel):
    kb_id: str = Field(..., min_length=1)
    hit_ids: List[str] = Field(default_factory=list)
    minimum_evidentiality_score: float = Field(0.5, ge=0.0, le=1.0)


class PromoteToWorkingSetRequest(BaseModel):
    hit_ids: List[str] = Field(default_factory=list)
    working_set_id: Optional[str] = None
    study_id: Optional[str] = None
    title: str = ""
    description: str = ""


class AttachToStudyRequest(BaseModel):
    study_id: str = Field(..., min_length=1)
    hit_ids: List[str] = Field(default_factory=list)


@dataclass
class _ParsedLMPContext:
    source_path: str
    accession: str
    protein_name: str
    genes: List[str]
    organism: str
    pdb_ids: List[str]
    string_ids: List[str]
    reactome_ids: List[str]
    open_targets_ids: List[str]
    kegg_ids: List[str]
    go_ids: List[str]
    chembl_ids: List[str]
    pubchem_ids: List[str]
    interaction_edges: List[Dict[str, Any]]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_jsonb(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return default
    return default


def _tokenize(text: str) -> List[str]:
    return [token for token in _TOKEN_RE.findall(str(text or "").lower()) if token]


def _dedupe_texts(values: Iterable[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(value)
    return ordered


def _dedupe_query_contributions(values: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(values or []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        variant_kind = str(item.get("variant_kind") or "").strip()
        query_text = str(item.get("query_text") or "").strip()
        key = f"{source.casefold()}|{variant_kind.casefold()}|{query_text.casefold()}"
        if not source and not query_text:
            continue
        if key in seen:
            continue
        seen.add(key)
        ordered.append(
            {
                "source": source,
                "variant_kind": variant_kind,
                "query_text": query_text,
                "requested_params": dict(item.get("requested_params") or {}),
            }
        )
    return ordered


def _dedupe_gcs_refs(values: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(values or []):
        if not isinstance(item, dict):
            continue
        key = "|".join(
            [
                str(item.get("kind") or "").strip().casefold(),
                str(item.get("object_path") or "").strip(),
                str(item.get("gcs_uri") or "").strip(),
                str(item.get("hit_id") or "").strip(),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        ordered.append(dict(item))
    return ordered


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(title or "").casefold())


def _coerce_author_names(authors: Any) -> List[str]:
    names: List[str] = []
    for item in list(authors or []):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return names


def _coerce_provider_sources(paper: Dict[str, Any]) -> List[str]:
    sources = list(paper.get("provider_sources") or [])
    provider = str(paper.get("provider") or "").strip()
    if provider:
        sources.append(provider)
    return _dedupe_texts(sources)


def _coerce_provider_ids(paper: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    provider_id = str(paper.get("provider_id") or "").strip()
    paper_id = str(paper.get("paperId") or paper.get("paper_id") or "").strip()
    if provider_id:
        values.append(provider_id)
    if paper_id:
        values.append(paper_id)
    external_ids = paper.get("externalIds") or {}
    if isinstance(external_ids, dict):
        for key, value in external_ids.items():
            if value:
                values.append(f"{key}:{value}")
    return _dedupe_texts(values)


def _dedupe_identity_key(hit: Dict[str, Any]) -> str:
    for key in ("doi", "pmid", "pmcid"):
        value = str(hit.get(key) or "").strip()
        if value:
            return f"{key}:{value.casefold()}"
    provider_ids = [
        str(value).strip()
        for value in list(hit.get("provider_ids") or [])
        if str(value).strip()
    ]
    if provider_ids:
        return f"provider:{sorted(value.casefold() for value in provider_ids)[0]}"
    title_key = _normalize_title_key(hit.get("title") or "")
    publication_year = str(hit.get("publication_year") or "").strip()
    journal_key = _normalize_title_key(hit.get("journal") or "")
    if title_key and publication_year and journal_key:
        return f"title-year-journal:{title_key}|{publication_year}|{journal_key}"
    if title_key:
        return f"title:{title_key}"
    return str(hit.get("hit_id") or "")


def _merge_duplicate_hits(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    existing_score = float(((existing.get("scores") or {}).get("final_score") or 0.0))
    incoming_score = float(((incoming.get("scores") or {}).get("final_score") or 0.0))
    preferred = dict(incoming if incoming_score > existing_score else existing)
    secondary = incoming if incoming_score > existing_score else existing

    for key in (
        "title",
        "abstract",
        "snippet",
        "journal",
        "doi",
        "pmid",
        "pmcid",
        "publication_year",
        "publication_date",
        "open_access_status",
        "fulltext_status",
        "fulltext_availability_kind",
    ):
        if not preferred.get(key) and secondary.get(key):
            preferred[key] = secondary.get(key)

    preferred["provider_sources"] = _dedupe_texts([*existing.get("provider_sources", []), *incoming.get("provider_sources", [])])
    preferred["provider_ids"] = _dedupe_texts([*existing.get("provider_ids", []), *incoming.get("provider_ids", [])])
    preferred["authors"] = _dedupe_texts([*existing.get("authors", []), *incoming.get("authors", [])])
    preferred["lmp_matches"] = _dedupe_texts([*existing.get("lmp_matches", []), *incoming.get("lmp_matches", [])])
    preferred["genes"] = _dedupe_texts([*existing.get("genes", []), *incoming.get("genes", [])])
    preferred["proteins"] = _dedupe_texts([*existing.get("proteins", []), *incoming.get("proteins", [])])
    preferred["organisms"] = _dedupe_texts([*existing.get("organisms", []), *incoming.get("organisms", [])])
    preferred["query_contributions"] = _dedupe_query_contributions(
        [*existing.get("query_contributions", []), *incoming.get("query_contributions", [])]
    )
    preferred["reasons"] = _dedupe_texts([*existing.get("reasons", []), *incoming.get("reasons", [])])
    preferred["ranking_reasons"] = _dedupe_texts(
        [*existing.get("ranking_reasons", []), *incoming.get("ranking_reasons", []), *preferred.get("reasons", [])]
    )
    preferred["limitations"] = _dedupe_texts([*existing.get("limitations", []), *incoming.get("limitations", [])])
    preferred["gcs_artifact_refs"] = _dedupe_gcs_refs(
        [*existing.get("gcs_artifact_refs", []), *incoming.get("gcs_artifact_refs", [])]
    )
    preferred["citation_count"] = max(int(existing.get("citation_count") or 0), int(incoming.get("citation_count") or 0))
    preferred["raw_payload"] = preferred.get("raw_payload") or incoming.get("raw_payload") or existing.get("raw_payload") or {}
    return preferred


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    import asyncpg

    dsn = choose_neon_database_url()
    if not dsn:
        raise RuntimeError("Database not configured")
    _POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        **asyncpg_connection_kwargs_for_database_url(dsn),
    )
    return _POOL


def _lmp_search_roots() -> List[Path]:
    roots: List[Path] = []
    override = str(os.getenv("MICA_LMP_V4_DIR") or "").strip()
    if override:
        roots.append(Path(override).expanduser().resolve())
    repo_root = Path(__file__).resolve().parents[4]
    roots.append((repo_root / ".tmp_lmp_v4").resolve())
    roots.append((repo_root / "src" / "bsm" / "lmp" / "test_output_v4_enriched" / "full").resolve())
    deduped: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _find_candidate_lmp_files(query_text: str, signals: Dict[str, List[str]]) -> List[Path]:
    candidate_tokens = _dedupe_texts(
        [
            *signals.get("proteins", []),
            *signals.get("genes", []),
            *signals.get("uniprot_ids", []),
            *signals.get("pdb_ids", []),
            query_text,
        ]
    )
    normalized_tokens = [re.sub(r"[^A-Za-z0-9]+", "", token).upper() for token in candidate_tokens if token]
    matched: List[Path] = []
    seen: set[str] = set()
    for root in _lmp_search_roots():
        if not root.exists():
            continue
        for path in root.glob("*.xml"):
            stem = path.stem.upper()
            if any(token and token in stem for token in normalized_tokens):
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    matched.append(path)
        if matched:
            break
    return matched[:5]


def _extract_lmp_texts(root: ET.Element, xpath: str, attr: str = "") -> List[str]:
    values: List[str] = []
    for node in root.findall(xpath, _NS):
        if attr:
            value = str(node.get(attr) or "").strip()
        else:
            value = str(node.text or "").strip()
        if value:
            values.append(value)
    return _dedupe_texts(values)


def _parse_lmp_context(path: Path) -> Optional[_ParsedLMPContext]:
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse LMP XML %s: %s", path, exc)
        return None

    accession = str(path.stem.split("_")[0] or "").strip().upper()
    protein_name = str(root.findtext(".//l:Semantics/l:ProteinName", default="", namespaces=_NS) or "").strip()
    genes = _extract_lmp_texts(root, ".//l:Semantics//l:Gene", "name")
    organism = str(root.find(".//l:Identity/l:Organism", _NS).get("name") if root.find(".//l:Identity/l:Organism", _NS) is not None else "").strip()

    xrefs_by_db: Dict[str, List[str]] = {}
    for node in root.findall(".//l:CrossReference", _NS):
        db_name = str(node.get("db") or "").strip()
        identifier = str(node.get("id") or "").strip()
        if not db_name or not identifier:
            continue
        xrefs_by_db.setdefault(db_name.casefold(), []).append(identifier)

    interaction_edges: List[Dict[str, Any]] = []
    for edge in root.findall(".//l:Edge", _NS):
        edge_type = str(edge.get("type") or "").strip().upper()
        db_name = str(edge.get("db") or "").strip()
        if edge_type != "INTERACTS_WITH" or db_name.casefold() != "string":
            continue
        interaction_edges.append(
            {
                "partner": str(edge.get("target") or "").strip(),
                "matched_identifier": str(edge.get("id") or "").strip(),
                "score": _safe_float(edge.get("score")) or 0.0,
                "source_db": db_name,
            }
        )

    return _ParsedLMPContext(
        source_path=str(path),
        accession=accession,
        protein_name=protein_name,
        genes=genes,
        organism=organism,
        pdb_ids=_dedupe_texts(xrefs_by_db.get("pdb", [])),
        string_ids=_dedupe_texts(xrefs_by_db.get("string", [])),
        reactome_ids=_dedupe_texts(xrefs_by_db.get("reactome", [])),
        open_targets_ids=_dedupe_texts(xrefs_by_db.get("opentargets", [])),
        kegg_ids=_dedupe_texts(xrefs_by_db.get("kegg", [])),
        go_ids=_dedupe_texts(xrefs_by_db.get("go", [])),
        chembl_ids=_dedupe_texts(xrefs_by_db.get("chembl", [])),
        pubchem_ids=_dedupe_texts(xrefs_by_db.get("pubchem", [])),
        interaction_edges=interaction_edges[:25],
    )


class AlejandriaSearchService:
    async def execute_search(
        self,
        *,
        request: AlejandriaSearchRequest,
        user_id: str,
        request_identity: RequestIdentity | str | None = None,
    ) -> Dict[str, Any]:
        scope_authority = resolve_literature_scope_authority(
            user_id=user_id,
            request_identity=request_identity,
            requested_tenant_id=request.workspace_id,
            requested_cache_write_scope=request.cache_write_scope,
        )
        await ensure_product_schema()
        run_id = f"asr_{uuid.uuid4().hex[:16]}"
        request = request.model_copy(
            update={
                "workspace_id": scope_authority.tenant_id or request.workspace_id,
                "cache_write_scope": scope_authority.cache_write_scope or request.cache_write_scope,
            }
        )
        detected = self.detect_entities(
            EntityDetectRequest(query_text=request.query_text),
        )
        lmp_context = self.expand_lmp_context(
            LMPExpandRequest(query_text=request.query_text, workspace_id=request.workspace_id, kb_id=request.kb_id or ""),
            precomputed_entities=detected,
        )
        bsm_context = self.build_bsm_context(
            BSMContextRequest(
                query_text=request.query_text,
                proteins=list(detected.get("proteins", [])),
                genes=list(detected.get("genes", [])),
            ),
            precomputed_entities=detected,
            precomputed_lmp=lmp_context,
        )

        provider_sources = _dedupe_texts(
            request.provider_policy.providers_requested
            or request.filters.providers
            or ["semantic_scholar", "pubmed", "openalex"]
        )
        spec = LiteratureQuerySpec(
            query=request.query_text.strip(),
            entities=_dedupe_texts(
                [
                    *detected.get("proteins", []),
                    *detected.get("genes", []),
                    *detected.get("organisms", []),
                ]
            ),
            max_papers=int(request.provider_policy.max_hits),
            sources=provider_sources,
            lane="driver_search",
            session_id=run_id,
            run_id=run_id,
            user_id=user_id,
        )
        plan = LiteratureProviderCompiler(
            lane_class="driver_search",
            preset_name="alejandria_search",
            openalex_available=True,
        ).compile_plan(spec)

        expansion_variants = self._collect_query_variants(
            request=request,
            lmp_context=lmp_context,
        )
        paper_identity_targets = self._build_paper_identity_targets(request.paper_seed)
        citation_mode_enabled = bool(
            request.expansion_policy.use_citation_expansion
            or request.search_mode in {"citation_graph", "paper_seed_citation"}
            or int(request.citation_depth or 0) > 0
        )
        limitations: List[str] = []
        if request.expansion_policy.use_lmp and lmp_context.get("status") != "available":
            limitations.append("lmp_context_unavailable")
        if request.expansion_policy.use_bsm and bsm_context.get("status") != "available":
            limitations.append("bsm_context_unavailable")
        limitations.append("semantic_score_is_provider_proxy_not_biolinkbert_runtime")
        if request.search_mode == "kb":
            limitations.append("kb_native_search_mode_not_yet_distinct_from_literature_mode")

        citation_graph: Dict[str, List[str]] = {}
        from mica.services.literature_search_service import LiteratureSearchService

        async with LiteratureSearchService() as literature_service:
            if citation_mode_enabled:
                result = await literature_service.deep_search(
                    query=self._resolved_search_query(request),
                    entities=expansion_variants,
                    paper_identity_targets=paper_identity_targets,
                    max_papers=spec.max_papers,
                    citation_depth=max(1, int(request.citation_depth or 1)),
                    sources=[item.value for item in plan.acquisition_order],
                    session_id=run_id,
                    run_id=run_id,
                    user_id=user_id,
                    tenant_id=request.workspace_id or None,
                    cache_write_scope=str(request.cache_write_scope or "").strip() or None,
                    acquisition_budget_usd=request.acquisition_budget_usd,
                    allow_paid_fulltext=request.allow_paid_fulltext,
                    retrieval_policy={
                        "alejandria_search_mode": request.search_mode,
                        "provider_timeout_s": request.provider_policy.provider_timeout_s,
                        "query_variants": expansion_variants,
                        "paper_seed": request.paper_seed.model_dump(mode="json") if request.paper_seed else None,
                    },
                )
                citation_graph = dict(getattr(result, "citation_graph", {}) or {})
            else:
                result = await literature_service.search(
                    query=self._resolved_search_query(request),
                    max_papers=spec.max_papers,
                    sources=[item.value for item in plan.acquisition_order],
                    extra_queries=expansion_variants,
                    paper_identity_targets=paper_identity_targets,
                    session_id=run_id,
                    run_id=run_id,
                    user_id=user_id,
                    tenant_id=request.workspace_id or None,
                    cache_write_scope=str(request.cache_write_scope or "").strip() or None,
                    acquisition_budget_usd=request.acquisition_budget_usd,
                    allow_paid_fulltext=request.allow_paid_fulltext,
                    retrieval_policy={
                        "alejandria_search_mode": request.search_mode,
                        "provider_timeout_s": request.provider_policy.provider_timeout_s,
                        "query_variants": expansion_variants,
                        "paper_seed": request.paper_seed.model_dump(mode="json") if request.paper_seed else None,
                    },
                )

        provider_attempts = self._build_provider_attempts(plan, result)
        provider_failures = list(result.failure_records or [])
        if not request.provider_policy.allow_degraded and provider_failures:
            limitations.append("provider_failures_present_allow_degraded_false")

        raw_papers = list(result.papers or [])
        raw_hits = self._normalize_hits(
            run_id=run_id,
            papers=raw_papers,
            request=request,
            query_entities=detected,
            lmp_context=lmp_context,
            bsm_context=bsm_context,
        )
        deduped_hits = self._dedupe_hits(raw_hits)
        ranked_hits = self._apply_ranking_v1(
            hits=deduped_hits,
            query_variants=[spec.query, *expansion_variants],
            citation_graph=citation_graph,
        )
        ranked_hits = sorted(ranked_hits, key=lambda item: float(((item.get("scores") or {}).get("final_score") or 0.0)), reverse=True)
        analytics = self._build_analytics(
            hits=ranked_hits,
            provider_attempts=provider_attempts,
            query_variants=expansion_variants,
        )
        analytics["query_contribution_map"] = self._build_query_contribution_map(ranked_hits)
        analytics["dedup_clusters"] = self._build_dedup_clusters(raw_hits)
        analytics["fulltext_availability"] = self._build_fulltext_availability_summary(ranked_hits)
        analytics["citation_graph"] = {
            "enabled": citation_mode_enabled,
            "edge_count": sum(len(list(values or [])) for values in citation_graph.values()),
            "node_count": len(citation_graph),
        }
        analytics["query_expansion_contribution"]["citation_expanded_hits"] = sum(
            1 for hit in ranked_hits if float(((hit.get("scores") or {}).get("citation_graph_score") or 0.0)) > 0.0
        )

        gcs_artifact_result = self._persist_run_artifacts_to_gcs(
            run_id=run_id,
            user_id=user_id,
            request=request,
            raw_papers=raw_papers,
            raw_hits=raw_hits,
            deduped_hits=ranked_hits,
            provider_attempts=provider_attempts,
            provider_failures=provider_failures,
            analytics=analytics,
            citation_graph=citation_graph,
        )
        if str(gcs_artifact_result.get("status") or "") != "written":
            limitations.append(str(gcs_artifact_result.get("limitation") or "gcs_artifact_pipeline_unavailable"))
        hit_gcs_refs = {
            str(hit_id): list(refs or [])
            for hit_id, refs in dict(gcs_artifact_result.get("hit_refs") or {}).items()
        }
        for hit in ranked_hits:
            hit["gcs_artifact_refs"] = list(hit_gcs_refs.get(str(hit.get("hit_id") or ""), []))

        run_payload = {
            "run_id": run_id,
            "user_id": user_id,
            "workspace_id": request.workspace_id,
            "study_id": request.study_id,
            "kb_id": request.kb_id,
            "working_set_id": request.working_set_id,
            "query_text": request.query_text,
            "search_mode": request.search_mode,
            "query_spec_hash": spec.query_spec_hash,
            "status": "completed",
            "created_at": _now_utc(),
            "completed_at": _now_utc(),
            "request": request.model_dump(mode="json"),
            "provider_execution_plan": plan.to_dict(),
            "expansion_trace": list(lmp_context.get("expansion_trace", [])),
            "provider_attempts": provider_attempts,
            "provider_failures": provider_failures,
            "hit_count_raw": len(raw_hits),
            "hit_count_deduped": len(deduped_hits),
            "hit_count_ranked": len(ranked_hits),
            "analytics_summary": analytics,
            "artifact_refs": [],
            "gcs_artifact_refs": list(gcs_artifact_result.get("run_refs") or []),
            "kb_refs": [],
            "limitations": _dedupe_texts(limitations + list(lmp_context.get("limitations", [])) + list(bsm_context.get("limitations", []))),
            "trace_payload": {
                "query_entities": detected,
                "lmp_context": lmp_context,
                "bsm_context": bsm_context,
                "scope_authority": scope_authority.to_dict(),
                "search_log": list(result.search_log or []),
                "source_health": dict(result.source_health or {}),
                "request_envelope": dict(result.request_envelope or {}),
                "citation_graph": citation_graph,
                "provider_controls": dict((result.request_envelope or {}).get("provider_controls") or {}),
                "gcs_artifact_manifest": dict(gcs_artifact_result.get("manifest") or {}),
            },
        }

        await self._persist_run_and_hits(run_payload=run_payload, hits=ranked_hits)
        return {
            "run": run_payload,
            "hits": ranked_hits,
            "analytics": analytics,
        }

    def detect_entities(self, request: EntityDetectRequest) -> Dict[str, Any]:
        text = " ".join(part for part in [request.query_text, request.snippet] if str(part).strip()).strip()
        encoded = DLMEncoder().encode(text)
        grouped: Dict[str, List[str]] = {
            "proteins": [],
            "genes": [],
            "organisms": [],
            "diseases": [],
            "pathways": [],
            "methods": [],
            "chemicals": [],
            "institutions": [],
            "journals": [],
            "authors": [],
            "uniprot_ids": [],
            "pdb_ids": [],
            "years": [],
        }
        for entity in list(encoded.entities or []):
            etype = str(entity.get("type") or entity.get("entity_type") or "").strip().upper()
            value = str(entity.get("text") or "").strip()
            if not value:
                continue
            if etype in {"PROT", "PROTEIN", "PROTEINS"}:
                grouped["proteins"].append(value)
            elif etype in {"GENE", "GENES"}:
                grouped["genes"].append(value)
            elif etype in {"ORG", "ORGANISM", "ORGANISMS", "SPECIES"}:
                grouped["organisms"].append(value)
            elif etype in {"DISEASE", "DISEASES", "DISO"}:
                grouped["diseases"].append(value)
            elif etype in {"PATHWAY", "PATHWAYS"}:
                grouped["pathways"].append(value)
            elif etype in {"METHOD", "METHODS"}:
                grouped["methods"].append(value)
            elif etype in {"CHEM", "CHEMICAL", "CHEMICALS", "DRUG", "DRUGS"}:
                grouped["chemicals"].append(value)
        grouped["uniprot_ids"] = _dedupe_texts(_UNIPROT_RE.findall(text))
        grouped["pdb_ids"] = _dedupe_texts(
            [
                match.upper()
                for match in _PDB_RE.findall(text)
                if any(char.isalpha() for char in match[1:])
            ]
        )
        grouped["years"] = _dedupe_texts(_YEAR_RE.findall(text))
        for key in list(grouped.keys()):
            grouped[key] = _dedupe_texts(grouped[key])
        return {
            "query_text": request.query_text,
            "source_text": text,
            "entities": list(encoded.entities or []),
            **grouped,
        }

    def expand_lmp_context(
        self,
        request: LMPExpandRequest,
        *,
        precomputed_entities: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entities = precomputed_entities or self.detect_entities(EntityDetectRequest(query_text=request.query_text))
        candidates = _find_candidate_lmp_files(request.query_text, entities)
        parsed: List[_ParsedLMPContext] = [ctx for ctx in (_parse_lmp_context(path) for path in candidates) if ctx is not None]
        if not parsed:
            return {
                "status": "unavailable",
                "query_text": request.query_text,
                "records": [],
                "query_variants": [],
                "expansion_trace": [],
                "limitations": ["lmp_context_unavailable"],
            }

        context = parsed[0]
        aliases = _dedupe_texts([context.accession, context.protein_name, *context.genes])
        pathway_terms = _dedupe_texts([*context.reactome_ids, *context.kegg_ids, *context.go_ids, *context.open_targets_ids])
        partner_terms = _dedupe_texts([edge.get("partner") or "" for edge in context.interaction_edges])
        handoff_payload = {
            "identity": {
                "accession": context.accession,
                "protein_name": context.protein_name,
                "genes": context.genes,
                "organism": context.organism,
            },
            "pdb_ids": context.pdb_ids,
            "crossrefs": {
                "string": context.string_ids,
                "reactome": context.reactome_ids,
                "open_targets": context.open_targets_ids,
                "kegg": context.kegg_ids,
                "go": context.go_ids,
                "chembl": context.chembl_ids,
                "pubchem": context.pubchem_ids,
            },
            "interaction_partners": partner_terms,
        }
        handoff = compile_lmp_bibliotecario_handoff(
            query=request.query_text,
            entities=aliases,
            pdb_ids=context.pdb_ids,
            lmp_handoff=handoff_payload,
            require_full_text=True,
        )
        query_variants = _dedupe_texts(
            [
                *aliases,
                *(f"{alias} {context.organism}" for alias in aliases if context.organism),
                *context.pdb_ids,
                *partner_terms[:8],
                *pathway_terms[:8],
                *list(handoff.get("extra_queries") or []),
            ]
        )[:32]

        expansion_trace: List[Dict[str, Any]] = []
        for alias in aliases:
            expansion_trace.append(
                {"source": "lmp_xml", "field": "alias", "value": alias, "weight": 1.0, "used_in_query_variant": alias in query_variants}
            )
        for pdb_id in context.pdb_ids:
            expansion_trace.append(
                {"source": "lmp_xml", "field": "pdb_id", "value": pdb_id, "weight": 0.8, "used_in_query_variant": pdb_id in query_variants}
            )
        for partner in partner_terms[:8]:
            expansion_trace.append(
                {"source": "lmp_xml_string_snapshot", "field": "interaction_partner", "value": partner, "weight": 0.55, "used_in_query_variant": partner in query_variants}
            )
        for term in pathway_terms[:8]:
            expansion_trace.append(
                {"source": "lmp_xml", "field": "pathway_or_context", "value": term, "weight": 0.35, "used_in_query_variant": term in query_variants}
            )

        return {
            "status": "available",
            "query_text": request.query_text,
            "records": [
                {
                    "source_path": context.source_path,
                    "accession": context.accession,
                    "protein_name": context.protein_name,
                    "genes": context.genes,
                    "organism": context.organism,
                    "pdb_ids": context.pdb_ids,
                    "string_ids": context.string_ids,
                    "reactome_ids": context.reactome_ids,
                    "open_targets_ids": context.open_targets_ids,
                    "kegg_ids": context.kegg_ids,
                    "go_ids": context.go_ids,
                    "chembl_ids": context.chembl_ids,
                    "pubchem_ids": context.pubchem_ids,
                }
            ],
            "query_variants": query_variants,
            "expansion_trace": expansion_trace,
            "limitations": ["lmp_context_from_local_xml_snapshot"],
            "handoff": handoff,
        }

    def build_bsm_context(
        self,
        request: BSMContextRequest,
        *,
        precomputed_entities: Optional[Dict[str, Any]] = None,
        precomputed_lmp: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entities = precomputed_entities or self.detect_entities(EntityDetectRequest(query_text=request.query_text))
        lmp_context = precomputed_lmp or self.expand_lmp_context(
            LMPExpandRequest(query_text=request.query_text),
            precomputed_entities=entities,
        )
        records = list(lmp_context.get("records") or [])
        if not records:
            return {
                "status": "unavailable",
                "query_text": request.query_text,
                "contexts": [],
                "limitations": ["bsm_context_unavailable"],
            }
        record = records[0]
        parsed = _parse_lmp_context(Path(record["source_path"]))
        if parsed is None:
            return {
                "status": "unavailable",
                "query_text": request.query_text,
                "contexts": [],
                "limitations": ["bsm_context_unavailable"],
            }
        top_partners = [
            {
                "partner": edge.get("partner"),
                "score": edge.get("score"),
                "matched_identifier": edge.get("matched_identifier"),
            }
            for edge in parsed.interaction_edges[:10]
        ]
        context_payload = {
            "protein_id": parsed.accession or (parsed.genes[0] if parsed.genes else ""),
            "matched_identifier": (parsed.string_ids[0] if parsed.string_ids else parsed.accession),
            "interaction_count": len(parsed.interaction_edges),
            "top_partners": top_partners,
            "confidence_scores": [edge.get("score") for edge in parsed.interaction_edges[:10]],
            "source_db": "lmp_xml_string_snapshot",
            "limitations": ["read_only_snapshot_context"],
        }
        return {
            "status": "available",
            "query_text": request.query_text,
            "contexts": [context_payload],
            "limitations": ["bsm_context_from_lmp_xml_snapshot"],
        }

    async def get_run(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM alejandria_search_runs WHERE run_id = $1 AND user_id = $2",
                run_id,
                user_id,
            )
        if not row:
            raise KeyError(run_id)
        return self._row_to_run(row)

    async def list_hits(
        self,
        *,
        run_id: str,
        user_id: str,
        filters: Optional[AlejandriaSearchFilters] = None,
        sort_by: str = "final_score",
        descending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM alejandria_search_hits WHERE run_id = $1 ORDER BY created_at ASC",
                run_id,
            )
        hits = [self._row_to_hit(row) for row in rows]
        if not hits:
            storage_ctx = self._build_library_storage_context(run)
            if self._artifact_exists(storage_ctx, "deduped_hits.jsonl"):
                hits = self._read_jsonl_artifact(storage_ctx, "deduped_hits.jsonl")
        filtered = [hit for hit in hits if self._hit_matches_filters(hit, filters or AlejandriaSearchFilters())]
        reverse = bool(descending)
        filtered.sort(key=lambda item: self._sort_value(item, sort_by), reverse=reverse)
        return {
            "run_id": run_id,
            "status": run["status"],
            "total": len(filtered),
            "hits": filtered[offset : offset + limit],
        }

    async def get_analytics(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        return dict(run.get("analytics_summary") or {})

    async def get_trace(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        return {
            "run_id": run_id,
            "provider_execution_plan": run.get("provider_execution_plan", {}),
            "expansion_trace": run.get("expansion_trace", []),
            "provider_attempts": run.get("provider_attempts", []),
            "provider_failures": run.get("provider_failures", []),
            "trace_payload": run.get("trace_payload", {}),
        }

    async def get_manifest(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        manifest_name = "library_manifest.json"
        if self._artifact_exists(storage_ctx, manifest_name):
            manifest = self._read_json_artifact(storage_ctx, manifest_name)
            if isinstance(manifest, dict):
                manifest.setdefault("run_id", run_id)
                manifest.setdefault("artifact_prefix", storage_ctx["prefix"])
                return manifest
        return self._synthesize_library_manifest(run, storage_ctx)

    async def get_abstracts(
        self,
        *,
        run_id: str,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        records = self._read_jsonl_artifact(storage_ctx, "abstracts.jsonl")
        sliced = records[offset : offset + limit]
        return {
            "run_id": run_id,
            "total": len(records),
            "offset": offset,
            "limit": limit,
            "records": sliced,
            "artifact": self._artifact_descriptor(storage_ctx, "abstracts.jsonl"),
        }

    async def get_resource_manifest(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        artifact_name = "dlm_resource_manifest.json"
        if self._artifact_exists(storage_ctx, artifact_name):
            payload = self._read_json_artifact(storage_ctx, artifact_name)
            if isinstance(payload, dict):
                payload.setdefault("run_id", run_id)
                payload.setdefault("artifact_prefix", storage_ctx["prefix"])
                payload.setdefault("resource_manifest_uri", f"gs://{storage_ctx['bucket']}/{self._artifact_object_path(storage_ctx, artifact_name)}")
                return payload

        payload = self._build_dlm_resource_manifest(run, storage_ctx)
        persisted = self._persist_json_artifact(
            storage_ctx,
            artifact_name,
            payload,
            metadata={
                "run_id": run_id,
                "artifact_kind": "dlm_resource_manifest",
            },
        )
        payload["resource_manifest_uri"] = persisted["gcs_uri"]
        payload["resource_manifest_sha256"] = persisted["sha256"]
        payload["resource_manifest_artifact"] = self._artifact_descriptor(storage_ctx, artifact_name)
        return payload

    async def get_fulltext_availability(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        payload = self._read_json_artifact(storage_ctx, "fulltext_availability.json")
        return {
            "run_id": run_id,
            "artifact": self._artifact_descriptor(storage_ctx, "fulltext_availability.json"),
            "payload": payload,
        }

    async def get_sections(
        self,
        *,
        run_id: str,
        user_id: str,
        hit_id: str = "",
        limit: int = 20,
    ) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        payload = self._read_json_artifact(storage_ctx, "section_figure_index.json")
        records = self._coerce_record_list(payload, preferred_keys=("records",))
        if hit_id:
            records = [record for record in records if str(record.get("hit_id") or "") == hit_id]
        flattened: List[Dict[str, Any]] = []
        for record in records:
            for section in list(record.get("sections") or []):
                flattened.append(
                    {
                        "hit_id": record.get("hit_id"),
                        "title": record.get("title"),
                        "section_status": record.get("section_status"),
                        **dict(section),
                    }
                )
        return {
            "run_id": run_id,
            "hit_id": hit_id or None,
            "total_records": len(records),
            "total_sections": len(flattened),
            "sections": flattened[:limit],
            "artifact": self._artifact_descriptor(storage_ctx, "section_figure_index.json"),
        }

    async def get_figures(
        self,
        *,
        run_id: str,
        user_id: str,
        hit_id: str = "",
        limit: int = 20,
    ) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        payload = self._read_json_artifact(storage_ctx, "section_figure_index.json")
        records = self._coerce_record_list(payload, preferred_keys=("records",))
        if hit_id:
            records = [record for record in records if str(record.get("hit_id") or "") == hit_id]
        figures: List[Dict[str, Any]] = []
        for record in records:
            for figure in list(record.get("figures") or []):
                figures.append(
                    {
                        "hit_id": record.get("hit_id"),
                        "title": record.get("title"),
                        **dict(figure),
                    }
                )
        return {
            "run_id": run_id,
            "hit_id": hit_id or None,
            "total_records": len(records),
            "total_figures": len(figures),
            "figures": figures[:limit],
            "artifact": self._artifact_descriptor(storage_ctx, "section_figure_index.json"),
        }

    async def get_dlm_matches(
        self,
        *,
        run_id: str,
        user_id: str,
        query: str = "",
        entity_type: str = "",
        limit: int = 20,
    ) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        entities_payload = self._read_json_artifact(storage_ctx, "dlm_entities.json")
        sections_payload = self._read_json_artifact(storage_ctx, "dlm_sections.json")
        receipt_payload = self._read_json_artifact(storage_ctx, "dlm_receipt.json")
        matches = self._search_dlm_payloads(
            query=query,
            entity_type=entity_type,
            entities_payload=entities_payload,
            sections_payload=sections_payload,
            storage_ctx=storage_ctx,
        )
        return {
            "run_id": run_id,
            "query": query,
            "entity_type": entity_type or None,
            "total_matches": len(matches),
            "matches": matches[:limit],
            "artifacts": {
                "entities": self._artifact_descriptor(storage_ctx, "dlm_entities.json"),
                "sections": self._artifact_descriptor(storage_ctx, "dlm_sections.json"),
                "receipt": self._artifact_descriptor(storage_ctx, "dlm_receipt.json"),
            },
            "receipt": receipt_payload,
        }

    async def get_pdf_audit(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        payload = self._read_json_artifact(storage_ctx, "free_pdf_audit.json")
        return {
            "run_id": run_id,
            "artifact": self._artifact_descriptor(storage_ctx, "free_pdf_audit.json"),
            "payload": payload,
            "downloaded_count": len(
                [item for item in payload if isinstance(item, dict) and str(item.get("status") or "").casefold() == "downloaded"]
            )
            if isinstance(payload, list)
            else None,
            "blocked_count": len(
                [item for item in payload if isinstance(item, dict) and str(item.get("status") or "").casefold() != "downloaded"]
            )
            if isinstance(payload, list)
            else None,
        }

    async def get_ranking_trace(self, *, run_id: str, user_id: str) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        payload = self._read_json_artifact(storage_ctx, "ranking_trace.json")
        return {
            "run_id": run_id,
            "artifact": self._artifact_descriptor(storage_ctx, "ranking_trace.json"),
            "payload": payload,
        }

    async def get_artifact(
        self,
        *,
        run_id: str,
        user_id: str,
        artifact_name: str,
        include_body: bool = False,
    ) -> Dict[str, Any]:
        run = await self.get_run(run_id=run_id, user_id=user_id)
        storage_ctx = self._build_library_storage_context(run)
        safe_name = artifact_name.strip().lstrip("/")
        if not safe_name:
            raise KeyError("artifact_name")
        descriptor = self._artifact_descriptor(storage_ctx, safe_name)
        payload: Dict[str, Any] = {
            "run_id": run_id,
            "artifact_name": safe_name,
            "artifact": descriptor,
        }
        if include_body and safe_name.lower().endswith(".json"):
            payload["body"] = self._read_json_artifact(storage_ctx, safe_name)
        elif include_body and safe_name.lower().endswith(".jsonl"):
            payload["body"] = self._read_jsonl_artifact(storage_ctx, safe_name)
        return payload

    async def promote_to_working_set(
        self,
        *,
        run_id: str,
        user_id: str,
        request: PromoteToWorkingSetRequest,
    ) -> Dict[str, Any]:
        selected_hits = await self._load_selected_hits(run_id=run_id, user_id=user_id, hit_ids=request.hit_ids)
        return self._build_read_propose_only_response(
            run_id=run_id,
            user_id=user_id,
            proposal_kind="working_set_attachment",
            selected_hits=selected_hits,
            target_ref=f"working-set://{request.working_set_id or 'new-proposal'}",
            compatibility={"working_set_id": request.working_set_id, "items_added": []},
        )

    async def attach_to_study(
        self,
        *,
        run_id: str,
        user_id: str,
        request: AttachToStudyRequest,
    ) -> Dict[str, Any]:
        selected_hits = await self._load_selected_hits(run_id=run_id, user_id=user_id, hit_ids=request.hit_ids)
        return self._build_read_propose_only_response(
            run_id=run_id,
            user_id=user_id,
            proposal_kind="study_attachment",
            selected_hits=selected_hits,
            target_ref=f"study://{request.study_id}",
            compatibility={"study_id": request.study_id, "attached": []},
        )

    async def promote_to_kb(
        self,
        *,
        run_id: str,
        user_id: str,
        request: PromoteToKBRequest,
        document_scan_service: Any = None,
    ) -> Dict[str, Any]:
        selected_hits = await self._load_selected_hits(run_id=run_id, user_id=user_id, hit_ids=request.hit_ids)
        del document_scan_service
        return self._build_read_propose_only_response(
            run_id=run_id,
            user_id=user_id,
            proposal_kind="kb_consolidation",
            selected_hits=selected_hits,
            target_ref=f"kb://{request.kb_id}",
            compatibility={"kb_id": request.kb_id, "promotions": []},
        )

    def _build_read_propose_only_response(
        self,
        *,
        run_id: str,
        user_id: str,
        proposal_kind: str,
        selected_hits: Sequence[Dict[str, Any]],
        target_ref: str,
        compatibility: Dict[str, Any],
    ) -> Dict[str, Any]:
        hit_refs = [
            {
                "hit_ref": f"alejandria-search://runs/{run_id}/hits/{hit['hit_id']}",
                "citation_ref": self._canonical_paper_id_for_hit(hit),
                "doi": str(hit.get("doi") or ""),
                "pmid": str(hit.get("pmid") or ""),
                "pmcid": str(hit.get("pmcid") or ""),
            }
            for hit in selected_hits
        ]
        receipt_seed = json.dumps(
            {
                "run_id": run_id,
                "user_id": user_id,
                "proposal_kind": proposal_kind,
                "target_ref": target_ref,
                "hit_refs": hit_refs,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt_ref = (
            "receipt://alejandria/read-propose-only/"
            + hashlib.sha256(receipt_seed.encode("utf-8")).hexdigest()[:24]
        )
        return {
            "run_id": run_id,
            **compatibility,
            "status": "proposal_only",
            "authority_mode": "read_propose_only",
            "proposal_kind": proposal_kind,
            "target_ref": target_ref,
            "hit_refs": hit_refs,
            "receipt_ref": receipt_ref,
            "requires_command": "protocol.p5.citations.consolidate",
            "requires_quetzal_receipt": True,
            "claim_promotion_performed": False,
            "kb_write_performed": False,
            "study_write_performed": False,
            "working_set_write_performed": False,
            "graph_write_performed": False,
        }

    def _build_provider_attempts(self, plan: Any, result: Any) -> List[Dict[str, Any]]:
        source_health = dict(result.source_health or {})
        attempts: List[Dict[str, Any]] = []
        for source in list(getattr(plan, "requested_sources", []) or []):
            key = source.value if hasattr(source, "value") else str(source)
            health = dict(source_health.get(key) or {})
            attempts.append(
                {
                    "provider": key,
                    "attempted": key in list(result.attempted_sources or []),
                    "failed": key in list(result.failed_sources or []),
                    "source_health": health,
                    "result_count": int(result.source_counts.get(key, 0) if hasattr(result, "source_counts") else 0),
                }
            )
        return attempts

    def _resolved_search_query(self, request: AlejandriaSearchRequest) -> str:
        if request.paper_seed is not None:
            for candidate in (
                request.paper_seed.title,
                request.paper_seed.doi,
                request.paper_seed.pmid,
                request.paper_seed.pmcid,
                request.paper_seed.openalex_id,
                request.paper_seed.paper_id,
            ):
                value = str(candidate or "").strip()
                if value:
                    return value
        return request.query_text.strip()

    def _collect_query_variants(
        self,
        *,
        request: AlejandriaSearchRequest,
        lmp_context: Dict[str, Any],
    ) -> List[str]:
        variants: List[str] = []
        variants.extend(list(request.query_groups or []))
        if request.expansion_policy.use_lmp:
            variants.extend(list(lmp_context.get("query_variants", []) or []))
        return _dedupe_texts(variants)

    def _build_paper_identity_targets(
        self,
        paper_seed: Optional[AlejandriaPaperSeed],
    ) -> List[Dict[str, Any]]:
        if paper_seed is None:
            return []
        payload = paper_seed.model_dump(mode="json")
        target = {
            "id": "seed_1",
            "doi": str(payload.get("doi") or "").strip(),
            "pmid": str(payload.get("pmid") or "").strip(),
            "pmcid": str(payload.get("pmcid") or "").strip(),
            "title": str(payload.get("title") or "").strip(),
            "paper_id": str(payload.get("paper_id") or "").strip(),
            "openalex_id": str(payload.get("openalex_id") or "").strip(),
        }
        if not any(str(target.get(key) or "").strip() for key in ("doi", "pmid", "pmcid", "title", "paper_id", "openalex_id")):
            return []
        return [target]

    def _citation_keys_for_hit(self, hit: Dict[str, Any]) -> List[str]:
        raw_payload = dict(hit.get("raw_payload") or {})
        metadata = dict(raw_payload.get("metadata") or {})
        return _dedupe_texts(
            [
                str(hit.get("doi") or "").strip(),
                str(hit.get("pmid") or "").strip(),
                str(hit.get("pmcid") or "").strip(),
                str(raw_payload.get("paperId") or "").strip(),
                str(metadata.get("openalex_id") or "").strip(),
                self._canonical_paper_id_for_hit(hit),
            ]
        )

    def _fulltext_availability_kind(self, hit: Dict[str, Any]) -> str:
        raw_payload = dict(hit.get("raw_payload") or {})
        metadata = dict(raw_payload.get("metadata") or {})
        fulltext_router = dict(metadata.get("fulltext_router") or {})
        acquisition_kind = str(
            fulltext_router.get("acquisition_kind")
            or hit.get("fulltext_status")
            or raw_payload.get("acquisition_kind")
            or ""
        ).strip().lower()
        if acquisition_kind == "pmc_jats":
            return "pmc_jats"
        if acquisition_kind in {"europe_pmc", "publisher_html"}:
            return "oa_html"
        if acquisition_kind in {"openalex_oa", "openalex_paid", "s2_fulltext", "oa_url", "pdf", "ocr_pdf"}:
            return "oa_pdf_url"
        if acquisition_kind in {"full_text", "fulltext_verified"}:
            return "oa_html"
        if acquisition_kind == "restricted":
            return "restricted"
        if acquisition_kind in {"provider_error", "error"}:
            return "provider_error"
        if acquisition_kind == "metadata_only":
            return "metadata_only"
        if str(hit.get("abstract") or "").strip():
            return "abstract_only"
        return "metadata_only"

    def _apply_ranking_v1(
        self,
        *,
        hits: List[Dict[str, Any]],
        query_variants: Sequence[str],
        citation_graph: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        graph_counter: Counter[str] = Counter()
        for source_key, values in dict(citation_graph or {}).items():
            source = str(source_key or "").strip()
            if source:
                graph_counter[source] += len(list(values or []))
            for value in list(values or []):
                candidate = str(value or "").strip()
                if candidate:
                    graph_counter[candidate] += 1
        max_graph = max(graph_counter.values(), default=0)
        total_queries = max(1, len(_dedupe_texts(query_variants)))

        ranked_hits: List[Dict[str, Any]] = []
        for hit in list(hits or []):
            current = dict(hit)
            scores = dict(current.get("scores") or {})
            base_score = float(scores.get("final_score") or 0.0)
            query_contributions = _dedupe_query_contributions(list(current.get("query_contributions") or []))
            provider_overlap_score = min(1.0, len(list(current.get("provider_sources") or [])) / 3.0)
            query_coverage_score = min(
                1.0,
                len({str(item.get("query_text") or "").strip().casefold() for item in query_contributions if str(item.get("query_text") or "").strip()})
                / float(total_queries),
            )
            identifier_score = 1.0 if any(str(current.get(key) or "").strip() for key in ("doi", "pmid", "pmcid")) else 0.4 if list(current.get("provider_ids") or []) else 0.0
            fulltext_kind = self._fulltext_availability_kind(current)
            fulltext_score = {
                "pmc_jats": 1.0,
                "oa_html": 0.9,
                "oa_pdf_url": 0.85,
                "abstract_only": 0.25,
                "metadata_only": 0.05,
                "restricted": 0.0,
                "provider_error": 0.0,
            }.get(fulltext_kind, 0.05)
            citation_graph_score = 0.0
            if max_graph > 0:
                citation_graph_score = max(
                    (float(graph_counter.get(key) or 0) / float(max_graph) for key in self._citation_keys_for_hit(current)),
                    default=0.0,
                )
            penalties: List[str] = []
            penalty_total = 0.0
            if not any(str(current.get(key) or "").strip() for key in ("doi", "pmid", "pmcid")):
                penalties.append("no_external_ids")
                penalty_total += 0.05
            if fulltext_kind in {"abstract_only", "metadata_only"}:
                penalties.append("fulltext_not_verified")
                penalty_total += 0.08
            if len(list(current.get("provider_sources") or [])) <= 1 and float(scores.get("provider_confidence") or 0.0) < 0.85:
                penalties.append("low_provider_overlap")
                penalty_total += 0.03
            if "review" in str(current.get("title") or "").casefold():
                penalties.append("review_only_penalty")
                penalty_total += 0.04
            if float(scores.get("lexical_score") or 0.0) < 0.2:
                penalties.append("generic_result_penalty")
                penalty_total += 0.04

            final_score = max(
                0.0,
                min(
                    1.0,
                    (
                        base_score * 0.45
                        + provider_overlap_score * 0.10
                        + query_coverage_score * 0.10
                        + identifier_score * 0.08
                        + fulltext_score * 0.10
                        + citation_graph_score * 0.07
                        + float(scores.get("citation_score") or 0.0) * 0.05
                        + float(scores.get("recency_score") or 0.0) * 0.05
                        + float(scores.get("exact_entity_score") or 0.0) * 0.05
                    )
                    - penalty_total
                ),
            )
            ranking_reasons = _dedupe_texts(
                [
                    *list(current.get("reasons") or []),
                    "provider_overlap" if provider_overlap_score >= 0.67 else "",
                    "multi_query_coverage" if query_coverage_score >= 0.5 else "",
                    "citation_graph_supported" if citation_graph_score > 0.0 else "",
                    "fulltext_available" if fulltext_score >= 0.85 else "",
                    "identifier_complete" if identifier_score >= 1.0 else "",
                    *penalties,
                ]
            )
            scores.update(
                {
                    "base_score": round(base_score, 6),
                    "provider_overlap_score": round(provider_overlap_score, 6),
                    "query_coverage_score": round(query_coverage_score, 6),
                    "identifier_score": round(identifier_score, 6),
                    "fulltext_availability_score": round(fulltext_score, 6),
                    "citation_graph_score": round(citation_graph_score, 6),
                    "penalty_total": round(penalty_total, 6),
                    "final_score": round(final_score, 6),
                }
            )
            current["query_contributions"] = query_contributions
            current["fulltext_availability_kind"] = fulltext_kind
            current["scores"] = scores
            current["ranking_reasons"] = ranking_reasons
            current["reasons"] = ranking_reasons
            current["limitations"] = _dedupe_texts([*list(current.get("limitations") or []), *penalties])
            ranked_hits.append(current)
        return ranked_hits

    def _build_query_contribution_map(self, hits: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = []
        for hit in list(hits or []):
            query_contributions = _dedupe_query_contributions(list(hit.get("query_contributions") or []))
            entries.append(
                {
                    "hit_id": str(hit.get("hit_id") or ""),
                    "title": str(hit.get("title") or ""),
                    "queries": _dedupe_texts([str(item.get("query_text") or "") for item in query_contributions]),
                    "providers": list(hit.get("provider_sources") or []),
                    "external_ids": {
                        "doi": str(hit.get("doi") or ""),
                        "pmid": str(hit.get("pmid") or ""),
                        "pmcid": str(hit.get("pmcid") or ""),
                    },
                    "query_count": len({str(item.get("query_text") or "").strip().casefold() for item in query_contributions if str(item.get("query_text") or "").strip()}),
                    "provider_count": len(list(hit.get("provider_sources") or [])),
                }
            )
        return {
            "entry_count": len(entries),
            "entries": entries,
        }

    def _build_dedup_clusters(self, hits: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        clusters: Dict[str, Dict[str, Any]] = {}
        for hit in list(hits or []):
            key = (
                str(hit.get("doi") or "").strip()
                or str(hit.get("pmid") or "").strip()
                or str(hit.get("pmcid") or "").strip()
                or _normalize_title_key(hit.get("title") or "")
                or str(hit.get("hit_id") or "").strip()
            )
            cluster = clusters.setdefault(
                key,
                {
                    "cluster_key": key,
                    "hit_ids": [],
                    "providers": [],
                    "queries": [],
                    "titles": [],
                },
            )
            cluster["hit_ids"] = _dedupe_texts([*cluster.get("hit_ids", []), str(hit.get("hit_id") or "")])
            cluster["providers"] = _dedupe_texts([*cluster.get("providers", []), *list(hit.get("provider_sources") or [])])
            cluster["queries"] = _dedupe_texts(
                [*cluster.get("queries", []), *[str(item.get("query_text") or "") for item in list(hit.get("query_contributions") or [])]]
            )
            cluster["titles"] = _dedupe_texts([*cluster.get("titles", []), str(hit.get("title") or "")])
        return {
            "cluster_count": len(clusters),
            "clusters": list(clusters.values()),
        }

    def _build_fulltext_availability_summary(self, hits: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for hit in list(hits or []):
            kind = self._fulltext_availability_kind(hit)
            counts[kind] += 1
            entries.append(
                {
                    "hit_id": str(hit.get("hit_id") or ""),
                    "title": str(hit.get("title") or ""),
                    "availability": kind,
                    "provider_sources": list(hit.get("provider_sources") or []),
                    "doi": str(hit.get("doi") or ""),
                    "pmid": str(hit.get("pmid") or ""),
                    "pmcid": str(hit.get("pmcid") or ""),
                    "fulltext_status": str(hit.get("fulltext_status") or ""),
                }
            )
        return {
            "counts": dict(counts),
            "entries": entries,
        }

    def _persist_run_artifacts_to_gcs(
        self,
        *,
        run_id: str,
        user_id: str,
        request: AlejandriaSearchRequest,
        raw_papers: Sequence[Dict[str, Any]],
        raw_hits: Sequence[Dict[str, Any]],
        deduped_hits: Sequence[Dict[str, Any]],
        provider_attempts: Sequence[Dict[str, Any]],
        provider_failures: Sequence[Dict[str, Any]],
        analytics: Dict[str, Any],
        citation_graph: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        if not request.enable_gcs_artifacts:
            return {
                "status": "disabled",
                "limitation": "gcs_artifact_pipeline_disabled",
                "manifest": {"enabled": False},
            }
        current_storage_status = storage_status()
        if not bool(current_storage_status.get("ready")):
            return {
                "status": "degraded",
                "limitation": "gcs_artifact_pipeline_unavailable",
                "manifest": {"enabled": True, "storage_status": current_storage_status},
            }
        try:
            storage = get_storage_manager()
            safe_prefix = sanitize_object_prefix(request.artifact_prefix or f"alejandria-search/{run_id}")
            bucket = storage.ensure_bucket(user_id)

            def _write_json(name: str, payload: Any, *, kind: str, hit_id: str = "") -> Dict[str, Any]:
                object_path = f"{safe_prefix}/{name}"
                rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2)
                gcs_uri = storage.upload_text(
                    user_id=user_id,
                    object_path=object_path,
                    text=rendered,
                    content_type="application/json",
                    metadata={"run_id": run_id, "artifact_kind": kind},
                )
                ref = {
                    "kind": kind,
                    "object_path": object_path,
                    "gcs_uri": gcs_uri,
                    "bucket": bucket.bucket_name,
                }
                if hit_id:
                    ref["hit_id"] = hit_id
                return ref

            def _write_jsonl(name: str, payload: Sequence[Dict[str, Any]], *, kind: str) -> Dict[str, Any]:
                object_path = f"{safe_prefix}/{name}"
                rendered = "\n".join(json.dumps(item, ensure_ascii=True, sort_keys=True) for item in list(payload or []))
                if rendered:
                    rendered += "\n"
                gcs_uri = storage.upload_text(
                    user_id=user_id,
                    object_path=object_path,
                    text=rendered,
                    content_type="application/jsonl",
                    metadata={"run_id": run_id, "artifact_kind": kind},
                )
                return {
                    "kind": kind,
                    "object_path": object_path,
                    "gcs_uri": gcs_uri,
                    "bucket": bucket.bucket_name,
                }

            run_refs = [
                _write_json("request.json", request.model_dump(mode="json"), kind="request"),
                _write_json(
                    "run_summary.json",
                    {
                        "run_id": run_id,
                        "query_text": request.query_text,
                        "search_mode": request.search_mode,
                        "raw_papers": len(list(raw_papers or [])),
                        "normalized_hits": len(list(raw_hits or [])),
                        "deduped_hits": len(list(deduped_hits or [])),
                        "provider_attempts": list(provider_attempts or []),
                        "provider_failures": list(provider_failures or []),
                        "citation_graph_edges": sum(len(list(values or [])) for values in dict(citation_graph or {}).values()),
                    },
                    kind="run_summary",
                ),
                _write_jsonl("provider_receipts.jsonl", [*list(provider_attempts or []), *list(provider_failures or [])], kind="provider_receipts"),
                _write_jsonl("raw_hits.jsonl", list(raw_papers or []), kind="raw_hits"),
                _write_jsonl("normalized_hits.jsonl", list(raw_hits or []), kind="normalized_hits"),
                _write_jsonl("deduped_hits.jsonl", list(deduped_hits or []), kind="deduped_hits"),
                _write_json("dedup_clusters.json", self._build_dedup_clusters(raw_hits), kind="dedup_clusters"),
                _write_json("query_contribution_map.json", self._build_query_contribution_map(deduped_hits), kind="query_contribution_map"),
                _write_json(
                    "ranking_trace.json",
                    [
                        {
                            "hit_id": str(hit.get("hit_id") or ""),
                            "title": str(hit.get("title") or ""),
                            "scores": dict(hit.get("scores") or {}),
                            "ranking_reasons": list(hit.get("ranking_reasons") or []),
                        }
                        for hit in list(deduped_hits or [])
                    ],
                    kind="ranking_trace",
                ),
                _write_json("fulltext_availability.json", self._build_fulltext_availability_summary(deduped_hits), kind="fulltext_availability"),
                _write_json("analytics_summary.json", analytics, kind="analytics_summary"),
            ]
            if citation_graph:
                run_refs.append(_write_json("citation_graph.json", citation_graph, kind="citation_graph"))

            hit_refs: Dict[str, List[Dict[str, Any]]] = {}
            for hit in list(deduped_hits or []):
                hit_id = str(hit.get("hit_id") or "").strip()
                if not hit_id:
                    continue
                hit_refs[hit_id] = [
                    _write_json(f"hits/{hit_id}.json", hit, kind="hit_record", hit_id=hit_id)
                ]

            return {
                "status": "written",
                "run_refs": _dedupe_gcs_refs(run_refs),
                "hit_refs": {key: _dedupe_gcs_refs(value) for key, value in hit_refs.items()},
                "manifest": {
                    "enabled": True,
                    "bucket": bucket.bucket_name,
                    "prefix": safe_prefix,
                    "file_count": len(run_refs) + sum(len(value) for value in hit_refs.values()),
                },
            }
        except Exception as exc:
            logger.warning("Alejandria Search GCS artifact persistence failed for %s: %s", run_id, exc)
            return {
                "status": "error",
                "limitation": "gcs_artifact_pipeline_error",
                "manifest": {"enabled": True, "error": str(exc)},
            }

    def _build_library_storage_context(self, run: Dict[str, Any]) -> Dict[str, Any]:
        request_payload = dict(run.get("request") or {})
        prefix = str(request_payload.get("artifact_prefix") or "").strip()
        if not prefix:
            prefixes = {
                "/".join(str(ref.get("object_path") or "").split("/")[:-1])
                for ref in list(run.get("gcs_artifact_refs") or [])
                if str(ref.get("object_path") or "").count("/") >= 1
            }
            prefix = next(iter(prefixes), "")
        if not prefix:
            prefix = sanitize_object_prefix(f"alejandria-search/{run['run_id']}")
        storage = get_storage_manager()
        bucket = storage.ensure_bucket(run["user_id"])
        objects = storage.list_objects(user_id=run["user_id"], prefix=prefix, max_results=500, include_metadata=True)
        object_map = {str(item.get("object_path") or item.get("name") or ""): dict(item) for item in objects}
        return {
            "run_id": run["run_id"],
            "user_id": run["user_id"],
            "prefix": prefix,
            "storage": storage,
            "bucket": bucket.bucket_name,
            "objects": objects,
            "object_map": object_map,
        }

    def _artifact_exists(self, storage_ctx: Dict[str, Any], artifact_name: str) -> bool:
        object_path = self._artifact_object_path(storage_ctx, artifact_name)
        return object_path in storage_ctx["object_map"]

    def _artifact_object_path(self, storage_ctx: Dict[str, Any], artifact_name: str) -> str:
        safe_name = artifact_name.strip().lstrip("/")
        if safe_name.startswith(storage_ctx["prefix"] + "/"):
            return safe_name
        return f"{storage_ctx['prefix']}/{safe_name}"

    def _artifact_descriptor(self, storage_ctx: Dict[str, Any], artifact_name: str) -> Dict[str, Any]:
        object_path = self._artifact_object_path(storage_ctx, artifact_name)
        info = storage_ctx["storage"].get_object_info(user_id=storage_ctx["user_id"], object_path=object_path)
        return {
            "artifact_name": artifact_name,
            "bucket": info.get("bucket") or storage_ctx["bucket"],
            "object_path": object_path,
            "evidence_object": info.get("evidence_object"),
            "content_type": info.get("content_type"),
            "size": info.get("size"),
            "updated": info.get("updated"),
            "metadata": info.get("metadata") or {},
        }

    def _persist_json_artifact(
        self,
        storage_ctx: Dict[str, Any],
        artifact_name: str,
        payload: Any,
        *,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        object_path = self._artifact_object_path(storage_ctx, artifact_name)
        rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2)
        sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("sha256", sha256)
        gcs_uri = storage_ctx["storage"].upload_text(
            user_id=storage_ctx["user_id"],
            object_path=object_path,
            text=rendered,
            content_type="application/json",
            metadata=merged_metadata,
        )
        storage_ctx["object_map"][object_path] = {
            "object_path": object_path,
            "name": object_path,
            "metadata": merged_metadata,
        }
        return {
            "object_path": object_path,
            "gcs_uri": gcs_uri,
            "sha256": sha256,
        }

    def _read_json_artifact(self, storage_ctx: Dict[str, Any], artifact_name: str) -> Any:
        object_path = self._artifact_object_path(storage_ctx, artifact_name)
        raw = storage_ctx["storage"].read_bytes(user_id=storage_ctx["user_id"], object_path=object_path)
        return json.loads(raw.decode("utf-8"))

    def _read_jsonl_artifact(self, storage_ctx: Dict[str, Any], artifact_name: str) -> List[Dict[str, Any]]:
        object_path = self._artifact_object_path(storage_ctx, artifact_name)
        raw = storage_ctx["storage"].read_bytes(user_id=storage_ctx["user_id"], object_path=object_path)
        lines = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def _synthesize_library_manifest(self, run: Dict[str, Any], storage_ctx: Dict[str, Any]) -> Dict[str, Any]:
        entries: Dict[str, str] = {}
        for object_path in sorted(storage_ctx["object_map"].keys()):
            if not object_path.startswith(storage_ctx["prefix"] + "/"):
                continue
            leaf = object_path[len(storage_ctx["prefix"]) + 1 :]
            if leaf in _LIBRARY_KNOWN_ARTIFACTS or leaf.lower().endswith(".pdf"):
                entries[leaf] = f"gs://{storage_ctx['bucket']}/{object_path}"
        return {
            "run_id": run["run_id"],
            "artifact_prefix": storage_ctx["prefix"],
            "manifest_uri": entries.get("library_manifest.json", ""),
            "entries": entries,
            "gcs_artifact_refs": list(run.get("gcs_artifact_refs") or []),
        }

    def _build_dlm_resource_manifest(self, run: Dict[str, Any], storage_ctx: Dict[str, Any]) -> Dict[str, Any]:
        prefix = storage_ctx["prefix"]
        bucket = storage_ctx["bucket"]
        object_map = dict(storage_ctx["object_map"] or {})
        hit_paths = sorted(path for path in object_map if path.startswith(f"{prefix}/hits/") and path.lower().endswith(".json"))
        pdf_paths = sorted(path for path in object_map if path.startswith(f"{prefix}/") and path.lower().endswith(".pdf"))
        manifest_uri = f"gs://{bucket}/{prefix}/library_manifest.json" if self._artifact_exists(storage_ctx, "library_manifest.json") else ""

        total_sections = 0
        total_figures = 0
        if self._artifact_exists(storage_ctx, "section_figure_index.json"):
            payload = self._read_json_artifact(storage_ctx, "section_figure_index.json")
            records = self._coerce_record_list(payload, preferred_keys=("records",))
            for record in records:
                total_sections += len(list(record.get("sections") or []))
                total_figures += len(list(record.get("figures") or []))

        fulltext_summary: Dict[str, Any] = {}
        if self._artifact_exists(storage_ctx, "fulltext_availability.json"):
            payload = self._read_json_artifact(storage_ctx, "fulltext_availability.json")
            if isinstance(payload, dict):
                fulltext_summary = dict(payload.get("counts") or {})

        return {
            "schema_version": "alejandria_dlm_resource_manifest_v1",
            "run_id": run["run_id"],
            "artifact_prefix": prefix,
            "library_manifest_uri": manifest_uri,
            "resource_policy": {
                "raw_abstract_dump_forbidden": True,
                "raw_fulltext_dump_forbidden": True,
                "bounded_snippet_route": "/api/v1/alejandria-search/runs/{run_id}/dlm",
                "bounded_section_route": "/api/v1/alejandria-search/runs/{run_id}/sections",
                "bounded_figure_route": "/api/v1/alejandria-search/runs/{run_id}/figures",
            },
            "resources": {
                "hits": {
                    "artifact": self._artifact_descriptor(storage_ctx, "deduped_hits.jsonl"),
                    "record_count": len(hit_paths),
                    "item_route": "/api/v1/alejandria-search/runs/{run_id}/hits",
                },
                "abstracts": {
                    "artifact": self._artifact_descriptor(storage_ctx, "abstracts.jsonl"),
                    "access": "bundle_only",
                    "item_route": "/api/v1/alejandria-search/runs/{run_id}/abstracts",
                },
                "sections": {
                    "artifact": self._artifact_descriptor(storage_ctx, "section_figure_index.json"),
                    "total_sections": total_sections,
                    "snippet_route": "/api/v1/alejandria-search/runs/{run_id}/sections",
                },
                "figures": {
                    "artifact": self._artifact_descriptor(storage_ctx, "section_figure_index.json"),
                    "total_figures": total_figures,
                    "snippet_route": "/api/v1/alejandria-search/runs/{run_id}/figures",
                    "figure_objects_present": False,
                },
                "dlm_entities": {
                    "artifact": self._artifact_descriptor(storage_ctx, "dlm_entities.json"),
                    "snippet_route": "/api/v1/alejandria-search/runs/{run_id}/dlm",
                },
                "dlm_sections": {
                    "artifact": self._artifact_descriptor(storage_ctx, "dlm_sections.json"),
                    "snippet_route": "/api/v1/alejandria-search/runs/{run_id}/dlm",
                },
                "fulltext_availability": {
                    "artifact": self._artifact_descriptor(storage_ctx, "fulltext_availability.json"),
                    "counts": fulltext_summary,
                },
                "pdf_objects": [
                    {
                        "artifact_name": path.split("/")[-1],
                        "object_path": path,
                        "gcs_uri": f"gs://{bucket}/{path}",
                    }
                    for path in pdf_paths
                ],
            },
            "provenance": {
                "bucket": bucket,
                "resource_owner": run["user_id"],
                "gcs_object_count": len(object_map),
            },
        }

    def _search_dlm_payloads(
        self,
        *,
        query: str,
        entity_type: str,
        entities_payload: Any,
        sections_payload: Any,
        storage_ctx: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        needle = query.casefold().strip()
        wanted_type = entity_type.casefold().strip()
        entity_records = self._coerce_record_list(entities_payload, preferred_keys=("records", "entities", "matches"))
        section_records = self._coerce_record_list(sections_payload, preferred_keys=("records", "sections", "matches"))
        matches: List[Dict[str, Any]] = []
        for record in entity_records:
            record_text = json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
            record_type = str(record.get("entity_type") or record.get("type") or "").casefold()
            if wanted_type and record_type != wanted_type:
                continue
            if needle and needle not in record_text:
                continue
            matches.append(
                {
                    "match_source": "dlm_entities",
                    "entity_type": record.get("entity_type") or record.get("type") or "",
                    "snippet": record.get("snippet") or record.get("text") or record.get("surface_text") or "",
                    "paper_id": record.get("paper_id") or record.get("hit_id") or record.get("document_id") or "",
                    "section_id": record.get("section_id") or "",
                    "record": record,
                    "artifact": {
                        "object_path": self._artifact_object_path(storage_ctx, "dlm_entities.json"),
                        "gcs_uri": f"gs://{storage_ctx['bucket']}/{self._artifact_object_path(storage_ctx, 'dlm_entities.json')}",
                    },
                }
            )
        for record in section_records:
            record_text = json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
            if needle and needle not in record_text:
                continue
            matches.append(
                {
                    "match_source": "dlm_sections",
                    "entity_type": record.get("entity_type") or record.get("type") or "section_text",
                    "snippet": record.get("snippet") or record.get("text") or "",
                    "paper_id": record.get("paper_id") or record.get("hit_id") or record.get("document_id") or "",
                    "section_id": record.get("section_id") or "",
                    "record": record,
                    "artifact": {
                        "object_path": self._artifact_object_path(storage_ctx, "dlm_sections.json"),
                        "gcs_uri": f"gs://{storage_ctx['bucket']}/{self._artifact_object_path(storage_ctx, 'dlm_sections.json')}",
                    },
                }
            )
        return matches

    def _coerce_record_list(self, payload: Any, *, preferred_keys: Sequence[str]) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in preferred_keys:
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    return [dict(item) for item in candidate if isinstance(item, dict)]
        return []

    def _normalize_hits(
        self,
        *,
        run_id: str,
        papers: List[Dict[str, Any]],
        request: AlejandriaSearchRequest,
        query_entities: Dict[str, Any],
        lmp_context: Dict[str, Any],
        bsm_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        lmp_aliases = set(
            item.casefold()
            for item in _dedupe_texts(
                [
                    *(query_entities.get("proteins", []) or []),
                    *(query_entities.get("genes", []) or []),
                    *(
                        (lmp_context.get("records") or [{}])[0].get("genes", [])
                        if lmp_context.get("records")
                        else []
                    ),
                    *(
                        [
                            (lmp_context.get("records") or [{}])[0].get("protein_name", ""),
                            (lmp_context.get("records") or [{}])[0].get("accession", ""),
                        ]
                        if lmp_context.get("records")
                        else []
                    ),
                ]
            )
            if item
        )
        partner_terms = {
            str(item.get("partner") or "").casefold()
            for item in ((bsm_context.get("contexts") or [{}])[0].get("top_partners", []) if bsm_context.get("contexts") else [])
            if str(item.get("partner") or "").strip()
        }
        for index, paper in enumerate(papers):
            metadata = dict(paper.get("metadata") or {})
            title = str(paper.get("title") or "(untitled)").strip()
            abstract = str(paper.get("abstract") or paper.get("summary") or "").strip()
            snippet = abstract[:500]
            authors = _coerce_author_names(paper.get("authors"))
            journal = str(paper.get("journal") or paper.get("venue") or "").strip()
            year = _safe_int(paper.get("year") or paper.get("publication_year"))
            external_ids = paper.get("externalIds") or {}
            doi = str(paper.get("doi") or external_ids.get("DOI") or "").strip()
            pmid = str(paper.get("pmid") or external_ids.get("PubMed") or "").strip()
            pmcid = str(paper.get("pmcid") or external_ids.get("PubMedCentral") or "").strip()
            citation_count = _safe_int(paper.get("citationCount") or paper.get("citation_count")) or 0
            provider_sources = _coerce_provider_sources(paper)
            provider_ids = _coerce_provider_ids(paper)
            combined_text = f"{title} {abstract}".strip()
            extracted = self.detect_entities(EntityDetectRequest(query_text=combined_text))
            lmp_matches = [
                alias
                for alias in lmp_aliases
                if alias and alias in combined_text.casefold()
            ]
            bsm_match = any(partner in combined_text.casefold() for partner in partner_terms)
            lexical = self._lexical_score(request.query_text, combined_text)
            exact_entity_score = 1.0 if lmp_matches else 0.0
            provider_conf = self._provider_confidence(provider_sources)
            semantic_proxy = min(1.0, (provider_conf * 0.6) + (exact_entity_score * 0.4))
            citation_score = min(1.0, math.log1p(max(0, citation_count)) / math.log1p(500))
            recency_score = self._recency_score(year)
            lmp_score = min(1.0, 0.5 + (0.1 * len(lmp_matches))) if lmp_matches else 0.0
            bsm_score = 0.5 if bsm_match else 0.0
            final_score = (
                lexical * request.ranking_policy.relevance_weight
                + semantic_proxy * request.ranking_policy.semantic_weight
                + recency_score * request.ranking_policy.recency_weight
                + citation_score * request.ranking_policy.citation_weight
                + lmp_score * request.ranking_policy.lmp_crossref_weight
                + bsm_score * request.ranking_policy.bsm_context_weight
                + exact_entity_score * request.ranking_policy.exact_entity_weight
            )
            fulltext_status = str(paper.get("acquisition_kind") or paper.get("content_type") or "abstract_only").strip()
            stable_key = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{doi}|{pmid}|{pmcid}|{title}|{index}",
            ).hex[:16]
            hit_id = f"{run_id}_{stable_key}"
            limitations: List[str] = []
            if fulltext_status in {"abstract_only", "metadata_only"}:
                limitations.append(fulltext_status)
            limitations.append("semantic_score_is_provider_proxy_not_biolinkbert_runtime")
            hit = {
                "hit_id": hit_id,
                "title": title,
                "snippet": snippet,
                "abstract": abstract,
                "authors": authors,
                "affiliations": list(paper.get("affiliations") or []),
                "institutions": list(paper.get("institutions") or []),
                "journal": journal,
                "publication_year": year,
                "publication_date": str(paper.get("publication_date") or ""),
                "doi": doi,
                "pmid": pmid,
                "pmcid": pmcid,
                "provider_ids": provider_ids,
                "provider_sources": provider_sources,
                "citation_count": citation_count,
                "open_access_status": str(((paper.get("openAccessPdf") or {}) if isinstance(paper.get("openAccessPdf"), dict) else {}).get("status") or paper.get("open_access_status") or ""),
                "fulltext_status": fulltext_status,
                "query_contributions": _dedupe_query_contributions(list(metadata.get("query_contributions") or [])),
                "gcs_artifact_refs": [],
                "entities": list(extracted.get("entities") or []),
                "proteins": list(extracted.get("proteins") or []),
                "genes": list(extracted.get("genes") or []),
                "organisms": list(extracted.get("organisms") or []),
                "lmp_matches": lmp_matches,
                "bsm_context": list(bsm_context.get("contexts") or []) if bsm_match or request.expansion_policy.use_bsm else [],
                "scores": {
                    "lexical_score": round(lexical, 6),
                    "semantic_score": round(semantic_proxy, 6),
                    "provider_confidence": round(provider_conf, 6),
                    "citation_score": round(citation_score, 6),
                    "recency_score": round(recency_score, 6),
                    "lmp_score": round(lmp_score, 6),
                    "bsm_score": round(bsm_score, 6),
                    "exact_entity_score": round(exact_entity_score, 6),
                    "final_score": round(final_score, 6),
                },
                "reasons": _dedupe_texts(
                    [
                        "provider_compiler_runtime_plan",
                        "lmp_crossref_match" if lmp_matches else "",
                        "bsm_partner_match" if bsm_match else "",
                        "exact_entity_match" if exact_entity_score else "",
                    ]
                ),
                "limitations": _dedupe_texts(limitations),
                "raw_payload": paper,
                "promotion_state": {},
            }
            hits.append(hit)
        return hits

    def _dedupe_hits(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped: Dict[str, Dict[str, Any]] = {}
        for hit in hits:
            key = _dedupe_identity_key(hit)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = hit
                continue
            deduped[key] = _merge_duplicate_hits(existing, hit)
        return list(deduped.values())

    def _build_analytics(
        self,
        *,
        hits: List[Dict[str, Any]],
        provider_attempts: List[Dict[str, Any]],
        query_variants: Sequence[str],
    ) -> Dict[str, Any]:
        journal_counts = Counter(hit.get("journal") for hit in hits if hit.get("journal"))
        author_counts = Counter(name for hit in hits for name in hit.get("authors", []) if name)
        protein_counts = Counter(name for hit in hits for name in hit.get("proteins", []) if name)
        gene_counts = Counter(name for hit in hits for name in hit.get("genes", []) if name)
        organism_counts = Counter(name for hit in hits for name in hit.get("organisms", []) if name)
        year_counts = Counter(str(hit.get("publication_year")) for hit in hits if hit.get("publication_year"))
        provider_counts = Counter(source for hit in hits for source in hit.get("provider_sources", []))
        open_access_counts = Counter(hit.get("open_access_status") or "unknown" for hit in hits)
        citation_distribution = {
            "zero": sum(1 for hit in hits if int(hit.get("citation_count") or 0) == 0),
            "one_to_ten": sum(1 for hit in hits if 1 <= int(hit.get("citation_count") or 0) <= 10),
            "eleven_to_fifty": sum(1 for hit in hits if 11 <= int(hit.get("citation_count") or 0) <= 50),
            "fifty_plus": sum(1 for hit in hits if int(hit.get("citation_count") or 0) > 50),
        }
        lmp_coverage = {
            "with_lmp_match": sum(1 for hit in hits if hit.get("lmp_matches")),
            "without_lmp_match": sum(1 for hit in hits if not hit.get("lmp_matches")),
        }
        bsm_coverage = {
            "with_bsm_context": sum(1 for hit in hits if hit.get("bsm_context")),
            "without_bsm_context": sum(1 for hit in hits if not hit.get("bsm_context")),
            "interaction_partner_frequency": Counter(
                partner.get("partner")
                for hit in hits
                for ctx in hit.get("bsm_context", [])
                for partner in ctx.get("top_partners", [])
                if partner.get("partner")
            ).most_common(10),
        }
        return {
            "top_institutions": [],
            "top_journals": [{"journal": key, "count": count} for key, count in journal_counts.most_common(10)],
            "top_authors": [{"author": key, "count": count} for key, count in author_counts.most_common(10)],
            "top_years": [{"year": key, "count": count} for key, count in year_counts.most_common(15)],
            "top_proteins": [{"protein": key, "count": count} for key, count in protein_counts.most_common(10)],
            "top_genes": [{"gene": key, "count": count} for key, count in gene_counts.most_common(10)],
            "top_methods": [],
            "top_organisms": [{"organism": key, "count": count} for key, count in organism_counts.most_common(10)],
            "provider_breakdown": [{"provider": key, "count": count} for key, count in provider_counts.most_common()],
            "provider_attempts": provider_attempts,
            "open_access_breakdown": dict(open_access_counts),
            "citation_distribution": citation_distribution,
            "institution_country_breakdown": [],
            "journal_publisher_breakdown": [],
            "bsm_interaction_context": bsm_coverage,
            "lmp_context_coverage": lmp_coverage,
            "query_expansion_contribution": {
                "base_hits": len(hits),
                "lmp_expanded_hits": sum(1 for hit in hits if hit.get("lmp_matches")),
                "bsm_expanded_hits": sum(1 for hit in hits if hit.get("bsm_context")),
                "citation_expanded_hits": 0,
                "query_variants": list(query_variants),
            },
        }

    async def _persist_run_and_hits(self, *, run_payload: Dict[str, Any], hits: List[Dict[str, Any]]) -> None:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alejandria_search_runs (
                    run_id, user_id, workspace_id, study_id, kb_id, working_set_id, query_text, search_mode,
                    query_spec_hash, status, request_payload, provider_execution_plan, expansion_trace,
                    provider_attempts, provider_failures, hit_count_raw, hit_count_deduped, hit_count_ranked,
                    analytics_summary, artifact_refs, gcs_artifact_refs, kb_refs, limitations, trace_payload, created_at, completed_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11::jsonb, $12::jsonb, $13::jsonb,
                    $14::jsonb, $15::jsonb, $16, $17, $18,
                    $19::jsonb, $20::jsonb, $21::jsonb, $22::jsonb, $23::jsonb, $24::jsonb, $25::timestamptz, $26::timestamptz
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    provider_attempts = EXCLUDED.provider_attempts,
                    provider_failures = EXCLUDED.provider_failures,
                    analytics_summary = EXCLUDED.analytics_summary,
                    artifact_refs = EXCLUDED.artifact_refs,
                    gcs_artifact_refs = EXCLUDED.gcs_artifact_refs,
                    kb_refs = EXCLUDED.kb_refs,
                    limitations = EXCLUDED.limitations,
                    trace_payload = EXCLUDED.trace_payload,
                    completed_at = EXCLUDED.completed_at
                """,
                run_payload["run_id"],
                run_payload["user_id"],
                run_payload["workspace_id"],
                run_payload["study_id"],
                run_payload["kb_id"],
                run_payload["working_set_id"],
                run_payload["query_text"],
                run_payload["search_mode"],
                run_payload["query_spec_hash"],
                run_payload["status"],
                json.dumps(run_payload["request"]),
                json.dumps(run_payload["provider_execution_plan"]),
                json.dumps(run_payload["expansion_trace"]),
                json.dumps(run_payload["provider_attempts"]),
                json.dumps(run_payload["provider_failures"]),
                run_payload["hit_count_raw"],
                run_payload["hit_count_deduped"],
                run_payload["hit_count_ranked"],
                json.dumps(run_payload["analytics_summary"]),
                json.dumps(run_payload["artifact_refs"]),
                json.dumps(run_payload.get("gcs_artifact_refs", [])),
                json.dumps(run_payload["kb_refs"]),
                json.dumps(run_payload["limitations"]),
                json.dumps(run_payload["trace_payload"]),
                run_payload["created_at"],
                run_payload["completed_at"],
            )
            await conn.execute("DELETE FROM alejandria_search_hits WHERE run_id = $1", run_payload["run_id"])
            for hit in hits:
                await conn.execute(
                    """
                    INSERT INTO alejandria_search_hits (
                        hit_id, run_id, title, snippet, abstract, authors, affiliations, institutions, journal,
                        publication_year, publication_date, doi, pmid, pmcid, provider_ids, provider_sources,
                        citation_count, open_access_status, fulltext_status, entities, proteins, genes, organisms,
                        lmp_matches, bsm_context, scores, reasons, limitations, query_contributions, gcs_artifact_refs, raw_payload, promotion_state
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9,
                        $10, $11, $12, $13, $14, $15::jsonb, $16::jsonb,
                        $17, $18, $19, $20::jsonb, $21::jsonb, $22::jsonb, $23::jsonb,
                        $24::jsonb, $25::jsonb, $26::jsonb, $27::jsonb, $28::jsonb, $29::jsonb, $30::jsonb, $31::jsonb, $32::jsonb
                    )
                    """,
                    hit["hit_id"],
                    run_payload["run_id"],
                    hit["title"],
                    hit["snippet"],
                    hit["abstract"],
                    json.dumps(hit["authors"]),
                    json.dumps(hit["affiliations"]),
                    json.dumps(hit["institutions"]),
                    hit["journal"],
                    hit["publication_year"],
                    hit["publication_date"],
                    hit["doi"],
                    hit["pmid"],
                    hit["pmcid"],
                    json.dumps(hit["provider_ids"]),
                    json.dumps(hit["provider_sources"]),
                    hit["citation_count"],
                    hit["open_access_status"],
                    hit["fulltext_status"],
                    json.dumps(hit["entities"]),
                    json.dumps(hit["proteins"]),
                    json.dumps(hit["genes"]),
                    json.dumps(hit["organisms"]),
                    json.dumps(hit["lmp_matches"]),
                    json.dumps(hit["bsm_context"]),
                    json.dumps(hit["scores"]),
                    json.dumps(hit["reasons"]),
                    json.dumps(hit["limitations"]),
                    json.dumps(hit.get("query_contributions", [])),
                    json.dumps(hit.get("gcs_artifact_refs", [])),
                    json.dumps(hit["raw_payload"]),
                    json.dumps(hit["promotion_state"]),
                )

    async def _load_selected_hits(self, *, run_id: str, user_id: str, hit_ids: Sequence[str]) -> List[Dict[str, Any]]:
        await self.get_run(run_id=run_id, user_id=user_id)
        requested = set(hit_ids or [])
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM alejandria_search_hits WHERE run_id = $1 ORDER BY created_at ASC",
                run_id,
            )
        hits = [self._row_to_hit(row) for row in rows]
        if not requested:
            return hits
        selected = [hit for hit in hits if hit["hit_id"] in requested]
        if len(selected) != len(requested):
            missing = sorted(requested - {hit["hit_id"] for hit in selected})
            raise ValueError(f"missing_hit_ids:{','.join(missing)}")
        return selected

    async def _ensure_hit_artifact(
        self,
        *,
        conn: Any,
        run_id: str,
        user_id: str,
        hit: Dict[str, Any],
    ) -> str:
        promotion_state = dict(hit.get("promotion_state") or {})
        artifact_id = str(promotion_state.get("artifact_id") or "").strip()
        if artifact_id:
            exists = await conn.fetchrow(
                "SELECT artifact_id FROM artifacts WHERE artifact_id = $1 AND user_id = $2",
                artifact_id,
                user_id,
            )
            if exists:
                return artifact_id

        row = await conn.fetchrow(
            """
            INSERT INTO artifacts (
                user_id, artifact_type, display_name, source, ref_url, metadata
            ) VALUES (
                $1, 'paper', $2, 'alejandria_search', $3, $4::jsonb
            )
            RETURNING artifact_id
            """,
            user_id,
            hit["title"],
            self._reference_url_for_hit(hit),
            json.dumps(
                {
                    "search_lineage": {"run_id": run_id, "hit_id": hit["hit_id"]},
                    "doi": hit.get("doi") or "",
                    "pmid": hit.get("pmid") or "",
                    "pmcid": hit.get("pmcid") or "",
                    "provider_sources": hit.get("provider_sources") or [],
                    "provider_ids": hit.get("provider_ids") or [],
                    "abstract": hit.get("abstract") or "",
                    "entities": hit.get("entities") or [],
                    "proteins": hit.get("proteins") or [],
                    "genes": hit.get("genes") or [],
                    "organisms": hit.get("organisms") or [],
                    "lmp_matches": hit.get("lmp_matches") or [],
                    "bsm_context": hit.get("bsm_context") or [],
                }
            ),
        )
        artifact_id = str(row["artifact_id"])
        await conn.execute(
            """
            INSERT INTO artifact_lineage (
                artifact_id, source_receipt_ref, lineage_type, metadata
            ) VALUES (
                $1, $2, 'derived_from_search_hit', $3::jsonb
            )
            """,
            artifact_id,
            f"alejandria_search:{run_id}:{hit['hit_id']}",
            json.dumps({"run_id": run_id, "hit_id": hit["hit_id"]}),
        )
        return artifact_id

    async def _update_hit_promotion_state(self, *, conn: Any, hit_id: str, merge_payload: Dict[str, Any]) -> None:
        row = await conn.fetchrow(
            "SELECT promotion_state FROM alejandria_search_hits WHERE hit_id = $1",
            hit_id,
        )
        current = _parse_jsonb(row["promotion_state"], {}) if row else {}
        current.update(merge_payload)
        await conn.execute(
            "UPDATE alejandria_search_hits SET promotion_state = $2::jsonb WHERE hit_id = $1",
            hit_id,
            json.dumps(current),
        )

    async def _append_run_refs(
        self,
        *,
        conn: Any,
        run_id: str,
        artifact_refs: Sequence[str],
        kb_refs: Sequence[str],
    ) -> None:
        row = await conn.fetchrow(
            "SELECT artifact_refs, kb_refs FROM alejandria_search_runs WHERE run_id = $1",
            run_id,
        )
        current_artifacts = list(_parse_jsonb(row["artifact_refs"], []) if row else [])
        current_kb_refs = list(_parse_jsonb(row["kb_refs"], []) if row else [])
        updated_artifacts = _dedupe_texts([*current_artifacts, *artifact_refs])
        updated_kb_refs = _dedupe_texts([*current_kb_refs, *kb_refs])
        await conn.execute(
            """
            UPDATE alejandria_search_runs
            SET artifact_refs = $2::jsonb, kb_refs = $3::jsonb
            WHERE run_id = $1
            """,
            run_id,
            json.dumps(updated_artifacts),
            json.dumps(updated_kb_refs),
        )

    def _row_to_run(self, row: Any) -> Dict[str, Any]:
        return {
            "run_id": str(row["run_id"]),
            "user_id": row["user_id"],
            "workspace_id": row["workspace_id"] or "",
            "study_id": row["study_id"],
            "kb_id": row["kb_id"],
            "working_set_id": row["working_set_id"],
            "query_text": row["query_text"],
            "search_mode": row["search_mode"],
            "query_spec_hash": row["query_spec_hash"] or "",
            "status": row["status"],
            "request": _parse_jsonb(row["request_payload"], {}),
            "provider_execution_plan": _parse_jsonb(row["provider_execution_plan"], {}),
            "expansion_trace": _parse_jsonb(row["expansion_trace"], []),
            "provider_attempts": _parse_jsonb(row["provider_attempts"], []),
            "provider_failures": _parse_jsonb(row["provider_failures"], []),
            "hit_count_raw": int(row["hit_count_raw"] or 0),
            "hit_count_deduped": int(row["hit_count_deduped"] or 0),
            "hit_count_ranked": int(row["hit_count_ranked"] or 0),
            "analytics_summary": _parse_jsonb(row["analytics_summary"], {}),
            "artifact_refs": _parse_jsonb(row["artifact_refs"], []),
            "gcs_artifact_refs": _parse_jsonb(row["gcs_artifact_refs"] if "gcs_artifact_refs" in row else [], []),
            "kb_refs": _parse_jsonb(row["kb_refs"], []),
            "limitations": _parse_jsonb(row["limitations"], []),
            "trace_payload": _parse_jsonb(row["trace_payload"], {}),
            "created_at": row["created_at"].isoformat() if row["created_at"] else "",
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else "",
        }

    def _row_to_hit(self, row: Any) -> Dict[str, Any]:
        return {
            "hit_id": row["hit_id"],
            "run_id": row["run_id"],
            "title": row["title"],
            "snippet": row["snippet"] or "",
            "abstract": row["abstract"] or "",
            "authors": _parse_jsonb(row["authors"], []),
            "affiliations": _parse_jsonb(row["affiliations"], []),
            "institutions": _parse_jsonb(row["institutions"], []),
            "journal": row["journal"] or "",
            "publication_year": row["publication_year"],
            "publication_date": row["publication_date"] or "",
            "doi": row["doi"] or "",
            "pmid": row["pmid"] or "",
            "pmcid": row["pmcid"] or "",
            "provider_ids": _parse_jsonb(row["provider_ids"], []),
            "provider_sources": _parse_jsonb(row["provider_sources"], []),
            "citation_count": int(row["citation_count"] or 0),
            "open_access_status": row["open_access_status"] or "",
            "fulltext_status": row["fulltext_status"] or "",
            "entities": _parse_jsonb(row["entities"], []),
            "proteins": _parse_jsonb(row["proteins"], []),
            "genes": _parse_jsonb(row["genes"], []),
            "organisms": _parse_jsonb(row["organisms"], []),
            "lmp_matches": _parse_jsonb(row["lmp_matches"], []),
            "bsm_context": _parse_jsonb(row["bsm_context"], []),
            "scores": _parse_jsonb(row["scores"], {}),
            "reasons": _parse_jsonb(row["reasons"], []),
            "limitations": _parse_jsonb(row["limitations"], []),
            "query_contributions": _parse_jsonb(row["query_contributions"] if "query_contributions" in row else [], []),
            "gcs_artifact_refs": _parse_jsonb(row["gcs_artifact_refs"] if "gcs_artifact_refs" in row else [], []),
            "raw_payload": _parse_jsonb(row["raw_payload"], {}),
            "promotion_state": _parse_jsonb(row["promotion_state"], {}),
        }

    def _reference_url_for_hit(self, hit: Dict[str, Any]) -> str:
        if hit.get("doi"):
            return f"https://doi.org/{hit['doi']}"
        raw_payload = dict(hit.get("raw_payload") or {})
        open_access = raw_payload.get("openAccessPdf") or {}
        if isinstance(open_access, dict) and open_access.get("url"):
            return str(open_access["url"])
        return str(raw_payload.get("source_url") or "")

    def _canonical_paper_id_for_hit(self, hit: Dict[str, Any]) -> str:
        return str(hit.get("doi") or hit.get("pmid") or hit.get("pmcid") or hit.get("hit_id") or "")

    def _hit_matches_filters(self, hit: Dict[str, Any], filters: AlejandriaSearchFilters) -> bool:
        if filters.year_from and (not hit.get("publication_year") or int(hit["publication_year"]) < filters.year_from):
            return False
        if filters.year_to and (not hit.get("publication_year") or int(hit["publication_year"]) > filters.year_to):
            return False
        if filters.journals and str(hit.get("journal") or "").casefold() not in {item.casefold() for item in filters.journals}:
            return False
        if filters.authors and not any(author.casefold() in {item.casefold() for item in filters.authors} for author in hit.get("authors", [])):
            return False
        if filters.providers and not any(source.casefold() in {item.casefold() for item in filters.providers} for source in hit.get("provider_sources", [])):
            return False
        if filters.organisms and not any(item.casefold() in {value.casefold() for value in filters.organisms} for item in hit.get("organisms", [])):
            return False
        if filters.proteins and not any(item.casefold() in {value.casefold() for value in filters.proteins} for item in hit.get("proteins", [])):
            return False
        if filters.genes and not any(item.casefold() in {value.casefold() for value in filters.genes} for item in hit.get("genes", [])):
            return False
        if filters.open_access_only and not hit.get("open_access_status"):
            return False
        if filters.has_fulltext and str(hit.get("fulltext_status") or "") in {"abstract_only", "metadata_only", ""}:
            return False
        if filters.has_lmp_context and not hit.get("lmp_matches"):
            return False
        if filters.has_bsm_context and not hit.get("bsm_context"):
            return False
        if filters.citation_count_min and int(hit.get("citation_count") or 0) < filters.citation_count_min:
            return False
        return True

    def _sort_value(self, hit: Dict[str, Any], sort_by: str) -> Any:
        if sort_by == "citation_count":
            return int(hit.get("citation_count") or 0)
        if sort_by == "publication_year":
            return int(hit.get("publication_year") or 0)
        return float(((hit.get("scores") or {}).get(sort_by) or (hit.get("scores") or {}).get("final_score") or 0.0))

    def _lexical_score(self, query_text: str, combined_text: str) -> float:
        query_terms = set(_tokenize(query_text))
        hit_terms = set(_tokenize(combined_text))
        if not query_terms or not hit_terms:
            return 0.0
        return len(query_terms & hit_terms) / max(1.0, len(query_terms))

    def _provider_confidence(self, provider_sources: Sequence[str]) -> float:
        if not provider_sources:
            return 0.4
        scores = [_SOURCE_CONFIDENCE.get(str(source).strip().lower(), 0.55) for source in provider_sources]
        return min(1.0, sum(scores) / len(scores))

    def _recency_score(self, year: Optional[int]) -> float:
        if not year:
            return 0.0
        age = max(0, _CURRENT_YEAR - int(year))
        return max(0.0, 1.0 - min(age, 20) / 20.0)


_service: Optional[AlejandriaSearchService] = None


def get_alejandria_search_service() -> AlejandriaSearchService:
    global _service
    if _service is None:
        _service = AlejandriaSearchService()
    return _service
