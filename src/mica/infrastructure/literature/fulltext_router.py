"""Full-text acquisition router — acquire richest possible document."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from mica.config.dotenv_loader import resolve_env_value
from mica.infrastructure.literature.control_plane import (
    OPENALEX_PDF_DOWNLOAD_COST_USD,
    LiteratureAcquisitionAudit,
    LiteratureBudgetSnapshot,
    best_openalex_pdf_candidate,
    merge_metadata,
    normalize_doi,
    resolve_literature_rate_limits,
)
from mica.redis_throttle_coalesce import RedisThrottleCoalesce
from mica.memory.dlm.openalexapi_client import OpenAlexAPIClient

from .evidence_objects import build_evidence_manifest, build_evidence_object, infer_owner_id
from .persistence_policy import assess_persistence

logger = logging.getLogger(__name__)


class AcqStep(str, Enum):
    """Ordered acquisition strategy steps."""
    PMC_JATS = "pmc_jats"
    EUROPE_PMC = "europe_pmc"
    OA_URL = "oa_url"
    UNPAYWALL = "unpaywall"
    OPENALEX_METADATA = "openalex_metadata"
    OPENALEX_PDF = "openalex_pdf"
    S2_FULLTEXT = "s2_fulltext"
    PUBLISHER_HTML = "publisher_html"
    PDF = "pdf"
    OCR_PDF = "ocr_pdf"
    ABSTRACT_ONLY = "abstract_only"


@dataclass
class NormalizedDocument:
    """Canonical output from the full-text router."""

    paper_id: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    arxiv_id: str = ""
    title: str = ""
    abstract: str = ""
    full_text: str = ""
    sections: List[Dict[str, str]] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    figures_metadata: List[Dict[str, Any]] = field(default_factory=list)
    acquisition_kind: str = "abstract_only"
    provider: str = ""
    license_status: str = "unknown"
    content_uri: str = ""
    normalized_text_uri: str = ""
    section_json_uri: str = ""
    citations_json_uri: str = ""
    checksum: str = ""
    degraded: bool = True
    graph_worthiness_score: float = 0.0
    persistence_eligible: bool = False
    persistence_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    year: Optional[int] = None
    authors: List[str] = field(default_factory=list)
    journal: str = ""
    acquisition_audit: List[Dict[str, Any]] = field(default_factory=list)
    acquisition_cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


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


def _normalize_pmcid(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if candidate.upper().startswith("PMCID:"):
        candidate = candidate.split(":", 1)[1].strip()
    if re.fullmatch(r"\d+", candidate):
        return f"PMC{candidate}"
    if re.fullmatch(r"PMC\d+", candidate, re.IGNORECASE):
        return candidate.upper()
    return candidate


def _pmc_efetch_id(value: str) -> str:
    normalized = _normalize_pmcid(value)
    match = re.fullmatch(r"PMC(?P<numeric>\d+)", normalized, re.IGNORECASE)
    return match.group("numeric") if match else normalized


def _aiohttp_client_session(aiohttp: Any) -> Any:
    try:
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(),
            family=socket.AF_INET,
        )
        return aiohttp.ClientSession(connector=connector)
    except Exception:
        return aiohttp.ClientSession()


def _xml_local_name(tag: Any) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1] if "}" in text else text


class PMCJATSClient:
    """Fetch JATS XML from PMC/NCBI."""

    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, *, throttle_coalesce: Any = None, rate_limits: Optional[Dict[str, int]] = None) -> None:
        self.throttle_coalesce = throttle_coalesce
        self.rate_limits = dict(rate_limits or {})
        self.last_receipt: Dict[str, Any] = {}

    async def fetch(self, pmcid: str) -> Optional[str]:
        """Fetch JATS XML for a PMC article. Returns XML string or None."""
        if not pmcid:
            return None
        normalized_pmcid = _normalize_pmcid(pmcid)
        efetch_id = _pmc_efetch_id(normalized_pmcid)
        url = f"{self.BASE_URL}/efetch.fcgi"
        params = {"db": "pmc", "id": efetch_id, "rettype": "xml", "retmode": "xml"}
        self.last_receipt = {
            "provider": "pmc_jats",
            "identifier": normalized_pmcid or str(pmcid or ""),
            "provider_id": efetch_id,
            "content_url": f"{url}?{urlencode(params)}",
            "status": "started",
            "http_status": None,
            "content_length": 0,
            "detail": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            import aiohttp
            if self.throttle_coalesce is not None:
                await self.throttle_coalesce.acquire_provider_slot("pmc", self.rate_limits.get("pmc", 180), 3)
            async with _aiohttp_client_session(aiohttp) as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    self.last_receipt["http_status"] = resp.status
                    if resp.status == 200:
                        text = await resp.text()
                        self.last_receipt["content_length"] = len(text or "")
                        if "<article" in text:
                            self.last_receipt["status"] = "success"
                            return text
                        self.last_receipt["status"] = "empty_or_non_article"
                        self.last_receipt["detail"] = "PMC EFetch response did not contain a JATS article"
                    else:
                        self.last_receipt["status"] = "http_error"
                        self.last_receipt["detail"] = f"HTTP {resp.status}"
        except Exception as e:
            self.last_receipt["status"] = "error"
            self.last_receipt["detail"] = f"{type(e).__name__}: {e}"
            logger.debug("PMC JATS fetch failed for %s: %s", pmcid, e)
        return None


class EuropePMCClient:
    """Fetch full text from Europe PMC REST API."""

    BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def __init__(self, *, throttle_coalesce: Any = None, rate_limits: Optional[Dict[str, int]] = None) -> None:
        self.throttle_coalesce = throttle_coalesce
        self.rate_limits = dict(rate_limits or {})
        self.last_receipt: Dict[str, Any] = {}

    async def fetch(self, pmcid: str = "", doi: str = "") -> Optional[str]:
        if not pmcid and not doi:
            return None
        normalized_pmcid = _normalize_pmcid(pmcid)
        identifier = normalized_pmcid or str(doi or "").strip()
        url = f"{self.BASE_URL}/{identifier}/fullTextXML"
        self.last_receipt = {
            "provider": "europe_pmc",
            "identifier": identifier,
            "provider_id": identifier,
            "content_url": url,
            "status": "started",
            "http_status": None,
            "content_length": 0,
            "detail": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            import aiohttp
            if self.throttle_coalesce is not None:
                await self.throttle_coalesce.acquire_provider_slot("pmc", self.rate_limits.get("pmc", 180), 3)
            async with _aiohttp_client_session(aiohttp) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    self.last_receipt["http_status"] = resp.status
                    if resp.status == 200:
                        text = await resp.text()
                        self.last_receipt["content_length"] = len(text or "")
                        if "<article" in text or "<body" in text:
                            self.last_receipt["status"] = "success"
                            return text
                        self.last_receipt["status"] = "empty_or_non_article"
                        self.last_receipt["detail"] = "Europe PMC response did not contain full-text XML"
                    else:
                        self.last_receipt["status"] = "http_error"
                        self.last_receipt["detail"] = f"HTTP {resp.status}"
        except Exception as e:
            self.last_receipt["status"] = "error"
            self.last_receipt["detail"] = f"{type(e).__name__}: {e}"
            logger.debug("Europe PMC fetch failed: %s", e)
        return None

    async def search(self, query: str, max_results: int = 25) -> List[Dict[str, Any]]:
        """Search Europe PMC for papers matching query."""
        results: List[Dict[str, Any]] = []
        try:
            import aiohttp
            if self.throttle_coalesce is not None:
                await self.throttle_coalesce.acquire_provider_slot("pmc", self.rate_limits.get("pmc", 180), 3)
            url = f"{self.BASE_URL}/search"
            params = {
                "query": query,
                "resultType": "core",
                "pageSize": min(max_results, 100),
                "format": "json",
            }
            async with _aiohttp_client_session(aiohttp) as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for r in data.get("resultList", {}).get("result", []):
                            results.append({
                                "paper_id": r.get("id", ""),
                                "doi": r.get("doi", ""),
                                "pmid": r.get("pmid", ""),
                                "pmcid": r.get("pmcid", ""),
                                "title": r.get("title", ""),
                                "abstract": r.get("abstractText", ""),
                                "year": r.get("pubYear"),
                                "journal": r.get("journalTitle", ""),
                                "authors": [
                                    a.get("fullName", "")
                                    for a in r.get("authorList", {}).get("author", [])
                                ],
                                "has_fulltext": r.get("hasTextMinedTerms") == "Y"
                                    or r.get("isOpenAccess") == "Y",
                            })
        except Exception as e:
            logger.warning("Europe PMC search failed: %s", e)
        return results[:max_results]


class SemanticScholarClient:
    """P2-1: Fetch abstract/metadata from Semantic Scholar Graph API."""

    BASE_URL = "https://api.semanticscholar.org/graph/v1/paper"
    _FIELDS = "title,abstract,externalIds,year,venue,authors,openAccessPdf"

    def __init__(self, *, throttle_coalesce: Any = None, rate_limits: Optional[Dict[str, int]] = None) -> None:
        self.throttle_coalesce = throttle_coalesce
        self.rate_limits = dict(rate_limits or {})

    async def fetch_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        if not doi:
            return None
        from urllib.parse import quote
        return await self._fetch(f"DOI:{quote(doi, safe='')}")

    async def fetch_by_paper_id(self, paper_id: str) -> Optional[Dict[str, Any]]:
        if not paper_id:
            return None
        return await self._fetch(paper_id)

    async def _fetch(self, identifier: str) -> Optional[Dict[str, Any]]:
        try:
            import aiohttp
            import os
            if self.throttle_coalesce is not None:
                await self.throttle_coalesce.acquire_provider_slot("semantic_scholar", self.rate_limits.get("semantic_scholar", 100), 6)
            url = f"{self.BASE_URL}/{identifier}"
            headers: Dict[str, str] = {}
            api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
            if api_key:
                headers["x-api-key"] = api_key
            async with _aiohttp_client_session(aiohttp) as session:
                async with session.get(
                    url,
                    params={"fields": self._FIELDS},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            logger.debug("Semantic Scholar fetch failed for %s: %s", identifier, e)
        return None


class UnpaywallClient:
    """Resolve open-access PDF candidates from DOI."""

    BASE_URL = "https://api.unpaywall.org/v2"

    def __init__(self, *, throttle_coalesce: Any = None, rate_limits: Optional[Dict[str, int]] = None) -> None:
        self._email = resolve_env_value("MICA_CONTACT_EMAIL", "CONTACT_EMAIL") or "mica-agent@local.invalid"
        self.throttle_coalesce = throttle_coalesce
        self.rate_limits = dict(rate_limits or {})

    async def lookup(self, doi: str) -> Optional[Dict[str, Any]]:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        try:
            import aiohttp
            if self.throttle_coalesce is not None:
                await self.throttle_coalesce.acquire_provider_slot("unpaywall", self.rate_limits.get("unpaywall", 120), 6)
            url = f"{self.BASE_URL}/{normalized}"
            async with _aiohttp_client_session(aiohttp) as session:
                async with session.get(
                    url,
                    params={"email": self._email},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as exc:
            logger.debug("Unpaywall lookup failed for %s: %s", doi, exc)
        return None


class DocumentSectionizer:
    """Extract sections from JATS XML or raw text."""

    SECTION_NAMES = [
        "title", "abstract", "introduction", "methods", "results",
        "discussion", "conclusion", "supplementary", "references",
    ]

    def from_jats_xml(self, xml_text: str) -> List[Dict[str, str]]:
        """Parse JATS XML into section dicts."""
        sections: List[Dict[str, str]] = []
        seen: set[str] = set()
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            # Extract abstract
            for abstract_el in root.iter():
                if _xml_local_name(abstract_el.tag) != "abstract":
                    continue
                text = "".join(abstract_el.itertext()).strip()
                dedupe_key = re.sub(r"\s+", " ", text)
                if text and dedupe_key not in seen:
                    seen.add(dedupe_key)
                    sections.append({"name": "abstract", "text": text})
            # Extract body sections
            for sec in root.iter():
                if _xml_local_name(sec.tag) != "sec":
                    continue
                title_el = next((child for child in list(sec) if _xml_local_name(child.tag) == "title"), None)
                sec_title = title_el.text.strip() if title_el is not None and title_el.text else ""
                sec_text = "".join(sec.itertext()).strip()
                dedupe_key = re.sub(r"\s+", " ", sec_text)
                name = self._classify_section(sec_title)
                if sec_text and dedupe_key not in seen:
                    seen.add(dedupe_key)
                    sections.append({"name": name, "title": sec_title, "text": sec_text})
            # Extract figure captions
            for fig in root.iter():
                if _xml_local_name(fig.tag) != "fig":
                    continue
                caption_el = next((child for child in fig.iter() if _xml_local_name(child.tag) == "caption"), None)
                if caption_el is not None:
                    text = "".join(caption_el.itertext()).strip()
                    dedupe_key = re.sub(r"\s+", " ", text)
                    if text and dedupe_key not in seen:
                        seen.add(dedupe_key)
                        sections.append({"name": "figure_caption", "text": text})
        except Exception as e:
            logger.debug("JATS sectionizer failed: %s", e)
        return sections

    def from_plain_text(self, text: str) -> List[Dict[str, str]]:
        """Heuristic section splitting for raw text."""
        sections: List[Dict[str, str]] = []
        if not text.strip():
            return sections
        # Simple heading-based split
        pattern = re.compile(
            r"^(abstract|introduction|methods|materials?\s*(?:and|&)\s*methods|"
            r"results|discussion|conclusions?|references|acknowledgments?)"
            r"\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        parts = pattern.split(text)
        if len(parts) <= 1:
            sections.append({"name": "body", "text": text.strip()})
        else:
            # parts layout: [pre, heading1, body1, heading2, body2, ...]
            preamble = parts[0].strip()
            if preamble:
                sections.append({"name": "other", "title": "preamble", "text": preamble})
            for i in range(1, len(parts) - 1, 2):
                heading = parts[i].strip()
                body = parts[i + 1].strip() if i + 1 < len(parts) else ""
                if body:
                    name = self._classify_section(heading)
                    sections.append({"name": name, "title": heading, "text": body})
        return sections

    def _classify_section(self, title: str) -> str:
        t = title.lower().strip()
        for name in self.SECTION_NAMES:
            if name in t:
                return name
        if "material" in t and "method" in t:
            return "methods"
        return "other"


class CitationBundleExtractor:
    """Extract citation identifiers from JATS XML."""

    def extract(self, xml_text: str) -> List[Dict[str, str]]:
        refs: List[Dict[str, str]] = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            for ref in root.iter():
                if _xml_local_name(ref.tag) != "ref":
                    continue
                entry: Dict[str, str] = {}
                for eid in ref.iter():
                    if _xml_local_name(eid.tag) != "pub-id":
                        continue
                    id_type = eid.get("pub-id-type", "")
                    if id_type == "doi" and eid.text:
                        entry["doi"] = eid.text.strip()
                    elif id_type == "pmid" and eid.text:
                        entry["pmid"] = eid.text.strip()
                # Article title
                for at in ref.iter():
                    if _xml_local_name(at.tag) != "article-title":
                        continue
                    entry["title"] = "".join(at.itertext()).strip()
                if entry:
                    refs.append(entry)
        except Exception as e:
            logger.debug("Citation extraction failed: %s", e)
        return refs


class FullTextRouter:
    """Acquire the richest possible document representation.

    Strategy order (per §5.2 of the spec):
    1. PMC JATS XML
    2. Europe PMC XML
    3. Semantic Scholar fulltext (abstract fallback)
    4. Publisher HTML (not yet implemented)
    5. PDF (not yet implemented)
    6. OCR (not yet implemented)
    7. Abstract-only degraded
    """

    def __init__(self, *, storage: Any = None) -> None:
        self._rate_limits = resolve_literature_rate_limits()
        self._throttle_coalesce = RedisThrottleCoalesce()
        self._throttle_init_attempted = False
        self._pmc = PMCJATSClient(throttle_coalesce=self._throttle_coalesce, rate_limits=self._rate_limits)
        self._epmc = EuropePMCClient(throttle_coalesce=self._throttle_coalesce, rate_limits=self._rate_limits)
        self._s2 = SemanticScholarClient(throttle_coalesce=self._throttle_coalesce, rate_limits=self._rate_limits)
        self._unpaywall = UnpaywallClient(throttle_coalesce=self._throttle_coalesce, rate_limits=self._rate_limits)
        self._openalex = OpenAlexAPIClient(
            api_key=resolve_env_value("OPENALEX_API_KEY"),
            throttle_coalesce=self._throttle_coalesce,
            rate_limits=self._rate_limits,
        )
        self._sectionizer = DocumentSectionizer()
        self._citation_extractor = CitationBundleExtractor()
        if storage is not None:
            self._storage = storage
        else:
            try:
                from mica.storage.gcs_user_storage import get_storage_manager

                self._storage = get_storage_manager()
            except Exception:
                self._storage = None

    async def _ensure_provider_controls(self) -> None:
        if self._throttle_init_attempted:
            return
        self._throttle_init_attempted = True
        await self._throttle_coalesce.initialize()

    def provider_controls_snapshot(self) -> Dict[str, Any]:
        snapshot = getattr(self._throttle_coalesce, "telemetry_snapshot", None)
        return {
            "rate_limits": dict(self._rate_limits),
            "throttle_telemetry": snapshot() if callable(snapshot) else {"mode": "unknown", "initialized": False},
        }

    def _append_audit(self, doc: NormalizedDocument, audit: LiteratureAcquisitionAudit) -> None:
        doc.acquisition_audit.append(audit.to_dict())
        doc.acquisition_cost_usd = round(doc.acquisition_cost_usd + float(audit.acquisition_cost_usd or 0.0), 6)
        doc.metadata["acquisition_audit"] = list(doc.acquisition_audit)
        doc.metadata["acquisition_cost_usd"] = doc.acquisition_cost_usd

    def _append_provider_receipt_audit(
        self,
        doc: NormalizedDocument,
        receipt: Dict[str, Any],
        *,
        source: str,
        method: str,
    ) -> None:
        if not receipt:
            return
        status = str(receipt.get("status") or "unknown")
        provider_receipts = doc.metadata.setdefault("provider_fetch_receipts", [])
        provider_receipts.append(
            self._canonicalize_provider_receipt(
                receipt,
                source=source,
                method=method,
            )
        )
        audit = LiteratureAcquisitionAudit(
            acquisition_source=source,
            acquisition_method=method,
            acquisition_status="success" if status == "success" else status,
            http_status=_safe_int(receipt.get("http_status")),
            content_url=str(receipt.get("content_url") or ""),
            access_tier="open_access" if status == "success" else "unknown",
            is_open_access=True if status == "success" else None,
            license_status="open" if status == "success" else "unknown",
            detail=str(receipt.get("detail") or ""),
        )
        self._append_audit(doc, audit)
        if status != "success":
            self._record_provider_degradation(
                doc,
                provider=source,
                status=status,
                detail=str(receipt.get("detail") or ""),
                http_status=receipt.get("http_status"),
                content_url=str(receipt.get("content_url") or ""),
            )

    def _record_provider_degradation(
        self,
        doc: NormalizedDocument,
        *,
        provider: str,
        status: str,
        detail: str = "",
        http_status: Any = None,
        content_url: str = "",
    ) -> None:
        failures = doc.metadata.setdefault("provider_failures", [])
        failures.append(
            {
                "provider": provider,
                "provider_role": _provider_role(provider),
                "status": status,
                "http_status": http_status,
                "content_url": content_url,
                "detail": detail,
            }
        )

    def _canonicalize_provider_receipt(
        self,
        receipt: Dict[str, Any],
        *,
        source: str,
        method: str,
        doc: Optional[NormalizedDocument] = None,
    ) -> Dict[str, Any]:
        raw = dict(receipt or {})
        provider = str(raw.get("provider") or source or "").strip()
        content_url = str(raw.get("content_url") or raw.get("source_url") or "").strip()
        fetch_timestamp = str(raw.get("fetch_timestamp") or raw.get("fetched_at") or "").strip() or datetime.now(timezone.utc).isoformat()
        acquisition_status = str(raw.get("acquisition_status") or raw.get("status") or "unknown").strip()
        content_checksum = ""
        if doc is not None and doc.checksum and acquisition_status == "success":
            content_checksum = str(doc.checksum)
        normalized = {
            **raw,
            "provider": provider,
            "provider_role": _provider_role(provider),
            "provider_id": str(raw.get("provider_id") or raw.get("identifier") or "").strip(),
            "source_url": content_url,
            "content_url": content_url,
            "fetch_timestamp": fetch_timestamp,
            "fetched_at": fetch_timestamp or str(raw.get("fetched_at") or ""),
            "acquisition_source": str(source or provider),
            "acquisition_method": str(method or raw.get("acquisition_method") or "").strip(),
            "acquisition_status": acquisition_status,
            "http_status": _safe_int(raw.get("http_status")),
            "content_checksum": content_checksum,
        }
        return normalized

    def _refresh_provider_lineage(self, doc: NormalizedDocument) -> None:
        receipts = [
            self._canonicalize_provider_receipt(
                dict(receipt),
                source=str(receipt.get("acquisition_source") or receipt.get("provider") or ""),
                method=str(receipt.get("acquisition_method") or ""),
                doc=doc,
            )
            for receipt in list(doc.metadata.get("provider_fetch_receipts") or [])
            if isinstance(receipt, dict)
        ]
        if receipts:
            doc.metadata["provider_fetch_receipts"] = receipts

        text_materialized = bool(doc.checksum)
        complete_receipt_count = 0
        missing_fields: List[str] = []
        successful_receipts = 0
        for receipt in receipts:
            if receipt.get("acquisition_status") == "success":
                successful_receipts += 1
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
            missing = [
                field_name
                for field_name in required_fields
                if receipt.get(field_name) in (None, "")
            ]
            if not missing and receipt.get("provider_role") == "canonical_literature_provider":
                complete_receipt_count += 1
            for field_name in missing:
                missing_fields.append(f"{receipt.get('provider') or 'unknown'}:{field_name}")

        primary_receipt: Dict[str, Any] = {}
        if receipts:
            primary_receipt = next(
                (receipt for receipt in receipts if receipt.get("acquisition_status") == "success"),
                receipts[0],
            )
        if doc.checksum:
            doc.metadata["content_checksum"] = doc.checksum
        doc.metadata["primary_provider_dna_receipt"] = dict(primary_receipt)
        doc.metadata["provider_lineage"] = {
            "status": (
                "complete"
                if complete_receipt_count > 0
                else "incomplete"
                if receipts
                else "absent"
            ),
            "receipt_count": len(receipts),
            "successful_receipt_count": successful_receipts,
            "complete_receipt_count": complete_receipt_count,
            "missing_fields": sorted(set(missing_fields)),
            "provider_roles": sorted({str(receipt.get("provider_role") or "") for receipt in receipts if receipt.get("provider_role")}),
            "text_materialized": text_materialized,
        }

    def _provider_degradation_flags(self, doc: NormalizedDocument) -> List[str]:
        flags: List[str] = []
        for failure in list(doc.metadata.get("provider_failures") or []):
            provider = re.sub(r"[^a-z0-9_]+", "_", str(failure.get("provider") or "provider").lower()).strip("_")
            status = re.sub(r"[^a-z0-9_]+", "_", str(failure.get("status") or "").lower()).strip("_")
            if provider:
                if status in {"error", "http_error", "unknown", "started"}:
                    flags.append(f"{provider}_unavailable")
                elif status:
                    flags.append(f"{provider}_{status}")
                else:
                    flags.append(f"{provider}_unavailable")
        return sorted(set(flags))

    def _summarize_jats_xml(self, xml: str) -> Dict[str, Any]:
        summary = {
            "article_count": 0,
            "abstract_count": 0,
            "body_count": 0,
            "sec_count": 0,
            "fig_count": 0,
            "ref_count": 0,
            "body_text_length": 0,
            "has_full_body": False,
        }
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(xml)
            for element in root.iter():
                name = _xml_local_name(element.tag)
                if name == "article":
                    summary["article_count"] += 1
                elif name == "abstract":
                    summary["abstract_count"] += 1
                elif name == "body":
                    summary["body_count"] += 1
                    summary["body_text_length"] += len("".join(element.itertext()).strip())
                elif name == "sec":
                    summary["sec_count"] += 1
                elif name == "fig":
                    summary["fig_count"] += 1
                elif name == "ref":
                    summary["ref_count"] += 1
        except Exception as exc:
            summary["parse_error"] = str(exc)
        summary["has_full_body"] = bool(summary["body_count"] and (summary["sec_count"] or summary["body_text_length"] > 1000))
        return summary

    def _retain_partial_jats_abstract(
        self,
        doc: NormalizedDocument,
        *,
        xml: str,
        provider: str,
        receipt: Dict[str, Any],
        summary: Dict[str, Any],
    ) -> None:
        sections = self._sectionizer.from_jats_xml(xml)
        for section in sections:
            if section.get("name") == "abstract" and section.get("text"):
                doc.abstract = doc.abstract or str(section.get("text") or "")
                break
        candidates = doc.metadata.setdefault("jats_xml_candidates", [])
        candidates.append({"provider": provider, **summary})
        self._record_provider_degradation(
            doc,
            provider=provider,
            status="abstract_only_jats",
            detail="JATS XML was fetched but did not include body/full-text sections",
            http_status=receipt.get("http_status"),
            content_url=str(receipt.get("content_url") or ""),
        )

    async def close(self) -> None:
        if self._openalex is not None:
            await self._openalex.close()

    async def _extract_text_from_pdf_bytes(self, payload: bytes) -> str:
        if not payload:
            return ""
        try:
            import fitz  # type: ignore

            doc = fitz.open(stream=payload, filetype="pdf")
            parts: List[str] = []
            for page in doc:
                txt = page.get_text("text") or ""
                if txt:
                    parts.append(txt)
            doc.close()
            return "\n".join(parts).strip()
        except Exception:
            pass

        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(io.BytesIO(payload))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    parts.append("")
            return "\n".join(parts).strip()
        except Exception:
            return ""

    async def _download_pdf_text(self, url: str) -> str:
        clean_url = str(url or "").strip()
        if not clean_url:
            return ""
        try:
            import aiohttp

            async with _aiohttp_client_session(aiohttp) as session:
                async with session.get(clean_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return ""
                    content_type = str(resp.headers.get("content-type") or "").lower()
                    payload = await resp.read()
                    if "pdf" in content_type or clean_url.lower().endswith(".pdf"):
                        return await self._extract_text_from_pdf_bytes(payload)
                    text = payload.decode("utf-8", errors="ignore")
                    return text if "<html" not in text.lower() else ""
        except Exception as exc:
            logger.debug("PDF/HTML download failed for %s: %s", clean_url, exc)
            return ""

    async def _build_pdf_document(
        self,
        doc: NormalizedDocument,
        *,
        url: str,
        provider: str,
        license_status: str,
        phase_separation_kind: str,
        audit: LiteratureAcquisitionAudit,
    ) -> Optional[NormalizedDocument]:
        extracted_text = await self._download_pdf_text(url)
        if not extracted_text.strip():
            self._append_audit(doc, audit)
            return None
        doc.full_text = extracted_text
        doc.sections = self._sectionizer.from_plain_text(extracted_text)
        doc.abstract = doc.abstract or (doc.sections[0].get("text", "") if doc.sections else "")
        doc.acquisition_kind = "pdf"
        doc.degraded = False
        doc.provider = provider
        doc.license_status = license_status or doc.license_status
        doc.metadata["pdf_url"] = url
        doc.checksum = hashlib.sha256(extracted_text.encode()).hexdigest()[:16]
        self._append_audit(doc, audit)
        return self._finalize_document(doc, phase_separation_kind=phase_separation_kind, raw_content=extracted_text)

    def _materialization_root(self) -> Path:
        raw_root = os.getenv("MICA_FULLTEXT_CACHE_DIR", "./outputs/kb_fulltext_router")
        root = Path(raw_root)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _materialize_document_payloads(
        self,
        doc: NormalizedDocument,
        *,
        raw_content: str,
    ) -> Dict[str, str]:
        doc_key = re.sub(r"[^A-Za-z0-9._-]+", "_", doc.paper_id or doc.doi or doc.pmcid or doc.pmid or doc.checksum or "document")
        doc_dir = self._materialization_root() / doc_key[:80]
        doc_dir.mkdir(parents=True, exist_ok=True)

        normalized_text = (doc.full_text or doc.abstract or "").strip()
        content_path = doc_dir / "content.txt"
        normalized_path = doc_dir / "normalized_text.txt"
        sections_path = doc_dir / "sections.json"
        citations_path = doc_dir / "citations.json"

        content_path.write_text(str(raw_content or normalized_text), encoding="utf-8")
        normalized_path.write_text(normalized_text, encoding="utf-8")
        sections_path.write_text(json.dumps(doc.sections, ensure_ascii=False, indent=2), encoding="utf-8")
        citations_path.write_text(json.dumps(doc.citations, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "content_uri": str(content_path),
            "normalized_text_uri": str(normalized_path),
            "section_json_uri": str(sections_path),
            "citations_json_uri": str(citations_path),
        }

    def _realize_evidence_objects(
        self,
        doc: NormalizedDocument,
        *,
        local_cache: Dict[str, str],
        raw_content: str,
    ) -> None:
        metadata = dict(doc.metadata or {})
        require_cloud_evidence = bool(
            metadata.get("require_cloud_evidence")
            or str(metadata.get("scientific_os_mode") or "").strip().lower() == "required"
        )
        require_authenticated_user_owner = bool(
            metadata.get("require_authenticated_user_owner")
            or metadata.get("artifact_grade_literature")
            or require_cloud_evidence
        )
        owner_id = infer_owner_id(
            metadata,
            require_authenticated_user=require_authenticated_user_owner,
        )
        storage = getattr(self, "_storage", None)
        session_id = str(
            metadata.get("session_id")
            or metadata.get("workspace_session_id")
            or metadata.get("request_session_id")
            or metadata.get("run_id")
            or ""
        )
        run_id = str(
            metadata.get("run_id")
            or metadata.get("request_run_id")
            or metadata.get("job_id")
            or session_id
            or ""
        )
        metadata["session_id"] = session_id
        metadata["run_id"] = run_id
        if owner_id:
            metadata["owner_id"] = owner_id
        failure_reason = ""
        if not owner_id:
            if require_authenticated_user_owner:
                failure_reason = "authenticated user_id missing for evidence persistence"
            else:
                failure_reason = "owner_id missing for evidence persistence"
        elif storage is None:
            failure_reason = "cloud storage unavailable for evidence persistence"
        elif require_cloud_evidence and (not session_id or not run_id):
            failure_reason = "artifact-grade evidence requires session_id and run_id"

        if failure_reason:
            if require_cloud_evidence:
                raise ValueError(failure_reason)
            doc.content_uri = local_cache["content_uri"]
            doc.normalized_text_uri = local_cache["normalized_text_uri"]
            doc.section_json_uri = local_cache["section_json_uri"]
            doc.citations_json_uri = local_cache["citations_json_uri"]
            doc.metadata["citations_json_uri"] = local_cache["citations_json_uri"]
            doc.metadata["evidence_backend"] = "local_cache_degraded"
            doc.metadata["evidence_persistence_error"] = failure_reason
            doc.metadata["local_cache"] = dict(local_cache)
            return

        doc_key = re.sub(r"[^A-Za-z0-9._-]+", "_", doc.paper_id or doc.doi or doc.pmcid or doc.pmid or doc.checksum or "document")[:80]
        evidence_prefix = f"evidence/fulltext/{doc_key}"
        evidence_objects = []
        payload_specs = [
            ("content_uri", "content.txt", str(raw_content or doc.full_text or doc.abstract or ""), "text/plain; charset=utf-8"),
            ("normalized_text_uri", "normalized_text.txt", str((doc.full_text or doc.abstract or "").strip()), "text/plain; charset=utf-8"),
            ("section_json_uri", "sections.json", json.dumps(doc.sections, ensure_ascii=False, indent=2), "application/json"),
            ("citations_json_uri", "citations.json", json.dumps(doc.citations, ensure_ascii=False, indent=2), "application/json"),
        ]

        parent_evidence_id = ""
        for field_name, filename, payload_text, content_type in payload_specs:
            payload_bytes = payload_text.encode("utf-8")
            storage_uri = storage.upload_bytes(
                user_id=owner_id,
                object_path=f"{evidence_prefix}/{filename}",
                data=payload_bytes,
                content_type=content_type,
                metadata={
                    "paper_id": str(doc.paper_id or ""),
                    "doi": str(doc.doi or ""),
                    "provider": str(doc.provider or ""),
                    "owner_id": owner_id,
                    "session_id": session_id,
                    "run_id": run_id,
                },
            )
            evidence = build_evidence_object(
                storage_uri=storage_uri,
                content_type=content_type,
                payload=payload_bytes,
                owner_id=owner_id,
                logical_alias=field_name,
                producer="fulltext_router",
                producer_type="literature_fulltext",
                session_id=session_id,
                run_id=run_id,
                parent_evidence_id=parent_evidence_id,
            )
            if not parent_evidence_id:
                parent_evidence_id = evidence.evidence_id
            setattr(doc, field_name, storage_uri)
            evidence_objects.append(evidence.to_dict())

        doc.metadata["citations_json_uri"] = doc.citations_json_uri
        doc.metadata["session_id"] = session_id
        doc.metadata["run_id"] = run_id
        doc.metadata["owner_id"] = owner_id
        doc.metadata["evidence_backend"] = "gcs_user_storage"
        doc.metadata["local_cache"] = dict(local_cache)
        doc.metadata["evidence_objects"] = evidence_objects
        evidence_manifest = build_evidence_manifest(
            owner_id=owner_id,
            producer="fulltext_router",
            producer_type="literature_fulltext",
            evidence_backend="gcs_user_storage",
            evidence_objects=evidence_objects,
            session_id=session_id,
            run_id=run_id,
        )
        manifest_uri = storage.upload_text(
            user_id=owner_id,
            object_path=f"{evidence_prefix}/evidence_manifest.json",
            text=json.dumps(evidence_manifest.to_dict(), ensure_ascii=False, indent=2),
            content_type="application/json",
            metadata={
                "paper_id": str(doc.paper_id or ""),
                "doi": str(doc.doi or ""),
                "session_id": session_id,
                "run_id": run_id,
                "manifest_id": evidence_manifest.manifest_id,
            },
        )
        doc.metadata["evidence_manifest_uri"] = manifest_uri
        doc.metadata["evidence_manifest"] = evidence_manifest.to_dict()
        doc.metadata["evidence_objects_by_alias"] = {
            str(item.get("logical_alias") or ""): str(item.get("storage_uri") or "")
            for item in evidence_objects
        }

    def _finalize_document(
        self,
        doc: NormalizedDocument,
        *,
        phase_separation_kind: str,
        degradation_flags: Optional[List[str]] = None,
        raw_content: str = "",
    ) -> NormalizedDocument:
        retained_text = (doc.full_text or "").strip()
        assessment = assess_persistence(
            text=retained_text or doc.abstract or "",
            sections_count=len(doc.sections),
            citation_count=len(doc.citations),
            degraded=doc.degraded,
        )
        flags = sorted({str(flag) for flag in (degradation_flags or []) if str(flag)})
        if doc.degraded:
            flags = sorted(set(flags + ["phase_separation_active"]))
        if not assessment.persistence_eligible:
            flags = sorted(set(flags + ["low_structural_yield"]))

        doc.graph_worthiness_score = assessment.graph_worthiness_score
        doc.persistence_eligible = assessment.persistence_eligible
        doc.persistence_reason = assessment.persistence_reason
        doc.metadata["degradation_flags"] = flags
        doc.metadata["phase_separation"] = {
            "kind": phase_separation_kind,
            "degraded": doc.degraded,
            "retained_sections": len(doc.sections),
            "retained_characters": len(retained_text),
            "citation_count": len(doc.citations),
            "monomer_signal_count": assessment.monomer_signal_count,
            "graph_worthiness_score": assessment.graph_worthiness_score,
            "persistence_eligible": assessment.persistence_eligible,
            "persistence_reason": assessment.persistence_reason,
            "acquisition_cost_usd": doc.acquisition_cost_usd,
        }
        self._refresh_provider_lineage(doc)
        local_cache = self._materialize_document_payloads(doc, raw_content=raw_content or retained_text or doc.abstract)
        self._realize_evidence_objects(doc, local_cache=local_cache, raw_content=raw_content or retained_text or doc.abstract)
        return doc

    async def acquire_single(
        self,
        *,
        paper_id: str = "",
        doi: str = "",
        pmid: str = "",
        pmcid: str = "",
        title: str = "",
        abstract: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> NormalizedDocument:
        """Acquire the richest representation of a single paper."""
        await self._ensure_provider_controls()
        seed_metadata = dict(metadata or {})
        budget = LiteratureBudgetSnapshot(
            tenant_id=str(seed_metadata.get("tenant_id") or "default"),
            max_budget_usd=(float(seed_metadata["acquisition_budget_usd"]) if seed_metadata.get("acquisition_budget_usd") not in (None, "") else None),
            spent_usd=float(seed_metadata.get("budget_spent_usd") or 0.0),
        )
        allow_paid_openalex = bool(seed_metadata.get("allow_paid_openalex") or seed_metadata.get("allow_paid_fulltext"))
        doc = NormalizedDocument(
            paper_id=paper_id,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            arxiv_id=str(seed_metadata.get("arxiv_id", "") or ""),
            title=title,
            abstract=abstract,
            year=_safe_int(seed_metadata.get("year")),
            authors=[str(author) for author in (seed_metadata.get("authors") or []) if str(author)],
            journal=str(seed_metadata.get("journal", "") or ""),
            metadata=seed_metadata,
        )
        doc.metadata["provider_controls"] = self.provider_controls_snapshot()
        if seed_metadata.get("pdf_url"):
            doc.metadata["pdf_url"] = seed_metadata.get("pdf_url")

        # Step 1: PMC JATS
        if pmcid:
            xml = await self._pmc.fetch(pmcid)
            pmc_receipt = dict(getattr(self._pmc, "last_receipt", {}) or {})
            self._append_provider_receipt_audit(doc, pmc_receipt, source="pmc_jats", method="efetch_jats_xml")
            if xml:
                pmc_summary = self._summarize_jats_xml(xml)
                if pmc_summary.get("has_full_body"):
                    return self._build_from_xml(doc, xml, provider="pmc_jats")
                self._retain_partial_jats_abstract(
                    doc,
                    xml=xml,
                    provider="pmc_jats",
                    receipt=pmc_receipt,
                    summary=pmc_summary,
                )

        # Step 2: Europe PMC
        xml = await self._epmc.fetch(pmcid=pmcid, doi=doi)
        epmc_receipt = dict(getattr(self._epmc, "last_receipt", {}) or {})
        self._append_provider_receipt_audit(doc, epmc_receipt, source="europe_pmc", method="full_text_xml")
        if xml:
            epmc_summary = self._summarize_jats_xml(xml)
            if epmc_summary.get("has_full_body"):
                return self._build_from_xml(doc, xml, provider="europe_pmc")
            self._retain_partial_jats_abstract(
                doc,
                xml=xml,
                provider="europe_pmc",
                receipt=epmc_receipt,
                summary=epmc_summary,
            )

        # Step 3: trusted OA URL already attached to metadata
        seed_pdf_url = str(seed_metadata.get("pdf_url") or "").strip()
        if seed_pdf_url:
            oa_doc = await self._build_pdf_document(
                doc,
                url=seed_pdf_url,
                provider="metadata_oa_url",
                license_status=str(seed_metadata.get("license_status") or "unknown"),
                phase_separation_kind="trusted_oa_pdf",
                audit=LiteratureAcquisitionAudit(
                    acquisition_source=AcqStep.OA_URL.value,
                    acquisition_method="direct_pdf_fetch",
                    acquisition_status="success",
                    license_status=str(seed_metadata.get("license_status") or "unknown"),
                    content_url=seed_pdf_url,
                    is_open_access=True,
                    access_tier="open_access",
                ),
            )
            if oa_doc is not None:
                oa_doc.metadata["budget"] = budget.to_dict()
                return oa_doc

        # Step 4: Unpaywall
        if doi:
            unpaywall_record = await self._unpaywall.lookup(doi)
            if isinstance(unpaywall_record, dict):
                best_location = dict(unpaywall_record.get("best_oa_location") or {})
                pdf_url = str(best_location.get("url_for_pdf") or best_location.get("url") or "").strip()
                if pdf_url:
                    oa_doc = await self._build_pdf_document(
                        doc,
                        url=pdf_url,
                        provider="unpaywall",
                        license_status=str(best_location.get("license") or "unknown"),
                        phase_separation_kind="unpaywall_oa_pdf",
                        audit=LiteratureAcquisitionAudit(
                            acquisition_source=AcqStep.UNPAYWALL.value,
                            acquisition_method="open_access_pdf",
                            acquisition_status="success",
                            license_status=str(best_location.get("license") or "unknown"),
                            content_url=pdf_url,
                            is_open_access=True,
                            access_tier="open_access",
                        ),
                    )
                    if oa_doc is not None:
                        oa_doc.metadata["unpaywall"] = unpaywall_record
                        oa_doc.metadata["budget"] = budget.to_dict()
                        return oa_doc

        # Step 5: OpenAlex metadata / OA locations / paid content
        openalex_work: Optional[Dict[str, Any]] = None
        if self._openalex is not None and doi:
            try:
                openalex_work = await self._openalex.get_work_by_doi(doi)
            except Exception as exc:
                logger.debug("OpenAlex DOI lookup failed for %s: %s", doi, exc)

        if isinstance(openalex_work, dict):
            doc.metadata = merge_metadata(doc.metadata, {"openalex": openalex_work})
            candidate = best_openalex_pdf_candidate(openalex_work)
            candidate_url = str(candidate.get("url") or "").strip()
            if candidate_url:
                oa_doc = await self._build_pdf_document(
                    doc,
                    url=candidate_url,
                    provider="openalex_oa",
                    license_status=str(candidate.get("license") or "unknown"),
                    phase_separation_kind="openalex_oa_pdf",
                    audit=LiteratureAcquisitionAudit(
                        acquisition_source=AcqStep.OPENALEX_METADATA.value,
                        acquisition_method="open_access_pdf",
                        acquisition_status="success",
                        license_status=str(candidate.get("license") or "unknown"),
                        content_url=candidate_url,
                        is_open_access=bool(candidate.get("is_oa")),
                        access_tier="open_access",
                    ),
                )
                if oa_doc is not None:
                    oa_doc.metadata["budget"] = budget.to_dict()
                    return oa_doc

            has_openalex_pdf = bool((openalex_work.get("has_content") or {}).get("pdf"))
            openalex_id = str(openalex_work.get("id") or "").rsplit("/", 1)[-1]
            if allow_paid_openalex and has_openalex_pdf and openalex_id:
                if budget.can_spend(OPENALEX_PDF_DOWNLOAD_COST_USD):
                    try:
                        pdf_bytes = await self._openalex.download_pdf(openalex_id)
                        extracted_text = await self._extract_text_from_pdf_bytes(pdf_bytes)
                        if extracted_text.strip():
                            budget.record(
                                kind="openalex_pdf_download",
                                amount_usd=OPENALEX_PDF_DOWNLOAD_COST_USD,
                                accepted=True,
                                detail=openalex_id,
                            )
                            doc.full_text = extracted_text
                            doc.sections = self._sectionizer.from_plain_text(extracted_text)
                            doc.acquisition_kind = "pdf"
                            doc.degraded = False
                            doc.provider = "openalex_paid"
                            doc.license_status = str((openalex_work.get("best_oa_location") or {}).get("license") or doc.license_status)
                            doc.checksum = hashlib.sha256(extracted_text.encode()).hexdigest()[:16]
                            self._append_audit(
                                doc,
                                LiteratureAcquisitionAudit(
                                    acquisition_source=AcqStep.OPENALEX_PDF.value,
                                    acquisition_method="paid_pdf_download",
                                    acquisition_status="success",
                                    acquisition_cost_usd=OPENALEX_PDF_DOWNLOAD_COST_USD,
                                    content_url=f"https://content.openalex.org/works/{openalex_id}.pdf",
                                    access_tier="paid_content",
                                    is_open_access=False,
                                    license_status=doc.license_status,
                                ),
                            )
                            doc.metadata["budget"] = budget.to_dict()
                            return self._finalize_document(
                                doc,
                                phase_separation_kind="openalex_paid_pdf",
                                raw_content=extracted_text,
                            )
                    except Exception as exc:
                        self._append_audit(
                            doc,
                            LiteratureAcquisitionAudit(
                                acquisition_source=AcqStep.OPENALEX_PDF.value,
                                acquisition_method="paid_pdf_download",
                                acquisition_status="error",
                                acquisition_cost_usd=0.0,
                                content_url=f"https://content.openalex.org/works/{openalex_id}.pdf",
                                access_tier="paid_content",
                                is_open_access=False,
                                detail=str(exc),
                            ),
                        )
                else:
                    budget.record(
                        kind="openalex_pdf_download",
                        amount_usd=OPENALEX_PDF_DOWNLOAD_COST_USD,
                        accepted=False,
                        detail=openalex_id,
                    )
                    self._append_audit(
                        doc,
                        LiteratureAcquisitionAudit(
                            acquisition_source=AcqStep.OPENALEX_PDF.value,
                            acquisition_method="paid_pdf_download",
                            acquisition_status="budget_denied",
                            acquisition_cost_usd=OPENALEX_PDF_DOWNLOAD_COST_USD,
                            content_url=f"https://content.openalex.org/works/{openalex_id}.pdf",
                            access_tier="paid_content",
                            is_open_access=False,
                            detail="budget_exhausted",
                        ),
                    )

        # Step 6: Semantic Scholar abstract + metadata (P2-1)
        s2_data = await self._s2.fetch_by_doi(doi) if doi else None
        if not s2_data and paper_id:
            s2_data = await self._s2.fetch_by_paper_id(paper_id)
        if s2_data:
            doc.title = doc.title or str(s2_data.get("title") or "")
            doc.year = doc.year or _safe_int(s2_data.get("year"))
            doc.journal = doc.journal or str(s2_data.get("venue") or "")
            if not doc.authors:
                doc.authors = [
                    str(author.get("name") or "")
                    for author in (s2_data.get("authors") or [])
                    if str(author.get("name") or "")
                ]
            external_ids = s2_data.get("externalIds") or {}
            if not doc.arxiv_id:
                doc.arxiv_id = str(external_ids.get("ArXiv") or external_ids.get("ARXIV") or "")
            open_access_pdf = (s2_data.get("openAccessPdf") or {}).get("url")
            if open_access_pdf:
                doc.metadata["pdf_url"] = open_access_pdf
            s2_abstract = (s2_data.get("abstract") or "").strip()
            if s2_abstract:
                doc.abstract = s2_abstract or doc.abstract
                doc.full_text = s2_abstract
                doc.sections = [{"name": "abstract", "text": s2_abstract}]
                doc.acquisition_kind = "s2_abstract"
                doc.degraded = True
                doc.provider = "semantic_scholar"
                doc.checksum = hashlib.sha256(s2_abstract.encode()).hexdigest()[:16]
                logger.info(
                    "Degraded to S2 abstract for paper_id=%s doi=%s",
                    paper_id, doi,
                )
                self._append_audit(
                    doc,
                    LiteratureAcquisitionAudit(
                        acquisition_source=AcqStep.S2_FULLTEXT.value,
                        acquisition_method="abstract_fallback",
                        acquisition_status="success",
                        access_tier="metadata_only",
                        is_open_access=None,
                        license_status="unknown",
                    ),
                )
                doc.metadata["budget"] = budget.to_dict()
                return self._finalize_document(
                    doc,
                    phase_separation_kind="semantic_scholar_abstract",
                    degradation_flags=["semantic_scholar_abstract_only", *self._provider_degradation_flags(doc)],
                    raw_content=s2_abstract,
                )

        # Step 7-9: publisher HTML, PDF, OCR remain future extensions

        # Step 10: Abstract-only degraded
        doc.acquisition_kind = "abstract_only"
        doc.degraded = True
        doc.provider = "abstract_fallback"
        fallback_text = doc.abstract or abstract
        if fallback_text:
            doc.sections = [{"name": "abstract", "text": fallback_text}]
            doc.full_text = fallback_text
        doc.checksum = hashlib.sha256(doc.full_text.encode()).hexdigest()[:16]
        self._append_audit(
            doc,
            LiteratureAcquisitionAudit(
                acquisition_source=AcqStep.ABSTRACT_ONLY.value,
                acquisition_method="abstract_only_degradation",
                acquisition_status="success",
                access_tier="metadata_only",
                is_open_access=None,
                license_status="unknown",
            ),
        )
        doc.metadata["budget"] = budget.to_dict()
        return self._finalize_document(
            doc,
            phase_separation_kind="abstract_only_fallback",
            degradation_flags=["abstract_only", *self._provider_degradation_flags(doc)],
            raw_content=doc.full_text or fallback_text,
        )

    async def acquire_batch(
        self,
        *,
        query: str = "",
        paper_ids: Optional[List[str]] = None,
        max_papers: int = 25,
        start_offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Acquire a batch of papers — search + acquire each."""
        search_results: List[Dict[str, Any]]
        if paper_ids:
            exact_ids = paper_ids[start_offset:start_offset + max_papers]
            search_results = [self._seed_record_from_paper_id(paper_id) for paper_id in exact_ids]
        else:
            # Use Europe PMC search to find papers
            search_results = await self._epmc.search(query, max_results=max_papers)
            search_results = search_results[start_offset:]

        results: List[Dict[str, Any]] = []
        for sr in search_results:
            doc = await self.acquire_single(
                paper_id=sr.get("paper_id", ""),
                doi=sr.get("doi", ""),
                pmid=sr.get("pmid", ""),
                pmcid=sr.get("pmcid", ""),
                title=sr.get("title", ""),
                abstract=sr.get("abstract", ""),
                metadata=sr,
            )
            results.append({
                "paper_id": doc.paper_id or sr.get("paper_id", ""),
                "doi": doc.doi,
                "pmid": doc.pmid,
                "pmcid": doc.pmcid,
                "arxiv_id": doc.arxiv_id,
                "title": doc.title,
                "abstract": doc.abstract,
                "authors": list(doc.authors),
                "year": doc.year,
                "journal": doc.journal,
                "pdf_url": str(doc.metadata.get("pdf_url", "") or ""),
                "provider": doc.provider,
                "kind": doc.acquisition_kind,
                "full_text_status": "acquired" if not doc.degraded else "degraded",
                "content_uri": doc.content_uri,
                "normalized_text_uri": doc.normalized_text_uri,
                "section_json_uri": doc.section_json_uri,
                "citations_json_uri": doc.citations_json_uri,
                "sections": list(doc.sections),
                "citations": list(doc.citations),
                "sections_count": len(doc.sections),
                "degradation_flags": list(doc.metadata.get("degradation_flags", [])),
                "graph_worthiness_score": doc.graph_worthiness_score,
                "persistence_eligible": doc.persistence_eligible,
                "persistence_reason": doc.persistence_reason,
                "phase_separation_kind": doc.metadata.get("phase_separation", {}).get("kind", ""),
                "phase_separation": dict(doc.metadata.get("phase_separation", {})),
            })
        return results

    def _seed_record_from_paper_id(self, paper_id: str) -> Dict[str, Any]:
        identifier = (paper_id or "").strip()
        seed = {
            "paper_id": identifier,
            "doi": "",
            "pmid": "",
            "pmcid": "",
            "title": "",
            "abstract": "",
        }
        if not identifier:
            return seed

        lowered = identifier.lower()
        if lowered.startswith("pmcid:"):
            seed["pmcid"] = identifier.split(":", 1)[1].strip().upper()
        elif lowered.startswith("pmid:"):
            seed["pmid"] = identifier.split(":", 1)[1].strip()
        elif lowered.startswith("doi:"):
            seed["doi"] = identifier.split(":", 1)[1].strip()
        elif lowered.startswith("arxiv:"):
            seed["arxiv_id"] = identifier.split(":", 1)[1].strip()
        elif re.fullmatch(r"PMC\d+", identifier, re.IGNORECASE):
            seed["pmcid"] = identifier.upper()
        elif re.fullmatch(r"\d+", identifier):
            seed["pmid"] = identifier
        elif re.match(r"^10\.\S+", identifier):
            seed["doi"] = identifier
        else:
            seed["external_id"] = identifier
        return seed

    def _build_from_xml(
        self, doc: NormalizedDocument, xml: str, provider: str
    ) -> NormalizedDocument:
        doc.provider = provider
        doc.acquisition_kind = "jats_xml"
        doc.degraded = False
        doc.sections = self._sectionizer.from_jats_xml(xml)
        doc.citations = self._citation_extractor.extract(xml)
        if not doc.abstract:
            for section in doc.sections:
                if section.get("name") == "abstract":
                    doc.abstract = section.get("text", "")
                    break
        doc.full_text = "\n\n".join(s.get("text", "") for s in doc.sections)
        doc.checksum = hashlib.sha256(doc.full_text.encode()).hexdigest()[:16]
        return self._finalize_document(doc, phase_separation_kind="full_text_retention", raw_content=xml)

    # ------------------------------------------------------------------
    # Unified acquisition dispatch — Slice-1 contract entry point
    # ------------------------------------------------------------------

    async def execute(
        self,
        request: "FullTextAcquisitionRequest",
    ) -> "FullTextAcquisitionResult":
        """Dispatch a ``FullTextAcquisitionRequest`` and return a ``FullTextAcquisitionResult``.

        Both single and batch modes share the same result envelope.  Lineage
        fields (session_id, run_id, user_id, tenant_id, budget) are threaded
        into every ``acquire_single`` call so traceability is never lost
        regardless of mode.

        This method is additive — it delegates entirely to the existing
        ``acquire_single`` / ``_epmc.search`` machinery and does not change
        any defaults on the public HTTP boundary.
        """
        # Import here to keep the module import graph clean and avoid circular deps.
        from mica.literature_consolidation.contracts.fulltext_acquisition import (
            DegradationEntry,
            FullTextAcquisitionRequest,
            FullTextAcquisitionResult,
        )

        # Build the lineage dict that will be merged into every acquire_single metadata.
        lineage: Dict[str, Any] = {
            "session_id": request.session_id,
            "run_id": request.run_id,
            "user_id": request.user_id,
            "tenant_id": request.tenant_id,
            "allow_paid_fulltext": request.allow_paid_fulltext,
            "require_cloud_evidence": request.require_cloud_evidence,
            "budget_spent_usd": request.budget_spent_usd,
        }
        if request.acquisition_budget_usd is not None:
            lineage["acquisition_budget_usd"] = request.acquisition_budget_usd

        acquired_docs: List[NormalizedDocument] = []

        if request.mode == "single":
            ref = request.paper_refs[0] if request.paper_refs else None
            if ref is not None:
                meta: Dict[str, Any] = {**ref.metadata, **lineage}
                doc = await self.acquire_single(
                    paper_id=ref.paper_id,
                    doi=ref.doi,
                    pmid=ref.pmid,
                    pmcid=ref.pmcid,
                    title=ref.title,
                    abstract=ref.abstract,
                    metadata=meta,
                )
            else:
                # No paper_refs supplied — acquire with lineage only (abstract-only degradation).
                doc = await self.acquire_single(metadata=lineage)
            acquired_docs = [doc]

        else:  # batch
            seeds: List[Dict[str, Any]]
            if request.paper_refs:
                seeds = [ref.model_dump() for ref in request.paper_refs]
            elif request.query:
                raw = await self._epmc.search(
                    request.query,
                    max_results=request.max_items + request.start_offset,
                )
                seeds = list(raw)[request.start_offset:]
            else:
                seeds = []

            for seed in seeds[: request.max_items]:
                # Merge per-seed fields with lineage; lineage wins on conflicts.
                seed_meta: Dict[str, Any] = {
                    **seed.get("metadata", {}),
                    **{k: v for k, v in seed.items() if k != "metadata"},
                    **lineage,
                }
                doc = await self.acquire_single(
                    paper_id=seed.get("paper_id", ""),
                    doi=seed.get("doi", ""),
                    pmid=seed.get("pmid", ""),
                    pmcid=seed.get("pmcid", ""),
                    title=seed.get("title", ""),
                    abstract=seed.get("abstract", ""),
                    metadata=seed_meta,
                )
                acquired_docs.append(doc)

        # Build the unified result envelope.
        degradation_summary = [
            DegradationEntry(
                paper_id=doc.paper_id,
                flags=list(doc.metadata.get("degradation_flags", [])),
                acquisition_kind=doc.acquisition_kind,
                provider=doc.provider,
            )
            for doc in acquired_docs
            if doc.degraded
        ]

        # Extract budget_snapshot and provider_controls from the last document.
        budget_snapshot: Dict[str, Any] = {}
        provider_controls: Dict[str, Any] = {}
        if acquired_docs:
            budget_snapshot = dict(acquired_docs[-1].metadata.get("budget", {}))
            provider_controls = dict(acquired_docs[-1].metadata.get("provider_controls", {}))

        audit_summary: List[Dict[str, Any]] = []
        for doc in acquired_docs:
            for entry in (doc.acquisition_audit or []):
                if isinstance(entry, dict):
                    audit_summary.append(entry)
                else:
                    try:
                        audit_summary.append(vars(entry))
                    except TypeError:
                        audit_summary.append({"raw": str(entry)})

        return FullTextAcquisitionResult(
            mode=request.mode,
            documents=[doc.to_dict() for doc in acquired_docs],
            requested_count=len(acquired_docs),
            acquired_count=sum(1 for doc in acquired_docs if not doc.degraded),
            degraded_count=len(degradation_summary),
            degradation_summary=degradation_summary,
            budget_snapshot=budget_snapshot,
            provider_controls=provider_controls,
            audit_summary=audit_summary,
        )
