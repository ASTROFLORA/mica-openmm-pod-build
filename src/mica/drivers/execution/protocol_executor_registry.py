from __future__ import annotations

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping, Callable, Awaitable, List, Tuple, Optional

from mica.agentic.backend_command_manifest import (
    BackendCommandManifestEntry,
    get_backend_command_manifest_entry,
    iter_manifest_entries,
)
from mica.agentic.command_kernel import UnifiedAgentCommandKernel
from mica.api_v1.routers.bibliotecario import (
    execute_protocol_mica_q_action,
    protocol_node_uses_mica_q_multisurface,
)
from mica.api_v1.routers.compute import (
    execute_protocol_compute_md_action,
    protocol_node_uses_compute_md_surface,
    resolve_protocol_compute_tool_name,
)
from mica.api_v1.routers.smic import execute_protocol_smic_action, protocol_node_uses_smic_surface
from mica.api_v1.routers.user_bucket import (
    execute_protocol_workspace_action,
    protocol_node_uses_workspace_surface,
)
from mica.drivers.execution.protocol_sandbox_node_adapter import (
    execute_protocol_sandbox_action,
    protocol_node_uses_sandbox_surface,
)
from mica.sdk.command_contracts import (
    BackendCommandEnvelope,
    BackendCommandPolicy,
    BackendCommandResult,
)
from mica.tools_authority.tool_alias_registry import canonical_tool_name_for_command
from mica.pipelines.knowledge_fabric.paper_kb_bundle_mvp import _json_safe, run_seed_paper_bundle_mvp, SeedPaperMVPRequest
from mica_q.protocol_jsonld_contract import ProtocolExecutorRequest, ProtocolNode

if TYPE_CHECKING:
    from .protocol_executor import ProtocolNodeDispatchResult


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for item in value:
        cleaned = str(item or "").strip()
        if cleaned:
            items.append(cleaned)
    return items


def _cost_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    base = dict(payload.get("cost_snapshot") or {})
    if "tool_name" not in base:
        base["tool_name"] = str(payload.get("tool_name") or "").strip()
    if "binding_surface" not in base:
        base["binding_surface"] = str(payload.get("binding_surface") or "").strip()
    if "route_decision_id" not in base and payload.get("route_decision_id"):
        base["route_decision_id"] = str(payload.get("route_decision_id"))
    if "job_id" not in base and payload.get("job_id"):
        base["job_id"] = str(payload.get("job_id"))
    return base


def _command_surface_manifest_by_tool_name() -> dict[str, BackendCommandManifestEntry]:
    return {
        canonical_tool_name_for_command(entry.command_name): entry
        for entry in iter_manifest_entries()
    }


def _resolve_protocol_backend_command_entry(node: ProtocolNode) -> BackendCommandManifestEntry | None:
    inputs = dict(node.inputs or {})
    candidates: list[str] = []
    for key in ("command_name", "tool_name", "action", "tool", "operation"):
        candidate = inputs.get(key)
        if isinstance(candidate, str) and candidate.strip():
            candidates.append(candidate.strip())

    canonical_tool_lookup = _command_surface_manifest_by_tool_name()
    for candidate in candidates:
        if candidate in canonical_tool_lookup:
            return canonical_tool_lookup[candidate]
        try:
            return get_backend_command_manifest_entry(candidate)
        except KeyError:
            continue
    return None


def _protocol_node_uses_backend_command_surface(node: ProtocolNode) -> bool:
    surface = str(node.executor_surface or "").strip().lower()
    if surface not in {"backend_api", "command_kernel", "resource_fabric"}:
        return False
    return _resolve_protocol_backend_command_entry(node) is not None


def _protocol_backend_command_arguments(node: ProtocolNode) -> dict[str, Any]:
    inputs = dict(node.inputs or {})
    reserved = {
        "command_name",
        "binding_surface",
        "backend_authority",
        "required_scope",
        "tool_aliases",
        "step_metadata",
    }
    return {key: value for key, value in inputs.items() if key not in reserved}


def _protocol_backend_command_status(result: BackendCommandResult) -> tuple[str, str, Optional[str]]:
    blocker_code = str(
        result.blocker_code
        or (result.blockers[0].code if result.blockers else "")
        or ""
    ).strip() or None
    normalized_status = str(result.status or "").strip().lower()
    if result.success:
        return ("completed", "node.completed", blocker_code)
    if normalized_status in {"blocked", "rejected", "unavailable"} or result.blockers:
        return ("blocked", "node.blocked", blocker_code)
    return ("failed", "node.failed", blocker_code or "backend_command_failed")


