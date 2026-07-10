"""Session closure ledger for delegated agent runs.

The driver already finalizes ``DelegationSession`` and ``ProgramEnvelope``
objects in memory. This contract captures the terminal closure state that
needs to survive cleanup so a run can be reconstructed later from the
persisted envelope snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


def _dedupe_strs(values: Iterable[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _mapping_list(values: Iterable[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in values or ():
        if isinstance(raw, Mapping):
            items.append(dict(raw))
    return items


def _first_nonempty_text(mapping: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        text = str(mapping.get(key) or "").strip()
        if text:
            return text
    return ""


def _collect_residual_gaps(
    role_ctx: Any,
    *,
    final_text: str,
    spawn_exc: Optional[BaseException],
) -> list[str]:
    gaps: list[str] = []

    for issue in list(getattr(role_ctx, "gaps_log", []) or []):
        if isinstance(issue, Mapping):
            text = _first_nonempty_text(
                issue,
                ("gap", "issue", "reason", "detail", "description", "message"),
            )
            if text:
                gaps.append(text)

    for issue in list(getattr(role_ctx, "review_issues", []) or []):
        if isinstance(issue, Mapping):
            text = _first_nonempty_text(
                issue,
                ("issue", "name", "reason", "detail", "description", "message"),
            )
            if text:
                gaps.append(text)

    if spawn_exc is not None:
        gaps.append(f"spawn_exception: {type(spawn_exc).__name__}: {spawn_exc}")

    if not str(final_text or "").strip():
        gaps.append("empty_final_text")

    if getattr(role_ctx, "pending_ledger_entries", None):
        pending_count = len(list(getattr(role_ctx, "pending_ledger_entries", []) or []))
        gaps.append(f"staged_ledger_entries:{pending_count}")

    return _dedupe_strs(gaps)


def _build_reopen_plan(
    *,
    status: str,
    residual_gaps: list[str],
    spawn_exc: Optional[BaseException],
) -> list[str]:
    status_text = str(status or "").strip().lower()
    if status_text == "completed" and not residual_gaps and spawn_exc is None:
        return []

    plan: list[str] = ["restore_session_snapshot"]
    if residual_gaps:
        plan.extend(f"resolve gap: {gap}" for gap in residual_gaps[:3])
    else:
        plan.append("replay_with_new_evidence")

    return _dedupe_strs(plan)


def _build_summary(
    *,
    status: str,
    program_id: str,
    residual_gaps: list[str],
    tombstones: list[dict[str, Any]],
    report_path: str,
) -> str:
    report_text = report_path or "none"
    return (
        f"status={str(status or 'unknown').strip() or 'unknown'} "
        f"program={str(program_id or 'n/a').strip() or 'n/a'} "
        f"gaps={len(residual_gaps)} "
        f"tombstones={len(tombstones)} "
        f"report={report_text}"
    )


@dataclass(frozen=True)
class SessionClosureLedger:
    """Terminal closure snapshot for a delegated session."""

    session_id: str
    run_id: str = ""
    program_id: str = ""
    delegated_agent: str = ""
    status: str = ""
    closed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    residual_gaps: list[str] = field(default_factory=list)
    tombstones: list[dict[str, Any]] = field(default_factory=list)
    archive_handoff: dict[str, Any] = field(default_factory=dict)
    reopen_plan: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionClosureLedger":
        return cls(
            session_id=str(data.get("session_id") or ""),
            run_id=str(data.get("run_id") or ""),
            program_id=str(data.get("program_id") or ""),
            delegated_agent=str(data.get("delegated_agent") or ""),
            status=str(data.get("status") or ""),
            closed_at=str(data.get("closed_at") or datetime.now(timezone.utc).isoformat()),
            residual_gaps=_dedupe_strs(data.get("residual_gaps") or ()),
            tombstones=_mapping_list(data.get("tombstones") or ()),
            archive_handoff=dict(data.get("archive_handoff") or {}),
            reopen_plan=_dedupe_strs(data.get("reopen_plan") or ()),
            summary=str(data.get("summary") or ""),
        )

    @classmethod
    def from_session(
        cls,
        session: Any,
        *,
        program_id: str = "",
        role_ctx: Any = None,
        report_path: str = "",
        final_text: str = "",
        spawn_exc: Optional[BaseException] = None,
    ) -> "SessionClosureLedger":
        status_obj = getattr(session, "status", "")
        status = str(getattr(status_obj, "value", status_obj) or "").strip()
        residual_gaps = _collect_residual_gaps(
            role_ctx,
            final_text=final_text,
            spawn_exc=spawn_exc,
        )
        tombstones = _mapping_list(getattr(role_ctx, "emitted_tombstones", []) or [])
        archive_handoff: dict[str, Any] = {
            "program_id": str(program_id or ""),
            "report_path": str(report_path or ""),
            "result_text": str(final_text or "")[:1000],
            "result_text_length": len(str(final_text or "")),
            "status": status,
            "embodiment_count": int(getattr(role_ctx, "embodiment_count", 0) or 0),
            "iterations_count": int(getattr(role_ctx, "iterations_count", 0) or 0),
            "tool_calls_count": int(getattr(role_ctx, "tool_calls_count", 0) or 0),
        }
        if spawn_exc is not None:
            archive_handoff["error"] = f"{type(spawn_exc).__name__}: {spawn_exc}"

        return cls(
            session_id=str(getattr(session, "session_id", "") or ""),
            run_id=str(getattr(session, "parent_run_id", "") or ""),
            program_id=str(program_id or ""),
            delegated_agent=str(getattr(session, "delegated_agent", "") or ""),
            status=status,
            residual_gaps=residual_gaps,
            tombstones=tombstones,
            archive_handoff=archive_handoff,
            reopen_plan=_build_reopen_plan(
                status=status,
                residual_gaps=residual_gaps,
                spawn_exc=spawn_exc,
            ),
            summary=_build_summary(
                status=status,
                program_id=program_id,
                residual_gaps=residual_gaps,
                tombstones=tombstones,
                report_path=report_path,
            ),
        )