"""Canonical acquisition contracts for single- and batch-mode fulltext acquisition.

These Pydantic models define the shared contract that both ``FullTextRouter.acquire_single``
and ``FullTextRouter.acquire_batch`` must honour when dispatched through
``FullTextRouter.execute``.

Invariants enforced by this contract:
  - Single and batch share one result envelope (``FullTextAcquisitionResult``).
  - Lineage fields (session_id, run_id, user_id, tenant_id, budget) are threaded
    into every ``acquire_single`` call regardless of mode.
  - ``degraded_count`` and ``degradation_summary`` are always present; callers must
    not infer success from absence of an error.
  - ``budget_snapshot`` and ``provider_controls`` are propagated from the last
    acquired document's internal metadata.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class PaperRef(BaseModel):
    """Identity fields for a single paper inside an acquisition request."""

    paper_id: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    title: str = ""
    abstract: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FullTextAcquisitionRequest(BaseModel):
    """Unified acquisition contract for both single and batch modes.

    Usage
    -----
    Single mode — acquire one paper by identity::

        FullTextAcquisitionRequest(
            mode="single",
            paper_refs=[PaperRef(doi="10.1016/j.cell.2021.01.001", title="...")],
            session_id="sess-abc",
            run_id="run-001",
            user_id="user-xyz",
        )

    Batch mode — acquire by query::

        FullTextAcquisitionRequest(
            mode="batch",
            query="GPCR signalling kinase",
            max_items=20,
            session_id="sess-abc",
            run_id="run-002",
        )

    Batch mode — acquire explicit paper list::

        FullTextAcquisitionRequest(
            mode="batch",
            paper_refs=[PaperRef(pmcid="PMC1234567"), PaperRef(doi="10.1038/nature01234")],
            session_id="sess-abc",
        )
    """

    mode: Literal["single", "batch"]

    # Paper identity.
    # Single mode uses paper_refs[0]; batch iterates all paper_refs or falls back to query.
    paper_refs: List[PaperRef] = Field(default_factory=list)
    query: str = ""

    # Lineage — threaded into every acquire_single() call.
    session_id: str = ""
    run_id: str = ""
    user_id: str = ""
    tenant_id: str = "default"

    # Budget.
    acquisition_budget_usd: Optional[float] = None
    budget_spent_usd: float = 0.0
    allow_paid_fulltext: bool = False

    # Controls.
    require_cloud_evidence: bool = False
    max_items: int = 25
    start_offset: int = 0


class DegradationEntry(BaseModel):
    """Per-document degradation record inside a result envelope."""

    paper_id: str
    flags: List[str]
    acquisition_kind: str
    provider: str


class FullTextAcquisitionResult(BaseModel):
    """Unified result envelope for both single and batch acquisition.

    Degradation is first-class: ``degraded_count`` and ``degradation_summary``
    are always populated.  Callers that care about abstract-only degradation must
    inspect these fields rather than relying on a non-empty ``documents`` list.
    """

    mode: str
    documents: List[Dict[str, Any]] = Field(default_factory=list)
    requested_count: int = 0
    acquired_count: int = 0
    degraded_count: int = 0
    degradation_summary: List[DegradationEntry] = Field(default_factory=list)
    budget_snapshot: Dict[str, Any] = Field(default_factory=dict)
    provider_controls: Dict[str, Any] = Field(default_factory=dict)
    audit_summary: List[Dict[str, Any]] = Field(default_factory=list)
