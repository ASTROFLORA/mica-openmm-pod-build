"""
ATOM Memory Integration (Phase 2)
===================================

Standalone async helpers for recording experiences, session events,
lab reports, quality scores, and gap-scan signals into the ATOM
temporal knowledge-graph memory system.

All functions receive the ``ATOMMemorySystem`` (or ``None``) and any
other dependencies as explicit parameters — no ``AgenticDriver``
instance required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:
    from ..memory.atom.models import TemporalQuintuple
else:  # pragma: no cover
    TemporalQuintuple = Any  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Core: store a single experience entry
# ────────────────────────────────────────────────────────────────────

async def record_atom_entry(
    atom_memory: Any,
    text: str,
    observation_time: Optional[datetime] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> None:
    """Store a single experience in *atom_memory* (best-effort)."""
    if not atom_memory:
        return
    try:
        await atom_memory.store_experience(
            experience=text,
            observation_time=observation_time,
            metadata=metadata or {},
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("ATOM memory store failed: %s", exc)


# ────────────────────────────────────────────────────────────────────
# Session events
# ────────────────────────────────────────────────────────────────────

async def record_session_event_in_atom(
    atom_memory: Any,
    session_id: str,
    state: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a session-state transition in ATOM memory."""
    if not atom_memory:
        return
    metadata: Dict[str, Any] = {
        "event": "session_state",
        "state": state,
        "session_id": session_id,
    }
    if payload:
        for key, value in payload.items():
            metadata[str(key)] = str(value)
    text = (
        f"Session[{session_id}] transitioned to {state} "
        f"payload={payload or {}}"
    )
    await record_atom_entry(atom_memory, text=text, metadata={k: str(v) for k, v in metadata.items()})


# ────────────────────────────────────────────────────────────────────
# Lab reports
# ────────────────────────────────────────────────────────────────────

async def record_lab_report_to_atom(
    atom_memory: Any,
    subtask_id: str,
    lab_report: Any,
    report_to_text_fn: Any,
) -> None:
    """Persist a lab-report summary into ATOM memory.

    Parameters
    ----------
    atom_memory:
        ``ATOMMemorySystem`` instance or ``None``.
    subtask_id:
        Identifier of the subtask that produced the report.
    lab_report:
        ``LabReport`` instance (or similar).
    report_to_text_fn:
        Callable that converts a lab report to plain text
        (typically ``AgenticDriver._report_to_text``).
    """
    if not atom_memory:
        return

    created_at = getattr(lab_report, "created_at", None)
    agent_persona = getattr(lab_report, "agent_persona", None)
    agent_name = (
        getattr(agent_persona, "name", None) or str(agent_persona)
        if agent_persona is not None
        else "unknown"
    )
    methodology = getattr(lab_report, "methodology", None)

    confidence = (
        getattr(lab_report, "confidence_level", None)
        if getattr(lab_report, "confidence_level", None) is not None
        else getattr(lab_report, "confidence", None)
    )
    findings = getattr(lab_report, "findings", None) or ""
    if not findings:
        findings = report_to_text_fn(lab_report)

    confidence_str = "unknown"
    if isinstance(confidence, (int, float)):
        confidence_str = f"{float(confidence):.2f}"

    text = f"LabReport[{subtask_id}] confidence={confidence_str}\n{findings}"

    metadata: Dict[str, str] = {
        "subtask_id": subtask_id,
        "agent": str(agent_name),
    }
    if methodology is not None:
        metadata["methodology"] = str(methodology)

    await record_atom_entry(
        atom_memory,
        text=text,
        observation_time=created_at,
        metadata=metadata,
    )


# ────────────────────────────────────────────────────────────────────
# Quality scores
# ────────────────────────────────────────────────────────────────────

