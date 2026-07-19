"""src/mica/sim/cg_martini/bootstrap_wrapper.py — BootstrapWrapper (P0.6).

Authority:
  Lane CG/Martini — SLICE CG-P0.6, cierra GAP-CG3.1
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D4: failure_domain ∈ {provider_bootstrap, cg_engine, unknown}

Scope:
  - Read existing GCS bootstrap artifacts from disk/local
  - Project to ReceiptCore with standardized phase timestamps
  - Never modifies the original worker (main_gcs.py)

Artifact sources (confirmed against main_gcs.py L185-207):
  worker_entrypoint_started.json  → container_started
  bootstrap_heartbeat.json        → first_heartbeat
  gcs_write_probe.json            → gcs_reachable
  _emit_crash_diagnostic()        → failed_at_phase (stderr, not GCS)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from mica.provenance.receipts import ReceiptCore, ReceiptHashes, ReceiptRefs

logger = logging.getLogger(__name__)

_receipt_counter = 0


def _next_receipt_id(kind: str) -> str:
    global _receipt_counter
    _receipt_counter += 1
    return f"{kind}_{_receipt_counter}_{datetime.now(tz=timezone.utc).timestamp():.0f}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# The 3 expected bootstrap artifact filenames from main_gcs.py
BOOTSTRAP_ARTIFACTS = [
    "worker_entrypoint_started.json",
    "bootstrap_heartbeat.json",
    "gcs_write_probe.json",
]

# Mapping from artifact event → phase name used in ReceiptCore
EVENT_TO_PHASE: dict[str, str] = {
    "worker_entrypoint_started": "container_started",
    "bootstrap_heartbeat": "first_heartbeat",
    "gcs_write_probe": "gcs_reachable",
}

# Schema versions expected (from main_gcs.py)
EXPECTED_SCHEMAS = {
    "worker_entrypoint_started": "worker_entrypoint_started_v1",
    "bootstrap_heartbeat": "bootstrap_heartbeat_v1",
    "gcs_write_probe": "gcs_write_probe_v1",
}


class BootstrapPhasesPayload(BaseModel):
    """Payload del wrapper de bootstrap — dentro de ReceiptCore.payload.

    Doctrina D1: NO es schema aislado.
    """

    job_id: str = Field(default="", description="MICA_JOB_ID del worker.")
    container_started_at: Optional[str] = Field(default=None, description="Timestamp del primer artifact.")
    first_heartbeat_at: Optional[str] = Field(default=None, description="Timestamp del heartbeat.")
    gcs_reachable_at: Optional[str] = Field(default=None, description="Timestamp del probe GCS.")
    failed_at_phase: Optional[str] = Field(default=None, description="Phase from crash diagnostic, si existe.")
    failure_domain: str = Field(default="provider_bootstrap", description="Siempre provider_bootstrap para este wrapper.")
    error_message: Optional[str] = Field(default=None, description="Error msg from crash diagnostic, si existe.")
    raw_gcs_refs: dict[str, str] = Field(
        default_factory=dict,
        description="Original GCS paths for audit trail.",
    )
    all_phases_completed: bool = Field(default=False, description="True if all 3 artifacts found and no crash.")
    execution_status: str = "completed"
    validation_errors: list[str] = Field(default_factory=list)


class BootstrapWrapper:
    """Reads existing GCS bootstrap artifacts and wraps into ReceiptCore.

    Pure reader — never modifies main_gcs.py or the worker.
    """

    def __init__(
        self,
        workspace_id: str = "cg_martini",
        actor_id: str = "system",
    ):
        self.workspace_id = workspace_id
        self.actor_id = actor_id

    def read_and_wrap(
        self,
        job_id: str,
        bootstrap_dir: str,
        crash_diagnostic_path: Optional[str] = None,
    ) -> ReceiptCore:
        """Read bootstrap artifacts from a local directory and wrap into ReceiptCore.

        Args:
            job_id: The MICA_JOB_ID for this run.
            bootstrap_dir: Local path containing the 3 bootstrap artifacts.
            crash_diagnostic_path: Optional path to crash diagnostic file (JSONL).

        Returns:
            ReceiptCore with BootstrapPhasesPayload.
        """
        errors: list[str] = []
        phases: dict[str, str] = {}
        raw_gcs_refs: dict[str, str] = {}
        job_id_from_artifact = job_id

        bootstrap_path = Path(bootstrap_dir)

        # Read each expected artifact
        for filename in BOOTSTRAP_ARTIFACTS:
            filepath = bootstrap_path / filename
            if not filepath.is_file():
                errors.append(f"Missing bootstrap artifact: {filename}")
                continue
            try:
                data = json.loads(filepath.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError) as exc:
                errors.append(f"Failed to parse {filename}: {exc}")
                continue

            # Extract event type
            event = data.get("event", "")
            phase = EVENT_TO_PHASE.get(event)
            produced_at = data.get("produced_at", "")

            # Validate schema version
            schema = data.get("schema_version", "")
            expected_schema = EXPECTED_SCHEMAS.get(event)
            if expected_schema and schema != expected_schema:
                errors.append(f"{filename}: expected schema {expected_schema}, got {schema}")

            if phase and produced_at:
                phases[phase] = produced_at

            # Capture GCS object path
            object_path = data.get("object_path", "")
            if object_path:
                raw_gcs_refs[filename] = f"gs://{object_path}"

            # Capture job_id from artifact if not provided
            if not job_id:
                job_id_from_artifact = data.get("job_id", "")

        # Read crash diagnostic if available
        failed_at_phase: Optional[str] = None
        error_message: Optional[str] = None
        if crash_diagnostic_path and os.path.isfile(crash_diagnostic_path):
            try:
                with open(crash_diagnostic_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            crash = json.loads(line)
                            if crash.get("event") == "mica_worker_crash_diagnostic":
                                failed_at_phase = crash.get("phase", "unknown")
                                error_message = crash.get("error", "")
                        except json.JSONDecodeError:
                            pass
            except OSError as exc:
                errors.append(f"Cannot read crash diagnostic: {exc}")

        # Determine if all phases completed
        all_phases = {"container_started", "first_heartbeat", "gcs_reachable"}
        completed_phases = set(phases.keys())
        all_phases_completed = all_phases.issubset(completed_phases) and failed_at_phase is None

        payload = BootstrapPhasesPayload(
            job_id=job_id_from_artifact,
            container_started_at=phases.get("container_started"),
            first_heartbeat_at=phases.get("first_heartbeat"),
            gcs_reachable_at=phases.get("gcs_reachable"),
            failed_at_phase=failed_at_phase,
            failure_domain="provider_bootstrap",
            error_message=error_message,
            raw_gcs_refs=raw_gcs_refs,
            all_phases_completed=all_phases_completed,
            execution_status="completed",
            validation_errors=errors,
        )

        status = (
            "completed"
            if all_phases_completed
            else (
                "failed_bootstrap"
                if failed_at_phase is not None
                else "incomplete"
            )
        )

        return self._build_receipt(
            kind="cg_bootstrap",
            status=status,
            operation_name="bootstrap_read_and_wrap",
            payload=payload,
            artifact_refs=list(raw_gcs_refs.values()),
            content_hash=f"bootstrap_{job_id_from_artifact}",
        )

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: BootstrapPhasesPayload,
        artifact_refs: Optional[list[str]] = None,
        content_hash: str = "",
    ) -> ReceiptCore:
        receipt_id = _next_receipt_id(kind)
        return ReceiptCore(
            receipt_id=receipt_id,
            kind=kind,
            status=status,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            operation_name=operation_name,
            refs=ReceiptRefs(
                output_refs=[],
                artifact_refs=artifact_refs or [],
            ),
            hashes=ReceiptHashes(
                request_hash="",
                output_hash="",
                content_hash=content_hash,
            ),
            started_at=_now_iso(),
            ended_at=_now_iso(),
            trace_id=f"trace_{receipt_id}",
            payload=payload.model_dump(),
        )
