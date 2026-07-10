from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


PROGRAM_ID = "VERTICAL_TRANSCRIPT_STANDARDIZATION_AND_CONSUMER_BRIDGE_V3"
CONTRACT_FILE = "vertical_transcript_contract_v3.json"
READER_RECEIPT_FILE = "vertical_transcript_reader_receipt_v3.json"
API_TIMELINE_FILE = "vertical_transcript_to_api_timeline_projection_v3.json"
SHELL_VIEW_FILE = "vertical_transcript_shell_view_receipt_v3.json"
MUDO_PROJECTION_FILE = "vertical_transcript_mudo_projection_receipt_v3.json"
GOG_AUDIT_FILE = "vertical_transcript_gog_compatibility_audit_v3.json"
SUMMARY_FILE = "runtime_transcript.txt"

RAW_REQUIRED_FIELDS: tuple[str, ...] = ("root_run_id", "timestamp", "event_type", "source", "status", "message")
CANONICAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "root_run_id",
    "event_id",
    "timestamp",
    "event_type",
    "source",
    "status",
    "message",
)
CANONICAL_REQUIRED_EVENT_TYPES: tuple[str, ...] = (
    "vertical.run.started",
    "driver.session.started",
    "protocol.run.started",
    "protocol.node.started",
    "protocol.node.completed",
    "protocol.node.blocked",
    "artifact.produced",
    "evidence.receipt.emitted",
    "evidencegate.verdict",
    "mudo.commit.linked",
    "vertical.run.completed",
    "vertical.run.failed",
)
SOURCE_TO_CANONICAL_EVENT_TYPES: dict[str, str] = {
    "run.started": "vertical.run.started",
    "run.completed": "vertical.run.completed",
    "run.failed": "vertical.run.failed",
    "node.completed": "protocol.node.completed",
    "node.blocked": "protocol.node.blocked",
}


class VerticalTranscriptStandardizationError(ValueError):
    pass


