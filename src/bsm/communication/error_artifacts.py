"""Utilities for persisting structured error artifacts for telemetry pipelines."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from uuid import uuid4


@dataclass
class ArtifactRecord:
    """Represents a persisted error artifact."""

    path: str
    rescue_hint: Optional[str]
    payload: Dict[str, Any]
    manifest_path: Optional[str] = None


class ErrorArtifactWriter:
    """Writes structured error artifacts to disk for downstream analysis."""

    def __init__(
        self,
        base_directory: Optional[str] = None,
        file_prefix: str = "biodynamo_error",
    ) -> None:
        configured_dir = base_directory or os.getenv("BSM_ERROR_ARTIFACT_DIR")
        self.base_directory = Path(configured_dir or "artifacts/error")
        self.file_prefix = file_prefix

    def persist(
        self,
        *,
        phase: str,
        error_type: str,
        message: str,
        traceback_text: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        program_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        tool_name: Optional[str] = None,
        severity: str = "error",
        retryable: Optional[bool] = None,
        subsystem: str = "runtime",
        artifact_refs: Optional[Iterable[Any]] = None,
        evidence_refs: Optional[Iterable[Any]] = None,
        exception_chain: Optional[Iterable[Any]] = None,
        redact_payload: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        manifest_path: Optional[str] = None,
    ) -> ArtifactRecord:
        """Persist the error context to a JSON artifact and return metadata."""

        timestamp = datetime.now(timezone.utc)
        target_directory = self.base_directory / timestamp.strftime("%Y/%m/%d")
        target_directory.mkdir(parents=True, exist_ok=True)

        file_name = (
            f"{self.file_prefix}_{timestamp.strftime('%H%M%S')}_{uuid4().hex[:8]}.json"
        )
        artifact_path = target_directory / file_name

        payload = {
            "phase": phase,
            "error_type": error_type,
            "message": message,
            "traceback": traceback_text,
            "context": context or {},
            "session_id": session_id,
            "run_id": run_id,
            "program_id": program_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "severity": severity,
            "retryable": retryable,
            "subsystem": subsystem,
            "artifact_refs": [str(value) for value in (artifact_refs or []) if str(value or "").strip()],
            "evidence_refs": [str(value) for value in (evidence_refs or []) if str(value or "").strip()],
            "exception_chain": [str(value) for value in (exception_chain or []) if str(value or "").strip()],
            "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
        }

        if callable(redact_payload):
            try:
                payload = redact_payload(payload)
            except Exception:
                pass

        with artifact_path.open("w", encoding="utf-8") as artifact_file:
            json.dump(payload, artifact_file, indent=2, sort_keys=True)

        rescue_hint = self._derive_rescue_hint(error_type, context)
        written_manifest_path = self._append_manifest_entry(
            manifest_path=manifest_path,
            artifact_path=str(artifact_path),
            payload=payload,
        )
        return ArtifactRecord(
            path=str(artifact_path),
            rescue_hint=rescue_hint,
            payload=payload,
            manifest_path=written_manifest_path,
        )

    def _append_manifest_entry(
        self,
        *,
        manifest_path: Optional[str],
        artifact_path: str,
        payload: Dict[str, Any],
    ) -> Optional[str]:
        if not manifest_path:
            return None
        manifest = Path(manifest_path)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp_utc": payload.get("timestamp_utc"),
            "artifact_path": artifact_path,
            "session_id": payload.get("session_id"),
            "run_id": payload.get("run_id"),
            "error_type": payload.get("error_type"),
            "severity": payload.get("severity"),
            "retryable": payload.get("retryable"),
            "subsystem": payload.get("subsystem"),
        }
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return str(manifest)

    def _derive_rescue_hint(
        self,
        error_type: str,
        context: Optional[Dict[str, Any]],
    ) -> str:
        """Generate a lightweight rescue hint based on the error characteristics."""

        hint_map = {
            "runtimeerror": "Inspect the runtime artifact, then rerun with diagnostics enabled for the failing phase.",
            "valueerror": "Validate input payloads and normalized evidence fields before retrying the workflow.",
            "importerror": "Verify the required module or optional dependency is available in the active runtime.",
            "timeouterror": "Increase the timeout or retry the upstream transport after checking provider health.",
            "connectionerror": "Check the transport endpoint or MCP server availability before retrying.",
            "permissionerror": "Review credentials, safety policy, and filesystem permissions for this run.",
        }

        normalized_error = str(error_type or "").strip().lower()
        if normalized_error in hint_map:
            return hint_map[normalized_error]

        manifest_source = (context or {}).get("manifest_origin")
        if manifest_source == "superdynamo_migration":
            return "Review SuperDynamo migration outputs for missing fields before retry."

        subsystem = str((context or {}).get("subsystem") or "").strip().lower()
        if subsystem == "review":
            return "Inspect reviewer verdict and unsupported claims, then rerun the revision cycle with tighter evidence queries."
        if subsystem == "mcp":
            return "Inspect MCP server health, tool permissions, and transport connectivity before retrying."

        return "Review artifact details and retry with fallback to SuperDynamo if needed."


RuntimeErrorArtifactWriter = ErrorArtifactWriter