def _backend_command_dispatch_result(
    *,
    result: BackendCommandResult,
    fallback_summary: str,
) -> "ProtocolNodeDispatchResult":
    from .protocol_executor import ProtocolNodeDispatchResult

    status, event_type, failure_code = _protocol_backend_command_status(result)
    state_after = {
        "backend_command": {
            "command_name": result.command_name,
            "binding_surface": result.binding_surface,
            "summary": result.summary,
            "result": dict(result.result or {}),
            "state_after": dict(result.state_after or {}),
            "receipt_refs": list(result.receipt_refs or []),
            "warnings": list(result.warnings or []),
            "degraded_reason": result.degraded_reason,
            "runtime_backing": result.runtime_backing,
            "durability": result.durability,
            "trust_state": result.trust_state,
        }
    }
    if failure_code:
        state_after["failure_code"] = failure_code

    evidence_refs = list(result.evidence_refs or [])
    for receipt_ref in list(result.receipt_refs or []):
        if receipt_ref not in evidence_refs:
            evidence_refs.append(receipt_ref)

    cost_snapshot = result.cost_snapshot.model_dump(mode="json")
    cost_snapshot.setdefault("tool_name", result.command_name)
    cost_snapshot.setdefault("binding_surface", result.binding_surface)

    return ProtocolNodeDispatchResult(
        summary=str(result.summary or fallback_summary),
        status=status,
        event_type=event_type,
        state_after=state_after,
        artifact_refs=list(result.artifact_refs or result.resource_refs or []),
        evidence_refs=evidence_refs,
        cost_snapshot=cost_snapshot,
        failure_code=failure_code,
    )


def _protocol_node_uses_serverless_model_surface(node: ProtocolNode) -> bool:
    surface = str(node.executor_surface or "").strip().lower()
    executor_id = str(node.executor_id or "").strip().lower()
    tool_name = str((node.inputs or {}).get("tool_name") or "").strip().lower()
    return surface in {"serverless_model", "serverless_models", "model", "models"} or executor_id in {
        "serverlessmodelgateway",
        "models.invoke",
    } or tool_name == "models.invoke"


def _safe_request_part(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip()).strip("-").lower()
    return normalized or "request"


def _node_model_payload(node: ProtocolNode) -> dict[str, Any]:
    inputs = dict(node.inputs or {})
    payload = inputs.get("payload_in")
    if payload is None:
        payload = inputs.get("inputs")
    if payload is None:
        payload = inputs.get("payload")
    return dict(payload or {}) if isinstance(payload, Mapping) else {}


def _node_model_metadata(
    *,
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
) -> dict[str, Any]:
    inputs = dict(node.inputs or {})
    request_metadata = dict(request.request_metadata or {})
    node_metadata = dict(inputs.get("metadata") or {}) if isinstance(inputs.get("metadata"), Mapping) else {}
    metadata = {**request_metadata, **node_metadata}
    metadata.setdefault("protocol_id", request.protocol_id)
    metadata.setdefault("protocol_ref", request.protocol_id)
    metadata.setdefault("session_id", request.session_id)
    metadata.setdefault("node_id", node.node_id)
    metadata.setdefault("owner_user_id", metadata.get("user_id") or user_id)
    input_refs = inputs.get("input_refs", metadata.get("input_refs", []))
    metadata["input_refs"] = _string_list(input_refs)
    return metadata


def _descriptor_revision_ref(descriptor: Any) -> str:
    metadata = dict(getattr(descriptor, "metadata", {}) or {})
    candidate = str(metadata.get("model_revision_ref") or metadata.get("revision_ref") or "").strip()
    if candidate:
        return candidate
    return f"serverless_model_descriptor://{descriptor.model_id}@builtin"


