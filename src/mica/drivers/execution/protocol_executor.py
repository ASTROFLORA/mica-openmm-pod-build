"""Protocol executor runtime and compatibility helpers for the Phase A typed handoff."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple
from uuid import uuid4

from bsm.communication.core import AgentPersona
from mica.agentic.protocol_cue_injector import ProtocolCueRuntimeManager
from mica.drivers.communication.runtime_communication_service import RuntimeCommunicationService
from mica.drivers.persistence.runtime_observability import (
    build_runtime_telemetry_emitter,
    emit_runtime_error,
    emit_runtime_status,
    persist_communication_store,
)
from mica_q.protocol_jsonld_contract import ProtocolExecutorRequest
from mica_q.protocol_jsonld_contract import ProtocolExecutionFrontier, ProtocolNode, ProtocolNodeReceipt, ProtocolRunReceipt


PROTOCOL_EXECUTOR_RUNTIME_TRANSPORT = "protocol_executor_runtime"
LEGACY_PROMPT_TRANSPORT = "agentic_prompt_queue"
LEGACY_PROMPT_RENDERER = "compile_protocol_draft_to_prompt"
_STATE_AFTER_TEMPLATE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\.state_after\.([A-Za-z0-9_.-]+)\}")


@dataclass(frozen=True)
class ProtocolNodeDispatchResult:
    summary: str
    status: str = "completed"
    event_type: str = "node.completed"
    state_after: Dict[str, Any] = field(default_factory=dict)
    artifact_refs: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    cost_snapshot: Dict[str, Any] = field(default_factory=dict)
    approval_refs: List[str] = field(default_factory=list)
    failure_code: Optional[str] = None


@dataclass(frozen=True)
class ProtocolExecutionOutcome:
    run_receipt: ProtocolRunReceipt
    node_receipts: List[ProtocolNodeReceipt] = field(default_factory=list)
    projection_message_ids: List[str] = field(default_factory=list)
    failure_message: Optional[str] = None


ProtocolNodeDispatchAdapter = Callable[[ProtocolNode], Awaitable[ProtocolNodeDispatchResult] | ProtocolNodeDispatchResult]


class PolicyEnforcementError(ValueError):
    def __init__(self, message: str, policy_receipt: Dict[str, Any]) -> None:
        super().__init__(message)
        self.policy_receipt = policy_receipt


def _protocol_bool(inputs: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        val = inputs.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            cleaned = val.strip().lower()
            if cleaned in {"true", "yes", "1", "on"}:
                return True
    return False


def build_protocol_dispatch_metadata(
    request: ProtocolExecutorRequest,
    *,
    legacy_fallback_enabled: bool = False,
) -> Dict[str, Any]:
    metadata = dict(request.request_metadata or {})
    metadata.update(
        {
            "execution_handoff": "protocol_executor_request",
            "execution_transport": PROTOCOL_EXECUTOR_RUNTIME_TRANSPORT,
            "protocol_executor_request": request.model_dump(mode="json"),
        }
    )
    if legacy_fallback_enabled:
        metadata.update(
            {
                "legacy_execution_transport": LEGACY_PROMPT_TRANSPORT,
                "legacy_fallback_renderer": LEGACY_PROMPT_RENDERER,
            }
        )
    return metadata


def build_protocol_runtime_communication_service() -> RuntimeCommunicationService:
    return RuntimeCommunicationService(
        persist_store_fn=persist_communication_store,
        build_runtime_telemetry_emitter_fn=build_runtime_telemetry_emitter,
        emit_runtime_status_fn=emit_runtime_status,
        emit_runtime_error_fn=emit_runtime_error,
        persona_system=AgentPersona.SYSTEM,
    )


def _resolve_tool_name(node: ProtocolNode) -> str:
    candidate = node.inputs.get("tool_name")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return f"{node.executor_surface}:{node.executor_id}"


def _default_artifact_refs(request: ProtocolExecutorRequest, node: ProtocolNode) -> List[str]:
    artifacts = node.expected_outputs.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        return [
            f"protocol://{request.protocol_id}/nodes/{node.node_id}/artifacts/{str(artifact).strip()}"
            for artifact in artifacts
            if str(artifact).strip()
        ]
    return [f"protocol://{request.protocol_id}/nodes/{node.node_id}/artifact/default"]


def _default_evidence_refs(request: ProtocolExecutorRequest, node: ProtocolNode) -> List[str]:
    if node.evidence_requirements:
        return [
            f"protocol://{request.protocol_id}/nodes/{node.node_id}/evidence/{requirement}"
            for requirement in node.evidence_requirements
        ]
    return [f"protocol://{request.protocol_id}/nodes/{node.node_id}/evidence/node_receipt"]


def _approval_refs(request: ProtocolExecutorRequest, node: ProtocolNode) -> List[str]:
    if node.policies.requires_human_approval or node.policies.protected_surface or node.policies.production_compute:
        return [f"approval://{approver}" for approver in request.approval_policy.required_approvers]
    return []


def _resolve_request_user_id(request: ProtocolExecutorRequest) -> str:
    metadata = dict(request.request_metadata or {})
    for key in ("user_id", "request_user_id", "owner_user_id"):
        candidate = metadata.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "protocol-operator"


def _tenant_scope_value(metadata: Mapping[str, Any], key: str) -> str:
    candidate = metadata.get(key)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return ""


def _evaluate_durable_tenant_scope(request: ProtocolExecutorRequest) -> Dict[str, Any]:
    metadata = dict(request.request_metadata or {})
    scope = {
        "owner_user_id": _tenant_scope_value(metadata, "owner_user_id")
        or _tenant_scope_value(metadata, "user_id"),
        "workspace_id": _tenant_scope_value(metadata, "workspace_id"),
        "study_id": _tenant_scope_value(metadata, "study_id"),
    }
    missing = [key for key, value in scope.items() if not value]
    decision = "rejected" if missing else "approved"
    return {
        "schema_id": "mica.protocol.tenant_scope_decision.v1",
        "protocol_id": request.protocol_id,
        "decision": decision,
        "reason": (
            f"Durable protocol execution missing required tenant scope: {', '.join(missing)}"
            if missing
            else "Durable protocol execution has required tenant scope."
        ),
        "required_fields": ["owner_user_id", "workspace_id", "study_id"],
        "missing_fields": missing,
        "scope": scope,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _resolve_state_after_path(value: Any, path: str) -> Any:
    current = value
    for segment in [part for part in path.split(".") if part]:
        if not isinstance(current, Mapping):
            raise ValueError(f"state_after path '{path}' is not available")
        if segment not in current:
            raise ValueError(f"state_after path '{path}' is not available")
        current = current[segment]
    return current


def _resolve_template_string(value: str, *, receipt_by_node_id: Mapping[str, ProtocolNodeReceipt]) -> str:
    matches = list(_STATE_AFTER_TEMPLATE.finditer(value))
    if not matches:
        return value

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        node_id = matches[0].group(1)
        path = matches[0].group(2)
        receipt = receipt_by_node_id.get(node_id)
        if receipt is None:
            raise ValueError(f"Template reference node '{node_id}' is not available")
        resolved = _resolve_state_after_path(dict(receipt.state_after or {}), path)
        if resolved is None:
            raise ValueError(f"Template reference '{value}' resolved to null")
        return str(resolved)

    def _replace(match: re.Match[str]) -> str:
        node_id = match.group(1)
        path = match.group(2)
        receipt = receipt_by_node_id.get(node_id)
        if receipt is None:
            raise ValueError(f"Template reference node '{node_id}' is not available")
        resolved = _resolve_state_after_path(dict(receipt.state_after or {}), path)
        if resolved is None:
            raise ValueError(f"Template reference '${{{node_id}.state_after.{path}}}' resolved to null")
        return str(resolved)

    return _STATE_AFTER_TEMPLATE.sub(_replace, value)


def _resolve_templates_in_value(value: Any, *, receipt_by_node_id: Mapping[str, ProtocolNodeReceipt]) -> Any:
    if isinstance(value, str):
        return _resolve_template_string(value, receipt_by_node_id=receipt_by_node_id)
    if isinstance(value, list):
        return [_resolve_templates_in_value(item, receipt_by_node_id=receipt_by_node_id) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_templates_in_value(item, receipt_by_node_id=receipt_by_node_id)
            for key, item in value.items()
        }
    return value


def _resolve_node_inputs_from_receipts(
    node: ProtocolNode,
    *,
    prior_receipts: List[ProtocolNodeReceipt],
) -> ProtocolNode:
    receipt_by_node_id: Dict[str, ProtocolNodeReceipt] = {receipt.node_id: receipt for receipt in prior_receipts}
    resolved_inputs = _resolve_templates_in_value(dict(node.inputs), receipt_by_node_id=receipt_by_node_id)
    if resolved_inputs == node.inputs:
        return node
    return node.model_copy(update={"inputs": resolved_inputs})


async def _dispatch_ready_node(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    *,
    protocol_id: str,
    dispatch_node: Optional[ProtocolNodeDispatchAdapter],
    specialist_drivers: Optional[Mapping[str, Any]] = None,
) -> ProtocolNodeDispatchResult:
    from mica.drivers.execution.protocol_executor_registry import global_registry

    result = await global_registry.dispatch(
        request=request,
        node=node,
        user_id=_resolve_request_user_id(request),
        specialist_drivers=specialist_drivers,
    )
    if isinstance(result, ProtocolNodeDispatchResult):
        return result

    if dispatch_node is None:
        if node.policies.protected_surface or node.policies.production_compute:
            raise ValueError(
                f"Protocol node {node.node_id} targets protected surface '{node.executor_surface}' "
                "but no registered executor handled it"
            )
        return ProtocolNodeDispatchResult(
            summary=(
                f"Protocol node {node.node_id} targets surface '{node.executor_surface}' "
                "but no registered executor handled it (unbacked executor surface)."
            ),
            status="failed",
            event_type="node.failed",
            state_after={
                "status": "failed",
                "tool_name": _resolve_tool_name(node),
                "failure_code": "unbacked_executor_surface",
                "requested_executor_surface": node.executor_surface,
                "requested_executor_id": node.executor_id,
            },
            failure_code="unbacked_executor_surface",
            cost_snapshot={"usd": 0.0, "tool_calls": 1},
        )

    result = dispatch_node(node)
    if isawaitable(result):
        result = await result
    if not isinstance(result, ProtocolNodeDispatchResult):
        raise TypeError("dispatch_node must return ProtocolNodeDispatchResult")
    return result


def _build_protocol_node_receipt(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    dispatch_result: ProtocolNodeDispatchResult,
) -> ProtocolNodeReceipt:
    artifact_refs = dispatch_result.artifact_refs or _default_artifact_refs(request, node)
    evidence_refs = dispatch_result.evidence_refs or _default_evidence_refs(request, node)
    approval_refs = dispatch_result.approval_refs or _approval_refs(request, node)
    state_after = {
        "status": str(dispatch_result.status or "completed"),
        "summary": dispatch_result.summary,
        **dict(dispatch_result.state_after or {}),
    }
    if dispatch_result.failure_code:
        state_after.setdefault("failure_code", dispatch_result.failure_code)
    return ProtocolNodeReceipt(
        schema_id=request.ledger_policy.receipt_schema,
        protocol_id=request.protocol_id,
        node_id=node.node_id,
        event_type=dispatch_result.event_type,
        actor_surface=node.executor_surface,
        actor_id=node.executor_id,
        state_before={
            "status": "ready",
            "dependencies": list(node.dependencies),
            "executor_surface": node.executor_surface,
            "executor_id": node.executor_id,
        },
        state_after=state_after,
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        cost_snapshot=dict(dispatch_result.cost_snapshot or {"usd": 0.0, "tool_calls": 1}),
        approval_refs=approval_refs,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _advance_protocol_frontier(
    request: ProtocolExecutorRequest,
    node_receipts: List[ProtocolNodeReceipt],
) -> ProtocolExecutionFrontier:
    completed_after = list(request.frontier.completed_node_ids)
    non_terminal_node_ids: set[str] = set()
    for receipt in node_receipts:
        event_type = str(receipt.event_type or "").strip().lower().replace("_", ".")
        status = str((receipt.state_after or {}).get("status") or "").strip().lower()
        if event_type == "node.completed" and status == "completed":
            if receipt.node_id not in completed_after:
                completed_after.append(receipt.node_id)
        else:
            non_terminal_node_ids.add(receipt.node_id)
    completed_set = set(completed_after)

    ready_after: List[str] = []
    blocked_after: List[str] = []
    for node in [*request.ready_nodes, *request.blocked_nodes]:
        if node.node_id in completed_set:
            continue
        if node.node_id in non_terminal_node_ids:
            blocked_after.append(node.node_id)
            continue
        if set(node.dependencies).issubset(completed_set):
            ready_after.append(node.node_id)
        else:
            blocked_after.append(node.node_id)

    return ProtocolExecutionFrontier(
        completed_node_ids=completed_after,
        ready_node_ids=ready_after,
        blocked_node_ids=blocked_after,
        receipt_count=request.frontier.receipt_count + len(node_receipts),
    )


def _build_node_cue_manager(
    request: ProtocolExecutorRequest,
    node: ProtocolNode,
    *,
    run_id: str,
) -> Optional[ProtocolCueRuntimeManager]:
    if not node.scientific_cues:
        return None
    return ProtocolCueRuntimeManager.for_protocol_node(
        node=node,
        protocol_id=request.protocol_id,
        session_id=request.session_id,
        run_id=run_id,
        transport=PROTOCOL_EXECUTOR_RUNTIME_TRANSPORT,
    )


def _attach_scientific_runtime(
    dispatch_result: ProtocolNodeDispatchResult,
    cue_manager: ProtocolCueRuntimeManager,
) -> ProtocolNodeDispatchResult:
    scientific_runtime = cue_manager.runtime_payload()
    state_after = dict(dispatch_result.state_after or {})
    state_after["scientific_runtime"] = scientific_runtime
    state_after["cue_results"] = [result.model_dump(mode="json") for result in cue_manager.envelope.cue_results]
    state_after["protocol_events"] = list(cue_manager.protocol_events)
    return ProtocolNodeDispatchResult(
        summary=dispatch_result.summary,
        status=dispatch_result.status,
        event_type=dispatch_result.event_type,
        state_after=state_after,
        artifact_refs=list(dispatch_result.artifact_refs),
        evidence_refs=list(dispatch_result.evidence_refs),
        cost_snapshot=dict(dispatch_result.cost_snapshot),
        approval_refs=list(dispatch_result.approval_refs),
        failure_code=dispatch_result.failure_code,
    )


def _rebuild_protocol_executor_request(
    request: ProtocolExecutorRequest,
    *,
    frontier: ProtocolExecutionFrontier,
    node_by_id: Mapping[str, ProtocolNode],
    prior_receipts: List[ProtocolNodeReceipt],
) -> ProtocolExecutorRequest:
    return ProtocolExecutorRequest(
        protocol_id=request.protocol_id,
        protocol_version=request.protocol_version,
        session_id=request.session_id,
        execution_mode=request.execution_mode,
        risk_profile=request.risk_profile,
        approval_policy=request.approval_policy,
        ledger_policy=request.ledger_policy,
        frontier=frontier,
        ready_nodes=[node_by_id[node_id] for node_id in frontier.ready_node_ids],
        blocked_nodes=[node_by_id[node_id] for node_id in frontier.blocked_node_ids],
        prior_receipts=list(prior_receipts),
        request_metadata=dict(request.request_metadata or {}),
    )


_REFERENCE_HASH_KEYS: Tuple[str, ...] = ("sha256", "sha1", "md5", "hash")
_REFERENCE_ID_KEYS: Tuple[str, ...] = (
    "artifact_id",
    "evidence_id",
    "ref_id",
    "id",
)
_REFERENCE_URI_KEYS: Tuple[str, ...] = (
    "storage_uri",
    "uri",
    "ref",
    "reference",
    "value",
)
_ARTIFACT_STATE_KEYS: Tuple[str, ...] = (
    "artifact_refs",
    "output_refs",
    "receipt_refs",
    "graph_artifacts",
    "node_output_artifacts",
    "node_output_refs",
)
_EVIDENCE_STATE_KEYS: Tuple[str, ...] = (
    "evidence_refs",
    "evidence_spans",
    "citation_spans",
)


def _first_non_empty_mapping_value(payload: Mapping[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, str):
            cleaned = candidate.strip()
            if cleaned:
                return cleaned
    return None


def _reference_identity_key(reference: Any) -> str:
    if isinstance(reference, str):
        return reference.strip()

    if isinstance(reference, Mapping):
        hash_value = _first_non_empty_mapping_value(reference, _REFERENCE_HASH_KEYS)
        uri_value = _first_non_empty_mapping_value(reference, _REFERENCE_URI_KEYS)
        if uri_value and hash_value:
            return f"{uri_value}#{hash_value}"
        if uri_value:
            return uri_value
        if hash_value:
            return f"hash://{hash_value}"

        ref_id = _first_non_empty_mapping_value(reference, _REFERENCE_ID_KEYS)
        if ref_id:
            return ref_id

        paper_id = _first_non_empty_mapping_value(reference, ("paper_id", "paperId", "pmid", "doi"))
        span_id = _first_non_empty_mapping_value(reference, ("span_id", "spanId", "section_id", "sectionId"))
        if paper_id and span_id:
            return f"paper://{paper_id}#span:{span_id}"

        return json.dumps(reference, sort_keys=True, ensure_ascii=True, default=str)

    return str(reference).strip()


def _reference_storage_value(reference: Any) -> str:
    if isinstance(reference, str):
        return reference.strip()

    if isinstance(reference, Mapping):
        hash_value = _first_non_empty_mapping_value(reference, _REFERENCE_HASH_KEYS)
        uri_value = _first_non_empty_mapping_value(reference, _REFERENCE_URI_KEYS)
        ref_id = _first_non_empty_mapping_value(reference, _REFERENCE_ID_KEYS)
        paper_id = _first_non_empty_mapping_value(reference, ("paper_id", "paperId", "pmid", "doi"))
        span_id = _first_non_empty_mapping_value(reference, ("span_id", "spanId", "section_id", "sectionId"))

        if uri_value:
            if hash_value:
                return f"{uri_value}#{hash_value}"
            return uri_value
        if paper_id and span_id:
            return f"paper://{paper_id}#span:{span_id}"
        if ref_id:
            return ref_id
        if hash_value:
            return f"hash://{hash_value}"
        return json.dumps(reference, sort_keys=True, ensure_ascii=True, default=str)

    return str(reference).strip()


def _extract_reference_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _dedupe_references(values: List[Any], *, label: str) -> Tuple[List[str], Dict[str, int]]:
    deduped: List[str] = []
    seen_keys: set[str] = set()
    skipped_blank = 0
    for value in values:
        identity_key = _reference_identity_key(value)
        if not identity_key.strip():
            skipped_blank += 1
            continue
        if identity_key in seen_keys:
            continue
        seen_keys.add(identity_key)
        storage_value = _reference_storage_value(value).strip()
        if not storage_value:
            skipped_blank += 1
            continue
        deduped.append(storage_value)

    stats = {
        "input_count": len(values),
        "output_count": len(deduped),
        "duplicates_removed": max(len(values) - len(deduped) - skipped_blank, 0),
        "blank_or_invalid_skipped": skipped_blank,
    }
    return deduped, {f"{label}_{key}": value for key, value in stats.items()}


def _collect_run_level_references(node_receipts: List[ProtocolNodeReceipt]) -> Tuple[List[str], List[str], Dict[str, Any]]:
    artifact_candidates: List[Any] = []
    evidence_candidates: List[Any] = []

    for receipt in node_receipts:
        artifact_candidates.extend(list(receipt.artifact_refs))
        evidence_candidates.extend(list(receipt.evidence_refs))

        state_after = dict(receipt.state_after or {})
        for key in _ARTIFACT_STATE_KEYS:
            artifact_candidates.extend(_extract_reference_list(state_after.get(key)))
        for key in _EVIDENCE_STATE_KEYS:
            evidence_candidates.extend(_extract_reference_list(state_after.get(key)))

    artifact_refs, artifact_stats = _dedupe_references(artifact_candidates, label="artifact")
    evidence_refs, evidence_stats = _dedupe_references(evidence_candidates, label="evidence")
    debug_stats = {
        "artifact_dedup": artifact_stats,
        "evidence_dedup": evidence_stats,
        "source_keys": {
            "artifact_state_keys": list(_ARTIFACT_STATE_KEYS),
            "evidence_state_keys": list(_EVIDENCE_STATE_KEYS),
        },
    }
    return artifact_refs, evidence_refs, debug_stats


async def _publish_protocol_receipts(
    *,
    communication_service: RuntimeCommunicationService,
    checkpoint_dir: str,
    session_id: str,
    run_id: str,
    agent_name: str,
    node_receipts: List[ProtocolNodeReceipt],
    run_receipt: ProtocolRunReceipt,
    unified_runtime: Dict[str, Any],
) -> List[str]:
    projection_message_ids: List[str] = []
    for receipt in node_receipts:
        try:
            message_id = await communication_service.publish_protocol_node_receipt(
                checkpoint_dir=checkpoint_dir,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                receipt=receipt,
            )
        except Exception:
            message_id = None
        if message_id is not None:
            projection_message_ids.append(str(message_id))

    try:
        message_id = await communication_service.publish_protocol_run_receipt(
            checkpoint_dir=checkpoint_dir,
            session_id=session_id,
            run_id=run_id,
            agent_name=agent_name,
            receipt=run_receipt,
        )
    except Exception:
        message_id = None
    if message_id is not None:
        projection_message_ids.append(str(message_id))

    try:
        message_id = await communication_service.publish_unified_protocol_runtime(
            checkpoint_dir=checkpoint_dir,
            session_id=session_id,
            run_id=run_id,
            agent_name=agent_name,
            unified_runtime=unified_runtime,
        )
    except Exception:
        message_id = None
    if message_id is not None:
        projection_message_ids.append(str(message_id))
    return projection_message_ids


def _build_unified_runtime_projection_payload(
    *,
    request: ProtocolExecutorRequest,
    run_receipt: ProtocolRunReceipt,
    node_receipts: List[ProtocolNodeReceipt],
) -> Dict[str, Any]:
    cue_statuses: List[Dict[str, Any]] = []
    cue_lineage: List[Dict[str, Any]] = []
    phase_lineage: List[Dict[str, Any]] = []
    warnings: List[str] = []
    blockers: List[str] = []
    run_receipt_ref = f"run_receipt:{run_receipt.run_id}"
    for receipt in node_receipts:
        state_after = dict(receipt.state_after or {})
        warning_value = ""
        blocked_by_value = ""
        warning = state_after.get("warning")
        if isinstance(warning, str) and warning.strip():
            warning_value = warning.strip()
            warnings.append(f"{receipt.node_id}:{warning_value}")
        blocked_by = state_after.get("blocked_by_cue")
        if isinstance(blocked_by, str) and blocked_by.strip():
            blocked_by_value = blocked_by.strip()
            blockers.append(f"{receipt.node_id}:{blocked_by_value}")

        cue_results = state_after.get("cue_results")
        if isinstance(cue_results, list):
            for entry in cue_results:
                if isinstance(entry, dict):
                    cue_id = str(entry.get("cue_id") or "")
                    cue_status = str(entry.get("status") or "")
                    cue_artifacts = [str(item) for item in list(entry.get("artifacts") or []) if str(item)]
                    cue_statuses.append(
                        {
                            "node_id": receipt.node_id,
                            "cue_id": cue_id,
                            "status": cue_status,
                            "required": bool(entry.get("required", False)),
                        }
                    )
                    cue_lineage.append(
                        {
                            "node_id": receipt.node_id,
                            "cue_id": cue_id,
                            "status": cue_status,
                            "target_prompt_node_id": str(entry.get("target_prompt_node_id") or receipt.node_id),
                            "timestamp": str(entry.get("timestamp") or ""),
                            "note": str(entry.get("note") or ""),
                            "executor_surface": receipt.actor_surface,
                            "executor_id": receipt.actor_id,
                            "source_artifact_refs": cue_artifacts,
                            "source_evidence_refs": list(receipt.evidence_refs),
                            "receipt_refs": {
                                "node_receipt": f"node_receipt:{receipt.node_id}",
                                "run_receipt": run_receipt_ref,
                            },
                            "warnings": [f"{receipt.node_id}:{warning_value}"] if warning_value else [],
                            "blockers": [f"{receipt.node_id}:{blocked_by_value}"] if blocked_by_value else [],
                        }
                    )

        protocol_events = state_after.get("protocol_events")
        if isinstance(protocol_events, list):
            for event in protocol_events:
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                event_data = payload if isinstance(payload, dict) else {}
                event_node_id = str(event_data.get("node_id") or receipt.node_id)
                phase_lineage.append(
                    {
                        "phase": str(event_data.get("phase") or ""),
                        "event_type": str(event.get("event") or ""),
                        "node_id": event_node_id,
                        "cue_id": str(event_data.get("cue_result", {}).get("cue_id") if isinstance(event_data.get("cue_result"), dict) else ""),
                        "timestamp": str(event.get("timestamp") or ""),
                        "executor_surface": receipt.actor_surface,
                        "executor_id": receipt.actor_id,
                        "source_artifact_refs": list(receipt.artifact_refs),
                        "source_evidence_refs": list(receipt.evidence_refs),
                        "receipt_refs": {
                            "node_receipt": f"node_receipt:{receipt.node_id}",
                            "run_receipt": run_receipt_ref,
                        },
                        "warnings": [f"{receipt.node_id}:{warning_value}"] if warning_value else [],
                        "blockers": [f"{receipt.node_id}:{blocked_by_value}"] if blocked_by_value else [],
                    }
                )

    # ── GoG node lookup for enriched projection ───────────────────────────
    _gog_node_by_id: Dict[str, Any] = {
        node.node_id: node
        for node in list(request.ready_nodes) + list(request.blocked_nodes)
    }

    def _subnode_trace_summary(receipt: "ProtocolNodeReceipt") -> Dict[str, Any]:
        sa = dict(receipt.state_after or {})
        trace = sa.get("subnode_trace")
        if isinstance(trace, list):
            return {"count": len(trace), "items": trace}
        # GoG native child execution: synthesize from child_completed_count
        completed = sa.get("child_completed_count")
        if isinstance(completed, int):
            return {"count": completed, "items": []}
        return {"count": 0, "items": []}

    node_statuses = []
    for receipt in node_receipts:
        _gog_node = _gog_node_by_id.get(receipt.node_id)
        ns: Dict[str, Any] = {
            "node_id": receipt.node_id,
            "event_type": receipt.event_type,
            "status": str((receipt.state_after or {}).get("status") or ""),
            "actor_surface": receipt.actor_surface,
            "actor_id": receipt.actor_id,
            # GoG collapse / grouping hints
            "semantic_group": getattr(_gog_node, "semantic_group", None),
            "phase_id": getattr(_gog_node, "phase_id", None),
            "collapsed_by_default": bool(getattr(_gog_node, "collapsed_by_default", False)),
            "child_graph_ref": getattr(_gog_node, "child_graph_id", None),
            "subnode_trace_summary": _subnode_trace_summary(receipt),
        }
        node_statuses.append(ns)

    # ── GoG graph_metadata (sourced from request_metadata or document fields) ──
    _rm = dict(request.request_metadata or {})
    graph_metadata: Dict[str, Any] = {
        "parent_graph_id": _rm.get("parent_graph_id"),
        "graph_level": _rm.get("graph_level"),
        "campaign_id": _rm.get("campaign_id"),
    }

    return {
        "projection_only": True,
        "projection_kind": "protocol_runtime_unified",
        "protocol_id": request.protocol_id,
        "run_id": run_receipt.run_id,
        "session_id": request.session_id,
        "graph_run_status": run_receipt.status,
        "plan_progress": {
            "completed_node_ids": list(run_receipt.frontier_after.completed_node_ids),
            "ready_node_ids": list(run_receipt.frontier_after.ready_node_ids),
            "blocked_node_ids": list(run_receipt.frontier_after.blocked_node_ids),
            "receipt_count": int(run_receipt.frontier_after.receipt_count),
        },
        "node_statuses": node_statuses,
        "cue_statuses": cue_statuses,
        "cue_lineage": cue_lineage,
        "phase_lineage": phase_lineage,
        "warnings": warnings,
        "blockers": blockers,
        "node_receipts": [receipt.model_dump(mode="json") for receipt in node_receipts],
        "run_receipts": [run_receipt.model_dump(mode="json")],
        "artifact_refs": list(run_receipt.artifact_refs),
        "evidence_refs": list(run_receipt.evidence_refs),
        "graph_metadata": graph_metadata,
        "compatibility": {
            "mirror_topics": ["runtime.protocol.node", "runtime.protocol.run"],
            "source": "protocol_executor",
        },
        "metadata": {
            "execution_transport": PROTOCOL_EXECUTOR_RUNTIME_TRANSPORT,
            "request_metadata": dict(request.request_metadata or {}),
        },
    }


async def execute_protocol_executor_request(
    request: ProtocolExecutorRequest,
    *,
    checkpoint_dir: str,
    dispatch_node: Optional[ProtocolNodeDispatchAdapter] = None,
    specialist_drivers: Optional[Mapping[str, Any]] = None,
    communication_service: Optional[RuntimeCommunicationService] = None,
    agent_name: str = "protocol_executor",
) -> ProtocolExecutionOutcome:
    active_service = communication_service or build_protocol_runtime_communication_service()
    run_id = uuid4().hex
    frontier_before = request.frontier
    node_by_id = {node.node_id: node for node in [*request.ready_nodes, *request.blocked_nodes]}
    active_request = request
    node_receipts: List[ProtocolNodeReceipt] = []
    prior_receipts: List[ProtocolNodeReceipt] = list(request.prior_receipts)
    failure_message: Optional[str] = None
    status = "in_progress"
    frontier_after = request.frontier
    tenant_scope_receipt: Optional[Dict[str, Any]] = None
    if request.ledger_policy.require_durable_lineage:
        tenant_scope_receipt = _evaluate_durable_tenant_scope(request)

    while True:
        if tenant_scope_receipt is not None and tenant_scope_receipt["decision"] == "rejected":
            failure_message = str(tenant_scope_receipt["reason"])
            status = "failed"
            break

        slice_receipts: List[ProtocolNodeReceipt] = []
        for node in active_request.ready_nodes:
            resolved_node = _resolve_node_inputs_from_receipts(
                node,
                prior_receipts=[*prior_receipts, *slice_receipts],
            )
            cue_manager = _build_node_cue_manager(active_request, node, run_id=run_id)
            try:
                has_policy = (
                    node.policies.protected_surface
                    or node.policies.production_compute
                    or node.policies.requires_human_approval
                )
                approval_granted = _protocol_bool(
                    resolved_node.inputs, "approval_granted", "approved", "approved_for_dispatch"
                )
                if has_policy:
                    policy_receipt = {
                        "protocol_id": active_request.protocol_id,
                        "node_id": node.node_id,
                        "policies_evaluated": {
                            "protected_surface": node.policies.protected_surface,
                            "production_compute": node.policies.production_compute,
                            "requires_human_approval": node.policies.requires_human_approval,
                        },
                        "decision": "approved" if approval_granted else "rejected",
                        "reason": (
                            "Explicit approval granted in node inputs."
                            if approval_granted
                            else "Explicit approval required but not granted."
                        ),
                        "approvers": list(active_request.approval_policy.required_approvers),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    if not approval_granted:
                        raise PolicyEnforcementError(
                            f"Protocol node {node.node_id} requires explicit approval before dispatch.",
                            policy_receipt,
                        )

                if cue_manager is not None:
                    pre_gate = cue_manager.pre_tool_gate(
                        tool_name=_resolve_tool_name(resolved_node),
                        args=dict(resolved_node.inputs),
                        call_id=f"{run_id}:{node.node_id}:pre",
                        step=1,
                    )
                    if pre_gate.get("blocked"):
                        raise RuntimeError(str(pre_gate.get("message") or f"Scientific cue blocked node {node.node_id}"))
                dispatch_result = await _dispatch_ready_node(
                    active_request,
                    resolved_node,
                    protocol_id=active_request.protocol_id,
                    dispatch_node=dispatch_node,
                    specialist_drivers=specialist_drivers,
                )
                if has_policy:
                    state_after = dict(dispatch_result.state_after or {})
                    state_after["policy_decision_receipt"] = policy_receipt
                    dispatch_result = ProtocolNodeDispatchResult(
                        summary=dispatch_result.summary,
                        status=dispatch_result.status,
                        event_type=dispatch_result.event_type,
                        state_after=state_after,
                        artifact_refs=dispatch_result.artifact_refs,
                        evidence_refs=dispatch_result.evidence_refs,
                        cost_snapshot=dispatch_result.cost_snapshot,
                        approval_refs=dispatch_result.approval_refs,
                        failure_code=dispatch_result.failure_code,
                    )
                if cue_manager is not None:
                    post_gate = cue_manager.post_tool_gate(
                        tool_name=_resolve_tool_name(resolved_node),
                        result_text=json.dumps(dict(dispatch_result.state_after or {}), ensure_ascii=True, default=str),
                        call_id=f"{run_id}:{node.node_id}:post",
                        step=1,
                    )
                    if post_gate.get("blocked"):
                        dispatch_result = ProtocolNodeDispatchResult(
                            summary=str(post_gate.get("message") or dispatch_result.summary),
                            status="blocked",
                            event_type="node.blocked",
                            state_after={
                                **dict(dispatch_result.state_after or {}),
                                "blocked_by_cue": str(post_gate.get("cue_id") or ""),
                            },
                            artifact_refs=list(dispatch_result.artifact_refs),
                            evidence_refs=list(dispatch_result.evidence_refs),
                            cost_snapshot=dict(dispatch_result.cost_snapshot),
                            approval_refs=list(dispatch_result.approval_refs),
                            failure_code=str(post_gate.get("cue_id") or "scientific_cue_blocked"),
                        )
                    dispatch_result = _attach_scientific_runtime(dispatch_result, cue_manager)
                slice_receipts.append(_build_protocol_node_receipt(active_request, node, dispatch_result))
                if dispatch_result.status != "completed":
                    failure_message = dispatch_result.summary
                    status = "failed"
                    break
            except PolicyEnforcementError as exc:
                failure_message = str(exc)
                status = "failed"
                failed_result = ProtocolNodeDispatchResult(
                    summary=str(exc),
                    status="failed",
                    event_type="node.failed",
                    state_after={
                        "error": str(exc),
                        "policy_decision_receipt": exc.policy_receipt,
                    },
                    failure_code="policy_violation",
                )
                slice_receipts.append(_build_protocol_node_receipt(active_request, node, failed_result))
                break
            except Exception as exc:
                failure_message = str(exc)
                status = "failed"
                failed_result = ProtocolNodeDispatchResult(
                    summary=str(exc),
                    status="failed",
                    event_type="node.failed",
                    state_after={"error": str(exc)},
                    failure_code="dispatch_error",
                )
                if cue_manager is not None:
                    failed_result = _attach_scientific_runtime(failed_result, cue_manager)
                slice_receipts.append(_build_protocol_node_receipt(active_request, node, failed_result))
                break

        node_receipts.extend(slice_receipts)
        prior_receipts.extend(slice_receipts)
        frontier_after = _advance_protocol_frontier(active_request, slice_receipts)
        if failure_message is not None:
            break

        if not frontier_after.ready_node_ids and not frontier_after.blocked_node_ids:
            status = "completed"
            break
        if not frontier_after.ready_node_ids:
            status = "in_progress"
            break

        active_request = _rebuild_protocol_executor_request(
            active_request,
            frontier=frontier_after,
            node_by_id=node_by_id,
            prior_receipts=prior_receipts,
        )

    run_artifact_refs, run_evidence_refs, dedup_debug = _collect_run_level_references(node_receipts)
    if tenant_scope_receipt is not None:
        dedup_debug["tenant_scope_receipt"] = tenant_scope_receipt

    preliminary_run_receipt = ProtocolRunReceipt(
        protocol_id=request.protocol_id,
        run_id=run_id,
        frontier_before=frontier_before,
        frontier_after=frontier_after,
        executed_node_ids=[receipt.node_id for receipt in node_receipts],
        emitted_node_receipt_ids=[receipt.node_id for receipt in node_receipts],
        status=status,
        artifact_refs=run_artifact_refs,
        evidence_refs=run_evidence_refs,
        projection_message_ids=[],
        validation_debug=dedup_debug,
    )

    projection_message_ids: List[str] = []
    unified_runtime_payload = _build_unified_runtime_projection_payload(
        request=request,
        run_receipt=preliminary_run_receipt,
        node_receipts=node_receipts,
    )
    if request.ledger_policy.emit_events and active_service is not None:
        projection_message_ids = await _publish_protocol_receipts(
            communication_service=active_service,
            checkpoint_dir=checkpoint_dir,
            session_id=request.session_id,
            run_id=run_id,
            agent_name=agent_name,
            node_receipts=node_receipts,
            run_receipt=preliminary_run_receipt,
            unified_runtime=unified_runtime_payload,
        )

    final_run_receipt = preliminary_run_receipt.model_copy(
        update={"projection_message_ids": projection_message_ids}
    )
    return ProtocolExecutionOutcome(
        run_receipt=final_run_receipt,
        node_receipts=node_receipts,
        projection_message_ids=projection_message_ids,
        failure_message=failure_message,
    )
