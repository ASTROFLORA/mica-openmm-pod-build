"""
MICA Driver Persistence Layer (Phase 2)
========================================

Sub-package providing file-based and Timescale-backed persistence
for the AgenticDriver:

  - **saga** — Append-only saga event logs (JSONL + optional Timescale)
  - **manifests** — Run manifest & report-card provenance files
  - **snapshots** — Named session snapshot save / restore
  - **conversation** — Structured conversation log (JSON)

Functions are standalone (explicit parameters, no ``self``).
"""

from .saga import (
    saga_log_path,
    append_saga_event,
    append_saga_event_timescale,
    get_timescale_store,
    best_effort_saga_mcp_metrics,
)
from .manifests import (
    run_manifest_dir,
    best_effort_git_info,
    best_effort_versions,
    write_run_manifest,
    write_report_card,
)
from .snapshots import (
    snapshot_dir,
    sha256_file,
    save_session_snapshot,
    restore_session_snapshot,
)
from .conversation import (
    conversation_log_path,
    safe_result_for_log,
    stringify_message_content,
    append_conversation_log,
)
from .runtime_observability import (
    communication_store_path,
    persist_communication_store,
    build_runtime_telemetry_emitter,
    emit_runtime_status,
    emit_runtime_error,
    runtime_error_artifact_base_dir,
    runtime_error_manifest_path,
    build_runtime_error_artifact_writer,
    is_retryable_runtime_exception,
    persist_runtime_error_artifact,
)
from .gcs_sync import DriverArtifactSync
from .driver_persistence_facade import DriverPersistenceFacade
from .driver_artifact_sync_facade import DriverArtifactSyncFacade
from .driver_conversation_log_facade import DriverConversationLogFacade
from .driver_session_artifact_facade import DriverSessionArtifactFacade
from .driver_memory_backend_facade import DriverMemoryBackendFacade
from .driver_snapshot_facade import DriverSnapshotFacade
__all__ = [
    # saga
    "saga_log_path",
    "append_saga_event",
    "append_saga_event_timescale",
    "get_timescale_store",
    "best_effort_saga_mcp_metrics",
    # manifests
    "run_manifest_dir",
    "best_effort_git_info",
    "best_effort_versions",
    "write_run_manifest",
    "write_report_card",
    # snapshots
    "snapshot_dir",
    "sha256_file",
    "save_session_snapshot",
    "restore_session_snapshot",
    # conversation
    "conversation_log_path",
    "safe_result_for_log",
    "stringify_message_content",
    "append_conversation_log",
    # runtime observability
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
    # gcs sync
    "DriverArtifactSync",
    # facade
    "DriverPersistenceFacade",
    "DriverArtifactSyncFacade",
    "DriverConversationLogFacade",
    "DriverSessionArtifactFacade",
    "DriverMemoryBackendFacade",
    "DriverSnapshotFacade",
]