def _dispatch_result_from_payload(*, payload: Mapping[str, Any], fallback_summary: str) -> "ProtocolNodeDispatchResult":
    from .protocol_executor import ProtocolNodeDispatchResult

    status = str(payload.get("status") or "").strip().lower() or "completed"
    event_type = str(payload.get("event_type") or "").strip()
    if not event_type:
        event_type = "node.completed" if status == "completed" else "node.failed"
    failure_code_value = payload.get("failure_code")
    failure_code = str(failure_code_value).strip() if failure_code_value else None
    return ProtocolNodeDispatchResult(
        summary=str(payload.get("summary") or fallback_summary),
        status=status,
        event_type=event_type,
        state_after=dict(payload.get("state_after") or {}),
        artifact_refs=_string_list(payload.get("artifact_refs")),
        evidence_refs=_string_list(payload.get("evidence_refs")),
        cost_snapshot=_cost_snapshot(payload),
        approval_refs=_string_list(payload.get("approval_refs")),
        failure_code=failure_code,
    )


class ProtocolExecutorRegistry:
    def __init__(self) -> None:
        self._handlers: List[
            Tuple[
                Callable[[ProtocolNode], bool],
                Callable[
                    [ProtocolExecutorRequest, ProtocolNode, str, Optional[Mapping[str, Any]]],
                    Awaitable[Optional["ProtocolNodeDispatchResult"]],
                ],
            ]
        ] = []

    def register(
        self,
        matcher: Callable[[ProtocolNode], bool],
        handler: Callable[
            [ProtocolExecutorRequest, ProtocolNode, str, Optional[Mapping[str, Any]]],
            Awaitable[Optional["ProtocolNodeDispatchResult"]],
        ],
    ) -> None:
        self._handlers.append((matcher, handler))

    def can_dispatch(self, node: ProtocolNode) -> bool:
        for matcher, _handler in self._handlers:
            if matcher(node):
                return True
        return False

    async def dispatch(
        self,
        *,
        request: ProtocolExecutorRequest,
        node: ProtocolNode,
        user_id: str,
        specialist_drivers: Optional[Mapping[str, Any]] = None,
    ) -> Optional["ProtocolNodeDispatchResult"]:
        for matcher, handler in self._handlers:
            if matcher(node):
                result = await handler(request, node, user_id, specialist_drivers)
                if result is not None:
                    return result
        return None


async def _specialist_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    if not specialist_drivers:
        return None
    from .protocol_specialist_binding import dispatch_protocol_node_to_worker_driver
    return await dispatch_protocol_node_to_worker_driver(
        protocol_id=request.protocol_id,
        node=node,
        specialist_drivers=specialist_drivers,
    )


async def _compute_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    payload = await execute_protocol_compute_md_action(
        tool_name=resolve_protocol_compute_tool_name(node),
        inputs=node.inputs,
        protocol_id=request.protocol_id,
        node_id=node.node_id,
        session_id=request.session_id,
        user_id=user_id,
        approval_required=(
            node.policies.requires_human_approval
            or node.policies.protected_surface
            or node.policies.production_compute
        ),
    )
    return _dispatch_result_from_payload(
        payload=payload,
        fallback_summary=f"Executed protocol node {node.node_id}.",
    )


async def _smic_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    payload = await execute_protocol_smic_action(
        node=node,
        protocol_id=request.protocol_id,
        node_id=node.node_id,
        session_id=request.session_id,
        user_id=user_id,
    )
    return _dispatch_result_from_payload(
        payload=payload,
        fallback_summary=f"Executed SMIC protocol node {node.node_id}.",
    )


async def _workspace_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    payload = await execute_protocol_workspace_action(
        node=node,
        protocol_id=request.protocol_id,
        node_id=node.node_id,
        session_id=request.session_id,
        user_id=user_id,
        request_metadata=dict(request.request_metadata or {}),
    )
    return _dispatch_result_from_payload(
        payload=payload,
        fallback_summary=f"Executed workspace protocol node {node.node_id}.",
    )


async def _sandbox_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    payload = await execute_protocol_sandbox_action(
        request=request,
        node=node,
        user_id=user_id,
    )
    return _dispatch_result_from_payload(
        payload=payload,
        fallback_summary=f"Executed sandbox protocol node {node.node_id}.",
    )


async def _mica_q_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    payload = await execute_protocol_mica_q_action(
        request=request,
        node=node,
        user_id=user_id,
    )
    return _dispatch_result_from_payload(
        payload=payload,
        fallback_summary=f"Executed MICA_Q protocol node {node.node_id}.",
    )


