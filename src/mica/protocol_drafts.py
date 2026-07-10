from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Dict, List, Mapping

from mica.agentic.execution_mode_selector import (
    AgenticExecutionRequest,
    AgenticExecutionSelection,
    select_agentic_execution_mode,
)
from mica.agentic.backend_command_manifest import (
    BackendCommandManifestEntry,
    canonical_backend_command_name,
    get_backend_command_manifest_entry,
    iter_manifest_entries,
)
from mica.tools_authority.tool_alias_registry import canonical_tool_name_for_command
from mica_q.protocol_jsonld_contract import (
    ProtocolApprovalMode,
    ProtocolApprovalPolicy,
    ProtocolBudgetPolicy,
    ProtocolExecutionMode,
    ProtocolExecutionFrontier,
    ProtocolExecutorRequest,
    ProtocolJSONLDDocument,
    ProtocolLedgerMode,
    ProtocolLedgerPolicy,
    ProtocolNode,
    ProtocolNodeReceipt,
    ProtocolReceiptSchema,
    ProtocolRiskProfile,
)
from mica_q.protocol_jsonld_validator import (
    derive_protocol_execution_frontier,
    load_protocol_node_receipts,
    validate_protocol_jsonld,
)


class ProtocolDraftValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ProtocolDraftStep:
    id: str
    tool_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    label: str = ""
    kind: str = "tool"
    description: str = ""
    status: str = "pending"


@dataclass(frozen=True)
class ProtocolDraft:
    id: str
    name: str
    steps: List[ProtocolDraftStep]
    description: str = ""
    goal: str = ""
    source: str = "frontend"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolToolPlanStep:
    id: str
    tool_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    objective: str = ""
    node_kind: str = "tool"
    executor_surface: str = ""
    executor_id: str = ""
    expected_outputs: Dict[str, Any] = field(default_factory=lambda: {"artifacts": []})
    evidence_requirements: List[str] = field(default_factory=lambda: ["node_receipt"])
    policies: Dict[str, Any] = field(default_factory=dict)
    failure_policy: str = "halt"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolToolPlan:
    id: str
    name: str
    steps: List[ProtocolToolPlanStep]
    description: str = ""
    goal: str = ""
    session_id: str = ""
    owner_lab: str = "Agentic Control Plane"
    execution_mode: str = "development"
    risk_profile: str = "medium"
    max_usd: float = 5.0
    max_wall_clock_s: int = 600
    approval_mode: str = "auto"
    required_approvers: List[str] = field(default_factory=list)
    protected_surfaces: List[str] = field(default_factory=list)
    ledger_mode: str = "protocol_and_node_receipts"
    receipt_schema: str = "mica.receipts.node.v1"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolTracePromotionArtifact:
    selection: AgenticExecutionSelection
    tool_plan: ProtocolToolPlan
    draft: ProtocolDraft
    document: ProtocolJSONLDDocument
    frontier: ProtocolExecutionFrontier


def protocol_jsonld_to_protocol_draft(
    payload: ProtocolJSONLDDocument | Mapping[str, Any],
    *,
    fallback_name: str = "",
    fallback_description: str = "",
    fallback_goal: str = "",
    node_receipts: list[ProtocolNodeReceipt | Mapping[str, Any]] | None = None,
) -> tuple[ProtocolDraft, ProtocolJSONLDDocument, ProtocolExecutionFrontier]:
    document = validate_protocol_jsonld(payload)
    frontier = derive_protocol_execution_frontier(document, node_receipts)
    if not frontier.ready_node_ids:
        raise ProtocolDraftValidationError(
            "No executable protocol frontier remains; all nodes already have conforming receipts"
        )

    metadata = dict(document.metadata)
    ready_node_ids = set(frontier.ready_node_ids)
    draft = ProtocolDraft(
        id=document.protocol_id,
        name=_resolve_protocol_name(document, fallback_name=fallback_name),
        description=_metadata_text_value(metadata.get("description")) or fallback_description.strip(),
        goal=_metadata_text_value(metadata.get("goal")) or fallback_goal.strip(),
        source="protocol_jsonld",
        metadata={
            **metadata,
            "protocol_version": document.version,
            "owner_lab": document.owner_lab,
            "execution_mode": document.execution_mode.value,
            "risk_profile": document.risk_profile.value,
            "approval_policy": document.approval_policy.model_dump(mode="json"),
            "approval_mode": document.approval_policy.mode.value,
            "receipt_schema": document.ledger_policy.receipt_schema,
            "receipt_gate_active": True,
            "completed_step_ids": list(frontier.completed_node_ids),
            "blocked_step_ids": list(frontier.blocked_node_ids),
            "ready_step_ids": list(frontier.ready_node_ids),
            "provided_receipt_count": frontier.receipt_count,
        },
        steps=[_protocol_node_to_draft_step(node) for node in document.nodes if node.node_id in ready_node_ids],
    )
    return draft, document, frontier