@dataclass(frozen=True)
class VerticalTranscriptEvent:
    root_run_id: str
    event_id: str
    timestamp: str
    event_type: str
    source: str
    status: str
    message: str
    node_id: str | None = None
    child_run_id: str | None = None
    protocol_run_id: str | None = None
    job_id: str | None = None
    artifact_refs: list[str] = field(default_factory=list)
    receipt_ref: str | None = None
    evidencegate_ref: str | None = None
    mudo_commit_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "root_run_id": self.root_run_id,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "source": self.source,
            "status": self.status,
            "message": self.message,
        }
        if self.node_id is not None:
            payload["node_id"] = self.node_id
        if self.child_run_id is not None:
            payload["child_run_id"] = self.child_run_id
        if self.protocol_run_id is not None:
            payload["protocol_run_id"] = self.protocol_run_id
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.artifact_refs:
            payload["artifact_refs"] = list(self.artifact_refs)
        if self.receipt_ref is not None:
            payload["receipt_ref"] = self.receipt_ref
        if self.evidencegate_ref is not None:
            payload["evidencegate_ref"] = self.evidencegate_ref
        if self.mudo_commit_ref is not None:
            payload["mudo_commit_ref"] = self.mudo_commit_ref
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class VerticalTranscriptParseResult:
    source_path: str
    source_receipt_path: str | None
    root_run_id: str
    protocol_run_id: str | None
    source_event_count: int
    canonical_event_count: int
    terminal_status: str
    event_counts: dict[str, int]
    grouped_counts: dict[str, int]
    artifact_refs: list[str]
    receipt_refs: list[str]
    malformed_entries: list[dict[str, Any]]
    canonical_events: list[VerticalTranscriptEvent]
    source_events: list[dict[str, Any]]

    def to_receipt(self) -> dict[str, Any]:
        return {
            "program": PROGRAM_ID,
            "status": "passed_reader_parse" if not self.malformed_entries else "partial_reader_parse",
            "source_path": self.source_path,
            "source_receipt_path": self.source_receipt_path,
            "root_run_id": self.root_run_id,
            "protocol_run_id": self.protocol_run_id,
            "source_event_count": self.source_event_count,
            "canonical_event_count": self.canonical_event_count,
            "terminal_status": self.terminal_status,
            "event_counts": dict(self.event_counts),
            "grouped_counts": dict(self.grouped_counts),
            "artifact_refs": list(self.artifact_refs),
            "receipt_refs": list(self.receipt_refs),
            "malformed_entries": list(self.malformed_entries),
            "canonical_required_fields": list(CANONICAL_REQUIRED_FIELDS),
            "canonical_required_event_types": list(CANONICAL_REQUIRED_EVENT_TYPES),
            "provider_execution": False,
            "md_execution": False,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        if not isinstance(payload, dict):
            raise VerticalTranscriptStandardizationError("Transcript JSONL entries must be objects")
        entries.append(payload)
    return entries


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _require_non_empty_fields(payload: Mapping[str, Any], field_names: Iterable[str], *, context: str) -> None:
    missing = [field_name for field_name in field_names if not _clean_string(payload.get(field_name))]
    if missing:
        joined = ", ".join(missing)
        raise VerticalTranscriptStandardizationError(f"{context} missing required fields: {joined}")


def _normalize_refs(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(item).strip() for item in values if str(item).strip()]
    if isinstance(values, str):
        cleaned = values.strip()
        return [cleaned] if cleaned else []
    return []


def _make_event_id(root_run_id: str, event_type: str, index: int, payload: Mapping[str, Any]) -> str:
    fingerprint = json.dumps(
        {
            "root_run_id": root_run_id,
            "event_type": event_type,
            "index": index,
            "node_id": _clean_string(payload.get("node_id")),
            "status": _clean_string(payload.get("status")),
            "message": _clean_string(payload.get("message")),
        },
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )
    return f"evt_{hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16]}"


def _canonical_event(
    *,
    root_run_id: str,
    event_type: str,
    index: int,
    source: str,
    status: str,
    message: str,
    timestamp: str,
    payload: Mapping[str, Any],
    protocol_run_id: str | None,
    synthetic: bool = False,
    node_id: str | None = None,
    child_run_id: str | None = None,
    job_id: str | None = None,
    artifact_refs: list[str] | None = None,
    receipt_ref: str | None = None,
    evidencegate_ref: str | None = None,
    mudo_commit_ref: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> VerticalTranscriptEvent:
    event_metadata = dict(metadata or {})
    if synthetic:
        event_metadata.setdefault("synthetic", True)
        event_metadata.setdefault("bridge_origin", PROGRAM_ID)
    if payload.get("metadata") and isinstance(payload.get("metadata"), dict):
        event_metadata.setdefault("source_metadata", dict(payload.get("metadata") or {}))
    return VerticalTranscriptEvent(
        root_run_id=root_run_id,
        event_id=_make_event_id(root_run_id, event_type, index, payload),
        timestamp=timestamp,
        event_type=event_type,
        source=source,
        status=status,
        message=message,
        node_id=node_id,
        child_run_id=child_run_id,
        protocol_run_id=protocol_run_id,
        job_id=job_id,
        artifact_refs=list(artifact_refs or []),
        receipt_ref=receipt_ref,
        evidencegate_ref=evidencegate_ref,
        mudo_commit_ref=mudo_commit_ref,
        metadata=event_metadata,
    )


def _source_event_to_canonical(
    payload: Mapping[str, Any],
    *,
    index: int,
    protocol_run_id: str | None,
) -> list[VerticalTranscriptEvent]:
    _require_non_empty_fields(payload, RAW_REQUIRED_FIELDS, context=f"line {index}")
    root_run_id = _clean_string(payload["root_run_id"])
    source_event_type = _clean_string(payload["event_type"])
    canonical_event_type = SOURCE_TO_CANONICAL_EVENT_TYPES.get(source_event_type, source_event_type)
    timestamp = _clean_string(payload["timestamp"])
    source = _clean_string(payload["source"])
    status = _clean_string(payload["status"])
    message = _clean_string(payload["message"])
    node_id = _clean_string(payload.get("node_id")) or None
    child_run_id = _clean_string(payload.get("child_run_id")) or None
    job_id = _clean_string(payload.get("job_id")) or None
    receipt_ref = _clean_string(payload.get("receipt_ref")) or None
    evidencegate_ref = _clean_string(payload.get("evidencegate_ref")) or None
    mudo_commit_ref = _clean_string(payload.get("mudo_commit_ref")) or None
    artifact_refs = _normalize_refs(payload.get("artifact_refs"))
    metadata = dict(payload.get("metadata") or {})
    canonical_events: list[VerticalTranscriptEvent] = []

    if source_event_type == "run.started":
        canonical_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type="vertical.run.started",
                index=index,
                source=source,
                status=status,
                message=message,
                timestamp=timestamp,
                payload=payload,
                protocol_run_id=protocol_run_id,
                node_id=node_id,
                child_run_id=child_run_id,
                job_id=job_id,
                artifact_refs=artifact_refs,
                receipt_ref=receipt_ref,
                evidencegate_ref=evidencegate_ref,
                mudo_commit_ref=mudo_commit_ref,
                metadata=metadata,
            )
        )
    elif source_event_type == "run.completed":
        canonical_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type="vertical.run.completed",
                index=index,
                source=source,
                status=status,
                message=message,
                timestamp=timestamp,
                payload=payload,
                protocol_run_id=protocol_run_id,
                node_id=node_id,
                child_run_id=child_run_id,
                job_id=job_id,
                artifact_refs=artifact_refs,
                receipt_ref=receipt_ref,
                evidencegate_ref=evidencegate_ref,
                mudo_commit_ref=mudo_commit_ref,
                metadata=metadata,
            )
        )
    elif source_event_type == "run.failed":
        canonical_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type="vertical.run.failed",
                index=index,
                source=source,
                status=status,
                message=message,
                timestamp=timestamp,
                payload=payload,
                protocol_run_id=protocol_run_id,
                node_id=node_id,
                child_run_id=child_run_id,
                job_id=job_id,
                artifact_refs=artifact_refs,
                receipt_ref=receipt_ref,
                evidencegate_ref=evidencegate_ref,
                mudo_commit_ref=mudo_commit_ref,
                metadata=metadata,
            )
        )
    elif source_event_type == "node.completed":
        canonical_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type="protocol.node.started",
                index=index,
                source=source,
                status="started",
                message=f"Node {node_id or 'unknown'} started in compatibility projection.",
                timestamp=timestamp,
                payload=payload,
                protocol_run_id=protocol_run_id,
                node_id=node_id,
                child_run_id=child_run_id,
                job_id=job_id,
                metadata={**metadata, "synthetic_phase": "start"},
                synthetic=True,
            )
        )
        canonical_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type=canonical_event_type,
                index=index,
                source=source,
                status=status,
                message=message,
                timestamp=timestamp,
                payload=payload,
                protocol_run_id=protocol_run_id,
                node_id=node_id,
                child_run_id=child_run_id,
                job_id=job_id,
                artifact_refs=artifact_refs,
                receipt_ref=receipt_ref,
                evidencegate_ref=evidencegate_ref,
                mudo_commit_ref=mudo_commit_ref,
                metadata=metadata,
            )
        )
        for artifact_ref in artifact_refs:
            canonical_events.append(
                _canonical_event(
                    root_run_id=root_run_id,
                    event_type="artifact.produced",
                    index=index,
                    source=source,
                    status="produced",
                    message=f"Artifact produced: {artifact_ref}",
                    timestamp=timestamp,
                    payload={**payload, "artifact_ref": artifact_ref},
                    protocol_run_id=protocol_run_id,
                    node_id=node_id,
                    child_run_id=child_run_id,
                    job_id=job_id,
                    artifact_refs=[artifact_ref],
                    receipt_ref=receipt_ref,
                    metadata={**metadata, "artifact_ref": artifact_ref, "source_event_type": source_event_type},
                )
            )
        if receipt_ref:
            canonical_events.append(
                _canonical_event(
                    root_run_id=root_run_id,
                    event_type="evidence.receipt.emitted",
                    index=index,
                    source=source,
                    status="emitted",
                    message=f"Receipt emitted: {receipt_ref}",
                    timestamp=timestamp,
                    payload=payload,
                    protocol_run_id=protocol_run_id,
                    node_id=node_id,
                    child_run_id=child_run_id,
                    job_id=job_id,
                    artifact_refs=artifact_refs,
                    receipt_ref=receipt_ref,
                    metadata={**metadata, "source_event_type": source_event_type},
                )
            )
    else:
        canonical_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type=canonical_event_type,
                index=index,
                source=source,
                status=status,
                message=message,
                timestamp=timestamp,
                payload=payload,
                protocol_run_id=protocol_run_id,
                node_id=node_id,
                child_run_id=child_run_id,
                job_id=job_id,
                artifact_refs=artifact_refs,
                receipt_ref=receipt_ref,
                evidencegate_ref=evidencegate_ref,
                mudo_commit_ref=mudo_commit_ref,
                metadata=metadata,
            )
        )

    return canonical_events


