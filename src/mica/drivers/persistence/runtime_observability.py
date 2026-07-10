"""Persistence helpers for runtime observability projections.

These helpers own file layout, artifact persistence, and runtime telemetry
policy for the active driver lifecycle. They intentionally accept explicit
parameters so the driver remains the runtime owner while communication-layer
objects stay as compatibility adapters.
"""

from __future__ import annotations

import asyncio
import re
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..utils import _redact_obj, _redact_text, _truncate_text


def communication_store_path(checkpoint_dir: str, session_id: str) -> Path:
    """Return the persisted compatibility-bus store path for a session."""

    out_dir = Path(checkpoint_dir).resolve() / "communication_bus"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sid = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "unknown"))
    return out_dir / f"{safe_sid}.json"


def persist_communication_store(*, bus: Any, checkpoint_dir: str, session_id: str) -> Optional[Path]:
    """Persist the compatibility-bus message store if the bus supports it."""

    if bus is None:
        return None
    save_store = getattr(bus, "save_store", None)
    if not callable(save_store):
        return None
    try:
        target = communication_store_path(checkpoint_dir, session_id)
        save_store(target)
        return target
    except Exception:
        return None


def build_runtime_telemetry_emitter(
    *,
    message_bus: Any,
    persona: Any,
    roadmap_phase: str,
    goal: str,
    agent_name: str = "driver",
    subsystem: str = "runtime",
) -> Any:
    """Create the compatibility telemetry emitter for runtime lifecycle events."""

    from bsm.communication.observability import RuntimeTelemetryEmitter

    return RuntimeTelemetryEmitter(
        persona=persona,
        roadmap_phase=roadmap_phase,
        goal=goal,
        message_bus=message_bus,
        agent_name=agent_name,
        subsystem=subsystem,
    )


async def emit_runtime_status(
    *,
    emitter: Any,
    session_id: str,
    run_id: str,
    phase: str,
    status: str,
    details: Optional[str] = None,
    mode: Optional[str] = None,
    severity: str = "info",
    metrics: Optional[Dict[str, Any]] = None,
    artifact_refs: Optional[Iterable[str]] = None,
    evidence_refs: Optional[Iterable[str]] = None,
    source_ids: Optional[Iterable[str]] = None,
    agent_name: str = "driver",
    subsystem: str = "runtime",
) -> None:
    """Emit a runtime status projection through the compatibility emitter."""

    if emitter is None:
        return
    await emitter.emit_status(
        phase=phase,
        status=status,
        details=details,
        metrics=metrics or {},
        context={"mode": mode} if mode else {},
        severity=severity,
        subsystem=subsystem,
        session_id=session_id,
        run_id=run_id,
        agent_name=agent_name,
        artifact_refs=list(artifact_refs or []),
        evidence_refs=list(evidence_refs or []),
        source_ids=list(source_ids or []),
    )


async def emit_runtime_error(
    *,
    emitter: Any,
    session_id: str,
    run_id: str,
    phase: str,
    error_type: str,
    message: str,
    traceback_text: Optional[str] = None,
    artifact_path: Optional[str] = None,
    rescue_suggestion: Optional[str] = None,
    mode: Optional[str] = None,
    retryable: Optional[bool] = None,
    artifact_refs: Optional[Iterable[str]] = None,
    evidence_refs: Optional[Iterable[str]] = None,
    agent_name: str = "driver",
    subsystem: str = "runtime",
) -> None:
    """Emit a runtime error projection through the compatibility emitter."""

    if emitter is None:
        return
    refs = list(artifact_refs or [])
    if artifact_path and artifact_path not in refs:
        refs.append(artifact_path)
    await emitter.emit_error(
        phase=phase,
        error_type=error_type,
        message=message,
        traceback_text=traceback_text,
        artifact_path=artifact_path,
        rescue_suggestion=rescue_suggestion,
        context={"mode": mode} if mode else {},
        severity="error",
        subsystem=subsystem,
        session_id=session_id,
        run_id=run_id,
        agent_name=agent_name,
        artifact_refs=refs,
        evidence_refs=list(evidence_refs or []),
        retryable=retryable,
    )


def runtime_error_artifact_base_dir(checkpoint_dir: str) -> Path:
    """Return the runtime error-artifact base directory."""

    base_directory = Path(checkpoint_dir).resolve() / "error_artifacts"
    base_directory.mkdir(parents=True, exist_ok=True)
    return base_directory


def runtime_error_manifest_path(checkpoint_dir: str) -> Path:
    """Return the runtime error manifest path."""

    return runtime_error_artifact_base_dir(checkpoint_dir) / "error_manifest.jsonl"


def build_runtime_error_artifact_writer(*, checkpoint_dir: str, file_prefix: str = "runtime_error") -> Tuple[Any, str]:
    """Create the compatibility error-artifact writer and manifest path."""

    from bsm.communication.observability import RuntimeErrorArtifactWriter

    base_directory = runtime_error_artifact_base_dir(checkpoint_dir)
    writer = RuntimeErrorArtifactWriter(
        base_directory=str(base_directory),
        file_prefix=file_prefix,
    )
    return writer, str(runtime_error_manifest_path(checkpoint_dir))


def is_retryable_runtime_exception(exc: Exception) -> bool:
    """Return whether a top-level runtime exception looks retryable."""

    return isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError))


def persist_runtime_error_artifact(
    *,
    writer: Any,
    manifest_path: Optional[str],
    session_id: str,
    run_id: str,
    phase: str,
    exc: Exception,
    mode: Optional[str],
    user_query: Optional[str],
    evidence_refs: Optional[List[str]] = None,
    artifact_refs: Optional[List[str]] = None,
    agent_name: str = "driver",
    subsystem: str = "runtime",
) -> Any:
    """Persist a structured runtime error artifact through the compatibility writer."""

    if writer is None:
        return None
    safe_query = _truncate_text(_redact_text(str(user_query or "")), max_len=1000)
    context = {
        "mode": mode,
        "subsystem": subsystem,
        "user_query": safe_query,
    }
    return writer.persist(
        phase=phase,
        error_type=type(exc).__name__,
        message=_truncate_text(_redact_text(str(exc)), max_len=2000),
        traceback_text=traceback.format_exc(),
        context=context,
        session_id=session_id,
        run_id=run_id,
        agent_name=agent_name,
        severity="error",
        retryable=is_retryable_runtime_exception(exc),
        subsystem=subsystem,
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        exception_chain=[type(exc).__name__, str(exc)],
        redact_payload=_redact_obj,
        manifest_path=manifest_path,
    )


__all__ = [
    "communication_store_path",
    "persist_communication_store",
    "build_runtime_telemetry_emitter",
    "emit_runtime_status",
    "emit_runtime_error",
    "runtime_error_artifact_base_dir",
    "runtime_error_manifest_path",
    "build_runtime_error_artifact_writer",
    "is_retryable_runtime_exception",
    "persist_runtime_error_artifact",
]