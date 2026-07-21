from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import storage
from google.cloud.exceptions import NotFound


def _bootstrap_runtime_import_roots() -> None:
    script_dir = Path(__file__).resolve().parent
    prepend_paths: list[str] = []

    for candidate in (script_dir, *script_dir.parents):
        if (candidate / "workers" / "dynamo").is_dir() and str(candidate) not in prepend_paths:
            prepend_paths.append(str(candidate))
        if (candidate / "src" / "mica").is_dir() and str(candidate / "src") not in prepend_paths:
            prepend_paths.append(str(candidate / "src"))
        if len(prepend_paths) == 2:
            break

    # Keep the repo/image root ahead of src so `workers.*` resolves from the
    # packaged worker tree instead of any unrelated `src/workers` package.
    for candidate_text in reversed(prepend_paths):
        while candidate_text in sys.path:
            sys.path.remove(candidate_text)
        sys.path.insert(0, candidate_text)


_bootstrap_runtime_import_roots()


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _decode_gcs_credentials() -> None:
    creds_b64 = os.getenv("GCS_CREDENTIALS_JSON_B64", "").strip() or os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", ""
    ).strip()
    if not creds_b64:
        return
    payload = base64.b64decode(creds_b64)
    fd, path = tempfile.mkstemp(prefix="mica_gcs_", suffix=".json")
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path


def _upload_file(client: storage.Client, bucket_name: str, local_path: Path, object_name: str) -> None:
    blob = client.bucket(bucket_name).blob(object_name)
    blob.upload_from_filename(str(local_path))


def _upload_json(client: storage.Client, bucket_name: str, payload: Any, object_name: str) -> None:
    client.bucket(bucket_name).blob(object_name).upload_from_string(
        json.dumps(payload, indent=2, default=str),
        content_type="application/json",
    )


