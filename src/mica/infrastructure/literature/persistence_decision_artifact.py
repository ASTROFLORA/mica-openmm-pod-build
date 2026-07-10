from __future__ import annotations

from typing import Any, Dict, Optional


def _stringify(value: Any) -> str:
    return str(value or "")


def build_acquired_document_decision(doc: Any) -> Dict[str, Any]:
    metadata = getattr(doc, "metadata", {}) or {}
    citations_json_uri = _stringify(
        getattr(doc, "citations_json_uri", "") or metadata.get("citations_json_uri", "")
    )
    return {
        "paper_id": _stringify(getattr(doc, "paper_id", "") or getattr(doc, "document_id", "")),
        "title": _stringify(getattr(doc, "title", "")),
        "provider": _stringify(getattr(doc, "source_provider", "") or getattr(doc, "provider", "")),
        "acquisition_kind": _stringify(getattr(getattr(doc, "acquisition_kind", ""), "value", getattr(doc, "acquisition_kind", ""))),
        "full_text_status": _stringify(getattr(doc, "full_text_status", "")),
        "graph_worthiness_score": float(getattr(doc, "graph_worthiness_score", 0.0) or 0.0),
        "persistence_eligible": bool(getattr(doc, "persistence_eligible", False)),
        "persistence_reason": _stringify(getattr(doc, "persistence_reason", "")),
        "degradation_flags": list(getattr(doc, "degradation_flags", []) or metadata.get("degradation_flags", []) or []),
        "content_uri": _stringify(getattr(doc, "content_uri", "")),
        "normalized_text_uri": _stringify(getattr(doc, "normalized_text_uri", "")),
        "section_json_uri": _stringify(getattr(doc, "section_json_uri", "")),
        "citations_json_uri": citations_json_uri,
        "decision": "persisted" if bool(getattr(doc, "persistence_eligible", False)) else "filtered",
        "backend_status": {
            "gcs": "not_attempted",
            "graphrag": "not_attempted",
            "user_rag": "not_attempted",
            "milvus": "not_attempted",
        },
    }


def build_ingested_paper_decision(paper: Any, assessment: Any) -> Dict[str, Any]:
    paper_id = ""
    get_unique_id = getattr(paper, "get_unique_id", None)
    if callable(get_unique_id):
        paper_id = _stringify(get_unique_id())
    return {
        "paper_id": paper_id or _stringify(getattr(paper, "paper_id", "")),
        "title": _stringify(getattr(paper, "title", "")),
        "provider": _stringify(getattr(paper, "platform", "")),
        "graph_worthiness_score": float(getattr(assessment, "graph_worthiness_score", 0.0) or 0.0),
        "persistence_eligible": bool(getattr(assessment, "persistence_eligible", False)),
        "persistence_reason": _stringify(getattr(assessment, "persistence_reason", "")),
        "has_full_text": bool(getattr(paper, "full_text", "")),
        "has_pdf": bool(getattr(paper, "pdf_path", None)),
        "decision": "persisted" if bool(getattr(assessment, "persistence_eligible", False)) else "filtered",
        "backend_status": {
            "gcs": "not_attempted",
            "user_rag": "not_attempted",
            "milvus": "not_attempted",
        },
    }


def update_backend_status(
    decision: Dict[str, Any],
    backend: str,
    status: str,
    *,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    backend_status = decision.get("backend_status")
    if not isinstance(backend_status, dict):
        backend_status = {}
        decision["backend_status"] = backend_status
    backend_status[str(backend)] = str(status)
    if detail:
        decision[f"{backend}_detail"] = str(detail)
    return decision