def compile_tool_plan_to_protocol_jsonld(
    plan: ProtocolToolPlan,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> ProtocolJSONLDDocument:
    protocol_id = str(plan.id or "").strip()
    if not protocol_id:
        raise ProtocolDraftValidationError("ProtocolToolPlan requires a non-empty id")
    protocol_name = str(plan.name or "").strip()
    if not protocol_name:
        raise ProtocolDraftValidationError("ProtocolToolPlan requires a non-empty name")
    if not plan.steps:
        raise ProtocolDraftValidationError("ProtocolToolPlan requires at least one step")

    owner_lab = str(plan.owner_lab or "").strip() or "Agentic Control Plane"
    session_id = str(plan.session_id or "").strip() or f"{protocol_id}-session"
    merged_metadata = {
        "name": protocol_name,
        "description": str(plan.description or "").strip(),
        "goal": str(plan.goal or "").strip(),
        "compiler_source": "apf-02-tool-plan",
        **dict(plan.metadata or {}),
        **dict(metadata or {}),
    }
    nodes = [_tool_plan_step_to_protocol_node(step) for step in plan.steps]
    edges = [
        {
            "source_node_id": dependency,
            "target_node_id": step.id,
            "edge_type": "data_dependency",
            "rationale": f"{step.id} depends on {dependency}",
        }
        for step in plan.steps
        for dependency in step.dependencies
    ]
    protocol_dict = {
        "@context": "https://mica.astroflora.org/schema/protocol/v1",
        "@type": "MICAProtocol",
        "protocol_id": protocol_id,
        "version": "1.0.0",
        "session_id": session_id,
        "owner_lab": owner_lab,
        "execution_mode": _coerce_execution_mode(plan.execution_mode),
        "risk_profile": _coerce_risk_profile(plan.risk_profile),
        "budgets": ProtocolBudgetPolicy(
            max_steps=len(plan.steps),
            max_usd=float(plan.max_usd),
            max_wall_clock_s=int(plan.max_wall_clock_s),
        ).model_dump(mode="json"),
        "approval_policy": ProtocolApprovalPolicy(
            mode=_coerce_approval_mode(plan.approval_mode),
            required_approvers=list(plan.required_approvers or []),
            protected_surfaces=list(plan.protected_surfaces or []),
        ).model_dump(mode="json"),
        "ledger_policy": ProtocolLedgerPolicy(
            mode=_coerce_ledger_mode(plan.ledger_mode),
            receipt_schema=str(plan.receipt_schema or "mica.receipts.node.v1").strip() or "mica.receipts.node.v1",
            emit_events=True,
            require_node_receipts=True,
        ).model_dump(mode="json"),
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "edges": edges,
        "metadata": merged_metadata,
    }
    try:
        return validate_protocol_jsonld(protocol_dict)
    except Exception as exc:
        raise ProtocolDraftValidationError(f"ProtocolToolPlan canonical validation failed: {exc}") from exc


def tool_plan_to_executor_request(
    plan: ProtocolToolPlan,
    *,
    node_receipts: list[ProtocolNodeReceipt | Mapping[str, Any]] | None = None,
    request_metadata: Mapping[str, Any] | None = None,
) -> tuple[ProtocolExecutorRequest, ProtocolJSONLDDocument, ProtocolExecutionFrontier]:
    document = compile_tool_plan_to_protocol_jsonld(plan)
    frontier = derive_protocol_execution_frontier(document, node_receipts)
    executor_request = build_protocol_executor_request(
        document,
        frontier,
        prior_receipts=node_receipts,
        request_metadata=request_metadata,
    )
    return executor_request, document, frontier


def promote_successful_tool_trace_to_protocol_artifact(
    trace_payload: Mapping[str, Any],
    *,
    protocol_id: str = "",
    protocol_name: str = "",
) -> ProtocolTracePromotionArtifact:
    query = str(trace_payload.get("query") or "").strip()
    tool_calls_raw = trace_payload.get("tool_calls")
    if not isinstance(tool_calls_raw, list) or not tool_calls_raw:
        raise ProtocolDraftValidationError("Trace payload does not contain promotable tool_calls.")

    successful_calls = [
        call for call in tool_calls_raw
        if isinstance(call, Mapping)
        and str(call.get("name") or "").strip()
        and _trace_call_is_promotable_success(call)
    ]
    if not successful_calls:
        raise ProtocolDraftValidationError("Trace payload has no successful tool calls to promote.")
    if len(successful_calls) < 2:
        raise ProtocolDraftValidationError("Trace payload needs at least two successful tool calls for durable promotion.")

    tool_names = tuple(str(call.get("name") or "").strip() for call in successful_calls)
    selection = select_agentic_execution_mode(
        AgenticExecutionRequest(
            tool_names=tool_names,
            expected_artifacts=(),
            requires_provider=bool(trace_payload.get("provider")),
            durable_replay_required=False,
            estimated_steps=len(successful_calls),
            goal_hint=query,
        )
    )
    if selection.mode == "tool":
        raise ProtocolDraftValidationError("Trace payload remains tool-first and should not be promoted to a durable protocol.")

    plan_steps: list[ProtocolToolPlanStep] = []
    previous_step_ids: list[str] = []
    for index, call in enumerate(successful_calls, start=1):
        step_id = f"step-{index:03d}"
        arguments = call.get("arguments")
        params = dict(arguments) if isinstance(arguments, Mapping) else {}
        plan_steps.append(
            ProtocolToolPlanStep(
                id=step_id,
                tool_name=str(call.get("name") or "").strip(),
                params=params,
                dependencies=list(previous_step_ids[-1:]),
                objective=f"Promoted from agentic trace step {index}",
                metadata={
                    "trace_started_at": call.get("started_at"),
                    "trace_finished_at": call.get("finished_at"),
                    "trace_duration_ms": call.get("duration_ms"),
                    "trace_success": bool(call.get("success", True)),
                    "trace_semantic_success": _trace_call_is_promotable_success(call),
                    "trace_response_summary": str(call.get("response_summary") or "")[:1000],
                },
            )
        )
        previous_step_ids.append(step_id)

    trace_identity = f"{query}||{'|'.join(tool_names)}".encode("utf-8")
    resolved_protocol_id = str(protocol_id or "").strip() or f"apf-promoted-{hashlib.sha256(trace_identity).hexdigest()[:12]}"
    resolved_protocol_name = str(protocol_name or "").strip() or (
        query[:96].strip() if query else f"Promoted agentic trace {resolved_protocol_id}"
    )
    plan = ProtocolToolPlan(
        id=resolved_protocol_id,
        name=resolved_protocol_name,
        description="Promoted from successful agentic sweep trace under APF-03.",
        goal=query,
        session_id=str(trace_payload.get("session_id") or "").strip(),
        owner_lab="Agentic Control Plane",
        execution_mode="production" if bool(trace_payload.get("provider")) else "development",
        risk_profile="medium",
        max_usd=10.0,
        max_wall_clock_s=900,
        steps=plan_steps,
        metadata={
            "compiler_source": "apf-03-trace-promotion",
            "source_trace_transport": str(trace_payload.get("transport") or "").strip(),
            "source_trace_provider": str(trace_payload.get("provider") or "").strip(),
            "source_trace_model": str(trace_payload.get("model") or "").strip(),
            "source_trace_success": bool(trace_payload.get("success", False)),
            "agentic_execution_selection": selection.to_dict(),
        },
    )
    document = compile_tool_plan_to_protocol_jsonld(plan)
    draft, document, frontier = protocol_jsonld_to_protocol_draft(
        document,
        fallback_name=plan.name,
        fallback_description=plan.description,
        fallback_goal=plan.goal,
    )
    return ProtocolTracePromotionArtifact(
        selection=selection,
        tool_plan=plan,
        draft=draft,
        document=document,
        frontier=frontier,
    )


def build_protocol_executor_request(
    document: ProtocolJSONLDDocument,
    frontier: ProtocolExecutionFrontier,
    *,
    session_id: str = "",
    prior_receipts: list[ProtocolNodeReceipt | Mapping[str, Any]] | None = None,
    request_metadata: Mapping[str, Any] | None = None,
) -> ProtocolExecutorRequest:
    ready_node_ids = set(frontier.ready_node_ids)
    blocked_node_ids = set(frontier.blocked_node_ids)
    ready_nodes = [node for node in document.nodes if node.node_id in ready_node_ids]
    blocked_nodes = [node for node in document.nodes if node.node_id in blocked_node_ids]
    if not ready_nodes:
        raise ProtocolDraftValidationError("ProtocolExecutorRequest requires at least one ready node")

    cleaned_session_id = session_id.strip()
    resolved_session_id = cleaned_session_id if cleaned_session_id else document.session_id
    normalized_request_metadata = dict(request_metadata or {})
    normalized_request_metadata.setdefault("protocol_id", document.protocol_id)
    normalized_request_metadata.setdefault("protocol_version", document.version)
    normalized_request_metadata.setdefault("completed_node_ids", list(frontier.completed_node_ids))
    normalized_request_metadata.setdefault("ready_node_ids", list(frontier.ready_node_ids))
    normalized_request_metadata.setdefault("blocked_node_ids", list(frontier.blocked_node_ids))
    normalized_request_metadata.setdefault("provided_receipt_count", frontier.receipt_count)
    # Propagate GoG hierarchy fields from the document into request_metadata
    normalized_request_metadata.setdefault("parent_graph_id", document.parent_graph_id)
    normalized_request_metadata.setdefault("graph_level", document.graph_level)
    normalized_request_metadata.setdefault("campaign_id", document.campaign_id)

    return ProtocolExecutorRequest(
        protocol_id=document.protocol_id,
        protocol_version=document.version,
        session_id=resolved_session_id,
        execution_mode=document.execution_mode,
        risk_profile=document.risk_profile,
        approval_policy=document.approval_policy,
        ledger_policy=document.ledger_policy,
        frontier=frontier,
        ready_nodes=ready_nodes,
        blocked_nodes=blocked_nodes,
        prior_receipts=load_protocol_node_receipts(prior_receipts),
        request_metadata=normalized_request_metadata,
    )


def protocol_jsonld_to_executor_request(
    payload: ProtocolJSONLDDocument | Mapping[str, Any],
    *,
    fallback_name: str = "",
    fallback_description: str = "",
    fallback_goal: str = "",
    node_receipts: list[ProtocolNodeReceipt | Mapping[str, Any]] | None = None,
    session_id: str = "",
    request_metadata: Mapping[str, Any] | None = None,
) -> tuple[ProtocolExecutorRequest, ProtocolDraft, ProtocolJSONLDDocument, ProtocolExecutionFrontier]:
    draft, document, frontier = protocol_jsonld_to_protocol_draft(
        payload,
        fallback_name=fallback_name,
        fallback_description=fallback_description,
        fallback_goal=fallback_goal,
        node_receipts=node_receipts,
    )
    executor_request = build_protocol_executor_request(
        document,
        frontier,
        session_id=session_id,
        prior_receipts=node_receipts,
        request_metadata=request_metadata,
    )
    return executor_request, draft, document, frontier


def _resolve_tool_plan_manifest_entry(step: ProtocolToolPlanStep) -> BackendCommandManifestEntry | None:
    normalized_tool_name = str(step.tool_name or "").strip()
    if not normalized_tool_name:
        raise ProtocolDraftValidationError("ProtocolToolPlan step requires a non-empty tool_name")

    canonical_tool_lookup = {
        canonical_tool_name_for_command(entry.command_name): entry
        for entry in iter_manifest_entries()
    }
    for entry in iter_manifest_entries():
        canonical_tool_lookup.setdefault(_canonical_protocol_tool_name(entry.command_name), entry)
    if normalized_tool_name in canonical_tool_lookup:
        return canonical_tool_lookup[normalized_tool_name]
    try:
        return get_backend_command_manifest_entry(normalized_tool_name)
    except KeyError:
        pass
    canonical_command_name = canonical_backend_command_name(normalized_tool_name)
    if canonical_command_name != normalized_tool_name:
        try:
            return get_backend_command_manifest_entry(canonical_command_name)
        except KeyError:
            return None
    return None


def _default_executor_id_for_surface(executor_surface: str, *, manifest_entry: BackendCommandManifestEntry | None) -> str:
    explicit_surface = str(executor_surface or "").strip()
    if explicit_surface == "compute":
        return "ComputeMD"
    if explicit_surface == "smic":
        return "SMIC"
    if explicit_surface == "smic_bundle":
        return "SMICBundle"
    if explicit_surface == "mica_q_multisurface":
        return "MICAQMultisurfaceService"
    if explicit_surface == "mica_user_workspace":
        return "UserWorkspaceService"
    if explicit_surface == "mica_q_sandbox":
        return "SandboxExecutor"
    if explicit_surface in {"serverless_model", "serverless_models", "model", "models"}:
        return "ServerlessModelGateway"
    if manifest_entry is not None:
        surface = manifest_entry.binding_surface
        if surface == "backend_api":
            return "BackendApiCommand"
        if surface == "command_kernel":
            return "CommandKernelCommand"
        if surface == "resource_fabric":
            return "ResourceFabricCommand"
    return "ToolPlanExecutor"


def _tool_plan_step_to_protocol_node(step: ProtocolToolPlanStep) -> ProtocolNode:
    manifest_entry = _resolve_tool_plan_manifest_entry(step)
    if manifest_entry is None and not (str(step.executor_surface or "").strip() and str(step.executor_id or "").strip()):
        raise ProtocolDraftValidationError(
            f"ProtocolToolPlan step '{step.id}' must resolve to a known manifest command or provide explicit executor_surface/executor_id"
        )

    canonical_command_name = manifest_entry.command_name if manifest_entry is not None else ""
    observed_tool_name = str(step.tool_name or "").strip()
    canonical_tool_name = _canonical_protocol_tool_name(
        canonical_command_name,
        fallback=observed_tool_name,
    )
    executor_surface = (
        str(step.executor_surface or "").strip()
        or (manifest_entry.binding_surface if manifest_entry is not None else "")
    )
    executor_id = (
        str(step.executor_id or "").strip()
        or _default_executor_id_for_surface(executor_surface, manifest_entry=manifest_entry)
    )

    inputs: Dict[str, Any] = dict(step.params or {})
    if manifest_entry is not None:
        inputs.setdefault("command_name", canonical_command_name)
        inputs.setdefault("binding_surface", manifest_entry.binding_surface)
        inputs.setdefault("backend_authority", manifest_entry.backend_authority)
        inputs.setdefault("required_scope", list(manifest_entry.required_scope))
        inputs.setdefault("tool_aliases", list(manifest_entry.tool_aliases))
    inputs.setdefault("tool_name", observed_tool_name or canonical_tool_name)
    if canonical_tool_name and canonical_tool_name != str(inputs.get("tool_name") or "").strip():
        inputs.setdefault("canonical_tool_name", canonical_tool_name)
    if step.metadata:
        inputs.setdefault("step_metadata", dict(step.metadata))

    objective = str(step.objective or "").strip() or f"Execute {canonical_tool_name}"
    return ProtocolNode(
        node_id=str(step.id or "").strip(),
        node_kind=str(step.node_kind or "tool").strip() or "tool",
        executor_surface=executor_surface,
        executor_id=executor_id,
        objective=objective,
        dependencies=list(step.dependencies or []),
        inputs=inputs,
        expected_outputs=dict(step.expected_outputs or {"artifacts": []}),
        evidence_requirements=list(step.evidence_requirements or ["node_receipt"]),
        policies=dict(step.policies or {}),
        failure_policy=str(step.failure_policy or "halt").strip() or "halt",
        receipt_schema=ProtocolReceiptSchema(),
    )


def _coerce_execution_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {mode.value for mode in ProtocolExecutionMode}:
        return normalized
    return ProtocolExecutionMode.DEVELOPMENT.value


def _canonical_protocol_tool_name(command_name: str, *, fallback: str = "") -> str:
    normalized = str(command_name or "").strip()
    if normalized:
        return f"mica.{normalized}"
    cleaned_fallback = str(fallback or "").strip()
    return cleaned_fallback or "mica.unknown"


def _trace_call_is_promotable_success(call: Mapping[str, Any]) -> bool:
    if not bool(call.get("success", True)):
        return False
    parsed_payload = _trace_call_response_payload(call)
    if isinstance(parsed_payload, Mapping):
        if "success" in parsed_payload:
            return bool(parsed_payload.get("success"))
        if "ok" in parsed_payload:
            return bool(parsed_payload.get("ok"))
        status = str(parsed_payload.get("status") or "").strip().lower()
        if status in {"failed", "error", "blocked", "rejected"}:
            return False
        blockers = parsed_payload.get("blockers")
        if isinstance(blockers, list) and blockers:
            return False
        if parsed_payload.get("error"):
            return False
    response_summary = str(call.get("response_summary") or "").strip().lower()
    if '"success": false' in response_summary or "'success': false" in response_summary:
        return False
    return True


def _trace_call_response_payload(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    response_summary = call.get("response_summary")
    if not isinstance(response_summary, str):
        return None
    try:
        parsed = json.loads(response_summary)
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _coerce_risk_profile(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {mode.value for mode in ProtocolRiskProfile}:
        return normalized
    return ProtocolRiskProfile.MEDIUM.value


def _coerce_approval_mode(value: str) -> ProtocolApprovalMode:
    normalized = str(value or "").strip().lower()
    if normalized in {mode.value for mode in ProtocolApprovalMode}:
        return ProtocolApprovalMode(normalized)
    return ProtocolApprovalMode.AUTO


def _coerce_ledger_mode(value: str) -> ProtocolLedgerMode:
    normalized = str(value or "").strip().lower()
    if normalized in {mode.value for mode in ProtocolLedgerMode}:
        return ProtocolLedgerMode(normalized)
    return ProtocolLedgerMode.PROTOCOL_AND_NODE_RECEIPTS


def _resolve_protocol_name(document: ProtocolJSONLDDocument, *, fallback_name: str) -> str:
    metadata_name = _metadata_text_value(document.metadata.get("name"))
    if metadata_name:
        return metadata_name
    cleaned_fallback = fallback_name.strip()
    if cleaned_fallback:
        return cleaned_fallback
    return document.protocol_id


def _metadata_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _resolve_protocol_node_tool_name(node: ProtocolNode) -> str:
    for key in ("tool_name", "action", "tool", "operation"):
        candidate = node.inputs.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return f"{node.executor_surface}:{node.executor_id}"


def _protocol_node_to_draft_step(node: ProtocolNode) -> ProtocolDraftStep:
    params: Dict[str, Any] = dict(node.inputs)
    params["expected_outputs"] = dict(node.expected_outputs)
    params["evidence_requirements"] = list(node.evidence_requirements)
    params["scientific_cues"] = [cue.model_dump(mode="json") for cue in node.scientific_cues]
    params["contradiction_policy"] = node.contradiction_policy.model_dump(mode="json")
    params["promotion_policy"] = node.promotion_policy.model_dump(mode="json")
    params["closure_policy"] = node.closure_policy.model_dump(mode="json")
    params["ui_schema"] = dict(node.ui_schema)
    params["executor_surface"] = node.executor_surface
    params["executor_id"] = node.executor_id
    params["policies"] = node.policies.model_dump(mode="json")
    params["receipt_schema"] = node.receipt_schema.model_dump(mode="json")
    return ProtocolDraftStep(
        id=node.node_id,
        tool_name=_resolve_protocol_node_tool_name(node),
        params=params,
        dependencies=list(node.dependencies),
        label=node.objective,
        kind=node.node_kind,
        description=node.objective,
        status="pending",
    )


def validate_protocol_draft(draft: ProtocolDraft) -> None:
    completed_step_ids = set(_metadata_step_ids(draft.metadata, "completed_step_ids"))
    ready_step_ids = set(_metadata_step_ids(draft.metadata, "ready_step_ids"))
    if not draft.id.strip():
        raise ProtocolDraftValidationError("ProtocolDraft requires a non-empty id")
    if not draft.name.strip():
        raise ProtocolDraftValidationError("ProtocolDraft requires a non-empty name")
    if not draft.steps:
        raise ProtocolDraftValidationError("ProtocolDraft requires at least one step")

    # Enforce ID matching
    metadata_proto_id = draft.metadata.get("protocol_id")
    if metadata_proto_id is not None and str(metadata_proto_id).strip() != draft.id:
        raise ProtocolDraftValidationError("ProtocolDraft metadata protocol_id must match draft id")

    seen: set[str] = set()
    for step in draft.steps:
        step_id = step.id.strip()
        if not step_id:
            raise ProtocolDraftValidationError("Every ProtocolDraft step requires a non-empty id")
        if step_id in seen:
            raise ProtocolDraftValidationError(f"Duplicate ProtocolDraft step id: {step_id}")
        seen.add(step_id)
        if not step.tool_name.strip():
            raise ProtocolDraftValidationError(f"ProtocolDraft step '{step_id}' requires tool_name")

    blocked_step_ids = set(_metadata_step_ids(draft.metadata, "blocked_step_ids"))

    overlap = seen & completed_step_ids
    if overlap:
        raise ProtocolDraftValidationError(
            f"ProtocolDraft completed_step_ids must not overlap executable steps: {sorted(overlap)}"
        )
    if ready_step_ids and seen != ready_step_ids:
        raise ProtocolDraftValidationError(
            "ProtocolDraft ready_step_ids metadata must match the executable frontier steps"
        )

    for step in draft.steps:
        for dep in step.dependencies:
            if dep not in seen and dep not in completed_step_ids:
                raise ProtocolDraftValidationError(
                    f"ProtocolDraft step '{step.id}' depends on missing step '{dep}'"
                )

    # Delegate cycle detection and topological sorting validity to canonical validation
    # by constructing a minimal ProtocolJSONLDDocument dict representation.
    nodes = []
    edges = []

    # Inject dummy nodes for completed/blocked steps to satisfy edge schema validation
    for dummy_id in (completed_step_ids | blocked_step_ids):
        nodes.append({
            "node_id": dummy_id,
            "node_kind": "tool",
            "executor_surface": "sandbox",
            "executor_id": "Sandbox",
            "objective": f"Non-frontier step {dummy_id}",
            "dependencies": [],
            "inputs": {},
            "expected_outputs": {"artifacts": []},
            "evidence_requirements": [],
            "policies": {},
            "failure_policy": "halt",
            "receipt_schema": {"schema_id": "mica.receipts.node.v1"},
        })

    for step in draft.steps:
        nodes.append({
            "node_id": step.id,
            "node_kind": step.kind or "tool",
            "executor_surface": step.params.get("executor_surface", "sandbox"),
            "executor_id": step.params.get("executor_id", "Sandbox"),
            "objective": step.label or step.description or f"Execute step {step.id}",
            "dependencies": list(step.dependencies),
            "inputs": dict(step.params),
            "expected_outputs": step.params.get("expected_outputs", {"artifacts": []}),
            "evidence_requirements": step.params.get("evidence_requirements", ["node_receipt"]),
            "policies": step.params.get("policies", {}),
            "failure_policy": step.params.get("failure_policy", "halt"),
            "receipt_schema": step.params.get("receipt_schema", {"schema_id": "mica.receipts.node.v1"}),
        })
        for dep in step.dependencies:
            edges.append({
                "source_node_id": dep,
                "target_node_id": step.id,
                "edge_type": "control_dependency",
            })

    protocol_dict = {
        "@context": "https://mica.astroflora.org/schema/protocol/v1",
        "@type": "MICAProtocol",
        "protocol_id": draft.id,
        "version": draft.metadata.get("protocol_version", "1.0.0"),
        "session_id": draft.metadata.get("session_id", "session-draft"),
        "owner_lab": draft.metadata.get("owner_lab", "DraftLab"),
        "execution_mode": draft.metadata.get("execution_mode", "development"),
        "risk_profile": draft.metadata.get("risk_profile", "medium"),
        "budgets": {
            "max_steps": len(nodes),
            "max_usd": 10.0,
            "max_wall_clock_s": 3600,
        },
        "approval_policy": {
            "mode": draft.metadata.get("approval_policy", {}).get("mode", draft.metadata.get("approval_mode", "auto")),
            "required_approvers": draft.metadata.get("approval_policy", {}).get("required_approvers", []),
            "protected_surfaces": draft.metadata.get("approval_policy", {}).get("protected_surfaces", []),
        },
        "ledger_policy": {
            "mode": "node_receipts",
            "receipt_schema": draft.metadata.get("receipt_schema", "mica.receipts.node.v1"),
            "emit_events": True,
            "require_node_receipts": True,
        },
        "nodes": nodes,
        "edges": edges,
    }

    try:
        validate_protocol_jsonld(protocol_dict)
    except Exception as exc:
        raise ProtocolDraftValidationError(f"ProtocolDraft canonical validation failed: {exc}") from exc

    _topological_order(draft)


def _topological_order(draft: ProtocolDraft) -> List[ProtocolDraftStep]:
    by_id = {step.id: step for step in draft.steps}
    completed_step_ids = set(_metadata_step_ids(draft.metadata, "completed_step_ids"))
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: List[ProtocolDraftStep] = []

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise ProtocolDraftValidationError(f"ProtocolDraft contains a dependency cycle at '{step_id}'")
        visiting.add(step_id)
        step = by_id[step_id]
        for dep in step.dependencies:
            if dep in by_id:
                visit(dep)
            elif dep in completed_step_ids:
                continue
            else:
                raise ProtocolDraftValidationError(
                    f"ProtocolDraft step '{step.id}' depends on missing step '{dep}'"
                )
        visiting.remove(step_id)
        visited.add(step_id)
        ordered.append(step)

    for step in draft.steps:
        visit(step.id)

    return ordered


def compile_protocol_draft_to_prompt(draft: ProtocolDraft) -> str:
    """LEGACY compatibility fallback prompt builder.

    This function is a legacy compatibility projection only. It must not call
    providers or persist protocol state.
    """
    import warnings
    warnings.warn(
        "compile_protocol_draft_to_prompt is a legacy compatibility projection. "
        "It should not be used as a primary execution model or call providers/persist state.",
        DeprecationWarning,
        stacklevel=2,
    )
    validate_protocol_draft(draft)
    ordered_steps = _topological_order(draft)
    receipt_gate_active = bool(draft.metadata.get("receipt_gate_active"))
    completed_step_ids = _metadata_step_ids(draft.metadata, "completed_step_ids")
    blocked_step_ids = _metadata_step_ids(draft.metadata, "blocked_step_ids")
    lines: List[str] = []
    lines.append(f"Execute protocol draft '{draft.name}' ({draft.id}).")
    if draft.goal:
        lines.append(f"Goal: {draft.goal}")
    if draft.description:
        lines.append(f"Description: {draft.description}")
    if receipt_gate_active:
        lines.append(
            "Receipt/ledger gate is active. Execute only the ready frontier below and do not advance blocked nodes."
        )
        if completed_step_ids:
            lines.append(
                f"Completed steps backed by conforming receipts: {', '.join(completed_step_ids)}"
            )
        if blocked_step_ids:
            lines.append(
                f"Blocked steps awaiting dependency receipts: {', '.join(blocked_step_ids)}"
            )
        lines.append("Executable frontier steps:")
    else:
        lines.append("Use the following DAG as the authoritative execution plan. Respect dependencies exactly.")
        lines.append("Protocol steps:")
    for index, step in enumerate(ordered_steps, start=1):
        deps = ", ".join(step.dependencies) if step.dependencies else "none"
        label = step.label or step.tool_name
        lines.append(
            f"{index}. step_id={step.id}; label={label}; tool={step.tool_name}; depends_on={deps}; params={step.params}"
        )
    lines.append("Return a structured execution summary and preserve step ids in your reasoning trace where possible.")
    return "\n".join(lines)


def _metadata_step_ids(metadata: Mapping[str, Any], key: str) -> list[str]:
    raw_value = metadata.get(key, [])
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ProtocolDraftValidationError(f"ProtocolDraft metadata '{key}' must be a list of step ids")

    step_ids: list[str] = []
    seen: set[str] = set()
    for value in raw_value:
        if not isinstance(value, str) or not value.strip():
            raise ProtocolDraftValidationError(
                f"ProtocolDraft metadata '{key}' must contain non-empty step ids"
            )
        cleaned = value.strip()
        if cleaned in seen:
            raise ProtocolDraftValidationError(
                f"ProtocolDraft metadata '{key}' cannot contain duplicates: {cleaned}"
            )
        seen.add(cleaned)
        step_ids.append(cleaned)
    return step_ids
