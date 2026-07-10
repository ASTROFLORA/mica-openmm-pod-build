"""LiteratureQueryFacadeService — Iteration 08 unified lane dispatcher.

Accepts a ``LiteratureQueryFacadeRequest`` and routes to the correct
execution lane service without any router imports in this module.

Lane routing table
------------------
  "ingest"        → literature_ingest_service.run_literature_ingest
  "deep_research" → deep_research_service.run_deep_research
  "bibliotecario" → bibliotecario_service.run_bibliotecario_scan

All lane service imports are lazy (inside dispatch methods) so that a
broken optional dependency in one lane never prevents the others from loading.

Iteration 09 — iterative intelligence stub
------------------------------------------
When ``request.iteration_budget > 1``, ``dispatch()`` emits one
``IterativeIntelligenceDirective`` per iteration and accumulates them into
an ``IterativeLiteratureSession`` attached to the result.  No real LLM call
is made — this is the typed contract stub required for Phase I closure.

Invariants
----------
- ProviderCompiler is never called here — it lives inside each lane service.
- This service never imports from any api_v1.routers module.
- ``services/__init__.py`` exports this under a ModuleNotFoundError guard.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from mica.literature_consolidation.contracts.query_facade import (
    LiteratureQueryFacadeRequest,
    LiteratureQueryFacadeResult,
    VALID_LANES,
)

logger = logging.getLogger("mica.literature_consolidation.services.query_facade_service")


class LiteratureQueryFacadeService:
    """Stateless dispatcher: routes ``LiteratureQueryFacadeRequest`` to lane services.

    Instantiate once (or per-request — state-free). Call ``dispatch()`` to run.
    """

    async def dispatch(
        self,
        request: LiteratureQueryFacadeRequest,
        *,
        user_id: str,
    ) -> LiteratureQueryFacadeResult:
        """Dispatch to the correct lane service.

        When ``request.iteration_budget > 1`` the method runs up to
        ``iteration_budget`` acquire-then-refine cycles, each emitting one
        ``IterativeIntelligenceDirective`` (stub — no real LLM call).
        The session terminates early when ``should_continue`` becomes ``False``
        (i.e. when the budget cap is reached or the iteration limit is hit).

        Args:
            request: Validated facade request with ``lane`` discriminator.
            user_id: Authenticated caller identity (injected from HTTP layer).

        Returns:
            ``LiteratureQueryFacadeResult`` with ``query_spec_hash``,
            ``protocol_version``, ``run_id``, ``lane_used``, and ``papers_fetched``
            always populated.  ``iterative_session`` is populated (not None)
            when ``iteration_budget > 1``.

        Raises:
            ValueError: If ``request.lane`` is not a recognized lane.
        """
        if request.lane not in VALID_LANES:
            raise ValueError(
                f"Unknown lane: {request.lane!r}. Valid lanes: {sorted(VALID_LANES)}"
            )

        run_id = request.run_id or f"facade-{uuid.uuid4().hex[:12]}"

        # Single-shot (default): dispatch once, no iterative session.
        if request.iteration_budget <= 1:
            return await self._dispatch_lane(request, user_id=user_id, run_id=run_id)

        # Iterative mode: run up to iteration_budget cycles.
        return await self._dispatch_iterative(request, user_id=user_id, run_id=run_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch_lane(
        self,
        request: LiteratureQueryFacadeRequest,
        *,
        user_id: str,
        run_id: str,
    ) -> LiteratureQueryFacadeResult:
        """Route to the correct lane dispatcher. Pure internal redirect."""
        if request.lane == "ingest":
            return await self._dispatch_ingest(request, user_id=user_id, run_id=run_id)
        elif request.lane == "deep_research":
            return await self._dispatch_deep_research(request, user_id=user_id, run_id=run_id)
        else:
            return await self._dispatch_bibliotecario(request, user_id=user_id, run_id=run_id)

    async def _dispatch_iterative(
        self,
        request: LiteratureQueryFacadeRequest,
        *,
        user_id: str,
        run_id: str,
    ) -> LiteratureQueryFacadeResult:
        """Run up to ``iteration_budget`` acquire-then-refine cycles (stub mode).

        Each cycle:
          1. Calls the real lane service once.
          2. Builds a ``IterativeIntelligenceDirective`` skeleton (no LLM).
          3. Appends to the ``IterativeLiteratureSession``.
          4. Stops when budget is exhausted or ``should_continue == False``.

        The final result carries the last lane result + ``iterative_session``.
        """
        from mica.literature_consolidation.contracts.iterative_intelligence import (  # noqa: PLC0415
            IterativeIntelligenceDirective,
            IterativeLiteratureSession,
        )

        session_id = request.session_id or f"isess-{uuid.uuid4().hex[:12]}"
        session = IterativeLiteratureSession(session_id=session_id)

        budget_remaining = request.acquisition_budget_usd  # may be None

        last_result: LiteratureQueryFacadeResult | None = None

        for iteration in range(request.iteration_budget):
            iter_run_id = f"{run_id}-i{iteration + 1}"
            last_result = await self._dispatch_lane(request, user_id=user_id, run_id=iter_run_id)

            is_last_iteration = iteration >= request.iteration_budget - 1
            should_continue = not is_last_iteration  # stub: stop at budget cap

            # Decrement budget stub (proportional to papers_fetched — placeholder math).
            if budget_remaining is not None and last_result.papers_fetched > 0:
                budget_remaining = max(
                    0.0, budget_remaining - last_result.papers_fetched * 0.001
                )

            directive = IterativeIntelligenceDirective(
                re_query_terms=[],          # stub: no LLM re-query generation
                budget_remaining_usd=budget_remaining,
                contradictions_found=[],    # stub: no contradiction detector
                quality_score=0.0,          # stub: no quality model
                should_continue=should_continue,
            )
            session.append_directive(directive)

            if not should_continue:
                break

        # Set final verdict based on how the loop closed.
        if session.iteration_count >= request.iteration_budget:
            session.final_verdict = "max_iterations_reached"
        elif budget_remaining is not None and budget_remaining <= 0.0:
            session.final_verdict = "budget_exhausted"
        else:
            session.final_verdict = "max_iterations_reached"

        assert last_result is not None  # loop ran at least once
        return last_result.model_copy(
            update={
                "run_id": run_id,
                "iterative_session": session,
            }
        )

    # ------------------------------------------------------------------
    # Lane dispatchers (lazy imports, no router dependencies)
    # ------------------------------------------------------------------

    async def _dispatch_ingest(
        self,
        request: LiteratureQueryFacadeRequest,
        *,
        user_id: str,
        run_id: str,
    ) -> LiteratureQueryFacadeResult:
        from mica.literature_consolidation.services.literature_ingest_service import (  # noqa: PLC0415
            LiteratureIngestExecutionRequest,
            run_literature_ingest,
        )

        lane_req = LiteratureIngestExecutionRequest(
            query=request.query,
            max_papers=request.max_papers,
            download_pdfs=request.download_pdfs,
            extract_full_text=request.extract_full_text,
            acquisition_budget_usd=request.acquisition_budget_usd,
            session_id=request.session_id,
            run_id=run_id,
            enable_atom=request.enable_atom,
            atom_backend=request.atom_backend,
            filters=request.filters,
        )

        result: Dict[str, Any] = await run_literature_ingest(lane_req, user_id)
        return LiteratureQueryFacadeResult(
            ok=True,
            lane_used="ingest",
            run_id=run_id,
            query_spec_hash=str(result.get("query_spec_hash") or ""),
            protocol_version=str(result.get("protocol_version") or "1.0"),
            papers_fetched=int(
                result.get("papers_fetched")
                or result.get("papers_ingested")
                or 0
            ),
            payload=result,
        )

    async def _dispatch_deep_research(
        self,
        request: LiteratureQueryFacadeRequest,
        *,
        user_id: str,
        run_id: str,
    ) -> LiteratureQueryFacadeResult:
        from mica.literature_consolidation.services.deep_research_service import (  # noqa: PLC0415
            DeepResearchExecutionRequest,
            run_deep_research,
        )

        lane_req = DeepResearchExecutionRequest(
            query=request.query,
            max_papers=request.max_papers,
            entities=request.entities,
            citation_depth=request.citation_depth,
            sources=request.sources,
            download_pdfs=request.download_pdfs,
            enable_atom_ingestion=request.enable_atom_ingestion,
            session_id=request.session_id,
            user_id=user_id,
            acquisition_budget_usd=request.acquisition_budget_usd,
        )

        result: Dict[str, Any] = await run_deep_research(lane_req)
        return LiteratureQueryFacadeResult(
            ok=True,
            lane_used="deep_research",
            run_id=run_id,
            query_spec_hash=str(result.get("query_spec_hash") or ""),
            protocol_version=str(result.get("protocol_version") or "1.0"),
            papers_fetched=int(
                result.get("paper_count")
                or result.get("papers_fetched")
                or 0
            ),
            payload=result,
        )

    async def _dispatch_bibliotecario(
        self,
        request: LiteratureQueryFacadeRequest,
        *,
        user_id: str,
        run_id: str,
    ) -> LiteratureQueryFacadeResult:
        from mica.literature_consolidation.services.bibliotecario_service import (  # noqa: PLC0415
            BibliotecarioScanExecutionRequest,
            _bib_scan_jobs,
            run_bibliotecario_scan,
        )

        job_id = f"bib-facade-{uuid.uuid4().hex[:12]}"

        lane_req = BibliotecarioScanExecutionRequest(
            query=request.query,
            max_papers=request.max_papers,
            entities=request.entities,
            sources=request.sources,
            preset=request.preset,
            session_id=request.session_id,
            run_id=run_id,
            user_id=user_id,
        )

        await run_bibliotecario_scan(job_id, lane_req)

        final: Dict[str, Any] = dict(_bib_scan_jobs.get(job_id) or {})
        inner_result: Dict[str, Any] = dict(final.get("result") or {})

        papers_fetched = int(
            inner_result.get("total_papers")
            or inner_result.get("paper_count")
            or len(inner_result.get("papers") or [])
            or 0
        )

        return LiteratureQueryFacadeResult(
            ok=True,
            lane_used="bibliotecario",
            run_id=run_id,
            job_id=job_id,
            query_spec_hash=str(final.get("query_spec_hash") or ""),
            protocol_version=str(final.get("protocol_version") or "1.0"),
            papers_fetched=papers_fetched,
            payload=final,
        )
