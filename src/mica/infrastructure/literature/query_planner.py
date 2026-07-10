from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


_QUOTE_RE = re.compile(r'"([^"]+)"')
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}")
_ENTITY_TOKEN_RE = re.compile(r"^(?:[A-Z0-9-]{2,12}|[A-Za-z]{2,8}\d[A-Za-z0-9-]*)$")
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
    "how",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "our",
    "the",
    "their",
    "this",
    "to",
    "we",
    "what",
    "with",
}
_AMBIGUOUS_TERMS = {
    "biology",
    "cancer",
    "cell",
    "cells",
    "disease",
    "drug",
    "drugs",
    "field",
    "learning",
    "mechanism",
    "meta",
    "pathway",
    "pathways",
    "precision",
    "program",
    "protein",
    "proteins",
    "research",
    "review",
    "scan",
    "science",
    "signaling",
    "state",
    "study",
    "system",
}
_GENERIC_PROGRAM_AUDIT_TERMS = {
    "analysis",
    "approach",
    "field",
    "framework",
    "learning",
    "meta",
    "method",
    "methods",
    "overview",
    "program",
    "reasoning",
    "research",
    "review",
    "science",
    "scientific",
    "state",
    "study",
    "system",
}


@dataclass(frozen=True)
class QueryVariant:
    kind: str
    text: str
    rationale: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "kind": self.kind,
            "text": self.text,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class RetrievalProfile:
    preset: str
    base_query: str
    extra_queries: List[str] = field(default_factory=list)
    quoted_phrases: List[str] = field(default_factory=list)
    entity_terms: List[str] = field(default_factory=list)
    significant_terms: List[str] = field(default_factory=list)
    ambiguity_terms: List[str] = field(default_factory=list)
    domain_terms: List[str] = field(default_factory=list)
    query_variants: List[QueryVariant] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preset": self.preset,
            "base_query": self.base_query,
            "extra_queries": list(self.extra_queries),
            "quoted_phrases": list(self.quoted_phrases),
            "entity_terms": list(self.entity_terms),
            "significant_terms": list(self.significant_terms),
            "ambiguity_terms": list(self.ambiguity_terms),
            "domain_terms": list(self.domain_terms),
            "query_variants": [variant.to_dict() for variant in self.query_variants],
        }


@dataclass(frozen=True)
class ProviderQueryPlan:
    source: str
    variant_kind: str
    query_text: str
    params: Dict[str, Any]
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "variant_kind": self.variant_kind,
            "query_text": self.query_text,
            "params": dict(self.params),
            "rationale": self.rationale,
        }


def build_retrieval_profile(
    query: str,
    *,
    extra_queries: Optional[Sequence[str]] = None,
    preset: Optional[str] = None,
) -> RetrievalProfile:
    base_query = str(query or "").strip()
    extras = _dedupe_texts(extra_queries or [])
    quoted_phrases = _dedupe_texts(_QUOTE_RE.findall(base_query))
    tokens = _tokenize(base_query)
    significant_terms = [token for token in tokens if token.casefold() not in _STOPWORDS][:12]
    entity_terms = _dedupe_texts(
        [token for token in tokens if _looks_like_entity(token)]
        + [value for value in extras if _looks_like_entity(value)]
    )[:8]
    ambiguity_terms = _dedupe_texts(
        [token for token in significant_terms if token.casefold() in _AMBIGUOUS_TERMS]
    )
    active_preset = str(preset or _choose_preset(base_query, extras, quoted_phrases, entity_terms, ambiguity_terms)).strip() or "biomed_precision"
    domain_terms = _extract_domain_terms(
        extras=extras,
        quoted_phrases=quoted_phrases,
        significant_terms=significant_terms,
        preset=active_preset,
    )
    query_variants = _build_query_variants(
        base_query,
        extras=extras,
        quoted_phrases=quoted_phrases,
        entity_terms=entity_terms,
        significant_terms=significant_terms,
        preset=active_preset,
    )
    return RetrievalProfile(
        preset=active_preset,
        base_query=base_query,
        extra_queries=extras,
        quoted_phrases=quoted_phrases,
        entity_terms=entity_terms,
        significant_terms=significant_terms,
        ambiguity_terms=ambiguity_terms,
        domain_terms=domain_terms,
        query_variants=query_variants,
    )


