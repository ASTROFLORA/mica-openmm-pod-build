"""bibliotecario_service.py — Service-layer entrypoint for bibliotecario scan execution.

``execution_service.py`` imports HERE, never from ``mica.api_v1.routers.bibliotecario``.
This is the Iter 04B decoupling boundary: the consolidation execution layer no longer
has a direct router dependency for bibliotecario scans.

State ownership
---------------
``_bib_scan_jobs`` is the canonical in-memory scan state for the service layer.
The router's own ``_scan_jobs`` dict is mirrored at seed time so the router's
``GET /bibliotecario/scan/{job_id}`` poll endpoint continues to work during the
transition period. Full persistence migration (canonical queue/store) is deferred
to Iter 06-persistence.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from mica.literature_consolidation.contracts.query_protocol import LiteratureQuerySpec
from mica.literature_consolidation.lmp_bibliotecario_handoff import compile_lmp_bibliotecario_handoff
from mica.literature_consolidation.provider_compiler import LiteratureProviderCompiler

logger = logging.getLogger("mica.literature_consolidation.services.bibliotecario_service")


# ---------------------------------------------------------------------------
# Request model (service-layer, not router-model)
# ---------------------------------------------------------------------------

class BibliotecarioScanExecutionRequest(BaseModel):
    """Normalised scan request used by the service layer.

    Accepts the same fields as the router's ``ScanRequest`` but is
    defined here so callers do not import from the router.
    """

    query: str = Field(..., description="Research query or entity name")
    preset: str = Field("deep-synthesis", description="BibliotecarioPreset value string")
    entities: List[str] = Field(default_factory=list)
    extra_queries: List[str] = Field(default_factory=list)
    pdb_ids: List[str] = Field(default_factory=list)
    lmp_handoff: Dict[str, Any] = Field(default_factory=dict)
    max_papers: int = Field(200, ge=10, le=10_000)
    sources: List[str] = Field(
        default_factory=lambda: ["semantic_scholar", "pubmed", "openalex"]
    )
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    user_id: str = Field("anonymous")
    acquisition_budget_usd: Optional[float] = None
    require_full_text: bool = Field(True)


# ---------------------------------------------------------------------------
# Canonical in-memory scan state (owned by the service, not the router)
# ---------------------------------------------------------------------------

_bib_scan_jobs: Dict[str, Dict[str, Any]] = {}


def get_bibliotecario_scan_state(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the canonical in-memory state for one bibliotecario scan."""
    return _bib_scan_jobs.get(job_id)


def pop_bibliotecario_scan_state(job_id: str) -> Optional[Dict[str, Any]]:
    """Remove and return one bibliotecario scan state entry, if present."""
    return _bib_scan_jobs.pop(job_id, None)


