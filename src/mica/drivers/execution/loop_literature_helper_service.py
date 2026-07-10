"""Bundle literature/query helper wiring for AgenticDriver loop execution."""

from __future__ import annotations

import os
import re as _re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class LoopLiteratureHelpers:
    shorten_query: Callable[[str, int], str]
    search_literature_records: Callable[..., Awaitable[List[Dict[str, Any]]]]
    search_literature_result: Callable[..., Awaitable[Any]]
    uniprot_search: Callable[[str, int], Awaitable[str]]
    close_literature_service: Callable[[], Awaitable[None]]


def _env_flag(name: str, default: bool) -> bool:
    raw_value = str(os.getenv(name, "") or "").strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "on"}


def build_loop_literature_helpers(
    *,
    resolve_literature_retrieval_policy_fn: Callable[..., Dict[str, Any]],
    atom_memory: Any,
    negative_memory_context: Any,
    active_session_id: str,
    latest_pipeline_outputs: Dict[str, Any],
    driver_literature_sources: Sequence[str],
    degraded_tool_response_fn: Callable[..., str],
) -> LoopLiteratureHelpers:
    literature_service: Any = None
    interactive_fulltext_hydration = _env_flag(
        "MICA_AGENTIC_LITERATURE_ENABLE_FULLTEXT_HYDRATION",
        False,
    )
    interactive_document_scan_enrichment = _env_flag(
        "MICA_AGENTIC_LITERATURE_ENABLE_DOCUMENT_SCAN_GOVERNANCE",
        False,
    )

    def _shorten_query(query: str, max_words: int = 6) -> str:
        compact = _re.sub(r"\([^)]{20,}\)", "", str(query or ""))
        compact = _re.sub(r"[,;]+", " ", compact)
        compact = " ".join(compact.split())
        words = compact.split()
        return " ".join(words[:max_words]) if len(words) > max_words else compact

    async def _get_literature_service() -> Any:
        nonlocal literature_service
        if literature_service is None:
            from mica.services.literature_search_service import LiteratureSearchService

            literature_service = LiteratureSearchService(
                atom_system=atom_memory,
                enable_atom_persistence=atom_memory is not None,
                enable_event_persistence=True,
                persistence_user_id=active_session_id,
                enable_fulltext_hydration=interactive_fulltext_hydration,
                enable_document_scan_enrichment=interactive_document_scan_enrichment,
            )
        return literature_service

    async def _search_literature_records(
        *,
        query: str,
        max_papers: int,
        sources: Optional[Sequence[str]] = None,
        extra_queries: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        service = await _get_literature_service()
        retrieval_policy = resolve_literature_retrieval_policy_fn(
            query=query,
            max_papers=max_papers,
            sources=sources,
            extra_queries=extra_queries,
            negative_memory_context=negative_memory_context,
        )
        result = await service.search(
            query=str(retrieval_policy.get("query") or query),
            max_papers=int(retrieval_policy.get("max_papers") or max_papers),
            sources=list(retrieval_policy.get("sources") or driver_literature_sources),
            extra_queries=list(retrieval_policy.get("extra_queries") or []),
            negative_memory_context=negative_memory_context,
        )
        if getattr(result, "pipeline_output", None) is not None:
            latest_pipeline_outputs[active_session_id] = result.pipeline_output
        return list(result.papers)

    async def _search_literature_result(
        *,
        query: str,
        max_papers: int,
        sources: Optional[Sequence[str]] = None,
        extra_queries: Optional[Sequence[str]] = None,
    ) -> Any:
        service = await _get_literature_service()
        retrieval_policy = resolve_literature_retrieval_policy_fn(
            query=query,
            max_papers=max_papers,
            sources=sources,
            extra_queries=extra_queries,
            negative_memory_context=negative_memory_context,
        )
        return await service.search(
            query=str(retrieval_policy.get("query") or query),
            max_papers=int(retrieval_policy.get("max_papers") or max_papers),
            sources=list(retrieval_policy.get("sources") or driver_literature_sources),
            extra_queries=list(retrieval_policy.get("extra_queries") or []),
            negative_memory_context=negative_memory_context,
        )

    async def _uniprot_search(query: str, max_results: int = 5) -> str:
        from .uniprot_service import run_uniprot_search

        return await run_uniprot_search(
            query=query,
            max_results=max_results,
            shorten_query_fn=lambda text: _shorten_query(text, max_words=5),
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    async def _close_literature_service() -> None:
        nonlocal literature_service
        if literature_service is None:
            return
        close = getattr(literature_service, "close", None)
        try:
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
        finally:
            literature_service = None

    return LoopLiteratureHelpers(
        shorten_query=_shorten_query,
        search_literature_records=_search_literature_records,
        search_literature_result=_search_literature_result,
        uniprot_search=_uniprot_search,
        close_literature_service=_close_literature_service,
    )
