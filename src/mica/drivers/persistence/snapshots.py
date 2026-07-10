"""
Persistence — Session Snapshots
================================

Named point-in-time copies of session artefacts (conversation log,
saga log) with SHA-256 integrity manifests and optional restore.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

from ..utils import _redact_obj


# ────────────────────────────────────────────────────────────────────
# Path / hash helpers
# ────────────────────────────────────────────────────────────────────

def snapshot_dir(
    checkpoint_dir: str,
    session_id: str,
    snapshots_dirname: Optional[str] = None,
) -> Path:
    """Return (and create) the snapshot directory for *session_id*."""
    base = Path(checkpoint_dir).resolve()
    root = base / (snapshots_dirname or "snapshots")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "unknown")
    out_dir = root / safe
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def sha256_file(path: Path) -> str:
    """Return hex SHA-256 of *path*, or empty string on any error."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


# ────────────────────────────────────────────────────────────────────
# Save / restore
# ────────────────────────────────────────────────────────────────────

async def save_session_snapshot(
    *,
    snapshots_enabled: bool,
    checkpoint_dir: str,
    snapshots_dirname: Optional[str],
    session_id: str,
    label: str,
    overwrite: bool = False,
    conversation_log_path_fn: Callable[[str], Path],
    saga_log_path_fn: Callable[[str], Path],
    append_saga_event_fn: Callable[..., Coroutine],
) -> Path:
    """Save a named snapshot of session artefacts (conversation + saga log).

    Bounded-overwrite: refuses to overwrite unless *overwrite* is ``True``.
    """
    if not snapshots_enabled:
        raise ValueError("Snapshots are disabled")

    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label or "snapshot")
    snap_dir = snapshot_dir(checkpoint_dir, session_id, snapshots_dirname) / safe_label
    if snap_dir.exists() and not overwrite:
        raise ValueError(f"Snapshot already exists: {safe_label}")
    snap_dir.mkdir(parents=True, exist_ok=True)

    # ---------- conversation log ----------
    src = conversation_log_path_fn(session_id)
    if not src.exists():
        raise ValueError("No conversation log found to snapshot")

    dst = snap_dir / "conversation_log.json"
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    tmp.replace(dst)

    # ---------- saga log (append-only audit trail) ----------
    saga_src = saga_log_path_fn(session_id)
    saga_dst: Optional[Path] = None
    if saga_src.exists():
        saga_dst = snap_dir / "saga_log.jsonl"
        tmp_saga = saga_dst.with_suffix(saga_dst.suffix + ".tmp")
        tmp_saga.write_text(saga_src.read_text(encoding="utf-8"), encoding="utf-8")
        tmp_saga.replace(saga_dst)

    manifest = {
        "version": 1,
        "session_id": session_id,
        "label": safe_label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": {
            "conversation_log": {
                "path": str(dst),
                "sha256": sha256_file(dst),
                "size_bytes": int(dst.stat().st_size) if dst.exists() else 0,
            },
            "saga_log": (
                {
                    "path": str(saga_dst),
                    "sha256": sha256_file(saga_dst) if saga_dst is not None else "",
                    "size_bytes": (
                        int(saga_dst.stat().st_size)
                        if saga_dst is not None and saga_dst.exists()
                        else 0
                    ),
                }
                if saga_dst is not None
                else None
            ),
        },
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(_redact_obj(manifest), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    await append_saga_event_fn(
        session_id=session_id,
        event={
            "event_id": str(uuid.uuid4()),
            "type": "snapshot_saved",
            "label": safe_label,
            "path": str(dst),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    return dst


async def restore_session_snapshot(
    *,
    snapshots_enabled: bool,
    checkpoint_dir: str,
    snapshots_dirname: Optional[str],
    session_id: str,
    label: str,
    conversation_log_path_fn: Callable[[str], Path],
    append_saga_event_fn: Callable[..., Coroutine],
) -> Path:
    """Restore conversation log from a named snapshot.

    Security: saga logs are NOT restored — they are append-only evidence.
    """
    if not snapshots_enabled:
        raise ValueError("Snapshots are disabled")

    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label or "snapshot")
    snap_dir = snapshot_dir(checkpoint_dir, session_id, snapshots_dirname) / safe_label
    src = snap_dir / "conversation_log.json"
    if not src.exists():
        raise ValueError(f"Snapshot not found: {safe_label}")

    dst = conversation_log_path_fn(session_id)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    tmp.replace(dst)

    await append_saga_event_fn(
        session_id=session_id,
        event={
            "event_id": str(uuid.uuid4()),
            "type": "snapshot_restored",
            "label": safe_label,
            "path": str(src),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    return dst