def _dedupe_texts(values: List[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(value)
    return ordered


def _build_bibliotecario_persistence_payload(
    req: "BibliotecarioScanExecutionRequest",
    final_state: Dict[str, Any],
    *,
    run_id: str,
) -> Dict[str, Any]:
    """Build canonical payloads for BibliotecarioRunWriter from scan result state."""
    from mica.infrastructure.literature.literature_artifact_bundle import canonicalize_paper_record

    result = dict(final_state.get("result") or {})
    papers = list(result.get("papers") or [])
    query = str(result.get("query") or req.query or "").strip()

    summary_lines = [
        f"# Bibliotecario Run Summary ({run_id})",
        "",
        f"Query: {query}",
        f"Preset: {req.preset}",
        f"Total papers: {int(result.get('total_papers') or len(papers))}",
        f"Sources: {', '.join(list(result.get('sources_used') or req.sources or []))}",
    ]
    summary_md = "\n".join(summary_lines)

    citations: List[Dict[str, Any]] = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        canonical = canonicalize_paper_record(paper)
        citations.append(
            {
                "paper_id": str(canonical.get("paper_id") or ""),
                "canonical_id": str(canonical.get("canonical_id") or ""),
                "title": str(canonical.get("title") or ""),
                "doi": str(canonical.get("doi") or ""),
                "pmid": str(canonical.get("pmid") or ""),
                "pmcid": str(canonical.get("pmcid") or ""),
                "source": str(canonical.get("provider") or ""),
                "provider": str(canonical.get("provider") or ""),
                "provider_id": str(canonical.get("provider_id") or ""),
                "provider_role": str(canonical.get("provider_role") or ""),
                "source_url": str(canonical.get("source_url") or ""),
                "fetch_timestamp": str(canonical.get("fetch_timestamp") or ""),
                "content_type": str(canonical.get("content_type") or ""),
                "acquisition_kind": str(canonical.get("acquisition_kind") or ""),
                "degradation_reason": str(canonical.get("degradation_reason") or ""),
                "content_checksum": str(canonical.get("content_checksum") or ""),
                "lineage": dict(canonical.get("lineage") or {}),
                "provider_fetch_receipts": list(canonical.get("provider_fetch_receipts") or []),
                "provider_failures": list(canonical.get("provider_failures") or []),
                "acquisition_audit": list(canonical.get("acquisition_audit") or []),
            }
        )

    retrieval_policy = dict(result.get("retrieval_policy") or {})
    evidence_ledger = [
        {
            "kind": "retrieval_policy",
            "policy": retrieval_policy,
        }
    ]
    query_strategy = dict(
        final_state.get("lmp_bibliotecario_handoff")
        or result.get("lmp_bibliotecario_handoff")
        or {}
    )
    if query_strategy:
        evidence_ledger.append(
            {
                "kind": "lmp_bibliotecario_handoff",
                "strategy": query_strategy,
            }
        )
    evidence_ledger.append(
        {
            "kind": "provider_dna_lineage",
            "paper_count": len(citations),
            "lineage_status_counts": {
                "complete": sum(1 for citation in citations if dict(citation.get("lineage") or {}).get("status") == "complete"),
                "incomplete": sum(1 for citation in citations if dict(citation.get("lineage") or {}).get("status") == "incomplete"),
                "absent": sum(1 for citation in citations if dict(citation.get("lineage") or {}).get("status") == "absent"),
            },
        }
    )

    graph_claims: List[Dict[str, Any]] = []
    for claim in list((result.get("entity_summary") or {}).get("top_entities") or [])[:50]:
        if not isinstance(claim, dict):
            continue
        graph_claims.append(
            {
                "subject": str(claim.get("text") or "unknown"),
                "predicate": "mentioned_in_bibliotecario",
                "object": query,
                "count": int(claim.get("count") or 0),
            }
        )

    return {
        "summary_md": summary_md,
        "citations": citations,
        "evidence_ledger": evidence_ledger,
        "graph_claims": graph_claims,
    }


async def _persist_bibliotecario_run_artifacts(
    job_id: str,
    req: "BibliotecarioScanExecutionRequest",
    final_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist bibliotecario run outputs to the canonical artifact fabric."""
    from mica.research_artifacts import ArtifactWriter, BibliotecarioRunWriter

    run_id = str(req.run_id or req.session_id or job_id).strip() or job_id
    base_dir = os.getenv("MICA_BIBLIOTECARIO_ARTIFACT_DIR", ".mica/runtime/bibliotecario")

    payload = _build_bibliotecario_persistence_payload(req, final_state, run_id=run_id)
    writer = ArtifactWriter(base_dir=base_dir)
    run_writer = BibliotecarioRunWriter(writer, user_id=req.user_id or "", kb_id="")
    records = await run_writer.persist_run(
        run_id=run_id,
        summary_md=payload["summary_md"],
        citations=payload["citations"],
        evidence_ledger=payload["evidence_ledger"],
        graph_claims=payload["graph_claims"],
        entity_ids=list(req.entities or []),
        protein_ids=list(req.pdb_ids or []),
    )

    manifest_uri = ""
    for record in records:
        if getattr(getattr(record, "kind", None), "value", "") == "artifact_manifest_json":
            manifest_uri = str(getattr(record, "uri", "") or "")
            break

    return {
        "run_id": run_id,
        "artifact_count": len(records),
        "manifest_uri": manifest_uri,
    }


# ---------------------------------------------------------------------------
# Service entrypoint
# ---------------------------------------------------------------------------

async def run_bibliotecario_scan(
    job_id: str,
    req: BibliotecarioScanExecutionRequest,
) -> None:
    """Execute a bibliotecario scan; results land in ``_bib_scan_jobs[job_id]``.

    Imports from the router are confined here (not in ``execution_service.py``).
    The router's own ``_scan_jobs`` dict is seeded with a shared reference so
    that the HTTP poll endpoint continues to reflect live progress.

    Args:
        job_id: Stable scan identifier.
        req: Parsed scan execution request.

    Raises:
        ImportError: When the bibliotecario router is unavailable.
        Exception: Propagated from the underlying scan after marking state.
    """
    query_strategy = compile_lmp_bibliotecario_handoff(
        query=req.query,
        entities=req.entities,
        pdb_ids=req.pdb_ids,
        extra_queries=req.extra_queries,
        lmp_handoff=req.lmp_handoff,
        require_full_text=req.require_full_text,
    )
    compiled_extra_queries = list(query_strategy.get("extra_queries") or [])
    effective_entities = _dedupe_texts([*(req.entities or []), *compiled_extra_queries])

    # ── Build QuerySpec for protocol observability ──────────────────────────
    try:
        spec = LiteratureQuerySpec(
            query=req.query,
            entities=effective_entities,
            max_papers=req.max_papers,
            sources=list(req.sources or []),
            lane="bibliotecario",
            extract_full_text=bool(req.require_full_text),
            session_id=req.session_id or None,
            run_id=req.run_id or None,
            user_id=req.user_id or None,
            acquisition_budget_usd=req.acquisition_budget_usd,
        )
        spec_hash = spec.query_spec_hash
        protocol_version = spec.protocol_version
        provider_plan = LiteratureProviderCompiler(
            lane_class="bibliotecario_review",
            preset_name=req.preset,
            openalex_available=True,
        ).compile_plan(spec)
    except Exception:
        spec_hash = ""
        protocol_version = ""
        provider_plan = None

    # ── Import public router execution surface (no router internals) ───────
    try:
        from mica.api_v1.routers.bibliotecario import (  # noqa: PLC0415
            BibliotecarioPreset,
            ScanRequest,
            execute_scan_request,
        )
    except ImportError as exc:
        raise ImportError(f"bibliotecario router unavailable: {exc}") from exc

    preset_str = req.preset
    try:
        preset = BibliotecarioPreset(preset_str)
    except ValueError:
        preset = BibliotecarioPreset.DEEP_SYNTHESIS

    scan_req = ScanRequest(
        query=req.query,
        preset=preset,
        entities=list(req.entities or []),
        extra_queries=compiled_extra_queries,
        pdb_ids=req.pdb_ids,
        lmp_handoff=req.lmp_handoff,
        max_papers=req.max_papers,
        sources=req.sources,
        session_id=req.session_id or req.run_id or job_id,
        run_id=req.run_id or job_id,
        user_id=req.user_id,
        acquisition_budget_usd=req.acquisition_budget_usd,
        require_full_text=req.require_full_text,
    )

    # ── Seed canonical service state ────────────────────────────────────────
    state: Dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "preset": scan_req.preset.value,
        "query": scan_req.query,
        "user_id": req.user_id,
        "session_id": req.session_id or req.run_id or job_id,
        "run_id": req.run_id or job_id,
        "query_spec_hash": spec_hash,
        "protocol_version": protocol_version,
        "provider_execution_plan": provider_plan.to_dict() if provider_plan is not None else None,
        "lmp_bibliotecario_handoff": query_strategy,
        "result": None,
        "error": None,
    }
    _bib_scan_jobs[job_id] = state

    # ── Execute ─────────────────────────────────────────────────────────────
    try:
        await execute_scan_request(job_id, scan_req, state=state)
        final = _bib_scan_jobs.get(job_id)
        if final is not None:
            if str(final.get("status") or "").strip().lower() == "done":
                try:
                    persistence = await _persist_bibliotecario_run_artifacts(job_id, req, final)
                    _bib_scan_jobs[job_id]["persistence"] = persistence
                except Exception as persistence_exc:
                    logger.warning(
                        "bibliotecario scan %s persistence skipped: %s",
                        job_id,
                        persistence_exc,
                    )
                    _bib_scan_jobs[job_id]["persistence"] = {
                        "error": str(persistence_exc),
                    }
        logger.info("bibliotecario scan %s completed (preset=%s)", job_id, scan_req.preset.value)
    except Exception as exc:
        _bib_scan_jobs[job_id]["status"] = "error"
        _bib_scan_jobs[job_id]["error"] = str(exc)
        logger.exception("bibliotecario scan %s failed", job_id)
        raise
