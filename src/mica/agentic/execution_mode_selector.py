from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal, Tuple

from .backend_command_manifest import get_backend_command_manifest_entry, is_backend_command_name
from .tool_capability_registry import get_tool_capability


AgenticExecutionMode = Literal["tool", "protocol", "gog"]
_SEMANTIC_LANE_HINTS = {
    "agent",
    "artifact",
    "cea",
    "compute",
    "graphrag",
    "graph",
    "kb",
    "literature",
    "lmp",
    "models",
    "mudo",
    "protein",
    "protocol",
    "provider",
    "quetzal",
    "resource",
    "structure",
    "study",
}


@dataclass(frozen=True)
class AgenticExecutionRequest:
    tool_names: Tuple[str, ...] = ()
    child_workflow_refs: Tuple[str, ...] = ()
    expected_artifacts: Tuple[str, ...] = ()
    explicit_lane_hints: Tuple[str, ...] = ()
    requires_provider: bool = False
    durable_replay_required: bool = False
    estimated_steps: int = 0
    goal_hint: str = ""
    # APV-11 driver constitution — when set, overrides composition/gog promotion.
    no_tool: bool = False
    no_mutation: bool = False
    product_intent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgenticExecutionSelection:
    mode: AgenticExecutionMode
    reasons: Tuple[str, ...]
    tool_names: Tuple[str, ...]
    lane_hints: Tuple[str, ...]
    protocol_eligible_tools: Tuple[str, ...]
    gog_eligible_tools: Tuple[str, ...]
    promotable_to_protocol: bool
    promotable_to_gog: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_names(names: Iterable[str]) -> Tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_name in names or ():
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return tuple(ordered)


def _lane_hints_for_tool(tool_name: str) -> Tuple[str, ...]:
    hints: list[str] = []
    try:
        spec = get_tool_capability(tool_name)
    except KeyError:
        spec = None
    if spec is not None:
        for tag in spec.protocol_tags:
            prefix = str(tag or "").split(".", 1)[0].strip().lower()
            if prefix in _SEMANTIC_LANE_HINTS and prefix not in hints:
                hints.append(prefix)
        if spec.requires_provider and "provider" not in hints:
            hints.append("provider")
    if is_backend_command_name(tool_name):
        entry = get_backend_command_manifest_entry(tool_name)
        family = str(entry.family or "").strip().lower()
        if family in _SEMANTIC_LANE_HINTS and family not in hints:
            hints.append(family)
    return tuple(hints)


def _protocol_eligible_tools(tool_names: Iterable[str]) -> Tuple[str, ...]:
    selected: list[str] = []
    for tool_name in _normalize_names(tool_names):
        spec = None
        try:
            spec = get_tool_capability(tool_name)
        except KeyError:
            spec = None
        if spec and spec.protocol_eligible:
            selected.append(tool_name)
            continue
        if is_backend_command_name(tool_name):
            entry = get_backend_command_manifest_entry(tool_name)
            if entry.protocol_step_eligible:
                selected.append(tool_name)
    return tuple(selected)


def _gog_eligible_tools(tool_names: Iterable[str]) -> Tuple[str, ...]:
    selected: list[str] = []
    for tool_name in _normalize_names(tool_names):
        spec = None
        try:
            spec = get_tool_capability(tool_name)
        except KeyError:
            spec = None
        if spec and spec.gog_eligible:
            selected.append(tool_name)
            continue
        if is_backend_command_name(tool_name):
            entry = get_backend_command_manifest_entry(tool_name)
            if entry.campaign_eligible:
                selected.append(tool_name)
    return tuple(selected)


def select_agentic_execution_mode(request: AgenticExecutionRequest) -> AgenticExecutionSelection:
    tool_names = _normalize_names(request.tool_names)
    child_workflow_refs = _normalize_names(request.child_workflow_refs)
    expected_artifacts = _normalize_names(request.expected_artifacts)
    explicit_lane_hints = _normalize_names(request.explicit_lane_hints)
    goal_hint = str(request.goal_hint or "").strip().lower()

    # APV-11: explicit no_tool / product_interview must never promote to protocol/gog.
    if bool(request.no_tool) or str(request.product_intent or "").strip() == "product_interview":
        return AgenticExecutionSelection(
            mode="tool",
            reasons=("explicit_no_tool_constraint",),
            tool_names=(),
            lane_hints=tuple(explicit_lane_hints),
            protocol_eligible_tools=(),
            gog_eligible_tools=(),
            promotable_to_protocol=False,
            promotable_to_gog=False,
        )

    protocol_eligible_tools = _protocol_eligible_tools(tool_names)
    gog_eligible_tools = _gog_eligible_tools(tool_names)

    lane_hints: list[str] = list(explicit_lane_hints)
    for tool_name in tool_names:
        for hint in _lane_hints_for_tool(tool_name):
            if hint not in lane_hints:
                lane_hints.append(hint)

    step_count = max(int(request.estimated_steps or 0), len(tool_names))
    cross_lane = len(lane_hints) >= 2
    requires_provider = bool(request.requires_provider)
    durable_replay_required = bool(request.durable_replay_required)
    has_artifacts = bool(expected_artifacts)
    multi_step = step_count >= 3
    # Ignore composition tokens when they appear only as product narrative ("workspace composition").
    wants_campaign = any(
        token in goal_hint
        for token in ("campaign", "graph-of-graphs", "gog", "compose workflows", "compose child")
    ) and "no tool" not in goal_hint and "product operator interview" not in goal_hint
    has_child_composition = len(child_workflow_refs) >= 2

    reasons: list[str] = []
    if has_child_composition or wants_campaign:
        if has_child_composition:
            reasons.append("multiple_child_workflows")
        if wants_campaign:
            reasons.append("goal_hint_requests_composition")
        mode: AgenticExecutionMode = "gog"
    elif durable_replay_required or has_artifacts or multi_step or cross_lane or requires_provider:
        if durable_replay_required:
            reasons.append("durable_replay_required")
        if has_artifacts:
            reasons.append("artifact_lineage_required")
        if multi_step:
            reasons.append("multi_step_workflow")
        if cross_lane:
            reasons.append("cross_lane_workflow")
        if requires_provider:
            reasons.append("provider_backed_execution")
        mode = "protocol"
    else:
        reasons.append("single_step_atomic_probe")
        mode = "tool"

    promotable_to_protocol = mode in {"protocol", "gog"} or (
        mode == "tool" and (len(tool_names) >= 2 or bool(protocol_eligible_tools))
    )
    promotable_to_gog = mode == "gog" or (mode == "protocol" and len(child_workflow_refs) >= 1)

    return AgenticExecutionSelection(
        mode=mode,
        reasons=tuple(reasons),
        tool_names=tool_names,
        lane_hints=tuple(lane_hints),
        protocol_eligible_tools=protocol_eligible_tools,
        gog_eligible_tools=gog_eligible_tools,
        promotable_to_protocol=promotable_to_protocol,
        promotable_to_gog=promotable_to_gog,
    )
