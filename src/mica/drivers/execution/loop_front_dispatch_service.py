"""Front dispatch helpers extracted from AgenticDriver loop executor."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from .feed_tools_service import run_feed_tool_branch
from .literature_search_service import run_literature_search_branch
from .protein_lookup_service import run_protein_lookup_branch
from .repo_ide_service import run_repo_ide_branch
from .websearch_tool_service import run_web_search_branch

LITERATURE_DISPATCH_TOOL_NAMES: frozenset[str] = frozenset({
    "search_literature",
    "run_dlm_scan",
    "run_bibliotecario_scan",
    "analyse_knowledge_decay",
    "analyse_citation_impact",
    "track_entity_evolution",
    "query_co_occurrence",
})

PROTEIN_LOOKUP_TOOL_NAMES: frozenset[str] = frozenset({
    "search_protein",
    "search_protein_metadata",
    "advanced_protein_search",
})


async def run_loop_front_branch(
    *,
    name: str,
    call_id: str,
    args: Dict[str, Any],
    pending: Any,
    invoke_feed_tool_fn: Callable[..., Awaitable[Any]],
    feed_tool_names: set[str] | frozenset[str],
    repo_ide_tool_names: set[str] | frozenset[str],
    backend_native_tool_names: set[str] | frozenset[str],
    get_backend_native_executor_fn: Callable[[], Awaitable[Callable[[str, str, Dict[str, Any]], Awaitable[str]]]],
    shorten_query_fn: Callable[..., str],
    search_literature_result_fn: Callable[..., Awaitable[Any]],
    driver_literature_sources: list[str],
    uniprot_search_fn: Callable[..., Awaitable[Any]],
) -> Optional[str]:
    """Handle the early dispatch ladder from ``_build_loop_executor``.

    Returns ``None`` when the tool is not handled by the front router so the
    caller can continue through the remaining dispatch layers.
    """

    if name in feed_tool_names:
        return await run_feed_tool_branch(
            name=name,
            args=args,
            invoke_feed_tool_fn=invoke_feed_tool_fn,
        )

    if name == "web_search":
        return await run_web_search_branch(
            name=name,
            args=args,
        )

    if name in repo_ide_tool_names:
        return await run_repo_ide_branch(
            name=name,
            args=args,
        )

    if name in LITERATURE_DISPATCH_TOOL_NAMES:
        return await run_literature_search_branch(
            name=name,
            args=args,
            shorten_query_fn=shorten_query_fn,
            search_literature_result_fn=search_literature_result_fn,
            driver_literature_sources=driver_literature_sources,
            pending=pending,
        )

    if name in PROTEIN_LOOKUP_TOOL_NAMES:
        return await run_protein_lookup_branch(
            args=args,
            shorten_query_fn=lambda query: shorten_query_fn(query, max_words=5),
            uniprot_search_fn=uniprot_search_fn,
        )

    if name in backend_native_tool_names:
        backend_executor = await get_backend_native_executor_fn()
        return await backend_executor(name, call_id, args)

    return None