from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .bcif_runtime import validate_bcif
from .local_preview_consumer import validate_preview_event


def _non_empty(value: Any) -> str:
    return str(value or "").strip()


class PreviewUIError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    event_type: str = ""
    artifact_path: str = ""


class PreviewUIEventLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=0)
    event_type: str
    subject: str = ""
    time: str = ""
    job_id: str = ""
    run_id: str = ""
    frame_index: int | None = Field(default=None, ge=0)
    time_ps: float | None = Field(default=None, ge=0.0)
    preview_status_after_event: str
    error_code: str = ""


class PreviewUIState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = ""
    run_id: str = ""
    system_name: str
    preview_status: str
    current_frame: int | None = Field(default=None, ge=0)
    frame_count: int | None = Field(default=None, ge=0)
    time_ps: float | None = Field(default=None, ge=0.0)
    bcif_ref: str = ""
    canonical_artifact_refs: dict[str, str] = Field(default_factory=dict)
    event_log: list[PreviewUIEventLogEntry] = Field(default_factory=list)
    errors: list[PreviewUIError] = Field(default_factory=list)
    actions_available: list[str] = Field(default_factory=list)
    preview_not_canonical: bool = True
    realtime_ws_claim: bool = False
    smic_metrics_status: str = "not_executed"


@dataclass(frozen=True)
class LocalPreviewWatchResult:
    ui_state: PreviewUIState
    timeline: list[dict[str, Any]]


def _load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).expanduser().resolve().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_event_stream(source_path: str | Path) -> list[dict[str, Any]]:
    source = Path(source_path).expanduser().resolve()
    jsonl_files: list[Path]
    if source.is_dir():
        jsonl_files = sorted(path for path in source.glob("*.jsonl") if path.is_file())
    else:
        jsonl_files = [source]
    events: list[dict[str, Any]] = []
    for path in jsonl_files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
    return events


def _append_error(
    state: PreviewUIState,
    *,
    code: str,
    message: str,
    event_type: str = "",
    artifact_path: str = "",
) -> None:
    state.errors.append(
        PreviewUIError(
            code=code,
            message=message,
            event_type=event_type,
            artifact_path=artifact_path,
        )
    )


def _canonical_refs_from_batch_results(batch_results_path: str | Path | None) -> dict[str, str]:
    if not batch_results_path:
        return {}
    payload = _load_json(batch_results_path)
    for experiment in list(payload.get("experiments") or []):
        if str(experiment.get("status") or "") != "completed":
            continue
        preview_receipt = dict(experiment.get("preview_receipt") or {})
        refs = {
            "final_structure_preview_ref": _non_empty(preview_receipt.get("final_structure_preview_ref")),
            "short_trajectory_preview_ref": _non_empty(preview_receipt.get("short_trajectory_preview_ref")),
            "canonical_trajectory_ref": _non_empty(preview_receipt.get("canonical_trajectory_ref")),
            "bcif_preview_ref": _non_empty(preview_receipt.get("bcif_preview_ref")),
        }
        return {key: value for key, value in refs.items() if value}
    return {}


def _adopt_preview_identity(state: PreviewUIState, data: dict[str, Any], *, overwrite_run: bool) -> None:
    source_job = _non_empty(data.get("source_job"))
    source_run = _non_empty(data.get("source_run"))
    source_run_id = _non_empty(data.get("source_run_id"))
    if source_job and not state.job_id:
        state.job_id = source_job
    preferred_run = source_run_id or source_run
    if overwrite_run and preferred_run:
        state.run_id = preferred_run
    elif preferred_run and not state.run_id:
        state.run_id = preferred_run


def _refresh_actions(state: PreviewUIState) -> None:
    actions: list[str] = []
    if state.bcif_ref:
        actions.append("open_preview")
    if state.canonical_artifact_refs.get("canonical_trajectory_ref"):
        actions.append("open_canonical_trajectory")
    if state.canonical_artifact_refs.get("final_structure_preview_ref"):
        actions.append("open_final_structure")
    if state.event_log:
        actions.append("inspect_event_log")
    if state.errors:
        actions.append("review_errors")
    state.actions_available = actions


def _refresh_status(state: PreviewUIState) -> None:
    if state.bcif_ref and state.errors:
        state.preview_status = "ready_with_errors"
    elif state.bcif_ref and state.current_frame is not None:
        state.preview_status = "frame_preview_available"
    elif state.bcif_ref:
        state.preview_status = "preview_available"
    elif state.errors:
        state.preview_status = "error"
    else:
        state.preview_status = "idle"
    _refresh_actions(state)


def _validate_bcif_from_event(state: PreviewUIState, event_type: str, data: dict[str, Any]) -> None:
    artifact_path = _non_empty(data.get("path"))
    expected_sha256 = _non_empty(data.get("sha256"))
    expected_size = data.get("size_bytes")
    if not artifact_path:
        _append_error(
            state,
            code="bcif_missing_path",
            message="Preview event does not declare a BCIF path.",
            event_type=event_type,
        )
        return
    validation = validate_bcif(artifact_path)
    if validation.validation_status != "passed":
        _append_error(
            state,
            code=validation.blocker or "bcif_validation_failed",
            message=validation.detail or "BCIF validation failed.",
            event_type=event_type,
            artifact_path=artifact_path,
        )
        return
    if expected_sha256 and validation.sha256 != expected_sha256:
        _append_error(
            state,
            code="bcif_sha256_mismatch",
            message=f"Expected sha256 {expected_sha256} but found {validation.sha256}.",
            event_type=event_type,
            artifact_path=artifact_path,
        )
        return
    if expected_size is not None and int(validation.size_bytes) != int(expected_size):
        _append_error(
            state,
            code="bcif_size_mismatch",
            message=f"Expected size {expected_size} but found {validation.size_bytes}.",
            event_type=event_type,
            artifact_path=artifact_path,
        )
        return
    state.bcif_ref = validation.path
    state.preview_not_canonical = True