def _synthesized_support_events(
    *,
    root_run_id: str,
    protocol_run_id: str | None,
    source_receipt: Mapping[str, Any] | None,
    source_events: list[dict[str, Any]],
) -> list[VerticalTranscriptEvent]:
    support_events: list[VerticalTranscriptEvent] = []
    first_timestamp = _clean_string(source_events[0].get("timestamp") if source_events else None) or _utc_now()
    source_protocol_id = _clean_string((source_receipt or {}).get("protocol_id")) or protocol_run_id or "unknown-protocol"
    support_events.append(
        _canonical_event(
            root_run_id=root_run_id,
            event_type="driver.session.started",
            index=0,
            source="vertical_transcript_standardizer_v3",
            status="started",
            message="Driver session compatibility projection started for vertical transcript bridge.",
            timestamp=first_timestamp,
            payload={"protocol_id": source_protocol_id},
            protocol_run_id=protocol_run_id,
            synthetic=True,
            metadata={"projection_kind": "driver_session", "protocol_id": source_protocol_id},
        )
    )
    support_events.append(
        _canonical_event(
            root_run_id=root_run_id,
            event_type="protocol.run.started",
            index=0,
            source="vertical_transcript_standardizer_v3",
            status="started",
            message=f"Protocol run projected for {source_protocol_id}.",
            timestamp=first_timestamp,
            payload={"protocol_id": source_protocol_id},
            protocol_run_id=protocol_run_id or source_protocol_id,
            synthetic=True,
            metadata={"projection_kind": "protocol_run", "protocol_id": source_protocol_id},
        )
    )

    blocked_node_ids = []
    if source_receipt:
        frontier_after = dict(source_receipt.get("raw_run_receipt") or {}).get("frontier_after") or {}
        blocked_node_ids = [str(item) for item in frontier_after.get("blocked_node_ids") or [] if str(item).strip()]
        for node_id in blocked_node_ids:
            support_events.append(
                _canonical_event(
                    root_run_id=root_run_id,
                    event_type="protocol.node.blocked",
                    index=0,
                    source="vertical_transcript_standardizer_v3",
                    status="blocked",
                    message=f"Node blocked in compatibility projection: {node_id}",
                    timestamp=first_timestamp,
                    payload={"node_id": node_id},
                    protocol_run_id=protocol_run_id,
                    node_id=node_id,
                    synthetic=True,
                    metadata={"projection_kind": "blocked_node", "node_id": node_id},
                )
            )
    if not blocked_node_ids:
        support_events.append(
            _canonical_event(
                root_run_id=root_run_id,
                event_type="protocol.node.blocked",
                index=0,
                source="vertical_transcript_standardizer_v3",
                status="not_applicable",
                message="No blocked nodes present in source transcript.",
                timestamp=first_timestamp,
                payload={"blocked_node_ids": []},
                protocol_run_id=protocol_run_id,
                synthetic=True,
                metadata={"projection_kind": "blocked_node", "present": False},
            )
        )

    support_events.append(
        _canonical_event(
            root_run_id=root_run_id,
            event_type="evidencegate.verdict",
            index=0,
            source="vertical_transcript_standardizer_v3",
            status="not_applicable",
            message="No EvidenceGate verdict present in source transcript; compatibility bridge supports it.",
            timestamp=first_timestamp,
            payload={"present": False},
            protocol_run_id=protocol_run_id,
            evidencegate_ref=None,
            synthetic=True,
            metadata={"projection_kind": "evidencegate", "present": False},
        )
    )
    support_events.append(
        _canonical_event(
            root_run_id=root_run_id,
            event_type="mudo.commit.linked",
            index=0,
            source="vertical_transcript_standardizer_v3",
            status="not_applicable",
            message="No MUDO commit linked in source transcript; compatibility bridge supports commit refs.",
            timestamp=first_timestamp,
            payload={"present": False},
            protocol_run_id=protocol_run_id,
            mudo_commit_ref=None,
            synthetic=True,
            metadata={"projection_kind": "mudo_commit", "present": False},
        )
    )
    return support_events