def build_provider_query_plans(
    profile: RetrievalProfile,
    *,
    sources: Sequence[str],
    max_papers: int,
    semantic_scholar_fields: Optional[Sequence[str]] = None,
) -> List[ProviderQueryPlan]:
    plans: List[ProviderQueryPlan] = []
    normalized_sources = _dedupe_texts(sources)
    openalex_limit = min(max(1, int(max_papers or 25)), 100)
    for variant in profile.query_variants:
        for source in normalized_sources:
            if source == "semantic_scholar":
                plans.append(
                    ProviderQueryPlan(
                        source=source,
                        variant_kind=variant.kind,
                        query_text=variant.text,
                        params={
                            "query": variant.text,
                            "max_papers": max_papers,
                            "fields": list(semantic_scholar_fields or []),
                        },
                        rationale=variant.rationale,
                    )
                )
                continue
            if source == "pubmed":
                plans.append(
                    ProviderQueryPlan(
                        source=source,
                        variant_kind=variant.kind,
                        query_text=variant.text,
                        params={
                            "query": variant.text,
                            "max_results": min(max(1, int(max_papers or 25)), 200),
                        },
                        rationale=variant.rationale,
                    )
                )
                continue
            if source == "openalex":
                params = _build_openalex_params(
                    profile=profile,
                    variant=variant,
                    max_results=openalex_limit,
                )
                plans.append(
                    ProviderQueryPlan(
                        source=source,
                        variant_kind=variant.kind,
                        query_text=variant.text,
                        params=params,
                        rationale=variant.rationale,
                    )
                )
                continue
    return plans


def summarize_query_plans(plans: Sequence[ProviderQueryPlan]) -> List[Dict[str, Any]]:
    return [plan.to_dict() for plan in plans]


def _build_query_variants(
    base_query: str,
    *,
    extras: Sequence[str],
    quoted_phrases: Sequence[str],
    entity_terms: Sequence[str],
    significant_terms: Sequence[str],
    preset: str,
) -> List[QueryVariant]:
    # ── WI-26: Multi-stage cascade ordering ──────────────────────────
    # Stage 1 (precision): entity-heavy variants first
    # Stage 2 (targeted):  exact phrases and context
    # Stage 3 (recall):    broad base query last
    # Bounded: max 8 variants to prevent combinatorial explosion
    _MAX_VARIANTS = 8

    variants: List[QueryVariant] = []

    # Stage 1 — entity precision (highest priority)
    allow_entity_variant = bool(entity_terms) and (
        preset in {"biomed_precision", "protein_entity_scan"}
        or len(entity_terms) >= 2
        or len(list(significant_terms)) <= 4
    )
    if allow_entity_variant:
        variants.append(
            QueryVariant(
                kind="entity",
                text=" ".join(entity_terms[:4]),
                rationale="entity-first cascade: identifier-oriented precision (stage 1)",
            )
        )

    # Stage 2 — exact phrases and context (targeted precision)
    for phrase in quoted_phrases[:2]:
        variants.append(
            QueryVariant(
                kind="exact_phrase",
                text=phrase,
                rationale="quoted phrase carried into precision plan (stage 2)",
            )
        )
    if extras:
        variants.append(
            QueryVariant(
                kind="context",
                text=" ".join(list(extras)[:4]),
                rationale="user-supplied extra queries kept as supporting context (stage 2)",
            )
        )
        for extra in extras[:2]:
            if len(str(extra or "").split()) < 2:
                continue
            variants.append(
                QueryVariant(
                    kind="supporting_phrase",
                    text=str(extra).strip(),
                    rationale="multi-word supporting context (stage 2)",
                )
            )

    # Stage 3 — broad recall (lowest priority, always present as backstop)
    variants.append(
        QueryVariant(kind="broad", text=base_query, rationale="broad recall backstop (stage 3)"),
    )

    if preset == "program_audit" and _has_acronym_anchor(entity_terms):
        anchor_text = _build_acronym_anchor_text(entity_terms=entity_terms, significant_terms=significant_terms)
        if anchor_text:
            variants.append(
                QueryVariant(
                    kind="acronym_anchor",
                    text=anchor_text,
                    rationale="ambiguous acronym anchored to nearby scientific context terms",
                )
            )
    if preset in {"field_scan", "program_audit"}:
        focused = " ".join(list(significant_terms)[:8]).strip()
        if focused and focused.casefold() != base_query.casefold():
            variants.append(
                QueryVariant(
                    kind="field_scan",
                    text=focused,
                    rationale="compressed field scan variant removes filler terms",
                )
            )
    return _dedupe_variants(variants)[:_MAX_VARIANTS]