def _protocol_node_uses_product_workflow_surface(node: ProtocolNode) -> bool:
    surface = str(node.executor_surface or "").strip().lower()
    executor_id = str(node.executor_id or "").strip().lower()
    return surface == "product_workflow" and executor_id in {"seed_paper_cascade", "seed_paper_bundle_mvp"}


async def _product_workflow_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    from .protocol_executor import ProtocolNodeDispatchResult

    executor_id = str(node.executor_id or "").strip().lower()
    if executor_id not in {"seed_paper_cascade", "seed_paper_bundle_mvp"}:
        return None

    workflow_request = SeedPaperMVPRequest.model_validate(dict(node.inputs or {}))
    result = await run_seed_paper_bundle_mvp(workflow_request)
    result_payload = _json_safe(result.model_dump())
    output_files = dict(result.output_files or {})
    canonical_persistence = dict(result.runtime.canonical_persistence or {})
    artifact_refs = [
        str(path).strip()
        for path in list(output_files.values()) + list(dict(canonical_persistence.get("artifacts") or {}).values())
        if str(path).strip()
    ]
    deduped_artifact_refs = list(dict.fromkeys(artifact_refs))
    evidence_refs = [f"protocol://{request.protocol_id}/nodes/{node.node_id}/product_workflow/seed_paper_cascade"]
    return ProtocolNodeDispatchResult(
        summary=(
            f"Executed product workflow {node.executor_id} "
            f"with status {result.runtime.run_status} and total_papers={result.runtime.total_papers}."
        ),
        status="completed",
        event_type="node.completed",
        state_after={
            "product_workflow": {
                "workflow_id": "seed_paper_cascade_v1",
                "workflow_aliases": ["seed_paper_bundle_mvp_v1"],
                "executor_id": "seed_paper_cascade",
                "observed_executor_id": node.executor_id,
                "run_status": result.runtime.run_status,
                "closure_status": result.runtime.closure_status,
                "runtime_mode": result.runtime.mode,
                "total_papers": result.runtime.total_papers,
                "canonical_persistence": canonical_persistence,
                "output_files": output_files,
                "result": result_payload,
            }
        },
        artifact_refs=deduped_artifact_refs,
        evidence_refs=evidence_refs,
        cost_snapshot={"usd": 0.0, "tool_calls": 1, "tool_name": str(node.executor_id or "product_workflow")},
    )


async def _backend_command_surface_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    manifest_entry = _resolve_protocol_backend_command_entry(node)
    if manifest_entry is None:
        return None

    arguments = _protocol_backend_command_arguments(node)
    kernel = UnifiedAgentCommandKernel(user_id=user_id)
    envelope = BackendCommandEnvelope(
        command_name=manifest_entry.command_name,
        session_id=request.session_id,
        study_id=(
            str(arguments.get("study_id") or "").strip()
            or str((request.request_metadata or {}).get("study_id") or "").strip()
            or None
        ),
        working_set_id=(
            str(arguments.get("working_set_id") or "").strip()
            or str((request.request_metadata or {}).get("working_set_id") or "").strip()
            or None
        ),
        workspace_id=(
            str(arguments.get("workspace_id") or "").strip()
            or str((request.request_metadata or {}).get("workspace_id") or "").strip()
            or None
        ),
        request_identity={
            "surface": "protocol_executor",
            "user_id": user_id,
            "protocol_id": request.protocol_id,
            "node_id": node.node_id,
        },
        arguments=arguments,
        resource_refs=list(arguments.get("resource_refs") or []),
        policy=BackendCommandPolicy(
            allow_side_effects=bool(arguments.get("allow_side_effects", manifest_entry.side_effects)),
        ),
    )
    result = await kernel.execute(envelope)
    return _backend_command_dispatch_result(
        result=result,
        fallback_summary=f"Executed backend command protocol node {node.node_id}.",
    )


