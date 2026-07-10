from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_GENE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9-]{2,9}\b")
_GENE_STOPWORDS = {
    "AND",
    "ARE",
    "CELL",
    "DATA",
    "DNA",
    "FOR",
    "MAP",
    "NEW",
    "NOT",
    "RNA",
    "THE",
    "USE",
    "VIA",
}


def _graph_promotion_state(summary: Dict[str, Any]) -> str:
    payload = dict(summary or {})
    if not payload.get("enabled"):
        return "skipped"
    if int(payload.get("promoted_papers") or 0) > 0:
        return "degraded" if list(payload.get("errors") or []) else "succeeded"
    if list(payload.get("errors") or []):
        return "degraded"
    if int(payload.get("attempted_papers") or 0) > 0:
        return "attempted"
    return "skipped"


def _best_candidate_text(entity: Dict[str, Any]) -> str:
    for key in ("text", "name", "canonical_name", "mention", "entity"):
        value = str(entity.get(key) or "").strip()
        if value:
            return value
    return ""


def _candidate_entity_type(entity: Dict[str, Any]) -> str:
    return str(entity.get("entity_type") or entity.get("type") or entity.get("kind") or "unknown").strip().lower()


def _extract_scan_entities(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = dict((paper or {}).get("metadata") or {})
    document_scan = dict(metadata.get("document_scan") or {})
    out: List[Dict[str, Any]] = []
    for source in (metadata.get("document_scan_entities"), document_scan.get("entities")):
        for entity in list(source or []):
            if isinstance(entity, dict):
                out.append(entity)
    return out


def select_lmp_candidates(
    *,
    query: str,
    papers: Sequence[Dict[str, Any]],
    max_candidates: int = 3,
) -> List[Dict[str, Any]]:
    scored: Dict[str, Dict[str, Any]] = {}

    def _remember(candidate: Dict[str, Any]) -> None:
        key = str(candidate.get("uniprot_id") or candidate.get("text") or "").strip().upper()
        if not key:
            return
        current = scored.get(key)
        if current is None or float(candidate.get("score") or 0.0) > float(current.get("score") or 0.0):
            scored[key] = candidate

    for paper in list(papers or []):
        for entity in _extract_scan_entities(paper):
            text = _best_candidate_text(entity)
            entity_type = _candidate_entity_type(entity)
            uniprot_id = str(entity.get("uniprot_id") or entity.get("accession") or entity.get("kb_id") or "").strip()
            if not text and not uniprot_id:
                continue
            if entity_type and all(token not in entity_type for token in ("protein", "gene", "uniprot")) and not uniprot_id:
                continue
            score = 0.55
            if uniprot_id:
                score += 0.25
            if "protein" in entity_type:
                score += 0.2
            elif "gene" in entity_type:
                score += 0.15
            _remember(
                {
                    "text": text or uniprot_id,
                    "entity_type": entity_type or "protein",
                    "uniprot_id": uniprot_id or None,
                    "score": round(min(score, 0.99), 4),
                    "source": "document_scan",
                    "paper_id": str(paper.get("canonical_id") or paper.get("paperId") or "") or None,
                }
            )
        title = str((paper or {}).get("title") or "")
        for token in _GENE_TOKEN_RE.findall(title):
            if token in _GENE_STOPWORDS:
                continue
            _remember(
                {
                    "text": token,
                    "entity_type": "gene_symbol",
                    "uniprot_id": None,
                    "score": 0.42,
                    "source": "title_regex",
                    "paper_id": str(paper.get("canonical_id") or paper.get("paperId") or "") or None,
                }
            )

    if not scored:
        for token in _GENE_TOKEN_RE.findall(str(query or "")):
            if token in _GENE_STOPWORDS:
                continue
            _remember(
                {
                    "text": token,
                    "entity_type": "query_regex",
                    "uniprot_id": None,
                    "score": 0.35,
                    "source": "query_regex",
                    "paper_id": None,
                }
            )

    ordered = sorted(scored.values(), key=lambda item: (float(item.get("score") or 0.0), bool(item.get("uniprot_id"))), reverse=True)
    return ordered[: max(0, int(max_candidates))]


def build_lmp_closure_metadata(
    *,
    query: str,
    papers: Sequence[Dict[str, Any]],
    user_id: Optional[str],
    attempt_lmp: bool,
    max_candidates: int = 1,
    bridge: Any = None,
    convergence_factory: Any = None,
) -> Dict[str, Any]:
    candidates = select_lmp_candidates(query=query, papers=papers, max_candidates=max(1, int(max_candidates or 1)) * 3)
    result: Dict[str, Any] = {
        "state": "skipped",
        "attempted": False,
        "candidate_count": len(candidates),
        "selected_candidates": [
            {
                "text": str(candidate.get("text") or ""),
                "entity_type": str(candidate.get("entity_type") or "unknown"),
                "uniprot_id": candidate.get("uniprot_id"),
                "score": float(candidate.get("score") or 0.0),
            }
            for candidate in candidates[: max(0, int(max_candidates or 1))]
        ],
        "resolved_candidates": [],
        "skip_reasons": [],
        "errors": [],
        "xml_generated": False,
    }
    if not attempt_lmp:
        result["skip_reasons"].append("lmp_closure_not_requested")
        return result
    if not candidates:
        result["skip_reasons"].append("no_high_confidence_lmp_candidates")
        return result

    try:
        if bridge is None:
            from mica.drivers.dlm_lmp_bridge import get_bridge

            bridge = get_bridge()
    except Exception as exc:
        result["state"] = "degraded"
        result["errors"].append({"stage": "bridge_init", "error": str(exc)})
        return result

    try:
        convergence_factory = convergence_factory or __import__(
            "mica.memory.dlm_lmp.convergence",
            fromlist=["LMPDLMConvergence"],
        ).LMPDLMConvergence
    except Exception as exc:
        result["state"] = "degraded"
        result["errors"].append({"stage": "convergence_init", "error": str(exc)})
        return result

    resolver = getattr(bridge, "preset_resolver", None)
    for candidate in candidates[: max(1, int(max_candidates or 1))]:
        candidate_query = str(candidate.get("uniprot_id") or candidate.get("text") or "").strip()
        if not candidate_query:
            continue
        result["attempted"] = True
        try:
            bridge_result = bridge.process_query(candidate_query, tool_type="pubmed")
        except Exception as exc:
            result["errors"].append({"candidate": candidate_query, "stage": "bridge_process_query", "error": str(exc)})
            continue

        accession = str(candidate.get("uniprot_id") or "").strip()
        if not accession:
            biological_context = getattr(bridge_result, "biological_context", None)
            if biological_context is not None:
                accession = str(getattr(biological_context, "uniprot_id", "") or "").strip()
        if not accession:
            for mapping in list(getattr(getattr(bridge_result, "linked", None), "uniprot_mappings", []) or []):
                accession = str(getattr(mapping, "kb_id", "") or "").strip()
                if accession:
                    break
        if not accession:
            result["skip_reasons"].append(f"unresolved_candidate:{candidate_query}")
            continue
        if resolver is None:
            result["skip_reasons"].append(f"missing_preset_resolver:{accession}")
            continue
        xml_path = resolver.resolve(accession) or resolver.resolve_by_gene_name(str(candidate.get("text") or accession))
        if not xml_path:
            result["skip_reasons"].append(f"missing_lmp_preset:{accession}")
            continue

        try:
            xml_text = Path(xml_path).read_text(encoding="utf-8")
            convergence = convergence_factory()
            merged_xml = convergence.merge(
                lmp_xml_string=xml_text,
                dlm_papers=list(papers or [])[:25],
                dlm_entities=[
                    {
                        "text": str(candidate.get("text") or accession),
                        "entity_type": str(candidate.get("entity_type") or "protein"),
                        "uniprot_id": accession,
                    }
                ],
                user_id=user_id,
            )
            result["state"] = "succeeded"
            result["xml_generated"] = bool(merged_xml)
            result["resolved_candidates"].append(
                {
                    "text": str(candidate.get("text") or accession),
                    "uniprot_id": accession,
                    "preset_path": str(xml_path),
                }
            )
            break
        except Exception as exc:
            logger.warning("Literature LMP closure failed for %s: %s", accession, exc)
            result["errors"].append({"candidate": accession, "stage": "convergence_merge", "error": str(exc)})

    if result["state"] != "succeeded":
        if result["errors"]:
            result["state"] = "degraded"
        elif result["attempted"]:
            result["state"] = "skipped"
            if not result["skip_reasons"]:
                result["skip_reasons"].append("lmp_trigger_not_satisfied")
    return result


def build_consumption_closure(
    *,
    query: str,
    papers: Sequence[Dict[str, Any]],
    persisted_claim_atoms: Optional[Sequence[Dict[str, Any]]] = None,
    paper_graph_promotion: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    attempt_lmp: bool = False,
    max_lmp_candidates: int = 1,
    bridge: Any = None,
    convergence_factory: Any = None,
) -> Dict[str, Any]:
    graph_summary = dict(paper_graph_promotion or {"enabled": False})
    consumable_fact_count = len(list(persisted_claim_atoms or []))
    if consumable_fact_count <= 0:
        consumable_fact_count = sum(len(list((paper or {}).get("claim_atoms") or [])) for paper in list(papers or []))
    lmp_candidates = select_lmp_candidates(query=query, papers=papers, max_candidates=max(1, int(max_lmp_candidates or 1)) * 3)
    lmp_closure = build_lmp_closure_metadata(
        query=query,
        papers=papers,
        user_id=user_id,
        attempt_lmp=attempt_lmp,
        max_candidates=max_lmp_candidates,
        bridge=bridge,
        convergence_factory=convergence_factory,
    )
    graph_state = _graph_promotion_state(graph_summary)
    overall_state = "succeeded" if graph_state == "succeeded" or lmp_closure.get("state") == "succeeded" else "skipped"
    if graph_state == "degraded" or lmp_closure.get("state") == "degraded":
        overall_state = "degraded"
    elif graph_state == "attempted":
        overall_state = "attempted"
    return {
        "state": overall_state,
        "graph_promotion_state": graph_state,
        "graph_promotion": graph_summary,
        "consumable_fact_count": int(consumable_fact_count),
        "lmp_candidate_count": len(lmp_candidates),
        "lmp_candidates": [
            {
                "text": str(candidate.get("text") or ""),
                "entity_type": str(candidate.get("entity_type") or "unknown"),
                "uniprot_id": candidate.get("uniprot_id"),
                "score": float(candidate.get("score") or 0.0),
            }
            for candidate in lmp_candidates[: max(0, int(max_lmp_candidates or 1))]
        ],
        "lmp_closure": lmp_closure,
    }