def _choose_preset(
    query: str,
    extras: Sequence[str],
    quoted_phrases: Sequence[str],
    entity_terms: Sequence[str],
    ambiguity_terms: Sequence[str],
) -> str:
    lowered = str(query or "").casefold()
    if any(token in lowered for token in ("landscape", "overview", "audit", "state of the art", "sota", "field")):
        return "field_scan"
    if quoted_phrases:
        return "biomed_precision"
    if entity_terms and len(entity_terms) >= max(1, len(_tokenize(query)) // 2):
        return "protein_entity_scan"
    if len(_tokenize(query)) >= 12 or extras:
        return "program_audit"
    if ambiguity_terms:
        return "biomed_precision"
    return "biomed_precision"


def _build_openalex_params(
    *,
    profile: RetrievalProfile,
    variant: QueryVariant,
    max_results: int,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "query": variant.text,
        "max_results": max_results,
        "search_mode": "search",
        "filters": [],
        "planner_preset": profile.preset,
    }
    if variant.kind == "exact_phrase":
        params["query"] = ""
        params["search_mode"] = "filter_only"
        params["filters"] = [f"title_and_abstract.search:{variant.text}"]
    elif variant.kind == "supporting_phrase":
        params["query"] = ""
        params["search_mode"] = "filter_only"
        params["filters"] = [f"title_and_abstract.search:{variant.text}"]
    elif variant.kind == "acronym_anchor":
        params["query"] = ""
        params["search_mode"] = "filter_only"
        params["filters"] = [f"title_and_abstract.search:{variant.text}"]
    elif variant.kind == "entity" and len(variant.text.split()) <= 5:
        params["query"] = ""
        params["search_mode"] = "filter_only"
        params["filters"] = [f"title.search:{variant.text}"]
    elif variant.kind == "field_scan":
        params["query"] = ""
        params["search_mode"] = "filter_only"
        params["filters"] = [f"title_and_abstract.search:{variant.text}"]

    if profile.preset in {"field_scan", "program_audit"}:
        params["filters"] = list(params.get("filters") or []) + _field_scan_filters()
    return params


def _field_scan_filters() -> List[str]:
    current_year = datetime.now(timezone.utc).year
    start_year = max(2018, current_year - 6)
    return [
        "type:article|review",
        f"from_publication_date:{start_year}-01-01",
    ]


def _extract_domain_terms(
    *,
    extras: Sequence[str],
    quoted_phrases: Sequence[str],
    significant_terms: Sequence[str],
    preset: str,
) -> List[str]:
    if preset not in {"field_scan", "program_audit"}:
        return []

    preferred: List[str] = []
    fallback: List[str] = []
    candidates: List[str] = []

    for raw_value in list(extras) + list(quoted_phrases):
        text = str(raw_value or "").strip()
        if not text:
            continue
        if "-" in text and len(text.split()) <= 4:
            candidates.append(text)
        candidates.extend(_tokenize(text))

    candidates.extend(list(significant_terms))

    for raw_value in candidates:
        term = str(raw_value or "").strip()
        if len(term) < 3:
            continue
        lowered = term.casefold()
        if lowered in _STOPWORDS:
            continue
        if term.upper() == term and any(ch.isalpha() for ch in term):
            fallback.append(term)
            continue
        target = fallback if lowered in _GENERIC_PROGRAM_AUDIT_TERMS and "-" not in term else preferred
        target.append(term)

    selected = _dedupe_texts(preferred) or _dedupe_texts(fallback)
    return selected[:6]


def _has_acronym_anchor(entity_terms: Sequence[str]) -> bool:
    for raw in entity_terms:
        token = str(raw or "").strip()
        if len(token) < 3:
            continue
        if token.upper() == token and any(ch.isalpha() for ch in token):
            return True
    return False


def _build_acronym_anchor_text(
    *,
    entity_terms: Sequence[str],
    significant_terms: Sequence[str],
) -> str:
    acronym = next(
        (
            str(raw or "").strip()
            for raw in entity_terms
            if str(raw or "").strip()
            and str(raw or "").strip().upper() == str(raw or "").strip()
        ),
        "",
    )
    if not acronym:
        return ""
    context_terms = [
        term
        for term in significant_terms
        if term.casefold() != acronym.casefold() and term.casefold() not in _AMBIGUOUS_TERMS
    ]
    if not context_terms:
        context_terms = [
            term
            for term in significant_terms
            if term.casefold() != acronym.casefold()
        ]
    anchor_terms = [acronym] + context_terms[:3]
    return " ".join(anchor_terms).strip()


def _dedupe_texts(values: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(value)
    return ordered


def _dedupe_variants(values: Sequence[QueryVariant]) -> List[QueryVariant]:
    ordered: List[QueryVariant] = []
    seen: set[str] = set()
    for variant in values:
        key = f"{variant.kind.casefold()}|{variant.text.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        ordered.append(variant)
    return ordered


def _tokenize(text: str) -> List[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(str(text or ""))]


def _looks_like_entity(token: str) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    return bool(_ENTITY_TOKEN_RE.fullmatch(text))