def parse_vertical_transcript_jsonl(
    transcript_path: str | Path,
    *,
    receipt_path: str | Path | None = None,
) -> VerticalTranscriptParseResult:
    transcript_path = Path(transcript_path)
    source_events = _read_jsonl(transcript_path)
    source_receipt = _read_json(Path(receipt_path)) if receipt_path is not None else None
    root_run_id = _clean_string(source_events[0].get("root_run_id") if source_events else None)
    if not root_run_id:
        raise VerticalTranscriptStandardizationError("Transcript is empty or missing root_run_id")
    protocol_run_id = _clean_string((source_receipt or {}).get("protocol_id")) or None
    canonical_events: list[VerticalTranscriptEvent] = []
    malformed_entries: list[dict[str, Any]] = []
    for index, payload in enumerate(source_events, start=1):
        try:
            canonical_events.extend(
                _source_event_to_canonical(payload, index=index, protocol_run_id=protocol_run_id)
            )
        except VerticalTranscriptStandardizationError as exc:
            malformed_entries.append({"line": index, "error": str(exc), "payload": payload})
    canonical_events = _synthesized_support_events(
        root_run_id=root_run_id,
        protocol_run_id=protocol_run_id,
        source_receipt=source_receipt,
        source_events=source_events,
    ) + canonical_events

    event_counts = Counter(event.event_type for event in canonical_events)
    artifact_refs = _dedupe_strings(
        artifact_ref
        for event in canonical_events
        for artifact_ref in event.artifact_refs
        if _clean_string(artifact_ref)
    )
    receipt_refs = _dedupe_strings(
        receipt_ref
        for event in canonical_events
        for receipt_ref in [event.receipt_ref]
        if _clean_string(receipt_ref)
    )
    grouped_counts = Counter(event.root_run_id for event in canonical_events)
    terminal_status = next(
        (
            event.status
            for event in reversed(canonical_events)
            if event.event_type in {"vertical.run.completed", "vertical.run.failed"}
        ),
        "unknown",
    )
    return VerticalTranscriptParseResult(
        source_path=str(transcript_path),
        source_receipt_path=str(receipt_path) if receipt_path is not None else None,
        root_run_id=root_run_id,
        protocol_run_id=protocol_run_id,
        source_event_count=len(source_events),
        canonical_event_count=len(canonical_events),
        terminal_status=terminal_status,
        event_counts=dict(event_counts),
        grouped_counts=dict(grouped_counts),
        artifact_refs=artifact_refs,
        receipt_refs=receipt_refs,
        malformed_entries=malformed_entries,
        canonical_events=canonical_events,
        source_events=source_events,
    )


