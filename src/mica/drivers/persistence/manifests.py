"""
Persistence — Run Manifests & Report Cards
============================================

Provenance files tracking run IDs, git state, software versions,
execution artifacts, and lightweight evaluation report cards.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..utils import _truncate_text, _redact_text, _redact_obj


# ────────────────────────────────────────────────────────────────────
# Path helpers
# ────────────────────────────────────────────────────────────────────

def run_manifest_dir(
    checkpoint_dir: str,
    session_id: str,
    run_manifest_dirname: Optional[str] = None,
) -> Path:
    """Return (and create) the run-manifest directory for *session_id*."""
    base = Path(checkpoint_dir).resolve()
    root = base / (run_manifest_dirname or "run_manifests")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "unknown")
    out_dir = root / safe
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


# ────────────────────────────────────────────────────────────────────
# Environment introspection (best-effort)
# ────────────────────────────────────────────────────────────────────

def best_effort_git_info() -> Dict[str, Any]:
    """Collect git commit / dirty status if repo metadata is available."""

    def _find_git_root(start: Path) -> Optional[Path]:
        cur = start
        for _ in range(10):
            if (cur / ".git").exists():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
        return None

    try:
        git_root = _find_git_root(Path(__file__).resolve())
        if git_root is None:
            return {"available": False}

        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(git_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(git_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
        ).strip()
        return {
            "available": True,
            "root": str(git_root),
            "head": head,
            "dirty": bool(dirty),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def best_effort_versions() -> Dict[str, Any]:
    """Gather Python / platform / key-package version info."""
    import platform
    import sys

    versions: Dict[str, Any] = {
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "packages": {},
    }

    try:
        from importlib import metadata

        for name in [
            "pytest",
            "pydantic",
            "numpy",
            "torch",
            "langgraph",
            "fastapi",
        ]:
            try:
                versions["packages"][name] = metadata.version(name)
            except Exception:
                versions["packages"][name] = None
    except Exception:
        pass

    return versions


# ────────────────────────────────────────────────────────────────────
# Run manifest
# ────────────────────────────────────────────────────────────────────

def write_run_manifest(
    *,
    checkpoint_dir: str,
    run_manifest_dirname: Optional[str],
    run_manifest_enabled: bool,
    session_id: str,
    mode: str,
    started_at: datetime,
    finished_at: datetime,
    result: Optional[Dict[str, Any]],
    error: Optional[str],
    session_run_ids: Dict[str, str],
    conversation_log_path_fn: Callable[[str], Path],
    saga_log_path_fn: Callable[[str], Path],
    sha256_file_fn: Callable[[Path], str],
    mcp_config_path: str,
    timescale_appender: Optional[Callable] = None,
) -> Optional[Path]:
    """Write a minimal provenance manifest for this run.

    Writes two files under ``checkpoint_dir/run_manifests/<session_id>/``:
    - ``run_manifest.json`` (latest)
    - ``run_manifest.<timestamp>.<run_id>.json`` (historical)
    """
    if not run_manifest_enabled:
        return None

    run_id = session_run_ids.get(session_id) or str(uuid.uuid4())
    session_run_ids[session_id] = run_id
    out_dir = run_manifest_dir(checkpoint_dir, session_id, run_manifest_dirname)

    # Artifacts to hash (best effort; existence is optional).
    conv_path = conversation_log_path_fn(session_id)
    saga_path = saga_log_path_fn(session_id)

    driver_path = Path(__file__).resolve()

    mcp_cfg_path = Path(mcp_config_path or "")
    mcp_cfg_info: Optional[Dict[str, Any]] = None
    try:
        if mcp_cfg_path and mcp_cfg_path.exists():
            mcp_cfg_info = {
                "path": str(mcp_cfg_path.resolve()),
                "sha256": sha256_file_fn(mcp_cfg_path),
                "size_bytes": int(mcp_cfg_path.stat().st_size),
            }
    except Exception:
        mcp_cfg_info = None

    try:
        from dataclasses import asdict as _asdict
        # We need config dict — caller should pass it.
        config_dict: Dict[str, Any] = {}
    except Exception:
        config_dict = {}

    manifest: Dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "session_id": session_id,
        "mode": mode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "status": "error" if error else "success",
        "error": _truncate_text(_redact_text(error), max_len=2000) if error else None,
        "git": best_effort_git_info(),
        "versions": best_effort_versions(),
        "config": _redact_obj(config_dict),
        "inputs": {
            "mcp_config": mcp_cfg_info,
        },
        "code": {
            "agentic_driver": {
                "path": str(driver_path),
                "sha256": sha256_file_fn(driver_path),
            }
        },
        "artifacts": {
            "conversation_log": {
                "path": str(conv_path),
                "exists": conv_path.exists(),
                "sha256": sha256_file_fn(conv_path) if conv_path.exists() else "",
                "size_bytes": int(conv_path.stat().st_size) if conv_path.exists() else 0,
            },
            "saga_log": {
                "path": str(saga_path),
                "exists": saga_path.exists(),
                "sha256": sha256_file_fn(saga_path) if saga_path.exists() else "",
                "size_bytes": int(saga_path.stat().st_size) if saga_path.exists() else 0,
            },
        },
        "result": {
            "keys": sorted(list(result.keys())) if isinstance(result, dict) else [],
            "provider_contract": _redact_obj(((result or {}).get("provider_contract") or (result or {}).get("config", {}).get("provider_contract") or {}) if isinstance(result, dict) else {}),
        },
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    historical = out_dir / f"run_manifest.{ts}.{run_id[:8]}.json"
    latest = out_dir / "run_manifest.json"

    payload = json.dumps(_redact_obj(manifest), ensure_ascii=False, indent=2, default=str)

    tmp_hist = historical.with_suffix(historical.suffix + ".tmp")
    tmp_hist.write_text(payload, encoding="utf-8")
    tmp_hist.replace(historical)

    tmp_latest = latest.with_suffix(latest.suffix + ".tmp")
    tmp_latest.write_text(payload, encoding="utf-8")
    tmp_latest.replace(latest)

    if timescale_appender is not None:
        try:
            import asyncio as _asyncio

            _asyncio.create_task(
                timescale_appender(
                    session_id=session_id,
                    event={
                        "type": "run_manifest",
                        "status": manifest.get("status"),
                        "run_id": run_id,
                        "mode": mode,
                        "duration_ms": manifest.get("duration_ms"),
                        "artifacts": {
                            "conversation_log_exists": manifest.get("artifacts", {})
                                .get("conversation_log", {})
                                .get("exists"),
                            "saga_log_exists": manifest.get("artifacts", {})
                                .get("saga_log", {})
                                .get("exists"),
                        },
                    },
                )
            )
        except Exception:
            pass

    return latest


# ────────────────────────────────────────────────────────────────────
# Report card
# ────────────────────────────────────────────────────────────────────

def write_report_card(
    *,
    checkpoint_dir: str,
    run_manifest_dirname: Optional[str],
    report_card_enabled: bool,
    session_id: str,
    mode: str,
    started_at: datetime,
    finished_at: datetime,
    result: Optional[Dict[str, Any]],
    error: Optional[str],
    conversation_log_path_fn: Callable[[str], Path],
    saga_log_path_fn: Callable[[str], Path],
    sha256_file_fn: Callable[[Path], str],
    saga_mcp_metrics_fn: Callable[[str], Dict[str, Any]],
    mcp_enabled: bool,
) -> Optional[Path]:
    """Write a per-run evaluation report card.

    Files under ``checkpoint_dir/run_manifests/<session_id>/``:
    - ``report_card.json`` (latest)
    - ``report_card.<timestamp>.<run_id>.json`` (historical)
    """
    if not report_card_enabled:
        return None

    run_id = str(uuid.uuid4())
    out_dir = run_manifest_dir(checkpoint_dir, session_id, run_manifest_dirname)

    conv_path = conversation_log_path_fn(session_id)
    saga_path = saga_log_path_fn(session_id)
    run_manifest_path = out_dir / "run_manifest.json"

    def _safe_len(v: Any) -> int:
        try:
            return len(v)  # type: ignore[arg-type]
        except Exception:
            return 0

    lab_reports_count = _safe_len((result or {}).get("lab_reports", {}) if isinstance(result, dict) else {})
    quality_scores_count = _safe_len((result or {}).get("quality_scores", {}) if isinstance(result, dict) else {})
    peer_feedback = (result or {}).get("peer_feedback", {}) if isinstance(result, dict) else {}
    peer_feedback_count = 0
    try:
        if isinstance(peer_feedback, dict):
            peer_feedback_count = sum(_safe_len(v) for v in peer_feedback.values())
    except Exception:
        peer_feedback_count = 0

    saga_mcp = saga_mcp_metrics_fn(session_id)

    report: Dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "session_id": session_id,
        "mode": mode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "status": "error" if error else "success",
        "error": _truncate_text(_redact_text(error), max_len=2000) if error else None,
        "summary": {
            "has_final_result": bool((result or {}).get("final_result")) if isinstance(result, dict) else False,
            "lab_reports_count": lab_reports_count,
            "quality_scores_count": quality_scores_count,
            "peer_feedback_count": peer_feedback_count,
            "result_keys": sorted(list(result.keys())) if isinstance(result, dict) else [],
            "provider_contract": _redact_obj(((result or {}).get("provider_contract") or (result or {}).get("config", {}).get("provider_contract") or {}) if isinstance(result, dict) else {}),
        },
        "mcp": {
            "enabled": bool(mcp_enabled),
            "saga_metrics": saga_mcp,
        },
        "artifacts": {
            "conversation_log": {
                "path": str(conv_path),
                "exists": conv_path.exists(),
            },
            "saga_log": {
                "path": str(saga_path),
                "exists": saga_path.exists(),
            },
            "run_manifest": {
                "path": str(run_manifest_path),
                "exists": run_manifest_path.exists(),
                "sha256": sha256_file_fn(run_manifest_path) if run_manifest_path.exists() else "",
            },
        },
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    historical = out_dir / f"report_card.{ts}.{run_id[:8]}.json"
    latest = out_dir / "report_card.json"

    payload = json.dumps(_redact_obj(report), ensure_ascii=False, indent=2, default=str)

    tmp_hist = historical.with_suffix(historical.suffix + ".tmp")
    tmp_hist.write_text(payload, encoding="utf-8")
    tmp_hist.replace(historical)

    tmp_latest = latest.with_suffix(latest.suffix + ".tmp")
    tmp_latest.write_text(payload, encoding="utf-8")
    tmp_latest.replace(latest)

    return latest
