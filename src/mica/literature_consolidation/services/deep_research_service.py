from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from mica.literature_consolidation.contracts.query_protocol import LiteratureQueryResult, LiteratureQuerySpec
from mica.literature_consolidation.provider_compiler import LiteratureProviderCompiler
from mica.literature_consolidation.pipeline import (
    best_available_literature_text,
    build_canonical_literature_bundle,
)
from mica.memory.dlm.encoder import DLMEncoder
from mica.memory.dlm.structured_paper import build_structured_paper


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000.0, 3)


class DeepResearchExecutionRequest(BaseModel):
    query: str = Field(...)
    entities: List[str] = Field(default_factory=list)
    max_papers: int = Field(500, ge=1, le=10000)
    citation_depth: int = Field(1, ge=0, le=3)
    sources: List[str] = Field(default_factory=list)
    download_pdfs: bool = False
    enable_atom_ingestion: bool = True
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0)


async def run_deep_research(payload: DeepResearchExecutionRequest) -> Dict[str, Any]:
    from mica.infrastructure.literature.control_plane import (
        default_tenant_id_for_user,
    )
    from mica.services.literature_search_service import LiteratureSearchService

    request_started = perf_counter()
    runtime_profile: Dict[str, Any] = {
        "stages": {
            "protocol_compile_ms": 0.0,
            "service_init_ms": 0.0,
            "deep_search_ms": 0.0,
            "pdf_export_ms": 0.0,
            "atom_ingestion_ms": 0.0,
            "artifact_bundle_ms": 0.0,
            "persistence_ms": 0.0,
        },
        "atom_ingestion": {
            "enabled": bool(payload.enable_atom_ingestion),
            "status": "disabled" if not payload.enable_atom_ingestion else "not_attempted",
            "attempted_documents": 0,
            "stored_documents": 0,
            "errors": 0,
            "classifier": {},
            "graph_prior": {
                "enabled": False,
                "mode": "explicit_disable_for_deep_research",
            },
            "llm": {
                "enabled": False,
                "mode": "explicit_disable_for_deep_research",
            },
        },
        "artifact_bundle": {
            "status": "not_attempted",
            "artifact_count": 0,
            "stages": {},
            "publication_assembly": {},
        },
        "persistence": {
            "status": "not_attempted",
        },
    }

    tenant_id = default_tenant_id_for_user(payload.user_id or "deep-research")

    # Build canonical QuerySpec for protocol traceability BEFORE execution.
    compile_started = perf_counter()
    spec = LiteratureQuerySpec.from_deep_research_request(payload, tenant_id=tenant_id)
    compiler = LiteratureProviderCompiler(
        lane_class="deep_research",
        preset_name="deep_research",
        openalex_available=True,
    )
    plan_obj = compiler.compile_plan(spec)
    plan = plan_obj.to_dict()
    runtime_profile["stages"]["protocol_compile_ms"] = _elapsed_ms(compile_started)

    service_started = perf_counter()
    service = LiteratureSearchService()
    runtime_profile["stages"]["service_init_ms"] = _elapsed_ms(service_started)

    try:
        deep_search_started = perf_counter()
        deep_result = await service.deep_search(
            query=plan_obj.query,
            entities=list(plan_obj.extra_queries or payload.entities),
            max_papers=int(plan_obj.max_papers or payload.max_papers),
            citation_depth=payload.citation_depth,
            sources=tuple(source.value for source in plan_obj.acquisition_order)
            or ("semantic_scholar", "pubmed", "openalex"),
            promote_canonical_papers=payload.enable_atom_ingestion,
            enable_governance_enrichment=payload.enable_atom_ingestion,
            retrieval_policy=plan,
            session_id=payload.session_id,
            user_id=payload.user_id or "deep-research",
            tenant_id=tenant_id,
            acquisition_budget_usd=payload.acquisition_budget_usd,
        )
        runtime_profile["stages"]["deep_search_ms"] = _elapsed_ms(deep_search_started)
        search_log: List[str] = list(deep_result.search_log)
        search_log.insert(
            0,
            "[policy] lane=deep_research sources="
            + ",".join(source.value for source in plan_obj.acquisition_order)
            + f" max={plan_obj.max_papers}",
        )
        for degraded in list(plan.get("degraded_sources") or []):
            if isinstance(degraded, dict):
                search_log.append(
                    "[policy-degrade] "
                    + str(degraded.get("source") or "unknown")
                    + ": "
                    + str(degraded.get("reason") or "degraded")
                )
        citation_graph: Dict[str, List[str]] = dict(deep_result.citation_graph)
        papers_by_id: Dict[str, Dict[str, Any]] = {
            str(p.get("paperId") or p.get("canonical_id") or p.get("title") or ""): p
            for p in deep_result.papers
            if str(p.get("paperId") or p.get("canonical_id") or p.get("title") or "")
        }

        if payload.download_pdfs and payload.session_id:
            pdf_export_started = perf_counter()
            # Route through canonical acquisition contract — never bypass FullTextRouter
            # with a raw pdf_url → workspace write.  acquisition_lineage_present=True is
            # guaranteed here because this block only runs when the caller requested
            # download_pdfs=True, meaning the acquisition intent is explicit.
            search_log.append("[pdf-export] starting post-acquisition PDF export via FullTextRouter")
            from mica.infrastructure.literature.fulltext_router import FullTextRouter
            from mica.literature_consolidation.contracts.fulltext_acquisition import (
                FullTextAcquisitionRequest,
                PaperRef,
            )

            refs = [
                PaperRef(
                    paper_id=pid,
                    title=str(paper.get("title") or ""),
                    abstract=str(paper.get("abstract") or ""),
                    metadata={
                        "pdf_url": paper.get("pdf_url"),
                        "session_id": payload.session_id,
                        "user_id": payload.user_id or "deep-research",
                    },
                )
                for pid, paper in papers_by_id.items()
                if paper.get("pdf_url")
            ]
            if refs:
                acq_request = FullTextAcquisitionRequest(
                    mode="batch",
                    paper_refs=refs,
                    session_id=payload.session_id,
                    user_id=payload.user_id or "deep-research",
                    tenant_id=default_tenant_id_for_user(payload.user_id or "deep-research"),
                    acquisition_budget_usd=payload.acquisition_budget_usd,
                    max_items=len(refs),
                )
                router = FullTextRouter()
                acq_result = await router.execute(acq_request)
                search_log.append(
                    f"[pdf-export] acquired {acq_result.acquired_count}/{acq_result.requested_count}"
                    f" papers; degraded={acq_result.degraded_count}"
                )
            else:
                search_log.append("[pdf-export] no papers with pdf_url — nothing to acquire")
            runtime_profile["stages"]["pdf_export_ms"] = _elapsed_ms(pdf_export_started)

        atom_stored = 0
        if payload.enable_atom_ingestion:
            atom_ingestion_started = perf_counter()
            atom_profile = runtime_profile["atom_ingestion"]
            try:
                from mica.memory.atom.system import ATOMMemoryConfig, ATOMMemorySystem

                atom_config = ATOMMemoryConfig(enable_llm=False, graph_prior_dsn="")
                _atom = ATOMMemorySystem(config=atom_config)
                atom_profile["llm"] = {
                    "enabled": bool(atom_config.enable_llm),
                    "mode": "disabled_for_deep_research" if not atom_config.enable_llm else "enabled",
                }
                encoder = DLMEncoder()
                section_classifier = getattr(encoder, "_section_classifier", None)
                classifier_stats = getattr(section_classifier, "get_stats", None)
                if callable(classifier_stats):
                    atom_profile["classifier"] = dict(classifier_stats())
                elif section_classifier is not None:
                    atom_profile["classifier"] = {
                        "mode": getattr(section_classifier, "use_ml", False) and "ml" or "heuristic",
                    }
                for _pid, _paper in list(papers_by_id.items())[:200]:
                    _text = best_available_literature_text(_paper)
                    if _text and len(_text) > 40:
                        atom_profile["attempted_documents"] += 1
                        try:
                            encoded = encoder.encode(_text)
                            structured = build_structured_paper(
                                _text,
                                encoded_document=encoded,
                                title=str(_paper.get("title") or "") or None,
                                metadata={
                                    "source_format": "deep_research",
                                    "canonical_id": str(_paper.get("canonical_id") or _paper.get("paperId") or ""),
                                },
                            )
                            await _atom.store_experience(structured)
                            atom_stored += 1
                            atom_profile["stored_documents"] = atom_stored
                        except Exception:
                            atom_profile["errors"] += 1
                            pass
                atom_profile["status"] = "ok" if atom_profile["errors"] == 0 else "partial"
                search_log.append(f"[atom-ingestion] stored {atom_stored}/{min(len(papers_by_id), 200)} document bodies into ATOM TKG")
            except ImportError:
                atom_profile["status"] = "unavailable"
                search_log.append("[atom-ingestion] ATOMMemorySystem not available — skipped")
            except Exception as exc:
                atom_profile["status"] = "error"
                atom_profile["error"] = str(exc)
                search_log.append(f"[atom-ingestion] ERROR: {exc}")
            finally:
                runtime_profile["stages"]["atom_ingestion_ms"] = _elapsed_ms(atom_ingestion_started)
        else:
            search_log.append("[atom-ingestion] disabled by request")

    finally:
        await service.close()

    total = len(papers_by_id)
    with_pdf = sum(1 for p in papers_by_id.values() if p.get("pdf_url"))
    with_abstract = sum(1 for p in papers_by_id.values() if p.get("abstract"))

    gaps = {
        "total_papers": total,
        "papers_with_pdf": with_pdf,
        "papers_without_pdf": total - with_pdf,
        "papers_with_abstract": with_abstract,
        "papers_without_abstract": total - with_abstract,
        "citation_graph_nodes": len(citation_graph),
        "total_reference_edges": sum(len(v) for v in citation_graph.values()),
    }

    artifact_bundle: Dict[str, Any] = {}
    artifact_manifest: Dict[str, Any] = {}
    artifact_list: List[Dict[str, Any]] = []
    artifact_bundle_started = perf_counter()
    try:
        bundle_payload = await build_canonical_literature_bundle(
            query=payload.query,
            preset="deep_research",
            user_id=payload.user_id or "deep-research",
            session_id=payload.session_id or "",
            backend=str(getattr(deep_result, "backend", "") or "dlm_literature_search_service"),
            papers=list(papers_by_id.values()),
            requested_sources=list(getattr(deep_result, "requested_sources", list(plan.get("requested_sources") or []))),
            attempted_sources=list(getattr(deep_result, "attempted_sources", [])),
            failed_sources=list(getattr(deep_result, "failed_sources", [])),
            source_counts=dict(getattr(deep_result, "source_counts", {}) or {}),
            provider_health=dict(getattr(deep_result, "source_health", {}) or {}),
            retrieval_policy=dict(plan or {}),
            acquisition_envelope=dict(getattr(deep_result, "request_envelope", {}) or {}),
            generation_notes=[
                "Deep research now exposes the canonical literature artifact family alongside citation-graph closure.",
                "The artifact family is shared with Bibliotecario deep synthesis so API and runtime lanes speak the same closure object.",
            ],
        )
        artifact_bundle = dict(bundle_payload.get("artifact_bundle") or {})
        artifact_manifest = dict(bundle_payload.get("artifact_manifest") or {})
        artifact_list = list(bundle_payload.get("artifact_list") or [])
        bundle_runtime_profile = dict(bundle_payload.get("runtime_profile") or {})
        bundle_stage_profile = dict(bundle_runtime_profile.get("stages") or {})
        publication_assembly_profile = dict(bundle_runtime_profile.get("publication_assembly") or {})
        runtime_profile["artifact_bundle"]["status"] = "ok"
        runtime_profile["artifact_bundle"]["artifact_count"] = len(artifact_list)
        if bundle_stage_profile:
            runtime_profile["artifact_bundle"]["stages"] = bundle_stage_profile
        if publication_assembly_profile:
            runtime_profile["artifact_bundle"]["publication_assembly"] = publication_assembly_profile
    except Exception as exc:
        runtime_profile["artifact_bundle"]["status"] = "error"
        runtime_profile["artifact_bundle"]["error"] = str(exc)
        search_log.append(f"[artifact-bundle] degraded: {exc}")
    finally:
        runtime_profile["stages"]["artifact_bundle_ms"] = _elapsed_ms(artifact_bundle_started)

    search_log.insert(0, f"[protocol] query_spec_hash={spec.query_spec_hash} version={spec.protocol_version}")

    result: Dict[str, Any] = {
        "query": payload.query,
        "query_spec_hash": spec.query_spec_hash,
        "protocol_version": spec.protocol_version,
        "total_papers": total,
        "papers": list(papers_by_id.values()),
        "citation_graph": citation_graph,
        "gaps": gaps,
        "search_log": search_log,
        "acquisition_envelope": dict(getattr(deep_result, "request_envelope", {}) or {}),
        "artifact_bundle": artifact_bundle,
        "artifact_manifest": artifact_manifest,
        "artifact_list": artifact_list,
    }

    # Tolerance-wrapped persistence — never raise, never block the main result.
    persistence_started = perf_counter()
    try:
        from mica.research_artifacts import ArtifactWriter, LiteratureRunWriter

        _writer = ArtifactWriter()
        _run_writer = LiteratureRunWriter(
            _writer,
            user_id=str(payload.user_id or ""),
            session_id=str(payload.session_id or ""),
            lane="deep_research",
        )
        _run_id = str(payload.session_id or "") or f"dr-{spec.query_spec_hash[:12]}"
        _citations = [
            {
                "canonical_id": str(p.get("canonical_id") or p.get("paperId") or ""),
                "title": str(p.get("title") or ""),
                "doi": str(p.get("doi") or ""),
                # Packet 6A: rich citation fields for continuity restore and downstream use
                "year": p.get("year"),
                "abstract": (str(p.get("abstract") or ""))[:300],
                "citationCount": int(p.get("citationCount") or 0),
            }
            for p in list(papers_by_id.values())[:500]
        ]
        _run_summary = {
            "query": payload.query,
            "query_spec_hash": spec.query_spec_hash,
            "protocol_version": spec.protocol_version,
            "total_papers": total,
            "gaps": gaps,
        }
        await _run_writer.persist_run(
            _run_id,
            _run_summary,
            citations=_citations,
            search_log=search_log,
        )
        runtime_profile["persistence"]["status"] = "ok"
        runtime_profile["persistence"]["run_id"] = _run_id
        search_log.append(f"[persistence] deep_research artifacts persisted run_id={_run_id}")
    except Exception as _persist_exc:
        runtime_profile["persistence"]["status"] = "error"
        runtime_profile["persistence"]["error"] = str(_persist_exc)
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "deep_research persistence failed (non-fatal): %s", _persist_exc
        )
    finally:
        runtime_profile["stages"]["persistence_ms"] = _elapsed_ms(persistence_started)

    runtime_profile["service"] = dict(
        (result.get("acquisition_envelope") or {}).get("runtime_profile") or {}
    )
    runtime_profile["total_ms"] = _elapsed_ms(request_started)
    search_log.append(
        "[timing] deep_research_total_ms="
        + str(runtime_profile["total_ms"])
        + " deep_search_ms="
        + str(runtime_profile["stages"]["deep_search_ms"])
        + " atom_ingestion_ms="
        + str(runtime_profile["stages"]["atom_ingestion_ms"])
        + " artifact_bundle_ms="
        + str(runtime_profile["stages"]["artifact_bundle_ms"])
        + " persistence_ms="
        + str(runtime_profile["stages"]["persistence_ms"])
    )
    result["runtime_profile"] = runtime_profile

    return result
