from __future__ import annotations

import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mica.infrastructure.literature.fulltext_router import FullTextRouter
from mica.infrastructure.literature.persistence_policy import assess_persistence
from mica.infrastructure.literature.persistence_decision_artifact import (
    build_ingested_paper_decision,
    update_backend_status,
)
from mica.infrastructure.persistence.milvus_user_rag_store import MilvusUserRAGStore
from mica.infrastructure.persistence.timescale_user_rag_store import TimescaleUserRAGStore
from mica.infrastructure.literature.control_plane import LiteratureBudgetSnapshot, merge_metadata
from mica.memory.dlm.batch_mapper import BatchIngestionResult, DLMBatchMapper, IngestedPaper
from mica.storage.gcs_user_storage import GCSUserStorage, get_storage_manager, sanitize_object_prefix

logger = logging.getLogger(__name__)


def _chunk_text(text: str, *, max_chars: int = 1800, overlap: int = 200) -> List[str]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    out: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        seg = text[start:end].strip()
        if seg:
            out.append(seg)
        if end == len(text):
            break
        start = max(0, end - overlap)
    return out


@dataclass(frozen=True)
class UserLiteratureIngestionConfig:
    collection: str = "papers"
    work_dir_base: Path = Path("./outputs/api_v1/literature")
    max_concurrent: int = 10
    tenant_id: str = "default"
    acquisition_budget_usd: Optional[float] = None
    allow_paid_fulltext: bool = False


@dataclass(frozen=True)
class UserLiteratureIngestionSummary:
    user_id: str
    collection: str
    total_papers: int
    successful_ingestions: int
    failed_ingestions: int
    atom_snapshots_created: int
    total_quintuples: int
    rag_chunks_inserted: int
    milvus_chunks_inserted: int
    gcs_pdfs_uploaded: int
    persistence_eligible_papers: int
    persistence_filtered_papers: int
    requested_session_id: str
    requested_run_id: str
    budget_spent_usd: float
    budget_remaining_usd: Optional[float]
    persistence_decisions: List[Dict[str, Any]]
    canonical_papers: int = 0
    governance_summary: Dict[str, Any] = field(default_factory=dict)
    # Degradation truth — always present so callers never assume full-text success silently.
    degraded_count: int = 0
    fulltext_acquired_count: int = 0


