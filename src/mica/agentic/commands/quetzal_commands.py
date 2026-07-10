from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from pydantic import ValidationError

from mica.quetzal.gates import QuetzalGate, InvokeContext
from mica.serverless_models.registry import get_default_model_registry, ModelNotRegistered
from mica.agentic.p5_validation_claim_gate import build_p5_validation_claim_gate_result
from mica.agentic.p6_proactive_proposal_gate import build_p6_proactive_proposal_gate_result

async def quetzal_evaluate(kernel, args: Dict[str, Any], envelope) -> Dict[str, Any]:
    model_ref = str(args.get("model_ref") or "").strip()
    payload_in = args.get("payload_in") or {}
    budget_ceiling_usd = float(args.get("budget_ceiling_usd") or 1.0)
    estimated_usd = float(args.get("estimated_usd") or 0.05)

    registry = get_default_model_registry()
    try:
        rev = registry.resolve(model_ref)
        revision_ref = rev.revision_ref
    except ModelNotRegistered:
        revision_ref = None

    ctx = InvokeContext(
        model_ref=model_ref,
        model_revision_ref=revision_ref,
        workspace_id=envelope.workspace_id,
        input_valid=bool(payload_in),
        estimated_usd=estimated_usd,
        budget_ceiling_usd=budget_ceiling_usd,
    )
    gate = QuetzalGate()
    verdict = gate.evaluate(ctx)

    return {
        "summary": f"quetzal.evaluate -> {verdict.decision}",
        "result": {
            "gate_name": verdict.gate_name,
            "decision": verdict.decision,
            "reason_codes": list(verdict.reason_codes),
            "max_allowed_tier": verdict.max_allowed_tier,
        },
        "state_after": {},
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [],
        "usd": 0.0,
        "tool_calls": 1,
    }
