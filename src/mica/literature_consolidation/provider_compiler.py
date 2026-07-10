from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from mica.literature_consolidation.contracts.query_protocol import LiteratureQuerySpec


class LiteratureSource(str, Enum):
    SEMANTIC_SCHOLAR = "semantic_scholar"
    PUBMED = "pubmed"
    OPENALEX = "openalex"
    BIORXIV = "biorxiv"
    ARXIV = "arxiv"


_ALLOWED_SOURCES = {source.value for source in LiteratureSource}
_DEFAULT_SOURCES_BY_LANE: Dict[str, Sequence[str]] = {
    "general": ("semantic_scholar", "pubmed", "openalex"),
    "driver_search": ("semantic_scholar", "pubmed", "openalex"),
    "deep_research": ("semantic_scholar", "pubmed", "openalex"),
    "bibliotecario_review": ("semantic_scholar", "pubmed", "openalex"),
    "bibliotecario": ("semantic_scholar", "pubmed", "openalex"),
    "research_orchestrator": ("semantic_scholar", "pubmed", "openalex"),
    "entity_scan": ("semantic_scholar",),
}


@dataclass(frozen=True)
class ProviderExecutionPlan:
    query: str
    max_papers: int
    quota_resolution_strategy: str
    acquisition_order: List[LiteratureSource]
    provider_flags: Dict[str, Any] = field(default_factory=dict)
    requested_sources: List[LiteratureSource] = field(default_factory=list)
    degraded_sources: List[Dict[str, str]] = field(default_factory=list)
    extra_queries: List[str] = field(default_factory=list)
    lane_class: str = "general"
    preset_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "max_papers": self.max_papers,
            "sources": [item.value for item in self.acquisition_order],
            "requested_sources": [item.value for item in self.requested_sources],
            "degraded_sources": list(self.degraded_sources),
            "extra_queries": list(self.extra_queries),
            "lane_class": self.lane_class,
            "preset_name": self.preset_name,
            "acquisition_order": [item.value for item in self.acquisition_order],
            "policy": dict(self.provider_flags),
            "quota_resolution_strategy": self.quota_resolution_strategy,
        }


class LiteratureProviderCompiler:
    """Compile a typed provider execution plan from a canonical QuerySpec."""

    def __init__(
        self,
        *,
        lane_class: str = "general",
        preset_name: str = "",
        negative_memory_context: Optional[Dict[str, Any]] = None,
        openalex_available: bool = True,
    ) -> None:
        self.lane_class = str(lane_class or "general").strip().lower() or "general"
        self.preset_name = str(preset_name or "").strip()
        self.negative_memory_context = dict(negative_memory_context or {})
        self.openalex_available = bool(openalex_available)

    def compile_plan(self, query_spec: LiteratureQuerySpec) -> ProviderExecutionPlan:
        query = str(query_spec.query or "").strip()
        if not query:
            raise ValueError("query must be non-empty")

        summary = dict(self.negative_memory_context.get("negative_memory_summary") or {})
        mode = str(
            self.negative_memory_context.get("negative_memory_mode")
            or summary.get("negative_memory_mode")
            or "full"
        ).strip() or "full"

        explicit_extra_queries = self._dedupe_texts(query_spec.entities or [])
        appeal_state = dict(self.negative_memory_context.get("appeal_regime_state") or {})
        soft_repulsion_warnings = [
            warning
            for warning in list(self.negative_memory_context.get("soft_repulsion_warnings") or [])
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
        appeal_candidates = self._dedupe_texts(appeal_candidates)

        max_papers = max(1, int(query_spec.max_papers or 1))
        extra_queries = list(explicit_extra_queries)
        if mode == "full":
            if appeal_state.get("appeal_regime_active") and appeal_candidates:
                extra_queries = self._dedupe_texts(list(explicit_extra_queries) + appeal_candidates[:3])
        elif mode == "semi_blind":
            max_papers = max(5, min(max_papers, int(round(max_papers * 0.75))))
        else:
            extra_queries = []
            max_papers = max(5, min(max_papers, int(round(max_papers * 0.5))))

        requested_raw = self._dedupe_texts(
            query_spec.sources or self._default_sources_for_lane(self.lane_class)
        )

        requested_sources: List[LiteratureSource] = []
        effective_sources: List[LiteratureSource] = []
        degraded_sources: List[Dict[str, str]] = []

        for source_name in requested_raw:
            normalized = str(source_name or "").strip().lower()
            if normalized not in _ALLOWED_SOURCES:
                degraded_sources.append(
                    {
                        "source": normalized,
                        "reason": "UNSUPPORTED_SOURCE",
                        "detail": "Source is not part of the canonical literature control plane.",
                    }
                )
                continue

            source = LiteratureSource(normalized)
            requested_sources.append(source)

            if source is LiteratureSource.OPENALEX and not self.openalex_available:
                degraded_sources.append(
                    {
                        "source": source.value,
                        "reason": "SOURCE_UNAVAILABLE",
                        "detail": "OpenAlex is disabled for this runtime lane by policy, not by client capability.",
                    }
                )
                continue

            if mode == "semi_blind" and source not in {
                LiteratureSource.SEMANTIC_SCHOLAR,
                LiteratureSource.PUBMED,
            }:
                degraded_sources.append(
                    {
                        "source": source.value,
                        "reason": "POLICY_EXCLUDED",
                        "detail": "Excluded by negative memory policy or lane constraints.",
                    }
                )
                continue

            if mode == "blind" and source not in {
                LiteratureSource.SEMANTIC_SCHOLAR,
                LiteratureSource.PUBMED,
            }:
                degraded_sources.append(
                    {
                        "source": source.value,
                        "reason": "POLICY_EXCLUDED",
                        "detail": "Excluded by negative memory policy or lane constraints.",
                    }
                )
                continue

            if source not in effective_sources:
                effective_sources.append(source)

        if not effective_sources:
            fallback = [
                LiteratureSource.SEMANTIC_SCHOLAR,
                LiteratureSource.PUBMED,
            ] if mode in {"semi_blind", "blind"} else [LiteratureSource.SEMANTIC_SCHOLAR]
            effective_sources = list(fallback)

        provider_flags = {
            "negative_memory_mode": mode,
            "appeal_regime_active": bool(appeal_state.get("appeal_regime_active")),
            "soft_repulsion_warning_count": len(soft_repulsion_warnings),
            "openalex_available": self.openalex_available,
            "source_hash": str(query_spec.query_spec_hash or ""),
        }

        return ProviderExecutionPlan(
            query=query,
            max_papers=max_papers,
            quota_resolution_strategy="lane_default_rate_limits",
            acquisition_order=effective_sources,
            provider_flags=provider_flags,
            requested_sources=requested_sources,
            degraded_sources=degraded_sources,
            extra_queries=extra_queries,
            lane_class=self.lane_class,
            preset_name=self.preset_name,
        )

    @staticmethod
    def _dedupe_texts(values: Sequence[str] | None) -> List[str]:
        ordered: List[str] = []
        seen: set[str] = set()
        for raw in values or []:
            value = str(raw or "").strip()
            if not value:
                continue
            folded = value.lower()
            if folded in seen:
                continue
            seen.add(folded)
            ordered.append(value)
        return ordered

    @staticmethod
    def _default_sources_for_lane(lane_class: str) -> List[str]:
        configured = _DEFAULT_SOURCES_BY_LANE.get(lane_class)
        if configured is None:
            configured = _DEFAULT_SOURCES_BY_LANE["general"]
        return list(configured)
