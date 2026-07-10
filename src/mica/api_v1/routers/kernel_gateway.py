"""kernel_gateway.py — REST Gateway endpoint for governed Command Kernel execution."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel, Field

from mica.api_v1.auth import effective_context_dependency, user_dependency
from mica.agentic.backend_command_manifest import BACKEND_COMMAND_MANIFEST
from mica.agentic.commands.protocol_commands import _load_protocol_payload
from mica.agentic.command_kernel import UnifiedAgentCommandKernel
from mica.identity.effective_context import EffectiveContext, resolve_effective_context
from mica.sdk.command_contracts import BackendCommandEnvelope, BackendCommandPolicy
from mica.drivers.execution.protocol_executor_registry import protocol_node_has_executor_binding
from mica_q.protocol_jsonld_validator import validate_protocol_jsonld

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kernel", tags=["kernel"])


class CapabilityItem(BaseModel):
    command: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    effect: str  # "read", "propose", "execute"
    requires_approval: bool
    blocked: bool
    implementation_state: str
    family: str
    owner_lab: str
    auth_required: bool
    idempotency_required: bool
    blocker_reason: Optional[str] = None
    runtime_backing: str
    durability: str
    trust_state: str


class ProtocolValidateRequest(BaseModel):
    protocol_jsonld: Optional[Dict[str, Any]] = Field(default=None, alias="protocolJsonld")
    protocol_json: Optional[Dict[str, Any]] = None
    protocol_draft: Optional[Dict[str, Any]] = None
    tool_plan: Optional[Dict[str, Any]] = None
    protocol_plan: Optional[Dict[str, Any]] = Field(default=None, alias="protocolPlan")
    steps: Optional[List[Dict[str, Any]]] = None
    protocol_id: Optional[str] = None
    protocol_name: Optional[str] = None
    goal: Optional[str] = None
    protocol_path: Optional[str] = None
    prepare_executor_request: bool = False
    workspace_id: Optional[str] = None
    study_id: Optional[str] = None

    class Config:
        populate_by_name = True


class ProtocolSubmitRequest(BaseModel):
    protocol_jsonld: Optional[Dict[str, Any]] = Field(default=None, alias="protocolJsonld")
    protocol_json: Optional[Dict[str, Any]] = None
    protocol_draft: Optional[Dict[str, Any]] = None
    tool_plan: Optional[Dict[str, Any]] = None
    protocol_plan: Optional[Dict[str, Any]] = Field(default=None, alias="protocolPlan")
    steps: Optional[List[Dict[str, Any]]] = None
    protocol_id: Optional[str] = None
    protocol_name: Optional[str] = None
    goal: Optional[str] = None
    protocol_path: Optional[str] = None
    workspace_id: str
    study_id: str
    idempotency_key: Optional[str] = None

    class Config:
        populate_by_name = True


def _runtime_truth_for_capability(
    *,
    command_name: str,
    family: str,
    binding_surface: str,
    implemented_status: str,
    kb_truth: tuple[str, str, str],
    graphrag_truth: tuple[str, str, str],
    artifact_truth: tuple[str, str, str],
) -> tuple[str, str, str]:
    if implemented_status != "implemented":
        return "unavailable", "non_durable", "blocked"
    if command_name.startswith("kb."):
        return kb_truth
    if command_name.startswith("graphrag."):
        return graphrag_truth
    if command_name in {"artifact.attach_to_study", "artifact.attach_to_working_set"}:
        return artifact_truth
    if family == "resource":
        return "local", "non_durable", "active"
    if command_name == "protocol.validate":
        return "local", "non_durable", "active"
    if binding_surface in {"command_kernel", "backend_api", "quetzal", "models", "ese"}:
        return "local", "non_durable", "preview"
    return "local", "non_durable", "preview"


@router.get("/capabilities", response_model=List[CapabilityItem])
async def list_capabilities(user_id: str = Depends(user_dependency)):
    """List all registered Command Kernel capabilities with status and metadata."""
    from mica.api_v1.routers.agent_tool_manifest import (
        _resolve_backing_statuses,
    )

    # Resolve backing status dynamically, but do not let slow probes freeze the gateway.
    (kb_backing, kb_durability, kb_trust, _), (
        gr_backing,
        gr_durability,
        gr_trust,
        _,
    ), (
        ne_backing,
        ne_durability,
        ne_trust,
        _,
    ) = await _resolve_backing_statuses()

    items: List[CapabilityItem] = []
    for name, entry in BACKEND_COMMAND_MANIFEST.items():
        # Determine effect type
        if entry.risk_tier in ("read_only", "read_scope_protected"):
            effect = "read"
        elif entry.risk_tier == "side_effecting":
            effect = "propose"
        else:
            effect = "execute"

        # Determine if approval is required
        requires_approval = bool(entry.requires_gate or entry.side_effects or entry.canonical_mutation)

        # Determine if blocked
        blocked = entry.implemented_status != "implemented"
        blocker_reason = entry.implemented_status if blocked else None

        runtime_backing, durability, trust_state = _runtime_truth_for_capability(
            command_name=name,
            family=entry.family,
            binding_surface=entry.binding_surface,
            implemented_status=entry.implemented_status,
            kb_truth=(kb_backing, kb_durability, kb_trust),
            graphrag_truth=(gr_backing, gr_durability, gr_trust),
            artifact_truth=(ne_backing, ne_durability, ne_trust),
        )

        items.append(
            CapabilityItem(
                command=name,
                description=entry.description,
                input_schema=entry.input_schema,
                output_schema=entry.output_schema,
                effect=effect,
                requires_approval=requires_approval,
                blocked=blocked,
                implementation_state=entry.implemented_status,
                family=entry.family,
                owner_lab=entry.owner_lab,
                auth_required=entry.auth_required,
                idempotency_required=entry.idempotency_required,
                blocker_reason=blocker_reason,
                runtime_backing=runtime_backing,
                durability=durability,
                trust_state=trust_state,
            )
        )
    return items


class AgentToolResponse(BaseModel):
    ok: bool
    success: bool
    status: str
    tool_name: str
    command_name: str
    summary: str
    data: Dict[str, Any]
    result: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)
    blocker: Optional[Dict[str, Any]] = None
    blocker_code: Optional[str] = None
    blockers: List[Dict[str, Any]] = Field(default_factory=list)
    degradation: Optional[Dict[str, Any]] = None
    degraded_reason: Optional[str] = None
    receipt_refs: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    next_actions: List[str] = Field(default_factory=list)


def map_to_agent_response(
    result: Any,
    tool_name: str,
) -> Dict[str, Any]:
    # Determine status
    if not result.success:
        status_val = "blocked"
        if result.blockers:
            if result.blockers[0].code == "unexpected_kernel_error":
                status_val = "failed"
            else:
                status_val = "blocked"
        blocker_obj = result.blockers[0].model_dump() if result.blockers else None
        blocker_code = result.blockers[0].code if result.blockers else None
        blockers_list = [blocker_obj] if blocker_obj else []

        return {
            "ok": False,
            "success": False,
            "status": status_val,
            "tool_name": tool_name,
            "command_name": result.command_name,
            "summary": result.summary,
            "data": {},
            "result": {},
            "warnings": [],
            "blocker": blocker_obj,
            "blocker_code": blocker_code,
            "blockers": blockers_list,
            "degradation": None,
            "degraded_reason": None,
            "receipt_refs": [],
            "artifact_refs": [],
            "next_actions": [],
        }

    # Success case
    degradation_obj = None
    status_val = str(result.status or "completed").strip().lower() or "completed"
    in_progress_statuses = {"queued", "pending", "submitted", "running", "in_progress"}
    if result.runtime_backing in ("in_memory", "degraded", "unavailable"):
        if status_val not in in_progress_statuses:
            status_val = "degraded"
        degradation_obj = {
            "backing": result.runtime_backing,
            "durability": result.durability or "non_durable",
            "trust_state": result.trust_state or "degraded",
            "degraded_reason": result.degraded_reason
        }

    if result.durability == "non_durable" and status_val == "completed":
        status_val = "non_durable"

    warnings_list = list(result.warnings or [])
    if result.degraded_reason and result.degraded_reason not in warnings_list:
        warnings_list.append(result.degraded_reason)

    data_val = dict(result.result or {})

    return {
        "ok": True,
        "success": True,
        "status": status_val,
        "tool_name": tool_name,
        "command_name": result.command_name,
        "summary": result.summary,
        "data": data_val,
        "result": data_val,
        "warnings": warnings_list,
        "blocker": None,
        "blocker_code": None,
        "blockers": [],
        "degradation": degradation_obj,
        "degraded_reason": result.degraded_reason,
        "receipt_refs": list(result.receipt_refs or []),
        "artifact_refs": list(result.artifact_refs or []),
        "next_actions": [],
    }


def _attach_effective_context(
    envelope: BackendCommandEnvelope,
    *,
    user_id: str,
    ctx: EffectiveContext | None = None,
) -> EffectiveContext:
    """Ensure every kernel envelope carries APV-01 EffectiveContext."""
    resolved = ctx or resolve_effective_context(
        identity=user_id,
        hints={
            "session_id": envelope.session_id,
            "study_id": envelope.study_id,
            "workspace_id": envelope.workspace_id,
            "active_scope_id": envelope.active_scope_id,
            "destination_scope_id": envelope.destination_scope_id,
        },
    )
    envelope.apply_effective_context(resolved)
    envelope.request_identity["surface"] = "api_gateway"
    envelope.request_identity["user_id"] = user_id
    return resolved


@router.post("/execute", response_model=AgentToolResponse)
async def execute_command(
    envelope: BackendCommandEnvelope,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Execute a governed Command Kernel command with agent-facing guardrails."""
    from mica.api_v1.routers.agent_tool_manifest import _get_classification, _get_tool_name

    cmd_name = envelope.command_name

    # Guardrail 1: Unknown command
    if cmd_name not in BACKEND_COMMAND_MANIFEST:
        from mica.sdk.command_contracts import BackendCommandResult, BackendCommandBlocker
        res = BackendCommandResult(
            success=False,
            command_name=cmd_name,
            summary=f"Command '{cmd_name}' not found in manifest.",
            blockers=[BackendCommandBlocker(code="unknown_command", message=f"Command '{cmd_name}' not found.")],
        )
        return map_to_agent_response(res, f"mica.{cmd_name}")

    entry = BACKEND_COMMAND_MANIFEST[cmd_name]
    classification = _get_classification(cmd_name, entry)
    tool_name = _get_tool_name(cmd_name)

    # Guardrail 2: Blocked command
    if entry.implemented_status == "registered_but_blocked" or classification == "blocked":
        from mica.sdk.command_contracts import BackendCommandResult, BackendCommandBlocker
        res = BackendCommandResult(
            success=False,
            command_name=cmd_name,
            summary=f"Command '{cmd_name}' is blocked.",
            blockers=[BackendCommandBlocker(code="registered_but_blocked", message=f"Command '{cmd_name}' is blocked.")],
        )
        return map_to_agent_response(res, tool_name)

    # Guardrail 3: Internal only
    if classification == "internal_only":
        from mica.sdk.command_contracts import BackendCommandResult, BackendCommandBlocker
        res = BackendCommandResult(
            success=False,
            command_name=cmd_name,
            summary=f"Command '{cmd_name}' is internal only.",
            blockers=[BackendCommandBlocker(code="internal_only", message=f"Command '{cmd_name}' is internal only.")],
        )
        return map_to_agent_response(res, tool_name)

    # Guardrail 4: Mutante sin approval
    if entry.risk_tier in ("mutating", "destructive") or entry.canonical_mutation:
        # Check if envelope policy allows side effects or has validation refs
        if not envelope.policy or not envelope.policy.allow_side_effects:
            from mica.sdk.command_contracts import BackendCommandResult, BackendCommandBlocker
            res = BackendCommandResult(
                success=False,
                command_name=cmd_name,
                summary=f"Command '{cmd_name}' is mutating and requires validation approval.",
                blockers=[BackendCommandBlocker(code="side_effects_not_allowed", message="Side effects not allowed.")],
            )
            return map_to_agent_response(res, tool_name)

    _attach_effective_context(envelope, user_id=user_id, ctx=ctx)

    kernel = UnifiedAgentCommandKernel(user_id=user_id)
    result = await kernel.execute(envelope)
    return map_to_agent_response(result, tool_name)


