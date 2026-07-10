from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from mica.api_v1.ws_ticket import verify_ws_ticket

from .bcif_runtime import encode_cif_to_bcif, encode_pdb_to_bcif
from .local_preview_consumer import (
    build_artifact_synced_event,
    build_local_preview_manifest,
    build_preview_available_event,
    consume_bcif_preview_artifact,
    validate_preview_event,
    write_event_fixtures_jsonl,
)
from .local_preview_ui_adapter import (
    PreviewUIState,
    apply_preview_event_to_ui_state,
    initialize_preview_ui_state,
)


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")




class PreviewReplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    system_id: str
    representation: Literal["all_atom", "coarse_grained"]
    system_name: str
    event_source_path: str
    preview_manifest_path: str | None = None
    batch_results_path: str | None = None
    replay_speed_multiplier: float = Field(default=1.0, gt=0.0)
    preserve_original_timestamps: bool = True
    representation_filter: Literal["all_atom", "coarse_grained", "both"] = "both"
    ticket_required_in_production: bool = True
    expected_ticket_scope: str = "preview:read"


class PreviewFrontendHandshakeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_status: str
    subscribed: bool
    job_id: str = ""
    run_id: str
    system_name: str = ""
    system_id: str
    representation: Literal["all_atom", "coarse_grained"]
    preview_status: str
    current_frame: int | None = Field(default=None, ge=0)
    frame_count: int | None = Field(default=None, ge=0)
    time_ps: float | None = Field(default=None, ge=0.0)
    bcif_ref: str = ""
    canonical_artifact_refs: dict[str, str] = Field(default_factory=dict)
    event_log: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    actions_available: list[str] = Field(default_factory=list)
    replay_completed: bool = False
    last_event_type: str = ""
    preview_not_canonical: bool = True
    realtime_ws_claim: bool = False


class PreviewReplaySession:
    def __init__(self, config: PreviewReplayConfig):
        self.config = config
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._replay_started_at: str | None = None

    def pause(self) -> None:
        self._pause_gate.clear()

    def resume(self) -> None:
        self._pause_gate.set()

    def _load_events(self) -> list[dict[str, Any]]:
        source = Path(self.config.event_source_path).expanduser().resolve()
        if source.is_dir():
            files = sorted(path for path in source.glob("*.jsonl") if path.is_file())
        else:
            files = [source]
        events: list[dict[str, Any]] = []
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                validate_preview_event(payload)
                events.append(payload)
        return events

    def _event_matches_filter(self, event: dict[str, Any]) -> bool:
        if self.config.representation_filter == "both":
            return True
        representation = str((event.get("data") or {}).get("representation") or "").strip()
        return representation == self.config.representation_filter

    async def replay_events(self) -> list[dict[str, Any]]:
        self._replay_started_at = _utcnow()
        replay_events: list[dict[str, Any]] = []
        for event in self._load_events():
            if not self._event_matches_filter(event):
                continue
            await self._pause_gate.wait()
            enriched = dict(event)
            if not self.config.preserve_original_timestamps:
                enriched["time"] = _utcnow()
            replay_events.append(enriched)
            if self.config.replay_speed_multiplier != 1.0:
                await asyncio.sleep(0)
        return replay_events


def _default_fixture_root() -> Path:
    return Path(__file__).resolve().parents[3] / ".mica" / "programs" / "REAL_ENGINE_BIODYNAMO_SUPERNOVA" / "subprograms"


def build_default_preview_fixture_registry() -> dict[str, PreviewReplayConfig]:
    root = _default_fixture_root()
    unified = root / "BIODYNAMO_PREVIEW_AA_CG_UNIFIED_CONTRACT_PROOF_V2_20260530"
    batch = root / "BIODYNAMO_CG_BCIF_ENCODER_AND_BATCH_EXPERIMENT_ENGINE_V2_20260529"
    registry: dict[str, PreviewReplayConfig] = {}
    cg_events = unified / "artifacts" / "cg" / "cg_preview_event_fixtures_v2.jsonl"
    cg_manifest = unified / "artifacts" / "cg" / "cg_preview_manifest_v2.json"
    cg_batch = batch / "clcn7_cg_batch_experiment_results_v2.json"
    if cg_events.exists() and cg_manifest.exists() and cg_batch.exists():
        cfg = PreviewReplayConfig(
            run_id="baseline_seed_20260529",
            system_id="clcn7",
            representation="coarse_grained",
            system_name="clcn7",
            event_source_path=str(cg_events),
            preview_manifest_path=str(cg_manifest),
            batch_results_path=str(cg_batch),
        )
        registry[cfg.run_id] = cfg
    aa_events = unified / "artifacts" / "aa" / "aa_preview_event_fixtures_v2.jsonl"
    aa_manifest = unified / "artifacts" / "aa" / "aa_preview_manifest_v2.json"
    if aa_events.exists() and aa_manifest.exists():
        cfg = PreviewReplayConfig(
            run_id="salad_1779654620",
            system_id="monomero",
            representation="all_atom",
            system_name="monomero",
            event_source_path=str(aa_events),
            preview_manifest_path=str(aa_manifest),
        )
        registry[cfg.run_id] = cfg
    return registry


