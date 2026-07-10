"""Dependency state helpers extracted from AgenticDriver execution loop."""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence


def provider_dependency_state(
    *,
    required: bool,
    configured_providers: Sequence[str],
    provider_error: Optional[str],
) -> Dict[str, Any]:
    return {
        "required": required,
        "satisfied": bool(configured_providers),
        "configured": list(configured_providers),
        "detail": provider_error,
    }


def backend_dependency_state(
    *,
    tool_name: str,
    args: Dict[str, Any],
    required_backend_workers: Sequence[str],
    mcp_enabled: bool,
    mcp_available: bool,
    specialist_drivers: Optional[Dict[str, Any]],
    specialist_pool_available: bool,
) -> Dict[str, Any]:
    workers = list(required_backend_workers)
    if not workers:
        return {
            "required": False,
            "satisfied": True,
            "required_workers": [],
            "available_workers": [],
            "unavailable_workers": [],
            "detail": "no backend dependency declared",
        }

    available_workers: List[str] = []
    unavailable_workers: List[str] = []
    detail_parts: List[str] = []
    for worker in workers:
        worker_available = False
        if tool_name == "consult_specialist":
            specialist = str(args.get("specialist") or "").strip()
            host_available = bool(
                specialist
                and specialist_drivers
                and specialist in specialist_drivers
            )
            worker_available = host_available or specialist_pool_available or bool(mcp_enabled and mcp_available)
            detail_parts.append(
                f"consult_specialist(host={host_available}, modal_pool={specialist_pool_available}, mcp={bool(mcp_enabled and mcp_available)})"
            )
        else:
            worker_available = bool(mcp_enabled and mcp_available)
            if not worker_available:
                detail_parts.append(
                    f"{worker}: backend transport disabled (mcp_enabled={mcp_enabled}, mcp_available={mcp_available})"
                )
        if worker_available:
            available_workers.append(worker)
        else:
            unavailable_workers.append(worker)

    return {
        "required": True,
        "satisfied": not unavailable_workers,
        "required_workers": workers,
        "available_workers": available_workers,
        "unavailable_workers": unavailable_workers,
        "detail": "; ".join(detail_parts) if detail_parts else None,
    }


def sandbox_dependency_state(*, requires_sandbox: bool) -> Dict[str, Any]:
    if not requires_sandbox:
        return {
            "required": False,
            "satisfied": True,
            "detail": "no sandbox dependency declared",
        }
    try:
        import modal  # noqa: F401

        return {
            "required": True,
            "satisfied": True,
            "detail": "modal_import_ok",
        }
    except Exception as exc:
        return {
            "required": True,
            "satisfied": False,
            "detail": str(exc),
        }


async def probe_host_reachability(
    *,
    host: str,
    dependency_probe_cache: Dict[str, Dict[str, Any]],
    dependency_probe_ttl_s: float,
) -> Dict[str, Any]:
    cache_key = f"dns::{host}"
    now = time.monotonic()
    cached = dependency_probe_cache.get(cache_key)
    if cached and now - float(cached.get("checked_at_monotonic") or 0.0) < dependency_probe_ttl_s:
        return dict(cached)

    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=False),
            timeout=2.0,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        result = {
            "host": host,
            "reachable": True,
            "detail": "tcp_connect_ok",
            "checked_at_monotonic": now,
        }
    except Exception as exc:
        result = {
            "host": host,
            "reachable": False,
            "detail": str(exc),
            "checked_at_monotonic": now,
        }
    dependency_probe_cache[cache_key] = dict(result)
    return result


async def network_dependency_state(
    *,
    required_external_hosts: Sequence[str],
    min_available_hosts: int,
    dependency_probe_cache: Dict[str, Dict[str, Any]],
    dependency_probe_ttl_s: float,
) -> Dict[str, Any]:
    hosts = list(required_external_hosts)
    if not hosts:
        return {
            "required": False,
            "satisfied": True,
            "required_hosts": [],
            "reachable_hosts": [],
            "unreachable_hosts": [],
            "min_available_hosts": 0,
            "detail": "no network dependency declared",
        }

    effective_min = int(min_available_hosts or len(hosts))
    probes = await asyncio.gather(
        *(
            probe_host_reachability(
                host=host,
                dependency_probe_cache=dependency_probe_cache,
                dependency_probe_ttl_s=dependency_probe_ttl_s,
            )
            for host in hosts
        )
    )
    reachable_hosts = [probe["host"] for probe in probes if probe.get("reachable")]
    unreachable_hosts = [
        {"host": probe["host"], "detail": probe.get("detail")}
        for probe in probes
        if not probe.get("reachable")
    ]
    return {
        "required": True,
        "satisfied": len(reachable_hosts) >= effective_min,
        "required_hosts": hosts,
        "reachable_hosts": reachable_hosts,
        "unreachable_hosts": unreachable_hosts,
        "min_available_hosts": effective_min,
        "detail": None if len(reachable_hosts) >= effective_min else "insufficient reachable upstream hosts",
    }


