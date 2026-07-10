"""Typed specialist binding for protocol nodes that target WorkerDriver surfaces."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from mica_q.protocol_jsonld_contract import ProtocolNode


PROTOCOL_SPECIALIST_EXECUTOR_SURFACES = frozenset({"worker_driver"})


def protocol_node_uses_specialist_binding(node: ProtocolNode) -> bool:
    return node.executor_surface.strip() in PROTOCOL_SPECIALIST_EXECUTOR_SURFACES


def _resolve_specialist_id(node: ProtocolNode, driver: Any) -> str:
    candidate = node.inputs.get("specialist_id")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()

    specialists = getattr(driver, "specialists", None)
    if isinstance(specialists, dict) and len(specialists) == 1:
        return str(next(iter(specialists.keys())))

    raise ValueError(
        f"Protocol node {node.node_id} requires inputs.specialist_id for worker driver '{node.executor_id}'"
    )


def _resolve_query(node: ProtocolNode) -> str:
    for key in ("query", "prompt"):
        candidate = node.inputs.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return node.objective


def _resolve_thermodynamic_context(node: ProtocolNode) -> Optional[dict[str, Any]]:
    candidate = node.inputs.get("thermodynamic_context")
    if isinstance(candidate, dict):
        return dict(candidate)
    return None


def _resolve_protocol_lineage(protocol_id: str, node: ProtocolNode, kernel_context: Any) -> dict[str, Any]:
    event_store = getattr(kernel_context, "event_store", None)
    event_store_path = str(getattr(event_store, "storage_path", "")).strip()
    return {
        "protocol_id": protocol_id,
        "node_id": node.node_id,
        "executor_surface": node.executor_surface,
        "executor_id": node.executor_id,
        "event_store_path": event_store_path,
    }


async def dispatch_protocol_node_to_worker_driver(
    *,
    protocol_id: str,
    node: ProtocolNode,
    specialist_drivers: Mapping[str, Any],
) -> Any | None:
    if not protocol_node_uses_specialist_binding(node):
        return None

    if node.policies.protected_surface:
        raise ValueError(
            f"Protocol node {node.node_id} cannot bind protected_surface through worker_driver"
        )

    worker_name = node.executor_id.strip()
    driver = specialist_drivers.get(worker_name)
    if driver is None:
        raise ValueError(
            f"Protocol node {node.node_id} references unknown worker driver '{worker_name}'"
        )

    kernel_context = getattr(driver, "protocol_runtime_kernel_context", None)
    event_store = getattr(kernel_context, "event_store", None) if kernel_context is not None else None
    if kernel_context is None or event_store is None:
        raise ValueError(
            f"Protocol node {node.node_id} requires MO-00 shared kernel event_store before worker binding"
        )

    specialist_id = _resolve_specialist_id(node, driver)
    protocol_lineage = _resolve_protocol_lineage(protocol_id, node, kernel_context)
    enforce_msrp = node.inputs.get("enforce_msrp")
    if not isinstance(enforce_msrp, bool):
        enforce_msrp = True

    result = await driver.route_to_specialist(
        query=_resolve_query(node),
        specialist_id=specialist_id,
        enforce_msrp=enforce_msrp,
        thermodynamic_context=_resolve_thermodynamic_context(node),
        protocol_node=node,
        protocol_lineage=protocol_lineage,
    )

    from .protocol_executor import ProtocolNodeDispatchResult

    answer = result.get("answer") if isinstance(result, dict) else None
    answer_preview = str(answer or "").strip()
    summary = (
        f"Executed protocol node {node.node_id} via worker_driver "
        f"{worker_name}/{specialist_id}."
    )
    return ProtocolNodeDispatchResult(
        summary=summary,
        state_after={
            "worker_driver": worker_name,
            "specialist_id": specialist_id,
            "specialist_status": str(result.get("status", "SUCCESS")) if isinstance(result, dict) else "SUCCESS",
            "protocol_lineage": protocol_lineage,
            "answer_preview": answer_preview[:200],
        },
        evidence_refs=[
            f"protocol://{protocol_id}/nodes/{node.node_id}/specialists/{worker_name}/{specialist_id}"
        ],
        cost_snapshot={
            "usd": float(result.get("cost_usd", 0.0)) if isinstance(result, dict) else 0.0,
            "tool_calls": 1,
            "binding_surface": node.executor_surface,
        },
    )