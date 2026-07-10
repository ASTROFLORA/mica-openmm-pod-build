"""Provider quorum contracts for bibliotecario and literature runtime lanes.

This schema records provider-by-provider execution status and a deterministic
quorum verdict without claiming production-readiness on partial evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


ProviderStatus = Literal[
    "ok", "degraded", "failed", "not_attempted",
    "success", "degraded_rate_limited", "degraded_timeout",
    "degraded_empty", "degraded_parser_error", "skipped_by_policy", "unavailable"
]
QuorumStatus = Literal["satisfied", "degraded", "blocked"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderExecutionReceipt(BaseModel):
    provider: str
    attempted: bool = False
    status: ProviderStatus = "not_attempted"
    paper_count: int = Field(0, ge=0)
    failure_count: int = Field(0, ge=0)
    failure_reasons: List[str] = Field(default_factory=list)
    http_statuses: List[int] = Field(default_factory=list)
    degraded: bool = False


class ProviderQuorumPolicy(BaseModel):
    min_attempted_providers: int = Field(2, ge=1)
    min_successful_providers: int = Field(2, ge=1)
    allow_degraded_success: bool = True
    require_nonempty_papers: bool = False


class ProviderQuorumReceipt(BaseModel):
    receipt_version: str = "1.0"
    query: str
    lane: str = "bibliotecario"
    task_type: str = "general"
    timestamp: str = Field(default_factory=_utc_now)
    query_spec_hash: str = ""
    run_id: str = ""
    policy: ProviderQuorumPolicy = Field(default_factory=ProviderQuorumPolicy)
    requested_sources: List[str] = Field(default_factory=list)
    effective_sources: List[str] = Field(default_factory=list)
    provider_receipts: List[ProviderExecutionReceipt] = Field(default_factory=list)
    attempted_provider_count: int = Field(0, ge=0)
    successful_provider_count: int = Field(0, ge=0)
    degraded_provider_count: int = Field(0, ge=0)
    blocked_provider_count: int = Field(0, ge=0)
    total_papers: int = Field(0, ge=0)
    status: QuorumStatus = "blocked"
    quorum_satisfied: bool = False
    blocked_reasons: List[str] = Field(default_factory=list)
    failure_records: List[Dict[str, Any]] = Field(default_factory=list)


class ProviderQuorumRuntimeResult(BaseModel):
    receipt: ProviderQuorumReceipt
    papers: List[Dict[str, Any]] = Field(default_factory=list)
    result_payload: Dict[str, Any] = Field(default_factory=dict)
