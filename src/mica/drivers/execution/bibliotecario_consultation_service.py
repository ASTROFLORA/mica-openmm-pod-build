"""Bibliotecario consultation helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Callable, Dict, Optional

from ..role_context import BIBLIOTECARIO_INVARIANTS, RoleSpec


def build_bibliotecario_role_spec(system_prompt: str) -> RoleSpec:
    return RoleSpec(
        role_id="bibliotecario",
        system_prompt=system_prompt,
        max_iterations=8,
        temperature=0.3,
        output_invariants=BIBLIOTECARIO_INVARIANTS,
    )


def build_last_bibliotecario_state_setter(
    state_store: Dict[str, Any],
) -> Callable[[Dict[str, Any]], None]:
    def _set_last_bibliotecario_state(state: Dict[str, Any]) -> None:
        state_store.clear()
        state_store.update(dict(state or {}))

    return _set_last_bibliotecario_state


async def run_consult_bibliotecario_branch(
    *,
    name: str,
    args: Dict[str, Any],
    pending: Any,
    shorten_query_fn: Callable[[Any], str],
    search_literature_result_fn: Callable[..., Any],
    driver_literature_sources: list,
    retrieval_planner_obj: Any,
    user_id: Optional[str],
    workspace_id: str,
    parent_run_id: Optional[str],
    provider_id: Optional[str],
    model_id: Optional[str],
    abort: Any,
    role_spec: Any,
    embody_role_fn: Callable[..., Any],
    normalize_citations_fn: Callable[..., Any],
    build_source_record_from_paper_fn: Callable[..., Any],
    format_bibliotecario_citation_entry_fn: Callable[..., str],
    active_session_id: str,
    agent_memory_obj: Any,
    summary_store_obj: Any,
    persist_agent_summary_fn: Callable[..., Any],
    record_claim_dicts_fn: Callable[..., Any],
    set_last_bibliotecario_state_fn: Callable[[Dict[str, Any]], None],
) -> str:
    from mica.agentic.events import SideData
    from mica.infrastructure.literature.literature_artifact_bundle import (
        build_literature_artifact_manifest,
        build_primary_synthesis_from_bundle,
        build_rich_literature_artifact_bundle,
    )

    _live_pending: list = pending if pending is not None else []
    query_lit = shorten_query_fn(args.get("query", ""))
    task = args.get("task", query_lit)
    max_p = int(args.get("max_papers", 40))
    try:
        search_result = await search_literature_result_fn(
            query=query_lit,
            max_papers=max_p,
            sources=driver_literature_sources,
        )
        papers = list(search_result.papers)
        paper_by_id = {
            str(p.get("paperId") or p.get("canonical_id") or "").strip(): p
            for p in (papers or [])
            if str(p.get("paperId") or p.get("canonical_id") or "").strip()
        }
        _live_pending.append(SideData(
            channel="research", agent="bibliotecario",
            payload={"papers": [
                {
                    "title": p.get("title"),
                    "year": p.get("year"),
                    "paperId": p.get("paperId"),
                    "citationCount": p.get("citationCount", 0),
                }
                for p in (papers or [])
            ]},
        ))
        max_corpus = min(max_p, 40)
        corpus_lines = [
            f"[{i+1}] {p.get('title','')} ({p.get('year','?')}) — "
            f"{(p.get('abstract') or '')[:350]}"
            for i, p in enumerate((papers or [])[:max_corpus])
        ]
        corpus_msg = (
            f"CORPUS ({len((papers or [])[:max_corpus])} papers on '{query_lit}'):\n\n"
            + "\n\n".join(corpus_lines)
        )

        citations_log: list = []
        gaps_log: list = []

        async def _biblio_exec(n: str, cid: str, a: dict) -> str:
            if n == "cite_finding":
                citations_log.append(dict(a))
            elif n == "identify_gap":
                gaps_log.append(dict(a))
            return json.dumps({"recorded": True, "name": n, "data": a}, ensure_ascii=False)

        biblio_messages = [{"role": "user", "content": corpus_msg}]
        prior_ctx = await retrieval_planner_obj.build_mode_context(
            agent_name="bibliotecario",
            query_text=query_lit,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
        )
        if prior_ctx:
            biblio_messages.append({"role": "user", "content": prior_ctx})
        biblio_messages.append({"role": "user", "content": f"TASK: {task}"})

        synthesis, report_path, _role_ctx = await embody_role_fn(
            role_spec=role_spec,
            task_messages=biblio_messages,
            provider_id=provider_id,
            model_id=model_id,
            pending_events=_live_pending,
            abort=abort,
            parent_executor=_biblio_exec,
        )
        citations_log = _role_ctx.citations_log
        gaps_log = _role_ctx.gaps_log

        normalized_citations = normalize_citations_fn(citations_log, paper_by_id)
        sources = [
            build_source_record_from_paper_fn(p)
            for p in (papers or [])[:20]
        ]

        artifact_bundle = await build_rich_literature_artifact_bundle(
            query=query_lit,
            preset="consult_bibliotecario",
            user_id=user_id or "default",
            session_id=active_session_id,
            backend=str(getattr(search_result, "backend", "") or "dlm_literature_search_service"),
            papers=papers,
            requested_sources=list(getattr(search_result, "requested_sources", []) or driver_literature_sources),
            attempted_sources=list(getattr(search_result, "attempted_sources", []) or []),
            failed_sources=list(getattr(search_result, "failed_sources", []) or []),
            source_counts=dict(getattr(search_result, "source_counts", {}) or {}),
            provider_health=dict(getattr(search_result, "source_health", {}) or {}),
            retrieval_policy=dict(getattr(search_result, "request_envelope", {}).get("retrieval_policy") or {}),
            acquisition_envelope=dict(getattr(search_result, "request_envelope", {}) or {}),
            generation_notes=[
                "Consult bibliotecario now promotes the canonical artifact family instead of synthesis-only closure.",
                "The driver uses the same bundle shape as Bibliotecario sync and deep research.",
            ],
            synthesis_hint=synthesis or "",
        )
        artifact_bundle_payload = artifact_bundle.model_dump()
        artifact_manifest = build_literature_artifact_manifest(artifact_bundle_payload)
        primary_synthesis = build_primary_synthesis_from_bundle(
            artifact_bundle_payload,
            fallback_synthesis=synthesis or "",
        )
        set_last_bibliotecario_state_fn({
            "query": query_lit,
            "task": task,
            "synthesis": primary_synthesis or synthesis or "",
            "role_synthesis": synthesis or "",
            "normalized_citations": normalized_citations,
            "gaps": gaps_log[:10],
            "report_path": report_path,
            "artifact_bundle": artifact_bundle_payload,
            "artifact_manifest": artifact_manifest,
            "artifact_list": list(artifact_manifest.get("artifacts") or []),
        })

        biblio_entry = agent_memory_obj.store(
            agent_name="bibliotecario",
            query=query_lit,
            synthesis=primary_synthesis or synthesis or "",
            citations=normalized_citations[:20],
            gaps=gaps_log[:10],
            metadata={
                "canonical_citations": normalized_citations[:20],
                "task": task,
                "artifact_manifest": artifact_manifest,
            },
        )
        persist_agent_summary_fn(
            summary_store=summary_store_obj,
            entry=biblio_entry,
            agent_name="bibliotecario",
            query=query_lit,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
            run_id=parent_run_id,
            artifact_path=report_path,
        )

        appendix_parts = []
        if normalized_citations:
            appendix_parts.append(
                "\n\n[CITED EVIDENCE]\n"
                + "\n".join(
                    format_bibliotecario_citation_entry_fn(c, paper_by_id)
                    for c in normalized_citations[:10]
                )
            )
        if gaps_log:
            appendix_parts.append(
                "\n\n[IDENTIFIED GAPS]\n"
                + "\n".join(
                    f"  ▶ [{g.get('gap_type','?')}] {g.get('description','')}"
                    for g in gaps_log[:5]
                )
            )
        if _live_pending is not None:
            _live_pending.append(SideData(
                channel="research_evidence",
                agent="bibliotecario",
                payload={
                    "citations": normalized_citations[:20],
                    "gaps": gaps_log[:10],
                    "sources": sources,
                    "artifact_manifest": artifact_manifest,
                },
            ))
        record_claim_dicts_fn(
            session_id=active_session_id,
            run_id=parent_run_id or active_session_id,
            claims=[{
                "claim_id": f"bibliotecario-{query_lit[:32] or 'query'}",
                "text": str(primary_synthesis or synthesis or "").strip(),
                "strength": "supported" if normalized_citations else "suggestive",
                "source_ids": [
                    str(c.get("source_id") or "")
                    for c in normalized_citations[:20]
                    if str(c.get("source_id") or "")
                ],
                "severity": "important",
            }],
            default_severity="important",
            validation_route="literature",
            evidence_type="literature",
            verification_status="verified" if normalized_citations else "unverified",
        )
        synthesis_text = (
            primary_synthesis or synthesis or "The bibliotecario found no relevant synthesis."
        ) + "".join(appendix_parts)
        return json.dumps(
            {
                "backend": "dlm_literature_search_service",
                "query": query_lit,
                "task": task,
                "papers_found": len(papers or []),
                "source_health": dict(search_result.source_health or {}),
                "synthesis": primary_synthesis or synthesis or "",
                "citations": normalized_citations[:20],
                "gaps": gaps_log[:10],
                "sources": sources,
                "report_path": report_path,
                "artifact_bundle": artifact_bundle_payload,
                "artifact_manifest": artifact_manifest,
                "artifact_list": list(artifact_manifest.get("artifacts") or []),
                "text": synthesis_text,
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
