"""Deep research helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional


async def run_deep_research_branch(
    *,
    name: str,
    args: Dict[str, Any],
    shorten_query_fn: Callable[[str], str],
    search_literature_result_fn: Callable[..., Awaitable[Any]],
    driver_literature_sources: List[str],
    pending: Optional[List[Any]],
) -> str:
    query = shorten_query_fn(args.get("query", ""))
    preset = args.get("preset", "standard")
    n_map = {"quick-scan": 30, "standard": 80, "deep-research": 150, "exhaustive": 300}

    try:
        from mica.agentic.events import SideData

        search_result = await search_literature_result_fn(
            query=query,
            max_papers=n_map.get(preset, 80),
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
            }
            for paper in (papers or [])[:30]
        ]
        if pending is not None:
            pending.append(
                SideData(
                    channel="research",
                    agent=name,
                    payload={"papers": (papers or [])[:30], "query": query, "preset": preset},
                )
            )

        return json.dumps(
            {
                "query": query,
                "total_papers": len(papers or []),
                "showing": len(output_rows),
                "source_health": dict(search_result.source_health or {}),
                "pipeline_output": search_result.pipeline_output.to_dict()
                if getattr(search_result, "pipeline_output", None)
                else {},
                "results": output_rows,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
