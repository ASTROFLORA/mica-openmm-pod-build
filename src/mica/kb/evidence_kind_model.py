"""
KB Evidence Kind Model — K5-12 (KB Slice 3)

Contracts per modality lane: literature, sequence, structure, dynamics,
simulation, network, domain. Each lane declares its evidence_kind,
max_tier_ceiling, required receipts, and blocking conditions.

Key objects:
- EvidenceKindContract: contract per modality lane
- EvidenceKindRegistry: manages lane contracts
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .claim_atom import ClaimTier
from .evidence_item import EvidenceKind


class LaneStatus(str, Enum):
    PRODUCTION = "production"
    EXPERIMENTAL = "experimental"
    DRAFT_NOT_RUNTIME = "draft_not_runtime"
    BLOCKED = "blocked"


@dataclass
class EvidenceKindContract:
    """K5-12: Contract for a modality lane."""
    lane_name: str
    evidence_kind: EvidenceKind
    max_tier_ceiling: ClaimTier
    required_receipts: List[str] = field(default_factory=list)
    status: LaneStatus = LaneStatus.DRAFT_NOT_RUNTIME
    tier_ceiling_reason: str = ""
    blocking_conditions: List[str] = field(default_factory=list)


# Pre-built contracts for known lanes
LANE_CONTRACTS = {
    "literature": EvidenceKindContract(
        lane_name="literature",
        evidence_kind=EvidenceKind.LITERATURE,
        max_tier_ceiling=ClaimTier.ESTABLISHED,
        required_receipts=["dlm_promotion_receipt", "evidence_promotion_receipt"],
        status=LaneStatus.PRODUCTION,
    ),
    "sequence": EvidenceKindContract(
        lane_name="sequence",
        evidence_kind=EvidenceKind.SIMULATION,
        max_tier_ceiling=ClaimTier.COMPUTATIONAL_SUPPORTED,
        required_receipts=["embedding_contract", "sequence_embedding_receipt"],
        status=LaneStatus.EXPERIMENTAL,
        tier_ceiling_reason="Contextualizes only; requires independent empirical evidence for higher tier",
    ),
    "structure": EvidenceKindContract(
        lane_name="structure",
        evidence_kind=EvidenceKind.SIMULATION,
        max_tier_ceiling=ClaimTier.COMPUTATIONAL_SUPPORTED,
        required_receipts=["model_invocation_receipt", "pau_node_receipt"],
        status=LaneStatus.BLOCKED,
        tier_ceiling_reason="P4 not closed; structure prediction not validated",
    ),
    "dynamics": EvidenceKindContract(
        lane_name="dynamics",
        evidence_kind=EvidenceKind.SIMULATION,
        max_tier_ceiling=ClaimTier.COMPUTATIONAL_SUPPORTED,
        required_receipts=["pau_node_receipt", "protocol_run_receipt"],
        status=LaneStatus.BLOCKED,
        tier_ceiling_reason="P4 not closed; dynamics simulation not validated",
    ),
    "simulation": EvidenceKindContract(
        lane_name="simulation",
        evidence_kind=EvidenceKind.PROTOCOL_RUN,
        max_tier_ceiling=ClaimTier.COMPUTATIONAL_SUPPORTED,
        required_receipts=["protocol_run_receipt", "artifact_refs"],
        status=LaneStatus.EXPERIMENTAL,
    ),
    "network": EvidenceKindContract(
        lane_name="network",
        evidence_kind=EvidenceKind.CURATED_EXTERNAL,
        max_tier_ceiling=ClaimTier.LITERATURE_SUPPORTED,
        required_receipts=["graph_edge_receipt"],
        status=LaneStatus.PRODUCTION,
        tier_ceiling_reason="Graph owns edge authority; KB consumes, not duplicates",
    ),
    "domain": EvidenceKindContract(
        lane_name="domain",
        evidence_kind=EvidenceKind.CURATED_EXTERNAL,
        max_tier_ceiling=ClaimTier.LITERATURE_SUPPORTED,
        required_receipts=["domain_annotation_receipt"],
        status=LaneStatus.EXPERIMENTAL,
    ),
}


class EvidenceKindRegistry:
    """K5-12: Manages evidence kind contracts per lane."""

    def __init__(self):
        self._contracts: Dict[str, EvidenceKindContract] = dict(LANE_CONTRACTS)

    def get_contract(self, lane: str) -> Optional[EvidenceKindContract]:
        return self._contracts.get(lane)

    def can_serve_claim(self, lane: str, tier: ClaimTier) -> bool:
        """Check if a claim at given tier can be served by this lane."""
        contract = self._contracts.get(lane)
        if contract is None:
            return False
        if contract.status == LaneStatus.BLOCKED:
            return False
        # Check tier ceiling
        tier_values = list(ClaimTier)
        ceiling_idx = tier_values.index(contract.max_tier_ceiling)
        claim_idx = tier_values.index(tier)
        return claim_idx <= ceiling_idx

    def register_contract(self, contract: EvidenceKindContract) -> None:
        self._contracts[contract.lane_name] = contract

    def active_lanes(self) -> List[str]:
        return [
            name for name, c in self._contracts.items()
            if c.status in (LaneStatus.PRODUCTION, LaneStatus.EXPERIMENTAL)
        ]
