from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mica.protocol_drafts import (
    ProtocolToolPlan,
    ProtocolToolPlanStep,
    build_protocol_executor_request,
    compile_tool_plan_to_protocol_jsonld,
)
from mica.drivers.execution.protocol_executor_registry import protocol_node_has_executor_binding
from mica_q.protocol_runtime import get_protocol_runtime_ledger
from mica_q.protocol_jsonld_validator import derive_protocol_execution_frontier, validate_protocol_jsonld
from mica.agentic.p5_cg_validation_contracts import build_p5_cg_validation_proposal
from mica.agentic.p5_citation_consolidation import build_p5_citation_consolidation
from mica.agentic.p5_ese_cg_contracts import build_p5_ese_cg_extraction_proposal
from mica.agentic.p5_proteome_scale_readiness import build_p5_proteome_scale_readiness
from mica.agentic.p6_live_debate_artifacts import build_p6_live_debate_artifacts
from mica.agentic.p6_msrp_self_improvement_design import build_p6_msrp_self_improvement_design
from mica.agentic.p6_proposal_projection import build_p6_proposal_projection
from mica.agentic.p6_scheduled_review_retry import build_p6_scheduled_reviews
from mica.agentic.post_p6_scheduler_outbox import (
    activate_post_p6_worker_handoff,
    claim_post_p6_scheduler_outbox_entry,
    inspect_post_p6_handoff_binding,
    inspect_post_p6_handoff_status,
    inspect_post_p6_handoff_retry_status,
    list_post_p6_handoff_deadletters,
    persist_post_p6_scheduled_review_outbox,
    project_post_p6_worker_mudo_lineage,
    record_post_p6_worker_retry_transition,
)


_COMMAND_CONTROL_KEYS = {
    "protocol_jsonld",
    "protocol_json",
    "protocol_draft",
    "protocol",
    "payload",
    "protocol_path",
    "path",
    "tool_plan",
    "protocol_plan",
    "steps",
    "protocol_id",
    "protocol_name",
    "name",
    "goal",
    "node_receipts",
    "prepare_executor_request",
}


def _envelope_user_id(envelope: Any) -> str:
    identity = dict(getattr(envelope, "request_identity", {}) or {})
    for key in ("user_id", "sub", "subject", "owner_user_id"):
        value = str(identity.get(key) or "").strip()
        if value:
            return value
    return ""


def _status_request_user_id(args: Mapping[str, Any], envelope: Any) -> str:
    for key in ("owner_user_id", "user_id"):
        value = str(args.get(key) or "").strip()
        if value:
            return value
    return _envelope_user_id(envelope)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _step_dependencies(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    deps: list[str] = []
    for item in value:
        cleaned = str(item or "").strip()
        if cleaned:
            deps.append(cleaned)
    return deps


def _stable_tool_plan_identity(
    *,
    protocol_id: str,
    protocol_name: str,
    goal: str,
    steps_payload: list[Mapping[str, Any]],
) -> tuple[str, str]:
    resolved_name = protocol_name or goal or "Agentic promoted workflow"
    if protocol_id:
        return protocol_id, resolved_name
    digest_seed = json.dumps(
        {
            "protocol_name": resolved_name,
            "goal": goal,
            "steps": steps_payload,
        },
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(digest_seed).hexdigest()[:12]
    return f"agentic-protocol-{digest}", resolved_name


def _compile_tool_plan_payload(args: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_tool_plan = args.get("tool_plan") or args.get("protocol_plan")
    raw_steps = args.get("steps")
    plan_payload: dict[str, Any] | None = dict(raw_tool_plan) if isinstance(raw_tool_plan, Mapping) else None
    if plan_payload is None and isinstance(raw_steps, list):
        plan_payload = {
            "id": args.get("protocol_id"),
            "name": args.get("protocol_name") or args.get("name"),
            "goal": args.get("goal"),
            "steps": raw_steps,
        }
    if plan_payload is None:
        return None

    raw_steps_payload = plan_payload.get("steps")
    if not isinstance(raw_steps_payload, list) or not raw_steps_payload:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_tool_plan",
            message="tool_plan requires a non-empty steps list.",
        )

    normalized_step_payloads: list[Mapping[str, Any]] = []
    plan_steps: list[ProtocolToolPlanStep] = []
    for index, raw_step in enumerate(raw_steps_payload, start=1):
        if not isinstance(raw_step, Mapping):
            from mica.agentic.command_kernel import _KernelBlocked

            raise _KernelBlocked(
                code="invalid_tool_plan_step",
                message=f"tool_plan step {index} must be an object.",
            )
        step_payload = dict(raw_step)
        normalized_step_payloads.append(step_payload)
        step_id = str(
            step_payload.get("id")
            or step_payload.get("step_id")
            or f"step-{index:03d}"
        ).strip()
        tool_name = str(
            step_payload.get("tool_name")
            or step_payload.get("command_name")
            or step_payload.get("tool")
            or step_payload.get("name")
            or ""
        ).strip()
        if not tool_name:
            from mica.agentic.command_kernel import _KernelBlocked

            raise _KernelBlocked(
                code="missing_tool_plan_tool_name",
                message=f"tool_plan step {step_id or index} requires tool_name.",
            )
        step_params = (
            dict(step_payload.get("params"))
            if isinstance(step_payload.get("params"), Mapping)
            else dict(step_payload.get("inputs"))
            if isinstance(step_payload.get("inputs"), Mapping)
            else dict(step_payload.get("arguments"))
            if isinstance(step_payload.get("arguments"), Mapping)
            else {}
        )
        plan_steps.append(
            ProtocolToolPlanStep(
                id=step_id,
                tool_name=tool_name,
                params=step_params,
                dependencies=_step_dependencies(
                    step_payload.get("dependencies") or step_payload.get("depends_on")
                ),
                objective=str(
                    step_payload.get("objective")
                    or step_payload.get("label")
                    or step_payload.get("description")
                    or f"Execute {tool_name}"
                ).strip(),
                node_kind=str(step_payload.get("node_kind") or step_payload.get("kind") or "tool").strip() or "tool",
                executor_surface=str(step_payload.get("executor_surface") or "").strip(),
                executor_id=str(step_payload.get("executor_id") or "").strip(),
                expected_outputs=dict(step_payload.get("expected_outputs") or {"artifacts": []}),
                evidence_requirements=list(step_payload.get("evidence_requirements") or ["node_receipt"]),
                policies=dict(step_payload.get("policies") or {}),
                failure_policy=str(step_payload.get("failure_policy") or "halt").strip() or "halt",
                metadata=dict(step_payload.get("metadata") or {}),
            )
        )

    resolved_protocol_id, resolved_protocol_name = _stable_tool_plan_identity(
        protocol_id=str(plan_payload.get("id") or plan_payload.get("protocol_id") or "").strip(),
        protocol_name=str(plan_payload.get("name") or plan_payload.get("protocol_name") or "").strip(),
        goal=str(plan_payload.get("goal") or "").strip(),
        steps_payload=normalized_step_payloads,
    )
    plan = ProtocolToolPlan(
        id=resolved_protocol_id,
        name=resolved_protocol_name,
        description=str(plan_payload.get("description") or "").strip(),
        goal=str(plan_payload.get("goal") or "").strip(),
        session_id=str(plan_payload.get("session_id") or "").strip(),
        owner_lab=str(plan_payload.get("owner_lab") or "Agentic Control Plane").strip() or "Agentic Control Plane",
        execution_mode=str(plan_payload.get("execution_mode") or "development").strip() or "development",
        risk_profile=str(plan_payload.get("risk_profile") or "medium").strip() or "medium",
        max_usd=float(plan_payload.get("max_usd") or 5.0),
        max_wall_clock_s=int(plan_payload.get("max_wall_clock_s") or 600),
        approval_mode=str(plan_payload.get("approval_mode") or "auto").strip() or "auto",
        required_approvers=[str(item).strip() for item in (plan_payload.get("required_approvers") or []) if str(item).strip()],
        protected_surfaces=[str(item).strip() for item in (plan_payload.get("protected_surfaces") or []) if str(item).strip()],
        ledger_mode=str(plan_payload.get("ledger_mode") or "protocol_and_node_receipts").strip() or "protocol_and_node_receipts",
        receipt_schema=str(plan_payload.get("receipt_schema") or "mica.receipts.node.v1").strip() or "mica.receipts.node.v1",
        steps=plan_steps,
        metadata=dict(plan_payload.get("metadata") or {}),
    )
    compiled = compile_tool_plan_to_protocol_jsonld(plan)
    return compiled.model_dump(mode="json", by_alias=True)


def _load_protocol_payload(args: Mapping[str, Any]) -> dict[str, Any]:
    inline_payload = (
        args.get("protocol_jsonld")
        or args.get("protocol_json")
        or args.get("protocol_draft")
        or args.get("protocol")
        or args.get("payload")
    )
    if isinstance(inline_payload, Mapping):
        return dict(inline_payload)
    compiled_tool_plan = _compile_tool_plan_payload(args)
    if compiled_tool_plan is not None:
        return compiled_tool_plan
    if isinstance(args.get("protocol_id"), str) and isinstance(args.get("nodes"), list):
        return {key: value for key, value in args.items() if key not in _COMMAND_CONTROL_KEYS}

    protocol_path = str(args.get("protocol_path") or args.get("path") or "").strip()
    if not protocol_path:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="missing_protocol_payload",
            message="protocol.validate requires protocol_jsonld/protocol_json/protocol_draft or protocol_path.",
        )

    path = Path(protocol_path)
    if not path.is_absolute():
        path = _repo_root() / path
    resolved = path.resolve()
    root = _repo_root().resolve()
    if root not in (resolved, *resolved.parents):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="protocol_path_outside_workspace",
            message="protocol.validate only reads protocol files inside the workspace.",
            details={"protocol_path": str(resolved)},
        )
    try:
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="protocol_file_not_found",
            message=f"Protocol file not found: {resolved}",
            details={"protocol_path": str(resolved)},
        ) from exc
    except json.JSONDecodeError as exc:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="protocol_json_parse_failed",
            message=f"Protocol file is not valid JSON: {resolved}",
            details={"protocol_path": str(resolved), "error": str(exc)},
        ) from exc
    if not isinstance(loaded, dict):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_protocol_payload",
            message="protocol.validate requires a JSON object protocol payload.",
        )
    return loaded


def _protocol_id_from_args(args: Mapping[str, Any]) -> str:
    protocol_id = str(args.get("protocol_id") or args.get("run_id") or "").strip()
    if protocol_id:
        return protocol_id
    from mica.agentic.command_kernel import _KernelBlocked

    raise _KernelBlocked(
        code="missing_protocol_id",
        message="Protocol status commands require protocol_id or run_id.",
    )


def _protocol_status_query(args: Mapping[str, Any]) -> dict[str, str]:
    protocol_id = str(args.get("protocol_id") or "").strip()
    protocol_run_id = str(args.get("protocol_run_id") or args.get("run_id") or "").strip()
    job_id = str(args.get("job_id") or "").strip()
    if protocol_id or protocol_run_id or job_id:
        return {
            "protocol_id": protocol_id,
            "protocol_run_id": protocol_run_id,
            "job_id": job_id,
        }
    from mica.agentic.command_kernel import _KernelBlocked

    raise _KernelBlocked(
        code="missing_protocol_id",
        message="Protocol status commands require protocol_id, protocol_run_id/run_id, or job_id.",
    )


