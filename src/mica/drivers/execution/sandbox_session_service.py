"""Sandbox session helpers extracted from AgenticDriver loop executor."""

import json
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from mica.sandbox.node_contracts_v1 import (
    SandboxImagePolicyV1,
    SandboxNodeRequestV1,
    build_receipt,
    evaluate_policy,
)


async def run_execute_in_sandbox_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    degraded_tool_response_fn,
) -> str:
    try:
        from mica.sandbox.modal_executor import SandboxManager

        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        request = SandboxNodeRequestV1.from_tool_args(args)
        policy = SandboxImagePolicyV1()
        policy_decision, blockers, warnings = evaluate_policy(request, policy)
        node_id = str(args.get("node_id") or f"sandbox_node:{request.request_version}:{request.workload_kind}")

        resolved_code = request.resolve_code()
        if not resolved_code.strip():
            blockers.append("missing_explicit_code_or_code_ref")

        if blockers:
            finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            blocked_receipt = build_receipt(
                node_id=node_id,
                request=request,
                resolved_code=resolved_code,
                policy=policy,
                policy_decision=policy_decision,
                blockers=blockers,
                warnings=warnings,
                started_at=started_at,
                finished_at=finished_at,
                stdout="",
                stderr="",
                exit_code=-1,
                output_files_b64={},
                failure_detail=";".join(blockers),
                degraded_reason="policy_blocked",
            )
            return degraded_tool_response_fn(
                name,
                "Sandbox execution blocked by policy.",
                args_payload=args,
                extra={
                    "detail": "policy_blocked",
                    "policy_version": policy.policy_version,
                    "policy_decision": "blocked",
                    "policy_blockers": blockers,
                    "policy_warnings": warnings,
                    "sandbox_node_receipt_v1": blocked_receipt.to_dict(),
                },
            )

        if not hasattr(executor_obj, "_sandbox_mgr"):
            executor_obj._sandbox_mgr = SandboxManager()  # type: ignore[attr-defined]
        sandbox_manager: SandboxManager = executor_obj._sandbox_mgr  # type: ignore[attr-defined]

        manager_args = request.to_manager_args(resolved_code=resolved_code)
        result = await sandbox_manager.execute(**manager_args)
        finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        result_dict = result.to_dict()
        receipt = build_receipt(
            node_id=node_id,
            request=request,
            resolved_code=resolved_code,
            policy=policy,
            policy_decision=policy_decision,
            blockers=blockers,
            warnings=warnings,
            started_at=started_at,
            finished_at=finished_at,
            stdout=str(result_dict.get("stdout") or ""),
            stderr=str(result_dict.get("stderr") or ""),
            exit_code=int(result_dict.get("exit_code", -1)),
            output_files_b64=dict(result_dict.get("output_files_b64") or {}),
            failure_detail=str(result.error) if getattr(result, "error", None) else str(result_dict.get("stderr") or "")[:4000],
            degraded_reason="runtime_error" if getattr(result, "error", None) else None,
        )

        if getattr(result, "error", None):
            return degraded_tool_response_fn(
                name,
                "Sandbox execution degraded instead of surfacing a hard runtime failure.",
                args_payload=args,
                extra={
                    "detail": result.error,
                    "session_id": result.session_id,
                    "sandbox_id": result.sandbox_id,
                    "preset": result.preset,
                    "exit_code": result.exit_code,
                    "duration_s": round(result.duration_s, 2),
                    "cost_estimate_usd": round(result.cost_estimate_usd, 5),
                    "cpu": request.cpu,
                    "memory_mb": request.memory_mb,
                    "memory_limit_mb": request.memory_limit_mb,
                    "storage_mb": request.storage_mb,
                    "policy_version": policy.policy_version,
                    "policy_decision": policy_decision,
                    "policy_warnings": warnings,
                    "sandbox_node_receipt_v1": receipt.to_dict(),
                },
            )

        result_dict.setdefault("status", "success" if int(result_dict.get("exit_code", 1)) == 0 else "failed")
        result_dict["sandbox_node_receipt_v1"] = receipt.to_dict()
        result_dict["outputs_ephemeral_by_default"] = True
        result_dict["promotion_required_for_durable_claim"] = True
        return json.dumps(result_dict, ensure_ascii=False, default=str)
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Sandbox execution degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )


async def run_sandbox_session_status_branch(
    *,
    executor_obj: Any,
    specialist_pool: Optional[Any],
) -> str:
    try:
        status: dict = {"active_sessions": [], "total_cost_usd": 0, "history": {}}
        if hasattr(executor_obj, "_sandbox_mgr"):
            status = await executor_obj._sandbox_mgr.list_sessions()
        if specialist_pool is not None:
            pool_stats = specialist_pool.get_stats()
            status["specialist_pool"] = pool_stats
            status["total_cost_usd"] = status.get("total_cost_usd", 0) + pool_stats.get("total_cost_usd", 0)
        return json.dumps(status, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def run_terminate_sandbox_session_branch(
    *,
    executor_obj: Any,
    args: Dict[str, Any],
) -> str:
    try:
        session_id = args.get("session_id", "")
        if hasattr(executor_obj, "_sandbox_mgr") and session_id:
            terminated = await executor_obj._sandbox_mgr.terminate_session(session_id)
            return json.dumps({"terminated": terminated, "session_id": session_id})
        return json.dumps({"error": "No active sandbox manager or missing session_id"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
