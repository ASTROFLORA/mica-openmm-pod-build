"""Build the prompt and tool plan for AgenticDriver.run_streaming."""

from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..runtime_skills import RuntimeSkillPlan
from ...tools_authority.tool_alias_registry import (
    canonical_tool_name_for_command,
    classify_legacy_alias,
)


@dataclass(frozen=True)
class RunStreamingPromptPlan:
    effective_system: str
    effective_mica_tools: List[Dict[str, Any]]
    routing_meta: Dict[str, Any]
    runtime_skill_plan: Any
    all_tools: List[Dict[str, Any]]


_READINESS_PROBE_QUERIES = {
    "ready",
    "ping",
    "health",
    "healthcheck",
    "heartbeat",
    "status",
}


def _is_readiness_probe(query: str) -> bool:
    normalized = " ".join(str(query or "").strip().lower().split())
    return normalized in _READINESS_PROBE_QUERIES


def _resolve_runtime_tool_allowlist() -> List[str]:
    raw = str(os.getenv("MICA_PUBLIC_TOOL_ALLOWLIST", "") or "").strip()
    if not raw:
        return []
    values: List[str] = []
    seen = set()
    for item in raw.split(","):
        normalized = _normalize_allowlisted_tool_name(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return values


def _normalize_allowlisted_tool_name(raw_name: str) -> str:
    normalized = str(raw_name or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("mica."):
        return normalized

    alias_entry = classify_legacy_alias(normalized)
    if alias_entry is not None:
        return alias_entry.canonical_name

    if "." in normalized:
        return canonical_tool_name_for_command(normalized)

    return normalized


def _filter_tool_schemas_by_allowlist(
    tool_schemas: Sequence[Dict[str, Any]],
    allowlist: Sequence[str],
) -> List[Dict[str, Any]]:
    allowed = {
        _normalize_allowlisted_tool_name(item)
        for item in allowlist
        if _normalize_allowlisted_tool_name(item)
    }
    if not allowed:
        return list(tool_schemas)
    return [
        tool
        for tool in list(tool_schemas)
        if _normalize_allowlisted_tool_name(str(tool.get("function", tool).get("name") or "").strip()) in allowed
    ]


def build_run_streaming_prompt_plan(
    *,
    query: str,
    system_prompt: Optional[str],
    loop_system_prompt: str,
    session_id: Optional[str],
    spawn_tools: Sequence[Dict[str, Any]],
    select_effective_tools_for_query_fn: Callable[[str], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
    runtime_skill_overrides_fn: Callable[[], Tuple[List[str], bool, bool]],
    resolve_runtime_skills_fn: Callable[..., Any],
    set_latest_runtime_skill_plan_fn: Callable[[str, Dict[str, Any]], None],
) -> RunStreamingPromptPlan:
    """Compose the effective system prompt, visible tools, and runtime skill plan."""

    if _is_readiness_probe(query):
        runtime_skill_plan = RuntimeSkillPlan()
        set_latest_runtime_skill_plan_fn(session_id or "default", runtime_skill_plan.to_dict())
        return RunStreamingPromptPlan(
            effective_system=(
                "You are MICA. This turn is a runtime readiness probe. "
                "Do not call tools. Reply in one short sentence confirming readiness."
            ),
            effective_mica_tools=[],
            routing_meta={
                "readiness_probe": True,
                "routing_hint": "readiness_probe_fast_path",
                "planned_tool_names": [],
                "intent_tags": ["readiness_probe"],
                "route_card": {
                    "required": False,
                    "planned_tools": [],
                    "required_tool_names": [],
                    "fast_path_reason": "short readiness probe bypasses ToolKG and runtime skill inflation",
                },
                "routed": False,
            },
            runtime_skill_plan=runtime_skill_plan,
            all_tools=[],
        )

    effective_system = system_prompt or loop_system_prompt
    effective_mica_tools, routing_meta = select_effective_tools_for_query_fn(query)
    tool_allowlist = _resolve_runtime_tool_allowlist()
    effective_mica_tools = _filter_tool_schemas_by_allowlist(effective_mica_tools, tool_allowlist)
    filtered_spawn_tools = _filter_tool_schemas_by_allowlist(spawn_tools, tool_allowlist)
    visible_skill_tools = [
        str(tool.get("function", tool).get("name") or "").strip()
        for tool in list(effective_mica_tools) + list(filtered_spawn_tools)
        if str(tool.get("function", tool).get("name") or "").strip()
    ]
    explicit_skill_ids, include_tier_2_skills, disable_auto_skills = runtime_skill_overrides_fn()
    runtime_skill_plan = resolve_runtime_skills_fn(
        query=query,
        visible_tool_names=visible_skill_tools,
        explicit_skill_ids=explicit_skill_ids,
        include_tier_2=include_tier_2_skills,
        disable_auto_skills=disable_auto_skills,
    )
    set_latest_runtime_skill_plan_fn(session_id or "default", runtime_skill_plan.to_dict())

    routing_hint = str(routing_meta.get("routing_hint") or "").strip()
    route_card = routing_meta.get("route_card") if isinstance(routing_meta.get("route_card"), dict) else {}
    if routing_hint:
        effective_system = (
            f"{effective_system}\n\n"
            f"## ToolKG ROUTING PLAN (use these tools FIRST):\n"
            f"{routing_hint}\n"
            f"Prioritize the tools listed above. "
            f"If the plan is degraded, supplement with general search."
        )

    route_card_tools = [str(item).strip() for item in list(route_card.get("planned_tools") or []) if str(item).strip()]
    route_requires_execution = bool(route_card.get("required")) and bool(route_card_tools)
    route_fast_path_reason = str(route_card.get("fast_path_reason") or "").strip()
    if route_requires_execution:
        effective_system = (
            f"{effective_system}\n\n"
            f"## REQUIRED TOOL EXECUTION CONTRACT\n"
            f"This route card is mandatory. Before writing narrative prose, call at least one visible planned tool from: {', '.join(route_card_tools)}.\n"
            f"Do not reply with intention-only text such as 'I'll start by...' or 'First, I will...'.\n"
            f"If a required tool is unavailable, explicitly name the missing tool and why it cannot run."
        )
    if route_card_tools and route_fast_path_reason:
        effective_system = (
            f"{effective_system}\n\n"
            f"## ROUTE CARD FAST PATH\n"
            f"Start with: {', '.join(route_card_tools)}\n"
            f"Reason: {route_fast_path_reason}\n"
            f"Only claim this fast path when those tools are actually visible in the current runtime surface."
        )

    if runtime_skill_plan.prompt_block:
        effective_system = (
            f"{effective_system}\n\n"
            f"## ACTIVE RUNTIME SKILLS (authority-filtered)\n"
            f"{runtime_skill_plan.prompt_block}\n\n"
            f"Only use tools that remain visible in the current route-authority surface."
        )

    return RunStreamingPromptPlan(
        effective_system=effective_system,
        effective_mica_tools=effective_mica_tools,
        routing_meta=routing_meta,
        runtime_skill_plan=runtime_skill_plan,
        all_tools=list(effective_mica_tools) + list(filtered_spawn_tools),
    )