class UserLiteratureIngestionService:
    """End-to-end literature ingestion (per user).

    Responsibilities:
    - Fetch papers via DLM
    - Download PDFs (optional)
    - Upload PDFs to per-user GCS bucket
    - Extract text (optional; via DLMBatchMapper)
    - Create ATOM snapshots (LLM or non-LLM; configured via DLMBatchMapper)
    - Persist user-scoped RAG chunks into TimescaleUserRAGStore
    """

    def __init__(
        self,
        *,
        config: Optional[UserLiteratureIngestionConfig] = None,
        storage: Optional[GCSUserStorage] = None,
        rag_store: Optional[TimescaleUserRAGStore] = None,
        milvus_store: Optional[MilvusUserRAGStore] = None,
        semantic_scholar_api_key: Optional[str] = None,
        fulltext_router: Optional[FullTextRouter] = None,
    ) -> None:
        self.config = config or UserLiteratureIngestionConfig()
        self.storage = storage or get_storage_manager()
        self.rag_store = rag_store or TimescaleUserRAGStore()
        self.milvus_store = milvus_store or MilvusUserRAGStore()
        self.semantic_scholar_api_key = semantic_scholar_api_key
        self.fulltext_router = fulltext_router or FullTextRouter()

    def _prepare_ingestion_context(
        self,
        *,
        gcs_object_prefix: Optional[str],
        session_id: Optional[str],
        run_id: Optional[str],
        tenant_id: Optional[str],
        acquisition_budget_usd: Optional[float],
        allow_paid_fulltext: Optional[bool],
    ) -> Tuple[Path, str, str, str, LiteratureBudgetSnapshot, bool]:
        self.config.work_dir_base.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="lit_", dir=str(self.config.work_dir_base)))

        safe_prefix = sanitize_object_prefix(gcs_object_prefix) if gcs_object_prefix else "literature/papers"
        safe_run_id = str(run_id or "").strip() or f"lit-ingest-{uuid.uuid4().hex[:12]}"
        safe_session_id = (session_id or "").strip() or safe_run_id
        budget = LiteratureBudgetSnapshot(
            tenant_id=str(tenant_id or self.config.tenant_id or "default"),
            max_budget_usd=acquisition_budget_usd if acquisition_budget_usd is not None else self.config.acquisition_budget_usd,
        )
        allow_paid = self.config.allow_paid_fulltext if allow_paid_fulltext is None else bool(allow_paid_fulltext)
        return work_dir, safe_prefix, safe_run_id, safe_session_id, budget, allow_paid

    @staticmethod
    def _compute_fulltext_counts(papers: Sequence[IngestedPaper]) -> Tuple[int, int]:
        fulltext_acquired_count = sum(1 for paper in list(papers or []) if bool(str(paper.full_text or "").strip()))
        degraded_count = max(0, len(list(papers or [])) - fulltext_acquired_count)
        return degraded_count, fulltext_acquired_count

    async def _finalize_ingestion_result(
        self,
        *,
        result: BatchIngestionResult,
        user_id: str,
        query: str,
        safe_prefix: str,
        safe_session_id: str,
        safe_run_id: str,
        budget: LiteratureBudgetSnapshot,
        allow_paid: bool,
        acquisition_ran: bool,
        run_fulltext_router_enrichment: bool,
    ) -> Tuple[UserLiteratureIngestionSummary, List[IngestedPaper]]:
        rag_chunks_inserted = 0
        milvus_chunks_inserted = 0
        gcs_pdfs_uploaded = 0
        persistence_eligible_papers = 0
        persistence_filtered_papers = 0
        persistence_decisions: List[Dict[str, Any]] = []
        governance_summary: Dict[str, Any] = {}

        if run_fulltext_router_enrichment:
            for paper in result.papers:
                await self._enrich_paper_with_fulltext(
                    paper,
                    user_id=user_id,
                    session_id=safe_session_id,
                    run_id=safe_run_id,
                    tenant_id=budget.tenant_id,
                    budget=budget,
                    allow_paid_fulltext=allow_paid,
                    require_cloud_evidence=True,
                )

        degraded_count, fulltext_acquired_count = self._compute_fulltext_counts(result.papers)

        governed_papers = await self._govern_ingested_papers(
            papers=list(result.papers),
            query=query,
            user_id=user_id,
            session_id=safe_session_id,
            run_id=safe_run_id,
            tenant_id=budget.tenant_id,
            budget=budget,
            acquisition_lineage_present=acquisition_ran,
        )
        if governed_papers:
            governance_summary = dict(governed_papers.get("summary") or {})
            papers_to_persist = list(governed_papers.get("papers") or [])
        else:
            papers_to_persist = list(result.papers)

        for paper in papers_to_persist:

            assessment = self._assess_paper_persistence(paper)
            decision = build_ingested_paper_decision(paper, assessment)
            if not assessment.persistence_eligible:
                persistence_filtered_papers += 1
                update_backend_status(decision, "user_rag", "skipped", detail=assessment.persistence_reason)
                update_backend_status(decision, "milvus", "skipped", detail=assessment.persistence_reason)
                update_backend_status(decision, "gcs", "skipped", detail=assessment.persistence_reason)
                persistence_decisions.append(decision)
                logger.info(
                    "Skipping low-yield literature persistence for %s (%s)",
                    paper.get_unique_id(),
                    assessment.persistence_reason,
                )
                continue

            persistence_eligible_papers += 1
            rag_count = await self._persist_paper_to_user_rag(
                user_id=user_id,
                collection=self.config.collection,
                paper=paper,
                session_id=safe_session_id,
            )
            rag_chunks_inserted += rag_count
            update_backend_status(decision, "user_rag", "persisted" if rag_count else "skipped")

            milvus_count = await self._persist_paper_chunks_to_milvus(
                user_id=user_id,
                collection=self.config.collection,
                paper=paper,
                session_id=safe_session_id,
            )
            milvus_chunks_inserted += milvus_count
            update_backend_status(decision, "milvus", "persisted" if milvus_count else "skipped")

            if paper.pdf_path and paper.pdf_path.exists():
                doc_key = paper.get_unique_id()
                object_path = f"{safe_prefix}/{doc_key}/{paper.pdf_path.name}"
                try:
                    _ = self.storage.upload_file(
                        user_id=user_id,
                        object_path=object_path,
                        local_path=paper.pdf_path,
                        content_type="application/pdf",
                    )
                    gcs_pdfs_uploaded += 1
                    update_backend_status(decision, "gcs", "persisted", detail=object_path)
                except Exception as exc:
                    logger.warning("Failed to upload PDF to GCS (user=%s doc=%s): %s", user_id, doc_key, exc)
                    update_backend_status(decision, "gcs", "error", detail=str(exc))
            else:
                update_backend_status(decision, "gcs", "skipped", detail="no_pdf_path")

            persistence_decisions.append(decision)

        return (
            UserLiteratureIngestionSummary(
                user_id=user_id,
                collection=self.config.collection,
                total_papers=result.total_papers,
                successful_ingestions=result.successful_ingestions,
                failed_ingestions=result.failed_ingestions,
                atom_snapshots_created=result.atom_snapshots_created,
                total_quintuples=result.total_quintuples,
                rag_chunks_inserted=rag_chunks_inserted,
                milvus_chunks_inserted=milvus_chunks_inserted,
                gcs_pdfs_uploaded=gcs_pdfs_uploaded,
                persistence_eligible_papers=persistence_eligible_papers,
                persistence_filtered_papers=persistence_filtered_papers,
                requested_session_id=safe_session_id,
                requested_run_id=safe_run_id,
                budget_spent_usd=budget.spent_usd,
                budget_remaining_usd=budget.remaining_usd,
                persistence_decisions=persistence_decisions,
                canonical_papers=len(papers_to_persist),
                governance_summary=governance_summary,
                degraded_count=degraded_count,
                fulltext_acquired_count=fulltext_acquired_count,
            ),
            papers_to_persist,
        )

    async def ingest_query(
        self,
        *,
        user_id: str,
        query: str,
        max_papers: int,
        download_pdfs: bool,
        extract_full_text: bool,
        enable_atom: bool,
        atom_backend: str,
        atom_timescale_dsn: Optional[str],
        atom_enable_llm: bool,
        atom_llm_provider: str,
        atom_llm_model_facts: str,
        filters: Optional[Dict[str, Any]] = None,
        s2_filters: Optional[Dict[str, Any]] = None,
        gcs_object_prefix: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        acquisition_budget_usd: Optional[float] = None,
        allow_paid_fulltext: Optional[bool] = None,
    ) -> UserLiteratureIngestionSummary:
        """Source-neutral canonical acquisition entry point.

        ``filters`` is the preferred parameter; ``s2_filters`` is a backwards-compatible
        alias that is promoted to ``filters`` when ``filters`` is absent.
        """
        canonical_filters = filters if filters is not None else s2_filters
        if not user_id:
            raise ValueError("user_id required")
        if not query.strip():
            raise ValueError("query required")
        work_dir, safe_prefix, safe_run_id, safe_session_id, budget, allow_paid = self._prepare_ingestion_context(
            gcs_object_prefix=gcs_object_prefix,
            session_id=session_id,
            run_id=run_id,
            tenant_id=tenant_id,
            acquisition_budget_usd=acquisition_budget_usd,
            allow_paid_fulltext=allow_paid_fulltext,
        )

        # Run the DLM/ATOM pipeline.
        async with DLMBatchMapper(
            semantic_scholar_api_key=self.semantic_scholar_api_key,
            output_dir=work_dir,
            max_concurrent=self.config.max_concurrent,
            enable_atom=enable_atom,
            atom_persistence_backend=atom_backend,
            atom_timescale_dsn=atom_timescale_dsn,
            atom_enable_llm=atom_enable_llm,
            atom_llm_provider=atom_llm_provider,
            atom_llm_model_facts=atom_llm_model_facts,
        ) as mapper:
            result: BatchIngestionResult = await mapper.ingest_from_semantic_scholar(
                query=query,
                max_papers=max_papers,
                download_pdfs=download_pdfs,
                extract_full_text=extract_full_text,
                filters=canonical_filters,
                user_id=user_id,
            )
        summary, _ = await self._finalize_ingestion_result(
            result=result,
            user_id=user_id,
            query=query,
            safe_prefix=safe_prefix,
            safe_session_id=safe_session_id,
            safe_run_id=safe_run_id,
            budget=budget,
            allow_paid=allow_paid,
            acquisition_ran=bool(download_pdfs or extract_full_text or allow_paid),
            run_fulltext_router_enrichment=bool(download_pdfs or extract_full_text or allow_paid),
        )
        return summary

    async def _ingest_prepared_papers_execution(
        self,
        *,
        user_id: str,
        query: str,
        papers: Sequence[Dict[str, Any]],
        download_pdfs: bool,
        extract_full_text: bool,
        enable_atom: bool,
        atom_backend: str,
        atom_timescale_dsn: Optional[str],
        atom_enable_llm: bool,
        atom_llm_provider: str,
        atom_llm_model_facts: str,
        gcs_object_prefix: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        acquisition_budget_usd: Optional[float] = None,
        allow_paid_fulltext: Optional[bool] = None,
    ) -> Tuple[UserLiteratureIngestionSummary, List[IngestedPaper]]:
        if not user_id:
            raise ValueError("user_id required")
        if not query.strip():
            raise ValueError("query required")
        work_dir, safe_prefix, safe_run_id, safe_session_id, budget, allow_paid = self._prepare_ingestion_context(
            gcs_object_prefix=gcs_object_prefix,
            session_id=session_id,
            run_id=run_id,
            tenant_id=tenant_id,
            acquisition_budget_usd=acquisition_budget_usd,
            allow_paid_fulltext=allow_paid_fulltext,
        )
        async with DLMBatchMapper(
            semantic_scholar_api_key=self.semantic_scholar_api_key,
            output_dir=work_dir,
            max_concurrent=self.config.max_concurrent,
            enable_atom=enable_atom,
            atom_persistence_backend=atom_backend,
            atom_timescale_dsn=atom_timescale_dsn,
            atom_enable_llm=atom_enable_llm,
            atom_llm_provider=atom_llm_provider,
            atom_llm_model_facts=atom_llm_model_facts,
        ) as mapper:
            result: BatchIngestionResult = await mapper.ingest_prepared_papers(
                papers=papers,
                download_pdfs=download_pdfs,
                extract_full_text=extract_full_text,
                user_id=user_id,
            )

        acquisition_ran = any(
            bool(str(paper.full_text or "").strip()) or paper.pdf_path or (paper.metadata or {}).get("fulltext_router")
            for paper in result.papers
        )
        return await self._finalize_ingestion_result(
            result=result,
            user_id=user_id,
            query=query,
            safe_prefix=safe_prefix,
            safe_session_id=safe_session_id,
            safe_run_id=safe_run_id,
            budget=budget,
            allow_paid=allow_paid,
            acquisition_ran=acquisition_ran,
            run_fulltext_router_enrichment=False,
        )

    async def ingest_prepared_papers(
        self,
        *,
        user_id: str,
        query: str,
        papers: Sequence[Dict[str, Any]],
        download_pdfs: bool,
        extract_full_text: bool,
        enable_atom: bool,
        atom_backend: str,
        atom_timescale_dsn: Optional[str],
        atom_enable_llm: bool,
        atom_llm_provider: str,
        atom_llm_model_facts: str,
        gcs_object_prefix: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        acquisition_budget_usd: Optional[float] = None,
        allow_paid_fulltext: Optional[bool] = None,
    ) -> UserLiteratureIngestionSummary:
        summary, _ = await self._ingest_prepared_papers_execution(
            user_id=user_id,
            query=query,
            papers=papers,
            download_pdfs=download_pdfs,
            extract_full_text=extract_full_text,
            enable_atom=enable_atom,
            atom_backend=atom_backend,
            atom_timescale_dsn=atom_timescale_dsn,
            atom_enable_llm=atom_enable_llm,
            atom_llm_provider=atom_llm_provider,
            atom_llm_model_facts=atom_llm_model_facts,
            gcs_object_prefix=gcs_object_prefix,
            session_id=session_id,
            run_id=run_id,
            tenant_id=tenant_id,
            acquisition_budget_usd=acquisition_budget_usd,
            allow_paid_fulltext=allow_paid_fulltext,
        )
        return summary

    async def ingest_semantic_scholar_query(
        self,
        *,
        user_id: str,
        query: str,
        max_papers: int,
        download_pdfs: bool,
        extract_full_text: bool,
        enable_atom: bool,
        atom_backend: str,
        atom_timescale_dsn: Optional[str],
        atom_enable_llm: bool,
        atom_llm_provider: str,
        atom_llm_model_facts: str,
        s2_filters: Optional[Dict[str, Any]] = None,
        gcs_object_prefix: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        acquisition_budget_usd: Optional[float] = None,
        allow_paid_fulltext: Optional[bool] = None,
    ) -> UserLiteratureIngestionSummary:
        """Backwards-compatibility alias for ``ingest_query``; prefer ``ingest_query`` for new call sites."""
        return await self.ingest_query(
            user_id=user_id,
            query=query,
            max_papers=max_papers,
            download_pdfs=download_pdfs,
            extract_full_text=extract_full_text,
            enable_atom=enable_atom,
            atom_backend=atom_backend,
            atom_timescale_dsn=atom_timescale_dsn,
            atom_enable_llm=atom_enable_llm,
            atom_llm_provider=atom_llm_provider,
            atom_llm_model_facts=atom_llm_model_facts,
            s2_filters=s2_filters,
            gcs_object_prefix=gcs_object_prefix,
            session_id=session_id,
            run_id=run_id,
            tenant_id=tenant_id,
            acquisition_budget_usd=acquisition_budget_usd,
            allow_paid_fulltext=allow_paid_fulltext,
        )

    async def _govern_ingested_papers(
        self,
        *,
        papers: List[IngestedPaper],
        query: str,
        user_id: str,
        session_id: str,
        run_id: str,
        tenant_id: str,
        budget: LiteratureBudgetSnapshot,
        acquisition_lineage_present: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Run governance over already-acquired papers.

        ``acquisition_lineage_present`` must be ``True`` when the caller has already
        run fulltext acquisition upstream (e.g. via ``_enrich_paper_with_fulltext``
        inside ``ingest_query``).  When ``True``, governance skips re-hydration
        because acquisition evidence is already in ``paper.metadata["fulltext_router"]``.
        When ``False``, governance is allowed to hydrate so papers are not permanently
        stranded without text — but a warning is emitted because the canonical path
        requires acquisition to run first.
        """
        if not papers:
            return None
        if not acquisition_lineage_present:
            logger.warning(
                "_govern_ingested_papers called without prior acquisition lineage "
                "(%d papers). Governance will attempt hydration. "
                "Prefer running acquisition via ingest_query before governance.",
                len(papers),
            )
        from mica.services.literature_search_service import LiteratureSearchService

        # enable_fulltext_hydration is the inverse of acquisition_lineage_present:
        # if acquisition already ran upstream, skip re-hydration inside governance;
        # if acquisition did not run, allow governance to hydrate.
        service = LiteratureSearchService(
            fulltext_router=self.fulltext_router,
            enable_fulltext_hydration=not acquisition_lineage_present,
        )
        try:
            governed_dicts, summary = await service.govern_paper_corpus(
                query=query,
                papers=[self._ingested_paper_to_dict(paper) for paper in papers],
                session_id=session_id,
                run_id=run_id,
                user_id=user_id,
                tenant_id=tenant_id,
                acquisition_budget_usd=budget.max_budget_usd,
                budget_spent_usd=budget.spent_usd,
                document_scan_limit=None,
                promote_to_graph=True,
            )
            governed_papers = self._apply_governed_papers(
                original_papers=papers,
                governed_papers=governed_dicts,
                service=service,
            )
            graph_summary = dict(summary.get("paper_graph_promotion") or {})
            if graph_summary.get("promoted_papers"):
                budget.record(kind="paper_graph_promotion", amount_usd=0.0, accepted=True, detail=str(graph_summary.get("promoted_papers")))
            return {"papers": governed_papers, "summary": summary}
        finally:
            await service.close()

    @staticmethod
    def _ingested_paper_to_dict(paper: IngestedPaper) -> Dict[str, Any]:
        metadata = dict(paper.metadata or {})
        return {
            "paperId": paper.paper_id,
            "canonical_id": paper.get_unique_id(),
            "source": paper.platform,
            "provider": paper.platform,
            "title": paper.title,
            "abstract": paper.abstract,
            "full_text": paper.full_text,
            "year": paper.year,
            "authors": [{"name": author} for author in list(paper.authors or []) if str(author or "").strip()],
            "doi": paper.doi,
            "pmid": paper.pmid,
            "arxivId": paper.arxiv_id,
            "externalIds": {
                "DOI": paper.doi,
                "PubMed": paper.pmid,
                "ArXiv": paper.arxiv_id,
            },
            "metadata": metadata,
        }

    @staticmethod
    def _lookup_keys_for_ingested(paper: IngestedPaper) -> List[str]:
        return [
            str(paper.get_unique_id() or "").strip(),
            str(paper.paper_id or "").strip(),
            str(paper.title or "").strip(),
        ]

    @staticmethod
    def _lookup_keys_for_governed(paper: Dict[str, Any]) -> List[str]:
        return [
            str(paper.get("canonical_id") or "").strip(),
            str(paper.get("paperId") or "").strip(),
            str(paper.get("title") or "").strip(),
        ]

    def _apply_governed_papers(
        self,
        *,
        original_papers: List[IngestedPaper],
        governed_papers: List[Dict[str, Any]],
        service: Any,
    ) -> List[IngestedPaper]:
        governed_lookup: Dict[str, Dict[str, Any]] = {}
        for governed in list(governed_papers or []):
            if not isinstance(governed, dict):
                continue
            for key in self._lookup_keys_for_governed(governed):
                if key:
                    governed_lookup[key] = governed

        updated: List[IngestedPaper] = []
        seen: set[str] = set()
        for paper in original_papers:
            governed: Optional[Dict[str, Any]] = None
            for key in self._lookup_keys_for_ingested(paper):
                if key and key in governed_lookup:
                    governed = governed_lookup[key]
                    break
            if governed is None:
                updated.append(paper)
                continue
            canonical_id = str(governed.get("canonical_id") or paper.get_unique_id() or "").strip()
            if canonical_id and canonical_id in seen:
                continue
            if canonical_id:
                seen.add(canonical_id)
            paper.abstract = str(governed.get("abstract") or paper.abstract or "") or None
            paper.full_text = str(governed.get("full_text") or paper.full_text or "") or None
            paper.metadata = merge_metadata(dict(paper.metadata or {}), dict(governed.get("metadata") or {}))
            if paper.full_text:
                paper.structured_paper = service._build_structured_paper(governed)
            updated.append(paper)
        return updated

    async def _enrich_paper_with_fulltext(
        self,
        paper: IngestedPaper,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        tenant_id: str,
        budget: LiteratureBudgetSnapshot,
        allow_paid_fulltext: bool,
        require_cloud_evidence: bool,
    ) -> None:
        metadata = merge_metadata(
            dict(paper.metadata or {}),
            {
                "owner_id": user_id,
                "user_id": user_id,
                "session_id": session_id,
                "run_id": run_id,
                "tenant_id": tenant_id,
                "acquisition_budget_usd": budget.max_budget_usd,
                "budget_spent_usd": budget.spent_usd,
                "allow_paid_fulltext": allow_paid_fulltext,
                "allow_paid_openalex": allow_paid_fulltext,
                "artifact_grade_literature": True,
                "require_authenticated_user_owner": True,
                "require_cloud_evidence": require_cloud_evidence,
            },
        )
        try:
            doc = await self.fulltext_router.acquire_single(
                paper_id=paper.paper_id,
                doi=paper.doi or "",
                pmid=paper.pmid or "",
                title=paper.title,
                abstract=paper.abstract or "",
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("Full-text enrichment skipped for %s: %s", paper.get_unique_id(), exc)
            return

        if doc.full_text and len(doc.full_text) >= len(paper.full_text or ""):
            paper.full_text = doc.full_text
        if doc.abstract:
            paper.abstract = doc.abstract
        paper.metadata = merge_metadata(
            dict(paper.metadata or {}),
            {
                "fulltext_router": doc.to_dict(),
                "acquisition_audit": list(doc.acquisition_audit),
                "budget": dict(doc.metadata.get("budget") or {}),
            },
        )
        budget.spent_usd = round(float((doc.metadata.get("budget") or {}).get("spent_usd") or budget.spent_usd), 6)

    def _assess_paper_persistence(self, paper: IngestedPaper):
        structured_sections = len(getattr(getattr(paper, "structured_paper", None), "sections", []) or [])
        degraded = not bool(paper.full_text)
        return assess_persistence(
            text=paper.full_text or paper.abstract or paper.title or "",
            sections_count=structured_sections or (1 if (paper.full_text or paper.abstract or paper.title) else 0),
            citation_count=0,
            degraded=degraded,
        )

    async def _persist_paper_to_user_rag(self, *, user_id: str, collection: str, paper: IngestedPaper, session_id: str) -> int:
        doc_key = paper.get_unique_id()
        content = paper.full_text or paper.abstract or paper.title or ""
        if not content:
            return 0

        external_ids: Dict[str, Any] = {
            "paper_id": paper.paper_id,
            "platform": paper.platform,
            "doi": paper.doi,
            "pmid": paper.pmid,
            "arxiv_id": paper.arxiv_id,
        }
        metadata: Dict[str, Any] = {
            "year": paper.year,
            "authors": paper.authors,
            "pdf_downloaded": paper.pdf_downloaded,
            "has_full_text": bool(paper.full_text),
            **dict(paper.metadata or {}),
        }

        await self.rag_store.upsert_document(
            user_id=user_id,
            collection=collection,
            doc_key=doc_key,
            title=paper.title,
            content=content,
            source=paper.platform,
            external_ids=external_ids,
            metadata=metadata,
        )

        chunks = _chunk_text(content)
        for idx, chunk in enumerate(chunks):
            await self.rag_store.insert_chunk(
                user_id=user_id,
                collection=collection,
                doc_key=doc_key,
                chunk_index=idx,
                content=chunk,
                embedding=None,  # embeddings can be added later (Milvus wiring phase)
                source=paper.platform,
                metadata={
                    **external_ids,
                    "title": paper.title,
                    "year": paper.year,
                    "session_id": session_id,
                    **dict(paper.metadata or {}),
                },
            )

        return len(chunks)

    async def _persist_paper_chunks_to_milvus(self, *, user_id: str, collection: str, paper: IngestedPaper, session_id: str) -> int:
        """Best-effort derived index in Milvus/Zilliz.

        This is intentionally non-fatal; Timescale remains the source of truth.
        """

        doc_key = paper.get_unique_id()
        content = paper.full_text or paper.abstract or paper.title or ""
        if not content:
            return 0

        chunks = _chunk_text(content)
        inserted = 0
        for idx, chunk in enumerate(chunks):
            try:
                ok = await self.milvus_store.insert_chunk(
                    user_id=user_id,
                    collection=collection,
                    doc_key=doc_key,
                    chunk_index=idx,
                    content=chunk,
                    source=paper.platform,
                    session_id=session_id,
                    metadata={
                        "paper_id": paper.paper_id,
                        "platform": paper.platform,
                        "doi": paper.doi,
                        "pmid": paper.pmid,
                        "arxiv_id": paper.arxiv_id,
                        "title": paper.title,
                        "year": paper.year,
                        "explicit_user_ingest": True,
                        **dict(paper.metadata or {}),
                    },
                )
                if ok:
                    inserted += 1
            except Exception as exc:
                logger.debug("Milvus insert skipped (user=%s doc=%s): %s", user_id, doc_key, exc)
        return inserted