def _preview_live_root() -> Path:
    return Path(__file__).resolve().parents[3] / ".mica" / "runtime" / "preview_ws_live"


def _compute_receipt_roots() -> list[Path]:
    subprogram_root = (
        Path(__file__).resolve().parents[3]
        / ".mica"
        / "programs"
        / "INSTITUTIONAL_SUPERNOVA"
        / "subprograms"
    )
    if not subprogram_root.exists():
        return []
    return sorted(path for path in subprogram_root.glob("*") if path.is_dir())


def _gcs_bucket_and_object(gcs_uri: str) -> tuple[str, str]:
    normalized = str(gcs_uri or "").strip()
    if not normalized.startswith("gs://"):
        return "", ""
    parts = normalized[5:].split("/", 1)
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _derive_structure_source_from_summary(summary: Any) -> str:
    metadata = dict(getattr(summary, "metadata", {}) or {})
    for key in ("input_structure_ref", "pdb_gcs_path", "pdb_path"):
        candidate = str(metadata.get(key) or "").strip()
        if candidate:
            return candidate
    output_gcs_prefix = str(metadata.get("output_gcs_prefix") or "").strip()
    if output_gcs_prefix.startswith("gs://"):
        bucket, object_prefix = _gcs_bucket_and_object(output_gcs_prefix)
        if bucket and object_prefix:
            return f"gs://{bucket}/{object_prefix.rstrip('/')}/input/protein.pdb"
    return ""


