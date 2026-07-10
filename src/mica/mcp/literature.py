"""Literature search helpers emulating MCP tool behaviour.

These utilities provide lightweight wrappers around public APIs for PubMed,
arXiv, and Semantic Scholar so agents inside the platform can perform
research-grade lookups without depending on the external MCP runtime.  When
network access or dependencies are unavailable the helpers degrade gracefully
and return informative errors that upstream agents can surface to the user.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import json
import time

try:
    import requests
except ImportError:  # pragma: no cover - optional dependency for offline runs
    requests = None  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class LiteratureQueryResult:
    """Normalised representation of a literature search hit."""

    source: str
    title: str
    url: str
    summary: Optional[str] = None
    metadata: Optional[Dict[str, object]] = None

    def model_dump(self) -> Dict[str, object]:  # compatibility helper
        payload = {
            "source": self.source,
            "title": self.title,
            "url": self.url,
        }
        if self.summary:
            payload["summary"] = self.summary
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


class LiteratureSearchService:
    """Aggregate access to PubMed, arXiv, and Semantic Scholar APIs."""

    def __init__(self, session: Optional["requests.Session"] = None, timeout: float = 10.0):
        if requests is None:
            raise RuntimeError("requests package is required for literature queries")
        self.session = session or requests.Session()
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    def search_pubmed(self, query: str, max_results: int = 5) -> List[LiteratureQueryResult]:
        """Return PubMed results using the eSearch and eSummary endpoints."""

        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

        params = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": max_results,
        }

        try:
            response = self.session.get(search_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            id_list = payload.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return []

            summary_params = {
                "db": "pubmed",
                "id": ",".join(id_list),
                "retmode": "json",
            }
            summary_resp = self.session.get(summary_url, params=summary_params, timeout=self.timeout)
            summary_resp.raise_for_status()
            summary_payload = summary_resp.json()
            result_map = summary_payload.get("result", {})
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("PubMed query failed: %s", exc)
            return [
                LiteratureQueryResult(
                    source="pubmed",
                    title="PubMed lookup failed",
                    url="https://pubmed.ncbi.nlm.nih.gov/",
                    summary=str(exc),
                )
            ]

        results: List[LiteratureQueryResult] = []
        for identifier in id_list:
            record = result_map.get(identifier)
            if not record:
                continue
            title = record.get("title", "Untitled PubMed record")
            url = f"https://pubmed.ncbi.nlm.nih.gov/{identifier}/"
            summary = record.get("sorttitle") or record.get("elocationid")
            metadata = {
                "authors": record.get("authors", []),
                "pubdate": record.get("pubdate"),
                "journal": record.get("fulljournalname"),
            }
            results.append(
                LiteratureQueryResult(
                    source="pubmed",
                    title=title,
                    url=url,
                    summary=summary,
                    metadata=metadata,
                )
            )
        return results

    def search_arxiv(self, query: str, max_results: int = 5) -> List[LiteratureQueryResult]:
        """Return arXiv results using the public Atom feed."""

        feed_url = "https://export.arxiv.org/api/query"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
        }

        try:
            response = self.session.get(feed_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            import xml.etree.ElementTree as ET

            root = ET.fromstring(response.text)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("arXiv query failed: %s", exc)
            return [
                LiteratureQueryResult(
                    source="arxiv",
                    title="arXiv lookup failed",
                    url="https://arxiv.org/",
                    summary=str(exc),
                )
            ]

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        results: List[LiteratureQueryResult] = []
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="Untitled", namespaces=ns) or "Untitled").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            link = entry.find("atom:link[@rel='alternate']", ns)
            href = link.attrib.get("href") if link is not None else "https://arxiv.org/"
            published = entry.findtext("atom:published", default="", namespaces=ns)
            authors = [
                author.findtext("atom:name", default="", namespaces=ns)
                for author in entry.findall("atom:author", ns)
            ]
            results.append(
                LiteratureQueryResult(
                    source="arxiv",
                    title=title,
                    url=href,
                    summary=summary,
                    metadata={"published": published, "authors": authors},
                )
            )
        return results

    def search_semantic_scholar(self, query: str, max_results: int = 5) -> List[LiteratureQueryResult]:
        """Return Semantic Scholar results using the public Graph API."""

        api_url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": max_results,
            "fields": "title,url,abstract,authors,year,venue",
        }

        try:
            response = self.session.get(api_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", [])
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Semantic Scholar query failed: %s", exc)
            return [
                LiteratureQueryResult(
                    source="semantic_scholar",
                    title="Semantic Scholar lookup failed",
                    url="https://www.semanticscholar.org/",
                    summary=str(exc),
                )
            ]

        results: List[LiteratureQueryResult] = []
        for item in data:
            title = item.get("title") or "Untitled Semantic Scholar record"
            url = item.get("url") or "https://www.semanticscholar.org/"
            abstract = item.get("abstract")
            metadata = {
                "year": item.get("year"),
                "venue": item.get("venue"),
                "authors": [author.get("name") for author in item.get("authors", [])],
            }
            results.append(
                LiteratureQueryResult(
                    source="semantic_scholar",
                    title=title,
                    url=url,
                    summary=abstract,
                    metadata=metadata,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def research_bundle(self, query: str, max_results: int = 5) -> Dict[str, List[LiteratureQueryResult]]:
        """Return results from all providers with rate-limit friendly sequencing."""

        logger.debug("Running literature bundle for query: %s", query)
        bundle: Dict[str, List[LiteratureQueryResult]] = {}

        bundle["pubmed"] = self.search_pubmed(query, max_results=max_results)
        time.sleep(0.5)
        bundle["arxiv"] = self.search_arxiv(query, max_results=max_results)
        time.sleep(0.5)
        bundle["semantic_scholar"] = self.search_semantic_scholar(query, max_results=max_results)
        return bundle

    def close(self) -> None:
        if self.session:
            self.session.close()


def results_to_dict(results: Dict[str, List[LiteratureQueryResult]]) -> Dict[str, List[Dict[str, object]]]:
    """Convert bundle results into JSON-serialisable structure."""

    serialised: Dict[str, List[Dict[str, object]]] = {}
    for source, records in results.items():
        serialised[source] = [item.model_dump() for item in records]
    return serialised


def save_bundle(results: Dict[str, List[LiteratureQueryResult]], output_path: str) -> None:
    """Persist research bundle to disk as JSON."""

    serialised = results_to_dict(results)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(serialised, handle, indent=2)

