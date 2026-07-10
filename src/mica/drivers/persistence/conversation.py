"""
Persistence — Conversation Log
================================

Bounded JSON conversation log with per-entry redaction, optional
Timescale mirroring, and safe serialisation of arbitrarily-typed results.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from ..utils import _truncate_text, _redact_text, _redact_obj


# ────────────────────────────────────────────────────────────────────
# Path helper
# ────────────────────────────────────────────────────────────────────

def conversation_log_path(
    checkpoint_dir: str,
    session_id: str,
    conversation_log_dirname: Optional[str] = None,
) -> Path:
    """Return (and create) the conversation-log file path for *session_id*."""
    base = Path(checkpoint_dir).resolve()
    out_dir = base / (conversation_log_dirname or "conversation_logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "unknown")
    return out_dir / f"{safe}.json"


# ────────────────────────────────────────────────────────────────────
# Serialisation helpers
# ────────────────────────────────────────────────────────────────────

def safe_result_for_log(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract high-signal, JSON-safe fields from a run result dict.

    Does NOT include raw LabReport / QualityScore objects.
    """
    if not isinstance(result, dict):
        return {}
    payload: Dict[str, Any] = {}

    # ---- final_result ----
    final_result = result.get("final_result")
    if isinstance(final_result, str):
        payload["final_result"] = _truncate_text(_redact_text(final_result), max_len=2000)
    elif isinstance(final_result, (int, float, bool)) or final_result is None:
        payload["final_result"] = final_result
    elif isinstance(final_result, dict):
        payload["final_result"] = _redact_obj(final_result)

    # ---- lab_reports ----
    lab_reports = result.get("lab_reports")
    if isinstance(lab_reports, (dict, list)):
        payload["lab_reports_count"] = len(lab_reports)

    # ---- quality_scores ----
    quality_scores = result.get("quality_scores")
    if isinstance(quality_scores, dict):
        payload["quality_scores_count"] = len(quality_scores)
        scores: List[float] = []
        for v in quality_scores.values():
            overall = getattr(v, "overall_score", None)
            if isinstance(overall, (int, float)):
                scores.append(float(overall))
        if scores:
            payload["avg_quality_overall"] = sum(scores) / len(scores)

    # ---- mqa_results ----
    mqa_results = result.get("mqa_results")
    if isinstance(mqa_results, dict):
        payload["mqa_results"] = _redact_obj(mqa_results)

    return _redact_obj(payload)


def stringify_message_content(content: Any) -> Optional[str]:
    """Convert arbitrary message content to a string (or ``None``)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


# ────────────────────────────────────────────────────────────────────
# Main append
# ────────────────────────────────────────────────────────────────────

async def append_conversation_log(
    *,
    conversation_log_enabled: bool,
    checkpoint_dir: str,
    conversation_log_dirname: Optional[str],
    conversation_log_max_entries: int,
    conversation_log_lock,  # asyncio.Lock
    session_id: str,
    user_query: str,
    mode: str,
    result: Optional[Dict[str, Any]],
    started_at: datetime,
    finished_at: datetime,
    error: Optional[str],
    timescale_appender: Optional[Callable[..., Coroutine]] = None,
    session_run_ids: Optional[Dict[str, str]] = None,
) -> None:
    """Append a turn to the bounded conversation log and optionally mirror to Timescale."""
    if not conversation_log_enabled:
        return

    path = conversation_log_path(checkpoint_dir, session_id, conversation_log_dirname)
    duration_s = (finished_at - started_at).total_seconds()

    safe_user_query = _truncate_text(_redact_text(user_query), max_len=2000)
    assistant_content: Any = None
    if isinstance(result, dict):
        assistant_content = result.get("final_result")
    if assistant_content is not None:
        assistant_content = _truncate_text(
            _redact_text(stringify_message_content(assistant_content) or ""),
            max_len=2000,
        )

    entry = {
        "timestamp": finished_at.isoformat(),
        "session_id": session_id,
        "mode": mode,
        "duration_s": duration_s,
        "user": {
            "role": "user",
            "content": safe_user_query,
        },
        "assistant": {
            "role": "assistant",
            "content": assistant_content,
            "summary": safe_result_for_log(result),
            "error": (
                _truncate_text(_redact_text(error or ""), max_len=1000)
                if error
                else None
            ),
        },
    }

    async with conversation_log_lock:
        data: List[Dict[str, Any]] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    data = existing
            except Exception:
                # Corrupted — start fresh (do not crash workflows).
                data = []
        data.append(entry)
        max_entries = int(conversation_log_max_entries or 250)
        if max_entries > 0 and len(data) > max_entries:
            data = data[-max_entries:]
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # ---- Best-effort Timescale mirror ----
    if timescale_appender is not None:
        try:
            await timescale_appender(
                session_id=session_id,
                event={
                    "type": "conversation_summary",
                    "status": "success" if not error else "error",
                    "duration_s": duration_s,
                    "run_id": (session_run_ids or {}).get(session_id),
                    "mode": mode,
                    "assistant_preview": assistant_content,
                },
            )
        except Exception:
            pass
