#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OutputSaver — Automatic GCS persistence for user compute data.
==================================================================

Wires the modern ``GCSUserStorage`` user-bucket plane into driver pipelines so
new compute artifacts are written canonically under ``mica-user``. The legacy
``UserStorageManager`` path remains only as a compatibility fallback for older
test harnesses or explicitly injected legacy callers.

Usage inside a driver::

    saver = OutputSaver.from_env()            # reads .env vars
    bucket = await saver.ensure_bucket(user_id)
    url    = await saver.save_result(
        user_id=user_id,
        run_id="dock_20260227_134500",
        filename="docking_results.json",
        data=json_bytes,
    )

Author  : MICA Team
Date    : 2026-02
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from mica.storage.compute_durability import (
    canonical_compute_object_path,
    infer_compute_lane,
)
from mica.storage.gcs_user_storage import GCSUserStorage, sanitize_content_type

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of heavy legacy deps (UserStorageManager wraps gcloud CLI)
# ---------------------------------------------------------------------------
_USM_LOADED = False


def _ensure_usm():
    global _USM_LOADED
    if _USM_LOADED:
        return
    _USM_LOADED = True
    try:
        from mica.infrastructure.storage.user_storage_manager import (
            UserStorageManager,
            UserBucket,
            UserQuota,
        )
        for _name, _obj in list(locals().items()):
            if not _name.startswith("_"):
                globals()[_name] = _obj
    except Exception as exc:  # pragma: no cover
        logger.warning("OutputSaver: UserStorageManager not importable: %s", exc)


