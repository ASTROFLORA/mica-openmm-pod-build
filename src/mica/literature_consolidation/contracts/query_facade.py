"""Unified query facade contracts — Iteration 08.

``LiteratureQueryFacadeRequest`` is the single request envelope accepted by
``POST /api/v1/literature/query``. It carries a lane discriminator and dispatches
to one of three execution lanes without any router-local logic:

  - ``"ingest"``        → LiteratureIngestExecutionRequest + run_literature_ingest
  - ``"deep_research"`` → DeepResearchExecutionRequest + run_deep_research
  - ``"bibliotecario"`` → BibliotecarioScanExecutionRequest + run_bibliotecario_scan

``LiteratureQueryFacadeResult`` is the shared top-level result envelope. Lane-specific
extras live in ``payload`` so callers retain full access without schema breakage.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from mica.literature_consolidation.contracts.query_protocol import PROTOCOL_VERSION

if TYPE_CHECKING:
    from mica.literature_consolidation.contracts.iterative_intelligence import IterativeLiteratureSession


# Frozenset used by service + tests for validation.
VALID_LANES: frozenset = frozenset({"ingest", "deep_research", "bibliotecario"})


class LiteratureQueryFacadeRequest(BaseModel):
    """Single entrypoint request envelope for all literature execution lanes.

    Lane discriminator
    ------------------
    ``lane`` selects the execution lane:
      - ``"ingest"``        : per-user PDF + ATOM + RAG ingestion
      - ``"deep_research"`` : broad multi-source deep search + DLM enrichment
      - ``"bibliotecario"`` : targeted entity/protein scan

    Source-neutral fields
    ---------------------
    ``sources`` is provider-agnostic. The ``ProviderCompiler`` inside each lane
    service handles provider-specific compilation. Do NOT pass provider-specific
    keys directly here.

    Invariant: ``ProviderCompiler`` is never called from this contract layer —
    only from inside the lane service.
    """

    lane: Literal["ingest", "deep_research", "bibliotecario"] = Field(
        "ingest",
        description="Execution lane: ingest | deep_research | bibliotecario",
    )

    # Core search intent.
    query: str = Field(..., min_length=1, description="Primary search query — never empty.")
    max_papers: int = Field(50, ge=1, le=10000)
    sources: List[str] = Field(
        default_factory=lambda: ["semantic_scholar", "pubmed", "openalex"],
        description="Ordered provider list. Defaults to semantic_scholar, pubmed, and openalex. Request biorxiv explicitly when recent preprints are needed.",
    )
    entities: List[str] = Field(
        default_factory=list,
        description="Supplemental entity search terms (used by deep_research + bibliotecario lanes).",
    )
    citation_depth: int = Field(
        0, ge=0, le=3,
        description="Citation graph expansion depth (deep_research lane).",
    )
    enable_atom_ingestion: bool = Field(
        False,
        description=(
            "Run ATOM ingestion during synchronous deep_research execution. "
            "Defaults to False for sync facade use so HTTP validation can "
            "measure the stock literature lane without the heavier ATOM tail."
        ),
    )

    # Acquisition controls.
    download_pdfs: bool = Field(False, description="Attempt PDF download when available.")
    extract_full_text: bool = Field(True, description="Fulltext-first default; False degrades to abstract-only.")
    acquisition_budget_usd: Optional[float] = Field(None, ge=0.0)

    # Lineage (threaded into the lane invocation).
    session_id: Optional[str] = None
    run_id: Optional[str] = None

    # Ingest-lane overrides.
    enable_atom: bool = Field(True, description="Create ATOM snapshots (ingest lane).")
    atom_backend: str = Field("timescale", description="ATOM store backend: sqlite | timescale")
    filters: Optional[Dict[str, Any]] = Field(
        None, description="Provider-agnostic search filters passed to the ingest lane."
    )

    # Bibliotecario-lane overrides.
    preset: str = Field(
        "deep-synthesis",
        description="Bibliotecario preset name (bibliotecario lane only).",
    )

    # Iterative intelligence controls (Iter 09).
    iteration_budget: int = Field(
        1,
        ge=1,
        le=10,
        description=(
            "Maximum number of acquire-then-refine iterations. "
            "Default 1 = single-shot (no adaptive loop). "
            ">1 emits a directive skeleton per iteration via "
            "LiteratureQueryFacadeService without a real LLM call."
        ),
    )


class LiteratureQueryFacadeResult(BaseModel):
    """Unified top-level result envelope emitted by ``LiteratureQueryFacadeService``.

    Canonical traceability fields (``query_spec_hash``, ``protocol_version``,
    ``run_id``) are always present. Lane-specific result data lives in ``payload``.
    """

    ok: bool = True
    lane_used: Literal["ingest", "deep_research", "bibliotecario"]
    run_id: Optional[str] = None
    job_id: Optional[str] = None

    # Protocol traceability — always populated.
    query_spec_hash: str = ""
    protocol_version: str = PROTOCOL_VERSION

    # Summary counters.
    papers_fetched: int = 0

    # Lane-specific extras.
    payload: Dict[str, Any] = Field(default_factory=dict)

    # Iterative intelligence session (Iter 09) — None when iteration_budget == 1.
    iterative_session: Optional[Any] = Field(
        None,
        description=(
            "IterativeLiteratureSession populated when iteration_budget > 1. "
            "Type is Any to avoid circular import at model level; runtime type "
            "is IterativeLiteratureSession."
        ),
    )