async def quetzal_validation_claim_gate(kernel, args: Dict[str, Any], envelope) -> Dict[str, Any]:
    del kernel, envelope
    from mica.agentic.command_kernel import _KernelBlocked
    from mica.agentic.commands.protocol_commands import _load_p5_intake_packet, _p5_common_result

    packet, path = _load_p5_intake_packet(args)
    raw_claim = args.get("claim") or args.get("claim_payload")
    if raw_claim is None:
        raw_claim = {
            "claim_ref": args.get("claim_ref"),
            "subject_ref": args.get("subject_ref"),
            "predicate": args.get("predicate"),
            "object_ref": args.get("object_ref"),
            "claim_text": args.get("claim_text"),
            "current_tier": args.get("current_tier") or "screening_signal",
            "requested_tier": args.get("requested_tier") or "cg_supported",
            "proposer_agent": args.get("proposer_agent"),
            "source_surface": args.get("source_surface"),
            "evidence_refs": args.get("evidence_refs") or [],
            "artifact_refs": args.get("artifact_refs") or [],
            "receipt_refs": args.get("receipt_refs") or [],
        }
    if isinstance(raw_claim, str) and raw_claim.strip():
        try:
            raw_claim = json.loads(raw_claim)
        except json.JSONDecodeError as exc:
            raise _KernelBlocked(
                code="invalid_claim_payload_json",
                message="quetzal.validation_claim_gate claim payload string must be valid JSON.",
                details={"error": str(exc), "claim_preview": raw_claim[:120]},
            ) from exc
    if not isinstance(raw_claim, Mapping):
        raise _KernelBlocked(
            code="invalid_claim_payload",
            message="quetzal.validation_claim_gate requires a claim object or claim_* arguments.",
            details={"claim_payload_type": type(raw_claim).__name__},
        )
    try:
        proposal = build_p5_validation_claim_gate_result(packet, claim_payload=raw_claim)
    except ValidationError as exc:
        raise _KernelBlocked(
            code="invalid_claim_payload",
            message="quetzal.validation_claim_gate claim payload failed schema validation.",
            details={"errors": exc.errors()},
        ) from exc
    common = _p5_common_result(packet, path)
    result = {
        **common,
        "p5_packet_status": common.get("status"),
        **proposal.model_dump(mode="json"),
        "proposal_authority": "p5_validation_claim_gate",
    }
    return {
        "summary": (
            f"quetzal.validation_claim_gate {result['p5_id']}: "
            f"status={proposal.status} requested_tier={proposal.claim.requested_tier} "
            f"decision={proposal.receipt.decision}"
        ),
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "p5_status": result["p5_packet_status"],
            "claim_ref": proposal.claim.claim_ref,
            "validation_claim_gate_status": proposal.status,
            "requested_tier": proposal.claim.requested_tier,
            "max_allowed_tier": proposal.receipt.max_allowed_tier,
            "quetzal_validation_claim_gate_receipt_ref": proposal.receipt.receipt_ref,
            "claim_promotion_performed": False,
            "graph_write_performed": False,
            "command_kernel_validation_claim_gate": True,
        },
        "artifact_refs": list(proposal.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"], proposal.receipt.receipt_ref, *proposal.evidence_refs],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def quetzal_proactive_proposal_gate(kernel, args: Dict[str, Any], envelope) -> Dict[str, Any]:
    del kernel, envelope
    from mica.agentic.command_kernel import _KernelBlocked
    from mica.agentic.commands.protocol_commands import _load_p6_intake_packet, _p6_common_result

    packet, path = _load_p6_intake_packet(args)
    raw_proposal = args.get("proposal") or args.get("proposal_payload")
    if raw_proposal is None:
        raw_proposal = {
            "proposal_ref": args.get("proposal_ref"),
            "artifact_ref": args.get("artifact_ref"),
            "p6_id": args.get("proposal_p6_id") or args.get("p6_id"),
            "trigger_family": args.get("trigger_family"),
            "source_event_refs": args.get("source_event_refs") or [],
            "source_receipt_refs": args.get("source_receipt_refs") or [],
            "target_refs": args.get("target_refs") or [],
            "reason_codes": args.get("reason_codes") or [],
            "priority": args.get("priority") or "normal",
            "proposed_protocol_ref": args.get("proposed_protocol_ref"),
            "proposed_episode_ref": args.get("proposed_episode_ref"),
            "proposed_action_kind": args.get("proposed_action_kind") or "protocol_request",
            "quetzal_policy_ref": args.get("quetzal_policy_ref") or "policy://quetzal/p6/proactive-proposal-gate",
            "idempotency_key": args.get("idempotency_key") or "",
        }
    if isinstance(raw_proposal, str) and raw_proposal.strip():
        try:
            raw_proposal = json.loads(raw_proposal)
        except json.JSONDecodeError as exc:
            raise _KernelBlocked(
                code="invalid_proposal_payload_json",
                message="quetzal.proactive_proposal_gate proposal payload string must be valid JSON.",
                details={"error": str(exc), "proposal_preview": raw_proposal[:120]},
            ) from exc
    if not isinstance(raw_proposal, Mapping):
        raise _KernelBlocked(
            code="invalid_proposal_payload",
            message="quetzal.proactive_proposal_gate requires a proposal object or proposal_* arguments.",
            details={"proposal_payload_type": type(raw_proposal).__name__},
        )

    proposal_receipt_ref = str(
        args.get("proposal_receipt_ref")
        or args.get("trigger_evaluation_receipt_ref")
        or ""
    ).strip() or None
    gate_result = build_p6_proactive_proposal_gate_result(
        packet,
        proposal_payload=raw_proposal,
        proposal_receipt_ref=proposal_receipt_ref,
    )
    common = _p6_common_result(packet, path)
    result = {
        **common,
        "p6_packet_status": common.get("status"),
        **gate_result.model_dump(mode="json"),
        "proposal_gate_authority": "quetzal_proactive_proposal_gate",
    }
    return {
        "summary": (
            f"quetzal.proactive_proposal_gate {result['p6_id']}: "
            f"status={gate_result.status} decision={gate_result.receipt.decision} "
            f"max_allowed_action={gate_result.receipt.max_allowed_action}"
        ),
        "result": result,
        "state_after": {
            "p6_id": result["p6_id"],
            "p6_status": result["p6_packet_status"],
            "proposal_ref": gate_result.receipt.proposal_ref,
            "proactive_proposal_gate_status": gate_result.status,
            "quetzal_proactive_proposal_gate_decision": gate_result.receipt.decision,
            "quetzal_proactive_proposal_gate_receipt_ref": gate_result.receipt.receipt_ref,
            "max_allowed_action": gate_result.receipt.max_allowed_action,
            "protocol_request_created": False,
            "episode_request_created": False,
            "protocol_run_created": False,
            "provider_job_created": False,
            "claim_promotion_performed": False,
            "graph_write_performed": False,
            "command_kernel_proactive_proposal_gate": True,
        },
        "artifact_refs": list(gate_result.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [
            result["p6_intake_packet_ref"],
            gate_result.receipt.receipt_ref,
            *gate_result.evidence_refs,
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }
