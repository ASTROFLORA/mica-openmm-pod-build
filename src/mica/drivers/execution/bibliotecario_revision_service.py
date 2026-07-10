"""Bibliotecario revision-cycle orchestration extracted from AgenticDriver."""

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set

from mica.agentic.events import SideData

from ..role_context import BIBLIOTECARIO_INVARIANTS, RoleSpec
from ...agentic.tool_capability_registry import filter_tools_for_lane


async def run_bibliotecario_revision_cycle(
    *,
    verdict: Dict[str, Any],
    reviewer_focus: str,
    reviewer_critique: str,
    live_pending: List[Any],
    last_bibliotecario_state: Dict[str, Any],
    shorten_query_fn: Callable[[str], str],
    search_literature_records_fn: Callable[..., Awaitable[List[Dict[str, Any]]]],
    driver_literature_sources: Sequence[str],
    build_source_record_from_paper_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    retrieval_planner: Any,
    user_id: str,
    workspace_id: str,
    parent_run_id: str,
    embody_role_fn: Callable[..., Awaitable[Any]],
    provider_id: str,
    model_id: Optional[str],
    abort: Optional[Any],
    bibliotecario_system_prompt: str,
    bibliotecario_tools: Sequence[Dict[str, Any]],
    depth_preset_name: str,
    normalize_bibliotecario_citations_fn: Callable[[List[Dict[str, Any]], Dict[str, Dict[str, Any]]], List[Dict[str, Any]]],
    agent_memory: Any,
    persist_agent_summary_fn: Callable[..., Any],
    summary_store: Any,
    active_session_id: str,
    record_claim_dicts_fn: Callable[..., Any],
) -> Optional[Dict[str, Any]]:
    if not verdict.get("should_revise") or not last_bibliotecario_state:
        return None

    try:
        merged_queries = [last_bibliotecario_state.get("query") or ""] + list(verdict.get("recommended_queries") or [])
        deduped_queries: List[str] = []
        seen_queries: Set[str] = set()
        for raw_query in merged_queries:
            short = shorten_query_fn(str(raw_query or ""))
            if not short:
                continue
            key = short.casefold()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            deduped_queries.append(short)

        enriched_papers: List[Dict[str, Any]] = []
        seen_papers: Set[str] = set()
        for extra_query in deduped_queries[:6]:
            papers = await search_literature_records_fn(
                query=extra_query,
                max_papers=8,
                sources=driver_literature_sources,
            )
            for paper in papers or []:
                key = str(paper.get("canonical_id") or paper.get("paperId") or paper.get("title") or "").strip()
                if not key or key in seen_papers:
                    continue
                seen_papers.add(key)
                enriched_papers.append(paper)

        paper_by_id = {
            str(p.get("paperId") or p.get("canonical_id") or "").strip(): p
            for p in enriched_papers
            if str(p.get("paperId") or p.get("canonical_id") or "").strip()
        }
        normalized_sources = [build_source_record_from_paper_fn(p) for p in enriched_papers[:24]]
        corpus_lines = [
            f"[{i+1}] {p.get('title','')} ({p.get('year','?')}) — {(p.get('abstract') or '')[:350]}"
            for i, p in enumerate(enriched_papers[:40])
        ]
        corpus_msg = (
            f"REVISION CORPUS ({len(enriched_papers[:40])} papers across reviewer-directed queries):\n\n"
            + "\n\n".join(corpus_lines)
        )

        citations_log: List[Dict[str, Any]] = []
        gaps_log: List[Dict[str, Any]] = []

        async def _biblio_revision_exec(n: str, cid: str, a: dict) -> str:
            if n == "cite_finding":
                citations_log.append(dict(a))
            elif n == "identify_gap":
                gaps_log.append(dict(a))
            return json.dumps({"recorded": True, "name": n, "data": a}, ensure_ascii=False)

        revision_messages = [{"role": "user", "content": corpus_msg}]
        prior_ctx = await retrieval_planner.build_mode_context(
            agent_name="bibliotecario",
            query_text="; ".join(deduped_queries[:6]) or str(last_bibliotecario_state.get("query") or reviewer_focus),
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
        )
        if prior_ctx:
            revision_messages.append({"role": "user", "content": prior_ctx})
        revision_messages.extend(
            [
                {"role": "user", "content": f"ORIGINAL SYNTHESIS:\n{last_bibliotecario_state.get('synthesis','')}"},
                {"role": "user", "content": f"REVIEWER VERDICT:\n{reviewer_critique[:4000]}"},
                {
                    "role": "user",
                    "content": (
                        "TASK: Produce a revised bibliotecario synthesis that directly addresses the reviewer verdict.\n"
                        "Requirements:\n"
                        "1. Use the reviewer-recommended searches and the expanded corpus.\n"
                        "2. Replace unsupported claims with primary-mechanistic citations when possible.\n"
                        "3. Clearly mark any remaining [UNVERIFIED] statements.\n"
                        f"4. Reviewer-directed searches to cover: {', '.join(deduped_queries[:6])}."
                    ),
                },
            ]
        )

        biblio_rev_spec = RoleSpec(
            role_id="bibliotecario",
            system_prompt=bibliotecario_system_prompt,
            max_iterations=8,
            temperature=0.3,
            output_invariants=BIBLIOTECARIO_INVARIANTS,
        )
        revised_synthesis, report_path, role_ctx = await embody_role_fn(
            role_spec=biblio_rev_spec,
            task_messages=revision_messages,
            provider_id=provider_id,
            model_id=model_id,
            pending_events=live_pending,
            abort=abort,
            parent_executor=_biblio_revision_exec,
            available_tools=filter_tools_for_lane(
                list(bibliotecario_tools),
                lane="scientific_audit",
                depth_preset_name=depth_preset_name,
            ),
        )
        citations_log = role_ctx.citations_log
        gaps_log = role_ctx.gaps_log

        normalized_citations = normalize_bibliotecario_citations_fn(citations_log, paper_by_id)
        revised_entry = agent_memory.store(
            agent_name="bibliotecario",
            query="; ".join(deduped_queries[:6]) or str(last_bibliotecario_state.get("query") or reviewer_focus),
            synthesis=revised_synthesis or "",
            citations=normalized_citations[:20],
            gaps=gaps_log[:10],
            metadata={
                "revision_cycle": True,
                "reviewer_decision": verdict.get("decision"),
                "recommended_queries": deduped_queries[:6],
            },
        )
        persist_agent_summary_fn(
            summary_store=summary_store,
            entry=revised_entry,
            agent_name="bibliotecario",
            query="; ".join(deduped_queries[:6]) or reviewer_focus,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
            run_id=parent_run_id,
            artifact_path=report_path,
        )

        record_claim_dicts_fn(
            session_id=active_session_id,
            run_id=parent_run_id or active_session_id,
            claims=[
                {
                    "claim_id": f"bibliotecario-revision-{len(normalized_citations)}",
                    "text": str(revised_synthesis or "").strip(),
                    "strength": "supported" if normalized_citations else "suggestive",
                    "source_ids": [
                        str(source.get("source_id") or "")
                        for source in normalized_sources
                        if str(source.get("source_id") or "")
                    ],
                    "severity": "important",
                }
            ],
            default_severity="important",
            validation_route="literature",
            evidence_type="review",
            verification_status="verified" if normalized_citations else "unverified",
        )

        live_pending.append(
            SideData(
                channel="research_revision",
                agent="bibliotecario",
                payload={
                    "queries": deduped_queries[:6],
                    "citations": normalized_citations[:20],
                    "gaps": gaps_log[:10],
                    "sources": normalized_sources,
                },
            )
        )

        last_bibliotecario_state.update(
            {
                "query": "; ".join(deduped_queries[:6]) or last_bibliotecario_state.get("query", ""),
                "task": reviewer_focus,
                "synthesis": revised_synthesis or "",
                "normalized_citations": normalized_citations,
                "gaps": gaps_log[:10],
                "report_path": report_path,
            }
        )
        return {
            "synthesis": revised_synthesis or "",
            "queries": deduped_queries[:6],
            "report_path": report_path,
            "citations": normalized_citations[:20],
        }
    except Exception as exc:
        return {"error": str(exc)}