def _download_gcs_object_to_local(*, user_id: str, gcs_uri: str, destination: Path) -> str:
    from mica.storage.gcs_user_storage import get_storage_manager

    _bucket, object_path = _gcs_bucket_and_object(gcs_uri)
    if not object_path:
        raise ValueError(f"Unsupported GCS URI: {gcs_uri}")
    payload = get_storage_manager().read_bytes(
        user_id=user_id,
        object_path=object_path,
        max_bytes=20 * 1024 * 1024,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return str(destination)


def _resolve_receipt_backed_structure_source_for_run(run_id: str) -> dict[str, Any]:
    for root in _compute_receipt_roots():
        submit_receipt_path = root / "OPENMM_ALL_ATOM_SUBMIT_RECEIPT.json"
        structure_receipt_path = root / "STRUCTURE_ACQUISITION_RECEIPT.json"
        if not submit_receipt_path.exists() or not structure_receipt_path.exists():
            continue
        try:
            submit_receipt = json.loads(submit_receipt_path.read_text(encoding="utf-8"))
            structure_receipt = json.loads(structure_receipt_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        matching_attempt = next(
            (
                dict(item)
                for item in list(submit_receipt.get("attempts") or [])
                if str(item.get("job_id") or "").strip() == run_id
            ),
            None,
        )
        if not matching_attempt:
            continue

        structure_ref = str(
            matching_attempt.get("structure_input_gcs_uri")
            or submit_receipt.get("structure_input_gcs_uri")
            or ""
        ).strip()
        if not structure_ref:
            continue

        source_modes = dict(structure_receipt.get("source_modes") or {})
        uploaded_artifacts = []
        user_uploaded_mode = source_modes.get("user_uploaded_artifact_mode")
        if isinstance(user_uploaded_mode, dict):
            uploaded_artifacts.append(dict(user_uploaded_mode))
        afdb_mode = source_modes.get("afdb_uniprot_mode")
        if isinstance(afdb_mode, dict):
            uploaded_artifacts.append(dict(afdb_mode))
        matching_artifact = next(
            (
                dict(item)
                for item in uploaded_artifacts
                if str(item.get("gcs_uri") or "").strip() == structure_ref
            ),
            None,
        )
        if not matching_artifact:
            continue

        local_cache_path = Path(
            str(
                matching_artifact.get("local_cache_path")
                or source_modes.get("afdb_uniprot_mode", {}).get("local_cache_path")
                or ""
            )
        ).expanduser()
        if not local_cache_path.is_absolute():
            local_cache_path = (Path(__file__).resolve().parents[3] / local_cache_path).resolve()
        if not local_cache_path.exists():
            continue

        output_gcs_prefix = str(matching_attempt.get("output_gcs_prefix") or "").strip()
        route_decision_id = str(matching_attempt.get("route_decision_id") or "").strip()
        user_id = str(matching_artifact.get("user_id") or "").strip()
        return {
            "run_id": run_id,
            "structure_ref": structure_ref,
            "local_source_path": str(local_cache_path),
            "output_gcs_prefix": output_gcs_prefix,
            "route_decision_id": route_decision_id,
            "user_id": user_id,
            "preview_origin": "governed_receipt_local_structure",
            "receipt_root": str(root),
            "submit_receipt_ref": str(submit_receipt_path),
            "structure_receipt_ref": str(structure_receipt_path),
        }

    return {}


def _encode_structure_preview_to_bcif(
    *,
    structure_source: str,
    local_source_path: Path,
    output_bcif_path: Path,
) -> dict[str, Any]:
    suffix = local_source_path.suffix.lower()
    if suffix == ".bcif":
        output_bcif_path.parent.mkdir(parents=True, exist_ok=True)
        output_bcif_path.write_bytes(local_source_path.read_bytes())
        return {
            "status": "completed",
            "output_path": str(output_bcif_path),
            "source_path": str(local_source_path),
            "source_artifact_ref": structure_source,
            "failure_code": "",
            "failure_detail": "",
        }
    if suffix in {".cif", ".mmcif"}:
        result = encode_cif_to_bcif(
            input_cif=local_source_path,
            output_bcif=output_bcif_path,
        )
    else:
        result = encode_pdb_to_bcif(
            input_pdb=local_source_path,
            output_bcif=output_bcif_path,
        )
    result["source_artifact_ref"] = structure_source
    result["source_path"] = str(local_source_path)
    return result


async def _build_live_preview_config_for_compute_run(
    *,
    run_id: str,
    user_id: str,
) -> PreviewReplayConfig | None:
    if not user_id or not run_id.startswith(("salad_", "md_")):
        receipt_backed = _resolve_receipt_backed_structure_source_for_run(run_id)
        if not receipt_backed:
            return None
        user_id = user_id or str(receipt_backed.get("user_id") or "") or "receipt-backed-preview"
    else:
        receipt_backed = {}

    from mica.unified_compute_client import UnifiedComputeClient

    client = UnifiedComputeClient.from_env()
    try:
        summary = await client.get_job_status(run_id, user_id=user_id)
    except Exception:
        summary = None

    preview_origin = "live_compute_structure"
    output_gcs_prefix = ""
    structure_source = ""
    local_source_path_from_receipt: Path | None = None
    if summary is not None:
        structure_source = _derive_structure_source_from_summary(summary)
        output_gcs_prefix = str(getattr(summary, "metadata", {}).get("output_gcs_prefix", "") or "")
    if not structure_source:
        receipt_backed = receipt_backed or _resolve_receipt_backed_structure_source_for_run(run_id)
        if not receipt_backed:
            return None
        structure_source = str(receipt_backed.get("structure_ref") or "").strip()
        output_gcs_prefix = str(receipt_backed.get("output_gcs_prefix") or "").strip()
        local_source_path_from_receipt = Path(str(receipt_backed.get("local_source_path") or "")).expanduser().resolve()
        preview_origin = str(receipt_backed.get("preview_origin") or "governed_receipt_local_structure")
    if not structure_source:
        return None

    work_root = _preview_live_root() / run_id
    work_root.mkdir(parents=True, exist_ok=True)
    source_suffix = Path(structure_source).suffix or ".pdb"
    local_source_path = work_root / f"source_structure{source_suffix}"
    output_bcif_path = work_root / "preview_frame_0000.bcif"
    manifest_path = work_root / "preview_manifest.json"
    event_path = work_root / "preview_events.jsonl"

    try:
        if local_source_path_from_receipt is not None:
            local_source_path.write_bytes(local_source_path_from_receipt.read_bytes())
        elif structure_source.startswith("gs://"):
            _download_gcs_object_to_local(
                user_id=user_id,
                gcs_uri=structure_source,
                destination=local_source_path,
            )
        else:
            source_path = Path(structure_source).expanduser().resolve()
            if not source_path.exists():
                return None
            local_source_path.write_bytes(source_path.read_bytes())
    except Exception:
        if summary is None:
            return None
        receipt_backed = receipt_backed or _resolve_receipt_backed_structure_source_for_run(run_id)
        if not receipt_backed:
            return None
        structure_source = str(receipt_backed.get("structure_ref") or "").strip()
        output_gcs_prefix = str(receipt_backed.get("output_gcs_prefix") or "").strip()
        local_source_path_from_receipt = Path(str(receipt_backed.get("local_source_path") or "")).expanduser().resolve()
        if not local_source_path_from_receipt.exists():
            return None
        preview_origin = str(receipt_backed.get("preview_origin") or "governed_receipt_local_structure")
        local_source_path.write_bytes(local_source_path_from_receipt.read_bytes())

    encode_result = _encode_structure_preview_to_bcif(
        structure_source=structure_source,
        local_source_path=local_source_path,
        output_bcif_path=output_bcif_path,
    )
    if str(encode_result.get("status") or "") != "completed":
        return None

    preview = consume_bcif_preview_artifact(
        artifact_id=f"{run_id}:preview:0",
        bcif_path=str(output_bcif_path),
        source_job=run_id,
        source_run=run_id,
        source_run_id=run_id,
        representation="all_atom",
        system_id=run_id,
        frame_index=0,
        time_ps=0.0,
        canonical_or_preview="preview",
        preview_not_canonical=True,
    )
    manifest = build_local_preview_manifest(
        manifest_id=f"{run_id}_live_preview_manifest_v1",
        previews=[preview],
    )
    manifest["preview_origin"] = preview_origin
    manifest["source_artifact_ref"] = structure_source
    manifest["source_job_id"] = run_id
    manifest["output_gcs_prefix"] = output_gcs_prefix
    if receipt_backed:
        manifest["governed_receipt_ref"] = str(receipt_backed.get("submit_receipt_ref") or "")
        manifest["governed_structure_receipt_ref"] = str(receipt_backed.get("structure_receipt_ref") or "")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    preview_event = build_preview_available_event(
        preview=preview,
        source="mica://preview/ws/live-compute",
        subject=f"{run_id}/preview",
    )
    preview_event["data"]["source_artifact_ref"] = structure_source
    preview_event["data"]["preview_origin"] = preview_origin
    preview_event["data"]["job_state"] = str(getattr(getattr(summary, "state", None), "value", getattr(summary, "state", "")) or "") if summary is not None else "receipt_backed_structure_ready"
    artifact_synced_event = build_artifact_synced_event(
        preview=preview,
        source="mica://preview/ws/live-compute",
        subject=f"{run_id}/artifact",
    )
    artifact_synced_event["data"]["source_artifact_ref"] = structure_source
    artifact_synced_event["data"]["preview_origin"] = preview_origin
    write_event_fixtures_jsonl(path=str(event_path), events=[preview_event, artifact_synced_event])

    return PreviewReplayConfig(
        run_id=run_id,
        system_id=run_id,
        representation="all_atom",
        system_name=run_id,
        event_source_path=str(event_path),
        preview_manifest_path=str(manifest_path),
        replay_speed_multiplier=1.0,
        preserve_original_timestamps=False,
        representation_filter="all_atom",
        ticket_required_in_production=True,
        expected_ticket_scope="preview:read",
    )


def _normalize_registry(raw_registry: Any) -> dict[str, PreviewReplayConfig]:
    registry: dict[str, PreviewReplayConfig] = {}
    if not isinstance(raw_registry, dict):
        return registry
    for run_id, value in raw_registry.items():
        if isinstance(value, PreviewReplayConfig):
            registry[str(run_id)] = value
        elif isinstance(value, dict):
            registry[str(run_id)] = PreviewReplayConfig.model_validate(value)
    return registry


def resolve_preview_ws_registry(*, websocket: WebSocket | None = None, registry: dict[str, PreviewReplayConfig] | None = None) -> dict[str, PreviewReplayConfig]:
    if registry:
        return dict(registry)
    if websocket is not None:
        state_registry = getattr(getattr(websocket.app, "state", None), "preview_ws_registry", None)
        normalized = _normalize_registry(state_registry)
        if normalized:
            return normalized
    if not is_production():
        return build_default_preview_fixture_registry()
    return {}


def build_preview_replayer_contract_v1() -> dict[str, Any]:
    return {
        "contract_id": "preview_replayer_contract_v1",
        "input_families": [
            "event_jsonl_fixtures",
            "bcif_preview_artifacts",
            "preview_manifest",
            "batch_results_optional",
        ],
        "output_stream_events": [
            "trajectory.preview.available",
            "trajectory.frame.preview",
            "artifact.synced",
            "cg.batch.experiment.completed",
            "cg.batch.experiment.blocked",
            "error.typed",
            "smic.metric",
        ],
        "required_behavior": {
            "preserve_event_order": True,
            "preserve_or_rewrite_timestamps": True,
            "replay_speed_multiplier_supported": True,
            "pause_resume_supported": True,
            "representation_filter_supported": True,
        },
        "ticket_required_in_production": True,
        "expected_ticket_scope": "preview:read",
        "local_auth_enforced": False,
    }


def build_preview_realtime_product_contract_v1() -> dict[str, Any]:
    return {
        "contract_id": "preview_realtime_product_contract_v1",
        "transport_mode": "local_websocket_live_or_fixture",
        "ws_path_template": "/ws/preview/{run_id}",
        "handshake": {
            "first_client_message": {"action": "subscribe"},
            "server_ack_type": "subscribed",
            "replay_completed_type": "replay.completed",
            "ticket_required_in_production": True,
            "expected_ticket_scope": "preview:read",
            "local_auth_enforced": False,
        },
        "frontend_state_shape": {
            "job_id": "string",
            "run_id": "string",
            "system_name": "string",
            "preview_status": "string",
            "current_frame": "int|null",
            "frame_count": "int|null",
            "time_ps": "float|null",
            "bcif_ref": "string",
            "canonical_artifact_refs": "object",
            "event_log": "list",
            "errors": "list",
            "actions_available": "list",
            "preview_not_canonical": "bool",
            "realtime_ws_claim": "bool",
        },
        "claim_boundary": {
            "provider_required": False,
            "frontend_rendering_proven": False,
            "production_claim": False,
            "biological_correctness_claim": False,
            "smic_metrics_status": "not_executed",
        },
    }


async def _resolve_preview_runtime_config(
    *,
    websocket: WebSocket,
    run_id: str,
    resolved_registry: dict[str, PreviewReplayConfig],
) -> tuple[PreviewReplayConfig | None, str]:
    if run_id in resolved_registry:
        config = resolved_registry[run_id]
        user_id = ""
        try:
            user_id = _verify_preview_ticket_if_required(websocket, run_id=run_id, config=config) or ""
        except HTTPException:
            raise
        return config, user_id

    ticket = str(websocket.query_params.get("ticket") or "").strip()
    workspace_id = str(websocket.query_params.get("workspace_id") or "").strip()
    user_id = ""
    if ticket:
        user_id = verify_ws_ticket(ticket, scope="preview", run_id=run_id, workspace_id=workspace_id or "")
    elif is_production():
        raise HTTPException(status_code=401, detail="Preview WebSocket ticket required in production")

    config = await _build_live_preview_config_for_compute_run(run_id=run_id, user_id=user_id)
    if config is not None:
        state_registry = getattr(getattr(websocket.app, "state", None), "preview_ws_registry", None)
        if isinstance(state_registry, dict):
            state_registry[run_id] = config
        return config, user_id

    return None, user_id


def build_preview_frontend_handshake_state(
    *,
    ui_state: PreviewUIState,
    run_id: str,
    system_id: str,
    representation: Literal["all_atom", "coarse_grained"],
    connection_status: str,
    subscribed: bool,
    replay_completed: bool,
    last_event_type: str,
) -> PreviewFrontendHandshakeState:
    return PreviewFrontendHandshakeState(
        connection_status=connection_status,
        subscribed=subscribed,
        job_id=ui_state.job_id,
        run_id=run_id,
        system_name=ui_state.system_name,
        system_id=system_id,
        representation=representation,
        preview_status=ui_state.preview_status,
        current_frame=ui_state.current_frame,
        frame_count=ui_state.frame_count,
        time_ps=ui_state.time_ps,
        bcif_ref=ui_state.bcif_ref,
        canonical_artifact_refs=dict(ui_state.canonical_artifact_refs),
        event_log=[entry.model_dump(mode="json") for entry in ui_state.event_log],
        errors=[entry.model_dump(mode="json") for entry in ui_state.errors],
        actions_available=list(ui_state.actions_available),
        replay_completed=replay_completed,
        last_event_type=last_event_type,
        preview_not_canonical=ui_state.preview_not_canonical,
        realtime_ws_claim=False,
    )


async def _send_typed_error_and_close(
    websocket: WebSocket,
    *,
    failure_code: str,
    failure_detail: str,
    close_code: int = 1008,
) -> None:
    await websocket.send_json(
        {
            "type": "error.typed",
            "source": "mica://preview/ws",
            "subject": "preview/error",
            "time": _utcnow(),
            "datacontenttype": "application/json",
            "data": {
                "failure_code": failure_code,
                "failure_detail": failure_detail,
            },
        }
    )
    await websocket.close(code=close_code)


def _verify_preview_ticket_if_required(websocket: WebSocket, *, run_id: str, config: PreviewReplayConfig) -> str | None:
    ticket = str(websocket.query_params.get("ticket") or "").strip()
    workspace_id = str(websocket.query_params.get("workspace_id") or "").strip()
    if ticket:
        return verify_ws_ticket(ticket, scope="preview", run_id=run_id, workspace_id=workspace_id or "")
    if is_production() and config.ticket_required_in_production:
        raise HTTPException(status_code=401, detail="Preview WebSocket ticket required in production")
    return None


async def handle_preview_websocket(
    websocket: WebSocket,
    run_id: str,
    *,
    registry: dict[str, PreviewReplayConfig] | None = None,
) -> None:
    resolved_registry = resolve_preview_ws_registry(websocket=websocket, registry=registry)
    await websocket.accept()
    try:
        message = await websocket.receive_json()
    except Exception:
        await _send_typed_error_and_close(
            websocket,
            failure_code="subscribe_payload_invalid",
            failure_detail="Client did not send a valid subscribe payload.",
        )
        return

    if str(message.get("action") or "") != "subscribe":
        await _send_typed_error_and_close(
            websocket,
            failure_code="subscribe_action_required",
            failure_detail="First message must be action=subscribe.",
        )
        return

    try:
        config, _user_id = await _resolve_preview_runtime_config(
            websocket=websocket,
            run_id=run_id,
            resolved_registry=resolved_registry,
        )
    except HTTPException as exc:
        await _send_typed_error_and_close(
            websocket,
            failure_code="preview_ticket_required" if exc.status_code == 401 else "preview_ticket_invalid",
            failure_detail=str(exc.detail),
        )
        return
    if config is None:
        await _send_typed_error_and_close(
            websocket,
            failure_code="run_id_not_found",
            failure_detail=f"Unknown run_id: {run_id}",
            close_code=1003,
        )
        return

    session = PreviewReplaySession(config)
    state = initialize_preview_ui_state(
        system_name=config.system_name,
        preview_manifest_path=config.preview_manifest_path,
        batch_results_path=config.batch_results_path,
    )
    await websocket.send_json(
        {
            "type": "subscribed",
            "run_id": run_id,
            "system_id": config.system_id,
            "representation": config.representation,
            "ticket_required_in_production": config.ticket_required_in_production,
            "expected_ticket_scope": config.expected_ticket_scope,
            "local_auth_enforced": False,
        }
    )

    replay_events = await session.replay_events()
    last_event_type = ""
    for sequence, event in enumerate(replay_events):
        await websocket.send_json(event)
        last_event_type = str(event.get("type") or "")
        apply_preview_event_to_ui_state(state=state, raw_event=event, sequence=sequence)

    final_state = build_preview_frontend_handshake_state(
        ui_state=state,
        run_id=run_id,
        system_id=config.system_id,
        representation=config.representation,
        connection_status="completed",
        subscribed=True,
        replay_completed=True,
        last_event_type=last_event_type or "replay.completed",
    )
    await websocket.send_json(
        {
            "type": "replay.completed",
            "run_id": run_id,
            "final_state": final_state.model_dump(mode="json"),
        }
    )
    await websocket.close()


def build_preview_ws_app(*, registry: dict[str, PreviewReplayConfig]) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws/preview/{run_id}")
    async def preview_ws(websocket: WebSocket, run_id: str) -> None:
        await handle_preview_websocket(websocket, run_id, registry=registry)

    return app
