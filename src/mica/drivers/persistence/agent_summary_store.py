from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mica.drivers.persistence.agent_memory import AgentMemoryEntry
from mica.memory.contracts import (
    AgentSummary,
    SummaryMutationAction,
    SummaryMutationEvent,
)

logger = logging.getLogger(__name__)

SUMMARY_BASE_DIR = Path.home() / ".mica" / "agent_summaries"
VALID_SUMMARY_STATUSES = {"active", "paused", "archived", "deleted"}
STATUS_TRANSITIONS = {
    "active": {"paused", "archived", "deleted"},
    "paused": {"active", "archived", "deleted"},
    "archived": {"active", "deleted"},
    "deleted": set(),
}


@dataclass(frozen=True)
class SummaryAccessLog:
    summary_id: str
    action: str
    actor_id: str
    timestamp: datetime
    workspace_id: Optional[str] = None
    user_id: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary_id": self.summary_id,
            "action": self.action,
            "actor_id": self.actor_id,
            "timestamp": self.timestamp.isoformat(),
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "reason": self.reason,
        }


class AgentSummaryStore:
    """Additive typed working-memory store for sub-agent summaries.

    This store is intentionally file-backed and additive so it can coexist with
    the current markdown report export and JSONL AgentMemory paths.
    """

    def __init__(self, *, base_dir: Optional[Path] = None):
        self._base_dir = base_dir or SUMMARY_BASE_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._summaries_path = self._base_dir / "summaries.jsonl"
        self._events_path = self._base_dir / "events.jsonl"
        self._access_path = self._base_dir / "access.jsonl"
        self._cache: Optional[Dict[str, AgentSummary]] = None
        self._events_cache: Optional[List[SummaryMutationEvent]] = None
        self._access_cache: Optional[List[SummaryAccessLog]] = None

    @staticmethod
    def validate_scope(summary: AgentSummary) -> None:
        if not summary.user_id:
            raise ValueError("user_id required")
        if not summary.workspace_id:
            raise ValueError("workspace_id required")
        if not summary.run_id:
            raise ValueError("run_id required")
        if not summary.agent_type:
            raise ValueError("agent_type required")

    @staticmethod
    def validate_event_scope(event: SummaryMutationEvent) -> None:
        scope = event.scope or {}
        if not scope.get("user_id"):
            raise ValueError("event.scope.user_id required")
        if not scope.get("workspace_id"):
            raise ValueError("event.scope.workspace_id required")
        if not scope.get("run_id"):
            raise ValueError("event.scope.run_id required")

    def _load_summaries(self) -> Dict[str, AgentSummary]:
        if self._cache is not None:
            return self._cache
        out: Dict[str, AgentSummary] = {}
        if self._summaries_path.exists():
            try:
                for line in self._summaries_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    out[payload["summary_id"]] = AgentSummary(
                        summary_id=payload["summary_id"],
                        agent_type=payload["agent_type"],
                        user_id=payload["user_id"],
                        workspace_id=payload["workspace_id"],
                        run_id=payload["run_id"],
                        query_summary=payload["query_summary"],
                        synthesis=payload["synthesis"],
                        session_id=payload.get("session_id"),
                        parent_run_id=payload.get("parent_run_id"),
                        citations=payload.get("citations", []),
                        gaps=payload.get("gaps", []),
                        review_issues=payload.get("review_issues", []),
                        artifact_paths=payload.get("artifact_paths", []),
                        status=payload.get("status", "active"),
                        promotion_state=payload.get("promotion_state", "not_promoted"),
                        created_at=datetime.fromisoformat(payload["created_at"]),
                        updated_at=datetime.fromisoformat(payload["updated_at"]),
                        metadata=payload.get("metadata", {}),
                    )
            except Exception as exc:
                logger.warning("Failed to load agent summaries: %s", exc)
        self._cache = out
        return out

    def _load_events(self) -> List[SummaryMutationEvent]:
        if self._events_cache is not None:
            return self._events_cache
        out: List[SummaryMutationEvent] = []
        if self._events_path.exists():
            try:
                for line in self._events_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    out.append(
                        SummaryMutationEvent(
                            event_id=payload["event_id"],
                            summary_id=payload["summary_id"],
                            action=SummaryMutationAction(payload["action"]),
                            actor_type=payload["actor_type"],
                            actor_id=payload["actor_id"],
                            scope=payload["scope"],
                            old_value=payload.get("old_value"),
                            new_value=payload.get("new_value"),
                            reason=payload.get("reason"),
                            timestamp=datetime.fromisoformat(payload["timestamp"]),
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to load agent summary events: %s", exc)
        self._events_cache = out
        return out

    def _load_access_logs(self) -> List[SummaryAccessLog]:
        if self._access_cache is not None:
            return self._access_cache
        out: List[SummaryAccessLog] = []
        if self._access_path.exists():
            try:
                for line in self._access_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    out.append(
                        SummaryAccessLog(
                            summary_id=payload["summary_id"],
                            action=payload["action"],
                            actor_id=payload["actor_id"],
                            timestamp=datetime.fromisoformat(payload["timestamp"]),
                            workspace_id=payload.get("workspace_id"),
                            user_id=payload.get("user_id"),
                            reason=payload.get("reason"),
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to load agent summary access logs: %s", exc)
        self._access_cache = out
        return out

    def _rewrite_summaries(self) -> None:
        summaries = self._load_summaries()
        with self._summaries_path.open("w", encoding="utf-8") as f:
            for summary in summaries.values():
                f.write(json.dumps(summary.to_dict(), ensure_ascii=False) + "\n")

    def _append_event(self, event: SummaryMutationEvent) -> None:
        events = self._load_events()
        events.append(event)
        self._events_cache = events
        with self._events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _append_access(self, log: SummaryAccessLog) -> None:
        logs = self._load_access_logs()
        logs.append(log)
        self._access_cache = logs
        with self._access_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log.to_dict(), ensure_ascii=False) + "\n")

    def apply_mutation(self, summary: AgentSummary, event: SummaryMutationEvent) -> AgentSummary:
        self.validate_scope(summary)
        self.validate_event_scope(event)
        summaries = self._load_summaries()
        current = summaries.get(summary.summary_id)

        if summary.status not in VALID_SUMMARY_STATUSES:
            raise ValueError(f"invalid summary status: {summary.status}")

        if event.action == SummaryMutationAction.DELETE:
            next_summary = replace(current or summary, status="deleted")
        elif event.action == SummaryMutationAction.NONE:
            next_summary = current or summary
        else:
            next_summary = summary

        summaries[next_summary.summary_id] = next_summary
        self._cache = summaries
        self._rewrite_summaries()
        self._append_event(event)
        self._append_access(
            SummaryAccessLog(
                summary_id=next_summary.summary_id,
                action=f"mutation:{event.action.value.lower()}",
                actor_id=event.actor_id,
                workspace_id=next_summary.workspace_id,
                user_id=next_summary.user_id,
                reason=event.reason,
                timestamp=datetime.now(timezone.utc),
            )
        )
        return next_summary

    def get_summary(self, summary_id: str, *, actor_id: str = "system") -> Optional[AgentSummary]:
        out = self._load_summaries().get(summary_id)
        if out is not None:
            self._append_access(
                SummaryAccessLog(
                    summary_id=summary_id,
                    action="read",
                    actor_id=actor_id,
                    workspace_id=out.workspace_id,
                    user_id=out.user_id,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        return out

    def list_active(self, *, workspace_id: Optional[str] = None, agent_type: Optional[str] = None) -> List[AgentSummary]:
        summaries = list(self._load_summaries().values())
        summaries = [s for s in summaries if s.status == "active"]
        if workspace_id is not None:
            summaries = [s for s in summaries if s.workspace_id == workspace_id]
        if agent_type is not None:
            summaries = [s for s in summaries if s.agent_type == agent_type]
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    def list_active_scoped(self, *, user_id: str, workspace_id: str, agent_type: Optional[str] = None) -> List[AgentSummary]:
        if not user_id or not workspace_id:
            raise ValueError("user_id and workspace_id required")
        summaries = self.list_active(workspace_id=workspace_id, agent_type=agent_type)
        summaries = [s for s in summaries if s.user_id == user_id]
        self._append_access(
            SummaryAccessLog(
                summary_id="*",
                action="list_active_scoped",
                actor_id="system",
                workspace_id=workspace_id,
                user_id=user_id,
                timestamp=datetime.now(timezone.utc),
            )
        )
        return summaries

    def update_status(
        self,
        *,
        summary_id: str,
        new_status: str,
        actor_id: str,
        workspace_id: str,
        user_id: str,
        reason: Optional[str] = None,
    ) -> AgentSummary:
        if new_status not in VALID_SUMMARY_STATUSES:
            raise ValueError(f"invalid status: {new_status}")
        current = self.get_summary(summary_id, actor_id=actor_id)
        if current is None:
            raise KeyError(f"summary not found: {summary_id}")
        if current.workspace_id != workspace_id or current.user_id != user_id:
            raise PermissionError("scope mismatch for status update")
        if current.status != new_status and new_status not in STATUS_TRANSITIONS.get(current.status, set()):
            raise ValueError(f"invalid status transition: {current.status} -> {new_status}")

        updated = replace(current, status=new_status, updated_at=datetime.now(timezone.utc))
        event = SummaryMutationEvent(
            event_id=f"{summary_id}-status-{new_status}",
            summary_id=summary_id,
            action=SummaryMutationAction.UPDATE,
            actor_type="user",
            actor_id=actor_id,
            scope={"user_id": user_id, "workspace_id": workspace_id, "run_id": updated.run_id},
            old_value=current.to_dict(),
            new_value=updated.to_dict(),
            reason=reason or f"status_change:{new_status}",
        )
        out = self.apply_mutation(updated, event)
        self._append_access(
            SummaryAccessLog(
                summary_id=summary_id,
                action="status_change",
                actor_id=actor_id,
                workspace_id=workspace_id,
                user_id=user_id,
                reason=reason,
                timestamp=datetime.now(timezone.utc),
            )
        )
        return out

    def safe_delete(
        self,
        *,
        summary_id: str,
        actor_id: str,
        workspace_id: str,
        user_id: str,
        reason: Optional[str] = None,
    ) -> AgentSummary:
        return self.update_status(
            summary_id=summary_id,
            new_status="deleted",
            actor_id=actor_id,
            workspace_id=workspace_id,
            user_id=user_id,
            reason=reason or "safe_delete",
        )

    def history_for(self, summary_id: str) -> List[SummaryMutationEvent]:
        return [e for e in self._load_events() if e.summary_id == summary_id]

    def list_access_logs(self, *, summary_id: Optional[str] = None, workspace_id: Optional[str] = None) -> List[SummaryAccessLog]:
        logs = list(self._load_access_logs())
        if summary_id is not None:
            logs = [l for l in logs if l.summary_id == summary_id]
        if workspace_id is not None:
            logs = [l for l in logs if l.workspace_id == workspace_id]
        return logs

    @staticmethod
    def from_agent_memory_entry(
        entry: AgentMemoryEntry,
        *,
        summary_id: str,
        user_id: str,
        workspace_id: str,
        run_id: str,
        session_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        artifact_paths: Optional[List[str]] = None,
    ) -> AgentSummary:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(entry.timestamp, tz=timezone.utc)
        return AgentSummary(
            summary_id=summary_id,
            agent_type=entry.agent_name,
            user_id=user_id,
            workspace_id=workspace_id,
            run_id=run_id,
            query_summary=entry.query_summary,
            synthesis=entry.synthesis,
            session_id=session_id,
            parent_run_id=parent_run_id,
            citations=entry.citations,
            gaps=entry.gaps,
            review_issues=entry.review_issues,
            artifact_paths=artifact_paths or [],
            created_at=dt,
            updated_at=dt,
            metadata=entry.metadata,
        )