from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from mica.drivers.execution.child_graph_registry import ChildGraphRegistry
from mica.drivers.execution.protocol_executor import (
    ProtocolExecutionOutcome,
    ProtocolNodeDispatchResult,
    execute_protocol_executor_request,
)
from mica.drivers.execution.vertical_transcript_standardization import (
    project_vertical_transcript_to_api_timeline,
    project_vertical_transcript_to_mudo,
    project_vertical_transcript_to_shell_view,
    validate_vertical_transcript_jsonl,
)
from mica.protocol_drafts import build_protocol_executor_request
from mica_q.protocol_jsonld_contract import ProtocolNode
from mica_q.protocol_jsonld_validator import derive_protocol_execution_frontier, validate_protocol_jsonld


PROGRAM_ID = "GOG_CHILD_RUN_TRANSCRIPT_INTEGRATION_V4"
AUDIT_FILE = "gog_child_run_transcript_audit_v4.json"
TRANSCRIPT_FILE = "gog_child_run_transcript_v4.jsonl"
RECEIPT_FILE = "gog_child_run_transcript_receipt_v4.json"
PROJECTION_FILE = "gog_child_run_projection_receipt_v4.json"
SUMMARY_FILE = "runtime_transcript.txt"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(dict(row), ensure_ascii=True) for row in rows) + "\n", encoding="utf-8")