async def record_quality_scores_to_atom(
    atom_memory: Any,
    quality_scores: Dict[str, Any],
) -> None:
    """Record per-subtask quality scores in ATOM memory."""
    if not atom_memory or not quality_scores:
        return
    for subtask_id, quality in quality_scores.items():
        overall = getattr(quality, "overall_score", None)
        try:
            overall_f = float(overall) if overall is not None else None
        except Exception:
            overall_f = None

        # ``QualityScore`` differs across implementations; some do not expose ``.metrics``.
        metrics = None
        for attr in ("metrics", "criteria_scores", "dimension_scores", "scores", "details"):
            if hasattr(quality, attr):
                metrics = getattr(quality, attr)
                break
        if metrics is None and isinstance(quality, dict):
            metrics = quality.get("metrics") or quality

        metadata = {
            "subtask_id": subtask_id,
            "metric": "quality_score",
            "overall_score": f"{overall_f:.4f}" if overall_f is not None else "unknown",
        }
        text = (
            f"QualityScore[{subtask_id}] overall={overall_f:.2f} metrics={metrics}"
            if overall_f is not None
            else f"QualityScore[{subtask_id}] overall=unknown metrics={metrics}"
        )
        await record_atom_entry(atom_memory, text=text, metadata=metadata)


# ────────────────────────────────────────────────────────────────────
# Proactive gap scanning
# ────────────────────────────────────────────────────────────────────

async def query_atom_for_gap_signals(
    atom_memory: Any,
    recent_facts: Sequence[TemporalQuintuple],
    quality_threshold: float,
) -> List[Dict[str, Any]]:
    """Analyse recent temporal facts and return gap-detection signals."""
    signals: List[Dict[str, Any]] = []
    if not atom_memory:
        return signals

    if not recent_facts:
        signals.append({
            "type": "no_recent_facts",
            "message": "No temporal facts available for the configured lookback window",
        })
        return signals

    summary = await atom_memory.summarize()
    last_update = summary.get("last_update")
    if last_update:
        try:
            last_update_dt = datetime.fromisoformat(last_update)
        except (TypeError, ValueError):
            last_update_dt = None
        if last_update_dt and datetime.now(timezone.utc) - last_update_dt > timedelta(days=7):
            signals.append({
                "type": "memory_stale",
                "message": "ATOM memory has not been updated in more than 7 days",
                "last_update": last_update,
            })

    low_quality_facts = [
        fact
        for fact in recent_facts
        if fact.metadata.get("metric") == "quality_score"
        and float(fact.metadata.get("overall_score", 0.0)) < quality_threshold
    ]
    if low_quality_facts:
        lowest = min(
            float(fact.metadata.get("overall_score", 0.0))
            for fact in low_quality_facts
        )
        signals.append({
            "type": "quality_regression",
            "message": "Recent tasks failed Nature quality threshold",
            "count": len(low_quality_facts),
            "lowest_score": round(lowest, 4),
        })

    if summary.get("quintuples", 0) < 5:
        signals.append({
            "type": "insufficient_memory",
            "message": "ATOM quintuples below minimum operating threshold",
            "quintuples": summary.get("quintuples", 0),
        })

    return signals


async def maybe_run_proactive_gap_scan(
    atom_memory: Any,
    proactive_gap_detection: bool,
    quality_threshold: float,
    session_logs: List[Dict[str, Any]],
    record_session_event_fn: Any,
) -> None:
    """Execute a proactive gap scan if enabled, appending signals to *session_logs*.

    Parameters
    ----------
    atom_memory:
        ``ATOMMemorySystem`` instance (or ``None``).
    proactive_gap_detection:
        Whether the config flag is enabled.
    quality_threshold:
        Nature quality threshold for regression detection.
    session_logs:
        Mutable list to which gap-scan log entries are appended.
    record_session_event_fn:
        Async callable ``(state, payload) -> None`` to relay signals to ATOM.
    """
    if not (proactive_gap_detection and atom_memory):
        return
    lookback_start = datetime.now(timezone.utc) - timedelta(days=90)
    lookback_end = datetime.now(timezone.utc)
    recent_facts = await atom_memory.query_temporal_facts(
        time_range=(lookback_start, lookback_end)
    )
    gap_signals = await query_atom_for_gap_signals(atom_memory, recent_facts, quality_threshold)
    for signal in gap_signals:
        detail = signal.get("message") if isinstance(signal, dict) else str(signal)
        session_logs.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "state": "proactive_gap",
            "detail": detail,
        })
        await record_session_event_fn(
            state="PROACTIVE",
            payload=signal,
        )