def validate_vertical_transcript_jsonl(
    transcript_path: str | Path,
    *,
    receipt_path: str | Path | None = None,
) -> VerticalTranscriptParseResult:
    parse_result = parse_vertical_transcript_jsonl(transcript_path, receipt_path=receipt_path)
    if parse_result.malformed_entries:
        first_entry = parse_result.malformed_entries[0]
        raise VerticalTranscriptStandardizationError(
            f"Malformed transcript entry on line {first_entry['line']}: {first_entry['error']}"
        )
    return parse_result


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = _clean_string(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def build_vertical_transcript_contract() -> dict[str, Any]:
    return {
        "contract_id": "vertical.transcript.contract.v3",
        "program": PROGRAM_ID,
        "version": "3.0.0",
        "format": "jsonl",
        "required_fields": list(CANONICAL_REQUIRED_FIELDS),
        "optional_fields": [
            "node_id",
            "child_run_id",
            "protocol_run_id",
            "job_id",
            "artifact_refs",
            "receipt_ref",
            "evidencegate_ref",
            "mudo_commit_ref",
            "metadata",
        ],
        "required_event_types": list(CANONICAL_REQUIRED_EVENT_TYPES),
        "event_type_mapping": dict(SOURCE_TO_CANONICAL_EVENT_TYPES),
        "projection_targets": {
            "api_timeline": {
                "fields": ["type", "timestamp", "source", "receipt_ref", "artifact_ref", "message", "severity"],
            },
            "shell_view": {
                "fields": ["run_id", "protocol_id", "status", "node_count", "completed_count", "blocked_count", "receipt_ref", "projection_ref", "artifacts", "errors"],
            },
            "mudo": {
                "fields": ["mudo_event_ref", "mudo_asset_candidates", "mudo_commit_ref", "readiness_refs"],
            },
            "graph_of_graphs": {
                "fields": ["parent_graph_run_id", "child_graph_run_id", "child_run_id", "node_status", "blocked_evidencegate_path", "artifact_threading"],
            },
        },
    }


def project_vertical_transcript_to_api_timeline(
    parse_result: VerticalTranscriptParseResult,
) -> dict[str, Any]:
    timeline_events: list[dict[str, Any]] = []
    for event in parse_result.canonical_events:
        severity = "info"
        if event.status in {"failed", "blocked"}:
            severity = "error"
        elif event.status in {"not_applicable", "absent"}:
            severity = "debug"
        artifact_ref = event.artifact_refs[0] if event.artifact_refs else ""
        timeline_events.append(
            {
                "type": event.event_type,
                "timestamp": event.timestamp,
                "source": event.source,
                "receipt_ref": event.receipt_ref or "",
                "artifact_ref": artifact_ref,
                "message": event.message,
                "severity": severity,
            }
        )
    return {
        "program": PROGRAM_ID,
        "status": "passed_api_timeline_projection",
        "root_run_id": parse_result.root_run_id,
        "source_path": parse_result.source_path,
        "event_count": len(timeline_events),
        "events": timeline_events,
        "provider_execution": False,
        "md_execution": False,
    }


def project_vertical_transcript_to_shell_view(
    parse_result: VerticalTranscriptParseResult,
    *,
    source_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    node_completed_events = [event for event in parse_result.canonical_events if event.event_type == "protocol.node.completed"]
    blocked_events = [event for event in parse_result.canonical_events if event.event_type == "protocol.node.blocked" and event.status == "blocked"]
    protocol_id = _clean_string((source_receipt or {}).get("protocol_id")) or parse_result.protocol_run_id or ""
    receipt_ref = parse_result.receipt_refs[0] if parse_result.receipt_refs else ""
    errors: list[str] = []
    if parse_result.malformed_entries:
        errors.extend(f"line {entry['line']}: {entry['error']}" for entry in parse_result.malformed_entries)
    if not receipt_ref:
        errors.append("missing_receipt_ref")
    return {
        "program": PROGRAM_ID,
        "status": parse_result.terminal_status,
        "run_id": parse_result.root_run_id,
        "protocol_id": protocol_id or None,
        "node_count": len(node_completed_events),
        "completed_count": len(node_completed_events),
        "blocked_count": len(blocked_events),
        "receipt_ref": receipt_ref,
        "projection_ref": f"{PROGRAM_ID}/shell/{parse_result.root_run_id}",
        "artifacts": list(parse_result.artifact_refs),
        "errors": errors,
        "provider_execution": False,
        "md_execution": False,
    }


def project_vertical_transcript_to_mudo(
    parse_result: VerticalTranscriptParseResult,
    *,
    source_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_candidates = [
        {
            "artifact_ref": artifact_ref,
            "artifact_kind": "transcript_artifact",
            "source_event_type": "artifact.produced",
            "source_run_id": parse_result.root_run_id,
        }
        for artifact_ref in parse_result.artifact_refs
    ]
    commit_ref = _clean_string((source_receipt or {}).get("mudo_commit_ref")) or None
    readiness_refs = list(parse_result.receipt_refs)
    return {
        "program": PROGRAM_ID,
        "status": "passed_mudo_projection" if artifact_candidates else "partial_mudo_projection",
        "root_run_id": parse_result.root_run_id,
        "mudo_event_ref": f"mudo-event:{parse_result.root_run_id}",
        "mudo_asset_candidates": artifact_candidates,
        "mudo_commit_ref": commit_ref,
        "readiness_refs": readiness_refs,
        "provider_execution": False,
        "md_execution": False,
    }


def audit_vertical_transcript_gog_compatibility(
    parse_result: VerticalTranscriptParseResult,
) -> dict[str, Any]:
    support_types = {event.event_type for event in parse_result.canonical_events}
    represented = {
        "parent_graph_run": bool(parse_result.root_run_id),
        "child_graph_run": any(bool(event.child_run_id) for event in parse_result.canonical_events),
        "child_run_id": any(bool(event.child_run_id) for event in parse_result.canonical_events),
        "node_status": any(event.event_type in {"protocol.node.started", "protocol.node.completed", "protocol.node.blocked"} for event in parse_result.canonical_events),
        "blocked_evidencegate_path": any(event.event_type == "evidencegate.verdict" for event in parse_result.canonical_events),
        "artifact_threading": bool(parse_result.artifact_refs),
    }
    return {
        "program": PROGRAM_ID,
        "status": "passed_gog_compatibility_audit" if all(represented.values()) else "partial_gog_compatibility_blocked",
        "root_run_id": parse_result.root_run_id,
        "compatibility": represented,
        "supported_event_types": sorted(support_types),
        "blocked_reason": None if all(represented.values()) else "one_or_more_required_gog_dimensions_not_observed_in_source_transcript",
        "provider_execution": False,
        "md_execution": False,
    }


def emit_vertical_transcript_standardization_package(
    output_dir: str | Path,
    *,
    transcript_path: str | Path,
    receipt_path: str | Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = Path(transcript_path)
    receipt_path = Path(receipt_path)
    source_receipt = _read_json(receipt_path)

    parse_result = validate_vertical_transcript_jsonl(transcript_path, receipt_path=receipt_path)
    contract = build_vertical_transcript_contract()
    reader_receipt = parse_result.to_receipt()
    api_timeline = project_vertical_transcript_to_api_timeline(parse_result)
    shell_view = project_vertical_transcript_to_shell_view(parse_result, source_receipt=source_receipt)
    mudo_projection = project_vertical_transcript_to_mudo(parse_result, source_receipt=source_receipt)
    gog_audit = audit_vertical_transcript_gog_compatibility(parse_result)

    _write_json(output_dir / CONTRACT_FILE, contract)
    _write_json(output_dir / READER_RECEIPT_FILE, reader_receipt)
    _write_json(output_dir / API_TIMELINE_FILE, api_timeline)
    _write_json(output_dir / SHELL_VIEW_FILE, shell_view)
    _write_json(output_dir / MUDO_PROJECTION_FILE, mudo_projection)
    _write_json(output_dir / GOG_AUDIT_FILE, gog_audit)

    summary_lines = [
        PROGRAM_ID,
        f"root_run_id: {parse_result.root_run_id}",
        f"terminal_status: {parse_result.terminal_status}",
        f"source_event_count: {parse_result.source_event_count}",
        f"canonical_event_count: {parse_result.canonical_event_count}",
        f"api_timeline_event_count: {len(api_timeline['events'])}",
        f"shell_node_count: {shell_view['node_count']}",
        f"mudo_asset_candidates: {len(mudo_projection['mudo_asset_candidates'])}",
        f"gog_status: {gog_audit['status']}",
        f"provider_execution: false",
        f"md_execution: false",
    ]
    (output_dir / SUMMARY_FILE).write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    return {
        "contract": contract,
        "reader_receipt": reader_receipt,
        "api_timeline": api_timeline,
        "shell_view": shell_view,
        "mudo_projection": mudo_projection,
        "gog_audit": gog_audit,
        "provider_execution": False,
        "md_execution": False,
    }


__all__ = [
    "PROGRAM_ID",
    "CONTRACT_FILE",
    "READER_RECEIPT_FILE",
    "API_TIMELINE_FILE",
    "SHELL_VIEW_FILE",
    "MUDO_PROJECTION_FILE",
    "GOG_AUDIT_FILE",
    "VerticalTranscriptEvent",
    "VerticalTranscriptParseResult",
    "VerticalTranscriptStandardizationError",
    "audit_vertical_transcript_gog_compatibility",
    "build_vertical_transcript_contract",
    "emit_vertical_transcript_standardization_package",
    "parse_vertical_transcript_jsonl",
    "validate_vertical_transcript_jsonl",
    "project_vertical_transcript_to_api_timeline",
    "project_vertical_transcript_to_mudo",
    "project_vertical_transcript_to_shell_view",
]