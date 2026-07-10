"""Provider compiler contracts for Iter 07 — Acquisition Authority Split.

This module defines first-class provider compilation contracts that separate
provider selection logic from DLM and put acquisition authority back under
the literature consolidation lane.

Contracts:
  - ``LiteratureSource`` — Enum of supported providers (semantic_scholar, pubmed, openalex, biorxiv)
  - ``ProviderCapability`` — Metadata about provider capabilities
  - ``DegradationReason`` — Why a provider was excluded from a plan
  - ``DegradedProvider`` — Record of a provider that could not be used
  - ``ProviderExecutionPlan`` — First-class output of provider compilation (replaces dict)
  - ``ProviderCompiler`` — Service that compiles QuerySpec → ProviderExecutionPlan

Invariants:
  - ``ProviderExecutionPlan`` is immutable (frozen dataclass).
  - All providers are typed as LiteratureSource, never raw strings.
  - Degradation reasons are explicit and traceable.
  - Provider order is deterministic and policy-driven.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence
from datetime import datetime


class LiteratureSource(str, Enum):
    """Canonical literature provider sources.
    
    All literature acquisition must route through one of these sources.
    Source-neutral ingestion adapters normalize caller-specific naming
    to one of these canonical values.
    """
    SEMANTIC_SCHOLAR = "semantic_scholar"
    PUBMED = "pubmed"
    OPENALEX = "openalex"
    BIORXIV = "biorxiv"
    
    @classmethod
    def from_string(cls, value: str) -> LiteratureSource:
        """Parse a string into a LiteratureSource, raising ValueError if invalid."""
        normalized = str(value or "").strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(
            f"Unknown literature source: {value}. "
            f"Valid sources: {[m.value for m in cls]}"
        )
    
    @classmethod
    def all_sources(cls) -> List[LiteratureSource]:
        """Return all sources in canonical order."""
        return list(cls)


@dataclass(frozen=True)
class ProviderCapability:
    """Metadata about a provider's capabilities and constraints.
    
    Used by ProviderCompiler to make policy-driven routing decisions.
    """
    source: LiteratureSource
    max_results_per_query: int = 1000
    supports_fulltext_search: bool = True
    supports_citation_search: bool = False
    rate_limit_rpm: int = 600  # Requests per minute
    availability: str = "always"  # "always" | "conditional" | "deprecated"
    
    @classmethod
    def canonical_capabilities(cls) -> Dict[LiteratureSource, ProviderCapability]:
        """Return canonical capability metadata for all providers."""
        return {
            LiteratureSource.SEMANTIC_SCHOLAR: cls(
                source=LiteratureSource.SEMANTIC_SCHOLAR,
                max_results_per_query=10000,
                supports_fulltext_search=True,
                supports_citation_search=True,
                rate_limit_rpm=300,
                availability="always",
            ),
            LiteratureSource.PUBMED: cls(
                source=LiteratureSource.PUBMED,
                max_results_per_query=100000,
                supports_fulltext_search=True,
                supports_citation_search=False,
                rate_limit_rpm=600,
                availability="always",
            ),
            LiteratureSource.OPENALEX: cls(
                source=LiteratureSource.OPENALEX,
                max_results_per_query=50000,
                supports_fulltext_search=True,
                supports_citation_search=True,
                rate_limit_rpm=1000,
                availability="always",
            ),
            LiteratureSource.BIORXIV: cls(
                source=LiteratureSource.BIORXIV,
                max_results_per_query=10000,
                supports_fulltext_search=True,
                supports_citation_search=False,
                rate_limit_rpm=500,
                availability="conditional",
            ),
        }


class DegradationReason(str, Enum):
    """Why a provider was excluded from an execution plan."""
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    POLICY_EXCLUDED = "POLICY_EXCLUDED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    NEGATIVE_MEMORY_CONSTRAINT = "NEGATIVE_MEMORY_CONSTRAINT"


@dataclass(frozen=True)
class DegradedProvider:
    """Record of a provider that could not be used in a plan."""
    source: LiteratureSource
    reason: DegradationReason
    detail: str = ""


@dataclass(frozen=True)
class ProviderExecutionPlan:
    """First-class provider compilation output.
    
    Replaces the dictionary returned by the old ``resolve_literature_operation_plan()``.
    This is the contract between ProviderCompiler and downstream consumers (DLM, fulltext_router).
    
    All fields are immutable (frozen).
    """
    
    # Core query intent
    query: str
    extra_queries: List[str] = field(default_factory=list)
    
    # Provider execution order and availability
    effective_sources: List[LiteratureSource] = field(default_factory=list)  # Ordered by priority
    requested_sources: List[LiteratureSource] = field(default_factory=list)
    degraded_providers: List[DegradedProvider] = field(default_factory=list)
    
    # Acquisition parameters
    max_papers: int = 50
    acquisition_order: List[str] = field(
        default_factory=lambda: [
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
        ]
    )
    
    # Governance and policy context
    lane_class: str = "general"  # "general" | "deep_research" | "research_orchestrator" | ...
    preset_name: str = ""
    
    # Negative memory policy
    negative_memory_mode: str = "full"  # "full" | "semi_blind" | "blind"
    appeal_regime_active: bool = False
    
    # Lineage
    source_hash: str = ""  # SHA-256 over query/sources/max_papers (from QuerySpec)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Backward-compat: convert to dict for existing services.
        
        This allows gradual migration of consumers from dict-based plans to typed ProviderExecutionPlan.
        """
        return {
            "query": self.query,
            "extra_queries": self.extra_queries,
            "sources": [s.value for s in self.effective_sources],
            "requested_sources": [s.value for s in self.requested_sources],
            "degraded_sources": [
                {
                    "source": dp.source.value,
                    "reason": dp.reason.value,
                    "detail": dp.detail,
                }
                for dp in self.degraded_providers
            ],
            "max_papers": self.max_papers,
            "acquisition_order": self.acquisition_order,
            "lane_class": self.lane_class,
            "preset_name": self.preset_name,
            "policy": {
                "negative_memory_mode": self.negative_memory_mode,
                "appeal_regime_active": self.appeal_regime_active,
            },
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderExecutionPlan:
        """Reconstruct from a dict (for testing and legacy deserialization)."""
        effective_sources = [
            LiteratureSource.from_string(s) for s in data.get("sources", [])
        ]
        requested_sources = [
            LiteratureSource.from_string(s) for s in data.get("requested_sources", [])
        ]
        degraded_dict = data.get("degraded_sources", [])
        degraded_providers = [
            DegradedProvider(
                source=LiteratureSource.from_string(d["source"]),
                reason=DegradationReason(d["reason"]),
                detail=d.get("detail", ""),
            )
            for d in degraded_dict
        ]
        policy = data.get("policy", {})
        
        return cls(
            query=data["query"],
            extra_queries=data.get("extra_queries", []),
            effective_sources=effective_sources,
            requested_sources=requested_sources,
            degraded_providers=degraded_providers,
            max_papers=data.get("max_papers", 50),
            acquisition_order=data.get("acquisition_order", []),
            lane_class=data.get("lane_class", "general"),
            preset_name=data.get("preset_name", ""),
            negative_memory_mode=policy.get("negative_memory_mode", "full"),
            appeal_regime_active=policy.get("appeal_regime_active", False),
            source_hash=data.get("source_hash", ""),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
        )


class ProviderCompiler:
    """Service that compiles provider execution plans from query intent.
    
    Separates provider selection/compilation logic from DLM so that:
    - Provider routing decisions are centralized and versioned
    - DLM consumes already-compiled plans, not raw provider intent
    - Provider policy (negative memory, degradation, etc.) is applied consistently
    - Addition of new providers is isolated to this service
    
    This replaces the ``resolve_literature_operation_plan()`` function from
    control_plane.py, which was previously scattered across multiple consumers.
    """
    
    def __init__(self):
        """Initialize the provider compiler with canonical provider capabilities."""
        self.capabilities = ProviderCapability.canonical_capabilities()
    
    def compile(
        self,
        query: str,
        *,
        max_papers: int = 50,
        requested_sources: Optional[Sequence[str]] = None,
        extra_queries: Optional[Sequence[str]] = None,
        lane_class: str = "general",
        preset_name: str = "",
        openalex_available: bool = True,
        negative_memory_context: Optional[Dict[str, Any]] = None,
        source_hash: str = "",
    ) -> ProviderExecutionPlan:
        """Compile a provider execution plan from user input and policy context.
        
        Args:
            query: Primary search query
            max_papers: Maximum papers to retrieve
            requested_sources: Ordered list of source preferences (as strings or LiteratureSource)
            extra_queries: Supplementary queries from appeal regime or negative memory
            lane_class: Execution lane (general, deep_research, etc.)
            preset_name: DLM preset name (for audit/lineage)
            openalex_available: Whether to include OpenAlex in defaults
            negative_memory_context: Policy context from negative memory store
            source_hash: SHA-256 hash from QuerySpec (for lineage)
        
        Returns:
            ProviderExecutionPlan with validated sources, degradation records, and execution parameters.
        
        Raises:
            ValueError: If query is empty or requested_sources contains invalid values.
        """
        if not query or not str(query).strip():
            raise ValueError("query must be non-empty")
        
        negative_memory_context = negative_memory_context or {}
        negative_memory_mode = str(
            negative_memory_context.get("negative_memory_mode", "full")
        ).strip() or "full"
        
        # Normalize and validate requested sources
        requested_sources_typed = self._normalize_sources(
            requested_sources, lane_class, openalex_available
        )
        
        # Apply negative memory policy to decide effective sources
        effective_sources = self._apply_policy_degradation(
            requested_sources_typed, negative_memory_mode
        )
        
        degraded = self._compute_degradation(
            requested_sources_typed, effective_sources
        )
        
        # Adjust max_papers based on policy
        effective_max = self._adjust_max_papers(max_papers, negative_memory_mode)
        
        return ProviderExecutionPlan(
            query=str(query).strip(),
            extra_queries=list(extra_queries or []),
            effective_sources=effective_sources,
            requested_sources=requested_sources_typed,
            degraded_providers=degraded,
            max_papers=effective_max,
            lane_class=lane_class,
            preset_name=preset_name,
            negative_memory_mode=negative_memory_mode,
            appeal_regime_active=bool(
                negative_memory_context.get("appeal_regime_active", False)
            ),
            source_hash=source_hash,
        )
    
    def _normalize_sources(
        self,
        requested: Optional[Sequence[str]],
        lane_class: str,
        openalex_available: bool,
    ) -> List[LiteratureSource]:
        """Normalize and validate requested sources."""
        if not requested:
            # Use lane-specific defaults
            return self._default_sources_for_lane(lane_class, openalex_available)
        
        result = []
        for source in requested:
            try:
                result.append(LiteratureSource.from_string(source))
            except ValueError as e:
                raise ValueError(f"Invalid source in requested_sources: {e}")
        
        return result
    
    def _default_sources_for_lane(
        self, lane_class: str, openalex_available: bool
    ) -> List[LiteratureSource]:
        """Return default sources based on lane and availability."""
        if openalex_available:
            return [
                LiteratureSource.SEMANTIC_SCHOLAR,
                LiteratureSource.PUBMED,
                LiteratureSource.OPENALEX,
                LiteratureSource.BIORXIV,
            ]
        else:
            return [
                LiteratureSource.SEMANTIC_SCHOLAR,
                LiteratureSource.PUBMED,
                LiteratureSource.BIORXIV,
            ]
    
    def _apply_policy_degradation(
        self, requested: List[LiteratureSource], mode: str
    ) -> List[LiteratureSource]:
        """Apply negative memory mode to decide effective source set."""
        if mode == "full":
            return requested
        elif mode == "semi_blind":
            # Reduce to indexed sources (faster, less flexible)
            return [s for s in requested if s in {
                LiteratureSource.SEMANTIC_SCHOLAR,
                LiteratureSource.PUBMED,
            }] or [LiteratureSource.SEMANTIC_SCHOLAR, LiteratureSource.PUBMED]
        elif mode == "blind":
            # Conservative: semantic_scholar + pubmed only
            return [LiteratureSource.SEMANTIC_SCHOLAR, LiteratureSource.PUBMED]
        else:
            return requested
    
    def _compute_degradation(
        self,
        requested: List[LiteratureSource],
        effective: List[LiteratureSource],
    ) -> List[DegradedProvider]:
        """Compute list of degraded providers.
        
        Degradation occurs when a provider was REQUESTED but is NOT EFFECTIVE.
        Reason depends on whether the provider was excluded by policy or genuinely unavailable.
        """
        degraded = []
        for source in requested:
            if source not in effective:
                # For now: treat all degradation as policy-driven (negative memory or lane settings)
                # In future: could distinguish between real unavailability vs policy
                reason = DegradationReason.POLICY_EXCLUDED
                detail = "Excluded by negative memory policy or lane constraints"
                
                degraded.append(DegradedProvider(source=source, reason=reason, detail=detail))
        
        return degraded
    
    def _adjust_max_papers(self, requested: int, mode: str) -> int:
        """Adjust max_papers based on negative memory mode."""
        max_val = max(1, int(requested or 50))
        if mode == "semi_blind":
            return max(5, min(max_val, int(round(max_val * 0.75))))
        elif mode == "blind":
            return max(5, min(max_val, int(round(max_val * 0.5))))
        else:
            return max_val