def _resolve_workspace_file(path_value: str, *, blocker_context: str) -> Path:
    path = Path(str(path_value or "").strip())
    if not path:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code=f"missing_{blocker_context}_path",
            message=f"{blocker_context} requires a workspace-local path.",
        )
    if not path.is_absolute():
        path = _repo_root() / path
    resolved = path.resolve()
    root = _repo_root().resolve()
    if root not in (resolved, *resolved.parents):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code=f"{blocker_context}_path_outside_workspace",
            message=f"{blocker_context} only reads files inside the workspace.",
            details={f"{blocker_context}_path": str(resolved)},
        )
    return resolved


def _load_json_file(path: Path, *, blocker_context: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code=f"{blocker_context}_file_not_found",
            message=f"{blocker_context} file not found: {path}",
            details={f"{blocker_context}_path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code=f"{blocker_context}_json_parse_failed",
            message=f"{blocker_context} file is not valid JSON: {path}",
            details={f"{blocker_context}_path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code=f"invalid_{blocker_context}",
            message=f"{blocker_context} requires a JSON object.",
        )
    return payload


def _find_protocol_closure_packet(protocol_id: str) -> tuple[dict[str, Any], Path] | None:
    p4_evidence_root = _repo_root() / ".mica" / "programs" / "PROYECTO_TOLOMEO" / "evidence" / "p4"
    if not p4_evidence_root.exists():
        return None
    for path in sorted(p4_evidence_root.glob("P4_CLOSURE_PACKET_*.json"), reverse=True):
        payload = _load_json_file(path, blocker_context="closure_packet")
        for proof in payload.get("proofs") or []:
            if isinstance(proof, Mapping) and str(proof.get("protocol_id") or "").strip() == protocol_id:
                return payload, path
    return None


def _load_protocol_closure_packet(
    protocol_id: str,
    args: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    explicit_path = str(args.get("closure_packet_path") or args.get("evidence_packet_path") or "").strip()
    if explicit_path:
        path = _resolve_workspace_file(explicit_path, blocker_context="closure_packet")
        payload = _load_json_file(path, blocker_context="closure_packet")
    else:
        discovered = _find_protocol_closure_packet(protocol_id)
        if discovered is None:
            from mica.agentic.command_kernel import _KernelBlocked

            raise _KernelBlocked(
                code="protocol_status_unavailable",
                message=f"unknown protocol_id {protocol_id!r}",
                details={"protocol_id": protocol_id, "durable_packet_lookup": "not_found"},
            )
        payload, path = discovered

    matching_proofs = [
        proof
        for proof in payload.get("proofs") or []
        if isinstance(proof, Mapping) and str(proof.get("protocol_id") or "").strip() == protocol_id
    ]
    if not matching_proofs:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="closure_packet_protocol_mismatch",
            message=f"Closure packet does not contain protocol_id {protocol_id!r}.",
            details={"protocol_id": protocol_id, "closure_packet_path": str(path)},
        )
    return payload, path


def _status_from_closure_packet(protocol_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    packet, path = _load_protocol_closure_packet(protocol_id, args)
    proofs = [
        proof
        for proof in packet.get("proofs") or []
        if isinstance(proof, Mapping) and str(proof.get("protocol_id") or "").strip() == protocol_id
    ]
    proof = dict(proofs[0])
    receipt_refs = dict(proof.get("receipt_refs") or {})
    node_receipts: list[dict[str, Any]] = []
    node_receipt_ref = str(receipt_refs.get("node_receipt") or "").strip()
    if node_receipt_ref:
        node_receipt_path = _resolve_workspace_file(node_receipt_ref, blocker_context="node_receipt")
        node_receipts.append(_load_json_file(node_receipt_path, blocker_context="node_receipt"))

    run_receipts: list[dict[str, Any]] = []
    run_receipt_ref = str(receipt_refs.get("run_receipt") or "").strip()
    if run_receipt_ref:
        run_receipt_path = _resolve_workspace_file(run_receipt_ref, blocker_context="run_receipt")
        run_receipts.append(_load_json_file(run_receipt_path, blocker_context="run_receipt"))

    artifact_refs = [
        dict(item)
        for item in proof.get("artifact_refs") or []
        if isinstance(item, Mapping)
    ]
    typed_blockers = [
        dict(item)
        for item in packet.get("typed_blockers") or []
        if isinstance(item, Mapping)
    ]
    graph_run_status = "completed" if str(proof.get("status") or "").lower() == "passed" else "partial"
    return {
        "protocol_id": protocol_id,
        "protocol_version": "unknown",
        "session_id": "",
        "owner_lab": "",
        "document_type": "closure_packet_projection",
        "frontier": {
            "completed_node_ids": [receipt.get("node_id") for receipt in node_receipts if receipt.get("node_id")],
            "ready_node_ids": [],
            "blocked_node_ids": [],
            "receipt_count": len(node_receipts),
        },
        "ready_nodes": [],
        "blocked_nodes": [],
        "node_receipts": node_receipts,
        "run_receipts": run_receipts,
        "executor_request": None,
        "protocol_document": {},
        "unified_runtime": {
            "projection_only": True,
            "projection_authority": "closure_packet",
            "closure_packet_ref": str(path.relative_to(_repo_root())).replace("\\", "/"),
            "graph_run_status": graph_run_status,
            "status": packet.get("status"),
            "closed": bool(packet.get("closed")),
            "typed_blockers": typed_blockers,
            "artifact_refs": artifact_refs,
            "receipt_refs": receipt_refs,
            "mudo": dict(proof.get("mudo") or {}),
            "provider_evidence_refs": list(proof.get("provider_evidence_refs") or []),
            "claim_policy": dict(packet.get("claim_policy") or {}),
        },
        "is_complete": graph_run_status == "completed",
        "projection_authority": "closure_packet",
        "closure_packet_ref": str(path.relative_to(_repo_root())).replace("\\", "/"),
    }


def _read_protocol_status(protocol_id: str) -> dict[str, Any]:
    try:
        return get_protocol_runtime_ledger().protocol_status(protocol_id)
    except ValueError as exc:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="protocol_status_unavailable",
            message=str(exc),
            details={"protocol_id": protocol_id},
        ) from exc


def _find_live_protocol_job(query: Mapping[str, str]) -> Any | None:
    try:
        from mica.api_v1 import main as api_main
    except Exception:
        return None

    jobs = getattr(api_main, "_agentic_jobs", None)
    if not isinstance(jobs, dict) or not jobs:
        return None

    job_id = str(query.get("job_id") or "").strip()
    protocol_run_id = str(query.get("protocol_run_id") or "").strip()
    protocol_id = str(query.get("protocol_id") or "").strip()

    if job_id:
        job = jobs.get(job_id)
        if job is not None:
            return job

    exact_run_match = None
    exact_protocol_match = None
    fallback_match = None

    for job in jobs.values():
        metadata = dict(getattr(job, "request_metadata", {}) or {})
        result = dict(getattr(job, "result", {}) or {})
        metadata_protocol_run_id = str(metadata.get("protocol_run_id") or result.get("protocol_run_id") or "").strip()
        metadata_protocol_id = str(metadata.get("protocol_id") or result.get("protocol_id") or "").strip()

        if protocol_run_id and metadata_protocol_run_id == protocol_run_id:
            exact_run_match = job
            break
        if protocol_id and metadata_protocol_id == protocol_id:
            exact_protocol_match = job
        elif fallback_match is None and (metadata_protocol_run_id or metadata_protocol_id):
            fallback_match = job

    if exact_run_match is not None:
        return exact_run_match
    if exact_protocol_match is not None:
        return exact_protocol_match
    return fallback_match if (job_id or protocol_run_id or protocol_id) else None


def _live_agentic_job_projection(
    query: Mapping[str, str],
    *,
    metadata: Mapping[str, Any],
    result: Mapping[str, Any],
    raw_job_status: Any,
    job_id: str,
) -> dict[str, Any]:
    run_receipt = result.get("run_receipt")
    run_receipts = [dict(run_receipt)] if isinstance(run_receipt, Mapping) else []
    node_receipts = [
        dict(receipt)
        for receipt in result.get("node_receipts") or []
        if isinstance(receipt, Mapping)
    ]
    artifacts = result.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    artifact_refs = list(artifacts.get("artifact_refs") or [])
    evidence_refs = list(artifacts.get("evidence_refs") or [])

    protocol_id = str(
        query.get("protocol_id")
        or metadata.get("protocol_id")
        or result.get("protocol_id")
        or ""
    ).strip()
    protocol_run_id = str(
        query.get("protocol_run_id")
        or query.get("run_id")
        or metadata.get("protocol_run_id")
        or result.get("protocol_run_id")
        or ""
    ).strip()

    completed_node_ids = [
        str(receipt.get("node_id") or "").strip()
        for receipt in node_receipts
        if str(receipt.get("node_id") or "").strip()
    ]
    if hasattr(raw_job_status, "value"):
        raw_job_status = raw_job_status.value
    graph_run_status = str(raw_job_status or "").strip().lower() or "unknown"
    if graph_run_status == "done":
        graph_run_status = str((run_receipt or {}).get("status") or "completed").strip().lower()
    elif graph_run_status == "error":
        graph_run_status = "failed"

    frontier = {
        "completed_node_ids": completed_node_ids,
        "ready_node_ids": [],
        "blocked_node_ids": [],
        "receipt_count": len(node_receipts),
    }

    return {
        "protocol_id": protocol_id or protocol_run_id or job_id,
        "protocol_version": str(metadata.get("protocol_version") or "").strip() or "unknown",
        "session_id": str(metadata.get("session_id") or result.get("session_id") or "").strip(),
        "owner_lab": "",
        "document_type": "live_agentic_job_projection",
        "frontier": frontier,
        "ready_nodes": [],
        "blocked_nodes": [],
        "node_receipts": node_receipts,
        "run_receipts": run_receipts,
        "executor_request": None,
        "protocol_document": {},
        "unified_runtime": {
            "projection_only": True,
            "projection_authority": "live_agentic_job",
            "graph_run_status": graph_run_status,
            "job_id": job_id,
            "protocol_run_id": protocol_run_id,
            "status": graph_run_status,
            "agentic_job_status": str(raw_job_status or "").strip(),
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "receipt_refs": {
                "agentic_job": f"/api/v1/agentic/jobs/{job_id}" if job_id else "",
            },
            "request_metadata": dict(metadata or {}),
        },
        "is_complete": graph_run_status in {"completed", "done"},
        "projection_authority": "live_agentic_job",
        "closure_packet_ref": None,
    }


def _status_from_live_agentic_job(query: Mapping[str, str]) -> dict[str, Any] | None:
    job = _find_live_protocol_job(query)
    if job is None:
        return None

    metadata = dict(getattr(job, "request_metadata", {}) or {})
    result = dict(getattr(job, "result", {}) or {})
    job_id = str(query.get("job_id") or getattr(job, "job_id", "") or "").strip()
    raw_job_status = getattr(job, "status", "")
    return _live_agentic_job_projection(
        query,
        metadata=metadata,
        result=result,
        raw_job_status=raw_job_status,
        job_id=job_id,
    )


def _candidate_backend_urls() -> list[str]:
    candidates = [
        str(os.getenv("MICA_BACKEND_URL") or "").strip(),
        "http://127.0.0.1:8131",
        "http://localhost:8080",
    ]
    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        cleaned = candidate.rstrip("/")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        resolved.append(cleaned)
    return resolved


def _http_get_json(url: str, *, user_id: str, timeout_s: float = 5.0) -> Any | None:
    headers = {"X-User-Id": str(user_id or "").strip() or "agent_cli"}
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {403, 404, 422}:
            return None
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def _match_backend_job_payload(query: Mapping[str, str], jobs: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    job_id = str(query.get("job_id") or "").strip()
    protocol_run_id = str(query.get("protocol_run_id") or query.get("run_id") or "").strip()
    protocol_id = str(query.get("protocol_id") or "").strip()
    if job_id:
        for job in jobs:
            if str(job.get("job_id") or "").strip() == job_id:
                return job
    if protocol_run_id:
        for job in jobs:
            metadata = dict(job.get("request_metadata") or {})
            result = dict(job.get("result") or {})
            if str(metadata.get("protocol_run_id") or result.get("protocol_run_id") or "").strip() == protocol_run_id:
                return job
    if protocol_id:
        for job in jobs:
            metadata = dict(job.get("request_metadata") or {})
            result = dict(job.get("result") or {})
            if str(metadata.get("protocol_id") or result.get("protocol_id") or "").strip() == protocol_id:
                return job
    return None


def _status_from_backend_agentic_job(query: Mapping[str, str], *, user_id: str) -> dict[str, Any] | None:
    for base_url in _candidate_backend_urls():
        payload: Mapping[str, Any] | None = None
        job_id = str(query.get("job_id") or "").strip()
        if job_id:
            payload = _http_get_json(
                f"{base_url}/api/v1/agentic/jobs/{job_id}",
                user_id=user_id,
                timeout_s=30.0,
            )
        if payload is None and (query.get("protocol_run_id") or query.get("run_id") or query.get("protocol_id")):
            jobs_payload = _http_get_json(
                f"{base_url}/api/v1/agentic/jobs",
                user_id=user_id,
                timeout_s=15.0,
            )
            if isinstance(jobs_payload, list):
                payload = _match_backend_job_payload(
                    query,
                    [dict(job) for job in jobs_payload if isinstance(job, Mapping)],
                )
        if payload is None:
            continue
        metadata = dict(payload.get("request_metadata") or {})
        result = dict(payload.get("result") or {})
        raw_job_status = payload.get("status")
        resolved_job_id = str(payload.get("job_id") or job_id or "").strip()
        return _live_agentic_job_projection(
            query,
            metadata=metadata,
            result=result,
            raw_job_status=raw_job_status,
            job_id=resolved_job_id,
        )
    return None


def _resolve_protocol_status(args: Mapping[str, Any], *, user_id: str = "") -> dict[str, Any]:
    query = _protocol_status_query(args)
    protocol_id = str(query.get("protocol_id") or "").strip()
    if protocol_id:
        try:
            return _read_protocol_status(protocol_id)
        except Exception as exc:
            if exc.__class__.__name__ != "_KernelBlocked" or getattr(exc, "code", "") != "protocol_status_unavailable":
                raise

    live_retry_count = 1
    live_retry_delay_s = 0.0
    if query.get("job_id") or query.get("protocol_run_id"):
        live_retry_count = 6
        live_retry_delay_s = 0.5

    live_status = None
    for attempt in range(live_retry_count):
        live_status = _status_from_live_agentic_job(query)
        if live_status is not None:
            return live_status
        if attempt + 1 < live_retry_count and live_retry_delay_s > 0:
            time.sleep(live_retry_delay_s)

    backend_live_status = _status_from_backend_agentic_job(query, user_id=user_id)
    if backend_live_status is not None:
        return backend_live_status

    if protocol_id:
        return _status_from_closure_packet(protocol_id, args)

    from mica.agentic.command_kernel import _KernelBlocked

    raise _KernelBlocked(
        code="protocol_status_unavailable",
        message="unknown live protocol run/job and no durable protocol_id was provided",
        details=query,
    )


def _protocol_scope_from_request(args: Mapping[str, Any], envelope: Any) -> dict[str, str]:
    return {
        "owner_user_id": str(args.get("owner_user_id") or _envelope_user_id(envelope) or "").strip(),
        "workspace_id": str(args.get("workspace_id") or getattr(envelope, "workspace_id", "") or "").strip(),
        "study_id": str(args.get("study_id") or getattr(envelope, "study_id", "") or "").strip(),
    }


def _scope_matches(metadata: Mapping[str, Any], supplied_scope: Mapping[str, str]) -> bool:
    expected = {
        "owner_user_id": str(metadata.get("owner_user_id") or metadata.get("user_id") or "").strip(),
        "workspace_id": str(metadata.get("workspace_id") or "").strip(),
        "study_id": str(metadata.get("study_id") or "").strip(),
    }
    for key, supplied_value in supplied_scope.items():
        if not supplied_value:
            continue
        if not expected.get(key) or expected[key] != supplied_value:
            return False
    return True


def _list_live_protocol_job_projections(supplied_scope: Mapping[str, str]) -> list[dict[str, Any]]:
    try:
        from mica.api_v1 import main as api_main
    except Exception:
        return []

    jobs = getattr(api_main, "_agentic_jobs", None)
    if not isinstance(jobs, dict) or not jobs:
        return []

    items: list[dict[str, Any]] = []
    for job in jobs.values():
        metadata = dict(getattr(job, "request_metadata", {}) or {})
        result = dict(getattr(job, "result", {}) or {})
        if not _scope_matches(metadata, supplied_scope):
            continue

        raw_job_status = getattr(job, "status", "")
        if hasattr(raw_job_status, "value"):
            raw_job_status = raw_job_status.value
        graph_run_status = str(raw_job_status or "").strip().lower() or "unknown"
        if graph_run_status == "done":
            graph_run_status = str((result.get("run_receipt") or {}).get("status") or "completed").strip().lower()
        elif graph_run_status == "error":
            graph_run_status = "failed"

        node_receipts = [
            dict(receipt)
            for receipt in result.get("node_receipts") or []
            if isinstance(receipt, Mapping)
        ]
        protocol_id = str(
            metadata.get("protocol_id")
            or result.get("protocol_id")
            or metadata.get("protocol_run_id")
            or result.get("protocol_run_id")
            or getattr(job, "job_id", "")
            or ""
        ).strip()
        if not protocol_id:
            continue
        items.append(
            {
                "protocol_id": protocol_id,
                "protocol_version": str(metadata.get("protocol_version") or "").strip() or "unknown",
                "session_id": str(metadata.get("session_id") or result.get("session_id") or "").strip(),
                "owner_lab": "",
                "document_type": "live_agentic_job_projection",
                "graph_run_status": graph_run_status,
                "is_complete": graph_run_status in {"completed", "done"},
                "ready_node_count": 0,
                "blocked_node_count": 0,
                "completed_node_count": len(
                    [
                        str(receipt.get("node_id") or "").strip()
                        for receipt in node_receipts
                        if str(receipt.get("node_id") or "").strip()
                    ]
                ),
                "node_receipt_count": len(node_receipts),
                "projection_authority": "live_agentic_job",
                "scope": {
                    "owner_user_id": supplied_scope.get("owner_user_id") or str(metadata.get("owner_user_id") or metadata.get("user_id") or "").strip(),
                    "workspace_id": supplied_scope.get("workspace_id") or str(metadata.get("workspace_id") or "").strip(),
                    "study_id": supplied_scope.get("study_id") or str(metadata.get("study_id") or "").strip(),
                },
            }
        )
    return items


def _find_p5_intake_packet() -> tuple[dict[str, Any], Path] | None:
    p5_evidence_root = _repo_root() / ".mica" / "programs" / "PROYECTO_TOLOMEO" / "evidence" / "p5"
    if not p5_evidence_root.exists():
        return None
    for path in sorted(p5_evidence_root.glob("P5_INTAKE_PACKET_*.json"), reverse=True):
        return _load_json_file(path, blocker_context="p5_intake_packet"), path
    return None


def _find_p6_intake_packet() -> tuple[dict[str, Any], Path] | None:
    p6_evidence_root = _repo_root() / ".mica" / "programs" / "PROYECTO_TOLOMEO" / "evidence" / "p6"
    if not p6_evidence_root.exists():
        return None
    for path in sorted(p6_evidence_root.glob("P6_INTAKE_PACKET_*.json"), reverse=True):
        return _load_json_file(path, blocker_context="p6_intake_packet"), path
    return None


def _load_p5_intake_packet(args: Mapping[str, Any]) -> tuple[dict[str, Any], Path]:
    explicit_path = str(
        args.get("p5_intake_packet_path")
        or args.get("intake_packet_path")
        or args.get("packet_path")
        or ""
    ).strip()
    if explicit_path:
        path = _resolve_workspace_file(explicit_path, blocker_context="p5_intake_packet")
        packet = _load_json_file(path, blocker_context="p5_intake_packet")
    else:
        discovered = _find_p5_intake_packet()
        if discovered is None:
            from mica.agentic.command_kernel import _KernelBlocked

            raise _KernelBlocked(
                code="p5_intake_packet_unavailable",
                message="No P5 intake packet was found under PROYECTO_TOLOMEO evidence/p5.",
                details={"lookup": ".mica/programs/PROYECTO_TOLOMEO/evidence/p5/P5_INTAKE_PACKET_*.json"},
            )
        packet, path = discovered

    schema_id = str(packet.get("schema_id") or "").strip()
    if schema_id != "mica.project_tolomeo.p5.intake_packet.v1":
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_p5_intake_packet_schema",
            message="P5 intake packet has an unexpected schema_id.",
            details={"schema_id": schema_id, "p5_intake_packet_path": str(path)},
        )

    requested_p5_id = str(args.get("p5_id") or "").strip()
    packet_p5_id = str(packet.get("p5_id") or "").strip()
    if requested_p5_id and requested_p5_id != packet_p5_id:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="p5_intake_packet_id_mismatch",
            message=f"P5 intake packet does not contain p5_id {requested_p5_id!r}.",
            details={"p5_id": requested_p5_id, "packet_p5_id": packet_p5_id, "p5_intake_packet_path": str(path)},
        )
    return packet, path


def _load_p6_intake_packet(args: Mapping[str, Any]) -> tuple[dict[str, Any], Path]:
    explicit_path = str(
        args.get("p6_intake_packet_path")
        or args.get("intake_packet_path")
        or args.get("packet_path")
        or ""
    ).strip()
    if explicit_path:
        path = _resolve_workspace_file(explicit_path, blocker_context="p6_intake_packet")
        packet = _load_json_file(path, blocker_context="p6_intake_packet")
    else:
        discovered = _find_p6_intake_packet()
        if discovered is None:
            from mica.agentic.command_kernel import _KernelBlocked

            raise _KernelBlocked(
                code="p6_intake_packet_unavailable",
                message="No P6 intake packet was found under PROYECTO_TOLOMEO evidence/p6.",
                details={"lookup": ".mica/programs/PROYECTO_TOLOMEO/evidence/p6/P6_INTAKE_PACKET_*.json"},
            )
        packet, path = discovered

    schema_id = str(packet.get("schema_id") or "").strip()
    if schema_id != "mica.project_tolomeo.p6.intake_packet.v1":
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_p6_intake_packet_schema",
            message="P6 intake packet has an unexpected schema_id.",
            details={"schema_id": schema_id, "p6_intake_packet_path": str(path)},
        )

    requested_p6_id = str(args.get("p6_id") or "").strip()
    packet_p6_id = str(packet.get("p6_id") or "").strip()
    if requested_p6_id and requested_p6_id != packet_p6_id:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="p6_intake_packet_id_mismatch",
            message=f"P6 intake packet does not contain p6_id {requested_p6_id!r}.",
            details={"p6_id": requested_p6_id, "packet_p6_id": packet_p6_id, "p6_intake_packet_path": str(path)},
        )
    return packet, path


