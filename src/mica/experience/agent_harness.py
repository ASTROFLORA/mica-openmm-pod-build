"""APV-10 Visual agent harness — composed product read model.

Authority: Frontend Runtime Contract V0.6 §8 / North Star APV-10
Hard gate: live agent run visible end-to-end (chat + DAG + events + approvals + artifacts).

Consumes: ProductEventOutbox (APV-07), EffectiveContext (APV-01), Experience BFF patterns (APV-06)
Does not own: /ws/mica chat, protocol executor authority, Astroflora geometry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from mica.experience.product_events import (
    ProductEventEnvelope,
    resume_product_events,
)
from mica.identity.effective_context import EffectiveContext

HarnessPanel = Literal["conversation", "execution_graph", "context_rail", "event_drawer"]

HARNESS_CONTROLS: tuple[str, ...] = (
    "stop_after_current_node",
    "cancel_run",
    "approve_reject_mutation",
    "inspect_tool_arguments",
    "open_artifact_in_workspace",
    "add_artifact_to_working_set",
    "open_evidence_path",
    "save_trace_as_protocol_draft",
    "derive_next_study",
    "switch_semantic_view",
    "toggle_fullscreen_surface",
    "inspect_renderer_lifecycle",
)


class HarnessLayout(BaseModel):
    """Contract §8 minimum layout."""

    left: Literal["conversation"] = "conversation"
    center: Literal["execution_graph"] = "execution_graph"
    right: Literal["context_rail"] = "context_rail"
    bottom: Literal["event_drawer"] = "event_drawer"


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant", "system"]
    text: str
    event_id: str | None = None
    occurred_at: datetime | None = None


class DagNodeProjection(BaseModel):
    node_id: str
    status: str
    label: str | None = None
    provider: str | None = None
    cost: float | None = None
    progress: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ApprovalProjection(BaseModel):
    case_id: str
    status: Literal["required", "resolved"]
    decision: str | None = None
    summary: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ArtifactProjection(BaseModel):
    artifact_id: str
    state: Literal["staged", "activated", "preview"]
    label: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AgentHarnessViewModel(BaseModel):
    """Read-only harness projection for Agents app / visual agent surface."""

    schema_urn: Literal["urn:mica:experience:agent_harness:v1"] = "urn:mica:experience:agent_harness:v1"
    actor_user_id: str
    session_id: str
    active_scope_id: str
    permission_fingerprint: str
    correlation_id: str | None = None
    protocol_run_id: str | None = None
    job_id: str | None = None
    layout: HarnessLayout = Field(default_factory=HarnessLayout)
    controls: list[str] = Field(default_factory=lambda: list(HARNESS_CONTROLS))
    conversation: list[ConversationTurn] = Field(default_factory=list)
    plan_summary: str | None = None
    execution_mode: str | None = None
    dag_nodes: list[DagNodeProjection] = Field(default_factory=list)
    approvals: list[ApprovalProjection] = Field(default_factory=list)
    artifacts: list[ArtifactProjection] = Field(default_factory=list)
    receipt_refs: list[str] = Field(default_factory=list)
    timeline: list[ProductEventEnvelope] = Field(default_factory=list)
    failure_reason: str | None = None
    run_status: str = "idle"
    assembled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    panels_populated: dict[str, bool] = Field(default_factory=dict)


def _role_from_payload(payload: dict[str, Any]) -> Literal["user", "assistant", "system"]:
    role = str(payload.get("role") or "").strip().lower()
    if role in {"user", "assistant", "system"}:
        return role  # type: ignore[return-value]
    if payload.get("token") is not None or payload.get("text"):
        return "assistant"
    return "system"


def assemble_agent_harness(
    *,
    ctx: EffectiveContext,
    session_id: str | None = None,
    correlation_id: str | None = None,
    protocol_run_id: str | None = None,
    job_id: str | None = None,
    replay_cursor: str | None = None,
    limit: int = 500,
) -> AgentHarnessViewModel:
    """Compose harness panels from the product event outbox (single bus)."""
    sid = session_id or ctx.session_id
    batch = resume_product_events(
        ctx=ctx,
        replay_cursor=replay_cursor,
        session_id=sid,
        limit=limit,
    )
    events = list(batch.events)
    if correlation_id:
        events = [e for e in events if e.correlation_id == correlation_id]

    conversation: list[ConversationTurn] = []
    dag_nodes: dict[str, DagNodeProjection] = {}
    approvals: dict[str, ApprovalProjection] = {}
    artifacts: dict[str, ArtifactProjection] = {}
    receipt_refs: list[str] = []
    plan_summary: str | None = None
    execution_mode: str | None = None
    failure_reason: str | None = None
    run_status = "idle"
    resolved_corr = correlation_id

    for event in events:
        if resolved_corr is None:
            resolved_corr = event.correlation_id
        payload = dict(event.payload or {})

        if event.event_type == "agent.message.delta":
            text = str(payload.get("text") or payload.get("token") or "").strip()
            if text:
                conversation.append(
                    ConversationTurn(
                        role=_role_from_payload(payload),
                        text=text,
                        event_id=event.event_id,
                        occurred_at=event.occurred_at,
                    )
                )
        elif event.event_type == "agent.plan.created":
            plan_summary = str(payload.get("summary") or plan_summary or "")
            execution_mode = str(payload.get("mode") or execution_mode or "")
            run_status = "planned"
        elif event.event_type == "execution.mode.selected":
            execution_mode = str(payload.get("mode") or execution_mode or "")
        elif event.event_type.startswith("run.node."):
            node = dict(payload.get("node") or payload)
            node_id = str(node.get("node_id") or node.get("id") or event.event_id)
            status = event.event_type.split(".")[-1]
            if status == "progress":
                status = str(node.get("status") or "running")
            dag_nodes[node_id] = DagNodeProjection(
                node_id=node_id,
                status=status,
                label=str(node.get("label") or node.get("name") or node_id),
                provider=node.get("provider"),
                cost=node.get("cost"),
                progress=node.get("progress"),
                raw=node,
            )
            run_status = "running" if status in {"started", "progress", "running"} else run_status
            if status == "failed":
                run_status = "failed"
                failure_reason = str(node.get("error") or node.get("reason") or failure_reason or "node_failed")
            if status == "completed":
                run_status = "running"
        elif event.event_type == "tool.call.started":
            tool = dict(payload.get("tool") or payload)
            node_id = f"tool:{tool.get('name') or tool.get('tool') or event.event_id}"
            dag_nodes[node_id] = DagNodeProjection(
                node_id=node_id,
                status="started",
                label=str(tool.get("name") or tool.get("tool") or node_id),
                raw=tool,
            )
            run_status = "running"
        elif event.event_type == "tool.call.completed":
            tool = dict(payload.get("tool") or payload)
            node_id = f"tool:{tool.get('name') or tool.get('tool') or event.event_id}"
            dag_nodes[node_id] = DagNodeProjection(
                node_id=node_id,
                status="completed" if payload.get("ok", True) else "failed",
                label=str(tool.get("name") or tool.get("tool") or node_id),
                raw=tool,
            )
        elif event.event_type == "approval.required":
            approval = dict(payload.get("approval") or payload)
            case_id = str(approval.get("case_id") or approval.get("id") or event.event_id)
            approvals[case_id] = ApprovalProjection(
                case_id=case_id,
                status="required",
                summary=str(approval.get("summary") or approval.get("rationale") or ""),
                raw=approval,
            )
            run_status = "awaiting_approval"
        elif event.event_type == "approval.resolved":
            approval = dict(payload.get("approval") or payload)
            case_id = str(approval.get("case_id") or approval.get("id") or event.event_id)
            approvals[case_id] = ApprovalProjection(
                case_id=case_id,
                status="resolved",
                decision=str(payload.get("decision") or approval.get("decision") or "approved"),
                summary=str(approval.get("summary") or ""),
                raw=approval,
            )
        elif event.event_type == "artifact.staged":
            artifact = dict(payload.get("artifact") or payload)
            art_id = str(artifact.get("artifact_id") or artifact.get("id") or event.event_id)
            artifacts[art_id] = ArtifactProjection(
                artifact_id=art_id,
                state="staged",
                label=str(artifact.get("label") or artifact.get("name") or art_id),
                raw=artifact,
            )
        elif event.event_type == "artifact.activated":
            artifact = dict(payload.get("artifact") or payload)
            art_id = str(artifact.get("artifact_id") or artifact.get("id") or event.event_id)
            artifacts[art_id] = ArtifactProjection(
                artifact_id=art_id,
                state="activated",
                label=str(artifact.get("label") or artifact.get("name") or art_id),
                raw=artifact,
            )
        elif event.event_type == "artifact.preview.ready":
            artifact = dict(payload.get("artifact") or payload)
            art_id = str(artifact.get("artifact_id") or artifact.get("id") or event.event_id)
            artifacts[art_id] = ArtifactProjection(
                artifact_id=art_id,
                state="preview",
                label=str(artifact.get("label") or art_id),
                raw=artifact,
            )
        elif event.event_type == "receipt.issued":
            ref = event.receipt_ref or str(
                (payload.get("receipt") or {}).get("receipt_ref")
                or (payload.get("receipt") or {}).get("id")
                or event.event_id
            )
            if ref not in receipt_refs:
                receipt_refs.append(ref)
        elif event.event_type == "protocol.status.changed":
            run_status = str(payload.get("status") or run_status)
        elif event.event_type == "run.node.failed":
            run_status = "failed"
            failure_reason = str(payload.get("reason") or failure_reason or "run_failed")
        elif event.event_type == "agent.refusal":
            run_status = "refused"
            failure_reason = str(payload.get("reason") or "agent_refusal")

    panels = {
        "conversation": bool(conversation),
        "execution_graph": bool(dag_nodes),
        "context_rail": bool(approvals or artifacts or receipt_refs or plan_summary),
        "event_drawer": bool(events),
    }

    return AgentHarnessViewModel(
        actor_user_id=ctx.actor_user_id,
        session_id=sid,
        active_scope_id=ctx.active_scope_id,
        permission_fingerprint=ctx.permission_fingerprint,
        correlation_id=resolved_corr,
        protocol_run_id=protocol_run_id,
        job_id=job_id,
        conversation=conversation,
        plan_summary=plan_summary,
        execution_mode=execution_mode,
        dag_nodes=list(dag_nodes.values()),
        approvals=list(approvals.values()),
        artifacts=list(artifacts.values()),
        receipt_refs=receipt_refs,
        timeline=events,
        failure_reason=failure_reason,
        run_status=run_status,
        panels_populated=panels,
    )


def harness_hard_gate_satisfied(vm: AgentHarnessViewModel) -> bool:
    """True when chat + DAG + events + approvals + artifacts are all visible."""
    return all(
        [
            vm.panels_populated.get("conversation"),
            vm.panels_populated.get("execution_graph"),
            vm.panels_populated.get("event_drawer"),
            bool(vm.approvals),
            bool(vm.artifacts),
        ]
    )


__all__ = [
    "HARNESS_CONTROLS",
    "AgentHarnessViewModel",
    "ApprovalProjection",
    "ArtifactProjection",
    "ConversationTurn",
    "DagNodeProjection",
    "HarnessLayout",
    "assemble_agent_harness",
    "harness_hard_gate_satisfied",
]
