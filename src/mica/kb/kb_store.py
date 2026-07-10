"""
KB Query Store — K1.8 (Query API del KB)

Structured knowledge store for claims, evidence, and contradictions.
The KB is NOT semantic search — it answers "what does MICA assert, with what support?"

Key objects:
- KBStore: in-memory store with query methods
- KBQueryResult: structured response for KB queries
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .claim_atom import ClaimAtom, ClaimStatus, ClaimTier
from .claim_versioning import ClaimFamily, ClaimVersion, SupersessionKind
from .contradiction import ContradictionRecord, ContradictionStatus
from .evidence_item import EvidenceItem, EvidenceKind


@dataclass
class ClaimSupportSummary:
    """Aggregated support information for a claim."""
    supporting_evidence_count: int = 0
    independent_source_count: int = 0
    contradiction_count: int = 0
    evidence_kinds: Dict[str, int] = field(default_factory=dict)
    last_evidence_at: Optional[str] = None


@dataclass
class KBQueryResult:
    """Structured KB query response."""
    claim_family_ref: str
    current_version_ref: str
    claim_atom: ClaimAtom
    tier: ClaimTier
    status: ClaimStatus
    support_summary: ClaimSupportSummary
    evidence_refs: List[str] = field(default_factory=list)
    contradiction_refs: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    as_of: Optional[str] = None
    version_count: int = 1


@dataclass
class KBEntityQueryResult:
    """KB query result for all claims about an entity."""
    entity_ref: str
    claims: List[KBQueryResult]
    total_claims: int = 0
    active_claims: int = 0
    tiers: Dict[str, int] = field(default_factory=dict)


class KBStore:
    """In-memory structured knowledge store.

    This is the authoritative KB — it stores promoted claims with evidence,
    tiers, contradictions, and version history. It is NOT a search engine.
    """

    def __init__(self):
        self._families: Dict[str, ClaimFamily] = {}
        self._evidence: Dict[str, EvidenceItem] = {}
        self._contradictions: Dict[str, ContradictionRecord] = {}

    def add_claim(
        self,
        family_ref: str,
        claim_atom: ClaimAtom,
        evidence_refs: Optional[List[str]] = None,
        receipt_ref: Optional[str] = None,
    ) -> ClaimVersion:
        """Add a new claim to the KB.

        If family_ref already exists, supersedes the current version.
        If not, creates a new ClaimFamily.
        """
        if family_ref in self._families:
            family = self._families[family_ref]
            version = family.supersede(
                new_atom=claim_atom,
                supersession_kind=SupersessionKind.CONTEXT_REFINEMENT,
                reason="KBStore.add_claim update",
                receipt_ref=receipt_ref,
            )
        else:
            version = ClaimVersion(
                claim_version_ref=f"{family_ref}/v1",
                claim_family_ref=family_ref,
                version_number=1,
                atom=claim_atom,
                status=ClaimStatus.ACTIVE,
                valid_from=datetime.now(timezone.utc),
                created_by_receipt_ref=receipt_ref,
            )
            self._families[family_ref] = ClaimFamily(family_ref, version)

        return version

    def retract_claim(
        self,
        family_ref: str,
        reason: str,
        receipt_ref: Optional[str] = None,
    ) -> Optional[ClaimVersion]:
        """Retract a claim from the KB."""
        family = self._families.get(family_ref)
        if family is None:
            return None
        return family.retract(reason=reason, receipt_ref=receipt_ref)

    def add_evidence(self, evidence: EvidenceItem):
        """Add evidence to the KB."""
        self._evidence[evidence.evidence_ref] = evidence

    def add_contradiction(self, contradiction: ContradictionRecord):
        """Record a contradiction between two claims."""
        self._contradictions[contradiction.contradiction_ref] = contradiction

    def get_claim(self, family_ref: str) -> Optional[KBQueryResult]:
        """Query a single claim by family ref."""
        family = self._families.get(family_ref)
        if family is None:
            return None
        current = family.current_version
        if current is None:
            return None
        return self._build_query_result(family, current)

    def get_claims_for_entity(self, entity_ref: str) -> KBEntityQueryResult:
        """Query all claims about a specific entity."""
        results = []
        for family in self._families.values():
            current = family.current_version
            if current is None:
                continue
            if (current.atom.subject and current.atom.subject.entity_ref.entity_id == entity_ref) or (current.atom.object and current.atom.object.entity_ref.entity_id == entity_ref):
                results.append(self._build_query_result(family, current))

        tiers = {}
        for r in results:
            tier_name = r.tier.value
            tiers[tier_name] = tiers.get(tier_name, 0) + 1

        return KBEntityQueryResult(
            entity_ref=entity_ref,
            claims=results,
            total_claims=len(results),
            active_claims=sum(1 for r in results if r.status == ClaimStatus.ACTIVE),
            tiers=tiers,
        )

    def query(
        self,
        predicate: Optional[str] = None,
        tier: Optional[ClaimTier] = None,
        status: Optional[ClaimStatus] = None,
        entity_ref: Optional[str] = None,
    ) -> List[KBQueryResult]:
        """Query claims with optional filters."""
        results = []
        for family in self._families.values():
            current = family.current_version
            if current is None:
                continue
            if predicate and current.atom.predicate_ref != predicate:
                continue
            if tier is not None:
                # Would need tier on ClaimVersion; approximate from atom
                continue
            if status and current.status != status:
                continue
            if entity_ref:
                if current.atom.subject and current.atom.subject.entity_ref.entity_id != entity_ref and current.atom.object and current.atom.object.entity_ref.entity_id != entity_ref:
                    continue
            results.append(self._build_query_result(family, current))
        return results

    def get_contradictions(self, family_ref: Optional[str] = None) -> List[ContradictionRecord]:
        """Query contradictions, optionally filtered by claim family."""
        results = list(self._contradictions.values())
        if family_ref:
            results = [
                c for c in results
                if c.claim_a_ref.startswith(family_ref) or c.claim_b_ref.startswith(family_ref)
            ]
        return results

    def get_evidence(self, evidence_ref: str) -> Optional[EvidenceItem]:
        """Get evidence by ref."""
        return self._evidence.get(evidence_ref)

    def get_timeline(self, family_ref: str) -> List[Dict[str, Any]]:
        """Get version timeline for audit."""
        family = self._families.get(family_ref)
        if family is None:
            return []
        return family.timeline()

    def summary(self) -> Dict[str, Any]:
        """KB summary statistics."""
        total = len(self._families)
        active = sum(1 for f in self._families.values() if f.current_version is not None)
        retracted = 0
        for _f in self._families.values():
            if _f.current_version is None:
                for _v in _f._versions:
                    if _v.status == ClaimStatus.RETRACTED:
                        retracted += 1
                        break
        return {
            "total_families": total,
            "active_claims": active,
            "retracted_claims": retracted,
            "total_evidence": len(self._evidence),
            "total_contradictions": len(self._contradictions),
            "open_contradictions": sum(
                1 for c in self._contradictions.values()
                if c.status == ContradictionStatus.OPEN
            ),
        }

    def _build_query_result(
        self,
        family: ClaimFamily,
        version: ClaimVersion,
    ) -> KBQueryResult:
        """Build a query result from a family and version."""
        # Gather evidence refs from evidence store
        evidence_refs = [
            ref for ref, ev in self._evidence.items()
            if ev.claim_ref == family.family_ref
        ]

        # Gather contradictions
        contradiction_refs = [
            c.contradiction_ref for c in self._contradictions.values()
            if c.claim_a_ref == family.family_ref or c.claim_b_ref == family.family_ref
        ]

        # Build support summary
        support = ClaimSupportSummary(
            supporting_evidence_count=len(evidence_refs),
            independent_source_count=len(set(
                ev.independence_key.source_work_ref
                for ev in self._evidence.values()
                if ev.claim_ref == family.family_ref and ev.independence_key.source_work_ref
            )),
            contradiction_count=len(contradiction_refs),
            evidence_kinds={},
        )
        for ref in evidence_refs:
            ev = self._evidence[ref]
            kind = ev.evidence_kind.value
            support.evidence_kinds[kind] = support.evidence_kinds.get(kind, 0) + 1

        return KBQueryResult(
            claim_family_ref=family.family_ref,
            current_version_ref=version.claim_version_ref,
            claim_atom=version.atom,
            tier=version.atom.tier,
            status=version.status,
            support_summary=support,
            evidence_refs=evidence_refs,
            contradiction_refs=contradiction_refs,
            caveats=[],
            version_count=family.version_count,
        )