async def _serverless_model_handler(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
    specialist_drivers: Optional[Mapping[str, Any]],
) -> Optional["ProtocolNodeDispatchResult"]:
    from .protocol_executor import ProtocolNodeDispatchResult
    from mica.quetzal.gates import InvokeContext, QuetzalGate
    from mica.serverless_models.contracts import ModelInvocationRequest, validate_inputs_against_schema
    from mica.serverless_models.factory import build_default_serverless_model_gateway

    inputs = dict(node.inputs or {})
    model_id = str(inputs.get("model_id") or inputs.get("model_ref") or "").strip()
    payload_in = _node_model_payload(node)
    budget_ceiling_usd = float(inputs.get("budget_ceiling_usd") or 1.0)
    request_id = f"{_safe_request_part(request.protocol_id)}-{_safe_request_part(node.node_id)}"

    gateway = None
    if specialist_drivers:
        gateway = specialist_drivers.get("serverless_model_gateway")
    if gateway is None:
        gateway = build_default_serverless_model_gateway()

    try:
        descriptor = gateway.get_descriptor(model_id)
    except Exception as exc:  # noqa: BLE001
        quetzal_receipt = {
            "gate_name": "quetzal.model_availability",
            "decision": "block",
            "reason_codes": ["model_descriptor_missing"],
            "provider_job_created": False,
        }
        return ProtocolNodeDispatchResult(
            summary=f"Protocol model node {node.node_id} blocked: unknown model {model_id!r}.",
            status="blocked",
            event_type="node.blocked",
            state_after={
                "model_invocation": {
                    "request_id": request_id,
                    "model_id": model_id,
                    "state": "blocked",
                    "error": str(exc),
                },
                "quetzal_decision_receipt": quetzal_receipt,
            },
            failure_code="model_descriptor_missing",
            cost_snapshot={"usd": 0.0, "tool_calls": 1, "tool_name": "models.invoke"},
        )

    try:
        validate_inputs_against_schema(descriptor, payload_in)
        input_valid = True
    except Exception:
        input_valid = False

    quetzal_verdict = QuetzalGate().evaluate(
        InvokeContext(
            model_ref=model_id,
            model_revision_ref=_descriptor_revision_ref(descriptor),
            workspace_id=str((request.request_metadata or {}).get("workspace_id") or ""),
            input_valid=input_valid,
            estimated_usd=float(inputs.get("estimated_usd") or 0.05),
            budget_ceiling_usd=budget_ceiling_usd,
            model_status=str(getattr(descriptor, "metadata", {}).get("status") or "registered"),
        )
    )
    quetzal_receipt = {
        "gate_name": quetzal_verdict.gate_name,
        "decision": quetzal_verdict.decision,
        "reason_codes": list(quetzal_verdict.reason_codes),
        "max_allowed_tier": quetzal_verdict.max_allowed_tier,
        "provider_job_created": quetzal_verdict.decision != "block",
    }
    if quetzal_verdict.decision == "block":
        return ProtocolNodeDispatchResult(
            summary=f"Protocol model node {node.node_id} blocked by Quetzal: {list(quetzal_verdict.reason_codes)}.",
            status="blocked",
            event_type="node.blocked",
            state_after={
                "model_invocation": {
                    "request_id": request_id,
                    "model_id": model_id,
                    "state": "blocked",
                },
                "quetzal_decision_receipt": quetzal_receipt,
            },
            failure_code=str(quetzal_verdict.reason_codes[0] if quetzal_verdict.reason_codes else "quetzal_blocked"),
            cost_snapshot={"usd": 0.0, "tool_calls": 1, "tool_name": "models.invoke"},
        )

    invocation_request = ModelInvocationRequest(
        request_id=request_id,
        model_id=model_id,
        user_id=user_id,
        session_id=request.session_id,
        run_id=request_id,
        inputs=payload_in,
        metadata=_node_model_metadata(request=request, node=node, user_id=user_id),
        requested_by="protocol_executor",
        provider_override=str(inputs.get("provider_override") or "").strip() or None,
    )
    result = await gateway.invoke(invocation_request)
    artifact_refs = list(result.artifact_uris or result.artifact_ids)
    evidence_refs = [f"serverless://execution/{request_id}"]
    mudo_receipt_ready = dict(result.metrics.get("mudo_receipt_ready") or {})

    state_after = {
        "model_invocation": {
            "request_id": result.request_id,
            "model_id": result.model_id,
            "state": result.state,
            "provider": result.provider,
            "provider_job_id": result.provider_job_id,
            "artifact_refs": artifact_refs,
            "normalized_output": dict(result.normalized_output or {}),
            "ui_payload": dict(result.ui_payload or {}),
            "metrics": dict(result.metrics or {}),
            "mudo_receipt_ready": mudo_receipt_ready,
            "error": result.error,
        },
        "quetzal_decision_receipt": quetzal_receipt,
    }
    if result.state != "completed":
        blocker_code = str(result.metrics.get("blocker_code") or "").strip()
        if blocker_code:
            state_after["failure_code"] = blocker_code
            return ProtocolNodeDispatchResult(
                summary=f"Protocol model node {node.node_id} blocked: {blocker_code}.",
                status="blocked",
                event_type="node.blocked",
                state_after=state_after,
                artifact_refs=artifact_refs,
                evidence_refs=evidence_refs,
                failure_code=blocker_code,
                cost_snapshot={"usd": 0.0, "tool_calls": 1, "tool_name": "models.invoke"},
            )
        return ProtocolNodeDispatchResult(
            summary=f"Protocol model node {node.node_id} failed: {result.error or result.state}.",
            status="failed",
            event_type="node.failed",
            state_after=state_after,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            failure_code=result.error or "model_invoke_failed",
            cost_snapshot={"usd": 0.05, "tool_calls": 1, "tool_name": "models.invoke"},
        )
    if request.ledger_policy.require_durable_lineage and mudo_receipt_ready.get("published") is not True:
        return ProtocolNodeDispatchResult(
            summary=(
                f"Protocol model node {node.node_id} completed provider inference but did not publish "
                f"MUDO receipt: {mudo_receipt_ready.get('reason') or 'unknown'}."
            ),
            status="failed",
            event_type="node.failed",
            state_after=state_after,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            failure_code=f"mudo_receipt_not_published:{mudo_receipt_ready.get('reason') or 'unknown'}",
            cost_snapshot={"usd": 0.05, "tool_calls": 1, "tool_name": "models.invoke"},
        )

    return ProtocolNodeDispatchResult(
        summary=f"Executed serverless model protocol node {node.node_id}.",
        status="completed",
        event_type="node.completed",
        state_after=state_after,
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        cost_snapshot={"usd": 0.05, "tool_calls": 1, "tool_name": "models.invoke"},
    )