def _list_existing(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    return sorted(child.name for child in path.iterdir())


def audit_gog_child_run_transcript_status(
    *,
    fixtures_dir: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    fixtures_dir = Path(fixtures_dir)
    output_dir = Path(output_dir)
    protocol_executor = Path("src/mica/drivers/execution/protocol_executor.py")
    gog_bridge = Path("src/mica/drivers/execution/gog_child_execution_bridge.py")
    vertical_writer = Path("src/mica/drivers/execution/vertical_single_transcript.py")
    previous_program = Path(
        ".mica/programs/GRAPH_OF_GRAPHS/subprograms/GOG_NATIVE_CHILD_EXECUTION_AND_ARTIFACT_THREADING_V3"
    )

    protocol_executor_text = protocol_executor.read_text(encoding="utf-8") if protocol_executor.exists() else ""
    gog_bridge_text = gog_bridge.read_text(encoding="utf-8") if gog_bridge.exists() else ""
    previous_receipt = previous_program / "native_child_execution_receipt.json"

    dispatch_block = ""
    marker = "class ProtocolNodeDispatchResult"
    next_marker = "class ProtocolExecutionOutcome"
    marker_index = protocol_executor_text.find(marker)
    next_index = protocol_executor_text.find(next_marker)
    if marker_index >= 0 and next_index > marker_index:
        dispatch_block = protocol_executor_text[marker_index:next_index]

    dispatch_shape_live = {
        "summary": "\n    summary:" in dispatch_block,
        "state_after": "\n    state_after:" in dispatch_block,
        "artifact_refs": "\n    artifact_refs:" in dispatch_block,
        "evidence_refs": "\n    evidence_refs:" in dispatch_block,
        "cost_snapshot": "\n    cost_snapshot:" in dispatch_block,
        "approval_refs": "\n    approval_refs:" in dispatch_block,
        "status": "\n    status:" in dispatch_block,
        "event_type": "\n    event_type:" in dispatch_block,
        "failure_code": "\n    failure_code:" in dispatch_block,
    }

    bridge_extended_dispatch_usage = {
        "uses_status": "status=\"" in gog_bridge_text or "status=" in gog_bridge_text,
        "uses_event_type": "event_type=" in gog_bridge_text,
        "uses_failure_code": "failure_code=" in gog_bridge_text,
    }

    fixture_status = {
        "parent_protocol": (fixtures_dir / "gog_v3_parent_campaign.json").exists(),
        "child_protocol_a": (fixtures_dir / "gog_v3_child_a.json").exists(),
        "child_protocol_b": (fixtures_dir / "gog_v3_child_b.json").exists(),
    }

    previous_output_status = {
        "program_dir_exists": previous_program.exists(),
        "program_files": _list_existing(previous_program),
        "native_child_execution_receipt_present": previous_receipt.exists(),
    }
    if previous_receipt.exists():
        previous_output_status["native_child_execution_receipt_summary"] = {
            "status": _read_json(previous_receipt).get("status"),
        }

    payload = {
        "program": PROGRAM_ID,
        "status": "audited",
        "timestamp": _utc_now(),
        "surfaces": {
            "protocol_executor": str(protocol_executor),
            "gog_child_execution_bridge": str(gog_bridge),
            "vertical_transcript_writer": str(vertical_writer),
            "fixtures_dir": str(fixtures_dir),
            "previous_gog_native_program": str(previous_program),
        },
        "dispatch_shape_live": dispatch_shape_live,
        "bridge_extended_dispatch_usage": bridge_extended_dispatch_usage,
        "fixture_status": fixture_status,
        "previous_output_status": previous_output_status,
        "audit_findings": {
            "child_graph_id_supported_in_contract": True,
            "live_executor_dispatch_shape_is_minimal": True,
            "bridge_and_live_executor_shape_drift_detected": (
                bridge_extended_dispatch_usage["uses_status"]
                and not dispatch_shape_live["status"]
            ),
            "closure_strategy": (
                "bounded_local_parent_child_run_with_live_dispatch_shape_and_real_child_run_id"
            ),
        },
        "provider_execution": False,
        "md_execution": False,
        "artifact_root": str(output_dir),
    }
    _write_json(output_dir / AUDIT_FILE, payload)
    return payload


def _parent_protocol_fixture() -> dict[str, Any]:
    return {
        "@context": "https://mica.astroflora.org/schema/protocol/v1",
        "@type": "MICAProtocol",
        "protocol_id": "gog-v4-parent-transcript-proof",
        "version": "1.0.0",
        "session_id": "gog-v4-session",
        "owner_lab": "Graph-of-Graphs",
        "execution_mode": "staging",
        "risk_profile": "low",
        "budgets": {"max_steps": 4, "max_usd": 0.0, "max_wall_clock_s": 60},
        "approval_policy": {"mode": "auto", "required_approvers": [], "protected_surfaces": []},
        "ledger_policy": {
            "mode": "protocol_and_node_receipts",
            "receipt_schema": "mica.receipts.node.v1",
            "emit_events": True,
            "require_node_receipts": True,
        },
        "nodes": [
            {
                "node_id": "parent-node-a",
                "node_kind": "tool",
                "executor_surface": "gog_local_parent",
                "executor_id": "GOGParentLocal",
                "objective": "Execute child graph A and promote artifacts.",
                "dependencies": [],
                "inputs": {
                    "tool_name": "run_child_a",
                    "artifact_key": "child_a",
                },
                "expected_outputs": {"artifacts": ["parent_node_a_child_receipt"]},
                "evidence_requirements": ["node_receipt"],
                "policies": {},
                "failure_policy": "halt",
                "receipt_schema": {
                    "schema_id": "mica.receipts.node.v1",
                    "required_fields": [
                        "protocol_id",
                        "node_id",
                        "event_type",
                        "actor_surface",
                        "actor_id",
                        "state_before",
                        "state_after",
                        "artifact_refs",
                        "evidence_refs",
                        "cost_snapshot",
                        "approval_refs",
                        "timestamp",
                    ],
                },
                "child_graph_id": "gog-v3-child-a",
            },
            {
                "node_id": "parent-node-b",
                "node_kind": "tool",
                "executor_surface": "gog_local_parent",
                "executor_id": "GOGParentLocal",
                "objective": "Execute child graph B with parent-threaded child A artifacts when available.",
                "dependencies": [],
                "inputs": {
                    "tool_name": "run_child_b",
                    "artifact_from_a": "${parent-node-a.state_after.artifact_refs}",
                    "artifact_key": "child_b",
                },
                "expected_outputs": {"artifacts": ["parent_node_b_child_receipt"]},
                "evidence_requirements": ["node_receipt"],
                "policies": {},
                "failure_policy": "halt",
                "receipt_schema": {
                    "schema_id": "mica.receipts.node.v1",
                    "required_fields": [
                        "protocol_id",
                        "node_id",
                        "event_type",
                        "actor_surface",
                        "actor_id",
                        "state_before",
                        "state_after",
                        "artifact_refs",
                        "evidence_refs",
                        "cost_snapshot",
                        "approval_refs",
                        "timestamp",
                    ],
                },
                "child_graph_id": "gog-v3-child-b",
            },
        ],
        "edges": [],
    }


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def run_gog_child_run_transcript_proof(
    output_dir: str | Path,
    *,
    fixtures_dir: str | Path = "tests/fixtures",
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir = Path(fixtures_dir)
    checkpoint_dir = output_dir / "checkpoint"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    audit_payload = audit_gog_child_run_transcript_status(fixtures_dir=fixtures_dir, output_dir=output_dir)

    registry = ChildGraphRegistry.from_dir(fixtures_dir, glob="gog_v3_child_*.json")
    parent_document = validate_protocol_jsonld(_parent_protocol_fixture())
    parent_frontier = derive_protocol_execution_frontier(parent_document, node_receipts=None)
    parent_request = build_protocol_executor_request(parent_document, parent_frontier)

    child_lifecycle: list[dict[str, Any]] = []
    promoted_artifacts_by_parent_node: dict[str, list[str]] = {}

    async def _leaf_dispatch(node: ProtocolNode) -> ProtocolNodeDispatchResult:
        base = f"protocol://{node.executor_surface}/{node.node_id}"
        return ProtocolNodeDispatchResult(
            summary=f"{node.node_id} completed",
            state_after={"status": "completed", "value": node.node_id},
            artifact_refs=[f"{base}/artifact"],
            evidence_refs=[f"{base}/evidence"],
            cost_snapshot={"usd": 0.0, "tool_calls": 1},
            approval_refs=[],
        )

    async def _parent_dispatch(node: ProtocolNode) -> ProtocolNodeDispatchResult:
        node_id = str(node.node_id)
        child_graph_id = str(node.child_graph_id or "").strip()
        if not child_graph_id:
            return await _leaf_dispatch(node)

        child_document = registry.get(child_graph_id)
        child_frontier = derive_protocol_execution_frontier(child_document, node_receipts=None)
        child_request = build_protocol_executor_request(child_document, child_frontier)

        child_started_ts = _utc_now()
        child_outcome: ProtocolExecutionOutcome = await execute_protocol_executor_request(
            child_request,
            checkpoint_dir=str(checkpoint_dir / f"child-{child_graph_id}"),
            dispatch_node=_leaf_dispatch,
            agent_name="gog_child_v4",
        )
        child_run_id = child_outcome.run_receipt.run_id
        child_completed_ts = _utc_now()

        child_lifecycle.append(
            {
                "node_id": node_id,
                "child_graph_id": child_graph_id,
                "child_run_id": child_run_id,
                "started_at": child_started_ts,
                "completed_at": child_completed_ts,
                "child_status": child_outcome.run_receipt.status,
                "child_artifact_refs": list(child_outcome.run_receipt.artifact_refs),
            }
        )

        promoted_artifacts = list(child_outcome.run_receipt.artifact_refs)
        promoted_artifacts_by_parent_node[node_id] = promoted_artifacts

        threaded_artifacts: list[str] = []
        if node_id == "parent-node-b":
            threaded_artifacts = list(promoted_artifacts_by_parent_node.get("parent-node-a", []))

        return ProtocolNodeDispatchResult(
            summary=f"Child graph {child_graph_id} finished with run {child_run_id}",
            state_after={
                "status": child_outcome.run_receipt.status,
                "child_graph_id": child_graph_id,
                "child_run_id": child_run_id,
                "child_run_receipt_ref": f"run_receipt:{child_run_id}",
                "child_protocol_id": child_outcome.run_receipt.protocol_id,
                "artifact_refs": promoted_artifacts,
                "threaded_parent_artifacts": threaded_artifacts,
                "native_child_execution": True,
            },
            artifact_refs=promoted_artifacts,
            evidence_refs=list(child_outcome.run_receipt.evidence_refs),
            cost_snapshot={"usd": 0.0, "tool_calls": 1 + len(child_outcome.node_receipts)},
            approval_refs=[],
        )

    parent_outcome: ProtocolExecutionOutcome = asyncio.run(
        execute_protocol_executor_request(
            parent_request,
            checkpoint_dir=str(checkpoint_dir / "parent"),
            dispatch_node=_parent_dispatch,
            agent_name="gog_parent_v4",
        )
    )
    parent_run_id = parent_outcome.run_receipt.run_id

    transcript_rows: list[dict[str, Any]] = []

    def _row(
        *,
        event_type: str,
        source: str,
        status: str,
        message: str,
        node_id: str = "",
        child_run_id: str = "",
        artifact_refs: list[str] | None = None,
        receipt_ref: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "root_run_id": parent_run_id,
            "event_id": f"evt_{len(transcript_rows) + 1:04d}",
            "timestamp": _utc_now(),
            "event_type": event_type,
            "source": source,
            "status": status,
            "message": message,
            "protocol_run_id": parent_run_id,
        }
        if node_id:
            payload["node_id"] = node_id
        if child_run_id:
            payload["child_run_id"] = child_run_id
        if artifact_refs:
            payload["artifact_refs"] = list(artifact_refs)
        if receipt_ref:
            payload["receipt_ref"] = receipt_ref
        return payload

    transcript_rows.append(
        _row(
            event_type="vertical.run.started",
            source="gog_child_transcript_v4",
            status="started",
            message="Started bounded local GoG parent-child transcript proof.",
            receipt_ref=f"{RECEIPT_FILE}#raw_parent_run_receipt",
        )
    )
    transcript_rows.append(
        _row(
            event_type="protocol.run.started",
            source="protocol_executor",
            status="started",
            message="Parent protocol execution started.",
            receipt_ref=f"{RECEIPT_FILE}#raw_parent_run_receipt",
        )
    )

    for entry in child_lifecycle:
        transcript_rows.append(
            _row(
                event_type="protocol.child.started",
                source="gog_child_bridge_v4",
                status="started",
                message=f"Child graph {entry['child_graph_id']} started.",
                node_id=entry["node_id"],
                child_run_id=entry["child_run_id"],
                receipt_ref=f"{RECEIPT_FILE}#child_lifecycle/{entry['node_id']}",
            )
        )
        child_event_type = (
            "protocol.child.completed"
            if entry["child_status"] == "completed"
            else "protocol.child.blocked"
        )
        transcript_rows.append(
            _row(
                event_type=child_event_type,
                source="gog_child_bridge_v4",
                status=entry["child_status"],
                message=f"Child graph {entry['child_graph_id']} finished with status {entry['child_status']}.",
                node_id=entry["node_id"],
                child_run_id=entry["child_run_id"],
                artifact_refs=list(entry["child_artifact_refs"]),
                receipt_ref=f"{RECEIPT_FILE}#child_lifecycle/{entry['node_id']}",
            )
        )

    for node_receipt in parent_outcome.node_receipts:
        child_run_id = str(node_receipt.state_after.get("child_run_id") or "")
        transcript_rows.append(
            _row(
                event_type="protocol.node.completed",
                source=node_receipt.actor_surface,
                status=str(node_receipt.state_after.get("status") or "completed"),
                message=str(node_receipt.state_after.get("summary") or node_receipt.node_id),
                node_id=node_receipt.node_id,
                child_run_id=child_run_id,
                artifact_refs=list(node_receipt.artifact_refs),
                receipt_ref=f"{RECEIPT_FILE}#node_receipts/{node_receipt.node_id}",
            )
        )

    transcript_rows.append(
        _row(
            event_type="evidence.receipt.emitted",
            source="protocol_executor",
            status="emitted",
            message="Parent run receipt emitted.",
            artifact_refs=list(parent_outcome.run_receipt.artifact_refs),
            receipt_ref=f"{RECEIPT_FILE}#raw_parent_run_receipt",
        )
    )

    terminal_event_type = (
        "vertical.run.completed"
        if parent_outcome.run_receipt.status == "completed"
        else "vertical.run.failed"
    )
    transcript_rows.append(
        _row(
            event_type=terminal_event_type,
            source="gog_child_transcript_v4",
            status=parent_outcome.run_receipt.status,
            message=f"Parent run finished with status {parent_outcome.run_receipt.status}.",
            artifact_refs=list(parent_outcome.run_receipt.artifact_refs),
            receipt_ref=f"{RECEIPT_FILE}#raw_parent_run_receipt",
        )
    )

    transcript_path = output_dir / TRANSCRIPT_FILE
    _write_jsonl(transcript_path, transcript_rows)

    child_run_ids = _dedupe(entry["child_run_id"] for entry in child_lifecycle)
    transcript_receipt = {
        "program": PROGRAM_ID,
        "status": (
            "passed_gog_child_run_transcript_integration"
            if parent_outcome.run_receipt.status == "completed" and child_run_ids
            else "partial_child_execution_missing"
        ),
        "protocol_id": parent_outcome.run_receipt.protocol_id,
        "parent_run_id": parent_run_id,
        "child_run_ids": child_run_ids,
        "transcript_file": TRANSCRIPT_FILE,
        "raw_parent_run_receipt": parent_outcome.run_receipt.model_dump(mode="json"),
        "node_receipts": [receipt.model_dump(mode="json") for receipt in parent_outcome.node_receipts],
        "child_lifecycle": child_lifecycle,
        "audit_file": AUDIT_FILE,
        "provider_execution": False,
        "md_execution": False,
        "failure_message": parent_outcome.failure_message,
        "projection_message_ids": list(parent_outcome.projection_message_ids),
        "emitted_at": _utc_now(),
    }
    _write_json(output_dir / RECEIPT_FILE, transcript_receipt)

    parse_result = validate_vertical_transcript_jsonl(transcript_path, receipt_path=output_dir / RECEIPT_FILE)
    api_projection = project_vertical_transcript_to_api_timeline(parse_result)
    shell_projection = project_vertical_transcript_to_shell_view(parse_result, source_receipt=transcript_receipt)
    mudo_projection = project_vertical_transcript_to_mudo(parse_result, source_receipt=transcript_receipt)
    child_event_count = sum(1 for event in parse_result.canonical_events if event.event_type.startswith("protocol.child."))
    child_projection_status = "pass" if child_event_count > 0 and child_run_ids else "partial"
    shell_projection["child_count"] = len(child_run_ids)
    shell_projection["child_event_count"] = child_event_count

    projection_receipt = {
        "program": PROGRAM_ID,
        "status": (
            "passed_gog_child_run_transcript_integration"
            if child_projection_status == "pass"
            else "partial_projection_missing_child_summary"
        ),
        "parent_run_id": parent_run_id,
        "child_run_ids": child_run_ids,
        "child_event_count": child_event_count,
        "child_run_id_preserved": bool(child_run_ids),
        "api_timeline": api_projection,
        "shell_view": shell_projection,
        "mudo_projection": mudo_projection,
        "provider_execution": False,
        "md_execution": False,
        "emitted_at": _utc_now(),
    }
    _write_json(output_dir / PROJECTION_FILE, projection_receipt)

    summary_lines = [
        PROGRAM_ID,
        f"parent_run_id: {parent_run_id}",
        f"child_run_ids: {', '.join(child_run_ids)}",
        f"parent_status: {parent_outcome.run_receipt.status}",
        f"transcript_event_count: {len(transcript_rows)}",
        f"child_event_count: {child_event_count}",
        f"projection_status: {projection_receipt['status']}",
        "provider_execution: false",
        "md_execution: false",
    ]
    (output_dir / SUMMARY_FILE).write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    return {
        "audit": audit_payload,
        "receipt": transcript_receipt,
        "projection_receipt": projection_receipt,
        "transcript_path": str(transcript_path),
    }


__all__ = [
    "PROGRAM_ID",
    "AUDIT_FILE",
    "TRANSCRIPT_FILE",
    "RECEIPT_FILE",
    "PROJECTION_FILE",
    "audit_gog_child_run_transcript_status",
    "run_gog_child_run_transcript_proof",
]