@router.post("/protocols/validate", response_model=AgentToolResponse)
async def validate_protocol(
    body: ProtocolValidateRequest,
    user_id: str = Depends(user_dependency),
    ctx: EffectiveContext = Depends(effective_context_dependency),
):
    """Validate a ProtocolJSONLDDocument through the Command Kernel."""
    kernel = UnifiedAgentCommandKernel(user_id=user_id)
    envelope = BackendCommandEnvelope(
        command_name="protocol.validate",
        workspace_id=body.workspace_id,
        study_id=body.study_id,
        request_identity={"surface": "api_gateway", "user_id": user_id},
        arguments={
            "protocol_jsonld": body.protocol_jsonld,
            "protocol_json": body.protocol_json,
            "protocol_draft": body.protocol_draft,
            "tool_plan": body.tool_plan,
            "protocol_plan": body.protocol_plan,
            "steps": body.steps,
            "protocol_id": body.protocol_id,
            "protocol_name": body.protocol_name,
            "goal": body.goal,
            "protocol_path": body.protocol_path,
            "prepare_executor_request": body.prepare_executor_request,
        },
        policy=BackendCommandPolicy(allow_side_effects=False),
    )
    _attach_effective_context(envelope, user_id=user_id, ctx=ctx)
    result = await kernel.execute(envelope)
    if not result.success:
        mapped = map_to_agent_response(result, "mica.protocol.validate")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=mapped,
        )
    return map_to_agent_response(result, "mica.protocol.validate")


