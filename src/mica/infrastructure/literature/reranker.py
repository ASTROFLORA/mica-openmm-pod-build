from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
_EDUCATION_TERMS = {
    "classroom",
    "curriculum",
    "education",
    "educational",
    "inquiry-based",
    "pembelajaran",
    "pedagogy",
    "problem-based",
    "school",
    "schools",
    "student",
    "students",
    "teacher",
    "teachers",
}
_PROVIDER_PRIORS = {
    "pubmed": 1.0,
    "semantic_scholar": 0.95,
    "openalex": 0.85,
    "biorxiv": 0.75,
}


@dataclass(frozen=True)
class RankedPaper:
    paper: Dict[str, Any]
    score: float
    rank: int
    features: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "score": self.score,
            "features": dict(self.features),
            "canonical_id": str(self.paper.get("canonical_id") or ""),
            "paper_id": str(self.paper.get("paperId") or ""),
            "provider": str(self.paper.get("provider") or self.paper.get("source") or ""),
            "title": str(self.paper.get("title") or ""),
        }


def rerank_literature_candidates(
    *,
    query: str,
    papers: Sequence[Dict[str, Any]],
    query_profile: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    query_text = str(query or "").strip()
    tokens = [token for token in _tokenize(query_text) if token.casefold() not in _STOPWORDS]
    normalized_profile = _normalize_query_profile(query_profile)
    ranked: List[RankedPaper] = []
    for paper in list(papers or []):
        if not isinstance(paper, dict):
            continue
        features = _score_features(query_text, tokens, paper, query_profile=normalized_profile)
        score = round(
            (6.0 * features["exact_phrase_hit"])
            + (3.5 * features["title_overlap"])
            + (2.0 * features["abstract_overlap"])
            + (2.5 * features["entity_overlap"])
            + (1.5 * features["topic_alignment"])
            + (2.75 * features["domain_anchor_alignment"])
            + (1.0 * features["provider_prior"])
            + (0.35 * features["citation_signal"])
            + (0.20 * features["recency_signal"])
            - (6.5 * features["acronym_context_penalty"])
            - (4.75 * features["off_domain_penalty"]),
            6,
        )
        ranked.append(RankedPaper(paper=dict(paper), score=score, rank=0, features=features))

    ranked.sort(
        key=lambda item: (
            item.score,
            int(item.paper.get("citationCount") or 0),
            int(item.paper.get("year") or 0),
            str(item.paper.get("title") or ""),
        ),
        reverse=True,
    )
    annotated: List[Dict[str, Any]] = []
    for index, item in enumerate(ranked, start=1):
        paper = dict(item.paper)
        metadata = dict(paper.get("metadata") or {})
        metadata["retrieval_features"] = dict(item.features)
        metadata["reranker_scores"] = {
            "light_first_pass": item.score,
            "rank": index,
            "model": "heuristic_v1",
        }
        paper["metadata"] = metadata
        annotated.append(paper)
        ranked[index - 1] = RankedPaper(paper=paper, score=item.score, rank=index, features=item.features)

    summary = {
        "strategy": "light_first_pass",
        "query": query_text,
        "query_profile": dict(normalized_profile),
        "candidate_count": len(annotated),
        "top_candidates": [item.to_dict() for item in ranked[:10]],
    }
    return annotated, summary


def _score_features(
    query_text: str,
    query_tokens: Sequence[str],
    paper: Dict[str, Any],
    *,
    query_profile: Dict[str, Any],
) -> Dict[str, float]:
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    metadata = dict(paper.get("metadata") or {})
    provider = str(paper.get("provider") or paper.get("source") or "").strip().lower()
    title_tokens = _tokenize(title)
    abstract_tokens = _tokenize(abstract)
    title_set = {token.casefold() for token in title_tokens}
    abstract_set = {token.casefold() for token in abstract_tokens}
    query_set = {token.casefold() for token in query_tokens}
    phrase_hit = 1.0 if query_text and query_text.casefold() in f"{title} {abstract}".casefold() else 0.0
    title_overlap = _overlap_ratio(query_set, title_set)
    abstract_overlap = _overlap_ratio(query_set, abstract_set)
    entity_overlap = _entity_overlap(query_tokens, title, abstract)
    acronym_context_penalty = _acronym_context_penalty(query_tokens, title_set, abstract_set)
    topic_alignment = _topic_alignment(query_set, metadata)
    domain_anchor_alignment = _domain_anchor_alignment(query_profile, title_set, abstract_set, metadata)
    off_domain_penalty = _off_domain_penalty(
        query_tokens,
        query_profile=query_profile,
        title=title,
        abstract=abstract,
        metadata=metadata,
        domain_anchor_alignment=domain_anchor_alignment,
    )
    citation_signal = math.log1p(max(0, int(paper.get("citationCount") or 0))) / 8.0
    recency_signal = _recency_signal(paper.get("year"))
    return {
        "exact_phrase_hit": round(phrase_hit, 4),
        "title_overlap": round(title_overlap, 4),
        "abstract_overlap": round(abstract_overlap, 4),
        "entity_overlap": round(entity_overlap, 4),
        "acronym_context_penalty": round(acronym_context_penalty, 4),
        "topic_alignment": round(topic_alignment, 4),
        "domain_anchor_alignment": round(domain_anchor_alignment, 4),
        "off_domain_penalty": round(off_domain_penalty, 4),
        "provider_prior": round(_PROVIDER_PRIORS.get(provider, 0.5), 4),
        "citation_signal": round(citation_signal, 4),
        "recency_signal": round(recency_signal, 4),
    }


def _normalize_query_profile(query_profile: Any) -> Dict[str, Any]:
    if isinstance(query_profile, dict):
        preset = str(query_profile.get("preset") or "").strip()
        domain_terms = query_profile.get("domain_terms") or []
    else:
        preset = str(getattr(query_profile, "preset", "") or "").strip()
        domain_terms = getattr(query_profile, "domain_terms", []) or []
    cleaned_terms = [
        str(term or "").strip()
        for term in list(domain_terms)
        if str(term or "").strip()
    ]
    return {
        "preset": preset,
        "domain_terms": list(dict.fromkeys(cleaned_terms))[:6],
    }


def _overlap_ratio(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = {str(value or "").casefold() for value in left if str(value or "").strip()}
    right_set = {str(value or "").casefold() for value in right if str(value or "").strip()}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / max(1, len(left_set))


def _entity_overlap(query_tokens: Sequence[str], title: str, abstract: str) -> float:
    entity_tokens = [token for token in query_tokens if any(ch.isdigit() for ch in token) or token.isupper()]
    if not entity_tokens:
        return 0.0
    haystack = f"{title} {abstract}".casefold()
    hits = sum(1 for token in entity_tokens if token.casefold() in haystack)
    return hits / max(1, len(entity_tokens))


def _acronym_context_penalty(
    query_tokens: Sequence[str],
    title_set: Sequence[str],
    abstract_set: Sequence[str],
) -> float:
    acronym_tokens = [token for token in query_tokens if token.isupper() and len(token) >= 3]
    if not acronym_tokens:
        return 0.0
    haystack = {str(value or "").casefold() for value in list(title_set) + list(abstract_set) if str(value or "").strip()}
    acronym_hits = sum(1 for token in acronym_tokens if token.casefold() in haystack)
    if acronym_hits <= 0:
        return 0.0
    contextual_tokens = [
        token for token in query_tokens
        if not token.isupper() and token.casefold() not in _STOPWORDS
    ]
    if not contextual_tokens:
        return 0.0
    contextual_hits = sum(1 for token in contextual_tokens if token.casefold() in haystack)
    contextual_coverage = contextual_hits / max(1, len(contextual_tokens))
    if contextual_coverage >= 0.35:
        return 0.0
    return min(1.0, (0.35 - contextual_coverage) / 0.35)


def _topic_alignment(query_set: Sequence[str], metadata: Dict[str, Any]) -> float:
    terms = _topic_metadata_terms(metadata)
    if not terms:
        return 0.0
    return _overlap_ratio(query_set, terms)


def _domain_anchor_alignment(
    query_profile: Dict[str, Any],
    title_set: Sequence[str],
    abstract_set: Sequence[str],
    metadata: Dict[str, Any],
) -> float:
    domain_terms = _expand_domain_terms(query_profile.get("domain_terms") or [])
    if not domain_terms:
        return 0.0
    topic_terms = _topic_metadata_terms(metadata)
    haystack = list(title_set) + list(abstract_set) + topic_terms
    return _overlap_ratio(domain_terms, haystack)


def _topic_metadata_terms(metadata: Dict[str, Any]) -> List[str]:
    openalex_data = dict(metadata.get("openalex_data") or {})
    terms: List[str] = []

    def _collect_topic(topic_like: Any) -> None:
        if not isinstance(topic_like, dict):
            return
        display_name = str(topic_like.get("display_name") or "").strip()
        if display_name:
            terms.extend(_tokenize(display_name))
        for nested_key in ("subfield", "field", "domain"):
            nested = topic_like.get(nested_key) or {}
            if isinstance(nested, dict):
                nested_display = str(nested.get("display_name") or "").strip()
                if nested_display:
                    terms.extend(_tokenize(nested_display))

    _collect_topic(openalex_data.get("primary_topic") or {})
    for topic in openalex_data.get("topics") or []:
        _collect_topic(topic)
    return terms


def _expand_domain_terms(domain_terms: Sequence[str]) -> List[str]:
    expanded: List[str] = []
    for raw_term in domain_terms:
        term = str(raw_term or "").strip()
        if not term:
            continue
        expanded.append(term)
        if "-" not in term:
            continue
        for chunk in term.split("-"):
            if len(chunk) < 3:
                continue
            if chunk.casefold() in _STOPWORDS:
                continue
            expanded.append(chunk)
    return list(dict.fromkeys(item.casefold() for item in expanded if item))


def _off_domain_penalty(
    query_tokens: Sequence[str],
    *,
    query_profile: Dict[str, Any],
    title: str,
    abstract: str,
    metadata: Dict[str, Any],
    domain_anchor_alignment: float,
) -> float:
    if str(query_profile.get("preset") or "") not in {"field_scan", "program_audit"}:
        return 0.0
    query_token_set = {str(token or "").casefold() for token in query_tokens if str(token or "").strip()}
    if query_token_set & _EDUCATION_TERMS:
        return 0.0
    haystack = {token.casefold() for token in _tokenize(f"{title} {abstract}")}
    topic_terms = {token.casefold() for token in _topic_metadata_terms(metadata)}
    education_hits = len(haystack & _EDUCATION_TERMS)
    education_topic_hits = len(topic_terms & _EDUCATION_TERMS)
    total_hits = max(education_hits, education_topic_hits)
    if total_hits <= 0:
        return 0.0
    if domain_anchor_alignment >= 0.55:
        return 0.0
    return min(1.0, (total_hits / 2.0) * (1.0 - domain_anchor_alignment))


def _recency_signal(year: Any) -> float:
    try:
        numeric_year = int(year)
    except (TypeError, ValueError):
        return 0.0
    if numeric_year <= 0:
        return 0.0
    delta = max(0, 2026 - numeric_year)
    return max(0.0, 1.0 - min(delta, 20) / 20.0)


def _tokenize(text: str) -> List[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(str(text or ""))]