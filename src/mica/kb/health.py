"""
KB Health Metrics — K3.5/K7.4 (Knowledge Health + Truth Drift)

Operational metrics for the KB: contradiction density, staleness,
coverage gaps, tier distribution, evidence quality.

Key objects:
- KBHealthMetrics: health status computation
- StalenessScorer: per-claim staleness scoring
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from .claim_atom import ClaimStatus, ClaimTier
from .claim_versioning import ClaimFamily
from .contradiction import ContradictionStatus
from .evidence_item import EvidenceItem, EvidenceKind


@dataclass
class KBHealthReport:
    """Health status of the knowledge base."""
    total_families: int = 0
    active_claims: int = 0
    retracted_claims: int = 0
    review_required_claims: int = 0
    tier_distribution: Dict[str, int] = field(default_factory=dict)
    total_evidence: int = 0
    evidence_kinds: Dict[str, int] = field(default_factory=dict)
    contradiction_count: int = 0
    open_contradictions: int = 0
    resolved_contradictions: int = 0
    stale_claims: int = 0
    claims_without_evidence: int = 0
    claims_with_high_tier: int = 0
    overall_health: str = "unknown"  # healthy | degraded | critical
    issues: List[str] = field(default_factory=list)


@dataclass
class ClaimStaleness:
    """Staleness assessment for a single claim."""
    family_ref: str
    staleness_score: float  # 0.0 = fresh, 1.0 = very stale
    days_since_last_evidence: Optional[int] = None
    has_open_contradiction: bool = False
    tier: str = ""
    status: str = ""


class KBHealthMetrics:
    """Compute health metrics for the KB."""

    def __init__(
        self,
        families: Dict[str, ClaimFamily],
        evidence: Dict[str, EvidenceItem],
        contradictions: Dict,
    ):
        self._families = families
        self._evidence = evidence
        self._contradictions = contradictions

    def compute_health(self) -> KBHealthReport:
        """Compute overall KB health report."""
        report = KBHealthReport()
        report.total_families = len(self._families)

        # Count by status
        for family in self._families.values():
            current = family.current_version
            if current is None:
                # Check if all versions are retracted
                if any(v.status.value == "retracted" for v in family._versions):
                    report.retracted_claims += 1
                continue

            report.total_families_counted = getattr(report, 'total_families_counted', 0) + 1
            if current.status == ClaimStatus.ACTIVE:
                report.active_claims += 1
            elif current.status == ClaimStatus.REVIEW_REQUIRED:
                report.review_required_claims += 1

            # Tier distribution
            tier_name = current.atom.tier.value
            report.tier_distribution[tier_name] = report.tier_distribution.get(tier_name, 0) + 1

        # Evidence stats
        report.total_evidence = len(self._evidence)
        for ev in self._evidence.values():
            kind = ev.evidence_kind.value
            report.evidence_kinds[kind] = report.evidence_kinds.get(kind, 0) + 1

        # Contradiction stats
        report.contradiction_count = len(self._contradictions)
        for c in self._contradictions.values():
            if c.status == ContradictionStatus.OPEN:
                report.open_contradictions += 1
            elif c.status in (ContradictionStatus.RESOLVED, ContradictionStatus.EXPLAINED_BY_CONTEXT):
                report.resolved_contradictions += 1

        # Claims without evidence
        claims_with_evidence = set(ev.claim_ref for ev in self._evidence.values())
        for family_ref, family in self._families.items():
            if family.current_version and family.current_version.status == ClaimStatus.ACTIVE:
                if family_ref not in claims_with_evidence:
                    report.claims_without_evidence += 1

        # High tier claims
        report.claims_with_high_tier = report.tier_distribution.get("established", 0) + report.tier_distribution.get("experimentally_supported", 0)

        # Stale claims
        scorer = StalenessScorer()
        for family_ref, family in self._families.items():
            current = family.current_version
            if current and current.status == ClaimStatus.ACTIVE:
                staleness = scorer.score(
                    family, self._evidence, self._contradictions
                )
                if staleness.staleness_score > 0.7:
                    report.stale_claims += 1

        # Health assessment
        issues = []
        if report.open_contradictions > 0:
            issues.append(f"{report.open_contradictions} open contradictions")
        if report.stale_claims > 0:
            issues.append(f"{report.stale_claims} stale claims")
        if report.claims_without_evidence > report.active_claims * 0.5 and report.active_claims > 0:
            issues.append(f"{report.claims_without_evidence}/{report.active_claims} active claims lack evidence")

        report.issues = issues
        if not issues:
            report.overall_health = "healthy"
        elif report.open_contradictions > 5 or report.stale_claims > report.active_claims * 0.3:
            report.overall_health = "critical"
        else:
            report.overall_health = "degraded"

        return report

    def compute_staleness(self) -> List[ClaimStaleness]:
        """Compute staleness for all active claims."""
        scorer = StalenessScorer()
        results = []
        for family_ref, family in self._families.items():
            current = family.current_version
            if current and current.status == ClaimStatus.ACTIVE:
                staleness = scorer.score(family, self._evidence, self._contradictions)
                results.append(staleness)
        return sorted(results, key=lambda s: s.staleness_score, reverse=True)


class StalenessScorer:
    """Compute staleness score for a claim (0=fresh, 1=very stale)."""

    def score(
        self,
        family: ClaimFamily,
        evidence: Dict[str, EvidenceItem],
        contradictions: Dict,
    ) -> ClaimStaleness:
        current = family.current_version
        if current is None:
            return ClaimStaleness(
                family_ref=family.family_ref, staleness_score=1.0,
                tier="none", status="inactive",
            )

        now = datetime.now(timezone.utc)
        score = 0.0
        days_since_evidence = None

        # Time since last evidence
        claim_evidence = [
            e for e in evidence.values()
            if e.claim_ref == family.family_ref
        ]
        if claim_evidence:
            latest = max(claim_evidence, key=lambda e: e.created_at if hasattr(e, 'created_at') else datetime.min.replace(tzinfo=timezone.utc))
            if hasattr(latest, 'created_at'):
                days = (now - latest.created_at).days
                days_since_evidence = days
                if days > 365:
                    score += 0.4
                elif days > 180:
                    score += 0.2
                elif days > 90:
                    score += 0.1
        else:
            score += 0.3  # No evidence at all
            days_since_evidence = None

        # Open contradictions penalty
        has_contradiction = False
        for c in contradictions.values():
            if c.claim_a_ref == family.family_ref or c.claim_b_ref == family.family_ref:
                if c.status == ContradictionStatus.OPEN:
                    score += 0.3
                    has_contradiction = True
                    break

        # High tier = less stale concern
        tier = current.atom.tier.value
        if tier in ("established", "experimentally_supported"):
            score *= 0.8  # Reduce staleness concern for well-supported claims

        score = min(1.0, max(0.0, score))

        return ClaimStaleness(
            family_ref=family.family_ref,
            staleness_score=round(score, 3),
            days_since_last_evidence=days_since_evidence,
            has_open_contradiction=has_contradiction,
            tier=tier,
            status=current.status.value,
        )
