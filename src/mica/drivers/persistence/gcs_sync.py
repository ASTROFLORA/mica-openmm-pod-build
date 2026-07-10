"""
GCS Artifact Sync — Cloud mirroring for driver-produced artifacts.
===================================================================

Provides best-effort upload of local driver artifacts to GCS after
each local write.  Uses the existing :class:`~mica.storage.gcs_user_storage.GCSUserStorage`
singleton to write into deterministic per-user buckets.

GCS object layout::

    driver_runs/{session_id}/runs/{run_id}/run_manifest.json
    driver_runs/{session_id}/runs/{run_id}/report_card.json
    driver_runs/{session_id}/runs/{run_id}/evidence_ledger.json
    driver_runs/{session_id}/runs/{run_id}/final_result.md
    driver_runs/{session_id}/conversation_log.json
    driver_runs/{session_id}/saga_log.jsonl

Design:
- **Dual-write**: Local persistence is unchanged; GCS upload happens
  after the local write succeeds (no data loss on cloud failure).
- **Best-effort**: Cloud failures are logged as warnings but never
  surface to the caller — the workflow continues gracefully.
- **Lazy init**: ``GCSUserStorage`` is resolved on first use so the
  module can be imported without GCS credentials available.
- **Run-scoped**: Each run within a session gets its own GCS prefix,
  preserving full run history in the cloud even though local storage
  uses a ``latest_run_overwrite_per_session_path`` policy.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _safe_segment(value: str, max_len: int = 120) -> str:
    """Sanitise a value for use as a GCS object-path segment."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown").strip())
    return text[:max_len] or "unknown"


