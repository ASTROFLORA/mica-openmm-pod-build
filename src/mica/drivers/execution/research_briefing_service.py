"""Research briefing helpers extracted from AgenticDriver loop executor."""

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _try_restore_continuity(
    *,
    prior_run_id: Optional[str],
    query_spec_hash: Optional[str],
    base_dir: str = "./.artifacts",
) -> Optional[Dict[str, Any]]:
    """Attempt to restore a prior research briefing from local artifact storage.

    Packet 6A — continuity restore contract:
    - Scans ``<base_dir>/run/<prior_run_id>/run_summary_json/`` when prior_run_id is set.
    - Falls back to scanning all run directories for a summary whose
      ``query_spec_hash`` matches when only query_spec_hash is set.
    - Returns the first matching summary dict, or None when nothing is found.

    Never raises — callers must handle None as a cache miss.
    """
    try:
        root = Path(base_dir) / "run"
        if not root.exists():
            return None

        if prior_run_id:
            candidates = [root / str(prior_run_id) / "run_summary_json"]
        else:
            # Scan all run directories for a matching hash
            candidates = [d / "run_summary_json" for d in root.iterdir() if d.is_dir()]

        for summary_dir in candidates:
            if not summary_dir.is_dir():
                continue
            for dat_file in summary_dir.glob("*.dat"):
                try:
                    payload = json.loads(dat_file.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        continue
                    # Match by explicit run_id or by query_spec_hash
                    if prior_run_id and str(payload.get("run_id") or "") == prior_run_id:
                        return payload
                    if query_spec_hash and str(payload.get("query_spec_hash") or "") == query_spec_hash:
                        return payload
                except Exception:
                    continue
    except Exception:
        pass
    return None


async def run_research_briefing_branch(
    *,
    name: str,
    args: Dict[str, Any],
    shorten_query_fn: Callable[[str], str],
    degraded_tool_response_fn: Callable[..., str],
    search_literature_result_fn: Callable[..., Awaitable[Any]],
    driver_literature_sources: List[str],
) -> str:
    if name == "compile_research_briefing":
        query_text = shorten_query_fn(
            args.get("query") or args.get("entity") or args.get("protein") or ""
        )
    else:
        query_text = shorten_query_fn(args.get("protein") or args.get("query") or "")

    if not query_text:
        return degraded_tool_response_fn(
            name,
            "A query or protein identifier is required.",
            args_payload=args,
        )

    # ── Packet 6A: continuity restore ───────────────────────────────────────
    if name == "compile_research_briefing":
        prior_run_id = str(args.get("prior_run_id") or "").strip() or None
        query_spec_hash = str(args.get("query_spec_hash") or "").strip() or None
        if prior_run_id or query_spec_hash:
            restored = _try_restore_continuity(
                prior_run_id=prior_run_id,
                query_spec_hash=query_spec_hash,
            )
            if restored:
                logger.info(
                    "[research_briefing] continuity restore hit: run_id=%s hash=%s",
                    restored.get("run_id"),
                    restored.get("query_spec_hash"),
                )
                return json.dumps(
                    {
                        "status": "restored_continuity",
                        "tool": name,
                        "query": query_text,
                        "prior_run_id": prior_run_id,
                        "query_spec_hash": query_spec_hash,
                        "briefing": {
                            "executive_summary": restored.get(
                                "executive_summary",
                                f"Restored briefing for '{query_text}' from prior run.",
                            ),
                            "key_findings": restored.get("key_findings", []),
                            "entity_landscape": restored.get("entity_landscape", []),
                            "open_gaps": restored.get("open_gaps", []),
                        },
                        "papers_found": restored.get("total_papers", 0),
                        "restored_from": str(restored.get("run_id") or prior_run_id or query_spec_hash),
                    },
                    ensure_ascii=False,
                )
            else:
                logger.info(
                    "[research_briefing] continuity restore miss — degraded: run_id=%s hash=%s",
                    prior_run_id,
                    query_spec_hash,
                )
                return json.dumps(
                    {
                        "status": "degraded_continuity",
                        "tool": name,
                        "query": query_text,
                        "prior_run_id": prior_run_id,
                        "query_spec_hash": query_spec_hash,
                        "reason": (
                            "No prior run artifact found matching the provided "
                            "prior_run_id or query_spec_hash. "
                            "Re-run compile_research_briefing without continuity "
                            "keys to perform a fresh search."
                        ),
                    },
                    ensure_ascii=False,
                )
    # ── End continuity restore ────────────────────────────────────────────────

    try:
        preset = args.get("preset", "standard")
        n_map = {"quick-scan": 12, "standard": 24, "deep-research": 40, "exhaustive": 60}
        search_query = f"{query_text} drug repurposing" if name == "scan_drug_repurposing" else query_text
        search_result = await search_literature_result_fn(
            query=search_query,
            max_papers=n_map.get(preset, 24),
            sources=driver_literature_sources,
        )
        papers = list(search_result.papers)
        top_papers = [
            {
                "title": paper.get("title", ""),
                "year": paper.get("year"),
                "paperId": paper.get("paperId", ""),
                "citations": paper.get("citationCount", 0),
                "abstract": (paper.get("abstract") or "")[:280],
            }
            for paper in (papers or [])[:8]
        ]

        if name == "compile_research_briefing":
            briefing = {
                "executive_summary": f"Briefing for '{query_text}' built from {len(papers or [])} retrieved papers.",
                "key_findings": [item["title"] for item in top_papers[:5]],
                "entity_landscape": top_papers[:5],
                "open_gaps": [
                    "Requires manual review of primary papers for mechanistic confidence.",
                    "Cross-source citation validation should be run before publication use.",
                ],
            }
            return json.dumps(
                {
                    "status": "ok",
                    "tool": name,
                    "query": query_text,
                    "papers_found": len(papers or []),
                    "source_health": dict(search_result.source_health or {}),
                    "briefing": briefing,
                },
                ensure_ascii=False,
            )

        alerts = [
            {
                "candidate_signal": paper["title"],
                "year": paper["year"],
                "paperId": paper["paperId"],
                "rationale": paper["abstract"],
            }
            for paper in top_papers[: min(len(top_papers), int(args.get("max_alerts", 20)))]
        ]
        return json.dumps(
            {
                "status": "ok",
                "tool": name,
                "protein": query_text,
                "papers_found": len(papers or []),
                "source_health": dict(search_result.source_health or {}),
                "pipeline_output": search_result.pipeline_output.to_dict()
                if getattr(search_result, "pipeline_output", None)
                else {},
                "alerts": alerts,
                "method": "literature_heuristic_scan",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
