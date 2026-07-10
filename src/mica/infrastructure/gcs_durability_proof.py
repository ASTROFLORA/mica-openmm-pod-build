"""SP-02 DURABLE_STORAGE_PROOF — GCS round-trip durability harness.

Proves that the canonical mica-user bucket authority is fully operational:
  1. Upload — PUT a deterministic probe blob
  2. Exists  — HEAD / list to confirm presence
  3. Read    — GET the blob back
  4. Verify  — SHA-256 round-trip matches
  5. Cleanup — DELETE blob
  6. Emit    — Structured JSON manifest with full evidence

Design constraints:
- No FastAPI or heavyweight web deps; only google-cloud-storage + stdlib
- Reads env from .env via seed_env_from_dotenv() before any GCS call
- Bucket derived via canonical compute_user_bucket_name() from compute_durability
- Never writes to production prefixes — uses sp02-probe/ sub-tree
- All exceptions captured as failure ledger; never raises to caller

Returns a dict with .decision.status in {"pass", "blocked"} plus full manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mica.config.dotenv_loader import seed_env_from_dotenv
from mica.storage.compute_durability import (
    COMPUTE_STORAGE_AUTHORITY,
    compute_user_bucket_name,
)

_PROBE_PREFIX = "sp02-probe"
_PROBE_CONTENT = b"MICA-SP02-DURABLE-STORAGE-PROOF-v1\n"
_PROBE_SHA256 = hashlib.sha256(_PROBE_CONTENT).hexdigest()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _gcs_client() -> Any:
    """Lazy GCS client import to avoid top-level dep failures."""
    from google.cloud import storage  # type: ignore

    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv(
        "MICA_GOOGLE_APPLICATION_CREDENTIALS"
    )
    project = os.getenv("GCP_PROJECT_ID") or os.getenv("GCP_PROJECT") or os.getenv(
        "GOOGLE_CLOUD_PROJECT"
    )
    if creds_path:
        return storage.Client.from_service_account_json(creds_path, project=project)
    return storage.Client(project=project)


def run_gcs_durability_proof(
    *,
    user_id: str = "sp02-probe-user",
    run_id: str | None = None,
    bucket_prefix: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute full GCS round-trip proof and return structured evidence dict."""
    seed_env_from_dotenv()

    probe_run_id = run_id or f"sp02-{uuid.uuid4().hex[:12]}"
    resolved_prefix = (
        bucket_prefix
        or os.getenv("GCS_BUCKET_PREFIX")
        or COMPUTE_STORAGE_AUTHORITY
    )
    bucket_name = compute_user_bucket_name(user_id, prefix=resolved_prefix)
    blob_path = f"{_PROBE_PREFIX}/{probe_run_id}/probe.txt"

    started_at = _utcnow()
    steps: list[dict[str, Any]] = []
    final_status = "blocked"
    reason_code = "unknown"
    remediation_hint = ""

    def step(name: str, ok: bool, detail: str = "", latency_ms: float | None = None) -> None:
        entry: dict[str, Any] = {"step": name, "ok": ok, "detail": detail}
        if latency_ms is not None:
            entry["latency_ms"] = round(latency_ms, 1)
        steps.append(entry)

    # ── Phase 1: Credentials check ───────────────────────────────────────────
    try:
        import time
        t0 = time.monotonic()
        client = _gcs_client()
        step("credentials", True, "GCS client initialised", (time.monotonic() - t0) * 1000)
    except Exception as exc:
        step("credentials", False, str(exc))
        reason_code = "gcs_credentials_missing"
        remediation_hint = (
            "Set GOOGLE_APPLICATION_CREDENTIALS to a valid service account JSON path "
            "or provide GOOGLE_APPLICATION_CREDENTIALS_JSON inline."
        )
        return _build_result(
            status="blocked",
            reason_code=reason_code,
            remediation_hint=remediation_hint,
            bucket_name=bucket_name,
            blob_path=blob_path,
            bucket_prefix=resolved_prefix,
            user_id=user_id,
            probe_run_id=probe_run_id,
            steps=steps,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    # ── Phase 2: Bucket ensure ───────────────────────────────────────────────
    import time

    try:
        t0 = time.monotonic()
        bucket = client.bucket(bucket_name)
        exists = bucket.exists()
        if not exists:
            bucket.storage_class = "STANDARD"
            region = os.getenv("GCS_REGION", "us-central1")
            bucket.location = region
            try:
                from google.api_core.exceptions import Conflict  # type: ignore

                bucket.create()
            except Conflict:
                pass  # race — already created
        step("bucket_ensure", True, f"bucket={bucket_name} exists={exists}", (time.monotonic() - t0) * 1000)
    except Exception as exc:
        step("bucket_ensure", False, str(exc))
        reason_code = "gcs_bucket_unavailable"
        remediation_hint = (
            "Verify GCP project permissions: roles/storage.admin or roles/storage.objectAdmin required. "
            f"Target bucket: {bucket_name}"
        )
        return _build_result(
            status="blocked",
            reason_code=reason_code,
            remediation_hint=remediation_hint,
            bucket_name=bucket_name,
            blob_path=blob_path,
            bucket_prefix=resolved_prefix,
            user_id=user_id,
            probe_run_id=probe_run_id,
            steps=steps,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    # ── Phase 3: Upload ──────────────────────────────────────────────────────
    try:
        t0 = time.monotonic()
        blob = bucket.blob(blob_path)
        blob.upload_from_string(_PROBE_CONTENT, content_type="text/plain; charset=utf-8", timeout=timeout_seconds)
        step("upload", True, f"blob_path={blob_path} size={len(_PROBE_CONTENT)}", (time.monotonic() - t0) * 1000)
    except Exception as exc:
        step("upload", False, str(exc))
        reason_code = "gcs_upload_failed"
        remediation_hint = "Check IAM permissions for storage.objects.create on the target bucket."
        return _build_result(
            status="blocked",
            reason_code=reason_code,
            remediation_hint=remediation_hint,
            bucket_name=bucket_name,
            blob_path=blob_path,
            bucket_prefix=resolved_prefix,
            user_id=user_id,
            probe_run_id=probe_run_id,
            steps=steps,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    # ── Phase 4: List / exists ───────────────────────────────────────────────
    try:
        t0 = time.monotonic()
        blobs = list(client.list_blobs(bucket_name, prefix=f"{_PROBE_PREFIX}/{probe_run_id}/"))
        found_names = [b.name for b in blobs]
        found = blob_path in found_names
        step("list_exists", found, f"found={found} listed={found_names}", (time.monotonic() - t0) * 1000)
        if not found:
            raise RuntimeError(f"Blob not found after upload: {blob_path}")
    except Exception as exc:
        step("list_exists", False, str(exc))
        reason_code = "gcs_list_failed"
        remediation_hint = "Check IAM permissions for storage.objects.list on the target bucket."
        return _build_result(
            status="blocked",
            reason_code=reason_code,
            remediation_hint=remediation_hint,
            bucket_name=bucket_name,
            blob_path=blob_path,
            bucket_prefix=resolved_prefix,
            user_id=user_id,
            probe_run_id=probe_run_id,
            steps=steps,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    # ── Phase 5: Read + SHA-256 verify ──────────────────────────────────────
    try:
        t0 = time.monotonic()
        downloaded = blob.download_as_bytes(timeout=timeout_seconds)
        actual_sha256 = hashlib.sha256(downloaded).hexdigest()
        sha_match = actual_sha256 == _PROBE_SHA256
        step(
            "read_verify",
            sha_match,
            f"sha256_match={sha_match} expected={_PROBE_SHA256[:16]}... got={actual_sha256[:16]}...",
            (time.monotonic() - t0) * 1000,
        )
        if not sha_match:
            raise RuntimeError(f"SHA-256 mismatch: expected {_PROBE_SHA256}, got {actual_sha256}")
    except Exception as exc:
        step("read_verify", False, str(exc))
        reason_code = "gcs_read_verify_failed"
        remediation_hint = "Data integrity failure: SHA-256 mismatch or download error. Suspect network or storage corruption."
        return _build_result(
            status="blocked",
            reason_code=reason_code,
            remediation_hint=remediation_hint,
            bucket_name=bucket_name,
            blob_path=blob_path,
            bucket_prefix=resolved_prefix,
            user_id=user_id,
            probe_run_id=probe_run_id,
            steps=steps,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    # ── Phase 6: Cleanup ─────────────────────────────────────────────────────
    try:
        t0 = time.monotonic()
        blob.delete(timeout=timeout_seconds)
        # Verify deletion
        still_exists = blob.exists(timeout=timeout_seconds)
        step("cleanup", not still_exists, f"deleted=True still_exists={still_exists}", (time.monotonic() - t0) * 1000)
    except Exception as exc:
        # Non-fatal: probe succeeded; log but don't block
        step("cleanup", False, f"non-fatal: {exc}")

    # ── All phases passed ────────────────────────────────────────────────────
    final_status = "pass"
    reason_code = "ok"
    return _build_result(
        status=final_status,
        reason_code=reason_code,
        remediation_hint="",
        bucket_name=bucket_name,
        blob_path=blob_path,
        bucket_prefix=resolved_prefix,
        user_id=user_id,
        probe_run_id=probe_run_id,
        steps=steps,
        started_at=started_at,
        finished_at=_utcnow(),
    )


def _build_result(
    *,
    status: str,
    reason_code: str,
    remediation_hint: str,
    bucket_name: str,
    blob_path: str,
    bucket_prefix: str,
    user_id: str,
    probe_run_id: str,
    steps: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    passed_count = sum(1 for s in steps if s["ok"])
    all_ok = all(s["ok"] for s in steps)
    return {
        "program": "SP-02-DURABLE_STORAGE_PROOF",
        "probe_run_id": probe_run_id,
        "decision": {
            "status": status,
            "not_durable": status != "pass",
            "reason_code": reason_code,
            "remediation_hint": remediation_hint,
        },
        "manifest": {
            "storage_authority": COMPUTE_STORAGE_AUTHORITY,
            "bucket_prefix": bucket_prefix,
            "bucket_name": bucket_name,
            "blob_path": blob_path,
            "probe_sha256": _PROBE_SHA256,
            "probe_size_bytes": len(_PROBE_CONTENT),
            "user_id": user_id,
            "storage_target": f"gs://{bucket_name}/{blob_path}",
        },
        "phases": {
            "credentials": _find_step(steps, "credentials"),
            "bucket_ensure": _find_step(steps, "bucket_ensure"),
            "upload": _find_step(steps, "upload"),
            "list_exists": _find_step(steps, "list_exists"),
            "read_verify": _find_step(steps, "read_verify"),
            "cleanup": _find_step(steps, "cleanup"),
        },
        "summary": {
            "steps_total": len(steps),
            "steps_passed": passed_count,
            "steps_failed": len(steps) - passed_count,
            "all_phases_ok": all_ok,
        },
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _find_step(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for s in steps:
        if s["step"] == name:
            return s
    return None
