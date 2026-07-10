from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Sequence


_ENTITY_RE = re.compile(r"\b(?:[A-Z]{2,}[0-9A-Z-]*|[A-Z]\d+[A-Z]?\d*)\b")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?(?:\s?(?:%|nM|uM|µM|mM|K|C|x|fold|ns|ms|s|mg|kg))?\b")
_ASSERTIVE_QUERY_RE = re.compile(r"\b(?:prove|confirm|demonstrate|show that|establish|verify)\b", re.IGNORECASE)
_EXPLORATORY_QUERY_RE = re.compile(r"\b(?:explore|investigate|assess|analyze|audit|map|review|characterize|survey)\b", re.IGNORECASE)
_UNIVERSAL_PREMISE_RE = re.compile(r"\b(?:always|never|universally|definitive(?:ly)?|guarantee(?:d|s)?|cure(?:s|d|ative)?)\b", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _extract_entities(text: str) -> List[str]:
    return sorted({match.group(0) for match in _ENTITY_RE.finditer(text or "") if len(match.group(0)) >= 3})


def _extract_numbers(text: str) -> List[str]:
    return sorted({match.group(0).strip() for match in _NUMBER_RE.finditer(text or "")})


@dataclasses.dataclass(frozen=True)
class FirewallVerdict:
    action: str
    reasons: List[str]
    contradicted_claim_ids: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class InvariantVerdict:
    passed: bool
    failed_checks: List[str]
    entity_drift: List[str]
    numeric_drift: List[str]
    contradiction_introduced: bool

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class EpistemicFirewall:
    def evaluate_pre_routing(self, *, query: str) -> FirewallVerdict:
        cleaned_query = _clean_text(query)
        reasons: List[str] = []
        action = "accept"

        if _ASSERTIVE_QUERY_RE.search(cleaned_query):
            reasons.append("query requests confirmatory closure before evidence is gathered")
            action = "challenge"

        if (
            _ASSERTIVE_QUERY_RE.search(cleaned_query)
            and _UNIVERSAL_PREMISE_RE.search(cleaned_query)
            and not _EXPLORATORY_QUERY_RE.search(cleaned_query)
        ):
            reasons.append("query encodes an explicitly contradicted-style universal premise")
            action = "block"

        return FirewallVerdict(action=action, reasons=reasons, contradicted_claim_ids=[])

    def evaluate(self, *, query: str, final_result: Dict[str, Any]) -> FirewallVerdict:
        contradicted_claim_ids = [
            str(claim.get("claim_id") or "")
            for claim in (final_result.get("claims") or [])
            if isinstance(claim, dict)
            and (
                str(claim.get("strength") or "") == "unsupported"
                and list(claim.get("counterevidence_ids") or [])
            )
        ]
        reasons: List[str] = []
        action = "accept"

        if final_result.get("misleading_support_detected"):
            reasons.append("promotion payload carries misleading support semantics")
            action = "challenge"

        if contradicted_claim_ids:
            reasons.append(f"contradicted evidence present for claims {contradicted_claim_ids}")
            action = "challenge"

        if contradicted_claim_ids and _ASSERTIVE_QUERY_RE.search(query or ""):
            reasons.append("assertive user premise conflicts with contradicted evidence")
            action = "block"

        return FirewallVerdict(action=action, reasons=reasons, contradicted_claim_ids=contradicted_claim_ids)


class InvariantValidator:
    def validate(self, *, query: str, final_result: Dict[str, Any]) -> InvariantVerdict:
        claims = [claim for claim in (final_result.get("claims") or []) if isinstance(claim, dict)]
        answer_text = _clean_text(final_result.get("answer") or final_result.get("summary") or "")

        supported_claims = [
            claim
            for claim in claims
            if str(claim.get("claim_kind") or "") == "positive_scientific"
            and str(claim.get("strength") or "") in {"supported", "observed"}
        ]

        critical_entity_pool = sorted({entity for claim in supported_claims for entity in _extract_entities(_clean_text(claim.get("text") or ""))})
        critical_number_pool = sorted({number for claim in supported_claims for number in _extract_numbers(_clean_text(claim.get("text") or ""))})

        entity_drift = [entity for entity in critical_entity_pool if entity and entity not in answer_text]
        numeric_drift = [number for number in critical_number_pool if number and number not in answer_text]
        contradiction_introduced = any(list(claim.get("counterevidence_ids") or []) for claim in supported_claims)

        failed_checks: List[str] = []
        if entity_drift:
            failed_checks.append("critical_entity_drift")
        if numeric_drift:
            failed_checks.append("numeric_exactness_drift")
        if contradiction_introduced:
            failed_checks.append("contradiction_introduced")

        return InvariantVerdict(
            passed=not failed_checks,
            failed_checks=failed_checks,
            entity_drift=entity_drift,
            numeric_drift=numeric_drift,
            contradiction_introduced=contradiction_introduced,
        )


def build_cold_evidence_spine(*, query: str, final_result: Dict[str, Any]) -> Dict[str, Any]:
    firewall = EpistemicFirewall().evaluate(query=query, final_result=final_result)
    invariants = InvariantValidator().validate(query=query, final_result=final_result)
    return {
        "schema_version": "mica.cold_evidence_spine.v0",
        "firewall": firewall.to_dict(),
        "invariants": invariants.to_dict(),
        "promotion_ready": firewall.action == "accept" and invariants.passed,
        "note": "Cold evidence spine only governs promotion/publication boundaries in this phase.",
    }