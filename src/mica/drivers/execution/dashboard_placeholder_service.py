"""Degraded dashboard placeholder helpers extracted from AgenticDriver loop executor."""

from typing import Any, Awaitable, Callable, Dict, List


async def run_dashboard_placeholder_branch(
    *,
    name: str,
    args: Dict[str, Any],
    shorten_query_fn: Callable[[str], str],
    search_literature_records_fn: Callable[..., Awaitable[List[Dict[str, Any]]]],
    driver_literature_sources: List[str],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    if name == "map_conformational_landscape":
        query_text = str(args.get("uniprot_id") or args.get("gene_name") or "").strip()
        label = "target"
    elif name == "scan_pharmacovigilance":
        query_text = str(args.get("entity") or "").strip()
        label = "entity"
    else:
        query_text = str(args.get("gene_name") or "").strip()
        label = "gene_name"

    if not query_text:
        return degraded_tool_response_fn(
            name,
            f"Missing required {label} for {name}.",
            args_payload=args,
        )

    try:
        query = shorten_query_fn(query_text)
        papers = await search_literature_records_fn(
            query=query,
            max_papers=12,
            sources=driver_literature_sources,
        )
        summarized = [
            {
                "title": paper.get("title", ""),
                "year": paper.get("year"),
                "paperId": paper.get("paperId", ""),
            }
            for paper in (papers or [])[:6]
        ]
        return degraded_tool_response_fn(
            name,
            "Returned a resilient literature-backed placeholder because the specialized local pipeline is unavailable without the backend/API path.",
            args_payload=args,
            extra={
                "query_used": query,
                "papers_found": len(papers or []),
                "supporting_papers": summarized,
            },
        )
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Specialized local pipeline failed; returning degraded metadata instead of raising.",
            args_payload=args,
            extra={"detail": str(exc)},
        )
