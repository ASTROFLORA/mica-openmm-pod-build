"""Literature-search branch helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional


async def run_literature_search_branch(
    *,
    name: str,
    args: Dict[str, Any],
    shorten_query_fn: Callable[[str], str],
    search_literature_result_fn: Callable[..., Awaitable[Any]],
    driver_literature_sources: List[str],
    pending: Optional[List[Any]],
) -> str:
    query = shorten_query_fn(args.get("query") or args.get("entity") or args.get("term", ""))

    try:
        from mica.agentic.events import SideData

        preset = args.get("preset", "standard")
        n_map = {
            "quick-scan": 30,
            "standard": 80,
            "deep-research": 150,
            "exhaustive": 300,
        }
        limit = min(
            int(args.get("limit", args.get("max_results", n_map.get(preset, 80)))),
            300,
        )
        search_result = await search_literature_result_fn(
            query=query,
            max_papers=limit,
            sources=driver_literature_sources,
        )
        papers = list(search_result.papers)
        output_rows = [
            {
                "title": paper.get("title", ""),
                "year": paper.get("year"),
                "citations": paper.get("citationCount", 0),
                "abstract": (paper.get("abstract") or "")[:400],
                "paperId": paper.get("paperId", ""),
                "authors": [author.get("name", "") for author in (paper.get("authors") or [])[:3]],
            }
            for paper in (papers or [])[:20]
        ]
        by_year: Dict[int, int] = {}
        for paper in (papers or []):
            year = paper.get("year")
            if year:
                by_year[int(year)] = by_year.get(int(year), 0) + 1

        if pending is not None:
            pending.append(
                SideData(
                    channel="research",
                    agent=name,
                    payload={"papers": (papers or [])[:20], "query": query},
                )
            )

        return json.dumps(
            {
                "backend": "dlm_literature_search_service",
                "lookup_mode": name,
                "papers_found": len(papers or []),
                "query_used": query,
                "source_health": dict(search_result.source_health or {}),
                "pipeline_output": search_result.pipeline_output.to_dict()
                if getattr(search_result, "pipeline_output", None)
                else {},
                "papers": output_rows,
                "results": output_rows,
                "by_year": dict(sorted(by_year.items())),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
