from __future__ import annotations

import re
from dataclasses import dataclass


_MONOMER_SIGNAL_RE = re.compile(r"\b(?:[A-Z]{2,}\d*|\d+(?:\.\d+)?)\b")


@dataclass(frozen=True)
class PersistenceAssessment:
    graph_worthiness_score: float
    persistence_eligible: bool
    persistence_reason: str
    monomer_signal_count: int


def assess_persistence(
    *,
    text: str,
    sections_count: int,
    citation_count: int,
    degraded: bool,
) -> PersistenceAssessment:
    retained_text = str(text or "").strip()
    monomer_signal_count = len(_MONOMER_SIGNAL_RE.findall(retained_text))
    section_score = min(max(sections_count, 0), 8) / 8.0 * 0.35
    citation_score = min(max(citation_count, 0), 10) / 10.0 * 0.25
    text_score = min(len(retained_text), 8000) / 8000.0 * 0.20
    monomer_score = min(monomer_signal_count, 20) / 20.0 * 0.20
    score = round(section_score + citation_score + text_score + monomer_score, 3)

    if not retained_text:
        return PersistenceAssessment(
            graph_worthiness_score=score,
            persistence_eligible=False,
            persistence_reason="no_text_retained",
            monomer_signal_count=monomer_signal_count,
        )
    if not degraded:
        return PersistenceAssessment(
            graph_worthiness_score=score,
            persistence_eligible=True,
            persistence_reason="full_text_crystallized",
            monomer_signal_count=monomer_signal_count,
        )
    if score >= 0.35 or citation_count >= 2 or sections_count >= 2:
        return PersistenceAssessment(
            graph_worthiness_score=score,
            persistence_eligible=True,
            persistence_reason="structural_signal_retained",
            monomer_signal_count=monomer_signal_count,
        )
    return PersistenceAssessment(
        graph_worthiness_score=score,
        persistence_eligible=False,
        persistence_reason="low_structural_yield",
        monomer_signal_count=monomer_signal_count,
    )