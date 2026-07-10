from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from mica.infrastructure.literature.control_plane import build_literature_identity_keys
from mica.infrastructure.literature.fulltext_router import UnpaywallClient
from mica.literature_consolidation.contracts.provider_quorum import (
    ProviderExecutionReceipt,
    ProviderQuorumPolicy,
    ProviderQuorumReceipt,
    ProviderQuorumRuntimeResult,
)
from mica.literature_consolidation.contracts.query_protocol import LiteratureQuerySpec
from mica.literature_consolidation.provider_compiler import LiteratureProviderCompiler
from mica.services.literature_search_service import LiteratureSearchService


def _dedupe_by_identity(papers: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected: Dict[str, Dict[str, Any]] = {}
    for paper in papers:
        item = dict(paper or {})
        keys = build_literature_identity_keys(
            canonical_id=str(item.get("canonical_id") or ""),
            doi=str(item.get("doi") or ""),
            pmid=str(item.get("pmid") or item.get("externalIds", {}).get("PubMed") or ""),
            pmcid=str(item.get("pmcid") or ""),
            arxiv_id=str(item.get("arxivId") or ""),
            title=str(item.get("title") or ""),
            platform=str(item.get("provider") or item.get("source") or ""),
            paper_id=str(item.get("paperId") or ""),
        )
        key = next((value for value in keys if str(value).strip()), "")
        if not key:
            key = str(item.get("paperId") or item.get("title") or "unknown")
        existing = selected.get(key)
        if existing is None:
            selected[key] = item
            continue
        existing_len = len(str(existing.get("full_text") or ""))
        new_len = len(str(item.get("full_text") or ""))
        if new_len > existing_len:
            selected[key] = item
    return list(selected.values())


def _group_failures_by_provider(failures: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for failure in list(failures or []):
        if not isinstance(failure, dict):
            continue
        provider = str(failure.get("source") or failure.get("provider") or "unknown").strip().lower()
        grouped[provider].append(dict(failure))
    return grouped


class ProviderQuorumService:
    """Canonical provider quorum executor for bibliotecario/runtime lanes.

    The service never hard-fails on one provider unless quorum policy fails.
    """

    def __init__(
        self,
        *,
        search_service: Optional[LiteratureSearchService] = None,
    ) -> None:
        self._search_service = search_service or LiteratureSearchService()

    async def close(self) -> None:
        close = getattr(self._search_service, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def run_quorum(
        self,
        *,
        spec: LiteratureQuerySpec,
        lane_class: str,
        preset_name: str,
        task_type: str,
        policy: Optional[ProviderQuorumPolicy] = None,
        enable_unpaywall_enrichment: bool = True,
    ) -> ProviderQuorumRuntimeResult:
        effective_policy = policy or ProviderQuorumPolicy()
        compiler = LiteratureProviderCompiler(
            lane_class=lane_class,
            preset_name=preset_name,
            openalex_available=True,
        )
        plan = compiler.compile_plan(spec)

        result = await self._search_service.search(
            query=spec.query,
            max_papers=spec.max_papers,
            sources=[item.value for item in list(plan.acquisition_order)],
            extra_queries=list(plan.extra_queries),
            session_id=spec.session_id,
            run_id=spec.run_id,
            user_id=spec.user_id,
            tenant_id=spec.tenant_id,
            acquisition_budget_usd=spec.acquisition_budget_usd,
        )

        failure_records = [dict(item) for item in list(result.failure_records or []) if isinstance(item, dict)]
        failures_by_provider = _group_failures_by_provider(failure_records)
        source_counts = dict(result.source_counts or {})

        provider_receipts: List[ProviderExecutionReceipt] = []
        attempted = list(result.attempted_sources or [])
        requested = [item.value for item in list(plan.requested_sources)]
        for source in list(plan.acquisition_order):
            provider = source.value
            provider_key = str(provider or "").strip().lower()
            provider_failures = list(failures_by_provider.get(provider_key) or [])
            provider_http = [
                int(item.get("status_code"))
                for item in provider_failures
                if str(item.get("status_code") or "").isdigit()
            ]
            paper_count = int(source_counts.get(provider_key, 0) or 0)
            attempted_flag = provider_key in attempted
            if provider_failures:
                reasons_lower = [str(item.get("error") or item.get("reason") or "").lower() for item in provider_failures]
                is_rate_limit = any(code == 429 for code in provider_http) or any("rate limit" in r or "429" in r for r in reasons_lower)
                is_timeout = any(code == 408 for code in provider_http) or any("timeout" in r for r in reasons_lower)
                is_parser = any("parse" in r or "json" in r for r in reasons_lower)
                is_unavailable = any("unavailable" in r for r in reasons_lower)
                is_failed = any(500 <= code < 600 for code in provider_http) or any("internal error" in r or "500" in r or "failed" in r for r in reasons_lower)
                
                if is_rate_limit:
                    status = "degraded_rate_limited"
                elif is_timeout:
                    status = "degraded_timeout"
                elif is_parser:
                    status = "degraded_parser_error"
                elif is_unavailable:
                    status = "unavailable"
                elif is_failed:
                    status = "failed"
                else:
                    status = "degraded" if effective_policy.allow_degraded_success else "failed"
            elif attempted_flag:
                if paper_count == 0:
                    status = "degraded_empty"
                else:
                    status = "success"
            else:
                if provider_key in requested:
                    status = "skipped_by_policy"
                else:
                    status = "not_attempted"
            provider_receipts.append(
                ProviderExecutionReceipt(
                    provider=provider_key,
                    attempted=attempted_flag,
                    status=status,
                    paper_count=paper_count,
                    failure_count=len(provider_failures),
                    failure_reasons=[str(item.get("error") or item.get("reason") or "provider_failure") for item in provider_failures],
                    http_statuses=provider_http,
                    degraded=bool(provider_failures) or status.startswith("degraded"),
                )
            )

        deduped_papers = _dedupe_by_identity(list(result.papers or []))
        if enable_unpaywall_enrichment:
            deduped_papers = await self._attach_unpaywall_metadata(deduped_papers)

        successful_provider_count = 0
        degraded_provider_count = 0
        blocked_provider_count = 0
        for item in provider_receipts:
            if item.status in {"failed", "unavailable"}:
                blocked_provider_count += 1
                continue
            if item.degraded or item.status.startswith("degraded"):
                degraded_provider_count += 1
            if effective_policy.require_nonempty_papers:
                if item.paper_count > 0:
                    successful_provider_count += 1
            elif item.attempted and item.status in {
                "ok", "success", "degraded", "degraded_rate_limited",
                "degraded_timeout", "degraded_empty", "degraded_parser_error",
                "skipped_by_policy"
            }:
                successful_provider_count += 1

        attempted_count = sum(1 for item in provider_receipts if item.attempted)
        blocked_reasons: List[str] = []
        if attempted_count < effective_policy.min_attempted_providers:
            blocked_reasons.append("insufficient_attempted_providers")
        if successful_provider_count < effective_policy.min_successful_providers:
            blocked_reasons.append("insufficient_successful_providers")

        status = "satisfied"
        if blocked_reasons:
            status = "blocked"
        elif degraded_provider_count > 0:
            status = "degraded"

        receipt = ProviderQuorumReceipt(
            query=spec.query,
            lane=spec.lane,
            task_type=task_type,
            query_spec_hash=spec.query_spec_hash,
            run_id=str(spec.run_id or ""),
            policy=effective_policy,
            requested_sources=requested,
            effective_sources=[item.value for item in list(plan.acquisition_order)],
            provider_receipts=provider_receipts,
            attempted_provider_count=attempted_count,
            successful_provider_count=successful_provider_count,
            degraded_provider_count=degraded_provider_count,
            blocked_provider_count=blocked_provider_count,
            total_papers=len(deduped_papers),
            status=status,
            quorum_satisfied=not blocked_reasons,
            blocked_reasons=blocked_reasons,
            failure_records=failure_records,
        )

        return ProviderQuorumRuntimeResult(
            receipt=receipt,
            papers=deduped_papers,
            result_payload={
                "source_counts": source_counts,
                "search_log": list(result.search_log or []),
                "request_envelope": dict(result.request_envelope or {}),
            },
        )

    async def _attach_unpaywall_metadata(self, papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        client = UnpaywallClient()
        try:
            for index, paper in enumerate(papers):
                if index >= 10:
                    break
                doi = str(paper.get("doi") or "").strip()
                if not doi:
                    continue
                record = await client.lookup(doi)
                if isinstance(record, dict) and record:
                    best_location = dict(record.get("best_oa_location") or {})
                    paper["unpaywall"] = {
                        "is_oa": bool(record.get("is_oa")),
                        "best_oa_url": str(best_location.get("url_for_pdf") or best_location.get("url") or ""),
                        "license": str(best_location.get("license") or ""),
                    }
            return papers
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