async def dependency_state_for_tool(
    tool_name: str,
    args: Dict[str, Any],
    *,
    get_tool_capability_fn: Callable[[str], Any],
    backend_dependency_state_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    network_dependency_state_fn: Callable[[str], Awaitable[Dict[str, Any]]],
    sandbox_dependency_state_fn: Callable[[str], Dict[str, Any]],
    provider_dependency_state_fn: Callable[[bool], Dict[str, Any]],
) -> Dict[str, Any]:
    spec = get_tool_capability_fn(tool_name)
    dependency_state = {
        "backend": backend_dependency_state_fn(tool_name, args),
        "network": await network_dependency_state_fn(tool_name),
        "sandbox": sandbox_dependency_state_fn(tool_name),
        "provider": provider_dependency_state_fn(bool(spec.requires_provider)),
    }
    dependency_state["ready"] = (
        (not dependency_state["backend"].get("required") or dependency_state["backend"].get("satisfied"))
        and (not dependency_state["network"].get("required") or dependency_state["network"].get("satisfied"))
        and (not dependency_state["sandbox"].get("required") or dependency_state["sandbox"].get("satisfied"))
        and (not dependency_state["provider"].get("required") or dependency_state["provider"].get("satisfied"))
    )
    return dependency_state


async def pre_dispatch_gate(
    tool_name: str,
    args: Dict[str, Any],
    *,
    get_tool_capability_fn: Callable[[str], Any],
    dependency_state_for_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
    last_bibliotecario_state: Optional[Dict[str, Any]],
    unavailable_tool_response_fn: Callable[..., str],
    degraded_tool_response_fn: Callable[..., str],
) -> Optional[str]:
    spec = get_tool_capability_fn(tool_name)
    dependency_state = await dependency_state_for_tool_fn(tool_name, args)

    if tool_name == "generate_vertical_report" and not (
        (last_bibliotecario_state or {}).get("synthesis")
        or (last_bibliotecario_state or {}).get("artifact_bundle")
    ):
        return unavailable_tool_response_fn(
            tool_name,
            "This tool requires a prior bibliotecario synthesis in the active session before a report can be generated.",
            args_payload=args,
            failure_reason="INVALID_INPUT",
            dependency_state=dependency_state,
        )

    if dependency_state["provider"].get("required") and not dependency_state["provider"].get("satisfied"):
        return unavailable_tool_response_fn(
            tool_name,
            "This tool requires a configured LLM provider and none is currently available.",
            args_payload=args,
            failure_reason="INTERNAL_ERROR",
            dependency_state=dependency_state,
            extra={"detail": dependency_state["provider"].get("detail")},
        )

    if dependency_state["backend"].get("required") and not dependency_state["backend"].get("satisfied") and spec.placeholder_policy == "synthetic_only":
        return degraded_tool_response_fn(
            tool_name,
            "This tool is currently available only as a synthetic placeholder because the specialized backend path is unavailable.",
            args_payload=args,
            failure_reason="PLACEHOLDER_ONLY",
            is_synthetic=True,
            dependency_state=dependency_state,
            extra={"detail": dependency_state["backend"].get("detail")},
        )

    if dependency_state["backend"].get("required") and not dependency_state["backend"].get("satisfied") and spec.placeholder_policy != "synthetic_only":
        return unavailable_tool_response_fn(
            tool_name,
            "This tool requires backend services that are not available in the active runtime.",
            args_payload=args,
            failure_reason="BACKEND_UNAVAILABLE",
            dependency_state=dependency_state,
            extra={"detail": dependency_state["backend"].get("detail")},
        )

    if dependency_state["network"].get("required") and not dependency_state["network"].get("satisfied"):
        return unavailable_tool_response_fn(
            tool_name,
            "This tool requires upstream network services that are unreachable from the active runtime.",
            args_payload=args,
            failure_reason="NETWORK_UNAVAILABLE",
            dependency_state=dependency_state,
            extra={"detail": dependency_state["network"].get("detail")},
        )

    if dependency_state["sandbox"].get("required") and not dependency_state["sandbox"].get("satisfied"):
        return unavailable_tool_response_fn(
            tool_name,
            "This tool requires the sandbox runtime and that dependency is unavailable.",
            args_payload=args,
            failure_reason="SANDBOX_UNAVAILABLE",
            dependency_state=dependency_state,
            extra={"detail": dependency_state["sandbox"].get("detail")},
        )

    return None