class DriverArtifactSync:
    """Best-effort GCS mirror for driver artifacts.

    Instantiate once per driver (or per user) and call :meth:`sync_file`
    after each local write to upload a copy into the user's GCS bucket.

    Parameters
    ----------
    user_id:
        Canonical user identifier (scopes the GCS bucket).
    """

    def __init__(self, user_id: str) -> None:
        self._user_id = (user_id or "").strip()
        self._gcs: Any = None  # Lazy GCSUserStorage singleton
        self._init_attempted: bool = False
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lazy GCS initialisation
    # ------------------------------------------------------------------

    def _ensure_gcs(self) -> Any:
        """Return the ``GCSUserStorage`` singleton or ``None``."""
        if self._gcs is not None:
            return self._gcs
        if self._init_attempted:
            return None

        with self._init_lock:
            if self._gcs is not None:
                return self._gcs
            if self._init_attempted:
                return None
            self._init_attempted = True
            try:
                from mica.storage.gcs_user_storage import get_storage_manager
                self._gcs = get_storage_manager()
                logger.info(
                    "DriverArtifactSync: GCS backend initialised for user=%s",
                    self._user_id[:16] if self._user_id else "?",
                )
            except Exception as exc:
                logger.warning(
                    "DriverArtifactSync: GCS unavailable — artifacts stay local-only: %s",
                    exc,
                )
                self._gcs = None
        return self._gcs

    @property
    def is_cloud_ready(self) -> bool:
        """Return ``True`` if a cloud backend is available."""
        return self._ensure_gcs() is not None

    # ------------------------------------------------------------------
    # Object-path builders
    # ------------------------------------------------------------------

    @staticmethod
    def _run_object_path(session_id: str, run_id: str, filename: str) -> str:
        """Build a run-scoped GCS object path."""
        s = _safe_segment(session_id)
        r = _safe_segment(run_id)
        f = _safe_segment(filename, max_len=200)
        return f"driver_runs/{s}/runs/{r}/{f}"

    @staticmethod
    def _session_object_path(session_id: str, filename: str) -> str:
        """Build a session-scoped GCS object path (no run_id)."""
        s = _safe_segment(session_id)
        f = _safe_segment(filename, max_len=200)
        return f"driver_runs/{s}/{f}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_file(
        self,
        *,
        local_path: Path,
        session_id: str,
        run_id: str,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Optional[str]:
        """Upload *local_path* to GCS under the run-scoped prefix.

        Returns the ``gs://`` URI on success, ``None`` on failure or
        if GCS is unavailable.
        """
        if not self._user_id:
            return None
        gcs = self._ensure_gcs()
        if gcs is None:
            return None

        local = Path(local_path)
        if not local.exists():
            logger.debug("DriverArtifactSync: local file does not exist: %s", local)
            return None

        fname = filename or local.name
        object_path = self._run_object_path(session_id, run_id, fname)
        ct = content_type or self._guess_content_type(fname)

        try:
            uri = gcs.upload_file(
                user_id=self._user_id,
                object_path=object_path,
                local_path=local,
                content_type=ct,
            )
            logger.debug("DriverArtifactSync: uploaded %s → %s", local.name, uri)
            return uri
        except Exception as exc:
            logger.warning(
                "DriverArtifactSync: upload failed for %s: %s",
                object_path,
                exc,
            )
            return None

    def sync_session_file(
        self,
        *,
        local_path: Path,
        session_id: str,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Optional[str]:
        """Upload a session-level file (not run-scoped)."""
        if not self._user_id:
            return None
        gcs = self._ensure_gcs()
        if gcs is None:
            return None

        local = Path(local_path)
        if not local.exists():
            return None

        fname = filename or local.name
        object_path = self._session_object_path(session_id, fname)
        ct = content_type or self._guess_content_type(fname)

        try:
            uri = gcs.upload_file(
                user_id=self._user_id,
                object_path=object_path,
                local_path=local,
                content_type=ct,
            )
            logger.debug("DriverArtifactSync: uploaded session file %s → %s", fname, uri)
            return uri
        except Exception as exc:
            logger.warning(
                "DriverArtifactSync: session upload failed for %s: %s",
                object_path,
                exc,
            )
            return None

    def list_run_artifacts(
        self,
        session_id: str,
        run_id: str,
    ) -> list[Dict[str, Any]]:
        """List artifacts for a specific run in GCS.

        Returns a list of dicts with ``name``, ``size_bytes``, ``updated_at``.
        """
        gcs = self._ensure_gcs()
        if gcs is None:
            return []
        prefix = self._run_object_path(session_id, run_id, "")[:-1]  # strip trailing filename placeholder
        try:
            bucket_info = gcs.ensure_bucket(self._user_id)
            bucket = gcs.client.bucket(bucket_info.bucket_name)
            blobs = bucket.list_blobs(prefix=prefix)
            results = []
            for blob in blobs:
                results.append({
                    "name": blob.name.split("/")[-1] if "/" in blob.name else blob.name,
                    "object_path": blob.name,
                    "size_bytes": blob.size,
                    "updated_at": blob.updated.isoformat() if blob.updated else None,
                })
            return results
        except Exception as exc:
            logger.warning("DriverArtifactSync: list_run_artifacts failed: %s", exc)
            return []

    def list_session_runs(self, session_id: str) -> list[str]:
        """List all run_ids for a session stored in GCS."""
        gcs = self._ensure_gcs()
        if gcs is None:
            return []
        prefix = f"driver_runs/{_safe_segment(session_id)}/runs/"
        try:
            bucket_info = gcs.ensure_bucket(self._user_id)
            bucket = gcs.client.bucket(bucket_info.bucket_name)
            # Use delimiter to get "directories" only
            iterator = bucket.list_blobs(prefix=prefix, delimiter="/")
            # Consume the page to populate prefixes
            _ = list(iterator)
            run_ids = []
            for p in iterator.prefixes:
                # p looks like "driver_runs/{sid}/runs/{run_id}/"
                parts = p.rstrip("/").split("/")
                if parts:
                    run_ids.append(parts[-1])
            return sorted(run_ids)
        except Exception as exc:
            logger.warning("DriverArtifactSync: list_session_runs failed: %s", exc)
            return []

    def signed_download_url(
        self,
        *,
        session_id: str,
        run_id: str,
        filename: str,
        expires_seconds: int = 3600,
    ) -> Optional[str]:
        """Generate a signed download URL for a specific artifact."""
        gcs = self._ensure_gcs()
        if gcs is None:
            return None
        object_path = self._run_object_path(session_id, run_id, filename)
        try:
            return gcs.signed_url(
                user_id=self._user_id,
                object_path=object_path,
                method="GET",
                expires_seconds=expires_seconds,
            )
        except Exception as exc:
            logger.warning("DriverArtifactSync: signed_url failed: %s", exc)
            return None

    def storage_snapshot(self) -> Dict[str, Any]:
        """Return a diagnostic snapshot for ``_runtime_storage_snapshot``."""
        gcs = self._ensure_gcs()
        if gcs is not None:
            try:
                bucket_info = gcs.ensure_bucket(self._user_id)
                return {
                    "cloud_backend_configured": True,
                    "artifact_backend": "gcs_dual_write",
                    "bucket": bucket_info.bucket_name,
                    "object_prefix": "driver_runs/",
                    "user_id_hash": bucket_info.bucket_name.split("-")[-1] if "-" in bucket_info.bucket_name else "",
                }
            except Exception as exc:
                return {
                    "cloud_backend_configured": False,
                    "artifact_backend": "local_filesystem",
                    "gcs_error": str(exc),
                }
        return {
            "cloud_backend_configured": False,
            "artifact_backend": "local_filesystem",
            "gcs_error": "GCSUserStorage not available",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        return {
            ".json": "application/json",
            ".jsonl": "application/x-ndjson",
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".pdf": "application/pdf",
            ".pdb": "chemical/x-pdb",
            ".xml": "application/xml",
        }.get(ext, "application/octet-stream")
