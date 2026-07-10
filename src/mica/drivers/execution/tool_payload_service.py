"""Tool payload/status helpers extracted from AgenticDriver execution loop."""

import json
from typing import Any, Dict, List, Optional, Tuple


def tool_status_response(
    status: str,
    tool_name: str,
    note: str,
    *,
    args_payload: Optional[Dict[str, Any]] = None,
    failure_reason: Optional[str] = None,
    is_synthetic: bool = False,
    dependency_state: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    from mica.agentic.tool_capability_registry import get_tool_capability

    spec = get_tool_capability(tool_name)
    payload: Dict[str, Any] = {
        "status": status,
        "tool": tool_name,
        "note": note,
        "capability_mode": spec.capability_mode,
        "is_synthetic": bool(is_synthetic),
        "scientific_result_valid": False,
    }
    if failure_reason:
        payload["failure_reason"] = failure_reason
    if args_payload is not None:
        payload["args"] = args_payload
    if dependency_state is not None:
        payload["dependency_state"] = dependency_state
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


def degraded_tool_response(
    tool_name: str,
    note: str,
    *,
    args_payload: Optional[Dict[str, Any]] = None,
    failure_reason: Optional[str] = None,
    is_synthetic: bool = False,
    dependency_state: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    return tool_status_response(
        "degraded",
        tool_name,
        note,
        args_payload=args_payload,
        failure_reason=failure_reason,
        is_synthetic=is_synthetic,
        dependency_state=dependency_state,
        extra=extra,
    )


def unavailable_tool_response(
    tool_name: str,
    note: str,
    *,
    args_payload: Optional[Dict[str, Any]] = None,
    failure_reason: Optional[str] = None,
    dependency_state: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    return tool_status_response(
        "unavailable",
        tool_name,
        note,
        args_payload=args_payload,
        failure_reason=failure_reason,
        dependency_state=dependency_state,
        extra=extra,
    )


def coerce_seed_entities(raw_args: Dict[str, Any]) -> Tuple[str, List[str]]:
    raw_entities = raw_args.get("entities") or []
    entities: List[str] = []
    if isinstance(raw_entities, str):
        entities = [item.strip() for item in raw_entities.split(",") if item.strip()]
    elif isinstance(raw_entities, list):
        entities = [str(item).strip() for item in raw_entities if str(item).strip()]
    raw_seeds = raw_args.get("seeds") or []
    seeds: List[str] = []
    if isinstance(raw_seeds, str):
        seeds = [item.strip() for item in raw_seeds.split(",") if item.strip()]
    elif isinstance(raw_seeds, list):
        seeds = [str(item).strip() for item in raw_seeds if str(item).strip()]
    query_text = str(raw_args.get("query") or ", ".join(entities)).strip()
    return query_text, (seeds or entities)


def transport_payload_or_degraded(
    tool_name: str,
    transport_result: Dict[str, Any],
    *,
    args_payload: Optional[Dict[str, Any]] = None,
) -> str:
    error_text = str(transport_result.get("error") or "").strip()
    if transport_result.get("status") == "FAILED" and error_text.startswith("No backend available for worker"):
        return unavailable_tool_response(
            tool_name,
            "This tool currently requires the backend/API path and that dependency is unavailable in the active runtime.",
            args_payload=args_payload,
            failure_reason="BACKEND_UNAVAILABLE",
            extra={
                "backend_type": transport_result.get("backend_type"),
                "detail": error_text,
            },
        )
    return json.dumps(transport_result, ensure_ascii=False, default=str)


def payload_has_no_scientific_results(tool_name: str, payload: Dict[str, Any]) -> bool:
    if tool_name in {"search_protein", "search_protein_metadata", "advanced_protein_search"}:
        return int(payload.get("count") or 0) == 0 and not list(payload.get("entries") or [])
    if tool_name in {
        "search_literature",
        "run_dlm_scan",
        "run_bibliotecario_scan",
        "analyse_knowledge_decay",
        "analyse_citation_impact",
        "track_entity_evolution",
        "query_co_occurrence",
        "run_deep_research",
        "compile_research_briefing",
        "scan_drug_repurposing",
        "run_cascade_pipeline",
        "consult_bibliotecario",
    }:
        paper_count = payload.get("papers_found")
        if paper_count is None:
            paper_count = payload.get("total_papers")
        if paper_count is None:
            paper_count = len(list(payload.get("papers") or payload.get("results") or []))
        return int(paper_count or 0) == 0 and not list(payload.get("papers") or payload.get("results") or [])
    return False


def source_health_status(source_health: Any) -> str:
    if not isinstance(source_health, dict) or not source_health:
        return "unknown"
    statuses = [str((entry or {}).get("status") or "unknown").strip().lower() for entry in source_health.values()]
    statuses = [status for status in statuses if status]
    if statuses and all(status == "unhealthy" for status in statuses):
        return "unhealthy"
    if any(status == "unhealthy" for status in statuses):
        return "degraded"
    if any(status == "healthy" for status in statuses):
        return "healthy"
    return "unknown"


def normalize_tool_payload(
    tool_name: str,
    raw_text: str,
    *,
    dependency_state: Dict[str, Any],
) -> str:
    from mica.agentic.tool_capability_registry import get_tool_capability

    spec = get_tool_capability(tool_name)
    try:
        payload = json.loads(raw_text)
    except Exception:
        return raw_text
    if not isinstance(payload, dict):
        return raw_text

    normalized = dict(payload)
    normalized.setdefault("tool", tool_name)
    normalized.setdefault("capability_mode", spec.capability_mode)
    normalized.setdefault("dependency_state", dependency_state)
    normalized.setdefault("is_synthetic", False)

    if normalized.get("error") and str(normalized.get("status") or "").strip().lower() not in {"degraded", "unavailable", "error"}:
        normalized["status"] = "error"
        normalized.setdefault("failure_reason", "INTERNAL_ERROR")
        normalized.setdefault("note", str(normalized.get("error") or "Tool execution failed."))

    if str(normalized.get("status") or "").strip().lower() not in {"ok", "degraded", "unavailable", "error"}:
        normalized["status"] = "ok"

    if normalized["status"] == "ok":
        no_results = payload_has_no_scientific_results(tool_name, normalized)
        if no_results:
            source_health = normalized.get("source_health")
            source_health_value = source_health_status(source_health)
            network_state = dependency_state.get("network") or {}
            if source_health_value == "unhealthy":
                normalized["status"] = "unavailable"
                normalized["failure_reason"] = "NETWORK_UNAVAILABLE"
                normalized.setdefault(
                    "note",
                    "All requested upstream literature providers failed in the active runtime.",
                )
                normalized["scientific_result_state"] = "NETWORK_UNAVAILABLE"
            elif source_health_value == "degraded" or (network_state.get("required") and network_state.get("unreachable_hosts")):
                normalized["scientific_result_state"] = "NO_RESULTS_PARTIAL_DEPENDENCY_COVERAGE"
            else:
                normalized["scientific_result_state"] = "NO_RESULTS"
            normalized["scientific_result_valid"] = False
        else:
            normalized["scientific_result_state"] = "HAS_RESULTS"
            normalized["scientific_result_valid"] = True
    else:
        normalized.setdefault("scientific_result_valid", False)

    if spec.placeholder_policy == "synthetic_only" and normalized.get("status") != "ok":
        normalized["is_synthetic"] = True
        normalized.setdefault("failure_reason", "PLACEHOLDER_ONLY")

    return json.dumps(normalized, ensure_ascii=False, default=str)