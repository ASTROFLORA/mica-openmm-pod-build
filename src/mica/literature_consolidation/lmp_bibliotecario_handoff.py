from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence


SCHEMA_VERSION = "mica.lmp_bibliotecario_handoff.v1"

_MAX_EXTRA_QUERIES = 64
_MAX_HANDOFF_TERMS = 48
_RELEVANT_KEY_PARTS = {
    "accession",
    "binding",
    "chain",
    "domain",
    "feature",
    "gene",
    "identity",
    "ligand",
    "motif",
    "name",
    "pdb",
    "peptide",
    "pocket",
    "protein",
    "residue",
    "sequence",
    "structure",
    "synonym",
    "term",
    "uniprot",
}

_WNK_OSR1_SPAK_SENTINEL = {
    "id": "osr1_activation_jbc_2014",
    "title": "Structural and biochemical insights into the activation mechanisms of germinal center kinase OSR1",
    "pmid": "25389294",
    "pmcid": "PMC4276864",
    "doi": "10.1074/jbc.m114.592097",
    "precision_query": "OSR1 WNK SPAK CCT RFxV MO25 activation autoinhibition",
    "why": "Primary mechanism paper for WNK-family activation of OSR1 through CCT/RFxV and MO25 context.",
}


def compile_lmp_bibliotecario_handoff(
    *,
    query: str,
    entities: Sequence[str] | None = None,
    pdb_ids: Sequence[str] | None = None,
    extra_queries: Sequence[str] | None = None,
    lmp_handoff: Dict[str, Any] | None = None,
    require_full_text: bool = True,
) -> Dict[str, Any]:
    """Compile LMP structural context into deterministic Bibliotecario search strategy."""
    cleaned_query = str(query or "").strip()
    user_entities = _dedupe_texts(entities or [])
    explicit_extra_queries = _dedupe_texts(extra_queries or [])
    structure_ids = _dedupe_texts(pdb_ids or [])
    handoff_terms = _extract_handoff_terms(lmp_handoff or {})

    haystack_values = _dedupe_texts(
        [cleaned_query, *user_entities, *explicit_extra_queries, *structure_ids, *handoff_terms]
    )
    domain_expansions = _domain_expansions(haystack_values)
    structural_queries = _structural_comparison_queries(
        haystack_values=haystack_values,
        pdb_ids=structure_ids,
        handoff_terms=handoff_terms,
    )
    recall_sentinels = _recall_sentinels(haystack_values)
    sentinel_queries: List[str] = []
    paper_identity_targets: List[Dict[str, str]] = []
    for sentinel in recall_sentinels:
        sentinel_queries.extend(
            [
                str(sentinel.get("precision_query") or ""),
                str(sentinel.get("title") or ""),
                f"PMID {sentinel.get('pmid')}",
                f"PMCID {sentinel.get('pmcid')}",
                f"DOI {sentinel.get('doi')}",
            ]
        )
        paper_identity_targets.append(
            {
                "id": str(sentinel.get("id") or ""),
                "title": str(sentinel.get("title") or ""),
                "pmid": str(sentinel.get("pmid") or ""),
                "pmcid": str(sentinel.get("pmcid") or ""),
                "doi": str(sentinel.get("doi") or ""),
            }
        )

    effective_extra_queries = _dedupe_texts(
        [
            *sentinel_queries,
            *explicit_extra_queries,
            *user_entities,
            *handoff_terms[:_MAX_HANDOFF_TERMS],
            *domain_expansions,
            *structural_queries,
        ]
    )[:_MAX_EXTRA_QUERIES]

    status = "active" if any([explicit_extra_queries, user_entities, structure_ids, handoff_terms, recall_sentinels]) else "query_only"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "primary_query": cleaned_query,
        "extra_queries": effective_extra_queries,
        "source_terms": {
            "user_entities": user_entities,
            "explicit_extra_queries": explicit_extra_queries,
            "pdb_ids": structure_ids,
            "lmp_handoff_terms": handoff_terms,
            "domain_expansions": domain_expansions,
            "structural_comparison_queries": structural_queries,
        },
        "recall_sentinels": recall_sentinels,
        "paper_identity_targets": paper_identity_targets,
        "full_text_policy": {
            "required": bool(require_full_text),
            "abstract_only_is_degraded": bool(require_full_text),
            "preferred_order": [
                "pmc_jats",
                "europe_pmc",
                "pubmed_structured_abstract",
                "openalex_metadata_or_pdf",
                "semantic_scholar_fulltext_or_abstract",
                "crossref_metadata",
            ],
        },
    }


def _extract_handoff_terms(value: Any) -> List[str]:
    terms: List[str] = []
    _walk_handoff(value, terms, parent_key="", depth=0)
    return _dedupe_texts(terms)[:_MAX_HANDOFF_TERMS]