def _packet_refs(packet: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return [dict(item) for item in packet.get(key) or [] if isinstance(item, Mapping)]


def _p5_packet_ref(path: Path) -> str:
    return str(path.relative_to(_repo_root())).replace("\\", "/")


def _p5_common_result(packet: Mapping[str, Any], path: Path) -> dict[str, Any]:
    return {
        "p5_id": packet.get("p5_id"),
        "status": packet.get("status"),
        "projection_authority": "p5_intake_packet",
        "p5_intake_packet_ref": _p5_packet_ref(path),
    }


def _p6_packet_ref(path: Path) -> str:
    return str(path.relative_to(_repo_root())).replace("\\", "/")


def _p6_common_result(packet: Mapping[str, Any], path: Path) -> dict[str, Any]:
    return {
        "p6_id": packet.get("p6_id"),
        "status": packet.get("status"),
        "projection_authority": "p6_intake_packet",
        "p6_intake_packet_ref": _p6_packet_ref(path),
    }


def _count_by_status(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


async def protocol_validate(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel
    payload = _load_protocol_payload(args)
    node_receipts = list(args.get("node_receipts") or [])
    document = validate_protocol_jsonld(payload)
    unresolved_nodes = [
        {
            "node_id": node.node_id,
            "executor_surface": node.executor_surface,
            "executor_id": node.executor_id,
        }
        for node in document.nodes
        if not protocol_node_has_executor_binding(node)
    ]
    if unresolved_nodes:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="unknown_executor_surface",
            message="protocol.validate found node executor surfaces with no ProtocolExecutor binding.",
            details={"unresolved_nodes": unresolved_nodes},
        )
    frontier = derive_protocol_execution_frontier(document, node_receipts)
    missing_durable_scope = [
        field_name
        for field_name, value in {
            "workspace_id": getattr(envelope, "workspace_id", None),
            "study_id": getattr(envelope, "study_id", None),
        }.items()
        if value in (None, "")
    ]

    result: dict[str, Any] = {
        "valid": True,
        "protocol_id": document.protocol_id,
        "protocol_version": document.version,
        "execution_mode": document.execution_mode.value,
        "risk_profile": document.risk_profile.value,
        "frontier": frontier.model_dump(mode="json"),
        "node_count": len(document.nodes),
        "edge_count": len(document.edges),
        "receipt_count": frontier.receipt_count,
        "ledger_policy": document.ledger_policy.model_dump(mode="json"),
        "runtime_backing": "local",
        "durability": "non_durable",
        "trust_state": "preview" if missing_durable_scope else "active",
        "missing_durable_scope": missing_durable_scope,
    }
    if bool(args.get("prepare_executor_request")) and frontier.ready_node_ids:
        executor_request = build_protocol_executor_request(
            document,
            frontier,
            session_id=str(envelope.session_id or document.session_id),
            prior_receipts=node_receipts,
            request_metadata={
                "command_name": "protocol.validate",
                "workspace_id": envelope.workspace_id,
                "study_id": envelope.study_id,
            },
        )
        result["executor_request"] = executor_request.model_dump(mode="json")
    return {
        "summary": (
            f"protocol.validate accepted {document.protocol_id}; "
            f"ready={len(frontier.ready_node_ids)} blocked={len(frontier.blocked_node_ids)}"
        ),
        "result": result,
        "state_after": {
            "protocol_id": document.protocol_id,
            "ready_node_ids": list(frontier.ready_node_ids),
            "blocked_node_ids": list(frontier.blocked_node_ids),
            "completed_node_ids": list(frontier.completed_node_ids),
            "receipt_count": frontier.receipt_count,
            "command_kernel_protocol_frontier": True,
        },
        "artifact_refs": [],
        "receipt_refs": [],
        "resource_refs": [],
        "evidence_refs": [],
        "usd": 0.0,
        "tool_calls": 1,
        "status": "completed_non_durable" if missing_durable_scope else "completed",
        "runtime_backing": "local",
        "durability": "non_durable",
        "trust_state": "preview" if missing_durable_scope else "active",
        "degraded_reason": (
            f"Missing durable scope for validate-only execution: {', '.join(missing_durable_scope)}"
            if missing_durable_scope
            else None
        ),
        "warnings": (
            [f"protocol.validate ran without durable scope: {', '.join(missing_durable_scope)}"]
            if missing_durable_scope
            else []
        ),
    }


async def protocol_run_status(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel
    status = _resolve_protocol_status(args, user_id=_status_request_user_id(args, envelope))
    protocol_id = str(status.get("protocol_id") or _protocol_status_query(args).get("protocol_id") or "").strip()
    frontier = dict(status.get("frontier") or {})
    unified_runtime = status.get("unified_runtime")
    if not isinstance(unified_runtime, Mapping):
        unified_runtime = {}
    graph_run_status = str(unified_runtime.get("graph_run_status") or "").strip().lower() or "unknown"
    result = {
        "protocol_id": protocol_id,
        "graph_run_status": graph_run_status,
        "is_complete": bool(status.get("is_complete")),
        "frontier": frontier,
        "ready_node_count": len(status.get("ready_nodes") or []),
        "blocked_node_count": len(status.get("blocked_nodes") or []),
        "node_receipt_count": len(status.get("node_receipts") or []),
        "run_receipt_count": len(status.get("run_receipts") or []),
        "executor_request_available": bool(status.get("executor_request")),
        "projection_authority": status.get("projection_authority") or unified_runtime.get("projection_authority"),
        "closure_packet_ref": status.get("closure_packet_ref") or unified_runtime.get("closure_packet_ref"),
        "artifact_refs": unified_runtime.get("artifact_refs") or [],
        "receipt_refs": unified_runtime.get("receipt_refs") or {},
        "mudo": unified_runtime.get("mudo") or {},
        "typed_blockers": unified_runtime.get("typed_blockers") or [],
        "claim_policy": unified_runtime.get("claim_policy") or {},
        "unified_runtime": unified_runtime,
    }
    evidence_refs = unified_runtime.get("evidence_refs") or []
    return {
        "summary": (
            f"protocol.run.status {protocol_id}: "
            f"state={graph_run_status} complete={result['is_complete']} "
            f"ready={result['ready_node_count']} blocked={result['blocked_node_count']} "
            f"artifacts={len(result['artifact_refs'])} evidence={len(evidence_refs)} "
            f"receipts={result['node_receipt_count']}"
        ),
        "result": result,
        "state_after": {
            "protocol_id": protocol_id,
            "ready_node_ids": list(frontier.get("ready_node_ids") or []),
            "blocked_node_ids": list(frontier.get("blocked_node_ids") or []),
            "completed_node_ids": list(frontier.get("completed_node_ids") or []),
            "receipt_count": int(frontier.get("receipt_count") or 0),
            "command_kernel_protocol_status": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [],
        "usd": 0.0,
        "tool_calls": 1,
        "status": graph_run_status,
    }


async def protocol_p5_status(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    slices = _packet_refs(packet, "slices")
    deferred_residuals = _packet_refs(packet, "deferred_residuals")
    proof_refs = _packet_refs(packet, "proof_refs")
    result = {
        **_p5_common_result(packet, path),
        "p4_intake": dict(packet.get("p4_intake") or {}),
        "scope": dict(packet.get("scope") or {}),
        "slice_count": len(slices),
        "slices": slices,
        "proof_refs": proof_refs,
        "artifact_refs": _packet_refs(packet, "artifact_refs"),
        "receipt_refs": _packet_refs(packet, "receipt_refs"),
        "mudo_refs": _packet_refs(packet, "mudo_refs"),
        "deferred_residuals": deferred_residuals,
        "runpod_deferred": any(
            "runpod" in str(item.get("code") or item.get("residual_id") or "").lower()
            for item in deferred_residuals
        ),
        "claim_policy": dict(packet.get("claim_policy") or {}),
        "acceptance_gates": dict(packet.get("acceptance_gates") or {}),
        "next_objective": dict(packet.get("next_objective") or {}),
    }
    return {
        "summary": (
            f"protocol.p5.status {result['p5_id']}: "
            f"status={result['status']} proofs={len(proof_refs)} residuals={len(deferred_residuals)}"
        ),
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "p5_status": result["status"],
            "p5_intake_packet_ref": result["p5_intake_packet_ref"],
            "p5_slice_count": len(slices),
            "p5_deferred_residual_count": len(deferred_residuals),
            "command_kernel_p5_status": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"]],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p6_status(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p6_intake_packet(args)
    slices = _packet_refs(packet, "slices")
    inherited_residuals = _packet_refs(packet, "inherited_residuals")
    p5_import = dict(packet.get("p5_import") or {})
    p4_import = dict(packet.get("p4_import") or {})
    readiness = dict(packet.get("proactivity_readiness") or {})
    result = {
        **_p6_common_result(packet, path),
        "phase": packet.get("phase"),
        "roadmap_anchor": dict(packet.get("roadmap_anchor") or {}),
        "scope": dict(packet.get("scope") or {}),
        "p5_import": p5_import,
        "p4_import": p4_import,
        "slice_count": len(slices),
        "slices": slices,
        "inherited_residuals": inherited_residuals,
        "runpod_deferred": any(
            "runpod" in str(item.get("code") or item.get("residual_id") or "").lower()
            for item in inherited_residuals
        ),
        "proactivity_readiness": readiness,
        "proposal_contract_status": readiness.get("proposal_contract_status"),
        "trigger_evaluator_status": readiness.get("trigger_evaluator_status"),
        "proposal_count": int(readiness.get("proposal_count") or 0),
        "protocol_runs_created": int(readiness.get("protocol_runs_created") or 0),
        "provider_jobs_created": int(readiness.get("provider_jobs_created") or 0),
        "claim_promotions_performed": int(readiness.get("claim_promotions_performed") or 0),
        "acceptance_gates": dict(packet.get("acceptance_gates") or {}),
        "next_objective": dict(packet.get("next_objective") or {}),
    }
    return {
        "summary": (
            f"protocol.p6.status {result['p6_id']}: "
            f"status={result['status']} slices={len(slices)} proposals={result['proposal_count']} "
            f"runs={result['protocol_runs_created']}"
        ),
        "result": result,
        "state_after": {
            "p6_id": result["p6_id"],
            "p6_status": result["status"],
            "p6_intake_packet_ref": result["p6_intake_packet_ref"],
            "p6_slice_count": len(slices),
            "p6_inherited_residual_count": len(inherited_residuals),
            "runpod_deferred": result["runpod_deferred"],
            "proactive_proposals_created": result["proposal_count"],
            "protocol_runs_created": result["protocol_runs_created"],
            "provider_jobs_created": result["provider_jobs_created"],
            "claim_promotions_performed": result["claim_promotions_performed"],
            "command_kernel_p6_status": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [result["p6_intake_packet_ref"]],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p6_requests_project(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel
    packet, path = _load_p6_intake_packet(args)
    command_name = "protocol.p6.requests.project"
    proposal_payload = _json_command_value(
        args.get("proposal"),
        field_name="proposal",
        command_name=command_name,
    )
    quetzal_receipt_payload = _json_command_value(
        args.get("quetzal_receipt"),
        field_name="quetzal_receipt",
        command_name=command_name,
    )
    projection = build_p6_proposal_projection(
        packet,
        proposal_payload=proposal_payload,
        proposal_receipt_ref=str(args.get("proposal_receipt_ref") or "").strip() or None,
        quetzal_receipt_payload=quetzal_receipt_payload,
        workspace_id=str(envelope.workspace_id or ""),
        study_id=str(envelope.study_id or ""),
    )
    common = _p6_common_result(packet, path)
    result = {
        **common,
        "p6_packet_status": common.get("status"),
        **projection.model_dump(mode="json"),
        "projection_authority": "p6_proposal_projection_contract",
    }


    request = projection.request
    return {
        "summary": (
            f"{command_name} {result['p6_id']}: status={projection.status} "
            f"kind={request.request_kind if request else 'none'} execution_started=false"
        ),
        "result": result,
        "state_after": {
            "p6_id": result["p6_id"],
            "p6_packet_status": result["p6_packet_status"],
            "p6_request_projection_status": projection.status,
            "projected_request_ref": request.request_ref if request else None,
            "projected_request_kind": request.request_kind if request else None,
            "proposal_projection_receipt_ref": projection.receipt.receipt_ref,
            "execution_authority": request.execution_authority if request else "none",
            "projection_status": request.projection_status if request else "blocked",
            "provider_jobs_created": 0,
            "protocol_runs_created": 0,
            "episode_runs_created": 0,
            "outbox_dispatch_performed": False,
            "claim_promotion_performed": False,
            "graph_write_performed": False,
            "execution_started": False,
            "command_kernel_p6_request_projection": True,
        },
        "artifact_refs": list(projection.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [
            result["p6_intake_packet_ref"],
            *projection.evidence_refs,
            *projection.receipt_refs,
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p6_debate_artifacts(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel
    packet, path = _load_p6_intake_packet(args)
    command_name = "protocol.p6.debate.artifacts"
    debate_outputs = _json_command_value(
        args.get("debate_outputs"),
        field_name="debate_outputs",
        command_name=command_name,
    )
    pipeline = build_p6_live_debate_artifacts(
        packet,
        debate_outputs=debate_outputs,
        workspace_id=str(envelope.workspace_id or ""),
        study_id=str(envelope.study_id or ""),
    )
    common = _p6_common_result(packet, path)
    result = {
        **common,
        "p6_packet_status": common.get("status"),
        **pipeline.model_dump(mode="json"),
        "projection_authority": "p6_live_debate_artifact_contract",
        "mudo_persistence_status": "pending_persistence",
    }
    return {
        "summary": (
            f"{command_name} {result['p6_id']}: status={pipeline.status} "
            f"artifacts={pipeline.materialized_count} duplicates={pipeline.duplicate_count}"
        ),
        "result": result,
        "state_after": {
            "p6_id": result["p6_id"],
            "p6_packet_status": result["p6_packet_status"],
            "live_debate_artifact_pipeline_status": pipeline.status,
            "debate_artifacts_materialized": pipeline.materialized_count,
            "debate_receipts_emitted": len(pipeline.receipts),
            "mudo_branch_receipts_emitted": len(pipeline.mudo_branch_receipts),
            "mudo_persistence_status": "pending_persistence",
            "contradiction_proposals_created": len(pipeline.proposal_refs),
            "claim_promotions_performed": 0,
            "graph_writes_performed": 0,
            "canonical_mudo_branch_mutations": 0,
            "execution_started": False,
            "command_kernel_p6_live_debate_artifacts": True,
        },
        "artifact_refs": list(pipeline.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [
            result["p6_intake_packet_ref"],
            *pipeline.receipt_refs,
            *pipeline.mudo_branch_receipt_refs,
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p6_reviews_schedule(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p6_intake_packet(args)
    command_name = "protocol.reviews.schedule"
    review_requests = _json_command_value(
        args.get("review_requests"),
        field_name="review_requests",
        command_name=command_name,
    )
    budget = _json_command_value(args.get("budget"), field_name="budget", command_name=command_name)
    retry_policy = _json_command_value(
        args.get("retry_policy"),
        field_name="retry_policy",
        command_name=command_name,
    )
    prior_receipts = _json_command_value(
        args.get("prior_receipts"),
        field_name="prior_receipts",
        command_name=command_name,
    )
    persist_outbox = bool(args.get("persist_outbox") or args.get("durable_outbox"))
    quetzal_gate_ref = str(args.get("quetzal_gate_ref") or "").strip()
    budget_ref = str(args.get("budget_ref") or "").strip()
    outbox_store_path = args.get("outbox_store_path")
    if review_requests is not None and not isinstance(review_requests, (Mapping, list)):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(code="invalid_review_requests", message="review_requests must be an object or array.")
    if budget is not None and not isinstance(budget, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(code="invalid_review_budget", message="budget must be an object.")
    if retry_policy is not None and not isinstance(retry_policy, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(code="invalid_review_retry_policy", message="retry_policy must be an object.")
    if prior_receipts is not None and not isinstance(prior_receipts, list):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(code="invalid_prior_review_receipts", message="prior_receipts must be an array.")

    schedule = build_p6_scheduled_reviews(
        packet,
        review_requests=review_requests,
        budget_payload=budget,
        retry_policy_payload=retry_policy,
        prior_receipts_payload=prior_receipts,
    )
    durable_outbox = None
    outbox_persistence_status = "projected_not_enqueued"
    if persist_outbox:
        durable_outbox = persist_post_p6_scheduled_review_outbox(
            schedule,
            quetzal_gate_ref=quetzal_gate_ref,
            budget_ref=budget_ref,
            store_path=outbox_store_path,
        )
        outbox_persistence_status = (
            "persisted_not_claimed"
            if durable_outbox.status in {"persisted", "partially_persisted"}
            else durable_outbox.status
        )
    common = _p6_common_result(packet, path)
    result = {
        **common,
        "p6_packet_status": common.get("status"),
        **schedule.model_dump(mode="json"),
        "projection_authority": "p6_scheduled_review_retry_contract",
        "canonical_command_name": command_name,
        "legacy_command_aliases": ["protocol.p6.reviews.schedule"],
        "outbox_persistence_status": outbox_persistence_status,
    }
    if durable_outbox is not None:
        result["durable_outbox"] = durable_outbox.model_dump(mode="json")
    return {
        "summary": (
            f"{command_name} {result['p6_id']}: status={schedule.status} "
            f"proposed={schedule.proposed_count} noop={schedule.noop_count} blocked={schedule.blocked_count} "
            f"outbox_persistence={outbox_persistence_status}"
        ),
        "result": result,
        "state_after": {
            "p6_id": result["p6_id"],
            "p6_packet_status": result["p6_packet_status"],
            "scheduled_review_status": schedule.status,
            "scheduled_review_proposal_count": schedule.proposed_count,
            "scheduled_review_noop_count": schedule.noop_count,
            "scheduled_review_blocked_count": schedule.blocked_count,
            "scheduled_review_duplicate_count": schedule.duplicate_count,
            "outbox_entries_projected": len(schedule.outbox_entries),
            "outbox_persistence_status": outbox_persistence_status,
            "durable_outbox_records_persisted": durable_outbox.persisted_count if durable_outbox is not None else 0,
            "durable_outbox_receipt_count": len(durable_outbox.receipts) if durable_outbox is not None else 0,
            "provider_jobs_created": 0,
            "protocol_runs_created": 0,
            "episode_runs_created": 0,
            "outbox_dispatch_performed": False,
            "claim_promotion_performed": False,
            "graph_write_performed": False,
            "execution_started": False,
            "command_kernel_p6_scheduled_review_retry": True,
            "command_kernel_protocol_reviews_schedule": True,
            "legacy_command_aliases": ["protocol.p6.reviews.schedule"],
        },
        "artifact_refs": list(schedule.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [
            result["p6_intake_packet_ref"],
            *schedule.receipt_refs,
            *([receipt.receipt_ref for receipt in durable_outbox.receipts] if durable_outbox is not None else []),
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_claim(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    command_name = "protocol.reviews.worker.claim"
    worker_id = str(args.get("worker_id") or "").strip()
    budget_payload = _json_command_value(
        args.get("budget_payload", args.get("budget")),
        field_name="budget_payload",
        command_name=command_name,
    )
    circuit_breaker_payload = _json_command_value(
        args.get("circuit_breaker_payload", args.get("circuit_breaker")),
        field_name="circuit_breaker_payload",
        command_name=command_name,
    )
    result = claim_post_p6_scheduler_outbox_entry(
        outbox_store_path=args.get("outbox_store_path"),
        claim_store_path=args.get("claim_store_path"),
        worker_id=worker_id,
        max_claims=int(args.get("max_claims") or 1),
        provider_job_requested=bool(args.get("provider_job_requested") or args.get("call_provider")),
        protocol_run_requested=bool(args.get("protocol_run_requested") or args.get("run_protocol")),
        episode_run_requested=bool(args.get("episode_run_requested") or args.get("run_episode")),
        claim_promotion_requested=bool(args.get("claim_promotion_requested") or args.get("promote_claim")),
        graph_write_requested=bool(args.get("graph_write_requested") or args.get("write_graph")),
        canonical_mudo_mutation_requested=bool(args.get("canonical_mudo_mutation_requested") or args.get("write_mudo")),
        execute_now=bool(args.get("execute_now") or args.get("execute_protocol")),
        enforce_budget=bool(args.get("enforce_budget")),
        budget_payload=budget_payload if isinstance(budget_payload, Mapping) else None,
        enforce_circuit_breaker=bool(args.get("enforce_circuit_breaker")),
        circuit_breaker_payload=circuit_breaker_payload if isinstance(circuit_breaker_payload, Mapping) else None,
    )
    payload = result.model_dump(mode="json")
    return {
        "summary": (
            f"{command_name}: status={result.status} claimed={result.claimed_count} "
            f"duplicates={result.duplicate_count} blocked={result.blocked_count}"
        ),
        "result": {
            **payload,
            "canonical_command_name": command_name,
            "projection_authority": "post_p6_scheduler_worker_claim_contract",
            "worker_authority": "retry_backfill_only",
        },
        "state_after": {
            "worker_claim_status": result.status,
            "worker_claimed_count": result.claimed_count,
            "worker_duplicate_count": result.duplicate_count,
            "worker_blocked_count": result.blocked_count,
            "worker_budget_blocked_count": result.budget_blocked_count,
            "worker_circuit_blocked_count": result.circuit_blocked_count,
            "handoff_count": result.handoff_count,
            "handoff_state": result.claims[0].handoff_state if result.claims else None,
            "budget_decision_refs": [decision.decision_ref for decision in result.budget_decisions],
            "circuit_decision_refs": [decision.decision_ref for decision in result.circuit_decisions],
            "provider_jobs_created": result.provider_jobs_created,
            "protocol_runs_created": result.protocol_runs_created,
            "episode_runs_created": result.episode_runs_created,
            "outbox_dispatch_performed": result.outbox_dispatch_performed,
            "claim_promotion_performed": result.claim_promotions_performed > 0,
            "graph_write_performed": result.graph_writes_performed > 0,
            "canonical_mudo_mutation_performed": result.canonical_mudo_mutations_performed > 0,
            "execution_started": result.execution_started,
            "command_kernel_protocol_reviews_worker_claim": True,
        },
        "artifact_refs": [],
        "resource_refs": [claim.handoff_ref for claim in result.claims],
        "evidence_refs": [receipt.receipt_ref for receipt in result.receipts],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_lineage_project(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    command_name = "protocol.reviews.worker.lineage.project"
    result = project_post_p6_worker_mudo_lineage(
        outbox_store_path=args.get("outbox_store_path"),
        claim_store_path=args.get("claim_store_path"),
        lineage_store_path=args.get("lineage_store_path"),
        max_events=int(args.get("max_events") or 1),
        provider_job_requested=bool(args.get("provider_job_requested") or args.get("call_provider")),
        protocol_run_requested=bool(args.get("protocol_run_requested") or args.get("run_protocol")),
        episode_run_requested=bool(args.get("episode_run_requested") or args.get("run_episode")),
        claim_promotion_requested=bool(args.get("claim_promotion_requested") or args.get("promote_claim")),
        graph_write_requested=bool(args.get("graph_write_requested") or args.get("write_graph")),
        canonical_mudo_mutation_requested=bool(args.get("canonical_mudo_mutation_requested") or args.get("write_mudo")),
        execute_now=bool(args.get("execute_now") or args.get("execute_protocol")),
    )
    payload = result.model_dump(mode="json")
    return {
        "summary": (
            f"{command_name}: status={result.status} projected={result.projected_count} "
            f"duplicates={result.duplicate_count} blocked={result.blocked_count}"
        ),
        "result": {
            **payload,
            "canonical_command_name": command_name,
            "projection_authority": "post_p6_worker_mudo_lineage_projection_contract",
        },
        "state_after": {
            "worker_mudo_lineage_status": result.status,
            "worker_mudo_lineage_projected_count": result.projected_count,
            "worker_mudo_lineage_duplicate_count": result.duplicate_count,
            "worker_mudo_lineage_blocked_count": result.blocked_count,
            "lineage_event_refs": [event.lineage_event_ref for event in result.events],
            "noncanonical_branch_refs": [event.noncanonical_branch_ref for event in result.events],
            "provider_jobs_created": result.provider_jobs_created,
            "protocol_runs_created": result.protocol_runs_created,
            "episode_runs_created": result.episode_runs_created,
            "outbox_dispatch_performed": result.outbox_dispatch_performed,
            "claim_promotion_performed": result.claim_promotions_performed > 0,
            "graph_write_performed": result.graph_writes_performed > 0,
            "canonical_mudo_mutation_performed": result.canonical_mudo_mutations_performed > 0,
            "execution_started": result.execution_started,
            "command_kernel_protocol_reviews_worker_lineage_project": True,
        },
        "artifact_refs": [],
        "resource_refs": [event.noncanonical_branch_ref for event in result.events],
        "evidence_refs": [receipt.receipt_ref for receipt in result.receipts],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_retry_transition_record(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    command_name = "protocol.reviews.worker.retry.transition.record"
    worker_receipt_payload = _json_command_value(
        args.get("worker_receipt_payload", args.get("worker_receipt")),
        field_name="worker_receipt_payload",
        command_name=command_name,
    )
    result = record_post_p6_worker_retry_transition(
        outbox_store_path=args.get("outbox_store_path"),
        claim_store_path=args.get("claim_store_path"),
        retry_transition_store_path=args.get("retry_transition_store_path"),
        source_outbox_ref=args.get("source_outbox_ref"),
        worker_receipt_payload=worker_receipt_payload if isinstance(worker_receipt_payload, Mapping) else None,
        max_transitions=int(args.get("max_transitions") or 1),
        provider_job_requested=bool(args.get("provider_job_requested") or args.get("call_provider")),
        protocol_run_requested=bool(args.get("protocol_run_requested") or args.get("run_protocol")),
        episode_run_requested=bool(args.get("episode_run_requested") or args.get("run_episode")),
        claim_promotion_requested=bool(args.get("claim_promotion_requested") or args.get("promote_claim")),
        graph_write_requested=bool(args.get("graph_write_requested") or args.get("write_graph")),
        canonical_mudo_mutation_requested=bool(args.get("canonical_mudo_mutation_requested") or args.get("write_mudo")),
        execute_now=bool(args.get("execute_now") or args.get("execute_protocol")),
    )
    payload = result.model_dump(mode="json")
    return {
        "summary": (
            f"{command_name}: status={result.status} recorded={result.recorded_count} "
            f"duplicates={result.duplicate_count} blocked={result.blocked_count}"
        ),
        "result": {
            **payload,
            "canonical_command_name": command_name,
            "transition_authority": "post_p6_worker_retry_transition_contract",
        },
        "state_after": {
            "worker_retry_transition_status": result.status,
            "worker_retry_transition_recorded_count": result.recorded_count,
            "worker_retry_transition_duplicate_count": result.duplicate_count,
            "worker_retry_transition_blocked_count": result.blocked_count,
            "retry_transition_receipt_refs": [receipt.receipt_ref for receipt in result.receipts],
            "retry_transition_keys": [receipt.transition_key for receipt in result.receipts if receipt.transition_key],
            "provider_jobs_created": result.provider_jobs_created,
            "protocol_runs_created": result.protocol_runs_created,
            "episode_runs_created": result.episode_runs_created,
            "outbox_dispatch_performed": result.outbox_dispatch_performed,
            "claim_promotion_performed": result.claim_promotions_performed > 0,
            "graph_write_performed": result.graph_writes_performed > 0,
            "canonical_mudo_mutation_performed": result.canonical_mudo_mutations_performed > 0,
            "execution_started": result.execution_started,
            "command_kernel_protocol_reviews_worker_retry_transition_record": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [receipt.receipt_ref for receipt in result.receipts],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_handoff_submit(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel
    command_name = "protocol.reviews.worker.handoff.submit"
    protocol_payload = _json_command_value(
        args.get("protocol_jsonld", args.get("protocol_payload", args.get("payload"))),
        field_name="protocol_payload",
        command_name=command_name,
    )
    proposal_payload = _json_command_value(
        args.get("proposal_payload", args.get("proposal")),
        field_name="proposal_payload",
        command_name=command_name,
    )
    quetzal_verdict_payload = _json_command_value(
        args.get("quetzal_verdict_payload", args.get("quetzal_verdict", args.get("submit_gate"))),
        field_name="quetzal_verdict_payload",
        command_name=command_name,
    )
    backpressure_policy = _json_command_value(
        args.get("backpressure_policy"),
        field_name="backpressure_policy",
        command_name=command_name,
    )
    retry_policy = _json_command_value(
        args.get("retry_policy"),
        field_name="retry_policy",
        command_name=command_name,
    )
    request_metadata: dict[str, Any] = {}
    if isinstance(protocol_payload, Mapping):
        request_metadata = dict(protocol_payload.get("metadata") or {})
        if request_metadata:
            protocol_payload = dict(protocol_payload)
            protocol_payload["metadata"] = request_metadata
    request_metadata.setdefault("workspace_id", str(envelope.workspace_id or args.get("workspace_id") or ""))
    request_metadata.setdefault("study_id", str(envelope.study_id or args.get("study_id") or ""))
    owner_user_id = str(
        args.get("owner_user_id")
        or envelope.request_identity.get("owner_user_id")
        or envelope.request_identity.get("user_id")
        or request_metadata.get("owner_user_id")
        or request_metadata.get("user_id")
        or ""
    ).strip()
    if owner_user_id:
        request_metadata.setdefault("owner_user_id", owner_user_id)
        request_metadata.setdefault("user_id", owner_user_id)
    selection_requirements = {
        key: value
        for key in (
            "gpu",
            "gpu_type",
            "timeout_s",
            "timeout",
            "max_cost_usd",
            "region",
            "features",
            "backend",
            "provider_candidates",
            "allowed_providers",
            "remote_only",
            "capability_requirements",
        )
        if (value := args.get(key)) not in (None, "", [], {})
    }

    result = await activate_post_p6_worker_handoff(
        handoff_ref=str(args.get("handoff_ref") or "").strip(),
        protocol_payload=protocol_payload if isinstance(protocol_payload, Mapping) else None,
        proposal_payload=proposal_payload if isinstance(proposal_payload, Mapping) else None,
        outbox_store_path=args.get("outbox_store_path"),
        claim_store_path=args.get("claim_store_path"),
        activation_store_path=args.get("activation_store_path"),
        lineage_store_path=args.get("lineage_store_path"),
        retry_transition_store_path=args.get("retry_transition_store_path"),
        quetzal_verdict_payload=quetzal_verdict_payload if isinstance(quetzal_verdict_payload, Mapping) else None,
        backpressure_policy=backpressure_policy if isinstance(backpressure_policy, Mapping) else None,
        retry_policy=retry_policy if isinstance(retry_policy, Mapping) else None,
        provider_available=args.get("provider_available"),
        source_authority=str(args.get("source_authority") or "command_kernel").strip() or "command_kernel",
        checkpoint_dir=args.get("checkpoint_dir"),
        request_metadata=request_metadata,
        selection_requirements=selection_requirements or None,
    )
    payload = result.model_dump(mode="json")
    return {
        "summary": (
            f"{command_name}: status={result.status} submitted={result.submitted_count} "
            f"duplicates={result.duplicate_count} blocked={result.blocked_count}"
        ),
        "result": {
            **payload,
            "canonical_command_name": command_name,
            "submission_authority": "protocol_executor",
            "worker_authority": "retry_backfill_only",
        },
        "state_after": {
            "worker_handoff_submit_status": result.status,
            "worker_handoff_submitted_count": result.submitted_count,
            "worker_handoff_duplicate_count": result.duplicate_count,
            "worker_handoff_blocked_count": result.blocked_count,
            "worker_handoff_retry_count": result.retry_count,
            "worker_handoff_dead_letter_count": result.dead_letter_count,
            "run_refs": [activation.run_ref for activation in result.activations if activation.run_ref],
            "run_receipt_refs": [activation.run_receipt_ref for activation in result.activations if activation.run_receipt_ref],
            "run_receipt_bundle_refs": [activation.run_receipt_bundle_ref for activation in result.activations if activation.run_receipt_bundle_ref],
            "provider_binding_refs": [activation.provider_binding_ref for activation in result.activations if activation.provider_binding_ref],
            "selected_providers": [activation.selected_provider for activation in result.activations if activation.selected_provider],
            "selection_receipt_refs": [activation.selection_receipt_ref for activation in result.activations if activation.selection_receipt_ref],
            "retry_transition_receipt_refs": list(result.retry_transition_receipt_refs),
            "noncanonical_branch_refs": [event.noncanonical_branch_ref for event in result.lineage_events],
            "protocol_runs_created": result.protocol_runs_created,
            "execution_started": result.execution_started,
            "hn_submit_metrics": dict(result.metrics),
            "command_kernel_protocol_reviews_worker_handoff_submit": True,
        },
        "artifact_refs": [
            artifact_ref
            for activation in result.activations
            for artifact_ref in activation.artifact_refs
        ],
        "resource_refs": [
            ref
            for activation in result.activations
            for ref in (activation.run_ref, activation.noncanonical_branch_ref, activation.provider_binding_ref)
            if ref
        ],
        "evidence_refs": [
            *[receipt.receipt_ref for receipt in result.receipts],
            *[
                evidence_ref
                for activation in result.activations
                for evidence_ref in activation.evidence_refs
            ],
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_handoff_binding(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    payload = inspect_post_p6_handoff_binding(
        run_ref=str(args.get("run_ref") or "").strip(),
        handoff_ref=str(args.get("handoff_ref") or "").strip(),
        activation_store_path=args.get("activation_store_path"),
        retry_transition_store_path=args.get("retry_transition_store_path"),
        claim_store_path=args.get("claim_store_path"),
        outbox_store_path=args.get("outbox_store_path"),
    )
    return {
        "summary": (
            f"protocol.reviews.worker.handoff.binding: status={payload['status']} "
            f"provider={payload.get('selected_provider') or 'none'} failovers={len(payload.get('failovers') or [])}"
        ),
        "result": {
            **payload,
            "canonical_command_name": "protocol.reviews.worker.handoff.binding",
        },
        "state_after": {
            "worker_handoff_binding_status": payload["status"],
            "worker_handoff_selected_provider": payload.get("selected_provider"),
            "worker_handoff_provider_binding_ref": payload.get("provider_binding_ref"),
            "worker_handoff_failover_count": len(payload.get("failovers") or []),
            "command_kernel_protocol_reviews_worker_handoff_binding": True,
        },
        "artifact_refs": [],
        "resource_refs": [ref for ref in (payload.get("run_ref"), payload.get("handoff_ref"), payload.get("provider_binding_ref")) if ref],
        "evidence_refs": [
            ref
            for ref in (
                payload.get("activation_ref"),
                payload.get("selection_receipt_ref"),
                *[item.get("receipt_ref") for item in payload.get("failovers") or []],
            )
            if ref
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_handoff_status(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    payload = inspect_post_p6_handoff_status(
        run_ref=str(args.get("run_ref") or "").strip(),
        handoff_ref=str(args.get("handoff_ref") or "").strip(),
        activation_store_path=args.get("activation_store_path"),
        retry_transition_store_path=args.get("retry_transition_store_path"),
        claim_store_path=args.get("claim_store_path"),
        outbox_store_path=args.get("outbox_store_path"),
    )
    return {
        "summary": (
            f"protocol.reviews.worker.handoff.status: status={payload['status']} "
            f"provider={payload.get('selected_provider') or 'none'} retries={payload.get('retry_attempt_count') or 0}"
        ),
        "result": {
            **payload,
            "canonical_command_name": "protocol.reviews.worker.handoff.status",
        },
        "state_after": {
            "worker_handoff_status": payload["status"],
            "worker_handoff_dead_lettered": payload.get("dead_lettered"),
            "worker_handoff_retry_attempt_count": payload.get("retry_attempt_count"),
            "command_kernel_protocol_reviews_worker_handoff_status": True,
        },
        "artifact_refs": [],
        "resource_refs": [
            ref
            for ref in (
                payload.get("run_ref"),
                payload.get("handoff_ref"),
                payload.get("provider_binding_ref"),
                payload.get("source_outbox_ref"),
            )
            if ref
        ],
        "evidence_refs": [
            ref
            for ref in (
                payload.get("activation_ref"),
                payload.get("selection_receipt_ref"),
                payload.get("dead_letter_entry", {}).get("receipt_ref"),
                *[item.get("receipt_ref") for item in payload.get("failovers") or []],
            )
            if ref
        ],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_handoff_retry_status(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    run_ref = str(args.get("run_ref") or "").strip()
    handoff_ref = str(args.get("handoff_ref") or "").strip()
    payload = inspect_post_p6_handoff_retry_status(
        run_ref=run_ref,
        handoff_ref=handoff_ref,
        activation_store_path=args.get("activation_store_path"),
        retry_transition_store_path=args.get("retry_transition_store_path"),
        claim_store_path=args.get("claim_store_path"),
        outbox_store_path=args.get("outbox_store_path"),
    )
    return {
        "summary": (
            f"protocol.reviews.worker.handoff.retry.status: status={payload['status']} "
            f"attempts={payload['retry_attempt_count']} dead_lettered={payload['dead_lettered']}"
        ),
        "result": {
            **payload,
            "canonical_command_name": "protocol.reviews.worker.handoff.retry.status",
        },
        "state_after": {
            "worker_handoff_retry_status": payload["status"],
            "worker_handoff_retry_attempt_count": payload["retry_attempt_count"],
            "worker_handoff_dead_lettered": payload["dead_lettered"],
            "command_kernel_protocol_reviews_worker_handoff_retry_status": True,
        },
        "artifact_refs": [],
        "resource_refs": [ref for ref in (payload.get("run_ref"), payload.get("handoff_ref")) if ref],
        "evidence_refs": [item.get("receipt_ref") for item in payload.get("retry_receipts") or [] if item.get("receipt_ref")],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_reviews_worker_handoff_deadletter(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    payload = list_post_p6_handoff_deadletters(
        retry_transition_store_path=args.get("retry_transition_store_path"),
        activation_store_path=args.get("activation_store_path"),
        claim_store_path=args.get("claim_store_path"),
    )
    return {
        "summary": (
            f"protocol.reviews.worker.handoff.deadletter: dead_letters={payload['dead_letter_count']}"
        ),
        "result": {
            **payload,
            "canonical_command_name": "protocol.reviews.worker.handoff.deadletter",
        },
        "state_after": {
            "worker_handoff_dead_letter_count": payload["dead_letter_count"],
            "command_kernel_protocol_reviews_worker_handoff_deadletter": True,
        },
        "artifact_refs": [],
        "resource_refs": [item.get("handoff_ref") for item in payload.get("dead_letters") or [] if item.get("handoff_ref")],
        "evidence_refs": [item.get("receipt_ref") for item in payload.get("dead_letters") or [] if item.get("receipt_ref")],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p6_msrp_self_improvement_design(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p6_intake_packet(args)
    command_name = "protocol.msrp.self_improvement.design"
    design_request = _json_command_value(
        args.get("design_request"),
        field_name="design_request",
        command_name=command_name,
    )
    if design_request is not None and not isinstance(design_request, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_msrp_self_improvement_design_request",
            message="design_request must be an object.",
        )

    design = build_p6_msrp_self_improvement_design(
        packet,
        design_request_payload=design_request,
    )
    common = _p6_common_result(packet, path)
    result = {
        **common,
        "p6_packet_status": common.get("status"),
        **design.model_dump(mode="json"),
        "projection_authority": "p6_msrp_self_improvement_design_contract",
    }
    proposal_ref = (
        design.calibration_proposal.proposal_ref
        if design.calibration_proposal is not None
        else None
    )
    return {
        "summary": (
            f"{command_name} {result['p6_id']}: status={design.status} "
            f"metrics={len(design.observations)} proposal={proposal_ref or 'none'}"
        ),
        "result": result,
        "state_after": {
            "p6_id": result["p6_id"],
            "p6_packet_status": result["p6_packet_status"],
            "msrp_self_improvement_status": design.current_status,
            "msrp_metric_observation_count": len(design.observations),
            "msrp_calibration_proposal_ref": proposal_ref,
            "msrp_calibration_proposal_status": (
                design.calibration_proposal.status
                if design.calibration_proposal is not None
                else "none"
            ),
            "metrics_read_only": all(item.read_only for item in design.observations),
            "provider_jobs_created": 0,
            "protocol_runs_created": 0,
            "training_runs_created": 0,
            "model_weights_changed": False,
            "evaluator_configuration_changed": False,
            "claim_promotion_performed": False,
            "graph_write_performed": False,
            "canonical_mudo_write_performed": False,
            "execution_started": False,
            "command_kernel_msrp_self_improvement_design": True,
            "legacy_command_aliases": ["protocol.p6.msrp.self_improvement.design"],
        },
        "artifact_refs": list(design.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [result["p6_intake_packet_ref"], *design.receipt_refs],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p5_slices(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    requested_slice_id = str(args.get("slice_id") or "").strip()
    slices = _packet_refs(packet, "slices")
    selected = [
        item for item in slices
        if not requested_slice_id or str(item.get("slice_id") or "") == requested_slice_id
    ]
    if requested_slice_id and not selected:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="p5_slice_not_found",
            message=f"P5 intake packet does not contain slice_id {requested_slice_id!r}.",
            details={"slice_id": requested_slice_id, "p5_intake_packet_path": str(path)},
        )
    result = {
        **_p5_common_result(packet, path),
        "slice_id": requested_slice_id or None,
        "slice_count": len(selected),
        "status_counts": _count_by_status(slices),
        "slices": selected,
        "next_objective": dict(packet.get("next_objective") or {}),
    }
    return {
        "summary": f"protocol.p5.slices {result['p5_id']}: slices={len(selected)}",
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "slice_id": requested_slice_id or None,
            "p5_slice_count": len(selected),
            "command_kernel_p5_slices": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"]],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p5_residuals(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    residual_id = str(args.get("residual_id") or args.get("code") or "").strip()
    residuals = _packet_refs(packet, "deferred_residuals")
    selected = [
        item for item in residuals
        if not residual_id
        or str(item.get("residual_id") or "") == residual_id
        or str(item.get("code") or "") == residual_id
    ]
    if residual_id and not selected:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="p5_residual_not_found",
            message=f"P5 intake packet does not contain residual {residual_id!r}.",
            details={"residual_id": residual_id, "p5_intake_packet_path": str(path)},
        )
    result = {
        **_p5_common_result(packet, path),
        "residual_id": residual_id or None,
        "residual_count": len(selected),
        "residuals": selected,
        "runpod_deferred": any(
            "runpod" in str(item.get("code") or item.get("residual_id") or "").lower()
            for item in selected
        ),
        "blocks_p5_start": False,
    }
    return {
        "summary": f"protocol.p5.residuals {result['p5_id']}: residuals={len(selected)}",
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "residual_id": residual_id or None,
            "p5_residual_count": len(selected),
            "command_kernel_p5_residuals": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"]],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p5_refs(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    ref_kind = str(args.get("ref_kind") or "all").strip()
    proof_id = str(args.get("proof_id") or "").strip()
    allowed_ref_kinds = {"all", "proof", "artifact", "receipt", "mudo"}
    if ref_kind not in allowed_ref_kinds:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_p5_ref_kind",
            message=f"Unsupported P5 ref_kind {ref_kind!r}.",
            details={"ref_kind": ref_kind, "allowed_ref_kinds": sorted(allowed_ref_kinds)},
        )

    refs = {
        "proof_refs": _packet_refs(packet, "proof_refs"),
        "artifact_refs": _packet_refs(packet, "artifact_refs"),
        "receipt_refs": _packet_refs(packet, "receipt_refs"),
        "mudo_refs": _packet_refs(packet, "mudo_refs"),
    }
    if proof_id:
        refs = {
            key: [item for item in values if str(item.get("proof_id") or "") == proof_id]
            for key, values in refs.items()
        }
    if ref_kind != "all":
        refs = {f"{ref_kind}_refs": refs[f"{ref_kind}_refs"]}
    result = {
        **_p5_common_result(packet, path),
        "ref_kind": ref_kind,
        "proof_id": proof_id or None,
        **refs,
        "ref_counts": {key: len(value) for key, value in refs.items()},
        "raw_provider_payload_embedded": bool((packet.get("scope") or {}).get("raw_provider_payload_embedded")),
    }
    return {
        "summary": f"protocol.p5.refs {result['p5_id']}: ref_kind={ref_kind}",
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "ref_kind": ref_kind,
            "proof_id": proof_id or None,
            "command_kernel_p5_refs": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"]],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p5_cg_proposals(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    try:
        top_k = int(args.get("top_k") or 3)
    except (TypeError, ValueError):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_p5_cg_top_k",
            message="protocol.p5.cg.proposals requires top_k to be an integer when provided.",
            details={"top_k": args.get("top_k")},
        )
    proposal = build_p5_cg_validation_proposal(packet, top_k=top_k)
    common = _p5_common_result(packet, path)
    result = {
        **common,
        "p5_packet_status": common.get("status"),
        **proposal.model_dump(mode="json"),
        "proposal_authority": "p5_cg_validation_contract",
        "cg_execution_started": False,
    }
    return {
        "summary": (
            f"protocol.p5.cg.proposals {result['p5_id']}: "
            f"status={proposal.status} candidates={len(proposal.candidates)} blockers={len(proposal.blockers)}"
        ),
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "p5_status": result["p5_packet_status"],
            "p5_intake_packet_ref": result["p5_intake_packet_ref"],
            "cg_proposal_status": proposal.status,
            "cg_candidate_count": len(proposal.candidates),
            "cg_blocker_count": len(proposal.blockers),
            "quetzal_cg_pre_submit_receipt_ref": proposal.quetzal_pre_submit_receipt.receipt_ref,
            "command_kernel_p5_cg_proposals": True,
        },
        "artifact_refs": list(proposal.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"], proposal.quetzal_pre_submit_receipt.receipt_ref],
        "usd": 0.0,
        "tool_calls": 1,
    }
async def protocol_p5_ese_cg_proposals(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    trajectory_ref = str(args.get("trajectory_ref") or "").strip()
    extraction_mode = str(args.get("extraction_mode") or "deterministic_fixture_contract").strip()
    trajectory_payload = args.get("trajectory_payload")
    if isinstance(trajectory_payload, str) and trajectory_payload.strip():
        try:
            trajectory_payload = json.loads(trajectory_payload)
        except json.JSONDecodeError:
            from mica.agentic.command_kernel import _KernelBlocked

            raise _KernelBlocked(
                code="invalid_trajectory_payload_json",
                message="protocol.p5.ese_cg.proposals trajectory_payload string must be valid JSON.",
                details={"trajectory_payload_preview": trajectory_payload[:120]},
            )
    if trajectory_payload is None:
        trajectory_payload = _default_p5_ese_cg_fixture_payload(trajectory_ref)
    if trajectory_payload is not None and not isinstance(trajectory_payload, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_trajectory_payload",
            message="protocol.p5.ese_cg.proposals requires trajectory_payload to be an object when provided.",
            details={"trajectory_payload_type": type(trajectory_payload).__name__},
        )
    proposal = build_p5_ese_cg_extraction_proposal(
        packet,
        trajectory_payload=trajectory_payload,
        trajectory_ref=trajectory_ref,
        extraction_mode=extraction_mode,
    )
    common = _p5_common_result(packet, path)
    result = {
        **common,
        "p5_packet_status": common.get("status"),
        **proposal.model_dump(mode="json"),
        "proposal_authority": "p5_ese_cg_contract",
        "mdanalysis_runtime_started": False,
    }
    return {
        "summary": (
            f"protocol.p5.ese_cg.proposals {result['p5_id']}: "
            f"status={proposal.status} artifact_ready={proposal.artifact is not None} blockers={len(proposal.blockers)}"
        ),
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "p5_status": result["p5_packet_status"],
            "p5_intake_packet_ref": result["p5_intake_packet_ref"],
            "ese_cg_proposal_status": proposal.status,
            "ese_cg_artifact_ref": proposal.artifact.artifact_ref if proposal.artifact else None,
            "quetzal_ese_cg_receipt_ref": proposal.quetzal_receipt.receipt_ref,
            "command_kernel_p5_ese_cg_proposals": True,
        },
        "artifact_refs": list(proposal.artifact_refs),
        "resource_refs": [],
        "evidence_refs": [result["p5_intake_packet_ref"], proposal.quetzal_receipt.receipt_ref],
        "usd": 0.0,
        "tool_calls": 1,
    }


def _json_command_value(
    value: Any,
    *,
    field_name: str,
    command_name: str = "protocol.p5.citations.consolidate",
) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code=f"invalid_{field_name}_json",
            message=f"{command_name} requires valid JSON for {field_name}.",
            details={"field_name": field_name, "error": str(exc)},
        ) from exc


async def protocol_p5_citations_consolidate(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    claim_payload = _json_command_value(args.get("claim"), field_name="claim")
    citation_evidence = _json_command_value(args.get("citation_evidence"), field_name="citation_evidence")
    quetzal_receipt = _json_command_value(args.get("quetzal_receipt"), field_name="quetzal_receipt")
    if not isinstance(claim_payload, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="missing_claim_payload",
            message="protocol.p5.citations.consolidate requires a claim object.",
        )
    if quetzal_receipt is not None and not isinstance(quetzal_receipt, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_quetzal_receipt",
            message="protocol.p5.citations.consolidate quetzal_receipt must be an object.",
        )
    proposal = build_p5_citation_consolidation(
        packet,
        claim_payload=claim_payload,
        citation_evidence=citation_evidence,
        quetzal_receipt_payload=quetzal_receipt,
    )
    common = _p5_common_result(packet, path)
    result = {
        **common,
        "p5_packet_status": common.get("status"),
        **proposal.model_dump(mode="json"),
        "proposal_authority": "p5_citation_consolidation_contract",
    }
    return {
        "summary": (
            f"protocol.p5.citations.consolidate {result['p5_id']}: "
            f"status={proposal.status} lines={len(proposal.citation_cascade.evidence_lines)} "
            f"blockers={len(proposal.blockers)}"
        ),
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "citation_consolidation_status": proposal.status,
            "citation_cascade_ref": proposal.citation_cascade.packet_ref,
            "citation_evidence_line_count": len(proposal.citation_cascade.evidence_lines),
            "claim_state_after": proposal.claim_state_after,
            "claim_promotion_performed": False,
            "alejandria_write_performed": False,
            "graph_write_performed": False,
            "command_kernel_p5_citation_consolidation": True,
        },
        "artifact_refs": list(proposal.artifact_refs),
        "resource_refs": [],
        "evidence_refs": list(proposal.evidence_refs) + list(proposal.receipt_refs),
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_p5_scale_readiness(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    packet, path = _load_p5_intake_packet(args)
    command_name = "protocol.p5.scale.readiness"
    candidates = _json_command_value(args.get("candidates"), field_name="candidates", command_name=command_name)
    budget = _json_command_value(args.get("budget"), field_name="budget", command_name=command_name)
    retry_policy = _json_command_value(
        args.get("retry_policy"),
        field_name="retry_policy",
        command_name=command_name,
    )
    if not isinstance(candidates, list):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(
            code="invalid_scale_candidates",
            message="protocol.p5.scale.readiness requires a candidates array.",
        )
    if budget is not None and not isinstance(budget, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(code="invalid_budget_envelope", message="budget must be an object.")
    if retry_policy is not None and not isinstance(retry_policy, Mapping):
        from mica.agentic.command_kernel import _KernelBlocked

        raise _KernelBlocked(code="invalid_retry_policy", message="retry_policy must be an object.")

    readiness = build_p5_proteome_scale_readiness(
        packet,
        candidates_payload=candidates,
        budget_payload=budget,
        retry_policy_payload=retry_policy,
    )
    common = _p5_common_result(packet, path)
    result = {
        **common,
        "p5_packet_status": common.get("status"),
        **readiness.model_dump(mode="json"),
        "proposal_authority": "command_kernel_protocol_runtime",
    }
    return {
        "summary": (
            f"protocol.p5.scale.readiness {result['p5_id']}: status={readiness.status} "
            f"candidates={len(readiness.candidates)} proposed={readiness.proposed_count} "
            f"blocked={readiness.blocked_count} provider_jobs=0"
        ),
        "result": result,
        "state_after": {
            "p5_id": result["p5_id"],
            "scale_readiness_status": readiness.status,
            "batch_candidate_count": len(readiness.candidates),
            "outbox_proposal_count": readiness.proposed_count,
            "duplicate_submission_count": readiness.duplicate_count,
            "provider_unavailable_count": readiness.provider_unavailable_count,
            "provider_jobs_created": 0,
            "outbox_dispatch_performed": False,
            "claim_promotion_performed": False,
            "graph_write_performed": False,
            "command_kernel_p5_scale_readiness": True,
        },
        "artifact_refs": list(readiness.artifact_refs),
        "resource_refs": [],
        "evidence_refs": list(readiness.receipt_refs),
        "usd": 0.0,
        "tool_calls": 1,
    }


def _default_p5_ese_cg_fixture_payload(trajectory_ref: str) -> Dict[str, Any] | None:
    if not trajectory_ref:
        return None
    return {
        "trajectory_ref": trajectory_ref,
        "frames": [
            [[0.0, 0.0, 0.0], [1.0, 0.2, 0.1], [0.4, 1.1, 0.0]],
            [[0.1, 0.0, 0.0], [1.1, 0.3, 0.2], [0.5, 1.0, 0.1]],
            [[0.2, 0.1, 0.0], [1.2, 0.4, 0.1], [0.6, 1.0, 0.2]],
        ],
    }
async def protocol_node_receipts(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    del kernel, envelope
    status = _resolve_protocol_status(args)
    protocol_id = str(status.get("protocol_id") or _protocol_status_query(args).get("protocol_id") or "").strip()
    node_filter = str(args.get("node_id") or "").strip()
    unified_runtime = status.get("unified_runtime")
    if not isinstance(unified_runtime, Mapping):
        unified_runtime = {}
    receipts = list(status.get("node_receipts") or [])
    if node_filter:
        receipts = [
            receipt
            for receipt in receipts
            if str((receipt or {}).get("node_id") or "") == node_filter
        ]
    result = {
        "protocol_id": protocol_id,
        "node_id": node_filter or None,
        "node_receipt_count": len(receipts),
        "node_receipts": receipts,
        "projection_authority": status.get("projection_authority") or unified_runtime.get("projection_authority"),
        "closure_packet_ref": status.get("closure_packet_ref") or unified_runtime.get("closure_packet_ref"),
        "artifact_refs": unified_runtime.get("artifact_refs") or [],
        "receipt_refs": unified_runtime.get("receipt_refs") or {},
    }
    return {
        "summary": f"protocol.node.receipts {protocol_id}: receipts={len(receipts)}",
        "result": result,
        "state_after": {
            "protocol_id": protocol_id,
            "node_id": node_filter or None,
            "node_receipt_count": len(receipts),
            "command_kernel_protocol_receipts": True,
        },
        "artifact_refs": [],
        "resource_refs": [],
        "evidence_refs": [],
        "usd": 0.0,
        "tool_calls": 1,
    }


async def protocol_list(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    """List governed protocols through the runtime ledger authority."""
    del kernel
    supplied_scope = _protocol_scope_from_request(args, envelope)
    ledger_protocols = get_protocol_runtime_ledger().list_protocols(**supplied_scope)
    visible: Dict[str, Dict[str, Any]] = {
        str(item.get("protocol_id") or "").strip(): dict(item)
        for item in ledger_protocols
        if str(item.get("protocol_id") or "").strip()
    }
    for item in _list_live_protocol_job_projections(supplied_scope):
        protocol_id = str(item.get("protocol_id") or "").strip()
        if protocol_id and protocol_id not in visible:
            visible[protocol_id] = dict(item)

    protocols = sorted(visible.values(), key=lambda item: str(item.get("protocol_id") or ""))
    return {
        "summary": f"protocol.list returned {len(protocols)} governed protocol(s) from runtime authority.",
        "result": {
            "protocols": protocols,
            "count": len(protocols),
            "list_mode": "runtime_authority",
            "scope": supplied_scope,
        },
        "state_after": {
            "command_kernel_protocol_list": True,
            "protocol_count": len(protocols),
        },
        "route_authority": "command_kernel_runtime",
        "route_backed": False,
    }


async def protocol_inspect(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    """Inspect one protocol — route-backed read adapter.

    GET /api/v1/protocols/{protocol_id}
    """
    del kernel
    protocol_id = str(args.get("protocol_id") or "").strip()
    if not protocol_id:
        from mica.agentic.command_kernel import _KernelBlocked
        raise _KernelBlocked(
            code="missing_protocol_id",
            message="protocol.inspect requires protocol_id.",
        )
    return {
        "summary": f"Protocol inspection for {protocol_id}",
        "result": {"protocol_id": protocol_id, "inspect_mode": "read_only"},
        "route_authority": "backend_api",
        "route_backed": True,
    }
