from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000.0, 3)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiteratureArtifactDocument(BaseModel):
    status: str = "not_generated"
    title: str = ""
    summary: str = ""
    markdown: str = ""
    claim_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LiteratureArtifactBundle(BaseModel):
    schema_version: str = "mica.literature_artifact_bundle.v0"
    bundle_type: str = "bibliotecario_deep_synthesis"
    created_at: str = Field(default_factory=_utcnow_iso)
    query: str = ""
    preset: str = ""
    user_id: str = ""
    session_id: str = ""
    backend: str = ""
    requested_sources: List[str] = Field(default_factory=list)
    attempted_sources: List[str] = Field(default_factory=list)
    failed_sources: List[str] = Field(default_factory=list)
    source_counts: Dict[str, int] = Field(default_factory=dict)
    provider_health: Dict[str, Any] = Field(default_factory=dict)
    retrieval_policy: Dict[str, Any] = Field(default_factory=dict)
    acquisition_envelope: Dict[str, Any] = Field(default_factory=dict)
    canonical_paper_set: List[Dict[str, Any]] = Field(default_factory=list)
    paper_count: int = 0
    frontier_claim_packet: LiteratureArtifactDocument = Field(default_factory=LiteratureArtifactDocument)
    knowledge_overview: LiteratureArtifactDocument = Field(default_factory=LiteratureArtifactDocument)
    vertical_report: LiteratureArtifactDocument = Field(default_factory=LiteratureArtifactDocument)
    figure_manifest: Dict[str, Any] = Field(default_factory=dict)
    unified_scientific_packet: Dict[str, Any] = Field(default_factory=dict)
    generation_profile: Dict[str, Any] = Field(default_factory=dict)
    generation_notes: List[str] = Field(default_factory=list)


@dataclass
class OverviewPaperRecord:
    paper_id: str = ""
    title: str = ""
    abstract: str = ""
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    journal: Optional[str] = None
    url: Optional[str] = None
    arxiv_id: Optional[str] = None
    pdf_url: Optional[str] = None

    def format_apa(self) -> str:
        parts: List[str] = []
        if self.authors:
            if len(self.authors) <= 20:
                author_str = ", ".join(self.authors)
            else:
                author_str = ", ".join(self.authors[:19]) + ", ... " + self.authors[-1]
            parts.append(author_str)
        else:
            parts.append("[Unknown authors]")
        parts.append(f"({self.year})" if self.year else "(n.d.)")
        parts.append(f"{self.title}." if self.title else "[Untitled].")
        if self.journal:
            parts.append(f"{self.journal}.")
        if self.doi:
            doi_clean = self.doi.strip()
            if not doi_clean.startswith("http"):
                doi_clean = f"https://doi.org/{doi_clean}"
            parts.append(doi_clean)
        elif self.pmid:
            parts.append(f"PMID: {self.pmid}")
        return " ".join(parts)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _provider_role(provider: Any) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(provider or "").strip().lower()).strip("_")
    if normalized.startswith("firecrawl") or normalized in {"web_search", "web_context"}:
        return "web_context_supplement"
    return "canonical_literature_provider"


def _provider_receipts_for_paper(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = dict(paper.get("metadata") or {})
    receipts = paper.get("provider_fetch_receipts")
    if receipts in (None, ""):
        receipts = metadata.get("provider_fetch_receipts")
    return [dict(receipt) for receipt in list(receipts or []) if isinstance(receipt, dict)]


def _provider_failures_for_paper(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = dict(paper.get("metadata") or {})
    failures = paper.get("provider_failures")
    if failures in (None, ""):
        failures = metadata.get("provider_failures")
    return [dict(failure) for failure in list(failures or []) if isinstance(failure, dict)]


def _content_checksum_for_paper(paper: Dict[str, Any], receipts: Optional[List[Dict[str, Any]]] = None) -> str:
    metadata = dict(paper.get("metadata") or {})
    fulltext_router = dict(metadata.get("fulltext_router") or {})
    candidates = [
        paper.get("content_checksum"),
        metadata.get("content_checksum"),
        fulltext_router.get("checksum"),
    ]
    for receipt in list(receipts or _provider_receipts_for_paper(paper)):
        if receipt.get("acquisition_status") == "success":
            candidates.append(receipt.get("content_checksum"))
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _lineage_fallback_receipt(paper: Dict[str, Any], *, content_checksum: str) -> Dict[str, Any]:
    metadata = dict(paper.get("metadata") or {})
    provenance = dict(metadata.get("provenance") or {})
    provider = str(paper.get("provider") or paper.get("source") or provenance.get("provider") or paper.get("backend") or "").strip()
    source_url = str(
        paper.get("source_url")
        or provenance.get("source_url")
        or metadata.get("source_url")
        or (paper.get("openAccessPdf") or {}).get("url")
        or paper.get("pdf_url")
        or ""
    ).strip()
    fetch_timestamp = str(
        paper.get("fetch_timestamp")
        or provenance.get("fetch_timestamp")
        or metadata.get("fetch_timestamp")
        or ""
    ).strip()
    content_type = str(paper.get("content_type") or provenance.get("content_type") or "").strip()
    is_success = bool(provider and source_url)
    acquisition_status = "success" if is_success else "unknown"
    return {
        "provider": provider,
        "provider_role": _provider_role(provider),
        "provider_id": str(
            paper.get("provider_id")
            or provenance.get("provider_id")
            or metadata.get("provider_id")
            or paper.get("paperId")
            or paper.get("canonical_id")
            or ""
        ).strip(),
        "source_url": source_url,
        "content_url": source_url,
        "fetch_timestamp": fetch_timestamp,
        "acquisition_source": provider,
        "acquisition_method": str(metadata.get("acquisition_method") or "record_projection").strip(),
        "acquisition_status": acquisition_status,
        "http_status": _safe_int(metadata.get("http_status")),
        "content_checksum": content_checksum if content_type == "full_text" else "",
    }


def _evaluate_paper_lineage(
    paper: Dict[str, Any],
    *,
    receipts: Optional[List[Dict[str, Any]]] = None,
    content_checksum: str = "",
) -> Dict[str, Any]:
    content_type = str(paper.get("content_type") or dict(paper.get("metadata") or {}).get("content_type") or "").strip()
    text_materialized = content_type == "full_text" or bool(content_checksum)
    candidate_receipts = [dict(receipt) for receipt in list(receipts or _provider_receipts_for_paper(paper)) if isinstance(receipt, dict)]
    if not candidate_receipts:
        candidate_receipts = [_lineage_fallback_receipt(paper, content_checksum=content_checksum)]
    complete_receipt_count = 0
    successful_receipt_count = 0
    missing_fields: List[str] = []
    for receipt in candidate_receipts:
        if receipt.get("acquisition_status") == "success":
            successful_receipt_count += 1
        required_fields = [
            "provider",
            "provider_id",
            "source_url",
            "fetch_timestamp",
            "acquisition_method",
            "acquisition_status",
        ]
        if receipt.get("acquisition_status") == "success":
            required_fields.append("http_status")
            if text_materialized:
                required_fields.append("content_checksum")
        missing = [field_name for field_name in required_fields if receipt.get(field_name) in (None, "")]
        if not missing and receipt.get("provider_role") == "canonical_literature_provider":
            complete_receipt_count += 1
        for field_name in missing:
            missing_fields.append(f"{receipt.get('provider') or 'unknown'}:{field_name}")
    status = "complete" if complete_receipt_count > 0 else "incomplete" if candidate_receipts else "absent"
    return {
        "status": status,
        "receipt_count": len(candidate_receipts),
        "successful_receipt_count": successful_receipt_count,
        "complete_receipt_count": complete_receipt_count,
        "missing_fields": sorted(set(missing_fields)),
        "provider_roles": sorted({str(receipt.get("provider_role") or "") for receipt in candidate_receipts if receipt.get("provider_role")}),
        "text_materialized": text_materialized,
    }


def _paper_identity_tokens(paper: Dict[str, Any]) -> List[str]:
    return [
        token
        for token in {
            str(paper.get("paper_id") or ""),
            str(paper.get("canonical_id") or ""),
            str(paper.get("provider_id") or ""),
            str(paper.get("doi") or ""),
            str(paper.get("pmid") or ""),
            str(paper.get("pmcid") or ""),
        }
        if token
    ]


def _build_fulltext_verification(bundle: Dict[str, Any], frontier_claims: List[Dict[str, Any]]) -> Dict[str, Any]:
    canonical_papers = [dict(paper) for paper in list(bundle.get("canonical_paper_set") or []) if isinstance(paper, dict)]
    paper_index: Dict[str, Dict[str, Any]] = {}
    for paper in canonical_papers:
        for token in _paper_identity_tokens(paper):
            paper_index[token] = paper

    required_papers: List[Dict[str, Any]] = []
    required_seen: set[int] = set()
    for claim in frontier_claims:
        for paper_id in list(claim.get("supporting_papers") or []):
            paper = paper_index.get(str(paper_id))
            if paper is None:
                continue
            marker = id(paper)
            if marker in required_seen:
                continue
            required_seen.add(marker)
            required_papers.append(paper)
    if not required_papers:
        required_papers = canonical_papers

    verified_entries: List[Dict[str, Any]] = []
    blocking_entries: List[Dict[str, Any]] = []
    for paper in required_papers:
        receipts = _provider_receipts_for_paper(paper)
        checksum = _content_checksum_for_paper(paper, receipts=receipts)
        lineage = dict(paper.get("lineage") or _evaluate_paper_lineage(paper, receipts=receipts, content_checksum=checksum))
        provider_role = str(paper.get("provider_role") or _provider_role(paper.get("provider") or paper.get("source") or "")).strip()
        content_type = str(paper.get("content_type") or "").strip() or str(dict(paper.get("provenance") or {}).get("content_type") or "")
        acquisition_kind = str(paper.get("acquisition_kind") or "").strip().lower()
        reasons: List[str] = []
        if provider_role != "canonical_literature_provider":
            reasons.append("web_context_supplement_non_canonical")
        if acquisition_kind in {"sections", "section_only"} and content_type != "full_text":
            reasons.append("section_only_unverified")
        elif content_type != "full_text":
            reasons.append("abstract_only" if content_type == "abstract" else "metadata_only")
        if lineage.get("status") != "complete":
            reasons.append("provider_lineage_incomplete")
        entry = {
            "paper_id": str(paper.get("paper_id") or paper.get("canonical_id") or ""),
            "canonical_id": str(paper.get("canonical_id") or ""),
            "title": str(paper.get("title") or ""),
            "provider": str(paper.get("provider") or paper.get("source") or ""),
            "provider_role": provider_role,
            "content_type": content_type,
            "acquisition_kind": acquisition_kind or None,
            "content_checksum": checksum,
            "lineage_status": str(lineage.get("status") or "unknown"),
            "verified": not reasons,
            "blocking_reasons": reasons,
        }
        if reasons:
            blocking_entries.append(entry)
        else:
            verified_entries.append(entry)

    claim_blockers: List[Dict[str, Any]] = []
    for claim in frontier_claims:
        supporting_papers = [paper_index.get(str(paper_id)) for paper_id in list(claim.get("supporting_papers") or [])]
        failed_support = []
        for paper in supporting_papers:
            if not isinstance(paper, dict):
                continue
            paper_id = str(paper.get("paper_id") or paper.get("canonical_id") or "")
            failed = next((entry for entry in blocking_entries if entry.get("paper_id") == paper_id), None)
            if failed is not None:
                failed_support.append(
                    {
                        "paper_id": paper_id,
                        "blocking_reasons": list(failed.get("blocking_reasons") or []),
                    }
                )
        if failed_support:
            claim_blockers.append(
                {
                    "claim_text": str(claim.get("claim_text") or "")[:200],
                    "supporting_papers": failed_support,
                }
            )

    required_count = len(required_papers)
    verified_count = len(verified_entries)
    coverage_ratio = verified_count / max(1, required_count) if required_count else 0.0
    return {
        "status": "pass" if required_count > 0 and not blocking_entries else "fail",
        "required_paper_count": required_count,
        "verified_paper_count": verified_count,
        "coverage_ratio": round(coverage_ratio, 3),
        "verified_papers": verified_entries,
        "blocking_papers": blocking_entries,
        "claim_blockers": claim_blockers,
    }


def canonicalize_paper_record(paper: Dict[str, Any]) -> Dict[str, Any]:
    external_ids = dict(paper.get("externalIds") or {})
    metadata = dict(paper.get("metadata") or {})
    provenance = dict(metadata.get("provenance") or {})
    doi = str(paper.get("doi") or external_ids.get("DOI") or "")
    pmid = str(paper.get("pmid") or external_ids.get("PubMed") or "")
    pmcid = str(paper.get("pmcid") or external_ids.get("PubMedCentral") or "")
    provider = str(paper.get("provider") or paper.get("source") or paper.get("backend") or "")
    provider_id = str(paper.get("provider_id") or paper.get("paperId") or paper.get("id") or paper.get("canonical_id") or "")
    source_url = str(
        paper.get("source_url")
        or (paper.get("openAccessPdf") or {}).get("url")
        or paper.get("pdf_url")
        or ""
    )
    content_type = str(paper.get("content_type") or ("full_text" if paper.get("full_text") else "abstract" if paper.get("abstract") else "metadata"))
    provider_fetch_receipts = _provider_receipts_for_paper(paper)
    content_checksum = _content_checksum_for_paper(paper, receipts=provider_fetch_receipts)
    lineage = _evaluate_paper_lineage(paper, receipts=provider_fetch_receipts, content_checksum=content_checksum)
    provider_role = _provider_role(provider)
    return {
        "paper_id": str(paper.get("paperId") or paper.get("canonical_id") or provider_id),
        "canonical_id": str(paper.get("canonical_id") or doi or pmid or provider_id),
        "title": str(paper.get("title") or ""),
        "abstract": str(paper.get("abstract") or ""),
        "year": paper.get("year"),
        "citation_count": int(paper.get("citationCount") or paper.get("citation_count") or 0),
        "reference_count": int(paper.get("referenceCount") or paper.get("reference_count") or 0),
        "provider": provider,
        "provider_id": provider_id,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "source_url": source_url,
        "fetch_timestamp": str(paper.get("fetch_timestamp") or provenance.get("fetch_timestamp") or metadata.get("fetch_timestamp") or ""),
        "content_type": content_type,
        "license_type": str(paper.get("license_type") or "unknown"),
        "degradation_reason": str(paper.get("degradation_reason") or metadata.get("degradation_reason") or ""),
        "acquisition_kind": str(paper.get("acquisition_kind") or metadata.get("acquisition_kind") or ""),
        "content_checksum": content_checksum,
        "provider_role": provider_role,
        "provider_fetch_receipts": provider_fetch_receipts,
        "provider_failures": _provider_failures_for_paper(paper),
        "acquisition_audit": [dict(item) for item in list(paper.get("acquisition_audit") or []) if isinstance(item, dict)],
        "provenance": provenance,
        "lineage": lineage,
        "content_uri": str(paper.get("content_uri") or metadata.get("content_uri") or ""),
        "normalized_text_uri": str(paper.get("normalized_text_uri") or metadata.get("normalized_text_uri") or ""),
        "section_json_uri": str(paper.get("section_json_uri") or metadata.get("section_json_uri") or ""),
        "citations_json_uri": str(paper.get("citations_json_uri") or metadata.get("citations_json_uri") or ""),
        "evidence_manifest_uri": str(metadata.get("evidence_manifest_uri") or ""),
        "evidence_backend": str(metadata.get("evidence_backend") or ""),
    }


def build_sota_sections_from_papers(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    for paper in list(papers or [])[:50]:
        title = str(paper.get("title") or "").strip()
        best_text, _ = _resolve_best_available_text(paper)
        text = f"{title}. {best_text}".strip(". ")
        if len(text) < 60:
            continue
        sections.append(
            {
                "heading": title[:160] or str(paper.get("canonical_id") or paper.get("paperId") or "paper"),
                "text": text,
                "source_paper_id": str(
                    paper.get("doi")
                    or paper.get("canonical_id")
                    or paper.get("paperId")
                    or paper.get("id")
                    or ""
                ),
            }
        )
    return sections


def _section_text_from_metadata(metadata: Dict[str, Any]) -> str:
    sections = list(metadata.get("sections") or [])
    parts: List[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        text = str(
            section.get("text")
            or section.get("content")
            or section.get("body")
            or section.get("full_text")
            or ""
        ).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _resolve_best_available_text(paper: Dict[str, Any]) -> tuple[str, str]:
    full_text = str(paper.get("full_text") or "").strip()
    if full_text:
        return full_text, str(paper.get("content_type") or "full_text")

    metadata = dict(paper.get("metadata") or {})
    section_text = _section_text_from_metadata(metadata)
    if section_text:
        return section_text, str(metadata.get("acquisition_kind") or "sections")

    abstract = str(paper.get("abstract") or "").strip()
    if abstract:
        return abstract, "abstract_only"
    return "", "missing_text"


def _topic_tokens(topic: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}", (topic or "").lower())
        if len(token) > 3
    ]


def _compute_domain_relevance(topic: str, claims: List[Dict[str, Any]]) -> Dict[str, Any]:
    tokens = set(_topic_tokens(topic))
    if not claims:
        return {
            "status": "warn",
            "matched_claim_ratio": 0.0,
            "off_domain_claim_ratio": 1.0,
            "top_off_domain_signals": ["no_claims"],
        }

    matched = 0
    off_domain_snippets: List[str] = []
    for claim in claims:
        text = str(claim.get("claim_text") or "").lower()
        if any(token in text for token in tokens):
            matched += 1
        else:
            snippet = str(claim.get("claim_text") or "")[:80].strip()
            if snippet:
                off_domain_snippets.append(snippet)

    matched_ratio = matched / max(1, len(claims))
    off_ratio = 1.0 - matched_ratio
    if matched_ratio >= 0.65:
        status = "pass"
    elif matched_ratio >= 0.4:
        status = "warn"
    else:
        status = "fail"

    return {
        "status": status,
        "matched_claim_ratio": round(matched_ratio, 3),
        "off_domain_claim_ratio": round(off_ratio, 3),
        "top_off_domain_signals": off_domain_snippets[:5],
    }


def build_unified_scientific_report_packet(bundle: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(bundle or {})
    incoming_lineage = dict(payload.get("protocol_lineage") or {})
    protocol_lineage = {
        "protocol_id": str(incoming_lineage.get("protocol_id") or payload.get("protocol_id") or ""),
        "protocol_version": str(incoming_lineage.get("protocol_version") or payload.get("protocol_version") or ""),
        "run_id": str(incoming_lineage.get("run_id") or payload.get("run_id") or ""),
        "session_id": str(incoming_lineage.get("session_id") or payload.get("session_id") or ""),
        "node_receipts": [
            dict(item)
            for item in list(incoming_lineage.get("node_receipts") or payload.get("node_receipts") or [])
            if isinstance(item, dict)
        ],
    }
    if not any(
        [
            protocol_lineage["protocol_id"],
            protocol_lineage["protocol_version"],
            protocol_lineage["run_id"],
            protocol_lineage["session_id"],
            protocol_lineage["node_receipts"],
        ]
    ):
        protocol_lineage = {}

    frontier_claims = list(
        dict(payload.get("frontier_claim_packet") or {}).get("metadata", {}).get("claims", []) or []
    )
    domain_ctrl = _compute_domain_relevance(str(payload.get("query") or ""), frontier_claims)
    evidence_coverage = 0.0
    if frontier_claims:
        cited = sum(1 for claim in frontier_claims if list(claim.get("supporting_papers") or []))
        evidence_coverage = cited / max(1, len(frontier_claims))
    evidence_gate = {
        "status": "pass" if evidence_coverage >= 0.6 else "fail",
        "coverage_ratio": round(evidence_coverage, 3),
        "uncited_claim_count": max(0, len(frontier_claims) - int(round(evidence_coverage * len(frontier_claims)))),
    }
    fulltext_verification = _build_fulltext_verification(payload, frontier_claims)
    allow_publication = (
        domain_ctrl["status"] == "pass"
        and evidence_gate["status"] == "pass"
        and fulltext_verification["status"] == "pass"
    )
    blocking_reasons: List[str] = []
    if domain_ctrl["status"] != "pass":
        blocking_reasons.append("domain_gate_failed")
    if evidence_gate["status"] != "pass":
        blocking_reasons.append("evidence_gate_failed")
    if fulltext_verification["status"] != "pass":
        blocking_reasons.append("fulltext_verification_failed")
    publication_gate = {
        "allow_publication": allow_publication,
        "publication_ready": allow_publication,
        "promotion_state": "promoted" if allow_publication else "blocked",
        "consolidation_state": "consolidated" if allow_publication else "blocked",
        "rationale": "domain_evidence_and_fulltext_pass" if allow_publication else "blocked_by_scientific_controls",
        "blocking_reasons": blocking_reasons,
    }

    return {
        "schema_version": "mica.scientific_report_packet.v1",
        "packet_id": f"srp-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "created_at": str(payload.get("created_at") or _utcnow_iso()),
        "program": "POC_SUPERNOVA",
        "session_id": str(payload.get("session_id") or ""),
        "protocol_lineage": protocol_lineage,
        "seed": {
            "doi": "10.1038/s42003-025-08551-5",
            "seed_title": str(payload.get("query") or ""),
            "seed_query": str(payload.get("query") or ""),
            "domain_tags": _topic_tokens(str(payload.get("query") or ""))[:12],
        },
        "acquisition": {
            "mode": str(dict(payload.get("acquisition_envelope") or {}).get("mode") or "unknown"),
            "requested_sources": list(payload.get("requested_sources") or []),
            "attempted_sources": list(payload.get("attempted_sources") or []),
            "failed_sources": list(payload.get("failed_sources") or []),
            "provider_health": dict(payload.get("provider_health") or {}),
            "paper_count": int(payload.get("paper_count") or 0),
        },
        "reports": {
            "frontier_claim_packet": dict(payload.get("frontier_claim_packet") or {}),
            "knowledge_overview": dict(payload.get("knowledge_overview") or {}),
            "vertical_report": dict(payload.get("vertical_report") or {}),
        },
        "artifacts": {
            "manifest_schema": "mica.literature_artifact_manifest.v1",
            "storage_backend": str(payload.get("backend") or ""),
        },
        "scientific_controls": {
            "seed_domain_enforcement": domain_ctrl,
            "evidence_gate": evidence_gate,
            "fulltext_verification": fulltext_verification,
            "publication_gate": publication_gate,
        },
    }


def _build_query_aligned_chapter_specs(query: str) -> List[Any]:
    from mica.sota_reports.knowledge_overview_pipeline import ChapterSpec

    topic = str(query or "").strip()
    tokens = [t for t in _topic_tokens(topic) if t]
    if not tokens:
        tokens = ["evidence", "synthesis", "mechanism", "context"]

    max_chapters = max(4, min(8, len(tokens) + 1))
    chapters: List[ChapterSpec] = []
    for idx in range(1, max_chapters + 1):
        a = tokens[(idx - 1) % len(tokens)]
        b = tokens[idx % len(tokens)] if len(tokens) > 1 else tokens[0]
        focus = " ".join(dict.fromkeys([a, b]))
        key = f"chapter_{idx:02d}_{a}"
        title_focus = " ".join(p.capitalize() for p in focus.split())
        chapters.append(
            ChapterSpec(
                key=key,
                title=f"Chapter {idx} — {title_focus}",
                query=f"{topic} {focus} evidence and contradictions".strip(),
                task=(
                    f"Produce a rigorous synthesis for '{topic}' focused on {focus}. "
                    "Use only evidence-linked claims, report contradictions with reasons, and separate facts from hypotheses."
                ),
                keywords=[a, b] + tokens[:4],
                order=idx,
            )
        )
    return chapters


def build_sota_paper_records(papers: List[Dict[str, Any]]) -> List[Any]:
    records: List[Any] = []
    for paper in list(papers or []):
        external_ids = dict(paper.get("externalIds") or {})
        records.append(
            SimpleNamespace(
                paper_id=str(paper.get("paperId") or paper.get("canonical_id") or ""),
                doi=str(paper.get("doi") or external_ids.get("DOI") or ""),
                title=str(paper.get("title") or ""),
                year=paper.get("year"),
                citation_count=int(paper.get("citationCount") or paper.get("citation_count") or 0),
            )
        )
    return records


def build_overview_paper_records(papers: List[Dict[str, Any]]) -> List[OverviewPaperRecord]:
    records: List[OverviewPaperRecord] = []
    for paper in list(papers or []):
        canonical = canonicalize_paper_record(paper)
        authors_raw = paper.get("authors") or canonical.get("authors") or []
        authors: List[str] = []
        for author in authors_raw:
            if isinstance(author, dict):
                name = str(author.get("name") or "").strip()
                if name:
                    authors.append(name)
            else:
                name = str(author or "").strip()
                if name:
                    authors.append(name)
        records.append(
            OverviewPaperRecord(
                paper_id=str(canonical.get("paper_id") or canonical.get("canonical_id") or ""),
                title=str(canonical.get("title") or ""),
                abstract=str(canonical.get("abstract") or ""),
                authors=authors,
                year=canonical.get("year"),
                doi=str(canonical.get("doi") or "") or None,
                pmid=str(canonical.get("pmid") or "") or None,
                pmcid=str(canonical.get("pmcid") or "") or None,
                journal=str(paper.get("journal") or paper.get("venue") or "") or None,
                url=str(canonical.get("source_url") or "") or None,
                arxiv_id=str(paper.get("arxivId") or paper.get("arxiv_id") or "") or None,
                pdf_url=str((paper.get("openAccessPdf") or {}).get("url") or paper.get("pdf_url") or "") or None,
            )
        )
    return records


def build_frontier_claim_packet(query: str, papers: List[Dict[str, Any]]) -> Dict[str, Any]:
    sections = build_sota_sections_from_papers(papers)
    if not sections:
        return {
            "status": "degraded_no_sections",
            "title": f"{query}: frontier claim packet",
            "summary": "No sufficiently rich literature sections were available for claim extraction.",
            "markdown": "",
            "claim_count": 0,
            "metadata": {"topic": query, "claims": []},
        }

    from mica.sota_reports.sota_pipeline import SOTAPipeline

    pipeline = SOTAPipeline()
    paper_records = build_sota_paper_records(papers)
    result = pipeline.run(
        sections,
        topic=query,
        title=f"{query}: Frontier Claim Packet",
        output_format="markdown",
        paper_records=paper_records,
    )
    return {
        "status": "generated" if result.claim_count else "degraded_no_claims",
        "title": f"{query}: frontier claim packet",
        "summary": result.summary,
        "markdown": result.markdown,
        "claim_count": int(result.claim_count or 0),
        "metadata": {
            "topic": query,
            "claims": [claim.to_dict() for claim in list(result.claims or [])],
            "landscape": result.landscape.to_dict() if result.landscape else {},
        },
    }


def build_vertical_report_sections_from_bundle(
    bundle: Dict[str, Any],
    *,
    fallback_synthesis: str = "",
) -> List[Dict[str, str]]:
    sections: List[Dict[str, str]] = []
    knowledge_overview = dict(bundle.get("knowledge_overview") or {})
    frontier_claim_packet = dict(bundle.get("frontier_claim_packet") or {})
    frontier_claims = list((frontier_claim_packet.get("metadata") or {}).get("claims") or [])
    knowledge_markdown = str(knowledge_overview.get("markdown") or "").strip()
    frontier_markdown = str(frontier_claim_packet.get("markdown") or "").strip()
    synthesis_text = str(fallback_synthesis or "").strip()

    if knowledge_markdown:
        sections.append({"heading": "Knowledge Overview", "text": knowledge_markdown})
    if frontier_claims:
        # Prefer structured claims so we do not reparse already-rendered markdown headers/metadata.
        for idx, claim in enumerate(frontier_claims[:40], start=1):
            claim_text = str((claim or {}).get("claim_text") or "").strip()
            if not claim_text:
                continue
            claim_type = str((claim or {}).get("claim_type") or "frontier").strip() or "frontier"
            sections.append(
                {
                    "heading": f"Frontier Claim {idx} ({claim_type})",
                    "text": claim_text,
                }
            )
    elif frontier_markdown:
        sections.append({"heading": "Frontier Claim Packet", "text": frontier_markdown})
    if synthesis_text and synthesis_text not in {knowledge_markdown, frontier_markdown}:
        sections.append({"heading": "Bibliotecario Synthesis", "text": synthesis_text})
    return sections


def build_primary_synthesis_from_bundle(
    bundle: Dict[str, Any],
    *,
    fallback_synthesis: str = "",
) -> str:
    vertical_report = dict(bundle.get("vertical_report") or {})
    knowledge_overview = dict(bundle.get("knowledge_overview") or {})
    frontier_claim_packet = dict(bundle.get("frontier_claim_packet") or {})
    parts = [
        str(vertical_report.get("summary") or "").strip(),
        str(knowledge_overview.get("summary") or "").strip(),
        str(frontier_claim_packet.get("summary") or "").strip(),
        str(fallback_synthesis or "").strip(),
    ]
    for part in parts:
        if part:
            return part
    return ""


def build_literature_artifact_manifest(bundle: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(bundle or {})
    figure_manifest = dict(payload.get("figure_manifest") or {})
    canonical_paper_set = list(payload.get("canonical_paper_set") or [])
    acquisition_envelope = dict(payload.get("acquisition_envelope") or {})

    def _document_entry(key: str, kind: str) -> Dict[str, Any]:
        document = dict(payload.get(key) or {})
        markdown = str(document.get("markdown") or "")
        return {
            "key": key,
            "content_kind": kind,
            "status": str(document.get("status") or "not_generated"),
            "title": str(document.get("title") or ""),
            "summary": str(document.get("summary") or ""),
            "logical_path": f"artifact_bundle.{key}",
            "markdown_char_count": len(markdown),
            "claim_count": int(document.get("claim_count") or 0),
        }

    artifacts: List[Dict[str, Any]] = [
        _document_entry("frontier_claim_packet", "frontier_claim_packet"),
        _document_entry("knowledge_overview", "knowledge_overview"),
        _document_entry("vertical_report", "vertical_report"),
        {
            "key": "figure_manifest",
            "content_kind": "figure_manifest",
            "status": str(figure_manifest.get("status") or "not_generated"),
            "logical_path": "artifact_bundle.figure_manifest",
            "figure_count": int(figure_manifest.get("total_figures") or len(list(figure_manifest.get("chapters_with_figures") or []))),
            "origin_counts": dict(figure_manifest.get("origin_counts") or {}),
        },
        {
            "key": "canonical_paper_set",
            "content_kind": "canonical_paper_set",
            "status": "available" if canonical_paper_set else "empty",
            "logical_path": "artifact_bundle.canonical_paper_set",
            "paper_count": int(payload.get("paper_count") or len(canonical_paper_set)),
        },
        {
            "key": "acquisition_envelope",
            "content_kind": "acquisition_envelope",
            "status": "available" if acquisition_envelope else "empty",
            "logical_path": "artifact_bundle.acquisition_envelope",
            "requested_sources": list(payload.get("requested_sources") or []),
            "attempted_sources": list(payload.get("attempted_sources") or []),
            "failed_sources": list(payload.get("failed_sources") or []),
        },
    ]

    primary_artifact_key = ""
    for candidate in ("vertical_report", "knowledge_overview", "frontier_claim_packet"):
        status = str(dict(payload.get(candidate) or {}).get("status") or "")
        if status and status != "not_generated":
            primary_artifact_key = candidate
            break

    return {
        "schema_version": "mica.literature_artifact_manifest.v1",
        "bundle_type": str(payload.get("bundle_type") or "bibliotecario_deep_synthesis"),
        "created_at": str(payload.get("created_at") or _utcnow_iso()),
        "query": str(payload.get("query") or ""),
        "preset": str(payload.get("preset") or ""),
        "user_id": str(payload.get("user_id") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "backend": str(payload.get("backend") or ""),
        "paper_count": int(payload.get("paper_count") or len(canonical_paper_set)),
        "artifact_count": len(artifacts),
        "primary_artifact_key": primary_artifact_key,
        "artifacts": artifacts,
    }


def _build_overview_full_text_map(papers: List[Dict[str, Any]]) -> Dict[str, Any]:
    full_text_map: Dict[str, Any] = {}
    paper_records = {str(paper.get("paperId") or paper.get("canonical_id") or ""): paper for paper in list(papers or [])}
    for record in build_overview_paper_records(papers):
        if not record.paper_id:
            continue
        text, source = _resolve_best_available_text(paper_records.get(record.paper_id) or {})
        if not text:
            continue
        full_text_map[record.paper_id] = SimpleNamespace(
            title=record.title,
            full_text=text,
            source=source,
        )
    return full_text_map


def _extract_shortlisted_ids(extra_context: str) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for paper_id in re.findall(r"\[([^\]]+)\]\s+score=", extra_context or ""):
        cleaned = str(paper_id or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _extract_evidence_lines(extra_context: str, *, limit: int = 4) -> List[str]:
    lines: List[str] = []
    for raw_line in str(extra_context or "").splitlines():
        line = str(raw_line or "").strip()
        if line.startswith("Title: "):
            lines.append(line[7:].strip())
        elif line.startswith("Snippet: "):
            lines.append(line[9:].strip())
        elif line.startswith("── "):
            cleaned = line.strip("─ ")
            if cleaned:
                lines.append(cleaned)
        if len(lines) >= limit:
            break
    return [line for line in lines if line]


def _compose_chapter_synthesis(
    *,
    topic: str,
    chapter_query: str,
    task: str,
    extra_context: str,
    paper_index: Dict[str, OverviewPaperRecord],
) -> str:
    shortlisted_records = [paper_index[paper_id] for paper_id in _extract_shortlisted_ids(extra_context) if paper_id in paper_index]
    if not shortlisted_records:
        shortlisted_records = list(paper_index.values())[:3]
    evidence_titles = [record.title for record in shortlisted_records if record.title][:3]
    evidence_lines = _extract_evidence_lines(extra_context)
    task_focus = re.sub(r"\s+", " ", str(task or "").strip())[:280]
    evidence_bullets = evidence_lines[:3] or evidence_titles[:3] or [
        f"The retained {topic} corpus contains relevant evidence for {chapter_query}."
    ]

    finding_lines = [f"- {bullet}" for bullet in evidence_bullets]
    source_line = ", ".join(evidence_titles) if evidence_titles else "the shortlisted evidence corpus"

    return "\n".join(
        [
            "[BACKGROUND]",
            f"This chapter examines {chapter_query} inside the broader topic of {topic}. The active evidence slice is anchored on {source_line}.",
            "",
            "[KEY FINDINGS]",
            *finding_lines,
            "",
            "[MECHANISTIC INSIGHT]",
            f"Across the retained papers, the chapter-level pattern is that {topic} should be interpreted through the specific lens encoded in the task prompt: {task_focus or chapter_query}.",
            "",
            "[CONTRADICTIONS]",
            "The current overview is generated from a shortlist-biased corpus block, so unresolved tensions should be treated as live epistemic conflicts rather than silently harmonized facts.",
            "",
            "[OPEN GAPS]",
            f"High-confidence closure still requires denser chapter-specific full text coverage, explicit benchmark extraction, and targeted validation of the subquestion '{chapter_query}'.",
        ]
    )


def _build_overview_abstract(
    *,
    topic: str,
    chapter_count: int,
    succeeded_chapters: int,
    frontier_claim_packet: Dict[str, Any],
    timeline_summary: str,
    sota_summary: str,
) -> str:
    summary_parts = [
        f"This knowledge overview synthesizes {topic} across {succeeded_chapters}/{chapter_count} thematic chapters.",
    ]
    if frontier_claim_packet.get("summary"):
        summary_parts.append(str(frontier_claim_packet.get("summary") or "").strip())
    if timeline_summary:
        summary_parts.append(str(timeline_summary).strip())
    if sota_summary:
        summary_parts.append(str(sota_summary).strip())
    return " ".join(part for part in summary_parts if part)


def _build_figure_manifest_from_overview(result: Any) -> Dict[str, Any]:
    figure_records = [
        figure
        for chapter in list(getattr(result, "chapters", []) or [])
        for figure in list(getattr(chapter, "figures", []) or [])
    ]
    generated = sum(1 for figure in figure_records if bool(getattr(figure, "is_generated", False)))
    origin_counts: Dict[str, int] = {}
    quality_counts: Dict[str, int] = {}
    for figure in figure_records:
        origin = str(getattr(figure, "origin", "unknown") or "unknown")
        quality = str(getattr(figure, "quality_tier", "unknown") or "unknown")
        origin_counts[origin] = int(origin_counts.get(origin, 0)) + 1
        quality_counts[quality] = int(quality_counts.get(quality, 0)) + 1
    return {
        "status": "generated" if figure_records else "no_figures_detected",
        "schema_version": "2.0",
        "figure_count": len(figure_records),
        "generated_figure_count": generated,
        "extracted_figure_count": len(figure_records) - generated,
        "origin_counts": origin_counts,
        "quality_tier_counts": quality_counts,
        "chapters_with_figures": [
            {
                "chapter_key": chapter.spec.key,
                "figure_count": len(list(getattr(chapter, "figures", []) or [])),
                "figures": [
                    {
                        "paper_id": getattr(figure, "paper_id", ""),
                        "figure_number": getattr(figure, "figure_number", 0),
                        "origin": getattr(figure, "origin", "unknown"),
                        "quality_tier": getattr(figure, "quality_tier", "unknown"),
                        "roi_strategy": getattr(figure, "roi_strategy", "none"),
                        "fallback_reason": getattr(figure, "fallback_reason", ""),
                    }
                    for figure in list(getattr(chapter, "figures", []) or [])
                ],
            }
            for chapter in list(getattr(result, "chapters", []) or [])
            if list(getattr(chapter, "figures", []) or [])
        ],
    }


async def build_knowledge_overview_document(
    *,
    query: str,
    papers: List[Dict[str, Any]],
    frontier_claim_packet: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    total_started = perf_counter()
    generation_profile: Dict[str, Any] = {
        "stages": {
            "timeline_ms": 0.0,
            "iterative_sota_ms": 0.0,
            "pipeline_run_ms": 0.0,
            "chapter_consult_ms": 0.0,
            "pipeline_overhead_ms": 0.0,
            "writer_markdown_ms": 0.0,
            "writer_docx_ms": 0.0,
            "unattributed_ms": 0.0,
        },
        "chapter_consult_count": 0,
        "chapter_consults": [],
        "total_ms": 0.0,
    }
    sections = build_sota_sections_from_papers(papers)
    paper_records = build_overview_paper_records(papers)
    if not sections or not paper_records:
        generation_profile["total_ms"] = _elapsed_ms(total_started)
        return (
            {
                "status": "degraded_no_sections",
                "title": f"{query}: knowledge overview",
                "summary": "No sufficiently rich literature sections were available to build a knowledge overview.",
                "markdown": "",
                "metadata": {
                    "chapter_count": 0,
                    "succeeded_chapter_count": 0,
                    "failed_chapter_count": 0,
                    "docx_bytes_len": 0,
                    "generation_profile": generation_profile,
                },
            },
            {"status": "not_generated", "figure_count": 0},
        )

    from mica.sota_reports.knowledge_overview_pipeline import KnowledgeOverviewPipeline
    from mica.sota_reports.knowledge_overview_writer import KnowledgeOverviewWriter
    from mica.sota_reports.sota_pipeline_iterative import SOTAPipelineIterative
    from mica.timeline_reports.timeline_pipeline import TimelinePipeline

    full_text_map = _build_overview_full_text_map(papers)
    paper_index = {record.paper_id: record for record in paper_records if record.paper_id}
    chapter_consults: List[Dict[str, Any]] = []
    chapter_consult_total_ms = 0.0

    # ── WI-23: ATOM quintuple wiring into timeline caller ──
    atom_quintuples: list = []
    try:
        from mica.scientific_workflow.memory_authority import MemoryAuthority
        _ma = MemoryAuthority()
        atom_quintuples = await _ma.quintuples_for(query, limit=200)
    except Exception as _atom_exc:
        logger.debug("ATOM quintuples unavailable for timeline: %s", _atom_exc)

    timeline_started = perf_counter()
    timeline_result = TimelinePipeline().run(
        sections,
        entity_scope=query,
        title=f"{query}: timeline overview",
        output_format="both",
        paper_records=paper_records,
        atom_quintuples=atom_quintuples or None,
    )
    generation_profile["stages"]["timeline_ms"] = _elapsed_ms(timeline_started)

    iterative_sota_started = perf_counter()
    _iterative = SOTAPipelineIterative(quality_threshold=0.55, max_iterations=2)
    _iter_result = _iterative.run(
        sections,
        topic=query,
        title=f"{query}: state-of-the-art overview",
        output_format="both",
        paper_records=paper_records,
    )
    generation_profile["stages"]["iterative_sota_ms"] = _elapsed_ms(iterative_sota_started)
    sota_result = _iter_result.pipeline_result

    async def _consult(chapter_query: str, task: str, **kwargs: Any) -> str:
        nonlocal chapter_consult_total_ms
        consult_started = perf_counter()
        try:
            return _compose_chapter_synthesis(
                topic=query,
                chapter_query=chapter_query,
                task=task,
                extra_context=str(kwargs.get("extra_context") or ""),
                paper_index=paper_index,
            )
        finally:
            consult_ms = _elapsed_ms(consult_started)
            chapter_consult_total_ms = round(chapter_consult_total_ms + consult_ms, 3)
            chapter_consults.append(
                {
                    "chapter_query": str(chapter_query or ""),
                    "ms": consult_ms,
                }
            )

    pipeline = KnowledgeOverviewPipeline(
        _consult,
        chapters=_build_query_aligned_chapter_specs(query),
        concurrency=3,
        max_papers_per_chapter=max(1, min(12, len(paper_records))),
    )
    pipeline_started = perf_counter()
    result = await pipeline.run(
        paper_records,
        full_text_map,
        query,
        timeline_result=timeline_result,
        sota_result=sota_result,
        report_title=f"{query}: Knowledge Overview",
    )
    pipeline_run_ms = _elapsed_ms(pipeline_started)
    generation_profile["stages"]["pipeline_run_ms"] = pipeline_run_ms
    generation_profile["stages"]["chapter_consult_ms"] = round(chapter_consult_total_ms, 3)
    generation_profile["stages"]["pipeline_overhead_ms"] = round(
        max(pipeline_run_ms - chapter_consult_total_ms, 0.0),
        3,
    )
    generation_profile["chapter_consult_count"] = len(chapter_consults)
    generation_profile["chapter_consults"] = list(chapter_consults)
    result.abstract_synthesis = _build_overview_abstract(
        topic=query,
        chapter_count=len(list(result.chapters or [])),
        succeeded_chapters=len(list(result.succeeded_chapters or [])),
        frontier_claim_packet=dict(frontier_claim_packet or {}),
        timeline_summary=str(result.timeline_summary or ""),
        sota_summary=str(result.sota_summary or ""),
    )

    writer = KnowledgeOverviewWriter()
    writer_markdown_started = perf_counter()
    markdown = writer.write_md(result, title=f"{query}: Knowledge Overview")
    generation_profile["stages"]["writer_markdown_ms"] = _elapsed_ms(writer_markdown_started)
    writer_docx_started = perf_counter()
    docx_bytes = writer.write_docx_bytes(result, title=f"{query}: Knowledge Overview")
    generation_profile["stages"]["writer_docx_ms"] = _elapsed_ms(writer_docx_started)
    generation_profile["total_ms"] = _elapsed_ms(total_started)
    accounted_ms = round(
        generation_profile["stages"]["timeline_ms"]
        + generation_profile["stages"]["iterative_sota_ms"]
        + generation_profile["stages"]["pipeline_run_ms"]
        + generation_profile["stages"]["writer_markdown_ms"]
        + generation_profile["stages"]["writer_docx_ms"],
        3,
    )
    generation_profile["stages"]["unattributed_ms"] = round(
        max(generation_profile["total_ms"] - accounted_ms, 0.0),
        3,
    )
    figure_manifest = _build_figure_manifest_from_overview(result)
    succeeded_chapters = len(list(result.succeeded_chapters or []))
    failed_chapters = len(list(result.failed_chapters or []))

    return (
        {
            "status": "generated" if succeeded_chapters else "degraded_no_chapters",
            "title": f"{query}: knowledge overview",
            "summary": result.abstract_synthesis,
            "markdown": markdown,
            "metadata": {
                "chapter_count": len(list(result.chapters or [])),
                "succeeded_chapter_count": succeeded_chapters,
                "failed_chapter_count": failed_chapters,
                "timeline_summary": str(result.timeline_summary or ""),
                "sota_summary": str(result.sota_summary or ""),
                "docx_bytes_len": len(docx_bytes),
                "figure_manifest": figure_manifest,
                "generation_profile": generation_profile,
            },
        },
        figure_manifest,
    )


def build_vertical_report_document(
    *,
    query: str,
    papers: List[Dict[str, Any]],
    frontier_claim_packet: Dict[str, Any],
    knowledge_overview: Dict[str, Any],
    synthesis_hint: str = "",
) -> Dict[str, Any]:
    from mica.sota_reports.sota_pipeline import SOTAPipeline

    sections = build_vertical_report_sections_from_bundle(
        {
            "frontier_claim_packet": frontier_claim_packet,
            "knowledge_overview": knowledge_overview,
        },
        fallback_synthesis=synthesis_hint,
    )
    if not sections:
        return {
            "status": "degraded_no_sections",
            "title": f"{query}: vertical report",
            "summary": "No literature artifact sections were available for vertical report generation.",
            "markdown": "",
            "claim_count": 0,
            "metadata": {"input_section_count": 0, "provisional": False},
        }

    paper_records = build_overview_paper_records(papers)
    result = SOTAPipeline().run(
        sections,
        topic=query,
        title=f"{query}: Vertical Report",
        output_format="both",
        paper_records=paper_records,
    )
    return {
        "status": "generated" if result.claim_count else "degraded_no_claims",
        "title": f"{query}: vertical report",
        "summary": result.summary,
        "markdown": result.markdown,
        "claim_count": int(result.claim_count or 0),
        "metadata": {
            "input_section_count": len(sections),
            "docx_bytes_len": len(result.docx_bytes or b""),
            "landscape": result.landscape.to_dict() if result.landscape else {},
            "provisional": False,
        },
    }


async def build_rich_literature_artifact_bundle(
    *,
    query: str,
    preset: str,
    user_id: str,
    session_id: str,
    backend: str,
    papers: List[Dict[str, Any]],
    requested_sources: List[str],
    attempted_sources: List[str],
    failed_sources: List[str],
    source_counts: Dict[str, int],
    provider_health: Dict[str, Any],
    retrieval_policy: Dict[str, Any],
    acquisition_envelope: Dict[str, Any],
    generation_notes: Optional[List[str]] = None,
    synthesis_hint: str = "",
) -> LiteratureArtifactBundle:
    total_started = perf_counter()
    generation_profile: Dict[str, Any] = {
        "stages": {
            "frontier_claim_packet_ms": 0.0,
            "knowledge_overview_ms": 0.0,
            "vertical_report_ms": 0.0,
            "bundle_model_ms": 0.0,
        },
        "knowledge_overview": {},
        "total_ms": 0.0,
    }

    frontier_started = perf_counter()
    frontier_claim_packet = build_frontier_claim_packet(query, papers)
    generation_profile["stages"]["frontier_claim_packet_ms"] = _elapsed_ms(frontier_started)

    knowledge_started = perf_counter()
    knowledge_overview, figure_manifest = await build_knowledge_overview_document(
        query=query,
        papers=papers,
        frontier_claim_packet=frontier_claim_packet,
    )
    generation_profile["stages"]["knowledge_overview_ms"] = _elapsed_ms(knowledge_started)
    generation_profile["knowledge_overview"] = dict(
        dict(knowledge_overview.get("metadata") or {}).get("generation_profile") or {}
    )

    vertical_started = perf_counter()
    vertical_report = build_vertical_report_document(
        query=query,
        papers=papers,
        frontier_claim_packet=frontier_claim_packet,
        knowledge_overview=knowledge_overview,
        synthesis_hint=synthesis_hint,
    )
    generation_profile["stages"]["vertical_report_ms"] = _elapsed_ms(vertical_started)

    bundle_started = perf_counter()
    bundle = build_literature_artifact_bundle(
        query=query,
        preset=preset,
        user_id=user_id,
        session_id=session_id,
        backend=backend,
        papers=papers,
        requested_sources=requested_sources,
        attempted_sources=attempted_sources,
        failed_sources=failed_sources,
        source_counts=source_counts,
        provider_health=provider_health,
        retrieval_policy=retrieval_policy,
        acquisition_envelope=acquisition_envelope,
        frontier_claim_packet=frontier_claim_packet,
        knowledge_overview=knowledge_overview,
        vertical_report=vertical_report,
        figure_manifest=figure_manifest,
        generation_profile=generation_profile,
        generation_notes=list(generation_notes or []),
    )
    generation_profile["stages"]["bundle_model_ms"] = _elapsed_ms(bundle_started)
    generation_profile["total_ms"] = _elapsed_ms(total_started)
    bundle = bundle.model_copy(update={"generation_profile": generation_profile})
    return bundle


def build_literature_artifact_bundle(
    *,
    query: str,
    preset: str,
    user_id: str,
    session_id: str,
    backend: str,
    papers: List[Dict[str, Any]],
    requested_sources: List[str],
    attempted_sources: List[str],
    failed_sources: List[str],
    source_counts: Dict[str, int],
    provider_health: Dict[str, Any],
    retrieval_policy: Dict[str, Any],
    acquisition_envelope: Dict[str, Any],
    frontier_claim_packet: Dict[str, Any] | None = None,
    knowledge_overview: Dict[str, Any] | None = None,
    vertical_report: Dict[str, Any] | None = None,
    figure_manifest: Dict[str, Any] | None = None,
    generation_profile: Dict[str, Any] | None = None,
    generation_notes: List[str] | None = None,
) -> LiteratureArtifactBundle:
    bundle = LiteratureArtifactBundle(
        query=query,
        preset=preset,
        user_id=user_id,
        session_id=session_id,
        backend=backend,
        requested_sources=list(requested_sources or []),
        attempted_sources=list(attempted_sources or []),
        failed_sources=list(failed_sources or []),
        source_counts=dict(source_counts or {}),
        provider_health=dict(provider_health or {}),
        retrieval_policy=dict(retrieval_policy or {}),
        acquisition_envelope=dict(acquisition_envelope or {}),
        canonical_paper_set=[canonicalize_paper_record(paper) for paper in list(papers or [])],
        paper_count=len(list(papers or [])),
        frontier_claim_packet=LiteratureArtifactDocument(**dict(frontier_claim_packet or {})),
        knowledge_overview=LiteratureArtifactDocument(**dict(knowledge_overview or {})),
        vertical_report=LiteratureArtifactDocument(**dict(vertical_report or {})),
        figure_manifest=dict(figure_manifest or {}),
        unified_scientific_packet={},
        generation_profile=dict(generation_profile or {}),
        generation_notes=list(generation_notes or []),
    )
    unified_packet = build_unified_scientific_report_packet(bundle.model_dump())
    bundle = bundle.model_copy(update={"unified_scientific_packet": unified_packet})
    return bundle
