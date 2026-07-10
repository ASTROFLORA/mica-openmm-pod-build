from __future__ import annotations

import hashlib
import os
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence


_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.IGNORECASE)
_ARXIV_PREFIX_RE = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)\s*", re.IGNORECASE)
_PMCID_RE = re.compile(r"^PMC\d+$", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d+$")

OPENALEX_PDF_DOWNLOAD_COST_USD = 0.01
_ALLOWED_LITERATURE_SOURCES = ("semantic_scholar", "pubmed", "openalex", "biorxiv")
_DEFAULT_LITERATURE_RATE_LIMITS = {
    "semantic_scholar": 100,
    "pubmed": 600,
    "pmc": 180,
    "openalex": 120,
    "openalex_content": 60,
    "biorxiv": 20,
    "unpaywall": 120,
}
_RATE_LIMIT_ALIASES = {
    "ss_rpm": "semantic_scholar",
    "semantic_scholar_rpm": "semantic_scholar",
    "semantic_scholar": "semantic_scholar",
    "pubmed_rpm": "pubmed",
    "pubmed": "pubmed",
    "pmc_rpm": "pmc",
    "pmc": "pmc",
    "openalex_rpm": "openalex",
    "openalex": "openalex",
    "openalex_content_rpm": "openalex_content",
    "openalex_content": "openalex_content",
    "biorxiv_rpm": "biorxiv",
    "biorxiv": "biorxiv",
    "unpaywall_rpm": "unpaywall",
    "unpaywall": "unpaywall",
}
_DEFAULT_LITERATURE_SOURCES_BY_LANE = {
    "general": ("semantic_scholar", "pubmed", "openalex"),
    "driver_search": ("semantic_scholar", "pubmed", "openalex"),
    "deep_research": ("semantic_scholar", "pubmed", "openalex"),
    "bibliotecario_review": ("semantic_scholar", "pubmed", "openalex"),
    "research_orchestrator": ("semantic_scholar", "pubmed", "openalex"),
    "entity_scan": ("semantic_scholar",),
}


def normalize_doi(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _DOI_PREFIX_RE.sub("", text)
    text = text.strip().strip(" \t\r\n.,;:)\"]")
    return text.lower()


def normalize_pmid(value: str | None) -> str:
    text = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    return text if _PMID_RE.fullmatch(text or "") else ""


def normalize_pmcid(value: str | None) -> str:
    text = str(value or "").strip().upper()
    if text and not text.startswith("PMC") and text.isdigit():
        text = f"PMC{text}"
    return text if _PMCID_RE.fullmatch(text or "") else ""


def normalize_arxiv_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _ARXIV_PREFIX_RE.sub("", text)
    text = text.removesuffix(".pdf").strip().strip("/")
    return text.lower()


def build_canonical_paper_id(
    *,
    doi: str | None = None,
    pmid: str | None = None,
    arxiv_id: str | None = None,
    platform: str = "",
    paper_id: str = "",
) -> str:
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"doi:{normalized_doi}"
    normalized_pmid = normalize_pmid(pmid)
    if normalized_pmid:
        return f"pmid:{normalized_pmid}"
    normalized_arxiv = normalize_arxiv_id(arxiv_id)
    if normalized_arxiv:
        return f"arxiv:{normalized_arxiv}"
    if str(platform or "").strip() and str(paper_id or "").strip():
        return f"{str(platform).strip().lower()}:{str(paper_id).strip()}"
    return str(paper_id or "").strip()


def reconstruct_openalex_abstract(abstract_inverted_index: Any) -> str:
    if not isinstance(abstract_inverted_index, dict):
        return ""
    indexed_tokens: Dict[int, str] = {}
    for token, positions in abstract_inverted_index.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for position in positions:
            try:
                indexed_tokens[int(position)] = token
            except (TypeError, ValueError):
                continue
    if not indexed_tokens:
        return ""
    ordered = [indexed_tokens[idx] for idx in sorted(indexed_tokens)]
    return " ".join(part for part in ordered if part).strip()


def normalize_openalex_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("https://openalex.org/"):
        text = text.rsplit("/", 1)[-1]
    return text.strip()


def normalize_literature_title(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def build_title_identity_key(title: str | None) -> str:
    normalized = normalize_literature_title(title)
    if not normalized:
        return ""
    return f"title:{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:16]}"


def build_preferred_literature_id(
    *,
    doi: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
    platform: str = "",
    paper_id: str = "",
    canonical_id: str = "",
) -> str:
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"doi:{normalized_doi}"
    normalized_pmid = normalize_pmid(pmid)
    if normalized_pmid:
        return f"pmid:{normalized_pmid}"
    normalized_pmcid = normalize_pmcid(pmcid)
    if normalized_pmcid:
        return f"pmcid:{normalized_pmcid}"
    normalized_arxiv = normalize_arxiv_id(arxiv_id)
    if normalized_arxiv:
        return f"arxiv:{normalized_arxiv}"
    title_key = build_title_identity_key(title)
    if title_key:
        return title_key
    if str(canonical_id or "").strip():
        return str(canonical_id).strip()
    return build_canonical_paper_id(
        doi=doi,
        pmid=pmid,
        arxiv_id=arxiv_id,
        platform=platform,
        paper_id=paper_id,
    )


def build_literature_identity_keys(
    *,
    canonical_id: str | None = None,
    doi: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
    platform: str = "",
    paper_id: str = "",
) -> List[str]:
    keys = dedupe_texts(
        [
            build_preferred_literature_id(
                doi=doi,
                pmid=pmid,
                pmcid=pmcid,
                arxiv_id=arxiv_id,
                title=title,
                platform=platform,
                paper_id=paper_id,
                canonical_id=str(canonical_id or ""),
            ),
            str(canonical_id or "").strip(),
            f"doi:{normalize_doi(doi)}" if normalize_doi(doi) else "",
            f"pmid:{normalize_pmid(pmid)}" if normalize_pmid(pmid) else "",
            f"pmcid:{normalize_pmcid(pmcid)}" if normalize_pmcid(pmcid) else "",
            f"arxiv:{normalize_arxiv_id(arxiv_id)}" if normalize_arxiv_id(arxiv_id) else "",
            build_title_identity_key(title),
            f"{str(platform or '').strip().lower()}:{str(paper_id or '').strip()}" if str(platform or '').strip() and str(paper_id or '').strip() else "",
        ]
    )
    return [key for key in keys if key]


def resolve_literature_rate_limits(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    limits = {key: int(value) for key, value in _DEFAULT_LITERATURE_RATE_LIMITS.items()}
    raw = str(os.getenv("LITERATURE_RATE_LIMITS", "") or "").strip()
    parsed: Dict[str, Any] = {}
    if raw:
        try:
            candidate = json.loads(raw)
            if isinstance(candidate, dict):
                parsed = dict(candidate)
        except Exception:
            for part in raw.split(","):
                text = str(part or "").strip()
                if not text or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                parsed[key.strip()] = value.strip()
    for part in (parsed, overrides or {}):
        for raw_key, raw_value in dict(part).items():
            provider = _RATE_LIMIT_ALIASES.get(str(raw_key or "").strip().lower())
            if not provider:
                continue
            try:
                value = max(1, int(float(raw_value)))
            except (TypeError, ValueError):
                continue
            limits[provider] = value
    return limits


def _pdf_url_from_location(location: Dict[str, Any]) -> str:
    if not isinstance(location, dict):
        return ""
    for key in ("pdf_url", "landing_page_url"):
        value = str(location.get(key) or "").strip()
        if value:
            return value
    return ""


def iter_openalex_pdf_candidates(work: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for field_name in ("best_oa_location", "primary_location"):
        location = work.get(field_name)
        if isinstance(location, dict):
            url = _pdf_url_from_location(location)
            if url:
                candidates.append(
                    {
                        "source": field_name,
                        "url": url,
                        "license": str(location.get("license") or "unknown"),
                        "is_oa": bool(location.get("is_oa")),
                        "version": str(location.get("version") or ""),
                    }
                )
    for location in work.get("locations") or []:
        if not isinstance(location, dict):
            continue
        url = _pdf_url_from_location(location)
        if not url:
            continue
        candidates.append(
            {
                "source": "locations",
                "url": url,
                "license": str(location.get("license") or "unknown"),
                "is_oa": bool(location.get("is_oa")),
                "version": str(location.get("version") or ""),
            }
        )
    deduped: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(candidate)
    return deduped


def best_openalex_pdf_candidate(work: Dict[str, Any]) -> Dict[str, Any]:
    for candidate in iter_openalex_pdf_candidates(work):
        if candidate.get("is_oa"):
            return candidate
    return iter_openalex_pdf_candidates(work)[0] if iter_openalex_pdf_candidates(work) else {}


@dataclass
class LiteratureAcquisitionAudit:
    acquisition_source: str
    acquisition_method: str
    acquisition_status: str
    acquisition_cost_usd: float = 0.0
    acquisition_attempts: int = 1
    http_status: Optional[int] = None
    provider_latency_ms: Optional[int] = None
    access_tier: str = "unknown"
    is_open_access: Optional[bool] = None
    license_status: str = "unknown"
    content_url: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LiteratureBudgetSnapshot:
    tenant_id: str = "default"
    max_budget_usd: Optional[float] = None
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    denied_costs_usd: float = 0.0
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def remaining_usd(self) -> Optional[float]:
        if self.max_budget_usd is None:
            return None
        return round(max(0.0, self.max_budget_usd - self.spent_usd - self.reserved_usd), 6)

    def can_spend(self, cost_usd: float) -> bool:
        if cost_usd <= 0:
            return True
        remaining = self.remaining_usd
        return remaining is None or remaining >= cost_usd

    def record(self, *, kind: str, amount_usd: float, accepted: bool, detail: str = "") -> None:
        amount = round(max(0.0, float(amount_usd or 0.0)), 6)
        if accepted:
            self.spent_usd = round(self.spent_usd + amount, 6)
        else:
            self.denied_costs_usd = round(self.denied_costs_usd + amount, 6)
        self.events.append(
            {
                "kind": kind,
                "amount_usd": amount,
                "accepted": bool(accepted),
                "detail": str(detail or ""),
                "remaining_usd": self.remaining_usd,
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "max_budget_usd": self.max_budget_usd,
            "spent_usd": self.spent_usd,
            "reserved_usd": self.reserved_usd,
            "denied_costs_usd": self.denied_costs_usd,
            "remaining_usd": self.remaining_usd,
            "events": list(self.events),
        }


def default_tenant_id_for_user(user_id: str | None) -> str:
    text = str(user_id or "").strip()
    return f"user:{text}" if text else "default"


def build_literature_budget_snapshot(
    *,
    tenant_id: str | None = None,
    max_budget_usd: Optional[float] = None,
    spent_usd: float = 0.0,
    reserved_usd: float = 0.0,
    denied_costs_usd: float = 0.0,
    events: Optional[Sequence[Dict[str, Any]]] = None,
) -> LiteratureBudgetSnapshot:
    snapshot = LiteratureBudgetSnapshot(
        tenant_id=str(tenant_id or "default"),
        max_budget_usd=max_budget_usd,
        spent_usd=float(spent_usd or 0.0),
        reserved_usd=float(reserved_usd or 0.0),
        denied_costs_usd=float(denied_costs_usd or 0.0),
    )
    for event in list(events or []):
        if isinstance(event, dict):
            snapshot.events.append(dict(event))
    return snapshot


def _safe_stripped_text(value: Any) -> str:
    return str(value or "").strip()


def _license_type_for_record(provider: str, metadata: Dict[str, Any], pdf_url: str) -> str:
    provider_name = str(provider or "").strip().lower()
    if provider_name == "openalex":
        candidate = dict(metadata.get("oa_candidate") or {})
        if str(candidate.get("license") or "").strip():
            return str(candidate.get("license") or "").strip()
        open_access = dict(metadata.get("open_access") or {})
        if str(open_access.get("license") or "").strip():
            return str(open_access.get("license") or "").strip()
    if provider_name == "biorxiv":
        return "open"
    if pdf_url:
        return "open"
    return "unknown"


def _is_open_access_record(provider: str, metadata: Dict[str, Any], pdf_url: str) -> Optional[bool]:
    provider_name = str(provider or "").strip().lower()
    if provider_name == "openalex":
        candidate = dict(metadata.get("oa_candidate") or {})
        if "is_oa" in candidate:
            return bool(candidate.get("is_oa"))
        open_access = dict(metadata.get("open_access") or {})
        if "is_oa" in open_access:
            return bool(open_access.get("is_oa"))
    if provider_name == "biorxiv":
        return True
    if pdf_url:
        return True
    return None


def _source_url_for_record(
    *,
    provider: str,
    paper_id: str,
    doi: str,
    pmid: str,
    pdf_url: str,
    metadata: Dict[str, Any],
) -> str:
    explicit = _safe_stripped_text(metadata.get("source_url") or metadata.get("official_url"))
    if explicit:
        return explicit
    if pdf_url:
        return pdf_url

    provider_name = str(provider or "").strip().lower()
    if provider_name == "openalex":
        work = dict(metadata.get("openalex_data") or {})
        if _safe_stripped_text(work.get("id")):
            return _safe_stripped_text(work.get("id"))
        openalex_id = _safe_stripped_text(metadata.get("openalex_id"))
        if openalex_id:
            return f"https://openalex.org/{openalex_id}"
    if provider_name == "pubmed" and pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if provider_name == "biorxiv" and doi:
        return f"https://www.biorxiv.org/content/{doi}v1"
    if provider_name == "semantic_scholar" and paper_id:
        return f"https://www.semanticscholar.org/paper/{paper_id}"
    return ""


def _content_type_for_record(full_text: str, abstract: str) -> str:
    if _safe_stripped_text(full_text):
        return "full_text"
    if _safe_stripped_text(abstract):
        return "abstract"
    return "metadata"


def _degradation_reason_for_record(content_type: str) -> str:
    normalized = str(content_type or "metadata").strip().lower() or "metadata"
    if normalized == "full_text":
        return ""
    if normalized == "abstract":
        return "abstract_only"
    return "metadata_only"


def build_literature_paper_record(
    *,
    paper_id: str,
    canonical_id: str,
    provider: str,
    title: str,
    abstract: str = "",
    full_text: str = "",
    year: Optional[int] = None,
    doi: str = "",
    pmid: str = "",
    pmcid: str = "",
    arxiv_id: str = "",
    pdf_url: str = "",
    authors: Optional[Sequence[Dict[str, Any]]] = None,
    external_ids: Optional[Dict[str, str]] = None,
    citation_count: Optional[int] = None,
    reference_count: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    backend: str = "",
    fetch_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    metadata_dict = dict(metadata or {})
    normalized_provider = _safe_stripped_text(provider).lower() or "unknown"
    canonical_identity = build_preferred_literature_id(
        doi=doi,
        pmid=pmid,
        pmcid=pmcid or metadata_dict.get("pmcid"),
        arxiv_id=arxiv_id,
        title=title,
        platform=normalized_provider,
        paper_id=paper_id,
        canonical_id=canonical_id,
    )
    identity_keys = build_literature_identity_keys(
        canonical_id=canonical_identity,
        doi=doi,
        pmid=pmid,
        pmcid=pmcid or metadata_dict.get("pmcid"),
        arxiv_id=arxiv_id,
        title=title,
        platform=normalized_provider,
        paper_id=paper_id,
    )
    provider_id = _safe_stripped_text(
        metadata_dict.get("provider_id")
        or metadata_dict.get("openalex_id")
        or pmid
        or doi
        or paper_id
    )
    normalized_pdf_url = _safe_stripped_text(pdf_url)
    source_url = _source_url_for_record(
        provider=normalized_provider,
        paper_id=_safe_stripped_text(paper_id),
        doi=normalize_doi(doi),
        pmid=normalize_pmid(pmid),
        pdf_url=normalized_pdf_url,
        metadata=metadata_dict,
    )
    content_type = _content_type_for_record(full_text, abstract)
    degradation_reason = _degradation_reason_for_record(content_type)
    license_type = _license_type_for_record(normalized_provider, metadata_dict, normalized_pdf_url)
    is_open_access = _is_open_access_record(normalized_provider, metadata_dict, normalized_pdf_url)
    audit = LiteratureAcquisitionAudit(
        acquisition_source=normalized_provider,
        acquisition_method="search_api",
        acquisition_status="success",
        access_tier="open_access" if is_open_access else "metadata_only",
        is_open_access=is_open_access,
        license_status=license_type,
        content_url=source_url,
        detail=degradation_reason,
    )
    provenance = {
        "provider": normalized_provider,
        "provider_id": provider_id,
        "pmid": normalize_pmid(pmid),
        "pmcid": normalize_pmcid(pmcid or metadata_dict.get("pmcid")),
        "doi": normalize_doi(doi),
        "source_url": source_url,
        "fetch_timestamp": fetch_timestamp or datetime.now(timezone.utc).isoformat(),
        "content_type": content_type,
        "license_type": license_type,
    }
    content_uri = _safe_stripped_text(metadata_dict.get("content_uri"))
    normalized_text_uri = _safe_stripped_text(metadata_dict.get("normalized_text_uri"))
    section_json_uri = _safe_stripped_text(metadata_dict.get("section_json_uri"))
    citations_json_uri = _safe_stripped_text(metadata_dict.get("citations_json_uri"))
    return {
        "paperId": _safe_stripped_text(paper_id),
        "canonical_id": _safe_stripped_text(canonical_identity),
        "source": normalized_provider,
        "source_id": _safe_stripped_text(canonical_identity),
        "provider": normalized_provider,
        "provider_id": provider_id,
        "title": _safe_stripped_text(title),
        "abstract": _safe_stripped_text(abstract) or None,
        "full_text": _safe_stripped_text(full_text) or None,
        "year": year,
        "citationCount": citation_count,
        "referenceCount": reference_count,
        "doi": provenance["doi"] or None,
        "pmid": provenance["pmid"] or None,
        "pmcid": provenance["pmcid"] or None,
        "arxivId": normalize_arxiv_id(arxiv_id) or None,
        "authors": list(authors or []),
        "externalIds": dict(external_ids or {}),
        "openAccessPdf": {"url": normalized_pdf_url} if normalized_pdf_url else None,
        "pdf_url": normalized_pdf_url or None,
        "backend": _safe_stripped_text(backend),
        "source_url": source_url,
        "official_url": source_url,
        "fetch_timestamp": provenance["fetch_timestamp"],
        "content_type": content_type,
        "license_type": license_type,
        "is_full_text": content_type == "full_text",
        "is_open_access": is_open_access,
        "degradation_reason": degradation_reason or None,
        "acquisition_audit": [audit.to_dict()],
        "content_uri": content_uri or None,
        "normalized_text_uri": normalized_text_uri or None,
        "section_json_uri": section_json_uri or None,
        "citations_json_uri": citations_json_uri or None,
        "sections": list(metadata_dict.get("sections") or []),
        "citations": list(metadata_dict.get("citations") or []),
        "acquisition_kind": _safe_stripped_text(metadata_dict.get("acquisition_kind")) or None,
        "graph_worthiness_score": metadata_dict.get("graph_worthiness_score"),
        "persistence_eligible": metadata_dict.get("persistence_eligible"),
        "persistence_reason": metadata_dict.get("persistence_reason"),
        "metadata": merge_metadata(
            metadata_dict,
            {
                "provenance": provenance,
                "identity_keys": identity_keys,
            },
        ),
    }


def build_literature_request_envelope(
    *,
    query: str,
    queries: Sequence[str],
    requested_sources: Sequence[str],
    attempted_sources: Sequence[str],
    failed_sources: Sequence[str],
    source_counts: Dict[str, Any],
    source_health: Dict[str, Any],
    retrieval_policy: Optional[Dict[str, Any]] = None,
    budget_snapshot: Optional[LiteratureBudgetSnapshot] = None,
    session_id: str | None = None,
    run_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    search_log: Optional[Sequence[str]] = None,
    failure_records: Optional[Sequence[Dict[str, Any]]] = None,
    provider_controls: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    budget = budget_snapshot or build_literature_budget_snapshot(
        tenant_id=tenant_id or default_tenant_id_for_user(user_id),
    )
    policy = dict(retrieval_policy or {})
    return {
        "query": _safe_stripped_text(query),
        "queries": list(queries or []),
        "requested_sources": list(requested_sources or []),
        "attempted_sources": list(attempted_sources or []),
        "failed_sources": list(failed_sources or []),
        "source_counts": dict(source_counts or {}),
        "source_health": dict(source_health or {}),
        "retrieval_policy": policy,
        "session_scope": {
            "session_id": _safe_stripped_text(session_id),
            "run_id": _safe_stripped_text(run_id),
            "user_id": _safe_stripped_text(user_id),
            "tenant_id": _safe_stripped_text(tenant_id) or budget.tenant_id,
        },
        "budget": budget.to_dict(),
        "provider_controls": merge_metadata({
            "throttle": "RedisThrottleCoalesce",
            "coalescing": "RedisThrottleCoalesce",
        }, provider_controls or {}),
        "source_degradations": list(policy.get("degraded_sources") or []),
        "search_log": list(search_log or []),
        "failure_records": [dict(record) for record in list(failure_records or []) if isinstance(record, dict)],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_metadata(*parts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for part in parts:
        if isinstance(part, dict):
            merged.update(part)
    return merged


def non_empty_values(values: Iterable[str | None]) -> List[str]:
    return [str(value).strip() for value in values if str(value or "").strip()]


def dedupe_texts(values: Sequence[str | None]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values or ():
        value = str(raw or "").strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(value)
    return ordered


def default_literature_sources_for_lane(lane_class: str | None = None) -> List[str]:
    normalized_lane = str(lane_class or "general").strip().lower() or "general"
    configured = _DEFAULT_LITERATURE_SOURCES_BY_LANE.get(normalized_lane)
    if configured is None:
        configured = _DEFAULT_LITERATURE_SOURCES_BY_LANE["general"]
    return list(configured)


def normalize_literature_sources(
    sources: Sequence[str] | None,
    *,
    lane_class: str | None = None,
    openalex_available: bool | None = None,
) -> Dict[str, Any]:
    requested = dedupe_texts(sources or default_literature_sources_for_lane(lane_class))
    cleaned: List[str] = []
    degraded_sources: List[Dict[str, str]] = []
    openalex_enabled = True if openalex_available is None else bool(openalex_available)
    for raw in requested:
        value = str(raw or "").strip().lower()
        if value not in _ALLOWED_LITERATURE_SOURCES:
            degraded_sources.append(
                {
                    "source": value,
                    "reason": "UNSUPPORTED_SOURCE",
                    "detail": "Source is not part of the canonical literature control plane.",
                }
            )
            continue
        if value == "openalex" and not openalex_enabled:
            degraded_sources.append(
                {
                    "source": "openalex",
                    "reason": "SOURCE_UNAVAILABLE",
                    "detail": "OpenAlex is disabled for this runtime lane by policy, not by client capability.",
                }
            )
            continue
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        fallback_defaults = [
            source
            for source in default_literature_sources_for_lane(lane_class)
            if source != "openalex" or openalex_enabled
        ]
        cleaned = dedupe_texts(fallback_defaults) or ["semantic_scholar"]
    return {
        "requested_sources": requested,
        "effective_sources": cleaned,
        "degraded_sources": degraded_sources,
    }


def resolve_literature_operation_plan(
    *,
    query: str,
    max_papers: int,
    sources: Sequence[str] | None = None,
    extra_queries: Sequence[str] | None = None,
    lane_class: str = "general",
    preset_name: str = "",
    negative_memory_context: Optional[Dict[str, Any]] = None,
    openalex_available: bool | None = None,
) -> Dict[str, Any]:
    from mica.literature_consolidation.contracts.query_protocol import LiteratureQuerySpec
    from mica.literature_consolidation.provider_compiler import LiteratureProviderCompiler

    context = negative_memory_context or {}
    summary = dict(context.get("negative_memory_summary") or {})
    mode = str(
        context.get("negative_memory_mode")
        or summary.get("negative_memory_mode")
        or "full"
    ).strip() or "full"
    explicit_extra_queries = dedupe_texts(extra_queries or ())
    appeal_state = dict(context.get("appeal_regime_state") or {})
    soft_repulsion_warnings = [
        warning for warning in list(context.get("soft_repulsion_warnings") or [])
        if isinstance(warning, dict)
    ]
    appeal_candidates: List[str] = []
    for value in list(appeal_state.get("appeal_candidates") or []):
        text = str(value or "").strip()
        if text:
            appeal_candidates.append(text)
    for warning in soft_repulsion_warnings:
        text = str(warning.get("target_id") or "").strip()
        if text:
            appeal_candidates.append(text)
    appeal_candidates = dedupe_texts(appeal_candidates)

    effective_max_papers = max(1, int(max_papers or 1))
    effective_extra_queries = list(explicit_extra_queries)
    applied_policy = [f"negative_memory_mode:{mode}"]

    if mode == "full":
        if appeal_state.get("appeal_regime_active") and appeal_candidates:
            effective_extra_queries = dedupe_texts(list(explicit_extra_queries) + appeal_candidates[:3])
            applied_policy.append("appeal_candidate_expansion")
    elif mode == "semi_blind":
        applied_policy.extend(["no_negative_memory_query_expansion", "reduced_retrieval_budget"])
    else:
        effective_extra_queries = []
        applied_policy.extend(["indexed_sources_only", "query_expansion_disabled", "reduced_retrieval_budget"])
        effective_max_papers = min(effective_max_papers, 100)

    requested_sources = dedupe_texts(sources or default_literature_sources_for_lane(lane_class))

    # Blind mode: constrain to indexed-only sources (semantic_scholar + pubmed).
    if mode not in {"full", "semi_blind"}:
        _indexed = [s for s in requested_sources if s in {"semantic_scholar", "pubmed"}]
        requested_sources = tuple(_indexed) if _indexed else ("semantic_scholar", "pubmed")
        if "pubmed" not in requested_sources:
            requested_sources = tuple(list(requested_sources) + ["pubmed"])
    try:
        spec_lane = str(lane_class or "general").strip().lower() or "general"
        allowed_spec_lanes = {"ingest", "deep_research", "bibliotecario", "driver_search", "general"}
        if spec_lane not in allowed_spec_lanes:
            spec_lane = "general"

        query_spec = LiteratureQuerySpec(
            query=str(query or "").strip(),
            entities=list(effective_extra_queries),
            max_papers=effective_max_papers,
            sources=list(requested_sources),
            lane=spec_lane,
        )
        compiler = LiteratureProviderCompiler(
            lane_class=str(lane_class or "general").strip() or "general",
            preset_name=str(preset_name or "").strip(),
            negative_memory_context=context,
            openalex_available=True if openalex_available is None else bool(openalex_available),
        )
        compiled_plan = compiler.compile_plan(query_spec)
        plan_dict = compiled_plan.to_dict()
    except ValueError:
        # Backward-compatible fallback for invalid requested sources.
        source_plan = normalize_literature_sources(
            requested_sources,
            lane_class=lane_class,
            openalex_available=openalex_available,
        )
        plan_dict = {
            "query": str(query or "").strip(),
            "extra_queries": list(effective_extra_queries),
            "sources": list(source_plan["effective_sources"]),
            "requested_sources": list(source_plan["requested_sources"]),
            "degraded_sources": list(source_plan["degraded_sources"]),
            "max_papers": effective_max_papers,
            "acquisition_order": [
                "pmc_jats",
                "europe_pmc",
                "oa_url",
                "unpaywall",
                "openalex_metadata_or_pdf",
                "semantic_scholar_fulltext_or_abstract",
                "publisher_html",
                "pdf",
                "ocr",
                "abstract_only",
            ],
            "lane_class": str(lane_class or "general").strip() or "general",
            "preset_name": str(preset_name or "").strip(),
            "policy": {
                "negative_memory_mode": mode,
                "appeal_regime_active": bool(appeal_state.get("appeal_regime_active")),
            },
        }

    queries = dedupe_texts([query, *effective_extra_queries])
    return {
        "query": str(query or "").strip(),
        "queries": queries,
        "max_papers": int(plan_dict.get("max_papers") or effective_max_papers),
        "sources": list(plan_dict.get("sources") or []),
        "requested_sources": list(plan_dict.get("requested_sources") or []),
        "degraded_sources": list(plan_dict.get("degraded_sources") or []),
        "extra_queries": list(plan_dict.get("extra_queries") or effective_extra_queries),
        "lane_class": str(plan_dict.get("lane_class") or lane_class or "general").strip() or "general",
        "preset_name": str(plan_dict.get("preset_name") or preset_name or "").strip(),
        "acquisition_order": list(plan_dict.get("acquisition_order") or []),
        "policy": {
            "negative_memory_mode": mode,
            "appeal_regime_active": bool(appeal_state.get("appeal_regime_active")),
            "soft_repulsion_warning_count": len(soft_repulsion_warnings),
            "applied_policy": applied_policy,
            "lane_class": str(plan_dict.get("lane_class") or lane_class or "general").strip() or "general",
            "preset_name": str(plan_dict.get("preset_name") or preset_name or "").strip(),
            "requested_sources": list(plan_dict.get("requested_sources") or []),
            "effective_sources": list(plan_dict.get("sources") or []),
            "degraded_sources": list(plan_dict.get("degraded_sources") or []),
        },
    }
