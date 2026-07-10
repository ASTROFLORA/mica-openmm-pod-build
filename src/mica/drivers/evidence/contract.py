"""
Evidence — Final-Result Contract & Lab Report Builder
======================================================

``normalize_final_result_contract``
    Enriches a raw result dict into the canonical ``mica.final_result.v1``
    contract (claims, sources, paper markdown, degradation flags …).

``build_minimal_lab_report``
    Constructs a BSM ``LabReport`` (or dict fallback) from worker output.

Both functions receive their non-trivial dependencies as explicit
parameters so they can be tested without an ``AgenticDriver`` instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .citations import derive_claims_and_sources, extract_sources_from_text
from ..utils import _truncate_text, _redact_text


_CLAIM_ID_RE = re.compile(r"[^a-z0-9]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=(?:\[?[A-Z0-9]|[-*]))")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)]|\[[ xX]\])\s+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-/+]{1,}")
_REFUSAL_RE = re.compile(r"\b(?:i\s+can(?:not|'t)|unable\s+to|cannot\s+complete|paper-grade|noncompliant)\b", re.IGNORECASE)
_HEDGED_RE = re.compile(r"\b(?:may|might|could|potential(?:ly)?|suggest(?:s|ed)?|likely|possible|appears\s+to)\b", re.IGNORECASE)
_META_RUNTIME_RE = re.compile(
    r"\b(?:dns|network|retrieval|tools?\s+.*fail|external\s+hosts|zero\s+papers|0\s+papers|runtime|transport|provider|artifact|fallback|official\s+sources|uniprot\s+tooling|query\s+page)\b",
    re.IGNORECASE,
)
_NEXT_STEP_RE = re.compile(
    r"^(?:if\s+you\s+want|what\s+i\s+need|pick\s+one|upload|provide|re-run|rerun|validate|inspect|run\s+a\s+deeper|define\s+|confirm\s+|demonstrate\s+)",
    re.IGNORECASE,
)
_SCIENTIFIC_SIGNAL_RE = re.compile(
    r"\b(?:kinase|protein|mutation|mutant|therap(?:y|eutic)|clinical|pathway|resistance|structure|mechanistic|biomarker|phospho|cotransporter|transporter|chloride|alloster(?:y|ic)|binding|KRAS|WNK1|OSR1|SPAK|TP53|G12C|RTK|ERK|PI3K|DNA|disease)\b",
    re.IGNORECASE,
)
_GENERIC_TOOLING_RE = re.compile(r"\b(?:queries|tooling|entry\s+queries|entry\s+tooling|search\s+results?)\b", re.IGNORECASE)
_RETRIEVAL_FAILURE_RE = re.compile(
    r"\b(?:unable\s+to\s+reach|dns\s+failure|returned\s+zero\s+papers|0\s+papers|external\s+retrieval\s+tools?\s+.*failing|evidence\s+requirements)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "were", "when", "will", "into", "your", "have",
    "what", "does", "then", "than", "them", "they", "their", "under", "because", "while", "which", "using",
    "used", "only", "still", "just", "like", "must", "mode", "modes", "claim", "claims", "source", "sources",
    "section", "sections", "output", "outputs", "answer", "answers", "paper", "grade", "audit", "framework",
}


def _clean_text(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _build_contradiction_state(claims: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    contradicted_claim_ids: List[str] = []
    unsupported_with_counterevidence: List[str] = []
    suggestive_with_counterevidence: List[str] = []
    total_counterevidence_refs = 0

    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        counterevidence_ids = [str(ref) for ref in (claim.get("counterevidence_ids") or []) if str(ref)]
        total_counterevidence_refs += len(counterevidence_ids)
        if not counterevidence_ids and "contradict" not in str(claim.get("claim_kind") or "").lower() and "contradict" not in str(claim.get("section") or "").lower():
            continue
        claim_id = str(claim.get("claim_id") or "")
        contradicted_claim_ids.append(claim_id)
        strength = str(claim.get("strength") or "").lower()
        support_score = float(claim.get("claim_support_score", 0.0) or 0.0)
        if strength == "unsupported" or support_score < 0.30:
            unsupported_with_counterevidence.append(claim_id)
        elif strength == "suggestive" or support_score < 0.60:
            suggestive_with_counterevidence.append(claim_id)

    count = len(dict.fromkeys([claim_id for claim_id in contradicted_claim_ids if claim_id]))
    return {
        "count": count,
        "status": "present" if count else "none",
        "claim_ids": list(dict.fromkeys([claim_id for claim_id in contradicted_claim_ids if claim_id])),
        "unsupported_claim_ids": list(dict.fromkeys([claim_id for claim_id in unsupported_with_counterevidence if claim_id])),
        "suggestive_claim_ids": list(dict.fromkeys([claim_id for claim_id in suggestive_with_counterevidence if claim_id])),
        "counterevidence_ref_count": total_counterevidence_refs,
    }


def _infer_artifact_refs(*, artifact: Dict[str, Any], claims: Sequence[Dict[str, Any]], sources: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    haystack = _clean_text(" ".join([
        str(artifact.get("description") or ""),
        str(artifact.get("path") or artifact.get("uri") or ""),
        str(artifact.get("type") or artifact.get("kind") or ""),
    ])).lower()
    inferred_claim_ids = [
        str(claim.get("claim_id") or "")
        for claim in claims or []
        if isinstance(claim, dict) and str(claim.get("claim_id") or "")
        and any(token in haystack for token in _tokenize(str(claim.get("text") or ""))[:4])
    ]
    inferred_source_ids = [
        str(source.get("source_id") or "")
        for source in sources or []
        if isinstance(source, dict) and str(source.get("source_id") or "")
        and any(token in haystack for token in _tokenize(str(source.get("title") or source.get("display_citation") or ""))[:4])
    ]
    return {
        "claim_ids": list(dict.fromkeys([ref for ref in inferred_claim_ids if ref])),
        "source_ids": list(dict.fromkeys([ref for ref in inferred_source_ids if ref])),
    }


def _build_figure_provenance(artifacts: Sequence[Any], claims: Sequence[Dict[str, Any]], sources: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    missing_provenance_count = 0
    linked_claim_count = 0
    linked_source_count = 0

    for index, artifact in enumerate(artifacts or [], start=1):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("type") or artifact.get("kind") or "").lower()
        if not any(token in artifact_type for token in ("figure", "image", "plot", "chart")):
            continue
        claim_refs = [str(ref) for ref in (artifact.get("claim_ids") or []) if str(ref)]
        source_refs = [str(ref) for ref in (artifact.get("source_ids") or []) if str(ref)]
        if not claim_refs or not source_refs:
            inferred = _infer_artifact_refs(artifact=artifact, claims=claims, sources=sources)
            if not claim_refs:
                claim_refs = inferred["claim_ids"]
            if not source_refs:
                source_refs = inferred["source_ids"]
        if claim_refs and source_refs:
            provenance_status = "grounded"
        elif claim_refs or source_refs:
            provenance_status = "partial"
        else:
            provenance_status = "missing"
        if provenance_status != "grounded":
            missing_provenance_count += 1
        linked_claim_count += len(claim_refs)
        linked_source_count += len(source_refs)
        entries.append(
            {
                "figure_id": str(artifact.get("figure_id") or f"figure-{index}"),
                "artifact_type": artifact_type or "figure",
                "path": str(artifact.get("path") or artifact.get("uri") or ""),
                "description": str(artifact.get("description") or ""),
                "claim_ids": claim_refs,
                "source_ids": source_refs,
                "provenance_status": provenance_status,
            }
        )

    status = "unavailable"
    if entries:
        status = "partial" if missing_provenance_count else "grounded"

    return {
        "schema_version": "mica.figure_provenance.v0",
        "status": status,
        "figure_count": len(entries),
        "missing_provenance_count": missing_provenance_count,
        "linked_claim_count": linked_claim_count,
        "linked_source_count": linked_source_count,
        "entries": entries,
        "note": "Figure provenance scaffolding is attached opportunistically and degrades gracefully when figures are absent.",
    }


def _build_hypothesis_registry(*, claims: Sequence[Dict[str, Any]], summary: str, query: str) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []

    for index, claim in enumerate(claims or [], start=1):
        if not isinstance(claim, dict):
            continue
        statement = _clean_text(claim.get("text") or claim.get("claim_text") or "")
        if not statement:
            continue
        hypothesis_id = str(claim.get("claim_id") or _stable_claim_id("hypothesis", str(claim.get("section") or "claim"), statement, index))
        strength = str(claim.get("strength") or "unknown").lower()
        contradiction_refs = [str(ref) for ref in (claim.get("counterevidence_ids") or []) if str(ref)]
        entries.append(
            {
                "hypothesis_id": hypothesis_id,
                "statement": statement,
                "kind": str(claim.get("claim_kind") or "positive_scientific"),
                "section": str(claim.get("section") or "findings"),
                "status": strength if strength in {"supported", "suggestive", "unsupported"} else "unknown",
                "confidence": float(claim.get("confidence", 0.0) or 0.0),
                "evidence_source_ids": [str(ref) for ref in (claim.get("relevant_source_ids") or claim.get("source_ids") or []) if str(ref)],
                "counterevidence_ids": contradiction_refs,
                "notes": str(claim.get("presentation_mode") or ""),
            }
        )

    if not entries and summary:
        entries.append(
            {
                "hypothesis_id": _stable_claim_id("hypothesis", "summary", summary, 1),
                "statement": _truncate_text(summary, 280),
                "kind": "summary_scaffold",
                "section": "summary",
                "status": "unknown",
                "confidence": 0.0,
                "evidence_source_ids": [],
                "counterevidence_ids": [],
                "notes": "Auto-generated scaffold from final summary because no explicit claim registry was available.",
            }
        )

    status_counts = {
        "supported": sum(1 for entry in entries if entry["status"] == "supported"),
        "suggestive": sum(1 for entry in entries if entry["status"] == "suggestive"),
        "unsupported": sum(1 for entry in entries if entry["status"] == "unsupported"),
        "unknown": sum(1 for entry in entries if entry["status"] == "unknown"),
    }

    return {
        "schema_version": "mica.hypothesis_registry.v0",
        "query": query,
        "status": "populated" if entries else "empty",
        "primary_hypothesis_id": entries[0]["hypothesis_id"] if entries else "",
        "hypothesis_count": len(entries),
        "status_counts": status_counts,
        "entries": entries,
        "note": "Phase 1 scaffold only. Entries expose user-facing hypothesis state without changing synthesis policy.",
    }


def _build_dossier_envelope(
    *,
    result: Dict[str, Any],
    query: str,
    output_mode: str,
    run_status: str,
    degradation_flags: Sequence[str],
    fallbacks_used: Sequence[str],
    sources: Sequence[Dict[str, Any]],
    relevant_sources: Sequence[Dict[str, Any]],
    artifacts: Sequence[Any],
    hypothesis_registry: Dict[str, Any],
    contradiction_state: Dict[str, Any],
    figure_provenance: Dict[str, Any],
    epistemic_firewall: Dict[str, Any],
    cognitive_layer: Dict[str, Any],
    thermodynamic_routing: Dict[str, Any],
    promotion_ledger: Dict[str, Any],
    reinjection_history: Sequence[Dict[str, Any]],
    residual_inventory: Sequence[Dict[str, Any]],
    branch_tombstones: Sequence[Dict[str, Any]],
    uncertainty_summary: str,
) -> Dict[str, Any]:
    return {
        "schema_version": "mica.dossier_envelope.v0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": str(result.get("session_id") or ""),
        "run_id": str(result.get("run_id") or ""),
        "query": query,
        "output_mode": output_mode,
        "run_status": run_status,
        "artifact_summary": {
            "artifact_count": len(list(artifacts or [])),
            "evidence_source_count": len(list(sources or [])),
            "relevant_evidence_source_count": len(list(relevant_sources or [])),
        },
        "hypothesis_registry": hypothesis_registry,
        "contradiction_state": contradiction_state,
        "figure_provenance": figure_provenance,
        "epistemic_firewall": epistemic_firewall,
        "cognitive_layer": cognitive_layer,
        "thermodynamic_routing": thermodynamic_routing,
        "promotion_ledger": promotion_ledger,
        "reinjection_history": list(reinjection_history or []),
        "residual_inventory": list(residual_inventory or []),
        "branch_tombstones": list(branch_tombstones or []),
        "uncertainty_summary": uncertainty_summary,
        "degradation_flags": sorted(set(str(flag) for flag in degradation_flags if flag)),
        "fallbacks_used": sorted(set(str(flag) for flag in fallbacks_used if flag)),
        "note": "Phase 2 dossier spine. Envelope now carries cognitive competition, thermodynamic routing, promotion rationale, and structured hot-loop reinjection metadata without exposing chain-of-thought.",
    }


def _build_promotion_ledger(
    *,
    result: Dict[str, Any],
    output_mode: str,
    run_status: str,
) -> Dict[str, Any]:
    promotion_gate = result.get("promotion_gate") if isinstance(result.get("promotion_gate"), dict) else {}
    cold_evidence_spine = result.get("cold_evidence_spine") if isinstance(result.get("cold_evidence_spine"), dict) else {}
    provisional_publication_ready = output_mode == "evidence_backed_answer" and run_status == "ok"
    publication_ready = bool(result.get("publication_ready", promotion_gate.get("passed", provisional_publication_ready)))
    block_reasons = list(promotion_gate.get("promotion_block_reasons") or [])
    cold_firewall = cold_evidence_spine.get("firewall") if isinstance(cold_evidence_spine.get("firewall"), dict) else {}
    cold_invariants = cold_evidence_spine.get("invariants") if isinstance(cold_evidence_spine.get("invariants"), dict) else {}
    return {
        "schema_version": "mica.promotion_ledger.v0",
        "publication_ready": publication_ready,
        "promotion_blocked": not publication_ready,
        "output_mode": output_mode,
        "run_status": run_status,
        "gate_passed": bool(promotion_gate.get("passed", publication_ready)),
        "block_reasons": block_reasons,
        "firewall_action": str(cold_firewall.get("action") or "accept"),
        "invariant_passed": bool(cold_invariants.get("passed", True)),
        "cold_evidence_spine": cold_evidence_spine,
        "note": "Promotion ledger summarizes why the run was or was not eligible for publication-style interpretation.",
    }


def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    for token in _TOKEN_RE.findall(text or ""):
        normalized = token.lower()
        if normalized in _STOPWORDS or len(normalized) < 3:
            continue
        tokens.append(normalized)
    return tokens


def _stable_claim_id(prefix: str, section: str, text: str, index: int) -> str:
    base = _CLAIM_ID_RE.sub("-", f"{prefix}-{section}-{text[:40]}").strip("-").lower() or f"{prefix}-{index}"
    return f"{base[:48]}-{index}"


def _looks_multi_section(text: str) -> bool:
    stripped = str(text or "")
    return bool(
        "## " in stripped
        or stripped.count("\n###") >= 1
        or len(re.findall(r"^\s*(?:[-*]|\d+[.)]|\[[ xX]\])\s+", stripped, flags=re.MULTILINE)) >= 2
        or len(_SENTENCE_SPLIT_RE.split(_clean_text(stripped))) >= 3
    )


def _split_paragraph_sentences(text: str) -> List[str]:
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    pieces = [piece.strip(" -") for piece in _SENTENCE_SPLIT_RE.split(cleaned) if piece.strip()]
    return pieces or [cleaned]


def _extract_atomic_texts(text: str) -> List[str]:
    lines = str(text or "").splitlines()
    pieces: List[str] = []
    paragraph: List[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        pieces.extend(_split_paragraph_sentences(" ".join(paragraph)))
        paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue
        if _HEADING_RE.match(stripped):
            flush_paragraph()
            heading = _HEADING_RE.sub("", stripped).strip()
            if heading:
                pieces.append(heading)
            continue
        if _BULLET_RE.match(stripped):
            flush_paragraph()
            bullet = _BULLET_RE.sub("", stripped).strip()
            if bullet:
                pieces.extend(_split_paragraph_sentences(bullet))
            continue
        paragraph.append(stripped)

    flush_paragraph()
    deduped: List[str] = []
    seen: set[str] = set()
    for piece in pieces:
        cleaned = _clean_text(piece)
        if not cleaned:
            continue
        marker = cleaned.lower()
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(cleaned)
    return deduped


def _classify_claim_kind(text: str) -> str:
    lowered = text.lower()
    if _NEXT_STEP_RE.search(text) or text.startswith("[ ]"):
        return "next_step"
    if _META_RUNTIME_RE.search(text) or "evidence requirements" in lowered or "official references" in lowered:
        return "meta_runtime"
    if lowered.startswith("no ") and _SCIENTIFIC_SIGNAL_RE.search(text):
        return "negative_scientific"
    if _SCIENTIFIC_SIGNAL_RE.search(text):
        return "positive_scientific"
    if lowered.startswith("methods/data used") or lowered.startswith("source types"):
        return "meta_runtime"
    return "process_instruction"


def _classify_presentation_mode(text: str) -> str:
    lowered = text.lower()
    if _REFUSAL_RE.search(text):
        return "refusal"
    if "[unsupported assertion]" in lowered or "unsupported" in lowered or "hypothetical" in lowered:
        return "unsupported_flagged"
    if _HEDGED_RE.search(text):
        return "hedged"
    return "established"


def _normalize_source_role(source: Dict[str, Any]) -> str:
    title = _clean_text(source.get("title"))
    source_id = _clean_text(source.get("source_id"))
    snippet = _clean_text(source.get("evidence_snippet"))
    source_type = _clean_text(source.get("source_type")).lower()
    haystack = " ".join([title, source_id, snippet, _clean_text(source.get("official_url"))])
    if _RETRIEVAL_FAILURE_RE.search(haystack):
        return "retrieval_diagnostic"
    if _GENERIC_TOOLING_RE.search(haystack):
        return "tooling"
    if source_type == "paper":
        return "primary"
    if source_type in {"protein", "structure", "database"}:
        return "metadata"
    return "review"


def _normalize_sources(sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        item = dict(source)
        metadata = dict(item.get("metadata") or {})
        role = str(metadata.get("source_role_type") or _normalize_source_role(item))
        metadata["source_role_type"] = role
        item["metadata"] = metadata
        normalized.append(item)
    return normalized


def _segment_claims(
    *,
    claims: Sequence[Dict[str, Any]],
    summary: str,
    answer_text: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    segmentation_degraded = False
    segmented: List[Dict[str, Any]] = []
    source_claims = [claim for claim in claims if isinstance(claim, dict)]

    if not source_claims:
        source_claims = [{"claim_id": "claim-summary", "section": "abstract", "text": summary or answer_text, "source_ids": []}]

    for claim_index, claim in enumerate(source_claims, start=1):
        raw_text = str(claim.get("text") or claim.get("claim_text") or "")
        text = _clean_text(raw_text)
        if not text:
            continue
        section = _clean_text(claim.get("section") or claim.get("claim_kind") or "finding") or "finding"
        source_ids = list(claim.get("source_ids") or [])
        pieces = _extract_atomic_texts(raw_text)
        if len(source_claims) == 1 and _looks_multi_section(summary or answer_text or text) and len(pieces) <= 1:
            segmentation_degraded = True
        for piece_index, piece in enumerate(pieces or [text], start=1):
            claim_kind = _classify_claim_kind(piece)
            presentation_mode = _classify_presentation_mode(piece)
            segmented_claim = {
                "claim_id": _stable_claim_id(claim.get("claim_id") or "claim", section, piece, piece_index + (claim_index * 10)),
                "claim_text": piece,
                "text": piece,
                "section": section,
                "claim_kind": claim_kind,
                "presentation_mode": presentation_mode,
                "source_ids": list(source_ids),
                "counterevidence_ids": list(claim.get("counterevidence_ids") or []),
                "confidence": float(claim.get("confidence", 0.0) or 0.0),
                "strength": str(claim.get("strength") or "suggestive"),
            }
            for optional_key in (
                "claim_origin",
                "atom_kind",
                "source_role_types",
                "paper_id",
                "canonical_paper_id",
            ):
                if optional_key in claim:
                    segmented_claim[optional_key] = claim.get(optional_key)
            segmented.append(segmented_claim)

    if not segmented:
        segmented.append(
            {
                "claim_id": "claim-fallback-1",
                "claim_text": summary or answer_text,
                "text": summary or answer_text,
                "section": "abstract",
                "claim_kind": "meta_runtime",
                "presentation_mode": "hedged",
                "source_ids": [],
                "counterevidence_ids": [],
                "confidence": 0.0,
                "strength": "suggestive",
            }
        )
    return segmented, segmentation_degraded


def _pair_relevance(
    *,
    user_query: str,
    claim: Dict[str, Any],
    source: Dict[str, Any],
) -> Tuple[float, str, List[str]]:
    reasons: List[str] = []
    claim_kind = str(claim.get("claim_kind") or "positive_scientific")
    claim_text = _clean_text(claim.get("claim_text") or claim.get("text"))
    source_text = " ".join(
        filter(
            None,
            [
                _clean_text(source.get("title")),
                _clean_text(source.get("display_citation")),
                _clean_text(source.get("source_id")),
                _clean_text(source.get("official_url")),
                _clean_text(source.get("evidence_snippet")),
            ],
        )
    )
    role = str((source.get("metadata") or {}).get("source_role_type") or _normalize_source_role(source))

    if claim_kind in {"meta_runtime", "process_instruction", "next_step"}:
        if role == "retrieval_diagnostic" and _RETRIEVAL_FAILURE_RE.search(source_text):
            return 1.0, "relevant", ["runtime diagnostic source matches runtime limitation claim"]
        if role == "tooling":
            return 0.5, "weakly_relevant", ["tooling source only weakly supports non-scientific runtime text"]
        return 0.2, "irrelevant", ["non-scientific claim does not require scientific provenance"]

    if role in {"tooling", "retrieval_diagnostic"}:
        reasons.append("generic tooling or retrieval diagnostic cannot support scientific closure")
        return 0.0, "irrelevant", reasons

    claim_tokens = set(_tokenize(claim_text))
    query_tokens = set(_tokenize(user_query))
    source_tokens = set(_tokenize(source_text))
    entity_overlap = len(claim_tokens & source_tokens)
    query_overlap = len(query_tokens & source_tokens)

    score = 0.0
    source_type = _clean_text(source.get("source_type")).lower()
    if source_type == "paper":
        score += 0.35
        reasons.append("paper source type is compatible with scientific claim")
    elif source_type in {"protein", "structure", "database"}:
        score += 0.15
        reasons.append("metadata source is only weakly compatible with scientific closure")

    if entity_overlap >= 2:
        score += 0.35
        reasons.append("strong entity overlap between claim and source")
    elif entity_overlap == 1:
        score += 0.2
        reasons.append("limited entity overlap between claim and source")

    if query_overlap >= 2:
        score += 0.2
        reasons.append("query topic overlaps with source")
    elif query_overlap == 1:
        score += 0.1
        reasons.append("partial query-topic overlap with source")

    if claim_tokens and source_tokens and len(claim_tokens & source_tokens) >= max(1, min(3, len(claim_tokens) // 4 or 1)):
        score += 0.1
        reasons.append("lexical support beyond identifier match")

    score = round(min(score, 1.0), 4)
    if score >= 0.6:
        return score, "relevant", reasons or ["meets relevance threshold"]
    if score >= 0.3:
        return score, "weakly_relevant", reasons or ["below relevance threshold but not irrelevant"]
    return score, "irrelevant", reasons or ["insufficient overlap or source compatibility"]


def _score_yes_partial_no(condition: int) -> float:
    return 1.0 if condition >= 2 else 0.5 if condition == 1 else 0.0


def _compute_abstention_scores(summary: str, claims: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    text = summary or " ".join(_clean_text(claim.get("claim_text") or claim.get("text")) for claim in claims)
    refusal_clarity = _score_yes_partial_no(2 if _REFUSAL_RE.search(text) else 0)
    limitation_markers = sum(
        1 for marker in ["dns", "network", "literature", "citations", "zero papers", "official references", "evidence"]
        if marker in text.lower()
    )
    limitation_specificity = _score_yes_partial_no(2 if limitation_markers >= 2 else 1 if limitation_markers == 1 else 0)
    next_step_count = sum(1 for claim in claims if str(claim.get("claim_kind")) in {"next_step", "process_instruction"})
    next_step_usefulness = _score_yes_partial_no(2 if next_step_count >= 3 else 1 if next_step_count >= 1 else 0)
    return {
        "refusal_clarity": refusal_clarity,
        "limitation_specificity": limitation_specificity,
        "next_step_usefulness": next_step_usefulness,
    }


def _compute_structure_scores(summary: str, claims: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    text = summary or ""
    section_count = len(re.findall(r"^#{2,6}\s+", text, flags=re.MULTILINE))
    checklist_count = len(re.findall(r"^\s*(?:[-*]|\d+[.)]|\[[ xX]\])\s+", text, flags=re.MULTILINE))
    gap_markers = sum(1 for marker in ["gap", "unknown", "missing evidence", "limitation", "contradiction"] if marker in text.lower())
    structure_score = 1.0 if section_count >= 2 or (section_count >= 1 and checklist_count >= 3) else 0.5 if section_count >= 1 or checklist_count >= 3 else 0.0
    actionability_score = 1.0 if checklist_count >= 3 else 0.5 if checklist_count >= 1 else 0.0
    gap_clarity_score = 1.0 if gap_markers >= 2 else 0.5 if gap_markers == 1 else 0.0
    return {
        "structure_score": structure_score,
        "actionability_score": actionability_score,
        "gap_clarity_score": gap_clarity_score,
    }
# BSM communication types — may be mocked in constrained envs.
try:
    from bsm.communication.legacy_reports import (
        DiscussionSection,
        ExperimentMetadata,
        LabReport,
        MethodsSection,
        ResultsSection,
    )
    from bsm.communication.core import AgentPersona
except ImportError:  # pragma: no cover
    class LabReport:  # type: ignore
        pass

    class ExperimentMetadata:  # type: ignore
        pass

    class MethodsSection:  # type: ignore
        pass

    class ResultsSection:  # type: ignore
        pass

    class DiscussionSection:  # type: ignore
        pass

    class AgentPersona:  # type: ignore
        SYSTEM = "system"


# ────────────────────────────────────────────────────────────────────
# Final-result contract normalisation
# ────────────────────────────────────────────────────────────────────

def normalize_final_result_contract(
    *,
    user_query: str,
    result: Dict[str, Any],
    runtime_capability_snapshot: Dict[str, Any],
    artifact_renderer: Any,
) -> Dict[str, Any]:
    """Enrich *result* into the ``mica.final_result.v1`` contract.

    Parameters
    ----------
    user_query:
        The original user prompt.
    result:
        Raw result dict produced by the orchestration pipeline.
    runtime_capability_snapshot:
        Dict returned by ``AgenticDriver._runtime_capability_snapshot``.
    artifact_renderer:
        ``FinalArtifactRenderer`` (or compatible) instance used to produce
        the paper-markdown overlay.

    Returns
    -------
    The *mutated* ``result`` dict with ``result["final_result"]`` and
    ``result["runtime"]`` populated in-place.
    """
    final_result = result.get("final_result")
    prior_runtime_state = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    transport_path = str((prior_runtime_state or {}).get("transport_path") or "unknown")
    runtime_state = runtime_capability_snapshot
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    quality_score = float(result.get("quality_score", 0.0) or 0.0)
    capability_envelope = runtime_state.get("capability_envelope") if isinstance(runtime_state.get("capability_envelope"), dict) else {}
    providers_state = capability_envelope.get("providers") if isinstance(capability_envelope.get("providers"), dict) else {}
    storage_state = capability_envelope.get("storage") if isinstance(capability_envelope.get("storage"), dict) else {}

    if isinstance(final_result, dict):
        normalized: Dict[str, Any] = dict(final_result)
    elif isinstance(final_result, str):
        normalized = {"summary": final_result, "answer": final_result}
    elif final_result is None:
        normalized = {}
    else:
        normalized = {"summary": str(final_result), "answer": str(final_result)}

    findings = normalized.get("findings")
    lab_reports = result.get("lab_reports")
    sources = normalized.get("sources") if isinstance(normalized.get("sources"), list) else []
    claims = normalized.get("claims") if isinstance(normalized.get("claims"), list) else []
    artifacts = normalized.get("artifacts") if isinstance(normalized.get("artifacts"), list) else []

    summary = str(normalized.get("summary") or normalized.get("answer") or "").strip()
    if not summary:
        finding_count = len(findings) if isinstance(findings, list) else 0
        report_count = len(lab_reports) if isinstance(lab_reports, (list, dict)) else 0
        if finding_count:
            summary = f"MICA completed analysis for: {user_query}. Synthesized {finding_count} finding(s)."
        elif report_count:
            summary = f"MICA completed analysis for: {user_query}. Generated {report_count} report artifact(s)."
        else:
            summary = f"MICA processed the request: {user_query}."

    if not claims or not sources:
        derived_claims, derived_sources = derive_claims_and_sources(summary=summary, findings=findings)
        if not claims:
            claims = derived_claims
        if not sources:
            sources = derived_sources

    answer_text = str(normalized.get("answer") or normalized.get("paper_markdown") or summary)
    sources = _normalize_sources(sources)
    claims, claim_segmentation_degraded = _segment_claims(claims=claims, summary=summary, answer_text=answer_text)

    methods_bits = [f"transport_path={transport_path}"]
    tool_uses = provenance.get("tool_uses") if isinstance(provenance.get("tool_uses"), dict) else {}
    if tool_uses:
        methods_bits.append("tools=" + ", ".join(f"{name}×{count}" for name, count in sorted(tool_uses.items())))
    paper = normalized.get("paper") if isinstance(normalized.get("paper"), dict) else {}
    paper = {
        "abstract": str(paper.get("abstract") or summary),
        "background": str(paper.get("background") or f"Question addressed: {user_query}"),
        "methods": str(paper.get("methods") or "; ".join(methods_bits)),
        "findings": paper.get("findings") if paper.get("findings") is not None else (findings or []),
        "limitations": paper.get("limitations") if paper.get("limitations") is not None else [],
        "next_steps": paper.get("next_steps") if paper.get("next_steps") is not None else [],
        "references": paper.get("references") if paper.get("references") is not None else sources,
    }

    degradation_flags = list(normalized.get("degradation_flags") or [])
    degradation_flags.extend(runtime_state["degradation_flags"])
    fallbacks_used = list(normalized.get("fallbacks_used") or [])
    fallbacks_used.extend(runtime_state["fallbacks_used"])
    capabilities_unavailable = list(normalized.get("capabilities_unavailable") or [])
    capabilities_unavailable.extend(runtime_state["capabilities_unavailable"])
    failure_records = list(normalized.get("failure_records") or [])
    runtime_failure_records = prior_runtime_state.get("failure_records") if isinstance(prior_runtime_state.get("failure_records"), list) else []
    failure_records.extend(runtime_failure_records)
    provenance_failure_records = provenance.get("failure_records") if isinstance(provenance.get("failure_records"), list) else []
    failure_records.extend(provenance_failure_records)
    deduped_failure_records: List[Dict[str, Any]] = []
    seen_failure_keys: set[str] = set()
    for record in failure_records:
        if not isinstance(record, dict):
            continue
        failure_key = "|".join(
            [
                str(record.get("source") or record.get("tool") or "").strip().casefold(),
                str(record.get("failure_reason") or "").strip().casefold(),
                str(record.get("message") or record.get("note") or "").strip(),
            ]
        )
        if failure_key in seen_failure_keys:
            continue
        seen_failure_keys.add(failure_key)
        deduped_failure_records.append(dict(record))
    failure_records = deduped_failure_records

    provenance_errors = provenance.get("errors") if isinstance(provenance.get("errors"), list) else []
    if provenance_errors:
        degradation_flags.append("execution_errors_present")

    configured_providers = [
        str(provider).strip().lower()
        for provider in (providers_state.get("configured") or [])
        if str(provider).strip()
    ]
    degraded_providers = {
        str(provider).strip().lower()
        for provider in (providers_state.get("degraded") or [])
        if str(provider).strip()
    }
    for error in provenance_errors:
        error_text = str(error or "").lower()
        for provider in configured_providers:
            if provider and provider in error_text:
                degraded_providers.add(provider)
    if degraded_providers:
        providers_state["degraded"] = sorted(degraded_providers)
        degradation_flags.append("provider_degradation_detected")

    storage_status = str(storage_state.get("status") or "").lower()
    if storage_status in {"degraded", "failed"}:
        degradation_flags.append("artifact_storage_degraded")
        if not storage_state.get("cloud_backend_configured", False):
            fallbacks_used.append("artifact_storage_local_only")
    if storage_status == "failed":
        capabilities_unavailable.append("cloud_artifact_backend")
    if not claims:
        degradation_flags.append("claim_provenance_missing")
    if not sources:
        degradation_flags.append("official_sources_missing")
    if not findings and not lab_reports:
        degradation_flags.append("evidence_thin")
    if claim_segmentation_degraded:
        degradation_flags.append("claim_segmentation_degraded")

    if not claims:
        fallbacks_used.append("non_claim_structured_output")
    if not sources:
        fallbacks_used.append("uncited_summary_fallback")

    source_lookup = {
        str(source.get("source_id") or "").strip(): source
        for source in sources
        if isinstance(source, dict) and str(source.get("source_id") or "").strip()
    }
    positive_claims = [claim for claim in claims if str(claim.get("claim_kind")) == "positive_scientific"]
    raw_claim_coverage = (
        sum(1 for claim in positive_claims if claim.get("source_ids")) / len(positive_claims)
        if positive_claims
        else 0.0
    )

    all_sources_generic = bool(sources) and all(
        str((source.get("metadata") or {}).get("source_role_type") or "") in {"tooling", "retrieval_diagnostic"}
        for source in sources
    )
    retrieval_failed_text = bool(_RETRIEVAL_FAILURE_RE.search(summary) or _RETRIEVAL_FAILURE_RE.search(answer_text))
    claim_support_scores: List[float] = []
    claims_with_relevant_support = 0
    unsupported_established = 0
    source_max_relevance: Dict[str, float] = {source_id: 0.0 for source_id in source_lookup}
    source_status: Dict[str, str] = {source_id: "irrelevant" for source_id in source_lookup}

    for claim in claims:
        relevant_ids: List[str] = []
        weak_ids: List[str] = []
        irrelevant_ids: List[str] = []
        relevance_details: List[Dict[str, Any]] = []
        support_score = 0.0
        for source_id in claim.get("source_ids") or []:
            source = source_lookup.get(str(source_id))
            if not source:
                continue
            score, status, reasons = _pair_relevance(user_query=user_query, claim=claim, source=source)
            support_score = max(support_score, score)
            source_max_relevance[str(source_id)] = max(source_max_relevance.get(str(source_id), 0.0), score)
            if score >= 0.6:
                relevant_ids.append(str(source_id))
            elif score >= 0.3:
                weak_ids.append(str(source_id))
            else:
                irrelevant_ids.append(str(source_id))
            relevance_details.append(
                {
                    "source_id": str(source_id),
                    "relevance_score": score,
                    "relevance_status": status,
                    "reasons": reasons,
                }
            )
        claim["claim_support_score"] = round(support_score, 4)
        claim["relevant_source_ids"] = relevant_ids
        claim["weakly_relevant_source_ids"] = weak_ids
        claim["irrelevant_source_ids"] = irrelevant_ids
        claim["source_relevance"] = relevance_details

        presentation_mode = str(claim.get("presentation_mode") or "established")
        claim_kind = str(claim.get("claim_kind") or "positive_scientific")
        if claim_kind == "positive_scientific":
            claim_support_scores.append(support_score)
            if support_score >= 0.6:
                claims_with_relevant_support += 1
            if support_score < 0.30 and presentation_mode == "established":
                unsupported_established += 1

        if claim_kind == "positive_scientific":
            if support_score >= 0.6 and presentation_mode not in {"unsupported_flagged", "refusal"}:
                claim["strength"] = "supported"
                claim["confidence"] = max(float(claim.get("confidence", 0.0) or 0.0), 0.8)
            elif support_score >= 0.3 or presentation_mode in {"hedged", "unsupported_flagged"}:
                claim["strength"] = "suggestive"
                claim["confidence"] = min(max(float(claim.get("confidence", 0.0) or 0.0), 0.45), 0.79)
            else:
                claim["strength"] = "unsupported"
                claim["confidence"] = min(float(claim.get("confidence", 0.0) or 0.0), 0.4) or 0.25
        else:
            claim["strength"] = "supported" if support_score >= 0.6 else "suggestive"
            claim["confidence"] = max(float(claim.get("confidence", 0.0) or 0.0), 0.6 if support_score >= 0.6 else 0.45)

    for source_id, source in source_lookup.items():
        max_relevance = round(source_max_relevance.get(source_id, 0.0), 4)
        status = "relevant" if max_relevance >= 0.6 else "weakly_relevant" if max_relevance >= 0.3 else "irrelevant"
        source_status[source_id] = status
        source["relevance_score"] = max_relevance
        source["relevance_status"] = status

    claim_relevance_coverage = round((claims_with_relevant_support / len(positive_claims)) if positive_claims else 0.0, 4)
    unsupported_assertion_rate = round((unsupported_established / len(positive_claims)) if positive_claims else 0.0, 4)
    provenance_relevance_score = round((sum(claim_support_scores) / len(positive_claims)) if positive_claims else 0.0, 4)
    evidentiality_score = round((0.60 * claim_relevance_coverage) + (0.40 * provenance_relevance_score), 4)
    scientific_closure_score = round(
        (0.50 * claim_relevance_coverage) + (0.30 * provenance_relevance_score) + (0.20 * (1 - unsupported_assertion_rate)),
        4,
    )

    abstention_parts = _compute_abstention_scores(summary, claims)
    non_overreach = 1.0 if unsupported_assertion_rate == 0.0 else 0.5 if unsupported_assertion_rate <= 0.20 else 0.0
    abstention_quality_score = round(
        (0.35 * abstention_parts["refusal_clarity"])
        + (0.25 * abstention_parts["limitation_specificity"])
        + (0.20 * non_overreach)
        + (0.20 * abstention_parts["next_step_usefulness"]),
        4,
    )
    scaffold_parts = _compute_structure_scores(answer_text or summary, claims)
    investigative_utility_score = round(
        (0.40 * scaffold_parts["structure_score"])
        + (0.30 * scaffold_parts["actionability_score"])
        + (0.30 * scaffold_parts["gap_clarity_score"]),
        4,
    )

    orchestration_coherence_score = 0.0 if storage_status == "failed" else 1.0 if not provenance_errors and summary else 0.5

    relevant_sources = [source for source in sources if str(source.get("relevance_status") or "") == "relevant"]
    irrelevant_sources = [source for source in sources if str(source.get("relevance_status") or "") == "irrelevant"]
    official_link_count = sum(1 for src in relevant_sources if str(src.get("official_url") or "").strip())
    official_links_available_ratio = round((official_link_count / len(relevant_sources)) if relevant_sources else 0.0, 4)
    provenance_completeness = claim_relevance_coverage

    misleading_support_reasons: List[str] = []
    supported_claim_present = any(str(claim.get("strength") or "") in {"supported", "observed"} for claim in claims)
    if raw_claim_coverage >= 0.8 and provenance_relevance_score < 0.65:
        misleading_support_reasons.append("high attachment-based coverage with low provenance relevance")
    if all_sources_generic:
        misleading_support_reasons.append("all attached sources are generic tooling or retrieval-diagnostic pages")
    if any(_RETRIEVAL_FAILURE_RE.search(_clean_text(source.get("evidence_snippet"))) for source in sources):
        misleading_support_reasons.append("attached source snippets merely restate retrieval failure")
    if retrieval_failed_text and supported_claim_present:
        misleading_support_reasons.append("output admits retrieval failure while retaining supported claims")
    raw_official_ratio = round(
        (sum(1 for src in sources if str(src.get("official_url") or "").strip()) / len(sources)) if sources else 0.0,
        4,
    )
    if raw_official_ratio >= 0.8 and official_links_available_ratio < raw_official_ratio and any(source_status.values()):
        misleading_support_reasons.append("official link ratio is inflated by irrelevant generic sources")
    misleading_support_detected = bool(misleading_support_reasons)

    if misleading_support_detected:
        for claim in claims:
            if str(claim.get("claim_kind") or "") == "positive_scientific" and str(claim.get("strength") or "") in {"supported", "observed"}:
                claim["strength"] = "suggestive" if float(claim.get("claim_support_score", 0.0) or 0.0) >= 0.30 else "unsupported"

    run_status = "ok"
    if storage_status == "failed":
        run_status = "failed"
    elif provenance_errors and not summary:
        run_status = "failed"
    elif misleading_support_detected:
        run_status = "degraded"
    elif degradation_flags:
        run_status = "degraded"

    if run_status == "failed":
        output_mode = "failed"
    elif misleading_support_detected:
        output_mode = "misleading_support_blocked"
    elif (
        positive_claims
        and sources
        and scientific_closure_score >= 0.75
        and provenance_relevance_score >= 0.65
        and unsupported_assertion_rate <= 0.20
    ):
        output_mode = "evidence_backed_answer"
    elif scientific_closure_score < 0.75 and abstention_quality_score >= 0.70 and investigative_utility_score >= 0.50:
        output_mode = "investigative_scaffold"
    elif scientific_closure_score < 0.75 and abstention_quality_score >= 0.70 and investigative_utility_score < 0.50:
        output_mode = "calibrated_abstention"
    else:
        output_mode = "failed"
        degradation_flags.append("epistemic_output_unusable")
        if run_status == "ok":
            run_status = "degraded"

    uncertainty_summary = {
        "evidence_backed_answer": "Evidence-backed answer with explicit provenance.",
        "calibrated_abstention": "Calibrated abstention: the system explicitly refused unsupported closure because evidence was insufficient.",
        "investigative_scaffold": "This artifact is an investigative scaffold, not scientific closure.",
        "misleading_support_blocked": "Publication-style support was blocked because attached sources do not materially support the claims.",
        "failed": "The run did not complete cleanly enough to support any publication-style interpretation.",
    }[output_mode]

    if misleading_support_detected:
        degradation_flags.append("misleading_support_detected")

    contradiction_state = _build_contradiction_state(claims)
    contradiction_count = int(contradiction_state.get("count", 0) or 0)
    figure_provenance = _build_figure_provenance(artifacts, claims, sources)
    hypothesis_registry = _build_hypothesis_registry(claims=claims, summary=summary, query=user_query)
    epistemic_firewall = result.get("epistemic_firewall") or runtime_state.get("epistemic_firewall") or {}
    if not isinstance(epistemic_firewall, dict):
        epistemic_firewall = {}
    cognitive_layer = result.get("cognitive_layer") or prior_runtime_state.get("cognitive_layer") or {}
    if not isinstance(cognitive_layer, dict):
        cognitive_layer = {}
    thermodynamic_routing = result.get("thermodynamic_routing") or prior_runtime_state.get("thermodynamic_routing") or {}
    if not isinstance(thermodynamic_routing, dict):
        thermodynamic_routing = {}
    reinjection_history = result.get("reinjection_history") if "reinjection_history" in result else prior_runtime_state.get("reinjection_history", [])
    if not isinstance(reinjection_history, list):
        reinjection_history = []
    residual_inventory = result.get("residual_inventory") if "residual_inventory" in result else prior_runtime_state.get("residual_inventory", [])
    if not isinstance(residual_inventory, list):
        residual_inventory = []
    branch_tombstones = result.get("branch_tombstones") if "branch_tombstones" in result else prior_runtime_state.get("branch_tombstones", [])
    if not isinstance(branch_tombstones, list):
        branch_tombstones = []
    promotion_ledger = _build_promotion_ledger(result=result, output_mode=output_mode, run_status=run_status)
    dossier_envelope = _build_dossier_envelope(
        result=result,
        query=user_query,
        output_mode=output_mode,
        run_status=run_status,
        degradation_flags=degradation_flags,
        fallbacks_used=fallbacks_used,
        sources=sources,
        relevant_sources=relevant_sources,
        artifacts=artifacts,
        hypothesis_registry=hypothesis_registry,
        contradiction_state=contradiction_state,
        figure_provenance=figure_provenance,
        epistemic_firewall=epistemic_firewall,
        cognitive_layer=cognitive_layer,
        thermodynamic_routing=thermodynamic_routing,
        promotion_ledger=promotion_ledger,
        reinjection_history=reinjection_history,
        residual_inventory=residual_inventory,
        branch_tombstones=branch_tombstones,
        uncertainty_summary=uncertainty_summary,
    )

    rendered_artifact = artifact_renderer.render(
        query=user_query,
        summary=summary,
        paper=paper,
        claims=claims,
        sources=sources,
        output_mode=output_mode,
        metrics={
            "raw_claim_to_source_coverage": round(raw_claim_coverage, 4),
            "claim_relevance_coverage": claim_relevance_coverage,
            "unsupported_assertion_rate": unsupported_assertion_rate,
            "provenance_relevance_score": provenance_relevance_score,
            "evidentiality_score": evidentiality_score,
            "scientific_closure_score": scientific_closure_score,
            "abstention_quality_score": abstention_quality_score,
            "investigative_utility_score": investigative_utility_score,
            "orchestration_coherence_score": orchestration_coherence_score,
            "relevant_source_count": len(relevant_sources),
            "irrelevant_source_count": len(irrelevant_sources),
            "cognitive_layer": cognitive_layer,
            "thermodynamic_routing": thermodynamic_routing,
            "promotion_ledger": promotion_ledger,
        },
        run_status=run_status,
        degradation_flags=sorted(set(str(flag) for flag in degradation_flags if flag)),
        capabilities_unavailable=sorted(set(str(flag) for flag in capabilities_unavailable if flag)),
        fallbacks_used=sorted(set(str(flag) for flag in fallbacks_used if flag)),
        failure_records=failure_records,
        uncertainty_summary=uncertainty_summary,
    )

    normalized.update(
        {
            "schema_version": "mica.final_result.v1",
            "query": normalized.get("query") or user_query,
            "summary": summary,
            "answer": str(rendered_artifact.get("paper_markdown") or normalized.get("answer") or summary),
            "paper": paper,
            "paper_markdown": str(rendered_artifact.get("paper_markdown") or ""),
            "claims": claims,
            "sources": sources,
            "artifacts": artifacts,
            "quality": {
                "score": quality_score,
                "converged": bool(provenance.get("converged", normalized.get("converged", False))),
                "iterations": int(provenance.get("iterations", normalized.get("iterations", 0)) or 0),
            },
            "provenance": provenance,
            "run_status": run_status,
            "degradation_flags": sorted(set(str(flag) for flag in degradation_flags if flag)),
            "capabilities_unavailable": sorted(set(str(flag) for flag in capabilities_unavailable if flag)),
            "fallbacks_used": sorted(set(str(flag) for flag in fallbacks_used if flag)),
            "failure_records": failure_records,
            "transport_path": transport_path,
            "execution_path": result.get("execution_path", transport_path),  # S0.2
            "run_id": result.get("run_id", ""),  # S0.3
            "output_mode": output_mode,
            "orchestration_coherence_score": orchestration_coherence_score,
            "evidentiality_score": evidentiality_score,
            "abstention_quality_score": abstention_quality_score,
            "unsupported_assertion_rate": unsupported_assertion_rate,
            "scientific_closure_score": scientific_closure_score,
            "investigative_utility_score": investigative_utility_score,
            "provenance_relevance_score": provenance_relevance_score,
            "relevant_source_count": len(relevant_sources),
            "irrelevant_source_count": len(irrelevant_sources),
            "claim_relevance_coverage": claim_relevance_coverage,
            "misleading_support_detected": misleading_support_detected,
            "misleading_support_reasons": misleading_support_reasons,
            "claim_segmentation_degraded": claim_segmentation_degraded,
            "evidence_source_count": len(sources),
            "relevant_evidence_source_count": len(relevant_sources),
            "official_links_available_ratio": official_links_available_ratio,
            "provenance_completeness": provenance_completeness,
            "uncertainty_summary": uncertainty_summary,
            "hypothesis_registry": hypothesis_registry,
            "contradiction_state": contradiction_state,
            "figure_provenance": figure_provenance,
            "epistemic_firewall": epistemic_firewall,
            "cognitive_layer": cognitive_layer,
            "thermodynamic_routing": thermodynamic_routing,
            "promotion_ledger": promotion_ledger,
            "reinjection_history": reinjection_history,
            "residual_inventory": residual_inventory,
            "branch_tombstones": branch_tombstones,
            "dossier_envelope": dossier_envelope,
            "capability_envelope": capability_envelope,
        }
    )

    result["runtime"] = {
        **prior_runtime_state,
        **runtime_state,
        "run_status": run_status,
        "output_mode": output_mode,
        "provenance_completeness": provenance_completeness,
        "official_links_available_ratio": official_links_available_ratio,
        "claim_relevance_coverage": claim_relevance_coverage,
        "provenance_relevance_score": provenance_relevance_score,
        "unsupported_assertion_rate": unsupported_assertion_rate,
        "misleading_support_detected": misleading_support_detected,
        "contradiction_count": contradiction_count,
        "figure_provenance_status": figure_provenance.get("status"),
        "hypothesis_count": hypothesis_registry.get("hypothesis_count", 0),
        "epistemic_firewall": epistemic_firewall,
        "cognitive_layer": cognitive_layer,
        "thermodynamic_routing": thermodynamic_routing,
        "promotion_ledger": promotion_ledger,
        "reinjection_history": reinjection_history,
        "residual_inventory": residual_inventory,
        "branch_tombstones": branch_tombstones,
        "providers": providers_state,
        "storage": storage_state,
        "failure_records": failure_records,
        "capability_envelope": capability_envelope,
    }
    result["final_result"] = normalized
    return result


# ────────────────────────────────────────────────────────────────────
# Minimal BSM lab report builder
# ────────────────────────────────────────────────────────────────────

def build_minimal_lab_report(
    *,
    worker_name: str,
    query: str,
    findings_text: str,
    quantitative_metrics: Dict[str, float],
    raw_attachments: List[Any],
) -> Any:
    """Construct a BSM ``LabReport`` (or plain dict if LabReport is mocked)."""
    # If LabReport is mocked, return a simple dict payload
    if not hasattr(LabReport, "model_fields"):
        return {
            "worker": worker_name,
            "query": query,
            "findings": findings_text,
            "metrics": quantitative_metrics,
            "attachments": raw_attachments,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    title = f"AgenticDriver result from {worker_name}: {query[:60]}"
    abstract = (
        f"This report summarizes automated execution by {worker_name} for the query: {query}. "
        f"The system produced structured outputs and tracked reproducibility artifacts where available. "
        f"Key findings are reported along with quantitative metrics and raw attachments for downstream validation."
    )
    if len(abstract) < 220:
        abstract = abstract + " " + (
            "Additional details were omitted for brevity but can be reproduced from the attached artifacts. " * 2
        )

    metadata = ExperimentMetadata(
        title=title,
        principal_investigator=AgentPersona.SYSTEM,
        collaborators=[],
        roadmap_phase="0.0",
        lab_directory="labs/agentic_driver",
    )

    methods = MethodsSection(
        summary=(
            "We executed the assigned worker/tool using the AgenticDriver orchestration layer. "
            "Inputs were derived from the user query and available context. "
            "Outputs were normalized into the BSM LabReport schema for evaluation and provenance tracking."
        ),
        materials=[],
        procedure=[
            "Parse user query and determine the worker to execute.",
            "Invoke specialist driver or MCP tool with best-effort parameters.",
            "Normalize results into structured sections and attach artifacts.",
        ],
        software={"mica": "unknown"},
        parameters={"worker": worker_name},
    )

    results = ResultsSection(
        summary=(
            "The worker produced an output payload and optional artifacts. "
            "Primary findings are listed below and metrics are included for gating and telemetry."
        ),
        primary_findings=[findings_text[:400] if findings_text else "No findings returned."],
        quantitative_metrics={k: float(v) for k, v in (quantitative_metrics or {}).items()},
        qualitative_observations=[],
        raw_data=list(raw_attachments or []),
    )

    discussion = DiscussionSection(
        summary=(
            "The results should be interpreted as an automated first-pass. "
            "Downstream validation is recommended, especially when external tools or databases are involved. "
            "Artifacts are included to facilitate reproducibility and follow-up analysis."
        ),
        interpretation=["Findings provide candidate evidence relevant to the query."],
        limitations=["Some worker outputs may depend on tool availability."],
        future_work=[
            "Run domain-specific validation and integrate structural quality assessment (MQA) where applicable."
        ],
    )

    return LabReport(
        metadata=metadata,
        abstract=abstract,
        methods=methods,
        results=results,
        discussion=discussion,
        references=[],
        supplementary_materials=list(raw_attachments or []),
    )