def _timeline_entry(
    *,
    sequence: int,
    event_type: str,
    subject: str,
    time: str,
    state: PreviewUIState,
    frame_index: int | None = None,
    time_ps: float | None = None,
    error_code: str = "",
) -> dict[str, Any]:
    return PreviewUIEventLogEntry(
        sequence=sequence,
        event_type=event_type,
        subject=subject,
        time=time,
        job_id=state.job_id,
        run_id=state.run_id,
        frame_index=frame_index,
        time_ps=time_ps,
        preview_status_after_event=state.preview_status,
        error_code=error_code,
    ).model_dump(mode="json")


def initialize_preview_ui_state(
    *,
    system_name: str,
    preview_manifest_path: str | Path | None = None,
    batch_results_path: str | Path | None = None,
) -> PreviewUIState:
    state = PreviewUIState(
        job_id="",
        run_id="",
        system_name=system_name,
        preview_status="idle",
        current_frame=None,
        frame_count=None,
        time_ps=None,
        bcif_ref="",
        canonical_artifact_refs=_canonical_refs_from_batch_results(batch_results_path),
    )
    if preview_manifest_path:
        manifest = _load_json(preview_manifest_path)
        previews = list(manifest.get("previews") or [])
        if previews and not state.job_id:
            state.job_id = _non_empty(previews[0].get("source_job"))
        if previews and not state.run_id:
            state.run_id = _non_empty(previews[0].get("source_run"))
    return state


def apply_preview_event_to_ui_state(
    *,
    state: PreviewUIState,
    raw_event: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    parsed = validate_preview_event(raw_event)
    data = dict(parsed.data or {})
    event_type = parsed.type
    error_code = ""

    if event_type == "trajectory.preview.available":
        _adopt_preview_identity(state, data, overwrite_run=False)
        _validate_bcif_from_event(state, event_type, data)
    elif event_type == "trajectory.frame.preview":
        _adopt_preview_identity(state, data, overwrite_run=True)
        _validate_bcif_from_event(state, event_type, data)
        if not state.errors or state.errors[-1].event_type != event_type:
            state.current_frame = int(data.get("frame_index") or 0)
            time_ps = data.get("time_ps")
            state.time_ps = float(time_ps) if time_ps is not None else state.time_ps
    elif event_type == "artifact.synced":
        artifact_id = _non_empty(data.get("artifact_id")) or f"artifact_{sequence}"
        artifact_path = _non_empty(data.get("path"))
        if artifact_path:
            state.canonical_artifact_refs[artifact_id] = artifact_path
    elif event_type == "cg.batch.experiment.completed":
        state.job_id = _non_empty(data.get("batch_id")) or state.job_id
        experiment_id = _non_empty(data.get("experiment_id"))
        if experiment_id:
            state.run_id = experiment_id
    elif event_type == "cg.batch.experiment.blocked":
        state.job_id = _non_empty(data.get("batch_id")) or state.job_id
        experiment_id = _non_empty(data.get("experiment_id"))
        if experiment_id and not state.run_id:
            state.run_id = experiment_id
        error_code = _non_empty(data.get("failure_code")) or "batch_experiment_blocked"
        blockers = list(data.get("blockers") or [])
        _append_error(
            state,
            code=error_code,
            message=", ".join(str(item) for item in blockers if str(item)) or "Batch experiment blocked.",
            event_type=event_type,
        )
    elif event_type == "error.typed":
        error_code = _non_empty(data.get("failure_code")) or "typed_error"
        _append_error(
            state,
            code=error_code,
            message=_non_empty(data.get("failure_detail")) or "Typed error event received.",
            event_type=event_type,
        )

    _refresh_status(state)
    timeline_entry = _timeline_entry(
        sequence=sequence,
        event_type=event_type,
        subject=parsed.subject,
        time=parsed.time,
        state=state,
        frame_index=(
            int(data.get("frame_index"))
            if data.get("frame_index") is not None
            else state.current_frame
        ),
        time_ps=(
            float(data.get("time_ps"))
            if data.get("time_ps") is not None
            else state.time_ps
        ),
        error_code=error_code,
    )
    state.event_log.append(PreviewUIEventLogEntry.model_validate(timeline_entry))
    return timeline_entry


def build_local_preview_ui_state(
    *,
    event_source_path: str | Path,
    system_name: str,
    preview_manifest_path: str | Path | None = None,
    batch_results_path: str | Path | None = None,
) -> LocalPreviewWatchResult:
    events = _read_event_stream(event_source_path)
    state = initialize_preview_ui_state(
        system_name=system_name,
        preview_manifest_path=preview_manifest_path,
        batch_results_path=batch_results_path,
    )

    timeline: list[dict[str, Any]] = []
    for index, raw_event in enumerate(events):
        timeline_entry = apply_preview_event_to_ui_state(
            state=state,
            raw_event=raw_event,
            sequence=index,
        )
        timeline.append(timeline_entry)

    _refresh_status(state)
    return LocalPreviewWatchResult(ui_state=state, timeline=timeline)


def write_ui_timeline_jsonl(*, timeline: list[dict[str, Any]], output_path: str | Path) -> str:
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for event in timeline:
            handle.write(json.dumps(event) + "\n")
    return str(destination)
