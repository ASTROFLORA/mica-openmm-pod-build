"""
Quality Adapter Service — extraction target for DD-CS-009 (Domain B)

Owns peer review verdict parsing, quality score calculation, and peer
feedback adapter composition.  Delegated from AgenticDriver.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Set


class QualityAdapterService:
    """Encapsulates quality evaluation and peer review adapter logic."""

    def __init__(self, *, serialize_fn: Callable[[Any], Dict[str, Any]]) -> None:
        self._serialize = serialize_fn

        # Stub fallbacks — replaced at import time when real modules are available.
        self._QualityScore: Any = None
        self._PeerFeedback: Any = None
        self._AgentPersona: Any = None
        try:
            from mica.scientific_workflow.quality_evaluator import QualityScore  # noqa: F401
            self._QualityScore = QualityScore
        except Exception:
            pass
        try:
            from mica.scientific_workflow.peer_review import PeerFeedback  # noqa: F401
            self._PeerFeedback = PeerFeedback
        except Exception:
            pass
        try:
            from mica.scientific_workflow.paper_consolidation import AgentPersona  # noqa: F401
            self._AgentPersona = AgentPersona
        except Exception:
            pass

        # Fallback stubs
        if self._QualityScore is None:
            class _QS:  # type: ignore
                pass
            self._QualityScore = _QS
        if self._PeerFeedback is None:
            class _PF:  # type: ignore
                pass
            self._PeerFeedback = _PF
        if self._AgentPersona is None:
            class _AP:  # type: ignore
                SYSTEM = "system"
            self._AgentPersona = _AP

    @staticmethod
    def parse_peer_review_verdict(
        critique: str,
        review_issues: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Parse a peer review critique text into a structured verdict."""
        text = str(critique or "")
        upper = text.upper()
        decision = "UNKNOWN"

        verdict_marker = re.search(
            r"\*{0,2}(?:VERDICT|VEREDICTO|DECISION)\s*:\s*"
            r"(REJECT|MAJOR_REVISION|MINOR_REVISION|ACCEPT)\b",
            upper,
        )
        if verdict_marker:
            decision = verdict_marker.group(1)
        else:
            for candidate in ("REJECT", "MAJOR_REVISION", "MINOR_REVISION", "ACCEPT"):
                if candidate in upper:
                    decision = candidate
                    break

        recommended_queries: List[str] = []
        in_query_block = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                if in_query_block:
                    break
                continue
            if "CONCRETE ADDITIONAL SEARCHES" in line.upper() or "BÚSQUEDAS CONCRETAS" in line.upper():
                in_query_block = True
                continue
            if in_query_block and line.startswith(("-", "\u2022", "*")):
                candidate = line[1:].strip().strip("\u201c\u201d\"'")
                if candidate:
                    recommended_queries.append(candidate)
                continue
            if in_query_block:
                break

        if not recommended_queries:
            _qpat = r'[\u201c\u201d\u201e\u201f\u00ab\u00bb"\u2018\u2019]'
            for match in re.findall(_qpat + r'([^"\u201c\u201d]{6,160})' + _qpat, text):
                candidate = str(match).strip()
                if len(candidate.split()) >= 3:
                    recommended_queries.append(candidate)

        deduped_queries: List[str] = []
        seen_queries: Set[str] = set()
        for query in recommended_queries:
            key = query.casefold()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            deduped_queries.append(query)

        unsupported_claims: List[str] = []
        for issue in review_issues or []:
            if not isinstance(issue, dict):
                continue
            claim = str(issue.get("claim") or "").strip()
            if claim:
                unsupported_claims.append(claim)

        if not unsupported_claims:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if "[UNVERIFIED]" in line.upper():
                    unsupported_claims.append(line)

        return {
            "decision": decision,
            "severity": (
                "critical" if decision == "REJECT"
                else "major" if decision == "MAJOR_REVISION"
                else "important" if decision == "MINOR_REVISION"
                else "normal"
            ),
            "recommended_queries": deduped_queries[:8],
            "unsupported_claims": unsupported_claims[:12],
            "should_revise": decision in {"MAJOR_REVISION", "REJECT"} and bool(deduped_queries),
        }

    def build_quality_score_adapter(
        self,
        *,
        verdict: Dict[str, Any],
        review_issues: List[Dict[str, Any]],
        citation_count: int = 0,
    ) -> Any:
        """Compute quality score from verdict, issues, and citation count."""
        critical_count = sum(1 for i in review_issues if str(i.get("severity") or "").lower() == "critical")
        major_count = sum(1 for i in review_issues if str(i.get("severity") or "").lower() == "major")
        minor_count = sum(1 for i in review_issues if str(i.get("severity") or "").lower() == "minor")

        methods = max(0.0, min(1.0, 0.92 - 0.34 * critical_count - 0.18 * major_count - 0.08 * minor_count))
        results = max(0.0, min(1.0, 0.88 - 0.36 * critical_count - 0.20 * major_count - 0.09 * minor_count))
        discussion = max(0.0, min(1.0, 0.84 - 0.20 * critical_count - 0.12 * major_count - 0.05 * minor_count))
        data = max(0.0, min(1.0, 0.30 + min(citation_count, 6) * 0.10 - 0.08 * critical_count))

        try:
            quality = self._QualityScore(
                methods_reproducibility=methods,
                results_rigor=results,
                discussion_depth=discussion,
                data_availability=data,
                overall_score=0.0,
                nature_compliance_checks={
                    "citations_present": citation_count > 0,
                    "major_revision_required": verdict.get("decision") in {"MAJOR_REVISION", "REJECT"},
                    "unverified_claims_flagged": bool(verdict.get("unsupported_claims")),
                },
            )
            if hasattr(quality, "calculate_overall"):
                quality.overall_score = float(quality.calculate_overall())
            return quality
        except Exception:
            overall = 0.30 * methods + 0.40 * results + 0.20 * discussion + 0.10 * data
            return {
                "methods_reproducibility": methods,
                "results_rigor": results,
                "discussion_depth": discussion,
                "data_availability": data,
                "overall_score": overall,
                "nature_compliance_checks": {
                    "citations_present": citation_count > 0,
                    "major_revision_required": verdict.get("decision") in {"MAJOR_REVISION", "REJECT"},
                    "unverified_claims_flagged": bool(verdict.get("unsupported_claims")),
                },
            }

    def build_peer_feedback_adapter(
        self,
        *,
        focus: str,
        verdict: Dict[str, Any],
        review_issues: List[Dict[str, Any]],
        quality_score: Any,
    ) -> Any:
        """Compose peer feedback from review issues and quality score."""
        recommendations = [
            str(issue.get("recommendation") or "").strip()
            for issue in review_issues
            if str(issue.get("recommendation") or "").strip()
        ]
        issue_texts = [
            str(issue.get("issue") or "").strip()
            for issue in review_issues
            if str(issue.get("issue") or "").strip()
        ]
        major_gaps = [
            text for issue, text in zip(review_issues, issue_texts)
            if str(issue.get("severity") or "").lower() in {"critical", "major"}
        ]
        assessment = (
            "ACCEPT" if verdict.get("decision") == "ACCEPT"
            else "REVISE_MINOR" if verdict.get("decision") == "MINOR_REVISION"
            else "REVISE_MAJOR"
        )
        reviewer_persona = getattr(self._AgentPersona, "DR_ARIS_THORNE", getattr(self._AgentPersona, "SYSTEM", "system"))

        try:
            return self._PeerFeedback(
                reviewer_persona=reviewer_persona,
                target_node_id="msrp_reviewer",
                target_report_version=1,
                methodological_concerns=major_gaps[:8],
                reproducibility_gaps=verdict.get("unsupported_claims", [])[:8],
                missing_evidence=verdict.get("unsupported_claims", [])[:8],
                insufficient_rigor=issue_texts[:8],
                nature_standard_violations=major_gaps[:8],
                publication_readiness_score=float(self._serialize(quality_score).get("overall_score", 0.0) or 0.0),
                specific_improvements=recommendations[:10],
                recommended_next_steps=list(verdict.get("recommended_queries", []))[:8],
                overall_assessment=assessment,
                quality_score=quality_score,
            )
        except Exception:
            return {
                "reviewer_persona": str(reviewer_persona),
                "target_node_id": "msrp_reviewer",
                "target_report_version": 1,
                "focus": focus,
                "methodological_concerns": major_gaps[:8],
                "reproducibility_gaps": verdict.get("unsupported_claims", [])[:8],
                "missing_evidence": verdict.get("unsupported_claims", [])[:8],
                "insufficient_rigor": issue_texts[:8],
                "nature_standard_violations": major_gaps[:8],
                "publication_readiness_score": float(self._serialize(quality_score).get("overall_score", 0.0) or 0.0),
                "specific_improvements": recommendations[:10],
                "recommended_next_steps": list(verdict.get("recommended_queries", []))[:8],
                "overall_assessment": assessment,
                "quality_score": self._serialize(quality_score),
            }
