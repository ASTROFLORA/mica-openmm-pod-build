from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any, Mapping

from mica.drivers.execution.sandbox_session_service import run_execute_in_sandbox_branch

SANDBOX_PROTOCOL_TOOL_NAMES = frozenset({"run_mica_q_sandbox", "execute_in_sandbox"})
SANDBOX_PROTOCOL_SURFACES = frozenset({"sandbox", "mica_q_sandbox", "sandbox_node"})


def _tool_name(node: Any) -> str:
    inputs = getattr(node, "inputs", None)
    if not isinstance(inputs, dict):
        return ""
    for key in ("tool_name", "action", "tool", "operation"):
        value = str(inputs.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def protocol_node_uses_sandbox_surface(node: Any) -> bool:
    if _tool_name(node) in SANDBOX_PROTOCOL_TOOL_NAMES:
        return True
    surface = str(getattr(node, "executor_surface", "") or "").strip().lower()
    return surface in SANDBOX_PROTOCOL_SURFACES


def _degraded_tool_response(
    name: str,
    message: str,
    *,
    args_payload: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "status": "degraded",
        "tool": name,
        "message": message,
        "args": dict(args_payload or {}),
        "extra": dict(extra or {}),
    }
    return json.dumps(payload, ensure_ascii=True, default=str)


def _safe_json_parse(payload: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except Exception:
        return {"status": "degraded", "message": "invalid_json", "raw": str(payload)}
    return parsed if isinstance(parsed, dict) else {"status": "degraded", "message": "non_object_payload"}


def _extract_topology_hint_packet(parsed_payload: Mapping[str, Any]) -> dict[str, Any] | None:
    output_files_b64 = parsed_payload.get("output_files_b64")
    if not isinstance(output_files_b64, Mapping):
        return None
    encoded = output_files_b64.get("topology_hint_packet.json")
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    try:
        decoded_text = base64.b64decode(encoded).decode("utf-8")
        decoded = json.loads(decoded_text)
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    if str(decoded.get("schema_version") or "").strip() != "topology_hint_packet_v1":
        return None
    return decoded


def _derive_dispatch_status(
    result_payload: Mapping[str, Any],
    sandbox_receipt: Mapping[str, Any],
) -> tuple[str, str, str | None]:
    receipt_status = str(sandbox_receipt.get("status") or "").strip().lower()
    result_status = str(result_payload.get("status") or "").strip().lower()
    if receipt_status == "blocked_by_policy":
        return "blocked", "node.blocked", str(sandbox_receipt.get("failure_code") or "sandbox_policy_blocked")
    if receipt_status == "failed":
        return "failed", "node.failed", str(sandbox_receipt.get("failure_code") or "sandbox_execution_failed")
    if result_status in {"failed", "error"}:
        return "failed", "node.failed", "sandbox_execution_failed"
    if result_status in {"degraded", "blocked"}:
        return "blocked", "node.blocked", str(sandbox_receipt.get("failure_code") or "sandbox_degraded")
    return "completed", "node.completed", None


async def execute_protocol_sandbox_action(
    *,
    request: Any,
    node: Any,
    user_id: str,
) -> dict[str, Any]:
    tool_name = _tool_name(node) or "run_mica_q_sandbox"
    inputs = dict(getattr(node, "inputs", {}) or {})

    execution_request = inputs.get("execution_request")
    if isinstance(execution_request, Mapping):
        merged_inputs = dict(execution_request)
        merged_inputs.update(inputs)
        inputs = merged_inputs

    inputs.setdefault("tool_name", tool_name)
    inputs.setdefault("node_id", str(getattr(node, "node_id", "sandbox-node")))
    inputs.setdefault("workdir", "/sandbox")
    inputs.setdefault("network_policy", "disabled")
    inputs.setdefault("allow_secrets", False)
    inputs.setdefault("mount_repo", False)
    inputs.setdefault("download_files", ["topology_hint_packet.json"])

    executor_obj = SimpleNamespace(_sandbox_mgr=None)
    raw_result = await run_execute_in_sandbox_branch(
        name=tool_name,
        args=inputs,
        executor_obj=executor_obj,
        degraded_tool_response_fn=_degraded_tool_response,
    )
    payload = _safe_json_parse(raw_result)

    sandbox_receipt = payload.get("sandbox_node_receipt_v1")
    if not isinstance(sandbox_receipt, Mapping):
        sandbox_receipt = {}

    output_artifacts = sandbox_receipt.get("outputs", {}).get("artifacts", [])
    if not isinstance(output_artifacts, list):
        output_artifacts = []

    topology_hint_packet = _extract_topology_hint_packet(payload)
    topology_hint_packet_path = str(payload.get("topology_hint_packet_path") or "").strip()
    if not topology_hint_packet_path:
        for item in output_artifacts:
            if not isinstance(item, Mapping):
                continue
            filename = str(item.get("filename") or "").strip()
            path = str(item.get("path") or "").strip()
            if filename == "topology_hint_packet.json" and path:
                topology_hint_packet_path = path
                break
    state_after: dict[str, Any] = {
        "status": "completed",
        "binding_surface": "mica_q_sandbox",
        "tool_name": tool_name,
        "sandbox_node_receipt_v1": dict(sandbox_receipt),
        "output_artifact_metadata": list(output_artifacts),
        "sandbox_output_scope": str(sandbox_receipt.get("outputs", {}).get("scope") or "ephemeral"),
        "sandbox_output_promotion_required": bool(
            sandbox_receipt.get("outputs", {}).get("promotion_required", True)
        ),
        "network_enforcement": "unsupported",
        "secrets_attestation": "not_injected_by_policy",
    }
    if topology_hint_packet is not None:
        state_after["topology_hint_packet"] = topology_hint_packet
    if topology_hint_packet_path:
        state_after["topology_hint_packet_path"] = topology_hint_packet_path

    status, event_type, failure_code = _derive_dispatch_status(payload, sandbox_receipt)
    state_after["status"] = status
    if failure_code:
        state_after["failure_code"] = failure_code

    artifact_refs = [
        f"sandbox://protocol/{request.protocol_id}/nodes/{node.node_id}/output/{str(item.get('path') or '').strip()}"
        for item in output_artifacts
        if str(item.get("path") or "").strip()
    ]
    evidence_refs = [f"sandbox-receipt://protocol/{request.protocol_id}/nodes/{node.node_id}"]
    cost_snapshot = {
        "usd": float(payload.get("cost_estimate_usd") or 0.0),
        "tool_name": tool_name,
        "binding_surface": "mica_q_sandbox",
    }

    summary = str(payload.get("message") or payload.get("summary") or "Executed sandbox protocol node.")
    if status == "completed":
        summary = f"Executed sandbox protocol node {node.node_id}."

    return {
        "tool_name": tool_name,
        "binding_surface": "mica_q_sandbox",
        "summary": summary,
        "state_after": state_after,
        "artifact_refs": artifact_refs,
        "evidence_refs": evidence_refs,
        "cost_snapshot": cost_snapshot,
        "approval_refs": [],
        "status": status,
        "event_type": event_type,
        "failure_code": failure_code,
    }


__all__ = [
    "execute_protocol_sandbox_action",
    "protocol_node_uses_sandbox_surface",
]