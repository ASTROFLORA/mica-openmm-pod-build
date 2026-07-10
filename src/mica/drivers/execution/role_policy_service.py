"""Role policy helpers extracted from AgenticDriver."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence


def filter_tools_for_role(
    role_spec: Any,
    available_tools: List[Dict[str, Any]],
    spawn_tools: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Filter tool schemas by the role's visible-tools contract."""
    spawn_names = {
        tool.get("function", {}).get("name", "")
        for tool in spawn_tools
    }
    visible_tools = getattr(role_spec, "visible_tools", None)
    if visible_tools is None:
        return [
            tool for tool in available_tools
            if tool.get("function", {}).get("name", "") not in spawn_names
        ]
    return [
        tool for tool in available_tools
        if tool.get("function", {}).get("name", "") in visible_tools
        and tool.get("function", {}).get("name", "") not in spawn_names
    ]


def run_output_invariants(
    role_spec: Any,
    synthesis: str,
    role_ctx: Any,
) -> List[Dict[str, Any]]:
    """Run output invariant checks and project only failed violations."""
    violations: List[Dict[str, Any]] = []
    for invariant in getattr(role_spec, "output_invariants", []) or []:
        try:
            passed = invariant.check(synthesis, role_ctx)
        except Exception:
            passed = True
        if not passed:
            violations.append({
                "name": invariant.name,
                "severity": invariant.severity,
                "description": invariant.description,
            })
    return violations