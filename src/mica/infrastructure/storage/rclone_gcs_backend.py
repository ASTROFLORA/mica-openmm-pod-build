from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from mica.config.dotenv_loader import seed_env_from_dotenv
from mica.storage.gcs_user_storage import get_storage_manager

logger = logging.getLogger(__name__)

RCLONE_TYPE_GCS = "google cloud storage"

# Token refresh margin — renew when less than this many seconds remain.
_TOKEN_REFRESH_MARGIN_S = 120


@dataclass
class RcloneGCSRemote:
    user_id: str
    bucket_name: str
    project: str
    region: str
    remote_name: str
    object_prefix: str = ""
    rclone_bin: str = "rclone"
    service_account_credentials_json: str = ""
    # ── W5-1: OAuth2 short-lived token support ──────────────────
    use_oauth2_token: bool = False
    _credentials: Any = field(default=None, repr=False)
    _token: str = field(default="", repr=False)
    _token_expiry: float = field(default=0.0, repr=False)

    @property
    def env_prefix(self) -> str:
        return self.remote_name.upper().replace("-", "_")

    @property
    def remote_root(self) -> str:
        base = f"{self.remote_name}:{self.bucket_name}"
        prefix = self.object_prefix.strip("/")
        return f"{base}/{prefix}" if prefix else base

    def remote_path(self, relative_path: str = "") -> str:
        rel = relative_path.replace("\\", "/").strip("/")
        if rel == ".":
            rel = ""
        return f"{self.remote_root}/{rel}" if rel else self.remote_root

    # ── W5-1: Token lifecycle ────────────────────────────────────

    def _ensure_fresh_token(self) -> str:
        """Return a valid short-lived access token, refreshing if needed.

        Uses google.auth service-account credentials scoped to
        devstorage.full_control.  Tokens live ~3600 s; we refresh early.
        """
        now = time.time()
        if self._token and now < (self._token_expiry - _TOKEN_REFRESH_MARGIN_S):
            return self._token

        if self._credentials is None:
            from google.oauth2 import service_account as _sa
            import google.auth.transport.requests as _tr

            sa_info = json.loads(self.service_account_credentials_json)
            self._credentials = _sa.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/devstorage.full_control"],
            )

        import google.auth.transport.requests as _tr

        self._credentials.refresh(_tr.Request())
        self._token = self._credentials.token
        # expiry is a datetime; convert to epoch
        if self._credentials.expiry:
            self._token_expiry = self._credentials.expiry.timestamp()
        else:
            self._token_expiry = now + 3500  # conservative fallback
        logger.debug(
            "Refreshed GCS OAuth2 token for remote %s (expires in %.0f s)",
            self.remote_name,
            self._token_expiry - now,
        )
        return self._token

    def to_env(self) -> Dict[str, str]:
        prefix = self.env_prefix
        base = {
            f"RCLONE_CONFIG_{prefix}_TYPE": RCLONE_TYPE_GCS,
            f"RCLONE_CONFIG_{prefix}_BUCKET_POLICY_ONLY": "true",
        }

        # Prefer short-lived token when enabled (W5-1 secure path)
        if self.use_oauth2_token and self.service_account_credentials_json:
            token = self._ensure_fresh_token()
            # rclone GCS backend: token JSON envelope expected by rclone
            token_json = json.dumps({
                "access_token": token,
                "token_type": "Bearer",
                "expiry": "2099-01-01T00:00:00Z",  # rclone ignores this for GCS SA tokens
            }, separators=(",", ":"))
            base[f"RCLONE_CONFIG_{prefix}_TOKEN"] = token_json
            return base

        # Legacy path: full SA JSON (backward compat)
        if not self.service_account_credentials_json:
            raise RuntimeError("GCS service account credentials JSON not loaded")
        base[f"RCLONE_CONFIG_{prefix}_SERVICE_ACCOUNT_CREDENTIALS"] = (
            self.service_account_credentials_json
        )
        return base

    def command_env(self, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(self.to_env())
        if extra_env:
            env.update(extra_env)
        return env

    def run(self, *args: str, extra_env: Optional[Dict[str, str]] = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.rclone_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.command_env(extra_env),
            check=False,
        )

    def copyto(self, local_path: str | Path, remote_relative_path: str, *, timeout: int = 300) -> str:
        source = str(local_path)
        dest = self.remote_path(remote_relative_path)
        result = self.run("copyto", source, dest, "--checkers", "4", "--transfers", "1", timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"rclone copyto failed: {result.stderr or result.stdout}")
        return dest

    def copy_from_remote(self, remote_relative_path: str, local_path: str | Path, *, timeout: int = 300) -> str:
        source = self.remote_path(remote_relative_path)
        dest = str(local_path)
        result = self.run("copyto", source, dest, "--checkers", "4", "--transfers", "1", timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"rclone copyto(download) failed: {result.stderr or result.stdout}")
        return dest

    def check(self, local_path: str | Path, remote_relative_path: str, *, timeout: int = 300) -> None:
        remote_parent = self.remote_path(str(Path(remote_relative_path).parent).replace("\\", "/"))
        file_filter = Path(remote_relative_path).name
        result = self.run(
            "check",
            str(Path(local_path).parent),
            remote_parent,
            "--one-way",
            "--include",
            file_filter,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"rclone check failed: {result.stderr or result.stdout}")

    def lsf(self, remote_relative_path: str = "", *, timeout: int = 120) -> str:
        result = self.run("lsf", self.remote_path(remote_relative_path), timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"rclone lsf failed: {result.stderr or result.stdout}")
        return result.stdout

    def purge(self, remote_relative_path: str = "", *, timeout: int = 300) -> None:
        result = self.run("purge", self.remote_path(remote_relative_path), timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"rclone purge failed: {result.stderr or result.stdout}")

    def audit_snapshot(self) -> Dict[str, Any]:
        redacted_keys = sorted(self.to_env().keys())
        version = self.run("version", timeout=60)
        return {
            "user_id": self.user_id,
            "bucket_name": self.bucket_name,
            "project": self.project,
            "region": self.region,
            "remote_name": self.remote_name,
            "remote_root": self.remote_root,
            "rclone_bin": self.rclone_bin,
            "env_keys": redacted_keys,
            "rclone_version": version.stdout.strip() if version.returncode == 0 else "",
        }


def _load_service_account_json(credentials_path: str) -> str:
    path = Path(credentials_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"GOOGLE_APPLICATION_CREDENTIALS does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(payload, separators=(",", ":"))


def resolve_rclone_binary(explicit: str | None = None) -> str:
    candidates = [explicit, os.environ.get("RCLONE_BINARY"), shutil.which("rclone")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
        if candidate and candidate == "rclone":
            return candidate
    raise RuntimeError("rclone binary not found. Set RCLONE_BINARY or install rclone.")


def build_user_rclone_remote(
    user_id: str,
    *,
    object_prefix: str = "",
    remote_name: str = "mica_gcs",
    rclone_bin: str | None = None,
    use_oauth2_token: bool | None = None,
) -> RcloneGCSRemote:
    seed_env_from_dotenv()
    storage = get_storage_manager()
    bucket = storage.ensure_bucket(user_id)
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not configured")

    # W5-1: Default to OAuth2 tokens in production; env override available.
    if use_oauth2_token is None:
        use_oauth2_token = os.environ.get(
            "MICA_RCLONE_USE_OAUTH2", "1"
        ).strip().lower() in ("1", "true", "yes")

    return RcloneGCSRemote(
        user_id=user_id,
        bucket_name=bucket.bucket_name,
        project=storage.project,
        region=storage.region,
        remote_name=remote_name,
        object_prefix=object_prefix,
        rclone_bin=resolve_rclone_binary(rclone_bin),
        service_account_credentials_json=_load_service_account_json(creds_path),
        use_oauth2_token=use_oauth2_token,
    )


def build_orchestrator_storage_options(
    user_id: str,
    *,
    remote_name: str = "mica_gcs",
    object_prefix: str = "md-jobs",
    rclone_bin: str | None = None,
) -> Dict[str, Any]:
    remote = build_user_rclone_remote(
        user_id,
        object_prefix="",
        remote_name=remote_name,
        rclone_bin=rclone_bin,
    )
    return {
        "storage_backend": "rclone",
        "storage_remote": f"{remote.remote_name}:{remote.bucket_name}",
        "storage_remote_prefix": object_prefix,
        "storage_env": remote.to_env(),
    }


def run_local_roundtrip_proof(
    user_id: str,
    *,
    object_prefix: str = "poc/local-roundtrip",
    remote_name: str = "mica_gcs",
    rclone_bin: str | None = None,
) -> Dict[str, Any]:
    remote = build_user_rclone_remote(
        user_id,
        object_prefix=object_prefix,
        remote_name=remote_name,
        rclone_bin=rclone_bin,
    )
    with tempfile.TemporaryDirectory(prefix="mica_rclone_poc_") as tmpdir:
        tmp = Path(tmpdir)
        source = tmp / "sample.txt"
        source.write_text("mica-rclone-proof\n", encoding="utf-8")
        remote_rel = "sample.txt"
        remote.copyto(source, remote_rel)
        listing = remote.lsf()
        restored = tmp / "restored.txt"
        remote.copy_from_remote(remote_rel, restored)
        remote.check(source, remote_rel)
        return {
            **remote.audit_snapshot(),
            "listing": listing.strip().splitlines(),
            "source_text": source.read_text(encoding="utf-8"),
            "restored_text": restored.read_text(encoding="utf-8"),
            "proof_remote_path": remote.remote_path(remote_rel),
        }
