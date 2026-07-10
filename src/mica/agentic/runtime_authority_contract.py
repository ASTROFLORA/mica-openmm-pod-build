from __future__ import annotations

from hashlib import sha1
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from mica.agentic.tool_capability_registry import get_tool_capability, registry_items


def _dedupe_strs(values: Iterable[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
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


def _sorted_unique(values: Iterable[Any]) -> list[str]:
    return sorted(_dedupe_strs(values), key=str.casefold)


def _context_hash(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(payload.keys(), key=str.casefold):
        value = payload[key]
        if isinstance(value, Mapping):
            parts.append(f"{key}={_context_hash(dict(value))}")
        elif isinstance(value, (list, tuple, set, frozenset)):
            parts.append(f"{key}=[{','.join(_dedupe_strs(value))}]")
        else:
            parts.append(f"{key}={str(value or '').strip()}")
    return sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


class BlockedCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    detail: str = Field(..., min_length=1)
    capability_id: str = ""
    tool_name: str = ""
    severity: str = Field(default="error")
    missing_dependencies: list[str] = Field(default_factory=list)


class CapabilityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(..., min_length=1)
    tool_ids: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    required_providers: list[str] = Field(default_factory=list)
    failure_mode: str = Field(default="fail_closed")
    cost_class: str = Field(default="standard")
    side_effect_class: str = Field(default="read")
    route_authority: str = Field(default="optional")
    closure_stage: str = ""


class SessionContextEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    route_card_id: str = ""
    route_class: str = "general"
    authority: str = "advisory"
    selected_tool_names: list[str] = Field(default_factory=list)
    visible_tool_names: list[str] = Field(default_factory=list)
    internal_spawn_tools: list[str] = Field(default_factory=list)
    role_local_tools: list[str] = Field(default_factory=list)
    configured_providers: list[str] = Field(default_factory=list)
    provider_id: str | None = None
    required_tool_names: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    required_closure_stages: list[str] = Field(default_factory=list)
    active_skill_ids: list[str] = Field(default_factory=list)
    requested_skill_ids: list[str] = Field(default_factory=list)
    blocked_capabilities: list[BlockedCapability] = Field(default_factory=list)
    no_tool_justification: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "SessionContextEnvelope":
        raw = dict(payload or {})
        route_card = raw.get("route_card") if isinstance(raw.get("route_card"), Mapping) else {}
        authority = str(raw.get("authority") or route_card.get("authority") or "advisory").strip() or "advisory"
        route_class = str(raw.get("route_class") or raw.get("lane_class") or route_card.get("lane_class") or "general").strip() or "general"
        blocked_items = raw.get("blocked_capabilities")
        if not isinstance(blocked_items, list):
            blocked_items = route_card.get("blocked_capabilities") if isinstance(route_card.get("blocked_capabilities"), list) else []
        return cls(
            route_card_id=str(raw.get("route_card_id") or route_card.get("route_card_id") or "").strip(),
            route_class=route_class,
            authority=authority,
            selected_tool_names=_dedupe_strs(raw.get("selected_tool_names") or route_card.get("selected_tool_names") or ()),
            visible_tool_names=_dedupe_strs(raw.get("visible_tool_names") or route_card.get("visible_tool_names") or ()),
            internal_spawn_tools=_dedupe_strs(raw.get("internal_spawn_tools") or ()),
            role_local_tools=_dedupe_strs(raw.get("role_local_tools") or ()),
            configured_providers=_dedupe_strs(raw.get("configured_providers") or raw.get("allowed_providers") or ()),
            provider_id=str(raw.get("provider_id") or raw.get("resolved_provider") or "").strip() or None,
            required_tool_names=_dedupe_strs(raw.get("required_tool_names") or route_card.get("required_tool_names") or ()),
            required_capabilities=_dedupe_strs(raw.get("required_capabilities") or route_card.get("required_capabilities") or ()),
            required_closure_stages=_dedupe_strs(raw.get("required_closure_stages") or route_card.get("required_closure_stages") or ()),
            active_skill_ids=_dedupe_strs(raw.get("active_skill_ids") or route_card.get("active_skill_ids") or ()),
            requested_skill_ids=_dedupe_strs(raw.get("requested_skill_ids") or route_card.get("requested_skill_ids") or ()),
            blocked_capabilities=[BlockedCapability.model_validate(item) for item in blocked_items if isinstance(item, Mapping)],
            no_tool_justification=str(raw.get("no_tool_justification") or route_card.get("no_tool_justification") or "").strip(),
        )


class CapabilityBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_card_id: str = ""
    route_class: str = "general"
    role_id: str = "default"
    user_id: str = ""
    authority: str = "advisory"
    allowed_public_tools: list[str] = Field(default_factory=list)
    allowed_spawn_tools: list[str] = Field(default_factory=list)
    allowed_role_local_tools: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    required_tool_names: list[str] = Field(default_factory=list)
    missing_required_tools: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    active_skill_ids: list[str] = Field(default_factory=list)
    requested_skill_ids: list[str] = Field(default_factory=list)
    capability_bindings: list[CapabilityBinding] = Field(default_factory=list)
    blocked_capabilities: list[BlockedCapability] = Field(default_factory=list)
    hard_block_reasons: list[str] = Field(default_factory=list)
    allowed_providers: list[str] = Field(default_factory=list)
    resolved_provider: str | None = None
    required_closure_stages: list[str] = Field(default_factory=list)
    degrade_mode: str = "best_effort"
    publication_gate_mode: str = "best_effort"
    session_context_hash: str = ""
    no_tool_justification: str = ""

    def to_runtime_authority_dict(self) -> dict[str, Any]:
        return {
            "route_card_id": self.route_card_id,
            "lane_class": self.route_class,
            "authority": self.authority,
            "allowed_tools": list(self.allowed_tools),
            "visible_tools": list(self.allowed_public_tools),
            "invocable_public_tools": list(self.allowed_public_tools),
            "internal_spawn_tools": list(self.allowed_spawn_tools),
            "invocable_tools": list(self.allowed_tools),
            "required_tool_names": list(self.required_tool_names),
            "missing_required_tools": list(self.missing_required_tools),
            "required_capabilities": list(self.required_capabilities),
            "active_skill_ids": list(self.active_skill_ids),
            "requested_skill_ids": list(self.requested_skill_ids),
            "blocked_capabilities": [item.model_dump() for item in self.blocked_capabilities],
            "hard_block_reasons": list(self.hard_block_reasons),
            "allowed_providers": list(self.allowed_providers),
            "resolved_provider": self.resolved_provider,
            "required_closure_stages": list(self.required_closure_stages),
            "degrade_mode": self.degrade_mode,
            "publication_gate_mode": self.publication_gate_mode,
            "session_context_hash": self.session_context_hash,
            "no_tool_justification": self.no_tool_justification,
        }


class _RolePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    public_allowlist: list[str] = Field(default_factory=list)
    spawn_allowlist: list[str] = Field(default_factory=list)
    local_allowlist: list[str] = Field(default_factory=list)


_DEFAULT_PUBLIC_TOOL_ALLOWLIST = _sorted_unique(registry_items().keys())
_SCIENTIFIC_CLOSURE_TOOLS = ["consult_bibliotecario", "request_peer_review", "generate_vertical_report"]

_ROLE_POLICIES: dict[str, _RolePolicy] = {
    "driver": _RolePolicy(
        public_allowlist=_DEFAULT_PUBLIC_TOOL_ALLOWLIST,
        spawn_allowlist=_SCIENTIFIC_CLOSURE_TOOLS,
        local_allowlist=[],
    ),
    "default": _RolePolicy(
        public_allowlist=_DEFAULT_PUBLIC_TOOL_ALLOWLIST,
        spawn_allowlist=[],
        local_allowlist=[],
    ),
    "bibliotecario": _RolePolicy(
        public_allowlist=[
            "search_literature",
            "search_protein",
            "search_protein_metadata",
            "advanced_protein_search",
            "get_citations_and_references",
            "verify_citations",
        ],
        spawn_allowlist=[],
        local_allowlist=["cite_finding", "identify_gap"],
    ),
    "reviewer": _RolePolicy(
        public_allowlist=[
            "search_literature",
            "search_protein",
            "verify_citations",
            "get_citations_and_references",
        ],
        spawn_allowlist=[],
        local_allowlist=["flag_issue", "cite_finding"],
    ),
    "msrp_reviewer": _RolePolicy(
        public_allowlist=[
            "search_literature",
            "search_protein",
            "verify_citations",
            "get_citations_and_references",
        ],
        spawn_allowlist=[],
        local_allowlist=["flag_issue", "cite_finding"],
    ),
    "expert": _RolePolicy(
        public_allowlist=[
            "search_literature",
            "search_protein",
            "search_protein_metadata",
            "advanced_protein_search",
            "verify_citations",
        ],
        spawn_allowlist=[],
        local_allowlist=["cite_finding", "identify_gap"],
    ),
}


class RuntimeAuthorityResolver:
    """Resolve one strict capability bundle per lane, role, and session context.

    The output contract is Pydantic-first so direct, REST, and WS paths can
    consume one machine-readable bundle instead of building local dict truth.
    """

    def __init__(self, tool_registry: Mapping[str, Any] | None = None) -> None:
        self._tool_registry = dict(tool_registry or registry_items())

    def _resolve_role_policy(self, role_id: str) -> _RolePolicy:
        normalized = str(role_id or "default").strip().lower()
        if normalized in _ROLE_POLICIES:
            return _ROLE_POLICIES[normalized]
        if normalized.endswith("_reviewer"):
            return _ROLE_POLICIES["msrp_reviewer"]
        if normalized in {"biophysics_idp", "structural_biology", "pharmacology", "bioinformatics"}:
            return _ROLE_POLICIES["expert"]
        return _ROLE_POLICIES["default"]

    def _route_public_allowlist(self, route_class: str, session_ctx: SessionContextEnvelope) -> list[str]:
        requested = _dedupe_strs(session_ctx.selected_tool_names or session_ctx.visible_tool_names)
        if route_class == "scientific_audit":
            return _dedupe_strs(requested + session_ctx.required_tool_names)
        return requested

    def _build_capability_bindings(
        self,
        *,
        tool_names: Sequence[str],
        role_id: str,
        allowed_providers: Sequence[str],
    ) -> list[CapabilityBinding]:
        bindings: list[CapabilityBinding] = []
        for tool_name in _dedupe_strs(tool_names):
            if tool_name not in self._tool_registry:
                continue
            spec = get_tool_capability(tool_name)
            capability_id = str(spec.protocol_tags[0] if spec.protocol_tags else tool_name)
            binding = CapabilityBinding(
                capability_id=capability_id,
                tool_ids=[tool_name],
                allowed_roles=[role_id],
                required_providers=_dedupe_strs(allowed_providers if spec.requires_provider else ()),
                failure_mode="fail_closed" if str(spec.route_authority or "").startswith("required") else "best_effort",
                cost_class="sandbox" if spec.requires_sandbox else ("network" if spec.required_external_hosts else "standard"),
                side_effect_class="write" if spec.surface == "spawn" else "read",
                route_authority=str(spec.route_authority or "optional"),
                closure_stage=str(spec.closure_stage or ""),
            )
            bindings.append(binding)
        return bindings

    def resolve(
        self,
        *,
        user_id: str,
        route_class: str,
        role_id: str,
        session_context: Mapping[str, Any] | None,
    ) -> CapabilityBundle:
        session_ctx = SessionContextEnvelope.from_mapping(session_context)
        effective_route_class = str(route_class or session_ctx.route_class or "general").strip() or "general"
        role_policy = self._resolve_role_policy(role_id)

        route_allowed_public = self._route_public_allowlist(effective_route_class, session_ctx)
        allowed_public_tools = [
            tool_name
            for tool_name in route_allowed_public
            if tool_name in set(role_policy.public_allowlist)
        ]

        allowed_spawn_tools = [
            tool_name
            for tool_name in _dedupe_strs(session_ctx.internal_spawn_tools)
            if tool_name in set(role_policy.spawn_allowlist)
        ]
        if effective_route_class == "scientific_audit" and role_id in {"default", "driver", "primary"}:
            allowed_spawn_tools = _dedupe_strs(list(allowed_spawn_tools) + _SCIENTIFIC_CLOSURE_TOOLS)

        allowed_role_local_tools = [
            tool_name
            for tool_name in _dedupe_strs(session_ctx.role_local_tools)
            if tool_name in set(role_policy.local_allowlist)
        ]

        missing_required_tools = [
            tool_name
            for tool_name in session_ctx.required_tool_names
            if tool_name not in allowed_public_tools and tool_name not in allowed_spawn_tools
        ]

        blocked_capabilities = list(session_ctx.blocked_capabilities)
        for tool_name in missing_required_tools:
            blocked_capabilities.append(
                BlockedCapability(
                    mechanism="RUNTIME_AUTHORITY_MISSING_REQUIRED_TOOLS",
                    detail=(
                        "Route authority requires this tool before embodiment, "
                        "but the resolved role-scoped capability bundle excludes it."
                    ),
                    tool_name=tool_name,
                    capability_id=tool_name,
                )
            )
        hard_block_reasons = _dedupe_strs(item.detail for item in blocked_capabilities)

        allowed_tools = _dedupe_strs(allowed_public_tools + allowed_spawn_tools + allowed_role_local_tools)
        allowed_providers = _dedupe_strs(session_ctx.configured_providers or ([session_ctx.provider_id] if session_ctx.provider_id else ()))
        authority = "mandatory" if effective_route_class == "scientific_audit" or session_ctx.authority == "mandatory" else session_ctx.authority
        degrade_mode = "fail_closed" if authority == "mandatory" else "best_effort"
        publication_gate_mode = "required" if authority == "mandatory" else "best_effort"

        context_hash = _context_hash(
            {
                "route_card_id": session_ctx.route_card_id,
                "route_class": effective_route_class,
                "role_id": role_id,
                "allowed_public_tools": allowed_public_tools,
                "allowed_spawn_tools": allowed_spawn_tools,
                "required_tool_names": session_ctx.required_tool_names,
                "required_capabilities": session_ctx.required_capabilities,
                "required_closure_stages": session_ctx.required_closure_stages,
                "active_skill_ids": session_ctx.active_skill_ids,
                "requested_skill_ids": session_ctx.requested_skill_ids,
                "provider_id": session_ctx.provider_id or "",
                "configured_providers": allowed_providers,
            }
        )

        return CapabilityBundle(
            route_card_id=session_ctx.route_card_id,
            route_class=effective_route_class,
            role_id=str(role_id or "default").strip() or "default",
            user_id=str(user_id or "").strip(),
            authority=authority,
            allowed_public_tools=allowed_public_tools,
            allowed_spawn_tools=allowed_spawn_tools,
            allowed_role_local_tools=allowed_role_local_tools,
            allowed_tools=allowed_tools,
            required_tool_names=list(session_ctx.required_tool_names),
            missing_required_tools=missing_required_tools,
            required_capabilities=list(session_ctx.required_capabilities),
            active_skill_ids=list(session_ctx.active_skill_ids),
            requested_skill_ids=list(session_ctx.requested_skill_ids),
            capability_bindings=self._build_capability_bindings(
                tool_names=allowed_public_tools + allowed_spawn_tools,
                role_id=str(role_id or "default").strip() or "default",
                allowed_providers=allowed_providers,
            ),
            blocked_capabilities=blocked_capabilities,
            hard_block_reasons=hard_block_reasons,
            allowed_providers=allowed_providers,
            resolved_provider=session_ctx.provider_id,
            required_closure_stages=list(session_ctx.required_closure_stages),
            degrade_mode=degrade_mode,
            publication_gate_mode=publication_gate_mode,
            session_context_hash=context_hash,
            no_tool_justification=session_ctx.no_tool_justification,
        )


def authorize_tool_invocation(bundle: Mapping[str, Any] | CapabilityBundle | None, tool_name: str) -> dict[str, Any]:
    normalized_tool_name = str(tool_name or "").strip()
    if isinstance(bundle, CapabilityBundle):
        authority = bundle.to_runtime_authority_dict()
    else:
        authority = dict(bundle or {})
    invocable_tools = _dedupe_strs(authority.get("allowed_tools") or authority.get("invocable_tools") or ())
    if not invocable_tools:
        invocable_tools = _dedupe_strs(
            list(authority.get("visible_tools") or ())
            + list(authority.get("internal_spawn_tools") or ())
            + list(authority.get("allowed_role_local_tools") or ())
        )
    blocked_capability_items = [
        item
        for item in list(authority.get("blocked_capabilities") or ())
        if isinstance(item, Mapping)
    ]
    hard_block_reasons = _dedupe_strs(
        list(authority.get("hard_block_reasons") or ())
        + [str(item.get("detail") or "").strip() for item in blocked_capability_items]
    )
    allowed = normalized_tool_name in invocable_tools if normalized_tool_name else False
    reason = "AUTHORIZED" if allowed else "TOOL_NOT_AUTHORIZED_FOR_ROUTE"
    if hard_block_reasons:
        allowed = False
        reason = "ROUTE_HARD_BLOCKED"
    return {
        "allowed": allowed,
        "tool_name": normalized_tool_name,
        "route_card_id": str(authority.get("route_card_id") or ""),
        "lane_class": str(authority.get("lane_class") or authority.get("route_class") or "general"),
        "authority": str(authority.get("authority") or "advisory"),
        "degrade_mode": str(authority.get("degrade_mode") or "best_effort"),
        "publication_gate_mode": str(authority.get("publication_gate_mode") or "best_effort"),
        "required_tool_names": _dedupe_strs(authority.get("required_tool_names") or ()),
        "missing_required_tools": _dedupe_strs(authority.get("missing_required_tools") or ()),
        "required_capabilities": _dedupe_strs(authority.get("required_capabilities") or ()),
        "required_closure_stages": _dedupe_strs(authority.get("required_closure_stages") or ()),
        "invocable_tools": invocable_tools,
        "hard_block_reasons": hard_block_reasons,
        "reason": reason,
    }
