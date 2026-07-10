from __future__ import annotations

import os
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from mica.infrastructure.literature import UserLiteratureIngestionService
from mica.literature_consolidation.contracts.query_protocol import LiteratureQueryResult, LiteratureQuerySpec
from mica.model_runtime.backends import DEFAULT_GEMINI_FLASH_MODEL
from mica.storage.gcs_user_storage import sanitize_object_prefix


class LiteratureIngestExecutionRequest(BaseModel):
    query: str = Field(...)
    max_papers: int = Field(50, ge=1, le=2000)

    download_pdfs: bool = True
    extract_full_text: bool = True  # fulltext-first default

    gcs_object_prefix: Optional[str] = None

    session_id: Optional[str] = None
    run_id: Optional[str] = None
    tenant_id: Optional[str] = None
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0)
    allow_paid_fulltext: bool = False

    enable_atom: bool = True
    atom_backend: str = "timescale"
    atom_timescale_dsn: Optional[str] = None

    atom_enable_llm: bool = False
    atom_llm_provider: str = "vertex"
    atom_llm_model_facts: str = DEFAULT_GEMINI_FLASH_MODEL

    # Canonical source-neutral filter field; s2_filters is a backwards-compatible alias.
    filters: Optional[Dict[str, Any]] = None
    s2_filters: Optional[Dict[str, Any]] = None  # deprecated: maps to filters

    @property
    def canonical_filters(self) -> Optional[Dict[str, Any]]:
        """Return filters with s2_filters as fallback for compatibility."""
        return self.filters if self.filters is not None else self.s2_filters


async def run_literature_ingest(
    payload: LiteratureIngestExecutionRequest,
    user_id: str,
    *,
    prod_env: bool | None = None,
) -> Dict[str, Any]:
    if prod_env is None:
        prod_env = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in {"prod", "production"}

    if prod_env and payload.atom_backend.lower() == "sqlite":
        raise ValueError("atom_backend='sqlite' is forbidden in production. Use 'timescale'.")

    safe_prefix: Optional[str] = None
    if payload.gcs_object_prefix is not None:
        safe_prefix = sanitize_object_prefix(payload.gcs_object_prefix)

    s2_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    svc = UserLiteratureIngestionService(semantic_scholar_api_key=s2_key)

    # Build canonical QuerySpec for protocol observability — used to attach
    # query_spec_hash and protocol_version to the result envelope.
    spec = LiteratureQuerySpec.from_ingest_request(payload, user_id=user_id)

    summary = await svc.ingest_query(
        user_id=user_id,
        query=spec.query,
        max_papers=spec.max_papers,
        download_pdfs=spec.download_pdfs,
        extract_full_text=spec.extract_full_text,
        enable_atom=payload.enable_atom,
        atom_backend=payload.atom_backend,
        atom_timescale_dsn=payload.atom_timescale_dsn,
        atom_enable_llm=payload.atom_enable_llm,
        atom_llm_provider=payload.atom_llm_provider,
        atom_llm_model_facts=payload.atom_llm_model_facts,
        filters=spec.filters,
        gcs_object_prefix=safe_prefix,
        session_id=spec.session_id or None,
        run_id=spec.run_id or None,
        tenant_id=spec.tenant_id or None,
        acquisition_budget_usd=spec.acquisition_budget_usd,
        allow_paid_fulltext=spec.allow_paid_fulltext,
    )
    summary_dict = dict(summary.__dict__)
    summary_dict.setdefault("papers_fetched", summary.total_papers)
    result = LiteratureQueryResult.from_spec_and_payload(
        spec,
        payload={"ok": True, "summary": summary_dict, "papers_fetched": summary.total_papers},
        paper_count=summary.total_papers,
        degraded_count=getattr(summary, "degraded_count", 0),
    )
    result = {"ok": True, "summary": summary_dict, "papers_fetched": summary.total_papers,
            "query_spec_hash": result.query_spec_hash, "protocol_version": result.protocol_version}

    # Tolerance-wrapped persistence — never raise, never block the main result.
    try:
        from mica.research_artifacts import ArtifactWriter, LiteratureRunWriter

        _writer = ArtifactWriter()
        _run_writer = LiteratureRunWriter(
            _writer,
            user_id=str(user_id or ""),
            session_id=str(spec.session_id or ""),
            lane="literature_ingest",
        )
        _run_id = str(spec.run_id or spec.session_id or "") or f"ingest-{spec.query_spec_hash[:12]}"
        _run_summary = {
            "query": spec.query,
            "query_spec_hash": spec.query_spec_hash,
            "protocol_version": spec.protocol_version,
            "papers_fetched": summary.total_papers,
        }
        await _run_writer.persist_run(_run_id, _run_summary)
    except Exception as _persist_exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "literature_ingest persistence failed (non-fatal): %s", _persist_exc
        )

    return result
