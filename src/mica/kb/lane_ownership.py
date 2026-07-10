"""
KB Lane Ownership Registry — K6-12 (KB Slice 4)

Lane ownership registry with per-lane maturity gates.
Order: literature → sequence → structure → dynamics → domain → network → simulation.
Criterio "primer contrato listo": artifact_ref + receipt_ref + entity binding
+ evidence_kind + max_tier_ceiling + tests.

Key objects:
- LaneDefinition: lane metadata and maturity
- MaturityGate: gate criteria for lane promotion
- LaneOwnershipRegistry: central registry
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class LaneMaturity(str, Enum):
    PLANNED = "planned"
    PROTOTYPE = "prototype"
    TESTED = "tested"
    PRODUCTION_READY = "production_ready"
    PRODUCTION = "production"


class MaturityGateStatus(str, Enum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"


# Canonical lane order per K6-12 spec
_LANE_ORDER = [
    "literature",
    "sequence",
    "structure",
    "dynamics",
    "domain",
    "network",
    "simulation",
]


@dataclass
class MaturityGateCriteria:
    """Gate criteria for a lane to advance."""
    has_artifact_ref: bool = False
    has_receipt_ref: bool = False
    has_entity_binding: bool = False
    has_evidence_kind: bool = False
    has_max_tier_ceiling: bool = False
    has_tests_blocking_unsupported: bool = False

    def all_met(self) -> bool:
        return all([
            self.has_artifact_ref,
            self.has_receipt_ref,
            self.has_entity_binding,
            self.has_evidence_kind,
            self.has_max_tier_ceiling,
            self.has_tests_blocking_unsupported,
        ])


@dataclass
class MaturityGate:
    """K6-12: Gate status for lane advancement."""
    lane_ref: str
    target_maturity: LaneMaturity
    criteria: MaturityGateCriteria = field(default_factory=MaturityGateCriteria)
    status: MaturityGateStatus = MaturityGateStatus.PENDING
    checked_at: Optional[datetime] = None
    receipt_ref: Optional[str] = None


@dataclass
class LaneDefinition:
    """K6-12: Lane metadata and maturity."""
    lane_ref: str
    display_name: str
    maturity: LaneMaturity = LaneMaturity.PLANNED
    lane_order: int = 0  # position in canonical order
    owner: str = "KB_SUBSTRATE_OPERATOR"
    evidence_kind_ref: Optional[str] = None  # max_tier_ceiling
    max_tier_ceiling: Optional[str] = None
    maturity_gate: Optional[MaturityGate] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


@dataclass
class LaneDependency:
    """Dependency between lanes."""
    source_lane: str
    target_lane: str
    dependency_type: str = "requires"  # requires, enhances


class LaneOwnershipRegistry:
    """K6-12: Central registry for lane ownership and maturity gates.

    Order: literature → sequence → structure → dynamics → domain → network → simulation.
    Criterio: artifact_ref + receipt_ref + entity binding + evidence_kind + max_tier_ceiling
    + tests that block unsupported types.

    Red-line: No multimodal without tier ceiling.
    """

    def __init__(self) -> None:
        self._lanes: Dict[str, LaneDefinition] = {}
        self._dependencies: List[LaneDependency] = []
        self._maturity_history: Dict[str, List[Tuple[LaneMaturity, datetime]]] = {}

    def register_lane(self, lane: LaneDefinition) -> LaneDefinition:
        """Register a new lane."""
        lane.lane_order = _LANE_ORDER.index(lane.lane_ref) + 1 if lane.lane_ref in _LANE_ORDER else 99
        self._lanes[lane.lane_ref] = lane
        self._maturity_history.setdefault(lane.lane_ref, []).append(
            (lane.maturity, datetime.now(timezone.utc))
        )
        return lane

    def get_lane(self, lane_ref: str) -> Optional[LaneDefinition]:
        return self._lanes.get(lane_ref)

    def list_lanes(self, maturity: Optional[LaneMaturity] = None) -> List[LaneDefinition]:
        lanes = sorted(self._lanes.values(), key=lambda l: l.lane_order)
        if maturity:
            lanes = [l for l in lanes if l.maturity == maturity]
        return lanes

    def check_maturity_gate(self, lane_ref: str, criteria: MaturityGateCriteria) -> MaturityGate:
        """Check if a lane meets maturity gate criteria."""
        lane = self._lanes.get(lane_ref)
        gate = MaturityGate(
            lane_ref=lane_ref,
            target_maturity=LaneMaturity.PROTOTYPE,
            criteria=criteria,
            status=MaturityGateStatus.PASS if criteria.all_met() else MaturityGateStatus.FAIL,
            checked_at=datetime.now(timezone.utc),
        )
        if lane:
            lane.maturity_gate = gate
        return gate

    def advance_maturity(self, lane_ref: str, target: LaneMaturity) -> LaneDefinition | None:
        """Advance lane maturity if gate passes."""
        lane = self._lanes.get(lane_ref)
        if not lane:
            return None
        if lane.maturity_gate and lane.maturity_gate.status != MaturityGateStatus.PASS:
            return None
        lane.maturity = target
        self._maturity_history.setdefault(lane_ref, []).append(
            (target, datetime.now(timezone.utc))
        )
        return lane

    def add_dependency(self, dependency: LaneDependency) -> None:
        self._dependencies.append(dependency)

    def get_dependencies(self, lane_ref: str) -> List[LaneDependency]:
        return [d for d in self._dependencies if d.target_lane == lane_ref]

    def get_maturity_history(self, lane_ref: str) -> List[Tuple[LaneMaturity, datetime]]:
        return self._maturity_history.get(lane_ref, [])

    def production_ready_lanes(self) -> List[LaneDefinition]:
        """Lanes that are production_ready or production."""
        return [
            l for l in self._lanes.values()
            if l.maturity in (LaneMaturity.PRODUCTION_READY, LaneMaturity.PRODUCTION)
        ]
