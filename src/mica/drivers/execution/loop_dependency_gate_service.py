"""Bundle dependency gate wiring for AgenticDriver loop execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional


@dataclass(frozen=True)
class LoopDependencyGates:
    dependency_state_for_tool: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]
    pre_dispatch_gate: Callable[[str, Dict[str, Any]], Awaitable[Optional[str]]]


def build_loop_dependency_gates(
    *,
    configured_provider_ids_fn: Callable[[], tuple[list[str], str | None]],
    get_tool_capability_fn: Callable[[str], Any],
    provider_dependency_state_service_fn: Callable[..., Dict[str, Any]],
    backend_dependency_state_service_fn: Callable[..., Dict[str, Any]],
    network_dependency_state_service_fn: Callable[..., Awaitable[Dict[str, Any]]],
    sandbox_dependency_state_service_fn: Callable[..., Dict[str, Any]],
    dependency_state_for_tool_service_fn: Callable[..., Awaitable[Dict[str, Any]]],
    pre_dispatch_gate_service_fn: Callable[..., Awaitable[Optional[str]]],
    mcp_enabled: bool,
    mcp_available: bool,
    specialist_drivers: Any,
    specialist_pool_available: bool,
    last_bibliotecario_state: Dict[str, Any],
    unavailable_tool_response_fn: Callable[..., str],
    degraded_tool_response_fn: Callable[..., str],
) -> LoopDependencyGates:
    dependency_probe_cache: Dict[str, Dict[str, Any]] = {}
    dependency_probe_ttl_s = 120.0

    def _provider_dependency_state(required: bool) -> Dict[str, Any]:
        configured_providers, provider_error = configured_provider_ids_fn()
        return provider_dependency_state_service_fn(
            required=required,
            configured_providers=configured_providers,
            provider_error=provider_error,
        )

    def _backend_dependency_state(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        spec = get_tool_capability_fn(tool_name)
        return backend_dependency_state_service_fn(
            tool_name=tool_name,
            args=args,
            required_backend_workers=spec.required_backend_workers,
            mcp_enabled=mcp_enabled,
            mcp_available=mcp_available,
            specialist_drivers=specialist_drivers,
            specialist_pool_available=specialist_pool_available,
        )

    async def _network_dependency_state(tool_name: str) -> Dict[str, Any]:
        spec = get_tool_capability_fn(tool_name)
        required_external_hosts = list(getattr(spec, "required_external_hosts", ()) or ())
        return await network_dependency_state_service_fn(
            required_external_hosts=required_external_hosts,
            min_available_hosts=int(getattr(spec, "min_available_hosts", 0) or len(required_external_hosts)),
            dependency_probe_cache=dependency_probe_cache,
            dependency_probe_ttl_s=dependency_probe_ttl_s,
        )

    def _sandbox_dependency_state(tool_name: str) -> Dict[str, Any]:
        spec = get_tool_capability_fn(tool_name)
        return sandbox_dependency_state_service_fn(requires_sandbox=bool(getattr(spec, "requires_sandbox", False)))

    async def _dependency_state_for_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return await dependency_state_for_tool_service_fn(
            tool_name,
            args,
            get_tool_capability_fn=get_tool_capability_fn,
            backend_dependency_state_fn=_backend_dependency_state,
            network_dependency_state_fn=_network_dependency_state,
            sandbox_dependency_state_fn=_sandbox_dependency_state,
            provider_dependency_state_fn=_provider_dependency_state,
        )

    async def _pre_dispatch_gate(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        return await pre_dispatch_gate_service_fn(
            tool_name,
            args,
            get_tool_capability_fn=get_tool_capability_fn,
            dependency_state_for_tool_fn=_dependency_state_for_tool,
            last_bibliotecario_state=last_bibliotecario_state,
            unavailable_tool_response_fn=unavailable_tool_response_fn,
            degraded_tool_response_fn=degraded_tool_response_fn,
        )

    return LoopDependencyGates(
        dependency_state_for_tool=_dependency_state_for_tool,
        pre_dispatch_gate=_pre_dispatch_gate,
    )