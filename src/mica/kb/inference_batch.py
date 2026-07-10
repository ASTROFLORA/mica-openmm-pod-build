"""
KB Inference Batch Runner — K5-4 (KB Slice 3)

Batch materializes InferredClaimProposals (not evidence).
Budget-bounded: max_hops, branching_factor, path cache.
Query-time preview only (bounded).

Key objects:
- InferredClaimProposal: proposal from inference batch
- InferenceBatchRunner: budget-bounded batch runner
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .inference import InferredClaim, InferenceEngine


@dataclass
class InferredClaimProposal:
    """A proposal from inference batch — NOT evidence."""
    proposal_ref: str
    inferred_claim: InferredClaim
    budget_used: int
    paths_explored: int
    confidence: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InferenceBatchRunner:
    """K5-4: Budget-bounded inference batch.

    Materializes proposals, never evidence.
    Query-time preview bounded by max_proposals.
    """

    def __init__(
        self,
        engine: InferenceEngine,
        max_hops: int = 2,
        max_proposals: int = 50,
        path_cache_enabled: bool = True,
    ):
        self._engine = engine
        self._max_hops = max_hops
        self._max_proposals = max_proposals
        self._path_cache: Dict[str, List[InferredClaimProposal]] = {}
        self._path_cache_enabled = path_cache_enabled

    def run(
        self,
        claims: list,
        scope_ref: Optional[str] = None,
    ) -> List[InferredClaimProposal]:
        """Run inference batch with budget bounds."""
        cache_key = scope_ref or "global"
        if self._path_cache_enabled and cache_key in self._path_cache:
            return self._path_cache[cache_key]

        inferred = self._engine.infer(claims, max_chain=self._max_hops)
        proposals = []
        for i, inf in enumerate(inferred[:self._max_proposals]):
            proposals.append(InferredClaimProposal(
                proposal_ref=f"proposal://{cache_key}/{i:04d}",
                inferred_claim=inf,
                budget_used=i + 1,
                paths_explored=len(inferred),
                confidence=inf.derivation.confidence,
            ))

        if self._path_cache_enabled:
            self._path_cache[cache_key] = proposals

        return proposals

    def invalidate_cache(self, scope_ref: Optional[str] = None) -> None:
        if scope_ref:
            self._path_cache.pop(scope_ref, None)
        else:
            self._path_cache.clear()

    @property
    def cache_size(self) -> int:
        return len(self._path_cache)