global_registry = ProtocolExecutorRegistry()

from .protocol_specialist_binding import protocol_node_uses_specialist_binding
global_registry.register(lambda n: protocol_node_uses_specialist_binding(n), _specialist_handler)
global_registry.register(lambda n: protocol_node_uses_compute_md_surface(n), _compute_handler)
global_registry.register(lambda n: protocol_node_uses_smic_surface(n), _smic_handler)
global_registry.register(lambda n: protocol_node_uses_workspace_surface(n), _workspace_handler)
global_registry.register(lambda n: protocol_node_uses_sandbox_surface(n), _sandbox_handler)
global_registry.register(lambda n: protocol_node_uses_mica_q_multisurface(n), _mica_q_handler)
global_registry.register(lambda n: _protocol_node_uses_backend_command_surface(n), _backend_command_surface_handler)
global_registry.register(lambda n: _protocol_node_uses_serverless_model_surface(n), _serverless_model_handler)
global_registry.register(lambda n: _protocol_node_uses_product_workflow_surface(n), _product_workflow_handler)


async def dispatch_protocol_node_via_executor_registry(
    *,
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    user_id: str,
) -> ProtocolNodeDispatchResult | None:
    return await global_registry.dispatch(
        request=request,
        node=node,
        user_id=user_id,
    )


def protocol_node_has_executor_binding(node: ProtocolNode) -> bool:
    executor_surface = str(getattr(node, "executor_surface", "") or "").strip()
    executor_id = str(getattr(node, "executor_id", "") or "").strip()
    if executor_surface or executor_id:
        inputs = getattr(node, "inputs", None)
        stripped_inputs = dict(inputs or {}) if isinstance(inputs, Mapping) else {}
        for key in ("tool_name", "action", "tool", "operation"):
            stripped_inputs.pop(key, None)
        explicit_binding_probe = SimpleNamespace(
            executor_surface=executor_surface,
            executor_id=executor_id,
            inputs=stripped_inputs,
        )
        if global_registry.can_dispatch(explicit_binding_probe):
            return True
        return False
    return global_registry.can_dispatch(node)


__all__ = [
    "dispatch_protocol_node_via_executor_registry",
    "ProtocolExecutorRegistry",
    "global_registry",
    "protocol_node_has_executor_binding",
]
