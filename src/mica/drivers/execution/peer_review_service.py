"""Peer review routing helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Callable, Dict, Optional

from ..role_context import REVIEWER_INVARIANTS, RoleSpec


def build_reviewer_role_spec() -> RoleSpec:
    return RoleSpec(
        role_id="msrp_reviewer",
        system_prompt=(
            "You are an independent MSRP peer reviewer with Nature-tier standards. "
            "You MUST use search_literature to independently verify key claims - "
            "do NOT trust the author's citations at face value. "
            "Use flag_issue for each concrete problem. "
            "End with a clear verdict: ACCEPT / MAJOR_REVISION / MINOR_REVISION / REJECT."
        ),
        max_iterations=6,
        temperature=0.3,
        temperature_override=0.1,
        output_invariants=REVIEWER_INVARIANTS,
    )


class PeerReviewQualityAdapterService:
    def __init__(self, driver_obj: Any):
        self._driver = driver_obj

    def parse_peer_review_verdict(self, critique: str, review_issues: list[Dict[str, Any]]) -> Dict[str, Any]:
        return self._driver._parse_peer_review_verdict(critique, review_issues)

    def build_quality_score_adapter(self, **kwargs: Any) -> Any:
        return self._driver._build_quality_score_adapter(**kwargs)

    def build_peer_feedback_adapter(self, **kwargs: Any) -> Any:
        return self._driver._build_peer_feedback_adapter(**kwargs)


async def run_request_peer_review_branch(
    *,
    name: str,
    args: Dict[str, Any],
    pending: Any,
    search_literature_records_fn: Callable[..., Any],
    retrieval_planner_obj: Any,
    driver_literature_sources: list,
    user_id: Optional[str],
    workspace_id: str,
    parent_run_id: Optional[str],
    evidence_ledger_obj: Any,
    role_spec: Any,
    embody_role_fn: Callable[..., Any],
    provider_id: Optional[str],
    model_id: Optional[str],
    abort: Any,
    quality_adapter_service_obj: Any,
    last_bibliotecario_state: Dict[str, Any],
    serialize_legacy_model_fn: Callable[..., Any],
    active_session_id: str,
    record_claim_dicts_fn: Callable[..., Any],
    agent_memory_obj: Any,
    summary_store_obj: Any,
    persist_agent_summary_fn: Callable[..., Any],
    publish_communication_review_projection_fn: Callable[..., Any],
    run_bibliotecario_revision_cycle_fn: Callable[..., Any],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    from mica.agentic.events import SideData

    _live_pending = pending if pending is not None else []
    content = args.get("content", "")
    focus = args.get("focus", "general")
    review_prompt = (
        f"You are an MSRP peer reviewer with Nature-tier publication standards.\n"
        f"Review focus: {focus}\n\n"
        f"Content to review:\n{content[:3000]}\n\n"
        f"REVIEW PROTOCOL:\n"
        f"1. SKEPTICISM: Identify methodological weaknesses, unsupported claims, logical gaps.\n"
        f"2. EVIDENCE DEMAND: For every major claim, use search_literature to independently "
        f"verify whether the cited evidence actually supports it. Flag [UNVERIFIED] claims.\n"
        f"3. PUBLICATION READINESS: Would Nature accept this as-is? What specific changes "
        f"are required before submission?\n"
        f"4. ACTIONABLE VERDICT: End with a clear ACCEPT / MAJOR_REVISION / MINOR_REVISION / "
        f"REJECT decision and numbered action items.\n\n"
        f"Use search_literature to spot-check at least 2 key claims. Be specific. Max 500 tokens."
    )

    reviewer_tools = [
        {
            "type": "function",
            "function": {
                "name": "search_literature",
                "description": (
                    "Search Semantic Scholar to independently verify claims. "
                    "Use this to spot-check cited evidence and find contradicting studies."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Short, specific query (3-6 words)"},
                        "max_papers": {"type": "integer", "description": "Max papers to retrieve (default 8)"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "flag_issue",
                "description": (
                    "Flag a specific issue found during review. "
                    "Severity: critical | major | minor. "
                    "Use for each concrete problem identified."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                        "claim": {"type": "string", "description": "The specific claim or section being flagged"},
                        "issue": {"type": "string", "description": "What is wrong and why"},
                        "recommendation": {"type": "string", "description": "Specific fix or action needed"},
                    },
                    "required": ["severity", "claim", "issue"],
                },
            },
        },
    ]

    review_issues: list = []

    try:
        async def _review_exec(n: str, cid: str, a: dict) -> str:
            if n == "search_literature":
                try:
                    papers = await search_literature_records_fn(
                        query=a.get("query", ""),
                        max_papers=int(a.get("max_papers", 8)),
                        sources=driver_literature_sources,
                    )
                    summaries = [
                        f"[{p.get('paperId','?')}] {p.get('title','')} ({p.get('year','?')}): "
                        f"{(p.get('abstract') or '')[:200]}"
                        for p in (papers or [])[:8]
                    ]
                    return json.dumps({"count": len(summaries), "results": summaries})
                except Exception as search_exc:
                    return json.dumps({"error": str(search_exc)})
            elif n == "flag_issue":
                review_issues.append(dict(a))
                return json.dumps({"recorded": True, "issue_count": len(review_issues)})
            return json.dumps({"ok": True})

        prior_review_ctx = await retrieval_planner_obj.build_mode_context(
            agent_name="msrp_reviewer",
            query_text=focus,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
        )
        review_messages = []
        if prior_review_ctx:
            review_messages.append({"role": "user", "content": prior_review_ctx})

        ledger_ctx = ""
        try:
            active_ledger = evidence_ledger_obj
            if active_ledger is not None and hasattr(active_ledger, "get_for_review"):
                critical = active_ledger.get_for_review(severity_filter="critical")
                contradicted = active_ledger.get_contradicted_claims() if hasattr(active_ledger, "get_contradicted_claims") else []
                if critical or contradicted:
                    parts = ["[EVIDENCE LEDGER - CLAIMS TO VERIFY]"]
                    for critical_entry in (critical or [])[:10]:
                        parts.append(
                            f"  CRITICAL ({critical_entry.status}): \"{critical_entry.claim_text}\" "
                            f"[sources={len(critical_entry.source_ids)}, conf={critical_entry.algorithmic_confidence:.2f}]"
                        )
                    for contradicted_entry in (contradicted or [])[:5]:
                        parts.append(
                            f"  CONTRADICTED: \"{contradicted_entry.claim_text}\" "
                            f"[neg_refs={len(contradicted_entry.negative_result_refs)}]"
                        )
                    ledger_ctx = "\n".join(parts)
        except Exception:
            pass

        full_review_prompt = review_prompt
        if ledger_ctx:
            full_review_prompt = review_prompt + "\n\n" + ledger_ctx

        review_messages.append({"role": "user", "content": full_review_prompt})

        critique, report_path, role_ctx = await embody_role_fn(
            role_spec=role_spec,
            task_messages=review_messages,
            provider_id=provider_id,
            model_id=model_id,
            pending_events=_live_pending,
            abort=abort,
            parent_executor=_review_exec,
            available_tools=reviewer_tools,
        )
        review_issues = role_ctx.review_issues

        verdict = quality_adapter_service_obj.parse_peer_review_verdict(critique or "", review_issues)
        if review_issues:
            issues_text = "\n\n[REVIEW ISSUES]\n" + "\n".join(
                f"  {'🔴' if ri.get('severity')=='critical' else '🟡' if ri.get('severity')=='major' else '🟢'} "
                f"[{ri.get('severity','?').upper()}] {ri.get('claim','')}: "
                f"{ri.get('issue','')} → {ri.get('recommendation','')}"
                for ri in review_issues
            )
            critique = (critique or "") + issues_text

        if _live_pending is not None:
            review_quality = quality_adapter_service_obj.build_quality_score_adapter(
                verdict=verdict,
                review_issues=review_issues,
                citation_count=len(last_bibliotecario_state.get("normalized_citations", []) or []),
            )
            peer_feedback = quality_adapter_service_obj.build_peer_feedback_adapter(
                focus=focus,
                verdict=verdict,
                review_issues=review_issues,
                quality_score=review_quality,
            )
            _live_pending.append(SideData(
                channel="peer_review",
                agent="msrp_reviewer",
                payload={
                    "issues": review_issues,
                    "verdict": verdict,
                    "peer_feedback": serialize_legacy_model_fn(peer_feedback),
                    "quality_score": serialize_legacy_model_fn(review_quality),
                },
            ))
        else:
            review_quality = quality_adapter_service_obj.build_quality_score_adapter(
                verdict=verdict,
                review_issues=review_issues,
                citation_count=len(last_bibliotecario_state.get("normalized_citations", []) or []),
            )
            peer_feedback = quality_adapter_service_obj.build_peer_feedback_adapter(
                focus=focus,
                verdict=verdict,
                review_issues=review_issues,
                quality_score=review_quality,
            )
        record_claim_dicts_fn(
            session_id=active_session_id,
            run_id=parent_run_id or active_session_id,
            claims=[{
                "claim_id": f"peer-review-{focus[:32] or 'general'}",
                "text": str(critique or "").strip(),
                "strength": "contradicted" if verdict.get("decision") in {"REJECT", "MAJOR_REVISION"} else "mixed",
                "source_ids": [],
                "severity": verdict.get("severity") or "important",
            }],
            default_severity=verdict.get("severity") or "important",
            validation_route="review",
            evidence_type="review",
            verification_status="unverified" if verdict.get("should_revise") else "verified",
        )

        reviewer_entry = agent_memory_obj.store(
            agent_name="msrp_reviewer",
            query=focus,
            synthesis=critique or "",
            review_issues=review_issues,
            metadata={
                "verdict": verdict,
                "peer_feedback": serialize_legacy_model_fn(peer_feedback),
                "quality_score": serialize_legacy_model_fn(review_quality),
            },
        )
        persist_agent_summary_fn(
            summary_store=summary_store_obj,
            entry=reviewer_entry,
            agent_name="msrp_reviewer",
            query=focus,
            user_id=user_id or "default",
            workspace_id=workspace_id,
            session_id=parent_run_id,
            run_id=parent_run_id,
            artifact_path=report_path,
        )
        try:
            await publish_communication_review_projection_fn(
                session_id=active_session_id,
                run_id=parent_run_id or active_session_id,
                focus=focus,
                critique=critique or "",
                verdict=verdict,
                review_issues=review_issues,
                peer_feedback=peer_feedback,
                quality_score=review_quality,
                artifact_path=report_path,
            )
        except Exception:
            pass

        revised_result = await run_bibliotecario_revision_cycle_fn(
            verdict=verdict,
            reviewer_focus=focus,
            reviewer_critique=critique or "",
            live_pending=_live_pending,
        )
        if revised_result and not revised_result.get("error"):
            critique = (
                (critique or "")
                + "\n\n[AUTO-REVISION]\n"
                + f"Reviewer-triggered second bibliotecario pass completed for queries: {', '.join(revised_result.get('queries') or [])}.\n"
                + (revised_result.get("synthesis") or "")[:3000]
            )
        elif revised_result and revised_result.get("error"):
            critique = (critique or "") + f"\n\n[AUTO-REVISION-ERROR]\n{revised_result.get('error')}"

        return critique or "No critique was generated."
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Peer review path degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )
