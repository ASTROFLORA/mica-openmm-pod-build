"""
Persistence — Saga Event Logs
===============================

Append-only saga log (JSONL) with optional Timescale/Neon mirroring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..utils import _redact_obj, _current_user_id_var, _current_bucket_var

# Lazy: heavy infra deps may not be installed
try:
    from ...infrastructure.persistence import TimescaleEventStore
    from ...infrastructure.event_store import SagaEvent
except Exception:  # pragma: no cover
    TimescaleEventStore = None  # type: ignore
    SagaEvent = None  # type: ignore

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Path helpers
# ────────────────────────────────────────────────────────────────────

def saga_log_path(
    checkpoint_dir: str,
    session_id: str,
    saga_log_dirname: Optional[str] = None,
) -> Path:
    """Return the JSONL saga-log path for *session_id*."""
    base = Path(checkpoint_dir).resolve()
    out_dir = base / (saga_log_dirname or "saga_logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "unknown")
    return out_dir / f"{safe}.jsonl"


# ────────────────────────────────────────────────────────────────────
# Core append
# ────────────────────────────────────────────────────────────────────

async def append_saga_event(
    *,
    checkpoint_dir: str,
    saga_log_dirname: Optional[str],
    saga_log_enabled: bool,
    saga_log_max_bytes: int,
    saga_log_lock: asyncio.Lock,
    timescale_appender: Callable,
    session_id: str,
    event: Dict[str, Any],
) -> None:
    """Append *event* to the saga JSONL file and mirror to Timescale.

    Parameters
    ----------
    checkpoint_dir / saga_log_dirname / saga_log_enabled / saga_log_max_bytes:
        Config values from ``AgenticDriverConfig``.
    saga_log_lock:
        ``asyncio.Lock`` for safe concurrent writes.
    timescale_appender:
        Async callable ``(session_id, event) -> None`` (typically the
        ``append_saga_event_timescale`` function partially bound).
    """
    if not saga_log_enabled:
        return
    if not isinstance(event, dict):
        return

    path = saga_log_path(checkpoint_dir, session_id, saga_log_dirname)
    max_bytes = saga_log_max_bytes if saga_log_max_bytes > 0 else 5_000_000

    safe_event = _redact_obj(event)

    async with saga_log_lock:
        try:
            if path.exists() and path.stat().st_size > max_bytes:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                rotated = path.with_name(f"{path.stem}.{ts}{path.suffix}")
                try:
                    path.replace(rotated)
                except Exception:
                    pass

            line = json.dumps(safe_event, ensure_ascii=False, default=str)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        except Exception:
            return

    await timescale_appender(session_id=session_id, event=safe_event)


# ────────────────────────────────────────────────────────────────────
# Timescale mirror
# ────────────────────────────────────────────────────────────────────

async def append_saga_event_timescale(
    *,
    session_id: str,
    event: Dict[str, Any],
    timescale_store: Any,
    session_run_ids: Dict[str, str],
) -> None:
    """Mirror a saga event to the Timescale/Neon event store."""
    if timescale_store is None:
        return

    if SagaEvent is None:  # pragma: no cover
        return

    try:
        user_id = _current_user_id_var.get(None)
        bucket = _current_bucket_var.get(None)
        run_id = session_run_ids.get(session_id)

        saga_event = SagaEvent(
            saga_session_id=session_id,
            run_id=run_id or (str(event.get("run_id")) if event.get("run_id") else None),
            stage=str(event.get("type") or event.get("node") or "unknown"),
            status=str(event.get("status") or event.get("state") or ""),
            payload=event,
            user_id=user_id,
            session_id=session_id,
            bucket=bucket,
        )

        await timescale_store.append(saga_event)
    except Exception:
        return


# ────────────────────────────────────────────────────────────────────
# Lazy Timescale store init
# ────────────────────────────────────────────────────────────────────

async def get_timescale_store(
    current_store: Any,
    failed_flag: bool,
) -> tuple:
    """Lazily initialise the Timescale event store.

    Returns ``(store_or_None, new_failed_flag)`` so the caller can
    update its own state.
    """
    if failed_flag:
        return None, True
    if current_store is not None:
        return current_store, False

    if TimescaleEventStore is None:
        return None, True

    try:
        store = TimescaleEventStore()
        await store.initialize()
        return store, False
    except Exception as exc:
        logger.warning("TimescaleEventStore unavailable; saga events will stay local: %s", exc)
        return None, True


# ────────────────────────────────────────────────────────────────────
# MCP metrics summary from saga JSONL
# ────────────────────────────────────────────────────────────────────

def best_effort_saga_mcp_metrics(
    checkpoint_dir: str,
    session_id: str,
    saga_log_dirname: Optional[str] = None,
) -> Dict[str, Any]:
    """Summarise MCP execution outcomes by scanning the saga log (best-effort)."""
    path = saga_log_path(checkpoint_dir, session_id, saga_log_dirname)
    metrics: Dict[str, Any] = {
        "available": False,
        "path": str(path),
        "counts": {
            "mcp_tool_begin": 0,
            "mcp_tool_retry": 0,
            "mcp_tool_commit": 0,
            "mcp_tool_abort": 0,
            "blocked": 0,
            "timeouts": 0,
        },
        "last_event": None,
    }

    if not path.exists():
        return metrics

    try:
        max_bytes = 2_000_000
        raw: bytes
        with path.open("rb") as f:
            try:
                f.seek(0, 2)
                size = f.tell()
                if size > max_bytes:
                    f.seek(-max_bytes, 2)
                else:
                    f.seek(0)
            except Exception:
                f.seek(0)
            raw = f.read()

        text = raw.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue

            t = evt.get("type")
            if t not in {"mcp_tool_begin", "mcp_tool_retry", "mcp_tool_commit", "mcp_tool_abort"}:
                continue

            metrics["available"] = True
            metrics["counts"][t] = int(metrics["counts"].get(t, 0)) + 1

            if t == "mcp_tool_abort":
                blocked = (
                    bool(evt.get("blocked"))
                    or (evt.get("blocked_by") is not None)
                    or (evt.get("security_risk") is not None)
                )
                if blocked:
                    metrics["counts"]["blocked"] = int(metrics["counts"].get("blocked", 0)) + 1
                if (evt.get("error_type") or "") == "timeout":
                    metrics["counts"]["timeouts"] = int(metrics["counts"].get("timeouts", 0)) + 1

            metrics["last_event"] = {
                "type": t,
                "ts": evt.get("ts"),
                "server": evt.get("server"),
                "tool": evt.get("tool"),
                "attempt": evt.get("attempt"),
                "error_type": evt.get("error_type"),
                "blocked_by": evt.get("blocked_by"),
            }

        return metrics
    except Exception as exc:
        metrics["available"] = False
        metrics["error"] = str(exc)
        return metrics
