"""Canonical query protocol contracts for the literature consolidation lane.

These contracts unify the query-protocol surface across all ingress points
(API router, WebSocket bridge, agentic driver, worker handlers) so that:

  - Every search/ingest invocation is described by exactly one ``LiteratureQuerySpec``.
  - Every result envelope carries ``query_spec_hash``, ``protocol_version``, and
    ``run_id`` for forensic reconstruction.
  - Ingress adapters (``from_ingest_request``, ``from_deep_research_request``,
    ``from_driver_payload``) normalize caller-specific shapes into one contract
    without touching the caller surface.

Invariants
----------
- ``query`` is always non-empty after normalization.
- ``sources`` defaults to the canonical primary providers if not specified.
- ``query_spec_hash`` is a stable SHA-256 hex digest over the deterministic
  JSON representation of the spec's query/sources/max_papers triple.
- ``protocol_version`` is bumped only when the spec shape changes in a
  backwards-incompatible way.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field, model_validator


PROTOCOL_VERSION = "1.0"

_DEFAULT_SOURCES = ("semantic_scholar", "pubmed", "openalex")


def _compute_spec_hash(query: str, sources: Sequence[str], max_papers: int) -> str:
    """Stable SHA-256 hex digest over the deterministic triple."""
    payload = json.dumps(
        {"query": query, "sources": sorted(sources), "max_papers": max_papers},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class LiteratureQuerySpec(BaseModel):
    """Canonical internal contract for a literature search/ingest invocation.

    All ingress surfaces must normalize their caller-specific payloads into
    this contract before dispatching to ``LiteratureSearchService`` or any
    downstream acquisition/governance lane.

    Factory class methods:
      - ``from_ingest_request`` — normalize ``LiteratureIngestExecutionRequest``
      - ``from_deep_research_request`` — normalize ``DeepResearchExecutionRequest``
      - ``from_driver_payload`` — normalize raw driver/WS tool payload dict
    """

    # Core search intent.
    query: str = Field(..., min_length=1, description="Primary search query — never empty.")
    entities: List[str] = Field(default_factory=list, description="Supplemental entity terms.")
    max_papers: int = Field(50, ge=1, le=10000)
    sources: List[str] = Field(
        default_factory=lambda: list(_DEFAULT_SOURCES),
        description="Ordered provider list; defaults to semantic_scholar, pubmed, and openalex. Request biorxiv explicitly when recent preprints are needed.",
    )

    # Mode
    lane: Literal["ingest", "deep_research", "bibliotecario", "driver_search", "general"] = "general"
    citation_depth: int = Field(0, ge=0, le=3)

    # Acquisition controls.
    download_pdfs: bool = False
    extract_full_text: bool = True  # fulltext-first default
    allow_paid_fulltext: bool = False
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0)

    # Filters (source-neutral).
    filters: Optional[Dict[str, Any]] = None

    # Optional metadata only; never required for lexical seeding.
    uniprot_id: Optional[str] = Field(
        None,
        description="UniProt accession is treated as optional metadata, never as a mandatory lexical seed.",
    )
    accessions: Optional[List[str]] = Field(
        None,
        description="UniProt accessions are optional metadata hints, never mandatory lexical seeds.",
    )

    # Lineage — threaded into every execution invocation.
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None

    # Protocol observability fields — populated by validator, not by callers.
    protocol_version: str = Field(PROTOCOL_VERSION, frozen=True)
    query_spec_hash: str = Field("", description="SHA-256 hex over query/sources/max_papers — set by validator.")

    @model_validator(mode="after")
    def _populate_hash(self) -> "LiteratureQuerySpec":
        if not self.query_spec_hash:
            self.query_spec_hash = _compute_spec_hash(self.query, self.sources, self.max_papers)
        return self

    # ------------------------------------------------------------------
    # Ingress adapters
    # ------------------------------------------------------------------

    @classmethod
    def from_ingest_request(
        cls,
        request: Any,
        *,
        user_id: Optional[str] = None,
    ) -> "LiteratureQuerySpec":
        """Normalize a ``LiteratureIngestExecutionRequest`` into a QuerySpec.

        ``user_id`` is accepted as an explicit override because ingest callers
        often receive it as a separate route parameter.
        """
        filters = None
        if hasattr(request, "canonical_filters"):
            filters = request.canonical_filters
        elif hasattr(request, "filters"):
            filters = request.filters

        sources_raw = getattr(request, "sources", None)
        sources = list(sources_raw) if sources_raw else list(_DEFAULT_SOURCES)

        return cls(
            query=str(request.query),
            max_papers=int(getattr(request, "max_papers", 50)),
            sources=sources,
            lane="ingest",
            download_pdfs=bool(getattr(request, "download_pdfs", True)),
            extract_full_text=bool(getattr(request, "extract_full_text", True)),
            allow_paid_fulltext=bool(getattr(request, "allow_paid_fulltext", False)),
            acquisition_budget_usd=getattr(request, "acquisition_budget_usd", None),
            uniprot_id=getattr(request, "uniprot_id", None),
            accessions=list(getattr(request, "accessions", []) or []) or None,
            filters=filters,
            session_id=str(getattr(request, "session_id", "") or ""),
            run_id=str(getattr(request, "run_id", "") or ""),
            user_id=str(user_id or getattr(request, "user_id", "") or ""),
            tenant_id=str(getattr(request, "tenant_id", "") or ""),
        )

    @classmethod
    def from_deep_research_request(
        cls,
        request: Any,
        *,
        tenant_id: Optional[str] = None,
    ) -> "LiteratureQuerySpec":
        """Normalize a ``DeepResearchExecutionRequest`` into a QuerySpec."""
        sources_raw = getattr(request, "sources", None)
        sources = list(sources_raw) if sources_raw else list(_DEFAULT_SOURCES)

        return cls(
            query=str(request.query),
            entities=list(getattr(request, "entities", []) or []),
            max_papers=int(getattr(request, "max_papers", 500)),
            sources=sources,
            lane="deep_research",
            citation_depth=int(getattr(request, "citation_depth", 1)),
            download_pdfs=bool(getattr(request, "download_pdfs", False)),
            extract_full_text=True,
            acquisition_budget_usd=getattr(request, "acquisition_budget_usd", None),
            uniprot_id=getattr(request, "uniprot_id", None),
            accessions=list(getattr(request, "accessions", []) or []) or None,
            session_id=str(getattr(request, "session_id", "") or ""),
            user_id=str(getattr(request, "user_id", "") or ""),
            tenant_id=str(tenant_id or ""),
        )

    @classmethod
    def from_driver_payload(cls, payload: Dict[str, Any]) -> "LiteratureQuerySpec":
        """Normalize a raw driver/WS tool payload dict into a QuerySpec.

        Handles both ``query`` and ``query_text`` as primary query keys.
        Accepts ``entities`` as a list or as a space-separated string.
        """
        query = str(payload.get("query") or payload.get("query_text") or "").strip()
        if not query:
            raise ValueError("Driver payload must contain a non-empty 'query' or 'query_text' field.")

        entities_raw = payload.get("entities") or payload.get("extra_queries") or []
        if isinstance(entities_raw, str):
            entities = [e.strip() for e in entities_raw.split() if e.strip()]
        else:
            entities = [str(e) for e in (entities_raw or [])]

        sources_raw = payload.get("sources") or []
        sources = list(sources_raw) if sources_raw else list(_DEFAULT_SOURCES)

        return cls(
            query=query,
            entities=entities,
            max_papers=int(payload.get("max_papers") or payload.get("max_results") or 50),
            sources=sources,
            lane="driver_search",
            citation_depth=int(payload.get("citation_depth") or 0),
            session_id=str(payload.get("session_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            tenant_id=str(payload.get("tenant_id") or ""),
            acquisition_budget_usd=payload.get("acquisition_budget_usd"),
            uniprot_id=payload.get("uniprot_id"),
            accessions=list(payload.get("accessions") or []) or None,
        )


class LiteratureQueryResult(BaseModel):
    """Minimal shared result envelope for any literature search/ingest invocation.

    Every result lane emits this envelope so protocol traceability fields
    (``query_spec_hash``, ``protocol_version``, ``run_id``) are present
    regardless of which execution path produced the result.

    Lane-specific extras (paper lists, bundles, timelines) are kept in the
    ``payload`` field so callers retain full access without schema breakage.
    """

    query_spec_hash: str
    protocol_version: str = PROTOCOL_VERSION
    run_id: Optional[str] = None
    query: str
    lane: str
    sources_attempted: List[str] = Field(default_factory=list)
    sources_failed: List[str] = Field(default_factory=list)
    paper_count: int = 0
    degraded_count: int = 0
    search_log: List[str] = Field(default_factory=list)
    failure_records: List[Dict[str, Any]] = Field(default_factory=list)

    # Lane-specific result data goes here.
    payload: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_spec_and_payload(
        cls,
        spec: LiteratureQuerySpec,
        payload: Dict[str, Any],
        *,
        sources_attempted: Optional[List[str]] = None,
        sources_failed: Optional[List[str]] = None,
        paper_count: int = 0,
        degraded_count: int = 0,
        search_log: Optional[List[str]] = None,
        failure_records: Optional[List[Dict[str, Any]]] = None,
    ) -> "LiteratureQueryResult":
        """Build a ``LiteratureQueryResult`` from a ``LiteratureQuerySpec`` and a payload dict."""
        return cls(
            query_spec_hash=spec.query_spec_hash,
            protocol_version=spec.protocol_version,
            run_id=spec.run_id or None,
            query=spec.query,
            lane=spec.lane,
            sources_attempted=list(sources_attempted or []),
            sources_failed=list(sources_failed or []),
            paper_count=paper_count,
            degraded_count=degraded_count,
            search_log=list(search_log or []),
            failure_records=list(failure_records or []),
            payload=payload,
        )
