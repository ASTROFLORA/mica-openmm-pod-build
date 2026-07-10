"""Tool surface preparation helpers extracted from AgenticDriver loop executor."""

from typing import Any, Callable, Dict, List, Sequence, Tuple

from ...agentic.tool_capability_registry import filter_tools_for_lane  # noqa: F401 (re-export)


def collect_named_tools(tool_schemas: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for tool in tool_schemas:
        name = str(tool.get("function", tool).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def prepare_tool_surface(
    *,
    effective_mica_tools: Sequence[Dict[str, Any]],
    spawn_tools: Sequence[Dict[str, Any]],
    validate_registry_coverage_fn: Callable[[List[str]], List[str]],
) -> Tuple[List[str], List[str], List[str]]:
    public_tool_names = collect_named_tools(effective_mica_tools)
    spawn_tool_names = collect_named_tools(spawn_tools)
    missing_registry_entries = validate_registry_coverage_fn(public_tool_names + spawn_tool_names)
    return public_tool_names, spawn_tool_names, list(missing_registry_entries or [])