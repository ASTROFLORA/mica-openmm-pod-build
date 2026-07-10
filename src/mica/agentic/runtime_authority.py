from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence


def _dedupe(values: Iterable[Any]) -> List[str]:
    ordered: List[str] = []
    seen = set()
    for raw in values or ():
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


@dataclass(frozen=True)
class RuntimeAuthorityBundle:
    route_card_id: str
    lane_class: str
    authority: str
    visible_tools: List[str]
    invocable_tools: List[str]
    invocable_public_tools: List[str]
    internal_spawn_tools: List[str]
    required_tool_names: List[str]
    missing_required_tools: List[str]
    required_capabilities: List[str]
    blocked_capabilities: List[Dict[str, Any]]
    hard_block_reasons: List[str]
    allowed_providers: List[str]
    resolved_provider: str | None
    required_closure_stages: List[str]
    checkpoint_policy: List[str]
    publication_gate_mode: str
    degrade_mode: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def resolve_runtime_authority(
    *,
    route_card: Mapping[str, Any] | None,
    visible_tool_names: Sequence[str] | None,
    selected_tool_names: Sequence[str] | None,
    internal_spawn_tools: Sequence[str] | None = None,
    provider_id: str | None = None,
    configured_providers: Sequence[str] | None = None,
) -> RuntimeAuthorityBundle:
    card = dict(route_card or {})
    required = bool(card.get("required"))
    required_tool_names = _dedupe(card.get("required_tool_names") or ())
    planned_tool_names = _dedupe(card.get("planned_tools") or ())
    visible_public_tools = _dedupe(visible_tool_names or selected_tool_names or ())
    selected_public_tools = _dedupe(selected_tool_names or visible_tool_names or ())
    if required:
        visible_public_tools = _dedupe(list(visible_public_tools) + planned_tool_names + required_tool_names)
        selected_public_tools = _dedupe(list(selected_public_tools) + required_tool_names)
    spawn_tools = _dedupe(internal_spawn_tools or ())
    invocable_tools = _dedupe(list(selected_public_tools) + spawn_tools + required_tool_names)
    allowed_providers = _dedupe(configured_providers or ([provider_id] if provider_id else ()))
    resolved_provider = str(provider_id or "").strip() or None
    missing_required_tools = [name for name in required_tool_names if name not in selected_public_tools]
    blocked_capabilities = [
        dict(item)
        for item in list(card.get("blocked_capabilities") or ())
        if isinstance(item, Mapping)
    ]
    hard_block_reasons = [
        str(item.get("detail") or "").strip()
        for item in blocked_capabilities
        if str(item.get("detail") or "").strip()
    ]
    if required and missing_required_tools:
        detail = (
            "Route authority requires the following public tools before embodiment: "
            + ", ".join(missing_required_tools)
        )
        blocked_capabilities.append(
            {
                "mechanism": "RUNTIME_AUTHORITY_MISSING_REQUIRED_TOOLS",
                "detail": detail,
                "missing_required_tools": list(missing_required_tools),
            }
        )
        hard_block_reasons.append(detail)
    return RuntimeAuthorityBundle(
        route_card_id=str(card.get("route_card_id") or ""),
        lane_class=str(card.get("lane_class") or "general"),
        authority=str(card.get("authority") or ("mandatory" if required else "advisory")),
        visible_tools=visible_public_tools,
        invocable_tools=invocable_tools,
        invocable_public_tools=list(selected_public_tools),
        internal_spawn_tools=spawn_tools,
        required_tool_names=required_tool_names,
        missing_required_tools=missing_required_tools,
        required_capabilities=_dedupe(card.get("required_capabilities") or ()),
        blocked_capabilities=blocked_capabilities,
        hard_block_reasons=_dedupe(hard_block_reasons),
        allowed_providers=allowed_providers,
        resolved_provider=resolved_provider,
        required_closure_stages=_dedupe(card.get("required_closure_stages") or ()),
        checkpoint_policy=["route_card_enforced", "required_tools_bound"] if required else ["best_effort"],
        publication_gate_mode="required" if required else "best_effort",
        degrade_mode="fail_closed" if required else "best_effort",
    )


def authorize_tool_invocation(bundle: Mapping[str, Any] | RuntimeAuthorityBundle | None, tool_name: str) -> Dict[str, Any]:
    authority = bundle.to_dict() if isinstance(bundle, RuntimeAuthorityBundle) else dict(bundle or {})
    normalized_tool_name = str(tool_name or "").strip()
    invocable_tools = _dedupe(authority.get("invocable_tools") or ())
    allowed = normalized_tool_name in invocable_tools if normalized_tool_name else False
    return {
        "allowed": allowed,
        "tool_name": normalized_tool_name,
        "route_card_id": str(authority.get("route_card_id") or ""),
        "lane_class": str(authority.get("lane_class") or "general"),
        "authority": str(authority.get("authority") or "advisory"),
        "degrade_mode": str(authority.get("degrade_mode") or "best_effort"),
        "publication_gate_mode": str(authority.get("publication_gate_mode") or "best_effort"),
        "required_tool_names": _dedupe(authority.get("required_tool_names") or ()),
        "missing_required_tools": _dedupe(authority.get("missing_required_tools") or ()),
        "required_capabilities": _dedupe(authority.get("required_capabilities") or ()),
        "required_closure_stages": _dedupe(authority.get("required_closure_stages") or ()),
        "invocable_tools": invocable_tools,
        "hard_block_reasons": _dedupe(authority.get("hard_block_reasons") or ()),
        "reason": "TOOL_NOT_AUTHORIZED_FOR_ROUTE" if not allowed else "AUTHORIZED",
    }