def _walk_handoff(value: Any, terms: List[str], *, parent_key: str, depth: int) -> None:
    if depth > 7:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key or "").strip()
            child_key = key_text or parent_key
            if isinstance(child, str) and _is_relevant_key(child_key):
                _append_term(terms, child)
            elif isinstance(child, (list, tuple, dict)):
                _walk_handoff(child, terms, parent_key=child_key, depth=depth + 1)
            elif _is_relevant_key(child_key):
                _append_term(terms, child)
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            _walk_handoff(child, terms, parent_key=parent_key, depth=depth + 1)
        return
    if _is_relevant_key(parent_key):
        _append_term(terms, value)


def _append_term(terms: List[str], value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        return
    text = str(value or "").strip()
    if not _is_useful_term(text):
        return
    terms.append(text)


def _is_relevant_key(key: str) -> bool:
    lowered = str(key or "").casefold()
    return any(part in lowered for part in _RELEVANT_KEY_PARTS)


def _is_useful_term(text: str) -> bool:
    if not text or len(text) > 180:
        return False
    if len(text) < 2:
        return False
    if ":\\" in text or "\\" in text or text.startswith("http://") or text.startswith("https://"):
        return False
    if text.count("{") or text.count("["):
        return False
    return any(ch.isalnum() for ch in text)


def _domain_expansions(values: Sequence[str]) -> List[str]:
    folded = _fold(values)
    expansions: List[str] = []
    if _contains_any(folded, ("spak", "stk39", "q9uew8")):
        expansions.extend(
            [
                "SPAK STK39 WNK OSR1 signaling",
                "SPAK CCT domain RFxV docking motif",
                "STK39 kinase CCT autoinhibition activation",
            ]
        )
    if _contains_any(folded, ("osr1", "oxsr1")):
        expansions.extend(
            [
                "OSR1 OXSR1 germinal center kinase activation mechanisms",
                "OSR1 CCT domain MO25 activation",
                "OSR1 PF1 domain pocket RFxV",
            ]
        )
    if _contains_any(folded, ("wnk", "wnk1", "wnk4", "q9h4a3")):
        expansions.extend(
            [
                "WNK kinase RFxV motif CCT interaction",
                "WNK1 SPAK OSR1 CCT RFxV",
                "WNK4 GRFQVT peptide OSR1 MO25",
            ]
        )
    if _contains_any(folded, ("rfxv", "rfiv", "rfxi", "grfqvt")) or re.search(r"\brf[a-z][vi]\b", folded):
        expansions.extend(
            [
                "RFxV RFIV docking motif CCT domain",
                "WNK RFxV peptide SPAK OSR1 binding",
                "CCT domain RFxV peptide pocket",
            ]
        )
    if "cct" in folded:
        expansions.extend(
            [
                "CCT domain autoinhibition kinase activation",
                "CCT domain WNK peptide binding",
                "PF2 CCT domain SPAK OSR1",
            ]
        )
    if "pf1" in folded:
        expansions.extend(
            [
                "PF1 domain OSR1 pocket structural comparison",
                "PASK Fray PF1 domain CCT pocket",
                "OSR1 PF1 RFxV pocket comparison",
            ]
        )
    if _contains_any(folded, ("mo25", "cab39")):
        expansions.extend(
            [
                "MO25 CAB39 OSR1 activation CCT",
                "MO25 binding OSR1 autoinhibition",
            ]
        )
    if _contains_any(folded, ("alphaal", "alpha al", "aal helix")):
        expansions.append("alphaAL helix OSR1 autoinhibition lysine glutamate")
    return _dedupe_texts(expansions)


def _structural_comparison_queries(
    *,
    haystack_values: Sequence[str],
    pdb_ids: Sequence[str],
    handoff_terms: Sequence[str],
) -> List[str]:
    folded = _fold([*haystack_values, *pdb_ids, *handoff_terms])
    has_structural_context = bool(pdb_ids or handoff_terms) or _contains_any(
        folded,
        ("pdb", "pocket", "chain", "domain", "motif", "structure"),
    )
    if not has_structural_context:
        return []
    queries = [
        "all PDB entries structural comparison CCT RFxV pocket",
        "SPAK OSR1 WNK CCT pocket structural comparison",
    ]
    if _contains_any(folded, ("osr1", "oxsr1", "pf1")):
        queries.append("OSR1 all PDB entries PF1 CCT RFxV pocket comparison")
    if _contains_any(folded, ("spak", "stk39")):
        queries.append("SPAK STK39 PDB structures CCT RFxV pocket comparison")
    return _dedupe_texts(queries)


def _recall_sentinels(values: Sequence[str]) -> List[Dict[str, str]]:
    folded = _fold(values)
    axis_present = _contains_any(folded, ("osr1", "oxsr1", "spak", "stk39", "q9uew8"))
    mechanism_present = _contains_any(
        folded,
        ("wnk", "wnk1", "wnk4", "q9h4a3", "rfxv", "rfiv", "cct", "pf1", "mo25", "cab39", "pocket"),
    )
    if axis_present and mechanism_present:
        return [dict(_WNK_OSR1_SPAK_SENTINEL)]
    return []


def _fold(values: Sequence[str]) -> str:
    return " ".join(str(value or "") for value in values).casefold()


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(str(needle or "").casefold() in text for needle in needles)


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