"""
event_log.py — P1-03
Run-scoped append-only event log for MICA agentic workflows.

Concerns:
- Append-only events.jsonl per workflow run
- Persisted plan.json
- Crash-resume detection via crash_resume.json

Anti-rigidity: all I/O is best-effort; logging errors NEVER raise.
Stdlib only — no third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REDACT_PATTERN = re.compile(r"password|token|secret|api_key", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_dict(d: dict) -> dict:
    """Return a shallow copy of *d* with sensitive keys redacted."""
    out: dict = {}
    for k, v in d.items():
        if _REDACT_PATTERN.search(str(k)):
            out[k] = _REDACTED
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# EventLogEntry
# ---------------------------------------------------------------------------

@dataclass
class EventLogEntry:
    entry_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: dict
    timestamp: str
    driver_id: str = ""
    phase: str = ""

    def to_json_line(self) -> str:
        """Return a single compact JSON line (no trailing newline)."""
        return json.dumps(
            {
                "entry_id": self.entry_id,
                "run_id": self.run_id,
                "sequence": self.sequence,
                "event_type": self.event_type,
                "payload": self.payload,
                "timestamp": self.timestamp,
                "driver_id": self.driver_id,
                "phase": self.phase,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json_line(cls, line: str) -> "EventLogEntry":
        """Parse a single line; raises ValueError on malformed JSON."""
        line = line.strip()
        if not line:
            raise ValueError("Empty line")
        try:
            d = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON: {exc}") from exc
        try:
            return cls(
                entry_id=d["entry_id"],
                run_id=d["run_id"],
                sequence=d["sequence"],
                event_type=d["event_type"],
                payload=d.get("payload", {}),
                timestamp=d["timestamp"],
                driver_id=d.get("driver_id", ""),
                phase=d.get("phase", ""),
            )
        except KeyError as exc:
            raise ValueError(f"Missing field in EventLogEntry: {exc}") from exc


# ---------------------------------------------------------------------------
# RunPlan
# ---------------------------------------------------------------------------

@dataclass
class RunPlan:
    run_id: str
    query: str
    intent_tags: list
    planned_tools: list
    created_at: str
    workflow_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        """Pretty JSON."""
        return json.dumps(
            {
                "run_id": self.run_id,
                "query": self.query,
                "intent_tags": self.intent_tags,
                "planned_tools": self.planned_tools,
                "created_at": self.created_at,
                "workflow_id": self.workflow_id,
                "extra": self.extra,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "RunPlan":
        """Parse; raises ValueError on bad JSON."""
        try:
            d = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in RunPlan: {exc}") from exc
        try:
            return cls(
                run_id=d["run_id"],
                query=d["query"],
                intent_tags=d.get("intent_tags", []),
                planned_tools=d.get("planned_tools", []),
                created_at=d["created_at"],
                workflow_id=d.get("workflow_id", ""),
                extra=d.get("extra", {}),
            )
        except KeyError as exc:
            raise ValueError(f"Missing field in RunPlan: {exc}") from exc


# ---------------------------------------------------------------------------
# CrashResumeMarker
# ---------------------------------------------------------------------------

@dataclass
class CrashResumeMarker:
    run_id: str
    last_sequence: int
    last_phase: str
    last_driver_id: str
    checkpoint_at: str
    resumable: bool = True
    resume_from_sequence: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "last_sequence": self.last_sequence,
                "last_phase": self.last_phase,
                "last_driver_id": self.last_driver_id,
                "checkpoint_at": self.checkpoint_at,
                "resumable": self.resumable,
                "resume_from_sequence": self.resume_from_sequence,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "CrashResumeMarker":
        try:
            d = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in CrashResumeMarker: {exc}") from exc
        try:
            return cls(
                run_id=d["run_id"],
                last_sequence=d["last_sequence"],
                last_phase=d.get("last_phase", ""),
                last_driver_id=d.get("last_driver_id", ""),
                checkpoint_at=d["checkpoint_at"],
                resumable=d.get("resumable", True),
                resume_from_sequence=d.get("resume_from_sequence", 0),
            )
        except KeyError as exc:
            raise ValueError(f"Missing field in CrashResumeMarker: {exc}") from exc

    def is_fresh_run(self) -> bool:
        return self.resume_from_sequence == 0


# ---------------------------------------------------------------------------
# RunPauseState (S2.4)
# ---------------------------------------------------------------------------

@dataclass
class RunPauseState:
    """Serializable state snapshot taken when a run is paused.

    Written to ``pause_state.json`` alongside ``events.jsonl`` so that
    resume can restore the exact context.
    """

    run_id: str
    pause_reason: str
    paused_at: str
    last_sequence: int
    last_phase: str
    last_driver_id: str
    program_id: str = ""
    envelope_snapshot: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "pause_reason": self.pause_reason,
                "paused_at": self.paused_at,
                "last_sequence": self.last_sequence,
                "last_phase": self.last_phase,
                "last_driver_id": self.last_driver_id,
                "program_id": self.program_id,
                "envelope_snapshot": self.envelope_snapshot,
                "extra": self.extra,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "RunPauseState":
        try:
            d = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in RunPauseState: {exc}") from exc
        try:
            return cls(
                run_id=d["run_id"],
                pause_reason=d["pause_reason"],
                paused_at=d["paused_at"],
                last_sequence=d["last_sequence"],
                last_phase=d.get("last_phase", ""),
                last_driver_id=d.get("last_driver_id", ""),
                program_id=d.get("program_id", ""),
                envelope_snapshot=d.get("envelope_snapshot", {}),
                extra=d.get("extra", {}),
            )
        except KeyError as exc:
            raise ValueError(f"Missing field in RunPauseState: {exc}") from exc


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------

class EventLog:
    """
    Thread-safe, append-only event log for a single workflow run.

    All I/O is best-effort: errors are logged as WARNING but never raised.
    """

    def __init__(self, run_id: str, log_dir: Optional[str] = None) -> None:
        self.run_id = run_id
        if log_dir is None:
            log_dir = os.path.join(".mica_runs", run_id)
        self.log_dir = log_dir
        self._log_path = os.path.join(log_dir, "events.jsonl")
        self._plan_path = os.path.join(log_dir, "plan.json")
        self._marker_path = os.path.join(log_dir, "crash_resume.json")
        self._sequence: int = 0
        self._lock = threading.Lock()
        # S1.9: per-session write locks — each delegation_session_id gets
        # its own lock so concurrent sessions don't contend on the global lock
        # for file I/O.  The global _lock still guards sequence increment.
        self._session_locks: dict[str, threading.Lock] = {}

        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog: could not create log_dir %s: %s", log_dir, exc)

    # ------------------------------------------------------------------
    # Core append
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        payload: dict,
        driver_id: str = "",
        phase: str = "",
    ) -> EventLogEntry:
        """Append one event; returns the created entry. Never raises."""
        with self._lock:
            self._sequence += 1
            seq = self._sequence

        entry = EventLogEntry(
            entry_id=uuid.uuid4().hex,
            run_id=self.run_id,
            sequence=seq,
            event_type=event_type,
            payload=payload,
            timestamp=_utcnow_iso(),
            driver_id=driver_id,
            phase=phase,
        )

        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(entry.to_json_line() + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.append: write failed: %s", exc)

        self._update_crash_marker(entry)
        return entry

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def write_plan(self, plan: RunPlan) -> None:
        """Write plan.json atomically (tmp → rename). Best-effort."""
        tmp_path = self._plan_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(plan.to_json())
            os.replace(tmp_path, self._plan_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.write_plan: failed: %s", exc)
        self.append("plan_written", {"run_id": plan.run_id, "query": plan.query})

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_all(self) -> list:
        """Read events.jsonl; skip malformed lines."""
        entries: list = []
        try:
            with open(self._log_path, "r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        entries.append(EventLogEntry.from_json_line(line))
                    except ValueError as exc:
                        logger.warning(
                            "EventLog.read_all: skipping line %d: %s", lineno, exc
                        )
        except FileNotFoundError:
            pass  # fresh log — no entries yet
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.read_all: read failed: %s", exc)
        return sorted(entries, key=lambda e: e.sequence)

    # ------------------------------------------------------------------
    # Crash / resume
    # ------------------------------------------------------------------

    def get_crash_resume_marker(self) -> Optional[CrashResumeMarker]:
        """Return the marker or None if missing / malformed."""
        try:
            with open(self._marker_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            return CrashResumeMarker.from_json(text)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.get_crash_resume_marker: %s", exc)
            return None

    def detect_incomplete_run(self) -> bool:
        """
        True if crash_resume.json AND events.jsonl both exist AND
        there is no 'run_complete' event at the end of the log.
        """
        if not os.path.exists(self._marker_path):
            return False
        if not os.path.exists(self._log_path):
            return False
        entries = self.read_all()
        if not entries:
            return False
        return entries[-1].event_type != "run_complete"

    def mark_run_complete(self) -> None:
        """Append run_complete event and update marker with resumable=False."""
        entry = self.append("run_complete", {})
        try:
            marker = CrashResumeMarker(
                run_id=self.run_id,
                last_sequence=entry.sequence,
                last_phase=entry.phase,
                last_driver_id=entry.driver_id,
                checkpoint_at=entry.timestamp,
                resumable=False,
                resume_from_sequence=0,
            )
            with open(self._marker_path, "w", encoding="utf-8") as fh:
                fh.write(marker.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.mark_run_complete: marker write failed: %s", exc)

    def mark_run_started(self, query: str = "") -> None:
        """Append run_started event."""
        self.append("run_started", {"query": query})

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay_from(self, sequence: int) -> list:
        """Return all entries with sequence >= sequence."""
        return [e for e in self.read_all() if e.sequence >= sequence]

    # ------------------------------------------------------------------
    # S2.4: Pause / Resume per run
    # ------------------------------------------------------------------

    _PAUSE_STATE_FILE = "pause_state.json"

    def _pause_state_path(self) -> str:
        return os.path.join(self.log_dir, self._PAUSE_STATE_FILE)

    def pause_run(
        self,
        reason: str = "",
        *,
        driver_id: str = "",
        phase: str = "",
        program_id: str = "",
        envelope_snapshot: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> RunPauseState:
        """Pause the current run, persisting a resumable state snapshot.

        Writes a ``run_paused`` event to the log, a ``pause_state.json``
        file, and updates the crash-resume marker with ``resumable=True``.

        Calling ``pause_run`` when already paused raises ``RuntimeError``.
        """
        if self.is_paused:
            raise RuntimeError(
                f"Run {self.run_id} is already paused — "
                "call resume_run() before pausing again."
            )

        entry = self.append(
            "run_paused",
            {
                "reason": reason,
                "driver_id": driver_id,
                "phase": phase,
                "program_id": program_id,
            },
            driver_id=driver_id,
            phase=phase,
        )

        state = RunPauseState(
            run_id=self.run_id,
            pause_reason=reason,
            paused_at=entry.timestamp,
            last_sequence=entry.sequence,
            last_phase=phase,
            last_driver_id=driver_id,
            program_id=program_id,
            envelope_snapshot=envelope_snapshot or {},
            extra=extra or {},
        )

        # Persist pause state
        try:
            with open(self._pause_state_path(), "w", encoding="utf-8") as fh:
                fh.write(state.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.pause_run: state write failed: %s", exc)

        # Update crash marker to point at this pause event
        try:
            marker = CrashResumeMarker(
                run_id=self.run_id,
                last_sequence=entry.sequence,
                last_phase=phase,
                last_driver_id=driver_id,
                checkpoint_at=entry.timestamp,
                resumable=True,
                resume_from_sequence=entry.sequence,
            )
            with open(self._marker_path, "w", encoding="utf-8") as fh:
                fh.write(marker.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.pause_run: marker write failed: %s", exc)

        return state

    def resume_run(self) -> RunPauseState:
        """Resume a paused run, returning the previously-saved state.

        Appends a ``run_resumed`` event and deletes ``pause_state.json``.
        Raises ``RuntimeError`` if the run is not paused.
        """
        if not self.is_paused:
            raise RuntimeError(
                f"Run {self.run_id} is not paused — nothing to resume."
            )

        # Load saved state
        try:
            with open(self._pause_state_path(), "r", encoding="utf-8") as fh:
                state = RunPauseState.from_json(fh.read())
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Run {self.run_id} has a run_paused event but no "
                f"pause_state.json — state file may have been deleted."
            ) from exc

        # Append resumed event
        self.append(
            "run_resumed",
            {
                "resumed_from_sequence": state.last_sequence,
                "pause_reason": state.pause_reason,
            },
            driver_id=state.last_driver_id,
            phase=state.last_phase,
        )

        # Clean up the pause state file
        try:
            os.remove(self._pause_state_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.resume_run: cleanup failed: %s", exc)

        return state

    @property
    def is_paused(self) -> bool:
        """True when the last non-system event is ``run_paused``.

        Also returns True if ``pause_state.json`` exists.
        """
        if os.path.exists(self._pause_state_path()):
            return True
        entries = self.read_all()
        if not entries:
            return False
        return entries[-1].event_type == "run_paused"

    def get_pause_state(self) -> Optional[RunPauseState]:
        """Read pause_state.json if it exists, else None."""
        try:
            with open(self._pause_state_path(), "r", encoding="utf-8") as fh:
                return RunPauseState.from_json(fh.read())
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog.get_pause_state: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _update_crash_marker(self, entry: EventLogEntry) -> None:
        """Write CrashResumeMarker to crash_resume.json. Best-effort."""
        try:
            marker = CrashResumeMarker(
                run_id=self.run_id,
                last_sequence=entry.sequence,
                last_phase=entry.phase,
                last_driver_id=entry.driver_id,
                checkpoint_at=entry.timestamp,
                resumable=True,
                resume_from_sequence=0,
            )
            with open(self._marker_path, "w", encoding="utf-8") as fh:
                fh.write(marker.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog._update_crash_marker: failed: %s", exc)

    # ------------------------------------------------------------------
    # S1.9: Per-session write locks
    # ------------------------------------------------------------------

    def session_lock(self, session_id: str) -> threading.Lock:
        """Return (or create) a per-session write lock.

        Concurrent delegation sessions can each hold their own lock so
        that their file-write operations don't contend on the global
        ``_lock``.  The global lock is still used for the shared
        sequence counter.

        Parameters
        ----------
        session_id:
            Typically ``DelegationSession.session_id``.  An empty string
            maps to the global ``_lock`` for backward compatibility.

        Returns
        -------
        threading.Lock
            A lock dedicated to *session_id*.
        """
        if not session_id:
            return self._lock
        with self._lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def append_for_session(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
        driver_id: str = "",
        phase: str = "",
    ) -> EventLogEntry:
        """Like ``append`` but acquires the per-session lock for the write.

        The global ``_lock`` is held only for the sequence increment;
        the file write is guarded by the per-session lock.
        """
        with self._lock:
            self._sequence += 1
            seq = self._sequence

        entry = EventLogEntry(
            entry_id=uuid.uuid4().hex,
            run_id=self.run_id,
            sequence=seq,
            event_type=event_type,
            payload=payload,
            timestamp=_utcnow_iso(),
            driver_id=driver_id,
            phase=phase,
        )

        slock = self.session_lock(session_id)
        with slock:
            try:
                with open(self._log_path, "a", encoding="utf-8") as fh:
                    fh.write(entry.to_json_line() + "\n")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EventLog.append_for_session(%s): write failed: %s",
                    session_id, exc,
                )

        self._update_crash_marker(entry)
        return entry

    # ------------------------------------------------------------------
    # Convenience loggers
    # ------------------------------------------------------------------

    def log_phase_transition(
        self,
        phase: str,
        driver_id: str,
        artifacts: list,
        quality_signals: dict,
    ) -> EventLogEntry:
        """Log a PhaseTransitionEvent payload."""
        return self.append(
            "phase_transition",
            {
                "phase": phase,
                "driver_id": driver_id,
                "artifacts": artifacts,
                "quality_signals": quality_signals,
            },
            driver_id=driver_id,
            phase=phase,
        )

    def log_tool_call(
        self,
        tool_id: str,
        server_id: str,
        arguments: dict,
        result_summary: str,
    ) -> EventLogEntry:
        """Log a tool call, redacting sensitive argument keys."""
        return self.append(
            "tool_call",
            {
                "tool_id": tool_id,
                "server_id": server_id,
                "arguments": _redact_dict(arguments),
                "result_summary": result_summary,
            },
        )

    def log_error(
        self,
        driver_id: str,
        error_type: str,
        detail: str,
    ) -> EventLogEntry:
        """Log an error event."""
        return self.append(
            "error",
            {
                "driver_id": driver_id,
                "error_type": error_type,
                "detail": detail,
            },
            driver_id=driver_id,
        )

    def log_mcp_policy_snapshot(self, snapshot_dict: dict) -> EventLogEntry:
        """Persist an MCP policy snapshot as an event + standalone JSON.

        Writes both:
        - An ``mcp_policy_snapshot`` event to events.jsonl
        - A ``mcp_policy_snapshot.json`` file in the log dir

        Parameters
        ----------
        snapshot_dict:
            The result of ``MCPPolicySnapshot.to_dict()``.
        """
        # standalone JSON for easy post-mortem access
        snap_path = os.path.join(self.log_dir, "mcp_policy_snapshot.json")
        try:
            with open(snap_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot_dict, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EventLog.log_mcp_policy_snapshot: write failed: %s", exc,
            )

        return self.append("mcp_policy_snapshot", snapshot_dict)

    def log_envelope_snapshot(
        self,
        envelope: Any,
        event_type: str = "program_envelope_snapshot",
        driver_id: str = "",
        phase: str = "",
    ) -> "EventLogEntry":
        """Persist a ProgramEnvelope state snapshot as an event.

        Writes an ``program_envelope_snapshot`` (or custom *event_type*) event
        to ``events.jsonl`` so the full lifecycle of a program can be
        reconstructed from the log alone.

        Parameters
        ----------
        envelope:
            Any object that exposes ``to_dict()`` — typically a
            :class:`~mica.drivers.program_envelope.ProgramEnvelope`.
        event_type:
            Override the event type string (default ``program_envelope_snapshot``).
        driver_id:
            Optional driver identifier to attach to the event.
        phase:
            Optional phase string to attach to the event.
        """
        try:
            payload = envelope.to_dict() if hasattr(envelope, "to_dict") else dict(envelope)
        except Exception:  # noqa: BLE001
            payload = {"repr": repr(envelope)}
        return self.append(event_type, payload, driver_id=driver_id, phase=phase)


# ---------------------------------------------------------------------------
# EventLogStats + summarize
# ---------------------------------------------------------------------------

@dataclass
class EventLogStats:
    run_id: str
    total_events: int
    phase_count: int
    tool_call_count: int
    error_count: int
    first_timestamp: str
    last_timestamp: str


def summarize(log: EventLog) -> EventLogStats:
    """Read all entries from *log* and return aggregate statistics."""
    entries = log.read_all()
    phase_count = sum(1 for e in entries if e.event_type == "phase_transition")
    tool_call_count = sum(1 for e in entries if e.event_type == "tool_call")
    error_count = sum(1 for e in entries if e.event_type == "error")
    first_ts = entries[0].timestamp if entries else ""
    last_ts = entries[-1].timestamp if entries else ""
    return EventLogStats(
        run_id=log.run_id,
        total_events=len(entries),
        phase_count=phase_count,
        tool_call_count=tool_call_count,
        error_count=error_count,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
    )