class OutputSaver:
    """Facade that persists driver outputs to GCS on behalf of a user.

    Designed to be injected into any ``WorkerDriver`` subclass.  Keeps a
    cache of provisioned buckets so repeat calls are free.
    """

    def __init__(
        self,
        project_id: str,
        credentials_path: Optional[str] = None,
        region: str = "us-central1",
        storage_manager: Any | None = None,
        legacy_manager: Any | None = None,
    ) -> None:
        if credentials_path and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        self._storage: Any = storage_manager or GCSUserStorage(
            project=project_id,
            region=region,
            prefix=os.environ.get("GCS_BUCKET_PREFIX", "mica-user"),
            bucket_hash_len=int(os.environ.get("GCS_BUCKET_HASH_LEN", "12")),
            cors_origins=[
                origin.strip()
                for origin in os.environ.get("CORS_ALLOW_ORIGINS", "http://localhost:5173").split(",")
                if origin.strip()
            ],
            worker_sa_email=os.environ.get("MICA_WORKER_SA_EMAIL") or None,
        )
        self._manager: Any = legacy_manager
        self._buckets: Dict[str, Any] = {}  # user_id -> UserBucket

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "OutputSaver":
        """Build an ``OutputSaver`` from the standard ``.env`` variables.

        Expected env vars (loaded from .env or shell)::

            GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
            GCP_PROJECT_ID=dark-yen-...
            GCS_REGION=us-central1          # optional, defaults to us-central1
        """
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        project = os.environ.get(
            "GCP_PROJECT_ID", os.environ.get("GCP_PROJECT", "")
        )
        region = os.environ.get("GCS_REGION", "us-central1")

        if not project:
            raise EnvironmentError(
                "GCP_PROJECT_ID (or GCP_PROJECT) environment variable not set"
            )
        return cls(
            project_id=project,
            credentials_path=creds or None,
            region=region,
        )

    # ------------------------------------------------------------------
    # Bucket helpers
    # ------------------------------------------------------------------

    async def ensure_bucket(self, user_id: str) -> Any:
        """Provision (or fetch from cache) the user's GCS bucket."""
        if user_id in self._buckets:
            return self._buckets[user_id]
        if getattr(self, "_storage", None) is not None:
            bucket = self._storage.ensure_bucket(user_id)
        else:
            bucket = await self._manager.provision_user_bucket(user_id)
        self._buckets[user_id] = bucket
        return bucket

    @staticmethod
    def canonical_object_path(
        *,
        run_id: str,
        filename: str,
        subdir: str = "output",
        lane: str | None = None,
    ) -> str:
        resolved_lane = infer_compute_lane(run_id=run_id, subdir=subdir, lane=lane)
        return canonical_compute_object_path(
            lane=resolved_lane,
            run_id=run_id,
            job_id=run_id if resolved_lane in {"remote_md", "job"} else "",
            request_id=run_id if resolved_lane == "serverless" else "",
            section=subdir,
            filename=filename,
        )

    # ------------------------------------------------------------------
    # Core persistence
    # ------------------------------------------------------------------

    async def save_result(
        self,
        user_id: str,
        run_id: str,
        filename: str,
        data: Union[bytes, str, Dict[str, Any]],
        subdir: str = "output",
        lane: str | None = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Persist one result artifact to the user's bucket.

        Parameters
        ----------
        user_id : str
            Unique user / session identifier.
        run_id : str
            Run identifier — will be used as a subfolder inside *subdir*.
        filename : str
            Name of the file inside ``<subdir>/<run_id>/``.
        data : bytes | str | dict
            Payload. Dicts are serialised as JSON.
        subdir : str
            Top-level directory (``"output"``, ``"checkpoints"``, etc.)

        Returns
        -------
        str
            The ``gs://…`` URL of the uploaded file.
        """
        object_path = self.canonical_object_path(
            run_id=run_id,
            filename=filename,
            subdir=subdir,
            lane=lane,
        )

        if getattr(self, "_storage", None) is not None:
            if isinstance(data, dict):
                payload = json.dumps(data, indent=2, default=str).encode("utf-8")
                resolved_content_type = content_type or "application/json"
            elif isinstance(data, str):
                payload = data.encode("utf-8")
                resolved_content_type = content_type or "text/plain; charset=utf-8"
            else:
                payload = data
                resolved_content_type = content_type or "application/octet-stream"
            gcs_url = self._storage.upload_bytes(
                user_id=user_id,
                object_path=object_path,
                data=payload,
                content_type=sanitize_content_type(resolved_content_type),
                metadata={
                    "storage_authority": "mica-user",
                    "run_id": str(run_id),
                    "source_subdir": str(subdir),
                    **dict(metadata or {}),
                },
            )
            logger.info("OutputSaver: saved %s -> %s", filename, gcs_url)
            return gcs_url

        bucket = await self.ensure_bucket(user_id)
        gcs_subdir = f"{subdir}/{run_id}"

        # Materialise to a temporary local file
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=f"_{filename}", mode="wb"
        )
        try:
            if isinstance(data, dict):
                tmp.write(json.dumps(data, indent=2, default=str).encode("utf-8"))
            elif isinstance(data, str):
                tmp.write(data.encode("utf-8"))
            else:
                tmp.write(data)
            tmp.close()

            gcs_url = await self._manager.upload_file(
                bucket, gcs_subdir, filename, tmp.name
            )
            logger.info(
                "OutputSaver: saved %s → %s",
                filename,
                gcs_url,
            )
            return gcs_url
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    async def save_run(
        self,
        user_id: str,
        run_id: str,
        artifacts: Dict[str, Union[bytes, str, Dict[str, Any]]],
        subdir: str = "output",
    ) -> Dict[str, str]:
        """Persist multiple artifacts for a single run in one shot.

        Parameters
        ----------
        artifacts : dict
            Mapping ``{filename: data}``.

        Returns
        -------
        dict
            Mapping ``{filename: gcs_url}``.
        """
        urls: Dict[str, str] = {}
        for fname, payload in artifacts.items():
            url = await self.save_result(
                user_id=user_id,
                run_id=run_id,
                filename=fname,
                data=payload,
                subdir=subdir,
            )
            urls[fname] = url
        return urls

    # ------------------------------------------------------------------
    # Convenience: save an entire local directory
    # ------------------------------------------------------------------

    async def save_directory(
        self,
        user_id: str,
        run_id: str,
        local_dir: Union[str, Path],
        subdir: str = "output",
        extensions: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """Upload every file in *local_dir* (non-recursive) to GCS.

        Parameters
        ----------
        extensions : list[str] | None
            If given, only upload files whose suffix is in this list
            (e.g. ``[".dcd", ".csv", ".pdb"]``).
        """
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise FileNotFoundError(f"Not a directory: {local_dir}")

        urls: Dict[str, str] = {}
        for fpath in sorted(local_dir.iterdir()):
            if not fpath.is_file():
                continue
            if extensions and fpath.suffix not in extensions:
                continue
            with open(fpath, "rb") as fh:
                payload = fh.read()
            url = await self.save_result(
                user_id=user_id,
                run_id=run_id,
                filename=fpath.name,
                data=payload,
                subdir=subdir,
            )
            urls[fpath.name] = url
        return urls

    # ------------------------------------------------------------------
    # Query helpers (delegate to UserStorageManager)
    # ------------------------------------------------------------------

    async def list_user_runs(
        self, user_id: str
    ) -> List[Dict[str, Any]]:
        """List all files under ``output/`` for this user."""
        if getattr(self, "_storage", None) is not None:
            return self._storage.list_objects(user_id=user_id, prefix="jobs", include_metadata=True)
        bucket = await self.ensure_bucket(user_id)
        return await self._manager.list_user_files(bucket, subdir="output")

    async def get_signed_download_url(
        self,
        user_id: str,
        run_id: str,
        filename: str,
        hours: int = 4,
    ) -> str:
        """Generate a time-limited download URL for a specific artifact."""
        if getattr(self, "_storage", None) is not None:
            object_path = self.canonical_object_path(run_id=run_id, filename=filename, subdir="output")
            return self._storage.signed_url(
                user_id=user_id,
                object_path=object_path,
                method="GET",
                expires_seconds=int(hours) * 3600,
            )
        bucket = await self.ensure_bucket(user_id)
        return await self._manager.generate_signed_url(
            bucket,
            subdir=f"output/{run_id}",
            filename=filename,
            expiration_hours=hours,
        )

    # ------------------------------------------------------------------
    # Run-ID helpers
    # ------------------------------------------------------------------

    @staticmethod
    def make_run_id(prefix: str = "run") -> str:
        """Generate a timestamped run ID."""
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{ts}"