def _upload_text(
    client: storage.Client,
    bucket_name: str,
    payload: str,
    object_name: str,
    *,
    content_type: str = "text/plain",
) -> None:
    client.bucket(bucket_name).blob(object_name).upload_from_string(
        payload,
        content_type=content_type,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit_crash_diagnostic(phase: str, error_msg: str, traceback_text: str) -> None:
    payload = {
        "event": "mica_worker_crash_diagnostic",
        "phase": phase,
        "error": str(error_msg)[:500],
        "traceback": str(traceback_text)[:2000],
        "produced_at": _utc_now_iso(),
        "env_checks": {
            "GCS_BUCKET": bool(os.getenv("GCS_BUCKET", "").strip()),
            "PDB_GCS_OBJECT": bool(os.getenv("PDB_GCS_OBJECT", "").strip()),
            "MICA_WORKER_MODE": os.getenv("MICA_WORKER_MODE", "").strip() or "(default)",
            "GCS_CREDENTIALS_JSON_B64": "present" if os.getenv("GCS_CREDENTIALS_JSON_B64", "").strip() else "missing",
        },
    }
    print(json.dumps(payload, indent=2, default=str), flush=True)


def _download_if_exists(client: storage.Client, bucket_name: str, object_name: str, local_path: Path) -> bool:
    blob = client.bucket(bucket_name).blob(object_name)
    try:
        blob.download_to_filename(str(local_path))
        return True
    except NotFound:
        return False


def _download_tree_if_exists(
    client: storage.Client,
    bucket_name: str,
    object_prefix: str,
    local_dir: Path,
) -> list[str]:
    prefix = object_prefix.strip().rstrip("/")
    if not prefix:
        return []

    downloaded: list[str] = []
    for blob in client.list_blobs(bucket_name, prefix=f"{prefix}/"):
        object_name = str(getattr(blob, "name", "") or "")
        if not object_name or object_name.endswith("/"):
            continue
        relative_path = object_name[len(prefix) + 1 :]
        if not relative_path:
            continue
        local_path = local_dir / Path(relative_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
        downloaded.append(object_name)
    return downloaded


def _safe_object_name(uri_or_object: str, default_bucket: str) -> str:
    text = (uri_or_object or "").strip()
    if text.startswith("gs://"):
        stripped = text[5:]
        if "/" not in stripped:
            return ""
        bucket, obj = stripped.split("/", 1)
        if bucket != default_bucket:
            raise RuntimeError(f"Object bucket mismatch: expected {default_bucket}, got {bucket}")
        return obj
    return text


def _emit_bootstrap_artifacts(
    *,
    client: storage.Client,
    bucket_name: str,
    root_prefix: str,
    worker_mode: str,
) -> dict[str, Any]:
    bootstrap_prefix = root_prefix.strip().rstrip("/")
    if bootstrap_prefix:
        bootstrap_prefix = f"{bootstrap_prefix}/bootstrap"
    else:
        bootstrap_prefix = "bootstrap"
    common = {
        "job_id": os.getenv("MICA_JOB_ID", "").strip(),
        "route_decision_id": os.getenv("MICA_ROUTE_DECISION_ID", "").strip(),
        "provider": os.getenv("MICA_PROVIDER", "salad").strip() or "salad",
        "requested_gpu_type": os.getenv("MICA_REQUESTED_GPU_TYPE", "").strip(),
        "container_group_id": os.getenv("MICA_CONTAINER_GROUP_ID", "").strip()
        or os.getenv("CONTAINER_GROUP_NAME", "").strip(),
        "output_gcs_prefix": os.getenv("MICA_OUTPUT_GCS_PREFIX", "").strip(),
        "image_ref": os.getenv("MICA_IMAGE_REF", "").strip(),
        "image_digest": os.getenv("MICA_IMAGE_DIGEST", "").strip(),
        "allocation_attempt": os.getenv("MICA_ALLOCATION_ATTEMPT", "").strip(),
        "worker_mode": worker_mode,
        "no_md": False,
    }
    payloads = [
        (
            "worker_entrypoint_started.json",
            {
                **common,
                "schema_version": "worker_entrypoint_started_v1",
                "event": "worker_entrypoint_started",
                "produced_at": _utc_now_iso(),
            },
        ),
        (
            "bootstrap_heartbeat.json",
            {
                **common,
                "schema_version": "bootstrap_heartbeat_v1",
                "event": "bootstrap_heartbeat",
                "produced_at": _utc_now_iso(),
            },
        ),
        (
            "gcs_write_probe.json",
            {
                **common,
                "schema_version": "gcs_write_probe_v1",
                "event": "gcs_write_probe",
                "gcs_write_ok": True,
                "produced_at": _utc_now_iso(),
            },
        ),
    ]
    written: dict[str, str] = {}
    for filename, payload in payloads:
        object_path = f"{bootstrap_prefix}/{filename}"
        payload["object_path"] = object_path
        _upload_json(client, bucket_name, payload, object_path)
        written[filename] = f"gs://{bucket_name}/{object_path}"
    return {
        "schema_version": "worker_bootstrap_manifest_v1",
        "bucket": bucket_name,
        "bootstrap_prefix": bootstrap_prefix,
        "worker_mode": worker_mode,
        "written": written,
        "produced_at": _utc_now_iso(),
    }


def _download_required(client: storage.Client, bucket_name: str, object_name: str, local_path: Path) -> None:
    client.bucket(bucket_name).blob(object_name).download_to_filename(str(local_path))


def _upload_tree(
    client: storage.Client,
    bucket_name: str,
    root_dir: Path,
    object_prefix: str,
) -> list[str]:
    uploaded: list[str] = []
    prefix = object_prefix.strip().rstrip("/")
    for local_path in sorted(root_dir.rglob("*")):
        if not local_path.is_file():
            continue
        rel_path = local_path.relative_to(root_dir).as_posix()
        object_name = f"{prefix}/{rel_path}" if prefix else rel_path
        _upload_file(client, bucket_name, local_path, object_name)
        uploaded.append(object_name)
    return uploaded


def _count_pdb_models(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        count = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("MODEL"):
                    count += 1
        return count or (1 if path.stat().st_size > 0 else 0)
    except Exception:
        return 0


def _append_multi_model_preview(
    *,
    output_dir: Path,
    preview_path: Path,
    max_models: int = 20,
) -> Path | None:
    try:
        if not preview_path.exists() or preview_path.stat().st_size <= 0:
            return None
        multi_model_path = output_dir / "multi_model_preview.pdb"
        current_models = _count_pdb_models(multi_model_path)
        if current_models >= max_models:
            return multi_model_path
        model_index = current_models + 1
        preview_lines = preview_path.read_text(encoding="utf-8", errors="replace").splitlines()
        atom_lines = [line for line in preview_lines if line.startswith(("ATOM", "HETATM", "TER"))]
        if not atom_lines:
            return None
        with multi_model_path.open("a", encoding="utf-8") as handle:
            handle.write(f"MODEL     {model_index}\n")
            for line in atom_lines:
                handle.write(f"{line}\n")
            handle.write("ENDMDL\n")
        return multi_model_path
    except Exception:
        return None


def _sync_complex_output_once(
    client: storage.Client,
    bucket_name: str,
    output_dir: Path,
    output_prefix: str,
    checkpoint_object: str,
    *,
    fallback_checkpoint: Path | None = None,
) -> dict[str, Any]:
    uploaded_outputs = _upload_tree(client, bucket_name, output_dir, output_prefix)
    checkpoint_candidate = _find_checkpoint_candidate(output_dir)
    if checkpoint_candidate is None and fallback_checkpoint is not None and fallback_checkpoint.is_file():
        checkpoint_candidate = fallback_checkpoint
    if checkpoint_candidate is not None:
        _upload_file(client, bucket_name, checkpoint_candidate, checkpoint_object)
    return {
        "uploaded_outputs": uploaded_outputs,
        "checkpoint_candidate": str(checkpoint_candidate) if checkpoint_candidate else "",
    }


def _periodic_complex_sync_loop(
    client: storage.Client,
    bucket_name: str,
    output_dir: Path,
    output_prefix: str,
    checkpoint_object: str,
    fallback_checkpoint: Path | None,
    interval_seconds: int,
    stop_event: threading.Event,
) -> None:
    wait_seconds = max(1, int(interval_seconds))
    while not stop_event.wait(wait_seconds):
        try:
            _sync_complex_output_once(
                client,
                bucket_name,
                output_dir,
                output_prefix,
                checkpoint_object,
                fallback_checkpoint=fallback_checkpoint,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "phase": "complex_sync_warning",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                ),
                flush=True,
            )


def _start_periodic_complex_sync(
    client: storage.Client,
    bucket_name: str,
    output_dir: Path,
    output_prefix: str,
    checkpoint_object: str,
    *,
    fallback_checkpoint: Path | None = None,
    interval_seconds: int = 600,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    sync_thread = threading.Thread(
        target=_periodic_complex_sync_loop,
        args=(
            client,
            bucket_name,
            output_dir,
            output_prefix,
            checkpoint_object,
            fallback_checkpoint,
            interval_seconds,
            stop_event,
        ),
        daemon=True,
    )
    sync_thread.start()
    return stop_event, sync_thread


def _find_checkpoint_candidate(output_dir: Path) -> Path | None:
    candidates = sorted(
        (
            path
            for path in output_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".chk", ".cpt", ".xml"}
            and ("chk" in path.name.lower() or "checkpoint" in path.name.lower())
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _select_paper_resume_structure(output_dir: Path) -> Path | None:
    for name in ("replica_1_equilibrated.pdb", "replica_1_prepared.pdb"):
        candidate = output_dir / name
        if candidate.is_file():
            return candidate
    return None


def _detect_worker_mode() -> str:
    explicit_mode = os.getenv("MICA_WORKER_MODE", "").strip().lower()
    if explicit_mode:
        return explicit_mode
    simulation_mode = os.getenv("SIMULATION_MODE", "").strip().lower()
    if simulation_mode:
        return simulation_mode
    if os.getenv("LIGAND_SMILES", "").strip() or os.getenv("DOCKED_LIGAND_GCS_OBJECT", "").strip():
        return "complex_stability"
    return "protein_only"


def _repo_root_for_worker() -> Path:
    script_dir = Path(__file__).resolve().parent
    for candidate in (script_dir, *script_dir.parents):
        if (candidate / "workers" / "dynamo" / "biodynamo" / "processors").is_dir():
            return candidate
    return script_dir


def _run_paper_dodecaedrica_job(
    *,
    client: storage.Client,
    bucket: str,
    pdb_bucket: str,
    pdb_object: str,
    checkpoint_object: str,
    output_prefix: str,
    workspace: Path,
) -> dict[str, Any]:
    local_protein = workspace / "protein.pdb"
    local_checkpoint = workspace / "checkpoint.cpt"
    output_dir = workspace / "paper_dodecaedrica_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    _download_required(client, pdb_bucket, pdb_object, local_protein)
    had_checkpoint = _download_if_exists(client, bucket, checkpoint_object, local_checkpoint)
    restored_output_objects = (
        _download_tree_if_exists(client, bucket, output_prefix, output_dir) if had_checkpoint else []
    )

    processor_name = (
        os.getenv("MICA_MD_PROCESSOR", "").strip()
        or "runcomplex_paper_dodecaedrica.py"
    )
    if Path(processor_name).name != "runcomplex_paper_dodecaedrica.py":
        raise RuntimeError(
            "paper_dodecaedrica mode only permits runcomplex_paper_dodecaedrica.py"
        )
    processor_path = (
        _repo_root_for_worker()
        / "workers"
        / "dynamo"
        / "biodynamo"
        / "processors"
        / "runcomplex_paper_dodecaedrica.py"
    )
    if not processor_path.exists():
        raise RuntimeError(f"Required processor not found: {processor_path}")

    sync_interval_seconds = int(os.getenv("SAVING_INTERVAL_SECONDS", "600"))
    stop_event = None
    sync_thread = None
    if sync_interval_seconds > 0:
        stop_event, sync_thread = _start_periodic_complex_sync(
            client,
            bucket,
            output_dir,
            output_prefix,
            checkpoint_object,
            fallback_checkpoint=local_checkpoint,
            interval_seconds=sync_interval_seconds,
        )

    started_at = datetime.now(timezone.utc).isoformat()
    stdout_path = output_dir / "paper_dodecaedrica_stdout.log"
    resume_structure = _select_paper_resume_structure(output_dir) if had_checkpoint else None
    processor_input = resume_structure if resume_structure is not None else local_protein

    cmd = [
        sys.executable,
        str(processor_path),
        "--pdb",
        str(processor_input),
        "--output_dir",
        str(output_dir),
        "--ns",
        str(float(os.getenv("PRODUCTION_NS", "230.0"))),
        "--platform",
        os.getenv("OPENMM_PLATFORM", "CUDA").strip() or "CUDA",
        "--gpu_id",
        os.getenv("OPENMM_GPU_ID", "0").strip() or "0",
    ]
    if had_checkpoint:
        cmd.extend(["--resume", str(local_checkpoint)])
    should_prepare = (
        not had_checkpoint
        and os.getenv("PREPARE_SYSTEM", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    if should_prepare:
        cmd.append("--prepare")

    try:
        with stdout_path.open("w", encoding="utf-8") as handle:
            subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, check=True)
    finally:
        if stop_event is not None:
            stop_event.set()
        if sync_thread is not None:
            sync_thread.join(timeout=5.0)

    sync_result = _sync_complex_output_once(
        client,
        bucket,
        output_dir,
        output_prefix,
        checkpoint_object,
        fallback_checkpoint=local_checkpoint,
    )
    return {
        "mode": "paper_dodecaedrica",
        "processor": str(processor_path),
        "command": cmd,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "production_ns": float(os.getenv("PRODUCTION_NS", "230.0")),
        "frame_interval_ps": 50,
        "resume_requested": had_checkpoint,
        "resume_checkpoint": str(local_checkpoint) if had_checkpoint else "",
        "resume_structure": str(resume_structure) if resume_structure is not None else "",
        "restored_output_objects": restored_output_objects,
        "output_dir": str(output_dir),
        "uploaded_outputs": sync_result["uploaded_outputs"],
        "checkpoint_uploaded": sync_result["checkpoint_candidate"],
    }


def _normalize_problematic_act_residues(local_pdb: Path) -> int:
    """
    Normalize ACT residues that are effectively N-terminal acetyl caps.

    Some PDBs include residue name ACT where the atom set matches an acetyl cap
    (no backbone N). Amber templates expect ACE for this case; leaving ACT causes
    forcefield.createSystem() template resolution errors.

    Returns number of residues renamed from ACT -> ACE.
    """
    lines = local_pdb.read_text(encoding="utf-8").splitlines()

    residues: dict[tuple[str, str, str], set[str]] = {}
    for line in lines:
        if not (line.startswith("ATOM  ") or line.startswith("HETATM")):
            continue
        if len(line) < 26:
            continue
        res_name = line[17:20].strip()
        if res_name != "ACT":
            continue
        chain = line[21:22]
        res_seq = line[22:26]
        ins_code = line[26:27] if len(line) > 26 else " "
        atom_name = line[12:16].strip()
        key = (chain, res_seq, ins_code)
        residues.setdefault(key, set()).add(atom_name)

    candidates = {k for k, atoms in residues.items() if "N" not in atoms}
    if not candidates:
        return 0

    rewritten: list[str] = []
    for line in lines:
        if not (line.startswith("ATOM  ") or line.startswith("HETATM")):
            rewritten.append(line)
            continue
        if len(line) < 27 or line[17:20].strip() != "ACT":
            rewritten.append(line)
            continue
        key = (line[21:22], line[22:26], line[26:27])
        if key in candidates:
            rewritten.append(f"{line[:17]}{'ACE':>3}{line[20:]}")
        else:
            rewritten.append(line)

    local_pdb.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return len(candidates)


def _stop_container_group_if_possible() -> None:
    api_key = os.getenv("SALAD_API_KEY", "").strip()
    org = os.getenv("ORGANIZATION_NAME", "").strip()
    project = os.getenv("PROJECT_NAME", "").strip()
    cg_name = os.getenv("CONTAINER_GROUP_NAME", "").strip()
    if not (api_key and org and project and cg_name):
        return

    try:
        from salad_cloud_sdk import SaladCloudSdk

        sdk = SaladCloudSdk(api_key=api_key, timeout=10000)
        sdk.container_groups.stop_container_group(
            organization_name=org,
            project_name=project,
            container_group_name=cg_name,
        )
    except Exception:
        # Container exits cleanly even if stop API fails.
        return


def _emit_failure_artifacts(
    *,
    client: storage.Client,
    bucket: str,
    output_prefix: str,
    workspace: Path,
    worker_mode: str,
    exc: Exception,
    traceback_text: str,
) -> dict[str, Any]:
    failed_at = datetime.now(timezone.utc).isoformat()
    failure_receipt = {
        "schema_version": "salad_worker_failure_v1",
        "status": "failed",
        "mode": worker_mode,
        "job_id": os.getenv("MICA_JOB_ID", "").strip(),
        "failed_at": failed_at,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": traceback_text,
        "output_gcs_prefix": output_prefix,
        "container_group_name": os.getenv("CONTAINER_GROUP_NAME", "").strip(),
    }
    history = {
        "mode": worker_mode,
        "status": "failed",
        "failed_at": failed_at,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "failure_receipt_object": f"{output_prefix}/failure_receipt.json",
        "failure_traceback_object": f"{output_prefix}/failure_traceback.txt",
    }

    history_path = workspace / "history.json"
    history_path.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")
    _upload_file(client, bucket, history_path, f"{output_prefix}/{history_path.name}")
    _upload_json(client, bucket, history, f"{output_prefix}/worker_history.json")
    _upload_json(client, bucket, failure_receipt, f"{output_prefix}/failure_receipt.json")
    _upload_text(client, bucket, traceback_text, f"{output_prefix}/failure_traceback.txt")

    print(
        json.dumps(
            {
                "phase": "worker_failure",
                "error_type": failure_receipt["error_type"],
                "error_message": failure_receipt["error_message"],
                "failed_at": failed_at,
            }
        ),
        flush=True,
    )
    return failure_receipt


def _frame_interval_ps_from_report_freq(report_freq: int, timestep_ps: float) -> float:
    return max(float(report_freq), 1.0) * max(float(timestep_ps), 0.0)


def _write_protein_only_preview_frame(
    *,
    topology: Any,
    simulation: Any,
    target_path: Path,
) -> None:
    from openmm.app import PDBFile

    state = simulation.context.getState(getPositions=True)
    with target_path.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(topology, state.getPositions(), handle)


def _write_protein_only_bcif_preview(
    *,
    topology: Any,
    simulation: Any,
    preview_path: Path,
    frame_index: int,
    step: int,
    time_ps: float,
    source_topology_ref: str,
    source_trajectory_ref: str,
) -> dict[str, Any]:
    from mica.md_preview import encode_preview_frame

    enable_bcif_preview = os.getenv("ENABLE_BCIF_PREVIEW", os.getenv("MICA_ENABLE_BCIF_PREVIEW", "true")).strip().lower()
    preview_enabled = enable_bcif_preview not in {"0", "false", "no", "off"}
    preview_max_encode_ms = int(os.getenv("PREVIEW_MAX_ENCODE_MS", os.getenv("MICA_PREVIEW_MAX_ENCODE_MS", "30000")).strip() or "30000")
    preview_max_payload_bytes = int(os.getenv("PREVIEW_MAX_PAYLOAD_BYTES", os.getenv("MICA_PREVIEW_MAX_PAYLOAD_BYTES", str(8 * 1024 * 1024))).strip() or str(8 * 1024 * 1024))
    preview_fallback_format = os.getenv("PREVIEW_FALLBACK_FORMAT", os.getenv("MICA_PREVIEW_FALLBACK_FORMAT", "pdb")).strip().lower() or "pdb"
    preview_output_format = os.getenv("PREVIEW_OUTPUT_FORMAT", os.getenv("MICA_PREVIEW_OUTPUT_FORMAT", "bcif")).strip().lower() or "bcif"
    common: dict[str, Any] = {
        "enable_bcif_preview": preview_enabled,
        "requested_frame_interval_ps": os.getenv("FRAME_INTERVAL_PS", "50").strip() or "50",
        "preview_max_encode_ms": preview_max_encode_ms,
        "preview_max_payload_bytes": preview_max_payload_bytes,
        "preview_upload_to_gcs": True,
        "preview_inline_allowed": False,
        "preview_fallback_format": preview_fallback_format,
        "fallback_event_format": "artifact_ref",
        "pdb_path": str(preview_path),
        "mmcif_path": "",
        "bcif_path": "",
        "source_topology_ref": source_topology_ref,
        "source_positions_ref": str(preview_path),
        "source_trajectory_ref": source_trajectory_ref,
    }
    if not preview_enabled:
        return {
            **common,
            "status": "degraded",
            "bcif_preview_status": "degraded_or_not_implemented",
            "preview_payload_format": preview_fallback_format,
            "encoder": "",
            "error": "bcif_preview_disabled",
            "failure_code": "bcif_preview_disabled",
            "failure_detail": "ENABLE_BCIF_PREVIEW/MICA_ENABLE_BCIF_PREVIEW disabled BinaryCIF preview.",
            "content_type": "chemical/x-pdb",
            "size_bytes": preview_path.stat().st_size if preview_path.exists() else 0,
            "sha256": "",
            "dropped": False,
        }

    state = simulation.context.getState(getPositions=True)
    result = encode_preview_frame(
        topology,
        state.getPositions(),
        frame_index,
        step,
        time_ps,
        preview_output_format,
        preview_path.with_suffix(".bcif"),
        {
            **common,
            "fallback_pdb_path": str(preview_path),
            "encoder_bin": os.getenv("MICA_BCIF_ENCODER_BIN", "cif2bcif"),
        },
    )
    payload = result.to_dict()
    output_path = Path(str(payload.get("output_path") or "")) if payload.get("output_path") else None
    generated_mmcif_path = preview_path.with_suffix(".cif")
    mmcif_path_text = str(payload.get("mmcif_path") or (generated_mmcif_path if generated_mmcif_path.exists() else ""))
    return {
        **common,
        **payload,
        "bcif_preview_status": payload.get("bcif_preview_status") or "degraded_or_not_implemented",
        "preview_payload_format": payload.get("format") or preview_fallback_format,
        "encoder": payload.get("encoder") or "",
        "error": payload.get("failure_code") or "",
        "failure_code": payload.get("failure_code") or "",
        "failure_detail": payload.get("failure_detail") or "",
        "mmcif_path": mmcif_path_text,
        "bcif_path": str(output_path) if output_path and output_path.suffix.lower() == ".bcif" else "",
    }


def _upload_protein_only_runtime_status(
    *,
    client: storage.Client,
    bucket: str,
    output_prefix: str,
    latest_status: dict[str, Any],
    history: dict[str, Any],
    latest_status_path: Path,
    history_path: Path,
) -> None:
    latest_status_path.write_text(json.dumps(latest_status, indent=2, default=str), encoding="utf-8")
    history_path.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")
    _upload_file(client, bucket, latest_status_path, f"{output_prefix}/{latest_status_path.name}")
    _upload_file(client, bucket, history_path, f"{output_prefix}/{history_path.name}")
    _upload_json(client, bucket, history, f"{output_prefix}/worker_history.json")


def _run_simple_protein_only_job(
    *,
    client: storage.Client,
    bucket: str,
    pdb_bucket: str,
    pdb_object: str,
    checkpoint_object: str,
    output_prefix: str,
    workspace: Path,
    max_steps: int,
    benchmark_steps: int,
    report_freq: int,
    saving_interval_seconds: int,
) -> dict[str, Any]:
    from openmm import LangevinMiddleIntegrator, Platform
    from openmm.app import DCDReporter, ForceField, HBonds, NoCutoff, PDBFile, Simulation, StateDataReporter
    from openmm.unit import kelvin, picosecond, picoseconds
    from pdbfixer import PDBFixer

    local_pdb = workspace / "input.pdb"
    local_checkpoint = workspace / "checkpoint.cpt"
    latest_status_path = workspace / "latest_status.json"
    history_path = workspace / "history.json"
    prepared_topology_path = workspace / "prepared_topology.pdb"
    frame_interval_ps_requested = float(os.getenv("FRAME_INTERVAL_PS", "50").strip() or "50")
    chunk_steps_override = int(os.getenv("CHUNK_STEPS_OVERRIDE", "0").strip() or "0")
    timestep_ps = 0.002

    _download_required(client, pdb_bucket, pdb_object, local_pdb)
    had_checkpoint = _download_if_exists(client, bucket, checkpoint_object, local_checkpoint)

    act_fixed = _normalize_problematic_act_residues(local_pdb)
    if act_fixed:
        print(
            json.dumps(
                {
                    "phase": "pdb_normalization",
                    "action": "ACT->ACE",
                    "residues_renamed": act_fixed,
                }
            ),
            flush=True,
        )

    fixer = PDBFixer(filename=str(local_pdb))
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(pH=7.0)

    forcefield = ForceField("amber14-all.xml", "amber14/tip3pfb.xml")
    system = forcefield.createSystem(
        fixer.topology,
        nonbondedMethod=NoCutoff,
        constraints=HBonds,
    )
    integrator = LangevinMiddleIntegrator(300 * kelvin, 1 / picosecond, 0.002 * picoseconds)

    platform = Platform.getPlatformByName("CUDA") if any(
        Platform.getPlatform(i).getName() == "CUDA" for i in range(Platform.getNumPlatforms())
    ) else Platform.getPlatformByName("CPU")

    simulation = Simulation(fixer.topology, system, integrator, platform)
    if had_checkpoint:
        with open(local_checkpoint, "rb") as handle:
            simulation.context.loadCheckpoint(handle.read())
    else:
        simulation.context.setPositions(fixer.positions)
        simulation.minimizeEnergy(maxIterations=1000)

    with prepared_topology_path.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(fixer.topology, simulation.context.getState(getPositions=True).getPositions(), handle)
    _upload_file(client, bucket, prepared_topology_path, f"{output_prefix}/{prepared_topology_path.name}")

    steps_done = int(simulation.context.getState().getStepCount())
    if steps_done < max_steps:
        bench = min(benchmark_steps, max_steps - steps_done)
        start = time.time()
        simulation.step(bench)
        elapsed = max(time.time() - start, 1e-6)
        steps_done = int(simulation.context.getState().getStepCount())
        per_step = elapsed / bench if bench else 0.001
        chunk_steps = chunk_steps_override if chunk_steps_override > 0 else max(250, int(saving_interval_seconds / max(per_step, 1e-6)))
    else:
        chunk_steps = chunk_steps_override if chunk_steps_override > 0 else 250

    actual_frame_interval_ps = _frame_interval_ps_from_report_freq(report_freq, timestep_ps)
    frame_interval_reason = ""
    if abs(actual_frame_interval_ps - frame_interval_ps_requested) > 1e-6:
        frame_interval_reason = "configured_report_freq_or_timestep_delta"

    history = {
        "mode": "protein_only",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps_initial": steps_done,
        "report_freq": int(report_freq),
        "frame_interval_ps_requested": frame_interval_ps_requested,
        "actual_frame_interval_ps": actual_frame_interval_ps,
        "frame_interval_reason": frame_interval_reason,
        "chunk_steps": int(chunk_steps),
        "topology_object": f"{output_prefix}/{prepared_topology_path.name}",
        "events": [],
    }
    status_identity = {
        "route_decision_id": os.getenv("MICA_ROUTE_DECISION_ID", "").strip(),
        "provider": "salad",
        "provider_instance_id": os.getenv("CONTAINER_GROUP_NAME", "").strip(),
        "requested_gpu_type": os.getenv("REQUESTED_GPU_TYPE", "").strip(),
        "actual_gpu_type": os.getenv("ACTUAL_GPU_TYPE", "").strip() or os.getenv("SALAD_MACHINE_GPU", "").strip(),
        "image_digest": os.getenv("MICA_IMAGE_DIGEST", "").strip() or os.getenv("IMAGE_DIGEST", "").strip(),
        "source_target_id": os.getenv("SOURCE_TARGET_ID", "").strip() or os.getenv("MICA_SOURCE_TARGET_ID", "").strip(),
        "allocation_attempt": int(os.getenv("ALLOCATION_ATTEMPT", "0").strip() or "0"),
        "protocol_node_id": os.getenv("PROTOCOL_NODE_ID", "").strip(),
        "session_id": os.getenv("MICA_SESSION_ID", "").strip(),
        "gcs_prefix": f"gs://{bucket}/{output_prefix}",
    }
    _upload_protein_only_runtime_status(
        client=client,
        bucket=bucket,
        output_prefix=output_prefix,
        latest_status={
            "job_id": os.getenv("MICA_JOB_ID", "").strip(),
            "status": "running",
            "mode": "protein_only",
            "steps_done": steps_done,
            "max_steps": max_steps,
            "progress_percent": round((steps_done / max(max_steps, 1)) * 100.0, 4),
            "time_ps": round(steps_done * timestep_ps, 6),
            "frame_interval_ps_requested": frame_interval_ps_requested,
            "actual_frame_interval_ps": actual_frame_interval_ps,
            "frame_interval_reason": frame_interval_reason,
            "produced_at": datetime.now(timezone.utc).isoformat(),
            **status_identity,
        },
        history=history,
        latest_status_path=latest_status_path,
        history_path=history_path,
    )

    while steps_done < max_steps:
        run_steps = min(chunk_steps, max_steps - steps_done)
        chunk_id = steps_done + run_steps
        dcd_file = workspace / f"output_{chunk_id:012}.dcd"
        log_file = workspace / f"log_{chunk_id:012}.txt"
        preview_file = workspace / f"preview_frame_{chunk_id:012}.pdb"
        started_chunk_at = datetime.now(timezone.utc).isoformat()

        simulation.reporters.clear()
        simulation.reporters.append(DCDReporter(str(dcd_file), max(1, min(report_freq, run_steps))))
        simulation.reporters.append(
            StateDataReporter(str(log_file), max(1, min(report_freq, run_steps)), step=True, temperature=True)
        )

        simulation.step(run_steps)
        steps_done = int(simulation.context.getState().getStepCount())
        with open(local_checkpoint, "wb") as handle:
            handle.write(simulation.context.createCheckpoint())
        _write_protein_only_preview_frame(
            topology=fixer.topology,
            simulation=simulation,
            target_path=preview_file,
        )
        time_ps = round(steps_done * timestep_ps, 6)
        frame_index = max(int(steps_done // max(report_freq, 1)), 0)
        bcif_preview = _write_protein_only_bcif_preview(
            topology=fixer.topology,
            simulation=simulation,
            preview_path=preview_file,
            frame_index=frame_index,
            step=steps_done,
            time_ps=time_ps,
            source_topology_ref=f"{output_prefix}/{prepared_topology_path.name}",
            source_trajectory_ref=f"{output_prefix}/{dcd_file.name}",
        )
        multi_model_preview = _append_multi_model_preview(output_dir=workspace, preview_path=preview_file)

        _upload_file(client, bucket, dcd_file, f"{output_prefix}/{dcd_file.name}")
        _upload_file(client, bucket, log_file, f"{output_prefix}/{log_file.name}")
        _upload_file(client, bucket, preview_file, f"{output_prefix}/{preview_file.name}")
        bcif_preview_object = ""
        mmcif_preview_object = ""
        bcif_path_text = str(bcif_preview.get("bcif_path") or "").strip()
        mmcif_path_text = str(bcif_preview.get("mmcif_path") or "").strip()
        bcif_path = Path(bcif_path_text) if bcif_path_text else None
        mmcif_path = Path(mmcif_path_text) if mmcif_path_text else None
        if bcif_path is not None and bcif_path.exists():
            bcif_preview_object = f"{output_prefix}/{bcif_path.name}"
            _upload_file(client, bucket, bcif_path, bcif_preview_object)
        if mmcif_path is not None and mmcif_path.exists():
            mmcif_preview_object = f"{output_prefix}/{mmcif_path.name}"
            _upload_file(client, bucket, mmcif_path, mmcif_preview_object)
        if multi_model_preview is not None:
            _upload_file(client, bucket, multi_model_preview, f"{output_prefix}/{multi_model_preview.name}")
        _upload_file(client, bucket, local_checkpoint, checkpoint_object)

        event_ts = datetime.now(timezone.utc).isoformat()
        progress_percent = round((steps_done / max(max_steps, 1)) * 100.0, 4)
        event = {
            "ts": event_ts,
            "chunk_started_at": started_chunk_at,
            "steps_done": steps_done,
            "chunk_steps": run_steps,
            "chunk_id": chunk_id,
            "frame_index": frame_index,
            "time_ps": time_ps,
            "progress_percent": progress_percent,
            "requested_frame_interval_ps": frame_interval_ps_requested,
            "actual_frame_interval_ps": actual_frame_interval_ps,
            "frame_interval_reason": frame_interval_reason,
            "preview_object": f"{output_prefix}/{preview_file.name}",
            "bcif_preview_object": bcif_preview_object,
            "mmcif_preview_object": mmcif_preview_object,
            "multi_model_preview_object": f"{output_prefix}/multi_model_preview.pdb" if multi_model_preview is not None else "",
            "dcd_object": f"{output_prefix}/{dcd_file.name}",
            "log_object": f"{output_prefix}/{log_file.name}",
            "checkpoint_object": checkpoint_object,
            "topology_object": f"{output_prefix}/{prepared_topology_path.name}",
            "preview_contract": {
                "bcif_preview_status": str(bcif_preview.get("bcif_preview_status") or "degraded_or_not_implemented"),
                "preview_payload_format": str(bcif_preview.get("preview_payload_format") or "pdb"),
                "fallback_event_format": "artifact_ref",
                "preview_not_canonical": True,
                "enable_bcif_preview": bool(bcif_preview.get("enable_bcif_preview", True)),
                "requested_frame_interval_ps": frame_interval_ps_requested,
                "preview_max_encode_ms": int(bcif_preview.get("preview_max_encode_ms") or 0),
                "preview_max_payload_bytes": int(bcif_preview.get("preview_max_payload_bytes") or 0),
                "preview_upload_to_gcs": True,
                "preview_inline_allowed": False,
                "preview_fallback_format": str(bcif_preview.get("preview_fallback_format") or "pdb"),
                "pdb_preview_ref": f"{output_prefix}/{preview_file.name}",
                "bcif_preview_ref": bcif_preview_object,
                "mmcif_preview_ref": mmcif_preview_object,
                "multi_model_preview_ref": f"{output_prefix}/multi_model_preview.pdb" if multi_model_preview is not None else "",
                "encoder": str(bcif_preview.get("encoder") or ""),
                "encoder_error": str(bcif_preview.get("error") or ""),
                "failure_code": str(bcif_preview.get("failure_code") or ""),
                "failure_detail": str(bcif_preview.get("failure_detail") or ""),
                "content_type": str(bcif_preview.get("content_type") or ""),
                "size_bytes": int(bcif_preview.get("size_bytes") or 0),
                "sha256": str(bcif_preview.get("sha256") or ""),
                "source_topology_ref": f"{output_prefix}/{prepared_topology_path.name}",
                "source_positions_ref": f"{output_prefix}/{preview_file.name}",
                "source_trajectory_ref": f"{output_prefix}/{dcd_file.name}",
                "dropped": bool(bcif_preview.get("dropped", False)),
            },
            "produced_at": event_ts,
            **status_identity,
        }
        history["events"].append(event)
        history["last_chunk"] = dict(event)
        _upload_protein_only_runtime_status(
            client=client,
            bucket=bucket,
            output_prefix=output_prefix,
            latest_status={
                "job_id": os.getenv("MICA_JOB_ID", "").strip(),
                "status": "running" if steps_done < max_steps else "finalizing",
                "mode": "protein_only",
                "steps_done": steps_done,
                "max_steps": max_steps,
                "progress_percent": progress_percent,
                "time_ps": time_ps,
                "frame_index": frame_index,
                "frame_interval_ps_requested": frame_interval_ps_requested,
                "actual_frame_interval_ps": actual_frame_interval_ps,
                "frame_interval_reason": frame_interval_reason,
                "preview_object": f"{output_prefix}/{preview_file.name}",
                "bcif_preview_object": bcif_preview_object,
                "mmcif_preview_object": mmcif_preview_object,
                "multi_model_preview_object": f"{output_prefix}/multi_model_preview.pdb" if multi_model_preview is not None else "",
                "dcd_object": f"{output_prefix}/{dcd_file.name}",
                "log_object": f"{output_prefix}/{log_file.name}",
                "checkpoint_object": checkpoint_object,
                "topology_object": f"{output_prefix}/{prepared_topology_path.name}",
                "preview_contract": dict(event["preview_contract"]),
                "produced_at": event_ts,
                **status_identity,
            },
            history=history,
            latest_status_path=latest_status_path,
            history_path=history_path,
        )

    history["completed_at"] = datetime.now(timezone.utc).isoformat()
    history["topology_atoms"] = int(getattr(fixer.topology, "getNumAtoms", lambda: 0)())
    history["trajectory_atoms"] = int(getattr(fixer.topology, "getNumAtoms", lambda: 0)())
    history["topology_trajectory_parity"] = history["topology_atoms"] == history["trajectory_atoms"]
    _upload_protein_only_runtime_status(
        client=client,
        bucket=bucket,
        output_prefix=output_prefix,
        latest_status={
            "job_id": os.getenv("MICA_JOB_ID", "").strip(),
            "status": "completed",
            "mode": "protein_only",
            "steps_done": steps_done,
            "max_steps": max_steps,
            "progress_percent": 100.0,
            "time_ps": round(steps_done * timestep_ps, 6),
            "frame_interval_ps_requested": frame_interval_ps_requested,
            "actual_frame_interval_ps": actual_frame_interval_ps,
            "frame_interval_reason": frame_interval_reason,
            "topology_atoms": history["topology_atoms"],
            "trajectory_atoms": history["trajectory_atoms"],
            "topology_trajectory_parity": history["topology_trajectory_parity"],
            "produced_at": history["completed_at"],
            **status_identity,
        },
        history=history,
        latest_status_path=latest_status_path,
        history_path=history_path,
    )
    return history


def _run_complex_stability_job(
    *,
    client: storage.Client,
    bucket: str,
    pdb_bucket: str,
    pdb_object: str,
    checkpoint_object: str,
    output_prefix: str,
    workspace: Path,
) -> dict[str, Any]:
    from workers.dynamo.biodynamo.core.md_engine import MDEngine, MDJobConfig

    local_protein = workspace / "protein.pdb"
    local_docked_pose = workspace / "docked_ligand.pdb"
    local_checkpoint = workspace / "checkpoint.cpt"
    output_dir = workspace / "complex_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    _download_required(client, pdb_bucket, pdb_object, local_protein)
    docked_bucket = os.getenv("DOCKED_LIGAND_GCS_BUCKET", pdb_bucket).strip() or pdb_bucket
    docked_object = _require_env("DOCKED_LIGAND_GCS_OBJECT")
    _download_required(client, docked_bucket, docked_object, local_docked_pose)
    had_checkpoint = _download_if_exists(client, bucket, checkpoint_object, local_checkpoint)
    restored_output_objects = (
        _download_tree_if_exists(client, bucket, output_prefix, output_dir) if had_checkpoint else []
    )

    # The complex-stability processor is AMBER-first; preserve that contract
    # for public-route packets unless an explicit override is injected.
    forcefield = os.getenv("FORCEFIELD", "").strip() or os.getenv("MD_FORCEFIELD", "").strip()
    protein_ff = os.getenv("PROTEIN_FF", "").strip()
    sync_interval_seconds = int(os.getenv("SAVING_INTERVAL_SECONDS", "600"))

    cfg = MDJobConfig(
        simulation_mode="complex_stability",
        pdb_path=str(local_protein),
        ligand_smiles=_require_env("LIGAND_SMILES"),
        docked_ligand_pdb=str(local_docked_pose),
        forcefield=forcefield or "amber14sb",
        protein_ff=protein_ff,
        production_ns=float(os.getenv("PRODUCTION_NS", "100.0")),
        padding_nm=float(os.getenv("PADDING_NM", "1.1")),
        ionic_strength_M=float(os.getenv("IONIC_STRENGTH_M", "0.15")),
        temperature_K=float(os.getenv("TEMPERATURE_K", "300.0")),
        platform=os.getenv("OPENMM_PLATFORM", "auto"),
        gpu_id=os.getenv("OPENMM_GPU_ID", "0"),
        resume_state_path=str(local_checkpoint) if had_checkpoint else "",
        output_dir=str(output_dir),
        job_name=os.getenv("MICA_JOB_ID", "protein_ligand_md"),
    )

    started_at = datetime.now(timezone.utc).isoformat()
    engine = MDEngine()
    stop_event = None
    sync_thread = None
    if sync_interval_seconds > 0:
        stop_event, sync_thread = _start_periodic_complex_sync(
            client,
            bucket,
            output_dir,
            output_prefix,
            checkpoint_object,
            fallback_checkpoint=local_checkpoint,
            interval_seconds=sync_interval_seconds,
        )

    try:
        result = engine.run(cfg)
    finally:
        if stop_event is not None:
            stop_event.set()
        if sync_thread is not None:
            sync_thread.join(timeout=5.0)

    sync_result = _sync_complex_output_once(
        client,
        bucket,
        output_dir,
        output_prefix,
        checkpoint_object,
        fallback_checkpoint=local_checkpoint,
    )

    result_path = output_dir / "worker_result.json"
    result_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    uploaded_outputs = sync_result["uploaded_outputs"]

    return {
        "mode": "complex_stability",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "resume_requested": had_checkpoint,
        "resume_checkpoint": str(local_checkpoint) if had_checkpoint else "",
        "restored_output_objects": restored_output_objects,
        "result": result,
        "output_dir": str(output_dir),
        "uploaded_outputs": uploaded_outputs,
        "checkpoint_uploaded": sync_result["checkpoint_candidate"],
    }


def _run_cg_martini_job(
    *,
    client: storage.Client,
    bucket: str,
    cg_top_object: str,
    cg_gro_object: str,
    output_prefix: str,
    workspace: Path,
    max_steps: int,
    benchmark_steps: int,
    report_freq: int,
    saving_interval_seconds: int,
) -> dict[str, Any]:
    """
    CG/Martini runtime job (CG_NATIVE_RUN INSTRUCCION 11).

    Loads a pre-built Martini 3 topology (.top) + solvated coords (.gro) from
    GCS, materializes an ``openmm.System`` via ``martini_openmm.MartiniTopFile``,
    runs CG-native MD at the 20 fs timestep (NOT 2 fs AA), uploads prod.dcd /
    prod.log / prod.chk / final_state.pdb.

    This function does NOT do AA->CG mapping; that lives upstream in
    ``src/mica/sim/cg_martini/Martinize2Adapter``. The runtime provider
    (INSTRUCCION 12) is responsible for emitting the .top + .gro pair and
    staging them at CG_TOP_GCS_OBJECT / CG_GRO_GCS_OBJECT. This worker is the
    dumb muscle that consumes them.

    The 20 fs timestep is the standard Martini 3 production dt (Martini 2
    uses 20-40 fs). Using 2 fs here would alias CG forces and explode.

    For new deployments prefer ``_run_cg_martini_from_pdb_job`` (INSTRUCCION 25)
    which builds the system topology INSIDE the worker from just the AA PDB,
    sidestepping the 569MB solvated.gro upload that GCS rejects on this host.
    """
    import martini_openmm
    import openmm
    import openmm.unit as _unit
    from openmm import LangevinIntegrator, MonteCarloBarostat, Platform
    from openmm.app import DCDReporter, GromacsGroFile, PDBFile, Simulation, StateDataReporter

    if not cg_top_object.strip() or not cg_gro_object.strip():
        raise RuntimeError(
            "cg_martini worker mode requires CG_TOP_GCS_OBJECT and CG_GRO_GCS_OBJECT env vars "
            "(prebuilt Martini 3 topology + solvated coords). See cg_system_builder.py."
        )

    cg_topology_dir = workspace / "cg_topology"
    cg_topology_dir.mkdir(parents=True, exist_ok=True)
    local_top = cg_topology_dir / "system.top"
    local_gro = cg_topology_dir / "solvated.gro"
    local_dcd = cg_topology_dir / "prod.dcd"
    local_log = cg_topology_dir / "prod.log"
    local_chk = cg_topology_dir / "prod.chk"
    local_final_pdb = cg_topology_dir / "final_state.pdb"

    _download_required(client, bucket, cg_top_object, local_top)
    _download_required(client, bucket, cg_gro_object, local_gro)

    # CG runtime parameters — defaults match the canonical Martini 3 setup.
    cg_timestep_fs = float(os.getenv("MICA_CG_TIMESTEP_FS", "20").strip() or "20")
    cg_epsilon_r = float(os.getenv("MICA_CG_EPSILON_R", "15").strip() or "15")
    cg_temperature_K = float(os.getenv("MICA_CG_TEMPERATURE_K", "310").strip() or "310")
    cg_pressure_bar = float(os.getenv("MICA_CG_PRESSURE_BAR", "1").strip() or "1")
    cg_is_npt = os.getenv("MICA_CG_NPT", "1").strip().lower() not in ("", "0", "false", "no")
    cg_friction_ps = float(os.getenv("MICA_CG_FRICTION_PS", "10").strip() or "10")
    cg_nonbonded_cutoff_nm = float(os.getenv("MICA_CG_NONBONDED_CUTOFF_NM", "1.1").strip() or "1.1")

    started_at = _utc_now_iso()

    conf = GromacsGroFile(str(local_gro))
    box_vectors = conf.getPeriodicBoxVectors()

    top = martini_openmm.MartiniTopFile(
        str(local_top),
        periodicBoxVectors=box_vectors,
        epsilon_r=cg_epsilon_r,
    )
    system = top.create_system(nonbonded_cutoff=cg_nonbonded_cutoff_nm * _unit.nanometer)
    if cg_is_npt:
        system.addForce(
            MonteCarloBarostat(cg_pressure_bar * _unit.bar, cg_temperature_K * _unit.kelvin, 100)
        )

    # CG timestep = 20 fs (NOT 2 fs AA). This is the hard invariant.
    integrator = LangevinIntegrator(
        cg_temperature_K * _unit.kelvin,
        cg_friction_ps / _unit.picosecond,
        cg_timestep_fs * _unit.femtoseconds,
    )

    # Platform preference: CUDA > OpenCL > CPU > Reference. We probe each
    # name because not every image ships all four (CPU-only workers are common
    # for the cg_martini smoke lane).
    platform_name = ""
    platform_obj = None
    for candidate in ("CUDA", "OpenCL", "CPU", "Reference"):
        try:
            platform_obj = Platform.getPlatformByName(candidate)
            platform_name = candidate
            break
        except Exception:
            continue
    if platform_obj is None:
        raise RuntimeError("No OpenMM platform available for cg_martini job")

    simulation = Simulation(top.topology, system, integrator, platform_obj)
    simulation.context.setPositions(conf.getPositions())
    simulation.context.computeVirtualSites()

    state0 = simulation.context.getState(getEnergy=True)
    energy_initial_kjmol = float(
        state0.getPotentialEnergy().value_in_unit(_unit.kilojoule_per_mole)
    )

    simulation.minimizeEnergy(maxIterations=100)

    simulation.reporters.append(DCDReporter(str(local_dcd), max(1, report_freq)))
    simulation.reporters.append(
        StateDataReporter(
            str(local_log),
            max(1, report_freq),
            step=True,
            time=True,
            potentialEnergy=True,
            kineticEnergy=True,
            totalEnergy=True,
            temperature=True,
            volume=True,
        )
    )

    steps_done = 0
    wall_started = time.time()
    next_checkpoint_at = wall_started + max(int(saving_interval_seconds), 1)
    while steps_done < max_steps:
        run_steps = min(max(int(benchmark_steps), 1), max_steps - steps_done)
        simulation.step(run_steps)
        steps_done = int(simulation.context.getState().getStepCount())
        now = time.time()
        if now >= next_checkpoint_at or steps_done >= max_steps:
            with open(local_chk, "wb") as handle:
                handle.write(simulation.context.createCheckpoint())
            next_checkpoint_at = now + max(int(saving_interval_seconds), 1)
    wall_elapsed_s = max(time.time() - wall_started, 1e-6)

    state_final = simulation.context.getState(getEnergy=True, getPositions=True)
    energy_final_kjmol = float(
        state_final.getPotentialEnergy().value_in_unit(_unit.kilojoule_per_mole)
    )
    with open(str(local_final_pdb), "w", encoding="utf-8") as handle:
        PDBFile.writeFile(
            simulation.topology,
            state_final.getPositions(asNumpy=True),
            handle,
        )

    cg_output_prefix = f"{output_prefix}/cg_martini"
    uploaded_outputs: dict[str, str] = {}
    for local_path in (local_dcd, local_log, local_chk, local_final_pdb):
        object_name = f"{cg_output_prefix}/{local_path.name}"
        _upload_file(client, bucket, local_path, object_name)
        uploaded_outputs[local_path.name] = object_name

    history = {
        "status": "completed",
        "mode": "cg_martini",
        "worker_mode": "cg_martini",
        "started_at": started_at,
        "completed_at": _utc_now_iso(),
        "system_particle_count": int(system.getNumParticles()),
        "n_steps_run": int(steps_done),
        "wall_time_s": round(wall_elapsed_s, 6),
        "energy_initial_kjmol": round(energy_initial_kjmol, 6),
        "energy_final_kjmol": round(energy_final_kjmol, 6),
        "platform": platform_name,
        "timestep_fs": float(cg_timestep_fs),
        "temperature_K": float(cg_temperature_K),
        "pressure_bar": float(cg_pressure_bar) if cg_is_npt else 0.0,
        "is_npt": bool(cg_is_npt),
        "epsilon_r": float(cg_epsilon_r),
        "nonbonded_cutoff_nm": float(cg_nonbonded_cutoff_nm),
        "cg_top_input_object": cg_top_object,
        "cg_gro_input_object": cg_gro_object,
        "outputs": uploaded_outputs,
    }
    return history


# ---------------------------------------------------------------------------
# CG_NATIVE_RUN INSTRUCCION 25 -- worker-side AA->CG build
# ---------------------------------------------------------------------------
# Pre-INSTRUCCION-25, the runtime provider had to upload a pre-built
# Martini 3 .top + 569MB solvated.gro to GCS. GCS upload from this host
# capped at ~0.3 MB/s (INSTRUCCION 24 diagnostic) so the upload timed out
# at the 120s wall-clock deadline of google-cloud-storage retry. The
# 569MB CG system therefore never reached the worker.
#
# Fix: keep the AA->CG build INSIDE the worker. The provider now only
# stages the original AA PDB (typically 0.1-5 MB for a soluble protein,
# ~2.4 MB for CLCN7). The worker runs:
#   1. Martinize2Adapter.map_protein(pdb)  -> CG protein .gro + .itp(s)
#   2. INSANEAdapter.build(protein_gro)   -> membrane.gro + header-only .top
#   3. build_cg_system_bundle(...)         -> solvated.gro + system.top
# then continues into the CG runtime as before.
#
# This is the canonical "rebuild in worker" path that makes the lane
# independent of the host's GCS upload bandwidth.
# ---------------------------------------------------------------------------


def _run_cg_martini_from_pdb_job(
    *,
    client: storage.Client,
    bucket: str,
    pdb_bucket: str,
    pdb_object: str,
    output_prefix: str,
    workspace: Path,
    max_steps: int,
    benchmark_steps: int,
    report_freq: int,
    saving_interval_seconds: int,
) -> dict[str, Any]:
    """CG/Martini runtime job that builds the system topology in-worker.

    CG_NATIVE_RUN INSTRUCCION 25. Replaces the pre-built-CG upload path
    (see ``_run_cg_martini_job``) with an in-worker build pipeline:

      PDB (AA) -> Martinize2Adapter -> CG protein (.gro + molecule_*.itp)
                                 -> INSANEAdapter     -> membrane.gro + .top
                                 -> build_cg_system_bundle -> solvated.gro + system.top

    Once ``solvated.gro`` and ``system.top`` exist on the local worker
    filesystem, the rest of the runtime (MartiniTopFile -> System ->
    LangevinIntegrator -> DCDReporter) is identical to
    ``_run_cg_martini_job``.

    The PDB download IS small (<10MB even for huge complexes) so the
    upload-bottleneck pathology discovered in INSTRUCCION 24 does not
    apply. The worker is the canonical site for AA->CG mapping because
    the canonical image already has vermouth, martini_openmm, INSANE and
    DSSP installed (see Dockerfile.worker / INSTRUCCION 11).

    Required env vars (provided by ``prepare_salad_md_submission`` when
    ``cg_from_pdb_only=True``):
      - PDB_GCS_OBJECT (gs:// path to AA PDB)
    Optional env vars:
      - MICA_CG_GEOMETRY_CLASS       (default "flat_bilayer")
      - MICA_CG_LIPID_COMPOSITION    (default "POPC:1")
      - MICA_CG_SOLVENT              (default "PW")
      - MICA_CG_SALT_MM              (default 150)
      - MICA_CG_MEMBRANE_ENABLED     (default "1")
    Plus the same MICA_CG_TIMESTEP_FS / _EPSILON_R / _TEMPERATURE_K /
    _PRESSURE_BAR / _NPT / _FRICTION_PS / _NONBONDED_CUTOFF_NM knobs
    that ``_run_cg_martini_job`` consumes.
    """
    import martini_openmm
    import openmm
    import openmm.unit as _unit
    from openmm import LangevinIntegrator, MonteCarloBarostat, Platform
    from openmm.app import DCDReporter, GromacsGroFile, PDBFile, Simulation, StateDataReporter

    if not pdb_object.strip():
        raise RuntimeError(
            "cg_martini_from_pdb worker mode requires PDB_GCS_OBJECT env var "
            "(AA PDB is the only input; the worker builds CG topology locally)."
        )

    # ── Phase 0: download AA PDB ─────────────────────────────────────
    cg_build_dir = workspace / "cg_build"
    cg_build_dir.mkdir(parents=True, exist_ok=True)
    local_pdb = cg_build_dir / "input.pdb"
    _download_required(client, pdb_bucket, pdb_object, local_pdb)

    cg_runtime_params = {
        "timestep_fs": float(os.getenv("MICA_CG_TIMESTEP_FS", "20").strip() or "20"),
        "epsilon_r": float(os.getenv("MICA_CG_EPSILON_R", "15").strip() or "15"),
        "temperature_K": float(os.getenv("MICA_CG_TEMPERATURE_K", "310").strip() or "310"),
        "pressure_bar": float(os.getenv("MICA_CG_PRESSURE_BAR", "1").strip() or "1"),
        "is_npt": os.getenv("MICA_CG_NPT", "1").strip().lower() not in ("", "0", "false", "no"),
        "friction_ps": float(os.getenv("MICA_CG_FRICTION_PS", "10").strip() or "10"),
        "nonbonded_cutoff_nm": float(os.getenv("MICA_CG_NONBONDED_CUTOFF_NM", "1.1").strip() or "1.1"),
    }

    geometry_class = os.getenv("MICA_CG_GEOMETRY_CLASS", "flat_bilayer").strip() or "flat_bilayer"
    lipid_composition = os.getenv("MICA_CG_LIPID_COMPOSITION", "POPC:1").strip() or "POPC:1"
    solvent = os.getenv("MICA_CG_SOLVENT", "PW").strip() or "PW"
    salt_mM = int(os.getenv("MICA_CG_SALT_MM", "150").strip() or "150")
    membrane_enabled = os.getenv("MICA_CG_MEMBRANE_ENABLED", "1").strip().lower() not in (
        "", "0", "false", "no"
    )

    started_at = _utc_now_iso()
    build_timings: dict[str, float] = {}
    build_receipts: dict[str, Any] = {}

    # ── Phase 1: Martinize2Adapter.map_protein ───────────────────────
    # INSTRUCCION 29 (2026-07-21): tolerant import. Some pinned mica submodule
    # commits (e.g. 81a817c23 -- docs-only checkpoint before INSTRUCCION 13's
    # re-export refactor) carry `cg_martini/__init__.py` as a docstring stub
    # without the public re-exports added in 249875cb7. The submodules
    # (`martinize2_adapter.py`, `insane_adapter.py`, `cg_system_builder.py`)
    # are always present from f51fd29b6 onward, so we fall back to them
    # when the package-level re-export is unavailable. Remove this fallback
    # once the build repo's mica submodule pin is advanced past 249875cb7.
    try:
        from mica.sim.cg_martini import (  # noqa: F401  (re-exported surface)
            Martinize2Adapter,  # noqa: F401
            INSANEAdapter,  # noqa: F401
            CGSystemBuildRequest,  # noqa: F401
            build_cg_system_bundle,  # noqa: F401
        )
    except ImportError:  # pragma: no cover -- pre-INSTRUCCION 13 stub
        from mica.sim.cg_martini.martinize2_adapter import Martinize2Adapter  # noqa: F401
        from mica.sim.cg_martini.insane_adapter import INSANEAdapter  # noqa: F401
        from mica.sim.cg_martini.cg_system_builder import (  # noqa: F401
            CGSystemBuildRequest,  # noqa: F401
            build_cg_system_bundle,  # noqa: F401
        )

    martinize_out = cg_build_dir / "martinize2"
    martinize_out.mkdir(parents=True, exist_ok=True)
    martinize_adapter = Martinize2Adapter()
    t0 = time.time()
    martinize_receipt = martinize_adapter.map_protein(
        input_structure_ref=str(local_pdb),
        output_dir=str(martinize_out),
        ss_policy="dssp",
        en_policy="elnedyn",
    )
    build_timings["martinize2_seconds"] = round(time.time() - t0, 6)
    build_receipts["martinize2_status"] = martinize_receipt.status
    martinize_payload = martinize_receipt.payload
    protein_gro_ref = martinize_payload.output_cg_gro_ref if hasattr(martinize_payload, "output_cg_gro_ref") else ""
    itp_refs_csv = martinize_payload.output_cg_itp_ref if hasattr(martinize_payload, "output_cg_itp_ref") else ""
    if not protein_gro_ref or not Path(protein_gro_ref).is_file():
        raise RuntimeError(
            f"Martinize2Adapter did not produce protein CG .gro: {protein_gro_ref!r}. "
            f"Receipt status={martinize_receipt.status}, errors={getattr(martinize_payload, 'validation_errors', [])}"
        )
    molecule_itp_refs = [p.strip() for p in (itp_refs_csv or "").split(",") if p.strip()]
    if not molecule_itp_refs or not all(Path(p).is_file() for p in molecule_itp_refs):
        raise RuntimeError(
            f"Martinize2Adapter did not produce molecule_*.itp files. "
            f"itp_refs_csv={itp_refs_csv!r}, molecule_itp_refs={molecule_itp_refs!r}"
        )

    # ── Phase 2: INSANEAdapter.build ────────────────────────────────
    insane_out = cg_build_dir / "insane"
    insane_out.mkdir(parents=True, exist_ok=True)
    insane_receipt: Any = None
    insane_gro_ref = ""
    insane_top_ref = ""
    insane_counts: dict[str, int] = {}
    if membrane_enabled:
        insane_adapter = INSANEAdapter()
        t0 = time.time()
        insane_receipt = insane_adapter.build(
            protein_gro_ref=protein_gro_ref,
            output_dir=str(insane_out),
            builder="insane",
            geometry_class=geometry_class,
            lipid_composition=lipid_composition,
            solvent=solvent,
            salt_concentration=salt_mM / 1000.0,
            center_protein=True,
        )
        build_timings["insane_seconds"] = round(time.time() - t0, 6)
        build_receipts["insane_status"] = insane_receipt.status
        insane_payload = insane_receipt.payload
        insane_outputs = insane_payload.outputs if hasattr(insane_payload, "outputs") else {}
        insane_gro_ref = insane_outputs.get("gro_ref", "")
        insane_top_ref = insane_outputs.get("top_ref", "")
        insane_counts = dict(insane_payload.counts or {}) if hasattr(insane_payload, "counts") else {}
        if not insane_gro_ref or not Path(insane_gro_ref).is_file():
            raise RuntimeError(
                f"INSANEAdapter.build did not produce membrane.gro: {insane_gro_ref!r}. "
                f"Receipt status={insane_receipt.status}, errors={getattr(insane_payload, 'validation_errors', [])}"
            )

    # ── Phase 3: build_cg_system_bundle ─────────────────────────────
    bundle_out = cg_build_dir / "bundle"
    bundle_out.mkdir(parents=True, exist_ok=True)
    bundle_request = CGSystemBuildRequest(
        forcefield_family="martini3",
        martini_version="3.0.0",
        geometry_class=geometry_class,
        membrane_enabled=membrane_enabled,
        water_model="martini3",
        ion_model="martini3",
        salt_mM=salt_mM,
        lipid_composition=lipid_composition,
        solvent=solvent,
        martinize2_output_cg_gro_ref=protein_gro_ref,
        martinize2_output_itp_refs=molecule_itp_refs,
        insane_output_gro_ref=insane_gro_ref,
        insane_output_top_ref=insane_top_ref,
        insane_counts=insane_counts,
        output_dir=str(bundle_out),
        source_target_id=os.getenv("MICA_SOURCE_TARGET_ID", "cg_martini_from_pdb"),
        bundle_id=os.getenv("MICA_CG_RUNTIME_BUNDLE_ID", "cg_martini_from_pdb"),
    )
    t0 = time.time()
    bundle_payload = build_cg_system_bundle(bundle_request)
    build_timings["cg_system_builder_seconds"] = round(time.time() - t0, 6)
    if bundle_payload.implementation_status != "real_compile":
        raise RuntimeError(
            f"build_cg_system_bundle did not compile: status={bundle_payload.implementation_status}, "
            f"blockers={bundle_payload.blockers}"
        )

    local_top = Path(bundle_payload.topology_path)
    local_gro = Path(bundle_payload.coordinate_path)
    local_dcd = cg_build_dir / "prod.dcd"
    local_log = cg_build_dir / "prod.log"
    local_chk = cg_build_dir / "prod.chk"
    local_final_pdb = cg_build_dir / "final_state.pdb"

    conf = GromacsGroFile(str(local_gro))
    box_vectors = conf.getPeriodicBoxVectors()

    top = martini_openmm.MartiniTopFile(
        str(local_top),
        periodicBoxVectors=box_vectors,
        epsilon_r=cg_runtime_params["epsilon_r"],
    )
    system = top.create_system(
        nonbonded_cutoff=cg_runtime_params["nonbonded_cutoff_nm"] * _unit.nanometer
    )
    if cg_runtime_params["is_npt"]:
        system.addForce(
            MonteCarloBarostat(
                cg_runtime_params["pressure_bar"] * _unit.bar,
                cg_runtime_params["temperature_K"] * _unit.kelvin,
                100,
            )
        )

    integrator = LangevinIntegrator(
        cg_runtime_params["temperature_K"] * _unit.kelvin,
        cg_runtime_params["friction_ps"] / _unit.picosecond,
        cg_runtime_params["timestep_fs"] * _unit.femtoseconds,
    )

    platform_name = ""
    platform_obj = None
    for candidate in ("CUDA", "OpenCL", "CPU", "Reference"):
        try:
            platform_obj = Platform.getPlatformByName(candidate)
            platform_name = candidate
            break
        except Exception:
            continue
    if platform_obj is None:
        raise RuntimeError("No OpenMM platform available for cg_martini_from_pdb job")

    simulation = Simulation(top.topology, system, integrator, platform_obj)
    simulation.context.setPositions(conf.getPositions())
    simulation.context.computeVirtualSites()

    state0 = simulation.context.getState(getEnergy=True)
    energy_initial_kjmol = float(
        state0.getPotentialEnergy().value_in_unit(_unit.kilojoule_per_mole)
    )

    simulation.minimizeEnergy(maxIterations=100)

    # -- THROWAWAY EQUILIBRATION (delete once MDEngine.run_graph() handles CG multi-fase) ---
    # Minimal NVT -> NPT ramp to prevent NaN explosions on freshly-built CG/Martini
    # systems when the underlying system has residual clashes (protein-lipid, water
    # grid overlap, ion placement). Bounded: <=3000 steps total, no reporters during
    # equilibracion (so prod.dcd / prod.log stay clean for the dashboard).
    # INSTRUCCION 29 of CG_NATIVE_RUN program; commit message carries the THROWAWAY
    # label and references CG_NATIVE_RUN/CLEANUP_LOG entry.
    simulation.context.setVelocitiesToTemperature(
        cg_runtime_params["temperature_K"] * _unit.kelvin
    )
    nvt_integrator = LangevinIntegrator(
        cg_runtime_params["temperature_K"],
        cg_runtime_params["friction_ps"] / _unit.picosecond,
        cg_runtime_params["timestep_fs"] * _unit.femtoseconds,
    )
    simulation.context.setIntegrator(nvt_integrator)
    simulation.step(1000)  # 20 ps @ dt=20fs NVT thermalization
    npt_integrator = LangevinIntegrator(
        cg_runtime_params["temperature_K"],
        cg_runtime_params["friction_ps"] / _unit.picosecond,
        cg_runtime_params["timestep_fs"] * _unit.femtoseconds,
    )
    simulation.context.setIntegrator(npt_integrator)
    simulation.step(2000)  # 40 ps @ dt=20fs NPT equilibration
    # -- /THROWAWAY EQUILIBRATION -----------------------------------------------

    simulation.reporters.append(DCDReporter(str(local_dcd), max(1, report_freq)))
    simulation.reporters.append(
        StateDataReporter(
            str(local_log),
            max(1, report_freq),
            step=True,
            time=True,
            potentialEnergy=True,
            kineticEnergy=True,
            totalEnergy=True,
            temperature=True,
            volume=True,
        )
    )

    steps_done = 0
    wall_started = time.time()
    next_checkpoint_at = wall_started + max(int(saving_interval_seconds), 1)
    while steps_done < max_steps:
        run_steps = min(max(int(benchmark_steps), 1), max_steps - steps_done)
        simulation.step(run_steps)
        steps_done = int(simulation.context.getState().getStepCount())
        now = time.time()
        if now >= next_checkpoint_at or steps_done >= max_steps:
            with open(local_chk, "wb") as handle:
                handle.write(simulation.context.createCheckpoint())
            next_checkpoint_at = now + max(int(saving_interval_seconds), 1)
    wall_elapsed_s = max(time.time() - wall_started, 1e-6)

    state_final = simulation.context.getState(getEnergy=True, getPositions=True)
    energy_final_kjmol = float(
        state_final.getPotentialEnergy().value_in_unit(_unit.kilojoule_per_mole)
    )
    with open(str(local_final_pdb), "w", encoding="utf-8") as handle:
        PDBFile.writeFile(
            simulation.topology,
            state_final.getPositions(asNumpy=True),
            handle,
        )

    cg_output_prefix = f"{output_prefix}/cg_martini_from_pdb"
    uploaded_outputs: dict[str, str] = {}
    for local_path in (local_dcd, local_log, local_chk, local_final_pdb):
        object_name = f"{cg_output_prefix}/{local_path.name}"
        _upload_file(client, bucket, local_path, object_name)
        uploaded_outputs[local_path.name] = object_name

    history = {
        "status": "completed",
        "mode": "cg_martini_from_pdb",
        "worker_mode": "cg_martini_from_pdb",
        "started_at": started_at,
        "completed_at": _utc_now_iso(),
        "build_timings": build_timings,
        "build_receipts": build_receipts,
        "pdb_input_object": pdb_object,
        "pdb_input_local": str(local_pdb),
        "cg_protein_gro_ref": protein_gro_ref,
        "cg_membrane_gro_ref": insane_gro_ref,
        "cg_solvated_gro_ref": str(local_gro),
        "cg_system_top_ref": str(local_top),
        "insane_counts": insane_counts,
        "system_particle_count": int(system.getNumParticles()),
        "n_steps_run": int(steps_done),
        "wall_time_s": round(wall_elapsed_s, 6),
        "energy_initial_kjmol": round(energy_initial_kjmol, 6),
        "energy_final_kjmol": round(energy_final_kjmol, 6),
        "platform": platform_name,
        "timestep_fs": float(cg_runtime_params["timestep_fs"]),
        "temperature_K": float(cg_runtime_params["temperature_K"]),
        "pressure_bar": float(cg_runtime_params["pressure_bar"]) if cg_runtime_params["is_npt"] else 0.0,
        "is_npt": bool(cg_runtime_params["is_npt"]),
        "epsilon_r": float(cg_runtime_params["epsilon_r"]),
        "nonbonded_cutoff_nm": float(cg_runtime_params["nonbonded_cutoff_nm"]),
        "outputs": uploaded_outputs,
    }
    return history


def main() -> None:
    client = None
    bucket = ""
    prefix = ""
    worker_mode = "unknown"
    workspace = Path("/tmp/mica-md")
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        try: _decode_gcs_credentials()
        except Exception as exc: _emit_crash_diagnostic("decode_gcs_credentials", str(exc), traceback.format_exc())

        bucket = _require_env("GCS_BUCKET")
        prefix = os.getenv("GCS_PREFIX", "").strip().strip("/")
        pdb_bucket = os.getenv("PDB_GCS_BUCKET", bucket).strip() or bucket
        checkpoint_object = _safe_object_name(_require_env("CHECKPOINT_OBJECT"), bucket)
        output_prefix = _safe_object_name(_require_env("OUTPUT_GCS_PREFIX"), bucket).strip().rstrip("/")
        marker_object = _safe_object_name(_require_env("COMPLETED_MARKER_OBJECT"), bucket)
        max_steps = int(os.getenv("MAX_STEPS", "500000"))
        benchmark_steps = int(os.getenv("BENCHMARK_STEPS", "5000"))
        report_freq = int(os.getenv("REPORT_FREQ", "500"))
        saving_interval_seconds = int(os.getenv("SAVING_INTERVAL_SECONDS", "600"))
        workspace.mkdir(parents=True, exist_ok=True)
        client = storage.Client()
        worker_mode = _detect_worker_mode()
        # CG_NATIVE_RUN INSTRUCCION 11 — cg_martini mode does NOT consume a PDB.
        # Its inputs come from CG_TOP_GCS_OBJECT + CG_GRO_GCS_OBJECT. For all
        # other modes the AA PDB is required, so we keep the hard fail-fast.
        # CG_NATIVE_RUN INSTRUCCION 25 — cg_martini_from_pdb DOES consume a PDB.
        # It builds the Martini 3 topology IN-WORKER (Martinize2 + INSANE +
        # cg_system_builder) instead of receiving a pre-built CG bundle.
        pdb_object = "" if worker_mode == "cg_martini" else _require_env("PDB_GCS_OBJECT")
        bootstrap_manifest = _emit_bootstrap_artifacts(client=client, bucket_name=bucket, root_prefix=prefix, worker_mode=worker_mode)
        if worker_mode == "paper_dodecaedrica":
            history = _run_paper_dodecaedrica_job(client=client, bucket=bucket, pdb_bucket=pdb_bucket, pdb_object=pdb_object, checkpoint_object=checkpoint_object, output_prefix=output_prefix, workspace=workspace)
        elif worker_mode == "complex_stability":
            history = _run_complex_stability_job(client=client, bucket=bucket, pdb_bucket=pdb_bucket, pdb_object=pdb_object, checkpoint_object=checkpoint_object, output_prefix=output_prefix, workspace=workspace)
        elif worker_mode == "cg_martini":
            history = _run_cg_martini_job(
                client=client, bucket=bucket,
                cg_top_object=os.environ.get("CG_TOP_GCS_OBJECT", ""),
                cg_gro_object=os.environ.get("CG_GRO_GCS_OBJECT", ""),
                output_prefix=output_prefix, workspace=workspace,
                max_steps=max_steps, benchmark_steps=benchmark_steps,
                report_freq=report_freq, saving_interval_seconds=saving_interval_seconds,
            )
        elif worker_mode == "cg_martini_from_pdb":
            history = _run_cg_martini_from_pdb_job(
                client=client, bucket=bucket, pdb_bucket=pdb_bucket,
                pdb_object=pdb_object,
                output_prefix=output_prefix, workspace=workspace,
                max_steps=max_steps, benchmark_steps=benchmark_steps,
                report_freq=report_freq, saving_interval_seconds=saving_interval_seconds,
            )
        else:
            history = _run_simple_protein_only_job(client=client, bucket=bucket, pdb_bucket=pdb_bucket, pdb_object=pdb_object, checkpoint_object=checkpoint_object, output_prefix=output_prefix, workspace=workspace, max_steps=max_steps, benchmark_steps=benchmark_steps, report_freq=report_freq, saving_interval_seconds=saving_interval_seconds)
        history["prefix"] = prefix
        history["worker_mode"] = worker_mode
        history["bootstrap_manifest"] = bootstrap_manifest
        history_path = workspace / "history.json"
        history_path.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")
        _upload_file(client, bucket, history_path, f"{output_prefix}/{history_path.name}")
        _upload_json(client, bucket, history, f"{output_prefix}/worker_history.json")
        client.bucket(bucket).blob(marker_object).upload_from_string("ok", content_type="text/plain")
    except Exception as exc:
        if client is not None and bucket:
            _emit_failure_artifacts(client=client, bucket=bucket, output_prefix=output_prefix, workspace=workspace, worker_mode=worker_mode, exc=exc, traceback_text=traceback.format_exc())
        print(f"[MICA-CRASH] phase=main worker_mode={worker_mode} error={exc}", flush=True)
        print(f"[MICA-CRASH] traceback={traceback.format_exc()[:1000]}", flush=True)
        raise
    finally:
        _stop_container_group_if_possible()


if __name__ == "__main__":
    main()
