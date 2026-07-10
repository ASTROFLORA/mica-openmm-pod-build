"""
EvidenceLedger — S0.5 + S0.6

Runtime, per-run container for claims with provenance, confidence scoring,
and negative-result tracking.

This is a **stateful runtime artefact** — it accumulates EvidenceEntry items
throughout a run and is persisted as ``evidence_ledger.json`` in the run
directory.  It intentionally lives *outside* ``evidence/`` (which is purely
stateless citation helpers).

Spec references:
    §6.6  EvidenceLedger  (fields, WHERE, tests)
    §6.5  AlgorithmicConfidence  (formula, usage rules)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


# ====================================================================
# EvidenceEntry
# ====================================================================

# Allowed literal sets (for validation)
_SEVERITIES = frozenset({"critical", "important", "informational"})
_EVIDENCE_TYPES = frozenset({"experimental", "review", "prediction", "database"})
_STATUSES = frozenset({"supported", "partial", "contradicted", "unsupported"})
_VERIFICATION = frozenset({"verified", "unverified", "hallucinated"})
_VALIDATION_ROUTES = frozenset(
    {"literature", "tool", "code", "database", "human_review", "mixed"}
)


@dataclass
class EvidenceEntry:
    """Single claim with provenance, typed per §6.6."""

    claim_id: str
    claim_text: str
    severity: str  # Literal["critical", "important", "informational"]
    source_ids: List[str] = field(default_factory=list)
    tool_call_ids: List[str] = field(default_factory=list)
    evidence_type: str = "review"  # Literal[…]
    status: str = "unsupported"  # Literal[…]
    verification_status: str = "unverified"  # Literal[…]
    validation_route: str = "literature"  # Literal[…]
    algorithmic_confidence: float = 0.0
    negative_result_refs: List[str] = field(default_factory=list)
    source_role_types: Dict[str, str] = field(default_factory=dict)
    source_relevance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    relevant_source_ids: List[str] = field(default_factory=list)
    weakly_relevant_source_ids: List[str] = field(default_factory=list)
    irrelevant_source_ids: List[str] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.claim_id:
            self.claim_id = uuid.uuid4().hex[:12]

    # ---- serialisation ------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvidenceEntry":
        return cls(
            claim_id=d.get("claim_id", ""),
            claim_text=d.get("claim_text", ""),
            severity=d.get("severity", "informational"),
            source_ids=list(d.get("source_ids", [])),
            tool_call_ids=list(d.get("tool_call_ids", [])),
            evidence_type=d.get("evidence_type", "review"),
            status=d.get("status", "unsupported"),
            verification_status=d.get("verification_status", "unverified"),
            validation_route=d.get("validation_route", "literature"),
            algorithmic_confidence=float(d.get("algorithmic_confidence", 0.0)),
            negative_result_refs=list(d.get("negative_result_refs", [])),
            source_role_types=dict(d.get("source_role_types", {})),
            source_relevance=dict(d.get("source_relevance", {})),
            relevant_source_ids=list(d.get("relevant_source_ids", [])),
            weakly_relevant_source_ids=list(d.get("weakly_relevant_source_ids", [])),
            irrelevant_source_ids=list(d.get("irrelevant_source_ids", [])),
            timestamp=d.get("timestamp", ""),
        )


# ====================================================================
# S0.6 — Algorithmic Confidence  (v0)
# ====================================================================
#
# Composite formula (per claim):
#
#   c = w_cov * coverage + w_val * validation_bonus - w_contra * contradiction_penalty
#
# - coverage        = min(len(source_ids) / EXPECTED_SOURCES, 1.0)
# - validation_bonus = ROUTE_BONUS_MAP[validation_route]
# - contradiction_penalty = min(len(negative_result_refs) * 0.1, 0.4)
#
# Weights: w_cov=0.5, w_val=0.3, w_contra=0.2
# Result clamped to [0.0, 1.0].

_EXPECTED_SOURCES = 3  # baseline for "well-sourced" claim

_ROUTE_BONUS: Dict[str, float] = {
    "literature": 0.10,
    "tool": 0.15,
    "database": 0.20,
    "code": 0.20,
    "mixed": 0.15,
    "human_review": 0.25,
}

_W_COV = 0.5
_W_VAL = 0.3
_W_CONTRA = 0.2


def compute_algorithmic_confidence(entry: EvidenceEntry) -> float:
    """Return composite confidence in [0.0, 1.0] for *entry*.

    This is the v0 heuristic; later sprints will refine with agent-consensus
    and tool-reliability factors.
    """
    coverage = min(len(entry.source_ids) / max(1, _EXPECTED_SOURCES), 1.0)
    val_bonus = _ROUTE_BONUS.get(entry.validation_route, 0.0)
    contra = min(len(entry.negative_result_refs) * 0.1, 0.4)

    score = _W_COV * coverage + _W_VAL * val_bonus - _W_CONTRA * contra
    return max(0.0, min(1.0, round(score, 4)))


# ====================================================================
# EvidenceLedger
# ====================================================================

class EvidenceLedger:
    """Run-scoped container for :class:`EvidenceEntry` items.

    Thread-safe (single writer expected per run, but protected with a dict
    keyed by ``claim_id`` to tolerate duplicate adds).
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._entries: Dict[str, EvidenceEntry] = {}  # keyed by claim_id
        self.created_at: str = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def add_claim(self, entry: EvidenceEntry) -> EvidenceEntry:
        """Add *entry* to the ledger (auto-compute confidence if zero)."""
        if entry.algorithmic_confidence == 0.0:
            entry.algorithmic_confidence = compute_algorithmic_confidence(entry)
        self._entries[entry.claim_id] = entry
        return entry

    def update_claim_status(
        self,
        claim_id: str,
        *,
        status: Optional[str] = None,
        verification_status: Optional[str] = None,
        source_ids: Optional[List[str]] = None,
        negative_result_refs: Optional[List[str]] = None,
    ) -> Optional[EvidenceEntry]:
        """Update fields on an existing claim; recompute confidence."""
        entry = self._entries.get(claim_id)
        if entry is None:
            logger.warning("EvidenceLedger: claim_id %r not found", claim_id)
            return None

        if status is not None:
            entry.status = status
        if verification_status is not None:
            entry.verification_status = verification_status
        if source_ids is not None:
            entry.source_ids = source_ids
        if negative_result_refs is not None:
            entry.negative_result_refs = negative_result_refs

        entry.algorithmic_confidence = compute_algorithmic_confidence(entry)
        return entry

    def get_entries(self) -> List[EvidenceEntry]:
        """Return all entries ordered by timestamp."""
        return sorted(self._entries.values(), key=lambda e: e.timestamp)

    def get_entry(self, claim_id: str) -> Optional[EvidenceEntry]:
        return self._entries.get(claim_id)

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Filtered views (S1.4 prep)
    # ------------------------------------------------------------------

    def get_for_review(
        self,
        severity_filter: Optional[str] = None,
    ) -> List[EvidenceEntry]:
        """Return entries filtered by severity (or all if None)."""
        entries = self.get_entries()
        if severity_filter is not None:
            entries = [e for e in entries if e.severity == severity_filter]
        return entries

    # ------------------------------------------------------------------
    # Quality gate helpers
    # ------------------------------------------------------------------

    def has_unsupported_critical_claims(self) -> bool:
        """True if any *critical* claim is still unsupported/contradicted."""
        return any(
            e.severity == "critical" and e.status in ("unsupported", "contradicted")
            for e in self._entries.values()
        )

    def critical_unsupported_claims(self) -> List[EvidenceEntry]:
        """Return critical claims that are still unsupported/contradicted."""
        return [
            e
            for e in self.get_entries()
            if e.severity == "critical"
            and e.status in ("unsupported", "contradicted")
        ]

    # ------------------------------------------------------------------
    # S1.7: Negative results
    # ------------------------------------------------------------------

    def add_negative_result(
        self,
        claim_id: str,
        negative_ref: str,
        *,
        auto_contradict: bool = False,
    ) -> Optional[EvidenceEntry]:
        """Link a negative finding to an existing claim.

        Parameters
        ----------
        claim_id:
            The claim that has a contradicting result.
        negative_ref:
            Identifier of the negative finding (e.g. paper DOI,
            tool call ID, experiment tag).
        auto_contradict:
            If True and the claim is still 'unsupported', automatically
            set its status to 'contradicted'.

        Returns
        -------
        The updated EvidenceEntry, or None if *claim_id* not found.
        """
        entry = self._entries.get(claim_id)
        if entry is None:
            logger.warning(
                "EvidenceLedger.add_negative_result: claim_id %r not found",
                claim_id,
            )
            return None

        if negative_ref not in entry.negative_result_refs:
            entry.negative_result_refs.append(negative_ref)

        if auto_contradict and entry.status == "unsupported":
            entry.status = "contradicted"

        entry.algorithmic_confidence = compute_algorithmic_confidence(entry)
        return entry

    def get_negative_results(self) -> List[EvidenceEntry]:
        """Return entries that have at least one negative result ref."""
        return [
            e for e in self.get_entries()
            if e.negative_result_refs
        ]

    def get_contradicted_claims(self) -> List[EvidenceEntry]:
        """Return all claims with status 'contradicted'."""
        return [
            e for e in self.get_entries()
            if e.status == "contradicted"
        ]

    # ------------------------------------------------------------------
    # Aggregate confidence
    # ------------------------------------------------------------------

    def aggregate_confidence(self) -> float:
        """Weighted average confidence across all entries.

        Critical entries have 3× weight, important 2×, informational 1×.
        """
        if not self._entries:
            return 0.0

        weight_map = {"critical": 3.0, "important": 2.0, "informational": 1.0}
        total_w = 0.0
        weighted_sum = 0.0
        for e in self._entries.values():
            w = weight_map.get(e.severity, 1.0)
            weighted_sum += w * e.algorithmic_confidence
            total_w += w

        return round(weighted_sum / total_w, 4) if total_w else 0.0

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        entries = [e.to_dict() for e in self.get_entries()]
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "aggregate_confidence": self.aggregate_confidence(),
            "total_claims": len(self._entries),
            "critical_unsupported": len(self.critical_unsupported_claims()),
            "entries": entries,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvidenceLedger":
        ledger = cls(run_id=data.get("run_id", ""))
        ledger.created_at = data.get("created_at", ledger.created_at)
        for ed in data.get("entries", []):
            entry = EvidenceEntry.from_dict(ed)
            ledger._entries[entry.claim_id] = entry
        return ledger

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "EvidenceLedger":
        data = json.loads(json_str)
        return cls.from_dict(data)