@router.post("/protocols/submit")
async def submit_protocol(
    body: ProtocolSubmitRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(user_dependency),
):
    """Submit a validated ProtocolJSONLDDocument for execution via the ProtocolExecutor."""
    # 1. Validate payload structure
    try:
        payload = _load_protocol_payload(
            {
                "protocol_jsonld": body.protocol_jsonld,
                "protocol_json": body.protocol_json,
                "protocol_draft": body.protocol_draft,
                "tool_plan": body.tool_plan,
                "protocol_plan": body.protocol_plan,
                "steps": body.steps,
                "protocol_id": body.protocol_id,
                "protocol_name": body.protocol_name,
                "goal": body.goal,
                "protocol_path": body.protocol_path,
            }
        )
        doc = validate_protocol_jsonld(payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ProtocolJSONLDDocument: {e}",
        )
    unresolved_nodes = [
        {
            "node_id": node.node_id,
            "executor_surface": node.executor_surface,
            "executor_id": node.executor_id,
        }
        for node in doc.nodes
        if not protocol_node_has_executor_binding(node)
    ]
    if unresolved_nodes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "status": "blocked",
                "blocker_code": "unknown_executor_surface",
                "summary": "protocol.submit found unresolved executor surfaces.",
                "result": {},
                "receipt_refs": [],
                "artifact_refs": [],
                "warnings": [],
                "degraded_reason": None,
                "details": {"unresolved_nodes": unresolved_nodes},
            },
        )

    # 2. Verify mandatory boundary scopes
    if not user_id or not body.workspace_id or not body.study_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="protocol.submit_request requires non-empty owner_user_id, workspace_id, and study_id.",
        )

    # Import main module helpers dynamically to avoid circular import dependencies
    from mica.api_v1.main import _queue_protocol_executor_job
    from mica.protocol_drafts import protocol_jsonld_to_executor_request

    # 3. Derive executor request
    try:
        executor_request, draft, document, frontier = protocol_jsonld_to_executor_request(
            payload,
            fallback_name=doc.protocol_id,
            session_id=body.idempotency_key or "",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error compiling protocol request: {e}",
        )

    request_metadata = {
        "request_type": "protocol_jsonld",
        "protocol_id": doc.protocol_id,
        "protocol_version": doc.version,
        "owner_user_id": user_id,
        "request_user_id": user_id,
        "workspace_id": body.workspace_id,
        "study_id": body.study_id,
        "mode": "production",
        "idempotency_key": body.idempotency_key,
    }
    executor_request = executor_request.model_copy(update={"request_metadata": request_metadata})

    # 4. Queue executor job as a background task
    res = _queue_protocol_executor_job(
        executor_request=executor_request,
        fallback_prompt="",
        mode="production",
        session_id=body.idempotency_key,
        mcp_enabled=True,
        resource_fabric_enabled=True,
        user_id=user_id,
        background_tasks=background_tasks,
        request_metadata=request_metadata,
        study_id=body.study_id,
    )
    status_tool_args = {
        "protocol_id": doc.protocol_id,
        "protocol_run_id": str(res.get("protocol_run_id") or "").strip(),
        "job_id": str(res.get("job_id") or "").strip(),
    }
    enriched_result = {
        **dict(res or {}),
        "protocol_id": doc.protocol_id,
        "protocol_version": doc.version,
        "status_tool_args": status_tool_args,
        "polling_hint": "Use mica.protocol.status with the exact protocol_id, protocol_run_id, or job_id returned here. Do not infer or shorten identifiers.",
    }
    next_actions = [
        {
            "tool_name": "mica.protocol.status",
            "description": "Poll the queued protocol run using the exact identifiers returned by submit.",
            "args": status_tool_args,
        }
    ]
    
    # Wrap result in the stable AgentToolResponse contract
    return {
        "ok": True,
        "success": True,
        "status": "queued",
        "job_id": res.get("job_id"),
        "protocol_run_id": res.get("protocol_run_id"),
        "protocol_id": doc.protocol_id,
        "protocol_version": doc.version,
        "idempotent_replay": bool(res.get("idempotent_replay")),
        "tool_name": "mica.protocol.submit",
        "command_name": "protocol.submit",
        "summary": "Protocol submitted to background executor queue.",
        "data": enriched_result,
        "result": enriched_result,
        "warnings": [],
        "blocker": None,
        "blocker_code": None,
        "blockers": [],
        "degradation": None,
        "receipt_refs": [],
        "artifact_refs": [],
        "next_actions": next_actions,
    }
